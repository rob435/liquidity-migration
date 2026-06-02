"""Phase 1 / Avenue C+G — does the continuous market-neutral L/S DIVERSIFY the daily short? (combined book)

p1l found a beta-neutral continuous L/S (low-DD, uncorrelated-in-principle). The actionable question: does
ADDING it to the daily short improve the combined book's risk-adjusted return (like the long sleeve does)?
This is cheap + in-framework — I have proxies for both: the daily-short (D9 liquid @00:00-01:00, the daily
cadence) and the continuous L/S (D0-D9 liquid, all hours). Same per-ts daily-mean method for all -> apples-to-
apples correlation + combined MAR.

Caveat: both are CONTINUOUS-PANEL proxies (full-universe deciles ∩ liquid), NOT the deployed event-filter
daily strategy — informative for "does the L/S decorrelate from the close-entry short", not a live-book MAR.
They SHARE the D9 short leg, so expect positive correlation; the question is whether the L/S's D0 long leg +
beta-neutrality decorrelates it enough to add diversification value. EXPLORATORY; NOT promotion evidence.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1m_combined_portfolio.py
"""
from __future__ import annotations

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
LIQ, COST = 500_000, 30 / 1e4
YEARS = (_date_str_to_ms(END_DATE) - _date_str_to_ms(START_DATE)) / (365.25 * MS_DAY)
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}


def _stats(series: dict[int, float]) -> dict:
    if not series:
        return {"n": 0}
    days = sorted(series)
    pnl = [series[d] for d in days]
    eq = peak = mdd = 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    ann = eq / YEARS
    sh = (st.mean(pnl) / st.pstdev(pnl) * (365 ** 0.5)) if len(pnl) > 2 and st.pstdev(pnl) > 0 else None
    return {"ann_ret_pct": round(ann * 100, 1), "max_dd_pct": round(mdd * 100, 2),
            "mar": round(ann / mdd, 2) if mdd > 0 else None, "sharpe": round(sh, 2) if sh else None,
            "early_pct": round(sum(series[d] for d in days if d < SPLIT) * 100, 1),
            "recent_pct": round(sum(series[d] for d in days if d >= SPLIT) * 100, 1)}


def main() -> int:
    out: dict = {}
    for venue, root in VENUES.items():
        if not (root / "_p1_continuous_panel.parquet").exists():
            print(f"SKIP {venue}")
            continue
        print(f"[{venue}] cached panel + turnover ...", flush=True)
        kname = _autodetect_dataset_names(root)["klines_dataset"]
        k = _read_window(root, kname, start_ms=_date_str_to_ms(START_DATE), end_ms=_date_str_to_ms(END_DATE),
                         columns=["ts_ms", "symbol", "turnover_quote"])
        panel = pl.read_parquet(root / "_p1_continuous_panel.parquet").join(k, on=["symbol", "ts_ms"], how="left")
        panel = panel.filter(pl.col("turnover_quote") >= LIQ).with_columns(((pl.col("ts_ms") % MS_DAY) // MS_H).alias("hod"))
        dt = panel.filter(pl.col("decile").is_in([0, 9])).group_by(["ts_ms", "decile", "hod"]).agg(pl.col("fwd").mean().alias("m"))
        top = dt.filter(pl.col("decile") == 9).select("ts_ms", "hod", pl.col("m").alias("d9"))
        bot = dt.filter(pl.col("decile") == 0).select("ts_ms", pl.col("m").alias("d0"))
        j = top.join(bot, on="ts_ms", how="inner").with_columns(
            (-pl.col("d9") - COST).alias("short_ret"), (pl.col("d0") - pl.col("d9") - 2 * COST).alias("ls_ret"))

        def _series(frame: pl.DataFrame, col: str) -> dict[int, float]:
            d = (frame.with_columns(((pl.col("ts_ms") // MS_DAY) * MS_DAY).alias("day"))
                 .group_by("day").agg(pl.col(col).mean().alias("r")).drop_nulls("r"))
            return {int(r["day"]): float(r["r"]) for r in d.iter_rows(named=True)}

        daily_short = _series(j.filter(pl.col("hod").is_in([0, 1])), "short_ret")
        cont_short = _series(j, "short_ret")
        cont_ls = _series(j, "ls_ret")
        # correlations on aligned days
        common = sorted(set(daily_short) & set(cont_ls) & set(cont_short))
        ds = [daily_short[d] for d in common]
        cs = [cont_short[d] for d in common]
        cl = [cont_ls[d] for d in common]
        corr_ds_cs = round(float(pl.DataFrame({"a": ds, "b": cs}).select(pl.corr("a", "b")).item()), 3)
        corr_ds_cl = round(float(pl.DataFrame({"a": ds, "b": cl}).select(pl.corr("a", "b")).item()), 3)
        print(f"[{venue}] corr(daily_short, cont_short)={corr_ds_cs}  corr(daily_short, cont_LS)={corr_ds_cl}", flush=True)
        print(f"[{venue}]  daily_short    : {_stats(daily_short)}", flush=True)
        print(f"[{venue}]  cont_LS        : {_stats(cont_ls)}", flush=True)
        # combined: daily_short + w*cont_LS
        combos = {}
        for w in (0.0, 0.5, 1.0, 2.0):
            comb = {d: daily_short[d] + w * cont_ls.get(d, 0.0) for d in daily_short}
            s = _stats(comb)
            combos[f"w{w}"] = s
            tag = " (daily-alone)" if w == 0 else ""
            print(f"[{venue}]  daily+{w}*LS   MAR={s['mar']} ann={s['ann_ret_pct']}% DD={s['max_dd_pct']}% Sh={s['sharpe']}{tag}", flush=True)
        out[venue] = {"corr_daily_contshort": corr_ds_cs, "corr_daily_contLS": corr_ds_cl,
                      "daily_short": _stats(daily_short), "cont_LS": _stats(cont_ls), "combined": combos}
        print(flush=True)
    (SHARED / "p1m_combined_portfolio_2026-06-01.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p1m_combined_portfolio_2026-06-01.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
