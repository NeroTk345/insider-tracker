"""SQLite storage."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tickers (
    cik INTEGER, ticker TEXT PRIMARY KEY, title TEXT, norm TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickers_cik ON tickers(cik);
CREATE INDEX IF NOT EXISTS ix_tickers_norm ON tickers(norm);

CREATE TABLE IF NOT EXISTS watchlist (ticker TEXT PRIMARY KEY, added TEXT DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE IF NOT EXISTS funds (
    cik INTEGER PRIMARY KEY, label TEXT, name TEXT, added TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS form4_filings (
    accession TEXT PRIMARY KEY, issuer_cik INTEGER, filing_date TEXT,
    status TEXT, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS insider_tx (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession TEXT, seq INTEGER,
    issuer_cik INTEGER, issuer_name TEXT, ticker TEXT,
    owner_cik INTEGER, owner_name TEXT, roles TEXT,
    is_officer INTEGER, is_director INTEGER,
    security TEXT, tx_date TEXT, code TEXT, acq_disp TEXT,
    shares REAL, price REAL, value REAL, shares_after REAL,
    direct INTEGER, derivative INTEGER, plan_10b5_1 INTEGER,
    filing_date TEXT,
    UNIQUE(accession, seq)
);
CREATE INDEX IF NOT EXISTS ix_tx_ticker ON insider_tx(ticker);
CREATE INDEX IF NOT EXISTS ix_tx_date ON insider_tx(tx_date);
CREATE INDEX IF NOT EXISTS ix_tx_code ON insider_tx(code);
CREATE INDEX IF NOT EXISTS ix_tx_issuer ON insider_tx(issuer_cik);

CREATE TABLE IF NOT EXISTS f13_filings (
    accession TEXT PRIMARY KEY, fund_cik INTEGER, period TEXT, filing_date TEXT,
    positions INTEGER, total_value REAL
);
CREATE INDEX IF NOT EXISTS ix_f13_fund ON f13_filings(fund_cik, period);

CREATE TABLE IF NOT EXISTS f13_holdings (
    accession TEXT, fund_cik INTEGER, period TEXT,
    cusip TEXT, name TEXT, title TEXT, put_call TEXT,
    shares REAL, value REAL, ticker TEXT,
    PRIMARY KEY (accession, cusip, put_call)
);
CREATE INDEX IF NOT EXISTS ix_h_cusip ON f13_holdings(cusip);
CREATE INDEX IF NOT EXISTS ix_h_ticker ON f13_holdings(ticker);

CREATE TABLE IF NOT EXISTS form4_queue (
    accession TEXT PRIMARY KEY, cik INTEGER, form TEXT, filing_date TEXT,
    added TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS f13_seen (
    accession TEXT PRIMARY KEY, fund_cik INTEGER, period TEXT, form TEXT, filing_date TEXT,
    amendment_type TEXT
);

CREATE TABLE IF NOT EXISTS cusip_map (
    cusip TEXT PRIMARY KEY, ticker TEXT, source TEXT, updated TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP,
    kind TEXT, title TEXT, body TEXT, ticker TEXT, url TEXT, sent INTEGER DEFAULT 0
);
"""


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self._migrate()

    # columns added after the first version
    MIGRATIONS = {
        "insider_tx": [("form", "TEXT DEFAULT '4'"), ("suspect", "TEXT")],
        "form4_filings": [("form", "TEXT DEFAULT '4'")],
        "f13_filings": [("amendments", "TEXT"), ("value_note", "TEXT")],
    }

    def _migrate(self):
        for table, cols in self.MIGRATIONS.items():
            have = {r["name"] for r in self.q(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.x(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def q(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, params=()):
        with self._write_lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def xmany(self, sql: str, rows):
        with self._write_lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def get_meta(self, key: str, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_meta(self, key: str, value):
        self.x("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))
