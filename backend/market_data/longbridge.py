from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, List, Optional, Sequence, Tuple, TypeVar

from backend.config import Settings
from backend.market_data.ratelimit import QuotaState, SlidingWindowBudget, TokenBucket

log = logging.getLogger(__name__)

T = TypeVar("T")

OPTION_QUOTA_CODE = 301607  # "Too many option securities request within one minute"


class LongbridgeError(Exception):
    pass


class AuthError(LongbridgeError):
    pass


class QuotaExhausted(LongbridgeError):
    pass


class ScanCancelled(Exception):
    pass


def _f(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_iv(value) -> Optional[float]:
    iv = _f(value)
    if iv is None or iv <= 0:
        return None
    # Some feeds report IV as a percentage (e.g. 23.5) instead of a fraction (0.235).
    return iv / 100.0 if iv > 3 else iv


@dataclass
class OptionQuoteRow:
    symbol: str
    underlying: str
    expiry: date
    strike: float
    last: Optional[float]
    iv: Optional[float]
    open_interest: int
    volume: int
    trade_status: str
    contract_multiplier: float


@dataclass
class DepthRow:
    bid: Optional[float]
    ask: Optional[float]
    bid_size: int
    ask_size: int


class LongbridgeClient:
    """Synchronous wrapper around the LongPort QuoteContext with pacing, backoff and status reporting."""

    def __init__(self, settings: Settings, quota: Optional[QuotaState] = None):
        lb = settings.section("longbridge")
        self.quota = quota or QuotaState()
        self._bucket = TokenBucket(float(lb.get("requests_per_second", 6)), int(lb.get("burst", 6)))
        self.max_retries = int(lb.get("max_retries", 6))
        self.base_backoff = float(lb.get("base_backoff_seconds", 2))
        self.max_backoff = float(lb.get("max_backoff_seconds", 60))
        self.rate_limit_codes = {int(c) for c in lb.get("rate_limit_error_codes", [301606])}
        self.option_budget = SlidingWindowBudget(int(lb.get("option_contracts_per_minute", 500)), 60.0)
        self.batch_size = min(int(lb.get("option_quote_batch_size", 200)), 500, self.option_budget.limit)
        self._ctx = None
        self._ctx_lock = threading.Lock()

    def status(self) -> dict:
        snap = self.quota.snapshot()
        snap["option_contracts_last_minute"] = self.option_budget.used()
        snap["option_contracts_per_minute"] = self.option_budget.limit
        return snap

    # ------------------------------------------------------------------ connection
    def _context(self):
        with self._ctx_lock:
            if self._ctx is None:
                from longport import openapi as lo

                last_exc: Optional[Exception] = None
                for attempt in range(1, 4):
                    try:
                        config = lo.Config.from_env()
                        self._ctx = lo.QuoteContext(config)
                        break
                    except Exception as exc:  # credentials missing/expired, network down
                        last_exc = exc
                        message = self._describe_error(exc)
                        if self._is_auth(getattr(exc, "code", None), message):
                            self.quota.set_auth_error(message)
                            log.error("Longbridge authentication failed: %s", message)
                            raise AuthError(message) from exc
                        log.warning("Longbridge connection attempt %d/3 failed: %s", attempt, message)
                        time.sleep(1.5 * attempt)
                if self._ctx is None:
                    message = self._describe_error(last_exc) if last_exc else "unknown error"
                    self.quota.set_error(f"connection failed: {message}")
                    raise LongbridgeError(f"could not connect to Longbridge: {message}")
                self.quota.set_connected(True)
                log.info("Longbridge QuoteContext connected")
            return self._ctx

    def reset(self) -> None:
        with self._ctx_lock:
            self._ctx = None

    def ping(self) -> dict:
        ctx = self._context()
        quotes = self._call("quote", lambda: ctx.quote(["SPY.US"]))
        q = quotes[0]
        return {"symbol": q.symbol, "last": _f(q.last_done), "timestamp": str(q.timestamp)}

    # ------------------------------------------------------------------ call wrapper
    def _call(self, method: str, fn: Callable[[], T], cancel: Optional[threading.Event] = None) -> T:
        from longport import openapi as lo

        attempt = 0
        while True:
            wait = self._bucket.reserve()
            if wait > 0.05:
                self.quota.set_waiting("Pacing requests to stay within the Longbridge rate limit", wait)
                self._sleep(wait, cancel)
            self._check_cancel(cancel)
            started = time.monotonic()
            try:
                result = fn()
            except lo.OpenApiException as exc:
                code = getattr(exc, "code", None)
                message = self._describe_error(exc)
                if self._is_option_quota(code, message):
                    attempt += 1
                    self.quota.note_rate_limited()
                    if attempt > self.max_retries:
                        self.quota.set_error(message)
                        raise QuotaExhausted(f"{method}: option-quote quota still exhausted after {self.max_retries} retries") from exc
                    self.option_budget.penalize()
                    wait = self.option_budget.window
                    log.warning("%s hit the option-contracts-per-minute quota (%s); waiting %.0fs (attempt %d/%d)",
                                method, message, wait, attempt, self.max_retries)
                    self.quota.set_waiting(
                        f"Longbridge option-quote quota reached (max {self.option_budget.limit} contracts per minute); waiting for the window to reset",
                        wait,
                    )
                    self._sleep(wait, cancel)
                    continue
                if self._is_rate_limit(code, message):
                    attempt += 1
                    self.quota.note_rate_limited()
                    if attempt > self.max_retries:
                        self.quota.set_error(message)
                        raise QuotaExhausted(f"{method}: rate limit persisted after {self.max_retries} retries") from exc
                    backoff = min(self.base_backoff * (2 ** (attempt - 1)), self.max_backoff)
                    log.warning("%s rate limited (%s); waiting %.1fs (attempt %d/%d)", method, message, backoff, attempt, self.max_retries)
                    self.quota.set_waiting("Longbridge API quota reached; waiting before retrying", backoff)
                    self._sleep(backoff, cancel)
                    continue
                if self._is_auth(code, message):
                    self.quota.set_auth_error(message)
                    self.reset()
                    raise AuthError(message) from exc
                self.quota.set_error(f"{method}: {message}")
                raise LongbridgeError(f"{method}: {message}") from exc
            finally:
                self.quota.clear_waiting()
            self.quota.record_call(method)
            log.debug("%s ok in %.0f ms", method, (time.monotonic() - started) * 1000)
            return result

    def _is_rate_limit(self, code, message: str) -> bool:
        if code in self.rate_limit_codes:
            return True
        lowered = message.lower()
        return "rate limit" in lowered or "too many requests" in lowered or "frequency" in lowered

    @staticmethod
    def _is_option_quota(code, message: str) -> bool:
        if code == OPTION_QUOTA_CODE:
            return True
        lowered = message.lower()
        return "option securities" in lowered and ("minute" in lowered or "too many" in lowered)

    @staticmethod
    def _is_auth(code, message: str) -> bool:
        try:
            if 401000 <= int(code) < 402000 or int(code) in (401, 403):
                return True
        except (TypeError, ValueError):
            pass
        lowered = message.lower()
        return "token" in lowered or "unauthorized" in lowered or "permission" in lowered

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None) or str(exc)
        return f"code={code} {message}" if code is not None else str(message)

    @staticmethod
    def _check_cancel(cancel: Optional[threading.Event]) -> None:
        if cancel is not None and cancel.is_set():
            raise ScanCancelled()

    def _sleep(self, seconds: float, cancel: Optional[threading.Event]) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if cancel is not None:
                if cancel.wait(min(remaining, 0.25)):
                    raise ScanCancelled()
            else:
                time.sleep(min(remaining, 0.25))

    # ------------------------------------------------------------------ data access
    def spot(self, symbol: str, cancel: Optional[threading.Event] = None) -> dict:
        ctx = self._context()
        quotes = self._call("quote", lambda: ctx.quote([symbol]), cancel)
        if not quotes:
            raise LongbridgeError(f"no quote returned for {symbol}")
        q = quotes[0]
        last = _f(q.last_done)
        prev_close = _f(q.prev_close)
        return {
            "symbol": q.symbol,
            "last": last,
            "prev_close": prev_close,
            "change_pct": ((last / prev_close - 1) * 100) if last and prev_close else None,
            "trade_status": str(getattr(q, "trade_status", "")),
            "timestamp": q.timestamp.isoformat() if hasattr(q.timestamp, "isoformat") else str(q.timestamp),
        }

    def expiry_dates(self, symbol: str, cancel: Optional[threading.Event] = None) -> List[date]:
        ctx = self._context()
        dates = self._call("option_chain_expiry_date_list", lambda: ctx.option_chain_expiry_date_list(symbol), cancel)
        return sorted(dates)

    def put_strikes(self, symbol: str, expiry: date, lo_strike: float, hi_strike: float,
                    cancel: Optional[threading.Event] = None) -> List[Tuple[float, str]]:
        """(strike, put_symbol) pairs for standard contracts within the strike band, ascending by strike."""
        ctx = self._context()
        rows = self._call("option_chain_info_by_date", lambda: ctx.option_chain_info_by_date(symbol, expiry), cancel)
        out: List[Tuple[float, str]] = []
        for row in rows:
            if not getattr(row, "standard", True) or not row.put_symbol:
                continue
            strike = _f(row.price)
            if strike is None or strike < lo_strike or strike > hi_strike:
                continue
            out.append((strike, row.put_symbol))
        out.sort(key=lambda p: p[0])
        return out

    def option_quotes(self, symbols: Sequence[str], cancel: Optional[threading.Event] = None,
                      progress: Optional[Callable[[int, int], None]] = None) -> List[OptionQuoteRow]:
        ctx = self._context()
        symbols = list(symbols)
        batches = [symbols[i:i + self.batch_size] for i in range(0, len(symbols), self.batch_size)]
        rows: List[OptionQuoteRow] = []
        for idx, batch in enumerate(batches):
            self._wait_for_option_budget(len(batch), cancel)
            quotes = self._call("option_quote", lambda b=batch: ctx.option_quote(b), cancel)
            for q in quotes:
                strike = _f(q.strike_price)
                if strike is None:
                    continue
                rows.append(OptionQuoteRow(
                    symbol=q.symbol,
                    underlying=str(q.underlying_symbol),
                    expiry=q.expiry_date,
                    strike=strike,
                    last=_f(q.last_done),
                    iv=normalize_iv(q.implied_volatility),
                    open_interest=int(q.open_interest or 0),
                    volume=int(q.volume or 0),
                    trade_status=str(q.trade_status),
                    contract_multiplier=_f(q.contract_multiplier) or 100.0,
                ))
            if progress:
                progress(idx + 1, len(batches))
        return rows

    def _wait_for_option_budget(self, units: int, cancel: Optional[threading.Event]) -> None:
        while True:
            wait = self.option_budget.reserve(units)
            if wait <= 0:
                return
            log.info("Option-quote budget: %d/%d contracts used this minute; waiting %.0fs before quoting %d more",
                     self.option_budget.used(), self.option_budget.limit, wait, units)
            self.quota.set_waiting(
                f"Longbridge option-quote quota: {self.option_budget.used()}/{self.option_budget.limit} contracts used this minute; "
                f"waiting for room to quote {units} more",
                wait,
            )
            try:
                self._sleep(wait + 0.2, cancel)
            finally:
                self.quota.clear_waiting()

    def depth(self, symbol: str, cancel: Optional[threading.Event] = None) -> DepthRow:
        ctx = self._context()
        d = self._call("depth", lambda: ctx.depth(symbol), cancel)
        bids = [b for b in (d.bids or []) if _f(b.price)]
        asks = [a for a in (d.asks or []) if _f(a.price)]
        bid = _f(bids[0].price) if bids else None
        ask = _f(asks[0].price) if asks else None
        return DepthRow(bid=bid, ask=ask, bid_size=int(bids[0].volume or 0) if bids else 0, ask_size=int(asks[0].volume or 0) if asks else 0)

    def daily_closes(self, symbol: str, count: int = 260, cancel: Optional[threading.Event] = None,
                     is_option: bool = False) -> List[Tuple[str, float]]:
        """(ISO day, close) for the last `count` daily bars, ascending."""
        from longport import openapi as lo

        ctx = self._context()
        if is_option:
            self._wait_for_option_budget(1, cancel)
        bars = self._call("candlesticks", lambda: ctx.candlesticks(symbol, lo.Period.Day, min(int(count), 1000), lo.AdjustType.NoAdjust), cancel)
        out: List[Tuple[str, float]] = []
        for bar in bars or []:
            close = _f(bar.close)
            if close is None or close <= 0:
                continue
            stamp = bar.timestamp
            day = stamp.date().isoformat() if hasattr(stamp, "date") else str(stamp)[:10]
            out.append((day, close))
        out.sort(key=lambda p: p[0])
        return out

    def intraday_bars(self, symbol: str, period_name: str = "Min_5", count: int = 160,
                      cancel: Optional[threading.Event] = None) -> List[Tuple[datetime, float]]:
        """(timestamp, close) regular-session minute bars, ascending. Timestamps are as returned by the SDK (server-local)."""
        from longport import openapi as lo

        ctx = self._context()
        period = getattr(lo.Period, period_name)
        bars = self._call("candlesticks", lambda: ctx.candlesticks(symbol, period, min(int(count), 1000), lo.AdjustType.NoAdjust), cancel)
        out: List[Tuple[datetime, float]] = []
        for bar in bars or []:
            close = _f(bar.close)
            if close is None or close <= 0:
                continue
            session = str(getattr(bar, "trade_session", "") or "")
            if session and "Intraday" not in session:
                continue
            out.append((bar.timestamp, close))
        out.sort(key=lambda p: p[0])
        return out
