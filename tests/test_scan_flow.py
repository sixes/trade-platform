import threading
import time
from datetime import datetime

import pytest

from backend.analytics.regime import classify_regime, compute_market_metrics
from backend.config import Settings, load_settings
from backend.jobs import DailySnapshotScheduler, IvRankResolver, IvRefreshService, Job, JobManager, ScanService
from backend.market_data.longbridge import AuthError, LongbridgeClient, QuotaExhausted, ScanCancelled
from backend.storage.db import Database
from tests.fakes import FakeLongbridge
from tests.test_regime import make_series

SETTINGS = load_settings()


class StubIndices:
    def __init__(self, vix):
        self.vix_series = vix

    def try_index(self, name):
        return self.vix_series if name.upper() == "VIX" else None


class StubMarket:
    def __init__(self, vix_tail, skew_tail):
        self.vix = make_series(14, 30, vix_tail)
        self.skew = make_series(128, 132, skew_tail)
        self.indices = StubIndices(self.vix)

    def metrics_and_regime(self):
        metrics = compute_market_metrics(self.vix, self.skew, SETTINGS.section("thresholds"))
        return self.vix, self.skew, metrics, classify_regime(metrics)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


def build_services(db, fake):
    market = StubMarket([19, 19], [130, 130])  # medium regime -> sell_20_delta_puts
    resolver = IvRankResolver(SETTINGS, market.indices, db, fake)
    scanner = ScanService(SETTINGS, fake, market, db, resolver)
    return scanner, IvRefreshService(SETTINGS, fake, scanner, resolver, db)


def run_scan(db, fake, symbols, scenario=None, filters=None):
    scanner, _ = build_services(db, fake)
    job = Job("scan", symbols, scenario, filters)
    return scanner.run(job), job


def test_scan_pipeline_produces_ranked_candidates_and_snapshots(db):
    fake = FakeLongbridge()
    result, job = run_scan(db, fake, ["SPY.US"])

    assert result["scenario"]["key"] == "sell_20_delta_puts"
    assert result["scenario_source"] == "regime"
    assert result["api_calls"] == len(fake.calls) > 0
    u = result["underlyings"][0]
    assert u["underlying"] == "SPY.US" and not u.get("error")
    cfg = SETTINGS.section("scenarios.sell_20_delta_puts")
    assert 1 <= len(u["candidates"]) <= cfg["max_results"]
    for c in u["candidates"]:
        assert cfg["delta"][0] <= abs(c["delta"]) <= cfg["delta"][1]
        assert cfg["dte"][0] <= c["dte"] <= cfg["dte"][1]
        assert c["price_source"] == "mid" and c["spread_pct"] <= cfg["max_spread_pct"]
    scores = [c["score"] for c in u["candidates"]]
    assert scores == sorted(scores, reverse=True)

    # Chain calls: one per scenario expiry plus the reference expiry (shared when it is in range).
    fetched_expiries = set(u["expiries"]) | {u["reference"]["expiry"]}
    assert fake.calls.count("option_chain_info_by_date") == len(fetched_expiries)
    assert len(u["expiries"]) <= SETTINGS.get("longbridge.max_expiries_per_symbol")
    assert fake.calls.count("depth") <= max(SETTINGS.get("longbridge.depth_top_n"), cfg["max_results"])
    # The whole underlying fits in one minute of option-quote budget.
    assert fake.option_contracts_quoted <= SETTINGS.get("longbridge.option_contracts_per_minute")
    assert fake.option_contracts_quoted <= (len(fetched_expiries)) * SETTINGS.get("longbridge.max_strikes_per_expiry")

    assert u["reference"]["atm_iv"] is not None and u["reference"]["skew_ratio"] > 1
    # SPY maps to the VIX index, so its IV rank comes from CBOE data rather than our snapshots.
    assert u["iv_rank"]["source_kind"] == "cboe_index" and u["iv_rank"]["value"] is not None
    assert db.atm_iv_history("SPY.US")[0][1] == pytest.approx(u["reference"]["atm_iv"])
    assert db.latest_scan()["scan_id"] == result["scan_id"]
    assert job.progress > 0.9


def test_scenario_override_changes_filters(db):
    fake = FakeLongbridge()
    result, _ = run_scan(db, fake, ["SPY.US"], scenario="buy_puts")
    assert result["scenario"]["key"] == "buy_puts" and result["scenario_source"] == "override"
    assert result["scenario"]["filters_source"] == "default"
    cfg = SETTINGS.section("scenarios.buy_puts")
    for c in result["underlyings"][0]["candidates"]:
        assert cfg["dte"][0] <= c["dte"] <= cfg["dte"][1]
        assert cfg["delta"][0] <= abs(c["delta"]) <= cfg["delta"][1]


def test_custom_filters_replace_scenario_defaults(db):
    fake = FakeLongbridge()
    custom = {"dte": [55, 100], "delta": [0.05, 0.12], "min_open_interest": 0, "max_spread_pct": 50, "max_results": 3}
    result, _ = run_scan(db, fake, ["SPY.US"], filters=custom)
    assert result["scenario"]["key"] == "sell_20_delta_puts"
    assert result["scenario"]["filters_source"] == "custom"
    assert result["scenario"]["filters"] == custom
    candidates = result["underlyings"][0]["candidates"]
    assert 1 <= len(candidates) <= 3
    for c in candidates:
        assert 55 <= c["dte"] <= 100
        assert 0.05 <= abs(c["delta"]) <= 0.12


def test_job_manager_runs_in_background_and_rejects_concurrent_scans(db):
    fake = FakeLongbridge()
    scanner, refresher = build_services(db, fake)
    manager = JobManager(scanner, refresher, db)
    job = manager.start_scan(["SPY.US"])
    with pytest.raises(RuntimeError):
        manager.start_iv_refresh(["QQQ.US"])
    for _ in range(200):
        if manager.current()["state"] == "done":
            break
        time.sleep(0.05)
    snap = manager.current()
    assert snap["state"] == "done" and snap["progress"] == 1.0 and snap["kind"] == "scan"
    assert manager.last_result()["scan_id"] == job.id


def test_iv_refresh_backfills_history_and_stores_watchlist_entry(db):
    fake = FakeLongbridge(atm_iv=0.18)
    scanner, refresher = build_services(db, fake)
    job = Job("iv_refresh", ["XYZ.US"])  # no CBOE proxy for XYZ -> falls back to the LEAPS backfill
    result = refresher.run(job)
    item = result["items"][0]
    rank = item["iv_rank"]
    assert item["ticker"] == "XYZ" and item["atm_iv"] == pytest.approx(0.18, abs=0.005)
    assert rank["source_kind"] == "leaps_proxy" and rank["history_days"] >= 200
    # Synthetic history: a spike to 1.8x IV in the middle of the window, today near the low end of the range.
    assert 5 <= rank["value"] <= 25 and rank["percentile"] >= 75
    assert rank["current_iv"] == pytest.approx(0.18, abs=0.01) and rank["high"] == pytest.approx(0.324, abs=0.01)
    assert fake.calls.count("candlesticks") <= 1 + SETTINGS.get("iv_backfill.max_strikes")
    stored = db.watchlist_iv(["XYZ.US"])["XYZ.US"]
    assert stored["iv_rank"]["value"] == pytest.approx(rank["value"]) and stored["market_date"]
    assert len(db.iv_history("XYZ.US", "leaps")) == rank["history_days"]

    # A second refresh the same day reuses the stored history instead of re-downloading candles.
    calls_before = fake.calls.count("candlesticks")
    refresher.run(Job("iv_refresh", ["XYZ.US"]))
    assert fake.calls.count("candlesticks") == calls_before


def test_daily_scheduler_due_logic(db):
    fake = FakeLongbridge()
    scanner, refresher = build_services(db, fake)
    scheduler = DailySnapshotScheduler(SETTINGS, JobManager(scanner, refresher, db), db)
    weekday_after_close = datetime(2026, 9, 23, 16, 30)
    assert scheduler.due(weekday_after_close) is True
    assert scheduler.due(datetime(2026, 9, 23, 15, 0)) is False  # before the configured time
    assert scheduler.due(datetime(2026, 9, 26, 16, 30)) is False  # Saturday
    for sym in scheduler.watchlist():
        db.save_watchlist_iv(sym, {"market_date": "2026-09-23"})
    assert scheduler.due(weekday_after_close) is False  # already refreshed for this market date
    assert scheduler.due(datetime(2026, 9, 24, 16, 30)) is True


def fast_client(**overrides):
    cfg = dict(SETTINGS.section("longbridge"))
    cfg.update({"base_backoff_seconds": 0.01, "max_backoff_seconds": 0.05, "requests_per_second": 1000, "burst": 1000})
    cfg.update(overrides)
    return LongbridgeClient(Settings({"longbridge": cfg}))


def test_rate_limit_errors_are_retried_and_reported():
    from longport import openapi as lo

    client = fast_client()
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise lo.OpenApiException(lo.ErrorKind.OpenApi, 301606, "trace", "request rate limit")
        return "ok"

    assert client._call("quote", flaky) == "ok"
    snap = client.quota.snapshot()
    assert attempts["n"] == 3
    assert snap["rate_limited_events"] == 2
    assert snap["total_calls"] == 1
    assert snap["waiting"] is False


def test_rate_limit_gives_up_after_max_retries():
    from longport import openapi as lo

    client = fast_client(max_retries=2)

    def always_limited():
        raise lo.OpenApiException(lo.ErrorKind.OpenApi, 301606, "trace", "request rate limit")

    with pytest.raises(QuotaExhausted):
        client._call("quote", always_limited)
    assert client.quota.snapshot()["rate_limited_events"] == 3


def test_auth_errors_surface_immediately():
    from longport import openapi as lo

    client = fast_client()

    def expired():
        raise lo.OpenApiException(lo.ErrorKind.OpenApi, 401003, "trace", "token expired")

    with pytest.raises(AuthError):
        client._call("quote", expired)
    assert "token expired" in client.quota.snapshot()["auth_error"]


def test_option_quota_error_waits_a_full_window_then_retries():
    from longport import openapi as lo

    client = fast_client(option_contracts_per_minute=50)
    client.option_budget.window = 0.3  # shrink the rolling minute for the test
    attempts = {"n": 0}

    def limited_once():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise lo.OpenApiException(lo.ErrorKind.OpenApi, 301607, "trace", "Too many option securities request within one minute")
        return "ok"

    started = time.monotonic()
    assert client._call("option_quote", limited_once) == "ok"
    assert time.monotonic() - started >= 0.3
    snap = client.status()
    assert snap["rate_limited_events"] == 1 and snap["option_contracts_per_minute"] == 50


def test_option_quotes_wait_when_minute_budget_is_spent():
    client = fast_client(option_contracts_per_minute=30, option_quote_batch_size=20)
    client.option_budget.window = 0.4
    assert client.batch_size == 20
    assert client.option_budget.reserve(20) == 0
    wait = client.option_budget.reserve(20)  # 40 > 30: must wait for the first reservation to age out
    assert 0 < wait <= 0.4
    started = time.monotonic()
    client._wait_for_option_budget(20, None)
    assert time.monotonic() - started >= wait - 0.05
    assert client.option_budget.used() == 20


def test_pacing_wait_is_visible_in_status_and_cancellable():
    client = fast_client(requests_per_second=2, burst=1, base_backoff_seconds=0.01)
    cancel = threading.Event()
    assert client._call("quote", lambda: 1) == 1
    started = time.monotonic()
    assert client._call("quote", lambda: 2) == 2
    assert time.monotonic() - started >= 0.3  # second call had to wait for a token

    cancel.set()
    with pytest.raises(ScanCancelled):
        client._call("quote", lambda: 3, cancel)
