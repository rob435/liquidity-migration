#!/usr/bin/env python3
"""R3a loss-budget SHADOW check (read-only oneshot; staged, not installed).

Prints the shadow decision JSON for one account root's current UTC day:
realized day P&L vs the frozen -1.5% budget, first-breach time, the frozen
A/B arm for the day, and what an armed governor WOULD do. Acts on nothing.

Intended future wiring (operator go required): a paper-root systemd timer in
the kill-criteria pattern appending rows to a shadow JSONL. Until then this
is a manual/read-only tool.

Usage: python scripts/loss_budget_shadow_check.py --account-root PATH [--at ISO_UTC]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration.loss_budget_shadow import evaluate_for_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--at", default=None, help="evaluate as of this UTC instant (ISO); default now")
    args = parser.parse_args()
    now_utc = (
        dt.datetime.fromisoformat(args.at).astimezone(dt.timezone.utc)
        if args.at
        else dt.datetime.now(tz=dt.timezone.utc)
    )
    report = evaluate_for_root(args.account_root, now_utc=now_utc)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
