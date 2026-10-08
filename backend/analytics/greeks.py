from __future__ import annotations

import math
from typing import Optional


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(spot: float, strike: float, t: float, sigma: float, r: float, q: float):
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def _valid(spot: float, strike: float, t: float, sigma: Optional[float]) -> bool:
    return bool(spot and strike and spot > 0 and strike > 0 and t > 0 and sigma and sigma > 0)


def put_delta(spot: float, strike: float, t: float, sigma: Optional[float], r: float = 0.0, q: float = 0.0) -> Optional[float]:
    if not _valid(spot, strike, t, sigma):
        return None
    d1, _ = _d1_d2(spot, strike, t, sigma, r, q)
    return -math.exp(-q * t) * norm_cdf(-d1)


def put_price(spot: float, strike: float, t: float, sigma: Optional[float], r: float = 0.0, q: float = 0.0) -> Optional[float]:
    if not _valid(spot, strike, t, sigma):
        return None
    d1, d2 = _d1_d2(spot, strike, t, sigma, r, q)
    return strike * math.exp(-r * t) * norm_cdf(-d2) - spot * math.exp(-q * t) * norm_cdf(-d1)


def put_theta_per_day(spot: float, strike: float, t: float, sigma: Optional[float], r: float = 0.0, q: float = 0.0) -> Optional[float]:
    if not _valid(spot, strike, t, sigma):
        return None
    d1, d2 = _d1_d2(spot, strike, t, sigma, r, q)
    theta = (
        -spot * math.exp(-q * t) * norm_pdf(d1) * sigma / (2 * math.sqrt(t))
        + r * strike * math.exp(-r * t) * norm_cdf(-d2)
        - q * spot * math.exp(-q * t) * norm_cdf(-d1)
    )
    return theta / 365.0


def vega(spot: float, strike: float, t: float, sigma: Optional[float], r: float = 0.0, q: float = 0.0) -> Optional[float]:
    if not _valid(spot, strike, t, sigma):
        return None
    d1, _ = _d1_d2(spot, strike, t, sigma, r, q)
    return spot * math.exp(-q * t) * norm_pdf(d1) * math.sqrt(t) / 100.0


def strike_for_put_delta(spot: float, delta_abs: float, t: float, sigma: float, r: float = 0.0, q: float = 0.0) -> Optional[float]:
    """Strike whose put has the given |delta| under Black-Scholes (inverse of put_delta in K)."""
    if not _valid(spot, spot, t, sigma) or not (0 < delta_abs < 1):
        return None
    from statistics import NormalDist

    # |delta| = e^{-qT} N(-d1)  =>  d1 = -N^{-1}(|delta| e^{qT})
    p = min(0.999999, delta_abs * math.exp(q * t))
    d1 = -NormalDist().inv_cdf(p)
    return spot * math.exp(-d1 * sigma * math.sqrt(t) + (r - q + 0.5 * sigma * sigma) * t)


def implied_vol_put(price: float, spot: float, strike: float, t: float, r: float = 0.0, q: float = 0.0,
                    lo: float = 0.01, hi: float = 5.0) -> Optional[float]:
    """Black-Scholes implied volatility of a put by bisection; None if the price is outside no-arbitrage bounds."""
    if not (price and price > 0 and spot > 0 and strike > 0 and t > 0):
        return None
    intrinsic = max(strike * math.exp(-r * t) - spot * math.exp(-q * t), 0.0)
    if price <= intrinsic + 1e-6 or price >= strike * math.exp(-r * t):
        return None
    p_lo, p_hi = put_price(spot, strike, t, lo, r, q), put_price(spot, strike, t, hi, r, q)
    if p_lo is None or p_hi is None or not (p_lo <= price <= p_hi):
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        p_mid = put_price(spot, strike, t, mid, r, q)
        if p_mid is None:
            return None
        if abs(p_mid - price) < 1e-7:
            return mid
        if p_mid < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
