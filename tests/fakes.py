"""Shared fakes: a synthetic option chain and a Longbridge client stand-in that never touches the network."""
from datetime import date, timedelta
from typing import Dict, List, Optional

from backend.analytics.greeks import put_price
from backend.config import load_settings
from backend.market_data.longbridge import DepthRow, OptionQuoteRow
from backend.market_data.ratelimit import QuotaState

_ANALYTICS = load_settings().section("analytics")
R, Q = float(_ANALYTICS.get("risk_free_rate", 0.04)), float(_ANALYTICS.get("dividend_yield", 0.0))


def iv_for(spot: float, strike: float, atm_iv: float, slope: float) -> float:
    return atm_iv * (1 + slope * max(0.0, (spot - strike) / spot))


class FakeLongbridge:
    def __init__(self, spot: float = 500.0, atm_iv: float = 0.18, slope: float = 1.5, today: Optional[date] = None):
        from backend.jobs import market_today

        self.spot_price = spot
        self.atm_iv = atm_iv
        self.slope = slope
        self.today = today or market_today()
        self.quota = QuotaState()
        self.calls: List[str] = []
        self.option_contracts_quoted = 0
        self.expiries = [self.today + timedelta(days=d) for d in (7, 14, 21, 31, 38, 45, 59, 73, 94, 122, 185)]
        self._rows: Dict[str, OptionQuoteRow] = {}
        for expiry in self.expiries:
            strike = 250.0
            while strike <= spot * 1.10:
                sym = f"SPY{expiry.strftime('%y%m%d')}P{int(strike * 1000):08d}.US"
                dte = (expiry - self.today).days
                iv = iv_for(spot, strike, atm_iv, slope)
                price = put_price(spot, strike, dte / 365.0, iv, R, Q)
                self._rows[sym] = OptionQuoteRow(
                    symbol=sym, underlying="SPY.US", expiry=expiry, strike=strike,
                    last=round(price, 2) if price and price > 0.01 else None, iv=iv,
                    open_interest=int(200 + (strike % 50) * 40), volume=int(strike % 7) * 10,
                    trade_status="TradeStatus.Normal", contract_multiplier=100.0,
                )
                strike += 5.0

    def _record(self, method: str) -> None:
        self.calls.append(method)
        self.quota.record_call(method)

    def spot(self, symbol, cancel=None):
        self._record("quote")
        return {"symbol": symbol, "last": self.spot_price, "prev_close": self.spot_price * 0.99, "change_pct": 1.0,
                "trade_status": "Normal", "timestamp": "2026-09-23T15:00:00"}

    def expiry_dates(self, symbol, cancel=None):
        self._record("option_chain_expiry_date_list")
        return list(self.expiries)

    def put_strikes(self, symbol, expiry, lo, hi, cancel=None):
        self._record("option_chain_info_by_date")
        return sorted((r.strike, s) for s, r in self._rows.items() if r.expiry == expiry and lo <= r.strike <= hi)

    def option_quotes(self, symbols, cancel=None, progress=None):
        batches = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]
        out = []
        for i, batch in enumerate(batches):
            self._record("option_quote")
            self.option_contracts_quoted += len(batch)
            out.extend(self._rows[s] for s in batch)
            if progress:
                progress(i + 1, len(batches))
        return out

    def depth(self, symbol, cancel=None):
        self._record("depth")
        row = self._rows[symbol]
        mid = row.last or 0.05
        half = max(0.01, mid * 0.02)
        return DepthRow(bid=round(mid - half, 2), ask=round(mid + half, 2), bid_size=50, ask_size=60)

    def daily_closes(self, symbol, count=260, cancel=None, is_option=False):
        """Synthetic year of history: the underlying drifts, option closes are BS prices at a known daily IV."""
        self._record("candlesticks")
        days = [self.today - timedelta(days=i) for i in range(count * 7 // 5)]
        days = sorted(d for d in days if d.weekday() < 5)[-count:]
        out = []
        for i, day in enumerate(days):
            spot = self.spot_price * (0.85 + 0.15 * i / max(len(days) - 1, 1))
            if not is_option:
                out.append((day.isoformat(), round(spot, 2)))
                continue
            row = self._rows[symbol]
            t = (row.expiry - day).days / 365.0
            if t <= 0:
                continue
            iv = self.history_iv(day)
            price = put_price(spot, row.strike, t, iv, R, Q)
            if price and price > 0.01:
                out.append((day.isoformat(), round(price, 2)))
        return out

    def history_iv(self, day):
        """Deterministic IV path: low early in the window, a spike in the middle, settling near atm_iv today."""
        age = (self.today - day).days
        if 100 <= age <= 130:
            return self.atm_iv * 1.8
        return self.atm_iv * (0.8 + 0.2 * (1 - age / 400))
