"""Pure LONG population feature and label primitives for the overhaul scout.

This module deliberately does not build features, choose a PIT population, apply
portfolio state, or run a backtest.  It annotates a caller-supplied daily feature
population with the current FC classifier diagnostics and causal hourly price
paths.  Daily feature ``ts_ms`` values are signal-bar close times; hourly-bar
``ts_ms`` values are bar-open times.

``build_long_feature_tape`` is strictly signal-time: for each row it reads no bar
after the signal timestamp. ``append_long_entry_policy`` is the separate,
explicit post-signal entry-reconstruction step and stops at the first close
trigger or configured fall-through deadline. ``append_long_path_labels`` then
adds forward outcomes from those frozen anchors.

The functions are intentionally row-local and deterministic.  They therefore do
not reconstruct cooldown, capacity, crowding, sizing, costs, or funding.  The
``classifier_selected`` flag means exact selection by ``_classify_entry`` only,
not an executed trade.
"""

from __future__ import annotations

import math
import re
import warnings
from collections.abc import Callable, Mapping
from dataclasses import fields
from numbers import Integral, Real
from typing import Any

import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .long_native import (
    LongNativeConfig,
    _btc_month_regime_allows,
    _classify_entry,
    _fc_atr_available,
    _fc_exit_params,
    _safe_float,
    detect_pattern_fomo_chase,
)

DEFAULT_POINT_HORIZONS = (1, 24, 72)
DEFAULT_EXCURSION_HORIZONS = (24, 72)
_DIAGNOSTIC_PATH_HORIZONS_HOURS = (6, 12, 24, 48, 72)
SIGNAL_CLOSE_REL_TOLERANCE = 1e-12
SIGNAL_CLOSE_ABS_TOLERANCE = 1e-12
EXPLORATORY_LABEL_SCHEMA_VERSION = "long_exploratory_minimal_labels_v1"

_FEATURE_SCHEMA_VERSION = "long_a0_signal_feature_v3"
_ENTRY_SCHEMA_VERSION = "long_a0_entry_policy_v1"
_LABEL_SCHEMA_VERSION = "long_a0_minimal_labels_v1"
LONG_S02_CLASSIFIER_PATTERN = "fomo_chase"

LONG_PATTERN_TOGGLE_FIELDS = (
    "enable_capitulation_rebound",
    "enable_volume_resurrection",
    "enable_funding_squeeze",
    "enable_oversold_bounce",
    "enable_uptrend_dip",
    "enable_fomo_chase",
    "enable_xsec_momentum",
    "enable_lowvol",
    "enable_reversal",
    "enable_funding_carry",
    "enable_oi_momentum",
    "enable_metrics_signal",
)
LONG_TRIGGER_AND_EXIT_PROFILE_FIELDS = (
    "fc_min_day_return",
    "fc_use_sigma_threshold",
    "fc_sigma_mult",
    "fc_enable_3d_trigger",
    "fc_enable_7d_trigger",
    "fc_enable_intraday_trigger",
    "fc_intraday_window_hours",
    "fc_use_own_pump_quantile",
    "fc_min_close_location",
    "fc_close_loc_multi_day",
    "fc_use_atr_exits",
    "fc_atr_stop_mult",
    "fc_atr_tp_mult",
    "fc_stop_pct",
    "fc_take_profit_pct",
)

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
    r"^(?:h[1-6]|[1-6]h)_(?:close|low)(?:$|_)|^(?:close|low)_(?:h[1-6]|[1-6]h)(?:$|_)"
)
_HORIZON_OUTCOME_RE = re.compile(
    r"^(?:h\d+|\d+h)_(?:return|point_return|mfe|mae|pnl)(?:$|_)|"
    r"^(?:return|point_return|mfe|mae|pnl)_(?:h\d+|\d+h)(?:$|_)"
)

# Source-order gates used by the FC detector.  The classifier-wide BTC monthly
# regime gate precedes this sequence.
FC_GATE_COLUMNS = (
    "gate_fc_enabled",
    "gate_in_universe",
    "gate_btc_regime",
    "gate_eth_regime",
    "gate_volume_rank",
    "gate_log_return_available",
    "gate_any_trigger",
    "gate_coin_above_own_sma",
    "gate_coin_min_30d_return",
    "gate_btc_not_near_high",
    "gate_btc_must_be_near_high",
    "gate_atr_cap",
    "gate_own_atr_percentile",
    "gate_min_volume_confirmation",
    "gate_max_volume_confirmation",
    "gate_max_coin_60d_return",
    "gate_min_btc_sma_distance",
    "gate_max_btc_sma_distance",
    "gate_lsr",
    "gate_oi_rising",
)


_FEATURE_DERIVED_DTYPES: dict[str, pl.DataType] = {
    "signal_ts_ms": pl.Int64,
    "simple_return_1d": pl.Float64,
    "simple_return_3d": pl.Float64,
    "simple_return_7d": pl.Float64,
    "fc_sigma_threshold_available": pl.Boolean,
    "fc_threshold_1d_log": pl.Float64,
    "fc_threshold_3d_log": pl.Float64,
    "fc_threshold_7d_log": pl.Float64,
    "fc_threshold_intraday_log": pl.Float64,
    "intraday_feature_available": pl.Boolean,
    "fc_pump_sigma_1d": pl.Float64,
    "fc_pump_sigma_3d": pl.Float64,
    "fc_pump_sigma_7d": pl.Float64,
    "ratio_1d": pl.Float64,
    "ratio_3d": pl.Float64,
    "ratio_7d": pl.Float64,
    "fc_trigger_1d_input_complete": pl.Boolean,
    "fc_trigger_3d_input_complete": pl.Boolean,
    "fc_trigger_7d_input_complete": pl.Boolean,
    "fc_trigger_1d": pl.Boolean,
    "fc_trigger_3d": pl.Boolean,
    "fc_trigger_7d": pl.Boolean,
    "fc_trigger_intraday": pl.Boolean,
    "fc_trigger_own_quantile": pl.Boolean,
    "fc_all_trigger": pl.Boolean,
    "fc_trigger_identities": pl.List(pl.String),
    "fc_trigger_identity": pl.String,
    "fc_trigger_bitmask": pl.Int8,
    "trigger_strength_ratio": pl.Float64,
    "active_trigger_close_location": pl.Float64,
    "gate_btc_month_regime": pl.Boolean,
    **{name: pl.Boolean for name in FC_GATE_COLUMNS},
    "fc_independent_gate_pass": pl.Boolean,
    "fc_classifier_gate_pass": pl.Boolean,
    "first_sequential_rejection_reason": pl.String,
    "signal_bar_present": pl.Boolean,
    "signal_bar_complete": pl.Boolean,
    "signal_close_hourly": pl.Float64,
    "fc_detector_selected": pl.Boolean,
    "classified_pattern": pl.String,
    "classifier_selected": pl.Boolean,
    "classifier_eligible": pl.Boolean,
    "classifier_stop_pct": pl.Float64,
    "classifier_take_profit_pct": pl.Float64,
    "classifier_max_hold_days": pl.Int64,
    "fc_exit_stop_pct": pl.Float64,
    "fc_exit_take_profit_pct": pl.Float64,
    "fc_exit_max_hold_hours": pl.Int64,
    "fc_atr_exit_available": pl.Boolean,
    "fc_atr_fallback_used": pl.Boolean,
    "fc_exit_param_source": pl.String,
    "long_feature_tape_schema_version": pl.String,
}


_ENTRY_DERIVED_DTYPES: dict[str, pl.DataType] = {
    "common_entry_available": pl.Boolean,
    "common_entry_ts_ms": pl.Int64,
    "common_entry_hour": pl.Int64,
    "common_entry_price": pl.Float64,
    "common_entry_reason": pl.String,
    "current_entry_available": pl.Boolean,
    "current_entry_ts_ms": pl.Int64,
    "current_entry_hour": pl.Int64,
    "current_entry_price": pl.Float64,
    "current_entry_reason": pl.String,
    "current_entry_retrace_pct": pl.Float64,
    "current_entry_retrace_threshold": pl.Float64,
    "current_entry_scan_first_hour": pl.Int64,
    "current_entry_scan_end_hour": pl.Int64,
    "current_entry_close_trigger_first_hour": pl.Int64,
    "current_entry_intrabar_low_first_hour_nonfill": pl.Int64,
    "current_entry_intrabar_low_observed_first_hour_nonfill": pl.Int64,
    "current_entry_scan_missing_hour_bitmask": pl.Int8,
    "current_entry_scan_prefix_complete": pl.Boolean,
    "current_entry_close_triggered": pl.Boolean,
    "current_entry_intrabar_low_touch_nonfill": pl.Boolean,
    "current_entry_policy_available": pl.Boolean,
    "current_entry_missing_reason": pl.String,
    "entry_price_improvement": pl.Float64,
    "entry_delay_hours_vs_common": pl.Int64,
    "long_entry_policy_schema_version": pl.String,
}


def _require_frozen_v11a_config(config: LongNativeConfig, *, stage: str) -> LongNativeConfig:
    """Require the exact mechanically-derived config behind the frozen schemas."""

    if not isinstance(config, LongNativeConfig):
        raise TypeError(f"{stage} requires an explicit LongNativeConfig")
    # Import lazily so this diagnostic module cannot become a configuration
    # authority of its own.  The runtime profile factory remains the one source.
    from .long_native_event_demo import _v11a_long_native_config

    expected = _v11a_long_native_config()
    if config == expected:
        return config
    mismatches = [
        field.name for field in fields(LongNativeConfig) if getattr(config, field.name) != getattr(expected, field.name)
    ]
    preview = ", ".join(mismatches[:8])
    if len(mismatches) > 8:
        preview += f", ... (+{len(mismatches) - 8} more)"
    raise ValueError(
        f"{stage} requires the exact _v11a_long_native_config; mismatched fields: {preview or '<unknown>'}"
    )


def long_population_runtime_parity_surface(config: LongNativeConfig) -> dict[str, object]:
    """Validate and expose the config values consumed by the LONG row builder.

    This is the consumer-owned proof surface for the classifier/exit and trigger
    targets in the A0 config-parity manifest.  It deliberately validates the
    live module constants and the schema version emitted by
    :func:`build_long_feature_tape`; a central manifest need not assert that
    these consumers were checked by hand.
    """

    cfg = _require_frozen_v11a_config(config, stage="long_population_runtime_parity_surface")
    active_pattern_toggles = {name: bool(getattr(cfg, name)) for name in LONG_PATTERN_TOGGLE_FIELDS}
    active_patterns = [name.removeprefix("enable_") for name, enabled in active_pattern_toggles.items() if enabled]
    if active_patterns != [LONG_S02_CLASSIFIER_PATTERN]:
        raise ValueError(
            "LONG population classifier parity failed: "
            f"active config patterns={active_patterns}, "
            f"runtime pattern={LONG_S02_CLASSIFIER_PATTERN!r}"
        )

    # Lazy import avoids making this low-level row builder depend on the schema
    # registry during ordinary module import.
    from .strategy_overhaul_schemas import ARTIFACT_SCHEMAS, LONG_SIGNAL_SCHEMA_ID

    registered_schema_version = ARTIFACT_SCHEMAS[LONG_SIGNAL_SCHEMA_ID].schema_version
    if _FEATURE_SCHEMA_VERSION != registered_schema_version:
        raise ValueError(
            "LONG population feature-schema parity failed: "
            f"builder={_FEATURE_SCHEMA_VERSION!r}, registry={registered_schema_version!r}"
        )

    classifier_and_exit_shape = {
        "active_pattern_toggles": active_pattern_toggles,
        "fc_max_hold_days": cfg.fc_max_hold_days,
        "fc_exit_max_hold_hours": cfg.fc_max_hold_days * 24,
    }
    trigger_and_exit_profile = {name: getattr(cfg, name) for name in LONG_TRIGGER_AND_EXIT_PROFILE_FIELDS}
    return {
        "consumer_validator": ("liquidity_migration.long_population_scout.long_population_runtime_parity_surface"),
        "validated_targets": [
            "classifier_and_exit_shape",
            "trigger_and_exit_profile",
        ],
        "validated_target_fields": {
            "classifier_and_exit_shape": [
                "active_pattern_toggles",
                "fc_max_hold_days",
                "fc_exit_max_hold_hours",
            ],
            "trigger_and_exit_profile": list(LONG_TRIGGER_AND_EXIT_PROFILE_FIELDS),
        },
        "validated_consumers": {
            "classifier_and_exit_shape": [
                "long_population_scout.build_long_feature_tape classifier_selected=fomo_chase",
                "long_population_scout.build_long_feature_tape fc_exit_max_hold_hours",
            ],
            "trigger_and_exit_profile": [
                "long_population_scout._trigger_diagnostics",
                "long_population_scout._fc_gate_diagnostics",
                "long_population_scout.build_long_feature_tape ATR/fallback exit fields",
            ],
        },
        "classifier_and_exit_shape": classifier_and_exit_shape,
        "trigger_and_exit_profile": trigger_and_exit_profile,
        "feature_schema_version": _FEATURE_SCHEMA_VERSION,
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


def _reject_outcome_like_columns(
    frame: pl.DataFrame,
    *,
    name: str,
    allowed: set[str] | None = None,
) -> None:
    allowed = allowed or set()
    rejected = sorted(column for column in frame.columns if column not in allowed and _is_outcome_like_column(column))
    if rejected:
        raise ValueError(f"{name} contains outcome-like caller columns: {rejected}")


def _strict_symbol(value: Any, *, name: str, row_number: int) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} row {row_number} symbol must be a non-empty canonical string")
    return value


def _strict_timestamp(
    value: Any,
    *,
    name: str,
    row_number: int,
    column: str,
    grid_ms: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} row {row_number} {column} must be an integer millisecond timestamp")
    timestamp = int(value)
    if timestamp < 0:
        raise ValueError(f"{name} row {row_number} {column} must be non-negative")
    if timestamp % grid_ms:
        raise ValueError(f"{name} row {row_number} {column} is not aligned to the {grid_ms}ms grid")
    return timestamp


def _validate_primary_keys(
    frame: pl.DataFrame,
    *,
    time_column: str,
    name: str,
    grid_ms: int,
) -> None:
    for row_number, row in enumerate(frame.select(["symbol", time_column]).iter_rows(named=True)):
        _strict_symbol(row["symbol"], name=name, row_number=row_number)
        _strict_timestamp(
            row[time_column],
            name=name,
            row_number=row_number,
            column=time_column,
            grid_ms=grid_ms,
        )


def _strict_positive_price(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite positive number")
    price = float(value)
    if not math.isfinite(price) or price <= 0.0:
        raise ValueError(f"{name} must be a finite positive number")
    return price


def _prices_match(left: float, right: float) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=SIGNAL_CLOSE_REL_TOLERANCE,
        abs_tol=SIGNAL_CLOSE_ABS_TOLERANCE,
    )


def _require_exact_stage_column(
    frame: pl.DataFrame,
    *,
    column: str,
    expected: Any,
    name: str,
) -> None:
    for row_number, actual in enumerate(frame[column].to_list()):
        type_matches = (
            isinstance(expected, str)
            and isinstance(actual, str)
            or isinstance(expected, bool)
            and isinstance(actual, bool)
            or isinstance(expected, int)
            and not isinstance(expected, bool)
            and isinstance(actual, Integral)
            and not isinstance(actual, bool)
        )
        if not type_matches or actual != expected:
            raise ValueError(f"{name} row {row_number} {column} must equal {expected!r}; got {actual!r}")


def _with_typed_columns(
    frame: pl.DataFrame,
    dtypes: Mapping[str, pl.DataType],
    *,
    defaults: Mapping[str, Any] | None = None,
) -> pl.DataFrame:
    defaults = defaults or {}
    expressions: list[pl.Expr] = []
    for name, dtype in dtypes.items():
        if name in frame.columns:
            expressions.append(pl.col(name).cast(dtype, strict=False).alias(name))
        else:
            expressions.append(pl.lit(defaults.get(name), dtype=dtype).alias(name))
    return frame.with_columns(expressions)


def _finite_expm1(value: Any) -> float | None:
    log_value = _safe_float(value)
    if log_value is None or not math.isfinite(log_value):
        return None
    try:
        simple = math.expm1(log_value)
    except OverflowError:
        return None
    return simple if math.isfinite(simple) else None


def _threshold_ratio(value: float | None, threshold: float) -> float | None:
    if value is None or not math.isfinite(value) or threshold <= 0.0 or not math.isfinite(threshold):
        return None
    return value / threshold


def _require_columns(frame: pl.DataFrame, required: set[str], *, name: str) -> None:
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {sorted(missing)}")


def _reject_duplicate_keys(frame: pl.DataFrame, keys: list[str], *, name: str) -> None:
    if frame.is_empty():
        return
    null_key = frame.filter(pl.any_horizontal(pl.col(key).is_null() for key in keys)).select(keys).head(1)
    if not null_key.is_empty():
        raise ValueError(f"{name} has null key values: {null_key.to_dicts()}")
    duplicates = frame.group_by(keys).len().filter(pl.col("len") > 1).sort(keys)
    if duplicates.is_empty():
        return
    examples = duplicates.select(keys + ["len"]).head(5).to_dicts()
    raise ValueError(f"{name} has duplicate {tuple(keys)} keys: {examples}")


def _positive_float(value: Any) -> float | None:
    value = _safe_float(value)
    if value is None or not math.isfinite(value) or value <= 0.0:
        return None
    return value


def _hourly_lookup(hourly_bars: pl.DataFrame) -> dict[str, dict[int, dict[str, float | None]]]:
    lookup: dict[str, dict[int, dict[str, float | None]]] = {}
    for row in hourly_bars.sort(["symbol", "ts_ms"]).iter_rows(named=True):
        symbol = str(row["symbol"])
        bar_open_ts = int(row["ts_ms"])
        bar_end_ts = bar_open_ts + MS_PER_HOUR
        lookup.setdefault(symbol, {})[bar_end_ts] = {
            "open": _positive_float(row.get("open")),
            "high": _positive_float(row.get("high")),
            "low": _positive_float(row.get("low")),
            "close": _positive_float(row.get("close")),
        }
    return lookup


def _hourly_subset_for_ends(
    hourly_bars: pl.DataFrame,
    required_ends: set[tuple[str, int]],
) -> pl.DataFrame:
    """Return only requested bar-end keys before any OHLC value is consumed."""

    if not required_ends or hourly_bars.is_empty():
        return hourly_bars.head(0)
    keys = pl.DataFrame(
        {
            "symbol": [key[0] for key in sorted(required_ends)],
            "__bar_end_ts_ms": [key[1] for key in sorted(required_ends)],
        },
        schema={"symbol": pl.String, "__bar_end_ts_ms": pl.Int64},
    )
    return (
        hourly_bars.with_columns(
            pl.col("symbol").cast(pl.String),
            (pl.col("ts_ms") + MS_PER_HOUR).alias("__bar_end_ts_ms"),
        )
        .join(keys, on=["symbol", "__bar_end_ts_ms"], how="semi")
        .drop("__bar_end_ts_ms")
    )


def _hourly_row_index(hourly_bars: pl.DataFrame) -> dict[tuple[str, int], int]:
    """Validate/index every source key without consuming any OHLC value."""

    index: dict[tuple[str, int], int] = {}
    for row_number, (symbol_raw, open_ts_raw) in enumerate(hourly_bars.select(["symbol", "ts_ms"]).iter_rows()):
        symbol = _strict_symbol(symbol_raw, name="hourly_bars", row_number=row_number)
        open_ts_ms = _strict_timestamp(
            open_ts_raw,
            name="hourly_bars",
            row_number=row_number,
            column="ts_ms",
            grid_ms=MS_PER_HOUR,
        )
        key = (symbol, open_ts_ms + MS_PER_HOUR)
        if key in index:
            raise ValueError(f"hourly_bars has duplicate ('symbol', 'ts_ms') key: {(symbol, open_ts_ms)}")
        index[key] = row_number
    return index


def _indexed_bar(
    hourly_bars: pl.DataFrame,
    row_index: Mapping[tuple[str, int], int | None],
    *,
    symbol: str,
    bar_end_ts_ms: int,
) -> dict[str, float | None] | None:
    key = (symbol, bar_end_ts_ms)
    if key not in row_index:
        return None
    row_number = row_index[key]
    if row_number is None:
        raise ValueError(f"hourly_bars has duplicate ('symbol', 'ts_ms') key: {key}")
    row = hourly_bars.row(row_number, named=True)
    row_symbol = _strict_symbol(row.get("symbol"), name="hourly_bars", row_number=row_number)
    open_ts_ms = _strict_timestamp(
        row.get("ts_ms"),
        name="hourly_bars",
        row_number=row_number,
        column="ts_ms",
        grid_ms=MS_PER_HOUR,
    )
    if row_symbol != symbol or open_ts_ms + MS_PER_HOUR != bar_end_ts_ms:
        raise ValueError(f"hourly_bars row {row_number} does not exactly match requested bar-end key {key}")
    values = {
        field: _strict_positive_price(
            row.get(field),
            name=f"hourly_bars row {row_number} {field}",
        )
        for field in ("open", "high", "low", "close")
    }
    if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
        raise ValueError(f"hourly_bars row {row_number} has OHLC outside [low, high]")
    if values["high"] < values["low"]:
        raise ValueError(f"hourly_bars row {row_number} has high below low")
    return {
        "open": values["open"],
        "high": values["high"],
        "low": values["low"],
        "close": values["close"],
    }


def _bar_range_valid(bar: Mapping[str, float | None] | None) -> bool:
    if bar is None:
        return False
    high = bar.get("high")
    low = bar.get("low")
    return high is not None and low is not None and high >= low


def _bar_path_complete(bar: Mapping[str, float | None] | None) -> bool:
    return _bar_range_valid(bar) and bar is not None and bar.get("close") is not None


def _return(value: float | None, anchor: float | None) -> float | None:
    if value is None or anchor is None or anchor <= 0.0:
        return None
    return value / anchor - 1.0


def _trigger_diagnostics(row: Mapping[str, Any], cfg: LongNativeConfig) -> dict[str, Any]:
    today_ret = _safe_float(row.get("log_return"))
    sigma = _safe_float(row.get("sigma_daily_30d")) if cfg.fc_use_sigma_threshold else None
    sigma_usable = sigma is not None and sigma > 0.0
    base_threshold = math.log1p(cfg.fc_min_day_return)
    threshold_1d = cfg.fc_sigma_mult * sigma if sigma_usable else base_threshold
    threshold_3d = threshold_1d * math.sqrt(3.0)
    threshold_7d = threshold_1d * math.sqrt(7.0)

    close_location = _safe_float(row.get("close_location"))
    pump_3d = _safe_float(row.get("pump_3d_log"))
    close_location_3d = _safe_float(row.get("close_loc_3d"))
    pump_7d = _safe_float(row.get("pump_7d_log"))
    close_location_7d = _safe_float(row.get("close_loc_7d"))
    intraday_pump = _safe_float(row.get("intra_max_Nh_pump_log"))
    own_quantile = _safe_float(row.get("own_pump_quantile_90d"))

    trigger_1d = bool(
        today_ret is not None
        and today_ret >= threshold_1d
        and close_location is not None
        and close_location >= cfg.fc_min_close_location
    )
    trigger_3d = bool(
        cfg.fc_enable_3d_trigger
        and pump_3d is not None
        and close_location_3d is not None
        and close_location_3d >= cfg.fc_close_loc_multi_day
        and pump_3d >= threshold_3d
    )
    trigger_7d = bool(
        cfg.fc_enable_7d_trigger
        and pump_7d is not None
        and close_location_7d is not None
        and close_location_7d >= cfg.fc_close_loc_multi_day
        and pump_7d >= threshold_7d
    )
    intraday_scale = math.sqrt(cfg.fc_intraday_window_hours / 24.0)
    intraday_threshold = threshold_1d * intraday_scale
    trigger_intraday = bool(
        cfg.fc_enable_intraday_trigger and intraday_pump is not None and intraday_pump >= intraday_threshold
    )
    trigger_own_quantile = bool(
        cfg.fc_use_own_pump_quantile
        and today_ret is not None
        and own_quantile is not None
        and today_ret >= own_quantile
        and close_location is not None
        and close_location >= cfg.fc_min_close_location
    )
    identities = [
        name
        for name, active in (
            ("1d", trigger_1d),
            ("3d", trigger_3d),
            ("7d", trigger_7d),
            ("intraday", trigger_intraday),
            ("own_quantile", trigger_own_quantile),
        )
        if active
    ]

    ratio_1d = _threshold_ratio(today_ret, threshold_1d)
    ratio_3d = _threshold_ratio(pump_3d, threshold_3d)
    ratio_7d = _threshold_ratio(pump_7d, threshold_7d)
    standard_triggers = (
        (1, True, trigger_1d, ratio_1d, close_location, cfg.fc_min_close_location),
        (3, cfg.fc_enable_3d_trigger, trigger_3d, ratio_3d, close_location_3d, cfg.fc_close_loc_multi_day),
        (7, cfg.fc_enable_7d_trigger, trigger_7d, ratio_7d, close_location_7d, cfg.fc_close_loc_multi_day),
    )
    location_qualified_ratios = [
        ratio
        for _horizon, enabled, _fired, ratio, location, minimum_location in standard_triggers
        if enabled and ratio is not None and location is not None and location >= minimum_location
    ]
    fired_close_locations = [
        location
        for _horizon, _enabled, fired, _ratio, location, _minimum_location in standard_triggers
        if fired and location is not None
    ]

    return {
        "fc_sigma_threshold_available": bool(sigma_usable),
        "fc_threshold_1d_log": threshold_1d,
        "fc_threshold_3d_log": threshold_3d,
        "fc_threshold_7d_log": threshold_7d,
        "fc_threshold_intraday_log": intraday_threshold,
        "intraday_feature_available": intraday_pump is not None,
        "fc_pump_sigma_1d": today_ret / sigma if today_ret is not None and sigma_usable else None,
        "fc_pump_sigma_3d": pump_3d / (sigma * math.sqrt(3.0)) if pump_3d is not None and sigma_usable else None,
        "fc_pump_sigma_7d": pump_7d / (sigma * math.sqrt(7.0)) if pump_7d is not None and sigma_usable else None,
        "ratio_1d": ratio_1d,
        "ratio_3d": ratio_3d,
        "ratio_7d": ratio_7d,
        "fc_trigger_1d_input_complete": today_ret is not None and close_location is not None,
        "fc_trigger_3d_input_complete": pump_3d is not None and close_location_3d is not None,
        "fc_trigger_7d_input_complete": pump_7d is not None and close_location_7d is not None,
        "fc_trigger_1d": trigger_1d,
        "fc_trigger_3d": trigger_3d,
        "fc_trigger_7d": trigger_7d,
        "fc_trigger_intraday": trigger_intraday,
        "fc_trigger_own_quantile": trigger_own_quantile,
        "fc_all_trigger": bool(identities),
        "fc_trigger_identities": identities,
        "fc_trigger_identity": "+".join(identities) if identities else None,
        "fc_trigger_bitmask": ((1 if trigger_1d else 0) | (2 if trigger_3d else 0) | (4 if trigger_7d else 0)),
        "trigger_strength_ratio": max(location_qualified_ratios) if location_qualified_ratios else None,
        "active_trigger_close_location": max(fired_close_locations) if fired_close_locations else None,
    }


def _fc_gate_diagnostics(row: dict[str, Any], cfg: LongNativeConfig) -> dict[str, Any]:
    result = _trigger_diagnostics(row, cfg)

    rank = _safe_float(row.get("today_volume_rank"))
    coin_sma = _safe_float(row.get("coin_fc_sma"))
    close = _safe_float(row.get("close"))
    coin_30d = _safe_float(row.get("coin_30d_return"))
    btc_high_proximity = _safe_float(row.get("btc_high_proximity"))
    atr = _safe_float(row.get("atr_14d_pct"))
    own_atr_quantile = _safe_float(row.get("own_atr_quantile_90d"))
    volume_multiple = _safe_float(row.get("vol_vs_30d_median"))
    coin_60d = _safe_float(row.get("coin_60d_return"))
    btc_sma_distance = _safe_float(row.get("btc_sma_dist"))
    lsr = _safe_float(row.get("global_lsr"))
    oi_change = _safe_float(row.get("oi_chg_7d"))

    gates: dict[str, bool] = {
        "gate_btc_month_regime": _btc_month_regime_allows(row, cfg),
        "gate_fc_enabled": bool(cfg.enable_fomo_chase),
        "gate_in_universe": bool(row.get("in_universe")),
        "gate_btc_regime": not cfg.fc_btc_regime_required or bool(row.get("regime_on")),
        "gate_eth_regime": not cfg.fc_eth_regime_required or bool(row.get("eth_regime_on")),
        "gate_volume_rank": rank is not None and rank <= cfg.fc_top_volume_rank_max,
        # The detector requires today's return before evaluating the OR trigger,
        # even when a multi-day trigger could otherwise be computed.
        "gate_log_return_available": _safe_float(row.get("log_return")) is not None,
        "gate_any_trigger": bool(result["fc_all_trigger"]),
        "gate_coin_above_own_sma": (
            cfg.fc_coin_above_own_sma_days <= 0 or (coin_sma is not None and close is not None and close > coin_sma)
        ),
        "gate_coin_min_30d_return": (
            cfg.fc_coin_min_30d_return <= -1.0 or (coin_30d is not None and coin_30d >= cfg.fc_coin_min_30d_return)
        ),
        "gate_btc_not_near_high": (
            cfg.fc_btc_not_near_high_window_days <= 0
            or btc_high_proximity is None
            or btc_high_proximity <= cfg.fc_btc_high_proximity_pct
        ),
        "gate_btc_must_be_near_high": (
            cfg.fc_btc_must_be_near_high_window_days <= 0
            or (btc_high_proximity is not None and btc_high_proximity >= cfg.fc_btc_must_be_near_high_pct)
        ),
        "gate_atr_cap": (cfg.fc_max_atr_pct >= 1.0 or (atr is not None and atr <= cfg.fc_max_atr_pct)),
        "gate_own_atr_percentile": (
            cfg.fc_max_atr_own_percentile >= 1.0 or atr is None or own_atr_quantile is None or atr <= own_atr_quantile
        ),
        "gate_min_volume_confirmation": (
            cfg.fc_min_vol_vs_median <= 0.0
            or (volume_multiple is not None and volume_multiple >= cfg.fc_min_vol_vs_median)
        ),
        "gate_max_volume_confirmation": (
            cfg.fc_max_vol_vs_median <= 0.0
            or (volume_multiple is not None and volume_multiple <= cfg.fc_max_vol_vs_median)
        ),
        "gate_max_coin_60d_return": (
            cfg.fc_max_coin_60d_return <= 0.0 or (coin_60d is not None and coin_60d <= cfg.fc_max_coin_60d_return)
        ),
        "gate_min_btc_sma_distance": (
            cfg.fc_min_btc_sma_dist <= -1.0
            or (btc_sma_distance is not None and btc_sma_distance >= cfg.fc_min_btc_sma_dist)
        ),
        "gate_max_btc_sma_distance": (
            cfg.fc_max_btc_sma_dist >= 1.0
            or (btc_sma_distance is not None and btc_sma_distance <= cfg.fc_max_btc_sma_dist)
        ),
        "gate_lsr": (not cfg.fc_lsr_filter or lsr is None or (cfg.fc_min_lsr <= lsr <= cfg.fc_max_lsr)),
        "gate_oi_rising": (not cfg.fc_require_oi_rising or (oi_change is not None and oi_change > cfg.fc_oi_chg_min)),
    }
    result.update(gates)

    checks: list[tuple[str, bool, str]] = [
        ("btc_month_regime", gates["gate_btc_month_regime"], "btc_month_regime"),
        ("fc_enabled", gates["gate_fc_enabled"], "fc_disabled"),
        ("in_universe", gates["gate_in_universe"], "not_in_universe"),
        ("btc_regime", gates["gate_btc_regime"], "btc_regime"),
        ("eth_regime", gates["gate_eth_regime"], "eth_regime"),
        (
            "volume_rank",
            gates["gate_volume_rank"],
            "volume_rank_missing" if rank is None else "volume_rank_above_max",
        ),
        ("log_return", gates["gate_log_return_available"], "log_return_missing"),
        ("trigger", gates["gate_any_trigger"], "no_trigger"),
        ("coin_sma", gates["gate_coin_above_own_sma"], "coin_not_above_own_sma"),
        ("coin_30d", gates["gate_coin_min_30d_return"], "coin_30d_return"),
        ("btc_not_near_high", gates["gate_btc_not_near_high"], "btc_near_high"),
        ("btc_must_near_high", gates["gate_btc_must_be_near_high"], "btc_not_near_high"),
        (
            "atr_cap",
            gates["gate_atr_cap"],
            "atr_missing" if atr is None else "atr_above_cap",
        ),
        ("own_atr", gates["gate_own_atr_percentile"], "atr_above_own_percentile"),
        ("min_volume", gates["gate_min_volume_confirmation"], "volume_below_min"),
        ("max_volume", gates["gate_max_volume_confirmation"], "volume_above_max"),
        ("coin_60d", gates["gate_max_coin_60d_return"], "coin_60d_return"),
        ("min_btc_sma", gates["gate_min_btc_sma_distance"], "btc_sma_distance_below_min"),
        ("max_btc_sma", gates["gate_max_btc_sma_distance"], "btc_sma_distance_above_max"),
        ("lsr", gates["gate_lsr"], "lsr_outside_band"),
        (
            "oi",
            gates["gate_oi_rising"],
            "oi_change_missing" if oi_change is None else "oi_not_rising",
        ),
    ]
    rejection = next((reason for _, passed, reason in checks if not passed), None)
    result["fc_independent_gate_pass"] = all(gates[name] for name in FC_GATE_COLUMNS)
    result["fc_classifier_gate_pass"] = gates["gate_btc_month_regime"] and result["fc_independent_gate_pass"]
    result["first_sequential_rejection_reason"] = rejection
    return result


def _common_anchor(
    bar_at: Callable[[int], Mapping[str, float | None] | None],
    *,
    signal_ts_ms: int,
) -> dict[str, Any]:
    entry_ts = signal_ts_ms + MS_PER_HOUR
    bar = bar_at(entry_ts)
    price = bar.get("close") if bar is not None else None
    return {
        "available": price is not None,
        "ts_ms": entry_ts if price is not None else None,
        "hour": 1 if price is not None else None,
        "price": price,
        "reason": "next_hour_close" if price is not None else "next_hour_bar_missing",
        "decision_complete": price is not None,
        "window_complete": price is not None,
    }


def _current_anchor(
    row: Mapping[str, Any],
    bar_at: Callable[[int], Mapping[str, float | None] | None],
    *,
    signal_ts_ms: int,
    signal_close: float | None,
    cfg: LongNativeConfig,
) -> dict[str, Any]:
    first_hour = max(1, cfg.entry_delay_hours)
    # The engine resolves and requires the ordinary delayed entry bar before it
    # enters the sniper branch.  A later retrace cannot rescue a missing initial
    # bar, even though the retrace scan itself would otherwise find it.
    initial_entry_ts = signal_ts_ms + cfg.entry_delay_hours * MS_PER_HOUR
    initial_entry_bar = bar_at(initial_entry_ts)
    if initial_entry_bar is None:
        return {
            "available": False,
            "ts_ms": None,
            "hour": None,
            "price": None,
            "reason": "initial_entry_bar_missing",
            "retrace_pct": None,
            "retrace_threshold": None,
            "decision_complete": False,
            "window_complete": False,
            "scan_first_hour": first_hour,
            "scan_end_hour": None,
        }
    if not cfg.fc_use_sniper_entry:
        entry_ts = initial_entry_ts
        price = initial_entry_bar.get("close")
        return {
            "available": price is not None,
            "ts_ms": entry_ts if price is not None else None,
            "hour": cfg.entry_delay_hours if price is not None else None,
            "price": price,
            "reason": "entry_delay" if price is not None else "entry_bar_missing",
            "retrace_pct": None,
            "retrace_threshold": None,
            "decision_complete": price is not None,
            "window_complete": price is not None,
            "scan_first_hour": cfg.entry_delay_hours,
            "scan_end_hour": cfg.entry_delay_hours,
        }

    deadline_hour = max(first_hour, cfg.fc_sniper_deadline_hours)
    if cfg.fc_sniper_use_atr_retrace:
        # Match the engine's ``safe_float(...) or 0.05`` fallback exactly.
        atr = _safe_float(row.get("atr_14d_pct")) or 0.05
        retrace_pct = cfg.fc_sniper_atr_mult * atr
    else:
        retrace_pct = cfg.fc_sniper_retrace_pct
    threshold = signal_close * (1.0 - retrace_pct) if signal_close is not None else None

    prefix_complete = True
    fired_hour: int | None = None
    scan_end_hour: int | None = None
    if signal_close is None:
        return {
            "available": False,
            "ts_ms": None,
            "hour": None,
            "price": None,
            "reason": "signal_bar_missing",
            "retrace_pct": retrace_pct,
            "retrace_threshold": None,
            "decision_complete": False,
            "window_complete": False,
            "scan_first_hour": first_hour,
            "scan_end_hour": None,
        }

    for hour in range(first_hour, deadline_hour + 1):
        scan_end_hour = hour
        bar = bar_at(signal_ts_ms + hour * MS_PER_HOUR)
        close = bar.get("close") if bar is not None else None
        if close is None:
            prefix_complete = False
            continue
        if threshold is not None and close <= threshold:
            fired_hour = hour
            break

    if fired_hour is not None:
        entry_ts = signal_ts_ms + fired_hour * MS_PER_HOUR
        fired_bar = bar_at(entry_ts)
        price = fired_bar.get("close") if fired_bar is not None else None
        return {
            "available": price is not None,
            "ts_ms": entry_ts,
            "hour": fired_hour,
            "price": price,
            "reason": "sniper_retrace",
            "retrace_pct": retrace_pct,
            "retrace_threshold": threshold,
            "decision_complete": prefix_complete,
            "window_complete": prefix_complete,
            "scan_first_hour": first_hour,
            "scan_end_hour": scan_end_hour,
        }

    if cfg.fc_sniper_skip_on_no_retrace:
        return {
            "available": False,
            "ts_ms": None,
            "hour": None,
            "price": None,
            "reason": "sniper_no_retrace_skip",
            "retrace_pct": retrace_pct,
            "retrace_threshold": threshold,
            "decision_complete": prefix_complete,
            "window_complete": prefix_complete,
            "scan_first_hour": first_hour,
            "scan_end_hour": scan_end_hour,
        }

    deadline_ts = signal_ts_ms + deadline_hour * MS_PER_HOUR
    deadline_bar = bar_at(deadline_ts)
    deadline_price = deadline_bar.get("close") if deadline_bar is not None else None
    if deadline_price is None:
        return {
            "available": False,
            "ts_ms": None,
            "hour": None,
            "price": None,
            "reason": "sniper_deadline_missing",
            "retrace_pct": retrace_pct,
            "retrace_threshold": threshold,
            "decision_complete": False,
            "window_complete": False,
            "scan_first_hour": first_hour,
            "scan_end_hour": scan_end_hour,
        }
    return {
        "available": True,
        "ts_ms": deadline_ts,
        "hour": deadline_hour,
        "price": deadline_price,
        "reason": "sniper_deadline_fallthrough",
        "retrace_pct": retrace_pct,
        "retrace_threshold": threshold,
        "decision_complete": prefix_complete,
        "window_complete": prefix_complete,
        "scan_first_hour": first_hour,
        "scan_end_hour": scan_end_hour,
    }


def _current_entry_prefix_fields(
    bar_at: Callable[[int], Mapping[str, float | None] | None],
    *,
    signal_ts_ms: int,
    current_anchor: Mapping[str, Any],
) -> dict[str, Any]:
    first_hour = current_anchor.get("scan_first_hour")
    end_hour = current_anchor.get("scan_end_hour")
    if first_hour is None or end_hour is None or int(end_hour) < int(first_hour):
        hours: list[int] = []
    else:
        hours = list(range(int(first_hour), int(end_hour) + 1))

    closes: list[float | None] = []
    lows: list[float | None] = []
    for hour in hours:
        bar = bar_at(signal_ts_ms + hour * MS_PER_HOUR)
        closes.append(bar.get("close") if bar is not None else None)
        lows.append(bar.get("low") if bar is not None else None)

    threshold = _safe_float(current_anchor.get("retrace_threshold"))
    close_triggered = current_anchor.get("reason") == "sniper_retrace"
    close_trigger_hour = int(current_anchor["hour"]) if close_triggered else None

    low_observed_first: int | None = None
    low_authoritative_first: int | None = None
    low_prefix_complete = True
    for hour, low in zip(hours, lows, strict=True):
        if low is None:
            low_prefix_complete = False
            continue
        if threshold is not None and low <= threshold:
            low_observed_first = hour
            if low_prefix_complete:
                low_authoritative_first = hour
            break
    if low_observed_first is not None:
        low_touched: bool | None = True
    elif threshold is not None and all(low is not None for low in lows):
        low_touched = False
    else:
        low_touched = None

    missing_close_hours = [hour for hour, close in zip(hours, closes, strict=True) if close is None]
    missing_hour_bitmask = 0
    for hour in missing_close_hours:
        if not 1 <= hour <= 6:
            raise ValueError(f"registered LONG entry bitmask supports h1..h6, got missing h{hour}")
        missing_hour_bitmask |= 1 << (hour - 1)
    return {
        "current_entry_scan_first_hour": hours[0] if hours else None,
        "current_entry_scan_end_hour": hours[-1] if hours else None,
        "current_entry_scan_missing_hour_bitmask": missing_hour_bitmask,
        "current_entry_close_triggered": close_triggered,
        "current_entry_close_trigger_first_hour": close_trigger_hour,
        "current_entry_intrabar_low_touch_nonfill": low_touched,
        "current_entry_intrabar_low_first_hour_nonfill": low_authoritative_first,
        "current_entry_intrabar_low_observed_first_hour_nonfill": low_observed_first,
    }


def _require_exit_geometry(
    row: Mapping[str, Any],
    cfg: LongNativeConfig,
    *,
    name: str,
) -> None:
    expected_stop, expected_take_profit = _fc_exit_params(dict(row), cfg)
    actual_stop = _strict_positive_price(row.get("fc_exit_stop_pct"), name=f"{name} fc_exit_stop_pct")
    actual_take_profit = _strict_positive_price(
        row.get("fc_exit_take_profit_pct"),
        name=f"{name} fc_exit_take_profit_pct",
    )
    if actual_stop >= 1.0:
        raise ValueError(f"{name} fc_exit_stop_pct must be below 1")
    if not _prices_match(actual_stop, expected_stop) or not _prices_match(
        actual_take_profit,
        expected_take_profit,
    ):
        raise ValueError(f"{name} exit percentages do not match the frozen config and signal row")
    hold = row.get("fc_exit_max_hold_hours")
    expected_hold = cfg.fc_max_hold_days * 24
    if isinstance(hold, bool) or not isinstance(hold, Integral) or int(hold) != expected_hold:
        raise ValueError(f"{name} fc_exit_max_hold_hours must equal frozen value {expected_hold}")


def _require_signal_bar_geometry(
    row: Mapping[str, Any],
    bar_at: Callable[[int], Mapping[str, float | None] | None],
    *,
    signal_ts_ms: int,
    name: str,
) -> None:
    source_bar = bar_at(signal_ts_ms)
    expected_present = source_bar is not None
    expected_complete = _bar_path_complete(source_bar)
    expected_close = source_bar.get("close") if source_bar is not None else None
    if row.get("signal_bar_present") is not expected_present:
        raise ValueError(f"{name} signal_bar_present does not match the exact signal-hour source bar")
    if row.get("signal_bar_complete") is not expected_complete:
        raise ValueError(f"{name} signal_bar_complete does not match the exact signal-hour source bar")
    actual_close_raw = row.get("signal_close_hourly")
    if expected_close is None:
        if actual_close_raw is not None:
            raise ValueError(f"{name} signal_close_hourly must be null when the signal bar is unavailable")
        return
    actual_close = _strict_positive_price(actual_close_raw, name=f"{name} signal_close_hourly")
    if not _prices_match(actual_close, float(expected_close)):
        raise ValueError(f"{name} signal_close_hourly does not match the exact signal-hour close")


def _derive_entry_policy_fields(
    row: Mapping[str, Any],
    bar_at: Callable[[int], Mapping[str, float | None] | None],
    *,
    signal_ts_ms: int,
    cfg: LongNativeConfig,
) -> dict[str, Any]:
    signal_close = _positive_float(row.get("signal_close_hourly"))
    current = _current_anchor(
        row,
        bar_at,
        signal_ts_ms=signal_ts_ms,
        signal_close=signal_close,
        cfg=cfg,
    )
    common = _common_anchor(bar_at, signal_ts_ms=signal_ts_ms)
    derived = _current_entry_prefix_fields(
        bar_at,
        signal_ts_ms=signal_ts_ms,
        current_anchor=current,
    )
    derived.update(_prefix_fields("common_entry", common))
    derived.update(_prefix_fields("current_entry", current))
    common_price = _positive_float(common.get("price"))
    current_price = _positive_float(current.get("price"))
    derived["entry_price_improvement"] = (
        common_price / current_price - 1.0 if common_price is not None and current_price is not None else None
    )
    derived["entry_delay_hours_vs_common"] = (
        int(current["hour"]) - int(common["hour"])
        if current.get("hour") is not None and common.get("hour") is not None
        else None
    )
    derived["current_entry_scan_prefix_complete"] = bool(current["decision_complete"])
    derived["current_entry_policy_available"] = bool(current["available"])
    derived["current_entry_missing_reason"] = None if current["available"] else str(current["reason"])
    for legacy_column in (
        "common_entry_decision_complete",
        "common_entry_window_complete",
        "current_entry_decision_complete",
        "current_entry_window_complete",
    ):
        derived.pop(legacy_column, None)
    derived["long_entry_policy_schema_version"] = _ENTRY_SCHEMA_VERSION
    return derived


def _stage_values_match(actual: Any, expected: Any) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, int):
        return isinstance(actual, Integral) and not isinstance(actual, bool) and int(actual) == expected
    if isinstance(expected, float):
        return (
            isinstance(actual, Real)
            and not isinstance(actual, bool)
            and math.isfinite(float(actual))
            and _prices_match(float(actual), expected)
        )
    return type(actual) is type(expected) and actual == expected


def _require_entry_policy_geometry(
    row: Mapping[str, Any],
    bar_at: Callable[[int], Mapping[str, float | None] | None],
    *,
    signal_ts_ms: int,
    cfg: LongNativeConfig,
    name: str,
) -> None:
    expected = _derive_entry_policy_fields(
        row,
        bar_at,
        signal_ts_ms=signal_ts_ms,
        cfg=cfg,
    )
    for column in _ENTRY_DERIVED_DTYPES:
        if column not in row:
            raise ValueError(f"{name} missing required anchor geometry column {column}")
        if not _stage_values_match(row[column], expected[column]):
            raise ValueError(
                f"{name} {column} does not match reconstructed frozen entry geometry: "
                f"expected {expected[column]!r}, got {row[column]!r}"
            )


def _path_labels(
    bars: Mapping[int, Mapping[str, float | None]],
    *,
    anchor_ts_ms: int | None,
    anchor_price: float | None,
    stop_pct: float,
    take_profit_pct: float,
    max_hold_hours: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if anchor_ts_ms is None or anchor_price is None:
        for horizon in _DIAGNOSTIC_PATH_HORIZONS_HOURS:
            out[f"{horizon}h_point_return"] = None
            out[f"{horizon}h_mfe"] = None
            out[f"{horizon}h_mae"] = None
            out[f"{horizon}h_observed_bars"] = 0
            out[f"{horizon}h_path_complete"] = False
        out.update(
            {
                "stop_price": None,
                "take_profit_price": None,
                "stop_observed_first_hour": None,
                "take_profit_observed_first_hour": None,
                "stop_first_hour": None,
                "take_profit_first_hour": None,
                "observed_first_passage_reason": None,
                "observed_first_passage_hour": None,
                "observed_same_bar_ambiguity": None,
                "first_passage_reason": None,
                "first_passage_hour": None,
                "first_passage_ts_ms": None,
                "same_bar_ambiguity": None,
                "first_passage_prefix_complete": False,
                "exit_path_complete": False,
            }
        )
        return out

    for horizon in _DIAGNOSTIC_PATH_HORIZONS_HOURS:
        observed = 0
        complete = True
        highs: list[float] = []
        lows: list[float] = []
        for hour in range(1, horizon + 1):
            bar = bars.get(anchor_ts_ms + hour * MS_PER_HOUR)
            if not _bar_path_complete(bar):
                complete = False
            else:
                observed += 1
            if bar is not None and bar.get("high") is not None:
                highs.append(float(bar["high"]))
            if bar is not None and bar.get("low") is not None:
                lows.append(float(bar["low"]))
        endpoint = bars.get(anchor_ts_ms + horizon * MS_PER_HOUR)
        endpoint_close = endpoint.get("close") if endpoint is not None else None
        out[f"{horizon}h_point_return"] = _return(endpoint_close, anchor_price)
        # Time zero is part of the excursion path.  MFE therefore cannot be
        # negative and signed MAE cannot be positive merely because every later
        # bar moved to one side of the entry.
        out[f"{horizon}h_mfe"] = max(0.0, max(highs) / anchor_price - 1.0) if highs else None
        out[f"{horizon}h_mae"] = min(0.0, min(lows) / anchor_price - 1.0) if lows else None
        out[f"{horizon}h_observed_bars"] = observed
        out[f"{horizon}h_path_complete"] = complete

    stop_price = anchor_price * (1.0 - stop_pct)
    take_profit_price = anchor_price * (1.0 + take_profit_pct)
    stop_observed: int | None = None
    tp_observed: int | None = None
    stop_authoritative: int | None = None
    tp_authoritative: int | None = None
    observed_reason: str | None = None
    observed_hour: int | None = None
    observed_ambiguous: bool | None = None
    authoritative_reason: str | None = None
    authoritative_hour: int | None = None
    authoritative_ambiguous: bool | None = None
    prefix_complete = True
    first_passage_prefix_complete = False
    exit_path_complete = True

    for hour in range(1, max_hold_hours + 1):
        bar = bars.get(anchor_ts_ms + hour * MS_PER_HOUR)
        if not _bar_range_valid(bar):
            prefix_complete = False
            exit_path_complete = False
            continue
        assert bar is not None
        stop_touched = float(bar["low"]) <= stop_price
        tp_touched = float(bar["high"]) >= take_profit_price
        if stop_touched and stop_observed is None:
            stop_observed = hour
            if prefix_complete:
                stop_authoritative = hour
        if tp_touched and tp_observed is None:
            tp_observed = hour
            if prefix_complete:
                tp_authoritative = hour
        if observed_reason is None and (stop_touched or tp_touched):
            observed_hour = hour
            observed_ambiguous = stop_touched and tp_touched
            # Conservative engine ordering: stop is checked before take profit.
            observed_reason = "stop" if stop_touched else "take_profit"
            if prefix_complete:
                authoritative_hour = hour
                authoritative_ambiguous = observed_ambiguous
                authoritative_reason = observed_reason
                first_passage_prefix_complete = True

    if observed_reason is None and exit_path_complete:
        authoritative_reason = "none"
        authoritative_ambiguous = False
        first_passage_prefix_complete = True

    out.update(
        {
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "stop_observed_first_hour": stop_observed,
            "take_profit_observed_first_hour": tp_observed,
            "stop_first_hour": stop_authoritative,
            "take_profit_first_hour": tp_authoritative,
            "observed_first_passage_reason": observed_reason,
            "observed_first_passage_hour": observed_hour,
            "observed_same_bar_ambiguity": observed_ambiguous,
            "first_passage_reason": authoritative_reason,
            "first_passage_hour": authoritative_hour,
            "first_passage_ts_ms": (
                anchor_ts_ms + authoritative_hour * MS_PER_HOUR if authoritative_hour is not None else None
            ),
            "same_bar_ambiguity": authoritative_ambiguous,
            "first_passage_prefix_complete": first_passage_prefix_complete,
            "exit_path_complete": exit_path_complete,
        }
    )
    return out


def _prefix_fields(prefix: str, values: Mapping[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{name}": value for name, value in values.items()}


def build_long_feature_tape(
    daily_features: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    config: LongNativeConfig,
) -> pl.DataFrame:
    """Build the strictly signal-time LONG feature tape.

    The input population is preserved without gate filtering.  The only hourly
    OHLC row consumed for a decision is the bar ending at that decision's signal
    timestamp.  Post-signal entry reconstruction belongs exclusively to
    :func:`append_long_entry_policy`.
    """

    cfg = _require_frozen_v11a_config(config, stage="build_long_feature_tape")
    # Invoke the same owner-local validator consumed by the central parity
    # manifest so its receipt describes an actual runtime guard, not a
    # separately asserted inspection result.
    long_population_runtime_parity_surface(cfg)
    _require_columns(daily_features, {"symbol", "ts_ms", "close"}, name="daily_features")
    _require_columns(
        hourly_bars,
        {"symbol", "ts_ms", "open", "high", "low", "close"},
        name="hourly_bars",
    )
    _reject_outcome_like_columns(daily_features, name="daily_features")
    _validate_primary_keys(
        daily_features,
        time_column="ts_ms",
        name="daily_features",
        grid_ms=MS_PER_DAY,
    )
    if "signal_ts_ms" in daily_features.columns:
        for row_number, row in enumerate(daily_features.select(["ts_ms", "signal_ts_ms"]).iter_rows(named=True)):
            raw_signal_ts_ms = row["signal_ts_ms"]
            if isinstance(raw_signal_ts_ms, bool) or not isinstance(raw_signal_ts_ms, Integral):
                raise ValueError(
                    f"daily_features row {row_number} signal_ts_ms must be an integer millisecond timestamp"
                )
            if int(raw_signal_ts_ms) != int(row["ts_ms"]):
                raise ValueError("daily_features signal_ts_ms must equal ts_ms exactly")
            signal_ts_ms = _strict_timestamp(
                raw_signal_ts_ms,
                name="daily_features",
                row_number=row_number,
                column="signal_ts_ms",
                grid_ms=MS_PER_DAY,
            )
            assert signal_ts_ms == int(row["ts_ms"])
        keyed_features = daily_features
    else:
        keyed_features = daily_features.with_columns(pl.col("ts_ms").alias("signal_ts_ms"))
    _reject_duplicate_keys(keyed_features, ["symbol", "signal_ts_ms"], name="daily_features")
    hourly_row_index = _hourly_row_index(hourly_bars)
    if daily_features.is_empty():
        empty = keyed_features.drop("ts_ms").with_columns(
            pl.lit(_FEATURE_SCHEMA_VERSION).cast(pl.String).alias("long_feature_tape_schema_version")
        )
        return _with_typed_columns(empty, _FEATURE_DERIVED_DTYPES)

    output: list[dict[str, Any]] = []
    for source_row in keyed_features.sort(["signal_ts_ms", "symbol"]).iter_rows(named=True):
        row = dict(source_row)
        symbol = str(row["symbol"])
        signal_ts_ms = int(row["signal_ts_ms"])
        row.pop("ts_ms", None)
        signal_bar = _indexed_bar(
            hourly_bars,
            hourly_row_index,
            symbol=symbol,
            bar_end_ts_ms=signal_ts_ms,
        )
        signal_close = signal_bar.get("close") if signal_bar is not None else None
        daily_close = _strict_positive_price(
            row.get("close"),
            name=f"daily_features {(symbol, signal_ts_ms)} close",
        )
        if signal_close is not None and not _prices_match(daily_close, float(signal_close)):
            raise ValueError(
                "daily_features close does not match the exact signal-hour close "
                f"within rel={SIGNAL_CLOSE_REL_TOLERANCE:g}, abs={SIGNAL_CLOSE_ABS_TOLERANCE:g} "
                f"for {(symbol, signal_ts_ms)}"
            )

        gate_fields = _fc_gate_diagnostics(row, cfg)
        detector_selected = detect_pattern_fomo_chase(row, cfg)
        if detector_selected != gate_fields["fc_independent_gate_pass"]:
            raise RuntimeError(
                f"FC gate reconstruction disagrees with detect_pattern_fomo_chase for {(symbol, signal_ts_ms)}"
            )
        pattern, classifier_stop, classifier_tp, classifier_hold_days = _classify_entry(row, cfg)
        classifier_selected = pattern == LONG_S02_CLASSIFIER_PATTERN
        rejection_reason = gate_fields["first_sequential_rejection_reason"]
        if rejection_reason is None:
            if classifier_selected:
                rejection_reason = "selected"
            elif pattern is not None:
                rejection_reason = f"preempted_by:{pattern}"
            else:
                rejection_reason = "classifier_mismatch"
        gate_fields["first_sequential_rejection_reason"] = rejection_reason

        stop_pct, take_profit_pct = _fc_exit_params(row, cfg)
        atr_available = _fc_atr_available(row, cfg)
        if not cfg.fc_use_atr_exits:
            exit_source = "fixed_config"
        elif atr_available:
            exit_source = "atr"
        else:
            exit_source = "fixed_fallback_missing_atr"

        row.update(gate_fields)
        row.update(
            {
                "simple_return_1d": _finite_expm1(row.get("log_return")),
                "simple_return_3d": _finite_expm1(row.get("pump_3d_log")),
                "simple_return_7d": _finite_expm1(row.get("pump_7d_log")),
                "signal_bar_present": signal_bar is not None,
                "signal_bar_complete": _bar_path_complete(signal_bar),
                "signal_close_hourly": signal_close,
                "fc_detector_selected": detector_selected,
                "classified_pattern": pattern,
                "classifier_selected": classifier_selected,
                "classifier_eligible": classifier_selected,
                "classifier_stop_pct": classifier_stop if classifier_selected else None,
                "classifier_take_profit_pct": classifier_tp if classifier_selected else None,
                "classifier_max_hold_days": classifier_hold_days if classifier_selected else None,
                "fc_exit_stop_pct": stop_pct,
                "fc_exit_take_profit_pct": take_profit_pct,
                "fc_exit_max_hold_hours": cfg.fc_max_hold_days * 24,
                "fc_atr_exit_available": atr_available,
                "fc_atr_fallback_used": cfg.fc_use_atr_exits and not atr_available,
                "fc_exit_param_source": exit_source,
                "long_feature_tape_schema_version": _FEATURE_SCHEMA_VERSION,
            }
        )
        output.append(row)

    result = pl.DataFrame(output, infer_schema_length=None).sort(["signal_ts_ms", "symbol"])
    return _with_typed_columns(result, _FEATURE_DERIVED_DTYPES)


def append_long_entry_policy(
    feature_tape: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    config: LongNativeConfig,
) -> pl.DataFrame:
    """Append explicit post-signal entry-policy reconstruction.

    This is not a signal-time feature step.  It spends the h1..deadline price
    prefix needed to reconstruct the common next-close anchor and the current
    close-based retrace/fall-through policy.  No bar after the first current-policy
    entry is read or written.
    """

    cfg = _require_frozen_v11a_config(config, stage="append_long_entry_policy")
    _require_columns(
        feature_tape,
        {
            "symbol",
            "signal_ts_ms",
            "signal_close_hourly",
            "signal_bar_present",
            "signal_bar_complete",
            "classifier_selected",
            "fc_exit_stop_pct",
            "fc_exit_take_profit_pct",
            "fc_exit_max_hold_hours",
            "long_feature_tape_schema_version",
        },
        name="feature_tape",
    )
    _require_columns(
        hourly_bars,
        {"symbol", "ts_ms", "open", "high", "low", "close"},
        name="hourly_bars",
    )
    _reject_outcome_like_columns(feature_tape, name="feature_tape")
    _validate_primary_keys(
        feature_tape,
        time_column="signal_ts_ms",
        name="feature_tape",
        grid_ms=MS_PER_DAY,
    )
    _reject_duplicate_keys(feature_tape, ["symbol", "signal_ts_ms"], name="feature_tape")
    _require_exact_stage_column(
        feature_tape,
        column="long_feature_tape_schema_version",
        expected=_FEATURE_SCHEMA_VERSION,
        name="feature_tape",
    )
    _require_exact_stage_column(
        feature_tape,
        column="fc_exit_max_hold_hours",
        expected=cfg.fc_max_hold_days * 24,
        name="feature_tape",
    )
    first_hour = max(1, cfg.entry_delay_hours)
    deadline_hour = max(first_hour, cfg.fc_sniper_deadline_hours)
    if cfg.fc_use_sniper_entry and (first_hour < 1 or deadline_hour > 6):
        raise ValueError("registered LONG entry-policy projection requires an h1..h6 scan")
    hourly_row_index = _hourly_row_index(hourly_bars)
    if feature_tape.is_empty():
        empty = feature_tape.clone().with_columns(
            pl.lit(_ENTRY_SCHEMA_VERSION).cast(pl.String).alias("long_entry_policy_schema_version")
        )
        return _with_typed_columns(empty, _ENTRY_DERIVED_DTYPES)

    output: list[dict[str, Any]] = []
    for source_row in feature_tape.sort(["signal_ts_ms", "symbol"]).iter_rows(named=True):
        row = dict(source_row)
        symbol = str(row["symbol"])
        signal_ts_ms = int(row["signal_ts_ms"])
        bar_cache: dict[int, Mapping[str, float | None] | None] = {}

        def bar_at(bar_end_ts_ms: int) -> Mapping[str, float | None] | None:
            if bar_end_ts_ms not in bar_cache:
                bar_cache[bar_end_ts_ms] = _indexed_bar(
                    hourly_bars,
                    hourly_row_index,
                    symbol=symbol,
                    bar_end_ts_ms=bar_end_ts_ms,
                )
            return bar_cache[bar_end_ts_ms]

        _require_signal_bar_geometry(
            row,
            bar_at,
            signal_ts_ms=signal_ts_ms,
            name=f"feature_tape {(symbol, signal_ts_ms)}",
        )
        _require_exit_geometry(
            row,
            cfg,
            name=f"feature_tape {(symbol, signal_ts_ms)}",
        )
        row.update(
            _derive_entry_policy_fields(
                row,
                bar_at,
                signal_ts_ms=signal_ts_ms,
                cfg=cfg,
            )
        )
        output.append(row)

    result = pl.DataFrame(output, infer_schema_length=None).sort(["signal_ts_ms", "symbol"])
    return _with_typed_columns(result, _ENTRY_DERIVED_DTYPES)


def _normalize_horizons(name: str, values: tuple[int, ...]) -> tuple[int, ...]:
    normalized: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must contain positive integer hours; got {value!r}")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} contains duplicate horizons: {values!r}")
    return tuple(sorted(normalized))


def _label_dtypes(
    point_horizons: tuple[int, ...],
    excursion_horizons: tuple[int, ...],
) -> dict[str, pl.DataType]:
    dtypes: dict[str, pl.DataType] = {}
    all_horizons = sorted(set(point_horizons) | set(excursion_horizons))
    for prefix in ("common", "current"):
        for horizon in all_horizons:
            base = f"{prefix}_{horizon}h"
            dtypes[f"{base}_endpoint_ts_ms"] = pl.Int64
            if horizon in point_horizons:
                dtypes[f"{base}_point_return"] = pl.Float64
            dtypes[f"{base}_point_available"] = pl.Boolean
            dtypes[f"{base}_observed_bars"] = pl.Int64
            dtypes[f"{base}_path_complete"] = pl.Boolean
            dtypes[f"{base}_missing_reason"] = pl.String
            dtypes[f"{base}_hourly_extrema_interval_censored"] = pl.Boolean
            if horizon in excursion_horizons:
                dtypes[f"{base}_mfe"] = pl.Float64
                dtypes[f"{base}_signed_mae"] = pl.Float64
                dtypes[f"{base}_adverse_magnitude"] = pl.Float64
        dtypes.update(
            {
                f"{prefix}_stop_price": pl.Float64,
                f"{prefix}_take_profit_price": pl.Float64,
                f"{prefix}_same_bar_stop_tp_ambiguity": pl.Boolean,
                f"{prefix}_label_complete": pl.Boolean,
                f"{prefix}_missing_path_reason": pl.String,
            }
        )
    dtypes.update(
        {
            "long_label_schema_version": pl.String,
            "long_label_point_horizons": pl.String,
            "long_label_excursion_horizons": pl.String,
        }
    )
    return dtypes


def _label_projection(
    frame: pl.DataFrame,
    *,
    dtypes: Mapping[str, pl.DataType],
    point_horizons: tuple[int, ...],
    excursion_horizons: tuple[int, ...],
    schema_version: str,
) -> pl.DataFrame:
    defaults: dict[str, Any] = {
        "long_label_schema_version": schema_version,
        "long_label_point_horizons": "|".join(map(str, point_horizons)),
        "long_label_excursion_horizons": "|".join(map(str, excursion_horizons)),
    }
    for name in dtypes:
        if name.endswith("_point_available") or name.endswith("_path_complete") or name.endswith("_label_complete"):
            defaults[name] = False
        elif name.endswith("_observed_bars"):
            defaults[name] = 0
        elif name.endswith("_hourly_extrema_interval_censored"):
            defaults[name] = True
    typed = _with_typed_columns(frame, dtypes, defaults=defaults)
    identity = [
        name for name in ("venue", "symbol", "signal_ts_ms", "canonical_instrument_id") if name in typed.columns
    ]
    return typed.select(identity + list(dtypes))


def _label_required_bar_ends(feature_tape: pl.DataFrame, max_horizon: int) -> set[tuple[str, int]]:
    required: set[tuple[str, int]] = set()
    if max_horizon <= 0:
        return required
    for row in feature_tape.select(["symbol", "common_entry_ts_ms", "current_entry_ts_ms"]).iter_rows(named=True):
        symbol = str(row["symbol"])
        for anchor_column in ("common_entry_ts_ms", "current_entry_ts_ms"):
            anchor = row[anchor_column]
            if anchor is None:
                continue
            anchor_ts = int(anchor)
            for hour in range(1, max_horizon + 1):
                required.add((symbol, anchor_ts + hour * MS_PER_HOUR))
    return required


def _label_bars_for_row(
    row: Mapping[str, Any],
    hourly_bars: pl.DataFrame,
    row_index: Mapping[tuple[str, int], int | None],
    *,
    max_horizon: int,
) -> dict[int, Mapping[str, float | None]]:
    symbol = str(row["symbol"])
    required_ends: set[int] = set()
    for anchor_column in ("common_entry_ts_ms", "current_entry_ts_ms"):
        anchor = row.get(anchor_column)
        if anchor is None:
            continue
        anchor_ts_ms = int(anchor)
        required_ends.update(anchor_ts_ms + hour * MS_PER_HOUR for hour in range(1, max_horizon + 1))
    bars: dict[int, Mapping[str, float | None]] = {}
    for bar_end_ts_ms in sorted(required_ends):
        bar = _indexed_bar(
            hourly_bars,
            row_index,
            symbol=symbol,
            bar_end_ts_ms=bar_end_ts_ms,
        )
        if bar is not None:
            bars[bar_end_ts_ms] = bar
    return bars


def _minimal_anchor_labels(
    row: Mapping[str, Any],
    bars: Mapping[int, Mapping[str, float | None]],
    *,
    prefix: str,
    point_horizons: tuple[int, ...],
    excursion_horizons: tuple[int, ...],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    anchor_ts_raw = row.get(f"{prefix}_entry_ts_ms")
    anchor_price = _positive_float(row.get(f"{prefix}_entry_price"))
    anchor_ts = int(anchor_ts_raw) if anchor_ts_raw is not None else None
    missing_reasons: list[str] = []
    all_horizons = tuple(sorted(set(point_horizons) | set(excursion_horizons)))
    anchor_available = anchor_ts is not None and anchor_price is not None

    for horizon in all_horizons:
        endpoint_ts = anchor_ts + horizon * MS_PER_HOUR if anchor_ts is not None else None
        endpoint = bars.get(endpoint_ts) if endpoint_ts is not None else None
        endpoint_close = endpoint.get("close") if endpoint is not None else None
        highs: list[float] = []
        lows: list[float] = []
        observed = 0
        if anchor_available:
            assert anchor_ts is not None
            for hour in range(1, horizon + 1):
                bar = bars.get(anchor_ts + hour * MS_PER_HOUR)
                if not _bar_path_complete(bar):
                    continue
                assert bar is not None
                observed += 1
                highs.append(float(bar["high"]))
                lows.append(float(bar["low"]))
        complete = anchor_available and observed == horizon
        point_available = anchor_available and endpoint_close is not None
        horizon_reasons: list[str] = []
        if not anchor_available:
            horizon_reasons.append(f"anchor_unavailable:{row.get(f'{prefix}_entry_reason')}")
        else:
            if endpoint_close is None:
                horizon_reasons.append("endpoint_close_missing")
            if not complete:
                horizon_reasons.append(f"path_incomplete:{observed}/{horizon}")

        out[f"{horizon}h_endpoint_ts_ms"] = endpoint_ts
        if horizon in point_horizons:
            out[f"{horizon}h_point_return"] = _return(endpoint_close, anchor_price) if point_available else None
        out[f"{horizon}h_point_available"] = point_available
        out[f"{horizon}h_observed_bars"] = observed
        out[f"{horizon}h_path_complete"] = complete
        out[f"{horizon}h_missing_reason"] = "+".join(horizon_reasons) or None
        out[f"{horizon}h_hourly_extrema_interval_censored"] = True
        if horizon_reasons:
            missing_reasons.append(f"{horizon}h:{'+'.join(horizon_reasons)}")
        if horizon in excursion_horizons:
            mfe = max(0.0, max(highs) / anchor_price - 1.0) if complete and highs and anchor_price else None
            signed_mae = min(0.0, min(lows) / anchor_price - 1.0) if complete and lows and anchor_price else None
            out[f"{horizon}h_mfe"] = mfe
            out[f"{horizon}h_signed_mae"] = signed_mae
            out[f"{horizon}h_adverse_magnitude"] = -signed_mae if signed_mae is not None else None

    max_horizon = max(all_horizons, default=0)
    stop_pct = _safe_float(row.get("fc_exit_stop_pct"))
    take_profit_pct = _safe_float(row.get("fc_exit_take_profit_pct"))
    stop_price = (
        anchor_price * (1.0 - stop_pct)
        if anchor_price is not None and stop_pct is not None and 0.0 <= stop_pct < 1.0
        else None
    )
    take_profit_price = (
        anchor_price * (1.0 + take_profit_pct)
        if anchor_price is not None and take_profit_pct is not None and take_profit_pct >= 0.0
        else None
    )
    out["stop_price"] = stop_price
    out["take_profit_price"] = take_profit_price
    ambiguity_path_complete = True
    ambiguity_observed = False
    if anchor_ts is not None and max_horizon > 0 and stop_price is not None and take_profit_price is not None:
        for hour in range(1, max_horizon + 1):
            bar = bars.get(anchor_ts + hour * MS_PER_HOUR)
            if not _bar_path_complete(bar):
                ambiguity_path_complete = False
                continue
            assert bar is not None
            if float(bar["low"]) <= stop_price and float(bar["high"]) >= take_profit_price:
                ambiguity_observed = True
        if ambiguity_observed:
            ambiguity: bool | None = True
        elif ambiguity_path_complete:
            ambiguity = False
        else:
            ambiguity = None
            missing_reasons.append("ambiguity_path")
    else:
        ambiguity = None
        if max_horizon > 0:
            missing_reasons.append("ambiguity_levels")
    out["same_bar_stop_tp_ambiguity"] = ambiguity
    out["label_complete"] = not missing_reasons
    out["missing_path_reason"] = "+".join(dict.fromkeys(missing_reasons)) or None
    return out


def append_long_path_labels(
    entry_tape: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    config: LongNativeConfig,
    point_horizons: tuple[int, ...] = DEFAULT_POINT_HORIZONS,
    excursion_horizons: tuple[int, ...] = DEFAULT_EXCURSION_HORIZONS,
) -> pl.DataFrame:
    """Append frozen minimal forward labels to an entry-policy tape."""

    cfg = _require_frozen_v11a_config(config, stage="append_long_path_labels")
    points = _normalize_horizons("point_horizons", point_horizons)
    excursions = _normalize_horizons("excursion_horizons", excursion_horizons)
    if points != DEFAULT_POINT_HORIZONS or excursions != DEFAULT_EXCURSION_HORIZONS:
        raise ValueError(
            "append_long_path_labels only permits frozen horizons "
            f"{DEFAULT_POINT_HORIZONS}/{DEFAULT_EXCURSION_HORIZONS}; use "
            "append_long_exploratory_path_labels for arbitrary horizons"
        )
    return _append_long_path_labels_impl(
        entry_tape,
        hourly_bars,
        cfg,
        point_horizons=points,
        excursion_horizons=excursions,
        schema_version=_LABEL_SCHEMA_VERSION,
        stage_name="append_long_path_labels",
    )


def append_long_exploratory_path_labels(
    entry_tape: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    config: LongNativeConfig,
    *,
    point_horizons: tuple[int, ...],
    excursion_horizons: tuple[int, ...],
) -> pl.DataFrame:
    """Append explicitly exploratory arbitrary-horizon labels.

    This API still validates the frozen entry tape and its exact v11a geometry,
    but it emits a non-frozen schema version and cannot masquerade as S04.
    """

    cfg = _require_frozen_v11a_config(config, stage="append_long_exploratory_path_labels")
    points = _normalize_horizons("point_horizons", point_horizons)
    excursions = _normalize_horizons("excursion_horizons", excursion_horizons)
    return _append_long_path_labels_impl(
        entry_tape,
        hourly_bars,
        cfg,
        point_horizons=points,
        excursion_horizons=excursions,
        schema_version=EXPLORATORY_LABEL_SCHEMA_VERSION,
        stage_name="append_long_exploratory_path_labels",
    )


def _append_long_path_labels_impl(
    entry_tape: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    cfg: LongNativeConfig,
    *,
    point_horizons: tuple[int, ...],
    excursion_horizons: tuple[int, ...],
    schema_version: str,
    stage_name: str,
) -> pl.DataFrame:
    points = point_horizons
    excursions = excursion_horizons
    _require_columns(
        entry_tape,
        set(_ENTRY_DERIVED_DTYPES)
        | {
            "symbol",
            "signal_ts_ms",
            "long_feature_tape_schema_version",
            "signal_bar_present",
            "signal_bar_complete",
            "signal_close_hourly",
            "fc_exit_stop_pct",
            "fc_exit_take_profit_pct",
            "fc_exit_max_hold_hours",
        },
        name="entry_tape",
    )
    _require_columns(
        hourly_bars,
        {"symbol", "ts_ms", "open", "high", "low", "close"},
        name="hourly_bars",
    )
    _reject_outcome_like_columns(
        entry_tape,
        name="entry_tape",
        allowed=set(_ENTRY_DERIVED_DTYPES),
    )
    _validate_primary_keys(
        entry_tape,
        time_column="signal_ts_ms",
        name="entry_tape",
        grid_ms=MS_PER_DAY,
    )
    _reject_duplicate_keys(entry_tape, ["symbol", "signal_ts_ms"], name="entry_tape")
    _require_exact_stage_column(
        entry_tape,
        column="long_feature_tape_schema_version",
        expected=_FEATURE_SCHEMA_VERSION,
        name="entry_tape",
    )
    _require_exact_stage_column(
        entry_tape,
        column="long_entry_policy_schema_version",
        expected=_ENTRY_SCHEMA_VERSION,
        name="entry_tape",
    )
    _require_exact_stage_column(
        entry_tape,
        column="fc_exit_max_hold_hours",
        expected=cfg.fc_max_hold_days * 24,
        name="entry_tape",
    )
    hourly_row_index = _hourly_row_index(hourly_bars)
    label_dtypes = _label_dtypes(points, excursions)
    if entry_tape.is_empty():
        return _label_projection(
            entry_tape.clone(),
            dtypes=label_dtypes,
            point_horizons=points,
            excursion_horizons=excursions,
            schema_version=schema_version,
        )

    max_horizon = max((*points, *excursions), default=0)

    output: list[dict[str, Any]] = []
    for source_row in entry_tape.sort(["signal_ts_ms", "symbol"]).iter_rows(named=True):
        row = dict(source_row)
        symbol = str(row["symbol"])
        signal_ts_ms = int(row["signal_ts_ms"])
        bar_cache: dict[int, Mapping[str, float | None] | None] = {}

        def bar_at(bar_end_ts_ms: int) -> Mapping[str, float | None] | None:
            if bar_end_ts_ms not in bar_cache:
                bar_cache[bar_end_ts_ms] = _indexed_bar(
                    hourly_bars,
                    hourly_row_index,
                    symbol=symbol,
                    bar_end_ts_ms=bar_end_ts_ms,
                )
            return bar_cache[bar_end_ts_ms]

        _require_signal_bar_geometry(
            row,
            bar_at,
            signal_ts_ms=signal_ts_ms,
            name=f"{stage_name} entry_tape {(symbol, signal_ts_ms)}",
        )
        _require_exit_geometry(
            row,
            cfg,
            name=f"{stage_name} entry_tape {(symbol, signal_ts_ms)}",
        )
        _require_entry_policy_geometry(
            row,
            bar_at,
            signal_ts_ms=signal_ts_ms,
            cfg=cfg,
            name=f"{stage_name} entry_tape {(symbol, signal_ts_ms)}",
        )
        bars = _label_bars_for_row(
            row,
            hourly_bars,
            hourly_row_index,
            max_horizon=max_horizon,
        )
        for prefix in ("common", "current"):
            row.update(
                _prefix_fields(
                    prefix,
                    _minimal_anchor_labels(
                        row,
                        bars,
                        prefix=prefix,
                        point_horizons=points,
                        excursion_horizons=excursions,
                    ),
                )
            )
        row["long_label_schema_version"] = schema_version
        row["long_label_point_horizons"] = "|".join(map(str, points))
        row["long_label_excursion_horizons"] = "|".join(map(str, excursions))
        output.append(row)
    result = pl.DataFrame(output, infer_schema_length=None).sort(["signal_ts_ms", "symbol"])
    return _label_projection(
        result,
        dtypes=label_dtypes,
        point_horizons=points,
        excursion_horizons=excursions,
        schema_version=schema_version,
    )


def _append_long_first_passage_diagnostics(
    entry_tape: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    config: LongNativeConfig | None = None,
) -> pl.DataFrame:
    """Explicit private diagnostic; not part of the frozen A0 label contract."""

    cfg = config or LongNativeConfig()
    max_hold = cfg.fc_max_hold_days * 24
    diagnostic_hourly = _hourly_subset_for_ends(
        hourly_bars,
        _label_required_bar_ends(entry_tape, max_hold),
    )
    _reject_duplicate_keys(diagnostic_hourly, ["symbol", "ts_ms"], name="diagnostic_hourly_bars")
    bars_by_symbol = _hourly_lookup(diagnostic_hourly)
    output: list[dict[str, Any]] = []
    for source_row in entry_tape.sort(["signal_ts_ms", "symbol"]).iter_rows(named=True):
        row = dict(source_row)
        bars = bars_by_symbol.get(str(row["symbol"]), {})
        for prefix in ("common", "current"):
            labels = _path_labels(
                bars,
                anchor_ts_ms=row.get(f"{prefix}_entry_ts_ms"),
                anchor_price=row.get(f"{prefix}_entry_price"),
                stop_pct=float(row["fc_exit_stop_pct"]),
                take_profit_pct=float(row["fc_exit_take_profit_pct"]),
                max_hold_hours=max_hold,
            )
            diagnostic = {
                key: value
                for key, value in labels.items()
                if "passage" in key
                or "same_bar" in key
                or key.startswith("stop_")
                or key.startswith("take_profit_")
                or key == "exit_path_complete"
            }
            row.update(_prefix_fields(f"diagnostic_{prefix}", diagnostic))
        output.append(row)
    return pl.DataFrame(output, infer_schema_length=None).sort(["signal_ts_ms", "symbol"])


def build_long_population_tape(
    daily_features: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    config: LongNativeConfig,
    *,
    point_horizons: tuple[int, ...] | None = None,
    excursion_horizons: tuple[int, ...] | None = None,
) -> pl.DataFrame:
    """Deprecated convenience wrapper requiring an explicit outcome contract."""

    if point_horizons is None or excursion_horizons is None:
        raise TypeError(
            "build_long_population_tape no longer appends labels implicitly; use "
            "build_long_feature_tape, append_long_entry_policy, then "
            "append_long_path_labels, or pass both point_horizons and "
            "excursion_horizons explicitly"
        )
    warnings.warn(
        "build_long_population_tape is deprecated; split feature and label stages",
        DeprecationWarning,
        stacklevel=2,
    )
    features = build_long_feature_tape(daily_features, hourly_bars, config)
    entries = append_long_entry_policy(features, hourly_bars, config)
    return append_long_path_labels(
        entries,
        hourly_bars,
        config,
        point_horizons=point_horizons,
        excursion_horizons=excursion_horizons,
    )


# The discoverable scout alias is deliberately outcome blind.
build_long_population_scout = build_long_feature_tape


__all__ = [
    "DEFAULT_EXCURSION_HORIZONS",
    "DEFAULT_POINT_HORIZONS",
    "EXPLORATORY_LABEL_SCHEMA_VERSION",
    "FC_GATE_COLUMNS",
    "LONG_PATTERN_TOGGLE_FIELDS",
    "LONG_S02_CLASSIFIER_PATTERN",
    "LONG_TRIGGER_AND_EXIT_PROFILE_FIELDS",
    "SIGNAL_CLOSE_ABS_TOLERANCE",
    "SIGNAL_CLOSE_REL_TOLERANCE",
    "append_long_entry_policy",
    "append_long_exploratory_path_labels",
    "append_long_path_labels",
    "build_long_feature_tape",
    "build_long_population_scout",
    "build_long_population_tape",
    "long_population_runtime_parity_surface",
]
