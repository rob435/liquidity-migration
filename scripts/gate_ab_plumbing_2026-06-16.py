#!/usr/bin/env python3
"""EXPLORATORY A/B — deployed continuous book with the 30d-BTC trend ENTRY gate
ON (btc_trend_gate="uptrend", the deployed value) vs OFF (btc_trend_gate="off").

Motivation (operator, 2026-06-16): the live demo book is FLAT because BTC's 30d
return is negative, so the uptrend entry gate is closed and no fills flow — the
demo/paper plumbing cannot be exercised. Question: how much performance changes if
the gate is turned off (so the book fades in all BTC regimes), to decide whether to
flip it OFF on demo/paper as a plumbing test.

Faithful to the DEPLOYED object: reproduces the orchestrator's `venue_update`
exactly (4 winner_base components combined by FROZEN_FORWARD_CONFIG weights ->
frozen rebalance rule -> BTC+ETH 2f hedge scaled by the frozen BTC-vol regime
intensity), changing ONLY the per-component `btc_trend_gate`. Full PIT (engine
default), fully costed (fees + slippage + funding modeled), both venues, the full
available history per venue.

RUN LABEL: exploratory. This is a config A/B diagnostic + plumbing-decision input,
NOT promotion evidence and NOT pre-registered. Demo/paper is execution-evidence
only; this number quantifies the strategy cost of the gate, it does not promote
anything. Tier-3 real-money gate unchanged.

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python -u \
        scripts/gate_ab_plumbing_2026-06-16.py --venues bybit,binance \
        --out ~/SHARED_DATA/gate_ab_plumbing_2026-06-16
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import rebuild_winner_base_component_ledgers as rb  # noqa: E402
from continuous_forward_replay_orchestrator import (  # noqa: E402
    NAME_TO_CELL,
    btc_inputs,
    data_end_day,
)
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _daily_pnl_metrics,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    build_full_ledger,
    frozen_config_hash,
    frozen_hedge_regime,
)
from liquidity_migration.continuous_regime import btcvol_intensity_series  # noqa: E402
from scripts._w5_shared import _load_component  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
CELL_OVERRIDES = {cell: ov for (_root, cell), ov in rb.CELLS.items()}
START = "2023-04-01"


def build_arm(venue: str, gate: str, work: Path, start: str, end: str) -> dict[str, Any]:
    """Build the 4 components with the given btc_trend_gate, then the full
    deployed 2f+regime hedged ledger. Returns metrics + per-component counts."""
    root = ROOTS[venue]
    pieces: dict[str, Any] = {}
    comp_counts: dict[str, int] = {}
    for name, cell in NAME_TO_CELL.items():
        overrides = CELL_OVERRIDES[cell]
        out_dir = work / venue / cell
        cfg = ContinuousEventConfig(
            **{**rb.COMMON, **overrides, "btc_trend_gate": gate,
               "start_date": start, "end_date": end}
        )
        t0 = time.time()
        payload = run_continuous_event_research(root, config=cfg, report_dir=out_dir)
        comp_counts[name] = int(payload["n_trades"])
        print(f"  [{venue}/{gate}] {cell}: {payload['n_trades']} trades "
              f"(label={payload.get('run_label')}, funding={payload.get('funding_mode')}, "
              f"{time.time() - t0:.0f}s)", flush=True)
        comp, _trades, _rep = _load_component(out_dir)
        pieces[name] = comp

    all_days = sorted({d for p in pieces.values() for d in p.days})
    rets, fund = btc_inputs(venue, all_days, "BTCUSDT")
    rets2, fund2 = btc_inputs(venue, all_days, "ETHUSDT")
    regime = frozen_hedge_regime()
    intensity = (
        btcvol_intensity_series(all_days, rets, regime["lam"],
                                regime["vol_window"], regime["pct_window"])
        if regime else None
    )
    ledger = build_full_ledger(pieces, rets, fund, rets2, fund2, hedge_intensity=intensity)
    m = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    mean_int = (sum(intensity.values()) / len(intensity)) if intensity else 1.0
    return {
        "gate": gate,
        "n_trades_total": int(sum(comp_counts.values())),
        "n_trades_by_component": comp_counts,
        "n_ledger_days": int(ledger.height),
        "total_return": m.get("total_return"),
        "annualized_return": m.get("annualized_return"),
        "mar": m.get("mar"),
        "max_drawdown": m.get("max_drawdown"),
        "sharpe_like": m.get("sharpe_like"),
        "worst_day_return": m.get("worst_day_return"),
        "mean_hedge_intensity": mean_int,
        "mean_hedge_ratio": (float(ledger["hedge_ratio"].mean())
                             if "hedge_ratio" in ledger.columns else None),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--gates", default="uptrend,off")
    ap.add_argument("--start", default=START)
    ap.add_argument("--out", default=str(SHARED / "gate_ab_plumbing_2026-06-16"))
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"

    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    gates = [g.strip() for g in args.gates.split(",") if g.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_label": "exploratory",
        "description": "deployed continuous book: btc_trend_gate uptrend(deployed) vs off",
        "frozen_forward_config_hash": frozen_config_hash(),
        "start": args.start, "gates": gates, "venues": {},
    }
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            print(f"[{venue}] MISSING ROOT {root}", flush=True)
            continue
        end = data_end_day(root)  # end-exclusive last kline partition date
        print(f"[{venue}] window {args.start} -> {end} (exclusive)", flush=True)
        arms: dict[str, Any] = {}
        for gate in gates:
            arms[gate] = build_arm(venue, gate, work, args.start, end)
        block: dict[str, Any] = {"data_end": end, "arms": arms}
        if "uptrend" in arms and "off" in arms:
            on, off = arms["uptrend"], arms["off"]
            block["delta_off_minus_on"] = {
                "mar": (None if on["mar"] is None or off["mar"] is None
                        else off["mar"] - on["mar"]),
                "total_return": off["total_return"] - on["total_return"],
                "max_drawdown_abs": abs(off["max_drawdown"]) - abs(on["max_drawdown"]),
                "sharpe_like": off["sharpe_like"] - on["sharpe_like"],
                "n_trades": off["n_trades_total"] - on["n_trades_total"],
            }
        payload["venues"][venue] = block
        (out / "gate_ab_summary.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")

    # console table
    print("\n=== GATE A/B (deployed 2f+regime book, full-PIT, costed) ===", flush=True)
    hdr = f"{'venue':8} {'gate':8} {'ret':>8} {'MAR':>7} {'maxDD':>8} {'Sharpe':>7} {'trades':>7} {'mean_hr':>8}"
    print(hdr, flush=True)
    for venue, block in payload["venues"].items():
        if "arms" not in block:
            print(f"{venue:8} ERROR {block.get('error')}", flush=True)
            continue
        for gate, a in block["arms"].items():
            mar = "n/a" if a["mar"] is None else f"{a['mar']:.2f}"
            hr = "n/a" if a["mean_hedge_ratio"] is None else f"{a['mean_hedge_ratio']:.3f}"
            print(f"{venue:8} {gate:8} {a['total_return']*100:7.1f}% {mar:>7} "
                  f"{abs(a['max_drawdown'])*100:7.1f}% {a['sharpe_like']:7.2f} "
                  f"{a['n_trades_total']:7d} {hr:>8}", flush=True)
        d = block.get("delta_off_minus_on")
        if d:
            dmar = "n/a" if d["mar"] is None else f"{d['mar']:+.2f}"
            print(f"{venue:8} {'Δoff-on':8} {d['total_return']*100:+7.1f}% {dmar:>7} "
                  f"{d['max_drawdown_abs']*100:+7.1f}% {d['sharpe_like']:+7.2f} "
                  f"{d['n_trades']:+7d}", flush=True)
    print(f"\nsummary -> {out / 'gate_ab_summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
