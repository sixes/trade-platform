from datetime import date

import pytest

from backend.analytics.screener import (
    apply_depth,
    atm_iv_by_expiry,
    build_candidates,
    filter_candidates,
    reference_metrics,
    score_candidates,
)
from backend.config import load_settings
from tests.fakes import FakeLongbridge, Q, R

SETTINGS = load_settings()
TODAY = date(2026, 9, 23)


def chain_candidates(fake: FakeLongbridge):
    rows = list(fake._rows.values())
    cands, skipped = build_candidates(rows, fake.spot_price, TODAY, R, Q)
    return cands, skipped


def test_build_candidates_computes_greeks_and_yields():
    fake = FakeLongbridge(today=TODAY)
    cands, skipped = chain_candidates(fake)
    assert cands and skipped["expired"] == 0
    c = next(c for c in cands if c.strike == 450.0 and c.dte == 38)
    assert -0.5 < c.delta < 0
    assert c.moneyness_pct == pytest.approx(-10.0)
    assert c.price == c.last and c.price_source == "last"
    assert c.premium_yield_pct == c.price / 450.0 * 100
    assert abs(c.annualized_yield_pct - c.premium_yield_pct * 365 / 38) < 1e-9
    assert c.breakeven == 450.0 - c.price
    assert c.theta_per_day < 0


def test_reference_metrics_picks_expiry_near_30_dte_and_measures_skew():
    fake = FakeLongbridge(atm_iv=0.18, slope=1.5, today=TODAY)
    cands, _ = chain_candidates(fake)
    ref = reference_metrics(cands, 30, 0.25, SETTINGS.get("analytics.chain_skew_ratio"))
    assert ref["dte"] == 31
    assert abs(ref["atm_iv"] - 0.18) < 0.005
    assert ref["otm_put_iv"] > ref["atm_iv"]
    assert ref["skew_ratio"] > 1.0
    assert ref["skew_level"] in ("LOW", "NORMAL", "HIGH", "EXTREME")


def test_filters_respect_scenario_ranges():
    fake = FakeLongbridge(today=TODAY)
    cands, _ = chain_candidates(fake)
    cfg = SETTINGS.section("scenarios.sell_20_delta_puts")
    filtered = filter_candidates(cands, cfg, atm_iv=0.18)
    assert filtered
    for c in filtered:
        assert cfg["dte"][0] <= c.dte <= cfg["dte"][1]
        assert cfg["delta"][0] <= abs(c.delta) <= cfg["delta"][1]
        assert c.open_interest >= cfg["min_open_interest"]
        assert c.strike < c.spot
        assert c.annualized_yield_pct >= cfg["min_annualized_yield_pct"]
        assert c.iv_ratio is not None and c.delta_target_gap is not None


def test_scoring_ranks_and_flags():
    fake = FakeLongbridge(today=TODAY)
    cands, _ = chain_candidates(fake)
    cfg = SETTINGS.section("scenarios.sell_20_delta_puts")
    filtered = filter_candidates(cands, cfg, atm_iv=0.18)
    ranked = score_candidates(filtered, "sell_20_delta_puts", cfg["max_spread_pct"])
    scores = [c.score for c in ranked]
    assert scores == sorted(scores, reverse=True)
    assert [c.rank for c in ranked] == list(range(1, len(ranked) + 1))
    assert all("price_from_last" in c.flags for c in ranked)


def test_apply_depth_switches_to_mid_and_enforces_spread():
    fake = FakeLongbridge(today=TODAY)
    cands, _ = chain_candidates(fake)
    cfg = dict(SETTINGS.section("scenarios.sell_20_delta_puts"))
    filtered = filter_candidates(cands, cfg, atm_iv=0.18)
    c = filtered[0]
    bid, ask = round(c.price * 0.98, 2), round(c.price * 1.02, 2)
    apply_depth(c, bid, ask)
    mid = (bid + ask) / 2
    assert c.mid == mid and c.price == mid and c.price_source == "mid"
    assert c.spread_pct == pytest.approx((ask - bid) / mid * 100)
    wide = filtered[1]
    apply_depth(wide, round(wide.price * 0.8, 2), round(wide.price * 1.2, 2))
    cfg["max_spread_pct"] = 10
    survivors = filter_candidates([c, wide], cfg, atm_iv=0.18, enforce_spread=True)
    assert c in survivors and wide not in survivors


def test_buy_puts_scoring_prefers_cheaper_relative_iv():
    fake = FakeLongbridge(today=TODAY)
    cands, _ = chain_candidates(fake)
    cfg = SETTINGS.section("scenarios.buy_puts")
    filtered = filter_candidates(cands, cfg, atm_iv=0.18)
    ranked = score_candidates(filtered, "buy_puts", cfg["max_spread_pct"])
    assert ranked
    top_ratio = sum(c.iv_ratio for c in ranked[:3]) / 3
    bottom_ratio = sum(c.iv_ratio for c in ranked[-3:]) / 3
    assert top_ratio < bottom_ratio


def test_atm_iv_by_expiry_and_per_expiry_iv_ratio():
    fake = FakeLongbridge(atm_iv=0.18, today=TODAY)
    cands, _ = chain_candidates(fake)
    per_expiry = atm_iv_by_expiry(cands)
    assert set(per_expiry) == {c.expiry for c in cands}
    assert all(abs(v - 0.18) < 1e-6 for v in per_expiry.values())
    # Only far-OTM strikes for one expiry: no bracketing pair and the nearest strike is >3% away -> no ATM IV.
    far = [c for c in cands if c.expiry == cands[0].expiry and c.strike <= 450]
    assert cands[0].expiry not in atm_iv_by_expiry(far)

    cfg = SETTINGS.section("scenarios.sell_20_delta_puts")
    biased = {e: 0.36 for e in per_expiry}  # pretend every expiry's ATM IV is double
    filtered = filter_candidates(cands, cfg, atm_iv=0.18, atm_by_expiry=biased)
    assert filtered and all(abs(c.iv_ratio - c.iv / 0.36) < 1e-9 for c in filtered)
