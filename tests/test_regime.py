from datetime import date, timedelta

from backend.analytics.regime import SCENARIO_BY_KEY, classify_regime, compute_market_metrics
from backend.config import load_settings

THRESHOLDS = load_settings().section("thresholds")


def make_series(low, high, tail, n=260):
    """n alternating points between low and high followed by the tail values."""
    values = [low if i % 2 == 0 else high for i in range(n)] + list(tail)
    start = date(2025, 1, 1)
    return [((start + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


def regime_for(vix_range, vix_tail, skew_range, skew_tail):
    metrics = compute_market_metrics(make_series(*vix_range, vix_tail), make_series(*skew_range, skew_tail), THRESHOLDS)
    return metrics, classify_regime(metrics)


def test_all_low_is_buy_puts():
    metrics, regime = regime_for((12, 35), [14, 14], (118, 122), [118, 118])
    assert metrics.vix.level == "LOW" and metrics.iv_rank_level == "LOW" and metrics.skew.level == "LOW"
    assert regime.scenario == "buy_puts" and regime.exact_match


def test_low_vix_high_rank_high_skew_is_selective_selling():
    metrics, regime = regime_for((10, 17), [15, 15], (130, 135), [145, 145])
    assert metrics.vix.level == "LOW" and metrics.iv_rank_level == "HIGH" and metrics.skew.level == "HIGH"
    assert regime.scenario == "selective_put_selling" and regime.exact_match


def test_medium_everything_is_20_delta_selling():
    metrics, regime = regime_for((14, 30), [19, 19], (128, 132), [130, 130])
    assert metrics.vix.level == "MEDIUM" and metrics.iv_rank_level == "MEDIUM" and metrics.skew.level == "NORMAL"
    assert regime.scenario == "sell_20_delta_puts" and regime.exact_match


def test_all_high_closes_shorts():
    metrics, regime = regime_for((12, 32), [26, 26], (130, 135), [145, 145])
    assert metrics.vix.level == "HIGH" and metrics.iv_rank_level == "HIGH" and metrics.skew.level == "HIGH"
    assert regime.scenario == "close_shorts_low_delta" and regime.exact_match


def test_extreme_everything_avoids_naked_puts():
    metrics, regime = regime_for((12, 50), [40, 45], (130, 135), [150, 155])
    assert metrics.vix.level == "VERY_HIGH" and metrics.iv_rank_level == "EXTREME" and metrics.skew.level == "EXTREME"
    assert regime.scenario == "avoid_naked_puts" and regime.exact_match


def test_panic_reversal_with_falling_rank_and_skew_scales_in():
    vix_tail = [18, 22, 30, 45, 41, 38, 35, 33, 32]
    skew_tail = [140, 145, 152, 155, 150, 147, 145, 142, 140]
    metrics, regime = regime_for((12, 25), vix_tail, (130, 135), skew_tail)
    assert metrics.panic["detected"] is True
    assert metrics.iv_rank_change_5d < 0 and metrics.skew.change_5d < 0
    assert regime.scenario == "scale_into_put_selling" and regime.exact_match


def test_panic_reversal_without_confirmation_falls_back_with_note():
    vix_tail = [18, 22, 30, 45, 41, 38, 35, 33, 32]
    skew_tail = [130, 130, 130, 130, 130, 135, 140, 145, 148]  # skew still rising
    metrics, regime = regime_for((12, 25), vix_tail, (130, 135), skew_tail)
    assert metrics.panic["detected"] is True
    assert regime.scenario != "scale_into_put_selling"
    assert any("not falling yet" in note for note in regime.notes)


def test_mixed_signals_pick_nearest_scenario_with_notes():
    # Low VIX, low IV rank, but high skew: nearest is buy_puts with a skew caveat.
    metrics, regime = regime_for((12, 35), [14, 14], (130, 135), [145, 145])
    assert regime.scenario == "buy_puts"
    assert not regime.exact_match
    assert 0 < regime.confidence < 1
    assert any("Put Skew is HIGH" in note for note in regime.notes)
    assert any("closer to the money" in note for note in regime.notes)
    assert [c["matched"] for c in regime.conditions] == [True, True, False]
    assert regime.alternatives and regime.alternatives[0]["scenario"] in SCENARIO_BY_KEY


def test_missing_data_defaults_defensive():
    metrics = compute_market_metrics([], [], THRESHOLDS)
    regime = classify_regime(metrics)
    assert regime.scenario == "close_shorts_low_delta"
    assert regime.confidence == 0.0
