#!/usr/bin/env python3
"""Rebind still-fresh demo-rule evidence to an equal/subset candidate artifact."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.candidate_rule_coverage import (  # noqa: E402
    CandidateRuleRefreshRequired,
    REGISTERED_MAX_RULE_AGE_SECONDS,
    project_demo_rules_to_candidate_subset,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-file", required=True)
    parser.add_argument("--prior-rules-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    started_ns = time.time_ns()
    try:
        output = project_demo_rules_to_candidate_subset(
            args.candidate_file,
            args.prior_rules_file,
            args.output,
            validation_now_ns=started_ns,
            max_rule_age_seconds=REGISTERED_MAX_RULE_AGE_SECONDS,
        )
    except CandidateRuleRefreshRequired as exc:
        print(
            json.dumps(
                {
                    "status": "fresh_probe_required",
                    "reason": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    print(
        json.dumps(
            {
                "status": "demo_rules_projected",
                "path": str(output),
                "elapsed_seconds": round((time.time_ns() - started_ns) / 1e9, 3),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
