from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.config import load_settings
from backend.jobs import JobManager
from backend.main import ScreenerFilters, app, normalize_symbols
from backend.storage.db import Database
from tests.fakes import FakeLongbridge
from tests.test_scan_flow import StubMarket, assert_contract_sides, build_services, provider_backed_quotes

client = TestClient(app)
DEFAULT_WATCHLIST = [str(s).upper() for s in load_settings().default_watchlist]

VALID_FILTERS = {"dte_min": 30, "dte_max": 45, "delta_min": 0.1, "delta_max": 0.25, "min_open_interest": 100,
                 "max_spread_pct": 10, "min_annualized_yield_pct": None, "target_delta": 0.2, "max_results": 10}


def test_normalize_symbols():
    assert normalize_symbols(["spy", " qqq.us ,IWM", "SPY.US", ""]) == ["SPY.US", "QQQ.US", "IWM.US"]
    assert normalize_symbols([]) == []
    assert normalize_symbols(["SPY", "QQQ", "IWM"]) == ["SPY.US", "QQQ.US", "IWM.US"]


def test_screener_filters_to_config():
    cfg = ScreenerFilters(**VALID_FILTERS).to_config()
    assert cfg == {"dte": [30, 45], "delta": [0.1, 0.25], "min_open_interest": 100, "max_spread_pct": 10.0,
                   "max_results": 10, "target_delta": 0.2}


def test_cleared_optional_filters_stay_omitted_without_mutating_input():
    raw = dict(VALID_FILTERS, min_annualized_yield_pct=None, target_delta=None)
    original = deepcopy(raw)
    cfg = ScreenerFilters(**raw).to_config()
    assert "min_annualized_yield_pct" not in cfg and "target_delta" not in cfg
    assert "option_side" not in cfg  # The selected scenario supplies identity downstream.
    assert cfg["dte"] == [30, 45] and cfg["delta"] == [0.1, 0.25]
    assert raw == original


@pytest.fixture
def scan_api(monkeypatch, tmp_path):
    fake = FakeLongbridge()
    provider_backed_quotes(fake, monkeypatch)
    scanner, refresher = build_services(Database(tmp_path / "api-scan.db"), fake)
    scanner.market = StubMarket([14, 14], [120, 120])  # Real regime selection -> cheap calls.
    manager = JobManager(scanner, refresher, scanner.db)

    def start_inline(job):
        manager._current = job
        manager._run(job)
        return job

    # Exercise the request, JobManager and scanner without threads, production I/O or credentials.
    monkeypatch.setattr(manager, "_start", start_inline)
    monkeypatch.setattr(main, "jobs", manager)
    monkeypatch.setattr(main, "settings", SimpleNamespace(
        default_watchlist=["SPY"], longbridge_credentials_present=True,
    ))
    return manager


@pytest.mark.parametrize("scenario,custom", [
    ("buy_cheap_calls", False), (None, False),
    ("buy_cheap_calls", True), (None, True),
    ("sell_20_delta_puts", True), ("buy_puts", False),
])
def test_scan_api_recommendations_preserve_contract_identity(scan_api, scenario, custom):
    request = {"symbols": ["spy"], "scenario": scenario}
    if custom:
        request["filters"] = dict(VALID_FILTERS, dte_min=30, dte_max=32, delta_min=0, delta_max=1,
                                  min_open_interest=0, max_spread_pct=100, target_delta=None, max_results=5)
    original = deepcopy(request)
    response = client.post("/api/scan", json=request)
    assert response.status_code == 202
    job = response.json()["job"]
    assert job["state"] == "done" and job["custom_filters"] == custom
    response = client.get("/api/recommendations")
    assert response.status_code == 200
    result = response.json()
    assert result["scan_id"] == job["id"]
    assert result["scenario"]["key"] == (scenario or "buy_cheap_calls")
    assert result["scenario_source"] == ("override" if scenario else "regime")
    assert result["scenario"]["filters_source"] == ("custom" if custom else "default")
    side = "call" if scenario in (None, "buy_cheap_calls") else "put"
    assert_contract_sides(result, side)
    if custom:
        cfg = result["scenario"]["filters"]
        assert cfg == dict(ScreenerFilters(**request["filters"]).to_config(), option_side=side)
        assert "target_delta" not in cfg and "min_annualized_yield_pct" not in cfg
        u = result["underlyings"][0]
        assert u["reference"]["expiry"] in u["expiries"]
        assert all(30 <= c["dte"] <= 32 and c["delta_target_gap"] is None for c in u["candidates"])
    assert request == original


def test_scan_rejects_inconsistent_filters():
    bad = dict(VALID_FILTERS, dte_min=60)
    resp = client.post("/api/scan", json={"symbols": ["SPY"], "filters": bad})
    assert resp.status_code == 422 and "dte_min" in resp.text
    bad = dict(VALID_FILTERS, delta_max=0.05)
    assert client.post("/api/scan", json={"symbols": ["SPY"], "filters": bad}).status_code == 422
    bad = dict(VALID_FILTERS, max_results=500)
    assert client.post("/api/scan", json={"symbols": ["SPY"], "filters": bad}).status_code == 422


def test_config_endpoint_lists_all_scenarios():
    data = client.get("/api/config").json()
    assert [s["key"] for s in data["scenarios"]] == [
        "buy_cheap_calls", "buy_puts", "selective_put_selling", "sell_20_delta_puts",
        "close_shorts_low_delta", "avoid_naked_puts", "scale_into_put_selling",
    ]
    assert all(s["filters"].get("dte") for s in data["scenarios"])
    calls = next(s for s in data["scenarios"] if s["key"] == "buy_cheap_calls")
    assert calls["option_side"] == calls["filters"]["option_side"] == "call"
    assert all(s["option_side"] == "put" for s in data["scenarios"] if s["key"] != "buy_cheap_calls")
    assert data["watchlist"] == DEFAULT_WATCHLIST and data["watchlist"]
    assert all("." not in t for t in data["watchlist"])


def test_status_endpoint_shape():
    data = client.get("/api/status").json()
    assert set(data) >= {"server_time", "job", "quota", "credentials_present", "sources"}
    assert set(data["quota"]) >= {"waiting", "waiting_seconds", "waiting_reason", "total_calls", "rate_limited_events"}


def test_scan_rejects_unknown_scenario_and_too_many_symbols():
    assert client.post("/api/scan", json={"symbols": ["SPY"], "scenario": "nope"}).status_code == 400
    assert client.post("/api/scan", json={"symbols": [f"S{i}" for i in range(11)]}).status_code == 400


def test_index_page_served():
    resp = client.get("/")
    assert resp.status_code == 200 and "Options Screener" in resp.text and "Watchlist IV Rank" in resp.text
    assert 'id="card-kalshi"' in resp.text and 'id="card-kalshi-december_hikes"' in resp.text


def test_watchlist_endpoint_lists_default_tickers_without_suffix():
    data = client.get("/api/watchlist").json()
    assert data["symbols"] == [f"{t}.US" for t in DEFAULT_WATCHLIST]
    assert [i["ticker"] for i in data["items"]] == DEFAULT_WATCHLIST
    assert set(data["scheduler"]) == {"enabled", "daily_at_et", "last_run_date"}
    custom = client.get("/api/watchlist?symbols=aapl,TSLA").json()
    assert [i["ticker"] for i in custom["items"]] == ["AAPL", "TSLA"]


def test_watchlist_refresh_validation():
    too_many = {"symbols": [f"S{i}" for i in range(16)]}
    assert client.post("/api/watchlist/refresh", json=too_many).status_code == 400


def test_macro_endpoint_rejects_unknown_range_and_section():
    assert client.get("/api/macro?range=2w").status_code == 400
    assert client.get("/api/macro?range=1d&section=nope").status_code == 404
    config = client.get("/api/config").json()
    assert [s["id"] for s in config["live_sections"]] == ["vix", "stocks", "macro", "japan_yields", "japan_fx", "us_jp_gaps", "commodities"]
    assert config["kalshi"]["enabled"] is True and config["kalshi"]["event"] == "next" and config["kalshi"]["markets"] == []
    assert [chart["id"] for chart in config["kalshi_charts"]] == ["next", "december_hikes"]
    december = config["kalshi_charts"][1]
    assert december["event"] == "december" and december["markets"] == ["HIKE"]
    assert client.get("/api/kalshi?range=2w").status_code == 400
    assert client.get("/api/kalshi?chart=nope").status_code == 404
    assert config["live_default_range"] in ("1d", "5d", "1m", "3m", "6m", "1y", "5y")


def test_macro_endpoint_validates_custom_symbols():
    config = client.get("/api/config").json()
    stocks = next(s for s in config["live_sections"] if s["id"] == "stocks")
    assert stocks["symbols"] == ["SOXL.US", "TECL.US", "UPRO.US", "SPXL.US"] and config["live_max_symbols"] == 12
    assert next(s for s in config["live_sections"] if s["id"] == "macro")["symbols"] is None
    # Only symbol sections take a ticker list, and it must be short and made of plain symbol characters.
    resp = client.get("/api/macro?section=macro&symbols=SOXL")
    assert resp.status_code == 400 and "custom symbols" in resp.json()["detail"]
    too_many = ",".join(f"S{i}" for i in range(13))
    assert client.get(f"/api/macro?section=stocks&symbols={too_many}").status_code == 400
    resp = client.get("/api/macro?section=stocks&symbols=SOXL,../etc")
    assert resp.status_code == 400 and "../ETC" in resp.json()["detail"]
    assert client.get("/api/macro?section=stocks&symbols=%24SPX").status_code == 400
