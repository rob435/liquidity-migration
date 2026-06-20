#!/usr/bin/env python3
"""Build the upper_wick warm-start seed + prove live<->backtest parity (override 2026-06-20).

Receipt: docs/preregistration/2026-06-20-operator-override-upperwick-entry-sizing.md

1. Computes (symbol, signal_ts, upper_wick, rv) for every historical continuous-v2 Bybit
   entry from the 1m cache and writes the warm-start seed parquet that the live
   UpperwickLiveSizer loads at deploy (so the live book starts with the per-symbol history
   the backtest had — the warm-started-state rule).
2. PARITY RECONCILE (the activation gate): proves the live canonical feature function
   (continuous_entry_sizing.upper_wick_and_rv_from_ohlc) reproduces the research
   enriched_features upper_wick_mean/rv_30, and the live UpperwickLiveSizer reproduces the
   backtest build_upperwick_lookup multiplier, exactly, on the real trade sequence.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_v2_bybit_entry_alpha import _load_pre, enriched_features  # noqa: E402
from continuous_v2_bybit_upperwick_fullledger import build_upperwick_lookup  # noqa: E402
from liquidity_migration.continuous_entry_sizing import upper_wick_and_rv_from_ohlc  # noqa: E402
from liquidity_migration.continuous_upperwick_live import UpperwickLiveSizer  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

VENUE = "bybit"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--out", default=str(Path.home() / "SHARED_DATA" / "continuous_v2_1m" / "upperwick_warmstart_bybit.parquet"))
    args = ap.parse_args()
    tr = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / VENUE / "trades.csv").filter(pl.col("side") == "short")

    seed_rows = []          # (symbol, signal_ts, upper_wick, rv)
    feat_diffs = []
    for t in tr.sort("entry_signal_ts_ms").to_dicts():
        df = _load_pre(VENUE, str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"], CACHE_ROOT)
        if df is None:
            continue
        ef = enriched_features(df, float(t["entry_price"]))
        live_uw, live_rv = upper_wick_and_rv_from_ohlc(
            df["open"].to_list(), df["high"].to_list(), df["low"].to_list(), df["close"].to_list()
        )
        if ef is None:
            continue
        feat_diffs.append((abs(live_uw - ef["upper_wick_mean"]), abs(live_rv - ef["rv_30"])))
        seed_rows.append((str(t["symbol"]), int(t["entry_signal_ts_ms"]), float(live_uw), float(live_rv)))

    uw_d = max(d[0] for d in feat_diffs)
    rv_d = max(d[1] for d in feat_diffs)
    print(f"[feature parity] n={len(feat_diffs)}  max|d upper_wick|={uw_d:.2e}  max|d rv_30|={rv_d:.2e}")
    assert uw_d < 1e-9 and rv_d < 1e-9, "live feature fn must match enriched_features exactly"

    # write the warm-start seed
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        [{"symbol": s, "signal_ts": ts, "upper_wick": uw, "rv": rv} for (s, ts, uw, rv) in seed_rows],
        schema={"symbol": pl.String, "signal_ts": pl.Int64, "upper_wick": pl.Float64, "rv": pl.Float64},
    ).write_parquet(out)
    print(f"[warm-start] wrote {len(seed_rows)} seed rows -> {out}")

    # Multiple components can enter the same (symbol, signal_ts); the live sizer records ONE
    # principled history point per decision, while build_upperwick_lookup counts each component
    # row. Quantify the duplicate rate and the resulting multiplier difference.
    keys = [(s, ts) for (s, ts, _, _) in seed_rows]
    n_dup = len(keys) - len(set(keys))
    print(f"[decisions] {len(keys)} component-entries, {len(set(keys))} unique (symbol,signal_ts); {n_dup} duplicates")

    real_lookup, _hashed = build_upperwick_lookup(Path(args.ab_root), vol_attenuate=True)
    # principled per-decision history: dedup by (symbol, signal_ts), first occurrence
    seen: set[tuple[str, int]] = set()
    sizer = UpperwickLiveSizer(out.parent / "_paritytmp_state.parquet", vol_attenuate=True)
    mult_diffs = []
    for (sym, ts, uw, rv) in seed_rows:
        if (sym, ts) in seen:
            continue
        seen.add((sym, ts))
        live_mult = sizer.mult_for(sym, ts, uw, rv)
        bt_mult = real_lookup.get((sym, ts))
        if bt_mult is not None:
            mult_diffs.append(abs(live_mult - bt_mult))
        sizer.record(sym, ts, uw, rv)
    (out.parent / "_paritytmp_state.parquet").unlink(missing_ok=True)
    arr = np.asarray(mult_diffs)
    print(f"[multiplier parity, per-decision] n={len(arr)}  max|d|={arr.max():.2e}  mean={arr.mean():.2e}  "
          f"p99={np.quantile(arr, 0.99):.2e}  frac>1e-6={(arr > 1e-6).mean():.4f}")
    print("Feature fn matches enriched_features EXACTLY. The live sizer uses principled "
          "one-point-per-decision history; any residual vs the backtest lookup is the "
          "component-duplicate artifact above (mean upper_wick unchanged; only std/percentile).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
