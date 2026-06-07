#!/usr/bin/env python3
"""Combined short+long book (leveraged) vs BTC vs Micron, in the OFFICIAL chart format.

Reuses the exact PNG renderer the sleeves use
(`volume_events_charts._write_equity_benchmark_png`) so the layout matches the
Strategy-vs-BTC chart one-for-one, then overlays a third line: Micron (MU)
buy-and-hold, normalised to $1 at the strategy start exactly like BTC.

The combined book = short daily returns + long daily returns over the union of
trading days (a day a sleeve doesn't trade contributes 0). ``--leverage L``
applies L to each sleeve's daily return (daily-rebalanced leverage:
combined_ret = L*short_ret + L*long_ret, then compounded). NOTE: leverage
financing / borrow cost is NOT modelled — this is the gross leveraged path.

Run the per-sleeve curves first (scripts/equity_curves.sh) so the equity CSVs
exist, and have a Micron CSV (date,close) — fetch with --fetch-mu.

    .venv/bin/python scripts/combine_equity_curves_micron.py \
        --root ~/SHARED_DATA/bybit_full_pit --leverage 3 \
        --mu-csv ~/SHARED_DATA/mu_daily.csv

Outputs under <root>/reports/equity_curves/combined/:
  combined_equity_btc_micron.png   official-format chart: Strategy / BTC / Micron
  combined_book_equity_micron.csv  date, short_ret, long_ret, combined_ret, equity
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.volume_events_charts import (  # noqa: E402
    _btc_daily_close_series,
    _monthly_table_rows,
    _normalised_price_series,
    _step_fill_daily,
    _strategy_equity_series,
    _write_equity_benchmark_png,
)

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
    if csv is None:
        return _EMPTY_TRADES
    df = pl.read_csv(csv)
    if "month" not in df.columns or "trades" not in df.columns:
        return _EMPTY_TRADES
    return df.select(month=pl.col("month").cast(pl.Utf8), tr=pl.col("trades").cast(pl.Int64))


def _btc_klines(root: Path, start: str, end: str) -> pl.DataFrame:
    return (
        pl.scan_parquet(str(root / "klines_1h"), hive_partitioning=True)
        .filter((pl.col("symbol") == "BTCUSDT") & (pl.col("date") >= start) & (pl.col("date") <= end))
        .select("symbol", "date", "ts_ms", "close")
        .collect()
    )


def _fetch_mu(path: Path, start: str, end: str) -> None:
    """Daily MU closes from Yahoo's public chart endpoint → CSV(date,close)."""
    import calendar
    import csv as _csv
    import datetime as _dt
    import json
    import urllib.request

    def _ep(d: str) -> int:
        y, m, day = (int(x) for x in d.split("-"))
        return calendar.timegm((y, m, day, 0, 0, 0))

    p1, p2 = _ep(start) - 7 * 86400, _ep(end) + 3 * 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/MU?period1={p1}&period2={p2}&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    res = data["chart"]["result"][0]
    ts, close = res["timestamp"], res["indicators"]["quote"][0]["close"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["date", "close"])
        for t, c in zip(ts, close):
            if c is None:
                continue
            w.writerow([_dt.datetime.fromtimestamp(t, _dt.UTC).date().isoformat(), f"{c:.4f}"])
    print(f"fetched MU → {path}")


def _mu_series(csv: Path, start: str, end: str) -> list[dict]:
    """Normalised ($1 at strategy start) MU buy-and-hold series within [start,end]."""
    df = (
        pl.read_csv(csv)
        .with_columns(date=pl.col("date").cast(pl.Utf8).str.slice(0, 10), value=pl.col("close").cast(pl.Float64))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end) & pl.col("value").is_not_null())
        .sort("date")
    )
    return _normalised_price_series([{"date": r["date"], "value": r["value"]} for r in df.to_dicts()])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="~/SHARED_DATA/bybit_full_pit")
    ap.add_argument("--curves-dir", default=None, help="defaults to <root>/reports/equity_curves")
    ap.add_argument("--leverage", type=float, default=3.0, help="per-sleeve daily-return leverage (both sleeves)")
    ap.add_argument("--mu-csv", default="~/SHARED_DATA/mu_daily.csv")
    ap.add_argument("--fetch-mu", action="store_true", help="(re)download MU closes from Yahoo before drawing")
    ap.add_argument("--title", default=None)
    ap.add_argument("--subtitle", default=None)
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    cdir = Path(args.curves_dir).expanduser() if args.curves_dir else root / "reports" / "equity_curves"
    mu_csv = Path(args.mu_csv).expanduser()
    short_csv = _find_csv(cdir / "short")
    long_csv = _find_csv(cdir / "long")
    if not short_csv or not long_csv:
        raise SystemExit(f"need both sleeve CSVs; short={short_csv} long={long_csv} under {cdir}")
    print(f"short csv: {short_csv}\nlong  csv: {long_csv}")

    lev = args.leverage
    s = _daily_returns(short_csv).rename({"ret": "short_ret"})
    lo = _daily_returns(long_csv).rename({"ret": "long_ret"})
    m = (
        s.join(lo, on="date", how="full", coalesce=True)
        .fill_null(0.0)
        .with_columns(combined_ret=(lev * pl.col("short_ret") + lev * pl.col("long_ret")))
        .with_columns(_d=pl.col("date").str.strptime(pl.Date, strict=False))
        .sort("_d")
    )
    m = m.with_columns(
        equity=(1.0 + pl.col("combined_ret")).cum_prod(),
        ts_ms=pl.col("_d").cast(pl.Datetime("ms")).dt.epoch("ms"),
    )
    equity_df = m.select("ts_ms", "equity", pl.col("combined_ret").alias("basket_return"), "date")

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

    start, end = str(m["date"].min()), str(m["date"].max())
    if args.fetch_mu or not mu_csv.exists():
        _fetch_mu(mu_csv, start, end)

    strategy = _step_fill_daily(_strategy_equity_series(equity_df))
    btc = _normalised_price_series(_btc_daily_close_series(_btc_klines(root, start, end), start=start, end=end))
    mu = _mu_series(mu_csv, start, end)

    strat_name = "Strategy" if lev == 1.0 else f"Strategy {lev:g}x"
    series = [
        {"name": strat_name, "color": (7, 14, 31), "alpha": 255, "width": 4, "points": strategy},
        {"name": "BTC", "color": (234, 88, 12), "alpha": 215, "width": 3, "points": btc},
        {"name": "Micron (MU)", "color": (13, 148, 136), "alpha": 230, "width": 3, "points": mu},
    ]
    monthly_rows = _monthly_table_rows(equity=equity_df, monthly=monthly_df)

    out = cdir / "combined"
    out.mkdir(parents=True, exist_ok=True)
    m.select("date", "short_ret", "long_ret", "combined_ret", "equity").write_csv(out / "combined_book_equity_micron.csv")
    png = out / "combined_equity_btc_micron.png"

    title = args.title or f"Combined Book (Short + Long, {lev:g}x) vs BTC vs Micron"
    subtitle = args.subtitle or (
        "Strategy, BTC and Micron normalised to $1 at the strategy start; "
        f"both sleeves levered {lev:g}x (gross, financing not modelled)."
    )
    _write_equity_benchmark_png(
        png, series=series, start=start, end=end, monthly_rows=monthly_rows, title=title, subtitle=subtitle
    )

    def _mult(pts: list[dict]) -> float:
        return float(pts[-1]["value"]) if pts else float("nan")

    fin = float(m["equity"][-1])
    dd = float((m["equity"] / m["equity"].cum_max() - 1.0).min())
    print(f"\ncombined({lev:g}x): {fin:.2f}x  max DD {dd:.1%}  ({start} -> {end})")
    print(f"BTC {_mult(btc):.2f}x   Micron {_mult(mu):.2f}x")
    print(f"PNG: {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
