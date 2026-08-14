"""Shared per-cycle planning mechanics for target-producing strategy sleeves.

Every forward cycle runner performs the same mechanical sequence around its
sleeve-specific signal logic: read the account owner's health for equity,
snapshot the account planning state (publisher, unresolved durable work,
canonical trades, terminal entry attempts), and suppress target intents that
would duplicate unresolved work, retry terminally rejected attempts, or
collide with a same-cycle exit.  These blocks are shared here so the
suppression invariant cannot drift between sleeves; signal selection, sizing,
and telemetry shapes remain profile-owned.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import polars as pl

from liquidity_migration.account.account_intent_client import (
    ENTRY_ATTEMPT_METADATA_KEY,
    AccountTargetPublisher,
    CompletedEntryAttemptCursor,
    UnresolvedTargetSnapshot,
    completed_expired_entry_attempt_keys,
    unresolved_target_snapshot,
)
from liquidity_migration.account.account_owner_health import (
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
)
from liquidity_migration.account.engine_account_health import require_recent_engine_account
from liquidity_migration.account.entry_attempts import signal_scoped_entry_attempt_key
from liquidity_migration.account.account_kernel import AccountJournalCursor, AccountJournalDigest
from liquidity_migration.account.account_route import AccountRoute
from liquidity_migration.account.account_service import RequestedIntent, SleeveAdapterKind
from liquidity_migration.strategy.account_strategy_state import (
    PROJECTION_EVENT_TYPES,
    CanonicalAccountProjection,
    canonical_account_projection,
    canonical_account_projection_from_digest,
    canonical_strategy_trade_rows,
    rejected_entry_attempt_expiries,
    terminal_entry_attempt_keys,
)


@dataclass(frozen=True, slots=True)
class OwnerHealthReading:
    """One owner-health observation taken off a producer's hot path.

    A producer that must not pay the health read (or its head-retry sleeps) on
    a latency-critical wake takes the reading on an earlier ordinary cycle and
    hands it back to :func:`account_owner_equity_or_error`, which serves it
    only while it is inside the same freshness bound a live read enforces.
    ``error`` is served too: a fresh-but-unusable reading blocks entries
    exactly as the live failure it recorded would.
    """

    equity_usdt: float
    error: str
    read_wall_ts_ns: int
    # The stamp of the owner receipt behind a successful live read. ``None``
    # for error readings (serving a failure only blocks entries) and for
    # callers that build their own reading and accept reading-age stacking on
    # top of receipt age -- carry does, explicitly, for its frozen boundary.
    receipt_wall_ts_ns: int | None = None

    def is_fresh(self, *, now_ns: int, max_age_ns: int = TARGET_PRODUCER_HEALTH_MAX_AGE_NS) -> bool:
        age_ns = int(now_ns) - int(self.read_wall_ts_ns)
        return 0 <= age_ns <= int(max_age_ns)

    def is_serveable(
        self,
        *,
        now_ns: int,
        max_age_ns: int = TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
        stored_max_age_ns: int | None = None,
    ) -> bool:
        """Fresh as a reading, and its receipt would still pass a live read now.

        The second check is what stops ages stacking: without it, a reading
        taken at the edge of the receipt bound could be served for another
        full reading lifetime, and the equity a cycle plans on could be twice
        as old as a live read allows. A reading without a receipt stamp keeps
        the reading-age-only contract.
        """

        if not self.is_fresh(
            now_ns=now_ns,
            max_age_ns=max_age_ns if stored_max_age_ns is None else stored_max_age_ns,
        ):
            return False
        if self.receipt_wall_ts_ns is None:
            return True
        receipt_age_ns = int(now_ns) - int(self.receipt_wall_ts_ns)
        return 0 <= receipt_age_ns <= int(max_age_ns)


def account_owner_health_reading(
    route: AccountRoute,
    *,
    environment: str,
    max_age_ns: int = TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    stored_reading: OwnerHealthReading | None = None,
    now_ns: int | None = None,
    stored_max_age_ns: int | None = None,
) -> OwnerHealthReading:
    """Serve the stored reading, or read live and return a stamped reading.

    A ``stored_reading`` (judged against the caller's ``now_ns``) is served
    only while it :meth:`OwnerHealthReading.is_serveable` -- the reading is
    inside ``stored_max_age_ns`` (defaulting to ``max_age_ns``) AND, when the
    reading carries its receipt's own stamp, a live read at ``now_ns`` would
    still accept that receipt under ``max_age_ns``. Serving returns the same
    object, original stamp and all, so age can never launder itself.

    A stale or absent reading falls through to a live read of the engine
    heartbeat; the returned reading is stamped at ``now_ns`` (or the wall
    clock) and carries the venue's own reading time on success. A failed live
    read returns an error reading with no receipt stamp -- serving a failure
    only blocks entries, the fail-closed direction.

    There is no head-retry ladder any more. It existed because the old owner
    receipt was bound to a journal head that could legitimately be a moment
    behind, and sleeping was cheaper than blocking a cycle over it. The engine
    heartbeat is one file replaced by rename, so there is no pending state to
    wait out: it reads or it does not.
    """

    if (
        stored_reading is not None
        and now_ns is not None
        and stored_reading.is_serveable(
            now_ns=now_ns,
            max_age_ns=max_age_ns,
            stored_max_age_ns=stored_max_age_ns,
        )
    ):
        return stored_reading
    stamp_ns = time.time_ns() if now_ns is None else int(now_ns)
    try:
        account = require_recent_engine_account(
            environment,
            max_age_ns=max_age_ns,
            expected_account_id=route.account_id,
            # Deliberately not `now_ns`. How old the venue reading is, is a
            # question about the wall clock, and the old owner-health check
            # asked it that way too. A caller's `now_ns` is its cycle stamp --
            # a replay or a back-dated test drives it -- and comparing a real
            # timestamp against a simulated one makes a healthy engine look
            # like one reading the venue from the future.
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return OwnerHealthReading(
            equity_usdt=0.0,
            error=f"{type(exc).__name__}: {exc}"[:500],
            read_wall_ts_ns=stamp_ns,
        )
    return OwnerHealthReading(
        equity_usdt=float(account.equity_usdt),
        error="",
        read_wall_ts_ns=stamp_ns,
        # The venue's own reading time, not the heartbeat's write time, so an
        # engine that keeps beating while its venue reads fail ages out here.
        receipt_wall_ts_ns=int(account.observed_ts_ns),
    )


def account_owner_equity_or_error(
    route: AccountRoute,
    *,
    environment: str,
    max_age_ns: int = TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    stored_reading: OwnerHealthReading | None = None,
    now_ns: int | None = None,
    stored_max_age_ns: int | None = None,
) -> tuple[float, str]:
    """Return (equity_usdt, "") from a fresh account reading, or (0.0, error).

    A missing or stale reading never fails the cycle; the caller records the
    error and plans entries as blocked. The serving and live-read contract
    lives in :func:`account_owner_health_reading`; this wrapper just unpacks
    the reading for callers that keep their own.
    """

    reading = account_owner_health_reading(
        route,
        environment=environment,
        max_age_ns=max_age_ns,
        stored_reading=stored_reading,
        now_ns=now_ns,
        stored_max_age_ns=stored_max_age_ns,
    )
    return float(reading.equity_usdt), str(reading.error)


@dataclass(frozen=True, slots=True)
class SleevePlanningSnapshot:
    """One causally ordered account-planning view for a sleeve's cycle."""

    publisher: AccountTargetPublisher
    unresolved_targets: UnresolvedTargetSnapshot
    canonical_trades: pl.DataFrame
    terminal_entry_attempts: frozenset[str]


class PlanningJournalCursor(AccountJournalCursor):
    """Journal cursor plus memos for the journal-pure planning read models.

    The canonical projection and trade rows are pure functions of the digest
    (and the sleeve scope), so on the many cycles where no new journal event
    arrived they would be rebuilt from identical inputs to identical values.
    Memo keys bind to the digest's head hash and fold count; any new event
    invalidates them. ``completed_attempts`` incrementally projects the
    inbox's expired entry attempts the same way. One cursor belongs to one
    daemon; not thread-safe.
    """

    __slots__ = ("_projection_memo", "_trades_memo", "_rejected_attempts_memo", "completed_attempts")

    def __init__(self) -> None:
        super().__init__(retain_event_types=PROJECTION_EVENT_TYPES)
        self._projection_memo: tuple[tuple[str, int], CanonicalAccountProjection] | None = None
        self._trades_memo: tuple[tuple[str, int, str, tuple[str, ...]], pl.DataFrame] | None = None
        self._rejected_attempts_memo: (
            tuple[tuple[str, int, str, tuple[str, ...]], dict[str, int]] | None
        ) = None
        self.completed_attempts = CompletedEntryAttemptCursor()

    def memoized_projection(self, digest: AccountJournalDigest) -> CanonicalAccountProjection:
        key = (digest.head_event_hash, digest.events_folded)
        if self._projection_memo is not None and self._projection_memo[0] == key:
            return self._projection_memo[1]
        projection = canonical_account_projection_from_digest(digest)
        self._projection_memo = (key, projection)
        self._trades_memo = None
        return projection

    def memoized_trades(
        self,
        account_root: Path,
        *,
        digest: AccountJournalDigest,
        projection: CanonicalAccountProjection,
        sleeve: SleeveAdapterKind,
        strategy_ids: tuple[str, ...] | list[str] | set[str],
    ) -> pl.DataFrame:
        key = (
            digest.head_event_hash,
            digest.events_folded,
            sleeve.value,
            tuple(sorted(str(value) for value in strategy_ids)),
        )
        if self._trades_memo is not None and self._trades_memo[0] == key:
            return self._trades_memo[1]
        trades = canonical_strategy_trade_rows(
            account_root,
            sleeve=sleeve.value,
            strategy_ids=strategy_ids,
            account_projection=projection,
        )
        self._trades_memo = (key, trades)
        return trades

    def memoized_rejected_entry_attempts(
        self,
        account_root: Path,
        *,
        digest: AccountJournalDigest,
        projection: CanonicalAccountProjection,
        sleeve: SleeveAdapterKind,
        strategy_ids: tuple[str, ...] | list[str] | set[str],
    ) -> dict[str, int]:
        # The rejected half of the terminal-attempt set is a pure function of
        # the journal and the sleeve scope. The expired half comes from the
        # inbox and keeps its own cursor, so only this half is memoized.
        # Expiries, not keys: whether a rejection still suppresses depends on
        # the clock, and a time-dependent memo keyed on journal identity would
        # keep serving an answer after its window had passed.
        key = (
            digest.head_event_hash,
            digest.events_folded,
            sleeve.value,
            tuple(sorted(str(value) for value in strategy_ids)),
        )
        if self._rejected_attempts_memo is not None and self._rejected_attempts_memo[0] == key:
            return self._rejected_attempts_memo[1]
        rejected = rejected_entry_attempt_expiries(
            account_root,
            sleeve=sleeve.value,
            strategy_ids=strategy_ids,
            account_events=projection.events,
        )
        self._rejected_attempts_memo = (key, rejected)
        return rejected


def new_planning_journal_cursor() -> PlanningJournalCursor:
    """A cycle runner's resumable reader for :func:`sleeve_planning_snapshot`.

    Retains only the event types the canonical read models inspect; every other
    event is still verified and folded into account state, just not kept.  One
    cursor belongs to one daemon and is reused across its cycles — sharing one
    between roots or threads is not supported.
    """

    return PlanningJournalCursor()


def sleeve_planning_snapshot(
    route: AccountRoute,
    *,
    sleeve: SleeveAdapterKind,
    strategy_ids: tuple[str, ...] | list[str] | set[str],
    journal_cursor: AccountJournalCursor | None = None,
    now_ms: int | None = None,
) -> SleevePlanningSnapshot:
    """Snapshot publisher, unresolved work, trades, and terminal attempts.

    The ordering is causal: unresolved durable work is snapshotted before the
    accepted journal is projected.  A request completing in between is
    therefore visible in the later journal read.

    Both read models below derive from ONE journal read.  They used to take a
    full verified read each, so every cycle paid for the whole account history
    twice to learn about a handful of live components.  Passing a
    ``journal_cursor`` (see :func:`new_planning_journal_cursor`) additionally
    re-reads only segments written since the previous cycle; without one the
    read is cold but still happens once.
    """

    publisher = AccountTargetPublisher(route)
    unresolved = unresolved_target_snapshot(publisher.inbox, sleeve=sleeve)
    account_root: Path = route.account_path
    completed_cursor = None
    if journal_cursor is None:
        projection = canonical_account_projection(account_root)
        canonical_trades = canonical_strategy_trade_rows(
            account_root,
            sleeve=sleeve.value,
            strategy_ids=strategy_ids,
            account_projection=projection,
        )
    elif isinstance(journal_cursor, PlanningJournalCursor):
        digest = journal_cursor.read(account_root)
        projection = journal_cursor.memoized_projection(digest)
        canonical_trades = journal_cursor.memoized_trades(
            account_root,
            digest=digest,
            projection=projection,
            sleeve=sleeve,
            strategy_ids=strategy_ids,
        )
        completed_cursor = journal_cursor.completed_attempts
    else:
        projection = canonical_account_projection_from_digest(journal_cursor.read(account_root))
        canonical_trades = canonical_strategy_trade_rows(
            account_root,
            sleeve=sleeve.value,
            strategy_ids=strategy_ids,
            account_projection=projection,
        )
    if isinstance(journal_cursor, PlanningJournalCursor):
        # The journal-pure rejected half rides the cursor's memo; the
        # inbox-driven expired half keeps its own incremental cursor. The
        # union is order-independent, so splitting the two halves here is a
        # pure refactor of terminal_entry_attempt_keys.
        horizon_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        terminal_attempts = frozenset(
            attempt_key
            for attempt_key, valid_until_ms in journal_cursor.memoized_rejected_entry_attempts(
                account_root,
                digest=digest,
                projection=projection,
                sleeve=sleeve,
                strategy_ids=strategy_ids,
            ).items()
            if horizon_ms < valid_until_ms
        ) | completed_expired_entry_attempt_keys(
            publisher.inbox,
            sleeve=sleeve.value,
            strategy_ids=tuple(strategy_ids),
            cursor=completed_cursor,
        )
    else:
        terminal_attempts = terminal_entry_attempt_keys(
            account_root,
            sleeve=sleeve.value,
            strategy_ids=strategy_ids,
            inbox=publisher.inbox,
            account_events=projection.events,
            completed_cursor=completed_cursor,
            now_ms=now_ms,
        )
    return SleevePlanningSnapshot(
        publisher=publisher,
        unresolved_targets=unresolved,
        canonical_trades=canonical_trades,
        terminal_entry_attempts=terminal_attempts,
    )


@dataclass(frozen=True, slots=True)
class SuppressedTargetIntents:
    """Filtered intents plus exact per-reason suppression counts."""

    exit_intents: list[RequestedIntent]
    entry_intents: list[RequestedIntent]
    unresolved_exit_suppressions: int
    unresolved_entry_suppressions: int
    terminal_entry_attempt_suppressions: int
    simultaneous_exit_suppressions: int


def suppress_target_intents(
    *,
    exit_intents: Sequence[RequestedIntent],
    entry_intents: Sequence[RequestedIntent],
    unresolved_target_keys: frozenset[str],
    terminal_entry_attempts: frozenset[str],
    on_entry_suppression: Callable[[RequestedIntent, str], None] | None = None,
) -> SuppressedTargetIntents:
    """Apply the shared three-way target-suppression invariant.

    Exits already pending as unresolved durable requests are dropped.  Entries
    are dropped, in priority order, when their component target is already
    unresolved (``unresolved_account_target``), when their entry attempt was
    terminally rejected or expired (``terminal_entry_attempt``), or when this
    same cycle plans an exit for the identical component key
    (``simultaneous_exit``).  ``on_entry_suppression`` observes each dropped
    entry with its reason so sleeves can attribute blocks without owning the
    filter.
    """

    unresolved_exit = sum(
        intent.intent.target_key in unresolved_target_keys for intent in exit_intents
    )
    kept_exits = [
        intent
        for intent in exit_intents
        if intent.intent.target_key not in unresolved_target_keys
    ]
    exit_target_keys = {intent.intent.target_key for intent in kept_exits}

    unresolved_entry = 0
    terminal_entry = 0
    simultaneous_exit = 0
    kept_entries: list[RequestedIntent] = []
    for intent in entry_intents:
        if intent.intent.target_key in unresolved_target_keys:
            unresolved_entry += 1
            if on_entry_suppression is not None:
                on_entry_suppression(intent, "unresolved_account_target")
            continue
        attempt_key = str(intent.intent.metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or "")
        # Two forms, because the two halves of the terminal set are bounded
        # differently: an account rejection matches the bare key for as long as
        # the signal it carried is valid, while a service expiry matches only
        # the signal instant it expired on.
        if attempt_key and (
            attempt_key in terminal_entry_attempts
            or signal_scoped_entry_attempt_key(attempt_key, intent.intent.metadata)
            in terminal_entry_attempts
        ):
            terminal_entry += 1
            if on_entry_suppression is not None:
                on_entry_suppression(intent, "terminal_entry_attempt")
            continue
        if intent.intent.target_key in exit_target_keys:
            simultaneous_exit += 1
            if on_entry_suppression is not None:
                on_entry_suppression(intent, "simultaneous_exit")
            continue
        kept_entries.append(intent)

    return SuppressedTargetIntents(
        exit_intents=kept_exits,
        entry_intents=kept_entries,
        unresolved_exit_suppressions=unresolved_exit,
        unresolved_entry_suppressions=unresolved_entry,
        terminal_entry_attempt_suppressions=terminal_entry,
        simultaneous_exit_suppressions=simultaneous_exit,
    )
