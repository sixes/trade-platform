import pytest

from backend.market_data.indices import IndexHistoryProvider, transform_series
from backend.market_data.macro import (
    MacroService,
    parse_aaii_sentiment,
    parse_cleveland_nowcasts,
    parse_cnn_put_call,
    parse_cot_net,
    parse_ism_releases,
    parse_redbook_events,
    parse_shiller_cape,
    parse_zillow_rent,
)
from backend.config import load_settings

SETTINGS = load_settings()


def test_transform_series_level_change_percent_and_scale():
    series = [("2026-01-01", 100.0), ("2026-02-01", 110.0), ("2026-03-01", 99.0)]
    assert transform_series(series) == series
    assert transform_series(series, "level") == series
    assert transform_series(series, "chg") == [("2026-02-01", 10.0), ("2026-03-01", -11.0)]
    assert transform_series(series, "pct") == [("2026-02-01", pytest.approx(10.0)),
                                               ("2026-03-01", pytest.approx(-10.0))]
    assert transform_series(series, "chg", 0.001) == [("2026-02-01", 0.01), ("2026-03-01", -0.011)]


def test_transform_series_yoy_uses_same_month_prior_year():
    series = [("2023-02-28", 100.0), ("2024-02-29", 110.0), ("2024-03-01", 120.0), ("2025-03-01", 90.0)]
    assert transform_series(series, "yoy") == [("2024-02-29", pytest.approx(10.0)),
                                               ("2025-03-01", pytest.approx(-25.0))]
    with pytest.raises(ValueError):
        transform_series(series, "bogus")


def test_fred_source_applies_transform_before_caching(tmp_path):
    provider = IndexHistoryProvider(tmp_path, ttl_minutes=60)
    provider._download = lambda url, column: [("2023-01-01", 100.0), ("2024-01-01", 110.0)]
    assert provider.series("TST_YOY", [{"type": "fred", "id": "X", "transform": "yoy"}]) == [
        ("2024-01-01", pytest.approx(10.0))]
    assert provider.series("TST_SCALED", [{"type": "fred", "id": "X", "scale": 0.001}]) == [
        ("2023-01-01", 0.1), ("2024-01-01", 0.11)]


ISM_PAGE = """
<a href="/news-releases/ism-report-on-business-manufacturing-pmi-at-487-for-october-2026-1.html">
  Manufacturing PMI&reg; at 48.7% October 2026 ISM Report On Business</a>
<a href="/news-releases/ism-report-on-business-services-pmi-at-524-for-october-2026-2.html">
  Services PMI&reg; at 52.4% October 2026 ISM Report On Business</a>
<a href="/news-releases/hospital-pmi-at-501-for-october-2026-3.html">
  Hospital PMI&reg; at 50.1% October 2026</a>
<a href="/news-releases/ism-report-on-business-manufacturing-pmi-at-492-for-september-2026-4.html">
  Manufacturing PMI&reg; at 49.2% September 2026 ISM Report On Business</a>
<a href="/news-releases/ism-report-on-business-manufacturing-pmi-at-49-for-november-2022-5.html">
  Manufacturing PMI&reg; at 49%; November 2022 Manufacturing ISM&reg; Report On Business&reg;</a>
<a href="/news-releases/no-numbers-here-5.html">Manufacturing PMI&reg; release coming</a>
"""


def test_parse_ism_releases_filters_hospital_and_unknown_titles():
    assert parse_ism_releases(ISM_PAGE, "manufacturing") == [
        ("2022-11-01", 49.0), ("2026-09-01", 49.2), ("2026-10-01", 48.7)]
    assert parse_ism_releases(ISM_PAGE, "services") == [("2026-10-01", 52.4)]


NOWCAST_PAYLOAD = [
    {"chart": {"subcaption": "2026-09"}, "dataset": [
        {"seriesname": "CPI Inflation", "data": [{"value": "0.2"}, {"value": "0.28"}, {"value": ""}]},
        {"seriesname": "Core CPI Inflation", "data": [{"value": "0.31"}]},
        {"seriesname": "Actual CPI Inflation", "data": [{"value": "0.4"}]},
    ]},
    {"chart": {"subcaption": "2026-10"}, "dataset": [
        {"seriesname": "CPI Inflation", "data": [{"value": ""}]},
        {"seriesname": "Core CPI Inflation", "data": [{"value": "0.26"}]},
        {"seriesname": "Core PCE Inflation", "data": [{"value": "0.19"}]},
    ]},
    {"chart": {"subcaption": "not a month"}, "dataset": [{"seriesname": "CPI Inflation", "data": [{"value": "9.9"}]}]},
]


def test_parse_cleveland_nowcasts_takes_final_value_per_target_month():
    parsed = parse_cleveland_nowcasts(NOWCAST_PAYLOAD)
    assert parsed["cpi_nowcast"] == [("2026-09-01", 0.28)]
    assert parsed["core_cpi_nowcast"] == [("2026-09-01", 0.31), ("2026-10-01", 0.26)]
    assert parsed["core_pce_nowcast"] == [("2026-10-01", 0.19)]
    assert parsed["pce_nowcast"] == []


def test_ism_fetcher_fetches_pages_once_per_ttl(tmp_path, monkeypatch):
    service = MacroService(SETTINGS, IndexHistoryProvider(tmp_path, ttl_minutes=60), None)
    calls = []

    class Resp:
        text = ISM_PAGE

        def raise_for_status(self):
            pass

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            calls.append(url)
            return Resp()

    monkeypatch.setattr("backend.market_data.macro.httpx.Client", FakeHttp)
    first = service._ism_series({"kind": "manufacturing", "pages": 2})
    second = service._ism_series({"kind": "manufacturing", "pages": 2})
    assert first == second == [("2022-11-01", 49.0), ("2026-09-01", 49.2), ("2026-10-01", 48.7)]
    assert len(calls) == 2  # both pages fetched once, then served from the TTL cache


ZORI_CSV = (
    "RegionID,SizeRank,RegionName,RegionType,2023-01-31,2024-01-31,2024-02-29\n"
    "1,1,New York,msa,2500,2600,2610\n"
    "102001,0,United States,country,100,110,112.5\n"
)


def test_parse_zillow_rent_picks_national_row_and_normalizes_dates():
    assert parse_zillow_rent(ZORI_CSV) == [("2023-01-01", 100.0), ("2024-01-01", 110.0), ("2024-02-01", 112.5)]
    assert parse_zillow_rent("RegionID,SizeRank,RegionName,RegionType\n") == []


def test_zillow_fetcher_applies_yoy_and_caches(tmp_path, monkeypatch):
    service = MacroService(SETTINGS, IndexHistoryProvider(tmp_path, ttl_minutes=60), None)
    calls = []

    class Resp:
        text = ZORI_CSV

        def raise_for_status(self):
            pass

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            calls.append(url)
            return Resp()

    monkeypatch.setattr("backend.market_data.macro.httpx.Client", FakeHttp)
    assert service._zillow_series({"transform": "yoy"}) == [("2024-01-01", pytest.approx(10.0))]
    assert service._zillow_series({"transform": "yoy"}) == [("2024-01-01", pytest.approx(10.0))]
    assert len(calls) == 1


REDBOOK_PAYLOAD = {"result": [
    {"title": "Redbook YoY", "referenceDate": "2026-11-14T00:00:00Z", "actual": 9.5},
    {"title": "Redbook YoY", "referenceDate": "2026-11-07T00:00:00Z", "actual": 10.1},
    {"title": "Redbook YoY", "referenceDate": "2026-11-21T00:00:00Z", "actual": None},
    {"title": "Retail Sales", "referenceDate": "2026-11-14T00:00:00Z", "actual": 0.5},
    {"title": "Redbook YoY", "date": "2026-10-31T13:55:00.000Z", "actual": "10.4"},
]}


def test_parse_redbook_events_dates_by_reference_week_and_skips_pending():
    assert parse_redbook_events(REDBOOK_PAYLOAD) == [
        ("2026-10-31", 10.4), ("2026-11-07", 10.1), ("2026-11-14", 9.5)]


def test_redbook_fetcher_windows_history_and_caches(tmp_path, monkeypatch):
    service = MacroService(SETTINGS, IndexHistoryProvider(tmp_path, ttl_minutes=60), None)
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return REDBOOK_PAYLOAD

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            calls.append(params)
            return Resp()

    monkeypatch.setattr("backend.market_data.macro.httpx.Client", FakeHttp)
    expected = [("2026-10-31", 10.4), ("2026-11-07", 10.1), ("2026-11-14", 9.5)]
    assert service._redbook_series({"years": 1}) == expected
    assert service._redbook_series({"years": 1}) == expected
    assert len(calls) == 5  # one 92-day window per request, then served from the TTL cache
    assert all(call["countries"] == "US" and call["from"] < call["to"] for call in calls)


CNN_PUT_CALL = {"put_call_options": {"data": [
    {"x": 1763942400000, "y": 0.8004527957774998},
    {"x": 1764028800000, "y": 0.7934567567979536},
    {"x": 1764115200000, "y": 0},
    {"x": "bad", "y": 1.0},
]}}


def test_parse_cnn_put_call_keeps_positive_daily_values():
    assert parse_cnn_put_call(CNN_PUT_CALL) == [
        ("2025-11-24", pytest.approx(0.8004527957774998)),
        ("2025-11-25", pytest.approx(0.7934567567979536)),
    ]


AAII_PAGE = """
<h3>Recent weekly results</h3>
<div class="weekending"><div>11/18/2026</div><div>33.3%</div><div>24.4%</div><div>42.2%</div></div>
<div class="weekending"><div>11/11/2026</div><div>30.8%</div><div>17.8%</div><div>51.4%</div></div>
<h3>Historical view</h3><div>37.5% 31.0% 31.5%</div>
"""


def test_parse_aaii_sentiment_uses_recent_table_only():
    assert parse_aaii_sentiment(AAII_PAGE) == [
        ("2026-11-11", pytest.approx(-20.6)),
        ("2026-11-18", pytest.approx(-8.9)),
    ]
    assert parse_aaii_sentiment("<html>no table</html>") == []


def test_parse_cot_net_subtracts_shorts_from_longs():
    payload = [
        {"report_date_as_yyyy_mm_dd": "2026-11-17T00:00:00.000",
         "noncomm_positions_long_all": "241667", "noncomm_positions_short_all": "335633"},
        {"report_date_as_yyyy_mm_dd": "not-a-date",
         "noncomm_positions_long_all": "1", "noncomm_positions_short_all": "2"},
        {"report_date_as_yyyy_mm_dd": "2026-11-10T00:00:00.000", "noncomm_positions_long_all": "x"},
    ]
    assert parse_cot_net(payload) == [("2026-11-17", -93966.0)]


def test_parse_shiller_cape_uses_date_fraction_and_skips_invalid_rows():
    row_a = [None] * 13
    row_a[5], row_a[12] = 2026.8333333333, 41.2
    row_b = [None] * 13
    row_b[5], row_b[12] = 2026.9583333333, 42.5
    skipped = [None] * 13
    skipped[5], skipped[12] = 2026.5, "nan"
    assert parse_shiller_cape([row_a, row_b, skipped]) == [
        ("2026-11-01", 41.2), ("2026-12-01", 42.5)]


def test_ratio_fetcher_divides_on_common_dates(tmp_path):
    class StubIndices:
        def series(self, key, sources, external=None):
            if key.endswith("_num"):
                return [("2026-01-02", 100.0), ("2026-01-05", 101.0), ("2026-01-06", 102.0)]
            return [("2026-01-02", 200.0), ("2026-01-05", 202.0)]

    service = MacroService(SETTINGS, StubIndices(), None)
    source = {"type": "ratio", "numerator": {"symbol": "A"}, "denominator": {"symbol": "B"}}
    assert service._ratio_series(source) == [
        ("2026-01-02", pytest.approx(0.5)), ("2026-01-05", pytest.approx(0.5))]
