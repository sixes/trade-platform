import time
from datetime import date, timedelta

import pytest

from backend.analytics.greeks import put_delta, strike_for_put_delta
from backend.analytics.screener import strike_band_for_deltas, subsample_strikes
from backend.jobs import ScanService
from backend.market_data.ratelimit import SlidingWindowBudget


def test_strike_for_put_delta_round_trips():
    spot, t, sigma, r, q = 500.0, 35 / 365, 0.22, 0.04, 0.012
    for target in (0.05, 0.2, 0.4):
        strike = strike_for_put_delta(spot, target, t, sigma, r, q)
        assert 0 < strike < spot
        assert abs(put_delta(spot, strike, t, sigma, r, q)) == pytest.approx(target, abs=1e-6)
    assert strike_for_put_delta(spot, 0, t, sigma) is None
    assert strike_for_put_delta(spot, 0.2, 0, sigma) is None


def test_strike_band_contains_target_deltas_under_skew():
    spot, dte, r, q = 500.0, 38, 0.04, 0.012
    atm_iv, skew_ratio = 0.18, 1.15
    lo, hi = strike_band_for_deltas(spot, 0.15, 0.25, dte, atm_iv, skew_ratio, r, q)
    assert lo < hi <= spot
    t = dte / 365
    # A 15-delta put priced with a steep skew (1.35x ATM) and a 25-delta put at a low vol both fall inside the band.
    assert lo <= strike_for_put_delta(spot, 0.15, t, atm_iv * 1.35, r, q)
    assert hi >= strike_for_put_delta(spot, 0.25, t, atm_iv * 0.85, r, q)
    # Without ATM IV we fall back to the configured band.
    assert strike_band_for_deltas(spot, 0.1, 0.3, dte, None, None, r, q, (0.6, 1.0)) == (300.0, 500.0)


def test_subsample_strikes_keeps_ends_and_count():
    items = list(range(100))
    picked = subsample_strikes(items, 10)
    assert len(picked) == 10 and picked[0] == 0 and picked[-1] == 99
    assert subsample_strikes(items[:5], 10) == items[:5]
    assert subsample_strikes(items, 1) == [99]


def test_select_expiries_keeps_reference_and_caps_count():
    today = date(2026, 9, 23)
    expiries = [today + timedelta(days=d) for d in (7, 14, 21, 28, 31, 35, 38, 42, 45, 59, 73)]
    reference, scenario = ScanService._select_expiries(expiries, today, 30, 45, 30, 3)
    assert (reference - today).days == 31
    assert len(scenario) == 3 and reference in scenario
    assert all(30 <= (e - today).days <= 45 for e in scenario)
    reference, scenario = ScanService._select_expiries(expiries, today, 55, 120, 30, 3)
    assert (reference - today).days == 31 and [(e - today).days for e in scenario] == [59, 73]
    assert ScanService._select_expiries([], today, 30, 45, 30, 3) == (None, [])


def test_sliding_window_budget():
    budget = SlidingWindowBudget(limit=100, window_seconds=0.3)
    assert budget.reserve(60) == 0 and budget.reserve(40) == 0
    wait = budget.reserve(1)
    assert 0 < wait <= 0.3 and budget.used() == 100
    time.sleep(wait + 0.02)
    assert budget.used() == 0 and budget.reserve(100) == 0
    budget.penalize()
    assert budget.used() >= 100 and budget.reserve(1) > 0
