from __future__ import annotations

import json
import os
import time
from pathlib import Path

import polars as pl

from liquidity_migration.storage import dataset_lock_path, exclusive_file_lock, read_dataset, write_dataset


def test_incremental_parquet_writes_merge_existing_partition(tmp_path: Path) -> None:
    first = pl.DataFrame(
        [
            {"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "buy_quote": 100.0, "sell_quote": 50.0},
        ]
    )
    second = pl.DataFrame(
        [
            {"ts_ms": 1_700_000_060_000, "symbol": "BTCUSDT", "buy_quote": 200.0, "sell_quote": 75.0},
        ]
    )

    write_dataset(first, tmp_path, "funding")
    write_dataset(second, tmp_path, "funding")
    stored = read_dataset(tmp_path, "funding")

    assert stored.height == 2
    assert stored["buy_quote"].sum() == 300.0


def test_incremental_parquet_writes_replace_duplicate_keys(tmp_path: Path) -> None:
    first = pl.DataFrame(
        [
            {"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "buy_quote": 100.0, "sell_quote": 50.0},
        ]
    )
    correction = pl.DataFrame(
        [
            {"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "buy_quote": 125.0, "sell_quote": 50.0},
        ]
    )

    write_dataset(first, tmp_path, "funding")
    write_dataset(correction, tmp_path, "funding")
    stored = read_dataset(tmp_path, "funding")

    assert stored.height == 1
    assert stored["buy_quote"][0] == 125.0


def test_event_demo_trades_dedupe_by_trade_id(tmp_path: Path) -> None:
    trade = pl.DataFrame(
        [{"trade_id": "trade-1", "symbol": "BTCUSDT", "ts_ms": 1_700_000_000_000, "return": 0.01}]
    )

    write_dataset(trade, tmp_path, "event_demo_trades")
    write_dataset(trade.with_columns(pl.lit(0.02).alias("return")), tmp_path, "event_demo_trades")

    stored = read_dataset(tmp_path, "event_demo_trades")

    assert stored.height == 1
    assert stored["return"][0] == 0.02


def test_event_demo_trades_dedupe_keeps_freshest_updated_at_ms_not_last_written(tmp_path: Path) -> None:
    """Two writers (demo cycle + ws_risk engine) both author trade rows, so the
    LAST physical write is not a reliable proxy for the freshest version. When
    rows carry updated_at_ms, dedup must keep the highest updated_at_ms even if
    a STALE-snapshot row is written afterwards — otherwise a slow cycle could
    resurrect a trade the risk engine already closed."""
    fresh = pl.DataFrame(
        [{"trade_id": "t-1", "symbol": "BTCUSDT", "ts_ms": 1_700_000_000_000,
          "status": "closed", "updated_at_ms": 200}]
    )
    stale = pl.DataFrame(
        [{"trade_id": "t-1", "symbol": "BTCUSDT", "ts_ms": 1_700_000_000_000,
          "status": "open", "updated_at_ms": 100}]
    )

    write_dataset(fresh, tmp_path, "event_demo_trades", partition_by=())
    # Stale write lands LAST but is an OLDER version.
    write_dataset(stale, tmp_path, "event_demo_trades", partition_by=())

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert stored.height == 1
    assert stored["status"][0] == "closed", "freshest updated_at_ms must win, not last-written"
    assert stored["updated_at_ms"][0] == 200


def test_read_dataset_handles_schema_evolution_across_partitions(tmp_path: Path) -> None:
    first = pl.DataFrame(
        [
            {
                "trade_id": "trade-1",
                "symbol": "BTCUSDT",
                "date": "2026-01-15",
                "exit_reason": "event_decay",
            }
        ]
    )
    second = pl.DataFrame(
        [
            {
                "trade_id": "trade-2",
                "symbol": "ETHUSDT",
                "date": "2026-01-16",
                "exit_reason": "stop_loss",
                "trigger_price": 99.5,
            }
        ]
    )

    write_dataset(first, tmp_path, "event_demo_trades")
    write_dataset(second, tmp_path, "event_demo_trades")

    stored = read_dataset(tmp_path, "event_demo_trades")

    assert stored.height == 2
    assert "trigger_price" in stored.columns
    assert stored.filter(pl.col("trade_id") == "trade-1").row(0, named=True)["trigger_price"] is None
    assert stored.filter(pl.col("trade_id") == "trade-2").row(0, named=True)["trigger_price"] == 99.5


def test_exclusive_file_lock_cleans_up_lock_file(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.exists()

    assert not lock_path.exists()


def test_exclusive_file_lock_recovers_dead_pid_lock_even_without_stale_timeout(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 2_147_483_647, "created": 1}), encoding="utf-8")

    with exclusive_file_lock(lock_path, stale_seconds=0, poll_seconds=0.0):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] != 2_147_483_647

    assert not lock_path.exists()


def test_exclusive_file_lock_recovers_windows_winerror_87_dead_pid(tmp_path: Path, monkeypatch) -> None:
    # Regression: the test above used pid 2_147_483_647, which on Windows trips
    # os.kill's OverflowError path — not the normal dead-pid path. A pid
    # orphaned by a real killed process is an ordinary integer; on Windows
    # os.kill(pid, 0) for a non-existent pid raises a bare OSError with
    # winerror 87 ("the parameter is incorrect"), NOT ProcessLookupError.
    # Stale-lock recovery must treat that as dead — otherwise every
    # read_dataset/write_dataset blocks until the 6h stale timeout.
    from liquidity_migration import storage

    def fake_kill(pid: int, sig: int) -> None:  # simulate Windows non-existent pid
        err = OSError("simulated non-existent pid")
        err.winerror = 87  # type: ignore[attr-defined]
        raise err

    monkeypatch.setattr(storage.os, "kill", fake_kill)

    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 4321, "created": 1}), encoding="utf-8")

    # stale_seconds=0 disables the timeout path, so recovery MUST come from
    # dead-owner detection alone.
    with exclusive_file_lock(lock_path, stale_seconds=0, poll_seconds=0.0):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()

    assert not lock_path.exists()


def test_exclusive_file_lock_self_heals_when_lock_read_hangs(tmp_path: Path, monkeypatch) -> None:
    """Windows-delete-pending scenario: another thread within this process has
    just unlinked the lock file, but the OS hasn't released the handle yet, so
    ``Path.read_text`` blocks indefinitely. The safe-read wrapper times out and
    returns None; ``_lock_owner_is_dead`` / ``_lock_payload_is_invalid`` MUST
    treat None as "treat as stale, unlink, retry" so the outer loop self-heals
    instead of wedging. Regression for an actual wedge observed under
    ThreadPoolExecutor sweep parallelism."""
    from liquidity_migration import storage

    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Plant a lock whose owner is THIS process (so _lock_owner_is_dead would
    # short-circuit to "alive" via pid==self check) and which has valid JSON
    # (so the payload-invalid path wouldn't fire either). The ONLY way the
    # outer loop can recover is via the safe-read returning None.
    lock_path.write_text(json.dumps({"pid": os.getpid(), "created": 1}), encoding="utf-8")

    # Force the safe-read to return None — simulating a hung Path.read_text.
    monkeypatch.setattr(storage, "_read_lock_text_safe", lambda *_args, **_kwargs: None)

    # stale_seconds=0 + poll_seconds=0 → the ONLY recovery available is
    # owner-dead returning True (which happens when text is None).
    with exclusive_file_lock(lock_path, stale_seconds=0, poll_seconds=0.0):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()

    assert not lock_path.exists()


def test_unlink_with_retry_succeeds_after_transient_permission_error(tmp_path: Path, monkeypatch) -> None:
    """Windows WinError 32 regression: the lock-release ``unlink`` raises
    PermissionError when another process briefly has the file open (a parallel
    sweep worker reading our lock payload via ``_read_lock_text_safe``).
    ``_unlink_with_retry`` must spin past those transient failures and
    eventually succeed, NOT propagate the PermissionError out and crash
    the subprocess. Phase 0 dispatched 8 cells in parallel and every single
    one wedged on this; the fix is the retry."""
    from liquidity_migration import storage

    lock_path = tmp_path / "dataset.lock"
    lock_path.write_text("{}", encoding="utf-8")

    real_unlink = Path.unlink
    call_count = [0]

    def flaky_unlink(self: Path, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 3:
            # First 3 attempts fail with WinError 32 (whether or not we're on
            # Windows — what matters is that the helper does the retry).
            err = PermissionError("simulated WinError 32")
            err.winerror = 32  # type: ignore[attr-defined]
            raise err
        return real_unlink(self, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    storage._unlink_with_retry(lock_path, retries=10, delay=0.001)
    # Eventually succeeded; file is gone.
    assert not lock_path.exists()
    assert call_count[0] == 4  # 3 failures + 1 success


def test_unlink_with_retry_gives_up_silently_when_retries_exhaust(tmp_path: Path, monkeypatch) -> None:
    """If a file is permanently locked (shouldn't happen in practice but
    let's be paranoid), ``_unlink_with_retry`` must return normally
    instead of propagating PermissionError. The next acquire's stale-
    detection path is the safety net that eventually cleans up."""
    from liquidity_migration import storage

    lock_path = tmp_path / "dataset.lock"
    lock_path.write_text("{}", encoding="utf-8")

    def always_locked(self: Path, **kwargs):
        err = PermissionError("simulated permanent lock")
        err.winerror = 32  # type: ignore[attr-defined]
        raise err

    monkeypatch.setattr(Path, "unlink", always_locked)

    # MUST NOT raise.
    storage._unlink_with_retry(lock_path, retries=3, delay=0.001)


def test_exclusive_file_lock_retries_on_windows_permission_error_at_open(tmp_path: Path, monkeypatch) -> None:
    """Windows EACCES regression: ``os.open(..., O_CREAT|O_EXCL)`` can raise
    PermissionError [Errno 13] instead of FileExistsError when the lock file
    is in delete-pending state (another worker is mid-unlink). The retry loop
    MUST treat that exactly like FileExistsError — fall through to the wait
    path — so the worker can re-attempt once the delete completes. Phase 0
    control cell crashed on this; we now treat both exceptions identically."""
    from liquidity_migration import storage

    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Plant a lock owned by a DEAD pid so stale-recovery kicks in after the
    # first PermissionError, letting the test's lock acquire succeed.
    lock_path.write_text(json.dumps({"pid": 2_147_483_647, "created": 1}), encoding="utf-8")

    real_open = storage.os.open
    permission_error_count = [0]

    def flaky_open(path: str, flags: int, *args, **kwargs):
        # First two os.open calls raise PermissionError; subsequent calls
        # work normally. Simulates a brief delete-pending window.
        if (flags & os.O_CREAT) and (flags & os.O_EXCL) and permission_error_count[0] < 2:
            permission_error_count[0] += 1
            raise PermissionError("simulated Windows delete-pending EACCES")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(storage.os, "open", flaky_open)

    with exclusive_file_lock(lock_path, stale_seconds=0, poll_seconds=0.0):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()

    assert permission_error_count[0] == 2  # both flaky attempts hit
    assert not lock_path.exists()


def test_unlink_with_retry_no_op_on_missing_file(tmp_path: Path) -> None:
    """Already-gone files (FileNotFoundError) return immediately without
    retrying — this is the most common case in practice (lock recovery
    after a dead-pid restart)."""
    from liquidity_migration import storage

    lock_path = tmp_path / "never_existed.lock"
    storage._unlink_with_retry(lock_path, retries=3, delay=0.001)
    # No exception. File still doesn't exist.
    assert not lock_path.exists()


def test_thread_lock_for_returns_same_lock_per_path() -> None:
    """The per-process thread-lock layer must return a STABLE Lock object
    per lock-path; otherwise two threads coming in for the same dataset
    grab different Locks and don't actually serialise. Companion to the
    8-writer concurrent test."""
    from liquidity_migration.storage import _thread_lock_for

    path_a = Path("/tmp/sweep_test_thread_lock_a.lock")
    path_b = Path("/tmp/sweep_test_thread_lock_b.lock")
    assert _thread_lock_for(path_a) is _thread_lock_for(path_a)
    assert _thread_lock_for(path_a) is not _thread_lock_for(path_b)


def test_exclusive_file_lock_recovers_malformed_lock_after_grace_without_stale_timeout(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    old_ts = time.time() - 10.0
    os.utime(lock_path, (old_ts, old_ts))

    with exclusive_file_lock(
        lock_path,
        stale_seconds=0,
        poll_seconds=0.0,
        invalid_lock_stale_seconds=0.01,
    ):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()

    assert not lock_path.exists()


def test_event_demo_orders_concurrent_writers_do_not_lose_rows(tmp_path: Path) -> None:
    """The demo (entry) and risk (exit) services BOTH write to
    event_demo_orders. The write path is read-modify-write under a per-dataset
    exclusive file lock with temp-file-rename atomicity. This test pins that
    contract: 8 threads writing 25 unique order_link_ids each must produce a
    final parquet with exactly 200 rows, no duplicates, no torn writes.
    """
    from concurrent.futures import ThreadPoolExecutor
    rows_per_writer = 25
    writer_count = 8

    def write_batch(writer_id: int) -> None:
        batch = pl.DataFrame(
            [
                {
                    "order_link_id": f"link-w{writer_id}-r{i}",
                    "ts_ms": 1_700_000_000_000 + writer_id * 1000 + i,
                    "symbol": "AAAUSDT",
                    "status": "submitted",
                }
                for i in range(rows_per_writer)
            ]
        )
        write_dataset(batch, tmp_path, "event_demo_orders", partition_by=())

    with ThreadPoolExecutor(max_workers=writer_count) as executor:
        list(executor.map(write_batch, range(writer_count)))

    stored = read_dataset(tmp_path, "event_demo_orders")
    assert stored.height == writer_count * rows_per_writer
    unique_links = stored.select("order_link_id").unique().height
    assert unique_links == writer_count * rows_per_writer


def test_event_demo_orders_lock_serializes_concurrent_writers(tmp_path: Path) -> None:
    """No reader should ever observe a torn/partial event_demo_orders parquet:
    while writer A is replacing the file, writer B must either see the
    pre-write contents or block. Implemented via O_CREAT|O_EXCL lock plus
    temp-file rename — verify by reading the dataset between concurrent writes
    and checking row counts are always multiples of the batch size.
    """
    from concurrent.futures import ThreadPoolExecutor
    batch_size = 10

    def write_batch(writer_id: int) -> None:
        batch = pl.DataFrame(
            [
                {
                    "order_link_id": f"link-w{writer_id}-r{i}",
                    "ts_ms": 1_700_000_000_000 + writer_id * 1000 + i,
                    "symbol": "AAAUSDT",
                    "status": "submitted",
                }
                for i in range(batch_size)
            ]
        )
        write_dataset(batch, tmp_path, "event_demo_orders", partition_by=())

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(write_batch, w) for w in range(4)]
        for _ in range(20):
            stored = read_dataset(tmp_path, "event_demo_orders")
            if not stored.is_empty():
                assert stored.height % batch_size == 0
                assert stored.select("order_link_id").n_unique() == stored.height
            time.sleep(0.005)
        for future in futures:
            future.result()
