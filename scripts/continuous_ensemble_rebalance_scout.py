#!/usr/bin/env python3
"""Component-level ensemble scout for adjacent continuous-fade signals."""
from __future__ import annotations

import argparse
import csv
import json
import os
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.continuous_events import _daily_pnl_metrics  # noqa: E402
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    combine_continuous_components,
    decompose_continuous_components,
    rebalance_rule_id,
)

VENUES = ("bybit", "binance")
BASE = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))


@dataclass(frozen=True)
class SourceSpec:
    root: Path
    cell: str


SOURCES = {
    "turn3p3": SourceSpec(BASE / "continuous_merged_signal_raw_2026-06-07", "merged_signal"),
    "turn3p4": SourceSpec(
        BASE / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn3pop4_crowd2",
    ),
    "turn4p3": SourceSpec(
        BASE / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop3_crowd2",
    ),
    "turn4p5": SourceSpec(
        BASE / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop5_crowd2",
    ),
    "age210tp12": SourceSpec(
        BASE / "independent_continuous_tp_hold_sweep_exploratory_2026-06-07",
        "age210_tp12_hold24_invvol10_crowd2",
    ),
    "age210tp14": SourceSpec(
        BASE / "independent_continuous_tp_hold_sweep_exploratory_2026-06-07",
        "age210_tp14_hold24_invvol10_crowd2",
    ),
}

SOURCE_ALIASES = {
    "turn3p3": "p3",
    "turn3p4": "p4",
    "turn4p3": "p4p3",
    "turn4p5": "p4p5",
    "age210tp12": "tp12",
    "age210tp14": "tp14",
}

PORTFOLIOS: dict[str, dict[str, float]] = {
    "single_turn3p3": {"turn3p3": 1.0},
    "single_turn3p4": {"turn3p4": 1.0},
    "single_turn4p3": {"turn4p3": 1.0},
    "single_turn4p5": {"turn4p5": 1.0},
    "single_age210tp12": {"age210tp12": 1.0},
    "single_age210tp14": {"age210tp14": 1.0},
    "core_70_p3_sat_30_p4p3": {"turn3p3": 0.70, "turn4p3": 0.30},
    "core_70_p3_sat_30_p4p5": {"turn3p3": 0.70, "turn4p5": 0.30},
    "core_50_p3_25_p4p3_25_p4p5": {"turn3p3": 0.50, "turn4p3": 0.25, "turn4p5": 0.25},
    "core_60_p3_20_p4p3_20_tp12": {"turn3p3": 0.60, "turn4p3": 0.20, "age210tp12": 0.20},
    "core_60_p3_20_p4p5_20_tp14": {"turn3p3": 0.60, "turn4p5": 0.20, "age210tp14": 0.20},
    "equal_p3_p4p3_p4p5": {"turn3p3": 1 / 3, "turn4p3": 1 / 3, "turn4p5": 1 / 3},
    "equal_p3_p4p3_p4p5_tp12": {
        "turn3p3": 0.25,
        "turn4p3": 0.25,
        "turn4p5": 0.25,
        "age210tp12": 0.25,
    },
    "equal_p3_p4p3_p4p5_tp14": {
        "turn3p3": 0.25,
        "turn4p3": 0.25,
        "turn4p5": 0.25,
        "age210tp14": 0.25,
    },
}


@dataclass(frozen=True)
class NamedRule:
    name: str
    rule: ContinuousRebalanceRule


RISK_RULES = (
    NamedRule(
        "base",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.025,
            max_scale=4.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
    NamedRule(
        "mid",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.035,
            max_scale=6.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
    NamedRule(
        "high",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.045,
            max_scale=8.0,
            drawdown_half_threshold=-0.05,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
    NamedRule(
        "tight",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.045,
            max_scale=8.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
)


def _source_paths(spec: SourceSpec, venue: str) -> tuple[Path, Path, Path]:
    cell = spec.root / venue / spec.cell
    return cell / "continuous_report.json", cell / "continuous_trades.csv", cell / "continuous_mtm_equity.csv"


def _load_source(spec: SourceSpec, venue: str) -> tuple[ContinuousRebalanceComponents, int, dict[str, Any]]:
    report_path, trades_path, mtm_path = _source_paths(spec, venue)
    if not report_path.exists() or not trades_path.exists() or not mtm_path.exists():
        raise FileNotFoundError(f"missing source artifacts: {report_path.parent}")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    config = payload["config"]
    trades = pl.read_csv(trades_path)
    mtm = pl.read_csv(mtm_path).select("ts_ms", "basket_return").sort("ts_ms")
    return decompose_continuous_components(trades, mtm, config), int(trades.height), config


def _combine_components(
    pieces: dict[str, ContinuousRebalanceComponents],
    weights: dict[str, float],
) -> ContinuousRebalanceComponents:
    # Canonical implementation moved to the package (combine_continuous_components);
    # this thin alias keeps the scout's public surface stable for existing drivers.
    return combine_continuous_components(pieces, weights)


def _apply_cost_multiplier(
    components: ContinuousRebalanceComponents,
    multiplier: float,
) -> ContinuousRebalanceComponents:
    if abs(multiplier - 1.0) <= 1e-12:
        return components
    if multiplier <= 0.0:
        raise SystemExit("--cost-multiplier must be positive")

    stressed_events: dict[int, list[tuple[float, float, float]]] = {}
    stressed_entry_cost_by_day: dict[int, float] = defaultdict(float)
    for day, events in components.cost_events.items():
        stressed: list[tuple[float, float, float]] = []
        for old_w, fixed_bps, impact_bps in events:
            fixed = fixed_bps * multiplier
            impact = impact_bps * multiplier
            stressed.append((old_w, fixed, impact))
            stressed_entry_cost_by_day[int(day)] -= old_w * (fixed + impact) / 10_000.0
        stressed_events[int(day)] = stressed

    raw_by_day = {
        day: (
            components.gross_by_day.get(day, 0.0)
            + components.funding_by_day.get(day, 0.0)
            + stressed_entry_cost_by_day.get(day, 0.0)
        )
        for day in components.days
    }
    return ContinuousRebalanceComponents(
        days=components.days,
        raw_by_day=raw_by_day,
        gross_by_day=components.gross_by_day,
        cost_events=stressed_events,
        funding_by_day=components.funding_by_day,
        active_gross_start=components.active_gross_start,
        impact_exponent=components.impact_exponent,
    )


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


def _parse_floats(raw: str | None) -> list[float]:
    if raw is None:
        return []
    return [float(part.strip()) for part in raw.split(",") if part.strip()]


def _parse_ints(raw: str | None) -> list[int]:
    if raw is None:
        return []
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _parse_source_list(raw: str | None) -> list[str]:
    if raw is None:
        return []
    names = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [name for name in names if name not in SOURCES]
    if unknown:
        raise SystemExit(f"unknown source id(s): {', '.join(unknown)}")
    return names


def _integer_compositions(total: int, parts: int) -> list[tuple[int, ...]]:
    if parts <= 1:
        return [(total,)]
    out: list[tuple[int, ...]] = []
    for value in range(total + 1):
        for tail in _integer_compositions(total - value, parts - 1):
            out.append((value, *tail))
    return out


def _weight_grid_portfolios(source_names: list[str], step: float) -> dict[str, dict[str, float]]:
    if not source_names:
        return {}
    if step <= 0.0 or step > 1.0:
        raise SystemExit("--weight-grid-step must be in (0, 1]")
    units = int(round(1.0 / step))
    if units <= 0 or abs(units * step - 1.0) > 1e-9:
        raise SystemExit("--weight-grid-step must evenly divide 1.0")

    out: dict[str, dict[str, float]] = {}
    for combo in _integer_compositions(units, len(source_names)):
        weights = {
            source: count / units
            for source, count in zip(source_names, combo)
            if count > 0
        }
        if not weights:
            continue
        suffix = "_".join(
            f"{SOURCE_ALIASES.get(source, source)}{count}"
            for source, count in zip(source_names, combo)
            if count > 0
        )
        out[f"grid_{suffix}"] = weights
    return out


def _parse_portfolio_specs(raw: str | None) -> dict[str, dict[str, float]]:
    if raw is None:
        return {}
    portfolios: dict[str, dict[str, float]] = {}
    for spec in [part.strip() for part in raw.split(";") if part.strip()]:
        if "=" not in spec:
            raise SystemExit("--portfolio-specs entries must be name=source:weight,...")
        name, body = spec.split("=", 1)
        name = name.strip()
        if not name:
            raise SystemExit("--portfolio-specs requires non-empty portfolio names")
        weights: dict[str, float] = {}
        for item in [part.strip() for part in body.split(",") if part.strip()]:
            if ":" not in item:
                raise SystemExit("--portfolio-specs weights must be source:weight")
            source, raw_weight = item.split(":", 1)
            source = source.strip()
            if source not in SOURCES:
                raise SystemExit(f"unknown source id in --portfolio-specs: {source}")
            weight = float(raw_weight)
            if weight < 0.0:
                raise SystemExit("--portfolio-specs weights must be non-negative")
            if weight > 0.0:
                weights[source] = weight
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-9:
            raise SystemExit(f"portfolio {name!r} weights sum to {total:g}, expected 1")
        portfolios[name] = weights
    return portfolios


def _risk_rules_from_args(args: argparse.Namespace) -> tuple[NamedRule, ...]:
    targets = _parse_floats(args.target_daily_vols)
    max_scales = _parse_floats(args.max_scales)
    dd_halves = _parse_floats(args.drawdown_halves)
    windows = _parse_ints(args.windows)
    if not any([targets, max_scales, dd_halves, windows]):
        return RISK_RULES
    if not targets or not max_scales or not dd_halves or not windows:
        raise SystemExit(
            "custom risk grid requires --windows, --target-daily-vols, "
            "--max-scales, and --drawdown-halves"
        )
    rules: list[NamedRule] = []
    for window in windows:
        for target in targets:
            for max_scale in max_scales:
                for dd_half in dd_halves:
                    rule = ContinuousRebalanceRule(
                        realized_vol_window_days=window,
                        target_daily_vol=target,
                        max_scale=max_scale,
                        drawdown_half_threshold=dd_half,
                        drawdown_zero_threshold=None,
                        resize_cost_bps=10.0,
                        strategy_momentum_window_days=0,
                    )
                    name = f"w{window}_tv{target:g}_max{max_scale:g}_ddh{dd_half:g}"
                    rules.append(NamedRule(name, rule))
    return tuple(rules)


def _mean(values: list[float]) -> float:
    finite = [float(v) for v in values if math.isfinite(float(v))]
    return sum(finite) / len(finite) if finite else 0.0


def _add_control_deltas(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    controls: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row["portfolio"] == "single_turn3p3":
            controls[(row["venue"], row["risk_rule"])] = row
    out: list[dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        control = controls.get((row["venue"], row["risk_rule"]))
        if control:
            copy["delta_return_vs_turn3p3"] = float(row["return"]) - float(control["return"])
            copy["delta_mar_vs_turn3p3"] = (
                float(row["mar"]) - float(control["mar"])
                if row["mar"] is not None and control["mar"] is not None else None
            )
        else:
            copy["delta_return_vs_turn3p3"] = None
            copy["delta_mar_vs_turn3p3"] = None
        out.append(copy)
    return out


def _pooled(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    frame = pl.DataFrame(rows, infer_schema_length=None)
    # NULL-MAR HAZARD (alpha-scripts-1): _daily_pnl_metrics returns mar=None on a
    # degenerate near-zero-drawdown path (abs(maxdd) <= 1e-9), and a missing source
    # artifact yields a venue row with no finite mar. Polars `.all()` and `.min()`
    # SKIP nulls, so a combo valid on only one venue ([7.0, None]) would otherwise
    # flag target_mar_both=True and sort by that lone venue's MAR — claiming a
    # cross-venue pass that never happened. Treat a missing MAR as a HARD FAIL of
    # the cross-venue gate: every venue must have a finite MAR (`is_not_null().all()`)
    # AND every venue must clear the bar. For the min_mar tiebreak, fill nulls with
    # -inf so a venue with no MAR can never win. `n_venues_with_mar` exposes coverage.
    # audit2: a SINGLE-venue run (e.g. --venues bybit) yields n_venues==1 groups
    # where `.all()` reduces each *_both gate to a one-venue test -> a false
    # cross-venue pass on one venue's numbers. Require BOTH venues present
    # (n_venues == len(VENUES)) before any *_both can be True; use a true row
    # count (pl.len()) rather than mar.len(), and the VENUES constant (not 2).
    both_venues = pl.len() == len(VENUES)
    return (
        frame.group_by(["portfolio", "risk_rule"])
        .agg(
            pl.col("component_trades").sum().alias("component_trades"),
            pl.col("return").min().alias("min_return"),
            pl.col("return").mean().alias("mean_return"),
            pl.col("mar").fill_null(float("-inf")).min().alias("min_mar"),
            pl.col("mar").mean().alias("mean_mar"),
            pl.col("mar").is_not_null().sum().alias("n_venues_with_mar"),
            pl.len().alias("n_venues"),
            pl.col("max_drawdown").min().alias("worst_dd"),
            pl.col("worst_day_return").min().alias("worst_day"),
            pl.col("delta_return_vs_turn3p3").mean().alias("mean_delta_return_vs_turn3p3"),
            pl.col("delta_mar_vs_turn3p3").mean().alias("mean_delta_mar_vs_turn3p3"),
            (both_venues & (pl.col("return") >= 1.20).all()).alias("target_return_both"),
            (
                both_venues
                & pl.col("mar").is_not_null().all()
                & (pl.col("mar") >= 6.0).all()
            ).alias("target_mar_both"),
            (both_venues & (pl.col("max_drawdown") >= -0.12).all()).alias("target_dd_ok_both"),
        )
        .sort(
            [
                "target_return_both",
                "target_mar_both",
                "target_dd_ok_both",
                "min_mar",
                "mean_return",
            ],
            descending=[True, True, True, True, True],
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--portfolio-filter", help="Comma-separated subset of portfolio ids.")
    parser.add_argument("--only-weight-grid", action="store_true", help="Skip built-in portfolios.")
    parser.add_argument("--weight-grid-sources", help="Comma-separated source ids for simplex weights.")
    parser.add_argument("--weight-grid-step", type=float, default=0.0, help="Simplex step, e.g. 0.1.")
    parser.add_argument("--portfolio-specs", help="Semicolon-separated name=source:weight,... specs.")
    parser.add_argument("--only-portfolio-specs", action="store_true", help="Skip built-ins and grids.")
    parser.add_argument("--cost-multiplier", type=float, default=1.0, help="Multiplier for entry cost events.")
    parser.add_argument("--windows", help="Custom risk grid realized-vol windows.")
    parser.add_argument("--target-daily-vols", help="Custom risk grid target daily vol values.")
    parser.add_argument("--max-scales", help="Custom risk grid max scale values.")
    parser.add_argument("--drawdown-halves", help="Custom risk grid drawdown half thresholds.")
    parser.add_argument("--write-ledgers", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    unknown = [v for v in venues if v not in VENUES]
    if unknown:
        raise SystemExit(f"unknown venue(s): {', '.join(unknown)}")
    args.out.mkdir(parents=True, exist_ok=True)
    risk_rules = _risk_rules_from_args(args)
    if args.only_weight_grid and args.only_portfolio_specs:
        raise SystemExit("--only-weight-grid and --only-portfolio-specs are mutually exclusive")
    selected_portfolios = {} if (args.only_weight_grid or args.only_portfolio_specs) else PORTFOLIOS
    if args.portfolio_filter and not (args.only_weight_grid or args.only_portfolio_specs):
        wanted = [part.strip() for part in args.portfolio_filter.split(",") if part.strip()]
        missing = [name for name in wanted if name not in PORTFOLIOS]
        if missing:
            raise SystemExit(f"unknown portfolio id(s): {', '.join(missing)}")
        selected_portfolios = {name: PORTFOLIOS[name] for name in wanted}
    elif args.portfolio_filter and (args.only_weight_grid or args.only_portfolio_specs):
        raise SystemExit("--portfolio-filter cannot be combined with only-* options")
    grid_sources = _parse_source_list(args.weight_grid_sources)
    if args.only_weight_grid and not grid_sources:
        raise SystemExit("--only-weight-grid requires --weight-grid-sources")
    if grid_sources:
        if args.weight_grid_step <= 0.0:
            raise SystemExit("--weight-grid-sources requires --weight-grid-step")
        selected_portfolios = {
            **selected_portfolios,
            **_weight_grid_portfolios(grid_sources, args.weight_grid_step),
        }
    manual_portfolios = _parse_portfolio_specs(args.portfolio_specs)
    if args.only_portfolio_specs and not manual_portfolios:
        raise SystemExit("--only-portfolio-specs requires --portfolio-specs")
    selected_portfolios = {**selected_portfolios, **manual_portfolios}

    rows: list[dict[str, Any]] = []
    source_counts: dict[tuple[str, str], int] = {}
    source_components: dict[str, dict[str, ContinuousRebalanceComponents]] = {}

    for venue in venues:
        source_components[venue] = {}
        for name, spec in SOURCES.items():
            comp, count, _ = _load_source(spec, venue)
            source_components[venue][name] = comp
            source_counts[(venue, name)] = count

    for venue in venues:
        for portfolio, weights in selected_portfolios.items():
            combined = _combine_components(source_components[venue], weights)
            combined = _apply_cost_multiplier(combined, args.cost_multiplier)
            component_trades = sum(int(source_counts[(venue, src)]) for src in weights)
            weights_label = ",".join(f"{src}:{weight:g}" for src, weight in weights.items())
            for named_rule in risk_rules:
                scaled = apply_rebalance_rule(combined, named_rule.rule)
                metrics = _daily_pnl_metrics(scaled.select("ts_ms", "equity", "drawdown", "basket_return"))
                scales = [float(x) for x in scaled["scale"].to_list()] if not scaled.is_empty() else []
                if args.write_ledgers:
                    out_dir = args.out / venue / portfolio / named_rule.name
                    out_dir.mkdir(parents=True, exist_ok=True)
                    scaled.write_csv(out_dir / "equity.csv")
                rows.append(
                    {
                        "venue": venue,
                        "portfolio": portfolio,
                        "weights": weights_label,
                        "risk_rule": named_rule.name,
                        "rule_id": rebalance_rule_id(named_rule.rule),
                        "component_trades": component_trades,
                        "return": metrics.get("total_return"),
                        "mar": metrics.get("mar"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "worst_day_return": metrics.get("worst_day_return"),
                        "avg_scale": _mean(scales),
                        "max_scale_observed": max(scales) if scales else 0.0,
                        "gross_return": float(scaled["gross_return"].sum()) if not scaled.is_empty() else 0.0,
                        "entry_cost_return": (
                            float(scaled["entry_cost_return"].sum()) if not scaled.is_empty() else 0.0
                        ),
                        "funding_return": float(scaled["funding_return"].sum()) if not scaled.is_empty() else 0.0,
                        "resize_cost_return": (
                            float(scaled["resize_cost_return"].sum()) if not scaled.is_empty() else 0.0
                        ),
                    }
                )

    rows = _add_control_deltas(rows)
    _write_csv(args.out / "summary.csv", rows)
    pooled = _pooled(rows)
    pooled.write_csv(args.out / "pooled.csv")
    # IN-SAMPLE SELECTION HAZARD (alpha-scripts-2): _pooled sorts the ENTIRE searched
    # space (weight-grid simplex x risk-rule grid) best-first by FULL-SAMPLE pooled
    # metrics — there is no train/holdout, no era split, and no multiple-testing
    # correction here. The top pooled.csv row is therefore in-sample grid mining
    # (house-rules #17/#18/#19); its forward performance is expected to be overstated.
    # Stamp the grid cardinality and an explicit machine-readable flag so the output
    # cannot be silently cited as candidate/promotion-grade evidence. The demo-forward
    # arbiter (not this ranking) remains the gate; split-stability/bootstrap validation
    # must be run separately and pre-registered before any promotion claim.
    grid_cardinality = len(selected_portfolios) * len(risk_rules)
    selection_caveat = (
        "Top pooled.csv row is the FULL-SAMPLE grid maximizer over "
        f"{grid_cardinality} (portfolio x risk_rule) cells with NO holdout / era split / "
        "multiple-testing correction. In-sample selection only — NOT candidate-grade. "
        "Run split-stability + block-bootstrap and pre-register before any promotion claim."
    )
    receipt = {
        "sources": {
            name: {"root": str(spec.root), "cell": spec.cell}
            for name, spec in SOURCES.items()
        },
        "portfolios": selected_portfolios,
        "weight_grid": {
            "sources": grid_sources,
            "step": args.weight_grid_step,
        },
        "manual_portfolios": manual_portfolios,
        "cost_multiplier": args.cost_multiplier,
        "risk_rules": [{"name": item.name, "rule_id": rebalance_rule_id(item.rule)} for item in risk_rules],
        "selection_is_in_sample": True,
        "n_portfolios": len(selected_portfolios),
        "n_risk_rules": len(risk_rules),
        "grid_cardinality": grid_cardinality,
        "selection_caveat": selection_caveat,
        "outputs": {
            "summary": str(args.out / "summary.csv"),
            "pooled": str(args.out / "pooled.csv"),
        },
    }
    (args.out / "run_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(f"summary={args.out / 'summary.csv'}")
    print(f"pooled={args.out / 'pooled.csv'}")
    print(f"WARNING: {selection_caveat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
