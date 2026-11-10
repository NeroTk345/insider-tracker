"""Alert rules and notification channels (Telegram, ntfy, console)."""
from __future__ import annotations

import hashlib
import logging
import urllib.parse
import urllib.request

from . import queries
from .db import DB

log = logging.getLogger("tracker.alerts")


def money(v) -> str:
    v = float(v or 0)
    for unit, div, dec in (("mld", 1e9, 2), ("mln", 1e6, 2), ("k", 1e3, 0)):
        if abs(v) >= div:
            return f"${v / div:,.{dec}f} {unit}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"${v:,.0f}".replace(",", ".")


def filing_url(cik, accession) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}/{accession}-index.htm")


def _add(db: DB, key, kind, title, body, ticker="", url="") -> dict | None:
    cur = db.x("INSERT OR IGNORE INTO alerts(key, kind, title, body, ticker, url) VALUES(?,?,?,?,?,?)",
               (key, kind, title, body, ticker, url))
    if cur.rowcount:
        return {"key": key, "kind": kind, "title": title, "body": body, "ticker": ticker, "url": url}
    return None


def evaluate(db: DB, cfg: dict) -> list[dict]:
    a = cfg.get("alerts", {})
    max_age = int(a.get("max_age_days", 3))          # don't alert on old history (first sync)
    recent = queries.days_ago(max_age)
    watch = {r["ticker"] for r in db.q("SELECT ticker FROM watchlist")}
    new: list[dict] = []

    # 1) Purchases: big ones anywhere, any size on the watchlist
    min_buy = float(a.get("min_purchase_value", 250_000))
    for r in queries.filings_summary(db, "P", days=max_age + 10, limit=1000):
        if (r["filing_date"] or "") < recent:
            continue
        on_watch = r["ticker"] in watch
        big = (r["value"] or 0) >= min_buy
        if not (big or (on_watch and a.get("watchlist_any_purchase", True))):
            continue
        if a.get("skip_10b5_1_plans", False) and r["plan_10b5_1"]:
            continue
        stake = f" (+{r['stake_change_pct']}% belang)" if r["stake_change_pct"] else ""
        title = f"Insider koopt {r['ticker'] or r['issuer_name']}: {money(r['value'])}"
        if r["suspect"]:
            title += " (controleer)"
        body = (f"{r['owner_name']} ({r['roles'] or 'insider'}) kocht {r['shares']:,.0f} aandelen "
                f"{r['issuer_name']} à ~${r['avg_price'] or 0:,.2f}{stake} op {r['last_date']}.")
        if on_watch:
            body += " [watchlist]"
        if r["suspect"]:
            body += f" Let op: {r['suspect']}, check de originele filing."
        if r["form"] == "4/A":
            body += " (gecorrigeerde melding)"
        x = _add(db, f"buy:{r['accession']}", "buy", title, body, r["ticker"],
                 filing_url(r["issuer_cik"], r["accession"]))
        if x:
            new.append(x)

    # 2) Big sales on the watchlist (optional)
    min_sell = float(a.get("min_watchlist_sale_value", 0) or 0)
    if min_sell > 0 and watch:
        for r in queries.filings_summary(db, "S", days=max_age + 10, min_value=min_sell, tickers=list(watch)):
            if (r["filing_date"] or "") < recent:
                continue
            x = _add(db, f"sell:{r['accession']}", "sell",
                     f"Insider verkoopt {r['ticker']}: {money(r['value'])}",
                     f"{r['owner_name']} ({r['roles'] or 'insider'}) verkocht {r['shares']:,.0f} aandelen "
                     f"op {r['last_date']}." + (" (10b5-1 plan)" if r["plan_10b5_1"] else ""),
                     r["ticker"], filing_url(r["issuer_cik"], r["accession"]))
            if x:
                new.append(x)

    # 3) Cluster buys: several insiders of the same company buying
    for c in queries.clusters(db, days=int(a.get("cluster_days", 14)),
                              min_insiders=int(a.get("cluster_min_insiders", 3))):
        if c["last_date"] < recent:
            continue
        owners_key = ",".join(sorted((c["owner_ciks"] or "").split(",")))
        h = hashlib.sha1(owners_key.encode()).hexdigest()[:10]
        x = _add(db, f"cluster:{c['issuer_cik']}:{h}", "cluster",
                 f"Cluster-aankoop {c['ticker'] or c['issuer_name']}: {c['insiders']} insiders",
                 f"{c['insiders']} insiders kochten samen {money(c['value'])} tussen {c['first_date']} en "
                 f"{c['last_date']}: {', '.join(c['owners'][:6])}.", c["ticker"])
        if x:
            new.append(x)

    # 4) Funds: new quarter filed -> summarize notable moves
    f_age = queries.days_ago(int(a.get("fund_max_age_days", 14)))
    min_w = float(a.get("fund_min_weight", 0.5))
    inc_pct = float(a.get("fund_increase_pct", 50))
    for f in db.q("SELECT * FROM funds"):
        ch = queries.fund_changes(db, f["cik"])
        fl = ch["filing"]
        if not fl or not ch["previous"] or fl["filing_date"] < f_age:
            continue
        moves = []
        for hd in ch["holdings"]:
            label = hd.get("ticker") or hd["name"]
            if hd["put_call"]:
                label += f" ({hd['put_call'].lower()})"
            if hd["status"] == "nieuw" and hd["weight"] >= min_w:
                moves.append(f"NIEUW {label} {money(hd['value'])} ({hd['weight']}%)")
            elif hd["status"] == "bijgekocht" and (hd["change_pct"] or 0) >= inc_pct and hd["weight"] >= min_w:
                moves.append(f"+{hd['change_pct']:.0f}% {label} ({hd['weight']}%)")
            elif hd["status"] == "verkocht" and hd["prev_value"] >= min_w / 100 * (ch["previous"]["total_value"] or 0):
                moves.append(f"VERKOCHT {label}")
        if not moves:
            continue
        name = f["label"] or f["name"]
        x = _add(db, f"13f:{fl['accession']}", "fund",
                 f"{name}: nieuwe 13F ({fl['period']})",
                 "; ".join(moves[:12]) + (f" … +{len(moves) - 12} meer" if len(moves) > 12 else ""),
                 "", filing_url(f["cik"], fl["accession"]))
        if x:
            new.append(x)
    return new


# ---- channels ---------------------------------------------------------------

def _post(url, data: bytes, headers: dict):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def send(cfg: dict, title: str, body: str, url: str = "") -> bool:
    n = cfg.get("notify", {})
    ok = True
    print(f"[ALERT] {title}\n        {body}" + (f"\n        {url}" if url else ""))
    if n.get("telegram_bot_token") and n.get("telegram_chat_id"):
        text = f"<b>{_esc(title)}</b>\n{_esc(body)}" + (f'\n<a href="{url}">SEC filing</a>' if url else "")
        try:
            _post(f"https://api.telegram.org/bot{n['telegram_bot_token']}/sendMessage",
                  urllib.parse.urlencode({"chat_id": n["telegram_chat_id"], "text": text,
                                          "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode(),
                  {"Content-Type": "application/x-www-form-urlencoded"})
        except Exception as e:
            log.warning("Telegram mislukt: %s", e); ok = False
    if n.get("ntfy_topic"):
        server = n.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "chart_with_upwards_trend"}
        if url:
            headers["Click"] = url
        try:
            _post(f"{server}/{n['ntfy_topic']}", body.encode(), headers)
        except Exception as e:
            log.warning("ntfy mislukt: %s", e); ok = False
    return ok


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify(db: DB, cfg: dict) -> int:
    pending = db.q("SELECT * FROM alerts WHERE sent = 0 ORDER BY created")
    limit = int(cfg.get("alerts", {}).get("max_per_run", 15))
    sent = 0
    for al in pending[:limit]:
        if send(cfg, al["title"], al["body"], al["url"]):
            db.x("UPDATE alerts SET sent = 1 WHERE key = ?", (al["key"],))
            sent += 1
    rest = pending[limit:]
    if rest:
        send(cfg, f"+{len(rest)} extra alerts", "Bekijk ze in het dashboard (tab Alerts).")
        db.xmany("UPDATE alerts SET sent = 1 WHERE key = ?", [(r["key"],) for r in rest])
    return sent
