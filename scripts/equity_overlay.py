"""Render an overlay equity-curve PNG from two or more volume-events runs.

Reuses the repo's own equity-curve maker — `_strategy_equity_series` to build the
point series and `_write_equity_benchmark_png` to render — so the output matches
the per-run `volume_event_best_equity_btc.png` styling exactly, just with the
strategy curves of several runs overlaid instead of one strategy vs BTC.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.volume_events import (
    _strategy_equity_series,
    _write_equity_benchmark_png,
)

# Repo chart palette (navy strategy, amber benchmark) plus two extras.
COLORS = [(7, 14, 31), (234, 88, 12), (13, 148, 136), (190, 24, 93)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--series", action="append", required=True,
        help="label=path/to/volume_event_best_equity.csv  (repeat for each curve)",
    )
    ap.add_argument("--title", default="Equity Curve Comparison")
    ap.add_argument("--subtitle", default="Each curve is multiplicative equity normalised to $1 at the strategy start.")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    series: list[dict] = []
    all_dates: list[str] = []
    for i, spec in enumerate(args.series):
        label, _, path = spec.partition("=")
        points = _strategy_equity_series(pl.read_csv(path))
        if not points:
            raise SystemExit(f"no equity points in {path}")
        all_dates += [p["date"] for p in points]
        series.append({
            "name": label,
            "color": COLORS[i % len(COLORS)],
            "alpha": 255,
            "width": 4,
            "points": points,
        })

    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_equity_benchmark_png(
        out, series=series, start=min(all_dates), end=max(all_dates), monthly_rows=None,
        title=args.title, subtitle=args.subtitle,
    )
    finals = ", ".join(f"{s['name']}: {s['points'][-1]['value']:.2f}x" for s in series)
    print(f"wrote {out}\n  final equity -> {finals}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
