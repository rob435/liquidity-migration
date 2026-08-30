#!/usr/bin/env python3
"""Rebuild native LONG with the shared live policy and minute-bound fills."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from liquidity_migration.research.backtest.long_live_physics import (
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_TAKER_FEE_BPS,
    EvidenceProvenance,
    LivePhysicsAssumptions,
    run_long_live_physics_research,
)
from liquidity_migration.rules.long_native import LONG_STRATEGY_PROFILE_CHOICES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay native LONG through the shared decision contract with Bybit "
            "PIT 1h signals and candidate-window 1m execution. The result is a "
            "minute execution bound, not tick/L1 parity."
        )
    )
    parser.add_argument("--data-root", required=True, help="Bybit full-PIT root")
    parser.add_argument("--start", required=True, help="Signal start date, inclusive")
    parser.add_argument("--end", required=True, help="Signal end date, exclusive")
    parser.add_argument("--profile", choices=LONG_STRATEGY_PROFILE_CHOICES, default="v12")
    parser.add_argument(
        "--operational-profile",
        default="configs/operational.mainnet.json",
        help="Typed fleet profile supplying current LONG sizing and throttle",
    )
    parser.add_argument(
        "--initial-equity-usdt",
        type=float,
        required=True,
        help=(
            "Declared starting account equity used by live sizing and absolute "
            "dead bands; use a verified live value or name the normalized scale"
        ),
    )
    parser.add_argument("--taker-fee-bps", type=float, default=DEFAULT_TAKER_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument(
        "--venue-min-notional-usdt",
        type=float,
        default=5.0,
        help=(
            "Supplied current venue minimum used in max($1, 5%% standing, venue minimum); "
            "historical instrument-rule changes are not reconstructed"
        ),
    )
    parser.add_argument("--report-dir", default=None)
    parser.add_argument(
        "--shaped-data",
        default=(
            "The LONG rule and this execution rebuild were shaped using prior Bybit "
            "history, including the historical surface replayed here."
        ),
    )
    parser.add_argument(
        "--graded-data",
        default="none; this is a seen-data rebuild",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    assumptions = LivePhysicsAssumptions(
        initial_equity_usdt=args.initial_equity_usdt,
        taker_fee_bps=args.taker_fee_bps,
        slippage_bps=args.slippage_bps,
        venue_min_notional_usdt=args.venue_min_notional_usdt,
        evidence=EvidenceProvenance(
            lane="lane_1_exploratory",
            shaped_data=args.shaped_data,
            graded_data=args.graded_data,
        ),
    )
    command = [str(Path(sys.argv[0]).resolve()), *(argv if argv is not None else sys.argv[1:])]
    report = run_long_live_physics_research(
        args.data_root,
        profile_name=args.profile,
        operational_profile_path=args.operational_profile,
        start=args.start,
        end=args.end,
        report_dir=args.report_dir,
        assumptions=assumptions,
        command=command,
    )
    print(
        json.dumps(
            {
                "run_label": report["run_label"],
                "tainted": report["tainted"],
                "summary": report["summary"],
                "report_dir": report["report_dir"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
