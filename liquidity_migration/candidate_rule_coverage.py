"""Source-bound exact coverage between the frozen population and demo rules."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Mapping

from .account_candidate_universe import load_candidate_universe
from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json


CANDIDATE_RULE_COVERAGE_SCHEMA_VERSION = 1
CANDIDATE_RULE_COVERAGE_KIND = "account_candidate_demo_rule_coverage"
REGISTERED_MAX_RULE_AGE_SECONDS = 7 * 24 * 60 * 60


def require_registered_rule_age(max_rule_age_seconds: float) -> float:
    """Return a finite freshness bound that cannot weaken the natural gate."""

    observed = float(max_rule_age_seconds)
    if not math.isfinite(observed) or observed <= 0.0:
        raise ValueError("max_rule_age_seconds must be finite and positive")
    if observed > REGISTERED_MAX_RULE_AGE_SECONDS:
        raise ValueError(
            "max_rule_age_seconds cannot exceed the registered 604800-second maximum"
        )
    return observed


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({**dict(payload), "artifact_sha256": ""})
    ).hexdigest()


def _snapshot_signature(snapshot: StableFileSnapshot) -> tuple[object, ...]:
    return (
        snapshot.path,
        snapshot.data,
        snapshot.device,
        snapshot.inode,
        snapshot.mode,
        snapshot.uid,
        snapshot.mtime_ns,
        snapshot.nlink,
    )


def _read_regular(
    path: str | Path,
    *,
    label: str,
    require_private: bool = False,
) -> StableFileSnapshot:
    snapshot = read_stable_file(
        path,
        label=label,
        require_single_link=False,
    )
    if require_private and snapshot.mode & 0o077:
        raise ValueError(f"{label} must have mode 0600")
    if require_private and snapshot.uid != os.geteuid():
        raise ValueError(f"{label} must be owned by the verifier")
    return snapshot


def _use_snapshot(
    path: str | Path,
    *,
    label: str,
    require_private: bool,
    snapshot: StableFileSnapshot | None,
) -> StableFileSnapshot:
    if snapshot is None:
        return _read_regular(path, label=label, require_private=require_private)
    if snapshot.path != Path(path).expanduser().absolute():
        raise ValueError(f"{label} snapshot path differs")
    if require_private and (
        snapshot.mode & 0o077 or snapshot.uid != os.geteuid()
    ):
        raise ValueError(f"{label} must be verifier-owned mode 0600")
    return snapshot


def build_candidate_rule_coverage(
    candidate_path: str | Path,
    demo_rules_path: str | Path,
    *,
    created_ts_ns: int | None = None,
    validation_now_ns: int | None = None,
    max_rule_age_seconds: float = REGISTERED_MAX_RULE_AGE_SECONDS,
    candidate_snapshot: StableFileSnapshot | None = None,
    demo_rules_snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Reopen both sources and prove one accepted rule per frozen symbol."""

    from .account_execution_config import load_demo_rules_bytes

    max_rule_age_seconds = require_registered_rule_age(max_rule_age_seconds)
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= 0:
        raise ValueError("created_ts_ns must be positive")
    candidate_snapshot = _use_snapshot(
        candidate_path,
        label="candidate-universe artifact",
        require_private=True,
        snapshot=candidate_snapshot,
    )
    candidate = load_candidate_universe(
        candidate_snapshot.path,
        snapshot=candidate_snapshot,
    )
    rules_snapshot = _use_snapshot(
        demo_rules_path,
        label="demo-rule receipt",
        require_private=True,
        snapshot=demo_rules_snapshot,
    )
    rules_resolved = rules_snapshot.path
    rules_data = rules_snapshot.data
    try:
        rules_payload = json.loads(rules_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("demo-rule receipt is not valid UTF-8 JSON") from exc
    if not isinstance(rules_payload, Mapping):
        raise ValueError("demo-rule receipt must be a JSON object")
    rules = load_demo_rules_bytes(
        rules_data,
        now_ns=validation_now_ns,
        max_age_seconds=max_rule_age_seconds,
    )
    rules_snapshot_after = _read_regular(
        rules_resolved,
        label="demo-rule receipt",
        require_private=True,
    )
    if _snapshot_signature(rules_snapshot_after) != _snapshot_signature(
        rules_snapshot
    ):
        raise RuntimeError("demo-rule receipt changed during validation")
    candidate_symbols = list(candidate.symbols)
    rule_symbols = sorted(rules)
    if rule_symbols != candidate_symbols:
        missing = sorted(set(candidate_symbols) - set(rule_symbols))
        extra = sorted(set(rule_symbols) - set(candidate_symbols))
        raise ValueError(
            "demo-rule receipt does not exactly cover candidate universe "
            f"(missing={missing[:20]!r}, extra={extra[:20]!r})"
        )
    source = rules_payload.get("symbol_source")
    if not isinstance(source, Mapping):
        raise ValueError("demo-rule receipt lacks candidate symbol_source binding")
    expected_source = {
        "kind": "candidate_universe_artifact",
        "path": str(candidate.path),
        "size_bytes": candidate_snapshot.size,
        "sha256": candidate.file_sha256,
        "artifact_sha256": candidate.artifact_sha256,
        "artifact_self_hash_verified": True,
    }
    for field, expected in expected_source.items():
        if source.get(field) != expected:
            raise ValueError(f"demo-rule symbol_source {field} does not bind candidate artifact")
    candidate_snapshot_after = _read_regular(
        candidate.path,
        label="candidate-universe artifact",
        require_private=True,
    )
    if _snapshot_signature(candidate_snapshot_after) != _snapshot_signature(
        candidate_snapshot
    ):
        raise RuntimeError("candidate-universe artifact changed during validation")
    payload: dict[str, Any] = {
        "schema_version": CANDIDATE_RULE_COVERAGE_SCHEMA_VERSION,
        "kind": CANDIDATE_RULE_COVERAGE_KIND,
        "created_ts_ns": created,
        "status": "passed",
        "candidate_universe": {
            "path": str(candidate.path),
            "file_sha256": candidate.file_sha256,
            "artifact_sha256": candidate.artifact_sha256,
            "symbol_count": len(candidate_symbols),
        },
        "demo_rules": {
            "path": str(rules_resolved),
            "file_sha256": rules_snapshot.sha256,
            "artifact_sha256": str(rules_payload.get("artifact_sha256") or ""),
            "verified_ts_ns": int(rules_payload.get("verified_ts_ns") or 0),
            "symbol_count": len(rule_symbols),
        },
        "symbols": candidate_symbols,
        "coverage": {
            "candidate_symbols": len(candidate_symbols),
            "rule_symbols": len(rule_symbols),
            "missing": 0,
            "extra": 0,
            "accepted_probe_evidence_per_symbol": True,
            "symbol_source_bound": True,
        },
        "limitations": [
            "rule_acceptance_does_not_prove_future_order_acceptance",
            "candidate_population_is_forward_point_in_time_not_historical_pit",
            "receipt_does_not_authorize_owner_start_or_deployment",
        ],
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload
