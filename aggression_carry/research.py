from __future__ import annotations

import json
import math
import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .config import DEFAULT_HORIZONS_H, SignalConfig
from .features import _demean_by_timestamp
from .math_utils import rank_correlation
from .storage import read_dataset, write_dataset


MS_PER_HOUR = 60 * 60 * 1000


SIGNAL_COLUMNS = {
    "aggression": "aggression_confirmed",
    "relative_volume": "rel_volume_z",
    "momentum": "momentum_z",
    "carry": "carry_z_adjusted",
    "quality": "quality_z",
    "oi_impulse": "oi_impulse_z",
    "composite": "composite_score",
}


@dataclass(frozen=True, slots=True)
class SignalMetrics:
    signal: str
    horizon_h: int
    mean_ic: float
    ic_tstat: float
    ic_hit_rate: float
    mean_quantile_spread: float
    mean_cost_adjusted_spread: float
    observations: int


def run_alpha_report(
    data_root: str | Path,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    cost_bps: float = 0.0,
    config_payload: Any | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    features = read_dataset(data_root, "features_1h")
    klines = read_dataset(data_root, "klines_1h")
    if features.is_empty():
        raise RuntimeError("features_1h is empty; run build-features first")
    enriched = attach_forward_returns(features, klines, horizons_h=horizons_h)
    write_dataset(enriched, data_root, "research_returns")
    metrics = compute_signal_metrics(enriched, horizons_h=horizons_h, cost_bps=cost_bps)
    ablations = compute_leave_one_out_metrics(enriched, horizons_h=horizons_h, cost_bps=cost_bps)
    timestamp_ic = compute_timestamp_ic_table(enriched, horizons_h=horizons_h)
    quantile_ledger = compute_quantile_ledger(enriched, horizons_h=horizons_h, cost_bps=cost_bps)
    monthly_spreads = compute_monthly_spreads(enriched, horizons_h=horizons_h, cost_bps=cost_bps)
    if timestamp_ic:
        write_dataset(pl.DataFrame(timestamp_ic), data_root, "research_timestamp_ic", partition_by=("signal",))
    if quantile_ledger:
        write_dataset(pl.DataFrame(quantile_ledger), data_root, "research_quantile_ledger", partition_by=("signal", "horizon_h"))
    if monthly_spreads:
        write_dataset(pl.DataFrame(monthly_spreads), data_root, "research_monthly_spreads", partition_by=("signal",))
    payload = {
        "config_hash": _stable_hash(
            {
                "horizons_h": horizons_h,
                "signals": list(SIGNAL_COLUMNS),
                "cost_bps": cost_bps,
                "config": config_payload,
                "dataset_manifest": _dataset_manifest(enriched),
            }
        ),
        "date_range": _date_range(enriched),
        "signals": [asdict(item) for item in metrics],
        "leave_one_out": [asdict(item) for item in ablations],
        "timestamp_ic": timestamp_ic,
        "quantile_ledger": quantile_ledger,
        "monthly_spreads": monthly_spreads,
        "monthly_consistency": compute_monthly_consistency(enriched, horizons_h=horizons_h),
        "acceptance_gates": _acceptance_gates(metrics),
        "rows": enriched.height,
        "symbols": sorted(enriched["symbol"].unique().to_list()),
        "horizons_h": list(horizons_h),
    }
    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "alpha_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "alpha_report.md").write_text(format_alpha_report(payload), encoding="utf-8")
    return payload


def attach_forward_returns(
    features: pl.DataFrame,
    klines: pl.DataFrame,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
) -> pl.DataFrame:
    close_by_symbol: dict[str, list[tuple[int, float]]] = {}
    for symbol, part in klines.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol_key = symbol[0] if isinstance(symbol, tuple) else symbol
        close_by_symbol[str(symbol_key)] = [
            (int(row["ts_ms"]), float(row["close"])) for row in part.to_dicts()
        ]
    close_lookup = {symbol: {ts: close for ts, close in rows} for symbol, rows in close_by_symbol.items()}

    rows = []
    for row in features.to_dicts():
        symbol = str(row["symbol"])
        ts_ms = int(row["ts_ms"])
        item = dict(row)
        for horizon in horizons_h:
            col = f"forward_return_{horizon}h"
            item[col] = float("nan")
            entry_ts = ts_ms + MS_PER_HOUR
            exit_ts = ts_ms + (1 + horizon) * MS_PER_HOUR
            entry = close_lookup.get(symbol, {}).get(entry_ts)
            exit_ = close_lookup.get(symbol, {}).get(exit_ts)
            if entry is None or exit_ is None:
                continue
            if entry > 0 and exit_ > 0:
                item[col] = math.log(exit_ / entry)
        rows.append(item)
    return pl.DataFrame(rows).sort(["ts_ms", "symbol"])


def compute_signal_metrics(
    df: pl.DataFrame,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    signal_columns: dict[str, str] = SIGNAL_COLUMNS,
    cost_bps: float = 0.0,
) -> list[SignalMetrics]:
    output: list[SignalMetrics] = []
    for signal_name, signal_col in signal_columns.items():
        if signal_col not in df.columns:
            continue
        for horizon in horizons_h:
            return_col = f"forward_return_{horizon}h"
            ics = []
            spreads = []
            cost_adjusted_spreads = []
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
                cost_adjusted_spreads.append(_cost_adjusted_quantile_spread(x[mask], y[mask], cost_bps=cost_bps))
            output.append(_summarize_metrics(signal_name, horizon, ics, spreads, cost_adjusted_spreads, observations))
    return output


def compute_leave_one_out_metrics(
    df: pl.DataFrame,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    signal_config: SignalConfig | None = None,
    cost_bps: float = 0.0,
) -> list[SignalMetrics]:
    cfg = signal_config or SignalConfig()
    component_cols = {
        "aggression_confirmed": "aggression_confirmed",
        "momentum": "momentum_z",
        "carry": "carry_z_adjusted",
        "quality": "quality_z",
        "oi_impulse": "oi_impulse_z",
    }
    frames = df
    ablation_cols: dict[str, str] = {}
    for omitted, col in component_cols.items():
        expr = pl.lit(0.0)
        for weight_name, feature_col in component_cols.items():
            if weight_name == omitted:
                continue
            expr = expr + cfg.weights[weight_name] * pl.col(feature_col)
        raw_col = f"score_without_{omitted}_raw"
        final_col = f"score_without_{omitted}"
        frames = frames.with_columns(expr.clip(-3.0, 3.0).alias(raw_col))
        frames = _demean_by_timestamp(frames, raw_col, final_col)
        ablation_cols[f"without_{omitted}"] = final_col
    return compute_signal_metrics(frames, horizons_h=horizons_h, signal_columns=ablation_cols, cost_bps=cost_bps)


def compute_timestamp_ic_table(
    df: pl.DataFrame,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    signal_columns: dict[str, str] = SIGNAL_COLUMNS,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal_name, signal_col in signal_columns.items():
        if signal_col not in df.columns:
            continue
        for horizon in horizons_h:
            return_col = f"forward_return_{horizon}h"
            for part in df.partition_by("ts_ms", maintain_order=True):
                values = part.select([signal_col, return_col]).drop_nulls()
                x = np.asarray(values[signal_col].to_list(), dtype=float)
                y = np.asarray(values[return_col].to_list(), dtype=float)
                mask = np.isfinite(x) & np.isfinite(y)
                if mask.sum() < 3:
                    continue
                rows.append(
                    {
                        "signal": signal_name,
                        "horizon_h": horizon,
                        "ts_ms": int(part["ts_ms"][0]),
                        "ic": rank_correlation(x[mask], y[mask]),
                        "observations": int(mask.sum()),
                    }
                )
    return rows


def compute_quantile_ledger(
    df: pl.DataFrame,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    signal_columns: dict[str, str] = SIGNAL_COLUMNS,
    cost_bps: float = 0.0,
    q: float = 0.20,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cost = cost_bps / 10_000.0
    for signal_name, signal_col in signal_columns.items():
        if signal_col not in df.columns:
            continue
        for horizon in horizons_h:
            return_col = f"forward_return_{horizon}h"
            for part in df.partition_by("ts_ms", maintain_order=True):
                candidates = part.select(["ts_ms", "symbol", signal_col, return_col]).drop_nulls()
                scores = np.asarray(candidates[signal_col].to_list(), dtype=float)
                returns = np.asarray(candidates[return_col].to_list(), dtype=float)
                mask = np.isfinite(scores) & np.isfinite(returns)
                if mask.sum() < 3:
                    continue
                symbols = np.asarray(candidates["symbol"].to_list(), dtype=object)[mask]
                scores = scores[mask]
                returns = returns[mask]
                order = np.argsort(scores)
                bucket = max(1, int(math.ceil(scores.size * q)))
                selected = [(index, "bottom", "short") for index in order[:bucket]]
                selected.extend((index, "top", "long") for index in order[-bucket:])
                for index, bucket_name, side in selected:
                    simple_return = math.exp(float(returns[index])) - 1.0
                    gross_return = simple_return if side == "long" else -simple_return
                    net_return = gross_return - cost
                    rows.append(
                        {
                            "signal": signal_name,
                            "horizon_h": horizon,
                            "ts_ms": int(part["ts_ms"][0]),
                            "month": _month(int(part["ts_ms"][0])),
                            "symbol": str(symbols[index]),
                            "bucket": bucket_name,
                            "side": side,
                            "score": float(scores[index]),
                            "forward_return": simple_return,
                            "gross_side_return": gross_return,
                            "estimated_cost": cost,
                            "net_side_return": net_return,
                            "contribution": net_return / bucket,
                        }
                    )
    return rows


def compute_monthly_spreads(
    df: pl.DataFrame,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    signal_columns: dict[str, str] = SIGNAL_COLUMNS,
    cost_bps: float = 0.0,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with_month = df.with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month")
    )
    for signal_name, signal_col in signal_columns.items():
        if signal_col not in df.columns:
            continue
        for horizon in horizons_h:
            return_col = f"forward_return_{horizon}h"
            for part in with_month.partition_by("month", maintain_order=True):
                spreads = []
                cost_adjusted = []
                for ts_part in part.partition_by("ts_ms", maintain_order=True):
                    values = ts_part.select([signal_col, return_col]).drop_nulls()
                    x = np.asarray(values[signal_col].to_list(), dtype=float)
                    y = np.asarray(values[return_col].to_list(), dtype=float)
                    mask = np.isfinite(x) & np.isfinite(y)
                    if mask.sum() < 3:
                        continue
                    spreads.append(_quantile_spread(x[mask], y[mask]))
                    cost_adjusted.append(_cost_adjusted_quantile_spread(x[mask], y[mask], cost_bps=cost_bps))
                if spreads:
                    rows.append(
                        {
                            "signal": signal_name,
                            "horizon_h": horizon,
                            "month": str(part["month"][0]),
                            "mean_quantile_spread": float(np.nanmean(np.asarray(spreads, dtype=float))),
                            "mean_cost_adjusted_spread": float(np.nanmean(np.asarray(cost_adjusted, dtype=float))),
                            "timestamps": len(spreads),
                        }
                    )
    return rows


def compute_monthly_consistency(
    df: pl.DataFrame,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if df.is_empty():
        return out
    with_month = df.with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month")
    )
    for horizon in horizons_h:
        return_col = f"forward_return_{horizon}h"
        positive = 0
        total = 0
        for part in with_month.partition_by("month", maintain_order=True):
            values = part.select(["composite_score", return_col]).drop_nulls()
            x = np.asarray(values["composite_score"].to_list(), dtype=float)
            y = np.asarray(values[return_col].to_list(), dtype=float)
            mask = np.isfinite(x) & np.isfinite(y)
            if mask.sum() < 3:
                continue
            spread = _quantile_spread(x[mask], y[mask])
            total += 1
            positive += int(spread > 0)
        out.append(
            {
                "signal": "composite",
                "horizon_h": horizon,
                "positive_months": positive,
                "total_months": total,
                "positive_month_rate": positive / total if total else float("nan"),
            }
        )
    return out


def format_alpha_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Aggression-Carry Alpha Report",
        "",
        f"Config hash: `{payload['config_hash']}`",
        f"Date range: {payload['date_range']['start']} to {payload['date_range']['end']}",
        f"Rows: {payload['rows']}",
        f"Symbols: {', '.join(payload['symbols'])}",
        "",
        "## Signal IC And Quantile Spread",
        "",
        "| Signal | Horizon | Mean IC | IC t-stat | Hit rate | Quantile spread | Cost-adj spread | Obs |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["signals"]:
        lines.append(
            f"| {item['signal']} | {item['horizon_h']}h | {item['mean_ic']:.4f} | "
            f"{item['ic_tstat']:.2f} | {item['ic_hit_rate']:.2%} | "
            f"{item['mean_quantile_spread']:.6f} | {item['mean_cost_adjusted_spread']:.6f} | "
            f"{item['observations']} |"
        )
    lines.extend(
        [
            "",
            "## Leave-One-Out Composite Ablations",
            "",
            "| Ablation | Horizon | Mean IC | IC t-stat | Hit rate | Quantile spread | Cost-adj spread | Obs |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in payload["leave_one_out"]:
        lines.append(
            f"| {item['signal']} | {item['horizon_h']}h | {item['mean_ic']:.4f} | "
            f"{item['ic_tstat']:.2f} | {item['ic_hit_rate']:.2%} | "
            f"{item['mean_quantile_spread']:.6f} | {item['mean_cost_adjusted_spread']:.6f} | "
            f"{item['observations']} |"
        )
    lines.extend(
        [
            "",
            "## Acceptance Gate Snapshot",
            "",
            "| Gate | Passed | Value |",
            "|---|---:|---:|",
        ]
    )
    for item in payload["acceptance_gates"]:
        lines.append(f"| {item['gate']} | {item['passed']} | {item['value']:.6f} |")
    lines.extend(
        [
            "",
            "## Monthly Consistency",
            "",
            "| Signal | Horizon | Positive months | Total months | Positive rate |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for item in payload["monthly_consistency"]:
        lines.append(
            f"| {item['signal']} | {item['horizon_h']}h | {item['positive_months']} | "
            f"{item['total_months']} | {item['positive_month_rate']:.2%} |"
        )
    lines.append("")
    return "\n".join(lines)


def _summarize_metrics(
    signal_name: str,
    horizon: int,
    ics: list[float],
    spreads: list[float],
    cost_adjusted_spreads: list[float],
    observations: int,
) -> SignalMetrics:
    ic_arr = np.asarray(ics, dtype=float)
    spread_arr = np.asarray(spreads, dtype=float)
    cost_spread_arr = np.asarray(cost_adjusted_spreads, dtype=float)
    mean_ic = float(np.nanmean(ic_arr)) if ic_arr.size else float("nan")
    ic_std = float(np.nanstd(ic_arr, ddof=1)) if ic_arr.size > 1 else float("nan")
    tstat = float(mean_ic / (ic_std / math.sqrt(ic_arr.size))) if ic_arr.size > 1 and ic_std > 1e-12 else float("nan")
    hit_rate = float(np.nanmean(ic_arr > 0.0)) if ic_arr.size else float("nan")
    spread = float(np.nanmean(spread_arr)) if spread_arr.size else float("nan")
    cost_spread = float(np.nanmean(cost_spread_arr)) if cost_spread_arr.size else float("nan")
    return SignalMetrics(
        signal=signal_name,
        horizon_h=horizon,
        mean_ic=mean_ic,
        ic_tstat=tstat,
        ic_hit_rate=hit_rate,
        mean_quantile_spread=spread,
        mean_cost_adjusted_spread=cost_spread,
        observations=observations,
    )


def _quantile_spread(signal: np.ndarray, returns: np.ndarray, *, q: float = 0.20) -> float:
    order = np.argsort(signal)
    bucket = max(1, int(math.ceil(signal.size * q)))
    bottom = returns[order[:bucket]]
    top = returns[order[-bucket:]]
    return float(np.nanmean(top) - np.nanmean(bottom))


def _cost_adjusted_quantile_spread(
    signal: np.ndarray,
    returns: np.ndarray,
    *,
    cost_bps: float,
    q: float = 0.20,
) -> float:
    order = np.argsort(signal)
    bucket = max(1, int(math.ceil(signal.size * q)))
    simple_returns = np.exp(returns) - 1.0
    bottom_short = -simple_returns[order[:bucket]]
    top_long = simple_returns[order[-bucket:]]
    cost = cost_bps / 10_000.0
    return float(np.nanmean(top_long - cost) + np.nanmean(bottom_short - cost))


def _date_range(df: pl.DataFrame) -> dict[str, str | None]:
    if df.is_empty():
        return {"start": None, "end": None}
    start_ms = int(df["ts_ms"].min())
    end_ms = int(df["ts_ms"].max())
    return {
        "start": datetime.fromtimestamp(start_ms / 1000, tz=UTC).isoformat(),
        "end": datetime.fromtimestamp(end_ms / 1000, tz=UTC).isoformat(),
    }


def _stable_hash(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _dataset_manifest(df: pl.DataFrame) -> dict[str, Any]:
    if df.is_empty():
        return {"rows": 0, "symbols": 0, "start_ts_ms": None, "end_ts_ms": None}
    return {
        "rows": df.height,
        "symbols": sorted(df["symbol"].unique().to_list()) if "symbol" in df.columns else [],
        "start_ts_ms": int(df["ts_ms"].min()) if "ts_ms" in df.columns else None,
        "end_ts_ms": int(df["ts_ms"].max()) if "ts_ms" in df.columns else None,
    }


def _month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m")


def _acceptance_gates(metrics: list[SignalMetrics]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in metrics:
        if item.signal != "aggression":
            continue
        out.append(
            {
                "gate": f"aggression_mean_ic_positive_{item.horizon_h}h",
                "passed": bool(item.mean_ic > 0),
                "value": item.mean_ic,
            }
        )
        out.append(
            {
                "gate": f"aggression_cost_adjusted_spread_positive_{item.horizon_h}h",
                "passed": bool(item.mean_cost_adjusted_spread > 0),
                "value": item.mean_cost_adjusted_spread,
            }
        )
    return out
