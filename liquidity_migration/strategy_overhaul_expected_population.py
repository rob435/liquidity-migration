"""Canonical, receipt-bound expected populations for strategy-overhaul S02.

This module is the consumer-owned population boundary between key-only S01
inputs and the two S02 orchestrators.  It reconstructs the supplied population
from hourly identity keys and PIT membership keys on every verification.  The
two persisted key artifacts are strict, sorted canonical JSONL:

* ``source_keys.jsonl`` preserves the entire supplied causal/warm-up population
  produced by :mod:`strategy_overhaul_population_keys`;
* ``expected_population.jsonl`` is the exact finite projection consumed by
  S02.  LONG retains root-reconstructed ``symbol_age_days`` but deliberately
  drops the source-only ``hourly_bar_count`` field.

The receipt binds the current repository-derived config and registered scope,
the exact bytes and canonical JSON identities of supplied root/PIT/map
receipts, the exact raw key projections, the full pair-grain PIT rows, and the
instrument-map entries used by S02.  The S02 guard rechecks those runtime PIT
and map values before consumption.  A root receipt is only byte/identity bound
here: this primitive does not
upgrade its completeness, authenticity, lineage, or readiness claims.  The PIT
receipt is likewise byte-bound while the exact supplied membership projection
is hashed independently.  No OHLCV values, features, ranks, labels, or outcomes
are accepted or read.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import errno
import hashlib
import json
import math
import os
import stat
import tempfile
import weakref
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast

import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .continuous_demo import ContinuousDemoCycleConfig
from .long_native import LongNativeConfig
from .strategy_overhaul_config_identity import (
    A0ConfigIdentityError,
    JsonValue,
    assert_stage_config_identity_is_current,
    canonical_json_bytes,
    canonical_json_sha256,
    registered_scope_bounds_ms,
)
from .strategy_overhaul_identity_adapter import (
    MANIFEST_PAIR_COLUMNS,
    S02IdentityAdapterError,
    validate_manifest_pairs,
)
from .strategy_overhaul_phase0 import INSTRUMENT_MAP_REVIEW_STATUSES, InstrumentMapEntry
from .strategy_overhaul_population_keys import (
    CONTINUOUS_KEY_SCHEMA,
    HOURLY_KEY_SCHEMA,
    LONG_KEY_SCHEMA,
    MANIFEST_KEY_SCHEMA,
    PopulationKeyWindow,
    build_continuous_population_keys,
    build_long_population_keys,
    long_expected_population,
)


Sleeve = Literal["continuous", "long"]
ArtifactKind = Literal["source_keys", "expected_population"]

EXPECTED_POPULATION_SCHEMA_VERSION = "strategy_overhaul_expected_population_v1"
EXPECTED_POPULATION_RECEIPT_TYPE = "strategy_overhaul_exact_expected_population_receipt"
EXPECTED_POPULATION_FORMAT = "canonical_json_lines_utf8"
SOURCE_KEYS_FILENAME = "source_keys.jsonl"
EXPECTED_POPULATION_FILENAME = "expected_population.jsonl"
EXPECTED_POPULATION_RECEIPT_FILENAME = "expected_population_receipt.json"
LONG_MIN_HOURLY_BARS = 20

MANIFEST_PAIR_SCHEMA = MappingProxyType(
    {
        "venue": pl.String,
        "symbol": pl.String,
        "manifest_date": pl.Date,
        "membership_source": pl.String,
        "membership_inferred": pl.Boolean,
        "first_archive_observed_date": pl.Date,
        "reported_launch_time_ms": pl.Int64,
        "root_first_bar_ts_ms": pl.Int64,
        "provenance_limitation": pl.String,
        "coverage_state": pl.String,
    }
)
if tuple(MANIFEST_PAIR_SCHEMA) != MANIFEST_PAIR_COLUMNS:  # pragma: no cover - import-time invariant
    raise RuntimeError("expected-population manifest-pair schema drifted from the S02 identity adapter")

LONG_EXPECTED_POPULATION_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "symbol_age_days": pl.Int64,
    }
)
CONTINUOUS_REGISTERED_S02_KEY_SCHEMA = MappingProxyType(
    {
        "venue": pl.String,
        "symbol": pl.String,
        "decision_ts_ms": pl.Int64,
    }
)
LONG_REGISTERED_S02_KEY_SCHEMA = MappingProxyType(
    {
        "venue": pl.String,
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
    }
)

_SLEEVES = frozenset({"continuous", "long"})
_VENUES = frozenset({"bybit", "binance"})
_HEX = frozenset("0123456789abcdef")
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "sleeve",
        "venue",
        "window",
        "config_parity",
        "identity_bindings",
        "raw_inputs",
        "population_builder_receipt",
        "long_min_hourly_bars",
        "config_exclusions",
        "artifacts",
        "registered_s02_key_projection",
        "s02_consumer_contract",
        "exact_supplied_keys_and_ages_verified",
        "root_receipt_bytes_verified",
        "root_completeness_proven",
        "root_authenticity_proven",
        "pit_receipt_bytes_verified",
        "pit_projection_exactly_hashed",
        "pit_provenance_authenticated",
        "instrument_map_content_identity_verified",
        "instrument_map_expected_row_coverage_verified",
        "outcome_values_read",
        "numeric_kline_values_read",
        "outcome_run_authorized",
        "real_money_authorized",
        "limitations",
        "artifact_sha256",
    }
)
_ARTIFACT_RECORD_KEYS = frozenset(
    {
        "logical_path",
        "format",
        "columns",
        "dtypes",
        "sort_key",
        "row_count",
        "bytes",
        "file_sha256",
    }
)


class ExpectedPopulationError(ValueError):
    """A key, identity, canonicalization, or immutable-write invariant failed."""


@dataclass(frozen=True, slots=True)
class BoundIdentityReceipt:
    """One JSON identity input exposed only through a normalized logical path."""

    logical_path: str
    path: Path


@dataclass(frozen=True, slots=True)
class ExpectedPopulationArtifacts:
    sleeve: Sleeve
    venue: str
    source_keys_jsonl: bytes
    expected_population_jsonl: bytes
    receipt: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True, init=False, eq=False, weakref_slot=True)
class VerifiedExpectedPopulation:
    """In-memory result constructible only by the full reconstruction verifier."""

    sleeve: Sleeve
    venue: str
    source_keys: pl.DataFrame
    expected_population: pl.DataFrame
    receipt_sha256: str
    receipt_identity: ExpectedPopulationReceiptIdentity

    def __new__(cls) -> VerifiedExpectedPopulation:
        raise TypeError("VerifiedExpectedPopulation is constructible only by full reconstruction")


@dataclass(frozen=True, slots=True)
class ExpectedPopulationWriteResult:
    directory: Path
    source_keys_path: Path
    expected_population_path: Path
    receipt_path: Path
    receipt_file_sha256: str
    reused: bool


@dataclass(frozen=True, slots=True)
class ExpectedPopulationReceiptIdentity:
    """Strict receipt identity used by stage semantic verification."""

    sleeve: Sleeve
    venue: str
    registered_s02_key_columns: tuple[str, ...]
    registered_s02_key_sha256: str
    registered_s02_key_row_count: int
    source_keys_file_sha256: str
    source_keys_row_count: int
    expected_population_file_sha256: str
    expected_population_row_count: int
    manifest_pairs_canonical_jsonl_sha256: str
    manifest_pairs_row_count: int
    instrument_map_sha256: str
    instrument_map_version: str
    identity_bindings: Mapping[str, Mapping[str, JsonValue]]
    receipt_artifact_sha256: str
    receipt_identity_sha256: str
    receipt_file_sha256: str | None


@dataclass(frozen=True, slots=True)
class _VerifiedExpectedPopulationAttestation:
    sleeve: Sleeve
    venue: str
    source_keys_file_sha256: str
    expected_population_file_sha256: str
    receipt_sha256: str
    receipt_identity_attestation_sha256: str


_FULLY_RECONSTRUCTED_POPULATIONS: weakref.WeakKeyDictionary[
    VerifiedExpectedPopulation,
    _VerifiedExpectedPopulationAttestation,
] = weakref.WeakKeyDictionary()


def _receipt_identity_attestation_payload(identity: ExpectedPopulationReceiptIdentity) -> dict[str, JsonValue]:
    return {
        "sleeve": identity.sleeve,
        "venue": identity.venue,
        "registered_s02_key_columns": list(identity.registered_s02_key_columns),
        "registered_s02_key_sha256": identity.registered_s02_key_sha256,
        "registered_s02_key_row_count": identity.registered_s02_key_row_count,
        "source_keys_file_sha256": identity.source_keys_file_sha256,
        "source_keys_row_count": identity.source_keys_row_count,
        "expected_population_file_sha256": identity.expected_population_file_sha256,
        "expected_population_row_count": identity.expected_population_row_count,
        "manifest_pairs_canonical_jsonl_sha256": identity.manifest_pairs_canonical_jsonl_sha256,
        "manifest_pairs_row_count": identity.manifest_pairs_row_count,
        "instrument_map_sha256": identity.instrument_map_sha256,
        "instrument_map_version": identity.instrument_map_version,
        "identity_bindings": {kind: dict(record) for kind, record in identity.identity_bindings.items()},
        "receipt_artifact_sha256": identity.receipt_artifact_sha256,
        "receipt_identity_sha256": identity.receipt_identity_sha256,
        "receipt_file_sha256": identity.receipt_file_sha256,
    }


@dataclass(frozen=True, slots=True)
class _ObservedIdentity:
    payload: dict[str, JsonValue]
    record: dict[str, JsonValue]


def _require_sha256(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in _HEX for character in value)
    ):
        raise ExpectedPopulationError(f"{name} must be one lowercase 64-character SHA-256")
    return value


def _normalise_logical_path(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        raise ExpectedPopulationError(f"{name} must be a non-blank normalized POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise ExpectedPopulationError(f"{name} must not be absolute or contain dot traversal")
    return value


def _regular_file_bytes(path: Path, *, name: str) -> bytes:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise ExpectedPopulationError(f"{name} must be a readable regular non-symlink file: {path}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise ExpectedPopulationError(f"{name} must be a regular non-symlink file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExpectedPopulationError(f"{name} must be a readable regular non-symlink file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ExpectedPopulationError(f"{name} must be a regular non-symlink file: {path}")
        if (observed.st_dev, observed.st_ino) != (before.st_dev, before.st_ino):
            raise ExpectedPopulationError(f"{name} changed while being opened: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fingerprint_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        fingerprint_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        data = b"".join(chunks)
        if fingerprint_before != fingerprint_after or len(data) != after.st_size:
            raise ExpectedPopulationError(f"{name} changed while being read: {path}")
        try:
            final_path = path.lstat()
        except OSError as exc:
            raise ExpectedPopulationError(f"{name} path changed while being read: {path}") from exc
        if stat.S_ISLNK(final_path.st_mode) or (final_path.st_dev, final_path.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise ExpectedPopulationError(f"{name} path was replaced while being read: {path}")
        return data
    except OSError as exc:
        raise ExpectedPopulationError(f"{name} could not be read: {path}") from exc
    finally:
        os.close(descriptor)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ExpectedPopulationError(f"strict JSON contains duplicate key {key!r}")
        output[key] = value
    return output


def _reject_json_constant(value: str) -> None:
    raise ExpectedPopulationError(f"strict JSON contains invalid constant {value!r}")


def _strict_json_object(data: bytes, *, name: str) -> dict[str, JsonValue]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExpectedPopulationError(f"{name} is not UTF-8 JSON") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except ExpectedPopulationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ExpectedPopulationError(f"{name} is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExpectedPopulationError(f"{name} must contain one JSON object")
    try:
        canonical_json_bytes(payload)
    except A0ConfigIdentityError as exc:
        raise ExpectedPopulationError(f"{name} is not a supported strict JSON value: {exc}") from exc
    return cast(dict[str, JsonValue], payload)


def _validate_optional_self_hash(payload: Mapping[str, JsonValue], *, name: str) -> None:
    if "artifact_sha256" not in payload:
        return
    observed = _require_sha256(payload["artifact_sha256"], name=f"{name}.artifact_sha256")
    unhashed = dict(payload)
    unhashed.pop("artifact_sha256")
    if observed != canonical_json_sha256(unhashed):
        raise ExpectedPopulationError(f"{name} artifact SHA-256 mismatch")


def _observe_identity(source: BoundIdentityReceipt, *, name: str) -> _ObservedIdentity:
    logical_path = _normalise_logical_path(source.logical_path, name=f"{name}.logical_path")
    data = _regular_file_bytes(Path(source.path), name=name)
    payload = _strict_json_object(data, name=name)
    _validate_optional_self_hash(payload, name=name)
    record: dict[str, JsonValue] = {
        "logical_path": logical_path,
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        # This name intentionally matches the stage byte-binding record so the
        # semantic stage verifier can compare config/root/PIT/map identities
        # without translating between hash conventions.
        "identity_sha256": canonical_json_sha256(payload),
        "artifact_type": payload.get("artifact_type"),
        "declared_artifact_sha256": payload.get("artifact_sha256"),
    }
    return _ObservedIdentity(payload=payload, record=record)


def _date_to_ms(value: object, *, name: str) -> int:
    if not isinstance(value, str):
        raise ExpectedPopulationError(f"{name} must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ExpectedPopulationError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ExpectedPopulationError(f"{name} must be a canonical ISO date")
    return int(dt.datetime.combine(parsed, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _config_for_sleeve(
    sleeve: Sleeve,
    config: LongNativeConfig | ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
) -> None:
    try:
        assert_stage_config_identity_is_current(config, config_identity, sleeve=sleeve)
    except A0ConfigIdentityError as exc:
        raise ExpectedPopulationError(f"{sleeve} expected-population config identity parity failed: {exc}") from exc


def continuous_population_exclusions_parity_surface(
    config: ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return the config-derived exclusion surface consumed by this builder."""

    _config_for_sleeve("continuous", config, config_identity)
    return {"exclude_symbols": list(config.exclude_symbols)}


def long_population_and_rolling_windows_parity_surface(
    config: LongNativeConfig,
    config_identity: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return the exact LONG population/rolling values bound by this consumer.

    Only ``exclude_symbols`` changes the key-only expected population.  The
    remaining values are bound here for the downstream S02 rolling/rank
    consumers; they must not be misrepresented as key filters.
    """

    _config_for_sleeve("long", config, config_identity)
    return {
        "exclude_symbols": list(config.exclude_symbols),
        "universe_size": config.universe_size,
        "universe_volume_window_days": config.universe_volume_window_days,
        "min_listing_history_days": config.min_listing_history_days,
        "vol_estimate_window_days": config.vol_estimate_window_days,
    }


def continuous_expected_population_consumer_parity_surface(
    config: ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return an executable consumer receipt for CONTINUOUS exclusions."""

    target = continuous_population_exclusions_parity_surface(config, config_identity)
    return {
        "consumer_validator": (
            "liquidity_migration.strategy_overhaul_expected_population."
            "continuous_expected_population_consumer_parity_surface"
        ),
        "validated_targets": ["population_exclusions"],
        "validated_target_fields": {"population_exclusions": list(target)},
        "validated_consumers": {
            "population_exclusions": ["strategy_overhaul_expected_population.build_expected_population_artifacts"]
        },
        "population_exclusions": target,
    }


def long_expected_population_consumer_parity_surface(
    config: LongNativeConfig,
    config_identity: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return an executable consumer receipt for LONG population config."""

    target = long_population_and_rolling_windows_parity_surface(config, config_identity)
    return {
        "consumer_validator": (
            "liquidity_migration.strategy_overhaul_expected_population.long_expected_population_consumer_parity_surface"
        ),
        "validated_targets": ["population_and_rolling_windows"],
        "validated_target_fields": {"population_and_rolling_windows": list(target)},
        "validated_consumers": {
            "population_and_rolling_windows": [
                "strategy_overhaul_expected_population.build_expected_population_artifacts"
            ]
        },
        "population_and_rolling_windows": target,
    }


def _window_from_root(
    *,
    sleeve: Sleeve,
    venue: str,
    config_identity: dict[str, JsonValue],
    root: _ObservedIdentity,
) -> PopulationKeyWindow:
    payload = root.payload
    if payload.get("artifact_type") != "strategy_overhaul_root_snapshot":
        raise ExpectedPopulationError("root identity receipt has the wrong artifact_type")
    if payload.get("venue") != venue:
        raise ExpectedPopulationError("root identity receipt venue does not match the expected population")
    root_window = payload.get("window")
    if not isinstance(root_window, dict):
        raise ExpectedPopulationError("root identity receipt has no window object")
    identity_start = _date_to_ms(
        root_window.get("identity_history_start_date"),
        name="root.window.identity_history_start_date",
    )
    root_causal = _date_to_ms(
        root_window.get("causal_read_start_date"),
        name="root.window.causal_read_start_date",
    )
    root_signal_end = _date_to_ms(
        root_window.get("signal_end_date_exclusive"),
        name="root.window.signal_end_date_exclusive",
    )
    bounds = registered_scope_bounds_ms(config_identity)
    if root_causal > bounds["causal_read_start_date_ms"]:
        raise ExpectedPopulationError(f"{sleeve} root begins after the sleeve's registered causal-read boundary")
    if root_signal_end < bounds["signal_end_date_exclusive_ms"]:
        raise ExpectedPopulationError(f"{sleeve} root ends before the sleeve's registered signal boundary")
    for field in (
        "numeric_values_decoded",
        "returns_calculated",
        "labels_calculated",
        "outcome_run_authorized",
        "real_money_authorized",
    ):
        if payload.get(field) is not False:
            raise ExpectedPopulationError(f"root identity receipt must retain {field}=false")
    return PopulationKeyWindow(
        identity_history_start_ts_ms=identity_start,
        causal_read_start_ts_ms=bounds["causal_read_start_date_ms"],
        signal_start_ts_ms=bounds["signal_start_date_ms"],
        signal_end_ts_ms_exclusive=bounds["signal_end_date_exclusive_ms"],
    )


def _schema_for(sleeve: Sleeve, artifact_kind: ArtifactKind) -> Mapping[str, pl.DataType]:
    if artifact_kind == "source_keys":
        return CONTINUOUS_KEY_SCHEMA if sleeve == "continuous" else LONG_KEY_SCHEMA
    return CONTINUOUS_KEY_SCHEMA if sleeve == "continuous" else LONG_EXPECTED_POPULATION_SCHEMA


def _require_exact_frame_schema(
    frame: pl.DataFrame,
    expected: Mapping[str, pl.DataType],
    *,
    name: str,
) -> None:
    missing = sorted(set(expected) - set(frame.columns))
    unknown = sorted(set(frame.columns) - set(expected))
    if missing or unknown or len(frame.columns) != len(expected):
        raise ExpectedPopulationError(f"{name} projection mismatch; missing={missing}, unknown={unknown}")
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise ExpectedPopulationError(f"{name} has invalid dtypes: {mismatched}")


def _validate_key_frame(frame: pl.DataFrame, *, sleeve: Sleeve, artifact_kind: ArtifactKind) -> pl.DataFrame:
    schema = _schema_for(sleeve, artifact_kind)
    _require_exact_frame_schema(frame, schema, name=artifact_kind)
    invalid = frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("signal_ts_ms").is_null()
        | (pl.col("signal_ts_ms") <= 0)
        | ((pl.col("signal_ts_ms") % MS_PER_HOUR) != 0)
    )
    if sleeve == "long":
        invalid = frame.filter(
            pl.col("symbol").is_null()
            | (pl.col("symbol").str.strip_chars() == "")
            | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
            | pl.col("signal_ts_ms").is_null()
            | (pl.col("signal_ts_ms") <= 0)
            | ((pl.col("signal_ts_ms") % MS_PER_DAY) != 0)
            | pl.col("symbol_age_days").is_null()
            | (pl.col("symbol_age_days") <= 0)
        )
        if artifact_kind == "source_keys":
            invalid = invalid.filter(pl.lit(True))
            bad_counts = frame.filter(
                pl.col("hourly_bar_count").is_null()
                | (pl.col("hourly_bar_count") < LONG_MIN_HOURLY_BARS)
                | (pl.col("hourly_bar_count") > 24)
            )
            if not bad_counts.is_empty():
                raise ExpectedPopulationError(
                    f"LONG source_keys hourly_bar_count must be between {LONG_MIN_HOURLY_BARS} and 24"
                )
    if not invalid.is_empty():
        raise ExpectedPopulationError(f"{artifact_kind} contains invalid keys or ages")
    duplicates = frame.group_by(["symbol", "signal_ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ExpectedPopulationError(f"{artifact_kind} contains duplicate (symbol,signal_ts_ms) keys")
    ordered = frame.sort(["symbol", "signal_ts_ms"])
    if not ordered.equals(frame):
        raise ExpectedPopulationError(f"{artifact_kind} must be sorted by (symbol,signal_ts_ms)")
    return ordered


def render_expected_population_jsonl(
    frame: pl.DataFrame,
    *,
    sleeve: Sleeve,
    artifact_kind: ArtifactKind,
) -> bytes:
    """Render one exact sorted S02 population projection as canonical JSONL."""

    if sleeve not in _SLEEVES:
        raise ExpectedPopulationError("sleeve must be continuous or long")
    canonical = _validate_key_frame(frame, sleeve=sleeve, artifact_kind=artifact_kind)
    return b"".join(canonical_json_bytes(row) + b"\n" for row in canonical.iter_rows(named=True))


def parse_expected_population_jsonl(
    data: bytes,
    *,
    sleeve: Sleeve,
    artifact_kind: ArtifactKind,
) -> pl.DataFrame:
    """Parse canonical JSONL using the registered finite Polars schema."""

    if sleeve not in _SLEEVES:
        raise ExpectedPopulationError("sleeve must be continuous or long")
    schema = _schema_for(sleeve, artifact_kind)
    if not data:
        return pl.DataFrame(schema=dict(schema))
    if not data.endswith(b"\n"):
        raise ExpectedPopulationError(f"{artifact_kind} must end with one newline")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(data.splitlines(), start=1):
        if not raw:
            raise ExpectedPopulationError(f"{artifact_kind} contains a blank JSONL row")
        row = _strict_json_object(raw, name=f"{artifact_kind} row {index}")
        if set(row) != set(schema) or len(row) != len(schema):
            raise ExpectedPopulationError(f"{artifact_kind} row {index} has an invalid projection")
        if canonical_json_bytes(row) != raw:
            raise ExpectedPopulationError(f"{artifact_kind} row {index} is not canonical JSON")
        rows.append(cast(dict[str, Any], row))
    try:
        frame = pl.DataFrame(rows, schema=dict(schema))
    except (TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise ExpectedPopulationError(f"{artifact_kind} cannot be loaded with its exact schema: {exc}") from exc
    canonical = _validate_key_frame(frame, sleeve=sleeve, artifact_kind=artifact_kind)
    if render_expected_population_jsonl(canonical, sleeve=sleeve, artifact_kind=artifact_kind) != data:
        raise ExpectedPopulationError(f"{artifact_kind} canonical round-trip mismatch")
    return canonical


def _registered_s02_key_schema(sleeve: Sleeve) -> Mapping[str, pl.DataType]:
    return CONTINUOUS_REGISTERED_S02_KEY_SCHEMA if sleeve == "continuous" else LONG_REGISTERED_S02_KEY_SCHEMA


def render_registered_s02_key_jsonl(frame: pl.DataFrame, *, sleeve: Sleeve) -> bytes:
    """Render the exact registered S02 artifact-key projection as canonical JSONL.

    CONTINUOUS uses ``(venue,symbol,decision_ts_ms)``; LONG uses
    ``(venue,symbol,signal_ts_ms)``.  This is the shared serialization contract
    for expected-population and stage-artifact semantic comparison.
    """

    if sleeve not in _SLEEVES:
        raise ExpectedPopulationError("sleeve must be continuous or long")
    schema = _registered_s02_key_schema(sleeve)
    _require_exact_frame_schema(frame, schema, name="registered S02 key projection")
    key_time = "decision_ts_ms" if sleeve == "continuous" else "signal_ts_ms"
    invalid = frame.filter(
        pl.col("venue").is_null()
        | ~pl.col("venue").is_in(sorted(_VENUES))
        | (pl.col("venue") != pl.col("venue").str.strip_chars().str.to_lowercase())
        | pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col(key_time).is_null()
        | (pl.col(key_time) <= 0)
        | ((pl.col(key_time) % MS_PER_HOUR) != 0)
    )
    if not invalid.is_empty():
        raise ExpectedPopulationError("registered S02 key projection contains invalid keys")
    duplicates = frame.group_by(["venue", "symbol", key_time]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ExpectedPopulationError("registered S02 key projection contains duplicate keys")
    ordered = frame.sort(["venue", "symbol", key_time])
    if not ordered.equals(frame):
        raise ExpectedPopulationError(f"registered S02 key projection must be sorted by (venue,symbol,{key_time})")
    return b"".join(canonical_json_bytes(row) + b"\n" for row in ordered.iter_rows(named=True))


def registered_s02_key_sha256(frame: pl.DataFrame, *, sleeve: Sleeve) -> str:
    """Return SHA-256 over :func:`render_registered_s02_key_jsonl` bytes."""

    return hashlib.sha256(render_registered_s02_key_jsonl(frame, sleeve=sleeve)).hexdigest()


def _registered_s02_keys(
    expected_population: pl.DataFrame,
    *,
    sleeve: Sleeve,
    venue: str,
    config: LongNativeConfig | ContinuousDemoCycleConfig,
) -> pl.DataFrame:
    if sleeve == "continuous":
        if not isinstance(config, ContinuousDemoCycleConfig):  # pragma: no cover - guarded by caller
            raise ExpectedPopulationError("continuous registered keys require ContinuousDemoCycleConfig")
        delay_ms = config.entry_confirm_delay_hours * MS_PER_HOUR
        return expected_population.select(
            pl.lit(venue, dtype=pl.String).alias("venue"),
            "symbol",
            (pl.col("signal_ts_ms") + delay_ms).cast(pl.Int64).alias("decision_ts_ms"),
        ).sort(["venue", "symbol", "decision_ts_ms"])
    return expected_population.select(
        pl.lit(venue, dtype=pl.String).alias("venue"),
        "symbol",
        "signal_ts_ms",
    ).sort(["venue", "symbol", "signal_ts_ms"])


def _frame_jsonl_hash(
    frame: pl.DataFrame,
    *,
    schema: Mapping[str, pl.DataType],
    name: str,
) -> tuple[int, str]:
    _require_exact_frame_schema(frame, schema, name=name)
    ordered = frame.sort(list(schema)) if not frame.is_empty() else frame
    data = b"".join(
        canonical_json_bytes(
            {
                key: (value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else value)
                for key, value in row.items()
            }
        )
        + b"\n"
        for row in ordered.iter_rows(named=True)
    )
    return ordered.height, hashlib.sha256(data).hexdigest()


def canonical_manifest_pair_identity(
    frame: pl.DataFrame,
    *,
    venue: str,
) -> tuple[int, str]:
    """Validate and hash the exact pair-grain PIT input consumed by S02."""

    try:
        validate_manifest_pairs(frame, venue=venue)
    except S02IdentityAdapterError as exc:
        raise ExpectedPopulationError(f"manifest-pair identity input failed: {exc}") from exc
    return _frame_jsonl_hash(
        frame,
        schema=MANIFEST_PAIR_SCHEMA,
        name="manifest_pairs",
    )


def _validate_raw_window(
    hourly_keys: pl.DataFrame,
    manifest_keys: pl.DataFrame,
    *,
    window: PopulationKeyWindow,
) -> None:
    _require_exact_frame_schema(hourly_keys, HOURLY_KEY_SCHEMA, name="hourly_keys")
    _require_exact_frame_schema(manifest_keys, MANIFEST_KEY_SCHEMA, name="manifest_keys")
    outside_hourly = hourly_keys.filter(
        (pl.col("ts_ms") < window.identity_history_start_ts_ms) | (pl.col("ts_ms") >= window.signal_end_ts_ms_exclusive)
    )
    if not outside_hourly.is_empty():
        raise ExpectedPopulationError("hourly_keys falls outside the root identity/signal window")
    history_date = dt.datetime.fromtimestamp(
        window.identity_history_start_ts_ms / 1000,
        tz=dt.timezone.utc,
    ).date()
    end_date = dt.datetime.fromtimestamp(
        window.signal_end_ts_ms_exclusive / 1000,
        tz=dt.timezone.utc,
    ).date()
    outside_manifest = manifest_keys.filter(
        (pl.col("manifest_date") < history_date) | (pl.col("manifest_date") >= end_date)
    )
    if not outside_manifest.is_empty():
        raise ExpectedPopulationError("manifest_keys falls outside the root identity/signal window")


def _validate_pit_identity(
    pit: _ObservedIdentity,
    *,
    venue: str,
    manifest_row_count: int,
) -> None:
    payload = pit.payload
    if payload.get("collapsed_membership_key") == ["venue", "symbol", "date"]:
        venues = payload.get("venues")
        if not isinstance(venues, dict) or not isinstance(venues.get(venue), dict):
            raise ExpectedPopulationError("PIT identity receipt does not contain the requested venue")
        venue_payload = venues[venue]
        observed_count = venue_payload.get("membership_pair_count")
        if observed_count != manifest_row_count:
            raise ExpectedPopulationError(
                "PIT identity membership_pair_count does not equal the exact supplied manifest projection"
            )
    elif payload.get("artifact_type") == "strategy_overhaul_venue_local_manifest_projection":
        venues = payload.get("venues")
        if not isinstance(venues, dict) or not isinstance(venues.get(venue), dict):
            raise ExpectedPopulationError("manifest-projection identity does not contain the requested venue")
        observed_count = venues[venue].get("source_projection_row_count")
        if observed_count != manifest_row_count:
            raise ExpectedPopulationError(
                "manifest-projection identity row count does not equal the exact supplied PIT projection"
            )
    else:
        raise ExpectedPopulationError(
            "PIT identity receipt must be Phase-0 PIT provenance or a venue-local manifest projection"
        )
    for field in ("outcome_values_read", "outcome_run_authorized", "real_money_authorized"):
        if field in payload and payload.get(field) is not False:
            raise ExpectedPopulationError(f"PIT identity receipt must retain {field}=false")


def _parse_map_date(value: str | None, *, name: str) -> dt.date | None:
    if value is None:
        return None
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ExpectedPopulationError(f"instrument map {name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ExpectedPopulationError(f"instrument map {name} must be a canonical ISO date")
    return parsed


def _validate_map_entries(
    entries: Sequence[InstrumentMapEntry],
    *,
    venue: str,
    expected_population: pl.DataFrame,
    sleeve: Sleeve,
) -> None:
    if not entries:
        raise ExpectedPopulationError("instrument map must not be empty")
    grouped: dict[tuple[str, str], list[tuple[dt.date, dt.date, InstrumentMapEntry]]] = defaultdict(list)
    canonical_intervals: dict[tuple[str, str], list[tuple[dt.date, dt.date, str]]] = defaultdict(list)
    canonical_products: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
    for entry in entries:
        if not isinstance(entry, InstrumentMapEntry):
            raise ExpectedPopulationError("instrument map entries must be InstrumentMapEntry instances")
        if (
            entry.venue != entry.venue.strip().lower()
            or entry.symbol != entry.symbol.strip()
            or entry.canonical_instrument != entry.canonical_instrument.strip()
            or entry.base_asset != entry.base_asset.strip().upper()
            or entry.quote_asset != entry.quote_asset.strip().upper()
            or entry.settlement_asset != entry.settlement_asset.strip().upper()
            or entry.contract_type != entry.contract_type.strip().lower()
            or entry.mapping_source != entry.mapping_source.strip()
            or entry.review_status not in INSTRUMENT_MAP_REVIEW_STATUSES
            or isinstance(entry.contract_multiplier, bool)
            or not isinstance(entry.contract_multiplier, (int, float))
            or not math.isfinite(float(entry.contract_multiplier))
            or float(entry.contract_multiplier) <= 0.0
        ):
            raise ExpectedPopulationError("instrument map contains a non-canonical or unreviewed entry")
        start = _parse_map_date(entry.valid_from_date, name="valid_from_date")
        end = _parse_map_date(entry.valid_to_date_exclusive, name="valid_to_date_exclusive")
        assert start is not None
        end_value = end or dt.date.max
        if start >= end_value:
            raise ExpectedPopulationError("instrument map validity interval is empty or reversed")
        grouped[(entry.venue, entry.symbol)].append((start, end_value, entry))
        canonical_intervals[(entry.venue, entry.canonical_instrument)].append((start, end_value, entry.symbol))
        canonical_products[entry.canonical_instrument].add(
            (
                entry.base_asset,
                entry.quote_asset,
                entry.settlement_asset,
                entry.contract_type,
            )
        )
    product_conflicts = {
        canonical: sorted(products) for canonical, products in canonical_products.items() if len(products) > 1
    }
    if product_conflicts:
        raise ExpectedPopulationError(f"canonical instruments have conflicting product identities: {product_conflicts}")
    for key, intervals in grouped.items():
        intervals.sort(key=lambda row: (row[0], row[1], row[2].canonical_instrument))
        for left, right in zip(intervals, intervals[1:]):
            if right[0] < left[1]:
                raise ExpectedPopulationError(f"instrument map has overlapping intervals for {key!r}")
    for key, intervals in canonical_intervals.items():
        for index, (left_start, left_end, left_symbol) in enumerate(intervals):
            for right_start, right_end, right_symbol in intervals[index + 1 :]:
                if left_symbol != right_symbol and max(left_start, right_start) < min(left_end, right_end):
                    raise ExpectedPopulationError(
                        f"instrument map has a same-venue canonical alias collision for {key!r}"
                    )
    for row in expected_population.select("symbol", "signal_ts_ms").iter_rows(named=True):
        signal_ts = int(row["signal_ts_ms"])
        instant = signal_ts if sleeve == "continuous" else signal_ts - 1
        manifest_date = dt.datetime.fromtimestamp(instant / 1000, tz=dt.timezone.utc).date()
        matches = [
            entry for start, end, entry in grouped.get((venue, str(row["symbol"])), []) if start <= manifest_date < end
        ]
        if len(matches) != 1:
            raise ExpectedPopulationError(
                "instrument map must resolve exactly once for every expected-population row; "
                f"key={(row['symbol'], signal_ts)!r}, matches={len(matches)}"
            )


def _validate_map_identity(
    identity: _ObservedIdentity,
    *,
    entries: Sequence[InstrumentMapEntry],
    version: str,
) -> str:
    if not isinstance(version, str) or not version.strip() or version != version.strip():
        raise ExpectedPopulationError("instrument_map_version must be a non-blank trimmed string")
    entry_payload = [dataclasses.asdict(entry) for entry in entries]
    map_sha256 = canonical_json_sha256(entry_payload)
    payload = identity.payload
    if payload.get("version") != version or payload.get("map_sha256") != map_sha256:
        raise ExpectedPopulationError("instrument-map receipt version/content identity mismatch")
    if "entry_count" in payload and payload.get("entry_count") != len(entries):
        raise ExpectedPopulationError("instrument-map receipt entry_count mismatch")
    if "map_entry_count" in payload and payload.get("map_entry_count") != len(entries):
        raise ExpectedPopulationError("instrument-map receipt map_entry_count mismatch")
    for field in ("outcome_values_read", "outcome_run_authorized", "real_money_authorized"):
        if field in payload and payload.get(field) is not False:
            raise ExpectedPopulationError(f"instrument-map receipt must retain {field}=false")
    return map_sha256


def _artifact_record(
    data: bytes,
    frame: pl.DataFrame,
    *,
    sleeve: Sleeve,
    artifact_kind: ArtifactKind,
    logical_path: str,
) -> dict[str, JsonValue]:
    schema = _schema_for(sleeve, artifact_kind)
    if parse_expected_population_jsonl(data, sleeve=sleeve, artifact_kind=artifact_kind).equals(frame) is False:
        raise ExpectedPopulationError(f"{artifact_kind} render/parse frame mismatch")
    return {
        "logical_path": logical_path,
        "format": EXPECTED_POPULATION_FORMAT,
        "columns": list(schema),
        "dtypes": [str(dtype) for dtype in schema.values()],
        "sort_key": ["symbol", "signal_ts_ms"],
        "row_count": frame.height,
        "bytes": len(data),
        "file_sha256": hashlib.sha256(data).hexdigest(),
    }


def _validate_artifact_receipt_record(
    value: object,
    *,
    sleeve: Sleeve,
    artifact_kind: ArtifactKind,
) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != _ARTIFACT_RECORD_KEYS:
        raise ExpectedPopulationError(f"receipt {artifact_kind} artifact record has an invalid schema")
    record = cast(dict[str, JsonValue], value)
    schema = _schema_for(sleeve, artifact_kind)
    logical_path = SOURCE_KEYS_FILENAME if artifact_kind == "source_keys" else EXPECTED_POPULATION_FILENAME
    if (
        record.get("logical_path") != logical_path
        or record.get("format") != EXPECTED_POPULATION_FORMAT
        or record.get("columns") != list(schema)
        or record.get("dtypes") != [str(dtype) for dtype in schema.values()]
        or record.get("sort_key") != ["symbol", "signal_ts_ms"]
    ):
        raise ExpectedPopulationError(f"receipt {artifact_kind} artifact format/schema mismatch")
    for field in ("row_count", "bytes"):
        observed = record.get(field)
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise ExpectedPopulationError(f"receipt {artifact_kind}.{field} must be a non-negative integer")
    _require_sha256(record.get("file_sha256"), name=f"receipt.{artifact_kind}.file_sha256")
    return record


def _validate_raw_input_record(
    value: object,
    *,
    name: str,
    schema: Mapping[str, pl.DataType],
) -> dict[str, JsonValue]:
    expected_keys = {"columns", "dtypes", "row_count", "canonical_jsonl_sha256"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ExpectedPopulationError(f"receipt raw_inputs.{name} has an invalid schema")
    record = cast(dict[str, JsonValue], value)
    if record.get("columns") != list(schema) or record.get("dtypes") != [str(dtype) for dtype in schema.values()]:
        raise ExpectedPopulationError(f"receipt raw_inputs.{name} column/dtype contract mismatch")
    row_count = record.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise ExpectedPopulationError(f"receipt raw_inputs.{name}.row_count must be non-negative")
    _require_sha256(
        record.get("canonical_jsonl_sha256"),
        name=f"receipt.raw_inputs.{name}.canonical_jsonl_sha256",
    )
    return record


def _derive_expected_population(
    hourly_keys: pl.DataFrame,
    manifest_keys: pl.DataFrame,
    manifest_pairs: pl.DataFrame,
    *,
    sleeve: Sleeve,
    venue: str,
    config: LongNativeConfig | ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
    config_identity_receipt: BoundIdentityReceipt,
    root_identity_receipt: BoundIdentityReceipt,
    pit_identity_receipt: BoundIdentityReceipt,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
    instrument_map_identity_receipt: BoundIdentityReceipt,
) -> ExpectedPopulationArtifacts:
    if sleeve not in _SLEEVES:
        raise ExpectedPopulationError("sleeve must be continuous or long")
    if venue not in _VENUES or venue != venue.strip().lower():
        raise ExpectedPopulationError(f"venue must be one of {sorted(_VENUES)}")
    _config_for_sleeve(sleeve, config, config_identity)
    if sleeve == "continuous":
        if not isinstance(config, ContinuousDemoCycleConfig):
            raise ExpectedPopulationError("continuous expected population requires ContinuousDemoCycleConfig")
        config_parity = continuous_population_exclusions_parity_surface(config, config_identity)
    else:
        if not isinstance(config, LongNativeConfig):
            raise ExpectedPopulationError("LONG expected population requires LongNativeConfig")
        config_parity = long_population_and_rolling_windows_parity_surface(config, config_identity)

    manifest_pair_row_count, manifest_pair_sha256 = canonical_manifest_pair_identity(
        manifest_pairs,
        venue=venue,
    )
    projected_manifest_keys = manifest_pairs.select("symbol", "manifest_date").sort(["symbol", "manifest_date"])
    if not projected_manifest_keys.equals(manifest_keys.sort(["symbol", "manifest_date"])):
        raise ExpectedPopulationError(
            "manifest_keys must exactly equal the key projection of the bound S02 manifest pairs"
        )

    config_receipt = _observe_identity(config_identity_receipt, name="config identity receipt")
    if config_receipt.payload != config_identity:
        raise ExpectedPopulationError(
            "config identity receipt payload does not exactly equal the supplied current config identity"
        )
    root = _observe_identity(root_identity_receipt, name="root identity receipt")
    pit = _observe_identity(pit_identity_receipt, name="PIT identity receipt")
    map_identity = _observe_identity(instrument_map_identity_receipt, name="instrument-map identity receipt")
    window = _window_from_root(
        sleeve=sleeve,
        venue=venue,
        config_identity=config_identity,
        root=root,
    )
    _validate_raw_window(hourly_keys, manifest_keys, window=window)
    hourly_row_count, hourly_sha256 = _frame_jsonl_hash(
        hourly_keys,
        schema=HOURLY_KEY_SCHEMA,
        name="hourly_keys",
    )
    manifest_row_count, manifest_sha256 = _frame_jsonl_hash(
        manifest_keys,
        schema=MANIFEST_KEY_SCHEMA,
        name="manifest_keys",
    )
    _validate_pit_identity(pit, venue=venue, manifest_row_count=manifest_row_count)
    map_sha256 = _validate_map_identity(
        map_identity,
        entries=instrument_map,
        version=instrument_map_version,
    )

    if sleeve == "continuous":
        built = build_continuous_population_keys(
            hourly_keys,
            manifest_keys,
            venue=venue,
            window=window,
        )
    else:
        built = build_long_population_keys(
            hourly_keys,
            manifest_keys,
            venue=venue,
            window=window,
            min_hourly_bars=LONG_MIN_HOURLY_BARS,
        )
    excluded_symbols = tuple(cast(Sequence[str], config_parity["exclude_symbols"]))
    source = built.source_keys.filter(~pl.col("symbol").is_in(list(excluded_symbols)))
    signal = built.signal_keys.filter(~pl.col("symbol").is_in(list(excluded_symbols)))
    expected = signal if sleeve == "continuous" else long_expected_population(signal)
    source = source.select(list(_schema_for(sleeve, "source_keys"))).sort(["symbol", "signal_ts_ms"])
    expected = expected.select(list(_schema_for(sleeve, "expected_population"))).sort(["symbol", "signal_ts_ms"])
    _validate_key_frame(source, sleeve=sleeve, artifact_kind="source_keys")
    _validate_key_frame(expected, sleeve=sleeve, artifact_kind="expected_population")
    _validate_map_entries(
        instrument_map,
        venue=venue,
        expected_population=expected,
        sleeve=sleeve,
    )

    source_jsonl = render_expected_population_jsonl(
        source,
        sleeve=sleeve,
        artifact_kind="source_keys",
    )
    expected_jsonl = render_expected_population_jsonl(
        expected,
        sleeve=sleeve,
        artifact_kind="expected_population",
    )
    registered_keys = _registered_s02_keys(
        expected,
        sleeve=sleeve,
        venue=venue,
        config=config,
    )
    registered_key_jsonl = render_registered_s02_key_jsonl(registered_keys, sleeve=sleeve)
    registered_key_schema = _registered_s02_key_schema(sleeve)
    registered_key_time = "decision_ts_ms" if sleeve == "continuous" else "signal_ts_ms"
    builder_receipt = dict(built.receipt)
    builder_hash = builder_receipt.get("artifact_sha256")
    unhashed_builder = dict(builder_receipt)
    unhashed_builder.pop("artifact_sha256", None)
    if builder_hash != canonical_json_sha256(unhashed_builder):
        raise ExpectedPopulationError("population-key builder receipt self-hash mismatch")

    removed_source = built.source_keys.height - source.height
    removed_signal = built.signal_keys.height - signal.height
    receipt: dict[str, JsonValue] = {
        "schema_version": EXPECTED_POPULATION_SCHEMA_VERSION,
        "artifact_type": EXPECTED_POPULATION_RECEIPT_TYPE,
        "sleeve": sleeve,
        "venue": venue,
        "window": cast(JsonValue, dataclasses.asdict(window)),
        "config_parity": cast(JsonValue, config_parity),
        "identity_bindings": {
            "config": {
                **config_receipt.record,
                "config_payload_identity_sha256": config_identity["identity_sha256"],
                "canonical_config_sha256": config_identity["canonical_config_sha256"],
                "registered_scope_sha256": config_identity["scope_sha256"],
                "component_config_sha256": config_identity.get("component_config_sha256"),
            },
            "root": root.record,
            "pit": pit.record,
            "instrument_map": {
                **map_identity.record,
                "version": instrument_map_version,
                "map_sha256": map_sha256,
                "entry_count": len(instrument_map),
            },
        },
        "raw_inputs": {
            "hourly_keys": {
                "columns": list(HOURLY_KEY_SCHEMA),
                "dtypes": [str(dtype) for dtype in HOURLY_KEY_SCHEMA.values()],
                "row_count": hourly_row_count,
                "canonical_jsonl_sha256": hourly_sha256,
            },
            "manifest_keys": {
                "columns": list(MANIFEST_KEY_SCHEMA),
                "dtypes": [str(dtype) for dtype in MANIFEST_KEY_SCHEMA.values()],
                "row_count": manifest_row_count,
                "canonical_jsonl_sha256": manifest_sha256,
            },
            "manifest_pairs": {
                "columns": list(MANIFEST_PAIR_SCHEMA),
                "dtypes": [str(dtype) for dtype in MANIFEST_PAIR_SCHEMA.values()],
                "row_count": manifest_pair_row_count,
                "canonical_jsonl_sha256": manifest_pair_sha256,
            },
        },
        "population_builder_receipt": cast(JsonValue, builder_receipt),
        "long_min_hourly_bars": LONG_MIN_HOURLY_BARS if sleeve == "long" else None,
        "config_exclusions": {
            "symbols": list(excluded_symbols),
            "source_rows_removed": removed_source,
            "signal_rows_removed": removed_signal,
            "excluded_symbols_absent_from_artifacts": not bool(
                set(source["symbol"].to_list()) & set(excluded_symbols)
                or set(expected["symbol"].to_list()) & set(excluded_symbols)
            ),
        },
        "artifacts": {
            "source_keys": _artifact_record(
                source_jsonl,
                source,
                sleeve=sleeve,
                artifact_kind="source_keys",
                logical_path=SOURCE_KEYS_FILENAME,
            ),
            "expected_population": _artifact_record(
                expected_jsonl,
                expected,
                sleeve=sleeve,
                artifact_kind="expected_population",
                logical_path=EXPECTED_POPULATION_FILENAME,
            ),
        },
        "registered_s02_key_projection": {
            "format": EXPECTED_POPULATION_FORMAT,
            "columns": list(registered_key_schema),
            "dtypes": [str(dtype) for dtype in registered_key_schema.values()],
            "sort_key": ["venue", "symbol", registered_key_time],
            "row_count": registered_keys.height,
            "canonical_jsonl_sha256": hashlib.sha256(registered_key_jsonl).hexdigest(),
            "derivation": (
                "venue_constant_plus_signal_ts_ms_plus_entry_confirm_delay_hours"
                if sleeve == "continuous"
                else "venue_constant_plus_expected_population_signal_ts_ms"
            ),
            "entry_confirm_delay_hours": (
                config.entry_confirm_delay_hours if isinstance(config, ContinuousDemoCycleConfig) else None
            ),
        },
        "s02_consumer_contract": {
            "verified_population_parameter": "verified_population",
            "runtime_manifest_pairs_parameter": "manifest_pairs",
            "runtime_instrument_map_parameter": "instrument_map",
            "runtime_instrument_map_version_parameter": "instrument_map_version",
            "full_reconstruction_verifier": (
                "liquidity_migration.strategy_overhaul_expected_population.verify_expected_population_artifacts"
            ),
            "s02_consumer_guard": (
                "liquidity_migration.strategy_overhaul_expected_population.verified_expected_population_s02_inputs"
            ),
        },
        "exact_supplied_keys_and_ages_verified": True,
        "root_receipt_bytes_verified": True,
        "root_completeness_proven": False,
        "root_authenticity_proven": False,
        "pit_receipt_bytes_verified": True,
        "pit_projection_exactly_hashed": True,
        "pit_provenance_authenticated": False,
        "instrument_map_content_identity_verified": True,
        "instrument_map_expected_row_coverage_verified": True,
        "outcome_values_read": False,
        "numeric_kline_values_read": False,
        "outcome_run_authorized": False,
        "real_money_authorized": False,
        "limitations": [
            "root receipt bytes and canonical JSON identity are bound but root completeness, authenticity, and lineage are not upgraded",
            "PIT receipt bytes are bound and the supplied membership projection is hashed exactly, but upstream PIT provenance is not authenticated",
            "supplied key projections are bound independently; their completeness and derivation from the root/PIT receipts are not proven here",
            "venue-local map coverage does not establish cross-venue alias or economic-unit portability",
            "rolling-window config values are identity-bound; only config exclusions alter this key-only population",
            "construction and full verification materialize canonical JSONL in memory rather than streaming partitions",
            "the writer is atomic per file but not transactional across the three-file population bundle",
        ],
    }
    receipt["artifact_sha256"] = canonical_json_sha256(receipt)
    return ExpectedPopulationArtifacts(
        sleeve=sleeve,
        venue=venue,
        source_keys_jsonl=source_jsonl,
        expected_population_jsonl=expected_jsonl,
        receipt=MappingProxyType(receipt),
    )


def build_expected_population_artifacts(
    hourly_keys: pl.DataFrame,
    manifest_keys: pl.DataFrame,
    manifest_pairs: pl.DataFrame,
    *,
    sleeve: Sleeve,
    venue: str,
    config: LongNativeConfig | ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
    config_identity_receipt: BoundIdentityReceipt,
    root_identity_receipt: BoundIdentityReceipt,
    pit_identity_receipt: BoundIdentityReceipt,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
    instrument_map_identity_receipt: BoundIdentityReceipt,
) -> ExpectedPopulationArtifacts:
    """Construct canonical population artifacts from outcome-blind key inputs."""

    return _derive_expected_population(
        hourly_keys,
        manifest_keys,
        manifest_pairs,
        sleeve=sleeve,
        venue=venue,
        config=config,
        config_identity=config_identity,
        config_identity_receipt=config_identity_receipt,
        root_identity_receipt=root_identity_receipt,
        pit_identity_receipt=pit_identity_receipt,
        instrument_map=instrument_map,
        instrument_map_version=instrument_map_version,
        instrument_map_identity_receipt=instrument_map_identity_receipt,
    )


def _validate_receipt_shape(receipt: Mapping[str, JsonValue], *, sleeve: Sleeve, venue: str) -> None:
    missing = sorted(_RECEIPT_KEYS - set(receipt))
    unknown = sorted(set(receipt) - _RECEIPT_KEYS)
    if missing or unknown:
        raise ExpectedPopulationError(
            f"expected-population receipt keys mismatch; missing={missing}, unknown={unknown}"
        )
    if (
        receipt.get("schema_version") != EXPECTED_POPULATION_SCHEMA_VERSION
        or receipt.get("artifact_type") != EXPECTED_POPULATION_RECEIPT_TYPE
        or receipt.get("sleeve") != sleeve
        or receipt.get("venue") != venue
    ):
        raise ExpectedPopulationError("expected-population receipt type/sleeve/venue mismatch")
    observed = _require_sha256(receipt.get("artifact_sha256"), name="receipt.artifact_sha256")
    unhashed = dict(receipt)
    unhashed.pop("artifact_sha256")
    if observed != canonical_json_sha256(unhashed):
        raise ExpectedPopulationError("expected-population receipt artifact SHA-256 mismatch")
    bindings = receipt.get("identity_bindings")
    if not isinstance(bindings, dict) or set(bindings) != {"config", "root", "pit", "instrument_map"}:
        raise ExpectedPopulationError(
            "expected-population identity_bindings must contain exactly config/root/pit/instrument_map"
        )
    for kind in ("config", "root", "pit", "instrument_map"):
        record = bindings.get(kind)
        if not isinstance(record, dict):
            raise ExpectedPopulationError(f"identity_bindings.{kind} must be an object")
        _normalise_logical_path(record.get("logical_path"), name=f"identity_bindings.{kind}.logical_path")
        _require_sha256(record.get("file_sha256"), name=f"identity_bindings.{kind}.file_sha256")
        _require_sha256(record.get("identity_sha256"), name=f"identity_bindings.{kind}.identity_sha256")
        byte_count = record.get("bytes")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ExpectedPopulationError(f"identity_bindings.{kind}.bytes must be a non-negative integer")
    config_binding = bindings["config"]
    assert isinstance(config_binding, dict)
    for field in (
        "config_payload_identity_sha256",
        "canonical_config_sha256",
        "registered_scope_sha256",
    ):
        _require_sha256(config_binding.get(field), name=f"identity_bindings.config.{field}")
    component_hash = config_binding.get("component_config_sha256")
    if component_hash is not None:
        _require_sha256(component_hash, name="identity_bindings.config.component_config_sha256")
    map_binding = bindings["instrument_map"]
    assert isinstance(map_binding, dict)
    _require_sha256(map_binding.get("map_sha256"), name="identity_bindings.instrument_map.map_sha256")
    if not isinstance(map_binding.get("version"), str) or not str(map_binding["version"]).strip():
        raise ExpectedPopulationError("identity_bindings.instrument_map.version must be non-blank")
    map_count = map_binding.get("entry_count")
    if isinstance(map_count, bool) or not isinstance(map_count, int) or map_count <= 0:
        raise ExpectedPopulationError("identity_bindings.instrument_map.entry_count must be positive")
    expected_min_hourly_bars = LONG_MIN_HOURLY_BARS if sleeve == "long" else None
    if receipt.get("long_min_hourly_bars") != expected_min_hourly_bars:
        raise ExpectedPopulationError(
            f"expected-population receipt must retain long_min_hourly_bars={expected_min_hourly_bars!r}"
        )

    raw_inputs = receipt.get("raw_inputs")
    expected_raw_schemas = {
        "hourly_keys": HOURLY_KEY_SCHEMA,
        "manifest_keys": MANIFEST_KEY_SCHEMA,
        "manifest_pairs": MANIFEST_PAIR_SCHEMA,
    }
    if not isinstance(raw_inputs, dict) or set(raw_inputs) != set(expected_raw_schemas):
        raise ExpectedPopulationError(
            "receipt raw_inputs must contain exactly hourly_keys, manifest_keys, and manifest_pairs"
        )
    for raw_name, raw_schema in expected_raw_schemas.items():
        _validate_raw_input_record(
            raw_inputs.get(raw_name),
            name=raw_name,
            schema=raw_schema,
        )

    expected_consumer_contract = {
        "verified_population_parameter": "verified_population",
        "runtime_manifest_pairs_parameter": "manifest_pairs",
        "runtime_instrument_map_parameter": "instrument_map",
        "runtime_instrument_map_version_parameter": "instrument_map_version",
        "full_reconstruction_verifier": (
            "liquidity_migration.strategy_overhaul_expected_population.verify_expected_population_artifacts"
        ),
        "s02_consumer_guard": (
            "liquidity_migration.strategy_overhaul_expected_population.verified_expected_population_s02_inputs"
        ),
    }
    if receipt.get("s02_consumer_contract") != expected_consumer_contract:
        raise ExpectedPopulationError(
            "expected-population S02 consumer contract does not match the verified-population API"
        )

    projection = receipt.get("registered_s02_key_projection")
    if not isinstance(projection, dict):
        raise ExpectedPopulationError("registered_s02_key_projection must be an object")
    schema = _registered_s02_key_schema(sleeve)
    key_time = "decision_ts_ms" if sleeve == "continuous" else "signal_ts_ms"
    expected_projection_keys = {
        "format",
        "columns",
        "dtypes",
        "sort_key",
        "row_count",
        "canonical_jsonl_sha256",
        "derivation",
        "entry_confirm_delay_hours",
    }
    if set(projection) != expected_projection_keys:
        raise ExpectedPopulationError("registered_s02_key_projection has an invalid schema")
    if (
        projection.get("format") != EXPECTED_POPULATION_FORMAT
        or projection.get("columns") != list(schema)
        or projection.get("dtypes") != [str(dtype) for dtype in schema.values()]
        or projection.get("sort_key") != ["venue", "symbol", key_time]
    ):
        raise ExpectedPopulationError("registered_s02_key_projection format/schema/sort key mismatch")
    row_count = projection.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise ExpectedPopulationError("registered_s02_key_projection.row_count must be non-negative")
    _require_sha256(
        projection.get("canonical_jsonl_sha256"),
        name="registered_s02_key_projection.canonical_jsonl_sha256",
    )
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"source_keys", "expected_population"}:
        raise ExpectedPopulationError("receipt artifacts must contain exactly source_keys and expected_population")
    _validate_artifact_receipt_record(
        artifacts.get("source_keys"),
        sleeve=sleeve,
        artifact_kind="source_keys",
    )
    expected_record = _validate_artifact_receipt_record(
        artifacts.get("expected_population"),
        sleeve=sleeve,
        artifact_kind="expected_population",
    )
    if expected_record.get("row_count") != row_count:
        raise ExpectedPopulationError("registered S02 key row count must equal expected_population artifact row count")
    if sleeve == "continuous":
        delay = projection.get("entry_confirm_delay_hours")
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise ExpectedPopulationError("continuous registered key delay must be a non-negative integer")
    elif projection.get("entry_confirm_delay_hours") is not None:
        raise ExpectedPopulationError("LONG registered key projection must not declare an entry-confirm delay")
    for field, expected in (
        ("exact_supplied_keys_and_ages_verified", True),
        ("root_receipt_bytes_verified", True),
        ("root_completeness_proven", False),
        ("root_authenticity_proven", False),
        ("pit_receipt_bytes_verified", True),
        ("pit_projection_exactly_hashed", True),
        ("pit_provenance_authenticated", False),
        ("instrument_map_content_identity_verified", True),
        ("instrument_map_expected_row_coverage_verified", True),
        ("outcome_values_read", False),
        ("numeric_kline_values_read", False),
        ("outcome_run_authorized", False),
        ("real_money_authorized", False),
    ):
        if receipt.get(field) is not expected:
            raise ExpectedPopulationError(f"expected-population receipt must retain {field}={expected!r}")


def verify_expected_population_receipt_identity(
    source: str | Path | bytes | Mapping[str, JsonValue],
) -> ExpectedPopulationReceiptIdentity:
    """Verify a canonical population receipt and expose stage-link identities.

    This receipt-only verifier is intentionally distinct from
    :func:`verify_expected_population_artifacts`: it validates the receipt's
    top-level schema/self-hash plus the identity, raw-input, artifact, consumer,
    and registered-S02 projection records it returns.  Informational nested
    sections such as ``window``, ``config_parity``, builder details,
    ``config_exclusions``, and ``limitations`` remain self-hash-bound but are
    not independently interpreted here.  Population reconstruction requires
    the sibling JSONL artifacts and raw key inputs.
    """

    file_sha256: str | None = None
    if isinstance(source, (str, Path)):
        data = _regular_file_bytes(Path(source), name="expected-population receipt")
        file_sha256 = hashlib.sha256(data).hexdigest()
        if not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise ExpectedPopulationError("expected-population receipt must be one newline-terminated JSON object")
        payload = _strict_json_object(data[:-1], name="expected-population receipt")
        if canonical_json_bytes(payload) + b"\n" != data:
            raise ExpectedPopulationError("expected-population receipt is not canonical JSON")
    elif isinstance(source, bytes):
        data = source
        file_sha256 = hashlib.sha256(data).hexdigest()
        if not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise ExpectedPopulationError("expected-population receipt must be one newline-terminated JSON object")
        payload = _strict_json_object(data[:-1], name="expected-population receipt")
        if canonical_json_bytes(payload) + b"\n" != data:
            raise ExpectedPopulationError("expected-population receipt is not canonical JSON")
    elif isinstance(source, Mapping):
        payload = dict(source)
    else:  # pragma: no cover - runtime type guard
        raise TypeError("population receipt source must be a path, bytes, or mapping")
    sleeve = payload.get("sleeve")
    venue = payload.get("venue")
    if sleeve not in _SLEEVES or venue not in _VENUES:
        raise ExpectedPopulationError("expected-population receipt has an invalid sleeve or venue")
    typed_sleeve = cast(Sleeve, sleeve)
    _validate_receipt_shape(payload, sleeve=typed_sleeve, venue=str(venue))
    projection = payload["registered_s02_key_projection"]
    bindings = payload["identity_bindings"]
    artifact_records = payload["artifacts"]
    raw_inputs = payload["raw_inputs"]
    assert (
        isinstance(projection, dict)
        and isinstance(bindings, dict)
        and isinstance(artifact_records, dict)
        and isinstance(raw_inputs, dict)
    )
    source_record = artifact_records["source_keys"]
    expected_record = artifact_records["expected_population"]
    manifest_pair_record = raw_inputs["manifest_pairs"]
    map_record = bindings["instrument_map"]
    assert (
        isinstance(source_record, dict)
        and isinstance(expected_record, dict)
        and isinstance(manifest_pair_record, dict)
        and isinstance(map_record, dict)
    )
    return ExpectedPopulationReceiptIdentity(
        sleeve=typed_sleeve,
        venue=str(venue),
        registered_s02_key_columns=tuple(cast(list[str], projection["columns"])),
        registered_s02_key_sha256=str(projection["canonical_jsonl_sha256"]),
        registered_s02_key_row_count=int(projection["row_count"]),
        source_keys_file_sha256=str(source_record["file_sha256"]),
        source_keys_row_count=int(source_record["row_count"]),
        expected_population_file_sha256=str(expected_record["file_sha256"]),
        expected_population_row_count=int(expected_record["row_count"]),
        manifest_pairs_canonical_jsonl_sha256=str(manifest_pair_record["canonical_jsonl_sha256"]),
        manifest_pairs_row_count=int(manifest_pair_record["row_count"]),
        instrument_map_sha256=str(map_record["map_sha256"]),
        instrument_map_version=str(map_record["version"]),
        identity_bindings=MappingProxyType(
            {
                kind: MappingProxyType(cast(dict[str, JsonValue], record))
                for kind, record in bindings.items()
                if isinstance(record, dict)
            }
        ),
        receipt_artifact_sha256=str(payload["artifact_sha256"]),
        receipt_identity_sha256=canonical_json_sha256(payload),
        receipt_file_sha256=file_sha256,
    )


def verify_expected_population_artifacts(
    artifacts: ExpectedPopulationArtifacts,
    hourly_keys: pl.DataFrame,
    manifest_keys: pl.DataFrame,
    manifest_pairs: pl.DataFrame,
    *,
    config: LongNativeConfig | ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
    config_identity_receipt: BoundIdentityReceipt,
    root_identity_receipt: BoundIdentityReceipt,
    pit_identity_receipt: BoundIdentityReceipt,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
    instrument_map_identity_receipt: BoundIdentityReceipt,
) -> VerifiedExpectedPopulation:
    """Reconstruct and verify every artifact, identity, key, age, and byte."""

    _validate_receipt_shape(artifacts.receipt, sleeve=artifacts.sleeve, venue=artifacts.venue)
    rebuilt = _derive_expected_population(
        hourly_keys,
        manifest_keys,
        manifest_pairs,
        sleeve=artifacts.sleeve,
        venue=artifacts.venue,
        config=config,
        config_identity=config_identity,
        config_identity_receipt=config_identity_receipt,
        root_identity_receipt=root_identity_receipt,
        pit_identity_receipt=pit_identity_receipt,
        instrument_map=instrument_map,
        instrument_map_version=instrument_map_version,
        instrument_map_identity_receipt=instrument_map_identity_receipt,
    )
    if artifacts.source_keys_jsonl != rebuilt.source_keys_jsonl:
        raise ExpectedPopulationError("source_keys bytes do not equal the reconstructed canonical population")
    if artifacts.expected_population_jsonl != rebuilt.expected_population_jsonl:
        raise ExpectedPopulationError("expected_population bytes do not equal the reconstructed canonical population")
    if dict(artifacts.receipt) != dict(rebuilt.receipt):
        raise ExpectedPopulationError("expected-population receipt does not equal the reconstructed receipt")
    source = parse_expected_population_jsonl(
        artifacts.source_keys_jsonl,
        sleeve=artifacts.sleeve,
        artifact_kind="source_keys",
    )
    expected = parse_expected_population_jsonl(
        artifacts.expected_population_jsonl,
        sleeve=artifacts.sleeve,
        artifact_kind="expected_population",
    )
    verified = object.__new__(VerifiedExpectedPopulation)
    object.__setattr__(verified, "sleeve", artifacts.sleeve)
    object.__setattr__(verified, "venue", artifacts.venue)
    object.__setattr__(verified, "source_keys", source)
    object.__setattr__(verified, "expected_population", expected)
    object.__setattr__(verified, "receipt_sha256", str(artifacts.receipt["artifact_sha256"]))
    receipt_identity = verify_expected_population_receipt_identity(artifacts.receipt)
    object.__setattr__(verified, "receipt_identity", receipt_identity)
    _FULLY_RECONSTRUCTED_POPULATIONS[verified] = _VerifiedExpectedPopulationAttestation(
        sleeve=artifacts.sleeve,
        venue=artifacts.venue,
        source_keys_file_sha256=hashlib.sha256(artifacts.source_keys_jsonl).hexdigest(),
        expected_population_file_sha256=hashlib.sha256(artifacts.expected_population_jsonl).hexdigest(),
        receipt_sha256=str(artifacts.receipt["artifact_sha256"]),
        receipt_identity_attestation_sha256=canonical_json_sha256(
            _receipt_identity_attestation_payload(receipt_identity)
        ),
    )
    return verified


def verified_expected_population_s02_inputs(
    verified: VerifiedExpectedPopulation,
    *,
    sleeve: Sleeve,
    venue: str,
    config: LongNativeConfig | ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
    manifest_pairs: pl.DataFrame,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return the exact source/retained frames after rechecking the S02 binding.

    The full reconstructing verifier owns root/PIT/map reconstruction.  This
    narrower consumer guard prevents a verified object from being replayed with
    another sleeve, venue, config, scope, or mutated in-memory key frame before
    an S02 builder consumes it.
    """

    if not isinstance(verified, VerifiedExpectedPopulation):
        raise ExpectedPopulationError("S02 requires a VerifiedExpectedPopulation")
    attestation = _FULLY_RECONSTRUCTED_POPULATIONS.get(verified)
    if attestation is None:
        raise ExpectedPopulationError("S02 requires a VerifiedExpectedPopulation produced by full reconstruction")
    if sleeve not in _SLEEVES or venue not in _VENUES:
        raise ExpectedPopulationError("S02 expected-population sleeve/venue is invalid")
    if (
        verified.sleeve != sleeve
        or verified.venue != venue
        or verified.receipt_identity.sleeve != sleeve
        or verified.receipt_identity.venue != venue
    ):
        raise ExpectedPopulationError("verified expected population belongs to another sleeve or venue")
    if (
        attestation.sleeve != sleeve
        or attestation.venue != venue
        or verified.receipt_sha256 != attestation.receipt_sha256
        or canonical_json_sha256(_receipt_identity_attestation_payload(verified.receipt_identity))
        != attestation.receipt_identity_attestation_sha256
    ):
        raise ExpectedPopulationError("verified expected-population proof object mutated after full reconstruction")
    _config_for_sleeve(sleeve, config, config_identity)
    config_binding = verified.receipt_identity.identity_bindings.get("config")
    if not isinstance(config_binding, Mapping):
        raise ExpectedPopulationError("verified expected population lacks its config identity binding")
    expected_config_binding = {
        "identity_sha256": canonical_json_sha256(config_identity),
        "config_payload_identity_sha256": config_identity["identity_sha256"],
        "canonical_config_sha256": config_identity["canonical_config_sha256"],
        "registered_scope_sha256": config_identity["scope_sha256"],
        "component_config_sha256": config_identity.get("component_config_sha256"),
    }
    mismatches = {
        name: {"expected": expected, "observed": config_binding.get(name)}
        for name, expected in expected_config_binding.items()
        if config_binding.get(name) != expected
    }
    if mismatches:
        raise ExpectedPopulationError(f"verified expected-population config binding drifted before S02: {mismatches}")

    source = _validate_key_frame(
        verified.source_keys,
        sleeve=sleeve,
        artifact_kind="source_keys",
    )
    expected = _validate_key_frame(
        verified.expected_population,
        sleeve=sleeve,
        artifact_kind="expected_population",
    )
    manifest_pair_rows, manifest_pair_sha256 = canonical_manifest_pair_identity(
        manifest_pairs,
        venue=venue,
    )
    runtime_map_sha256 = canonical_json_sha256([dataclasses.asdict(entry) for entry in instrument_map])
    source_jsonl = render_expected_population_jsonl(
        source,
        sleeve=sleeve,
        artifact_kind="source_keys",
    )
    expected_jsonl = render_expected_population_jsonl(
        expected,
        sleeve=sleeve,
        artifact_kind="expected_population",
    )
    identity = verified.receipt_identity
    if (
        source.height != identity.source_keys_row_count
        or hashlib.sha256(source_jsonl).hexdigest() != identity.source_keys_file_sha256
        or hashlib.sha256(source_jsonl).hexdigest() != attestation.source_keys_file_sha256
        or expected.height != identity.expected_population_row_count
        or hashlib.sha256(expected_jsonl).hexdigest() != identity.expected_population_file_sha256
        or hashlib.sha256(expected_jsonl).hexdigest() != attestation.expected_population_file_sha256
    ):
        raise ExpectedPopulationError(
            "verified expected-population source/retained artifact identity drifted before consumption"
        )
    if (
        manifest_pair_rows != identity.manifest_pairs_row_count
        or manifest_pair_sha256 != identity.manifest_pairs_canonical_jsonl_sha256
        or runtime_map_sha256 != identity.instrument_map_sha256
        or instrument_map_version != identity.instrument_map_version
    ):
        raise ExpectedPopulationError(
            "verified expected-population runtime PIT/map identity drifted before consumption"
        )
    registered = _registered_s02_keys(
        expected,
        sleeve=sleeve,
        venue=venue,
        config=config,
    )
    observed_key_hash = registered_s02_key_sha256(registered, sleeve=sleeve)
    expected_columns = tuple(_registered_s02_key_schema(sleeve))
    if (
        identity.registered_s02_key_columns != expected_columns
        or identity.registered_s02_key_row_count != expected.height
        or identity.registered_s02_key_row_count != identity.expected_population_row_count
        or identity.registered_s02_key_sha256 != observed_key_hash
        or verified.receipt_sha256 != identity.receipt_artifact_sha256
    ):
        raise ExpectedPopulationError(
            "verified expected-population registered S02 key identity drifted before consumption"
        )
    expected_outside_source = expected.select("symbol", "signal_ts_ms").join(
        source.select("symbol", "signal_ts_ms"),
        on=["symbol", "signal_ts_ms"],
        how="anti",
    )
    if not expected_outside_source.is_empty():
        raise ExpectedPopulationError("verified expected population is not a subset of its source keys")
    return source, expected


def render_expected_population_receipt(receipt: Mapping[str, JsonValue]) -> bytes:
    sleeve = receipt.get("sleeve")
    venue = receipt.get("venue")
    if sleeve not in _SLEEVES or venue not in _VENUES:
        raise ExpectedPopulationError("receipt has an invalid sleeve or venue")
    _validate_receipt_shape(receipt, sleeve=cast(Sleeve, sleeve), venue=str(venue))
    return canonical_json_bytes(dict(receipt)) + b"\n"


def _existing_identical(path: Path, expected: bytes, *, name: str) -> bool:
    try:
        actual = _regular_file_bytes(path, name=f"existing {name}")
    except ExpectedPopulationError:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        raise
    if actual != expected:
        raise ExpectedPopulationError(f"refusing to overwrite non-identical {name}: {path}")
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


def _create_immutable(path: Path, data: bytes, *, name: str) -> bool:
    if _existing_identical(path, data, name=name):
        return True
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            linked = True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise ExpectedPopulationError(f"failed to atomically create {name}: {path}") from exc
            _existing_identical(path, data, name=name)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return not linked


def write_expected_population_artifacts(
    directory: str | Path,
    artifacts: ExpectedPopulationArtifacts,
) -> ExpectedPopulationWriteResult:
    """Atomically create each artifact; only byte-identical files are reusable."""

    _validate_receipt_shape(artifacts.receipt, sleeve=artifacts.sleeve, venue=artifacts.venue)
    source = parse_expected_population_jsonl(
        artifacts.source_keys_jsonl,
        sleeve=artifacts.sleeve,
        artifact_kind="source_keys",
    )
    expected = parse_expected_population_jsonl(
        artifacts.expected_population_jsonl,
        sleeve=artifacts.sleeve,
        artifact_kind="expected_population",
    )
    artifact_records = artifacts.receipt.get("artifacts")
    if not isinstance(artifact_records, dict):
        raise ExpectedPopulationError("receipt artifacts must be an object")
    for name, frame, data in (
        ("source_keys", source, artifacts.source_keys_jsonl),
        ("expected_population", expected, artifacts.expected_population_jsonl),
    ):
        record = artifact_records.get(name)
        if not isinstance(record, dict):
            raise ExpectedPopulationError(f"receipt is missing the {name} record")
        expected_record = _artifact_record(
            data,
            frame,
            sleeve=artifacts.sleeve,
            artifact_kind=cast(ArtifactKind, name),
            logical_path=SOURCE_KEYS_FILENAME if name == "source_keys" else EXPECTED_POPULATION_FILENAME,
        )
        if record != expected_record:
            raise ExpectedPopulationError(f"receipt {name} record does not match its canonical bytes")

    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    source_path = destination / SOURCE_KEYS_FILENAME
    expected_path = destination / EXPECTED_POPULATION_FILENAME
    receipt_path = destination / EXPECTED_POPULATION_RECEIPT_FILENAME
    receipt_bytes = render_expected_population_receipt(artifacts.receipt)
    reused = [
        _create_immutable(source_path, artifacts.source_keys_jsonl, name="source_keys artifact"),
        _create_immutable(expected_path, artifacts.expected_population_jsonl, name="expected_population artifact"),
        _create_immutable(receipt_path, receipt_bytes, name="expected-population receipt"),
    ]
    return ExpectedPopulationWriteResult(
        directory=destination,
        source_keys_path=source_path,
        expected_population_path=expected_path,
        receipt_path=receipt_path,
        receipt_file_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        reused=all(reused),
    )


def load_expected_population_artifacts(directory: str | Path) -> ExpectedPopulationArtifacts:
    """Load strict canonical files; semantic verification remains a separate call."""

    source_root = Path(directory)
    receipt_bytes = _regular_file_bytes(
        source_root / EXPECTED_POPULATION_RECEIPT_FILENAME,
        name="expected-population receipt",
    )
    if not receipt_bytes.endswith(b"\n") or receipt_bytes.count(b"\n") != 1:
        raise ExpectedPopulationError("expected-population receipt must be one newline-terminated JSON object")
    receipt = _strict_json_object(receipt_bytes[:-1], name="expected-population receipt")
    if canonical_json_bytes(receipt) + b"\n" != receipt_bytes:
        raise ExpectedPopulationError("expected-population receipt is not canonical JSON")
    sleeve = receipt.get("sleeve")
    venue = receipt.get("venue")
    if sleeve not in _SLEEVES or venue not in _VENUES:
        raise ExpectedPopulationError("expected-population receipt has an invalid sleeve or venue")
    typed_sleeve = cast(Sleeve, sleeve)
    _validate_receipt_shape(receipt, sleeve=typed_sleeve, venue=str(venue))
    source_bytes = _regular_file_bytes(source_root / SOURCE_KEYS_FILENAME, name="source_keys artifact")
    expected_bytes = _regular_file_bytes(
        source_root / EXPECTED_POPULATION_FILENAME,
        name="expected_population artifact",
    )
    source_frame = parse_expected_population_jsonl(
        source_bytes,
        sleeve=typed_sleeve,
        artifact_kind="source_keys",
    )
    expected_frame = parse_expected_population_jsonl(
        expected_bytes,
        sleeve=typed_sleeve,
        artifact_kind="expected_population",
    )
    records = receipt.get("artifacts")
    if not isinstance(records, dict):
        raise ExpectedPopulationError("expected-population receipt artifacts must be an object")
    expected_records = {
        "source_keys": _artifact_record(
            source_bytes,
            source_frame,
            sleeve=typed_sleeve,
            artifact_kind="source_keys",
            logical_path=SOURCE_KEYS_FILENAME,
        ),
        "expected_population": _artifact_record(
            expected_bytes,
            expected_frame,
            sleeve=typed_sleeve,
            artifact_kind="expected_population",
            logical_path=EXPECTED_POPULATION_FILENAME,
        ),
    }
    if records != expected_records:
        raise ExpectedPopulationError("loaded population files do not match receipt artifact records")
    return ExpectedPopulationArtifacts(
        sleeve=typed_sleeve,
        venue=str(venue),
        source_keys_jsonl=source_bytes,
        expected_population_jsonl=expected_bytes,
        receipt=MappingProxyType(receipt),
    )


__all__ = [
    "EXPECTED_POPULATION_FILENAME",
    "EXPECTED_POPULATION_FORMAT",
    "EXPECTED_POPULATION_RECEIPT_FILENAME",
    "EXPECTED_POPULATION_RECEIPT_TYPE",
    "EXPECTED_POPULATION_SCHEMA_VERSION",
    "CONTINUOUS_REGISTERED_S02_KEY_SCHEMA",
    "LONG_EXPECTED_POPULATION_SCHEMA",
    "LONG_MIN_HOURLY_BARS",
    "LONG_REGISTERED_S02_KEY_SCHEMA",
    "MANIFEST_PAIR_SCHEMA",
    "SOURCE_KEYS_FILENAME",
    "BoundIdentityReceipt",
    "ExpectedPopulationArtifacts",
    "ExpectedPopulationError",
    "ExpectedPopulationReceiptIdentity",
    "ExpectedPopulationWriteResult",
    "VerifiedExpectedPopulation",
    "build_expected_population_artifacts",
    "canonical_manifest_pair_identity",
    "continuous_population_exclusions_parity_surface",
    "continuous_expected_population_consumer_parity_surface",
    "load_expected_population_artifacts",
    "long_population_and_rolling_windows_parity_surface",
    "long_expected_population_consumer_parity_surface",
    "parse_expected_population_jsonl",
    "render_expected_population_jsonl",
    "render_expected_population_receipt",
    "render_registered_s02_key_jsonl",
    "registered_s02_key_sha256",
    "verify_expected_population_artifacts",
    "verify_expected_population_receipt_identity",
    "verified_expected_population_s02_inputs",
    "write_expected_population_artifacts",
]
