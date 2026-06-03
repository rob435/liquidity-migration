#!/usr/bin/env python3
"""One-time migration: split each monolithic demo/paper ledger ``part.parquet`` into
month buckets (reconcile-ledger-5 / quality-dup-5).

The code change (storage.LEDGER_BUCKET_SOURCE) is backward-compatible: once deployed,
NEW writes go to ``_ledger_month=YYYYMM/`` buckets and reads union the legacy monolith
with the buckets (deduped on the dataset key). This script DRAINS the legacy monolith
into buckets so the windowed reconcile read (read_ledger_window) becomes fully
effective. It is NOT required for correctness — it is an optimization — so it can be
run at the operator's convenience.

Idempotent + dedup-safe: re-running re-reads the legacy rows and re-buckets them; a
trade_id / order_link_id present in both the legacy monolith and a bucket is never
double-counted (read dedups on the dataset key, keeping the freshest updated_at_ms).
The legacy ``part.parquet`` is removed ONLY after the buckets are written AND a re-read
key-set check passes.

RUN WITH ALL DEMO DAEMONS STOPPED so no writer races the migration:

    # on the VPS, with the systemd units stopped:
    python3 scripts/migrate_ledger_buckets.py --dry-run /path/to/demo_root [more roots...]
    python3 scripts/migrate_ledger_buckets.py /path/to/demo_root [more roots...]

Each positional arg is a data root; the script migrates every LEDGER_BUCKET_SOURCE
dataset that has a legacy monolith under that root.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

from liquidity_migration import storage
from liquidity_migration.storage import (
    LEDGER_BUCKET_SOURCE,
    dataset_path,
    read_dataset,
    write_dataset,
)


def migrate_one(root: Path, dataset: str, *, dry_run: bool) -> bool:
    """Migrate one (root, dataset). Returns True if a migration was performed (or would
    be, in dry-run), False if skipped (no legacy monolith)."""
    path = dataset_path(root, dataset)
    legacy = path / "part.parquet"
    if not legacy.exists():
        return False
    keys = list(storage.DATASET_KEYS.get(dataset, ()))
    legacy_df = pl.read_parquet(legacy)
    # Deduped union (legacy monolith + any already-written buckets) BEFORE migration —
    # this is the invariant the migration must preserve exactly.
    before = read_dataset(root, dataset)
    key_cols = [c for c in keys if c in before.columns]
    print(f"  {dataset}: legacy_rows={legacy_df.height} union_rows={before.height} key={key_cols}")
    if dry_run:
        return True

    # Re-write the legacy rows through the bucketed writer (adds _ledger_month, appends
    # + dedups into the correct month buckets). append=True (default) makes it idempotent.
    write_dataset(legacy_df, root, dataset, partition_by=())

    after = read_dataset(root, dataset)
    if key_cols and not before.is_empty():
        b = set(map(tuple, before.select(key_cols).iter_rows()))
        a = set(map(tuple, after.select(key_cols).iter_rows()))
        if a != b:
            raise SystemExit(
                f"ABORT {dataset}: key-set changed by migration "
                f"(before={len(b)} after={len(a)}); legacy monolith NOT removed."
            )
    if after.height < before.height:
        raise SystemExit(
            f"ABORT {dataset}: row-count shrank {before.height}->{after.height}; "
            "legacy monolith NOT removed."
        )

    # Buckets written + verified -> the legacy monolith is now redundant; remove it so
    # the windowed read no longer has to always pull it.
    legacy.unlink()
    final = read_dataset(root, dataset)
    if key_cols and set(map(tuple, final.select(key_cols).iter_rows())) != (
        set(map(tuple, before.select(key_cols).iter_rows()))
    ):
        raise SystemExit(f"ABORT {dataset}: key-set changed AFTER removing legacy monolith.")
    print(f"    migrated: legacy part.parquet drained into buckets; final_rows={final.height}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", help="data root(s) to migrate")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = parser.parse_args(argv)

    any_done = False
    for root_str in args.roots:
        root = Path(root_str).expanduser()
        if not root.exists():
            print(f"skip (missing root): {root}")
            continue
        print(f"root: {root}{'  [DRY RUN]' if args.dry_run else ''}")
        for dataset in sorted(LEDGER_BUCKET_SOURCE):
            try:
                if migrate_one(root, dataset, dry_run=args.dry_run):
                    any_done = True
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001 - report + continue other datasets
                print(f"  ERROR {dataset}: {exc!r}")
    if not any_done:
        print("nothing to migrate (no legacy part.parquet found under any root).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
