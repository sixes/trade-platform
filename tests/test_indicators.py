from backend.analytics.indicators import (
    change_over,
    classify_level,
    detect_panic_reversal,
    iv_rank,
    percentile_rank,
    rank_series,
    sma,
)


def series(values, start_day=1):
    return [(f"2026-01-{i + start_day:02d}", v) for i, v in enumerate(values)]


def test_iv_rank_and_percentile():
    values = [10, 20, 30, 40, 25]
    assert iv_rank(values, 252) == 50.0
    assert percentile_rank(values, 252) == 50.0
    assert iv_rank(values, 3) == 0.0
    assert iv_rank([5, 5, 5], 10) == 50.0
    assert iv_rank([5], 10) is None


def test_sma_and_change():
    values = [1, 2, 3, 4, 5]
    assert sma(values, 5) == 3
    assert sma(values, 6) is None
    assert change_over(values, 2) == 2
    assert change_over(values, 5) is None


def test_rank_series_matches_iv_rank_at_end():
    values = [12, 15, 30, 18, 14, 20]
    ranks = rank_series(values, 4)
    assert ranks[0] is None
    assert abs(ranks[-1] - iv_rank(values, 4)) < 1e-9


def test_classify_level():
    labels = ("LOW", "MEDIUM", "HIGH", "VERY_HIGH")
    assert classify_level(10, [16, 22, 30], labels) == "LOW"
    assert classify_level(16, [16, 22, 30], labels) == "MEDIUM"
    assert classify_level(29.9, [16, 22, 30], labels) == "HIGH"
    assert classify_level(45, [16, 22, 30], labels) == "VERY_HIGH"
    assert classify_level(None, [16], ("A", "B")) is None


def test_panic_reversal_detected_after_spike_and_drop():
    vix = series([15, 16, 18, 25, 38, 34, 30, 27, 26])
    info = detect_panic_reversal(vix, peak_lookback=15, min_peak=30, min_drop_pct=15)
    assert info["detected"] is True
    assert info["peak"] == 38
    assert info["days_since_peak"] == 4
    assert round(info["drop_from_peak_pct"]) == 32


def test_panic_reversal_not_detected_when_peak_is_today_or_too_low():
    still_rising = series([15, 18, 25, 33, 40])
    assert detect_panic_reversal(still_rising, 15, 30, 15)["detected"] is False
    small_spike = series([15, 18, 24, 20, 17])
    assert detect_panic_reversal(small_spike, 15, 30, 15)["detected"] is False
    shallow_drop = series([15, 20, 35, 33, 32])
    assert detect_panic_reversal(shallow_drop, 15, 30, 15)["detected"] is False
