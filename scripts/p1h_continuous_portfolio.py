"""Phase 1 / Avenue D — portfolio proxy for the CORRECTED continuous strategy (enter fresh rmom-D9, short hold).

p1b established (signal level) that the fade is an all-weather all-day intraday process -> continuous viable,
right design = enter a FRESH rmom-gated D9 spell-entry at ANY hour, hold ~6-12h. This turns that per-trade
signal into a portfolio MAR via a faithful CONCURRENT-position simulation (not the i2 per-day booking, which
loses intraday concurrency):
  - entries: fresh gap-aware D9 spell-starts, per-name cooldown = hold H (no re-entry while holding);
  - each position: ret_net = -fwd_H - cost + funding_to_exit (short receives +funding; funding asof-cumsum);
  - simulation: process entries in time order, max_active CONCURRENT positions (skip when full), equal weight
    w; realize each position's P&L at exit -> daily P&L -> compounding equity -> MAR/DD/worst-day/early-recent.
MAR (= total_ret/maxDD) is ~weight-invariant; it is the comparable headline vs the daily book (E2 age+rmom
MAR ~3-6). Holds {6,12}h x cost {15,45}bps x both venues. EXPLORATORY proxy (approx concurrency/fills); NOT
promotion evidence. Funding was ~0 at this entry profile in P0c; included here for rigor.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1h_continuous_portfolio.py
"""
from __future__ import annotations

import heapq
import json
import statistics as st
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import glob  # noqa: E402

import polars as pl  # noqa: E402

from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)

SHARED = Path.home() / "SHARED_DATA"
START_DATE, END_DATE = "2023-04-01", "2026-05-28"
MS_DAY, MS_H = 86_400_000, 3_600_000
SPLIT = _date_str_to_ms("2025-06-01")
HOLDS = [6, 12]
COSTS_BPS = [15, 45]
WEIGHT = 0.02
MAX_ACTIVE = 25
VENUES = {"bybit": (SHARED / "bybit_full_pit", "funding"),
          "binance": (SHARED / "binance_full_pit", "binance_usdm_funding")}


def _entries(root: Path, ds: str, H: int) -> pl.DataFrame:
    """Fresh gap-aware D9 spell-starts (per-name cooldown=H) with fwd_H and funding-to-exit_H."""
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(root, kname, start_ms=_date_str_to_ms(START_DATE),
                     end_ms=_date_str_to_ms(END_DATE) + (H + 1) * MS_H, columns=["ts_ms", "symbol", "close"])
    k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    k = k.with_columns((pl.col("close").shift(-H).over("symbol") / pl.col("close") - 1.0).alias("fwdH"))
    d9 = pl.read_parquet(root / "_p1_continuous_panel.parquet").filter(pl.col("decile") == 9)
    d9 = d9.select("symbol", "ts_ms").sort(["symbol", "ts_ms"])
    d9 = d9.with_columns(
        ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_H).fill_null(True).alias("fresh")
    )
    fresh = d9.filter(pl.col("fresh")).join(k.select("symbol", "ts_ms", "fwdH"), on=["symbol", "ts_ms"], how="left")
    # funding-to-exit over [t, t+H]
    files = glob.glob(str(root / ds / "**" / "*.parquet"), recursive=True)
    if files:
        fund = pl.scan_parquet(files).select(["ts_ms", "symbol", "funding_rate"]).collect().drop_nulls()
        fund = fund.sort(["symbol", "ts_ms"]).with_columns(pl.col("funding_rate").cum_sum().over("symbol").alias("cf"))
        cf = fund.select("symbol", "ts_ms", "cf")
        fresh = fresh.sort(["symbol", "ts_ms"]).join_asof(cf, on="ts_ms", by="symbol", strategy="backward").rename({"cf": "cfe"})
        fresh = fresh.with_columns((pl.col("ts_ms") + H * MS_H).alias("tx")).sort(["symbol", "tx"])
        fresh = fresh.join_asof(cf.rename({"ts_ms": "txk", "cf": "cfx"}), left_on="tx", right_on="txk", by="symbol", strategy="backward")
        fresh = fresh.with_columns(pl.col("cfe").fill_null(0.0))
        fresh = fresh.with_columns((pl.col("cfx").fill_null(pl.col("cfe")) - pl.col("cfe")).alias("fundH"))
    else:
        fresh = fresh.with_columns(pl.lit(0.0).alias("fundH"))
    fresh = fresh.drop_nulls("fwdH").sort(["symbol", "ts_ms"])
    # per-name cooldown = H (no re-entry while holding)
    sy = fresh["symbol"].to_list()
    ts = fresh["ts_ms"].to_list()
    keep = []
    last: dict[str, int] = {}
    for i in range(len(sy)):
        if sy[i] not in last or ts[i] - last[sy[i]] >= H * MS_H:
            keep.append(i)
            last[sy[i]] = ts[i]
    return fresh[keep].select("symbol", "ts_ms", "fwdH", "fundH")


def _simulate(ev: pl.DataFrame, H: int, cost: float) -> dict:
    """Concurrent-position sim: max_active cap, equal weight; realize P&L at exit -> daily series -> stats."""
    rows = ev.with_columns((-pl.col("fwdH") + pl.col("fundH") - cost).alias("rn")).sort("ts_ms")
    tser = rows["ts_ms"].to_list()
    rn = rows["rn"].to_list()
    active: list[int] = []  # heap of exit_ts
    realized: list[tuple[int, float]] = []
    taken = skipped = 0
    for te, r in zip(tser, rn):
        tx = te + H * MS_H
        while active and active[0] <= te:
            heapq.heappop(active)
        if len(active) < MAX_ACTIVE:
            heapq.heappush(active, tx)
            realized.append((tx, WEIGHT * r))
            taken += 1
        else:
            skipped += 1
    if not realized:
        return {"n": 0}
    # daily P&L -> compounding equity
    realized.sort()
    daily: dict[int, float] = {}
    for tx, p in realized:
        d = (tx // MS_DAY) * MS_DAY
        daily[d] = daily.get(d, 0.0) + p
    days = sorted(daily)
    pnl = [daily[d] for d in days]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for p in pnl:
        eq *= (1 + p)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    tot = eq - 1.0

    def _ret(lo, hi):
        seg = [daily[d] for d in days if lo <= d < hi]
        e = 1.0
        for p in seg:
            e *= (1 + p)
        return (e - 1.0) * 100

    sharpe = (st.mean(pnl) / st.pstdev(pnl) * (365 ** 0.5)) if len(pnl) > 2 and st.pstdev(pnl) > 0 else None
    return {
        "n_trades": taken, "skipped": skipped, "skip_frac": round(skipped / (taken + skipped), 3),
        "total_ret_pct": round(tot * 100, 1), "max_dd_pct": round(mdd * 100, 1),
        "mar": round(tot / mdd, 2) if mdd > 0 else None, "worst_day_pct": round(min(pnl) * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe else None,
        "early_ret_pct": round(_ret(0, SPLIT), 1), "recent_ret_pct": round(_ret(SPLIT, 1 << 62), 1),
        "n_days": len(days),
    }


def main() -> int:
    out: dict = {}
    for venue, (root, ds) in VENUES.items():
        if not (root / "_p1_continuous_panel.parquet").exists():
            print(f"SKIP {venue}: no cached panel")
            continue
        out[venue] = {}
        for H in HOLDS:
            print(f"[{venue}] H={H}h: build fresh entries (+funding) ...", flush=True)
            ev = _entries(root, ds, H)
            for c in COSTS_BPS:
                r = _simulate(ev, H, c / 1e4)
                out[venue][f"H{H}_cost{c}"] = r
                if r.get("n_trades"):
                    print(f"[{venue}] H{H}h {c}bps  MAR={r['mar']} ret={r['total_ret_pct']}% DD={r['max_dd_pct']}% "
                          f"worst={r['worst_day_pct']}% Sh={r['sharpe']} early={r['early_ret_pct']}% "
                          f"recent={r['recent_ret_pct']}% n={r['n_trades']} skip={r['skip_frac']}", flush=True)
        print(flush=True)
    (SHARED / "p1h_continuous_portfolio_2026-06-01.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p1h_continuous_portfolio_2026-06-01.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
