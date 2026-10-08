from __future__ import annotations

import argparse
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from backend.analytics.regime import SCENARIO_BY_KEY, SCENARIOS
from backend.config import settings
from backend.jobs import DailySnapshotScheduler, IvRankResolver, IvRefreshService, JobManager, MarketService, ScanService, ticker_of
from backend.logging_setup import setup_logging
from backend.market_data.fear_greed import FearGreedProvider
from backend.market_data.indices import IndexHistoryProvider
from backend.market_data.kalshi import RANGES as KALSHI_RANGES, KalshiService
from backend.market_data.longbridge import LongbridgeClient
from backend.market_data.macro import RANGE_KEYS, MacroService
from backend.storage.db import Database

setup_logging(settings)
log = logging.getLogger("backend.main")

cache_ttl = float(settings.get("data.index_cache_ttl_minutes", 30))
indices = IndexHistoryProvider(settings.data_dir, cache_ttl)
fear_greed = FearGreedProvider(settings.data_dir, cache_ttl)
client = LongbridgeClient(settings)
db = Database(settings.db_file)
macro = MacroService(settings, indices, client)
market = MarketService(settings, indices, fear_greed, live_vix=lambda: macro.live_value("vix", "VIX"))
kalshi = KalshiService(settings)
iv_ranks = IvRankResolver(settings, indices, db, client)
scanner = ScanService(settings, client, market, db, iv_ranks)
jobs = JobManager(scanner, IvRefreshService(settings, client, scanner, iv_ranks, db), db)
scheduler = DailySnapshotScheduler(settings, jobs, db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    builders = [market.payload] + [
        (lambda sid=section["id"]: macro.snapshot("1y", sid)) for section in macro.sections()
    ]
    if kalshi.enabled:
        builders.append(lambda: kalshi.snapshot("1m"))
    warmer = CacheWarmer(builders, cache_ttl)
    warmer.start()
    if settings.longbridge_credentials_present:
        scheduler.start()
    else:
        log.info("Daily IV snapshot scheduler not started: Longbridge credentials missing")
    yield
    warmer.stop()
    scheduler.stop()


class CacheWarmer:
    """Builds the market payloads at startup and shortly before each cache expiry so page loads stay fast."""

    def __init__(self, builders, ttl_minutes: float):
        self.builders = list(builders)
        self.interval = max(60.0, (ttl_minutes - 2) * 60)
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="cache-warmer", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while True:
            started = time.monotonic()
            for build in self.builders:
                try:
                    build()
                except Exception as exc:
                    log.warning("Cache warm-up failed: %s", exc)
            log.info("Market data cache warmed in %.1fs", time.monotonic() - started)
            if self._stop.wait(self.interval):
                return


app = FastAPI(title="Trade Plat", version="1.0", lifespan=lifespan)


class ScreenerFilters(BaseModel):
    dte_min: int = Field(..., ge=1, le=730)
    dte_max: int = Field(..., ge=1, le=730)
    delta_min: float = Field(..., ge=0.0, le=1.0)
    delta_max: float = Field(..., ge=0.0, le=1.0)
    min_open_interest: int = Field(0, ge=0)
    max_spread_pct: float = Field(100.0, ge=0.0, le=100.0)
    min_annualized_yield_pct: Optional[float] = Field(None, ge=0.0)
    target_delta: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_results: int = Field(12, ge=1, le=50)

    @model_validator(mode="after")
    def check_ranges(self):
        if self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        if self.delta_min > self.delta_max:
            raise ValueError("delta_min must be <= delta_max")
        return self

    def to_config(self) -> dict:
        cfg = {
            "dte": [self.dte_min, self.dte_max],
            "delta": [self.delta_min, self.delta_max],
            "min_open_interest": self.min_open_interest,
            "max_spread_pct": self.max_spread_pct,
            "max_results": self.max_results,
        }
        if self.min_annualized_yield_pct is not None:
            cfg["min_annualized_yield_pct"] = self.min_annualized_yield_pct
        if self.target_delta is not None:
            cfg["target_delta"] = self.target_delta
        return cfg


class ScanRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)
    scenario: Optional[str] = None
    filters: Optional[ScreenerFilters] = None


class WatchlistRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)


def normalize_symbols(raw: List[str]) -> List[str]:
    out: List[str] = []
    for item in raw:
        for part in item.replace(";", ",").split(","):
            sym = part.strip().upper()
            if not sym:
                continue
            if "." not in sym:
                sym = f"{sym}.US"
            if sym not in out:
                out.append(sym)
    return out


# Tickers for the user-editable chart sections also name cache files, so keep them to plain symbol characters.
CHART_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
MAX_CHART_SYMBOLS = int(settings.get("macro.max_symbols_per_section", 12))


@app.get("/")
def index():
    return FileResponse(settings.frontend_dir / "index.html")


@app.get("/api/market")
def api_market():
    try:
        return market.payload()
    except Exception as exc:
        log.exception("market payload failed")
        raise HTTPException(status_code=503, detail=f"index data unavailable: {exc}")


@app.get("/api/macro")
def api_macro(range: str = "1d", section: str = "macro", variants: Optional[str] = None, symbols: Optional[str] = None):
    if range.lower() not in RANGE_KEYS:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(RANGE_KEYS)}")
    section_cfg = next((s for s in macro.sections() if s["id"] == section), None)
    if section_cfg is None:
        raise HTTPException(status_code=404, detail=f"unknown live section {section!r}")
    chosen = {}
    for part in (variants or "").split(","):
        if ":" in part:
            key, name = part.split(":", 1)
            if key.strip() and name.strip():
                chosen[key.strip().upper()] = name.strip().lower()
    chosen_symbols = None
    if symbols is not None:
        if section_cfg["symbols"] is None:
            raise HTTPException(status_code=400, detail=f"live section {section!r} does not take custom symbols")
        chosen_symbols = normalize_symbols(symbols.split(","))
        if len(chosen_symbols) > MAX_CHART_SYMBOLS:
            raise HTTPException(status_code=400, detail=f"at most {MAX_CHART_SYMBOLS} symbols per section")
        bad = [s for s in chosen_symbols if not CHART_SYMBOL_RE.match(s)]
        if bad:
            raise HTTPException(status_code=400, detail=f"invalid symbols: {', '.join(bad)}")
    try:
        return macro.snapshot(range, section, chosen, chosen_symbols)
    except Exception as exc:
        log.exception("macro snapshot failed")
        raise HTTPException(status_code=503, detail=f"macro data unavailable: {exc}")


@app.get("/api/kalshi")
def api_kalshi(range: str = "1m"):
    if not kalshi.enabled:
        raise HTTPException(status_code=404, detail="Kalshi section disabled in config.yaml")
    if range not in KALSHI_RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(KALSHI_RANGES)}")
    try:
        return kalshi.snapshot(range)
    except Exception as exc:
        log.exception("kalshi snapshot failed")
        raise HTTPException(status_code=503, detail=f"Kalshi data unavailable: {exc}")


@app.get("/api/status")
def api_status():
    from datetime import datetime

    return {
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "job": jobs.current(),
        "quota": client.status(),
        "credentials_present": settings.longbridge_credentials_present,
        "sources": dict(indices.freshness(), fear_greed=fear_greed.freshness()),
    }


@app.post("/api/scan", status_code=202)
def api_scan(req: ScanRequest):
    symbols = normalize_symbols(req.symbols or settings.default_watchlist)
    if not symbols:
        raise HTTPException(status_code=400, detail="no symbols given")
    if len(symbols) > 10:
        raise HTTPException(status_code=400, detail="at most 10 symbols per scan")
    if req.scenario and req.scenario not in SCENARIO_BY_KEY:
        raise HTTPException(status_code=400, detail=f"unknown scenario {req.scenario!r}")
    if not settings.longbridge_credentials_present:
        raise HTTPException(status_code=400, detail="Longbridge credentials missing: set LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN in .env")
    try:
        job = jobs.start_scan(symbols, req.scenario or None, req.filters.to_config() if req.filters else None)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"job": job.snapshot()}


@app.post("/api/scan/cancel")
def api_scan_cancel():
    return {"cancelled": jobs.cancel()}


@app.get("/api/recommendations")
def api_recommendations():
    result = jobs.last_result()
    if result is None:
        return JSONResponse(status_code=404, content={"detail": "no scan has completed yet"})
    return result


@app.get("/api/watchlist")
def api_watchlist(symbols: Optional[str] = None):
    wanted = normalize_symbols(symbols.split(",") if symbols else settings.default_watchlist)
    stored = db.watchlist_iv(wanted)
    items = []
    for sym in wanted:
        entry = stored.get(sym)
        if entry is None:
            items.append({"underlying": sym, "ticker": ticker_of(sym), "missing": True,
                          "snapshots": len(db.atm_iv_history(sym, 252))})
        else:
            items.append(entry)
    return {
        "symbols": wanted,
        "items": items,
        "scheduler": {
            "enabled": scheduler.enabled,
            "daily_at_et": settings.get("iv_snapshot.daily_at_et", "16:20"),
            "last_run_date": scheduler.last_run_date,
        },
        "last_refresh": jobs.last_iv_result(),
    }


@app.post("/api/watchlist/refresh", status_code=202)
def api_watchlist_refresh(req: WatchlistRequest):
    symbols = normalize_symbols(req.symbols or settings.default_watchlist)
    if not symbols:
        raise HTTPException(status_code=400, detail="no symbols given")
    if len(symbols) > 15:
        raise HTTPException(status_code=400, detail="at most 15 symbols per refresh")
    if not settings.longbridge_credentials_present:
        raise HTTPException(status_code=400, detail="Longbridge credentials missing: set LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN in .env")
    try:
        job = jobs.start_iv_refresh(symbols)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"job": job.snapshot()}


@app.get("/api/config")
def api_config():
    return {
        "watchlist": settings.default_watchlist,
        "thresholds": settings.section("thresholds"),
        "analytics": settings.section("analytics"),
        "live_sections": macro.sections(),
        "live_default_range": settings.get("macro.default_range", "1d"),
        "live_max_symbols": MAX_CHART_SYMBOLS,
        "kalshi": dict(kalshi.config(), enabled=kalshi.enabled) if kalshi.enabled else {"enabled": False},
        "scenarios": [
            {
                "key": s.key,
                "title": s.title,
                "stance": s.stance,
                "action": s.action,
                "description": s.description,
                "target": s.target,
                "filters": settings.section(f"scenarios.{s.key}"),
            }
            for s in SCENARIOS
        ],
    }


app.mount("/static", StaticFiles(directory=str(settings.frontend_dir)), name="static")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Trade Plat server")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = parser.parse_args()
    log.info("Starting server on %s:%s", settings.host, settings.port)
    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=args.reload, log_config=None)


if __name__ == "__main__":
    main()
