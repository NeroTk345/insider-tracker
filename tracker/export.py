"""Export the database as static JSON + dashboard, for GitHub Pages."""
from __future__ import annotations

import json
import os
import re
import shutil

from . import queries
from .db import DB

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")

# columns the dashboard uses (keeps the JSON small)
TX_COLS = ["accession", "issuer_cik", "issuer_name", "ticker", "owner_cik", "owner_name", "roles", "is_officer",
           "is_director", "tx_date", "code", "shares", "price", "value", "shares_after", "derivative",
           "plan_10b5_1", "filing_date", "form", "suspect"]


def _write(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, default=str, separators=(",", ":"), ensure_ascii=False)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def export(db: DB, cfg: dict, out: str) -> dict:
    ecfg = cfg.get("export", {})
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    shutil.copy(os.path.join(WEB_DIR, "index.html"), os.path.join(out, "index.html"))
    with open(os.path.join(out, "static.js"), "w") as f:
        f.write("window.STATIC_MODE = true;\n")
    open(os.path.join(out, ".nojekyll"), "w").close()
    d = os.path.join(out, "data")

    watch = [r["ticker"] for r in db.q("SELECT ticker FROM watchlist ORDER BY ticker")]
    min_sale = float(ecfg.get("min_sale_value", 100_000))
    wl_sql = f"OR (ticker IN ({','.join('?' * len(watch))}) AND tx_date >= ?)" if watch else ""
    txs = db.q(
        f"""SELECT {','.join(TX_COLS)} FROM insider_tx WHERE derivative = 0 AND (
              (code = 'P' AND tx_date >= ?)
           OR (code = 'S' AND value >= ? AND tx_date >= ?)
           {wl_sql})
           ORDER BY tx_date DESC, filing_date DESC, id DESC""",
        [queries.days_ago(ecfg.get("purchase_days", 365)), min_sale, queries.days_ago(ecfg.get("sale_days", 90))]
        + (watch + [queries.days_ago(730)] if watch else []))
    _write(os.path.join(d, "transactions.json"), {
        "min_sale_value": min_sale, "rows": [[r[c] for c in TX_COLS] for r in txs], "cols": TX_COLS})

    last = db.get_meta("last_sync", {}) or {}
    _write(os.path.join(d, "stats.json"), {**queries.stats(db), "static": True, "sync": {
        "running": False, "last_run": last.get("time"), "last_counts": last.get("counts", {}),
        "warnings": last.get("warnings", []), "last_error": last.get("error")}})
    _write(os.path.join(d, "alerts.json"), db.q("SELECT * FROM alerts ORDER BY created DESC LIMIT 200"))
    _write(os.path.join(d, "watchlist.json"), db.q(
        "SELECT w.ticker, t.title FROM watchlist w LEFT JOIN tickers t ON t.ticker = w.ticker ORDER BY w.ticker"))
    _write(os.path.join(d, "funds.json"), queries.funds_overview(db))

    for f in db.q("SELECT * FROM funds"):
        periods = [x["period"] for x in queries.fund_filings(db, f["cik"])]
        for i, p in enumerate(periods):
            data = queries.fund_changes(db, f["cik"], p)
            data["fund"] = f
            data["periods"] = periods
            _write(os.path.join(d, "fund", f"{f['cik']}_{p}.json"), data)
            if i == 0:
                _write(os.path.join(d, "fund", f"{f['cik']}.json"), data)

    tickers = set(watch) | {r["ticker"] for r in txs if r["ticker"]} | {
        r["ticker"] for r in db.q("SELECT DISTINCT ticker FROM f13_holdings WHERE ticker != ''")}
    cache = {f["cik"]: queries.fund_changes(db, f["cik"]) for f in db.q("SELECT cik FROM funds")}
    for t in tickers:
        _write(os.path.join(d, "stock", _safe(t) + ".json"), queries.stock_detail(db, t, cache))
    titles = {r["ticker"]: r["title"] for r in db.q("SELECT ticker, title FROM tickers")}
    _write(os.path.join(d, "search.json"), sorted([[t, titles.get(t, "")] for t in tickers]))
    return {"transactions": len(txs), "stocks": len(tickers), "out": out}
