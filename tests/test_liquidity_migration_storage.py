from __future__ import annotations

import json
import os
import time
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.storage import dataset_lock_path, dataset_path, exclusive_file_lock, read_dataset, write_dataset


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


def test_continuous_fade_datasets_registered_and_roundtrip(tmp_path: Path) -> None:
    """Regression: run_continuous_demo_cycle ALWAYS writes its cycles ledger
    (and trades/orders when recording), so every continuous dataset must be in
    the DATASETS allowlist + DATASET_KEYS, else the live cycle crashes with
    'Unknown dataset' on its first write. Roundtrip each with its dedupe key."""
    from liquidity_migration.continuous_demo import (
        ContinuousDemoCycleConfig,
        continuous_dataset_names,
    )
    from liquidity_migration.storage import DATASET_KEYS, DATASETS

    demo_names = continuous_dataset_names(ContinuousDemoCycleConfig())
    paper_names = continuous_dataset_names(ContinuousDemoCycleConfig(paper_mode=True))
    key_by_suffix = {"trades": "trade_id", "orders": "order_link_id", "cycles": "cycle_id"}
    for trades, orders, cycles in (demo_names, paper_names):
        for dataset in (trades, orders, cycles):
            assert dataset in DATASETS, f"{dataset} missing from DATASETS allowlist"
            assert dataset in DATASET_KEYS, f"{dataset} missing from DATASET_KEYS"
        # Roundtrip + dedupe on each dataset's declared key.
        for dataset, suffix in ((trades, "trades"), (orders, "orders"), (cycles, "cycles")):
            key = key_by_suffix[suffix]
            row = pl.DataFrame([{key: "k1", "symbol": "AAAUSDT", "v": 1}])
            write_dataset(row, tmp_path, dataset, partition_by=())
            write_dataset(row.with_columns(pl.lit(2).alias("v")), tmp_path, dataset, partition_by=())
            stored = read_dataset(tmp_path, dataset)
            assert stored.height == 1 and stored["v"][0] == 2


def test_continuous_orders_read_prefers_bucketed_final_over_legacy_preflight(tmp_path: Path) -> None:
    """A migrated continuous order can have an old monolithic preflight row and a
    newer bucketed final row. The read path must keep the newest order state by
    updated_at_ms, otherwise restart reconciliation can resurrect a stale intent."""
    root = dataset_path(tmp_path, "continuous_fade_demo_orders")
    root.mkdir(parents=True, exist_ok=True)
    link = "lm-en-c-ABC-aaaa"
    pl.DataFrame(
        [
            {
                "order_link_id": link,
                "ts_ms": 1_700_000_000_000,
                "trade_id": "t1",
                "symbol": "ABCUSDT",
                "submit_mode": "preflight",
                "status": "submitted",
            }
        ]
    ).write_parquet(root / "part.parquet")

    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": link,
                    "ts_ms": 1_700_000_010_000,
                    "updated_at_ms": 1_700_000_010_000,
                    "trade_id": "t1",
                    "symbol": "ABCUSDT",
                    "submit_mode": "submitted",
                    "status": "filled",
                }
            ]
        ),
        tmp_path,
        "continuous_fade_demo_orders",
        partition_by=(),
    )

    stored = read_dataset(tmp_path, "continuous_fade_demo_orders")
    assert stored.height == 1
    row = stored.to_dicts()[0]
    assert row["submit_mode"] == "submitted"
    assert row["status"] == "filled"
    assert row["updated_at_ms"] == 1_700_000_010_000


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


def test_exclusive_file_lock_recovers_windows_winerror_11_dead_pid(tmp_path: Path, monkeypatch) -> None:
    # Regression (Python 3.13 / Windows): os.kill(pid, 0) for an out-of-range pid
    # (e.g. the 2_147_483_647 used by the test above) raises OSError winerror 11
    # ERROR_BAD_FORMAT — not 87, not OverflowError. _lock_owner_is_dead must treat
    # ANY non-permission OSError as dead, else exclusive_file_lock with
    # poll_seconds=0.0 + stale_seconds=0 spins CPU-bound forever (observed: the
    # full pytest gate wedged ~40 min on this single test on the 5950X box).
    from liquidity_migration import storage

    def fake_kill(pid: int, sig: int) -> None:  # simulate Windows out-of-range pid
        err = OSError("simulated out-of-range pid")
        err.winerror = 11  # type: ignore[attr-defined]
        raise err

    monkeypatch.setattr(storage.os, "kill", fake_kill)

    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 4321, "created": 1}), encoding="utf-8")

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


# --- reconcile-ledger-5 / quality-dup-5: month-bucketed ledgers -------------
from liquidity_migration.storage import (  # noqa: E402
    _LEDGER_MONTH_COL,
    read_ledger_window,
)

_MS_PER_DAY = 86_400_000


def _trade(trade_id: str, entry_ts_ms: int, **over) -> dict:
    row = {
        "trade_id": trade_id, "symbol": "AAAUSDT", "side": "short", "status": "open",
        "entry_ts_ms": entry_ts_ms, "ts_ms": entry_ts_ms, "qty": "1", "entry_price": 100.0,
        "updated_at_ms": entry_ts_ms,
    }
    row.update(over)
    return row


def test_ledger_writes_month_buckets_and_read_strips_partition_col(tmp_path: Path) -> None:
    """A bucketed ledger writes one part per calendar month (keyed on the immutable
    entry_ts_ms), and read_dataset returns a frame schema-identical to the legacy
    monolith — the internal _ledger_month column never leaks to callers."""
    jan = 1_704_067_200_000  # 2024-01-01 UTC
    mar = jan + 70 * _MS_PER_DAY  # ~2024-03
    write_dataset(pl.DataFrame([_trade("t-jan", jan)]), tmp_path, "event_demo_trades", partition_by=())
    write_dataset(pl.DataFrame([_trade("t-mar", mar)]), tmp_path, "event_demo_trades", partition_by=())
    root = dataset_path(tmp_path, "event_demo_trades")
    buckets = sorted(p.name for p in root.glob(f"{_LEDGER_MONTH_COL}=*"))
    assert buckets == [f"{_LEDGER_MONTH_COL}=202401", f"{_LEDGER_MONTH_COL}=202403"]
    out = read_dataset(tmp_path, "event_demo_trades")
    assert _LEDGER_MONTH_COL not in out.columns
    assert set(out["trade_id"].to_list()) == {"t-jan", "t-mar"}


def test_ledger_immutable_entry_ts_key_no_phantom_open_after_update(tmp_path: Path) -> None:
    """CRITICAL (the design's central risk): a trade UPDATE rewrites ts_ms=now but
    PRESERVES entry_ts_ms. Bucketing on the immutable entry_ts_ms keeps the open and
    closed versions in the SAME bucket, so _write_part dedup collapses them to ONE
    closed row. Had the bucket keyed on the mutable ts_ms, the update would land in a
    new bucket and the stale OPEN copy would survive -> a phantom open trade on the
    netted account."""
    entry = 1_704_067_200_000  # 2024-01
    later = entry + 70 * _MS_PER_DAY  # ~2024-03 (would be a different bucket if ts_ms-keyed)
    write_dataset(pl.DataFrame([_trade("t1", entry)]), tmp_path, "event_demo_trades", partition_by=())
    # Update: same trade_id + entry_ts_ms, but ts_ms moved forward and status closed.
    write_dataset(
        pl.DataFrame([_trade("t1", entry, ts_ms=later, status="closed", updated_at_ms=later)]),
        tmp_path, "event_demo_trades", partition_by=(),
    )
    out = read_dataset(tmp_path, "event_demo_trades")
    assert out.height == 1                          # no phantom: exactly one row
    assert out["status"].to_list() == ["closed"]    # the freshest version wins
    # And it stayed in the entry-month bucket, not the ts_ms-month bucket.
    root = dataset_path(tmp_path, "event_demo_trades")
    assert [p.name for p in root.glob(f"{_LEDGER_MONTH_COL}=*")] == [f"{_LEDGER_MONTH_COL}=202401"]


def test_ledger_read_dedups_legacy_monolith_and_bucket_coexistence(tmp_path: Path) -> None:
    """Half-migrated state: a trade_id present in BOTH the legacy monolithic
    part.parquet AND a (newer) month bucket must read as ONE row — the freshest
    updated_at_ms — never doubled. Guards the migration window."""
    entry = 1_704_067_200_000
    root = dataset_path(tmp_path, "event_demo_trades")
    root.mkdir(parents=True, exist_ok=True)
    # Simulate a legacy monolith (pre-migration) with the OLD version of t1.
    pl.DataFrame([_trade("t1", entry, status="open", updated_at_ms=entry)]).write_parquet(root / "part.parquet")
    # A newer bucketed write of the SAME trade_id (closed).
    write_dataset(
        pl.DataFrame([_trade("t1", entry, status="closed", updated_at_ms=entry + 1000)]),
        tmp_path, "event_demo_trades", partition_by=(),
    )
    out = read_dataset(tmp_path, "event_demo_trades")
    assert out.height == 1                          # not double-counted across monolith+bucket
    assert out["status"].to_list() == ["closed"]    # freshest updated_at_ms wins


def test_ledger_schema_drift_across_buckets_unions(tmp_path: Path) -> None:
    """Schema drift across month buckets (a later era adds a column) must union via the
    diagonal_relaxed fallback rather than raise — the regression that surfaced as
    ColumnNotFoundError under a plain multi-file scan_parquet."""
    jan = 1_704_067_200_000
    mar = jan + 70 * _MS_PER_DAY
    write_dataset(pl.DataFrame([_trade("t-jan", jan)]), tmp_path, "event_demo_trades", partition_by=())
    write_dataset(
        pl.DataFrame([_trade("t-mar", mar, exit_reason="stop_loss", exit_price=95.0)]),  # extra cols
        tmp_path, "event_demo_trades", partition_by=(),
    )
    out = read_dataset(tmp_path, "event_demo_trades")
    assert set(out["trade_id"].to_list()) == {"t-jan", "t-mar"}
    assert "exit_reason" in out.columns  # the wider schema is preserved


def test_read_ledger_window_recent_plus_legacy_excludes_old(tmp_path: Path) -> None:
    """The windowed reconcile read returns the most-recent months_back buckets PLUS the
    legacy (_ledger_month=0) tail, and excludes older non-recent buckets — bounding the
    per-pass read while never dropping the not-yet-migrated open-trade tail."""
    base = 1_704_067_200_000  # 2024-01
    m_jan = base
    m_mar = base + 70 * _MS_PER_DAY   # 2024-03
    m_may = base + 130 * _MS_PER_DAY  # ~2024-05
    for tid, ts in [("t-jan", m_jan), ("t-mar", m_mar), ("t-may", m_may)]:
        write_dataset(pl.DataFrame([_trade(tid, ts)]), tmp_path, "event_demo_trades", partition_by=())
    # A legacy monolith row (bucket 0) must ALWAYS be included.
    root = dataset_path(tmp_path, "event_demo_trades")
    pl.DataFrame([_trade("t-legacy", m_jan)]).write_parquet(root / "part.parquet")
    out = read_ledger_window(tmp_path, "event_demo_trades", months_back=1)
    got = set(out["trade_id"].to_list())
    assert "t-may" in got and "t-legacy" in got   # newest bucket + legacy tail
    assert "t-jan" not in got and "t-mar" not in got  # older buckets excluded by the window


def test_non_ledger_dataset_is_not_month_bucketed(tmp_path: Path) -> None:
    """A dataset not in LEDGER_BUCKET_SOURCE keeps its normal layout — no _ledger_month
    bucketing, no read-time key dedup behavior change."""
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_704_067_200_000, "symbol": "BTCUSDT", "buy_quote": 1.0, "sell_quote": 2.0}]),
        tmp_path, "funding",
    )
    root = dataset_path(tmp_path, "funding")
    assert not list(root.glob(f"{_LEDGER_MONTH_COL}=*"))  # not bucketed
    assert read_dataset(tmp_path, "funding").height == 1


def test_exclusive_file_lock_release_does_not_delete_successor_lock(tmp_path: Path) -> None:
    """CROS-1: if our lock is stale-evicted and a successor recreates the path with a NEW
    inode while we're still in the critical section, release must NOT delete the successor's
    lock (an unconditional unlink-by-path would admit a second concurrent writer)."""
    lock = tmp_path / "x.lock"
    with exclusive_file_lock(lock, stale_seconds=600):
        os.unlink(lock)
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": 999_999, "created": time.time()}))
        successor_ino = os.stat(lock).st_ino
    assert lock.exists(), "release deleted a successor's lock (CROS-1)"
    assert os.stat(lock).st_ino == successor_ino


def test_exclusive_file_lock_release_unlinks_its_own_lock(tmp_path: Path) -> None:
    """CROS-1 happy path: a normal release (no eviction) still removes our own lock."""
    lock = tmp_path / "z.lock"
    with exclusive_file_lock(lock, stale_seconds=600):
        assert lock.exists()
    assert not lock.exists()


def test_lock_owner_is_dead_evicts_reused_pid(tmp_path: Path, monkeypatch) -> None:
    """CROS-2: a live-but-REUSED pid (started after the lock's created ts) is treated as
    dead so the stale lock self-heals immediately instead of waiting out the stale timeout.
    An unknown start time (non-Linux / no /proc) stays conservative (owner alive).

    Uses a live CHILD process pid, not pid 1: pid 1 does not exist on Windows, so
    os.kill(pid, 0) classifies the owner dead before the reused-pid logic is ever
    reached (the long-standing env-artifact failure on the Windows research box)."""
    import subprocess
    import sys

    from liquidity_migration import storage

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
    try:
        lock = tmp_path / "y.lock"
        lock.write_text(
            json.dumps({"pid": proc.pid, "created": time.time() - 100}), encoding="utf-8"
        )
        monkeypatch.setattr(storage, "_pid_started_after", lambda pid, _created: True)
        assert storage._lock_owner_is_dead(lock) is True
        monkeypatch.setattr(storage, "_pid_started_after", lambda pid, _created: None)
        assert storage._lock_owner_is_dead(lock) is False
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)


def test_pid_started_after_guards_and_current_process() -> None:
    import sys
    from liquidity_migration import storage

    assert storage._pid_started_after(os.getpid(), 0.0) is None  # created<=0 -> unknown
    if sys.platform.startswith("linux"):
        assert storage._pid_started_after(os.getpid(), 1.0) is True  # we started after epoch 1
        assert storage._pid_started_after(os.getpid(), time.time() + 1e9) is False
    else:
        assert storage._pid_started_after(os.getpid(), 1.0) is None  # no /proc -> unknown


def test_funding_resolves_to_binance_usdm_funding_without_symlink(tmp_path: Path) -> None:
    """A raw per-venue root that stores funding as binance_usdm_funding is read as
    canonical `funding` with no symlink/rename — read_dataset auto-resolves it."""
    from liquidity_migration.storage import resolve_dataset_name

    rows = pl.DataFrame(
        {"ts_ms": [1, 2], "symbol": ["BTCUSDT", "ETHUSDT"], "funding_rate": [0.0001, -0.0002]}
    )
    write_dataset(rows, tmp_path, "binance_usdm_funding", partition_by=())

    # canonical 'funding' dir is absent, so the request resolves to the venue variant
    assert resolve_dataset_name(tmp_path, "funding") == "binance_usdm_funding"
    got = read_dataset(tmp_path, "funding")
    assert got.height == 2
    assert set(got["symbol"].to_list()) == {"BTCUSDT", "ETHUSDT"}


def test_funding_canonical_takes_precedence_over_fallback(tmp_path: Path) -> None:
    """When a root has the canonical `funding` dir, it is used as-is (the fallback
    only fires when canonical is absent), so existing roots are unaffected."""
    from liquidity_migration.storage import resolve_dataset_name

    canonical = pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.001]})
    write_dataset(canonical, tmp_path, "funding", partition_by=())
    assert resolve_dataset_name(tmp_path, "funding") == "funding"
    got = read_dataset(tmp_path, "funding")
    assert got["funding_rate"].to_list() == [0.001]


def test_missing_funding_everywhere_returns_empty(tmp_path: Path) -> None:
    """No canonical and no venue-variant funding -> empty frame, name unchanged."""
    from liquidity_migration.storage import resolve_dataset_name

    assert resolve_dataset_name(tmp_path, "funding") == "funding"
    assert read_dataset(tmp_path, "funding").is_empty()


# --- audit bucket b10 (relocated from test_audit_fix_b10.py) -----------------
# pit-data-6 / storage-concurrency-2 / -4 / -5: funding-fallback gating, the
# reused-pid stale singleton-lock eviction, orphaned `.*.tmp` sweeping, and the
# parent-dir fsync after the atomic rename. Each FAILs on the original bug.
import threading  # noqa: E402

from liquidity_migration import storage  # noqa: E402
from liquidity_migration.storage import resolve_dataset_name as _b10_resolve_dataset_name  # noqa: E402


def test_canonical_klines_root_with_binance_variant_resolves_to_binance_funding(tmp_path: Path) -> None:
    """A Binance full-PIT root stores klines under the canonical ``klines_1h/`` name
    AND its funding as ``binance_usdm_funding/`` with NO canonical ``funding/``.
    resolve_dataset_name must use the present venue-variant (a binance_usdm_* dir only
    ever exists on a Binance root), matching _autodetect_dataset_names. The old code
    suppressed this on ANY root carrying a canonical kline marker and silently returned
    empty funding -> funding_mode missing on a fully populated dataset (the 2026-06-15
    resolver regression)."""
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "close": [1.0]}),
        tmp_path,
        "klines_1h",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.0009]}),
        tmp_path,
        "binance_usdm_funding",
        partition_by=(),
    )
    assert _b10_resolve_dataset_name(tmp_path, "funding") == "binance_usdm_funding"
    assert read_dataset(tmp_path, "funding").height == 1


def test_pitdata6_bybit_root_canonical_funding_wins_over_binance_proxy(tmp_path: Path) -> None:
    """pit-data-6 safety, preserved by canonical-precedence: a real Bybit root ALWAYS
    has its own canonical ``funding/`` dir, which takes precedence over any
    ``binance_usdm_*`` PROXY dataset on the same root — the wrong-venue curve is never
    served. (A real Bybit root never lacks canonical funding/, so the variant fallback
    cannot mis-substitute; the only roots without canonical funding/ are Binance-native
    roots, which SHOULD use the variant.)"""
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "close": [1.0]}),
        tmp_path,
        "klines_1h",
        partition_by=(),
    )
    write_dataset(  # bybit's own native funding
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.0001]}),
        tmp_path,
        "funding",
        partition_by=(),
    )
    write_dataset(  # a binance PROXY dataset on the same root
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.0009]}),
        tmp_path,
        "binance_usdm_funding",
        partition_by=(),
    )
    assert _b10_resolve_dataset_name(tmp_path, "funding") == "funding"
    assert read_dataset(tmp_path, "funding")["funding_rate"].to_list() == [0.0001]


def test_pitdata6_pure_binance_root_still_resolves_fallback(tmp_path: Path) -> None:
    """The intended pure-Binance per-venue root (NO Bybit-native kline marker)
    still transparently resolves canonical funding -> binance_usdm_funding."""
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.0009]}),
        tmp_path,
        "binance_usdm_funding",
        partition_by=(),
    )
    # No klines_1h/ etc. -> unambiguously a Binance root -> fallback fires.
    assert _b10_resolve_dataset_name(tmp_path, "funding") == "binance_usdm_funding"
    assert read_dataset(tmp_path, "funding").height == 1


def test_storageconcurrency2_reused_pid_stale_lock_is_dead(tmp_path: Path) -> None:
    """A crashed daemon leaves a VALID-JSON lock bearing its pid + token. If a
    fast restart reuses the SAME pid, the successor reads pid==os.getpid() and,
    on a stale_seconds=0 singleton lock, would wedge forever (the age-eviction
    and None-self-heal paths are both disabled). The per-acquisition token is the
    tiebreaker: a token NOT in this process's live-owned set means the lock is a
    predecessor that merely reused our pid -> the owner is dead. Before the fix
    _lock_owner_is_dead short-circuited to False (alive) on pid==getpid()."""
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Plant a readable, valid-JSON lock with OUR pid but a foreign token (a dead
    # predecessor that reused our pid). _read_lock_text_safe returns the real text
    # here (NOT monkeypatched), so the None self-heal path cannot fire.
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "created": time.time(), "token": "deadbeef" * 4}),
        encoding="utf-8",
    )
    assert storage._lock_owner_is_dead(lock_path) is True

    # A live-owned token (one we currently hold) must still read as ALIVE.
    live_token = os.urandom(16).hex()
    storage._register_owned_token(live_token)
    try:
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "created": time.time(), "token": live_token}),
            encoding="utf-8",
        )
        assert storage._lock_owner_is_dead(lock_path) is False
    finally:
        storage._unregister_owned_token(live_token)

    # A legacy payload with no token field is conservatively treated as our own
    # live lock (unchanged behaviour).
    lock_path.write_text(json.dumps({"pid": os.getpid(), "created": time.time()}), encoding="utf-8")
    assert storage._lock_owner_is_dead(lock_path) is False


def test_storageconcurrency2_acquire_recovers_over_reused_pid_lock(tmp_path: Path) -> None:
    """End-to-end: exclusive_file_lock with stale_seconds=0 (the singleton-guard
    config) must ACQUIRE over a reused-pid foreign-token lock instead of wedging.
    Run in a thread with a join timeout so a regression cannot hang the suite."""
    lock_path = dataset_lock_path(tmp_path, "funding")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "created": time.time(), "token": "f00dface" * 4}),
        encoding="utf-8",
    )

    acquired = threading.Event()

    def _acquire() -> None:
        with exclusive_file_lock(lock_path, stale_seconds=0, poll_seconds=0.0):
            acquired.set()

    worker = threading.Thread(target=_acquire, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert acquired.is_set(), "acquire wedged on a reused-pid stale singleton lock"
    # Lock is released (our acquisition owned it, foreign token replaced on write).
    assert not lock_path.exists()


def test_storageconcurrency4_orphaned_tmp_swept_on_next_write(tmp_path: Path) -> None:
    """A SIGKILL between write_parquet and the atomic rename orphans a
    `.{name}.{pid}.{ns}.tmp` file (the finally unlink never runs). The next
    write_dataset must sweep stale `.*.tmp` files so a Restart=always daemon does
    not accumulate orphans until the disk fills. Before the fix there was no sweep
    anywhere, so the orphan survived every subsequent write."""
    dataset = "event_demo_orders"
    # Seed the dataset so its dir exists.
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "order_id": ["a"]}),
        tmp_path,
        dataset,
        partition_by=(),
    )
    dataset_dir = tmp_path / dataset
    # Simulate an orphaned temp left by a crash mid-write, aged past the threshold.
    orphan = dataset_dir / f".part.parquet.{os.getpid()}.123456789.tmp"
    orphan.write_bytes(b"truncated parquet fragment")
    old = time.time() - storage._STALE_TMP_SECONDS - 60
    os.utime(orphan, (old, old))
    # A FRESH temp (in-flight) must be preserved by the age gate.
    fresh = dataset_dir / f".part.parquet.{os.getpid()}.987654321.tmp"
    fresh.write_bytes(b"in-flight")

    # The next write sweeps stale temps (it holds the dataset lock).
    write_dataset(
        pl.DataFrame({"ts_ms": [2], "symbol": ["ETHUSDT"], "order_id": ["b"]}),
        tmp_path,
        dataset,
        partition_by=(),
    )

    assert not orphan.exists(), "stale orphaned .tmp should have been swept"
    assert fresh.exists(), "a fresh in-flight .tmp must NOT be swept"
    # The real data is intact and readers never saw the orphan.
    got = read_dataset(tmp_path, dataset)
    assert set(got["order_id"].to_list()) == {"a", "b"}


def test_storageconcurrency4_full_tmp_sweep_is_throttled(tmp_path: Path, monkeypatch) -> None:
    """The broad orphan-temp sweep must not recursively walk a large dataset on
    every partition write; the target partition directory is still checked every
    time so same-partition orphans are cleaned promptly."""
    storage._DATASET_TMP_SWEEP_LAST.clear()
    real_sweep = storage._sweep_orphaned_tmp_parts
    full_sweeps: list[Path] = []
    local_sweeps: list[Path] = []

    def recording_sweep(
        path: Path,
        *,
        stale_seconds: float = storage._STALE_TMP_SECONDS,
        recursive: bool = True,
    ) -> None:
        if recursive:
            full_sweeps.append(path)
        else:
            local_sweeps.append(path)
        real_sweep(path, stale_seconds=stale_seconds, recursive=recursive)

    monkeypatch.setattr(storage, "_sweep_orphaned_tmp_parts", recording_sweep)

    write_dataset(
        pl.DataFrame({"ts_ms": [1_704_067_200_000], "symbol": ["BTCUSDT"], "close": [1.0]}),
        tmp_path,
        "klines_1h",
    )
    write_dataset(
        pl.DataFrame({"ts_ms": [1_704_070_800_000], "symbol": ["ETHUSDT"], "close": [2.0]}),
        tmp_path,
        "klines_1h",
    )

    assert len(full_sweeps) == 1
    assert len(local_sweeps) >= 2
    storage._DATASET_TMP_SWEEP_LAST.clear()


def test_storageconcurrency5_parent_dir_fsynced_after_rename(tmp_path: Path, monkeypatch) -> None:
    """_write_part must fsync the PARENT DIRECTORY fd after temp_path.replace(path),
    not just the temp file — otherwise the rename is not power-loss durable. Pin it
    by recording every os.fsync target and asserting a directory fd is fsync'd.
    Before the fix only the temp file was fsync'd."""
    import stat as stat_module

    fsynced_kinds: list[str] = []
    real_fsync = os.fsync
    real_fstat = os.fstat

    def recording_fsync(fd: int) -> None:
        try:
            mode = real_fstat(fd).st_mode
            fsynced_kinds.append("dir" if stat_module.S_ISDIR(mode) else "file")
        except OSError:
            fsynced_kinds.append("unknown")
        real_fsync(fd)

    monkeypatch.setattr(storage.os, "fsync", recording_fsync)

    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "close": [1.0]}),
        tmp_path,
        "klines_1h",
        partition_by=(),
    )

    assert "file" in fsynced_kinds, "the temp part file must still be fsync'd"
    if os.name == "nt" and "dir" not in fsynced_kinds:
        pytest.skip("Windows does not expose a fsync-able directory fd via os.open")
    assert "dir" in fsynced_kinds, "the parent directory must be fsync'd after the rename"
