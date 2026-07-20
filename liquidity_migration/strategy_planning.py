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

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import polars as pl

from .account_intent_client import (
    ENTRY_ATTEMPT_METADATA_KEY,
    AccountTargetPublisher,
    UnresolvedTargetSnapshot,
    unresolved_target_snapshot,
)
from .account_owner_health import (
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    require_recent_account_owner_health,
)
from .account_route import AccountRoute
from .account_service import RequestedIntent, SleeveAdapterKind
from .account_strategy_state import (
    canonical_strategy_trade_rows,
    terminal_entry_attempt_keys,
)


def account_owner_equity_or_error(
    route: AccountRoute,
    *,
    environment: str,
    max_age_ns: int = TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
) -> tuple[float, str]:
    """Return (equity_usdt, "") from fresh owner health, or (0.0, error).

    A missing or stale owner-health receipt never fails the cycle; the caller
    records the error and plans entries as blocked.
    """

    try:
        owner_health = require_recent_account_owner_health(
            route.account_path,
            environment=environment,
            max_age_ns=max_age_ns,
            expected_account_id=route.account_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return 0.0, f"{type(exc).__name__}: {exc}"[:500]
    return float(owner_health.equity_usdt), ""


@dataclass(frozen=True, slots=True)
class SleevePlanningSnapshot:
    """One causally ordered account-planning view for a sleeve's cycle."""

    publisher: AccountTargetPublisher
    unresolved_targets: UnresolvedTargetSnapshot
    canonical_trades: pl.DataFrame
    terminal_entry_attempts: frozenset[str]


def sleeve_planning_snapshot(
    route: AccountRoute,
    *,
    sleeve: SleeveAdapterKind,
    strategy_ids: tuple[str, ...] | list[str] | set[str],
) -> SleevePlanningSnapshot:
    """Snapshot publisher, unresolved work, trades, and terminal attempts.

    The ordering is causal: unresolved durable work is snapshotted before the
    accepted journal is projected.  A request completing in between is
    therefore visible in the later journal read.
    """

    publisher = AccountTargetPublisher(route)
    unresolved = unresolved_target_snapshot(publisher.inbox, sleeve=sleeve)
    account_root: Path = route.account_path
    canonical_trades = canonical_strategy_trade_rows(
        account_root,
        sleeve=sleeve.value,
        strategy_ids=strategy_ids,
    )
    terminal_attempts = terminal_entry_attempt_keys(
        account_root,
        sleeve=sleeve.value,
        strategy_ids=strategy_ids,
        inbox=publisher.inbox,
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
        if str(intent.intent.metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or "") in terminal_entry_attempts:
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
