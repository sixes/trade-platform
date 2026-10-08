from datetime import date, timedelta

import pytest

from backend.analytics.greeks import implied_vol_put, put_price
from backend.analytics.ivhistory import backfill_expiry_candidates, build_proxy_iv_history, pick_backfill_expiry, strikes_covering
from tests.fakes import FakeLongbridge, Q, R


def test_implied_vol_round_trip_and_bounds():
    spot, strike, t = 500.0, 480.0, 120 / 365
    for sigma in (0.12, 0.25, 0.6):
        price = put_price(spot, strike, t, sigma, R, Q)
        assert implied_vol_put(price, spot, strike, t, R, Q) == pytest.approx(sigma, abs=1e-5)
    assert implied_vol_put(0.0, spot, strike, t, R, Q) is None
    assert implied_vol_put(5.0, spot, 600.0, t, R, Q) is None  # below intrinsic value
    assert implied_vol_put(1000.0, spot, strike, t, R, Q) is None  # above the strike


def test_proxy_history_recovers_known_iv_path():
    fake = FakeLongbridge(atm_iv=0.20, today=date(2026, 9, 23))
    expiry = date(2027, 1, 23)
    chain = fake.put_strikes("SPY.US", expiry, 0, float("inf"))
    closes = fake.daily_closes("SPY.US", 260)
    lo, hi = min(c for _, c in closes), max(c for _, c in closes)
    strikes = strikes_covering([k for k, _ in chain], lo, hi, 14)
    by_strike = dict(chain)
    option_closes = {k: dict(fake.daily_closes(by_strike[k], 260, is_option=True)) for k in strikes}
    series = build_proxy_iv_history(closes, option_closes, expiry, R, Q, smooth=1)
    assert len(series) >= len(closes) - 2
    for day, iv in series:
        expected = fake.history_iv(date.fromisoformat(day))
        assert iv == pytest.approx(expected, abs=0.01)


def test_strikes_covering_and_backfill_expiry_choice():
    strikes = list(range(100, 200, 5))
    chosen = strikes_covering(strikes, 128, 162, 6)
    assert chosen[0] <= 128 and chosen[-1] >= 162 and len(chosen) <= 6
    assert strikes_covering(strikes, 128, 132, 10) == [125, 130, 135]

    today = date(2026, 9, 23)
    expiries = [today + timedelta(days=d) for d in (7, 30, 58, 86)] + [date(2026, 12, 18), date(2027, 1, 15), date(2027, 6, 18), date(2028, 1, 21)]
    assert pick_backfill_expiry(expiries, today, 60, 400) == date(2027, 1, 15)  # January third Friday within range
    # January LEAPS first, then other monthlies farthest-first; today+86 is Dec 18 itself, so it dedups.
    assert (today + timedelta(days=86)) == date(2026, 12, 18)
    assert backfill_expiry_candidates(expiries, today, 60, 400) == [date(2027, 1, 15), date(2027, 6, 18), date(2026, 12, 18)]
    assert pick_backfill_expiry([today + timedelta(days=90)], today, 60, 400) == today + timedelta(days=90)
    assert pick_backfill_expiry([today + timedelta(days=10)], today, 60, 400) is None
