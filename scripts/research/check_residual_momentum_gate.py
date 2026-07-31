#!/usr/bin/env python3
"""Validate that a live residual-momentum gate has usable stable rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.residual_momentum import inspect_gate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-stable-age-days", type=float, default=2.0)
    parser.add_argument("--min-current-stable-symbols", type=int, default=20)
    args = parser.parse_args()
    try:
        result = inspect_gate(
            Path(args.path).expanduser(),
            max_stable_age_days=args.max_stable_age_days,
            min_current_stable_symbols=args.min_current_stable_symbols,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must return one actionable gate failure
        print(f"rmom gate invalid: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
