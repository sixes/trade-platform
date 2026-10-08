import time

import pytest

from backend.config import Settings, load_settings
from backend.jobs import MarketService
from backend.market_data.indices import IndexHistoryProvider, parse_csv_series, parse_mof_jgb, parse_yahoo_chart

SETTINGS = load_settings()


def test_parse_csv_series_picks_close_or_named_or_last_column():
    ohlc = "DATE,OPEN,HIGH,LOW,CLOSE\n09/21/2026,1,2,0.5,1.5\n09/22/2026,1,2,0.5,1.7,\n"
    assert parse_csv_series(ohlc, "COR3M") == [("2026-09-21", 1.5), ("2026-09-22", 1.7)]
    named = "DATE,VVIX\n01/02/2026,80.5\n01/05/2026,.\n01/06/2026,82.0\n"
    assert parse_csv_series(named, "VVIX") == [("2026-01-02", 80.5), ("2026-01-06", 82.0)]
    fred = "observation_date,DGS2\n2026-09-21,4.70\n2026-09-22,4.71\n"
    assert parse_csv_series(fred, "DGS2") == [("2026-09-21", 4.7), ("2026-09-22", 4.71)]
    cache = "DATE,VALUE\n2026-09-22,3.0\n"
    assert parse_csv_series(cache, "VALUE") == [("2026-09-22", 3.0)]


MOF_CSV = (
    "Interest Rate (September 2026),,,,,,,,,,,,,,,(Unit : %)\r\n"
    "Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y\r\n"
    "1974/9/24,10.327,9.362,8.83,8.515,8.348,8.29,8.24,8.121,8.127,-,-,-,-,-,-\r\n"
    "2026/9/2,1.56,1.854,2.009,2.199,2.332,2.45,2.585,2.743,2.874,3.006,3.554,3.864,4.141,4.122,4.134\r\n"
    "2026/9/1,1.527,1.802,1.952,2.14,2.28,2.411,2.559,2.718,2.848,2.987,3.544,3.859,4.143,4.131,4.145\r\n"
    ",,,,,,,,,,,,,,,\r\n"
    "\"  \u203bIf you cannot download the latest csv data, please clear the browser's cache and download again.\",,,,,,,,,,,,,,,\r\n"
)


def test_parse_mof_jgb_skips_title_footer_and_missing_tenors():
    assert parse_mof_jgb(MOF_CSV, "30Y") == [("2026-09-01", 4.131), ("2026-09-02", 4.122)]
    assert parse_mof_jgb(MOF_CSV, "1y")[0] == ("1974-09-24", 10.327)
    with pytest.raises(ValueError):
        parse_mof_jgb(MOF_CSV, "50Y")
    with pytest.raises(ValueError):
        parse_mof_jgb("no header here\n1,2,3\n")


def test_mof_jgb_source_merges_history_and_current_month(tmp_path, monkeypatch):
    provider = IndexHistoryProvider(tmp_path, ttl_minutes=60)
    history = "Interest Rate,,(Unit : %)\nDate,10Y,30Y\n2026/8/28,2.93,4.084\n2026/8/31,2.943,4.092\n"
    current = MOF_CSV

    class Resp:
        def __init__(self, text): self.content = text.encode("cp932")
        def raise_for_status(self): pass

    class FakeHttp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return Resp(history if "historical" in url else current)

    monkeypatch.setattr("backend.market_data.indices.httpx.Client", FakeHttp)
    series = provider.series("JP30Y", [{"type": "mof_jgb", "tenor": "30Y", "label": "Japan MOF"}])
    assert series == [("2026-08-28", 4.084), ("2026-08-31", 4.092), ("2026-09-01", 4.131), ("2026-09-02", 4.122)]
    assert provider.source_used["jp30y"] == "Japan MOF:30Y"


def test_parse_yahoo_chart_dedups_live_bar_and_skips_nulls():
    payload = {"chart": {"result": [{
        "timestamp": [1758585600, 1758672000, 1758672000 + 3600],
        "indicators": {"quote": [{"close": [93.1, None, 101.13]}]},
    }], "error": None}}
    assert parse_yahoo_chart(payload) == [("2025-09-23", 93.1), ("2025-09-24", 101.13)]
    with pytest.raises(ValueError):
        parse_yahoo_chart({"chart": {"result": None, "error": {"code": "Not Found"}}})


def test_series_falls_back_across_sources_and_records_which_worked(tmp_path, monkeypatch):
    provider = IndexHistoryProvider(tmp_path, ttl_minutes=60)
    calls = []

    def fake_download(url, column):
        calls.append(url)
        if "DTWEXBGS" in url:
            return [("2026-09-18", 119.5)]
        raise RuntimeError("blocked")

    monkeypatch.setattr(provider, "_download", fake_download)
    monkeypatch.setattr(provider, "_download_yahoo", lambda symbol, range_="5y": (_ for _ in ()).throw(RuntimeError("yahoo down")))
    sources = [{"type": "yahoo", "symbol": "DX-Y.NYB", "label": "ICE DXY via Yahoo"},
               {"type": "fred", "id": "DTWEXBGS", "label": "Fed broad dollar index"}]
    series = provider.series("DXY", sources)
    assert series == [("2026-09-18", 119.5)]
    assert provider.source_used["dxy"] == "Fed broad dollar index:DTWEXBGS"
    assert (tmp_path / "dxy_history.src").read_text() == "Fed broad dollar index:DTWEXBGS"

    # A fresh provider reads the disk cache and still knows the original source.
    again = IndexHistoryProvider(tmp_path, ttl_minutes=60)
    assert again.series("DXY", sources) == series
    assert again.source_used["dxy"].startswith("Fed broad dollar index:DTWEXBGS")

    # Longbridge sources are skipped entirely when no fetcher is available.
    with pytest.raises(RuntimeError):
        provider.series("HYG", [{"type": "longbridge", "symbol": "HYG.US"}])
    assert provider.series("HYG", [{"type": "longbridge", "symbol": "HYG.US"}], {"longbridge": lambda s: [("2026-09-23", 78.1)]}) == [("2026-09-23", 78.1)]


class StubProvider:
    """Serves canned series for VIX/SKEW and the macro keys without touching the network."""

    def __init__(self):
        from tests.test_regime import make_series
        self.vix_series = make_series(14, 30, [19, 19])
        self.skew_series = make_series(128, 132, [130, 130])
        self.source_used = {}

    def vix(self): return self.vix_series
    def skew(self): return self.skew_series
    def try_index(self, name): return None
    def freshness(self): return {}

    def try_series(self, key, sources, fetch=None):
        curve = {"US2Y": 4.71, "US5Y": 4.83, "US10Y": 4.96, "US30Y": 5.29, "VXTLT": 16.7, "JP30Y": 4.12, "HYG": 78.1, "DXY": 101.1,
                 "GOLD_SPOT": 4320.0, "GOLD_FUTURES": 4322.0, "GOLD_ETF": 392.9, "SILVER_SPOT": 64.0, "SILVER_FUTURES": 64.6, "SILVER_ETF": 58.2,
                 "BRENT_SPOT": 114.9, "BRENT_FUTURES": 97.5, "WTI_SPOT": 96.4, "WTI_FUTURES": 91.6, "COPPER": 6.77,
                 "STOCKS_SOXL": 142.3, "STOCKS_TECL": 228.2, "STOCKS_UPRO": 148.8, "STOCKS_SPXL": 284.7, "STOCKS_NVDA": 190.5}
        if key.upper() not in curve:
            return None
        self.source_used[key.lower()] = f"stub:{key.upper()}"
        return [(f"2026-09-{d:02d}", curve[key.upper()] - 0.01 * (22 - d)) for d in range(1, 23)]


def test_market_payload_no_longer_carries_macro_but_indices_survive_unavailable_sources():
    payload = MarketService(SETTINGS, StubProvider(), None).payload()
    assert "macro" not in payload
    expected = {(str(n["key"]) if isinstance(n, dict) else str(n)).upper() for n in SETTINGS.get("indices.charts")}
    assert set(payload["indices"]) == expected and "MOVE" in expected
    assert all(v["error"] == "unavailable" for v in payload["indices"].values())


class FakeSina:
    """Offline stand-in for SinaClient: canned quotes/bars for the symbols it knows, nothing for the rest."""

    def __init__(self, quotes=None, bars=None):
        from datetime import datetime
        from backend.market_data.sina import SinaQuote, eastern
        self.quote_ttl = 4.0
        self.requests = 0
        self.batches = []
        as_of = datetime.now(eastern()).isoformat(timespec="minutes")
        self._quotes = {k: SinaQuote(k, name, last, None, None, last * 1.01, last * 0.99, last, prev, as_of)
                        for k, (name, last, prev) in (quotes or {}).items()}
        self._bars = bars or {}

    def quotes(self, symbols):
        self.batches.append([s.upper() for s in symbols])
        return {s.upper(): self._quotes[s.upper()] for s in symbols if s.upper() in self._quotes}

    def minute_bars(self, symbol, minutes=5, scale=1.0):
        return [(t, v * scale) for t, v in self._bars.get((symbol.upper(), minutes), [])]

    def daily(self, symbol, scale=1.0):
        raise RuntimeError("offline")


def offline_macro(monkeypatch, live=None, sina=None):
    """MacroService whose live fetchers are stubbed: `live` maps series key -> LiveData (or None)."""
    from backend.market_data.macro import MacroService

    service = MacroService(SETTINGS, StubProvider(), None, sina=sina or FakeSina())
    live = live or {}

    def fake_yahoo(symbol, range_key):
        for section in service.sections():
            for spec in service.specs(section["id"]):
                if any(src.get("symbol") == symbol for src in spec.get("live", [])):
                    if live.get(spec["key"]) is None:
                        raise RuntimeError("offline")
                    return live[spec["key"]]
        if live.get(symbol) is not None:  # user-added tickers are keyed by their Yahoo symbol
            return live[symbol]
        raise RuntimeError("offline")

    monkeypatch.setattr(service, "_yahoo_intraday", fake_yahoo)
    return service


def test_live_sections_are_declared_and_commodities_snapshot_works(monkeypatch):
    service = offline_macro(monkeypatch)
    sections = service.sections()
    assert [s["id"] for s in sections] == ["vix", "stocks", "macro", "commodities"]
    assert sections[0]["hidden"] is True and sections[2]["hidden"] is False
    assert sections[2]["curve"] is True and sections[3]["curve"] is False
    snap = service.snapshot("1d", "commodities")
    assert snap["section"]["title"] == "Commodities" and snap["curve"] == {}
    assert list(snap["items"]) == ["GOLD", "SILVER", "BRENT", "WTI", "COPPER"]
    gold = snap["items"]["GOLD"]
    assert gold["unit"] == "usd" and gold["metrics"]["value"] == pytest.approx(4320.0)
    assert gold["fallback"] == "daily"  # live feed offline in tests -> daily fallback
    with pytest.raises(KeyError):
        service.snapshot("1d", "nope")


def test_symbol_section_charts_default_or_requested_tickers(monkeypatch):
    from backend.market_data.macro import LiveData, MacroService

    stocks = next(s for s in offline_macro(monkeypatch).sections() if s["id"] == "stocks")
    assert stocks["symbols"] == ["SOXL.US", "TECL.US", "UPRO.US", "SPXL.US"]
    assert stocks["refresh_seconds"] == 15.0 and stocks["live_ttl_seconds"] == 15.0
    assert next(s for s in offline_macro(monkeypatch).sections() if s["id"] == "macro")["symbols"] is None

    spec = MacroService.symbol_spec("nvda")
    assert spec["key"] == "NVDA" and spec["unit"] == "usd"
    assert spec["sources"] == [{"type": "longbridge", "symbol": "NVDA.US"}, {"type": "yahoo", "symbol": "NVDA"}] and spec["live"] == spec["sources"]
    assert MacroService.symbol_spec("700.HK")["sources"] == [{"type": "longbridge", "symbol": "700.HK"}]  # no Yahoo fallback outside the US

    bars = [(f"2026-09-23T{9 + i // 12:02d}:{(i % 12) * 5:02d}-04:00", 140.0 + 0.1 * i) for i in range(30)]
    service = offline_macro(monkeypatch, {"NVDA": LiveData(source="Yahoo NVDA", price=191.2, prev_close=190.5, as_of="2026-09-23T11:25-04:00", bars=bars)})
    snap = service.snapshot("1d", "stocks")
    assert list(snap["items"]) == ["SOXL", "TECL", "UPRO", "SPXL"]
    assert snap["items"]["SOXL"]["daily_source"] == "stub:STOCKS_SOXL"  # cached under the section namespace, not "SOXL"
    assert snap["items"]["SOXL"]["metrics"]["value"] == pytest.approx(142.3) and snap["items"]["SOXL"]["fallback"] == "daily"
    assert snap["refresh_seconds"] == 15.0 and snap["live_ttl_seconds"] == 15.0

    custom = service.snapshot("1d", "stocks", symbols=["NVDA.US", "SOXL.US", "ZZZZ.US"])
    assert list(custom["items"]) == ["NVDA", "SOXL", "ZZZZ"]
    nvda = custom["items"]["NVDA"]
    assert nvda["metrics"]["live"] is True and nvda["metrics"]["value"] == 191.2 and nvda["live_source"] == "Yahoo NVDA"
    assert nvda["intraday"] is True and len(nvda["series"]) == 30 and "error" not in nvda
    assert custom["items"]["ZZZZ"]["error"] == "unavailable" and custom["items"]["ZZZZ"]["series"] == []
    assert list(service.snapshot("1d", "stocks", symbols=[])["items"]) == []


def test_variants_default_to_spot_for_oil_and_can_be_switched(monkeypatch):
    service = offline_macro(monkeypatch)
    snap = service.snapshot("3m", "commodities")
    assert snap["refresh_seconds"] == 5.0 and snap["section"]["refresh_seconds"] == 5.0
    brent = snap["items"]["BRENT"]
    assert brent["variant"] == "futures" and brent["daily_source"] == "stub:BRENT_FUTURES"
    assert [v["name"] for v in brent["variants"]] == ["futures", "spot"]
    spot = service.snapshot("3m", "commodities", {"BRENT": "spot"})["items"]["BRENT"]
    assert spot["daily_source"] == "stub:BRENT_SPOT" and spot["metrics"]["value"] == pytest.approx(114.9) and "EIA" in spot["label"]
    assert spot["live_note"].startswith("daily data only") and "lag" in spot["note"]
    assert snap["items"]["GOLD"]["variant"] == "spot" and snap["items"]["COPPER"]["variant"] is None
    assert snap["items"]["COPPER"]["variants"] == [] and "LME" in snap["items"]["COPPER"]["note"]

    switched = service.snapshot("3m", "commodities", {"brent": "futures", "GOLD": "etf", "WTI": "bogus"})
    assert switched["items"]["BRENT"]["variant"] == "futures" and switched["items"]["BRENT"]["metrics"]["value"] == pytest.approx(97.5)
    assert switched["items"]["GOLD"]["variant"] == "etf" and switched["items"]["GOLD"]["metrics"]["value"] == pytest.approx(392.9)
    assert switched["items"]["WTI"]["variant"] == "futures"  # unknown variant falls back to the default


def test_sina_live_quotes_drive_the_commodities_cards(monkeypatch):
    from datetime import datetime, timedelta
    from backend.market_data.sina import eastern, session_start

    start = session_start(datetime.now(eastern()))
    t1, t2 = (start + timedelta(minutes=5)).isoformat(timespec="minutes"), (start + timedelta(minutes=10)).isoformat(timespec="minutes")
    sina = FakeSina(
        quotes={"XAU": ("伦敦金", 4283.13, 4287.28), "XAG": ("伦敦银", 64.17, 64.42), "OIL": ("布伦特原油", 99.69, 98.12),
                "CL": ("纽约原油", 93.63, 92.16), "HG": ("美铜", 677.36, 675.35)},
        bars={("XAU", 5): [(t1, 4290.0), (t2, 4284.0)], ("HG", 5): [(t2, 677.0)]},
    )
    service = offline_macro(monkeypatch, sina=sina)
    snap = service.snapshot("1d", "commodities")
    gold = snap["items"]["GOLD"]
    assert gold["variant"] == "spot" and gold["metrics"]["live"] is True and gold["metrics"]["value"] == 4283.13
    assert gold["metrics"]["change"] == pytest.approx(4283.13 - 4287.28) and gold["live_source"] == "Sina hf_XAU (伦敦金)"
    assert gold["intraday"] is True and gold["series"][-1]["v"] == 4283.13 and len(gold["series"]) == 3  # live tick appended
    assert gold["metrics"]["day_high"] == pytest.approx(4283.13 * 1.01)
    copper = snap["items"]["COPPER"]
    assert copper["metrics"]["value"] == pytest.approx(6.7736) and copper["series"][0]["v"] == pytest.approx(6.77)  # cents -> $/lb
    assert snap["items"]["BRENT"]["metrics"]["value"] == 99.69 and snap["items"]["WTI"]["metrics"]["value"] == 93.63
    # All Sina symbols of the section were prefetched in one batch before the per-card lookups.
    assert sina.batches[0] == ["XAU", "XAG", "OIL", "CL", "HG"]


def test_sina_1d_falls_back_to_last_session_when_market_is_closed(monkeypatch):
    from datetime import datetime, timedelta
    from backend.market_data.sina import eastern, session_start

    # Bars only exist in the previous session (e.g. it is Saturday): the 1D chart must still show that session.
    prev_start = session_start(datetime.now(eastern())) - timedelta(days=1)
    bars = [((prev_start + timedelta(minutes=5 * i)).isoformat(timespec="minutes"), 4300.0 + i) for i in range(1, 40)]
    sina = FakeSina(quotes={"XAU": ("伦敦金", 4340.0, 4330.0)}, bars={("XAU", 5): bars})
    service = offline_macro(monkeypatch, sina=sina)
    gold = service.snapshot("1d", "commodities")["items"]["GOLD"]
    assert gold["intraday"] is True
    assert len(gold["series"]) == len(bars) + 1  # previous session's bars plus the live tick
    assert gold["series"][0]["v"] == 4301.0 and gold["series"][-1]["v"] == 4340.0


def test_macro_snapshot_daily_ranges_and_curve_without_live_feeds(monkeypatch):
    service = offline_macro(monkeypatch)
    snap = service.snapshot("3m")
    items = snap["items"]
    assert list(items) == ["US2Y", "US5Y", "US10Y", "US30Y", "VXTLT", "JP30Y", "HYG", "DXY"]
    assert items["US2Y"]["unit"] == "pct" and items["US2Y"]["metrics"]["value"] == pytest.approx(4.71)
    assert items["US2Y"]["live_note"].startswith("daily data only")
    assert items["US10Y"]["metrics"]["live"] is False and items["US10Y"]["live_note"] == "no live feed reachable"
    assert items["HYG"]["daily_source"] == "stub:HYG"
    assert items["VXTLT"]["unit"] == "index" and items["VXTLT"]["metrics"]["value"] == pytest.approx(16.7) and "TLT" in items["VXTLT"]["note"]
    assert items["JP30Y"]["unit"] == "pct" and items["JP30Y"]["daily_source"] == "stub:JP30Y" and items["JP30Y"]["live_note"].startswith("daily data only")
    assert len(items["US10Y"]["series"]) == 22 and items["US10Y"]["series"][-1]["t"] == "2026-09-22"
    assert snap["curve"]["2s10s"]["bp"] == pytest.approx(25.0) and snap["curve"]["2s10s"]["state"] == "normal"
    assert snap["range"] == "3m" and "1d" in snap["ranges"]

    intraday = service.snapshot("1d")
    for key in ("US10Y", "DXY"):
        entry = intraday["items"][key]
        assert entry["intraday"] is False and entry["fallback"] == "daily" and len(entry["series"]) == 22


def test_macro_snapshot_uses_live_bars_and_appends_live_point(monkeypatch):
    from backend.market_data.macro import LiveData

    bars = [(f"2026-09-23T{9 + i // 12:02d}:{(i % 12) * 5:02d}-04:00", 5.0 + 0.001 * i) for i in range(78)]
    live = {"US10Y": LiveData(source="Yahoo ^TNX", price=5.114, prev_close=4.968, as_of="2026-09-23T15:00-04:00",
                              bars=bars, day_high=5.12, day_low=4.99)}
    service = offline_macro(monkeypatch, live)

    snap = service.snapshot("1d")
    entry = snap["items"]["US10Y"]
    assert entry["intraday"] is True and entry["live_source"] == "Yahoo ^TNX"
    assert len(entry["series"]) == 78 and entry["series"][0]["t"] == "2026-09-23T09:00-04:00"
    m = entry["metrics"]
    assert m["live"] is True and m["value"] == 5.114 and m["change"] == pytest.approx(0.146) and m["day_high"] == 5.12
    assert snap["curve"]["2s10s"]["bp"] == pytest.approx((5.114 - 4.71) * 100)

    daily = service.snapshot("1m")["items"]["US10Y"]
    assert daily["intraday"] is False
    assert daily["series"][-1] == {"t": "2026-09-23", "v": 5.114}  # live point appended after FRED's 09-22 close
    assert daily["series"][-2]["t"] == "2026-09-22"
    assert daily["metrics"]["high_52w"] >= 5.114

    # The live fetch is cached: a second snapshot within the TTL does not call the source again.
    calls = {"n": 0}
    original = service._yahoo_intraday
    monkeypatch.setattr(service, "_yahoo_intraday", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or original(*a, **k))
    service.snapshot("1d")
    assert calls["n"] == 0
