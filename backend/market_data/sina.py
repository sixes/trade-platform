"""Sina Finance global commodity quotes (伦敦金 XAU, 伦敦银 XAG, 布伦特原油 OIL, 纽约原油 CL, 美铜 HG, ...).

Three endpoints, all free and keyless (they only require a finance.sina.com.cn Referer):
  - hq.sinajs.cn/list=hf_XAU,hf_XAG   live quotes, one request for many symbols, GBK encoded
  - GlobalService.getMink              1/5/15/30/60-minute bars (~1000 bars), Beijing time
  - GlobalFuturesService.getGlobalFuturesDailyKLine   daily OHLC history back to 2006 for spot metals
Quotes are cached for a few seconds so a page polling every 5 s costs one upstream request per poll.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

log = logging.getLogger(__name__)

HQ_URL = "https://hq.sinajs.cn/list={codes}"
MINK_URL = "https://gu.sina.cn/ft/api/jsonp.php/var%20_x=/GlobalService.getMink?symbol={symbol}&type={minutes}"
DAILY_URL = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={symbol}"
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}
SESSION_START_HOUR_BEIJING = 6  # Sina rolls these global quotes to a new trading day at 06:00 Beijing (18:00 ET)


def beijing():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8))


def eastern():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:
        return timezone(timedelta(hours=-4))


@dataclass
class SinaQuote:
    symbol: str
    name: str
    last: float
    bid: Optional[float]
    ask: Optional[float]
    high: Optional[float]
    low: Optional[float]
    open: Optional[float]
    prev_close: Optional[float]
    as_of: str  # ISO datetime in US Eastern

    def scaled(self, factor: float) -> "SinaQuote":
        if factor == 1.0:
            return self
        f = lambda v: None if v is None else v * factor  # noqa: E731
        return SinaQuote(self.symbol, self.name, self.last * factor, f(self.bid), f(self.ask), f(self.high), f(self.low), f(self.open), f(self.prev_close), self.as_of)


def _num(value) -> Optional[float]:
    try:
        v = float(value)
        return v if v != 0 else None
    except (TypeError, ValueError):
        return None


def parse_hq(text: str) -> Dict[str, SinaQuote]:
    """Parse `var hq_str_hf_XAU="last,prev_settle,bid,ask,high,low,time,prev_close,open,oi,bidvol,askvol,date,name";` lines."""
    out: Dict[str, SinaQuote] = {}
    tz_bj, tz_et = beijing(), eastern()
    for match in re.finditer(r'hq_str_hf_([A-Za-z0-9]+)="([^"]*)"', text):
        symbol, body = match.group(1).upper(), match.group(2)
        fields = body.split(",")
        if len(fields) < 14:
            continue
        last = _num(fields[0])
        if last is None:
            continue
        stamp = None
        try:
            stamp = datetime.strptime(f"{fields[12]} {fields[6]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_bj).astimezone(tz_et)
        except ValueError:
            stamp = datetime.now(tz_et)
        out[symbol] = SinaQuote(
            symbol=symbol, name=fields[13].strip(), last=last, bid=_num(fields[2]), ask=_num(fields[3]),
            high=_num(fields[4]), low=_num(fields[5]), open=_num(fields[8]), prev_close=_num(fields[7]),
            as_of=stamp.isoformat(timespec="minutes"),
        )
    return out


def parse_jsonp(text: str):
    match = re.search(r"\((.*)\)\s*;?\s*$", text, re.S)
    if not match:
        raise ValueError("not a JSONP payload")
    data = json.loads(match.group(1))
    if isinstance(data, dict) and "__ERROR" in data:
        raise ValueError(f"sina error: {data.get('__ERRORMSG')}")
    return data


def parse_minute_bars(data, scale: float = 1.0) -> List[Tuple[str, float]]:
    """Minute bars `{"d": "2026-09-24 16:00:00", "c": "4280.26", ...}` (Beijing) -> (ET ISO, close)."""
    tz_bj, tz_et = beijing(), eastern()
    out: List[Tuple[str, float]] = []
    for bar in data or []:
        try:
            stamp = datetime.strptime(bar["d"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_bj).astimezone(tz_et)
            close = float(bar["c"]) * scale
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            out.append((stamp.isoformat(timespec="minutes"), close))
    out.sort()
    return out


def parse_daily(data, scale: float = 1.0) -> List[Tuple[str, float]]:
    out: List[Tuple[str, float]] = []
    for bar in data or []:
        try:
            close = float(bar["close"]) * scale
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            out.append((str(bar["date"]), close))
    out.sort()
    return out


def session_start(now_et: datetime) -> datetime:
    """Start (in ET) of the current Sina trading day, which rolls at 06:00 Beijing."""
    now_bj = now_et.astimezone(beijing())
    start_bj = now_bj.replace(hour=SESSION_START_HOUR_BEIJING, minute=0, second=0, microsecond=0)
    if now_bj < start_bj:
        start_bj -= timedelta(days=1)
    return start_bj.astimezone(eastern())


class SinaClient:
    def __init__(self, quote_ttl: float = 4.0, bars_ttl: float = 60.0, timeout: float = 10.0):
        self.quote_ttl = quote_ttl
        self.bars_ttl = bars_ttl
        self.timeout = timeout
        self._quotes: Dict[str, Tuple[float, SinaQuote]] = {}
        self._bars: Dict[Tuple[str, int, float], Tuple[float, List[Tuple[str, float]]]] = {}
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None
        self.requests = 0

    def _get(self, url: str) -> str:
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=HEADERS) as client:
            resp = client.get(url)
            resp.raise_for_status()
            self.requests += 1
            raw = resp.content
            try:
                return raw.decode("gbk")
            except UnicodeDecodeError:
                return raw.decode("utf-8", errors="replace")

    def quotes(self, symbols: Sequence[str]) -> Dict[str, SinaQuote]:
        """Fresh quotes for `symbols`; stale ones are refreshed together in a single request."""
        wanted = [s.upper() for s in symbols]
        now = time.time()
        with self._lock:
            stale = [s for s in wanted if s not in self._quotes or now - self._quotes[s][0] >= self.quote_ttl]
        if stale:
            try:
                text = self._get(HQ_URL.format(codes=",".join(f"hf_{s}" for s in stale)))
                parsed = parse_hq(text)
                with self._lock:
                    for symbol, quote in parsed.items():
                        self._quotes[symbol] = (time.time(), quote)
                self.last_error = None
                missing = [s for s in stale if s not in parsed]
                if missing:
                    log.warning("Sina returned no quote for %s", ", ".join(missing))
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Sina quote request failed: %s", exc)
        with self._lock:
            return {s: self._quotes[s][1] for s in wanted if s in self._quotes}

    def minute_bars(self, symbol: str, minutes: int = 5, scale: float = 1.0) -> List[Tuple[str, float]]:
        key = (symbol.upper(), int(minutes), float(scale))
        with self._lock:
            cached = self._bars.get(key)
            if cached and time.time() - cached[0] < self.bars_ttl:
                return cached[1]
        bars = parse_minute_bars(parse_jsonp(self._get(MINK_URL.format(symbol=symbol.upper(), minutes=int(minutes)))), scale)
        with self._lock:
            self._bars[key] = (time.time(), bars)
        return bars

    def daily(self, symbol: str, scale: float = 1.0) -> List[Tuple[str, float]]:
        return parse_daily(parse_jsonp(self._get(DAILY_URL.format(symbol=symbol.upper()))), scale)
