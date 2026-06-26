"""Pre-registered daily-vol rebalance A/B for the continuous TP12 object.

Research-only. This script rebuilds the same component object used by the
continuous forward replay, then swaps only the daily rebalance rule via
``build_full_ledger(..., rebalance_rule=...)``. It does not change the frozen
forward config hash, does not submit orders, and does not make a promotion claim.

Run:
    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/Scripts/python.exe \
        scripts/continuous_daily_rebalance_ab.py \
        --venues bybit,binance \
        --out reports/continuous_daily_rebalance_ab_2026-06-25
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import random
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_forward_replay_orchestrator as orch  # noqa: E402
import rebuild_continuous_component_ledgers as rb  # noqa: E402
from liquidity_migration.continuous_component_sources import (  # noqa: E402
    ContinuousComponentSource,
    load_continuous_component_source,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    FROZEN_FORWARD_CONFIG,
    build_full_ledger,
    frozen_config_hash,
    frozen_hedge_regime,
)
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    run_continuous_event_research,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceRule,
    rebalance_rule_id,
)
from liquidity_migration.continuous_regime import btcvol_intensity_series  # noqa: E402
from liquidity_migration.trade_lifecycle import annualized_sharpe  # noqa: E402

ANN_DAYS = 365.25
MS_PER_DAY = 86_400_000
CONTROL = "off_current"
PRIMARY_ON = "on_045_max4_legacy"
RUN_LABEL = "RESEARCH_ONLY_CONTINUOUS_DAILY_VOL_REBALANCE_AB"
COMPONENT_TP = 0.12
TP12_CELL_OVERRIDES: dict[str, dict[str, Any]] = {
    "merged_signal": dict(entry_event_trigger="turn3_pop3", age_days_min=240, take_profit_pct=COMPONENT_TP),
    "age240_turn4pop3_crowd2": dict(entry_event_trigger="turn4_pop3", age_days_min=240, take_profit_pct=COMPONENT_TP),
    "age240_turn4pop5_crowd2": dict(entry_event_trigger="turn4_pop5", age_days_min=240, take_profit_pct=COMPONENT_TP),
}


def arm_specs() -> dict[str, ContinuousRebalanceRule]:
    """Fixed preregistered arm set.

    Only ``PRIMARY_ON`` can support a switch-back-on decision. The other ON
    variants are diagnostics for mechanism and future preregistration.
    """
    base = ContinuousRebalanceRule(**FROZEN_FORWARD_CONFIG["rebalance"])
    return {
        CONTROL: replace(base, enabled=False),
        PRIMARY_ON: replace(
            base,
            enabled=True,
            realized_vol_window_days=90,
            target_daily_vol=0.045,
            max_scale=4.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            strategy_momentum_window_days=0,
        ),
        "on_045_max4_volonly": replace(
            base,
            enabled=True,
            realized_vol_window_days=90,
            target_daily_vol=0.045,
            max_scale=4.0,
            drawdown_half_threshold=None,
            drawdown_zero_threshold=None,
            strategy_momentum_window_days=0,
        ),
        "on_035_max3_balanced": replace(
            base,
            enabled=True,
            realized_vol_window_days=90,
            target_daily_vol=0.035,
            max_scale=3.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            strategy_momentum_window_days=0,
        ),
        "on_025_max2_defensive": replace(
            base,
            enabled=True,
            realized_vol_window_days=90,
            target_daily_vol=0.025,
            max_scale=2.0,
            drawdown_half_threshold=-0.03,
            drawdown_zero_threshold=None,
            strategy_momentum_window_days=0,
        ),
        "on_045_max4_mom90_quarter": replace(
            base,
            enabled=True,
            realized_vol_window_days=90,
            target_daily_vol=0.045,
            max_scale=4.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            strategy_momentum_window_days=90,
            strategy_momentum_min_return=0.0,
            strategy_momentum_scale_when_below=0.25,
        ),
    }


def _date_str(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date().isoformat()


def _month_str(ts_ms: int) -> str:
    d = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date()
    return f"{d.year:04d}-{d.month:02d}"


def _calendar_series(df: pl.DataFrame) -> tuple[np.ndarray, list[str]]:
    if df.is_empty():
        return np.array([], dtype=float), []
    d = df.sort("ts_ms")
    ts = np.asarray(d["ts_ms"].to_list(), dtype=np.int64)
    rets = np.asarray(d["basket_return"].to_list(), dtype=float)
    first = int(ts[0])
    last = int(ts[-1])
    n = int((last - first) // MS_PER_DAY) + 1
    series = np.zeros(n, dtype=float)
    idx = ((ts - first) // MS_PER_DAY).astype(int)
    series[idx] = rets
    dates = [_date_str(first + i * MS_PER_DAY) for i in range(n)]
    return series, dates


def _compound(rets: list[float] | np.ndarray) -> float:
    arr = np.asarray(rets, dtype=float)
    if arr.size == 0:
        return 0.0
    return float(np.prod(1.0 + arr) - 1.0)


def _max_drawdown(series: np.ndarray) -> float:
    if series.size == 0:
        return 0.0
    eq = np.cumprod(1.0 + series)
    return float((eq / np.maximum.accumulate(eq) - 1.0).min())


def _annualized(total: float, n_days: int) -> float:
    if n_days <= 0:
        return 0.0
    growth = 1.0 + total
    if growth <= 0.0:
        return -1.0
    return float(growth ** (ANN_DAYS / n_days) - 1.0)


def _mar(total: float, max_dd: float, n_days: int) -> float | None:
    if max_dd >= -1e-12:
        return None
    return _annualized(total, n_days) / abs(max_dd)


def _worst_rolling(series: np.ndarray, window: int) -> float | None:
    if series.size < window:
        return None
    worst = min(_compound(series[i : i + window]) for i in range(series.size - window + 1))
    return float(worst)


def monthly_returns(df: pl.DataFrame) -> list[dict[str, Any]]:
    series, dates = _calendar_series(df)
    by_month: dict[str, list[float]] = {}
    for date, ret in zip(dates, series, strict=True):
        by_month.setdefault(date[:7], []).append(float(ret))
    return [
        {"month": month, "strategy_return": _compound(vals)}
        for month, vals in sorted(by_month.items())
    ]


def summary_metrics(df: pl.DataFrame) -> dict[str, Any]:
    series, dates = _calendar_series(df)
    total = _compound(series)
    dd = _max_drawdown(series)
    mar = _mar(total, dd, int(series.size))
    scale = df["scale"].to_numpy() if "scale" in df.columns and df.height else np.array([])
    return {
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "calendar_days": int(series.size),
        "ledger_days": int(df.height),
        "total_return": total,
        "annualized_return": _annualized(total, int(series.size)),
        "max_drawdown": dd,
        "mar": mar,
        "sharpe": annualized_sharpe(series, ann_days=ANN_DAYS) if series.size > 1 else 0.0,
        "worst_90d": _worst_rolling(series, 90),
        "worst_day": float(series.min()) if series.size else 0.0,
        "best_day": float(series.max()) if series.size else 0.0,
        "scale_mean": float(scale.mean()) if scale.size else 0.0,
        "scale_median": float(np.median(scale)) if scale.size else 0.0,
        "scale_p95": float(np.quantile(scale, 0.95)) if scale.size else 0.0,
        "scale_max": float(scale.max()) if scale.size else 0.0,
        "days_scale_lt1": int((scale < 1.0 - 1e-12).sum()) if scale.size else 0,
        "days_scale_gt1": int((scale > 1.0 + 1e-12).sum()) if scale.size else 0,
        "days_scale_gt2": int((scale > 2.0 + 1e-12).sum()) if scale.size else 0,
        "resize_cost_sum": float(df["resize_cost_return"].sum()) if "resize_cost_return" in df.columns else 0.0,
        "hedge_cost_sum": float(df["hedge_cost_return"].sum()) if "hedge_cost_return" in df.columns else 0.0,
    }


def _bootstrap_delta(
    base_monthly: list[float],
    arm_monthly: list[float],
    *,
    seed: int,
    n_boot: int,
    block: int = 3,
) -> dict[str, float | None]:
    n = len(base_monthly)
    if n == 0 or n != len(arm_monthly) or n_boot <= 0:
        return {"boot_p5": None, "boot_p50": None, "boot_p95": None, "boot_p_delta_gt0": None}
    rng = random.Random(seed)
    deltas: list[float] = []
    starts = list(range(n))
    for _ in range(n_boot):
        b: list[float] = []
        a: list[float] = []
        while len(b) < n:
            s = rng.choice(starts)
            for k in range(block):
                idx = (s + k) % n
                b.append(base_monthly[idx])
                a.append(arm_monthly[idx])
                if len(b) == n:
                    break
        deltas.append(_compound(a) - _compound(b))
    arr = np.asarray(deltas, dtype=float)
    return {
        "boot_p5": float(np.quantile(arr, 0.05)),
        "boot_p50": float(np.quantile(arr, 0.50)),
        "boot_p95": float(np.quantile(arr, 0.95)),
        "boot_p_delta_gt0": float((arr > 0.0).mean()),
    }


def comparison_metrics(
    control_monthly: list[dict[str, Any]],
    arm_monthly: list[dict[str, Any]],
    control_summary: dict[str, Any],
    arm_summary: dict[str, Any],
    *,
    seed: int,
    n_boot: int,
) -> dict[str, Any]:
    base_by_month = {str(row["month"]): float(row["strategy_return"]) for row in control_monthly}
    arm_by_month = {str(row["month"]): float(row["strategy_return"]) for row in arm_monthly}
    months = sorted(set(base_by_month) & set(arm_by_month))
    base_r = [base_by_month[m] for m in months]
    arm_r = [arm_by_month[m] for m in months]
    deltas = [a - b for a, b in zip(arm_r, base_r, strict=True)]
    positive = [d for d in deltas if d > 0.0]
    top_pos_share = max(positive) / sum(positive) if positive and sum(positive) > 0.0 else None
    lomo = []
    for i in range(len(months)):
        lomo.append(_compound(arm_r[:i] + arm_r[i + 1 :]) - _compound(base_r[:i] + base_r[i + 1 :]))
    full_delta = float(arm_summary["total_return"] - control_summary["total_return"])
    lomo_min = min(lomo) if lomo else None
    thirds: list[float] = []
    if len(months) >= 3:
        k = len(months) // 3
        bounds = [(0, k), (k, 2 * k), (2 * k, len(months))]
        thirds = [_compound(arm_r[a:b]) - _compound(base_r[a:b]) for a, b in bounds]
    out: dict[str, Any] = {
        "months": len(months),
        "total_return_delta": full_delta,
        "annualized_return_delta": float(arm_summary["annualized_return"] - control_summary["annualized_return"]),
        "max_drawdown_delta": float(arm_summary["max_drawdown"] - control_summary["max_drawdown"]),
        "mar_delta": None
        if arm_summary.get("mar") is None or control_summary.get("mar") is None
        else float(arm_summary["mar"] - control_summary["mar"]),
        "worst_90d_delta": None
        if arm_summary.get("worst_90d") is None or control_summary.get("worst_90d") is None
        else float(arm_summary["worst_90d"] - control_summary["worst_90d"]),
        "top_positive_month_delta_share": top_pos_share,
        "lomo_min_total_delta": lomo_min,
        "lomo_flips_positive_edge": bool(full_delta > 0.0 and lomo_min is not None and lomo_min <= 0.0),
        "third1_delta": thirds[0] if len(thirds) > 0 else None,
        "third2_delta": thirds[1] if len(thirds) > 1 else None,
        "third3_delta": thirds[2] if len(thirds) > 2 else None,
    }
    out.update(_bootstrap_delta(base_r, arm_r, seed=seed, n_boot=n_boot))
    return out


def primary_acceptance(comparisons: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> tuple[str, list[str]]:
    primary = [row for row in comparisons if row["arm"] == PRIMARY_ON]
    if not primary:
        return "REJECT_KEEP_DAILY_REBALANCE_DISABLED", ["primary comparison missing"]
    reasons: list[str] = []
    summary_lookup = {(row["venue"], row["arm"]): row for row in summaries}
    for row in primary:
        venue = row["venue"]
        ctrl = summary_lookup[(venue, CONTROL)]
        arm = summary_lookup[(venue, PRIMARY_ON)]
        ctrl_mar = ctrl.get("mar")
        arm_mar = arm.get("mar")
        if ctrl_mar is None or arm_mar is None or not math.isfinite(float(ctrl_mar)) or not math.isfinite(float(arm_mar)):
            reasons.append(f"{venue}: MAR unavailable")
        elif float(arm_mar) < float(ctrl_mar) * 1.10:
            reasons.append(f"{venue}: MAR did not improve by 10%")
        if float(row["max_drawdown_delta"]) < -0.01:
            reasons.append(f"{venue}: max drawdown worsened beyond 1pp tolerance")
        if row.get("worst_90d_delta") is not None and float(row["worst_90d_delta"]) < -0.01:
            reasons.append(f"{venue}: worst 90d worsened beyond 1pp tolerance")
        total_delta = float(row["total_return_delta"])
        loose_loss = max(abs(float(ctrl["total_return"])) * 0.10, 0.05)
        if total_delta < -loose_loss:
            reasons.append(f"{venue}: total return loss exceeded tolerance")
        if bool(row["lomo_flips_positive_edge"]):
            reasons.append(f"{venue}: positive edge is single-month fragile")
    if reasons:
        return "REJECT_KEEP_DAILY_REBALANCE_DISABLED", reasons
    return "ACCEPT_REENABLE_DAILY_VOL_REBALANCE_RESEARCH_ONLY", ["primary legacy ON arm passed both venues"]


def _csv_write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: (
                        ""
                        if row.get(field) is None
                        else json.dumps(row.get(field), sort_keys=True)
                        if isinstance(row.get(field), (dict, list, tuple))
                        else row.get(field)
                    )
                    for field in fields
                }
            )


def _fmt_pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{float(value) * 100:.{digits}f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{float(value):.{digits}f}"


def _report(
    out_dir: Path,
    summaries: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    verdict: str,
    reasons: list[str],
    manifest: dict[str, Any],
) -> str:
    lines: list[str] = [
        "# Continuous Daily Vol Rebalance A/B",
        "",
        f"Run label: `{RUN_LABEL}`",
        f"Verdict: **{verdict}**",
        "",
        "This is research evidence only. It does not deploy, promote, or approve real-money trading.",
        "",
        "## Decision Notes",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons)
    limits: list[str] = [
        "BTC-risk entry-size overlay is not re-simulated inside the component ledgers; this isolates the daily rebalance layer on the TP12 component object.",
    ]
    for row in summaries:
        if row["arm"] == CONTROL and int(row["calendar_days"]) < 180:
            limits.append(
                f"{row['venue']}: current TP12 component rebuild spans only "
                f"{row['calendar_days']} calendar days / {row['ledger_days']} ledger days "
                f"({row['start']} to {row['end']})."
            )
    if limits:
        lines += [
            "",
            "## Limitations",
            "",
        ]
        lines.extend(f"- {limit}" for limit in limits)
    lines += [
        "",
        "## Summary",
        "",
        "| Venue | Arm | Total | Max DD | MAR | Sharpe | Worst 90d | Mean Scale | P95 Scale | Max Scale |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summaries:
        lines.append(
            "| {venue} | {arm} | {total} | {dd} | {mar} | {sharpe} | {worst90} | {mean_scale} | {p95} | {max_scale} |".format(
                venue=row["venue"],
                arm=row["arm"],
                total=_fmt_pct(row["total_return"]),
                dd=_fmt_pct(row["max_drawdown"]),
                mar=_fmt_num(row.get("mar")),
                sharpe=_fmt_num(row["sharpe"]),
                worst90=_fmt_pct(row.get("worst_90d")),
                mean_scale=_fmt_num(row["scale_mean"]),
                p95=_fmt_num(row["scale_p95"]),
                max_scale=_fmt_num(row["scale_max"]),
            )
        )
    lines += [
        "",
        "## Versus Off Control",
        "",
        "| Venue | Arm | Total Delta | DD Delta | MAR Delta | Worst 90d Delta | Boot P(delta>0) | LOMO Min Delta |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in comparisons:
        lines.append(
            "| {venue} | {arm} | {total} | {dd} | {mar} | {worst90} | {boot} | {lomo} |".format(
                venue=row["venue"],
                arm=row["arm"],
                total=_fmt_pct(row["total_return_delta"]),
                dd=_fmt_pct(row["max_drawdown_delta"]),
                mar=_fmt_num(row.get("mar_delta")),
                worst90=_fmt_pct(row.get("worst_90d_delta")),
                boot=_fmt_num(row.get("boot_p_delta_gt0")),
                lomo=_fmt_pct(row.get("lomo_min_total_delta")),
            )
        )
    lines += [
        "",
        "## Method",
        "",
        f"- Frozen config hash: `{manifest['frozen_config_hash']}`",
        f"- Component take-profit: `{manifest['component_take_profit_pct']}`",
        f"- Hedge regime: `{json.dumps(manifest['hedge_regime'], sort_keys=True)}`",
        f"- Output directory: `{out_dir}`",
        "- TP12 components are rebuilt/reused in this run's component cache.",
        "- Every arm uses the same component ledgers, same hedge inputs, and same funding inputs per venue.",
        "",
        "## Artifacts",
        "",
        "- `summary.csv`",
        "- `comparisons.csv`",
        "- `monthly.csv`",
        "- `manifest.json`",
        "- `ledgers/*.csv`",
        "",
    ]
    return "\n".join(lines)


def ensure_tp12_cells(venue: str, work: Path, end_date: str) -> None:
    """Build/reuse the current local-target TP12 components for one venue."""
    for cell, overrides in TP12_CELL_OVERRIDES.items():
        out_dir = work / venue / cell
        report = out_dir / "continuous_report.json"
        needed_csvs = (
            out_dir / "continuous_trades.csv",
            out_dir / "continuous_mtm_equity.csv",
            out_dir / "continuous_equity.csv",
        )
        if report.exists() and all(p.exists() for p in needed_csvs):
            cfg = json.loads(report.read_text(encoding="utf-8")).get("config", {})
            if cfg.get("end_date") == end_date and float(cfg.get("take_profit_pct") or 0.0) == COMPONENT_TP:
                print(f"[{venue}] {cell} TP12: current to {end_date}, skip", flush=True)
                continue
        t0 = time.time()
        cfg = ContinuousEventConfig(**{**rb.COMMON, **overrides, "end_date": end_date})
        payload = run_continuous_event_research(orch.ROOTS[venue], config=cfg, report_dir=out_dir)
        print(
            f"[{venue}] {cell} TP12: {payload['n_trades']} trades to {end_date} "
            f"({time.time() - t0:.0f}s)",
            flush=True,
        )


def _load_venue_inputs(venue: str, component_work: Path) -> tuple[dict[str, Any], dict[int, float], dict[int, float], dict[int, float], dict[int, float], dict[int, float] | None, str]:
    end_date = orch.data_end_day(orch.ROOTS[venue])
    ensure_tp12_cells(venue, component_work, end_date)
    pieces = {
        name: load_continuous_component_source(ContinuousComponentSource(component_work, cell), venue)[0]
        for name, cell in orch.NAME_TO_CELL.items()
    }
    all_days = sorted({d for p in pieces.values() for d in p.days})
    rets, fund = orch.btc_inputs(venue, all_days, "BTCUSDT")
    rets2, fund2 = orch.btc_inputs(venue, all_days, "ETHUSDT")
    regime = frozen_hedge_regime()
    intensity = (
        btcvol_intensity_series(all_days, rets, regime["lam"], regime["vol_window"], regime["pct_window"])
        if regime
        else None
    )
    return pieces, rets, fund, rets2, fund2, intensity, end_date


def run(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir = out_dir / "ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)

    component_work = Path(args.component_work).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    arms = arm_specs()
    summaries: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    all_monthly: list[dict[str, Any]] = []
    data_end: dict[str, str] = {}

    for venue in venues:
        pieces, rets, fund, rets2, fund2, intensity, end_date = _load_venue_inputs(venue, component_work)
        data_end[venue] = end_date
        venue_monthly: dict[str, list[dict[str, Any]]] = {}
        venue_summary: dict[str, dict[str, Any]] = {}
        for arm, rule in arms.items():
            ledger = build_full_ledger(
                pieces,
                rets,
                fund,
                rets2,
                fund2,
                hedge_intensity=intensity,
                rebalance_rule=rule,
            )
            ledger.write_csv(ledger_dir / f"{venue}_{arm}.csv")
            metrics = {
                "venue": venue,
                "arm": arm,
                "rule_id": rebalance_rule_id(rule),
                "rule": asdict(rule),
                **summary_metrics(ledger),
            }
            summaries.append(metrics)
            venue_summary[arm] = metrics
            months = [{"venue": venue, "arm": arm, **row} for row in monthly_returns(ledger)]
            all_monthly.extend(months)
            venue_monthly[arm] = months
        for arm in arms:
            if arm == CONTROL:
                continue
            comp = comparison_metrics(
                venue_monthly[CONTROL],
                venue_monthly[arm],
                venue_summary[CONTROL],
                venue_summary[arm],
                seed=int(args.seed),
                n_boot=int(args.n_boot),
            )
            comparisons.append({"venue": venue, "arm": arm, **comp})

    verdict, reasons = primary_acceptance(comparisons, summaries)
    manifest: dict[str, Any] = {
        "run_label": RUN_LABEL,
        "created_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "venues": venues,
        "data_end": data_end,
        "component_work": str(component_work),
        "component_take_profit_pct": COMPONENT_TP,
        "frozen_config_hash": frozen_config_hash(),
        "frozen_config": FROZEN_FORWARD_CONFIG,
        "hedge_regime": frozen_hedge_regime(),
        "btc_risk_entry_overlay_included": False,
        "control": CONTROL,
        "primary_on": PRIMARY_ON,
        "arms": {name: asdict(rule) for name, rule in arms.items()},
        "verdict": verdict,
        "reasons": reasons,
    }

    _csv_write(out_dir / "summary.csv", summaries)
    _csv_write(out_dir / "comparisons.csv", comparisons)
    _csv_write(out_dir / "monthly.csv", all_monthly)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "report.md").write_text(
        _report(out_dir, summaries, comparisons, verdict, reasons, manifest),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "reasons": reasons, "out": str(out_dir)}, indent=2))
    return {"manifest": manifest, "summaries": summaries, "comparisons": comparisons}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--out", default="reports/continuous_daily_rebalance_ab_2026-06-25")
    ap.add_argument("--component-work", default="")
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    if not args.component_work:
        args.component_work = str(Path(args.out).expanduser() / "_tp12_components")
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
