import json

from backend.market_data.fear_greed import FearGreedProvider, parse_graphdata, rating_for

SAMPLE = {
    "fear_and_greed": {
        "score": 35.2285714285714, "rating": "fear", "timestamp": "2026-09-23T00:00:00+00:00",
        "previous_close": 35.2285714285714, "previous_1_week": 27.31, "previous_1_month": 54.66, "previous_1_year": 56.77,
    },
    "fear_and_greed_historical": {
        "timestamp": 1790121600000.0, "score": 35.23, "rating": "fear",
        "data": [
            {"x": 1758585600000.0, "y": 56.77, "rating": "greed"},
            {"x": 1758672000000.0, "y": 50.1, "rating": "neutral"},
            {"x": 1758672000000.0, "y": 50.1, "rating": "neutral"},  # duplicate day is dropped
            {"x": 1790121600000.0, "y": 35.23, "rating": "fear"},
        ],
    },
    "market_momentum_sp500": {"score": 40, "rating": "fear", "data": []},
    "put_call_options": {"score": 48.4, "rating": "neutral", "data": []},
    "junk_bond_demand": {"score": None, "rating": None},
}


def test_rating_bands():
    assert rating_for(10) == "extreme fear"
    assert rating_for(25) == "fear"
    assert rating_for(50) == "neutral"
    assert rating_for(60) == "greed"
    assert rating_for(90) == "extreme greed"
    assert rating_for(None) is None


def test_parse_graphdata():
    data = parse_graphdata(SAMPLE)
    assert data["score"] == 35.2 and data["rating"] == "fear"
    assert data["previous_1_week"] == 27.31
    assert [p["d"] for p in data["history"]] == ["2025-09-23", "2025-09-24", "2026-09-23"]
    assert data["history"][-1]["v"] == 35.23
    keys = [c["key"] for c in data["components"]]
    assert keys == ["market_momentum_sp500", "put_call_options"]  # missing score is skipped


def test_provider_uses_disk_cache_and_stale_fallback(tmp_path, monkeypatch):
    provider = FearGreedProvider(tmp_path, ttl_minutes=60)
    calls = {"n": 0}

    def fake_download():
        calls["n"] += 1
        if calls["n"] == 1:
            return parse_graphdata(SAMPLE)
        raise RuntimeError("418 teapot")

    monkeypatch.setattr(provider, "_download", fake_download)
    first = provider.get()
    assert first["score"] == 35.2 and provider.source == "CNN"
    assert json.loads((tmp_path / "fear_greed.json").read_text())["score"] == 35.2

    # Within TTL: served from memory, no new download.
    assert provider.get() is first and calls["n"] == 1

    # Expired TTL + failing download: stale cache is served and the error is recorded.
    provider.ttl = 0
    stale = provider.get()
    assert stale["score"] == 35.2 and provider.source == "stale-disk-cache"
    assert "418" in provider.freshness()["last_error"]
