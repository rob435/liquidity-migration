#!/usr/bin/env python3
"""Build a source-bound exact candidate-universe/demo-rule coverage receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.candidate_rule_coverage import (  # noqa: E402
    build_candidate_rule_coverage,
    require_registered_rule_age,
    write_candidate_rule_coverage,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-universe", required=True)
    parser.add_argument("--demo-rules", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-rule-age-hours", type=float, default=168.0)
    args = parser.parse_args(argv)
    try:
        max_rule_age_seconds = require_registered_rule_age(
            args.max_rule_age_hours * 3600.0
        )
    except ValueError as exc:
        parser.error(str(exc))
    payload = build_candidate_rule_coverage(
        args.candidate_universe,
        args.demo_rules,
        max_rule_age_seconds=max_rule_age_seconds,
    )
    output = write_candidate_rule_coverage(args.output, payload)
    print(json.dumps({
        "status": "candidate_rule_coverage_passed",
        "output": str(output),
        "symbols": len(payload["symbols"]),
        "artifact_sha256": payload["artifact_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
