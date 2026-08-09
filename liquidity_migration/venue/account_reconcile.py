"""REST recovery and venue-truth reconciliation for the account kernel."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from liquidity_migration.venue.account_execution_stream import BybitAccountExecutionConsumer
from liquidity_migration.venue.account_service_bybit import inspect_bybit_order_ownership, require_named_realm
from liquidity_migration.venue.wedged_command_resolution import (
    EXTERNAL_BATCH_PREFIXES,
    WedgedCommandResolutionRefused,
    probe_wedged_command,
    terminalize_wedged_command,
    venue_identified_order_id,
)
from liquidity_migration.account.account_contracts import (
    AccountEvent,
    AccountEventType,
    InstrumentRules,
)
from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.account.wedged_command_watch import (
    WEDGE_AMBIGUOUS_SUBMISSION,
    WEDGE_STALLED_WORKING_ORDER,
    stalled_working_orders,
    wedged_commands,
)
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock
from liquidity_migration.core.venue_realm import VenueRealm

_logger = logging.getLogger(__name__)

NATIVE_ACTIVATION_CLOCK_TOLERANCE_NS = 5_000_000_000
BYBIT_ACCOUNTING_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
DEFAULT_FUNDING_OVERLAP_MS = 24 * 60 * 60 * 1000
# A settled funding row is immutable and Bybit settles hourly at the fastest,
# so re-asking every reconcile pass re-fetches facts the journal already
# holds. The overlap window above is what makes a late posting safe; this
# only decides how often the window is asked for, and it must stay far under
# the overlap: consecutive query windows [t-overlap, t] keep unbroken
# coverage of transaction time whenever queries are less than the overlap
# apart, so a 60s cadence delays discovery of a late-posted row by at most
# 60s and can never miss one. Zero disables the gate (query every pass).
DEFAULT_FUNDING_QUERY_INTERVAL_NS = 60 * 1_000_000_000
# Funding settles hourly at the fastest and one recovery pass makes several
# paginated REST calls, so the 2x-reconcile-cadence bound turned a slow venue
# response into a false staleness page. This floor still fails a wedged
# funding-recovery loop well inside one liveness cycle.
FUNDING_HEALTH_MAX_AGE_FLOOR_NS = 30 * 1_000_000_000
# Same failure mode: a cycle runs funding recovery first (so position truth is
# freshest), and the previous position report legitimately ages by cadence +
# funding duration + position duration. This floor absorbs one slow pass while
# still failing a wedged reconciler inside the 60s venue-fact liveness bound.
# Half the funding floor because position truth gates reduction admission.
POSITION_HEALTH_MAX_AGE_FLOOR_NS = 15 * 1_000_000_000
# The journal records venue-fact changes; any position, mismatch or
# order-ownership change below is journaled the moment it happens. This
# interval is only the floor that keeps an unchanging book visible in the
# permanent record. Proof that the venue loop actually ran lives in the
# owner-health file's venue_facts_at_ns, which the watchdog checks at a
# one-minute bound, so this number no longer sets the liveness contract.
# Ten minutes: about 144 segments a day on a flat book instead of 2,880.
VENUE_SNAPSHOT_CHECKPOINT_INTERVAL_NS = 10 * 60 * 1_000_000_000
# A wedged command is re-probed at most this often, and at most this many per
# pass: wedges are rare, and a resting acknowledged order must not turn every
# reconcile pass into three extra REST reads per order.
WEDGE_PROBE_INTERVAL_NS = 60 * 1_000_000_000
#: Drop-recovery cadence for exchange-native fills. The private stream delivers
#: these; this only catches what it missed, so it does not belong on the 2s
#: reconcile cadence.
TRADE_HISTORY_RECOVERY_INTERVAL_NS = 60 * 1_000_000_000
WEDGE_PROBES_PER_PASS = 5
#: Pending-order confirmation is drop recovery, not the primary path — the
#: private stream is. Ungated it cost two signed REST reads per pending order
#: per pass; at ~175 ms each, one entry resting for its 120 s quote window paid
#: 120 round trips, and six at once put the loop permanently behind its own 2 s
#: cadence. Everything starved that way is a risk control: software stops,
#: take-profits, quote repricing. A dropped fill is now recovered up to this
#: interval later, and the every-pass position-truth check still stands behind
#: it.
PENDING_ORDER_POLL_INTERVAL_NS = 10 * 1_000_000_000
PENDING_ORDER_POLLS_PER_PASS = 5
#: How long confirming pending orders may stand aside for arriving intents
#: before it goes ahead regardless. Five seconds is ten times the reconcile
#: cadence and well inside the ten-second poll interval each order already has.
PENDING_ORDER_POLL_DEFERRAL_CEILING_NS = 5 * 1_000_000_000
#: Default age at which a warm position snapshot stops being trusted and the
#: reconcile reads inline instead. One second is the venue-fact freshness bound
#: at the default 0.5 s cadence, and twice the interval the feed refreshes on.
DEFAULT_POSITION_FEED_TRUST_AGE_NS = 1_000_000_000


class AccountReconciliationStaleError(RuntimeError):
    """The last authenticated position fact is outside its freshness bound."""


class AccountPositionTruthMismatchError(RuntimeError):
    """Authenticated venue position truth contradicts the canonical journal."""


@dataclass(frozen=True, slots=True)
class AccountReconciliationReport:
    snapshot_key: str
    healthy: bool
    pending_orders_checked: int
    execution_rows_observed: int
    order_rows_observed: int
    venue_positions: Mapping[str, float]
    reconstructed_positions: Mapping[str, float]
    mismatches: tuple[str, ...]
    observed_ts_ns: int
    native_protection_breach_only: bool = False
    #: Venue exposure per symbol that this book does not own, signed the way
    #: the venue holds it. Reported, never traded, and never a mismatch.
    foreign_positions: Mapping[str, float] = field(default_factory=dict)

    def require_healthy(self) -> None:
        if not self.healthy:
            raise RuntimeError("account reconciliation unhealthy: " + "; ".join(self.mismatches))


class VenuePositionFeed:
    """The reads every reconcile pass makes, kept warm off the caller's thread.

    Three REST reads ran on every pass and nothing gated them: one
    ``get_positions``, and the two ``get_open_orders`` queries behind
    order-ownership inspection. At ~175 ms each against a Frankfurt edge that
    is over half a second of the owner loop, per pass, blocked -- and
    everything it starves is time-critical: software stops, take-profits,
    quote repricing. A live profile caught the loop sitting in exactly that
    call.

    Moving them here makes venue truth *fresher*, not staler: the feed
    refreshes on its own intervals rather than on the reconcile cadence. The
    thread only reads the venue -- it touches no kernel state, so it adds no
    second mutator.

    The two are polled at different rates because they are worth different
    things. Position truth is what the reduction gate ages, so it is polled
    hard. Order ownership only decides whether a hand-placed order gets logged
    and journaled -- since 2026-08-07 it blocks nothing -- so it keeps roughly
    the cadence reconciliation itself used to run at.

    When the feed has nothing usable, the caller reads synchronously instead. A
    dead or lagging feed therefore degrades to exactly the behaviour that
    shipped before it existed.
    """

    __slots__ = (
        "_client",
        "_settle_coin",
        "_clock",
        "_position_interval_s",
        "_order_interval_s",
        "_wallet_interval_s",
        "_wallet_cache",
        "_positions",
        "_orders",
        "_lock",
        "_stop",
        "_thread",
    )

    def __init__(
        self,
        *,
        client: Any,
        settle_coin: str,
        clock: Clock,
        interval_seconds: float,
        order_interval_seconds: float,
        wallet_interval_seconds: float = 0.0,
        wallet_cache: Any | None = None,
    ) -> None:
        self._client = client
        self._settle_coin = settle_coin
        self._clock = clock
        self._position_interval_s = max(float(interval_seconds), 0.05)
        self._order_interval_s = max(float(order_interval_seconds), 0.05)
        # Bybit's wallet topic pushes only when the balance CHANGES, so on a
        # quiet book the pushed row ages out of its freshness window and every
        # batch pays a blocking get_wallet_balance instead -- measured at a
        # pinned ~195 ms inside intent-durable-to-order-commanded. Refreshing
        # here makes the cached equity *fresher* than the window it replaces,
        # and off the order path entirely.
        self._wallet_interval_s = max(float(wallet_interval_seconds), 0.0)
        self._wallet_cache = wallet_cache
        self._positions: tuple[tuple[dict[str, Any], ...], int] | None = None
        self._orders: tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], int] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._run, name="venue-truth-feed", daemon=True)
        self._thread = thread
        thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    def latest(self) -> tuple[tuple[dict[str, Any], ...], int] | None:
        with self._lock:
            return self._positions

    def latest_open_orders(
        self,
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], int] | None:
        with self._lock:
            return self._orders

    def _read_positions(self) -> None:
        rows = self._client.get_positions(settle_coin=self._settle_coin)
        # Stamped on receipt, exactly where the inline read stamps it.
        observed_ns = self._clock.wall_time_ns()
        with self._lock:
            self._positions = (tuple(rows), observed_ns)

    def _read_open_orders(self) -> None:
        all_kinds = self._client.get_open_orders(settle_coin=self._settle_coin)
        conditional = self._client.get_open_orders(
            settle_coin=self._settle_coin, order_filter="StopOrder"
        )
        observed_ns = self._clock.wall_time_ns()
        with self._lock:
            self._orders = (tuple(all_kinds), tuple(conditional), observed_ns)

    def _read_wallet(self) -> None:
        cache = self._wallet_cache
        if cache is None:
            return
        result = self._client.get_wallet_balance(account_type="UNIFIED", coin="USDT")
        rows = result.get("list") if isinstance(result, Mapping) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
            cache.absorb_account_row(rows[0])

    def _run(self) -> None:
        next_positions = 0.0
        next_orders = 0.0
        next_wallet = 0.0
        wallet_enabled = self._wallet_cache is not None and self._wallet_interval_s > 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            due = (
                ("positions", self._read_positions)
                if now >= next_positions
                else ("wallet", self._read_wallet)
                if wallet_enabled and now >= next_wallet
                else ("open orders", self._read_open_orders)
                if now >= next_orders
                else None
            )
            if due is None:
                self._stop.wait(0.02)
                continue
            label, read = due
            try:
                read()
            except Exception:  # noqa: BLE001 - the caller reads synchronously instead
                _logger.exception(
                    "venue %s feed read failed; reconcile will read inline", label
                )
            # Scheduled from completion, so a slow venue stretches the interval
            # instead of queueing reads back to back.
            if label == "positions":
                next_positions = time.monotonic() + self._position_interval_s
            elif label == "wallet":
                next_wallet = time.monotonic() + self._wallet_interval_s
            else:
                next_orders = time.monotonic() + self._order_interval_s


class BybitAccountReconciler:
    """Recover dropped WS facts, then verify REST order and position truth."""

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        client: Any,
        instrument_rules: Mapping[str, InstrumentRules],
        native_protection_manager: Any | None = None,
        fill_observer: Callable[[str], Any] | None = None,
        clock: Clock | None = None,
        settle_coin: str = "USDT",
        health_max_age_floor_ns: int = POSITION_HEALTH_MAX_AGE_FLOOR_NS,
        auto_resolve_wedges: bool | None = None,
        venue_leverage_observer: Callable[..., None] | None = None,
    ) -> None:
        self.realm = require_named_realm(client, label="account reconciler")
        if int(health_max_age_floor_ns) <= 0:
            raise ValueError("position health max-age floor must be positive")
        self.kernel = kernel
        self.client = client
        self.rules = {symbol.upper(): rule for symbol, rule in instrument_rules.items()}
        self.clock = clock or SystemClock()
        self.health_max_age_floor_ns = int(health_max_age_floor_ns)
        self.settle_coin = settle_coin
        self.native_protection_manager = native_protection_manager
        # Told the leverage every open position actually carries, once a pass.
        # The execution adapter caches what it last set, and this account has a
        # second party — the owner, by hand — who can change it underneath.
        self.venue_leverage_observer = venue_leverage_observer
        # Every realm terminalizes dead commands itself, on the same evidence
        # ladder: a live order, an unreadable venue, or fills this book has not
        # reduced still refuse. Leaving mainnet to classify only meant a wedge
        # sat until an operator noticed, and the account owner is blocked for
        # every minute of that — four hours on 2026-08-07, overnight.
        self.auto_resolve_wedges = (
            True if auto_resolve_wedges is None else bool(auto_resolve_wedges)
        )
        self._wedge_probe_last_ns: dict[str, int] = {}
        self._last_trade_history_recovery_ns: dict[str, int] = {}
        self._last_pending_poll_ns: dict[str, int] = {}
        # Set by the owner to "an intent is already waiting". Confirming pending
        # orders costs two REST reads each, five orders a pass -- up to ten
        # blocking round trips of ~172 ms, and a live profile put the owner
        # inside this call for 23.2% of all wall-clock time. An intent that
        # lands mid-pass waits out whatever is left of that. Deferred orders
        # roll to the next pass least-recently-polled first, so none starves,
        # and the ceiling below stops a steady stream of intents from putting
        # the backstop off indefinitely.
        self.pending_poll_deferral: Callable[[], bool] | None = None
        self._pending_polls_deferred_since_ns = 0
        # How old a warm position snapshot may be and still be used instead of
        # a blocking inline read. The owner sets it from its own venue-fact
        # freshness bound; the default is the same bound at the default cadence.
        self.position_feed_trust_age_ns = DEFAULT_POSITION_FEED_TRUST_AGE_NS
        self._wedge_last_classification: dict[str, str] = {}
        self.consumer = BybitAccountExecutionConsumer(
            kernel=kernel,
            native_protection_manager=native_protection_manager,
            fill_observer=fill_observer,
            clock=self.clock,
        )
        self.last_report: AccountReconciliationReport | None = None
        self._last_journaled_semantic_hash: str | None = None
        self._last_journal_checkpoint_monotonic_ns: int | None = None
        #: Optional warm read of venue positions. Set by the owner runtime once
        #: startup reconciliation has completed strictly and inline.
        self.position_feed: VenuePositionFeed | None = None

    def _venue_positions(self, *, recovered_rows: bool) -> tuple[Any, int]:
        """Venue position truth, from the warm feed when that is sound.

        Two conditions send the read back inline, and both matter:

        ``recovered_rows`` -- this pass applied fills, orders or a wedge
        terminalization to the kernel. A feed snapshot taken before those
        landed shows the venue behind a book that has moved, which is not drift
        but reads exactly like it, and a drift mismatch blocks new risk. Passes
        that changed nothing cannot have that problem. In steady state every
        recovery read is interval-gated, so nothing changes on almost every
        pass and the warm read is used.

        Older than the trust window -- a stalled feed must not have its snapshot
        re-served indefinitely, because freshness is what the reduction gate
        consumes. The window, not "newer than the last report", is the test.

        That distinction was worth 18.55% of the owner's wall clock. The feed
        runs one thread over three reads -- positions, wallet, open orders --
        at roughly 172 ms each against a Frankfurt edge, scheduled from
        completion, so a position refresh lands every ~420 ms and later still
        when the other two interleave. Against a 500 ms reconcile cadence, the
        strictly-newer test failed often enough that a live profile found the
        owner blocked in this inline read for nearly a fifth of all time, with
        a warm snapshot sitting unused a few hundred milliseconds old.

        Timestamps stay honest either way: the report carries the feed's own
        observation time, never this pass's clock, so a lagging feed still ages
        out through the freshness bounds downstream instead of looking current.
        """

        feed = self.position_feed
        if feed is not None and not recovered_rows:
            latest = feed.latest()
            if latest is not None:
                rows, observed_ns = latest
                age_ns = self.clock.wall_time_ns() - observed_ns
                if 0 <= age_ns <= self.position_feed_trust_age_ns:
                    return rows, observed_ns
        raw = self.client.get_positions(settle_coin=self.settle_coin)
        return raw, self.clock.wall_time_ns()

    def _warm_open_orders(
        self, *, recovered_rows: bool
    ) -> tuple[Sequence[Any], Sequence[Any]] | None:
        """The two open-order queries, when the feed holds them.

        ``None`` means inspection reads them itself, unchanged. The
        ``recovered_rows`` condition is the same one position truth uses, and
        is here for the same reason: a pass that just moved the book should
        classify against a venue view taken after it, not before.

        Ownership has no blocking verdict -- an order this book did not place
        is logged and journaled, never refused (decision 2026-08-07) -- so a
        snapshot on the order cadence rather than the pass cadence costs
        nothing but the age of a log line.
        """

        feed = self.position_feed
        if feed is None or recovered_rows:
            return None
        latest = feed.latest_open_orders()
        if latest is None:
            return None
        all_kinds, conditional, _observed_ns = latest
        return all_kinds, conditional

    def _defer_pending_poll(self, now_ns: int) -> bool:
        """Stand aside for a waiting intent, but never past the ceiling."""

        deferral = self.pending_poll_deferral
        if deferral is None:
            self._pending_polls_deferred_since_ns = 0
            return False
        try:
            waiting = bool(deferral())
        except Exception:  # noqa: BLE001 - a broken hint never blocks the backstop
            waiting = False
        if not waiting:
            self._pending_polls_deferred_since_ns = 0
            return False
        if self._pending_polls_deferred_since_ns == 0:
            self._pending_polls_deferred_since_ns = now_ns
            return True
        if now_ns - self._pending_polls_deferred_since_ns >= PENDING_ORDER_POLL_DEFERRAL_CEILING_NS:
            # Long enough. Confirming pending orders is how a fill the private
            # stream missed is recovered; it does not queue behind order flow
            # forever.
            self._pending_polls_deferred_since_ns = 0
            return False
        return True

    def reconcile_once(self) -> AccountReconciliationReport:
        pending_statuses = {"commanded", "acknowledged", "partially_filled"}
        # Cache snapshots are immutable after publication, so this
        # owner-internal read skips deep-copying the venue-snapshot history.
        state = self.kernel._state_ref()
        pending = [order for order in state.orders.values() if order.status in pending_statuses]
        execution_rows = 0
        order_rows = 0
        # Least-recently-polled first, so a large slice batch cannot starve the
        # orders at the end of the command-id ordering. A never-polled order is
        # due at once, so a fresh command still gets one prompt confirmation.
        poll_now_ns = self.clock.wall_time_ns()
        pending_ids = {order.command_id for order in pending}
        self._last_pending_poll_ns = {
            command_id: stamp
            for command_id, stamp in self._last_pending_poll_ns.items()
            if command_id in pending_ids
        }
        due = []
        for order in sorted(
            pending,
            key=lambda item: (self._last_pending_poll_ns.get(item.command_id, -1), item.command_id),
        ):
            last_poll_ns = self._last_pending_poll_ns.get(order.command_id)
            # Never polled is due now, stated rather than inferred from a zero
            # default: under a clock whose epoch is near zero the arithmetic
            # form makes a fresh order wait out the whole interval.
            if last_poll_ns is not None and poll_now_ns - last_poll_ns < PENDING_ORDER_POLL_INTERVAL_NS:
                continue
            due.append(order)
        for order in due[:PENDING_ORDER_POLLS_PER_PASS]:
            if self._defer_pending_poll(poll_now_ns):
                break
            venue_identified_external = order.batch_id.startswith(EXTERNAL_BATCH_PREFIXES) and bool(
                order.venue_order_id
            )
            if venue_identified_external:
                executions = self.client.get_trade_history(
                    symbol=order.symbol,
                    order_id=order.venue_order_id,
                    limit=100,
                )
            else:
                executions = self.client.get_trade_history(
                    symbol=order.symbol,
                    order_link_id=order.command_id,
                )
            if executions:
                execution_rows += len(executions)
                self.consumer.on_execution(
                    {"data": executions},
                    local_receive_ts_ns=self.clock.wall_time_ns(),
                )
            if venue_identified_external:
                history = self.client.get_order_history(
                    symbol=order.symbol,
                    order_id=order.venue_order_id,
                    limit=10,
                )
            else:
                history = self.client.get_order_history(
                    symbol=order.symbol,
                    order_link_id=order.command_id,
                    limit=10,
                )
            if history:
                order_rows += len(history)
                self.consumer.on_order(
                    {"data": history},
                    local_receive_ts_ns=self.clock.wall_time_ns(),
                )
            # Stamped only once both reads landed. Stamping before them would
            # lock a command out for the whole interval on the pass where the
            # read raised — exactly when recovery is most wanted.
            self._last_pending_poll_ns[order.command_id] = self.clock.wall_time_ns()

        # Exchange-native TP/SL orders carry no kernel orderLinkId. Recover
        # executions newer than the active protection installation only, so an
        # older manual fill cannot be mistaken for this stop.
        if self.native_protection_manager is not None:
            current = self.kernel._state_ref()
            protected_symbols = sorted({
                symbol
                for symbol, position in current.positions.items()
                if position.signed_qty != 0.0
                and self.native_protection_manager.active(symbol) is not None
            })
            for symbol in protected_symbols:
                active = self.native_protection_manager.active(symbol)
                if active is None:
                    continue
                # The private stream is the primary path for these rows; this
                # is drop recovery. Ungated it fanned out one paginated REST
                # call per held symbol on every reconcile pass — thirty times a
                # minute per symbol, blocking the owner loop, to re-find rows
                # the socket had already delivered. Same interval the wedge
                # probe above already uses.
                last_recovery_ns = self._last_trade_history_recovery_ns.get(symbol, 0)
                recovery_now_ns = self.clock.monotonic_ns()
                if (
                    last_recovery_ns > 0
                    and recovery_now_ns - last_recovery_ns < TRADE_HISTORY_RECOVERY_INTERVAL_NS
                ):
                    continue
                activated_ns = int(active[1].get("local_receive_ts_ns") or 0)
                # Every row older than the activation is dropped by the filter
                # below, so paging past them is wasted work — and this runs per
                # held symbol every couple of seconds, in line with the owner
                # loop, up to twenty round trips deep each time.
                recent = self.client.get_trade_history(
                    symbol=symbol,
                    limit=50,
                    stop_before_ns=(
                        max(activated_ns - NATIVE_ACTIVATION_CLOCK_TOLERANCE_NS, 0)
                        if activated_ns > 0
                        else 0
                    ),
                )
                # Stamped after the read landed: stamping before it locked the
                # symbol out of recovery for a full interval on the one pass
                # where the call raised.
                self._last_trade_history_recovery_ns[symbol] = recovery_now_ns
                external = []
                refreshed = self.kernel._state_ref()
                for row in recent or []:
                    execution_id = str(row.get("execId") or row.get("exec_id") or "")
                    order_link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
                    exec_ns = _timestamp_ns(row.get("execTime") or row.get("exec_time"))
                    if (
                        not execution_id
                        or execution_id in refreshed.executions
                        or order_link in refreshed.orders
                        or (
                            activated_ns > 0
                            and exec_ns > 0
                            and exec_ns + NATIVE_ACTIVATION_CLOCK_TOLERANCE_NS < activated_ns
                        )
                        or not self.native_protection_manager.is_position_execution(row)
                    ):
                        continue
                    external.append(row)
                if external:
                    execution_rows += len(external)
                    self.consumer.on_execution(
                        {"data": sorted(
                            external,
                            key=lambda row: _timestamp_ns(
                                row.get("execTime") or row.get("exec_time")
                            ),
                        )},
                        local_receive_ts_ns=self.clock.wall_time_ns(),
                    )

        # A command the venue demonstrably does not hold can only be freed by a
        # terminal journal transition — no venue row will ever arrive for it.
        # This pass performs that transition automatically on the CLI's own
        # evidence ladder (a live order or unreduced fills always refuse). On
        # mainnet it only classifies, so the wedge is visible in health while
        # the transition stays an operator act.
        wedge_now_ns = self.clock.wall_time_ns()
        post_wedge_orders = list(self.kernel._state_ref().orders.values())
        open_wedges = (
            *wedged_commands(post_wedge_orders, now_ns=wedge_now_ns),
            *stalled_working_orders(post_wedge_orders, now_ns=wedge_now_ns),
        )
        wedges_resolved = 0
        open_wedge_ids = {wedge.command_id for wedge in open_wedges}
        for stale_id in [key for key in self._wedge_probe_last_ns if key not in open_wedge_ids]:
            self._wedge_probe_last_ns.pop(stale_id, None)
            self._wedge_last_classification.pop(stale_id, None)
        probes_this_pass = 0
        for wedge in open_wedges:
            if probes_this_pass >= WEDGE_PROBES_PER_PASS:
                break
            last_probe_ns = self._wedge_probe_last_ns.get(wedge.command_id, 0)
            if last_probe_ns > 0 and wedge_now_ns - last_probe_ns < WEDGE_PROBE_INTERVAL_NS:
                continue
            wedged_order = self.kernel._state_ref().orders.get(wedge.command_id)
            if wedged_order is None:
                continue
            probes_this_pass += 1
            self._wedge_probe_last_ns[wedge.command_id] = wedge_now_ns
            evidence = probe_wedged_command(
                client=self.client,
                command_id=wedge.command_id,
                symbol=wedged_order.symbol,
                venue_order_id=venue_identified_order_id(wedged_order),
            )
            self._wedge_last_classification[wedge.command_id] = evidence.classification
            if not self.auto_resolve_wedges:
                continue
            owned = self.kernel._state_ref().positions.get(str(wedged_order.symbol).upper())
            try:
                terminalize_wedged_command(
                    kernel=self.kernel,
                    order=wedged_order,
                    wedge=wedge,
                    evidence=evidence,
                    now_ns=wedge_now_ns,
                    resolved_by="account_reconciler",
                    authorize_absent=True,
                    owned_position_qty=owned.signed_qty if owned is not None else 0.0,
                )
            except WedgedCommandResolutionRefused:
                # live / unreadable / unreduced fills: correct refusals. The
                # wedge stays in the mismatch list below until evidence clears.
                continue
            wedges_resolved += 1
            self._wedge_probe_last_ns.pop(wedge.command_id, None)
            self._wedge_last_classification.pop(wedge.command_id, None)

        recovered_rows = bool(execution_rows or order_rows or wedges_resolved)
        # Freshness starts when position truth is received, not before the
        # preceding REST recovery: admission must age the venue fact itself.
        raw_positions, observed_ns = self._venue_positions(recovered_rows=recovered_rows)
        position_rows = _validated_venue_position_rows(raw_positions, realm=self.realm)
        venue_positions: dict[str, float] = {}
        active_sides: dict[str, set[str]] = {}
        venue_leverage: dict[str, float] = {}
        for row, symbol, side, size in position_rows:
            if size == 0.0:
                continue
            signed = size if side == "buy" else -size
            venue_positions[symbol] = math.fsum((venue_positions.get(symbol, 0.0), signed))
            active_sides.setdefault(symbol, set()).add(side)
            leverage = _finite_or_zero(row.get("leverage"))
            if leverage > 0.0:
                venue_leverage[symbol] = leverage
        if self.venue_leverage_observer is not None:
            # Both arguments: a symbol whose row omitted a usable leverage is
            # no evidence, and must not read as a contradiction.
            self.venue_leverage_observer(
                venue_leverage, positioned_symbols=frozenset(venue_positions)
            )
        reconstructed = {
            symbol: position.signed_qty
            for symbol, position in self.kernel._state_ref().positions.items()
            if abs(position.signed_qty) > 1e-12
        }
        post_recovery_state = self.kernel._state_ref()
        # Track which symbols a mismatch implicates: protection
        # re-verification is suspended only for those, so every other symbol
        # still has its stop proved.
        illegible_symbols: set[str] = set()
        mismatches: list[str] = []
        wedged_command_ages_s: dict[str, int] = {}
        for order in sorted(
            post_recovery_state.orders.values(),
            key=lambda item: item.command_id,
        ):
            if (
                order.status != "commanded"
                or order.submission_attempts <= 0
                or order.reduce_only
            ):
                continue
            mismatches.append(
                f"{order.symbol}:ambiguous_submission_unresolved:"
                f"command={order.command_id}:attempts={order.submission_attempts}"
            )
            # An ambiguous exposure-creating submission may land at any time,
            # so the reconstructed quantity cannot anchor a stop plan.
            illegible_symbols.add(str(order.symbol).upper())
        # A wedge that survived the automatic pass is a real health problem:
        # its symbol cannot converge until the command terminalizes. The
        # prefix is deliberately not the symbol — a journal-proven phantom
        # command must not block same-symbol reductions, only entries.
        for wedge in (
            *wedged_commands(post_recovery_state.orders.values(), now_ns=observed_ns),
            *stalled_working_orders(post_recovery_state.orders.values(), now_ns=observed_ns),
        ):
            if wedge.kind == WEDGE_AMBIGUOUS_SUBMISSION and not wedge.reduce_only:
                continue  # already listed as ambiguous_submission_unresolved
            if wedge.kind == WEDGE_STALLED_WORKING_ORDER:
                classification = self._wedge_last_classification.get(wedge.command_id, "")
                if classification not in ("absent", "terminal"):
                    # A long-lived resting order is normal; only one whose
                    # venue side is provably gone is stalled.
                    continue
            # Identity only. This string is hashed into the snapshot's semantic
            # identity, so a ticking age made an unchanged wedge look like new
            # truth every second and journaled a transaction on nearly every
            # pass — tens of thousands of events a day, during exactly the
            # incident being diagnosed. The age is still reported, in the
            # metadata below, which is written but not hashed.
            mismatches.append(
                f"wedged_command:{wedge.kind}:{wedge.symbol}:command={wedge.command_id}"
            )
            wedged_command_ages_s[wedge.command_id] = wedge.age_ns // 1_000_000_000
        native_protection_breach_only = False
        for symbol, sides in sorted(active_sides.items()):
            if len(sides) > 1:
                mismatches.append(f"{symbol}:dual_side_position_not_supported")
                illegible_symbols.add(symbol)
        # The venue nets one position per symbol. Exposure it holds *beyond*
        # this book's own was opened outside the account owner (the owner
        # trading by hand on the same account), and by decision 2026-08-07 the
        # two books run side by side: foreign exposure is recorded, never
        # traded, and never blocks. The reverse — this book claiming exposure
        # the venue does not hold — is still a real contradiction, and it
        # self-heals as external-reduction adoption books the reduction.
        foreign_positions: dict[str, float] = {}
        for symbol in sorted(set(venue_positions) | set(reconstructed)):
            venue_qty = venue_positions.get(symbol, 0.0)
            reconstructed_qty = reconstructed.get(symbol, 0.0)
            rule = self.rules.get(symbol)
            tolerance = max((rule.qty_step / 2.0 if rule else 0.0), 1e-12)
            unbacked = (
                abs(reconstructed_qty)
                if venue_qty * reconstructed_qty <= 0.0
                else max(abs(reconstructed_qty) - abs(venue_qty), 0.0)
            )
            if unbacked > tolerance:
                mismatches.append(
                    f"{symbol}:venue={venue_qty:.16g}:reconstructed={reconstructed_qty:.16g}:"
                    f"unbacked={unbacked:.16g}:tol={tolerance:.16g}"
                )
                illegible_symbols.add(symbol)
                continue
            foreign_qty = venue_qty - reconstructed_qty
            if abs(foreign_qty) > tolerance:
                foreign_positions[symbol] = foreign_qty
                # Deliberately NOT added to ``illegible_symbols``. Skipping a
                # symbol leaves it out of the protection sweep *and* stops
                # ``last_sync_ns_by_symbol`` advancing, so it ages out and
                # ``require_recent_healthy`` raises "native protection health is
                # stale" — re-blocking the account through the back door, on
                # every position the owner scales by hand. The skip is for a
                # book that cannot be trusted; here this book is right, it is
                # simply not the whole venue position. The stop plan is a
                # price, and Bybit's Full-position stops carry no quantity of
                # their own, so foreign size changes nothing about the plan.
        if self.native_protection_manager is not None:
            # Skip per symbol, not for the whole account: a skipped symbol
            # ages out of ``last_sync_ns_by_symbol`` and fails
            # ``require_recent_healthy`` on its own.
            try:
                self.native_protection_manager.reconcile_venue_positions(
                    [row for row, _symbol, _side, _size in position_rows],
                    skip_symbols=frozenset(illegible_symbols),
                )
            except Exception as exc:  # noqa: BLE001 - protection failure makes the snapshot unhealthy
                mismatches.append(f"native_protection:{type(exc).__name__}:{exc}")
                native_protection_breach_only = bool(getattr(exc, "breaches_only", False))
        order_ownership = None
        try:
            order_ownership = inspect_bybit_order_ownership(
                client=self.client,
                state=self.kernel._state_ref(),
                native_order_verifier=(
                    self.native_protection_manager.is_verified_native_order
                    if self.native_protection_manager is not None
                    else None
                ),
                prefetched_rows=self._warm_open_orders(recovered_rows=recovered_rows),
            )
        except Exception as exc:  # noqa: BLE001 - unknown venue orders block health, not the owner loop
            mismatches.append(
                "venue_order_ownership:inspection_failed:"
                f"{type(exc).__name__}:{exc}"
            )
        else:
            # A venue order this owner did not place is the owner trading by
            # hand on the same account (decision 2026-08-07). It is recorded in
            # the journal snapshot below and left strictly alone; blocking on it
            # stopped the whole fleet from entering for hours.
            for unowned_order in order_ownership.unowned_orders:
                _logger.info(
                    "leaving hand-placed venue order alone: %s", unowned_order.description
                )
        ownership_semantics = (
            {
                "status": (
                    "verified"
                    if not order_ownership.unowned_orders
                    else "unowned"
                ),
                "all_kinds_rows_observed": order_ownership.all_kinds_rows_observed,
                "conditional_rows_observed": order_ownership.conditional_rows_observed,
                "unique_orders_observed": order_ownership.unique_orders_observed,
            }
            if order_ownership is not None
            else {
                "status": "unknown",
                "all_kinds_rows_observed": 0,
                "conditional_rows_observed": 0,
                "unique_orders_observed": 0,
            }
        )
        snapshot_semantics = {
            "venue_positions": venue_positions,
            "reconstructed_positions": reconstructed,
            "foreign_positions": foreign_positions,
            "mismatches": mismatches,
            "venue_order_ownership": ownership_semantics,
        }
        snapshot_material = {
            "observed_ts_ns": observed_ns,
            **snapshot_semantics,
        }
        snapshot_key = (
            f"bybit-{self.realm.value}-position:"
            + hashlib.sha256(canonical_json(snapshot_material)).hexdigest()[:20]
        )
        semantic_hash = hashlib.sha256(canonical_json(snapshot_semantics)).hexdigest()
        checkpoint_monotonic_ns = self.clock.monotonic_ns()
        last_checkpoint_ns = self._last_journal_checkpoint_monotonic_ns
        checkpoint_due = (
            last_checkpoint_ns is None
            or checkpoint_monotonic_ns < last_checkpoint_ns
            or checkpoint_monotonic_ns - last_checkpoint_ns
            >= VENUE_SNAPSHOT_CHECKPOINT_INTERVAL_NS
        )
        # Persist semantic transitions immediately; heartbeat unchanged truth.
        if semantic_hash != self._last_journaled_semantic_hash or checkpoint_due:
            self.kernel.record_venue_snapshot(
                snapshot_key=snapshot_key,
                venue_positions=venue_positions,
                reconstructed_positions=reconstructed,
                mismatches=mismatches,
                exchange_ts_ns=0,
                local_receive_ts_ns=observed_ns,
                metadata={
                    "pending_orders_checked": len(pending),
                    "execution_rows_observed": execution_rows,
                    "order_rows_observed": order_rows,
                    "position_rows_observed": len(position_rows),
                    "wedged_command_ages_s": dict(sorted(wedged_command_ages_s.items())),
                    "venue_order_ownership": ownership_semantics,
                    "wedged_commands_resolved": wedges_resolved,
                    "foreign_positions": foreign_positions,
                    "source": f"bybit_{self.realm.value}_rest_reconcile",
                },
            )
            self._last_journaled_semantic_hash = semantic_hash
            self._last_journal_checkpoint_monotonic_ns = checkpoint_monotonic_ns
        report = AccountReconciliationReport(
            snapshot_key=snapshot_key,
            healthy=not mismatches,
            pending_orders_checked=len(pending),
            execution_rows_observed=execution_rows,
            order_rows_observed=order_rows,
            venue_positions=venue_positions,
            reconstructed_positions=reconstructed,
            mismatches=tuple(mismatches),
            observed_ts_ns=observed_ns,
            native_protection_breach_only=(
                native_protection_breach_only
                and len(mismatches) == 1
                and mismatches[0].startswith("native_protection:")
            ),
            foreign_positions=foreign_positions,
        )
        self.last_report = report
        return report

    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        report = self.last_report
        if report is None:
            raise RuntimeError("account reconciliation has not completed")
        age_ns = self.clock.wall_time_ns() - report.observed_ts_ns
        bound_ns = max(int(max_age_ns), self.health_max_age_floor_ns)
        if age_ns < 0 or age_ns > bound_ns:
            raise AccountReconciliationStaleError(
                f"account reconciliation is stale: age_ns={age_ns}"
            )
        report.require_healthy()

    def require_recent_symbols_consistent(
        self,
        symbols: Sequence[str],
        *,
        max_age_ns: int,
    ) -> None:
        """Require fresh direct venue agreement only for requested symbols.

        A strictly reducing order passes unrelated account-health failures but
        never a same-symbol position contradiction. The comparison uses the
        *current* kernel position, not the one captured in the report, so a
        fill reconstructed after the last REST snapshot waits for the next
        reconciliation instead of trading against stale venue-flat evidence.
        """

        report = self.last_report
        if report is None:
            raise RuntimeError("account reconciliation has not completed")
        age_ns = self.clock.wall_time_ns() - report.observed_ts_ns
        bound_ns = max(int(max_age_ns), self.health_max_age_floor_ns)
        if age_ns < 0 or age_ns > bound_ns:
            raise AccountReconciliationStaleError(
                f"account reconciliation is stale: age_ns={age_ns}"
            )
        state = self.kernel._state_ref()
        contradictions = [
            mismatch
            for mismatch in report.mismatches
            if mismatch.startswith("venue_order_ownership:")
        ]
        for raw_symbol in symbols:
            symbol = str(raw_symbol).upper()
            direct_mismatches = [
                mismatch
                for mismatch in report.mismatches
                if mismatch.startswith(f"{symbol}:")
            ]
            if direct_mismatches:
                contradictions.extend(direct_mismatches)
                continue
            venue_qty = float(report.venue_positions.get(symbol, 0.0))
            position = state.positions.get(symbol)
            reconstructed_qty = position.signed_qty if position is not None else 0.0
            rule = self.rules.get(symbol)
            tolerance = max((rule.qty_step / 2.0 if rule else 0.0), 1e-12)
            # Venue exposure above this book's own is foreign and does not
            # contradict a reduction; the venue holding *less* than this book
            # claims does, because the reduction would then oversell.
            unbacked = (
                abs(reconstructed_qty)
                if venue_qty * reconstructed_qty <= 0.0
                else max(abs(reconstructed_qty) - abs(venue_qty), 0.0)
            )
            if unbacked > tolerance:
                contradictions.append(
                    f"{symbol}:venue={venue_qty:.16g}:"
                    f"current_reconstructed={reconstructed_qty:.16g}:"
                    f"unbacked={unbacked:.16g}:tol={tolerance:.16g}"
                )
        if contradictions:
            raise AccountPositionTruthMismatchError(
                "requested venue position truth contradicts reduction: "
                + "; ".join(contradictions)
            )


@dataclass(frozen=True, slots=True)
class AccountFundingReconciliationReport:
    """One strict, bounded transaction-log recovery pass.

    ``query_end_ms`` is the recovered-through bound sampled before the queries;
    ``observed_ts_ns`` is when the pass completed. Freshness measures the
    latter, so a slow venue response cannot birth an already-stale report.
    """

    healthy: bool
    query_start_ms: int
    query_end_ms: int
    settlement_rows_observed: int
    settlement_rows_recorded: int
    observed_ts_ns: int
    # False on a pass that skipped the venue query inside the gate interval:
    # telemetry must never read "we looked and saw nothing" when the truth is
    # "we did not look".
    queried: bool = True

    def require_healthy(self) -> None:
        if not self.healthy:
            raise RuntimeError("account funding reconciliation is unhealthy")


class BybitAccountFundingReconciler:
    """Recover immutable Bybit funding settlements into the account journal.

    Startup replays the whole ledger epoch in API-sized chunks; later passes
    keep an overlap so a delayed transaction-log row is not skipped. An
    existing transaction identity is compared to its canonical P&L event before
    counting as already recovered.
    """

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        client: Any,
        clock: Clock | None = None,
        overlap_ms: int = DEFAULT_FUNDING_OVERLAP_MS,
        health_max_age_floor_ns: int = FUNDING_HEALTH_MAX_AGE_FLOOR_NS,
        query_interval_ns: int = DEFAULT_FUNDING_QUERY_INTERVAL_NS,
    ) -> None:
        self.realm = require_named_realm(client, label="account funding reconciler")
        if int(overlap_ms) <= 0 or int(overlap_ms) > BYBIT_ACCOUNTING_MAX_WINDOW_MS:
            raise ValueError("funding reconciliation overlap must be in (0, 7 days]")
        if int(health_max_age_floor_ns) <= 0:
            raise ValueError("funding health max-age floor must be positive")
        if int(query_interval_ns) < 0 or int(query_interval_ns) // 1_000_000 >= int(overlap_ms):
            raise ValueError("funding query interval must be non-negative and well under the overlap")
        self.kernel = kernel
        self.client = client
        self.clock = clock or SystemClock()
        self.overlap_ms = int(overlap_ms)
        self.health_max_age_floor_ns = int(health_max_age_floor_ns)
        self.query_interval_ns = int(query_interval_ns)
        self._last_query_ns: int | None = None
        self._next_query_start_ms: int | None = None
        self.last_report: AccountFundingReconciliationReport | None = None
        # Incremental settlement index: the committed event list only extends,
        # so this advances from the last verified position and rebuilds if the
        # remembered tail stops matching (reset/replacement).
        self._funding_index: dict[str, AccountEvent] = {}
        self._funding_index_count = 0
        self._funding_index_tail_hash = ""

    def _canonical_funding_index(
        self,
        events: Sequence[AccountEvent],
    ) -> dict[str, AccountEvent]:
        """Advance the incremental settlement index over the append-only list."""

        count = self._funding_index_count
        if (
            len(events) < count
            or (count > 0 and events[count - 1].event_hash != self._funding_index_tail_hash)
        ):
            self._funding_index = _canonical_funding_events(events)
            self._funding_index_count = len(events)
            self._funding_index_tail_hash = events[-1].event_hash if events else ""
            return self._funding_index
        for event in events[count:]:
            if event.event_type != AccountEventType.PNL.value or str(
                event.payload.get("source") or ""
            ) != "venue_funding_settlement":
                continue
            metadata = event.payload.get("metadata") or {}
            if not isinstance(metadata, Mapping):
                raise RuntimeError("canonical venue-funding event lacks metadata")
            identity = str(metadata.get("venue_transaction_id") or "")
            if not identity or identity in self._funding_index:
                raise RuntimeError(
                    "canonical venue-funding events have missing or duplicate transaction ids"
                )
            self._funding_index[identity] = event
        self._funding_index_count = len(events)
        if events:
            self._funding_index_tail_hash = events[-1].event_hash
        return self._funding_index

    def reconcile_once(self) -> AccountFundingReconciliationReport:
        # AccountJournal caches a verified read and invalidates it on any
        # storage-signature change, so this does not re-read every segment.
        events = self.kernel.journal.events()
        if not events:
            raise RuntimeError(
                "funding reconciliation requires the owner startup venue snapshot first"
            )
        epoch_start_ms = max(events[0].wall_ts_ns // 1_000_000, 1)
        observed_ns = self.clock.wall_time_ns()
        observed_ms = observed_ns // 1_000_000
        prior_report = self.last_report
        due = (
            self._last_query_ns is None
            or observed_ns < self._last_query_ns
            or observed_ns - self._last_query_ns >= self.query_interval_ns
            or (observed_ms // 3_600_000) != (self._last_query_ns // 1_000_000 // 3_600_000)
        )
        if not due and prior_report is not None:
            # Nothing was asked, so the recovered-through bound is carried
            # forward unchanged; only liveness advances.
            report = AccountFundingReconciliationReport(
                healthy=True,
                query_start_ms=prior_report.query_start_ms,
                query_end_ms=prior_report.query_end_ms,
                settlement_rows_observed=0,
                settlement_rows_recorded=0,
                observed_ts_ns=observed_ns,
                queried=False,
            )
            self.last_report = report
            return report
        query_start_ms = max(
            epoch_start_ms,
            self._next_query_start_ms
            if self._next_query_start_ms is not None
            else epoch_start_ms,
        )
        if observed_ms <= query_start_ms:
            report = AccountFundingReconciliationReport(
                healthy=True,
                query_start_ms=query_start_ms,
                query_end_ms=observed_ms,
                settlement_rows_observed=0,
                settlement_rows_recorded=0,
                observed_ts_ns=observed_ns,
            )
            self.last_report = report
            return report

        sourced_rows: list[tuple[dict[str, Any], int, int]] = []
        chunk_start_ms = query_start_ms
        while chunk_start_ms < observed_ms:
            chunk_end_ms = min(
                chunk_start_ms + BYBIT_ACCOUNTING_MAX_WINDOW_MS,
                observed_ms,
            )
            rows = self.client.get_account_transactions(
                transaction_type="SETTLEMENT",
                start_time_ms=chunk_start_ms,
                end_time_ms=chunk_end_ms,
                limit=50,
                max_pages=50,
                strict=True,
            )
            if not isinstance(rows, list) or any(
                not isinstance(row, Mapping) for row in rows
            ):
                raise RuntimeError("Bybit SETTLEMENT recovery returned malformed rows")
            sourced_rows.extend(
                (dict(row), chunk_start_ms, chunk_end_ms) for row in rows
            )
            if chunk_end_ms == observed_ms:
                break
            chunk_start_ms = chunk_end_ms + 1

        existing = self._canonical_funding_index(events)
        normalized: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
        seen: set[str] = set()
        for row, source_start_ms, source_end_ms in sourced_rows:
            identity, transaction_ms, values = _validated_funding_row(row, realm=self.realm)
            if identity in seen:
                raise RuntimeError(
                    f"Bybit SETTLEMENT recovery returned duplicate id {identity!r}"
                )
            if not source_start_ms <= transaction_ms <= source_end_ms:
                raise RuntimeError(
                    f"Bybit SETTLEMENT {identity!r} lies outside its requested window"
                )
            seen.add(identity)
            normalized.append((transaction_ms, identity, row, values))

        recorded = 0
        for transaction_ms, identity, row, values in sorted(normalized):
            prior = existing.get(identity)
            if prior is not None:
                _require_same_funding_event(prior, row=row, values=values)
                continue
            row_sha256 = hashlib.sha256(canonical_json(_funding_row_material(row))).hexdigest()
            # Book only this book's own share. The venue settles funding on its
            # netted position, so a settlement can cover exposure this book does
            # not own; booking the row whole credited 10.72 USDT to a book that
            # had earned 0.44 of it (ACEUSDT, 2026-08-07). The share is 1.0
            # whenever the venue position is this book's own, so this changes
            # nothing on an account nobody else trades — and funding is what
            # this strategy is *for*, so a wrong share inflates the measured
            # edge directly.
            settled_size = float(values["settled_size"])
            owned_qty = _owned_qty_at(
                self.kernel._state_ref(), str(values["symbol"]), transaction_ms * 1_000_000
            )
            share = _funding_share(owned_qty, settled_size)
            appended = self.kernel.record_pnl(
                pnl_key=f"venue-funding:{identity}",
                close_key="",
                symbol=str(values["symbol"]),
                gross_pnl_usdt=float(values["cash_flow"]) * share,
                fee_usdt=float(values["fee"]) * share,
                funding_usdt=float(values["funding"]) * share,
                net_pnl_usdt=float(values["change"]) * share,
                exchange_ts_ns=transaction_ms * 1_000_000,
                local_receive_ts_ns=observed_ns,
                source="venue_funding_settlement",
                metadata={
                    "venue_transaction_id": identity,
                    "venue_row_sha256": row_sha256,
                    "transaction_time_ms": transaction_ms,
                    "category": "linear",
                    "currency": "USDT",
                    "cash_equation": "change=cashFlow+funding-fee",
                    # The venue's own immutable numbers, kept verbatim so the
                    # re-verification below still checks this book against the
                    # venue and not against its own arithmetic.
                    "venue_funding_usdt": float(values["funding"]),
                    "venue_change_usdt": float(values["change"]),
                    "venue_settled_size": settled_size,
                    "owned_qty_at_settlement": owned_qty,
                    "owned_share": share,
                },
            )
            if appended:
                recorded += 1

        self._next_query_start_ms = max(
            epoch_start_ms,
            observed_ms - self.overlap_ms,
        )
        self._last_query_ns = observed_ns
        report = AccountFundingReconciliationReport(
            healthy=True,
            query_start_ms=query_start_ms,
            query_end_ms=observed_ms,
            settlement_rows_observed=len(normalized),
            settlement_rows_recorded=recorded,
            observed_ts_ns=self.clock.wall_time_ns(),
        )
        self.last_report = report
        return report

    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        report = self.last_report
        if report is None:
            raise RuntimeError("account funding reconciliation has not completed")
        age_ns = self.clock.wall_time_ns() - report.observed_ts_ns
        bound_ns = max(int(max_age_ns), self.health_max_age_floor_ns)
        if age_ns < 0 or age_ns > bound_ns:
            raise RuntimeError(f"account funding reconciliation is stale: age_ns={age_ns}")
        report.require_healthy()

def _owned_qty_at(state: Any, symbol: str, at_ns: int) -> float:
    """This book's own position in ``symbol`` at ``at_ns``, from its own fills.

    Funding settles on the position held at the settlement instant, and the
    settlement is normally discovered up to a minute later — long enough for
    the position to have changed, and on 2026-08-07 it had gone to flat before
    discovery. Reading the current position would have booked the wrong share.
    """

    symbol = symbol.upper()
    total = 0.0
    for execution in state.executions.values():
        order = state.orders.get(str(execution.get("command_id") or ""))
        if order is None or str(order.symbol).upper() != symbol:
            continue
        if int(execution.get("exchange_ts_ns") or 0) > at_ns:
            continue
        total = math.fsum((total, float(execution.get("signed_qty") or 0.0)))
    return total


def _funding_share(owned_qty: float, settled_size: float) -> float:
    """The fraction of one settlement that belongs to this book.

    1.0 whenever the venue charged exactly what this book owns — which is every
    settlement on an account nobody else trades, so this is an identity there
    and only bites when the venue position is larger than this book.
    """

    if settled_size <= 0.0:
        # No size to divide by: book the row whole, as before.
        return 1.0
    return min(abs(owned_qty) / settled_size, 1.0)


def _canonical_funding_events(
    events: Sequence[AccountEvent],
) -> dict[str, AccountEvent]:
    output: dict[str, AccountEvent] = {}
    for event in events:
        if event.event_type != AccountEventType.PNL.value or str(
            event.payload.get("source") or ""
        ) != "venue_funding_settlement":
            continue
        metadata = event.payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise RuntimeError("canonical venue-funding event lacks metadata")
        identity = str(metadata.get("venue_transaction_id") or "")
        if not identity or identity in output:
            raise RuntimeError(
                "canonical venue-funding events have missing or duplicate transaction ids"
            )
        output[identity] = event
    return output


def _funding_number(
    value: Any,
    *,
    label: str,
    empty_is_zero: bool = False,
) -> float:
    if empty_is_zero and value in (None, ""):
        return 0.0
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be numeric") from exc
    if not math.isfinite(output):
        raise RuntimeError(f"{label} must be finite")
    return output


def _funding_row_material(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id",
            "type",
            "category",
            "currency",
            "symbol",
            "transactionTime",
            "cashFlow",
            "funding",
            "fee",
            "change",
        )
    }


def _validated_funding_row(
    row: Mapping[str, Any],
    *,
    realm: VenueRealm,
) -> tuple[str, int, dict[str, float | str]]:
    identity = str(row.get("id") or "")
    if not identity:
        raise RuntimeError("Bybit SETTLEMENT row lacks id")
    if str(row.get("type") or "").upper() != "SETTLEMENT":
        raise RuntimeError(f"Bybit transaction {identity!r} is not SETTLEMENT")
    if str(row.get("category") or "").lower() != "linear":
        raise RuntimeError(f"Bybit SETTLEMENT {identity!r} is not linear")
    if str(row.get("currency") or "").upper() != "USDT":
        raise RuntimeError(f"Bybit SETTLEMENT {identity!r} is not USDT")
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        raise RuntimeError(f"Bybit SETTLEMENT {identity!r} lacks symbol")
    try:
        transaction_ms = int(str(row.get("transactionTime") or ""))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Bybit SETTLEMENT {identity!r} has invalid transactionTime"
        ) from exc
    if transaction_ms <= 0:
        raise RuntimeError(
            f"Bybit SETTLEMENT {identity!r} has non-positive transactionTime"
        )
    cash_flow = _funding_number(
        row.get("cashFlow"),
        label=f"Bybit SETTLEMENT {identity} cashFlow",
        empty_is_zero=True,
    )
    funding = _funding_number(
        row.get("funding"),
        label=f"Bybit SETTLEMENT {identity} funding",
        empty_is_zero=True,
    )
    fee = _funding_number(
        row.get("fee"),
        label=f"Bybit SETTLEMENT {identity} fee",
        empty_is_zero=True,
    )
    change = _funding_number(
        row.get("change"), label=f"Bybit SETTLEMENT {identity} change"
    )
    if not math.isclose(
        change,
        cash_flow + funding - fee,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise RuntimeError(
            f"Bybit SETTLEMENT {identity!r} violates change=cashFlow+funding-fee"
        )
    # ``cashFlow`` lands in ``gross_pnl_usdt``, which fill reconstruction already
    # books, so a settlement carrying cash would be counted twice. Demo has only
    # ever returned funding-only rows; off demo the shape is unproven.
    if realm is not VenueRealm.DEMO and cash_flow != 0.0:
        raise RuntimeError(
            f"Bybit SETTLEMENT {identity!r} carries cashFlow this reconciler would "
            "double-count against reconstructed fill P&L"
        )
    # The position the venue charged this settlement on. Bybit nets one
    # position per symbol, so this can exceed what this book owns; the share is
    # what makes the booked amount this book's own.
    settled_size = abs(
        _funding_number(
            row.get("size"),
            label=f"Bybit SETTLEMENT {identity} size",
            empty_is_zero=True,
        )
    )
    return identity, transaction_ms, {
        "symbol": symbol,
        "cash_flow": cash_flow,
        "funding": funding,
        "fee": fee,
        "change": change,
        "settled_size": settled_size,
    }


def _require_same_funding_event(
    event: AccountEvent,
    *,
    row: Mapping[str, Any],
    values: Mapping[str, float | str],
) -> None:
    identity = str(row.get("id") or "")
    transaction_ms = int(str(row.get("transactionTime") or ""))
    payload = event.payload
    metadata = payload.get("metadata") or {}
    row_sha256 = hashlib.sha256(canonical_json(_funding_row_material(row))).hexdigest()
    # Compare the venue's own numbers, recorded verbatim, against the venue —
    # not the share-scaled amounts this book earned. Events written before
    # 2026-08-07 carry no verbatim copy and were booked whole, so their booked
    # amounts *are* the venue's.
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    venue_funding = metadata_map.get("venue_funding_usdt", payload.get("funding_usdt"))
    venue_change = metadata_map.get("venue_change_usdt", payload.get("net_pnl_usdt"))
    numeric_pairs = (
        (venue_funding, values["funding"], "funding"),
        (venue_change, values["change"], "net/change"),
    )
    mismatches = [
        label
        for local, venue, label in numeric_pairs
        if not math.isclose(
            _funding_number(local, label=f"canonical funding {identity} {label}"),
            float(venue),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ]
    if (
        event.symbol != str(values["symbol"])
        or str(payload.get("source") or "") != "venue_funding_settlement"
        or int(payload.get("exchange_ts_ns") or 0) != transaction_ms * 1_000_000
        or not isinstance(metadata, Mapping)
        or str(metadata.get("venue_transaction_id") or "") != identity
        or str(metadata.get("venue_row_sha256") or "") != row_sha256
        or mismatches
    ):
        raise RuntimeError(
            f"canonical funding event disagrees with immutable Bybit SETTLEMENT {identity!r}"
        )


def _finite_or_zero(value: Any) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return 0.0
    return output if math.isfinite(output) else 0.0


def _validated_venue_position_rows(
    value: Any,
    *,
    realm: VenueRealm,
) -> tuple[tuple[Mapping[str, Any], str, str, float], ...]:
    """Validate one complete authenticated Bybit position response.

    An empty list is authoritative flatness. A returned row must carry the
    fields needed to prove that; normalizing a malformed row to zero would
    erase unknown exposure.
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(f"Bybit {realm.value} position query returned a non-list payload")
    output: list[tuple[Mapping[str, Any], str, str, float]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise RuntimeError(
                f"Bybit {realm.value} position query returned a non-object row at index {index}"
            )
        raw_symbol = row.get("symbol")
        symbol = raw_symbol.strip().upper() if type(raw_symbol) is str else ""
        if not symbol:
            raise RuntimeError(f"Bybit {realm.value} position row {index} lacks symbol")

        raw_size = row.get("size")
        if raw_size is None or type(raw_size) is bool:
            raise RuntimeError(f"Bybit {realm.value} position row {index} size must be numeric")
        try:
            size = float(raw_size)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Bybit {realm.value} position row {index} size must be numeric"
            ) from exc
        if not math.isfinite(size):
            raise RuntimeError(f"Bybit {realm.value} position row {index} size must be finite")
        if size < 0.0:
            raise RuntimeError(
                f"Bybit {realm.value} position row {index} size must be non-negative"
            )

        raw_side = row.get("side")
        side = raw_side.strip().lower() if type(raw_side) is str else "<invalid>"
        allowed_sides = {"", "buy", "sell"} if size == 0.0 else {"buy", "sell"}
        if side not in allowed_sides:
            raise RuntimeError(
                f"Bybit {realm.value} position row {index} has invalid side {raw_side!r}"
            )
        output.append((row, symbol, side, size))
    return tuple(output)


def _timestamp_ns(value: Any) -> int:
    output = _finite_or_zero(value)
    return int(output * 1_000_000) if output > 0.0 else 0
