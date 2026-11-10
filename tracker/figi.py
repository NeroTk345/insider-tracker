"""CUSIP -> ticker via OpenFIGI (free, by Bloomberg).

Without an API key: 25 requests/min, 10 CUSIPs per request.
With a free key (openfigi.com/api): 250 requests/min, 100 CUSIPs per request.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("tracker.figi")
URL = "https://api.openfigi.com/v3/mapping"

# Prefer common stock over other security types for the same CUSIP
_TYPE_RANK = {"Common Stock": 0, "ADR": 1, "REIT": 1, "ETP": 2, "Closed-End Fund": 2, "Open-End Fund": 3}


class Figi:
    def __init__(self, api_key: str = "", timeout: int = 30):
        self.key = (api_key or "").strip()
        self.batch = 100 if self.key else 10
        self.interval = 60 / 240 if self.key else 60 / 24   # stay just under the limits
        self.timeout = timeout
        self._last = 0.0

    def _post(self, jobs: list[dict]) -> list[dict]:
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-OPENFIGI-APIKEY"] = self.key
        for attempt in range(4):
            self._last = time.monotonic()
            req = urllib.request.Request(URL, data=json.dumps(jobs).encode(), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(int(e.headers.get("ratelimit-reset") or 30) + 1)
                    continue
                raise

    @staticmethod
    def pick(data: list[dict]) -> str | None:
        if not data:
            return None
        us = [d for d in data if d.get("exchCode") == "US"] or data
        us.sort(key=lambda d: _TYPE_RANK.get(d.get("securityType2") or d.get("securityType") or "", 9))
        t = (us[0].get("ticker") or "").strip().upper()
        # OpenFIGI uses "BRK/B"; SEC uses "BRK-B"
        return t.replace("/", "-").replace(" ", "-") or None

    def map_cusips(self, cusips: list[str]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        todo = [c for c in dict.fromkeys(cusips) if c and len(c) == 9]
        for i in range(0, len(todo), self.batch):
            chunk = todo[i:i + self.batch]
            res = self._post([{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk])
            for c, r in zip(chunk, res):
                out[c] = self.pick(r.get("data") or [])
        return out
