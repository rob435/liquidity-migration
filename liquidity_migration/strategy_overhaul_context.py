"""Outcome-blind market context and static diagnostics for CONTINUOUS-A0.

These functions annotate an existing signal-time population.  They never read a
future bar relative to the annotated row, filter a row, or replay portfolio
state.  Exact clock-time joins are used for context returns so a storage gap is
not silently treated as a one-, 24-, or 168-hour lag.
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .continuous_events import BTC_TREND_MODE_DAILY_PRIOR, _btc_trend_return_lookup


CONTINUOUS_BTC_TREND_GATE = "uptrend"
CONTINUOUS_BTC_TREND_LOOKBACK_DAYS = 30
CONTINUOUS_BTC_TREND_MODE = BTC_TREND_MODE_DAILY_PRIOR
CONTINUOUS_STATIC_COMPONENT_ORDER = ("p3", "p4p3", "p4p5")
CONTINUOUS_STATIC_COMPONENT_TRIGGER_BY_NAME = {
    "p3": "turn3_pop3",
    "p4p3": "turn4_pop3",
    "p4p5": "turn4_pop5",
}
CONTINUOUS_STATIC_COMPONENT_AGE_DAYS_MIN = 240


_MARKET_REQUIRED = {"symbol", "close"}
_STATIC_REQUIRED = {
    "rmom_stable_available",
    "current_q25_pass",
    "btc_uptrend_known",
    "btc_uptrend_pass",
    "current_q25_d9",
    "current_liquidity_500k_pass",
    "trigger_turn3_pop3",
    "trigger_turn4_pop3",
    "trigger_turn4_pop5",
    "current_age_source_available",
    "current_age_240_pass",
}
_FORBIDDEN_SIGNAL_TOKENS = (
    "forward_return",
    "path_",
    "entry_price",
    "entry_anchor",
    "first_passage",
    "trade_pnl",
    "realized_pnl",
)


def _validate_signal_frame(
    frame: pl.DataFrame,
    *,
    required: set[str],
) -> tuple[pl.DataFrame, str]:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"signal-time frame missing required columns: {missing}")
    time_column = "signal_ts_ms" if "signal_ts_ms" in frame.columns else "ts_ms"
    if time_column not in frame.columns:
        raise ValueError("signal-time frame requires signal_ts_ms or ts_ms")
    forbidden = sorted(
        column for column in frame.columns if any(token in column.lower() for token in _FORBIDDEN_SIGNAL_TOKENS)
    )
    if forbidden:
        raise ValueError(f"signal-time frame contains outcome/entry columns: {forbidden}")
    typed = frame.with_columns(
        pl.col("symbol").cast(pl.String),
        pl.col(time_column).cast(pl.Int64),
    )
    if {"signal_ts_ms", "ts_ms"} <= set(typed.columns):
        typed = typed.with_columns(pl.col("ts_ms").cast(pl.Int64))
        alias_mismatch = typed.filter(pl.col("signal_ts_ms") != pl.col("ts_ms"))
        if not alias_mismatch.is_empty():
            raise ValueError("signal_ts_ms and ts_ms must agree when both are supplied")
    bad_key = typed.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col(time_column).is_null()
        | ((pl.col(time_column) % MS_PER_HOUR) != 0)
    )
    if not bad_key.is_empty():
        raise ValueError("signal-time frame has blank/null symbols or off-grid timestamps")
    key = ["symbol", time_column]
    duplicates = typed.group_by(key).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ValueError(f"signal-time frame has duplicate {tuple(key)} keys")
    if "venue" in typed.columns and typed["venue"].drop_nulls().n_unique() > 1:
        raise ValueError("market context must be built for one venue at a time")
    return typed.sort(key), time_column


def _exact_symbol_metrics(
    frame: pl.DataFrame,
    *,
    symbol: str,
    time_column: str,
) -> dict[int, dict[str, float | None]]:
    rows = frame.filter(pl.col("symbol") == symbol).select(pl.col(time_column).alias("ts_ms"), "close").sort("ts_ms")
    closes = {
        int(ts): float(close)
        for ts, close in rows.iter_rows()
        if close is not None and math.isfinite(float(close)) and float(close) > 0.0
    }
    ret1 = {ts: close / closes[ts - MS_PER_HOUR] - 1.0 for ts, close in closes.items() if ts - MS_PER_HOUR in closes}
    output: dict[int, dict[str, float | None]] = {}
    for ts, close in closes.items():
        recent = [
            ret1[source_ts]
            for source_ts in range(ts - 167 * MS_PER_HOUR, ts + MS_PER_HOUR, MS_PER_HOUR)
            if source_ts in ret1 and math.isfinite(ret1[source_ts])
        ]
        output[ts] = {
            "ret1": ret1.get(ts),
            "ret24": (close / closes[ts - 24 * MS_PER_HOUR] - 1.0 if ts - 24 * MS_PER_HOUR in closes else None),
            "ret168": (close / closes[ts - 168 * MS_PER_HOUR] - 1.0 if ts - 168 * MS_PER_HOUR in closes else None),
            "rv_168h": float(np.std(recent, ddof=1)) if len(recent) >= 48 else None,
        }
    return output


def _exact_population_returns(
    frame: pl.DataFrame,
    *,
    time_column: str,
) -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], float]]:
    closes: dict[str, dict[int, float]] = defaultdict(dict)
    for symbol, ts, close in frame.select("symbol", time_column, "close").iter_rows():
        if close is not None and math.isfinite(float(close)) and float(close) > 0.0:
            closes[str(symbol)][int(ts)] = float(close)
    ret1: dict[tuple[str, int], float] = {}
    ret24: dict[tuple[str, int], float] = {}
    for symbol, values in closes.items():
        for ts, close in values.items():
            prior1 = values.get(ts - MS_PER_HOUR)
            prior24 = values.get(ts - 24 * MS_PER_HOUR)
            if prior1 is not None:
                ret1[(symbol, ts)] = close / prior1 - 1.0
            if prior24 is not None:
                ret24[(symbol, ts)] = close / prior24 - 1.0
    return ret1, ret24


def attach_continuous_market_context(
    feature_tape: pl.DataFrame,
    *,
    btc_symbol: str = "BTCUSDT",
    eth_symbol: str = "ETHUSDT",
    btc_trend_lookback_days: int = CONTINUOUS_BTC_TREND_LOOKBACK_DAYS,
) -> pl.DataFrame:
    """Attach the finite registered market-context surface without filtering.

    The BTC gate reuses the production daily-prior lookup.  Its value for an
    hourly signal is keyed by that signal's UTC day and uses only completed days
    before the day.  Unknown gate input remains distinct from a failed gate.
    """

    if CONTINUOUS_BTC_TREND_GATE != "uptrend":
        raise ValueError("CONTINUOUS market-context adapter supports the canonical uptrend gate only")
    if CONTINUOUS_BTC_TREND_MODE != BTC_TREND_MODE_DAILY_PRIOR:
        raise ValueError("CONTINUOUS market-context adapter supports the canonical daily-prior mode only")
    if btc_trend_lookback_days < 1:
        raise ValueError("btc_trend_lookback_days must be positive")
    if btc_symbol != "BTCUSDT":
        raise ValueError(
            "btc_symbol must be BTCUSDT while the production daily-prior gate is explicitly keyed to BTCUSDT"
        )
    frame, time_column = _validate_signal_frame(feature_tape, required=_MARKET_REQUIRED)
    frame = frame.with_columns(pl.col("close").cast(pl.Float64))
    if frame.is_empty():
        float_fields = [
            "btc_uptrend_value",
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
        ]
        count_fields = [
            f"{prefix}_{suffix}"
            for prefix in (
                "alt_breadth_ret24_positive",
                "alt_breadth_ret1_ge_3pct",
                "xs_ret1_dispersion",
            )
            for suffix in ("peer_count", "missing_peer_count", "denominator_count")
        ]
        return frame.with_columns(
            *[pl.lit(None, dtype=pl.Float64).alias(name) for name in float_fields],
            pl.lit(False, dtype=pl.Boolean).alias("btc_uptrend_known"),
            pl.lit(False, dtype=pl.Boolean).alias("btc_uptrend_pass"),
            pl.lit(False, dtype=pl.Boolean).alias("btc_uptrend_fail"),
            pl.lit(True, dtype=pl.Boolean).alias("btc_uptrend_unknown"),
            *[pl.lit(None, dtype=pl.Int64).alias(name) for name in count_fields],
        )

    btc = _exact_symbol_metrics(frame, symbol=btc_symbol, time_column=time_column)
    eth = _exact_symbol_metrics(frame, symbol=eth_symbol, time_column=time_column)
    trend_lookup = _btc_trend_return_lookup(
        frame.select("symbol", pl.col(time_column).alias("ts_ms"), "close"),
        mode=CONTINUOUS_BTC_TREND_MODE,
        lookback_days=btc_trend_lookback_days,
    )
    exact_ret1, exact_ret24 = _exact_population_returns(
        frame,
        time_column=time_column,
    )
    rows_by_ts: dict[int, list[str]] = defaultdict(list)
    for symbol, ts in frame.select("symbol", time_column).iter_rows():
        rows_by_ts[int(ts)].append(str(symbol))

    context_rows: list[dict[str, object]] = []
    for ts in sorted(rows_by_ts):
        symbols = rows_by_ts[ts]
        alts = [symbol for symbol in symbols if symbol not in {btc_symbol, eth_symbol}]
        alt_ret24 = [exact_ret24[(symbol, ts)] for symbol in alts if (symbol, ts) in exact_ret24]
        alt_ret1 = [exact_ret1[(symbol, ts)] for symbol in alts if (symbol, ts) in exact_ret1]
        all_ret1 = [exact_ret1[(symbol, ts)] for symbol in symbols if (symbol, ts) in exact_ret1]
        trend = trend_lookup.get((ts // MS_PER_DAY) * MS_PER_DAY)
        trend_known = trend is not None and math.isfinite(float(trend))
        trend_pass = trend_known and float(trend) > 0.0
        trend_fail = trend_known and not trend_pass

        row: dict[str, object] = {
            "__context_ts_ms": ts,
            "btc_uptrend_value": float(trend) if trend_known else None,
            "btc_uptrend_known": trend_known,
            "btc_uptrend_pass": trend_pass,
            "btc_uptrend_fail": trend_fail,
            "btc_uptrend_unknown": not trend_known,
            "alt_breadth_ret24_positive": (
                sum(value > 0.0 for value in alt_ret24) / len(alt_ret24) if alt_ret24 else None
            ),
            "alt_breadth_ret1_ge_3pct": (
                sum(value >= 0.03 for value in alt_ret1) / len(alt_ret1) if alt_ret1 else None
            ),
            "xs_ret1_dispersion": (float(np.std(all_ret1, ddof=1)) if len(all_ret1) >= 2 else None),
            "alt_breadth_ret24_positive_peer_count": len(alts),
            "alt_breadth_ret24_positive_missing_peer_count": len(alts) - len(alt_ret24),
            "alt_breadth_ret24_positive_denominator_count": len(alt_ret24),
            "alt_breadth_ret1_ge_3pct_peer_count": len(alts),
            "alt_breadth_ret1_ge_3pct_missing_peer_count": len(alts) - len(alt_ret1),
            "alt_breadth_ret1_ge_3pct_denominator_count": len(alt_ret1),
            "xs_ret1_dispersion_peer_count": len(symbols),
            "xs_ret1_dispersion_missing_peer_count": len(symbols) - len(all_ret1),
            "xs_ret1_dispersion_denominator_count": len(all_ret1),
        }
        for prefix, values in (("btc", btc), ("eth", eth)):
            metrics = values.get(ts, {})
            for field in ("ret1", "ret24", "ret168", "rv_168h"):
                row[f"{prefix}_{field}"] = metrics.get(field)
        context_rows.append(row)

    context = pl.DataFrame(context_rows, infer_schema_length=None)
    output = frame.join(
        context,
        left_on=time_column,
        right_on="__context_ts_ms",
        how="left",
    ).sort(["symbol", time_column])
    key = ["symbol", time_column]
    if output.height != frame.height or output.select(key).n_unique() != frame.height:
        raise RuntimeError("market-context annotation changed signal population cardinality")
    invalid_uptrend_state = output.filter(
        (
            pl.sum_horizontal(
                pl.col("btc_uptrend_pass").cast(pl.Int8),
                pl.col("btc_uptrend_fail").cast(pl.Int8),
                pl.col("btc_uptrend_unknown").cast(pl.Int8),
            )
            != 1
        )
        | (pl.col("btc_uptrend_known") == pl.col("btc_uptrend_unknown"))
    )
    if not invalid_uptrend_state.is_empty():  # pragma: no cover - construction invariant
        raise RuntimeError("BTC uptrend known/pass/fail/unknown state is not one-hot")
    return output


def attach_continuous_static_diagnostics(feature_tape: pl.DataFrame) -> pl.DataFrame:
    """Attach frozen component-specific static first-rejection diagnostics."""

    frame, time_column = _validate_signal_frame(
        feature_tape,
        required=_MARKET_REQUIRED | _STATIC_REQUIRED,
    )
    if frame.is_empty():
        return frame.with_columns(
            *[
                expression
                for component in CONTINUOUS_STATIC_COMPONENT_ORDER
                for expression in (
                    pl.lit(None, dtype=pl.Boolean).alias(f"{component}_static_candidate"),
                    pl.lit(None, dtype=pl.String).alias(f"{component}_static_first_rejection_reason"),
                )
            ]
        )
    component_columns = {
        component: f"trigger_{CONTINUOUS_STATIC_COMPONENT_TRIGGER_BY_NAME[component]}"
        for component in CONTINUOUS_STATIC_COMPONENT_ORDER
    }
    rows: list[dict[str, object]] = []
    for row in frame.iter_rows(named=True):
        annotation: dict[str, object] = {
            "symbol": row["symbol"],
            time_column: row[time_column],
        }
        for component, trigger_column in component_columns.items():
            checks = (
                (bool(row["rmom_stable_available"]), "rmom_not_stable"),
                (bool(row["current_q25_pass"]), "not_current_q25"),
                (bool(row["btc_uptrend_known"]), "btc_trend_unknown"),
                (row["btc_uptrend_pass"] is True, "btc_trend_fail"),
                (bool(row["current_q25_d9"]), "not_production_d9"),
                (bool(row["current_liquidity_500k_pass"]), "liquidity_below_500k"),
                (bool(row[trigger_column]), "component_not_triggered"),
                (bool(row["current_age_source_available"]), "age_source_unavailable"),
                (bool(row["current_age_240_pass"]), f"age_below_{CONTINUOUS_STATIC_COMPONENT_AGE_DAYS_MIN}d"),
            )
            reason = next((name for passed, name in checks if not passed), "static_candidate")
            annotation[f"{component}_static_candidate"] = reason == "static_candidate"
            annotation[f"{component}_static_first_rejection_reason"] = reason
        rows.append(annotation)
    annotations = pl.DataFrame(rows, infer_schema_length=None)
    key = ["symbol", time_column]
    output = frame.join(annotations, on=key, how="left").sort(key)
    if output.height != frame.height or output.select(key).n_unique() != frame.height:
        raise RuntimeError("static diagnostics changed signal population cardinality")
    return output


__all__ = [
    "CONTINUOUS_BTC_TREND_GATE",
    "CONTINUOUS_BTC_TREND_LOOKBACK_DAYS",
    "CONTINUOUS_BTC_TREND_MODE",
    "CONTINUOUS_STATIC_COMPONENT_AGE_DAYS_MIN",
    "CONTINUOUS_STATIC_COMPONENT_ORDER",
    "CONTINUOUS_STATIC_COMPONENT_TRIGGER_BY_NAME",
    "attach_continuous_market_context",
    "attach_continuous_static_diagnostics",
]
