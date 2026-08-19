#!/usr/bin/env python3
"""Build the P0 cross-venue research panel, one calendar year per shard.

Sharding is a memory decision, not a semantic one: a full-history build is tens
of millions of rows and does not need to exist in one frame. Each shard carries
its own manifest, and a top-level manifest records the set.

`liquidity_migration/research/panels/cross_venue_panel.py` holds the causality contract this
inherits.

Example:

    python scripts/data/build_cross_venue_panel.py \\
        --start 2021-01-01 --end 2026-07-18 \\
        --out ~/SHARED_DATA/cross_venue_panel_v1
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.research.panels.cross_venue_panel import (  # noqa: E402
    PanelBuildError,
    PanelSpec,
    build_panel,
    write_panel,
)

DEFAULT_BYBIT_ROOT = Path.home() / "SHARED_DATA" / "bybit_full_pit"
DEFAULT_BINANCE_ROOT = Path.home() / "SHARED_DATA" / "binance_full_pit"


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=_date, required=True, help="inclusive first date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_date, required=True, help="EXCLUSIVE last date (YYYY-MM-DD)")
    parser.add_argument("--out", type=Path, required=True, help="output directory for shards + manifests")
    parser.add_argument("--bybit-root", type=Path, default=DEFAULT_BYBIT_ROOT)
    parser.add_argument("--binance-root", type=Path, default=DEFAULT_BINANCE_ROOT)
    parser.add_argument(
        "--metrics-root",
        type=Path,
        default=None,
        help=(
            "daily Binance USDM metrics parquet (scripts/data/refresh_binance_metrics.py); "
            "adds the bn_tt_ls top-trader positioning columns. Omitted = columns absent, "
            "and configs that need them fail loudly at scoring."
        ),
    )
    parser.add_argument(
        "--execution-delay-ms",
        type=int,
        default=0,
        help="claim-appropriate delay ON TOP OF the mandatory 1h bar-completion lag",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="optional comma-separated symbol restriction; default is the full both-venue population",
    )
    return parser.parse_args(argv)


def year_windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Split ``[start, end)`` into per-calendar-year half-open windows."""
    windows: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor < end:
        next_year = min(dt.date(cursor.year + 1, 1, 1), end)
        windows.append((cursor, next_year))
        cursor = next_year
    return windows


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.end <= args.start:
        print(f"error: --end {args.end} must be after --start {args.start} (end is exclusive)", file=sys.stderr)
        return 2

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",")) if args.symbols else None
    out_root: Path = args.out.expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    shards: list[dict[str, object]] = []
    total_rows = 0
    for window_start, window_end in year_windows(args.start, args.end):
        label = str(window_start.year)
        spec = PanelSpec(
            bybit_root=args.bybit_root.expanduser(),
            binance_root=args.binance_root.expanduser(),
            start=window_start,
            end=window_end,
            execution_delay_ms=args.execution_delay_ms,
            symbols=symbols,
            binance_metrics_root=(
                args.metrics_root.expanduser() if args.metrics_root else None
            ),
        )
        try:
            panel, manifest = build_panel(spec)
        except PanelBuildError as exc:
            print(f"[{label}] SKIPPED: {exc}", file=sys.stderr)
            shards.append({"year": label, "status": "skipped", "reason": str(exc)})
            continue
        panel_path, manifest_path = write_panel(panel, manifest, out_root / label)
        total_rows += panel.height
        print(
            f"[{label}] {panel.height:>10,} rows  {manifest['population']['symbols']:>4} symbols  "
            f"-> {panel_path.relative_to(out_root)}"
        )
        shards.append(
            {
                "year": label,
                "status": "built",
                "rows": panel.height,
                "symbols": manifest["population"]["symbols"],
                "manifest": str(manifest_path.relative_to(out_root)),
                "config_hash": manifest["config_hash"],
            }
        )

    index = {
        "artifact": "cross_venue_panel_index",
        "date_bounds": {"start": args.start.isoformat(), "end_exclusive": args.end.isoformat()},
        "execution_delay_ms": args.execution_delay_ms,
        "total_rows": total_rows,
        "shards": shards,
    }
    (out_root / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\n{total_rows:,} rows across {sum(1 for s in shards if s['status'] == 'built')} shards -> {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
