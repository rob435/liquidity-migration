#!/usr/bin/env python3
"""Report whether the demo and paper fleets want, and hold, the same book.

Exits 0 when they agree, 1 when they do not, 2 on a usage error, so it works as
a standalone gate as well as through the liveness watchdog.

Reading the tapes verifies their whole hash chain — a few seconds at current
sizes, growing linearly. If that stops fitting the watchdog's timer, the fix is
a resumable cursor like the mirror's, not skipping verification.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.demo_paper_agreement import (  # noqa: E402
    DEFAULT_RELATIVE_TOLERANCE,
    evaluate_demo_paper_agreement,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-capture-root", required=True)
    parser.add_argument("--paper-capture-root", required=True)
    parser.add_argument(
        "--demo-account-root",
        default="",
        help="Optional. Adds the accepted-quantity comparison.",
    )
    parser.add_argument("--paper-account-root", default="")
    parser.add_argument("--sleeve", action="append", default=None)
    parser.add_argument("--relative-tolerance", type=float, default=DEFAULT_RELATIVE_TOLERANCE)
    args = parser.parse_args(argv)
    if not (0.0 <= args.relative_tolerance < 1.0):
        parser.error("--relative-tolerance must be in [0, 1)")
    if bool(args.demo_account_root) != bool(args.paper_account_root):
        parser.error("--demo-account-root and --paper-account-root go together")

    report = evaluate_demo_paper_agreement(
        demo_tape=Path(args.demo_capture_root) / "strategy-targets.jsonl",
        paper_tape=Path(args.paper_capture_root) / "strategy-targets.jsonl",
        sleeves=args.sleeve or ("carry",),
        relative_tolerance=args.relative_tolerance,
        demo_account_root=args.demo_account_root or None,
        paper_account_root=args.paper_account_root or None,
    )
    print(f"sleeves={','.join(report.sleeves)} scale={report.applied_scale:g}")
    print(f"demo  book: {', '.join(report.demo_symbols) or '(flat)'}")
    print(f"paper book: {', '.join(report.paper_symbols) or '(flat)'}")
    if report.unreadable:
        print(f"UNREADABLE {report.unreadable}")
        return 1
    if report.agree:
        print("AGREE")
        return 0
    for item in report.disagreements:
        print(f"DISAGREE [{item.kind}] {item.describe()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
