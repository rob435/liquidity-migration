"""Diagnostic, tamper-evident byte bindings for strategy-overhaul stages.

This module binds already-created files and JSON identity documents by their
current bytes.  It does not validate an artifact's schema, rows, key projection,
outcome blindness, provenance, or strategy semantics.  Those values are
caller declarations and are labelled as unverified in every receipt.  The
config identity is the one exception at construction time: it must exactly
equal the repository-derived canonical identity.  Archival byte verification
does not reconsult mutable current factories or the current artifact registry.
Construction still checks the current registry declaration for every receipt in
an attached parent chain.
The writer refuses to overwrite a different receipt, but it does not make the
filesystem immutable and it never authorizes an outcome run.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeAlias, cast

from .strategy_overhaul_config_identity import (
    derive_continuous_a0_config_identity,
    derive_long_a0_config_identity,
    verify_a0_config_identity,
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
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
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
            CONFIG_IDENTITY_VERIFICATION_STATUS
            if kind == "config"
            else OPAQUE_IDENTITY_VERIFICATION_STATUS
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
    expected = (
        derive_continuous_a0_config_identity()
        if sleeve == "continuous"
        else derive_long_a0_config_identity()
    )
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
            f"{context} {sleeve}/{stage} declared schema does not match the "
            "construction-time current registry"
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
            f"{sleeve}/{stage} declared schema identity mismatch; "
            f"expected={expected!r}, supplied={supplied!r}"
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
        "declared_schema_identity": (
            dict(declared_schema_identity) if declared_schema_identity is not None else None
        ),
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
                raise StageReceiptError(
                    f"S01 {kind} identity byte binding does not match its S00 parent"
                )
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
        raise StageReceiptError(
            f"{stage} identity receipt kinds mismatch; missing={missing}, unknown={unknown}"
        )
    identity_records = {
        kind: _identity_receipt_record(identity_receipts[kind], kind=kind)
        for kind in required_identity_kinds
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
            CONFIG_IDENTITY_VERIFICATION_STATUS
            if identity_kind == "config"
            else OPAQUE_IDENTITY_VERIFICATION_STATUS
        )
        if row["semantic_verification_status"] != expected_status:
            raise StageReceiptError(
                f"{name}.semantic_verification_status must equal {expected_status!r}"
            )
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
                raise StageReceiptError(
                    f"artifact.declared_schema_identity.{field} must be a non-blank string"
                )
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
        current_registry_or_config_factories_consulted=(
            require_current_registry_declarations
        ),
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
    "StageReceiptError",
    "StageSchemaIdentity",
    "UNVERIFIED_ARTIFACT_DECLARATIONS",
    "build_stage_receipt",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "load_stage_receipt",
    "registered_stage_schema",
    "render_stage_receipt",
    "verify_stage_receipt_byte_bindings",
    "write_stage_receipt",
]
