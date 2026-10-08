from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
# The endpoint rejects non-browser clients with HTTP 418, so present as a browser.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://www.cnn.com/markets/fear-and-greed",
    "Origin": "https://www.cnn.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

COMPONENTS = [
    ("market_momentum_sp500", "Market momentum (S&P 500 vs 125d MA)"),
    ("stock_price_strength", "Stock price strength (52w highs vs lows)"),
    ("stock_price_breadth", "Stock price breadth (McClellan volume)"),
    ("put_call_options", "Put/call ratio (5d)"),
    ("market_volatility_vix", "Market volatility (VIX vs 50d MA)"),
    ("junk_bond_demand", "Junk bond demand (yield spread)"),
    ("safe_haven_demand", "Safe haven demand (stocks vs bonds 20d)"),
]

RATING_BANDS = [(25, "extreme fear"), (45, "fear"), (55, "neutral"), (75, "greed")]


def rating_for(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    for cut, label in RATING_BANDS:
        if score < cut:
            return label
    return "extreme greed"


def _ms_to_date(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date().isoformat()


def parse_graphdata(raw: dict) -> dict:
    fg = raw.get("fear_and_greed") or {}
    history = []
    seen = set()
    for point in (raw.get("fear_and_greed_historical") or {}).get("data", []):
        try:
            day = _ms_to_date(float(point["x"]))
            value = float(point["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if day in seen:
            continue
        seen.add(day)
        history.append({"d": day, "v": round(value, 2), "rating": point.get("rating") or rating_for(value)})
    history.sort(key=lambda p: p["d"])
    components = []
    for key, label in COMPONENTS:
        comp = raw.get(key) or {}
        if comp.get("score") is None:
            continue
        components.append({"key": key, "label": label, "score": round(float(comp["score"]), 1), "rating": comp.get("rating")})
    score = fg.get("score")
    return {
        "score": round(float(score), 1) if score is not None else None,
        "rating": fg.get("rating") or rating_for(score),
        "timestamp": fg.get("timestamp"),
        "previous_close": fg.get("previous_close"),
        "previous_1_week": fg.get("previous_1_week"),
        "previous_1_month": fg.get("previous_1_month"),
        "previous_1_year": fg.get("previous_1_year"),
        "history": history,
        "components": components,
    }


class FearGreedProvider:
    """CNN Fear & Greed index (composite score, 1y history, components) with disk + memory caching."""

    def __init__(self, cache_dir: Path, ttl_minutes: float = 30, timeout: float = 25.0):
        self.cache_file = cache_dir / "fear_greed.json"
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_minutes * 60
        self.timeout = timeout
        self._lock = threading.Lock()
        self._memory: Optional[dict] = None
        self._fetched_at = 0.0
        self.last_error: Optional[str] = None
        self.source: Optional[str] = None

    def get(self) -> Optional[dict]:
        with self._lock:
            if self._memory and time.time() - self._fetched_at < self.ttl:
                return self._memory
            cached = self._read_cache()
            if cached and time.time() - self.cache_file.stat().st_mtime < self.ttl:
                self._remember(cached, "disk-cache", self.cache_file.stat().st_mtime)
                return cached
            try:
                data = self._download()
                self.cache_file.write_text(json.dumps(data), encoding="utf-8")
                self._remember(data, "CNN", time.time())
                self.last_error = None
                log.info("Loaded CNN Fear & Greed: %s (%s), %d history points", data["score"], data["rating"], len(data["history"]))
                return data
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Fear & Greed fetch failed: %s", exc)
            if cached:
                self._remember(cached, "stale-disk-cache", time.time())
                return cached
            return self._memory

    def freshness(self) -> dict:
        return {
            "fetched_at": datetime.fromtimestamp(self._fetched_at).isoformat(timespec="seconds") if self._fetched_at else None,
            "source": self.source,
            "last_error": self.last_error,
        }

    def _remember(self, data: dict, source: str, fetched_at: float) -> None:
        self._memory = data
        self.source = source
        self._fetched_at = fetched_at

    def _download(self) -> dict:
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=HEADERS) as client:
            resp = client.get(CNN_URL)
            resp.raise_for_status()
            data = parse_graphdata(resp.json())
        if data["score"] is None:
            raise ValueError("response had no fear_and_greed score")
        return data

    def _read_cache(self) -> Optional[dict]:
        if not self.cache_file.exists():
            return None
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not read %s: %s", self.cache_file, exc)
            return None
