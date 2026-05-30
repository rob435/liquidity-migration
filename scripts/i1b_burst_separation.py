#!/usr/bin/env python3
"""I1b — can a PIT-causal intraday signal SEPARATE fading pumps from continuing pumps? (EXPLORATORY)

The make-or-break for a purpose-built intraday short selector. I1a showed faders carry an
intraday exhaustion fingerprint — but a volume-climax-with-a-wick happens at the top of EVERY
sharp pump, including the ones that keep running and squeeze a short (the RD1 failure). So we
must scan ALL intraday pumps (incl. ones that never became daily events) and ask: at the burst,
does any PIT-causal feature tell faders from continuers?

Method (per venue, in-memory on the projected klines panel; PIT-causal):
  - universe: mid-liquidity band (prior-7d-avg-turnover cross-sectional rank in [RANK_MIN,RANK_MAX])
    + age >= AGE_MIN days. (Excludes majors; matches the strategy's universe in spirit.)
  - burst trigger at hour h (causal, info through h): intraday_gain = close_h/day_open-1 >= G
    AND vol_spike = turnover_h / (prior7d_avg_daily_turnover/24) >= M. First h per (symbol,day);
    cross-day cooldown COOLDOWN_D. This fires DURING the pump (near the peak), NOT at the slow
    daily-cumulative confirmation that K1a used.
  - forward label (the short): entry at h+1 open; fwd_ret = close(h+1+H)/entry-1; short_pnl=-fwd_ret;
    fade = fwd_ret<0. H = FWD_H hours.
  - features at the burst (all causal at h): gain, vol_spike, trailing-3h return (velocity),
    acceleration, intrabar upper-wick + close-location (rejection), market context
    (mkt_median_ret, mkt_pct_up, btc_ret that day), idiosyncratic strength = gain - mkt_median
    (RD1: lone-wolf high-idio => continues/squeezes), hour-of-day, gain magnitude.
  - separation: per feature, Spearman corr(feature, fwd_ret) [negative => predicts the fade] and
    short_pnl by feature quintile. Reported per venue + EARLY/RECENT. Cross-venue agreement is
    the bar; the full distribution is reported, not a winner. Derivative channels (premium/OI/
    funding) are a targeted follow-up (I1b-ext). EXPLORATORY — never promotion evidence.
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

import polars as pl

MS_H = 3_600_000
MS_D = 86_400_000


def _load_panel(root: Path) -> pl.DataFrame:
    fs = glob.glob(str(root / "klines_1h" / "**" / "*.parquet"), recursive=True)
    if not fs:
        raise SystemExit(f"no klines_1h under {root}")
    df = pl.scan_parquet(fs).select(["ts_ms", "symbol", "open", "high", "low", "close", "turnover_quote"]).collect()
    return df.with_columns([
        (pl.col("ts_ms") // MS_D).alias("day_idx"),
        ((pl.col("ts_ms") % MS_D) // MS_H).alias("hod"),
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="I1b burst fade/continue separation (read-only).")
    ap.add_argument("--root", required=True)
    ap.add_argument("--venue", default="?")
    ap.add_argument("--age-min", type=int, default=300)
    ap.add_argument("--rank-min", type=int, default=31)
    ap.add_argument("--rank-max", type=int, default=400)
    ap.add_argument("--gain-min", type=float, default=0.08)
    ap.add_argument("--vol-spike-min", type=float, default=5.0)
    ap.add_argument("--cooldown-days", type=int, default=3)
    ap.add_argument("--fwd-h", type=int, default=48)
    ap.add_argument("--split-date", default="2025-06-01")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args()

    panel = _load_panel(Path(args.root).expanduser())
    # --- daily panel ---
    daily = (panel.sort("ts_ms").group_by(["symbol", "day_idx"]).agg([
        pl.col("open").first().alias("day_open"),
        pl.col("close").last().alias("day_close"),
        pl.col("turnover_quote").sum().alias("day_turn"),
    ]).with_columns((pl.col("day_close") / pl.col("day_open") - 1.0).alias("day_ret")))
    # symbol age (first day_idx)
    first_day = daily.group_by("symbol").agg(pl.col("day_idx").min().alias("first_day"))
    daily = daily.join(first_day, on="symbol")
    # prior-7d avg daily turnover (causal: shift 1, mean of prior 7) + its cross-sectional rank per day
    daily = daily.sort(["symbol", "day_idx"]).with_columns(
        pl.col("day_turn").shift(1).rolling_mean(window_size=7, min_samples=3).over("symbol").alias("prior7_turn"))
    daily = daily.with_columns(
        pl.col("prior7_turn").rank(method="ordinal", descending=True).over("day_idx").alias("liq_rank"))
    # market aggregates per day
    mkt = daily.group_by("day_idx").agg([
        pl.col("day_ret").median().alias("mkt_median_ret"),
        (pl.col("day_ret") > 0).mean().alias("mkt_pct_up"),
    ])
    # market FORWARD return over the burst's forward window (~fwd_h hours => nd days) for
    # beta-neutralisation: is the pump fading RELATIVE to the market, or just market-timing?
    nd = max(1, round(args.fwd_h / 24))
    mkt_fwd_expr = pl.col("mkt_median_ret").shift(-1)
    for i in range(2, nd + 1):
        mkt_fwd_expr = mkt_fwd_expr + pl.col("mkt_median_ret").shift(-i)
    mkt = mkt.sort("day_idx").with_columns(mkt_fwd_expr.alias("mkt_fwd"))
    # BTC daily return for context
    btc = daily.filter(pl.col("symbol") == "BTCUSDT").select(["day_idx", pl.col("day_ret").alias("btc_ret")])

    # --- burst scan: join hourly panel to daily(day_open, prior7_turn, liq_rank, age, mkt, btc) ---
    dsel = daily.select(["symbol", "day_idx", "day_open", "prior7_turn", "liq_rank", "first_day"])
    h = (panel.join(dsel, on=["symbol", "day_idx"], how="inner")
         .join(mkt, on="day_idx", how="left").join(btc, on="day_idx", how="left"))
    h = h.with_columns([
        (pl.col("close") / pl.col("day_open") - 1.0).alias("gain"),
        pl.when(pl.col("prior7_turn") > 0)
          .then(pl.col("turnover_quote") / (pl.col("prior7_turn") / 24.0)).otherwise(0.0).alias("vol_spike"),
        ((pl.col("day_idx") - pl.col("first_day"))).alias("age_days"),
    ])
    cand = h.filter(
        (pl.col("gain") >= args.gain_min) & (pl.col("vol_spike") >= args.vol_spike_min)
        & (pl.col("age_days") >= args.age_min)
        & (pl.col("liq_rank") >= args.rank_min) & (pl.col("liq_rank") <= args.rank_max)
    )
    # first burst per (symbol, day)
    cand = cand.sort("ts_ms").group_by(["symbol", "day_idx"]).first()
    # cross-day cooldown per symbol (greedy in python on the small candidate set)
    cand = cand.sort(["symbol", "ts_ms"])
    keep = []
    last = {}
    for r in cand.iter_rows(named=True):
        s, d = r["symbol"], r["day_idx"]
        if s in last and d - last[s] < args.cooldown_days:
            continue
        last[s] = d
        keep.append(r)
    bursts = pl.DataFrame(keep)
    if bursts.is_empty():
        raise SystemExit("no bursts under these params")

    # intrabar + velocity features need neighbor bars -> join from panel by (symbol, ts)
    pk = panel.select(["symbol", "ts_ms", "open", "high", "low", "close"])
    def at(dt_ms, cols, suffix):
        j = pk.select(["symbol", "ts_ms"] + cols).rename({c: f"{c}{suffix}" for c in cols})
        return bursts.with_columns((pl.col("ts_ms") + dt_ms).alias("_jts")).join(
            j, left_on=["symbol", "_jts"], right_on=["symbol", "ts_ms"], how="left").drop("_jts")
    bursts = at(0, ["high", "low", "open", "close"], "_0")
    bursts = bursts.with_columns([
        pl.when((pl.col("high_0") - pl.col("low_0")) > 0)
          .then((pl.col("high_0") - pl.max_horizontal("open_0", "close_0")) / (pl.col("high_0") - pl.col("low_0")))
          .otherwise(0.0).alias("wick"),
        pl.when((pl.col("high_0") - pl.col("low_0")) > 0)
          .then((pl.col("close_0") - pl.col("low_0")) / (pl.col("high_0") - pl.col("low_0")))
          .otherwise(0.5).alias("close_loc"),
        (pl.col("gain") - pl.col("mkt_median_ret")).alias("idio"),
    ])
    # velocity: close_h / close_{h-3} - 1 ; accel vs prior 3h
    c3 = pk.select(["symbol", "ts_ms", "close"]).rename({"close": "c_m3"})
    c6 = pk.select(["symbol", "ts_ms", "close"]).rename({"close": "c_m6"})
    bursts = bursts.with_columns((pl.col("ts_ms") - 3 * MS_H).alias("_t3"), (pl.col("ts_ms") - 6 * MS_H).alias("_t6"))
    bursts = bursts.join(c3, left_on=["symbol", "_t3"], right_on=["symbol", "ts_ms"], how="left")
    bursts = bursts.join(c6, left_on=["symbol", "_t6"], right_on=["symbol", "ts_ms"], how="left").drop("_t3", "_t6")
    bursts = bursts.with_columns([
        pl.when(pl.col("c_m3") > 0).then(pl.col("close") / pl.col("c_m3") - 1.0).otherwise(None).alias("vel3"),
        pl.when((pl.col("c_m3") > 0) & (pl.col("c_m6") > 0))
          .then((pl.col("close") / pl.col("c_m3") - 1.0) - (pl.col("c_m3") / pl.col("c_m6") - 1.0)).otherwise(None).alias("accel"),
    ])
    # forward label
    ent = pk.select(["symbol", "ts_ms", "open"]).rename({"open": "entry_open"})
    fwd = pk.select(["symbol", "ts_ms", "close"]).rename({"close": "fwd_close"})
    bursts = bursts.with_columns((pl.col("ts_ms") + MS_H).alias("_et"), (pl.col("ts_ms") + (1 + args.fwd_h) * MS_H).alias("_ft"))
    bursts = bursts.join(ent, left_on=["symbol", "_et"], right_on=["symbol", "ts_ms"], how="left")
    bursts = bursts.join(fwd, left_on=["symbol", "_ft"], right_on=["symbol", "ts_ms"], how="left").drop("_et", "_ft")
    bursts = bursts.filter((pl.col("entry_open") > 0) & pl.col("fwd_close").is_not_null()).with_columns([
        (pl.col("fwd_close") / pl.col("entry_open") - 1.0).alias("fwd_ret"),
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("d"),
    ]).with_columns((-pl.col("fwd_ret")).alias("short_pnl"))
    # beta-neutral forward return = coin fwd_ret - market fwd return over the same window
    bursts = bursts.with_columns((pl.col("fwd_ret") - pl.col("mkt_fwd")).alias("fwd_ret_neutral"))

    feats = ["gain", "vol_spike", "vel3", "accel", "wick", "close_loc", "idio",
             "mkt_median_ret", "mkt_pct_up", "btc_ret", "hod"]
    def sep(df: pl.DataFrame) -> dict:
        if df.height < 30:
            return {"n": df.height}
        dn = df.filter(pl.col("fwd_ret_neutral").is_not_null())
        out = {"n": df.height, "base_fade_rate": round((df["fwd_ret"] < 0).mean(), 3),
               "mean_short_pnl_pct": round(df["short_pnl"].mean() * 100, 2),
               "base_fade_rate_neutral": round((dn["fwd_ret_neutral"] < 0).mean(), 3) if dn.height else None,
               "mean_neutral_short_pnl_pct": round((-dn["fwd_ret_neutral"]).mean() * 100, 2) if dn.height else None}
        ic = {}
        for f in feats:
            sub = df.filter(pl.col(f).is_not_null())
            if sub.height < 30:
                continue
            c = sub.select(pl.corr(pl.col(f).rank(), pl.col("fwd_ret").rank())).item()
            subn = sub.filter(pl.col("fwd_ret_neutral").is_not_null())
            cn = subn.select(pl.corr(pl.col(f).rank(), pl.col("fwd_ret_neutral").rank())).item() if subn.height >= 30 else None
            # short_pnl (raw and beta-neutral) in top vs bottom quintile of the feature
            q = sub.with_columns(pl.col(f).qcut(5, labels=["q1", "q2", "q3", "q4", "q5"], allow_duplicates=True).alias("qb"))
            qm = {r["qb"]: round(r["m"] * 100, 2) for r in q.group_by("qb").agg(pl.col("short_pnl").mean().alias("m")).iter_rows(named=True)}
            qn = {r["qb"]: round(r["m"] * 100, 2) for r in q.group_by("qb").agg((-pl.col("fwd_ret_neutral")).mean().alias("m")).iter_rows(named=True)}
            ic[f] = {"ic_raw": round(c, 3) if c is not None else None,
                     "ic_neutral": round(cn, 3) if cn is not None else None,
                     "raw_q1_pct": qm.get("q1"), "raw_q5_pct": qm.get("q5"),
                     "neutral_q1_pct": qn.get("q1"), "neutral_q5_pct": qn.get("q5")}
        out["features"] = ic
        return out

    early = bursts.filter(pl.col("d") < args.split_date)
    recent = bursts.filter(pl.col("d") >= args.split_date)
    res = {"venue": args.venue, "params": {"gain_min": args.gain_min, "vol_spike_min": args.vol_spike_min,
           "rank": [args.rank_min, args.rank_max], "age_min": args.age_min, "fwd_h": args.fwd_h, "cooldown_d": args.cooldown_days},
           "n_bursts": bursts.height, "ALL": sep(bursts), "EARLY": sep(early), "RECENT": sep(recent)}
    print(json.dumps(res, indent=2))
    if args.output_json:
        Path(args.output_json).expanduser().write_text(json.dumps(res, indent=2))
        bursts.select(["symbol", "d", "hod", "gain", "vol_spike", "vel3", "wick", "close_loc", "idio",
                       "mkt_median_ret", "mkt_pct_up", "btc_ret", "fwd_ret", "short_pnl", "fwd_ret_neutral"]).write_csv(
            Path(args.output_json).expanduser().with_suffix(".bursts.csv"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
