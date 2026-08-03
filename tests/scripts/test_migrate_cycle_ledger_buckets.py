"""``migrate_cycle_ledger_buckets`` rewrites the retired cycle-ledger layouts
(``_ledger_month=`` dirs and the single top-level ``part.parquet``) into day
buckets without changing a row, and refuses to do it under a live writer.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.data.storage import (
    _LEDGER_MONTH_COL,
    dataset_lock_path,
    dataset_path,
    exclusive_file_lock,
    read_dataset,
    write_dataset,
)

import scripts.maintain.migrate_cycle_ledger_buckets as migrate

DATASET = "carry_hold_demo_cycles"
_MS_PER_DAY = 86_400_000
_AUG_1 = 1_754_006_400_000  # 2025-08-01 UTC


def _hold_lock(path: str, acquired, release) -> None:
    with exclusive_file_lock(path, poll_seconds=0.005):
        acquired.set()
        if not release.wait(timeout=10.0):
            raise RuntimeError("timed out waiting to release the test lock")


def _seed_month_part(root: Path, cycle_id: str, ts_ms: int, month: int) -> None:
    part_dir = dataset_path(root, DATASET) / f"{_LEDGER_MONTH_COL}={month}"
    part_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        [
            {
                "cycle_id": cycle_id,
                "ts_ms": ts_ms,
                "date": "2025-08-01",
                "equity_usdt": 1000.0,
                _LEDGER_MONTH_COL: month,
            }
        ]
    ).write_parquet(part_dir / "part.parquet")


def _mixed_root(tmp_path: Path) -> Path:
    """A root as it looks mid-flip: one legacy month part plus a fresh day part."""
    _seed_month_part(tmp_path, "old", _AUG_1, 202508)
    write_dataset(
        pl.DataFrame([{"cycle_id": "new", "ts_ms": _AUG_1 + _MS_PER_DAY, "equity_usdt": 1001.0}]),
        tmp_path,
        DATASET,
        partition_by=(),
    )
    return tmp_path


def test_dry_run_reports_and_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _mixed_root(tmp_path)
    before = sorted(p.name for p in dataset_path(root, DATASET).iterdir())

    assert migrate.main(["--root", str(root), "--dataset", DATASET]) == 0

    out = capsys.readouterr().out
    assert "would rewrite into day buckets" in out
    assert sorted(p.name for p in dataset_path(root, DATASET).iterdir()) == before


def test_execute_unifies_the_layout_without_changing_rows(tmp_path: Path) -> None:
    root = _mixed_root(tmp_path)
    before = read_dataset(root, DATASET)

    code = migrate.main(
        ["--root", str(root), "--dataset", DATASET, "--execute", "--i-stopped-the-writer"]
    )

    assert code == 0
    path = dataset_path(root, DATASET)
    assert not list(path.glob(f"{_LEDGER_MONTH_COL}=*"))
    assert not (path / "part.parquet").exists()
    assert sorted(p.name for p in path.glob("date=*")) == ["date=2025-08-01", "date=2025-08-02"]
    after = read_dataset(root, DATASET)
    assert after.height == before.height
    assert migrate.content_digest(after) == migrate.content_digest(before)
    assert set(after["cycle_id"].to_list()) == {"old", "new"}
    assert _LEDGER_MONTH_COL not in after.columns


def test_top_level_monolith_is_migrated(tmp_path: Path) -> None:
    """The unregistered-dataset shape: one part.parquet holding everything."""
    path = dataset_path(tmp_path, DATASET)
    path.mkdir(parents=True)
    pl.DataFrame(
        [
            {"cycle_id": "a", "ts_ms": _AUG_1, "date": "2025-08-01"},
            {"cycle_id": "b", "ts_ms": _AUG_1 + _MS_PER_DAY, "date": "2025-08-02"},
        ]
    ).write_parquet(path / "part.parquet")

    code = migrate.main(
        ["--root", str(tmp_path), "--dataset", DATASET, "--execute", "--i-stopped-the-writer"]
    )

    assert code == 0
    assert not (path / "part.parquet").exists()
    assert sorted(p.name for p in path.glob("date=*")) == ["date=2025-08-01", "date=2025-08-02"]
    assert read_dataset(tmp_path, DATASET).height == 2


def test_second_run_is_a_no_op(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _mixed_root(tmp_path)
    args = ["--root", str(root), "--dataset", DATASET, "--execute", "--i-stopped-the-writer"]
    assert migrate.main(args) == 0
    first = {p: p.stat().st_mtime_ns for p in sorted(dataset_path(root, DATASET).rglob("*.parquet"))}
    capsys.readouterr()

    assert migrate.main(args) == 0

    out = capsys.readouterr().out
    assert "already migrated" in out
    assert {p: p.stat().st_mtime_ns for p in sorted(dataset_path(root, DATASET).rglob("*.parquet"))} == first


def test_execute_requires_the_writer_acknowledgement(tmp_path: Path) -> None:
    root = _mixed_root(tmp_path)
    with pytest.raises(SystemExit):
        migrate.main(["--root", str(root), "--dataset", DATASET, "--execute"])
    assert list(dataset_path(root, DATASET).glob(f"{_LEDGER_MONTH_COL}=*"))


def test_refuses_while_the_dataset_lock_is_held(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _mixed_root(tmp_path)
    lock_path = str(dataset_lock_path(root, DATASET))
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(lock_path, acquired, release))
    holder.start()
    try:
        assert acquired.wait(timeout=10.0)
        assert migrate.dataset_lock_is_held(root, DATASET) is True
        code = migrate.main(
            ["--root", str(root), "--dataset", DATASET, "--execute", "--i-stopped-the-writer"]
        )
    finally:
        release.set()
        holder.join(timeout=10.0)

    assert code == 1
    assert "the dataset lock is held" in capsys.readouterr().out
    assert list(dataset_path(root, DATASET).glob(f"{_LEDGER_MONTH_COL}=*"))
    assert migrate.dataset_lock_is_held(root, DATASET) is False


def test_duplicate_keys_are_refused_before_any_rewrite(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A cross-part duplicate would be deduped away by the rewrite. Refuse
    instead of losing the row and reporting a row-count failure afterwards."""
    _seed_month_part(tmp_path, "dup", _AUG_1, 202508)
    _seed_month_part(tmp_path, "dup", _AUG_1, 202509)

    code = migrate.main(
        ["--root", str(tmp_path), "--dataset", DATASET, "--execute", "--i-stopped-the-writer"]
    )

    assert code == 1
    assert "duplicate cycle_id" in capsys.readouterr().out
    assert len(list(dataset_path(tmp_path, DATASET).glob(f"{_LEDGER_MONTH_COL}=*"))) == 2


def test_unregistered_dataset_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = migrate.main(["--root", str(tmp_path), "--dataset", "funding", "--execute", "--i-stopped-the-writer"])
    assert code == 1
    assert "not a registered cycle ledger" in capsys.readouterr().out
