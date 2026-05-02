from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl

from .config import CostConfig, DEFAULT_HORIZONS_H, PortfolioConfig, SignalConfig
from .features import _demean_by_timestamp
from .portfolio import attach_latest_funding, run_detailed_cost_scenario
from .research import attach_forward_returns, compute_signal_metrics
from .storage import read_dataset, write_dataset


CANDIDATE_SCORES = {
    "original_composite": "composite_score",
    "inverse_composite": "score_inverse_composite",
    "aggression_only": "aggression_confirmed",
    "carry_only": "carry_z_adjusted",
    "inverse_momentum": "score_inverse_momentum",
    "inverse_relative_volume": "score_inverse_relative_volume",
    "inverse_oi_impulse": "score_inverse_oi_impulse",
    "carry_plus_aggression": "score_carry_plus_aggression",
    "carry_plus_inverse_momentum": "score_carry_plus_inverse_momentum",
    "carry_plus_inverse_relative_volume": "score_carry_plus_inverse_relative_volume",
    "carry_inverse_momentum_relvol": "score_carry_inverse_momentum_relvol",
    "carry_inverse_bad_stack": "score_carry_inverse_bad_stack",
}


def run_research_sweep(
    data_root: str | Path,
    *,
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    portfolio_config: PortfolioConfig | None = None,
    signal_config: SignalConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    features = read_dataset(data_root, "features_1h")
    klines = read_dataset(data_root, "klines_1h")
    funding = read_dataset(data_root, "funding")
    if features.is_empty():
        raise RuntimeError("features_1h is empty; run build-features first")

    cost_config = cost_config or CostConfig()
    portfolio_config = portfolio_config or PortfolioConfig()
    signal_config = signal_config or SignalConfig()
    enriched = attach_forward_returns(features, klines, horizons_h=horizons_h)
    enriched = attach_latest_funding(enriched, funding)
    candidates = build_sweep_candidates(enriched)
    metrics = compute_signal_metrics(
        candidates,
        horizons_h=horizons_h,
        signal_columns=CANDIDATE_SCORES,
        cost_bps=cost_config.base_entry_exit_cost_bps,
    )
    portfolio_rows = []
    for candidate, score_col in CANDIDATE_SCORES.items():
        candidate_df = _candidate_portfolio_frame(candidates, score_col)
        for scenario, multiplier, all_taker in (
            ("base", 1.0, False),
            ("2x_costs", 2.0, False),
            ("3x_costs", 3.0, False),
            ("all_taker", 1.0, True),
        ):
            run = run_detailed_cost_scenario(
                candidate_df,
                scenario=scenario,
                cost_multiplier=multiplier,
                portfolio_config=portfolio_config,
                signal_config=signal_config,
                cost_config=cost_config,
                all_taker=all_taker,
            )
            row = asdict(run.summary)
            row["candidate"] = candidate
            portfolio_rows.append(row)

    metric_rows = [asdict(item) for item in metrics]
    payload = {
        "rows": candidates.height,
        "horizons_h": list(horizons_h),
        "candidates": list(CANDIDATE_SCORES),
        "metrics": metric_rows,
        "portfolio": portfolio_rows,
        "best_by_horizon": _best_by_horizon(metric_rows),
        "best_portfolio": _best_portfolio(portfolio_rows),
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "research_sweep.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "research_sweep.md").write_text(format_research_sweep(payload), encoding="utf-8")
    write_dataset(pl.DataFrame(metric_rows), data_root, "research_sweep_metrics", partition_by=("signal",))
    if portfolio_rows:
        write_dataset(pl.DataFrame(portfolio_rows), data_root, "research_sweep_portfolio", partition_by=("candidate", "scenario"))
    return payload


def build_sweep_candidates(features: pl.DataFrame) -> pl.DataFrame:
    required = {
        "composite_score",
        "aggression_confirmed",
        "rel_volume_z",
        "momentum_z",
        "carry_z_adjusted",
        "oi_impulse_z",
    }
    missing = sorted(required - set(features.columns))
    if missing:
        raise RuntimeError(f"features_1h is missing sweep columns: {', '.join(missing)}")

    df = features.with_columns(
        [
            (-pl.col("composite_score")).alias("score_inverse_composite_raw"),
            (-pl.col("momentum_z")).alias("score_inverse_momentum_raw"),
            (-pl.col("rel_volume_z")).alias("score_inverse_relative_volume_raw"),
            (-pl.col("oi_impulse_z")).alias("score_inverse_oi_impulse_raw"),
            (0.60 * pl.col("carry_z_adjusted") + 0.40 * pl.col("aggression_confirmed")).alias("score_carry_plus_aggression_raw"),
            (0.65 * pl.col("carry_z_adjusted") - 0.35 * pl.col("momentum_z")).alias("score_carry_plus_inverse_momentum_raw"),
            (0.65 * pl.col("carry_z_adjusted") - 0.35 * pl.col("rel_volume_z")).alias("score_carry_plus_inverse_relative_volume_raw"),
            (0.50 * pl.col("carry_z_adjusted") - 0.25 * pl.col("momentum_z") - 0.25 * pl.col("rel_volume_z")).alias("score_carry_inverse_momentum_relvol_raw"),
            (
                0.45 * pl.col("carry_z_adjusted")
                + 0.20 * pl.col("aggression_confirmed")
                - 0.15 * pl.col("momentum_z")
                - 0.15 * pl.col("rel_volume_z")
                - 0.05 * pl.col("oi_impulse_z")
            ).alias("score_carry_inverse_bad_stack_raw"),
        ]
    )
    for col in [value for value in df.columns if value.startswith("score_") and value.endswith("_raw")]:
        df = _demean_by_timestamp(df, col, col.removesuffix("_raw"))
    return df


def format_research_sweep(payload: dict[str, Any]) -> str:
    lines = [
        "# Aggression-Carry Research Sweep",
        "",
        f"Rows: {payload['rows']}",
        f"Candidates: {', '.join(payload['candidates'])}",
        "",
        "## Best IC/Spread By Horizon",
        "",
        "| Horizon | Best IC | Mean IC | T-stat | Best cost-adj spread | Cost-adj spread |",
        "|---:|---|---:|---:|---|---:|",
    ]
    for item in payload["best_by_horizon"]:
        lines.append(
            f"| {item['horizon_h']}h | {item['best_ic_signal']} | {item['best_ic']:.4f} | "
            f"{item['best_ic_tstat']:.2f} | {item['best_cost_signal']} | {item['best_cost_adjusted_spread']:.6f} |"
        )
    lines.extend(
        [
            "",
            "## Portfolio Sweep",
            "",
            "| Candidate | Scenario | Total return | Sharpe-like | Max DD | Long | Short | Funding | Fees | Slippage |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in payload["portfolio"]:
        lines.append(
            f"| {item['candidate']} | {item['scenario']} | {item['total_return']:.4f} | "
            f"{item['sharpe_like']:.2f} | {item['max_drawdown']:.4f} | {item['long_pnl']:.4f} | "
            f"{item['short_pnl']:.4f} | {item['funding_pnl']:.4f} | {item['fee_pnl']:.4f} | "
            f"{item['slippage_pnl']:.4f} |"
        )
    best = payload["best_portfolio"]
    lines.extend(
        [
            "",
            "## Best Base Portfolio",
            "",
            f"Candidate: `{best.get('candidate')}`",
            f"Total return: {best.get('total_return', 0.0):.4f}",
            f"Sharpe-like: {best.get('sharpe_like', 0.0):.2f}",
            f"Max drawdown: {best.get('max_drawdown', 0.0):.4f}",
            "",
        ]
    )
    return "\n".join(lines)


def _candidate_portfolio_frame(df: pl.DataFrame, score_col: str) -> pl.DataFrame:
    columns = [col for col in df.columns if col != "composite_score"]
    return df.select(columns + [pl.col(score_col).alias("composite_score")])


def _best_by_horizon(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    horizons = sorted({int(row["horizon_h"]) for row in metrics})
    for horizon in horizons:
        rows = [row for row in metrics if int(row["horizon_h"]) == horizon]
        best_ic = max(rows, key=lambda row: _finite_or_low(row["mean_ic"]))
        best_cost = max(rows, key=lambda row: _finite_or_low(row["mean_cost_adjusted_spread"]))
        output.append(
            {
                "horizon_h": horizon,
                "best_ic_signal": best_ic["signal"],
                "best_ic": float(best_ic["mean_ic"]),
                "best_ic_tstat": float(best_ic["ic_tstat"]),
                "best_cost_signal": best_cost["signal"],
                "best_cost_adjusted_spread": float(best_cost["mean_cost_adjusted_spread"]),
            }
        )
    return output


def _best_portfolio(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base_rows = [row for row in rows if row["scenario"] == "base"]
    if not base_rows:
        return {}
    return max(base_rows, key=lambda row: _finite_or_low(row["total_return"]))


def _finite_or_low(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    return number if number == number else float("-inf")
