#!/usr/bin/env python3
"""Measure the paper execution twin's price error against real demo fills.

Reports twin optimism in basis points: positive means the model filled at a
better price than the venue did, which is the direction that turns a paper edge
into a live loss.

Only meaningful once the paper fleet mirrors the demo fleet's targets: before
that the two never executed the same decision, so there is nothing to match and
the script says so rather than comparing unrelated trades.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.execution_twin_calibration import (  # noqa: E402
    calibration_report,
    load_states,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-account-root", required=True)
    parser.add_argument("--paper-account-root", required=True)
    parser.add_argument("--demo-account-id", default="bybit-demo-unified")
    parser.add_argument("--paper-account-id", default="bybit-paper-unified")
    parser.add_argument("--json", action="store_true", help="emit the full matched sample")
    args = parser.parse_args(argv)

    demo_state, paper_state = load_states(
        demo_account_root=args.demo_account_root,
        paper_account_root=args.paper_account_root,
        demo_account_id=args.demo_account_id,
        paper_account_id=args.paper_account_id,
    )
    report = calibration_report(demo_state, paper_state)
    if args.json:
        print(
            json.dumps(
                {
                    "matched_pairs": report.matched_pairs,
                    "demo_only": report.demo_only,
                    "paper_only": report.paper_only,
                    "optimism_bps_mean": report.optimism_bps_mean,
                    "optimism_bps_median": report.optimism_bps_median,
                    "optimism_bps_p90": report.optimism_bps_p90,
                    "fee_bps_demo_mean": report.fee_bps_demo_mean,
                    "fee_bps_paper_mean": report.fee_bps_paper_mean,
                    "by_symbol": dict(report.by_symbol),
                    "executions": [
                        {
                            "batch_id": item.batch_id,
                            "symbol": item.symbol,
                            "demo_vwap": item.demo_vwap,
                            "paper_vwap": item.paper_vwap,
                            "optimism_bps": item.optimism_bps,
                            "quantity_ratio": item.quantity_ratio,
                        }
                        for item in report.executions
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(report.summary())
    if report.matched_pairs:
        print("\nper symbol (mean twin optimism, bp):")
        for symbol, value in report.by_symbol.items():
            print(f"  {symbol:12s} {value:+8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
