"""Walk-forward ridge-combiner scout — cheap-falsification diagnostics.

Pre-registration: docs/preregistration/ridge-combiner-2026-06-09.md

This scout answers the Tier-1 gating question BEFORE any engine change: does the
ridge combiner carry real OUT-OF-FOLD signal, and are its coefficients stable?
For each venue it:

  1. builds the frozen causal feature panel (signal_harness.build_feature_panel);
  2. optionally left-joins the precomputed residual_momentum signal;
  3. generates strictly-causal walk-forward ridge scores;
  4. writes an engine-consumable <venue>_ridge_score.parquet (symbol, ts_ms, score);
  5. reports pooled + per-fold out-of-fold rank-IC, per-fold lambda, and
     coefficient sign-consistency against the pre-registered >=80% bar.

It does NOT modify the backtest sizing path — that engine wiring is the next,
separately test-gated milestone, gated on this scout showing positive IC. Running
this is cheap and falsifies a dead combiner without touching the engine.

Run on the operator data box (full-PIT roots ~23GB):

    POLARS_MAX_THREADS=8 .venv/bin/python scripts/ridge_combiner_scout.py \
      --data-roots "bybit=$HOME/SHARED_DATA/bybit_full_pit,binance=$HOME/SHARED_DATA/binance_full_pit" \
      --start 2023-04-01 --end 2026-05-28 \
      --out "$HOME/SHARED_DATA/ridge_combiner_2026-06-09"
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.ridge_combiner import (
    FROZEN_FEATURES,
    RidgeCombinerConfig,
    coefficient_sign_consistency,
    rank_ic,
    walk_forward_scores,
)
from liquidity_migration.signal_harness import build_feature_panel
from liquidity_migration.walk_forward import WalkForwardConfig, make_walk_forward_folds

SIGN_CONSISTENCY_BAR = 0.80  # pre-registered dominant-feature stability threshold


def _parse_roots(spec: str) -> dict[str, Path]:
    """Parse 'bybit=/p/a,binance=/p/b' into {venue: Path}."""
    out: dict[str, Path] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"--data-roots entry {chunk!r} must be 'venue=path'")
        venue, path = chunk.split("=", 1)
        out[venue.strip()] = Path(path.strip())
    if not out:
        raise ValueError("--data-roots produced no venues")
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def per_fold_ic(
    scores: pl.DataFrame, panel: pl.DataFrame, cfg: RidgeCombinerConfig
) -> list[dict[str, Any]]:
    """Out-of-fold rank-IC of ridge_score vs realized target, per test window."""
    if scores.is_empty():
        return []
    joined = scores.join(
        panel.select(["symbol", "ts_ms", cfg.target_col]), on=["symbol", "ts_ms"], how="inner"
    ).drop_nulls(subset=[cfg.target_col])
    folds = make_walk_forward_folds(panel["ts_ms"].to_list(), cfg.walk_forward)
    out: list[dict[str, Any]] = []
    for fold in folds:
        window = joined.filter(
            (pl.col("ts_ms") >= fold.test_start_ms) & (pl.col("ts_ms") < fold.test_end_ms)
        )
        if window.height < 3:
            continue
        out.append(
            {
                "fold_index": fold.index,
                "n": window.height,
                "rank_ic": rank_ic(
                    window["ridge_score"].to_numpy(), window[cfg.target_col].to_numpy()
                ),
            }
        )
    return out


def pooled_ic(scores: pl.DataFrame, panel: pl.DataFrame, cfg: RidgeCombinerConfig) -> float:
    if scores.is_empty():
        return 0.0
    joined = scores.join(
        panel.select(["symbol", "ts_ms", cfg.target_col]), on=["symbol", "ts_ms"], how="inner"
    ).drop_nulls(subset=[cfg.target_col])
    if joined.height < 3:
        return 0.0
    return rank_ic(joined["ridge_score"].to_numpy(), joined[cfg.target_col].to_numpy())


def _config_hash(cfg: RidgeCombinerConfig) -> str:
    payload = {
        "features": list(cfg.features),
        "target_col": cfg.target_col,
        "lambda_grid": list(cfg.lambda_grid),
        "warmup_days": cfg.walk_forward.warmup_days,
        "step_days": cfg.walk_forward.step_days,
        "embargo_days": cfg.walk_forward.embargo_days,
        "min_train_rows": cfg.min_train_rows,
        "standardize": cfg.standardize,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-roots", required=True, help="'bybit=/path,binance=/path'")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--features", default=",".join(FROZEN_FEATURES))
    p.add_argument("--lambda-grid", default="0.1,1,10,100,1000")
    p.add_argument("--warmup-days", type=int, default=540)
    p.add_argument("--step-days", type=int, default=90)
    p.add_argument("--embargo-days", type=int, default=2)
    p.add_argument("--min-train-rows", type=int, default=200)
    p.add_argument("--universe-min-turnover", type=float, default=1_000_000.0)
    p.add_argument(
        "--join-residual-momentum",
        action="store_true",
        help="Left-join <root>/residual_momentum.parquet as a 9th feature when present.",
    )
    return p.parse_args()


def build_config(args: argparse.Namespace, features: tuple[str, ...]) -> RidgeCombinerConfig:
    return RidgeCombinerConfig(
        features=features,
        lambda_grid=tuple(float(x) for x in args.lambda_grid.split(",")),
        walk_forward=WalkForwardConfig(
            warmup_days=args.warmup_days,
            step_days=args.step_days,
            embargo_days=args.embargo_days,
        ),
        min_train_rows=args.min_train_rows,
    )


def main() -> int:
    args = parse_args()
    roots = _parse_roots(args.data_roots)
    feature_names = tuple(s.strip() for s in args.features.split(",") if s.strip())
    args.out.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    receipt: dict[str, Any] = {"venues": {}, "sign_consistency_bar": SIGN_CONSISTENCY_BAR}

    for venue, root in roots.items():
        print(f"[{venue}] building feature panel from {root} ...")
        panel = build_feature_panel(
            root,
            start=args.start,
            end=args.end,
            feature_specs=",".join(feature_names),
            universe_min_daily_turnover=args.universe_min_turnover,
        )
        features = feature_names
        if args.join_residual_momentum:
            rmom_path = root / "residual_momentum.parquet"
            if rmom_path.exists():
                rmom = pl.read_parquet(rmom_path).select(["symbol", "ts_ms", "residual_momentum"])
                panel = panel.join(rmom, on=["symbol", "ts_ms"], how="left")
                features = (*feature_names, "residual_momentum")
                print(f"[{venue}] joined residual_momentum ({rmom.height} rows)")
            else:
                print(f"[{venue}] residual_momentum.parquet absent — skipping 9th feature")

        cfg = build_config(args, features)
        if panel.is_empty():
            print(f"[{venue}] EMPTY panel — skipping")
            summary_rows.append({"venue": venue, "status": "empty_panel"})
            continue

        scores, coefs = walk_forward_scores(panel, cfg)
        score_path = args.out / f"{venue}_ridge_score.parquet"
        scores.write_parquet(score_path)

        pooled = pooled_ic(scores, panel, cfg)
        folds_ic = per_fold_ic(scores, panel, cfg)
        consistency = coefficient_sign_consistency(coefs, features)
        dominant_stable = {f: v for f, v in consistency.items() if v >= SIGN_CONSISTENCY_BAR}

        summary_rows.append(
            {
                "venue": venue,
                "status": "ok",
                "n_scored": scores.height,
                "n_folds": len(coefs),
                "pooled_rank_ic": round(pooled, 5),
                "mean_fold_rank_ic": round(
                    float(np.mean([f["rank_ic"] for f in folds_ic])) if folds_ic else 0.0, 5
                ),
                "n_features_sign_stable": len(dominant_stable),
                "n_features": len(features),
                "config_hash": _config_hash(cfg),
                "score_parquet": str(score_path),
            }
        )
        for fc in coefs:
            row = {
                "venue": venue,
                "fold_index": fc.fold_index,
                "chosen_lambda": fc.chosen_lambda,
                "n_train": fc.n_train,
                "n_test": fc.n_test,
                "intercept": round(fc.intercept, 6),
            }
            row.update({f"coef_{k}": round(v, 6) for k, v in fc.coef.items()})
            fold_rows.append(row)

        receipt["venues"][venue] = {
            "pooled_rank_ic": pooled,
            "per_fold_ic": folds_ic,
            "sign_consistency": consistency,
            "dominant_stable_features": sorted(dominant_stable),
            "score_parquet": str(score_path),
            "n_scored": scores.height,
        }
        print(
            f"[{venue}] pooled OOF rank-IC={pooled:+.4f}  folds={len(coefs)}  "
            f"sign-stable features={sorted(dominant_stable)}"
        )

    _write_csv(args.out / "summary.csv", summary_rows)
    _write_csv(args.out / "fold_coefficients.csv", fold_rows)
    (args.out / "run_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(f"\nWrote {args.out}/summary.csv, fold_coefficients.csv, run_receipt.json")
    print(
        "Tier-1 read: positive pooled OOF rank-IC on BOTH venues + dominant features "
        "sign-stable => proceed to engine-sizing wiring. Otherwise reject cheaply."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
