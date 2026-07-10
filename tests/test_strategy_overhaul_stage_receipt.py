"""Focused tests for diagnostic strategy-overhaul stage byte bindings."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import polars as pl
import pytest

import liquidity_migration.strategy_overhaul_stage_receipt as stage_receipt_module
from liquidity_migration.strategy_overhaul_config_identity import (
    derive_continuous_a0_config_identity,
    derive_long_a0_config_identity,
)
from liquidity_migration.strategy_overhaul_expected_population import (
    CONTINUOUS_REGISTERED_S02_KEY_SCHEMA,
    EXPECTED_POPULATION_FILENAME,
    EXPECTED_POPULATION_FORMAT,
    EXPECTED_POPULATION_RECEIPT_FILENAME,
    EXPECTED_POPULATION_RECEIPT_TYPE,
    EXPECTED_POPULATION_SCHEMA_VERSION,
    LONG_EXPECTED_POPULATION_SCHEMA,
    LONG_REGISTERED_S02_KEY_SCHEMA,
    MANIFEST_PAIR_SCHEMA,
    SOURCE_KEYS_FILENAME,
    registered_s02_key_sha256,
)
from liquidity_migration.strategy_overhaul_population_keys import (
    CONTINUOUS_KEY_SCHEMA,
    HOURLY_KEY_SCHEMA,
    LONG_KEY_SCHEMA,
    MANIFEST_KEY_SCHEMA,
)
from liquidity_migration.strategy_overhaul_projection import artifact_polars_schema, empty_artifact_frame
from liquidity_migration.strategy_overhaul_schemas import ARTIFACT_SCHEMAS
from liquidity_migration.strategy_overhaul_stage_receipt import (
    ArtifactInput,
    BoundFileInput,
    CONFIG_IDENTITY_VERIFICATION_STATUS,
    IDENTITY_RECEIPT_KINDS,
    OPAQUE_IDENTITY_VERIFICATION_STATUS,
    RECEIPT_SCOPE,
    STAGE_IDENTITY_RECEIPT_KINDS,
    STAGE_RECEIPT_TYPE,
    StageReceiptError,
    UNVERIFIED_ARTIFACT_DECLARATIONS,
    build_stage_receipt,
    canonical_json_bytes,
    canonical_stage_key_projection_sha256,
    load_stage_semantic_receipt,
    load_stage_receipt,
    registered_stage_schema,
    render_stage_semantic_receipt,
    render_stage_receipt,
    verify_stage_receipt_semantics,
    verify_stage_receipt_byte_bindings,
    write_stage_semantic_receipt,
    write_stage_receipt,
)


CONT_SIGNAL_TS_MS = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
CONT_DECISION_TS_MS = CONT_SIGNAL_TS_MS + 60 * 60 * 1000
LONG_SIGNAL_TS_MS = int(dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _config_identity(*, sleeve: str = "continuous") -> dict:
    return derive_continuous_a0_config_identity() if sleeve == "continuous" else derive_long_a0_config_identity()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _identity_inputs(
    root: Path,
    kinds: tuple[str, ...] = IDENTITY_RECEIPT_KINDS,
    *,
    sleeve: str = "continuous",
) -> dict[str, BoundFileInput]:
    config_identity = _config_identity(sleeve=sleeve)
    result: dict[str, BoundFileInput] = {}
    for kind in kinds:
        logical = f"identities/{kind}.json"
        payload = (
            config_identity
            if kind == "config"
            else {
                "artifact_type": f"test_{kind}_receipt",
                "artifact_sha256": _sha(kind),
                "outcome_values_read": False,
            }
        )
        path = _write(root / logical, canonical_json_bytes(payload) + b"\n")
        result[kind] = BoundFileInput(logical_path=logical, path=path)
    return result


def _rehash_config_identity(payload: dict) -> dict:
    payload["canonical_config_sha256"] = stage_receipt_module.canonical_json_sha256(payload["canonical_config"])
    payload["scope_sha256"] = stage_receipt_module.canonical_json_sha256(payload["scope"])
    component = payload["component_config"]
    payload["component_config_sha256"] = (
        stage_receipt_module.canonical_json_sha256(component) if component is not None else None
    )
    unhashed = dict(payload)
    unhashed.pop("identity_sha256", None)
    payload["identity_sha256"] = stage_receipt_module.canonical_json_sha256(unhashed)
    return payload


def _artifact(root: Path, stage: str, *, suffix: str = "", row_count: int | None = None) -> ArtifactInput:
    logical = f"artifacts/{stage.lower()}{suffix}.bin"
    data = f"{stage}-artifact{suffix}".encode("utf-8")
    path = _write(root / logical, data)
    return ArtifactInput(
        logical_path=logical,
        path=path,
        declared_row_count=(int(stage[1:]) if row_count is None else row_count),
        declared_key_projection_sha256=hashlib.sha256(f"{stage}-keys{suffix}".encode()).hexdigest(),
    )


def _receipt_source(root: Path, stage: str, *, suffix: str = "") -> BoundFileInput:
    logical = f"receipts/{stage.lower()}{suffix}.json"
    return BoundFileInput(logical_path=logical, path=root / logical)


def _build(
    root: Path,
    identities: dict[str, BoundFileInput],
    stage: str,
    *,
    sleeve: str = "continuous",
    venue: str = "bybit",
    parents: tuple[BoundFileInput, ...] = (),
    artifact: ArtifactInput | None = None,
    schema_override=None,
    outcome_blind: bool | None = None,
):
    config_identity = _config_identity(sleeve=sleeve)
    expected_outcome_blind = stage in {"S00", "S01", "S02"}
    schema = registered_stage_schema(sleeve, stage) if stage in {"S02", "S03", "S04"} else None
    if schema_override is not None:
        schema = schema_override
    return build_stage_receipt(
        sleeve=sleeve,
        venue=venue,
        stage=stage,
        declared_outcome_blind=expected_outcome_blind if outcome_blind is None else outcome_blind,
        canonical_config_identity_sha256=str(config_identity["identity_sha256"]),
        registered_scope_sha256=str(config_identity["scope_sha256"]),
        identity_receipts={kind: identities[kind] for kind in STAGE_IDENTITY_RECEIPT_KINDS[stage]},
        artifact=artifact or _artifact(root, stage),
        parents=parents,
        declared_artifact_schema_identity=schema,
        binding_root=root,
    )


def _build_and_write(
    root: Path,
    identities: dict[str, BoundFileInput],
    stage: str,
    *,
    sleeve: str = "continuous",
    parents: tuple[BoundFileInput, ...] = (),
    suffix: str = "",
    artifact: ArtifactInput | None = None,
):
    payload = _build(
        root,
        identities,
        stage,
        sleeve=sleeve,
        parents=parents,
        artifact=artifact or _artifact(root, stage, suffix=suffix),
    )
    target = _receipt_source(root, stage, suffix=suffix)
    result = write_stage_receipt(target.path, payload)
    assert result.reused is False
    return payload, target


def _chain(root: Path):
    identities = _identity_inputs(root)
    s00, s00_source = _build_and_write(root, identities, "S00")
    s01, s01_source = _build_and_write(root, identities, "S01", parents=(s00_source,))
    s02, s02_source = _build_and_write(root, identities, "S02", parents=(s01_source,))
    s03, s03_source = _build_and_write(root, identities, "S03", parents=(s02_source,))
    s04, s04_source = _build_and_write(
        root,
        identities,
        "S04",
        parents=(s02_source, s03_source),
    )
    return identities, {
        "S00": (s00, s00_source),
        "S01": (s01, s01_source),
        "S02": (s02, s02_source),
        "S03": (s03, s03_source),
        "S04": (s04, s04_source),
    }


def _identity_binding(source: BoundFileInput) -> dict[str, object]:
    data = source.path.read_bytes()
    payload = json.loads(data)
    return {
        "logical_path": source.logical_path,
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "identity_sha256": stage_receipt_module.canonical_json_sha256(payload),
        "artifact_type": payload.get("artifact_type"),
        "declared_artifact_sha256": payload.get("artifact_sha256"),
    }


def _jsonl(frame: pl.DataFrame) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in frame.iter_rows(named=True))


def _population_artifact_record(
    frame: pl.DataFrame,
    data: bytes,
    *,
    logical_path: str,
) -> dict[str, object]:
    return {
        "logical_path": logical_path,
        "format": EXPECTED_POPULATION_FORMAT,
        "columns": frame.columns,
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "sort_key": ["symbol", "signal_ts_ms"],
        "row_count": frame.height,
        "bytes": len(data),
        "file_sha256": hashlib.sha256(data).hexdigest(),
    }


def _raw_input_record(schema: Mapping[str, pl.DataType]) -> dict[str, object]:
    return {
        "columns": list(schema),
        "dtypes": [str(dtype) for dtype in schema.values()],
        "row_count": 0,
        "canonical_jsonl_sha256": hashlib.sha256(b"").hexdigest(),
    }


def _install_population_identity(
    root: Path,
    identities: dict[str, BoundFileInput],
    *,
    sleeve: str,
    venue: str,
    expected: pl.DataFrame | None = None,
    source: pl.DataFrame | None = None,
    binding_mutation=None,
) -> None:
    if sleeve == "continuous":
        expected_schema = dict(CONTINUOUS_KEY_SCHEMA)
        source_schema = dict(CONTINUOUS_KEY_SCHEMA)
    else:
        expected_schema = dict(LONG_EXPECTED_POPULATION_SCHEMA)
        source_schema = dict(LONG_KEY_SCHEMA)
    expected_frame = expected if expected is not None else pl.DataFrame(schema=expected_schema)
    source_frame = source
    if source_frame is None:
        source_frame = (
            expected_frame
            if sleeve == "continuous"
            else expected_frame.with_columns(pl.lit(24, dtype=pl.UInt32).alias("hourly_bar_count")).select(
                list(source_schema)
            )
        )
    source_data = _jsonl(source_frame)
    expected_data = _jsonl(expected_frame)
    directory = root / "population"
    _write(directory / SOURCE_KEYS_FILENAME, source_data)
    _write(directory / EXPECTED_POPULATION_FILENAME, expected_data)
    config_identity = _config_identity(sleeve=sleeve)
    config_binding = {
        **_identity_binding(identities["config"]),
        "config_payload_identity_sha256": config_identity["identity_sha256"],
        "canonical_config_sha256": config_identity["canonical_config_sha256"],
        "registered_scope_sha256": config_identity["scope_sha256"],
        "component_config_sha256": config_identity.get("component_config_sha256"),
    }
    map_binding = {
        **_identity_binding(identities["instrument_map"]),
        "version": "synthetic-map-v1",
        "map_sha256": _sha("synthetic-map"),
        "entry_count": 1,
    }
    identity_bindings = {
        "config": config_binding,
        "root": _identity_binding(identities["root"]),
        "pit": _identity_binding(identities["pit"]),
        "instrument_map": map_binding,
    }
    if binding_mutation is not None:
        binding_mutation(identity_bindings)
    if sleeve == "continuous":
        registered_schema = CONTINUOUS_REGISTERED_S02_KEY_SCHEMA
        registered_keys = expected_frame.select(
            pl.lit(venue, dtype=pl.String).alias("venue"),
            pl.col("symbol"),
            (pl.col("signal_ts_ms") + 60 * 60 * 1000).cast(pl.Int64).alias("decision_ts_ms"),
        ).sort(["venue", "symbol", "decision_ts_ms"])
        key_time = "decision_ts_ms"
        derivation = "venue_constant_plus_signal_ts_ms_plus_entry_confirm_delay_hours"
        delay = 1
    else:
        registered_schema = LONG_REGISTERED_S02_KEY_SCHEMA
        registered_keys = expected_frame.select(
            pl.lit(venue, dtype=pl.String).alias("venue"),
            pl.col("symbol"),
            pl.col("signal_ts_ms"),
        ).sort(["venue", "symbol", "signal_ts_ms"])
        key_time = "signal_ts_ms"
        derivation = "venue_constant_plus_expected_population_signal_ts_ms"
        delay = None
    registered_key_hash = canonical_stage_key_projection_sha256(
        registered_keys,
        "continuous_a0_signal_features" if sleeve == "continuous" else "long_a0_signal_features",
    )
    receipt = {
        "schema_version": EXPECTED_POPULATION_SCHEMA_VERSION,
        "artifact_type": EXPECTED_POPULATION_RECEIPT_TYPE,
        "sleeve": sleeve,
        "venue": venue,
        "window": {},
        "config_parity": {},
        "identity_bindings": identity_bindings,
        "raw_inputs": {
            "hourly_keys": _raw_input_record(HOURLY_KEY_SCHEMA),
            "manifest_keys": _raw_input_record(MANIFEST_KEY_SCHEMA),
            "manifest_pairs": _raw_input_record(MANIFEST_PAIR_SCHEMA),
        },
        "population_builder_receipt": {},
        "long_min_hourly_bars": 20 if sleeve == "long" else None,
        "config_exclusions": {},
        "artifacts": {
            "source_keys": _population_artifact_record(
                source_frame,
                source_data,
                logical_path=SOURCE_KEYS_FILENAME,
            ),
            "expected_population": _population_artifact_record(
                expected_frame,
                expected_data,
                logical_path=EXPECTED_POPULATION_FILENAME,
            ),
        },
        "registered_s02_key_projection": {
            "format": EXPECTED_POPULATION_FORMAT,
            "columns": list(registered_schema),
            "dtypes": [str(dtype) for dtype in registered_schema.values()],
            "sort_key": ["venue", "symbol", key_time],
            "row_count": registered_keys.height,
            "canonical_jsonl_sha256": registered_key_hash,
            "derivation": derivation,
            "entry_confirm_delay_hours": delay,
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
        "limitations": [],
    }
    receipt["artifact_sha256"] = stage_receipt_module.canonical_json_sha256(receipt)
    logical = f"population/{EXPECTED_POPULATION_RECEIPT_FILENAME}"
    receipt_path = _write(root / logical, canonical_json_bytes(receipt) + b"\n")
    identities["population"] = BoundFileInput(logical_path=logical, path=receipt_path)


def _empty_stage_artifact(
    root: Path,
    *,
    sleeve: str,
    stage: str,
    suffix: str,
    frame: pl.DataFrame | None = None,
    declared_row_count: int | None = None,
    declared_key_hash: str | None = None,
) -> ArtifactInput:
    schema = ARTIFACT_SCHEMAS[registered_stage_schema(sleeve, stage).schema_id]
    artifact_frame = frame if frame is not None else empty_artifact_frame(schema.schema_id)
    logical = f"artifacts/{sleeve}-{stage.lower()}{suffix}"
    path = root / logical
    path.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".parquet":
        artifact_frame.write_parquet(path)
    else:
        artifact_frame.write_ipc(path)
    return ArtifactInput(
        logical_path=logical,
        path=path,
        declared_row_count=artifact_frame.height if declared_row_count is None else declared_row_count,
        declared_key_projection_sha256=(
            canonical_stage_key_projection_sha256(artifact_frame, schema.schema_id)
            if declared_key_hash is None
            else declared_key_hash
        ),
    )


def _semantic_chain(
    root: Path,
    *,
    sleeve: str = "continuous",
    suffix: str = ".parquet",
    frames: dict[str, pl.DataFrame] | None = None,
    population_expected: pl.DataFrame | None = None,
    population_source: pl.DataFrame | None = None,
    artifact_kwargs_by_stage: dict[str, dict[str, object]] | None = None,
    leaf_stage: str = "S04",
    population_binding_mutation=None,
):
    identities = _identity_inputs(root, sleeve=sleeve)
    expected = population_expected
    if expected is None and frames and "S02" in frames and not frames["S02"].is_empty():
        s02 = frames["S02"]
        if sleeve == "continuous":
            expected = s02.select("symbol", "signal_ts_ms")
        else:
            expected = s02.select("symbol", "signal_ts_ms", "symbol_age_days")
    _install_population_identity(
        root,
        identities,
        sleeve=sleeve,
        venue="bybit",
        expected=expected,
        source=population_source,
        binding_mutation=population_binding_mutation,
    )
    _s00, s00_source = _build_and_write(root, identities, "S00", sleeve=sleeve)
    _s01, s01_source = _build_and_write(
        root,
        identities,
        "S01",
        sleeve=sleeve,
        parents=(s00_source,),
    )
    sources = {"S01": s01_source}
    receipts = {}
    for stage in ("S02", "S03", "S04"):
        if STAGES_INDEX[stage] > STAGES_INDEX[leaf_stage]:
            break
        parents = (
            (sources["S01"],)
            if stage == "S02"
            else (sources["S02"],)
            if stage == "S03"
            else (sources["S02"], sources["S03"])
        )
        artifact = _empty_stage_artifact(
            root,
            sleeve=sleeve,
            stage=stage,
            suffix=suffix,
            frame=(frames or {}).get(stage),
            **(artifact_kwargs_by_stage or {}).get(stage, {}),
        )
        payload = _build(
            root,
            identities,
            stage,
            sleeve=sleeve,
            parents=parents,
            artifact=artifact,
        )
        source = _receipt_source(root, stage, suffix=f"-{sleeve}-semantic")
        write_stage_receipt(source.path, payload)
        sources[stage] = source
        receipts[stage] = payload
    return identities, receipts, sources


STAGES_INDEX = {stage: index for index, stage in enumerate(("S00", "S01", "S02", "S03", "S04"))}


def _one_row(schema_id: str, overrides: dict[str, object]) -> pl.DataFrame:
    schema = ARTIFACT_SCHEMAS[schema_id]
    values: dict[str, object] = {}
    for field in schema.fields:
        if field.nullable:
            values[field.name] = None
        elif field.dtype == "utf8":
            values[field.name] = "x"
        elif field.dtype == "bool":
            values[field.name] = False
        elif field.dtype == "float64":
            values[field.name] = 1.0
        elif field.dtype == "list<utf8>":
            values[field.name] = []
        else:
            values[field.name] = 0
    values.update(overrides)
    return pl.DataFrame([values], schema=dict(artifact_polars_schema(schema_id)), strict=True)


def _continuous_s02_row() -> pl.DataFrame:
    return _one_row(
        "continuous_a0_signal_features",
        {
            "venue": "bybit",
            "symbol": "AAAUSDT",
            "canonical_instrument_id": "bybit:AAAUSDT",
            "signal_ts_ms": CONT_SIGNAL_TS_MS,
            "decision_ts_ms": CONT_DECISION_TS_MS,
            "signal_bar_close_ts_ms": CONT_DECISION_TS_MS,
            "feature_data_available_ts_ms": CONT_DECISION_TS_MS,
            "data_available_ts_ms": CONT_DECISION_TS_MS,
            "rmom_source_day_ts_ms": CONT_SIGNAL_TS_MS,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "turnover_quote_available": False,
            "rmom_stable_available": False,
            "rmom_is_provisional": False,
            "rmom_source_row_present": False,
            "rmom_present": False,
            "current_rmom_quantile_cutoff": 0.25,
            "component_mask": 0,
            "current_component_mask_before_liquidity": 0,
            "component_membership_count": 0,
            "component_tags": "",
            "implied_tier_weight": 0.0,
            "unique_decision_id": f"AAAUSDT|{CONT_SIGNAL_TS_MS}",
            "simultaneous_trigger_decision_count": 0,
            "current_p3_component_membership": False,
            "current_p4p3_component_membership": False,
            "current_p4p5_component_membership": False,
            "btc_uptrend_known": False,
            "btc_uptrend_pass": False,
            "btc_uptrend_fail": False,
            "btc_uptrend_unknown": True,
        },
    )


def _continuous_s03_row(*, missing_reason: str | None = "no_next_entry_bar") -> pl.DataFrame:
    return _one_row(
        "continuous_a0_entry_anchor",
        {
            "venue": "bybit",
            "symbol": "AAAUSDT",
            "canonical_instrument_id": "bybit:AAAUSDT",
            "signal_ts_ms": CONT_SIGNAL_TS_MS,
            "decision_ts_ms": CONT_DECISION_TS_MS,
            "entry_bar_start_ts_ms": CONT_DECISION_TS_MS,
            "entry_anchor_available": False,
            "missing_anchor_reason": missing_reason,
        },
    )


def _continuous_s03_available_row() -> pl.DataFrame:
    return _one_row(
        "continuous_a0_entry_anchor",
        {
            "venue": "bybit",
            "symbol": "AAAUSDT",
            "canonical_instrument_id": "bybit:AAAUSDT",
            "signal_ts_ms": CONT_SIGNAL_TS_MS,
            "decision_ts_ms": CONT_DECISION_TS_MS,
            "entry_bar_start_ts_ms": CONT_DECISION_TS_MS,
            "entry_anchor_ts_ms": CONT_DECISION_TS_MS + 60 * 60 * 1000,
            "entry_price": 1.0,
            "entry_anchor_available": True,
            "missing_anchor_reason": None,
        },
    )


def _continuous_s04_row() -> pl.DataFrame:
    values: dict[str, object] = {
        "venue": "bybit",
        "symbol": "AAAUSDT",
        "canonical_instrument_id": "bybit:AAAUSDT",
        "decision_ts_ms": CONT_DECISION_TS_MS,
        "path_all_minimal_labels_complete": False,
        "missing_path_reason": "no_next_executable_close",
    }
    for horizon in (1, 24, 72):
        values.update(
            {
                f"path_{horizon}h_observed_hours": 0,
                f"path_{horizon}h_available": False,
                f"path_{horizon}h_complete": False,
                f"path_{horizon}h_missing_reason": "no_entry_anchor",
                f"path_{horizon}h_hourly_extrema_interval_censored": True,
            }
        )
    return _one_row("continuous_a0_path_labels", values)


def _continuous_s04_complete_row() -> pl.DataFrame:
    values: dict[str, object] = {
        "venue": "bybit",
        "symbol": "AAAUSDT",
        "canonical_instrument_id": "bybit:AAAUSDT",
        "decision_ts_ms": CONT_DECISION_TS_MS,
        "path_all_minimal_labels_complete": True,
        "missing_path_reason": None,
    }
    for horizon in (1, 24, 72):
        values.update(
            {
                f"path_{horizon}h_close_ts_ms": CONT_DECISION_TS_MS + (horizon + 1) * 60 * 60 * 1000,
                f"path_{horizon}h_observed_hours": horizon,
                f"path_{horizon}h_available": True,
                f"path_{horizon}h_complete": True,
                f"path_{horizon}h_missing_reason": None,
                f"path_{horizon}h_underlying_return": 0.1,
                f"path_{horizon}h_short_directional_return": -0.1,
                f"path_{horizon}h_hourly_extrema_interval_censored": True,
            }
        )
        if horizon in (24, 72):
            values[f"path_{horizon}h_short_mfe"] = 0.1
            values[f"path_{horizon}h_short_mae"] = 0.1
    return _one_row("continuous_a0_path_labels", values)


def _long_s02_row() -> pl.DataFrame:
    return _one_row(
        "long_a0_signal_features",
        {
            "venue": "bybit",
            "symbol": "AAAUSDT",
            "canonical_instrument_id": "bybit:AAAUSDT",
            "signal_ts_ms": LONG_SIGNAL_TS_MS,
            "symbol_age_days": 1,
            "signal_feature_available_ts_ms": LONG_SIGNAL_TS_MS,
            "daily_bar_available_ts_ms": LONG_SIGNAL_TS_MS,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "today_volume_rank_population_peer_count": 1,
            "today_volume_rank_rankable_peer_count": 0,
            "today_volume_rank_missing_peer_count": 1,
            "today_volume_rank_tie_method": "ordinal_descending_value_then_symbol_ascending",
            "today_volume_rank_denominator_rule": "supplied_signal_ts_population",
            "universe_rank_population_peer_count": 1,
            "universe_rank_rankable_peer_count": 0,
            "universe_rank_missing_peer_count": 1,
            "universe_rank_tie_method": "ordinal_descending_value_then_symbol_ascending",
            "universe_rank_denominator_rule": "supplied_signal_ts_population",
            "global_lsr": None,
            "oi_chg_7d": None,
            "fc_trigger_1d": False,
            "fc_trigger_3d": False,
            "fc_trigger_7d": False,
            "fc_trigger_bitmask": 0,
            "fc_all_trigger": False,
            "classifier_selected": False,
            "classifier_eligible": False,
            "signal_bar_present": False,
            "signal_bar_complete": False,
            "fc_atr_exit_available": False,
            "fc_atr_fallback_used": True,
            "fc_exit_stop_pct": 0.1,
            "fc_exit_take_profit_pct": 0.2,
            "fc_exit_max_hold_hours": 72,
            "long_feature_tape_schema_version": "long_a0_signal_feature_v3",
        },
    )


def _long_s02_with_signal_close_row() -> pl.DataFrame:
    return _long_s02_row().with_columns(
        pl.lit(True).alias("signal_bar_present"),
        pl.lit(True).alias("signal_bar_complete"),
        pl.lit(1.0, dtype=pl.Float64).alias("signal_close_hourly"),
    )


def _long_s03_row() -> pl.DataFrame:
    return _one_row(
        "long_a0_entry_policy",
        {
            "venue": "bybit",
            "symbol": "AAAUSDT",
            "canonical_instrument_id": "bybit:AAAUSDT",
            "signal_ts_ms": LONG_SIGNAL_TS_MS,
            "common_entry_available": False,
            "common_entry_reason": "next_hour_bar_missing",
            "current_entry_available": False,
            "current_entry_reason": "initial_entry_bar_missing",
            "current_entry_scan_first_hour": 1,
            "current_entry_scan_missing_hour_bitmask": 0,
            "current_entry_scan_prefix_complete": False,
            "current_entry_close_triggered": False,
            "current_entry_policy_available": False,
            "current_entry_missing_reason": "initial_entry_bar_missing",
            "long_entry_policy_schema_version": "long_a0_entry_policy_v1",
        },
    )


def _long_s03_available_row() -> pl.DataFrame:
    improvement = 1.0 / 0.99 - 1.0
    return _one_row(
        "long_a0_entry_policy",
        {
            "venue": "bybit",
            "symbol": "AAAUSDT",
            "canonical_instrument_id": "bybit:AAAUSDT",
            "signal_ts_ms": LONG_SIGNAL_TS_MS,
            "common_entry_available": True,
            "common_entry_ts_ms": LONG_SIGNAL_TS_MS + 60 * 60 * 1000,
            "common_entry_hour": 1,
            "common_entry_price": 1.0,
            "common_entry_reason": "next_hour_close",
            "current_entry_available": True,
            "current_entry_ts_ms": LONG_SIGNAL_TS_MS + 60 * 60 * 1000,
            "current_entry_hour": 1,
            "current_entry_price": 0.99,
            "current_entry_reason": "sniper_retrace",
            "current_entry_retrace_pct": 0.01,
            "current_entry_retrace_threshold": 0.99,
            "current_entry_scan_first_hour": 1,
            "current_entry_scan_end_hour": 1,
            "current_entry_close_trigger_first_hour": 1,
            "current_entry_scan_missing_hour_bitmask": 0,
            "current_entry_scan_prefix_complete": True,
            "current_entry_close_triggered": True,
            "current_entry_intrabar_low_touch_nonfill": True,
            "current_entry_intrabar_low_first_hour_nonfill": 1,
            "current_entry_intrabar_low_observed_first_hour_nonfill": 1,
            "current_entry_policy_available": True,
            "current_entry_missing_reason": None,
            "entry_price_improvement": improvement,
            "entry_delay_hours_vs_common": 0,
            "long_entry_policy_schema_version": "long_a0_entry_policy_v1",
        },
    )


def _long_s03_signal_missing_row() -> pl.DataFrame:
    return _long_s03_row().with_columns(
        pl.lit(True).alias("common_entry_available"),
        pl.lit(LONG_SIGNAL_TS_MS + 60 * 60 * 1000, dtype=pl.Int64).alias("common_entry_ts_ms"),
        pl.lit(1, dtype=pl.Int64).alias("common_entry_hour"),
        pl.lit(1.0, dtype=pl.Float64).alias("common_entry_price"),
        pl.lit("next_hour_close").alias("common_entry_reason"),
        pl.lit("signal_bar_missing").alias("current_entry_reason"),
        pl.lit(0.01, dtype=pl.Float64).alias("current_entry_retrace_pct"),
        pl.lit("signal_bar_missing").alias("current_entry_missing_reason"),
    )


def _long_s03_fallthrough_row() -> pl.DataFrame:
    return _long_s03_available_row().with_columns(
        pl.lit(LONG_SIGNAL_TS_MS + 6 * 60 * 60 * 1000, dtype=pl.Int64).alias("current_entry_ts_ms"),
        pl.lit(6, dtype=pl.Int64).alias("current_entry_hour"),
        pl.lit(1.0, dtype=pl.Float64).alias("current_entry_price"),
        pl.lit("sniper_deadline_fallthrough").alias("current_entry_reason"),
        pl.lit(6, dtype=pl.Int64).alias("current_entry_scan_end_hour"),
        pl.lit(None, dtype=pl.Int64).alias("current_entry_close_trigger_first_hour"),
        pl.lit(False).alias("current_entry_close_triggered"),
        pl.lit(False).alias("current_entry_intrabar_low_touch_nonfill"),
        pl.lit(None, dtype=pl.Int64).alias("current_entry_intrabar_low_first_hour_nonfill"),
        pl.lit(None, dtype=pl.Int64).alias("current_entry_intrabar_low_observed_first_hour_nonfill"),
        pl.lit(0.0, dtype=pl.Float64).alias("entry_price_improvement"),
        pl.lit(5, dtype=pl.Int64).alias("entry_delay_hours_vs_common"),
    )


def _long_s03_deadline_missing_row() -> pl.DataFrame:
    return _long_s03_signal_missing_row().with_columns(
        pl.lit("sniper_deadline_missing").alias("current_entry_reason"),
        pl.lit(0.99, dtype=pl.Float64).alias("current_entry_retrace_threshold"),
        pl.lit(6, dtype=pl.Int64).alias("current_entry_scan_end_hour"),
        pl.lit(32, dtype=pl.Int8).alias("current_entry_scan_missing_hour_bitmask"),
        pl.lit("sniper_deadline_missing").alias("current_entry_missing_reason"),
    )


def _long_s04_row() -> pl.DataFrame:
    values: dict[str, object] = {
        "venue": "bybit",
        "symbol": "AAAUSDT",
        "canonical_instrument_id": "bybit:AAAUSDT",
        "signal_ts_ms": LONG_SIGNAL_TS_MS,
        "long_label_schema_version": "long_a0_minimal_labels_v1",
        "long_label_point_horizons": "1|24|72",
        "long_label_excursion_horizons": "24|72",
    }
    for prefix, reason in (
        ("common", "next_hour_bar_missing"),
        ("current", "initial_entry_bar_missing"),
    ):
        for horizon in (1, 24, 72):
            values.update(
                {
                    f"{prefix}_{horizon}h_point_available": False,
                    f"{prefix}_{horizon}h_observed_bars": 0,
                    f"{prefix}_{horizon}h_path_complete": False,
                    f"{prefix}_{horizon}h_missing_reason": f"anchor_unavailable:{reason}",
                    f"{prefix}_{horizon}h_hourly_extrema_interval_censored": True,
                }
            )
        values[f"{prefix}_label_complete"] = False
        values[f"{prefix}_missing_path_reason"] = "+".join(
            [
                f"1h:anchor_unavailable:{reason}",
                f"24h:anchor_unavailable:{reason}",
                f"72h:anchor_unavailable:{reason}",
                "ambiguity_levels",
            ]
        )
    return _one_row("long_a0_path_labels", values)


def _long_s04_complete_row() -> pl.DataFrame:
    signal = LONG_SIGNAL_TS_MS
    values: dict[str, object] = {
        "venue": "bybit",
        "symbol": "AAAUSDT",
        "canonical_instrument_id": "bybit:AAAUSDT",
        "signal_ts_ms": signal,
        "long_label_schema_version": "long_a0_minimal_labels_v1",
        "long_label_point_horizons": "1|24|72",
        "long_label_excursion_horizons": "24|72",
    }
    for prefix, anchor_price in (("common", 1.0), ("current", 0.99)):
        anchor_ts = signal + 60 * 60 * 1000
        for horizon in (1, 24, 72):
            values.update(
                {
                    f"{prefix}_{horizon}h_endpoint_ts_ms": anchor_ts + horizon * 60 * 60 * 1000,
                    f"{prefix}_{horizon}h_point_return": 0.0,
                    f"{prefix}_{horizon}h_point_available": True,
                    f"{prefix}_{horizon}h_observed_bars": horizon,
                    f"{prefix}_{horizon}h_path_complete": True,
                    f"{prefix}_{horizon}h_missing_reason": None,
                    f"{prefix}_{horizon}h_hourly_extrema_interval_censored": True,
                }
            )
            if horizon in (24, 72):
                values[f"{prefix}_{horizon}h_mfe"] = 0.0
                values[f"{prefix}_{horizon}h_signed_mae"] = 0.0
                values[f"{prefix}_{horizon}h_adverse_magnitude"] = 0.0
        values[f"{prefix}_stop_price"] = anchor_price * 0.9
        values[f"{prefix}_take_profit_price"] = anchor_price * 1.2
        values[f"{prefix}_same_bar_stop_tp_ambiguity"] = False
        values[f"{prefix}_label_complete"] = True
        values[f"{prefix}_missing_path_reason"] = None
    return _one_row("long_a0_path_labels", values)


def test_full_chain_is_deterministic_exact_and_transitively_verifiable(tmp_path: Path) -> None:
    _identities, stages = _chain(tmp_path)

    assert stages["S00"][0]["run_id"] != stages["S01"][0]["run_id"]
    assert len({stages[name][0]["run_id"] for name in ("S01", "S02", "S03", "S04")}) == 1
    assert stages["S00"][0]["declared_outcome_blind"] is True
    assert stages["S01"][0]["declared_outcome_blind"] is True
    assert stages["S02"][0]["declared_outcome_blind"] is True
    assert stages["S03"][0]["declared_outcome_blind"] is False
    assert stages["S04"][0]["declared_outcome_blind"] is False
    assert [row["stage"] for row in stages["S04"][0]["parents"]] == ["S02", "S03"]
    assert stages["S04"][0]["artifact"]["declared_schema_identity"] == {
        "schema_id": registered_stage_schema("continuous", "S04").schema_id,
        "schema_version": registered_stage_schema("continuous", "S04").schema_version,
        "schema_sha256": registered_stage_schema("continuous", "S04").schema_sha256,
    }
    assert all(payload["real_money_authorized"] is False for payload, _source in stages.values())
    assert all(payload["provenance_blockers_cleared"] is False for payload, _source in stages.values())
    assert all(payload["outcome_run_authorized"] is False for payload, _source in stages.values())

    s04_path = stages["S04"][1].path
    verification = verify_stage_receipt_byte_bindings(s04_path, binding_root=tmp_path)
    assert verification.stage == "S04"
    assert verification.byte_verified_receipt_count == 5
    assert verification.byte_verified_bound_file_count == 16
    assert verification.semantic_validation_performed is False
    assert load_stage_receipt(s04_path) == stages["S04"][0]
    assert (
        _build(
            tmp_path,
            _identities,
            "S04",
            parents=(stages["S02"][1], stages["S03"][1]),
            artifact=ArtifactInput(
                logical_path="artifacts/s04.bin",
                path=tmp_path / "artifacts/s04.bin",
                declared_row_count=4,
                declared_key_projection_sha256=_sha("S04-keys"),
            ),
        )
        == stages["S04"][0]
    )

    serialized = render_stage_receipt(stages["S04"][0]).decode("utf-8")
    assert str(tmp_path) not in serialized
    assert "generated_at" not in serialized
    assert "timestamp" not in serialized


def test_s00_is_constructible_before_any_s01_identity_exists(tmp_path: Path) -> None:
    s00_kinds = STAGE_IDENTITY_RECEIPT_KINDS["S00"]
    identities = _identity_inputs(tmp_path, s00_kinds)
    s00, s00_source = _build_and_write(tmp_path, identities, "S00")

    assert tuple(s00["identity_receipts"]) == s00_kinds
    assert all(not (tmp_path / f"identities/{kind}.json").exists() for kind in IDENTITY_RECEIPT_KINDS[3:])

    future_kinds = tuple(kind for kind in IDENTITY_RECEIPT_KINDS if kind not in s00_kinds)
    identities.update(_identity_inputs(tmp_path, future_kinds))
    s01, s01_source = _build_and_write(tmp_path, identities, "S01", parents=(s00_source,))

    assert s00["run_id"] != s01["run_id"]
    verification = verify_stage_receipt_byte_bindings(s01_source.path, binding_root=tmp_path)
    assert verification.byte_verified_receipt_count == 2


def test_s01_must_reuse_s00_shared_identity_byte_bindings(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    alternative = _write(
        tmp_path / "identities/source_snapshot_s01.json",
        canonical_json_bytes({"different_source_snapshot": True}) + b"\n",
    )
    identities["source_snapshot"] = BoundFileInput(
        logical_path="identities/source_snapshot_s01.json",
        path=alternative,
    )

    with pytest.raises(StageReceiptError, match="source_snapshot identity byte binding"):
        _build(tmp_path, identities, "S01", parents=(s00_source,))


def test_arbitrary_artifact_bytes_are_bound_without_semantic_overclaim(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    _s01, s01_source = _build_and_write(tmp_path, identities, "S01", parents=(s00_source,))
    arbitrary_path = _write(tmp_path / "artifacts/arbitrary.bin", b"\x00not-a-table\xfffuture_return")
    receipt = _build(
        tmp_path,
        identities,
        "S02",
        parents=(s01_source,),
        artifact=ArtifactInput(
            logical_path="artifacts/arbitrary.bin",
            path=arbitrary_path,
            declared_row_count=999_999,
            declared_key_projection_sha256=_sha("caller-invented-key-projection"),
        ),
    )

    artifact = receipt["artifact"]
    assert receipt["receipt_type"] == STAGE_RECEIPT_TYPE
    assert receipt["receipt_scope"] == RECEIPT_SCOPE
    assert receipt["diagnostic_only"] is True
    assert receipt["artifact_claims_verified"] is False
    assert receipt["outcome_blindness_verified"] is False
    assert receipt["declared_outcome_blind"] is True
    assert artifact["declaration_status"] == UNVERIFIED_ARTIFACT_DECLARATIONS
    assert artifact["declared_row_count"] == 999_999
    assert artifact["declared_schema_identity"]["schema_id"] == registered_stage_schema("continuous", "S02").schema_id
    assert receipt["identity_receipts"]["config"]["semantic_verification_status"] == (
        CONFIG_IDENTITY_VERIFICATION_STATUS
    )
    assert receipt["identity_receipts"]["root"]["semantic_verification_status"] == (OPAQUE_IDENTITY_VERIFICATION_STATUS)

    overclaim = dict(receipt)
    overclaim["artifact_claims_verified"] = True
    with pytest.raises(StageReceiptError, match="cannot be presented as verified"):
        render_stage_receipt(overclaim)


@pytest.mark.parametrize("counterfeit", ["artifact_type", "strategy_profile", "extra_field"])
def test_self_consistent_counterfeit_config_identity_is_refused_at_binding(
    tmp_path: Path,
    counterfeit: str,
) -> None:
    identities = _identity_inputs(tmp_path)
    payload = copy.deepcopy(_config_identity())
    if counterfeit == "artifact_type":
        payload["artifact_type"] = "counterfeit_config_identity"
    elif counterfeit == "strategy_profile":
        payload["canonical_config"]["config"]["strategy_profile"] = "counterfeit_profile"
    else:
        payload["counterfeit_extra_field"] = True
    payload = _rehash_config_identity(payload)
    identities["config"].path.write_bytes(canonical_json_bytes(payload) + b"\n")

    with pytest.raises(StageReceiptError, match="repository-derived canonical config identity"):
        build_stage_receipt(
            sleeve="continuous",
            venue="bybit",
            stage="S00",
            declared_outcome_blind=True,
            canonical_config_identity_sha256=str(payload["identity_sha256"]),
            registered_scope_sha256=str(payload["scope_sha256"]),
            identity_receipts={kind: identities[kind] for kind in STAGE_IDENTITY_RECEIPT_KINDS["S00"]},
            artifact=_artifact(tmp_path, "S00"),
            binding_root=tmp_path,
        )


def test_archival_byte_verification_does_not_consult_current_registry_or_factories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _identities, stages = _chain(tmp_path)

    def unexpected_current_lookup(*_args, **_kwargs):
        raise AssertionError("archival byte verification consulted mutable current state")

    monkeypatch.setattr(stage_receipt_module, "registered_stage_schema", unexpected_current_lookup)
    monkeypatch.setattr(
        stage_receipt_module,
        "derive_continuous_a0_config_identity",
        unexpected_current_lookup,
    )
    verification = verify_stage_receipt_byte_bindings(
        stages["S04"][1].path,
        binding_root=tmp_path,
    )

    assert verification.byte_verified_receipt_count == 5
    assert verification.semantic_validation_performed is False
    assert verification.current_registry_or_config_factories_consulted is False


def test_construction_refuses_old_parent_schema_after_registry_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = _identity_inputs(tmp_path)
    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    _s01, s01_source = _build_and_write(
        tmp_path,
        identities,
        "S01",
        parents=(s00_source,),
    )
    _old_s02, old_s02_source = _build_and_write(
        tmp_path,
        identities,
        "S02",
        parents=(s01_source,),
    )
    original_lookup = stage_receipt_module.registered_stage_schema

    def drifted_registry(sleeve: str, stage: str):
        previous = original_lookup(sleeve, stage)
        return type(previous)(
            schema_id=previous.schema_id,
            schema_version=f"{previous.schema_version}-drifted",
            schema_sha256=_sha(f"drifted-{sleeve}-{stage}"),
        )

    monkeypatch.setattr(
        stage_receipt_module,
        "registered_stage_schema",
        drifted_registry,
    )
    with pytest.raises(
        StageReceiptError,
        match="parent-chain receipt continuous/S02 declared schema.*current registry",
    ):
        _build(
            tmp_path,
            identities,
            "S03",
            parents=(old_s02_source,),
            schema_override=drifted_registry("continuous", "S03"),
        )


@pytest.mark.parametrize("tamper_target", ["artifact", "identity", "parent"])
def test_verification_rejects_current_byte_tampering(tmp_path: Path, tamper_target: str) -> None:
    identities, stages = _chain(tmp_path)
    if tamper_target == "artifact":
        (tmp_path / "artifacts/s03.bin").write_bytes(b"tampered-artifact")
    elif tamper_target == "identity":
        identities["environment"].path.write_bytes(b'{"tampered":true}\n')
    else:
        stages["S03"][1].path.write_bytes(stages["S03"][1].path.read_bytes() + b" ")

    with pytest.raises(StageReceiptError, match="current bytes"):
        verify_stage_receipt_byte_bindings(stages["S04"][1].path, binding_root=tmp_path)


def test_wrong_parent_stage_and_s04_lineage_fail_closed(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    with pytest.raises(StageReceiptError, match="S02 requires parents"):
        _build(tmp_path, identities, "S02", parents=(s00_source,))

    _s01, s01_source = _build_and_write(tmp_path, identities, "S01", parents=(s00_source,))
    _primary_s02, primary_s02_source = _build_and_write(tmp_path, identities, "S02", parents=(s01_source,))
    _other_s02, other_s02_source = _build_and_write(
        tmp_path,
        identities,
        "S02",
        parents=(s01_source,),
        suffix="-other",
        artifact=_artifact(tmp_path, "S02", suffix="-other", row_count=99),
    )
    _other_s03, other_s03_source = _build_and_write(
        tmp_path,
        identities,
        "S03",
        parents=(other_s02_source,),
        suffix="-other",
    )

    with pytest.raises(StageReceiptError, match="same direct S02"):
        _build(
            tmp_path,
            identities,
            "S04",
            parents=(primary_s02_source, other_s03_source),
        )


def test_wrong_schema_stage_and_outcome_blind_state_fail_closed(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    with pytest.raises(StageReceiptError, match="S00 requires declared_outcome_blind=true"):
        _build(tmp_path, identities, "S00", outcome_blind=False)

    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    with pytest.raises(StageReceiptError, match="S01 requires declared_outcome_blind=true"):
        _build(tmp_path, identities, "S01", parents=(s00_source,), outcome_blind=False)

    _s01, s01_source = _build_and_write(tmp_path, identities, "S01", parents=(s00_source,))

    with pytest.raises(StageReceiptError, match="schema identity mismatch"):
        _build(
            tmp_path,
            identities,
            "S02",
            parents=(s01_source,),
            schema_override=registered_stage_schema("long", "S02"),
        )
    with pytest.raises(StageReceiptError, match="S02 requires declared_outcome_blind=true"):
        _build(tmp_path, identities, "S02", parents=(s01_source,), outcome_blind=False)

    _s02, s02_source = _build_and_write(tmp_path, identities, "S02", parents=(s01_source,))
    with pytest.raises(StageReceiptError, match="S03 requires declared_outcome_blind=false"):
        _build(tmp_path, identities, "S03", parents=(s02_source,), outcome_blind=True)


def test_atomic_write_reuses_only_byte_identical_receipt(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    artifact = _artifact(tmp_path, "S00")
    first = _build(tmp_path, identities, "S00", artifact=artifact)
    target = tmp_path / "receipts/s00.json"

    initial = write_stage_receipt(target, first)
    reused = write_stage_receipt(target, first)
    assert initial.reused is False
    assert reused.reused is True
    assert initial.file_sha256 == reused.file_sha256

    changed = _build(
        tmp_path,
        identities,
        "S00",
        artifact=ArtifactInput(
            logical_path=artifact.logical_path,
            path=artifact.path,
            declared_row_count=123,
            declared_key_projection_sha256=artifact.declared_key_projection_sha256,
        ),
    )
    with pytest.raises(StageReceiptError, match="refusing to overwrite non-identical"):
        write_stage_receipt(target, changed)


def test_real_money_and_non_strict_json_are_refused(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    config_identity = _config_identity()
    with pytest.raises(StageReceiptError, match="real_money_authorized=false"):
        build_stage_receipt(
            sleeve="continuous",
            venue="bybit",
            stage="S00",
            declared_outcome_blind=True,
            canonical_config_identity_sha256=str(config_identity["identity_sha256"]),
            registered_scope_sha256=str(config_identity["scope_sha256"]),
            identity_receipts={kind: identities[kind] for kind in STAGE_IDENTITY_RECEIPT_KINDS["S00"]},
            artifact=_artifact(tmp_path, "S00"),
            real_money_authorized=True,
        )

    receipt = _build(tmp_path, identities, "S00")
    receipt["unexpected_nan"] = float("nan")
    with pytest.raises(StageReceiptError, match="NaN or infinity"):
        canonical_json_bytes(receipt)

    bad_identity = identities["source_snapshot"]
    bad_identity.path.write_text('{"value":NaN}\n', encoding="utf-8")
    with pytest.raises(StageReceiptError, match="invalid constant"):
        _build(tmp_path, identities, "S00")


def test_absolute_or_traversing_logical_paths_are_never_embedded(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    with pytest.raises(StageReceiptError, match="must not be absolute"):
        _build(
            tmp_path,
            identities,
            "S00",
            artifact=ArtifactInput(
                logical_path=str((tmp_path / "absolute.bin").resolve()),
                path=_write(tmp_path / "absolute.bin", b"artifact"),
                declared_row_count=0,
                declared_key_projection_sha256=_sha("keys"),
            ),
        )
    identities["environment"] = BoundFileInput(
        logical_path="../environment.json",
        path=identities["environment"].path,
    )
    with pytest.raises(StageReceiptError, match="dot traversal"):
        _build(tmp_path, identities, "S00")


def test_artifact_and_existing_receipt_symlinks_are_refused(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    artifact_target = _write(tmp_path / "artifacts/target.bin", b"target")
    artifact_link = tmp_path / "artifacts/link.bin"
    artifact_link.symlink_to(artifact_target)
    with pytest.raises(StageReceiptError, match="regular non-symlink"):
        _build(
            tmp_path,
            identities,
            "S00",
            artifact=ArtifactInput(
                logical_path="artifacts/link.bin",
                path=artifact_link,
                declared_row_count=1,
                declared_key_projection_sha256=_sha("keys"),
            ),
        )

    receipt = _build(tmp_path, identities, "S00")
    receipt_target = _write(
        tmp_path / "receipts/target.json",
        render_stage_receipt(receipt),
    )
    receipt_link = tmp_path / "receipts/link.json"
    receipt_link.symlink_to(receipt_target)
    with pytest.raises(StageReceiptError, match="regular non-symlink"):
        write_stage_receipt(receipt_link, receipt)


def test_descriptor_read_does_not_follow_path_swap_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "source.bin", b"original-bytes")
    moved = tmp_path / "opened-file.bin"
    replacement = _write(tmp_path / "replacement.bin", b"replacement-bytes")
    real_read = stage_receipt_module.os.read
    swapped = False

    def swapping_read(descriptor: int, byte_count: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(moved)
            source.symlink_to(replacement)
        return real_read(descriptor, byte_count)

    monkeypatch.setattr(stage_receipt_module.os, "read", swapping_read)
    with pytest.raises(StageReceiptError, match="changed while being read"):
        stage_receipt_module._regular_file_bytes(source, name="test input")
    assert moved.read_bytes() == b"original-bytes"
    assert replacement.read_bytes() == b"replacement-bytes"
    with pytest.raises(StageReceiptError, match="regular non-symlink"):
        stage_receipt_module._regular_file_bytes(source, name="test input")


def test_descriptor_read_rejects_concurrent_file_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "source.bin", b"a" * (2 * 1024 * 1024))
    real_read = stage_receipt_module.os.read
    mutated = False

    def mutating_read(descriptor: int, byte_count: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, byte_count)
        if chunk and not mutated:
            mutated = True
            with source.open("r+b") as handle:
                handle.seek(-1, 2)
                handle.write(b"b")
                handle.flush()
        return chunk

    monkeypatch.setattr(stage_receipt_module.os, "read", mutating_read)
    with pytest.raises(StageReceiptError, match="changed while being read"):
        stage_receipt_module._regular_file_bytes(source, name="test input")


def test_descriptor_read_rejects_fifo_without_waiting_for_a_writer(tmp_path: Path) -> None:
    if not hasattr(stage_receipt_module.os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform")
    fifo = tmp_path / "blocking-input.fifo"
    stage_receipt_module.os.mkfifo(fifo)
    probe = """
import sys
from pathlib import Path
from liquidity_migration.strategy_overhaul_stage_receipt import StageReceiptError, _regular_file_bytes

try:
    _regular_file_bytes(Path(sys.argv[1]), name="FIFO probe")
except StageReceiptError as exc:
    if "regular non-symlink" not in str(exc):
        raise
else:
    raise AssertionError("FIFO input was accepted")
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(fifo)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("sleeve", ["continuous", "long"])
@pytest.mark.parametrize("stage", ["S02", "S03", "S04"])
def test_registered_schema_identity_matches_exact_registry(sleeve: str, stage: str) -> None:
    identity = registered_stage_schema(sleeve, stage)
    assert identity.schema_id.startswith(f"{sleeve}_a0_")
    assert identity.schema_version
    assert len(identity.schema_sha256) == 64


def test_written_receipt_is_strict_canonical_json(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    payload = _build(tmp_path, identities, "S00")
    path = tmp_path / "receipt.json"
    write_stage_receipt(path, payload)

    assert path.read_bytes() == canonical_json_bytes(payload) + b"\n"
    assert json.loads(path.read_text(encoding="utf-8")) == payload

    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(StageReceiptError, match="canonical byte representation"):
        load_stage_receipt(path)


def test_self_payload_tamper_is_detected_even_when_bytes_remain_canonical(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    payload = _build(tmp_path, identities, "S00")
    path = tmp_path / "receipt.json"
    write_stage_receipt(path, payload)

    tampered = dict(payload)
    artifact = dict(tampered["artifact"])
    artifact["declared_row_count"] = 999
    tampered["artifact"] = artifact
    path.write_bytes(canonical_json_bytes(tampered) + b"\n")

    with pytest.raises(StageReceiptError, match="payload SHA-256 mismatch"):
        load_stage_receipt(path)


@pytest.mark.parametrize("sleeve", ["continuous", "long"])
@pytest.mark.parametrize("suffix", [".parquet", ".ipc"])
@pytest.mark.parametrize("leaf_stage", ["S02", "S03", "S04"])
def test_semantic_verification_accepts_transitive_registered_chains(
    tmp_path: Path,
    sleeve: str,
    suffix: str,
    leaf_stage: str,
) -> None:
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve=sleeve,
        suffix=suffix,
        leaf_stage=leaf_stage,
    )
    byte_receipt_before = sources[leaf_stage].path.read_bytes()

    result = verify_stage_receipt_semantics(sources[leaf_stage].path, binding_root=tmp_path)

    assert result.stage == leaf_stage
    assert result.sleeve == sleeve
    assert result.semantic_verified_stage_count == STAGES_INDEX[leaf_stage] - STAGES_INDEX["S01"]
    assert result.semantic_validation_performed is True
    assert result.current_registry_or_config_factories_consulted is True
    assert result.receipt["verification_status"] == ("VERIFIED_SCHEMA_KEYS_POPULATION_AND_SELECTED_STAGE_INVARIANTS")
    assert result.receipt["population_identity_and_s02_keys_verified"] is True
    assert result.receipt["population_verification"]["source_keys_file_sha256"]
    assert result.receipt["population_verification"]["source_keys_row_count"] >= 0
    assert result.receipt["outcome_blindness_verified"] is False
    assert result.receipt["population_or_root_completeness_verified"] is False
    assert "does_not_exhaustively_validate_every_field_level_semantic_relation" in result.receipt["limitations"]
    assert [row["stage"] for row in result.receipt["semantic_stage_artifacts"]] == [
        stage for stage in ("S02", "S03", "S04") if STAGES_INDEX[stage] <= STAGES_INDEX[leaf_stage]
    ]
    assert sources[leaf_stage].path.read_bytes() == byte_receipt_before


@pytest.mark.parametrize(
    ("sleeve", "schema_id", "frame"),
    [
        (
            "continuous",
            "continuous_a0_signal_features",
            pl.DataFrame(
                {"venue": ["bybit"], "symbol": ["AAAUSDT"], "decision_ts_ms": [3_600_000]},
                schema=dict(CONTINUOUS_REGISTERED_S02_KEY_SCHEMA),
            ),
        ),
        (
            "long",
            "long_a0_signal_features",
            pl.DataFrame(
                {"venue": ["bybit"], "symbol": ["AAAUSDT"], "signal_ts_ms": [86_400_000]},
                schema=dict(LONG_REGISTERED_S02_KEY_SCHEMA),
            ),
        ),
    ],
)
def test_stage_and_population_modules_share_one_registered_s02_key_hash(
    sleeve: str,
    schema_id: str,
    frame: pl.DataFrame,
) -> None:
    assert canonical_stage_key_projection_sha256(frame, schema_id) == registered_s02_key_sha256(
        frame,
        sleeve=sleeve,
    )


@pytest.mark.parametrize(
    ("sleeve", "frames"),
    [
        (
            "continuous",
            {
                "S02": _continuous_s02_row(),
                "S03": _continuous_s03_row(),
                "S04": _continuous_s04_row(),
            },
        ),
        (
            "long",
            {
                "S02": _long_s02_row(),
                "S03": _long_s03_row(),
                "S04": _long_s04_row(),
            },
        ),
    ],
)
@pytest.mark.parametrize("suffix", [".parquet", ".feather"])
def test_nonempty_semantic_chain_verifies_stage_and_parent_invariants(
    tmp_path: Path,
    sleeve: str,
    frames: dict[str, pl.DataFrame],
    suffix: str,
) -> None:
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve=sleeve,
        suffix=suffix,
        frames=frames,
        leaf_stage="S04",
    )

    result = verify_stage_receipt_semantics(sources["S04"].path, binding_root=tmp_path)

    assert result.semantic_verified_stage_count == 3
    assert result.receipt["population_verification"]["expected_population_row_count"] == 1
    assert len(result.receipt["transitive_stage_relations"]) == 2


def test_semantic_verification_rejects_long_age_drift_from_expected_population(
    tmp_path: Path,
) -> None:
    s02 = _long_s02_row().with_columns(pl.lit(999, dtype=pl.Int64).alias("symbol_age_days"))
    expected = s02.select("symbol", "signal_ts_ms").with_columns(pl.lit(1, dtype=pl.Int64).alias("symbol_age_days"))
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve="long",
        frames={"S02": s02},
        population_expected=expected,
        leaf_stage="S02",
    )

    with pytest.raises(StageReceiptError, match="symbol_age_days does not exactly equal"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_expected_population_outside_source_keys(
    tmp_path: Path,
) -> None:
    s02 = _continuous_s02_row()
    expected = s02.select("symbol", "signal_ts_ms")
    empty_source = pl.DataFrame(schema=dict(CONTINUOUS_KEY_SCHEMA))
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve="continuous",
        frames={"S02": s02},
        population_expected=expected,
        population_source=empty_source,
        leaf_stage="S02",
    )

    with pytest.raises(StageReceiptError, match="not an exact key/age subset"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_current_config_exclusions(tmp_path: Path) -> None:
    s02 = _continuous_s02_row().with_columns(
        pl.lit("BUSDUSDT", dtype=pl.String).alias("symbol"),
        pl.lit(f"BUSDUSDT|{CONT_SIGNAL_TS_MS}", dtype=pl.String).alias("unique_decision_id"),
    )
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve="continuous",
        frames={"S02": s02},
        leaf_stage="S02",
    )

    with pytest.raises(StageReceiptError, match="current config exclusion"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_s02_keys_outside_registered_scope(
    tmp_path: Path,
) -> None:
    signal = CONT_SIGNAL_TS_MS - 10 * 365 * 24 * 60 * 60 * 1000
    decision = signal + 60 * 60 * 1000
    s02 = _continuous_s02_row().with_columns(
        pl.lit(signal, dtype=pl.Int64).alias("signal_ts_ms"),
        pl.lit(decision, dtype=pl.Int64).alias("decision_ts_ms"),
        pl.lit(decision, dtype=pl.Int64).alias("signal_bar_close_ts_ms"),
        pl.lit(decision, dtype=pl.Int64).alias("feature_data_available_ts_ms"),
        pl.lit(decision, dtype=pl.Int64).alias("data_available_ts_ms"),
        pl.lit(signal, dtype=pl.Int64).alias("rmom_source_day_ts_ms"),
        pl.lit(f"AAAUSDT|{signal}", dtype=pl.String).alias("unique_decision_id"),
    )
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve="continuous",
        frames={"S02": s02},
        leaf_stage="S02",
    )

    with pytest.raises(StageReceiptError, match="outside the canonical registered scope"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


@pytest.mark.parametrize(
    ("sleeve", "frames"),
    [
        (
            "continuous",
            {
                "S02": _continuous_s02_row(),
                "S03": _continuous_s03_available_row(),
                "S04": _continuous_s04_complete_row(),
            },
        ),
        (
            "long",
            {
                "S02": _long_s02_with_signal_close_row(),
                "S03": _long_s03_available_row(),
                "S04": _long_s04_complete_row(),
            },
        ),
    ],
)
def test_complete_available_semantic_chain_passes_nullable_branch_matrix(
    tmp_path: Path,
    sleeve: str,
    frames: dict[str, pl.DataFrame],
) -> None:
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve=sleeve,
        frames=frames,
        leaf_stage="S04",
    )

    result = verify_stage_receipt_semantics(sources["S04"].path, binding_root=tmp_path)

    assert result.stage == "S04"
    assert result.receipt["verification_status"].endswith("SELECTED_STAGE_INVARIANTS")


def test_semantic_receipt_is_separate_canonical_and_immutable(tmp_path: Path) -> None:
    _identities, _receipts, sources = _semantic_chain(tmp_path, leaf_stage="S04")
    verification = verify_stage_receipt_semantics(sources["S04"].path, binding_root=tmp_path)
    target = tmp_path / "semantic/s04.json"

    first = write_stage_semantic_receipt(target, verification.receipt)
    reused = write_stage_semantic_receipt(target, verification.receipt)

    assert first.reused is False
    assert reused.reused is True
    assert target.read_bytes() == render_stage_semantic_receipt(verification.receipt)
    assert load_stage_semantic_receipt(target) == verification.receipt
    changed = copy.deepcopy(verification.receipt)
    changed["limitations"] = []
    with pytest.raises(StageReceiptError, match="limitations"):
        write_stage_semantic_receipt(target, changed)


def test_semantic_verification_rejects_unsupported_text_format(tmp_path: Path) -> None:
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        suffix=".csv",
        leaf_stage="S02",
    )

    with pytest.raises(StageReceiptError, match="unsupported semantic artifact format"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


@pytest.mark.parametrize("malformation", ["order", "dtype"])
def test_semantic_verification_rejects_physical_schema_drift(
    tmp_path: Path,
    malformation: str,
) -> None:
    frame = empty_artifact_frame("continuous_a0_signal_features")
    if malformation == "order":
        frame = frame.select(list(reversed(frame.columns)))
        match = "column order/projection mismatch"
    else:
        frame = frame.with_columns(pl.col("decision_ts_ms").cast(pl.Int32))
        match = "invalid physical dtypes"
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames={"S02": frame},
        leaf_stage="S02",
        artifact_kwargs_by_stage=(
            {"S02": {"declared_key_hash": hashlib.sha256(b"").hexdigest()}} if malformation == "dtype" else None
        ),
    )

    with pytest.raises(StageReceiptError, match=match):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


@pytest.mark.parametrize("declaration", ["rows", "keys"])
def test_semantic_verification_rejects_false_artifact_declarations(
    tmp_path: Path,
    declaration: str,
) -> None:
    kwargs = (
        {"declared_row_count": 1}
        if declaration == "rows"
        else {"declared_key_hash": _sha("not-the-canonical-empty-projection")}
    )
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        leaf_stage="S02",
        artifact_kwargs_by_stage={"S02": kwargs},
    )

    with pytest.raises(StageReceiptError, match="declared row count|declared key projection"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_population_identity_mismatch(tmp_path: Path) -> None:
    def counterfeit(bindings: dict[str, object]) -> None:
        root = dict(bindings["root"])
        root["file_sha256"] = _sha("different-root")
        bindings["root"] = root

    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        leaf_stage="S02",
        population_binding_mutation=counterfeit,
    )

    with pytest.raises(StageReceiptError, match="root bytes/JSON identity do not match S02"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_s02_population_key_mismatch(tmp_path: Path) -> None:
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames={"S02": _continuous_s02_row()},
        population_expected=pl.DataFrame(schema=dict(CONTINUOUS_KEY_SCHEMA)),
        leaf_stage="S02",
    )

    with pytest.raises(StageReceiptError, match="S02 registered key population does not exactly equal"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


@pytest.mark.parametrize("tamper", ["delete_source", "change_expected"])
def test_semantic_verification_rejects_missing_or_tampered_population_artifacts(
    tmp_path: Path,
    tamper: str,
) -> None:
    _identities, _receipts, sources = _semantic_chain(tmp_path, leaf_stage="S02")
    if tamper == "delete_source":
        (tmp_path / "population" / SOURCE_KEYS_FILENAME).unlink()
    else:
        (tmp_path / "population" / EXPECTED_POPULATION_FILENAME).write_bytes(b'{"counterfeit":true}\n')

    with pytest.raises(StageReceiptError, match="expected-population artifacts failed verification"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_stage_specific_invariant_drift(tmp_path: Path) -> None:
    frames = {
        "S02": _continuous_s02_row(),
        "S03": _continuous_s03_row(missing_reason="counterfeit_reason"),
    }
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames=frames,
        leaf_stage="S03",
    )

    with pytest.raises(StageReceiptError, match="CONTINUOUS S03 violates frozen"):
        verify_stage_receipt_semantics(sources["S03"].path, binding_root=tmp_path)


def test_continuous_s02_accepts_raw_p3_trigger_outside_current_q25(tmp_path: Path) -> None:
    s02 = _continuous_s02_row().with_columns(
        pl.lit(True).alias("trigger_turn3_pop3"),
        pl.lit(True).alias("trigger_any_current_component"),
        pl.lit(1, dtype=pl.Int8).alias("component_mask"),
        pl.lit(1, dtype=pl.Int8).alias("component_membership_count"),
        pl.lit("p3").alias("component_tags"),
        pl.lit(1.0 / 3.0, dtype=pl.Float64).alias("implied_tier_weight"),
        pl.lit(1, dtype=pl.Int64).alias("simultaneous_trigger_decision_count"),
    )
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames={"S02": s02},
        leaf_stage="S02",
    )

    result = verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)
    assert result.semantic_verified_stage_count == 1


def test_continuous_s02_rejects_counterfeit_derived_component_fields(tmp_path: Path) -> None:
    s02 = _continuous_s02_row().with_columns(
        pl.lit(7, dtype=pl.Int8).alias("component_mask"),
        pl.lit(7, dtype=pl.Int8).alias("current_component_mask_before_liquidity"),
        pl.lit("counterfeit").alias("component_tags"),
        pl.lit(999.0, dtype=pl.Float64).alias("implied_tier_weight"),
        pl.lit("counterfeit").alias("unique_decision_id"),
        pl.lit(999, dtype=pl.Int64).alias("simultaneous_trigger_decision_count"),
    )
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames={"S02": s02},
        leaf_stage="S02",
    )

    with pytest.raises(StageReceiptError, match="source/static-state semantics"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


@pytest.mark.parametrize(
    ("s02", "s03"),
    [
        (_long_s02_row(), _long_s03_row()),
        (_long_s02_row(), _long_s03_signal_missing_row()),
        (_long_s02_with_signal_close_row(), _long_s03_available_row()),
        (_long_s02_with_signal_close_row(), _long_s03_fallthrough_row()),
        (_long_s02_with_signal_close_row(), _long_s03_deadline_missing_row()),
    ],
    ids=["initial-missing", "signal-missing", "retrace", "fallthrough", "deadline-missing"],
)
def test_long_s03_accepts_each_frozen_entry_state(
    tmp_path: Path,
    s02: pl.DataFrame,
    s03: pl.DataFrame,
) -> None:
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve="long",
        frames={"S02": s02, "S03": s03},
        leaf_stage="S03",
    )

    result = verify_stage_receipt_semantics(sources["S03"].path, binding_root=tmp_path)
    assert result.semantic_verified_stage_count == 2


@pytest.mark.parametrize("case", ["unknown_reason", "fallthrough_null_state", "mask_prefix"])
def test_long_s03_rejects_counterfeit_state_reason_or_prefix(
    tmp_path: Path,
    case: str,
) -> None:
    if case == "unknown_reason":
        s02 = _long_s02_row()
        s03 = _long_s03_row().with_columns(
            pl.lit("totally_counterfeit_reason").alias("current_entry_reason"),
            pl.lit("totally_counterfeit_reason").alias("current_entry_missing_reason"),
        )
    elif case == "fallthrough_null_state":
        s02 = _long_s02_with_signal_close_row()
        s03 = _long_s03_fallthrough_row().with_columns(
            pl.lit(None, dtype=pl.Float64).alias("current_entry_retrace_pct"),
            pl.lit(None, dtype=pl.Float64).alias("current_entry_retrace_threshold"),
            pl.lit(None, dtype=pl.Int64).alias("current_entry_scan_first_hour"),
            pl.lit(None, dtype=pl.Int64).alias("current_entry_scan_end_hour"),
        )
    else:
        s02 = _long_s02_with_signal_close_row()
        s03 = _long_s03_available_row().with_columns(
            pl.lit(1, dtype=pl.Int8).alias("current_entry_scan_missing_hour_bitmask"),
            pl.lit(True).alias("current_entry_scan_prefix_complete"),
        )
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve="long",
        frames={"S02": s02, "S03": s03},
        leaf_stage="S03",
    )

    with pytest.raises(StageReceiptError, match="LONG S03 violates frozen"):
        verify_stage_receipt_semantics(sources["S03"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_null_unavailable_s03_reason(tmp_path: Path) -> None:
    frames = {
        "S02": _continuous_s02_row(),
        "S03": _continuous_s03_row(missing_reason=None),
    }
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames=frames,
        leaf_stage="S03",
    )

    with pytest.raises(StageReceiptError, match="CONTINUOUS S03 violates frozen"):
        verify_stage_receipt_semantics(sources["S03"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_null_s04_endpoints_for_available_anchor(tmp_path: Path) -> None:
    frames = {
        "S02": _continuous_s02_row(),
        "S03": _continuous_s03_available_row(),
        "S04": _continuous_s04_row(),
    }
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames=frames,
        leaf_stage="S04",
    )

    with pytest.raises(StageReceiptError, match="S04 anchor support does not propagate"):
        verify_stage_receipt_semantics(sources["S04"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_null_unavailable_long_s03_reason(tmp_path: Path) -> None:
    long_s03 = _long_s03_row().with_columns(pl.lit(None, dtype=pl.String).alias("current_entry_missing_reason"))
    frames = {"S02": _long_s02_row(), "S03": long_s03}
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve="long",
        frames=frames,
        leaf_stage="S03",
    )

    with pytest.raises(StageReceiptError, match="LONG S03 violates frozen"):
        verify_stage_receipt_semantics(sources["S03"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_null_long_s04_endpoints_for_available_anchors(
    tmp_path: Path,
) -> None:
    frames = {
        "S02": _long_s02_with_signal_close_row(),
        "S03": _long_s03_available_row(),
        "S04": _long_s04_row(),
    }
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve="long",
        frames=frames,
        leaf_stage="S04",
    )

    with pytest.raises(StageReceiptError, match="S04 anchors/exit levels do not propagate"):
        verify_stage_receipt_semantics(sources["S04"].path, binding_root=tmp_path)


@pytest.mark.parametrize("sleeve", ["continuous", "long"])
def test_s04_rejects_observed_full_horizon_with_false_completeness(
    tmp_path: Path,
    sleeve: str,
) -> None:
    if sleeve == "continuous":
        s04 = _continuous_s04_complete_row().with_columns(
            pl.lit(False).alias("path_1h_complete"),
            pl.lit(None, dtype=pl.Float64).alias("path_1h_underlying_return"),
            pl.lit(None, dtype=pl.Float64).alias("path_1h_short_directional_return"),
            pl.lit("incomplete_path").alias("path_1h_missing_reason"),
            pl.lit(False).alias("path_all_minimal_labels_complete"),
            pl.lit("incomplete_1h_path").alias("missing_path_reason"),
        )
        frames = {
            "S02": _continuous_s02_row(),
            "S03": _continuous_s03_available_row(),
            "S04": s04,
        }
    else:
        s04 = _long_s04_complete_row().with_columns(
            pl.lit(False).alias("common_1h_path_complete"),
            pl.lit("path_incomplete:1/1").alias("common_1h_missing_reason"),
            pl.lit(False).alias("common_label_complete"),
            pl.lit("1h:path_incomplete:1/1").alias("common_missing_path_reason"),
        )
        frames = {
            "S02": _long_s02_with_signal_close_row(),
            "S03": _long_s03_available_row(),
            "S04": s04,
        }
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve=sleeve,
        frames=frames,
        leaf_stage="S04",
    )

    with pytest.raises(StageReceiptError, match="S04"):
        verify_stage_receipt_semantics(sources["S04"].path, binding_root=tmp_path)


@pytest.mark.parametrize("sleeve", ["continuous", "long"])
def test_s04_rejects_counterfeit_horizon_and_overall_reasons(
    tmp_path: Path,
    sleeve: str,
) -> None:
    if sleeve == "continuous":
        s04 = _continuous_s04_row().with_columns(
            pl.lit("counterfeit_horizon").alias("path_1h_missing_reason"),
            pl.lit("counterfeit_overall").alias("missing_path_reason"),
        )
        frames = {"S02": _continuous_s02_row(), "S03": _continuous_s03_row(), "S04": s04}
    else:
        s04 = _long_s04_row().with_columns(
            pl.lit("counterfeit_horizon").alias("current_1h_missing_reason"),
            pl.lit("counterfeit_overall").alias("current_missing_path_reason"),
        )
        frames = {"S02": _long_s02_row(), "S03": _long_s03_row(), "S04": s04}
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        sleeve=sleeve,
        frames=frames,
        leaf_stage="S04",
    )

    with pytest.raises(StageReceiptError, match="S04"):
        verify_stage_receipt_semantics(sources["S04"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_nonmaximal_continuous_data_availability(tmp_path: Path) -> None:
    s02 = _continuous_s02_row().with_columns(pl.lit(0, dtype=pl.Int64).alias("data_available_ts_ms"))
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames={"S02": s02},
        leaf_stage="S02",
    )

    with pytest.raises(StageReceiptError, match="signal/decision/availability timing"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_parent_key_drift(tmp_path: Path) -> None:
    s03 = _continuous_s03_row().with_columns(pl.lit("BBBUSDT").alias("symbol"))
    frames = {"S02": _continuous_s02_row(), "S03": s03}
    _identities, _receipts, sources = _semantic_chain(
        tmp_path,
        frames=frames,
        leaf_stage="S03",
    )

    with pytest.raises(StageReceiptError, match="keys/canonical identity do not exactly equal"):
        verify_stage_receipt_semantics(sources["S03"].path, binding_root=tmp_path)


def test_semantic_verification_rejects_bound_artifact_tampering(tmp_path: Path) -> None:
    _identities, _receipts, sources = _semantic_chain(tmp_path, leaf_stage="S02")
    artifact = tmp_path / "artifacts/continuous-s02.parquet"
    artifact.write_bytes(artifact.read_bytes() + b"tamper")

    with pytest.raises(StageReceiptError, match="current bytes"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)


@pytest.mark.parametrize("drift", ["missing_config", "topology"])
def test_config_parity_surface_fails_closed_on_stage_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    if drift == "missing_config":
        monkeypatch.setitem(
            stage_receipt_module.STAGE_IDENTITY_RECEIPT_KINDS,
            "S03",
            tuple(kind for kind in stage_receipt_module.STAGE_IDENTITY_RECEIPT_KINDS["S03"] if kind != "config"),
        )
        match = "does not require config identity"
    else:
        monkeypatch.setitem(stage_receipt_module.PARENT_STAGES, "S04", ("S03",))
        match = "topology"

    with pytest.raises(StageReceiptError, match=match):
        stage_receipt_module.stage_receipt_config_parity_surface(_config_identity())


@pytest.mark.parametrize(
    ("sleeve", "consumer"),
    [
        ("continuous", "all downstream CONTINUOUS S03/S04 stage receipts"),
        ("long", "all downstream LONG S03/S04 stage receipts"),
    ],
)
def test_config_consumer_parity_surface_reports_mechanical_coverage(
    sleeve: str,
    consumer: str,
) -> None:
    identity = _config_identity(sleeve=sleeve)
    surface = stage_receipt_module.stage_receipt_config_consumer_parity_surface(identity)

    exact = stage_receipt_module.stage_receipt_config_parity_surface(identity)
    assert surface["full_config_and_scope_identity"] == exact["full_config_and_scope_identity"]
    assert surface["validated_targets"] == list(exact)
    assert surface["validated_consumers"]["full_config_and_scope_identity"] == [consumer]
    assert set(surface["validated_consumers"]) == set(exact)
    assert surface["validated_target_fields"]["full_config_and_scope_identity"]
    assert surface["consumer_validator"].endswith("stage_receipt_config_consumer_parity_surface")


def test_semantic_verification_rejects_current_config_factory_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _identities, _receipts, sources = _semantic_chain(tmp_path, leaf_stage="S02")
    counterfeit = copy.deepcopy(_config_identity())
    counterfeit["canonical_config"]["config"]["rmom_quantile"] = 0.99
    counterfeit = _rehash_config_identity(counterfeit)
    monkeypatch.setattr(
        stage_receipt_module,
        "derive_continuous_a0_config_identity",
        lambda: counterfeit,
    )

    with pytest.raises(StageReceiptError, match="repository-derived canonical config identity"):
        verify_stage_receipt_semantics(sources["S02"].path, binding_root=tmp_path)
