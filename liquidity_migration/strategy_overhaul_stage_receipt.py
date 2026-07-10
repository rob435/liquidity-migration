"""Byte bindings and separate semantic verification for strategy-overhaul stages.

The v2 stage receipt binds already-created files and JSON identity documents by
their current bytes.  Its row/schema/key values remain caller declarations; its
archival verifier deliberately does not reconsult mutable factories.  A distinct
semantic-receipt path handles S02--S04: it parses self-describing Parquet or
Arrow IPC/Feather artifacts, checks the current registry/config, canonical
population, stage invariants, and transitive parent key/identity relations.

Neither path recomputes features or labels from source data, proves root/PIT
authenticity, authorizes an outcome run, or makes the filesystem immutable.
Both immutable writers refuse to overwrite different receipt bytes.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import math
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast

import polars as pl

from .strategy_overhaul_config_identity import (
    LONG_WINDOW_FIELDS,
    derive_continuous_a0_config_identity,
    derive_long_a0_config_identity,
    registered_scope_bounds_ms,
    verify_a0_config_identity,
)
from .strategy_overhaul_projection import (
    ArtifactProjectionError,
    artifact_polars_schema,
    project_artifact_frame,
)
from .strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    CONTINUOUS_ENTRY_SCHEMA_ID,
    CONTINUOUS_LABEL_SCHEMA_ID,
    CONTINUOUS_SIGNAL_SCHEMA_ID,
    LONG_ENTRY_SCHEMA_ID,
    LONG_LABEL_SCHEMA_ID,
    LONG_SIGNAL_SCHEMA_ID,
    schema_sha256,
)


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
Sleeve = Literal["continuous", "long"]
Venue = Literal["bybit", "binance"]
Stage = Literal["S00", "S01", "S02", "S03", "S04"]

STAGE_RECEIPT_SCHEMA_VERSION = "strategy_overhaul_stage_receipt_v2"
STAGE_RECEIPT_TYPE = "strategy_overhaul_diagnostic_stage_byte_binding"
RECEIPT_SCOPE = (
    "tamper_evident_byte_binding_only_artifact_schema_rows_keys_outcome_blindness_"
    "and_non_config_identity_semantics_unverified"
)
UNVERIFIED_ARTIFACT_DECLARATIONS = "UNVERIFIED_CALLER_DECLARATIONS"
CONFIG_IDENTITY_VERIFICATION_STATUS = "EXACT_REPOSITORY_DERIVED_CONFIG_MATCH_AT_BINDING"
OPAQUE_IDENTITY_VERIFICATION_STATUS = "UNVERIFIED_JSON_PAYLOAD_BYTE_BOUND_ONLY"

STAGE_SEMANTIC_RECEIPT_SCHEMA_VERSION = "strategy_overhaul_stage_semantic_receipt_v1"
STAGE_SEMANTIC_RECEIPT_TYPE = "strategy_overhaul_stage_artifact_semantic_verification"
STAGE_SEMANTIC_RECEIPT_SCOPE = (
    "registered_schema_dtypes_rows_keys_population_selected_stage_consistency_"
    "invariants_and_transitive_identity_with_current_config_registry_parity"
)
STAGE_SEMANTIC_VERIFICATION_STATUS = "VERIFIED_SCHEMA_KEYS_POPULATION_AND_SELECTED_STAGE_INVARIANTS"
STAGE_SEMANTIC_LIMITATIONS = (
    "does_not_recompute_features_labels_or_builder_parity_from_source_data",
    "canonical_population_receipt_identity_is_verified_but_raw_key_reconstruction_is_an_upstream_s01_responsibility",
    "population_receipt_informational_window_config_builder_exclusion_and_limitation_sections_are_hash_bound_not_semantically_interpreted",
    "does_not_exhaustively_validate_every_field_level_semantic_relation",
    "canonical_instrument_id_is_propagation_checked_but_not_rederived_from_the_bound_map",
    "does_not_prove_population_or_root_completeness",
    "does_not_prove_outcome_blind_construction_or_source_provenance",
    "does_not_authorize_an_outcome_run_deployment_or_real_money",
    "csv_ndjson_and_unknown_formats_are_rejected_because_the_frozen_physical_dtype_contract_is_not_preserved",
    "semantic_artifact_verification_is_whole_file_in_memory_without_partition_checkpoints",
    "archival_semantic_receipt_loading_checks_canonical_bytes_and_self_hash_but_does_not_reverify_current_artifacts",
)

_ARTIFACT_FORMAT_BY_SUFFIX = MappingProxyType(
    {
        ".parquet": "parquet",
        ".ipc": "arrow_ipc",
        ".arrow": "arrow_ipc",
        ".feather": "arrow_ipc",
    }
)
SUPPORTED_SEMANTIC_ARTIFACT_SUFFIXES = tuple(_ARTIFACT_FORMAT_BY_SUFFIX)
_MS_PER_HOUR = 60 * 60 * 1000
_MS_PER_DAY = 24 * _MS_PER_HOUR

SLEEVES = frozenset({"continuous", "long"})
VENUES = frozenset({"bybit", "binance"})
STAGES = ("S00", "S01", "S02", "S03", "S04")
IDENTITY_RECEIPT_KINDS = (
    "config",
    "source_snapshot",
    "environment",
    "root",
    "pit",
    "instrument_map",
    "population",
)
STAGE_IDENTITY_RECEIPT_KINDS: Mapping[str, tuple[str, ...]] = {
    # S00 is constructed before the root/PIT/map/population freeze in S01.
    "S00": ("config", "source_snapshot", "environment"),
    "S01": IDENTITY_RECEIPT_KINDS,
    "S02": IDENTITY_RECEIPT_KINDS,
    "S03": IDENTITY_RECEIPT_KINDS,
    "S04": IDENTITY_RECEIPT_KINDS,
}
PARENT_STAGES: Mapping[str, tuple[str, ...]] = {
    "S00": (),
    "S01": ("S00",),
    "S02": ("S01",),
    "S03": ("S02",),
    "S04": ("S02", "S03"),
}

_SCHEMA_IDS: Mapping[tuple[str, str], str] = {
    ("continuous", "S02"): CONTINUOUS_SIGNAL_SCHEMA_ID,
    ("continuous", "S03"): CONTINUOUS_ENTRY_SCHEMA_ID,
    ("continuous", "S04"): CONTINUOUS_LABEL_SCHEMA_ID,
    ("long", "S02"): LONG_SIGNAL_SCHEMA_ID,
    ("long", "S03"): LONG_ENTRY_SCHEMA_ID,
    ("long", "S04"): LONG_LABEL_SCHEMA_ID,
}
_HEX = frozenset("0123456789abcdef")
_TOP_LEVEL_KEYS = frozenset(
    {
        "artifact",
        "artifact_claims_verified",
        "canonical_config",
        "declared_outcome_blind",
        "diagnostic_only",
        "identity_receipts",
        "outcome_blindness_verified",
        "outcome_run_authorized",
        "parents",
        "provenance_blockers_cleared",
        "real_money_authorized",
        "receipt_id",
        "receipt_payload_sha256",
        "receipt_schema_version",
        "receipt_scope",
        "receipt_type",
        "run_id",
        "sleeve",
        "stage",
        "venue",
    }
)
_SEMANTIC_TOP_LEVEL_KEYS = frozenset(
    {
        "receipt_schema_version",
        "receipt_type",
        "receipt_scope",
        "verification_status",
        "run_id",
        "sleeve",
        "venue",
        "stage",
        "semantic_validation_performed",
        "stage_byte_bindings_verified",
        "current_config_and_registry_verified",
        "config_propagation_verified",
        "population_identity_and_s02_keys_verified",
        "diagnostic_only",
        "source_recomputation_performed",
        "outcome_blindness_verified",
        "population_or_root_completeness_verified",
        "outcome_run_authorized",
        "real_money_authorized",
        "stage_byte_receipt",
        "config_identity",
        "population_verification",
        "semantic_stage_artifacts",
        "transitive_stage_relations",
        "artifact_format_policy",
        "limitations",
        "receipt_payload_sha256",
        "receipt_id",
    }
)


class StageReceiptError(ValueError):
    """A stage-chain, identity, file, or canonical-JSON invariant failed."""


@dataclass(frozen=True, slots=True)
class BoundFileInput:
    """One physical input exposed in a receipt only by a safe logical path."""

    logical_path: str
    path: Path


@dataclass(frozen=True, slots=True)
class ArtifactInput:
    """Primary artifact plus explicitly unverified caller declarations."""

    logical_path: str
    path: Path
    declared_row_count: int
    declared_key_projection_sha256: str


@dataclass(frozen=True, slots=True)
class StageSchemaIdentity:
    schema_id: str
    schema_version: str
    schema_sha256: str


@dataclass(frozen=True, slots=True)
class ReceiptWriteResult:
    path: Path
    receipt_id: str
    file_sha256: str
    byte_count: int
    reused: bool


@dataclass(frozen=True, slots=True)
class ByteBindingVerification:
    """Successful byte-binding verification; no artifact semantics were checked."""

    receipt_id: str
    run_id: str
    sleeve: str
    venue: str
    stage: str
    byte_verified_receipt_count: int
    byte_verified_bound_file_count: int
    semantic_validation_performed: bool = False
    current_registry_or_config_factories_consulted: bool = False


@dataclass(frozen=True, slots=True)
class StageSemanticVerification:
    """Successful semantic verification plus its separately renderable receipt."""

    receipt_id: str
    stage_byte_receipt_id: str
    run_id: str
    sleeve: str
    venue: str
    stage: str
    semantic_verified_stage_count: int
    semantic_validation_performed: bool
    current_registry_or_config_factories_consulted: bool
    receipt: dict[str, JsonValue]


def _strict_json_value(value: Any, *, location: str = "$") -> JsonValue:
    """Return a strict JSON value without default conversion or stringification."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StageReceiptError(f"{location} contains NaN or infinity")
        return value
    if isinstance(value, list):
        return [_strict_json_value(item, location=f"{location}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StageReceiptError(f"{location} contains a non-string mapping key")
            output[key] = _strict_json_value(item, location=f"{location}.{key}")
        return output
    raise StageReceiptError(f"{location} contains unsupported JSON type {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode strict canonical JSON; unsupported objects and non-finite values fail."""

    ready = _strict_json_value(value)
    return json.dumps(
        ready,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise StageReceiptError(f"strict JSON contains duplicate key {key!r}")
        output[key] = value
    return output


def _reject_json_constant(value: str) -> None:
    raise StageReceiptError(f"strict JSON contains invalid constant {value!r}")


def _parse_json_object(data: bytes, *, name: str) -> dict[str, JsonValue]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageReceiptError(f"{name} is not UTF-8 JSON") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except StageReceiptError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StageReceiptError(f"{name} is not strict JSON: {exc}") from exc
    ready = _strict_json_value(payload, location=name)
    if not isinstance(ready, dict):
        raise StageReceiptError(f"{name} must contain one JSON object")
    return ready


def _require_sha256(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in _HEX for character in value)
    ):
        raise StageReceiptError(f"{name} must be one lowercase 64-character SHA-256")
    return value


def _require_exact_keys(value: object, expected: set[str] | frozenset[str], *, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise StageReceiptError(f"{name} must be an object")
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        raise StageReceiptError(f"{name} keys mismatch; missing={missing}, unknown={unknown}")
    return value


def _require_nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StageReceiptError(f"{name} must be a non-negative integer")
    return value


def _require_list(value: object, *, name: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise StageReceiptError(f"{name} must be a list")
    return value


def _normalise_logical_path(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        raise StageReceiptError(f"{name} must be a non-blank normalized POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise StageReceiptError(f"{name} must not be absolute or contain dot traversal")
    return value


def _regular_file_bytes(path: Path, *, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StageReceiptError(f"{name} must be a regular non-symlink file: {path}") from exc
        raise StageReceiptError(f"{name} is not readable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise StageReceiptError(f"{name} must be a regular non-symlink file: {path}")
        if not getattr(os, "O_NOFOLLOW", 0):  # pragma: no cover - supported on CI/macOS/Linux
            path_metadata = path.lstat()
            if (
                stat.S_ISLNK(path_metadata.st_mode)
                or path_metadata.st_dev != before.st_dev
                or path_metadata.st_ino != before.st_ino
            ):
                raise StageReceiptError(f"{name} changed or became a symlink while opening: {path}")

        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        before_fingerprint = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_fingerprint = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        data = b"".join(chunks)
        if before_fingerprint != after_fingerprint or len(data) != after.st_size:
            raise StageReceiptError(f"{name} changed while being read: {path}")
        return data
    except StageReceiptError:
        raise
    except OSError as exc:
        raise StageReceiptError(f"{name} is not readable: {path}") from exc
    finally:
        os.close(descriptor)


def _file_record(source: BoundFileInput, *, name: str) -> dict[str, JsonValue]:
    logical_path = _normalise_logical_path(source.logical_path, name=f"{name}.logical_path")
    data = _regular_file_bytes(Path(source.path), name=name)
    return {
        "logical_path": logical_path,
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _identity_receipt_record(source: BoundFileInput, *, kind: str) -> dict[str, JsonValue]:
    logical_path = _normalise_logical_path(
        source.logical_path,
        name=f"identity_receipts.{kind}.logical_path",
    )
    data = _regular_file_bytes(Path(source.path), name=f"identity_receipts.{kind}")
    payload = _parse_json_object(data, name=f"identity receipt {kind}")
    return {
        "logical_path": logical_path,
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "identity_sha256": canonical_json_sha256(payload),
        "semantic_verification_status": (
            CONFIG_IDENTITY_VERIFICATION_STATUS if kind == "config" else OPAQUE_IDENTITY_VERIFICATION_STATUS
        ),
    }


def _validate_config_identity_payload(
    payload: dict[str, JsonValue],
    *,
    sleeve: str,
    identity_sha256: str,
    scope_sha256: str,
) -> None:
    try:
        verify_a0_config_identity(payload)
    except (TypeError, ValueError) as exc:
        raise StageReceiptError(f"config identity receipt is invalid: {exc}") from exc
    if payload.get("sleeve") != sleeve:
        raise StageReceiptError("config identity receipt sleeve does not match the stage")
    if payload.get("identity_sha256") != identity_sha256:
        raise StageReceiptError("declared canonical config identity hash does not match its receipt")
    if payload.get("scope_sha256") != scope_sha256:
        raise StageReceiptError("declared registered scope hash does not match its config receipt")
    expected = derive_continuous_a0_config_identity() if sleeve == "continuous" else derive_long_a0_config_identity()
    if payload != expected:
        raise StageReceiptError(
            "config identity receipt is self-consistent but does not exactly equal the "
            "repository-derived canonical config identity"
        )


def registered_stage_schema(sleeve: str, stage: str) -> StageSchemaIdentity:
    """Return the exact current registry identity for an outcome-capable stage."""

    if sleeve not in SLEEVES:
        raise StageReceiptError(f"sleeve must be one of {sorted(SLEEVES)}")
    schema_id = _SCHEMA_IDS.get((sleeve, stage))
    if schema_id is None:
        raise StageReceiptError("registered stage schemas exist only for S02, S03, and S04")
    artifact = ARTIFACT_SCHEMAS[schema_id]
    return StageSchemaIdentity(
        schema_id=schema_id,
        schema_version=artifact.schema_version,
        schema_sha256=schema_sha256(schema_id),
    )


def stage_receipt_config_parity_surface(
    config_identity: Mapping[str, JsonValue],
) -> dict[str, dict[str, JsonValue]]:
    """Validate the bound identity against current factories and expose its guard surface.

    This is the owner-local consumer used by semantic S02--S04 verification.  It
    intentionally returns the same ``full_config_and_scope_identity`` shape as
    the central S02 parity manifest, so that manifest can call the guard rather
    than asserting that downstream receipts are wired by inspection.
    """

    for stage in ("S02", "S03", "S04"):
        if "config" not in STAGE_IDENTITY_RECEIPT_KINDS.get(stage, ()):
            raise StageReceiptError(f"{stage} stage receipt contract does not require config identity")
    if PARENT_STAGES.get("S03") != ("S02",) or PARENT_STAGES.get("S04") != ("S02", "S03"):
        raise StageReceiptError("stage receipt topology no longer preserves S02->S03 and same-S02 S04")

    payload = dict(config_identity)
    sleeve = payload.get("sleeve")
    if sleeve not in SLEEVES:
        raise StageReceiptError("stage-receipt config identity has an invalid sleeve")
    identity_sha256 = payload.get("identity_sha256")
    scope_sha256 = payload.get("scope_sha256")
    _validate_config_identity_payload(
        payload,
        sleeve=str(sleeve),
        identity_sha256=_require_sha256(
            identity_sha256,
            name="stage-receipt config identity.identity_sha256",
        ),
        scope_sha256=_require_sha256(
            scope_sha256,
            name="stage-receipt config identity.scope_sha256",
        ),
    )
    canonical = payload.get("canonical_config")
    if not isinstance(canonical, dict) or not isinstance(canonical.get("config"), dict):
        raise StageReceiptError("config identity canonical_config.config is malformed")
    config = canonical["config"]
    assert isinstance(config, dict)
    if sleeve == "continuous":
        observed: dict[str, JsonValue] = {
            "full_config_sha256": cast(JsonValue, payload["canonical_config_sha256"]),
            "registered_scope_sha256": cast(JsonValue, payload["scope_sha256"]),
            "component_config_sha256": cast(JsonValue, payload["component_config_sha256"]),
        }
        return {
            "full_config_and_scope_identity": observed,
            "selection_profile": {"rmom_quantile": cast(JsonValue, config["rmom_quantile"])},
            "decision_and_btc_gate": {
                "entry_confirm_delay_hours": cast(JsonValue, config["entry_confirm_delay_hours"]),
            },
        }
    else:
        observed = {
            "full_config_sha256": cast(JsonValue, payload["canonical_config_sha256"]),
            "registered_scope_sha256": cast(JsonValue, payload["scope_sha256"]),
            "undated_window_fields": {name: cast(JsonValue, config[name]) for name in LONG_WINDOW_FIELDS},
        }
        return {
            "full_config_and_scope_identity": observed,
            "population_and_rolling_windows": {
                name: cast(JsonValue, config[name])
                for name in ("universe_size", "universe_volume_window_days", "min_listing_history_days")
            },
            "classifier_and_exit_shape": {
                "fc_max_hold_days": cast(JsonValue, config["fc_max_hold_days"]),
            },
            "trigger_and_exit_profile": {
                "fc_use_atr_exits": cast(JsonValue, config["fc_use_atr_exits"]),
            },
        }


def stage_receipt_config_consumer_parity_surface(
    config_identity: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return exact receipt values plus mechanically owned consumer metadata."""

    values = stage_receipt_config_parity_surface(config_identity)
    sleeve = config_identity.get("sleeve")
    if sleeve not in SLEEVES:  # pragma: no cover - exact surface rejects first
        raise StageReceiptError("stage-receipt config identity has an invalid sleeve")
    consumers: dict[str, list[JsonValue]] = {
        "full_config_and_scope_identity": [f"all downstream {str(sleeve).upper()} S03/S04 stage receipts"],
    }
    if sleeve == "continuous":
        consumers.update(
            {
                "selection_profile": [
                    "strategy_overhaul_stage_receipt._validate_continuous_s02 rmom quantile semantics"
                ],
                "decision_and_btc_gate": [
                    "strategy_overhaul_stage_receipt._validate_continuous_s02/_validate_continuous_s03 config-derived timing"
                ],
            }
        )
    else:
        consumers.update(
            {
                "population_and_rolling_windows": [
                    "strategy_overhaul_stage_receipt._validate_long_s02 rank/membership semantics"
                ],
                "classifier_and_exit_shape": ["strategy_overhaul_stage_receipt._validate_long_s02 max-hold semantics"],
                "trigger_and_exit_profile": [
                    "strategy_overhaul_stage_receipt._validate_long_s02 ATR-fallback semantics"
                ],
            }
        )
    return {
        **{target: cast(JsonValue, values[target]) for target in consumers},
        "consumer_validator": (
            "liquidity_migration.strategy_overhaul_stage_receipt.stage_receipt_config_consumer_parity_surface"
        ),
        "validated_targets": list(consumers),
        "validated_target_fields": {target: list(values[target]) for target in consumers},
        "validated_consumers": consumers,
    }


def _validate_current_registry_declaration(
    payload: Mapping[str, JsonValue],
    *,
    context: str,
) -> None:
    """Check one already-validated receipt against the current stage registry."""

    stage = str(payload["stage"])
    if stage in {"S00", "S01"}:
        return
    sleeve = str(payload["sleeve"])
    artifact = payload["artifact"]
    if not isinstance(artifact, dict):  # pragma: no cover - structural validation runs first
        raise StageReceiptError(f"{context} artifact declaration is malformed")
    expected = registered_stage_schema(sleeve, stage)
    expected_record: dict[str, JsonValue] = {
        "schema_id": expected.schema_id,
        "schema_version": expected.schema_version,
        "schema_sha256": expected.schema_sha256,
    }
    if artifact.get("declared_schema_identity") != expected_record:
        raise StageReceiptError(
            f"{context} {sleeve}/{stage} declared schema does not match the construction-time current registry"
        )


def _schema_record(
    *,
    sleeve: str,
    stage: str,
    supplied: StageSchemaIdentity | None,
) -> dict[str, JsonValue] | None:
    if stage in {"S00", "S01"}:
        if supplied is not None:
            raise StageReceiptError(f"{stage} must not declare an S02-S04 artifact schema identity")
        return None
    if supplied is None:
        raise StageReceiptError(f"{stage} requires an exact declared registry schema identity")
    expected = registered_stage_schema(sleeve, stage)
    if supplied != expected:
        raise StageReceiptError(
            f"{sleeve}/{stage} declared schema identity mismatch; expected={expected!r}, supplied={supplied!r}"
        )
    return {
        "schema_id": supplied.schema_id,
        "schema_version": supplied.schema_version,
        "schema_sha256": supplied.schema_sha256,
    }


def _run_identity_payload(
    *,
    sleeve: str,
    venue: str,
    canonical_config: Mapping[str, JsonValue],
    identity_receipts: Mapping[str, Mapping[str, JsonValue]],
) -> dict[str, JsonValue]:
    return {
        "receipt_schema_version": STAGE_RECEIPT_SCHEMA_VERSION,
        "sleeve": sleeve,
        "venue": venue,
        "canonical_config": dict(canonical_config),
        "identity_receipts": {
            kind: {
                "identity_sha256": row["identity_sha256"],
                "file_sha256": row["file_sha256"],
                "bytes": row["bytes"],
                "semantic_verification_status": row["semantic_verification_status"],
            }
            for kind, row in sorted(identity_receipts.items())
        },
    }


def _run_id(
    *,
    sleeve: str,
    venue: str,
    canonical_config: Mapping[str, JsonValue],
    identity_receipts: Mapping[str, Mapping[str, JsonValue]],
) -> str:
    digest = canonical_json_sha256(
        _run_identity_payload(
            sleeve=sleeve,
            venue=venue,
            canonical_config=canonical_config,
            identity_receipts=identity_receipts,
        )
    )
    return f"strategy-overhaul-{sleeve}-{venue}-{digest[:24]}"


def _validate_declared_outcome_blind(stage: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise StageReceiptError("declared_outcome_blind must be a boolean")
    expected = {
        "S00": True,
        "S01": True,
        "S02": True,
        "S03": False,
        "S04": False,
    }[stage]
    if value is not expected:
        raise StageReceiptError(f"{stage} requires declared_outcome_blind={str(expected).lower()}")
    return value


def _parent_record(source: BoundFileInput) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    data = _regular_file_bytes(Path(source.path), name="parent receipt")
    payload = _parse_stage_receipt_bytes(data, name=f"parent receipt {source.path}")
    record: dict[str, JsonValue] = {
        "logical_path": _normalise_logical_path(source.logical_path, name="parent logical_path"),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "stage": payload["stage"],
        "run_id": payload["run_id"],
        "receipt_id": payload["receipt_id"],
        "receipt_payload_sha256": payload["receipt_payload_sha256"],
    }
    return record, payload


def _artifact_record(
    artifact: ArtifactInput,
    *,
    declared_schema_identity: Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    if (
        isinstance(artifact.declared_row_count, bool)
        or not isinstance(artifact.declared_row_count, int)
        or artifact.declared_row_count < 0
    ):
        raise StageReceiptError("artifact.declared_row_count must be a non-negative integer")
    key_hash = _require_sha256(
        artifact.declared_key_projection_sha256,
        name="artifact.declared_key_projection_sha256",
    )
    record = _file_record(
        BoundFileInput(logical_path=artifact.logical_path, path=artifact.path),
        name="artifact",
    )
    return {
        **record,
        "declaration_status": UNVERIFIED_ARTIFACT_DECLARATIONS,
        "declared_row_count": artifact.declared_row_count,
        "declared_key_projection_sha256": key_hash,
        "declared_schema_identity": (dict(declared_schema_identity) if declared_schema_identity is not None else None),
    }


def _assert_input_unchanged(
    source: BoundFileInput,
    record: Mapping[str, JsonValue],
    *,
    name: str,
    json_identity: bool,
) -> None:
    data = _regular_file_bytes(Path(source.path), name=name)
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["file_sha256"]:
        raise StageReceiptError(f"{name} changed during stage-receipt construction")
    if json_identity:
        payload = _parse_json_object(data, name=name)
        if canonical_json_sha256(payload) != record["identity_sha256"]:
            raise StageReceiptError(f"{name} JSON identity changed during stage-receipt construction")


def _validate_parent_chain(
    *,
    stage: str,
    sleeve: str,
    venue: str,
    run_id: str,
    child_identity_records: Mapping[str, Mapping[str, JsonValue]],
    parent_records: Sequence[Mapping[str, JsonValue]],
    parent_payloads: Mapping[str, Mapping[str, JsonValue]],
) -> None:
    expected = PARENT_STAGES[stage]
    observed = tuple(str(row["stage"]) for row in parent_records)
    if observed != expected:
        raise StageReceiptError(f"{stage} requires parents {expected}, received {observed}")
    for parent_stage in expected:
        payload = parent_payloads[parent_stage]
        if payload["sleeve"] != sleeve or payload["venue"] != venue:
            raise StageReceiptError(f"{parent_stage} parent sleeve/venue does not match the child")
        if stage != "S01" and payload["run_id"] != run_id:
            raise StageReceiptError(f"{parent_stage} parent belongs to a different downstream run identity")
    if stage == "S01":
        s00_payload = parent_payloads["S00"]
        parent_identities = s00_payload["identity_receipts"]
        if not isinstance(parent_identities, dict):
            raise StageReceiptError("S01/S00 identity bindings are malformed")
        for kind in STAGE_IDENTITY_RECEIPT_KINDS["S00"]:
            if parent_identities.get(kind) != child_identity_records.get(kind):
                raise StageReceiptError(f"S01 {kind} identity byte binding does not match its S00 parent")
    if stage == "S04":
        direct_s02 = next(row for row in parent_records if row["stage"] == "S02")
        s03_payload = parent_payloads["S03"]
        s03_parents = _require_list(s03_payload["parents"], name="S03 parents")
        s03_s02 = next(
            (row for row in s03_parents if isinstance(row, dict) and row.get("stage") == "S02"),
            None,
        )
        if not isinstance(s03_s02, dict) or any(
            s03_s02.get(name) != direct_s02.get(name)
            for name in ("run_id", "receipt_id", "receipt_payload_sha256", "file_sha256")
        ):
            raise StageReceiptError("S04 S03 parent does not descend from the same direct S02 receipt")


def build_stage_receipt(
    *,
    sleeve: Sleeve,
    venue: Venue,
    stage: Stage,
    declared_outcome_blind: bool,
    canonical_config_identity_sha256: str,
    registered_scope_sha256: str,
    identity_receipts: Mapping[str, BoundFileInput],
    artifact: ArtifactInput,
    parents: Sequence[BoundFileInput] = (),
    declared_artifact_schema_identity: StageSchemaIdentity | None = None,
    real_money_authorized: bool = False,
    binding_root: str | Path | None = None,
    file_overrides: Mapping[str, str | Path] | None = None,
) -> dict[str, JsonValue]:
    """Build a deterministic diagnostic byte-binding receipt.

    ``declared_outcome_blind``, the artifact schema identity, row count, and key
    hash are bound as caller declarations.  This function does not establish
    that those declarations describe the artifact bytes.
    """

    if sleeve not in SLEEVES:
        raise StageReceiptError(f"sleeve must be one of {sorted(SLEEVES)}")
    if venue not in VENUES:
        raise StageReceiptError(f"venue must be one of {sorted(VENUES)}")
    if stage not in STAGES:
        raise StageReceiptError(f"stage must be one of {list(STAGES)}")
    declared_outcome_blind = _validate_declared_outcome_blind(stage, declared_outcome_blind)
    if not isinstance(real_money_authorized, bool) or real_money_authorized:
        raise StageReceiptError("stage receipts require real_money_authorized=false")

    canonical_config: dict[str, JsonValue] = {
        "identity_sha256": _require_sha256(
            canonical_config_identity_sha256,
            name="canonical_config_identity_sha256",
        ),
        "scope_sha256": _require_sha256(registered_scope_sha256, name="registered_scope_sha256"),
    }
    required_identity_kinds = STAGE_IDENTITY_RECEIPT_KINDS[stage]
    if set(identity_receipts) != set(required_identity_kinds):
        missing = sorted(set(required_identity_kinds) - set(identity_receipts))
        unknown = sorted(set(identity_receipts) - set(required_identity_kinds))
        raise StageReceiptError(f"{stage} identity receipt kinds mismatch; missing={missing}, unknown={unknown}")
    identity_records = {
        kind: _identity_receipt_record(identity_receipts[kind], kind=kind) for kind in required_identity_kinds
    }
    config_identity_payload = _parse_json_object(
        _regular_file_bytes(identity_receipts["config"].path, name="identity_receipts.config"),
        name="config identity receipt",
    )
    _validate_config_identity_payload(
        config_identity_payload,
        sleeve=sleeve,
        identity_sha256=str(canonical_config["identity_sha256"]),
        scope_sha256=str(canonical_config["scope_sha256"]),
    )
    run_id = _run_id(
        sleeve=sleeve,
        venue=venue,
        canonical_config=canonical_config,
        identity_receipts=identity_records,
    )

    parent_by_stage: dict[str, tuple[dict[str, JsonValue], dict[str, JsonValue]]] = {}
    parent_sources_by_stage: dict[str, BoundFileInput] = {}
    for source in parents:
        record, parent_payload = _parent_record(source)
        parent_stage = str(record["stage"])
        if parent_stage in parent_by_stage:
            raise StageReceiptError(f"duplicate {parent_stage} parent receipt")
        _verify_stage_receipt_byte_bindings(
            source.path,
            binding_root=binding_root,
            file_overrides=file_overrides,
            require_current_registry_declarations=True,
        )
        parent_by_stage[parent_stage] = (record, parent_payload)
        parent_sources_by_stage[parent_stage] = source
    if set(parent_by_stage) != set(PARENT_STAGES[stage]):
        raise StageReceiptError(
            f"{stage} requires parents {PARENT_STAGES[stage]}, received {tuple(sorted(parent_by_stage))}"
        )
    ordered_parent_records = [parent_by_stage[name][0] for name in PARENT_STAGES[stage] if name in parent_by_stage]
    parent_payloads = {name: pair[1] for name, pair in parent_by_stage.items()}
    _validate_parent_chain(
        stage=stage,
        sleeve=sleeve,
        venue=venue,
        run_id=run_id,
        child_identity_records=identity_records,
        parent_records=ordered_parent_records,
        parent_payloads=parent_payloads,
    )

    direct_paths = [
        str(_normalise_logical_path(artifact.logical_path, name="artifact.logical_path")),
        *(str(row["logical_path"]) for row in identity_records.values()),
        *(str(row["logical_path"]) for row in ordered_parent_records),
    ]
    if len(direct_paths) != len(set(direct_paths)):
        raise StageReceiptError("direct artifact, identity, and parent logical paths must be unique")

    declared_schema_record = _schema_record(
        sleeve=sleeve,
        stage=stage,
        supplied=declared_artifact_schema_identity,
    )
    artifact_record = _artifact_record(
        artifact,
        declared_schema_identity=declared_schema_record,
    )
    for kind in required_identity_kinds:
        _assert_input_unchanged(
            identity_receipts[kind],
            identity_records[kind],
            name=f"identity receipt {kind}",
            json_identity=True,
        )
    _assert_input_unchanged(
        BoundFileInput(logical_path=artifact.logical_path, path=artifact.path),
        artifact_record,
        name="artifact",
        json_identity=False,
    )
    for parent_stage, parent_record in zip(PARENT_STAGES[stage], ordered_parent_records, strict=True):
        _assert_input_unchanged(
            parent_sources_by_stage[parent_stage],
            parent_record,
            name=f"{parent_stage} parent receipt",
            json_identity=False,
        )

    payload: dict[str, JsonValue] = {
        "receipt_schema_version": STAGE_RECEIPT_SCHEMA_VERSION,
        "receipt_type": STAGE_RECEIPT_TYPE,
        "receipt_scope": RECEIPT_SCOPE,
        "sleeve": sleeve,
        "venue": venue,
        "stage": stage,
        "run_id": run_id,
        "declared_outcome_blind": declared_outcome_blind,
        "outcome_blindness_verified": False,
        "artifact_claims_verified": False,
        "diagnostic_only": True,
        "real_money_authorized": False,
        "outcome_run_authorized": False,
        "provenance_blockers_cleared": False,
        "canonical_config": canonical_config,
        "identity_receipts": cast(JsonValue, identity_records),
        "artifact": artifact_record,
        "parents": cast(JsonValue, ordered_parent_records),
    }
    payload_sha256 = canonical_json_sha256(payload)
    payload["receipt_payload_sha256"] = payload_sha256
    payload["receipt_id"] = f"{run_id}-{stage.lower()}-{payload_sha256[:24]}"
    _validate_stage_receipt_payload(payload)
    return payload


def _validate_file_record(
    value: object,
    *,
    name: str,
    identity_kind: str | None = None,
) -> dict[str, JsonValue]:
    keys = {"logical_path", "file_sha256", "bytes"}
    if identity_kind is not None:
        keys.update({"identity_sha256", "semantic_verification_status"})
    row = _require_exact_keys(value, keys, name=name)
    _normalise_logical_path(row["logical_path"], name=f"{name}.logical_path")
    _require_sha256(row["file_sha256"], name=f"{name}.file_sha256")
    _require_nonnegative_int(row["bytes"], name=f"{name}.bytes")
    if identity_kind is not None:
        _require_sha256(row["identity_sha256"], name=f"{name}.identity_sha256")
        expected_status = (
            CONFIG_IDENTITY_VERIFICATION_STATUS if identity_kind == "config" else OPAQUE_IDENTITY_VERIFICATION_STATUS
        )
        if row["semantic_verification_status"] != expected_status:
            raise StageReceiptError(f"{name}.semantic_verification_status must equal {expected_status!r}")
    return row


def _validate_stage_receipt_payload(payload: dict[str, JsonValue]) -> None:
    _require_exact_keys(payload, _TOP_LEVEL_KEYS, name="stage receipt")
    if payload["receipt_schema_version"] != STAGE_RECEIPT_SCHEMA_VERSION:
        raise StageReceiptError("unsupported stage receipt schema version")
    if payload["receipt_type"] != STAGE_RECEIPT_TYPE or payload["receipt_scope"] != RECEIPT_SCOPE:
        raise StageReceiptError("stage receipt type/scope is invalid")
    sleeve = payload["sleeve"]
    venue = payload["venue"]
    stage = payload["stage"]
    if sleeve not in SLEEVES or venue not in VENUES or stage not in STAGES:
        raise StageReceiptError("stage receipt has an invalid sleeve, venue, or stage")
    _validate_declared_outcome_blind(str(stage), payload["declared_outcome_blind"])
    if payload["diagnostic_only"] is not True:
        raise StageReceiptError("a stage byte binding must retain diagnostic_only=true")
    if payload["artifact_claims_verified"] is not False:
        raise StageReceiptError("artifact declarations cannot be presented as verified")
    if payload["outcome_blindness_verified"] is not False:
        raise StageReceiptError("artifact outcome blindness is not verified by this primitive")
    if payload["real_money_authorized"] is not False:
        raise StageReceiptError("stage receipt must retain real_money_authorized=false")
    if payload["outcome_run_authorized"] is not False or payload["provenance_blockers_cleared"] is not False:
        raise StageReceiptError("a stage receipt cannot authorize outcomes or claim provenance blockers cleared")

    config = _require_exact_keys(
        payload["canonical_config"],
        {"identity_sha256", "scope_sha256"},
        name="canonical_config",
    )
    _require_sha256(config["identity_sha256"], name="canonical_config.identity_sha256")
    _require_sha256(config["scope_sha256"], name="canonical_config.scope_sha256")

    required_identity_kinds = STAGE_IDENTITY_RECEIPT_KINDS[str(stage)]
    identities = _require_exact_keys(
        payload["identity_receipts"],
        set(required_identity_kinds),
        name="identity_receipts",
    )
    identity_rows: dict[str, dict[str, JsonValue]] = {}
    for kind in required_identity_kinds:
        identity_rows[kind] = _validate_file_record(
            identities[kind],
            name=f"identity_receipts.{kind}",
            identity_kind=kind,
        )

    expected_run_id = _run_id(
        sleeve=str(sleeve),
        venue=str(venue),
        canonical_config=config,
        identity_receipts=identity_rows,
    )
    if payload["run_id"] != expected_run_id:
        raise StageReceiptError("stage receipt run_id does not match its bound identity inputs")

    artifact = _require_exact_keys(
        payload["artifact"],
        {
            "logical_path",
            "file_sha256",
            "bytes",
            "declaration_status",
            "declared_row_count",
            "declared_key_projection_sha256",
            "declared_schema_identity",
        },
        name="artifact",
    )
    _normalise_logical_path(artifact["logical_path"], name="artifact.logical_path")
    _require_sha256(artifact["file_sha256"], name="artifact.file_sha256")
    _require_nonnegative_int(artifact["bytes"], name="artifact.bytes")
    if artifact["declaration_status"] != UNVERIFIED_ARTIFACT_DECLARATIONS:
        raise StageReceiptError("artifact declaration status must remain explicitly unverified")
    _require_nonnegative_int(artifact["declared_row_count"], name="artifact.declared_row_count")
    _require_sha256(
        artifact["declared_key_projection_sha256"],
        name="artifact.declared_key_projection_sha256",
    )

    if stage in {"S00", "S01"}:
        if artifact["declared_schema_identity"] is not None:
            raise StageReceiptError(f"{stage} must not contain a declared schema identity")
    else:
        schema = _require_exact_keys(
            artifact["declared_schema_identity"],
            {"schema_id", "schema_version", "schema_sha256"},
            name="artifact.declared_schema_identity",
        )
        for field in ("schema_id", "schema_version"):
            value = schema[field]
            if not isinstance(value, str) or not value or value != value.strip():
                raise StageReceiptError(f"artifact.declared_schema_identity.{field} must be a non-blank string")
        _require_sha256(
            schema["schema_sha256"],
            name="artifact.declared_schema_identity.schema_sha256",
        )

    parents = payload["parents"]
    if not isinstance(parents, list):
        raise StageReceiptError("parents must be a list")
    observed_parent_stages: list[str] = []
    for index, raw in enumerate(parents):
        row = _require_exact_keys(
            raw,
            {
                "logical_path",
                "file_sha256",
                "bytes",
                "stage",
                "run_id",
                "receipt_id",
                "receipt_payload_sha256",
            },
            name=f"parents[{index}]",
        )
        _normalise_logical_path(row["logical_path"], name=f"parents[{index}].logical_path")
        _require_sha256(row["file_sha256"], name=f"parents[{index}].file_sha256")
        _require_nonnegative_int(row["bytes"], name=f"parents[{index}].bytes")
        _require_sha256(row["receipt_payload_sha256"], name=f"parents[{index}].receipt_payload_sha256")
        if stage != "S01" and row["run_id"] != expected_run_id:
            raise StageReceiptError(f"parents[{index}] has a different run_id")
        observed_parent_stages.append(str(row["stage"]))
    if tuple(observed_parent_stages) != PARENT_STAGES[str(stage)]:
        raise StageReceiptError(f"{stage} parent stages must equal {PARENT_STAGES[str(stage)]}")

    direct_paths = [
        str(artifact["logical_path"]),
        *(str(identity_rows[kind]["logical_path"]) for kind in required_identity_kinds),
        *(str(row["logical_path"]) for row in parents if isinstance(row, dict)),
    ]
    if len(direct_paths) != len(set(direct_paths)):
        raise StageReceiptError("direct bound logical paths are ambiguous")

    payload_hash = payload["receipt_payload_sha256"]
    _require_sha256(payload_hash, name="receipt_payload_sha256")
    unhashed = dict(payload)
    unhashed.pop("receipt_id")
    unhashed.pop("receipt_payload_sha256")
    expected_payload_hash = canonical_json_sha256(unhashed)
    if payload_hash != expected_payload_hash:
        raise StageReceiptError("stage receipt payload SHA-256 mismatch")
    expected_receipt_id = f"{expected_run_id}-{str(stage).lower()}-{expected_payload_hash[:24]}"
    if payload["receipt_id"] != expected_receipt_id:
        raise StageReceiptError("stage receipt_id does not match its payload")


def _parse_stage_receipt_bytes(data: bytes, *, name: str) -> dict[str, JsonValue]:
    payload = _parse_json_object(data, name=name)
    expected_bytes = canonical_json_bytes(payload) + b"\n"
    if data != expected_bytes:
        raise StageReceiptError(f"{name} is not in canonical byte representation")
    _validate_stage_receipt_payload(payload)
    return payload


def load_stage_receipt(path: str | Path) -> dict[str, JsonValue]:
    resolved = Path(path)
    data = _regular_file_bytes(resolved, name="stage receipt")
    return _parse_stage_receipt_bytes(data, name=f"stage receipt {resolved}")


def render_stage_receipt(receipt: dict[str, JsonValue]) -> bytes:
    ready = _strict_json_value(receipt, location="stage receipt")
    if not isinstance(ready, dict):
        raise StageReceiptError("stage receipt must be an object")
    _validate_stage_receipt_payload(ready)
    return canonical_json_bytes(ready) + b"\n"


def _existing_identical(path: Path, expected: bytes) -> bool:
    try:
        actual = _regular_file_bytes(path, name="existing stage receipt")
    except StageReceiptError:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        raise
    if actual != expected:
        raise StageReceiptError(f"refusing to overwrite non-identical stage receipt: {path}")
    return True


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_stage_receipt(path: str | Path, receipt: dict[str, JsonValue]) -> ReceiptWriteResult:
    """Atomically create a receipt; an existing file is reusable only byte-for-byte."""

    destination = Path(path)
    data = render_stage_receipt(receipt)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _existing_identical(destination, data):
        return ReceiptWriteResult(
            path=destination,
            receipt_id=str(receipt["receipt_id"]),
            file_sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            reused=True,
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
            linked = True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise StageReceiptError(f"failed to atomically create stage receipt: {destination}") from exc
            if not _existing_identical(destination, data):  # pragma: no cover - helper raises on mismatch
                raise StageReceiptError(f"receipt creation raced with a non-identical writer: {destination}")
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return ReceiptWriteResult(
        path=destination,
        receipt_id=str(receipt["receipt_id"]),
        file_sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        reused=not linked,
    )


def _resolve_bound_path(
    logical_path: str,
    *,
    binding_root: Path,
    file_overrides: Mapping[str, Path],
) -> Path:
    override = file_overrides.get(logical_path)
    return override if override is not None else binding_root / PurePosixPath(logical_path)


def _verify_file_record(
    record: Mapping[str, JsonValue],
    *,
    name: str,
    binding_root: Path,
    file_overrides: Mapping[str, Path],
    require_json_identity: bool,
) -> Path:
    logical_path = str(record["logical_path"])
    path = _resolve_bound_path(logical_path, binding_root=binding_root, file_overrides=file_overrides)
    data = _regular_file_bytes(path, name=name)
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["file_sha256"]:
        raise StageReceiptError(f"{name} current bytes do not match the stage receipt: {logical_path}")
    if require_json_identity:
        payload = _parse_json_object(data, name=name)
        if canonical_json_sha256(payload) != record["identity_sha256"]:
            raise StageReceiptError(f"{name} JSON identity does not match the stage receipt: {logical_path}")
    return path


def _verify_stage_receipt_byte_bindings(
    path: str | Path,
    *,
    binding_root: str | Path | None = None,
    file_overrides: Mapping[str, str | Path] | None = None,
    require_current_registry_declarations: bool,
) -> ByteBindingVerification:
    """Verify byte bindings, optionally checking current schema declarations."""

    receipt_path = Path(path)
    root = Path(binding_root) if binding_root is not None else receipt_path.parent
    overrides = {name: Path(value) for name, value in (file_overrides or {}).items()}
    for logical_path in overrides:
        _normalise_logical_path(logical_path, name="file_overrides key")
    verified_receipts: dict[Path, str] = {}
    active: set[Path] = set()
    verified_files: set[Path] = set()

    def verify_one(current_path: Path) -> dict[str, JsonValue]:
        resolved_current = current_path.resolve(strict=False)
        if resolved_current in active:
            raise StageReceiptError("stage receipt parent cycle detected")
        if resolved_current in verified_receipts:
            return load_stage_receipt(current_path)
        active.add(resolved_current)
        try:
            payload = load_stage_receipt(current_path)
            if require_current_registry_declarations:
                _validate_current_registry_declaration(
                    payload,
                    context="parent-chain receipt",
                )
            identities = payload["identity_receipts"]
            assert isinstance(identities, dict)
            required_identity_kinds = STAGE_IDENTITY_RECEIPT_KINDS[str(payload["stage"])]
            identity_rows: dict[str, dict[str, JsonValue]] = {}
            for kind in required_identity_kinds:
                record = identities[kind]
                assert isinstance(record, dict)
                identity_rows[kind] = record
                identity_path = _verify_file_record(
                    record,
                    name=f"identity receipt {kind}",
                    binding_root=root,
                    file_overrides=overrides,
                    require_json_identity=True,
                )
                verified_files.add(identity_path.resolve(strict=False))

            artifact = payload["artifact"]
            assert isinstance(artifact, dict)
            artifact_path = _verify_file_record(
                artifact,
                name=f"{payload['stage']} artifact",
                binding_root=root,
                file_overrides=overrides,
                require_json_identity=False,
            )
            verified_files.add(artifact_path.resolve(strict=False))

            parents = payload["parents"]
            assert isinstance(parents, list)
            parent_payload_by_stage: dict[str, dict[str, JsonValue]] = {}
            for raw in parents:
                assert isinstance(raw, dict)
                parent_path = _verify_file_record(
                    raw,
                    name=f"{raw['stage']} parent receipt",
                    binding_root=root,
                    file_overrides=overrides,
                    require_json_identity=False,
                )
                verified_files.add(parent_path.resolve(strict=False))
                parent_payload = verify_one(parent_path)
                for field in ("stage", "run_id", "receipt_id", "receipt_payload_sha256"):
                    if parent_payload[field] != raw[field]:
                        raise StageReceiptError(f"parent receipt {field} disagrees with the child binding")
                parent_payload_by_stage[str(raw["stage"])] = parent_payload

            _validate_parent_chain(
                stage=str(payload["stage"]),
                sleeve=str(payload["sleeve"]),
                venue=str(payload["venue"]),
                run_id=str(payload["run_id"]),
                child_identity_records=identity_rows,
                parent_records=[raw for raw in parents if isinstance(raw, dict)],
                parent_payloads=parent_payload_by_stage,
            )

            verified_receipts[resolved_current] = str(payload["receipt_id"])
            return payload
        finally:
            active.remove(resolved_current)

    receipt = verify_one(receipt_path)
    return ByteBindingVerification(
        receipt_id=str(receipt["receipt_id"]),
        run_id=str(receipt["run_id"]),
        sleeve=str(receipt["sleeve"]),
        venue=str(receipt["venue"]),
        stage=str(receipt["stage"]),
        byte_verified_receipt_count=len(verified_receipts),
        byte_verified_bound_file_count=len(verified_files),
        current_registry_or_config_factories_consulted=(require_current_registry_declarations),
    )


def verify_stage_receipt_byte_bindings(
    path: str | Path,
    *,
    binding_root: str | Path | None = None,
    file_overrides: Mapping[str, str | Path] | None = None,
) -> ByteBindingVerification:
    """Verify canonical receipt bytes and direct/transitive file hashes only.

    Success does not validate artifact schema, row count, key projection,
    outcome blindness, provenance, or strategy semantics.  This archival path
    deliberately uses the recorded declarations rather than the mutable current
    schema registry or config factories.
    """

    return _verify_stage_receipt_byte_bindings(
        path,
        binding_root=binding_root,
        file_overrides=file_overrides,
        require_current_registry_declarations=False,
    )


def canonical_stage_key_projection_sha256(frame: pl.DataFrame, schema_id: str) -> str:
    """Hash the registered keys as sorted canonical JSONL objects.

    The projection contains no header.  Each row is a strict canonical JSON
    object followed by ``\n``; rows are sorted by the registered key tuple and
    an empty projection is the SHA-256 of zero bytes.  This convention is
    independent of Parquet/IPC encoding and is suitable for the declaration in
    a v2 byte-binding receipt.
    """

    try:
        schema = ARTIFACT_SCHEMAS[schema_id]
    except KeyError as exc:
        raise StageReceiptError(f"unknown artifact schema for key projection: {schema_id}") from exc
    keys = list(schema.key_fields)
    missing = sorted(set(keys) - set(frame.columns))
    if missing:
        raise StageReceiptError(f"artifact key projection is missing registered keys: {missing}")
    expected_dtypes = artifact_polars_schema(schema_id)
    mismatched = {
        name: {"expected": str(expected_dtypes[name]), "actual": str(frame.schema[name])}
        for name in keys
        if frame.schema[name] != expected_dtypes[name]
    }
    if mismatched:
        raise StageReceiptError(f"artifact key projection has invalid dtypes: {mismatched}")
    ordered = frame.select(keys).sort(keys)
    digest = hashlib.sha256()
    for row in ordered.iter_rows(named=True):
        digest.update(canonical_json_bytes(row))
        digest.update(b"\n")
    return digest.hexdigest()


def _read_parquet_artifact(data: bytes) -> pl.DataFrame:
    return pl.read_parquet(io.BytesIO(data), memory_map=False)


def _read_arrow_ipc_artifact(data: bytes) -> pl.DataFrame:
    return pl.read_ipc(io.BytesIO(data), memory_map=False)


_ARTIFACT_FORMAT_READERS = MappingProxyType(
    {
        "parquet": _read_parquet_artifact,
        "arrow_ipc": _read_arrow_ipc_artifact,
    }
)


def _artifact_format(logical_path: str) -> str:
    suffix = PurePosixPath(logical_path).suffix
    artifact_format = _ARTIFACT_FORMAT_BY_SUFFIX.get(suffix)
    if artifact_format is None:
        raise StageReceiptError(
            "unsupported semantic artifact format; normalized logical-path suffix must be one of "
            f"{list(SUPPORTED_SEMANTIC_ARTIFACT_SUFFIXES)}, got {suffix!r}"
        )
    return artifact_format


def _parse_semantic_artifact(data: bytes, *, logical_path: str) -> tuple[str, pl.DataFrame]:
    artifact_format = _artifact_format(logical_path)
    try:
        frame = _ARTIFACT_FORMAT_READERS[artifact_format](data)
    except (OSError, TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise StageReceiptError(f"{logical_path} is not a readable {artifact_format} artifact: {exc}") from exc
    return artifact_format, frame


def _reject_rows(frame: pl.DataFrame, predicate: pl.Expr, *, message: str) -> None:
    if frame.is_empty():
        return
    try:
        # A nullable comparison that was not explicitly excluded is a failed
        # semantic check, not permission to skip the row.  Individual
        # predicates must guard nullable-but-valid states before this boundary.
        invalid = frame.filter(predicate.fill_null(True))
    except (TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise StageReceiptError(f"could not evaluate {message}: {exc}") from exc
    if not invalid.is_empty():
        raise StageReceiptError(f"{message}; invalid_row_count={invalid.height}")


def _not_close(actual: str, expected: pl.Expr, *, tolerance: float = 1e-12) -> pl.Expr:
    difference = (pl.col(actual) - expected).abs()
    scale = pl.max_horizontal(pl.lit(1.0), pl.col(actual).abs(), expected.abs())
    return difference > tolerance * scale


def _require_exact_registered_artifact(
    frame: pl.DataFrame,
    *,
    schema_id: str,
    venue: str,
) -> tuple[str, ...]:
    artifact = ARTIFACT_SCHEMAS[schema_id]
    expected = artifact_polars_schema(schema_id)
    expected_names = tuple(expected)
    actual_names = tuple(frame.columns)
    if actual_names != expected_names:
        missing = sorted(set(expected_names) - set(actual_names))
        unknown = sorted(set(actual_names) - set(expected_names))
        raise StageReceiptError(
            f"artifact {schema_id} column order/projection mismatch; missing={missing}, unknown={unknown}"
        )
    mismatched = {
        name: {"expected": str(dtype), "actual": str(frame.schema[name])}
        for name, dtype in expected.items()
        if frame.schema[name] != dtype
    }
    if mismatched:
        raise StageReceiptError(f"artifact {schema_id} has invalid physical dtypes: {mismatched}")
    try:
        projected = project_artifact_frame(frame, schema_id)
    except ArtifactProjectionError as exc:
        raise StageReceiptError(f"artifact {schema_id} failed registered projection: {exc}") from exc
    keys = list(artifact.key_fields)
    if not projected.select(keys).equals(projected.select(keys).sort(keys)):
        raise StageReceiptError(f"artifact {schema_id} rows are not in canonical registered-key order")
    _reject_rows(
        projected,
        (pl.col("venue") != venue)
        | (pl.col("venue").str.strip_chars() != pl.col("venue"))
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol").str.strip_chars() != pl.col("symbol"))
        | (pl.col("canonical_instrument_id").str.strip_chars() == "")
        | (pl.col("canonical_instrument_id").str.strip_chars() != pl.col("canonical_instrument_id")),
        message=f"artifact {schema_id} violates receipt venue or normalized identity semantics",
    )
    key_time = "decision_ts_ms" if "decision_ts_ms" in keys else "signal_ts_ms"
    _reject_rows(
        projected,
        (pl.col(key_time) < 0) | ((pl.col(key_time) % (60 * 60 * 1000)) != 0),
        message=f"artifact {schema_id} has negative or off-grid registered timestamps",
    )
    float_columns = [field.name for field in artifact.fields if field.dtype == "float64"]
    if float_columns:
        _reject_rows(
            projected,
            pl.any_horizontal(pl.col(name).is_not_null() & ~pl.col(name).is_finite() for name in float_columns),
            message=f"artifact {schema_id} contains non-finite float values",
        )
    return (
        "exact_registered_column_order",
        "exact_physical_dtypes",
        "registered_nullability_and_unique_keys",
        "canonical_registered_key_order",
        "receipt_venue_and_normalized_identity",
        "finite_or_null_float_values",
    )


def _identity_config_values(config_identity: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    canonical = config_identity.get("canonical_config")
    if not isinstance(canonical, dict) or not isinstance(canonical.get("config"), dict):
        raise StageReceiptError("config identity canonical_config.config is malformed")
    return cast(dict[str, JsonValue], canonical["config"])


def _validate_ohlc(frame: pl.DataFrame, *, schema_id: str) -> None:
    _reject_rows(
        frame,
        (pl.col("open") <= 0.0)
        | (pl.col("high") <= 0.0)
        | (pl.col("low") <= 0.0)
        | (pl.col("close") <= 0.0)
        | (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("low") > pl.col("high")),
        message=f"artifact {schema_id} violates finite positive OHLC ordering",
    )


def _validate_continuous_s02(
    frame: pl.DataFrame,
    *,
    config_identity: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    config = _identity_config_values(config_identity)
    quantile = config.get("rmom_quantile")
    entry_delay_hours = config.get("entry_confirm_delay_hours")
    if isinstance(quantile, bool) or not isinstance(quantile, (int, float)):
        raise StageReceiptError("canonical CONTINUOUS rmom_quantile is not numeric")
    if isinstance(entry_delay_hours, bool) or not isinstance(entry_delay_hours, int) or entry_delay_hours <= 0:
        raise StageReceiptError("canonical CONTINUOUS entry_confirm_delay_hours is not a positive integer")
    entry_delay_ms = entry_delay_hours * _MS_PER_HOUR
    expected_data_availability = (
        pl.when(pl.col("rmom_data_available_ts_ms").is_not_null())
        .then(pl.max_horizontal("feature_data_available_ts_ms", "rmom_data_available_ts_ms"))
        .otherwise(pl.col("feature_data_available_ts_ms"))
    )
    _reject_rows(
        frame,
        (pl.col("signal_ts_ms") < 0)
        | ((pl.col("signal_ts_ms") % _MS_PER_HOUR) != 0)
        | (pl.col("decision_ts_ms") != pl.col("signal_ts_ms") + entry_delay_ms)
        | (pl.col("signal_bar_close_ts_ms") != pl.col("decision_ts_ms"))
        | (pl.col("feature_data_available_ts_ms") != pl.col("decision_ts_ms"))
        | (pl.col("data_available_ts_ms") != expected_data_availability)
        | (pl.col("data_available_ts_ms") > pl.col("decision_ts_ms"))
        | ((pl.col("rmom_source_day_ts_ms") % _MS_PER_DAY) != 0)
        | (
            pl.col("rmom_data_available_ts_ms").is_not_null()
            & (pl.col("rmom_data_available_ts_ms") > pl.col("decision_ts_ms"))
        ),
        message="CONTINUOUS S02 violates registered signal/decision/availability timing",
    )
    _validate_ohlc(frame, schema_id=CONTINUOUS_SIGNAL_SCHEMA_ID)
    trigger_p3 = pl.col("trigger_turn3_pop3")
    trigger_p4p3 = pl.col("trigger_turn4_pop3")
    trigger_p4p5 = pl.col("trigger_turn4_pop5")
    expected_component_mask = trigger_p3.cast(pl.Int8) + 2 * trigger_p4p3.cast(pl.Int8) + 4 * trigger_p4p5.cast(pl.Int8)
    expected_component_count = trigger_p3.cast(pl.Int8) + trigger_p4p3.cast(pl.Int8) + trigger_p4p5.cast(pl.Int8)
    expected_component_tags = (
        pl.when(trigger_p4p5)
        .then(pl.lit("p3,p4p3,p4p5"))
        .when(trigger_p4p3)
        .then(pl.lit("p3,p4p3"))
        .when(trigger_p3)
        .then(pl.lit("p3"))
        .otherwise(pl.lit(""))
    )
    expected_weight = (
        trigger_p3.cast(pl.Float64) * (1.0 / 3.0)
        + trigger_p4p3.cast(pl.Float64) * (2.0 / 9.0)
        + trigger_p4p5.cast(pl.Float64) * (4.0 / 9.0)
    )
    expected_current_mask = pl.when(pl.col("current_q25_d9")).then(expected_component_mask).otherwise(0)
    _reject_rows(
        frame,
        (pl.col("turnover_quote_available") != pl.col("turnover_quote").is_not_null())
        | (pl.col("turnover_quote").is_not_null() & (pl.col("turnover_quote") < 0.0))
        | (pl.col("rmom_stable_available") & pl.col("rmom_is_provisional"))
        | (pl.col("rmom_stable_available") & ~pl.col("rmom_source_row_present"))
        | (pl.col("rmom_stable_available") & ~pl.col("rmom_present"))
        | (pl.col("rmom_stable_available") & pl.col("residual_momentum").is_null())
        | (pl.col("rmom_stable_available") & pl.col("rmom_data_available_ts_ms").is_null())
        | _not_close("current_rmom_quantile_cutoff", pl.lit(float(quantile)))
        | (trigger_p4p3 & ~trigger_p3)
        | (trigger_p4p5 & ~trigger_p4p3)
        | (pl.col("trigger_any_current_component") != trigger_p3)
        | (pl.col("component_mask") != expected_component_mask)
        | (pl.col("component_membership_count") != expected_component_count)
        | (pl.col("component_tags") != expected_component_tags)
        | _not_close("implied_tier_weight", expected_weight)
        | (pl.col("unique_decision_id") != pl.concat_str("symbol", pl.lit("|"), pl.col("signal_ts_ms").cast(pl.String)))
        | (pl.col("simultaneous_trigger_decision_count") != trigger_p3.cast(pl.Int64).sum().over("signal_ts_ms"))
        | (pl.col("current_p3_component_membership") != (pl.col("current_q25_d9") & trigger_p3))
        | (pl.col("current_p4p3_component_membership") != (pl.col("current_q25_d9") & trigger_p4p3))
        | (pl.col("current_p4p5_component_membership") != (pl.col("current_q25_d9") & trigger_p4p5))
        | (pl.col("current_component_mask_before_liquidity") != expected_current_mask)
        | (pl.col("btc_uptrend_known") == pl.col("btc_uptrend_unknown"))
        | (pl.col("btc_uptrend_pass") & pl.col("btc_uptrend_fail"))
        | (pl.col("btc_uptrend_unknown") & (pl.col("btc_uptrend_pass") | pl.col("btc_uptrend_fail")))
        | (pl.col("btc_uptrend_known") & ~(pl.col("btc_uptrend_pass") | pl.col("btc_uptrend_fail"))),
        message="CONTINUOUS S02 violates registered source/static-state semantics",
    )
    count_columns = [
        field.name
        for field in ARTIFACT_SCHEMAS[CONTINUOUS_SIGNAL_SCHEMA_ID].fields
        if (field.name.endswith("_count") or field.name.endswith("_peer_count")) and field.dtype in {"int64", "uint32"}
    ]
    _reject_rows(
        frame,
        pl.any_horizontal(pl.col(name).is_not_null() & (pl.col(name) < 0) for name in count_columns),
        message="CONTINUOUS S02 contains negative registered counts",
    )
    return (
        "continuous_s02_signal_decision_availability_timing",
        "continuous_s02_ohlc_and_turnover_semantics",
        "continuous_s02_rmom_static_mask_and_btc_state_semantics",
        "continuous_s02_nonnegative_support_counts",
    )


def _validate_long_s02(
    frame: pl.DataFrame,
    *,
    config_identity: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    config = _identity_config_values(config_identity)
    max_hold_days = config.get("fc_max_hold_days")
    use_atr = config.get("fc_use_atr_exits")
    universe_size = config.get("universe_size")
    min_listing_history_days = config.get("min_listing_history_days")
    universe_volume_window_days = config.get("universe_volume_window_days")
    if isinstance(max_hold_days, bool) or not isinstance(max_hold_days, int):
        raise StageReceiptError("canonical LONG fc_max_hold_days is not an integer")
    if not isinstance(use_atr, bool):
        raise StageReceiptError("canonical LONG fc_use_atr_exits is not a boolean")
    if (
        isinstance(universe_size, bool)
        or not isinstance(universe_size, int)
        or universe_size <= 0
        or isinstance(min_listing_history_days, bool)
        or not isinstance(min_listing_history_days, int)
        or min_listing_history_days < 0
        or isinstance(universe_volume_window_days, bool)
        or not isinstance(universe_volume_window_days, int)
        or universe_volume_window_days <= 0
    ):
        raise StageReceiptError("canonical LONG universe membership config is malformed")
    universe_turnover_column = f"turnover_median_{universe_volume_window_days}d"
    if universe_turnover_column not in frame.columns:
        raise StageReceiptError(
            f"registered LONG S02 schema lacks config-derived universe turnover column {universe_turnover_column!r}"
        )
    availability_columns = (
        "signal_feature_available_ts_ms",
        "daily_bar_available_ts_ms",
        "btc_context_available_ts_ms",
        "eth_context_available_ts_ms",
        "btc_month_context_available_ts_ms",
    )
    _reject_rows(
        frame,
        ((pl.col("signal_ts_ms") % _MS_PER_DAY) != 0)
        | pl.any_horizontal(
            pl.col(name).is_not_null() & (pl.col(name) > pl.col("signal_ts_ms")) for name in availability_columns
        )
        | pl.col("daily_bar_available_ts_ms").is_null()
        | pl.col("signal_feature_available_ts_ms").is_null()
        | (
            pl.col("signal_feature_available_ts_ms")
            != pl.max_horizontal(
                "daily_bar_available_ts_ms",
                "btc_context_available_ts_ms",
                "eth_context_available_ts_ms",
                "btc_month_context_available_ts_ms",
            )
        )
        | (pl.col("btc_regime_available") & pl.col("btc_context_available_ts_ms").is_null())
        | (pl.col("eth_regime_available") & pl.col("eth_context_available_ts_ms").is_null())
        | (pl.col("btc_month_regime_available") & pl.col("btc_month_context_available_ts_ms").is_null()),
        message="LONG S02 violates daily signal or source-availability timing",
    )
    _validate_ohlc(frame, schema_id=LONG_SIGNAL_SCHEMA_ID)
    expected_bitmask = (
        pl.col("fc_trigger_1d").cast(pl.Int8)
        + 2 * pl.col("fc_trigger_3d").cast(pl.Int8)
        + 4 * pl.col("fc_trigger_7d").cast(pl.Int8)
    )
    fallback_expected = (~pl.col("fc_atr_exit_available")) if use_atr else pl.lit(False)
    _reject_rows(
        frame,
        pl.col("global_lsr").is_not_null()
        | pl.col("oi_chg_7d").is_not_null()
        | (pl.col("long_feature_tape_schema_version") != "long_a0_signal_feature_v3")
        | (pl.col("fc_exit_max_hold_hours") != max_hold_days * 24)
        | (pl.col("fc_trigger_bitmask") != expected_bitmask)
        | (pl.col("fc_all_trigger") != (pl.col("fc_trigger_bitmask") != 0))
        | (pl.col("classifier_eligible") != pl.col("classifier_selected"))
        | (pl.col("signal_bar_present") != pl.col("signal_bar_complete"))
        | (pl.col("signal_bar_present") != pl.col("signal_close_hourly").is_not_null())
        | (pl.col("fc_atr_fallback_used") != fallback_expected)
        | (pl.col("fc_exit_stop_pct") <= 0.0)
        | (pl.col("fc_exit_stop_pct") >= 1.0)
        | (pl.col("fc_exit_take_profit_pct") <= 0.0),
        message="LONG S02 violates frozen trigger/classifier/exit/schema semantics",
    )
    count_columns = [
        field.name
        for field in ARTIFACT_SCHEMAS[LONG_SIGNAL_SCHEMA_ID].fields
        if (field.name.endswith("_count") or field.name.endswith("_peer_count")) and field.dtype in {"int64", "uint32"}
    ]
    _reject_rows(
        frame,
        pl.any_horizontal(pl.col(name).is_not_null() & (pl.col(name) < 0) for name in count_columns),
        message="LONG S02 contains negative registered counts",
    )
    rank_predicates: list[pl.Expr] = []
    for rank_column, value_column in (
        ("today_volume_rank", "turnover_quote"),
        ("universe_rank", universe_turnover_column),
    ):
        population = pl.col(f"{rank_column}_population_peer_count")
        rankable = pl.col(f"{rank_column}_rankable_peer_count")
        missing = pl.col(f"{rank_column}_missing_peer_count")
        tie_count = pl.col(f"{rank_column}_tie_count")
        value = pl.col(value_column)
        finite_value = value.is_not_null() & value.is_finite()
        expected_rank = value.rank(method="ordinal", descending=True).over("signal_ts_ms").cast(pl.UInt32)
        expected_population = pl.len().over("signal_ts_ms")
        expected_rankable = finite_value.cast(pl.Int64).sum().over("signal_ts_ms")
        expected_tie_count = pl.when(finite_value).then(pl.len().over(["signal_ts_ms", value_column])).otherwise(None)
        rank_predicates.extend(
            (
                population != expected_population,
                rankable != expected_rankable,
                missing != population - rankable,
                ~pl.col(rank_column).eq_missing(expected_rank),
                ~tie_count.eq_missing(expected_tie_count),
                pl.col(f"{rank_column}_tie_method") != "ordinal_descending_value_then_symbol_ascending",
                pl.col(f"{rank_column}_denominator_rule") != "supplied_signal_ts_population",
            )
        )
    expected_membership = (
        (pl.col("universe_rank") <= universe_size)
        & (pl.col("symbol_age_days") >= min_listing_history_days)
        & pl.col(universe_turnover_column).is_not_null()
        & pl.col(universe_turnover_column).is_finite()
    ).fill_null(False)
    rank_predicates.append(pl.col("in_universe") != expected_membership)
    _reject_rows(
        frame,
        pl.any_horizontal(rank_predicates),
        message="LONG S02 violates exact rank metadata or configured universe-membership semantics",
    )
    return (
        "long_s02_daily_signal_and_availability_timing",
        "long_s02_ohlc_semantics",
        "long_s02_frozen_trigger_classifier_exit_and_schema_semantics",
        "long_s02_tier_c_forced_null_and_nonnegative_support_counts",
        "long_s02_exact_rank_metadata_and_configured_universe_membership",
    )


def _validate_continuous_s03(
    frame: pl.DataFrame,
    *,
    config_identity: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    entry_delay_hours = _identity_config_values(config_identity).get("entry_confirm_delay_hours")
    if isinstance(entry_delay_hours, bool) or not isinstance(entry_delay_hours, int) or entry_delay_hours <= 0:
        raise StageReceiptError("canonical CONTINUOUS entry_confirm_delay_hours is not a positive integer")
    decision_delay_ms = entry_delay_hours * _MS_PER_HOUR
    available = pl.col("entry_anchor_available")
    _reject_rows(
        frame,
        (pl.col("signal_ts_ms") < 0)
        | ((pl.col("signal_ts_ms") % _MS_PER_HOUR) != 0)
        | (pl.col("decision_ts_ms") != pl.col("signal_ts_ms") + decision_delay_ms)
        | (pl.col("entry_bar_start_ts_ms") != pl.col("decision_ts_ms"))
        | (
            available
            & (
                (pl.col("entry_anchor_ts_ms") != pl.col("decision_ts_ms") + _MS_PER_HOUR)
                | pl.col("entry_price").is_null()
                | (pl.col("entry_price") <= 0.0)
                | pl.col("missing_anchor_reason").is_not_null()
            )
        )
        | (
            ~available
            & (
                pl.col("entry_anchor_ts_ms").is_not_null()
                | pl.col("entry_price").is_not_null()
                | (pl.col("missing_anchor_reason") != "no_next_entry_bar")
            )
        ),
        message="CONTINUOUS S03 violates frozen timestamp/anchor/reason semantics",
    )
    return ("continuous_s03_frozen_next_close_anchor_semantics",)


def _validate_continuous_s04(frame: pl.DataFrame) -> tuple[str, ...]:
    predicates: list[pl.Expr] = []
    for horizon in (1, 24, 72):
        observed = pl.col(f"path_{horizon}h_observed_hours")
        available = pl.col(f"path_{horizon}h_available")
        complete = pl.col(f"path_{horizon}h_complete")
        endpoint = pl.col(f"path_{horizon}h_close_ts_ms")
        underlying = pl.col(f"path_{horizon}h_underlying_return")
        directional = pl.col(f"path_{horizon}h_short_directional_return")
        reason = pl.col(f"path_{horizon}h_missing_reason")
        predicates.append(
            (observed < 0)
            | (observed > horizon)
            | (complete != (available & (observed == horizon)))
            | (endpoint.is_not_null() & (endpoint != pl.col("decision_ts_ms") + (horizon + 1) * _MS_PER_HOUR))
            | (complete & (underlying.is_null() | directional.is_null() | reason.is_not_null()))
            | (~complete & (underlying.is_not_null() | directional.is_not_null() | reason.is_null()))
            | (underlying.is_not_null() & _not_close(f"path_{horizon}h_short_directional_return", -underlying))
            | ~pl.col(f"path_{horizon}h_hourly_extrema_interval_censored")
        )
        if horizon in (24, 72):
            predicates.append(
                complete
                & (pl.col(f"path_{horizon}h_short_mfe").is_null() | pl.col(f"path_{horizon}h_short_mae").is_null())
                | (
                    ~complete
                    & (
                        pl.col(f"path_{horizon}h_short_mfe").is_not_null()
                        | pl.col(f"path_{horizon}h_short_mae").is_not_null()
                    )
                )
            )
    all_complete = pl.all_horizontal(pl.col(f"path_{horizon}h_complete") for horizon in (1, 24, 72))
    predicates.extend(
        (
            pl.col("path_1h_observed_hours") > pl.col("path_24h_observed_hours"),
            pl.col("path_24h_observed_hours") > pl.col("path_72h_observed_hours"),
            pl.col("path_72h_complete") & ~pl.col("path_24h_complete"),
            pl.col("path_24h_complete") & ~pl.col("path_1h_complete"),
            pl.col("path_all_minimal_labels_complete") != all_complete,
            all_complete & pl.col("missing_path_reason").is_not_null(),
            ~all_complete & pl.col("missing_path_reason").is_null(),
        )
    )
    _reject_rows(
        frame,
        pl.any_horizontal(predicates),
        message="CONTINUOUS S04 violates frozen minimal-label support/return/reason semantics",
    )
    return ("continuous_s04_frozen_horizon_support_return_and_reason_semantics",)


def _validate_long_s03(
    frame: pl.DataFrame,
    *,
    config_identity: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    config = _identity_config_values(config_identity)
    delay = config.get("entry_delay_hours")
    deadline = config.get("fc_sniper_deadline_hours")
    retrace = config.get("fc_sniper_retrace_pct")
    use_sniper = config.get("fc_use_sniper_entry")
    use_atr_retrace = config.get("fc_sniper_use_atr_retrace")
    skip_on_no_retrace = config.get("fc_sniper_skip_on_no_retrace")
    if (
        isinstance(delay, bool)
        or not isinstance(delay, int)
        or isinstance(deadline, bool)
        or not isinstance(deadline, int)
        or isinstance(retrace, bool)
        or not isinstance(retrace, (int, float))
        or not isinstance(use_sniper, bool)
        or not isinstance(use_atr_retrace, bool)
        or not isinstance(skip_on_no_retrace, bool)
    ):
        raise StageReceiptError("canonical LONG entry-policy config is malformed")
    first_hour = max(1, delay)
    deadline_hour = max(first_hour, deadline)
    if not use_sniper or use_atr_retrace or skip_on_no_retrace or first_hour < 1 or deadline_hour > 6:
        raise StageReceiptError(
            "registered LONG S03 semantic verifier supports the frozen h1..h6 fixed-retrace fallthrough policy"
        )
    common = pl.col("common_entry_available")
    current = pl.col("current_entry_available")
    reason = pl.col("current_entry_reason")
    initial_missing = (reason == "initial_entry_bar_missing").fill_null(False)
    signal_missing = (reason == "signal_bar_missing").fill_null(False)
    retrace_entry = (reason == "sniper_retrace").fill_null(False)
    deadline_fallthrough = (reason == "sniper_deadline_fallthrough").fill_null(False)
    deadline_missing = (reason == "sniper_deadline_missing").fill_null(False)
    scan_state = retrace_entry | deadline_fallthrough | deadline_missing
    mask = pl.col("current_entry_scan_missing_hour_bitmask")
    scan_end = pl.col("current_entry_scan_end_hour")
    observed_low_hour = pl.col("current_entry_intrabar_low_observed_first_hour_nonfill")
    authoritative_low_hour = pl.col("current_entry_intrabar_low_first_hour_nonfill")
    missing_before_observed = pl.lit(False)
    mask_outside_scan = pl.lit(False)
    observed_on_missing_bar = pl.lit(False)
    retrace_on_missing_bar = pl.lit(False)
    for hour in range(1, 7):
        bit_set = ((mask // (1 << (hour - 1))) % 2) == 1
        mask_outside_scan |= bit_set & (
            ~scan_state
            | (hour < first_hour)
            | (retrace_entry & (pl.lit(hour) > pl.col("current_entry_hour")))
            | ((deadline_fallthrough | deadline_missing) & (hour > deadline_hour))
        )
        observed_on_missing_bar |= bit_set & (observed_low_hour == hour).fill_null(False)
        retrace_on_missing_bar |= bit_set & (retrace_entry & (pl.col("current_entry_hour") == hour))
        missing_before_observed |= bit_set & (observed_low_hour > hour).fill_null(False)
    expected_authoritative_low_hour = (
        pl.when(observed_low_hour.is_not_null() & ~missing_before_observed).then(observed_low_hour).otherwise(None)
    )
    expected_low_touch = (
        pl.when(observed_low_hour.is_not_null())
        .then(pl.lit(True))
        .when(scan_state & pl.col("current_entry_retrace_threshold").is_not_null() & (mask == 0))
        .then(pl.lit(False))
        .otherwise(None)
    )
    _reject_rows(
        frame,
        ((pl.col("signal_ts_ms") % _MS_PER_DAY) != 0)
        | (pl.col("long_entry_policy_schema_version") != "long_a0_entry_policy_v1")
        | (
            common
            & (
                (pl.col("common_entry_ts_ms") != pl.col("signal_ts_ms") + _MS_PER_HOUR)
                | (pl.col("common_entry_hour") != 1)
                | (pl.col("common_entry_price") <= 0.0)
                | (pl.col("common_entry_reason") != "next_hour_close")
            )
        )
        | (
            ~common
            & (
                pl.col("common_entry_ts_ms").is_not_null()
                | pl.col("common_entry_hour").is_not_null()
                | pl.col("common_entry_price").is_not_null()
                | (pl.col("common_entry_reason") != "next_hour_bar_missing")
            )
        )
        | reason.is_null()
        | ~reason.is_in(
            [
                "initial_entry_bar_missing",
                "signal_bar_missing",
                "sniper_retrace",
                "sniper_deadline_fallthrough",
                "sniper_deadline_missing",
            ]
        )
        | (initial_missing != ~common)
        | (current != (retrace_entry | deadline_fallthrough))
        | (
            ~current
            & (
                pl.col("current_entry_ts_ms").is_not_null()
                | pl.col("current_entry_hour").is_not_null()
                | pl.col("current_entry_price").is_not_null()
            )
        )
        | (
            common
            & (
                pl.col("current_entry_retrace_pct").is_null()
                | _not_close("current_entry_retrace_pct", pl.lit(float(retrace)))
            )
        )
        | (common != pl.col("current_entry_retrace_pct").is_not_null())
        | (mask < 0)
        | (mask > 63)
        | mask_outside_scan
        | observed_on_missing_bar
        | retrace_on_missing_bar
        | pl.col("current_entry_scan_prefix_complete").is_null()
        | pl.col("current_entry_close_triggered").is_null()
        | pl.col("current_entry_policy_available").is_null()
        | (
            (initial_missing | signal_missing)
            & (
                pl.col("current_entry_retrace_threshold").is_not_null()
                | (pl.col("current_entry_scan_first_hour") != first_hour)
                | scan_end.is_not_null()
                | (mask != 0)
                | pl.col("current_entry_scan_prefix_complete")
            )
        )
        | (
            retrace_entry
            & (
                (pl.col("current_entry_scan_first_hour") != first_hour)
                | (scan_end != pl.col("current_entry_hour"))
                | (pl.col("current_entry_hour") < first_hour)
                | (pl.col("current_entry_hour") > deadline_hour)
                | (pl.col("current_entry_retrace_threshold") <= 0.0)
                | (pl.col("current_entry_price") > pl.col("current_entry_retrace_threshold"))
                | (pl.col("current_entry_scan_prefix_complete") != (mask == 0))
            )
        )
        | (
            deadline_fallthrough
            & (
                (pl.col("current_entry_hour") != deadline_hour)
                | (pl.col("current_entry_scan_first_hour") != first_hour)
                | (scan_end != deadline_hour)
                | (pl.col("current_entry_retrace_threshold") <= 0.0)
                | (pl.col("current_entry_price") <= pl.col("current_entry_retrace_threshold"))
                | (pl.col("current_entry_scan_prefix_complete") != (mask == 0))
                | (((mask // (1 << (deadline_hour - 1))) % 2) != 0)
            )
        )
        | (
            deadline_missing
            & (
                (pl.col("current_entry_scan_first_hour") != first_hour)
                | (scan_end != deadline_hour)
                | (pl.col("current_entry_retrace_threshold") <= 0.0)
                | (((mask // (1 << (deadline_hour - 1))) % 2) != 1)
                | pl.col("current_entry_scan_prefix_complete")
            )
        )
        | (pl.col("current_entry_close_triggered").fill_null(False) != retrace_entry)
        | (retrace_entry & (pl.col("current_entry_close_trigger_first_hour") != pl.col("current_entry_hour")))
        | (~retrace_entry & pl.col("current_entry_close_trigger_first_hour").is_not_null())
        | (pl.col("current_entry_policy_available") != current)
        | (current & pl.col("current_entry_missing_reason").is_not_null())
        | (
            ~current
            & (
                pl.col("current_entry_missing_reason").is_null()
                | (pl.col("current_entry_missing_reason") != pl.col("current_entry_reason"))
            )
        )
        | (
            current
            & (
                pl.col("current_entry_hour").is_null()
                | pl.col("current_entry_ts_ms").is_null()
                | (
                    pl.col("current_entry_ts_ms")
                    != pl.col("signal_ts_ms") + pl.col("current_entry_hour") * _MS_PER_HOUR
                )
                | pl.col("current_entry_price").is_null()
                | (pl.col("current_entry_price") <= 0.0)
            )
        )
        | (
            observed_low_hour.is_not_null()
            & (~scan_state | (observed_low_hour < first_hour) | (observed_low_hour > scan_end))
        )
        | ~authoritative_low_hour.eq_missing(expected_authoritative_low_hour)
        | ~pl.col("current_entry_intrabar_low_touch_nonfill").eq_missing(expected_low_touch),
        message="LONG S03 violates frozen common/current anchor and prefix semantics",
    )
    both = common & current
    expected_improvement = pl.col("common_entry_price") / pl.col("current_entry_price") - 1.0
    _reject_rows(
        frame,
        (
            both
            & (
                pl.col("entry_price_improvement").is_null()
                | _not_close("entry_price_improvement", expected_improvement)
                | (pl.col("entry_delay_hours_vs_common") != pl.col("current_entry_hour") - pl.col("common_entry_hour"))
            )
        )
        | (
            ~both
            & (pl.col("entry_price_improvement").is_not_null() | pl.col("entry_delay_hours_vs_common").is_not_null())
        ),
        message="LONG S03 violates derived cross-anchor improvement/delay semantics",
    )
    return (
        "long_s03_frozen_common_and_current_anchor_semantics",
        "long_s03_prefix_trigger_reason_and_cross_anchor_semantics",
    )


def _validate_long_s04(frame: pl.DataFrame) -> tuple[str, ...]:
    predicates: list[pl.Expr] = [
        pl.col("long_label_schema_version") != "long_a0_minimal_labels_v1",
        pl.col("long_label_point_horizons") != "1|24|72",
        pl.col("long_label_excursion_horizons") != "24|72",
    ]
    for prefix in ("common", "current"):
        for horizon in (1, 24, 72):
            observed = pl.col(f"{prefix}_{horizon}h_observed_bars")
            available = pl.col(f"{prefix}_{horizon}h_point_available")
            complete = pl.col(f"{prefix}_{horizon}h_path_complete")
            point_return = pl.col(f"{prefix}_{horizon}h_point_return")
            reason = pl.col(f"{prefix}_{horizon}h_missing_reason")
            predicates.append(
                (observed < 0)
                | (observed > horizon)
                | (complete & (observed != horizon))
                | (available != point_return.is_not_null())
                | (complete & (~available | reason.is_not_null()))
                | (~complete & reason.is_null())
                | ~pl.col(f"{prefix}_{horizon}h_hourly_extrema_interval_censored")
            )
            if horizon in (24, 72):
                mfe = pl.col(f"{prefix}_{horizon}h_mfe")
                signed_mae = pl.col(f"{prefix}_{horizon}h_signed_mae")
                adverse = pl.col(f"{prefix}_{horizon}h_adverse_magnitude")
                predicates.append(
                    (
                        complete
                        & (
                            mfe.is_null()
                            | signed_mae.is_null()
                            | adverse.is_null()
                            | (mfe < 0.0)
                            | (signed_mae > 0.0)
                            | _not_close(f"{prefix}_{horizon}h_adverse_magnitude", -signed_mae)
                        )
                    )
                    | (~complete & (mfe.is_not_null() | signed_mae.is_not_null() | adverse.is_not_null()))
                )
        predicates.extend(
            (
                pl.col(f"{prefix}_1h_observed_bars") > pl.col(f"{prefix}_24h_observed_bars"),
                pl.col(f"{prefix}_24h_observed_bars") > pl.col(f"{prefix}_72h_observed_bars"),
                pl.col(f"{prefix}_72h_path_complete") & ~pl.col(f"{prefix}_24h_path_complete"),
                pl.col(f"{prefix}_24h_path_complete") & ~pl.col(f"{prefix}_1h_path_complete"),
                pl.col(f"{prefix}_label_complete") & pl.col(f"{prefix}_missing_path_reason").is_not_null(),
                ~pl.col(f"{prefix}_label_complete") & pl.col(f"{prefix}_missing_path_reason").is_null(),
                pl.col(f"{prefix}_label_complete")
                != (
                    pl.all_horizontal(pl.col(f"{prefix}_{horizon}h_path_complete") for horizon in (1, 24, 72))
                    & pl.col(f"{prefix}_stop_price").is_not_null()
                    & pl.col(f"{prefix}_take_profit_price").is_not_null()
                    & pl.col(f"{prefix}_same_bar_stop_tp_ambiguity").is_not_null()
                ),
                (pl.col(f"{prefix}_same_bar_stop_tp_ambiguity").is_not_null())
                & (pl.col(f"{prefix}_same_bar_stop_tp_ambiguity") == pl.lit(False))
                & ~pl.col(f"{prefix}_72h_path_complete"),
            )
        )
    _reject_rows(
        frame,
        pl.any_horizontal(predicates),
        message="LONG S04 violates frozen horizon/support/excursion/schema semantics",
    )
    return ("long_s04_frozen_horizon_support_excursion_reason_and_schema_semantics",)


def _validate_stage_specific_artifact(
    frame: pl.DataFrame,
    *,
    sleeve: str,
    stage: str,
    config_identity: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    if (sleeve, stage) == ("continuous", "S02"):
        return _validate_continuous_s02(frame, config_identity=config_identity)
    if (sleeve, stage) == ("continuous", "S03"):
        return _validate_continuous_s03(frame, config_identity=config_identity)
    if (sleeve, stage) == ("continuous", "S04"):
        return _validate_continuous_s04(frame)
    if (sleeve, stage) == ("long", "S02"):
        return _validate_long_s02(frame, config_identity=config_identity)
    if (sleeve, stage) == ("long", "S03"):
        return _validate_long_s03(frame, config_identity=config_identity)
    if (sleeve, stage) == ("long", "S04"):
        return _validate_long_s04(frame)
    raise StageReceiptError(f"semantic stage validation is unsupported for {sleeve}/{stage}")


def _assert_exact_stage_identity_relation(
    left: pl.DataFrame,
    right: pl.DataFrame,
    *,
    sleeve: str,
    left_stage: str,
    right_stage: str,
) -> None:
    left_schema = ARTIFACT_SCHEMAS[_SCHEMA_IDS[(sleeve, left_stage)]]
    right_schema = ARTIFACT_SCHEMAS[_SCHEMA_IDS[(sleeve, right_stage)]]
    if left_schema.key_fields != right_schema.key_fields:
        raise StageReceiptError(f"current registry no longer permits direct {left_stage}/{right_stage} key equality")
    columns = [*left_schema.key_fields, "canonical_instrument_id"]
    if not left.select(columns).equals(right.select(columns)):
        raise StageReceiptError(f"{right_stage} keys/canonical identity do not exactly equal bound {left_stage} parent")


def _validate_continuous_parent_relations(
    frames: Mapping[str, pl.DataFrame],
) -> tuple[str, ...]:
    relations: list[str] = []
    s02 = frames.get("S02")
    s03 = frames.get("S03")
    s04 = frames.get("S04")
    if s03 is not None:
        if s02 is None:  # pragma: no cover - receipt topology guard
            raise StageReceiptError("CONTINUOUS S03 semantic verification lacks bound S02")
        _assert_exact_stage_identity_relation(
            s02,
            s03,
            sleeve="continuous",
            left_stage="S02",
            right_stage="S03",
        )
        if not s02.select("venue", "symbol", "decision_ts_ms", "signal_ts_ms").equals(
            s03.select("venue", "symbol", "decision_ts_ms", "signal_ts_ms")
        ):
            raise StageReceiptError("CONTINUOUS S03 signal timestamps do not exactly propagate from S02")
        relations.append("S03_KEYS_CANONICAL_ID_AND_SIGNAL_TS_EQUAL_S02")
    if s04 is not None:
        if s02 is None or s03 is None:  # pragma: no cover - receipt topology guard
            raise StageReceiptError("CONTINUOUS S04 semantic verification lacks bound S02/S03")
        for parent_stage, parent in (("S02", s02), ("S03", s03)):
            _assert_exact_stage_identity_relation(
                parent,
                s04,
                sleeve="continuous",
                left_stage=parent_stage,
                right_stage="S04",
            )
        joined = s03.join(
            s04,
            on=["venue", "symbol", "decision_ts_ms", "canonical_instrument_id"],
            how="inner",
            validate="1:1",
        )
        predicates: list[pl.Expr] = []
        for horizon in (1, 24, 72):
            expected_reason = (
                pl.when(~pl.col("entry_anchor_available"))
                .then(pl.lit("no_entry_anchor"))
                .when(~pl.col(f"path_{horizon}h_available"))
                .then(pl.lit("endpoint_unavailable"))
                .when(~pl.col(f"path_{horizon}h_complete"))
                .then(pl.lit("incomplete_path"))
                .otherwise(None)
            )
            predicates.append(
                (
                    ~pl.col("entry_anchor_available")
                    & (
                        pl.col(f"path_{horizon}h_close_ts_ms").is_not_null()
                        | pl.col(f"path_{horizon}h_available")
                        | pl.col(f"path_{horizon}h_complete")
                        | (pl.col(f"path_{horizon}h_observed_hours") != 0)
                    )
                )
                | (
                    pl.col("entry_anchor_available")
                    & (pl.col(f"path_{horizon}h_close_ts_ms") != pl.col("entry_anchor_ts_ms") + horizon * _MS_PER_HOUR)
                )
                | ~pl.col(f"path_{horizon}h_missing_reason").eq_missing(expected_reason)
            )
        expected_overall_reason = (
            pl.when(~pl.col("entry_anchor_available"))
            .then(pl.lit("no_next_executable_close"))
            .when(~pl.col("path_1h_complete"))
            .then(pl.lit("incomplete_1h_path"))
            .when(~pl.col("path_24h_complete"))
            .then(pl.lit("incomplete_24h_path"))
            .when(~pl.col("path_72h_complete"))
            .then(pl.lit("incomplete_72h_path"))
            .otherwise(None)
        )
        predicates.append(~pl.col("missing_path_reason").eq_missing(expected_overall_reason))
        _reject_rows(
            joined,
            pl.any_horizontal(predicates),
            message="CONTINUOUS S04 anchor support does not propagate from bound S03",
        )
        relations.append("S04_KEYS_CANONICAL_ID_EQUAL_S02_AND_S03_WITH_S03_ANCHOR_PROPAGATION")
    return tuple(relations)


def _validate_long_parent_relations(frames: Mapping[str, pl.DataFrame]) -> tuple[str, ...]:
    relations: list[str] = []
    s02 = frames.get("S02")
    s03 = frames.get("S03")
    s04 = frames.get("S04")
    if s03 is not None:
        if s02 is None:  # pragma: no cover - receipt topology guard
            raise StageReceiptError("LONG S03 semantic verification lacks bound S02")
        _assert_exact_stage_identity_relation(
            s02,
            s03,
            sleeve="long",
            left_stage="S02",
            right_stage="S03",
        )
        joined = s02.select(
            "venue",
            "symbol",
            "signal_ts_ms",
            "canonical_instrument_id",
            "signal_close_hourly",
        ).join(
            s03,
            on=["venue", "symbol", "signal_ts_ms", "canonical_instrument_id"],
            how="inner",
            validate="1:1",
        )
        expected_threshold = pl.col("signal_close_hourly") * (1.0 - pl.col("current_entry_retrace_pct"))
        _reject_rows(
            joined,
            (~pl.col("common_entry_available") & (pl.col("current_entry_reason") != "initial_entry_bar_missing"))
            | (
                pl.col("common_entry_available")
                & pl.col("signal_close_hourly").is_null()
                & (pl.col("current_entry_reason") != "signal_bar_missing")
            )
            | (
                pl.col("common_entry_available")
                & pl.col("signal_close_hourly").is_not_null()
                & ~pl.col("current_entry_reason").is_in(
                    ["sniper_retrace", "sniper_deadline_fallthrough", "sniper_deadline_missing"]
                )
            )
            | (
                pl.col("signal_close_hourly").is_not_null()
                & pl.col("current_entry_retrace_pct").is_not_null()
                & (
                    pl.col("current_entry_retrace_threshold").is_null()
                    | _not_close("current_entry_retrace_threshold", expected_threshold)
                )
            )
            | (
                (pl.col("signal_close_hourly").is_null() | pl.col("current_entry_retrace_pct").is_null())
                & pl.col("current_entry_retrace_threshold").is_not_null()
            ),
            message="LONG S03 retrace threshold does not propagate from bound S02 signal geometry",
        )
        relations.append("S03_KEYS_CANONICAL_ID_EQUAL_S02_WITH_SIGNAL_GEOMETRY_PROPAGATION")
    if s04 is not None:
        if s02 is None or s03 is None:  # pragma: no cover - receipt topology guard
            raise StageReceiptError("LONG S04 semantic verification lacks bound S02/S03")
        for parent_stage, parent in (("S02", s02), ("S03", s03)):
            _assert_exact_stage_identity_relation(
                parent,
                s04,
                sleeve="long",
                left_stage=parent_stage,
                right_stage="S04",
            )
        joined = s03.join(
            s02.select(
                "venue",
                "symbol",
                "signal_ts_ms",
                "canonical_instrument_id",
                "fc_exit_stop_pct",
                "fc_exit_take_profit_pct",
            ),
            on=["venue", "symbol", "signal_ts_ms", "canonical_instrument_id"],
            how="inner",
            validate="1:1",
        ).join(
            s04,
            on=["venue", "symbol", "signal_ts_ms", "canonical_instrument_id"],
            how="inner",
            validate="1:1",
        )
        predicates: list[pl.Expr] = []
        for prefix in ("common", "current"):
            anchor_available = pl.col(f"{prefix}_entry_available")
            anchor_ts = pl.col(f"{prefix}_entry_ts_ms")
            anchor_price = pl.col(f"{prefix}_entry_price")
            horizon_reason_tokens: list[pl.Expr] = []
            for horizon in (1, 24, 72):
                observed = pl.col(f"{prefix}_{horizon}h_observed_bars")
                point_available = pl.col(f"{prefix}_{horizon}h_point_available")
                path_complete = pl.col(f"{prefix}_{horizon}h_path_complete")
                expected_horizon_reason = (
                    pl.when(~anchor_available)
                    .then(pl.concat_str(pl.lit("anchor_unavailable:"), pl.col(f"{prefix}_entry_reason")))
                    .when(~point_available & ~path_complete)
                    .then(
                        pl.concat_str(
                            pl.lit("endpoint_close_missing+path_incomplete:"),
                            observed.cast(pl.String),
                            pl.lit(f"/{horizon}"),
                        )
                    )
                    .when(~point_available)
                    .then(pl.lit("endpoint_close_missing"))
                    .when(~path_complete)
                    .then(
                        pl.concat_str(
                            pl.lit("path_incomplete:"),
                            observed.cast(pl.String),
                            pl.lit(f"/{horizon}"),
                        )
                    )
                    .otherwise(None)
                )
                predicates.append(
                    (
                        anchor_available
                        & (pl.col(f"{prefix}_{horizon}h_endpoint_ts_ms") != anchor_ts + horizon * _MS_PER_HOUR)
                    )
                    | (
                        ~anchor_available
                        & (
                            pl.col(f"{prefix}_{horizon}h_endpoint_ts_ms").is_not_null()
                            | pl.col(f"{prefix}_{horizon}h_point_available")
                            | pl.col(f"{prefix}_{horizon}h_path_complete")
                            | (pl.col(f"{prefix}_{horizon}h_observed_bars") != 0)
                        )
                    )
                    | (path_complete != (anchor_available & (observed == horizon)))
                    | ~pl.col(f"{prefix}_{horizon}h_missing_reason").eq_missing(expected_horizon_reason)
                )
                horizon_reason_tokens.append(
                    pl.when(pl.col(f"{prefix}_{horizon}h_missing_reason").is_not_null())
                    .then(
                        pl.concat_str(
                            pl.lit(f"{horizon}h:"),
                            pl.col(f"{prefix}_{horizon}h_missing_reason"),
                        )
                    )
                    .otherwise(pl.lit(""))
                )
            expected_stop = anchor_price * (1.0 - pl.col("fc_exit_stop_pct"))
            expected_take_profit = anchor_price * (1.0 + pl.col("fc_exit_take_profit_pct"))
            predicates.append(
                (
                    anchor_available
                    & (
                        pl.col(f"{prefix}_stop_price").is_null()
                        | pl.col(f"{prefix}_take_profit_price").is_null()
                        | _not_close(f"{prefix}_stop_price", expected_stop)
                        | _not_close(f"{prefix}_take_profit_price", expected_take_profit)
                    )
                )
                | (
                    ~anchor_available
                    & (
                        pl.col(f"{prefix}_stop_price").is_not_null()
                        | pl.col(f"{prefix}_take_profit_price").is_not_null()
                    )
                )
            )
            ambiguity_reason = (
                pl.when(
                    ~anchor_available
                    | pl.col(f"{prefix}_stop_price").is_null()
                    | pl.col(f"{prefix}_take_profit_price").is_null()
                )
                .then(pl.lit("ambiguity_levels"))
                .when(pl.col(f"{prefix}_same_bar_stop_tp_ambiguity").is_null())
                .then(pl.lit("ambiguity_path"))
                .otherwise(pl.lit(""))
            )
            joined_reason = (
                pl.concat_str([*horizon_reason_tokens, ambiguity_reason], separator="+")
                .str.replace_all(r"\++", "+")
                .str.strip_chars("+")
            )
            expected_overall_reason = pl.when(joined_reason == "").then(None).otherwise(joined_reason)
            predicates.append(~pl.col(f"{prefix}_missing_path_reason").eq_missing(expected_overall_reason))
        _reject_rows(
            joined,
            pl.any_horizontal(predicates),
            message="LONG S04 anchors/exit levels do not propagate from bound S02/S03",
        )
        relations.append("S04_KEYS_CANONICAL_ID_EQUAL_S02_AND_S03_WITH_ANCHOR_EXIT_PROPAGATION")
    return tuple(relations)


def _validate_transitive_stage_relations(
    frames: Mapping[str, pl.DataFrame],
    *,
    sleeve: str,
) -> tuple[str, ...]:
    if sleeve == "continuous":
        return _validate_continuous_parent_relations(frames)
    return _validate_long_parent_relations(frames)


def _semantic_relation_projection(
    frame: pl.DataFrame,
    *,
    sleeve: str,
    stage: str,
) -> pl.DataFrame:
    """Retain only columns needed after per-artifact semantic validation."""

    if stage != "S02":
        return frame
    if sleeve == "continuous":
        return frame.select(
            "venue",
            "symbol",
            "decision_ts_ms",
            "canonical_instrument_id",
            "signal_ts_ms",
        )
    return frame.select(
        "venue",
        "symbol",
        "signal_ts_ms",
        "canonical_instrument_id",
        "symbol_age_days",
        "signal_close_hourly",
        "fc_exit_stop_pct",
        "fc_exit_take_profit_pct",
    )


def verify_stage_population_identity_binding(
    *,
    population_receipt_path: str | Path,
    population_record: Mapping[str, JsonValue],
    stage_identity_records: Mapping[str, Mapping[str, JsonValue]],
    sleeve: str,
    venue: str,
    s02_artifact: pl.DataFrame,
    config_identity: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Verify the canonical population receipt and its exact S02 key relation.

    Root, PIT, and map records must bind the same logical files, bytes, and
    canonical JSON identities as S02.  The population module deliberately keeps
    root completeness/authenticity and PIT provenance limited; this helper
    preserves those limitations while proving that S02 has exactly the expected
    registered key population.
    """

    try:
        from .strategy_overhaul_expected_population import (
            EXPECTED_POPULATION_RECEIPT_FILENAME,
            ExpectedPopulationError,
            load_expected_population_artifacts,
            parse_expected_population_jsonl,
            registered_s02_key_sha256,
            verify_expected_population_receipt_identity,
        )
    except ImportError as exc:  # pragma: no cover - repository packaging failure
        raise StageReceiptError("canonical expected-population verifier is unavailable") from exc

    path = Path(population_receipt_path)
    if path.name != EXPECTED_POPULATION_RECEIPT_FILENAME:
        raise StageReceiptError(f"population identity must be {EXPECTED_POPULATION_RECEIPT_FILENAME!r}")
    data = _regular_file_bytes(path, name="canonical expected-population receipt")
    if len(data) != population_record.get("bytes") or hashlib.sha256(data).hexdigest() != population_record.get(
        "file_sha256"
    ):
        raise StageReceiptError("canonical population receipt bytes do not match the stage identity record")
    payload = _parse_json_object(data, name="canonical expected-population receipt")
    if canonical_json_sha256(payload) != population_record.get("identity_sha256"):
        raise StageReceiptError("canonical population receipt JSON identity does not match S02")
    try:
        identity = verify_expected_population_receipt_identity(path)
    except (ExpectedPopulationError, OSError, TypeError, ValueError) as exc:
        raise StageReceiptError(f"canonical expected-population verification failed: {exc}") from exc
    if identity.receipt_file_sha256 != hashlib.sha256(
        data
    ).hexdigest() or identity.receipt_identity_sha256 != canonical_json_sha256(payload):
        raise StageReceiptError("canonical expected-population receipt changed during semantic verification")
    if identity.sleeve != sleeve or identity.venue != venue:
        raise StageReceiptError("canonical expected-population sleeve/venue does not match S02")
    try:
        artifacts = load_expected_population_artifacts(path.parent)
        source_population = parse_expected_population_jsonl(
            artifacts.source_keys_jsonl,
            sleeve=cast(Any, sleeve),
            artifact_kind="source_keys",
        )
        expected_population = parse_expected_population_jsonl(
            artifacts.expected_population_jsonl,
            sleeve=cast(Any, sleeve),
            artifact_kind="expected_population",
        )
    except (ExpectedPopulationError, OSError, TypeError, ValueError) as exc:
        raise StageReceiptError(f"canonical expected-population artifacts failed verification: {exc}") from exc
    if dict(artifacts.receipt) != payload:
        raise StageReceiptError("loaded expected-population artifacts bind a different receipt")
    config_values = _identity_config_values(config_identity)
    raw_exclusions = config_values.get("exclude_symbols")
    if not isinstance(raw_exclusions, list) or any(
        not isinstance(symbol, str) or not symbol for symbol in raw_exclusions
    ):
        raise StageReceiptError("canonical config exclude_symbols is malformed")
    excluded_symbols = cast(list[str], raw_exclusions)
    excluded_population = expected_population.filter(pl.col("symbol").is_in(excluded_symbols))
    excluded_source = source_population.filter(pl.col("symbol").is_in(excluded_symbols))
    if not excluded_population.is_empty() or not excluded_source.is_empty():
        raise StageReceiptError("canonical expected-population artifacts contain a current config exclusion")
    population_key_columns = ["symbol", "signal_ts_ms"]
    if sleeve == "long":
        population_key_columns.append("symbol_age_days")
    expected_outside_source = expected_population.select(population_key_columns).join(
        source_population.select(population_key_columns),
        on=population_key_columns,
        how="anti",
    )
    if not expected_outside_source.is_empty():
        raise StageReceiptError("canonical expected population is not an exact key/age subset of source_keys.jsonl")
    for kind in ("config", "root", "pit", "instrument_map"):
        population_binding = identity.identity_bindings.get(kind)
        stage_binding = stage_identity_records.get(kind)
        if not isinstance(population_binding, Mapping) or not isinstance(stage_binding, Mapping):
            raise StageReceiptError(f"canonical population/{kind} stage binding is malformed")
        if any(
            stage_binding.get(name) != population_binding.get(name)
            for name in ("logical_path", "file_sha256", "bytes", "identity_sha256")
        ):
            raise StageReceiptError(f"canonical expected population {kind} bytes/JSON identity do not match S02")
    schema_id = _SCHEMA_IDS[(sleeve, "S02")]
    if sleeve == "continuous":
        delay = config_values.get("entry_confirm_delay_hours")
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise StageReceiptError("canonical CONTINUOUS entry_confirm_delay_hours is invalid")
        projection = payload.get("registered_s02_key_projection")
        if not isinstance(projection, dict) or projection.get("entry_confirm_delay_hours") != delay:
            raise StageReceiptError("population registered-key delay does not equal canonical config")
        registered_keys = expected_population.select(
            pl.lit(venue, dtype=pl.String).alias("venue"),
            pl.col("symbol"),
            (pl.col("signal_ts_ms") + delay * _MS_PER_HOUR).cast(pl.Int64).alias("decision_ts_ms"),
        ).sort(["venue", "symbol", "decision_ts_ms"])
    else:
        registered_keys = expected_population.select(
            pl.lit(venue, dtype=pl.String).alias("venue"),
            pl.col("symbol"),
            pl.col("signal_ts_ms"),
        ).sort(["venue", "symbol", "signal_ts_ms"])
    recomputed_population_hash = registered_s02_key_sha256(
        registered_keys,
        sleeve=cast(Any, sleeve),
    )
    if (
        registered_keys.height != identity.registered_s02_key_row_count
        or recomputed_population_hash != identity.registered_s02_key_sha256
    ):
        raise StageReceiptError(
            "canonical expected-population JSONL does not reproduce its registered S02 key projection"
        )
    observed_hash = canonical_stage_key_projection_sha256(s02_artifact, schema_id)
    try:
        scope_bounds = registered_scope_bounds_ms(cast(dict[str, Any], config_identity))
    except (KeyError, TypeError, ValueError) as exc:
        raise StageReceiptError(f"canonical registered scope is invalid: {exc}") from exc
    outside_registered_signal_scope = s02_artifact.filter(
        (pl.col("signal_ts_ms") < scope_bounds["signal_start_date_ms"])
        | (pl.col("signal_ts_ms") >= scope_bounds["signal_end_date_exclusive_ms"])
    )
    if not outside_registered_signal_scope.is_empty():
        raise StageReceiptError("S02 signal keys fall outside the canonical registered scope")
    expected_key_columns = ARTIFACT_SCHEMAS[schema_id].key_fields
    if identity.registered_s02_key_columns != expected_key_columns:
        raise StageReceiptError("canonical expected population registered S02 key schema has drifted")
    if (
        identity.registered_s02_key_row_count != s02_artifact.height
        or identity.registered_s02_key_sha256 != observed_hash
    ):
        raise StageReceiptError(
            "S02 registered key population does not exactly equal the canonical expected-population receipt"
        )
    long_symbol_age_equal_to_expected_population: bool | None = None
    if sleeve == "long":
        expected_ages = expected_population.select(
            "symbol",
            "signal_ts_ms",
            pl.col("symbol_age_days").alias("__expected_symbol_age_days"),
        )
        observed_ages = s02_artifact.select(
            "symbol",
            "signal_ts_ms",
            "symbol_age_days",
        ).join(
            expected_ages,
            on=["symbol", "signal_ts_ms"],
            how="left",
            validate="1:1",
        )
        age_mismatch = observed_ages.filter(
            pl.col("__expected_symbol_age_days").is_null()
            | (pl.col("symbol_age_days") != pl.col("__expected_symbol_age_days"))
        )
        if not age_mismatch.is_empty():
            raise StageReceiptError("LONG S02 symbol_age_days does not exactly equal expected_population.jsonl")
        long_symbol_age_equal_to_expected_population = True
    return {
        "verification_status": ("VERIFIED_CANONICAL_EXPECTED_POPULATION_SOURCE_SUBSET_AND_S02_EQUALITY"),
        "population_receipt_file_sha256": cast(JsonValue, identity.receipt_file_sha256),
        "population_receipt_identity_sha256": identity.receipt_identity_sha256,
        "source_keys_file_sha256": identity.source_keys_file_sha256,
        "source_keys_row_count": identity.source_keys_row_count,
        "expected_population_file_sha256": identity.expected_population_file_sha256,
        "expected_population_row_count": identity.expected_population_row_count,
        "registered_s02_key_projection_sha256": identity.registered_s02_key_sha256,
        "expected_population_key_age_subset_of_source_keys": True,
        "long_symbol_age_equal_to_expected_population": long_symbol_age_equal_to_expected_population,
        "config_root_pit_instrument_map_identity_equal_to_s02": True,
        "root_completeness_proven": False,
        "root_authenticity_proven": False,
        "pit_provenance_authenticated": False,
    }


def verify_stage_receipt_semantics(
    path: str | Path,
    *,
    binding_root: str | Path | None = None,
    file_overrides: Mapping[str, str | Path] | None = None,
) -> StageSemanticVerification:
    """Verify one S02--S04 artifact and every bound semantic-stage ancestor.

    This current-code path is intentionally distinct from archival byte
    verification: it consults the current schema/config factories, parses only
    self-describing Parquet or Arrow IPC/Feather artifacts, validates the
    canonical population receipt, and proves the registered parent key and
    canonical-instrument relations.  It still does not recompute source-derived
    features/labels or authorize outcomes.
    """

    receipt_path = Path(path)
    root = Path(binding_root) if binding_root is not None else receipt_path.parent
    overrides = {name: Path(value) for name, value in (file_overrides or {}).items()}
    for logical_path in overrides:
        _normalise_logical_path(logical_path, name="file_overrides key")

    byte_verification = _verify_stage_receipt_byte_bindings(
        receipt_path,
        binding_root=root,
        file_overrides=overrides,
        require_current_registry_declarations=True,
    )
    if byte_verification.stage not in {"S02", "S03", "S04"}:
        raise StageReceiptError("semantic artifact verification exists only for S02, S03, and S04")

    nodes_by_path: dict[Path, dict[str, JsonValue]] = {}
    nodes_by_stage: dict[str, tuple[dict[str, JsonValue], Path]] = {}
    active: set[Path] = set()

    def collect(current_path: Path) -> dict[str, JsonValue]:
        resolved = current_path.resolve(strict=False)
        if resolved in active:
            raise StageReceiptError("stage receipt parent cycle detected during semantic verification")
        existing = nodes_by_path.get(resolved)
        if existing is not None:
            return existing
        active.add(resolved)
        try:
            payload = load_stage_receipt(current_path)
            stage = str(payload["stage"])
            prior = nodes_by_stage.get(stage)
            if prior is not None and prior[0]["receipt_id"] != payload["receipt_id"]:
                raise StageReceiptError(f"semantic chain contains two different {stage} receipts")
            nodes_by_stage[stage] = (payload, current_path)
            nodes_by_path[resolved] = payload
            parents = payload["parents"]
            assert isinstance(parents, list)
            for raw in parents:
                assert isinstance(raw, dict)
                parent_path = _resolve_bound_path(
                    str(raw["logical_path"]),
                    binding_root=root,
                    file_overrides=overrides,
                )
                parent_payload = collect(parent_path)
                for field in ("stage", "run_id", "receipt_id", "receipt_payload_sha256"):
                    if parent_payload[field] != raw[field]:
                        raise StageReceiptError(
                            f"semantic-chain parent receipt {field} disagrees with its child binding"
                        )
            return payload
        finally:
            active.remove(resolved)

    leaf = collect(receipt_path)
    leaf_stage = str(leaf["stage"])
    expected_chain_stages = tuple(STAGES[: STAGES.index(leaf_stage) + 1])
    if tuple(stage for stage in STAGES if stage in nodes_by_stage) != expected_chain_stages:
        raise StageReceiptError("semantic stage chain is incomplete")

    s01_payload = nodes_by_stage["S01"][0]
    baseline_identities = s01_payload["identity_receipts"]
    assert isinstance(baseline_identities, dict)
    baseline_config = s01_payload["canonical_config"]
    for stage in ("S02", "S03", "S04"):
        node = nodes_by_stage.get(stage)
        if node is None:
            continue
        payload = node[0]
        if payload["canonical_config"] != baseline_config:
            raise StageReceiptError(f"{stage} did not propagate the S01 canonical config binding")
        if payload["identity_receipts"] != baseline_identities:
            raise StageReceiptError(f"{stage} did not propagate every S01 identity record exactly")
    s00_identities = nodes_by_stage["S00"][0]["identity_receipts"]
    assert isinstance(s00_identities, dict)
    for kind in STAGE_IDENTITY_RECEIPT_KINDS["S00"]:
        if s00_identities.get(kind) != baseline_identities.get(kind):
            raise StageReceiptError(f"S00/S01 {kind} identity propagation failed")

    config_record = baseline_identities["config"]
    assert isinstance(config_record, dict)
    config_path = _resolve_bound_path(
        str(config_record["logical_path"]),
        binding_root=root,
        file_overrides=overrides,
    )
    config_data = _regular_file_bytes(config_path, name="semantic config identity receipt")
    if (
        len(config_data) != config_record["bytes"]
        or hashlib.sha256(config_data).hexdigest() != config_record["file_sha256"]
    ):
        raise StageReceiptError("semantic config identity bytes do not match the stage chain")
    config_identity = _parse_json_object(config_data, name="semantic config identity receipt")
    if canonical_json_sha256(config_identity) != config_record["identity_sha256"]:
        raise StageReceiptError("semantic config JSON identity does not match the stage chain")
    parity_surface = stage_receipt_config_parity_surface(config_identity)

    sleeve = str(leaf["sleeve"])
    venue = str(leaf["venue"])
    semantic_stage_names = tuple(stage for stage in ("S02", "S03", "S04") if stage in nodes_by_stage)
    frames: dict[str, pl.DataFrame] = {}
    artifact_rows: list[JsonValue] = []
    for stage in semantic_stage_names:
        payload = nodes_by_stage[stage][0]
        if payload["sleeve"] != sleeve or payload["venue"] != venue:
            raise StageReceiptError(f"{stage} semantic ancestor has a different sleeve/venue")
        _validate_current_registry_declaration(payload, context="semantic-chain receipt")
        artifact_record = payload["artifact"]
        assert isinstance(artifact_record, dict)
        artifact_path = _resolve_bound_path(
            str(artifact_record["logical_path"]),
            binding_root=root,
            file_overrides=overrides,
        )
        data = _regular_file_bytes(artifact_path, name=f"semantic {stage} artifact")
        if len(data) != artifact_record["bytes"] or hashlib.sha256(data).hexdigest() != artifact_record["file_sha256"]:
            raise StageReceiptError(f"semantic {stage} artifact bytes do not match its byte receipt")
        artifact_format, frame = _parse_semantic_artifact(
            data,
            logical_path=str(artifact_record["logical_path"]),
        )
        schema_id = _SCHEMA_IDS[(sleeve, stage)]
        invariant_checks = [
            *_require_exact_registered_artifact(frame, schema_id=schema_id, venue=venue),
            *_validate_stage_specific_artifact(
                frame,
                sleeve=sleeve,
                stage=stage,
                config_identity=config_identity,
            ),
        ]
        row_count = frame.height
        key_hash = canonical_stage_key_projection_sha256(frame, schema_id)
        if row_count != artifact_record["declared_row_count"]:
            raise StageReceiptError(
                f"{stage} declared row count does not equal the parsed artifact: "
                f"declared={artifact_record['declared_row_count']}, observed={row_count}"
            )
        if key_hash != artifact_record["declared_key_projection_sha256"]:
            raise StageReceiptError(
                f"{stage} declared key projection SHA-256 does not equal the canonical artifact keys"
            )
        frames[stage] = _semantic_relation_projection(frame, sleeve=sleeve, stage=stage)
        artifact_rows.append(
            cast(
                JsonValue,
                {
                    "stage": stage,
                    "logical_path": artifact_record["logical_path"],
                    "file_sha256": artifact_record["file_sha256"],
                    "bytes": artifact_record["bytes"],
                    "format": artifact_format,
                    "format_detected_from": "normalized_logical_path_suffix",
                    "schema_identity": artifact_record["declared_schema_identity"],
                    "column_order_verified": True,
                    "physical_dtypes_verified": True,
                    "registered_nullability_verified": True,
                    "canonical_key_order_verified": True,
                    "row_count": row_count,
                    "declared_row_count_equal": True,
                    "canonical_key_projection_algorithm": "sorted_registered_keys_canonical_jsonl_v1",
                    "canonical_key_projection_sha256": key_hash,
                    "declared_key_projection_equal": True,
                    "stage_invariant_checks": invariant_checks,
                },
            )
        )

    transitive_relations = _validate_transitive_stage_relations(frames, sleeve=sleeve)
    s02_payload = nodes_by_stage["S02"][0]
    s02_identities = s02_payload["identity_receipts"]
    assert isinstance(s02_identities, dict)
    population_record = s02_identities["population"]
    assert isinstance(population_record, dict)
    population_path = _resolve_bound_path(
        str(population_record["logical_path"]),
        binding_root=root,
        file_overrides=overrides,
    )
    population_verification = verify_stage_population_identity_binding(
        population_receipt_path=population_path,
        population_record=population_record,
        stage_identity_records=cast(Mapping[str, Mapping[str, JsonValue]], s02_identities),
        sleeve=sleeve,
        venue=venue,
        s02_artifact=frames["S02"],
        config_identity=config_identity,
    )

    leaf_bytes = _regular_file_bytes(receipt_path, name="semantic leaf stage byte receipt")
    if (
        hashlib.sha256(leaf_bytes).hexdigest() != hashlib.sha256(render_stage_receipt(leaf)).hexdigest()
    ):  # catches a path swap between collection and final binding
        raise StageReceiptError("leaf stage byte receipt changed during semantic verification")
    semantic_receipt: dict[str, JsonValue] = {
        "receipt_schema_version": STAGE_SEMANTIC_RECEIPT_SCHEMA_VERSION,
        "receipt_type": STAGE_SEMANTIC_RECEIPT_TYPE,
        "receipt_scope": STAGE_SEMANTIC_RECEIPT_SCOPE,
        "verification_status": STAGE_SEMANTIC_VERIFICATION_STATUS,
        "run_id": leaf["run_id"],
        "sleeve": sleeve,
        "venue": venue,
        "stage": leaf_stage,
        "semantic_validation_performed": True,
        "stage_byte_bindings_verified": True,
        "current_config_and_registry_verified": True,
        "config_propagation_verified": True,
        "population_identity_and_s02_keys_verified": True,
        "diagnostic_only": True,
        "source_recomputation_performed": False,
        "outcome_blindness_verified": False,
        "population_or_root_completeness_verified": False,
        "outcome_run_authorized": False,
        "real_money_authorized": False,
        "stage_byte_receipt": {
            "receipt_id": leaf["receipt_id"],
            "receipt_payload_sha256": leaf["receipt_payload_sha256"],
            "file_sha256": hashlib.sha256(leaf_bytes).hexdigest(),
            "bytes": len(leaf_bytes),
            "byte_verified_receipt_count": byte_verification.byte_verified_receipt_count,
            "byte_verified_bound_file_count": byte_verification.byte_verified_bound_file_count,
        },
        "config_identity": {
            "identity_sha256": config_record["identity_sha256"],
            "file_sha256": config_record["file_sha256"],
            "bytes": config_record["bytes"],
            "current_parity_surface": cast(JsonValue, parity_surface),
            "propagated_stage_receipts": list(expected_chain_stages),
        },
        "population_verification": population_verification,
        "semantic_stage_artifacts": artifact_rows,
        "transitive_stage_relations": list(transitive_relations),
        "artifact_format_policy": {
            "supported_normalized_suffixes": list(SUPPORTED_SEMANTIC_ARTIFACT_SUFFIXES),
            "unsupported_formats_fail_closed": True,
            "csv_and_ndjson_supported": False,
        },
        "limitations": list(STAGE_SEMANTIC_LIMITATIONS),
    }
    payload_sha256 = canonical_json_sha256(semantic_receipt)
    semantic_receipt["receipt_payload_sha256"] = payload_sha256
    semantic_receipt["receipt_id"] = f"{leaf['run_id']}-{leaf_stage.lower()}-semantic-{payload_sha256[:24]}"
    _validate_stage_semantic_receipt_payload(semantic_receipt)
    return StageSemanticVerification(
        receipt_id=str(semantic_receipt["receipt_id"]),
        stage_byte_receipt_id=str(leaf["receipt_id"]),
        run_id=str(leaf["run_id"]),
        sleeve=sleeve,
        venue=venue,
        stage=leaf_stage,
        semantic_verified_stage_count=len(semantic_stage_names),
        semantic_validation_performed=True,
        current_registry_or_config_factories_consulted=True,
        receipt=semantic_receipt,
    )


def _validate_stage_semantic_receipt_payload(payload: dict[str, JsonValue]) -> None:
    _require_exact_keys(payload, _SEMANTIC_TOP_LEVEL_KEYS, name="stage semantic receipt")
    if payload["receipt_schema_version"] != STAGE_SEMANTIC_RECEIPT_SCHEMA_VERSION:
        raise StageReceiptError("unsupported stage semantic receipt schema version")
    if (
        payload["receipt_type"] != STAGE_SEMANTIC_RECEIPT_TYPE
        or payload["receipt_scope"] != STAGE_SEMANTIC_RECEIPT_SCOPE
        or payload["verification_status"] != STAGE_SEMANTIC_VERIFICATION_STATUS
    ):
        raise StageReceiptError("stage semantic receipt type/scope/status is invalid")
    sleeve = payload["sleeve"]
    venue = payload["venue"]
    stage = payload["stage"]
    if sleeve not in SLEEVES or venue not in VENUES or stage not in {"S02", "S03", "S04"}:
        raise StageReceiptError("stage semantic receipt has an invalid sleeve, venue, or stage")
    for field in (
        "semantic_validation_performed",
        "stage_byte_bindings_verified",
        "current_config_and_registry_verified",
        "config_propagation_verified",
        "population_identity_and_s02_keys_verified",
        "diagnostic_only",
    ):
        if payload[field] is not True:
            raise StageReceiptError(f"stage semantic receipt must retain {field}=true")
    for field in (
        "source_recomputation_performed",
        "outcome_blindness_verified",
        "population_or_root_completeness_verified",
        "outcome_run_authorized",
        "real_money_authorized",
    ):
        if payload[field] is not False:
            raise StageReceiptError(f"stage semantic receipt must retain {field}=false")

    byte_receipt = _require_exact_keys(
        payload["stage_byte_receipt"],
        {
            "receipt_id",
            "receipt_payload_sha256",
            "file_sha256",
            "bytes",
            "byte_verified_receipt_count",
            "byte_verified_bound_file_count",
        },
        name="stage_byte_receipt",
    )
    for field in ("receipt_payload_sha256", "file_sha256"):
        _require_sha256(byte_receipt[field], name=f"stage_byte_receipt.{field}")
    for field in ("bytes", "byte_verified_receipt_count", "byte_verified_bound_file_count"):
        _require_nonnegative_int(byte_receipt[field], name=f"stage_byte_receipt.{field}")
    if not isinstance(byte_receipt["receipt_id"], str) or not byte_receipt["receipt_id"]:
        raise StageReceiptError("stage_byte_receipt.receipt_id must be nonblank")

    config = _require_exact_keys(
        payload["config_identity"],
        {
            "identity_sha256",
            "file_sha256",
            "bytes",
            "current_parity_surface",
            "propagated_stage_receipts",
        },
        name="config_identity",
    )
    _require_sha256(config["identity_sha256"], name="config_identity.identity_sha256")
    _require_sha256(config["file_sha256"], name="config_identity.file_sha256")
    _require_nonnegative_int(config["bytes"], name="config_identity.bytes")
    expected_parity_fields = (
        {
            "full_config_and_scope_identity": {
                "full_config_sha256",
                "registered_scope_sha256",
                "component_config_sha256",
            },
            "selection_profile": {"rmom_quantile"},
            "decision_and_btc_gate": {"entry_confirm_delay_hours"},
        }
        if sleeve == "continuous"
        else {
            "full_config_and_scope_identity": {
                "full_config_sha256",
                "registered_scope_sha256",
                "undated_window_fields",
            },
            "population_and_rolling_windows": {
                "universe_size",
                "universe_volume_window_days",
                "min_listing_history_days",
            },
            "classifier_and_exit_shape": {"fc_max_hold_days"},
            "trigger_and_exit_profile": {"fc_use_atr_exits"},
        }
    )
    parity_surface = _require_exact_keys(
        config["current_parity_surface"],
        set(expected_parity_fields),
        name="config_identity.current_parity_surface",
    )
    for target, fields in expected_parity_fields.items():
        _require_exact_keys(
            parity_surface[target],
            fields,
            name=f"config_identity.current_parity_surface.{target}",
        )
    expected_chain = list(STAGES[: STAGES.index(str(stage)) + 1])
    if config["propagated_stage_receipts"] != expected_chain:
        raise StageReceiptError("semantic config propagation stages do not match the leaf stage")

    population = _require_exact_keys(
        payload["population_verification"],
        {
            "verification_status",
            "population_receipt_file_sha256",
            "population_receipt_identity_sha256",
            "source_keys_file_sha256",
            "source_keys_row_count",
            "expected_population_file_sha256",
            "expected_population_row_count",
            "registered_s02_key_projection_sha256",
            "expected_population_key_age_subset_of_source_keys",
            "long_symbol_age_equal_to_expected_population",
            "config_root_pit_instrument_map_identity_equal_to_s02",
            "root_completeness_proven",
            "root_authenticity_proven",
            "pit_provenance_authenticated",
        },
        name="population_verification",
    )
    if population["verification_status"] != "VERIFIED_CANONICAL_EXPECTED_POPULATION_SOURCE_SUBSET_AND_S02_EQUALITY":
        raise StageReceiptError("semantic population verification status is invalid")
    for field in (
        "population_receipt_file_sha256",
        "population_receipt_identity_sha256",
        "source_keys_file_sha256",
        "expected_population_file_sha256",
        "registered_s02_key_projection_sha256",
    ):
        _require_sha256(population[field], name=f"population_verification.{field}")
    _require_nonnegative_int(
        population["source_keys_row_count"],
        name="population_verification.source_keys_row_count",
    )
    _require_nonnegative_int(
        population["expected_population_row_count"],
        name="population_verification.expected_population_row_count",
    )
    if population["config_root_pit_instrument_map_identity_equal_to_s02"] is not True:
        raise StageReceiptError("semantic population identity equality must remain true")
    if population["expected_population_key_age_subset_of_source_keys"] is not True:
        raise StageReceiptError("semantic expected population must remain a source-key/age subset")
    expected_age_status = True if sleeve == "long" else None
    if population["long_symbol_age_equal_to_expected_population"] is not expected_age_status:
        raise StageReceiptError("semantic LONG expected-population age status is invalid")
    for field in ("root_completeness_proven", "root_authenticity_proven", "pit_provenance_authenticated"):
        if population[field] is not False:
            raise StageReceiptError(f"semantic population verification must retain {field}=false")

    artifacts = _require_list(payload["semantic_stage_artifacts"], name="semantic_stage_artifacts")
    expected_semantic_stages = [
        name for name in ("S02", "S03", "S04") if STAGES.index(name) <= STAGES.index(str(stage))
    ]
    observed_stages: list[str] = []
    artifact_keys = {
        "stage",
        "logical_path",
        "file_sha256",
        "bytes",
        "format",
        "format_detected_from",
        "schema_identity",
        "column_order_verified",
        "physical_dtypes_verified",
        "registered_nullability_verified",
        "canonical_key_order_verified",
        "row_count",
        "declared_row_count_equal",
        "canonical_key_projection_algorithm",
        "canonical_key_projection_sha256",
        "declared_key_projection_equal",
        "stage_invariant_checks",
    }
    for index, raw in enumerate(artifacts):
        row = _require_exact_keys(raw, artifact_keys, name=f"semantic_stage_artifacts[{index}]")
        observed_stage = str(row["stage"])
        observed_stages.append(observed_stage)
        _normalise_logical_path(row["logical_path"], name=f"semantic_stage_artifacts[{index}].logical_path")
        _require_sha256(row["file_sha256"], name=f"semantic_stage_artifacts[{index}].file_sha256")
        _require_sha256(
            row["canonical_key_projection_sha256"],
            name=f"semantic_stage_artifacts[{index}].canonical_key_projection_sha256",
        )
        _require_nonnegative_int(row["bytes"], name=f"semantic_stage_artifacts[{index}].bytes")
        _require_nonnegative_int(row["row_count"], name=f"semantic_stage_artifacts[{index}].row_count")
        if row["format"] not in set(_ARTIFACT_FORMAT_READERS):
            raise StageReceiptError("semantic artifact record contains an unsupported format")
        if row["format_detected_from"] != "normalized_logical_path_suffix":
            raise StageReceiptError("semantic artifact format detection claim is invalid")
        schema = _require_exact_keys(
            row["schema_identity"],
            {"schema_id", "schema_version", "schema_sha256"},
            name=f"semantic_stage_artifacts[{index}].schema_identity",
        )
        _require_sha256(schema["schema_sha256"], name=f"semantic_stage_artifacts[{index}].schema_sha256")
        for field in (
            "column_order_verified",
            "physical_dtypes_verified",
            "registered_nullability_verified",
            "canonical_key_order_verified",
            "declared_row_count_equal",
            "declared_key_projection_equal",
        ):
            if row[field] is not True:
                raise StageReceiptError(f"semantic artifact record must retain {field}=true")
        if row["canonical_key_projection_algorithm"] != "sorted_registered_keys_canonical_jsonl_v1":
            raise StageReceiptError("semantic artifact key-projection algorithm is invalid")
        checks = _require_list(row["stage_invariant_checks"], name=f"semantic_stage_artifacts[{index}].checks")
        if not checks or any(not isinstance(item, str) or not item for item in checks):
            raise StageReceiptError("semantic artifact invariant checks must be nonblank strings")
    if observed_stages != expected_semantic_stages:
        raise StageReceiptError("semantic artifact stages do not match the transitive leaf chain")
    s02_row = artifacts[0]
    assert isinstance(s02_row, dict)
    if s02_row["canonical_key_projection_sha256"] != population["registered_s02_key_projection_sha256"]:
        raise StageReceiptError("semantic S02/population key hashes disagree")
    if s02_row["row_count"] != population["expected_population_row_count"]:
        raise StageReceiptError("semantic S02/population row counts disagree")

    relations = _require_list(payload["transitive_stage_relations"], name="transitive_stage_relations")
    if len(relations) != STAGES.index(str(stage)) - STAGES.index("S02"):
        raise StageReceiptError("semantic transitive relation count does not match the leaf stage")
    if any(not isinstance(item, str) or not item for item in relations):
        raise StageReceiptError("semantic transitive relations must be nonblank strings")
    format_policy = _require_exact_keys(
        payload["artifact_format_policy"],
        {"supported_normalized_suffixes", "unsupported_formats_fail_closed", "csv_and_ndjson_supported"},
        name="artifact_format_policy",
    )
    if (
        format_policy["supported_normalized_suffixes"] != list(SUPPORTED_SEMANTIC_ARTIFACT_SUFFIXES)
        or format_policy["unsupported_formats_fail_closed"] is not True
        or format_policy["csv_and_ndjson_supported"] is not False
    ):
        raise StageReceiptError("semantic artifact format policy is invalid")
    if payload["limitations"] != list(STAGE_SEMANTIC_LIMITATIONS):
        raise StageReceiptError("semantic receipt limitations must remain exact")

    payload_hash = _require_sha256(payload["receipt_payload_sha256"], name="receipt_payload_sha256")
    unhashed = dict(payload)
    unhashed.pop("receipt_id")
    unhashed.pop("receipt_payload_sha256")
    expected_hash = canonical_json_sha256(unhashed)
    if payload_hash != expected_hash:
        raise StageReceiptError("stage semantic receipt payload SHA-256 mismatch")
    expected_id = f"{payload['run_id']}-{str(stage).lower()}-semantic-{expected_hash[:24]}"
    if payload["receipt_id"] != expected_id:
        raise StageReceiptError("stage semantic receipt_id does not match its payload")


def render_stage_semantic_receipt(receipt: dict[str, JsonValue]) -> bytes:
    ready = _strict_json_value(receipt, location="stage semantic receipt")
    if not isinstance(ready, dict):
        raise StageReceiptError("stage semantic receipt must be an object")
    _validate_stage_semantic_receipt_payload(ready)
    return canonical_json_bytes(ready) + b"\n"


def load_stage_semantic_receipt(path: str | Path) -> dict[str, JsonValue]:
    resolved = Path(path)
    data = _regular_file_bytes(resolved, name="stage semantic receipt")
    payload = _parse_json_object(data, name=f"stage semantic receipt {resolved}")
    if data != canonical_json_bytes(payload) + b"\n":
        raise StageReceiptError("stage semantic receipt is not in canonical byte representation")
    _validate_stage_semantic_receipt_payload(payload)
    return payload


def write_stage_semantic_receipt(
    path: str | Path,
    receipt: dict[str, JsonValue],
) -> ReceiptWriteResult:
    """Atomically create a separate semantic receipt; never mutate its v2 byte receipt."""

    destination = Path(path)
    data = render_stage_semantic_receipt(receipt)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _existing_identical(destination, data):
        return ReceiptWriteResult(
            path=destination,
            receipt_id=str(receipt["receipt_id"]),
            file_sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            reused=True,
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
            linked = True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise StageReceiptError(f"failed to atomically create stage semantic receipt: {destination}") from exc
            _existing_identical(destination, data)
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return ReceiptWriteResult(
        path=destination,
        receipt_id=str(receipt["receipt_id"]),
        file_sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        reused=not linked,
    )


__all__ = [
    "ArtifactInput",
    "BoundFileInput",
    "ByteBindingVerification",
    "CONFIG_IDENTITY_VERIFICATION_STATUS",
    "IDENTITY_RECEIPT_KINDS",
    "OPAQUE_IDENTITY_VERIFICATION_STATUS",
    "PARENT_STAGES",
    "RECEIPT_SCOPE",
    "ReceiptWriteResult",
    "STAGE_IDENTITY_RECEIPT_KINDS",
    "STAGE_RECEIPT_SCHEMA_VERSION",
    "STAGE_RECEIPT_TYPE",
    "STAGE_SEMANTIC_LIMITATIONS",
    "STAGE_SEMANTIC_RECEIPT_SCHEMA_VERSION",
    "STAGE_SEMANTIC_RECEIPT_SCOPE",
    "STAGE_SEMANTIC_RECEIPT_TYPE",
    "STAGE_SEMANTIC_VERIFICATION_STATUS",
    "SUPPORTED_SEMANTIC_ARTIFACT_SUFFIXES",
    "StageReceiptError",
    "StageSemanticVerification",
    "StageSchemaIdentity",
    "UNVERIFIED_ARTIFACT_DECLARATIONS",
    "build_stage_receipt",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "canonical_stage_key_projection_sha256",
    "load_stage_semantic_receipt",
    "load_stage_receipt",
    "registered_stage_schema",
    "render_stage_semantic_receipt",
    "render_stage_receipt",
    "stage_receipt_config_consumer_parity_surface",
    "stage_receipt_config_parity_surface",
    "verify_stage_receipt_byte_bindings",
    "verify_stage_receipt_semantics",
    "verify_stage_population_identity_binding",
    "write_stage_semantic_receipt",
    "write_stage_receipt",
]
