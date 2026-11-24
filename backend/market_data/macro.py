"""Rates, credit and dollar series with intraday/live data on top of the cached daily history.

Daily history comes from IndexHistoryProvider (FRED, Longbridge, Yahoo). Live/intraday bars come from
Yahoo's chart endpoint or Longbridge minute candlesticks and are cached for about a minute so a page
polling every minute costs one upstream request per series.
"""
from __future__ import annotations

import calendar
import csv
import html
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from backend.analytics.indicators import percentile_rank
from backend.config import Settings
from backend.market_data.fear_greed import CNN_URL as CNN_FEAR_GREED_URL, HEADERS as CNN_HEADERS
from backend.market_data.indices import BROWSER_HEADERS, BUILTIN_SOURCE_TYPES, IndexHistoryProvider, transform_series
from backend.market_data.sina import SinaClient, session_start

log = logging.getLogger(__name__)

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}&includePrePost=false"
ISM_LIST_URL = "https://www.prnewswire.com/news/institute-for-supply-management/?page={page}"
CLEVELAND_NOWCAST_URL = "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_year.json"
ZILLOW_ZORI_URL = "https://files.zillowstatic.com/research/public_csvs/zori/National_zori_uc_sfrcondomfr_sm_month.csv"
TRADINGVIEW_CALENDAR_URL = "https://economic-calendar.tradingview.com/events"
AAII_SENTIMENT_URL = "https://www.aaii.com/sentimentsurvey"
SHILLER_DATA_URL = "https://shillerdata.com/"
CFTC_COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
REDBOOK_WINDOW_DAYS = 92
MACRO_SLOW_TTL = 12 * 3600

ISM_MONTHS = {name: index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), start=1)}
CLEVELAND_SERIES = {
    "CPI Inflation": "cpi_nowcast",
    "Core CPI Inflation": "core_cpi_nowcast",
    "PCE Inflation": "pce_nowcast",
    "Core PCE Inflation": "core_pce_nowcast",
}

# range key -> intraday settings; anything else is a daily range measured in trading days.
INTRADAY_RANGES = {
    "1d": {"sessions": 1, "yahoo_range": "1d", "yahoo_interval": "5m", "lb_period": "Min_5", "lb_count": 160},
    "5d": {"sessions": 5, "yahoo_range": "5d", "yahoo_interval": "30m", "lb_period": "Min_30", "lb_count": 220},
}
DAILY_RANGES = {"1m": 22, "3m": 63, "6m": 126, "1y": 252, "3y": 756, "5y": 1260}
RANGE_KEYS = list(INTRADAY_RANGES) + list(DAILY_RANGES)


def eastern():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:
        return timezone.utc


@dataclass
class LiveData:
    source: str
    price: Optional[float]
    prev_close: Optional[float]
    as_of: Optional[str]
    bars: List[Tuple[str, float]] = field(default_factory=list)
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    bars_timezone_known: bool = True  # False when the provider supplied naive timestamps.
    chart_source: Optional[str] = None
    chart_reference: Optional[dict] = None
    session: Optional[dict] = None  # Exchange open/close of the displayed session, when known.


def _aware_stamp(value) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    return stamp


def _finite_price(value) -> Optional[float]:
    if value is None:
        return None
    price = float(value)
    if not math.isfinite(price):
        raise ValueError("non-finite price")
    return price


def _stock_quote(price, prev_close, stamp) -> tuple:
    price = _finite_price(price)
    if price is None:
        return None, None, None
    as_of = _aware_stamp(stamp).astimezone(eastern()).isoformat(timespec="minutes") if stamp is not None else None
    return price, _finite_price(prev_close), as_of


def session_reference(live: LiveData, timezone_name: str) -> Optional[dict]:
    try:
        tz = ZoneInfo(timezone_name)
        value = _finite_price(live.prev_close)
        if value is None or value <= 0 or not live.bars_timezone_known:
            return None
        session_date = _aware_stamp(live.as_of).astimezone(tz).date().isoformat()
        dates = {_aware_stamp(t).astimezone(tz).date().isoformat() for t, _ in live.bars}
        if dates != {session_date}:
            return None
        return {"kind": "previous_close", "value": value, "session_date": session_date, "timezone": timezone_name}
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def session_window(bars: List[Tuple[str, float]], timezone_name: str, trading_period: Optional[dict]) -> Optional[dict]:
    """Open/close of the session the bars belong to, in the exchange's timezone.

    Yahoo's currentTradingPeriod describes the current (or next) session, so only its clock times are reused for
    the displayed session's date. That keeps a closed market's chart at the full previous session, and an open
    market's chart at the full session width with the line ending at the latest bar.
    """
    try:
        tz = ZoneInfo(timezone_name)
        stamps = sorted(_aware_stamp(t).astimezone(tz) for t, _ in bars)
    except (ValueError, TypeError, AttributeError, OverflowError, KeyError):
        return None
    if not stamps:
        return None
    day = stamps[-1].date()
    session_stamps = [stamp for stamp in stamps if stamp.date() == day]
    open_at = close_at = None
    try:
        start = (trading_period or {}).get("start")
        end = (trading_period or {}).get("end")
        if start is not None and end is not None:
            open_local = datetime.fromtimestamp(int(start), tz)
            close_local = datetime.fromtimestamp(int(end), tz)
            open_at = datetime.combine(day, open_local.timetz().replace(tzinfo=None), tzinfo=tz)
            close_at = datetime.combine(day, close_local.timetz().replace(tzinfo=None), tzinfo=tz)
    except (TypeError, ValueError, OverflowError, OSError):
        open_at = close_at = None
    if open_at is None or close_at is None or close_at <= open_at:
        if len(session_stamps) < 2:
            return None
        open_at, close_at = session_stamps[0], session_stamps[-1]
    if close_at <= open_at:
        return None
    return {
        "date": day.isoformat(),
        "start": open_at.isoformat(timespec="minutes"),
        "end": close_at.isoformat(timespec="minutes"),
        "timezone": timezone_name,
    }


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_friday(year: int, month: int) -> date:
    day = date(year, month, calendar.monthrange(year, month)[1])
    while day.weekday() != 4:
        day -= timedelta(days=1)
    return day


def next_expiry(day: str, rule: str) -> date:
    """Expiry of the front contract quoted on `day`: CME equity-index quarters or monthly Bitcoin futures."""
    start = date.fromisoformat(day)
    if rule == "last_friday":
        for offset in range(2):
            months = start.month - 1 + offset
            year, month = start.year + months // 12, months % 12 + 1
            expiry = _last_friday(year, month)
            if expiry >= start:
                return expiry
    for offset in range(8):
        months = start.month - 1 + offset
        year, month = start.year + months // 12, months % 12 + 1
        if month not in (3, 6, 9, 12):
            continue
        expiry = _nth_weekday(year, month, 4, 3)
        if expiry >= start:
            return expiry
    return start


def latest_stock_session(live: LiveData) -> LiveData:
    bars = []
    known = live.bars_timezone_known
    try:
        tz = eastern()
        if not known or getattr(tz, "key", None) != "America/New_York":
            raise ValueError("regular-session timezone unavailable")
        sessions = {}
        for t, value in live.bars:
            stamp = _aware_stamp(t).astimezone(tz)
            if not math.isfinite(value):
                raise ValueError("non-finite bar price")
            start = stamp.replace(hour=9, minute=30, second=0, microsecond=0)
            end = stamp.replace(hour=16, minute=0, second=0, microsecond=0)
            if stamp.weekday() < 5 and start <= stamp <= end:
                sessions.setdefault(stamp.date(), []).append((stamp, t, value))
        if sessions:
            bars = [(t, value) for _, t, value in sorted(sessions[max(sessions)], key=lambda point: point[0])]
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        log.warning("Unusable stock session via %s: %s", live.source, exc)
        known = False
    values = [value for _, value in bars]
    return replace(live, bars=bars, day_high=max(values) if values else None,
                   day_low=min(values) if values else None, bars_timezone_known=known,
                   chart_source=live.source if bars else None)


def stock_session(series: List[dict]) -> Optional[dict]:
    """Regular-day reference bounds for the last displayed bar date, never a quote/today date."""
    try:
        tz = eastern()
        # eastern() has a legacy UTC fallback for other feeds; it cannot define a U.S. session.
        if getattr(tz, "key", None) != "America/New_York" or not series:
            return None
        stamps = []
        for point in series:
            stamp = datetime.fromisoformat(point["t"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None or not math.isfinite(point["v"]):
                return None
            stamps.append(stamp.astimezone(tz))
        last = max(stamps)
        return {
            "date": last.date().isoformat(),
            "start": last.replace(hour=9, minute=30, second=0, microsecond=0).isoformat(timespec="minutes"),
            "end": last.replace(hour=16, minute=0, second=0, microsecond=0).isoformat(timespec="minutes"),
            "timezone": "America/New_York",
        }
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def parse_ism_releases(text: str, kind: str = "manufacturing") -> List[Tuple[str, float]]:
    """ISM headline PMI values from a PR Newswire listing page: (first of release month, index)."""
    label = "Services" if str(kind).lower() == "services" else "Manufacturing"
    headline = re.compile(rf"\b{label} PMI\b[^0-9%]{{0,16}}([0-9]{{1,2}}(?:\.[0-9]+)?)\s*%")
    period = re.compile(r"\b(" + "|".join(ISM_MONTHS) + r")\s+(20\d{2})\b")
    out: Dict[str, float] = {}
    for _, raw in re.findall(r'<a[^>]+href="(/news-releases/[^"]+)"[^>]*>(.*?)</a>', text, flags=re.S):
        title = re.sub(r"<[^>]+>", " ", raw)
        title = re.sub(r"\s+", " ", title)
        if "Hospital PMI" in title:
            continue
        value = headline.search(title)
        month = period.search(title)
        if not value or not month:
            continue
        day = f"{month.group(2)}-{ISM_MONTHS[month.group(1)]:02d}-01"
        out.setdefault(day, float(value.group(1)))
    return sorted(out.items())


def parse_cleveland_nowcasts(payload) -> Dict[str, List[Tuple[str, float]]]:
    """Final Cleveland Fed nowcast per target month from the chart JSON, keyed by series kind."""
    out: Dict[str, Dict[str, float]] = {name: {} for name in CLEVELAND_SERIES.values()}
    for entry in payload or []:
        match = re.match(r"(\d{4})-(\d{1,2})", str((entry.get("chart") or {}).get("subcaption") or ""))
        if not match:
            continue
        day = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-01"
        for dataset in entry.get("dataset") or []:
            key = CLEVELAND_SERIES.get(str(dataset.get("seriesname") or ""))
            if not key:
                continue
            values = [float(point["value"]) for point in dataset.get("data") or []
                      if str(point.get("value", "")).strip() not in ("", "None")]
            if values:
                out[key][day] = values[-1]
    return {key: sorted(values.items()) for key, values in out.items()}


def parse_zillow_rent(text: str) -> List[Tuple[str, float]]:
    """National Zillow Observed Rent Index row: monthly median rent, dated the first of each month."""
    rows = list(csv.reader(text.splitlines()))
    if len(rows) < 2:
        return []
    header = rows[0]
    dates = []
    for column in header[4:]:
        match = re.fullmatch(r"(\d{4})-(\d{2})-\d{2}", column.strip())
        dates.append(f"{match.group(1)}-{match.group(2)}-01" if match else None)
    target = next((row for row in rows[1:] if len(row) > 3 and row[2].strip() == "United States"), None)
    if target is None:
        return []
    out = []
    for day, raw in zip(dates, target[4:]):
        if not day or str(raw).strip() in ("", "None"):
            continue
        out.append((day, float(raw)))
    return out


def parse_redbook_events(payload) -> List[Tuple[str, float]]:
    """Johnson Redbook same-store sales YoY from TradingView calendar events, dated by reference week end."""
    out: Dict[str, float] = {}
    for event in (payload or {}).get("result") or []:
        if "redbook" not in str(event.get("title") or "").lower():
            continue
        try:
            actual = float(event.get("actual"))
        except (TypeError, ValueError):
            continue
        stamp = str(event.get("referenceDate") or event.get("date") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
            continue
        out.setdefault(stamp, actual)
    return sorted(out.items())


def parse_cnn_put_call(payload) -> List[Tuple[str, float]]:
    """CNN's 5-day put/call ratio history from the fear-and-greed JSON."""
    out: Dict[str, float] = {}
    for point in ((payload or {}).get("put_call_options") or {}).get("data") or []:
        try:
            day = datetime.fromtimestamp(float(point["x"]) / 1000.0, tz=timezone.utc).date().isoformat()
            value = float(point["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0:
            out[day] = value
    return sorted(out.items())


def parse_aaii_sentiment(text: str) -> List[Tuple[str, float]]:
    """AAII weekly bull-bear spread from the public page's recent-results table."""
    start = text.find("Recent weekly results")
    end = text.find("Historical view", start if start >= 0 else 0)
    if start < 0 or end <= start:
        return []
    tokens = re.findall(r"\d{1,2}/\d{1,2}/\d{4}|\d{1,3}(?:\.\d+)?%", re.sub(r"<[^>]+>", " ", text[start:end]))
    out: Dict[str, float] = {}
    index = 0
    while index < len(tokens):
        if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", tokens[index]):
            index += 1
            continue
        values = []
        cursor = index + 1
        while cursor < len(tokens) and len(values) < 3 and tokens[cursor].endswith("%"):
            values.append(float(tokens[cursor][:-1]))
            cursor += 1
        if len(values) == 3 and all(0.0 <= value <= 100.0 for value in values) and 95.0 <= sum(values) <= 105.0:
            try:
                day = datetime.strptime(tokens[index], "%m/%d/%Y").date().isoformat()
            except ValueError:
                day = None
            if day:
                out[day] = values[0] - values[2]
        index = cursor if cursor > index else index + 1
    return sorted(out.items())


def parse_cot_net(payload) -> List[Tuple[str, float]]:
    """Weekly net non-commercial position (longs minus shorts) from a CFTC legacy COT response."""
    out: Dict[str, float] = {}
    for row in payload or []:
        stamp = str(row.get("report_date_as_yyyy_mm_dd") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
            continue
        try:
            net = float(row["noncomm_positions_long_all"]) - float(row["noncomm_positions_short_all"])
        except (KeyError, TypeError, ValueError):
            continue
        out[stamp] = net
    return sorted(out.items())


def parse_shiller_cape(rows) -> List[Tuple[str, float]]:
    """Monthly CAPE from Shiller's Data sheet rows (Date Fraction column, CAPE column)."""
    out: Dict[str, float] = {}
    for row in rows:
        if len(row) <= 12:
            continue
        try:
            fraction = float(row[5])
            cape = float(row[12])
        except (TypeError, ValueError):
            continue
        if not 1800.0 < fraction < 2200.0 or not 0.0 < cape < 200.0:
            continue
        year = int(fraction)
        month = int(round((fraction - year) * 12.0)) + 1
        if month > 12:
            year += 1
            month = 1
        out[f"{year:04d}-{month:02d}-01"] = cape
    return sorted(out.items())


class MacroService:
    def __init__(self, settings: Settings, indices: IndexHistoryProvider, client=None, sina: Optional[SinaClient] = None):
        self.settings = settings
        self.indices = indices
        self.client = client
        self.live_ttl = float(settings.get("macro.live_ttl_seconds", 55))
        self.sina = sina or SinaClient(
            quote_ttl=float(settings.get("sina.quote_ttl_seconds", 4)),
            bars_ttl=float(settings.get("sina.bars_ttl_seconds", 60)),
        )
        self.timeout = 20.0
        self._live_cache: Dict[Tuple[str, str], Tuple[float, Optional[LiveData]]] = {}
        self._slow_cache: Dict[Tuple[str, str], Tuple[float, object]] = {}
        self._lock = threading.Lock()

    def external_fetchers(self) -> Dict[str, Callable[[dict], List[Tuple[str, float]]]]:
        """Daily-history fetchers for source types beyond the plain HTTP CSV/JSON ones."""
        fetchers: Dict[str, Callable[[dict], List[Tuple[str, float]]]] = {
            "sina": lambda src: self.sina.daily(src["symbol"], float(src.get("scale", 1.0))),
            "ism": self._ism_series,
            "cleveland_nowcast": self._cleveland_series,
            "zillow_zori": self._zillow_series,
            "redbook": self._redbook_series,
            "ratio": self._ratio_series,
            "cnn_putcall": self._cnn_put_call_series,
            "aaii": self._aaii_series,
            "cot": self._cot_series,
            "shiller_cape": self._shiller_cape_series,
        }
        if self.client is not None and self.settings.longbridge_credentials_present:
            fetchers["longbridge"] = lambda src: self.client.daily_closes(src["symbol"], 1000)
        return fetchers

    def _slow(self, key: Tuple[str, str], fetch: Callable[[], object]):
        with self._lock:
            entry = self._slow_cache.get(key)
            if entry and time.time() - entry[0] < MACRO_SLOW_TTL:
                return entry[1]
        value = fetch()
        with self._lock:
            self._slow_cache[key] = (time.time(), value)
        return value

    def _ism_series(self, src: dict) -> List[Tuple[str, float]]:
        kind = str(src.get("kind", "manufacturing")).lower()
        pages = max(1, min(int(src.get("pages", 14)), 30))

        def fetch() -> List[Tuple[str, float]]:
            merged: Dict[str, float] = {}
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=BROWSER_HEADERS) as http:
                for page in range(1, pages + 1):
                    try:
                        resp = http.get(ISM_LIST_URL.format(page=page))
                        resp.raise_for_status()
                    except Exception as exc:
                        log.warning("ISM listing page %d failed: %s", page, exc)
                        break
                    for day, value in parse_ism_releases(resp.text, kind):
                        merged.setdefault(day, value)
            if not merged:
                raise RuntimeError(f"no ISM {kind} releases parsed")
            log.info("ISM %s history: %d monthly values, last %s", kind, len(merged), max(merged))
            return sorted(merged.items())

        return self._slow(("ism", kind), fetch)

    def _cleveland_series(self, src: dict) -> List[Tuple[str, float]]:
        kind = str(src.get("kind", "cpi_nowcast")).lower()

        def fetch() -> Dict[str, List[Tuple[str, float]]]:
            with httpx.Client(timeout=max(self.timeout, 60.0), follow_redirects=True, headers=BROWSER_HEADERS) as http:
                resp = http.get(CLEVELAND_NOWCAST_URL)
                resp.raise_for_status()
                return parse_cleveland_nowcasts(resp.json())

        parsed = self._slow(("cleveland", "nowcasts"), fetch)
        return parsed.get(kind, [])

    def _zillow_series(self, src: dict) -> List[Tuple[str, float]]:
        def fetch() -> List[Tuple[str, float]]:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=BROWSER_HEADERS) as http:
                resp = http.get(ZILLOW_ZORI_URL)
                resp.raise_for_status()
                return parse_zillow_rent(resp.text)

        series = self._slow(("zillow", "zori_national"), fetch)
        return transform_series(series, src.get("transform"), float(src.get("scale", 1.0)))

    def _redbook_series(self, src: dict) -> List[Tuple[str, float]]:
        years = max(1, min(int(src.get("years", 5)), 10))

        def fetch() -> List[Tuple[str, float]]:
            merged: Dict[str, float] = {}
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=365 * years + 62)
            windows = []
            cursor = start
            while cursor < end:
                following = min(cursor + timedelta(days=REDBOOK_WINDOW_DAYS), end)
                windows.append((cursor, following))
                cursor = following

            def one(window) -> List[Tuple[str, float]]:
                lo, hi = window
                params = {"from": lo.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                          "to": hi.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "countries": "US"}
                with httpx.Client(timeout=max(self.timeout, 45.0), follow_redirects=True,
                                  headers={**BROWSER_HEADERS, "Origin": "https://www.tradingview.com",
                                           "Referer": "https://www.tradingview.com/"}) as http:
                    resp = http.get(TRADINGVIEW_CALENDAR_URL, params=params)
                    resp.raise_for_status()
                    return parse_redbook_events(resp.json())

            with ThreadPoolExecutor(max_workers=4) as pool:
                for chunk in pool.map(one, windows):
                    merged.update(dict(chunk))
            if not merged:
                raise RuntimeError("no Redbook events parsed")
            log.info("Redbook history: %d weekly values, last %s", len(merged), max(merged))
            return sorted(merged.items())

        return self._slow(("redbook", str(years)), fetch)

    def _ratio_series(self, src: dict) -> List[Tuple[str, float]]:
        numerator = src.get("numerator") or {}
        denominator = src.get("denominator") or {}
        if not numerator or not denominator:
            raise RuntimeError("ratio source needs numerator and denominator")
        ident = lambda part: str(part.get("symbol") or part.get("name") or part.get("id") or "")
        key = f"ratio:{ident(numerator)}/{ident(denominator)}"
        safe = re.sub(r"[^a-z0-9_.-]+", "_", key.lower())
        top = self.indices.series(safe + "_num", [numerator])
        bottom = dict(self.indices.series(safe + "_den", [denominator]))
        series = [(day, value / bottom[day]) for day, value in top if bottom.get(day)]
        if not series:
            raise RuntimeError(f"no overlapping dates for {key}")
        log.info("Ratio %s: %d daily values, last %s", key, len(series), series[-1][0])
        return transform_series(series, src.get("transform"), float(src.get("scale", 1.0)))

    def _cnn_put_call_series(self, src: dict) -> List[Tuple[str, float]]:
        def fetch() -> List[Tuple[str, float]]:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=CNN_HEADERS) as http:
                resp = http.get(CNN_FEAR_GREED_URL)
                resp.raise_for_status()
                series = parse_cnn_put_call(resp.json())
            if not series:
                raise RuntimeError("no CNN put/call history parsed")
            log.info("CNN put/call: %d daily values, last %s", len(series), series[-1][0])
            return series

        return self._slow(("cnn", "put_call"), fetch)

    def _aaii_series(self, src: dict) -> List[Tuple[str, float]]:
        """Weekly spread accumulated locally: AAII only exposes four recent weeks publicly."""
        path = self.settings.data_dir / "aaii_sentiment.csv"

        def fetch() -> List[Tuple[str, float]]:
            rows: Dict[str, float] = {}
            if path.exists():
                with path.open(newline="", encoding="utf-8") as handle:
                    for row in csv.DictReader(handle):
                        try:
                            rows[row["date"]] = float(row["spread"])
                        except (KeyError, TypeError, ValueError):
                            continue
            fresh: List[Tuple[str, float]] = []
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=BROWSER_HEADERS) as http:
                    resp = http.get(AAII_SENTIMENT_URL)
                    resp.raise_for_status()
                    fresh = parse_aaii_sentiment(resp.text)
            except Exception as exc:
                if not rows:
                    raise
                log.warning("AAII sentiment refresh failed, using local history: %s", exc)
            if fresh:
                changed = any(rows.get(day) != value for day, value in fresh)
                rows.update(dict(fresh))
                if changed:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp")
                    with tmp.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(["date", "spread"])
                        for day in sorted(rows):
                            writer.writerow([day, rows[day]])
                    tmp.replace(path)
                    log.info("AAII sentiment: %d weekly values, last %s", len(rows), max(rows))
            if not rows:
                raise RuntimeError("no AAII sentiment parsed")
            return sorted(rows.items())

        return self._slow(("aaii", "spread"), fetch)

    def _cot_series(self, src: dict) -> List[Tuple[str, float]]:
        code = str(src.get("code", "")).strip()
        if not code:
            raise RuntimeError("COT source needs a contract code")

        def fetch() -> List[Tuple[str, float]]:
            params = {
                "$where": f"cftc_contract_market_code='{code}'",
                "$select": "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all",
                "$order": "report_date_as_yyyy_mm_dd ASC",
                "$limit": "5000",
            }
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=BROWSER_HEADERS) as http:
                resp = http.get(CFTC_COT_URL, params=params)
                resp.raise_for_status()
                series = parse_cot_net(resp.json())
            if not series:
                raise RuntimeError(f"no COT rows for {code}")
            log.info("COT %s: %d weekly values, last %s", code, len(series), series[-1][0])
            return transform_series(series, src.get("transform"), float(src.get("scale", 1.0)))

        return self._slow(("cot", code), fetch)

    def _shiller_cape_series(self, src: dict) -> List[Tuple[str, float]]:
        def fetch() -> List[Tuple[str, float]]:
            import xlrd

            with httpx.Client(timeout=max(self.timeout, 60.0), follow_redirects=True, headers=BROWSER_HEADERS) as http:
                page = http.get(SHILLER_DATA_URL)
                page.raise_for_status()
                match = re.search(r'href="([^"]*ie_data\.xls[^"]*)"', page.text, re.I)
                if not match:
                    raise RuntimeError("Shiller ie_data.xls link not found")
                url = html.unescape(match.group(1))
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = "https://shillerdata.com" + url
                resp = http.get(url)
                resp.raise_for_status()
            book = xlrd.open_workbook(file_contents=resp.content)
            sheet = book.sheet_by_name("Data")
            series = parse_shiller_cape([sheet.row_values(index) for index in range(sheet.nrows)])
            if not series:
                raise RuntimeError("no CAPE values parsed")
            log.info("Shiller CAPE: %d monthly values, last %s", len(series), series[-1][0])
            return series

        return self._slow(("shiller", "cape"), fetch)

    # ------------------------------------------------------------------ public
    def sections(self) -> List[dict]:
        out = []
        for section in self.settings.get("live_sections", []) or []:
            if section.get("id"):
                symbols = section.get("symbols")
                groups = section["groups"] if isinstance(section.get("groups"), list) else []
                group_keys = {str(key).upper() for group in groups for key in group.get("series", [])}
                series_keys = [str(spec["key"]).upper() for spec in (section.get("series") or []) if spec.get("key")]
                out.append({
                    "id": str(section["id"]),
                    "title": section.get("title", section["id"]),
                    "curve": bool(section.get("curve", False)),
                    "hidden": bool(section.get("hidden", False)),
                    "chart_mode": section.get("chart_mode"),
                    "groups": groups,
                    # Only series outside a group get their own mini chart; a section whose series all feed
                    # groups (e.g. the daily comparisons) renders as overlay cards alone.
                    "has_series": any(key not in group_keys for key in series_keys),
                    "daily_only": bool(section.get("daily_only", False)),
                    "ranges": section["ranges"] if isinstance(section.get("ranges"), list) else list(RANGE_KEYS),
                    "default_range": section.get("default_range", self.settings.get("macro.default_range", "1d")),
                    "refresh_seconds": float(section.get("refresh_seconds", self.live_ttl)),
                    "live_ttl_seconds": float(section["live_ttl_seconds"]) if section.get("live_ttl_seconds") else None,
                    # Symbol sections chart a user-editable ticker list; this is the default list from config.yaml.
                    "symbols": [self.normalize_symbol(s) for s in symbols] if isinstance(symbols, list) else None,
                })
        return out

    def live_value(self, section_id: str, key: str) -> Optional[Tuple[str, float]]:
        """(ISO ET timestamp, price) of a series' live feed, or None. Shares the live cache with snapshot()."""
        section = next((s for s in self.sections() if s["id"] == section_id), None)
        if section is None or section["daily_only"]:
            return None
        for raw_spec in self.specs(section_id):
            if str(raw_spec["key"]).upper() != key.upper():
                continue
            variant_name, spec = self._resolve_variant(raw_spec, None, BUILTIN_SOURCE_TYPES | set(self.external_fetchers()))
            series_key = self._cache_prefix(section) + (f"{key.upper()}_{variant_name}" if variant_name else key.upper())
            live = self._live(series_key, spec, "1d", section["live_ttl_seconds"])
            if live and live.price is not None and live.as_of:
                return live.as_of, live.price
            return None
        return None

    @staticmethod
    def _cache_prefix(section: dict) -> str:
        # Symbol sections cache under their own namespace so a ticker never collides with an index key (e.g. VIX).
        return f"{section['id'].upper()}_" if section.get("symbols") is not None else ""

    def specs(self, section_id: str = "macro", symbols: Optional[List[str]] = None) -> List[dict]:
        for section in self.settings.get("live_sections", []) or []:
            if str(section.get("id")) == section_id:
                if isinstance(section.get("symbols"), list):
                    chosen = section["symbols"] if symbols is None else symbols
                    return [self.symbol_spec(s) for s in chosen]
                return [s for s in (section.get("series") or []) if s.get("key")]
        return []

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        symbol = str(symbol).strip().upper()
        return symbol if "." in symbol else f"{symbol}.US"

    @classmethod
    def symbol_spec(cls, symbol: str) -> dict:
        """Series spec for a plain stock/ETF ticker: Longbridge first, Yahoo as fallback for US names."""
        symbol = cls.normalize_symbol(symbol)
        ticker = symbol[:-3] if symbol.endswith(".US") else symbol
        sources = [{"type": "longbridge", "symbol": symbol}]
        if symbol.endswith(".US"):
            sources.append({"type": "yahoo", "symbol": ticker})
        return {
            "key": ticker,
            "label": f"{symbol} last price",
            "unit": "usd",
            "sources": sources,
            "live": list(sources),
        }

    def snapshot(self, range_key: str = "1d", section_id: str = "macro", variants: Optional[Dict[str, str]] = None,
                 symbols: Optional[List[str]] = None) -> dict:
        range_key = range_key.lower() if range_key.lower() in RANGE_KEYS else "1d"
        section = next((s for s in self.sections() if s["id"] == section_id), None)
        if section is None:
            raise KeyError(f"unknown live section {section_id!r}")
        daily_only = section["daily_only"]
        if daily_only and range_key not in DAILY_RANGES:
            range_key = section["default_range"]
        window_start = window_end = None
        if daily_only:
            end = datetime.now(eastern()).date()
            months = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60}[range_key]
            year, month = divmod(end.year * 12 + end.month - 1 - months, 12)
            month += 1
            # Every component shares inclusive calendar bounds, clamped at month-end rather than by row count.
            window_start = end.replace(year=year, month=month, day=min(end.day, calendar.monthrange(year, month)[1])).isoformat()
            window_end = end.isoformat()
        intraday = range_key in INTRADAY_RANGES and not daily_only
        history_days = int(self.settings.get("data.history_days", 1260))
        # Daily-only sections still need non-Yahoo history (e.g. Sina spot XAU for an overlay group).
        fetchers = self.external_fetchers()
        available_types = BUILTIN_SOURCE_TYPES | set(fetchers)
        variants = {str(k).upper(): str(v) for k, v in (variants or {}).items()}
        live_ttl = section["live_ttl_seconds"]
        prefix = self._cache_prefix(section)

        resolved = []
        for raw_spec in self.specs(section_id, symbols):
            key = str(raw_spec["key"]).upper()
            variant_name, spec = self._resolve_variant(raw_spec, variants.get(key), available_types)
            resolved.append((key, raw_spec, variant_name, spec))
        # One Sina request refreshes every Sina-backed quote in the section instead of one request per card.
        sina_symbols = [] if daily_only else [str(src["symbol"]) for _, _, _, spec in resolved
                        for src in (spec.get("live") or [])[:1] if str(src.get("type", "")).lower() == "sina"]
        if sina_symbols:
            self.sina.quotes(sina_symbols)

        items: dict = {}
        latest: dict = {}
        daily_by_key: Dict[str, List[Tuple[str, float]]] = {}
        specs_by_key = {key: spec for key, _, _, spec in resolved}
        for configured_section in self.sections():
            for raw_spec in self.specs(configured_section["id"]):
                candidate_key = str(raw_spec["key"]).upper()
                if candidate_key not in specs_by_key:
                    _, candidate = self._resolve_variant(raw_spec, None, available_types)
                    specs_by_key[candidate_key] = candidate

        def daily_for(key: str, trail: Optional[set] = None) -> List[Tuple[str, float]]:
            if key in daily_by_key:
                return daily_by_key[key]
            trail = set(trail or ())
            if key in trail:
                log.warning("Derived series dependency cycle at %s", key)
                return []
            spec = specs_by_key.get(key)
            if spec is None:
                log.warning("Derived series dependency %s is not in this section", key)
                return []
            trail.add(key)
            sources = spec.get("sources", [])
            derived = next((src for src in sources if str(src.get("type", "")).lower() in ("derived_spread", "derived_basis")), None)
            if derived and str(derived.get("type", "")).lower() == "derived_basis":
                futures = self.indices.try_series(f"{key.lower()}_futures", [derived.get("futures") or {}], fetchers) or []
                spot = self.indices.try_series(f"{key.lower()}_spot", [derived.get("spot") or {}], fetchers) or []
                spot_by_date = dict(spot)
                metric = str(derived.get("metric", "gap")).lower()
                rule = str(derived.get("expiry", "third_friday")).lower()
                scale = float(derived.get("scale", 1.0))
                daily = []
                for day, future in futures:
                    cash = spot_by_date.get(day)
                    if cash is None or cash <= 0:
                        continue
                    if metric == "annualized":
                        # A front contract in its final days makes the denominator collapse; floor it so the
                        # non-simultaneous closes do not turn into hundred-percent annualized spikes.
                        days = max((next_expiry(day, rule) - date.fromisoformat(day)).days, 7)
                        value = (future / cash - 1.0) * 365.0 / days * 100.0 * scale
                    else:
                        value = (future - cash) * scale
                    if math.isfinite(value):
                        daily.append((day, value))
            elif derived:
                left = daily_for(str(derived.get("left", "")).upper(), trail)
                right = daily_for(str(derived.get("right", "")).upper(), trail)
                right_by_date = dict(right)
                scale = float(derived.get("scale", 1.0))
                daily = [(day, (value - right_by_date[day]) * scale) for day, value in left if day in right_by_date]
            else:
                cache_key = prefix + key if key in {item_key for item_key, _, _, _ in resolved} else key
                variant = next((name for item_key, _, name, _ in resolved if item_key == key), None)
                if variant:
                    cache_key = prefix + f"{key}_{variant}"
                daily = self.indices.try_series(cache_key, sources, fetchers) or []
            if daily_only:
                daily = [(day, value) for day, value in daily
                         if day <= window_end and value is not None and math.isfinite(value)]
            daily_by_key[key] = daily
            return daily

        for key, _, _, _ in resolved:
            daily_for(key)

        for key, raw_spec, variant_name, spec in resolved:
            series_key = prefix + (f"{key}_{variant_name}" if variant_name else key)
            unit = spec.get("unit", "index")
            daily = daily_for(key)
            stock_1d = range_key == "1d" and section.get("symbols") is not None and any(
                str(src.get("symbol", "")).endswith(".US") for src in spec.get("sources", [])
            )
            # Metrics (last price, daily change, day range) always come from the 1-day feed; the chart's bars
            # come from the requested intraday range so a 5D view still shows a proper daily change.
            live = bars_live = None
            if not daily_only:
                live = (self._live(series_key, spec, "1d", live_ttl, stock_1d=True) if stock_1d
                        else self._live(series_key, spec, "1d", live_ttl))
                bars_live = live if range_key == "1d" else (self._live(series_key, spec, range_key, live_ttl) if intraday else None)
            entry = {"label": spec.get("label", key), "unit": unit, "range": range_key, "intraday": intraday}
            entry["variant"] = variant_name
            entry["variants"] = [{"name": n, "label": (v or {}).get("label", n)} for n, v in (raw_spec.get("variants") or {}).items()]
            entry["note"] = spec.get("note")
            derived_source = next((src for src in spec.get("sources", []) if str(src.get("type", "")).lower() in ("derived_spread", "derived_basis")), None)
            if derived_source:
                default_label = "Derived basis" if str(derived_source.get("type", "")).lower() == "derived_basis" else "Derived spread"
                entry["daily_source"] = derived_source.get("label", default_label)
            else:
                entry["daily_source"] = self.indices.source_used.get(series_key.lower())
            if daily_only and not entry["daily_source"]:
                configured_sources = []
                for src in spec.get("sources", []):
                    label = src.get("label") or str(src.get("type", "")).upper()
                    ident = src.get("id") or src.get("symbol") or src.get("name") or src.get("tenor")
                    configured_sources.append(f"{label}:{ident}" if ident else label)
                entry["daily_source"] = ", ".join(configured_sources) or None
            entry["live_source"] = live.source if live and (not stock_1d or live.price is not None) else None
            entry["live_note"] = None if (live or not spec.get("live")) else "no live feed reachable"
            if not spec.get("live"):
                entry["live_note"] = "daily data only (no intraday source for this series)"
            if daily_only:
                entry["live_note"] = "daily data only"
                entry["daily_last_date"] = daily[-1][0] if daily else None

            if daily_only:
                series = [{"t": day, "v": value} for day, value in daily if window_start <= day <= window_end]
            elif intraday and bars_live and bars_live.bars:
                series = [{"t": t, "v": v} for t, v in bars_live.bars]
            elif intraday:
                # No intraday feed: fall back to the last month of daily closes so the card is never empty.
                series = [{"t": d, "v": v} for d, v in daily[-DAILY_RANGES["1m"]:]]
                entry["intraday"] = False
                entry["fallback"] = "daily"
            else:
                points = list(daily[-DAILY_RANGES[range_key]:])
                live_date = live.as_of[:10] if live and live.as_of else None
                if live_date and spec.get("timezone"):
                    live_date = _aware_stamp(live.as_of).astimezone(ZoneInfo(spec["timezone"])).date().isoformat()
                if live and live.price is not None and live_date and (not points or live_date > points[-1][0]):
                    points.append((live_date, live.price))
                series = [{"t": d, "v": v} for d, v in points]
            entry["series"] = series if daily_only else series[-max(history_days, 2000):]
            if spec.get("timezone"):
                entry["timezone"] = spec["timezone"]
            if section.get("chart_mode") == "percent_change":
                session_chart = range_key == "1d" and entry["intraday"]
                entry["chart_reference"] = (
                    bars_live.chart_reference if session_chart and bars_live and bars_live.chart_reference
                    else {"kind": "first_point_fallback" if session_chart else "first_point", "value": None}
                )
            if not stock_1d and entry["intraday"] and bars_live and bars_live.session:
                entry["session"] = bars_live.session
            if stock_1d:
                entry["chart_source"] = bars_live.chart_source if entry["intraday"] and bars_live else None
            if stock_1d and entry["intraday"] and not derived_source:
                entry["session"] = stock_session(entry["series"]) if bars_live.bars_timezone_known else None
                if entry["session"] is None:
                    entry["session_note"] = "session time unavailable"
            metrics_daily = daily if not daily_only or series else []
            entry["metrics"] = self._metrics(metrics_daily, live)
            if stock_1d and live and live.price is not None:
                entry["metrics"]["as_of"] = live.as_of
            if not metrics_daily and not (live and (live.price is not None or (stock_1d and live.bars))):
                entry["error"] = "unavailable"
            if entry["metrics"].get("value") is not None:
                latest[key] = entry["metrics"]["value"]
            items[key] = entry
        payload = {
            "section": section,
            "range": range_key,
            "ranges": section["ranges"],
            "items": items,
            "curve": self._curve(latest) if section["curve"] else {},
            "generated_at": datetime.now(eastern()).isoformat(timespec="seconds"),
            "live_ttl_seconds": live_ttl or self.live_ttl,
            "refresh_seconds": section["refresh_seconds"],
        }
        if daily_only:
            payload.update(window_start=window_start, window_end=window_end)
        return payload

    # ------------------------------------------------------------------ metrics
    @staticmethod
    def _resolve_variant(spec: dict, requested: Optional[str], available_types: Optional[set] = None) -> Tuple[Optional[str], dict]:
        """Merge the chosen variant (requested, else default, else first usable) into the series spec.

        A variant is usable when its primary (first) daily source has a working fetcher, so a spot variant
        that depends on an unconfigured API key is skipped by default (but can still be requested explicitly).
        """
        variants = spec.get("variants") or {}
        if not variants:
            return None, spec
        available = available_types if available_types is not None else set(BUILTIN_SOURCE_TYPES)

        def usable(name: str) -> bool:
            sources = (variants.get(name) or {}).get("sources") or []
            return not sources or str(sources[0].get("type", "")).lower() in available

        if requested in variants:
            name = requested
        else:
            order = [str(spec.get("default_variant"))] + [n for n in variants if n != str(spec.get("default_variant"))]
            name = next((n for n in order if n in variants and usable(n)), next(iter(variants)))
        merged = {k: v for k, v in spec.items() if k not in ("variants", "default_variant", "sources", "live", "note")}
        merged.update(variants[name] or {})
        return name, merged

    @staticmethod
    def _metrics(daily: List[Tuple[str, float]], live: Optional[LiveData]) -> dict:
        values = [v for _, v in daily]
        value = live.price if live and live.price is not None else (values[-1] if values else None)
        if live and live.prev_close:
            prev = live.prev_close
        else:
            prev = values[-2] if len(values) >= 2 else None
        change = (value - prev) if value is not None and prev is not None else None
        window = values[-252:] + ([value] if value is not None else [])
        return {
            "value": value,
            "prev_close": prev,
            "change": change,
            "change_pct": (change / prev * 100.0) if change is not None and prev else None,
            "day_high": live.day_high if live else None,
            "day_low": live.day_low if live else None,
            "high_52w": max(window) if window else None,
            "low_52w": min(window) if window else None,
            "percentile": percentile_rank(window, 253) if len(window) > 2 else None,
            "as_of": live.as_of if live and live.as_of else (daily[-1][0] if daily else None),
            "live": bool(live and live.price is not None),
            "daily_last_date": daily[-1][0] if daily else None,
        }

    @staticmethod
    def _curve(latest: dict) -> dict:
        curve = {}
        for name, short, long_ in (("2s10s", "US2Y", "US10Y"), ("5s30s", "US5Y", "US30Y"), ("2s30s", "US2Y", "US30Y")):
            if latest.get(short) is not None and latest.get(long_) is not None:
                bp = (latest[long_] - latest[short]) * 100.0
                curve[name] = {"bp": bp, "state": "inverted" if bp < 0 else "normal", "short": short, "long": long_}
        return curve

    # ------------------------------------------------------------------ live data
    def _live(self, key: str, spec: dict, range_key: str, ttl: Optional[float] = None,
              *, stock_1d: bool = False) -> Optional[LiveData]:
        sources = spec.get("live") or []
        if not sources:
            return None
        cache_key = (key, "stock_1d" if stock_1d else range_key)
        # Sina quotes are cheap and meant for a ~5 s refresh; the other feeds keep the minute-level TTL unless
        # the section asks for a faster one.
        if str(sources[0].get("type", "")).lower() == "sina":
            ttl = self.sina.quote_ttl
        elif not ttl:
            ttl = self.live_ttl
        with self._lock:
            cached = self._live_cache.get(cache_key)
            if cached and time.time() - cached[0] < ttl:
                return cached[1]
        result: Optional[LiveData] = None
        if stock_1d:
            result = self._stock_live(sources)
        else:
            for src in sources:
                kind = str(src.get("type", "")).lower()
                try:
                    if kind == "yahoo":
                        result = self._yahoo_intraday(src["symbol"], range_key, **(
                            {"timezone_name": src["timezone"]} if src.get("timezone") else {}
                        ))
                    elif kind == "sina":
                        result = self._sina_live(src, range_key)
                    elif kind == "longbridge":
                        if self.client is None or not self.settings.longbridge_credentials_present:
                            continue
                        result = self._longbridge_intraday(src["symbol"], range_key)
                    else:
                        log.warning("Unknown live source type %r for %s", kind, key)
                        continue
                    if result and (result.price is not None or result.bars):
                        break
                    result = None
                except Exception as exc:
                    log.warning("Live data for %s via %s failed: %s", key, src, exc)
                    result = None
        with self._lock:
            self._live_cache[cache_key] = (time.time(), result)
        return result

    def _stock_live(self, sources: List[dict]) -> Optional[LiveData]:
        quote = None
        chart = None
        for src in sources:
            kind = str(src.get("type", "")).lower()
            if kind == "longbridge" and (self.client is None or not self.settings.longbridge_credentials_present):
                continue
            for lookback in ((False, True) if kind == "yahoo" and chart is None else (False,)):
                try:
                    if kind == "yahoo":
                        candidate = self._yahoo_intraday(src["symbol"], "1d", stock_1d=True, lookback=lookback)
                    elif kind == "longbridge":
                        candidate = self._longbridge_intraday(src["symbol"], "1d", stock_1d=True)
                    else:
                        log.warning("Unknown stock live source type %r", kind)
                        break
                    if candidate is None:
                        continue
                    candidate = latest_stock_session(candidate)
                    if quote is None and not lookback and candidate.price is not None:
                        quote = candidate
                    if chart is None and candidate.bars:
                        chart = candidate
                    if chart is not None:
                        break
                except Exception as exc:
                    log.warning("Stock live data via %s (lookback=%s) failed: %s", src, lookback, exc)
            if chart is not None and quote is not None:
                break
        base = quote or chart
        if base is None:
            return None
        return replace(base, bars=chart.bars if chart else [], chart_source=chart.source if chart else None,
                       day_high=chart.day_high if chart else None, day_low=chart.day_low if chart else None,
                       chart_reference=chart.chart_reference if chart else None,
                       bars_timezone_known=chart.bars_timezone_known if chart else base.bars_timezone_known)

    def _sina_live(self, src: dict, range_key: str) -> LiveData:
        """Live quote plus 5- or 30-minute bars for the current Sina trading day(s), with the tick appended."""
        symbol = str(src["symbol"]).upper()
        scale = float(src.get("scale", 1.0))
        quote = self.sina.quotes([symbol]).get(symbol)
        if quote is None:
            raise ValueError(f"no Sina quote for hf_{symbol}")
        quote = quote.scaled(scale)
        tz = eastern()
        now = datetime.now(tz)
        if range_key == "1d":
            bars = self.sina.minute_bars(symbol, 5, scale)
            start = session_start(now)
            # Outside trading hours (weekends, holidays, pre-open) the current session has no bars yet:
            # show the last session that does, so the 1D chart is never a single dot. A bar stamped exactly
            # at the boundary closes the previous session, hence <=.
            if bars and bars[-1][0] <= start.isoformat(timespec="minutes"):
                start = session_start(datetime.fromisoformat(bars[-1][0]) - timedelta(minutes=1))
        else:
            bars = self.sina.minute_bars(symbol, 30, scale)
            start = session_start(now) - timedelta(days=7)  # ~5 trading sessions on a 24h market
        cutoff = start.isoformat(timespec="minutes")
        bars = [b for b in bars if b[0] > cutoff]
        if not bars or quote.as_of > bars[-1][0]:
            bars.append((quote.as_of, quote.last))
        return LiveData(
            source=f"Sina hf_{symbol} ({quote.name})",
            price=quote.last,
            prev_close=quote.prev_close,
            as_of=quote.as_of,
            bars=bars,
            day_high=quote.high if range_key == "1d" else None,
            day_low=quote.low if range_key == "1d" else None,
        )

    def _yahoo_intraday(self, symbol: str, range_key: str, *, stock_1d: bool = False,
                        lookback: bool = False, timezone_name: Optional[str] = None) -> LiveData:
        cfg = INTRADAY_RANGES[range_key]
        url = YAHOO_CHART_URL.format(symbol=symbol, range="5d" if lookback else cfg["yahoo_range"],
                                     interval="5m" if lookback else cfg["yahoo_interval"])
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=BROWSER_HEADERS) as http:
            resp = http.get(url)
            resp.raise_for_status()
            payload = resp.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise ValueError(f"yahoo chart error: {(payload.get('chart') or {}).get('error')}")
        node = result[0]
        meta = node.get("meta") or {}
        tz = ZoneInfo(timezone_name) if timezone_name else eastern()
        if stock_1d:
            bars = []
            known = True
            try:
                stamps = node.get("timestamp") or []
                closes = ((node.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
                bars = [(datetime.fromtimestamp(float(t), tz=tz).isoformat(), _finite_price(c))
                        for t, c in zip(stamps, closes) if c is not None]
            except Exception as exc:
                known = False
                log.warning("Stock bars via Yahoo %s failed: %s", symbol, exc)
            price = prev_close = as_of = None
            # A multi-day chartPreviousClose is not a one-day comparison baseline.
            if not lookback:
                try:
                    stamp = meta.get("regularMarketTime")
                    stamp = datetime.fromtimestamp(float(stamp), tz=tz) if stamp is not None else None
                    price, prev_close, as_of = _stock_quote(meta.get("regularMarketPrice"), meta.get("chartPreviousClose"), stamp)
                except Exception as exc:
                    log.warning("Stock quote via Yahoo %s failed: %s", symbol, exc)
            live = LiveData(source=f"Yahoo {symbol}", price=price, prev_close=prev_close, as_of=as_of,
                            bars=bars, bars_timezone_known=known)
            if not lookback:
                live.chart_reference = session_reference(live, "America/New_York")
            return live
        stamps = node.get("timestamp") or []
        closes = ((node.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        bars = [(datetime.fromtimestamp(int(t), tz=tz).isoformat(timespec="minutes"), float(c)) for t, c in zip(stamps, closes) if c is not None]
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        as_of = None
        if meta.get("regularMarketTime"):
            as_of = datetime.fromtimestamp(int(meta["regularMarketTime"]), tz=tz).isoformat(timespec="minutes")
        elif bars:
            as_of = bars[-1][0]
        if timezone_name:
            bars = sorted((t, v) for t, v in bars if math.isfinite(v))
            price = _finite_price(price)
        values = [v for _, v in bars]
        live = LiveData(
            source=f"Yahoo {symbol}",
            price=float(price) if price is not None else None,
            prev_close=float(meta["chartPreviousClose"]) if meta.get("chartPreviousClose") is not None else None,
            as_of=as_of,
            bars=bars,
            day_high=max(values) if values and range_key == "1d" else None,
            day_low=min(values) if values and range_key == "1d" else None,
        )
        if timezone_name and live.prev_close is not None and not math.isfinite(live.prev_close):
            live.prev_close = None
        if timezone_name and range_key == "1d":
            # Validate the reference against the whole response first: a multi-session payload cannot claim a
            # one-session previous close even though the chart is trimmed to the latest session below.
            if meta.get("regularMarketTime"):
                live.chart_reference = session_reference(live, timezone_name)
            if bars:
                live.bars = [(t, v) for t, v in bars if t[:10] == bars[-1][0][:10]]
                live.day_high = max(v for _, v in live.bars)
                live.day_low = min(v for _, v in live.bars)
            trading_period = (meta.get("currentTradingPeriod") or {}).get("regular")
            live.session = session_window(live.bars, timezone_name, trading_period)
        return live

    def _longbridge_intraday(self, symbol: str, range_key: str, *, stock_1d: bool = False) -> LiveData:
        cfg = INTRADAY_RANGES[range_key]
        tz = eastern()
        if stock_1d:
            bars = []
            known = True
            try:
                raw = self.client.intraday_bars(symbol, cfg["lb_period"], cfg["lb_count"])
                bars = [(_aware_stamp(stamp).astimezone(tz).isoformat(), _finite_price(close)) for stamp, close in raw]
            except Exception as exc:
                known = False
                log.warning("Stock bars via Longbridge %s failed: %s", symbol, exc)
            price = prev_close = as_of = None
            try:
                quote = self.client.spot(symbol)
                price, prev_close, as_of = _stock_quote(quote.get("last"), quote.get("prev_close"), quote.get("timestamp"))
            except Exception as exc:
                log.warning("Stock quote via Longbridge %s failed: %s", symbol, exc)
            live = latest_stock_session(LiveData(source=f"Longbridge {symbol}", price=price, prev_close=prev_close, as_of=as_of,
                                                 bars=bars, bars_timezone_known=known))
            live.chart_reference = session_reference(live, "America/New_York")
            return live
        raw = self.client.intraday_bars(symbol, cfg["lb_period"], cfg["lb_count"])
        by_session: Dict[str, List[Tuple[str, float]]] = {}
        timezone_known = True
        for stamp, close in raw:
            # Legacy local-time guesses cannot establish a stock-session timezone.
            timezone_known = timezone_known and stamp.tzinfo is not None and stamp.utcoffset() is not None
            local = stamp if stamp.tzinfo else stamp.astimezone()
            et = local.astimezone(tz)
            by_session.setdefault(et.date().isoformat(), []).append((et.isoformat(timespec="minutes"), close))
        sessions = sorted(by_session)[-cfg["sessions"]:]
        bars = [bar for day in sessions for bar in by_session[day]]
        quote = self.client.spot(symbol)
        values = [v for _, v in bars]
        stamp = quote.get("timestamp")
        as_of = None
        if stamp:
            try:
                parsed = datetime.fromisoformat(stamp)
                parsed = parsed if parsed.tzinfo else parsed.astimezone()
                as_of = parsed.astimezone(tz).isoformat(timespec="minutes")
            except ValueError:
                as_of = bars[-1][0] if bars else None
        return LiveData(
            source=f"Longbridge {symbol}",
            price=quote.get("last"),
            prev_close=quote.get("prev_close"),
            as_of=as_of,
            bars=bars,
            day_high=max(values) if values and range_key == "1d" else None,
            day_low=min(values) if values and range_key == "1d" else None,
            bars_timezone_known=timezone_known,
        )
