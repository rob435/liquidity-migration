#!/usr/bin/env python3
"""Publish captured post-T1 demo zero targets and bind their safety manifest."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.account_route import require_account_route  # noqa: E402
from liquidity_migration.natural_safety_flatten import (  # noqa: E402
    publish_natural_safety_flatten,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-demo-safety-flatten",
        action="store_true",
        help="required target-publication handshake; this never grants venue credentials",
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--inbox-root", required=True)
    parser.add_argument("--freeze-id", required=True)
    parser.add_argument("--t1-ns", required=True, type=int)
    parser.add_argument("--target-capture", required=True)
    parser.add_argument("--manifest-output", required=True)
    parser.add_argument("--max-owner-health-age-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    if not args.confirm_demo_safety_flatten:
        parser.error("--confirm-demo-safety-flatten is required")
    if (
        not math.isfinite(args.max_owner_health_age_seconds)
        or args.max_owner_health_age_seconds <= 0.0
    ):
        parser.error("--max-owner-health-age-seconds must be finite and positive")
    max_age_ns = int(args.max_owner_health_age_seconds * 1_000_000_000)
    if max_age_ns <= 0:
        parser.error("--max-owner-health-age-seconds is below nanosecond precision")
    if max_age_ns > 30_000_000_000:
        parser.error(
            "--max-owner-health-age-seconds cannot exceed the registered 30 seconds"
        )

    route = require_account_route(
        account_id=args.account_id,
        environment="demo",
        account_root=args.account_root,
        inbox_root=args.inbox_root,
    )
    result = publish_natural_safety_flatten(
        route=route,
        freeze_id=args.freeze_id,
        t1_ns=args.t1_ns,
        target_capture_path=args.target_capture,
        manifest_output_path=args.manifest_output,
        max_owner_health_age_ns=max_age_ns,
    )
    payload = {
        "status": "passed" if result.passed else "publication_failed",
        "scope": "post_window_target_publication_only",
        "already_flat": result.already_flat,
        "active_component_count": result.active_component_count,
        "published_request_ids": list(result.published_request_ids),
        "published_batch_ids": list(result.published_batch_ids),
        "capture_event_ids": list(result.capture_event_ids),
        "errors": [
            {
                "stage": error.stage,
                "target_key": error.target_key,
                "error_type": error.error_type,
                "message": error.message,
            }
            for error in result.errors
        ],
        "target_capture": str(result.target_capture_path),
        "manifest": str(result.manifest_path) if result.manifest_path else "",
        "execution_authorization": "not_granted",
        "convergence_claimed": False,
        "final_flatness_claimed": False,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
