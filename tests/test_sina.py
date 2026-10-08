from datetime import datetime, timezone

import pytest

from backend.market_data.sina import SinaClient, parse_daily, parse_hq, parse_jsonp, parse_minute_bars, session_start, beijing, eastern

HQ_TEXT = (
    'var hq_str_hf_XAU="4278.67,4287.280,4278.67,4279.02,4303.09,4273.66,16:00:00,4287.28,4290.90,0,0,0,2026-09-24,伦敦金（现货黄金）";\n'
    'var hq_str_hf_OIL="99.030,,99.020,99.030,99.120,97.090,16:00:30,98.120,98.060,0,1,2,2026-09-24,布伦特原油,41192";\n'
    'var hq_str_hf_HG="677.431,,677.050,677.200,679.700,674.000,16:00:36,675.350,678.050,0,3,7,2026-09-24,美铜,0";\n'
)


def test_parse_hq_fields_and_timezone():
    quotes = parse_hq(HQ_TEXT)
    gold = quotes["XAU"]
    assert gold.name.startswith("伦敦金") and gold.last == 4278.67 and gold.bid == 4278.67 and gold.ask == 4279.02
    assert gold.high == 4303.09 and gold.low == 4273.66 and gold.open == 4290.9 and gold.prev_close == 4287.28
    assert gold.as_of == "2026-09-24T04:00-04:00"  # 16:00 Beijing == 04:00 EDT
    oil = quotes["OIL"]
    assert oil.last == 99.03 and oil.prev_close == 98.12
    copper = quotes["HG"].scaled(0.01)
    assert copper.last == pytest.approx(6.77431) and copper.prev_close == pytest.approx(6.7535)


def test_parse_jsonp_minute_and_daily():
    minute = parse_jsonp('/*<script>location.href=\'//sina.com\';</script>*/\nvar _x=([{"d":"2026-09-24 16:00:00","o":"1","h":"1","l":"1","c":"4280.26","v":"0","p":"0"},{"d":"2026-09-24 15:55:00","o":"1","h":"1","l":"1","c":"4281.00","v":"0","p":"0"}]);')
    assert parse_minute_bars(minute) == [("2026-09-24T03:55-04:00", 4281.0), ("2026-09-24T04:00-04:00", 4280.26)]
    daily = parse_jsonp('var _x=([{"date":"2026-09-23","close":"678.350"},{"date":"2026-09-24","close":"677.431"}]);')
    assert parse_daily(daily, 0.01) == [("2026-09-23", pytest.approx(6.7835)), ("2026-09-24", pytest.approx(6.77431))]
    with pytest.raises(ValueError, match="Service not found"):
        parse_jsonp('var _x=({"__ERROR":3,"__ERRORMSG":"Service not found"});')


def test_session_start_rolls_at_0600_beijing():
    # 04:10 EDT == 16:10 Beijing -> session started 06:00 Beijing the same Beijing day == 18:00 EDT the day before.
    now = datetime(2026, 9, 24, 4, 10, tzinfo=eastern())
    assert session_start(now).isoformat(timespec="minutes") == "2026-09-23T18:00-04:00"
    # 15:00 EDT == 03:00 Beijing next day -> still the session that started 06:00 Beijing on 09-24 (18:00 EDT 09-23).
    later = datetime(2026, 9, 24, 15, 0, tzinfo=eastern())
    assert session_start(later).isoformat(timespec="minutes") == "2026-09-23T18:00-04:00"
    # 19:00 EDT == 07:00 Beijing next day -> a new session began at 06:00 Beijing == 18:00 EDT today.
    evening = datetime(2026, 9, 24, 19, 0, tzinfo=eastern())
    assert session_start(evening).isoformat(timespec="minutes") == "2026-09-24T18:00-04:00"


def test_quotes_are_batched_and_cached(monkeypatch):
    client = SinaClient(quote_ttl=60)
    urls = []

    def fake_get(url):
        urls.append(url)
        wanted = url.split("list=")[1].split(",")
        return "".join(line + "\n" for line in HQ_TEXT.splitlines() if any(f"hq_str_{code}=" in line for code in wanted))

    monkeypatch.setattr(client, "_get", fake_get)
    quotes = client.quotes(["XAU", "OIL"])
    assert set(quotes) == {"XAU", "OIL"} and len(urls) == 1 and "hf_XAU,hf_OIL" in urls[0]
    client.quotes(["XAU"])  # within TTL: no request
    assert len(urls) == 1
    client.quotes(["HG", "XAU"])  # only the missing symbol is requested
    assert len(urls) == 2 and urls[1].endswith("list=hf_HG")
