#!/usr/bin/env python3
"""Verify old runtime authority for shutdown when only demo rules have expired.

This helper is deliberately narrower than normal runtime verification.  It
accepts an otherwise byte-exact operational receipt whose bound demo-rule
artifact is genuinely older than the registered freshness window, solely so a
guarded rollout can prove the current topology and stop it.  Future-dated rules
and every other verification failure remain fatal.  Activation and new
authority issuance always use the normal strict verifier.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import liquidity_migration.operational_runtime_authority as authority  # noqa: E402
from liquidity_migration.candidate_rule_coverage import (  # noqa: E402
    REGISTERED_MAX_RULE_AGE_SECONDS,
)


_STALE_RULE_ERROR = "demo rules receipt is stale or future-dated"


def verify_rollout_shutdown_authority(
    *,
    receipt_path: str | Path,
    repo_root: str | Path,
    machine_id_path: str | Path = Path("/etc/machine-id"),
    now_ns: int | None = None,
    authority_module: Any = authority,
) -> dict[str, Any]:
    """Run strict verification, relaxing only proven rule expiry for shutdown."""

    arguments = {
        "receipt_path": receipt_path,
        "repo_root": repo_root,
        "machine_id_path": machine_id_path,
        "unit": None,
    }
    try:
        return authority_module.verify_operational_authorization(**arguments)
    except ValueError as exc:
        if str(exc) != _STALE_RULE_ERROR:
            raise

    _receipt_snapshot, payload = authority_module._load_receipt(receipt_path)
    profile = str(payload.get("profile") or "")
    _environment_snapshots, values = authority_module._parse_environment_snapshots(
        profile
    )
    rules_path = Path(
        values["account-execution.env"]["ACCOUNT_DEMO_RULES_FILE"]
    ).expanduser()
    rules_snapshot = authority_module.read_stable_file(
        rules_path,
        label="rollout shutdown demo-rule receipt",
        reject_empty=True,
        require_mode=0o600,
        require_owner=True,
    )
    try:
        rules_payload = json.loads(rules_snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("rollout shutdown demo-rule receipt is invalid JSON") from exc
    if not isinstance(rules_payload, dict):
        raise ValueError("rollout shutdown demo-rule receipt must contain an object")
    verified_ts_ns = int(rules_payload.get("verified_ts_ns") or 0)
    observed_now_ns = time.time_ns() if now_ns is None else int(now_ns)
    age_ns = observed_now_ns - verified_ts_ns
    maximum_age_ns = REGISTERED_MAX_RULE_AGE_SECONDS * 1_000_000_000
    if verified_ts_ns <= 0 or age_ns < 0:
        raise ValueError("future-dated demo rules cannot authorize rollout shutdown")
    if age_ns <= maximum_age_ns:
        raise RuntimeError(
            "strict authority verification reported stale rules inside the registered window"
        )

    original_coverage = authority_module.build_candidate_rule_coverage

    def verify_expired_coverage(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["validation_now_ns"] = verified_ts_ns
        return original_coverage(*args, **kwargs)

    authority_module.build_candidate_rule_coverage = verify_expired_coverage
    try:
        return authority_module.verify_operational_authorization(**arguments)
    finally:
        authority_module.build_candidate_rule_coverage = original_coverage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--machine-id-path",
        type=Path,
        default=Path("/etc/machine-id"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = verify_rollout_shutdown_authority(
            receipt_path=args.receipt,
            repo_root=args.repo_root,
            machine_id_path=args.machine_id_path,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"rollout shutdown authority failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
