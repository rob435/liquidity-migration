"""Rest entry orders at the touch instead of crossing the spread.

The execution adapter places an exposure-increasing command as a GTC limit at
the near touch (its price never worse than the market order it replaces would
have started from). This manager then advances every resting entry on the
account owner's normal loop cadence, so nothing ever blocks the owner:

- placement reads the displayed touch sizes: when the book leans toward the
  entry, rest one tick inside the spread (the market is about to leave);
  when it leans hard against, rest one tick behind the touch (joining a
  touch that is about to trade buys the adverse fill); otherwise join;
- while resting, chase only toward the market (a touch that fell away left
  our order alone at the front — the best place to be), improve when the
  book starts leaning our way, and escalate with the clock: past half the
  window never rest behind the touch, near the end improve into the spread;
- when the mid has already run against the entry by more than twice the
  half-spread-plus-taker-fee, cross immediately — the market has proven the
  wait more expensive than the spread;
- when the window closes, amend the price through the far touch so the
  remainder fills as a taker at a bounded price;
- if the cross cannot clear the remainder inside ``cross_grace_seconds``,
  cancel it — the owner's convergence machinery re-plans the shortfall;
- when the order has filled, run the attached-stop verification that market
  entries run at the create boundary.

Exits (reduce-only commands) never rest; they stay market orders.

Change point 2026-08-04 (quote-forge lab): the lean/urgency/drift recipe
replaced the 15 s staleness reprice after a 34-symbol full-night replay of
recorded books (199,785 paired attempts, queue-honest fill bounds) measured
it 0.36 bp/entry cheaper than the join-and-reprice control (t = -11) with
deadline crosses halved, and a cadence check showed 89% of that edge
survives a 3 s evaluation loop — this loop. The original 2026-08-03
overnight-lab numbers for the control (70.4% fill, median 41.6 s, 1.9 bp
vs the 7.78 bp taker basis) understate GTC reality: that lab ran PostOnly
into the demo realm's phantom internal liquidity.

Restart safety: quotes live in memory, but every pass re-scans the kernel's
working entry commands and adopts any resting venue limit it is not tracking,
rebuilding the deadline from the venue row's own creation time. A dead manager
therefore converges to the same terminal states after a restart.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Mapping, Protocol

from liquidity_migration.core.deterministic_runtime import Clock
from liquidity_migration.marketdata.bybit_errors import BybitRequestRejected

_logger = logging.getLogger(__name__)

# Quoting only pays when there is spread to save: below two ticks or ~1 bp the
# taker cost is already near the maker floor and the overnight lab showed
# tight-spread books losing to adverse selection.
MIN_SPREAD_TICKS = 2.0
MIN_SPREAD_FRACTION = 0.0001


class EntryStopVerifier(Protocol):
    def __call__(self, *, symbol: str, expected_stop_price: float, command_id: str) -> str: ...


class TouchSource(Protocol):
    """Current touch with displayed sizes: (bid, ask, bid_qty, ask_qty),
    or None while the book is unhealthy or one-sided."""

    def __call__(self, symbol: str) -> tuple[float, float, float, float] | None: ...


@dataclass(frozen=True, slots=True)
class EntryQuoteConfig:
    """Knobs for the resting-entry lifecycle. ``window_seconds <= 0`` disables
    quoting entirely and every entry goes back to a market order.

    ``clip_touch_fraction`` caps how much of a large entry rests in one
    window: at most that fraction of the quantity already displayed at the
    touch (measured 2026-08-04: the whole touch on the thin half of the
    universe is 23-181 USDT, so a large order resting whole would BE the
    market). The uncapped remainder is not lost — the command terminates at
    the window end and convergence re-plans it, so a big entry arrives as a
    sequence of touch-sized windows. ``0`` disables capping.
    ``min_clip_notional_usdt`` floors the clip so a near-empty touch cannot
    stretch one entry into hundreds of windows."""

    window_seconds: float = 120.0
    reprice_seconds: float = 3.0
    cross_grace_seconds: float = 20.0
    max_amends: int = 8
    clip_touch_fraction: float = 1.0
    min_clip_notional_usdt: float = 100.0
    # Book-lean placement (displayed touch share minus one half, from the
    # entry's chair). ``improve_lean``: at or above this, rest one tick
    # inside the spread. ``back_lean``: at or above this magnitude against
    # us, rest one tick behind the touch. Either 0 disables that behavior.
    improve_lean: float = 0.15
    back_lean: float = 0.15
    # Urgency: past ``urgency_join_frac`` of the window never rest behind
    # the touch; past ``urgency_improve_frac`` improve when the spread allows.
    urgency_join_frac: float = 0.5
    urgency_improve_frac: float = 0.85
    # Cross early once the mid has run against the entry by more than
    # 2 x (half-spread + this fee); 0 disables the early cross. Measured on
    # the quote-forge night replay: making this trigger MORE sensitive was
    # the only clearly harmful dial.
    drift_cross_fee_bp: float = 5.5

    def __post_init__(self) -> None:
        if not math.isfinite(self.window_seconds):
            raise ValueError("entry quote window_seconds must be finite")
        if self.window_seconds > 0.0 and (
            self.reprice_seconds <= 0.0
            or self.cross_grace_seconds <= 0.0
            or self.max_amends < 1
        ):
            raise ValueError("entry quote reprice/grace/amend settings must be positive")
        if not math.isfinite(self.clip_touch_fraction) or self.clip_touch_fraction < 0.0:
            raise ValueError("entry clip_touch_fraction must be finite and non-negative")
        if not math.isfinite(self.min_clip_notional_usdt) or self.min_clip_notional_usdt < 0.0:
            raise ValueError("entry min_clip_notional_usdt must be finite and non-negative")
        for name in ("improve_lean", "back_lean", "drift_cross_fee_bp"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"entry quote {name} must be finite and non-negative")
        for name in ("urgency_join_frac", "urgency_improve_frac"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"entry quote {name} must be inside [0, 1]")

    @property
    def enabled(self) -> bool:
        return self.window_seconds > 0.0


@dataclass(slots=True)
class _QuoteState:
    command_id: str
    symbol: str
    is_buy: bool
    price: float
    deadline_ns: int
    last_reprice_ns: int
    window_start_ns: int = 0
    decision_mid: float = 0.0
    amend_count: int = 0
    crossed: bool = False
    cross_deadline_ns: int = 0
    cancel_requested: bool = False
    verified: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def snap_price_text(price: float, tick_size: float, *, round_up: bool) -> str:
    """Snap to the instrument grid, rendering exactly like the venue expects."""

    if tick_size <= 0.0 or price <= 0.0:
        raise ValueError("price snapping requires positive price and tick size")
    tick = Decimal(str(tick_size))
    snapped = (Decimal(str(price)) / tick).to_integral_value(
        rounding=ROUND_UP if round_up else ROUND_DOWN
    ) * tick
    return format(snapped.normalize(), "f")


class EntryQuoteManager:
    """Owns the lifecycle of resting entry quotes for one account owner."""

    def __init__(
        self,
        client: Any,
        *,
        config: EntryQuoteConfig,
        instrument_rules: Mapping[str, Any],
        kernel: Any,
        clock: Clock,
        entry_stop_verifier: EntryStopVerifier | None = None,
        touch_source: TouchSource | None = None,
    ) -> None:
        self.client = client
        self.config = config
        # Live mapping shared with the runner; symbols appear as rules refresh.
        self.instrument_rules = instrument_rules
        self.kernel = kernel
        self.clock = clock
        self.entry_stop_verifier = entry_stop_verifier
        # The owner's own reconstructed book (free, carries displayed sizes);
        # REST tickers remain the fallback and carry no sizes.
        self.touch_source = touch_source
        self._quotes: dict[str, _QuoteState] = {}
        # Working entry commands probed once and found to be non-resting
        # (a market order in flight, or a venue row not yet visible).
        self._adoption_probe_ns: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Placement-side helpers used by the execution adapter.

    def tick_size(self, symbol: str) -> float:
        rule = self.instrument_rules.get(symbol.upper())
        tick = float(getattr(rule, "tick_size", 0.0) or 0.0)
        return tick if math.isfinite(tick) and tick > 0.0 else 0.0

    @staticmethod
    def _lean(is_buy: bool, bid_qty: float | None, ask_qty: float | None) -> float | None:
        """Displayed touch share from the entry's chair: positive = the book
        leans toward the entry filling late (price about to move away),
        negative = the touch is about to trade into us. None without sizes."""

        if bid_qty is None or ask_qty is None:
            return None
        try:
            bid_size, ask_size = float(bid_qty), float(ask_qty)
        except (TypeError, ValueError):
            return None
        total = bid_size + ask_size
        if not math.isfinite(total) or total <= 0.0 or bid_size < 0.0 or ask_size < 0.0:
            return None
        share = bid_size / total
        return share - 0.5 if is_buy else 0.5 - share

    def plan_entry_quote(
        self,
        *,
        symbol: str,
        is_buy: bool,
        bid: float | None,
        ask: float | None,
        bid_qty: float | None = None,
        ask_qty: float | None = None,
    ) -> str | None:
        """The resting limit price, or None when quoting cannot help here.

        Every None falls back to the exact market order the fleet sent before
        this module existed. Placement is by book lean when the displayed
        touch sizes are known: improve into the spread when the book leans
        toward the entry, rest one tick behind the touch when it leans hard
        against, join the touch otherwise (and always, without sizes).
        """

        if not self.config.enabled:
            return None
        tick = self.tick_size(symbol)
        if tick <= 0.0:
            return None
        if bid is None or ask is None:
            bid, ask, bid_qty, ask_qty = self._touch(symbol)
            if bid is None or ask is None:
                return None
        if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0.0 or ask <= bid:
            return None
        spread = ask - bid
        mid = (ask + bid) / 2.0
        if spread < MIN_SPREAD_TICKS * tick or spread / mid < MIN_SPREAD_FRACTION:
            return None
        lean = self._lean(is_buy, bid_qty, ask_qty)
        if lean is not None and self.config.improve_lean > 0.0 and lean >= self.config.improve_lean:
            # The spread gate above guarantees one tick inside stays a maker.
            improved = bid + tick if is_buy else ask - tick
            return snap_price_text(improved, tick, round_up=not is_buy)
        if lean is not None and self.config.back_lean > 0.0 and lean <= -self.config.back_lean:
            behind = bid - tick if is_buy else ask + tick
            if behind > 0.0:
                return snap_price_text(behind, tick, round_up=not is_buy)
        # Buy rests at the bid, sell at the ask; snapping toward the inside of
        # the book can never produce a price that crosses.
        return snap_price_text(bid if is_buy else ask, tick, round_up=not is_buy)

    def plan_entry_clip(
        self,
        *,
        symbol: str,
        is_buy: bool,
        command_qty: float,
        price: float,
        bid_qty: float | None,
        ask_qty: float | None,
    ) -> str | None:
        """The quantity to rest this window, or None to place the full command.

        A resting buy joins the bid queue, so the cap reads the bid size (the
        ask for a sell). Any missing or degenerate input means no cap — the
        full commanded quantity rests, exactly as before this method existed.
        """

        fraction = self.config.clip_touch_fraction
        if fraction <= 0.0 or not math.isfinite(command_qty) or command_qty <= 0.0:
            return None
        if not math.isfinite(price) or price <= 0.0:
            return None
        touch_qty = bid_qty if is_buy else ask_qty
        if touch_qty is None or not math.isfinite(touch_qty) or touch_qty <= 0.0:
            return None
        rule = self.instrument_rules.get(symbol.upper())
        qty_step = float(getattr(rule, "qty_step", 0.0) or 0.0)
        min_qty = float(getattr(rule, "min_qty", 0.0) or 0.0)
        min_notional = float(getattr(rule, "min_notional", 0.0) or 0.0)
        if qty_step <= 0.0 or not math.isfinite(qty_step):
            return None
        clip = max(fraction * touch_qty, self.config.min_clip_notional_usdt / price)
        step = Decimal(str(qty_step))
        clip_dec = (Decimal(str(clip)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        # The clip itself must be a viable venue order.
        min_viable = max(min_qty, min_notional / price if min_notional > 0.0 else 0.0)
        min_viable_dec = (
            (Decimal(str(min_viable)) / step).to_integral_value(rounding=ROUND_UP) * step
            if min_viable > 0.0
            else step
        )
        if clip_dec < min_viable_dec:
            clip_dec = min_viable_dec
        command_dec = Decimal(str(command_qty))
        # No cap when it would not shrink the order, and no cap when the
        # remainder it leaves behind could never be re-ordered (venue-minimum
        # dust): a slightly oversized final window beats a stranded tail.
        if clip_dec >= command_dec or command_dec - clip_dec < min_viable_dec:
            return None
        return format(clip_dec.normalize(), "f")

    def register(
        self,
        *,
        command_id: str,
        symbol: str,
        is_buy: bool,
        price: float,
        decision_mid: float | None = None,
    ) -> None:
        now_ns = self.clock.wall_time_ns()
        mid = float(decision_mid or 0.0)
        self._quotes[command_id] = _QuoteState(
            command_id=command_id,
            symbol=symbol.upper(),
            is_buy=is_buy,
            price=price,
            deadline_ns=now_ns + int(self.config.window_seconds * 1e9),
            last_reprice_ns=now_ns,
            window_start_ns=now_ns,
            decision_mid=mid if math.isfinite(mid) and mid > 0.0 else 0.0,
        )

    # ------------------------------------------------------------------
    # Health-side probe used by the convergence report.

    def symbol_has_active_quote(self, symbol: str) -> bool:
        """True while a resting quote for this symbol is inside its window
        (plus the cross grace), which is the whole period the convergence
        report should read the working order as intentional."""

        now_ns = self.clock.wall_time_ns()
        grace_ns = int(self.config.cross_grace_seconds * 1e9)
        symbol = symbol.upper()
        return any(
            quote.symbol == symbol and now_ns <= quote.deadline_ns + 2 * grace_ns
            for quote in self._quotes.values()
        )

    # ------------------------------------------------------------------
    # The per-pass lifecycle driver.

    def advance(self) -> None:
        """One pass: adopt, verify fills, reprice, cross, cancel. Never raises;
        a persistently failing pass leaves quotes past their window, which the
        convergence health bound then surfaces on its own."""

        if not self.config.enabled:
            return
        try:
            self._adopt_untracked()
        except Exception:  # noqa: BLE001 - adoption is retried next pass
            _logger.exception("resting entry quote adoption failed")
        for command_id in list(self._quotes):
            try:
                self._advance_one(command_id)
            except Exception:  # noqa: BLE001 - one bad quote must not stall the rest
                _logger.exception(
                    "resting entry quote pass failed for command %s", command_id
                )

    # ------------------------------------------------------------------

    def _order_state(self, command_id: str) -> Any | None:
        return self.kernel._state_ref().orders.get(command_id)

    def _touch(
        self, symbol: str
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """(bid, ask, bid_qty, ask_qty). The owner's own reconstructed book
        first (free, carries sizes); REST tickers as the fallback (no sizes)."""

        if self.touch_source is not None:
            try:
                touch = self.touch_source(symbol)
            except Exception:  # noqa: BLE001 - the REST fallback still answers
                touch = None
            if touch is not None:
                bid, ask, bid_qty, ask_qty = touch
                if (
                    math.isfinite(bid)
                    and math.isfinite(ask)
                    and bid > 0.0
                    and ask > bid
                ):
                    return bid, ask, bid_qty, ask_qty
        try:
            rows = self.client.get_tickers(symbol=symbol)
        except Exception:  # noqa: BLE001 - a failed read just skips this reprice
            return None, None, None, None
        for row in rows or []:
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            try:
                bid = float(row.get("bid1Price") or 0.0)
                ask = float(row.get("ask1Price") or 0.0)
            except (TypeError, ValueError):
                return None, None, None, None
            if bid > 0.0 and ask > bid:
                return bid, ask, None, None
            return None, None, None, None
        return None, None, None, None

    def _adopt_untracked(self) -> None:
        """Track any working entry command whose resting limit this process
        does not know — the restart path."""

        state = self.kernel._state_ref()
        now_ns = self.clock.wall_time_ns()
        # The maintained working-order index, never the full order history:
        # this runs on every owner loop pass.
        for command_id in list(state.working_order_ids):
            order = state.orders.get(command_id)
            if (
                order is None
                or command_id in self._quotes
                or order.reduce_only
                or order.status not in {"acknowledged", "partially_filled"}
            ):
                continue
            last_probe = self._adoption_probe_ns.get(command_id, 0)
            if now_ns - last_probe < 10_000_000_000:
                continue
            self._adoption_probe_ns[command_id] = now_ns
            try:
                rows = self.client.get_open_orders(
                    symbol=order.symbol, order_link_id=command_id
                )
            except Exception:  # noqa: BLE001 - probe again next window
                continue
            row = next(
                (
                    r
                    for r in rows or []
                    if str(r.get("orderLinkId") or "") == command_id
                    and str(r.get("orderType") or "") == "Limit"
                ),
                None,
            )
            if row is None:
                continue
            try:
                price = float(row.get("price") or 0.0)
                created_ms = int(float(row.get("createdTime") or 0))
            except (TypeError, ValueError):
                continue
            if price <= 0.0:
                continue
            created_ns = created_ms * 1_000_000 if created_ms > 0 else now_ns
            self._quotes[command_id] = _QuoteState(
                command_id=command_id,
                symbol=order.symbol.upper(),
                is_buy=order.signed_qty > 0.0,
                price=price,
                deadline_ns=created_ns + int(self.config.window_seconds * 1e9),
                last_reprice_ns=now_ns,
                window_start_ns=created_ns,
                # No decision mid survives the restart; the drift-cross stays
                # off for adopted quotes and the window-end cross bounds them.
                metadata={"adopted_after_restart": True},
            )
            _logger.warning(
                "adopted an untracked resting entry quote after restart: %s %s @ %s",
                order.symbol,
                command_id,
                price,
            )
        if len(self._adoption_probe_ns) > 10_000:
            self._adoption_probe_ns.clear()

    def _advance_one(self, command_id: str) -> None:
        quote = self._quotes[command_id]
        order = self._order_state(command_id)
        if order is None:
            self._quotes.pop(command_id, None)
            return
        now_ns = self.clock.wall_time_ns()
        if order.status in {"filled", "cancelled", "rejected", "partially_filled_cancelled"}:
            self._verify_if_filled(quote, order)
            # Retain the terminal state until the probe horizon: a sliced
            # entry's next window starts a couple of owner ticks after this
            # one ends, and dropping the state here would flicker the
            # convergence exemption (and its paging) through that gap.
            if now_ns > quote.deadline_ns + 2 * int(self.config.cross_grace_seconds * 1e9):
                self._quotes.pop(command_id, None)
            return
        if quote.crossed:
            if quote.cancel_requested:
                return
            if now_ns >= quote.cross_deadline_ns:
                # The bounded cross could not clear the remainder: take the
                # order down and let convergence re-plan the shortfall.
                quote.cancel_requested = True
                try:
                    self.client.cancel_order(
                        symbol=quote.symbol, order_link_id=command_id
                    )
                except Exception:  # noqa: BLE001 - a fill/cancel race is the common cause
                    _logger.warning(
                        "cancel of an uncleared crossed entry quote was rejected "
                        "(usually a fill race): %s %s",
                        quote.symbol,
                        command_id,
                    )
            return
        if now_ns >= quote.deadline_ns:
            self._cross(quote, now_ns)
            return
        if now_ns - quote.last_reprice_ns < int(self.config.reprice_seconds * 1e9):
            return
        quote.last_reprice_ns = now_ns
        tick = self.tick_size(quote.symbol)
        if tick <= 0.0:
            return
        bid, ask, bid_qty, ask_qty = self._touch(quote.symbol)
        if bid is None or ask is None:
            return
        # The market has already run past the price of crossing: stop paying
        # for patience and take the far touch now, bounded exactly like the
        # window-end cross.
        if self.config.drift_cross_fee_bp > 0.0 and quote.decision_mid > 0.0:
            mid = (bid + ask) / 2.0
            drift_bp = (mid - quote.decision_mid) / quote.decision_mid * 1e4
            against_bp = drift_bp if quote.is_buy else -drift_bp
            half_spread_bp = (ask - bid) / 2.0 / mid * 1e4
            if against_bp >= 2.0 * (half_spread_bp + self.config.drift_cross_fee_bp):
                self._cross(quote, now_ns)
                return
        if quote.amend_count >= self.config.max_amends:
            return
        new_price = self._desired_price(quote, now_ns, tick, bid, ask, bid_qty, ask_qty)
        if new_price is None or abs(float(new_price) - quote.price) < tick * 0.5:
            return
        if self._amend(quote, new_price):
            quote.price = float(new_price)
            quote.amend_count += 1

    def _desired_price(
        self,
        quote: _QuoteState,
        now_ns: int,
        tick: float,
        bid: float,
        ask: float,
        bid_qty: float | None,
        ask_qty: float | None,
    ) -> str | None:
        """Where this quote should rest right now, or None to hold.

        A touch that fell away left our order alone at the front of the book
        — the best place to be, so never chase toward a retreating market.
        Everything else escalates: rejoin when overtaken, jump the queue when
        the book leans our way and there is spread to jump into, never rest
        behind the touch past half the window, improve outright near the end.
        """

        touch = bid if quote.is_buy else ask
        can_improve = (ask - bid) >= MIN_SPREAD_TICKS * tick
        improved = bid + tick if quote.is_buy else ask - tick
        window_ns = int(self.config.window_seconds * 1e9)
        start_ns = quote.window_start_ns or (quote.deadline_ns - window_ns)
        elapsed = (
            min(1.0, max(0.0, (now_ns - start_ns) / window_ns)) if window_ns > 0 else 1.0
        )
        lean = self._lean(quote.is_buy, bid_qty, ask_qty)
        half = tick * 0.5
        ahead = quote.price > touch + half if quote.is_buy else quote.price < touch - half
        behind = quote.price < touch - half if quote.is_buy else quote.price > touch + half

        def text(price: float) -> str:
            return snap_price_text(price, tick, round_up=not quote.is_buy)

        wants_improve = (
            lean is not None
            and self.config.improve_lean > 0.0
            and lean >= self.config.improve_lean
            and can_improve
        )
        if elapsed >= self.config.urgency_improve_frac and can_improve and not ahead:
            return text(improved)
        if behind:
            stay_back = (
                lean is not None
                and self.config.back_lean > 0.0
                and lean <= -self.config.back_lean
                and elapsed < self.config.urgency_join_frac
            )
            if stay_back:
                return None
            return text(improved) if wants_improve else text(touch)
        if not ahead and wants_improve:
            return text(improved)
        return None

    def _cross(self, quote: _QuoteState, now_ns: int) -> None:
        """Amend through the far touch: the remainder fills as a taker at a
        price bounded by the far touch, unlike the unbounded market order."""

        tick = self.tick_size(quote.symbol)
        bid, ask, _bid_qty, _ask_qty = self._touch(quote.symbol)
        far = ask if quote.is_buy else bid
        if tick <= 0.0 or far is None:
            # Cannot price a bounded cross right now; retry next pass until
            # the cross grace runs out, then the cancel path takes over.
            if quote.cross_deadline_ns == 0:
                quote.cross_deadline_ns = now_ns + int(self.config.cross_grace_seconds * 1e9)
                quote.crossed = True
            return
        pad = max(tick * MIN_SPREAD_TICKS, far * MIN_SPREAD_FRACTION)
        crossing = far + pad if quote.is_buy else max(far - pad, tick)
        price_text = snap_price_text(crossing, tick, round_up=quote.is_buy)
        if self._amend(quote, price_text):
            quote.price = float(price_text)
        quote.crossed = True
        quote.cross_deadline_ns = now_ns + int(self.config.cross_grace_seconds * 1e9)

    def _amend(self, quote: _QuoteState, price_text: str) -> bool:
        try:
            self.client.amend_order(
                symbol=quote.symbol,
                order_link_id=quote.command_id,
                price=price_text,
            )
        except BybitRequestRejected:
            # The usual cause is a fill or cancel that beat the amend; the
            # kernel's stream state decides on the next pass.
            return False
        except Exception:  # noqa: BLE001 - transport faults retry next pass
            _logger.exception(
                "amend of resting entry quote failed: %s %s", quote.symbol, quote.command_id
            )
            return False
        return True

    def _verify_if_filled(self, quote: _QuoteState, order: Any) -> None:
        if quote.verified or self.entry_stop_verifier is None:
            return
        filled = abs(float(order.filled_signed_qty or 0.0))
        stop_price = order.entry_stop_price
        if filled <= 0.0 or stop_price is None:
            return
        quote.verified = True
        try:
            outcome = self.entry_stop_verifier(
                symbol=quote.symbol,
                expected_stop_price=float(stop_price),
                command_id=quote.command_id,
            )
        except Exception:  # noqa: BLE001 - reconciliation owns the fallback proof
            _logger.exception(
                "deferred entry stop verification failed: %s %s",
                quote.symbol,
                quote.command_id,
            )
            return
        if outcome not in {"armed", "repaired"}:
            _logger.warning(
                "deferred entry stop verification returned %s for %s %s",
                outcome,
                quote.symbol,
                quote.command_id,
            )
