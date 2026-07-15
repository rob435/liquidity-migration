"""Replay frozen demo account requests through the production account kernel.

This adapter joins frozen post-publication strategy targets, the actual demo
account journal, raw public-market capture, a disjoint V7 training receipt, and
the separately registered post-T1 safety flatten.  A mandatory child manifest
binds those inputs before every selected natural request is reconstructed with
its exact demo market inputs and capital snapshot and run through two isolated
execution-twin roots.  It never opens an inbox, constructs a venue client, or
reads credentials.

The resulting claims are intentionally split:

* historical and paper are required to have byte/canonically identical modeled
  outcomes under the same kernel, inputs, rules, risk policy, and twin config;
* demo plan parity compares only the pre-execution account plan, because actual
  venue fills are observations rather than execution-twin outputs;
* scheduling parity and actual demo fill/P&L sufficiency remain separate gates.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .account_kernel import (
    GENESIS_HASH,
    AccountEvent,
    AccountEventType,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
    MarketInputRef,
    account_journal_path,
    account_transactions_path,
    read_account_journal,
    read_account_journal_bytes,
    target_batch_request_hash,
)
from .account_intent_client import account_target_request_id, component_target_key
from .account_service import (
    RequestedIntent,
    SleeveAdapterKind,
    prepare_account_request_intents,
)
from .account_execution_config import load_demo_rules_bytes, load_risk_policy_bytes
from .artifact_snapshot import StableFileSnapshot, read_stable_file, rename_noreplace
from .deterministic_serialization import canonical_json, json_safe
from .execution_adapters import BookLevel, L2BookSnapshot
from .execution_twin_calibration import (
    execution_twin_config_from_calibration,
    verify_calibration_receipt,
)
from .historical_account_replay import HistoricalAccountReplay, HistoricalReplayCycle
from .kernel_parity import compare_kernel_journals
from .market_capture import capture_record_id
from .natural_cutover_freeze_manifest import load_natural_cutover_freeze_manifest
from .natural_effective_config import (
    load_effective_runtime_config_bundle_binding,
    validate_effective_runtime_config_bundle_join,
)
from .strategy_target_replay import (
    CapturedTargetRequest,
    TargetSchedulingCaptureEvent,
    load_target_scheduling_capture_bytes,
)


ACCOUNT_REPLAY_SCHEMA_VERSION = 3
ACCOUNT_REPLAY_KIND = "captured_demo_account_kernel_replay"
ACCOUNT_REPLAY_RECEIPT_FILENAME = "captured_account_replay_receipt.json"
ACCOUNT_REPLAY_INPUT_MANIFEST_SCHEMA_VERSION = 2
ACCOUNT_REPLAY_INPUT_MANIFEST_KIND = "natural_account_replay_input_manifest_v2"
POST_WINDOW_SAFETY_MANIFEST_SCHEMA_VERSION = 1
POST_WINDOW_SAFETY_MANIFEST_KIND = "natural_account_post_window_safety_manifest_v1"
POST_WINDOW_SAFETY_NAMESPACE = "natural-safety-flatten"
POST_WINDOW_SAFETY_STRATEGY_PROFILE = "natural-account-safety-flatten-v1"
POST_WINDOW_SAFETY_REASON = "registered post-window natural safety flatten"
POST_WINDOW_SAFETY_SCOPE = "post_window_account_safety_only"
DEFAULT_MAX_MARKET_AGE_NS = 5_000_000_000
DEFAULT_MAX_SNAPSHOT_AGE_NS = 5_000_000_000
DEFAULT_KERNEL_ID_SEED = "account-kernel-v1"
DEFAULT_TWIN_ID_SEED = "captured-demo-account-replay-v1:execution"
REGISTERED_MAX_DECISION_AGE_NS = 250_000_000
REGISTERED_MAX_MARKET_AGE_NS = DEFAULT_MAX_MARKET_AGE_NS
REGISTERED_MAX_SNAPSHOT_AGE_NS = DEFAULT_MAX_SNAPSHOT_AGE_NS
REGISTERED_LATENCY_QUANTILE = "p50"
REGISTERED_SLIPPAGE_QUANTILE = "p50"
NATURAL_WINDOW_HOURS = 120
HOUR_NS = 3_600_000_000_000
NATURAL_MIN_FILLED_COMMANDS = 30
NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE = 10
NATURAL_MIN_FILLED_SYMBOLS = 3
NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE = 3
NATURAL_MIN_PNL_EVENTS = 10
_PLAN_EVENT_TYPES = frozenset(
    {
        AccountEventType.MARKET_INPUT_REF.value,
        AccountEventType.DECISION.value,
        AccountEventType.TARGET.value,
        AccountEventType.RISK_DECISION.value,
        AccountEventType.ORDER_COMMAND.value,
    }
)
_LIMITATIONS = (
    "target_capture_is_post_publication_provenance_not_signal_recomputation",
    "demo_plan_parity_excludes_ack_fill_status_close_pnl_and_venue_timing",
    "historical_and_paper_outputs_are_execution_twin_models_not_venue_fills",
    "raw_preview_and_background_contexts_are_bound_but_not_selected_as_market_inputs",
    "does_not_establish_actual_demo_fill_pnl_or_funding_sufficiency",
    "natural_lifecycle_sufficiency_requires_a_separate_machine_verifier",
    "venue_accounting_2_trade_1_closed_pnl_1_settlement_defaults_do_not_satisfy_natural_floors",
    "does_not_establish_alpha_validity_deployment_readiness_or_trading_authorization",
)


def _require_registered_replay_configuration(
    *,
    max_decision_age_ns: int,
    max_market_age_ns: int,
    max_snapshot_age_ns: int,
    latency_quantile: str,
    slippage_quantile: str,
    kernel_id_seed: str,
    twin_id_seed: str,
) -> None:
    expected = {
        "max_decision_age_ns": REGISTERED_MAX_DECISION_AGE_NS,
        "max_market_age_ns": REGISTERED_MAX_MARKET_AGE_NS,
        "max_snapshot_age_ns": REGISTERED_MAX_SNAPSHOT_AGE_NS,
        "latency_quantile": REGISTERED_LATENCY_QUANTILE,
        "slippage_quantile": REGISTERED_SLIPPAGE_QUANTILE,
        "kernel_id_seed": DEFAULT_KERNEL_ID_SEED,
        "twin_id_seed": DEFAULT_TWIN_ID_SEED,
    }
    observed = {
        "max_decision_age_ns": max_decision_age_ns,
        "max_market_age_ns": max_market_age_ns,
        "max_snapshot_age_ns": max_snapshot_age_ns,
        "latency_quantile": latency_quantile,
        "slippage_quantile": slippage_quantile,
        "kernel_id_seed": kernel_id_seed,
        "twin_id_seed": twin_id_seed,
    }
    for label, expected_value in expected.items():
        if observed[label] != expected_value:
            raise ValueError(
                f"captured-account replay {label} must equal the registered "
                f"value {expected_value!r}"
            )


@dataclass(frozen=True, slots=True)
class CapturedAccountReplayReceipt:
    """Verified receipt returned after atomic publication of both replay roots."""

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


@dataclass(frozen=True, slots=True)
class _CaptureRecord:
    payload: Mapping[str, Any]
    source_label: str
    line_number: int


@dataclass(frozen=True, slots=True)
class _MappedBatch:
    captured: CapturedTargetRequest
    risk_event: AccountEvent
    market_inputs: Mapping[str, MarketInputRef]
    books: Mapping[str, L2BookSnapshot]
    replay_intents: tuple[RequestedIntent, ...]
    command_symbols: frozenset[str]
    require_strict_risk_reduction: bool
    context_sources: Mapping[str, tuple[str, int]]


@dataclass(frozen=True, slots=True)
class _ReplaySourcePaths:
    target_capture: Path
    demo_account: Path
    market_capture: Path
    demo_rules: Path
    risk_policy: Path
    calibration: Path
    freeze_manifest: Path
    effective_runtime_config_bundle: Path
    safety_target_capture: Path
    safety_manifest: Path
    input_manifest: Path

    def source_directories(self) -> tuple[Path, ...]:
        return tuple(
            {
                self.target_capture.parent,
                self.demo_account,
                self.market_capture,
                self.demo_rules.parent,
                self.risk_policy.parent,
                self.calibration.parent,
                self.freeze_manifest.parent,
                self.effective_runtime_config_bundle.parent,
                self.safety_target_capture.parent,
                self.safety_manifest.parent,
                self.input_manifest.parent,
            }
        )


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**dict(payload), "artifact_sha256": ""})).hexdigest()


def _load_calibration_receipt_bytes(
    data: bytes,
    *,
    require_registered_requirements: bool,
) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("execution-twin calibration receipt is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("execution-twin calibration receipt must be an object")
    return verify_calibration_receipt(
        value,
        require_registered_requirements=require_registered_requirements,
    )


def _is_lower_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _required_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _accepts_keyword(loader: object, keyword: str) -> bool:
    try:
        return keyword in inspect.signature(loader).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _load_freeze_snapshot(
    path: Path,
    snapshot: StableFileSnapshot,
) -> dict[str, Any]:
    if _accepts_keyword(load_natural_cutover_freeze_manifest, "snapshot"):
        return load_natural_cutover_freeze_manifest(path, snapshot=snapshot)
    return load_natural_cutover_freeze_manifest(path)


def _required_sha(value: object, *, label: str) -> str:
    if not _is_lower_sha256(value):
        raise ValueError(f"{label} must be a lowercase sha256")
    return cast(str, value)


def _freeze_artifact(
    section: Mapping[str, Any],
    name: str,
    *,
    label: str,
) -> Mapping[str, Any]:
    return _required_mapping(section.get(name), label=f"{label} {name}")


def _validate_natural_freeze_binding(
    freeze: Mapping[str, Any],
    *,
    freeze_path: Path,
    account_root: Path,
    capture_root: Path,
    rules_path: Path,
    policy_path: Path,
    calibration_path: Path,
    calibration_receipt: Mapping[str, Any],
    safety_manifest: Mapping[str, Any],
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
    source_snapshots: Mapping[str, StableFileSnapshot],
) -> dict[str, Any]:
    repository = _required_mapping(freeze.get("repository"), label="freeze repository")
    window = _required_mapping(freeze.get("window"), label="freeze window")
    runtime = _required_mapping(freeze.get("runtime"), label="freeze runtime")
    population = _required_mapping(freeze.get("population"), label="freeze population")
    training = _required_mapping(freeze.get("v7_training"), label="freeze V7 training")
    clock = _required_mapping(freeze.get("clock"), label="freeze clock")
    roots = _required_mapping(runtime.get("roots"), label="freeze roots")
    demo_roots = _required_mapping(roots.get("demo"), label="freeze demo roots")
    account_ids = _required_mapping(runtime.get("account_ids"), label="freeze account ids")
    if window.get("t0_ns") != t0_ns or window.get("t1_ns") != t1_ns:
        raise ValueError("natural replay T0/T1 differ from the top-level freeze")
    if account_ids.get("demo") != expected_account_id:
        raise ValueError("natural replay account id differs from the top-level freeze")
    if demo_roots.get("account") != str(account_root):
        raise ValueError("natural replay account root differs from the top-level freeze")
    if demo_roots.get("capture") != str(capture_root):
        raise ValueError("natural replay capture root differs from the top-level freeze")
    if freeze.get("freeze_id") != safety_manifest.get("freeze_id"):
        raise ValueError("post-window safety manifest names another top-level freeze")

    rules = _freeze_artifact(population, "demo_rules", label="freeze population")
    if rules.get("path") != str(rules_path):
        raise ValueError("natural replay demo rules path differs from the top-level freeze")
    rules_identity, _rules_data = _read_frozen_file(
        rules_path,
        label="bound demo rules",
        snapshot=source_snapshots["demo_rules"],
    )
    if rules.get("file_sha256") != rules_identity.sha256:
        raise ValueError("natural replay demo rules bytes differ from the top-level freeze")

    candidate = _freeze_artifact(
        population, "candidate_universe", label="freeze population"
    )
    candidate_path = _require_regular_file(
        Path(str(candidate.get("path") or "")), label="frozen candidate universe"
    )
    candidate_identity, _candidate_data = _read_frozen_file(
        candidate_path, label="bound candidate universe"
    )
    if candidate.get("file_sha256") != candidate_identity.sha256:
        raise ValueError(
            "natural replay candidate universe bytes differ from the top-level freeze"
        )

    calibration = _freeze_artifact(training, "calibration", label="freeze V7 training")
    if calibration.get("path") != str(calibration_path):
        raise ValueError("natural replay calibration path differs from the top-level freeze")
    calibration_identity, _calibration_data = _read_frozen_file(
        calibration_path,
        label="bound V7 calibration",
        snapshot=source_snapshots["v7_execution_twin_calibration"],
    )
    if calibration.get("file_sha256") != calibration_identity.sha256 or calibration.get(
        "artifact_sha256"
    ) != calibration_receipt.get("artifact_sha256"):
        raise ValueError("natural replay calibration differs from the top-level freeze")

    risk = _required_mapping(runtime.get("risk_policy"), label="freeze risk policy")
    risk_artifacts = _required_mapping(risk.get("artifacts"), label="freeze risk-policy artifacts")
    matching_policy = [
        _required_mapping(value, label=f"freeze risk artifact {name}")
        for name, value in risk_artifacts.items()
        if isinstance(value, Mapping) and value.get("path") == str(policy_path)
    ]
    policy_identity, _policy_data = _read_frozen_file(
        policy_path,
        label="bound risk policy",
        snapshot=source_snapshots["risk_policy"],
    )
    if len(matching_policy) != 1 or matching_policy[0].get("sha256") != policy_identity.sha256:
        raise ValueError("natural replay risk policy differs from the top-level freeze")

    clock_receipt = _freeze_artifact(clock, "receipt", label="freeze clock")
    freeze_identity, _freeze_data = _read_frozen_file(
        freeze_path,
        label="bound natural cutover freeze manifest",
        snapshot=source_snapshots["natural_cutover_freeze_manifest"],
    )
    return {
        "path": str(freeze_path),
        "file_sha256": freeze_identity.sha256,
        "artifact_sha256": _required_sha(freeze.get("artifact_sha256"), label="freeze artifact hash"),
        "freeze_id": str(freeze.get("freeze_id") or ""),
        "candidate_commit": str(repository.get("candidate_commit") or ""),
        "origin_main_commit": str(repository.get("origin_main_commit") or ""),
        "natural_window": {"t0_ns": t0_ns, "t1_ns": t1_ns},
        "routes_sha256": _required_sha(
            _required_mapping(runtime.get("routes"), label="freeze routes").get("sha256"),
            label="freeze routes hash",
        ),
        "risk_policy_sha256": _required_sha(risk.get("sha256"), label="freeze risk-policy set hash"),
        "seed_sha256": _required_sha(
            _required_mapping(runtime.get("seed"), label="freeze seed").get("sha256"),
            label="freeze seed hash",
        ),
        "demo_rules_artifact_sha256": _required_sha(
            rules.get("artifact_sha256"), label="freeze demo-rules artifact hash"
        ),
        "candidate_universe_path": str(candidate_path),
        "candidate_universe_file_sha256": candidate_identity.sha256,
        "candidate_universe_artifact_sha256": _required_sha(
            candidate.get("artifact_sha256"),
            label="freeze candidate-universe artifact hash",
        ),
        "calibration_artifact_sha256": _required_sha(
            calibration.get("artifact_sha256"),
            label="freeze calibration artifact hash",
        ),
        "clock_artifact_sha256": _required_sha(
            clock_receipt.get("artifact_sha256"),
            label="freeze clock artifact hash",
        ),
        "clock_file_sha256": _required_sha(
            clock_receipt.get("file_sha256"),
            label="freeze clock file hash",
        ),
    }


def _validate_effective_runtime_binding(
    *,
    bundle_path: Path,
    bundle_identity: _FrozenFile,
    freeze_binding: Mapping[str, Any],
    target_capture_path: Path,
    t0_ns: int,
    t1_ns: int,
    snapshot: StableFileSnapshot,
) -> dict[str, Any]:
    if _accepts_keyword(load_effective_runtime_config_bundle_binding, "snapshot"):
        _payload, binding = load_effective_runtime_config_bundle_binding(
            bundle_path,
            snapshot=snapshot,
        )
    else:
        _payload, binding = load_effective_runtime_config_bundle_binding(bundle_path)
    if (
        binding.get("path") != bundle_identity.path
        or binding.get("file_sha256") != bundle_identity.sha256
    ):
        raise ValueError(
            "effective runtime config bundle identity differs from replay source"
        )
    return validate_effective_runtime_config_bundle_join(
        binding,
        freeze_manifest_path=str(freeze_binding["path"]),
        freeze_manifest_file_sha256=str(freeze_binding["file_sha256"]),
        freeze_artifact_sha256=str(freeze_binding["artifact_sha256"]),
        freeze_id=str(freeze_binding["freeze_id"]),
        candidate_universe_path=str(freeze_binding["candidate_universe_path"]),
        candidate_universe_file_sha256=str(
            freeze_binding["candidate_universe_file_sha256"]
        ),
        candidate_universe_artifact_sha256=str(
            freeze_binding["candidate_universe_artifact_sha256"]
        ),
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        target_capture_path=target_capture_path,
    )


def _require_regular_file(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    try:
        observed = expanded.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {expanded}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise ValueError(f"{label} must be a non-symlink regular file: {expanded}")
    return expanded.resolve(strict=True)


def _require_directory(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    try:
        observed = expanded.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {expanded}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise ValueError(f"{label} must be a non-symlink directory: {expanded}")
    return expanded.resolve(strict=True)


def _frozen_file_from_snapshot(
    snapshot: StableFileSnapshot,
    *,
    label: str,
) -> _FrozenFile:
    return _FrozenFile(
        label=label,
        path=str(snapshot.path),
        size=snapshot.size,
        sha256=snapshot.sha256,
        device=snapshot.device,
        inode=snapshot.inode,
        mtime_ns=snapshot.mtime_ns,
        mode=snapshot.mode,
    )


def _read_frozen_file(
    path: Path,
    *,
    label: str,
    snapshot: StableFileSnapshot | None = None,
    require_single_link: bool = False,
) -> tuple[_FrozenFile, bytes]:
    if snapshot is None:
        resolved = _require_regular_file(path, label=label)
        snapshot = read_stable_file(
            resolved,
            label=label,
            require_single_link=require_single_link,
        )
    elif snapshot.path != path.expanduser().absolute():
        raise ValueError(f"{label} snapshot path differs")
    return (
        _frozen_file_from_snapshot(snapshot, label=label),
        snapshot.data,
    )


def _journal_source_paths(account_root: Path) -> list[tuple[str, Path]]:
    transaction_root = account_transactions_path(account_root)
    transactions = sorted(transaction_root.glob("*.json")) if transaction_root.is_dir() else []
    output: list[tuple[str, Path]] = []
    for path in transactions:
        output.append((f"demo_journal_transaction/{path.name}", path))
    projection = account_journal_path(account_root)
    if projection.exists():
        output.append(("demo_journal_projection/events.jsonl", projection))
    return output


def _market_source_paths(capture_root: Path) -> list[tuple[str, Path]]:
    paths = sorted(capture_root.rglob("segment-*.jsonl"))
    if not paths:
        raise ValueError(f"no raw market-capture segments under {capture_root}")
    return [(f"market_capture/{path.relative_to(capture_root)}", path) for path in paths]


def _freeze_sources(
    *,
    target_capture_path: Path,
    demo_account_root: Path,
    market_capture_root: Path,
    demo_rules_file: Path,
    risk_policy_file: Path,
    calibration_file: Path,
    freeze_manifest_file: Path,
    effective_runtime_config_bundle_file: Path,
    safety_target_capture_file: Path | None = None,
    safety_manifest_file: Path | None = None,
    input_manifest_file: Path | None = None,
) -> tuple[
    dict[str, _FrozenFile],
    dict[str, bytes],
    dict[str, StableFileSnapshot],
]:
    inputs = [
        ("natural_cutover_freeze_manifest", freeze_manifest_file),
        ("target_scheduling_capture", target_capture_path),
        ("demo_rules", demo_rules_file),
        ("risk_policy", risk_policy_file),
        ("v7_execution_twin_calibration", calibration_file),
        ("effective_runtime_config_bundle", effective_runtime_config_bundle_file),
        *_journal_source_paths(demo_account_root),
        *_market_source_paths(market_capture_root),
    ]
    if safety_target_capture_file is not None:
        inputs.append(("post_window_safety_target_capture", safety_target_capture_file))
    if safety_manifest_file is not None:
        inputs.append(("post_window_safety_manifest", safety_manifest_file))
    if input_manifest_file is not None:
        inputs.append(("natural_account_replay_input_manifest", input_manifest_file))
    labels = [label for label, _path in inputs]
    if len(set(labels)) != len(labels):
        raise RuntimeError("internal source labels are not unique")
    identities: dict[str, _FrozenFile] = {}
    contents: dict[str, bytes] = {}
    snapshots: dict[str, StableFileSnapshot] = {}
    for label, path in inputs:
        resolved = _require_regular_file(path, label=label)
        snapshot = read_stable_file(
            resolved,
            label=label,
            require_single_link=False,
        )
        identities[label] = _frozen_file_from_snapshot(snapshot, label=label)
        contents[label] = snapshot.data
        snapshots[label] = snapshot
    paths = [identity.path for identity in identities.values()]
    inodes = [(identity.device, identity.inode) for identity in identities.values()]
    if len(set(paths)) != len(paths) or len(set(inodes)) != len(inodes):
        raise ValueError("captured-account replay source labels alias the same file/inode")
    return identities, contents, snapshots


def _identity_payload(identities: Mapping[str, _FrozenFile]) -> dict[str, Any]:
    return {label: identity.to_dict() for label, identity in sorted(identities.items())}


def _journal_from_frozen_sources(contents: Mapping[str, bytes]) -> list[AccountEvent]:
    transaction_files = [
        (label, contents[label])
        for label in sorted(contents)
        if label.startswith("demo_journal_transaction/")
    ]
    projection_label = "demo_journal_projection/events.jsonl"
    projection_data = contents.get(projection_label)
    events = read_account_journal_bytes(
        transaction_files=transaction_files,
        projection_data=projection_data,
        projection_label=projection_label,
        verify=True,
    )
    if projection_data is not None:
        expected_projection = b"".join(
            canonical_json(event.to_dict()) + b"\n" for event in events
        )
        if projection_data != expected_projection:
            raise ValueError(
                "demo journal projection does not exactly match authoritative transactions"
            )
    return events


def _validate_source_layout(
    *,
    account_root: Path,
    capture_root: Path,
    files: Mapping[str, Path],
) -> None:
    if account_root == capture_root or account_root in capture_root.parents or capture_root in account_root.parents:
        raise ValueError("demo account and market-capture roots must be disjoint and non-nested")
    for label, path in files.items():
        if path == account_root or path == capture_root:
            raise ValueError(f"{label} aliases a source root")
        if account_root in path.parents or capture_root in path.parents:
            raise ValueError(f"{label} must be outside account and market-capture roots")


def _require_sources_unchanged(
    expected: Mapping[str, _FrozenFile],
    *,
    target_capture_path: Path,
    demo_account_root: Path,
    market_capture_root: Path,
    demo_rules_file: Path,
    risk_policy_file: Path,
    calibration_file: Path,
    freeze_manifest_file: Path,
    effective_runtime_config_bundle_file: Path,
    safety_target_capture_file: Path | None = None,
    safety_manifest_file: Path | None = None,
    input_manifest_file: Path | None = None,
) -> None:
    observed, _contents, _snapshots = _freeze_sources(
        target_capture_path=target_capture_path,
        demo_account_root=demo_account_root,
        market_capture_root=market_capture_root,
        demo_rules_file=demo_rules_file,
        risk_policy_file=risk_policy_file,
        calibration_file=calibration_file,
        freeze_manifest_file=freeze_manifest_file,
        effective_runtime_config_bundle_file=effective_runtime_config_bundle_file,
        safety_target_capture_file=safety_target_capture_file,
        safety_manifest_file=safety_manifest_file,
        input_manifest_file=input_manifest_file,
    )
    if _identity_payload(observed) != _identity_payload(expected):
        raise RuntimeError("captured-account replay source files changed during the run")


def _parse_market_capture(
    identities: Mapping[str, _FrozenFile],
    contents: Mapping[str, bytes],
) -> tuple[dict[str, _CaptureRecord], int]:
    records: dict[str, _CaptureRecord] = {}
    count = 0
    for label in sorted(key for key in identities if key.startswith("market_capture/")):
        data = contents[label]
        if data and not data.endswith(b"\n"):
            raise ValueError(f"market capture has a partial final line: {label}")
        for line_number, raw in enumerate(data.splitlines(), start=1):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid market capture JSON: {label}:{line_number}") from exc
            if not isinstance(value, dict) or int(value.get("schema_version") or 0) != 1:
                raise ValueError(f"unknown market capture schema: {label}:{line_number}")
            if type(value.get("local_receive_ts_ns")) is not int or int(value["local_receive_ts_ns"]) <= 0:
                raise ValueError(f"market capture row lacks receive time: {label}:{line_number}")
            record_id = str(value.get("record_id") or "")
            if record_id != capture_record_id(value):
                raise ValueError(f"market capture record id mismatch: {label}:{line_number}")
            if record_id in records:
                raise ValueError(f"duplicate market capture record id: {record_id}")
            records[record_id] = _CaptureRecord(value, label, line_number)
            count += 1
    return records, count


def _strict_levels(value: object, *, label: str) -> tuple[BookLevel, ...]:
    if type(value) is not list or not value:
        raise ValueError(f"{label} must be a nonempty list")
    output: list[BookLevel] = []
    for index, row in enumerate(value):
        if type(row) is not list or len(row) != 2:
            raise ValueError(f"{label}[{index}] must be [price, quantity]")
        try:
            price = float(row[0])
            quantity = float(row[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}[{index}] is nonnumeric") from exc
        if price <= 0.0 or quantity <= 0.0:
            raise ValueError(f"{label}[{index}] must have positive price and quantity")
        output.append(BookLevel(price, quantity))
    return tuple(output)


def _market_from_demo_event(
    *,
    event: AccountEvent,
    batch_id: str,
    capture: _CaptureRecord,
) -> tuple[MarketInputRef, L2BookSnapshot]:
    context = capture.payload
    input_key = str(event.payload.get("input_key") or "")
    if (
        str(context.get("record_id") or "") != input_key
        or context.get("kind") != "book_context"
        or context.get("context_kind") != "account_service_decision"
        or context.get("reference_key") != batch_id
        or str(context.get("symbol") or "").upper() != event.symbol.upper()
    ):
        raise ValueError(f"demo market input {input_key!r} does not map to its exact decision book_context")
    bids = _strict_levels(context.get("bids"), label=f"{input_key} bids")
    asks = _strict_levels(context.get("asks"), label=f"{input_key} asks")
    if any(left.price < right.price for left, right in zip(bids, bids[1:])):
        raise ValueError(f"captured book_context {input_key!r} bids are not descending")
    if any(left.price > right.price for left, right in zip(asks, asks[1:])):
        raise ValueError(f"captured book_context {input_key!r} asks are not ascending")
    if max(level.price for level in bids) > min(level.price for level in asks):
        raise ValueError(f"captured book_context {input_key!r} is crossed")
    metadata = event.payload.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError(f"demo market input {input_key!r} has invalid metadata")
    sequence_gap = context.get("sequence_gap")
    if type(sequence_gap) is not bool:
        raise ValueError(f"captured book_context {input_key!r} lacks exact gap state")
    exchange_ts_ns = int(context.get("exchange_engine_ts_ns") or context.get("exchange_system_ts_ns") or 0)
    local_receive_ts_ns = int(context.get("book_local_receive_ts_ns") or 0)
    sequence = int(context.get("cross_sequence") or 0)
    if min(exchange_ts_ns, local_receive_ts_ns, sequence) <= 0:
        raise ValueError(f"captured book_context {input_key!r} lacks exact book clocks/sequence")
    expected_offset = local_receive_ts_ns - exchange_ts_ns
    expected_metadata = {
        "previous_sequence": None,
        "sequence_gap": sequence_gap,
        "clock_offset_estimate_ns": expected_offset,
        "capture_record_id": input_key,
        "update_id": int(context.get("update_id") or 0),
        "sequence_gap_reason": str(context.get("sequence_gap_reason") or ""),
    }
    if canonical_json(dict(metadata)) != canonical_json(expected_metadata):
        raise ValueError(f"demo market input {input_key!r} metadata disagrees with raw capture")
    book = L2BookSnapshot(
        symbol=event.symbol.upper(),
        sequence=sequence,
        previous_sequence=None,
        exchange_ts_ns=exchange_ts_ns,
        local_receive_ts_ns=local_receive_ts_ns,
        bids=bids,
        asks=asks,
        sequence_gap=sequence_gap,
        clock_offset_estimate_ns=expected_offset,
    )
    market = MarketInputRef(
        input_key=input_key,
        symbol=event.symbol.upper(),
        exchange_ts_ns=exchange_ts_ns,
        local_receive_ts_ns=local_receive_ts_ns,
        reference_price=(bids[0].price + asks[0].price) / 2.0,
        bid_price=bids[0].price,
        ask_price=asks[0].price,
        book_sequence=sequence,
        source="bybit_raw_l2",
        metadata=expected_metadata,
    )
    expected_payload = {"batch_id": batch_id, **asdict(market)}
    # Symbol is the account-event envelope field, not duplicated in the
    # MARKET_INPUT_REF payload.
    expected_payload.pop("symbol")
    if canonical_json(event.payload) != canonical_json(expected_payload):
        raise ValueError(f"demo MARKET_INPUT_REF {input_key!r} changed from its raw context")
    return market, book


def _expected_target_payload(
    *,
    batch_id: str,
    requested: RequestedIntent,
    market: MarketInputRef,
    rules: InstrumentRules,
) -> dict[str, Any]:
    target = requested.adapter().desired_target(requested.intent, market, rules)
    return {
        "batch_id": batch_id,
        "decision_key": target.decision_key,
        "target_key": target.target_key,
        "sleeve": target.sleeve,
        "strategy_id": target.strategy_id,
        "component_id": target.component_id,
        "symbol": target.symbol.upper(),
        "signed_qty": float(target.signed_qty),
        "reference_price": float(target.reference_price),
        "leverage": float(target.leverage),
        "reason": target.reason,
        "metadata": json_safe(dict(target.metadata)),
    }


def _expected_decision_payload(target_payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = target_payload.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("expected target metadata is malformed")
    return {
        "batch_id": target_payload["batch_id"],
        "decision_key": target_payload["decision_key"],
        "strategy_id": target_payload["strategy_id"],
        "component_id": target_payload["component_id"],
        "reason": target_payload["reason"],
        "market_input_key": metadata.get("market_input_key") or "",
        "metadata": dict(metadata),
    }


def _events_by_type(
    events: Sequence[AccountEvent],
    *,
    batch_id: str,
    event_type: AccountEventType,
) -> list[AccountEvent]:
    return [event for event in events if event.correlation_id == batch_id and event.event_type == event_type.value]


def _risk_snapshot_from_event(event: AccountEvent) -> AccountRiskSnapshot:
    raw = event.payload.get("risk_snapshot")
    expected = set(AccountRiskSnapshot.__dataclass_fields__)
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError(f"RISK_DECISION {event.correlation_id!r} has an invalid risk snapshot")
    try:
        snapshot = AccountRiskSnapshot(
            equity_usdt=float(raw["equity_usdt"]),
            available_margin_usdt=float(raw["available_margin_usdt"]),
            snapshot_key=str(raw["snapshot_key"]),
            snapshot_ts_ns=int(raw["snapshot_ts_ns"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"RISK_DECISION {event.correlation_id!r} risk snapshot is malformed") from exc
    if canonical_json(asdict(snapshot)) != canonical_json(dict(raw)):
        raise ValueError(f"RISK_DECISION {event.correlation_id!r} coerces its risk snapshot")
    if snapshot.snapshot_ts_ns <= 0 or not snapshot.snapshot_key:
        raise ValueError(f"RISK_DECISION {event.correlation_id!r} lacks snapshot identity")
    return snapshot


def _validate_risk_inputs(
    *,
    risk_event: AccountEvent,
    snapshot: AccountRiskSnapshot,
    market_inputs: Mapping[str, MarketInputRef],
    risk_policy: AccountRiskPolicy,
    instrument_rules: Mapping[str, InstrumentRules],
    max_market_age_ns: int,
    max_snapshot_age_ns: int,
) -> bool:
    raw_policy = risk_event.payload.get("risk_policy")
    if not isinstance(raw_policy, Mapping) or canonical_json(raw_policy) != canonical_json(asdict(risk_policy)):
        raise ValueError(f"RISK_DECISION {risk_event.correlation_id!r} does not use the supplied risk policy")
    raw_rules = risk_event.payload.get("instrument_rules")
    aggregates = risk_event.payload.get("aggregate_targets")
    if not isinstance(raw_rules, Mapping) or not isinstance(aggregates, Mapping):
        raise ValueError(f"RISK_DECISION {risk_event.correlation_id!r} has invalid rule evidence")
    expected_rule_symbols = {str(symbol).upper() for symbol in aggregates if str(symbol).upper() in instrument_rules}
    if {str(symbol).upper() for symbol in raw_rules} != expected_rule_symbols:
        raise ValueError(f"RISK_DECISION {risk_event.correlation_id!r} instrument-rule coverage changed")
    for symbol, raw in raw_rules.items():
        normalized = str(symbol).upper()
        if not isinstance(raw, Mapping) or canonical_json(raw) != canonical_json(asdict(instrument_rules[normalized])):
            raise ValueError(
                f"RISK_DECISION {risk_event.correlation_id!r} rule for {normalized} "
                "does not match the supplied demo receipt"
            )
    strict_reduction = risk_event.payload.get("strict_risk_reduction_required")
    if type(strict_reduction) is not bool:
        raise ValueError(f"RISK_DECISION {risk_event.correlation_id!r} lacks exact reduction admission mode")
    # Exact demo timestamps are replay inputs. Non-reduction requests must
    # still satisfy the production service's freshness defaults; the adapter
    # refuses to make them fresh by moving either clock. Exit-only admission
    # deliberately permits the service's explicit unavailable/stale preview.
    if not strict_reduction:
        for symbol, market in market_inputs.items():
            age = risk_event.wall_ts_ns - market.local_receive_ts_ns
            if age < 0 or age > max_market_age_ns:
                raise ValueError(
                    f"demo batch {risk_event.correlation_id!r} has stale/future market input for {symbol}: age_ns={age}"
                )
        snapshot_age = risk_event.wall_ts_ns - snapshot.snapshot_ts_ns
        if snapshot_age < 0 or snapshot_age > max_snapshot_age_ns:
            raise ValueError(
                f"demo batch {risk_event.correlation_id!r} has stale/future risk snapshot: age_ns={snapshot_age}"
            )
        if risk_event.payload.get("risk_snapshot_status") != "observed":
            raise ValueError(f"demo batch {risk_event.correlation_id!r} has unavailable entry capital")
    return strict_reduction


def _flatten_captured_requests(
    capture_events: Sequence[TargetSchedulingCaptureEvent],
    *,
    expected_account_id: str,
) -> tuple[CapturedTargetRequest, ...]:
    if not capture_events:
        raise ValueError("target scheduling capture must contain at least one successful event")
    requests: list[CapturedTargetRequest] = []
    for event in capture_events:
        if event.source_environment != "demo":
            raise ValueError("captured-account replay requires one demo-source target capture")
        for captured in event.requests:
            request = captured.request
            if request.account_id != expected_account_id or request.environment != "demo":
                raise ValueError(f"captured request {request.request_id!r} is not for the expected demo account")
            requests.append(captured)
    request_ids = [item.request.request_id for item in requests]
    batch_ids = [item.request.batch_id for item in requests]
    if len(set(request_ids)) != len(request_ids):
        raise ValueError("target scheduling capture repeats a durable request_id")
    if len(set(batch_ids)) != len(batch_ids):
        raise ValueError("target scheduling capture repeats a durable batch_id")
    return tuple(requests)


def _validated_post_window_safety_capture(
    *,
    freeze_id: str,
    t1_ns: int,
    expected_account_id: str,
    capture_events: Sequence[TargetSchedulingCaptureEvent],
) -> tuple[tuple[CapturedTargetRequest, ...], str]:
    """Validate the exact reserved target producer, not merely zero notionals."""

    requests = _flatten_captured_requests(
        capture_events,
        expected_account_id=expected_account_id,
    )
    namespace_prefix = f"{POST_WINDOW_SAFETY_NAMESPACE}/{freeze_id}/"
    expected_payload_fields = {
        "execution_environment",
        "strategy_profile",
        "natural_safety_flatten",
        "natural_freeze_id",
        "natural_t1_ns",
        "account_id",
        "route_id",
        "journal_sequence",
        "journal_state_hash",
        "scope",
    }
    route_id = ""
    empty_event_count = 0
    request_event_count = 0
    prior_event_ts_ns = 0
    for capture_index, event in enumerate(capture_events):
        source = event.source_event
        payload = source.payload
        if set(payload) != expected_payload_fields:
            raise ValueError("post-window safety source event has invalid producer fields")
        if (
            event.source_environment != "demo"
            or event.strategy_profile != POST_WINDOW_SAFETY_STRATEGY_PROFILE
            or source.kind != "timer"
            or source.ingest_ts_ns != source.event_ts_ns
            or source.source_sequence != capture_index
            or payload.get("execution_environment") != "demo"
            or payload.get("strategy_profile") != POST_WINDOW_SAFETY_STRATEGY_PROFILE
            or payload.get("natural_safety_flatten") is not True
            or payload.get("natural_freeze_id") != freeze_id
            or payload.get("natural_t1_ns") != t1_ns
            or payload.get("account_id") != expected_account_id
            or payload.get("scope") != POST_WINDOW_SAFETY_SCOPE
        ):
            raise ValueError("post-window safety source event is not the reserved producer")
        observed_route_id = payload.get("route_id")
        if type(observed_route_id) is not str or not observed_route_id:
            raise ValueError("post-window safety source event lacks route identity")
        if route_id and observed_route_id != route_id:
            raise ValueError("post-window safety capture spans multiple account routes")
        route_id = observed_route_id
        journal_sequence = payload.get("journal_sequence")
        journal_state_hash = payload.get("journal_state_hash")
        if type(journal_sequence) is not int or journal_sequence < 0 or not _is_lower_sha256(journal_state_hash):
            raise ValueError("post-window safety source event has invalid journal identity")
        if source.event_ts_ns < t1_ns:
            raise ValueError("post-window safety capture contains a pre-T1 source event")
        if source.event_ts_ns <= prior_event_ts_ns:
            raise ValueError("post-window safety source events are not strictly ordered")
        prior_event_ts_ns = source.event_ts_ns

        if not event.requests:
            empty_event_count += 1
            if event.sleeve != SleeveAdapterKind.LONG.value or event.decision_keys:
                raise ValueError("post-window safety empty event has invalid account-wide identity")
            continue
        request_event_count += 1
        if len(event.requests) != 1:
            raise ValueError("post-window safety event must contain one component request")
        captured = event.requests[0]
        request = captured.request
        if len(request.intents) != 1:
            raise ValueError("post-window safety request must contain one component target")
        if (
            request.account_id != expected_account_id
            or request.environment != "demo"
            or request.route_id != observed_route_id
            or request.created_ts_ns != source.event_ts_ns
            or request.created_ts_ns < t1_ns
            or not request.batch_id.startswith(namespace_prefix)
        ):
            raise ValueError("post-window safety request changed route, account, or time")
        item = request.intents[0]
        if SleeveAdapterKind(item.adapter_kind) is not SleeveAdapterKind.RISK:
            raise ValueError("post-window safety request must use the RISK adapter")
        intent = item.intent
        if float(intent.signed_notional_usdt) != 0.0:
            raise ValueError("post-window safety request contains a nonzero target")
        if dict(intent.metadata) != {
            "natural_safety_flatten": True,
            "natural_freeze_id": freeze_id,
        }:
            raise ValueError("post-window safety request lacks exact safety metadata")
        owner_sleeve = intent.target_key.split("/", 1)[0]
        if (
            owner_sleeve
            not in {
                SleeveAdapterKind.LONG.value,
                SleeveAdapterKind.CONTINUOUS.value,
            }
            or event.sleeve != owner_sleeve
            or intent.strategy_id != intent.strategy_id.strip()
            or intent.component_id != intent.component_id.strip()
            or intent.symbol != intent.symbol.strip().upper()
        ):
            raise ValueError("post-window safety target has invalid natural ownership")
        expected_target_key = component_target_key(
            sleeve=owner_sleeve,
            strategy_id=intent.strategy_id,
            component_id=intent.component_id,
            symbol=intent.symbol,
        )
        if intent.target_key != expected_target_key:
            raise ValueError("post-window safety target is not a canonical component")
        if (
            not math.isfinite(float(intent.leverage))
            or float(intent.leverage) <= 0.0
            or intent.reason != POST_WINDOW_SAFETY_REASON
            or intent.decision_key != f"{request.batch_id}/zero"
        ):
            raise ValueError("post-window safety target has invalid decision identity")
        batch_parts = request.batch_id.split("/")
        target_digest = hashlib.sha256(intent.target_key.encode("utf-8")).hexdigest()[:16]
        if (
            len(batch_parts) != 5
            or batch_parts[0] != POST_WINDOW_SAFETY_NAMESPACE
            or batch_parts[1] != freeze_id
            or batch_parts[2] != str(request.created_ts_ns)
            or len(batch_parts[3]) != 4
            or not batch_parts[3].isdigit()
            or batch_parts[4] != target_digest
        ):
            raise ValueError("post-window safety request has invalid reserved batch identity")
        expected_request_id = account_target_request_id(
            batch_id=request.batch_id,
            created_ts_ns=request.created_ts_ns,
            route_id=request.route_id,
            account_id=request.account_id,
            environment=request.environment,
            intents=request.intents,
        )
        if request.request_id != expected_request_id:
            raise ValueError("post-window safety request_id is not producer-derived")
    if empty_event_count and (empty_event_count != 1 or request_event_count):
        raise ValueError("post-window safety empty event cannot be duplicated or mixed")
    return requests, route_id


def _post_window_safety_manifest_payload(
    *,
    freeze_id: str,
    t1_ns: int,
    expected_account_id: str,
    capture_path: Path,
    capture_identity: _FrozenFile,
    capture_events: Sequence[TargetSchedulingCaptureEvent],
    capture_tape_hash: str,
) -> tuple[dict[str, Any], tuple[CapturedTargetRequest, ...]]:
    if (
        not freeze_id
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in freeze_id
        )
        or "/" in freeze_id
    ):
        raise ValueError("post-window safety freeze_id must use only letters, digits, dot, dash, underscore")
    if type(t1_ns) is not int or t1_ns <= 0:
        raise ValueError("post-window safety manifest requires a positive T1")
    if capture_identity.mode != 0o600:
        raise ValueError("post-window safety target capture must be mode 0600")
    requests, route_id = _validated_post_window_safety_capture(
        freeze_id=freeze_id,
        t1_ns=t1_ns,
        expected_account_id=expected_account_id,
        capture_events=capture_events,
    )
    namespace_prefix = f"{POST_WINDOW_SAFETY_NAMESPACE}/{freeze_id}/"
    payload: dict[str, Any] = {
        "schema_version": POST_WINDOW_SAFETY_MANIFEST_SCHEMA_VERSION,
        "kind": POST_WINDOW_SAFETY_MANIFEST_KIND,
        "freeze_id": freeze_id,
        "t1_ns": t1_ns,
        "expected_account_id": expected_account_id,
        "route_id": route_id,
        "producer_profile": POST_WINDOW_SAFETY_STRATEGY_PROFILE,
        "namespace_prefix": namespace_prefix,
        "required_target_metadata": {
            "natural_safety_flatten": True,
            "natural_freeze_id": freeze_id,
        },
        "target_capture": capture_identity.to_dict(),
        "target_capture_path": str(capture_path),
        "capture_tape_hash": capture_tape_hash,
        "capture_event_count": len(capture_events),
        "successful_empty_event_count": sum(not event.requests for event in capture_events),
        "request_ids": [captured.request.request_id for captured in requests],
        "batch_ids": [captured.request.batch_id for captured in requests],
        "execution_authorization": "not_granted",
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload, requests


def _load_post_window_safety_manifest(
    *,
    manifest_data: bytes,
    manifest_identity: _FrozenFile,
    capture_path: Path,
    capture_identity: _FrozenFile,
    capture_events: Sequence[TargetSchedulingCaptureEvent],
    capture_tape_hash: str,
    expected_account_id: str,
    expected_t1_ns: int,
) -> tuple[dict[str, Any], tuple[CapturedTargetRequest, ...]]:
    if manifest_identity.mode != 0o600:
        raise ValueError("post-window safety manifest must be mode 0600")
    try:
        value = json.loads(manifest_data)
    except json.JSONDecodeError as exc:
        raise ValueError("post-window safety manifest is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("post-window safety manifest must be an object")
    payload = dict(value)
    if int(payload.get("schema_version") or 0) != POST_WINDOW_SAFETY_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unknown post-window safety manifest schema")
    if payload.get("kind") != POST_WINDOW_SAFETY_MANIFEST_KIND:
        raise ValueError("unexpected post-window safety manifest kind")
    if payload.get("execution_authorization") != "not_granted":
        raise ValueError("post-window safety manifest cannot grant execution authority")
    observed_hash = payload.get("artifact_sha256")
    if not _is_lower_sha256(observed_hash) or observed_hash != _self_hash(payload):
        raise ValueError("post-window safety manifest hash mismatch")
    if payload.get("t1_ns") != expected_t1_ns:
        raise ValueError("post-window safety manifest changed the frozen T1")
    expected, requests = _post_window_safety_manifest_payload(
        freeze_id=str(payload.get("freeze_id") or ""),
        t1_ns=expected_t1_ns,
        expected_account_id=expected_account_id,
        capture_path=capture_path,
        capture_identity=capture_identity,
        capture_events=capture_events,
        capture_tape_hash=capture_tape_hash,
    )
    if canonical_json(payload) != canonical_json(expected):
        raise ValueError("post-window safety manifest does not match its exact target capture")
    return payload, requests


def build_post_window_safety_manifest(
    *,
    target_capture_path: str | Path,
    expected_account_id: str,
    freeze_id: str,
    t1_ns: int,
    output_path: str | Path,
    capture_snapshot: StableFileSnapshot | None = None,
) -> Path:
    """Bind one separate post-T1 target-only safety capture without replaying it."""

    capture_path = _require_regular_file(
        Path(target_capture_path),
        label="post-window safety target capture",
    )
    capture_identity, capture_data = _read_frozen_file(
        capture_path,
        label="post_window_safety_target_capture",
        snapshot=capture_snapshot,
    )
    events, tape_hash = load_target_scheduling_capture_bytes(capture_data)
    payload, _requests = _post_window_safety_manifest_payload(
        freeze_id=freeze_id,
        t1_ns=t1_ns,
        expected_account_id=expected_account_id,
        capture_path=capture_path,
        capture_identity=capture_identity,
        capture_events=events,
        capture_tape_hash=tape_hash,
    )
    observed_identity, _observed_data = _read_frozen_file(
        capture_path,
        label="post_window_safety_target_capture",
    )
    if observed_identity != capture_identity:
        raise RuntimeError("post-window safety target capture changed while manifesting")
    manifest_path = _write_new_atomic_json(
        Path(output_path),
        payload,
        mode=0o600,
        label="post-window safety manifest",
    )
    loaded = load_post_window_safety_manifest(
        manifest_path,
        target_capture_path=capture_path,
        expected_account_id=expected_account_id,
        expected_t1_ns=t1_ns,
    )
    if canonical_json(loaded) != canonical_json(payload):
        raise RuntimeError("published post-window safety manifest changed")
    return manifest_path


def load_post_window_safety_manifest(
    path: str | Path,
    *,
    target_capture_path: str | Path,
    expected_account_id: str,
    expected_t1_ns: int,
    manifest_snapshot: StableFileSnapshot | None = None,
    capture_snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    manifest_identity, manifest_data = _read_frozen_file(
        Path(path),
        label="post-window safety manifest",
        snapshot=manifest_snapshot,
    )
    capture_path = _require_regular_file(
        Path(target_capture_path),
        label="post-window safety target capture",
    )
    capture_identity, capture_data = _read_frozen_file(
        capture_path,
        label="post_window_safety_target_capture",
        snapshot=capture_snapshot,
    )
    capture_events, capture_tape_hash = load_target_scheduling_capture_bytes(
        capture_data
    )
    payload, _requests = _load_post_window_safety_manifest(
        manifest_data=manifest_data,
        manifest_identity=manifest_identity,
        capture_path=capture_path,
        capture_identity=capture_identity,
        capture_events=capture_events,
        capture_tape_hash=capture_tape_hash,
        expected_account_id=expected_account_id,
        expected_t1_ns=expected_t1_ns,
    )
    return payload


def _journal_window(
    events: Sequence[AccountEvent],
    *,
    selected_batch_ids: frozenset[str],
) -> dict[str, Any]:
    selected = [event for event in events if event.correlation_id in selected_batch_ids]
    selected_risks = [event for event in selected if event.event_type == AccountEventType.RISK_DECISION.value]
    first = events[0] if events else None
    head = events[-1] if events else None
    first_selected = selected[0] if selected else None
    last_selected = selected[-1] if selected else None
    first_risk = selected_risks[0] if selected_risks else None
    last_risk = selected_risks[-1] if selected_risks else None
    return {
        "genesis_prev_event_hash": GENESIS_HASH,
        "journal_first_sequence": first.sequence if first else 0,
        "journal_first_event_hash": first.event_hash if first else GENESIS_HASH,
        "journal_first_wall_ts_ns": first.wall_ts_ns if first else None,
        "journal_first_monotonic_ns": first.monotonic_ns if first else None,
        "journal_head_sequence": head.sequence if head else 0,
        "journal_head_event_hash": head.event_hash if head else GENESIS_HASH,
        "journal_head_state_hash": head.state_hash if head else None,
        "journal_head_wall_ts_ns": head.wall_ts_ns if head else None,
        "journal_head_monotonic_ns": head.monotonic_ns if head else None,
        "first_selected_sequence": first_selected.sequence if first_selected else None,
        "first_selected_wall_ts_ns": first_selected.wall_ts_ns if first_selected else None,
        "first_selected_monotonic_ns": first_selected.monotonic_ns if first_selected else None,
        "last_selected_sequence": last_selected.sequence if last_selected else None,
        "last_selected_wall_ts_ns": last_selected.wall_ts_ns if last_selected else None,
        "last_selected_monotonic_ns": last_selected.monotonic_ns if last_selected else None,
        "first_selected_risk_sequence": first_risk.sequence if first_risk else None,
        "first_selected_risk_wall_ts_ns": first_risk.wall_ts_ns if first_risk else None,
        "first_selected_risk_monotonic_ns": first_risk.monotonic_ns if first_risk else None,
        "last_selected_risk_sequence": last_risk.sequence if last_risk else None,
        "last_selected_risk_wall_ts_ns": last_risk.wall_ts_ns if last_risk else None,
        "last_selected_risk_monotonic_ns": last_risk.monotonic_ns if last_risk else None,
    }


def _validate_post_window_safety_batches(
    *,
    captured_requests: Sequence[CapturedTargetRequest],
    demo_events: Sequence[AccountEvent],
    t1_ns: int,
) -> dict[str, Any]:
    captured_by_batch = {item.request.batch_id: item for item in captured_requests}
    safety_batch_ids = frozenset(captured_by_batch)
    safety_risks = [
        event
        for event in demo_events
        if event.event_type == AccountEventType.RISK_DECISION.value and event.correlation_id in safety_batch_ids
    ]
    observed_batch_ids = [event.correlation_id for event in safety_risks]
    if len(observed_batch_ids) != len(set(observed_batch_ids)):
        raise ValueError("demo journal repeats a post-window safety RISK_DECISION batch")
    if frozenset(observed_batch_ids) != safety_batch_ids:
        raise ValueError(
            "post-window safety capture/demo batch sets differ: "
            f"captured={sorted(safety_batch_ids)!r} observed={sorted(observed_batch_ids)!r}"
        )
    rows: list[dict[str, Any]] = []
    for risk_event in sorted(safety_risks, key=lambda event: event.sequence):
        batch_id = risk_event.correlation_id
        captured = captured_by_batch[batch_id]
        if risk_event.wall_ts_ns < t1_ns:
            raise ValueError("post-window safety risk decision precedes T1")
        if risk_event.payload.get("accepted") is not True or risk_event.payload.get("rejection_keys") not in (
            [],
            (),
        ):  # canonical journals use a list; tuple retained for defensive reads
            raise ValueError("post-window safety risk decision was not accepted cleanly")
        expected_targets = {item.intent.target_key: item.intent for item in captured.request.intents}
        target_events = _events_by_type(
            demo_events,
            batch_id=batch_id,
            event_type=AccountEventType.TARGET,
        )
        observed_targets = {str(event.payload.get("target_key") or ""): event for event in target_events}
        if set(observed_targets) != set(expected_targets):
            raise ValueError("post-window safety TARGET keys differ from its captured request")
        for target_key, event in observed_targets.items():
            metadata = event.payload.get("metadata")
            if (
                float(event.payload.get("signed_qty") or 0.0) != 0.0
                or not isinstance(metadata, Mapping)
                or metadata.get("natural_safety_flatten") is not True
                or metadata.get("natural_freeze_id") != expected_targets[target_key].metadata.get("natural_freeze_id")
            ):
                raise ValueError("post-window safety TARGET lost its zero/metadata identity")
        expected_decision_keys = sorted(item.intent.decision_key for item in captured.request.intents)
        observed_decision_keys = sorted(
            str(event.payload.get("decision_key") or "")
            for event in _events_by_type(
                demo_events,
                batch_id=batch_id,
                event_type=AccountEventType.DECISION,
            )
        )
        if observed_decision_keys != expected_decision_keys:
            raise ValueError("post-window safety DECISION keys differ from its captured request")
        rows.append(
            {
                "batch_id": batch_id,
                "request_id": captured.request.request_id,
                "risk_event_sequence": risk_event.sequence,
                "risk_event_wall_ts_ns": risk_event.wall_ts_ns,
                "target_keys": sorted(expected_targets),
                "decision_keys": expected_decision_keys,
                "accepted": True,
            }
        )
    return {
        "registered_batch_ids": [item.request.batch_id for item in captured_requests],
        "journal_batch_ids": [row["batch_id"] for row in rows],
        "batch_set_exact": True,
        "batches": rows,
    }


def _map_batches(
    *,
    captured_requests: Sequence[CapturedTargetRequest],
    demo_events: Sequence[AccountEvent],
    capture_records: Mapping[str, _CaptureRecord],
    risk_policy: AccountRiskPolicy,
    instrument_rules: Mapping[str, InstrumentRules],
    max_market_age_ns: int,
    max_snapshot_age_ns: int,
    safety_batch_ids: frozenset[str],
) -> tuple[tuple[_MappedBatch, ...], dict[str, Any], list[dict[str, Any]]]:
    captured_by_batch = {item.request.batch_id: item for item in captured_requests}
    captured_batch_ids = frozenset(captured_by_batch)
    nonconvergence_risks = [
        event
        for event in demo_events
        if event.event_type == AccountEventType.RISK_DECISION.value
        and not event.correlation_id.startswith("account-convergence/")
        and event.correlation_id not in safety_batch_ids
    ]
    duplicate_risks = sorted(
        {
            event.correlation_id
            for event in nonconvergence_risks
            if sum(other.correlation_id == event.correlation_id for other in nonconvergence_risks) > 1
        }
    )
    if duplicate_risks:
        raise ValueError("demo journal repeats strategy RISK_DECISION batches: " + ",".join(duplicate_risks))
    risks_by_batch = {event.correlation_id: event for event in nonconvergence_risks}
    if captured_batch_ids:
        first_selected_event = min(
            (event for event in demo_events if event.correlation_id in captured_batch_ids),
            key=lambda event: event.sequence,
            default=None,
        )
        if first_selected_event is None:
            raise ValueError("no captured request batch appears in the demo journal")
        pre_window = [
            event.correlation_id for event in nonconvergence_risks if event.sequence < first_selected_event.sequence
        ]
        if pre_window:
            raise ValueError(
                "fresh-reset demo journal has pre-window strategy request batches: " + ",".join(pre_window)
            )
    observed_batch_ids = frozenset(risks_by_batch)
    if observed_batch_ids != captured_batch_ids:
        missing = sorted(captured_batch_ids - observed_batch_ids)
        extra = sorted(observed_batch_ids - captured_batch_ids)
        raise ValueError(f"captured/demo strategy batch sets differ: missing={missing!r} extra={extra!r}")
    used_context_ids: set[str] = set()
    mapped: list[_MappedBatch] = []
    mapping_rows: list[dict[str, Any]] = []
    for batch_id, risk_event in sorted(risks_by_batch.items(), key=lambda item: item[1].sequence):
        captured = captured_by_batch[batch_id]
        request = captured.request
        if risk_event.payload.get("batch_id") != batch_id:
            raise ValueError(f"demo RISK_DECISION {batch_id!r} payload batch id changed")
        market_events = _events_by_type(
            demo_events,
            batch_id=batch_id,
            event_type=AccountEventType.MARKET_INPUT_REF,
        )
        if not market_events:
            raise ValueError(f"demo batch {batch_id!r} has no MARKET_INPUT_REF events")
        market_inputs: dict[str, MarketInputRef] = {}
        books: dict[str, L2BookSnapshot] = {}
        context_sources: dict[str, tuple[str, int]] = {}
        for market_event in market_events:
            symbol = market_event.symbol.upper()
            input_key = str(market_event.payload.get("input_key") or "")
            if symbol in market_inputs:
                raise ValueError(f"demo batch {batch_id!r} has extra MARKET_INPUT_REF for {symbol}")
            if input_key in used_context_ids:
                raise ValueError(f"raw book_context {input_key!r} is reused across request batches")
            capture = capture_records.get(input_key)
            if capture is None:
                raise ValueError(f"demo MARKET_INPUT_REF {input_key!r} is missing from raw capture")
            market, book = _market_from_demo_event(
                event=market_event,
                batch_id=batch_id,
                capture=capture,
            )
            market_inputs[symbol] = market
            books[symbol] = book
            context_sources[input_key] = (capture.source_label, capture.line_number)
            used_context_ids.add(input_key)
        prepared = prepare_account_request_intents(request)
        replay_intents = tuple(
            RequestedIntent(adapter_kind=source.adapter_kind, intent=intent) for source, intent in prepared
        )
        request_symbols = frozenset(intent.intent.symbol.upper() for intent in replay_intents)
        if not request_symbols.issubset(market_inputs):
            missing = sorted(request_symbols - set(market_inputs))
            raise ValueError(f"demo batch {batch_id!r} lacks requested market inputs: {missing!r}")
        missing_rules = sorted(request_symbols - set(instrument_rules))
        if missing_rules:
            raise ValueError(f"demo batch {batch_id!r} lacks supplied demo rules: {missing_rules!r}")
        expected_targets = {
            intent.intent.decision_key: _expected_target_payload(
                batch_id=batch_id,
                requested=intent,
                market=market_inputs[intent.intent.symbol.upper()],
                rules=instrument_rules[intent.intent.symbol.upper()],
            )
            for intent in replay_intents
        }
        target_events = _events_by_type(
            demo_events,
            batch_id=batch_id,
            event_type=AccountEventType.TARGET,
        )
        observed_targets = {str(event.payload.get("decision_key") or ""): event.payload for event in target_events}
        if set(observed_targets) != set(expected_targets):
            raise ValueError(f"demo batch {batch_id!r} target decisions do not match capture")
        for decision_key, expected in expected_targets.items():
            if canonical_json(observed_targets[decision_key]) != canonical_json(expected):
                raise ValueError(f"demo TARGET {batch_id!r}/{decision_key!r} differs from captured request")
        decision_events = _events_by_type(
            demo_events,
            batch_id=batch_id,
            event_type=AccountEventType.DECISION,
        )
        observed_decisions = {str(event.payload.get("decision_key") or ""): event.payload for event in decision_events}
        expected_decisions = {key: _expected_decision_payload(value) for key, value in expected_targets.items()}
        if set(observed_decisions) != set(expected_decisions):
            raise ValueError(f"demo batch {batch_id!r} decision events do not match capture")
        for decision_key, expected in expected_decisions.items():
            if canonical_json(observed_decisions[decision_key]) != canonical_json(expected):
                raise ValueError(f"demo DECISION {batch_id!r}/{decision_key!r} differs from captured request")
        snapshot = _risk_snapshot_from_event(risk_event)
        strict_reduction = _validate_risk_inputs(
            risk_event=risk_event,
            snapshot=snapshot,
            market_inputs=market_inputs,
            risk_policy=risk_policy,
            instrument_rules=instrument_rules,
            max_market_age_ns=max_market_age_ns,
            max_snapshot_age_ns=max_snapshot_age_ns,
        )
        expected_kernel_request_hash = target_batch_request_hash(
            batch_id=batch_id,
            target_payloads=list(expected_targets.values()),
            command_symbols=set(request_symbols),
            require_strict_risk_reduction=strict_reduction,
            request_content_hash=request.content_hash(),
        )
        if risk_event.payload.get("request_hash") != expected_kernel_request_hash:
            raise ValueError(f"demo RISK_DECISION {batch_id!r} request identity does not match capture")
        mapped.append(
            _MappedBatch(
                captured=captured,
                risk_event=risk_event,
                market_inputs=market_inputs,
                books=books,
                replay_intents=replay_intents,
                command_symbols=request_symbols,
                require_strict_risk_reduction=strict_reduction,
                context_sources=context_sources,
            )
        )
        mapping_rows.append(
            {
                "request_id": request.request_id,
                "request_hash": request.content_hash(),
                "batch_id": batch_id,
                "arrival_sequence": captured.arrival_sequence,
                "risk_event_sequence": risk_event.sequence,
                "risk_event_wall_ts_ns": risk_event.wall_ts_ns,
                "risk_event_monotonic_ns": risk_event.monotonic_ns,
                "risk_snapshot": asdict(snapshot),
                "risk_snapshot_sha256": hashlib.sha256(canonical_json(asdict(snapshot))).hexdigest(),
                "require_strict_risk_reduction": strict_reduction,
                "market_inputs": [
                    {
                        "symbol": symbol,
                        "input_key": market.input_key,
                        "source_file": context_sources[market.input_key][0],
                        "source_line": context_sources[market.input_key][1],
                        "market_input_sha256": hashlib.sha256(canonical_json(asdict(market))).hexdigest(),
                    }
                    for symbol, market in sorted(market_inputs.items())
                ],
            }
        )
    return (
        tuple(mapped),
        _journal_window(demo_events, selected_batch_ids=captured_batch_ids),
        mapping_rows,
    )


def _journal_sha256(events: Sequence[AccountEvent]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event.to_dict()))
        digest.update(b"\n")
    return digest.hexdigest()


def _natural_market_manifest(
    source_identities: Mapping[str, _FrozenFile],
) -> list[dict[str, str]]:
    return [
        {
            "path": label.removeprefix("market_capture/"),
            "sha256": identity.sha256,
        }
        for label, identity in sorted(source_identities.items())
        if label.startswith("market_capture/")
    ]


def _calibration_epoch_identity(
    receipt: Mapping[str, Any],
    *,
    demo_events: Sequence[AccountEvent],
    source_identities: Mapping[str, _FrozenFile],
    expected_account_id: str,
) -> dict[str, Any]:
    raw_inputs = receipt.get("inputs")
    if not isinstance(raw_inputs, Mapping):
        raise ValueError("V7 calibration receipt lacks bound inputs")
    if receipt.get("expected_account_id") != expected_account_id:
        raise ValueError("V7 calibration receipt belongs to another account")
    calibration_journal_sha256 = raw_inputs.get("account_journal_sha256")
    calibration_journal_head = raw_inputs.get("account_last_event_hash")
    if not _is_lower_sha256(calibration_journal_sha256) or not _is_lower_sha256(calibration_journal_head):
        raise ValueError("V7 calibration receipt lacks a valid training journal identity")
    raw_manifest = raw_inputs.get("market_capture_manifest")
    if not isinstance(raw_manifest, list) or not raw_manifest:
        raise ValueError("V7 calibration receipt lacks its market-capture manifest")
    calibration_manifest: list[dict[str, str]] = []
    for index, row in enumerate(raw_manifest):
        if (
            not isinstance(row, Mapping)
            or set(row) != {"path", "sha256"}
            or type(row.get("path")) is not str
            or not str(row["path"])
            or not _is_lower_sha256(row.get("sha256"))
        ):
            raise ValueError(f"V7 calibration market manifest row {index} is malformed")
        calibration_manifest.append(
            {
                "path": str(row["path"]),
                "sha256": str(row["sha256"]),
            }
        )
    if len({row["path"] for row in calibration_manifest}) != len(calibration_manifest):
        raise ValueError("V7 calibration market manifest repeats a path")
    manifest_hash = hashlib.sha256(canonical_json({"files": calibration_manifest})).hexdigest()
    if raw_inputs.get("market_capture_manifest_sha256") != manifest_hash:
        raise ValueError("V7 calibration market-capture manifest hash does not reproduce")
    natural_journal_sha256 = _journal_sha256(demo_events)
    natural_journal_head = demo_events[-1].event_hash if demo_events else GENESIS_HASH
    natural_manifest = _natural_market_manifest(source_identities)
    natural_manifest_sha256 = hashlib.sha256(canonical_json({"files": natural_manifest})).hexdigest()
    natural_event_hashes = {event.event_hash for event in demo_events}
    calibration_segment_hashes = {row["sha256"] for row in calibration_manifest}
    natural_segment_hashes = {row["sha256"] for row in natural_manifest}
    reused_segments = sorted(calibration_segment_hashes & natural_segment_hashes)
    if calibration_journal_sha256 == natural_journal_sha256:
        raise ValueError("V7 training and natural holdout reuse the same journal bytes")
    if calibration_journal_head in natural_event_hashes:
        raise ValueError("natural holdout journal continues or reuses the V7 training chain")
    if raw_inputs.get("market_capture_manifest_sha256") == natural_manifest_sha256:
        raise ValueError("V7 training and natural holdout reuse the same raw-capture manifest")
    if reused_segments:
        raise ValueError("V7 training and natural holdout reuse raw-capture segment bytes")
    return {
        "v7_artifact_sha256": receipt["artifact_sha256"],
        "expected_account_id": expected_account_id,
        "training_account_journal_sha256": calibration_journal_sha256,
        "training_account_last_event_hash": calibration_journal_head,
        "training_market_capture_manifest_sha256": raw_inputs["market_capture_manifest_sha256"],
        "natural_account_journal_sha256": natural_journal_sha256,
        "natural_account_last_event_hash": natural_journal_head,
        "natural_market_capture_manifest_sha256": natural_manifest_sha256,
        "shared_market_capture_segment_sha256s": [],
        "epoch_separation_passed": True,
        "v7_embedded_source_paths_are_historical_labels": True,
    }


def _make_cycles(mapped: Sequence[_MappedBatch]) -> tuple[HistoricalReplayCycle, ...]:
    cycles: list[HistoricalReplayCycle] = []
    for item in mapped:
        snapshot = _risk_snapshot_from_event(item.risk_event)
        cycles.append(
            HistoricalReplayCycle(
                batch_id=item.captured.request.batch_id,
                wall_ts_ns=item.risk_event.wall_ts_ns,
                monotonic_ns=item.risk_event.monotonic_ns,
                books=item.books,
                market_inputs=item.market_inputs,
                intents=item.replay_intents,
                risk_snapshot=snapshot,
                command_symbols=item.command_symbols,
                require_strict_risk_reduction=item.require_strict_risk_reduction,
                request_content_hash=item.captured.request.content_hash(),
            )
        )
    return tuple(cycles)


def _normalized_plan_rows(
    events: Sequence[AccountEvent],
    *,
    batch_ids: frozenset[str],
) -> list[dict[str, Any]]:
    return [
        {
            "event_type": event.event_type,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "account_id": event.account_id,
            "sleeve": event.sleeve,
            "symbol": event.symbol,
            "wall_ts_ns": event.wall_ts_ns,
            "monotonic_ns": event.monotonic_ns,
            "payload": event.payload,
        }
        for event in events
        if event.correlation_id in batch_ids and event.event_type in _PLAN_EVENT_TYPES
    ]


def _exact_plan_comparison(
    demo_events: Sequence[AccountEvent],
    historical_events: Sequence[AccountEvent],
    paper_events: Sequence[AccountEvent],
    *,
    ordered_batch_ids: Sequence[str],
) -> tuple[bool, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    passed = True
    for batch_id in ordered_batch_ids:
        selected = frozenset({batch_id})
        demo = _normalized_plan_rows(demo_events, batch_ids=selected)
        historical = _normalized_plan_rows(historical_events, batch_ids=selected)
        paper = _normalized_plan_rows(paper_events, batch_ids=selected)
        demo_material = {"events": demo}
        historical_material = {"events": historical}
        paper_material = {"events": paper}
        historical_match = canonical_json(historical_material) == canonical_json(demo_material)
        paper_match = canonical_json(paper_material) == canonical_json(demo_material)
        modeled_match = canonical_json(historical_material) == canonical_json(paper_material)
        passed = passed and historical_match and paper_match and modeled_match
        rows.append(
            {
                "batch_id": batch_id,
                "demo_plan_sha256": hashlib.sha256(canonical_json(demo_material)).hexdigest(),
                "historical_plan_sha256": hashlib.sha256(canonical_json(historical_material)).hexdigest(),
                "paper_plan_sha256": hashlib.sha256(canonical_json(paper_material)).hexdigest(),
                "historical_matches_demo": historical_match,
                "paper_matches_demo": paper_match,
                "historical_matches_paper": modeled_match,
            }
        )
    return passed, rows


def _batch_summaries(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "batch_id": batch.batch_id,
            "accepted": batch.accepted,
            "rejection_keys": list(batch.rejection_keys),
            "command_payloads": [asdict(command) for command in batch.commands],
            "state_hash": batch.state_hash,
        }
        for batch in result.batches
    ]


def _event_counts(events: Sequence[AccountEvent]) -> dict[str, int]:
    return {
        event_type.value: sum(event.event_type == event_type.value for event in events)
        for event_type in AccountEventType
    }


def _natural_tape_sufficiency_evidence(
    *,
    capture_events: Sequence[TargetSchedulingCaptureEvent],
    demo_events: Sequence[AccountEvent],
    t0_ns: int,
    t1_ns: int,
) -> dict[str, Any]:
    scheduling_rows: list[dict[str, Any]] = []
    covered_hours: dict[str, set[int]] = {"long": set(), "continuous": set()}
    batch_lineage: dict[str, dict[str, Any]] = {}
    for capture in capture_events:
        hour_index = (capture.source_event.event_ts_ns - t0_ns) // HOUR_NS
        covered_hours[capture.sleeve].add(hour_index)
        batch_ids = [request.request.batch_id for request in capture.requests]
        scheduling_rows.append(
            {
                "capture_event_id": capture.capture_event_id,
                "source_event_id": capture.source_event.event_id,
                "sleeve": capture.sleeve,
                "event_ts_ns": capture.source_event.event_ts_ns,
                "hour_index": hour_index,
                "decision_keys": list(capture.decision_keys),
                "batch_ids": batch_ids,
            }
        )
        for captured in capture.requests:
            adapter_kinds = sorted({SleeveAdapterKind(item.adapter_kind).value for item in captured.request.intents})
            unambiguous = (
                capture.sleeve
                if adapter_kinds == [capture.sleeve] and capture.sleeve in {"long", "continuous"}
                else None
            )
            batch_lineage[captured.request.batch_id] = {
                "batch_id": captured.request.batch_id,
                "capture_event_id": capture.capture_event_id,
                "capture_sleeve": capture.sleeve,
                "adapter_kinds": adapter_kinds,
                "unambiguous_sleeve": unambiguous,
            }

    command_rows: list[dict[str, Any]] = []
    commands: dict[str, dict[str, Any]] = {}
    for event in demo_events:
        if event.event_type != AccountEventType.ORDER_COMMAND.value:
            continue
        command_id = str(event.payload.get("command_id") or "")
        batch_id = str(event.payload.get("batch_id") or event.correlation_id)
        lineage = batch_lineage.get(batch_id)
        row = {
            "sequence": event.sequence,
            "command_id": command_id,
            "batch_id": batch_id,
            "symbol": event.symbol,
            "unambiguous_sleeve": (lineage.get("unambiguous_sleeve") if lineage is not None else None),
            "source_kind": "captured_strategy_batch" if lineage is not None else "owner_or_unscoped",
        }
        command_rows.append(row)
        commands[command_id] = row

    fill_rows: list[dict[str, Any]] = []
    all_filled_command_ids: set[str] = set()
    filled_command_ids: set[str] = set()
    filled_command_ids_by_sleeve: dict[str, set[str]] = {
        "long": set(),
        "continuous": set(),
    }
    filled_symbols: set[str] = set()
    for event in demo_events:
        if event.event_type != AccountEventType.FILL.value:
            continue
        command_id = str(event.payload.get("command_id") or "")
        command = commands.get(command_id)
        sleeve = command.get("unambiguous_sleeve") if command is not None else None
        all_filled_command_ids.add(command_id)
        counts_toward_natural = command is not None and command.get("source_kind") == "captured_strategy_batch"
        if counts_toward_natural:
            filled_command_ids.add(command_id)
            filled_symbols.add(event.symbol)
            if sleeve in filled_command_ids_by_sleeve:
                filled_command_ids_by_sleeve[str(sleeve)].add(command_id)
        fill_rows.append(
            {
                "sequence": event.sequence,
                "execution_id": str(event.payload.get("execution_id") or ""),
                "command_id": command_id,
                "batch_id": command.get("batch_id") if command is not None else None,
                "symbol": event.symbol,
                "signed_qty": event.payload.get("signed_qty"),
                "unambiguous_sleeve": sleeve,
                "counts_toward_natural_lifecycle": counts_toward_natural,
            }
        )

    positions: dict[str, float] = {}
    cycle_sleeves: dict[str, set[str]] = {}
    cycle_start: dict[str, int] = {}
    round_trips: list[dict[str, Any]] = []
    round_trips_by_sleeve = {"long": 0, "continuous": 0}
    for fill in fill_rows:
        if fill["counts_toward_natural_lifecycle"] is not True:
            continue
        symbol = str(fill["symbol"])
        signed_qty = float(fill["signed_qty"])
        prior = positions.get(symbol, 0.0)
        if abs(prior) <= 1e-12:
            prior = 0.0
            cycle_sleeves[symbol] = set()
            cycle_start[symbol] = int(fill["sequence"])
        sleeve = fill["unambiguous_sleeve"]
        cycle_sleeves.setdefault(symbol, set()).add(str(sleeve) if sleeve in {"long", "continuous"} else "unattributed")
        updated = math.fsum((prior, signed_qty))
        if abs(updated) <= 1e-12:
            updated = 0.0
        crossed = prior != 0.0 and updated != 0.0 and prior * updated < 0.0
        closed = prior != 0.0 and (updated == 0.0 or crossed)
        if closed:
            observed_sleeves = sorted(cycle_sleeves.get(symbol, set()))
            attributable = (
                observed_sleeves[0]
                if len(observed_sleeves) == 1 and observed_sleeves[0] in round_trips_by_sleeve
                else None
            )
            if attributable is not None:
                round_trips_by_sleeve[attributable] += 1
            round_trips.append(
                {
                    "symbol": symbol,
                    "start_fill_sequence": cycle_start.get(symbol),
                    "close_fill_sequence": fill["sequence"],
                    "observed_sleeves": observed_sleeves,
                    "unambiguous_sleeve": attributable,
                    "sign_flip": crossed,
                }
            )
        if crossed:
            cycle_sleeves[symbol] = {str(sleeve) if sleeve in {"long", "continuous"} else "unattributed"}
            cycle_start[symbol] = int(fill["sequence"])
        elif updated == 0.0:
            cycle_sleeves.pop(symbol, None)
            cycle_start.pop(symbol, None)
        positions[symbol] = updated

    natural_close_keys = {
        str(event.payload.get("close_key") or "")
        for event in demo_events
        if event.event_type == AccountEventType.CLOSE.value
        and str(event.payload.get("command_id") or "") in filled_command_ids
        and str(event.payload.get("close_key") or "")
    }
    all_pnl_rows = [
        {
            "sequence": event.sequence,
            "pnl_key": str(event.payload.get("pnl_key") or ""),
            "close_key": str(event.payload.get("close_key") or ""),
            "symbol": event.symbol,
            "source": str(event.payload.get("source") or ""),
        }
        for event in demo_events
        if event.event_type == AccountEventType.PNL.value
    ]
    pnl_rows = [row for row in all_pnl_rows if row["close_key"] in natural_close_keys]
    required_hours = set(range(NATURAL_WINDOW_HOURS))
    missing_hours = {sleeve: sorted(required_hours - observed) for sleeve, observed in covered_hours.items()}
    filled_counts = {sleeve: len(command_ids) for sleeve, command_ids in filled_command_ids_by_sleeve.items()}
    floor_checks: dict[str, bool] = {
        "fixed_half_open_120h_window": t1_ns - t0_ns == NATURAL_WINDOW_HOURS * HOUR_NS,
        "hourly_long_capture_coverage": not missing_hours["long"],
        "hourly_continuous_capture_coverage": not missing_hours["continuous"],
        "at_least_30_filled_demo_commands": (len(filled_command_ids) >= NATURAL_MIN_FILLED_COMMANDS),
        "at_least_10_attributable_filled_commands_per_sleeve": all(
            count >= NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE for count in filled_counts.values()
        ),
        "at_least_3_filled_symbols": len(filled_symbols) >= NATURAL_MIN_FILLED_SYMBOLS,
        "at_least_3_conservative_round_trips_per_sleeve": all(
            count >= NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE for count in round_trips_by_sleeve.values()
        ),
        "at_least_10_account_pnl_events": len(pnl_rows) >= NATURAL_MIN_PNL_EVENTS,
    }
    unattributed_filled = sorted(
        filled_command_ids - filled_command_ids_by_sleeve["long"] - filled_command_ids_by_sleeve["continuous"]
    )
    return {
        "registered_floors": {
            "window_hours": NATURAL_WINDOW_HOURS,
            "hourly_event_and_outcome_per_sleeve": 1,
            "filled_demo_commands": NATURAL_MIN_FILLED_COMMANDS,
            "filled_demo_commands_per_sleeve": NATURAL_MIN_FILLED_COMMANDS_PER_SLEEVE,
            "filled_symbols": NATURAL_MIN_FILLED_SYMBOLS,
            "round_trips_per_sleeve": NATURAL_MIN_ROUND_TRIPS_PER_SLEEVE,
            "pnl_events": NATURAL_MIN_PNL_EVENTS,
        },
        "observed_counts": {
            "capture_events_by_sleeve": {
                sleeve: sum(event.sleeve == sleeve for event in capture_events) for sleeve in ("long", "continuous")
            },
            "filled_demo_commands": len(filled_command_ids),
            "all_epoch_filled_commands_including_post_window_safety": len(all_filled_command_ids),
            "attributable_filled_commands_by_sleeve": filled_counts,
            "filled_symbols": sorted(filled_symbols),
            "conservative_round_trips_by_sleeve": round_trips_by_sleeve,
            "account_pnl_events": len(pnl_rows),
        },
        "missing_hour_indices_by_sleeve": missing_hours,
        "registered_floor_checks": floor_checks,
        "all_registered_numeric_floors_observed_conservatively": all(floor_checks.values()),
        "unattributed_filled_command_ids": unattributed_filled,
        "scheduling_rows": scheduling_rows,
        "batch_lineage": [batch_lineage[key] for key in sorted(batch_lineage)],
        "command_rows": command_rows,
        "fill_rows": fill_rows,
        "conservative_round_trip_rows": round_trips,
        "pnl_rows": pnl_rows,
        "excluded_non_natural_pnl_rows": [row for row in all_pnl_rows if row not in pnl_rows],
        "sufficiency_gate_passed": False,
        "status": "separate_natural_tape_sufficiency_verifier_required",
        "reason": (
            "this receipt exposes source-bound counts and conservative mappings but does not "
            "bind the companion outcome tape or authenticated venue-accounting receipt"
        ),
        "venue_accounting_default_2_trade_1_closed_pnl_1_settlement_is_sufficient": False,
    }


def _validate_output_location(
    output_root: Path,
    *,
    source_directories: Sequence[Path],
) -> Path:
    expanded = output_root.expanduser()
    if not expanded.is_absolute():
        raise ValueError("captured-account replay output root must be absolute")
    resolved = expanded.resolve(strict=False)
    if resolved.exists():
        raise FileExistsError(f"captured-account replay output already exists: {resolved}")
    parent = resolved.parent
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise ValueError(f"captured-account replay output parent is unavailable: {parent}") from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("captured-account replay output parent must be a non-symlink directory")
    for source in source_directories:
        resolved_source = source.resolve(strict=True)
        if resolved == resolved_source or resolved_source in resolved.parents or resolved in resolved_source.parents:
            raise ValueError(f"captured-account replay output overlaps source root {resolved_source}")
    return resolved


def _validate_demo_journal_identity(
    events: Sequence[AccountEvent],
    *,
    expected_account_id: str,
) -> None:
    if not events:
        raise ValueError("natural demo account journal is empty")
    if events[0].prev_event_hash != GENESIS_HASH:
        raise ValueError("demo journal does not begin at the fresh-reset genesis boundary")
    account_ids = {event.account_id for event in events}
    if account_ids != {expected_account_id}:
        raise ValueError(f"demo journal account ids {sorted(account_ids)!r} do not match {expected_account_id!r}")


def _input_manifest_payload(
    *,
    target_path: Path,
    account_root: Path,
    capture_root: Path,
    rules_path: Path,
    policy_path: Path,
    calibration_path: Path,
    safety_capture_path: Path,
    safety_manifest_path: Path,
    freeze_manifest_path: Path,
    effective_runtime_config_bundle_path: Path,
    freeze_binding: Mapping[str, Any],
    effective_runtime_config_binding: Mapping[str, Any],
    safety_manifest: Mapping[str, Any],
    safety_journal_evidence: Mapping[str, Any],
    source_identities: Mapping[str, _FrozenFile],
    capture_events: Sequence[TargetSchedulingCaptureEvent],
    capture_tape_hash: str,
    demo_events: Sequence[AccountEvent],
    calibration_receipt: Mapping[str, Any],
    twin_config: Any,
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
    max_decision_age_ns: int,
    max_market_age_ns: int,
    max_snapshot_age_ns: int,
    latency_quantile: str,
    slippage_quantile: str,
    kernel_id_seed: str,
    twin_id_seed: str,
) -> dict[str, Any]:
    if type(t0_ns) is not int or type(t1_ns) is not int or t0_ns <= 0 or t1_ns <= t0_ns:
        raise ValueError("natural account replay input manifest requires 0 < T0 < T1")
    captured_requests = _flatten_captured_requests(
        capture_events,
        expected_account_id=expected_account_id,
    )
    outside_window = [
        event.source_event.event_id for event in capture_events if not t0_ns <= event.source_event.event_ts_ns < t1_ns
    ]
    if outside_window:
        raise ValueError("target capture contains source events outside [T0,T1)")
    request_times = [captured.request.created_ts_ns for event in capture_events for captured in event.requests]
    if any(not t0_ns <= timestamp < t1_ns for timestamp in request_times):
        raise ValueError("target capture contains durable requests outside [T0,T1)")
    _validate_demo_journal_identity(demo_events, expected_account_id=expected_account_id)
    base_identities = {
        label: identity
        for label, identity in source_identities.items()
        if label != "natural_account_replay_input_manifest"
    }
    epoch_identity = _calibration_epoch_identity(
        calibration_receipt,
        demo_events=demo_events,
        source_identities=base_identities,
        expected_account_id=expected_account_id,
    )
    twin_payload = json_safe(asdict(twin_config))
    twin_sha256 = hashlib.sha256(canonical_json(twin_payload)).hexdigest()
    payload: dict[str, Any] = {
        "schema_version": ACCOUNT_REPLAY_INPUT_MANIFEST_SCHEMA_VERSION,
        "kind": ACCOUNT_REPLAY_INPUT_MANIFEST_KIND,
        "evidence_scope": "frozen_natural_demo_account_replay_inputs",
        "natural_window": {"t0_ns": t0_ns, "t1_ns": t1_ns},
        "expected_account_id": expected_account_id,
        "source_roots": {
            "natural_cutover_freeze_manifest_path": str(freeze_manifest_path),
            "effective_runtime_config_bundle_file": str(
                effective_runtime_config_bundle_path
            ),
            "target_capture_path": str(target_path),
            "demo_account_root": str(account_root),
            "market_capture_root": str(capture_root),
            "demo_rules_file": str(rules_path),
            "risk_policy_file": str(policy_path),
            "calibration_file": str(calibration_path),
            "post_window_safety_target_capture_path": str(safety_capture_path),
            "post_window_safety_manifest_path": str(safety_manifest_path),
        },
        "source_files": _identity_payload(base_identities),
        "natural_cutover_freeze": json_safe(dict(freeze_binding)),
        "effective_runtime_config": json_safe(
            dict(effective_runtime_config_binding)
        ),
        "target_capture": {
            "capture_tape_hash": capture_tape_hash,
            "event_count": len(capture_events),
            "successful_empty_event_count": sum(not event.requests for event in capture_events),
            "durable_request_count": len(captured_requests),
        },
        "natural_epoch_identity": {
            key: value for key, value in epoch_identity.items() if key.startswith(("natural_", "shared_", "epoch_"))
        },
        "v7_training_identity": {
            key: value for key, value in epoch_identity.items() if key.startswith("v7_") or key.startswith("training_")
        },
        "post_window_safety": {
            "manifest_artifact_sha256": safety_manifest["artifact_sha256"],
            "freeze_id": safety_manifest["freeze_id"],
            "t1_ns": safety_manifest["t1_ns"],
            "namespace_prefix": safety_manifest["namespace_prefix"],
            "capture_tape_hash": safety_manifest["capture_tape_hash"],
            "capture_event_count": safety_manifest["capture_event_count"],
            "successful_empty_event_count": safety_manifest["successful_empty_event_count"],
            "request_ids": safety_manifest["request_ids"],
            "batch_ids": safety_manifest["batch_ids"],
            "journal_classification": json_safe(safety_journal_evidence),
        },
        "derived_execution_twin": twin_payload,
        "derived_execution_twin_sha256": twin_sha256,
        "replay_configuration": {
            "max_decision_age_ns": max_decision_age_ns,
            "max_market_age_ns": max_market_age_ns,
            "max_snapshot_age_ns": max_snapshot_age_ns,
            "latency_quantile": latency_quantile,
            "slippage_quantile": slippage_quantile,
            "kernel_id_seed": kernel_id_seed,
            "twin_id_seed": twin_id_seed,
        },
        "execution_authorization": "not_granted",
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _load_input_manifest_bytes(
    data: bytes,
    *,
    identity: _FrozenFile,
) -> dict[str, Any]:
    if identity.mode != 0o600:
        raise ValueError("natural account replay input manifest must be mode 0600")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError("natural account replay input manifest is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("natural account replay input manifest must be an object")
    payload = dict(value)
    if int(payload.get("schema_version") or 0) != ACCOUNT_REPLAY_INPUT_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unknown natural account replay input manifest schema")
    if payload.get("kind") != ACCOUNT_REPLAY_INPUT_MANIFEST_KIND:
        raise ValueError("unexpected natural account replay input manifest kind")
    observed = payload.get("artifact_sha256")
    if not _is_lower_sha256(observed) or observed != _self_hash(payload):
        raise ValueError("natural account replay input manifest hash mismatch")
    if payload.get("execution_authorization") != "not_granted":
        raise ValueError("natural account replay input manifest cannot grant execution authority")
    return payload


def load_natural_account_replay_input_manifest(path: str | Path) -> dict[str, Any]:
    identity, data = _read_frozen_file(
        Path(path),
        label="natural account replay input manifest",
    )
    payload = _load_input_manifest_bytes(data, identity=identity)
    roots = _required_mapping(payload.get("source_roots"), label="input source roots")
    source_files = _required_mapping(
        payload.get("source_files"), label="input source files"
    )
    bundle_source = _required_mapping(
        source_files.get("effective_runtime_config_bundle"),
        label="input effective runtime config source",
    )
    bundle_path = str(roots.get("effective_runtime_config_bundle_file") or "")
    if bundle_path != bundle_source.get("path"):
        raise ValueError("input manifest effective runtime config source path changed")
    _bundle, binding = load_effective_runtime_config_bundle_binding(bundle_path)
    if (
        binding.get("file_sha256") != bundle_source.get("sha256")
        or binding != payload.get("effective_runtime_config")
    ):
        raise ValueError("input manifest effective runtime config bundle changed")
    return payload


def _write_new_atomic_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    mode: int,
    label: str,
) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    resolved = expanded.resolve(strict=False)
    parent = _require_directory(resolved.parent, label=f"{label} parent")
    data = canonical_json(payload) + b"\n"
    created = False
    try:
        try:
            fd = os.open(
                str(resolved),
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
            )
        except FileExistsError as exc:
            raise FileExistsError(f"{label} already exists: {resolved}") from exc
        created = True
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(fd, view[offset:])
                if written <= 0:
                    raise OSError("input manifest write made no progress")
                offset += written
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if created:
            resolved.unlink(missing_ok=True)
        raise
    return resolved


def build_natural_account_replay_input_manifest(
    *,
    target_capture_path: str | Path,
    demo_account_root: str | Path,
    market_capture_root: str | Path,
    demo_rules_file: str | Path,
    risk_policy_file: str | Path,
    calibration_file: str | Path,
    freeze_manifest_path: str | Path,
    effective_runtime_config_bundle_file: str | Path,
    safety_target_capture_path: str | Path,
    safety_manifest_path: str | Path,
    expected_account_id: str,
    output_path: str | Path,
    t0_ns: int,
    t1_ns: int,
    max_decision_age_ns: int,
    latency_quantile: str = "p50",
    slippage_quantile: str = "p50",
    max_market_age_ns: int = DEFAULT_MAX_MARKET_AGE_NS,
    max_snapshot_age_ns: int = DEFAULT_MAX_SNAPSHOT_AGE_NS,
    kernel_id_seed: str = DEFAULT_KERNEL_ID_SEED,
    twin_id_seed: str = DEFAULT_TWIN_ID_SEED,
) -> Path:
    """Freeze the exact post-reset natural replay inputs into a child manifest."""

    if not expected_account_id.strip():
        raise ValueError("input manifest requires an expected account id")
    _require_registered_replay_configuration(
        max_decision_age_ns=max_decision_age_ns,
        max_market_age_ns=max_market_age_ns,
        max_snapshot_age_ns=max_snapshot_age_ns,
        latency_quantile=latency_quantile,
        slippage_quantile=slippage_quantile,
        kernel_id_seed=kernel_id_seed,
        twin_id_seed=twin_id_seed,
    )
    target_path = _require_regular_file(Path(target_capture_path), label="target capture")
    account_root = _require_directory(Path(demo_account_root), label="demo account root")
    capture_root = _require_directory(Path(market_capture_root), label="market capture root")
    rules_path = _require_regular_file(Path(demo_rules_file), label="demo rules")
    policy_path = _require_regular_file(Path(risk_policy_file), label="risk policy")
    calibration_path = _require_regular_file(Path(calibration_file), label="V7 calibration")
    freeze_path = _require_regular_file(
        Path(freeze_manifest_path),
        label="natural cutover freeze manifest",
    )
    effective_bundle_path = _require_regular_file(
        Path(effective_runtime_config_bundle_file),
        label="effective runtime config bundle",
    )
    safety_capture_path = _require_regular_file(
        Path(safety_target_capture_path),
        label="post-window safety target capture",
    )
    safety_manifest_path_resolved = _require_regular_file(
        Path(safety_manifest_path),
        label="post-window safety manifest",
    )
    _validate_source_layout(
        account_root=account_root,
        capture_root=capture_root,
        files={
            "target capture": target_path,
            "demo rules": rules_path,
            "risk policy": policy_path,
            "V7 calibration": calibration_path,
            "natural cutover freeze manifest": freeze_path,
            "effective runtime config bundle": effective_bundle_path,
            "post-window safety target capture": safety_capture_path,
            "post-window safety manifest": safety_manifest_path_resolved,
        },
    )
    source_identities, source_contents, source_snapshots = _freeze_sources(
        target_capture_path=target_path,
        demo_account_root=account_root,
        market_capture_root=capture_root,
        demo_rules_file=rules_path,
        risk_policy_file=policy_path,
        calibration_file=calibration_path,
        freeze_manifest_file=freeze_path,
        effective_runtime_config_bundle_file=effective_bundle_path,
        safety_target_capture_file=safety_capture_path,
        safety_manifest_file=safety_manifest_path_resolved,
    )
    capture_events, capture_tape_hash = load_target_scheduling_capture_bytes(
        source_contents["target_scheduling_capture"]
    )
    safety_capture_events, safety_capture_tape_hash = load_target_scheduling_capture_bytes(
        source_contents["post_window_safety_target_capture"]
    )
    safety_manifest, safety_requests = _load_post_window_safety_manifest(
        manifest_data=source_contents["post_window_safety_manifest"],
        manifest_identity=source_identities["post_window_safety_manifest"],
        capture_path=safety_capture_path,
        capture_identity=source_identities["post_window_safety_target_capture"],
        capture_events=safety_capture_events,
        capture_tape_hash=safety_capture_tape_hash,
        expected_account_id=expected_account_id,
        expected_t1_ns=t1_ns,
    )
    demo_events = _journal_from_frozen_sources(source_contents)
    safety_journal_evidence = _validate_post_window_safety_batches(
        captured_requests=safety_requests,
        demo_events=demo_events,
        t1_ns=t1_ns,
    )
    calibration_receipt = _load_calibration_receipt_bytes(
        source_contents["v7_execution_twin_calibration"],
        require_registered_requirements=True,
    )
    freeze = _load_freeze_snapshot(
        freeze_path,
        source_snapshots["natural_cutover_freeze_manifest"],
    )
    freeze_binding = _validate_natural_freeze_binding(
        freeze,
        freeze_path=freeze_path,
        account_root=account_root,
        capture_root=capture_root,
        rules_path=rules_path,
        policy_path=policy_path,
        calibration_path=calibration_path,
        calibration_receipt=calibration_receipt,
        safety_manifest=safety_manifest,
        expected_account_id=expected_account_id,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        source_snapshots=source_snapshots,
    )
    effective_runtime_config_binding = _validate_effective_runtime_binding(
        bundle_path=effective_bundle_path,
        bundle_identity=source_identities["effective_runtime_config_bundle"],
        freeze_binding=freeze_binding,
        target_capture_path=target_path,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        snapshot=source_snapshots["effective_runtime_config_bundle"],
    )
    twin_config = execution_twin_config_from_calibration(
        calibration_receipt,
        max_decision_age_ns=max_decision_age_ns,
        latency_quantile=latency_quantile,
        slippage_quantile=slippage_quantile,
        require_gate=True,
        require_registered_requirements=True,
    )
    payload = _input_manifest_payload(
        target_path=target_path,
        account_root=account_root,
        capture_root=capture_root,
        rules_path=rules_path,
        policy_path=policy_path,
        calibration_path=calibration_path,
        safety_capture_path=safety_capture_path,
        safety_manifest_path=safety_manifest_path_resolved,
        freeze_manifest_path=freeze_path,
        effective_runtime_config_bundle_path=effective_bundle_path,
        freeze_binding=freeze_binding,
        effective_runtime_config_binding=effective_runtime_config_binding,
        safety_manifest=safety_manifest,
        safety_journal_evidence=safety_journal_evidence,
        source_identities=source_identities,
        capture_events=capture_events,
        capture_tape_hash=capture_tape_hash,
        demo_events=demo_events,
        calibration_receipt=calibration_receipt,
        twin_config=twin_config,
        expected_account_id=expected_account_id,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        max_decision_age_ns=max_decision_age_ns,
        max_market_age_ns=max_market_age_ns,
        max_snapshot_age_ns=max_snapshot_age_ns,
        latency_quantile=latency_quantile,
        slippage_quantile=slippage_quantile,
        kernel_id_seed=kernel_id_seed,
        twin_id_seed=twin_id_seed,
    )
    _require_sources_unchanged(
        source_identities,
        target_capture_path=target_path,
        demo_account_root=account_root,
        market_capture_root=capture_root,
        demo_rules_file=rules_path,
        risk_policy_file=policy_path,
        calibration_file=calibration_path,
        freeze_manifest_file=freeze_path,
        effective_runtime_config_bundle_file=effective_bundle_path,
        safety_target_capture_file=safety_capture_path,
        safety_manifest_file=safety_manifest_path_resolved,
    )
    manifest_path = _write_new_atomic_json(
        Path(output_path),
        payload,
        mode=0o600,
        label="natural account replay input manifest",
    )
    if load_natural_account_replay_input_manifest(manifest_path) != payload:
        raise RuntimeError("published natural account replay input manifest changed")
    return manifest_path


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    created_ts_ns = payload.get("created_ts_ns")
    if type(created_ts_ns) is not int or created_ts_ns <= 0:
        raise ValueError("captured-account replay receipt requires a positive creation timestamp")
    data = canonical_json(payload) + b"\n"
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(fd, view[offset:])
            if written <= 0:
                raise OSError("captured-account replay receipt write made no progress")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.utime(path, ns=(created_ts_ns, created_ts_ns), follow_symlinks=False)
    os.chmod(path, 0o400)
    metadata_fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(metadata_fd)
    finally:
        os.close(metadata_fd)


def verify_captured_account_replay_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    if int(value.get("schema_version") or 0) != ACCOUNT_REPLAY_SCHEMA_VERSION:
        raise ValueError("unknown captured-account replay receipt schema")
    if value.get("kind") != ACCOUNT_REPLAY_KIND:
        raise ValueError("unexpected captured-account replay receipt kind")
    if type(value.get("created_ts_ns")) is not int or int(value["created_ts_ns"]) <= 0:
        raise ValueError("captured-account replay receipt lacks its creation timestamp")
    observed = value.get("artifact_sha256")
    if not _is_lower_sha256(observed) or observed != _self_hash(value):
        raise ValueError("captured-account replay receipt hash mismatch")
    if value.get("execution_authorization") != "not_granted":
        raise ValueError("captured-account replay receipt cannot grant execution authority")
    limitations = value.get("limitations")
    if limitations != list(_LIMITATIONS):
        raise ValueError("captured-account replay limitations changed")
    effective = value.get("effective_runtime_config")
    if (
        not isinstance(effective, Mapping)
        or effective.get("execution_authorization") != "not_granted"
    ):
        raise ValueError(
            "captured-account replay lacks its effective runtime config binding"
        )
    return value


def _read_captured_account_replay_receipt(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> tuple[_FrozenFile, bytes, dict[str, Any]]:
    identity, data = _read_frozen_file(
        Path(path),
        label="account replay receipt",
        snapshot=snapshot,
        require_single_link=True,
    )
    value = json.loads(data)
    if not isinstance(value, Mapping):
        raise ValueError("captured-account replay receipt must be an object")
    payload = verify_captured_account_replay_receipt(value)
    if identity.mode != 0o400:
        raise ValueError("captured-account replay receipt must be mode 0400")
    if snapshot is not None and snapshot.nlink != 1:
        raise ValueError("captured-account replay receipt must not be hard-linked")
    if identity.mtime_ns != payload["created_ts_ns"]:
        raise ValueError(
            "captured-account replay creation timestamp is not bound to receipt metadata"
        )
    if payload["created_ts_ns"] > time.time_ns():
        raise ValueError("captured-account replay creation timestamp is in the future")
    return identity, data, payload


def _resolve_replay_source_paths(roots: Mapping[str, Any]) -> _ReplaySourcePaths:
    expected_keys = {
        "natural_cutover_freeze_manifest_path",
        "effective_runtime_config_bundle_file",
        "target_capture_path",
        "demo_account_root",
        "market_capture_root",
        "demo_rules_file",
        "risk_policy_file",
        "calibration_file",
        "post_window_safety_target_capture_path",
        "post_window_safety_manifest_path",
        "input_manifest_path",
    }
    if set(roots) != expected_keys or any(
        type(roots.get(key)) is not str or not str(roots[key]) for key in expected_keys
    ):
        raise ValueError("captured-account replay source roots are malformed")
    paths = _ReplaySourcePaths(
        target_capture=_require_regular_file(
            Path(str(roots["target_capture_path"])), label="target capture"
        ),
        demo_account=_require_directory(
            Path(str(roots["demo_account_root"])), label="demo account root"
        ),
        market_capture=_require_directory(
            Path(str(roots["market_capture_root"])), label="market capture root"
        ),
        demo_rules=_require_regular_file(
            Path(str(roots["demo_rules_file"])), label="demo rules"
        ),
        risk_policy=_require_regular_file(
            Path(str(roots["risk_policy_file"])), label="risk policy"
        ),
        calibration=_require_regular_file(
            Path(str(roots["calibration_file"])), label="V7 calibration"
        ),
        freeze_manifest=_require_regular_file(
            Path(str(roots["natural_cutover_freeze_manifest_path"])),
            label="natural cutover freeze manifest",
        ),
        effective_runtime_config_bundle=_require_regular_file(
            Path(str(roots["effective_runtime_config_bundle_file"])),
            label="effective runtime config bundle",
        ),
        safety_target_capture=_require_regular_file(
            Path(str(roots["post_window_safety_target_capture_path"])),
            label="post-window safety target capture",
        ),
        safety_manifest=_require_regular_file(
            Path(str(roots["post_window_safety_manifest_path"])),
            label="post-window safety manifest",
        ),
        input_manifest=_require_regular_file(
            Path(str(roots["input_manifest_path"])),
            label="natural account replay input manifest",
        ),
    )
    _validate_source_layout(
        account_root=paths.demo_account,
        capture_root=paths.market_capture,
        files={
            "target capture": paths.target_capture,
            "demo rules": paths.demo_rules,
            "risk policy": paths.risk_policy,
            "V7 calibration": paths.calibration,
            "natural cutover freeze manifest": paths.freeze_manifest,
            "effective runtime config bundle": paths.effective_runtime_config_bundle,
            "post-window safety target capture": paths.safety_target_capture,
            "post-window safety manifest": paths.safety_manifest,
            "natural account replay input manifest": paths.input_manifest,
        },
    )
    return paths


def _freeze_replay_source_paths(
    paths: _ReplaySourcePaths,
) -> tuple[
    dict[str, _FrozenFile],
    dict[str, bytes],
    dict[str, StableFileSnapshot],
]:
    return _freeze_sources(
        target_capture_path=paths.target_capture,
        demo_account_root=paths.demo_account,
        market_capture_root=paths.market_capture,
        demo_rules_file=paths.demo_rules,
        risk_policy_file=paths.risk_policy,
        calibration_file=paths.calibration,
        freeze_manifest_file=paths.freeze_manifest,
        effective_runtime_config_bundle_file=paths.effective_runtime_config_bundle,
        safety_target_capture_file=paths.safety_target_capture,
        safety_manifest_file=paths.safety_manifest,
        input_manifest_file=paths.input_manifest,
    )


def _output_tree_snapshot(
    root: Path,
    *,
    label: str,
) -> tuple[list[dict[str, Any]], set[tuple[int, int]], int]:
    resolved = _require_directory(root, label=label)
    root_stat = resolved.stat()
    rows: list[dict[str, Any]] = [
        {"path": ".", "kind": "directory", "mode": stat.S_IMODE(root_stat.st_mode)}
    ]
    file_inodes: set[tuple[int, int]] = set()
    max_mtime_ns = root_stat.st_mtime_ns

    def visit(directory: Path) -> None:
        nonlocal max_mtime_ns
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError(f"{label} cannot be enumerated: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            observed = path.lstat()
            relative = path.relative_to(resolved).as_posix()
            max_mtime_ns = max(max_mtime_ns, observed.st_mtime_ns)
            if stat.S_ISLNK(observed.st_mode):
                raise ValueError(f"{label} contains a symlink: {relative}")
            if stat.S_ISDIR(observed.st_mode):
                rows.append(
                    {
                        "path": relative,
                        "kind": "directory",
                        "mode": stat.S_IMODE(observed.st_mode),
                    }
                )
                visit(path)
                continue
            if not stat.S_ISREG(observed.st_mode):
                raise ValueError(f"{label} contains a non-regular artifact: {relative}")
            identity, _data = _read_frozen_file(path, label=f"{label}/{relative}")
            current = path.stat()
            if current.st_nlink != 1:
                raise ValueError(f"{label} contains a hard-linked artifact: {relative}")
            inode = (identity.device, identity.inode)
            if inode in file_inodes:
                raise ValueError(f"{label} aliases an output inode: {relative}")
            file_inodes.add(inode)
            rows.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": identity.mode,
                    "size": identity.size,
                    "sha256": identity.sha256,
                }
            )

    visit(resolved)
    return rows, file_inodes, max_mtime_ns


def _replay_outputs_snapshot(
    historical_root: Path,
    paper_root: Path,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    historical, historical_inodes, historical_mtime = _output_tree_snapshot(
        historical_root, label="historical replay output"
    )
    paper, paper_inodes, paper_mtime = _output_tree_snapshot(
        paper_root, label="paper replay output"
    )
    if historical_inodes & paper_inodes:
        raise ValueError("historical and paper replay outputs alias file inodes")
    return {"historical": historical, "paper": paper}, max(
        historical_mtime, paper_mtime
    )


def _validate_existing_replay_output_layout(
    receipt_path: Path,
    *,
    payload: Mapping[str, Any],
    sources: _ReplaySourcePaths,
) -> tuple[Path, Path]:
    if receipt_path.name != ACCOUNT_REPLAY_RECEIPT_FILENAME:
        raise ValueError("captured-account replay receipt has an unexpected filename")
    output_root = _require_directory(receipt_path.parent, label="account replay output root")
    expected_top_level = {
        ACCOUNT_REPLAY_RECEIPT_FILENAME,
        "historical",
        "paper",
    }
    if {entry.name for entry in output_root.iterdir()} != expected_top_level:
        raise ValueError("captured-account replay output root has unexpected artifacts")
    for source in sources.source_directories():
        resolved_source = source.resolve(strict=True)
        if (
            output_root == resolved_source
            or resolved_source in output_root.parents
            or output_root in resolved_source.parents
        ):
            raise ValueError(
                f"captured-account replay output overlaps source root {resolved_source}"
            )
    historical_root = _require_directory(
        output_root / "historical", label="historical replay output"
    )
    paper_root = _require_directory(output_root / "paper", label="paper replay output")
    directory_inodes = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in (output_root, historical_root, paper_root)
    }
    if len(directory_inodes) != 3:
        raise ValueError("captured-account replay output directories alias one another")
    outputs = _required_mapping(payload.get("outputs"), label="replay outputs")
    if outputs.get("historical_root") != str(historical_root) or outputs.get(
        "paper_root"
    ) != str(paper_root):
        raise ValueError("captured-account replay output roots do not match receipt location")
    return historical_root, paper_root


def _strict_replay_configuration(
    input_manifest: Mapping[str, Any],
) -> tuple[str, int, int, int, str, str, str, str]:
    account_id = input_manifest.get("expected_account_id")
    config = _required_mapping(
        input_manifest.get("replay_configuration"), label="input replay configuration"
    )
    max_decision_age_ns = config.get("max_decision_age_ns")
    max_market_age_ns = config.get("max_market_age_ns")
    max_snapshot_age_ns = config.get("max_snapshot_age_ns")
    latency_quantile = config.get("latency_quantile")
    slippage_quantile = config.get("slippage_quantile")
    kernel_id_seed = config.get("kernel_id_seed")
    twin_id_seed = config.get("twin_id_seed")
    if type(account_id) is not str or not account_id:
        raise ValueError("input manifest lacks its expected account id")
    if any(
        type(value) is not int or value <= 0
        for value in (max_decision_age_ns, max_market_age_ns, max_snapshot_age_ns)
    ):
        raise ValueError("input manifest replay age limits are malformed")
    if any(
        type(value) is not str or not value
        for value in (latency_quantile, slippage_quantile, kernel_id_seed, twin_id_seed)
    ):
        raise ValueError("input manifest replay configuration is malformed")
    _require_registered_replay_configuration(
        max_decision_age_ns=cast(int, max_decision_age_ns),
        max_market_age_ns=cast(int, max_market_age_ns),
        max_snapshot_age_ns=cast(int, max_snapshot_age_ns),
        latency_quantile=cast(str, latency_quantile),
        slippage_quantile=cast(str, slippage_quantile),
        kernel_id_seed=cast(str, kernel_id_seed),
        twin_id_seed=cast(str, twin_id_seed),
    )
    return (
        account_id,
        cast(int, max_decision_age_ns),
        cast(int, max_market_age_ns),
        cast(int, max_snapshot_age_ns),
        cast(str, latency_quantile),
        cast(str, slippage_quantile),
        cast(str, kernel_id_seed),
        cast(str, twin_id_seed),
    )


def load_captured_account_replay_receipt(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Source-reopen and deterministically reproduce a published replay receipt."""

    receipt_identity, receipt_data, payload = _read_captured_account_replay_receipt(
        path,
        snapshot=snapshot,
    )
    receipt_path = Path(receipt_identity.path)
    roots = _required_mapping(payload.get("source_roots"), label="replay source roots")
    sources = _resolve_replay_source_paths(roots)
    source_files = _required_mapping(
        payload.get("source_files"), label="replay source files"
    )
    source_identities, source_contents, _source_snapshots = (
        _freeze_replay_source_paths(sources)
    )
    if _identity_payload(source_identities) != source_files:
        raise ValueError("captured-account replay bound source files changed")
    input_manifest = _load_input_manifest_bytes(
        source_contents["natural_account_replay_input_manifest"],
        identity=source_identities["natural_account_replay_input_manifest"],
    )
    (
        expected_account_id,
        max_decision_age_ns,
        max_market_age_ns,
        max_snapshot_age_ns,
        latency_quantile,
        slippage_quantile,
        kernel_id_seed,
        twin_id_seed,
    ) = _strict_replay_configuration(input_manifest)
    historical_root, paper_root = _validate_existing_replay_output_layout(
        receipt_path,
        payload=payload,
        sources=sources,
    )
    output_snapshot_before, latest_output_mtime_ns = _replay_outputs_snapshot(
        historical_root, paper_root
    )
    if payload["created_ts_ns"] < latest_output_mtime_ns:
        raise ValueError("captured-account replay receipt predates its modeled outputs")

    # The receipt claims exact batch results and exact modeled event bytes. Those
    # cannot be recovered independently from the receipt, so reproduce them in a
    # private temporary root and remove it automatically after comparison.
    with tempfile.TemporaryDirectory(prefix="lm-captured-account-reverify-") as temporary:
        temporary_path = Path(temporary)
        temporary_path.chmod(0o700)
        regenerated = _run_captured_account_replay(
            target_capture_path=sources.target_capture,
            demo_account_root=sources.demo_account,
            market_capture_root=sources.market_capture,
            demo_rules_file=sources.demo_rules,
            risk_policy_file=sources.risk_policy,
            calibration_file=sources.calibration,
            freeze_manifest_path=sources.freeze_manifest,
            effective_runtime_config_bundle_file=sources.effective_runtime_config_bundle,
            safety_target_capture_path=sources.safety_target_capture,
            safety_manifest_path=sources.safety_manifest,
            input_manifest_path=sources.input_manifest,
            expected_account_id=expected_account_id,
            output_root=temporary_path / "replay",
            max_decision_age_ns=max_decision_age_ns,
            latency_quantile=latency_quantile,
            slippage_quantile=slippage_quantile,
            max_market_age_ns=max_market_age_ns,
            max_snapshot_age_ns=max_snapshot_age_ns,
            kernel_id_seed=kernel_id_seed,
            twin_id_seed=twin_id_seed,
            source_reverify=False,
        )
        regenerated_outputs = _required_mapping(
            regenerated.payload.get("outputs"), label="regenerated replay outputs"
        )
        regenerated_snapshot, _regenerated_mtime = _replay_outputs_snapshot(
            Path(str(regenerated_outputs["historical_root"])),
            Path(str(regenerated_outputs["paper_root"])),
        )
        if regenerated_snapshot != output_snapshot_before:
            raise ValueError("captured-account replay modeled outputs changed")
        expected_payload = dict(regenerated.payload)
        expected_outputs = dict(regenerated_outputs)
        expected_outputs["historical_root"] = str(historical_root)
        expected_outputs["paper_root"] = str(paper_root)
        expected_payload["outputs"] = expected_outputs
        expected_payload["created_ts_ns"] = payload["created_ts_ns"]
        expected_payload["artifact_sha256"] = ""
        expected_payload["artifact_sha256"] = _self_hash(expected_payload)
        if canonical_json(expected_payload) != canonical_json(payload):
            raise ValueError("captured-account replay receipt claims do not reproduce")

    output_snapshot_after, _latest_output_mtime_ns = _replay_outputs_snapshot(
        historical_root, paper_root
    )
    if output_snapshot_after != output_snapshot_before:
        raise RuntimeError("captured-account replay outputs changed during verification")
    _require_sources_unchanged(
        source_identities,
        target_capture_path=sources.target_capture,
        demo_account_root=sources.demo_account,
        market_capture_root=sources.market_capture,
        demo_rules_file=sources.demo_rules,
        risk_policy_file=sources.risk_policy,
        calibration_file=sources.calibration,
        freeze_manifest_file=sources.freeze_manifest,
        effective_runtime_config_bundle_file=sources.effective_runtime_config_bundle,
        safety_target_capture_file=sources.safety_target_capture,
        safety_manifest_file=sources.safety_manifest,
        input_manifest_file=sources.input_manifest,
    )
    final_identity, final_data, final_payload = _read_captured_account_replay_receipt(
        receipt_path
    )
    if final_identity != receipt_identity or final_data != receipt_data or final_payload != payload:
        raise RuntimeError("captured-account replay receipt changed during verification")
    return payload


def _run_captured_account_replay(
    *,
    target_capture_path: str | Path,
    demo_account_root: str | Path,
    market_capture_root: str | Path,
    demo_rules_file: str | Path,
    risk_policy_file: str | Path,
    calibration_file: str | Path,
    freeze_manifest_path: str | Path,
    effective_runtime_config_bundle_file: str | Path,
    safety_target_capture_path: str | Path,
    safety_manifest_path: str | Path,
    input_manifest_path: str | Path,
    expected_account_id: str,
    output_root: str | Path,
    max_decision_age_ns: int,
    latency_quantile: str = "p50",
    slippage_quantile: str = "p50",
    max_market_age_ns: int = DEFAULT_MAX_MARKET_AGE_NS,
    max_snapshot_age_ns: int = DEFAULT_MAX_SNAPSHOT_AGE_NS,
    kernel_id_seed: str = DEFAULT_KERNEL_ID_SEED,
    twin_id_seed: str = DEFAULT_TWIN_ID_SEED,
    source_reverify: bool,
) -> CapturedAccountReplayReceipt:
    """Build two immutable local account replays from one frozen demo epoch."""

    if not expected_account_id.strip():
        raise ValueError("captured-account replay requires an expected account id")
    _require_registered_replay_configuration(
        max_decision_age_ns=max_decision_age_ns,
        max_market_age_ns=max_market_age_ns,
        max_snapshot_age_ns=max_snapshot_age_ns,
        latency_quantile=latency_quantile,
        slippage_quantile=slippage_quantile,
        kernel_id_seed=kernel_id_seed,
        twin_id_seed=twin_id_seed,
    )
    target_path = _require_regular_file(Path(target_capture_path), label="target capture")
    account_root = _require_directory(Path(demo_account_root), label="demo account root")
    capture_root = _require_directory(Path(market_capture_root), label="market capture root")
    rules_path = _require_regular_file(Path(demo_rules_file), label="demo rules")
    policy_path = _require_regular_file(Path(risk_policy_file), label="risk policy")
    calibration_path = _require_regular_file(Path(calibration_file), label="V7 calibration")
    freeze_path = _require_regular_file(
        Path(freeze_manifest_path),
        label="natural cutover freeze manifest",
    )
    effective_bundle_path = _require_regular_file(
        Path(effective_runtime_config_bundle_file),
        label="effective runtime config bundle",
    )
    safety_capture_path = _require_regular_file(
        Path(safety_target_capture_path),
        label="post-window safety target capture",
    )
    safety_manifest_path_resolved = _require_regular_file(
        Path(safety_manifest_path),
        label="post-window safety manifest",
    )
    input_manifest_path_resolved = _require_regular_file(
        Path(input_manifest_path),
        label="natural account replay input manifest",
    )
    _validate_source_layout(
        account_root=account_root,
        capture_root=capture_root,
        files={
            "target capture": target_path,
            "demo rules": rules_path,
            "risk policy": policy_path,
            "V7 calibration": calibration_path,
            "natural cutover freeze manifest": freeze_path,
            "effective runtime config bundle": effective_bundle_path,
            "post-window safety target capture": safety_capture_path,
            "post-window safety manifest": safety_manifest_path_resolved,
            "natural account replay input manifest": input_manifest_path_resolved,
        },
    )
    source_directories = tuple(
        {
            target_path.parent,
            account_root,
            capture_root,
            rules_path.parent,
            policy_path.parent,
            calibration_path.parent,
            freeze_path.parent,
            effective_bundle_path.parent,
            safety_capture_path.parent,
            safety_manifest_path_resolved.parent,
            input_manifest_path_resolved.parent,
        }
    )
    destination = _validate_output_location(
        Path(output_root),
        source_directories=source_directories,
    )
    source_identities, source_contents, source_snapshots = _freeze_sources(
        target_capture_path=target_path,
        demo_account_root=account_root,
        market_capture_root=capture_root,
        demo_rules_file=rules_path,
        risk_policy_file=policy_path,
        calibration_file=calibration_path,
        freeze_manifest_file=freeze_path,
        effective_runtime_config_bundle_file=effective_bundle_path,
        safety_target_capture_file=safety_capture_path,
        safety_manifest_file=safety_manifest_path_resolved,
        input_manifest_file=input_manifest_path_resolved,
    )
    input_manifest = _load_input_manifest_bytes(
        source_contents["natural_account_replay_input_manifest"],
        identity=source_identities["natural_account_replay_input_manifest"],
    )
    raw_window = input_manifest.get("natural_window")
    if (
        not isinstance(raw_window, Mapping)
        or type(raw_window.get("t0_ns")) is not int
        or type(raw_window.get("t1_ns")) is not int
    ):
        raise ValueError("natural account replay input manifest lacks exact T0/T1")
    capture_events, capture_tape_hash = load_target_scheduling_capture_bytes(
        source_contents["target_scheduling_capture"]
    )
    safety_capture_events, safety_capture_tape_hash = load_target_scheduling_capture_bytes(
        source_contents["post_window_safety_target_capture"]
    )
    safety_manifest, safety_requests = _load_post_window_safety_manifest(
        manifest_data=source_contents["post_window_safety_manifest"],
        manifest_identity=source_identities["post_window_safety_manifest"],
        capture_path=safety_capture_path,
        capture_identity=source_identities["post_window_safety_target_capture"],
        capture_events=safety_capture_events,
        capture_tape_hash=safety_capture_tape_hash,
        expected_account_id=expected_account_id,
        expected_t1_ns=int(raw_window["t1_ns"]),
    )
    captured_requests = _flatten_captured_requests(
        capture_events,
        expected_account_id=expected_account_id,
    )
    demo_events = _journal_from_frozen_sources(source_contents)
    _validate_demo_journal_identity(demo_events, expected_account_id=expected_account_id)
    safety_journal_evidence = _validate_post_window_safety_batches(
        captured_requests=safety_requests,
        demo_events=demo_events,
        t1_ns=int(raw_window["t1_ns"]),
    )
    capture_records, raw_record_count = _parse_market_capture(
        source_identities,
        source_contents,
    )
    instrument_rules = load_demo_rules_bytes(source_contents["demo_rules"])
    risk_policy = load_risk_policy_bytes(source_contents["risk_policy"])
    calibration_receipt = _load_calibration_receipt_bytes(
        source_contents["v7_execution_twin_calibration"],
        require_registered_requirements=True,
    )
    freeze = _load_freeze_snapshot(
        freeze_path,
        source_snapshots["natural_cutover_freeze_manifest"],
    )
    freeze_binding = _validate_natural_freeze_binding(
        freeze,
        freeze_path=freeze_path,
        account_root=account_root,
        capture_root=capture_root,
        rules_path=rules_path,
        policy_path=policy_path,
        calibration_path=calibration_path,
        calibration_receipt=calibration_receipt,
        safety_manifest=safety_manifest,
        expected_account_id=expected_account_id,
        t0_ns=int(raw_window["t0_ns"]),
        t1_ns=int(raw_window["t1_ns"]),
        source_snapshots=source_snapshots,
    )
    effective_runtime_config_binding = _validate_effective_runtime_binding(
        bundle_path=effective_bundle_path,
        bundle_identity=source_identities["effective_runtime_config_bundle"],
        freeze_binding=freeze_binding,
        target_capture_path=target_path,
        t0_ns=int(raw_window["t0_ns"]),
        t1_ns=int(raw_window["t1_ns"]),
        snapshot=source_snapshots["effective_runtime_config_bundle"],
    )
    twin_config = execution_twin_config_from_calibration(
        calibration_receipt,
        max_decision_age_ns=max_decision_age_ns,
        latency_quantile=latency_quantile,
        slippage_quantile=slippage_quantile,
        require_gate=True,
        require_registered_requirements=True,
    )
    expected_input_manifest = _input_manifest_payload(
        target_path=target_path,
        account_root=account_root,
        capture_root=capture_root,
        rules_path=rules_path,
        policy_path=policy_path,
        calibration_path=calibration_path,
        safety_capture_path=safety_capture_path,
        safety_manifest_path=safety_manifest_path_resolved,
        freeze_manifest_path=freeze_path,
        effective_runtime_config_bundle_path=effective_bundle_path,
        freeze_binding=freeze_binding,
        effective_runtime_config_binding=effective_runtime_config_binding,
        safety_manifest=safety_manifest,
        safety_journal_evidence=safety_journal_evidence,
        source_identities=source_identities,
        capture_events=capture_events,
        capture_tape_hash=capture_tape_hash,
        demo_events=demo_events,
        calibration_receipt=calibration_receipt,
        twin_config=twin_config,
        expected_account_id=expected_account_id,
        t0_ns=int(raw_window["t0_ns"]),
        t1_ns=int(raw_window["t1_ns"]),
        max_decision_age_ns=max_decision_age_ns,
        max_market_age_ns=max_market_age_ns,
        max_snapshot_age_ns=max_snapshot_age_ns,
        latency_quantile=latency_quantile,
        slippage_quantile=slippage_quantile,
        kernel_id_seed=kernel_id_seed,
        twin_id_seed=twin_id_seed,
    )
    if canonical_json(input_manifest) != canonical_json(expected_input_manifest):
        raise ValueError("natural account replay input manifest does not match the exact frozen inputs")
    natural_tape_sufficiency = _natural_tape_sufficiency_evidence(
        capture_events=capture_events,
        demo_events=demo_events,
        t0_ns=int(raw_window["t0_ns"]),
        t1_ns=int(raw_window["t1_ns"]),
    )
    mapped, journal_window, mapping_rows = _map_batches(
        captured_requests=captured_requests,
        demo_events=demo_events,
        capture_records=capture_records,
        risk_policy=risk_policy,
        instrument_rules=instrument_rules,
        max_market_age_ns=max_market_age_ns,
        max_snapshot_age_ns=max_snapshot_age_ns,
        safety_batch_ids=frozenset(captured.request.batch_id for captured in safety_requests),
    )
    cycles = _make_cycles(mapped)
    ordered_batch_ids = [cycle.batch_id for cycle in cycles]
    stage = destination.parent / f".{destination.name}.staging-{os.getpid()}-{uuid.uuid4().hex}"
    if stage.exists():
        raise FileExistsError(f"captured-account replay staging path already exists: {stage}")
    stage.mkdir(mode=0o700)
    try:
        historical_root = stage / "historical"
        paper_root = stage / "paper"
        historical_root.mkdir(mode=0o700)
        paper_root.mkdir(mode=0o700)
        historical_result = HistoricalAccountReplay(
            historical_root,
            account_id=expected_account_id,
            risk_policy=risk_policy,
            instrument_rules=instrument_rules,
            execution_config=twin_config,
            id_seed=kernel_id_seed,
            execution_id_seed=twin_id_seed,
        ).run(cycles)
        paper_result = HistoricalAccountReplay(
            paper_root,
            account_id=expected_account_id,
            risk_policy=risk_policy,
            instrument_rules=instrument_rules,
            execution_config=twin_config,
            id_seed=kernel_id_seed,
            execution_id_seed=twin_id_seed,
        ).run(cycles)
        historical_events = read_account_journal(historical_root, verify=True)
        paper_events = read_account_journal(paper_root, verify=True)
        historical_paper_exact = (
            [event.to_dict() for event in historical_events] == [event.to_dict() for event in paper_events]
            and _batch_summaries(historical_result) == _batch_summaries(paper_result)
            and historical_result.event_tape_hash == paper_result.event_tape_hash
        )
        if ordered_batch_ids:
            kernel_parity = compare_kernel_journals(
                {
                    "historical": historical_events,
                    "paper": paper_events,
                    "demo": demo_events,
                },
                comparison_batch_ids=ordered_batch_ids,
            )
            kernel_parity_payload: Mapping[str, Any] = json_safe(asdict(kernel_parity))
            demo_plan_parity = kernel_parity.passed
            exact_plan_match, plan_rows = _exact_plan_comparison(
                demo_events,
                historical_events,
                paper_events,
                ordered_batch_ids=ordered_batch_ids,
            )
        else:
            kernel_parity_payload = {
                "status": "not_run_no_durable_request_batches",
                "passed": False,
                "scoped_batch_ids": [],
            }
            demo_plan_parity = False
            exact_plan_match = False
            plan_rows = []
        final_historical_root = destination / "historical"
        final_paper_root = destination / "paper"
        receipt_payload: dict[str, Any] = {
            "schema_version": ACCOUNT_REPLAY_SCHEMA_VERSION,
            "kind": ACCOUNT_REPLAY_KIND,
            "created_ts_ns": time.time_ns(),
            "evidence_scope": "captured_demo_account_kernel_and_execution_twin_replay",
            "source_roots": {
                "natural_cutover_freeze_manifest_path": str(freeze_path),
                "effective_runtime_config_bundle_file": str(
                    effective_bundle_path
                ),
                "target_capture_path": str(target_path),
                "demo_account_root": str(account_root),
                "market_capture_root": str(capture_root),
                "demo_rules_file": str(rules_path),
                "risk_policy_file": str(policy_path),
                "calibration_file": str(calibration_path),
                "post_window_safety_target_capture_path": str(safety_capture_path),
                "post_window_safety_manifest_path": str(safety_manifest_path_resolved),
                "input_manifest_path": str(input_manifest_path_resolved),
            },
            "source_files": _identity_payload(source_identities),
            "natural_cutover_freeze": json_safe(dict(freeze_binding)),
            "effective_runtime_config": json_safe(
                dict(effective_runtime_config_binding)
            ),
            "input_manifest": {
                "artifact_sha256": input_manifest["artifact_sha256"],
                "natural_window": input_manifest["natural_window"],
                "natural_epoch_identity": input_manifest["natural_epoch_identity"],
                "v7_training_identity": input_manifest["v7_training_identity"],
                "derived_execution_twin_sha256": input_manifest["derived_execution_twin_sha256"],
            },
            "target_capture": {
                "capture_tape_hash": capture_tape_hash,
                "event_count": len(capture_events),
                "successful_empty_event_count": sum(not event.requests for event in capture_events),
                "durable_request_count": len(captured_requests),
            },
            "post_window_safety": {
                "manifest_artifact_sha256": safety_manifest["artifact_sha256"],
                "freeze_id": safety_manifest["freeze_id"],
                "capture_tape_hash": safety_capture_tape_hash,
                "capture_event_count": len(safety_capture_events),
                "durable_request_count": len(safety_requests),
                "journal_classification": safety_journal_evidence,
                "excluded_from_natural_replay_parity_and_lifecycle_floors": True,
                "included_in_final_venue_accounting_and_flatness": True,
            },
            "demo_journal_window": journal_window,
            "demo_event_counts": _event_counts(demo_events),
            "raw_market_record_count": raw_record_count,
            "request_batch_mappings": mapping_rows,
            "ordered_batch_ids": ordered_batch_ids,
            "config": {
                "risk_policy": asdict(risk_policy),
                "instrument_rules": {symbol: asdict(rule) for symbol, rule in sorted(instrument_rules.items())},
                "execution_twin": asdict(twin_config),
                "execution_twin_calibration_artifact_sha256": calibration_receipt["artifact_sha256"],
                "max_market_age_ns": max_market_age_ns,
                "max_snapshot_age_ns": max_snapshot_age_ns,
                "max_decision_age_ns": max_decision_age_ns,
                "latency_quantile": latency_quantile,
                "slippage_quantile": slippage_quantile,
                "kernel_id_seed": kernel_id_seed,
                "twin_id_seed": twin_id_seed,
            },
            "normalization": {
                "historical_paper_outcome": "exact canonical account events, batch summaries, and strategy-event tape hash",
                "kernel_plan_parity": "normalized account-kernel strategy-to-order-plan contract",
                "exact_preexecution_diagnostic_event_types": sorted(_PLAN_EVENT_TYPES),
                "exact_preexecution_diagnostic_included_fields": [
                    "event_type",
                    "correlation_id",
                    "causation_id",
                    "account_id",
                    "sleeve",
                    "symbol",
                    "wall_ts_ns",
                    "monotonic_ns",
                    "payload",
                ],
                "exact_preexecution_diagnostic_excluded_fields": [
                    "sequence",
                    "event_id",
                    "prev_event_hash",
                    "state_hash",
                    "event_hash",
                ],
                "exact_preexecution_diagnostic_quantity_tolerance": 0.0,
                "kernel_plan_quantity_tolerance": kernel_parity_payload.get("quantity_abs_tolerance"),
            },
            "outputs": {
                "historical_root": str(final_historical_root),
                "paper_root": str(final_paper_root),
                "historical_account_journal_sha256": _journal_sha256(historical_events),
                "paper_account_journal_sha256": _journal_sha256(paper_events),
                "historical_final_state_hash": historical_result.final_state_hash,
                "paper_final_state_hash": paper_result.final_state_hash,
                "historical_strategy_event_tape_hash": historical_result.event_tape_hash,
                "paper_strategy_event_tape_hash": paper_result.event_tape_hash,
                "historical_batch_summaries": _batch_summaries(historical_result),
                "paper_batch_summaries": _batch_summaries(paper_result),
            },
            "historical_paper_exact_outcome_passed": historical_paper_exact,
            "demo_plan_parity_passed": demo_plan_parity,
            "kernel_parity_report": kernel_parity_payload,
            "exact_preexecution_plan_match": exact_plan_match,
            "exact_preexecution_plan_batch_results": plan_rows,
            "natural_tape_sufficiency": natural_tape_sufficiency,
            "has_durable_request_batches": bool(ordered_batch_ids),
            "scheduling_parity_claim": "separate_artifact_required",
            "actual_demo_execution_evidence_claim": "separate_actual_venue_gate_required",
            "execution_authorization": "not_granted",
            "limitations": list(_LIMITATIONS),
            "artifact_sha256": "",
        }
        receipt_payload["artifact_sha256"] = _self_hash(receipt_payload)
        verify_captured_account_replay_receipt(receipt_payload)
        _write_receipt(stage / ACCOUNT_REPLAY_RECEIPT_FILENAME, receipt_payload)
        _require_sources_unchanged(
            source_identities,
            target_capture_path=target_path,
            demo_account_root=account_root,
            market_capture_root=capture_root,
            demo_rules_file=rules_path,
            risk_policy_file=policy_path,
            calibration_file=calibration_path,
            freeze_manifest_file=freeze_path,
            effective_runtime_config_bundle_file=effective_bundle_path,
            safety_target_capture_file=safety_capture_path,
            safety_manifest_file=safety_manifest_path_resolved,
            input_manifest_file=input_manifest_path_resolved,
        )
        stage_fd = os.open(str(stage), os.O_RDONLY)
        try:
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        rename_noreplace(
            stage,
            destination,
            label="captured-account replay output",
        )
        directory_fd = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    final_receipt_path = destination / ACCOUNT_REPLAY_RECEIPT_FILENAME
    if source_reverify:
        loaded = load_captured_account_replay_receipt(final_receipt_path)
    else:
        _identity, _data, loaded = _read_captured_account_replay_receipt(
            final_receipt_path
        )
    return CapturedAccountReplayReceipt(final_receipt_path, loaded)


def run_captured_account_replay(
    *,
    target_capture_path: str | Path,
    demo_account_root: str | Path,
    market_capture_root: str | Path,
    demo_rules_file: str | Path,
    risk_policy_file: str | Path,
    calibration_file: str | Path,
    freeze_manifest_path: str | Path,
    effective_runtime_config_bundle_file: str | Path,
    safety_target_capture_path: str | Path,
    safety_manifest_path: str | Path,
    input_manifest_path: str | Path,
    expected_account_id: str,
    output_root: str | Path,
    max_decision_age_ns: int,
    latency_quantile: str = "p50",
    slippage_quantile: str = "p50",
    max_market_age_ns: int = DEFAULT_MAX_MARKET_AGE_NS,
    max_snapshot_age_ns: int = DEFAULT_MAX_SNAPSHOT_AGE_NS,
    kernel_id_seed: str = DEFAULT_KERNEL_ID_SEED,
    twin_id_seed: str = DEFAULT_TWIN_ID_SEED,
) -> CapturedAccountReplayReceipt:
    """Build and source-reverify two immutable replays from one demo epoch."""

    return _run_captured_account_replay(
        target_capture_path=target_capture_path,
        demo_account_root=demo_account_root,
        market_capture_root=market_capture_root,
        demo_rules_file=demo_rules_file,
        risk_policy_file=risk_policy_file,
        calibration_file=calibration_file,
        freeze_manifest_path=freeze_manifest_path,
        effective_runtime_config_bundle_file=effective_runtime_config_bundle_file,
        safety_target_capture_path=safety_target_capture_path,
        safety_manifest_path=safety_manifest_path,
        input_manifest_path=input_manifest_path,
        expected_account_id=expected_account_id,
        output_root=output_root,
        max_decision_age_ns=max_decision_age_ns,
        latency_quantile=latency_quantile,
        slippage_quantile=slippage_quantile,
        max_market_age_ns=max_market_age_ns,
        max_snapshot_age_ns=max_snapshot_age_ns,
        kernel_id_seed=kernel_id_seed,
        twin_id_seed=twin_id_seed,
        source_reverify=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay frozen demo request/journal/raw-capture evidence into isolated roots"
    )
    parser.add_argument("--target-capture", required=True)
    parser.add_argument("--demo-account-root", required=True)
    parser.add_argument("--market-capture-root", required=True)
    parser.add_argument("--demo-rules-file", required=True)
    parser.add_argument("--risk-policy-file", required=True)
    parser.add_argument("--calibration-file", required=True)
    parser.add_argument("--freeze-manifest", required=True)
    parser.add_argument("--effective-runtime-config-bundle", required=True)
    parser.add_argument("--safety-target-capture", required=True)
    parser.add_argument("--safety-manifest", required=True)
    parser.add_argument("--input-manifest")
    parser.add_argument("--build-input-manifest")
    parser.add_argument("--t0-ns", type=int)
    parser.add_argument("--t1-ns", type=int)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--output-root")
    parser.add_argument("--max-decision-age-ms", type=float, default=250.0)
    parser.add_argument("--max-market-age-ms", type=float, default=5_000.0)
    parser.add_argument("--max-snapshot-age-ms", type=float, default=5_000.0)
    parser.add_argument("--latency-quantile", choices=("p50", "p75", "p95", "p99"), default="p50")
    parser.add_argument("--slippage-quantile", choices=("p50", "p75", "p95", "p99"), default="p50")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.build_input_manifest:
        if args.input_manifest or args.output_root:
            parser.error("--build-input-manifest cannot be combined with --input-manifest or --output-root")
        if args.t0_ns is None or args.t1_ns is None:
            parser.error("--build-input-manifest requires --t0-ns and --t1-ns")
    else:
        if not args.input_manifest or not args.output_root:
            parser.error("replay requires --input-manifest and --output-root")
        if args.t0_ns is not None or args.t1_ns is not None:
            parser.error("T0/T1 come from the immutable --input-manifest during replay")
    try:
        if args.build_input_manifest:
            path = build_natural_account_replay_input_manifest(
                target_capture_path=args.target_capture,
                demo_account_root=args.demo_account_root,
                market_capture_root=args.market_capture_root,
                demo_rules_file=args.demo_rules_file,
                risk_policy_file=args.risk_policy_file,
                calibration_file=args.calibration_file,
                freeze_manifest_path=args.freeze_manifest,
                effective_runtime_config_bundle_file=args.effective_runtime_config_bundle,
                safety_target_capture_path=args.safety_target_capture,
                safety_manifest_path=args.safety_manifest,
                expected_account_id=args.expected_account_id,
                output_path=args.build_input_manifest,
                t0_ns=cast(int, args.t0_ns),
                t1_ns=cast(int, args.t1_ns),
                max_decision_age_ns=int(args.max_decision_age_ms * 1_000_000),
                max_market_age_ns=int(args.max_market_age_ms * 1_000_000),
                max_snapshot_age_ns=int(args.max_snapshot_age_ms * 1_000_000),
                latency_quantile=args.latency_quantile,
                slippage_quantile=args.slippage_quantile,
            )
            print(json.dumps({"input_manifest": str(path)}, sort_keys=True))
            return 0
        receipt = run_captured_account_replay(
            target_capture_path=args.target_capture,
            demo_account_root=args.demo_account_root,
            market_capture_root=args.market_capture_root,
            demo_rules_file=args.demo_rules_file,
            risk_policy_file=args.risk_policy_file,
            calibration_file=args.calibration_file,
            freeze_manifest_path=args.freeze_manifest,
            effective_runtime_config_bundle_file=args.effective_runtime_config_bundle,
            safety_target_capture_path=args.safety_target_capture,
            safety_manifest_path=args.safety_manifest,
            input_manifest_path=args.input_manifest,
            expected_account_id=args.expected_account_id,
            output_root=args.output_root,
            max_decision_age_ns=int(args.max_decision_age_ms * 1_000_000),
            max_market_age_ns=int(args.max_market_age_ms * 1_000_000),
            max_snapshot_age_ns=int(args.max_snapshot_age_ms * 1_000_000),
            latency_quantile=args.latency_quantile,
            slippage_quantile=args.slippage_quantile,
        )
    except Exception as exc:  # noqa: BLE001 - concise command-line failure boundary
        print(f"captured-account replay failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "receipt": str(receipt.path),
                "historical_paper_exact_outcome_passed": receipt.payload["historical_paper_exact_outcome_passed"],
                "demo_plan_parity_passed": receipt.payload["demo_plan_parity_passed"],
                "has_durable_request_batches": receipt.payload["has_durable_request_batches"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
