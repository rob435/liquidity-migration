"""Aggregate a long-native FC sweep across multiple data roots.

Reads each <root>/reports/long_native_fc_sweep/sweep_summary.json and the
per-value report JSONs, and produces:

- A combined comparison markdown
- An equity-curves CSV per root (one column per fc_min_day value)
- Per-value trade counts and patterns

Usage:
    python scripts/long_native_sweep_aggregate.py \\
        --root bybit:~/SHARED_DATA/bybit_full_pit \\
        --root binance:~/SHARED_DATA/binance_full_pit \\
        --output /tmp/long_native_fc_sweep_report
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import polars as pl


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", action="append", required=True,
                    help="label:path repeatable, e.g. bybit:~/SHARED_DATA/bybit_full_pit")
    ap.add_argument("--report-subdir", default="long_native_fc_sweep")
    ap.add_argument("--output", required=True, help="output dir")
    args = ap.parse_args()

    out_dir = Path(args.output).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    roots: list[tuple[str, Path]] = []
    for r in args.root:
        if ":" not in r:
            print(f"ERROR: --root must be label:path, got {r!r}", file=sys.stderr)
            return 2
        label, path = r.split(":", 1)
        roots.append((label.strip(), Path(path.strip()).expanduser()))

    # Build per-root summary tables
    summaries: dict[str, list[dict]] = {}
    for label, root in roots:
        sweep_dir = root / "reports" / args.report_subdir
        summary_json = sweep_dir / "sweep_summary.json"
        if not summary_json.exists():
            print(f"[warn] missing {summary_json}")
            summaries[label] = []
            continue
        summaries[label] = json.loads(summary_json.read_text())

    # Write combined comparison MD
    lines = [
        "# Long-Native FC Daily-Increase Sweep — Combined Report",
        "",
        f"Sweep of `fc_min_day_return` on the v11a long-only profile (only this single parameter varied).",
        f"Roots: {', '.join(label for label, _ in roots)}",
        "",
    ]

    # Pull union of values for the column header
    all_values = sorted({row["fc_min_day_return"] for rows in summaries.values() for row in rows})

    for label, _ in roots:
        rows = {row["fc_min_day_return"]: row for row in summaries.get(label, [])}
        lines += [
            f"## {label}",
            "",
            "### Overall (Sharpe is hold-day annualised, see findings doc on inflation)",
            "",
            "| fc_min_day | Trades | Return | Sharpe | MaxDD | WinRate | PF | Funding | Label |",
            "|---:|---:|---:|---:|---:|---:|---:|:---|:---|",
        ]
        for v in all_values:
            r = rows.get(v)
            if r is None:
                lines.append(f"| {v:.2f} | — | — | — | — | — | — | — | missing |")
                continue
            lines.append(
                f"| {v:.2f} | {r['trades']} | "
                f"{r['total_return']*100:.1f}% | {r['sharpe_like']:.2f} | "
                f"{r['max_drawdown']*100:.1f}% | {r['trade_win_rate']*100:.0f}% | "
                f"{r['profit_factor']:.2f} | {r['funding_mode']} | {r['run_label']} |"
            )
        lines += ["", "### Splits (basket_count / return / sharpe / max_dd)", ""]
        split_names: list[str] = []
        for r in rows.values():
            for n in r["splits"]:
                if n not in split_names:
                    split_names.append(n)
        if split_names:
            lines.append("| fc_min_day | " + " | ".join(split_names) + " |")
            lines.append("|---:|" + ("---|" * len(split_names)))
            for v in all_values:
                r = rows.get(v)
                if r is None:
                    lines.append(f"| {v:.2f} |" + " — |" * len(split_names))
                    continue
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
                lines.append(f"| {v:.2f} | " + " | ".join(cells) + " |")
        else:
            lines.append("_(no in-range splits — typical for pre-2023 OOS roots which fall outside SPLITS)_")
        lines.append("")

    # Build per-root equity-curve CSV: union of dates, one column per value
    for label, root in roots:
        sweep_dir = root / "reports" / args.report_subdir
        curves: dict[float, pl.DataFrame] = {}
        for v in all_values:
            tag = f"{int(round(v * 100)):03d}"
            eq_path = sweep_dir / f"fc_min_day_{tag}" / "long_native_equity.csv"
            if not eq_path.exists():
                continue
            curves[v] = pl.read_csv(eq_path)
        if not curves:
            continue
        # combine on date (the basket exit date)
        merged = None
        for v, df in sorted(curves.items()):
            if "date" not in df.columns or "equity" not in df.columns:
                continue
            renamed = df.select(["date", "equity"]).rename({"equity": f"equity_fc_{int(round(v*100)):03d}"})
            merged = renamed if merged is None else merged.join(renamed, on="date", how="full", coalesce=True)
        if merged is not None:
            merged = merged.sort("date")
            csv_out = out_dir / f"equity_curves_{label}.csv"
            merged.write_csv(csv_out)
            lines += [f"Equity curves CSV ({label}): `{csv_out}`", ""]

    # Per-value trade samples per root (last few trades by date for the baseline 0.15)
    for label, root in roots:
        sweep_dir = root / "reports" / args.report_subdir
        for v in all_values:
            tag = f"{int(round(v * 100)):03d}"
            trades_path = sweep_dir / f"fc_min_day_{tag}" / "long_native_trades.csv"
            if not trades_path.exists():
                continue
            df = pl.read_csv(trades_path)
            if df.is_empty():
                continue
            symbol_counts = (df.group_by("symbol").len().sort("len", descending=True).head(8))
            pattern_counts = (df.group_by("pattern").len().sort("len", descending=True))
            net_mean = float(df["net_return"].mean() or 0.0)
            net_median = float(df["net_return"].median() or 0.0)
            lines += [
                f"### Trade detail — {label} fc_min_day={v:.2f}  (n={df.height})",
                "",
                f"- Mean net trade return: {net_mean*100:.2f}%",
                f"- Median net trade return: {net_median*100:.2f}%",
                f"- Top symbols: " + ", ".join(f"{row['symbol']}({row['len']})" for row in symbol_counts.iter_rows(named=True)),
                f"- Pattern counts: " + ", ".join(f"{row['pattern']}={row['len']}" for row in pattern_counts.iter_rows(named=True)),
                "",
            ]

    md_path = out_dir / "long_native_fc_sweep_report.md"
    md_path.write_text("\n".join(lines))
    print(f"combined report -> {md_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
