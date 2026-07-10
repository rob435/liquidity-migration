"""Strict outcome-blind orchestration for the proposed CONTINUOUS-A0 S02 tape.

This module composes the existing population, context, identity/PIT, and static
diagnostic primitives.  It does not read an entry anchor, a future path, a PnL,
or any other outcome.  The caller must supply an independently inventoried
``expected_source_keys`` frame for every warm-up and signal-window input row,
plus a separate ``expected_population_keys`` frame for the retained signal
window.  Equality between the raw input and source inventory is a hard gate;
the retained population must be an exact subset.  Features and market context
are built on the complete source before warm-up-only rows are removed.

Historical RMOM publication times were never recorded and are not fabricated
here. The population builder's day-start alias is replaced with the conservative
causal-computability timestamp mechanically implied by the frozen shift-3
forward-target construction. Stable rows must be computable by the decision;
provisional rows remain unavailable. This establishes an offline causal boundary,
not operational refresh latency or root-content provenance.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import MappingProxyType
from typing import Literal

import polars as pl

from ._common import MS_PER_HOUR
from . import continuous_population_scout as continuous_scout
from . import strategy_overhaul_context as context_adapter
from . import strategy_overhaul_identity_adapter as identity_adapter
from .continuous_demo import ContinuousDemoCycleConfig
from .strategy_overhaul_config_identity import (
    A0ConfigIdentityError,
    JsonValue,
    assert_stage_config_identity_is_current,
    registered_scope_bounds_ms,
)
from .strategy_overhaul_context import (
    attach_continuous_market_context,
    attach_continuous_static_diagnostics,
)
from .strategy_overhaul_identity_adapter import (
    COMMON_IDENTITY_COLUMNS,
    CONTINUOUS_FEATURE_KEY_COLUMNS,
    SUPPORTED_VENUES,
    annotate_continuous_s02_identity,
)
from .strategy_overhaul_phase0 import InstrumentMapEntry
from .strategy_overhaul_projection import (
    empty_artifact_frame,
    project_artifact_frame,
)
from .strategy_overhaul_rmom_availability import (
    RMOM_CAUSAL_AVAILABILITY_SCHEMA,
    derive_rmom_causal_availability,
)
from .strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    CONTINUOUS_SIGNAL_SCHEMA_ID,
)

CANONICAL_BTC_UPTREND_LOOKBACK_DAYS = 30
CONTINUOUS_S02_EVIDENCE_STATUS = "DIAGNOSTIC_ONLY_ROOT_CONFIG_POPULATION_AND_IDENTITY_RECEIPTS_UNBOUND"

HOURLY_KLINE_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "ts_ms": pl.Int64,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "turnover_quote": pl.Float64,
    }
)
EXPECTED_SOURCE_KEY_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
    }
)
EXPECTED_POPULATION_KEY_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
    }
)

_OUTCOME_COLUMN_TOKENS = (
    "forward_",
    "future_",
    "outcome",
    "label",
    "entry_",
    "path_",
    "first_passage",
    "mfe",
    "mae",
    "pnl",
    "profit",
    "drawdown",
    "target_hit",
    "stop_hit",
)
_RMOM_RANK_METADATA_COLUMNS = (
    "rmom_rank_denominator_count",
    "rmom_tie_method",
    "rmom_rank_denominator_rule",
)
_CONTEXT_COLUMNS = (
    "btc_uptrend_value",
    "btc_uptrend_known",
    "btc_uptrend_pass",
    "btc_uptrend_fail",
    "btc_uptrend_unknown",
    "btc_ret1",
    "btc_ret24",
    "btc_ret168",
    "btc_rv_168h",
    "eth_ret1",
    "eth_ret24",
    "eth_ret168",
    "eth_rv_168h",
    "alt_breadth_ret24_positive",
    "alt_breadth_ret1_ge_3pct",
    "xs_ret1_dispersion",
    "alt_breadth_ret24_positive_peer_count",
    "alt_breadth_ret24_positive_missing_peer_count",
    "alt_breadth_ret24_positive_denominator_count",
    "alt_breadth_ret1_ge_3pct_peer_count",
    "alt_breadth_ret1_ge_3pct_missing_peer_count",
    "alt_breadth_ret1_ge_3pct_denominator_count",
    "xs_ret1_dispersion_peer_count",
    "xs_ret1_dispersion_missing_peer_count",
    "xs_ret1_dispersion_denominator_count",
)
_STATIC_DIAGNOSTIC_COLUMNS = tuple(
    column
    for component in ("p3", "p4p3", "p4p5")
    for column in (
        f"{component}_static_candidate",
        f"{component}_static_first_rejection_reason",
    )
)
_IDENTITY_GENERATED_COLUMNS = frozenset(
    {
        "venue",
        "canonical_instrument_id",
        *COMMON_IDENTITY_COLUMNS,
        "current_age_source",
        "current_age_source_available",
        "current_age_240_pass",
    }
)
_FINAL_SCHEMA = ARTIFACT_SCHEMAS[CONTINUOUS_SIGNAL_SCHEMA_ID]
_FINAL_COLUMNS = tuple(field.name for field in _FINAL_SCHEMA.fields)
_FINAL_COLUMN_SET = frozenset(_FINAL_COLUMNS)
_STRUCTURAL_COLUMNS = tuple(CONTINUOUS_FEATURE_KEY_COLUMNS)
_PRE_IDENTITY_COLUMNS = tuple(
    name
    for name in _FINAL_COLUMNS
    if name not in _IDENTITY_GENERATED_COLUMNS
    and name not in _CONTEXT_COLUMNS
    and name not in _STATIC_DIAGNOSTIC_COLUMNS
)
_ADAPTER_IMPLEMENTATIONS = frozenset({"builder", "passthrough", "projection", "semantic_mismatch"})
_IDENTITY_PAYLOAD_COLUMNS = tuple(
    field.name
    for field in _FINAL_SCHEMA.fields
    if field.implementation in _ADAPTER_IMPLEMENTATIONS
    and field.name not in _IDENTITY_GENERATED_COLUMNS
    and field.name not in _STRUCTURAL_COLUMNS
    and field.name not in _STATIC_DIAGNOSTIC_COLUMNS
)
_PRE_STATIC_COLUMNS = tuple(name for name in _FINAL_COLUMNS if name not in _STATIC_DIAGNOSTIC_COLUMNS)


class ContinuousS02Error(ValueError):
    """A population, source, timing, or exact-projection invariant failed."""


def continuous_s02_runtime_parity_surface(
    config: ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
) -> dict[str, dict[str, JsonValue]]:
    """Return the config-derived surface actually consumed by CONTINUOUS S02.

    This is also the runtime guard used by the builder.  Expected values come
    from the canonical identity while observed values come from the live module
    globals that the population/context/identity functions dereference.  A
    monkeypatched literal or a re-signed non-canonical identity therefore fails
    before any source frame is processed.
    """

    try:
        assert_stage_config_identity_is_current(config, config_identity, sleeve="continuous")
        registered_scope_bounds_ms(config_identity)
    except A0ConfigIdentityError as exc:
        raise ContinuousS02Error(f"CONTINUOUS S02 config identity parity failed: {exc}") from exc

    component_artifact = config_identity.get("component_config")
    if not isinstance(component_artifact, dict) or not isinstance(component_artifact.get("components"), list):
        raise ContinuousS02Error("CONTINUOUS S02 identity lacks canonical component rows")
    component_rows = component_artifact["components"]
    if any(not isinstance(row, dict) for row in component_rows):
        raise ContinuousS02Error("CONTINUOUS S02 component rows must be objects")
    typed_rows = [row for row in component_rows if isinstance(row, dict)]

    selection: dict[str, JsonValue] = {
        "strategy_profile": continuous_scout.CURRENT_STRATEGY_PROFILE,
        "side": continuous_scout.CURRENT_SIDE,
        "decile": continuous_scout.CURRENT_SELECTION_DECILE,
        "feature_set": list(continuous_scout.CURRENT_FEATURE_SET),
        "rmom_quantile": continuous_scout.CURRENT_RMOM_QUANTILE,
        "liq_turnover_min": continuous_scout.CURRENT_LIQUIDITY_FLOOR,
    }
    expected_selection = {
        "strategy_profile": config.strategy_profile,
        "side": config.side,
        "decile": config.decile,
        "feature_set": list(config.feature_set),
        "rmom_quantile": config.rmom_quantile,
        "liq_turnover_min": config.liq_turnover_min,
    }
    if selection != expected_selection:
        raise ContinuousS02Error(
            f"CONTINUOUS S02 selection-profile parity failed: expected={expected_selection}, observed={selection}"
        )

    decision_gate: dict[str, JsonValue] = {
        "entry_confirm_delay_hours": continuous_scout.CURRENT_ENTRY_CONFIRM_DELAY_HOURS,
        "btc_trend_gate": context_adapter.CONTINUOUS_BTC_TREND_GATE,
        "btc_trend_lookback_days": CANONICAL_BTC_UPTREND_LOOKBACK_DAYS,
        "btc_trend_mode": context_adapter.CONTINUOUS_BTC_TREND_MODE,
    }
    expected_decision_gate = {
        "entry_confirm_delay_hours": config.entry_confirm_delay_hours,
        "btc_trend_gate": config.btc_trend_gate,
        "btc_trend_lookback_days": config.btc_trend_lookback_days,
        "btc_trend_mode": config.btc_trend_mode,
    }
    if (
        context_adapter.CONTINUOUS_BTC_TREND_LOOKBACK_DAYS != CANONICAL_BTC_UPTREND_LOOKBACK_DAYS
        or decision_gate != expected_decision_gate
    ):
        raise ContinuousS02Error(
            "CONTINUOUS S02 decision/BTC-gate parity failed: "
            f"expected={expected_decision_gate}, observed={decision_gate}, "
            f"context_lookback={context_adapter.CONTINUOUS_BTC_TREND_LOOKBACK_DAYS}"
        )

    component_identity: dict[str, JsonValue] = {
        "component_order": list(continuous_scout.COMPONENT_ORDER),
        "component_trigger_by_name": dict(continuous_scout.COMPONENT_TRIGGERS),
        "component_age_days_min_by_name": dict(continuous_scout.COMPONENT_AGE_DAYS_MIN),
        "component_bit_by_name": dict(continuous_scout.COMPONENT_BITS),
        "component_weight_by_name": dict(continuous_scout.COMPONENT_WEIGHTS),
    }
    expected_components: dict[str, JsonValue] = {
        "component_order": [str(row["component"]) for row in typed_rows],
        "component_trigger_by_name": {
            str(row["component"]): row["entry_event_trigger"] for row in typed_rows
        },
        "component_age_days_min_by_name": {str(row["component"]): row["age_days_min"] for row in typed_rows},
        "component_bit_by_name": {str(row["component"]): row["component_bit"] for row in typed_rows},
        "component_weight_by_name": {str(row["component"]): row["weight"] for row in typed_rows},
    }
    duplicate_component_surfaces_match = bool(
        tuple(context_adapter.CONTINUOUS_STATIC_COMPONENT_ORDER) == tuple(continuous_scout.COMPONENT_ORDER)
        and dict(context_adapter.CONTINUOUS_STATIC_COMPONENT_TRIGGER_BY_NAME)
        == dict(continuous_scout.COMPONENT_TRIGGERS)
        and context_adapter.CONTINUOUS_STATIC_COMPONENT_AGE_DAYS_MIN
        == identity_adapter.CONTINUOUS_CURRENT_AGE_DAYS_MIN
        and set(continuous_scout.COMPONENT_AGE_DAYS_MIN.values())
        == {identity_adapter.CONTINUOUS_CURRENT_AGE_DAYS_MIN}
    )
    if component_identity != expected_components or not duplicate_component_surfaces_match:
        raise ContinuousS02Error(
            "CONTINUOUS S02 component parity failed: "
            f"expected={expected_components}, observed={component_identity}, "
            f"duplicate_surfaces_match={duplicate_component_surfaces_match}"
        )

    return {
        "full_config_and_scope_identity": {
            "full_config_sha256": config_identity["canonical_config_sha256"],
            "registered_scope_sha256": config_identity["scope_sha256"],
            "component_config_sha256": config_identity["component_config_sha256"],
        },
        "selection_profile": selection,
        "decision_and_btc_gate": decision_gate,
        "component_identity": component_identity,
    }


def _reject_outcome_columns(frame: pl.DataFrame, *, name: str) -> None:
    forbidden = sorted(
        column for column in frame.columns if any(token in column.lower() for token in _OUTCOME_COLUMN_TOKENS)
    )
    if forbidden:
        raise ContinuousS02Error(f"{name} contains outcome-like columns: {forbidden}")


def _require_exact_schema(
    frame: pl.DataFrame,
    expected: MappingProxyType[str, pl.DataType] | dict[str, pl.DataType],
    *,
    name: str,
) -> None:
    expected_names = tuple(expected)
    missing = sorted(set(expected_names) - set(frame.columns))
    unknown = sorted(set(frame.columns) - set(expected_names))
    if missing or unknown or len(frame.columns) != len(expected_names):
        raise ContinuousS02Error(f"{name} projection mismatch; missing={missing}, unknown={unknown}")
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise ContinuousS02Error(f"{name} has invalid dtypes: {mismatched}")


def _validate_expected_keys(frame: pl.DataFrame, *, name: str) -> pl.DataFrame:
    _reject_outcome_columns(frame, name=name)
    _require_exact_schema(
        frame,
        EXPECTED_SOURCE_KEY_SCHEMA,
        name=name,
    )
    invalid = frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("signal_ts_ms").is_null()
        | (pl.col("signal_ts_ms") < 0)
        | ((pl.col("signal_ts_ms") % MS_PER_HOUR) != 0)
    )
    if not invalid.is_empty():
        raise ContinuousS02Error(f"{name} contains invalid symbol/hour keys")
    duplicates = frame.group_by(["symbol", "signal_ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ContinuousS02Error(f"{name} contains duplicate (symbol,signal_ts_ms) keys")
    return frame.sort(["symbol", "signal_ts_ms"])


def _assert_keys_in_registered_scope(
    frame: pl.DataFrame,
    *,
    config_identity: dict[str, JsonValue],
    lower_bound: Literal["causal_read_start_date_ms", "signal_start_date_ms"],
    name: str,
) -> None:
    bounds = registered_scope_bounds_ms(config_identity)
    lower = bounds[lower_bound]
    upper = bounds["signal_end_date_exclusive_ms"]
    outside = frame.filter((pl.col("signal_ts_ms") < lower) | (pl.col("signal_ts_ms") >= upper))
    if not outside.is_empty():
        raise ContinuousS02Error(
            f"{name} falls outside registered scope [{lower}, {upper}): "
            f"{outside.select('symbol', 'signal_ts_ms').head(5).to_dicts()}"
        )


def _assert_same_population_keys(
    actual: pl.DataFrame,
    expected: pl.DataFrame,
    *,
    name: str,
    expected_name: str,
) -> None:
    keys = ["symbol", "signal_ts_ms"]
    missing_from_actual = expected.join(actual, on=keys, how="anti")
    unexpected_actual = actual.join(expected, on=keys, how="anti")
    if actual.height != expected.height or not missing_from_actual.is_empty() or not unexpected_actual.is_empty():
        raise ContinuousS02Error(
            f"{name} does not equal {expected_name}; "
            f"missing_input={missing_from_actual.head(5).to_dicts()}, "
            f"unexpected_input={unexpected_actual.head(5).to_dicts()}"
        )


def _validate_hourly_klines(
    hourly_klines: pl.DataFrame,
    expected_source_keys: pl.DataFrame,
) -> None:
    _reject_outcome_columns(hourly_klines, name="hourly_klines")
    _require_exact_schema(hourly_klines, HOURLY_KLINE_SCHEMA, name="hourly_klines")
    actual = hourly_klines.select(
        "symbol",
        pl.col("ts_ms").alias("signal_ts_ms"),
    )
    duplicates = actual.group_by(["symbol", "signal_ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ContinuousS02Error("hourly_klines contains duplicate (symbol,ts_ms) keys")
    _assert_same_population_keys(
        actual,
        expected_source_keys,
        name="hourly_klines keys",
        expected_name="expected_source_keys",
    )


def _require_population_subset(
    expected_population_keys: pl.DataFrame,
    expected_source_keys: pl.DataFrame,
) -> None:
    keys = ["symbol", "signal_ts_ms"]
    outside_source = expected_population_keys.join(expected_source_keys, on=keys, how="anti")
    if not outside_source.is_empty():
        raise ContinuousS02Error(
            "expected_population_keys must be a subset of expected_source_keys; "
            f"outside_source={outside_source.head(5).to_dicts()}"
        )


def _validate_stable_rmom(stable_rmom: pl.DataFrame | None) -> None:
    if stable_rmom is None:
        return
    _reject_outcome_columns(stable_rmom, name="stable_rmom")
    time_columns = [column for column in ("ts_ms", "day_ts") if column in stable_rmom.columns]
    if len(time_columns) != 1:
        raise ContinuousS02Error("stable_rmom must contain exactly one of ts_ms or day_ts")
    expected: dict[str, pl.DataType] = {
        "symbol": pl.String,
        time_columns[0]: pl.Int64,
        "residual_momentum": pl.Float64,
        "is_provisional": pl.Boolean,
    }
    _require_exact_schema(stable_rmom, expected, name="stable_rmom")


def _apply_causal_rmom_availability(population: pl.DataFrame) -> pl.DataFrame:
    source_provenance = (
        population.filter(pl.col("rmom_source_row_present"))
        .select(
            "symbol",
            pl.col("rmom_source_day_ts_ms").alias("day_ts"),
            pl.col("rmom_is_provisional").alias("is_provisional"),
        )
        .unique()
        .sort(["symbol", "day_ts"])
    )
    availability = derive_rmom_causal_availability(source_provenance).frame

    joined = population.drop(
        "rmom_data_available_ts_ms",
        "data_available_ts_ms",
    ).join(
        availability.rename(
            {
                "day_ts": "rmom_source_day_ts_ms",
                "rmom_data_available_ts_ms": "__causal_rmom_available_ts_ms",
            }
        ),
        on=["symbol", "rmom_source_day_ts_ms"],
        how="left",
    )
    timely = pl.col("__causal_rmom_available_ts_ms").is_not_null() & (
        pl.col("__causal_rmom_available_ts_ms") <= pl.col("decision_ts_ms")
    )
    invalid_stable = joined.filter(pl.col("rmom_stable_available") & ~timely.fill_null(False))
    if not invalid_stable.is_empty():
        raise ContinuousS02Error(
            "stable/rankable RMOM is not causally computable by the decision: "
            f"{invalid_stable.select('symbol', 'signal_ts_ms', 'decision_ts_ms', 'rmom_source_day_ts_ms', '__causal_rmom_available_ts_ms').head(5).to_dicts()}"
        )

    stable_and_timely = pl.col("rmom_stable_available") & timely
    joined = (
        joined.with_columns(
            pl.when(stable_and_timely)
            .then(pl.col("__causal_rmom_available_ts_ms"))
            .otherwise(None)
            .cast(pl.Int64)
            .alias("rmom_data_available_ts_ms")
        )
        .with_columns(
            pl.when(pl.col("rmom_data_available_ts_ms").is_not_null())
            .then(pl.max_horizontal("feature_data_available_ts_ms", "rmom_data_available_ts_ms"))
            .otherwise(pl.col("feature_data_available_ts_ms"))
            .cast(pl.Int64)
            .alias("data_available_ts_ms")
        )
        .drop("__causal_rmom_available_ts_ms")
    )
    invalid_availability = joined.filter(pl.col("data_available_ts_ms") > pl.col("decision_ts_ms"))
    if not invalid_availability.is_empty():  # pragma: no cover - guarded above and by the builder
        raise ContinuousS02Error("recomputed S02 data availability exceeds decision time")
    return joined


def _attach_rmom_rank_metadata(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.col("rmom_rankable_peer_count").cast(pl.Int64).alias("rmom_rank_denominator_count"),
        pl.lit("average", dtype=pl.String).alias("rmom_tie_method"),
        pl.lit("rankable_peers_minus_one_clamped_1", dtype=pl.String).alias("rmom_rank_denominator_rule"),
    )


def _strict_select(frame: pl.DataFrame, columns: Sequence[str], *, name: str) -> pl.DataFrame:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ContinuousS02Error(f"{name} missing required columns: {missing}")
    return frame.select(columns)


def build_continuous_s02_feature_tape(
    hourly_klines: pl.DataFrame,
    *,
    config: ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
    stable_rmom: pl.DataFrame | None,
    expected_source_keys: pl.DataFrame,
    expected_population_keys: pl.DataFrame,
    venue: str,
    manifest_pairs: pl.DataFrame,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
    current_age_source: Literal["reported_launch_time_ms", "root_first_bar_ts_ms"],
    btc_uptrend_lookback_days: int,
) -> pl.DataFrame:
    """Build one exact 196-field, registry-typed CONTINUOUS S02 artifact.

    ``expected_source_keys`` must independently inventory every supplied
    warm-up and signal row. ``expected_population_keys`` independently defines
    the retained signal window and must be its exact subset.  Both frames use
    exactly ``(symbol, signal_ts_ms)``.  The function is diagnostic-only while
    the authoritative root/config/population receipts remain unbound.
    """

    continuous_s02_runtime_parity_surface(config, config_identity)
    if venue not in SUPPORTED_VENUES or venue != venue.strip().lower():
        raise ContinuousS02Error(f"venue must be one of {sorted(SUPPORTED_VENUES)}")
    if btc_uptrend_lookback_days != CANONICAL_BTC_UPTREND_LOOKBACK_DAYS:
        raise ContinuousS02Error("btc_uptrend_lookback_days must equal the canonical registered value 30")

    source_keys = _validate_expected_keys(expected_source_keys, name="expected_source_keys")
    expected_keys = _validate_expected_keys(
        expected_population_keys,
        name="expected_population_keys",
    )
    _assert_keys_in_registered_scope(
        source_keys,
        config_identity=config_identity,
        lower_bound="causal_read_start_date_ms",
        name="expected_source_keys",
    )
    _assert_keys_in_registered_scope(
        expected_keys,
        config_identity=config_identity,
        lower_bound="signal_start_date_ms",
        name="expected_population_keys",
    )
    excluded = expected_keys.filter(pl.col("symbol").is_in(list(config.exclude_symbols)))
    if not excluded.is_empty():
        raise ContinuousS02Error(
            "expected_population_keys contains canonical config exclusions: "
            f"{excluded.select('symbol', 'signal_ts_ms').head(5).to_dicts()}"
        )
    _require_population_subset(expected_keys, source_keys)
    _validate_hourly_klines(hourly_klines, source_keys)
    _validate_stable_rmom(stable_rmom)
    _reject_outcome_columns(manifest_pairs, name="manifest_pairs")

    builder_input = hourly_klines.with_columns(pl.lit(venue, dtype=pl.String).alias("venue"))
    population = continuous_scout.build_continuous_feature_tape(builder_input, stable_rmom)
    if population.is_empty():
        population = empty_artifact_frame(CONTINUOUS_SIGNAL_SCHEMA_ID).select(_PRE_IDENTITY_COLUMNS)
    population = _attach_rmom_rank_metadata(population)
    population = _strict_select(population, _PRE_IDENTITY_COLUMNS, name="population builder projection")
    built_keys = population.select("symbol", "signal_ts_ms").sort(["symbol", "signal_ts_ms"])
    _assert_same_population_keys(
        built_keys,
        source_keys,
        name="built source keys",
        expected_name="expected_source_keys",
    )

    contextual = attach_continuous_market_context(
        population,
        btc_trend_lookback_days=CANONICAL_BTC_UPTREND_LOOKBACK_DAYS,
    )
    contextual = _strict_select(
        contextual,
        (*_PRE_IDENTITY_COLUMNS, *_CONTEXT_COLUMNS),
        name="market-context projection",
    )
    contextual = contextual.join(
        expected_keys,
        on=["symbol", "signal_ts_ms"],
        how="semi",
    )
    retained_keys = contextual.select("symbol", "signal_ts_ms").sort(["symbol", "signal_ts_ms"])
    _assert_same_population_keys(
        retained_keys,
        expected_keys,
        name="retained S02 keys",
        expected_name="expected_population_keys",
    )
    contextual = _apply_causal_rmom_availability(contextual)

    adapter_input_columns = (*_STRUCTURAL_COLUMNS, *_IDENTITY_PAYLOAD_COLUMNS)
    adapter_input = _strict_select(
        contextual,
        adapter_input_columns,
        name="identity-adapter input projection",
    )
    annotated = annotate_continuous_s02_identity(
        adapter_input,
        venue=venue,
        manifest_pairs=manifest_pairs,
        instrument_map=instrument_map,
        instrument_map_version=instrument_map_version,
        feature_payload_allowlist=_IDENTITY_PAYLOAD_COLUMNS,
        current_age_source=current_age_source,
    )

    sidecar_columns = tuple(
        name for name in (*_PRE_IDENTITY_COLUMNS, *_CONTEXT_COLUMNS) if name not in adapter_input_columns
    )
    if sidecar_columns:
        sidecar = contextual.select("symbol", "signal_ts_ms", *sidecar_columns)
        annotated = annotated.join(
            sidecar,
            on=["symbol", "signal_ts_ms"],
            how="left",
        )
    annotated = _strict_select(annotated, _PRE_STATIC_COLUMNS, name="pre-static S02 projection")
    static = attach_continuous_static_diagnostics(annotated)
    if set(static.columns) != _FINAL_COLUMN_SET or len(static.columns) != len(_FINAL_COLUMNS):
        missing = sorted(_FINAL_COLUMN_SET - set(static.columns))
        unknown = sorted(set(static.columns) - _FINAL_COLUMN_SET)
        raise ContinuousS02Error(f"final S02 projection mismatch; missing={missing}, unknown={unknown}")
    projected = project_artifact_frame(
        static.select(_FINAL_COLUMNS),
        CONTINUOUS_SIGNAL_SCHEMA_ID,
    ).sort(["venue", "symbol", "decision_ts_ms"])
    projected_keys = projected.select("symbol", "signal_ts_ms")
    _assert_same_population_keys(
        projected_keys,
        expected_keys,
        name="projected S02 keys",
        expected_name="expected_population_keys",
    )
    return projected


__all__ = [
    "CANONICAL_BTC_UPTREND_LOOKBACK_DAYS",
    "CONTINUOUS_S02_EVIDENCE_STATUS",
    "ContinuousS02Error",
    "EXPECTED_POPULATION_KEY_SCHEMA",
    "EXPECTED_SOURCE_KEY_SCHEMA",
    "HOURLY_KLINE_SCHEMA",
    "RMOM_CAUSAL_AVAILABILITY_SCHEMA",
    "build_continuous_s02_feature_tape",
    "continuous_s02_runtime_parity_surface",
]
