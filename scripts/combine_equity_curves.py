#!/usr/bin/env python3
"""Build the COMBINED short+long equity curve in the OFFICIAL strategy-vs-BTC format.

Reuses the exact renderer the short and long sleeves use
(`volume_events_charts._write_equity_benchmark_chart`), so the combined PNG matches
their layout one-for-one (Strategy-vs-BTC line + monthly-returns table) instead of a
side-step chart. The combined book = the short sleeve's daily returns plus the long
sleeve as an additive overlay (combined_ret = short_ret + w*long_ret over the union of
trading days; a day a sleeve doesn't trade contributes 0). Run the per-sleeve curves
first (scripts/equity_curves.sh) so the equity CSVs exist.

    .venv/bin/python scripts/combine_equity_curves.py --root ~/SHARED_DATA/bybit_full_pit --weight 1.0

Outputs under <root>/reports/equity_curves/combined/:
  combined_equity_btc.png    official-format Strategy-vs-BTC chart + monthly table
  combined_book_equity.csv   date, short_ret, long_ret, combined_ret, equity
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.volume_events_charts import _write_equity_benchmark_chart  # noqa: E402

_DATE_COLS = ("date", "day", "timestamp", "ts_ms")
_RET_COLS = ("basket_return", "period_return", "net_return", "return", "ret")
_EQ_COLS = ("equity", "equity_usdt", "nav", "cum_return", "cumulative_return")
_EMPTY_TRADES = pl.DataFrame(schema={"month": pl.Utf8, "tr": pl.Int64})


def _find_csv(out: Path) -> Path | None:
    hits = sorted(out.rglob("*equity*.csv")) or sorted(out.rglob("*baskets*.csv")) or sorted(out.rglob("*.csv"))
    return hits[-1] if hits else None


def _find_monthly(out: Path) -> Path | None:
    hits = sorted(out.rglob("*monthly*.csv"))
    return hits[-1] if hits else None


def _daily_returns(csv: Path) -> pl.DataFrame:
    """[date(str), ret] of per-day returns, robust to the sleeve's CSV schema."""
    df = pl.read_csv(csv)
    cols = {c.lower(): c for c in df.columns}
    dcol = next((cols[c] for c in _DATE_COLS if c in cols), None)
    if dcol is None:
        raise SystemExit(f"{csv.name}: no date column in {df.columns}")
    if dcol.lower() == "ts_ms":
        date_expr = pl.from_epoch(pl.col(dcol), "ms").dt.date().cast(pl.Utf8)
    else:
        date_expr = pl.col(dcol).cast(pl.Utf8).str.slice(0, 10)
    rcol = next((cols[c] for c in _RET_COLS if c in cols), None)
    ecol = next((cols[c] for c in _EQ_COLS if c in cols), None)
    if rcol is not None:
        out = df.select(date=date_expr, ret=pl.col(rcol).cast(pl.Float64))
        return out.group_by("date").agg(pl.col("ret").sum()).sort("date")
    if ecol is not None:
        out = df.select(date=date_expr, eq=pl.col(ecol).cast(pl.Float64))
        out = out.group_by("date").agg(pl.col("eq").last()).sort("date")
        return out.with_columns(ret=(pl.col("eq") / pl.col("eq").shift(1) - 1.0).fill_null(0.0)).select("date", "ret")
    raise SystemExit(f"{csv.name}: no return or equity column in {df.columns}")


def _monthly_trades(csv: Path | None) -> pl.DataFrame:
    """[month, tr] real trade counts from a sleeve's monthly CSV (empty if absent)."""
    if csv is None:
        return _EMPTY_TRADES
    df = pl.read_csv(csv)
    if "month" not in df.columns or "trades" not in df.columns:
        return _EMPTY_TRADES
    return df.select(month=pl.col("month").cast(pl.Utf8), tr=pl.col("trades").cast(pl.Int64))


def _btc_klines(root: Path, start: str, end: str) -> pl.DataFrame:
    """BTCUSDT klines (symbol,date,ts_ms,close) for the window — the renderer's benchmark."""
    return (
        pl.scan_parquet(str(root / "klines_1h"), hive_partitioning=True)
        .filter((pl.col("symbol") == "BTCUSDT") & (pl.col("date") >= start) & (pl.col("date") <= end))
        .select("symbol", "date", "ts_ms", "close")
        .collect()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="~/SHARED_DATA/bybit_full_pit")
    ap.add_argument("--curves-dir", default=None, help="defaults to <root>/reports/equity_curves")
    ap.add_argument("--weight", type=float, default=1.0, help="long overlay weight")
    ap.add_argument("--title", default="Combined Book (Short + Long) vs BTC")
    ap.add_argument("--subtitle", default=None)
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    cdir = Path(args.curves_dir).expanduser() if args.curves_dir else root / "reports" / "equity_curves"
    short_csv = _find_csv(cdir / "short")
    long_csv = _find_csv(cdir / "long")
    if not short_csv or not long_csv:
        raise SystemExit(f"need both sleeve CSVs; short={short_csv} long={long_csv} under {cdir}")
    print(f"short csv: {short_csv}\nlong  csv: {long_csv}")

    s = _daily_returns(short_csv).rename({"ret": "short_ret"})
    lo = _daily_returns(long_csv).rename({"ret": "long_ret"})
    w = args.weight
    m = (
        s.join(lo, on="date", how="full", coalesce=True)
        .fill_null(0.0)
        .with_columns(combined_ret=(pl.col("short_ret") + w * pl.col("long_ret")))
        .with_columns(_d=pl.col("date").str.strptime(pl.Date, strict=False))
        .sort("_d")
    )
    m = m.with_columns(
        equity=(1.0 + pl.col("combined_ret")).cum_prod(),
        ts_ms=pl.col("_d").cast(pl.Datetime("ms")).dt.epoch("ms"),
    )

    # equity frame in the engine's schema (date, equity, basket_return, ts_ms)
    equity_df = m.select("ts_ms", "equity", pl.col("combined_ret").alias("basket_return"), "date")

    # combined monthly: strategy_return from the combined daily returns; trades = short + long
    monthly_ret = (
        m.with_columns(month=pl.col("date").str.slice(0, 7))
        .group_by("month")
        .agg(((pl.col("combined_ret") + 1.0).product() - 1.0).alias("strategy_return"))
    )
    trades = (
        pl.concat([_monthly_trades(_find_monthly(cdir / "short")), _monthly_trades(_find_monthly(cdir / "long"))])
        .group_by("month")
        .agg(pl.col("tr").sum().alias("trades"))
    )
    monthly_df = monthly_ret.join(trades, on="month", how="left").with_columns(pl.col("trades").fill_null(0)).sort("month")

    start, end = m["date"].min(), m["date"].max()
    btc = _btc_klines(root, str(start), str(end))

    out = cdir / "combined"
    out.mkdir(parents=True, exist_ok=True)
    m.select("date", "short_ret", "long_ret", "combined_ret", "equity").write_csv(out / "combined_book_equity.csv")

    meta = _write_equity_benchmark_chart(
        out,
        root=root,
        equity=equity_df,
        raw_klines=btc,
        monthly=monthly_df,
        png_name="combined_equity_btc.png",
        title=args.title,
        subtitle=args.subtitle,
    )

    fin = float(m["equity"][-1])
    dd = float((m["equity"] / m["equity"].cum_max() - 1.0).min())
    print(f"\ncombined(w={w}): {fin:.2f}x  max DD {dd:.1%}  ({start} -> {end})")
    print(f"PNG: {meta.get('png') or '(render failed)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
