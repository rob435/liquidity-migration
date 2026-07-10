"""Strict outcome-blind orchestration for the proposed LONG-A0 S02 tape.

This module is a mechanical boundary around the existing LONG population,
source-context, and identity/PIT adapters.  It deliberately accepts a broad
upstream daily frame but passes only the finite registered daily inputs to
``build_long_feature_tape``.  Caller-provided venue/canonical identity and every
other unregistered column are therefore unable to influence the builder.

The caller must supply a fully verified canonical expected-population object at
exact ``(symbol, signal_ts_ms, symbol_age_days)`` grain. Equality is checked
before the builder and again after the final 138-field projection. The receipt
binds current config/root/PIT/map identities, while its explicit root
completeness/authenticity and upstream PIT-provenance limitations remain.

This remains diagnostic-only.  The source sidecars have strict shape, timing,
formula, and coverage checks in ``attach_long_source_context``, and the runtime
configuration must equal ``_v11a_long_native_config`` through the population
builder's fail-closed check. Their immutable sidecar provenance is not yet
bound, so this artifact is not confirmatory evidence and authorizes no
deployment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Literal, cast

import polars as pl

from . import long_population_scout as long_scout
from . import strategy_overhaul_long_context as long_context
from ._common import MS_PER_DAY, MS_PER_HOUR
from .long_native import LongNativeConfig
from .strategy_overhaul_config_identity import (
    A0ConfigIdentityError,
    LONG_WINDOW_FIELDS,
    JsonValue,
    assert_stage_config_identity_is_current,
    registered_scope_bounds_ms,
)
from .strategy_overhaul_identity_adapter import (
    COMMON_IDENTITY_COLUMNS,
    LONG_FEATURE_KEY_COLUMNS,
    SUPPORTED_VENUES,
    annotate_long_s02_identity,
)
from .strategy_overhaul_expected_population import (
    ExpectedPopulationError,
    VerifiedExpectedPopulation,
    verified_expected_population_s02_inputs,
)
from .strategy_overhaul_long_context import attach_long_source_context
from .strategy_overhaul_phase0 import InstrumentMapEntry
from .strategy_overhaul_projection import (
    artifact_polars_schema,
    project_artifact_frame,
)
from .strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    LONG_SIGNAL_SCHEMA_ID,
    long_schema_runtime_parity_surface,
)


LONG_S02_EVIDENCE_STATUS = (
    "DIAGNOSTIC_ONLY_POPULATION_AND_CONFIG_IDENTITY_BOUND_ROOT_COMPLETENESS_AUTHENTICITY_AND_SIDECAR_PROVENANCE_LIMITED"
)
LONG_S02_DIAGNOSTICS = MappingProxyType(
    {
        "outcome_blind": True,
        "exact_v11a_config_equality_checked": True,
        "expected_population_keys_and_ages_checked": True,
        "sidecar_shape_timing_formula_and_coverage_checked": True,
        "sidecar_provenance_bound": False,
        "config_hash_bound": True,
        "population_receipt_identity_bound": True,
    }
)

EXPECTED_POPULATION_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "symbol_age_days": pl.Int64,
    }
)
HOURLY_BAR_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "ts_ms": pl.Int64,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
    }
)

_FINAL_ARTIFACT = ARTIFACT_SCHEMAS[LONG_SIGNAL_SCHEMA_ID]
_FINAL_SCHEMA = artifact_polars_schema(LONG_SIGNAL_SCHEMA_ID)
_FINAL_COLUMNS = tuple(_FINAL_SCHEMA)
_STRUCTURAL_COLUMNS = tuple(LONG_FEATURE_KEY_COLUMNS)
_IDENTITY_GENERATED_COLUMNS = frozenset(
    {
        "venue",
        "canonical_instrument_id",
        *COMMON_IDENTITY_COLUMNS,
        "symbol_age_source",
    }
)
_PASSTHROUGH_COLUMNS = tuple(field.name for field in _FINAL_ARTIFACT.fields if field.implementation == "passthrough")
_PASSTHROUGH_WITHOUT_SYMBOL = tuple(name for name in _PASSTHROUGH_COLUMNS if name != "symbol")
# The context adapter owns these registered output fields, but it first requires
# the canonical builder values as a narrow parity surface before replacing them
# with unavailable-aware sidecar values.  Keep that source surface explicit
# instead of misclassifying the final fields as passthrough in the registry.
_CONTEXT_PARITY_INPUT_COLUMNS = ("regime_on", "eth_regime_on", "btc_sma_dist")
DAILY_PREBUILDER_SCHEMA = MappingProxyType(
    {
        "symbol": _FINAL_SCHEMA["symbol"],
        "ts_ms": _FINAL_SCHEMA["signal_ts_ms"],
        **{name: _FINAL_SCHEMA[name] for name in _PASSTHROUGH_WITHOUT_SYMBOL},
        **{name: _FINAL_SCHEMA[name] for name in _CONTEXT_PARITY_INPUT_COLUMNS},
    }
)

_BUILDER_COLUMNS = tuple(
    field.name
    for field in _FINAL_ARTIFACT.fields
    if (field.implementation in {"builder", "passthrough"} or field.name in _CONTEXT_PARITY_INPUT_COLUMNS)
    and field.name not in _IDENTITY_GENERATED_COLUMNS
)
_A0_FORCED_NULL_TIER_C_COLUMNS = ("global_lsr", "oi_chg_7d")
_PRE_IDENTITY_COLUMNS = tuple(name for name in _FINAL_COLUMNS if name not in _IDENTITY_GENERATED_COLUMNS)
_ADAPTER_IMPLEMENTATIONS = frozenset({"builder", "passthrough", "projection", "semantic_mismatch"})
_IDENTITY_PAYLOAD_COLUMNS = tuple(
    field.name
    for field in _FINAL_ARTIFACT.fields
    if field.implementation in _ADAPTER_IMPLEMENTATIONS
    and field.name not in _IDENTITY_GENERATED_COLUMNS
    and field.name not in _STRUCTURAL_COLUMNS
)
_ADAPTER_INPUT_COLUMNS = (*_STRUCTURAL_COLUMNS, *_IDENTITY_PAYLOAD_COLUMNS)
_SIDECAR_JOIN_COLUMNS = tuple(name for name in _PRE_IDENTITY_COLUMNS if name not in _ADAPTER_INPUT_COLUMNS)

_OUTCOME_COLUMN_TOKENS = (
    "adverse_magnitude",
    "first_passage",
    "forward_",
    "future_",
    "label_",
    "next_hour",
    "point_return",
    "same_bar_stop_tp",
    "trade_pnl",
    "realized_pnl",
)
_OUTCOME_COLUMN_PREFIXES = (
    "common_entry_",
    "current_entry_",
    "entry_path_",
    "retrace_",
)
_OUTCOME_COLUMN_EXACT = {
    "cost",
    "funding",
    "label",
    "mae",
    "mfe",
    "net_pnl",
    "outcome",
    "pnl",
    "realized_pnl",
}
_HOURLY_PREFIX_OUTCOME_RE = re.compile(
    r"^(?:h[1-6]|[1-6]h)_(?:close|low)(?:$|_)|"
    r"^(?:close|low)_(?:h[1-6]|[1-6]h)(?:$|_)"
)
_HORIZON_OUTCOME_RE = re.compile(
    r"^(?:h\d+|\d+h)_(?:return|point_return|mfe|mae|pnl)(?:$|_)|"
    r"^(?:return|point_return|mfe|mae|pnl)_(?:h\d+|\d+h)(?:$|_)"
)


class LongS02Error(ValueError):
    """A population, source, schema, or exact-projection invariant failed."""


def long_s02_runtime_parity_surface(
    config: LongNativeConfig,
    config_identity: dict[str, JsonValue],
) -> dict[str, object]:
    """Return and enforce the config surface actually consumed by LONG S02."""

    long_scout._require_frozen_v11a_config(config, stage="build_long_s02_feature_tape")
    try:
        assert_stage_config_identity_is_current(config, config_identity, sleeve="long")
        registered_scope_bounds_ms(config_identity)
    except A0ConfigIdentityError as exc:
        raise LongS02Error(f"LONG S02 config identity parity failed: {exc}") from exc

    try:
        from .strategy_overhaul_expected_population import (
            long_expected_population_consumer_parity_surface,
        )

        expected_population_consumer = long_expected_population_consumer_parity_surface(
            config,
            config_identity,
        )
        population_surface = long_scout.long_population_runtime_parity_surface(config)
        context_surface = long_context.long_context_runtime_parity_surface(config)
        schema_surface = long_schema_runtime_parity_surface(config)
    except (TypeError, ValueError) as exc:
        raise LongS02Error(f"LONG S02 consumer parity failed: {exc}") from exc

    def agreed_target(
        target: str,
        primary: dict[str, object] | dict[str, JsonValue],
        *partial_surfaces: dict[str, object],
    ) -> dict[str, JsonValue]:
        value = primary.get(target) if target in primary else primary
        if not isinstance(value, dict):
            raise LongS02Error(f"LONG S02 primary owner surface is malformed for {target}: {value!r}")
        expected = value
        observed_partials: list[object] = []
        for surface in partial_surfaces:
            partial = surface.get(target)
            observed_partials.append(partial)
            if not isinstance(partial, dict) or any(expected.get(name) != item for name, item in partial.items()):
                raise LongS02Error(
                    f"LONG S02 owner-validator parity failed for {target}: "
                    f"expected={expected}, partials={observed_partials}"
                )
        if not expected:
            raise LongS02Error(f"LONG S02 owner-validator parity surface is empty for {target}")
        return cast(dict[str, JsonValue], expected)

    population_and_rolling_windows = agreed_target(
        "population_and_rolling_windows",
        expected_population_consumer,
        context_surface,
        schema_surface,
    )
    regime_context = agreed_target("regime_context", context_surface)
    classifier_and_exit_shape = agreed_target(
        "classifier_and_exit_shape",
        population_surface,
        schema_surface,
    )
    trigger_and_exit_profile = agreed_target(
        "trigger_and_exit_profile",
        population_surface,
        schema_surface,
    )
    forced_null_expected = agreed_target("tier_c_forced_null_gates", schema_surface)
    if _A0_FORCED_NULL_TIER_C_COLUMNS != ("global_lsr", "oi_chg_7d"):
        raise LongS02Error("LONG S02 tier-C forced-null column surface drifted")

    return {
        "consumer_validator": ("liquidity_migration.strategy_overhaul_long_s02.long_s02_runtime_parity_surface"),
        "validated_targets": [
            "full_config_and_scope_identity",
            "population_and_rolling_windows",
            "regime_context",
            "classifier_and_exit_shape",
            "trigger_and_exit_profile",
            "tier_c_forced_null_gates",
        ],
        "validated_target_fields": {
            "full_config_and_scope_identity": [
                "full_config_sha256",
                "registered_scope_sha256",
                "undated_window_fields",
            ],
            "population_and_rolling_windows": list(population_and_rolling_windows),
            "regime_context": list(regime_context),
            "classifier_and_exit_shape": list(classifier_and_exit_shape),
            "trigger_and_exit_profile": list(trigger_and_exit_profile),
            "tier_c_forced_null_gates": list(forced_null_expected),
        },
        "validated_consumers": {
            "full_config_and_scope_identity": [
                "long_population_scout._require_frozen_v11a_config",
                "strategy_overhaul_long_s02.build_long_s02_feature_tape",
            ],
            "population_and_rolling_windows": [],
            "regime_context": [],
            "classifier_and_exit_shape": [],
            "trigger_and_exit_profile": [],
            "tier_c_forced_null_gates": [
                "strategy_overhaul_long_s02._A0_FORCED_NULL_TIER_C_COLUMNS",
            ],
        },
        "full_config_and_scope_identity": {
            "full_config_sha256": config_identity["canonical_config_sha256"],
            "registered_scope_sha256": config_identity["scope_sha256"],
            "undated_window_fields": {name: getattr(config, name) for name in LONG_WINDOW_FIELDS},
        },
        "population_and_rolling_windows": population_and_rolling_windows,
        "regime_context": regime_context,
        "classifier_and_exit_shape": classifier_and_exit_shape,
        "trigger_and_exit_profile": trigger_and_exit_profile,
        "tier_c_forced_null_gates": forced_null_expected,
        "consumer_validators": {
            "strategy_overhaul_expected_population": expected_population_consumer,
            "long_population_scout": population_surface,
            "strategy_overhaul_long_context": context_surface,
            "strategy_overhaul_schemas": schema_surface,
        },
    }


def _is_outcome_like_column(name: str) -> bool:
    lowered = name.lower()
    if lowered in _OUTCOME_COLUMN_EXACT:
        return True
    if lowered.startswith(_OUTCOME_COLUMN_PREFIXES):
        return True
    if any(token in lowered for token in _OUTCOME_COLUMN_TOKENS):
        return True
    if {"mae", "mfe", "outcome", "pnl"} & set(lowered.split("_")):
        return True
    if lowered.endswith(("_mfe", "_mae", "_pnl", "_stop_price", "_take_profit_price")):
        return True
    return bool(_HOURLY_PREFIX_OUTCOME_RE.search(lowered) or _HORIZON_OUTCOME_RE.search(lowered))


def _reject_outcome_columns(frame: pl.DataFrame, *, name: str) -> None:
    forbidden = sorted(column for column in frame.columns if _is_outcome_like_column(column))
    if forbidden:
        raise LongS02Error(f"{name} contains outcome-like columns: {forbidden}")


def _require_exact_schema(
    frame: pl.DataFrame,
    expected: Mapping[str, pl.DataType],
    *,
    name: str,
) -> None:
    expected_names = tuple(expected)
    missing = sorted(set(expected_names) - set(frame.columns))
    unknown = sorted(set(frame.columns) - set(expected_names))
    if missing or unknown or len(frame.columns) != len(expected_names):
        raise LongS02Error(f"{name} projection mismatch; missing={missing}, unknown={unknown}")
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise LongS02Error(f"{name} has invalid dtypes: {mismatched}")


def _require_projectable_schema(
    frame: pl.DataFrame,
    expected: Mapping[str, pl.DataType],
    *,
    name: str,
) -> None:
    """Require every finite projection column while permitting ignored extras."""

    missing = sorted(set(expected) - set(frame.columns))
    if missing:
        raise LongS02Error(f"{name} missing registered prebuilder columns: {missing}")
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise LongS02Error(f"{name} has invalid registered prebuilder dtypes: {mismatched}")


def _strict_select(frame: pl.DataFrame, columns: Sequence[str], *, name: str) -> pl.DataFrame:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise LongS02Error(f"{name} missing required columns: {missing}")
    return frame.select(columns)


def _validate_expected_population(frame: pl.DataFrame) -> pl.DataFrame:
    _reject_outcome_columns(frame, name="expected_population")
    _require_exact_schema(
        frame,
        EXPECTED_POPULATION_SCHEMA,
        name="expected_population",
    )
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
    if not invalid.is_empty():
        raise LongS02Error("expected_population contains invalid symbol/daily-close/current-age values")
    duplicates = frame.group_by(["symbol", "signal_ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise LongS02Error("expected_population contains duplicate (symbol,signal_ts_ms) keys")
    return frame.sort(["symbol", "signal_ts_ms"])


def _assert_frame_in_registered_scope(
    frame: pl.DataFrame,
    *,
    time_column: str,
    config_identity: dict[str, JsonValue],
    lower_bound: Literal["causal_read_start_date_ms", "signal_start_date_ms"],
    name: str,
) -> None:
    bounds = registered_scope_bounds_ms(config_identity)
    lower = bounds[lower_bound]
    upper = bounds["signal_end_date_exclusive_ms"]
    outside = frame.filter((pl.col(time_column) < lower) | (pl.col(time_column) >= upper))
    if not outside.is_empty():
        raise LongS02Error(
            f"{name} falls outside registered scope [{lower}, {upper}): "
            f"{outside.select('symbol', time_column).head(5).to_dicts()}"
        )


def _assert_population_and_ages(
    actual: pl.DataFrame,
    expected: pl.DataFrame,
    *,
    name: str,
) -> None:
    columns = ["symbol", "signal_ts_ms", "symbol_age_days"]
    selected = actual.select(columns).sort(["symbol", "signal_ts_ms"])
    if selected.height == expected.height and selected.equals(expected):
        return
    keys = ["symbol", "signal_ts_ms"]
    missing = expected.select(keys).join(selected.select(keys), on=keys, how="anti")
    unexpected = selected.select(keys).join(expected.select(keys), on=keys, how="anti")
    age_mismatch = (
        selected.join(
            expected.rename({"symbol_age_days": "__expected_symbol_age_days"}),
            on=keys,
            how="inner",
        )
        .filter(pl.col("symbol_age_days") != pl.col("__expected_symbol_age_days"))
        .select(*keys, "symbol_age_days", "__expected_symbol_age_days")
    )
    raise LongS02Error(
        f"{name} does not equal expected_population keys/ages; "
        f"missing={missing.head(5).to_dicts()}, "
        f"unexpected={unexpected.head(5).to_dicts()}, "
        f"age_mismatch={age_mismatch.head(5).to_dicts()}"
    )


def _project_daily_prebuilder(
    daily_features: pl.DataFrame,
    expected_population: pl.DataFrame,
) -> pl.DataFrame:
    _reject_outcome_columns(daily_features, name="daily_features")
    _require_projectable_schema(
        daily_features,
        DAILY_PREBUILDER_SCHEMA,
        name="daily_features",
    )
    if "signal_ts_ms" in daily_features.columns:
        if daily_features.schema["signal_ts_ms"] != pl.Int64:
            raise LongS02Error("daily_features.signal_ts_ms alias must have Int64 dtype")
        alias_mismatch = daily_features.filter(
            pl.col("signal_ts_ms").is_null() | (pl.col("signal_ts_ms") != pl.col("ts_ms"))
        )
        if not alias_mismatch.is_empty():
            raise LongS02Error("daily_features signal_ts_ms alias must equal ts_ms exactly before it is discarded")

    projected = daily_features.select(tuple(DAILY_PREBUILDER_SCHEMA))
    actual_population = projected.select(
        "symbol",
        pl.col("ts_ms").alias("signal_ts_ms"),
        "symbol_age_days",
    )
    _assert_population_and_ages(
        actual_population,
        expected_population,
        name="daily prebuilder population",
    )
    return projected


def _project_hourly_bars(hourly_bars: pl.DataFrame) -> pl.DataFrame:
    _reject_outcome_columns(hourly_bars, name="hourly_bars")
    _require_projectable_schema(hourly_bars, HOURLY_BAR_SCHEMA, name="hourly_bars")
    projected = hourly_bars.select(tuple(HOURLY_BAR_SCHEMA))
    invalid = projected.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("ts_ms").is_null()
        | (pl.col("ts_ms") < 0)
        | ((pl.col("ts_ms") % MS_PER_HOUR) != 0)
    )
    if not invalid.is_empty():
        raise LongS02Error("hourly_bars contains invalid symbol/hourly-open keys")
    return projected


def build_long_s02_feature_tape(
    daily_features: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    *,
    config: LongNativeConfig,
    config_identity: dict[str, JsonValue],
    verified_population: VerifiedExpectedPopulation,
    source_availability: pl.DataFrame,
    regime_context: pl.DataFrame,
    btc_month_context: pl.DataFrame,
    venue: str,
    manifest_pairs: pl.DataFrame,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
) -> pl.DataFrame:
    """Build one exact 138-field, registry-typed LONG S02 diagnostic tape.

    The daily input may be broader than the registered feature set, but only
    :data:`DAILY_PREBUILDER_SCHEMA` is passed to the population builder.
    Caller-provided ``venue`` and ``canonical_instrument_id`` are ignored and
    replaced by the strict identity/PIT adapter.  Outcome-like columns are
    refused even when they would otherwise be outside the finite projection.
    """

    long_s02_runtime_parity_surface(config, config_identity)
    if not isinstance(venue, str) or venue != venue.strip().lower() or venue not in SUPPORTED_VENUES:
        raise LongS02Error(f"venue must be one of {sorted(SUPPORTED_VENUES)}")

    try:
        _verified_source, verified_expected = verified_expected_population_s02_inputs(
            verified_population,
            sleeve="long",
            venue=venue,
            config=config,
            config_identity=config_identity,
            manifest_pairs=manifest_pairs,
            instrument_map=instrument_map,
            instrument_map_version=instrument_map_version,
        )
    except ExpectedPopulationError as exc:
        raise LongS02Error(f"LONG S02 expected-population receipt failed: {exc}") from exc
    expected = _validate_expected_population(verified_expected)
    _assert_frame_in_registered_scope(
        expected,
        time_column="signal_ts_ms",
        config_identity=config_identity,
        lower_bound="signal_start_date_ms",
        name="expected_population",
    )
    excluded = expected.filter(pl.col("symbol").is_in(list(config.exclude_symbols)))
    if not excluded.is_empty():
        raise LongS02Error(
            "expected_population contains canonical config exclusions: "
            f"{excluded.select('symbol', 'signal_ts_ms').head(5).to_dicts()}"
        )
    daily = _project_daily_prebuilder(daily_features, expected)
    hourly = _project_hourly_bars(hourly_bars)
    _assert_frame_in_registered_scope(
        hourly,
        time_column="ts_ms",
        config_identity=config_identity,
        lower_bound="causal_read_start_date_ms",
        name="hourly_bars",
    )
    for name, frame in (
        ("source_availability", source_availability),
        ("regime_context", regime_context),
        ("btc_month_context", btc_month_context),
        ("manifest_pairs", manifest_pairs),
    ):
        _reject_outcome_columns(frame, name=name)

    built = long_scout.build_long_feature_tape(daily, hourly, config)
    # Non-empty row-dictionary reconstruction materializes the ordinal ranks as
    # Int64.  Normalize the typed-empty path to the same explicit context API
    # contract before selecting the finite registered builder projection.
    built = built.with_columns(
        pl.col("today_volume_rank").cast(pl.Int64),
        pl.col("universe_rank").cast(pl.Int64),
    )
    built = _strict_select(
        built,
        _BUILDER_COLUMNS,
        name="registered LONG feature-builder projection",
    )
    built = built.with_columns(
        *(pl.lit(None, dtype=_FINAL_SCHEMA[name]).alias(name) for name in _A0_FORCED_NULL_TIER_C_COLUMNS)
    )
    _assert_population_and_ages(built, expected, name="built LONG S02 population")

    contextual = attach_long_source_context(
        built,
        config=config,
        source_availability=source_availability,
        regime_context=regime_context,
        btc_month_context=btc_month_context,
    )
    contextual = _strict_select(
        contextual,
        _PRE_IDENTITY_COLUMNS,
        name="registered LONG source-context projection",
    )
    _assert_population_and_ages(
        contextual,
        expected,
        name="contextual LONG S02 population",
    )

    adapter_input = _strict_select(
        contextual,
        _ADAPTER_INPUT_COLUMNS,
        name="LONG identity-adapter input projection",
    )
    annotated = annotate_long_s02_identity(
        adapter_input,
        venue=venue,
        manifest_pairs=manifest_pairs,
        instrument_map=instrument_map,
        instrument_map_version=instrument_map_version,
        feature_payload_allowlist=_IDENTITY_PAYLOAD_COLUMNS,
    )

    if _SIDECAR_JOIN_COLUMNS:
        sidecars = contextual.select(
            "symbol",
            "signal_ts_ms",
            *_SIDECAR_JOIN_COLUMNS,
        )
        annotated = annotated.join(
            sidecars,
            on=["symbol", "signal_ts_ms"],
            how="left",
        )

    projected = project_artifact_frame(
        _strict_select(annotated, _FINAL_COLUMNS, name="final LONG S02 projection"),
        LONG_SIGNAL_SCHEMA_ID,
    ).sort(["venue", "symbol", "signal_ts_ms"])
    _assert_population_and_ages(
        projected,
        expected,
        name="projected LONG S02 population",
    )
    return projected


__all__ = [
    "DAILY_PREBUILDER_SCHEMA",
    "EXPECTED_POPULATION_SCHEMA",
    "HOURLY_BAR_SCHEMA",
    "LONG_S02_DIAGNOSTICS",
    "LONG_S02_EVIDENCE_STATUS",
    "LongS02Error",
    "build_long_s02_feature_tape",
    "long_s02_runtime_parity_surface",
]
