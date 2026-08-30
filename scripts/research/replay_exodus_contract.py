#!/usr/bin/env python3
"""Replay typed Exodus decisions without publishing a live target book."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from liquidity_migration.research.backtest.exodus_contract import (
    render_exodus_replay_report,
    replay_exodus_contract_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay typed Exodus events, prior state, engine projections, and effective "
            "config through the live decide_exodus reducer. This checks exact state and "
            "target bytes; event tapes and minute klines do not prove venue fills."
        )
    )
    parser.add_argument("--input", required=True, help="Typed Exodus replay JSON")
    parser.add_argument(
        "--output",
        default="",
        help="Optional canonical JSON report path; stdout is used when omitted",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = replay_exodus_contract_file(args.input)
    rendered = render_exodus_replay_report(report)
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(rendered)
        print(
            json.dumps(
                {
                    "name": report["name"],
                    "output": str(output.resolve()),
                    "steps": len(report["steps"]),
                },
                sort_keys=True,
            ),
            flush=True,
        )
    else:
        print(rendered.decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
