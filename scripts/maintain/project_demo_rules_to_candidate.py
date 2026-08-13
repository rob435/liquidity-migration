#!/usr/bin/env python3
"""Rebind still-fresh demo-rule evidence to an equal/subset candidate artifact."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.ops.candidate_rule_coverage import (  # noqa: E402
    CandidateRuleRefreshRequired,
    REGISTERED_MAX_RULE_AGE_SECONDS,
    project_demo_rules_to_candidate_subset,
)
from liquidity_migration.account.account_route import require_account_route  # noqa: E402
from liquidity_migration.account.execution_environment import (  # noqa: E402
    account_id_for_environment,
)
from liquidity_migration.strategy.account_candidate_universe import (  # noqa: E402
    account_exposure_labels,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-file", required=True)
    parser.add_argument("--prior-rules-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--held-exposure-account-root",
        default="",
        help=(
            "Demo account journal root. Symbols the account still has exposure "
            "on keep their prior rule and probe evidence through the "
            "projection, so their remaining exits can still be built."
        ),
    )
    parser.add_argument(
        "--held-exposure-inbox-root",
        default="",
        help="Intent inbox root paired with --held-exposure-account-root.",
    )
    args = parser.parse_args(argv)

    if bool(args.held_exposure_account_root) != bool(args.held_exposure_inbox_root):
        parser.error(
            "--held-exposure-account-root and --held-exposure-inbox-root "
            "must be passed together"
        )
    exposure_symbols: set[str] = set()
    if args.held_exposure_account_root:
        account_root = Path(args.held_exposure_account_root).expanduser()
        if account_root.exists():
            # Any failure here is a hard stop: a projection that silently
            # drops a held symbol's rules is the wedge this scan prevents.
            route = require_account_route(
                account_id=account_id_for_environment("demo"),
                environment="demo",
                account_root=account_root,
                inbox_root=Path(args.held_exposure_inbox_root).expanduser(),
            )
            exposure_symbols = set(account_exposure_labels(route=route))

    started_ns = time.time_ns()
    try:
        output = project_demo_rules_to_candidate_subset(
            args.candidate_file,
            args.prior_rules_file,
            args.output,
            validation_now_ns=started_ns,
            max_rule_age_seconds=REGISTERED_MAX_RULE_AGE_SECONDS,
            held_exposure_symbols=sorted(exposure_symbols),
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
