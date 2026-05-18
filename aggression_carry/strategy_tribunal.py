from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

MS_PER_HOUR = 60 * 60 * 1000


@dataclass(frozen=True, slots=True)
class StrategyTribunalConfig:
    bootstrap_samples: int = 500
    bootstrap_block_size: int = 20
    random_seed: int = 17
    min_robust_family_variants: int = 3
    robust_return_fraction: float = 0.50
    robust_drawdown_multiple: float = 1.50
    symbol_concentration_watch: float = 0.35
    symbol_concentration_fail: float = 0.50
    clustered_loss_min_trades: int = 3


def run_strategy_tribunal(
    report_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
    comparison_csvs: tuple[str | Path, ...] = (),
    config: StrategyTribunalConfig | None = None,
) -> dict[str, Any]:
    tribunal_config = config or StrategyTribunalConfig()
    source_dir = Path(report_dir).expanduser()
    target_dir = Path(output_dir).expanduser() if output_dir else source_dir / "strategy_tribunal"
    target_dir.mkdir(parents=True, exist_ok=True)

    artifacts = _read_artifacts(source_dir, comparison_csvs=comparison_csvs)
    summary = artifacts["summary"]
    sensitivity_source = artifacts["comparison_summary"] if not artifacts["comparison_summary"].is_empty() else summary
    trades = artifacts["trades"]
    baskets = artifacts["baskets"]
    best = _best_summary_row(summary, artifacts["research_report"])
    returns = _basket_returns(baskets)
    actual_metrics = _return_path_metrics(returns)
    bootstrap = _block_bootstrap(returns, config=tribunal_config)
    random_sign = _random_sign_control(returns, config=tribunal_config)
    inverted = _inverted_edge_control(baskets)
    sensitivity = _sensitivity_report(sensitivity_source, best=best, config=tribunal_config)
    concentration = _concentration_report(trades)
    clustering = _cluster_report(trades, baskets)
    findings = _findings(
        artifact_checks=artifacts["checks"],
        best=best,
        actual_metrics=actual_metrics,
        bootstrap=bootstrap,
        random_sign=random_sign,
        inverted=inverted,
        sensitivity=sensitivity,
        concentration=concentration,
        clustering=clustering,
        config=tribunal_config,
    )
    verdict = _verdict(findings)
    payload = {
        "verdict": verdict,
        "source_report_dir": str(source_dir),
        "config": asdict(tribunal_config),
        "artifact_checks": artifacts["checks"],
        "comparison_csvs": [str(Path(path).expanduser()) for path in comparison_csvs],
        "best_scenario": best,
        "actual_path": actual_metrics,
        "bootstrap": bootstrap,
        "negative_controls": {
            "random_sign": random_sign,
            "inverted_edge": inverted,
        },
        "sensitivity": sensitivity,
        "concentration": concentration,
        "clustering": clustering,
        "findings": findings,
        "output_files": {
            "json": str(target_dir / "strategy_tribunal_report.json"),
            "markdown": str(target_dir / "strategy_tribunal_report.md"),
        },
    }
    (target_dir / "strategy_tribunal_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (target_dir / "strategy_tribunal_report.md").write_text(format_strategy_tribunal_report(payload), encoding="utf-8")
    return payload


def format_strategy_tribunal_report(payload: dict[str, Any]) -> str:
    best = payload.get("best_scenario", {})
    actual = payload.get("actual_path", {})
    bootstrap = payload.get("bootstrap", {})
    controls = payload.get("negative_controls", {})
    sensitivity = payload.get("sensitivity", {})
    concentration = payload.get("concentration", {})
    clustering = payload.get("clustering", {})
    lines = [
        "# Strategy Tribunal",
        "",
        "Adversarial research audit for a completed `volume-events` report. This does not prove future PnL; it looks for ways the backtest could be fragile.",
        "",
        "## Verdict",
        "",
        f"- Verdict: **{payload.get('verdict', 'UNKNOWN')}**",
        f"- Source: `{payload.get('source_report_dir', '')}`",
        f"- Comparison CSVs: {len(payload.get('comparison_csvs', []))}",
        f"- Scenario: `{best.get('scenario_id', best.get('strategy', 'unknown'))}`",
        f"- Promotion gate: `{best.get('promotion_gate_pass', 'unknown')}` ({best.get('promotion_reason', 'unknown')})",
        f"- Total return: {_pct(best.get('total_return', actual.get('total_return', 0.0)))}",
        f"- Max drawdown: {_pct(best.get('max_drawdown', actual.get('max_drawdown', 0.0)))}",
        f"- Trades: {best.get('trades', concentration.get('trades', 0))}",
        "",
        "## Findings",
        "",
        "| Level | Check | Finding |",
        "|---|---|---|",
    ]
    for finding in payload.get("findings", []):
        lines.append(f"| {finding.get('level', '')} | {finding.get('check', '')} | {finding.get('message', '')} |")
    lines.extend(
        [
            "",
            "## Negative Controls",
            "",
            "| Control | Metric | Value |",
            "|---|---|---:|",
            f"| Block bootstrap | p05 total return | {_pct(bootstrap.get('p05_total_return'))} |",
            f"| Block bootstrap | positive sample rate | {_pct(bootstrap.get('positive_rate'))} |",
            f"| Random sign | p95 total return | {_pct(controls.get('random_sign', {}).get('p95_total_return'))} |",
            f"| Random sign | exceed actual rate | {_pct(controls.get('random_sign', {}).get('exceed_actual_rate'))} |",
            f"| Inverted edge | total return | {_pct(controls.get('inverted_edge', {}).get('total_return'))} |",
            f"| Inverted edge | max drawdown | {_pct(controls.get('inverted_edge', {}).get('max_drawdown'))} |",
            "",
            "## Sensitivity",
            "",
            f"- Status: `{sensitivity.get('status', 'unknown')}`",
            f"- Same-family variants: {sensitivity.get('same_family_variants', 0)}",
            f"- Robust same-family variants: {sensitivity.get('robust_family_variants', 0)}",
            f"- Robust return cutoff: {_pct(sensitivity.get('robust_return_cutoff'))}",
            f"- Robust max-drawdown cutoff: {_pct(sensitivity.get('robust_drawdown_cutoff'))}",
            "",
        ]
    )
    if sensitivity.get("parameter_groups"):
        lines.extend(["| Parameter | Levels | Best Level Return | Worst Level Return |", "|---|---:|---:|---:|"])
        for group in sensitivity["parameter_groups"]:
            lines.append(
                f"| `{group['parameter']}` | {group['levels']} | {_pct(group['best_level_total_return'])} | {_pct(group['worst_level_total_return'])} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Concentration And Crowding",
            "",
            "| Check | Value |",
            "|---|---:|",
            f"| Top symbol absolute contribution share | {_pct(concentration.get('top_symbol_abs_share'))} |",
            f"| Top symbol | `{concentration.get('top_symbol', '')}` |",
            f"| Top 5 symbol absolute contribution share | {_pct(concentration.get('top5_symbol_abs_share'))} |",
            f"| Worst entry-hour net return | {_pct(clustering.get('worst_entry_hour_net_return'))} |",
            f"| Worst entry-hour trades | {clustering.get('worst_entry_hour_trades', 0)} |",
            f"| Worst exit-date basket return | {_pct(clustering.get('worst_exit_date_return'))} |",
            "",
            "## Artifact Checks",
            "",
            "| Artifact | Present | Rows |",
            "|---|---:|---:|",
        ]
    )
    for check in payload.get("artifact_checks", []):
        lines.append(f"| `{check['name']}` | {check['present']} | {check['rows']} |")
    lines.append("")
    return "\n".join(lines)


def _read_artifacts(report_dir: Path, *, comparison_csvs: tuple[str | Path, ...]) -> dict[str, Any]:
    summary = _read_csv(report_dir / "volume_event_scenario_summary.csv")
    comparison_summary = _read_comparison_csvs(comparison_csvs)
    trades = _read_csv(report_dir / "volume_event_best_trades.csv")
    baskets = _read_csv(report_dir / "volume_event_best_baskets.csv")
    equity = _read_csv(report_dir / "volume_event_best_equity.csv")
    monthly = _read_csv(report_dir / "volume_event_best_monthly.csv")
    research_report = _read_json(report_dir / "volume_event_research_report.json")
    checks = [
        _artifact_check("volume_event_scenario_summary.csv", summary),
        _artifact_check("volume_event_best_trades.csv", trades),
        _artifact_check("volume_event_best_baskets.csv", baskets),
        _artifact_check("volume_event_best_equity.csv", equity),
        _artifact_check("volume_event_research_report.json", research_report),
    ]
    for path in comparison_csvs:
        expanded = Path(path).expanduser()
        checks.append(_artifact_check(f"comparison:{expanded.name}", _read_csv(expanded)))
    return {
        "summary": summary,
        "comparison_summary": comparison_summary,
        "trades": trades,
        "baskets": baskets,
        "equity": equity,
        "monthly": monthly,
        "research_report": research_report,
        "checks": checks,
    }


def _read_comparison_csvs(paths: tuple[str | Path, ...]) -> pl.DataFrame:
    frames = [_read_csv(Path(path).expanduser()) for path in paths]
    frames = [frame for frame in frames if not frame.is_empty()]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _read_csv(path: Path) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame()
    return pl.read_csv(path, infer_schema_length=1000)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_check(name: str, artifact: pl.DataFrame | dict[str, Any]) -> dict[str, Any]:
    if isinstance(artifact, pl.DataFrame):
        return {"name": name, "present": not artifact.is_empty(), "rows": artifact.height}
    return {"name": name, "present": bool(artifact), "rows": 1 if artifact else 0}


def _best_summary_row(summary: pl.DataFrame, report: dict[str, Any]) -> dict[str, Any]:
    if report.get("best_scenario"):
        return _json_ready(report["best_scenario"])
    if summary.is_empty():
        return {}
    return _json_ready(summary.head(1).to_dicts()[0])


def _basket_returns(baskets: pl.DataFrame) -> list[float]:
    if baskets.is_empty() or "basket_return" not in baskets.columns:
        return []
    return [float(value) for value in baskets["basket_return"].to_list() if value is not None and math.isfinite(float(value))]


def _return_path_metrics(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {"total_return": 0.0, "max_drawdown": 0.0, "observations": 0}
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    worst = min(returns)
    for value in returns:
        equity *= max(0.0, 1.0 + float(value))
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0 if peak > 0.0 else -1.0)
    arr = np.asarray(returns, dtype=float)
    stdev = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
    mean = float(np.mean(arr)) if arr.size else 0.0
    return {
        "total_return": float(equity - 1.0),
        "max_drawdown": float(max_dd),
        "mean_return": mean,
        "return_stdev": stdev,
        "sharpe_like": float(mean / stdev * math.sqrt(365.0)) if stdev > 1e-12 else 0.0,
        "worst_return": float(worst),
        "observations": len(returns),
    }


def _block_bootstrap(returns: list[float], *, config: StrategyTribunalConfig) -> dict[str, Any]:
    if not returns or config.bootstrap_samples <= 0:
        return {"samples": 0}
    rng = np.random.default_rng(config.random_seed)
    n = len(returns)
    block = max(1, min(config.bootstrap_block_size, n))
    max_start = max(1, n - block + 1)
    totals = []
    drawdowns = []
    for _ in range(config.bootstrap_samples):
        sample: list[float] = []
        while len(sample) < n:
            start = int(rng.integers(0, max_start))
            sample.extend(returns[start : start + block])
        metrics = _return_path_metrics(sample[:n])
        totals.append(float(metrics["total_return"]))
        drawdowns.append(float(metrics["max_drawdown"]))
    total_arr = np.asarray(totals, dtype=float)
    dd_arr = np.asarray(drawdowns, dtype=float)
    return {
        "samples": config.bootstrap_samples,
        "block_size": block,
        "p05_total_return": float(np.quantile(total_arr, 0.05)),
        "median_total_return": float(np.quantile(total_arr, 0.50)),
        "p95_total_return": float(np.quantile(total_arr, 0.95)),
        "positive_rate": float(np.mean(total_arr > 0.0)),
        "p05_max_drawdown": float(np.quantile(dd_arr, 0.05)),
        "median_max_drawdown": float(np.quantile(dd_arr, 0.50)),
    }


def _random_sign_control(returns: list[float], *, config: StrategyTribunalConfig) -> dict[str, Any]:
    if not returns or config.bootstrap_samples <= 0:
        return {"samples": 0}
    actual = _return_path_metrics(returns)["total_return"]
    rng = np.random.default_rng(config.random_seed + 1)
    magnitudes = np.abs(np.asarray(returns, dtype=float))
    totals = []
    for _ in range(config.bootstrap_samples):
        signs = rng.choice(np.asarray([-1.0, 1.0]), size=magnitudes.size)
        totals.append(float(_return_path_metrics((magnitudes * signs).tolist())["total_return"]))
    arr = np.asarray(totals, dtype=float)
    return {
        "samples": config.bootstrap_samples,
        "median_total_return": float(np.quantile(arr, 0.50)),
        "p95_total_return": float(np.quantile(arr, 0.95)),
        "exceed_actual_rate": float(np.mean(arr >= float(actual))),
    }


def _inverted_edge_control(baskets: pl.DataFrame) -> dict[str, Any]:
    if baskets.is_empty():
        return {"observations": 0}
    if {"gross_return", "cost_return"}.issubset(set(baskets.columns)):
        funding = baskets["funding_return"].to_list() if "funding_return" in baskets.columns else [0.0] * baskets.height
        returns = [
            -float(row["gross_return"]) + float(row["cost_return"]) - float(funding[index] or 0.0)
            for index, row in enumerate(baskets.select(["gross_return", "cost_return"]).to_dicts())
        ]
    else:
        returns = [-value for value in _basket_returns(baskets)]
    return _return_path_metrics(returns)


def _sensitivity_report(
    summary: pl.DataFrame,
    *,
    best: dict[str, Any],
    config: StrategyTribunalConfig,
) -> dict[str, Any]:
    if summary.is_empty():
        return {"status": "missing", "same_family_variants": 0, "robust_family_variants": 0, "parameter_groups": []}
    if summary.height < 2:
        return {"status": "insufficient_single_scenario", "same_family_variants": summary.height, "robust_family_variants": 0, "parameter_groups": []}
    family = summary
    for column in ("event_type", "side_hypothesis", "side", "strategy"):
        if column in family.columns and best.get(column) is not None:
            family = family.filter(pl.col(column) == best[column])
    best_return = _finite_float(best.get("total_return"))
    best_dd = _finite_float(best.get("max_drawdown"))
    return_cutoff = best_return * config.robust_return_fraction if best_return > 0.0 else best_return
    dd_cutoff = best_dd * config.robust_drawdown_multiple if best_dd < 0.0 else -1.0
    robust = family
    if "total_return" in robust.columns:
        robust = robust.filter(pl.col("total_return") >= return_cutoff)
    if "max_drawdown" in robust.columns:
        robust = robust.filter(pl.col("max_drawdown") >= dd_cutoff)
    if "promotion_gate_pass" in robust.columns:
        robust = robust.filter(pl.col("promotion_gate_pass") == True)  # noqa: E712
    parameter_groups = []
    for column in (
        "threshold",
        "hold_days",
        "stop_loss_pct",
        "take_profit_pct",
        "cost_multiplier",
        "entry_selector",
        "stop_fill_mode",
    ):
        if column not in summary.columns or summary[column].n_unique() <= 1:
            continue
        grouped = (
            summary.group_by(column)
            .agg(
                [
                    pl.len().alias("rows"),
                    pl.col("total_return").max().alias("best_total_return") if "total_return" in summary.columns else pl.lit(0.0).alias("best_total_return"),
                    pl.col("total_return").min().alias("worst_total_return") if "total_return" in summary.columns else pl.lit(0.0).alias("worst_total_return"),
                    pl.col("promotion_gate_pass").sum().alias("promotion_passes")
                    if "promotion_gate_pass" in summary.columns
                    else pl.lit(0).alias("promotion_passes"),
                ]
            )
            .sort("best_total_return", descending=True)
        )
        rows = grouped.to_dicts()
        parameter_groups.append(
            {
                "parameter": column,
                "levels": len(rows),
                "best_level": _json_ready(rows[0]) if rows else {},
                "best_level_total_return": _finite_float(rows[0].get("best_total_return")) if rows else 0.0,
                "worst_level_total_return": min((_finite_float(row.get("worst_total_return")) for row in rows), default=0.0),
            }
        )
    status = "robust" if robust.height >= config.min_robust_family_variants else "fragile_or_underexplored"
    return {
        "status": status,
        "same_family_variants": family.height,
        "robust_family_variants": robust.height,
        "robust_return_cutoff": return_cutoff,
        "robust_drawdown_cutoff": dd_cutoff,
        "parameter_groups": parameter_groups,
    }


def _concentration_report(trades: pl.DataFrame) -> dict[str, Any]:
    if trades.is_empty() or not {"symbol", "net_return"}.issubset(set(trades.columns)):
        return {"trades": trades.height, "status": "missing"}
    by_symbol = (
        trades.group_by("symbol")
        .agg(
            [
                pl.len().alias("trades"),
                pl.col("net_return").sum().alias("net_return_sum"),
                pl.col("net_return").mean().alias("mean_net_return"),
            ]
        )
        .with_columns(pl.col("net_return_sum").abs().alias("abs_net_return_sum"))
        .sort("abs_net_return_sum", descending=True)
    )
    total_abs = float(by_symbol["abs_net_return_sum"].sum()) if not by_symbol.is_empty() else 0.0
    top = by_symbol.head(1).to_dicts()[0] if total_abs > 0.0 else {}
    top5_abs = float(by_symbol.head(5)["abs_net_return_sum"].sum()) if total_abs > 0.0 else 0.0
    return {
        "status": "ok",
        "trades": trades.height,
        "symbols": by_symbol.height,
        "top_symbol": str(top.get("symbol", "")),
        "top_symbol_net_return": _finite_float(top.get("net_return_sum")),
        "top_symbol_abs_share": _finite_float(top.get("abs_net_return_sum")) / total_abs if total_abs > 0.0 else 0.0,
        "top5_symbol_abs_share": top5_abs / total_abs if total_abs > 0.0 else 0.0,
        "top_symbols": _json_ready(by_symbol.head(10).to_dicts()),
    }


def _cluster_report(trades: pl.DataFrame, baskets: pl.DataFrame) -> dict[str, Any]:
    report: dict[str, Any] = {}
    if not trades.is_empty() and {"entry_ts_ms", "net_return"}.issubset(set(trades.columns)):
        hourly = (
            trades.with_columns(((pl.col("entry_ts_ms") // MS_PER_HOUR) * MS_PER_HOUR).alias("entry_hour_ms"))
            .group_by("entry_hour_ms")
            .agg(
                [
                    pl.len().alias("trades"),
                    pl.col("net_return").sum().alias("net_return"),
                    (pl.col("net_return") < 0.0).sum().alias("losing_trades"),
                ]
            )
            .with_columns(pl.from_epoch(pl.col("entry_hour_ms"), time_unit="ms").dt.strftime("%Y-%m-%d %H:00").alias("entry_hour"))
            .sort(["net_return", "trades"], descending=[False, True])
        )
        worst = hourly.head(1).to_dicts()[0] if not hourly.is_empty() else {}
        report.update(
            {
                "entry_hour_clusters": hourly.height,
                "worst_entry_hour": str(worst.get("entry_hour", "")),
                "worst_entry_hour_trades": int(worst.get("trades", 0) or 0),
                "worst_entry_hour_losing_trades": int(worst.get("losing_trades", 0) or 0),
                "worst_entry_hour_net_return": _finite_float(worst.get("net_return")),
                "largest_entry_hour_trades": int(hourly["trades"].max()) if not hourly.is_empty() else 0,
            }
        )
    if not baskets.is_empty() and {"exit_ts_ms", "basket_return"}.issubset(set(baskets.columns)):
        daily = (
            baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("exit_date"))
            .group_by("exit_date")
            .agg([((pl.col("basket_return") + 1.0).product() - 1.0).alias("return"), pl.len().alias("baskets")])
            .sort("return")
        )
        worst_day = daily.head(1).to_dicts()[0] if not daily.is_empty() else {}
        report.update(
            {
                "exit_dates": daily.height,
                "worst_exit_date": str(worst_day.get("exit_date", "")),
                "worst_exit_date_return": _finite_float(worst_day.get("return")),
                "worst_exit_date_baskets": int(worst_day.get("baskets", 0) or 0),
            }
        )
    return report


def _findings(
    *,
    artifact_checks: list[dict[str, Any]],
    best: dict[str, Any],
    actual_metrics: dict[str, Any],
    bootstrap: dict[str, Any],
    random_sign: dict[str, Any],
    inverted: dict[str, Any],
    sensitivity: dict[str, Any],
    concentration: dict[str, Any],
    clustering: dict[str, Any],
    config: StrategyTribunalConfig,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    missing = [item["name"] for item in artifact_checks if not item["present"]]
    findings.append(
        _finding(
            "FAIL" if missing else "PASS",
            "artifacts",
            f"Missing required report artifacts: {', '.join(missing)}" if missing else "All core volume-event artifacts are present.",
        )
    )
    promotion = _boolish(best.get("promotion_gate_pass"))
    findings.append(
        _finding(
            "PASS" if promotion else "FAIL",
            "promotion_gate",
            f"Best scenario promotion gate is {best.get('promotion_gate_pass')} with reason `{best.get('promotion_reason', 'unknown')}`.",
        )
    )
    p05 = _finite_float(bootstrap.get("p05_total_return"))
    findings.append(
        _finding(
            "PASS" if p05 > 0.0 else "WATCH" if p05 > -0.10 else "FAIL",
            "bootstrap_left_tail",
            f"Block-bootstrap p05 total return is {_pct(p05)} across {bootstrap.get('samples', 0)} samples.",
        )
    )
    random_p95 = _finite_float(random_sign.get("p95_total_return"))
    actual_total = _finite_float(best.get("total_return", actual_metrics.get("total_return")))
    findings.append(
        _finding(
            "PASS" if random_p95 < actual_total else "FAIL",
            "random_sign_control",
            f"Random sign p95 total return is {_pct(random_p95)} versus actual {_pct(actual_total)}.",
        )
    )
    inverted_total = _finite_float(inverted.get("total_return"))
    findings.append(
        _finding(
            "PASS" if inverted_total < 0.0 else "FAIL",
            "inverted_edge_control",
            f"Gross-return inverted edge total return is {_pct(inverted_total)}.",
        )
    )
    robust_count = int(sensitivity.get("robust_family_variants", 0) or 0)
    sensitivity_status = str(sensitivity.get("status", "missing"))
    if sensitivity_status == "robust":
        level = "PASS"
    elif sensitivity_status in {"missing", "insufficient_single_scenario"}:
        level = "WATCH"
    else:
        level = "FAIL" if robust_count == 0 else "WATCH"
    findings.append(
        _finding(
            level,
            "parameter_sensitivity",
            f"Sensitivity status `{sensitivity_status}` with {robust_count} robust same-family variants.",
        )
    )
    top_share = _finite_float(concentration.get("top_symbol_abs_share"))
    if top_share >= config.symbol_concentration_fail:
        level = "FAIL"
    elif top_share >= config.symbol_concentration_watch:
        level = "WATCH"
    else:
        level = "PASS"
    findings.append(
        _finding(
            level,
            "symbol_concentration",
            f"Top symbol `{concentration.get('top_symbol', '')}` contributes {_pct(top_share)} of absolute additive symbol PnL.",
        )
    )
    cluster_trades = int(clustering.get("worst_entry_hour_trades", 0) or 0)
    cluster_return = _finite_float(clustering.get("worst_entry_hour_net_return"))
    findings.append(
        _finding(
            "WATCH" if cluster_trades >= config.clustered_loss_min_trades and cluster_return < 0.0 else "PASS",
            "entry_hour_crowding",
            f"Worst entry hour has {cluster_trades} trades and {_pct(cluster_return)} additive net return.",
        )
    )
    return findings


def _finding(level: str, check: str, message: str) -> dict[str, str]:
    return {"level": level, "check": check, "message": message}


def _verdict(findings: list[dict[str, str]]) -> str:
    levels = {finding.get("level", "") for finding in findings}
    if "FAIL" in levels:
        return "FAIL"
    if "WATCH" in levels:
        return "WATCH"
    return "PASS"


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    return value


def _finite_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        output = float(value)
    except (TypeError, ValueError):
        return 0.0
    return output if math.isfinite(output) else 0.0


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "pass"}
    return bool(value)


def _pct(value: Any) -> str:
    number = _finite_float(value)
    return f"{number:.2%}"
