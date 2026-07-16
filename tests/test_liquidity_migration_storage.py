from __future__ import annotations

import json
import os
import time
from pathlib import Path

import polars as pl

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


def test_continuous_cycle_datasets_registered_and_roundtrip(tmp_path: Path) -> None:
    from liquidity_migration.continuous_demo import (
        ContinuousDemoCycleConfig,
        continuous_cycles_dataset,
    )
    from liquidity_migration.storage import DATASET_KEYS, DATASETS

    datasets = {
        continuous_cycles_dataset(ContinuousDemoCycleConfig(execution_environment="demo")),
        continuous_cycles_dataset(ContinuousDemoCycleConfig(execution_environment="paper")),
    }
    for dataset in datasets:
        assert dataset in DATASETS
        assert DATASET_KEYS[dataset] == ("cycle_id",)
        row = pl.DataFrame([{"cycle_id": "k1", "ts_ms": 1_700_000_000_000, "v": 1}])
        write_dataset(row, tmp_path, dataset, partition_by=())
        write_dataset(row.with_columns(pl.lit(2).alias("v")), tmp_path, dataset, partition_by=())
        stored = read_dataset(tmp_path, dataset)
        assert stored.height == 1 and stored["v"][0] == 2


def test_read_dataset_handles_schema_evolution_across_partitions(tmp_path: Path) -> None:
    first = pl.DataFrame(
        [
            {
                "ts_ms": 1_700_000_000_000,
                "symbol": "BTCUSDT",
                "funding_rate": 0.001,
            }
        ]
    )
    second = pl.DataFrame(
        [
            {
                "ts_ms": 1_700_086_400_000,
                "symbol": "ETHUSDT",
                "funding_rate": 0.002,
                "mark_price": 99.5,
            }
        ]
    )

    write_dataset(first, tmp_path, "funding")
    write_dataset(second, tmp_path, "funding")

    stored = read_dataset(tmp_path, "funding")

    assert stored.height == 2
    assert "mark_price" in stored.columns
    assert stored.filter(pl.col("symbol") == "BTCUSDT").row(0, named=True)["mark_price"] is None
    assert stored.filter(pl.col("symbol") == "ETHUSDT").row(0, named=True)["mark_price"] == 99.5


def test_exclusive_file_lock_cleans_up_lock_file(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.exists()

    assert not lock_path.exists()


def test_exclusive_file_lock_recovers_dead_pid_lock_even_without_stale_timeout(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 2_147_483_647, "created": 1}), encoding="utf-8")

    with exclusive_file_lock(
        lock_path,
        stale_seconds=0,
        poll_seconds=0.0,
        invalid_lock_stale_seconds=0.0,
    ):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] != 2_147_483_647

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


def test_concurrent_cycle_writers_do_not_lose_or_tear_rows(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    rows_per_writer = 10
    writer_count = 4

    def write_batch(writer_id: int) -> None:
        batch = pl.DataFrame(
            [
                {
                    "cycle_id": f"cycle-w{writer_id}-r{i}",
                    "ts_ms": 1_700_000_000_000 + writer_id * 1000 + i,
                }
                for i in range(rows_per_writer)
            ]
        )
        write_dataset(batch, tmp_path, "continuous_fade_demo_cycles", partition_by=())

    with ThreadPoolExecutor(max_workers=writer_count) as executor:
        futures = [executor.submit(write_batch, writer) for writer in range(writer_count)]
        for _ in range(20):
            stored = read_dataset(tmp_path, "continuous_fade_demo_cycles")
            if not stored.is_empty():
                assert stored.height % rows_per_writer == 0
                assert stored.select("cycle_id").n_unique() == stored.height
            time.sleep(0.005)
        for future in futures:
            future.result()
    stored = read_dataset(tmp_path, "continuous_fade_demo_cycles")
    assert stored.height == writer_count * rows_per_writer


from liquidity_migration.storage import _LEDGER_MONTH_COL  # noqa: E402

_MS_PER_DAY = 86_400_000


def _cycle(cycle_id: str, ts_ms: int, **over) -> dict:
    row = {
        "cycle_id": cycle_id,
        "ts_ms": ts_ms,
    }
    row.update(over)
    return row


def test_cycles_write_month_buckets_without_leaking_partition_column(tmp_path: Path) -> None:
    jan = 1_704_067_200_000  # 2024-01-01 UTC
    mar = jan + 70 * _MS_PER_DAY  # ~2024-03
    dataset = "continuous_fade_demo_cycles"
    write_dataset(pl.DataFrame([_cycle("c-jan", jan)]), tmp_path, dataset, partition_by=())
    write_dataset(pl.DataFrame([_cycle("c-mar", mar)]), tmp_path, dataset, partition_by=())
    root = dataset_path(tmp_path, dataset)
    buckets = sorted(p.name for p in root.glob(f"{_LEDGER_MONTH_COL}=*"))
    assert buckets == [f"{_LEDGER_MONTH_COL}=202401", f"{_LEDGER_MONTH_COL}=202403"]
    out = read_dataset(tmp_path, dataset)
    assert _LEDGER_MONTH_COL not in out.columns
    assert set(out["cycle_id"].to_list()) == {"c-jan", "c-mar"}


def test_cycle_schema_drift_across_buckets_unions(tmp_path: Path) -> None:
    jan = 1_704_067_200_000
    mar = jan + 70 * _MS_PER_DAY
    dataset = "continuous_fade_demo_cycles"
    write_dataset(pl.DataFrame([_cycle("c-jan", jan)]), tmp_path, dataset, partition_by=())
    write_dataset(
        pl.DataFrame([_cycle("c-mar", mar, candidate_count=3)]),
        tmp_path, dataset, partition_by=(),
    )
    out = read_dataset(tmp_path, dataset)
    assert set(out["cycle_id"].to_list()) == {"c-jan", "c-mar"}
    assert "candidate_count" in out.columns


def test_non_ledger_dataset_is_not_month_bucketed(tmp_path: Path) -> None:
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
    An unknown start time (non-Linux / no /proc) stays conservative (owner alive)."""
    import subprocess
    import sys

    from liquidity_migration import storage

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
    try:
        lock = tmp_path / "y.lock"
        lock.write_text(
            json.dumps(
                {
                    "pid": proc.pid,
                    "created": time.time() - 100,
                    "token": "0123456789abcdef" * 2,
                }
            ),
            encoding="utf-8",
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

    # Tokenless payloads are invalid and must age through the explicit invalid-
    # lock grace path rather than being adopted as current ownership evidence.
    lock_path.write_text(json.dumps({"pid": os.getpid(), "created": time.time()}), encoding="utf-8")
    assert storage._lock_owner_is_dead(lock_path) is False
    assert storage._lock_payload_is_invalid(lock_path) is True


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
    dataset = "funding"
    # Seed the dataset so its dir exists.
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.001]}),
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
        pl.DataFrame({"ts_ms": [2], "symbol": ["ETHUSDT"], "funding_rate": [0.002]}),
        tmp_path,
        dataset,
        partition_by=(),
    )

    assert not orphan.exists(), "stale orphaned .tmp should have been swept"
    assert fresh.exists(), "a fresh in-flight .tmp must NOT be swept"
    # The real data is intact and readers never saw the orphan.
    got = read_dataset(tmp_path, dataset)
    assert set(got["symbol"].to_list()) == {"BTCUSDT", "ETHUSDT"}


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
    assert "dir" in fsynced_kinds, "the parent directory must be fsync'd after the rename"
