"""B.6 — sweep over the v11a long-only sleeve enabling one disabled pattern at
a time, then run an ensemble of any survivors.

The v11a config (`_v11a_long_native_config`) ships with only `enable_fomo_chase`
on. The legacy `findings.md` reported that the other 5 patterns systematically
lose money — but those evaluations come from a narrower universe and the
pre-sigma-threshold ATR/regime/cost framework. Worth a fresh look on the new
full-PIT roots.

Run shape mirrors `scripts/long_native_sweep_fc_min_day.py`:

    python scripts/long_native_sweep_patterns.py \\
        --data-root ~/SHARED_DATA/bybit_full_pit \\
        --patterns cap_rebound,funding_squeeze,volume_resurrection,oversold_bounce,uptrend_dip \\
        --include-ensemble \\
        --report-subdir long_native_pattern_sweep

Each enabled pattern produces its own per-pattern report dir; the optional
ensemble run enables all surviving patterns simultaneously.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.config import load_config
from liquidity_migration.long_native import LongNativeConfig, run_long_native_research
from liquidity_migration.long_native_event_demo import _v11a_long_native_config


PATTERN_FLAGS: dict[str, str] = {
    "fomo_chase": "enable_fomo_chase",
    "cap_rebound": "enable_capitulation_rebound",
    "funding_squeeze": "enable_funding_squeeze",
    "volume_resurrection": "enable_volume_resurrection",
    "oversold_bounce": "enable_oversold_bounce",
    "uptrend_dip": "enable_uptrend_dip",
}


def _config_with_only(patterns: list[str], base: LongNativeConfig) -> LongNativeConfig:
    """Return a copy of `base` with the given pattern set enabled and the
    remaining patterns disabled. Always uses the v11a baseline as the
    starting point.
    """
    overrides: dict[str, object] = {flag: False for flag in PATTERN_FLAGS.values()}
    for pattern in patterns:
        if pattern not in PATTERN_FLAGS:
            raise ValueError(f"unknown pattern: {pattern}")
        overrides[PATTERN_FLAGS[pattern]] = True
    return replace(base, **overrides)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", required=True, help="Data root (e.g. ~/SHARED_DATA/bybit_full_pit).")
    ap.add_argument(
        "--patterns",
        default="cap_rebound,funding_squeeze,volume_resurrection,oversold_bounce,uptrend_dip",
        help="Comma-separated pattern names to test individually. fomo_chase is always "
             "included as the baseline.",
    )
    ap.add_argument(
        "--include-ensemble",
        action="store_true",
        help="Also run an ensemble with fomo_chase + every individually-tested pattern enabled.",
    )
    ap.add_argument(
        "--ensemble-only",
        action="store_true",
        help="Skip the per-pattern runs; run only the all-on ensemble.",
    )
    ap.add_argument("--config", default="configs/volume_alpha.default.yaml")
    ap.add_argument("--report-subdir", default="long_native_pattern_sweep",
                    help="Subdirectory under <data-root>/reports/")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip runs whose report JSON already exists.")
    args = ap.parse_args()

    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    for p in patterns:
        if p not in PATTERN_FLAGS:
            print(f"ERROR: unknown pattern '{p}'. Valid: {sorted(PATTERN_FLAGS)}", file=sys.stderr)
            return 2

    research = load_config(args.config)
    data_root = Path(args.data_root).expanduser()
    base_report_dir = data_root / "reports" / args.report_subdir
    base_report_dir.mkdir(parents=True, exist_ok=True)

    base_cfg = _v11a_long_native_config()

    plan: list[tuple[str, list[str]]] = []
    if not args.ensemble_only:
        # Baseline: fomo_chase only (matches v11a default). Always run so
        # downstream comparison has the right anchor.
        plan.append(("baseline_fomo_chase", ["fomo_chase"]))
        for pattern in patterns:
            plan.append((f"fc_plus_{pattern}", ["fomo_chase", pattern]))
    if args.include_ensemble or args.ensemble_only:
        plan.append(("ensemble_all", ["fomo_chase"] + patterns))

    sweep_summary: list[dict] = []
    for name, enabled in plan:
        run_dir = base_report_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        report_json = run_dir / "long_native_research_report.json"

        if args.skip_existing and report_json.exists():
            print(f"[skip] {name}: report already exists", flush=True)
            payload = json.loads(report_json.read_text())
        else:
            cfg = _config_with_only(enabled, base_cfg)
            t0 = time.perf_counter()
            print(f"[run ] {name}: enabled={enabled} -> {run_dir}", flush=True)
            payload = run_long_native_research(
                data_root,
                config=cfg,
                cost_config=research.costs,
                report_dir=run_dir,
            )
            elapsed = time.perf_counter() - t0
            print(f"[done] {name} in {elapsed:.1f}s", flush=True)

        summary = payload.get("summary", {})
        rows = payload.get("rows", {})
        sweep_summary.append({
            "run_name": name,
            "patterns_enabled": enabled,
            "trades": rows.get("trades", 0),
            "total_return": summary.get("total_return", 0.0),
            "sharpe_like": summary.get("sharpe_like", 0.0),
            "max_drawdown": summary.get("max_drawdown", 0.0),
            "trade_win_rate": summary.get("trade_win_rate", 0.0),
            "profit_factor": summary.get("profit_factor", 0.0),
            "funding_mode": summary.get("funding_mode", "missing"),
            "run_label": payload.get("run_label", "unknown"),
        })

    summary_path = base_report_dir / "sweep_summary.json"
    summary_path.write_text(json.dumps(sweep_summary, indent=2, default=str))

    md_lines = [
        f"# Long-native pattern sweep — {data_root.name}",
        "",
        "Per-pattern v11a backtest (fomo_chase + one other pattern at a time) plus an"
        " optional all-on ensemble. Sharpe is daily-aligned from the calendar-day"
        " equity series.",
        "",
        "| Run | Patterns | Trades | Return | Sharpe | MaxDD | WinRate | PF | Label |",
        "|---|---|---:|---:|---:|---:|---:|---:|:---|",
    ]
    for r in sweep_summary:
        md_lines.append(
            f"| {r['run_name']} | {','.join(r['patterns_enabled'])} | {r['trades']} | "
            f"{r['total_return']*100:.1f}% | {r['sharpe_like']:.2f} | "
            f"{r['max_drawdown']*100:.1f}% | {r['trade_win_rate']*100:.0f}% | "
            f"{r['profit_factor']:.2f} | {r['run_label']} |"
        )

    (base_report_dir / "sweep_summary.md").write_text("\n".join(md_lines) + "\n")
    print(f"\nsweep summary -> {base_report_dir / 'sweep_summary.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
