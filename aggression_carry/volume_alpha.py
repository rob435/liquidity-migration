from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .config import CostConfig
from .math_utils import rank_correlation
from .storage import read_dataset, write_dataset


MS_PER_HOUR = 60 * 60 * 1000
MS_PER_DAY = 24 * MS_PER_HOUR
VOLUME_SCORE_COLUMNS = {
    "volume_change_1d": "volume_change_1d_z",
    "volume_change_3d": "volume_change_3d_z",
    "volume_persistence": "volume_persistence_z",
    "dollar_volume_rank": "dollar_volume_rank_z",
    "volume_composite": "volume_composite",
}


@dataclass(frozen=True, slots=True)
class VolumeMetric:
    signal: str
    horizon_d: int
    mean_ic: float
    ic_tstat: float
    ic_hit_rate: float
    mean_quantile_spread: float
    mean_cost_adjusted_spread: float
    observations: int


def run_volume_alpha(
    data_root: str | Path,
    *,
    horizons_d: tuple[int, ...] = (1, 3, 7),
    quantiles: tuple[float, ...] = (0.20, 0.30, 0.50),
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    klines = read_dataset(data_root, "klines_1h")
    if klines.is_empty():
        raise RuntimeError("klines_1h is empty; run download-data first")
    cost_config = cost_config or CostConfig()
    features = build_volume_features(klines)
    with_returns = attach_volume_forward_returns(features, klines, horizons_d=horizons_d)
    metrics = compute_volume_metrics(
        with_returns,
        horizons_d=horizons_d,
        cost_bps=cost_config.base_entry_exit_cost_bps,
    )
    portfolios = []
    for score_name, score_col in VOLUME_SCORE_COLUMNS.items():
        for quantile in quantiles:
            for hold_days in horizons_d:
                for scenario, multiplier in (("base", 1.0), ("2x_costs", 2.0), ("3x_costs", 3.0)):
                    portfolios.append(
                        run_volume_portfolio(
                            with_returns,
                            score_name=score_name,
                            score_col=score_col,
                            hold_days=hold_days,
                            quantile=quantile,
                            cost_bps=cost_config.base_entry_exit_cost_bps * multiplier,
                            scenario=scenario,
                        )
                    )
    payload = {
        "rows": with_returns.height,
        "symbols": sorted(with_returns["symbol"].unique().to_list()),
        "date_range": _date_range(with_returns),
        "signals": list(VOLUME_SCORE_COLUMNS),
        "horizons_d": list(horizons_d),
        "metrics": [asdict(item) for item in metrics],
        "portfolios": portfolios,
        "best_base_portfolio": _best_base_portfolio(portfolios),
    }
    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "volume_alpha_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "volume_alpha_report.md").write_text(format_volume_alpha_report(payload), encoding="utf-8")
    write_dataset(with_returns, data_root, "volume_alpha_features")
    write_dataset(pl.DataFrame(payload["metrics"]), data_root, "volume_alpha_metrics", partition_by=("signal",))
    write_dataset(pl.DataFrame(portfolios), data_root, "volume_alpha_portfolios", partition_by=("score", "scenario"))
    return payload


def build_volume_features(klines: pl.DataFrame) -> pl.DataFrame:
    daily_rows = _daily_bars(klines)
    if daily_rows.is_empty():
        return daily_rows

    rows = []
    for key, part in daily_rows.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        turnover = np.asarray(part["turnover_quote"].to_list(), dtype=float)
        log_turnover = np.log(turnover + 1.0)
        roll_3 = _rolling_sum(turnover, 3)
        roll_20_mean = _rolling_mean(turnover, 20)
        for index, row in enumerate(part.to_dicts()):
            volume_change_1d = math.log((turnover[index] + 1.0) / (turnover[index - 1] + 1.0)) if index >= 1 else float("nan")
            volume_change_3d = math.log((roll_3[index] + 1.0) / (roll_3[index - 3] + 1.0)) if index >= 5 else float("nan")
            volume_persistence = math.log((roll_3[index] / 3.0 + 1.0) / (roll_20_mean[index] + 1.0)) if index >= 19 else float("nan")
            rows.append(
                {
                    "ts_ms": int(row["ts_ms"]),
                    "symbol": symbol,
                    "turnover_quote": float(turnover[index]),
                    "log_turnover": float(log_turnover[index]),
                    "volume_change_1d_raw": volume_change_1d,
                    "volume_change_3d_raw": volume_change_3d,
                    "volume_persistence_raw": volume_persistence,
                    "dollar_volume_rank_raw": float(log_turnover[index]),
                }
            )
    df = pl.DataFrame(rows).sort(["ts_ms", "symbol"])
    for raw_col in (
        "volume_change_1d_raw",
        "volume_change_3d_raw",
        "volume_persistence_raw",
        "dollar_volume_rank_raw",
    ):
        df = _add_cross_sectional_z(df, raw_col, raw_col.replace("_raw", "_z"))
    return df.with_columns(
        (
            0.35 * pl.col("volume_change_1d_z").fill_nan(0.0)
            + 0.35 * pl.col("volume_change_3d_z").fill_nan(0.0)
            + 0.20 * pl.col("volume_persistence_z").fill_nan(0.0)
            + 0.10 * pl.col("dollar_volume_rank_z").fill_nan(0.0)
        )
        .clip(-3.0, 3.0)
        .alias("volume_composite")
    )


def attach_volume_forward_returns(
    features: pl.DataFrame,
    klines: pl.DataFrame,
    *,
    horizons_d: tuple[int, ...] = (1, 3, 7),
) -> pl.DataFrame:
    close_lookup: dict[str, dict[int, float]] = {}
    for key, part in klines.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        close_lookup[symbol] = {
            int(row["ts_ms"]) + MS_PER_HOUR: float(row["close"])
            for row in part.to_dicts()
        }
    output = []
    for row in features.to_dicts():
        item = dict(row)
        symbol = str(row["symbol"])
        ts_ms = int(row["ts_ms"])
        entry_ts = ts_ms + MS_PER_HOUR
        entry = close_lookup.get(symbol, {}).get(entry_ts)
        for horizon_d in horizons_d:
            col = f"forward_return_{horizon_d}d"
            item[col] = float("nan")
            exit_ = close_lookup.get(symbol, {}).get(entry_ts + horizon_d * MS_PER_DAY)
            if entry is not None and exit_ is not None and entry > 0 and exit_ > 0:
                item[col] = math.log(exit_ / entry)
        output.append(item)
    return pl.DataFrame(output).sort(["ts_ms", "symbol"])


def compute_volume_metrics(
    df: pl.DataFrame,
    *,
    horizons_d: tuple[int, ...] = (1, 3, 7),
    signal_columns: dict[str, str] = VOLUME_SCORE_COLUMNS,
    cost_bps: float = 0.0,
) -> list[VolumeMetric]:
    output = []
    for signal_name, signal_col in signal_columns.items():
        if signal_col not in df.columns:
            continue
        for horizon_d in horizons_d:
            return_col = f"forward_return_{horizon_d}d"
            ics = []
            spreads = []
            cost_spreads = []
            observations = 0
            for part in df.partition_by("ts_ms", maintain_order=True):
                values = part.select([signal_col, return_col]).drop_nulls()
                x = np.asarray(values[signal_col].to_list(), dtype=float)
                y = np.asarray(values[return_col].to_list(), dtype=float)
                mask = np.isfinite(x) & np.isfinite(y)
                if mask.sum() < 3:
                    continue
                observations += int(mask.sum())
                ic = rank_correlation(x[mask], y[mask])
                if math.isfinite(ic):
                    ics.append(ic)
                spreads.append(_quantile_spread(x[mask], y[mask]))
                cost_spreads.append(_cost_adjusted_spread(x[mask], y[mask], cost_bps=cost_bps))
            output.append(_summarize_metric(signal_name, horizon_d, ics, spreads, cost_spreads, observations))
    return output


def run_volume_portfolio(
    df: pl.DataFrame,
    *,
    score_name: str,
    score_col: str,
    hold_days: int,
    quantile: float,
    cost_bps: float,
    scenario: str,
) -> dict[str, Any]:
    returns = []
    long_pnl = 0.0
    short_pnl = 0.0
    cost_pnl = 0.0
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    periods = 0
    selected = 0
    return_col = f"forward_return_{hold_days}d"
    first_ts = int(df["ts_ms"].min()) if not df.is_empty() else 0

    for part in df.sort(["ts_ms", "symbol"]).partition_by("ts_ms", maintain_order=True):
        ts_ms = int(part["ts_ms"][0])
        day_index = (ts_ms - first_ts) // MS_PER_DAY
        if day_index % hold_days != 0:
            continue
        values = part.select(["symbol", score_col, return_col]).drop_nulls()
        scores = np.asarray(values[score_col].to_list(), dtype=float)
        fwd = np.asarray(values[return_col].to_list(), dtype=float)
        mask = np.isfinite(scores) & np.isfinite(fwd)
        if mask.sum() < 4:
            continue
        scores = scores[mask]
        simple_returns = np.exp(fwd[mask]) - 1.0
        order = np.argsort(scores)
        bucket = max(1, int(math.ceil(scores.size * quantile)))
        short_idx = order[:bucket]
        long_idx = order[-bucket:]
        long_weight = 0.5 / len(long_idx)
        short_weight = -0.5 / len(short_idx)
        period_long = float(np.sum(simple_returns[long_idx] * long_weight))
        period_short = float(np.sum(simple_returns[short_idx] * short_weight))
        period_cost = -(cost_bps / 10_000.0)
        period_return = period_long + period_short + period_cost
        equity *= 1.0 + period_return
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
        returns.append(period_return)
        long_pnl += period_long
        short_pnl += period_short
        cost_pnl += period_cost
        periods += 1
        selected += len(long_idx) + len(short_idx)

    arr = np.asarray(returns, dtype=float)
    mean_period = float(np.mean(arr)) if arr.size else 0.0
    vol = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
    annual_periods = 365 / hold_days
    sharpe_like = float(mean_period / vol * math.sqrt(annual_periods)) if vol > 1e-12 else 0.0
    return {
        "score": score_name,
        "scenario": scenario,
        "hold_days": hold_days,
        "quantile": quantile,
        "periods": periods,
        "selected_positions": selected,
        "total_return": float(equity - 1.0),
        "mean_period_return": mean_period,
        "volatility": vol,
        "sharpe_like": sharpe_like,
        "max_drawdown": max_dd,
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
        "cost_pnl": cost_pnl,
        "gross_alpha_pnl": long_pnl + short_pnl,
    }


def format_volume_alpha_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Volume Alpha Report",
        "",
        f"Date range: {payload['date_range']['start']} to {payload['date_range']['end']}",
        f"Rows: {payload['rows']}",
        f"Symbols: {', '.join(payload['symbols'])}",
        "",
        "## IC And Quantile Spread",
        "",
        "| Signal | Horizon | Mean IC | IC t-stat | Hit rate | Quantile spread | Cost-adj spread | Obs |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["metrics"]:
        lines.append(
            f"| {item['signal']} | {item['horizon_d']}d | {item['mean_ic']:.4f} | "
            f"{item['ic_tstat']:.2f} | {item['ic_hit_rate']:.2%} | "
            f"{item['mean_quantile_spread']:.6f} | {item['mean_cost_adjusted_spread']:.6f} | "
            f"{item['observations']} |"
        )
    lines.extend(
        [
            "",
            "## Portfolio Sweep",
            "",
            "| Score | Scenario | Hold | Quantile | Return | Sharpe-like | Max DD | Long | Short | Costs | Periods |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in payload["portfolios"]:
        lines.append(
            f"| {item['score']} | {item['scenario']} | {item['hold_days']}d | {item['quantile']:.0%} | "
            f"{item['total_return']:.4f} | {item['sharpe_like']:.2f} | {item['max_drawdown']:.4f} | "
            f"{item['long_pnl']:.4f} | {item['short_pnl']:.4f} | {item['cost_pnl']:.4f} | {item['periods']} |"
        )
    best = payload.get("best_base_portfolio", {})
    lines.extend(
        [
            "",
            "## Best Base Portfolio",
            "",
            f"Score: `{best.get('score')}`",
            f"Hold days: {best.get('hold_days')}",
            f"Quantile: {best.get('quantile', 0.0):.0%}",
            f"Total return: {best.get('total_return', 0.0):.4f}",
            f"Sharpe-like: {best.get('sharpe_like', 0.0):.2f}",
            f"Max drawdown: {best.get('max_drawdown', 0.0):.4f}",
            "",
        ]
    )
    return "\n".join(lines)


def _daily_bars(klines: pl.DataFrame) -> pl.DataFrame:
    return (
        klines.with_columns(
            [
                (pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)).alias("day_start_ms"),
            ]
        )
        .sort(["symbol", "ts_ms"])
        .group_by(["symbol", "day_start_ms"], maintain_order=True)
        .agg(
            [
                pl.col("turnover_quote").sum().alias("turnover_quote"),
                pl.col("close").last().alias("close"),
                pl.len().alias("hourly_bars"),
            ]
        )
        .filter(pl.col("hourly_bars") >= 20)
        .with_columns((pl.col("day_start_ms") + MS_PER_DAY).alias("ts_ms"))
        .select(["ts_ms", "symbol", "turnover_quote", "close", "hourly_bars"])
        .sort(["ts_ms", "symbol"])
    )


def _add_cross_sectional_z(df: pl.DataFrame, input_col: str, output_col: str) -> pl.DataFrame:
    frames = []
    for part in df.partition_by("ts_ms", maintain_order=True):
        values = np.asarray(part[input_col].to_list(), dtype=float)
        finite = np.isfinite(values)
        z = np.full(values.shape, np.nan, dtype=float)
        if finite.sum() >= 3:
            center = float(np.nanmedian(values[finite]))
            mad = float(np.nanmedian(np.abs(values[finite] - center)))
            scale = 1.4826 * mad if mad > 1e-12 else float(np.nanstd(values[finite]))
            if scale > 1e-12:
                z[finite] = np.clip((values[finite] - center) / scale, -3.0, 3.0)
        frames.append(part.with_columns(pl.Series(output_col, z)))
    return pl.concat(frames).sort(["ts_ms", "symbol"]) if frames else df


def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    output = np.full(values.shape, np.nan, dtype=float)
    for index in range(window - 1, values.size):
        output[index] = float(np.sum(values[index - window + 1 : index + 1]))
    return output


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    output = np.full(values.shape, np.nan, dtype=float)
    for index in range(window - 1, values.size):
        output[index] = float(np.mean(values[index - window + 1 : index + 1]))
    return output


def _quantile_spread(signal: np.ndarray, returns: np.ndarray, *, q: float = 0.20) -> float:
    order = np.argsort(signal)
    bucket = max(1, int(math.ceil(signal.size * q)))
    return float(np.nanmean(returns[order[-bucket:]]) - np.nanmean(returns[order[:bucket]]))


def _cost_adjusted_spread(signal: np.ndarray, returns: np.ndarray, *, cost_bps: float, q: float = 0.20) -> float:
    order = np.argsort(signal)
    bucket = max(1, int(math.ceil(signal.size * q)))
    simple = np.exp(returns) - 1.0
    cost = cost_bps / 10_000.0
    return float(np.nanmean(simple[order[-bucket:]] - cost) + np.nanmean(-simple[order[:bucket]] - cost))


def _summarize_metric(
    signal: str,
    horizon_d: int,
    ics: list[float],
    spreads: list[float],
    cost_spreads: list[float],
    observations: int,
) -> VolumeMetric:
    ic_arr = np.asarray(ics, dtype=float)
    mean_ic = float(np.nanmean(ic_arr)) if ic_arr.size else float("nan")
    ic_std = float(np.nanstd(ic_arr, ddof=1)) if ic_arr.size > 1 else float("nan")
    tstat = float(mean_ic / (ic_std / math.sqrt(ic_arr.size))) if ic_arr.size > 1 and ic_std > 1e-12 else float("nan")
    return VolumeMetric(
        signal=signal,
        horizon_d=horizon_d,
        mean_ic=mean_ic,
        ic_tstat=tstat,
        ic_hit_rate=float(np.nanmean(ic_arr > 0.0)) if ic_arr.size else float("nan"),
        mean_quantile_spread=float(np.nanmean(np.asarray(spreads, dtype=float))) if spreads else float("nan"),
        mean_cost_adjusted_spread=float(np.nanmean(np.asarray(cost_spreads, dtype=float))) if cost_spreads else float("nan"),
        observations=observations,
    )


def _best_base_portfolio(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = [row for row in rows if row["scenario"] == "base"]
    return max(base, key=lambda row: float(row["total_return"])) if base else {}


def _date_range(df: pl.DataFrame) -> dict[str, str | None]:
    if df.is_empty():
        return {"start": None, "end": None}
    return {
        "start": datetime.fromtimestamp(int(df["ts_ms"].min()) / 1000, tz=UTC).isoformat(),
        "end": datetime.fromtimestamp(int(df["ts_ms"].max()) / 1000, tz=UTC).isoformat(),
    }
