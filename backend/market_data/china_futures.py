from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

PAIRS = (
    ("IF", "IF0", "sh000300", "CSI 300", "IF main contract"),
    ("IH", "IH0", "sh000016", "SSE 50", "IH main contract"),
    ("IC", "IC0", "sh000905", "CSI 500", "IC main contract"),
    ("IM", "IM0", "sh000852", "CSI 1000", "IM main contract"),
)

RANGES = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60}


def _date_range(range_key: str) -> Tuple[str, str]:
    months = RANGES.get(range_key, 12)
    end = datetime.now(SHANGHAI).date()
    year, month = divmod(end.year * 12 + end.month - 1 - months, 12)
    month += 1
    start = date(year, month, min(end.day, 28))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


class ChinaFuturesService:
    def __init__(self, ttl_minutes: float = 30):
        self._ttl = ttl_minutes * 60
        self._cache: Dict[Tuple[str, str], Tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def snapshot(self, range_key: str = "1y") -> dict:
        if range_key not in RANGES:
            range_key = "1y"
        start, end = _date_range(range_key)
        pairs = {}
        for code, futures_sym, index_code, index_name, futures_name in PAIRS:
            pairs[code] = self._cached_pair(code, futures_sym, index_code,
                                            index_name, futures_name, start, end)
        return {
            "range": range_key,
            "pairs": pairs,
            "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        }

    def _cached_pair(self, code: str, futures_sym: str, index_code: str,
                     index_name: str, futures_name: str,
                     start: str, end: str) -> dict:
        cache_key = (code, start)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < self._ttl:
                return cached[1]
        try:
            result = self._fetch_pair(code, futures_sym, index_code,
                                      index_name, futures_name, start, end)
        except Exception:
            time.sleep(2)
            try:
                result = self._fetch_pair(code, futures_sym, index_code,
                                          index_name, futures_name, start, end)
            except Exception as exc:
                log.warning("China futures %s unavailable: %s", code, exc)
                result = {"code": code, "futures_name": futures_name,
                          "index_name": index_name, "error": str(exc)}
        with self._lock:
            self._cache[cache_key] = (time.monotonic(), result)
        return result

    def _fetch_pair(self, code: str, futures_sym: str, index_code: str,
                    index_name: str, futures_name: str,
                    start: str, end: str) -> dict:
        import akshare as ak

        futures_df = ak.futures_main_sina(symbol=futures_sym,
                                          start_date=start, end_date=end)
        index_df = ak.stock_zh_index_daily(symbol=index_code)

        f_col_date = "日期" if "日期" in futures_df.columns else "date"
        f_col_close = "收盘价" if "收盘价" in futures_df.columns else "close"

        start_iso = f"{start[:4]}-{start[4:6]}-{start[6:]}"

        f_map: Dict[str, float] = {}
        for _, row in futures_df.iterrows():
            d = str(row[f_col_date])[:10]
            v = float(row[f_col_close])
            if math.isfinite(v) and v > 0:
                f_map[d] = v

        i_map: Dict[str, float] = {}
        for _, row in index_df.iterrows():
            d = str(row["date"])[:10]
            if d < start_iso:
                continue
            v = float(row["close"])
            if math.isfinite(v) and v > 0:
                i_map[d] = v

        dates = sorted(set(f_map) & set(i_map))
        if not dates:
            raise ValueError("No overlapping dates between futures and index")

        futures_series = [{"t": d, "v": f_map[d]} for d in dates]
        index_series = [{"t": d, "v": i_map[d]} for d in dates]
        basis_series = [{"t": d, "v": round(f_map[d] - i_map[d], 2)} for d in dates]
        basis_pct_series = [{"t": d, "v": round((f_map[d] / i_map[d] - 1) * 100, 4)}
                           for d in dates]

        last = dates[-1]
        return {
            "code": code,
            "futures_name": futures_name,
            "index_name": index_name,
            "futures": futures_series,
            "index": index_series,
            "basis": basis_series,
            "basis_pct": basis_pct_series,
            "metrics": {
                "futures_last": f_map[last],
                "index_last": i_map[last],
                "basis_last": round(f_map[last] - i_map[last], 2),
                "basis_pct": round((f_map[last] / i_map[last] - 1) * 100, 4),
                "date": last,
                "points": len(dates),
            },
        }
