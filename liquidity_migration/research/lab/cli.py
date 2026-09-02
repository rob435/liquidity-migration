"""Shell entry for the lab: dump the root datasets once, build the daily panel.

    python -m liquidity_migration.research.lab.cli dump --data-root ~/SHARED_DATA/bybit_full_pit --out LAB
    python -m liquidity_migration.research.lab.cli panel --inputs LAB/inputs --out LAB/panel/daily.parquet

Dates are UTC days; ``--end`` is exclusive.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Sequence

from liquidity_migration.research.lab.dumps import DEFAULT_DATASETS, dump_inputs
from liquidity_migration.research.lab.panel import build_daily_panel


def _day_ms(text: str | None) -> int | None:
    if text is None:
        return None
    day = dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(day.timestamp() * 1000)


def _day_text(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m liquidity_migration.research.lab.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    dump = sub.add_parser("dump", help="read each dataset from the point-in-time root once and keep it as one parquet")
    dump.add_argument("--data-root", required=True, type=Path)
    dump.add_argument("--out", required=True, type=Path, help="lab directory; dumps go under OUT/inputs/")
    dump.add_argument("--datasets", nargs="+", default=list(DEFAULT_DATASETS))
    dump.add_argument("--start", default=None, help="YYYY-MM-DD, inclusive")
    dump.add_argument("--end", default=None, help="YYYY-MM-DD, exclusive")
    dump.add_argument("--force", action="store_true", help="rewrite a dump that already exists")

    panel = sub.add_parser("panel", help="build the daily symbol x day panel from the dumps")
    panel.add_argument("--inputs", required=True, type=Path, help="the OUT/inputs directory a dump wrote")
    panel.add_argument("--out", required=True, type=Path, help="parquet path for the panel")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "dump":
        written = dump_inputs(
            args.data_root, args.out, datasets=args.datasets,
            start_ms=_day_ms(args.start), end_ms=_day_ms(args.end), force=args.force,
        )
        for name, path in written.items():
            print(f"{name}: {path}")
        return 0
    panel = build_daily_panel(args.inputs, args.out)
    days = panel["day"]
    print(
        f"{panel.height} rows, {panel['symbol'].n_unique()} symbols, "
        f"{_day_text(int(days.min()))} to {_day_text(int(days.max()))} -> {args.out}"  # type: ignore[arg-type]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
