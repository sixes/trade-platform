"""SDK-shaped quote fixtures; the real adapter runs against an in-memory context."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.market_data.longbridge import LongbridgeClient, LongbridgeError


def sdk_quote(symbol="SPY261023C104125.US", **overrides):
    fields = dict(
        symbol=symbol, underlying_symbol="SPY.US", expiry_date=date(2026, 10, 23),
        strike_price=Decimal("104.125"), last_done=Decimal("3.25"),
        implied_volatility=Decimal("25"), open_interest=300, volume=12,
        trade_status="TradeStatus.Normal", contract_multiplier=Decimal("100"),
    )
    fields.update(overrides)
    # Deliberately no normalized option_side: this is the provider adapter's job.
    return SimpleNamespace(**fields)


def quote_client(quotes, batch_size=200):
    client = LongbridgeClient(Settings({"longbridge": {
        "requests_per_second": 1000, "burst": 1000,
        "option_quote_batch_size": batch_size, "option_contracts_per_minute": 500,
    }}))
    by_symbol = {q.symbol: q for q in quotes}
    requests = []

    def option_quote(symbols):
        requests.append(list(symbols))
        return [by_symbol[s] for s in symbols]

    # Never initialize a real QuoteContext or load SDK credentials.
    client._ctx = SimpleNamespace(option_quote=option_quote, requests=requests)
    return client


def test_option_quotes_decode_sides_and_exact_strikes_without_changing_batching():
    quotes = [
        sdk_quote("SPY261023C104125.US"),
        sdk_quote("SPY261023P00104125.US"),
        sdk_quote("PCAR261023C00104126.US", underlying_symbol="PCAR.US", strike_price=Decimal("104.126")),
    ]
    original = deepcopy([vars(q) for q in quotes])
    client = quote_client(quotes, batch_size=2)
    progress = []
    rows = client.option_quotes([q.symbol for q in quotes], progress=lambda n, total: progress.append((n, total)))

    assert [r.option_side for r in rows] == ["call", "put", "call"]
    assert [r.strike for r in rows] == [104.125, 104.125, 104.126]
    assert [r.symbol for r in rows] == [q.symbol for q in quotes]
    assert [r.underlying for r in rows] == ["SPY.US", "SPY.US", "PCAR.US"]
    assert all(r.expiry == date(2026, 10, 23) and r.iv == 0.25 for r in rows)
    assert all(r.last == 3.25 and r.contract_multiplier == 100 for r in rows)
    assert all(r.open_interest == 300 and r.volume == 12 for r in rows)
    assert progress == [(1, 2), (2, 2)]
    assert client._ctx.requests == [[q.symbol for q in quotes[:2]], [quotes[2].symbol]]
    assert client.quota.total_calls == 2 and client.option_budget.used() == 3
    assert [vars(q) for q in quotes] == original


@pytest.mark.parametrize("symbol", [
    "SPY.US", "SPY261023X104125.US", "SPY261023104125.US",
    "junkSPY261023C104125.US", "SPY261023C104125.US.extra",
    "SPY261023C104125.US\n", "SPY261023c104125.US", "SPY261023C104125",
    "SPY261023C000104125.US", "SPY 261023C104125.US",
    "SPY261332C104125.US", "SPY260230P104125.US", "", None,
])
def test_option_quotes_reject_unrecognized_or_invalid_symbols(symbol):
    client = quote_client([sdk_quote(symbol)])
    with pytest.raises(LongbridgeError, match="(unrecognized option symbol|invalid option identity)"):
        client.option_quotes([symbol])


@pytest.mark.parametrize("overrides", [
    {"strike_price": Decimal("104.126")},
    {"strike_price": Decimal("104.125000000000001")},
    {"strike_price": Decimal("NaN")},
    {"strike_price": Decimal("Infinity")},
    {"strike_price": None},
    {"expiry_date": date(2026, 10, 24)},
])
def test_option_quotes_reject_contradictory_identity_before_float_rounding(overrides):
    quote = sdk_quote(**overrides)
    client = quote_client([quote])
    with pytest.raises(LongbridgeError, match="(option quote identity mismatch|invalid option identity)"):
        client.option_quotes([quote.symbol])
