from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
FUNDS = (
    ("513100", "SH", "Nasdaq-100", "国泰纳斯达克100ETF", "ETF"),
    ("513110", "SH", "Nasdaq-100", "华泰柏瑞纳斯达克100ETF", "ETF"),
    ("513300", "SH", "Nasdaq-100", "华夏纳斯达克100ETF", "ETF"),
    ("513390", "SH", "Nasdaq-100", "博时纳斯达克100ETF", "ETF"),
    ("513870", "SH", "Nasdaq-100", "富国纳斯达克100ETF", "ETF"),
    ("159501", "SZ", "Nasdaq-100", "嘉实纳斯达克100ETF", "ETF"),
    ("159513", "SZ", "Nasdaq-100", "大成纳斯达克100ETF", "ETF"),
    ("159632", "SZ", "Nasdaq-100", "华安纳斯达克100ETF", "ETF"),
    ("159659", "SZ", "Nasdaq-100", "招商纳斯达克100ETF", "ETF"),
    ("159660", "SZ", "Nasdaq-100", "汇添富纳斯达克100ETF", "ETF"),
    ("159696", "SZ", "Nasdaq-100", "易方达纳斯达克100ETF", "ETF"),
    ("159941", "SZ", "Nasdaq-100", "广发纳斯达克100ETF", "ETF"),
    ("161130", "SZ", "Nasdaq-100", "易方达纳斯达克100ETF联接（LOF）人民币A", "ETF feeder · LOF · RMB A"),
    ("513500", "SH", "S&P 500", "博时标普500ETF", "ETF"),
    ("513650", "SH", "S&P 500", "南方标普500ETF", "ETF"),
    ("159612", "SZ", "S&P 500", "国泰标普500ETF", "ETF"),
    ("159655", "SZ", "S&P 500", "华夏标普500ETF", "ETF"),
    ("161125", "SZ", "S&P 500", "易方达标普500指数（LOF）人民币A", "LOF · RMB A"),
)
PROFILE_URL = "https://fundf10.eastmoney.com/jbgk_{code}.html"
NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
DAILY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
FEEDER_DISCLOSURE_URL = "https://static.cninfo.com.cn/finalpage/2026-08-11/1225467509.PDF"
PARTS = ("close", "nav", "profile")
TTL = {"close": 600, "nav": 600, "profile": 86400, "iopv": 600}
RETRY_SECONDS = 60
REFRESH_DEADLINE = 60
RESPONSE_LIMIT = 2_000_000


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def source_date(value, today):
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed <= today else None
    except (ValueError, TypeError):
        return None


def reject_constant(_value):
    raise ValueError("Non-finite source data")


class ProfileTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = []
        self.cell = None
        self.fields = {}
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
        elif tag == "tr":
            self.handle_endtag("tr")
        elif tag in ("td", "th"):
            # The public profile omits optional closing cell tags.
            self.handle_endtag("td")
            self.cell = []

    def handle_data(self, text):
        if self.cell is not None and not self.ignored:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)
        elif tag in ("td", "th") and self.cell is not None:
            self.cells.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr":
            self.handle_endtag("td")
            for index in range(0, len(self.cells) - 1, 2):
                self.fields[self.cells[index]] = self.cells[index + 1]
            self.cells = []


class ChinaEtfService:
    def __init__(self):
        self._cache = {}
        self._attempts = {}
        self._errors = {}
        self._task = None
        self._iopv_cache: tuple = (0.0, {})  # (monotonic_time, {code: {iopv, prev_close, price, ...}})

    async def close(self):
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def snapshot(self):
        now = time.monotonic()
        if self._task is None or self._task.done():
            due = []
            for fund in FUNDS:
                for part in PARTS:
                    key = fund[0], part
                    cached = self._cache.get(key)
                    if (now - self._attempts.get(key, -math.inf) >= RETRY_SECONDS
                            and (cached is None or now - cached[0] >= TTL[part])):
                        due.append((fund, part))
            iopv_due = now - self._iopv_cache[0] >= TTL["iopv"]
            if due or iopv_due:
                self._task = asyncio.create_task(self._refresh(due, iopv_due))
        refreshing = self._task is not None and not self._task.done()
        return {
            "items": self._rows(),
            "refreshing": refreshing,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "refresh_seconds": 3 if refreshing else 600,
        }

    async def _get(self, client, url, params=None):
        async with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > RESPONSE_LIMIT:
                    raise ValueError("Oversized public response")
            return bytes(body).decode("utf-8")

    async def _refresh(self, due, iopv_due=False):
        market_tasks = [(fund[0], part) for fund, part in due if part in ("close", "nav")]
        concurrency = 2 if market_tasks else 4
        semaphore = asyncio.Semaphore(concurrency)
        now = time.monotonic()
        for fund, part in due:
            self._attempts[fund[0], part] = now
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), trust_env=False,
                                         follow_redirects=False, limits=httpx.Limits(max_connections=concurrency),
                                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"}) as client:
                async def fetch(fund, part):
                    key = fund[0], part
                    try:
                        async with semaphore:
                            value = await getattr(self, "_" + part)(client, fund)
                        value["retrieved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        self._cache[key] = time.monotonic(), value
                        self._errors.pop(key, None)
                    except asyncio.CancelledError:
                        self._errors[key] = "Source refresh timed out; retained observations may be stale."
                        raise
                    except Exception as exc:
                        self._errors[key] = "Source unavailable or its data could not be validated."
                        log.warning("China fund %s %s unavailable (%s)", fund[0], part, type(exc).__name__)
                work = [fetch(fund, part) for fund, part in due if part == "profile"]
                if market_tasks:
                    work.append(self._refresh_market_data(market_tasks))
                if iopv_due:
                    work.append(self._refresh_iopv())
                await asyncio.wait_for(asyncio.gather(*work), REFRESH_DEADLINE)
        except asyncio.TimeoutError:
            log.warning("China fund refresh reached its deadline")

    async def _refresh_market_data(self, tasks):
        pending = set(tasks)
        as_of = datetime.now(SHANGHAI)
        with tempfile.TemporaryDirectory(prefix="trade-plat-fund-data-") as home:
            process = None
            try:
                launch = asyncio.create_task(asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-B", str(Path(__file__).with_name("china_etf_akshare.py")),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=8192, cwd=home,
                    env={"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": home,
                         "LANG": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1"},
                ))
                try:
                    process = await asyncio.shield(launch)
                except asyncio.CancelledError:
                    process = await launch
                    raise
                async def collect():
                    batch = [{"code": code, "part": part} for code, part in sorted(pending)]
                    process.stdin.write((json.dumps({"tasks": batch, "as_of": as_of.isoformat()}) + "\n").encode())
                    await process.stdin.drain()
                    process.stdin.close()
                    async for line in process.stdout:
                        result = json.loads(line, parse_constant=reject_constant)
                        if not isinstance(result, dict):
                            raise ValueError("Invalid worker record")
                        code, part = result.get("code"), result.get("part")
                        if not isinstance(code, str) or not isinstance(part, str) or (code, part) not in pending:
                            raise ValueError("Unexpected worker result")
                        key = code, part
                        try:
                            if set(result) == {"code", "part", "error"} and isinstance(result["error"], str):
                                raise ValueError(result["error"])
                            value = result.get(part)
                            if set(result) != {"code", "part", part} or not isinstance(value, dict):
                                raise ValueError("Invalid worker payload")
                            day, price = source_date(value.get("date"), as_of.date()), number(value.get("value"))
                            if (day is None or day < as_of.date() - timedelta(days=45)
                                    or price is None or price <= 0 or value["date"] != day.isoformat()
                                    or not isinstance(value.get("source_url"), str)
                                    or not isinstance(value.get("note"), str)):
                                raise ValueError("Invalid worker observation")
                            url = httpx.URL(value["source_url"])
                            if url.scheme != "https" or url.userinfo or url.fragment or url.port not in (None, 443):
                                raise ValueError("Invalid worker source URL")
                            if part == "nav":
                                first = source_date(value.get("history_start"), as_of.date())
                                last = source_date(value.get("history_end"), as_of.date())
                                if (first is None or last is None or not first <= day <= last
                                        or first < as_of.date() - timedelta(days=45)
                                        or value["history_start"] != first.isoformat() or value["history_end"] != last.isoformat()):
                                    raise ValueError("Invalid NAV history coverage")
                                for field in ("share_events", "uncertain_events"):
                                    events = value.get(field)
                                    if (not isinstance(events, list) or len(events) > 46
                                            or any(not isinstance(event, str) or source_date(event, last) is None
                                                   or event < first.isoformat() for event in events)
                                            or len(set(events)) != len(events)):
                                        raise ValueError("Invalid NAV action metadata")
                                received = datetime.fromisoformat(value["source_received_at"])
                                if received.utcoffset() != timedelta(hours=8) or received.date() < day:
                                    raise ValueError("Invalid NAV receipt time")
                                params = dict(url.params)
                                if (str(url.copy_with(query=None)) != NAV_URL
                                        or len(url.params.multi_items()) != len(params)
                                        or params != {"fundCode": code, "pageIndex": params.get("pageIndex"),
                                                      "pageSize": "20", "startDate": (as_of.date() - timedelta(days=45)).isoformat(),
                                                      "endDate": as_of.date().isoformat(), "_": params.get("_")}
                                        or params.get("pageIndex") not in ("1", "2", "3")
                                        or not re.fullmatch(r"\d{1,16}", params.get("_", ""))):
                                    raise ValueError("NAV request identity mismatch")
                            elif str(url.copy_with(query=None)) != DAILY_URL:
                                raise ValueError("Invalid close source URL")
                            previous = self._cache.get(key)
                            if previous and value["date"] < previous[1]["date"]:
                                self._errors[key] = f"AKShare returned an older {part}; retaining the newer cached observation."
                            else:
                                value["retrieved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                                self._cache[key] = time.monotonic(), value
                                self._errors.pop(key, None)
                        except (ValueError, TypeError, KeyError) as exc:
                            self._errors[key] = f"AKShare {part} source unavailable or its data could not be validated."
                            log.warning("China fund %s %s unavailable (%s)", code, part, type(exc).__name__)
                        pending.remove(key)
                    await process.wait()
                    if process.returncode:
                        log.warning("China fund market-data worker exited unsuccessfully")
                await asyncio.wait_for(collect(), max(1, REFRESH_DEADLINE - 3))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("China fund market-data batch unavailable (%s)", type(exc).__name__)
            finally:
                if process is not None and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 1)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                for code, part in pending:
                    self._errors[code, part] = f"AKShare {part} refresh did not complete; retained observations may be stale."

    async def _profile(self, client, fund):
        code, _, benchmark, _, kind = fund
        url = PROFILE_URL.format(code=code)
        parser = ProfileTable()
        parser.feed(await self._get(client, url))
        fields = parser.fields
        identity = fields.get("基金代码", "")
        name = fields.get("基金全称", "")
        tracked = re.sub(r"\s+", "", fields.get("跟踪标的", "") + fields.get("业绩比较基准", ""))
        tokens = ("纳斯达克100", "Nasdaq-100", "NASDAQ-100") if benchmark == "Nasdaq-100" else ("标准普尔500", "标普500", "S&P500")
        if not re.search(r"(?<!\d)" + code + r"(?!\d)", identity) or not name or not any(t.lower() in tracked.lower() for t in tokens):
            raise ValueError("Fund profile identity or benchmark mismatch")
        def fee(label):
            text = fields.get(label, "")
            match = re.match(r"(\d+(?:\.\d+)?)\s*%", text) if "每年" in text else None
            value = number(match[1]) if match else None
            return value if value is not None and 0 <= value <= 100 else None
        asset_text = fields.get("净资产规模", "")
        amount = re.match(r"([\d,.]+)\s*(万亿元|亿元|万元|元)", asset_text)
        dated = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", asset_text)
        asset_date = source_date(f"{dated[1]}-{int(dated[2]):02}-{int(dated[3]):02}", datetime.now(SHANGHAI).date()) if dated else None
        aum = number(amount[1]) if amount else None
        if aum is not None:
            aum *= {"万亿元": 1e12, "亿元": 1e8, "万元": 1e4, "元": 1}[amount[2]]
        if asset_date is None or aum is None or not math.isfinite(aum) or aum < 0:
            aum, asset_date = None, None
        management, custody = fee("管理费率"), fee("托管费率")
        notes = []
        if management is None or custody is None:
            notes.append("An annual fee component is unavailable.")
        if aum is None:
            notes.append("Dated net assets are unavailable.")
        return {"name": name, "management_fee_pct": management, "custody_fee_pct": custody,
                "aum_cny": aum, "aum_date": asset_date.isoformat() if asset_date else None,
                "aum_scope": "Source-reported net assets; share-class scope unspecified" if "LOF" in kind else "Fund net assets",
                "source_url": url, "note": " ".join(notes)}

    async def _refresh_iopv(self):
        try:
            import akshare as ak
            df = await asyncio.get_event_loop().run_in_executor(None, ak.fund_etf_spot_em)
            codes = {f[0] for f in FUNDS}
            result = {}
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                if code not in codes:
                    continue
                iopv = number(row.get("IOPV实时估值"))
                prev_close = number(row.get("昨收"))
                price = number(row.get("最新价"))
                result[code] = {
                    "iopv": iopv,
                    "prev_close": prev_close,
                    "price": price,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            self._iopv_cache = (time.monotonic(), result)
        except Exception as exc:
            log.warning("IOPV refresh unavailable: %s", exc)
            self._iopv_cache = (time.monotonic(), self._iopv_cache[1])

    def _iopv(self, code):
        _, data = self._iopv_cache
        return data.get(code, {})

    def _part(self, code, part):
        cached = self._cache.get((code, part))
        value = dict(cached[1]) if cached else {}
        value.setdefault("source_url", PROFILE_URL.format(code=code) if part == "profile" else f"{NAV_URL}?fundCode={code}" if part == "nav" else DAILY_URL)
        value.setdefault("retrieved_at", None)
        value.setdefault("note", "")
        if part == "profile":
            for field in ("management_fee_pct", "custody_fee_pct", "aum_cny", "aum_date"):
                value.setdefault(field, None)
            value.setdefault("aum_scope", "")
            if code == "161130":
                value["fee_note"] = "Both fee bases exclude target-ETF fair value (floor zero); disclosure dated 2026-08-11."
                value["fee_source_url"] = FEEDER_DISCLOSURE_URL
        else:
            value.setdefault("value", None)
            value.setdefault("date", None)
        error = self._errors.get((code, part))
        value["status"] = "unavailable" if not cached else "stale" if error or time.monotonic() - cached[0] >= TTL[part] else "ok"
        if error:
            value["note"] = " ".join(filter(None, (value["note"], error)))
        return value

    def _rows(self):
        rows = []
        for code, exchange, benchmark, fallback_name, kind in sorted(FUNDS, key=lambda f: (f[2], f[0])):
            close, nav, profile = (self._part(code, part) for part in PARTS)
            iopv_data = self._iopv(code)
            iopv_value = iopv_data.get("iopv")
            prev_close = iopv_data.get("prev_close")
            premium = None
            premium_note = "Reference only; price and US-asset valuation are not simultaneous."
            if close["value"] is not None and nav["value"] is not None:
                result = (close["value"] / nav["value"] - 1) * 100
                if math.isfinite(result):
                    premium = result
                if close["date"] != nav["date"]:
                    premium_note = f"Dates differ: close {close['date']} / NAV {nav['date']}. Reference only, not live fair value."
            else:
                premium_note = "Unavailable until both a completed close and unit NAV are available."
            close_iopv = None
            if close["value"] is not None and iopv_value is not None and iopv_value > 0:
                ratio = close["value"] / iopv_value
                if math.isfinite(ratio):
                    close_iopv = ratio
            warnings = [f"{part.upper() if part == 'nav' else part.capitalize()}: {value['status']}"
                        for part, value in (("close", close), ("nav", nav), ("profile", profile)) if value["status"] != "ok"]
            rows.append({"code": code, "exchange": exchange, "index": benchmark, "kind": kind,
                         "name": profile.get("name", fallback_name), "fund_url": PROFILE_URL.format(code=code),
                         "close": close, "nav": nav, "profile": profile, "premium_pct": premium,
                         "premium_note": premium_note, "warnings": warnings,
                         "iopv": iopv_value, "prev_close": prev_close,
                         "close_iopv": close_iopv,
                         "iopv_retrieved_at": iopv_data.get("retrieved_at")})
        return rows
