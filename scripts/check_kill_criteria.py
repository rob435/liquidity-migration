#!/usr/bin/env python3
"""Weekly sleeve kill-criteria check (read-only, no operational authority).

Prints the K1/K2/K3 trip report from the canonical journal per
docs/preregistration/sleeve_kill_criteria_2026-07-20.md. Exit code 0 on
NO TRIP, 3 on any tripped criterion (so the weekly cadence can alert).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.sleeve_kill_criteria import (  # noqa: E402
    evaluate_kill_criteria_for_root,
)


DEFAULT_OPERATIONAL_PROFILE = REPO / "configs" / "operational.demo.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument(
        "--operational-profile",
        type=Path,
        default=DEFAULT_OPERATIONAL_PROFILE,
        help=(
            "committed operational profile whose capital_reference_usdt scales "
            "the registered K1 percentages (default: configs/operational.demo.json)"
        ),
    )
    args = parser.parse_args(argv)
    report = evaluate_kill_criteria_for_root(
        str(args.account_root.expanduser().resolve()),
        operational_profile_path=args.operational_profile.expanduser().resolve(),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 3 if report["tripped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
