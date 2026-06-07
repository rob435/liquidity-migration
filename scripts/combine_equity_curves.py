#!/usr/bin/env python3
"""Build the COMBINED short+long equity curve in the OFFICIAL strategy-vs-BTC format,
with optional modular benchmark overlays (a stock, an index, a second strategy).

Reuses the exact renderer the short and long sleeves use
(`volume_events_charts._write_equity_benchmark_chart`), so the combined PNG matches
their layout one-for-one (Strategy-vs-BTC line + monthly-returns table). The combined
book = short daily returns + the long sleeve as a weighted overlay, optionally levered:
``combined_ret = leverage * (short_ret + weight*long_ret)`` over the union of trading
days (a day a sleeve doesn't trade contributes 0). ``--leverage`` is gross
(daily-rebalanced); financing/borrow cost is NOT modelled. Run the per-sleeve curves
first (scripts/equity_curves.sh) so the equity CSVs exist.

Overlays are modular — add any number, from a CSV or auto-fetched from Yahoo:

    # plain combined book
    .venv/bin/python scripts/combine_equity_curves.py --root ~/SHARED_DATA/bybit_full_pit

    # 3x book + Micron (auto-fetched) + an index from a local CSV
    .venv/bin/python scripts/combine_equity_curves.py --root ~/SHARED_DATA/bybit_full_pit \
        --leverage 3 --fetch-yahoo "MU=Micron (MU)" --overlay "S&P 500=~/data/spx.csv"

Outputs under <root>/reports/equity_curves/combined/:
  combined_equity_btc.png    official-format Strategy-vs-BTC chart (+ overlays) + monthly table
  combined_book_equity.csv   date, short_ret, long_ret, combined_ret, equity
Auto-fetched overlays are cached under <curves-dir>/overlays/<TICKER>.csv.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.volume_events_charts import (  # noqa: E402
    _write_equity_benchmark_chart,
    price_overlay_from_csv,
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


def _split_label_target(spec: str, *, sep: str) -> tuple[str, str]:
    """Parse a "TARGET<sep>LABEL" arg; LABEL defaults to TARGET when omitted."""
    target, _, label = spec.partition(sep)
    target = target.strip()
    label = label.strip() or target
    return target, label


def _fetch_yahoo_daily(ticker: str, path: Path, start: str, end: str) -> None:
    """Daily closes for any ticker from Yahoo's public chart endpoint → CSV(date,close)."""
    import calendar
    import csv as _csv
    import datetime as _dt
    import json
    import urllib.request

    def _ep(d: str) -> int:
        y, m, day = (int(x) for x in d.split("-"))
        return calendar.timegm((y, m, day, 0, 0, 0))

    p1, p2 = _ep(start) - 7 * 86400, _ep(end) + 3 * 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1={p1}&period2={p2}&interval=1d"
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
    print(f"fetched {ticker} → {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="~/SHARED_DATA/bybit_full_pit")
    ap.add_argument("--curves-dir", default=None, help="defaults to <root>/reports/equity_curves")
    ap.add_argument("--weight", type=float, default=1.0, help="long overlay weight")
    ap.add_argument("--leverage", type=float, default=1.0, help="gross per-book daily leverage (financing not modelled)")
    ap.add_argument("--overlay", action="append", default=[], metavar="LABEL=CSV",
                    help="overlay a benchmark from a CSV (date,close); repeatable")
    ap.add_argument("--fetch-yahoo", action="append", default=[], metavar="TICKER[=LABEL]",
                    help="download a Yahoo ticker's daily closes and overlay it; repeatable")
    ap.add_argument("--title", default=None)
    ap.add_argument("--subtitle", default=None)
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    cdir = Path(args.curves_dir).expanduser() if args.curves_dir else root / "reports" / "equity_curves"
    short_csv = _find_csv(cdir / "short")
    long_csv = _find_csv(cdir / "long")
    if not short_csv or not long_csv:
        raise SystemExit(f"need both sleeve CSVs; short={short_csv} long={long_csv} under {cdir}")
    print(f"short csv: {short_csv}\nlong  csv: {long_csv}")

    w, lev = args.weight, args.leverage
    s = _daily_returns(short_csv).rename({"ret": "short_ret"})
    lo = _daily_returns(long_csv).rename({"ret": "long_ret"})
    m = (
        s.join(lo, on="date", how="full", coalesce=True)
        .fill_null(0.0)
        .with_columns(combined_ret=(lev * (pl.col("short_ret") + w * pl.col("long_ret"))))
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

    start, end = str(m["date"].min()), str(m["date"].max())
    btc = _btc_klines(root, start, end)

    # --- modular overlays: auto-fetched Yahoo tickers + local CSVs ---
    overlays = []
    for spec in args.fetch_yahoo:
        ticker, label = _split_label_target(spec, sep="=")
        ov_csv = cdir / "overlays" / f"{ticker}.csv"
        _fetch_yahoo_daily(ticker, ov_csv, start, end)
        overlays.append(price_overlay_from_csv(ov_csv, name=label, start=start, end=end))
    for spec in args.overlay:
        label, csv_path = _split_label_target(spec, sep="=")
        overlays.append(price_overlay_from_csv(csv_path, name=label, start=start, end=end))

    out = cdir / "combined"
    out.mkdir(parents=True, exist_ok=True)
    m.select("date", "short_ret", "long_ret", "combined_ret", "equity").write_csv(out / "combined_book_equity.csv")

    strat_name = "Strategy" if lev == 1.0 else f"Strategy {lev:g}x"
    title = args.title or (
        "Combined Book (Short + Long) vs BTC" if lev == 1.0
        else f"Combined Book (Short + Long, {lev:g}x) vs BTC"
    )
    meta = _write_equity_benchmark_chart(
        out,
        root=root,
        equity=equity_df,
        raw_klines=btc,
        monthly=monthly_df,
        png_name="combined_equity_btc.png",
        title=title,
        subtitle=args.subtitle,
        overlays=overlays,
        strategy_name=strat_name,
    )

    fin = float(m["equity"][-1])
    dd = float((m["equity"] / m["equity"].cum_max() - 1.0).min())
    print(f"\ncombined(w={w}, lev={lev:g}): {fin:.2f}x  max DD {dd:.1%}  ({start} -> {end})")
    if meta.get("overlays"):
        print(f"overlays: {', '.join(meta['overlays'])}")
    print(f"PNG: {meta.get('png') or '(render failed)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
