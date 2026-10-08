"""Reconstructs a daily at-the-money IV history for a stock from option daily closes.

Longbridge keeps ~300 daily bars for listed option contracts, so a long-dated expiry that
has existed for a year gives, for each past day, prices of strikes near that day's close.
Inverting Black-Scholes on the two strikes bracketing the close and interpolating yields an
ATM IV series for that expiry. It is a proxy for the usual 30-day IV (the contract's tenor
shrinks over the window), but it is internally consistent, so its rank is meaningful.
"""
from __future__ import annotations

from datetime import date
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

from backend.analytics.greeks import implied_vol_put

DatedSeries = List[Tuple[str, float]]

MIN_IV, MAX_IV = 0.03, 3.0


def build_proxy_iv_history(
    closes: Sequence[Tuple[str, float]],
    option_closes: Dict[float, Dict[str, float]],
    expiry: date,
    r: float,
    q: float,
    smooth: int = 3,
) -> DatedSeries:
    """closes: (ISO day, underlying close); option_closes: strike -> {ISO day: put close}."""
    strikes = sorted(k for k, bars in option_closes.items() if bars)
    if not strikes:
        return []
    raw: DatedSeries = []
    for day, spot in closes:
        t = (expiry - date.fromisoformat(day)).days / 365.0
        if t <= 1 / 365 or not spot or spot <= 0:
            continue
        point = _iv_at_spot(day, spot, t, strikes, option_closes, r, q)
        if point is not None:
            raw.append((day, point))
    return _rolling_median(raw, smooth) if smooth > 1 else raw


def _iv_at_spot(day: str, spot: float, t: float, strikes: List[float], option_closes: Dict[float, Dict[str, float]],
                r: float, q: float) -> Optional[float]:
    cache: Dict[float, Optional[float]] = {}

    def iv_for(strike: float) -> Optional[float]:
        if strike not in cache:
            price = option_closes[strike].get(day)
            iv = implied_vol_put(price, spot, strike, t, r, q) if price else None
            cache[strike] = iv if iv is not None and MIN_IV <= iv <= MAX_IV else None
        return cache[strike]

    below = [k for k in strikes if k <= spot]
    above = [k for k in strikes if k >= spot]
    # Walk outwards from the money until each side yields a usable IV.
    k_lo = next((k for k in reversed(below) if iv_for(k) is not None), None)
    k_hi = next((k for k in above if iv_for(k) is not None), None)
    lo = iv_for(k_lo) if k_lo is not None else None
    hi = iv_for(k_hi) if k_hi is not None else None
    if lo is not None and hi is not None:
        if k_hi == k_lo:
            return lo
        w = (spot - k_lo) / (k_hi - k_lo)
        return lo * (1 - w) + hi * w
    nearest_k = k_lo if lo is not None else k_hi
    nearest = lo if lo is not None else hi
    if nearest is not None and abs(nearest_k / spot - 1) <= 0.10:
        return nearest
    return None


def _rolling_median(series: DatedSeries, window: int) -> DatedSeries:
    values = [v for _, v in series]
    half = window // 2
    out: DatedSeries = []
    for i, (day, _) in enumerate(series):
        chunk = values[max(0, i - half): i + half + 1]
        out.append((day, median(chunk)))
    return out


def backfill_expiry_candidates(expiries: Sequence[date], today: date, min_dte: int = 60, max_dte: int = 400) -> List[date]:
    """Expiries worth trying for a year of history, best first: January LEAPS (listed longest ago),
    then other standard monthlies farthest out, then anything else in range."""
    in_range = [e for e in expiries if min_dte <= (e - today).days <= max_dte]
    monthlies = [e for e in in_range if _is_third_friday(e)]
    january = sorted(e for e in monthlies if e.month == 1)
    other_monthlies = sorted((e for e in monthlies if e.month != 1), reverse=True)
    rest = sorted((e for e in in_range if not _is_third_friday(e)), reverse=True)
    ordered: List[date] = []
    for e in january + other_monthlies + rest:
        if e not in ordered:
            ordered.append(e)
    return ordered


def pick_backfill_expiry(expiries: Sequence[date], today: date, min_dte: int = 60, max_dte: int = 400) -> Optional[date]:
    candidates = backfill_expiry_candidates(expiries, today, min_dte, max_dte)
    return candidates[0] if candidates else None


def _is_third_friday(day: date) -> bool:
    return day.weekday() == 4 and 15 <= day.day <= 21


def strikes_covering(strikes: Sequence[float], low: float, high: float, max_count: int) -> List[float]:
    """Strikes spanning [low, high] with one extra on each side, thinned evenly to max_count."""
    ordered = sorted(strikes)
    inside = [k for k in ordered if low <= k <= high]
    below = [k for k in ordered if k < low]
    above = [k for k in ordered if k > high]
    chosen = ([below[-1]] if below else []) + inside + ([above[0]] if above else [])
    if len(chosen) <= max_count:
        return chosen
    step = (len(chosen) - 1) / (max_count - 1)
    return sorted({chosen[round(i * step)] for i in range(max_count)})
