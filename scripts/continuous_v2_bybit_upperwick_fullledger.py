#!/usr/bin/env python3
"""Full-ledger validation of Bybit upper_wick entry sizing (operator direction 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

The OOS screen found size-up-by-upper_wick lifts the Bybit MAR proxy +3.0 and beats hash.
This confirms it at the FULL LEDGER (the bar the prior both-venue B-sizing FAILED): build a
strictly-causal per-symbol expanding-z upper_wick multiplier keyed by (symbol, signal_ts),
run the 3 fade components with size_mult_lookup through run_continuous_event_research, then
build_full_ledger (frozen rebalance + 2f hedge) and compare MAR vs control and vs a
hash-permuted multiplier (same distribution, alignment destroyed).

Bybit only. EXPLORATORY. A pass here makes it an operator-gated per-venue forward-shadow
lead; a fail kills it cleanly. Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import continuous_v2_ab_research_runner as abr  # noqa: E402
from continuous_v2_bybit_entry_alpha import _load_pre, enriched_features  # noqa: E402
from liquidity_migration.continuous_events import run_continuous_event_research  # noqa: E402
from liquidity_migration.continuous_forward_replay import build_full_ledger, frozen_hedge_regime  # noqa: E402
from liquidity_migration.continuous_regime import btcvol_intensity_series  # noqa: E402

VENUE = "bybit"
START, END = "2023-04-01", "2026-06-12"
K, CLIP = 0.5, (0.5, 1.5)  # defaults; overridable via CLI (sensitivity sweep found clip~3 better)
CACHE = Path.home() / "SHARED_DATA" / "continuous_v2_1m"


def _hash01(s):
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def build_upperwick_lookup(ab_root: Path, k: float = K, clip: tuple = CLIP):
    """Causal per-symbol expanding-z upper_wick multiplier keyed by (symbol, signal_ts)."""
    tr = pl.read_csv(ab_root / "V2_CONTROL" / VENUE / "trades.csv").filter(pl.col("side") == "short")
    recs = []
    for t in tr.sort("entry_signal_ts_ms").to_dicts():
        df = _load_pre(VENUE, str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"], CACHE)
        if df is None:
            continue
        f = enriched_features(df, float(t["entry_price"]))
        if f is None:
            continue
        recs.append((str(t["symbol"]), int(t["entry_signal_ts_ms"]), float(f["upper_wick_mean"])))
    # per-symbol expanding-prior z (min 10 obs), causal
    by_sym: dict[str, list] = {}
    for sym, sig, val in recs:
        by_sym.setdefault(sym, []).append((sig, val))
    real: dict[tuple[str, int], float] = {}
    for sym, seq in by_sym.items():
        seq.sort()
        hist = []
        for sig, val in seq:
            if len(hist) >= 10:
                mu, sd = float(np.mean(hist)), float(np.std(hist)) or 1.0
                z = (val - mu) / sd
                real[(sym, sig)] = float(np.clip(1.0 + k * z, *clip))
            else:
                real[(sym, sig)] = 1.0
            hist.append(val)
    # hash null: permute the multiplier multiset across keys
    keys = list(real)
    mults = [real[k] for k in keys]
    order = sorted(range(len(keys)), key=lambda i: _hash01(f"UWH:{keys[i][0]}:{keys[i][1]}"))
    hashed = {keys[i]: mults[order[i]] for i in range(len(keys))}
    return real, hashed


def run_arm(lookup, tag, out_root, resume):
    pieces = {}
    for spec in abr.V2_COMPONENTS:
        cfg = abr.v2_component_config(spec, start_date=START, end_date=END)
        cdir = out_root / tag / spec.key
        cdir.mkdir(parents=True, exist_ok=True)
        rep = cdir / "continuous_report.json"
        if resume and rep.exists() and (cdir / "continuous_trades.csv").exists():
            pass
        else:
            run_continuous_event_research(abr.ROOTS[VENUE], config=cfg, report_dir=cdir, size_mult_lookup=lookup)
        piece, _n, _c = abr._load_component_piece(cdir)
        pieces[spec.key] = piece
    all_days = sorted({d for p in pieces.values() for d in p.days})
    btc_ret, btc_fund = abr.instrument_inputs(abr.ROOTS[VENUE], VENUE, all_days, "BTCUSDT")
    eth_ret, eth_fund = abr.instrument_inputs(abr.ROOTS[VENUE], VENUE, all_days, "ETHUSDT")
    reg = frozen_hedge_regime()
    hi = btcvol_intensity_series(all_days, btc_ret, reg["lam"], reg["vol_window"], reg["pct_window"]) if reg else None
    ledger = build_full_ledger(pieces, btc_ret, btc_fund, eth_ret, eth_fund, hedge_intensity=hi)
    eq = ledger.select([c for c in ("ts_ms", "basket_return", "equity", "drawdown") if c in ledger.columns])
    return abr._metrics_from_equity(eq)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--out", default="backtest-runs/continuous_v2_bybit_upperwick_fullledger_2026-06-20")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--k", type=float, default=K)
    ap.add_argument("--clip", type=float, default=CLIP[1], help="symmetric clip c -> [1/c, c]")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    real, hashed = build_upperwick_lookup(Path(args.ab_root), k=args.k, clip=(1.0 / args.clip, args.clip))
    print(f"built upper_wick lookup: {len(real)} keys, mult mean={np.mean(list(real.values())):.4f} "
          f"min={min(real.values()):.3f} max={max(real.values()):.3f}", flush=True)
    res = {}
    res["control"] = run_arm(None, "control", out, args.resume)
    print(f"control: MAR={res['control']['mar']:.3f} dd={res['control']['max_drawdown']:.4f} tot={res['control']['total_return']:.4f}", flush=True)
    res["upperwick"] = run_arm(real, "upperwick", out, args.resume)
    print(f"upperwick: MAR={res['upperwick']['mar']:.3f} dd={res['upperwick']['max_drawdown']:.4f} tot={res['upperwick']['total_return']:.4f}", flush=True)
    res["hash"] = run_arm(hashed, "hash", out, args.resume)
    print(f"hash: MAR={res['hash']['mar']:.3f} dd={res['hash']['max_drawdown']:.4f} tot={res['hash']['total_return']:.4f}", flush=True)
    res["mar_delta_vs_control"] = res["upperwick"]["mar"] - res["control"]["mar"]
    res["mar_delta_vs_hash"] = res["upperwick"]["mar"] - res["hash"]["mar"]
    res["passes"] = bool(res["mar_delta_vs_control"] > 0 and res["mar_delta_vs_hash"] > 0)
    (out / "upperwick_fullledger.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"mar_delta_vs_control": res["mar_delta_vs_control"],
                      "mar_delta_vs_hash": res["mar_delta_vs_hash"], "passes": res["passes"]}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
