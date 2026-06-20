#!/usr/bin/env python3
"""Gates-off LARGER-N power test for global-z upper_wick sizing (operator direction 2026-06-20).

The gated null (932 decisions) left upper_wick at the favorable tail (~83rd pct MAR) but NOT
significant — the hedged-MAR null is too wide (few-trade drawdown sensitivity). Turning the
BTC-trend gate OFF adds ~68% more trades; a larger sample TIGHTENS the permutation null, so a
real effect of fixed size separates better. This is a POWER/MECHANISM test on a DIFFERENT
(gate-off) book — NOT deployment evidence for the gated book. EXPLORATORY. REAL_MONEY false.

Staged + resumable: (1) gate-off control, (2) combine trades, (3) coverage manifest,
(4) extend 1m cache, (5) global-z lookup, (6) sized + multi-seed hash arms, (7) percentile.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import continuous_v2_ab_research_runner as abr  # noqa: E402
from continuous_v2_bybit_upperwick_fullledger import (  # noqa: E402
    VENUE,
    build_upperwick_lookup,
    run_arm,
)
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

GATESOFF = Path("backtest-runs/continuous_v2_gatesoff_2026-06-20")
AB_GATESOFF = GATESOFF / "ab_root"
TRADES_DEST = AB_GATESOFF / "V2_CONTROL" / VENUE / "trades.csv"
HASH_SEEDS = ["", "s2", "s3", "s5", "s7"]  # 5-seed null (compute-bounded)


def main() -> int:
    GATESOFF.mkdir(parents=True, exist_ok=True)
    # (1) gate-off control — run_arm builds the components + returns the hedged metrics
    print("[1] gate-off control ...", flush=True)
    ctrl = run_arm(None, "control", GATESOFF, resume=True, gate="off")
    print(f"    control(gate off): MAR={ctrl['mar']:.3f} tot={ctrl['total_return']:.4f}", flush=True)

    # (2) combine the 3 control component trades into the lookup's expected V2_CONTROL path
    parts = []
    for spec in abr.V2_COMPONENTS:
        df = pl.read_csv(GATESOFF / "control" / spec.key / "continuous_trades.csv")
        parts.append(df.filter(pl.col("side") == "short").with_columns(pl.lit(spec.key).alias("component")))
    trades = pl.concat(parts, how="diagonal")
    TRADES_DEST.parent.mkdir(parents=True, exist_ok=True)
    trades.write_csv(TRADES_DEST)
    n_dec = trades.select(["symbol", "entry_signal_ts_ms"]).unique().height
    print(f"[2] gate-off trades: {trades.height} component-rows, {n_dec} unique decisions "
          f"(gated was 2367 / 932)", flush=True)

    # (3) coverage manifest: [entry_date-1 .. exit_date] per trade, UNIONed with the existing cache
    man_path = CACHE_ROOT / f"coverage_needed_{VENUE}.parquet"
    existing = pl.read_parquet(man_path) if man_path.exists() else pl.DataFrame({"symbol": [], "date": []})
    need = set(zip(existing["symbol"].to_list(), existing["date"].to_list()))
    for t in trades.select(["symbol", "entry_date", "exit_date"]).to_dicts():
        sym = str(t["symbol"])
        d = dt.date.fromisoformat(str(t["entry_date"])[:10]) - dt.timedelta(days=1)
        xd = dt.date.fromisoformat(str(t["exit_date"])[:10])
        while d <= xd:
            need.add((sym, d.isoformat()))
            d += dt.timedelta(days=1)
    pl.DataFrame(sorted(need), schema=["symbol", "date"], orient="row").write_parquet(man_path)
    print(f"[3] coverage manifest: {len(need)} partitions (was {existing.height})", flush=True)

    # (4) extend the 1m cache (resume skips cached partitions; missing 404s are ledgered)
    print("[4] extending 1m cache from public archives ...", flush=True)
    subprocess.run([sys.executable, "scripts/continuous_v2_build_1m_trade_windows.py",
                    "--venues", VENUE, "--resume", "--workers", "8"], check=False)

    # (5) global-z lookup on the gate-off trades + (6) sized + multi-seed hash arms (gate off)
    print("[5/6] global-z lookup + sized + hash arms (gate off) ...", flush=True)
    res = {"control": ctrl}
    real, _ = build_upperwick_lookup(AB_GATESOFF, vol_attenuate=True, mode="global")
    res["upperwick"] = run_arm(real, "upperwick", GATESOFF, resume=True, gate="off")
    print(f"    upperwick: MAR={res['upperwick']['mar']:.3f} tot={res['upperwick']['total_return']:.4f}", flush=True)
    hash_mars, hash_tots = [], []
    for sd in HASH_SEEDS:
        _, hashed = build_upperwick_lookup(AB_GATESOFF, vol_attenuate=True, mode="global", hash_seed=sd)
        hm = run_arm(hashed, f"hash_{sd or '0'}", GATESOFF, resume=True, gate="off")
        hash_mars.append(hm["mar"])
        hash_tots.append(hm["total_return"])
        print(f"    hash[{sd or '0'}]: MAR={hm['mar']:.3f} tot={hm['total_return']:.4f}", flush=True)

    # (7) percentile of upperwick in the gate-off hash null
    uw_mar, uw_tot = res["upperwick"]["mar"], res["upperwick"]["total_return"]
    mar_beat = sum(1 for h in hash_mars if uw_mar > h)
    tot_beat = sum(1 for h in hash_tots if uw_tot > h)
    p_mar = (sum(1 for h in hash_mars if h >= uw_mar) + 1) / (len(hash_mars) + 1)
    p_tot = (sum(1 for h in hash_tots if h >= uw_tot) + 1) / (len(hash_tots) + 1)
    summary = {
        "n_decisions": n_dec, "control_mar": ctrl["mar"], "upperwick_mar": uw_mar,
        "hash_mars": hash_mars, "mar_beats": f"{mar_beat}/{len(hash_mars)}", "perm_p_mar": p_mar,
        "upperwick_tot": uw_tot, "hash_tots": hash_tots, "tot_beats": f"{tot_beat}/{len(hash_tots)}",
        "perm_p_tot": p_tot,
    }
    (GATESOFF / "gatesoff_upperwick_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n[7] GATE-OFF VERDICT ({n_dec} decisions):", flush=True)
    print(f"    MAR  : upperwick {uw_mar:.3f} beats {mar_beat}/{len(hash_mars)} hashes, perm p={p_mar:.3f}", flush=True)
    print(f"    total: upperwick {uw_tot:.4f} beats {tot_beat}/{len(hash_tots)} hashes, perm p={p_tot:.3f}", flush=True)
    print("    (significant if perm p < 0.05; gated was ~p 0.17-0.29)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
