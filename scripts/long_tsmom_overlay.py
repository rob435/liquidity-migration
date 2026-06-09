#!/usr/bin/env python3
"""Daily-MTM long-sleeve equity + majors TSMOM overlay (EXPLORATORY, 2026-06-09).

Two improvements to the LONG system in one daily-grid engine:

1. **Daily mark-to-market equity for the FC book.** The engine books P&L on exit only,
   so the published curve is a step function and hides intraday risk. Here every open
   trade marks daily (entry day: entry->close; interior: close->close; exit day:
   prev-close->exit), weighted by its ledger notional_weight. Same total P&L per trade
   as the ledger (costs+funding booked on exit day), honest daily path.

2. **Majors time-series-momentum overlay.** TSMOM on BTC/ETH is the most documented
   crypto anomaly (Moskowitz-Ooi-Pedersen family) and is ABSENT from the prior
   long-sleeve alpha search (which was alt cross-sectional only). Rule variants are
   fixed a priori — the canonical documented ones, not a mined grid:
       sma50   : long when close(lag1) > SMA50(lag1)
       sma20   : long when close(lag1) > SMA20(lag1)
       tsm30   : long when 30d return(lag1) > 0   (matches the repo's regime indicator)
       tsm90   : long when 90d return(lag1) > 0
   Per-major weight is vol-targeted (target_vol / realized_30d, capped 1.0 — never
   levered), costed at cost_bps per side on |Δweight| turnover, and charged REAL
   funding from the root's funding dataset (longs pay positive funding).
   Robustness is read ACROSS variants (all should be positive if majors trend is
   real); the combination test uses the a-priori primary variant sma50.

Outputs per venue: stats table (return, ann/maxDD MAR, sharpe, %days-with-PnL, worst
day) for FC-only / TSMOM-only / combined at tsmom_weight w, plus a PNG of the three
curves. Analysis-only: reads ledgers + klines, writes a report dir.

    POLARS_MAX_THREADS=4 .venv/bin/python scripts/long_tsmom_overlay.py \
        --fc-trades ~/SHARED_DATA/bybit_full_pit/reports/long_improve_2026-06-09/00_baseline/long_native_trades.csv \
        --root ~/SHARED_DATA/bybit_full_pit [--tsmom-weight 0.5] [--target-vol 0.30]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import polars as pl

MS_PER_DAY = 86_400_000
MAJORS = ("BTCUSDT", "ETHUSDT")
VARIANTS = ("sma50", "sma20", "tsm30", "tsm90", "donchian")
PRIMARY = "sma50"  # a-priori primary simple rule; fixed BEFORE results
# "donchian" implements the published Zarattini-Pagani-Barbon Combo rule (SSRN 5209907):
# entry on n-day CLOSE-channel high, exit on close <= trailing stop (initialized at the
# Donchian mid at entry, ratcheted up to max(stop, mid) daily), equal-weight ensemble over
# n in {5,10,20,30,60,90,150,250,360}, vol-sized w = min(target/sigma90, cap). Published
# top-20 rotational: Sharpe 1.57 / MDD 11% / MAR 1.61 net 10bps (spot, no funding) —
# here: majors only, REAL funding charged, cap 1.0 (never levered).
DONCHIAN_LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)


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
        k.sort("ts_ms").group_by("date", maintain_order=True)
        .agg(pl.col("close").last().alias("close")).sort("date")
    )


def _funding_daily(root: Path, symbol: str) -> pl.DataFrame:
    """Per-day summed funding rate for symbol (one row per settlement in data)."""
    fdir = root / "funding"
    if not fdir.is_dir():
        fdir = root / "binance_usdm_funding"
    if not fdir.is_dir():
        return pl.DataFrame({"date": [], "funding_day": []})
    lf = pl.scan_parquet(str(fdir / "**" / "*.parquet"))
    f = lf.select("symbol", "ts_ms", "funding_rate").filter(pl.col("symbol") == symbol).collect()
    if f.is_empty():
        return pl.DataFrame({"date": [], "funding_day": []})
    return (
        f.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"))
        .group_by("date").agg(pl.col("funding_rate").sum().alias("funding_day")).sort("date")
    )


def _donchian_on_fraction(px: pl.DataFrame) -> pl.DataFrame:
    """Published Donchian Combo state: per day, the FRACTION of the 9 lookback
    sub-strategies that are long (each: enter on n-day close-high; exit on close <=
    trailing stop = ratcheted Donchian mid). Causal: state at D uses closes <= D-1."""
    closes = px["close"].to_list()
    n_rows = len(closes)
    frac = [0.0] * n_rows
    for lb in DONCHIAN_LOOKBACKS:
        in_pos = False
        stop = 0.0
        state = [0] * n_rows  # state[i] = position held INTO day i (decided at i-1 close)
        for i in range(1, n_rows):
            state[i] = 1 if in_pos else 0
            # update decision AT day i's close (affects day i+1)
            lo = max(0, i - lb + 1)
            window = closes[lo:i + 1]
            up = max(window)
            mid = 0.5 * (max(window) + min(window))
            c = closes[i]
            if not in_pos:
                if c >= up:  # new n-day closing high
                    in_pos = True
                    stop = mid
            else:
                stop = max(stop, mid)
                if c <= stop:
                    in_pos = False
        for i in range(n_rows):
            frac[i] += state[i] / len(DONCHIAN_LOOKBACKS)
    return px.with_columns(pl.Series("on_frac", frac))


def _tsmom_stream(root: Path, *, start: str, end: str, variant: str,
                  target_vol_annual: float, cost_bps_side: float) -> pl.DataFrame:
    """Daily return stream of the vol-targeted majors TSMOM rule (equal split across majors)."""
    per_major = []
    for sym in MAJORS:
        px = _daily_closes(root, sym, "2022-10-01" if variant != "donchian" else "2021-06-01", end)
        fd = _funding_daily(root, sym)
        d = px.with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret1"),
        )
        if variant == "donchian":
            d = _donchian_on_fraction(d).with_columns(pl.col("on_frac").alias("on"))
        else:
            sig = {
                "sma50": (pl.col("close").shift(1) > pl.col("close").rolling_mean(50).shift(1)),
                "sma20": (pl.col("close").shift(1) > pl.col("close").rolling_mean(20).shift(1)),
                "tsm30": (pl.col("close").shift(1) / pl.col("close").shift(31) - 1.0 > 0),
                "tsm90": (pl.col("close").shift(1) / pl.col("close").shift(91) - 1.0 > 0),
            }[variant]
            d = d.with_columns(sig.cast(pl.Int8).alias("on"))
        # causal vol target: trailing 30d realized vol, lag-1, weight = min(1, tv/rv)
        vol_win = 90 if variant == "donchian" else 30  # paper uses sigma_90 for donchian
        d = d.with_columns(
            (pl.col("ret1").rolling_std(vol_win).shift(1) * math.sqrt(365.0)).alias("rv_ann")
        ).with_columns(
            (pl.col("on") * (pl.lit(target_vol_annual) / pl.col("rv_ann")).clip(0.0, 1.0)).alias("w")
        ).with_columns(
            pl.col("w").fill_null(0.0)
        ).with_columns(
            (pl.col("w") - pl.col("w").shift(1).fill_null(0.0)).abs().alias("turnover")
        )
        d = d.join(fd, on="date", how="left").with_columns(pl.col("funding_day").fill_null(0.0))
        # long pays positive funding: -w * funding_day; costs on turnover
        d = d.with_columns(
            (pl.col("w") * pl.col("ret1")
             - pl.col("w") * pl.col("funding_day")
             - pl.col("turnover") * (cost_bps_side / 10_000.0)).alias("pnl")
        )
        per_major.append(d.select("date", pl.col("pnl").alias(f"pnl_{sym}")))
    out = per_major[0]
    for p in per_major[1:]:
        out = out.join(p, on="date", how="full", coalesce=True)
    cols = [c for c in out.columns if c.startswith("pnl_")]
    out = out.with_columns(
        (sum(pl.col(c).fill_null(0.0) for c in cols) / len(cols)).alias("tsmom_ret")
    ).select("date", "tsmom_ret").sort("date")
    return out.filter(pl.col("date") >= start)


def _fc_daily_mtm(root: Path, trades: pl.DataFrame, *, start: str, end: str) -> pl.DataFrame:
    """Daily MTM return stream for the FC book from the ledger + daily closes.

    Interior days mark close->close on notional_weight; entry day marks entry->close;
    exit day marks prev-close->exit_price PLUS the trade's booked costs+funding
    (cost_return + funding_return land on exit day, matching ledger totals)."""
    syms = trades["symbol"].unique().to_list()
    lf = pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"), hive_partitioning=True)
    px = (
        lf.filter(pl.col("symbol").cast(pl.Utf8).is_in(syms)
                  & (pl.col("date").cast(pl.Utf8) >= "2023-01-01")
                  & (pl.col("date").cast(pl.Utf8) < end))
        .select(pl.col("symbol").cast(pl.Utf8), pl.col("date").cast(pl.Utf8), "ts_ms", "close")
        .collect(engine="streaming")
        .sort("ts_ms").group_by(["symbol", "date"], maintain_order=True)
        .agg(pl.col("close").last().alias("close")).sort(["symbol", "date"])
    )
    px_map: dict[str, dict[str, float]] = {}
    for sym, part in px.partition_by("symbol", as_dict=True).items():
        key = sym[0] if isinstance(sym, tuple) else sym
        px_map[str(key)] = dict(zip(part["date"].to_list(), part["close"].to_list()))

    daily: dict[str, float] = {}
    for t in trades.iter_rows(named=True):
        sym = t["symbol"]
        w = float(t["notional_weight"])
        e_date = str(t["entry_date"])
        x_date = str(t["exit_date"])
        e_px = float(t["entry_price"])
        x_px = float(t["exit_price"])
        cost_fund = float(t.get("cost_return") or 0.0) + float(t.get("funding_return") or 0.0)
        closes = px_map.get(sym, {})
        days = sorted(d for d in closes if e_date <= d <= x_date)
        if not days:
            daily[x_date] = daily.get(x_date, 0.0) + float(t["net_return"])
            continue
        prev_px = e_px
        for i, d in enumerate(days):
            if d == x_date or i == len(days) - 1:
                break
            c = closes[d]
            daily[d] = daily.get(d, 0.0) + w * (c / prev_px - 1.0)
            prev_px = c
        daily[x_date] = daily.get(x_date, 0.0) + w * (x_px / prev_px - 1.0) + cost_fund
    out = pl.DataFrame({"date": list(daily), "fc_ret": list(daily.values())}).sort("date")
    return out.filter((pl.col("date") >= start) & (pl.col("date") < end))


def _stats(dates: list[str], rets: list[float]) -> dict:
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    n = len(rets)
    years = max(n / 365.0, 1e-9)
    total = eq - 1.0
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    mar = ann / abs(mdd) if abs(mdd) > 1e-9 else float("nan")
    mu = sum(rets) / n if n else 0.0
    sd = (sum((r - mu) ** 2 for r in rets) / n) ** 0.5 if n else 0.0
    sharpe = (mu / sd * math.sqrt(365.0)) if sd > 0 else float("nan")
    active = sum(1 for r in rets if abs(r) > 1e-12) / n if n else 0.0
    return {"total_return": total, "ann_return": ann, "max_dd": mdd, "mar": mar,
            "sharpe_daily_ann": sharpe, "worst_day": min(rets) if rets else 0.0,
            "pct_days_active": active, "days": n}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fc-trades", required=True)
    ap.add_argument("--root", default=str(Path.home() / "SHARED_DATA" / "bybit_full_pit"))
    ap.add_argument("--start", default="2023-04-01")
    ap.add_argument("--end", default="2026-05-28")
    ap.add_argument("--tsmom-weight", type=float, default=0.5)
    ap.add_argument("--target-vol", type=float, default=0.30)
    ap.add_argument("--cost-bps-side", type=float, default=10.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    trades = pl.read_csv(Path(args.fc_trades).expanduser())
    out_dir = Path(args.out) if args.out else Path(args.fc_trades).expanduser().parent / "tsmom_overlay"
    out_dir.mkdir(parents=True, exist_ok=True)

    fc = _fc_daily_mtm(root, trades, start=args.start, end=args.end)

    # all-days grid (union of dates seen in majors closes — trading calendar)
    grid = _daily_closes(root, "BTCUSDT", args.start, args.end).select("date")
    fc_g = grid.join(fc, on="date", how="left").with_columns(pl.col("fc_ret").fill_null(0.0))

    results = {}
    streams = {}
    for v in VARIANTS:
        ts = _tsmom_stream(root, start=args.start, end=args.end, variant=v,
                           target_vol_annual=args.target_vol, cost_bps_side=args.cost_bps_side)
        g = fc_g.join(ts, on="date", how="left").with_columns(pl.col("tsmom_ret").fill_null(0.0))
        results[f"tsmom_{v}_only"] = _stats(g["date"].to_list(), g["tsmom_ret"].to_list())
        streams[v] = g

    results["fc_only_daily_mtm"] = _stats(streams[PRIMARY]["date"].to_list(), streams[PRIMARY]["fc_ret"].to_list())

    # save the daily streams (wide) so weight-grids/correlations need no recompute
    wide = streams[VARIANTS[0]].select("date", "fc_ret").sort("date")
    for v in VARIANTS:
        wide = wide.join(streams[v].select("date", pl.col("tsmom_ret").alias(f"tsmom_{v}")), on="date", how="left")
    wide.write_csv(out_dir / "daily_streams.csv")

    # weight grid for the two a-priori combination candidates (sma50, donchian)
    for v in ("sma50", "donchian"):
        g = streams[v]
        fcs, tss = g["fc_ret"].to_list(), g["tsmom_ret"].to_list()
        mu_f = sum(fcs) / len(fcs)
        mu_t = sum(tss) / len(tss)
        cov = sum((a - mu_f) * (b - mu_t) for a, b in zip(fcs, tss)) / len(fcs)
        sd_f = (sum((a - mu_f) ** 2 for a in fcs) / len(fcs)) ** 0.5
        sd_t = (sum((b - mu_t) ** 2 for b in tss) / len(tss)) ** 0.5
        corr = cov / (sd_f * sd_t) if sd_f > 0 and sd_t > 0 else float("nan")
        results[f"corr_fc_{v}"] = {"corr": corr}
        for w in (0.1, 0.15, 0.2, 0.25, 0.3, 0.5):
            combined = [(1 - w) * f + w * t for f, t in zip(fcs, tss)]
            results[f"combined_{v}_w{int(w*100):02d}"] = _stats(g["date"].to_list(), combined)
    g = streams[PRIMARY]
    w = args.tsmom_weight
    combined = [(1 - w) * f + w * t for f, t in zip(g["fc_ret"].to_list(), g["tsmom_ret"].to_list())]

    print(f"{'stream':<28}{'total':>9}{'ann':>8}{'maxDD':>8}{'MAR':>7}{'Shp':>6}{'worst':>8}{'%active':>9}")
    for name, s in results.items():
        if "corr" in s:
            print(f"{name:<28}  corr={s['corr']:+.3f}")
            continue
        print(f"{name:<28}{s['total_return']:>+9.1%}{s['ann_return']:>+8.1%}{s['max_dd']:>8.1%}"
              f"{s['mar']:>7.2f}{s['sharpe_daily_ann']:>6.2f}{s['worst_day']:>8.2%}{s['pct_days_active']:>9.0%}")

    (out_dir / "tsmom_overlay_stats.json").write_text(json.dumps(results, indent=2))

    # PNG: FC-only vs TSMOM(primary) vs combined
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        dates = g["date"].to_list()
        def _curve(rets):
            eq = [1.0]
            for r in rets:
                eq.append(eq[-1] * (1 + r))
            return eq[1:]
        x = np.arange(len(dates))
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.plot(x, _curve(g["fc_ret"].to_list()), label="FC only (daily MTM)", lw=1.2)
        ax.plot(x, _curve(g["tsmom_ret"].to_list()), label=f"TSMOM {PRIMARY} only", lw=1.2)
        ax.plot(x, _curve(combined), label=f"combined w={w:.0%} tsmom", lw=1.6)
        ticks = list(range(0, len(dates), max(1, len(dates) // 8)))
        ax.set_xticks(ticks)
        ax.set_xticklabels([dates[i] for i in ticks], rotation=30, fontsize=8)
        ax.set_ylabel("equity (1x)")
        ax.legend()
        ax.grid(alpha=0.3)
        ax.set_title(f"LONG sleeve: FC daily-MTM vs majors TSMOM overlay ({root.name})")
        fig.tight_layout()
        fig.savefig(out_dir / "tsmom_overlay_curves.png", dpi=120)
        print(f"PNG -> {out_dir / 'tsmom_overlay_curves.png'}")
    except Exception as exc:  # noqa: BLE001 - plotting is best-effort
        print(f"(no PNG: {exc})")
    print(f"stats -> {out_dir / 'tsmom_overlay_stats.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
