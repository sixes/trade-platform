from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from backend.analytics.indicators import (
    change_over,
    classify_level,
    detect_panic_reversal,
    iv_rank,
    percentile_rank,
    rank_series,
    sma,
    values_of,
)

VIX_LEVELS = ("LOW", "MEDIUM", "HIGH", "VERY_HIGH")
IVR_LEVELS = ("LOW", "MEDIUM", "HIGH", "EXTREME")
SKEW_LEVELS = ("LOW", "NORMAL", "HIGH", "EXTREME")

# Weighted distance: VIX level matters most, then IV rank, then skew.
DIMENSION_WEIGHTS = (3, 2, 1)
MAX_DISTANCE = sum(w * 3 for w in DIMENSION_WEIGHTS)


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    stance: str
    action: str
    description: str
    target: Optional[Tuple[int, int, int]]  # (vix, iv_rank, skew) level indexes; None = rule based


SCENARIOS: List[Scenario] = [
    Scenario(
        key="buy_puts",
        title="Buy Puts (cheap protection)",
        stance="long_vol",
        action="Buy 45-120 DTE puts at 15-40 delta. Favour the cheapest implied vol relative to ATM.",
        description="VIX, IV rank and put skew are all low: downside protection is cheap.",
        target=(0, 0, 0),
    ),
    Scenario(
        key="selective_put_selling",
        title="Selective Put Selling",
        stance="short_vol",
        action="Sell 8-20 delta puts, 30-45 DTE, only where OTM IV is rich versus ATM. Keep size modest.",
        description="VIX is low but IV rank and put skew are high: OTM puts are overpriced relative to at-the-money.",
        target=(0, 2, 2),
    ),
    Scenario(
        key="sell_20_delta_puts",
        title="Sell 20-Delta Puts",
        stance="short_vol",
        action="Sell ~20 delta puts, 30-45 DTE, on liquid underlyings. Standard premium collection.",
        description="VIX and IV rank are medium with normal skew: a balanced environment for systematic put selling.",
        target=(1, 1, 1),
    ),
    Scenario(
        key="close_shorts_low_delta",
        title="Close Existing Shorts; Wait or Very Low Delta",
        stance="defensive",
        action="Close or roll existing short puts. Either stay flat or sell only 3-10 delta puts in small size.",
        description="VIX, IV rank and put skew are all high: the market is paying up for protection and risk is rising.",
        target=(2, 2, 2),
    ),
    Scenario(
        key="avoid_naked_puts",
        title="Avoid Aggressive Naked Puts",
        stance="defensive",
        action="No aggressive naked puts. If anything, tiny size at <=5 delta or defined-risk put spreads.",
        description="VIX is very high and IV rank and skew are extreme: tail risk is being priced aggressively.",
        target=(3, 3, 3),
    ),
    Scenario(
        key="scale_into_put_selling",
        title="Gradually Increase Put Selling",
        stance="short_vol",
        action="Scale into 10-20 delta puts, 30-60 DTE, in tranches as volatility keeps falling.",
        description="VIX is reversing from a panic spike while IV rank and skew are falling: premium is still rich but the crash phase is likely past.",
        target=None,
    ),
]

SCENARIO_BY_KEY: Dict[str, Scenario] = {s.key: s for s in SCENARIOS}


@dataclass
class IndexMetrics:
    value: Optional[float] = None
    date: Optional[str] = None
    level: Optional[str] = None
    iv_rank: Optional[float] = None
    percentile: Optional[float] = None
    sma5: Optional[float] = None
    sma20: Optional[float] = None
    change_5d: Optional[float] = None
    change_1d: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None


@dataclass
class MarketMetrics:
    vix: IndexMetrics = field(default_factory=IndexMetrics)
    skew: IndexMetrics = field(default_factory=IndexMetrics)
    iv_rank_level: Optional[str] = None
    iv_rank_change_5d: Optional[float] = None
    panic: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def summarize_series(series: Sequence[Tuple[str, float]], lookback: int = 252, change_days: int = 5) -> IndexMetrics:
    """Level-free statistics for any daily index series."""
    if not series:
        return IndexMetrics()
    values = values_of(series)
    window = values[-lookback:]
    return IndexMetrics(
        value=values[-1],
        date=series[-1][0],
        iv_rank=iv_rank(values, lookback),
        percentile=percentile_rank(values, lookback),
        sma5=sma(values, 5),
        sma20=sma(values, 20),
        change_5d=change_over(values, change_days),
        change_1d=change_over(values, 1),
        high_52w=max(window),
        low_52w=min(window),
    )


def compute_market_metrics(vix_series: Sequence[Tuple[str, float]], skew_series: Sequence[Tuple[str, float]], thresholds: dict) -> MarketMetrics:
    vix_cfg = thresholds.get("vix", {})
    ivr_cfg = thresholds.get("iv_rank", {})
    skew_cfg = thresholds.get("skew", {})
    panic_cfg = thresholds.get("panic_reversal", {})
    ivr_lookback = int(ivr_cfg.get("lookback_days", 252))
    skew_lookback = int(skew_cfg.get("lookback_days", 252))
    falling_days = int(panic_cfg.get("falling_lookback_days", 5))

    m = MarketMetrics()
    if vix_series:
        values = values_of(vix_series)
        window = values[-ivr_lookback:]
        m.vix = IndexMetrics(
            value=values[-1],
            date=vix_series[-1][0],
            level=classify_level(values[-1], [vix_cfg.get("low", 16), vix_cfg.get("medium", 22), vix_cfg.get("high", 30)], VIX_LEVELS),
            iv_rank=iv_rank(values, ivr_lookback),
            percentile=percentile_rank(values, ivr_lookback),
            sma5=sma(values, 5),
            sma20=sma(values, 20),
            change_5d=change_over(values, falling_days),
            change_1d=change_over(values, 1),
            high_52w=max(window),
            low_52w=min(window),
        )
        m.iv_rank_level = classify_level(
            m.vix.iv_rank, [ivr_cfg.get("low", 25), ivr_cfg.get("medium", 50), ivr_cfg.get("high", 75)], IVR_LEVELS
        )
        ranks = rank_series(values, ivr_lookback)
        if len(ranks) > falling_days and ranks[-1] is not None and ranks[-1 - falling_days] is not None:
            m.iv_rank_change_5d = ranks[-1] - ranks[-1 - falling_days]
        m.panic = detect_panic_reversal(
            vix_series,
            int(panic_cfg.get("peak_lookback_days", 15)),
            float(panic_cfg.get("min_peak", 30)),
            float(panic_cfg.get("min_drop_from_peak_pct", 15)),
        )
    if skew_series:
        values = values_of(skew_series)
        window = values[-skew_lookback:]
        m.skew = IndexMetrics(
            value=values[-1],
            date=skew_series[-1][0],
            level=classify_level(values[-1], [skew_cfg.get("low", 125), skew_cfg.get("normal", 140), skew_cfg.get("high", 150)], SKEW_LEVELS),
            iv_rank=iv_rank(values, skew_lookback),
            percentile=percentile_rank(values, skew_lookback),
            sma5=sma(values, 5),
            sma20=sma(values, 20),
            change_5d=change_over(values, falling_days),
            change_1d=change_over(values, 1),
            high_52w=max(window),
            low_52w=min(window),
        )
    return m


@dataclass
class RegimeResult:
    scenario: str
    title: str
    stance: str
    action: str
    description: str
    exact_match: bool
    confidence: float
    conditions: List[dict]
    notes: List[str]
    alternatives: List[dict]
    panic: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _index(levels: Sequence[str], label: Optional[str]) -> Optional[int]:
    return levels.index(label) if label in levels else None


def _distance(levels: Tuple[int, int, int], target: Tuple[int, int, int]) -> int:
    return sum(w * abs(a - b) for w, a, b in zip(DIMENSION_WEIGHTS, levels, target))


def classify_regime(m: MarketMetrics) -> RegimeResult:
    vix_idx = _index(VIX_LEVELS, m.vix.level)
    ivr_idx = _index(IVR_LEVELS, m.iv_rank_level)
    skew_idx = _index(SKEW_LEVELS, m.skew.level)
    notes: List[str] = []

    if vix_idx is None or ivr_idx is None or skew_idx is None:
        missing = [n for n, v in (("VIX", vix_idx), ("IV rank", ivr_idx), ("SKEW", skew_idx)) if v is None]
        scenario = SCENARIO_BY_KEY["close_shorts_low_delta"]
        return RegimeResult(
            scenario=scenario.key, title=scenario.title, stance=scenario.stance, action=scenario.action,
            description=scenario.description, exact_match=False, confidence=0.0, conditions=[],
            notes=[f"Missing market data for: {', '.join(missing)}. Defaulting to a defensive stance."],
            alternatives=[], panic=m.panic,
        )

    levels = (vix_idx, ivr_idx, skew_idx)
    panic_detected = bool(m.panic.get("detected"))
    ivr_falling = m.iv_rank_change_5d is not None and m.iv_rank_change_5d < 0
    skew_falling = m.skew.change_5d is not None and m.skew.change_5d < 0

    if panic_detected and ivr_falling and skew_falling:
        scenario = SCENARIO_BY_KEY["scale_into_put_selling"]
        conditions = [
            _condition("VIX", m.vix.value, m.vix.level, "PANIC REVERSAL", True,
                       f"peaked at {m.panic.get('peak'):.2f} on {m.panic.get('peak_date')}, now {m.panic.get('drop_from_peak_pct'):.0f}% lower"),
            _condition("IV Rank", m.vix.iv_rank, m.iv_rank_level, "FALLING", True, f"{m.iv_rank_change_5d:+.1f} pts over 5 days"),
            _condition("Put Skew", m.skew.value, m.skew.level, "FALLING", True, f"{m.skew.change_5d:+.2f} over 5 days"),
        ]
        return RegimeResult(
            scenario=scenario.key, title=scenario.title, stance=scenario.stance, action=scenario.action,
            description=scenario.description, exact_match=True, confidence=1.0, conditions=conditions,
            notes=["Scale in gradually: add a tranche only while VIX keeps making lower highs."],
            alternatives=[], panic=m.panic,
        )

    if panic_detected:
        pending = [name for name, ok in (("IV rank", ivr_falling), ("put skew", skew_falling)) if not ok]
        notes.append(
            f"VIX is reversing from a panic peak of {m.panic.get('peak'):.2f}, but {' and '.join(pending)} "
            f"{'is' if len(pending) == 1 else 'are'} not falling yet. Wait for confirmation before scaling into put selling."
        )

    ranked = []
    for scenario in SCENARIOS:
        if scenario.target is None:
            continue
        distance = _distance(levels, scenario.target)
        temperature_gap = abs(sum(levels) - sum(scenario.target))
        ranked.append((distance, temperature_gap, scenario))
    ranked.sort(key=lambda r: (r[0], r[1]))
    distance, _, best = ranked[0]
    exact = distance == 0
    confidence = max(0.0, 1.0 - distance / MAX_DISTANCE)

    conditions = [
        _condition("VIX", m.vix.value, m.vix.level, VIX_LEVELS[best.target[0]], vix_idx == best.target[0],
                   f"IV rank {m.vix.iv_rank:.0f}, 52w range {m.vix.low_52w:.1f}-{m.vix.high_52w:.1f}"),
        _condition("IV Rank", m.vix.iv_rank, m.iv_rank_level, IVR_LEVELS[best.target[1]], ivr_idx == best.target[1],
                   f"percentile {m.vix.percentile:.0f}"),
        _condition("Put Skew", m.skew.value, m.skew.level, SKEW_LEVELS[best.target[2]], skew_idx == best.target[2],
                   f"percentile {m.skew.percentile:.0f}, 5d change {m.skew.change_5d:+.2f}" if m.skew.change_5d is not None else ""),
    ]
    if not exact:
        mismatched = [c for c in conditions if not c["matched"]]
        notes.append(
            "Mixed signals - nearest scenario chosen. "
            + "; ".join(f"{c['name']} is {c['level']} (scenario expects {c['expected']})" for c in mismatched)
            + "."
        )
        notes.extend(_mixed_signal_advice(best.key, levels))

    alternatives = [
        {"scenario": s.key, "title": s.title, "distance": d, "confidence": round(max(0.0, 1 - d / MAX_DISTANCE), 2)}
        for d, _, s in ranked[1:3]
    ]
    return RegimeResult(
        scenario=best.key, title=best.title, stance=best.stance, action=best.action, description=best.description,
        exact_match=exact, confidence=round(confidence, 2), conditions=conditions, notes=notes,
        alternatives=alternatives, panic=m.panic,
    )


def _condition(name: str, value: Optional[float], level: Optional[str], expected: str, matched: bool, detail: str = "") -> dict:
    return {"name": name, "value": value, "level": level, "expected": expected, "matched": matched, "detail": detail}


def _mixed_signal_advice(scenario_key: str, levels: Tuple[int, int, int]) -> List[str]:
    vix_idx, ivr_idx, skew_idx = levels
    advice: List[str] = []
    if scenario_key == "buy_puts" and skew_idx >= 2:
        advice.append("Skew is elevated: OTM puts carry a premium, so favour strikes closer to the money or put spreads.")
    if scenario_key in ("selective_put_selling", "sell_20_delta_puts") and vix_idx >= 2:
        advice.append("VIX is elevated relative to this scenario: reduce size and lean to lower deltas.")
    if scenario_key in ("selective_put_selling", "sell_20_delta_puts", "scale_into_put_selling") and ivr_idx == 0:
        advice.append("IV rank is low: absolute premium is thin, so be selective and avoid over-sizing.")
    if scenario_key == "close_shorts_low_delta" and vix_idx <= 1:
        advice.append("VIX is not yet high: existing shorts may be kept but avoid adding new risk.")
    return advice
