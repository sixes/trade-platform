from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

Series = Sequence[Tuple[str, float]]


def values_of(series: Series) -> List[float]:
    return [v for _, v in series]


def iv_rank(values: Sequence[float], lookback: int) -> Optional[float]:
    window = list(values[-lookback:])
    if len(window) < 2:
        return None
    lo, hi = min(window), max(window)
    if hi == lo:
        return 50.0
    return (window[-1] - lo) / (hi - lo) * 100.0


def percentile_rank(values: Sequence[float], lookback: int) -> Optional[float]:
    window = list(values[-lookback:])
    if len(window) < 2:
        return None
    current = window[-1]
    below = sum(1 for v in window[:-1] if v < current)
    return below / (len(window) - 1) * 100.0


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if n <= 0 or len(values) < n:
        return None
    window = values[-n:]
    return sum(window) / n


def change_over(values: Sequence[float], n: int) -> Optional[float]:
    if n <= 0 or len(values) <= n:
        return None
    return values[-1] - values[-1 - n]


def rank_series(values: Sequence[float], lookback: int) -> List[Optional[float]]:
    """IV-rank style value for each point using a trailing window (used for change-in-rank)."""
    out: List[Optional[float]] = []
    for i in range(len(values)):
        window = values[max(0, i + 1 - lookback):i + 1]
        if len(window) < 2:
            out.append(None)
            continue
        lo, hi = min(window), max(window)
        out.append(50.0 if hi == lo else (window[-1] - lo) / (hi - lo) * 100.0)
    return out


def classify_level(value: Optional[float], cut_points: Sequence[float], labels: Sequence[str]) -> Optional[str]:
    """labels has len(cut_points)+1 entries; value < cut_points[i] -> labels[i]."""
    if value is None:
        return None
    for cut, label in zip(cut_points, labels):
        if value < cut:
            return label
    return labels[-1]


def detect_panic_reversal(series: Series, peak_lookback: int, min_peak: float, min_drop_pct: float) -> dict:
    values = values_of(series)
    if len(values) < 3:
        return {"detected": False}
    window = series[-peak_lookback:]
    peak_date, peak = max(window, key=lambda p: p[1])
    current = values[-1]
    drop_pct = (peak - current) / peak * 100.0 if peak else 0.0
    peak_index = len(series) - len(window) + [p[0] for p in window].index(peak_date)
    peak_is_past = peak_index < len(series) - 1
    detected = peak >= min_peak and peak_is_past and drop_pct >= min_drop_pct
    return {
        "detected": detected,
        "peak": peak,
        "peak_date": peak_date,
        "current": current,
        "drop_from_peak_pct": drop_pct,
        "days_since_peak": len(series) - 1 - peak_index,
    }
