from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from backend.analytics.indicators import classify_level, iv_rank, percentile_rank
from backend.analytics.ivhistory import backfill_expiry_candidates, build_proxy_iv_history, strikes_covering
from backend.analytics.regime import IVR_LEVELS, SCENARIO_BY_KEY, classify_regime, compute_market_metrics, summarize_series
from backend.analytics.screener import (
    apply_depth,
    atm_iv_by_expiry,
    build_candidates,
    filter_candidates,
    reference_metrics,
    score_candidates,
    strike_band_for_deltas,
    subsample_strikes,
)
from backend.config import Settings
from backend.market_data.fear_greed import FearGreedProvider
from backend.market_data.indices import INDEX_LABELS, IndexHistoryProvider
from backend.market_data.longbridge import AuthError, LongbridgeClient, LongbridgeError, ScanCancelled
from backend.storage.db import Database

log = logging.getLogger(__name__)

LEAPS_SOURCE = "leaps"


def market_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()


def market_today() -> date:
    return market_now().date()


def ticker_of(symbol: str) -> str:
    return symbol[:-3] if symbol.upper().endswith(".US") else symbol


def with_market(symbol: str) -> str:
    return symbol if "." in symbol else f"{symbol}.US"


class Job:
    def __init__(self, kind: str, symbols: List[str], scenario_override: Optional[str] = None, filters: Optional[dict] = None):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.symbols = symbols
        self.scenario_override = scenario_override
        self.filters = filters
        self.state = "queued"
        self.phase = "Queued"
        self.message = ""
        self.progress = 0.0
        self.current_symbol: Optional[str] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()

    def update(self, phase: Optional[str] = None, message: Optional[str] = None, progress: Optional[float] = None,
               symbol: Optional[str] = None) -> None:
        with self._lock:
            if phase is not None:
                self.phase = phase
            if message is not None:
                self.message = message
            if progress is not None:
                self.progress = max(0.0, min(1.0, progress))
            if symbol is not None:
                self.current_symbol = symbol
        if phase or message:
            log.info("[job %s] %s%s", self.id, phase or "", f" - {message}" if message else "")

    def finish(self, state: str, error: Optional[str] = None) -> None:
        with self._lock:
            self.state = state
            self.error = error
            self.finished_at = time.time()
            self.progress = 1.0 if state == "done" else self.progress
            label = "Scan" if self.kind == "scan" else "IV refresh"
            self.phase = {"done": f"{label} complete", "error": f"{label} failed", "cancelled": f"{label} cancelled"}.get(state, state)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "symbols": self.symbols,
                "scenario_override": self.scenario_override,
                "custom_filters": self.filters is not None,
                "state": self.state,
                "phase": self.phase,
                "message": self.message,
                "progress": round(self.progress, 3),
                "current_symbol": self.current_symbol,
                "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
                "finished_at": datetime.fromtimestamp(self.finished_at).isoformat(timespec="seconds") if self.finished_at else None,
                "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at, 1),
                "error": self.error,
            }


class MarketService:
    def __init__(self, settings: Settings, indices: IndexHistoryProvider, fear_greed: Optional[FearGreedProvider] = None,
                 live_vix: Optional[Callable[[], Optional[Tuple[str, float]]]] = None):
        self.settings = settings
        self.indices = indices
        self.fear_greed = fear_greed
        self.live_vix = live_vix

    def vix_series(self) -> List[Tuple[str, float]]:
        """CBOE daily closes with today's live VIX (Longbridge) appended when the close is not published yet."""
        vix = list(self.indices.vix())
        if self.live_vix is not None:
            try:
                live = self.live_vix()
            except Exception as exc:
                log.warning("live VIX unavailable: %s", exc)
                live = None
            if live and vix and live[0][:10] > vix[-1][0]:
                vix.append((live[0][:10], live[1]))
        return vix

    def metrics_and_regime(self):
        vix = self.vix_series()
        skew = self.indices.skew()
        metrics = compute_market_metrics(vix, skew, self.settings.section("thresholds"))
        regime = classify_regime(metrics)
        return vix, skew, metrics, regime

    def payload(self) -> dict:
        history_days = int(self.settings.get("data.history_days", 1260))
        vix, skew, metrics, regime = self.metrics_and_regime()
        sources = self.indices.freshness()
        fear_greed = None
        if self.fear_greed is not None:
            fear_greed = self.fear_greed.get()
            sources["fear_greed"] = self.fear_greed.freshness()
        extra = {}
        latest: dict = {"VIX": metrics.vix.value}
        for item in self.settings.get("indices.charts", ["VIX9D", "VIX3M", "VVIX", "DSPX", "COR3M"]):
            # Plain strings are CBOE index names; dicts declare a key, label and source list (e.g. Yahoo ^MOVE).
            if isinstance(item, dict):
                name = str(item.get("key", "")).upper()
                label = item.get("label", INDEX_LABELS.get(name, name))
                series = self.indices.try_series(name, item.get("sources", [])) if name else None
            else:
                name = str(item).upper()
                label = INDEX_LABELS.get(name, name)
                series = self.indices.try_index(name)
            if not name:
                continue
            if not series:
                extra[name] = {"label": label, "error": "unavailable", "series": [], "metrics": {}}
                continue
            summary = summarize_series(series)
            latest[name] = summary.value
            extra[name] = {
                "label": label,
                "series": [{"d": d, "v": v} for d, v in series[-history_days:]],
                "metrics": summary.__dict__,
                "source": self.indices.source_used.get(name.lower()),
            }
        return {
            "vix": {"series": [{"d": d, "v": v} for d, v in vix[-history_days:]], "metrics": metrics.vix.__dict__},
            "skew": {"series": [{"d": d, "v": v} for d, v in skew[-history_days:]], "metrics": metrics.skew.__dict__},
            "fear_greed": fear_greed,
            "indices": extra,
            "term_structure": self._term_structure(latest),
            "iv_rank_level": metrics.iv_rank_level,
            "iv_rank_change_5d": metrics.iv_rank_change_5d,
            "panic": metrics.panic,
            "regime": regime.to_dict(),
            "thresholds": self.settings.section("thresholds"),
            "sources": sources,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    def _term_structure(latest: dict) -> dict:
        def ratio(a: str, b: str) -> Optional[float]:
            x, y = latest.get(a), latest.get(b)
            return (x / y) if x and y else None

        out = {}
        for key, a, b in (("vix9d_vix", "VIX9D", "VIX"), ("vix_vix3m", "VIX", "VIX3M")):
            value = ratio(a, b)
            if value is not None:
                out[key] = {"value": value, "numerator": a, "denominator": b,
                            "state": "backwardation" if value > 1.0 else "contango"}
        return out


class IvRankResolver:
    """Best available IV rank for one underlying.

    Sources, in order of preference:
      1. a CBOE volatility index that tracks the ETF (SPY -> VIX, QQQ -> VXN, ...): a true 30-day IV series;
      2. our own daily ATM-IV snapshots once enough have accumulated;
      3. an ATM-IV series rebuilt from a long-dated expiry's daily option closes (proxy);
      4. a partial snapshot history, flagged as such.
    """

    def __init__(self, settings: Settings, indices: IndexHistoryProvider, db: Database, client: Optional[LongbridgeClient]):
        self.settings = settings
        self.indices = indices
        self.db = db
        self.client = client
        analytics = settings.section("analytics")
        self.lookback = int(settings.get("thresholds.iv_rank.lookback_days", 252))
        self.min_partial = int(analytics.get("min_snapshots_for_iv_rank", 20))
        self.min_full = int(analytics.get("min_snapshots_for_full_iv_rank", 120))
        self.min_full_proxy = int(settings.get("iv_backfill.min_points", 60))
        self.cuts = [settings.get("thresholds.iv_rank.low", 25), settings.get("thresholds.iv_rank.medium", 50), settings.get("thresholds.iv_rank.high", 75)]

    def resolve(self, symbol: str, today: date, expiries: Optional[List[date]] = None, allow_backfill: bool = False,
                cancel: Optional[threading.Event] = None, progress: Optional[Callable[[str], None]] = None) -> dict:
        proxy = self._from_cboe(symbol)
        if proxy:
            return proxy
        snapshots = self.db.atm_iv_history(symbol, self.lookback)
        if len(snapshots) >= self.min_full:
            return self._describe(snapshots, "snapshots", f"ATM IV snapshots ({len(snapshots)} days)")
        leaps = self.db.iv_history(symbol, LEAPS_SOURCE, self.lookback)
        stale = not leaps or leaps[-1][0] < self._last_session(today).isoformat()
        if stale and allow_backfill and self.client is not None:
            try:
                rebuilt = self.backfill(symbol, today, expiries, cancel, progress)
                if rebuilt:
                    leaps = rebuilt[-self.lookback:]
            except (ScanCancelled, AuthError):
                raise
            except LongbridgeError as exc:
                log.warning("IV backfill failed for %s: %s", symbol, exc)
        if len(leaps) >= self.min_full_proxy:
            note = "Proxy: ATM IV of one long-dated expiry rebuilt from daily option closes - not a 30-day IV series."
            return self._describe(leaps, "leaps_proxy", f"LEAPS ATM-IV proxy ({len(leaps)} days)", note)
        # Both partial histories are short; use whichever covers more days.
        if max(len(snapshots), len(leaps)) >= self.min_partial:
            if len(leaps) > len(snapshots):
                return self._describe(leaps, "leaps_proxy_partial", f"LEAPS ATM-IV proxy, partial ({len(leaps)} days)",
                                      f"Thin option history: only {len(leaps)} days of long-dated closes, so the rank covers that window, not 52 weeks.")
            return self._describe(snapshots, "snapshots_partial", f"ATM IV snapshots, partial ({len(snapshots)} days)",
                                  f"Only {len(snapshots)} daily snapshots so far; the rank covers that window, not 52 weeks.")
        return {
            "value": None, "percentile": None, "level": None, "source": None, "source_kind": None,
            "history_days": max(len(snapshots), len(leaps)), "current_iv": None, "high": None, "low": None, "as_of": None,
            "note": f"No usable IV history yet ({len(snapshots)} daily snapshots, {len(leaps)} days of option history). "
                    "Thinly traded chains build up through the daily snapshots.",
        }

    def _from_cboe(self, symbol: str) -> Optional[dict]:
        proxies = {str(k).upper(): str(v).upper() for k, v in self.settings.section("indices.iv_rank_proxies").items()}
        index_name = proxies.get(ticker_of(symbol).upper())
        if not index_name:
            return None
        series = self.indices.try_index(index_name)
        if not series:
            return None
        desc = self._describe(series, "cboe_index", f"CBOE {index_name}",
                              f"{INDEX_LABELS.get(index_name, index_name)}: the index tracks this ETF's 30-day IV.")
        for key in ("current_iv", "high", "low"):
            desc[key] = desc[key] / 100.0
        return desc

    def _describe(self, series: List[Tuple[str, float]], kind: str, label: str, note: Optional[str] = None) -> dict:
        values = [v for _, v in series][-self.lookback:]
        rank = iv_rank(values, self.lookback)
        return {
            "value": rank,
            "percentile": percentile_rank(values, self.lookback),
            "level": classify_level(rank, self.cuts, IVR_LEVELS),
            "source": label,
            "source_kind": kind,
            "history_days": len(values),
            "current_iv": values[-1],
            "high": max(values),
            "low": min(values),
            "as_of": series[-1][0],
            "note": note,
        }

    @staticmethod
    def _last_session(today: date) -> date:
        day = today
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day

    def backfill(self, symbol: str, today: date, expiries: Optional[List[date]], cancel: Optional[threading.Event],
                 progress: Optional[Callable[[str], None]]) -> List[Tuple[str, float]]:
        assert self.client is not None
        cfg = self.settings.section("iv_backfill")
        analytics = self.settings.section("analytics")
        r = float(analytics.get("risk_free_rate", 0.04))
        q = float(analytics.get("dividend_yield", 0.0))
        max_strikes = int(cfg.get("max_strikes", 14))
        bars = int(cfg.get("days", 270))
        attempts = max(1, int(cfg.get("max_expiry_attempts", 2)))

        def report(text: str) -> None:
            if progress:
                progress(text)

        report("loading underlying price history")
        closes = self.client.daily_closes(symbol, bars, cancel)[-self.lookback:]
        if len(closes) < self.min_partial:
            raise LongbridgeError(f"only {len(closes)} daily closes for {symbol}")
        if expiries is None:
            expiries = self.client.expiry_dates(symbol, cancel)
        candidates = backfill_expiry_candidates(expiries, today, int(cfg.get("min_dte", 60)), int(cfg.get("max_dte", 400)))
        if not candidates:
            raise LongbridgeError(f"no suitable long-dated expiry for {symbol}")
        lo, hi = min(c for _, c in closes), max(c for _, c in closes)
        best: List[Tuple[str, float]] = []
        # Thin chains may only have a few weeks of history on the preferred expiry; try the next one before giving up.
        for expiry in candidates[:attempts]:
            report(f"loading {expiry.isoformat()} strikes")
            chain = self.client.put_strikes(symbol, expiry, 0.0, float("inf"), cancel)
            strikes = strikes_covering([k for k, _ in chain], lo, hi, max_strikes)
            by_strike = {k: sym for k, sym in chain}
            option_closes: Dict[float, Dict[str, float]] = {}
            for i, strike in enumerate(strikes):
                report(f"option history {i + 1}/{len(strikes)} (strike {strike:g}, expiry {expiry.isoformat()})")
                option_closes[strike] = dict(self.client.daily_closes(by_strike[strike], bars, cancel, is_option=True))
            series = build_proxy_iv_history(closes, option_closes, expiry, r, q)
            log.info("IV backfill for %s: %d points from expiry %s using %d strikes", symbol, len(series), expiry, len(strikes))
            if len(series) > len(best):
                best = series
            if len(best) >= self.min_full_proxy:
                break
        if len(best) >= self.min_partial:
            self.db.replace_iv_history(symbol, LEAPS_SOURCE, best)
        return best


class ScanService:
    def __init__(self, settings: Settings, client: LongbridgeClient, market: MarketService, db: Database,
                 iv_ranks: Optional[IvRankResolver] = None):
        self.settings = settings
        self.client = client
        self.market = market
        self.db = db
        self.iv_ranks = iv_ranks or IvRankResolver(settings, market.indices, db, None)

    # ------------------------------------------------------------------ shared steps
    def reference_sample(self, job: Job, symbol: str, spot: float, expiries: List[date], today: date,
                         progress_at: Tuple[float, float]) -> Tuple[list, dict, Dict[date, list]]:
        """Quote a thin near-the-money sample on the ~30 DTE expiry: ATM IV, 25-delta put IV and skew."""
        analytics = self.settings.section("analytics")
        lb_cfg = self.settings.section("longbridge")
        r = float(analytics.get("risk_free_rate", 0.04))
        q = float(analytics.get("dividend_yield", 0.0))
        reference_dte = int(analytics.get("reference_dte", 30))
        ref_band = lb_cfg.get("reference_strike_band", [0.85, 1.03])
        max_strikes = int(lb_cfg.get("max_strikes_per_expiry", 30))
        base, end = progress_at
        cancel = job.cancel_event

        future = [e for e in expiries if (e - today).days > 0]
        if not future:
            raise LongbridgeError(f"no future expiries for {symbol}")
        reference_expiry = min(future, key=lambda e: abs((e - today).days - reference_dte))
        chains: Dict[date, list] = {}
        job.update(phase=f"{symbol}: sampling reference expiry {reference_expiry.isoformat()}", message="ATM IV and skew", progress=base)
        chains[reference_expiry] = self.client.put_strikes(symbol, reference_expiry, 0.0, float("inf"), cancel)
        pairs = [p for p in chains[reference_expiry] if spot * ref_band[0] <= p[0] <= spot * ref_band[1]]
        pairs = subsample_strikes(pairs, max_strikes)
        if not pairs:
            raise LongbridgeError(f"option chain returned no puts near the money for {symbol}")
        job.update(phase=f"{symbol}: quoting reference puts", message=f"{len(pairs)} contracts", progress=(base + end) / 2)
        rows = self.client.option_quotes([sym for _, sym in pairs], cancel)
        candidates, _ = build_candidates(rows, spot, today, r, q)
        reference = reference_metrics(candidates, reference_dte, float(analytics.get("skew_delta", 0.25)), analytics.get("chain_skew_ratio", {}))
        self._store_snapshot(symbol, today, spot, reference)
        job.update(progress=end)
        return rows, reference, chains

    def _store_snapshot(self, symbol: str, today: date, spot: float, reference: dict) -> None:
        try:
            self.db.upsert_snapshot(symbol, today.isoformat(), spot, reference.get("atm_iv"), reference.get("skew_ratio"))
        except Exception as exc:
            log.warning("snapshot storage failed for %s: %s", symbol, exc)

    # ------------------------------------------------------------------ scan
    def run(self, job: Job) -> dict:
        started = time.time()
        calls_before = self.client.quota.total_calls
        job.update(phase="Loading VIX / SKEW history", message="Fetching CBOE index data", progress=0.02)
        _, _, metrics, regime = self.market.metrics_and_regime()

        scenario_key = job.scenario_override or regime.scenario
        scenario = SCENARIO_BY_KEY[scenario_key]
        scenario_cfg = job.filters or self.settings.section(f"scenarios.{scenario_key}")
        today = market_today()
        warnings: List[str] = []
        underlyings: List[dict] = []

        n = len(job.symbols)
        for i, symbol in enumerate(job.symbols):
            base = 0.05 + 0.95 * i / n
            span = 0.95 / n
            job.update(symbol=symbol)
            try:
                underlyings.append(self._scan_symbol(job, symbol, scenario_key, scenario_cfg, today, base, span))
            except (ScanCancelled, AuthError):
                raise
            except LongbridgeError as exc:
                log.warning("Scan of %s failed: %s", symbol, exc)
                warnings.append(f"{symbol}: {exc}")
                underlyings.append({"underlying": symbol, "error": str(exc), "candidates": []})

        result = {
            "scan_id": job.id,
            "started_at": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "duration_seconds": round(time.time() - started, 1),
            "api_calls": self.client.quota.total_calls - calls_before,
            "symbols": job.symbols,
            "scenario_source": "override" if job.scenario_override else "regime",
            "scenario": {
                "key": scenario.key,
                "title": scenario.title,
                "stance": scenario.stance,
                "action": scenario.action,
                "description": scenario.description,
                "filters": scenario_cfg,
                "filters_source": "custom" if job.filters else "default",
            },
            "regime": regime.to_dict(),
            "market": metrics.to_dict(),
            "underlyings": underlyings,
            "warnings": warnings,
        }
        try:
            self.db.save_scan(result)
        except Exception as exc:  # persistence must never fail the scan
            log.warning("Could not persist scan: %s", exc)
        return result

    def _scan_symbol(self, job: Job, symbol: str, scenario_key: str, scenario_cfg: dict, today: date, base: float, span: float) -> dict:
        cancel = job.cancel_event
        analytics = self.settings.section("analytics")
        lb_cfg = self.settings.section("longbridge")
        r = float(analytics.get("risk_free_rate", 0.04))
        q = float(analytics.get("dividend_yield", 0.0))
        fallback_band = lb_cfg.get("strike_band", [0.6, 1.0])
        max_expiries = int(lb_cfg.get("max_expiries_per_symbol", 3))
        max_strikes = int(lb_cfg.get("max_strikes_per_expiry", 30))
        depth_top_n = int(lb_cfg.get("depth_top_n", 12))
        max_results = int(scenario_cfg.get("max_results", 12))
        dte_lo, dte_hi = scenario_cfg.get("dte", [30, 45])
        delta_lo, delta_hi = scenario_cfg.get("delta", [0.1, 0.3])
        warnings: List[str] = []

        job.update(phase=f"{symbol}: fetching spot price", message="", progress=base + span * 0.03)
        spot_info = self.client.spot(symbol, cancel)
        spot = spot_info["last"]
        if not spot:
            raise LongbridgeError(f"no last price for {symbol}")

        job.update(phase=f"{symbol}: loading expiry dates", progress=base + span * 0.06)
        expiries = self.client.expiry_dates(symbol, cancel)
        reference_expiry, scenario_expiries = self._select_expiries(
            expiries, today, dte_lo, dte_hi, int(analytics.get("reference_dte", 30)), max_expiries)
        if reference_expiry is None:
            raise LongbridgeError(f"no future expiries for {symbol}")
        if not scenario_expiries:
            raise LongbridgeError(f"no expiries between {dte_lo} and {dte_hi} DTE for {symbol}")

        # Phase 1: a thin sample around the money on the reference expiry gives ATM IV and the put skew.
        rows, reference, chains = self.reference_sample(job, symbol, spot, expiries, today, (base + span * 0.10, base + span * 0.20))
        iv_rank_info = self.iv_ranks.resolve(symbol, today, expiries)
        if not reference.get("atm_iv"):
            warnings.append("ATM implied volatility unavailable; using the fallback strike band")

        def chain(expiry: date) -> list:
            if expiry not in chains:
                chains[expiry] = self.client.put_strikes(symbol, expiry, 0.0, float("inf"), cancel)
            return chains[expiry]

        # Phase 2: only the strikes that can hold the scenario's delta range, on a few expiries.
        quoted = {row.symbol for row in rows}
        wanted: List[str] = []
        for k, expiry in enumerate(scenario_expiries):
            job.update(phase=f"{symbol}: loading option chain", message=f"expiry {expiry.isoformat()} ({k + 1}/{len(scenario_expiries)})",
                       progress=base + span * (0.22 + 0.16 * (k + 1) / len(scenario_expiries)))
            dte = (expiry - today).days
            lo, hi = strike_band_for_deltas(spot, delta_lo, delta_hi, dte, reference.get("atm_iv"), reference.get("skew_ratio"), r, q, fallback_band)
            full_chain = chain(expiry)
            pairs = subsample_strikes([p for p in full_chain if lo <= p[0] <= hi], max_strikes)
            # Two strikes bracketing spot give this expiry its own ATM IV, so IV/ATM is not distorted by term structure.
            below = [p for p in full_chain if p[0] <= spot]
            above = [p for p in full_chain if p[0] >= spot]
            pairs += ([below[-1]] if below else []) + ([above[0]] if above else [])
            wanted.extend(sym for _, sym in pairs if sym not in quoted)
        wanted = list(dict.fromkeys(wanted))

        def on_batch(done: int, total: int) -> None:
            job.update(phase=f"{symbol}: fetching option quotes", message=f"batch {done}/{total} ({len(wanted)} contracts)",
                       progress=base + span * (0.40 + 0.16 * done / total))

        if wanted:
            job.update(phase=f"{symbol}: fetching option quotes", message=f"{len(wanted)} contracts", progress=base + span * 0.40)
            rows = rows + self.client.option_quotes(wanted, cancel, on_batch)
        candidates, skipped = build_candidates(rows, spot, today, r, q)
        if skipped.get("no_iv"):
            warnings.append(f"{skipped['no_iv']} puts had no implied volatility and were skipped")
        atm_by_expiry = atm_iv_by_expiry(candidates)

        filtered = filter_candidates(candidates, scenario_cfg, reference.get("atm_iv"), atm_by_expiry=atm_by_expiry)
        ranked = score_candidates(filtered, scenario_key, scenario_cfg.get("max_spread_pct"))
        shortlist = ranked[: max(depth_top_n, max_results)]

        for k, cand in enumerate(shortlist):
            job.update(phase=f"{symbol}: fetching bid/ask for top candidates", message=f"{k + 1}/{len(shortlist)} {cand.symbol}",
                       progress=base + span * (0.58 + 0.40 * (k + 1) / max(len(shortlist), 1)))
            try:
                depth = self.client.depth(cand.symbol, cancel)
                apply_depth(cand, depth.bid, depth.ask)
            except LongbridgeError as exc:
                log.warning("depth failed for %s: %s", cand.symbol, exc)
                cand.flags.append("no_depth")

        final = filter_candidates(shortlist, scenario_cfg, reference.get("atm_iv"), enforce_spread=True, atm_by_expiry=atm_by_expiry)
        final = score_candidates(final, scenario_key, scenario_cfg.get("max_spread_pct"))[:max_results]
        dropped = len(shortlist) - len(final)
        if dropped > 0:
            warnings.append(f"{dropped} candidates dropped after bid/ask check (wide spread or no quote)")
        job.update(phase=f"{symbol}: done", message=f"{len(final)} candidates", progress=base + span)

        return {
            "underlying": symbol,
            "spot": spot_info,
            "reference": reference,
            "atm_iv_by_expiry": {k: round(v, 5) for k, v in atm_by_expiry.items()},
            "iv_rank": iv_rank_info,
            "expiries": [e.isoformat() for e in scenario_expiries],
            "stats": {
                "puts_quoted": len(rows),
                "with_greeks": len(candidates),
                "after_filters": len(filtered),
                "skipped": skipped,
            },
            "candidates": [c.to_dict() for c in final],
            "warnings": warnings,
        }

    @staticmethod
    def _select_expiries(expiries: List[date], today: date, dte_lo: int, dte_hi: int, reference_dte: int,
                         max_expiries: int) -> Tuple[Optional[date], List[date]]:
        future = [e for e in expiries if (e - today).days > 0]
        if not future:
            return None, []
        reference = min(future, key=lambda e: abs((e - today).days - reference_dte))
        in_range = [e for e in future if dte_lo <= (e - today).days <= dte_hi]
        if len(in_range) > max_expiries:
            if reference in in_range:
                others = [e for e in in_range if e != reference]
                keep = subsample_strikes(others, max_expiries - 1) if max_expiries > 1 else []
                in_range = sorted([reference] + keep)
            else:
                in_range = subsample_strikes(in_range, max_expiries)
        return reference, in_range


class IvRefreshService:
    """Refreshes ATM IV, skew and IV rank for a watchlist; backfills IV history where needed."""

    def __init__(self, settings: Settings, client: LongbridgeClient, scanner: ScanService, iv_ranks: IvRankResolver, db: Database):
        self.settings = settings
        self.client = client
        self.scanner = scanner
        self.iv_ranks = iv_ranks
        self.db = db

    def run(self, job: Job) -> dict:
        started = time.time()
        calls_before = self.client.quota.total_calls
        today = market_today()
        items: List[dict] = []
        warnings: List[str] = []
        n = len(job.symbols)
        for i, symbol in enumerate(job.symbols):
            base = 0.02 + 0.98 * i / n
            span = 0.98 / n
            job.update(symbol=symbol)
            try:
                items.append(self._refresh_symbol(job, symbol, today, base, span))
            except (ScanCancelled, AuthError):
                raise
            except LongbridgeError as exc:
                log.warning("IV refresh of %s failed: %s", symbol, exc)
                warnings.append(f"{symbol}: {exc}")
                items.append({"underlying": symbol, "ticker": ticker_of(symbol), "error": str(exc)})
        return {
            "job_id": job.id,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "duration_seconds": round(time.time() - started, 1),
            "api_calls": self.client.quota.total_calls - calls_before,
            "symbols": job.symbols,
            "items": items,
            "warnings": warnings,
        }

    def _refresh_symbol(self, job: Job, symbol: str, today: date, base: float, span: float) -> dict:
        cancel = job.cancel_event
        job.update(phase=f"{symbol}: fetching spot price", message="", progress=base + span * 0.05)
        spot_info = self.client.spot(symbol, cancel)
        spot = spot_info["last"]
        if not spot:
            raise LongbridgeError(f"no last price for {symbol}")
        job.update(phase=f"{symbol}: loading expiry dates", progress=base + span * 0.10)
        expiries = self.client.expiry_dates(symbol, cancel)
        _, reference, _ = self.scanner.reference_sample(job, symbol, spot, expiries, today, (base + span * 0.15, base + span * 0.35))

        def progress(text: str) -> None:
            job.update(phase=f"{symbol}: rebuilding IV history", message=text)

        job.update(phase=f"{symbol}: resolving IV rank", message="", progress=base + span * 0.40)
        rank = self.iv_ranks.resolve(symbol, today, expiries, allow_backfill=True, cancel=cancel, progress=progress)
        summary = f"IV rank {round(rank['value'])}" if rank.get("value") is not None else "IV rank unavailable"
        job.update(phase=f"{symbol}: done", message=summary, progress=base + span)
        payload = {
            "underlying": symbol,
            "ticker": ticker_of(symbol),
            "market_date": today.isoformat(),
            "spot": spot_info,
            "atm_iv": reference.get("atm_iv"),
            "otm_put_iv": reference.get("otm_put_iv"),
            "skew_ratio": reference.get("skew_ratio"),
            "skew_level": reference.get("skew_level"),
            "reference_expiry": reference.get("expiry"),
            "reference_dte": reference.get("dte"),
            "iv_rank": rank,
            "snapshots": len(self.db.atm_iv_history(symbol, 252)),
        }
        try:
            self.db.save_watchlist_iv(symbol, payload)
        except Exception as exc:
            log.warning("watchlist storage failed for %s: %s", symbol, exc)
        return payload


class JobManager:
    def __init__(self, scanner: ScanService, iv_refresh: IvRefreshService, db: Database):
        self.scanner = scanner
        self.iv_refresh = iv_refresh
        self.db = db
        self._lock = threading.Lock()
        self._current: Optional[Job] = None
        self._last_result: Optional[dict] = None
        self._last_iv_result: Optional[dict] = None

    def start_scan(self, symbols: List[str], scenario_override: Optional[str] = None, filters: Optional[dict] = None) -> Job:
        return self._start(Job("scan", symbols, scenario_override, filters))

    def start_iv_refresh(self, symbols: List[str]) -> Job:
        return self._start(Job("iv_refresh", symbols))

    def _start(self, job: Job) -> Job:
        with self._lock:
            if self._current and self._current.state in ("queued", "running"):
                raise RuntimeError("a job is already running")
            self._current = job
        thread = threading.Thread(target=self._run, args=(job,), name=f"{job.kind}-{job.id}", daemon=True)
        thread.start()
        return job

    def is_busy(self) -> bool:
        with self._lock:
            return bool(self._current and self._current.state in ("queued", "running"))

    def _run(self, job: Job) -> None:
        job.state = "running"
        job.update(phase="Starting scan" if job.kind == "scan" else "Starting IV refresh", message=", ".join(job.symbols))
        try:
            if job.kind == "scan":
                result = self.scanner.run(job)
                with self._lock:
                    self._last_result = result
            else:
                result = self.iv_refresh.run(job)
                with self._lock:
                    self._last_iv_result = result
            job.result = result
            job.finish("done")
            log.info("[job %s] %s finished in %.1fs using %d API calls", job.id, job.kind, result["duration_seconds"], result["api_calls"])
        except ScanCancelled:
            job.finish("cancelled")
        except AuthError as exc:
            job.finish("error", f"Longbridge authentication failed: {exc}. Update LONGPORT_ACCESS_TOKEN in .env and restart.")
        except Exception as exc:
            log.exception("[job %s] failed", job.id)
            job.finish("error", str(exc))

    def cancel(self) -> bool:
        with self._lock:
            job = self._current
        if job and job.state in ("queued", "running"):
            job.cancel_event.set()
            job.update(phase="Cancelling", message="waiting for the current request to finish")
            return True
        return False

    def current(self) -> Optional[dict]:
        with self._lock:
            return self._current.snapshot() if self._current else None

    def last_result(self) -> Optional[dict]:
        with self._lock:
            if self._last_result is not None:
                return self._last_result
        try:
            result = self.db.latest_scan()
        except Exception as exc:
            log.warning("could not load last scan: %s", exc)
            return None
        with self._lock:
            self._last_result = result
        return result

    def last_iv_result(self) -> Optional[dict]:
        with self._lock:
            return self._last_iv_result


class DailySnapshotScheduler:
    """Refreshes the default watchlist once per weekday after the US close so IV history keeps growing."""

    def __init__(self, settings: Settings, jobs: JobManager, db: Database):
        self.settings = settings
        self.jobs = jobs
        self.db = db
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_run_date: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("iv_snapshot.enabled", True))

    def start(self) -> None:
        if not self.enabled:
            log.info("Daily IV snapshot scheduler disabled")
            return
        self._thread = threading.Thread(target=self._loop, name="iv-snapshot-scheduler", daemon=True)
        self._thread.start()
        log.info("Daily IV snapshot scheduler armed for %s ET on weekdays", self.settings.get("iv_snapshot.daily_at_et", "16:20"))

    def stop(self) -> None:
        self._stop.set()

    def watchlist(self) -> List[str]:
        return [with_market(str(s).upper()) for s in self.settings.default_watchlist]

    def due(self, now: Optional[datetime] = None) -> bool:
        now = now or market_now()
        if now.weekday() >= 5:
            return False
        hh, mm = str(self.settings.get("iv_snapshot.daily_at_et", "16:20")).split(":")
        if (now.hour, now.minute) < (int(hh), int(mm)):
            return False
        today = now.date().isoformat()
        if self.last_run_date == today:
            return False
        symbols = self.watchlist()
        stored = self.db.watchlist_iv(symbols)
        # Compare on the US market date: the server clock may sit in another timezone.
        return any(not stored.get(s) or str(stored[s].get("market_date", "")) < today for s in symbols)

    def _loop(self) -> None:
        while not self._stop.wait(60):
            try:
                if self.due() and not self.jobs.is_busy():
                    symbols = self.watchlist()
                    self.jobs.start_iv_refresh(symbols)
                    self.last_run_date = market_today().isoformat()
                    log.info("Daily IV snapshot started for %s", ", ".join(symbols))
            except Exception as exc:
                log.warning("Daily IV snapshot scheduler error: %s", exc)
