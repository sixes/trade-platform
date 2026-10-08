from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from backend.analytics.greeks import put_delta, put_theta_per_day, strike_for_put_delta
from backend.analytics.indicators import classify_level
from backend.market_data.longbridge import OptionQuoteRow

log = logging.getLogger(__name__)

CHAIN_SKEW_LEVELS = ("LOW", "NORMAL", "HIGH", "EXTREME")


def strike_band_for_deltas(spot: float, delta_lo: float, delta_hi: float, dte: int, atm_iv: Optional[float],
                           skew_ratio: Optional[float], r: float, q: float, fallback: Sequence[float] = (0.6, 1.0)) -> Tuple[float, float]:
    """Strike range that should contain every put with |delta| in [delta_lo, delta_hi], with a safety margin."""
    if not atm_iv or atm_iv <= 0 or dte <= 0:
        return spot * fallback[0], spot * fallback[1]
    t = dte / 365.0
    skew = max(skew_ratio or 1.0, 1.0)
    # Far-OTM puts carry higher IV than ATM, which pushes the same delta further down: assume a steep skew.
    lo = strike_for_put_delta(spot, delta_lo, t, atm_iv * skew * 1.35, r, q)
    hi = strike_for_put_delta(spot, delta_hi, t, atm_iv * 0.8, r, q)
    lo = (lo or spot * fallback[0]) * 0.96
    hi = min((hi or spot) * 1.02, spot)
    if hi <= lo:
        return spot * fallback[0], spot
    return lo, hi


def subsample_strikes(items: Sequence, max_count: int) -> List:
    """Evenly thin a strike-sorted list down to max_count entries, keeping both ends."""
    items = list(items)
    if max_count <= 0 or len(items) <= max_count:
        return items
    if max_count == 1:
        return [items[-1]]
    step = (len(items) - 1) / (max_count - 1)
    picked = [items[round(i * step)] for i in range(max_count)]
    seen = set()
    return [p for p in picked if not (id(p) in seen or seen.add(id(p)))]


@dataclass
class PutCandidate:
    symbol: str
    underlying: str
    expiry: str
    dte: int
    strike: float
    spot: float
    moneyness_pct: float
    last: Optional[float]
    iv: float
    delta: float
    theta_per_day: Optional[float]
    open_interest: int
    volume: int
    contract_multiplier: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    spread_pct: Optional[float] = None
    price: Optional[float] = None
    price_source: str = "last"
    iv_ratio: Optional[float] = None
    premium_yield_pct: Optional[float] = None
    annualized_yield_pct: Optional[float] = None
    breakeven: Optional[float] = None
    cushion_pct: Optional[float] = None
    cost_pct_of_spot: Optional[float] = None
    cost_per_delta_pct: Optional[float] = None
    delta_target_gap: Optional[float] = None
    score: Optional[float] = None
    rank: Optional[int] = None
    flags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_candidates(rows: Sequence[OptionQuoteRow], spot: float, today: date, r: float, q: float) -> Tuple[List[PutCandidate], dict]:
    out: List[PutCandidate] = []
    skipped = {"expired": 0, "no_iv": 0, "no_delta": 0, "bad_status": 0}
    for row in rows:
        dte = (row.expiry - today).days
        if dte <= 0:
            skipped["expired"] += 1
            continue
        status = row.trade_status.lower()
        if "expired" in status or "delisted" in status or "halt" in status:
            skipped["bad_status"] += 1
            continue
        if row.iv is None:
            skipped["no_iv"] += 1
            continue
        t = dte / 365.0
        delta = put_delta(spot, row.strike, t, row.iv, r, q)
        if delta is None:
            skipped["no_delta"] += 1
            continue
        cand = PutCandidate(
            symbol=row.symbol,
            underlying=row.underlying,
            expiry=row.expiry.isoformat(),
            dte=dte,
            strike=row.strike,
            spot=spot,
            moneyness_pct=(row.strike / spot - 1) * 100.0,
            last=row.last if row.last and row.last > 0 else None,
            iv=row.iv,
            delta=delta,
            theta_per_day=put_theta_per_day(spot, row.strike, t, row.iv, r, q),
            open_interest=row.open_interest,
            volume=row.volume,
            contract_multiplier=row.contract_multiplier,
        )
        cand.price = cand.last
        recompute(cand)
        out.append(cand)
    return out, skipped


def recompute(c: PutCandidate) -> None:
    price = c.price
    if price is None or price <= 0:
        c.premium_yield_pct = c.annualized_yield_pct = c.breakeven = c.cushion_pct = None
        c.cost_pct_of_spot = c.cost_per_delta_pct = None
        return
    c.premium_yield_pct = price / c.strike * 100.0
    c.annualized_yield_pct = c.premium_yield_pct * 365.0 / max(c.dte, 1)
    c.breakeven = c.strike - price
    c.cushion_pct = (c.spot - c.breakeven) / c.spot * 100.0
    c.cost_pct_of_spot = price / c.spot * 100.0
    c.cost_per_delta_pct = c.cost_pct_of_spot / max(abs(c.delta), 1e-6)


def apply_depth(c: PutCandidate, bid: Optional[float], ask: Optional[float]) -> None:
    c.bid, c.ask = bid, ask
    if bid and ask and ask >= bid > 0:
        c.mid = (bid + ask) / 2
        c.spread_pct = (ask - bid) / c.mid * 100.0
        c.price = c.mid
        c.price_source = "mid"
    elif ask and ask > 0 and not bid:
        c.mid = None
        c.spread_pct = None
        c.price = c.last or ask
        c.price_source = "last" if c.last else "ask"
    recompute(c)


def reference_metrics(cands: Sequence[PutCandidate], reference_dte: int, skew_delta: float, skew_cuts: dict) -> dict:
    if not cands:
        return {}
    by_expiry: Dict[str, List[PutCandidate]] = {}
    for c in cands:
        by_expiry.setdefault(c.expiry, []).append(c)
    ref_expiry = min(by_expiry, key=lambda e: abs(by_expiry[e][0].dte - reference_dte))
    chain = sorted(by_expiry[ref_expiry], key=lambda c: c.strike)
    spot = chain[0].spot
    atm_iv = _interpolate(chain, key=lambda c: c.strike, target=spot, value=lambda c: c.iv)
    otm_iv = _interpolate(sorted(chain, key=lambda c: abs(c.delta)), key=lambda c: abs(c.delta), target=skew_delta, value=lambda c: c.iv)
    skew_ratio = (otm_iv / atm_iv) if atm_iv and otm_iv else None
    return {
        "expiry": ref_expiry,
        "dte": chain[0].dte,
        "atm_iv": atm_iv,
        "otm_put_iv": otm_iv,
        "otm_put_delta": skew_delta,
        "skew_ratio": skew_ratio,
        "skew_level": classify_level(
            skew_ratio,
            [skew_cuts.get("low", 1.08), skew_cuts.get("normal", 1.20), skew_cuts.get("high", 1.35)],
            CHAIN_SKEW_LEVELS,
        ),
        "strikes": len(chain),
    }


def _interpolate(sorted_items: Sequence[PutCandidate], key, target: float, value) -> Optional[float]:
    if not sorted_items:
        return None
    lower = [c for c in sorted_items if key(c) <= target]
    upper = [c for c in sorted_items if key(c) >= target]
    if lower and upper:
        a, b = lower[-1], upper[0]
        ka, kb = key(a), key(b)
        if kb == ka:
            return value(a)
        w = (target - ka) / (kb - ka)
        return value(a) * (1 - w) + value(b) * w
    nearest = min(sorted_items, key=lambda c: abs(key(c) - target))
    return value(nearest)


def atm_iv_by_expiry(cands: Sequence[PutCandidate], max_gap_pct: float = 3.0) -> Dict[str, float]:
    """Per-expiry ATM IV interpolated from the strikes bracketing spot (or the nearest strike within max_gap_pct)."""
    out: Dict[str, float] = {}
    by_expiry: Dict[str, List[PutCandidate]] = {}
    for c in cands:
        by_expiry.setdefault(c.expiry, []).append(c)
    for expiry, chain in by_expiry.items():
        chain = sorted(chain, key=lambda c: c.strike)
        spot = chain[0].spot
        lower = [c for c in chain if c.strike <= spot]
        upper = [c for c in chain if c.strike >= spot]
        if lower and upper:
            out[expiry] = _interpolate(chain, key=lambda c: c.strike, target=spot, value=lambda c: c.iv)
            continue
        nearest = min(chain, key=lambda c: abs(c.strike - spot))
        if abs(nearest.strike / spot - 1) * 100 <= max_gap_pct:
            out[expiry] = nearest.iv
    return out


def filter_candidates(cands: Sequence[PutCandidate], scenario_cfg: dict, atm_iv: Optional[float], enforce_spread: bool = False,
                      atm_by_expiry: Optional[Dict[str, float]] = None) -> List[PutCandidate]:
    dte_lo, dte_hi = scenario_cfg.get("dte", [30, 45])
    delta_lo, delta_hi = scenario_cfg.get("delta", [0.1, 0.3])
    min_oi = int(scenario_cfg.get("min_open_interest", 0))
    min_yield = scenario_cfg.get("min_annualized_yield_pct")
    max_spread = scenario_cfg.get("max_spread_pct")
    target_delta = scenario_cfg.get("target_delta")
    atm_by_expiry = atm_by_expiry or {}
    out: List[PutCandidate] = []
    for c in cands:
        if not (dte_lo <= c.dte <= dte_hi):
            continue
        if c.strike >= c.spot:
            continue
        if not (delta_lo <= abs(c.delta) <= delta_hi):
            continue
        if c.open_interest < min_oi:
            continue
        if c.price is None or c.price <= 0:
            continue
        if min_yield is not None and c.annualized_yield_pct is not None and c.annualized_yield_pct < float(min_yield):
            continue
        if enforce_spread and max_spread is not None and c.spread_pct is not None and c.spread_pct > float(max_spread):
            continue
        base_iv = atm_by_expiry.get(c.expiry) or atm_iv
        c.iv_ratio = (c.iv / base_iv) if base_iv else None
        c.delta_target_gap = abs(abs(c.delta) - float(target_delta)) if target_delta is not None else None
        out.append(c)
    return out


# (metric, higher_is_better, weight)
SCORE_WEIGHTS: Dict[str, List[Tuple[str, bool, float]]] = {
    "buy_puts": [("iv_ratio", False, 0.35), ("cost_per_delta_pct", False, 0.30), ("open_interest", True, 0.20), ("spread_pct", False, 0.15)],
    "selective_put_selling": [("annualized_yield_pct", True, 0.30), ("iv_ratio", True, 0.30), ("cushion_pct", True, 0.15), ("open_interest", True, 0.15), ("spread_pct", False, 0.10)],
    "sell_20_delta_puts": [("annualized_yield_pct", True, 0.30), ("delta_target_gap", False, 0.25), ("cushion_pct", True, 0.15), ("open_interest", True, 0.20), ("spread_pct", False, 0.10)],
    "close_shorts_low_delta": [("cushion_pct", True, 0.35), ("annualized_yield_pct", True, 0.25), ("open_interest", True, 0.25), ("spread_pct", False, 0.15)],
    "avoid_naked_puts": [("cushion_pct", True, 0.40), ("annualized_yield_pct", True, 0.20), ("open_interest", True, 0.25), ("spread_pct", False, 0.15)],
    "scale_into_put_selling": [("annualized_yield_pct", True, 0.30), ("iv_ratio", True, 0.15), ("cushion_pct", True, 0.20), ("open_interest", True, 0.20), ("spread_pct", False, 0.15)],
}


def _percentile_scores(values: List[Optional[float]], higher_is_better: bool) -> List[float]:
    known = sorted(v for v in values if v is not None)
    if not known:
        return [0.5] * len(values)
    if len(known) == 1:
        return [0.5 if v is None else 1.0 for v in values]
    out = []
    for v in values:
        if v is None:
            out.append(0.5)
            continue
        below = sum(1 for k in known if k < v)
        equal = sum(1 for k in known if k == v)
        pct = (below + 0.5 * equal) / len(known)
        out.append(pct if higher_is_better else 1 - pct)
    return out


def score_candidates(cands: List[PutCandidate], scenario_key: str, max_spread_pct: Optional[float] = None) -> List[PutCandidate]:
    weights = SCORE_WEIGHTS.get(scenario_key, SCORE_WEIGHTS["sell_20_delta_puts"])
    if not cands:
        return []
    totals = [0.0] * len(cands)
    for metric, higher, weight in weights:
        scores = _percentile_scores([getattr(c, metric) for c in cands], higher)
        for i, s in enumerate(scores):
            totals[i] += s * weight
    for c, total in zip(cands, totals):
        c.score = round(total * 100, 1)
        c.flags = _flags(c, max_spread_pct)
    ranked = sorted(cands, key=lambda c: c.score or 0, reverse=True)
    for i, c in enumerate(ranked, start=1):
        c.rank = i
    return ranked


def _flags(c: PutCandidate, max_spread_pct: Optional[float]) -> List[str]:
    flags: List[str] = []
    if c.spread_pct is not None and max_spread_pct is not None and c.spread_pct > float(max_spread_pct):
        flags.append("wide_spread")
    if c.volume == 0:
        flags.append("no_volume_today")
    if c.iv_ratio is not None:
        if c.iv_ratio >= 1.30:
            flags.append("rich_skew")
        elif c.iv_ratio <= 1.05:
            flags.append("cheap_iv")
    if c.price_source != "mid":
        flags.append("price_from_last")
    return flags
