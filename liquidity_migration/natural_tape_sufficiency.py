"""Verify the registered 120-hour natural demo evidence floor.

This verifier is intentionally independent from the captured account replay.
It re-opens both sleeves' raw event/outcome tapes, the shared post-publication
target capture, the actual demo account journal, the separately classified
post-window safety capture, account replay, and venue-accounting receipt.  A
valid but quiet window is ``inconclusive``; broken provenance, scheduling,
lineage, accounting, or the fixed-window contract raises and publishes no
receipt.

Nothing in this module opens an inbox, reads credentials, constructs a venue
client, or grants execution/deployment authority.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_kernel import (
    GENESIS_HASH,
    AccountEvent,
    AccountEventType,
    account_journal_path,
    account_transactions_path,
    read_account_journal_bytes,
    reduce_account_events,
)
from .account_strategy_state import canonical_strategy_trade_rows
from .account_venue_accounting import verify_venue_accounting_receipt
from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .captured_account_replay import (
    load_captured_account_replay_receipt,
    load_post_window_safety_manifest,
    verify_captured_account_replay_receipt,
)
from .deterministic_serialization import canonical_json
from .natural_cutover_freeze_manifest import load_natural_cutover_freeze_manifest
from .natural_effective_config import (
    load_effective_runtime_config_bundle_binding,
    validate_effective_runtime_config_bundle_join,
)
from .strategy_event_clock import StrategyEvent, load_strategy_event_tape_bytes
from .strategy_event_outcome import load_strategy_event_decision_tape_bytes
from .strategy_target_replay import (
    CapturedTargetRequest,
    TargetSchedulingCaptureEvent,
    load_target_scheduling_capture_bytes,
)


NATURAL_TAPE_SUFFICIENCY_SCHEMA_VERSION = 3
NATURAL_TAPE_SUFFICIENCY_KIND = "natural_demo_tape_sufficiency_v3"
NATURAL_WINDOW_HOURS = 120
HOUR_NS = 3_600_000_000_000
NATURAL_MIN_FILLED_COMMANDS = 30
NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE = 10
NATURAL_MIN_FILLED_SYMBOLS = 3
NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE = 3
NATURAL_MIN_PNL_EVENTS = 10
_SLEEVES = ("long", "continuous")
_TERMINAL_ORDER_STATUSES = frozenset({"filled", "partially_filled_cancelled", "cancelled", "rejected"})
_LIMITATIONS = (
    "activity_floors_measure_repeated_lifecycle_coverage_not_alpha_or_performance",
    "same_symbol_component_attribution_is_counted_once_per_execution_batch_pair",
    "post_window_safety_batches_and_their_pnl_do_not_count_toward_natural_floors",
    "historical_and_paper_are_execution_twin_models_not_actual_venue_fills",
    "does_not_establish_capacity_distributional_stationarity_or_tail_calibration",
    "does_not_establish_deployment_readiness_or_trading_authorization",
)


@dataclass(frozen=True, slots=True)
class NaturalTapeSufficiencyReceipt:
    path: Path
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class _FrozenFile:
    label: str
    path: str
    size: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    mode: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**dict(payload), "artifact_sha256": ""})).hexdigest()


def _lower_sha256(value: object, *, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase sha256")
    return value


def _required_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _accepts_keyword(loader: object, keyword: str) -> bool:
    try:
        return keyword in inspect.signature(loader).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _verify_top_level_freeze(
    freeze: Mapping[str, Any],
    *,
    freeze_path: Path,
    account_root: Path,
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
    safety_manifest: Mapping[str, Any],
    account_replay: Mapping[str, Any],
) -> dict[str, Any]:
    window = _required_mapping(freeze.get("window"), label="freeze window")
    runtime = _required_mapping(freeze.get("runtime"), label="freeze runtime")
    roots = _required_mapping(runtime.get("roots"), label="freeze roots")
    demo_roots = _required_mapping(roots.get("demo"), label="freeze demo roots")
    account_ids = _required_mapping(runtime.get("account_ids"), label="freeze account ids")
    repository = _required_mapping(freeze.get("repository"), label="freeze repository")
    clock = _required_mapping(freeze.get("clock"), label="freeze clock")
    clock_receipt = _required_mapping(clock.get("receipt"), label="freeze clock receipt")
    if window.get("t0_ns") != t0_ns or window.get("t1_ns") != t1_ns:
        raise ValueError("natural sufficiency window differs from the top-level freeze")
    if account_ids.get("demo") != expected_account_id:
        raise ValueError("natural sufficiency account id differs from the top-level freeze")
    if demo_roots.get("account") != str(account_root):
        raise ValueError("natural sufficiency account root differs from the top-level freeze")
    freeze_id = str(freeze.get("freeze_id") or "")
    if not freeze_id or safety_manifest.get("freeze_id") != freeze_id:
        raise ValueError("natural safety manifest names another top-level freeze")
    replay_freeze = _required_mapping(
        account_replay.get("natural_cutover_freeze"),
        label="captured-account replay freeze binding",
    )
    replay_roots = _required_mapping(
        account_replay.get("source_roots"),
        label="captured-account replay source roots",
    )
    if (
        replay_roots.get("natural_cutover_freeze_manifest_path") != str(freeze_path)
        or replay_freeze.get("path") != str(freeze_path)
        or replay_freeze.get("freeze_id") != freeze_id
        or replay_freeze.get("artifact_sha256") != freeze.get("artifact_sha256")
    ):
        raise ValueError("captured-account replay is bound to another top-level freeze")
    capture_root = str(demo_roots.get("capture") or "")
    if replay_roots.get("market_capture_root") != capture_root:
        raise ValueError("captured-account replay uses another frozen market-capture root")
    return {
        "path": str(freeze_path),
        "freeze_id": freeze_id,
        "artifact_sha256": _lower_sha256(freeze.get("artifact_sha256"), label="freeze artifact hash"),
        "candidate_commit": str(repository.get("candidate_commit") or ""),
        "origin_main_commit": str(repository.get("origin_main_commit") or ""),
        "clock_artifact_sha256": _lower_sha256(
            clock_receipt.get("artifact_sha256"),
            label="freeze clock artifact hash",
        ),
        "clock_file_sha256": _lower_sha256(
            clock_receipt.get("file_sha256"),
            label="freeze clock file hash",
        ),
    }


def _secure_directory(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    try:
        observed = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {candidate}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise ValueError(f"{label} must be a non-symlink directory: {candidate}")
    if observed.st_uid != os.geteuid():
        raise ValueError(f"{label} is not owned by the verifier")
    return candidate.resolve(strict=True)


def _read_secure_file(
    path: str | Path,
    *,
    label: str,
    snapshot: StableFileSnapshot | None = None,
) -> tuple[_FrozenFile, bytes]:
    if snapshot is None:
        snapshot = read_stable_file(
            path,
            label=label,
            require_owner=True,
            require_single_link=False,
        )
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError(f"{label} snapshot path differs")
    if snapshot.uid != os.geteuid():
        raise ValueError(f"{label} is not owned by the verifier")
    if snapshot.mode & 0o077:
        raise ValueError(f"{label} must not be accessible by group or other users")
    return (
        _FrozenFile(
            label=label,
            path=str(snapshot.path),
            size=snapshot.size,
            sha256=snapshot.sha256,
            device=snapshot.device,
            inode=snapshot.inode,
            mtime_ns=snapshot.mtime_ns,
            mode=snapshot.mode,
        ),
        snapshot.data,
    )


def _journal_paths(root: Path, *, prefix: str) -> dict[str, Path]:
    transaction_root = account_transactions_path(root)
    transactions = sorted(transaction_root.glob("*.json")) if transaction_root.is_dir() else []
    paths = {f"{prefix}/transaction/{path.name}": path for path in transactions}
    projection = account_journal_path(root)
    if projection.exists():
        paths[f"{prefix}/projection/events.jsonl"] = projection
    if not paths:
        raise ValueError(f"{prefix} has no account journal files")
    return paths


def _identity_from_mapping(value: object, *, label: str) -> _FrozenFile:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} source identity is invalid")
    required = {"label", "path", "size", "sha256", "device", "inode", "mtime_ns", "mode"}
    if set(value) != required:
        raise ValueError(f"{label} source identity fields changed")
    return _FrozenFile(
        label=str(value["label"]),
        path=str(value["path"]),
        size=int(value["size"]),
        sha256=_lower_sha256(value["sha256"], label=f"{label} sha256"),
        device=int(value["device"]),
        inode=int(value["inode"]),
        mtime_ns=int(value["mtime_ns"]),
        mode=int(value["mode"]),
    )


def _reopen_account_replay_sources(receipt: Mapping[str, Any]) -> dict[str, Path]:
    raw = receipt.get("source_files")
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("captured-account replay receipt lacks source identities")
    paths: dict[str, Path] = {}
    for source_label, value in sorted(raw.items(), key=lambda item: str(item[0])):
        if source_label == "effective_runtime_config_bundle":
            # Added directly below so its identity is not duplicated under an
            # opaque replay-prefixed label.
            continue
        expected = _identity_from_mapping(value, label=f"account replay/{source_label}")
        observed, _data = _read_secure_file(
            expected.path,
            label=f"account replay bound source {source_label}",
        )
        if expected.label != str(source_label) or (
            observed.path,
            observed.size,
            observed.sha256,
            observed.device,
            observed.inode,
            observed.mtime_ns,
            observed.mode,
        ) != (
            expected.path,
            expected.size,
            expected.sha256,
            expected.device,
            expected.inode,
            expected.mtime_ns,
            expected.mode,
        ):
            raise ValueError(f"captured-account replay source {source_label!r} changed")
        paths[f"account_replay_bound/{source_label}"] = Path(expected.path)
    return paths


def _collect_source_paths(
    *,
    long_event_tape: Path,
    long_outcome_tape: Path,
    continuous_event_tape: Path,
    continuous_outcome_tape: Path,
    target_capture: Path,
    demo_account_root: Path,
    safety_target_capture: Path,
    safety_manifest: Path,
    account_replay_receipt_path: Path,
    account_replay_receipt: Mapping[str, Any],
    venue_accounting_receipt_path: Path,
    freeze_manifest: Path,
    effective_runtime_config_bundle: Path,
) -> dict[str, Path]:
    paths = {
        "long/strategy_event_tape": long_event_tape,
        "long/strategy_outcome_tape": long_outcome_tape,
        "continuous/strategy_event_tape": continuous_event_tape,
        "continuous/strategy_outcome_tape": continuous_outcome_tape,
        "natural/target_capture": target_capture,
        "safety/target_capture": safety_target_capture,
        "safety/manifest": safety_manifest,
        "account_replay/receipt": account_replay_receipt_path,
        "venue_accounting/receipt": venue_accounting_receipt_path,
        "natural/freeze_manifest": freeze_manifest,
        "natural/effective_runtime_config_bundle": effective_runtime_config_bundle,
    }
    paths.update(_journal_paths(demo_account_root, prefix="demo_account"))
    paths.update(_reopen_account_replay_sources(account_replay_receipt))
    outputs = account_replay_receipt.get("outputs")
    if not isinstance(outputs, Mapping):
        raise ValueError("captured-account replay receipt lacks output roots")
    for environment in ("historical", "paper"):
        root = _secure_directory(
            str(outputs.get(f"{environment}_root") or ""),
            label=f"captured-account replay {environment} root",
        )
        paths.update(_journal_paths(root, prefix=f"account_replay_output/{environment}"))
    return paths


def _freeze_path_snapshots(
    paths: Mapping[str, Path],
) -> tuple[dict[str, _FrozenFile], dict[str, StableFileSnapshot]]:
    identities: dict[str, _FrozenFile] = {}
    snapshots: dict[str, StableFileSnapshot] = {}
    for label, path in sorted(paths.items()):
        snapshot = read_stable_file(
            path,
            label=label,
            require_owner=True,
            require_single_link=False,
        )
        identity, _data = _read_secure_file(path, label=label, snapshot=snapshot)
        identities[label] = identity
        snapshots[label] = snapshot
    return identities, snapshots


def _freeze_paths(paths: Mapping[str, Path]) -> dict[str, _FrozenFile]:
    identities, _snapshots = _freeze_path_snapshots(paths)
    return identities


def _journal_sha256(events: Sequence[AccountEvent]) -> str:
    return hashlib.sha256(canonical_json({"events": [event.to_dict() for event in events]})).hexdigest()


def _journal_from_snapshots(
    snapshots: Mapping[str, StableFileSnapshot],
    *,
    prefix: str,
) -> list[AccountEvent]:
    transaction_prefix = f"{prefix}/transaction/"
    transactions = [
        (label, snapshot.data) for label, snapshot in sorted(snapshots.items()) if label.startswith(transaction_prefix)
    ]
    if transactions:
        return read_account_journal_bytes(
            transaction_files=transactions,
            verify=True,
        )
    projection_label = f"{prefix}/projection/events.jsonl"
    projection = snapshots.get(projection_label)
    if projection is None:
        raise ValueError(f"{prefix} has no captured authoritative journal")
    return read_account_journal_bytes(
        projection_data=projection.data,
        projection_label=projection_label,
        verify=True,
    )


def _require_exact_window(t0_ns: int, t1_ns: int) -> None:
    if type(t0_ns) is not int or type(t1_ns) is not int:
        raise ValueError("natural window timestamps must be integers")
    if t0_ns <= 0 or t0_ns % HOUR_NS != 0:
        raise ValueError("natural T0 must be a positive UTC hour boundary")
    if t1_ns != t0_ns + NATURAL_WINDOW_HOURS * HOUR_NS:
        raise ValueError("natural window must be exactly 120 hours")


def _verify_sleeve_tapes(
    *,
    sleeve: str,
    event_tape_data: bytes,
    outcome_tape_data: bytes,
    capture_by_event_id: Mapping[str, TargetSchedulingCaptureEvent],
    t0_ns: int,
    t1_ns: int,
) -> dict[str, Any]:
    events, event_tape_hash = load_strategy_event_tape_bytes(event_tape_data)
    outcomes, outcome_tape_hash = load_strategy_event_decision_tape_bytes(outcome_tape_data)
    if not events:
        raise ValueError(f"{sleeve} natural event tape is empty")
    expected_source = f"{sleeve}:demo"
    expected_sequences = list(range(1, len(events) + 1))
    observed_sequences = [event.source_sequence for event in events]
    if observed_sequences != expected_sequences:
        raise ValueError(f"{sleeve} natural source sequence is not fresh and contiguous")
    event_by_id: dict[str, StrategyEvent] = {}
    covered_hours: set[int] = set()
    for event in events:
        if event.source != expected_source:
            raise ValueError(f"{sleeve} natural tape contains another source")
        if not (t0_ns <= event.event_ts_ns < t1_ns):
            raise ValueError(f"{sleeve} natural tape contains an out-of-window event")
        if event.ingest_ts_ns < event.event_ts_ns:
            raise ValueError(f"{sleeve} natural event has negative ingest latency")
        if event.payload.get("execution_environment") != "demo":
            raise ValueError(f"{sleeve} natural event is not demo")
        if event.payload.get("natural_evidence_required") is not True:
            raise ValueError(f"{sleeve} event was not produced in fail-fast natural evidence mode")
        profile = event.payload.get("strategy_profile")
        if type(profile) is not str or not profile:
            raise ValueError(f"{sleeve} natural event lacks strategy profile identity")
        event_by_id[event.event_id] = event
        covered_hours.add((event.event_ts_ns - t0_ns) // HOUR_NS)
    expected_hours = set(range(NATURAL_WINDOW_HOURS))
    if covered_hours != expected_hours:
        missing = sorted(expected_hours - covered_hours)
        raise ValueError(f"{sleeve} natural tape misses registered hours: {missing}")

    outcome_by_id = {outcome.event_id: outcome for outcome in outcomes}
    if set(outcome_by_id) != set(event_by_id):
        raise ValueError(f"{sleeve} raw-event and explicit-outcome sets differ")
    sleeve_capture_ids = {event_id for event_id, capture in capture_by_event_id.items() if capture.sleeve == sleeve}
    if sleeve_capture_ids != set(event_by_id):
        raise ValueError(f"{sleeve} raw-event and target-capture sets differ")
    successful_empty = 0
    request_count = 0
    for event_id, event in event_by_id.items():
        capture = capture_by_event_id[event_id]
        outcome = outcome_by_id[event_id]
        if capture.source_event != event:
            raise ValueError(f"{sleeve} target capture changed its raw source event")
        if capture.source_environment != "demo" or capture.sleeve != sleeve:
            raise ValueError(f"{sleeve} target capture changed source classification")
        if tuple(outcome.decision_keys) != tuple(capture.decision_keys):
            raise ValueError(f"{sleeve} explicit outcome differs from durable capture decisions")
        request_count += len(capture.requests)
        successful_empty += int(not capture.requests)
    return {
        "event_count": len(events),
        "outcome_count": len(outcomes),
        "capture_event_count": len(sleeve_capture_ids),
        "successful_empty_event_count": successful_empty,
        "durable_request_count": request_count,
        "covered_hour_count": len(covered_hours),
        "first_source_sequence": observed_sequences[0],
        "last_source_sequence": observed_sequences[-1],
        "event_tape_hash": event_tape_hash,
        "outcome_tape_hash": outcome_tape_hash,
    }


def _natural_requests(
    capture_events: Sequence[TargetSchedulingCaptureEvent],
    *,
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
) -> tuple[
    dict[str, CapturedTargetRequest],
    dict[str, str],
    dict[str, int],
]:
    by_batch: dict[str, CapturedTargetRequest] = {}
    sleeve_by_batch: dict[str, str] = {}
    event_ts_by_batch: dict[str, int] = {}
    request_ids: set[str] = set()
    for capture in capture_events:
        for captured in capture.requests:
            request = captured.request
            batch_id = request.batch_id
            if batch_id in by_batch:
                raise ValueError(f"natural target capture repeats batch {batch_id!r}")
            if request.request_id in request_ids:
                raise ValueError("natural target capture repeats request_id")
            if request.account_id != expected_account_id or request.environment != "demo":
                raise ValueError("natural captured request changed account/environment identity")
            if not (t0_ns <= request.created_ts_ns < t1_ns):
                raise ValueError("natural captured request was not created inside [T0,T1)")
            if request.created_ts_ns < capture.source_event.event_ts_ns:
                raise ValueError("natural captured request predates its source event")
            by_batch[batch_id] = captured
            sleeve_by_batch[batch_id] = capture.sleeve
            event_ts_by_batch[batch_id] = capture.source_event.event_ts_ns
            request_ids.add(request.request_id)
    return by_batch, sleeve_by_batch, event_ts_by_batch


def _verify_account_replay(
    receipt: Mapping[str, Any],
    *,
    target_capture: Path,
    demo_account_root: Path,
    safety_target_capture: Path,
    safety_manifest: Path,
    natural_requests: Mapping[str, CapturedTargetRequest],
    safety_batch_ids: frozenset[str],
    capture_event_count: int,
    durable_request_count: int,
    t0_ns: int,
    t1_ns: int,
    source_snapshots: Mapping[str, StableFileSnapshot],
) -> dict[str, Any]:
    natural_batch_ids = frozenset(natural_requests)
    roots = receipt.get("source_roots")
    if not isinstance(roots, Mapping):
        raise ValueError("captured-account replay receipt lacks source roots")
    expected_paths = {
        "target_capture_path": target_capture,
        "demo_account_root": demo_account_root,
        "post_window_safety_target_capture_path": safety_target_capture,
        "post_window_safety_manifest_path": safety_manifest,
    }
    for name, expected in expected_paths.items():
        observed = Path(str(roots.get(name) or "")).expanduser().resolve(strict=True)
        if observed != expected:
            raise ValueError(f"captured-account replay {name} differs from natural verifier input")
    raw_window = (receipt.get("input_manifest") or {}).get("natural_window")
    if raw_window != {"t0_ns": t0_ns, "t1_ns": t1_ns}:
        raise ValueError("captured-account replay changed the registered natural window")
    ordered_batches = receipt.get("ordered_batch_ids")
    if not isinstance(ordered_batches, list) or frozenset(str(value) for value in ordered_batches) != natural_batch_ids:
        raise ValueError("captured-account replay natural batch set differs")
    target_summary = receipt.get("target_capture")
    if not isinstance(target_summary, Mapping):
        raise ValueError("captured-account replay lacks target-capture summary")
    if (
        int(target_summary.get("event_count") or 0) != capture_event_count
        or int(target_summary.get("durable_request_count") or 0) != durable_request_count
    ):
        raise ValueError("captured-account replay target-capture counts differ")
    safety = receipt.get("post_window_safety")
    classification = safety.get("journal_classification") if isinstance(safety, Mapping) else None
    if not isinstance(classification, Mapping) or classification.get("batch_set_exact") is not True:
        raise ValueError("captured-account replay lacks exact safety-batch classification")
    classified_safety_batches = classification.get("registered_batch_ids")
    if (
        not isinstance(classified_safety_batches, list)
        or frozenset(str(value) for value in classified_safety_batches) != safety_batch_ids
    ):
        raise ValueError("captured-account replay safety batch identities differ")
    raw_mappings = receipt.get("request_batch_mappings")
    if not isinstance(raw_mappings, list):
        raise ValueError("captured-account replay lacks request/batch mappings")
    mapping_by_batch: dict[str, Mapping[str, Any]] = {}
    for raw_mapping in raw_mappings:
        if not isinstance(raw_mapping, Mapping):
            raise ValueError("captured-account replay has an invalid request/batch mapping")
        batch_id = str(raw_mapping.get("batch_id") or "")
        if not batch_id or batch_id in mapping_by_batch:
            raise ValueError("captured-account replay repeats a request/batch mapping")
        mapping_by_batch[batch_id] = raw_mapping
    if frozenset(mapping_by_batch) != natural_batch_ids:
        raise ValueError("captured-account replay mapping batch set differs")
    for batch_id, captured in natural_requests.items():
        mapping = mapping_by_batch[batch_id]
        if (
            mapping.get("request_id") != captured.request.request_id
            or mapping.get("request_hash") != captured.request_hash
        ):
            raise ValueError("captured-account replay mapping changed request identity")
    required_true = (
        "historical_paper_exact_outcome_passed",
        "demo_plan_parity_passed",
        "exact_preexecution_plan_match",
        "has_durable_request_batches",
    )
    if any(receipt.get(name) is not True for name in required_true):
        raise ValueError("captured-account replay did not pass every required plan gate")
    if receipt.get("execution_authorization") != "not_granted":
        raise ValueError("captured-account replay improperly grants execution authority")
    outputs = receipt.get("outputs")
    if not isinstance(outputs, Mapping):
        raise ValueError("captured-account replay lacks modeled output identities")
    for environment in ("historical", "paper"):
        _secure_directory(
            str(outputs.get(f"{environment}_root") or ""),
            label=f"captured-account replay {environment} root",
        )
        observed_hash = _journal_sha256(
            _journal_from_snapshots(
                source_snapshots,
                prefix=f"account_replay_output/{environment}",
            )
        )
        if observed_hash != outputs.get(f"{environment}_account_journal_sha256"):
            raise ValueError(f"captured-account replay {environment} output changed")
    return {
        "artifact_sha256": receipt["artifact_sha256"],
        "historical_paper_exact_outcome_passed": True,
        "demo_plan_parity_passed": True,
        "exact_preexecution_plan_match": True,
        "natural_batch_set_exact": True,
        "safety_batch_set_exact": True,
    }


def _verify_venue_accounting(
    receipt: Mapping[str, Any],
    *,
    account_root: Path,
    expected_account_id: str,
    events: Sequence[AccountEvent],
    t0_ns: int,
    t1_ns: int,
) -> dict[str, Any]:
    observed_root = Path(str(receipt.get("account_root") or "")).expanduser().resolve(strict=True)
    if observed_root != account_root:
        raise ValueError("venue-accounting receipt names another account root")
    if receipt.get("account_id") != expected_account_id:
        raise ValueError("venue-accounting receipt names another account")
    journal = receipt.get("journal")
    if not isinstance(journal, Mapping) or journal.get("normalized_journal_sha256") != _journal_sha256(events):
        raise ValueError("venue-accounting receipt journal identity differs")
    window = receipt.get("query_window_ms")
    if not isinstance(window, Mapping):
        raise ValueError("venue-accounting receipt lacks query window")
    if int(window.get("start") or 0) > t0_ns // 1_000_000:
        raise ValueError("venue-accounting query begins after natural T0")
    if int(window.get("end") or 0) < t1_ns // 1_000_000:
        raise ValueError("venue-accounting query ends before natural T1")
    if receipt.get("venue_accounting_gate_passed") is not True:
        raise ValueError("venue-accounting reconciliation did not pass")
    if receipt.get("final_demo_flatness_gate_passed") is not True:
        raise ValueError("final demo flatness did not pass")
    return {
        "artifact_sha256": receipt["artifact_sha256"],
        "venue_accounting_gate_passed": True,
        "final_demo_flatness_gate_passed": True,
        "query_window_ms": dict(window),
        "sample_counts": dict(receipt.get("sample_counts") or {}),
    }


def _verify_journal_lineage(
    events: Sequence[AccountEvent],
    *,
    expected_account_id: str,
    natural_requests: Mapping[str, CapturedTargetRequest],
    sleeve_by_batch: Mapping[str, str],
    safety_batch_ids: frozenset[str],
    t0_ns: int,
    t1_ns: int,
) -> dict[str, Any]:
    if not events or events[0].sequence != 1 or events[0].prev_event_hash != GENESIS_HASH:
        raise ValueError("natural demo journal is not a fresh genesis journal")
    if any(event.account_id != expected_account_id for event in events):
        raise ValueError("natural demo journal contains another account id")
    state = reduce_account_events(events)
    natural_batch_ids = frozenset(natural_requests)
    if natural_batch_ids & safety_batch_ids:
        raise ValueError("natural and post-window safety batch sets overlap")
    risks = [event for event in events if event.event_type == AccountEventType.RISK_DECISION.value]
    risk_by_batch: dict[str, AccountEvent] = {}
    for event in risks:
        if event.correlation_id in risk_by_batch:
            raise ValueError(f"demo journal repeats RISK_DECISION batch {event.correlation_id!r}")
        risk_by_batch[event.correlation_id] = event
    expected_risks = natural_batch_ids | safety_batch_ids
    if frozenset(risk_by_batch) != expected_risks:
        raise ValueError("demo RISK_DECISION set is not exactly natural plus registered safety batches")
    for batch_id, captured in natural_requests.items():
        event = risk_by_batch[batch_id]
        request = captured.request
        if not (t0_ns <= event.wall_ts_ns < t1_ns):
            raise ValueError(f"natural risk batch {batch_id!r} was not planned inside [T0,T1)")
        if event.wall_ts_ns < request.created_ts_ns:
            raise ValueError(f"natural risk batch {batch_id!r} predates its durable request")
        payload = event.payload
        if (
            payload.get("batch_id", batch_id) != batch_id
            or _lower_sha256(
                payload.get("request_hash"),
                label=f"natural risk batch {batch_id!r} request hash",
            )
            != payload.get("request_hash")
            or type(payload.get("accepted")) is not bool
        ):
            raise ValueError(f"natural risk batch {batch_id!r} changed request identity")

    command_events = [event for event in events if event.event_type == AccountEventType.ORDER_COMMAND.value]
    command_by_id: dict[str, AccountEvent] = {}
    for event in command_events:
        command_id = str(event.payload.get("command_id") or "")
        if not command_id or command_id in command_by_id:
            raise ValueError("demo journal has missing or duplicate order command identity")
        if event.correlation_id not in expected_risks:
            raise ValueError("demo journal has an order command outside natural/safety scope")
        if event.correlation_id in natural_batch_ids and not (t0_ns <= event.wall_ts_ns < t1_ns):
            raise ValueError("natural command was not planned inside [T0,T1)")
        command_by_id[command_id] = event

    fills_by_command: dict[str, list[AccountEvent]] = {}
    for event in events:
        if event.event_type != AccountEventType.FILL.value:
            continue
        command_id = str(event.payload.get("command_id") or "")
        if command_id not in command_by_id:
            raise ValueError("demo fill has no canonical order command")
        fills_by_command.setdefault(command_id, []).append(event)

    filled_natural_commands: list[str] = []
    filled_per_sleeve = {sleeve: 0 for sleeve in _SLEEVES}
    filled_symbols: set[str] = set()
    natural_execution_ids: set[str] = set()
    safety_execution_ids: set[str] = set()
    post_t1_terminal_fill_commands = 0
    for command_id, command in command_by_id.items():
        batch_id = command.correlation_id
        fills = sorted(fills_by_command.get(command_id, []), key=lambda event: event.sequence)
        execution_ids = [str(event.payload.get("execution_id") or "") for event in fills]
        if any(not value for value in execution_ids) or len(set(execution_ids)) != len(execution_ids):
            raise ValueError("demo command has missing or duplicate fill execution identity")
        if batch_id in safety_batch_ids:
            safety_execution_ids.update(execution_ids)
            continue
        if not fills:
            continue
        if any(event.wall_ts_ns < t0_ns for event in fills):
            raise ValueError("natural command has a pre-T0 fill")
        post_t1 = [event for event in fills if event.wall_ts_ns >= t1_ns]
        if post_t1:
            if len(post_t1) != 1 or post_t1[0] is not fills[-1]:
                raise ValueError("a non-terminal natural fill arrived after T1")
            signed_command_qty = float(command.payload.get("signed_qty") or 0.0)
            cumulative_qty = math.fsum(float(event.payload.get("signed_qty") or 0.0) for event in fills)
            tolerance = max(abs(signed_command_qty) * 1e-12, 1e-12)
            metadata = post_t1[0].payload.get("metadata") or {}
            terminal_fill = math.isclose(
                cumulative_qty,
                signed_command_qty,
                rel_tol=0.0,
                abs_tol=tolerance,
            ) or (isinstance(metadata, Mapping) and metadata.get("terminal") is True)
            order = state.orders.get(command_id)
            if not terminal_fill or order is None or order.status not in _TERMINAL_ORDER_STATUSES:
                raise ValueError("post-T1 natural fill is not the terminal command observation")
            post_t1_terminal_fill_commands += 1
        sleeve = sleeve_by_batch[batch_id]
        filled_natural_commands.append(command_id)
        filled_per_sleeve[sleeve] += 1
        filled_symbols.add(command.symbol)
        natural_execution_ids.update(execution_ids)

    if natural_execution_ids & safety_execution_ids:
        raise ValueError("natural and safety execution identities overlap")
    return {
        "journal_event_count": len(events),
        "journal_head_sequence": events[-1].sequence,
        "journal_head_event_hash": events[-1].event_hash,
        "journal_head_state_hash": events[-1].state_hash,
        "natural_risk_batch_count": len(natural_batch_ids),
        "safety_risk_batch_count": len(safety_batch_ids),
        "order_command_count": len(command_by_id),
        "filled_natural_command_ids": sorted(filled_natural_commands),
        "filled_command_count": len(filled_natural_commands),
        "filled_command_count_by_sleeve": filled_per_sleeve,
        "filled_symbols": sorted(filled_symbols),
        "natural_execution_ids": sorted(natural_execution_ids),
        "safety_execution_ids": sorted(safety_execution_ids),
        "post_t1_terminal_fill_command_count": post_t1_terminal_fill_commands,
    }


def _round_trip_counts(
    *,
    account_root: Path,
    account_events: Sequence[AccountEvent],
    natural_batch_ids: frozenset[str],
    sleeve_by_batch: Mapping[str, str],
    event_ts_by_batch: Mapping[str, int],
    t0_ns: int,
    t1_ns: int,
) -> tuple[dict[str, int], list[dict[str, str]]]:
    counts = {sleeve: 0 for sleeve in _SLEEVES}
    rows: list[dict[str, str]] = []
    for sleeve in _SLEEVES:
        frame = canonical_strategy_trade_rows(
            account_root,
            sleeve=sleeve,
            account_events=account_events,
        )
        projected = [] if frame.is_empty() else frame.to_dicts()
        identities: set[tuple[str, str, str, str]] = set()
        for row in projected:
            entry_target_batch = str(row.get("entry_target_batch_id") or "")
            exit_target_batch = str(row.get("account_target_batch_id") or "")
            entry_execution_batch = str(row.get("entry_execution_batch_id") or "")
            exit_execution_batch = str(row.get("exit_execution_batch_id") or "")
            required_batches = {
                entry_target_batch,
                exit_target_batch,
                entry_execution_batch,
                exit_execution_batch,
            }
            if "" in required_batches or not required_batches <= natural_batch_ids:
                continue
            if any(sleeve_by_batch.get(batch_id) != sleeve for batch_id in required_batches):
                continue
            if any(
                not (t0_ns <= event_ts_by_batch.get(batch_id, 0) < t1_ns)
                for batch_id in (entry_target_batch, exit_target_batch)
            ):
                continue
            if (
                row.get("status") != "closed"
                or row.get("entry_fill_complete") is not True
                or row.get("exit_fill_complete") is not True
                or str(row.get("entry_attribution_scope") or "") == "none"
                or str(row.get("exit_attribution_scope") or "") == "none"
                or str(row.get("account_lifecycle_attribution_status") or "")
                in {"", "missing_entry_transition", "pending_first_fill"}
                or str(row.get("account_close_attribution_status") or "")
                in {"", "not_closed", "pending_reduction_fill"}
            ):
                continue
            identity = (
                entry_execution_batch,
                exit_execution_batch,
                str(row.get("symbol") or "").upper(),
                sleeve,
            )
            if identity in identities:
                continue
            identities.add(identity)
            rows.append(
                {
                    "entry_execution_batch_id": identity[0],
                    "exit_execution_batch_id": identity[1],
                    "symbol": identity[2],
                    "sleeve": identity[3],
                }
            )
        counts[sleeve] = len(identities)
    rows.sort(
        key=lambda row: (
            row["sleeve"],
            row["entry_execution_batch_id"],
            row["exit_execution_batch_id"],
            row["symbol"],
        )
    )
    return counts, rows


def _natural_pnl_events(
    events: Sequence[AccountEvent],
    *,
    natural_execution_ids: frozenset[str],
    safety_execution_ids: frozenset[str],
) -> list[str]:
    output: list[str] = []
    accounted_seen: set[str] = set()
    for event in events:
        if event.event_type != AccountEventType.PNL.value:
            continue
        payload = event.payload
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("canonical PNL event has invalid metadata")
        is_fill_checkpoint = bool(metadata.get("fill_accounting_checkpoint")) or str(
            payload.get("source") or ""
        ).startswith("fill_reconstructed")
        if not is_fill_checkpoint:
            continue
        raw_ids = metadata.get("accounted_execution_ids") or ()
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes, bytearray)):
            raise ValueError("fill-accounting PNL event lacks execution lineage")
        execution_ids = {str(value) for value in raw_ids if str(value)}
        if not execution_ids:
            raise ValueError("fill-accounting PNL event has empty execution lineage")
        duplicates = execution_ids & accounted_seen
        if duplicates:
            raise ValueError("canonical PNL events account for one execution more than once")
        accounted_seen.update(execution_ids)
        if execution_ids & safety_execution_ids:
            continue
        if not execution_ids <= natural_execution_ids:
            raise ValueError("canonical PNL event contains an execution outside natural/safety scope")
        output.append(event.event_id)
    return output


def build_natural_tape_sufficiency_receipt(
    *,
    long_event_tape: str | Path,
    long_outcome_tape: str | Path,
    continuous_event_tape: str | Path,
    continuous_outcome_tape: str | Path,
    target_capture_path: str | Path,
    demo_account_root: str | Path,
    safety_target_capture_path: str | Path,
    safety_manifest_path: str | Path,
    account_replay_receipt_path: str | Path,
    venue_accounting_receipt_path: str | Path,
    freeze_manifest_path: str | Path,
    effective_runtime_config_bundle_file: str | Path,
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
    created_ts_ns: int | None = None,
) -> dict[str, Any]:
    """Recompute the fixed-window integrity contract and activity floors."""

    _require_exact_window(t0_ns, t1_ns)
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= t1_ns:
        raise ValueError("natural tape sufficiency receipt must be created after the natural window")
    if type(expected_account_id) is not str or not expected_account_id.strip():
        raise ValueError("expected account id is required")
    account_root = _secure_directory(demo_account_root, label="natural demo account root")
    supplied_paths = {
        "long_event_tape": Path(long_event_tape),
        "long_outcome_tape": Path(long_outcome_tape),
        "continuous_event_tape": Path(continuous_event_tape),
        "continuous_outcome_tape": Path(continuous_outcome_tape),
        "target_capture": Path(target_capture_path),
        "safety_target_capture": Path(safety_target_capture_path),
        "safety_manifest": Path(safety_manifest_path),
        "account_replay_receipt": Path(account_replay_receipt_path),
        "venue_accounting_receipt": Path(venue_accounting_receipt_path),
        "freeze_manifest": Path(freeze_manifest_path),
        "effective_runtime_config_bundle": Path(effective_runtime_config_bundle_file),
    }
    named_paths: dict[str, Path] = {}
    supplied_snapshots: dict[str, StableFileSnapshot] = {}
    for name, supplied in supplied_paths.items():
        snapshot = read_stable_file(
            supplied,
            label=name,
            require_owner=True,
            require_single_link=False,
        )
        identity, _data = _read_secure_file(
            supplied,
            label=name,
            snapshot=snapshot,
        )
        named_paths[name] = Path(identity.path)
        supplied_snapshots[name] = snapshot
    raw_replay = json.loads(supplied_snapshots["account_replay_receipt"].data)
    if not isinstance(raw_replay, Mapping):
        raise ValueError("captured-account replay receipt must contain an object")
    replay_shape = verify_captured_account_replay_receipt(raw_replay)
    all_source_paths = _collect_source_paths(
        long_event_tape=named_paths["long_event_tape"],
        long_outcome_tape=named_paths["long_outcome_tape"],
        continuous_event_tape=named_paths["continuous_event_tape"],
        continuous_outcome_tape=named_paths["continuous_outcome_tape"],
        target_capture=named_paths["target_capture"],
        demo_account_root=account_root,
        safety_target_capture=named_paths["safety_target_capture"],
        safety_manifest=named_paths["safety_manifest"],
        account_replay_receipt_path=named_paths["account_replay_receipt"],
        account_replay_receipt=replay_shape,
        venue_accounting_receipt_path=named_paths["venue_accounting_receipt"],
        freeze_manifest=named_paths["freeze_manifest"],
        effective_runtime_config_bundle=named_paths["effective_runtime_config_bundle"],
    )
    before, source_snapshots = _freeze_path_snapshots(all_source_paths)
    supplied_labels = {
        "long_event_tape": "long/strategy_event_tape",
        "long_outcome_tape": "long/strategy_outcome_tape",
        "continuous_event_tape": "continuous/strategy_event_tape",
        "continuous_outcome_tape": "continuous/strategy_outcome_tape",
        "target_capture": "natural/target_capture",
        "safety_target_capture": "safety/target_capture",
        "safety_manifest": "safety/manifest",
        "account_replay_receipt": "account_replay/receipt",
        "venue_accounting_receipt": "venue_accounting/receipt",
        "freeze_manifest": "natural/freeze_manifest",
        "effective_runtime_config_bundle": "natural/effective_runtime_config_bundle",
    }
    for name, label in supplied_labels.items():
        initial, _data = _read_secure_file(
            supplied_snapshots[name].path,
            label=name,
            snapshot=supplied_snapshots[name],
        )
        initial_identity = initial.to_dict()
        frozen_identity = before[label].to_dict()
        initial_identity.pop("label")
        frozen_identity.pop("label")
        if initial_identity != frozen_identity:
            raise RuntimeError(f"{name} changed while natural sources were frozen")

    if _accepts_keyword(load_captured_account_replay_receipt, "snapshot"):
        account_replay_receipt = load_captured_account_replay_receipt(
            named_paths["account_replay_receipt"],
            snapshot=source_snapshots["account_replay/receipt"],
        )
    else:
        account_replay_receipt = load_captured_account_replay_receipt(named_paths["account_replay_receipt"])
    raw_venue = json.loads(source_snapshots["venue_accounting/receipt"].data)
    if not isinstance(raw_venue, Mapping):
        raise ValueError("venue-accounting receipt must contain an object")
    venue_accounting_receipt = verify_venue_accounting_receipt(raw_venue)
    if _accepts_keyword(
        load_effective_runtime_config_bundle_binding,
        "snapshot",
    ):
        _effective_payload, effective_runtime_config = load_effective_runtime_config_bundle_binding(
            named_paths["effective_runtime_config_bundle"],
            snapshot=source_snapshots["natural/effective_runtime_config_bundle"],
        )
    else:
        _effective_payload, effective_runtime_config = load_effective_runtime_config_bundle_binding(
            named_paths["effective_runtime_config_bundle"]
        )

    capture_events, capture_tape_hash = load_target_scheduling_capture_bytes(
        source_snapshots["natural/target_capture"].data
    )
    if not capture_events:
        raise ValueError("natural target capture is empty")
    capture_by_event_id = {capture.source_event.event_id: capture for capture in capture_events}
    if len(capture_by_event_id) != len(capture_events):
        raise ValueError("natural target capture repeats a raw source event")
    if any(capture.sleeve not in _SLEEVES for capture in capture_events):
        raise ValueError("natural target capture contains an unregistered sleeve")
    tape_summaries = {
        "long": _verify_sleeve_tapes(
            sleeve="long",
            event_tape_data=source_snapshots["long/strategy_event_tape"].data,
            outcome_tape_data=source_snapshots["long/strategy_outcome_tape"].data,
            capture_by_event_id=capture_by_event_id,
            t0_ns=t0_ns,
            t1_ns=t1_ns,
        ),
        "continuous": _verify_sleeve_tapes(
            sleeve="continuous",
            event_tape_data=source_snapshots["continuous/strategy_event_tape"].data,
            outcome_tape_data=source_snapshots["continuous/strategy_outcome_tape"].data,
            capture_by_event_id=capture_by_event_id,
            t0_ns=t0_ns,
            t1_ns=t1_ns,
        ),
    }
    natural_requests, sleeve_by_batch, event_ts_by_batch = _natural_requests(
        capture_events,
        expected_account_id=expected_account_id,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
    )
    safety_manifest = load_post_window_safety_manifest(
        named_paths["safety_manifest"],
        target_capture_path=named_paths["safety_target_capture"],
        expected_account_id=expected_account_id,
        expected_t1_ns=t1_ns,
        manifest_snapshot=source_snapshots["safety/manifest"],
        capture_snapshot=source_snapshots["safety/target_capture"],
    )
    if _accepts_keyword(load_natural_cutover_freeze_manifest, "snapshot"):
        freeze = load_natural_cutover_freeze_manifest(
            named_paths["freeze_manifest"],
            snapshot=source_snapshots["natural/freeze_manifest"],
        )
    else:
        freeze = load_natural_cutover_freeze_manifest(named_paths["freeze_manifest"])
    freeze_binding = _verify_top_level_freeze(
        freeze,
        freeze_path=named_paths["freeze_manifest"],
        account_root=account_root,
        expected_account_id=expected_account_id,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        safety_manifest=safety_manifest,
        account_replay=account_replay_receipt,
    )
    population = _required_mapping(freeze.get("population"), label="freeze population")
    candidate = _required_mapping(population.get("candidate_universe"), label="freeze candidate universe")
    effective_runtime_config = validate_effective_runtime_config_bundle_join(
        effective_runtime_config,
        freeze_manifest_path=named_paths["freeze_manifest"],
        freeze_manifest_file_sha256=before["natural/freeze_manifest"].sha256,
        freeze_artifact_sha256=str(freeze_binding["artifact_sha256"]),
        freeze_id=str(freeze_binding["freeze_id"]),
        candidate_universe_path=str(candidate.get("path") or ""),
        candidate_universe_file_sha256=_lower_sha256(
            candidate.get("file_sha256"), label="candidate-universe file hash"
        ),
        candidate_universe_artifact_sha256=_lower_sha256(
            candidate.get("artifact_sha256"),
            label="candidate-universe artifact hash",
        ),
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        target_capture_path=named_paths["target_capture"],
        sleeve_tape_paths={
            "long": {
                "event_tape_path": named_paths["long_event_tape"],
                "outcome_tape_path": named_paths["long_outcome_tape"],
            },
            "continuous": {
                "event_tape_path": named_paths["continuous_event_tape"],
                "outcome_tape_path": named_paths["continuous_outcome_tape"],
            },
        },
    )
    if (
        effective_runtime_config.get("file_sha256") != before["natural/effective_runtime_config_bundle"].sha256
        or account_replay_receipt.get("effective_runtime_config") != effective_runtime_config
    ):
        raise ValueError("natural sufficiency/replay effective runtime config bindings differ")
    safety_capture_events, safety_capture_tape_hash = load_target_scheduling_capture_bytes(
        source_snapshots["safety/target_capture"].data
    )
    safety_requests = [captured for capture in safety_capture_events for captured in capture.requests]
    safety_batch_ids = frozenset(captured.request.batch_id for captured in safety_requests)
    if len(safety_batch_ids) != len(safety_requests):
        raise ValueError("post-window safety target capture repeats a batch")

    events = _journal_from_snapshots(
        source_snapshots,
        prefix="demo_account",
    )
    lineage = _verify_journal_lineage(
        events,
        expected_account_id=expected_account_id,
        natural_requests=natural_requests,
        sleeve_by_batch=sleeve_by_batch,
        safety_batch_ids=safety_batch_ids,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
    )
    natural_batch_ids = frozenset(natural_requests)
    account_replay = _verify_account_replay(
        account_replay_receipt,
        target_capture=named_paths["target_capture"],
        demo_account_root=account_root,
        safety_target_capture=named_paths["safety_target_capture"],
        safety_manifest=named_paths["safety_manifest"],
        natural_requests=natural_requests,
        safety_batch_ids=safety_batch_ids,
        capture_event_count=len(capture_events),
        durable_request_count=len(natural_requests),
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        source_snapshots=source_snapshots,
    )
    venue_accounting = _verify_venue_accounting(
        venue_accounting_receipt,
        account_root=account_root,
        expected_account_id=expected_account_id,
        events=events,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
    )
    round_trip_counts, round_trip_rows = _round_trip_counts(
        account_root=account_root,
        account_events=events,
        natural_batch_ids=natural_batch_ids,
        sleeve_by_batch=sleeve_by_batch,
        event_ts_by_batch=event_ts_by_batch,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
    )
    natural_pnl_event_ids = _natural_pnl_events(
        events,
        natural_execution_ids=frozenset(lineage["natural_execution_ids"]),
        safety_execution_ids=frozenset(lineage["safety_execution_ids"]),
    )

    floor_gates = {
        "filled_commands": lineage["filled_command_count"] >= NATURAL_MIN_FILLED_COMMANDS,
        "long_filled_commands": lineage["filled_command_count_by_sleeve"]["long"]
        >= NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE,
        "continuous_filled_commands": lineage["filled_command_count_by_sleeve"]["continuous"]
        >= NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE,
        "filled_symbols": len(lineage["filled_symbols"]) >= NATURAL_MIN_FILLED_SYMBOLS,
        "long_round_trips": round_trip_counts["long"] >= NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE,
        "continuous_round_trips": round_trip_counts["continuous"] >= NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE,
        "canonical_pnl_events": len(natural_pnl_event_ids) >= NATURAL_MIN_PNL_EVENTS,
    }
    sufficiency_passed = all(floor_gates.values())
    after, final_source_snapshots = _freeze_path_snapshots(all_source_paths)
    if after != before:
        raise RuntimeError("natural sufficiency sources changed during verification")
    if _accepts_keyword(
        load_effective_runtime_config_bundle_binding,
        "snapshot",
    ):
        _final_effective_payload, final_effective_runtime_config = load_effective_runtime_config_bundle_binding(
            named_paths["effective_runtime_config_bundle"],
            snapshot=final_source_snapshots["natural/effective_runtime_config_bundle"],
        )
    else:
        _final_effective_payload, final_effective_runtime_config = load_effective_runtime_config_bundle_binding(
            named_paths["effective_runtime_config_bundle"]
        )
    if final_effective_runtime_config != effective_runtime_config:
        raise RuntimeError("effective runtime config sources changed during sufficiency verification")

    payload: dict[str, Any] = {
        "schema_version": NATURAL_TAPE_SUFFICIENCY_SCHEMA_VERSION,
        "kind": NATURAL_TAPE_SUFFICIENCY_KIND,
        "created_ts_ns": created,
        "evidence_scope": "fixed_120h_natural_demo_integrity_and_lifecycle_floors",
        "expected_account_id": expected_account_id,
        "natural_window": {
            "t0_ns": t0_ns,
            "t1_ns": t1_ns,
            "hours": NATURAL_WINDOW_HOURS,
            "interval": "half_open",
        },
        "input_paths": {
            **{name: str(path) for name, path in sorted(named_paths.items())},
            "demo_account_root": str(account_root),
        },
        "source_files": {label: identity.to_dict() for label, identity in sorted(before.items())},
        "natural_cutover_freeze": freeze_binding,
        "effective_runtime_config": effective_runtime_config,
        "source_set_sha256": hashlib.sha256(
            canonical_json({label: identity.to_dict() for label, identity in sorted(before.items())})
        ).hexdigest(),
        "target_capture": {
            "capture_tape_hash": capture_tape_hash,
            "event_count": len(capture_events),
            "durable_request_count": len(natural_requests),
            "natural_batch_ids": sorted(natural_batch_ids),
        },
        "sleeve_tapes": tape_summaries,
        "post_window_safety": {
            "freeze_id": safety_manifest["freeze_id"],
            "manifest_artifact_sha256": safety_manifest["artifact_sha256"],
            "capture_tape_hash": safety_capture_tape_hash,
            "capture_event_count": len(safety_capture_events),
            "durable_request_count": len(safety_requests),
            "batch_ids": sorted(safety_batch_ids),
            "excluded_from_all_natural_floors": True,
            "included_in_final_venue_accounting_and_flatness": True,
        },
        "account_lineage": lineage,
        "round_trips": {
            "count_by_sleeve": round_trip_counts,
            "conservatively_deduplicated_rows": round_trip_rows,
        },
        "canonical_natural_pnl": {
            "event_count": len(natural_pnl_event_ids),
            "event_ids": natural_pnl_event_ids,
        },
        "account_replay": account_replay,
        "venue_accounting": venue_accounting,
        "registered_floors": {
            "filled_commands": NATURAL_MIN_FILLED_COMMANDS,
            "filled_commands_per_sleeve": NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE,
            "filled_symbols": NATURAL_MIN_FILLED_SYMBOLS,
            "round_trips_per_sleeve": NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE,
            "canonical_pnl_events": NATURAL_MIN_PNL_EVENTS,
        },
        "floor_gates": floor_gates,
        "integrity_gate_passed": True,
        "status": "passed" if sufficiency_passed else "inconclusive",
        "sufficiency_gate_passed": sufficiency_passed,
        "execution_authorization": "not_granted",
        "limitations": list(_LIMITATIONS),
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    verify_natural_tape_sufficiency_receipt(payload)
    return payload


def verify_natural_tape_sufficiency_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    if int(value.get("schema_version") or 0) != NATURAL_TAPE_SUFFICIENCY_SCHEMA_VERSION:
        raise ValueError("unknown natural tape sufficiency schema")
    if value.get("kind") != NATURAL_TAPE_SUFFICIENCY_KIND:
        raise ValueError("unexpected natural tape sufficiency kind")
    if value.get("evidence_scope") != "fixed_120h_natural_demo_integrity_and_lifecycle_floors":
        raise ValueError("natural tape sufficiency evidence scope changed")
    observed = value.get("artifact_sha256")
    if observed != _self_hash(value):
        raise ValueError("natural tape sufficiency receipt hash mismatch")
    if value.get("execution_authorization") != "not_granted":
        raise ValueError("natural tape sufficiency receipt cannot grant execution authority")
    if value.get("limitations") != list(_LIMITATIONS):
        raise ValueError("natural tape sufficiency limitations changed")
    window = value.get("natural_window")
    if not isinstance(window, Mapping):
        raise ValueError("natural tape sufficiency receipt lacks its fixed window")
    _require_exact_window(
        int(window.get("t0_ns") or 0),
        int(window.get("t1_ns") or 0),
    )
    if int(value.get("created_ts_ns") or 0) <= int(window.get("t1_ns") or 0):
        raise ValueError("natural tape sufficiency receipt creation time is not post-window")
    if window.get("hours") != NATURAL_WINDOW_HOURS or window.get("interval") != "half_open":
        raise ValueError("natural tape sufficiency window semantics changed")
    expected_floors = {
        "filled_commands": NATURAL_MIN_FILLED_COMMANDS,
        "filled_commands_per_sleeve": NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE,
        "filled_symbols": NATURAL_MIN_FILLED_SYMBOLS,
        "round_trips_per_sleeve": NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE,
        "canonical_pnl_events": NATURAL_MIN_PNL_EVENTS,
    }
    if value.get("registered_floors") != expected_floors:
        raise ValueError("natural tape sufficiency registered floors changed")
    source_files = value.get("source_files")
    if not isinstance(source_files, Mapping) or not source_files:
        raise ValueError("natural tape sufficiency receipt lacks source identities")
    if value.get("source_set_sha256") != hashlib.sha256(canonical_json(dict(source_files))).hexdigest():
        raise ValueError("natural tape sufficiency source-set hash mismatch")
    freeze = value.get("natural_cutover_freeze")
    effective = value.get("effective_runtime_config")
    safety = value.get("post_window_safety")
    if not isinstance(freeze, Mapping) or not isinstance(effective, Mapping) or not isinstance(safety, Mapping):
        raise ValueError("natural tape sufficiency receipt lacks freeze/config/safety binding")
    if freeze.get("freeze_id") != safety.get("freeze_id") or freeze.get("path") != value.get("input_paths", {}).get(
        "freeze_manifest"
    ):
        raise ValueError("natural tape sufficiency freeze/safety binding changed")
    _lower_sha256(freeze.get("artifact_sha256"), label="freeze artifact hash")
    _lower_sha256(freeze.get("clock_artifact_sha256"), label="freeze clock artifact hash")
    _lower_sha256(freeze.get("clock_file_sha256"), label="freeze clock file hash")
    if effective.get("execution_authorization") != "not_granted":
        raise ValueError("effective runtime config binding cannot grant execution")
    lineage = value.get("account_lineage")
    round_trips = value.get("round_trips")
    pnl = value.get("canonical_natural_pnl")
    if (
        not isinstance(lineage, Mapping)
        or not isinstance(round_trips, Mapping)
        or not isinstance(round_trips.get("count_by_sleeve"), Mapping)
        or not isinstance(pnl, Mapping)
    ):
        raise ValueError("natural tape sufficiency receipt lacks lifecycle metrics")
    by_sleeve = lineage.get("filled_command_count_by_sleeve")
    filled_symbols = lineage.get("filled_symbols")
    if (
        not isinstance(by_sleeve, Mapping)
        or set(by_sleeve) != set(_SLEEVES)
        or any(type(count) is not int or count < 0 for count in by_sleeve.values())
        or not isinstance(filled_symbols, list)
        or any(type(symbol) is not str or not symbol for symbol in filled_symbols)
        or len(set(filled_symbols)) != len(filled_symbols)
    ):
        raise ValueError("natural tape sufficiency receipt has invalid sleeve/symbol fill metrics")
    round_trip_counts = round_trips["count_by_sleeve"]
    if set(round_trip_counts) != set(_SLEEVES) or any(
        type(count) is not int or count < 0 for count in round_trip_counts.values()
    ):
        raise ValueError("natural tape sufficiency receipt has invalid round-trip counts")
    filled_command_count = lineage.get("filled_command_count")
    pnl_event_count = pnl.get("event_count")
    if (
        type(filled_command_count) is not int
        or filled_command_count < 0
        or type(pnl_event_count) is not int
        or pnl_event_count < 0
    ):
        raise ValueError("natural tape sufficiency receipt has invalid lifecycle counts")
    expected_gates = {
        "filled_commands": filled_command_count >= NATURAL_MIN_FILLED_COMMANDS,
        "long_filled_commands": by_sleeve["long"] >= NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE,
        "continuous_filled_commands": by_sleeve["continuous"] >= NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE,
        "filled_symbols": len(filled_symbols) >= NATURAL_MIN_FILLED_SYMBOLS,
        "long_round_trips": round_trip_counts["long"] >= NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE,
        "continuous_round_trips": round_trip_counts["continuous"] >= NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE,
        "canonical_pnl_events": pnl_event_count >= NATURAL_MIN_PNL_EVENTS,
    }
    gates = value.get("floor_gates")
    if not isinstance(gates, Mapping) or any(type(item) is not bool for item in gates.values()):
        raise ValueError("natural tape sufficiency floor gates are invalid")
    if dict(gates) != expected_gates:
        raise ValueError("natural tape sufficiency gates do not reproduce from metrics")
    passed = all(gates.values())
    if value.get("sufficiency_gate_passed") is not passed:
        raise ValueError("natural tape sufficiency status disagrees with its gates")
    if value.get("status") != ("passed" if passed else "inconclusive"):
        raise ValueError("natural tape sufficiency label disagrees with its gates")
    if value.get("integrity_gate_passed") is not True:
        raise ValueError("published natural tape sufficiency receipt must have valid integrity")
    return value


def _write_new_receipt(path: Path, payload: Mapping[str, Any]) -> Path:
    output = path.expanduser()
    if not output.is_absolute():
        raise ValueError("natural tape sufficiency receipt output must be absolute")
    output.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(payload) + b"\n"
    descriptor = os.open(str(output), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("natural tape sufficiency receipt write made no progress")
                offset += written
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    directory = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return output


def write_natural_tape_sufficiency_receipt(
    path: str | Path,
    payload: Mapping[str, Any],
) -> NaturalTapeSufficiencyReceipt:
    verified = verify_natural_tape_sufficiency_receipt(payload)
    output = _write_new_receipt(Path(path), verified)
    loaded = load_natural_tape_sufficiency_receipt(output)
    if canonical_json(loaded) != canonical_json(verified):
        raise RuntimeError("published natural tape sufficiency receipt changed")
    return NaturalTapeSufficiencyReceipt(path=output, payload=loaded)


def load_natural_tape_sufficiency_receipt(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    identity, data = _read_secure_file(
        path,
        label="natural tape sufficiency receipt",
        snapshot=snapshot,
    )
    if identity.mode != 0o600:
        raise ValueError("natural tape sufficiency receipt must have mode 0600")
    raw = json.loads(data)
    if not isinstance(raw, Mapping):
        raise ValueError("natural tape sufficiency receipt must contain an object")
    value = verify_natural_tape_sufficiency_receipt(raw)
    inputs = value.get("input_paths")
    window = value.get("natural_window")
    if not isinstance(inputs, Mapping) or not isinstance(window, Mapping):
        raise ValueError("natural tape sufficiency receipt lacks replayable inputs")
    rebuilt = build_natural_tape_sufficiency_receipt(
        long_event_tape=str(inputs.get("long_event_tape") or ""),
        long_outcome_tape=str(inputs.get("long_outcome_tape") or ""),
        continuous_event_tape=str(inputs.get("continuous_event_tape") or ""),
        continuous_outcome_tape=str(inputs.get("continuous_outcome_tape") or ""),
        target_capture_path=str(inputs.get("target_capture") or ""),
        demo_account_root=str(inputs.get("demo_account_root") or ""),
        safety_target_capture_path=str(inputs.get("safety_target_capture") or ""),
        safety_manifest_path=str(inputs.get("safety_manifest") or ""),
        account_replay_receipt_path=str(inputs.get("account_replay_receipt") or ""),
        venue_accounting_receipt_path=str(inputs.get("venue_accounting_receipt") or ""),
        freeze_manifest_path=str(inputs.get("freeze_manifest") or ""),
        effective_runtime_config_bundle_file=str(inputs.get("effective_runtime_config_bundle") or ""),
        expected_account_id=str(value.get("expected_account_id") or ""),
        t0_ns=int(window.get("t0_ns") or 0),
        t1_ns=int(window.get("t1_ns") or 0),
        created_ts_ns=int(value.get("created_ts_ns") or 0),
    )
    if canonical_json(rebuilt) != canonical_json(value):
        raise ValueError("natural tape sufficiency receipt does not reproduce from current sources")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the registered 120-hour natural demo tape and lifecycle floors."
    )
    parser.add_argument("--long-event-tape", required=True)
    parser.add_argument("--long-outcome-tape", required=True)
    parser.add_argument("--continuous-event-tape", required=True)
    parser.add_argument("--continuous-outcome-tape", required=True)
    parser.add_argument("--target-capture", required=True)
    parser.add_argument("--demo-account-root", required=True)
    parser.add_argument("--safety-target-capture", required=True)
    parser.add_argument("--safety-manifest", required=True)
    parser.add_argument("--account-replay-receipt", required=True)
    parser.add_argument("--venue-accounting-receipt", required=True)
    parser.add_argument("--freeze-manifest", required=True)
    parser.add_argument("--effective-runtime-config-bundle", required=True)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--t0-ns", required=True, type=int)
    parser.add_argument("--t1-ns", required=True, type=int)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = build_natural_tape_sufficiency_receipt(
            long_event_tape=args.long_event_tape,
            long_outcome_tape=args.long_outcome_tape,
            continuous_event_tape=args.continuous_event_tape,
            continuous_outcome_tape=args.continuous_outcome_tape,
            target_capture_path=args.target_capture,
            demo_account_root=args.demo_account_root,
            safety_target_capture_path=args.safety_target_capture,
            safety_manifest_path=args.safety_manifest,
            account_replay_receipt_path=args.account_replay_receipt,
            venue_accounting_receipt_path=args.venue_accounting_receipt,
            freeze_manifest_path=args.freeze_manifest,
            effective_runtime_config_bundle_file=args.effective_runtime_config_bundle,
            expected_account_id=args.expected_account_id,
            t0_ns=args.t0_ns,
            t1_ns=args.t1_ns,
        )
        receipt = write_natural_tape_sufficiency_receipt(args.output, payload)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"natural tape sufficiency failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "receipt": str(receipt.path),
                "status": receipt.payload["status"],
                "sufficiency_gate_passed": receipt.payload["sufficiency_gate_passed"],
                "artifact_sha256": receipt.payload["artifact_sha256"],
                "execution_authorization": "not_granted",
            },
            sort_keys=True,
        )
    )
    return 0 if receipt.payload["sufficiency_gate_passed"] else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
