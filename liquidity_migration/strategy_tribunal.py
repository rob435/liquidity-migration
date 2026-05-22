from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.crowding import audit_crowding_model
from liquidity_migration.storage import read_dataset
from liquidity_migration.trade_lifecycle import build_equity_curve

from ._common import MS_PER_HOUR, date_ms, finite_float, pct


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
    stress_return_fail: float = 0.0
    stress_drawdown_watch: float = -0.25
    stress_drawdown_fail: float = -0.35
    min_positive_month_rate: float = 0.55
    shuffled_time_worse_rate_watch: float = 0.95
    shuffled_symbol_top_share_buffer: float = 0.05
    shuffled_event_worst_hour_buffer: float = 0.0025
    execution_price_drift_watch_bps: float = 50.0
    execution_price_drift_fail_bps: float = 150.0
    execution_delay_drift_watch_minutes: float = 15.0
    execution_delay_drift_fail_minutes: float = 60.0


def run_strategy_tribunal(
    report_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
    comparison_csvs: tuple[str | Path, ...] = (),
    comparison_families: tuple[str, ...] = (),
    court_windows: tuple[str, ...] = (),
    execution_data_root: str | Path | None = None,
    config: StrategyTribunalConfig | None = None,
) -> dict[str, Any]:
    tribunal_config = config or StrategyTribunalConfig()
    source_dir = Path(report_dir).expanduser()
    target_dir = Path(output_dir).expanduser() if output_dir else source_dir / "strategy_tribunal"
    target_dir.mkdir(parents=True, exist_ok=True)

    artifacts = _read_artifacts(source_dir, comparison_csvs=comparison_csvs)
    summary = artifacts["summary"]
    trades = artifacts["trades"]
    baskets = artifacts["baskets"]
    best = _best_summary_row(summary, artifacts["research_report"])
    comparison = _comparison_family_frame(
        artifacts["comparison_summary"],
        best=best,
        requested_families=comparison_families,
    )
    sensitivity_source = comparison["frame"] if not comparison["frame"].is_empty() else summary
    returns = _basket_returns(baskets)
    actual_metrics = _actual_path_metrics(baskets, returns)
    consistency = _report_consistency(best, actual_metrics)
    windows = _pre_registered_window_report(baskets, best=best, window_specs=court_windows)
    bootstrap = _block_bootstrap(returns, config=tribunal_config)
    random_sign = _random_sign_control(returns, config=tribunal_config)
    inverted = _inverted_edge_control(baskets)
    shuffled_time = _shuffled_time_control(returns, config=tribunal_config)
    shuffled_symbol = _shuffled_symbol_control(trades, config=tribunal_config)
    shuffled_event = _shuffled_event_control(trades, config=tribunal_config)
    sensitivity = _sensitivity_report(sensitivity_source, best=best, config=tribunal_config)
    heatmaps = _sensitivity_heatmaps(sensitivity_source, target_dir=target_dir)
    stress = _stress_report(comparison["frame"], best=best)
    cost_funding_slippage = _cost_funding_slippage_report(comparison["frame"], best=best)
    regime = _regime_report(baskets)
    execution_drift = _execution_drift_report(execution_data_root, trades=trades)
    crowding_model = audit_crowding_model(trades, output_dir=target_dir)
    concentration = _concentration_report(trades)
    clustering = _cluster_report(trades, baskets)
    findings = _findings(
        artifact_checks=artifacts["checks"],
        comparison_metadata=comparison["metadata"],
        best=best,
        actual_metrics=actual_metrics,
        consistency=consistency,
        windows=windows,
        bootstrap=bootstrap,
        random_sign=random_sign,
        inverted=inverted,
        shuffled_time=shuffled_time,
        shuffled_symbol=shuffled_symbol,
        shuffled_event=shuffled_event,
        sensitivity=sensitivity,
        heatmaps=heatmaps,
        stress=stress,
        cost_funding_slippage=cost_funding_slippage,
        regime=regime,
        execution_drift=execution_drift,
        crowding_model=crowding_model,
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
        "comparison_family_request": list(comparison_families),
        "comparison_family": comparison["metadata"],
        "best_scenario": best,
        "actual_path": actual_metrics,
        "report_consistency": consistency,
        "pre_registered_windows": windows,
        "bootstrap": bootstrap,
        "negative_controls": {
            "random_sign": random_sign,
            "inverted_edge": inverted,
            "shuffled_time": shuffled_time,
            "shuffled_symbol": shuffled_symbol,
            "shuffled_event": shuffled_event,
        },
        "sensitivity": sensitivity,
        "sensitivity_heatmaps": heatmaps,
        "stress": stress,
        "cost_funding_slippage": cost_funding_slippage,
        "regime": regime,
        "execution_drift": execution_drift,
        "crowding_model": crowding_model,
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
    consistency = payload.get("report_consistency", {})
    windows = payload.get("pre_registered_windows", {})
    comparison = payload.get("comparison_family", {})
    bootstrap = payload.get("bootstrap", {})
    controls = payload.get("negative_controls", {})
    sensitivity = payload.get("sensitivity", {})
    heatmaps = payload.get("sensitivity_heatmaps", {})
    stress = payload.get("stress", {})
    cost_funding_slippage = payload.get("cost_funding_slippage", {})
    regime = payload.get("regime", {})
    execution_drift = payload.get("execution_drift", {})
    crowding_model = payload.get("crowding_model", {})
    concentration = payload.get("concentration", {})
    clustering = payload.get("clustering", {})
    lines = [
        "# Model Court",
        "",
        "Formal promotion court for a completed `volume-events` report. This does not prove future PnL; it looks for ways the backtest could be fragile.",
        "",
        "## Verdict",
        "",
        f"- Verdict: **{payload.get('verdict', 'UNKNOWN')}**",
        f"- Source: `{payload.get('source_report_dir', '')}`",
        f"- Comparison CSVs: {len(payload.get('comparison_csvs', []))}",
        f"- Comparison family: `{comparison.get('status', 'none')}` ({_comparison_family_label(comparison)})",
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
            f"| Shuffled time | p05 max drawdown | {_pct(controls.get('shuffled_time', {}).get('p05_max_drawdown'))} |",
            f"| Shuffled symbol | p95 top-symbol share | {_pct(controls.get('shuffled_symbol', {}).get('p95_top_symbol_abs_share'))} |",
            f"| Shuffled event | p05 worst hour return | {_pct(controls.get('shuffled_event', {}).get('p05_worst_entry_hour_return'))} |",
            "",
            "## Path Consistency",
            "",
            "| Metric | Reported | Recomputed | Absolute Diff |",
            "|---|---:|---:|---:|",
            (
                f"| Total return | {_pct(consistency.get('total_return_reported'))} | "
                f"{_pct(consistency.get('total_return_recomputed'))} | "
                f"{_pct(consistency.get('total_return_abs_diff'))} |"
            ),
            (
                f"| Max drawdown | {_pct(consistency.get('max_drawdown_reported'))} | "
                f"{_pct(consistency.get('max_drawdown_recomputed'))} | "
                f"{_pct(consistency.get('max_drawdown_abs_diff'))} |"
            ),
            "",
            "## Pre-Registered Windows",
            "",
            f"- Status: `{windows.get('status', 'missing')}`",
            f"- Window count: {windows.get('window_count', 0)}",
            f"- Positive windows: {windows.get('positive_windows', 0)}",
            f"- Minimum window return: {_pct(windows.get('min_total_return'))}",
            "",
            "| Window | Start | End | Baskets | Total Return | Max Drawdown |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for row in windows.get("windows", []):
        lines.append(
            f"| `{row.get('name', '')}` | {row.get('start_date', '')} | {row.get('end_date', '')} | "
            f"{row.get('basket_count', 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('max_drawdown'))} |"
        )
    lines.extend(
        [
            "",
            "## Stress Matrix",
            "",
            f"- Status: `{stress.get('status', 'missing')}`",
            f"- Rows: {stress.get('rows', 0)}",
            f"- Min stress return: {_pct(stress.get('min_total_return'))}",
            f"- Worst stress drawdown: {_pct(stress.get('worst_max_drawdown'))}",
            f"- Promotion pass rate: {_pct(stress.get('promotion_pass_rate'))}",
            "",
            "## Cost Funding Slippage",
            "",
            f"- Status: `{cost_funding_slippage.get('status', 'missing')}`",
            f"- Cost levels: {', '.join(str(item) for item in cost_funding_slippage.get('cost_levels', []))}",
            f"- Stop/slippage modes: {', '.join(str(item) for item in cost_funding_slippage.get('stop_fill_modes', []))}",
            f"- Funding modes: {', '.join(str(item) for item in cost_funding_slippage.get('funding_modes', []))}",
            "",
            "## Sensitivity",
            "",
            f"- Status: `{sensitivity.get('status', 'unknown')}`",
            f"- Same-family variants: {sensitivity.get('same_family_variants', 0)}",
            f"- Robust same-family variants: {sensitivity.get('robust_family_variants', 0)}",
            f"- Robust return cutoff: {_pct(sensitivity.get('robust_return_cutoff'))}",
            f"- Robust max-drawdown cutoff: {_pct(sensitivity.get('robust_drawdown_cutoff'))}",
            f"- Heatmaps: {heatmaps.get('heatmap_count', 0)}",
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
            "## Regime Path",
            "",
            "| Check | Value |",
            "|---|---:|",
            f"| Months | {regime.get('months', 0)} |",
            f"| Positive month rate | {_pct(regime.get('positive_month_rate'))} |",
            f"| Worst month | `{regime.get('worst_month', '')}` |",
            f"| Worst month return | {_pct(regime.get('worst_month_return'))} |",
            f"| Max monthly no-new-high stretch | {regime.get('max_monthly_underwater_months', 0)} |",
            "",
            "## Execution Drift",
            "",
            f"- Status: `{execution_drift.get('status', 'missing')}`",
            f"- Execution root: `{execution_drift.get('execution_data_root', '')}`",
            f"- Matched entry orders: {execution_drift.get('matched_entry_orders', 0)}",
            f"- P95 absolute entry price drift: {_num(execution_drift.get('p95_abs_entry_price_drift_bps'))} bps",
            f"- P95 absolute entry delay drift: {_num(execution_drift.get('p95_abs_entry_delay_drift_minutes'))} minutes",
            "",
            "## Crowding Model",
            "",
            f"- Status: `{crowding_model.get('status', 'missing')}`",
            f"- Tradeable rows: {crowding_model.get('tradeable_rows', 0)}",
            f"- Non-tradeable rows: {crowding_model.get('non_tradeable_rows', 0)}",
            "",
            "| Class | Rows | Additive Return | Compounded Return |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in crowding_model.get("summary", []):
        lines.append(
            f"| `{row.get('crowding_class', '')}` | {row.get('rows', 0)} | "
            f"{_pct(row.get('additive_return'))} | {_pct(row.get('compounded_return'))} |"
        )
    lines.extend(
        [
            "",
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


def _comparison_family_frame(
    comparison: pl.DataFrame,
    *,
    best: dict[str, Any],
    requested_families: tuple[str, ...],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "status": "missing",
        "requested_families": list(requested_families),
        "selected_families": [],
        "rows_before": comparison.height,
        "rows_after": 0,
    }
    if comparison.is_empty():
        return {"frame": comparison, "metadata": metadata}
    frame = comparison
    selected = tuple(item for item in requested_families if item)
    if not selected:
        selected = _infer_comparison_families(comparison, best=best)
        metadata["inferred_families"] = list(selected)
    if selected and "strategy" in comparison.columns:
        frame = comparison.filter(pl.col("strategy").is_in(list(selected)))
        metadata["status"] = "explicit" if requested_families else "inferred"
    else:
        metadata["status"] = "unfiltered"
    metadata["selected_families"] = list(selected)
    metadata["rows_after"] = frame.height
    if frame.is_empty():
        metadata["status"] = "empty_after_filter"
    return {"frame": frame, "metadata": metadata}


def _infer_comparison_families(comparison: pl.DataFrame, *, best: dict[str, Any]) -> tuple[str, ...]:
    if comparison.is_empty() or "strategy" not in comparison.columns:
        return ()
    strategies = sorted(str(item) for item in comparison["strategy"].drop_nulls().unique().to_list())
    if not strategies:
        return ()
    trade_count = int(_finite_float(best.get("trades")))
    funding_mode = str(best.get("funding_mode", "")).lower()
    candidate = strategies
    if trade_count > 0:
        near = [
            item
            for item in candidate
            if _strategy_trade_distance(comparison, strategy=item, trade_count=trade_count) <= max(5, int(trade_count * 0.05))
        ]
        if near:
            candidate = near
    if funding_mode in {"modeled", "partial"}:
        funded = [item for item in candidate if "funding" in item and "nofunding" not in item]
        if funded:
            candidate = funded
    elif funding_mode == "missing":
        nofunding = [item for item in candidate if "nofunding" in item]
        if nofunding:
            candidate = nofunding
    return tuple(candidate[:3])


def _strategy_trade_distance(comparison: pl.DataFrame, *, strategy: str, trade_count: int) -> int:
    if "trades" not in comparison.columns:
        return 0
    rows = comparison.filter(pl.col("strategy") == strategy)
    if rows.is_empty():
        return 1_000_000_000
    values = [abs(int(_finite_float(item)) - trade_count) for item in rows["trades"].drop_nulls().to_list()]
    return min(values) if values else 1_000_000_000


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
    ordered = baskets.sort("exit_ts_ms") if "exit_ts_ms" in baskets.columns else baskets
    return [float(value) for value in ordered["basket_return"].to_list() if value is not None and math.isfinite(float(value))]


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


def _actual_path_metrics(baskets: pl.DataFrame, returns: list[float]) -> dict[str, Any]:
    """Recompute the realised path the way volume-events reports it -- via
    build_equity_curve (daily-grid compounding) -- so report_consistency
    compares like with like. _return_path_metrics' per-basket cum_prod is kept
    for the resampled negative-control series, which carry no exit dates to
    compound on.
    """
    metrics = _return_path_metrics(returns)
    if baskets.is_empty():
        return metrics
    equity = build_equity_curve(baskets)
    if equity.is_empty():
        return metrics
    return {
        **metrics,
        "total_return": float(equity["equity"][-1] - 1.0),
        "max_drawdown": float(equity["drawdown"].min()),
    }


def _report_consistency(best: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    expected_return = _finite_float(best.get("total_return"))
    expected_dd = _finite_float(best.get("max_drawdown"))
    actual_return = _finite_float(actual.get("total_return"))
    actual_dd = _finite_float(actual.get("max_drawdown"))
    return {
        "total_return_reported": expected_return,
        "total_return_recomputed": actual_return,
        "total_return_abs_diff": abs(expected_return - actual_return),
        "max_drawdown_reported": expected_dd,
        "max_drawdown_recomputed": actual_dd,
        "max_drawdown_abs_diff": abs(expected_dd - actual_dd),
    }


def _pre_registered_window_report(
    baskets: pl.DataFrame,
    *,
    best: dict[str, Any],
    window_specs: tuple[str, ...],
) -> dict[str, Any]:
    windows, errors = _parse_window_specs(window_specs)
    if errors:
        return {"status": "invalid", "errors": errors, "windows": [], "window_count": 0, "positive_windows": 0}
    if not windows:
        return {"status": "missing", "windows": [], "window_count": 0, "positive_windows": 0}
    rows = []
    sorted_baskets = baskets.sort("entry_signal_ts_ms") if not baskets.is_empty() and "entry_signal_ts_ms" in baskets.columns else baskets
    for window in windows:
        part = pl.DataFrame()
        if not sorted_baskets.is_empty() and "entry_signal_ts_ms" in sorted_baskets.columns:
            part = sorted_baskets.filter(
                (pl.col("entry_signal_ts_ms") >= window["start_ms"]) & (pl.col("entry_signal_ts_ms") < window["end_ms"])
            )
        metrics = _return_path_metrics(_basket_returns(part))
        rows.append(
            {
                "name": window["name"],
                "start_date": window["start_date"],
                "end_date": window["end_date"],
                "basket_count": int(metrics.get("observations", 0) or 0),
                "total_return": _finite_float(metrics.get("total_return")),
                "max_drawdown": _finite_float(metrics.get("max_drawdown")),
                "sharpe_like": _finite_float(metrics.get("sharpe_like")),
            }
        )
    returns = [_finite_float(row.get("total_return")) for row in rows]
    drawdowns = [_finite_float(row.get("max_drawdown")) for row in rows]
    return {
        "status": "explicit",
        "windows": rows,
        "window_count": len(rows),
        "positive_windows": int(sum(1 for value in returns if value > 0.0)),
        "complete_windows": int(sum(1 for row in rows if int(row.get("basket_count", 0) or 0) > 0)),
        "min_total_return": min(returns, default=0.0),
        "worst_max_drawdown": min(drawdowns, default=0.0),
    }


def _parse_window_specs(window_specs: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for raw in window_specs:
        spec = str(raw).strip()
        if not spec:
            continue
        parts = spec.split(":")
        if len(parts) != 3:
            errors.append(f"`{spec}` must be name:start:end")
            continue
        name, start, end = (part.strip() for part in parts)
        if not name or not start or not end:
            errors.append(f"`{spec}` must include non-empty name, start, and end")
            continue
        start_ms = _date_ms(start)
        end_ms = _date_ms(end)
        if start_ms >= end_ms:
            errors.append(f"`{spec}` has start >= end")
            continue
        rows.append({"name": name, "start_date": start, "end_date": end, "start_ms": start_ms, "end_ms": end_ms})
    return rows, errors


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


def _shuffled_time_control(returns: list[float], *, config: StrategyTribunalConfig) -> dict[str, Any]:
    if not returns or config.bootstrap_samples <= 0:
        return {"samples": 0}
    rng = np.random.default_rng(config.random_seed + 2)
    values = np.asarray(returns, dtype=float)
    actual = _return_path_metrics(values.tolist())
    drawdowns = []
    for _ in range(config.bootstrap_samples):
        metrics = _return_path_metrics(rng.permutation(values).tolist())
        drawdowns.append(float(metrics["max_drawdown"]))
    arr = np.asarray(drawdowns, dtype=float)
    return {
        "samples": config.bootstrap_samples,
        "actual_max_drawdown": _finite_float(actual.get("max_drawdown")),
        "p05_max_drawdown": float(np.quantile(arr, 0.05)),
        "median_max_drawdown": float(np.quantile(arr, 0.50)),
        "p95_max_drawdown": float(np.quantile(arr, 0.95)),
        "shuffled_worse_or_equal_actual_rate": float(np.mean(arr <= _finite_float(actual.get("max_drawdown")))),
    }


def _shuffled_symbol_control(trades: pl.DataFrame, *, config: StrategyTribunalConfig) -> dict[str, Any]:
    if trades.is_empty() or config.bootstrap_samples <= 0 or not {"symbol", "net_return"}.issubset(set(trades.columns)):
        return {"samples": 0}
    symbols = [str(item) for item in trades["symbol"].to_list()]
    returns = np.asarray([_finite_float(item) for item in trades["net_return"].to_list()], dtype=float)
    actual_share = _symbol_top_abs_share(symbols, returns.tolist())
    rng = np.random.default_rng(config.random_seed + 3)
    shares = []
    for _ in range(config.bootstrap_samples):
        shares.append(_symbol_top_abs_share(symbols, rng.permutation(returns).tolist()))
    arr = np.asarray(shares, dtype=float)
    return {
        "samples": config.bootstrap_samples,
        "actual_top_symbol_abs_share": actual_share,
        "median_top_symbol_abs_share": float(np.quantile(arr, 0.50)),
        "p95_top_symbol_abs_share": float(np.quantile(arr, 0.95)),
        "shuffled_exceed_actual_rate": float(np.mean(arr >= actual_share)),
    }


def _symbol_top_abs_share(symbols: list[str], returns: list[float]) -> float:
    by_symbol: dict[str, float] = {}
    for symbol, value in zip(symbols, returns):
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + float(value)
    abs_values = [abs(value) for value in by_symbol.values()]
    total_abs = sum(abs_values)
    return max(abs_values, default=0.0) / total_abs if total_abs > 0.0 else 0.0


def _shuffled_event_control(trades: pl.DataFrame, *, config: StrategyTribunalConfig) -> dict[str, Any]:
    if trades.is_empty() or config.bootstrap_samples <= 0 or not {"entry_ts_ms", "net_return"}.issubset(set(trades.columns)):
        return {"samples": 0}
    hours = np.asarray([int(_finite_float(item)) // MS_PER_HOUR for item in trades["entry_ts_ms"].to_list()], dtype=np.int64)
    returns = np.asarray([_finite_float(item) for item in trades["net_return"].to_list()], dtype=float)
    actual_worst = _worst_group_sum(hours, returns)
    rng = np.random.default_rng(config.random_seed + 4)
    worst_returns = []
    for _ in range(config.bootstrap_samples):
        worst_returns.append(_worst_group_sum(hours, rng.permutation(returns)))
    arr = np.asarray(worst_returns, dtype=float)
    return {
        "samples": config.bootstrap_samples,
        "actual_worst_entry_hour_return": actual_worst,
        "p05_worst_entry_hour_return": float(np.quantile(arr, 0.05)),
        "median_worst_entry_hour_return": float(np.quantile(arr, 0.50)),
        "p95_worst_entry_hour_return": float(np.quantile(arr, 0.95)),
        "shuffled_worse_or_equal_actual_rate": float(np.mean(arr <= actual_worst)),
    }


def _worst_group_sum(groups: np.ndarray, returns: np.ndarray) -> float:
    totals: dict[int, float] = {}
    for group, value in zip(groups.tolist(), returns.tolist()):
        totals[int(group)] = totals.get(int(group), 0.0) + float(value)
    return min(totals.values(), default=0.0)


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
        robust = robust.filter(pl.col("promotion_gate_pass"))
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


def _sensitivity_heatmaps(summary: pl.DataFrame, *, target_dir: Path) -> dict[str, Any]:
    axes = [
        column
        for column in (
            "threshold",
            "hold_days",
            "stop_loss_pct",
            "take_profit_pct",
            "cost_multiplier",
            "entry_selector",
            "stop_fill_mode",
        )
        if column in summary.columns and summary[column].n_unique() > 1
    ]
    heatmaps = []
    if summary.is_empty() or len(axes) < 2:
        return {"status": "insufficient_axes", "heatmap_count": 0, "heatmaps": []}
    for left_index, left in enumerate(axes):
        for right in axes[left_index + 1 :]:
            grouped = (
                summary.group_by([left, right])
                .agg(
                    [
                        pl.len().alias("rows"),
                        pl.col("total_return").max().alias("best_total_return")
                        if "total_return" in summary.columns
                        else pl.lit(0.0).alias("best_total_return"),
                        pl.col("total_return").min().alias("worst_total_return")
                        if "total_return" in summary.columns
                        else pl.lit(0.0).alias("worst_total_return"),
                        pl.col("max_drawdown").min().alias("worst_max_drawdown")
                        if "max_drawdown" in summary.columns
                        else pl.lit(0.0).alias("worst_max_drawdown"),
                        pl.col("promotion_gate_pass").cast(pl.Int64).sum().alias("promotion_passes")
                        if "promotion_gate_pass" in summary.columns
                        else pl.lit(0).alias("promotion_passes"),
                    ]
                )
                .sort([left, right])
            )
            filename = f"sensitivity_heatmap_{_safe_name(left)}_x_{_safe_name(right)}.csv"
            path = target_dir / filename
            grouped.write_csv(path)
            heatmaps.append(
                {
                    "axes": [left, right],
                    "rows": grouped.height,
                    "path": str(path),
                    "min_total_return": float(grouped["worst_total_return"].min()) if "worst_total_return" in grouped.columns else 0.0,
                    "worst_max_drawdown": float(grouped["worst_max_drawdown"].min()) if "worst_max_drawdown" in grouped.columns else 0.0,
                }
            )
    return {"status": "present", "heatmap_count": len(heatmaps), "heatmaps": heatmaps}


def _stress_report(summary: pl.DataFrame, *, best: dict[str, Any]) -> dict[str, Any]:
    if summary.is_empty():
        return {"status": "missing", "rows": 0}
    out: dict[str, Any] = {"status": "present", "rows": summary.height}
    if "total_return" in summary.columns:
        out["min_total_return"] = float(summary["total_return"].min())
        out["median_total_return"] = float(summary["total_return"].median())
    else:
        out["min_total_return"] = 0.0
        out["median_total_return"] = 0.0
    if "max_drawdown" in summary.columns:
        out["worst_max_drawdown"] = float(summary["max_drawdown"].min())
        out["median_max_drawdown"] = float(summary["max_drawdown"].median())
    else:
        out["worst_max_drawdown"] = 0.0
        out["median_max_drawdown"] = 0.0
    if "promotion_gate_pass" in summary.columns:
        out["promotion_pass_rate"] = float(summary["promotion_gate_pass"].cast(pl.Float64).mean())
        out["promotion_passes"] = int(summary["promotion_gate_pass"].sum())
    else:
        out["promotion_pass_rate"] = 0.0
        out["promotion_passes"] = 0
    stress_axes = []
    for column in ("strategy", "stop_fill_mode", "cost_multiplier", "entry_selector"):
        if column not in summary.columns or summary[column].n_unique() <= 1:
            continue
        grouped = (
            summary.group_by(column)
            .agg(
                [
                    pl.len().alias("rows"),
                    pl.col("total_return").min().alias("min_total_return")
                    if "total_return" in summary.columns
                    else pl.lit(0.0).alias("min_total_return"),
                    pl.col("max_drawdown").min().alias("worst_max_drawdown")
                    if "max_drawdown" in summary.columns
                    else pl.lit(0.0).alias("worst_max_drawdown"),
                    pl.col("promotion_gate_pass").sum().alias("promotion_passes")
                    if "promotion_gate_pass" in summary.columns
                    else pl.lit(0).alias("promotion_passes"),
                ]
            )
            .sort("min_total_return")
        )
        stress_axes.append({"axis": column, "levels": _json_ready(grouped.to_dicts())})
    out["axes"] = stress_axes
    out["reported_best_total_return"] = _finite_float(best.get("total_return"))
    return out


def _cost_funding_slippage_report(summary: pl.DataFrame, *, best: dict[str, Any]) -> dict[str, Any]:
    if summary.is_empty():
        return {"status": "missing"}
    report: dict[str, Any] = {
        "status": "present",
        "rows": summary.height,
        "reported_best_total_return": _finite_float(best.get("total_return")),
    }
    if "cost_multiplier" in summary.columns:
        cost_levels = sorted({_finite_float(item) for item in summary["cost_multiplier"].drop_nulls().to_list()})
        report["cost_levels"] = cost_levels
        if cost_levels:
            max_cost = max(cost_levels)
            high_cost = summary.filter(pl.col("cost_multiplier") == max_cost)
            report["max_cost_multiplier"] = max_cost
            report["max_cost_min_total_return"] = float(high_cost["total_return"].min()) if "total_return" in high_cost.columns else 0.0
            report["max_cost_worst_drawdown"] = float(high_cost["max_drawdown"].min()) if "max_drawdown" in high_cost.columns else 0.0
    else:
        report["cost_levels"] = []
    if "stop_fill_mode" in summary.columns:
        report["stop_fill_modes"] = sorted(str(item) for item in summary["stop_fill_mode"].drop_nulls().unique().to_list())
    else:
        report["stop_fill_modes"] = []
    if "funding_mode" in summary.columns:
        report["funding_modes"] = sorted(str(item) for item in summary["funding_mode"].drop_nulls().unique().to_list())
    else:
        report["funding_modes"] = []
    if "funding_return" in summary.columns:
        report["worst_funding_return"] = float(summary["funding_return"].min())
        report["median_funding_return"] = float(summary["funding_return"].median())
    return report


def _execution_drift_report(execution_data_root: str | Path | None, *, trades: pl.DataFrame) -> dict[str, Any]:
    if execution_data_root is None or str(execution_data_root).strip() == "":
        return {"status": "missing", "execution_data_root": ""}
    root = Path(execution_data_root).expanduser()
    orders = read_dataset(root, "event_demo_orders")
    demo_trades = read_dataset(root, "event_demo_trades")
    cycles = read_dataset(root, "event_demo_cycles")
    report: dict[str, Any] = {
        "status": "missing",
        "execution_data_root": str(root),
        "orders": orders.height,
        "demo_trades": demo_trades.height,
        "cycles": cycles.height,
    }
    if orders.is_empty():
        return report
    if trades.is_empty() or not {"symbol", "entry_signal_ts_ms", "entry_ts_ms", "entry_price"}.issubset(set(trades.columns)):
        report["status"] = "missing_backtest_join_keys"
        return report
    entry_orders = orders
    if "reduce_only" in entry_orders.columns:
        entry_orders = entry_orders.filter(pl.col("reduce_only") == False)  # noqa: E712
    if "status" in entry_orders.columns:
        entry_orders = entry_orders.filter(pl.col("status").is_in(["filled", "partial", "planned"]))
    if entry_orders.is_empty() or not {"symbol", "signal_ts_ms", "avg_price", "ts_ms"}.issubset(set(entry_orders.columns)):
        report["status"] = "no_entry_orders"
        return report
    backtest = trades.select(
        [
            pl.col("symbol"),
            pl.col("entry_signal_ts_ms").alias("signal_ts_ms"),
            pl.col("entry_ts_ms").alias("backtest_entry_ts_ms"),
            pl.col("entry_price").alias("backtest_entry_price"),
            pl.col("side").alias("backtest_side") if "side" in trades.columns else pl.lit("").alias("backtest_side"),
        ]
    )
    joined = entry_orders.join(backtest, on=["symbol", "signal_ts_ms"], how="left")
    matched = joined.filter(pl.col("backtest_entry_price").is_not_null())
    report["entry_orders"] = entry_orders.height
    report["matched_entry_orders"] = matched.height
    report["unmatched_entry_orders"] = entry_orders.height - matched.height
    if matched.is_empty():
        report["status"] = "no_matches"
        return report
    matched = matched.with_columns(
        [
            ((pl.col("avg_price").cast(pl.Float64) / pl.col("backtest_entry_price").cast(pl.Float64) - 1.0) * 10_000.0).alias(
                "entry_price_drift_bps"
            ),
            ((pl.col("ts_ms").cast(pl.Float64) - pl.col("backtest_entry_ts_ms").cast(pl.Float64)) / 60_000.0).alias(
                "entry_delay_drift_minutes"
            ),
        ]
    ).with_columns(
        [
            pl.col("entry_price_drift_bps").abs().alias("abs_entry_price_drift_bps"),
            pl.col("entry_delay_drift_minutes").abs().alias("abs_entry_delay_drift_minutes"),
        ]
    )
    report.update(
        {
            "status": "present",
            "median_abs_entry_price_drift_bps": float(matched["abs_entry_price_drift_bps"].median()),
            "p95_abs_entry_price_drift_bps": _quantile(matched["abs_entry_price_drift_bps"].to_list(), 0.95),
            "median_abs_entry_delay_drift_minutes": float(matched["abs_entry_delay_drift_minutes"].median()),
            "p95_abs_entry_delay_drift_minutes": _quantile(matched["abs_entry_delay_drift_minutes"].to_list(), 0.95),
            "max_abs_entry_price_drift_bps": float(matched["abs_entry_price_drift_bps"].max()),
            "max_abs_entry_delay_drift_minutes": float(matched["abs_entry_delay_drift_minutes"].max()),
        }
    )
    return report


def _regime_report(baskets: pl.DataFrame) -> dict[str, Any]:
    if baskets.is_empty() or not {"exit_ts_ms", "basket_return"}.issubset(set(baskets.columns)):
        return {"status": "missing", "months": 0}
    monthly = (
        baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg([((pl.col("basket_return") + 1.0).product() - 1.0).alias("return"), pl.len().alias("baskets")])
        .sort("month")
    )
    returns = [float(item) for item in monthly["return"].to_list()]
    equity = 1.0
    peak = 1.0
    underwater = 0
    max_underwater = 0
    for value in returns:
        equity *= 1.0 + value
        if equity >= peak - 1e-12:
            peak = equity
            underwater = 0
        else:
            underwater += 1
            max_underwater = max(max_underwater, underwater)
    worst = monthly.sort("return").head(1).to_dicts()[0] if not monthly.is_empty() else {}
    return {
        "status": "ok",
        "months": monthly.height,
        "positive_months": int(sum(1 for value in returns if value > 0.0)),
        "positive_month_rate": float(np.mean(np.asarray(returns) > 0.0)) if returns else 0.0,
        "worst_month": str(worst.get("month", "")),
        "worst_month_return": _finite_float(worst.get("return")),
        "worst_month_baskets": int(worst.get("baskets", 0) or 0),
        "max_monthly_underwater_months": max_underwater,
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
    comparison_metadata: dict[str, Any],
    best: dict[str, Any],
    actual_metrics: dict[str, Any],
    consistency: dict[str, Any],
    windows: dict[str, Any],
    bootstrap: dict[str, Any],
    random_sign: dict[str, Any],
    inverted: dict[str, Any],
    shuffled_time: dict[str, Any],
    shuffled_symbol: dict[str, Any],
    shuffled_event: dict[str, Any],
    sensitivity: dict[str, Any],
    heatmaps: dict[str, Any],
    stress: dict[str, Any],
    cost_funding_slippage: dict[str, Any],
    regime: dict[str, Any],
    execution_drift: dict[str, Any],
    crowding_model: dict[str, Any],
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
    requested_families = [str(item) for item in comparison_metadata.get("requested_families", []) if item]
    comparison_status = str(comparison_metadata.get("status", "missing"))
    comparison_rows = int(comparison_metadata.get("rows_after", 0) or 0)
    if requested_families and comparison_rows <= 0:
        comparison_level = "FAIL"
        comparison_message = f"Requested comparison family produced no rows: {', '.join(requested_families)}."
    elif requested_families:
        comparison_level = "PASS"
        comparison_message = f"Comparison family filter selected {_comparison_family_label(comparison_metadata)}."
    elif comparison_status in {"inferred", "unfiltered"} and comparison_rows > 0:
        comparison_level = "WATCH" if comparison_status == "unfiltered" else "PASS"
        comparison_message = f"Comparison family status `{comparison_status}` selected {_comparison_family_label(comparison_metadata)}."
    else:
        comparison_level = "WATCH"
        comparison_message = "No comparison family stress evidence was attached."
    findings.append(_finding(comparison_level, "comparison_family", comparison_message))
    consistency_ok = (
        _finite_float(consistency.get("total_return_abs_diff")) < 1e-6
        and _finite_float(consistency.get("max_drawdown_abs_diff")) < 1e-6
    )
    promotion = _boolish(best.get("promotion_gate_pass"))
    promotion_reason = str(best.get("promotion_reason", "") or "").strip()
    if promotion and (not promotion_reason or promotion_reason.lower() == "unknown"):
        # A gate pass with no stated reason is not independently verifiable.
        promotion_level = "FAIL"
        promotion_message = (
            f"Promotion gate is {best.get('promotion_gate_pass')} but the reason is empty/unknown "
            "-- an unexplained gate pass is not trusted."
        )
    elif promotion and not consistency_ok:
        # The reported gate is derived from the reported metrics; if the
        # recomputed basket path does not reconcile with them, it is not trusted.
        promotion_level = "FAIL"
        promotion_message = (
            f"Promotion gate is {best.get('promotion_gate_pass')} but the recomputed basket path "
            "does not reconcile with the reported metrics -- the gate is not trusted."
        )
    else:
        promotion_level = "PASS" if promotion else "FAIL"
        promotion_message = (
            f"Best scenario promotion gate is {best.get('promotion_gate_pass')} "
            f"with reason `{best.get('promotion_reason', 'unknown')}`."
        )
    findings.append(_finding(promotion_level, "promotion_gate", promotion_message))
    findings.append(
        _finding(
            "PASS" if consistency_ok else "FAIL",
            "report_consistency",
            "Recomputed basket path matches the reported best row."
            if consistency_ok
            else (
                f"Recomputed path differs: return diff {_pct(consistency.get('total_return_abs_diff'))}, "
                f"drawdown diff {_pct(consistency.get('max_drawdown_abs_diff'))}."
            ),
        )
    )
    window_status = str(windows.get("status", "missing"))
    window_count = int(windows.get("window_count", 0) or 0)
    positive_windows = int(windows.get("positive_windows", 0) or 0)
    complete_windows = int(windows.get("complete_windows", 0) or 0)
    if window_status == "explicit":
        if window_count > 0 and positive_windows == window_count and complete_windows == window_count:
            window_level = "PASS"
        else:
            window_level = "FAIL"
    elif window_status == "invalid":
        window_level = "FAIL"
    else:
        window_level = "WATCH"
    findings.append(
        _finding(
            window_level,
            "pre_registered_windows",
            f"Window status `{window_status}` with {positive_windows}/{window_count} positive windows.",
        )
    )
    funding_mode = str(best.get("funding_mode", "missing"))
    findings.append(
        _finding(
            "PASS" if funding_mode == "modeled" else "WATCH" if funding_mode == "partial" else "FAIL",
            "funding_coverage",
            f"Funding mode is `{funding_mode}`.",
        )
    )
    # A control that did not run (0 samples / no basket data) must FAIL: it is
    # absent evidence, never a defaulted pass. _block_bootstrap / _random_sign_control
    # omit their metric key entirely when they do not run; _inverted_edge_control
    # then carries no `total_return`.
    if "p05_total_return" not in bootstrap:
        findings.append(
            _finding(
                "FAIL",
                "bootstrap_left_tail",
                f"Block-bootstrap control did not run ({bootstrap.get('samples', 0)} samples) -- no left-tail evidence.",
            )
        )
    else:
        p05 = _finite_float(bootstrap.get("p05_total_return"))
        findings.append(
            _finding(
                "PASS" if p05 > 0.0 else "WATCH" if p05 > -0.10 else "FAIL",
                "bootstrap_left_tail",
                f"Block-bootstrap p05 total return is {_pct(p05)} across {bootstrap.get('samples', 0)} samples.",
            )
        )
    if "p95_total_return" not in random_sign:
        findings.append(
            _finding(
                "FAIL",
                "random_sign_control",
                f"Random-sign control did not run ({random_sign.get('samples', 0)} samples) -- "
                "the edge is not distinguished from noise.",
            )
        )
    else:
        random_p95 = _finite_float(random_sign.get("p95_total_return"))
        actual_total = _finite_float(best.get("total_return", actual_metrics.get("total_return")))
        findings.append(
            _finding(
                "PASS" if random_p95 < actual_total else "FAIL",
                "random_sign_control",
                f"Random sign p95 total return is {_pct(random_p95)} versus actual {_pct(actual_total)}.",
            )
        )
    if "total_return" not in inverted:
        findings.append(
            _finding(
                "FAIL",
                "inverted_edge_control",
                "Inverted-edge control did not run (no basket observations).",
            )
        )
    else:
        inverted_total = _finite_float(inverted.get("total_return"))
        findings.append(
            _finding(
                "PASS" if inverted_total < 0.0 else "FAIL",
                "inverted_edge_control",
                f"Gross-return inverted edge total return is {_pct(inverted_total)}.",
            )
        )
    shuffled_time_worse_rate = _finite_float(shuffled_time.get("shuffled_worse_or_equal_actual_rate"))
    shuffled_time_level = (
        "WATCH"
        if _finite_float(shuffled_time.get("actual_max_drawdown")) < 0.0
        and shuffled_time_worse_rate >= config.shuffled_time_worse_rate_watch
        else "PASS"
    )
    findings.append(
        _finding(
            shuffled_time_level,
            "shuffled_time_control",
            f"Shuffled-time paths are worse than actual drawdown in {_pct(shuffled_time_worse_rate)} of samples.",
        )
    )
    actual_symbol_share = _finite_float(shuffled_symbol.get("actual_top_symbol_abs_share"))
    symbol_p95 = _finite_float(shuffled_symbol.get("p95_top_symbol_abs_share"))
    findings.append(
        _finding(
            "WATCH" if actual_symbol_share > symbol_p95 + config.shuffled_symbol_top_share_buffer else "PASS",
            "shuffled_symbol_control",
            f"Actual top-symbol share {_pct(actual_symbol_share)} versus shuffled p95 {_pct(symbol_p95)}.",
        )
    )
    actual_worst_hour = _finite_float(shuffled_event.get("actual_worst_entry_hour_return"))
    shuffled_hour_p05 = _finite_float(shuffled_event.get("p05_worst_entry_hour_return"))
    findings.append(
        _finding(
            "WATCH" if actual_worst_hour < shuffled_hour_p05 - config.shuffled_event_worst_hour_buffer else "PASS",
            "shuffled_event_control",
            f"Actual worst entry hour {_pct(actual_worst_hour)} versus shuffled p05 {_pct(shuffled_hour_p05)}.",
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
    heatmap_count = int(heatmaps.get("heatmap_count", 0) or 0)
    findings.append(
        _finding(
            "PASS" if heatmap_count > 0 else "WATCH",
            "parameter_heatmaps",
            f"Generated {heatmap_count} pairwise parameter heatmap CSVs.",
        )
    )
    stress_status = str(stress.get("status", "missing"))
    min_stress_return = _finite_float(stress.get("min_total_return"))
    worst_stress_dd = _finite_float(stress.get("worst_max_drawdown"))
    if stress_status == "missing":
        stress_level = "WATCH"
    elif min_stress_return < config.stress_return_fail or worst_stress_dd < config.stress_drawdown_fail:
        stress_level = "FAIL"
    elif worst_stress_dd < config.stress_drawdown_watch:
        stress_level = "WATCH"
    else:
        stress_level = "PASS"
    findings.append(
        _finding(
            stress_level,
            "stress_matrix",
            f"Filtered stress matrix rows={stress.get('rows', 0)}, min return {_pct(min_stress_return)}, worst drawdown {_pct(worst_stress_dd)}.",
        )
    )
    cfs_status = str(cost_funding_slippage.get("status", "missing"))
    cost_levels = cost_funding_slippage.get("cost_levels", [])
    stop_modes = cost_funding_slippage.get("stop_fill_modes", [])
    funding_modes = cost_funding_slippage.get("funding_modes", [])
    cfs_level = "PASS" if cfs_status == "present" and cost_levels and stop_modes and funding_modes else "WATCH"
    findings.append(
        _finding(
            cfs_level,
            "cost_funding_slippage",
            f"Cost levels={len(cost_levels)}, stop/slippage modes={len(stop_modes)}, funding modes={len(funding_modes)}.",
        )
    )
    positive_month_rate = _finite_float(regime.get("positive_month_rate"))
    findings.append(
        _finding(
            "PASS" if positive_month_rate >= config.min_positive_month_rate else "WATCH",
            "monthly_regime",
            f"Positive month rate is {_pct(positive_month_rate)}; worst month is `{regime.get('worst_month', '')}` at {_pct(regime.get('worst_month_return'))}.",
        )
    )
    drift_status = str(execution_drift.get("status", "missing"))
    p95_price_drift = _finite_float(execution_drift.get("p95_abs_entry_price_drift_bps"))
    p95_delay_drift = _finite_float(execution_drift.get("p95_abs_entry_delay_drift_minutes"))
    if drift_status == "present":
        if p95_price_drift >= config.execution_price_drift_fail_bps or p95_delay_drift >= config.execution_delay_drift_fail_minutes:
            drift_level = "FAIL"
        elif p95_price_drift >= config.execution_price_drift_watch_bps or p95_delay_drift >= config.execution_delay_drift_watch_minutes:
            drift_level = "WATCH"
        else:
            drift_level = "PASS"
    else:
        drift_level = "WATCH"
    findings.append(
        _finding(
            drift_level,
            "execution_drift",
            f"Execution drift status `{drift_status}`; p95 price drift {_num(p95_price_drift)} bps, p95 delay drift {_num(p95_delay_drift)} minutes.",
        )
    )
    crowding_status = str(crowding_model.get("status", "missing"))
    non_tradeable_rows = int(crowding_model.get("non_tradeable_rows", 0) or 0)
    crowding_level = "PASS" if crowding_status == "present" and non_tradeable_rows == 0 else "WATCH"
    findings.append(
        _finding(
            crowding_level,
            "crowding_model",
            f"Cross-sectional crowding model status `{crowding_status}` flags {non_tradeable_rows} non-tradeable rows.",
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
    return finite_float(value, default=0.0)


def _quantile(values: list[Any], q: float) -> float:
    clean = [_finite_float(value) for value in values if value is not None]
    return float(np.quantile(np.asarray(clean, dtype=float), q)) if clean else 0.0


def _date_ms(value: str) -> int:
    return date_ms(value)


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "pass"}
    return bool(value)


def _pct(value: Any) -> str:
    return pct(value, invalid="0.00%")


def _num(value: Any) -> str:
    return f"{_finite_float(value):.2f}"


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value).strip("_") or "axis"


def _comparison_family_label(comparison: dict[str, Any]) -> str:
    selected = [str(item) for item in comparison.get("selected_families", []) if item]
    if not selected:
        selected = [str(item) for item in comparison.get("inferred_families", []) if item]
    family = ", ".join(selected) if selected else "all rows"
    return f"{family}; rows {comparison.get('rows_after', 0)}/{comparison.get('rows_before', 0)}"
