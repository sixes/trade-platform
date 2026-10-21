from __future__ import annotations

import contextlib
import json
import math
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.market_data.china_etfs import FUNDS, NAV_URL, SHANGHAI, number, reject_constant, source_date

HISTORY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
DISCOVERY_URL = "https://web.ifzq.gtimg.cn/other/klineweb/klineWeb/weekTrends"


def main():
    output = sys.stdout
    line = sys.stdin.readline(8192)
    request = json.loads(line, parse_constant=reject_constant)
    roster = {f[0]: f[1].lower() + f[0] for f in FUNDS}
    if not line.endswith("\n") or not isinstance(request, dict) or set(request) != {"tasks", "as_of"}:
        raise ValueError("Invalid fund batch")
    tasks = request["tasks"]
    if (not isinstance(tasks, list) or not 1 <= len(tasks) <= 2 * len(roster)
            or any(not isinstance(task, dict) or set(task) != {"code", "part"}
                   or not isinstance(task["code"], str) or task["code"] not in roster
                   or task["part"] not in ("close", "nav") for task in tasks)
            or len({(task["code"], task["part"]) for task in tasks}) != len(tasks)):
        raise ValueError("Invalid fund tasks")
    now = datetime.fromisoformat(request["as_of"])
    if now.utcoffset() != timedelta(hours=8):
        raise ValueError("Missing Shanghai reference timezone")
    now = now.astimezone(SHANGHAI)
    start, today = now.date() - timedelta(days=45), now.date()
    local = threading.local()

    with contextlib.redirect_stdout(sys.stderr):
        import akshare as ak
        import requests

        with httpx.Client(timeout=httpx.Timeout(5, connect=3), trust_env=False, follow_redirects=False,
                          limits=httpx.Limits(max_connections=2), headers={"User-Agent": "Mozilla/5.0"}) as client:
            def get_nav(url, params, kwargs):
                state = local.state
                page = max(1, state["requests"])
                headers = kwargs.get("headers")
                if (url != NAV_URL or set(kwargs) != {"headers"} or not isinstance(headers, dict)
                        or set(headers) != {"User-Agent", "Referer"}
                        or headers["Referer"] != f"https://fundf10.eastmoney.com/jjjz_{state['code']}.html"
                        or not isinstance(headers["User-Agent"], str)
                        or not isinstance(params, dict) or type(params.get("_")) is not int
                        or not 0 <= params["_"] < 10 ** 16
                        or type(params.get("pageIndex")) not in (str, int)
                        or str(params["pageIndex"]) != str(page)
                        or params != {"fundCode": state["code"], "pageIndex": params["pageIndex"],
                                      "pageSize": "20", "startDate": start.isoformat(),
                                      "endDate": today.isoformat(), "_": params["_"]}
                        or state["requests"] >= 4
                        or state["requests"] and (page > state["pages"] or params["_"] != state["cache_key"])):
                    raise ValueError("Unapproved NAV request or exhausted budget")
                state["requests"] += 1
                with client.stream("GET", url, params=params, headers=headers) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            raise ValueError("Oversized NAV response")
                    received_at = datetime.now(SHANGHAI)
                    payload = json.loads(bytes(body), parse_constant=reject_constant)
                    if (not isinstance(payload, dict) or type(payload.get("ErrCode")) is not int
                            or payload["ErrCode"] != 0 or payload.get("ErrMsg") not in (None, "")
                            or type(payload.get("TotalCount")) is not int or not 0 <= payload["TotalCount"] <= 46
                            or type(payload.get("PageSize")) is not int or payload["PageSize"] != 20
                            or type(payload.get("PageIndex")) is not int or payload["PageIndex"] != page):
                        raise ValueError("Invalid NAV envelope or pagination")
                    total = payload["TotalCount"]
                    data = payload.get("Data")
                    rows = data.get("LSJZList") if isinstance(data, dict) else None
                    if not isinstance(rows, list) or len(rows) != min(20, max(0, total - (page - 1) * 20)):
                        raise ValueError("Incomplete NAV page")
                    if state["requests"] == 1:
                        state.update(total=total, pages=math.ceil(total / 20), probe=rows, cache_key=params["_"])
                    elif total != state["total"] or page == 1 and rows != state["probe"]:
                        raise ValueError("NAV pages changed during retrieval")
                    seen = set()
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(row.get("FSRQ"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["FSRQ"]):
                            raise ValueError("Invalid NAV valuation date")
                        day = source_date(row["FSRQ"], today)
                        if day is None or day < start or day in seen:
                            raise ValueError("Conflicting or out-of-range NAV dates")
                        seen.add(day)
                        if state["requests"] == 1:
                            continue
                        if day in state["nav_rows"]:
                            raise ValueError("Duplicate NAV observations across pages")
                        state["nav_rows"][day] = row
                        action_fields = ("FHFCZ", "FHFCZ10", "FHFCBZ", "DTYPE", "FHSP")
                        if re.search(r"拆分|分拆|折算|合并", str(row.get("FHSP", ""))):
                            state["share_events"].add(day.isoformat())
                        elif any(field not in row or row[field] not in (None, "") for field in action_fields):
                            state["uncertain_events"].add(day.isoformat())
                        value = number(row.get("DWJZ"))
                        if value is not None and value > 0:
                            state["navs"][day] = value, str(response.url), received_at
                    return httpx.Response(response.status_code, content=bytes(body), request=response.request)

            def get(url, params=None, **kwargs):
                state = local.state
                if state["part"] == "nav":
                    return get_nav(url, params, kwargs)
                symbol = state["symbol"]
                if set(kwargs) - {"timeout"} or not isinstance(params, dict):
                    raise ValueError("Unexpected AKShare request")
                discovery = url == DISCOVERY_URL and params == {
                    "code": symbol, "type": "qfq", "_var": "trend_qfq", "r": "0.3506048543943414",
                }
                discovery_fallback = url == HISTORY_URL and params == {
                    "_var": "kline_dayqfq", "param": f"{symbol},day,,,320,qfq", "r": "0.751892490072597",
                }
                history = url == HISTORY_URL and any(params == {
                    "_var": f"kline_day{year}",
                    "param": f"{symbol},day,{year}-01-01,{year + 1}-12-31,640,",
                    "r": "0.8205512681390605",
                } for year in range(start.year, today.year + 1))
                if not (discovery or discovery_fallback or history) or state["requests"] >= 4:
                    raise ValueError("Unapproved AKShare request or exhausted budget")
                state["requests"] += 1
                if history and params["_var"] == f"kline_day{today.year}":
                    params = {**params, "param": f"{symbol},day,,,60,"}
                with client.stream("GET", url, params=params) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            raise ValueError("Oversized price response")
                    received_at = datetime.now(SHANGHAI)
                    text = bytes(body).decode("utf-8")
                    prefix, separator, encoded = text.partition("=")
                    if not separator or prefix.strip() != params["_var"]:
                        raise ValueError("Unexpected price assignment")
                    encoded = encoded.lstrip()
                    payload, end = json.JSONDecoder(parse_constant=reject_constant).raw_decode(encoded)
                    if encoded[end:].strip() not in ("", ";") or not isinstance(payload, dict):
                        raise ValueError("Unexpected price expression")
                    if type(payload.get("code", 0)) is not int or payload.get("code", 0) != 0:
                        raise ValueError("Price source rejected request")
                    if history:
                        data = payload.get("data")
                        series = data.get(symbol) if isinstance(data, dict) else None
                        if not isinstance(series, dict) or not isinstance(series.get("day"), list):
                            raise ValueError("Missing unadjusted fund history")
                        quotes = series.get("qt")
                        quote = quotes.get(symbol) if isinstance(quotes, dict) else None
                        if not isinstance(quote, list) or len(quote) < 3 or quote[2] != symbol[2:]:
                            raise ValueError("Fund quote identity mismatch")
                        source_url = str(response.url)
                        for bar in series["day"]:
                            if not isinstance(bar, list) or len(bar) < 3:
                                continue
                            day, price = source_date(bar[0], today), number(bar[2])
                            if day is None or day < start or price is None or price <= 0:
                                continue
                            old = state["bars"].get(day)
                            if old and old[0] != price:
                                raise ValueError("Conflicting daily observations")
                            state["bars"][day] = (price, source_url, quote, received_at)
                    return httpx.Response(response.status_code, content=bytes(body), request=response.request)

            # AKShare's discovery and NAV APIs omit timeouts; bound transport inside the owned worker.
            requests.get = get

            def fetch(task):
                code, part = task["code"], task["part"]
                state = local.state = {"code": code, "part": part, "symbol": roster[code], "requests": 0,
                                       "bars": {}, "nav_rows": {}, "navs": {}, "share_events": set(),
                                       "uncertain_events": set()}
                try:
                    if part == "nav":
                        frame = ak.fund_etf_fund_info_em(
                            fund=code, start_date=start.strftime("%Y%m%d"), end_date=today.strftime("%Y%m%d"),
                        )
                        if (state["requests"] != state["pages"] + 1 or len(state["nav_rows"]) != state["total"]
                                or "净值日期" not in frame or "单位净值" not in frame):
                            raise ValueError("Incomplete published NAV history")
                        accepted = set()
                        for day_value, nav_value in frame[["净值日期", "单位净值"]].itertuples(index=False, name=None):
                            day = source_date(str(day_value), today)
                            value = number(nav_value)
                            if day is None or day < start or value is None or value <= 0:
                                continue
                            raw = state["navs"].get(day)
                            if raw is None or day in accepted or not math.isclose(value, raw[0], rel_tol=0, abs_tol=1e-10):
                                raise ValueError("AKShare unit NAV differs from published source")
                            accepted.add(day)
                        if not accepted or accepted != state["navs"].keys():
                            raise ValueError("No complete validated unit NAV observations")
                        day = max(accepted)
                        value, url, received_at = state["navs"][day]
                        return {"code": code, "part": part, "nav": {
                            "value": value, "date": day.isoformat(), "source_url": url,
                            "source_received_at": received_at.isoformat(),
                            "note": "AKShare / Eastmoney: published unit NAV; valuation may lag the price.",
                            "share_events": sorted(state["share_events"]),
                            "uncertain_events": sorted(state["uncertain_events"]),
                            "history_start": min(state["nav_rows"]).isoformat(),
                            "history_end": max(state["nav_rows"]).isoformat(),
                        }}
                    frame = ak.stock_zh_a_hist_tx(
                        symbol=roster[code], start_date=start.isoformat(), end_date=today.isoformat(),
                        adjust="", timeout=5,
                    )
                    if "date" not in frame or "close" not in frame:
                        raise ValueError("Missing price columns")
                    accepted = {}
                    for day_value, price_value in frame[["date", "close"]].itertuples(index=False, name=None):
                        day = source_date(str(day_value), today)
                        price = number(price_value)
                        if day is None or day < start or price is None or price <= 0:
                            continue
                        raw = local.state["bars"].get(day)
                        if raw is None or not math.isclose(price, raw[0], rel_tol=0, abs_tol=1e-10):
                            raise ValueError("AKShare observation differs from unadjusted source")
                        if day == today:
                            quote = raw[2]
                            confirmed = False
                            try:
                                stamp = datetime.strptime(quote[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
                                quoted_price = number(quote[3])
                                confirmed = (now.hour >= 15 and stamp.date() == today and stamp.hour >= 15
                                             and stamp <= raw[3] and quoted_price is not None
                                             and math.isclose(quoted_price, price, rel_tol=0, abs_tol=1e-8))
                            except (ValueError, TypeError, IndexError):
                                pass
                            if not confirmed:
                                continue
                        accepted[day] = raw
                    if not accepted:
                        raise ValueError("No completed unadjusted daily price")
                    day = max(accepted)
                    price, url, _, _ = accepted[day]
                    note = "AKShare / Tencent: unadjusted completed-session close."
                    if now.hour >= 15 and day != today and today.weekday() < 5:
                        note += " No confirmed close for today; holiday, suspension or publication delay possible."
                    elif (today - day).days > 3:
                        note += " Older dated observation; holiday, suspension or publication delay possible."
                    return {"code": code, "part": part, "close": {"value": price, "date": day.isoformat(), "source_url": url, "note": note}}
                except Exception as exc:
                    return {"code": code, "part": part, "error": type(exc).__name__}

            with ThreadPoolExecutor(max_workers=2) as pool:
                for future in as_completed([pool.submit(fetch, task) for task in tasks]):
                    print(json.dumps(future.result(), allow_nan=False), file=output, flush=True)


if __name__ == "__main__":
    main()
