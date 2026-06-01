"""Phase 1 / Avenue D — SANE portfolio proxy for the LIQUID-universe continuous strategy.

p1i overturned the capacity verdict: the fade is STRONGEST on liquid names (>$1M/h: 6h +118/+93 bps
bybit/binance), all-weather, with real capacity (~$1.4-1.8M @1%). This converts that per-trade signal into a
portfolio number, fixing p1h's two bugs: (1) ADDITIVE returns (no compounding explosion); (2) realistic
LIQUID-name cost (~30 bps RT, not 15-bps mid). Restricts entries to liquid names (entry turnover >= threshold),
concurrent-position sim (max_active, equal weight), funding-to-exit. MAR = arithmetic_annual_return / maxDD
on the additive equity (weight-invariant). Both venues, early/recent. EXPLORATORY proxy; NOT promotion evidence.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1j_liquid_portfolio.py
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
YEARS = (_date_str_to_ms(END_DATE) - _date_str_to_ms(START_DATE)) / (365.25 * MS_DAY)
# (hold_h, liq_threshold_usd_per_h, cost_bps)
CELLS = [(6, 500_000, 30), (6, 500_000, 50), (6, 1_000_000, 30), (12, 500_000, 30), (12, 1_000_000, 30)]
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
    return fresh.drop_nulls(["fwdH", "turnover_quote"])


def _sim(ev: pl.DataFrame, H: int, liq: float, cost: float) -> dict:
    rows = ev.filter(pl.col("turnover_quote") >= liq).with_columns(
        (-pl.col("fwdH") + pl.col("fundH") - cost).alias("rn")).sort("ts_ms")
    # per-name cooldown=H
    sy, tsl, rnl = rows["symbol"].to_list(), rows["ts_ms"].to_list(), rows["rn"].to_list()
    last: dict[str, int] = {}
    ev2 = []
    for s, t, r in zip(sy, tsl, rnl):
        if s not in last or t - last[s] >= H * MS_H:
            ev2.append((t, r))
            last[s] = t
    active: list[int] = []
    daily: dict[int, float] = {}
    taken = skipped = 0
    for t, r in ev2:
        tx = t + H * MS_H
        while active and active[0] <= t:
            heapq.heappop(active)
        if len(active) < MAX_ACTIVE:
            heapq.heappush(active, tx)
            d = (tx // MS_DAY) * MS_DAY
            daily[d] = daily.get(d, 0.0) + WEIGHT * r
            taken += 1
        else:
            skipped += 1
    if not daily:
        return {"n": 0}
    days = sorted(daily)
    pnl = [daily[d] for d in days]
    # ADDITIVE equity (no compounding)
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    total = eq
    ann = total / YEARS
    sharpe = (st.mean(pnl) / st.pstdev(pnl) * (365 ** 0.5)) if len(pnl) > 2 and st.pstdev(pnl) > 0 else None
    early = sum(daily[d] for d in days if d < SPLIT)
    recent = sum(daily[d] for d in days if d >= SPLIT)
    return {"n_trades": taken, "skip_frac": round(skipped / (taken + skipped), 3),
            "ann_ret_pct": round(ann * 100, 1), "total_ret_pct": round(total * 100, 1),
            "max_dd_pct": round(mdd * 100, 2), "mar": round(ann / mdd, 2) if mdd > 0 else None,
            "sharpe": round(sharpe, 2) if sharpe else None,
            "early_pnl_pct": round(early * 100, 1), "recent_pnl_pct": round(recent * 100, 1)}


def main() -> int:
    out: dict = {}
    for venue, (root, ds) in VENUES.items():
        if not (root / "_p1_continuous_panel.parquet").exists():
            print(f"SKIP {venue}")
            continue
        out[venue] = {}
        print(f"[{venue}] additive portfolio proxy on the LIQUID universe (weight {WEIGHT}, max_active {MAX_ACTIVE}):", flush=True)
        cache: dict[int, pl.DataFrame] = {}
        for H, liq, cost in CELLS:
            if H not in cache:
                print(f"[{venue}]  build entries H={H}h ...", flush=True)
                cache[H] = _entries(root, ds, H)
            r = _sim(cache[H], H, liq, cost / 1e4)
            tag = f"H{H}_liq{int(liq/1000)}k_c{cost}"
            out[venue][tag] = r
            if r.get("n_trades"):
                print(f"[{venue}]  {tag:18s} MAR={r['mar']} ann={r['ann_ret_pct']}%/yr DD={r['max_dd_pct']}% "
                      f"Sh={r['sharpe']} early={r['early_pnl_pct']}% recent={r['recent_pnl_pct']}% "
                      f"n={r['n_trades']} skip={r['skip_frac']}", flush=True)
        print(flush=True)
    (SHARED / "p1j_liquid_portfolio_2026-06-01.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p1j_liquid_portfolio_2026-06-01.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
