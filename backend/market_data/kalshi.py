"""Kalshi prediction markets (public, keyless read API) for the next Fed rate decision.

Series KXFEDDECISION has one event per FOMC meeting with mutually exclusive outcome markets
(Cut 25bps, Fed maintains rate, Hike 25bps, ...). Prices are in dollars per $1 contract, i.e. probabilities.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

from backend.config import Settings

log = logging.getLogger(__name__)

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
HEADERS = {"Accept": "application/json", "User-Agent": "trade-plat/1.0"}

# range key -> (candle period in minutes, lookback seconds)
RANGES = {
    "1d": (1, 24 * 3600),
    "5d": (60, 5 * 24 * 3600),
    "1m": (60, 30 * 24 * 3600),
    "3m": (1440, 90 * 24 * 3600),
    "all": (1440, 400 * 24 * 3600),
}


def _f(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class KalshiClient:
    def __init__(self, timeout: float = 20.0, markets_ttl: float = 25.0, candles_ttl: float = 240.0):
        self.timeout = timeout
        self.markets_ttl = markets_ttl
        self.candles_ttl = candles_ttl
        self._cache: Dict[str, Tuple[float, object]] = {}
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    def _get(self, path: str, params: Optional[dict] = None, ttl: float = 0.0):
        key = f"{path}?{sorted((params or {}).items())}"
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.time() - cached[0] < ttl:
                return cached[1]
        with httpx.Client(timeout=self.timeout, headers=HEADERS) as client:
            resp = client.get(f"{BASE_URL}/{path}", params=params or {})
            resp.raise_for_status()
            payload = resp.json()
        with self._lock:
            self._cache[key] = (time.time(), payload)
        return payload

    def open_events(self, series_ticker: str) -> List[dict]:
        payload = self._get("events", {"series_ticker": series_ticker, "status": "open", "limit": 50}, ttl=self.markets_ttl * 4)
        events = list(payload.get("events") or [])
        events.sort(key=lambda e: e.get("strike_date") or "")
        return events

    def markets(self, event_ticker: str) -> List[dict]:
        payload = self._get("markets", {"event_ticker": event_ticker, "limit": 100}, ttl=self.markets_ttl)
        return list(payload.get("markets") or [])

    def candlesticks(self, series_ticker: str, market_ticker: str, period_minutes: int, lookback_seconds: int) -> List[Tuple[str, float]]:
        now = int(time.time())
        params = {"start_ts": now - int(lookback_seconds), "end_ts": now, "period_interval": int(period_minutes)}
        # Round the window to the period so consecutive polls share the cache entry.
        bucket = max(60, int(period_minutes) * 60)
        params["end_ts"] = now - (now % bucket) + bucket
        params["start_ts"] = params["end_ts"] - int(lookback_seconds)
        payload = self._get(f"series/{series_ticker}/markets/{market_ticker}/candlesticks", params, ttl=self.candles_ttl)
        out: List[Tuple[str, float]] = []
        for candle in payload.get("candlesticks") or []:
            price = (candle.get("price") or {})
            close = _f(price.get("close_dollars"))
            if close is None:
                close = _f(price.get("mean_dollars"))
            stamp = candle.get("end_period_ts")
            if close is None or stamp is None:
                continue
            out.append((datetime.fromtimestamp(int(stamp), tz=timezone.utc).isoformat(timespec="minutes"), close * 100.0))
        return out


class KalshiService:
    def __init__(self, settings: Settings, client: Optional[KalshiClient] = None):
        self.settings = settings
        self.client = client or KalshiClient()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("kalshi.enabled", True))

    def configs(self) -> List[dict]:
        root = self.settings.section("kalshi")
        charts = root.get("charts")
        if not isinstance(charts, list):
            charts = [dict(root, id="next")]
        out = []
        for chart in charts:
            if not chart.get("id"):
                continue
            out.append({
                "id": str(chart["id"]),
                "series_ticker": str(chart.get("series_ticker", root.get("series_ticker", "KXFEDDECISION"))),
                "event": str(chart.get("event", root.get("event", "next"))),
                "markets": [str(m).upper() for m in (chart.get("markets", root.get("markets")) or [])],
                "refresh_seconds": float(chart.get("refresh_seconds", root.get("refresh_seconds", 30))),
                "title": str(chart.get("title", root.get("title", "Fed decision (Kalshi)"))),
            })
        return out

    def config(self, chart_id: Optional[str] = None) -> dict:
        wanted = chart_id or "next"
        configs = self.configs()
        config = next((cfg for cfg in configs if cfg["id"] == wanted), None)
        if config is None and chart_id is None and configs:
            config = configs[0]
        if config is None:
            raise KeyError(f"unknown Kalshi chart {wanted!r}")
        return config

    def snapshot(self, range_key: str = "1m", chart_id: Optional[str] = None) -> dict:
        cfg = self.config(chart_id)
        range_key = range_key if range_key in RANGES else "1m"
        period, lookback = RANGES[range_key]
        series_ticker = cfg["series_ticker"]
        event = self._select_event(series_ticker, cfg["event"])
        if event is None:
            return {"config": cfg, "event": None, "markets": [], "range": range_key, "ranges": list(RANGES),
                    "refresh_seconds": cfg["refresh_seconds"], "error": f"no open event found for {series_ticker}"}
        markets = self.client.markets(event["event_ticker"])
        wanted = cfg["markets"]
        rows: List[dict] = []
        for market in markets:
            ticker = str(market.get("ticker", ""))
            suffix = ticker.split("-")[-1].upper()
            label = market.get("yes_sub_title") or market.get("subtitle") or suffix
            if wanted and not any(w == suffix or w in label.upper() for w in wanted):
                continue
            history: List[Tuple[str, float]] = []
            try:
                history = self.client.candlesticks(series_ticker, ticker, period, lookback)
            except Exception as exc:
                log.warning("Kalshi candlesticks failed for %s: %s", ticker, exc)
            last = _f(market.get("last_price_dollars"))
            rows.append({
                "ticker": ticker,
                "code": suffix,
                "label": label,
                "last_pct": last * 100.0 if last is not None else None,
                "yes_bid_pct": (_f(market.get("yes_bid_dollars")) or 0) * 100.0,
                "yes_ask_pct": (_f(market.get("yes_ask_dollars")) or 0) * 100.0,
                "previous_pct": (_f(market.get("previous_price_dollars")) or 0) * 100.0 if market.get("previous_price_dollars") else None,
                "volume": _f(market.get("volume_fp")),
                "volume_24h": _f(market.get("volume_24h_fp")),
                "open_interest": _f(market.get("open_interest_fp")),
                "status": market.get("status"),
                "series": [{"t": t, "v": v} for t, v in history],
            })
        rows.sort(key=lambda r: -(r["last_pct"] or 0))
        return {
            "config": cfg,
            "event": {
                "ticker": event.get("event_ticker"),
                "title": event.get("title"),
                "sub_title": event.get("sub_title"),
                "strike_date": event.get("strike_date"),
                "mutually_exclusive": event.get("mutually_exclusive"),
            },
            "markets": rows,
            "range": range_key,
            "ranges": list(RANGES),
            "refresh_seconds": cfg["refresh_seconds"],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _select_event(self, series_ticker: str, which: str) -> Optional[dict]:
        events = self.client.open_events(series_ticker)
        if not events:
            return None
        now = datetime.now(timezone.utc)
        if which and which.lower() == "december":
            december = []
            for event in events:
                try:
                    strike = datetime.fromisoformat(str(event.get("strike_date", "")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if strike >= now and strike.month == 12:
                    december.append((strike, event))
            return min(december, key=lambda item: item[0])[1] if december else None
        if which and which.lower() != "next":
            for event in events:
                if str(event.get("event_ticker", "")).upper() == which.upper():
                    return event
            log.warning("Kalshi event %s not open; falling back to the next one", which)
        upcoming = [e for e in events if (e.get("strike_date") or "") >= now.isoformat()]
        return (upcoming or events)[0]
