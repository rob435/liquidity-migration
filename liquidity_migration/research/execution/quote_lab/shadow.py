"""Shadow quoting: would a resting post-only limit order have filled, and at
what cost.

Replays a recorded tape and rests virtual orders against it. The tape shows
displayed size but not our true place in line, so each attempt carries two
fill bounds:

- conservative: cancels (others pulling their orders) happen behind us, so
  only trades move us forward, and the queue ahead is clamped to the
  displayed size, which it can never exceed;
- optimistic: cancels happen ahead of us.

A price strictly trading through our level fills both bounds. All timing uses
the tape's local receive timestamps, so a run is deterministic and replayable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from liquidity_migration.research.execution.quote_lab.book import BookMirror

MARKOUT_HORIZONS_S: tuple[float, ...] = (10.0, 30.0, 60.0, 300.0)
MARKOUT_FIELDS: dict[float, str] = {
    10.0: "adverse_markout_bp_10s",
    30.0: "adverse_markout_bp_30s",
    60.0: "adverse_markout_bp_60s",
    300.0: "adverse_markout_bp_300s",
}
TERMINAL_FILLED = "filled"
TERMINAL_TIMEOUT = "timeout"
TERMINAL_CHASE_EXHAUSTED = "chase_exhausted"

_BOOK_KINDS = frozenset({"orderbook_snapshot", "orderbook_delta"})
_EPS = 1e-9
_NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class ShadowPolicy:
    """How one virtual quote behaves.

    ``placement``: "join" rests at the current touch; "improve" rests one tick
    inside the touch when the spread is at least 2 ticks, else joins.
    ``chase_ticks``: cancel and replace at the new touch once the touch moves
    away from our price by more than this many ticks. ``our_qty`` is the
    resting size; 0.0 means the first trade after the queue ahead clears
    counts as our fill.
    """

    side: str
    placement: str
    tick_size: float
    timeout_seconds: float = 120.0
    chase_ticks: int = 2
    max_reprices: int = 4
    our_qty: float = 0.0

    def __post_init__(self) -> None:
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("side must be 'Buy' or 'Sell'")
        if self.placement not in {"join", "improve"}:
            raise ValueError("placement must be 'join' or 'improve'")
        if self.tick_size <= 0.0 or self.timeout_seconds <= 0.0:
            raise ValueError("tick_size and timeout_seconds must be positive")
        if self.chase_ticks < 0 or self.max_reprices < 0 or self.our_qty < 0.0:
            raise ValueError("chase_ticks, max_reprices, and our_qty must not be negative")


@dataclass(slots=True)
class ShadowOutcome:
    """One terminal virtual quote attempt.

    Adverse markout is the mid move against our side after the fill (after
    the terminal time when unfilled) in bp (basis points, 1/100 of a
    percent); positive = the market moved against us. Horizons past the end
    of the tape stay None.
    """

    symbol: str
    side: str
    decision_ts_ns: int
    decision_bid: float
    decision_ask: float
    placed_prices: list[float]
    filled_conservative: bool
    filled_optimistic: bool
    time_to_fill_s_conservative: float | None
    time_to_fill_s_optimistic: float | None
    traded_through: bool
    reprices: int
    terminal_ts_ns: int
    terminal_bid: float | None
    terminal_ask: float | None
    terminal_reason: str
    decision_spread_bp: float
    queue_ahead_at_placement: float
    adverse_markout_bp_10s: float | None = None
    adverse_markout_bp_30s: float | None = None
    adverse_markout_bp_60s: float | None = None
    adverse_markout_bp_300s: float | None = None


def _snap(price: float) -> float:
    """Bring a computed price back onto the canonical float for its decimal."""

    return round(price, 12)


def _mid(mirror: BookMirror, symbol: str) -> float | None:
    bid = mirror.best_bid(symbol)
    ask = mirror.best_ask(symbol)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


@dataclass(slots=True)
class _ModelState:
    queue_ahead: float
    our_filled: float = 0.0
    filled_ts_ns: int | None = None
    fill_mid: float | None = None


@dataclass(slots=True)
class _MarkoutTracker:
    outcome: ShadowOutcome
    anchor_ts_ns: int
    anchor_mid: float
    remaining: dict[float, int]

    def observe(self, ts_ns: int, mid: float) -> None:
        for horizon_s in sorted(self.remaining):
            if ts_ns < self.remaining[horizon_s]:
                continue
            drift_bp = (mid - self.anchor_mid) / self.anchor_mid * 1e4
            adverse_bp = -drift_bp if self.outcome.side == "Buy" else drift_bp
            setattr(self.outcome, MARKOUT_FIELDS[horizon_s], adverse_bp)
            del self.remaining[horizon_s]


class _Attempt:
    """One live virtual order, tracked under both queue bounds."""

    def __init__(
        self,
        *,
        symbol: str,
        policy: ShadowPolicy,
        decision_ts_ns: int,
        decision_bid: float,
        decision_ask: float,
        mirror: BookMirror,
    ) -> None:
        self.symbol = symbol
        self.policy = policy
        self.side = policy.side
        self.tick = policy.tick_size
        self.decision_ts_ns = decision_ts_ns
        self.decision_bid = decision_bid
        self.decision_ask = decision_ask
        self.deadline_ns = decision_ts_ns + int(policy.timeout_seconds * _NS)
        self.traded_through = False
        self.reprices = 0
        self.price = self._placement_price(decision_bid, decision_ask)
        displayed = mirror.depth_at(symbol, self.side, self.price)
        self.queue_ahead_at_placement = displayed
        self.conservative = _ModelState(queue_ahead=displayed)
        self.optimistic = _ModelState(queue_ahead=displayed)
        self.last_displayed = displayed
        self.traded_since_obs = 0.0
        self.placed_prices: list[float] = [self.price]

    def _placement_price(self, bid: float, ask: float) -> float:
        if self.policy.placement == "improve" and (ask - bid) > 1.5 * self.tick:
            return _snap(bid + self.tick if self.side == "Buy" else ask - self.tick)
        return bid if self.side == "Buy" else ask

    def _both_filled(self) -> bool:
        return self.conservative.filled_ts_ns is not None and self.optimistic.filled_ts_ns is not None

    def _fill_unfilled(self, mirror: BookMirror, ts_ns: int) -> None:
        for model in (self.conservative, self.optimistic):
            if model.filled_ts_ns is None:
                model.filled_ts_ns = ts_ns
                model.fill_mid = _mid(mirror, self.symbol)

    def on_trade(self, mirror: BookMirror, price: float, qty: float, aggressor: str, ts_ns: int) -> bool:
        """Feed one public trade; True when the attempt is fully terminal."""

        half = 0.5 * self.tick
        through = price < self.price - half if self.side == "Buy" else price > self.price + half
        if through:
            self.traded_through = True
            self._fill_unfilled(mirror, ts_ns)
            return True
        consuming = aggressor == ("Sell" if self.side == "Buy" else "Buy")
        if consuming and abs(price - self.price) <= half:
            self.traded_since_obs += qty
            for model in (self.conservative, self.optimistic):
                if model.filled_ts_ns is not None:
                    continue
                take = min(qty, model.queue_ahead)
                model.queue_ahead -= take
                model.our_filled += qty - take
                if model.queue_ahead <= _EPS and (
                    self.policy.our_qty <= _EPS or model.our_filled >= self.policy.our_qty - _EPS
                ):
                    model.filled_ts_ns = ts_ns
                    model.fill_mid = _mid(mirror, self.symbol)
        return self._both_filled()

    def on_book(self, mirror: BookMirror, ts_ns: int) -> str | None:
        """React to a book change; a terminal reason ends the attempt.

        While the mirror is unhealthy (gap or crossed book) the displayed
        size is unreliable, so cancel inference and chasing pause; trades
        keep counting through ``on_trade``.
        """

        if not mirror.healthy(self.symbol):
            return None
        displayed = mirror.depth_at(self.symbol, self.side, self.price)
        # Size the level lost beyond what trades explain was cancelled.
        cancelled = max(0.0, (self.last_displayed - displayed) - self.traded_since_obs)
        for model, cancels_ahead in ((self.conservative, False), (self.optimistic, True)):
            if model.filled_ts_ns is None:
                if cancels_ahead:
                    model.queue_ahead = max(0.0, model.queue_ahead - cancelled)
                model.queue_ahead = min(model.queue_ahead, displayed)
        self.last_displayed = displayed
        self.traded_since_obs = 0.0
        bid = mirror.best_bid(self.symbol)
        ask = mirror.best_ask(self.symbol)
        half = 0.5 * self.tick
        moved_through = (
            ask is not None and ask < self.price + half
            if self.side == "Buy"
            else bid is not None and bid > self.price - half
        )
        if moved_through:
            # The market cannot pass a resting order without trading with it.
            self.traded_through = True
            self._fill_unfilled(mirror, ts_ns)
            return TERMINAL_FILLED
        touch = bid if self.side == "Buy" else ask
        if touch is None:
            return None
        away = touch - self.price if self.side == "Buy" else self.price - touch
        if away > (self.policy.chase_ticks + 0.5) * self.tick:
            if self.reprices >= self.policy.max_reprices:
                return TERMINAL_CHASE_EXHAUSTED
            self._reprice(mirror, touch)
        return None

    def _reprice(self, mirror: BookMirror, touch: float) -> None:
        self.price = _snap(touch)
        displayed = mirror.depth_at(self.symbol, self.side, self.price)
        for model in (self.conservative, self.optimistic):
            if model.filled_ts_ns is None:
                model.queue_ahead = displayed
        self.last_displayed = displayed
        self.traded_since_obs = 0.0
        self.placed_prices.append(self.price)
        self.reprices += 1

    def finalize(
        self,
        mirror: BookMirror,
        terminal_ts_ns: int,
        reason: str,
    ) -> tuple[ShadowOutcome, _MarkoutTracker | None]:
        decision_mid = (self.decision_bid + self.decision_ask) / 2.0
        cons, opt = self.conservative, self.optimistic
        outcome = ShadowOutcome(
            symbol=self.symbol,
            side=self.side,
            decision_ts_ns=self.decision_ts_ns,
            decision_bid=self.decision_bid,
            decision_ask=self.decision_ask,
            placed_prices=list(self.placed_prices),
            filled_conservative=cons.filled_ts_ns is not None,
            filled_optimistic=opt.filled_ts_ns is not None,
            time_to_fill_s_conservative=(
                (cons.filled_ts_ns - self.decision_ts_ns) / _NS if cons.filled_ts_ns is not None else None
            ),
            time_to_fill_s_optimistic=(
                (opt.filled_ts_ns - self.decision_ts_ns) / _NS if opt.filled_ts_ns is not None else None
            ),
            traded_through=self.traded_through,
            reprices=self.reprices,
            terminal_ts_ns=terminal_ts_ns,
            terminal_bid=mirror.best_bid(self.symbol),
            terminal_ask=mirror.best_ask(self.symbol),
            terminal_reason=reason,
            decision_spread_bp=(self.decision_ask - self.decision_bid) / decision_mid * 1e4,
            queue_ahead_at_placement=self.queue_ahead_at_placement,
        )
        # Markouts anchor at the most trusted fill time, else the terminal time.
        if cons.filled_ts_ns is not None:
            anchor_ts_ns, anchor_mid = cons.filled_ts_ns, cons.fill_mid
        elif opt.filled_ts_ns is not None:
            anchor_ts_ns, anchor_mid = opt.filled_ts_ns, opt.fill_mid
        else:
            anchor_ts_ns, anchor_mid = terminal_ts_ns, _mid(mirror, self.symbol)
        if anchor_mid is None:
            return outcome, None
        remaining = {horizon_s: anchor_ts_ns + int(horizon_s * _NS) for horizon_s in MARKOUT_HORIZONS_S}
        return outcome, _MarkoutTracker(
            outcome=outcome,
            anchor_ts_ns=anchor_ts_ns,
            anchor_mid=anchor_mid,
            remaining=remaining,
        )


def run_shadow_attempts(
    records: Iterable[Mapping[str, Any]],
    policy: ShadowPolicy,
    attempt_interval_seconds: float,
    tick_size: float | None = None,
    *,
    sides: Sequence[str] | None = None,
) -> list[ShadowOutcome]:
    """Walk the tape once, resting one virtual quote per side at a time.

    A new attempt starts once the prior one on that side is terminal and
    ``attempt_interval_seconds`` has passed since the prior decision, at the
    first record with a healthy two-sided book. ``tick_size`` overrides the
    policy's when given; ``sides`` runs several sides interleaved in one pass
    (default: just the policy's side). Attempts still open when the tape ends
    are dropped — their fate is unknown, and counting them as unfilled would
    slant the fill rate. Meant for a single-symbol tape; records for several
    symbols get independent attempt streams but share one tick size.
    """

    if attempt_interval_seconds <= 0.0:
        raise ValueError("attempt_interval_seconds must be positive")
    tick = float(tick_size) if tick_size is not None else policy.tick_size
    side_policies = {
        side: replace(policy, side=side, tick_size=tick)
        for side in (tuple(sides) if sides is not None else (policy.side,))
    }
    interval_ns = int(attempt_interval_seconds * _NS)
    mirror = BookMirror()
    live: dict[tuple[str, str], _Attempt] = {}
    next_allowed: dict[tuple[str, str], int] = {}
    outcomes: list[ShadowOutcome] = []
    trackers: list[_MarkoutTracker] = []

    def finish(key: tuple[str, str], attempt: _Attempt, terminal_ts_ns: int, reason: str) -> None:
        outcome, tracker = attempt.finalize(mirror, terminal_ts_ns, reason)
        outcomes.append(outcome)
        if tracker is not None:
            trackers.append(tracker)
        next_allowed[key] = max(attempt.decision_ts_ns + interval_ns, terminal_ts_ns)
        del live[key]

    for record in records:
        kind = str(record.get("kind") or "")
        if kind not in _BOOK_KINDS and kind != "public_trade":
            continue
        symbol = str(record.get("symbol") or "").upper()
        ts = int(record.get("local_receive_ts_ns") or 0)
        if not symbol or ts <= 0:
            continue

        for key in list(live):
            attempt = live[key]
            if ts > attempt.deadline_ns:
                finish(key, attempt, attempt.deadline_ns, TERMINAL_TIMEOUT)

        mirror.apply(record)

        if kind == "public_trade":
            price = float(record.get("price") or 0.0)
            qty = float(record.get("qty") or 0.0)
            aggressor = str(record.get("side") or "")
            if price > 0.0 and qty > 0.0 and aggressor in {"Buy", "Sell"}:
                for side in side_policies:
                    key = (symbol, side)
                    attempt_or_none = live.get(key)
                    if attempt_or_none is not None and attempt_or_none.on_trade(mirror, price, qty, aggressor, ts):
                        finish(key, attempt_or_none, ts, TERMINAL_FILLED)
        else:
            for side in side_policies:
                key = (symbol, side)
                attempt_or_none = live.get(key)
                if attempt_or_none is None:
                    continue
                verdict = attempt_or_none.on_book(mirror, ts)
                if verdict is not None:
                    finish(key, attempt_or_none, ts, verdict)

        if trackers:
            mid = _mid(mirror, symbol) if mirror.healthy(symbol) else None
            if mid is not None:
                open_trackers: list[_MarkoutTracker] = []
                for tracker in trackers:
                    if tracker.outcome.symbol == symbol:
                        tracker.observe(ts, mid)
                    if tracker.remaining:
                        open_trackers.append(tracker)
                trackers = open_trackers

        for side, side_policy in side_policies.items():
            key = (symbol, side)
            if key in live or ts < next_allowed.get(key, 0):
                continue
            if not mirror.healthy(symbol):
                continue
            bid = mirror.best_bid(symbol)
            ask = mirror.best_ask(symbol)
            if bid is None or ask is None:
                continue
            live[key] = _Attempt(
                symbol=symbol,
                policy=side_policy,
                decision_ts_ns=ts,
                decision_bid=bid,
                decision_ask=ask,
                mirror=mirror,
            )

    return sorted(outcomes, key=lambda outcome: (outcome.decision_ts_ns, outcome.symbol, outcome.side))
