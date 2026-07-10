"""Strict outcome-blind source context for a canonical LONG-A0 S02 tape.

The adapter consumes the output of :func:`build_long_feature_tape`; it does not
load a root, derive a publication timestamp, or assert that the supplied
cross-section is the complete PIT universe.  Source availability and market
context therefore arrive as exact, caller-owned sidecars.  Their shape,
causality, formulas, and key coverage are checked here, but their upstream
provenance is not established here.

``daily_bar_available_ts_ms`` is required for every row.  The aggregate
``signal_feature_available_ts_ms`` is also required and must equal the maximum
of the non-null declared daily, BTC, ETH, and BTC-month availability timestamps.
This makes the sidecar's aggregation semantics reconstructable without claiming
that the caller's source timestamps are authoritative.

Production LONG ranks are ordinal-descending.  The adapter reconstructs both
registered ranks over every row supplied for a signal timestamp and requires
exact parity with the builder output before emitting the twelve registered
population/tie diagnostics.  Ties retain the canonical builder order
(``signal_ts_ms``, then ``symbol``); ordinal ranks deliberately assign distinct
ranks to tied rows.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from types import MappingProxyType

import polars as pl

from ._common import MS_PER_DAY
from .long_native import LongNativeConfig
from .strategy_overhaul_schemas import ARTIFACT_SCHEMAS, LONG_SIGNAL_SCHEMA_ID


LONG_SOURCE_CONTEXT_EVIDENCE_STATUS = "DIAGNOSTIC_ONLY_SIDECAR_PROVENANCE_AND_POPULATION_COMPLETENESS_UNVERIFIED"
LONG_CONTEXT_REGIME_SYMBOL = "BTCUSDT"
LONG_CONTEXT_REGIME_SMA_DAYS = 30
LONG_CONTEXT_BTC_MONTH_REGIME_GATE = "off"
LONG_CONTEXT_UNIVERSE_VOLUME_WINDOW_DAYS = 90
LONG_SOURCE_CONTEXT_DIAGNOSTICS = MappingProxyType(
    {
        "outcome_blind": True,
        "sidecar_shape_causality_and_declared_max_checked": True,
        "rank_parity_checked_over_supplied_rows": True,
        "sidecar_provenance_verified": False,
        "pit_population_completeness_verified": False,
    }
)

SOURCE_AVAILABILITY_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "signal_feature_available_ts_ms": pl.Int64,
        "daily_bar_available_ts_ms": pl.Int64,
        "btc_context_available_ts_ms": pl.Int64,
        "eth_context_available_ts_ms": pl.Int64,
        "btc_month_context_available_ts_ms": pl.Int64,
    }
)

# These raw values are sidecar evidence used to validate the registered
# distance/pass fields.  They are intentionally not added to S02 because the
# current registered schema does not contain raw BTC/ETH close or SMA columns.
REGIME_CONTEXT_SCHEMA = MappingProxyType(
    {
        "signal_ts_ms": pl.Int64,
        "btc_close": pl.Float64,
        f"btc_sma_{LONG_CONTEXT_REGIME_SMA_DAYS}d": pl.Float64,
        "btc_sma_dist": pl.Float64,
        "btc_regime_available": pl.Boolean,
        "btc_regime_pass": pl.Boolean,
        "eth_close": pl.Float64,
        f"eth_sma_{LONG_CONTEXT_REGIME_SMA_DAYS}d": pl.Float64,
        "eth_sma_dist": pl.Float64,
        "eth_regime_available": pl.Boolean,
        "eth_regime_pass": pl.Boolean,
    }
)

BTC_MONTH_CONTEXT_SCHEMA = MappingProxyType(
    {
        "signal_ts_ms": pl.Int64,
        "btc_month_regime_value": pl.Float64,
        "btc_month_regime_available": pl.Boolean,
        "btc_month_regime_pass": pl.Boolean,
    }
)

RANK_METADATA_COLUMNS = tuple(
    f"{prefix}_{suffix}"
    for prefix in ("today_volume_rank", "universe_rank")
    for suffix in (
        "population_peer_count",
        "rankable_peer_count",
        "missing_peer_count",
        "tie_count",
        "tie_method",
        "denominator_rule",
    )
)

_KEY_COLUMNS = ("symbol", "signal_ts_ms")
_AVAILABILITY_COLUMNS = tuple(SOURCE_AVAILABILITY_SCHEMA)[2:]
_SCHEMA_VERSION = ARTIFACT_SCHEMAS[LONG_SIGNAL_SCHEMA_ID].schema_version
_REQUIRED_FEATURE_BASE_DTYPES = MappingProxyType(
    {
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "turnover_quote": pl.Float64,
        # build_long_feature_tape reconstructs row dictionaries and therefore
        # currently materializes source ranks as Int64.  Exact parity is checked
        # before the adapter projects the registered UInt32 dtype.
        "today_volume_rank": pl.Int64,
        "universe_rank": pl.Int64,
        "symbol_age_days": pl.Int64,
        "in_universe": pl.Boolean,
        "regime_on": pl.Boolean,
        "eth_regime_on": pl.Boolean,
        "btc_sma_dist": pl.Float64,
        "gate_btc_month_regime": pl.Boolean,
        "long_feature_tape_schema_version": pl.String,
    }
)
_SIDECAR_OWNED_COLUMNS = frozenset(
    {
        *_AVAILABILITY_COLUMNS,
        "btc_regime_available",
        "eth_regime_available",
        "eth_sma_dist",
        "btc_month_regime_value",
        "btc_month_regime_available",
        "btc_month_regime_pass",
        *RANK_METADATA_COLUMNS,
    }
)
_TIE_METHOD = "ordinal_descending_value_then_symbol_ascending"
_DENOMINATOR_RULE = "supplied_signal_ts_population"
_ROW_ORDER = "__long_context_row_order"
_FLOAT_REL_TOLERANCE = 1e-12
_FLOAT_ABS_TOLERANCE = 1e-12

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


class LongSourceContextError(ValueError):
    """A source-context shape, timing, parity, or cardinality invariant failed."""


def _required_feature_dtypes(config: LongNativeConfig) -> dict[str, pl.DataType]:
    return {
        **_REQUIRED_FEATURE_BASE_DTYPES,
        f"turnover_median_{config.universe_volume_window_days}d": pl.Float64,
    }


def _rank_specs(config: LongNativeConfig) -> tuple[tuple[str, str], ...]:
    return (
        ("today_volume_rank", "turnover_quote"),
        ("universe_rank", f"turnover_median_{config.universe_volume_window_days}d"),
    )


def long_context_runtime_parity_surface(config: LongNativeConfig) -> dict[str, object]:
    """Validate and expose config-sensitive LONG context consumers."""

    if not isinstance(config, LongNativeConfig):
        raise TypeError("long_context_runtime_parity_surface requires LongNativeConfig")
    expected_constants = {
        "regime_symbol": config.regime_symbol,
        "regime_sma_days": config.regime_sma_days,
        "btc_month_regime_gate": config.btc_month_regime_gate,
        "universe_volume_window_days": config.universe_volume_window_days,
    }
    observed_constants = {
        "regime_symbol": LONG_CONTEXT_REGIME_SYMBOL,
        "regime_sma_days": LONG_CONTEXT_REGIME_SMA_DAYS,
        "btc_month_regime_gate": LONG_CONTEXT_BTC_MONTH_REGIME_GATE,
        "universe_volume_window_days": LONG_CONTEXT_UNIVERSE_VOLUME_WINDOW_DAYS,
    }
    if observed_constants != expected_constants:
        raise LongSourceContextError(
            f"LONG context config parity failed: expected={expected_constants}, observed={observed_constants}"
        )

    expected_regime_schema = {
        "signal_ts_ms": pl.Int64,
        "btc_close": pl.Float64,
        f"btc_sma_{config.regime_sma_days}d": pl.Float64,
        "btc_sma_dist": pl.Float64,
        "btc_regime_available": pl.Boolean,
        "btc_regime_pass": pl.Boolean,
        "eth_close": pl.Float64,
        f"eth_sma_{config.regime_sma_days}d": pl.Float64,
        "eth_sma_dist": pl.Float64,
        "eth_regime_available": pl.Boolean,
        "eth_regime_pass": pl.Boolean,
    }
    if dict(REGIME_CONTEXT_SCHEMA) != expected_regime_schema:
        raise LongSourceContextError("LONG context regime schema does not match config-derived SMA fields")
    expected_rank_specs = (
        ("today_volume_rank", "turnover_quote"),
        (
            "universe_rank",
            f"turnover_median_{config.universe_volume_window_days}d",
        ),
    )
    if _rank_specs(config) != expected_rank_specs:
        raise LongSourceContextError("LONG context rank specs do not match the configured rolling window")
    if (
        _TIE_METHOD != "ordinal_descending_value_then_symbol_ascending"
        or _DENOMINATOR_RULE != "supplied_signal_ts_population"
    ):
        raise LongSourceContextError("LONG context rank metadata literals drifted")

    population_and_rolling_windows = {
        "universe_size": config.universe_size,
        "universe_volume_window_days": config.universe_volume_window_days,
        "min_listing_history_days": config.min_listing_history_days,
    }
    regime_context = {
        "regime_symbol": config.regime_symbol,
        "regime_sma_days": config.regime_sma_days,
        "btc_month_regime_gate": config.btc_month_regime_gate,
    }
    return {
        "consumer_validator": (
            "liquidity_migration.strategy_overhaul_long_context.long_context_runtime_parity_surface"
        ),
        "validated_targets": ["population_and_rolling_windows", "regime_context"],
        "validated_target_fields": {
            "population_and_rolling_windows": [
                "universe_size",
                "universe_volume_window_days",
                "min_listing_history_days",
            ],
            "regime_context": [
                "regime_symbol",
                "regime_sma_days",
                "btc_month_regime_gate",
            ],
        },
        "validated_consumers": {
            "population_and_rolling_windows": ["strategy_overhaul_long_context universe-rank reconstruction"],
            "regime_context": [
                "strategy_overhaul_long_context.REGIME_CONTEXT_SCHEMA *_sma_30d fields",
                "strategy_overhaul_long_context._validate_regime_context",
                "strategy_overhaul_long_context._require_feature_columns BTC-month gate-off assumption",
                "strategy_overhaul_long_context._validate_btc_month_context gate-off pass semantics",
            ],
        },
        "population_and_rolling_windows": population_and_rolling_windows,
        "regime_context": regime_context,
        "rank_specs": [list(spec) for spec in expected_rank_specs],
        "rank_tie_method": _TIE_METHOD,
        "rank_denominator_rule": _DENOMINATOR_RULE,
        "scope_limitation": (
            "registered research/S02 membership and steady-state demo parity only; "
            "the demo latest-cycle cold-start fallback is a separate runtime path"
        ),
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
        raise LongSourceContextError(f"{name} contains outcome-like columns: {forbidden}")


def _require_exact_schema(
    frame: pl.DataFrame,
    expected: Mapping[str, pl.DataType],
    *,
    name: str,
) -> None:
    expected_names = set(expected)
    actual_names = set(frame.columns)
    missing = sorted(expected_names - actual_names)
    unknown = sorted(actual_names - expected_names)
    if missing or unknown or len(frame.columns) != len(expected):
        raise LongSourceContextError(f"{name} projection mismatch; missing={missing}, unknown={unknown}")
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise LongSourceContextError(f"{name} has invalid dtypes: {mismatched}")


def _require_feature_columns(frame: pl.DataFrame, config: LongNativeConfig) -> None:
    _reject_outcome_columns(frame, name="feature_tape")
    required_feature_dtypes = _required_feature_dtypes(config)
    missing = sorted(set(required_feature_dtypes) - set(frame.columns))
    if missing:
        raise LongSourceContextError(f"feature_tape missing required columns: {missing}")
    mismatched = {
        column: {
            "expected": str(expected),
            "actual": str(frame.schema[column]),
        }
        for column, expected in required_feature_dtypes.items()
        if frame.schema[column] != expected
    }
    if mismatched:
        raise LongSourceContextError(f"feature_tape has invalid dtypes: {mismatched}")
    collisions = sorted(_SIDECAR_OWNED_COLUMNS & set(frame.columns))
    if collisions:
        raise LongSourceContextError(f"feature_tape already contains sidecar-owned columns: {collisions}")
    if "ts_ms" in frame.columns:
        raise LongSourceContextError("feature_tape must use canonical signal_ts_ms and must not retain ts_ms")
    if _ROW_ORDER in frame.columns:
        raise LongSourceContextError(f"feature_tape contains reserved column {_ROW_ORDER!r}")

    invalid_keys = frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("signal_ts_ms").is_null()
        | (pl.col("signal_ts_ms") < 0)
        | ((pl.col("signal_ts_ms") % MS_PER_DAY) != 0)
    )
    if not invalid_keys.is_empty():
        raise LongSourceContextError("feature_tape contains invalid symbol/daily signal keys")
    invalid_membership_inputs = frame.filter(pl.col("symbol_age_days").is_null() | pl.col("in_universe").is_null())
    if not invalid_membership_inputs.is_empty():
        raise LongSourceContextError(
            "feature_tape requires non-null symbol_age_days and in_universe for membership parity"
        )
    duplicates = frame.group_by(list(_KEY_COLUMNS)).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise LongSourceContextError("feature_tape contains duplicate (symbol,signal_ts_ms) keys")
    canonical_keys = frame.select(_KEY_COLUMNS).sort(["signal_ts_ms", "symbol"])
    if not frame.select(_KEY_COLUMNS).equals(canonical_keys):
        raise LongSourceContextError("feature_tape must retain canonical (signal_ts_ms,symbol) builder order")
    bad_versions = frame.filter(
        pl.col("long_feature_tape_schema_version").is_null()
        | (pl.col("long_feature_tape_schema_version") != _SCHEMA_VERSION)
    )
    if not bad_versions.is_empty():
        raise LongSourceContextError(f"feature_tape schema version must equal {_SCHEMA_VERSION!r}")
    if LONG_CONTEXT_BTC_MONTH_REGIME_GATE != "off":
        raise LongSourceContextError("LONG source-context adapter supports the canonical BTC-month gate-off mode only")
    bad_gate = frame.filter(pl.col("gate_btc_month_regime").is_null() | ~pl.col("gate_btc_month_regime"))
    if not bad_gate.is_empty():
        raise LongSourceContextError(
            "LONG source context requires the frozen configuration with btc_month_regime_gate=off"
        )


def _assert_exact_key_coverage(
    actual: pl.DataFrame,
    expected: pl.DataFrame,
    *,
    keys: list[str],
    name: str,
) -> None:
    duplicates = actual.group_by(keys).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise LongSourceContextError(f"{name} contains duplicate {tuple(keys)} keys")
    missing = expected.join(actual.select(keys), on=keys, how="anti")
    extra = actual.select(keys).join(expected, on=keys, how="anti")
    if actual.height != expected.height or not missing.is_empty() or not extra.is_empty():
        raise LongSourceContextError(
            f"{name} keys must exactly equal the feature population; "
            f"missing={missing.head(5).to_dicts()}, extra={extra.head(5).to_dicts()}"
        )


def _validate_source_availability(
    sidecar: pl.DataFrame,
    feature_tape: pl.DataFrame,
) -> pl.DataFrame:
    _require_exact_schema(sidecar, SOURCE_AVAILABILITY_SCHEMA, name="source_availability")
    expected = feature_tape.select(_KEY_COLUMNS)
    _assert_exact_key_coverage(
        sidecar,
        expected,
        keys=list(_KEY_COLUMNS),
        name="source_availability",
    )
    invalid_keys = sidecar.filter(
        pl.col("symbol").is_null() | (pl.col("symbol").str.strip_chars() == "") | pl.col("signal_ts_ms").is_null()
    )
    if not invalid_keys.is_empty():
        raise LongSourceContextError("source_availability contains null/blank keys")
    invalid_times = sidecar.filter(
        pl.any_horizontal(
            (
                pl.col(column).is_not_null() & ((pl.col(column) < 0) | (pl.col(column) > pl.col("signal_ts_ms")))
            ).fill_null(False)
            for column in _AVAILABILITY_COLUMNS
        )
    )
    if not invalid_times.is_empty():
        raise LongSourceContextError(
            "every non-null source availability timestamp must be non-negative and <= signal_ts_ms"
        )
    missing_required = sidecar.filter(
        pl.col("daily_bar_available_ts_ms").is_null() | pl.col("signal_feature_available_ts_ms").is_null()
    )
    if not missing_required.is_empty():
        raise LongSourceContextError(
            "daily_bar_available_ts_ms and signal_feature_available_ts_ms must be non-null for every feature row"
        )
    declared_source_max = pl.max_horizontal(
        "daily_bar_available_ts_ms",
        "btc_context_available_ts_ms",
        "eth_context_available_ts_ms",
        "btc_month_context_available_ts_ms",
    )
    mismatched_aggregate = sidecar.filter(pl.col("signal_feature_available_ts_ms") != declared_source_max)
    if not mismatched_aggregate.is_empty():
        raise LongSourceContextError(
            "signal_feature_available_ts_ms must equal the maximum non-null declared daily/BTC/ETH/BTC-month availability timestamp"
        )
    return sidecar


def _validate_context_timestamp_coverage(
    sidecar: pl.DataFrame,
    feature_tape: pl.DataFrame,
    *,
    name: str,
) -> None:
    expected = feature_tape.select("signal_ts_ms").unique().sort("signal_ts_ms")
    _assert_exact_key_coverage(
        sidecar,
        expected,
        keys=["signal_ts_ms"],
        name=name,
    )
    invalid = sidecar.filter(
        pl.col("signal_ts_ms").is_null() | (pl.col("signal_ts_ms") < 0) | ((pl.col("signal_ts_ms") % MS_PER_DAY) != 0)
    )
    if not invalid.is_empty():
        raise LongSourceContextError(f"{name} contains invalid daily signal timestamps")


def _validate_regime_context(
    sidecar: pl.DataFrame,
    feature_tape: pl.DataFrame,
) -> pl.DataFrame:
    _require_exact_schema(sidecar, REGIME_CONTEXT_SCHEMA, name="regime_context")
    _validate_context_timestamp_coverage(sidecar, feature_tape, name="regime_context")
    for row in sidecar.iter_rows(named=True):
        signal_ts_ms = int(row["signal_ts_ms"])
        for asset in ("btc", "eth"):
            close = row[f"{asset}_close"]
            sma = row[f"{asset}_sma_{LONG_CONTEXT_REGIME_SMA_DAYS}d"]
            distance = row[f"{asset}_sma_dist"]
            available = row[f"{asset}_regime_available"]
            passed = row[f"{asset}_regime_pass"]
            if available is True:
                values = (close, sma, distance)
                if any(value is None or not math.isfinite(float(value)) for value in values):
                    raise LongSourceContextError(
                        f"{asset.upper()} regime marked available with missing/non-finite value at {signal_ts_ms}"
                    )
                if float(close) <= 0.0 or float(sma) <= 0.0:
                    raise LongSourceContextError(f"{asset.upper()} close/SMA must be positive at {signal_ts_ms}")
                expected_distance = float(close) / float(sma) - 1.0
                if not math.isclose(
                    float(distance),
                    expected_distance,
                    rel_tol=_FLOAT_REL_TOLERANCE,
                    abs_tol=_FLOAT_ABS_TOLERANCE,
                ):
                    raise LongSourceContextError(f"{asset.upper()} exact-30d distance parity failed at {signal_ts_ms}")
                if passed is None or bool(passed) != (float(close) > float(sma)):
                    raise LongSourceContextError(f"{asset.upper()} exact-30d pass parity failed at {signal_ts_ms}")
            elif available is False:
                if any(value is not None for value in (close, sma, distance, passed)):
                    raise LongSourceContextError(
                        f"{asset.upper()} unavailable regime must preserve null value/distance/pass at {signal_ts_ms}"
                    )
            else:  # pragma: no cover - exact Boolean dtype permits null, handled explicitly
                raise LongSourceContextError(f"{asset.upper()} regime availability must not be null at {signal_ts_ms}")
    return sidecar


def _validate_btc_month_context(
    sidecar: pl.DataFrame,
    feature_tape: pl.DataFrame,
) -> pl.DataFrame:
    _require_exact_schema(sidecar, BTC_MONTH_CONTEXT_SCHEMA, name="btc_month_context")
    _validate_context_timestamp_coverage(sidecar, feature_tape, name="btc_month_context")
    for row in sidecar.iter_rows(named=True):
        signal_ts_ms = int(row["signal_ts_ms"])
        value = row["btc_month_regime_value"]
        available = row["btc_month_regime_available"]
        passed = row["btc_month_regime_pass"]
        if available is True:
            if value is None or not math.isfinite(float(value)):
                raise LongSourceContextError(
                    f"BTC-month regime marked available without a finite value at {signal_ts_ms}"
                )
            # The registered configuration has no directional month gate.  The
            # contextual pass therefore means only that the disabled gate has a
            # usable diagnostic value; it does not mean 'uptrend'.
            if passed is not True:
                raise LongSourceContextError(
                    f"BTC-month pass must be true for an available value while the gate is off at {signal_ts_ms}"
                )
        elif available is False:
            if value is not None or passed is not None:
                raise LongSourceContextError(
                    f"unavailable BTC-month context must preserve null value/pass at {signal_ts_ms}"
                )
        else:  # pragma: no cover - exact Boolean dtype permits null, handled explicitly
            raise LongSourceContextError(f"BTC-month availability must not be null at {signal_ts_ms}")
    return sidecar


def _attach_rank_metadata_and_check_parity(
    frame: pl.DataFrame,
    config: LongNativeConfig,
) -> pl.DataFrame:
    output = frame
    for rank_column, value_column in _rank_specs(config):
        prefix = f"__{rank_column}"
        value = f"{prefix}_value"
        recomputed = f"{prefix}_recomputed"
        population = f"{rank_column}_population_peer_count"
        rankable = f"{rank_column}_rankable_peer_count"
        missing = f"{rank_column}_missing_peer_count"
        tie_count = f"{rank_column}_tie_count"
        output = (
            output.with_columns(
                pl.when(pl.col(value_column).is_not_null() & pl.col(value_column).is_finite())
                .then(pl.col(value_column))
                .otherwise(None)
                .cast(pl.Float64)
                .alias(value)
            )
            .with_columns(
                pl.col(value)
                .rank(method="ordinal", descending=True)
                .over("signal_ts_ms")
                .cast(pl.UInt32)
                .alias(recomputed),
                pl.len().over("signal_ts_ms").cast(pl.Int64).alias(population),
                pl.col(value).count().over("signal_ts_ms").cast(pl.Int64).alias(rankable),
                pl.when(pl.col(value).is_not_null())
                .then(pl.len().over(["signal_ts_ms", value]))
                .otherwise(None)
                .cast(pl.Int64)
                .alias(tie_count),
            )
            .with_columns(
                (pl.col(population) - pl.col(rankable)).cast(pl.Int64).alias(missing),
                pl.lit(_TIE_METHOD, dtype=pl.String).alias(f"{rank_column}_tie_method"),
                pl.lit(_DENOMINATOR_RULE, dtype=pl.String).alias(f"{rank_column}_denominator_rule"),
            )
        )
        mismatch = output.filter(~pl.col(rank_column).eq_missing(pl.col(recomputed)))
        if not mismatch.is_empty():
            examples = mismatch.select(
                "symbol",
                "signal_ts_ms",
                value_column,
                rank_column,
                recomputed,
            ).head(5)
            raise LongSourceContextError(
                f"{rank_column} ordinal-descending parity failed over the supplied timestamp population: "
                f"{examples.to_dicts()}"
            )
        output = output.with_columns(pl.col(recomputed).alias(rank_column)).drop(value, recomputed)

    universe_turnover = f"turnover_median_{config.universe_volume_window_days}d"
    recomputed_membership = "__in_universe_recomputed"
    output = output.with_columns(
        (
            (pl.col("universe_rank") <= config.universe_size)
            & (pl.col("symbol_age_days") >= config.min_listing_history_days)
            & pl.col(universe_turnover).is_not_null()
            & pl.col(universe_turnover).is_finite()
        )
        .fill_null(False)
        .cast(pl.Boolean)
        .alias(recomputed_membership)
    )
    membership_mismatch = output.filter(~pl.col("in_universe").eq_missing(pl.col(recomputed_membership)))
    if not membership_mismatch.is_empty():
        examples = membership_mismatch.select(
            "symbol",
            "signal_ts_ms",
            universe_turnover,
            "universe_rank",
            "symbol_age_days",
            "in_universe",
            recomputed_membership,
        ).head(5)
        raise LongSourceContextError(
            "in_universe parity failed against configured rank/listing-age/finite-turnover rules: "
            f"{examples.to_dicts()}"
        )
    output = output.drop(recomputed_membership)
    return output


def _assert_population_preserved(output: pl.DataFrame, expected_keys: pl.DataFrame) -> None:
    actual_keys = output.select(_KEY_COLUMNS)
    if output.height != expected_keys.height or not actual_keys.equals(expected_keys):
        raise RuntimeError("LONG source-context annotation changed row/key/cardinality")


def attach_long_source_context(
    feature_tape: pl.DataFrame,
    *,
    config: LongNativeConfig,
    source_availability: pl.DataFrame,
    regime_context: pl.DataFrame,
    btc_month_context: pl.DataFrame,
) -> pl.DataFrame:
    """Attach registered LONG S02 source context without reading outcomes.

    Sidecar key coverage must be exact.  Daily and aggregate signal-feature
    availability are required; the latter must reconstruct exactly as the
    maximum of all non-null declared daily/BTC/ETH/BTC-month timestamps.  A null
    optional context timestamp means unknown, not zero or signal time.  BTC/ETH
    unavailable state is projected to the current registry as
    ``*_regime_available=false`` plus a false pass flag; the pair preserves
    unknown even though the pass fields themselves are currently non-nullable.
    BTC-month pass has separate gate-off semantics: ``true`` means an available
    diagnostic value under a disabled gate, while null means the value is
    unavailable.  ``gate_btc_month_regime`` remains true independently because
    the configured gate is disabled.

    Rank diagnostics cover every supplied row at each signal timestamp.  The
    module deliberately does not claim that this supplied population is a
    complete PIT universe; see :data:`LONG_SOURCE_CONTEXT_DIAGNOSTICS`.
    """

    long_context_runtime_parity_surface(config)
    _require_feature_columns(feature_tape, config)
    availability = _validate_source_availability(source_availability, feature_tape)
    regimes = _validate_regime_context(regime_context, feature_tape)
    btc_month = _validate_btc_month_context(btc_month_context, feature_tape)

    expected_keys = feature_tape.select(_KEY_COLUMNS)
    output = feature_tape.with_row_index(_ROW_ORDER)
    output = output.join(availability, on=list(_KEY_COLUMNS), how="left")

    renamed_regimes = regimes.rename(
        {column: f"__context_{column}" for column in regimes.columns if column != "signal_ts_ms"}
    )
    output = output.join(renamed_regimes, on="signal_ts_ms", how="left")

    for asset, builder_pass_column in (
        ("btc", "regime_on"),
        ("eth", "eth_regime_on"),
    ):
        available = f"__context_{asset}_regime_available"
        passed = f"__context_{asset}_regime_pass"
        pass_mismatch = output.filter(
            pl.col(available)
            & (pl.col(builder_pass_column).is_null() | (pl.col(builder_pass_column) != pl.col(passed))).fill_null(True)
        )
        if not pass_mismatch.is_empty():
            raise LongSourceContextError(
                f"canonical builder {builder_pass_column} disagrees with available {asset.upper()} sidecar context"
            )
    btc_distance_mismatch = output.filter(
        pl.col("__context_btc_regime_available")
        & (
            pl.col("btc_sma_dist").is_null()
            | ~pl.col("btc_sma_dist").is_finite()
            | (
                (pl.col("btc_sma_dist") - pl.col("__context_btc_sma_dist")).abs()
                > _FLOAT_ABS_TOLERANCE + _FLOAT_REL_TOLERANCE * pl.col("__context_btc_sma_dist").abs()
            )
        ).fill_null(True)
    )
    if not btc_distance_mismatch.is_empty():
        raise LongSourceContextError("canonical builder btc_sma_dist disagrees with available BTC sidecar context")

    output = output.drop("regime_on", "eth_regime_on", "btc_sma_dist").with_columns(
        pl.col("__context_btc_regime_pass").fill_null(False).cast(pl.Boolean).alias("regime_on"),
        pl.col("__context_eth_regime_pass").fill_null(False).cast(pl.Boolean).alias("eth_regime_on"),
        pl.col("__context_btc_regime_available").cast(pl.Boolean).alias("btc_regime_available"),
        pl.col("__context_eth_regime_available").cast(pl.Boolean).alias("eth_regime_available"),
        pl.col("__context_btc_sma_dist").cast(pl.Float64).alias("btc_sma_dist"),
        pl.col("__context_eth_sma_dist").cast(pl.Float64).alias("eth_sma_dist"),
    )
    output = output.drop(column for column in output.columns if column.startswith("__context_"))

    output = output.join(btc_month, on="signal_ts_ms", how="left")
    missing_context_availability = output.filter(
        (pl.col("btc_regime_available") & pl.col("btc_context_available_ts_ms").is_null())
        | (pl.col("eth_regime_available") & pl.col("eth_context_available_ts_ms").is_null())
        | (pl.col("btc_month_regime_available") & pl.col("btc_month_context_available_ts_ms").is_null())
    )
    if not missing_context_availability.is_empty():
        raise LongSourceContextError(
            "available BTC/ETH/month context requires an explicit source availability timestamp"
        )

    output = _attach_rank_metadata_and_check_parity(output, config)
    output = output.sort(_ROW_ORDER).drop(_ROW_ORDER)
    _assert_population_preserved(output, expected_keys)
    return output


__all__ = [
    "BTC_MONTH_CONTEXT_SCHEMA",
    "LONG_CONTEXT_BTC_MONTH_REGIME_GATE",
    "LONG_CONTEXT_REGIME_SMA_DAYS",
    "LONG_CONTEXT_REGIME_SYMBOL",
    "LONG_CONTEXT_UNIVERSE_VOLUME_WINDOW_DAYS",
    "LONG_SOURCE_CONTEXT_DIAGNOSTICS",
    "LONG_SOURCE_CONTEXT_EVIDENCE_STATUS",
    "LongSourceContextError",
    "RANK_METADATA_COLUMNS",
    "REGIME_CONTEXT_SCHEMA",
    "SOURCE_AVAILABILITY_SCHEMA",
    "attach_long_source_context",
    "long_context_runtime_parity_surface",
]
