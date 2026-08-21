"""Write-behind journal durability: deferred fsyncs, one pre-send barrier.

The write-behind journal renames segments into place synchronously (that is
what makes a commit visible) and moves every fsync to one background thread.
These tests pin the contract that makes that safe:

- committed events and segment bytes are identical to the synchronous journal;
- the committing thread performs no fsync, and ``barrier()`` makes everything
  durable in commit order (files before their directory);
- commits are visible to a fresh reader before any sync happens;
- a lost unsynced suffix reads back as a clean shorter journal, never a hole;
- a dead flusher degrades to synchronous durability instead of losing it;
- a synchronous writer that appends beside a write-behind owner first syncs
  the owner's newest segments, so its durable segment cannot outlive them;
- the execution driver waits on the barrier after the durable attempt claim
  and before the provider send.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import liquidity_migration.account.account_kernel as account_kernel_module
from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountJournal,
    AccountJournalIntegrityError,
    AccountKernelError,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
    account_transactions_path,
    read_account_journal,
)
from liquidity_migration.account.account_contracts import (
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
)
from liquidity_migration.account.execution_adapters import KernelExecutionDriver, OrderCommand
from liquidity_migration.core.deterministic_runtime import VirtualClock


def _market(*, price: float = 10.0, key: str = "book-1") -> MarketInputRef:
    return MarketInputRef(
        input_key=key,
        symbol="BUSDT",
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        reference_price=price,
        bid_price=price - 0.01,
        ask_price=price + 0.01,
        book_sequence=42,
        source="test_l2",
    )


def _target(*, decision: str, key: str, qty: float) -> DesiredTarget:
    return DesiredTarget(
        decision_key=decision,
        target_key=key,
        sleeve="long",
        strategy_id="long-strategy",
        component_id="long-component",
        symbol="BUSDT",
        signed_qty=qty,
        reference_price=10.0,
        leverage=10.0,
        reason="test target",
        metadata={},
    )


def _rules() -> dict[str, InstrumentRules]:
    return {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=1.0,
            tick_size=0.1,
            max_order_qty=100.0,
            max_leverage=20.0,
        )
    }


def _snapshot() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        equity_usdt=10_000.0,
        available_margin_usdt=9_000.0,
        snapshot_key="wallet-1",
        snapshot_ts_ns=950_000_000,
    )


def _policy() -> AccountRiskPolicy:
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=500.0,
        max_initial_margin_usdt=100.0,
        max_leverage=10.0,
    )


def _kernel(root: Path, *, write_behind: bool = False) -> AccountExecutionKernel:
    return AccountExecutionKernel(
        root,
        account_id="bybit-demo-test",
        clock=VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100_000_000),
        id_seed="test-seed",
        journal_write_behind=write_behind,
    )


def _submit_one(kernel: AccountExecutionKernel, *, batch_id: str = "batch-1", qty: float = 1.0):
    return kernel.submit_targets(
        batch_id=batch_id,
        market_inputs=[_market()],
        targets=[_target(decision=batch_id, key=f"long/main/{batch_id}", qty=qty)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )


class _FsyncRecorder:
    """Record every fsync with the thread that performed it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str]] = []  # (thread name, path)
        self._lock = threading.Lock()
        original = account_kernel_module._fsync_path

        def record_path(path: Path) -> None:
            with self._lock:
                self.calls.append((threading.current_thread().name, str(path)))
            original(path)

        monkeypatch.setattr(account_kernel_module, "_fsync_path", record_path)

        import os as os_module

        original_fsync = os_module.fsync

        def record_fd(fd: int) -> None:
            with self._lock:
                self.calls.append((threading.current_thread().name, f"fd:{fd}"))
            original_fsync(fd)

        monkeypatch.setattr(account_kernel_module.os, "fsync", record_fd)

    def threads(self) -> set[str]:
        with self._lock:
            return {thread for thread, _ in self.calls}

    def paths_on(self, thread_name: str) -> list[str]:
        with self._lock:
            return [path for thread, path in self.calls if thread == thread_name]

    def snapshot(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self.calls)


def test_write_behind_matches_synchronous_journal_byte_for_byte(tmp_path: Path) -> None:
    sync_root = tmp_path / "sync"
    behind_root = tmp_path / "behind"
    sync_kernel = _kernel(sync_root)
    behind_kernel = _kernel(behind_root, write_behind=True)

    for batch in ("batch-1", "batch-2"):
        _submit_one(sync_kernel, batch_id=batch)
        _submit_one(behind_kernel, batch_id=batch)
    behind_kernel.journal.barrier()

    sync_segments = sorted(account_transactions_path(sync_root).glob("*.json"))
    behind_segments = sorted(account_transactions_path(behind_root).glob("*.json"))
    assert [path.name for path in sync_segments] == [path.name for path in behind_segments]
    for sync_path, behind_path in zip(sync_segments, behind_segments):
        assert sync_path.read_bytes() == behind_path.read_bytes()

    sync_events = read_account_journal(sync_root, verify=True)
    behind_events = read_account_journal(behind_root, verify=True)
    assert [event.event_hash for event in sync_events] == [
        event.event_hash for event in behind_events
    ]
    behind_kernel.journal.close()


def test_committing_thread_never_fsyncs_in_write_behind_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _FsyncRecorder(monkeypatch)
    this_thread = threading.current_thread().name

    # The synchronous journal fsyncs inline on this thread — the write-behind
    # assertion below would fail against it, which is what makes it a test.
    _submit_one(_kernel(tmp_path / "sync"))
    assert this_thread in recorder.threads()

    recorder.calls.clear()
    kernel = _kernel(tmp_path / "behind", write_behind=True)
    recorder.calls.clear()  # drop the marker write's syncs
    _submit_one(kernel)
    assert this_thread not in recorder.threads()

    kernel.journal.barrier()
    segment = sorted(account_transactions_path(tmp_path / "behind").glob("*.json"))[0]
    synced = {path for _, path in recorder.snapshot()}
    assert str(segment) in synced
    assert str(segment.parent) in synced
    kernel.journal.close()


def test_barrier_syncs_segments_in_commit_order_before_their_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _FsyncRecorder(monkeypatch)
    kernel = _kernel(tmp_path, write_behind=True)
    recorder.calls.clear()
    _submit_one(kernel, batch_id="batch-1")
    _submit_one(kernel, batch_id="batch-2", qty=2.0)
    kernel.journal.barrier()

    segments = sorted(account_transactions_path(tmp_path).glob("*.json"))
    assert len(segments) == 2
    calls = [path for _, path in recorder.snapshot()]
    first, second = (str(path) for path in segments)
    directory = str(segments[0].parent)
    assert first in calls and second in calls and directory in calls
    assert calls.index(first) < calls.index(second)
    assert max(index for index, path in enumerate(calls) if path == directory) > calls.index(
        second
    )
    kernel.journal.close()


def test_unbarriered_commit_is_visible_to_a_fresh_reader(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    result = _submit_one(kernel)
    assert result.accepted

    # A fresh read of the directory — what another process would see — finds
    # the commit before any barrier, because the rename is the visibility
    # point and the page cache is coherent.
    events = read_account_journal(tmp_path, verify=True)
    assert any(event.event_type == "order_command" for event in events)
    kernel.journal.close()


def test_lost_unsynced_suffix_reads_back_as_clean_prefix(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    _submit_one(kernel, batch_id="batch-1")
    prefix_events = len(read_account_journal(tmp_path, verify=True))
    _submit_one(kernel, batch_id="batch-2", qty=2.0)
    kernel.journal.close()

    # Simulate a power loss that cost the last commit: its segment vanishes.
    segments = sorted(account_transactions_path(tmp_path).glob("*.json"))
    segments[-1].unlink()

    survivors = read_account_journal(tmp_path, verify=True)
    assert len(survivors) == prefix_events
    for expected_sequence, event in enumerate(survivors, start=1):
        assert event.sequence == expected_sequence


def test_dead_flusher_falls_back_to_synchronous_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    flusher = kernel.journal._flusher
    assert flusher is not None

    original = account_kernel_module._fsync_path
    failures = {"remaining": 1}

    # A non-OSError escapes the rewrite-through-fresh-pages retry and kills
    # the worker outright — the case this test is about.
    def failing_fsync(path: Path) -> None:
        if failures["remaining"] > 0 and threading.current_thread() is flusher._thread:
            failures["remaining"] -= 1
            raise RuntimeError("injected flusher death")
        original(path)

    monkeypatch.setattr(account_kernel_module, "_fsync_path", failing_fsync)
    _submit_one(kernel, batch_id="batch-1")
    deadline = threading.Event()
    for _ in range(200):
        if not flusher.healthy:
            break
        deadline.wait(0.01)
    assert not flusher.healthy

    # The next commit drains the dead flusher's leftovers inline and then
    # commits synchronously; nothing enqueued is ever dropped. Both batches'
    # events survive with an intact hash chain, including the one whose
    # background fsync failed.
    _submit_one(kernel, batch_id="batch-2", qty=2.0)
    events = read_account_journal(tmp_path, verify=True)
    assert sum(1 for event in events if event.event_type == "risk_decision") == 2
    kernel.journal.close()


def test_synchronous_writer_defensively_syncs_tail_behind_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _kernel(tmp_path, write_behind=True)
    _submit_one(owner, batch_id="batch-1")
    owner.journal.barrier()  # settle the flusher so its syncs stay out of the recording
    owner_segment = sorted(account_transactions_path(tmp_path).glob("*.json"))[0]
    # The owner is still running: its marker is present and its tail may be
    # unsynced. A synchronous writer appends beside it.
    recorder = _FsyncRecorder(monkeypatch)
    bystander = AccountJournal(tmp_path, account_id="bybit-demo-test")
    bystander.transact(lambda state: [], trusted_readonly_builder=True)
    # A no-op transact appends nothing and must not pay the defensive sync.
    assert str(owner_segment) not in recorder.paths_on(threading.current_thread().name)

    recorder.calls.clear()
    sync_kernel = _kernel(tmp_path)
    _submit_one(sync_kernel, batch_id="batch-2", qty=2.0)
    synced_by_writer = recorder.paths_on(threading.current_thread().name)
    assert str(owner_segment) in synced_by_writer
    owner.journal.close()

    # After a clean close the marker is gone and the defensive sync stops.
    recorder.calls.clear()
    _submit_one(_kernel(tmp_path), batch_id="batch-3", qty=3.0)
    assert str(owner_segment) not in recorder.paths_on(threading.current_thread().name)


def test_execution_driver_waits_on_barrier_between_attempt_claim_and_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    result = _submit_one(kernel)
    command_id = result.commands[0].command_id
    ordering: list[str] = []
    original_barrier = kernel.journal.barrier

    def recording_barrier() -> None:
        # The attempt must already be claimed when the barrier runs: a
        # barrier hoisted above record_submission_attempt would still precede
        # the send but would leave the claim unsynced at send time.
        assert kernel.state().orders[command_id].submission_attempts == 1
        ordering.append("barrier")
        original_barrier()

    monkeypatch.setattr(kernel.journal, "barrier", recording_barrier)

    class AmbiguousProvider:
        name = "ambiguous_provider"
        submission_outcome_can_be_ambiguous = True
        max_unsubmitted_exposure_age_ns = 5_000_000_000

        def submit(self, command: OrderCommand, _market: MarketInputRef):
            ordering.append("send")
            durable = kernel.state().orders[command.command_id]
            assert durable.submission_attempts == 1
            return []

    KernelExecutionDriver(kernel).execute_batch(
        result,
        market_inputs={"BUSDT": _market()},
        adapter=AmbiguousProvider(),
    )
    assert ordering[:2] == ["barrier", "send"]
    kernel.journal.close()


def test_barrier_waits_for_another_threads_inflight_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second barrier must not return while another thread's stolen item is
    still being made durable — the pre-send exposure contract depends on it."""

    kernel = _kernel(tmp_path, write_behind=True)
    flusher = kernel.journal._flusher
    assert flusher is not None
    started = threading.Event()
    release = threading.Event()
    original = account_kernel_module._process_durability_item

    def gated(item: tuple) -> None:
        if threading.current_thread() is flusher._thread:
            raise RuntimeError("kill worker")
        if item[0] == "segment":
            started.set()
            assert release.wait(5.0)
        original(item)

    monkeypatch.setattr(account_kernel_module, "_process_durability_item", gated)
    _submit_one(kernel)
    for _ in range(200):
        if not flusher.healthy:
            break
        threading.Event().wait(0.01)
    assert not flusher.healthy

    first = threading.Thread(target=kernel.journal.barrier, name="first-drainer")
    first.start()
    assert started.wait(2.0)

    second_done = threading.Event()

    def second_barrier() -> None:
        kernel.journal.barrier()
        second_done.set()

    second = threading.Thread(target=second_barrier, name="second-drainer")
    second.start()
    # Before the fix this returned immediately with the segment not durable.
    assert not second_done.wait(0.3)
    release.set()
    first.join(5.0)
    second.join(5.0)
    assert second_done.is_set()
    kernel.journal.close()


def test_barrier_does_not_wait_for_trailing_projection_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-wire barrier waits on segments only; the rebuildable projection
    append behind the last segment must not hold the order path.

    Fails without the fix: ``barrier()`` targeted every enqueued item, so with
    the projection append blocked the barrier thread below never finished.
    """

    kernel = _kernel(tmp_path, write_behind=True)
    flusher = kernel.journal._flusher
    assert flusher is not None
    projection_started = threading.Event()
    release_projection = threading.Event()
    original = account_kernel_module._process_durability_item

    def gated(item: tuple) -> None:
        if item[0] != "segment":
            projection_started.set()
            assert release_projection.wait(5.0)
        original(item)

    monkeypatch.setattr(account_kernel_module, "_process_durability_item", gated)
    _submit_one(kernel)

    barrier_done = threading.Event()

    def run_barrier() -> None:
        kernel.journal.barrier()
        barrier_done.set()

    barrier_thread = threading.Thread(target=run_barrier, name="segment-barrier")
    barrier_thread.start()
    # The worker syncs the segment first (commit order), then blocks inside
    # the projection append; the barrier must return regardless.
    assert barrier_done.wait(2.0), "barrier waited for a rebuildable projection"
    assert projection_started.wait(2.0)
    release_projection.set()
    barrier_thread.join(5.0)
    kernel.journal.close()
    # The projection was still processed, in order, by the worker.
    assert account_kernel_module.account_journal_path(tmp_path).exists()


def test_failed_segment_fsync_rewrites_through_fresh_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    flusher = kernel.journal._flusher
    assert flusher is not None
    original_fsync = account_kernel_module._fsync_path
    failures = {"remaining": 1}

    def failing_fsync(path: Path) -> None:
        if (
            failures["remaining"] > 0
            and threading.current_thread() is flusher._thread
            and str(path).endswith(".json")
        ):
            failures["remaining"] -= 1
            raise OSError(5, "Input/output error")
        original_fsync(path)

    rewrites: list[str] = []
    original_replace = account_kernel_module._atomic_replace

    def recording_replace(path: Path, data: bytes, *, durable: bool = True) -> None:
        if durable:
            rewrites.append(str(path))
        original_replace(path, data, durable=durable)

    monkeypatch.setattr(account_kernel_module, "_fsync_path", failing_fsync)
    monkeypatch.setattr(account_kernel_module, "_atomic_replace", recording_replace)
    _submit_one(kernel)
    kernel.journal.barrier()
    # An I/O error heals by rewriting the same bytes through fresh pages —
    # a bare fsync retry can falsely succeed after the kernel drops the
    # dirty pages — and the worker survives.
    assert flusher.healthy
    segment = sorted(account_transactions_path(tmp_path).glob("*.json"))[0]
    assert str(segment) in rewrites
    events = read_account_journal(tmp_path, verify=True)
    assert any(event.event_type == "order_command" for event in events)
    kernel.journal.close()


def test_transact_throttles_when_unsynced_tail_reaches_the_defensive_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    flusher = kernel.journal._flusher
    assert flusher is not None
    barriers: list[bool] = []
    original_barrier = flusher.barrier

    def recording_barrier() -> None:
        barriers.append(True)
        original_barrier()

    monkeypatch.setattr(flusher, "barrier", recording_barrier)
    monkeypatch.setattr(
        flusher,
        "pending_segments",
        lambda: account_kernel_module._DEFENSIVE_TAIL_SEGMENTS,
    )
    _submit_one(kernel)
    assert barriers, "an unsynced tail at the defensive bound must force a drain"
    kernel.journal.close()


def test_init_settles_a_crashed_write_behind_root(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    _submit_one(kernel, batch_id="batch-1")
    _submit_one(kernel, batch_id="batch-2", qty=2.0)
    kernel.journal.close()

    # Simulate the crash: the marker back in place (torn by the same power
    # loss, hence unreadable) and the last segment torn mid-file.
    marker = account_transactions_path(tmp_path).parent / "write_behind.owner"
    marker.write_bytes(b"torn marker")
    segments = sorted(account_transactions_path(tmp_path).glob("*.json"))
    prefix_segments = len(segments) - 1
    torn = segments[-1]
    torn.write_bytes(torn.read_bytes()[: max(1, torn.stat().st_size // 2)])

    fresh = _kernel(tmp_path)  # synchronous init settles the root
    assert not marker.exists()
    assert torn.with_name(torn.name + ".torn").exists()
    assert len(sorted(account_transactions_path(tmp_path).glob("*.json"))) == prefix_segments
    survivors = read_account_journal(tmp_path, verify=True)
    for expected_sequence, event in enumerate(survivors, start=1):
        assert event.sequence == expected_sequence
    # The settled journal keeps working.
    _submit_one(fresh, batch_id="batch-3", qty=3.0)


def test_init_refuses_a_torn_segment_followed_by_a_readable_one(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    _submit_one(kernel, batch_id="batch-1")
    _submit_one(kernel, batch_id="batch-2", qty=2.0)
    kernel.journal.close()

    marker = account_transactions_path(tmp_path).parent / "write_behind.owner"
    marker.write_bytes(b"torn marker")
    segments = sorted(account_transactions_path(tmp_path).glob("*.json"))
    segments[0].write_bytes(b"{torn")

    # An unreadable segment with a readable one after it is corruption, not a
    # crash tail: the settle must refuse, never quarantine mid-chain.
    with pytest.raises(AccountJournalIntegrityError, match="refusing to repair"):
        _kernel(tmp_path)


def test_second_write_behind_owner_on_a_live_marker_is_refused(tmp_path: Path) -> None:
    journal_dir = tmp_path / "account_journal"
    journal_dir.mkdir(parents=True)
    # pid 1 is always alive and never ours.
    (journal_dir / "write_behind.owner").write_bytes(b'{"pid": 1}')
    with pytest.raises(AccountKernelError, match="another live write-behind"):
        _kernel(tmp_path, write_behind=True)


def test_close_drains_and_retires_the_marker(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, write_behind=True)
    _submit_one(kernel)
    marker = account_transactions_path(tmp_path).parent / "write_behind.owner"
    assert marker.exists()
    kernel.journal.close()
    assert not marker.exists()
    events = read_account_journal(tmp_path, verify=True)
    assert any(event.event_type == "order_command" for event in events)
