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
    sections = {s["id"]: s for s in config["live_sections"]}
    assert list(sections) == ["vix", "stocks", "global_equities", "macro", "japan_yields", "japan_fx", "us_jp_gaps", "commodities"]
    assert sections["stocks"]["chart_mode"] == sections["global_equities"]["chart_mode"] == "percent_change"
    assert sections["global_equities"]["symbols"] is None and sections["global_equities"]["hidden"] is False
    assert all(s["chart_mode"] is None for key, s in sections.items() if key not in ("stocks", "global_equities"))
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
    for section in ("macro", "global_equities"):
        resp = client.get(f"/api/macro?section={section}&symbols=SOXL")
        assert resp.status_code == 400 and "custom symbols" in resp.json()["detail"]
    too_many = ",".join(f"S{i}" for i in range(13))
    assert client.get(f"/api/macro?section=stocks&symbols={too_many}").status_code == 400
    resp = client.get("/api/macro?section=stocks&symbols=SOXL,../etc")
    assert resp.status_code == 400 and "../ETC" in resp.json()["detail"]
    assert client.get("/api/macro?section=stocks&symbols=%24SPX").status_code == 400


def test_macro_endpoint_exposes_percent_references_without_converting_prices(monkeypatch):
    from backend.market_data.macro import LiveData, session_reference
    from tests.test_series_sources import offline_macro

    dax = LiveData(source="Yahoo ^GDAXI", price=25020, prev_close=25000, as_of="2026-09-25T09:05+02:00",
                   bars=[("2026-09-25T09:00+02:00", 25010)])
    dax.chart_reference = session_reference(dax, "Europe/Berlin")
    ftse = LiveData(source="Yahoo ^FTSE", price=9520, prev_close=9500, as_of="2026-09-28T08:00+01:00",
                    bars=[("2026-09-25T16:30+01:00", 9510)])
    ftse.chart_reference = session_reference(ftse, "Europe/London")
    live = {"DAX": dax, "FTSE100": ftse}
    for key, symbol, tz, offset, base in (("TAIEX", "^TWII", "Asia/Taipei", "+08:00", 25000),
                                       ("KOSPI", "^KS11", "Asia/Seoul", "+09:00", 3500)):
        quote = LiveData(source=f"Yahoo {symbol}", price=base + 20, prev_close=base,
                         as_of=f"2026-09-25T09:05{offset}", bars=[(f"2026-09-25T09:00{offset}", base + 10)])
        quote.chart_reference = session_reference(quote, tz)
        live[key] = quote
    monkeypatch.setattr(main, "macro", offline_macro(monkeypatch, live))

    response = client.get("/api/macro?section=global_equities&range=1d")
    assert response.status_code == 200
    data = response.json()
    assert data["section"]["chart_mode"] == "percent_change" and data["section"]["symbols"] is None
    assert list(data["items"]) == ["DAX", "FTSE100", "NIKKEI225", "TAIEX", "KOSPI"] and data["curve"] == {}
    assert all(item["unit"] == "index" for item in data["items"].values())
    dax_item = data["items"]["DAX"]
    assert dax_item["chart_reference"] == {
        "kind": "previous_close", "value": 25000, "session_date": "2026-09-25", "timezone": "Europe/Berlin",
    }
    assert dax_item["series"] == [{"t": "2026-09-25T09:00+02:00", "v": 25010}]
    assert dax_item["metrics"]["value"] == 25020 and dax_item["metrics"]["change"] == 20
    assert dax_item["timezone"] == "Europe/Berlin" and dax_item["live_source"] == "Yahoo ^GDAXI"
    ftse_item = data["items"]["FTSE100"]
    assert ftse_item["intraday"] is True and ftse_item["metrics"]["prev_close"] == 9500
    assert ftse_item["chart_reference"] == {"kind": "first_point_fallback", "value": None}
    nikkei = data["items"]["NIKKEI225"]
    assert nikkei["intraday"] is False and nikkei["fallback"] == "daily"
    assert nikkei["chart_reference"] == {"kind": "first_point", "value": None}
    assert nikkei["timezone"] == "Asia/Tokyo" and nikkei["metrics"]["value"] == 42000
    for key, label, tz in (("TAIEX", "Taiwan TAIEX", "Asia/Taipei"), ("KOSPI", "South Korea KOSPI", "Asia/Seoul")):
        item, quote = data["items"][key], live[key]
        assert item["label"] == label and item["timezone"] == tz and item["intraday"] is True
        assert item["chart_reference"] == {
            "kind": "previous_close", "value": quote.prev_close, "session_date": "2026-09-25", "timezone": tz,
        }
        assert item["series"] == [{"t": t, "v": v} for t, v in quote.bars]
        assert item["metrics"]["value"] == quote.price and item["metrics"]["change"] == 20
        assert item["live_source"] == quote.source and item["daily_source"] == f"stub:{key}"
    assert all("session" not in item and "chart_series" not in item for item in data["items"].values())


@pytest.mark.parametrize("key,symbol,tz,offset", [
    ("TAIEX", "^TWII", "Asia/Taipei", "+08:00"), ("KOSPI", "^KS11", "Asia/Seoul", "+09:00"),
])
@pytest.mark.parametrize("earlier_session", [False, True])
def test_macro_endpoint_asian_index_date_boundary_and_reference_fallback(monkeypatch, key, symbol, tz, offset, earlier_session):
    from tests.test_series_sources import SETTINGS, stock_provider_macro, yahoo_stock_payload

    bars = [(f"2026-09-25T00:05{offset}", 101)]
    if earlier_session:
        bars.insert(0, (f"2026-09-24T23:55{offset}", 99))
    service, calls = stock_provider_macro(monkeypatch, daily=[("2026-09-24", 100)], yahoo={
        ("1d", "5m"): yahoo_stock_payload(bars, price=102, prev_close=100, stamp=f"2026-09-25T00:05{offset}"),
    })
    service.settings = SETTINGS
    monkeypatch.setattr(main, "macro", service)
    response = client.get("/api/macro?section=global_equities&range=1d")
    assert response.status_code == 200
    entry = response.json()["items"][key]
    assert entry["unit"] == "index" and entry["timezone"] == tz and entry["intraday"] is True
    assert entry["live_source"] == f"Yahoo {symbol}" and "error" not in entry
    assert entry["series"] == [{"t": f"2026-09-25T00:05{offset}", "v": 101}]
    assert entry["metrics"]["as_of"] == f"2026-09-25T00:05{offset}" and entry["metrics"]["value"] == 102
    assert entry["metrics"]["day_low"] == entry["metrics"]["day_high"] == 101
    assert entry["chart_reference"] == (
        {"kind": "first_point_fallback", "value": None} if earlier_session else
        {"kind": "previous_close", "value": 100, "session_date": "2026-09-25", "timezone": tz}
    )
    assert "session" not in entry and "chart_series" not in entry
    daily_response = client.get("/api/macro?section=global_equities&range=1m")
    assert daily_response.status_code == 200
    daily = daily_response.json()["items"][key]
    assert daily["series"] == [{"t": "2026-09-24", "v": 100}, {"t": "2026-09-25", "v": 102}]
    assert daily["chart_reference"] == {"kind": "first_point", "value": None}
    assert calls == [("yahoo", "1d", "5m")] * 5


@pytest.mark.parametrize("key,tz,base", [("TAIEX", "Asia/Taipei", 25000), ("KOSPI", "Asia/Seoul", 3500)])
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("range_key", ["1d", "1m"])
def test_macro_endpoint_asian_indices_daily_fallback_and_unavailable(monkeypatch, key, tz, base, available, range_key):
    from tests.test_series_sources import offline_macro

    monkeypatch.setattr(main, "macro", offline_macro(monkeypatch, daily=None if available else {key: None}))
    response = client.get(f"/api/macro?section=global_equities&range={range_key}")
    assert response.status_code == 200
    data = response.json()
    assert list(data["items"]) == ["DAX", "FTSE100", "NIKKEI225", "TAIEX", "KOSPI"]
    entry = data["items"][key]
    assert entry["unit"] == "index" and entry["timezone"] == tz and entry["intraday"] is False
    assert entry["chart_reference"] == {"kind": "first_point", "value": None}
    assert entry["live_source"] is None and entry["metrics"]["live"] is False
    assert "session" not in entry and "chart_series" not in entry
    if range_key == "1d":
        assert entry["fallback"] == "daily"
    if available:
        assert entry["metrics"]["value"] == base and entry["daily_source"] == f"stub:{key}"
        assert len(entry["series"]) == 22 and entry["series"][-1] == {"t": "2026-09-22", "v": base}
        assert "error" not in entry
    else:
        assert entry["error"] == "unavailable" and entry["series"] == [] and entry["metrics"]["value"] is None
    assert all("error" not in item for other, item in data["items"].items() if other != key)


@pytest.mark.parametrize("range_key", ["1d", "3m"])
def test_macro_endpoint_gap_series_are_same_date_signed_bp_not_components(monkeypatch, range_key):
    from tests.test_series_sources import offline_macro

    daily = {}
    for tenor in ("2", "5", "10", "30"):
        daily[f"US{tenor}Y"] = [("2026-09-01", 1.0), ("2026-09-02", 0.5), ("2026-09-03", 8.0),
                               ("2026-09-04", 4.0), ("2026-09-06", 9.0)]
        daily[f"JP{tenor}Y"] = [("2026-09-01", 1.0), ("2026-09-02", 1.0), ("2026-09-04", 1.0), ("2026-09-05", 2.0)]
    monkeypatch.setattr(main, "macro", offline_macro(monkeypatch, daily=daily))
    response = client.get(f"/api/macro?section=us_jp_gaps&range={range_key}")
    assert response.status_code == 200
    data = response.json()
    assert list(data["items"]) == ["USJP2Y", "USJP5Y", "USJP10Y", "USJP30Y"]
    assert data["section"]["chart_mode"] is None and data["curve"] == {}
    for tenor in ("2", "5", "10", "30"):
        entry = data["items"][f"USJP{tenor}Y"]
        assert entry["unit"] == "bp" and "error" not in entry
        assert entry["series"] == [{"t": "2026-09-01", "v": 0.0}, {"t": "2026-09-02", "v": -50.0},
                                   {"t": "2026-09-04", "v": 300.0}]
        assert entry["metrics"]["value"] == 300.0 and entry["metrics"]["prev_close"] == -50.0
        assert entry["metrics"]["change"] == 350.0 and entry["metrics"]["as_of"] == "2026-09-04"
        assert entry["daily_source"] == f"US{tenor}Y minus JP{tenor}Y" and entry["metrics"]["live"] is False
        assert "chart_series" not in entry and "chart_reference" not in entry
        assert entry["intraday"] is False
        if range_key == "1d":
            assert entry["fallback"] == "daily"


@pytest.mark.parametrize("bp", [0.0, -50.0])
def test_macro_endpoint_gap_zero_and_negative_latest_values_are_not_unavailable(monkeypatch, bp):
    from tests.test_series_sources import offline_macro

    monkeypatch.setattr(main, "macro", offline_macro(monkeypatch, daily={
        "US2Y": [("2026-09-01", 1.0 + bp / 100)], "JP2Y": [("2026-09-01", 1.0)],
    }))
    response = client.get("/api/macro?section=us_jp_gaps&range=3m")
    assert response.status_code == 200
    entry = response.json()["items"]["USJP2Y"]
    assert entry["unit"] == "bp" and entry["series"] == [{"t": "2026-09-01", "v": bp}]
    assert entry["metrics"]["value"] == bp and "error" not in entry
    assert "chart_series" not in entry and "chart_reference" not in entry


@pytest.mark.parametrize("side", ["US", "JP"])
@pytest.mark.parametrize("missing", [None, [], [("2026-08-31", 1.0)]], ids=["missing", "empty", "no-shared-date"])
@pytest.mark.parametrize("range_key", ["1d", "3m"])
def test_macro_endpoint_gap_missing_component_is_unavailable(monkeypatch, side, missing, range_key):
    from tests.test_series_sources import offline_macro

    daily = {"US2Y": [("2026-09-01", 4.0)], "JP2Y": [("2026-09-01", 1.0)], f"{side}2Y": missing}
    monkeypatch.setattr(main, "macro", offline_macro(monkeypatch, daily=daily))
    response = client.get(f"/api/macro?section=us_jp_gaps&range={range_key}")
    assert response.status_code == 200
    items = response.json()["items"]
    entry = items["USJP2Y"]
    assert entry["unit"] == "bp" and entry["series"] == [] and entry["error"] == "unavailable"
    assert entry["metrics"]["value"] is None and entry["metrics"]["as_of"] is None
    assert "chart_series" not in entry and "chart_reference" not in entry
    assert all("error" not in item for key, item in items.items() if key != "USJP2Y")
