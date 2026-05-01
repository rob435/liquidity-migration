from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .config import FeatureConfig, SignalConfig
from .math_utils import ema, robust_z, rolling_median
from .storage import read_dataset, write_dataset


RAW_COLUMNS = (
    "aggression_raw",
    "rel_volume_raw",
    "momentum_raw",
    "carry_raw",
    "quality_raw",
    "oi_impulse_raw",
)


def compute_features_from_store(
    data_root: str | Path,
    *,
    feature_config: FeatureConfig | None = None,
    signal_config: SignalConfig | None = None,
) -> pl.DataFrame:
    flow = read_dataset(data_root, "signed_flow_1h")
    klines = read_dataset(data_root, "klines_1h")
    funding = read_dataset(data_root, "funding")
    ticker = read_dataset(data_root, "ticker_snapshots")
    open_interest = read_dataset(data_root, "open_interest")
    features = compute_features(
        flow=flow,
        klines=klines,
        funding=funding,
        ticker_snapshots=ticker,
        open_interest=open_interest,
        feature_config=feature_config,
        signal_config=signal_config,
    )
    write_dataset(features, data_root, "features_1h")
    return features


def compute_features(
    *,
    flow: pl.DataFrame,
    klines: pl.DataFrame,
    funding: pl.DataFrame | None = None,
    ticker_snapshots: pl.DataFrame | None = None,
    open_interest: pl.DataFrame | None = None,
    feature_config: FeatureConfig | None = None,
    signal_config: SignalConfig | None = None,
) -> pl.DataFrame:
    if klines.is_empty():
        return pl.DataFrame()
    cfg = feature_config or FeatureConfig()
    signals = signal_config or SignalConfig()
    flow_map = _rows_by_symbol_ts(flow)
    funding_by_symbol = _rows_by_symbol(funding if funding is not None else pl.DataFrame())
    ticker_by_symbol = _rows_by_symbol(ticker_snapshots if ticker_snapshots is not None else pl.DataFrame())
    oi_by_symbol = _rows_by_symbol(open_interest if open_interest is not None else pl.DataFrame())

    rows: list[dict[str, Any]] = []
    for symbol, symbol_klines in _rows_by_symbol(klines).items():
        symbol_klines = sorted(symbol_klines, key=lambda item: item["ts_ms"])
        ts_values = [int(row["ts_ms"]) for row in symbol_klines]
        close = np.asarray([float(row["close"]) for row in symbol_klines], dtype=float)
        turnover = np.asarray([float(row["turnover_quote"]) for row in symbol_klines], dtype=float)
        buy_quote = np.asarray(
            [float(flow_map.get((symbol, ts), {}).get("buy_quote", 0.0)) for ts in ts_values],
            dtype=float,
        )
        sell_quote = np.asarray(
            [float(flow_map.get((symbol, ts), {}).get("sell_quote", 0.0)) for ts in ts_values],
            dtype=float,
        )
        total_quote = buy_quote + sell_quote
        eps = np.maximum(1.0, 0.001 * rolling_median(total_quote, 168))

        buy_ema_fast = ema(buy_quote, cfg.aggression_fast_span_h)
        sell_ema_fast = ema(sell_quote, cfg.aggression_fast_span_h)
        buy_ema_slow = ema(buy_quote, cfg.aggression_slow_span_h)
        sell_ema_slow = ema(sell_quote, cfg.aggression_slow_span_h)
        aggr_fast = np.log((buy_ema_fast + eps) / (sell_ema_fast + eps))
        aggr_slow = np.log((buy_ema_slow + eps) / (sell_ema_slow + eps))
        aggression_raw = 0.70 * aggr_fast + 0.30 * aggr_slow

        qvol_fast = ema(turnover, cfg.volume_fast_span_h)
        qvol_slow = ema(turnover, cfg.volume_slow_span_h)
        rel_volume_raw = np.log((qvol_fast + 1.0) / (qvol_slow + 1.0))

        momentum_raw = np.full(close.shape, np.nan, dtype=float)
        for index in range(close.size):
            weighted_return = 0.0
            ok = True
            for window, weight in zip(cfg.momentum_windows_h, cfg.momentum_weights):
                if index < window or close[index - window] <= 0:
                    ok = False
                    break
                weighted_return += weight * math.log(close[index] / close[index - window])
            if ok:
                momentum_raw[index] = weighted_return

        funding_series = _latest_series(
            ts_values,
            funding_by_symbol.get(symbol, []),
            value_col="funding_rate_8h_equiv",
            fallback_col="funding_rate",
        )
        carry_raw = -ema(funding_series, cfg.carry_ema_span)

        quality_turnover = _latest_series(
            ts_values,
            ticker_by_symbol.get(symbol, []),
            value_col="turnover_24h",
            fallback_array=turnover * 24.0,
        )
        quality_oi = _latest_series(
            ts_values,
            ticker_by_symbol.get(symbol, []),
            value_col="open_interest_value",
            fallback_array=_latest_series(ts_values, oi_by_symbol.get(symbol, []), value_col="open_interest_value"),
        )
        liq_raw = np.log(rolling_median(quality_turnover, 24) + 1.0)
        oi_raw = np.log(rolling_median(quality_oi, 24) + 1.0)
        quality_raw = 0.70 * liq_raw + 0.30 * oi_raw

        oi_impulse_raw = np.full(close.shape, np.nan, dtype=float)
        window = cfg.oi_impulse_window_h
        for index in range(window, close.size):
            if close[index - window] <= 0:
                continue
            price_ret = math.log(close[index] / close[index - window])
            oi_ret = math.log((quality_oi[index] + 1.0) / (quality_oi[index - window] + 1.0))
            oi_impulse_raw[index] = math.copysign(1.0, price_ret) * oi_ret if price_ret != 0 else 0.0

        for index, ts_ms in enumerate(ts_values):
            rows.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "aggression_raw": aggression_raw[index],
                    "rel_volume_raw": rel_volume_raw[index],
                    "momentum_raw": momentum_raw[index],
                    "carry_raw": carry_raw[index],
                    "quality_raw": quality_raw[index],
                    "oi_impulse_raw": oi_impulse_raw[index],
                }
            )

    df = pl.DataFrame(rows).sort(["ts_ms", "symbol"])
    for raw_col in RAW_COLUMNS:
        df = _add_cross_sectional_z(df, raw_col, raw_col.replace("_raw", "_z"), clip=cfg.robust_z_clip)

    df = df.with_columns(
        [
            (
                pl.col("aggression_z")
                * (1.0 + 0.25 * pl.col("rel_volume_z").clip(-1.0, 2.0))
            )
            .clip(-3.0, 3.0)
            .alias("aggression_confirmed"),
            pl.when((pl.col("carry_z").sign() != pl.col("aggression_z").sign()) & (pl.col("aggression_z").abs() > 1.5))
            .then(0.50 * pl.col("carry_z"))
            .otherwise(pl.col("carry_z"))
            .alias("carry_z_adjusted"),
        ]
    )
    weights = signals.weights
    score_expr = (
        weights["aggression_confirmed"] * pl.col("aggression_confirmed")
        + weights["momentum"] * pl.col("momentum_z")
        + weights["carry"] * pl.col("carry_z_adjusted")
        + weights["quality"] * pl.col("quality_z")
        + weights["oi_impulse"] * pl.col("oi_impulse_z")
    )
    df = df.with_columns(score_expr.clip(-3.0, 3.0).alias("score_raw"))
    df = _demean_by_timestamp(df, "score_raw", "composite_score")
    return df.sort(["ts_ms", "symbol"])


def _add_cross_sectional_z(df: pl.DataFrame, input_col: str, output_col: str, *, clip: float) -> pl.DataFrame:
    frames = []
    for part in df.partition_by("ts_ms", maintain_order=True):
        z = robust_z(part[input_col].to_list(), clip=clip)
        frames.append(part.with_columns(pl.Series(output_col, z)))
    return pl.concat(frames).sort(["ts_ms", "symbol"])


def _demean_by_timestamp(df: pl.DataFrame, input_col: str, output_col: str) -> pl.DataFrame:
    frames = []
    for part in df.partition_by("ts_ms", maintain_order=True):
        values = np.asarray(part[input_col].to_list(), dtype=float)
        mean = float(np.nanmean(values)) if np.isfinite(values).any() else 0.0
        frames.append(part.with_columns(pl.Series(output_col, values - mean)))
    return pl.concat(frames).sort(["ts_ms", "symbol"])


def _rows_by_symbol(df: pl.DataFrame) -> dict[str, list[dict[str, Any]]]:
    if df.is_empty():
        return {}
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in df.to_dicts():
        out[str(row["symbol"])].append(row)
    for rows in out.values():
        rows.sort(key=lambda item: item["ts_ms"])
    return out


def _rows_by_symbol_ts(df: pl.DataFrame) -> dict[tuple[str, int], dict[str, Any]]:
    if df.is_empty():
        return {}
    return {(str(row["symbol"]), int(row["ts_ms"])): row for row in df.to_dicts()}


def _latest_series(
    ts_values: list[int],
    rows: list[dict[str, Any]],
    *,
    value_col: str,
    fallback_col: str | None = None,
    fallback_array: np.ndarray | None = None,
) -> np.ndarray:
    if not rows:
        if fallback_array is not None:
            return np.asarray(fallback_array, dtype=float)
        return np.zeros(len(ts_values), dtype=float)
    rows = sorted(rows, key=lambda item: item["ts_ms"])
    out = np.full(len(ts_values), np.nan, dtype=float)
    cursor = 0
    latest = np.nan
    for index, ts_ms in enumerate(ts_values):
        while cursor < len(rows) and int(rows[cursor]["ts_ms"]) <= ts_ms:
            value = rows[cursor].get(value_col)
            if value is None and fallback_col is not None:
                value = rows[cursor].get(fallback_col)
            if value is not None:
                latest = float(value)
            cursor += 1
        if math.isfinite(latest):
            out[index] = latest
        elif fallback_array is not None:
            out[index] = float(fallback_array[index])
        else:
            out[index] = 0.0
    return out
