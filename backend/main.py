from __future__ import annotations

import argparse
import logging
import math
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, List, Literal, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from backend.analytics.regime import SCENARIO_BY_KEY, SCENARIOS
from backend.config import PROJECT_ROOT, load_settings, settings
from backend.jobs import DailySnapshotScheduler, IvRankResolver, IvRefreshService, JobManager, MarketService, ScanService, ticker_of
from backend.logging_setup import setup_logging
from backend.market_data.china_etfs import ChinaEtfService
from backend.market_data.china_futures import ChinaFuturesService, RANGES as CHINA_FUTURES_RANGES
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
china_etfs = ChinaEtfService()
china_futures = ChinaFuturesService(ttl_minutes=cache_ttl)
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
        builders.extend((lambda chart_id=cfg["id"]: kalshi.snapshot("1m", chart_id)) for cfg in kalshi.configs())
    warmer = CacheWarmer(builders, cache_ttl)
    warmer.start()
    if settings.longbridge_credentials_present:
        scheduler.start()
    else:
        log.info("Daily IV snapshot scheduler not started: Longbridge credentials missing")
    try:
        yield
    finally:
        warmer.stop()
        scheduler.stop()
        await china_etfs.close()


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


@app.exception_handler(RequestValidationError)
async def request_validation_error(_request, exc: RequestValidationError):
    # Rejected non-finite inputs must not break JSON serialization of the validation error itself.
    errors = jsonable_encoder(exc.errors(), custom_encoder={float: lambda value: value if math.isfinite(value) else str(value)})
    return JSONResponse(status_code=422, content={"detail": errors})


class ScreenerFilters(BaseModel):
    dte_min: int = Field(..., ge=1, le=730)
    dte_max: int = Field(..., ge=1, le=730)
    delta_min: float = Field(..., ge=0.0, le=1.0)
    delta_max: float = Field(..., ge=0.0, le=1.0)
    min_open_interest: int = Field(0, ge=0)
    max_spread_pct: float = Field(100.0, ge=0.0, le=100.0)
    min_annualized_yield_pct: Optional[float] = Field(None, ge=0.0)
    target_delta: Optional[float] = Field(None, ge=0.0, le=1.0)
    target_strike: Optional[float] = Field(None, gt=0.0, allow_inf_nan=False)
    strike_mode: Literal["exact", "lt", "gt"] = "exact"
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
        if self.target_strike is not None:
            cfg["target_strike"] = self.target_strike
            cfg["strike_mode"] = self.strike_mode
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
    if range.lower() not in section_cfg["ranges"]:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(section_cfg['ranges'])} for section {section!r}")
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
def api_kalshi(range: str = "1m", chart: Optional[str] = None):
    if not kalshi.enabled:
        raise HTTPException(status_code=404, detail="Kalshi section disabled in config.yaml")
    if range not in KALSHI_RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(KALSHI_RANGES)}")
    try:
        return kalshi.snapshot(range, chart)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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
        raise HTTPException(status_code=400, detail="Longbridge credentials missing: export LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN in ~/.bashrc; environment/.env is used when no profile exports exist. Restart after updating.")
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


@app.get("/api/china-etfs")
async def api_china_etfs():
    return await china_etfs.snapshot()


@app.get("/api/china-futures")
def api_china_futures(range: str = "1y"):
    if range.lower() not in CHINA_FUTURES_RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(CHINA_FUTURES_RANGES)}")
    try:
        return china_futures.snapshot(range.lower())
    except Exception as exc:
        log.exception("china futures snapshot failed")
        raise HTTPException(status_code=503, detail=f"China futures data unavailable: {exc}")


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
        raise HTTPException(status_code=400, detail="Longbridge credentials missing: export LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN in ~/.bashrc; environment/.env is used when no profile exports exist. Restart after updating.")
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
        "kalshi_charts": [dict(cfg, enabled=kalshi.enabled) for cfg in kalshi.configs()] if kalshi.enabled else [],
        "scenarios": [
            {
                "key": s.key,
                "title": s.title,
                "stance": s.stance,
                "action": s.action,
                "description": s.description,
                "target": s.target,
                "option_side": settings.get(f"scenarios.{s.key}.option_side", "put"),
                "filters": settings.section(f"scenarios.{s.key}"),
            }
            for s in SCENARIOS
        ],
    }


@app.get("/api/config/raw")
def api_config_raw():
    return settings._data


ALLOWED_CONFIG_SECTIONS = {
    "server", "logging", "data", "longbridge", "watchlist", "indices",
    "thresholds", "analytics", "macro", "kalshi", "scenarios",
    "iv_backfill", "iv_snapshot", "live_sections",
}
RESTART_PATHS = {"server.host", "server.port", "logging.level", "logging.file",
                 "logging.max_bytes", "logging.backup_count"}


class ConfigUpdate(BaseModel):
    path: str = Field(..., min_length=1, max_length=200)
    value: Any = None


class ConfigBatchUpdate(BaseModel):
    updates: List[ConfigUpdate] = Field(default_factory=list)


@app.post("/api/config")
def api_config_update(batch: ConfigBatchUpdate):
    cfg_path = PROJECT_ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    requires_restart = []
    for update in batch.updates:
        keys = update.path.split(".")
        if not keys or keys[0] not in ALLOWED_CONFIG_SECTIONS:
            raise HTTPException(status_code=400,
                                detail=f"config path {update.path!r} is not in an allowed section")
        if update.path in RESTART_PATHS:
            requires_restart.append(update.path)
        node = data
        for key in keys[:-1]:
            if key not in node or not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        node[keys[-1]] = update.value
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
    settings._data = data
    return {
        "status": "saved",
        "requires_restart": requires_restart,
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
