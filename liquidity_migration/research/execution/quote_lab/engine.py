"""Multi-symbol post-only quoting engine: a single-threaded tick loop.

The engine rests one tiny post-only limit order at the touch per symbol,
reprices it when the touch moves or the quote goes stale, flattens any fill
immediately with a reduce-only market order, and emits a ``QuoteEvent`` for
every lifecycle step. Venue access goes through the ``QuoteVenue`` protocol so
this module never imports the credentialed transport; the runner script wires
the real client in.

Correctness controls (bounded loss, not ceremony):
- inventory guard: total open position notional above the cap cancels all
  working orders, flattens everything, and aborts the engine;
- orphan recovery: a position found on a symbol with no attempt in flight is
  flattened before any new quote;
- shutdown(reason) cancels every working order and flattens every position,
  and is idempotent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from liquidity_migration.research.execution.quote_lab.records import (
    TERMINAL_ABORTED,
    TERMINAL_FILLED,
    TERMINAL_REJECTED_WOULD_CROSS,
    TERMINAL_TIMEOUT_CANCELLED,
    QuoteAttempt,
    QuoteEvent,
)

WOULD_CROSS_BACKOFF_SECONDS = 5.0
MAX_WOULD_CROSS_RETRIES = 3
FLATTEN_TIMEOUT_SECONDS = 15.0

_GONE_STATUSES = frozenset({"Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"})

_IDLE = "idle"
_WORKING = "working"
_WAIT_RETRY = "wait_retry"
_FLATTENING = "flattening"
_COOLDOWN = "cooldown"
_PARKED = "parked"
_DONE = "done"


class WouldCrossReject(Exception):
    """A post-only order the venue refused (or cancelled) for would-cross."""


class QuoteLabVenueError(RuntimeError):
    """An unexpected venue failure; the runner shuts the engine down on it."""


class QuoteVenue(Protocol):
    def place_post_only(
        self, symbol: str, side: str, price: str, qty: str, order_link_id: str
    ) -> Mapping[str, Any]: ...

    def place_reduce_only_market(
        self, symbol: str, side: str, qty: str, order_link_id: str
    ) -> Mapping[str, Any]: ...

    def cancel(self, symbol: str, order_link_id: str) -> None: ...

    def open_orders(self) -> Sequence[Mapping[str, Any]]: ...

    def positions(self) -> Sequence[Mapping[str, Any]]: ...

    def order_row(self, symbol: str, order_link_id: str) -> Mapping[str, Any] | None: ...

    def fill_fee_bp(self, symbol: str, order_link_id: str) -> tuple[float | None, bool]: ...


class TouchSource(Protocol):
    def best_bid(self, symbol: str) -> float | None: ...

    def best_ask(self, symbol: str) -> float | None: ...

    def healthy(self, symbol: str) -> bool: ...


class Clock(Protocol):
    def now_ns(self) -> int: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Instrument grid and the precomputed quote size for one symbol."""

    symbol: str
    tick_size: float
    qty_step: float
    min_qty: float
    quote_qty: float
    price_decimals: int
    qty_decimals: int

    def __post_init__(self) -> None:
        if self.tick_size <= 0.0 or self.qty_step <= 0.0:
            raise ValueError(f"{self.symbol}: tick_size and qty_step must be positive")
        if self.quote_qty < self.min_qty:
            raise ValueError(f"{self.symbol}: quote_qty {self.quote_qty} is below min_qty {self.min_qty}")

    def price_text(self, price: float) -> str:
        snapped = round(round(price / self.tick_size) * self.tick_size, self.price_decimals)
        return f"{snapped:.{self.price_decimals}f}"

    def qty_text(self, qty: float) -> str:
        return f"{qty:.{self.qty_decimals}f}"


@dataclass(frozen=True, slots=True)
class EngineConfig:
    side: str = "Buy"
    reprice_seconds: float = 15.0
    chase_ticks: int = 2
    attempt_timeout_seconds: float = 120.0
    cooldown_seconds: float = 10.0
    max_inventory_notional_usdt: float = 200.0
    max_attempts_per_symbol: int = 0
    poll_seconds: float = 1.0
    max_runtime_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.side not in ("Buy", "Sell"):
            raise ValueError(f"unknown side: {self.side}")
        for name in ("reprice_seconds", "attempt_timeout_seconds", "poll_seconds"):
            if float(getattr(self, name)) <= 0.0:
                raise ValueError(f"{name} must be positive")
        if self.chase_ticks < 0 or self.cooldown_seconds < 0.0:
            raise ValueError("chase_ticks and cooldown_seconds must be nonnegative")
        if self.max_inventory_notional_usdt <= 0.0:
            raise ValueError("max_inventory_notional_usdt must be positive")
        if self.max_attempts_per_symbol < 0:
            raise ValueError("max_attempts_per_symbol must be nonnegative (0 = unlimited)")


@dataclass(slots=True)
class _Attempt:
    index: int
    decision_ts_ns: int
    decision_bid: float
    decision_ask: float
    started_monotonic: float
    link_ids: list[str] = field(default_factory=list)
    placed_prices: list[float] = field(default_factory=list)
    reprices: int = 0
    would_cross_rejects: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _SymbolState:
    spec: SymbolSpec
    phase: str = _IDLE
    attempt: _Attempt | None = None
    working_link: str = ""
    working_price: float = 0.0
    seen_open: bool = False
    partial_recorded: bool = False
    placed_monotonic: float = 0.0
    next_action_monotonic: float = 0.0
    attempts_done: int = 0
    fill_price: float = 0.0
    fill_qty_text: str = ""
    flatten_deadline_monotonic: float = 0.0


def _opposite(side: str) -> str:
    return "Sell" if side == "Buy" else "Buy"


def _executed_qty(row: Mapping[str, Any] | None) -> float:
    if row is None:
        return 0.0
    try:
        return float(row.get("cumExecQty") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _position_size(row: Mapping[str, Any]) -> float:
    try:
        return abs(float(row.get("size") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _position_notional(row: Mapping[str, Any]) -> float:
    for key in ("positionValue",):
        try:
            value = abs(float(row.get(key) or 0.0))
        except (TypeError, ValueError):
            value = 0.0
        if value > 0.0:
            return value
    size = _position_size(row)
    for key in ("markPrice", "avgPrice"):
        try:
            price = float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0.0:
            return size * price
    return 0.0


class QuoteEngine:
    """Drive every symbol's quote state machine from one ``tick()`` loop."""

    def __init__(
        self,
        *,
        venue: QuoteVenue,
        touch: TouchSource,
        clock: Clock,
        config: EngineConfig,
        specs: Sequence[SymbolSpec],
        on_event: Callable[[QuoteEvent], None] | None = None,
        on_attempt: Callable[[QuoteAttempt], None] | None = None,
        link_prefix: str = "lm-qlab-",
    ) -> None:
        if not specs:
            raise ValueError("at least one SymbolSpec is required")
        self.venue = venue
        self.touch = touch
        self.clock = clock
        self.config = config
        self._states: dict[str, _SymbolState] = {}
        for spec in specs:
            symbol = spec.symbol.upper()
            if symbol in self._states:
                raise ValueError(f"duplicate symbol spec: {symbol}")
            self._states[symbol] = _SymbolState(spec=spec)
        self._on_event = on_event
        self._on_attempt = on_attempt
        self._link_prefix = link_prefix
        self._link_seq = 0
        self.events: list[QuoteEvent] = []
        self.attempts: list[QuoteAttempt] = []
        self._started_monotonic: float | None = None
        self._shutdown_done = False
        self.shutdown_reason = ""
        self.abort_reason = ""
        self.shutdown_flat: bool | None = None

    @property
    def aborted(self) -> bool:
        return bool(self.abort_reason)

    @property
    def active(self) -> bool:
        return not self._shutdown_done

    # ------------------------------------------------------------------ events

    def _emit(
        self,
        *,
        symbol: str,
        kind: str,
        link: str = "",
        side: str = "",
        price: float | None = None,
        qty: float | None = None,
        detail: str = "",
    ) -> None:
        bid: float | None = None
        ask: float | None = None
        if symbol in self._states:
            try:
                bid = self.touch.best_bid(symbol)
                ask = self.touch.best_ask(symbol)
            except Exception:  # noqa: BLE001 - an event must never fail on a quote read
                bid = ask = None
        event = QuoteEvent(
            ts_ns=self.clock.now_ns(),
            symbol=symbol,
            kind=kind,
            order_link_id=link,
            side=side or self.config.side,
            price=price,
            qty=qty,
            best_bid=bid,
            best_ask=ask,
            detail=detail[:200],
        )
        self.events.append(event)
        if self._on_event is not None:
            self._on_event(event)

    def _new_link(self, symbol: str) -> str:
        self._link_seq += 1
        return f"{self._link_prefix}{symbol[:6].lower()}-{self._link_seq}-{self.clock.now_ns():x}"[-36:]

    # ------------------------------------------------------------ attempt close

    def _close_attempt(
        self,
        state: _SymbolState,
        terminal_state: str,
        *,
        fill_price: float | None = None,
        fill_fee_bp: float | None = None,
        fee_observed: bool = False,
        detail: str = "",
    ) -> None:
        attempt = state.attempt
        if attempt is None:
            return
        symbol = state.spec.symbol
        terminal_bid: float | None = None
        terminal_ask: float | None = None
        try:
            terminal_bid = self.touch.best_bid(symbol)
            terminal_ask = self.touch.best_ask(symbol)
        except Exception:  # noqa: BLE001 - terminal quote is evidence, not a dependency
            pass
        metadata = dict(attempt.metadata)
        metadata["would_cross_rejects"] = attempt.would_cross_rejects
        metadata["quote_qty"] = state.spec.quote_qty
        if detail:
            metadata["terminal_detail"] = detail
        record = QuoteAttempt(
            symbol=symbol,
            side=self.config.side,
            attempt_index=attempt.index,
            order_link_ids=tuple(attempt.link_ids),
            decision_ts_ns=attempt.decision_ts_ns,
            decision_bid=attempt.decision_bid,
            decision_ask=attempt.decision_ask,
            placed_prices=tuple(attempt.placed_prices),
            terminal_state=terminal_state,
            terminal_ts_ns=self.clock.now_ns(),
            fill_price=fill_price,
            fill_fee_bp=fill_fee_bp,
            fee_observed=fee_observed,
            terminal_bid=terminal_bid,
            terminal_ask=terminal_ask,
            reprices=attempt.reprices,
            metadata=metadata,
        )
        self.attempts.append(record)
        if self._on_attempt is not None:
            self._on_attempt(record)
        state.attempt = None
        state.working_link = ""
        state.attempts_done += 1
        state.phase = _COOLDOWN
        state.next_action_monotonic = self.clock.monotonic() + self.config.cooldown_seconds

    # ------------------------------------------------------------------- orders

    def _place(self, state: _SymbolState, price: float) -> None:
        attempt = state.attempt
        if attempt is None:
            raise RuntimeError("placement requires an open attempt")
        spec = state.spec
        symbol = spec.symbol
        price_text = spec.price_text(price)
        qty_text = spec.qty_text(spec.quote_qty)
        link = self._new_link(symbol)
        attempt.link_ids.append(link)
        attempt.placed_prices.append(float(price_text))
        self._emit(
            symbol=symbol,
            kind="place_submitted",
            link=link,
            price=float(price_text),
            qty=spec.quote_qty,
        )
        try:
            self.venue.place_post_only(symbol, self.config.side, price_text, qty_text, link)
        except WouldCrossReject as exc:
            self._emit(
                symbol=symbol,
                kind="place_rejected_would_cross",
                link=link,
                price=float(price_text),
                detail=str(exc),
            )
            self._handle_would_cross(state)
            return
        except Exception as exc:
            self._emit(
                symbol=symbol,
                kind="place_rejected_other",
                link=link,
                price=float(price_text),
                detail=f"{type(exc).__name__}: {exc}",
            )
            self._close_attempt(state, TERMINAL_ABORTED, detail=f"place_error: {exc}")
            raise QuoteLabVenueError(f"{symbol}: post-only placement failed: {exc}") from exc
        self._emit(symbol=symbol, kind="place_acked", link=link, price=float(price_text), qty=spec.quote_qty)
        state.working_link = link
        state.working_price = float(price_text)
        state.seen_open = False
        state.partial_recorded = False
        state.placed_monotonic = self.clock.monotonic()
        state.phase = _WORKING

    def _handle_would_cross(self, state: _SymbolState) -> None:
        attempt = state.attempt
        if attempt is None:
            return
        state.working_link = ""
        attempt.would_cross_rejects += 1
        if attempt.would_cross_rejects > MAX_WOULD_CROSS_RETRIES:
            self._close_attempt(state, TERMINAL_REJECTED_WOULD_CROSS)
            return
        state.phase = _WAIT_RETRY
        state.next_action_monotonic = self.clock.monotonic() + WOULD_CROSS_BACKOFF_SECONDS

    def _cancel_working(self, state: _SymbolState, *, kind: str, detail: str) -> Mapping[str, Any] | None:
        """Cancel the working order and resolve its terminal row.

        Returns the order-history row (None when the venue has not surfaced it
        yet). A cancel that races a fill is resolved by the row's cumExecQty;
        an unresolved row is treated as cancelled and any missed fill is caught
        by orphan recovery before the next attempt.
        """
        symbol = state.spec.symbol
        link = state.working_link
        self._emit(symbol=symbol, kind=kind, link=link, price=state.working_price, detail=detail)
        cancel_note = ""
        try:
            self.venue.cancel(symbol, link)
        except Exception as exc:  # noqa: BLE001 - cancel races a fill; the row decides
            cancel_note = f" cancel_error: {str(exc)[:120]}"
        row = self.venue.order_row(symbol, link)
        state.working_link = ""
        if _executed_qty(row) <= 0.0:
            resolved = "" if row is not None else " row_unresolved"
            self._emit(
                symbol=symbol,
                kind="cancelled",
                link=link,
                price=state.working_price,
                detail=(detail + cancel_note + resolved).strip(),
            )
        return row

    def _begin_flatten(self, state: _SymbolState, resolved_row: Mapping[str, Any]) -> None:
        attempt = state.attempt
        symbol = state.spec.symbol
        executed_text = str(resolved_row.get("cumExecQty") or "")
        executed = _executed_qty(resolved_row)
        try:
            fill_price = float(resolved_row.get("avgPrice") or 0.0)
        except (TypeError, ValueError):
            fill_price = 0.0
        if fill_price <= 0.0:
            fill_price = state.working_price
        link = attempt.link_ids[-1] if attempt is not None and attempt.link_ids else state.working_link
        if attempt is not None:
            attempt.metadata["fill_ts_ns"] = self.clock.now_ns()
            attempt.metadata["fill_qty"] = executed
        self._emit(symbol=symbol, kind="filled", link=link, price=fill_price, qty=executed)
        state.fill_price = fill_price
        state.fill_qty_text = executed_text
        state.working_link = ""
        flatten_link = self._new_link(symbol)
        self._emit(
            symbol=symbol,
            kind="flatten_submitted",
            link=flatten_link,
            side=_opposite(self.config.side),
            qty=executed,
        )
        try:
            self.venue.place_reduce_only_market(
                symbol, _opposite(self.config.side), executed_text, flatten_link
            )
        except Exception as exc:  # noqa: BLE001 - the flatten deadline decides
            self._emit(symbol=symbol, kind="error", link=flatten_link, detail=f"flatten_submit_failed: {exc}")
        state.phase = _FLATTENING
        state.flatten_deadline_monotonic = self.clock.monotonic() + FLATTEN_TIMEOUT_SECONDS

    def _flatten_orphan(self, state: _SymbolState, position_row: Mapping[str, Any]) -> None:
        symbol = state.spec.symbol
        size_text = str(position_row.get("size") or "")
        side = str(position_row.get("side") or self.config.side)
        self._emit(symbol=symbol, kind="error", detail=f"orphan_position size={size_text} side={side}")
        flatten_link = self._new_link(symbol)
        self._emit(
            symbol=symbol,
            kind="flatten_submitted",
            link=flatten_link,
            side=_opposite(side),
            qty=_position_size(position_row),
        )
        try:
            self.venue.place_reduce_only_market(symbol, _opposite(side), size_text, flatten_link)
        except Exception as exc:  # noqa: BLE001 - the flatten deadline decides
            self._emit(symbol=symbol, kind="error", link=flatten_link, detail=f"flatten_submit_failed: {exc}")
        state.phase = _FLATTENING
        state.flatten_deadline_monotonic = self.clock.monotonic() + FLATTEN_TIMEOUT_SECONDS

    # -------------------------------------------------------------------- tick

    def tick(self) -> None:
        if not self.active:
            return
        now_monotonic = self.clock.monotonic()
        if self._started_monotonic is None:
            self._started_monotonic = now_monotonic
        if (
            self.config.max_runtime_seconds is not None
            and now_monotonic - self._started_monotonic >= self.config.max_runtime_seconds
        ):
            self.shutdown("max_runtime_elapsed")
            return
        open_rows = self.venue.open_orders()
        open_by_link = {str(row.get("orderLinkId") or ""): row for row in open_rows}
        position_rows = [row for row in self.venue.positions() if _position_size(row) > 0.0]
        positions_by_symbol = {str(row.get("symbol") or "").upper(): row for row in position_rows}
        inventory = sum(_position_notional(row) for row in position_rows)
        if inventory > self.config.max_inventory_notional_usdt:
            self._abort(
                f"inventory_cap_exceeded: {inventory:.2f} > "
                f"{self.config.max_inventory_notional_usdt:.2f} USDT"
            )
            return
        for state in self._states.values():
            self._tick_symbol(state, open_by_link, positions_by_symbol, now_monotonic)
            if not self.active:
                return

    def _tick_symbol(
        self,
        state: _SymbolState,
        open_by_link: Mapping[str, Mapping[str, Any]],
        positions_by_symbol: Mapping[str, Mapping[str, Any]],
        now_monotonic: float,
    ) -> None:
        symbol = state.spec.symbol
        if state.phase == _DONE:
            return
        if state.phase == _PARKED:
            if not self.touch.healthy(symbol):
                return
            state.phase = _IDLE
        if state.phase == _COOLDOWN:
            if now_monotonic < state.next_action_monotonic:
                return
            state.phase = _IDLE
        if state.phase == _FLATTENING:
            self._tick_flattening(state, positions_by_symbol, now_monotonic)
            return
        if state.phase == _WAIT_RETRY:
            self._tick_wait_retry(state, now_monotonic)
            return
        if state.phase == _IDLE:
            self._tick_idle(state, positions_by_symbol, now_monotonic)
            return
        if state.phase == _WORKING:
            self._tick_working(state, open_by_link, now_monotonic)

    def _tick_idle(
        self,
        state: _SymbolState,
        positions_by_symbol: Mapping[str, Mapping[str, Any]],
        now_monotonic: float,
    ) -> None:
        symbol = state.spec.symbol
        position = positions_by_symbol.get(symbol)
        if position is not None:
            self._flatten_orphan(state, position)
            return
        if self.config.max_attempts_per_symbol and state.attempts_done >= self.config.max_attempts_per_symbol:
            state.phase = _DONE
            return
        if now_monotonic < state.next_action_monotonic:
            return
        if not self.touch.healthy(symbol):
            return
        bid = self.touch.best_bid(symbol)
        ask = self.touch.best_ask(symbol)
        if bid is None or ask is None or bid <= 0.0 or ask < bid:
            return
        state.attempt = _Attempt(
            index=state.attempts_done,
            decision_ts_ns=self.clock.now_ns(),
            decision_bid=bid,
            decision_ask=ask,
            started_monotonic=now_monotonic,
        )
        self._place(state, bid if self.config.side == "Buy" else ask)

    def _tick_wait_retry(self, state: _SymbolState, now_monotonic: float) -> None:
        attempt = state.attempt
        symbol = state.spec.symbol
        if attempt is None:
            state.phase = _IDLE
            return
        if now_monotonic - attempt.started_monotonic >= self.config.attempt_timeout_seconds:
            self._close_attempt(state, TERMINAL_REJECTED_WOULD_CROSS, detail="timeout_during_backoff")
            return
        if not self.touch.healthy(symbol):
            self._close_attempt(state, TERMINAL_ABORTED, detail="touch_unhealthy")
            state.phase = _PARKED
            return
        if now_monotonic < state.next_action_monotonic:
            return
        bid = self.touch.best_bid(symbol)
        ask = self.touch.best_ask(symbol)
        if bid is None or ask is None or bid <= 0.0 or ask < bid:
            return
        self._place(state, bid if self.config.side == "Buy" else ask)

    def _tick_working(
        self,
        state: _SymbolState,
        open_by_link: Mapping[str, Mapping[str, Any]],
        now_monotonic: float,
    ) -> None:
        attempt = state.attempt
        symbol = state.spec.symbol
        if attempt is None:
            state.phase = _IDLE
            return
        if not self.touch.healthy(symbol):
            row = self._cancel_working(state, kind="cancel_submitted", detail="touch_unhealthy")
            if _executed_qty(row) > 0.0 and row is not None:
                self._begin_flatten(state, row)
                return
            self._close_attempt(state, TERMINAL_ABORTED, detail="touch_unhealthy")
            state.phase = _PARKED
            return
        row = open_by_link.get(state.working_link)
        if row is None:
            resolved = self.venue.order_row(symbol, state.working_link)
            if resolved is None:
                return  # not yet observable; the attempt timeout bounds the wait
            if _executed_qty(resolved) > 0.0:
                self._begin_flatten(state, resolved)
                return
            status = str(resolved.get("orderStatus") or "")
            if not state.seen_open and status in _GONE_STATUSES:
                # PostOnly that would cross reports Cancelled right after create.
                self._emit(
                    symbol=symbol,
                    kind="place_rejected_would_cross",
                    link=state.working_link,
                    price=state.working_price,
                    detail=f"post_create_status: {status}",
                )
                self._handle_would_cross(state)
                return
            self._emit(
                symbol=symbol,
                kind="cancelled",
                link=state.working_link,
                price=state.working_price,
                detail=f"external_cancel: {status}",
            )
            state.working_link = ""
            self._close_attempt(state, TERMINAL_ABORTED, detail=f"external_cancel: {status}")
            return
        state.seen_open = True
        executed = _executed_qty(row)
        if executed > 0.0:
            if not state.partial_recorded and executed < state.spec.quote_qty:
                state.partial_recorded = True
                self._emit(
                    symbol=symbol,
                    kind="partial_fill",
                    link=state.working_link,
                    price=state.working_price,
                    qty=executed,
                )
            link = state.working_link
            resolved = self._cancel_working(state, kind="cancel_submitted", detail="fill_detected")
            final_row: Mapping[str, Any] = resolved if _executed_qty(resolved) > 0.0 and resolved is not None else row
            state.working_link = link  # _begin_flatten falls back to it for the fill link
            self._begin_flatten(state, final_row)
            return
        if now_monotonic - attempt.started_monotonic >= self.config.attempt_timeout_seconds:
            resolved = self._cancel_working(state, kind="cancel_submitted", detail="attempt_timeout")
            if _executed_qty(resolved) > 0.0 and resolved is not None:
                self._begin_flatten(state, resolved)
                return
            self._close_attempt(state, TERMINAL_TIMEOUT_CANCELLED)
            return
        bid = self.touch.best_bid(symbol)
        ask = self.touch.best_ask(symbol)
        touch_price = bid if self.config.side == "Buy" else ask
        if touch_price is None or touch_price <= 0.0:
            return
        moved_ticks = abs(touch_price - state.working_price) / state.spec.tick_size
        chased = moved_ticks > self.config.chase_ticks + 1e-9
        stale = now_monotonic - state.placed_monotonic >= self.config.reprice_seconds
        if not chased and not stale:
            return
        resolved = self._cancel_working(
            state, kind="reprice_cancel", detail="chase" if chased else "stale"
        )
        if _executed_qty(resolved) > 0.0 and resolved is not None:
            self._begin_flatten(state, resolved)
            return
        attempt.reprices += 1
        self._place(state, touch_price)

    def _tick_flattening(
        self,
        state: _SymbolState,
        positions_by_symbol: Mapping[str, Mapping[str, Any]],
        now_monotonic: float,
    ) -> None:
        symbol = state.spec.symbol
        position = positions_by_symbol.get(symbol)
        if position is None:
            self._emit(symbol=symbol, kind="flattened", qty=0.0)
            attempt = state.attempt
            if attempt is None:
                state.phase = _COOLDOWN
                state.next_action_monotonic = now_monotonic + self.config.cooldown_seconds
                return
            fee_bp: float | None = None
            fee_observed = False
            fill_link = attempt.link_ids[-1] if attempt.link_ids else ""
            if fill_link:
                try:
                    fee_bp, fee_observed = self.venue.fill_fee_bp(symbol, fill_link)
                except Exception as exc:  # noqa: BLE001 - fee is evidence, not a dependency
                    self._emit(symbol=symbol, kind="error", link=fill_link, detail=f"fee_lookup_failed: {exc}")
            self._close_attempt(
                state,
                TERMINAL_FILLED,
                fill_price=state.fill_price,
                fill_fee_bp=fee_bp,
                fee_observed=fee_observed,
            )
            return
        if now_monotonic >= state.flatten_deadline_monotonic:
            self._emit(symbol=symbol, kind="error", detail="flatten_unverified_within_deadline")
            if state.attempt is not None:
                self._close_attempt(
                    state,
                    TERMINAL_FILLED,
                    fill_price=state.fill_price,
                    detail="flatten_unverified",
                )
            self._abort(f"flatten_timeout: {symbol}")

    # ---------------------------------------------------------------- shutdown

    def _abort(self, reason: str) -> None:
        if not self.abort_reason:
            self.abort_reason = reason
        self._emit(symbol="*", kind="error", detail=f"abort: {reason}")
        self.shutdown(reason)

    def shutdown(self, reason: str) -> None:
        """Cancel every working order, flatten every position. Idempotent."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.shutdown_reason = reason
        for state in self._states.values():
            if state.working_link:
                symbol = state.spec.symbol
                link = state.working_link
                self._emit(
                    symbol=symbol,
                    kind="cancel_submitted",
                    link=link,
                    price=state.working_price,
                    detail=f"shutdown: {reason}",
                )
                note = ""
                try:
                    self.venue.cancel(symbol, link)
                except Exception as exc:  # noqa: BLE001 - shutdown must keep going
                    note = f" cancel_error: {str(exc)[:120]}"
                self._emit(
                    symbol=symbol,
                    kind="cancelled",
                    link=link,
                    price=state.working_price,
                    detail=(f"shutdown: {reason}" + note).strip(),
                )
                state.working_link = ""
            if state.attempt is not None:
                self._close_attempt(state, TERMINAL_ABORTED, detail=f"shutdown: {reason}")
            state.phase = _DONE
        self.shutdown_flat = self._flatten_all_positions(reason)

    def _flatten_all_positions(self, reason: str) -> bool:
        try:
            rows = [row for row in self.venue.positions() if _position_size(row) > 0.0]
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            self._emit(symbol="*", kind="error", detail=f"shutdown_positions_read_failed: {exc}")
            return False
        if not rows:
            return True
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "Buy")
            size_text = str(row.get("size") or "")
            link = self._new_link(symbol or "pos")
            self._emit(
                symbol=symbol,
                kind="flatten_submitted",
                link=link,
                side=_opposite(side),
                qty=_position_size(row),
                detail=f"shutdown: {reason}",
            )
            try:
                self.venue.place_reduce_only_market(symbol, _opposite(side), size_text, link)
            except Exception as exc:  # noqa: BLE001 - keep flattening the rest
                self._emit(symbol=symbol, kind="error", link=link, detail=f"flatten_submit_failed: {exc}")
        deadline = self.clock.monotonic() + FLATTEN_TIMEOUT_SECONDS
        while True:
            try:
                remaining = [row for row in self.venue.positions() if _position_size(row) > 0.0]
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                self._emit(symbol="*", kind="error", detail=f"shutdown_positions_read_failed: {exc}")
                return False
            if not remaining:
                self._emit(symbol="*", kind="flattened", detail=f"shutdown: {reason}")
                return True
            if self.clock.monotonic() >= deadline:
                symbols = ",".join(sorted(str(row.get("symbol") or "") for row in remaining))
                self._emit(symbol="*", kind="error", detail=f"shutdown_flatten_unverified: {symbols}")
                return False
            self.clock.sleep(0.5)
