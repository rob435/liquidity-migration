#!/usr/bin/env python3
"""I2 — realistic backtest of the intraday extreme-pump-burst short (CANDIDATE-grade).

Pre-reg: docs/preregistration/i2-intraday-burst-selector-2026-05-30.md. Gated on I1b PASS.
Does the I1b gross-forward separation survive realistic costs + stops + the +1h fill, as a
net-positive cross-venue all-weather strategy — and does the EXTREME selection beat shorting
ALL bursts? (Tier-3 residual / factor test is a follow-up via risk_model.)

Selector FROZEN from I1b (no tuning on the result):
  universe age>=300, liq-rank 31-400; burst = intraday gain>=0.08 AND hourly turnover >= 5x the
  prior-7d avg hour; first per (symbol,day); cooldown 3d. EXTREME = top tercile of the composite
  pump-extremity score z(idio)+z(vel3)+z(vol_spike) over the burst population (wick excluded =
  noise). Entry: short at burst_h+1 open (+1h fill). Exit: 12% stop (realistic fill -(12%+slip),
  slip default 2%) OR 48h max-hold close. Costs: 15 & 45 bps round-trip.

Stage A (per-trade, the make-or-break): net return extreme-vs-all, both venues, early/recent.
Stage B (portfolio proxy): equal-weight 2%, max_active 12/day by score -> equity, MAR, DD,
worst-day, per-third (P&L booked at entry day; holds <=48h so the approximation is mild).
READ-ONLY on the projected panel (in-memory). PIT: features causal at burst h; stop uses the
realized forward high over the hold (a pre-placed order, not look-ahead).
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
from pathlib import Path

import polars as pl

MS_H = 3_600_000
MS_D = 86_400_000


def _panel(root: Path) -> pl.DataFrame:
    fs = glob.glob(str(root / "klines_1h" / "**" / "*.parquet"), recursive=True)
    if not fs:
        raise SystemExit(f"no klines_1h under {root}")
    df = pl.scan_parquet(fs).select(["ts_ms", "symbol", "open", "high", "low", "close", "turnover_quote"]).collect()
    return df.with_columns((pl.col("ts_ms") // MS_D).alias("day_idx"))


def _bursts(panel: pl.DataFrame, a) -> pl.DataFrame:
    daily = (panel.sort("ts_ms").group_by(["symbol", "day_idx"]).agg([
        pl.col("open").first().alias("day_open"), pl.col("close").last().alias("day_close"),
        pl.col("turnover_quote").sum().alias("day_turn")])
        .with_columns((pl.col("day_close") / pl.col("day_open") - 1.0).alias("day_ret")))
    fd = daily.group_by("symbol").agg(pl.col("day_idx").min().alias("first_day"))
    daily = daily.join(fd, on="symbol").sort(["symbol", "day_idx"]).with_columns(
        pl.col("day_turn").shift(1).rolling_mean(window_size=7, min_samples=3).over("symbol").alias("prior7_turn"))
    daily = daily.with_columns(pl.col("prior7_turn").rank("ordinal", descending=True).over("day_idx").alias("liq_rank"))
    mkt = daily.group_by("day_idx").agg(pl.col("day_ret").median().alias("mkt_median_ret"))
    dsel = daily.select(["symbol", "day_idx", "day_open", "prior7_turn", "liq_rank", "first_day"])
    h = panel.join(dsel, on=["symbol", "day_idx"], how="inner").join(mkt, on="day_idx", how="left")
    h = h.with_columns([
        (pl.col("close") / pl.col("day_open") - 1.0).alias("gain"),
        pl.when(pl.col("prior7_turn") > 0).then(pl.col("turnover_quote") / (pl.col("prior7_turn") / 24.0)).otherwise(0.0).alias("vol_spike"),
        (pl.col("day_idx") - pl.col("first_day")).alias("age_days")])
    cand = h.filter((pl.col("gain") >= a.gain_min) & (pl.col("vol_spike") >= a.vol_spike_min)
                    & (pl.col("age_days") >= a.age_min) & (pl.col("liq_rank") >= a.rank_min) & (pl.col("liq_rank") <= a.rank_max))
    cand = cand.sort("ts_ms").group_by(["symbol", "day_idx"]).first().sort(["symbol", "ts_ms"])
    keep, last = [], {}
    for r in cand.iter_rows(named=True):
        if r["symbol"] in last and r["day_idx"] - last[r["symbol"]] < a.cooldown_days:
            continue
        last[r["symbol"]] = r["day_idx"]
        keep.append(r)
    b = pl.DataFrame(keep)
    # velocity (close_h/close_{h-3}-1) + idio
    c3 = panel.select(["symbol", "ts_ms", "close"]).rename({"close": "c3"})
    b = b.with_columns((pl.col("ts_ms") - 3 * MS_H).alias("_t3")).join(c3, left_on=["symbol", "_t3"], right_on=["symbol", "ts_ms"], how="left").drop("_t3")
    b = b.with_columns([
        pl.when(pl.col("c3") > 0).then(pl.col("close") / pl.col("c3") - 1.0).otherwise(None).alias("vel3"),
        (pl.col("gain") - pl.col("mkt_median_ret")).alias("idio")])
    return b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--venue", default="?")
    ap.add_argument("--age-min", type=int, default=300)
    ap.add_argument("--rank-min", type=int, default=31)
    ap.add_argument("--rank-max", type=int, default=400)
    ap.add_argument("--gain-min", type=float, default=0.08)
    ap.add_argument("--vol-spike-min", type=float, default=5.0)
    ap.add_argument("--cooldown-days", type=int, default=3)
    ap.add_argument("--hold-h", type=int, default=48)
    ap.add_argument("--stop-pct", type=float, default=0.12)
    ap.add_argument("--stop-slip", type=float, default=0.02)
    ap.add_argument("--funding-ds", default="", help="funding dataset (bybit: funding | binance: binance_usdm_funding) to overlay realized funding over the hold; short receives positive funding")
    ap.add_argument("--funding-filter-floor", type=float, default=None, help="PIT crowded-short filter: skip trades whose trailing funding rate at entry < FLOOR (deeply-negative funding = crowded short = short pays)")
    ap.add_argument("--split-date", default="2025-06-01")
    ap.add_argument("--max-active", type=int, default=12)
    ap.add_argument("--weight", type=float, default=0.02)
    ap.add_argument("--output-json", default=None)
    a = ap.parse_args()

    panel = _panel(Path(a.root).expanduser()).sort(["symbol", "ts_ms"])
    # forward max high over the hold window [entry, entry+hold_h] (row-based ~hold_h+1 bars)
    panel = panel.with_columns(
        pl.col("high").reverse().rolling_max(window_size=a.hold_h + 1, min_samples=1).reverse().over("symbol").alias("fwd_max_high"))
    b = _bursts(panel, a)

    # extreme composite score over the burst population (per venue)
    def z(col):
        m, s = b[col].mean(), b[col].std()
        return ((pl.col(col) - m) / s) if s and s > 0 else pl.lit(0.0)
    b = b.with_columns((z("idio") + z("vel3") + z("vol_spike")).alias("score"))
    thr = b["score"].quantile(2 / 3)
    b = b.with_columns((pl.col("score") >= thr).alias("extreme"))

    # entry (+1h open), forward max high at entry, exit close at +hold_h
    pk_o = panel.select(["symbol", "ts_ms", "open", "fwd_max_high"]).rename({"open": "entry_open"})
    pk_c = panel.select(["symbol", "ts_ms", "close"]).rename({"close": "exit_close"})
    b = b.with_columns((pl.col("ts_ms") + MS_H).alias("_et"), (pl.col("ts_ms") + (1 + a.hold_h) * MS_H).alias("_xt"))
    b = b.join(pk_o, left_on=["symbol", "_et"], right_on=["symbol", "ts_ms"], how="left")
    b = b.join(pk_c, left_on=["symbol", "_xt"], right_on=["symbol", "ts_ms"], how="left").drop("_et", "_xt")
    b = b.filter((pl.col("entry_open") > 0) & pl.col("exit_close").is_not_null() & pl.col("fwd_max_high").is_not_null())
    stop_trig = pl.col("entry_open") * (1 + a.stop_pct)
    b = b.with_columns([
        (pl.col("fwd_max_high") >= stop_trig).alias("stopped"),
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("d")])
    # gross short return: stopped -> -(stop+slip); else (entry-exit)/entry
    b = b.with_columns(
        pl.when(pl.col("stopped")).then(pl.lit(-(a.stop_pct + a.stop_slip)))
          .otherwise((pl.col("entry_open") - pl.col("exit_close")) / pl.col("entry_open")).alias("gross"))
    b = b.with_columns([
        (pl.col("gross") - 0.0015).alias("net15"), (pl.col("gross") - 0.0045).alias("net45"),
        # diagnostic: NO-STOP variant (always exit at +hold_h close) - is the 12% stop the killer?
        (((pl.col("entry_open") - pl.col("exit_close")) / pl.col("entry_open")) - 0.0015).alias("net15_nostop")])

    # funding overlay: short RECEIVES positive funding (longs pay shorts) over the hold [entry, exit].
    # Also a PIT-causal trailing_funding (last settlement <= entry) for a crowded-short filter.
    if a.funding_ds:
        ffiles = glob.glob(str(Path(a.root).expanduser() / a.funding_ds / "**" / "*.parquet"), recursive=True)
        fund = pl.scan_parquet(ffiles).select(["ts_ms", "symbol", "funding_rate"]).collect().sort(["symbol", "ts_ms"])
        fund = fund.with_columns(pl.col("funding_rate").cum_sum().over("symbol").alias("cf"))
        fmap = {}
        for s, df_ in fund.partition_by("symbol", as_dict=True).items():
            sk = s[0] if isinstance(s, tuple) else s
            fmap[sk] = (df_["ts_ms"].to_list(), df_["cf"].to_list(), df_["funding_rate"].to_list())
        ets = (b["ts_ms"] + MS_H).to_list()
        xts = (b["ts_ms"] + (1 + a.hold_h) * MS_H).to_list()
        syms = b["symbol"].to_list()
        fpnl, tfund = [], []
        for s, e, x in zip(syms, ets, xts):
            d = fmap.get(s)
            if d is None:
                fpnl.append(0.0)
                tfund.append(0.0)
                continue
            tsl, cfl, rl = d
            ie = bisect.bisect_right(tsl, e) - 1
            ix = bisect.bisect_right(tsl, x) - 1
            ce = cfl[ie] if ie >= 0 else 0.0
            cx = cfl[ix] if ix >= 0 else 0.0
            fpnl.append(cx - ce)  # sum of funding settlements in (entry, exit]; short receives + funding
            tfund.append(rl[ie] if ie >= 0 else 0.0)  # PIT: funding rate of the last settlement <= entry
        b = b.with_columns([pl.Series("funding_pnl", fpnl), pl.Series("trailing_funding", tfund)])
    else:
        b = b.with_columns([pl.lit(0.0).alias("funding_pnl"), pl.lit(0.0).alias("trailing_funding")])
    b = b.with_columns([(pl.col("net45") + pl.col("funding_pnl")).alias("net45f")])
    # crowded-short funding FILTER (PIT): skip coins already paying deeply-negative funding at entry
    if a.funding_filter_floor is not None:
        b = b.filter(pl.col("trailing_funding") >= a.funding_filter_floor)

    def stat(df):
        if df.height < 20:
            return {"n": df.height}
        return {"n": df.height, "frac_stopped": round(df["stopped"].mean(), 3),
                "net15_mean_pct": round(df["net15"].mean() * 100, 3), "net15_median_pct": round(df["net15"].median() * 100, 3),
                "net15_win_pct": round((df["net15"] > 0).mean() * 100, 1), "net15_sum_pct": round(df["net15"].sum() * 100, 1),
                "net45_mean_pct": round(df["net45"].mean() * 100, 3), "net45_sum_pct": round(df["net45"].sum() * 100, 1),
                "funding_pnl_mean_pct": round(df["funding_pnl"].mean() * 100, 3),
                "funding_pnl_median_pct": round(df["funding_pnl"].median() * 100, 3),
                "net45_with_funding_mean_pct": round(df["net45f"].mean() * 100, 3),
                "net45_with_funding_median_pct": round(df["net45f"].median() * 100, 3),
                "frac_funding_drag_gt1pct": round((df["funding_pnl"] < -0.01).mean(), 3),
                "net15_NOSTOP_mean_pct": round(df["net15_nostop"].mean() * 100, 3)}

    def by_split(df, tag):
        return {"ALL": stat(df), "EARLY": stat(df.filter(pl.col("d") < a.split_date)),
                "RECENT": stat(df.filter(pl.col("d") >= a.split_date)), "_tag": tag}

    ext = b.filter(pl.col("extreme"))
    res = {"venue": a.venue, "n_bursts": b.height, "params": vars(a),
           "stageA_per_trade": {"ALL_BURSTS": by_split(b, "all"), "EXTREME": by_split(ext, "extreme")}}

    # Stage B portfolio proxy: per entry-day, top max_active by score, equal weight; P&L booked at entry day
    def portfolio(df, costcol):
        # cap per day to max_active by score (short the strongest signals first)
        capped = (df.sort(["d", "score"], descending=[False, True]).group_by("d", maintain_order=True).head(a.max_active))
        daily = capped.group_by("d").agg((pl.col(costcol) * a.weight).sum().alias("pnl")).sort("d")
        pnl = daily["pnl"].to_list()
        eq, peak, mdd = 1.0, 1.0, 0.0
        for p in pnl:
            eq *= (1 + p)
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
        tot = eq - 1.0
        import statistics as st
        sharpe = (st.mean(pnl) / st.pstdev(pnl) * (365 ** 0.5)) if len(pnl) > 2 and st.pstdev(pnl) > 0 else None
        # thirds
        n = len(pnl)
        t = n // 3
        thirds = [round(sum(pnl[i:j]) * 100, 1) for i, j in [(0, t), (t, 2 * t), (2 * t, n)]] if n >= 3 else []
        return {"n_days": n, "total_ret_pct": round(tot * 100, 1), "max_dd_pct": round(mdd * 100, 1),
                "mar": round(tot / mdd, 2) if mdd > 0 else None, "worst_day_pct": round(min(pnl) * 100, 2) if pnl else None,
                "sharpe": round(sharpe, 2) if sharpe else None, "thirds_pnl_pct": thirds}
    res["stageB_portfolio_extreme_net15"] = portfolio(ext, "net15")
    res["stageB_portfolio_extreme_net45"] = portfolio(ext, "net45")
    res["stageB_portfolio_extreme_net45f"] = portfolio(ext, "net45f")  # WITH funding (portfolio caps outliers via weight)
    res["stageB_portfolio_allbursts_net15"] = portfolio(b, "net15")

    print(json.dumps(res, indent=2))
    if a.output_json:
        Path(a.output_json).expanduser().write_text(json.dumps(res, indent=2))
        b.select(["symbol", "d", "score", "extreme", "gain", "vel3", "vol_spike", "idio",
                  "stopped", "gross", "net15", "net45", "funding_pnl", "net45f"]).write_csv(Path(a.output_json).expanduser().with_suffix(".trades.csv"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
