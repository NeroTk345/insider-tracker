"""SEC EDGAR client and parsers for Form 4 (insiders) and 13F-HR (funds).

SEC rules: send a descriptive User-Agent ("Name email") and stay under
10 requests per second. Both are enforced here.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"


class SecError(Exception):
    pass


class SecClient:
    def __init__(self, user_agent: str, min_interval: float = 0.12, timeout: int = 30):
        if not user_agent or "@" not in user_agent:
            raise SecError(
                'Zet een geldige "user_agent" in config.json, bv. "Jan Jansen jan@mail.nl" '
                "(verplicht door de SEC)."
            )
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self):
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def get(self, url: str, retries: int = 3) -> bytes:
        for attempt in range(retries + 1):
            self._throttle()
            req = urllib.request.Request(
                url,
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise SecError(f"404 {url}") from e
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise SecError(f"HTTP {e.code} {url}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise SecError(f"Netwerkfout {url}: {e}") from e
        raise SecError(f"Mislukt: {url}")

    def get_text(self, url: str) -> str:
        return self.get(url).decode("utf-8", errors="replace")

    def get_json(self, url: str):
        return json.loads(self.get(url))

    # ---- endpoints -------------------------------------------------------

    def company_tickers(self) -> list[dict]:
        data = self.get_json(f"{SEC_WWW}/files/company_tickers.json")
        return [
            {"cik": int(v["cik_str"]), "ticker": v["ticker"].upper(), "title": v["title"]}
            for v in data.values()
        ]

    def submissions(self, cik: int) -> dict:
        return self.get_json(f"{SEC_DATA}/submissions/CIK{int(cik):010d}.json")

    def filing_text(self, cik: int, accession: str) -> str:
        nodash = accession.replace("-", "")
        return self.get_text(f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{nodash}/{accession}.txt")

    def filing_index(self, cik: int, accession: str) -> dict:
        nodash = accession.replace("-", "")
        return self.get_json(f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{nodash}/index.json")

    def filing_file(self, cik: int, accession: str, name: str) -> str:
        nodash = accession.replace("-", "")
        return self.get_text(f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{nodash}/{name}")

    def daily_index(self, day: dt.date) -> str:
        q = (day.month - 1) // 3 + 1
        return self.get_text(
            f"{SEC_WWW}/Archives/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx"
        )

    def current_feed(self, form: str = "4", start: int = 0, count: int = 100) -> str:
        return self.get_text(
            f"{SEC_WWW}/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb="
            f"&owner=include&start={start}&count={count}&output=atom"
        )


# ---- listing helpers -----------------------------------------------------

def recent_filings(submissions: dict, forms: set[str]) -> list[dict]:
    """Flatten submissions['filings']['recent'] into dicts, filtered by form type."""
    r = submissions.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(r.get("form", [])):
        if form in forms:
            out.append({
                "accession": r["accessionNumber"][i],
                "form": form,
                "filing_date": r["filingDate"][i],
                "report_date": (r.get("reportDate") or [""] * (i + 1))[i],
                "primary_document": (r.get("primaryDocument") or [""] * (i + 1))[i],
            })
    return out


_IDX_RE = re.compile(r"^(?P<form>\S+)\s+.*?\s(?P<date>\d{8})\s+(?P<file>edgar/data/(?P<cik>\d+)/\S+)\s*$")


def parse_daily_index(text: str, forms=("4", "4/A")) -> list[dict]:
    """Parse form.YYYYMMDD.idx. Returns unique filings of the given form types.

    The CIK is taken from the file path, so company names containing digits
    can't confuse the parser.
    """
    forms = (forms,) if isinstance(forms, str) else tuple(forms)
    seen, out = set(), []
    for line in text.splitlines():
        if not line.split(" ", 1)[0] in forms:
            continue
        m = _IDX_RE.match(line)
        if not m or m["form"] not in forms:
            continue
        acc = m["file"].rsplit("/", 1)[-1].removesuffix(".txt")
        if acc in seen:
            continue
        seen.add(acc)
        d = m["date"]
        out.append({"accession": acc, "cik": int(m["cik"]), "form": m["form"],
                    "filing_date": f"{d[:4]}-{d[4:6]}-{d[6:]}"})
    return out


_ACC_RE = re.compile(r"/data/(\d+)/\d+/(\d{10}-\d{2}-\d{6})-index")


def parse_current_feed(atom: str) -> list[dict]:
    """Parse the EDGAR 'latest filings' Atom feed into unique filings."""
    root = ET.fromstring(atom)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    seen, out = set(), []
    for entry in root.findall("a:entry", ns):
        link = entry.find("a:link", ns)
        href = link.get("href", "") if link is not None else ""
        m = _ACC_RE.search(href)
        if not m or m[2] in seen:
            continue
        seen.add(m[2])
        updated = (entry.findtext("a:updated", "", ns) or "")[:10]
        cat = entry.find("a:category", ns)
        form = (cat.get("term") if cat is not None else None) or "4"
        if form not in ("4", "4/A"):
            continue
        out.append({"accession": m[2], "cik": int(m[1]), "form": form, "filing_date": updated})
    return out


# ---- XML helpers ---------------------------------------------------------

def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def extract_xml(full_text: str, root_tag: str) -> str | None:
    """Pull the <XML> block containing root_tag out of a full .txt submission."""
    for m in re.finditer(r"<XML>(.*?)</XML>", full_text, re.S | re.I):
        block = m.group(1).strip()
        if root_tag in block:
            return block
    if root_tag in full_text and full_text.lstrip().startswith("<"):
        return full_text.strip()
    return None


def _v(el: ET.Element | None, path: str) -> str | None:
    """Read 'path/value' (SEC style) or the element text itself."""
    if el is None:
        return None
    node = el.find(path)
    if node is None:
        return None
    val = node.find("value")
    text = (val.text if val is not None else node.text) or ""
    text = text.strip()
    return text or None


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _flag(s: str | None) -> bool:
    return (s or "").strip().lower() in ("1", "true", "y", "yes")


TX_CODES = {
    "P": "Aankoop (beurs)",
    "S": "Verkoop (beurs)",
    "A": "Toekenning",
    "M": "Optie-uitoefening",
    "F": "Belasting-inhouding",
    "G": "Schenking",
    "D": "Teruggave aan bedrijf",
    "C": "Conversie",
    "X": "Optie-uitoefening",
    "J": "Overig",
}


def parse_form4(xml_text: str) -> dict:
    root = _strip_ns(ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text))
    issuer = root.find("issuer")
    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        roles = []
        if rel is not None:
            if _flag(rel.findtext("isOfficer")):
                roles.append((rel.findtext("officerTitle") or "Officer").strip())
            if _flag(rel.findtext("isDirector")):
                roles.append("Director")
            if _flag(rel.findtext("isTenPercentOwner")):
                roles.append("10% eigenaar")
            if _flag(rel.findtext("isOther")):
                roles.append((rel.findtext("otherText") or "Overig").strip())
        owners.append({
            "cik": int(ro.findtext("reportingOwnerId/rptOwnerCik", "0").strip() or 0),
            "name": (ro.findtext("reportingOwnerId/rptOwnerName") or "").strip(),
            "roles": roles,
            "is_officer": rel is not None and _flag(rel.findtext("isOfficer")),
            "is_director": rel is not None and _flag(rel.findtext("isDirector")),
        })

    plan_10b5 = _flag(root.findtext("aff10b5One"))
    txs = []
    for table, deriv in (("nonDerivativeTable", False), ("derivativeTable", True)):
        t = root.find(table)
        if t is None:
            continue
        for tx in list(t):
            if not tx.tag.endswith("Transaction"):
                continue
            shares = _num(_v(tx, "transactionAmounts/transactionShares"))
            price = _num(_v(tx, "transactionAmounts/transactionPricePerShare"))
            code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
            txs.append({
                "security": _v(tx, "securityTitle") or "",
                "date": (_v(tx, "transactionDate") or "")[:10],
                "code": code,
                "acq_disp": _v(tx, "transactionAmounts/transactionAcquiredDisposedCode") or "",
                "shares": shares,
                "price": price,
                "value": round(shares * price, 2) if shares and price else None,
                "shares_after": _num(_v(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                "direct": (_v(tx, "ownershipNature/directOrIndirectOwnership") or "D") == "D",
                "derivative": deriv,
            })

    def it(tag: str) -> str:
        return ((issuer.findtext(tag) if issuer is not None else None) or "").strip()

    return {
        "issuer_cik": int(it("issuerCik") or 0),
        "issuer_name": it("issuerName"),
        "ticker": it("issuerTradingSymbol").upper(),
        "period": (root.findtext("periodOfReport") or "").strip(),
        "form": (root.findtext("documentType") or "4").strip(),
        "plan_10b5_1": plan_10b5,
        "owners": owners,
        "transactions": txs,
    }


def parse_13f_infotable(xml_text: str, in_thousands: bool = False) -> list[dict]:
    """Parse a 13F information table, aggregated per (CUSIP, put/call)."""
    root = _strip_ns(ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text))
    agg: dict[tuple, dict] = {}
    for row in root.iter("infoTable"):
        cusip = (row.findtext("cusip") or "").strip().upper()
        put_call = (row.findtext("putCall") or "").strip().upper()
        value = _num(row.findtext("value")) or 0.0
        if in_thousands:
            value *= 1000
        shares = _num(row.findtext("shrsOrPrnAmt/sshPrnamt")) or 0.0
        key = (cusip, put_call)
        h = agg.setdefault(key, {
            "cusip": cusip,
            "name": (row.findtext("nameOfIssuer") or "").strip(),
            "title": (row.findtext("titleOfClass") or "").strip(),
            "put_call": put_call,
            "shares": 0.0,
            "value": 0.0,
        })
        h["shares"] += shares
        h["value"] += value
    return list(agg.values())


def parse_13f_cover(primary_doc_xml: str) -> dict:
    """Read amendment info from a 13F primary_doc.xml."""
    root = _strip_ns(ET.fromstring(primary_doc_xml.encode() if isinstance(primary_doc_xml, str) else primary_doc_xml))
    amend_type = None
    is_amend = False
    for el in root.iter():
        if el.tag == "isAmendment":
            is_amend = _flag(el.text)
        elif el.tag == "amendmentType":
            amend_type = (el.text or "").strip().upper() or None
    total = None
    for el in root.iter("tableValueTotal"):
        total = _num(el.text)
    return {"is_amendment": is_amend, "amendment_type": amend_type, "table_value_total": total}


def find_infotable_name(index_json: dict) -> str | None:
    items = index_json.get("directory", {}).get("item", [])
    xmls = [i for i in items if i.get("name", "").lower().endswith(".xml")
            and i.get("name", "").lower() != "primary_doc.xml"]
    if not xmls:
        return None
    for i in xmls:
        if "info" in i["name"].lower():
            return i["name"]
    return max(xmls, key=lambda i: int(i.get("size") or 0))["name"]


_SUFFIXES = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|HLDGS?|HOLDINGS?|GROUP|"
    r"CL|CLASS|A|B|C|NEW|DEL|THE|SA|NV|AG|SE|LP|LLC|ETF|TR|TRUST)\b"
)


def normalize_name(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper().replace("&", " AND "))
    s = _SUFFIXES.sub(" ", s)
    return " ".join(s.split())
