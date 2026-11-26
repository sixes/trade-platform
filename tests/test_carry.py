from backend.analytics.carry import CarryModel
from backend.config import Settings

import math
import pytest


class Indices:
    def __init__(self, values):
        self.values = values

    def try_series(self, key, sources):
        value = self.values.get(key)
        return [("2026-09-22", value)] if value is not None else None


def settings(**analytics):
    base = {"risk_free_rate": 0.03, "dividend_yield": 0.012,
            "rate_curve": [{"dte": 91, "sources": []}, {"dte": 365, "sources": []}, {"dte": 730, "sources": []}]}
    return Settings({"analytics": dict(base, **analytics)})


def test_rate_interpolates_the_curve_and_is_flat_beyond_its_ends():
    carry = CarryModel(settings(), Indices({"rate_curve_91": 4.0, "rate_curve_365": 5.0, "rate_curve_730": 6.0}))
    lo, mid, hi = math.log1p(0.04), math.log1p(0.05), math.log1p(0.06)
    assert carry.rate(30) == lo and carry.rate(900) == hi
    assert carry.rate(228) == pytest.approx((lo + mid) / 2)
    assert carry.describe("SPY.US", 30)["rate_source"] == "treasury_curve"


def test_rate_skips_missing_tenors_and_falls_back_to_the_flat_rate():
    assert CarryModel(settings(), Indices({"rate_curve_365": 5.0})).rate(30) == math.log1p(0.05)
    assert CarryModel(settings(), Indices({})).rate(30) == 0.03
    assert CarryModel(settings()).rate(400) == 0.03


def test_dividend_yield_prefers_config_then_trailing_then_default():
    calls = []

    def fetch(ticker):
        calls.append(ticker)
        if ticker == "BAD":
            raise ValueError("no data")
        return 0.004

    carry = CarryModel(settings(dividend_yields={"spy": 0.011}), dividend_fetcher=fetch)
    assert carry.dividend("SPY.US") == (0.011, "config")
    assert carry.dividend("MSFT.US") == (0.004, "trailing_12m")
    assert carry.dividend("MSFT.US") == (0.004, "trailing_12m") and calls == ["MSFT"]  # cached
    assert carry.dividend("BAD.US") == (0.012, "default")
    assert carry.dividend("BAD.US") == (0.012, "default") and calls == ["MSFT", "BAD"]  # failure cached too
    assert CarryModel(settings()).dividend("MSFT.US") == (0.012, "default")
