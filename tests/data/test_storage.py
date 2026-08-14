from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.data import storage
from liquidity_migration.core.symbol_codec import encode_symbol_partition
from liquidity_migration.data.storage import dataset_lock_path, dataset_path, exclusive_file_lock, read_dataset, write_dataset


def _hold_exclusive_file_lock(path: str, acquired, release) -> None:
    with exclusive_file_lock(path, stale_seconds=600, poll_seconds=0.005):
        acquired.set()
        if not release.wait(timeout=10.0):
            raise RuntimeError("timed out waiting to release test lock")


def _hold_lock_until_abrupt_exit(path: str, acquired, exit_now) -> None:
    with exclusive_file_lock(path, poll_seconds=0.005):
        acquired.set()
        if not exit_now.wait(timeout=10.0):
            raise RuntimeError("timed out waiting for abrupt lock-holder exit")
        os._exit(17)


def _flock_stress_worker(path: str, barrier, active, overlaps, iterations: int) -> None:
    for _ in range(iterations):
        barrier.wait(timeout=10.0)
        with exclusive_file_lock(path, poll_seconds=0.0005):
            with active.get_lock():
                if active.value:
                    overlaps.value += 1
                active.value += 1
            time.sleep(0.0005)
            with active.get_lock():
                active.value -= 1
        barrier.wait(timeout=10.0)


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


def test_unicode_symbol_partition_is_canonical_and_round_trips(tmp_path: Path) -> None:
    symbol = "\u5e01\u5b89\u4eba\u751fUSDT"
    frame = pl.DataFrame(
        [{"ts_ms": 1_704_067_200_000, "symbol": symbol, "funding_rate": 0.001}]
    )

    write_dataset(frame, tmp_path, "funding")

    encoded = encode_symbol_partition(symbol)
    part = tmp_path / "funding" / "date=2024-01-01" / f"symbol={encoded}" / "part.parquet"
    assert part.is_file()
    assert read_dataset(tmp_path, "funding")["symbol"].to_list() == [symbol]


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


def _evolved_funding_root(tmp_path: Path) -> Path:
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "funding_rate": 0.001}]),
        tmp_path,
        "funding",
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "ts_ms": 1_700_086_400_000,
                    "symbol": "ETHUSDT",
                    "funding_rate": 0.002,
                    "funding_event_kind": "settlement",
                }
            ]
        ),
        tmp_path,
        "funding",
    )
    return tmp_path


def test_evolved_schemas_are_unioned_in_one_scan_not_read_file_by_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Bybit funding root is 600k parts; per-file reads cost ~158s against ~59s.

    Declaring the union of the on-disk schemas keeps the whole thing on one
    scan. Failing the per-file call outright is the only way to prove the fast
    path is the one taken.
    """

    root = _evolved_funding_root(tmp_path)

    def refuse(*args: object, **kwargs: object) -> pl.DataFrame:
        raise AssertionError("per-file read is the last resort, not the schema-drift path")

    monkeypatch.setattr(pl, "read_parquet", refuse)
    stored = read_dataset(root, "funding")

    assert stored.height == 2
    assert stored.filter(pl.col("symbol") == "BTCUSDT").row(0, named=True)["funding_event_kind"] is None
    assert (
        stored.filter(pl.col("symbol") == "ETHUSDT").row(0, named=True)["funding_event_kind"]
        == "settlement"
    )


def test_evolved_schemas_honour_a_column_projection(tmp_path: Path) -> None:
    root = _evolved_funding_root(tmp_path)

    stored = storage.read_dataset_columns(
        root, "funding", columns=["symbol", "funding_event_kind"]
    )

    assert stored.columns == ["symbol", "funding_event_kind"]
    assert sorted(stored.get_column("symbol").to_list()) == ["BTCUSDT", "ETHUSDT"]
    assert set(stored.get_column("funding_event_kind").to_list()) == {"settlement", None}


def test_an_all_null_column_does_not_poison_the_declared_union(tmp_path: Path) -> None:
    """Polars writes an all-null column as dtype Null.

    First-wins over the sorted paths would declare Null for the whole scan, and
    every part holding a real value then mismatches — dropping a 600k-part read
    back to the per-file path with no signal at all, 59s to 158s.
    """

    write_dataset(
        pl.DataFrame(
            [{"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "funding_rate": 0.001}],
        ).with_columns(pl.lit(None).alias("funding_event_kind")),
        tmp_path,
        "funding",
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "ts_ms": 1_700_086_400_000,
                    "symbol": "ETHUSDT",
                    "funding_rate": 0.002,
                    "funding_event_kind": "settlement",
                }
            ]
        ),
        tmp_path,
        "funding",
    )

    def refuse(*args: object, **kwargs: object) -> pl.DataFrame:
        raise AssertionError("an all-null part must not force the per-file path")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pl, "read_parquet", refuse)
        stored = read_dataset(tmp_path, "funding")

    assert stored.height == 2
    assert set(stored.get_column("funding_event_kind").to_list()) == {"settlement", None}


def test_conflicting_dtypes_across_partitions_still_read_file_by_file(tmp_path: Path) -> None:
    """A union cannot describe one column carrying two types; that case keeps its fallback."""

    write_dataset(
        pl.DataFrame([{"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "funding_rate": 1}]),
        tmp_path,
        "funding",
    )
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_700_086_400_000, "symbol": "ETHUSDT", "funding_rate": 0.002}]),
        tmp_path,
        "funding",
    )

    stored = read_dataset(tmp_path, "funding")

    assert stored.height == 2
    assert sorted(stored.get_column("funding_rate").to_list()) == [0.002, 1.0]


def test_exclusive_file_lock_persists_same_single_link_inode(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.exists()
        first = lock_path.stat()

    assert lock_path.exists()
    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        second = lock_path.stat()

    assert (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)
    assert second.st_nlink == 1
    assert stat.S_IMODE(second.st_mode) == 0o600


def test_exclusive_file_lock_adopts_legacy_payload_without_unlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = json.dumps({"pid": 2_147_483_647, "created": 1})
    lock_path.write_text(legacy, encoding="utf-8")
    inode = lock_path.stat().st_ino
    real_unlink = Path.unlink

    def reject_lock_unlink(path: Path, *args, **kwargs) -> None:
        if path == lock_path:
            raise AssertionError("persistent lock acquisition must not unlink")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_lock_unlink)

    with exclusive_file_lock(
        lock_path,
        stale_seconds=0,
        poll_seconds=0.0,
    ):
        assert lock_path.read_text(encoding="utf-8") == legacy

    assert lock_path.stat().st_ino == inode


def test_thread_lock_for_returns_same_lock_per_path() -> None:
    """The per-process thread-lock layer must return a stable Lock object per lock-path,
    or two threads on the same dataset grab different Locks and do not serialise.
    """
    from liquidity_migration.data.storage import _thread_lock_for

    path_a = Path("/tmp/sweep_test_thread_lock_a.lock")
    path_b = Path("/tmp/sweep_test_thread_lock_b.lock")
    assert _thread_lock_for(path_a) is _thread_lock_for(path_a)
    assert _thread_lock_for(path_a) is not _thread_lock_for(path_b)


def test_exclusive_file_lock_never_unlinks_malformed_legacy_leaf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    old_ts = time.time() - 10.0
    os.utime(lock_path, (old_ts, old_ts))
    inode = lock_path.stat().st_ino
    real_unlink = Path.unlink

    def reject_lock_unlink(path: Path, *args, **kwargs) -> None:
        if path == lock_path:
            raise AssertionError("malformed legacy content is not lock ownership")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_lock_unlink)

    with exclusive_file_lock(
        lock_path,
        stale_seconds=0,
        poll_seconds=0.0,
    ):
        assert lock_path.read_bytes() == b""

    assert lock_path.stat().st_ino == inode


def test_exclusive_file_lock_never_age_evicts_live_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "live-owner.lock"
    context = multiprocessing.get_context("spawn")
    holder_acquired = context.Event()
    release_holder = context.Event()
    holder = context.Process(
        target=_hold_exclusive_file_lock,
        args=(str(lock_path), holder_acquired, release_holder),
    )
    contender_started = threading.Event()
    contender_entered = threading.Event()
    contender_errors: list[BaseException] = []

    def contend() -> None:
        contender_started.set()
        try:
            with exclusive_file_lock(lock_path, stale_seconds=0.05, poll_seconds=0.005):
                contender_entered.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            contender_errors.append(exc)

    contender = threading.Thread(target=contend, daemon=True)
    overlapped = False
    holder.start()
    try:
        assert holder_acquired.wait(timeout=5.0)
        old_ts = time.time() - 10.0
        os.utime(lock_path, (old_ts, old_ts))
        contender.start()
        assert contender_started.wait(timeout=1.0)
        overlapped = contender_entered.wait(timeout=0.25)
    finally:
        release_holder.set()
        holder.join(timeout=5.0)
        if contender.ident is not None:
            contender.join(timeout=5.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)

    assert holder.exitcode == 0
    assert not contender.is_alive()
    assert not contender_errors
    assert contender_entered.is_set()
    assert not overlapped, "a valid live-owner lock was evicted solely because of age"


def test_exclusive_file_lock_multiprocess_stress_has_no_overlap(tmp_path: Path) -> None:
    lock_path = tmp_path / "stress.lock"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    active = context.Value("i", 0)
    overlaps = context.Value("i", 0)
    iterations = 100
    workers = [
        context.Process(
            target=_flock_stress_worker,
            args=(str(lock_path), barrier, active, overlaps, iterations),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15.0)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5.0)

    assert [worker.exitcode for worker in workers] == [0, 0]
    assert overlaps.value == 0
    assert active.value == 0


def test_exclusive_file_lock_reopens_if_path_changes_after_flock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = tmp_path / "replaced.lock"
    lock_path.touch(mode=0o600)
    real_flock = storage.fcntl.flock
    calls = 0

    def replace_after_first_flock(fd: int, operation: int) -> None:
        nonlocal calls
        real_flock(fd, operation)
        calls += 1
        if calls == 1:
            lock_path.unlink()
            lock_path.touch(mode=0o600)

    monkeypatch.setattr(storage.fcntl, "flock", replace_after_first_flock)

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert calls == 2
        assert lock_path.stat().st_nlink == 1

    assert lock_path.exists()


def test_lock_owner_policy_allows_root_only_for_directory_owner() -> None:
    paper_uid = 991

    assert storage._lock_owner_allowed(paper_uid, effective_uid=paper_uid)
    assert storage._lock_owner_allowed(paper_uid, effective_uid=0)
    assert not storage._lock_owner_allowed(paper_uid, effective_uid=992)


def test_root_first_lock_creation_inherits_lock_directory_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = tmp_path / ".locks" / "root-first.lock"
    lock_path.parent.mkdir(mode=0o700)
    expected = lock_path.parent.stat()
    real_fchown = os.fchown
    ownership_calls: list[tuple[int, int]] = []

    def observe_fchown(fd: int, uid: int, gid: int) -> None:
        ownership_calls.append((uid, gid))
        real_fchown(fd, uid, gid)

    monkeypatch.setattr(storage.os, "geteuid", lambda: 0)
    monkeypatch.setattr(storage.os, "fchown", observe_fchown)

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        actual = lock_path.stat()
        assert (actual.st_uid, actual.st_gid) == (expected.st_uid, expected.st_gid)

    assert (expected.st_uid, expected.st_gid) in ownership_calls


def test_root_bootstrap_of_dataset_lock_directory_inherits_root_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_root = tmp_path / "paper-root"
    data_root.mkdir(mode=0o700)
    expected = data_root.stat()
    lock_path = data_root / ".locks" / "dataset.lock"
    real_fchown = os.fchown
    ownership_calls: list[tuple[int, int]] = []

    def observe_fchown(fd: int, uid: int, gid: int) -> None:
        ownership_calls.append((uid, gid))
        real_fchown(fd, uid, gid)

    monkeypatch.setattr(storage.os, "geteuid", lambda: 0)
    monkeypatch.setattr(storage.os, "fchown", observe_fchown)

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        directory = lock_path.parent.stat()
        leaf = lock_path.stat()
        assert (directory.st_uid, directory.st_gid) == (expected.st_uid, expected.st_gid)
        assert leaf.st_uid == directory.st_uid

    assert (expected.st_uid, expected.st_gid) in ownership_calls


def test_exclusive_file_lock_rejects_symlinked_lock_directory(tmp_path: Path) -> None:
    real_directory = tmp_path / "real-locks"
    real_directory.mkdir(mode=0o700)
    symlinked_directory = tmp_path / ".locks"
    symlinked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(RuntimeError, match="lock directory"):
        with exclusive_file_lock(symlinked_directory / "unsafe.lock", poll_seconds=0.0):
            pytest.fail("symlinked lock directory was admitted")


def test_exclusive_file_lock_rejects_writable_lock_directory(tmp_path: Path) -> None:
    lock_directory = tmp_path / ".locks"
    lock_directory.mkdir(mode=0o700)
    lock_directory.chmod(0o770)

    with pytest.raises(RuntimeError, match="must not be group/world writable"):
        with exclusive_file_lock(lock_directory / "unsafe.lock", poll_seconds=0.0):
            pytest.fail("group-writable lock directory was admitted")


def test_exclusive_file_lock_reopens_if_directory_changes_after_flock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_directory = tmp_path / ".locks"
    lock_directory.mkdir(mode=0o700)
    lock_path = lock_directory / "directory-race.lock"
    lock_path.touch(mode=0o600)
    retired_directory = tmp_path / "retired-locks"
    real_flock = storage.fcntl.flock
    calls = 0

    def replace_directory_after_first_flock(fd: int, operation: int) -> None:
        nonlocal calls
        real_flock(fd, operation)
        calls += 1
        if calls == 1:
            lock_directory.rename(retired_directory)
            lock_directory.mkdir(mode=0o700)
            lock_path.touch(mode=0o600)

    monkeypatch.setattr(storage.fcntl, "flock", replace_directory_after_first_flock)

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert calls == 2
        assert lock_path.parent.samefile(lock_directory)

    assert (retired_directory / lock_path.name).exists()


def test_exclusive_file_lock_recovers_internal_alias_left_after_publication(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "recover.lock"
    alias = tmp_path / f".{lock_path.name}.create-{'a' * 32}"
    alias.touch(mode=0o600)
    os.link(alias, lock_path)

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.stat().st_nlink == 1
        assert not alias.exists()


def test_exclusive_file_lock_cleans_old_unpublished_internal_alias(tmp_path: Path) -> None:
    lock_path = tmp_path / "orphan.lock"
    alias = tmp_path / f".{lock_path.name}.create-{'b' * 32}"
    alias.touch(mode=0o600)
    old_ts = time.time() - storage._LOCK_CREATE_ORPHAN_SECONDS - 1.0
    os.utime(alias, (old_ts, old_ts))

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.exists()

    assert not alias.exists()


def test_old_unpublished_alias_cleanup_does_not_trust_creator_uid(tmp_path: Path) -> None:
    alias = tmp_path / f".lock.create-{'c' * 32}"
    alias.touch(mode=0o600)
    old_ts = time.time() - storage._LOCK_CREATE_ORPHAN_SECONDS - 1.0
    os.utime(alias, (old_ts, old_ts))
    values = list(alias.stat())
    values[4] = os.geteuid() + 1
    foreign_owner = os.stat_result(values)

    assert storage._lock_create_orphan_removable(foreign_owner, cutoff=time.time())


def test_old_unpublished_alias_is_cleaned_when_canonical_already_exists(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "canonical.lock"
    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        pass
    alias = tmp_path / f".{lock_path.name}.create-{'d' * 32}"
    alias.touch(mode=0o600)
    old_ts = time.time() - storage._LOCK_CREATE_ORPHAN_SECONDS - 1.0
    os.utime(alias, (old_ts, old_ts))

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.exists()

    assert not alias.exists()


def test_fresh_unpublished_alias_is_swept_when_cached_expiry_arrives(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage._LOCK_CREATE_SWEEP_CACHE.clear()
    lock_path = tmp_path / "cached-expiry.lock"
    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        pass
    alias = tmp_path / f".{lock_path.name}.create-{'e' * 32}"
    alias.touch(mode=0o600)
    base = time.time()
    os.utime(alias, (base, base))
    monkeypatch.setattr(storage.time, "time", lambda: base)

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert alias.exists()
    directory_signature = lock_path.parent.stat().st_mtime_ns

    monkeypatch.setattr(
        storage.time,
        "time",
        lambda: base + storage._LOCK_CREATE_ORPHAN_SECONDS + 1.0,
    )
    assert lock_path.parent.stat().st_mtime_ns == directory_signature
    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        pass

    assert not alias.exists()


def test_concurrent_orphan_sweep_missing_name_is_benign(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = tmp_path / "sweep-race.lock"
    alias = tmp_path / f".{lock_path.name}.create-{'f' * 32}"
    alias.touch(mode=0o600)
    old_ts = time.time() - storage._LOCK_CREATE_ORPHAN_SECONDS - 1.0
    os.utime(alias, (old_ts, old_ts))
    real_unlink = os.unlink
    raced = False

    def remove_then_report_missing(path, *, dir_fd=None) -> None:
        nonlocal raced
        if path == alias.name and dir_fd is not None and not raced:
            raced = True
            real_unlink(path, dir_fd=dir_fd)
            raise FileNotFoundError(path)
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(storage.os, "unlink", remove_then_report_missing)

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.exists()

    assert raced
    assert not alias.exists()


def test_exclusive_file_lock_fork_child_does_not_inherit_parent_mutex(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "fork.lock"
    source = f"""
import os
import select
import threading

from liquidity_migration.data.storage import exclusive_file_lock

lock_path = {str(lock_path)!r}
read_fd, write_fd = os.pipe()
child_status = []

def fork_and_wait():
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            with exclusive_file_lock(lock_path, poll_seconds=0.005):
                os.write(write_fd, b"1")
        except BaseException:
            os._exit(72)
        os._exit(0)
    os.close(write_fd)
    _, status = os.waitpid(pid, 0)
    child_status.append(os.waitstatus_to_exitcode(status))

with exclusive_file_lock(lock_path, poll_seconds=0.005):
    worker = threading.Thread(target=fork_and_wait)
    worker.start()
    ready, _, _ = select.select([read_fd], [], [], 0.2)
    if ready:
        raise SystemExit(70)

worker.join(timeout=5.0)
if worker.is_alive():
    raise SystemExit(71)
ready, _, _ = select.select([read_fd], [], [], 1.0)
if not ready or os.read(read_fd, 1) != b"1" or child_status != [0]:
    raise SystemExit(73)
"""
    result = subprocess.run(
        [sys.executable, "-W", "ignore::DeprecationWarning", "-c", source],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


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


from liquidity_migration.data.storage import _LEDGER_MONTH_COL  # noqa: E402
from liquidity_migration.data.storage import read_dataset_columns  # noqa: E402

_MS_PER_DAY = 86_400_000


def _cycle(cycle_id: str, ts_ms: int, **over) -> dict:
    row = {
        "cycle_id": cycle_id,
        "ts_ms": ts_ms,
    }
    row.update(over)
    return row


def _write_legacy_month_part(root: Path, dataset: str, row: dict, month: int) -> Path:
    """Hand-write a part in the retired `_ledger_month=` layout, as a root written
    before the day-bucket switch still holds."""
    part_dir = dataset_path(root, dataset) / f"{_LEDGER_MONTH_COL}={month}"
    part_dir.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame([{**row, _LEDGER_MONTH_COL: month}])
    part = part_dir / "part.parquet"
    frame.write_parquet(part)
    return part


def test_cycles_write_day_buckets_without_leaking_partition_column(tmp_path: Path) -> None:
    jan = 1_704_067_200_000  # 2024-01-01 UTC
    mar = jan + 70 * _MS_PER_DAY  # 2024-03-11
    dataset = "continuous_fade_demo_cycles"
    write_dataset(pl.DataFrame([_cycle("c-jan", jan)]), tmp_path, dataset, partition_by=())
    write_dataset(pl.DataFrame([_cycle("c-mar", mar)]), tmp_path, dataset, partition_by=())
    root = dataset_path(tmp_path, dataset)
    buckets = sorted(p.name for p in root.glob("date=*"))
    assert buckets == ["date=2024-01-01", "date=2024-03-11"]
    assert not (root / "part.parquet").exists()
    out = read_dataset(tmp_path, dataset)
    assert _LEDGER_MONTH_COL not in out.columns
    assert out.height == 2
    assert set(out["cycle_id"].to_list()) == {"c-jan", "c-mar"}


def test_day_bucket_write_lands_in_the_right_part(tmp_path: Path) -> None:
    day0 = 1_704_067_200_000  # 2024-01-01 UTC
    dataset = "carry_hold_demo_cycles"
    for offset in (0, 1, 2):
        write_dataset(
            pl.DataFrame([_cycle(f"c-{offset}", day0 + offset * _MS_PER_DAY)]),
            tmp_path,
            dataset,
            partition_by=(),
        )
    root = dataset_path(tmp_path, dataset)
    parts = {p.parent.name: pl.read_parquet(p) for p in root.glob("date=*/part.parquet")}
    assert sorted(parts) == ["date=2024-01-01", "date=2024-01-02", "date=2024-01-03"]
    assert [frame.height for frame in parts.values()] == [1, 1, 1]
    assert parts["date=2024-01-02"]["cycle_id"].to_list() == ["c-1"]


def test_append_rewrite_reads_only_the_current_day_part(tmp_path: Path) -> None:
    """The point of the day bucket: appending a row for day B must not rewrite
    day A's part, so per-append cost stays at one day of rows."""
    day_a = 1_704_067_200_000  # 2024-01-01 UTC
    day_b = day_a + _MS_PER_DAY
    dataset = "carry_hold_demo_cycles"
    for i in range(5):
        write_dataset(
            pl.DataFrame([_cycle(f"a-{i}", day_a + i * 60_000)]), tmp_path, dataset, partition_by=()
        )
    root = dataset_path(tmp_path, dataset)
    part_a = root / "date=2024-01-01" / "part.parquet"
    before = part_a.stat().st_mtime_ns

    write_dataset(pl.DataFrame([_cycle("b-0", day_b)]), tmp_path, dataset, partition_by=())

    assert part_a.stat().st_mtime_ns == before
    assert pl.read_parquet(part_a).height == 5
    assert pl.read_parquet(root / "date=2024-01-02" / "part.parquet").height == 1


def test_mainnet_carry_cycles_are_day_bucketed(tmp_path: Path) -> None:
    """An unregistered cycles dataset gets no bucket at all — one part.parquet
    rewritten in full every cycle, forever. The mainnet carry ledger must stay
    registered."""
    dataset = "carry_hold_mainnet_cycles"
    write_dataset(
        pl.DataFrame([_cycle("m-0", 1_704_067_200_000)]), tmp_path, dataset, partition_by=()
    )
    root = dataset_path(tmp_path, dataset)
    assert (root / "date=2024-01-01" / "part.parquet").exists()
    assert not (root / "part.parquet").exists()


def test_mixed_month_and_day_partitions_read_as_one_frame(tmp_path: Path) -> None:
    dataset = "continuous_fade_demo_cycles"
    aug = 1_754_006_400_000  # 2025-08-01 UTC
    _write_legacy_month_part(
        tmp_path, dataset, {"cycle_id": "old", "ts_ms": aug, "date": "2025-08-01"}, 202508
    )
    write_dataset(
        pl.DataFrame([_cycle("new", aug + _MS_PER_DAY)]), tmp_path, dataset, partition_by=()
    )

    out = read_dataset(tmp_path, dataset)
    assert set(out["cycle_id"].to_list()) == {"old", "new"}
    assert _LEDGER_MONTH_COL not in out.columns

    projected = read_dataset_columns(tmp_path, dataset, columns=["cycle_id", "ts_ms"])
    assert set(projected["cycle_id"].to_list()) == {"old", "new"}
    assert projected.columns == ["cycle_id", "ts_ms"]


def test_since_date_read_keeps_parts_outside_the_date_tree(tmp_path: Path) -> None:
    """A since_date read prunes by directory name. Parts outside the date= tree
    carry no date in their path, so they are not prunable and must still be
    read — dropping them silently loses the pre-migration history."""
    dataset = "continuous_fade_demo_cycles"
    aug = 1_754_006_400_000  # 2025-08-01 UTC
    _write_legacy_month_part(
        tmp_path, dataset, {"cycle_id": "old", "ts_ms": aug, "date": "2025-08-01"}, 202508
    )
    write_dataset(
        pl.DataFrame([_cycle("new", aug + _MS_PER_DAY)]), tmp_path, dataset, partition_by=()
    )
    root = dataset_path(tmp_path, dataset)
    # The pruning fast path only runs when a top-level date= dir exists.
    assert list(root.glob("date=*"))
    assert list(root.glob(f"{_LEDGER_MONTH_COL}=*"))

    out = read_dataset_columns(tmp_path, dataset, since_date="2025-08-02")
    assert set(out["cycle_id"].to_list()) == {"old", "new"}


def test_malformed_ledger_ts_lands_in_unknown_bucket(tmp_path: Path) -> None:
    dataset = "carry_hold_demo_cycles"
    write_dataset(pl.DataFrame([_cycle("bad", 0)]), tmp_path, dataset, partition_by=())
    write_dataset(
        pl.DataFrame([_cycle("good", 1_704_067_200_000)]), tmp_path, dataset, partition_by=()
    )
    root = dataset_path(tmp_path, dataset)
    assert (root / "date=unknown" / "part.parquet").exists()

    # `unknown` sorts after any real date, so a since_date read never prunes it.
    out = read_dataset_columns(tmp_path, dataset, since_date="2024-01-01")
    assert set(out["cycle_id"].to_list()) == {"bad", "good"}


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


def test_non_ledger_dataset_keeps_its_own_partitioning(tmp_path: Path) -> None:
    """The ledger hook must not touch a dataset outside LEDGER_BUCKET_SOURCE: the
    caller's (date, symbol) layout stays, and no month column is written."""
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_704_067_200_000, "symbol": "BTCUSDT", "buy_quote": 1.0, "sell_quote": 2.0}]),
        tmp_path, "funding",
    )
    root = dataset_path(tmp_path, "funding")
    assert not list(root.glob(f"{_LEDGER_MONTH_COL}=*"))  # not bucketed
    assert (root / "date=2024-01-01" / "symbol=BTCUSDT" / "part.parquet").exists()
    assert read_dataset(tmp_path, "funding").height == 1


def test_exclusive_file_lock_release_does_not_delete_successor_lock(tmp_path: Path) -> None:
    """If an external unlink lets a successor recreate the path with a NEW inode while
    we are still in the critical section, release must not delete the successor's
    lock -- an unconditional unlink-by-path admits a second concurrent writer.
    """
    lock = tmp_path / "x.lock"
    with exclusive_file_lock(lock, stale_seconds=600):
        os.unlink(lock)
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": 999_999, "created": time.time()}))
        successor_ino = os.stat(lock).st_ino
    assert lock.exists(), "release deleted a successor's lock (CROS-1)"
    assert os.stat(lock).st_ino == successor_ino


def test_exclusive_file_lock_release_keeps_its_persistent_inode(tmp_path: Path) -> None:
    lock = tmp_path / "z.lock"
    with exclusive_file_lock(lock, stale_seconds=600):
        inode = lock.stat().st_ino
    assert lock.exists()
    assert lock.stat().st_ino == inode


def test_funding_resolves_to_binance_usdm_funding_without_symlink(tmp_path: Path) -> None:
    """A raw per-venue root that stores funding as binance_usdm_funding is read as
    canonical `funding` with no symlink/rename — read_dataset auto-resolves it."""
    from liquidity_migration.data.storage import resolve_dataset_name

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
    from liquidity_migration.data.storage import resolve_dataset_name

    canonical = pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.001]})
    write_dataset(canonical, tmp_path, "funding", partition_by=())
    assert resolve_dataset_name(tmp_path, "funding") == "funding"
    got = read_dataset(tmp_path, "funding")
    assert got["funding_rate"].to_list() == [0.001]


def test_missing_funding_everywhere_returns_empty(tmp_path: Path) -> None:
    """No canonical and no venue-variant funding -> empty frame, name unchanged."""
    from liquidity_migration.data.storage import resolve_dataset_name

    assert resolve_dataset_name(tmp_path, "funding") == "funding"
    assert read_dataset(tmp_path, "funding").is_empty()


# --- Funding-fallback gating, reused-pid stale singleton-lock eviction, orphaned
# `.*.tmp` sweeping, and the parent-dir fsync after the atomic rename ---------
from liquidity_migration.data.storage import resolve_dataset_name as _b10_resolve_dataset_name  # noqa: E402


def test_canonical_klines_root_with_binance_variant_resolves_to_binance_funding(tmp_path: Path) -> None:
    """A Binance full-PIT root stores klines under the canonical ``klines_1h/`` name AND
    its funding as ``binance_usdm_funding/`` with no canonical ``funding/``.
    ``resolve_dataset_name`` must use the present venue variant (a ``binance_usdm_*``
    dir only ever exists on a Binance root), matching ``_autodetect_dataset_names``;
    suppressing it on any root carrying a canonical kline marker returns empty funding
    on a fully populated dataset.
    """
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
    """A real Bybit root always has its own canonical ``funding/`` dir, which takes
    precedence over any ``binance_usdm_*`` proxy dataset on the same root, so the
    wrong-venue curve is never served. Only Binance-native roots lack canonical
    funding/, and those should use the variant.
    """
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


def test_exclusive_file_lock_recovers_after_abrupt_holder_exit(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "funding")
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    exit_now = context.Event()
    holder = context.Process(
        target=_hold_lock_until_abrupt_exit,
        args=(str(lock_path), acquired, exit_now),
    )
    holder.start()
    assert acquired.wait(timeout=5.0)
    inode = lock_path.stat().st_ino

    exit_now.set()
    holder.join(timeout=5.0)
    assert holder.exitcode == 17

    started = time.monotonic()
    with exclusive_file_lock(lock_path, poll_seconds=0.005):
        assert lock_path.stat().st_ino == inode
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("leaf_kind", ["symlink", "hardlink", "directory", "fifo"])
def test_exclusive_file_lock_refuses_aliased_or_nonregular_leaf(
    tmp_path: Path,
    leaf_kind: str,
) -> None:
    target = tmp_path / "target"
    target.write_text("do-not-touch", encoding="utf-8")
    target.chmod(0o644)
    lock_path = tmp_path / "unsafe.lock"
    if leaf_kind == "symlink":
        lock_path.symlink_to(target)
    elif leaf_kind == "hardlink":
        os.link(target, lock_path)
    elif leaf_kind == "directory":
        lock_path.mkdir()
    else:
        os.mkfifo(lock_path)

    with pytest.raises(RuntimeError, match="lock path"):
        with exclusive_file_lock(lock_path, poll_seconds=0.0):
            pytest.fail("unsafe lock leaf was admitted")

    assert target.read_text(encoding="utf-8") == "do-not-touch"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_storageconcurrency4_orphaned_tmp_swept_on_next_write(tmp_path: Path) -> None:
    """A SIGKILL between ``write_parquet`` and the atomic rename orphans a
    ``.{name}.{pid}.{ns}.tmp`` file (the finally-unlink never runs). The next
    ``write_dataset`` must sweep stale ``.*.tmp`` files so a Restart=always daemon
    does not accumulate orphans until the disk fills.
    """
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
    """The broad orphan-temp sweep must not recursively walk a large dataset on every
    partition write; the target partition directory is still checked every time so
    same-partition orphans are cleaned promptly.
    """
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
    """``_write_part`` must fsync the PARENT DIRECTORY fd after
    ``temp_path.replace(path)``, not just the temp file, or the rename is not
    power-loss durable. Pinned by recording every ``os.fsync`` target.
    """
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


def test_replace_dataset_swaps_atomically_and_holds_the_reader_lock(tmp_path: Path) -> None:
    """``rewrite_manifest_to_coverage`` must not rmtree the dataset outside the lock
    readers take for a consistent snapshot: a concurrent reader could collect a
    half-deleted dataset, and a kill between the two steps leaves the PIT membership
    dataset gone.
    """

    old = pl.DataFrame({"date": ["2026-07-01", "2026-07-02"], "symbol": ["A", "B"], "value": [1, 2]})
    write_dataset(old, tmp_path, "archive_trade_manifest", partition_by=("date",))
    assert read_dataset(tmp_path, "archive_trade_manifest").height == 2

    new = pl.DataFrame({"date": ["2026-07-03"], "symbol": ["C"], "value": [3]})
    storage.replace_dataset(new, tmp_path, "archive_trade_manifest", partition_by=("date",))

    replaced = read_dataset(tmp_path, "archive_trade_manifest")
    # A true replacement, not a per-partition merge: the old dates are gone.
    assert sorted(replaced["symbol"].to_list()) == ["C"]
    # No staging or retired directory survives a successful swap.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".archive_trade_manifest")]
    assert leftovers == []


def test_replace_dataset_leaves_the_previous_generation_intact_on_failure(tmp_path: Path) -> None:
    old = pl.DataFrame({"date": ["2026-07-01"], "symbol": ["A"], "value": [1]})
    write_dataset(old, tmp_path, "archive_trade_manifest", partition_by=("date",))

    class Boom(RuntimeError):
        pass

    original = storage._write_dataset_unlocked

    def failing(*args: object, **kwargs: object) -> None:
        raise Boom("simulated write failure")

    storage._write_dataset_unlocked = failing  # type: ignore[assignment]
    try:
        with pytest.raises(Boom):
            storage.replace_dataset(
                pl.DataFrame({"date": ["2026-07-02"], "symbol": ["B"], "value": [2]}),
                tmp_path,
                "archive_trade_manifest",
                partition_by=("date",),
            )
    finally:
        storage._write_dataset_unlocked = original  # type: ignore[assignment]

    survived = read_dataset(tmp_path, "archive_trade_manifest")
    assert survived["symbol"].to_list() == ["A"]
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".archive_trade_manifest")]
    assert leftovers == []
