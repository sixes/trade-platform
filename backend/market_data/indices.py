from __future__ import annotations

import csv
import io
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

Series = List[Tuple[str, float]]  # (ISO date, value), ascending by date

CBOE_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
CBOE_VIX_URL = CBOE_HISTORY_URL.format(name="VIX")
CBOE_SKEW_URL = CBOE_HISTORY_URL.format(name="SKEW")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
FRED_VIX_URL = FRED_URL.format(series_id="VIXCLS")
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d"
# Japan MOF JGB par yields: the historical file ends at the previous month, the current file covers this month.
MOF_JGB_HISTORY_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv"
MOF_JGB_CURRENT_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"

# Source types served by IndexHistoryProvider itself (others need an external fetcher, e.g. longbridge, sina).
BUILTIN_SOURCE_TYPES = frozenset({"fred", "yahoo", "cboe", "mof_jgb"})

HEADERS = {"User-Agent": "trade-plat/1.0 (+https://localhost)"}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# What each CBOE index measures, for display.
INDEX_LABELS = {
    "VIX": "S&P 500 30-day implied volatility",
    "VIX1D": "S&P 500 1-day implied volatility",
    "VIX9D": "S&P 500 9-day implied volatility",
    "VIX3M": "S&P 500 3-month implied volatility",
    "VIX6M": "S&P 500 6-month implied volatility",
    "VVIX": "Volatility of VIX (VIX option IV)",
    "SKEW": "S&P 500 tail-risk skew",
    "DSPX": "S&P 500 dispersion (single-stock vs index IV)",
    "COR3M": "S&P 500 3-month implied correlation",
    "VXN": "Nasdaq-100 30-day implied volatility",
    "RVX": "Russell 2000 30-day implied volatility",
    "VXD": "Dow Jones 30-day implied volatility",
    "GVZ": "Gold (GLD) 30-day implied volatility",
    "OVX": "Crude oil (USO) 30-day implied volatility",
    "VXEEM": "Emerging markets (EEM) implied volatility",
    "VXEFA": "EAFE (EFA) implied volatility",
    "VXSLV": "Silver (SLV) implied volatility",
    "VXTLT": "20y Treasury (TLT) implied volatility",
}


def _parse_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_csv_series(text: str, value_column: Optional[str] = None) -> Series:
    """Parse a CBOE/FRED style CSV. Uses value_column if given, else CLOSE, else the last column."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    fieldnames = [f for f in reader.fieldnames if f]
    date_col = fieldnames[0]
    columns = {c.strip().lower(): c for c in fieldnames}
    col = None
    for candidate in ([value_column] if value_column else []) + ["close"]:
        if candidate and candidate.strip().lower() in columns:
            col = columns[candidate.strip().lower()]
            break
    if col is None:
        if value_column and value_column.strip().lower() not in columns and len(fieldnames) > 2:
            raise ValueError(f"column {value_column!r} not in {fieldnames}")
        col = fieldnames[-1]
    out: Series = []
    for row in reader:
        date = _parse_date(row.get(date_col) or "")
        raw = (row.get(col) or "").strip()
        if not date or raw in ("", "."):
            continue
        try:
            out.append((date, float(raw)))
        except ValueError:
            continue
    out.sort(key=lambda p: p[0])
    return out


def parse_mof_jgb(text: str, tenor: str = "30Y") -> Series:
    """Parse a Japan MOF JGB yield CSV: a title row, then `Date,1Y,...,40Y` with YYYY/M/D dates and '-' for gaps."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.split(",")[0].strip().lower() == "date"), None)
    if start is None:
        raise ValueError("MOF JGB csv: header row not found")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    fieldnames = [f for f in (reader.fieldnames or []) if f]
    date_col = fieldnames[0]
    col = next((f for f in fieldnames if f.strip().upper() == tenor.strip().upper()), None)
    if col is None:
        raise ValueError(f"MOF JGB csv: column {tenor!r} not in {fieldnames}")
    out: Series = []
    for row in reader:
        try:
            day = datetime.strptime((row.get(date_col) or "").strip(), "%Y/%m/%d").date().isoformat()
            out.append((day, float((row.get(col) or "").strip())))
        except ValueError:
            continue  # title/footer rows, or '-' where no bond of that tenor traded yet
    out.sort(key=lambda p: p[0])
    return out


class IndexHistoryProvider:
    """Daily closes for CBOE indices (VIX, SKEW, VIX9D, VVIX, ...) with disk + memory caching."""

    def __init__(self, cache_dir: Path, ttl_minutes: float = 30, timeout: float = 25.0):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_minutes * 60
        self.timeout = timeout
        self._lock = threading.Lock()
        self._memory: dict = {}
        self.source_used: dict = {}

    def vix(self) -> Series:
        return self._get(
            "vix",
            [
                ("CBOE", lambda: self._download(CBOE_VIX_URL, "CLOSE")),
                ("FRED", lambda: self._download(FRED_VIX_URL, "VIXCLS")),
            ],
        )

    def skew(self) -> Series:
        return self._get("skew", [("CBOE", lambda: self._download(CBOE_SKEW_URL, "SKEW"))])

    def index(self, name: str) -> Series:
        """Any CBOE index published under daily_prices/<NAME>_History.csv."""
        name = name.upper()
        if name == "VIX":
            return self.vix()
        if name == "SKEW":
            return self.skew()
        url = CBOE_HISTORY_URL.format(name=name)
        return self._get(name.lower(), [("CBOE", lambda: self._download(url, name))])

    def try_index(self, name: str) -> Optional[Series]:
        try:
            return self.index(name)
        except Exception as exc:
            log.warning("Index %s unavailable: %s", name, exc)
            return None

    def series(self, key: str, sources: List[dict], external: Optional[Dict[str, Callable[[dict], Series]]] = None) -> Series:
        """A series from the first working source.

        Each source is {"type": fred|yahoo|cboe|<external>, ...}. `external` maps extra type names (e.g. longbridge)
        to callables taking the source dict; sources whose type has no fetcher are skipped.
        """
        external = external or {}
        fetchers: List[Tuple[str, Callable[[], Series]]] = []
        for src in sources:
            kind = str(src.get("type", "")).lower()
            label = src.get("label") or kind.upper()
            if kind == "fred":
                fetchers.append((f"{label}:{src['id']}", lambda s=src: self._download(FRED_URL.format(series_id=s["id"]), s["id"])))
            elif kind == "yahoo":
                fetchers.append((f"{label}:{src['symbol']}", lambda s=src: self._download_yahoo(s["symbol"], s.get("range", "5y"))))
            elif kind == "cboe":
                fetchers.append((f"{label}:{src['name']}", lambda s=src: self._download(CBOE_HISTORY_URL.format(name=s["name"].upper()), s["name"])))
            elif kind == "mof_jgb":
                fetchers.append((f"{label}:{src.get('tenor', '30Y')}", lambda s=src: self._download_mof_jgb(str(s.get("tenor", "30Y")))))
            elif kind in external:
                ident = src.get("symbol") or src.get("id") or src.get("from") or ""
                fetchers.append((f"{label}:{ident}", lambda s=src, fn=external[kind]: fn(s)))
            elif kind == "longbridge":
                continue  # no fetcher available (credentials missing)
            else:
                log.warning("Unknown series source type %r for %s", kind, key)
        if not fetchers:
            raise RuntimeError(f"no usable sources for {key}")
        return self._get(key.lower(), fetchers)

    def try_series(self, key: str, sources: List[dict], external: Optional[Dict[str, Callable[[dict], Series]]] = None) -> Optional[Series]:
        try:
            return self.series(key, sources, external)
        except Exception as exc:
            log.warning("Series %s unavailable: %s", key, exc)
            return None

    def freshness(self) -> dict:
        info = {}
        for key, entry in self._memory.items():
            info[key] = {
                "fetched_at": datetime.fromtimestamp(entry["fetched_at"]).isoformat(timespec="seconds"),
                "source": self.source_used.get(key),
                "last_date": entry["series"][-1][0] if entry["series"] else None,
                "points": len(entry["series"]),
            }
        return info

    def _get(self, key: str, sources: List[Tuple[str, Callable[[], Series]]]) -> Series:
        ttl = self.ttl
        with self._lock:
            entry = self._memory.get(key)
            if entry and time.time() - entry["fetched_at"] < ttl:
                return entry["series"]

            cache_file = self.cache_dir / f"{key}_history.csv"
            if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
                series = self._read_cache(cache_file)
                if series:
                    self.source_used[key] = f"{self._read_source(cache_file)} (cached)"
                    self._memory[key] = {"series": series, "fetched_at": cache_file.stat().st_mtime}
                    return series

            for name, fetch in sources:
                try:
                    series = fetch()
                    if not series:
                        raise ValueError("empty series")
                    self._write_cache(cache_file, series, name)
                    self.source_used[key] = name
                    self._memory[key] = {"series": series, "fetched_at": time.time()}
                    log.info("Loaded %s history from %s (%d points, last %s)", key, name, len(series), series[-1][0])
                    return series
                except Exception as exc:  # network / parsing errors: try next source
                    log.warning("Failed to load %s from %s: %s", key, name, exc)

            if cache_file.exists():
                series = self._read_cache(cache_file)
                if series:
                    log.warning("Serving stale %s cache from %s", key, cache_file)
                    self.source_used[key] = f"{self._read_source(cache_file)} (stale cache)"
                    self._memory[key] = {"series": series, "fetched_at": time.time()}
                    return series
            if entry:
                return entry["series"]
            raise RuntimeError(f"no data source available for {key}")

    def _download(self, url: str, column: str) -> Series:
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=HEADERS) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return parse_csv_series(resp.text, column)

    def _download_yahoo(self, symbol: str, range_: str = "5y") -> Series:
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            resp = client.get(YAHOO_CHART_URL.format(symbol=symbol, range=range_))
            resp.raise_for_status()
            return parse_yahoo_chart(resp.json())

    def _download_mof_jgb(self, tenor: str) -> Series:
        merged: Dict[str, float] = {}
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=HEADERS) as client:
            for url in (MOF_JGB_HISTORY_URL, MOF_JGB_CURRENT_URL):
                resp = client.get(url)
                resp.raise_for_status()
                # MOF serves Shift-JIS (cp932) with a Japanese footer note; the data rows are plain ASCII.
                merged.update(parse_mof_jgb(resp.content.decode("cp932", errors="replace"), tenor))
        return sorted(merged.items())

    @staticmethod
    def _read_cache(path: Path) -> Series:
        try:
            return parse_csv_series(path.read_text(encoding="utf-8"), "VALUE")
        except Exception as exc:
            log.warning("Could not read cache %s: %s", path, exc)
            return []

    @staticmethod
    def _read_source(path: Path) -> str:
        try:
            return path.with_suffix(".src").read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            return "unknown"

    @staticmethod
    def _write_cache(path: Path, series: Series, source: str = "") -> None:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["DATE", "VALUE"])
            writer.writerows(series)
        tmp.replace(path)
        if source:
            path.with_suffix(".src").write_text(source, encoding="utf-8")


def parse_yahoo_chart(payload: dict) -> Series:
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"yahoo chart error: {(payload.get('chart') or {}).get('error')}")
    node = result[0]
    stamps = node.get("timestamp") or []
    closes = ((node.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out: Series = []
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(int(stamp), tz=timezone.utc).date().isoformat()
        out.append((day, float(close)))
    # Yahoo can emit two bars for the live day; keep the last one.
    dedup = {}
    for day, value in out:
        dedup[day] = value
    return sorted(dedup.items())
