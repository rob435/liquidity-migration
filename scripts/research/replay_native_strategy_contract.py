#!/usr/bin/env python3
"""Replay checked-in directional fixtures through the native Rust reducers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from liquidity_migration.research.backtest.exodus_contract import (
    render_exodus_replay_report,
    replay_exodus_contract_file,
)
from liquidity_migration.research.backtest.native_directional_contract import (
    render_native_fixture_report,
    replay_native_fixture_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a LONG, CARRY, or Exodus fixture through one persistent Rust "
            "strategy-contract process without publishing orders."
        )
    )
    parser.add_argument("--sleeve", required=True, choices=("long", "carry", "exodus"))
    parser.add_argument("--input", required=True, help="Checked-in typed replay fixture")
    parser.add_argument("--output", default="", help="Optional canonical JSON report path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sleeve == "exodus":
        report = replay_exodus_contract_file(args.input)
        rendered = render_exodus_replay_report(report)
    else:
        report = replay_native_fixture_file(args.input, sleeve=args.sleeve)
        rendered = render_native_fixture_report(report)
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(rendered)
        print(
            json.dumps(
                {"name": report["name"], "output": str(output.resolve()), "sleeve": args.sleeve},
                sort_keys=True,
            ),
            flush=True,
        )
    else:
        print(rendered.decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
