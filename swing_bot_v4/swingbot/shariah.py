"""Shariah compliance screening (yaaqen.com page classification) with a TTL cache.

Semantics (unchanged from v3, made explicit):
  COMPLIANT     the only status that permits a NEW entry
  NONCOMPLIANT  blocks entries; forces an exit of a held position
  DOUBTFUL      ("محل نظر") blocks entries; forces an exit of a held position
  UNKNOWN       network/parse failure: blocks entries; never forces an exit; never cached

Changes vs v3:
* Rulings are cached for ``cache_days`` (screens change on quarterly filings), not wiped
  daily, cutting ~100 scrapes/day to a handful.
* A local overrides file (``shariah_overrides.json``: {"allow": [...], "deny": [...]})
  lets the operator apply a scholar's/board's ruling without code changes.
* Honest, configurable User-Agent; bounded concurrency; retry with backoff on 429/5xx.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from enum import Enum
from pathlib import Path

import requests

log = logging.getLogger("shariah")


class Compliance(str, Enum):
    COMPLIANT = "compliant"
    NONCOMPLIANT = "noncompliant"
    DOUBTFUL = "doubtful"
    UNKNOWN = "unknown"

    @property
    def forces_exit(self) -> bool:
        return self in (Compliance.NONCOMPLIANT, Compliance.DOUBTFUL)


_BADGE = re.compile(r'class="([^"]*bg-[^"]*)"')
_MARKERS = (
    ("bg-red-50", "غير شرعي", Compliance.NONCOMPLIANT),
    ("bg-[#FEF1C7]", "محل نظر", Compliance.DOUBTFUL),
    ("bg-[#ECFEE7]", "شرعي", Compliance.COMPLIANT),
)


def classify_html(html: str) -> Compliance:
    """First status badge on the page decides. ``text-xs`` badges are list chips, not the ruling."""
    for match in _BADGE.finditer(html):
        classes = match.group(1)
        if "text-xs" in classes:
            continue
        window = html[match.end():match.end() + 400]
        for css, label, status in _MARKERS:
            if css in classes and label in window:
                return status
    return Compliance.UNKNOWN


class ShariahScreen:
    BASE_URL = "https://yaaqen.com/stocks/{symbol}"

    def __init__(self, cache_path: Path, overrides_path: Path, cache_days: int = 7, concurrency: int = 4,
                 user_agent: str = "swingbot/4.0", session: requests.Session | None = None,
                 today: callable = date.today):  # type: ignore[valid-type]
        self.cache_path, self.overrides_path = Path(cache_path), Path(overrides_path)
        self.cache_days, self.concurrency, self.today = cache_days, concurrency, today
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html",
                                     "Accept-Language": "ar,en;q=0.8"})
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, str]] = self._load_cache()

    # ------------------------------------------------------------------ cache
    def _load_cache(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, dict) and "status" in v and "date" in v}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("shariah cache unreadable (%s); starting empty", exc)
            return {}

    def _save_cache(self) -> None:
        tmp = self.cache_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._cache, fh, ensure_ascii=False, indent=0)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.cache_path)

    def _overrides(self) -> tuple[set[str], set[str]]:
        try:
            data = json.loads(self.overrides_path.read_text(encoding="utf-8"))
            return {s.upper() for s in data.get("allow", [])}, {s.upper() for s in data.get("deny", [])}
        except FileNotFoundError:
            return set(), set()
        except (OSError, ValueError, AttributeError) as exc:
            log.error("overrides file invalid (%s); ignoring it and failing closed on entries", exc)
            return set(), set()

    # ------------------------------------------------------------------ lookup
    def _fetch(self, symbol: str) -> Compliance:
        url = self.BASE_URL.format(symbol=symbol)
        for attempt in range(3):
            try:
                res = self.session.get(url, timeout=(5, 15))
            except requests.RequestException as exc:
                log.warning("shariah %s: %s", symbol, exc)
                time.sleep(1.5 * (attempt + 1))
                continue
            if res.status_code == 200:
                status = classify_html(res.text)
                if status is Compliance.UNKNOWN:
                    log.warning("shariah %s: page parsed but no ruling badge found", symbol)
                return status
            if res.status_code in (429, 500, 502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue
            log.warning("shariah %s: HTTP %s", symbol, res.status_code)
            return Compliance.UNKNOWN
        return Compliance.UNKNOWN

    def status(self, symbol: str) -> Compliance:
        symbol = symbol.upper()
        allow, deny = self._overrides()
        if symbol in deny:
            return Compliance.NONCOMPLIANT
        if symbol in allow:
            return Compliance.COMPLIANT
        today = self.today()
        with self._lock:
            hit = self._cache.get(symbol)
            if hit and date.fromisoformat(hit["date"]) >= today - timedelta(days=self.cache_days):
                return Compliance(hit["status"])
        status = self._fetch(symbol)
        if status is not Compliance.UNKNOWN:
            with self._lock:
                self._cache[symbol] = {"status": status.value, "date": today.isoformat()}
                self._save_cache()
        return status

    def screen(self, symbols: list[str]) -> dict[str, Compliance]:
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            results = dict(zip(symbols, pool.map(self.status, symbols), strict=True))
        counts = {s.value: sum(1 for v in results.values() if v is s) for s in Compliance}
        log.info("shariah screen of %d symbols: %s", len(symbols), counts)
        return results
