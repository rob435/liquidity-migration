"""Strict internal re-execution checks for strategy-overhaul Phase-0 bundles.

The Phase-0 receipt is an untrusted index, not evidence by itself.  This module
rehashes every bundled byte, reconstructs the current source/config/environment
identities, re-runs the outcome-blind manifest and instrument-map scans, and
requires every derived artifact to equal the bundled payload exactly.  This is
not source authentication: Git objects, unsigned receipts, persisted provenance
labels, the selected module-byte environment manifest, and external map files
remain trust inputs with explicit blockers.

No OHLCV, residual-momentum, return, excursion, label, or PnL value is read by
this verifier.  The only parquet values materialised are the identity and
provenance columns permitted by :mod:`strategy_overhaul_phase0`.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from liquidity_migration.strategy_overhaul_phase0 import (
    REGISTERED_SLEEVE_WINDOWS,
    build_phase0_artifacts,
    canonicalize_phase0_roots,
)


PHASE0_BUNDLE_RECEIPT_SCHEMA_VERSION = 1
PHASE0_BUNDLE_RECEIPT_TYPE = "strategy_overhaul_phase0_bundle"
_HASH_CHUNK_BYTES = 1024 * 1024
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "receipt_type",
        "phase0_id",
        "identity_sha256",
        "inventory_sha256",
        "readiness_status",
        "outcome_run_authorized",
        "files",
    }
)
_JSON_ARTIFACTS = frozenset(
    {
        "command_plan.json",
        "identity.json",
        "phase0_inventory.json",
        "field_availability.json",
        "pit_provenance.json",
        "manifest_kline_coverage.json",
        "rmom_population_coverage.json",
        "root_lineage.json",
        "resource_estimate.json",
        "proposed_schemas.json",
        "child_schema_registry.json",
        "instrument_map_coverage.json",
        "outcome_blind_audit.json",
        "registered_child_designs.json",
        "support_design_and_counts.json",
        "s01_template_input_status.json",
        "environment_manifest.json",
        "source_snapshot.json",
        "untracked_sources_manifest.json",
        "instrument_map_input.json",
        "config_artifact_index.json",
        "continuous_canonical_config.json",
        "continuous_registered_scope.json",
        "continuous_config_identity.json",
        "continuous_component_config.json",
        "long_canonical_config.json",
        "long_registered_scope.json",
        "long_config_identity.json",
        "s02_config_parity_manifest.json",
    }
)
_BINARY_ARTIFACTS = frozenset({"tracked_worktree.patch", "untracked_sources.tar"})
_EXPECTED_ARTIFACTS = _JSON_ARTIFACTS | _BINARY_ARTIFACTS
_INVENTORY_PROJECTIONS = MappingProxyType(
    {
        "field_availability.json": "field_availability",
        "pit_provenance.json": "pit_provenance",
        "manifest_kline_coverage.json": "manifest_kline_coverage",
        "rmom_population_coverage.json": "rmom_population_coverage",
        "root_lineage.json": "root_lineage",
        "resource_estimate.json": "resource_estimate",
        "proposed_schemas.json": "proposed_schemas",
        "child_schema_registry.json": "child_schema_registry",
        "instrument_map_coverage.json": "instrument_map_coverage",
        "outcome_blind_audit.json": "outcome_blind_audit",
    }
)


class Phase0BundleVerificationError(RuntimeError):
    """A Phase-0 byte, schema, identity, runtime, or derivation check failed."""


@dataclass(frozen=True, slots=True)
class Phase0BundleVerification:
    """Internally re-executed Phase-0 state and its unresolved trust boundaries."""

    receipt: Mapping[str, Any]
    receipt_sha256: str
    phase0_id: str
    identity: Mapping[str, Any]
    inventory: Mapping[str, Any]
    command_plan: Mapping[str, Any]
    roots: Mapping[str, str]
    phase0_internal_reexecution_verified: bool
    phase0_semantics_fully_verified: bool
    source_authenticity_proven: bool
    full_process_environment_identity_proven: bool
    upstream_root_lineage_proven: bool
    outcome_values_read: bool
    outcome_run_authorized: bool


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Phase0BundleVerificationError(f"Phase-0 artifact contains a non-JSON value: {exc}") from exc


def _json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _render_json(value: Any) -> bytes:
    try:
        return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Phase0BundleVerificationError(f"Phase-0 artifact cannot be rendered deterministically: {exc}") from exc


def _regular_file_stat(path: Path, *, label: str) -> os.stat_result:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise Phase0BundleVerificationError(f"cannot stat {label}: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise Phase0BundleVerificationError(f"{label} must be a regular non-symlink file")
    return observed


def _file_bytes_and_sha256(path: Path, *, label: str) -> tuple[bytes, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise Phase0BundleVerificationError(f"cannot open {label}: {exc}") from exc
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise Phase0BundleVerificationError(f"{label} must be a regular file")
        while chunk := os.read(descriptor, _HASH_CHUNK_BYTES):
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise Phase0BundleVerificationError(f"{label} changed while it was read")
    observed = _regular_file_stat(path, label=label)
    path_identity = (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
    if path_identity != after_identity:
        raise Phase0BundleVerificationError(f"{label} path was replaced while it was read")
    return b"".join(chunks), digest.hexdigest()


def _strict_json(data: bytes, *, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    try:
        payload = json.loads(data, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Phase0BundleVerificationError(f"{label} is not strict JSON: {exc}") from exc
    if _render_json(payload) != data:
        raise Phase0BundleVerificationError(f"{label} is not in the canonical Phase-0 file rendering")
    return payload


def _require_object(payload: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise Phase0BundleVerificationError(f"{label} must be a JSON object")
    return dict(payload)


def _verify_self_hash(payload: Mapping[str, Any], *, label: str, field: str = "artifact_sha256") -> None:
    observed = payload.get(field)
    unhashed = dict(payload)
    unhashed.pop(field, None)
    if observed != _json_hash(unhashed):
        raise Phase0BundleVerificationError(f"{label} {field} mismatch")


def _validate_receipt_and_load(
    receipt_path: Path,
) -> tuple[dict[str, Any], str, dict[str, dict[str, Any]], dict[str, bytes]]:
    receipt_bytes, receipt_sha256 = _file_bytes_and_sha256(receipt_path, label="Phase-0 receipt")
    receipt = _require_object(_strict_json(receipt_bytes, label="Phase-0 receipt"), label="Phase-0 receipt")
    if set(receipt) != _RECEIPT_KEYS:
        raise Phase0BundleVerificationError(
            "Phase-0 receipt schema mismatch; "
            f"missing={sorted(_RECEIPT_KEYS - set(receipt))}, unknown={sorted(set(receipt) - _RECEIPT_KEYS)}"
        )
    if receipt.get("schema_version") != PHASE0_BUNDLE_RECEIPT_SCHEMA_VERSION:
        raise Phase0BundleVerificationError("unsupported Phase-0 bundle receipt schema version")
    if receipt.get("receipt_type") != PHASE0_BUNDLE_RECEIPT_TYPE:
        raise Phase0BundleVerificationError("receipt is not a strategy-overhaul Phase-0 bundle")
    phase0_id = receipt.get("phase0_id")
    if not isinstance(phase0_id, str) or not phase0_id or phase0_id != phase0_id.strip():
        raise Phase0BundleVerificationError("Phase-0 receipt phase0_id must be canonical and nonblank")
    if receipt_path.name != "receipt.json" or receipt_path.parent.name != phase0_id:
        raise Phase0BundleVerificationError("Phase-0 receipt path must be <phase0_id>/receipt.json")
    if receipt.get("outcome_run_authorized") is not False:
        raise Phase0BundleVerificationError("Phase-0 receipt must explicitly refuse outcome-run authorization")

    raw_rows = receipt.get("files")
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        raise Phase0BundleVerificationError("Phase-0 receipt files must be a list of objects")
    rows = [dict(row) for row in raw_rows]
    for row in rows:
        if set(row) != {"path", "sha256", "bytes"}:
            raise Phase0BundleVerificationError("Phase-0 receipt file rows require exactly path/sha256/bytes")
        relative = row.get("path")
        pure = PurePosixPath(str(relative))
        digest = row.get("sha256")
        size = row.get("bytes")
        if (
            not isinstance(relative, str)
            or not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or len(pure.parts) != 1
            or pure.as_posix() != relative
        ):
            raise Phase0BundleVerificationError(f"non-canonical Phase-0 artifact path: {relative!r}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise Phase0BundleVerificationError(f"invalid Phase-0 receipt metadata for {relative!r}")
    paths = [str(row["path"]) for row in rows]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise Phase0BundleVerificationError("Phase-0 receipt file rows must be unique and path-sorted")
    if set(paths) != _EXPECTED_ARTIFACTS:
        raise Phase0BundleVerificationError(
            "Phase-0 artifact set mismatch; "
            f"missing={sorted(_EXPECTED_ARTIFACTS - set(paths))}, extra={sorted(set(paths) - _EXPECTED_ARTIFACTS)}"
        )

    entries = list(receipt_path.parent.iterdir())
    actual_names = {entry.name for entry in entries}
    expected_names = _EXPECTED_ARTIFACTS | {"receipt.json"}
    if actual_names != expected_names:
        raise Phase0BundleVerificationError(
            "Phase-0 bundle directory inventory mismatch; "
            f"missing={sorted(expected_names - actual_names)}, extra={sorted(actual_names - expected_names)}"
        )
    for entry in entries:
        _regular_file_stat(entry, label=f"Phase-0 bundle entry {entry.name}")

    payloads: dict[str, dict[str, Any]] = {}
    binary: dict[str, bytes] = {}
    for row in rows:
        name = str(row["path"])
        data, digest = _file_bytes_and_sha256(receipt_path.parent / name, label=f"Phase-0 artifact {name}")
        if len(data) != row["bytes"] or digest != row["sha256"]:
            raise Phase0BundleVerificationError(f"Phase-0 artifact hash/size mismatch: {name}")
        if name in _JSON_ARTIFACTS:
            payloads[name] = _require_object(_strict_json(data, label=name), label=name)
        else:
            binary[name] = data
    return receipt, receipt_sha256, payloads, binary


def _validate_source_bundle(
    payloads: Mapping[str, Mapping[str, Any]],
    binary: Mapping[str, bytes],
) -> None:
    source = payloads["source_snapshot.json"]
    untracked = payloads["untracked_sources_manifest.json"]
    _verify_self_hash(source, label="source_snapshot.json")
    _verify_self_hash(untracked, label="untracked_sources_manifest.json")
    tracked_patch = binary["tracked_worktree.patch"]
    archive = binary["untracked_sources.tar"]
    tracked_row = source.get("tracked_patch")
    untracked_row = source.get("untracked_sources")
    if not isinstance(tracked_row, dict) or not isinstance(untracked_row, dict):
        raise Phase0BundleVerificationError("source snapshot lacks tracked/untracked receipt objects")
    if (
        tracked_row.get("path") != "tracked_worktree.patch"
        or tracked_row.get("sha256") != hashlib.sha256(tracked_patch).hexdigest()
        or tracked_row.get("bytes") != len(tracked_patch)
    ):
        raise Phase0BundleVerificationError("tracked source patch disagrees with source_snapshot.json")
    untracked_file_bytes = _render_json(untracked)
    if (
        untracked_row.get("archive_path") != "untracked_sources.tar"
        or untracked_row.get("archive_sha256") != hashlib.sha256(archive).hexdigest()
        or untracked_row.get("manifest_path") != "untracked_sources_manifest.json"
        or untracked_row.get("manifest_file_sha256") != hashlib.sha256(untracked_file_bytes).hexdigest()
        or untracked_row.get("manifest_payload_sha256") != untracked.get("artifact_sha256")
    ):
        raise Phase0BundleVerificationError("untracked source receipts disagree with bundled bytes")
    if (
        untracked.get("archive_path") != "untracked_sources.tar"
        or untracked.get("archive_sha256") != hashlib.sha256(archive).hexdigest()
    ):
        raise Phase0BundleVerificationError("untracked source manifest disagrees with its archive")

    raw_files = untracked.get("files")
    if not isinstance(raw_files, list) or any(not isinstance(row, dict) for row in raw_files):
        raise Phase0BundleVerificationError("untracked source manifest files must be a list of objects")
    rows = [dict(row) for row in raw_files]
    names = [row.get("path") for row in rows]
    if names != sorted(names) or len(names) != len(set(names)):
        raise Phase0BundleVerificationError("untracked source manifest paths must be unique and sorted")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
            members = handle.getmembers()
            if [member.name for member in members] != names:
                raise Phase0BundleVerificationError("untracked source archive paths disagree with its manifest")
            for member, row in zip(members, rows, strict=True):
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != member.name:
                    raise Phase0BundleVerificationError(f"unsafe untracked source archive path: {member.name!r}")
                if not member.isfile():
                    raise Phase0BundleVerificationError(f"untracked source archive member is not regular: {member.name}")
                extracted = handle.extractfile(member)
                if extracted is None:
                    raise Phase0BundleVerificationError(f"cannot read untracked source archive member: {member.name}")
                data = extracted.read()
                if len(data) != row.get("bytes") or hashlib.sha256(data).hexdigest() != row.get("sha256"):
                    raise Phase0BundleVerificationError(f"untracked source archive content mismatch: {member.name}")
                if (
                    member.mode != int(str(row.get("archive_mode")), 8)
                    or member.mtime != row.get("archive_mtime")
                    or member.uid != row.get("archive_uid")
                    or member.gid != row.get("archive_gid")
                    or member.uname != ""
                    or member.gname != ""
                ):
                    raise Phase0BundleVerificationError(
                        f"untracked source archive normalization mismatch: {member.name}"
                    )
    except (tarfile.TarError, ValueError) as exc:
        raise Phase0BundleVerificationError(f"untracked source archive is invalid: {exc}") from exc


def _expected_identity(
    *,
    scout: Any,
    plan: Mapping[str, Any],
    inventory: Mapping[str, Any],
    source_snapshot: Mapping[str, Any],
    environment: Mapping[str, Any],
    map_artifact: Mapping[str, Any],
    config_payloads: Mapping[str, Any],
    config_index: Mapping[str, Any],
    designs: Mapping[str, Any],
    support: Mapping[str, Any],
) -> dict[str, Any]:
    config_file_hashes = {
        path: hashlib.sha256(_render_json(payload)).hexdigest()
        for path, payload in sorted(config_payloads.items())
    }
    return {
        "schema_version": 2,
        "contract": plan["sources"][str(scout.CONTRACT.relative_to(scout.REPO))],
        "diagnosis": plan["sources"][str(scout.DIAGNOSIS.relative_to(scout.REPO))],
        "git": plan["git"],
        "source_snapshot_sha256": source_snapshot["artifact_sha256"],
        "environment_manifest_path": scout.ENVIRONMENT_MANIFEST_ARTIFACT,
        "environment_manifest_file_sha256": hashlib.sha256(_render_json(environment)).hexdigest(),
        "instrument_map_artifact_path": scout.INSTRUMENT_MAP_ARTIFACT,
        "instrument_map_artifact_sha256": map_artifact["artifact_sha256"],
        "config_artifact_index_path": scout.CONFIG_ARTIFACT_INDEX,
        "config_artifact_index_file_sha256": config_file_hashes[scout.CONFIG_ARTIFACT_INDEX],
        "config_artifact_index_payload_sha256": config_index["artifact_sha256"],
        "config_artifact_file_sha256": config_file_hashes,
        "sources": plan["sources"],
        "configs": plan["configs"],
        "windows": plan["windows"],
        "phase0_input_plan_sha256": plan["phase0_input_plan_sha256"],
        "registered_child_designs_sha256": designs["artifact_sha256"],
        "support_design_and_counts_sha256": support["artifact_sha256"],
        "s01_status_derivation_version": scout.S01_STATUS_DERIVATION_VERSION,
        "phase0_inventory_sha256": inventory["artifact_sha256"],
    }


def _assert_expected_consumer(
    *,
    roots: Mapping[str, Path],
    inventory: Mapping[str, Any],
    plan: Mapping[str, Any],
    expected_venue: str | None,
    expected_root: Path | None,
    expected_window: Mapping[str, str] | None,
) -> None:
    if expected_venue is None and expected_root is None and expected_window is None:
        return
    if expected_venue not in roots:
        raise Phase0BundleVerificationError(f"Phase-0 does not register venue {expected_venue!r}")
    assert expected_venue is not None
    if expected_root is None or expected_window is None:
        raise Phase0BundleVerificationError("expected_venue, expected_root, and expected_window must be supplied together")
    supplied = expected_root.expanduser()
    if supplied.is_symlink():
        raise Phase0BundleVerificationError(f"expected root must not be a symlink: {supplied}")
    resolved = supplied.resolve()
    if resolved != roots[expected_venue]:
        raise Phase0BundleVerificationError(
            f"Phase-0 {expected_venue} root mismatch: bundle={roots[expected_venue]}, consumer={resolved}"
        )
    inventory_window = inventory.get("window")
    plan_window = plan.get("windows")
    if not isinstance(inventory_window, dict) or not isinstance(plan_window, dict):
        raise Phase0BundleVerificationError("Phase-0 window artifacts are absent")
    registered = {
        "causal_read_start_date": inventory_window.get("inventory_read_start_date"),
        "signal_end_date_exclusive": inventory_window.get("inventory_read_end_date_exclusive"),
        "label_end_date_exclusive": plan_window.get("label_end_date_exclusive"),
    }
    for field, expected in registered.items():
        if expected_window.get(field) != expected:
            raise Phase0BundleVerificationError(
                f"root snapshot {field} is outside the exact registered Phase-0 scope: "
                f"expected {expected!r}, got {expected_window.get(field)!r}"
            )
    identity_start = expected_window.get("identity_history_start_date")
    causal_start = expected_window.get("causal_read_start_date")
    if not isinstance(identity_start, str) or not isinstance(causal_start, str) or identity_start > causal_start:
        raise Phase0BundleVerificationError(
            "root snapshot identity_history_start_date must be present and no later than the registered causal start"
        )


def verify_phase0_bundle(
    receipt: str | Path,
    *,
    require_ready: bool = True,
    expected_venue: str | None = None,
    expected_root: str | Path | None = None,
    expected_window: Mapping[str, str] | None = None,
) -> Phase0BundleVerification:
    """Recompute and cross-check an outcome-blind Phase-0 bundle internally.

    This verifier intentionally binds the bundle to the currently checked-out
    reconstructable source snapshot and observed Python environment.  To verify
    on another machine, first restore the bundled commit/patch/untracked archive
    and the recorded selected environment.  A successful result does not
    authenticate those trust inputs or establish canonical S01 readiness.
    """

    raw_receipt_path = Path(receipt).expanduser()
    if raw_receipt_path.is_symlink():
        raise Phase0BundleVerificationError(f"Phase-0 receipt must not be a symlink: {raw_receipt_path}")
    receipt_path = raw_receipt_path.resolve()
    receipt_payload, receipt_sha256, payloads, binary = _validate_receipt_and_load(receipt_path)
    _validate_source_bundle(payloads, binary)

    inventory = payloads["phase0_inventory.json"]
    identity = payloads["identity.json"]
    plan = payloads["command_plan.json"]
    if inventory.get("artifact_type") != "strategy_overhaul_phase0_outcome_blind_inventory":
        raise Phase0BundleVerificationError("phase0_inventory.json has the wrong artifact type")
    _verify_self_hash(inventory, label="phase0_inventory.json")
    if plan.get("receipt_type") != "strategy_overhaul_phase0_input_plan":
        raise Phase0BundleVerificationError("command_plan.json is not a Phase-0 input plan")
    _verify_self_hash(plan, label="command_plan.json", field="phase0_input_plan_sha256")
    for path, field in _INVENTORY_PROJECTIONS.items():
        if payloads[path] != inventory.get(field):
            raise Phase0BundleVerificationError(f"{path} disagrees with phase0_inventory.json[{field!r}]")
    audit = inventory.get("outcome_blind_audit")
    if not isinstance(audit, dict) or any(
        audit.get(field) is not False
        for field in (
            "outcome_values_read",
            "ohlcv_values_read",
            "residual_momentum_values_read",
            "returns_calculated",
            "mfe_calculated",
            "mae_calculated",
            "pnl_calculated",
            "ranks_calculated",
            "labels_calculated",
            "wall_clock_fields_emitted",
        )
    ):
        raise Phase0BundleVerificationError("Phase-0 outcome-blind audit is absent or contradictory")

    raw_roots = inventory.get("roots")
    if not isinstance(raw_roots, dict) or set(raw_roots) != {"bybit", "binance"}:
        raise Phase0BundleVerificationError("Phase-0 inventory must bind exactly bybit and binance roots")
    try:
        roots = canonicalize_phase0_roots(raw_roots, require_registered_venues=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Phase0BundleVerificationError(f"Phase-0 root identity is invalid: {exc}") from exc
    for venue, root in roots.items():
        if str(root) != raw_roots[venue]:
            raise Phase0BundleVerificationError(f"Phase-0 {venue} root path is not canonical: {raw_roots[venue]!r}")

    # Lazy import prevents the CLI bootstrap from participating in module import
    # cycles while still making its source/config/environment derivations the
    # single implementation used by both writer and verifier.
    from scripts import strategy_overhaul_scout_2026_07_10 as scout

    source = scout._capture_source_snapshot()
    if (
        source.manifest != payloads["source_snapshot.json"]
        or source.untracked_manifest != payloads["untracked_sources_manifest.json"]
        or source.tracked_patch != binary["tracked_worktree.patch"]
        or source.untracked_archive != binary["untracked_sources.tar"]
    ):
        raise Phase0BundleVerificationError(
            "current repository source snapshot does not equal the reconstructable Phase-0 source bundle"
        )
    environment = scout._environment_receipt()
    if environment != payloads["environment_manifest.json"] or not scout._environment_manifest_ready(environment):
        raise Phase0BundleVerificationError("current Python/platform environment does not equal the Phase-0 manifest")
    configs = scout._config_receipts()
    config_payloads, config_index = scout._config_bundle_artifacts(configs)
    for path, expected in config_payloads.items():
        if payloads.get(path) != expected:
            raise Phase0BundleVerificationError(f"bundled canonical config artifact is stale or fabricated: {path}")
    if payloads["config_artifact_index.json"] != config_index:
        raise Phase0BundleVerificationError("config_artifact_index.json disagrees with derived canonical configs")

    args = argparse.Namespace(
        bybit_root=roots["bybit"],
        binance_root=roots["binance"],
        deep_root_hash=False,
    )
    observed_full_plan = scout.build_plan(args, include_generated_at_utc=False, git_state=source.git)
    observed_plan = scout._phase0_input_plan(observed_full_plan)
    if observed_plan != plan:
        raise Phase0BundleVerificationError("command_plan.json does not equal the current exact Phase-0 input plan")

    inventory_window = inventory.get("window")
    if not isinstance(inventory_window, dict):
        raise Phase0BundleVerificationError("Phase-0 inventory window is absent")
    start_date = inventory_window.get("inventory_read_start_date")
    end_date = inventory_window.get("inventory_read_end_date_exclusive")
    if not isinstance(start_date, str) or not isinstance(end_date, str):
        raise Phase0BundleVerificationError("Phase-0 inventory boundaries must be ISO date strings")
    map_payload = payloads["instrument_map_input.json"]
    _verify_self_hash(map_payload, label="instrument_map_input.json")
    source_kind = map_payload.get("source_kind")
    if source_kind == "auto_derived_archive_trade_manifest_symbol_date_projection":
        map_entries, map_version, observed_map = scout._resolve_phase0_instrument_map(
            path=None,
            version_override=None,
            roots=roots,
            start_date=start_date,
            end_date_exclusive=end_date,
            batch_size=65_536,
        )
    elif source_kind == "external_json":
        source_path = map_payload.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            raise Phase0BundleVerificationError("external instrument map lacks its exact source path")
        map_entries, map_version, observed_map = scout._resolve_phase0_instrument_map(
            path=Path(source_path),
            version_override=str(map_payload.get("version") or ""),
            roots=roots,
            start_date=start_date,
            end_date_exclusive=end_date,
            batch_size=65_536,
        )
    else:
        raise Phase0BundleVerificationError(f"unsupported Phase-0 instrument-map source kind: {source_kind!r}")
    if observed_map != map_payload:
        raise Phase0BundleVerificationError("instrument_map_input.json does not equal its recomputed source projection")

    rebuilt_inventory = build_phase0_artifacts(
        roots,
        start_date=start_date,
        end_date_exclusive=end_date,
        instrument_map=map_entries,
        instrument_map_version=map_version,
        instrument_map_authority=str(map_payload.get("trust_class") or "external_untrusted"),
        sleeve_windows=REGISTERED_SLEEVE_WINDOWS,
        batch_size=65_536,
    )
    if _canonical_json(rebuilt_inventory) != _canonical_json(inventory):
        raise Phase0BundleVerificationError(
            "phase0_inventory.json does not equal a fresh outcome-blind scan of the bound roots"
        )
    scout._assert_auto_map_matches_phase0_inventory(map_payload, rebuilt_inventory)

    designs = scout._load_registered_child_designs(plan["sources"])
    if designs != payloads["registered_child_designs.json"]:
        raise Phase0BundleVerificationError("registered_child_designs.json does not equal its bound templates")
    support = scout._support_design_and_counts(rebuilt_inventory, designs)
    if support != payloads["support_design_and_counts.json"]:
        raise Phase0BundleVerificationError("support_design_and_counts.json does not equal its exact derivation")
    expected_identity = _expected_identity(
        scout=scout,
        plan=plan,
        inventory=rebuilt_inventory,
        source_snapshot=source.manifest,
        environment=environment,
        map_artifact=map_payload,
        config_payloads=config_payloads,
        config_index=config_index,
        designs=designs,
        support=support,
    )
    if identity != expected_identity:
        raise Phase0BundleVerificationError("identity.json does not equal the exact Phase-0 identity derivation")
    expected_phase0_id = f"strategy-overhaul-phase0-{_json_hash(expected_identity)[:20]}"
    if receipt_payload.get("phase0_id") != expected_phase0_id:
        raise Phase0BundleVerificationError("Phase-0 directory/receipt ID does not derive from identity.json")

    expected_s01 = scout._s01_template_input_status(
        phase0_id=expected_phase0_id,
        plan=plan,
        inventory=rebuilt_inventory,
        environment=environment,
        designs=designs,
        support_artifact=support,
        instrument_map_artifact=map_payload,
        config_artifact_index=config_index,
    )
    if expected_s01 != payloads["s01_template_input_status.json"]:
        raise Phase0BundleVerificationError("s01_template_input_status.json does not equal its exact derivation")

    if receipt_payload.get("identity_sha256") != _json_hash(identity):
        raise Phase0BundleVerificationError("Phase-0 receipt identity_sha256 disagrees with identity.json")
    if receipt_payload.get("inventory_sha256") != rebuilt_inventory.get("artifact_sha256"):
        raise Phase0BundleVerificationError("Phase-0 receipt inventory_sha256 disagrees with the rebuilt inventory")
    readiness = (rebuilt_inventory.get("readiness") or {}).get("status")
    if receipt_payload.get("readiness_status") != readiness:
        raise Phase0BundleVerificationError("Phase-0 receipt readiness disagrees with the rebuilt inventory")
    if require_ready and readiness != "READY":
        raise Phase0BundleVerificationError("downstream root snapshot requires Phase-0 readiness_status=READY")

    resolved_expected_root = Path(expected_root).expanduser() if expected_root is not None else None
    _assert_expected_consumer(
        roots=roots,
        inventory=rebuilt_inventory,
        plan=plan,
        expected_venue=expected_venue,
        expected_root=resolved_expected_root,
        expected_window=expected_window,
    )
    if scout._capture_source_snapshot() != source:
        raise Phase0BundleVerificationError("repository source snapshot changed during Phase-0 verification")
    if scout._environment_receipt() != environment:
        raise Phase0BundleVerificationError("Python/platform environment changed during Phase-0 verification")
    if scout._config_receipts() != configs:
        raise Phase0BundleVerificationError("canonical A0 config identities changed during Phase-0 verification")
    final_receipt, final_receipt_sha256, final_payloads, final_binary = _validate_receipt_and_load(receipt_path)
    if (
        final_receipt_sha256 != receipt_sha256
        or final_receipt != receipt_payload
        or final_payloads != payloads
        or final_binary != binary
    ):
        raise Phase0BundleVerificationError("Phase-0 bundle changed during semantic verification")

    return Phase0BundleVerification(
        receipt=MappingProxyType(receipt_payload),
        receipt_sha256=receipt_sha256,
        phase0_id=expected_phase0_id,
        identity=MappingProxyType(identity),
        inventory=MappingProxyType(rebuilt_inventory),
        command_plan=MappingProxyType(plan),
        roots=MappingProxyType({venue: str(root) for venue, root in sorted(roots.items())}),
        phase0_internal_reexecution_verified=True,
        phase0_semantics_fully_verified=False,
        source_authenticity_proven=False,
        full_process_environment_identity_proven=False,
        upstream_root_lineage_proven=False,
        outcome_values_read=False,
        outcome_run_authorized=False,
    )


__all__ = [
    "PHASE0_BUNDLE_RECEIPT_SCHEMA_VERSION",
    "PHASE0_BUNDLE_RECEIPT_TYPE",
    "Phase0BundleVerification",
    "Phase0BundleVerificationError",
    "verify_phase0_bundle",
]
