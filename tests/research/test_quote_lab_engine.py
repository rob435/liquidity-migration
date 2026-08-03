"""Deterministic quote-lab engine tests: fake venue, fake touch, fake clock."""

from __future__ import annotations

import json

import pytest

from liquidity_migration.research.execution.quote_lab.engine import (
    EngineConfig,
    QuoteEngine,
    SymbolSpec,
    WouldCrossReject,
)
from liquidity_migration.research.execution.quote_lab.records import (
    TERMINAL_ABORTED,
    TERMINAL_FILLED,
    TERMINAL_REJECTED_WOULD_CROSS,
    TERMINAL_TIMEOUT_CANCELLED,
    JsonlWriter,
)

SYMBOL = "AAAUSDT"


class FakeClock:
    def __init__(self) -> None:
        self._ns = 1_700_000_000_000_000_000
        self._monotonic = 1000.0

    def now_ns(self) -> int:
        return self._ns

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._ns += int(seconds * 1e9)


class FakeTouch:
    def __init__(self) -> None:
        self.quotes: dict[str, tuple[float, float]] = {}
        self.health: dict[str, bool] = {}

    def set(self, symbol: str, bid: float, ask: float, healthy: bool = True) -> None:
        self.quotes[symbol] = (bid, ask)
        self.health[symbol] = healthy

    def best_bid(self, symbol: str) -> float | None:
        quote = self.quotes.get(symbol)
        return quote[0] if quote else None

    def best_ask(self, symbol: str) -> float | None:
        quote = self.quotes.get(symbol)
        return quote[1] if quote else None

    def healthy(self, symbol: str) -> bool:
        return self.health.get(symbol, False)


class FakeVenue:
    def __init__(self) -> None:
        self.open: dict[str, dict] = {}
        self.history: dict[str, dict] = {}
        self.pos: dict[str, dict] = {}
        self.place_behaviors: dict[str, list[str]] = {}
        self.fees: dict[str, tuple[float | None, bool]] = {}
        self.calls: list[tuple] = []
        self.block_flatten = False

    def _behavior(self, symbol: str) -> str:
        queue = self.place_behaviors.get(symbol)
        return queue.pop(0) if queue else "ack"

    def place_post_only(self, symbol, side, price, qty, order_link_id):
        self.calls.append(("place_post_only", symbol, side, price, qty, order_link_id))
        behavior = self._behavior(symbol)
        if behavior == "reject":
            raise WouldCrossReject("would cross at create")
        if behavior == "boom":
            raise RuntimeError("venue exploded")
        row = {
            "symbol": symbol,
            "orderLinkId": order_link_id,
            "side": side,
            "price": price,
            "qty": qty,
            "cumExecQty": "0",
            "orderStatus": "New",
        }
        if behavior == "ghost":
            # accepted then immediately cancelled by the venue: post-create would-cross
            self.history[order_link_id] = {**row, "orderStatus": "Cancelled"}
        else:
            self.open[order_link_id] = row
        return {}

    def place_reduce_only_market(self, symbol, side, qty, order_link_id):
        self.calls.append(("flatten", symbol, side, qty, order_link_id))
        if not self.block_flatten:
            self.pos.pop(symbol, None)
        return {}

    def cancel(self, symbol, order_link_id):
        self.calls.append(("cancel", symbol, order_link_id))
        row = self.open.pop(order_link_id, None)
        if row is None:
            raise RuntimeError("order not exists or too late to cancel")
        executed = float(row.get("cumExecQty") or 0.0)
        status = "Cancelled" if executed == 0.0 else "PartiallyFilledCanceled"
        self.history[order_link_id] = {**row, "orderStatus": status}

    def open_orders(self):
        return list(self.open.values())

    def positions(self):
        return list(self.pos.values())

    def order_row(self, symbol, order_link_id):
        return self.history.get(order_link_id)

    def fill_fee_bp(self, symbol, order_link_id):
        return self.fees.get(order_link_id, (2.0, True))

    # -- test helpers -------------------------------------------------------

    def fill(self, order_link_id: str, price: float | None = None) -> None:
        row = self.open.pop(order_link_id)
        avg = price if price is not None else float(row["price"])
        self.history[order_link_id] = {
            **row,
            "orderStatus": "Filled",
            "cumExecQty": row["qty"],
            "avgPrice": str(avg),
        }
        self._add_position(row["symbol"], row["side"], float(row["qty"]), avg)

    def partial_fill_open(self, order_link_id: str, qty: float) -> None:
        row = self.open[order_link_id]
        row["cumExecQty"] = str(qty)
        row["orderStatus"] = "PartiallyFilled"
        self._add_position(row["symbol"], row["side"], qty, float(row["price"]))

    def _add_position(self, symbol: str, side: str, size: float, price: float) -> None:
        self.pos[symbol] = {
            "symbol": symbol,
            "side": side,
            "size": str(size),
            "avgPrice": str(price),
            "positionValue": str(size * price),
        }


def make_engine(venue, touch, clock, symbols=(SYMBOL,), **overrides):
    specs = [
        SymbolSpec(
            symbol=symbol,
            tick_size=0.001,
            qty_step=1.0,
            min_qty=1.0,
            quote_qty=10.0,
            price_decimals=3,
            qty_decimals=0,
        )
        for symbol in symbols
    ]
    config = EngineConfig(**overrides)
    return QuoteEngine(venue=venue, touch=touch, clock=clock, config=config, specs=specs)


def _fresh(**overrides):
    venue = FakeVenue()
    touch = FakeTouch()
    touch.set(SYMBOL, 1.000, 1.002)
    clock = FakeClock()
    engine = make_engine(venue, touch, clock, **overrides)
    return venue, touch, clock, engine


def _kinds(engine):
    return [event.kind for event in engine.events]


def test_places_post_only_at_touch():
    venue, touch, clock, engine = _fresh()
    engine.tick()
    places = [call for call in venue.calls if call[0] == "place_post_only"]
    assert len(places) == 1
    _, symbol, side, price, qty, link = places[0]
    assert symbol == SYMBOL and side == "Buy"
    assert price == "1.000"  # buy quotes rest at the bid
    assert qty == "10"
    assert _kinds(engine) == ["place_submitted", "place_acked"]
    assert link in venue.open


def test_chase_reprice_after_touch_move():
    venue, touch, clock, engine = _fresh()
    engine.tick()
    first_link = venue.calls[-1][5]
    touch.set(SYMBOL, 1.004, 1.006)  # 4 ticks > chase_ticks 2
    clock.advance(1.0)
    engine.tick()
    assert ("cancel", SYMBOL, first_link) in venue.calls
    places = [call for call in venue.calls if call[0] == "place_post_only"]
    assert len(places) == 2
    assert places[1][3] == "1.004"
    assert "reprice_cancel" in _kinds(engine)
    assert engine.attempts == []  # reprice stays inside the same attempt


def test_reprice_on_staleness():
    venue, touch, clock, engine = _fresh(reprice_seconds=15.0)
    engine.tick()
    clock.advance(16.0)  # touch unchanged, quote is stale
    engine.tick()
    places = [call for call in venue.calls if call[0] == "place_post_only"]
    assert len(places) == 2
    assert places[1][3] == "1.000"
    stale_events = [event for event in engine.events if event.kind == "reprice_cancel"]
    assert stale_events and stale_events[0].detail == "stale"


def test_would_cross_reject_retry_then_terminal():
    venue, touch, clock, engine = _fresh()
    venue.place_behaviors[SYMBOL] = ["reject", "reject", "reject", "reject"]
    engine.tick()
    engine.tick()  # inside the 5s backoff: no retry yet
    assert len([call for call in venue.calls if call[0] == "place_post_only"]) == 1
    for _ in range(3):
        clock.advance(5.0)
        engine.tick()
    assert len([call for call in venue.calls if call[0] == "place_post_only"]) == 4
    assert _kinds(engine).count("place_rejected_would_cross") == 4
    assert len(engine.attempts) == 1
    attempt = engine.attempts[0]
    assert attempt.terminal_state == TERMINAL_REJECTED_WOULD_CROSS
    assert len(attempt.order_link_ids) == 4
    assert attempt.metadata["would_cross_rejects"] == 4


def test_post_create_cancel_counts_as_would_cross():
    venue, touch, clock, engine = _fresh()
    venue.place_behaviors[SYMBOL] = ["ghost"]
    engine.tick()  # placed, venue reports success then cancels it
    clock.advance(1.0)
    engine.tick()  # order gone from the book without ever being seen open
    assert "place_rejected_would_cross" in _kinds(engine)
    assert engine.attempts == []  # retrying within the same attempt


def test_fill_flatten_cooldown_then_next_attempt():
    venue, touch, clock, engine = _fresh(cooldown_seconds=10.0)
    engine.tick()
    link = venue.calls[-1][5]
    venue.fill(link, price=1.000)
    clock.advance(1.0)
    engine.tick()  # detect fill, submit reduce-only flatten
    flattens = [call for call in venue.calls if call[0] == "flatten"]
    assert flattens == [("flatten", SYMBOL, "Sell", "10", flattens[0][4])]
    assert "filled" in _kinds(engine) and "flatten_submitted" in _kinds(engine)
    clock.advance(1.0)
    engine.tick()  # verify the position is gone, close the attempt
    assert "flattened" in _kinds(engine)
    assert len(engine.attempts) == 1
    attempt = engine.attempts[0]
    assert attempt.terminal_state == TERMINAL_FILLED
    assert attempt.fill_price == pytest.approx(1.000)
    assert attempt.fill_fee_bp == pytest.approx(2.0)
    assert attempt.fee_observed is True
    n_places = len([call for call in venue.calls if call[0] == "place_post_only"])
    clock.advance(1.0)
    engine.tick()  # still cooling down: no new quote
    assert len([call for call in venue.calls if call[0] == "place_post_only"]) == n_places
    clock.advance(10.0)
    engine.tick()  # cooldown over: next attempt starts
    assert len([call for call in venue.calls if call[0] == "place_post_only"]) == n_places + 1
    assert engine.attempts[0].attempt_index == 0


def test_partial_fill_cancels_remainder_and_flattens():
    venue, touch, clock, engine = _fresh()
    engine.tick()
    link = venue.calls[-1][5]
    venue.partial_fill_open(link, 4.0)
    clock.advance(1.0)
    engine.tick()  # partial fill: cancel the rest, flatten what filled
    assert "partial_fill" in _kinds(engine)
    flattens = [call for call in venue.calls if call[0] == "flatten"]
    assert flattens and flattens[0][3] == "4.0"
    clock.advance(1.0)
    engine.tick()
    attempt = engine.attempts[0]
    assert attempt.terminal_state == TERMINAL_FILLED
    assert attempt.metadata["fill_qty"] == pytest.approx(4.0)


def test_attempt_timeout_terminal():
    venue, touch, clock, engine = _fresh(reprice_seconds=1000.0, attempt_timeout_seconds=120.0)
    engine.tick()
    link = venue.calls[-1][5]
    clock.advance(121.0)
    engine.tick()
    assert ("cancel", SYMBOL, link) in venue.calls
    assert len(engine.attempts) == 1
    assert engine.attempts[0].terminal_state == TERMINAL_TIMEOUT_CANCELLED


def test_inventory_cap_triggers_global_shutdown():
    venue, touch, clock, engine = _fresh(max_inventory_notional_usdt=200.0)
    engine.tick()
    working_link = venue.calls[-1][5]
    venue.pos["ZZZUSDT"] = {
        "symbol": "ZZZUSDT", "side": "Buy", "size": "500", "positionValue": "500",
    }
    clock.advance(1.0)
    engine.tick()
    assert engine.aborted
    assert "inventory_cap_exceeded" in engine.abort_reason
    assert ("cancel", SYMBOL, working_link) in venue.calls
    assert any(call[0] == "flatten" and call[1] == "ZZZUSDT" for call in venue.calls)
    assert engine.shutdown_flat is True
    assert engine.attempts[0].terminal_state == TERMINAL_ABORTED
    n_calls = len(venue.calls)
    engine.tick()  # aborted engine is inert
    assert len(venue.calls) == n_calls


def test_shutdown_cancels_workings_and_flattens():
    venue, touch, clock, engine = _fresh()
    engine.tick()
    working_link = venue.calls[-1][5]
    venue.pos["ZZZUSDT"] = {
        "symbol": "ZZZUSDT", "side": "Sell", "size": "3", "positionValue": "30",
    }
    engine.shutdown("operator_stop")
    assert ("cancel", SYMBOL, working_link) in venue.calls
    flattens = [call for call in venue.calls if call[0] == "flatten"]
    assert flattens and flattens[0][1] == "ZZZUSDT" and flattens[0][2] == "Buy"
    kinds = _kinds(engine)
    assert "cancel_submitted" in kinds and "cancelled" in kinds
    assert "flatten_submitted" in kinds and "flattened" in kinds
    assert engine.attempts[0].terminal_state == TERMINAL_ABORTED
    assert engine.shutdown_flat is True
    assert not engine.active
    n_calls = len(venue.calls)
    engine.shutdown("again")  # idempotent
    assert len(venue.calls) == n_calls
    assert engine.shutdown_reason == "operator_stop"


def test_unhealthy_touch_parks_symbol_until_healthy():
    venue, touch, clock, engine = _fresh(cooldown_seconds=0.0)
    engine.tick()
    link = venue.calls[-1][5]
    touch.set(SYMBOL, 1.000, 1.002, healthy=False)
    clock.advance(1.0)
    engine.tick()  # cancel the working order, park the symbol
    assert ("cancel", SYMBOL, link) in venue.calls
    assert engine.attempts and engine.attempts[0].terminal_state == TERMINAL_ABORTED
    n_places = len([call for call in venue.calls if call[0] == "place_post_only"])
    clock.advance(5.0)
    engine.tick()  # still unhealthy: parked, no quoting
    assert len([call for call in venue.calls if call[0] == "place_post_only"]) == n_places
    touch.set(SYMBOL, 1.000, 1.002, healthy=True)
    clock.advance(1.0)
    engine.tick()
    assert len([call for call in venue.calls if call[0] == "place_post_only"]) == n_places + 1


def test_max_attempts_per_symbol_stops_quoting():
    venue, touch, clock, engine = _fresh(max_attempts_per_symbol=1, cooldown_seconds=0.0)
    venue.place_behaviors[SYMBOL] = ["reject"] * 4
    for _ in range(6):
        engine.tick()
        clock.advance(5.0)
    assert len(engine.attempts) == 1
    assert len([call for call in venue.calls if call[0] == "place_post_only"]) == 4


def test_events_journal_contents(tmp_path):
    venue = FakeVenue()
    touch = FakeTouch()
    touch.set(SYMBOL, 1.000, 1.002)
    clock = FakeClock()
    from dataclasses import asdict

    events_path = tmp_path / "events.jsonl"
    attempts_path = tmp_path / "attempts.jsonl"
    events_writer = JsonlWriter(events_path, fsync_every=1)
    attempts_writer = JsonlWriter(attempts_path, fsync_every=1)
    specs = [
        SymbolSpec(
            symbol=SYMBOL, tick_size=0.001, qty_step=1.0, min_qty=1.0,
            quote_qty=10.0, price_decimals=3, qty_decimals=0,
        )
    ]
    engine = QuoteEngine(
        venue=venue, touch=touch, clock=clock, config=EngineConfig(), specs=specs,
        on_event=lambda event: events_writer.write(asdict(event)),
        on_attempt=lambda attempt: attempts_writer.write(asdict(attempt)),
    )
    engine.tick()
    venue.fill(venue.calls[-1][5], price=1.000)
    clock.advance(1.0)
    engine.tick()
    clock.advance(1.0)
    engine.tick()
    events_writer.close()
    attempts_writer.close()
    event_rows = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [row["kind"] for row in event_rows] == [
        "place_submitted", "place_acked", "filled", "flatten_submitted", "flattened",
    ]
    assert all(row["symbol"] == SYMBOL for row in event_rows)
    assert event_rows[0]["best_bid"] == pytest.approx(1.000)
    attempt_rows = [json.loads(line) for line in attempts_path.read_text().splitlines()]
    assert len(attempt_rows) == 1
    assert attempt_rows[0]["terminal_state"] == "filled"
    assert attempt_rows[0]["fill_price"] == pytest.approx(1.000)


def test_attempt_rollup_correctness():
    venue, touch, clock, engine = _fresh()
    engine.tick()  # first placement at 1.000
    touch.set(SYMBOL, 1.004, 1.006)
    clock.advance(1.0)
    engine.tick()  # chase reprice to 1.004
    second_link = venue.calls[-1][5]
    venue.fill(second_link, price=1.004)
    clock.advance(1.0)
    engine.tick()  # fill + flatten submit
    clock.advance(1.0)
    engine.tick()  # flatten verified, attempt closes
    assert len(engine.attempts) == 1
    attempt = engine.attempts[0]
    assert attempt.symbol == SYMBOL and attempt.side == "Buy"
    assert attempt.attempt_index == 0
    assert len(attempt.order_link_ids) == 2
    assert attempt.placed_prices == (1.000, 1.004)
    assert attempt.reprices == 1
    assert attempt.decision_bid == pytest.approx(1.000)
    assert attempt.decision_ask == pytest.approx(1.002)
    assert attempt.terminal_bid == pytest.approx(1.004)
    assert attempt.terminal_ask == pytest.approx(1.006)
    assert attempt.fill_price == pytest.approx(1.004)
    assert attempt.terminal_ts_ns > attempt.decision_ts_ns
    assert attempt.metadata["fill_qty"] == pytest.approx(10.0)


def test_flatten_retry_reclosing_current_size_heals():
    venue, touch, clock, engine = _fresh()
    engine.tick()
    link = venue.calls[-1][5]
    venue.fill(link, price=1.000)
    venue.block_flatten = True  # the first close never clears the position
    clock.advance(1.0)
    engine.tick()  # detect fill, submit the first flatten (blocked)
    assert "flatten_submitted" in _kinds(engine)
    clock.advance(21.0)
    venue.block_flatten = False  # the retry's close lands
    engine.tick()  # deadline passed -> retry round 1 re-closes current size
    retry_events = [e for e in engine.events if e.detail.startswith("retry_round_")]
    assert len(retry_events) == 1 and retry_events[0].qty == pytest.approx(10.0)
    clock.advance(1.0)
    engine.tick()  # position gone -> attempt closes filled, engine stays alive
    assert engine.active and not engine.aborted
    assert engine.attempts[0].terminal_state == TERMINAL_FILLED


def test_flatten_retry_rounds_exhausted_aborts():
    venue, touch, clock, engine = _fresh()
    engine.tick()
    link = venue.calls[-1][5]
    venue.fill(link, price=1.000)
    venue.block_flatten = True
    clock.advance(1.0)
    engine.tick()  # first flatten, blocked
    for _ in range(2):  # rounds 1 and 2
        clock.advance(21.0)
        engine.tick()
    retry_events = [e for e in engine.events if e.detail.startswith("retry_round_")]
    assert [e.detail for e in retry_events] == ["retry_round_1", "retry_round_2"]
    assert engine.active  # still trying
    clock.advance(21.0)
    engine.tick()  # rounds exhausted -> abort
    assert engine.aborted and engine.abort_reason.startswith("flatten_timeout")
    assert engine.attempts[0].terminal_state == TERMINAL_FILLED
    assert engine.attempts[0].metadata["terminal_detail"] == "flatten_unverified"
