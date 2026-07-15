#!/usr/bin/env python3
"""Build the source-derived natural batch scope for account-kernel parity."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.kernel_parity import (  # noqa: E402
    build_comparison_scope,
    write_comparison_scope,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captured-account-replay-receipt", required=True)
    parser.add_argument("--event-parity-receipt", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        payload = build_comparison_scope(
            captured_account_replay_receipt=args.captured_account_replay_receipt,
            event_parity_receipt=args.event_parity_receipt,
        )
        output = write_comparison_scope(args.output, payload)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"kernel-parity scope failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "batch_count": len(payload["batch_ids"]),
                "captured_account_replay_receipt": payload[
                    "captured_account_replay_receipt"
                ],
                "event_parity_receipt": payload["event_parity_receipt"],
                "output": str(output),
                "scope_artifact_sha256": payload["artifact_sha256"],
                "status": "valid",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
