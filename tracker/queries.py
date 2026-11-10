"""Read-side queries shared by the API and the alert engine."""
from __future__ import annotations

import datetime as dt

from .db import DB


def days_ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=int(n))).isoformat()


def insider_transactions(db: DB, ticker=None, code=None, min_value=None, days=90,
                         role=None, owner_cik=None, include_derivative=False, limit=300) -> list[dict]:
    sql = ["SELECT * FROM insider_tx WHERE tx_date >= ?"]
    p: list = [days_ago(days)]
    if not include_derivative:
        sql.append("AND derivative = 0")
    if ticker:
        sql.append("AND ticker = ?"); p.append(ticker.upper())
    if code:
        codes = [c.strip().upper() for c in str(code).split(",") if c.strip()]
        sql.append(f"AND code IN ({','.join('?' * len(codes))})"); p += codes
    if min_value:
        sql.append("AND value >= ?"); p.append(float(min_value))
    if role == "officer":
        sql.append("AND is_officer = 1")
    elif role == "director":
        sql.append("AND is_director = 1")
    elif role == "ceo":
        sql.append("AND (roles LIKE '%CEO%' OR roles LIKE '%Chief Executive%')")
    if owner_cik:
        sql.append("AND owner_cik = ?"); p.append(int(owner_cik))
    sql.append("ORDER BY tx_date DESC, filing_date DESC, id DESC LIMIT ?"); p.append(int(limit))
    return db.q(" ".join(sql), p)


def filings_summary(db: DB, code="P", days=30, min_value=0, tickers=None, limit=100) -> list[dict]:
    """One row per filing (accession): summed value of all lines with that code."""
    sql = """
        SELECT accession, issuer_cik, issuer_name, ticker, owner_cik, owner_name, roles,
               MAX(is_officer) is_officer, MAX(is_director) is_director, MAX(plan_10b5_1) plan_10b5_1,
               MAX(suspect) suspect, MAX(form) form,
               MIN(tx_date) first_date, MAX(tx_date) last_date, MAX(filing_date) filing_date,
               SUM(shares) shares, SUM(value) value,
               CASE WHEN SUM(shares) > 0 THEN SUM(value) / SUM(shares) END avg_price,
               MAX(shares_after) max_after, MIN(shares_after) min_after
        FROM insider_tx
        WHERE derivative = 0 AND code = ? AND tx_date >= ?
    """
    p: list = [code, days_ago(days)]
    if tickers:
        sql += f" AND ticker IN ({','.join('?' * len(tickers))})"
        p += [t.upper() for t in tickers]
    sql += " GROUP BY accession HAVING COALESCE(SUM(value),0) >= ? ORDER BY value DESC LIMIT ?"
    p += [float(min_value or 0), int(limit)]
    rows = db.q(sql, p)
    for r in rows:
        if code == "P":
            r["shares_after"] = r["max_after"]
            before = (r["max_after"] or 0) - (r["shares"] or 0)
        else:
            r["shares_after"] = r["min_after"]
            before = (r["min_after"] or 0) + (r["shares"] or 0)
        r["stake_change_pct"] = round(100 * (r["shares"] or 0) / before, 1) if before > 0 else None
    return rows


def clusters(db: DB, days=14, min_insiders=3, limit=50) -> list[dict]:
    rows = db.q(
        """
        SELECT issuer_cik, MAX(issuer_name) issuer_name, MAX(ticker) ticker,
               COUNT(DISTINCT owner_cik) insiders, SUM(value) value,
               MIN(tx_date) first_date, MAX(tx_date) last_date,
               GROUP_CONCAT(DISTINCT owner_name) owners,
               GROUP_CONCAT(DISTINCT owner_cik) owner_ciks
        FROM insider_tx
        WHERE derivative = 0 AND code = 'P' AND tx_date >= ?
        GROUP BY issuer_cik HAVING COUNT(DISTINCT owner_cik) >= ?
        ORDER BY insiders DESC, value DESC LIMIT ?
        """,
        (days_ago(days), int(min_insiders), int(limit)),
    )
    for r in rows:
        r["owners"] = sorted(set((r["owners"] or "").split(",")))
    return rows


def fund_filings(db: DB, fund_cik: int) -> list[dict]:
    return db.q(
        "SELECT * FROM f13_filings WHERE fund_cik = ? ORDER BY period DESC, filing_date DESC",
        (int(fund_cik),),
    )


def fund_changes(db: DB, fund_cik: int, period: str | None = None) -> dict:
    filings = fund_filings(db, fund_cik)
    if not filings:
        return {"filing": None, "previous": None, "holdings": []}
    idx = 0
    if period:
        idx = next((i for i, f in enumerate(filings) if f["period"] == period), 0)
    cur = filings[idx]
    prev = filings[idx + 1] if idx + 1 < len(filings) else None

    def load(acc):
        return {(h["cusip"], h["put_call"]): h for h in
                db.q("SELECT * FROM f13_holdings WHERE accession = ?", (acc,))}

    now = load(cur["accession"])
    before = load(prev["accession"]) if prev else {}
    total = sum(h["value"] for h in now.values()) or 1.0
    out = []
    for key in set(now) | set(before):
        c, b = now.get(key), before.get(key)
        h = dict(c or b)
        h["shares"] = c["shares"] if c else 0
        h["value"] = c["value"] if c else 0
        h["prev_shares"] = b["shares"] if b else 0
        h["prev_value"] = b["value"] if b else 0
        h["weight"] = round(100 * h["value"] / total, 2)
        if not prev:
            h["status"] = "onbekend"; h["change_pct"] = None
        elif c and not b:
            h["status"] = "nieuw"; h["change_pct"] = None
        elif b and not c:
            h["status"] = "verkocht"; h["change_pct"] = -100.0
        else:
            diff = h["shares"] - h["prev_shares"]
            h["change_pct"] = round(100 * diff / h["prev_shares"], 1) if h["prev_shares"] else None
            if abs(diff) < 1e-9 or (h["change_pct"] is not None and abs(h["change_pct"]) < 0.5):
                h["status"] = "gelijk"
            else:
                h["status"] = "bijgekocht" if diff > 0 else "afgebouwd"
        out.append(h)
    out.sort(key=lambda h: (h["value"], h["prev_value"]), reverse=True)
    return {"filing": cur, "previous": prev, "holdings": out}


def funds_overview(db: DB) -> list[dict]:
    funds = db.q("SELECT * FROM funds ORDER BY COALESCE(label, name)")
    for f in funds:
        fl = fund_filings(db, f["cik"])
        f["latest"] = fl[0] if fl else None
        f["quarters"] = len(fl)
    return funds


def stock_detail(db: DB, ticker: str, fund_cache: dict | None = None) -> dict:
    t = ticker.upper()
    info = db.one("SELECT * FROM tickers WHERE ticker = ?", (t,))
    txs = insider_transactions(db, ticker=t, days=730, limit=500)
    buys = filings_summary(db, "P", days=365, tickers=[t])
    sells = filings_summary(db, "S", days=365, tickers=[t])
    holders = []
    for f in db.q("SELECT * FROM funds"):
        ch = fund_cache[f["cik"]] if fund_cache is not None else fund_changes(db, f["cik"])
        if not ch["filing"]:
            continue
        for h in ch["holdings"]:
            if h.get("ticker") == t and h["put_call"] == "":
                holders.append({"fund_cik": f["cik"], "fund": f["label"] or f["name"],
                                "period": ch["filing"]["period"], **{k: h[k] for k in
                                ("shares", "value", "weight", "status", "change_pct", "name")}})
    return {
        "ticker": t, "info": info, "transactions": txs,
        "buy_total_12m": sum(b["value"] or 0 for b in buys),
        "sell_total_12m": sum(s["value"] or 0 for s in sells),
        "buyers_12m": len({b["owner_cik"] for b in buys}),
        "sellers_12m": len({s["owner_cik"] for s in sells}),
        "fund_holders": sorted(holders, key=lambda h: -h["value"]),
    }


def stats(db: DB) -> dict:
    return {
        "transactions": db.one("SELECT COUNT(*) n FROM insider_tx")["n"],
        "filings": db.one("SELECT COUNT(*) n FROM form4_filings WHERE status='ok'")["n"],
        "companies": db.one("SELECT COUNT(DISTINCT issuer_cik) n FROM insider_tx")["n"],
        "buys_30d": db.one("SELECT COUNT(DISTINCT accession) n FROM insider_tx WHERE code='P' AND derivative=0 AND tx_date>=?", (days_ago(30),))["n"],
        "funds": db.one("SELECT COUNT(*) n FROM funds")["n"],
        "f13_filings": db.one("SELECT COUNT(*) n FROM f13_filings")["n"],
        "alerts": db.one("SELECT COUNT(*) n FROM alerts")["n"],
        "latest_filing": db.one("SELECT MAX(filing_date) d FROM insider_tx")["d"],
    }
