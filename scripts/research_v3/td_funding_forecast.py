#!/usr/bin/env python3
"""T-D: cumulative-funding forecasts beyond the next interval (CONTINUOUS symbols).

Exploratory Lane-1 walk-forward development inside the spent window.  For each
realized settlement time t, the targets are the realized cumulative funding
sums over (t, t+24h], (t, t+48h], (t, t+72h].  All predictors are PIT-known at
t (the just-settled rate, trailing settlement history, and 1h aux bars whose
bar end is at or before t).

Declared candidates:
- persistence (baseline):   rate_t * n_settlements(horizon)
- ewma_hl{3,10,30}:         EWMA of settled rates (half-life in settlements) * n
- meanrev_phi{0.5,0.8}:     (mu + phi * (rate_t - mu)) * n, mu = trailing mean
                            of the last 90 settlements (min 10, else rate_t)
- ols_premium:              OLS on [rate_t, premium close, premium 8h mean,
                            premium 8h trend, basis, OI 24h change] fitted on
                            the early era only, predicting the horizon's mean
                            per-settlement rate, scaled by n

Walk-forward: train era = settlements before the V2 ledger era midpoint
(2023-02-22); scored era = at or after it.  Metrics: MAE, RMSE, and tail MAE
restricted to |target| >= the train era's 95th and 99th percentile of |target|
(the crazy-funding cases are the point).

Stage-2 trigger (declared before scores are inspected): a candidate must beat
persistence on the scored era, 24h horizon, by >= 10 percent on BOTH overall
MAE and q95 tail MAE.  If triggered, the winning model's floor replaces the
T-B floor and the T-B entry-filter grid is re-run for comparison.

No alpha or promotion claim.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/td_funding_forecast.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_HOUR, exact_duration_ms  # noqa: E402
from scripts.research_v3 import common  # noqa: E402

HORIZON_HOURS: tuple[int, ...] = (24, 48, 72)
EWMA_HALF_LIVES: tuple[int, ...] = (3, 10, 30)
MEANREV_PHIS: tuple[float, ...] = (0.5, 0.8)
TRAILING_MEAN_WINDOW = 90
TRAILING_MEAN_MIN = 10
TAIL_QUANTILES: tuple[float, ...] = (0.95, 0.99)
STAGE2_HORIZON = 24
STAGE2_MIN_IMPROVEMENT = 0.10
SAMPLE_START = dt.date(2021, 5, 1)
SAMPLE_END_EXCLUSIVE = dt.date(2024, 12, 1)


def read_aux_dataset(root: Path, dataset: str, columns: list[str], symbols: set[str]) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    day = common.FUNDING_START
    symbol_list = sorted(symbols)
    while day < common.FUNDING_END_EXCLUSIVE:
        partition = root / dataset / f"date={day.isoformat()}"
        if partition.is_dir():
            files = sorted(partition.rglob("*.parquet"))
            if files:
                frame = pl.read_parquet(files, columns=columns)
                frames.append(frame.filter(pl.col("symbol").is_in(symbol_list)))
        day += dt.timedelta(days=1)
    if not frames:
        raise RuntimeError(f"no {dataset} files under {root}")
    return pl.concat(frames, how="vertical", rechunk=True).sort(["symbol", "ts_ms"])


def build_aux_panel(root: Path, symbols: set[str]) -> pl.DataFrame:
    premium = read_aux_dataset(root, "premium_index_1h", ["ts_ms", "symbol", "close"], symbols).rename(
        {"close": "premium_close"}
    )
    mark = read_aux_dataset(root, "mark_price_1h", ["ts_ms", "symbol", "close"], symbols).rename(
        {"close": "mark_close"}
    )
    index = read_aux_dataset(root, "index_price_1h", ["ts_ms", "symbol", "close"], symbols).rename(
        {"close": "index_close"}
    )
    oi = read_aux_dataset(root, "open_interest", ["ts_ms", "symbol", "open_interest"], symbols)
    panel = premium.join(mark, on=["symbol", "ts_ms"], how="full", coalesce=True)
    panel = panel.join(index, on=["symbol", "ts_ms"], how="full", coalesce=True)
    panel = panel.join(oi, on=["symbol", "ts_ms"], how="full", coalesce=True)
    return panel.sort(["symbol", "ts_ms"])


def _last_at_or_before(ts_array: np.ndarray, values: np.ndarray, at_ms: int) -> float:
    index = int(np.searchsorted(ts_array, at_ms, side="right")) - 1
    if index < 0:
        return float("nan")
    return float(values[index])


def build_settlement_table(
    funding: pl.DataFrame,
    aux: pl.DataFrame,
) -> pl.DataFrame:
    """One row per settlement with PIT features and horizon targets."""
    aux_by_symbol: dict[str, dict[str, np.ndarray]] = {}
    for key, part in aux.partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        part = part.sort("ts_ms")
        aux_by_symbol[symbol] = {
            "bar_end": part["ts_ms"].to_numpy() + MS_PER_HOUR,
            "premium": part["premium_close"].to_numpy(),
            "mark": part["mark_close"].to_numpy(),
            "index": part["index_close"].to_numpy(),
            "oi": part["open_interest"].to_numpy(),
        }

    rows: dict[str, list[Any]] = {name: [] for name in (
        "symbol", "ts_ms", "rate", "n24", "n48", "n72", "target24", "target48", "target72",
        "trailing_mean", "premium_close", "premium_mean_8h", "premium_trend_8h", "basis", "oi_chg_24h",
        *[f"ewma_hl{hl}" for hl in EWMA_HALF_LIVES],
    )}
    for key, part in funding.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        ts = part["ts_ms"].to_numpy()
        rate = part["funding_rate"].to_numpy()
        count = len(ts)
        if count == 0:
            continue
        prefix = np.concatenate([[0.0], np.cumsum(rate)])
        ewmas = {}
        for half_life in EWMA_HALF_LIVES:
            alpha = 1.0 - 0.5 ** (1.0 / half_life)
            series = np.empty(count)
            running = rate[0]
            for i in range(count):
                running = alpha * rate[i] + (1.0 - alpha) * running if i else rate[0]
                series[i] = running
            ewmas[half_life] = series
        symbol_aux = aux_by_symbol.get(symbol)
        symbol_end = int(ts[-1])
        for i in range(count):
            t = int(ts[i])
            horizon_ends = [t + exact_duration_ms(hours=h) for h in HORIZON_HOURS]
            if horizon_ends[-1] > symbol_end:
                continue
            his = [int(np.searchsorted(ts, end, side="right")) for end in horizon_ends]
            lo = i + 1
            rows["symbol"].append(symbol)
            rows["ts_ms"].append(t)
            rows["rate"].append(float(rate[i]))
            for h_label, hi in zip(("24", "48", "72"), his):
                rows[f"n{h_label}"].append(hi - lo)
                rows[f"target{h_label}"].append(float(prefix[hi] - prefix[lo]))
            window_lo = max(0, i - TRAILING_MEAN_WINDOW + 1)
            window = rate[window_lo : i + 1]
            rows["trailing_mean"].append(
                float(window.mean()) if len(window) >= TRAILING_MEAN_MIN else float(rate[i])
            )
            for half_life in EWMA_HALF_LIVES:
                rows[f"ewma_hl{half_life}"].append(float(ewmas[half_life][i]))
            if symbol_aux is None:
                for name in ("premium_close", "premium_mean_8h", "premium_trend_8h", "basis", "oi_chg_24h"):
                    rows[name].append(float("nan"))
            else:
                bar_end = symbol_aux["bar_end"]
                prem = symbol_aux["premium"]
                idx_now = int(np.searchsorted(bar_end, t, side="right")) - 1
                prem_now = float(prem[idx_now]) if idx_now >= 0 else float("nan")
                prem_8h_ago = _last_at_or_before(bar_end, prem, t - 8 * MS_PER_HOUR)
                if idx_now >= 0:
                    window8 = prem[max(0, idx_now - 7) : idx_now + 1]
                    finite = window8[np.isfinite(window8)]
                    prem_mean = float(finite.mean()) if len(finite) else float("nan")
                else:
                    prem_mean = float("nan")
                mark_now = _last_at_or_before(bar_end, symbol_aux["mark"], t)
                index_now = _last_at_or_before(bar_end, symbol_aux["index"], t)
                basis = (
                    (mark_now - index_now) / index_now
                    if np.isfinite(mark_now) and np.isfinite(index_now) and index_now != 0.0
                    else float("nan")
                )
                oi_now = _last_at_or_before(bar_end, symbol_aux["oi"], t)
                oi_prev = _last_at_or_before(bar_end, symbol_aux["oi"], t - 24 * MS_PER_HOUR)
                oi_chg = (
                    oi_now / oi_prev - 1.0
                    if np.isfinite(oi_now) and np.isfinite(oi_prev) and oi_prev > 0.0
                    else float("nan")
                )
                rows["premium_close"].append(prem_now)
                rows["premium_mean_8h"].append(prem_mean)
                rows["premium_trend_8h"].append(
                    prem_now - prem_8h_ago
                    if np.isfinite(prem_now) and np.isfinite(prem_8h_ago)
                    else float("nan")
                )
                rows["basis"].append(basis)
                rows["oi_chg_24h"].append(oi_chg)
    return pl.DataFrame(rows)


def fit_ols(train: pl.DataFrame, features: list[str], target: str) -> np.ndarray | None:
    frame = train.select([*features, target]).drop_nans().drop_nulls()
    if frame.height < 1000:
        return None
    x = np.column_stack([frame[f].to_numpy() for f in features] + [np.ones(frame.height)])
    y = frame[target].to_numpy()
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    return coef


def ols_predict(frame: pl.DataFrame, features: list[str], coef: np.ndarray) -> np.ndarray:
    x = np.column_stack([frame[f].to_numpy() for f in features] + [np.ones(frame.height)])
    prediction = x @ coef
    invalid = ~np.isfinite(x).all(axis=1)
    prediction[invalid] = np.nan
    return prediction


def error_metrics(target: np.ndarray, prediction: np.ndarray, tail_masks: dict[str, np.ndarray]) -> dict[str, Any]:
    valid = np.isfinite(prediction) & np.isfinite(target)
    error = np.abs(prediction[valid] - target[valid])
    output: dict[str, Any] = {
        "n": int(valid.sum()),
        "coverage": float(valid.mean()) if len(valid) else 0.0,
        "mae_bps": float(error.mean()) * 10_000.0 if len(error) else None,
        "rmse_bps": float(np.sqrt((error**2).mean())) * 10_000.0 if len(error) else None,
    }
    for label, mask in tail_masks.items():
        tail_valid = valid & mask
        tail_error = np.abs(prediction[tail_valid] - target[tail_valid])
        output[f"mae_{label}_bps"] = float(tail_error.mean()) * 10_000.0 if len(tail_error) else None
        output[f"n_{label}"] = int(tail_valid.sum())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = common.REPORT_ROOT / "t-d" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger = common.load_ledger("continuous")
    symbols = set(ledger["symbol"].to_list())
    funding = pl.read_parquet(shared_dir / "funding_events.parquet").filter(
        pl.col("symbol").is_in(sorted(symbols))
    )

    aux_path = shared_dir / "aux_panel_1h.parquet"
    aux, aux_sha = common.cached_parquet(aux_path, lambda: build_aux_panel(args.data_root, symbols))
    print(f"aux panel: {aux.shape} sha={aux_sha[:12]}", flush=True)

    table_path = out_dir / "td_settlement_table.parquet"
    table, table_sha = common.cached_parquet(table_path, lambda: build_settlement_table(funding, aux))
    print(f"settlement table: {table.shape} sha={table_sha[:12]}", flush=True)

    start_ms = int(dt.datetime.combine(SAMPLE_START, dt.time(), dt.timezone.utc).timestamp() * 1000)
    end_ms = int(
        dt.datetime.combine(SAMPLE_END_EXCLUSIVE, dt.time(), dt.timezone.utc).timestamp() * 1000
    )
    table = table.filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))
    midpoint = common.era_midpoint_ts_ms(ledger)
    train = table.filter(pl.col("ts_ms") < midpoint)
    score = table.filter(pl.col("ts_ms") >= midpoint)
    print(f"train rows: {train.height}, score rows: {score.height}", flush=True)

    ols_features = [
        "rate", "premium_close", "premium_mean_8h", "premium_trend_8h", "basis", "oi_chg_24h",
    ]
    scoreboard_rows: list[dict[str, Any]] = []
    stage2_metrics: dict[str, dict[str, Any]] = {}
    for horizon in HORIZON_HOURS:
        label = str(horizon)
        target_col = f"target{label}"
        n_col = f"n{label}"
        target_train = train[target_col].to_numpy()
        tail_thresholds = {
            f"tail_q{int(q * 100)}": float(np.quantile(np.abs(target_train), q)) for q in TAIL_QUANTILES
        }
        coef = fit_ols(
            train.with_columns((pl.col(target_col) / pl.col(n_col).clip(1)).alias("_mean_rate")),
            ols_features,
            "_mean_rate",
        )
        for half, part in (("score_all", score),):
            target = part[target_col].to_numpy()
            n_events = part[n_col].to_numpy().astype(float)
            tail_masks = {
                name: np.abs(target) >= threshold for name, threshold in tail_thresholds.items()
            }
            predictions: dict[str, np.ndarray] = {
                "persistence": part["rate"].to_numpy() * n_events,
            }
            for half_life in EWMA_HALF_LIVES:
                predictions[f"ewma_hl{half_life}"] = part[f"ewma_hl{half_life}"].to_numpy() * n_events
            mu = part["trailing_mean"].to_numpy()
            rate_now = part["rate"].to_numpy()
            for phi in MEANREV_PHIS:
                predictions[f"meanrev_phi{phi}"] = (mu + phi * (rate_now - mu)) * n_events
            if coef is not None:
                predictions["ols_premium"] = ols_predict(part, ols_features, coef) * n_events
            for model, prediction in predictions.items():
                row = {"horizon_h": horizon, "sample": half, "model": model}
                row.update(error_metrics(target, prediction, tail_masks))
                scoreboard_rows.append(row)
                if horizon == STAGE2_HORIZON:
                    stage2_metrics[model] = row

    scoreboard = pl.from_dicts(scoreboard_rows, infer_schema_length=None)
    scoreboard_path = out_dir / "td_scoreboard.csv"
    scoreboard.write_csv(scoreboard_path)
    print(scoreboard.filter(pl.col("horizon_h") == STAGE2_HORIZON), flush=True)

    persistence = stage2_metrics.get("persistence") or {}
    stage2_winner: str | None = None
    for model, row in stage2_metrics.items():
        if model == "persistence" or row.get("mae_bps") is None:
            continue
        base_mae = persistence.get("mae_bps")
        base_tail = persistence.get("mae_tail_q95_bps")
        model_tail = row.get("mae_tail_q95_bps")
        if base_mae and base_tail and model_tail is not None:
            mae_gain = 1.0 - row["mae_bps"] / base_mae
            tail_gain = 1.0 - model_tail / base_tail
            if mae_gain >= STAGE2_MIN_IMPROVEMENT and tail_gain >= STAGE2_MIN_IMPROVEMENT:
                if stage2_winner is None or row["mae_bps"] < stage2_metrics[stage2_winner]["mae_bps"]:
                    stage2_winner = model
    stage2 = {
        "rule": f"model beats persistence on scored era {STAGE2_HORIZON}h by >= "
        f"{STAGE2_MIN_IMPROVEMENT:.0%} on BOTH overall MAE and q95 tail MAE",
        "triggered": stage2_winner is not None,
        "winner": stage2_winner,
    }
    print(json.dumps({"stage2": stage2}), flush=True)

    common.write_manifest(
        out_dir,
        kind="strategy_research_v3_td_funding_forecast",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                "funding_events.parquet": common.sha256_file(shared_dir / "funding_events.parquet"),
                "aux_panel_1h.parquet": aux_sha,
            },
            "shared_cache_dir": str(shared_dir),
        },
        params={
            "symbols": len(symbols),
            "horizons_h": list(HORIZON_HOURS),
            "sample_window": [SAMPLE_START.isoformat(), SAMPLE_END_EXCLUSIVE.isoformat()],
            "train_before": common.iso_date(midpoint),
            "models": {
                "persistence": "rate_t * n_settlements",
                "ewma": {"half_lives_settlements": list(EWMA_HALF_LIVES)},
                "meanrev": {
                    "phis": list(MEANREV_PHIS),
                    "mu": f"trailing mean of last {TRAILING_MEAN_WINDOW} settlements (min {TRAILING_MEAN_MIN})",
                },
                "ols_premium": {"features": ols_features, "fit": "train era only, pooled"},
            },
            "tail_quantiles_from_train": list(TAIL_QUANTILES),
            "stage2": stage2,
        },
        output_files={
            "td_scoreboard.csv": scoreboard_path,
            "td_settlement_table.parquet": table_path,
        },
        extra={"explicit_non_conclusions": [
            "walk-forward development inside the spent window; not confirmatory evidence",
            "no alpha or promotion claim; forward evidence accrues only via the rolling ledger",
        ]},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
