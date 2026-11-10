"""Offline fake of the SEC endpoints, with data in the real EDGAR formats."""
import datetime as dt
import json
import re

from tracker.sec import SecClient, SecError

TODAY = dt.date.today()


def d(n):
    return (TODAY - dt.timedelta(days=n)).isoformat()


COMPANIES = {
    320193: ("AAPL", "Apple Inc."),
    1045810: ("NVDA", "NVIDIA CORP"),
    1318605: ("TSLA", "Tesla, Inc."),
    789019: ("MSFT", "MICROSOFT CORP"),
    1652044: ("GOOGL", "Alphabet Inc."),
    1067983: ("BRK-B", "BERKSHIRE HATHAWAY INC"),
}

# (accession, issuer_cik, owner_cik, owner, officer_title|None, director, code, shares, price, after, days_ago, plan)
FORM4 = [
    ("0001127602-26-000101", 1318605, 1494730, "Musk Elon", "CEO", True, "P", 2_500_000, 400.0, 413_000_000, 1, False),
    ("0001127602-26-000102", 1318605, 1771364, "Taneja Vaibhav", "Chief Financial Officer", False, "P", 2_000, 401.5, 120_000, 2, False),
    ("0001127602-26-000103", 1318605, 1790565, "Denholm Robyn M", None, True, "P", 5_000, 399.0, 90_000, 3, False),
    ("0001127602-26-000104", 1318605, 1494731, "Kimbal Musk", None, True, "S", 30_000, 410.0, 1_200_000, 5, True),
    ("0000320193-26-000201", 320193, 1214156, "COOK TIMOTHY D", "Chief Executive Officer", True, "S", 108_136, 228.4, 3_280_000, 2, True),
    ("0000320193-26-000202", 320193, 1631982, "Levinson Arthur D", None, True, "P", 1_000, 225.1, 4_500_000, 1, False),
    ("0000320193-26-000203", 320193, 1496686, "O'BRIEN DEIRDRE", "Senior Vice President", False, "A", 60_000, None, 200_000, 20, False),
    ("0001045810-26-000301", 1045810, 1197649, "HUANG JEN HSUN", "President and CEO", True, "S", 75_000, 180.2, 70_000_000, 4, True),
    ("0001045810-26-000302", 1045810, 1197650, "Stevens Mark A", None, True, "P", 10_000, 179.0, 2_500_000, 60, False),
    # typo: price per thousand -> must be flagged as suspect
    ("0001127602-26-000105", 1318605, 1494799, "Kirkhorn Zachary", "Chief Accounting Officer", False, "S", 4_000, 0.41, 50_000, 6, False),
    # amendment of Levinson's AAPL buy: real amount was 10x larger
    ("0000320193-26-000204", 320193, 1631982, "Levinson Arthur D", None, True, "P", 10_000, 225.1, 4_509_000, 1, False, "4/A"),
]


def form4_xml(acc, issuer_cik, owner_cik, owner, title, director, code, shares, price, after, ago, plan, form="4"):
    tk, name = COMPANIES[issuer_cik]
    ad = "A" if code in ("P", "A", "M") else "D"
    price_xml = f"<value>{price}</value>" if price is not None else '<footnoteId id="F1"/>'
    # Split purchases into 2 lots to exercise aggregation
    lots = [(shares // 2, price), (shares - shares // 2, (price or 0) + 1 if price else None)] if code == "P" else [(shares, price)]
    txs = ""
    running = after - (shares if ad == "A" else -shares)
    for sh, pr in lots:
        running += sh if ad == "A" else -sh
        pr_xml = f"<value>{pr}</value>" if pr is not None else price_xml
        txs += f"""
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>{d(ago)}</value></transactionDate>
            <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode>
              <equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{sh}</value></transactionShares>
                <transactionPricePerShare>{pr_xml}</transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{running}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>"""
    officer = f"<isOfficer>1</isOfficer><officerTitle>{title}</officerTitle>" if title else "<isOfficer>0</isOfficer>"
    deriv = ""
    if code == "S":
        deriv = f"""<derivativeTable><derivativeTransaction>
            <securityTitle><value>Stock Option (Right to Buy)</value></securityTitle>
            <conversionOrExercisePrice><value>10.0</value></conversionOrExercisePrice>
            <transactionDate><value>{d(ago)}</value></transactionDate>
            <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>M</transactionCode></transactionCoding>
            <transactionAmounts><transactionShares><value>500</value></transactionShares>
              <transactionPricePerShare><value>0</value></transactionPricePerShare>
              <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>0</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </derivativeTransaction></derivativeTable>"""
    return f"""<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0508</schemaVersion>
    <documentType>{form}</documentType>
    <periodOfReport>{d(ago)}</periodOfReport>
    <aff10b5One>{1 if plan else 0}</aff10b5One>
    <issuer><issuerCik>{issuer_cik:010d}</issuerCik><issuerName>{name}</issuerName><issuerTradingSymbol>{tk}</issuerTradingSymbol></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>{owner_cik:010d}</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>{1 if director else 0}</isDirector>{officer}<isTenPercentOwner>0</isTenPercentOwner><isOther>0</isOther></reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>{txs}
    </nonDerivativeTable>
    {deriv}
    <footnotes><footnote id="F1">Granted under the 2014 Plan.</footnote></footnotes>
</ownershipDocument>"""


def full_txt(acc, xml):
    return f"""<SEC-DOCUMENT>{acc}.txt
<SEC-HEADER>ACCESSION NUMBER: {acc}
CONFORMED SUBMISSION TYPE: 4
</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<SEQUENCE>1
<FILENAME>wk-form4.xml
<TEXT>
<XML>
{xml}
</XML>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>"""


# Funds: cik -> (name, {period: [(form, filing_date, rows, amendment_type)]})
# rows: (name, cusip, value_usd, shares, putcall)
FUNDS = {
    1067983: ("BERKSHIRE HATHAWAY INC", {
        "2026-06-30": [("13F-HR", d(100), [("APPLE INC", "037833100", 60_000_000_000, 280_000_000, ""),
                                           ("BANK AMER CORP", "060505104", 30_000_000_000, 680_000_000, ""),
                                           ("COCA COLA CO", "191216100", 28_000_000_000, 400_000_000, ""),
                                           ("CHEVRON CORP NEW", "166764100", 17_000_000_000, 118_000_000, "")], None)],
        "2026-09-30": [("13F-HR", d(5), [("APPLE INC", "037833100", 41_000_000_000, 180_000_000, ""),
                                         ("BANK AMER CORP", "060505104", 31_000_000_000, 680_000_000, ""),
                                         ("COCA COLA CO", "191216100", 29_000_000_000, 400_000_000, ""),
                                         ("ALPHABET INC", "02079K305", 4_300_000_000, 17_800_000, ""),
                                         ("NVIDIA CORPORATION", "67066G104", 50_000_000, 280_000, "")], None)],
    }),
    1649339: ("Scion Asset Management, LLC", {
        "2026-06-30": [("13F-HR", d(100), [("NVIDIA CORPORATION", "67066G104", 180_000_000, 1_000_000, "PUT"),
                                           ("TESLA INC", "88160R101", 20_000_000, 50_000, ""),
                                           ("APPLE INC", "037833100", 21_400_000, 100_000, "")], None)],
        "2026-09-30": [("13F-HR", d(8), [("TESLA INC", "88160R101", 60_000_000, 150_000, ""),
                                         ("MICROSOFT CORP", "594918104", 30_000_000, 60_000, ""),
                                         ("APPLE INC", "037833100", 22_800_000, 100_000, "")], None),
                       # confidential position disclosed later
                       ("13F-HR/A", d(3), [("AMAZON COM INC", "023135106", 45_000_000, 200_000, "")], "NEW HOLDINGS")],
    }),
    # small filer that (wrongly) still reports in thousands after 2023
    1000001: ("TINY CAPITAL LLC", {
        "2026-09-30": [("13F-HR", d(7), [("APPLE INC", "037833100", 45_600, 200_000, ""),
                                         ("TESLA INC", "88160R101", 40_000, 100_000, "")], None)],
    }),
}

FIGI = {"037833100": "AAPL", "060505104": "BAC", "191216100": "KO", "166764100": "CVX", "02079K305": "GOOGL",
        "67066G104": "NVDA", "88160R101": "TSLA", "594918104": "MSFT", "023135106": "AMZN"}


class FakeFigi:
    def __init__(self):
        self.calls = 0

    def map_cusips(self, cusips):
        self.calls += 1
        return {c: FIGI.get(c) for c in cusips}


def primary_doc(amend_type):
    amend = (f"<isAmendment>true</isAmendment><amendmentInfo><amendmentType>{amend_type}</amendmentType>"
             f"</amendmentInfo>") if amend_type else "<isAmendment>false</isAmendment>"
    return f"""<?xml version="1.0"?><edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
<formData><coverPage><reportCalendarOrQuarter>09-30-2026</reportCalendarOrQuarter>{amend}</coverPage>
<summaryPage><tableValueTotal>1</tableValueTotal></summaryPage></formData></edgarSubmission>"""


def fund_filings(cik):
    out = []
    for period, fls in FUNDS[cik][1].items():
        for i, (form, fd, rows, at) in enumerate(fls):
            acc = f"0000950123-26-{(cik * 7 + int(period[:4]) * 13 + int(period[5:7]) * 101 + i) % 900000 + 100000:06d}"
            out.append({"acc": acc, "form": form, "date": fd, "period": period, "rows": rows, "atype": at})
    return out


def infotable(rows):
    body = ""
    for name, cusip, value, shares, pc in rows:
        # split each position across two managers to test aggregation
        for part in (0.6, 0.4):
            body += f"""
  <infoTable>
    <nameOfIssuer>{name}</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip>
    <value>{int(value * part)}</value>
    <shrsOrPrnAmt><sshPrnamt>{int(shares * part)}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    {f"<putCall>{pc}</putCall>" if pc else ""}
    <investmentDiscretion>DFND</investmentDiscretion>
    <votingAuthority><Sole>{int(shares * part)}</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{body}
</informationTable>"""


class FakeSecClient(SecClient):
    def __init__(self):
        super().__init__("Test Runner test@example.com", min_interval=0)
        self.calls = []
        self.f4 = {a[0]: a for a in FORM4}

    def get(self, url, retries=3):
        self.calls.append(url)
        return self._route(url).encode()

    def _route(self, url):
        if url.endswith("/files/company_tickers.json"):
            return json.dumps({str(i): {"cik_str": c, "ticker": t, "title": n}
                               for i, (c, (t, n)) in enumerate(COMPANIES.items())})
        m = re.search(r"submissions/CIK(\d{10})\.json", url)
        if m:
            cik = int(m[1])
            if cik in FUNDS:
                name = FUNDS[cik][0]
                rows = [(f["acc"], f["form"], f["date"], f["period"], "primary_doc.xml") for f in fund_filings(cik)]
            elif cik in COMPANIES:
                name = COMPANIES[cik][1]
                rows = [(a[0], a[12] if len(a) > 12 else "4", d(a[10]), d(a[10]), "xslF345X05/wk-form4.xml")
                        for a in FORM4 if a[1] == cik]
                rows.append(("0000320193-26-000999", "10-Q", d(10), d(40), "q.htm"))
            else:
                raise SecError("404 " + url)
            return json.dumps({"cik": str(cik), "name": name, "filings": {"recent": {
                "accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
                "filingDate": [r[2] for r in rows], "reportDate": [r[3] for r in rows],
                "primaryDocument": [r[4] for r in rows]}}})
        m = re.search(r"/(\d{10}-\d{2}-\d{6})\.txt$", url)
        if m and m[1] in self.f4:
            return full_txt(m[1], form4_xml(*self.f4[m[1]]))
        m = re.search(r"/data/(\d+)/(\d{18})/(index\.json|infotable\.xml|primary_doc\.xml)$", url)
        if m and int(m[1]) in FUNDS:
            for f in fund_filings(int(m[1])):
                if f["acc"].replace("-", "") == m[2]:
                    if m[3] == "index.json":
                        return json.dumps({"directory": {"item": [
                            {"name": "primary_doc.xml", "size": "3000"}, {"name": "infotable.xml", "size": "90000"},
                            {"name": f"{f['acc']}-index.htm", "size": "2000"}]}})
                    if m[3] == "primary_doc.xml":
                        return primary_doc(f["atype"])
                    return infotable(f["rows"])
        if "daily-index" in url:
            lines = ["Description:           Daily Index of EDGAR Dissemination Feed by Form Type", "",
                     "Form Type   Company Name                                                  CIK         Date Filed  File Name",
                     "-" * 140]
            for a in FORM4[:4]:
                for cik, nm in ((a[1], COMPANIES[a[1]][1] + " 2"), (a[2], a[3])):
                    lines.append(f"{'4':<12}{nm:<62}{cik:<12}{d(a[10]).replace('-', ''):<12}edgar/data/{cik}/{a[0]}.txt")
            lines.append(f"{'4/A':<12}{'SOME AMENDMENT CO':<62}{'999':<12}{TODAY:%Y%m%d}    edgar/data/999/0000000999-26-000001.txt")
            lines.append(f"{'SC 13G':<12}{'OTHER CO':<62}{'998':<12}{TODAY:%Y%m%d}    edgar/data/998/0000000998-26-000001.txt")
            return "\n".join(lines)
        if "getcurrent" in url:
            if "start=0" not in url:
                return '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
            entries = ""
            for a in FORM4[4:8]:
                for role, cik in (("Issuer", a[1]), ("Reporting", a[2])):
                    entries += f"""<entry><title>4 - {a[3]} ({cik:010d}) ({role})</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/{cik}/{a[0].replace('-', '')}/{a[0]}-index.htm"/>
<updated>{d(a[10])}T16:05:11-04:00</updated><category scheme="https://www.sec.gov/" label="form type" term="4"/></entry>"""
            return f'<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom"><title>Latest Filings</title>{entries}</feed>'
        raise SecError("404 " + url)
