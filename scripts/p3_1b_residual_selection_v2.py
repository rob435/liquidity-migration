"""P3-1b — residual-momentum selection IC, PIT-hardened + full-coverage (the rigorous re-run).

P3-1 v1 found a strong cross-venue negative IC (bybit -0.21 / binance -0.16) between trailing
factor-residual momentum and the age300 short's net_return: idiosyncratically-weak candidates are
better shorts. BUT (a) binance matched only 54% (residual-panel gaps), (b) an rmom30 bug, (c) the
trailing window included the SIGNAL-DAY residual (residual_return is keyed to the FORWARD 1d return,
so residual[D_signal] is only known at entry, not at the signal-close decision). This re-run fixes
all three:
  * residuals under COMMON4 factor model (full coverage on binance -> fixes the 54%).
  * two windows x two PIT modes:
      incl  = rolling_sum ending at the signal day (residual[D_signal] included; known at ENTRY +1h)
      lag1  = rolling_sum ending the day BEFORE (residual[D_signal] excluded; known at SIGNAL CLOSE)
    if the IC survives lag1, it is strictly pre-decision PIT-clean; if it collapses, it was the
    (borderline) event-day residual.
Read-only, EXPLORATORY. Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p3_1b_residual_selection_v2.py
"""
from __future__ import annotations

import csv, json, math, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import polars as pl  # noqa: E402
from liquidity_migration.risk_model import build_factor_panel, fit_factor_returns  # noqa: E402

SHARED = Path.home() / "SHARED_DATA"
START, END = "2023-04-01", "2026-05-28"
MS_PER_DAY = 86_400_000
VENUES = {"bybit": "bybit_full_pit", "binance": "binance_full_pit"}
LEDGER = ("e2_exhaustion_select_2026-05-30", "02_age_min")
COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]
SIGNALS = ["rmom7_incl", "rmom7_lag1", "rmom30_incl", "rmom30_lag1"]


def _spearman(pairs):
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None and not (isinstance(a, float) and math.isnan(a))]
    n = len(pairs)
    if n < 20:
        return float("nan"), n
    xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]: j += 1
            for k in range(i, j + 1): r[order[k]] = (i + j) / 2.0
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys); mx = sum(rx) / n; my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx)); vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return (cov / (vx * vy) if vx > 0 and vy > 0 else float("nan")), n


def main() -> int:
    print(f"P3-1b residual-momentum selection (common4 residuals; incl=entry-PIT, lag1=signal-close-PIT)\n", flush=True)
    out = {}
    for venue, sub in VENUES.items():
        root = SHARED / sub
        if not root.exists():
            continue
        print(f"[{venue}] build panel + common4 residuals ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty():
            continue
        _fr, resid = fit_factor_returns(panel, factor_cols=COMMON4)
        resid = resid.sort(["symbol", "ts_ms"]).with_columns([
            pl.col("residual_return").rolling_sum(window_size=7, min_samples=4).over("symbol").alias("rmom7_incl"),
            pl.col("residual_return").rolling_sum(window_size=30, min_samples=15).over("symbol").alias("rmom30_incl"),
        ]).with_columns([
            pl.col("rmom7_incl").shift(1).over("symbol").alias("rmom7_lag1"),
            pl.col("rmom30_incl").shift(1).over("symbol").alias("rmom30_lag1"),
        ])
        rmap = {(r["symbol"], r["ts_ms"]): {s: r[s] for s in SIGNALS} for r in resid.iter_rows(named=True)}
        rows = list(csv.DictReader(open(root / "reports" / LEDGER[0] / LEDGER[1] / "volume_event_best_trades.csv")))
        recs = []
        for r in rows:
            try:
                day = (int(r["entry_signal_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
                sig = rmap.get((r["symbol"], day))
                if sig is None:
                    continue
                recs.append((sig, float(r["net_return"])))
            except (KeyError, ValueError):
                continue
        matched, total = len(recs), len(rows)
        out[venue] = {"matched": matched, "total": total, "match_frac": round(matched / total, 2)}
        print(f"[{venue}] matched={matched}/{total} ({matched/total:.0%})", flush=True)
        for s in SIGNALS:
            ic, n = _spearman([(rec[0][s], rec[1]) for rec in recs])
            # tercile net spread on this signal
            valid = [(rec[0][s], rec[1]) for rec in recs if rec[0][s] is not None and not (isinstance(rec[0][s], float) and math.isnan(rec[0][s]))]
            spread = float("nan")
            if len(valid) >= 30:
                valid.sort(key=lambda p: p[0]); t = len(valid) // 3
                lo = sum(p[1] for p in valid[:t]) / t; hi = sum(p[1] for p in valid[-t:]) / t
                spread = lo - hi  # low-residual minus high-residual net (expect >0 if neg IC)
            out[venue][s] = {"ic": round(ic, 4), "n": n, "net_lo_minus_hi": round(spread, 5) if not math.isnan(spread) else None}
            print(f"[{venue}]   {s:12s} IC={ic:+.4f} (n={n})  net(lowResid-highResid)={spread:+.5f}", flush=True)
        print(flush=True)
    (SHARED / "p3_1b_residual_selection_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p3_1b_residual_selection_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
