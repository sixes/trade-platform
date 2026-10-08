"""Rates, credit and dollar series with intraday/live data on top of the cached daily history.

Daily history comes from IndexHistoryProvider (FRED, Longbridge, Yahoo). Live/intraday bars come from
Yahoo's chart endpoint or Longbridge minute candlesticks and are cached for about a minute so a page
polling every minute costs one upstream request per series.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import httpx

from backend.analytics.indicators import percentile_rank
from backend.config import Settings
from backend.market_data.indices import BROWSER_HEADERS, BUILTIN_SOURCE_TYPES, IndexHistoryProvider
from backend.market_data.sina import SinaClient, session_start

log = logging.getLogger(__name__)

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}&includePrePost=false"

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
    as_of: Optional[str]  # ISO datetime in US Eastern
    bars: List[Tuple[str, float]] = field(default_factory=list)  # (ISO ET datetime, value)
    day_high: Optional[float] = None
    day_low: Optional[float] = None


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
        self._lock = threading.Lock()

    def external_fetchers(self) -> Dict[str, Callable[[dict], List[Tuple[str, float]]]]:
        """Daily-history fetchers for source types beyond the plain HTTP CSV/JSON ones."""
        fetchers: Dict[str, Callable[[dict], List[Tuple[str, float]]]] = {
            "sina": lambda src: self.sina.daily(src["symbol"], float(src.get("scale", 1.0))),
        }
        if self.client is not None and self.settings.longbridge_credentials_present:
            fetchers["longbridge"] = lambda src: self.client.daily_closes(src["symbol"], 1000)
        return fetchers

    # ------------------------------------------------------------------ public
    def sections(self) -> List[dict]:
        out = []
        for section in self.settings.get("live_sections", []) or []:
            if section.get("id"):
                symbols = section.get("symbols")
                out.append({
                    "id": str(section["id"]),
                    "title": section.get("title", section["id"]),
                    "curve": bool(section.get("curve", False)),
                    "hidden": bool(section.get("hidden", False)),
                    "refresh_seconds": float(section.get("refresh_seconds", self.live_ttl)),
                    "live_ttl_seconds": float(section["live_ttl_seconds"]) if section.get("live_ttl_seconds") else None,
                    # Symbol sections chart a user-editable ticker list; this is the default list from config.yaml.
                    "symbols": [self.normalize_symbol(s) for s in symbols] if isinstance(symbols, list) else None,
                })
        return out

    def live_value(self, section_id: str, key: str) -> Optional[Tuple[str, float]]:
        """(ISO ET timestamp, price) of a series' live feed, or None. Shares the live cache with snapshot()."""
        section = next((s for s in self.sections() if s["id"] == section_id), None)
        if section is None:
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
        intraday = range_key in INTRADAY_RANGES
        history_days = int(self.settings.get("data.history_days", 1260))
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
        sina_symbols = [str(src["symbol"]) for _, _, _, spec in resolved
                        for src in (spec.get("live") or [])[:1] if str(src.get("type", "")).lower() == "sina"]
        if sina_symbols:
            self.sina.quotes(sina_symbols)

        items: dict = {}
        latest: dict = {}
        for key, raw_spec, variant_name, spec in resolved:
            series_key = prefix + (f"{key}_{variant_name}" if variant_name else key)
            unit = spec.get("unit", "index")
            daily = self.indices.try_series(series_key, spec.get("sources", []), fetchers) or []
            # Metrics (last price, daily change, day range) always come from the 1-day feed; the chart's bars
            # come from the requested intraday range so a 5D view still shows a proper daily change.
            live = self._live(series_key, spec, "1d", live_ttl)
            bars_live = live if range_key == "1d" else (self._live(series_key, spec, range_key, live_ttl) if intraday else None)
            entry = {"label": spec.get("label", key), "unit": unit, "range": range_key, "intraday": intraday}
            entry["variant"] = variant_name
            entry["variants"] = [{"name": n, "label": (v or {}).get("label", n)} for n, v in (raw_spec.get("variants") or {}).items()]
            entry["note"] = spec.get("note")
            entry["daily_source"] = self.indices.source_used.get(series_key.lower())
            entry["live_source"] = live.source if live else None
            entry["live_note"] = None if (live or not spec.get("live")) else "no live feed reachable"
            if not spec.get("live"):
                entry["live_note"] = "daily data only (no intraday source for this series)"

            if intraday and bars_live and bars_live.bars:
                series = [{"t": t, "v": v} for t, v in bars_live.bars]
            elif intraday:
                # No intraday feed: fall back to the last month of daily closes so the card is never empty.
                series = [{"t": d, "v": v} for d, v in daily[-DAILY_RANGES["1m"]:]]
                entry["intraday"] = False
                entry["fallback"] = "daily"
            else:
                points = list(daily[-DAILY_RANGES[range_key]:])
                if live and live.price is not None and live.as_of and (not points or live.as_of[:10] > points[-1][0]):
                    points.append((live.as_of[:10], live.price))
                series = [{"t": d, "v": v} for d, v in points]
            entry["series"] = series[-max(history_days, 2000):]
            entry["metrics"] = self._metrics(daily, live)
            if not daily and not (live and live.price is not None):
                entry["error"] = "unavailable"
            if entry["metrics"].get("value") is not None:
                latest[key] = entry["metrics"]["value"]
            items[key] = entry
        return {
            "section": section,
            "range": range_key,
            "ranges": RANGE_KEYS,
            "items": items,
            "curve": self._curve(latest) if section["curve"] else {},
            "generated_at": datetime.now(eastern()).isoformat(timespec="seconds"),
            "live_ttl_seconds": live_ttl or self.live_ttl,
            "refresh_seconds": section["refresh_seconds"],
        }

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
    def _live(self, key: str, spec: dict, range_key: str, ttl: Optional[float] = None) -> Optional[LiveData]:
        sources = spec.get("live") or []
        if not sources:
            return None
        cache_key = (key, range_key)
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
        for src in sources:
            kind = str(src.get("type", "")).lower()
            try:
                if kind == "yahoo":
                    result = self._yahoo_intraday(src["symbol"], range_key)
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

    def _yahoo_intraday(self, symbol: str, range_key: str) -> LiveData:
        cfg = INTRADAY_RANGES[range_key]
        url = YAHOO_CHART_URL.format(symbol=symbol, range=cfg["yahoo_range"], interval=cfg["yahoo_interval"])
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=BROWSER_HEADERS) as http:
            resp = http.get(url)
            resp.raise_for_status()
            payload = resp.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise ValueError(f"yahoo chart error: {(payload.get('chart') or {}).get('error')}")
        node = result[0]
        meta = node.get("meta") or {}
        tz = eastern()
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
        values = [v for _, v in bars]
        return LiveData(
            source=f"Yahoo {symbol}",
            price=float(price) if price is not None else None,
            prev_close=float(meta["chartPreviousClose"]) if meta.get("chartPreviousClose") is not None else None,
            as_of=as_of,
            bars=bars,
            day_high=max(values) if values and range_key == "1d" else None,
            day_low=min(values) if values and range_key == "1d" else None,
        )

    def _longbridge_intraday(self, symbol: str, range_key: str) -> LiveData:
        cfg = INTRADAY_RANGES[range_key]
        tz = eastern()
        raw = self.client.intraday_bars(symbol, cfg["lb_period"], cfg["lb_count"])
        by_session: Dict[str, List[Tuple[str, float]]] = {}
        for stamp, close in raw:
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
        )
