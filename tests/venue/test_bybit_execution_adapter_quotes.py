"""Quote-first entry placement through the execution adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from liquidity_migration.account.account_contracts import (
    InstrumentRules,
    MarketInputRef,
    OrderCommand,
)
from liquidity_migration.marketdata.bybit_errors import BybitRequestRejected
from liquidity_migration.venue.bybit_execution_adapter import BybitDemoExecutionAdapter
from liquidity_migration.venue.entry_quote_manager import EntryQuoteConfig, EntryQuoteManager


@dataclass
class FakeClock:
    now_ns: int = 1_000_000_000_000

    def wall_time_ns(self) -> int:
        return self.now_ns

    def monotonic_ns(self) -> int:
        return self.now_ns


@dataclass
class FakeState:
    orders: dict[str, Any] = field(default_factory=dict)
    working_order_ids: set[str] = field(default_factory=set)


class FakeKernel:
    def __init__(self) -> None:
        self.state = FakeState()

    def _state_ref(self) -> FakeState:
        return self.state


class FakeClient:
    realm = "demo"
    demo = True
    testnet = False

    def __init__(self, *, reject_limit_create: bool = False) -> None:
        self.reject_limit_create = reject_limit_create
        self.placed: list[dict[str, Any]] = []

    def place_order(self, **params: Any) -> dict[str, Any]:
        if self.reject_limit_create and params.get("orderType") == "Limit":
            raise BybitRequestRejected("stop loss attach refused for this order shape")
        self.placed.append(dict(params))
        return {"orderId": f"venue-{len(self.placed)}", "_response_time_ms": 1_700_000_000_000}

    def set_leverage(self, **params: Any) -> dict[str, Any]:
        return {}


def entry_command(signed_qty: float = 100.0) -> OrderCommand:
    return OrderCommand(
        command_id="cmd-quote-1",
        batch_id="batch-1",
        symbol="LAUSDT",
        side="Buy" if signed_qty > 0 else "Sell",
        qty=abs(signed_qty),
        signed_qty=signed_qty,
        reduce_only=False,
        reference_price=0.0378,
        target_signed_qty=signed_qty,
        chunk_index=0,
        chunk_count=1,
        leverage=2.0,
        created_ts_ns=1_000_000_000_000,
        entry_stop_price=0.030 if signed_qty > 0 else 0.045,
        entry_stop_fraction=0.2,
        entry_stop_source="declared_stop",
        entry_stop_trigger_by="MarkPrice",
    )


def exit_command() -> OrderCommand:
    return OrderCommand(
        command_id="cmd-exit-1",
        batch_id="batch-1",
        symbol="LAUSDT",
        side="Sell",
        qty=100.0,
        signed_qty=-100.0,
        reduce_only=True,
        reference_price=0.0378,
        target_signed_qty=0.0,
        chunk_index=0,
        chunk_count=1,
    )


def market_input(bid: float | None = 0.0376, ask: float | None = 0.0380) -> MarketInputRef:
    return MarketInputRef(
        input_key="cap-1",
        symbol="LAUSDT",
        exchange_ts_ns=1_000_000_000_000,
        local_receive_ts_ns=1_000_000_000_000,
        reference_price=0.0378,
        bid_price=bid,
        ask_price=ask,
    )


def build_adapter(
    client: FakeClient,
    *,
    with_quotes: bool = True,
    verifier_calls: list[dict[str, Any]] | None = None,
) -> tuple[BybitDemoExecutionAdapter, EntryQuoteManager | None]:
    def verifier(**kwargs: Any) -> str:
        if verifier_calls is not None:
            verifier_calls.append(kwargs)
        return "armed"

    quotes = None
    if with_quotes:
        quotes = EntryQuoteManager(
            client,
            config=EntryQuoteConfig(window_seconds=120.0),
            instrument_rules={
                "LAUSDT": InstrumentRules(
                    symbol="LAUSDT",
                    qty_step=1.0,
                    min_qty=1.0,
                    min_notional=5.0,
                    tick_size=0.0001,
                )
            },
            kernel=FakeKernel(),
            clock=FakeClock(),
            entry_stop_verifier=verifier,
        )
    adapter = BybitDemoExecutionAdapter(
        client,
        clock=FakeClock(),
        entry_stop_verifier=verifier,
        entry_quotes=quotes,
    )
    return adapter, quotes


def test_entry_rests_at_the_touch_with_the_stop_attached() -> None:
    client = FakeClient()
    verifier_calls: list[dict[str, Any]] = []
    adapter, quotes = build_adapter(client, verifier_calls=verifier_calls)
    observations = list(adapter.submit_prepared(entry_command(), market_input()))

    assert len(client.placed) == 1
    params = client.placed[0]
    assert params["orderType"] == "Limit"
    assert params["price"] == "0.0376"
    assert params["timeInForce"] == "GTC"
    assert params["stopLoss"] == "0.03"
    assert params["reduceOnly"] is False

    ack = observations[0]
    assert ack.accepted is True
    assert ack.metadata["execution_style"] == "resting_quote"
    assert ack.metadata["entry_quote_price"] == "0.0376"
    assert ack.metadata["entry_attached_stop_verification"] == "deferred_resting_quote"
    # Verification happens at fill through the manager, not at create.
    assert verifier_calls == []
    assert quotes is not None and quotes.symbol_has_active_quote("LAUSDT")


def test_short_entry_rests_at_the_ask() -> None:
    client = FakeClient()
    adapter, _ = build_adapter(client)
    list(adapter.submit_prepared(entry_command(signed_qty=-100.0), market_input()))
    assert client.placed[0]["price"] == "0.038"
    assert client.placed[0]["side"] == "Sell"


def test_limit_reject_falls_back_to_the_market_order() -> None:
    client = FakeClient(reject_limit_create=True)
    verifier_calls: list[dict[str, Any]] = []
    adapter, quotes = build_adapter(client, verifier_calls=verifier_calls)
    observations = list(adapter.submit_prepared(entry_command(), market_input()))

    assert len(client.placed) == 1
    assert client.placed[0]["orderType"] == "Market"
    assert "price" not in client.placed[0]
    ack = observations[0]
    assert ack.accepted is True
    assert ack.metadata["execution_style"] == "market_after_quote_reject"
    # The market path keeps its create-boundary verification.
    assert len(verifier_calls) == 1
    assert quotes is not None and not quotes.symbol_has_active_quote("LAUSDT")


def test_tight_spread_goes_straight_to_market() -> None:
    client = FakeClient()
    adapter, _ = build_adapter(client)
    list(adapter.submit_prepared(entry_command(), market_input(bid=0.0376, ask=0.0377)))
    assert client.placed[0]["orderType"] == "Market"


def test_reduce_only_never_quotes() -> None:
    client = FakeClient()
    adapter, _ = build_adapter(client)
    list(adapter.submit_prepared(exit_command(), market_input()))
    assert client.placed[0]["orderType"] == "Market"
    assert client.placed[0]["reduceOnly"] is True


def test_without_a_manager_behavior_is_unchanged() -> None:
    client = FakeClient()
    verifier_calls: list[dict[str, Any]] = []
    adapter, _ = build_adapter(client, with_quotes=False, verifier_calls=verifier_calls)
    observations = list(adapter.submit_prepared(entry_command(), market_input()))
    assert client.placed[0]["orderType"] == "Market"
    ack = observations[0]
    assert ack.metadata["execution_style"] == "market"
    assert len(verifier_calls) == 1
