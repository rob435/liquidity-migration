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
        self.cancel_error: Exception | None = None
        # Counts refused attempts too, which ``amends`` cannot.
        self.amend_attempts = 0
        # Every REST tickers read, hit or miss: the blocking round trips.
        self.ticker_reads = 0

    def get_tickers(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        self.ticker_reads += 1
        if symbol and symbol in self.tickers:
            bid, ask = self.tickers[symbol]
            return [{"symbol": symbol, "bid1Price": str(bid), "ask1Price": str(ask)}]
        return []

    def get_open_orders(self, **params: Any) -> list[dict[str, Any]]:
        link = params.get("order_link_id")
        return [row for row in self.open_orders if not link or row.get("orderLinkId") == link]

    def amend_order(self, *, symbol: str, order_link_id: str, price: str) -> dict[str, Any]:
        self.amend_attempts += 1
        if self.amend_error is not None:
            raise self.amend_error
        self.amends.append({"symbol": symbol, "orderLinkId": order_link_id, "price": price})
        return {}

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, Any]:
        self.cancels.append(order_link_id)
        if self.cancel_error is not None:
            raise self.cancel_error
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
    clip_fraction: float = 1.0,
    min_clip_notional: float = 100.0,
    touch_source: Any = None,
) -> tuple[EntryQuoteManager, FakeClient, FakeKernel, FakeClock]:
    client = client or FakeClient()
    kernel = kernel or FakeKernel()
    clock = clock or FakeClock()
    manager = EntryQuoteManager(
        client,
        config=EntryQuoteConfig(
            window_seconds=window,
            clip_touch_fraction=clip_fraction,
            min_clip_notional_usdt=min_clip_notional,
        ),
        instrument_rules=rules_for("LAUSDT", tick),
        kernel=kernel,
        clock=clock,
        entry_stop_verifier=verifier,
        touch_source=touch_source,
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

    manager, _, kernel, clock = build_manager(verifier=verifier)
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
    # The terminal state outlives the fill until the probe horizon (so a
    # sliced entry's exemption never flickers), then expires.
    assert manager.symbol_has_active_quote("LAUSDT")
    clock.tick(120.0 + 2 * 20.0 + 1.0)
    manager.advance()
    assert not manager.symbol_has_active_quote("LAUSDT")


def test_plan_places_by_book_lean() -> None:
    manager, _, _, _ = build_manager()
    # The book leans toward the buy (bid-heavy): improve one tick inside.
    assert (
        manager.plan_entry_quote(
            symbol="LAUSDT", is_buy=True, bid=0.0376, ask=0.0380, bid_qty=900.0, ask_qty=100.0
        )
        == "0.0377"
    )
    # The book leans hard against the buy (ask-heavy): rest one tick behind.
    assert (
        manager.plan_entry_quote(
            symbol="LAUSDT", is_buy=True, bid=0.0376, ask=0.0380, bid_qty=100.0, ask_qty=900.0
        )
        == "0.0375"
    )
    # Balanced, or without displayed sizes: join the touch, as always.
    assert (
        manager.plan_entry_quote(
            symbol="LAUSDT", is_buy=True, bid=0.0376, ask=0.0380, bid_qty=500.0, ask_qty=500.0
        )
        == "0.0376"
    )
    assert manager.plan_entry_quote(symbol="LAUSDT", is_buy=True, bid=0.0376, ask=0.0380) == "0.0376"
    # Mirrored for a sell.
    assert (
        manager.plan_entry_quote(
            symbol="LAUSDT", is_buy=False, bid=0.0376, ask=0.0380, bid_qty=100.0, ask_qty=900.0
        )
        == "0.0379"
    )


def test_drift_against_the_entry_crosses_before_the_deadline() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(
        command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376, decision_mid=0.0378
    )
    # The market runs far above the decision mid: waiting has already cost
    # more than the spread, so the quote crosses well before the 120 s end.
    client.tickers["LAUSDT"] = (0.0390, 0.0394)
    clock.tick(10.0)
    manager.advance()
    quote = manager._quotes[order.command_id]
    assert quote.crossed
    assert len(client.amends) == 1
    assert float(client.amends[0]["price"]) >= 0.0394


def test_without_a_decision_mid_the_drift_cross_stays_off() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    client.tickers["LAUSDT"] = (0.0390, 0.0394)
    clock.tick(10.0)
    manager.advance()
    quote = manager._quotes[order.command_id]
    assert not quote.crossed  # it rejoined the touch instead
    assert client.amends and client.amends[0]["price"] == "0.039"


def test_adverse_lean_holds_back_early_then_urgency_joins_and_improves() -> None:
    # Ask-heavy book: the resting buy placed one tick behind stays there
    # early, joins the touch past half the window, improves near the end.
    touch = {"value": (0.0378, 0.0382, 100.0, 900.0)}
    manager, client, kernel, clock = build_manager(touch_source=lambda _s: touch["value"])
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0377)

    clock.tick(10.0)
    manager.advance()
    assert client.amends == []  # behind the touch on purpose, early window

    clock.tick(55.0)  # past urgency_join_frac
    manager.advance()
    assert client.amends == [{"symbol": "LAUSDT", "orderLinkId": "cmd-1", "price": "0.0378"}]

    clock.tick(40.0)  # past urgency_improve_frac (105 s of 120)
    manager.advance()
    assert client.amends[-1]["price"] == "0.0379"


def test_favorable_lean_jumps_the_queue_from_the_touch() -> None:
    touch = {"value": (0.0378, 0.0382, 900.0, 100.0)}
    manager, client, kernel, clock = build_manager(touch_source=lambda _s: touch["value"])
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0378)
    clock.tick(5.0)
    manager.advance()
    assert client.amends == [{"symbol": "LAUSDT", "orderLinkId": "cmd-1", "price": "0.0379"}]
    # Once inside the spread and ahead of the displayed touch, hold.
    clock.tick(5.0)
    manager.advance()
    assert len(client.amends) == 1


def test_touch_source_outranks_the_rest_fallback() -> None:
    manager, client, kernel, clock = build_manager(
        touch_source=lambda _s: (0.0380, 0.0384, 500.0, 500.0)
    )
    client.tickers["LAUSDT"] = (0.0300, 0.0304)  # stale REST would mislead
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    clock.tick(5.0)
    manager.advance()
    assert client.amends == [{"symbol": "LAUSDT", "orderLinkId": "cmd-1", "price": "0.038"}]


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


def test_a_spent_amend_budget_cannot_hide_the_late_window_urgency_ladder() -> None:
    """The budget spanned the window at 15s reprices; at 3s it spans a fifth of it.

    A quote whose touch keeps moving spends all 8 amends inside the first 24s,
    and before this the escalations at half (join) and 85% (improve) of the
    window were then structurally unreachable — `_desired_price` was never even
    called. The outcome was not a stranded order but a would-be maker fill
    degraded to a taker cross at the deadline.
    """

    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)

    # Spend the whole budget chasing a rising touch inside the first 24s.
    price = 0.0376
    for _ in range(manager.config.max_amends):
        price += 0.0001
        client.tickers["LAUSDT"] = (price, price + 0.0004)
        clock.tick(manager.config.reprice_seconds)
        manager.advance()
    assert len(client.amends) == manager.config.max_amends

    # Still early in the window: the budget holds and nothing further amends.
    price += 0.0001
    client.tickers["LAUSDT"] = (price, price + 0.0004)
    clock.tick(manager.config.reprice_seconds)
    manager.advance()
    assert len(client.amends) == manager.config.max_amends

    # Past the join threshold the escalation outranks the spent budget.
    clock.tick(manager.config.window_seconds * manager.config.urgency_join_frac)
    price += 0.0001
    client.tickers["LAUSDT"] = (price, price + 0.0004)
    manager.advance()
    assert len(client.amends) == manager.config.max_amends + 1
    assert client.amends[-1]["price"] == f"{price:.4f}"


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

    # The venue cancel lands through the stream; the state is retained (the
    # convergence exemption covers the gap to the next clip) and dropped once
    # past the probe horizon.
    order.status = "partially_filled_cancelled"
    manager.advance()
    assert "cmd-1" in manager._quotes
    assert manager.symbol_has_active_quote("LAUSDT")
    clock.tick(30.0)
    manager.advance()
    assert "cmd-1" not in manager._quotes


def test_an_unpriceable_cross_retries_until_the_grace_runs_out() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)

    # The touch is unreadable exactly when the window closes.
    client.tickers.pop("LAUSDT", None)
    clock.tick(121.0)
    manager.advance()
    assert client.amends == []

    # The book comes back inside the grace: the cross must still happen rather
    # than the order resting untouched until the cancel. The retry is paced on
    # the reprice cadence, so a pass inside it does nothing.
    client.tickers["LAUSDT"] = (0.0376, 0.0380)
    clock.tick(1.0)
    manager.advance()
    assert client.amends == []

    clock.tick(3.0)
    manager.advance()
    assert len(client.amends) == 1
    assert float(client.amends[0]["price"]) >= 0.0380
    assert client.cancels == []


def test_a_rejected_cancel_is_retried_instead_of_abandoning_a_live_order() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    client.tickers["LAUSDT"] = (0.0376, 0.0380)

    clock.tick(121.0)
    manager.advance()  # crosses at the deadline
    clock.tick(21.0)

    # This is the only cancel in the live runtime. A rejected one that latched
    # would leave a marketable limit resting at the venue for good.
    client.cancel_error = BybitRequestRejected("cancel refused")
    manager.advance()
    assert client.cancels == ["cmd-1"]

    # Paced on the reprice cadence, so a venue that keeps refusing cannot
    # become a hot loop, but the attempt does come back.
    manager.advance()
    assert client.cancels == ["cmd-1"]
    clock.tick(manager.config.reprice_seconds + 0.1)
    manager.advance()
    assert client.cancels == ["cmd-1", "cmd-1"]

    # Once the venue accepts, the manager stops asking.
    client.cancel_error = None
    clock.tick(manager.config.reprice_seconds + 0.1)
    manager.advance()
    assert client.cancels == ["cmd-1", "cmd-1", "cmd-1"]
    clock.tick(manager.config.reprice_seconds + 0.1)
    manager.advance()
    assert client.cancels == ["cmd-1", "cmd-1", "cmd-1"]


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


def test_clip_caps_a_large_entry_to_the_displayed_touch() -> None:
    manager, _, _, _ = build_manager()
    # 120 units displayed at the bid: the commanded 1,000 rests as 120.
    assert manager.plan_entry_clip(
        symbol="LAUSDT", is_buy=True, command_qty=1000.0, price=1.0,
        bid_qty=120.0, ask_qty=9999.0,
    ) == "120"
    # A resting sell joins the ask queue, so it reads the ask size.
    assert manager.plan_entry_clip(
        symbol="LAUSDT", is_buy=False, command_qty=1000.0, price=1.0,
        bid_qty=9999.0, ask_qty=120.0,
    ) == "120"


def test_clip_floor_stops_a_near_empty_touch_from_making_dust_windows() -> None:
    manager, _, _, _ = build_manager()
    # 3 units displayed, floor 100 USDT at price 1.0 -> clip 100 units.
    assert manager.plan_entry_clip(
        symbol="LAUSDT", is_buy=True, command_qty=1000.0, price=1.0,
        bid_qty=3.0, ask_qty=3.0,
    ) == "100"


def test_clip_places_the_full_command_when_blind_disabled_or_not_needed() -> None:
    manager, _, _, _ = build_manager()
    # No displayed size to read.
    assert manager.plan_entry_clip(
        symbol="LAUSDT", is_buy=True, command_qty=1000.0, price=1.0,
        bid_qty=None, ask_qty=None,
    ) is None
    # The touch absorbs the whole command.
    assert manager.plan_entry_clip(
        symbol="LAUSDT", is_buy=True, command_qty=100.0, price=1.0,
        bid_qty=5000.0, ask_qty=5000.0,
    ) is None
    disabled, _, _, _ = build_manager(clip_fraction=0.0)
    assert disabled.plan_entry_clip(
        symbol="LAUSDT", is_buy=True, command_qty=1000.0, price=1.0,
        bid_qty=120.0, ask_qty=120.0,
    ) is None


def test_clip_never_strands_a_venue_minimum_remainder() -> None:
    manager, _, _, _ = build_manager()
    # min_notional is 5 at price 1.0: a 104 command capped at 100 would leave
    # a 4-unit tail no future order could place, so the whole command rests.
    assert manager.plan_entry_clip(
        symbol="LAUSDT", is_buy=True, command_qty=104.0, price=1.0,
        bid_qty=100.0, ask_qty=100.0,
    ) is None
    # A 105 command leaves a viable 5-unit tail and is capped.
    assert manager.plan_entry_clip(
        symbol="LAUSDT", is_buy=True, command_qty=105.0, price=1.0,
        bid_qty=100.0, ask_qty=100.0,
    ) == "100"


def test_terminal_quote_keeps_the_probe_alive_until_the_horizon() -> None:
    manager, _, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    # The clip's order terminates 140s in (cross grace exhausted, cancelled).
    clock.tick(140.0)
    kernel.state.orders[order.command_id] = working_entry(
        status="partially_filled_cancelled", filled=40.0
    )
    manager.advance()
    # The convergence exemption must cover the seconds between this window
    # and the next clip's create, so the state survives its own terminal.
    assert manager.symbol_has_active_quote("LAUSDT")
    clock.tick(25.0)  # past window + 2x cross grace
    manager.advance()
    assert not manager.symbol_has_active_quote("LAUSDT")
    assert manager._quotes == {}


def test_a_rejected_cross_amend_retries_instead_of_resting_at_the_passive_price() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    client.tickers["LAUSDT"] = (0.0376, 0.0380)

    # The venue refuses the window-end cross. Latching it would leave the entry
    # sitting at its old passive price until the cancel, never having crossed.
    client.amend_error = BybitRequestRejected("amend refused")
    clock.tick(121.0)
    manager.advance()
    assert client.cancels == []

    client.amend_error = None
    clock.tick(3.5)
    manager.advance()
    assert len(client.amends) == 1
    assert float(client.amends[0]["price"]) >= 0.0380


def test_the_cross_retry_is_paced_so_a_refusing_venue_is_not_hammered() -> None:
    """``advance`` runs every owner tick, so an ungated retry is a hot loop.

    Each attempt is a touch read plus a signed amend at roughly 175 ms. Unpaced
    over the 20 s grace at 10 Hz that is ~200 of each, which blocks the owner
    loop for longer than the window and trips the venue's order-rate limit.
    """

    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    client.tickers["LAUSDT"] = (0.0376, 0.0380)
    client.amend_error = BybitRequestRejected("amend refused")

    clock.tick(121.0)
    # The whole 20 s grace at the owner's ~10 Hz tick.
    for _ in range(200):
        manager.advance()
        clock.tick(0.1)

    # Paced on reprice_seconds (3 s), so about one attempt per 3 s of grace,
    # not one per pass.
    assert 1 <= client.amend_attempts <= 8, client.amend_attempts


def test_a_tick_wake_reprices_inside_the_periodic_interval() -> None:
    """``reprice_now`` is the tick-driven bypass: the caller saw the quoted
    book actually move, so the 3 s gate has nothing left to protect. The
    bypass reads the in-memory book only — production always wires the
    recorder as the touch source, and the ticked symbol's touch is by
    definition readable there."""

    touch: dict[str, tuple[float, float, float, float]] = {}
    manager, client, kernel, clock = build_manager(
        touch_source=lambda symbol: touch.get(symbol)
    )
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)

    # Touch moved up 1 s after registration: the plain pass holds...
    touch["LAUSDT"] = (0.0378, 0.0384, 100.0, 100.0)
    clock.tick(1.0)
    manager.advance()
    assert client.amends == []

    # ...and the tick-driven pass chases at once.
    manager.advance(reprice_now=True)
    assert client.amends == [{"symbol": "LAUSDT", "orderLinkId": "cmd-1", "price": "0.0378"}]

    # An unmoved touch amends nothing even with the gate bypassed.
    manager.advance(reprice_now=True)
    assert len(client.amends) == 1


def test_a_tick_wake_does_not_bypass_cross_or_cancel_pacing() -> None:
    """The pacing on cross and cancel retries exists to stop a refusing venue
    becoming a hot loop; a book tick must not reopen that."""

    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    client.tickers["LAUSDT"] = (0.0376, 0.0380)
    client.amend_error = BybitRequestRejected("amend refused")

    # Window over: the cross attempt fails and starts the grace clock.
    clock.tick(manager.config.window_seconds + 1.0)
    manager.advance()
    assert client.amend_attempts == 1

    # Inside the cross-retry pacing a tick wake changes nothing.
    clock.tick(1.0)
    manager.advance(reprice_now=True)
    assert client.amend_attempts == 1

    # Grace over: the cancel is attempted and refused once.
    client.cancel_error = BybitRequestRejected("cancel refused")
    clock.tick(manager.config.cross_grace_seconds)
    manager.advance()
    assert client.cancels == ["cmd-1"]

    # Inside the cancel pacing a tick wake changes nothing either.
    clock.tick(1.0)
    manager.advance(reprice_now=True)
    assert client.cancels == ["cmd-1"]


def test_active_resting_symbols_tracks_only_quotes_still_resting() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    assert manager.active_resting_symbols() == {"LAUSDT"}

    # Crossing at the deadline moves the quote to its own paced clock.
    client.tickers["LAUSDT"] = (0.0376, 0.0380)
    clock.tick(manager.config.window_seconds + 1.0)
    manager.advance()
    assert manager.active_resting_symbols() == set()


def test_active_resting_symbols_drops_terminal_orders_and_disabled_config() -> None:
    manager, client, kernel, clock = build_manager()
    order = working_entry(status="filled", filled=100.0)
    kernel.state.orders[order.command_id] = order
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    assert manager.active_resting_symbols() == set()

    disabled, _, _, _ = build_manager(window=0.0)
    assert disabled.active_resting_symbols() == set()


def test_a_tick_wake_never_pays_a_rest_read_for_a_dark_book() -> None:
    """The wake proves SOME quoted book moved. A quote whose own book is
    unreadable must keep its blocking REST fallback on the 3 s clock — one
    read per interval — not one per wake on the owner loop."""

    manager, client, kernel, clock = build_manager()
    order = working_entry()
    kernel.state.orders[order.command_id] = order
    kernel.state.working_order_ids.add(order.command_id)
    manager.register(command_id=order.command_id, symbol="LAUSDT", is_buy=True, price=0.0376)
    # No touch_source and no REST rows: the book is dark either way.

    # Wakes arrive at the floor rate inside the periodic interval: no reads.
    for _ in range(5):
        clock.tick(0.2)
        manager.advance(reprice_now=True)
    assert client.ticker_reads == 0

    # The periodic schedule still pays its one read per interval.
    clock.tick(manager.config.reprice_seconds)
    manager.advance()
    assert client.ticker_reads == 1

    # And a failed read stays paced: the very next wake reads nothing.
    manager.advance(reprice_now=True)
    assert client.ticker_reads == 1
