"""Fetch SEC data into the database, with data-quality checks."""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
import threading
import time
import traceback

from . import sec
from .db import DB
from .figi import Figi

log = logging.getLogger("tracker.sync")

# A real 13F filer manages >= $100M. A table this small after 2023 almost
# certainly still reports in thousands (a common filer mistake).
MIN_PLAUSIBLE_13F_TOTAL = 20_000_000


class Syncer:
    def __init__(self, db: DB, client: sec.SecClient, cfg: dict, figi: Figi | None = None):
        self.db = db
        self.client = client
        self.cfg = cfg
        fcfg = cfg.get("openfigi", {})
        self.figi = figi if figi is not None else (Figi(fcfg.get("api_key", "")) if fcfg.get("enabled", True) else None)
        self.lock = threading.Lock()
        self.status = {"running": False, "phase": "idle", "detail": "", "last_run": None,
                       "last_error": None, "last_counts": {}, "warnings": []}

    # ---- setup ------------------------------------------------------------

    def seed(self):
        """Load watchlist/funds from config. On first start always; on every
        start when config_is_source is set (GitHub Pages mode)."""
        source = bool(self.cfg.get("config_is_source"))
        if self.db.get_meta("seeded") and not source:
            return
        wl = [t.upper().strip() for t in self.cfg.get("watchlist", [])]
        funds = {int(f["cik"]): f.get("label") for f in self.cfg.get("funds", [])}
        if source:
            if wl:
                self.db.x(f"DELETE FROM watchlist WHERE ticker NOT IN ({','.join('?' * len(wl))})", wl)
            else:
                self.db.x("DELETE FROM watchlist")
            for cik in {r["cik"] for r in self.db.q("SELECT cik FROM funds")} - set(funds):
                self._delete_fund(cik)
        for t in wl:
            self.db.x("INSERT OR IGNORE INTO watchlist(ticker) VALUES(?)", (t,))
        for cik, label in funds.items():
            self.db.x("INSERT OR IGNORE INTO funds(cik, label) VALUES(?, ?)", (cik, label))
            if source and label:
                self.db.x("UPDATE funds SET label = ? WHERE cik = ?", (label, cik))
        self.db.set_meta("seeded", True)

    def _delete_fund(self, cik: int):
        for t in ("f13_holdings", "f13_filings", "f13_seen"):
            self.db.x(f"DELETE FROM {t} WHERE fund_cik = ?", (cik,))
        self.db.x("DELETE FROM funds WHERE cik = ?", (cik,))

    def refresh_tickers(self, max_age_hours: int = 24):
        last = self.db.get_meta("tickers_updated", 0)
        if time.time() - last < max_age_hours * 3600 and self.db.one("SELECT 1 x FROM tickers LIMIT 1"):
            return
        self._phase("tickers", "Tickerlijst ophalen")
        rows = self.client.company_tickers()
        self.db.x("DELETE FROM tickers")
        self.db.xmany("INSERT OR REPLACE INTO tickers(cik, ticker, title, norm) VALUES(?,?,?,?)",
                      [(r["cik"], r["ticker"], r["title"], sec.normalize_name(r["title"])) for r in rows])
        self.db.set_meta("tickers_updated", time.time())
        norm_map: dict[str, str] = {}
        for r in rows:   # company_tickers.json is roughly ordered by size; keep the first per name
            norm_map.setdefault(sec.normalize_name(r["title"]), r["ticker"])
        self.db.set_meta("norm_map", norm_map)

    def cik_for(self, ticker: str) -> int | None:
        r = self.db.one("SELECT cik FROM tickers WHERE ticker = ?", (ticker.upper(),))
        return r["cik"] if r else None

    def _phase(self, phase: str, detail: str = ""):
        self.status["phase"] = phase
        self.status["detail"] = detail
        log.info("%s: %s", phase, detail)

    def _warn(self, msg: str):
        log.warning(msg)
        self.status["warnings"] = (self.status["warnings"] + [msg])[-20:]

    # ---- Form 4 -----------------------------------------------------------

    def ingest_form4(self, cik: int, accession: str, filing_date: str) -> int:
        """Download and store one Form 4 or 4/A. Returns number of transactions stored."""
        if self.db.one("SELECT 1 x FROM form4_filings WHERE accession = ?", (accession,)):
            return 0

        def mark(status, issuer=cik, form="4"):
            self.db.x("INSERT OR REPLACE INTO form4_filings(accession, issuer_cik, filing_date, status, form) "
                      "VALUES(?,?,?,?,?)", (accession, issuer, filing_date, status, form))

        try:
            text = self.client.filing_text(cik, accession)
            xml = sec.extract_xml(text, "ownershipDocument")
            if not xml:
                mark("no-xml")
                return 0
            doc = sec.parse_form4(xml)
        except sec.SecError as e:
            if str(e).startswith("404"):
                mark("error: 404")
                return 0
            # network trouble: don't mark as done, so it is retried next run
            log.warning("Form 4 %s niet opgehaald: %s", accession, e)
            raise
        except Exception as e:
            log.warning("Form 4 %s onleesbaar: %s", accession, e)
            mark(f"error: {e}"[:200])
            return 0

        form = "4/A" if doc["form"].upper().startswith("4/A") else "4"
        owners = doc["owners"] or [{"cik": 0, "name": "?", "roles": [], "is_officer": False, "is_director": False}]
        main = owners[0]
        name = main["name"] if len(owners) == 1 else " / ".join(o["name"] for o in owners)
        roles = ", ".join(dict.fromkeys(r for o in owners for r in o["roles"]))
        ticker = doc["ticker"].split(",")[0].strip()
        if not ticker or ticker in ("NONE", "N/A", "NA"):
            r = self.db.one("SELECT ticker FROM tickers WHERE cik = ? LIMIT 1", (doc["issuer_cik"],))
            ticker = r["ticker"] if r else ""
        issuer = doc["issuer_cik"] or cik

        rows = []
        for i, t in enumerate(doc["transactions"]):
            key = (issuer, main["cik"], t["date"], t["code"], int(t["derivative"]))
            if form == "4":
                # an amendment for this exact transaction was already stored -> it wins
                if self.db.one("SELECT 1 x FROM insider_tx WHERE issuer_cik=? AND owner_cik=? AND tx_date=? "
                               "AND code=? AND derivative=? AND form='4/A' AND filing_date >= ?",
                               key + (filing_date,)):
                    continue
            else:
                # amendment replaces the matching lines of earlier originals
                self.db.x("DELETE FROM insider_tx WHERE issuer_cik=? AND owner_cik=? AND tx_date=? AND code=? "
                          "AND derivative=? AND accession != ? AND filing_date <= ?",
                          key + (accession, filing_date))
            suspect = None
            if (t["code"] == "P" and not t["derivative"] and t["shares_after"] is not None and t["shares"]
                    and t["shares_after"] + 0.5 < t["shares"]):
                suspect = "bezit na aankoop kleiner dan gekocht aantal"
            if t["value"] and t["value"] > 10e9:
                suspect = "onwaarschijnlijk grote waarde"
            rows.append((
                accession, i, issuer, doc["issuer_name"], ticker,
                main["cik"], name, roles, int(any(o["is_officer"] for o in owners)),
                int(any(o["is_director"] for o in owners)),
                t["security"], t["date"], t["code"], t["acq_disp"], t["shares"], t["price"], t["value"],
                t["shares_after"], int(t["direct"]), int(t["derivative"]), int(doc["plan_10b5_1"]),
                filing_date, form, suspect,
            ))
        self.db.xmany(
            """INSERT OR IGNORE INTO insider_tx(accession, seq, issuer_cik, issuer_name, ticker,
               owner_cik, owner_name, roles, is_officer, is_director, security, tx_date, code, acq_disp,
               shares, price, value, shares_after, direct, derivative, plan_10b5_1, filing_date, form, suspect)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        mark("ok", issuer, form)
        return len(rows)

    def sync_watchlist(self) -> int:
        days = int(self.cfg.get("insider_lookback_days", 180))
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        tickers = [r["ticker"] for r in self.db.q("SELECT ticker FROM watchlist ORDER BY ticker")]
        n = 0
        for i, t in enumerate(tickers, 1):
            cik = self.cik_for(t)
            if not cik:
                self._warn(f"Ticker {t} niet gevonden bij de SEC (buitenlandse bedrijven vullen geen Form 4 in)")
                continue
            self._phase("watchlist", f"{t} ({i}/{len(tickers)})")
            try:
                subs = self.client.submissions(cik)
            except sec.SecError as e:
                self._warn(f"{t}: {e}")
                continue
            # oldest first, so amendments are processed after their originals
            filings = sorted((f for f in sec.recent_filings(subs, {"4", "4/A"}) if f["filing_date"] >= since),
                             key=lambda f: (f["filing_date"], f["accession"]))
            for f in filings:
                try:
                    n += self.ingest_form4(cik, f["accession"], f["filing_date"])
                except sec.SecError:
                    continue
        return n

    def sync_market(self) -> int:
        mcfg = self.cfg.get("market_scan", {})
        if not mcfg.get("enabled", True):
            return 0
        found: dict[str, dict] = {}

        self._phase("markt", "Laatste Form 4-meldingen ophalen")
        for page in range(int(mcfg.get("feed_pages", 5))):
            try:
                items = sec.parse_current_feed(self.client.current_feed("4", start=page * 100))
            except Exception as e:
                self._warn(f"Live feed pagina {page}: {e}")
                break
            if not items:
                break
            for it in items:
                found.setdefault(it["accession"], it)

        # Daily indexes of the last N business days overlap with earlier runs on
        # purpose: anything a previous run missed is picked up here.
        day = dt.date.today()
        for _ in range(int(mcfg.get("days_back", 2))):
            day -= dt.timedelta(days=1)
            while day.weekday() >= 5:
                day -= dt.timedelta(days=1)
            done_key = f"idx_done:{day.isoformat()}"
            if self.db.get_meta(done_key):
                continue
            try:
                for it in sec.parse_daily_index(self.client.daily_index(day)):
                    found.setdefault(it["accession"], it)
                if day < dt.date.today() - dt.timedelta(days=4):
                    self.db.set_meta(done_key, True)   # old enough to be final
            except sec.SecError as e:
                log.info("Dagindex %s niet beschikbaar: %s", day, e)

        # Persistent queue: what doesn't fit in this run is done in the next one.
        self.db.xmany("INSERT OR IGNORE INTO form4_queue(accession, cik, form, filing_date) "
                      "SELECT ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM form4_filings WHERE accession = ?)",
                      [(a, it["cik"], it.get("form", "4"), it["filing_date"], a) for a, it in found.items()])
        self.db.x("DELETE FROM form4_queue WHERE filing_date < ?",
                  ((dt.date.today() - dt.timedelta(days=int(mcfg.get("queue_max_age_days", 10)))).isoformat(),))
        todo = self.db.q("SELECT * FROM form4_queue ORDER BY filing_date, form, accession LIMIT ?",
                         (int(mcfg.get("max_filings", 3000)),))
        backlog = self.db.one("SELECT COUNT(*) n FROM form4_queue")["n"] - len(todo)
        n = errors = 0
        for i, it in enumerate(todo, 1):
            if i % 25 == 0 or i == len(todo):
                self._phase("markt", f"Form 4 {i}/{len(todo)}" + (f" (+{backlog} in wachtrij)" if backlog else ""))
            try:
                n += self.ingest_form4(it["cik"], it["accession"], it["filing_date"])
            except sec.SecError:
                errors += 1
                if errors > 20:
                    self._warn("Te veel netwerkfouten bij de SEC, marktscan gestopt (wordt hervat bij volgende sync)")
                    break
                continue
            self.db.x("DELETE FROM form4_queue WHERE accession = ?", (it["accession"],))
        if backlog:
            self._warn(f"{backlog} Form 4-meldingen in de wachtrij voor de volgende sync")
        return n

    def flag_suspect_prices(self, days: int = 120):
        """Mark transactions whose price is far off the median price of other
        trades in the same stock around that time (typos, per-thousand prices)."""
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = self.db.q("SELECT id, issuer_cik, tx_date, price, suspect FROM insider_tx WHERE derivative = 0 "
                         "AND code IN ('P','S') AND price > 0 AND tx_date >= ?", (since,))
        by: dict[int, list] = {}
        for r in rows:
            by.setdefault(r["issuer_cik"], []).append(r)
        updates = []
        for group in by.values():
            if len(group) < 4:
                continue
            for r in group:
                d0 = dt.date.fromisoformat(r["tx_date"])
                ref = [g["price"] for g in group if g["id"] != r["id"]
                       and abs((dt.date.fromisoformat(g["tx_date"]) - d0).days) <= 45]
                if len(ref) < 3:
                    continue
                med = statistics.median(ref)
                ratio = r["price"] / med
                if ratio > 4 or ratio < 0.25:
                    note = f"prijs ${r['price']:,.2f} wijkt sterk af van andere trades (~${med:,.2f})"
                    if r["suspect"] != note:
                        updates.append((note, r["id"]))
                elif (r["suspect"] or "").startswith("prijs "):
                    updates.append((None, r["id"]))
        if updates:
            self.db.xmany("UPDATE insider_tx SET suspect = ? WHERE id = ?", updates)

    # ---- 13F --------------------------------------------------------------

    def _load_13f(self, cik: int, fl: dict) -> tuple[list[dict] | None, dict]:
        idx = self.client.filing_index(cik, fl["accession"])
        cover = {}
        names = {i.get("name", "").lower() for i in idx.get("directory", {}).get("item", [])}
        if fl["form"] == "13F-HR/A" and "primary_doc.xml" in names:
            try:
                cover = sec.parse_13f_cover(self.client.filing_file(cik, fl["accession"], "primary_doc.xml"))
            except Exception as e:
                log.info("13F cover %s: %s", fl["accession"], e)
        name = sec.find_infotable_name(idx)
        if not name:
            return None, cover
        xml = self.client.filing_file(cik, fl["accession"], name)
        return sec.parse_13f_infotable(xml, in_thousands=fl["filing_date"] < "2023-01-03"), cover

    def _build_period(self, cik: int, period: str, filings: list[dict]) -> int:
        """Combine original + amendments of one quarter into a single holdings set."""
        filings = sorted(filings, key=lambda f: (f["filing_date"], f["accession"]))
        originals = [f for f in filings if f["form"] == "13F-HR"]
        base = originals[-1] if originals else filings[0]
        holdings, _ = self._load_13f(cik, base)
        if holdings is None:
            self._warn(f"Geen positietabel in 13F {base['accession']}")
            holdings = []
        merged = {(h["cusip"], h["put_call"]): h for h in holdings}
        applied = []
        for am in [f for f in filings if f["form"] == "13F-HR/A" and f["filing_date"] >= base["filing_date"]
                   and f is not base]:
            rows, cover = self._load_13f(cik, am)
            atype = cover.get("amendment_type")
            self.db.x("UPDATE f13_seen SET amendment_type = ? WHERE accession = ?", (atype, am["accession"]))
            if not rows:
                continue
            if atype is None:   # unknown: a near-complete table is a restatement
                atype = "RESTATEMENT" if len(rows) >= 0.5 * max(len(merged), 1) else "NEW HOLDINGS"
            if atype.startswith("RESTATEMENT"):
                merged = {(h["cusip"], h["put_call"]): h for h in rows}
            else:
                for h in rows:
                    k = (h["cusip"], h["put_call"])
                    if k in merged:
                        merged[k] = {**merged[k], "shares": merged[k]["shares"] + h["shares"],
                                     "value": merged[k]["value"] + h["value"]}
                    else:
                        merged[k] = h
            applied.append({"accession": am["accession"], "type": atype, "filing_date": am["filing_date"]})

        hl = list(merged.values())
        total = sum(h["value"] for h in hl)
        note = None
        if hl and base["filing_date"] >= "2023-01-03" and 0 < total < MIN_PLAUSIBLE_13F_TOTAL:
            for h in hl:
                h["value"] *= 1000
            total *= 1000
            note = "waarden ×1000: fonds rapporteerde in duizenden i.p.v. dollars"
        norm_map = self.db.get_meta("norm_map", {})
        self.db.x("DELETE FROM f13_holdings WHERE fund_cik = ? AND period = ?", (cik, period))
        self.db.x("DELETE FROM f13_filings WHERE fund_cik = ? AND period = ?", (cik, period))
        self.db.xmany("INSERT OR REPLACE INTO f13_holdings VALUES(?,?,?,?,?,?,?,?,?,?)",
                      [(base["accession"], cik, period, h["cusip"], h["name"], h["title"], h["put_call"],
                        h["shares"], h["value"], norm_map.get(sec.normalize_name(h["name"]), "")) for h in hl])
        self.db.x("INSERT INTO f13_filings(accession, fund_cik, period, filing_date, positions, total_value, "
                  "amendments, value_note) VALUES(?,?,?,?,?,?,?,?)",
                  (base["accession"], cik, period, (applied[-1]["filing_date"] if applied else base["filing_date"]),
                   len(hl), total, json.dumps(applied) if applied else None, note))
        return 1

    def sync_funds(self) -> int:
        quarters = int(self.cfg.get("fund_quarters", 4))
        funds = self.db.q("SELECT * FROM funds")
        n = 0
        for i, f in enumerate(funds, 1):
            label = f["label"] or f["name"] or f["cik"]
            self._phase("fondsen", f"{label} ({i}/{len(funds)})")
            try:
                subs = self.client.submissions(f["cik"])
            except sec.SecError as e:
                self._warn(f"Fonds {label}: {e}")
                continue
            if subs.get("name") and subs["name"] != f["name"]:
                self.db.x("UPDATE funds SET name = ? WHERE cik = ?", (subs["name"], f["cik"]))
            by_period: dict[str, list] = {}
            for fl in sec.recent_filings(subs, {"13F-HR", "13F-HR/A"}):
                by_period.setdefault(fl["report_date"] or fl["filing_date"], []).append(fl)
            if not by_period:
                self._warn(f"{label}: geen 13F-rapporten gevonden. Klopt de CIK?")
            for period in sorted(by_period, reverse=True)[:quarters]:
                fls = by_period[period]
                seen = {r["accession"] for r in self.db.q(
                    f"SELECT accession FROM f13_seen WHERE accession IN ({','.join('?' * len(fls))})",
                    [x["accession"] for x in fls])}
                if all(x["accession"] in seen for x in fls):
                    continue
                self.db.xmany("INSERT OR IGNORE INTO f13_seen(accession, fund_cik, period, form, filing_date) "
                              "VALUES(?,?,?,?,?)",
                              [(x["accession"], f["cik"], period, x["form"], x["filing_date"]) for x in fls])
                try:
                    n += self._build_period(f["cik"], period, fls)
                except Exception as e:
                    self._warn(f"13F {label} {period} mislukt: {e}")
                    self.db.x(f"DELETE FROM f13_seen WHERE accession IN ({','.join('?' * len(fls))})",
                              [x["accession"] for x in fls])   # retry next run
        if n:
            self.cross_check_13f_units()
        self.map_tickers()
        return n

    def cross_check_13f_units(self):
        """Compare price-per-share of the same CUSIP across funds in the same
        quarter. A whole filing that is ~1000x off was reported in the wrong unit."""
        rows = self.db.q("SELECT accession, fund_cik, period, cusip, value, shares FROM f13_holdings "
                         "WHERE put_call = '' AND shares > 0 AND value > 0")
        px: dict[tuple, dict] = {}
        for r in rows:
            px.setdefault((r["period"], r["cusip"]), {})[r["accession"]] = r["value"] / r["shares"]
        by_acc: dict[str, list] = {}
        for (period, cusip), accs in px.items():
            if len(accs) < 2:
                continue
            for acc, p in accs.items():
                others = [v for a, v in accs.items() if a != acc]
                by_acc.setdefault(acc, []).append(p / statistics.median(others))
        for acc, ratios in by_acc.items():
            if len(ratios) < 3:
                continue
            m = statistics.median(ratios)
            factor = 1000 if m < 0.003 else (0.001 if m > 300 else None)
            if factor:
                self.db.x("UPDATE f13_holdings SET value = value * ? WHERE accession = ?", (factor, acc))
                self.db.x("UPDATE f13_filings SET total_value = total_value * ?, value_note = ? WHERE accession = ?",
                          (factor, f"waarden ×{factor:g}: eenheid klopte niet (vergeleken met andere fondsen)", acc))
                self._warn(f"13F {acc}: waarden gecorrigeerd met factor {factor:g}")

    def map_tickers(self):
        """CUSIP -> ticker via OpenFIGI; fall back to matching on company name."""
        if self.figi:
            stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            todo = [r["cusip"] for r in self.db.q(
                "SELECT DISTINCT h.cusip FROM f13_holdings h LEFT JOIN cusip_map m ON m.cusip = h.cusip "
                "WHERE m.cusip IS NULL OR (m.ticker = '' AND m.updated < ?)", (stale,))]
            if todo:
                self._phase("tickers", f"{len(todo)} CUSIP-codes koppelen via OpenFIGI")
                try:
                    res = self.figi.map_cusips(todo)
                    self.db.xmany("INSERT OR REPLACE INTO cusip_map(cusip, ticker, source, updated) "
                                  "VALUES(?, ?, 'openfigi', CURRENT_TIMESTAMP)",
                                  [(c, (res.get(c) or "")) for c in todo if c in res])
                except Exception as e:
                    self._warn(f"OpenFIGI niet bereikbaar, tickers op naam gekoppeld: {e}")
        self.db.x("UPDATE f13_holdings SET ticker = (SELECT m.ticker FROM cusip_map m WHERE m.cusip = "
                  "f13_holdings.cusip) WHERE EXISTS (SELECT 1 FROM cusip_map m WHERE m.cusip = f13_holdings.cusip "
                  "AND m.ticker != '')")

    # ---- orchestration ----------------------------------------------------

    def run(self, insiders=True, market=True, funds=True, alerts=True) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"skipped": "sync draait al"}
        self.status.update(running=True, last_error=None, warnings=[])
        counts = {}
        try:
            self.seed()
            self.refresh_tickers()
            if insiders:
                counts["watchlist_tx"] = self.sync_watchlist()
            if market:
                counts["market_tx"] = self.sync_market()
            if insiders or market:
                self.flag_suspect_prices()
            if funds:
                counts["f13_filings"] = self.sync_funds()
            if alerts:
                from . import alerts as al
                self._phase("alerts", "Regels controleren")
                new = al.evaluate(self.db, self.cfg)
                counts["new_alerts"] = len(new)
                counts["sent"] = al.notify(self.db, self.cfg)
        except Exception as e:
            self.status["last_error"] = f"{e}"
            log.error("Sync mislukt: %s\n%s", e, traceback.format_exc())
        finally:
            self.status.update(running=False, phase="idle", detail="",
                               last_run=dt.datetime.now().astimezone().isoformat(timespec="seconds"), last_counts=counts)
            self.db.set_meta("last_sync", {"time": self.status["last_run"], "counts": counts,
                                           "warnings": self.status["warnings"], "error": self.status["last_error"]})
            self.lock.release()
        log.info("Sync klaar: %s", counts)
        return counts

    def run_in_background(self, **kw):
        t = threading.Thread(target=self.run, kwargs=kw, daemon=True)
        t.start()
        return t
