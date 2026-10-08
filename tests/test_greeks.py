import math

from backend.analytics.greeks import put_delta, put_price, put_theta_per_day, vega


def test_put_delta_ranges():
    atm = put_delta(100, 100, 30 / 365, 0.20, r=0.0, q=0.0)
    assert -0.55 < atm < -0.45
    deep_otm = put_delta(100, 60, 30 / 365, 0.20)
    assert -0.001 < deep_otm <= 0
    deep_itm = put_delta(100, 140, 30 / 365, 0.20)
    assert -1.0 <= deep_itm < -0.99
    assert put_delta(100, 100, 0, 0.2) is None
    assert put_delta(100, 100, 0.1, None) is None


def test_put_price_matches_reference_black_scholes():
    # Hull-style reference: S=42, K=40, r=10%, sigma=20%, T=0.5 -> put ~ 0.81
    price = put_price(42, 40, 0.5, 0.20, r=0.10)
    assert math.isclose(price, 0.8086, abs_tol=0.002)


def test_theta_negative_and_vega_positive_for_otm_put():
    theta = put_theta_per_day(500, 470, 35 / 365, 0.22, r=0.04, q=0.012)
    assert theta < 0
    assert vega(500, 470, 35 / 365, 0.22, r=0.04, q=0.012) > 0
