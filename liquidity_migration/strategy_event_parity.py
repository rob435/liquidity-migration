"""Machine-checked historical/paper/demo strategy-event tape parity.

This module is deliberately stricter than comparing the terminal tape hashes.
The three environments have different raw source labels, so callers must name
every allowed raw-to-canonical source mapping.  Apart from that declared
environment normalization, scheduling inputs and decision identities are
compared exactly. Arrival/ingest timestamps remain bound in each raw source
tape but are telemetry, not part of normalized scheduling parity.

Each event must bind the immutable replay input bytes through
``payload.replay_input_sha256``. Decisions are recorded after the callback in a
separate hash-chained companion tape keyed to the durable raw event id; an
explicit empty list is a valid no-decision outcome. That makes an absent
input/decision observation a failed evidence boundary instead of an
accidentally green comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json, json_safe
from .strategy_event_clock import StrategyEvent, load_strategy_event_tape_bytes
from .strategy_event_outcome import load_strategy_event_decision_tape_bytes
from .strategy_target_replay import (
    REPLAY_MANIFEST_SCHEMA_VERSION,
    load_offline_target_scheduling_replay_manifest,
)


SCHEMA_VERSION = 3
RECEIPT_KIND = "strategy_event_replay_parity_receipt"
EVIDENCE_SCOPE = "deterministic_strategy_event_replay_parity"
ENVIRONMENTS = ("historical", "paper", "demo")
ENVIRONMENT_PAYLOAD_FIELD = "execution_environment"
ENVIRONMENT_REPLACEMENT = "<execution-environment>"
REPLAY_INPUT_HASH_FIELD = "replay_input_sha256"
DECISION_KEYS_FIELD = "decision_keys"
_NORMALIZED_GENESIS_HASH = hashlib.sha256(b"liquidity-migration-normalized-strategy-event-tape-v1").hexdigest()
_NORMALIZED_DECISION_GENESIS_HASH = hashlib.sha256(
    b"liquidity-migration-normalized-strategy-decision-tape-v1"
).hexdigest()

LIMITATIONS = (
    "compares_only_the_supplied_normalized_scheduling_and_decision_tapes",
    "replay_input_hash_binding_does_not_authenticate_market_data_provenance",
    "does_not_prove_strategy_callback_or_configuration_identity_outside_the_tape_payload",
    "does_not_prove_account_kernel_order_fill_fee_pnl_or_funding_parity",
    "does_not_establish_alpha_deployment_readiness_or_trading_authorization",
    "deployment_validity_requires_a_source_reopened_demo_target_replay_manifest",
)


@dataclass(frozen=True, slots=True)
class _NormalizedEvent:
    full_record: Mapping[str, Any]
    event_id: str
    phase: int


def _lower_sha256(value: Any, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _positive_int(value: Any, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _strict_regular_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    try:
        metadata = candidate.stat()
    except OSError as exc:
        raise ValueError(f"cannot stat {label} {candidate}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file: {candidate}")
    return candidate.resolve(strict=True)


def _read_private_receipt(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> tuple[Path, bytes]:
    if snapshot is None:
        receipt_path = _strict_regular_file(path, label="strategy-event parity receipt")
        snapshot = read_stable_file(
            receipt_path,
            label="strategy-event parity receipt",
            require_mode=0o600,
            require_owner=True,
            require_single_link=False,
        )
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("strategy-event parity receipt snapshot path differs")
    if snapshot.mode != 0o600 or snapshot.uid != os.geteuid() or snapshot.nlink != 1:
        raise ValueError(
            "strategy-event parity receipt must be mode 0600, singly linked, and owned by this user"
        )
    return snapshot.path, snapshot.data


def _snapshot_identity(snapshot: StableFileSnapshot) -> dict[str, Any]:
    return {
        "path": str(snapshot.path),
        "size_bytes": snapshot.size,
        "sha256": snapshot.sha256,
    }


def _file_snapshot(path: Path, *, reject_empty: bool, label: str) -> StableFileSnapshot:
    return read_stable_file(
        path,
        label=label,
        reject_empty=reject_empty,
        require_single_link=True,
    )


def _file_identity(path: Path, *, reject_empty: bool) -> dict[str, Any]:
    return _snapshot_identity(
        _file_snapshot(path, reject_empty=reject_empty, label="source file")
    )


def _load_bound_replay_manifest(
    path: str | Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    resolved = _strict_regular_file(path, label="offline target replay manifest")
    snapshot = _file_snapshot(
        resolved,
        reject_empty=True,
        label="offline target replay manifest",
    )
    manifest = load_offline_target_scheduling_replay_manifest(
        snapshot.path,
        snapshot=snapshot,
    )
    source_capture = manifest.get("source_capture")
    if not isinstance(source_capture, Mapping) or source_capture.get("source_environment") != "demo":
        raise ValueError("deployment-valid event parity requires a demo source target capture")
    identity = {
        **_snapshot_identity(snapshot),
        "schema_version": int(manifest["schema_version"]),
        "artifact_sha256": _lower_sha256(
            manifest.get("artifact_sha256"), label="offline target replay manifest artifact"
        ),
        "created_ts_ns": int(manifest["created_ts_ns"]),
    }
    return resolved, manifest, identity


def _manifest_source_subset(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": value.get("path"),
        "size_bytes": value.get("size_bytes"),
        "sha256": value.get("sha256"),
    }


def _normalized_payload(value: Any, *, environment: str) -> Any:
    """Normalize only explicit execution-environment fields.

    Source labels are normalized separately from an operator-supplied exact
    mapping.  No free-form string replacement is permitted because that could
    erase a real strategy-input difference.
    """

    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = str(raw_key)
            if key == ENVIRONMENT_PAYLOAD_FIELD:
                if item != environment:
                    raise ValueError(f"{ENVIRONMENT_PAYLOAD_FIELD} is {item!r}, expected {environment!r}")
                output[key] = ENVIRONMENT_REPLACEMENT
            else:
                output[key] = _normalized_payload(item, environment=environment)
        return output
    if isinstance(value, (list, tuple)):
        return [_normalized_payload(item, environment=environment) for item in value]
    return json_safe(value)


def _normalize_event(
    event: StrategyEvent,
    *,
    environment: str,
    index: int,
    source_map: Mapping[str, str],
    replay_input_sha256: str,
) -> _NormalizedEvent:
    if event.source not in source_map:
        raise ValueError(f"{environment} strategy event {index} source {event.source!r} has no explicit normalization")
    payload = dict(event.payload)
    if DECISION_KEYS_FIELD in payload:
        raise ValueError(
            f"{environment} strategy event {index} embeds post-callback decision_keys; "
            "use the companion strategy decision tape"
        )
    observed_input = _lower_sha256(
        payload.get(REPLAY_INPUT_HASH_FIELD),
        label=f"{environment} strategy event {index} replay input hash",
    )
    if observed_input != replay_input_sha256:
        raise ValueError(f"{environment} strategy event {index} replay input hash does not match its bound artifact")
    normalized_payload = cast(Mapping[str, Any], _normalized_payload(payload, environment=environment))
    normalized_source = source_map[event.source]
    identity_material = {
        "event_ts_ns": event.event_ts_ns,
        "kind": event.kind,
        "payload": normalized_payload,
        "source": normalized_source,
        "source_sequence": event.source_sequence,
    }
    normalized_event_id = "strategy-event-" + hashlib.sha256(canonical_json(identity_material)).hexdigest()
    phase = int(event.order_key[1])
    full_record = {
        "event_id": normalized_event_id,
        "event_ts_ns": event.event_ts_ns,
        "source": normalized_source,
        "source_sequence": event.source_sequence,
        "phase": phase,
        "kind": event.kind,
        "payload": normalized_payload,
    }
    return _NormalizedEvent(
        full_record=full_record,
        event_id=normalized_event_id,
        phase=phase,
    )


def _normalized_tape_hash(events: Sequence[_NormalizedEvent]) -> str:
    tape_hash = _NORMALIZED_GENESIS_HASH
    for event in events:
        tape_hash = hashlib.sha256(
            tape_hash.encode("ascii") + canonical_json({"event": dict(event.full_record)})
        ).hexdigest()
    return tape_hash


def _normalized_decision_tape_hash(events: Sequence[_NormalizedEvent], decisions: Sequence[tuple[str, ...]]) -> str:
    tape_hash = _NORMALIZED_DECISION_GENESIS_HASH
    for event, decision_keys in zip(events, decisions, strict=True):
        tape_hash = hashlib.sha256(
            tape_hash.encode("ascii")
            + canonical_json(
                {
                    "outcome": {
                        "event_id": event.event_id,
                        "decision_keys": list(decision_keys),
                    }
                }
            )
        ).hexdigest()
    return tape_hash


def _sequence_hash(label: str, values: Sequence[Any]) -> str:
    return hashlib.sha256(canonical_json({label: list(values)})).hexdigest()


def _first_difference(left: Sequence[Any], right: Sequence[Any]) -> int | None:
    for index, (left_item, right_item) in enumerate(zip(left, right, strict=False)):
        if left_item != right_item:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def _validate_inputs(
    event_tapes: Mapping[str, str | Path],
    decision_tapes: Mapping[str, str | Path],
    replay_inputs: Mapping[str, str | Path],
    source_normalizations: Mapping[str, Mapping[str, str]],
) -> None:
    required = set(ENVIRONMENTS)
    for label, values in (
        ("event tapes", event_tapes),
        ("decision tapes", decision_tapes),
        ("replay inputs", replay_inputs),
        ("source normalizations", source_normalizations),
    ):
        if set(values) != required:
            raise ValueError(f"{label} must be exactly historical, paper, and demo")
    canonical_sets: list[set[str]] = []
    for environment in ENVIRONMENTS:
        mapping = source_normalizations[environment]
        if not mapping:
            raise ValueError(f"{environment} source normalization is empty")
        if any(not str(raw).strip() or not str(normalized).strip() for raw, normalized in mapping.items()):
            raise ValueError(f"{environment} source normalization contains an empty label")
        if len(set(mapping.values())) != len(mapping):
            raise ValueError(f"{environment} source normalization must not collapse distinct raw sources")
        canonical_sets.append(set(mapping.values()))
    if any(values != canonical_sets[0] for values in canonical_sets[1:]):
        raise ValueError("source normalizations must expose the same canonical source set in every environment")


def build_strategy_event_parity_receipt(
    event_tapes: Mapping[str, str | Path],
    *,
    decision_tapes: Mapping[str, str | Path],
    replay_inputs: Mapping[str, str | Path],
    source_normalizations: Mapping[str, Mapping[str, str]],
    replay_manifest: str | Path | None = None,
    created_ts_ns: int | None = None,
) -> dict[str, Any]:
    """Build one deterministic receipt from immutable local source files."""

    _validate_inputs(event_tapes, decision_tapes, replay_inputs, source_normalizations)
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= 0:
        raise ValueError("strategy-event parity creation time must be positive")
    bound_manifest_path: Path | None = None
    bound_manifest: dict[str, Any] | None = None
    bound_manifest_identity: dict[str, Any] | None = None
    if replay_manifest is not None:
        (
            bound_manifest_path,
            bound_manifest,
            bound_manifest_identity,
        ) = _load_bound_replay_manifest(replay_manifest)
        if created < int(bound_manifest["created_ts_ns"]):
            raise ValueError("strategy-event parity predates its bound target replay manifest")
        raw_manifest_environments = bound_manifest.get("environments")
        if not isinstance(raw_manifest_environments, Mapping):
            raise ValueError("bound target replay manifest lacks environments")
        requested_sources = {
            "event_tape": event_tapes,
            "decision_tape": decision_tapes,
            "replay_input": replay_inputs,
        }
        for environment in ENVIRONMENTS:
            raw_environment = raw_manifest_environments.get(environment)
            if not isinstance(raw_environment, Mapping):
                raise ValueError(f"bound target replay manifest lacks {environment} sources")
            for role, requested in requested_sources.items():
                raw_identity = raw_environment.get(role)
                if not isinstance(raw_identity, Mapping):
                    raise ValueError(
                        f"bound target replay manifest lacks {environment} {role} identity"
                    )
                requested_path = _strict_regular_file(
                    requested[environment],
                    label=f"{environment} requested {role}",
                )
                if str(requested_path) != raw_identity.get("path"):
                    raise ValueError(
                        f"{environment} {role} does not match the bound target replay manifest"
                    )
    input_snapshots: dict[str, StableFileSnapshot] = {}
    input_identities: dict[str, dict[str, Any]] = {}
    for environment in ENVIRONMENTS:
        path = _strict_regular_file(replay_inputs[environment], label=f"{environment} replay input")
        snapshot = _file_snapshot(
            path,
            reject_empty=True,
            label=f"{environment} replay input",
        )
        input_snapshots[environment] = snapshot
        input_identities[environment] = _snapshot_identity(snapshot)
    input_hashes = {str(identity["sha256"]) for identity in input_identities.values()}
    if len(input_hashes) != 1:
        raise ValueError("historical, paper, and demo replay input artifacts differ")
    replay_input_sha256 = next(iter(input_hashes))

    normalized_by_environment: dict[str, tuple[_NormalizedEvent, ...]] = {}
    decisions_by_environment: dict[str, tuple[tuple[str, ...], ...]] = {}
    source_receipts: dict[str, dict[str, Any]] = {}
    event_tape_snapshots: dict[str, StableFileSnapshot] = {}
    decision_tape_snapshots: dict[str, StableFileSnapshot] = {}
    for environment in ENVIRONMENTS:
        tape_path = _strict_regular_file(event_tapes[environment], label=f"{environment} strategy event tape")
        tape_snapshot = _file_snapshot(
            tape_path,
            reject_empty=True,
            label=f"{environment} strategy event tape",
        )
        events, raw_chain_hash = load_strategy_event_tape_bytes(tape_snapshot.data)
        event_tape_snapshots[environment] = tape_snapshot
        if not events:
            raise ValueError(f"{environment} strategy event tape has no events")
        last_sequence_by_source: dict[str, int] = {}
        for index, event in enumerate(events):
            prior_sequence = last_sequence_by_source.get(event.source)
            if prior_sequence is not None and event.source_sequence <= prior_sequence:
                raise ValueError(
                    f"{environment} strategy event {index} has a duplicate or backward "
                    f"source sequence for {event.source!r}"
                )
            last_sequence_by_source[event.source] = event.source_sequence
        mapping = source_normalizations[environment]
        normalized = tuple(
            _normalize_event(
                event,
                environment=environment,
                index=index,
                source_map=mapping,
                replay_input_sha256=replay_input_sha256,
            )
            for index, event in enumerate(events)
        )
        used_sources = {event.source for event in events}
        unused_sources = sorted(set(mapping) - used_sources)
        if unused_sources:
            raise ValueError(
                f"{environment} source normalization contains unused raw sources: {', '.join(unused_sources)}"
            )
        normalized_order_keys = [
            (
                int(event.full_record["event_ts_ns"]),
                event.phase,
                str(event.full_record["source"]),
                int(event.full_record["source_sequence"]),
                event.event_id,
            )
            for event in normalized
        ]
        if any(right < left for left, right in zip(normalized_order_keys, normalized_order_keys[1:], strict=False)):
            raise ValueError(f"{environment} strategy event tape moves backward after source normalization")
        normalized_ids = [event.event_id for event in normalized]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError(f"{environment} strategy event tape has duplicate identities after normalization")
        decision_path = _strict_regular_file(
            decision_tapes[environment],
            label=f"{environment} strategy decision tape",
        )
        decision_snapshot = _file_snapshot(
            decision_path,
            reject_empty=True,
            label=f"{environment} strategy decision tape",
        )
        outcomes, raw_decision_chain_hash = load_strategy_event_decision_tape_bytes(
            decision_snapshot.data
        )
        decision_tape_snapshots[environment] = decision_snapshot
        if not outcomes:
            raise ValueError(f"{environment} strategy decision tape has no outcomes")
        if len(outcomes) != len(events):
            raise ValueError(f"{environment} strategy decision tape count does not match its event tape")
        for index, (event, outcome) in enumerate(zip(events, outcomes, strict=True)):
            if outcome.event_id != event.event_id:
                raise ValueError(f"{environment} strategy decision tape does not align with event index {index}")
        decisions = tuple(outcome.decision_keys for outcome in outcomes)
        normalized_by_environment[environment] = normalized
        decisions_by_environment[environment] = decisions
        source_receipts[environment] = {
            "event_tape": {
                **_snapshot_identity(tape_snapshot),
                "event_count": len(events),
                "raw_chain_hash": raw_chain_hash,
                "normalized_chain_hash": _normalized_tape_hash(normalized),
                "normalized_event_ids_sha256": _sequence_hash("event_ids", normalized_ids),
            },
            "decision_tape": {
                **_snapshot_identity(decision_snapshot),
                "outcome_count": len(outcomes),
                "raw_chain_hash": raw_decision_chain_hash,
                "normalized_chain_hash": _normalized_decision_tape_hash(normalized, decisions),
                "decision_keys_sha256": _sequence_hash("decision_keys", [list(keys) for keys in decisions]),
            },
            "replay_input": input_identities[environment],
        }

    if bound_manifest is not None:
        manifest_environments = bound_manifest.get("environments")
        if not isinstance(manifest_environments, Mapping):
            raise ValueError("bound target replay manifest lacks environments")
        for environment in ENVIRONMENTS:
            manifest_source = manifest_environments.get(environment)
            if not isinstance(manifest_source, Mapping):
                raise ValueError(f"bound target replay manifest lacks {environment} sources")
            for receipt_role, manifest_role in (
                ("event_tape", "event_tape"),
                ("decision_tape", "decision_tape"),
                ("replay_input", "replay_input"),
            ):
                raw_manifest_identity = manifest_source.get(manifest_role)
                if not isinstance(raw_manifest_identity, Mapping):
                    raise ValueError(
                        f"bound target replay manifest lacks {environment} {manifest_role} identity"
                    )
                compared_identity = cast(
                    Mapping[str, Any], source_receipts[environment][receipt_role]
                )
                if _manifest_source_subset(raw_manifest_identity) != _manifest_source_subset(
                    compared_identity
                ):
                    raise ValueError(
                        f"{environment} {receipt_role} does not match the bound target replay manifest"
                    )

    baseline_name = ENVIRONMENTS[0]
    baseline = normalized_by_environment[baseline_name]
    baseline_records = [dict(event.full_record) for event in baseline]
    baseline_ids = [event.event_id for event in baseline]
    baseline_kinds = [(event.phase, str(event.full_record["kind"])) for event in baseline]
    baseline_sources = [str(event.full_record["source"]) for event in baseline]
    baseline_sequences = [int(event.full_record["source_sequence"]) for event in baseline]
    baseline_timestamps = [int(event.full_record["event_ts_ns"]) for event in baseline]
    baseline_payloads = [event.full_record["payload"] for event in baseline]
    baseline_decisions = decisions_by_environment[baseline_name]

    checks = {
        "event_counts_identical": True,
        "normalized_event_order_identical": True,
        "canonical_event_identities_identical": True,
        "phases_and_kinds_identical": True,
        "sources_identical_after_normalization": True,
        "source_sequences_identical": True,
        "event_timestamps_identical": True,
        "payloads_identical": True,
        "decision_keys_identical": True,
        "normalized_event_chains_identical": True,
        "normalized_decision_chains_identical": True,
        "replay_input_bytes_identical": True,
        "all_tape_chains_verified": True,
    }
    mismatches: list[str] = []
    for environment in ENVIRONMENTS[1:]:
        observed = normalized_by_environment[environment]
        observed_records = [dict(event.full_record) for event in observed]
        comparisons: tuple[tuple[str, Sequence[Any], Sequence[Any], str], ...] = (
            (
                "normalized_event_order_identical",
                baseline_records,
                observed_records,
                "normalized event records/order",
            ),
            (
                "canonical_event_identities_identical",
                baseline_ids,
                [event.event_id for event in observed],
                "canonical event identities",
            ),
            (
                "phases_and_kinds_identical",
                baseline_kinds,
                [(event.phase, str(event.full_record["kind"])) for event in observed],
                "phases/kinds",
            ),
            (
                "sources_identical_after_normalization",
                baseline_sources,
                [str(event.full_record["source"]) for event in observed],
                "normalized sources",
            ),
            (
                "source_sequences_identical",
                baseline_sequences,
                [int(event.full_record["source_sequence"]) for event in observed],
                "source sequences",
            ),
            (
                "event_timestamps_identical",
                baseline_timestamps,
                [int(event.full_record["event_ts_ns"]) for event in observed],
                "event timestamps",
            ),
            (
                "payloads_identical",
                baseline_payloads,
                [event.full_record["payload"] for event in observed],
                "normalized payloads",
            ),
            (
                "decision_keys_identical",
                baseline_decisions,
                decisions_by_environment[environment],
                "decision keys",
            ),
        )
        if len(observed) != len(baseline):
            checks["event_counts_identical"] = False
            mismatches.append(f"event counts differ: {baseline_name}={len(baseline)} vs {environment}={len(observed)}")
        for check, expected, actual, label in comparisons:
            difference = _first_difference(expected, actual)
            if difference is not None:
                checks[check] = False
                mismatches.append(f"{label} differ: {baseline_name} vs {environment} at index {difference}")
        for check, tape_name, label in (
            (
                "normalized_event_chains_identical",
                "event_tape",
                "normalized event chains",
            ),
            (
                "normalized_decision_chains_identical",
                "decision_tape",
                "normalized decision chains",
            ),
        ):
            baseline_chain = source_receipts[baseline_name][tape_name]["normalized_chain_hash"]
            observed_chain = source_receipts[environment][tape_name]["normalized_chain_hash"]
            if observed_chain != baseline_chain:
                checks[check] = False
                mismatches.append(f"{label} differ: {baseline_name} vs {environment}")

    passed = all(checks.values())
    report = {
        "passed": passed,
        "compared_environments": list(ENVIRONMENTS),
        **checks,
        "mismatches": mismatches,
    }
    normalizations = {
        "source_maps": {
            environment: dict(sorted(source_normalizations[environment].items())) for environment in ENVIRONMENTS
        },
        "payload_environment_field": ENVIRONMENT_PAYLOAD_FIELD,
        "payload_environment_replacement": ENVIRONMENT_REPLACEMENT,
        "all_other_payload_fields": "exact",
        "ingest_ts_ns": "bound_in_raw_tape_but_excluded_from_normalized_parity",
    }
    replay_provenance: dict[str, Any]
    if bound_manifest is None or bound_manifest_identity is None:
        replay_provenance = {
            "deployment_valid": False,
            "replay_manifest": None,
            "canonical_source_capture": None,
        }
    else:
        raw_source_capture = bound_manifest.get("source_capture")
        if not isinstance(raw_source_capture, Mapping):
            raise ValueError("bound target replay manifest lacks its canonical source capture")
        replay_provenance = {
            "deployment_valid": True,
            "replay_manifest": bound_manifest_identity,
            "canonical_source_capture": dict(raw_source_capture),
        }
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "created_ts_ns": created,
        "evidence_scope": EVIDENCE_SCOPE,
        "strategy_event_replay_gate_passed": passed,
        "normalization": normalizations,
        "comparison_policy": {
            "discrete_fields": "exact",
            "numeric_tolerance_applied": False,
            "numeric_tolerance_reason": (
                "event timestamps, phases, sequences, identities, input hashes, and decision keys "
                "are discrete replay evidence; downstream target quantities remain in the "
                "separate account-kernel parity gate with its declared tolerance"
            ),
            "ingest_ts_ns": (
                "arrival telemetry bound by each raw event-tape hash chain; not a "
                "StrategyEvent order-key field or normalized replay input"
            ),
        },
        "replay_provenance": replay_provenance,
        "sources": source_receipts,
        "report": report,
        "limitations": list(LIMITATIONS),
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    if bound_manifest_path is not None and bound_manifest is not None:
        _, reloaded_manifest, reloaded_identity = _load_bound_replay_manifest(
            bound_manifest_path
        )
        if (
            canonical_json(reloaded_manifest) != canonical_json(bound_manifest)
            or reloaded_identity != bound_manifest_identity
        ):
            raise ValueError("bound target replay manifest changed during event parity")
    for environment in ENVIRONMENTS:
        for role, initial in (
            ("event tape", event_tape_snapshots[environment]),
            ("decision tape", decision_tape_snapshots[environment]),
            ("replay input", input_snapshots[environment]),
        ):
            final = _file_snapshot(
                initial.path,
                reject_empty=True,
                label=f"{environment} {role}",
            )
            if final != initial:
                raise ValueError(f"{environment} {role} changed during event parity")
    return receipt


def _source_maps_from_receipt(payload: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    normalization = payload.get("normalization")
    if not isinstance(normalization, Mapping):
        raise ValueError("strategy-event parity receipt lacks normalization")
    expected_normalization_fields = {
        "source_maps",
        "payload_environment_field",
        "payload_environment_replacement",
        "all_other_payload_fields",
        "ingest_ts_ns",
    }
    if set(normalization) != expected_normalization_fields:
        raise ValueError("strategy-event parity normalization has invalid fields")
    if (
        normalization.get("payload_environment_field") != ENVIRONMENT_PAYLOAD_FIELD
        or normalization.get("payload_environment_replacement") != ENVIRONMENT_REPLACEMENT
        or normalization.get("all_other_payload_fields") != "exact"
        or normalization.get("ingest_ts_ns") != "bound_in_raw_tape_but_excluded_from_normalized_parity"
    ):
        raise ValueError("strategy-event parity normalization policy changed")
    raw_maps = normalization.get("source_maps")
    if not isinstance(raw_maps, Mapping):
        raise ValueError("strategy-event parity receipt lacks source maps")
    output: dict[str, dict[str, str]] = {}
    for environment in ENVIRONMENTS:
        raw = raw_maps.get(environment)
        if not isinstance(raw, Mapping):
            raise ValueError(f"strategy-event parity receipt lacks {environment} source map")
        output[environment] = {str(key): str(value) for key, value in raw.items()}
    if set(raw_maps) != set(ENVIRONMENTS):
        raise ValueError("strategy-event parity receipt has an invalid environment set")
    return output


def _replay_manifest_path_from_provenance(payload: Mapping[str, Any]) -> Path | None:
    provenance = payload.get("replay_provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "deployment_valid",
        "replay_manifest",
        "canonical_source_capture",
    }:
        raise ValueError("strategy-event parity replay provenance has invalid fields")
    deployment_valid = provenance.get("deployment_valid")
    if type(deployment_valid) is not bool:
        raise ValueError("strategy-event parity replay provenance has an invalid validity flag")
    raw_manifest_identity = provenance.get("replay_manifest")
    raw_source_capture = provenance.get("canonical_source_capture")
    if not deployment_valid:
        if raw_manifest_identity is not None or raw_source_capture is not None:
            raise ValueError("exploratory strategy-event parity cannot claim replay provenance")
        return None
    expected_manifest_fields = {
        "path",
        "size_bytes",
        "sha256",
        "schema_version",
        "artifact_sha256",
        "created_ts_ns",
    }
    if (
        not isinstance(raw_manifest_identity, Mapping)
        or set(raw_manifest_identity) != expected_manifest_fields
        or not isinstance(raw_source_capture, Mapping)
    ):
        raise ValueError("deployment-valid strategy-event parity lacks replay provenance")
    manifest_path = _strict_regular_file(
        str(raw_manifest_identity.get("path") or ""),
        label="bound offline target replay manifest",
    )
    _positive_int(raw_manifest_identity.get("size_bytes"), label="replay manifest size")
    _positive_int(
        raw_manifest_identity.get("created_ts_ns"), label="replay manifest creation time"
    )
    if raw_manifest_identity.get("schema_version") != REPLAY_MANIFEST_SCHEMA_VERSION:
        raise ValueError("bound offline target replay manifest schema changed")
    _lower_sha256(raw_manifest_identity.get("sha256"), label="replay manifest file hash")
    _lower_sha256(
        raw_manifest_identity.get("artifact_sha256"), label="replay manifest artifact hash"
    )
    _, manifest, observed_identity = _load_bound_replay_manifest(manifest_path)
    if observed_identity != dict(raw_manifest_identity):
        raise ValueError("bound offline target replay manifest identity changed")
    source_capture = manifest.get("source_capture")
    if not isinstance(source_capture, Mapping) or canonical_json(source_capture) != canonical_json(
        raw_source_capture
    ):
        raise ValueError("canonical target replay source capture identity changed")
    return manifest_path


def verify_strategy_event_parity_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the self-hash and reproduce every claim from bound source files."""

    payload = dict(receipt)
    expected_fields = {
        "schema_version",
        "kind",
        "created_ts_ns",
        "evidence_scope",
        "strategy_event_replay_gate_passed",
        "normalization",
        "comparison_policy",
        "replay_provenance",
        "sources",
        "report",
        "limitations",
        "artifact_sha256",
    }
    if set(payload) != expected_fields:
        raise ValueError("strategy-event parity receipt has unexpected or missing fields")
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError("unsupported strategy-event parity receipt schema")
    if payload.get("kind") != RECEIPT_KIND or payload.get("evidence_scope") != EVIDENCE_SCOPE:
        raise ValueError("strategy-event parity receipt has the wrong kind or scope")
    if int(payload.get("created_ts_ns") or 0) <= 0:
        raise ValueError("strategy-event parity receipt has an invalid creation time")
    if payload.get("limitations") != list(LIMITATIONS):
        raise ValueError("strategy-event parity receipt limitations changed")
    policy = payload.get("comparison_policy")
    if not isinstance(policy, Mapping) or set(policy) != {
        "discrete_fields",
        "numeric_tolerance_applied",
        "numeric_tolerance_reason",
        "ingest_ts_ns",
    }:
        raise ValueError("strategy-event parity receipt has an invalid comparison policy")
    if policy.get("discrete_fields") != "exact" or policy.get("numeric_tolerance_applied") is not False:
        raise ValueError("strategy-event parity receipt weakened exact identity comparison")
    report = payload.get("report")
    sources = payload.get("sources")
    if not isinstance(report, Mapping) or not isinstance(sources, Mapping):
        raise ValueError("strategy-event parity receipt lacks report or sources")
    if set(sources) != set(ENVIRONMENTS):
        raise ValueError("strategy-event parity receipt requires historical, paper, and demo")
    passed = payload.get("strategy_event_replay_gate_passed")
    if not isinstance(passed, bool) or report.get("passed") is not passed:
        raise ValueError("strategy-event parity aggregate gate is inconsistent")
    observed_hash = str(payload.get("artifact_sha256") or "")
    expected_hash = hashlib.sha256(canonical_json({**payload, "artifact_sha256": ""})).hexdigest()
    if observed_hash != expected_hash:
        raise ValueError("strategy-event parity receipt hash mismatch")
    replay_manifest_path = _replay_manifest_path_from_provenance(payload)

    event_tapes: dict[str, str] = {}
    decision_tapes: dict[str, str] = {}
    replay_inputs: dict[str, str] = {}
    for environment in ENVIRONMENTS:
        source = sources[environment]
        if not isinstance(source, Mapping) or set(source) != {
            "event_tape",
            "decision_tape",
            "replay_input",
        }:
            raise ValueError(f"strategy-event parity source {environment!r} has invalid fields")
        event_tape = source.get("event_tape")
        decision_tape = source.get("decision_tape")
        replay_input = source.get("replay_input")
        if (
            not isinstance(event_tape, Mapping)
            or not isinstance(decision_tape, Mapping)
            or not isinstance(replay_input, Mapping)
        ):
            raise ValueError(f"strategy-event parity source {environment!r} lacks file identities")
        if (
            set(event_tape)
            != {
                "path",
                "size_bytes",
                "sha256",
                "event_count",
                "raw_chain_hash",
                "normalized_chain_hash",
                "normalized_event_ids_sha256",
            }
            or set(decision_tape)
            != {
                "path",
                "size_bytes",
                "sha256",
                "outcome_count",
                "raw_chain_hash",
                "normalized_chain_hash",
                "decision_keys_sha256",
            }
            or set(replay_input) != {"path", "size_bytes", "sha256"}
        ):
            raise ValueError(f"strategy-event parity source {environment!r} identity fields changed")
        for label, value in (
            ("event tape", event_tape.get("sha256")),
            ("raw chain", event_tape.get("raw_chain_hash")),
            ("normalized chain", event_tape.get("normalized_chain_hash")),
            ("normalized ids", event_tape.get("normalized_event_ids_sha256")),
            ("decision tape", decision_tape.get("sha256")),
            ("raw decision chain", decision_tape.get("raw_chain_hash")),
            (
                "normalized decision chain",
                decision_tape.get("normalized_chain_hash"),
            ),
            ("decision keys", decision_tape.get("decision_keys_sha256")),
            ("replay input", replay_input.get("sha256")),
        ):
            _lower_sha256(value, label=f"{environment} {label} hash")
        if int(event_tape.get("event_count") or 0) <= 0:
            raise ValueError(f"strategy-event parity source {environment!r} is empty")
        if int(decision_tape.get("outcome_count") or 0) != int(event_tape.get("event_count") or 0):
            raise ValueError(f"strategy-event parity source {environment!r} has an invalid outcome count")
        event_tapes[environment] = str(event_tape.get("path") or "")
        decision_tapes[environment] = str(decision_tape.get("path") or "")
        replay_inputs[environment] = str(replay_input.get("path") or "")

    rebuilt = build_strategy_event_parity_receipt(
        event_tapes,
        decision_tapes=decision_tapes,
        replay_inputs=replay_inputs,
        source_normalizations=_source_maps_from_receipt(payload),
        replay_manifest=replay_manifest_path,
        created_ts_ns=int(payload.get("created_ts_ns") or 0),
    )
    if canonical_json(rebuilt) != canonical_json(payload):
        raise ValueError("strategy-event parity receipt does not reproduce from bound sources")
    return payload


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> Path:
    if not path.is_absolute():
        raise ValueError("strategy-event parity receipt output must be an absolute path")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
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
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("strategy-event parity receipt write made no progress")
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
    return path


def write_strategy_event_parity_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    payload = verify_strategy_event_parity_receipt(receipt)
    return _atomic_write(Path(path).expanduser(), payload)


def load_strategy_event_parity_receipt(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    _, receipt_bytes = _read_private_receipt(path, snapshot=snapshot)
    value = json.loads(receipt_bytes)
    if not isinstance(value, Mapping):
        raise ValueError("strategy-event parity receipt must contain an object")
    return verify_strategy_event_parity_receipt(value)


def _named_path(raw: str) -> tuple[str, Path]:
    name, separator, path = raw.partition("=")
    if not separator or not name.strip() or not path.strip():
        raise argparse.ArgumentTypeError("value must be ENVIRONMENT=PATH")
    return name.strip(), Path(path.strip()).expanduser()


def _source_map(raw: str) -> tuple[str, str, str]:
    environment, separator, remainder = raw.partition("=")
    source, second_separator, normalized = remainder.partition("=")
    if not separator or not second_separator or not environment.strip() or not source.strip() or not normalized.strip():
        raise argparse.ArgumentTypeError("source map must be ENVIRONMENT=RAW_SOURCE=CANONICAL_SOURCE")
    return environment.strip(), source.strip(), normalized.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline exact comparison of source-bound historical, paper, and demo strategy-event/replay tapes."
        )
    )
    parser.add_argument(
        "--environment",
        action="append",
        required=True,
        type=_named_path,
        metavar="NAME=EVENT_TAPE",
        help="Repeat exactly once for historical, paper, and demo.",
    )
    parser.add_argument(
        "--decision-tape",
        action="append",
        required=True,
        type=_named_path,
        metavar="NAME=DECISION_TAPE",
        help=("Repeat for the companion post-callback decision tape paired with each environment event tape."),
    )
    parser.add_argument(
        "--replay-input",
        action="append",
        required=True,
        type=_named_path,
        metavar="NAME=INPUT_ARTIFACT",
        help=(
            "Repeat for the exact input artifact consumed by each environment; all three files must be byte-identical."
        ),
    )
    parser.add_argument(
        "--source-map",
        action="append",
        required=True,
        type=_source_map,
        metavar="NAME=RAW_SOURCE=CANONICAL_SOURCE",
        help="Explicitly normalize every raw event source; repeat as needed.",
    )
    parser.add_argument(
        "--replay-manifest",
        type=Path,
        required=True,
        help=(
            "Schema-v2 offline target replay manifest; source-reopened and required for "
            "a deployment-valid receipt."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    event_tapes: dict[str, Path] = {}
    decision_tapes: dict[str, Path] = {}
    replay_inputs: dict[str, Path] = {}
    source_maps: dict[str, dict[str, str]] = {environment: {} for environment in ENVIRONMENTS}
    for name, path in args.environment:
        if name in event_tapes:
            parser.error(f"duplicate strategy-event environment: {name}")
        event_tapes[name] = path
    for name, path in args.decision_tape:
        if name in decision_tapes:
            parser.error(f"duplicate decision-tape environment: {name}")
        decision_tapes[name] = path
    for name, path in args.replay_input:
        if name in replay_inputs:
            parser.error(f"duplicate replay-input environment: {name}")
        replay_inputs[name] = path
    for environment, raw_source, normalized_source in args.source_map:
        if environment not in source_maps:
            parser.error(f"unknown source-map environment: {environment}")
        if raw_source in source_maps[environment]:
            parser.error(f"duplicate source-map raw source for {environment}: {raw_source}")
        source_maps[environment][raw_source] = normalized_source
    try:
        receipt = build_strategy_event_parity_receipt(
            event_tapes,
            decision_tapes=decision_tapes,
            replay_inputs=replay_inputs,
            source_normalizations=source_maps,
            replay_manifest=args.replay_manifest,
        )
        output = write_strategy_event_parity_receipt(args.output, receipt)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"strategy-event parity failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "strategy_event_replay_gate_passed": receipt["strategy_event_replay_gate_passed"],
                "artifact_sha256": receipt["artifact_sha256"],
                "mismatches": receipt["report"]["mismatches"],
            },
            sort_keys=True,
        )
    )
    return 0 if receipt["strategy_event_replay_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
