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
        curve = {"US2Y": 4.71, "US5Y": 4.83, "US10Y": 4.96, "US30Y": 5.29, "VXTLT": 16.7,
                 "JP2Y": 1.02, "JP5Y": 1.35, "JP10Y": 1.76, "JP30Y": 4.12, "HYG": 78.1, "DXY": 101.1,
                 "USDJPY": 152.3, "JYVIX_PROXY": 28.7, "JPVL_PROXY": 24.2, "G5_COLV_PROXY": 17.4,
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

    def fake_yahoo(symbol, range_key, **kwargs):
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
    assert [s["id"] for s in sections] == ["vix", "stocks", "macro", "japan_yields", "japan_fx", "us_jp_gaps", "commodities"]
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

    bars = [(f"2026-09-23T{9 + i // 12:02d}:{(i % 12) * 5:02d}-04:00", 140.0 + 0.1 * i) for i in range(6, 36)]
    service = offline_macro(monkeypatch, {"NVDA": LiveData(source="Yahoo NVDA", price=191.2, prev_close=190.5, as_of="2026-09-23T11:55-04:00", bars=bars)})
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


def session_macro(monkeypatch, bars):
    """Snapshot-level fixture: no provider I/O, and a quote date deliberately unrelated to the bars."""
    from backend.market_data.macro import LiveData, latest_stock_session

    service = offline_macro(monkeypatch)
    live = LiveData(source="fixture", price=191.2, prev_close=190.5,
                    as_of="2030-01-02T12:00-05:00", bars=bars)
    monkeypatch.setattr(service, "_live", lambda *args, **kwargs: latest_stock_session(live) if kwargs.get("stock_1d") else live)
    return service, live


@pytest.mark.parametrize("day,offset,utc_hour", [
    ("2026-07-10", "-04:00", "13"),  # EDT
    ("2026-01-09", "-05:00", "14"),  # EST
])
def test_stock_session_uses_bar_date_and_eastern_dst(monkeypatch, day, offset, utc_hour):
    # A single real point stays a single point: session metadata is not price padding.
    bars = [(f"{day}T{utc_hour}:35:00Z", 191.0)]
    service, _ = session_macro(monkeypatch, bars)
    entry = service.snapshot("1d", "stocks", symbols=["NVDA.US"])["items"]["NVDA"]
    assert entry["intraday"] is True and "fallback" not in entry
    assert entry["series"] == [{"t": bars[0][0], "v": 191.0}]
    assert entry["session"] == {
        "date": day,
        "start": f"{day}T09:30{offset}",
        "end": f"{day}T16:00{offset}",
        "timezone": "America/New_York",
    }
    assert entry["chart_source"] == "fixture"
    assert entry["metrics"]["day_high"] == entry["metrics"]["day_low"] == 191.0
    assert "session_note" not in entry


def test_stock_session_last_session_date_is_not_quote_date_or_timestamp_date_prefix(monkeypatch):
    # Saturday in this input's offset is still the last Friday session in New York.
    bars = [("2026-09-26T01:30+08:00", 191.0), ("2026-09-26T03:55+08:00", 191.2)]
    service, live = session_macro(monkeypatch, bars)
    live.as_of = "2026-09-28T08:00-04:00"  # Monday pre-open quote, Friday bars
    entry = service.snapshot("1d", "stocks", symbols=["NVDA.US"])["items"]["NVDA"]
    assert entry["session"]["start"] == "2026-09-25T09:30-04:00"
    assert entry["session"]["end"] == "2026-09-25T16:00-04:00"
    assert entry["metrics"]["as_of"] == live.as_of
    assert entry["series"] == [{"t": t, "v": v} for t, v in bars]


def test_stock_session_is_refreshed_and_absent_on_other_ranges_and_daily_fallback(monkeypatch):
    service, live = session_macro(monkeypatch, [("2026-10-30T09:35-04:00", 191.0)])

    def stock(range_key="1d"):
        return service.snapshot(range_key, "stocks", symbols=["NVDA.US"])["items"]["NVDA"]

    assert stock()["session"]["start"] == "2026-10-30T09:30-04:00"
    for range_key in ("5d", "1m", "1y"):
        assert "session" not in stock(range_key)
        assert "session_note" not in stock(range_key)
    live.bars = [("2026-11-02T09:40-05:00", 192.0)]  # next session, after DST ends
    assert stock()["session"]["start"] == "2026-11-02T09:30-05:00"
    assert stock()["session"]["end"] == "2026-11-02T16:00-05:00"
    live.bars = []
    fallback = stock()
    assert fallback["intraday"] is False and fallback["fallback"] == "daily"
    assert len(fallback["series"]) == 22
    assert "session" not in fallback and "session_note" not in fallback


def test_session_metadata_excludes_non_us_symbols(monkeypatch):
    service, _ = session_macro(monkeypatch, [("2026-09-25T10:00-04:00", 191.0)])
    items = service.snapshot("1d", "stocks", symbols=["700.HK", "NVDA.US"])["items"]
    assert items["700.HK"]["intraday"] is True
    assert "session" not in items["700.HK"] and "session_note" not in items["700.HK"]
    assert items["NVDA"]["session"]["timezone"] == "America/New_York"


@pytest.mark.parametrize("section,key", [("vix", "VIX"), ("macro", "HYG"), ("commodities", "GOLD"), ("us_jp_gaps", "USJP2Y")])
def test_session_metadata_excludes_non_symbol_sections_and_overlays(monkeypatch, section, key):
    service, _ = session_macro(monkeypatch, [("2026-09-25T10:00-04:00", 191.0)])
    entry = service.snapshot("1d", section)["items"][key]
    assert "session" not in entry and "session_note" not in entry


@pytest.mark.parametrize("bad_bar", [
    ("not a timestamp", 191.0),
    ("2026-02-30T10:00-05:00", 191.0),
    ("2026-09-25T10:00", 191.0),  # naive time must not acquire the server timezone
    ("2026-09-25", 191.0),
    (None, 191.0),
    ("2026-09-25T10:00-04:00", None),
    ("2026-09-25T10:00-04:00", float("nan")),
    ("2026-09-25T10:00-04:00", float("inf")),
])
def test_stock_session_invalid_bars_are_unavailable_not_guessed(monkeypatch, bad_bar):
    service, _ = session_macro(monkeypatch, [("2026-09-25T09:35-04:00", 190.0), bad_bar])
    entry = service.snapshot("1d", "stocks", symbols=["NVDA.US"])["items"]["NVDA"]
    assert entry["intraday"] is False and entry["fallback"] == "daily"
    assert "session" not in entry and entry["chart_source"] is None
    assert entry["metrics"]["value"] == 191.2 and entry["metrics"]["day_high"] is None


def test_stock_session_requires_real_eastern_timezone(monkeypatch):
    from datetime import timezone

    service, _ = session_macro(monkeypatch, [("2026-09-25T14:00+00:00", 191.0)])
    monkeypatch.setattr("backend.market_data.macro.eastern", lambda: timezone.utc)
    entry = service.snapshot("1d", "stocks", symbols=["NVDA.US"])["items"]["NVDA"]
    assert entry["fallback"] == "daily" and entry["chart_source"] is None
    assert "session" not in entry


def test_stock_session_rejects_longbridge_naive_timestamp_provenance(monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace

    service, _ = session_macro(monkeypatch, [])
    service.client = SimpleNamespace(
        intraday_bars=lambda *args: [(datetime(2026, 9, 25, 10, 0), 191.0)],
        spot=lambda symbol: {"last": 191.0, "timestamp": "2026-09-25T10:00-04:00"},
    )
    legacy = service._longbridge_intraday("NVDA.US", "1d")
    assert legacy.bars and legacy.bars_timezone_known is False
    live = service._longbridge_intraday("NVDA.US", "1d", stock_1d=True)
    assert live.bars == [] and live.bars_timezone_known is False
    assert live.price == 191.0
    monkeypatch.setattr(service, "_live", lambda *args, **kwargs: live)
    entry = service.snapshot("1d", "stocks", symbols=["NVDA.US"])["items"]["NVDA"]
    assert entry["fallback"] == "daily" and entry["chart_source"] is None
    assert "session" not in entry


def test_stock_session_empty_data_and_missing_feed_have_no_bounds(monkeypatch):
    service, live = session_macro(monkeypatch, [])
    monkeypatch.setattr(service.indices, "try_series", lambda *args, **kwargs: [])
    live.price = None
    for feed in (live, None):
        monkeypatch.setattr(service, "_live", lambda *args, **kwargs: feed)
        entry = service.snapshot("1d", "stocks", symbols=["NVDA.US"])["items"]["NVDA"]
        assert entry["series"] == [] and entry["error"] == "unavailable"
        assert entry["intraday"] is False and entry["fallback"] == "daily"
        assert "session" not in entry and "session_note" not in entry


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
    assert list(items) == ["US2Y", "US5Y", "US10Y", "US30Y", "VXTLT", "HYG", "DXY"]
    assert items["US2Y"]["unit"] == "pct" and items["US2Y"]["metrics"]["value"] == pytest.approx(4.71)
    assert items["US2Y"]["live_note"].startswith("daily data only")
    assert items["US10Y"]["metrics"]["live"] is False and items["US10Y"]["live_note"] == "no live feed reachable"
    assert items["HYG"]["daily_source"] == "stub:HYG"
    assert items["VXTLT"]["unit"] == "index" and items["VXTLT"]["metrics"]["value"] == pytest.approx(16.7) and "TLT" in items["VXTLT"]["note"]
    assert len(items["US10Y"]["series"]) == 22 and items["US10Y"]["series"][-1]["t"] == "2026-09-22"
    assert snap["curve"]["2s10s"]["bp"] == pytest.approx(25.0) and snap["curve"]["2s10s"]["state"] == "normal"
    assert snap["range"] == "3m" and "1d" in snap["ranges"]

    intraday = service.snapshot("1d")
    for key in ("US10Y", "DXY"):
        entry = intraday["items"][key]
        assert entry["intraday"] is False and entry["fallback"] == "daily" and len(entry["series"]) == 22


def test_japan_fx_and_us_japan_gap_sections(monkeypatch):
    service = offline_macro(monkeypatch)

    yields = service.snapshot("3m", "japan_yields")
    assert list(yields["items"]) == ["JP2Y", "JP5Y", "JP10Y", "JP30Y"]
    assert all(item["unit"] == "pct" and item["live_note"].startswith("daily data only") for item in yields["items"].values())

    japan = service.snapshot("3m", "japan_fx")
    assert list(japan["items"]) == ["USDJPY", "JYVIX_PROXY", "JPVL_PROXY", "G5_COLV_PROXY"]
    assert japan["items"]["USDJPY"]["unit"] == "fx"
    assert japan["items"]["JYVIX_PROXY"]["daily_source"] == "stub:JYVIX_PROXY"
    assert "not the Cboe/CME FX Yen" in japan["items"]["JYVIX_PROXY"]["note"]
    assert japan["items"]["JPVL_PROXY"]["unit"] == "jpy"
    assert "1577.T" in japan["items"]["JPVL_PROXY"]["note"] and "not the proprietary JPVL" in japan["items"]["JPVL_PROXY"]["note"]
    assert "not the proprietary G5 COLV" in japan["items"]["G5_COLV_PROXY"]["note"]

    gaps = service.snapshot("3m", "us_jp_gaps")
    assert list(gaps["items"]) == ["USJP2Y", "USJP5Y", "USJP10Y", "USJP30Y"]
    assert gaps["items"]["USJP2Y"]["unit"] == "bp"
    assert gaps["items"]["USJP2Y"]["metrics"]["value"] == pytest.approx((4.71 - 1.02) * 100)
    assert gaps["items"]["USJP10Y"]["daily_source"] == "US10Y minus JP10Y"
    for tenor in ("2", "5", "10", "30"):
        components = gaps["items"][f"USJP{tenor}Y"]["chart_series"]
        assert [component["key"] for component in components] == [f"US{tenor}Y", f"JP{tenor}Y"]
        assert all(component["unit"] == "pct" and len(component["series"]) == 22 for component in components)
    assert "chart_series" not in japan["items"]["USDJPY"]
    assert all(item["live_note"].startswith("daily data only") for item in gaps["items"].values())


def test_derived_gap_uses_only_shared_dates(monkeypatch):
    service = offline_macro(monkeypatch)
    original = service.indices.try_series

    def uneven(key, sources, fetch=None):
        if key == "US2Y":
            return [("2026-09-01", 4.0), ("2026-09-02", 4.1), ("2026-09-03", 4.2)]
        if key == "JP2Y":
            return [("2026-09-01", 1.0), ("2026-09-03", 1.1)]
        return original(key, sources, fetch)

    monkeypatch.setattr(service.indices, "try_series", uneven)
    entry = service.snapshot("3m", "us_jp_gaps")["items"]["USJP2Y"]
    assert entry["series"] == [
        {"t": "2026-09-01", "v": pytest.approx(300.0)},
        {"t": "2026-09-03", "v": pytest.approx(310.0)},
    ]
    assert entry["metrics"]["as_of"] == "2026-09-03"
    assert entry["chart_series"][0]["series"] == [
        {"t": "2026-09-01", "v": 4.0},
        {"t": "2026-09-02", "v": 4.1},
        {"t": "2026-09-03", "v": 4.2},
    ]
    assert entry["chart_series"][1]["series"] == [
        {"t": "2026-09-01", "v": 1.0},
        {"t": "2026-09-03", "v": 1.1},
    ]


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


def stock_provider_macro(monkeypatch, *, lb_bars=(), lb_quote=None, yahoo=None, daily=None):
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse
    from backend.market_data.macro import MacroService

    calls = []
    yahoo = yahoo if yahoo is not None else {}
    settings = Settings({"live_sections": [{"id": "stocks", "symbols": ["NVDA.US"], "live_ttl_seconds": 15}]})
    monkeypatch.setattr(Settings, "longbridge_credentials_present", property(lambda self: True))

    def outcome(value):
        if isinstance(value, Exception):
            raise value
        return value

    def bars(symbol, period, count):
        calls.append(("longbridge", "bars", symbol, period, count))
        return outcome(lb_bars)

    def spot(symbol):
        calls.append(("longbridge", "quote", symbol))
        return outcome(lb_quote) or {}

    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    class Http:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def get(self, url):
            params = parse_qs(urlparse(url).query)
            request = (params["range"][0], params["interval"][0])
            assert params["includePrePost"] == ["false"]
            calls.append(("yahoo", *request))
            return Response(outcome(yahoo.get(request, RuntimeError("offline Yahoo"))))

    monkeypatch.setattr("backend.market_data.macro.httpx.Client", Http)
    provider = StubProvider()
    monkeypatch.setattr(provider, "try_series", lambda *args, **kwargs: (
        [("2026-09-23", 188.0), ("2026-09-24", 190.0)] if daily is None else daily
    ))
    service = MacroService(settings, provider, SimpleNamespace(intraday_bars=bars, spot=spot), sina=FakeSina())
    return service, calls


def yahoo_stock_payload(bars=(), *, price=191.2, prev_close=190.5, stamp="2026-09-28T08:00-04:00"):
    from datetime import datetime

    return {"chart": {"result": [{
        "meta": {"regularMarketPrice": price, "chartPreviousClose": prev_close,
                 "regularMarketTime": int(datetime.fromisoformat(stamp).timestamp())},
        "timestamp": [int(datetime.fromisoformat(t).timestamp()) for t, _ in bars],
        "indicators": {"quote": [{"close": [v for _, v in bars]}]},
    }]}}


@pytest.mark.parametrize("quote_date,bars,day,offset,values", [
    ("2026-09-26", [("2026-09-25T15:55-04:00", 192), ("2026-09-25T09:30-04:00", 191)],
     "2026-09-25", "-04:00", [191, 192]),
    ("2026-09-27", [("2026-09-25T09:30-04:00", 191)], "2026-09-25", "-04:00", [191]),
    ("2026-07-03", [("2026-07-02T10:00-04:00", 191)], "2026-07-02", "-04:00", [191]),
    ("2026-09-28", [("2026-09-24T10:00-04:00", 180), ("2026-09-25T09:30-04:00", 191),
                    ("2026-09-25T16:00-04:00", 192), ("2026-09-25T16:00:30-04:00", 900),
                    ("2026-09-26T10:00-04:00", 901), ("2026-09-27T10:00-04:00", 902),
                    ("2026-09-28T09:29-04:00", 903)], "2026-09-25", "-04:00", [191, 192]),
    ("2026-09-28", [("2026-09-25T15:55-04:00", 192), ("2026-09-28T09:29-04:00", 903),
                    ("2026-09-28T09:30-04:00", 193)], "2026-09-28", "-04:00", [193]),
    ("2026-11-02", [("2026-10-30T15:55-04:00", 192), ("2026-11-02T08:00-05:00", 900)],
     "2026-10-30", "-04:00", [192]),
    ("2026-11-02", [("2026-10-30T15:55-04:00", 192), ("2026-11-02T09:30-05:00", 193)],
     "2026-11-02", "-05:00", [193]),
    ("2026-11-28", [("2026-11-27T09:30-05:00", 191), ("2026-11-27T13:00-05:00", 192)],
     "2026-11-27", "-05:00", [191, 192]),
    ("2026-09-26", [("2026-09-26T01:30+08:00", 191)], "2026-09-25", "-04:00", [191]),
])
def test_stock_provider_selects_latest_regular_session_before_trimming(monkeypatch, quote_date, bars, day, offset, values):
    from datetime import datetime
    from backend.market_data.macro import eastern

    quote_time = datetime.fromisoformat(f"{quote_date}T08:00").replace(tzinfo=eastern()).isoformat(timespec="minutes")
    service, calls = stock_provider_macro(
        monkeypatch, lb_bars=[(datetime.fromisoformat(t), v) for t, v in bars],
        lb_quote={"last": 195, "prev_close": 194, "timestamp": quote_time},
        yahoo={("1d", "5m"): yahoo_stock_payload([("2026-11-30T10:00-05:00", 999)])},
    )
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert entry["intraday"] is True and "fallback" not in entry
    assert [p["v"] for p in entry["series"]] == values
    assert entry["session"] == {"date": day, "start": f"{day}T09:30{offset}",
                                "end": f"{day}T16:00{offset}", "timezone": "America/New_York"}
    assert entry["live_source"] == entry["chart_source"] == "Longbridge NVDA.US"
    assert entry["metrics"]["as_of"] == quote_time and entry["metrics"]["value"] == 195
    assert entry["metrics"]["change"] == 1
    assert entry["metrics"]["day_high"] == max(values) and entry["metrics"]["day_low"] == min(values)
    assert calls == [("longbridge", "bars", "NVDA.US", "Min_5", 160), ("longbridge", "quote", "NVDA.US")]


@pytest.mark.parametrize("daily", [None, []])
@pytest.mark.parametrize("quote", [
    RuntimeError("quote down"),
    {"last": 195, "prev_close": 194, "timestamp": "2026-09-25T16:00"},
    {"last": 195, "prev_close": 194, "timestamp": "broken"},
    {"last": float("inf"), "prev_close": 194, "timestamp": "2026-09-25T16:00-04:00"},
])
def test_stock_longbridge_quote_failure_retains_usable_history(monkeypatch, quote, daily):
    service, calls = stock_provider_macro(monkeypatch, lb_bars=[("2026-09-25T10:00-04:00", 191)],
                                         lb_quote=quote, daily=daily)
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert entry["session"]["date"] == "2026-09-25" and entry["series"][0]["v"] == 191
    assert "error" not in entry
    assert entry["metrics"]["live"] is False and entry["metrics"]["day_high"] == 191
    assert entry["live_source"] is None and entry["chart_source"] == "Longbridge NVDA.US"
    if daily == []:
        assert entry["metrics"]["value"] is None and entry["metrics"]["change"] is None
    assert len(calls) == 3 and calls[-1] == ("yahoo", "1d", "5m")


def test_stock_fallback_quote_preserves_primary_chart(monkeypatch):
    service, calls = stock_provider_macro(
        monkeypatch, lb_bars=[("2026-09-25T10:00-04:00", 191)], lb_quote=RuntimeError("quote down"),
        yahoo={("1d", "5m"): yahoo_stock_payload([("2026-09-28T09:30-04:00", 999)])},
    )
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert entry["session"]["date"] == "2026-09-25" and entry["series"][0]["v"] == 191
    assert entry["live_source"] == "Yahoo NVDA" and entry["chart_source"] == "Longbridge NVDA.US"
    assert entry["metrics"]["value"] == 191.2 and entry["metrics"]["prev_close"] == 190.5
    assert entry["metrics"]["change"] == pytest.approx(0.7)
    assert entry["metrics"]["as_of"] == "2026-09-28T08:00-04:00"
    assert entry["metrics"]["day_low"] == entry["metrics"]["day_high"] == 191
    assert len(calls) == 3 and calls[-1] == ("yahoo", "1d", "5m")


@pytest.mark.parametrize("lb_bars", [
    [], RuntimeError("history down"), [("2026-09-28T08:00-04:00", 900)],
    [("2026-09-25T10:00", 900)], [("broken", 900)], [("2026-09-25T10:00-04:00", float("nan"))],
])
def test_stock_recovers_yahoo_lookback_without_replacing_primary_quote(monkeypatch, lb_bars):
    service, calls = stock_provider_macro(
        monkeypatch, lb_bars=lb_bars,
        lb_quote={"last": 195, "prev_close": 194, "timestamp": "2026-09-28T08:00-04:00"},
        yahoo={
            ("1d", "5m"): yahoo_stock_payload([("2026-09-28T08:00-04:00", 900)], price=999, prev_close=100),
            ("5d", "5m"): yahoo_stock_payload([
                ("2026-09-24T10:00-04:00", 100), ("2026-09-25T15:55-04:00", 193),
                ("2026-09-25T09:30-04:00", 191), ("2026-09-26T10:00-04:00", 900),
            ], price=1000, prev_close=50, stamp="2026-09-25T16:00-04:00"),
        },
    )
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert [p["v"] for p in entry["series"]] == [191, 193]
    assert entry["session"]["date"] == "2026-09-25"
    assert entry["chart_source"] == "Yahoo NVDA" and entry["live_source"] == "Longbridge NVDA.US"
    metrics = entry["metrics"]
    assert (metrics["value"], metrics["prev_close"], metrics["change"]) == (195, 194, 1)
    assert metrics["change_pct"] == pytest.approx(100 / 194)
    assert metrics["as_of"] == "2026-09-28T08:00-04:00"
    assert (metrics["day_low"], metrics["day_high"]) == (191, 193)
    cached = service._live_cache[("STOCKS_NVDA", "stock_1d")][1]
    assert cached.bars_timezone_known is True
    assert calls[-2:] == [("yahoo", "1d", "5m"), ("yahoo", "5d", "5m")]
    before = list(calls)
    assert service.snapshot("1d", "stocks")["items"]["NVDA"] == entry
    assert calls == before


@pytest.mark.parametrize("primary", ["longbridge", "yahoo"])
def test_stock_usable_initial_history_stops_at_configured_provider(monkeypatch, primary):
    service, calls = stock_provider_macro(
        monkeypatch, lb_quote={"last": 195, "prev_close": 194, "timestamp": "2026-09-28T08:00-04:00"},
        yahoo={("1d", "5m"): yahoo_stock_payload([("2026-09-25T10:00-04:00", 191)])},
    )
    spec = service.symbol_spec("NVDA")
    if primary == "yahoo":
        spec["live"].reverse()
    live = service._live("STOCKS_NVDA", spec, "1d", stock_1d=True)
    assert live.chart_source == "Yahoo NVDA" and len(live.bars) == 1
    assert live.price == (195 if primary == "longbridge" else 191.2)
    assert calls[-1] == ("yahoo", "1d", "5m")
    assert len(calls) == (3 if primary == "longbridge" else 1)


@pytest.mark.parametrize("lookback_ok", [True, False])
@pytest.mark.parametrize("malformed_history", [True, False])
def test_stock_yahoo_initial_quote_survives_empty_or_bad_history_and_lookback_failure(monkeypatch, lookback_ok, malformed_history):
    initial = yahoo_stock_payload()
    if malformed_history:
        initial["chart"]["result"][0]["timestamp"] = ["naive-or-broken"]
        initial["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [191]
    service, calls = stock_provider_macro(
        monkeypatch, lb_bars=RuntimeError("LB history down"), lb_quote=RuntimeError("LB quote down"),
        yahoo={
            ("1d", "5m"): initial,
            ("5d", "5m"): (yahoo_stock_payload([("2026-09-25T10:00-04:00", 192)], price=999, prev_close=50)
                           if lookback_ok else RuntimeError("lookback down")),
        },
    )
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert entry["live_source"] == "Yahoo NVDA"
    assert entry["metrics"]["value"] == 191.2 and entry["metrics"]["prev_close"] == 190.5
    assert entry["metrics"]["change"] == pytest.approx(0.7)
    assert entry["metrics"]["as_of"] == "2026-09-28T08:00-04:00"
    assert entry["intraday"] is lookback_ok
    if lookback_ok:
        assert entry["chart_source"] == "Yahoo NVDA" and entry["session"]["date"] == "2026-09-25"
    else:
        assert entry["fallback"] == "daily" and entry["chart_source"] is None and "session" not in entry
    assert calls[-2:] == [("yahoo", "1d", "5m"), ("yahoo", "5d", "5m")]


@pytest.mark.parametrize("initial", [RuntimeError("initial request down"), yahoo_stock_payload(price=None)])
def test_stock_widened_history_never_supplies_quote_or_multiday_comparison(monkeypatch, initial):
    service, calls = stock_provider_macro(monkeypatch, yahoo={
        ("1d", "5m"): initial,
        ("5d", "5m"): yahoo_stock_payload([("2026-09-25T10:00-04:00", 192)], price=999, prev_close=50),
    })
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert entry["session"]["date"] == "2026-09-25" and entry["chart_source"] == "Yahoo NVDA"
    assert entry["live_source"] is None
    metrics = entry["metrics"]
    assert (metrics["value"], metrics["prev_close"], metrics["change"]) == (190, 188, 2)
    assert metrics["live"] is False and metrics["as_of"] == "2026-09-24"
    assert metrics["day_high"] == metrics["day_low"] == 192
    assert len(calls) == 4


@pytest.mark.parametrize("daily", [[], [("2026-09-24", 190)]])
def test_stock_no_history_keeps_quote_and_only_labeled_daily_fallback(monkeypatch, daily):
    service, calls = stock_provider_macro(
        monkeypatch, daily=daily, lb_bars=RuntimeError("no bars"),
        lb_quote={"last": 195, "prev_close": 194, "timestamp": "2026-09-28T08:00-04:00"},
    )
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert entry["series"] == [{"t": t, "v": v} for t, v in daily]
    assert entry["fallback"] == "daily" and entry["intraday"] is False
    assert "session" not in entry and entry["chart_source"] is None
    assert entry["metrics"]["value"] == 195 and entry["metrics"]["change"] == 1
    assert entry["metrics"]["day_high"] is None and entry["metrics"]["day_low"] is None
    assert "error" not in entry and len(calls) == 4


@pytest.mark.parametrize("scoped_first", [True, False])
def test_stock_recovery_cache_isolated_from_5d_daily_and_live_value(monkeypatch, scoped_first):
    service, calls = stock_provider_macro(
        monkeypatch, lb_quote={"last": 195, "prev_close": 194, "timestamp": "2026-09-28T08:00-04:00"},
        yahoo={("1d", "5m"): yahoo_stock_payload(),
               ("5d", "5m"): yahoo_stock_payload([("2026-09-25T10:00-04:00", 192)], prev_close=50)},
    )

    def ordinary():
        for range_key in ("5d", "1m"):
            entry = service.snapshot(range_key, "stocks")["items"]["NVDA"]
            assert "session" not in entry and "chart_source" not in entry
            assert entry["metrics"]["value"] == 195 and entry["metrics"]["change"] == 1
            assert entry["intraday"] is False
        assert service.live_value("stocks", "NVDA") == ("2026-09-28T08:00-04:00", 195)

    if not scoped_first:
        ordinary()
        assert len(calls) == 4
    recovered = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert recovered["session"]["date"] == "2026-09-25" and recovered["chart_source"] == "Yahoo NVDA"
    ordinary()
    assert set(service._live_cache) == {("STOCKS_NVDA", key) for key in ("stock_1d", "1d", "5d")}
    assert len(calls) == 8
    assert service.snapshot("1d", "stocks")["items"]["NVDA"] == recovered
    ordinary()
    assert len(calls) == 8


@pytest.mark.parametrize("initially_available", [True, False])
def test_stock_recovery_positive_and_negative_caches_expire(monkeypatch, initially_available):
    clock = [1000.0]
    monkeypatch.setattr("backend.market_data.macro.time.time", lambda: clock[0])
    yahoo = ({("1d", "5m"): yahoo_stock_payload([("2026-09-25T10:00-04:00", 191)])}
             if initially_available else {})
    service, calls = stock_provider_macro(monkeypatch, yahoo=yahoo, daily=[])
    initial = service.snapshot("1d", "stocks")["items"]["NVDA"]
    if initially_available:
        assert initial["session"]["date"] == "2026-09-25"
    else:
        assert initial["error"] == "unavailable" and initial["series"] == []
        assert service._live_cache[("STOCKS_NVDA", "stock_1d")][1] is None
    before = list(calls)
    yahoo[("1d", "5m")] = yahoo_stock_payload([("2026-09-28T09:30-04:00", 193)], stamp="2026-09-28T09:30-04:00")
    clock[0] = 1014.9
    assert service.snapshot("1d", "stocks")["items"]["NVDA"] == initial and calls == before
    clock[0] = 1015.0
    fresh = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert fresh["session"]["date"] == "2026-09-28" and len(fresh["series"]) == 1
    assert len(calls) == len(before) + 3
    yahoo.clear()
    clock[0] += 15
    unavailable = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert unavailable["series"] == [] and unavailable["error"] == "unavailable"
    assert "session" not in unavailable


@pytest.mark.parametrize("field,value", [
    ("regularMarketTime", "2026-09-25T10:00"),
    ("regularMarketPrice", float("nan")),
    ("chartPreviousClose", "bad price"),
])
def test_stock_yahoo_quote_parse_failure_retains_initial_bars(monkeypatch, field, value):
    payload = yahoo_stock_payload([("2026-09-25T10:00-04:00", 191)])
    payload["chart"]["result"][0]["meta"][field] = value
    service, calls = stock_provider_macro(monkeypatch, yahoo={("1d", "5m"): payload})
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert entry["session"]["date"] == "2026-09-25" and entry["series"][0]["v"] == 191
    assert entry["metrics"]["live"] is False and entry["live_source"] is None
    assert entry["chart_source"] == "Yahoo NVDA" and entry["metrics"]["day_high"] == 191
    assert calls[-1] == ("yahoo", "1d", "5m") and len(calls) == 3


def test_stock_scope_leaves_non_us_and_non_editable_us_series_unchanged(monkeypatch):
    from datetime import datetime

    service, calls = stock_provider_macro(
        monkeypatch,
        lb_bars=[(datetime.fromisoformat(t), v) for t, v in [
            ("2026-09-25T10:00-04:00", 191), ("2026-09-28T08:00-04:00", 192),
        ]],
        lb_quote={"last": 195, "prev_close": 194, "timestamp": "2026-09-28T08:00-04:00"},
    )
    service.settings = Settings({"live_sections": [
        {"id": "stocks", "symbols": ["700.HK"]},
        {"id": "vix", "series": [{"key": "VIX", "sources": [{"type": "longbridge", "symbol": "VIX.US"}],
                                  "live": [{"type": "longbridge", "symbol": "VIX.US"}]}]},
    ]})
    for section, key in (("stocks", "700.HK"), ("vix", "VIX")):
        entry = service.snapshot("1d", section)["items"][key]
        assert entry["series"] == [{"t": "2026-09-28T08:00-04:00", "v": 192}]
        assert "session" not in entry and "chart_source" not in entry
        assert entry["metrics"]["day_high"] == 192
    assert len(calls) == 4 and all(call[0] == "longbridge" for call in calls)
    assert all(cache_key[1] == "1d" for cache_key in service._live_cache)


def test_stock_5d_keeps_30_minute_yahoo_chart_and_ordinary_1d_metrics(monkeypatch):
    service, calls = stock_provider_macro(monkeypatch, yahoo={
        ("1d", "5m"): yahoo_stock_payload([("2026-09-28T08:00-04:00", 900)]),
        ("5d", "30m"): yahoo_stock_payload([
            ("2026-09-24T10:00-04:00", 191), ("2026-09-25T10:00-04:00", 192),
        ], price=999, prev_close=50),
    })
    service.client = None
    entry = service.snapshot("5d", "stocks")["items"]["NVDA"]
    assert [p["v"] for p in entry["series"]] == [191, 192]
    assert entry["metrics"]["value"] == 191.2 and entry["metrics"]["change"] == pytest.approx(0.7)
    assert entry["metrics"]["day_high"] == 900
    assert "session" not in entry and "chart_source" not in entry
    assert calls == [("yahoo", "1d", "5m"), ("yahoo", "5d", "30m")]
    assert service.live_value("stocks", "NVDA") == ("2026-09-28T08:00-04:00", 191.2)
    assert len(calls) == 2


def test_stock_primary_quote_missing_time_is_not_borrowed_from_chart_or_daily(monkeypatch):
    service, _ = stock_provider_macro(
        monkeypatch, lb_quote={"last": 195, "prev_close": 194},
        yahoo={("1d", "5m"): yahoo_stock_payload([("2026-09-25T10:00-04:00", 192)], price=999, prev_close=50)},
    )
    entry = service.snapshot("1d", "stocks")["items"]["NVDA"]
    assert entry["live_source"] == "Longbridge NVDA.US" and entry["chart_source"] == "Yahoo NVDA"
    assert entry["metrics"]["as_of"] is None
    assert (entry["metrics"]["value"], entry["metrics"]["prev_close"], entry["metrics"]["change"]) == (195, 194, 1)
    assert entry["session"]["date"] == "2026-09-25"
