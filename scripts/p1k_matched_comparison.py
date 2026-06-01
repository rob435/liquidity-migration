"""Phase 1 / Avenue D — matched-sizing: does any-hour CONTINUOUS entry beat a once-DAILY proxy (same signal)?

The p1j MAR (12-39) is partly de-concentrated sizing, not necessarily added edge over the daily. This isolates
the ENTRY-FREQUENCY value with everything else matched (same liquid universe, sizing 2%/max_active 25, cost,
funding). Three arms on the LIQUID (>=500k/h) universe:
  cont_6h   : enter fresh rmom-D9 at ANY hour, hold 6h
  cont_24h  : enter fresh rmom-D9 at ANY hour, hold 24h
  daily_24h : enter fresh rmom-D9 ONLY at 00:00-01:00 UTC (the daily-cadence proxy), hold 24h
If cont beats daily_24h at matched sizing -> any-hour entry adds real value (off-close breadth). If they
match -> continuous just trades turnover for breadth (re-size the daily instead). Additive returns; both
venues, early/recent. EXPLORATORY; NOT promotion evidence.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1k_matched_comparison.py
"""
from __future__ import annotations

import glob
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
WEIGHT, MAX_ACTIVE = 0.02, 25
LIQ, COST = 500_000, 30 / 1e4
YEARS = (_date_str_to_ms(END_DATE) - _date_str_to_ms(START_DATE)) / (365.25 * MS_DAY)
VENUES = {"bybit": (SHARED / "bybit_full_pit", "funding"),
          "binance": (SHARED / "binance_full_pit", "binance_usdm_funding")}


def _entries(root: Path, ds: str, H: int) -> pl.DataFrame:
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(root, kname, start_ms=_date_str_to_ms(START_DATE),
                     end_ms=_date_str_to_ms(END_DATE) + (H + 1) * MS_H, columns=["ts_ms", "symbol", "close", "turnover_quote"])
    k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    k = k.with_columns((pl.col("close").shift(-H).over("symbol") / pl.col("close") - 1.0).alias("fwdH"))
    d9 = pl.read_parquet(root / "_p1_continuous_panel.parquet").filter(pl.col("decile") == 9).select("symbol", "ts_ms").sort(["symbol", "ts_ms"])
    d9 = d9.with_columns(((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_H).fill_null(True).alias("fresh")).filter(pl.col("fresh"))
    fresh = d9.join(k.select("symbol", "ts_ms", "fwdH", "turnover_quote"), on=["symbol", "ts_ms"], how="left")
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
    fresh = fresh.drop_nulls(["fwdH", "turnover_quote"]).with_columns(((pl.col("ts_ms") % MS_DAY) // MS_H).alias("hod"))
    return fresh.filter(pl.col("turnover_quote") >= LIQ)


def _sim(ev: pl.DataFrame, H: int, daily_only: bool) -> dict:
    rows = ev
    if daily_only:
        rows = rows.filter(pl.col("hod").is_in([0, 1]))
    rows = rows.with_columns((-pl.col("fwdH") + pl.col("fundH") - COST).alias("rn")).sort("ts_ms")
    sy, tsl, rnl = rows["symbol"].to_list(), rows["ts_ms"].to_list(), rows["rn"].to_list()
    last: dict[str, int] = {}
    ev2 = []
    for s, t, r in zip(sy, tsl, rnl):
        if s not in last or t - last[s] >= H * MS_H:
            ev2.append((t, r))
            last[s] = t
    active: list[int] = []
    daily: dict[int, float] = {}
    taken = 0
    for t, r in ev2:
        tx = t + H * MS_H
        while active and active[0] <= t:
            heapq.heappop(active)
        if len(active) < MAX_ACTIVE:
            heapq.heappush(active, tx)
            d = (tx // MS_DAY) * MS_DAY
            daily[d] = daily.get(d, 0.0) + WEIGHT * r
            taken += 1
    if not daily:
        return {"n": 0}
    days = sorted(daily)
    pnl = [daily[d] for d in days]
    eq = peak = mdd = 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    ann = eq / YEARS
    sharpe = (st.mean(pnl) / st.pstdev(pnl) * (365 ** 0.5)) if len(pnl) > 2 and st.pstdev(pnl) > 0 else None
    return {"n_trades": taken, "ann_ret_pct": round(ann * 100, 1), "max_dd_pct": round(mdd * 100, 2),
            "mar": round(ann / mdd, 2) if mdd > 0 else None, "sharpe": round(sharpe, 2) if sharpe else None,
            "early_pnl_pct": round(sum(daily[d] for d in days if d < SPLIT) * 100, 1),
            "recent_pnl_pct": round(sum(daily[d] for d in days if d >= SPLIT) * 100, 1)}


def main() -> int:
    out: dict = {}
    for venue, (root, ds) in VENUES.items():
        if not (root / "_p1_continuous_panel.parquet").exists():
            print(f"SKIP {venue}")
            continue
        print(f"[{venue}] matched-sizing (liquid>=500k, 2% wt, max_active 25, 30bps):", flush=True)
        e6 = _entries(root, ds, 6)
        e24 = _entries(root, ds, 24)
        arms = {"cont_6h": _sim(e6, 6, False), "cont_24h": _sim(e24, 24, False), "daily_24h": _sim(e24, 24, True)}
        out[venue] = arms
        for name, r in arms.items():
            if r.get("n_trades"):
                print(f"[{venue}]  {name:10s} MAR={r['mar']:>6} ann={r['ann_ret_pct']:>6}%/yr DD={r['max_dd_pct']:>5}% "
                      f"Sh={r['sharpe']} early={r['early_pnl_pct']}% recent={r['recent_pnl_pct']}% n={r['n_trades']}", flush=True)
        print(flush=True)
    (SHARED / "p1k_matched_comparison_2026-06-01.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p1k_matched_comparison_2026-06-01.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
