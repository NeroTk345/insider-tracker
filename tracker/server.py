"""Tiny JSON API + static dashboard, stdlib only."""
from __future__ import annotations

import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import queries
from .db import DB
from .sync import Syncer

log = logging.getLogger("tracker.server")
WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")


def make_handler(db: DB, syncer: Syncer, cfg: dict):
    token = (cfg.get("dashboard_password") or "").strip()

    class H(BaseHTTPRequestHandler):
        server_version = "InsiderTracker/1.0"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        # ---- helpers
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def json(self, data, code=200):
            self._send(code, json.dumps(data, default=str).encode(), "application/json; charset=utf-8")

        def _auth_ok(self) -> bool:
            if not token:
                return True
            if self.headers.get("X-Token") == token:
                return True
            return f"tk={token}" in (self.headers.get("Cookie") or "")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # ---- routes
        def do_GET(self):
            u = urlparse(self.path)
            qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
            p = u.path
            if not p.startswith("/api/"):
                return self._static(p)
            if not self._auth_ok():
                return self.json({"error": "auth"}, 401)
            try:
                if p == "/api/stats":
                    return self.json({**queries.stats(db), "sync": syncer.status, "static": False})
                if p == "/api/transactions":
                    return self.json(queries.insider_transactions(
                        db, ticker=qs.get("ticker"), code=qs.get("code"), min_value=qs.get("min_value"),
                        days=int(qs.get("days", 30)), role=qs.get("role"), owner_cik=qs.get("owner"),
                        include_derivative=qs.get("derivative") == "1", limit=int(qs.get("limit", 300))))
                if p == "/api/top":
                    tick = [r["ticker"] for r in db.q("SELECT ticker FROM watchlist")] if qs.get("watchlist") == "1" else None
                    return self.json(queries.filings_summary(
                        db, code=qs.get("code", "P"), days=int(qs.get("days", 30)),
                        min_value=float(qs.get("min_value", 0)), tickers=tick, limit=int(qs.get("limit", 100))))
                if p == "/api/clusters":
                    return self.json(queries.clusters(db, days=int(qs.get("days", 30)),
                                                      min_insiders=int(qs.get("min", 2))))
                if p == "/api/funds":
                    return self.json(queries.funds_overview(db))
                m = re.fullmatch(r"/api/funds/(\d+)", p)
                if m:
                    data = queries.fund_changes(db, int(m[1]), qs.get("period"))
                    data["fund"] = db.one("SELECT * FROM funds WHERE cik = ?", (int(m[1]),))
                    data["periods"] = [f["period"] for f in queries.fund_filings(db, int(m[1]))]
                    return self.json(data)
                m = re.fullmatch(r"/api/stock/([A-Za-z0-9.\-]+)", p)
                if m:
                    return self.json(queries.stock_detail(db, m[1]))
                if p == "/api/alerts":
                    return self.json(db.q("SELECT * FROM alerts ORDER BY created DESC LIMIT 200"))
                if p == "/api/watchlist":
                    return self.json(db.q("SELECT w.ticker, t.title FROM watchlist w "
                                          "LEFT JOIN tickers t ON t.ticker = w.ticker ORDER BY w.ticker"))
                if p == "/api/search":
                    term = (qs.get("q") or "").upper()
                    return self.json(db.q("SELECT ticker, title, cik FROM tickers WHERE ticker LIKE ? OR "
                                          "UPPER(title) LIKE ? ORDER BY ticker = ? DESC, LENGTH(ticker) LIMIT 10",
                                          (term + "%", f"%{term}%", term)))
                return self.json({"error": "not found"}, 404)
            except Exception as e:
                log.exception("API fout")
                return self.json({"error": str(e)}, 500)

        def do_POST(self):
            p = urlparse(self.path).path
            if p == "/api/login":
                b = self._body()
                if not token or b.get("password") == token:
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"tk={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                return self.json({"error": "fout wachtwoord"}, 401)
            if not self._auth_ok():
                return self.json({"error": "auth"}, 401)
            try:
                b = self._body()
                if p == "/api/sync":
                    if syncer.status["running"]:
                        return self.json({"ok": False, "msg": "Sync draait al"})
                    syncer.run_in_background(
                        insiders=b.get("insiders", True), market=b.get("market", True),
                        funds=b.get("funds", True))
                    return self.json({"ok": True})
                if p == "/api/watchlist":
                    t = (b.get("ticker") or "").upper().strip()
                    if not re.fullmatch(r"[A-Z0-9.\-]{1,10}", t):
                        return self.json({"error": "ongeldige ticker"}, 400)
                    if b.get("remove"):
                        db.x("DELETE FROM watchlist WHERE ticker = ?", (t,))
                    else:
                        if not db.one("SELECT 1 x FROM tickers WHERE ticker = ?", (t,)) and \
                                db.one("SELECT 1 x FROM tickers LIMIT 1"):
                            return self.json({"error": f"{t} niet gevonden bij de SEC"}, 400)
                        db.x("INSERT OR IGNORE INTO watchlist(ticker) VALUES(?)", (t,))
                    return self.json({"ok": True})
                if p == "/api/funds":
                    cik = int(re.sub(r"\D", "", str(b.get("cik") or "0")) or 0)
                    if not cik:
                        return self.json({"error": "ongeldige CIK"}, 400)
                    if b.get("remove"):
                        syncer._delete_fund(cik)
                    else:
                        db.x("INSERT OR IGNORE INTO funds(cik, label) VALUES(?, ?)", (cik, b.get("label") or None))
                    return self.json({"ok": True})
                if p == "/api/test-alert":
                    from .alerts import send
                    ok = send(cfg, "Test-alert Insider Tracker", "Als je dit ziet werken je meldingen.")
                    return self.json({"ok": ok})
                return self.json({"error": "not found"}, 404)
            except Exception as e:
                log.exception("API fout")
                return self.json({"error": str(e)}, 500)

        def _static(self, p: str):
            if p in ("", "/"):
                p = "/index.html"
            path = os.path.normpath(os.path.join(WEB_DIR, p.lstrip("/")))
            if not path.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(path):
                return self._send(404, b"not found", "text/plain")
            ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                     ".svg": "image/svg+xml", ".png": "image/png"}.get(os.path.splitext(path)[1], "application/octet-stream")
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)

    return H


def serve(db: DB, syncer: Syncer, cfg: dict):
    host, port = cfg.get("host", "0.0.0.0"), int(cfg.get("port", 8090))
    httpd = ThreadingHTTPServer((host, port), make_handler(db, syncer, cfg))
    log.info("Dashboard op http://%s:%d", "localhost" if host == "0.0.0.0" else host, port)
    httpd.serve_forever()
