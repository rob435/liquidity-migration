"""Sweep fc_min_day_return on the v11a long-only profile.

Runs run_long_native_research with the v11a config from
long_native_event_demo._v11a_long_native_config, overriding only
fc_min_day_return per value. Each run writes its own report dir under
<data_root>/reports/long_native_fc_sweep/fc_min_day_<value>/.
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
from liquidity_migration.long_native import run_long_native_research
from liquidity_migration.long_native_event_demo import _v11a_long_native_config


def _fmt_pct_tag(value: float) -> str:
    return f"{int(round(value * 100)):03d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", required=True, help="Data root (e.g. ~/SHARED_DATA/bybit_full_pit)")
    ap.add_argument("--values", required=True,
                    help="Comma-separated fc_min_day_return values, e.g. 0.07,0.10,0.12,0.15,0.18,0.20,0.25")
    ap.add_argument("--config", default="configs/volume_alpha.default.yaml")
    ap.add_argument("--report-subdir", default="long_native_fc_sweep",
                    help="Subdirectory under <data-root>/reports/")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip values where the report JSON already exists")
    ap.add_argument("--disable-sigma", action="store_true",
                    help="Force fc_use_sigma_threshold=False so fc_min_day_return becomes the binding 1d gate.")
    args = ap.parse_args()

    values = [float(v.strip()) for v in args.values.split(",") if v.strip()]
    if not values:
        print("ERROR: no sweep values parsed", file=sys.stderr)
        return 2

    research = load_config(args.config)
    data_root = Path(args.data_root).expanduser()
    base_report_dir = data_root / "reports" / args.report_subdir
    base_report_dir.mkdir(parents=True, exist_ok=True)

    base_cfg = _v11a_long_native_config()
    sweep_summary: list[dict] = []

    for value in values:
        tag = _fmt_pct_tag(value)
        run_dir = base_report_dir / f"fc_min_day_{tag}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_json = run_dir / "long_native_research_report.json"

        if args.skip_existing and report_json.exists():
            print(f"[skip] fc_min_day_return={value}: report already exists", flush=True)
            payload = json.loads(report_json.read_text())
        else:
            overrides: dict[str, object] = {"fc_min_day_return": float(value)}
            if args.disable_sigma:
                overrides["fc_use_sigma_threshold"] = False
            cfg = replace(base_cfg, **overrides)
            t0 = time.perf_counter()
            print(f"[run ] fc_min_day_return={value} -> {run_dir}", flush=True)
            payload = run_long_native_research(
                data_root,
                config=cfg,
                cost_config=research.costs,
                report_dir=run_dir,
            )
            elapsed = time.perf_counter() - t0
            print(f"[done] fc_min_day_return={value} in {elapsed:.1f}s", flush=True)

        summary = payload.get("summary", {})
        splits = payload.get("splits", [])
        rows = payload.get("rows", {})
        row = {
            "fc_min_day_return": value,
            "trades": rows.get("trades", 0),
            "total_return": summary.get("total_return", 0.0),
            "sharpe_like": summary.get("sharpe_like", 0.0),
            "max_drawdown": summary.get("max_drawdown", 0.0),
            "trade_win_rate": summary.get("trade_win_rate", 0.0),
            "profit_factor": summary.get("profit_factor", 0.0),
            "funding_mode": summary.get("funding_mode", "missing"),
            "run_label": payload.get("run_label", "unknown"),
            "splits": {s["name"]: s for s in splits},
        }
        sweep_summary.append(row)

    # Write a sweep summary
    summary_path = base_report_dir / "sweep_summary.json"
    summary_path.write_text(json.dumps(sweep_summary, indent=2, default=str))

    md_lines = [
        f"# Long-Native FC sweep — {data_root.name}",
        "",
        f"Sweep of `fc_min_day_return` on v11a long-only profile (all other settings unchanged).",
        f"Values: {', '.join(f'{v:.2f}' for v in values)}",
        "",
        "## Overall metrics per value",
        "",
        "| fc_min_day | Trades | Return | Sharpe | MaxDD | WinRate | PF | Funding | Label |",
        "|---:|---:|---:|---:|---:|---:|---:|:---|:---|",
    ]
    for r in sweep_summary:
        md_lines.append(
            f"| {r['fc_min_day_return']:.2f} | {r['trades']} | "
            f"{r['total_return']*100:.1f}% | {r['sharpe_like']:.2f} | "
            f"{r['max_drawdown']*100:.1f}% | {r['trade_win_rate']*100:.0f}% | "
            f"{r['profit_factor']:.2f} | {r['funding_mode']} | {r['run_label']} |"
        )

    md_lines += ["", "## Splits per value (basket_count / return / sharpe / max_dd)", ""]
    split_names = []
    for r in sweep_summary:
        for n in r["splits"]:
            if n not in split_names:
                split_names.append(n)
    md_lines.append("| fc_min_day | " + " | ".join(split_names) + " |")
    md_lines.append("|---:|" + ("---|" * len(split_names)))
    for r in sweep_summary:
        cells = []
        for n in split_names:
            s = r["splits"].get(n)
            if s is None or s.get("basket_count", 0) == 0:
                cells.append("—")
            else:
                cells.append(
                    f"{s['basket_count']}b / {s['total_return']*100:.1f}% / "
                    f"{s['sharpe_like']:.2f} / {s['max_drawdown']*100:.1f}%"
                )
        md_lines.append(f"| {r['fc_min_day_return']:.2f} | " + " | ".join(cells) + " |")
    md_lines.append("")

    (base_report_dir / "sweep_summary.md").write_text("\n".join(md_lines))
    print(f"\nsweep summary -> {base_report_dir / 'sweep_summary.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
