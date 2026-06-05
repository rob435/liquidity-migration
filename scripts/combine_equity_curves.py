#!/usr/bin/env python3
"""Build the COMBINED short+long equity curve from the per-sleeve equity CSVs
that scripts/equity_curves.py emits.

The combined book = the short sleeve's daily returns plus the long sleeve as an
additive overlay (combined_ret = short_ret + w*long_ret over the union of trading
days; a day a sleeve doesn't trade contributes 0). This mirrors how the deployed
demo runs the two together. One-off helper for the write-up; not wired into the CLI.

    .venv/bin/python scripts/combine_equity_curves.py \
        --root ~/SHARED_DATA/bybit_full_pit --weight 1.0

Outputs (under <root>/reports/equity_curves/combined/):
  combined_book_equity.png   the combined cumulative equity (bold) with short/long
                             shown lighter for context, plus a drawdown panel.
  combined_book_equity.csv   date, short_ret, long_ret, combined_ret, *_eq, drawdown.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_DATE_COLS = ("date", "day", "timestamp", "ts_ms")
_RET_COLS = ("basket_return", "period_return", "net_return", "return", "ret")
_EQ_COLS = ("equity", "equity_usdt", "nav", "cum_return", "cumulative_return")


def _find_csv(out: Path) -> Path | None:
    hits = sorted(out.rglob("*equity*.csv"))
    if not hits:
        hits = sorted(out.rglob("*baskets*.csv")) or sorted(out.rglob("*.csv"))
    return hits[-1] if hits else None


def _daily_returns(csv: Path) -> pl.DataFrame:
    """Return a [date, ret] frame of per-day returns, robust to schema."""
    df = pl.read_csv(csv)
    cols = {c.lower(): c for c in df.columns}
    dcol = next((cols[c] for c in _DATE_COLS if c in cols), None)
    if dcol is None:
        raise SystemExit(f"{csv.name}: no date column in {df.columns}")
    if dcol.lower() == "ts_ms":
        date_expr = pl.from_epoch(pl.col(dcol), "ms").dt.date()
    else:
        date_expr = (
            pl.col(dcol).cast(pl.Utf8).str.slice(0, 10).str.strptime(pl.Date, strict=False)
        )

    rcol = next((cols[c] for c in _RET_COLS if c in cols), None)
    ecol = next((cols[c] for c in _EQ_COLS if c in cols), None)

    if rcol is not None:
        out = df.select(date=date_expr, ret=pl.col(rcol).cast(pl.Float64))
        # multiple baskets in a day compound; sum is a fine first-order approx for small returns
        return out.group_by("date").agg(pl.col("ret").sum()).sort("date")
    if ecol is not None:
        out = df.select(date=date_expr, eq=pl.col(ecol).cast(pl.Float64)).sort("date")
        # one equity point per day (last), then pct-change
        out = out.group_by("date").agg(pl.col("eq").last()).sort("date")
        out = out.with_columns(ret=(pl.col("eq") / pl.col("eq").shift(1) - 1.0).fill_null(0.0))
        return out.select("date", "ret")
    raise SystemExit(f"{csv.name}: no return or equity column in {df.columns}")


def _cum(series: pl.Series) -> pl.Series:
    return (1.0 + series).cum_prod()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="~/SHARED_DATA/bybit_full_pit")
    ap.add_argument("--curves-dir", default=None, help="defaults to <root>/reports/equity_curves")
    ap.add_argument("--weight", type=float, default=1.0, help="long overlay weight (deployed ~0.5-1.0)")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    cdir = Path(args.curves_dir).expanduser() if args.curves_dir else root / "reports" / "equity_curves"
    short_csv = _find_csv(cdir / "short")
    long_csv = _find_csv(cdir / "long")
    if not short_csv or not long_csv:
        raise SystemExit(f"need both sleeve CSVs; short={short_csv} long={long_csv} under {cdir}")
    print(f"short csv: {short_csv}")
    print(f"long  csv: {long_csv}")

    s = _daily_returns(short_csv).rename({"ret": "short_ret"})
    lo = _daily_returns(long_csv).rename({"ret": "long_ret"})
    w = args.weight
    m = (
        s.join(lo, on="date", how="full", coalesce=True)
        .fill_null(0.0)
        .sort("date")
        .with_columns(combined_ret=(pl.col("short_ret") + w * pl.col("long_ret")))
    )
    m = m.with_columns(
        short_eq=_cum(m["short_ret"]),
        long_eq=_cum(m["long_ret"]),
        combined_eq=_cum(m["combined_ret"]),
    )
    m = m.with_columns(
        drawdown=(pl.col("combined_eq") / pl.col("combined_eq").cum_max() - 1.0)
    )

    out = cdir / "combined"
    out.mkdir(parents=True, exist_ok=True)
    m.write_csv(out / "combined_book_equity.csv")

    def _mult(col: str) -> float:
        return float(m[col][-1])

    def _maxdd(eq: pl.Series) -> float:
        return float((eq / eq.cum_max() - 1.0).min())

    print(
        f"\nfinal multiples (start=1.0):  short {_mult('short_eq'):.2f}x | "
        f"long {_mult('long_eq'):.2f}x | combined(w={w}) {_mult('combined_eq'):.2f}x"
    )
    print(
        f"max drawdown:  short {_maxdd(m['short_eq']):.1%} | "
        f"long {_maxdd(m['long_eq']):.1%} | combined {_maxdd(m['combined_eq']):.1%}"
    )

    # plot
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dates = m["date"].to_list()
    fig, (ax, axd) = plt.subplots(
        2, 1, figsize=(12, 7), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )
    ax.plot(dates, m["short_eq"].to_list(), lw=1.0, alpha=0.55, label=f"short only ({_mult('short_eq'):.2f}x)")
    ax.plot(dates, m["long_eq"].to_list(), lw=1.0, alpha=0.55, label=f"long only ({_mult('long_eq'):.2f}x)")
    ax.plot(dates, m["combined_eq"].to_list(), lw=2.2, color="black", label=f"combined book ({_mult('combined_eq'):.2f}x)")
    ax.set_yscale("log")
    ax.set_ylabel("equity (x, log)")
    ax.set_title(f"combined book: short fade + long overlay (w={w}) — in-sample, bybit full-PIT")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3, which="both")
    axd.fill_between(dates, m["drawdown"].to_list(), 0.0, color="crimson", alpha=0.4)
    axd.set_ylabel("combined DD")
    axd.grid(alpha=0.3)
    fig.tight_layout()
    png = out / "combined_book_equity.png"
    fig.savefig(png, dpi=120)
    plt.close(fig)
    print(f"\nPNG: {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
