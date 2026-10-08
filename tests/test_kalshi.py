import pytest

from backend.config import Settings, load_settings
from backend.market_data.kalshi import KalshiClient, KalshiService

SETTINGS = load_settings()

EVENTS = {"events": [
    {"event_ticker": "KXFEDDECISION-27JAN", "title": "Fed decision in Jan 2027?", "sub_title": "On Jan 27, 2027", "strike_date": "2027-01-27T19:00:00Z", "mutually_exclusive": True},
    {"event_ticker": "KXFEDDECISION-26OCT", "title": "Fed decision in Oct 2026?", "sub_title": "On Oct 28, 2026", "strike_date": "2026-10-28T18:00:00Z", "mutually_exclusive": True},
]}
MARKETS = {"markets": [
    {"ticker": "KXFEDDECISION-26OCT-H25", "yes_sub_title": "Hike 25bps", "last_price_dollars": "0.6400", "yes_bid_dollars": "0.6300", "yes_ask_dollars": "0.6400", "previous_price_dollars": "0.6000", "volume_fp": "997530.86", "volume_24h_fp": "1200", "open_interest_fp": "534607", "status": "active"},
    {"ticker": "KXFEDDECISION-26OCT-H0", "yes_sub_title": "Fed maintains rate", "last_price_dollars": "0.3600", "yes_bid_dollars": "0.3500", "yes_ask_dollars": "0.3600", "previous_price_dollars": "0.4000", "volume_fp": "1240834", "volume_24h_fp": "900", "open_interest_fp": "700000", "status": "active"},
    {"ticker": "KXFEDDECISION-26OCT-C25", "yes_sub_title": "Cut 25bps", "last_price_dollars": "0.0100", "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0100", "volume_fp": "555153", "open_interest_fp": "1000", "status": "active"},
]}
CANDLES = {"candlesticks": [
    {"end_period_ts": 1790380800, "price": {"close_dollars": "0.6200", "mean_dollars": "0.6150"}},
    {"end_period_ts": 1790384400, "price": {"close_dollars": None, "mean_dollars": "0.6300"}},
    {"end_period_ts": 1790388000, "price": {"close_dollars": "0.6400"}},
]}


def offline_client(monkeypatch):
    client = KalshiClient()
    calls = []

    def fake_get(path, params=None, ttl=0.0):
        calls.append((path, dict(params or {})))
        if path == "events":
            return EVENTS
        if path == "markets":
            return MARKETS
        if path.endswith("/candlesticks"):
            return CANDLES
        raise AssertionError(path)

    monkeypatch.setattr(client, "_get", fake_get)
    return client, calls


def test_snapshot_picks_next_meeting_and_charts_all_outcomes(monkeypatch):
    client, calls = offline_client(monkeypatch)
    snap = KalshiService(SETTINGS, client).snapshot("1m")
    assert snap["event"]["ticker"] == "KXFEDDECISION-26OCT"  # nearest upcoming meeting, not the first listed
    assert [m["code"] for m in snap["markets"]] == ["H25", "H0", "C25"]  # sorted by probability
    hike = snap["markets"][0]
    assert hike["label"] == "Hike 25bps" and hike["last_pct"] == pytest.approx(64.0) and hike["yes_bid_pct"] == pytest.approx(63.0)
    assert hike["previous_pct"] == pytest.approx(60.0) and hike["open_interest"] == pytest.approx(534607)
    assert [p["v"] for p in hike["series"]] == [pytest.approx(62.0), pytest.approx(63.0), pytest.approx(64.0)]  # mean used when close is null
    assert hike["series"][0]["t"].startswith("2026-09-2") and snap["refresh_seconds"] == 30.0
    candle_calls = [c for c in calls if c[0].endswith("/candlesticks")]
    assert len(candle_calls) == 3 and candle_calls[0][1]["period_interval"] == 60
    assert snap["markets"][2]["previous_pct"] is None


def test_market_filter_and_explicit_event(monkeypatch):
    client, calls = offline_client(monkeypatch)
    cfg = dict(SETTINGS.section("kalshi"), markets=["H25", "maintains"], event="KXFEDDECISION-27JAN")
    service = KalshiService(Settings({"kalshi": cfg}), client)
    snap = service.snapshot("1d")
    assert snap["event"]["ticker"] == "KXFEDDECISION-27JAN"
    assert [m["code"] for m in snap["markets"]] == ["H25", "H0"]  # code match + label fragment match, cut excluded
    assert [c for c in calls if c[0].endswith("/candlesticks")][0][1]["period_interval"] == 1

    missing = KalshiService(Settings({"kalshi": dict(cfg, event="KXFEDDECISION-99XXX")}), client).snapshot("1m")
    assert missing["event"]["ticker"] == "KXFEDDECISION-26OCT"  # unknown explicit event falls back to the next one


def test_disabled_and_no_events(monkeypatch):
    client, _ = offline_client(monkeypatch)
    monkeypatch.setattr(client, "open_events", lambda series: [])
    snap = KalshiService(SETTINGS, client).snapshot("1m")
    assert snap["event"] is None and "no open event" in snap["error"] and snap["markets"] == []
    assert KalshiService(Settings({"kalshi": {"enabled": False}}), client).enabled is False
