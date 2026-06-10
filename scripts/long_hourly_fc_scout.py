"""LR-scout: hourly-cadence FC detection feasibility (descriptive; NOT a promotion cell).

Pre-registered in docs/preregistration/long-regularity-program-2026-06-10.md.
At each hourly close: trailing-24h return vs a 2.5-sigma(daily,30d) threshold
(floor log1p(0.15)), trailing-24h close-location >= 0.7, trailing-24h turnover rank
<= 10, BTC+ETH daily-SMA30 regime causal through the PRIOR day, >=30d listing
history. Events deduped (no trigger in prior 24h) + 7d per-symbol cooldown, mirroring
the engine's per-trade cooldown. Compared against a daily-grid 1d-trigger replica
(3d/7d triggers omitted — declared simplification). Forward returns costed 12 bps RT,
entry = trigger-hour close.

Usage:
    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python scripts/long_hourly_fc_scout.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import polars as pl

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "long_hourly_scout_2026-06-10"
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
START, END = dt.date(2023, 4, 1), dt.date(2026, 5, 28)
WARMUP_DAYS = 45  # sigma/SMA/listing warmup before START
RT_COST = 12.0 / 1e4
SIGMA_MULT, FIXED_THR, CLOSE_LOC_MIN, RANK_MAX = 2.5, math.log1p(0.15), 0.7, 10
MIN_LISTING_DAYS, COOLDOWN_H, HORIZONS = 30, 7 * 24, (24, 48, 72)
STABLES = {
    "BUSDUSDT", "DAIUSDT", "FDUSDUSDT", "FRAXUSDT", "GUSDUSDT", "LUSDUSDT", "PYUSDUSDT",
    "SUSDUSDT", "TUSDUSDT", "USD1USDT", "USDDUSDT", "USDCUSDT", "USDEUSDT", "USDPUSDT",
    "USTCUSDT", "USDYUSDT",
}


def load_hourly(root: Path) -> pl.DataFrame:
    frames = []
    day = START - dt.timedelta(days=WARMUP_DAYS)
    horizon_pad = END + dt.timedelta(days=4)
    while day <= horizon_pad:
        part = root / "klines_1h" / f"date={day.isoformat()}"
        if part.exists():
            frames.append(pl.read_parquet(
                part, columns=["ts_ms", "symbol", "high", "low", "close", "turnover_quote"]))
        day += dt.timedelta(days=1)
    return pl.concat(frames)


def daily_context(hourly: pl.DataFrame) -> tuple[pl.DataFrame, dict, dict]:
    """Per-symbol daily closes -> sigma30, listing age; BTC/ETH regime by day."""
    d = hourly.with_columns(
        (pl.col("ts_ms").cast(pl.Datetime("ms")).dt.date()).alias("d")
    ).sort("ts_ms").group_by(["symbol", "d"], maintain_order=True).agg(
        pl.col("close").last(), pl.col("turnover_quote").sum()
    ).sort(["symbol", "d"])
    d = d.with_columns(
        (pl.col("close").log() - pl.col("close").log().shift(1)).over("symbol").alias("lr"),
        pl.int_range(pl.len()).over("symbol").alias("age_days"),
    ).with_columns(
        pl.col("lr").rolling_std(30, min_samples=20).over("symbol").alias("sigma30"),
    )
    regimes = {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        s = d.filter(pl.col("symbol") == sym).sort("d").with_columns(
            pl.col("close").rolling_mean(30, min_samples=30).alias("sma30")
        )
        regimes[sym] = {row["d"]: row["close"] > row["sma30"]
                        for row in s.iter_rows(named=True) if row["sma30"] is not None}
    return d, regimes["BTCUSDT"], regimes["ETHUSDT"]


def hourly_signals(hourly: pl.DataFrame, daily: pl.DataFrame) -> pl.DataFrame:
    h = hourly.sort(["symbol", "ts_ms"]).with_columns(
        (pl.col("close").log() - pl.col("close").log().shift(24)).over("symbol").alias("ret24_log"),
        pl.col("high").rolling_max(24, min_samples=24).over("symbol").alias("hi24"),
        pl.col("low").rolling_min(24, min_samples=24).over("symbol").alias("lo24"),
        pl.col("turnover_quote").rolling_sum(24, min_samples=24).over("symbol").alias("turn24"),
        (pl.col("ts_ms").cast(pl.Datetime("ms")).dt.date()).alias("d"),
    ).with_columns(
        ((pl.col("close") - pl.col("lo24")) / (pl.col("hi24") - pl.col("lo24"))).alias("cl24"),
        (pl.col("d") - pl.duration(days=1)).alias("d_prev"),
    )
    ctx = daily.select(
        pl.col("symbol"), pl.col("d").alias("d_prev"),
        pl.col("sigma30").alias("sigma30_prev"), pl.col("age_days").alias("age_prev"),
    )
    h = h.join(ctx, on=["symbol", "d_prev"], how="left")
    h = h.with_columns(
        pl.col("turn24").rank(method="ordinal", descending=True).over("ts_ms").alias("rank24")
    )
    thr = pl.max_horizontal(pl.col("sigma30_prev") * SIGMA_MULT, pl.lit(FIXED_THR))
    return h.with_columns(
        ((pl.col("ret24_log") >= thr) & (pl.col("cl24") >= CLOSE_LOC_MIN)
         & (pl.col("rank24") <= RANK_MAX) & (pl.col("age_prev") >= MIN_LISTING_DAYS)
         ).fill_null(False).alias("raw_trigger")
    )


def daily_replica_events(daily: pl.DataFrame, hourly: pl.DataFrame,
                         btc_reg: dict, eth_reg: dict) -> set[tuple[str, dt.date]]:
    """Daily-grid 1d-trigger replica: day log-return >= thr, daily close-location >= 0.7,
    daily turnover rank <= 10, regimes causal (prior day), listing age."""
    dd = hourly.with_columns(
        (pl.col("ts_ms").cast(pl.Datetime("ms")).dt.date()).alias("d")
    ).sort("ts_ms").group_by(["symbol", "d"], maintain_order=True).agg(
        pl.col("high").max(), pl.col("low").min(), pl.col("close").last(),
        pl.col("turnover_quote").sum(),
    ).sort(["symbol", "d"])
    dd = dd.join(
        daily.select("symbol", "d", "lr", "sigma30", "age_days"), on=["symbol", "d"], how="left"
    ).with_columns(
        ((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low"))).alias("cl"),
        pl.col("turnover_quote").rank(method="ordinal", descending=True).over("d").alias("rank_d"),
        pl.col("sigma30").shift(1).over("symbol").alias("sigma_prev"),
        pl.col("age_days").shift(1).over("symbol").alias("age_prev"),
        (pl.col("d") - pl.duration(days=1)).alias("d_prev"),
    )
    out = set()
    thr_expr = pl.max_horizontal(pl.col("sigma_prev") * SIGMA_MULT, pl.lit(FIXED_THR))
    ev = dd.filter(
        (pl.col("lr") >= thr_expr) & (pl.col("cl") >= CLOSE_LOC_MIN)
        & (pl.col("rank_d") <= RANK_MAX) & (pl.col("age_prev") >= MIN_LISTING_DAYS)
        & (pl.col("d") >= START) & (pl.col("d") <= END)
        & (~pl.col("symbol").is_in(list(STABLES))) & (pl.col("symbol") != "BTCUSDT")
    )
    for row in ev.iter_rows(named=True):
        dp = row["d_prev"]
        if btc_reg.get(dp) and eth_reg.get(dp):
            out.add((row["symbol"], row["d"]))
    return out


def pe_columns(ev: dict, ts_arr: np.ndarray, cl_arr: np.ndarray, shared: bool) -> dict | None:
    """PE1 provisional-entry estimate columns (long-provisional-entry-2026-06-10.md)."""
    ts = ev["ts_ms"]
    i = int(np.searchsorted(ts_arr, ts))
    if i >= len(ts_arr) or ts_arr[i] != ts:
        return None
    entry_trig = cl_arr[i]
    day_start = (ts // 86_400_000) * 86_400_000
    next_day_0100 = day_start + 86_400_000 + 3_600_000
    j72 = min(int(np.searchsorted(ts_arr, ts + 72 * 3_600_000)), len(ts_arr) - 1)
    exit72 = cl_arr[j72]
    # cut at the trigger day's last hourly close (uses only the completed daily bar)
    k = int(np.searchsorted(ts_arr, day_start + 86_400_000)) - 1
    cut_px = cl_arr[max(k, i)]
    out = {"entry_trig": float(entry_trig), "exit72": float(exit72), "cut_px": float(cut_px)}
    if shared:
        m = int(np.searchsorted(ts_arr, next_day_0100))
        if m >= len(ts_arr):
            return None
        entry_eng = cl_arr[m]
        out["engine_net"] = float(exit72 / entry_eng - 1.0 - RT_COST)
        out["prov_net"] = float(exit72 / entry_trig - 1.0 - RT_COST)
    else:
        out["engine_net"] = None
        out["prov_net"] = float(cut_px / entry_trig - 1.0 - RT_COST)
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pe", action="store_true", help="PE1 provisional-entry analysis")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"preregistration": "docs/preregistration/long-regularity-program-2026-06-10.md"}
    for venue, root in ROOTS.items():
        hourly = load_hourly(root)
        daily, btc_reg, eth_reg = daily_context(hourly)
        sig = hourly_signals(hourly, daily)
        daily_ev = daily_replica_events(daily, hourly, btc_reg, eth_reg)
        print(f"[{venue}] hourly rows {sig.height}; daily replica events {len(daily_ev)}")

        # close paths for forward returns
        closes: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for sym, grp in sig.group_by("symbol"):
            g = grp.sort("ts_ms")
            closes[sym[0] if isinstance(sym, tuple) else sym] = (
                g["ts_ms"].to_numpy(), g["close"].to_numpy())

        trig = sig.filter(
            pl.col("raw_trigger") & (pl.col("d") >= START) & (pl.col("d") <= END)
            & (~pl.col("symbol").is_in(list(STABLES))) & (pl.col("symbol") != "BTCUSDT")
        ).sort("ts_ms")
        last_event_ts: dict[str, int] = {}
        events = []
        for row in trig.iter_rows(named=True):
            dp = row["d_prev"]
            if not (btc_reg.get(dp) and eth_reg.get(dp)):
                continue
            sym, ts = row["symbol"], row["ts_ms"]
            prev = last_event_ts.get(sym)
            if prev is not None and ts - prev < COOLDOWN_H * 3_600_000:
                continue
            last_event_ts[sym] = ts
            events.append({"symbol": sym, "ts_ms": ts, "d": row["d"]})
        print(f"[{venue}] hourly events {len(events)}")

        rows = []
        for ev in events:
            ts_arr, cl_arr = closes[ev["symbol"]]
            i = int(np.searchsorted(ts_arr, ev["ts_ms"]))
            if i >= len(ts_arr) or ts_arr[i] != ev["ts_ms"]:
                continue
            entry = cl_arr[i]
            fwd = {}
            for hz in HORIZONS:
                j = int(np.searchsorted(ts_arr, ev["ts_ms"] + hz * 3_600_000))
                j = min(j, len(ts_arr) - 1)
                fwd[hz] = cl_arr[j] / entry - 1.0 - RT_COST
            shared = (ev["symbol"], ev["d"]) in daily_ev
            row = {"symbol": ev["symbol"], "ts_ms": int(ev["ts_ms"]),
                   "date": str(ev["d"]), "shared_with_daily": shared,
                   **{f"net_{hz}h": round(float(fwd[hz]), 6) for hz in HORIZONS}}
            if args.pe:
                pe = pe_columns(ev, ts_arr, cl_arr, shared)
                if pe is None:
                    continue
                row.update(pe)
            rows.append(row)
        evdf = pl.DataFrame(rows)
        evdf.write_parquet(OUT / (f"{venue}_events_pe.parquet" if args.pe else f"{venue}_events.parquet"))

        stats: dict = {"hourly_events": len(rows), "daily_replica_events": len(daily_ev)}
        for cls, sub in [("shared", evdf.filter(pl.col("shared_with_daily"))),
                         ("extra", evdf.filter(~pl.col("shared_with_daily")))]:
            stats[cls] = {"n": sub.height}
            for hz in HORIZONS:
                col = sub[f"net_{hz}h"]
                stats[cls][f"net_{hz}h_mean"] = round(float(col.mean()), 5) if sub.height else None
                stats[cls][f"net_{hz}h_median"] = round(float(col.median()), 5) if sub.height else None
        if args.pe:
            conf = evdf.filter(pl.col("shared_with_daily"))
            unconf = evdf.filter(~pl.col("shared_with_daily"))
            n = evdf.height
            prov_ev = float(evdf["prov_net"].mean())
            engine_ev = float(conf["engine_net"].mean()) * conf.height / n if conf.height else 0.0
            cut_losses = unconf["prov_net"]
            stats["pe"] = {
                "n_total": n, "n_confirmed": conf.height,
                "prov_policy_ev": round(prov_ev, 5),
                "engine_proxy_ev": round(engine_ev, 5),
                "ev_ratio": round(prov_ev / engine_ev, 3) if engine_ev else None,
                "confirmed_prov_net_mean": round(float(conf["prov_net"].mean()), 5) if conf.height else None,
                "confirmed_engine_net_mean": round(float(conf["engine_net"].mean()), 5) if conf.height else None,
                "cut_loss_mean": round(float(cut_losses.mean()), 5) if unconf.height else None,
                "cut_loss_p10": round(float(cut_losses.quantile(0.10)), 5) if unconf.height else None,
            }
        report[venue] = stats
        print(json.dumps(stats, indent=1))
        del hourly, sig, closes

    out_name = "report_pe.json" if args.pe else "report.json"
    with open(OUT / out_name, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"wrote {OUT / out_name}")


if __name__ == "__main__":
    main()
