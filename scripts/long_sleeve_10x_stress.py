#!/usr/bin/env python3
"""Levered long-sleeve stress: what does the LIVE 10x execution multiplier do to the
1x research evidence?

The deployed long sleeve (v11a FC + div) is validated at 1x; live execution applies
notional_multiplier=10 / entry_leverage=10 (owner choice, promoted.py docstring). All
promoted evidence (MAR 1.58, DD -3.3%) is the 1x curve. This script produces the levered
read from the 1x backtest trade ledger + klines:

  1. levered equity curve (trade net returns x MULT, compounded on exit) + max DD,
     worst day -- vs the 1x curve;
  2. per-trade intratrade MAE from klines (the ledger's mae column is NaN) and
     isolated-liquidation analysis: distance to liq for a 10x long is
     -(1/L - MMR) ~= -9.5%; count breaches, stop-vs-liq gap-through bars;
  3. margin / concurrency timeline: per-position initial margin at live scale is
     notional_weight x equity (notional_weight x MULT / L); account gross leverage =
     MULT x sum(concurrent notional_weight); flags margin exhaustion;
  4. correlated-crash stress: account loss at observed gross leverage under uniform
     -5/-10/-15% book shocks (FC entries cluster in same-day FOMO names; crypto-wide
     flash crashes hit them together) + the worst OBSERVED joint day;
  5. funding drag at live scale.

Reads an existing long-sleeve backtest report dir (default: the equity_curves/long
artifacts). Analysis-only: touches no datasets, writes only a report.

    POLARS_MAX_THREADS=6 .venv/bin/python scripts/long_sleeve_10x_stress.py \
        [--report-dir ~/SHARED_DATA/bybit_full_pit/reports/equity_curves/long] \
        [--root ~/SHARED_DATA/bybit_full_pit] [--mult 10] [--leverage 10] [--mmr 0.005]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl


def _read_trade_window_lows(root: Path, trades: pl.DataFrame) -> pl.DataFrame:
    """Per trade: min(low)/max(high) over (entry_ts_ms, exit_ts_ms] from klines_1h.
    Hive-partition scan with symbol/date pushdown keeps this read small."""
    symbols = trades["symbol"].unique().to_list()
    d0 = trades["entry_date"].min()
    d1 = trades["exit_date"].max()
    lf = pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"), hive_partitioning=True)
    k = (
        lf.filter(
            pl.col("symbol").cast(pl.Utf8).is_in(symbols)
            & (pl.col("date").cast(pl.Utf8) >= str(d0))
            & (pl.col("date").cast(pl.Utf8) <= str(d1))
        )
        .select(pl.col("symbol").cast(pl.Utf8), "ts_ms", "open", "high", "low", "close")
        .collect(engine="streaming")
    )
    t = trades.select("trade_id", "symbol", "entry_ts_ms", "exit_ts_ms", "entry_price", "stop_price")
    j = t.join(k, on="symbol", how="inner").filter(
        (pl.col("ts_ms") >= pl.col("entry_ts_ms")) & (pl.col("ts_ms") < pl.col("exit_ts_ms"))
    )
    per_bar = j.with_columns(
        (pl.col("low") / pl.col("entry_price") - 1.0).alias("bar_mae"),
        (pl.col("high") / pl.col("entry_price") - 1.0).alias("bar_mfe"),
        (pl.col("open") / pl.col("entry_price") - 1.0).alias("bar_open_ret"),
    )
    return per_bar.group_by("trade_id").agg(
        pl.col("bar_mae").min().alias("mae"),
        pl.col("bar_mfe").max().alias("mfe"),
        pl.len().alias("bars"),
        # gap-through proxy: a single bar that OPENS above the stop but trades below the
        # liq threshold -- the stop cannot have filled before the liq level printed.
        # Threshold filled in later (needs liq_ret); store the per-trade worst single-bar
        # open->low drop as the gap-severity measure.
        (pl.col("bar_open_ret") - pl.col("bar_mae")).max().alias("worst_bar_open_to_low"),
    )


def _equity_from_trades(trades: pl.DataFrame, mult: float) -> pl.DataFrame:
    """Compound equity from per-trade equity-relative net returns, booked at exit
    (the engine's additive-on-exit convention), scaled by `mult`."""
    by_exit = (
        trades.group_by("exit_ts_ms")
        .agg((pl.col("net_return").sum() * mult).alias("ret"))
        .sort("exit_ts_ms")
    )
    eq, peak, worst_dd = 1.0, 1.0, 0.0
    rows = []
    for ts, r in by_exit.iter_rows():
        eq *= (1.0 + r)
        peak = max(peak, eq)
        worst_dd = min(worst_dd, eq / peak - 1.0)
        rows.append({"exit_ts_ms": ts, "ret": r, "equity": eq, "dd": eq / peak - 1.0})
    out = pl.DataFrame(rows) if rows else pl.DataFrame()
    return out.with_columns(pl.lit(worst_dd).alias("max_dd")) if not out.is_empty() else out


def _concurrency_timeline(trades: pl.DataFrame, mult: float, leverage: float) -> pl.DataFrame:
    """Sweep entry/exit events; track concurrent positions, summed notional_weight,
    live gross leverage (mult x sum w) and margin used (sum w x mult / leverage)."""
    ev = pl.concat([
        trades.select(pl.col("entry_ts_ms").alias("ts"), pl.col("notional_weight").alias("dw")),
        trades.select(pl.col("exit_ts_ms").alias("ts"), (-pl.col("notional_weight")).alias("dw")),
    ]).sort("ts")
    rows, w, n = [], 0.0, 0
    for ts, dw in ev.iter_rows():
        w += dw
        n += 1 if dw > 0 else -1
        rows.append({
            "ts_ms": ts, "concurrent": n, "sum_w": max(w, 0.0),
            "gross_leverage": mult * max(w, 0.0),
            "margin_used": max(w, 0.0) * mult / leverage,
        })
    return pl.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report-dir", default=str(Path.home() / "SHARED_DATA" / "bybit_full_pit" / "reports" / "equity_curves" / "long"))
    ap.add_argument("--root", default=str(Path.home() / "SHARED_DATA" / "bybit_full_pit"))
    ap.add_argument("--mult", type=float, default=10.0, help="live notional multiplier")
    ap.add_argument("--leverage", type=float, default=10.0, help="live entry leverage")
    ap.add_argument("--mmr", type=float, default=0.005, help="maintenance margin rate")
    ap.add_argument("--out", default=None, help="markdown report path (default <report-dir>/levered_stress_10x.md)")
    args = ap.parse_args()

    rep = Path(args.report_dir).expanduser()
    root = Path(args.root).expanduser()
    trades = pl.read_csv(rep / "long_native_trades.csv")
    n = trades.height
    liq_ret = -(1.0 / args.leverage - args.mmr)  # e.g. -9.5% for 10x, 0.5% mmr

    print(f"trades: {n}   window: {trades['entry_date'].min()} .. {trades['exit_date'].max()}")
    print(f"liq distance (isolated, {args.leverage:.0f}x, mmr {args.mmr:.2%}): {liq_ret:+.2%}")

    # 1) equity curves
    eq1 = _equity_from_trades(trades, 1.0)
    eqM = _equity_from_trades(trades, args.mult)
    ret1 = float(eq1["equity"][-1]) - 1.0
    retM = float(eqM["equity"][-1]) - 1.0
    dd1 = float(eq1["max_dd"][0])
    ddM = float(eqM["max_dd"][0])
    worst1 = float(eq1["ret"].min())
    worstM = float(eqM["ret"].min())

    # 2) intratrade MAE + liquidation analysis
    mae = _read_trade_window_lows(root, trades)
    t = trades.join(mae, on="trade_id", how="left")
    t = t.with_columns(
        (pl.col("stop_price") / pl.col("entry_price") - 1.0).alias("stop_ret"),
        (pl.col("mae") * args.mult * pl.col("notional_weight")).alias("mae_equity_live"),
    )
    liq_breach = t.filter(pl.col("mae") <= liq_ret)
    near = t.filter(pl.col("mae") <= liq_ret * 0.8)
    # gap-through severity: worst single-bar open->low drop vs the stop->liq cushion
    cushion = (t["stop_ret"] - liq_ret).abs()
    gap_risk = t.filter(pl.col("worst_bar_open_to_low") >= cushion)

    # 3) concurrency / margin
    tl = _concurrency_timeline(trades, args.mult, args.leverage)
    peak_n = int(tl["concurrent"].max())
    peak_gl = float(tl["gross_leverage"].max())
    peak_margin = float(tl["margin_used"].max())
    exhausted = tl.filter(pl.col("margin_used") > 1.0)

    # 4) correlated-crash stress at peak + median gross leverage
    med_gl = float(tl.filter(pl.col("concurrent") > 0)["gross_leverage"].median() or 0.0)
    shocks = (-0.05, -0.10, -0.15)

    # 5) funding at live scale
    funding_1x = float(trades["funding_return"].sum())
    net_1x = float(trades["net_return"].sum())

    lines = []

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    emit("\n=== LEVERED LONG-SLEEVE STRESS (live mult %.0fx / leverage %.0fx) ===" % (args.mult, args.leverage))
    emit(f"| metric | 1x (research evidence) | {args.mult:.0f}x (live config) |")
    emit("|---|---:|---:|")
    emit(f"| total return (exit-compounded) | {ret1:+.1%} | {retM:+.1%} |")
    emit(f"| max drawdown | {dd1:.1%} | {ddM:.1%} |")
    emit(f"| worst single exit-day | {worst1:.2%} | {worstM:.2%} |")
    emit(f"| funding P&L (sum, equity-rel) | {funding_1x:+.2%} | {funding_1x*args.mult:+.2%} |")
    emit(f"| net P&L (sum, equity-rel) | {net_1x:+.2%} | {net_1x*args.mult:+.2%} |")
    emit("")
    emit("=== ISOLATED-LIQUIDATION ANALYSIS (per-trade intratrade MAE from klines) ===")
    emit(f"- liq distance: {liq_ret:+.2%}  | trades with MAE <= liq: {liq_breach.height} / {n}")
    emit(f"- trades with MAE <= 80% of liq distance (near-miss): {near.height} / {n}")
    emit(f"- stop->liq cushion breached by a single bar's open->low range (gap-through risk): {gap_risk.height} / {n}")
    if liq_breach.height:
        emit("- liq-breach trades (would have LIQUIDATED live, loss = full margin):")
        for r in liq_breach.select("trade_id", "entry_date", "mae", "stop_ret", "net_return").to_dicts():
            emit(f"    {r['trade_id']}  entry {r['entry_date']}  MAE {r['mae']:+.1%} (stop {r['stop_ret']:+.1%})  backtest net {r['net_return']:+.3%}")
    emit("")
    emit("=== MARGIN / CONCURRENCY (live scale) ===")
    emit(f"- peak concurrent positions: {peak_n}")
    emit(f"- peak account gross leverage: {peak_gl:.1f}x   median (while in-market): {med_gl:.1f}x")
    emit(f"- peak margin used: {peak_margin:.0%} of equity   | events with margin > 100%: {exhausted.height}")
    emit("")
    emit("=== CORRELATED-CRASH STRESS (uniform book shock x gross leverage) ===")
    emit("| shock | account loss @ median GL | @ peak GL | account survives peak? |")
    emit("|---|---:|---:|---|")
    for s in shocks:
        lm = s * med_gl
        lp = s * peak_gl
        emit(f"| {s:.0%} | {lm:.0%} | {lp:.0%} | {'NO — wipeout' if lp <= -1.0 else 'yes'} |")
    emit(f"- account wipe threshold at peak GL: uniform {-1.0/peak_gl:.1%} book move" if peak_gl > 0 else "")
    emit("")
    emit("NOTE: equity curves compound on exit (engine convention) — intraday account DD is")
    emit("worse than shown; the MAE section bounds per-trade intratrade pain. Cross-margin")
    emit("means single-position liq is LESS likely than isolated, but the account-level")
    emit("correlated-crash row is the binding risk: FC entries cluster in same-day FOMO names.")

    out_path = Path(args.out) if args.out else rep / "levered_stress_10x.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
