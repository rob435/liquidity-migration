#!/usr/bin/env python3
"""Book-level regime concentration: how much of the whole book is ONE BTC-trend trade?

All three sleeves condition on a BTC regime indicator:
  - SHORT  (deployed): entries require btc_return_30d > 0 (trailing 30d return, causal)
  - LONG   (deployed): FC requires BTC close > SMA30 AND ETH close > SMA30
  - CONTINUOUS (research, frozen): uptrend ensemble on btc 30d uptrend (the 40% downtrend
    add-on trades the complement, premium-gated)

This script quantifies the joint exposure:
  1. per-day regime indicators for each sleeve definition (lag-1, causal);
  2. overlap stats: % days each is on, pairwise, and ALL-ON simultaneously; flip count;
  3. joint deployed-book P&L (short + long ledgers) conditional on regime state, around
     regime flips, and on the 10 worst BTC days in-window.

Analysis-only; writes a markdown report next to the short ledger.

    POLARS_MAX_THREADS=4 .venv/bin/python scripts/book_regime_concentration.py \
        --short-trades <dir>/volume_event_best_trades.csv \
        --long-trades  <dir>/long_native_trades.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

MS_PER_DAY = 86_400_000


def _daily_closes(root: Path, symbol: str, start: str, end: str) -> pl.DataFrame:
    lf = pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"), hive_partitioning=True)
    k = (
        lf.filter(
            (pl.col("symbol").cast(pl.Utf8) == symbol)
            & (pl.col("date").cast(pl.Utf8) >= start)
            & (pl.col("date").cast(pl.Utf8) < end)
        )
        .select(pl.col("date").cast(pl.Utf8), "ts_ms", "close")
        .collect(engine="streaming")
    )
    return (
        k.sort("ts_ms")
        .group_by("date", maintain_order=True)
        .agg(pl.col("close").last().alias("close"))
        .sort("date")
    )


def _regimes(btc: pl.DataFrame, eth: pl.DataFrame) -> pl.DataFrame:
    """Per-day causal (lag-1) regime indicators for each sleeve's definition."""
    b = btc.with_columns(
        (pl.col("close").shift(1) / pl.col("close").shift(31) - 1.0).alias("btc_ret30_lag1"),
        (pl.col("close").rolling_mean(30).shift(1)).alias("btc_sma30_lag1"),
        pl.col("close").shift(1).alias("btc_close_lag1"),
    )
    e = eth.with_columns(
        (pl.col("close").rolling_mean(30).shift(1)).alias("eth_sma30_lag1"),
        pl.col("close").shift(1).alias("eth_close_lag1"),
    ).select("date", "eth_sma30_lag1", "eth_close_lag1")
    out = b.join(e, on="date", how="inner").with_columns(
        (pl.col("btc_ret30_lag1") > 0).alias("short_on"),          # short gate (and continuous uptrend sleeve)
        ((pl.col("btc_close_lag1") > pl.col("btc_sma30_lag1"))
         & (pl.col("eth_close_lag1") > pl.col("eth_sma30_lag1"))).alias("long_on"),
    )
    return out.with_columns(
        (pl.col("short_on")).alias("continuous_up_on"),
        (pl.col("short_on") & pl.col("long_on")).alias("both_deployed_on"),
    ).drop_nulls(["short_on", "long_on"])


def _daily_pnl_from_trades(trades: pl.DataFrame, ret_col: str, exit_ts_col: str) -> pl.DataFrame:
    return (
        trades.with_columns(
            (pl.from_epoch((pl.col(exit_ts_col) - 1), time_unit="ms").dt.strftime("%Y-%m-%d")).alias("date")
        )
        .group_by("date")
        .agg(pl.col(ret_col).sum().alias("pnl"))
        .sort("date")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path.home() / "SHARED_DATA" / "bybit_full_pit"))
    ap.add_argument("--short-trades", required=True, help="short-sleeve trade ledger CSV (UNGATED baseline preferred)")
    ap.add_argument("--long-trades", required=True, help="long-sleeve trade ledger CSV")
    ap.add_argument("--start", default="2023-04-01")
    ap.add_argument("--end", default="2026-05-28")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    btc = _daily_closes(root, "BTCUSDT", "2023-01-01", args.end)  # warmup before window
    eth = _daily_closes(root, "ETHUSDT", "2023-01-01", args.end)
    reg = _regimes(btc, eth).filter(pl.col("date") >= args.start)

    n = reg.height
    short_on = int(reg["short_on"].sum())
    long_on = int(reg["long_on"].sum())
    both_on = int(reg["both_deployed_on"].sum())
    flips_short = int((reg["short_on"].cast(pl.Int8).diff().abs() > 0).sum())
    flips_long = int((reg["long_on"].cast(pl.Int8).diff().abs() > 0).sum())
    # conditional overlap: P(long_on | short_on)
    p_long_given_short = both_on / short_on if short_on else float("nan")

    st = pl.read_csv(Path(args.short_trades).expanduser())
    lt = pl.read_csv(Path(args.long_trades).expanduser())
    s_ret = "net_return" if "net_return" in st.columns else "strategy_return"
    s_exit = "exit_ts_ms" if "exit_ts_ms" in st.columns else "exit_time_ms"
    sp = _daily_pnl_from_trades(st, s_ret, s_exit).rename({"pnl": "short_pnl"})
    lp = _daily_pnl_from_trades(lt, "net_return", "exit_ts_ms").rename({"pnl": "long_pnl"})

    j = (
        reg.select("date", "short_on", "long_on", "both_deployed_on", "btc_ret30_lag1")
        .join(sp, on="date", how="left")
        .join(lp, on="date", how="left")
        .with_columns(pl.col("short_pnl").fill_null(0.0), pl.col("long_pnl").fill_null(0.0))
        .with_columns((pl.col("short_pnl") + pl.col("long_pnl")).alias("book_pnl"))
    )

    # BTC daily return (raw, not lagged) for worst-day conditioning
    braw = btc.with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("btc_ret_1d")).select("date", "btc_ret_1d")
    j = j.join(braw, on="date", how="left")
    worst10 = j.filter(pl.col("date") >= args.start).sort("btc_ret_1d").head(10)

    # regime-flip windows: +-5 days around each short_on flip
    flips = j.with_columns(pl.col("short_on").cast(pl.Int8).diff().abs().alias("_f")).with_row_index()
    flip_idx = flips.filter(pl.col("_f") > 0)["index"].to_list()
    flip_window_pnl = []
    for i in flip_idx:
        w = j.slice(max(0, i - 5), 11)
        flip_window_pnl.append({
            "flip_date": j["date"][i],
            "to_state": "ON" if j["short_on"][i] else "OFF",
            "short_pnl_11d": float(w["short_pnl"].sum()),
            "long_pnl_11d": float(w["long_pnl"].sum()),
            "book_pnl_11d": float(w["book_pnl"].sum()),
        })

    lines: list[str] = []
    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    emit(f"=== BOOK REGIME CONCENTRATION ({args.start} .. {args.end}, {n} days) ===")
    emit(f"- SHORT regime-on  (btc_ret30>0, lag1)      : {short_on}/{n} = {short_on/n:.0%}")
    emit(f"- LONG  regime-on  (BTC&ETH > SMA30, lag1)  : {long_on}/{n} = {long_on/n:.0%}")
    emit(f"- BOTH deployed sleeves on simultaneously   : {both_on}/{n} = {both_on/n:.0%}")
    emit(f"- P(long_on | short_on)                     : {p_long_given_short:.0%}")
    emit(f"- continuous uptrend sleeve shares the short indicator (same {short_on/n:.0%}); its 40% downtrend add-on trades the complement")
    emit(f"- regime flips in-window: short {flips_short}, long {flips_long}  (≈ effective independent regime observations)")
    emit("")
    emit("=== JOINT DEPLOYED-BOOK P&L BY REGIME STATE (1x research scale, exit-day booked) ===")
    for state, df in (("both ON", j.filter(pl.col("both_deployed_on"))),
                      ("short ON only", j.filter(pl.col("short_on") & ~pl.col("long_on"))),
                      ("all OFF", j.filter(~pl.col("short_on") & ~pl.col("long_on")))):
        emit(f"- {state:<15}: days={df.height:>4}  short={float(df['short_pnl'].sum()):+8.2%}  "
             f"long={float(df['long_pnl'].sum()):+8.2%}  book={float(df['book_pnl'].sum()):+8.2%}")
    emit("")
    emit("=== 10 WORST BTC DAYS IN-WINDOW: JOINT BOOK P&L ===")
    emit("| date | BTC 1d | short pnl | long pnl | book |")
    emit("|---|---:|---:|---:|---:|")
    for r in worst10.to_dicts():
        emit(f"| {r['date']} | {r['btc_ret_1d']:+.1%} | {r['short_pnl']:+.2%} | {r['long_pnl']:+.2%} | {r['book_pnl']:+.2%} |")
    emit("")
    emit("=== REGIME-FLIP WINDOWS (±5 days around each short-gate flip) ===")
    emit("| flip date | to | short 11d | long 11d | book 11d |")
    emit("|---|---|---:|---:|---:|")
    for r in flip_window_pnl:
        emit(f"| {r['flip_date']} | {r['to_state']} | {r['short_pnl_11d']:+.2%} | {r['long_pnl_11d']:+.2%} | {r['book_pnl_11d']:+.2%} |")

    out_path = Path(args.out) if args.out else Path(args.short_trades).expanduser().parent / "book_regime_concentration.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
