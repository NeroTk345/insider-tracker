import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from tracker import queries, sec  # noqa: E402
from tracker.db import DB  # noqa: E402
from tracker.server import make_handler  # noqa: E402
from tracker.sync import Syncer  # noqa: E402
import fake_sec  # noqa: E402

CFG = {
    "watchlist": ["AAPL", "NVDA", "TSLA", "ASML"],
    "funds": [{"cik": "0001067983", "label": "Berkshire"}, {"cik": "0001649339", "label": "Scion"},
              {"cik": "1000001", "label": "Tiny"}],
    "market_scan": {"enabled": True, "feed_pages": 3, "days_back": 1, "max_filings": 100},
    "alerts": {"min_purchase_value": 250000, "min_watchlist_sale_value": 5_000_000, "cluster_min_insiders": 3},
    "notify": {},
}


class ParserTests(unittest.TestCase):
    def test_form4(self):
        xml = fake_sec.form4_xml(*fake_sec.FORM4[0])
        doc = sec.parse_form4(sec.extract_xml(fake_sec.full_txt("x", xml), "ownershipDocument"))
        self.assertEqual(doc["ticker"], "TSLA")
        self.assertEqual(doc["issuer_cik"], 1318605)
        self.assertEqual(doc["owners"][0]["roles"], ["CEO", "Director"])
        self.assertEqual(len(doc["transactions"]), 2)
        t = doc["transactions"][0]
        self.assertEqual((t["code"], t["acq_disp"], t["shares"], t["price"]), ("P", "A", 1_250_000, 400.0))
        self.assertEqual(t["value"], 500_000_000)

    def test_form4_footnote_price_and_derivative(self):
        doc = sec.parse_form4(fake_sec.form4_xml(*fake_sec.FORM4[6]))  # grant, no price
        self.assertIsNone(doc["transactions"][0]["price"])
        self.assertIsNone(doc["transactions"][0]["value"])
        doc = sec.parse_form4(fake_sec.form4_xml(*fake_sec.FORM4[4]))  # sale + option exercise
        self.assertTrue(doc["plan_10b5_1"])
        self.assertEqual([t["derivative"] for t in doc["transactions"]], [False, True])

    def test_13f(self):
        rows = sec.parse_13f_infotable(fake_sec.infotable(fake_sec.FUNDS[1649339][1]["2026-06-30"][0][2]))
        by = {(r["cusip"], r["put_call"]): r for r in rows}
        self.assertEqual(by[("67066G104", "PUT")]["shares"], 1_000_000)
        self.assertEqual(by[("88160R101", "")]["value"], 20_000_000)
        old = sec.parse_13f_infotable(fake_sec.infotable([("X", "1", 5, 1, "")]), in_thousands=True)
        self.assertEqual(old[0]["value"], 5000)

    def test_daily_index_and_feed(self):
        c = fake_sec.FakeSecClient()
        idx = sec.parse_daily_index(c.daily_index(fake_sec.TODAY))
        self.assertEqual(len(idx), 5)                     # deduped, 4/A kept, SC 13G skipped
        self.assertEqual({i["form"] for i in idx}, {"4", "4/A"})
        self.assertIn(1318605, {i["cik"] for i in idx})   # digits in company name don't break parsing
        feed = sec.parse_current_feed(c.current_feed())
        self.assertEqual(len(feed), 4)
        self.assertEqual(feed[0]["accession"], fake_sec.FORM4[4][0])

    def test_normalize(self):
        self.assertEqual(sec.normalize_name("Apple Inc."), sec.normalize_name("APPLE INC"))
        self.assertEqual(sec.normalize_name("NVIDIA CORP"), sec.normalize_name("NVIDIA CORPORATION"))
        self.assertEqual(sec.normalize_name("Alphabet Inc."), sec.normalize_name("ALPHABET INC"))


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = DB(os.path.join(cls.tmp, "t.db"))
        cls.client = fake_sec.FakeSecClient()
        cls.figi = fake_sec.FakeFigi()
        cls.syncer = Syncer(cls.db, cls.client, CFG, figi=cls.figi)
        cls.counts = cls.syncer.run()

    def test_counts(self):
        c = self.counts
        self.assertGreater(c["watchlist_tx"], 10)
        self.assertEqual(c["f13_filings"], 5)
        self.assertIsNone(self.syncer.status["last_error"])
        # second run is incremental: nothing new, no new alerts
        n_calls = len(self.client.calls)
        again = self.syncer.run()
        self.assertEqual((again["watchlist_tx"], again["market_tx"], again["f13_filings"], again["new_alerts"]), (0, 0, 0, 0))
        self.assertLess(len(self.client.calls) - n_calls, 25)

    def test_buys_aggregated(self):
        top = queries.filings_summary(self.db, "P", days=30)
        musk = next(r for r in top if r["owner_name"] == "Musk Elon")
        self.assertEqual(musk["shares"], 2_500_000)
        self.assertAlmostEqual(musk["value"], 1_250_000 * 400 + 1_250_000 * 401)
        self.assertEqual(top[0]["owner_name"], "Musk Elon")
        # NVDA buy 60 days ago is outside 30d window
        self.assertFalse(any(r["ticker"] == "NVDA" for r in top))

    def test_cluster(self):
        cl = queries.clusters(self.db, days=14, min_insiders=3)
        self.assertEqual([c["ticker"] for c in cl], ["TSLA"])
        self.assertEqual(cl[0]["insiders"], 3)

    def test_fund_changes(self):
        ch = queries.fund_changes(self.db, 1067983)
        st = {h["name"]: (h["status"], h.get("ticker")) for h in ch["holdings"]}
        self.assertEqual(st["APPLE INC"], ("afgebouwd", "AAPL"))
        self.assertEqual(st["BANK AMER CORP"][1], "BAC")    # via CUSIP (OpenFIGI); name match would miss
        self.assertEqual(st["ALPHABET INC"], ("nieuw", "GOOGL"))
        self.assertEqual(st["CHEVRON CORP NEW"][0], "verkocht")
        self.assertEqual(st["BANK AMER CORP"][0], "gelijk")
        ch = queries.fund_changes(self.db, 1649339)
        sc = {h["name"] + h["put_call"]: h["status"] for h in ch["holdings"]}
        self.assertEqual(sc, {"NVIDIA CORPORATIONPUT": "verkocht", "TESLA INC": "bijgekocht", "MICROSOFT CORP": "nieuw",
                              "APPLE INC": "gelijk", "AMAZON COM INC": "nieuw"})   # AMZN from 13F-HR/A
        self.assertEqual(json.loads(ch["filing"]["amendments"])[0]["type"], "NEW HOLDINGS")

    def test_13f_unit_fix(self):
        ch = queries.fund_changes(self.db, 1000001)
        self.assertAlmostEqual(ch["filing"]["total_value"], 85_600_000)
        self.assertIn("1000", ch["filing"]["value_note"])
        aapl = next(h for h in ch["holdings"] if h["ticker"] == "AAPL")
        self.assertAlmostEqual(aapl["value"] / aapl["shares"], 228.0)

    def test_form4_amendment(self):
        rows = self.db.q("SELECT accession, shares, form FROM insider_tx WHERE owner_name = 'Levinson Arthur D'")
        self.assertEqual({r["form"] for r in rows}, {"4/A"})
        self.assertEqual(sum(r["shares"] for r in rows), 10_000)

    def test_suspect_price(self):
        bad = self.db.one("SELECT suspect FROM insider_tx WHERE owner_name = 'Kirkhorn Zachary'")
        self.assertIn("wijkt sterk af", bad["suspect"])
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM insider_tx WHERE suspect IS NOT NULL")["n"], 1)

    def test_queue_drained(self):
        # the unreachable 4/A from the daily index is marked as 404, not retried forever
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM form4_queue")["n"], 0)

    def test_config_is_source(self):
        db = DB(os.path.join(self.tmp, "pages.db"))
        cfg = {**CFG, "config_is_source": True}
        s = Syncer(db, fake_sec.FakeSecClient(), cfg, figi=fake_sec.FakeFigi())
        s.seed()
        s.seed()
        cfg2 = {**cfg, "watchlist": ["NVDA"], "funds": [{"cik": "1067983", "label": "Buffett"}]}
        Syncer(db, fake_sec.FakeSecClient(), cfg2, figi=fake_sec.FakeFigi()).seed()
        self.assertEqual([r["ticker"] for r in db.q("SELECT ticker FROM watchlist")], ["NVDA"])
        self.assertEqual([(r["cik"], r["label"]) for r in db.q("SELECT cik, label FROM funds")], [(1067983, "Buffett")])

    def test_alerts(self):
        kinds = {}
        for a in self.db.q("SELECT * FROM alerts"):
            kinds.setdefault(a["kind"], []).append(a)
        self.assertIn("cluster", kinds)
        self.assertEqual(len(kinds["fund"]), 2)   # Tiny has only one quarter: nothing to compare
        aapl_buy = next(a for a in kinds["buy"] if a["ticker"] == "AAPL")
        self.assertIn("10.000", aapl_buy["body"].replace(",", "."))   # amended amount, not the original 1,000
        buys = {a["ticker"] for a in kinds["buy"]}
        self.assertIn("TSLA", buys)                     # Musk big buy
        self.assertIn("AAPL", buys)                     # small watchlist buy
        # >= $5M watchlist sales filed within max_age_days (NVDA/TSLA sales are 4-5 days old)
        self.assertEqual({a["ticker"] for a in kinds["sell"]}, {"AAPL"})
        self.assertTrue(all(a["sent"] for a in self.db.q("SELECT sent FROM alerts")))

    def test_stock_detail(self):
        s = queries.stock_detail(self.db, "TSLA")
        self.assertEqual(s["buyers_12m"], 3)
        self.assertEqual({h["fund"] for h in s["fund_holders"]}, {"Scion", "Tiny"})

    def test_export(self):
        from tracker.export import export
        out = os.path.join(self.tmp, "site")
        res = export(self.db, CFG, out)
        self.assertGreater(res["transactions"], 5)
        for f in ("index.html", "static.js", ".nojekyll", "data/stats.json", "data/transactions.json",
                  "data/funds.json", "data/fund/1067983.json", "data/fund/1067983_2026-06-30.json",
                  "data/stock/TSLA.json", "data/stock/AMZN.json", "data/search.json"):
            self.assertTrue(os.path.exists(os.path.join(out, f)), f)
        with open(os.path.join(out, "static.js")) as fh:
            self.assertIn("true", fh.read())
        self.assertTrue(json.load(open(os.path.join(out, "data/stats.json")))["static"])

    def test_api(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, self.syncer, CFG))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        get = lambda p: json.loads(urllib.request.urlopen(base + p).read())
        try:
            self.assertGreater(get("/api/stats")["transactions"], 0)
            self.assertTrue(get("/api/top?code=P&days=30"))
            self.assertTrue(get("/api/transactions?code=P,S&days=90&role=ceo"))
            self.assertEqual(get("/api/funds/1067983")["periods"], ["2026-09-30", "2026-06-30"])
            self.assertEqual(get("/api/search?q=tes")[0]["ticker"], "TSLA")
            self.assertIn(b"Insider Tracker", urllib.request.urlopen(base + "/").read())
            req = urllib.request.Request(base + "/api/watchlist", data=b'{"ticker":"msft"}', method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            self.assertIn("MSFT", [w["ticker"] for w in get("/api/watchlist")])
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/../config.json")
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
