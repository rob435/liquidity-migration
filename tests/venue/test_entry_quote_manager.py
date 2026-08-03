"""Resting entry quotes: placement gates, lifecycle, adoption, health probe."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from liquidity_migration.account.account_contracts import InstrumentRules, OrderState
from liquidity_migration.marketdata.bybit_errors import BybitRequestRejected
from liquidity_migration.venue.entry_quote_manager import (
    EntryQuoteConfig,
    EntryQuoteManager,
    snap_price_text,
)


@dataclass
class FakeClock:
    now_ns: int = 1_000_000_000_000

    def wall_time_ns(self) -> int:
        return self.now_ns

    def monotonic_ns(self) -> int:
        return self.now_ns

    def tick(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1e9)


@dataclass
class FakeState:
    orders: dict[str, OrderState] = field(default_factory=dict)
    working_order_ids: set[str] = field(default_factory=set)


class FakeKernel:
    def __init__(self) -> None:
        self.state = FakeState()

    def _state_ref(self) -> FakeState:
        return self.state


class FakeClient:
    def __init__(self) -> None:
        self.tickers: dict[str, tuple[float, float]] = {}
        self.open_orders: list[dict[str, Any]] = []
        self.amends: list[dict[str, str]] = []
        self.cancels: list[str] = []
        self.amend_error: Exception | None = None

    def get_tickers(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol and symbol in self.tickers:
            bid, ask = self.tickers[symbol]
            return [{"symbol": symbol, "bid1Price": str(bid), "ask1Price": str(ask)}]
        return []

    def get_open_orders(self, **params: Any) -> list[dict[str, Any]]:
        link = params.get("order_link_id")
        return [row for row in self.open_orders if not link or row.get("orderLinkId") == link]

    def amend_order(self, *, symbol: str, order_link_id: str, price: str) -> dict[str, Any]:
        if self.amend_error is not None:
            raise self.amend_error
        self.amends.append({"symbol": symbol, "orderLinkId": order_link_id, "price": price})
        return {}

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, Any]:
        self.cancels.append(order_link_id)
        return {}


def rules_for(symbol: str, tick: float) -> dict[str, InstrumentRules]:
    return {
        symbol: InstrumentRules(
            symbol=symbol,
            qty_step=1.0,
            min_qty=1.0,
            min_notional=5.0,
            tick_size=tick,
        )
    }


def working_entry(
    command_id: str = "cmd-1",
    symbol: str = "LAUSDT",
    signed_qty: float = 100.0,
    status: str = "acknowledged",
    filled: float = 0.0,
) -> OrderState:
    return OrderState(
        command_id=command_id,
        batch_id="batch-1",
        symbol=symbol,
        signed_qty=signed_qty,
        reduce_only=False,
        entry_stop_price=0.03,
        status=status,
        filled_signed_qty=filled,
    )


def build_manager(
    *,
    client: FakeClient | None = None,
    kernel: FakeKernel | None = None,
    clock: FakeClock | None = None,
    verifier: Any = None,
    tick: float = 0.0001,
    window: float = 120.0,
) -> tuple[EntryQuoteManager, FakeClient, FakeKernel, FakeClock]:
    client = client or FakeClient()
    kernel = kernel or FakeKernel()
    clock = clock or FakeClock()
    manager = EntryQuoteManager(
        client,
        config=EntryQuoteConfig(window_seconds=window),
        instrument_rules=rules_for("LAUSDT", tick),
        kernel=kernel,
        clock=clock,
        entry_stop_verifier=verifier,
    )
    return manager, client, kernel, clock


def test_snap_price_text_stays_inside_the_book() -> None:
    assert snap_price_text(0.037691, 0.0001, round_up=False) == "0.0376"
    assert snap_price_text(0.037601, 0.0001, round_up=True) == "0.0377"


def test_plan_quotes_the_near_touch() -> None:
    manager, _, _, _ = build_manager()
    assert manager.plan_entry_quote(symbol="LAUSDT", is_buy=True, bid=0.0376, ask=0.0380) == "0.0376"
    assert manager.plan_entry_quote(symbol="LAUSDT", is_buy=False, bid=0.0376, ask=0.0380) == "0.038"


def test_plan_refuses_tight_spread_missing_rules_and_bad_touch() -> None:
    manager, client, _, _ = build_manager()
    # One tick of spread: crossing is already near the maker floor.
    assert manager.plan_entry_quote(symbol="LAUSDT", is_buy=True, bid=0.0376, ask=0.0377) is None
    # Unknown instrument grid.
    assert manager.plan_entry_quote(symbol="OTHERUSDT", is_buy=True, bid=1.0, ask=2.0) is None
    # Crossed/absent book falls back to the ticker read, which is empty here.
    assert manager.plan_entry_quote(symbol="LAUSDT", is_buy=True, bid=None, ask=None) is None
    client.tickers["LAUSDT"] = (0.0376, 0.0380)
    assert manager.plan_entry_quote(symbol="LAUSDT", is_buy=True, bid=None, ask=None) == "0.0376"


def test_plan_refuses_when_disabled() -> None:
    manager, _, _, _ = build_manager(window=0.0)
    assert not manager.config.enabled
    assert manager.plan_entry_quote(symbol="LAUSDT", is_buy=True, bid=0.0376, ask=0.0380) is None


def test_fill_runs_deferred_stop_verification_once() -> None:
    calls: list[dict[str, Any]] = []

    def verifier(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "armed"

    manager, _, kernel, _ = build_manager(verifier=verifier)
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)

    order.status = "filled"
    order.filled_signed_qty = order.signed_qty
    kernel.state.working_order_ids.discard(order.command_id)
    manager.advance()
    manager.advance()
    assert len(calls) == 1
    assert calls[0]["expected_stop_price"] == pytest.approx(0.03)
    assert calls[0]["command_id"] == order.command_id
    assert not manager.symbol_has_active_quote("LAUSDT")


def test_reprice_chases_only_toward_the_market() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)

    # Touch moved away (down): the order is alone at the front; no amend.
    client.tickers["LAUSDT"] = (0.0374, 0.0380)
    clock.tick(16.0)
    manager.advance()
    assert client.amends == []

    # Touch moved up: chase it.
    client.tickers["LAUSDT"] = (0.0378, 0.0384)
    clock.tick(16.0)
    manager.advance()
    assert client.amends == [{"symbol": "LAUSDT", "orderLinkId": "cmd-1", "price": "0.0378"}]

    # Inside the reprice interval nothing happens even if the touch moves.
    client.tickers["LAUSDT"] = (0.0380, 0.0386)
    clock.tick(1.0)
    manager.advance()
    assert len(client.amends) == 1


def test_deadline_crosses_at_a_bounded_price_then_cancels_the_remainder() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    client.tickers["LAUSDT"] = (0.0376, 0.0380)

    clock.tick(121.0)
    manager.advance()
    assert len(client.amends) == 1
    crossed_price = float(client.amends[0]["price"])
    assert crossed_price >= 0.0380  # through the far touch, bounded by the pad
    assert crossed_price <= 0.0380 * 1.01
    assert manager.symbol_has_active_quote("LAUSDT")  # cross grace still runs

    # Remainder never clears: the manager takes the order down once.
    clock.tick(21.0)
    manager.advance()
    assert client.cancels == ["cmd-1"]
    manager.advance()
    assert client.cancels == ["cmd-1"]

    # The venue cancel lands through the stream; the quote is then dropped.
    order.status = "partially_filled_cancelled"
    manager.advance()
    assert "cmd-1" not in manager._quotes


def test_amend_reject_is_survivable() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    client.tickers["LAUSDT"] = (0.0378, 0.0384)
    client.amend_error = BybitRequestRejected("order not exists or too late to amend")
    clock.tick(16.0)
    manager.advance()  # must not raise
    quote = manager._quotes[order.command_id]
    assert quote.amend_count == 0
    assert quote.price == pytest.approx(0.0376)


def test_restart_adopts_a_resting_venue_limit() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    created_ms = (clock.now_ns - int(30e9)) // 1_000_000
    client.open_orders.append(
        {
            "orderLinkId": "cmd-1",
            "orderType": "Limit",
            "price": "0.0375",
            "createdTime": str(created_ms),
        }
    )
    manager.advance()
    quote = manager._quotes["cmd-1"]
    assert quote.price == pytest.approx(0.0375)
    assert quote.metadata["adopted_after_restart"] is True
    # Deadline anchored to the venue creation time, not the adoption instant.
    assert quote.deadline_ns == created_ms * 1_000_000 + int(120e9)


def test_adoption_ignores_market_orders_and_backs_off() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    probes: list[int] = []
    original = client.get_open_orders

    def counting(**params: Any) -> list[dict[str, Any]]:
        probes.append(1)
        return original(**params)

    client.get_open_orders = counting  # type: ignore[method-assign]
    manager.advance()
    manager.advance()
    assert len(probes) == 1  # 10s probe backoff
    clock.tick(11.0)
    manager.advance()
    assert len(probes) == 2
    assert manager._quotes == {}


def test_active_quote_probe_expires_after_window_and_grace() -> None:
    manager, _, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    assert manager.symbol_has_active_quote("LAUSDT")
    assert not manager.symbol_has_active_quote("OTHERUSDT")
    clock.tick(120.0 + 2 * 20.0 + 1.0)
    assert not manager.symbol_has_active_quote("LAUSDT")
