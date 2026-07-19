#!/usr/bin/env python3
"""T-H: expected-net ranker over the frozen V4 feature set (walk-forward, numpy-only).

Exploratory Lane-1 model development inside the spent window (V4 draft thesis
T-H).  Frozen feature set (all PIT at entry bar close, declared in the draft;
nothing added after the first fit):

  hours_since_high_168h, r_1h, mom_delta, known_rate_prev, funding_ewma_hl3,
  n_intervals_planned_hold, cost_per_unit, score, age_days_censored

Models: ridge regression (lambda=1.0, standardized winsorized features,
rank-transformed target net-per-unit-notional) plus a logistic P(net>0) twin
(IRLS, same L2).  Walk-forward: expanding window, quarterly refits starting
2022-07-01; the training set at each refit is every trade whose EXIT precedes
the refit boundary (strictly PIT); each trade is scored by the last refit at
or before its entry.  Trades entered before the first refit stay unscored and
pass through at weight 1.0.

Declared actions: drop the bottom walk-forward score decile (train-window
decile cutoffs); sizing by score quintile with the fixed map 0.25 / 0.75 /
1.0 / 1.25 / 1.5.  Comparators on the SAME scored sample: baseline, the best
declared T-E cell (skip_h1), the best declared T-G cell (combo_K-0.001), and
T-E AND T-G combined; the ML thesis survives only if it beats the combined
conditioner.  Coefficient stability across refits is reported (sign flips are
a refutation regardless of net).  Double-verification arm: the final refit's
ridge model scores both T-A render books (ranking-transfer diagnostic only;
for render entries before the final train boundary this is not a tradeable
rule and is labelled as such).  No alpha or promotion claim.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/th_expected_net.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import bisect
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

from liquidity_migration._common import MS_PER_DAY, exact_duration_ms  # noqa: E402
from scripts.research_v3 import common, v4_shared  # noqa: E402
from scripts.research_v3 import tc_pump_deceleration as tc  # noqa: E402

FEATURES: tuple[str, ...] = (
    "hours_since_high_168h", "r_1h", "mom_delta", "known_rate_prev", "funding_ewma_hl3",
    "n_intervals_planned_hold", "cost_per_unit", "score", "age_days_censored",
)
RIDGE_LAMBDA = 1.0
LOGISTIC_L2 = 1.0
WINSOR_QUANTILES = (0.01, 0.99)
EWMA_HALF_LIFE = 3
FIRST_REFIT = dt.date(2022, 7, 1)
PLANNED_HOLD_MS = exact_duration_ms(hours=24)
QUINTILE_SIZING = {1: 0.25, 2: 0.75, 3: 1.0, 4: 1.25, 5: 1.5}
COMPARATOR_TE = "skip_h1"
COMPARATOR_TG = "combo_K-0.001"
TG_CUT = -0.001


def quarterly_refits(start: dt.date, end_exclusive: dt.date) -> list[int]:
    dates = []
    year, month = start.year, start.month
    while dt.date(year, month, 1) < end_exclusive:
        dates.append(
            int(dt.datetime(year, month, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
        )
        month += 3
        if month > 12:
            month -= 12
            year += 1
    return dates


def ewma_series(rates: list[float], half_life: int) -> list[float]:
    alpha = 1.0 - 0.5 ** (1.0 / half_life)
    out: list[float] = []
    running = 0.0
    for index, rate in enumerate(rates):
        running = rate if index == 0 else alpha * rate + (1.0 - alpha) * running
        out.append(running)
    return out


def build_features(
    trades: pl.DataFrame,
    ohlc: tc.OhlcSeries,
    series: common.FundingSeries,
    klines: pl.DataFrame,
    *,
    ledger_mode: bool,
) -> pl.DataFrame:
    """Frozen feature panel; ledger_mode enforces exact entry-bar alignment."""
    ewma_by_symbol = {
        symbol: (ts_list, ewma_series(rate_list, EWMA_HALF_LIFE))
        for symbol, (ts_list, rate_list) in series.items()
    }
    first_bar = {
        str(row[0]): int(row[1])
        for row in klines.group_by("symbol").agg(pl.col("bar_end_ts_ms").min()).iter_rows()
    }
    rows: list[dict[str, Any]] = []
    for trade in trades.iter_rows(named=True):
        symbol = str(trade["symbol"])
        entry_ts = int(trade["entry_ts_ms"])
        ends, _highs, _lows, closes = ohlc.get(symbol, ([], [], [], []))
        index = bisect.bisect_right(ends, entry_ts) - 1
        if ledger_mode and (index < 0 or ends[index] != entry_ts):
            raise RuntimeError(f"{trade['trade_id']}: entry bar absent from kline slice")
        r_1h = mom_delta = None
        hours_high = None
        if index >= 2:
            r_now = closes[index] / closes[index - 1] - 1.0
            r_prev = closes[index - 1] / closes[index - 2] - 1.0
            r_1h, mom_delta = r_now, r_now - r_prev
        hours_high, _status = v4_shared.hours_since_high_tolerant(entry_ts, ends, closes)
        ts_list, rate_list = series.get(symbol, ([], []))
        lo = bisect.bisect_right(ts_list, entry_ts)
        hi = bisect.bisect_right(ts_list, entry_ts + PLANNED_HOLD_MS)
        known_prev = rate_list[lo - 1] if lo > 0 else None
        ewma = ewma_by_symbol.get(symbol, ([], []))[1]
        funding_ewma = ewma[lo - 1] if lo > 0 else None
        weight = float(trade["notional_weight"])
        rows.append(
            {
                "hours_since_high_168h": hours_high,
                "r_1h": r_1h,
                "mom_delta": mom_delta,
                "known_rate_prev": known_prev,
                "funding_ewma_hl3": funding_ewma,
                "n_intervals_planned_hold": hi - lo,
                "cost_per_unit": -float(trade["cost_return"]) / weight if weight else None,
                "age_days_censored": (
                    (entry_ts - first_bar[symbol]) / MS_PER_DAY if symbol in first_bar else None
                ),
                "net_per_unit": float(trade["net_return"]) / weight if weight else None,
            }
        )
    features = pl.from_dicts(rows, infer_schema_length=None)
    if features.height != trades.height:
        raise RuntimeError("feature rows diverge from trade rows")
    # Positional hstack: render-book trade_ids repeat across component books,
    # so a trade_id join would multiply rows.
    return trades.hstack(features)


def winsorize_fit(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lo = np.nanquantile(x, WINSOR_QUANTILES[0], axis=0)
    hi = np.nanquantile(x, WINSOR_QUANTILES[1], axis=0)
    return lo, hi


def design_matrix(
    frame: pl.DataFrame, lo: np.ndarray, hi: np.ndarray, mean: np.ndarray | None, std: np.ndarray | None
) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
    x = np.column_stack([frame[f].to_numpy().astype(float) for f in FEATURES])
    x = np.clip(x, lo, hi)
    if mean is None:
        mean = np.nanmean(x, axis=0)
        std = np.nanstd(x, axis=0)
        std = np.where(std > 0, std, 1.0)
    x = (x - mean) / std
    x = np.nan_to_num(x, nan=0.0)
    return np.column_stack([x, np.ones(len(x))]), mean, std


def fit_ridge(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    n_col = x.shape[1]
    penalty = RIDGE_LAMBDA * np.eye(n_col)
    penalty[-1, -1] = 0.0
    return np.linalg.solve(x.T @ x + penalty, x.T @ y)


def fit_logistic(x: np.ndarray, y: np.ndarray, iterations: int = 30) -> np.ndarray:
    beta = np.zeros(x.shape[1])
    penalty = LOGISTIC_L2 * np.eye(x.shape[1])
    penalty[-1, -1] = 0.0
    for _ in range(iterations):
        p = 1.0 / (1.0 + np.exp(-(x @ beta)))
        w = np.clip(p * (1.0 - p), 1e-6, None)
        gradient = x.T @ (y - p) - penalty @ beta
        hessian = (x.T * w) @ x + penalty
        step = np.linalg.solve(hessian, gradient)
        beta = beta + step
        if float(np.max(np.abs(step))) < 1e-10:
            break
    return beta


def rank_transform(y: np.ndarray) -> np.ndarray:
    order = np.argsort(np.argsort(y))
    return order / max(1, len(y) - 1) - 0.5


def walk_forward(panel: pl.DataFrame, refits: list[int]) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Score every trade with the last refit at or before its entry."""
    scores = np.full(panel.height, np.nan)
    logit_scores = np.full(panel.height, np.nan)
    deciles = np.zeros(panel.height, dtype=int)
    quintiles = np.zeros(panel.height, dtype=int)
    entry = panel["entry_ts_ms"].to_numpy()
    exit_ts = panel["exit_ts_ms"].to_numpy()
    coef_rows: list[dict[str, Any]] = []
    boundaries = refits + [int(entry.max()) + 1]
    for k, refit_ms in enumerate(refits):
        window_mask = (entry >= refit_ms) & (entry < boundaries[k + 1])
        if not window_mask.any():
            continue
        train_mask = exit_ts < refit_ms
        train = panel.filter(pl.Series(train_mask))
        if train.height < 500:
            continue
        lo, hi = winsorize_fit(
            np.column_stack([train[f].to_numpy().astype(float) for f in FEATURES])
        )
        x_train, mean, std = design_matrix(train, lo, hi, None, None)
        y_net = train["net_per_unit"].to_numpy().astype(float)
        beta = fit_ridge(x_train, rank_transform(y_net))
        beta_logit = fit_logistic(x_train, (y_net > 0).astype(float))
        train_scores = x_train @ beta
        decile_cuts = np.quantile(train_scores, np.linspace(0.1, 0.9, 9))
        quintile_cuts = np.quantile(train_scores, np.linspace(0.2, 0.8, 4))
        x_score, _m, _s = design_matrix(panel.filter(pl.Series(window_mask)), lo, hi, mean, std)
        window_scores = x_score @ beta
        scores[window_mask] = window_scores
        logit_scores[window_mask] = x_score @ beta_logit
        deciles[window_mask] = 1 + np.searchsorted(decile_cuts, window_scores)
        quintiles[window_mask] = 1 + np.searchsorted(quintile_cuts, window_scores)
        coef_rows.append(
            {
                "refit": common.iso_date(refit_ms),
                "train_rows": train.height,
                "scored_rows": int(window_mask.sum()),
                **{f"ridge_{name}": float(beta[i]) for i, name in enumerate(FEATURES)},
                "ridge_intercept": float(beta[-1]),
                **{f"logit_{name}": float(beta_logit[i]) for i, name in enumerate(FEATURES)},
            }
        )
    out = panel.with_columns(
        pl.Series("wf_score", scores),
        pl.Series("wf_logit", logit_scores),
        pl.Series("wf_decile", deciles),
        pl.Series("wf_quintile", quintiles),
    )
    return out, coef_rows


def action_weight_expr(action: str) -> pl.Expr:
    decile = pl.col("wf_decile")
    quintile = pl.col("wf_quintile")
    scored = pl.col("wf_score").is_not_nan() & pl.col("wf_score").is_not_null()
    if action == "baseline":
        return pl.lit(1.0)
    if action == "drop_bottom_decile":
        return pl.when(scored & (decile == 1)).then(0.0).otherwise(1.0)
    if action == "quintile_sizing":
        expr: pl.Expr = pl.lit(1.0)
        for q, w in QUINTILE_SIZING.items():
            expr = pl.when(scored & (quintile == q)).then(w).otherwise(expr)
        return expr
    if action == COMPARATOR_TE:
        h = pl.col("hours_since_high_168h")
        return pl.when(h.is_null()).then(1.0).when(h <= 1.0).then(1.0).otherwise(0.0)
    if action == COMPARATOR_TG:
        rate = pl.col("known_rate_prev")
        forecast = pl.col("tg_forecast")
        threshold = TG_CUT * pl.col("n_intervals_planned_hold")
        skip = (
            rate.is_not_null() & (rate < TG_CUT) & forecast.is_not_null() & (forecast < threshold)
        )
        return pl.when(skip).then(0.0).otherwise(1.0)
    if action == "te_and_tg":
        te = action_weight_expr(COMPARATOR_TE)
        tg = action_weight_expr(COMPARATOR_TG)
        return te * tg
    raise ValueError(f"unknown action {action}")


def decile_table(panel: pl.DataFrame, midpoint: int) -> pl.DataFrame:
    scored = panel.filter(pl.col("wf_score").is_not_null() & pl.col("wf_score").is_not_nan())
    frames = []
    for era in ("full", "early", "late"):
        part = scored
        if era == "early":
            part = scored.filter(pl.col("entry_ts_ms") < midpoint)
        elif era == "late":
            part = scored.filter(pl.col("entry_ts_ms") >= midpoint)
        frames.append(
            part.group_by("wf_decile")
            .agg(
                pl.len().alias("trades"),
                (10_000.0 * pl.col("net_per_unit").mean()).alias("mean_net_per_unit_bps"),
                (10_000.0 * pl.col("net_return").mean()).alias("mean_net_bps"),
                (pl.col("net_return") > 0).mean().alias("win_rate"),
            )
            .with_columns(pl.lit(era).alias("era"))
        )
    return pl.concat(frames, how="vertical").sort(["era", "wf_decile"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = common.REPORT_ROOT / "t-h" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger = common.load_ledger("continuous")
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)
    ohlc = tc.ohlc_series_by_symbol(klines)
    common.crosscheck_ledger_funding(ledger, series)
    midpoint = common.era_midpoint_ts_ms(ledger)
    end_day = common.utc_day_ms(int(ledger["exit_ts_ms"].max()))

    panel = build_features(ledger, ohlc, series, klines, ledger_mode=True)
    # T-G combo comparator needs the meanrev forecast; recompute with T-G's frozen definition.
    from scripts.research_v3.tg_funding_state import build_panel as tg_build_panel

    tg_panel = tg_build_panel(ledger, series).select(
        "trade_id", pl.col("meanrev_forecast_24h").alias("tg_forecast")
    )
    panel = panel.join(tg_panel, on="trade_id", how="left")

    refits = quarterly_refits(FIRST_REFIT, dt.date(2024, 12, 1))
    scored_panel, coef_rows = walk_forward(panel, refits)
    scored_mask = scored_panel["wf_score"].is_not_null() & scored_panel["wf_score"].is_not_nan()
    print(
        f"scored {int(scored_mask.sum())}/{panel.height} trades over {len(coef_rows)} refits",
        flush=True,
    )

    panel_path = out_dir / "th_trade_panel.parquet"
    scored_panel.write_parquet(panel_path)
    coef_frame = pl.from_dicts(coef_rows, infer_schema_length=None)
    coef_path = out_dir / "th_coefficients.csv"
    coef_frame.write_csv(coef_path)

    deciles = decile_table(scored_panel, midpoint)
    decile_path = out_dir / "th_decile_diagnostic.csv"
    deciles.write_csv(decile_path)

    # All actions and comparators are evaluated on the scored sample only, so
    # every cell acts on the same trades.
    scored = scored_panel.filter(scored_mask)
    actions = [
        "baseline", "drop_bottom_decile", "quintile_sizing",
        COMPARATOR_TE, COMPARATOR_TG, "te_and_tg",
    ]
    scored_start_day = common.utc_day_ms(int(scored["entry_ts_ms"].min()))
    grid_rows: list[dict[str, Any]] = []
    for action in actions:
        cell_panel = scored.with_columns(action_weight_expr(action).alias("weight_factor"))
        for row in v4_shared.weighted_cell_metrics(
            cell_panel, series, bars, midpoint_ts_ms=midpoint, start_day_ms=scored_start_day,
            end_day_ms=end_day,
        ):
            row["action"] = action
            grid_rows.append(row)
        print(f"action done: {action}", flush=True)

    grid = pl.from_dicts(grid_rows, infer_schema_length=None).select(
        "action", "era", "trades_kept", "trades_removed", "trades_downweighted",
        "net_return", "gross_return", "cost_return", "funding_return", "max_drawdown",
        "worst_day_return", "worst_day", "per_trade_net_bps", "tp_rate", "mean_mae",
        "share_mae_below_10pct", "removed_gross_forgone", "removed_funding_saved",
        "removed_cost_saved", "removed_net_delta",
    )
    grid_path = out_dir / "th_grid.csv"
    grid.write_csv(grid_path)

    # Render-book arm: score with the FINAL refit model (ranking-transfer diagnostic).
    render_klines, render_kline_sha = v4_shared.render_kline_cache(args.data_root)
    render_funding, render_funding_sha = v4_shared.render_funding_cache(args.data_root)
    render_series = common.funding_series_by_symbol(render_funding)
    render_ohlc = tc.ohlc_series_by_symbol(render_klines)
    final_refit = refits[-1]
    train_mask = panel["exit_ts_ms"].to_numpy() < final_refit
    train = panel.filter(pl.Series(train_mask))
    lo, hi = winsorize_fit(np.column_stack([train[f].to_numpy().astype(float) for f in FEATURES]))
    x_train, mean, std = design_matrix(train, lo, hi, None, None)
    beta = fit_ridge(x_train, rank_transform(train["net_per_unit"].to_numpy().astype(float)))
    train_scores = x_train @ beta
    decile_cuts = np.quantile(train_scores, np.linspace(0.1, 0.9, 9))
    render_frames = []
    for arm in ("gate_on", "gate_off"):
        book = v4_shared.load_render_book(arm)
        featured = build_features(book, render_ohlc, render_series, render_klines, ledger_mode=False)
        x_book, _m, _s = design_matrix(featured, lo, hi, mean, std)
        book_scores = x_book @ beta
        featured = featured.with_columns(
            pl.Series("wf_score", book_scores),
            pl.Series("wf_decile", 1 + np.searchsorted(decile_cuts, book_scores)),
            pl.Series("wf_quintile", np.zeros(len(book_scores), dtype=int)),
        )
        table = decile_table(featured, v4_shared.render_era_midpoint_ms(book)).with_columns(
            pl.lit(arm).alias("arm")
        )
        render_frames.append(table)
        print(f"render arm {arm} scored", flush=True)
    render_table = pl.concat(render_frames, how="vertical")
    render_path = out_dir / "th_render_deciles.csv"
    render_table.write_csv(render_path)

    common.write_manifest(
        out_dir,
        kind="strategy_research_v4_th_expected_net",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "shared_cache_dir": str(shared_dir),
            "render_caches": {
                "render_kline_slice_1h.parquet": render_kline_sha,
                "render_funding_events.parquet": render_funding_sha,
            },
            "ta_render_books": str(v4_shared.TA_DIR),
        },
        params={
            "sleeve": "continuous",
            "features": list(FEATURES),
            "ridge_lambda": RIDGE_LAMBDA,
            "logistic_l2": LOGISTIC_L2,
            "winsor_quantiles": list(WINSOR_QUANTILES),
            "target": "rank-transformed net_per_unit (ridge); net_per_unit > 0 (logistic)",
            "walk_forward": {
                "first_refit": FIRST_REFIT.isoformat(),
                "cadence": "quarterly, expanding window",
                "train_rule": "exit_ts_ms < refit boundary (strictly PIT)",
                "min_train_rows": 500,
                "refits": [common.iso_date(r) for r in refits],
            },
            "actions": {
                "drop_bottom_decile": "weight 0 for walk-forward decile 1 (train-window cutoffs)",
                "quintile_sizing": QUINTILE_SIZING,
            },
            "comparators": {
                "te": COMPARATOR_TE, "tg": COMPARATOR_TG,
                "rule": "best full-era net_return among the declared T-E / T-G cells",
                "sample": "scored trades only (entries at or after the first refit)",
            },
            "era_midpoint": common.iso_date(midpoint),
            "render_arm": "final-refit ridge model; ranking-transfer diagnostic, not a tradeable rule"
            " for entries before the final train boundary; age feature censored at the render cache"
            " origin (2023-03-26) unlike the ledger origin (2021-04-25)",
        },
        output_files={
            "th_grid.csv": grid_path,
            "th_decile_diagnostic.csv": decile_path,
            "th_coefficients.csv": coef_path,
            "th_render_deciles.csv": render_path,
            "th_trade_panel.parquet": panel_path,
        },
        extra={"explicit_non_conclusions": [
            "walk-forward development inside the spent window; not confirmatory evidence",
            "no capacity backfill; no alpha or promotion claim",
            "render-book scoring is a ranking-transfer diagnostic on already-rendered outputs",
        ]},
    )
    print(json.dumps({"actions": actions}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
