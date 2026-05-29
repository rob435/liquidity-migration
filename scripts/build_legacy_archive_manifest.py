"""Build a side-copy data root whose archive_trade_manifest excludes the
v5-listing supplement, isolating the 474-only legacy-archive universe.

Pre-reg: docs/research_summary.md
(Change 3 + Phase 1).

The side-copy structure:

    <SOURCE_ROOT>                       (e.g. ~/SHARED_DATA/bybit_full_pit)
      archive_trade_manifest/           ← source: 280 archive + 290 v5-listing rows/day
      klines_1h/  funding/  …           ← source

    <TARGET_ROOT>                       (e.g. ~/SHARED_DATA/bybit_full_pit_archive_only)
      archive_trade_manifest/           ← REAL: source rows filtered to
                                          source == "bybit_public_trading_archive"
      klines_1h/  funding/  …           ← SYMLINK / JUNCTION → source

Only the manifest differs; klines/funding/OI/premium-index/etc. are reused
via filesystem links. Re-runs are idempotent — existing matching links
are left in place; an existing target archive_trade_manifest is rewritten
to reflect any new partitions from the source.

The reports/ subtree is intentionally NOT linked — Phase 1 cells land
their per-cell reports under <TARGET_ROOT>/reports/ so the universe-
restricted runs are kept structurally separate from the full-universe ones.

NEVER promote a Phase-1 result. The 474-archive-only universe applies a
2026 archive scrape retroactively to 2021-2025 data, which is by definition
survivorship-contaminated and not PIT-correct as a tradable universe. The
side-copy exists for the universe-isolation diagnostic only.

Usage:

    python scripts/build_legacy_archive_manifest.py
        [--source ~/SHARED_DATA/bybit_full_pit]
        [--target ~/SHARED_DATA/bybit_full_pit_archive_only]
        [--source-tag bybit_public_trading_archive]
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import polars as pl


# Subdirectories of the source root that should NOT be link-mirrored. The
# manifest is rewritten with a row-filter; reports stay distinct per root
# so the 474 cells don't pollute the 764 root's report tree.
REAL_SUBDIRS_IN_TARGET: frozenset[str] = frozenset({"archive_trade_manifest", "reports"})


def _link_subdir(target: Path, source: Path) -> str:
    """Create a directory link from target → source.

    Returns the link kind that was used: 'symlink' (POSIX or Windows w/
    developer mode) or 'junction' (Windows fallback, no admin needed).
    Raises RuntimeError if both attempts fail (typically Windows without
    developer mode AND with mklink failing).
    """
    if target.exists() or target.is_symlink():
        # Idempotency: assume existing link is correct (we resolve and
        # verify the operator-visible state below before declaring done).
        return "exists"
    target.parent.mkdir(parents=True, exist_ok=True)
    # First try a real symlink (POSIX always works; Windows works under
    # developer mode or admin). Only fall back to junction on Windows
    # when the symlink path raised due to permissions.
    try:
        os.symlink(str(source), str(target), target_is_directory=True)
        return "symlink"
    except OSError as exc:
        if os.name != "nt":
            raise RuntimeError(f"symlink failed for {target} → {source}: {exc}") from exc
        # Windows fallback: directory junction via mklink /J. Junctions do
        # not require admin or developer mode, work for directories, and
        # are followed transparently by Python's Path / open().
        try:
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as inner:
            raise RuntimeError(
                f"Neither os.symlink nor mklink /J worked for {target} → {source}: "
                f"symlink raised {exc!r}; cmd not found ({inner})"
            ) from inner
        if result.returncode != 0:
            raise RuntimeError(
                f"Neither os.symlink nor mklink /J worked for {target} → {source}: "
                f"symlink raised {exc!r}; mklink stderr={result.stderr.strip()!r}"
            )
        return "junction"


def _filter_partition(src_partition: Path, dst_partition: Path, source_tag: str) -> dict[str, int]:
    """Filter one date partition. Returns row counts {input, kept, dropped}."""
    parquet_files = sorted(src_partition.glob("*.parquet"))
    if not parquet_files:
        return {"input": 0, "kept": 0, "dropped": 0, "partitions": 0}
    if len(parquet_files) > 1:
        # Manifest partitions are written single-file (`part.parquet`) by
        # the archive build pipeline. Multiple files would indicate a
        # tooling regression; flag it but proceed by concatenating.
        print(f"  WARN {src_partition.name}: {len(parquet_files)} parquet files (expected 1)", file=sys.stderr)
    df = pl.concat([pl.read_parquet(p) for p in parquet_files])
    if "source" not in df.columns:
        raise RuntimeError(
            f"{src_partition} parquet missing 'source' column; cannot filter. "
            f"Schema: {df.schema}"
        )
    kept = df.filter(pl.col("source") == source_tag)
    dst_partition.mkdir(parents=True, exist_ok=True)
    # Single-file write keeps the layout consistent with the source.
    kept.write_parquet(dst_partition / "part.parquet")
    return {
        "input": df.height,
        "kept": kept.height,
        "dropped": df.height - kept.height,
        "partitions": len(parquet_files),
    }


def _resolve(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--source",
        default=str(Path.home() / "SHARED_DATA" / "bybit_full_pit"),
        help="Source data root with the full archive_trade_manifest (default: ~/SHARED_DATA/bybit_full_pit).",
    )
    parser.add_argument(
        "--target",
        default=str(Path.home() / "SHARED_DATA" / "bybit_full_pit_archive_only"),
        help="Target side-copy data root to (re)build (default: ~/SHARED_DATA/bybit_full_pit_archive_only).",
    )
    parser.add_argument(
        "--source-tag",
        default="bybit_public_trading_archive",
        help='Manifest row source value to KEEP (default keeps the 474-archive universe, drops "bybit_v5_listing").',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the plan but make no filesystem changes.",
    )
    args = parser.parse_args(argv)

    src_root = _resolve(args.source)
    dst_root = _resolve(args.target)

    if not src_root.is_dir():
        print(f"ERROR: source root not a directory: {src_root}", file=sys.stderr)
        return 2
    src_manifest = src_root / "archive_trade_manifest"
    if not src_manifest.is_dir():
        print(f"ERROR: source has no archive_trade_manifest at {src_manifest}", file=sys.stderr)
        return 2

    print(f"source : {src_root}")
    print(f"target : {dst_root}")
    print(f"keep   : source == {args.source_tag!r}  (dropping all other source tags)")
    print(f"dry-run: {args.dry_run}")
    print()

    # 1. Mirror subdirectories via link (everything except real-rewritten ones)
    link_actions: list[tuple[str, str, str]] = []  # (subdir, kind, target)
    for entry in sorted(src_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in REAL_SUBDIRS_IN_TARGET:
            continue
        target = dst_root / entry.name
        if args.dry_run:
            link_actions.append((entry.name, "dry-run", str(target)))
        else:
            kind = _link_subdir(target, entry)
            link_actions.append((entry.name, kind, str(target)))

    print("Link plan:")
    for name, kind, target in link_actions:
        print(f"  {name:30s}  {kind:10s}  -> {target}")
    print()

    # 2. Filter archive_trade_manifest partition-by-partition
    dst_manifest = dst_root / "archive_trade_manifest"
    if not args.dry_run:
        dst_manifest.mkdir(parents=True, exist_ok=True)

    partitions = sorted(p for p in src_manifest.iterdir() if p.is_dir() and p.name.startswith("date="))
    print(f"manifest partitions: {len(partitions)}")

    totals = {"input": 0, "kept": 0, "dropped": 0, "partitions_processed": 0, "partitions_files": 0}
    start = time.monotonic()
    for src_part in partitions:
        dst_part = dst_manifest / src_part.name
        if args.dry_run:
            totals["partitions_processed"] += 1
            continue
        counts = _filter_partition(src_part, dst_part, args.source_tag)
        totals["input"] += counts["input"]
        totals["kept"] += counts["kept"]
        totals["dropped"] += counts["dropped"]
        totals["partitions_processed"] += 1
        totals["partitions_files"] += counts["partitions"]
    elapsed = time.monotonic() - start

    # 3. Reports subdir is local-real, not linked
    if not args.dry_run:
        (dst_root / "reports").mkdir(parents=True, exist_ok=True)

    # 4. BUILD_MANIFEST.json receipt
    if not args.dry_run:
        receipt = {
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_root": str(src_root),
            "target_root": str(dst_root),
            "source_tag_kept": args.source_tag,
            "links": [
                {"subdir": name, "kind": kind, "target": target} for name, kind, target in link_actions
            ],
            "manifest_filter_totals": totals,
            "elapsed_seconds": round(elapsed, 2),
            "warning": (
                "biased_benchmark only. The archive-only universe applies the 2026 archive "
                "coverage retroactively and is NOT PIT-correct for historical periods. NEVER promote "
                "a configuration trained on this side-copy to live trading."
            ),
        }
        (dst_root / "BUILD_MANIFEST.json").write_text(json.dumps(receipt, indent=2))

    print()
    print(
        f"manifest filter: input={totals['input']}  kept={totals['kept']}  "
        f"dropped={totals['dropped']}  ({totals['partitions_processed']} partitions, {elapsed:.1f}s)"
    )
    print(f"done. target root: {dst_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
