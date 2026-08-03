"""Rewrite a cycle-ledger dataset from the retired layouts into day buckets.

Two layouts predate the day bucket and both make every cycle append rewrite far
more than one day of rows:

- ``_ledger_month=YYYYMM/part.parquet`` — the month scheme;
- a single top-level ``part.parquet`` — what an unregistered cycles dataset got.

Reads keep working on either without this script (``read_dataset`` unions
whatever parts exist and drops the month column), so this is a cost migration,
not a correctness one. Run it to stop paying the old rewrite.

    PYTHONPATH=. .venv/bin/python \\
        scripts/maintain/migrate_cycle_ledger_buckets.py --root PATH \\
        [--dataset carry_hold_demo_cycles] [--execute --i-stopped-the-writer]

Dry run by default: it prints what it would rewrite and touches nothing.
``--execute`` needs ``--i-stopped-the-writer`` as well, because the producer
must be stopped for the duration and this script cannot see systemd from a dev
box. It refuses to run while the dataset lock is held, but that is a snapshot —
a writer that starts a millisecond later would just interleave whole cycles
with the rewrite, and the last one to write wins.

The rewrite is read-all, verify, replace whole, verify again, and it is
idempotent: a dataset already in day buckets is reported and skipped.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import polars as pl  # noqa: E402

from liquidity_migration.data.storage import (  # noqa: E402
    _LEDGER_BUCKET_COL,
    _LEDGER_MONTH_COL,
    LEDGER_BUCKET_SOURCE,
    dataset_lock_path,
    dataset_path,
    read_dataset,
    replace_dataset,
)


def dataset_lock_is_held(root: Path, dataset: str) -> bool:
    """True when some other process holds the dataset's writer lock right now.

    Every storage write takes a blocking ``flock`` on this path, so one
    non-blocking attempt is the honest question. A missing lock file means the
    lock was never taken on this root.
    """
    lock_path = dataset_lock_path(root, dataset)
    if not lock_path.exists():
        return False
    fd = os.open(lock_path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def legacy_parts(root: Path, dataset: str) -> list[Path]:
    """Parts still in a pre-day-bucket layout, newest scheme first."""
    path = dataset_path(root, dataset)
    if not path.exists():
        return []
    found = sorted(
        part
        for month_dir in path.glob(f"{_LEDGER_MONTH_COL}=*")
        if month_dir.is_dir()
        for part in month_dir.glob("**/*.parquet")
    )
    monolith = path / "part.parquet"
    if monolith.exists():
        found.append(monolith)
    return found


def content_digest(frame: pl.DataFrame) -> str:
    """Order-independent hash of the frame's content, ignoring the derived
    ``date`` column.

    Row order is not content here — the rewrite re-sorts every part — and the
    bucket column is recomputed from ``ts_ms`` by definition, so both are
    excluded and the date column is checked separately.
    """
    columns = sorted(col for col in frame.columns if col != _LEDGER_BUCKET_COL)
    if not columns or frame.is_empty():
        return hashlib.sha256(f"empty:{len(columns)}".encode()).hexdigest()
    row_hashes = sorted(int(value) for value in frame.select(columns).hash_rows().to_list())
    digest = hashlib.sha256()
    for value in row_hashes:
        digest.update(value.to_bytes(8, "big"))
    return digest.hexdigest()


def _duplicate_keys(frame: pl.DataFrame) -> int:
    if "cycle_id" not in frame.columns or frame.is_empty():
        return 0
    return int(frame.height - frame.select("cycle_id").n_unique())


def _bucket_disagreements(frame: pl.DataFrame, dataset: str) -> int:
    """Rows whose stored ``date`` does not match the day their ``ts_ms`` falls in."""
    if frame.is_empty() or _LEDGER_BUCKET_COL not in frame.columns:
        return 0
    source = LEDGER_BUCKET_SOURCE[dataset]
    if source not in frame.columns:
        return 0
    expected = (
        pl.from_epoch(pl.col(source).cast(pl.Int64, strict=False), time_unit="ms")
        .dt.strftime("%Y-%m-%d")
        .fill_null("unknown")
    )
    return int(
        frame.select(
            (pl.col(_LEDGER_BUCKET_COL).cast(pl.String, strict=False) != expected).sum()
        ).item()
    )


def migrate_dataset(root: Path, dataset: str, *, execute: bool) -> bool:
    """Rewrite one dataset on one root. Returns False on a refusal or a failure."""
    label = f"[{root.name}/{dataset}]"
    if dataset not in LEDGER_BUCKET_SOURCE:
        print(f"  {label} REFUSING: not a registered cycle ledger; nothing defines its day bucket")
        return False
    path = dataset_path(root, dataset)
    if not path.exists():
        print(f"  {label} absent on this root, skipping")
        return True
    stale = legacy_parts(root, dataset)
    if not stale:
        print(f"  {label} already migrated ({len(list(path.glob('date=*')))} day buckets)")
        return True
    shown = [str(part.relative_to(path)) for part in stale[:3]]
    more = "" if len(stale) <= 3 else f" (+{len(stale) - 3} more)"
    print(f"  {label} {len(stale)} part(s) in a retired layout: {', '.join(shown)}{more}")
    if not execute:
        print(f"  {label} would rewrite into day buckets (dry run; pass --execute)")
        return True

    if dataset_lock_is_held(root, dataset):
        print(f"  {label} REFUSING: the dataset lock is held — a writer is running")
        return False

    before = read_dataset(root, dataset)
    duplicates = _duplicate_keys(before)
    if duplicates:
        print(
            f"  {label} REFUSING: {duplicates} duplicate cycle_id row(s) on disk; "
            "the rewrite dedups per part and would drop them silently"
        )
        return False
    before_height = before.height
    before_digest = content_digest(before)
    print(f"  {label} {before_height} rows, digest {before_digest[:12]}")

    replace_dataset(before, root, dataset, partition_by=(_LEDGER_BUCKET_COL,))

    after = read_dataset(root, dataset)
    problems = []
    if after.height != before_height:
        problems.append(f"row count {before_height} -> {after.height}")
    after_digest = content_digest(after)
    if after_digest != before_digest:
        problems.append(f"content digest {before_digest[:12]} -> {after_digest[:12]}")
    remaining = legacy_parts(root, dataset)
    if remaining:
        problems.append(f"{len(remaining)} retired-layout part(s) still present")
    if problems:
        print(f"  {label} FAILED after rewrite: {'; '.join(problems)}")
        return False
    disagreements = _bucket_disagreements(after, dataset)
    if disagreements:
        print(f"  {label} FAILED after rewrite: {disagreements} row(s) in the wrong day bucket")
        return False
    buckets = sorted(p.name for p in path.glob("date=*"))
    print(
        f"  {label} rewrote {after.height} rows into {len(buckets)} day buckets "
        f"({buckets[0] if buckets else 'none'} .. {buckets[-1] if buckets else 'none'})"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--root",
        action="append",
        required=True,
        help="data root holding the cycle ledgers; repeat for several roots",
    )
    ap.add_argument(
        "--dataset",
        action="append",
        default=None,
        help=f"cycle ledger to migrate; repeat. Default: all of {sorted(LEDGER_BUCKET_SOURCE)}",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="actually rewrite; without it the run only reports what it would do",
    )
    ap.add_argument(
        "--i-stopped-the-writer",
        action="store_true",
        help=(
            "acknowledge that the producer unit writing these datasets is stopped. "
            "This script cannot see systemd, so it takes your word for it"
        ),
    )
    args = ap.parse_args(argv)
    if args.execute and not args.i_stopped_the_writer:
        ap.error("--execute also needs --i-stopped-the-writer (stop the producer unit first)")

    datasets = list(args.dataset) if args.dataset else sorted(LEDGER_BUCKET_SOURCE)
    failures = 0
    for raw_root in args.root:
        root = Path(raw_root).expanduser()
        print(f"{root}:")
        if not root.exists():
            print("  REFUSING: data root does not exist")
            failures += 1
            continue
        for dataset in datasets:
            try:
                ok = migrate_dataset(root, dataset, execute=args.execute)
            except ValueError as exc:  # unknown dataset name
                print(f"  [{root.name}/{dataset}] REFUSING: {exc}")
                ok = False
            if not ok:
                failures += 1
    if failures:
        print(f"{failures} dataset(s) refused or failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
