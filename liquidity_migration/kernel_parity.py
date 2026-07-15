"""Machine gate for scoped account-kernel strategy-to-order-plan parity.

The actual demo account and the deterministic execution twin are not expected
to produce identical acknowledgements, fill partitions, prices, fees, or P&L.
This module therefore compares only a source-bound strategy-to-order-plan slice
across historical, paper, and demo journals.  It retains execution/accounting
facts in classified source counts and exposes a separate historical-versus-
paper modeled-execution diagnostic; neither is folded into the main gate.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import stat
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .account_kernel import (
    AccountEvent,
    AccountEventType,
    account_journal_path,
    account_transactions_path,
    read_account_journal_bytes,
)
from .deterministic_serialization import canonical_json
from .execution_twin_calibration import verify_calibration_receipt
from .natural_effective_config import (
    load_effective_runtime_config_bundle_binding,
)


KERNEL_PARITY_SCHEMA_VERSION = 4
KERNEL_PARITY_CONTRACT_ID = "account-kernel-strategy-order-plan-parity-v4"
KERNEL_PARITY_EVIDENCE_SCOPE = "account_kernel_strategy_to_order_plan_parity"
COMPARISON_SCOPE_SCHEMA_VERSION = 3
COMPARISON_SCOPE_CONTRACT_ID = "account-kernel-parity-batch-scope-v3"
QUANTITY_ABS_TOLERANCE = 1e-12
REQUIRED_ENVIRONMENTS = ("historical", "paper", "demo")

_PLAN_EVENT_TYPES = frozenset(
    {
        AccountEventType.DECISION.value,
        AccountEventType.TARGET.value,
        AccountEventType.RISK_DECISION.value,
        AccountEventType.ORDER_COMMAND.value,
    }
)
_MODELED_EXECUTION_EVENT_TYPES = frozenset(
    {
        AccountEventType.ACK.value,
        AccountEventType.FILL.value,
        AccountEventType.ORDER_STATUS.value,
        AccountEventType.CLOSE.value,
        AccountEventType.PNL.value,
    }
)
_OWNER_BATCH_PREFIXES = (
    "account-convergence/",
    "external-protection/",
    "external-reduction/",
)
_EVIDENCE_ARGUMENTS = {
    "event_parity_receipt": "event_parity_receipt",
    "fresh_epoch_reset_receipt": "fresh_epoch_reset_receipt",
    "risk_policy_file": "risk_policy_file",
    "rules_file": "rules_file",
    "effective_runtime_config_bundle_file": "effective_runtime_config_bundle_file",
    "twin_calibration_receipt": "twin_calibration_receipt",
}

UNVERIFIED_EXTERNAL_GATES = (
    "actual_market_source_authenticity",
    "actual_demo_execution_twin_drift",
    "venue_closed_pnl_and_funding_reconciliation",
    "source_process_quiescence_operator_evidence",
    "deployment_authorization",
)

NORMALIZATION_CONTRACT = {
    "account_id": "validated_within_each_journal_but_not_compared_across_accounts",
    "command_id": "one_to_one_map_by_semantic_command_key",
    "environment": "not_part_of_strategy_order_plan",
    "event_transport_times": "excluded_from_strategy_order_plan",
    "demo_execution_outcomes": "classified_but_not_compared_for_main_gate",
    "semantic_command_key": [
        "batch_id",
        "symbol",
        "chunk_index",
        "chunk_count",
        "side",
        "reduce_only",
    ],
}


@dataclass(frozen=True, slots=True)
class KernelParityReport:
    passed: bool
    contract_id: str
    quantity_abs_tolerance: float
    compared_environments: tuple[str, ...]
    scoped_batch_ids: tuple[str, ...]
    decision_keys_identical: bool
    target_keys_identical: bool
    target_discrete_fields_identical: bool
    risk_acceptance_and_rejection_keys_identical: bool
    risk_target_presence_identical: bool
    semantic_commands_identical: bool
    quantity_values_within_tolerance: bool
    command_id_mapping_one_to_one: bool
    historical_paper_normalized_modeled_execution_exact: bool
    command_id_mapping: tuple[Mapping[str, Any], ...]
    mismatches: tuple[str, ...]

    def require_passed(self) -> None:
        if not self.passed:
            raise RuntimeError("account-kernel structural plan parity failed: " + "; ".join(self.mismatches))


@dataclass(frozen=True, slots=True)
class _CapturedSource:
    root: Path
    events: tuple[AccountEvent, ...]
    identity: Mapping[str, Any]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lower_sha256(value: Any, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _full_commit(value: Any, *, label: str = "expected_commit") -> str:
    commit = str(value or "")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError(f"{label} must be a full lowercase Git commit")
    return commit


def _accepts_keyword(loader: object, keyword: str) -> bool:
    try:
        return keyword in inspect.signature(loader).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _identity_from_snapshot(snapshot: StableFileSnapshot) -> dict[str, Any]:
    return {
        "path": str(snapshot.path),
        "size_bytes": snapshot.size,
        "sha256": snapshot.sha256,
    }


def _strict_file_snapshot(
    path: str | Path,
    *,
    label: str,
    required_mode: int | None = None,
    require_single_link: bool = False,
    require_owner: bool = False,
) -> StableFileSnapshot:
    candidate = Path(path).expanduser()
    try:
        lexical = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is missing: {candidate}") from exc
    if stat.S_ISLNK(lexical.st_mode):
        raise ValueError(f"{label} must not be a symbolic link: {candidate}")
    return read_stable_file(
        candidate.resolve(strict=True),
        label=label,
        reject_empty=True,
        require_mode=required_mode,
        require_owner=require_owner,
        require_single_link=require_single_link,
    )


def _strict_file_identity(
    path: str | Path,
    *,
    label: str,
    required_mode: int | None = None,
    require_single_link: bool = False,
    require_owner: bool = False,
) -> dict[str, Any]:
    return _identity_from_snapshot(
        _strict_file_snapshot(
            path,
            label=label,
            required_mode=required_mode,
            require_single_link=require_single_link,
            require_owner=require_owner,
        )
    )


def _git_binding(repo_root: str | Path, *, expected_commit: str) -> dict[str, Any]:
    commit = _full_commit(expected_commit)
    try:
        root = Path(repo_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"repository root is missing: {repo_root}") from exc
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")

    def git(*args: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed Git executable and arguments
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"Git identity check failed for {root}")
        return result.stdout.strip()

    top = Path(git("rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != root:
        raise ValueError(f"repository root must be the Git top level: {root}")
    observed = git("rev-parse", "HEAD")
    if observed != commit:
        raise ValueError(f"repository HEAD {observed!r} does not match expected commit {commit!r}")
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("kernel-parity receipt requires a clean Git worktree")
    return {"root": str(root), "commit": commit, "clean": True}


def _journal_authoritative_paths(root: Path) -> tuple[str, tuple[Path, ...]]:
    transactions = account_transactions_path(root)
    transaction_paths = tuple(sorted(transactions.glob("*.json"))) if transactions.is_dir() else ()
    if transaction_paths:
        return "transactions", transaction_paths
    projection = account_journal_path(root)
    if projection.is_file():
        return "jsonl", (projection,)
    raise ValueError(f"account journal source is missing under {root}")


def _journal_raw_snapshot(
    root: str | Path,
) -> tuple[dict[str, Any], tuple[tuple[Path, StableFileSnapshot], ...]]:
    try:
        resolved_root = Path(root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"account root is missing: {root}") from exc
    if not resolved_root.is_dir():
        raise ValueError(f"account root is not a directory: {resolved_root}")
    storage, paths = _journal_authoritative_paths(resolved_root)
    files: list[dict[str, Any]] = []
    snapshots: list[tuple[Path, StableFileSnapshot]] = []
    for path in paths:
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"journal source escapes account root: {resolved}") from exc
        snapshot = read_stable_file(
            resolved,
            label=f"account journal source {relative.as_posix()}",
            require_single_link=False,
        )
        files.append(
            {
                "path": str(snapshot.path),
                "relative_path": relative.as_posix(),
                "size_bytes": snapshot.size,
                "sha256": snapshot.sha256,
            }
        )
        snapshots.append((resolved, snapshot))
    manifest_sha256 = _sha256_bytes(canonical_json({"storage": storage, "files": files}))
    return (
        {
            "root": str(resolved_root),
            "storage": storage,
            "files": files,
            "raw_manifest_sha256": manifest_sha256,
        },
        tuple(snapshots),
    )


def _journal_raw_identity(root: str | Path) -> dict[str, Any]:
    identity, _snapshots = _journal_raw_snapshot(root)
    return identity


def _capture_journal(root: str | Path) -> _CapturedSource:
    before, snapshots = _journal_raw_snapshot(root)
    if before["storage"] == "transactions":
        events = tuple(
            read_account_journal_bytes(
                transaction_files=[(str(path), snapshot.data) for path, snapshot in snapshots],
                verify=True,
            )
        )
    else:
        projection_path, projection_snapshot = snapshots[0]
        events = tuple(
            read_account_journal_bytes(
                projection_data=projection_snapshot.data,
                projection_label=str(projection_path),
                verify=True,
            )
        )
    after = _journal_raw_identity(root)
    if before != after:
        raise RuntimeError(f"account journal mutated while being captured: {before['root']}")
    if not events:
        raise ValueError(f"account parity source has no events: {before['root']}")
    normalized = canonical_json({"events": [event.to_dict() for event in events]})
    identity = {
        **before,
        "event_count": len(events),
        "account_id": events[0].account_id,
        "first_sequence": events[0].sequence,
        "first_event_hash": events[0].event_hash,
        "last_sequence": events[-1].sequence,
        "last_event_hash": events[-1].event_hash,
        "normalized_journal_sha256": _sha256_bytes(normalized),
    }
    return _CapturedSource(Path(cast(str, before["root"])), events, identity)


def _journal_stream_sha256(events: Sequence[AccountEvent]) -> str:
    """Match the immutable journal digest recorded by calibration receipts."""

    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event.to_dict()))
        digest.update(b"\n")
    return digest.hexdigest()


def _calibration_epoch_binding(
    calibration_receipt_path: str | Path,
    *,
    natural_sources: Mapping[str, _CapturedSource],
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Bind a pre-reset calibration epoch without reopening recycled live paths.

    The reset workflow reuses the lexical live account/capture paths for the
    post-reset natural epoch.  Consequently, only the signed receipt bytes and
    their embedded immutable digests are valid here.  Archived-source replay is
    a separate drift-verifier responsibility with an explicit archive mapping.
    """

    if snapshot is None:
        snapshot = _strict_file_snapshot(
            calibration_receipt_path,
            label="execution-twin calibration receipt",
        )
    elif snapshot.path != Path(calibration_receipt_path).expanduser().absolute():
        raise ValueError("execution-twin calibration receipt snapshot path differs")
    try:
        raw = json.loads(snapshot.data)
    except json.JSONDecodeError as exc:
        raise ValueError("execution-twin calibration receipt is unreadable") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("execution-twin calibration receipt must be an object")
    receipt = verify_calibration_receipt(
        raw,
        require_registered_requirements=True,
    )
    if receipt.get("execution_twin_gate_passed") is not True:
        raise ValueError("execution-twin calibration gate has not passed")
    inputs = receipt.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ValueError("execution-twin calibration receipt lacks input bindings")
    calibration_journal_sha256 = _lower_sha256(
        inputs.get("account_journal_sha256"),
        label="calibration account journal hash",
    )
    calibration_last_event_hash = _lower_sha256(
        inputs.get("account_last_event_hash"),
        label="calibration account last event hash",
    )
    capture_manifest_sha256 = _lower_sha256(
        inputs.get("market_capture_manifest_sha256"),
        label="calibration market capture manifest hash",
    )
    raw_manifest = inputs.get("market_capture_manifest")
    if not isinstance(raw_manifest, list) or not raw_manifest:
        raise ValueError("execution-twin calibration receipt has no capture manifest")
    capture_file_hashes: set[str] = set()
    for index, row in enumerate(raw_manifest):
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256"}:
            raise ValueError("execution-twin calibration capture manifest is malformed")
        relative_path = str(row.get("path") or "")
        if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            raise ValueError("execution-twin calibration capture path is not relative")
        digest = _lower_sha256(
            row.get("sha256"),
            label=f"calibration capture file hash {index}",
        )
        if digest in capture_file_hashes:
            raise ValueError("execution-twin calibration capture manifest repeats a file hash")
        capture_file_hashes.add(digest)
    if capture_manifest_sha256 != _sha256_bytes(canonical_json({"files": raw_manifest})):
        raise ValueError("execution-twin calibration capture manifest hash does not reproduce")

    expected_account_id = str(receipt.get("expected_account_id") or "")
    observed_ts_ns = receipt.get("observed_ts_ns")
    if not expected_account_id or type(observed_ts_ns) is not int or observed_ts_ns <= 0:
        raise ValueError("execution-twin calibration epoch identity is malformed")

    natural_journal_hashes = {
        environment: _journal_stream_sha256(source.events) for environment, source in natural_sources.items()
    }
    if calibration_journal_sha256 in set(natural_journal_hashes.values()):
        raise ValueError("calibration and post-reset natural epochs reuse the same journal hash")
    natural_last_hashes = {environment: source.events[-1].event_hash for environment, source in natural_sources.items()}
    if calibration_last_event_hash in set(natural_last_hashes.values()):
        raise ValueError("calibration and post-reset natural epochs reuse the same event-chain head")
    natural_file_hashes = {
        str(file_identity["sha256"])
        for source in natural_sources.values()
        for file_identity in cast(Sequence[Mapping[str, Any]], source.identity["files"])
    }
    if capture_file_hashes & natural_file_hashes:
        raise ValueError("calibration capture and post-reset journal reuse source bytes")

    settings = {
        "requirements": receipt.get("requirements"),
        "latency_ns": receipt.get("latency_ns"),
        "fills": receipt.get("fills"),
        "slippage": receipt.get("slippage"),
        "queue_assumption": receipt.get("queue_assumption"),
    }
    return {
        "epoch": "pre_reset_calibration",
        "receipt_artifact_sha256": _lower_sha256(
            receipt.get("artifact_sha256"),
            label="execution-twin calibration artifact hash",
        ),
        "expected_account_id": expected_account_id,
        "observed_ts_ns": observed_ts_ns,
        "account_journal_sha256": calibration_journal_sha256,
        "account_last_event_hash": calibration_last_event_hash,
        "market_capture_manifest_sha256": capture_manifest_sha256,
        "calibration_settings_sha256": _sha256_bytes(canonical_json(settings)),
        "execution_twin_gate_passed": receipt.get("execution_twin_gate_passed") is True,
        "embedded_source_paths_dereferenced": False,
        "archive_source_revalidation": "delegated_to_execution_twin_drift_gate",
        "batch_overlap_check": "unavailable_calibration_receipt_has_no_batch_ids",
        "natural_journal_sha256": natural_journal_hashes,
        "natural_last_event_hash": natural_last_hashes,
        "hash_overlap_rejected": True,
    }


def _finite_number(value: Any, *, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} must be present and finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _event_batch_id(event: AccountEvent) -> str:
    metadata = event.payload.get("metadata")
    metadata_batch_id = metadata.get("batch_id") if isinstance(metadata, Mapping) else None
    return str(event.payload.get("batch_id") or metadata_batch_id or event.correlation_id or "")


def _is_owner_batch(batch_id: str) -> bool:
    return batch_id.startswith(_OWNER_BATCH_PREFIXES)


def _comparison_scope_self_hash(payload: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json({**dict(payload), "artifact_sha256": ""}))


def _receipt_identity_from_scope(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"path", "size_bytes", "sha256"}:
        raise ValueError(f"comparison scope {label} identity is malformed")
    path = str(value.get("path") or "")
    size = value.get("size_bytes")
    digest = _lower_sha256(value.get("sha256"), label=f"comparison scope {label} hash")
    if not Path(path).is_absolute() or type(size) is not int or size <= 0:
        raise ValueError(f"comparison scope {label} identity is malformed")
    return {"path": path, "size_bytes": size, "sha256": digest}


def _comparison_scope_batch_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("comparison scope requires a non-empty batch_ids list")
    if any(type(item) is not str for item in value):
        raise ValueError("comparison scope batch ids must be strings")
    batch_ids = tuple(cast(str, item) for item in value)
    if any(not item or item.strip() != item for item in batch_ids):
        raise ValueError("comparison scope batch ids must be non-empty canonical strings")
    if len(set(batch_ids)) != len(batch_ids):
        raise ValueError("comparison scope contains duplicate batch ids")
    if any(_is_owner_batch(item) for item in batch_ids):
        raise ValueError("comparison scope cannot name owner-derived batches")
    return batch_ids


def verify_comparison_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reopen both source receipts and reproduce an exact scope payload."""

    value = dict(payload)
    required = {
        "schema_version",
        "contract_id",
        "batch_ids",
        "captured_account_replay_receipt",
        "event_parity_receipt",
        "captured_replay_outputs",
        "event_replay_provenance",
        "effective_runtime_config",
        "artifact_sha256",
    }
    if set(value) != required:
        raise ValueError("comparison scope has the wrong fields")
    if value.get("schema_version") != COMPARISON_SCOPE_SCHEMA_VERSION:
        raise ValueError("unsupported comparison scope schema")
    if value.get("contract_id") != COMPARISON_SCOPE_CONTRACT_ID:
        raise ValueError("comparison scope has the wrong contract id")
    _comparison_scope_batch_ids(value.get("batch_ids"))
    replay_identity = _receipt_identity_from_scope(
        value.get("captured_account_replay_receipt"),
        label="captured-account replay receipt",
    )
    event_identity = _receipt_identity_from_scope(
        value.get("event_parity_receipt"),
        label="event-parity receipt",
    )
    if replay_identity["path"] == event_identity["path"]:
        raise ValueError("comparison scope source receipts must be distinct files")
    if not isinstance(value.get("captured_replay_outputs"), Mapping):
        raise ValueError("comparison scope lacks captured replay outputs")
    event_provenance = value.get("event_replay_provenance")
    if not isinstance(event_provenance, Mapping) or event_provenance.get("deployment_valid") is not True:
        raise ValueError("comparison scope lacks deployment-valid event replay provenance")
    effective_runtime_config = value.get("effective_runtime_config")
    if not isinstance(effective_runtime_config, Mapping):
        raise ValueError("comparison scope lacks its effective runtime configuration")
    if effective_runtime_config.get("execution_authorization") != "not_granted":
        raise ValueError("comparison scope effective configuration grants execution authority")
    observed_hash = _lower_sha256(
        value.get("artifact_sha256"),
        label="comparison scope artifact hash",
    )
    if observed_hash != _comparison_scope_self_hash(value):
        raise ValueError("comparison scope artifact hash mismatch")

    rebuilt = build_comparison_scope(
        captured_account_replay_receipt=replay_identity["path"],
        event_parity_receipt=event_identity["path"],
    )
    if canonical_json(rebuilt) != canonical_json(value):
        raise ValueError("comparison scope does not reproduce from its bound receipts")
    return value


def _load_comparison_scope(
    path: str | Path,
    *,
    expected_event_parity_identity: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...], dict[str, Any], dict[str, Any]]:
    scope_snapshot = _strict_file_snapshot(
        path,
        label="comparison scope file",
        required_mode=0o600,
        require_single_link=True,
        require_owner=True,
    )
    identity = _identity_from_snapshot(scope_snapshot)
    try:
        payload = json.loads(scope_snapshot.data)
    except json.JSONDecodeError as exc:
        raise ValueError("comparison scope file is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("comparison scope file must contain an object")
    verified = verify_comparison_scope(payload)
    event_identity = _receipt_identity_from_scope(
        verified.get("event_parity_receipt"),
        label="event-parity receipt",
    )
    expected_event = _receipt_identity_from_scope(
        expected_event_parity_identity,
        label="supplied event-parity receipt",
    )
    if event_identity != expected_event:
        raise ValueError("comparison scope does not bind the supplied event-parity receipt")
    final_identity = _strict_file_identity(
        identity["path"],
        label="comparison scope file",
        required_mode=0o600,
        require_single_link=True,
        require_owner=True,
    )
    if final_identity != identity:
        raise RuntimeError("comparison scope changed during verification")
    provenance = {
        "captured_account_replay_receipt": dict(cast(Mapping[str, Any], verified["captured_account_replay_receipt"])),
        "event_parity_receipt": dict(cast(Mapping[str, Any], verified["event_parity_receipt"])),
        "captured_replay_outputs": dict(cast(Mapping[str, Any], verified["captured_replay_outputs"])),
        "event_replay_provenance": dict(cast(Mapping[str, Any], verified["event_replay_provenance"])),
        "scope_artifact_sha256": verified["artifact_sha256"],
    }
    return (
        identity,
        _comparison_scope_batch_ids(verified.get("batch_ids")),
        dict(cast(Mapping[str, Any], verified["effective_runtime_config"])),
        provenance,
    )


def build_comparison_scope(
    *,
    captured_account_replay_receipt: str | Path,
    event_parity_receipt: str | Path,
) -> dict[str, Any]:
    """Derive the exact natural batch scope from source-bound replay evidence."""

    # Local imports avoid a module cycle: captured_account_replay uses the
    # journal comparator in this module to produce its immediate diagnostic.
    from .captured_account_replay import load_captured_account_replay_receipt
    from .strategy_event_parity import load_strategy_event_parity_receipt

    replay_snapshot = _strict_file_snapshot(
        captured_account_replay_receipt,
        label="captured-account replay receipt",
        required_mode=0o400,
        require_single_link=True,
        require_owner=True,
    )
    event_snapshot = _strict_file_snapshot(
        event_parity_receipt,
        label="event-parity receipt",
        required_mode=0o600,
        require_single_link=True,
        require_owner=True,
    )
    replay_identity_before = _identity_from_snapshot(replay_snapshot)
    event_identity_before = _identity_from_snapshot(event_snapshot)
    if replay_identity_before["path"] == event_identity_before["path"]:
        raise ValueError("captured-account replay and event-parity receipts must be distinct")
    replay_path = Path(cast(str, replay_identity_before["path"]))
    event_path = Path(cast(str, event_identity_before["path"]))
    if _accepts_keyword(load_captured_account_replay_receipt, "snapshot"):
        replay = load_captured_account_replay_receipt(
            replay_path,
            snapshot=replay_snapshot,
        )
    else:
        replay = load_captured_account_replay_receipt(replay_path)
    if _accepts_keyword(load_strategy_event_parity_receipt, "snapshot"):
        event = load_strategy_event_parity_receipt(
            event_path,
            snapshot=event_snapshot,
        )
    else:
        event = load_strategy_event_parity_receipt(event_path)
    replay_identity_after = _strict_file_identity(
        replay_path,
        label="captured-account replay receipt",
        required_mode=0o400,
        require_single_link=True,
        require_owner=True,
    )
    event_identity_after = _strict_file_identity(
        event_path,
        label="event-parity receipt",
        required_mode=0o600,
        require_single_link=True,
        require_owner=True,
    )
    if replay_identity_after != replay_identity_before or event_identity_after != event_identity_before:
        raise RuntimeError("comparison-scope source receipt changed during verification")
    if replay.get("has_durable_request_batches") is not True:
        raise ValueError("captured-account replay has no durable natural batches")
    effective_runtime_config = replay.get("effective_runtime_config")
    if not isinstance(effective_runtime_config, Mapping):
        raise ValueError("captured-account replay lacks its effective runtime configuration")
    if effective_runtime_config.get("execution_authorization") != "not_granted":
        raise ValueError("captured-account replay effective configuration grants execution authority")
    raw_batch_ids = replay.get("ordered_batch_ids")
    if not isinstance(raw_batch_ids, list) or not raw_batch_ids:
        raise ValueError("captured-account replay lacks an ordered natural batch set")
    if any(type(value) is not str for value in raw_batch_ids):
        raise ValueError("captured-account replay natural batch ids must be strings")
    batch_ids = [cast(str, value) for value in raw_batch_ids]
    if (
        any(not value or value.strip() != value for value in batch_ids)
        or len(set(batch_ids)) != len(batch_ids)
        or any(_is_owner_batch(value) for value in batch_ids)
    ):
        raise ValueError("captured-account replay has an invalid natural batch set")

    replay_sources = replay.get("source_files")
    if not isinstance(replay_sources, Mapping):
        raise ValueError("captured-account replay lacks source identities")
    target_source = replay_sources.get("target_scheduling_capture")
    if not isinstance(target_source, Mapping):
        raise ValueError("captured-account replay lacks its target-capture identity")
    target_path = str(target_source.get("path") or "")
    target_identity = _strict_file_identity(target_path, label="natural target capture")
    if (
        target_identity["path"] != target_path
        or target_identity["size_bytes"] != target_source.get("size")
        or target_identity["sha256"]
        != _lower_sha256(
            target_source.get("sha256"),
            label="natural target-capture file hash",
        )
    ):
        raise ValueError("captured-account replay target-capture source changed")

    event_provenance = event.get("replay_provenance")
    if event.get("strategy_event_replay_gate_passed") is not True:
        raise ValueError("event parity gate did not pass")
    if not isinstance(event_provenance, Mapping) or event_provenance.get("deployment_valid") is not True:
        raise ValueError("event parity lacks deployment-valid target-replay provenance")
    replay_manifest = event_provenance.get("replay_manifest")
    canonical_source = event_provenance.get("canonical_source_capture")
    if not isinstance(replay_manifest, Mapping) or not isinstance(canonical_source, Mapping):
        raise ValueError("event parity lacks its verified target-replay manifest binding")
    source_matches = (
        canonical_source.get("path") == target_identity["path"]
        and canonical_source.get("size_bytes") == target_identity["size_bytes"]
        and canonical_source.get("sha256") == target_identity["sha256"]
        and canonical_source.get("device") == target_source.get("device")
        and canonical_source.get("inode") == target_source.get("inode")
        and canonical_source.get("mtime_ns") == target_source.get("mtime_ns")
        and canonical_source.get("mode") == target_source.get("mode")
    )
    if not source_matches:
        raise ValueError("event parity was not replayed from the natural target capture")

    outputs = replay.get("outputs")
    if not isinstance(outputs, Mapping):
        raise ValueError("captured-account replay lacks modeled output identities")
    for environment in ("historical", "paper"):
        root = str(outputs.get(f"{environment}_root") or "")
        digest = outputs.get(f"{environment}_account_journal_sha256")
        if not Path(root).is_absolute():
            raise ValueError(f"captured-account replay {environment} output root is not absolute")
        _lower_sha256(digest, label=f"captured-account replay {environment} journal hash")
    if outputs.get("historical_root") == outputs.get("paper_root"):
        raise ValueError("captured-account replay historical and paper outputs alias")

    payload: dict[str, Any] = {
        "schema_version": COMPARISON_SCOPE_SCHEMA_VERSION,
        "contract_id": COMPARISON_SCOPE_CONTRACT_ID,
        "batch_ids": batch_ids,
        "captured_account_replay_receipt": replay_identity_after,
        "event_parity_receipt": event_identity_after,
        "captured_replay_outputs": dict(outputs),
        "event_replay_provenance": dict(event_provenance),
        "effective_runtime_config": dict(effective_runtime_config),
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _comparison_scope_self_hash(payload)
    return payload


def write_comparison_scope(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Publish one owner-only comparison scope without replacing prior evidence."""

    verified = verify_comparison_scope(payload)
    output = Path(path).expanduser()
    if not output.is_absolute() or output.is_symlink():
        raise ValueError("comparison scope output must be an absolute non-symlink path")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"comparison scope output already exists: {output}")
    data = canonical_json(verified) + b"\n"
    descriptor = os.open(str(output), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("comparison scope write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        output.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    os.chmod(output, 0o600)
    directory_descriptor = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    resolved = output.resolve(strict=True)
    published = _strict_file_snapshot(
        resolved,
        label="published comparison scope",
        required_mode=0o600,
        require_single_link=True,
        require_owner=True,
    )
    if verify_comparison_scope(json.loads(published.data)) != verified:
        raise RuntimeError("published comparison scope changed")
    return resolved


def _decision_row(event: AccountEvent, *, batch_id: str) -> dict[str, Any]:
    decision_key = str(event.payload.get("decision_key") or "")
    if not decision_key:
        raise ValueError(f"scoped decision in {batch_id!r} lacks decision_key")
    return {
        "decision_key": decision_key,
        "sleeve": event.sleeve,
        "symbol": event.symbol,
        "strategy_id": str(event.payload.get("strategy_id") or ""),
        "component_id": str(event.payload.get("component_id") or ""),
        "reason": str(event.payload.get("reason") or ""),
    }


def _target_row(event: AccountEvent, *, batch_id: str) -> dict[str, Any]:
    target_key = str(event.payload.get("target_key") or "")
    decision_key = str(event.payload.get("decision_key") or "")
    if not target_key or not decision_key:
        raise ValueError(f"scoped target in {batch_id!r} lacks target/decision key")
    if str(event.payload.get("batch_id") or "") != batch_id:
        raise ValueError(f"scoped target in {batch_id!r} changed its payload batch id")
    return {
        "target_key": target_key,
        "decision_key": decision_key,
        "sleeve": event.sleeve,
        "symbol": event.symbol,
        "strategy_id": str(event.payload.get("strategy_id") or ""),
        "component_id": str(event.payload.get("component_id") or ""),
        "reason": str(event.payload.get("reason") or ""),
        "signed_qty": _finite_number(
            event.payload.get("signed_qty"),
            label=f"target {batch_id}:{target_key} signed_qty",
        ),
    }


def _risk_row(event: AccountEvent, *, batch_id: str) -> dict[str, Any]:
    if str(event.payload.get("batch_id") or "") != batch_id:
        raise ValueError(f"scoped risk decision in {batch_id!r} changed its payload batch id")
    accepted = event.payload.get("accepted")
    if not isinstance(accepted, bool):
        raise ValueError(f"scoped risk decision in {batch_id!r} lacks boolean accepted")
    raw_rejections = event.payload.get("rejection_keys")
    if not isinstance(raw_rejections, list) or any(not isinstance(value, str) for value in raw_rejections):
        raise ValueError(f"scoped risk decision in {batch_id!r} has invalid rejection keys")
    rejection_keys = tuple(cast(list[str], raw_rejections))
    if rejection_keys != tuple(sorted(set(rejection_keys))):
        raise ValueError(f"scoped risk rejection keys in {batch_id!r} are not sorted and unique")
    raw_updates = event.payload.get("target_updates")
    raw_aggregates = event.payload.get("aggregate_targets")
    if not isinstance(raw_updates, Mapping) or not isinstance(raw_aggregates, Mapping):
        raise ValueError(f"scoped risk decision in {batch_id!r} lacks target maps")
    target_updates: list[dict[str, Any]] = []
    for key, value in sorted(raw_updates.items(), key=lambda pair: str(pair[0])):
        if not isinstance(value, Mapping):
            raise ValueError(f"scoped risk target update {key!r} is not an object")
        target_updates.append(
            {
                "target_key": str(key),
                "symbol": str(value.get("symbol") or "").upper(),
                "signed_qty": _finite_number(
                    value.get("signed_qty"),
                    label=f"risk target {batch_id}:{key} signed_qty",
                ),
            }
        )
    aggregate_targets = [
        {
            "symbol": str(symbol).upper(),
            "signed_qty": _finite_number(
                quantity,
                label=f"risk aggregate {batch_id}:{symbol} signed_qty",
            ),
        }
        for symbol, quantity in sorted(raw_aggregates.items(), key=lambda pair: str(pair[0]))
    ]
    return {
        "accepted": accepted,
        "rejection_keys": rejection_keys,
        "target_updates": target_updates,
        "aggregate_targets": aggregate_targets,
    }


def _semantic_command(payload: Mapping[str, Any], *, batch_id: str) -> dict[str, Any]:
    symbol = str(payload.get("symbol") or "").upper()
    side = str(payload.get("side") or "")
    chunk_index = _integer(payload.get("chunk_index"), label=f"command {batch_id} chunk_index")
    chunk_count = _integer(payload.get("chunk_count"), label=f"command {batch_id} chunk_count")
    reduce_only = payload.get("reduce_only")
    if not symbol or side not in {"Buy", "Sell"} or not isinstance(reduce_only, bool):
        raise ValueError(f"scoped command in {batch_id!r} has invalid discrete fields")
    if chunk_index < 0 or chunk_count <= 0 or chunk_index >= chunk_count:
        raise ValueError(f"scoped command in {batch_id!r} has invalid chunk bounds")
    return {
        "batch_id": batch_id,
        "symbol": symbol,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "side": side,
        "reduce_only": reduce_only,
    }


def _command_row(event: AccountEvent, *, batch_id: str) -> dict[str, Any]:
    if str(event.payload.get("batch_id") or "") != batch_id:
        raise ValueError(f"scoped command in {batch_id!r} changed its payload batch id")
    command_id = str(event.payload.get("command_id") or "")
    if not command_id:
        raise ValueError(f"scoped command in {batch_id!r} lacks command_id")
    semantic = _semantic_command(event.payload, batch_id=batch_id)
    qty = _finite_number(event.payload.get("qty"), label=f"command {command_id} qty")
    signed_qty = _finite_number(
        event.payload.get("signed_qty"),
        label=f"command {command_id} signed_qty",
    )
    target_signed_qty = _finite_number(
        event.payload.get("target_signed_qty"),
        label=f"command {command_id} target_signed_qty",
    )
    if qty <= 0.0 or signed_qty == 0.0:
        raise ValueError(f"scoped command {command_id!r} has non-positive quantity")
    if abs(abs(signed_qty) - qty) > QUANTITY_ABS_TOLERANCE:
        raise ValueError(f"scoped command {command_id!r} qty and signed_qty disagree")
    if (signed_qty > 0.0) != (semantic["side"] == "Buy"):
        raise ValueError(f"scoped command {command_id!r} side contradicts signed_qty")
    return {
        "semantic": semantic,
        "command_id": command_id,
        "qty": qty,
        "signed_qty": signed_qty,
        "target_signed_qty": target_signed_qty,
    }


def _extract_batch_plan(events: Sequence[AccountEvent], *, batch_id: str) -> dict[str, Any]:
    selected = [
        event for event in events if event.event_type in _PLAN_EVENT_TYPES and _event_batch_id(event) == batch_id
    ]
    decisions = [_decision_row(event, batch_id=batch_id) for event in selected if event.event_type == "decision"]
    targets = [_target_row(event, batch_id=batch_id) for event in selected if event.event_type == "target"]
    risks = [_risk_row(event, batch_id=batch_id) for event in selected if event.event_type == "risk_decision"]
    commands = [_command_row(event, batch_id=batch_id) for event in selected if event.event_type == "order_command"]
    if not decisions or not targets or len(risks) != 1:
        raise ValueError(f"scoped batch {batch_id!r} requires decisions, targets, and exactly one risk decision")
    decisions.sort(key=lambda row: cast(str, row["decision_key"]))
    targets.sort(key=lambda row: cast(str, row["target_key"]))
    commands.sort(
        key=lambda row: (
            cast(Mapping[str, Any], row["semantic"])["symbol"],
            cast(Mapping[str, Any], row["semantic"])["chunk_index"],
        )
    )
    decision_keys = [cast(str, row["decision_key"]) for row in decisions]
    target_keys = [cast(str, row["target_key"]) for row in targets]
    target_decisions = [cast(str, row["decision_key"]) for row in targets]
    if len(set(decision_keys)) != len(decision_keys):
        raise ValueError(f"scoped batch {batch_id!r} has duplicate decision keys")
    if len(set(target_keys)) != len(target_keys):
        raise ValueError(f"scoped batch {batch_id!r} has duplicate target keys")
    if sorted(target_decisions) != decision_keys:
        raise ValueError(f"scoped batch {batch_id!r} target/decision keys do not align")
    semantic_keys = [canonical_json({"semantic": row["semantic"]}) for row in commands]
    command_ids = [cast(str, row["command_id"]) for row in commands]
    if len(set(semantic_keys)) != len(semantic_keys):
        raise ValueError(f"scoped batch {batch_id!r} has duplicate semantic commands")
    if len(set(command_ids)) != len(command_ids):
        raise ValueError(f"scoped batch {batch_id!r} maps one command id more than once")
    return {
        "batch_id": batch_id,
        "decisions": decisions,
        "targets": targets,
        "risk": risks[0],
        "commands": commands,
    }


def _derive_scope_window(
    events: Sequence[AccountEvent],
    *,
    batch_ids: Sequence[str],
) -> tuple[tuple[AccountEvent, ...], dict[str, Any]]:
    wanted = set(batch_ids)
    scoped_plan_events = [
        event for event in events if event.event_type in _PLAN_EVENT_TYPES and _event_batch_id(event) in wanted
    ]
    if not scoped_plan_events:
        raise ValueError("comparison scope has no matching strategy-plan events")
    scoped_events = [event for event in events if _event_batch_id(event) in wanted]
    first_sequence = min(event.sequence for event in scoped_events)
    last_sequence = max(event.sequence for event in scoped_events)
    window = tuple(event for event in events if first_sequence <= event.sequence <= last_sequence)
    observed_order: list[str] = []
    for event in scoped_plan_events:
        batch_id = _event_batch_id(event)
        if batch_id not in observed_order:
            observed_order.append(batch_id)
    if tuple(observed_order) != tuple(batch_ids):
        raise ValueError(
            "comparison scope batch order differs from journal order: "
            f"expected={list(batch_ids)!r}, observed={observed_order!r}"
        )
    extras = sorted(
        {
            _event_batch_id(event)
            for event in window
            if event.event_type in _PLAN_EVENT_TYPES
            and _event_batch_id(event) not in wanted
            and not _is_owner_batch(_event_batch_id(event))
        }
    )
    if extras:
        raise ValueError("comparison scope window contains unscoped strategy batches: " + ", ".join(extras))
    first_event = events[first_sequence - 1]
    last_event = events[last_sequence - 1]
    return window, {
        "first_sequence": first_sequence,
        "first_event_hash": first_event.event_hash,
        "last_sequence": last_sequence,
        "last_event_hash": last_event.event_hash,
    }


def _primary_family(event: AccountEvent, *, scoped_batch_ids: frozenset[str]) -> str:
    batch_id = _event_batch_id(event)
    payload = event.payload
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    source = str(payload.get("source") or "")
    if event.event_type == AccountEventType.PNL.value and (
        source == "venue_funding_settlement" or str(payload.get("pnl_key") or "").startswith("venue-funding:")
    ):
        return "funding"
    if event.event_type == AccountEventType.VENUE_SNAPSHOT.value:
        return "reconciliation"
    if batch_id.startswith("external-protection/") or bool(
        cast(Mapping[str, Any], metadata).get("external_native_protection")
    ):
        return "native_protection"
    if batch_id.startswith("external-reduction/"):
        return "external_reduction"
    if batch_id.startswith("account-convergence/"):
        return "owner_convergence"
    if event.event_type in _PLAN_EVENT_TYPES:
        return "scoped_strategy_plan" if batch_id in scoped_batch_ids else "out_of_scope_strategy_plan"
    if event.event_type in {
        AccountEventType.ACK.value,
        AccountEventType.ACK_OBSERVATION.value,
        AccountEventType.FILL.value,
        AccountEventType.ORDER_STATUS.value,
    }:
        return "execution"
    if event.event_type == AccountEventType.PNL.value:
        return "pnl"
    if event.event_type == AccountEventType.PROTECTION.value:
        return "protection"
    if event.event_type == AccountEventType.CLOSE.value:
        return "close"
    if event.event_type == AccountEventType.MARKET_INPUT_REF.value:
        return "market_input"
    return "other"


def _classified_counts(
    events: Sequence[AccountEvent],
    *,
    scoped_batch_ids: frozenset[str],
) -> dict[str, Any]:
    event_types = Counter(event.event_type for event in events)
    families = Counter(_primary_family(event, scoped_batch_ids=scoped_batch_ids) for event in events)
    facts: Counter[str] = Counter()
    for event in events:
        if event.event_type == AccountEventType.FILL.value:
            metadata = event.payload.get("metadata")
            status = str(metadata.get("fee_status") or "") if isinstance(metadata, Mapping) else ""
            if status == "observed_execution_fee":
                facts["fee_observed"] += 1
            elif status == "modeled_execution_fee":
                facts["fee_modeled"] += 1
            else:
                facts["fee_pending_or_unknown"] += 1
        if event.event_type == AccountEventType.PNL.value:
            facts["pnl"] += 1
            if _primary_family(event, scoped_batch_ids=scoped_batch_ids) == "funding":
                facts["funding"] += 1
    return {
        "event_type_counts": dict(sorted(event_types.items())),
        "primary_family_counts": dict(sorted(families.items())),
        "fee_pnl_funding_fact_counts": dict(sorted(facts.items())),
    }


def _plan_decision_keys(plan: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row["decision_key"]) for row in cast(Sequence[Mapping[str, Any]], plan["decisions"]))


def _plan_target_keys(plan: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row["target_key"]) for row in cast(Sequence[Mapping[str, Any]], plan["targets"]))


def _target_discrete(plan: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        {key: value for key, value in row.items() if key != "signed_qty"}
        for row in cast(Sequence[Mapping[str, Any]], plan["targets"])
    )


def _risk_discrete(plan: Mapping[str, Any]) -> dict[str, Any]:
    risk = cast(Mapping[str, Any], plan["risk"])
    updates = cast(Sequence[Mapping[str, Any]], risk["target_updates"])
    aggregates = cast(Sequence[Mapping[str, Any]], risk["aggregate_targets"])
    return {
        "accepted": risk["accepted"],
        "rejection_keys": list(cast(Sequence[str], risk["rejection_keys"])),
        "target_updates": [{"target_key": row["target_key"], "symbol": row["symbol"]} for row in updates],
        "aggregate_symbols": [row["symbol"] for row in aggregates],
    }


def _semantic_commands(plan: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(row["semantic"] for row in cast(Sequence[Mapping[str, Any]], plan["commands"]))


def _semantic_label(semantic: Mapping[str, Any]) -> str:
    return canonical_json({"semantic": semantic}).decode("utf-8")


def _quantity_map(plan: Mapping[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for row in cast(Sequence[Mapping[str, Any]], plan["targets"]):
        output[f"target:{row['target_key']}"] = cast(float, row["signed_qty"])
    risk = cast(Mapping[str, Any], plan["risk"])
    for row in cast(Sequence[Mapping[str, Any]], risk["target_updates"]):
        output[f"risk-target:{row['target_key']}"] = cast(float, row["signed_qty"])
    for row in cast(Sequence[Mapping[str, Any]], risk["aggregate_targets"]):
        output[f"aggregate:{row['symbol']}"] = cast(float, row["signed_qty"])
    for row in cast(Sequence[Mapping[str, Any]], plan["commands"]):
        label = _semantic_label(cast(Mapping[str, Any], row["semantic"]))
        output[f"command:{label}:qty"] = cast(float, row["qty"])
        output[f"command:{label}:signed_qty"] = cast(float, row["signed_qty"])
        output[f"command:{label}:target_signed_qty"] = cast(float, row["target_signed_qty"])
    return output


def _quantities_match(left: Mapping[str, float], right: Mapping[str, float]) -> bool:
    return set(left) == set(right) and all(abs(left[key] - right[key]) <= QUANTITY_ABS_TOLERANCE for key in left)


def _normalized_modeled_execution(
    events: Sequence[AccountEvent],
    *,
    plans: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    command_semantics: dict[str, Mapping[str, Any]] = {}
    scoped_batches = {str(plan["batch_id"]) for plan in plans}
    for plan in plans:
        for command in cast(Sequence[Mapping[str, Any]], plan["commands"]):
            command_semantics[str(command["command_id"])] = cast(Mapping[str, Any], command["semantic"])
    fill_ordinals: Counter[str] = Counter()
    output: list[Mapping[str, Any]] = []
    for event in events:
        if event.event_type not in _MODELED_EXECUTION_EVENT_TYPES:
            continue
        batch_id = _event_batch_id(event)
        if batch_id not in scoped_batches:
            continue
        payload = event.payload
        command_id = str(payload.get("command_id") or "")
        semantic = command_semantics.get(command_id)
        if (
            event.event_type
            in {
                AccountEventType.ACK.value,
                AccountEventType.FILL.value,
                AccountEventType.ORDER_STATUS.value,
            }
            and semantic is None
        ):
            raise ValueError(f"modeled execution references unmapped scoped command {command_id!r}")
        if event.event_type == AccountEventType.ACK.value:
            output.append(
                {
                    "event_type": event.event_type,
                    "semantic_command": semantic,
                    "accepted": payload.get("accepted"),
                    # Twin rejection keys include raw command ids. Preserve the
                    # stable terminal reason rather than normalizing arbitrary text.
                    "rejection_reason": str(payload.get("rejection_key") or "").rsplit(":", 1)[-1],
                }
            )
        elif event.event_type == AccountEventType.FILL.value:
            label = _semantic_label(cast(Mapping[str, Any], semantic))
            fill_ordinals[label] += 1
            output.append(
                {
                    "event_type": event.event_type,
                    "semantic_command": semantic,
                    "fill_ordinal": fill_ordinals[label],
                    "signed_qty": _finite_number(payload.get("signed_qty"), label="modeled fill signed_qty"),
                    "price": _finite_number(payload.get("price"), label="modeled fill price"),
                    "fee_usdt": _finite_number(payload.get("fee_usdt"), label="modeled fill fee"),
                }
            )
        elif event.event_type == AccountEventType.ORDER_STATUS.value:
            output.append(
                {
                    "event_type": event.event_type,
                    "semantic_command": semantic,
                    "status": str(payload.get("status") or ""),
                    "cumulative_filled_qty": _finite_number(
                        payload.get("cumulative_filled_qty"),
                        label="modeled terminal cumulative quantity",
                    ),
                }
            )
        elif event.event_type == AccountEventType.CLOSE.value:
            output.append(
                {
                    "event_type": event.event_type,
                    "batch_id": batch_id,
                    "symbol": event.symbol,
                    "reason": str(payload.get("reason") or ""),
                    "venue_flat": payload.get("venue_flat"),
                }
            )
        else:
            output.append(
                {
                    "event_type": event.event_type,
                    "batch_id": batch_id,
                    "symbol": event.symbol,
                    "gross_pnl_usdt": _finite_number(payload.get("gross_pnl_usdt"), label="modeled gross PnL"),
                    "fee_usdt": _finite_number(payload.get("fee_usdt"), label="modeled PnL fee"),
                    "funding_usdt": _finite_number(payload.get("funding_usdt"), label="modeled funding"),
                    "net_pnl_usdt": _finite_number(payload.get("net_pnl_usdt"), label="modeled net PnL"),
                    "source": str(payload.get("source") or ""),
                }
            )
    return tuple(output)


def _compare_captured(
    captured: Mapping[str, _CapturedSource],
    *,
    comparison_batch_ids: Sequence[str],
) -> tuple[KernelParityReport, dict[str, Any]]:
    if set(captured) != set(REQUIRED_ENVIRONMENTS):
        raise ValueError("kernel parity requires exactly historical, paper, and demo sources")
    batch_ids = tuple(comparison_batch_ids)
    if not batch_ids or len(set(batch_ids)) != len(batch_ids):
        raise ValueError("kernel parity requires non-empty unique comparison batch ids")
    plans: dict[str, tuple[Mapping[str, Any], ...]] = {}
    windows: dict[str, tuple[AccountEvent, ...]] = {}
    scope_metadata: dict[str, Any] = {}
    source_evidence: dict[str, Any] = {}
    wanted = frozenset(batch_ids)
    for environment in REQUIRED_ENVIRONMENTS:
        source = captured[environment]
        window, window_identity = _derive_scope_window(source.events, batch_ids=batch_ids)
        extracted = tuple(_extract_batch_plan(window, batch_id=batch_id) for batch_id in batch_ids)
        plans[environment] = extracted
        windows[environment] = window
        scope_metadata[environment] = window_identity
        first = int(window_identity["first_sequence"])
        last = int(window_identity["last_sequence"])
        pre_scope_events = source.events[: first - 1]
        classified_event_counts = {
            "before_scope": _classified_counts(pre_scope_events, scoped_batch_ids=wanted),
            "comparison_scope": _classified_counts(window, scoped_batch_ids=wanted),
            "after_scope": _classified_counts(source.events[last:], scoped_batch_ids=wanted),
        }
        source_evidence[environment] = {
            **dict(source.identity),
            "comparison_window": window_identity,
            "classified_event_counts": classified_event_counts,
        }
        if environment == "demo":
            source_evidence[environment]["demo_prefix_classification"] = {
                "event_count": len(pre_scope_events),
                **classified_event_counts["before_scope"],
            }

    baseline_name = "historical"
    baseline = plans[baseline_name]
    decisions_ok = targets_ok = target_discrete_ok = risk_ok = risk_presence_ok = True
    commands_ok = quantities_ok = mapping_ok = True
    mismatches: list[str] = []
    for environment in ("paper", "demo"):
        other = plans[environment]
        for batch_id, left, right in zip(batch_ids, baseline, other, strict=True):
            if _plan_decision_keys(left) != _plan_decision_keys(right):
                decisions_ok = False
                mismatches.append(f"decision keys differ: historical vs {environment}: {batch_id}")
            if _plan_target_keys(left) != _plan_target_keys(right):
                targets_ok = False
                mismatches.append(f"target keys differ: historical vs {environment}: {batch_id}")
            if _target_discrete(left) != _target_discrete(right):
                target_discrete_ok = False
                mismatches.append(f"target discrete fields differ: historical vs {environment}: {batch_id}")
            left_risk = _risk_discrete(left)
            right_risk = _risk_discrete(right)
            if (
                left_risk["accepted"] != right_risk["accepted"]
                or left_risk["rejection_keys"] != right_risk["rejection_keys"]
            ):
                risk_ok = False
                mismatches.append(f"risk acceptance/rejection keys differ: historical vs {environment}: {batch_id}")
            if (
                left_risk["target_updates"] != right_risk["target_updates"]
                or left_risk["aggregate_symbols"] != right_risk["aggregate_symbols"]
            ):
                risk_presence_ok = False
                mismatches.append(f"risk target presence differs: historical vs {environment}: {batch_id}")
            if _semantic_commands(left) != _semantic_commands(right):
                commands_ok = False
                mismatches.append(f"semantic commands differ: historical vs {environment}: {batch_id}")
            if not _quantities_match(_quantity_map(left), _quantity_map(right)):
                quantities_ok = False
                mismatches.append(f"plan quantities differ: historical vs {environment}: {batch_id}")

    command_maps: dict[str, dict[str, str]] = {}
    for environment in REQUIRED_ENVIRONMENTS:
        mapping: dict[str, str] = {}
        observed_raw: set[str] = set()
        for plan in plans[environment]:
            for command in cast(Sequence[Mapping[str, Any]], plan["commands"]):
                label = _semantic_label(cast(Mapping[str, Any], command["semantic"]))
                raw = str(command["command_id"])
                if label in mapping or raw in observed_raw:
                    mapping_ok = False
                mapping[label] = raw
                observed_raw.add(raw)
        command_maps[environment] = mapping
    semantic_union = sorted(set().union(*(set(value) for value in command_maps.values())))
    if any(set(mapping) != set(semantic_union) for mapping in command_maps.values()):
        mapping_ok = False
    command_id_mapping = tuple(
        {
            "semantic_command": json.loads(label)["semantic"],
            "environment_command_ids": {
                environment: command_maps[environment].get(label, "") for environment in REQUIRED_ENVIRONMENTS
            },
        }
        for label in semantic_union
    )
    if not mapping_ok:
        mismatches.append("environment command ids do not form a one-to-one semantic mapping")

    historical_modeled = _normalized_modeled_execution(
        captured["historical"].events,
        plans=plans["historical"],
    )
    paper_modeled = _normalized_modeled_execution(
        captured["paper"].events,
        plans=plans["paper"],
    )
    modeled_exact = canonical_json({"events": historical_modeled}) == canonical_json({"events": paper_modeled})

    passed = all(
        (
            decisions_ok,
            targets_ok,
            target_discrete_ok,
            risk_ok,
            risk_presence_ok,
            commands_ok,
            quantities_ok,
            mapping_ok,
        )
    )
    report = KernelParityReport(
        passed=passed,
        contract_id=KERNEL_PARITY_CONTRACT_ID,
        quantity_abs_tolerance=QUANTITY_ABS_TOLERANCE,
        compared_environments=REQUIRED_ENVIRONMENTS,
        scoped_batch_ids=batch_ids,
        decision_keys_identical=decisions_ok,
        target_keys_identical=targets_ok,
        target_discrete_fields_identical=target_discrete_ok,
        risk_acceptance_and_rejection_keys_identical=risk_ok,
        risk_target_presence_identical=risk_presence_ok,
        semantic_commands_identical=commands_ok,
        quantity_values_within_tolerance=quantities_ok,
        command_id_mapping_one_to_one=mapping_ok,
        historical_paper_normalized_modeled_execution_exact=modeled_exact,
        command_id_mapping=command_id_mapping,
        mismatches=tuple(mismatches),
    )
    return report, {
        "source_windows": scope_metadata,
        "sources": source_evidence,
    }


def compare_kernel_journals(
    environments: Mapping[str, str | Path | Sequence[AccountEvent]],
    *,
    comparison_batch_ids: Sequence[str],
    quantity_tolerance: float = QUANTITY_ABS_TOLERANCE,
) -> KernelParityReport:
    """Compare a non-empty, explicitly named strategy-plan slice.

    This in-memory helper does not create deployable evidence. Deployment uses
    :func:`build_kernel_parity_receipt`, which binds and revalidates source and
    configuration files plus the exact clean commit.
    """

    if quantity_tolerance != QUANTITY_ABS_TOLERANCE:
        raise ValueError("kernel parity quantity tolerance is fixed prospectively at 1e-12")
    if set(environments) != set(REQUIRED_ENVIRONMENTS):
        raise ValueError("kernel parity requires exactly historical, paper, and demo")
    captured: dict[str, _CapturedSource] = {}
    for environment in REQUIRED_ENVIRONMENTS:
        source = environments[environment]
        if isinstance(source, (str, Path)):
            captured[environment] = _capture_journal(source)
        else:
            events = tuple(source)
            if not events:
                raise ValueError(f"account parity source {environment!r} has no events")
            captured[environment] = _CapturedSource(
                Path(f"/{environment}-in-memory"),
                events,
                {"root": f"/{environment}-in-memory"},
            )
    report, _metadata = _compare_captured(captured, comparison_batch_ids=comparison_batch_ids)
    return report


def _report_payload(report: KernelParityReport) -> dict[str, Any]:
    payload = asdict(report)
    payload["compared_environments"] = list(report.compared_environments)
    payload["scoped_batch_ids"] = list(report.scoped_batch_ids)
    payload["command_id_mapping"] = list(report.command_id_mapping)
    payload["mismatches"] = list(report.mismatches)
    return payload


def _validate_captured_replay_roots(
    captured: Mapping[str, _CapturedSource],
    *,
    scope_provenance: Mapping[str, Any],
) -> None:
    outputs = scope_provenance.get("captured_replay_outputs")
    if not isinstance(outputs, Mapping):
        raise ValueError("kernel comparison scope lacks captured replay outputs")
    for environment in ("historical", "paper"):
        source = captured[environment]
        expected_root = str(outputs.get(f"{environment}_root") or "")
        expected_journal = _lower_sha256(
            outputs.get(f"{environment}_account_journal_sha256"),
            label=f"captured replay {environment} journal hash",
        )
        if str(source.root) != expected_root:
            raise ValueError(f"kernel {environment} root is not the captured-account replay output")
        if _journal_stream_sha256(source.events) != expected_journal:
            raise ValueError(f"kernel {environment} journal is not the captured-account replay output")


def _build_receipt_payload(
    environments: Mapping[str, str | Path],
    *,
    comparison_scope_file: str | Path,
    event_parity_receipt: str | Path,
    fresh_epoch_reset_receipt: str | Path,
    risk_policy_file: str | Path,
    rules_file: str | Path,
    effective_runtime_config_bundle_file: str | Path,
    twin_calibration_receipt: str | Path,
    repo_root: str | Path,
    expected_commit: str,
    created_ts_ns: int,
) -> dict[str, Any]:
    if set(environments) != set(REQUIRED_ENVIRONMENTS):
        raise ValueError("kernel parity receipt requires exactly historical, paper, and demo")
    if created_ts_ns <= 0:
        raise ValueError("kernel parity receipt creation time must be positive")
    evidence_paths = {
        "event_parity_receipt": event_parity_receipt,
        "fresh_epoch_reset_receipt": fresh_epoch_reset_receipt,
        "risk_policy_file": risk_policy_file,
        "rules_file": rules_file,
        "effective_runtime_config_bundle_file": effective_runtime_config_bundle_file,
        "twin_calibration_receipt": twin_calibration_receipt,
    }
    evidence_snapshots = {
        name: _strict_file_snapshot(path, label=name.replace("_", " ")) for name, path in evidence_paths.items()
    }
    evidence = {name: _identity_from_snapshot(snapshot) for name, snapshot in evidence_snapshots.items()}
    scope_identity, batch_ids, scope_effective_runtime_config, scope_provenance = _load_comparison_scope(
        comparison_scope_file,
        expected_event_parity_identity=evidence["event_parity_receipt"],
    )
    effective_path = cast(
        str,
        evidence["effective_runtime_config_bundle_file"]["path"],
    )
    if _accepts_keyword(
        load_effective_runtime_config_bundle_binding,
        "snapshot",
    ):
        _effective_payload, effective_runtime_config = load_effective_runtime_config_bundle_binding(
            effective_path,
            snapshot=evidence_snapshots["effective_runtime_config_bundle_file"],
        )
    else:
        _effective_payload, effective_runtime_config = load_effective_runtime_config_bundle_binding(effective_path)
    if effective_runtime_config.get("file_sha256") != evidence["effective_runtime_config_bundle_file"]["sha256"]:
        raise ValueError("effective runtime config bundle changed during kernel parity")
    if effective_runtime_config != scope_effective_runtime_config:
        raise ValueError("kernel comparison scope names another effective runtime configuration")
    effective_repository = effective_runtime_config.get("repository")
    if not isinstance(effective_repository, Mapping):
        raise ValueError("effective runtime config bundle lacks its repository binding")
    if effective_repository.get("candidate_commit") != _full_commit(expected_commit):
        raise ValueError("effective runtime config bundle belongs to another candidate commit")
    captured = {environment: _capture_journal(environments[environment]) for environment in REQUIRED_ENVIRONMENTS}
    natural_roots = [str(source.root) for source in captured.values()]
    if len(set(natural_roots)) != len(REQUIRED_ENVIRONMENTS):
        raise ValueError("historical, paper, and demo natural epochs require distinct roots")
    _validate_captured_replay_roots(captured, scope_provenance=scope_provenance)
    calibration_epoch = _calibration_epoch_binding(
        twin_calibration_receipt,
        natural_sources=captured,
        snapshot=evidence_snapshots["twin_calibration_receipt"],
    )
    if calibration_epoch["expected_account_id"] != captured["demo"].identity["account_id"]:
        raise ValueError("execution-twin calibration belongs to a different demo account")
    report, comparison_metadata = _compare_captured(
        captured,
        comparison_batch_ids=batch_ids,
    )
    return {
        "schema_version": KERNEL_PARITY_SCHEMA_VERSION,
        "contract_id": KERNEL_PARITY_CONTRACT_ID,
        "created_ts_ns": created_ts_ns,
        "evidence_scope": KERNEL_PARITY_EVIDENCE_SCOPE,
        "journal_parity_passed": report.passed,
        "full_cross_environment_acceptance_passed": False,
        "quantity_abs_tolerance": QUANTITY_ABS_TOLERANCE,
        "comparison_scope": {
            "scope_file": scope_identity,
            "batch_ids": list(batch_ids),
            **scope_provenance,
            "source_windows": comparison_metadata["source_windows"],
        },
        "sources": comparison_metadata["sources"],
        "evidence_bindings": evidence,
        "effective_runtime_config": effective_runtime_config,
        "epoch_bindings": {
            "calibration_pre_reset": calibration_epoch,
            "natural_post_reset": {
                "reset_receipt_file_sha256": evidence["fresh_epoch_reset_receipt"]["sha256"],
                "event_parity_receipt_file_sha256": evidence["event_parity_receipt"]["sha256"],
                "captured_account_replay_receipt_file_sha256": scope_provenance["captured_account_replay_receipt"][
                    "sha256"
                ],
                "effective_runtime_config_bundle_file_sha256": evidence["effective_runtime_config_bundle_file"][
                    "sha256"
                ],
                "effective_runtime_config_bundle_artifact_sha256": effective_runtime_config["artifact_sha256"],
                "comparison_batch_ids": list(batch_ids),
                "sources": {
                    environment: {
                        "root": str(captured[environment].root),
                        "account_id": captured[environment].identity["account_id"],
                        "raw_manifest_sha256": captured[environment].identity["raw_manifest_sha256"],
                        "journal_stream_sha256": _journal_stream_sha256(captured[environment].events),
                    }
                    for environment in REQUIRED_ENVIRONMENTS
                },
            },
            "separation_contract": {
                "calibration_epoch": "pre_reset_training",
                "natural_epoch": "post_reset_natural_replay",
                "same_journal_digest_forbidden": True,
                "same_event_chain_head_forbidden": True,
                "embedded_calibration_live_paths_reopened": False,
            },
        },
        "repo_binding": _git_binding(repo_root, expected_commit=expected_commit),
        "normalization_contract": NORMALIZATION_CONTRACT,
        "report": _report_payload(report),
        "unverified_external_gates": list(UNVERIFIED_EXTERNAL_GATES),
        "artifact_sha256": "",
    }


def build_kernel_parity_receipt(
    environments: Mapping[str, str | Path],
    *,
    comparison_scope_file: str | Path,
    event_parity_receipt: str | Path,
    fresh_epoch_reset_receipt: str | Path,
    risk_policy_file: str | Path,
    rules_file: str | Path,
    effective_runtime_config_bundle_file: str | Path,
    twin_calibration_receipt: str | Path,
    repo_root: str | Path,
    expected_commit: str,
    quantity_tolerance: float = QUANTITY_ABS_TOLERANCE,
    created_ts_ns: int | None = None,
) -> dict[str, Any]:
    if quantity_tolerance != QUANTITY_ABS_TOLERANCE:
        raise ValueError("kernel parity quantity tolerance is fixed prospectively at 1e-12")
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    receipt = _build_receipt_payload(
        environments,
        comparison_scope_file=comparison_scope_file,
        event_parity_receipt=event_parity_receipt,
        fresh_epoch_reset_receipt=fresh_epoch_reset_receipt,
        risk_policy_file=risk_policy_file,
        rules_file=rules_file,
        effective_runtime_config_bundle_file=effective_runtime_config_bundle_file,
        twin_calibration_receipt=twin_calibration_receipt,
        repo_root=repo_root,
        expected_commit=expected_commit,
        created_ts_ns=created,
    )
    receipt["artifact_sha256"] = _sha256_bytes(canonical_json(receipt))
    return receipt


def _path_from_binding(binding: Any, *, label: str) -> str:
    if not isinstance(binding, Mapping):
        raise ValueError(f"kernel-parity {label} binding must be an object")
    path = str(binding.get("path") or "")
    if not path.startswith("/"):
        raise ValueError(f"kernel-parity {label} binding must use an absolute path")
    return path


def verify_kernel_parity_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Re-read every source/evidence path and recompute the complete receipt."""

    payload = dict(receipt)
    schema = payload.get("schema_version")
    if schema == 1:
        raise ValueError("account-kernel parity schema v1 is not deploy-valid")
    if schema != KERNEL_PARITY_SCHEMA_VERSION:
        raise ValueError("unsupported account-kernel parity receipt schema")
    if payload.get("contract_id") != KERNEL_PARITY_CONTRACT_ID:
        raise ValueError("account-kernel parity receipt has the wrong contract id")
    if payload.get("evidence_scope") != KERNEL_PARITY_EVIDENCE_SCOPE:
        raise ValueError("account-kernel parity receipt has the wrong evidence scope")
    if int(payload.get("created_ts_ns") or 0) <= 0:
        raise ValueError("account-kernel parity receipt has an invalid creation time")
    if payload.get("quantity_abs_tolerance") != QUANTITY_ABS_TOLERANCE:
        raise ValueError("account-kernel parity receipt changed the registered 1e-12 tolerance")
    observed_hash = _lower_sha256(payload.get("artifact_sha256"), label="kernel-parity artifact hash")
    unhashed = {**payload, "artifact_sha256": ""}
    if observed_hash != _sha256_bytes(canonical_json(unhashed)):
        raise ValueError("account-kernel parity receipt hash mismatch")

    sources = payload.get("sources")
    evidence = payload.get("evidence_bindings")
    scope = payload.get("comparison_scope")
    repo = payload.get("repo_binding")
    effective_runtime_config = payload.get("effective_runtime_config")
    if not isinstance(sources, Mapping) or set(sources) != set(REQUIRED_ENVIRONMENTS):
        raise ValueError("account-kernel parity receipt has invalid sources")
    if not isinstance(evidence, Mapping) or set(evidence) != set(_EVIDENCE_ARGUMENTS):
        raise ValueError("account-kernel parity receipt has invalid evidence bindings")
    if (
        not isinstance(scope, Mapping)
        or not isinstance(repo, Mapping)
        or not isinstance(effective_runtime_config, Mapping)
    ):
        raise ValueError("account-kernel parity receipt lacks scope, repository, or effective-config binding")

    environment_paths = {
        environment: str(cast(Mapping[str, Any], sources[environment]).get("root") or "")
        for environment in REQUIRED_ENVIRONMENTS
    }
    if any(not path.startswith("/") for path in environment_paths.values()):
        raise ValueError("account-kernel parity source roots must be absolute")
    evidence_paths = {name: _path_from_binding(evidence[name], label=name) for name in _EVIDENCE_ARGUMENTS}
    expected = _build_receipt_payload(
        environment_paths,
        comparison_scope_file=_path_from_binding(scope.get("scope_file"), label="comparison scope"),
        event_parity_receipt=evidence_paths["event_parity_receipt"],
        fresh_epoch_reset_receipt=evidence_paths["fresh_epoch_reset_receipt"],
        risk_policy_file=evidence_paths["risk_policy_file"],
        rules_file=evidence_paths["rules_file"],
        effective_runtime_config_bundle_file=evidence_paths["effective_runtime_config_bundle_file"],
        twin_calibration_receipt=evidence_paths["twin_calibration_receipt"],
        repo_root=str(repo.get("root") or ""),
        expected_commit=str(repo.get("commit") or ""),
        created_ts_ns=int(payload.get("created_ts_ns") or 0),
    )
    if canonical_json(expected) != canonical_json(unhashed):
        raise ValueError("account-kernel parity receipt does not match recomputed sources and contract")
    return payload


def _atomic_write_receipt(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        descriptor = os.open(
            str(path),
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        try:
            offset = 0
            view = memoryview(data)
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("account-kernel parity receipt write made no progress")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def write_kernel_parity_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    output = Path(path).expanduser()
    payload = verify_kernel_parity_receipt(receipt)
    _atomic_write_receipt(output, json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n")
    return output


def load_kernel_parity_receipt(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    if snapshot is None:
        snapshot = _strict_file_snapshot(
            path,
            label="account-kernel parity receipt",
            required_mode=0o600,
            require_single_link=True,
            require_owner=True,
        )
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("account-kernel parity receipt snapshot path differs")
    elif snapshot.mode != 0o600 or snapshot.uid != os.geteuid() or snapshot.nlink != 1:
        raise ValueError("account-kernel parity receipt must be owner-owned mode 0600")
    try:
        value = json.loads(snapshot.data)
    except json.JSONDecodeError as exc:
        raise ValueError("account-kernel parity receipt is unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("account-kernel parity receipt must be an object")
    return verify_kernel_parity_receipt(value)


def _environment_arg(raw: str) -> tuple[str, Path]:
    name, separator, raw_path = raw.partition("=")
    name = name.strip()
    raw_path = raw_path.strip()
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError("environment must be NAME=ACCOUNT_ROOT")
    return name, Path(raw_path).expanduser()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a frozen strategy-to-order-plan batch scope across stopped "
            "historical, paper, and actual demo account journals."
        )
    )
    parser.add_argument(
        "--environment",
        action="append",
        required=True,
        type=_environment_arg,
        metavar="NAME=ACCOUNT_ROOT",
        help="Repeat exactly once for historical, paper, and demo.",
    )
    parser.add_argument("--comparison-scope-file", type=Path, required=True)
    parser.add_argument("--event-parity-receipt", type=Path, required=True)
    parser.add_argument("--fresh-epoch-reset-receipt", type=Path, required=True)
    parser.add_argument("--risk-policy-file", type=Path, required=True)
    parser.add_argument("--rules-file", type=Path, required=True)
    parser.add_argument("--effective-runtime-config-bundle", type=Path, required=True)
    parser.add_argument("--twin-calibration-receipt", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--quantity-tolerance", type=float, default=QUANTITY_ABS_TOLERANCE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.quantity_tolerance != QUANTITY_ABS_TOLERANCE:
        parser.error("--quantity-tolerance is fixed prospectively at 1e-12")

    environments: dict[str, Path] = {}
    for name, root in args.environment:
        if name in environments:
            parser.error(f"duplicate environment name: {name}")
        environments[name] = root
    if set(environments) != set(REQUIRED_ENVIRONMENTS):
        parser.error("environments must be exactly historical, paper, and demo")

    receipt = build_kernel_parity_receipt(
        environments,
        comparison_scope_file=args.comparison_scope_file,
        event_parity_receipt=args.event_parity_receipt,
        fresh_epoch_reset_receipt=args.fresh_epoch_reset_receipt,
        risk_policy_file=args.risk_policy_file,
        rules_file=args.rules_file,
        effective_runtime_config_bundle_file=args.effective_runtime_config_bundle,
        twin_calibration_receipt=args.twin_calibration_receipt,
        repo_root=args.repo_root,
        expected_commit=args.expected_commit,
        quantity_tolerance=args.quantity_tolerance,
    )
    write_kernel_parity_receipt(args.output, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["journal_parity_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
