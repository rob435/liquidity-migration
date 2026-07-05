#!/usr/bin/env python3
"""Run the 2026-07-04 BTC month-regime preregistration arms.

This is a dispatcher, not a generic sweep framework. It runs the exact arms in
docs/preregistration/btc-month-regime-2026-07-04.md and writes a compact summary
under the chosen output root.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.config import load_config
from liquidity_migration.continuous_events import (
    BTC_EXACT_MONTH_DAYS,
    BTC_TREND_MODE_DAILY_PRIOR,
    BTC_TREND_MODE_HOURLY_30D,
    BTC_TREND_MODE_HOURLY_EXACT_MONTH,
    BTC_TREND_MODE_SMART_MONTH,
    ContinuousEventConfig,
)
from liquidity_migration.long_native import (
    BTC_MONTH_REGIME_MODE_DAILY_30D,
    BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH,
    BTC_MONTH_REGIME_MODE_SMART_MONTH,
    build_long_research_inputs,
    run_long_native_research,
)
from liquidity_migration.promoted import long_profile
from scripts.continuous_deployed_equity_refresh import run_venue as run_continuous_venue

SHARED = Path.home() / "SHARED_DATA"
ROOTS = {
    "bybit": SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}

CONTINUOUS_ARMS = {
    "control_daily_prior": BTC_TREND_MODE_DAILY_PRIOR,
    "hourly_30d": BTC_TREND_MODE_HOURLY_30D,
    "hourly_exact_month": BTC_TREND_MODE_HOURLY_EXACT_MONTH,
    "smart_month": BTC_TREND_MODE_SMART_MONTH,
}

LONG_ARMS = {
    "control_off": ("off", BTC_MONTH_REGIME_MODE_DAILY_30D),
    "daily_30d_uptrend": ("uptrend", BTC_MONTH_REGIME_MODE_DAILY_30D),
    "hourly_exact_month_uptrend": ("uptrend", BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH),
    "smart_month_uptrend": ("uptrend", BTC_MONTH_REGIME_MODE_SMART_MONTH),
}


def _continuous_transform(
    *,
    mode: str,
    month_days: float,
    smart_tolerance: float,
) -> Callable[[ContinuousEventConfig], ContinuousEventConfig]:
    def transform(cfg: ContinuousEventConfig) -> ContinuousEventConfig:
        return replace(
            cfg,
            btc_trend_gate="uptrend",
            btc_trend_mode=mode,
            btc_trend_lookback_days=30,
            btc_trend_month_days=float(month_days),
            btc_trend_smart_tolerance=float(smart_tolerance),
        )

    return transform


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _continuous_row(*, arm: str, venue: str, run_root: Path, elapsed: float) -> dict[str, Any]:
    payload = _load_json(run_root / "continuous" / arm / venue / "continuous_equity_summary.json")
    stats = payload.get("stats") or {}
    return {
        "sleeve": "continuous",
        "arm": arm,
        "venue": venue,
        "elapsed_sec": round(elapsed, 1),
        "total_return_pct": stats.get("total_return_pct"),
        "max_drawdown_pct": stats.get("max_drawdown_pct"),
        "mar": stats.get("mar"),
        "sharpe_daily_ann": stats.get("sharpe_daily_ann"),
        "worst_day_pct": stats.get("worst_day_pct"),
        "funding_modes": payload.get("funding_modes"),
        "report": str(run_root / "continuous" / arm / venue / "continuous_equity_summary.json"),
    }


def _long_row(*, arm: str, venue: str, run_dir: Path, payload: dict[str, Any], elapsed: float) -> dict[str, Any]:
    summary = payload.get("summary") or {}
    return {
        "sleeve": "long",
        "arm": arm,
        "venue": venue,
        "elapsed_sec": round(elapsed, 1),
        "total_return_pct": None
        if summary.get("total_return") is None else float(summary.get("total_return", 0.0)) * 100.0,
        "max_drawdown_pct": None
        if summary.get("max_drawdown") is None else float(summary.get("max_drawdown", 0.0)) * 100.0,
        "mar": summary.get("mar"),
        "sharpe_like": summary.get("sharpe_like"),
        "trades": summary.get("trades") or summary.get("trade_count"),
        "funding_mode": summary.get("funding_mode"),
        "run_label": payload.get("run_label"),
        "report": str(run_dir / "long_native_research_report.json"),
    }


def _parse_venues(raw: list[str]) -> list[str]:
    venues = [v.strip().lower() for v in raw if v.strip()]
    unknown = sorted(set(venues) - set(ROOTS))
    if unknown:
        raise ValueError(f"unknown venues: {unknown}; expected one of {sorted(ROOTS)}")
    return venues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--end-date", required=True, help="End boundary, exclusive, YYYY-MM-DD.")
    ap.add_argument("--start-date", default=None, help="Optional start boundary, YYYY-MM-DD.")
    ap.add_argument("--venues", nargs="+", default=["bybit", "binance"])
    ap.add_argument("--output-root", default="research/btc_month_regime_2026-07-04")
    ap.add_argument("--config", default="configs/volume_alpha.default.yaml")
    ap.add_argument("--month-days", type=float, default=BTC_EXACT_MONTH_DAYS)
    ap.add_argument("--smart-tolerance", type=float, default=0.01)
    ap.add_argument("--component-take-profit-pct", type=float, default=0.12)
    ap.add_argument("--no-btc-risk-sizing", action="store_true")
    ap.add_argument("--write-candidate-tape", action="store_true")
    ap.add_argument(
        "--long-read-warmup-days",
        type=int,
        default=None,
        help="With --start-date, read long data from start-minus-this-many days instead of the full root.",
    )
    ap.add_argument("--only", choices=["all", "continuous", "long"], default="all")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    venues = _parse_venues(args.venues)
    output_root = Path(args.output_root).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    costs = load_config(args.config).costs
    rows: list[dict[str, Any]] = []

    if args.only in ("all", "continuous"):
        for arm, mode in CONTINUOUS_ARMS.items():
            arm_root = output_root / "continuous" / arm
            for venue in venues:
                summary_path = arm_root / venue / "continuous_equity_summary.json"
                if args.skip_existing and summary_path.exists():
                    rows.append(_continuous_row(arm=arm, venue=venue, run_root=output_root, elapsed=0.0))
                    print(f"[skip] continuous {venue} {arm}", flush=True)
                    continue
                t0 = time.perf_counter()
                print(f"[run ] continuous {venue} {arm}", flush=True)
                run_continuous_venue(
                    venue,
                    output_root=arm_root,
                    end_date=args.end_date,
                    start_date=args.start_date,
                    data_root=ROOTS[venue],
                    chart_leverage=1.0,
                    component_take_profit_pct=args.component_take_profit_pct,
                    btc_risk_sizing=not args.no_btc_risk_sizing,
                    config_transform=_continuous_transform(
                        mode=mode,
                        month_days=float(args.month_days),
                        smart_tolerance=float(args.smart_tolerance),
                    ),
                    write_candidate_tape=bool(args.write_candidate_tape),
                )
                rows.append(_continuous_row(arm=arm, venue=venue, run_root=output_root, elapsed=time.perf_counter() - t0))

    if args.only in ("all", "long"):
        for venue in venues:
            root = ROOTS[venue]
            base_cfg = long_profile(start=args.start_date, end=args.end_date)
            base_cfg = replace(
                base_cfg,
                btc_month_regime_month_days=float(args.month_days),
                btc_month_regime_smart_tolerance=float(args.smart_tolerance),
            )
            if args.long_read_warmup_days is not None:
                if args.start_date is None:
                    raise ValueError("--long-read-warmup-days requires --start-date")
                read_start = (
                    dt.date.fromisoformat(args.start_date) - dt.timedelta(days=int(args.long_read_warmup_days))
                ).isoformat()
                base_cfg = replace(base_cfg, read_start_date=read_start)
            print(f"[prep] long {venue} feature inputs", flush=True)
            shared_inputs = build_long_research_inputs(root, config=base_cfg)
            for arm, (gate, mode) in LONG_ARMS.items():
                run_dir = output_root / "long" / arm / venue
                report_json = run_dir / "long_native_research_report.json"
                if args.skip_existing and report_json.exists():
                    payload = _load_json(report_json)
                    rows.append(_long_row(arm=arm, venue=venue, run_dir=run_dir, payload=payload, elapsed=0.0))
                    print(f"[skip] long {venue} {arm}", flush=True)
                    continue
                cfg = replace(base_cfg, btc_month_regime_gate=gate, btc_month_regime_mode=mode)
                t0 = time.perf_counter()
                print(f"[run ] long {venue} {arm}", flush=True)
                payload = run_long_native_research(
                    root,
                    config=cfg,
                    cost_config=costs,
                    report_dir=run_dir,
                    precomputed_inputs=shared_inputs,
                )
                rows.append(_long_row(arm=arm, venue=venue, run_dir=run_dir, payload=payload, elapsed=time.perf_counter() - t0))

    summary = {
        "date": dt.date(2026, 7, 4).isoformat(),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "month_days": args.month_days,
        "smart_tolerance": args.smart_tolerance,
        "run_label": "exploratory",
        "rows": rows,
    }
    summary_path = output_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"summary: {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
