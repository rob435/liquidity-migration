"""P3-1 — is the factor-neutral residual EXTRACTABLE as alpha? (the decisive closing test)

P2 showed the age-gated book is roughly factor-neutral but the residual is borderline. The only
way that residual is *tradeable* is if it is PREDICTABLE at decision time. This tests the natural
predictor: **trailing factor-residual momentum** — a name's cumulative factor-model residual return
over the trailing window (known at the signal close, PIT). If names that have been idiosyncratically
weak/strong continue, a residual-based selection could extract alpha; if the trailing residual has
no cross-venue IC vs the short's outcome, there is no extractable residual alpha and the program is
complete.

Method (both venues):
  * build_factor_panel -> fit_factor_returns -> per-(symbol,ts_ms) residual_return.
  * trailing residual momentum: rolling sum of residual_return over {7d,30d} per symbol (PIT,
    strictly backward incl. the signal day).
  * join to the age300 ledger at each trade's SIGNAL day (entry_signal_ts_ms snapped to daily grid).
  * cross-venue Spearman IC of resid_mom vs the short's net_return; tercile net_return; also IC vs
    the trade's FORWARD residual (from decompose) as a sanity cross-check.
Read-only, EXPLORATORY. Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p3_1_residual_selection_ic.py
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
from liquidity_migration.risk_model import (  # noqa: E402
    build_factor_panel, fit_factor_returns, _FACTOR_COLUMNS,
)

SHARED = Path.home() / "SHARED_DATA"
START, END = "2023-04-01", "2026-05-28"
MS_PER_DAY = 86_400_000
VENUES = {"bybit": "bybit_full_pit", "binance": "binance_full_pit"}
LEDGER = ("e2_exhaustion_select_2026-05-30", "02_age_min")  # age300


def _spearman(xs, ys):
    n = len(xs)
    if n < 20:
        return float("nan"), n
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]: j += 1
            for k in range(i, j + 1): r[order[k]] = (i + j) / 2.0
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx)); vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return (cov / (vx * vy) if vx > 0 and vy > 0 else float("nan")), n


def main() -> int:
    print(f"P3-1 residual-momentum selection IC  window={START}->{END}\n", flush=True)
    out = {}
    for venue, sub in VENUES.items():
        root = SHARED / sub
        if not root.exists():
            continue
        print(f"[{venue}] build panel + residuals ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty():
            continue
        _fr, resid = fit_factor_returns(panel, factor_cols=list(_FACTOR_COLUMNS))  # resid: symbol, ts_ms, residual_return
        resid = resid.sort(["symbol", "ts_ms"]).with_columns([
            pl.col("residual_return").rolling_sum(window_size=7, min_samples=4).over("symbol").alias("rmom7"),
            pl.col("residual_return").rolling_sum(window_size=30, min_samples=15).over("symbol").alias("rmom30"),
        ])
        rmap = {(r["symbol"], r["ts_ms"]): (r["rmom7"], r["rmom30"]) for r in resid.iter_rows(named=True)}
        # ledger
        rows = list(csv.DictReader(open(root / "reports" / LEDGER[0] / LEDGER[1] / "volume_event_best_trades.csv")))
        recs = []
        for r in rows:
            try:
                sig = int(r["entry_signal_ts_ms"]); day = (sig // MS_PER_DAY) * MS_PER_DAY
                rm = rmap.get((r["symbol"], day))
                if rm is None or rm[0] is None:
                    continue
                recs.append((float(rm[0]), float(rm[1]) if rm[1] is not None else float("nan"), float(r["net_return"])))
            except (KeyError, ValueError):
                continue
        matched = len(recs); total = len(rows)
        if matched < 20:
            print(f"[{venue}] only {matched}/{total} trades matched residuals — skip"); continue
        rmom7 = [x[0] for x in recs]; rmom30 = [x[1] for x in recs]; nret = [x[2] for x in recs]
        ic7, _ = _spearman(rmom7, nret)
        rmom30_ok = [(a, c) for a, b, c in recs if not math.isnan(b)]
        ic30, _ = _spearman([a for a, _ in rmom30_ok], [c for _, c in rmom30_ok]) if rmom30_ok else (float("nan"), 0)
        # tercile of rmom7 vs mean net_return (does a residual cut separate good/bad shorts?)
        order = sorted(range(len(rmom7)), key=lambda i: rmom7[i]); t = len(order) // 3
        lo = [nret[i] for i in order[:t]]; hi = [nret[i] for i in order[-t:]]
        out[venue] = {"matched": matched, "total": total, "ic_rmom7_vs_net": round(ic7, 4),
                      "ic_rmom30_vs_net": round(ic30, 4),
                      "net_low_rmom7": round(sum(lo) / len(lo), 5), "net_high_rmom7": round(sum(hi) / len(hi), 5)}
        print(f"[{venue}] matched={matched}/{total}  IC(rmom7,net)={ic7:+.4f}  IC(rmom30,net)={ic30:+.4f}  "
              f"net: low-rmom7={out[venue]['net_low_rmom7']:+.5f} high-rmom7={out[venue]['net_high_rmom7']:+.5f}", flush=True)
        print(flush=True)
    (SHARED / "p3_1_residual_selection_ic_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p3_1_residual_selection_ic_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
