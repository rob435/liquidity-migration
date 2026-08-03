"""Resumable account-journal reads must equal a cold verified read.

A resumed read folds and verifies exactly the events a cold read would, and the
retained subset changes nothing the canonical read models see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import liquidity_migration.account.account_kernel as account_kernel_module
from liquidity_migration.account.account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountJournalCursor,
    AccountJournalIntegrityError,
    account_journal_path,
    account_transactions_path,
    read_account_journal,
)
from liquidity_migration.strategy.account_strategy_state import (
    PROJECTION_EVENT_TYPES,
    canonical_account_projection,
    canonical_account_projection_from_digest,
    canonical_entry_attempts,
    canonical_strategy_trade_rows,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock

from tests.account.test_account_kernel import (
    _market,
    _policy,
    _rules,
    _snapshot,
    _target,
)


def _kernel(root: Path) -> AccountExecutionKernel:
    return AccountExecutionKernel(
        root,
        account_id="bybit-demo-test",
        clock=VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100_000_000),
        id_seed="cursor-seed",
    )


def _submit(kernel: AccountExecutionKernel, batch: str, qty: float) -> None:
    kernel.submit_targets(
        batch_id=batch,
        market_inputs=[_market()],
        targets=[_target(decision=f"carry-{batch}", key="carry/main/BUSDT", sleeve="carry", qty=qty)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )


def _journal(root: Path, batches: int = 4) -> AccountExecutionKernel:
    root.mkdir(parents=True, exist_ok=True)
    kernel = _kernel(root)
    for index in range(batches):
        _submit(kernel, f"batch-{index}", 1.0 + index)
    return kernel


def test_unfiltered_cursor_reproduces_a_cold_verified_read(tmp_path: Path) -> None:
    _journal(tmp_path)
    expected = read_account_journal(tmp_path, verify=True)

    digest = AccountJournalCursor().read(tmp_path)

    assert [event.event_id for event in digest.events] == [e.event_id for e in expected]
    assert digest.events_folded == len(expected)
    assert digest.head_event_hash == expected[-1].event_hash
    assert digest.resumed_from_segments == 0


def test_resumed_read_equals_a_cold_read_of_the_same_journal(tmp_path: Path) -> None:
    kernel = _journal(tmp_path)
    cursor = AccountJournalCursor()
    first = cursor.read(tmp_path)
    assert first.resumed_from_segments == 0

    _submit(kernel, "batch-late", 9.0)

    resumed = cursor.read(tmp_path)
    cold = AccountJournalCursor().read(tmp_path)

    assert resumed.resumed_from_segments == len(first.segment_names)
    assert resumed.resumed_from_segments > 0
    assert [e.event_id for e in resumed.events] == [e.event_id for e in cold.events]
    assert resumed.events_folded == cold.events_folded
    assert resumed.head_event_hash == cold.head_event_hash
    assert resumed.state.state_hash() == cold.state.state_hash()


def test_reread_without_new_segments_is_stable(tmp_path: Path) -> None:
    _journal(tmp_path)
    cursor = AccountJournalCursor()
    first = cursor.read(tmp_path)
    second = cursor.read(tmp_path)

    assert second.segment_names == first.segment_names
    assert second.events_folded == first.events_folded
    assert [e.event_id for e in second.events] == [e.event_id for e in first.events]
    assert second.resumed_from_segments == len(first.segment_names)


def test_filtered_cursor_folds_every_event_but_retains_only_what_read_models_read(
    tmp_path: Path,
) -> None:
    _journal(tmp_path)
    full = AccountJournalCursor().read(tmp_path)
    filtered = AccountJournalCursor(retain_event_types=PROJECTION_EVENT_TYPES).read(tmp_path)

    # Same fold, same verified head, same account state — only the kept slice differs.
    assert filtered.events_folded == full.events_folded
    assert filtered.head_event_hash == full.head_event_hash
    assert filtered.state.state_hash() == full.state.state_hash()
    assert len(filtered.events) < len(full.events)
    assert {event.event_type for event in filtered.events} <= PROJECTION_EVENT_TYPES
    assert [e.event_id for e in filtered.events] == [
        e.event_id for e in full.events if e.event_type in PROJECTION_EVENT_TYPES
    ]


def test_projection_from_filtered_digest_matches_a_full_projection(tmp_path: Path) -> None:
    _journal(tmp_path)
    full = canonical_account_projection(tmp_path)
    digest = AccountJournalCursor(retain_event_types=PROJECTION_EVENT_TYPES).read(tmp_path)
    filtered = canonical_account_projection_from_digest(digest)

    assert filtered.accepted_batches == full.accepted_batches
    assert filtered.quantity_tolerance == full.quantity_tolerance
    assert filtered.state.state_hash() == full.state.state_hash()

    for sleeve, strategy_ids in (("carry", ("carry_hold_v3",)), ("carry", ()), ("long", ())):
        assert canonical_strategy_trade_rows(
            tmp_path, sleeve=sleeve, strategy_ids=strategy_ids, account_projection=filtered
        ).equals(
            canonical_strategy_trade_rows(
                tmp_path, sleeve=sleeve, strategy_ids=strategy_ids, account_projection=full
            )
        )
        assert canonical_entry_attempts(
            tmp_path, sleeve=sleeve, strategy_ids=strategy_ids, account_events=filtered.events
        ) == canonical_entry_attempts(
            tmp_path, sleeve=sleeve, strategy_ids=strategy_ids, account_events=full.events
        )


def test_projection_refuses_a_digest_missing_read_model_event_types(tmp_path: Path) -> None:
    _journal(tmp_path)
    digest = AccountJournalCursor(
        retain_event_types={AccountEventType.FILL.value}
    ).read(tmp_path)

    with pytest.raises(RuntimeError, match="dropped event types"):
        canonical_account_projection_from_digest(digest)


def test_a_different_journal_is_never_resumed_onto(tmp_path: Path) -> None:
    """Carried state belongs to the prefix it was folded from, nothing else."""

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _journal(first_root, batches=4)
    _journal(second_root, batches=2)

    cursor = AccountJournalCursor()
    first = cursor.read(first_root)
    second = cursor.read(second_root)
    cold = AccountJournalCursor().read(second_root)

    assert second.resumed_from_segments == 0
    assert second.events_folded < first.events_folded
    assert [e.event_id for e in second.events] == [e.event_id for e in cold.events]
    assert second.state.state_hash() == cold.state.state_hash()


def test_an_unreadable_filename_fails_closed(tmp_path: Path) -> None:
    _journal(tmp_path)
    directory = account_transactions_path(tmp_path)
    (directory / "not-a-transaction.json").write_text("{}")

    with pytest.raises(AccountJournalIntegrityError, match="invalid account transaction filename"):
        AccountJournalCursor().read(tmp_path)


def test_a_racy_scan_that_settles_is_retried_not_reported_as_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory scan concurrent with the owner's renames can miss a segment that
    exists; that hole looks exactly like a sequence gap, so the scan is repeated
    before anything is called corrupt.
    """

    _journal(tmp_path)
    real = account_kernel_module._transaction_segment_names
    calls: list[int] = []

    def flaky(root: object) -> list[str]:
        names = real(root)
        calls.append(len(names))
        # First scan drops a middle segment, exactly as a racy scandir would.
        return names[:1] + names[2:] if len(calls) == 1 else names

    monkeypatch.setattr(account_kernel_module, "_transaction_segment_names", flaky)
    digest = AccountJournalCursor().read(tmp_path)

    assert len(calls) == 2
    assert [e.event_id for e in digest.events] == [
        e.event_id for e in read_account_journal(tmp_path, verify=True)
    ]


def test_a_persistent_hole_is_still_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _journal(tmp_path)
    real = account_kernel_module._transaction_segment_names
    monkeypatch.setattr(
        account_kernel_module,
        "_transaction_segment_names",
        lambda root: real(root)[:1] + real(root)[2:],
    )

    with pytest.raises(AccountJournalIntegrityError, match="not contiguous"):
        AccountJournalCursor().read(tmp_path)


def test_a_reset_journal_is_not_resumed_onto_stale_state(tmp_path: Path) -> None:
    _journal(tmp_path)
    cursor = AccountJournalCursor()
    before = cursor.read(tmp_path)

    # An owner-authorized ledger reset archives the journal and starts over.
    # Resuming across that boundary would fold a fresh chain onto dead state.
    for path in account_transactions_path(tmp_path).glob("*.json"):
        path.unlink()
    account_journal_path(tmp_path).unlink(missing_ok=True)
    _submit(_kernel(tmp_path), "batch-after-reset", 3.0)

    after = cursor.read(tmp_path)
    cold = AccountJournalCursor().read(tmp_path)
    assert after.resumed_from_segments == 0
    assert after.events_folded < before.events_folded
    assert [e.event_id for e in after.events] == [e.event_id for e in cold.events]
    assert after.state.state_hash() == cold.state.state_hash()


def test_tampered_new_segment_fails_closed_and_resets_the_cursor(tmp_path: Path) -> None:
    kernel = _journal(tmp_path)
    cursor = AccountJournalCursor()
    good = cursor.read(tmp_path)

    _submit(kernel, "batch-tampered", 7.0)
    directory = account_transactions_path(tmp_path)
    latest = sorted(directory.glob("*.json"))[-1]
    payload = json.loads(latest.read_text())
    payload["events"][0]["payload"] = {"tampered": True}
    latest.write_text(json.dumps(payload))

    with pytest.raises(AccountJournalIntegrityError):
        cursor.read(tmp_path)

    # A cursor that kept the pre-tamper prefix would silently resume on the next
    # call and never re-check the bad segment; it must have reset instead.
    latest.unlink()
    reread = cursor.read(tmp_path)
    assert reread.resumed_from_segments == 0
    assert [e.event_id for e in reread.events] == [e.event_id for e in good.events]


def test_empty_journal_reads_as_an_empty_digest(tmp_path: Path) -> None:
    digest = AccountJournalCursor().read(tmp_path)
    assert digest.events == ()
    assert digest.events_folded == 0
    assert digest.segment_names == ()


def test_every_cycle_runner_receives_a_resumable_cursor() -> None:
    """Each producer must reach ``sleeve_planning_snapshot`` with a cursor.

    The base daemon supplies one through ``_extra_cycle_kwargs``; a subclass that
    overrides that hook and returns a fresh dict silently drops it. The CARRY daemon
    is the deliberate exception -- its cursor rides inside ``CarryCycleState``.
    """

    import inspect

    from liquidity_migration.strategy.carry_demo import CarryCycleState, run_carry_demo_cycle
    from liquidity_migration.strategy.continuous_demo import run_continuous_demo_cycle
    from liquidity_migration.strategy.long_native_event_demo import run_long_native_demo_cycle
    from liquidity_migration.strategy.strategy_planning import new_planning_journal_cursor

    for runner in (run_long_native_demo_cycle, run_continuous_demo_cycle):
        assert "journal_cursor" in inspect.signature(runner).parameters, runner.__name__

    # CARRY carries its own inside the cycle state.
    assert "cycle_state" in inspect.signature(run_carry_demo_cycle).parameters
    assert isinstance(CarryCycleState().journal_cursor, AccountJournalCursor)

    cursor = new_planning_journal_cursor()
    assert isinstance(cursor, AccountJournalCursor)


def test_subclass_cycle_kwargs_keep_the_base_cursor() -> None:
    from liquidity_migration.strategy.continuous_demo_daemon import ContinuousDemoDaemon

    # Bypass __init__: this pins the kwargs contract, not daemon construction.
    daemon = object.__new__(ContinuousDemoDaemon)
    sentinel = AccountJournalCursor()
    daemon._journal_cursor = sentinel  # type: ignore[attr-defined]
    daemon._panel_cache = object()  # type: ignore[attr-defined]

    merged = daemon._extra_cycle_kwargs()

    assert merged["journal_cursor"] is sentinel
    assert merged["panel_cache"] is daemon._panel_cache  # type: ignore[attr-defined]


def test_planning_cursor_memoizes_projection_and_trades_until_a_new_event(tmp_path: Path) -> None:
    from liquidity_migration.account.account_service import SleeveAdapterKind
    from liquidity_migration.strategy.strategy_planning import new_planning_journal_cursor

    kernel = _journal(tmp_path)
    cursor = new_planning_journal_cursor()

    digest = cursor.read(tmp_path)
    projection_first = cursor.memoized_projection(digest)
    trades_first = cursor.memoized_trades(
        tmp_path,
        digest=digest,
        projection=projection_first,
        sleeve=SleeveAdapterKind.CARRY,
        strategy_ids=("carry-v1",),
    )

    digest_same = cursor.read(tmp_path)
    assert cursor.memoized_projection(digest_same) is projection_first
    assert (
        cursor.memoized_trades(
            tmp_path,
            digest=digest_same,
            projection=projection_first,
            sleeve=SleeveAdapterKind.CARRY,
            strategy_ids=("carry-v1",),
        )
        is trades_first
    )

    _submit(kernel, "batch-memo-invalidate", 9.0)
    digest_new = cursor.read(tmp_path)
    projection_new = cursor.memoized_projection(digest_new)
    assert projection_new is not projection_first
    # digest.state aliases the cursor's live fold (documented on
    # AccountJournalDigest); the immutable events tuple is what distinguishes
    # the two projections.
    assert len(projection_new.events) > len(projection_first.events)
    trades_new = cursor.memoized_trades(
        tmp_path,
        digest=digest_new,
        projection=projection_new,
        sleeve=SleeveAdapterKind.CARRY,
        strategy_ids=("carry-v1",),
    )
    assert trades_new is not trades_first


def test_planning_cursor_trades_memo_is_scope_keyed(tmp_path: Path) -> None:
    from liquidity_migration.account.account_service import SleeveAdapterKind
    from liquidity_migration.strategy.strategy_planning import new_planning_journal_cursor

    _journal(tmp_path)
    cursor = new_planning_journal_cursor()
    digest = cursor.read(tmp_path)
    projection = cursor.memoized_projection(digest)
    carry_trades = cursor.memoized_trades(
        tmp_path,
        digest=digest,
        projection=projection,
        sleeve=SleeveAdapterKind.CARRY,
        strategy_ids=("carry-v1",),
    )
    long_trades = cursor.memoized_trades(
        tmp_path,
        digest=digest,
        projection=projection,
        sleeve=SleeveAdapterKind.LONG,
        strategy_ids=("long-v1",),
    )
    assert long_trades is not carry_trades
