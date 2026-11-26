"""Black-Scholes carry inputs: a tenor-dependent risk-free rate and a per-underlying dividend yield."""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import httpx

from backend.config import Settings

log = logging.getLogger(__name__)

YAHOO_DIVIDEND_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d&events=div"
MAX_PLAUSIBLE_YIELD = 0.15
DIVIDEND_TTL = 24 * 3600
DIVIDEND_RETRY_TTL = 3600


def fetch_trailing_dividend_yield(ticker: str, timeout: float = 15.0) -> float:
    """Dividends paid over the last year divided by the latest price."""
    from backend.market_data.indices import BROWSER_HEADERS

    with httpx.Client(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS) as http:
        resp = http.get(YAHOO_DIVIDEND_URL.format(symbol=ticker))
        resp.raise_for_status()
        node = resp.json()["chart"]["result"][0]
    price = float(node["meta"]["regularMarketPrice"])
    cutoff = time.time() - 365 * 86400
    events = (node.get("events") or {}).get("dividends") or {}
    paid = sum(float(e["amount"]) for e in events.values() if float(e["date"]) >= cutoff)
    value = paid / price
    if not math.isfinite(value) or not 0 <= value <= MAX_PLAUSIBLE_YIELD:
        raise ValueError(f"implausible dividend yield {value!r}")
    return value


class CarryModel:
    def __init__(self, settings: Settings, indices=None,
                 dividend_fetcher: Optional[Callable[[str], float]] = None):
        self.settings = settings
        self.indices = indices
        self.dividend_fetcher = dividend_fetcher
        self._lock = threading.Lock()
        self._dividends: Dict[str, Tuple[float, Optional[float]]] = {}

    def _analytics(self) -> dict:
        return self.settings.section("analytics")

    def curve(self) -> List[Tuple[int, float]]:
        """(DTE, continuously compounded rate) points from the configured Treasury tenors that are available."""
        if self.indices is None:
            return []
        points: List[Tuple[int, float]] = []
        for node in self._analytics().get("rate_curve") or []:
            dte = int(node["dte"])
            series = self.indices.try_series(f"rate_curve_{dte}", node.get("sources") or [])
            if series:
                points.append((dte, math.log1p(series[-1][1] / 100.0)))
        return sorted(points)

    def rate(self, dte: int) -> float:
        points = self.curve()
        if not points:
            return float(self._analytics().get("risk_free_rate", 0.04))
        if dte <= points[0][0]:
            return points[0][1]
        for (d0, r0), (d1, r1) in zip(points, points[1:]):
            if dte <= d1:
                return r0 + (r1 - r0) * (dte - d0) / (d1 - d0)
        return points[-1][1]

    def dividend(self, symbol: str) -> Tuple[float, str]:
        """(yield, source) where source is config, trailing_12m or default."""
        analytics = self._analytics()
        ticker = symbol.upper().split(".")[0]
        overrides = {str(k).upper(): v for k, v in (analytics.get("dividend_yields") or {}).items()}
        if ticker in overrides:
            return float(overrides[ticker]), "config"
        default = float(analytics.get("dividend_yield", 0.0))
        if self.dividend_fetcher is None:
            return default, "default"
        with self._lock:
            cached = self._dividends.get(ticker)
            if cached is None or time.time() >= cached[0]:
                try:
                    cached = (time.time() + DIVIDEND_TTL, self.dividend_fetcher(ticker))
                except Exception as exc:
                    log.warning("Dividend yield for %s unavailable, using the default %.3f: %s", ticker, default, exc)
                    cached = (time.time() + DIVIDEND_RETRY_TTL, None)
                self._dividends[ticker] = cached
        return (cached[1], "trailing_12m") if cached[1] is not None else (default, "default")

    def dividend_yield(self, symbol: str) -> float:
        return self.dividend(symbol)[0]

    def describe(self, symbol: str, dte: int) -> dict:
        q, source = self.dividend(symbol)
        return {"dividend_yield": round(q, 5), "dividend_source": source,
                "risk_free_rate": round(self.rate(dte), 5), "rate_dte": dte,
                "rate_source": "treasury_curve" if self.curve() else "config"}
