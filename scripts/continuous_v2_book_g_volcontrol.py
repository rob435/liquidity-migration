#!/usr/bin/env python3
"""Problem Book G — volatility-control rework, full-ledger (continuous v2 next-level).

Receipt: docs/preregistration/2026-06-20-continuous-v2-book-g-volcontrol-construction.md

The 2026-06-19 operator override DISABLED the daily vol-target adjuster ("to be
reworked + retuned when research is finished"). This book searches for a daily risk
control that improves risk-adjusted behavior (MAR / drawdown) vs the current OFF
override WITHOUT re-introducing the Bybit/Binance venue split — at the FULL daily-
marked ledger level (the adjuster acts on daily equity, so the per-trade proxy used
by Books A/B is the wrong tool here).

Method: load the frozen V2_CONTROL (TP12) component pieces cached by the Phase-0
freeze run, compute the 2f BTC+ETH hedge inputs + BTC-vol regime intensity once per
venue (exactly as the A/B runner does), then rebuild the full hedged ledger via
build_full_ledger(..., rebalance_rule=<variant>) for each G arm. TP and entries are
held fixed (same components) so this isolates the vol-control. All G arms are
expressible with the existing ContinuousRebalanceRule:
- G0_CONSTANT_OFF: current override (enabled=False -> scale 1.0).
- G1_MAX4:         prior daily vol-target (tv0.045/max4/w90/ddh-0.04).
- G2_CAP2 / G2b_CAP15: same vol-target but a tighter leverage cap (smoother).
- G3_DD_ONLY:      no vol lever (max_scale=1, target huge -> scale pinned to 1),
                   only drawdown-half derisk.
- G4_MOM_DERISK:   drawdown + strategy-momentum derisk, no vol lever.
- G5_TGT_LOWER:    lower vol target (more conservative) + cap 3.

EXPLORATORY: this is the full backtest ledger, but forward demo/paper is the OOS
arbiter; any winner is an operator-gated frozen-object change that voids the forward
ledger. Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import continuous_v2_ab_research_runner as abr  # noqa: E402
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    build_full_ledger,
    frozen_hedge_regime,
    frozen_rebalance_rule,
)
from liquidity_migration.continuous_regime import btcvol_intensity_series  # noqa: E402


def _rule(**kw: Any):
    return dataclasses.replace(frozen_rebalance_rule(), **kw)


G_ARMS: dict[str, Any] = {
    "G0_CONSTANT_OFF": _rule(enabled=False),
    "G1_MAX4": _rule(enabled=True),
    "G2_CAP2": _rule(enabled=True, max_scale=2.0),
    "G2b_CAP15": _rule(enabled=True, max_scale=1.5),
    "G3_DD_ONLY": _rule(enabled=True, max_scale=1.0, target_daily_vol=10.0, strategy_momentum_window_days=0),
    "G4_MOM_DERISK": _rule(
        enabled=True, max_scale=1.0, target_daily_vol=10.0,
        strategy_momentum_window_days=30, strategy_momentum_min_return=0.0,
        strategy_momentum_scale_when_below=0.5,
    ),
    "G5_TGT_LOWER": _rule(enabled=True, target_daily_vol=0.03, max_scale=3.0),
    # Constant-gross controls (vol-timing OFF, just leverage): MAR is ~leverage-
    # invariant, so if the vol-timed arms beat these at matched gross, the daily
    # vol-target TIMING is the source of the MAR gain, not the average leverage.
    "G_CONST2": _rule(enabled=True, max_scale=2.0, target_daily_vol=10.0, drawdown_half_threshold=None, strategy_momentum_window_days=0),
    "G_CONST3": _rule(enabled=True, max_scale=3.0, target_daily_vol=10.0, drawdown_half_threshold=None, strategy_momentum_window_days=0),
    "G_CONST4": _rule(enabled=True, max_scale=4.0, target_daily_vol=10.0, drawdown_half_threshold=None, strategy_momentum_window_days=0),
}


def load_pieces(phase0_root: Path, venue: str) -> dict[str, Any]:
    pieces = {}
    for spec in abr.V2_COMPONENTS:
        cdir = phase0_root / abr.CONTROL_ARM / venue / "components" / spec.key
        piece, _n, _cfg = abr._load_component_piece(cdir)
        pieces[spec.key] = piece
    return pieces


def venue_metrics(phase0_root: Path, venue: str) -> dict[str, Any]:
    pieces = load_pieces(phase0_root, venue)
    data_root = abr.ROOTS[venue]
    all_days = sorted({d for p in pieces.values() for d in p.days})
    btc_ret, btc_fund = abr.instrument_inputs(data_root, venue, all_days, "BTCUSDT")
    eth_ret, eth_fund = abr.instrument_inputs(data_root, venue, all_days, "ETHUSDT")
    regime = frozen_hedge_regime()
    hedge_intensity = (
        btcvol_intensity_series(all_days, btc_ret, regime["lam"], regime["vol_window"], regime["pct_window"])
        if regime else None
    )
    out: dict[str, Any] = {}
    for name, rule in G_ARMS.items():
        ledger = build_full_ledger(
            pieces, btc_ret, btc_fund, eth_ret, eth_fund,
            hedge_intensity=hedge_intensity, rebalance_rule=rule,
        )
        equity = ledger.select([c for c in ("ts_ms", "basket_return", "equity", "drawdown") if c in ledger.columns])
        m = abr._metrics_from_equity(equity)
        out[name] = {
            "total_return": m["total_return"], "max_drawdown": m["max_drawdown"],
            "mar": m["mar"], "sharpe_like": m["sharpe_like"], "worst_day_return": m["worst_day_return"],
            "rebalance_rule": dataclasses.asdict(rule),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase0-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--out", default="backtest-runs/continuous_v2_book_g_2026-06-20")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_full_ledger", "note": "isolates vol-control; TP/entries fixed (V2_CONTROL TP12 pieces)", "venues": {}}
    phase0 = Path(args.phase0_root)
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        summary["venues"][venue] = venue_metrics(phase0, venue)
        print(f"[{venue}]", flush=True)
        g0 = summary["venues"][venue]["G0_CONSTANT_OFF"]
        for name, m in summary["venues"][venue].items():
            dmar = (m["mar"] - g0["mar"]) if (m["mar"] is not None and g0["mar"] is not None) else None
            print(f"   {name:16s} mar={m['mar']:.3f} dd={m['max_drawdown']:+.4f} tot={m['total_return']:+.4f} "
                  f"worst={m['worst_day_return']:+.4f}  Δmar_vs_G0={dmar}", flush=True)
    # both-venue: improve MAR vs G0 AND not worsen drawdown on BOTH venues
    venues = list(summary["venues"])
    winners = []
    if len(venues) == 2:
        a, b = venues
        for name in G_ARMS:
            if name == "G0_CONSTANT_OFF":
                continue
            ma, mb = summary["venues"][a][name], summary["venues"][b][name]
            g0a, g0b = summary["venues"][a]["G0_CONSTANT_OFF"], summary["venues"][b]["G0_CONSTANT_OFF"]
            if ma["mar"] and mb["mar"] and ma["mar"] > g0a["mar"] and mb["mar"] > g0b["mar"]:
                winners.append(name)
    summary["both_venue_mar_improvers_vs_off"] = winners
    (out / "book_g_volcontrol.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"both_venue_mar_improvers_vs_off": winners}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
