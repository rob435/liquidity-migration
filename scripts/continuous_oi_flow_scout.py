"""Stage-1 OI-flow squeeze-proxy scout on the frozen winner_base trade set.

Pre-registered in docs/preregistration/continuous-oi-flow-scout-2026-06-10.md.
Outcome statistics are REFUSED until the binance metrics backfill manifest is
complete (procedure note in the receipt); --coverage-only computes feature
coverage fractions ONLY (no return statistics).

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/continuous_oi_flow_scout.py [--coverage-only]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import continuous_ensemble_rebalance_scout as scout  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_oi_flow_scout_2026-06-10"
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
# dedup priority = ensemble weight order (receipt)
COMP_PRIORITY = ["turn4p5", "turn3p3", "turn4p3", "age210tp14"]
MS_H = 3_600_000
STALE_MS = 3 * MS_H  # an OI endpoint counts as present only if a snapshot exists within 3h
IC_BAR = 0.08


def load_events(venue: str) -> pl.DataFrame:
    frames = []
    for rank, src in enumerate(COMP_PRIORITY):
        spec = scout.SOURCES[src]
        t = pl.read_csv(
            spec.root / venue / spec.cell / "continuous_trades.csv",
            columns=["symbol", "entry_signal_ts_ms", "entry_ts_ms", "net_return",
                     "gross_trade_return", "notional_weight", "cost_return", "score", "entry_date"],
        ).with_columns(pl.lit(rank).alias("prio"), pl.lit(src).alias("comp"))
        frames.append(t)
    allt = pl.concat(frames)
    return (
        allt.sort("prio")
        .unique(subset=["symbol", "entry_signal_ts_ms"], keep="first")
        .with_columns(
            # hourly turnover at entry from the exact cost-model inversion (pi = ((bps-16)/100)^2)
            ((-pl.col("cost_return") / pl.col("notional_weight").abs().clip(1e-12) * 1e4 - 16.0)
             .clip(0.0) / 100.0).pow(2).alias("pi"),
        )
        .with_columns(
            (pl.col("notional_weight").abs() * 1e6 / pl.col("pi").clip(1e-12)).alias("hourly_turnover")
        )
    )


def bybit_oi_lookup(events: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Per-symbol OI series covering each event's [t0-26h, t0] windows."""
    root = ROOTS["bybit"] / "open_interest"
    need: dict[str, set[str]] = {}
    for sym, t0 in events.select(["symbol", "entry_signal_ts_ms"]).iter_rows():
        d0 = dt.datetime.fromtimestamp((t0 - 26 * MS_H) / 1000, tz=dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp(t0 / 1000, tz=dt.timezone.utc).date()
        s = need.setdefault(sym, set())
        d = d0
        while d <= d1:
            s.add(d.isoformat())
            d += dt.timedelta(days=1)
    out: dict[str, list[pl.DataFrame]] = {}
    for sym, dates in need.items():
        frames = []
        for date in sorted(dates):
            p = root / f"date={date}" / f"symbol={sym}"
            if p.exists():
                frames.append(pl.read_parquet(p, columns=["ts_ms", "open_interest"]))
        if frames:
            out[sym] = [pl.concat(frames).sort("ts_ms")]
    return {s: f[0] for s, f in out.items()}


def binance_oi_lookup(events: pl.DataFrame) -> dict[str, pl.DataFrame]:
    root = ROOTS["binance"] / "binance_usdm_metrics_5m"
    out = {}
    for sym in events["symbol"].unique().to_list():
        p = root / f"{sym}.parquet"
        if p.exists() and p.stat().st_size > 0:
            try:
                df = pl.read_parquet(p, columns=["ts_ms", "sum_open_interest"]).rename(
                    {"sum_open_interest": "open_interest"}).sort("ts_ms")
                out[sym] = df
            except Exception:
                continue
    return out


def oi_at(series_ts: np.ndarray, series_oi: np.ndarray, t: int) -> float | None:
    i = np.searchsorted(series_ts, t, side="right") - 1
    if i < 0 or t - series_ts[i] > STALE_MS:
        return None
    v = series_oi[i]
    return float(v) if v and v > 0 else None


def compute_features(events: pl.DataFrame, lookup: dict[str, pl.DataFrame]) -> pl.DataFrame:
    rows = []
    for r in events.to_dicts():
        ser = lookup.get(r["symbol"])
        f = {"d_oi_6h": None, "d_oi_24h": None, "d_oi_6h_lag1": None}
        if ser is not None:
            ts = ser["ts_ms"].to_numpy()
            oi = ser["open_interest"].to_numpy()
            t0 = int(r["entry_signal_ts_ms"])
            for name, t_end, t_start in [
                ("d_oi_6h", t0, t0 - 6 * MS_H),
                ("d_oi_24h", t0, t0 - 24 * MS_H),
                ("d_oi_6h_lag1", t0 - MS_H, t0 - 7 * MS_H),
            ]:
                a, b = oi_at(ts, oi, t_end), oi_at(ts, oi, t_start)
                if a is not None and b is not None:
                    f[name] = a / b - 1.0
        rows.append({**r, **f})
    return pl.DataFrame(rows, infer_schema_length=None)


def spearman(x: np.ndarray, y: np.ndarray) -> tuple[float, float, int]:
    n = len(x)
    if n < 10:
        return 0.0, 1.0, n
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rho = float(np.corrcoef(rx, ry)[0, 1])
    t = rho * math.sqrt((n - 2) / max(1e-12, 1.0 - rho * rho))
    # two-sided p via normal approx (n large)
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return rho, p, n


def venue_stats(df: pl.DataFrame) -> dict:
    d = df.drop_nulls(["d_oi_6h", "net_return"])
    x = d["d_oi_6h"].to_numpy()
    y = d["net_return"].to_numpy()
    ic6, p6, n6 = spearman(x, y)
    d24 = df.drop_nulls(["d_oi_24h", "net_return"])
    ic24, p24, n24 = spearman(d24["d_oi_24h"].to_numpy(), d24["net_return"].to_numpy())
    dl = df.drop_nulls(["d_oi_6h_lag1", "net_return"])
    icl, pl_, nl = spearman(dl["d_oi_6h_lag1"].to_numpy(), dl["net_return"].to_numpy())
    # terciles on d_oi_6h
    q = np.quantile(x, [1 / 3, 2 / 3]) if len(x) else [0, 0]
    top = y[x >= q[1]].mean() if len(x) else 0.0
    bot = y[x <= q[0]].mean() if len(x) else 0.0
    # per-year ICs
    yrs = {}
    if "entry_date" in d.columns:
        yr = np.array([s[:4] for s in d["entry_date"].to_list()])
        for yv in sorted(set(yr)):
            m = yr == yv
            if m.sum() >= 30:
                r, p, n = spearman(x[m], y[m])
                yrs[yv] = {"ic": round(r, 4), "n": int(n)}
    liq = d.filter(pl.col("hourly_turnover") >= 1_000_000)
    ic_liq, p_liq, n_liq = spearman(liq["d_oi_6h"].to_numpy(), liq["net_return"].to_numpy())
    score_corr = spearman(x, d["score"].to_numpy())[0] if "score" in d.columns else None
    # Amendment-1 validity diagnostic: are uncovered (delisted-symbol) events outcome-different?
    cov_m = df.filter(pl.col("d_oi_6h").is_not_null())["net_return"]
    unc_m = df.filter(pl.col("d_oi_6h").is_null())["net_return"]
    survivor_bias = {
        "covered_mean_net": round(float(cov_m.mean() or 0.0), 5),
        "uncovered_mean_net": round(float(unc_m.mean() or 0.0), 5) if unc_m.len() else None,
        "covered_winrate": round(float((cov_m > 0).mean() or 0.0), 3),
        "uncovered_winrate": round(float((unc_m > 0).mean() or 0.0), 3) if unc_m.len() else None,
        "n_uncovered": unc_m.len(),
    }
    return {
        "n_events": df.height,
        "coverage_6h": round(1.0 - df["d_oi_6h"].null_count() / max(df.height, 1), 3),
        "coverage_24h": round(1.0 - df["d_oi_24h"].null_count() / max(df.height, 1), 3),
        "ic_6h": round(ic6, 4), "p_6h": round(p6, 5), "n_6h": n6,
        "ic_24h": round(ic24, 4), "p_24h": round(p24, 5), "n_24h": n24,
        "ic_6h_lag1": round(icl, 4), "n_lag1": nl,
        "tercile_top_mean": round(float(top), 5), "tercile_bot_mean": round(float(bot), 5),
        "tercile_spread": round(float(top - bot), 5),
        "per_year_ic": yrs,
        "ic_6h_liquid1m": round(ic_liq, 4), "n_liquid": n_liq,
        "diag_corr_oi_score": round(score_corr, 4) if score_corr is not None else None,
        "survivor_bias_diag": survivor_bias,
    }


def coverage_only(df: pl.DataFrame) -> dict:
    return {
        "n_events": df.height,
        "coverage_6h": round(1.0 - df["d_oi_6h"].null_count() / max(df.height, 1), 3),
        "coverage_24h": round(1.0 - df["d_oi_24h"].null_count() / max(df.height, 1), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="run outcomes even if backfill manifest incomplete")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    # backfill completeness gate (receipt procedure note)
    manifest = ROOTS["binance"] / "binance_usdm_metrics_5m" / "_manifest.json"
    complete = False
    if manifest.exists():
        m = json.loads(manifest.read_text())
        complete = len(m) >= 600  # ~685 expected; allow stragglers with no data
    if not args.coverage_only and not complete and not args.force:
        raise SystemExit("binance metrics backfill incomplete — outcome statistics refused "
                         "(receipt procedure note). Use --coverage-only for plumbing checks.")

    report: dict = {"preregistration": "docs/preregistration/continuous-oi-flow-scout-2026-06-10.md",
                    "mode": "coverage_only" if args.coverage_only else "full"}
    for venue in ["bybit", "binance"]:
        ev = load_events(venue)
        lookup = bybit_oi_lookup(ev) if venue == "bybit" else binance_oi_lookup(ev)
        feats = compute_features(ev, lookup)
        feats.write_parquet(OUT / f"events_{venue}.parquet")
        if args.coverage_only:
            report[venue] = coverage_only(feats)
        else:
            report[venue] = venue_stats(feats)
        print(f"[{venue}] {json.dumps(report[venue], indent=2)}", flush=True)

    if not args.coverage_only:
        b, n = report["bybit"], report["binance"]
        conds = {
            "primary_ic_both": bool(b["ic_6h"] >= IC_BAR and n["ic_6h"] >= IC_BAR
                                    and b["p_6h"] < 0.05 and n["p_6h"] < 0.05),
            "confirm_24h_sign": bool(np.sign(b["ic_24h"]) == np.sign(b["ic_6h"]) and np.sign(n["ic_24h"]) == np.sign(n["ic_6h"])),
            "confirm_tercile": bool(b["tercile_spread"] > 0 and n["tercile_spread"] > 0),
            "confirm_lag": bool(np.sign(b["ic_6h_lag1"]) == np.sign(b["ic_6h"]) and np.sign(n["ic_6h_lag1"]) == np.sign(n["ic_6h"])),
        }
        report["conditions"] = conds
        report["PASS"] = bool(all(conds.values()))
        print("\n==== VERDICT ====")
        print(json.dumps({"conditions": conds, "PASS": report["PASS"]}, indent=2))

    with open(OUT / ("coverage.json" if args.coverage_only else "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
