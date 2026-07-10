"""Mechanical, outcome-blind source sidecars for the registered LONG-A0 tape.

The source-context adapter validates caller-owned sidecars, but validation alone
cannot establish where those rows came from.  This module closes that narrower
gap: it derives the three registered sidecars from raw hourly OHLC and the exact
daily feature population produced under ``_v11a_long_native_config``.

Hourly timestamps are bar-open times and daily feature timestamps are closed
UTC-day bar-end times.  A derived availability timestamp of ``signal_ts_ms``
means only that the source event is causally computable once that daily boundary
has closed.  It is deliberately not a claim about historical vendor publication,
root ingestion, refresh cadence, or operational processing latency.

Missing BTC/ETH history is never converted into observed market context.  The
production feature builder currently uses ``False``/``0.0`` fallbacks for those
missing joins; this builder verifies that fallback as parity evidence, then emits
the registered honest representation: null values, a false availability flag,
and a null pass value.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any

import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR, calendar_roll, calendar_shift
from .long_native import (
    BTC_MONTH_REGIME_MODE_DAILY_30D,
    BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH,
    BTC_MONTH_REGIME_MODE_SMART_MONTH,
    LongNativeConfig,
    _btc_hourly_exact_month_context,
)
from .long_native_event_demo import _v11a_long_native_config
from .momentum_signals import daily_bars
from .strategy_overhaul_config_identity import (
    assert_stage_config_matches_identity,
    canonical_json_sha256,
    derive_long_a0_config_identity,
)
from .strategy_overhaul_long_context import (
    BTC_MONTH_CONTEXT_SCHEMA,
    REGIME_CONTEXT_SCHEMA,
    SOURCE_AVAILABILITY_SCHEMA,
)


LONG_A0_SIDECAR_SCHEMA_VERSION = "long_a0_source_sidecars_v1"
LONG_A0_SIDECAR_AVAILABILITY_SEMANTICS = (
    "closed_daily_source_event_causal_computability_not_vendor_publication_or_ingestion"
)

RAW_HOURLY_OHLC_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "ts_ms": pl.Int64,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
    }
)

DAILY_CONTEXT_FEATURE_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "ts_ms": pl.Int64,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "hourly_bars": pl.UInt32,
        "regime_on": pl.Boolean,
        "eth_regime_on": pl.Boolean,
        "btc_sma_dist": pl.Float64,
        "btc_month_ret_30d": pl.Float64,
        "btc_month_ret_exact": pl.Float64,
        "btc_month_ret_smart": pl.Float64,
        "btc_month_ret_exact_source_ts_ms": pl.Int64,
    }
)

_KEY_COLUMNS = ("symbol", "ts_ms")
_DAILY_OHLC_COLUMNS = ("open", "high", "low", "close")
_FLOAT_REL_TOLERANCE = 1e-12
_FLOAT_ABS_TOLERANCE = 1e-12
_CONTEXT_ROW_ORDER = ("signal_ts_ms",)

_MONTH_VALUE_COLUMN_BY_MODE = MappingProxyType(
    {
        BTC_MONTH_REGIME_MODE_DAILY_30D: "btc_month_ret_30d",
        BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH: "btc_month_ret_exact",
        BTC_MONTH_REGIME_MODE_SMART_MONTH: "btc_month_ret_smart",
    }
)
_MONTH_PARITY_COLUMNS_BY_MODE = MappingProxyType(
    {
        BTC_MONTH_REGIME_MODE_DAILY_30D: ("btc_month_ret_30d",),
        BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH: (
            "btc_month_ret_exact",
            "btc_month_ret_exact_source_ts_ms",
        ),
        BTC_MONTH_REGIME_MODE_SMART_MONTH: (
            "btc_month_ret_30d",
            "btc_month_ret_exact",
            "btc_month_ret_smart",
            "btc_month_ret_exact_source_ts_ms",
        ),
    }
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


class LongA0SidecarError(ValueError):
    """A config, source, population, timing, or feature-parity invariant failed."""


@dataclass(frozen=True, slots=True)
class LongA0SidecarBundle:
    """The exact three sidecars plus a deterministic, outcome-free receipt."""

    source_availability: pl.DataFrame
    regime_context: pl.DataFrame
    btc_month_context: pl.DataFrame
    receipt: Mapping[str, Any]


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
        raise LongA0SidecarError(f"{name} contains outcome-like columns: {forbidden}")


def _require_columns_and_dtypes(
    frame: pl.DataFrame,
    expected: Mapping[str, pl.DataType],
    *,
    name: str,
) -> None:
    missing = sorted(set(expected) - set(frame.columns))
    if missing:
        raise LongA0SidecarError(f"{name} missing required columns: {missing}")
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise LongA0SidecarError(f"{name} has invalid required dtypes: {mismatched}")


def _require_exact_v11a_config(config: LongNativeConfig) -> LongNativeConfig:
    if not isinstance(config, LongNativeConfig):
        raise TypeError("LONG-A0 sidecar construction requires an explicit LongNativeConfig")
    expected = _v11a_long_native_config()
    if config == expected:
        return config
    mismatches = [
        field.name
        for field in fields(LongNativeConfig)
        if getattr(config, field.name) != getattr(expected, field.name)
    ]
    preview = ", ".join(mismatches[:8])
    suffix = "..." if len(mismatches) > 8 else ""
    raise LongA0SidecarError(
        "LONG-A0 sidecars require the exact _v11a_long_native_config; "
        f"mismatched fields: {preview}{suffix}"
    )


def _float_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        left_float = float(left)
        right_float = float(right)
    except (TypeError, ValueError):
        return False
    return math.isfinite(left_float) and math.isfinite(right_float) and math.isclose(
        left_float,
        right_float,
        rel_tol=_FLOAT_REL_TOLERANCE,
        abs_tol=_FLOAT_ABS_TOLERANCE,
    )


def _validate_daily_features(frame: pl.DataFrame, config: LongNativeConfig) -> pl.DataFrame:
    _reject_outcome_columns(frame, name="daily_features")
    _require_columns_and_dtypes(frame, DAILY_CONTEXT_FEATURE_SCHEMA, name="daily_features")
    if "signal_ts_ms" in frame.columns:
        if frame.schema["signal_ts_ms"] != pl.Int64:
            raise LongA0SidecarError("daily_features.signal_ts_ms alias must have Int64 dtype")
        bad_alias = frame.filter(
            pl.col("signal_ts_ms").is_null() | (pl.col("signal_ts_ms") != pl.col("ts_ms"))
        )
        if not bad_alias.is_empty():
            raise LongA0SidecarError("daily_features.signal_ts_ms alias must equal ts_ms exactly")

    invalid_keys = frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("ts_ms").is_null()
        | (pl.col("ts_ms") <= 0)
        | ((pl.col("ts_ms") % MS_PER_DAY) != 0)
    )
    if not invalid_keys.is_empty():
        raise LongA0SidecarError("daily_features contains invalid symbol/closed-daily keys")
    duplicates = frame.group_by(list(_KEY_COLUMNS)).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise LongA0SidecarError("daily_features contains duplicate (symbol,ts_ms) keys")

    invalid_ohlc = frame.filter(
        pl.any_horizontal(
            pl.col(column).is_null() | ~pl.col(column).is_finite() | (pl.col(column) <= 0.0)
            for column in _DAILY_OHLC_COLUMNS
        )
        | (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("high") < pl.col("low"))
        | pl.col("hourly_bars").is_null()
        | (pl.col("hourly_bars") < 20)
    )
    if not invalid_ohlc.is_empty():
        raise LongA0SidecarError("daily_features contains invalid daily OHLC/bar-count source values")

    invalid_builder_context = frame.filter(
        pl.col("regime_on").is_null()
        | pl.col("eth_regime_on").is_null()
        | pl.col("btc_sma_dist").is_null()
        | ~pl.col("btc_sma_dist").is_finite()
    )
    if not invalid_builder_context.is_empty():
        raise LongA0SidecarError("daily_features contains invalid production BTC/ETH context fallbacks")

    for column in ("btc_month_ret_30d", "btc_month_ret_exact", "btc_month_ret_smart"):
        invalid = frame.filter(pl.col(column).is_not_null() & ~pl.col(column).is_finite())
        if not invalid.is_empty():
            raise LongA0SidecarError(f"daily_features.{column} contains non-finite values")
    invalid_source_ts = frame.filter(
        pl.col("btc_month_ret_exact_source_ts_ms").is_not_null()
        & (
            (pl.col("btc_month_ret_exact_source_ts_ms") < 0)
            | ((pl.col("btc_month_ret_exact_source_ts_ms") % MS_PER_HOUR) != 0)
            | (pl.col("btc_month_ret_exact_source_ts_ms") >= pl.col("ts_ms"))
        )
    )
    if not invalid_source_ts.is_empty():
        raise LongA0SidecarError("daily_features contains invalid BTC exact-month source timestamps")

    if "gate_btc_month_regime" in frame.columns:
        if frame.schema["gate_btc_month_regime"] != pl.Boolean:
            raise LongA0SidecarError("daily_features.gate_btc_month_regime must have Boolean dtype")
        bad_gate = frame.filter(
            pl.col("gate_btc_month_regime").is_null()
            | (pl.col("gate_btc_month_regime") != (config.btc_month_regime_gate == "off"))
        )
        if not bad_gate.is_empty():
            raise LongA0SidecarError(
                "daily_features gate_btc_month_regime disagrees with the registered gate-off config"
            )
    return frame.sort(["ts_ms", "symbol"])


def _context_warmup_days(config: LongNativeConfig) -> int:
    spans = [int(config.regime_sma_days)]
    if config.btc_month_regime_mode in (
        BTC_MONTH_REGIME_MODE_DAILY_30D,
        BTC_MONTH_REGIME_MODE_SMART_MONTH,
    ):
        spans.append(int(config.btc_month_regime_lookback_days))
    if config.btc_month_regime_mode in (
        BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH,
        BTC_MONTH_REGIME_MODE_SMART_MONTH,
    ):
        spans.append(int(math.ceil(float(config.btc_month_regime_month_days))) + 1)
    # One full source day is needed before a shifted daily close.  A second day
    # makes the non-integral exact-month cutoff conservative without changing any
    # signal-time computation.
    return max(spans) + 2


def _validate_consumed_hourly(frame: pl.DataFrame) -> None:
    invalid = frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("ts_ms").is_null()
        | (pl.col("ts_ms") < 0)
        | ((pl.col("ts_ms") % MS_PER_HOUR) != 0)
        | pl.any_horizontal(
            pl.col(column).is_null() | ~pl.col(column).is_finite() | (pl.col(column) <= 0.0)
            for column in _DAILY_OHLC_COLUMNS
        )
        | (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("high") < pl.col("low"))
    )
    if not invalid.is_empty():
        raise LongA0SidecarError("consumed hourly source contains invalid key/OHLC geometry")
    duplicates = frame.group_by(list(_KEY_COLUMNS)).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise LongA0SidecarError("consumed hourly source contains duplicate (symbol,ts_ms) keys")


def _consumed_hourly_source(
    hourly_bars: pl.DataFrame,
    features: pl.DataFrame,
    config: LongNativeConfig,
) -> tuple[pl.DataFrame, int | None]:
    _reject_outcome_columns(hourly_bars, name="hourly_bars")
    _require_columns_and_dtypes(hourly_bars, RAW_HOURLY_OHLC_SCHEMA, name="hourly_bars")
    projected = hourly_bars.select(tuple(RAW_HOURLY_OHLC_SCHEMA))
    if features.is_empty():
        return projected.head(0), None

    min_signal_ts = int(features["ts_ms"].min())
    max_signal_ts = int(features["ts_ms"].max())
    context_start_ts = max(0, min_signal_ts - _context_warmup_days(config) * MS_PER_DAY)
    relevant_symbols = set(features["symbol"].unique().to_list()) | {config.regime_symbol, "ETHUSDT"}
    candidates = projected.filter(
        pl.col("symbol").is_in(sorted(relevant_symbols))
        & (pl.col("ts_ms") >= context_start_ts)
        & (pl.col("ts_ms") < max_signal_ts)
    )
    _validate_consumed_hourly(candidates)

    feature_keys = features.select(
        "symbol",
        pl.col("ts_ms").alias("__feature_signal_ts_ms"),
    )
    population_source = (
        candidates.with_columns(
            ((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)) + MS_PER_DAY).alias(
                "__feature_signal_ts_ms"
            )
        )
        .join(feature_keys, on=["symbol", "__feature_signal_ts_ms"], how="semi")
        .select(tuple(RAW_HOURLY_OHLC_SCHEMA))
    )
    context_source = candidates.filter(
        pl.col("symbol").is_in([config.regime_symbol, "ETHUSDT"])
    )
    consumed = (
        pl.concat([population_source, context_source], how="vertical")
        .unique(subset=list(_KEY_COLUMNS), keep="first")
        .sort(["symbol", "ts_ms"])
    )
    return consumed, context_start_ts


def _assert_exact_population_keys(actual: pl.DataFrame, features: pl.DataFrame, *, name: str) -> None:
    expected = features.select("symbol", pl.col("ts_ms").alias("signal_ts_ms")).sort(
        ["signal_ts_ms", "symbol"]
    )
    selected = actual.select("symbol", "signal_ts_ms").sort(["signal_ts_ms", "symbol"])
    if selected.height == expected.height and selected.equals(expected):
        return
    missing = expected.join(selected, on=["symbol", "signal_ts_ms"], how="anti")
    extra = selected.join(expected, on=["symbol", "signal_ts_ms"], how="anti")
    raise LongA0SidecarError(
        f"{name} keys must exactly equal the daily feature population; "
        f"missing={missing.head(5).to_dicts()}, extra={extra.head(5).to_dicts()}"
    )


def _assert_daily_source_parity(features: pl.DataFrame, rebuilt_daily: pl.DataFrame) -> None:
    raw_daily = rebuilt_daily.select(
        "symbol",
        pl.col("ts_ms"),
        *(pl.col(column).alias(f"__raw_{column}") for column in _DAILY_OHLC_COLUMNS),
        pl.col("hourly_bars").alias("__raw_hourly_bars"),
    )
    joined = features.join(raw_daily, on=["symbol", "ts_ms"], how="left")
    missing = joined.filter(pl.col("__raw_close").is_null())
    if not missing.is_empty():
        raise LongA0SidecarError(
            "raw hourly OHLC does not reconstruct every daily feature key under daily_bars(min_hourly_bars=20)"
        )
    for row in joined.iter_rows(named=True):
        for column in _DAILY_OHLC_COLUMNS:
            if not _float_equal(row[column], row[f"__raw_{column}"]):
                raise LongA0SidecarError(
                    f"daily_bars {column} parity failed for {(row['symbol'], row['ts_ms'])}"
                )
        if int(row["hourly_bars"]) != int(row["__raw_hourly_bars"]):
            raise LongA0SidecarError(
                f"daily_bars hourly_bars parity failed for {(row['symbol'], row['ts_ms'])}"
            )


def _asset_regime_rows(
    rebuilt_daily: pl.DataFrame,
    *,
    symbol: str,
    sma_days: int,
) -> dict[int, tuple[float, float, float, bool]]:
    asset = rebuilt_daily.filter(pl.col("symbol") == symbol).sort(["symbol", "ts_ms"])
    if asset.is_empty():
        return {}
    asset = asset.with_columns(
        calendar_roll(
            pl.col("close"),
            "mean",
            sma_days,
            shifted=False,
            min_samples=sma_days,
        )
        .over("symbol")
        .alias("__sma")
    )
    output: dict[int, tuple[float, float, float, bool]] = {}
    for row in asset.select("ts_ms", "close", "__sma").iter_rows(named=True):
        close = row["close"]
        sma = row["__sma"]
        if close is None or sma is None:
            continue
        close_float = float(close)
        sma_float = float(sma)
        if not math.isfinite(close_float) or not math.isfinite(sma_float) or close_float <= 0.0 or sma_float <= 0.0:
            continue
        output[int(row["ts_ms"])] = (
            close_float,
            sma_float,
            close_float / sma_float - 1.0,
            close_float > sma_float,
        )
    return output


def _build_regime_context(
    signal_timestamps: list[int],
    rebuilt_daily: pl.DataFrame,
    config: LongNativeConfig,
) -> pl.DataFrame:
    btc = _asset_regime_rows(
        rebuilt_daily,
        symbol=config.regime_symbol,
        sma_days=int(config.regime_sma_days),
    )
    eth = _asset_regime_rows(
        rebuilt_daily,
        symbol="ETHUSDT",
        sma_days=int(config.regime_sma_days),
    )
    rows: list[dict[str, object]] = []
    for signal_ts_ms in signal_timestamps:
        btc_values = btc.get(signal_ts_ms)
        eth_values = eth.get(signal_ts_ms)
        rows.append(
            {
                "signal_ts_ms": signal_ts_ms,
                "btc_close": btc_values[0] if btc_values is not None else None,
                "btc_sma_30d": btc_values[1] if btc_values is not None else None,
                "btc_sma_dist": btc_values[2] if btc_values is not None else None,
                "btc_regime_available": btc_values is not None,
                "btc_regime_pass": btc_values[3] if btc_values is not None else None,
                "eth_close": eth_values[0] if eth_values is not None else None,
                "eth_sma_30d": eth_values[1] if eth_values is not None else None,
                "eth_sma_dist": eth_values[2] if eth_values is not None else None,
                "eth_regime_available": eth_values is not None,
                "eth_regime_pass": eth_values[3] if eth_values is not None else None,
            }
        )
    return pl.DataFrame(rows, schema=dict(REGIME_CONTEXT_SCHEMA)).sort(_CONTEXT_ROW_ORDER)


def _btc_month_source_rows(
    rebuilt_daily: pl.DataFrame,
    consumed_hourly: pl.DataFrame,
    config: LongNativeConfig,
) -> dict[int, dict[str, object]]:
    btc = rebuilt_daily.filter(pl.col("symbol") == config.regime_symbol).sort(["symbol", "ts_ms"])
    if btc.is_empty():
        return {}
    btc = btc.with_columns(
        (
            pl.col("close")
            / calendar_shift(pl.col("close"), int(config.btc_month_regime_lookback_days))
            - 1.0
        ).alias("btc_month_ret_30d")
    )
    exact = _btc_hourly_exact_month_context(
        consumed_hourly,
        regime_symbol=config.regime_symbol,
        month_days=float(config.btc_month_regime_month_days),
    )
    if "btc_month_ret_exact_source_ts_ms" not in exact.columns:
        exact = exact.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("btc_month_ret_exact_source_ts_ms")
        )
    if exact.is_empty():
        btc = btc.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("btc_month_ret_exact"),
            pl.lit(None, dtype=pl.Int64).alias("btc_month_ret_exact_source_ts_ms"),
        )
    else:
        btc = btc.join(
            exact.select(
                "ts_ms",
                "btc_month_ret_exact",
                "btc_month_ret_exact_source_ts_ms",
            ),
            on="ts_ms",
            how="left",
        )
    btc = btc.with_columns(
        pl.when(
            pl.col("btc_month_ret_exact").is_not_null()
            & pl.col("btc_month_ret_30d").is_not_null()
        )
        .then(
            pl.max_horizontal(
                pl.min_horizontal(
                    pl.col("btc_month_ret_exact"),
                    pl.col("btc_month_ret_30d") + float(config.btc_month_regime_smart_tolerance),
                ),
                pl.min_horizontal(
                    pl.col("btc_month_ret_30d"),
                    pl.col("btc_month_ret_exact") + float(config.btc_month_regime_smart_tolerance),
                ),
            )
        )
        .otherwise(None)
        .alias("btc_month_ret_smart")
    )
    return {
        int(row["ts_ms"]): row
        for row in btc.select(
            "ts_ms",
            "btc_month_ret_30d",
            "btc_month_ret_exact",
            "btc_month_ret_smart",
            "btc_month_ret_exact_source_ts_ms",
        ).iter_rows(named=True)
    }


def _month_gate_pass(value: float, gate: str) -> bool:
    if gate == "off":
        return True
    if gate == "uptrend":
        return value > 0.0
    if gate == "downtrend":
        return value <= 0.0
    raise LongA0SidecarError(f"unsupported btc_month_regime_gate {gate!r}")


def _build_btc_month_context(
    signal_timestamps: list[int],
    rebuilt_daily: pl.DataFrame,
    consumed_hourly: pl.DataFrame,
    config: LongNativeConfig,
) -> tuple[pl.DataFrame, dict[int, dict[str, object]]]:
    value_column = _MONTH_VALUE_COLUMN_BY_MODE.get(config.btc_month_regime_mode)
    if value_column is None:
        raise LongA0SidecarError(
            f"unsupported btc_month_regime_mode {config.btc_month_regime_mode!r}"
        )
    source_rows = _btc_month_source_rows(rebuilt_daily, consumed_hourly, config)
    rows: list[dict[str, object]] = []
    for signal_ts_ms in signal_timestamps:
        source = source_rows.get(signal_ts_ms)
        value = source.get(value_column) if source is not None else None
        available = value is not None and math.isfinite(float(value))
        normalized_value = float(value) if available else None
        rows.append(
            {
                "signal_ts_ms": signal_ts_ms,
                "btc_month_regime_value": normalized_value,
                "btc_month_regime_available": available,
                "btc_month_regime_pass": (
                    _month_gate_pass(normalized_value, str(config.btc_month_regime_gate))
                    if normalized_value is not None
                    else None
                ),
            }
        )
    frame = pl.DataFrame(rows, schema=dict(BTC_MONTH_CONTEXT_SCHEMA)).sort(_CONTEXT_ROW_ORDER)
    return frame, source_rows


def _assert_context_parity(
    features: pl.DataFrame,
    regime_context: pl.DataFrame,
    btc_month_context: pl.DataFrame,
    month_source_rows: dict[int, dict[str, object]],
    config: LongNativeConfig,
) -> None:
    regime_by_ts = {int(row["signal_ts_ms"]): row for row in regime_context.iter_rows(named=True)}
    month_by_ts = {int(row["signal_ts_ms"]): row for row in btc_month_context.iter_rows(named=True)}
    month_parity_columns = _MONTH_PARITY_COLUMNS_BY_MODE[config.btc_month_regime_mode]

    for row in features.iter_rows(named=True):
        signal_ts_ms = int(row["ts_ms"])
        regime = regime_by_ts[signal_ts_ms]
        month = month_by_ts[signal_ts_ms]

        if regime["btc_regime_available"]:
            if bool(row["regime_on"]) != bool(regime["btc_regime_pass"]):
                raise LongA0SidecarError(f"regime_on parity failed at {signal_ts_ms}")
            if not _float_equal(row["btc_sma_dist"], regime["btc_sma_dist"]):
                raise LongA0SidecarError(f"btc_sma_dist parity failed at {signal_ts_ms}")
        elif bool(row["regime_on"]) or not _float_equal(row["btc_sma_dist"], 0.0):
            raise LongA0SidecarError(
                f"production missing-BTC False/0.0 fallback parity failed at {signal_ts_ms}"
            )

        if regime["eth_regime_available"]:
            if bool(row["eth_regime_on"]) != bool(regime["eth_regime_pass"]):
                raise LongA0SidecarError(f"eth_regime_on parity failed at {signal_ts_ms}")
        elif bool(row["eth_regime_on"]):
            raise LongA0SidecarError(
                f"production missing-ETH False fallback parity failed at {signal_ts_ms}"
            )

        source = month_source_rows.get(signal_ts_ms, {})
        for column in month_parity_columns:
            if column == "btc_month_ret_exact_source_ts_ms":
                if row[column] != source.get(column):
                    raise LongA0SidecarError(f"{column} parity failed at {signal_ts_ms}")
            elif not _float_equal(row[column], source.get(column)):
                raise LongA0SidecarError(f"{column} parity failed at {signal_ts_ms}")

        selected_column = _MONTH_VALUE_COLUMN_BY_MODE[config.btc_month_regime_mode]
        if not _float_equal(month["btc_month_regime_value"], source.get(selected_column)):
            raise LongA0SidecarError(f"configured BTC-month value parity failed at {signal_ts_ms}")


def _build_source_availability(
    features: pl.DataFrame,
    regime_context: pl.DataFrame,
    btc_month_context: pl.DataFrame,
) -> pl.DataFrame:
    regime_by_ts = {int(row["signal_ts_ms"]): row for row in regime_context.iter_rows(named=True)}
    month_by_ts = {int(row["signal_ts_ms"]): row for row in btc_month_context.iter_rows(named=True)}
    rows: list[dict[str, object]] = []
    for symbol, signal_ts_ms in features.select("symbol", "ts_ms").iter_rows():
        signal_ts = int(signal_ts_ms)
        regime = regime_by_ts[signal_ts]
        month = month_by_ts[signal_ts]
        rows.append(
            {
                "symbol": str(symbol),
                "signal_ts_ms": signal_ts,
                "signal_feature_available_ts_ms": signal_ts,
                "daily_bar_available_ts_ms": signal_ts,
                "btc_context_available_ts_ms": (
                    signal_ts if regime["btc_regime_available"] else None
                ),
                "eth_context_available_ts_ms": (
                    signal_ts if regime["eth_regime_available"] else None
                ),
                "btc_month_context_available_ts_ms": (
                    signal_ts if month["btc_month_regime_available"] else None
                ),
            }
        )
    return pl.DataFrame(rows, schema=dict(SOURCE_AVAILABILITY_SCHEMA)).sort(
        ["signal_ts_ms", "symbol"]
    )


def _frame_sha256(frame: pl.DataFrame, *, artifact_name: str, sort_by: list[str]) -> str:
    canonical = frame.sort(sort_by) if sort_by and not frame.is_empty() else frame
    digest = hashlib.sha256()
    header = {
        "artifact_name": artifact_name,
        "columns": [
            {"name": name, "dtype": str(dtype)}
            for name, dtype in canonical.schema.items()
        ],
    }
    digest.update(
        (json.dumps(header, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(
            "utf-8"
        )
    )
    for row in canonical.iter_rows(named=True):
        digest.update(
            (json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def _receipt(
    *,
    config: LongNativeConfig,
    features: pl.DataFrame,
    consumed_hourly: pl.DataFrame,
    context_start_ts_ms: int | None,
    source_availability: pl.DataFrame,
    regime_context: pl.DataFrame,
    btc_month_context: pl.DataFrame,
) -> Mapping[str, Any]:
    config_identity = derive_long_a0_config_identity()
    assert_stage_config_matches_identity(config, config_identity)
    month_columns = list(_MONTH_PARITY_COLUMNS_BY_MODE[config.btc_month_regime_mode])
    feature_projection_columns = [
        "symbol",
        "ts_ms",
        *_DAILY_OHLC_COLUMNS,
        "hourly_bars",
        "regime_on",
        "eth_regime_on",
        "btc_sma_dist",
        *month_columns,
    ]
    # Preserve first occurrence while avoiding the exact-source timestamp being
    # listed twice in a future mode definition.
    feature_projection_columns = list(dict.fromkeys(feature_projection_columns))
    feature_projection = features.select(feature_projection_columns)
    payload: dict[str, Any] = {
        "schema_version": LONG_A0_SIDECAR_SCHEMA_VERSION,
        "artifact_type": "strategy_overhaul_long_a0_source_sidecars",
        "availability_semantics": LONG_A0_SIDECAR_AVAILABILITY_SEMANTICS,
        "daily_bar_builder": "liquidity_migration.momentum_signals.daily_bars",
        "daily_bar_min_hourly_bars": 20,
        "calendar_rolling_semantics": "calendar_roll closed=right exact daily grid",
        "config_factory": "liquidity_migration.long_native_event_demo._v11a_long_native_config",
        "canonical_config_sha256": config_identity["canonical_config_sha256"],
        "registered_scope_sha256": config_identity["scope_sha256"],
        "config_identity_sha256": config_identity["identity_sha256"],
        "btc_month_regime_mode": config.btc_month_regime_mode,
        "btc_month_regime_gate": config.btc_month_regime_gate,
        "configured_btc_month_parity_columns": month_columns,
        "context_source_read_start_ts_ms": context_start_ts_ms,
        "source_read_end_ts_ms_exclusive": (
            int(features["ts_ms"].max()) if not features.is_empty() else None
        ),
        "feature_population_row_count": features.height,
        "feature_signal_timestamp_count": features["ts_ms"].n_unique(),
        "feature_projection_columns_hashed": feature_projection_columns,
        "feature_projection_sha256": _frame_sha256(
            feature_projection,
            artifact_name="long_a0_daily_context_feature_projection",
            sort_by=["ts_ms", "symbol"],
        ),
        "consumed_raw_ohlc_columns_hashed": list(RAW_HOURLY_OHLC_SCHEMA),
        "consumed_raw_ohlc_row_count": consumed_hourly.height,
        "consumed_raw_ohlc_sha256": _frame_sha256(
            consumed_hourly,
            artifact_name="long_a0_consumed_raw_hourly_ohlc",
            sort_by=["symbol", "ts_ms"],
        ),
        "source_availability_sha256": _frame_sha256(
            source_availability,
            artifact_name="long_a0_source_availability",
            sort_by=["signal_ts_ms", "symbol"],
        ),
        "regime_context_sha256": _frame_sha256(
            regime_context,
            artifact_name="long_a0_regime_context",
            sort_by=["signal_ts_ms"],
        ),
        "btc_month_context_sha256": _frame_sha256(
            btc_month_context,
            artifact_name="long_a0_btc_month_context",
            sort_by=["signal_ts_ms"],
        ),
        "exact_feature_population_coverage_checked": True,
        "daily_ohlc_and_bar_count_parity_checked": True,
        "regime_and_configured_btc_month_parity_checked": True,
        "missing_context_preserved_as_null_and_false_availability": True,
        "actual_vendor_publication_time_claimed": False,
        "historical_ingestion_time_claimed": False,
        "operational_latency_claimed": False,
        "post_latest_signal_bars_read": False,
        "post_row_signal_bar_values_used": False,
        "raw_ohlc_across_registered_s02_window_read_and_hashed": True,
        "outcome_labels_or_metrics_calculated": False,
        "root_snapshot_identity_bound": False,
    }
    payload["artifact_sha256"] = canonical_json_sha256(payload)
    return MappingProxyType(payload)


def build_long_a0_sidecars(
    hourly_bars: pl.DataFrame,
    daily_features: pl.DataFrame,
    *,
    config: LongNativeConfig,
) -> LongA0SidecarBundle:
    """Derive exact LONG-A0 source sidecars without reading post-signal bars.

    ``daily_features`` must be the exact retained feature population from the
    registered LONG builder.  Warmup rows may remain only in ``hourly_bars``;
    sidecar outputs cover exactly the supplied ``(symbol, ts_ms)`` population.
    Raw rows at or after the latest supplied signal timestamp are ignored.
    """

    frozen_config = _require_exact_v11a_config(config)
    features = _validate_daily_features(daily_features, frozen_config)
    consumed, context_start_ts_ms = _consumed_hourly_source(
        hourly_bars,
        features,
        frozen_config,
    )
    rebuilt_daily = daily_bars(consumed)
    _assert_daily_source_parity(features, rebuilt_daily)

    signal_timestamps = sorted(int(value) for value in features["ts_ms"].unique().to_list())
    regime_context = _build_regime_context(signal_timestamps, rebuilt_daily, frozen_config)
    btc_month_context, month_source_rows = _build_btc_month_context(
        signal_timestamps,
        rebuilt_daily,
        consumed,
        frozen_config,
    )
    _assert_context_parity(
        features,
        regime_context,
        btc_month_context,
        month_source_rows,
        frozen_config,
    )
    source_availability = _build_source_availability(
        features,
        regime_context,
        btc_month_context,
    )
    _assert_exact_population_keys(
        source_availability,
        features,
        name="source_availability",
    )

    expected_timestamps = pl.DataFrame(
        {"signal_ts_ms": pl.Series(signal_timestamps, dtype=pl.Int64)}
    )
    for name, frame in (
        ("regime_context", regime_context),
        ("btc_month_context", btc_month_context),
    ):
        if not frame.select("signal_ts_ms").equals(expected_timestamps):
            raise RuntimeError(f"{name} timestamp coverage changed during construction")

    receipt = _receipt(
        config=frozen_config,
        features=features,
        consumed_hourly=consumed,
        context_start_ts_ms=context_start_ts_ms,
        source_availability=source_availability,
        regime_context=regime_context,
        btc_month_context=btc_month_context,
    )
    return LongA0SidecarBundle(
        source_availability=source_availability,
        regime_context=regime_context,
        btc_month_context=btc_month_context,
        receipt=receipt,
    )


__all__ = [
    "DAILY_CONTEXT_FEATURE_SCHEMA",
    "LONG_A0_SIDECAR_AVAILABILITY_SEMANTICS",
    "LONG_A0_SIDECAR_SCHEMA_VERSION",
    "RAW_HOURLY_OHLC_SCHEMA",
    "LongA0SidecarBundle",
    "LongA0SidecarError",
    "build_long_a0_sidecars",
]
