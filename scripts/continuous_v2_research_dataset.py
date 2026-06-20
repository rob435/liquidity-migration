#!/usr/bin/env python3
"""Build a RICHER continuous-fade research dataset (operator direction 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-research-dataset-construction.md

For mechanism research (adverse-trade characterization, TWAP/VWAP entry, dynamic TP) we
want as many real fade trades as possible and a clean per-trade (equal-weight) basis.
This builds the v2 fade components with TWO gates relaxed vs the frozen control:

- `btc_trend_gate = "off"`   (frozen control: "uptrend") -> include the BTC-downtrend
  entries the live strategy skips. MORE trades, full behavior.
- `sizing_mode = "flat"`     (frozen control: "inverse_vol") -> equal weight, so the
  trade rule is studied BEFORE position sizing ("perfect the trade first"); sizing is a
  portfolio-construction step applied later.

Everything else (short, decile 9, rmom q0.25, +1h entry delay, TP 12%, 24h/48h hold,
$500k liquidity floor, funding) matches the frozen control so the trades ARE v2 fades,
just ungated and unsized. This is an EXPLORATORY research dataset — NOT the strategy, NOT
a candidate, NOT promotion evidence. It must never be cited as the V2_CONTROL ledger.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import continuous_v2_ab_research_runner as abr  # noqa: E402
from liquidity_migration.continuous_events import run_continuous_event_research  # noqa: E402

START, END = "2023-04-01", "2026-06-12"


def research_config(spec, *, start: str, end: str):
    base = abr.v2_component_config(spec, start_date=start, end_date=end)  # frozen control config
    return dataclasses.replace(base, btc_trend_gate="off", sizing_mode="flat")


def build_venue(venue: str, out_root: Path, *, resume: bool) -> dict[str, Any]:
    data_root = abr.ROOTS[venue]
    frames = []
    per_comp = {}
    for spec in abr.V2_COMPONENTS:
        cfg = research_config(spec, start=START, end=END)
        cdir = out_root / venue / spec.key
        cdir.mkdir(parents=True, exist_ok=True)
        report = cdir / "continuous_report.json"
        if resume and report.exists() and (cdir / "continuous_trades.csv").exists():
            payload = json.loads(report.read_text(encoding="utf-8"))
        else:
            payload = run_continuous_event_research(data_root, config=cfg, report_dir=cdir)
        per_comp[spec.key] = {"n_trades": payload.get("n_trades"), "config_hash": payload.get("config_hash")}
        tp = cdir / "continuous_trades.csv"
        if tp.exists():
            df = pl.read_csv(tp)
            if not df.is_empty():
                df = df.with_columns(pl.lit(spec.key).alias("component"))
                frames.append(df)
    combined = pl.concat(frames, how="diagonal") if frames else pl.DataFrame()
    combined.write_csv(out_root / f"research_trades_{venue}.csv")
    return {"venue": venue, "n_trades": int(combined.height), "components": per_comp,
            "trades_path": str(out_root / f"research_trades_{venue}.csv")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--out", default="backtest-runs/continuous_v2_research_dataset_2026-06-20")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_research_dataset", "start": START, "end": END,
                               "gates_relaxed": {"btc_trend_gate": "off", "sizing_mode": "flat"},
                               "note": "ungated + equal-weight v2 fades for mechanism research; NOT the V2_CONTROL strategy",
                               "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        res = build_venue(venue, out, resume=args.resume)
        summary["venues"][venue] = res
        print(f"[{venue}] research trades: {res['n_trades']}  "
              f"(components: {[ (k, c['n_trades']) for k,c in res['components'].items() ]})", flush=True)
    (out / "research_dataset_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"venues": {v: summary["venues"][v]["n_trades"] for v in summary["venues"]}}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
