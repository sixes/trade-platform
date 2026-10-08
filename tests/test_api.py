from fastapi.testclient import TestClient

from backend.config import load_settings
from backend.main import ScreenerFilters, app, normalize_symbols

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
        "buy_puts", "selective_put_selling", "sell_20_delta_puts",
        "close_shorts_low_delta", "avoid_naked_puts", "scale_into_put_selling",
    ]
    assert all(s["filters"].get("dte") for s in data["scenarios"])
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
    assert resp.status_code == 200 and "Put Screener" in resp.text and "Watchlist IV Rank" in resp.text
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
