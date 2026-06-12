"""P10 Stage-1 taker-flow squeeze-proxy scout on the frozen winner_base trade set.

Pre-registered in docs/preregistration/continuous-taker-flow-scout-2026-06-12.md
(registered BEFORE this run; external priors in
docs/research_notes_external_priors_2026-06-12.md). Reuses the OI scout's event
loader, OI lookups and stats so the falsifier-7 OI-proxy check shares the exact
machinery the OI Stage-1 used.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe \
        scripts/continuous_taker_flow_scout.py [--coverage-only]
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
import continuous_oi_flow_scout as oi_scout  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_taker_flow_scout_2026-06-12"
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
MS_H = 3_600_000
MS_5M = 300_000
IC_BAR = 0.08
COVERAGE_GATE = 0.60

# (window_start_offset_h, window_end_offset_h, min_buckets). Per the receipt's
# pre-run causality amendment every window additionally ends 5m early (no
# bucket may extend past t0 under either stamp convention).
WINDOWS = {
    "flow_support_6h": (6, 0, 48),
    "flow_support_24h": (24, 0, 192),
    "flow_support_6h_lag1": (7, 1, 48),
}


def bybit_flow_lookup(events: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Per-symbol 5m signed-imbalance series covering each event's windows."""
    root = ROOTS["bybit"] / "taker_flow_5m"
    need: dict[str, set[str]] = {}
    rows = events.select(
        pl.col("symbol").cast(pl.Utf8), pl.col("entry_signal_ts_ms").cast(pl.Int64)
    )
    for sym, t0 in rows.iter_rows():
        d0 = dt.datetime.fromtimestamp((t0 - 25 * MS_H) / 1000, tz=dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp(t0 / 1000, tz=dt.timezone.utc).date()
        s = need.setdefault(sym, set())
        d = d0
        while d <= d1:
            s.add(d.isoformat())
            d += dt.timedelta(days=1)
    out: dict[str, pl.DataFrame] = {}
    for sym, dates in need.items():
        frames = []
        for date in sorted(dates):
            p = root / f"date={date}" / f"symbol={sym}" / "part.parquet"
            if p.exists():
                frames.append(pl.read_parquet(p, columns=["ts_ms", "taker_buy_quote", "taker_sell_quote"]))
        if frames:
            df = (
                pl.concat(frames)
                .with_columns(
                    ((pl.col("taker_buy_quote") - pl.col("taker_sell_quote"))
                     / (pl.col("taker_buy_quote") + pl.col("taker_sell_quote"))).alias("imb")
                )
                .filter(pl.col("imb").is_not_null() & pl.col("imb").is_finite())
                .select(["ts_ms", "imb"])
                .sort("ts_ms")
            )
            # bybit buckets are START-stamped: bucket b covers [b, b+5m).
            # Re-stamp to bucket END so both venues share end-stamp semantics.
            out[sym] = df.with_columns((pl.col("ts_ms") + MS_5M).alias("ts_ms"))
    return out


def binance_flow_lookup(events: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """metrics_5m taker ratio -> bucket imbalance, observation-time stamps.

    ts_ms is treated as the observation/snapshot time (END of the 5m interval)
    — the same convention the OI scout used for the metrics snapshots.
    """
    root = ROOTS["binance"] / "binance_usdm_metrics_5m"
    out: dict[str, pl.DataFrame] = {}
    for sym in events["symbol"].unique().to_list():
        p = root / f"{sym}.parquet"
        if p.exists() and p.stat().st_size > 0:
            try:
                df = (
                    pl.read_parquet(p, columns=["ts_ms", "sum_taker_long_short_vol_ratio"])
                    .rename({"sum_taker_long_short_vol_ratio": "r"})
                    .filter(pl.col("r").is_not_null() & (pl.col("r") > 0))
                    .with_columns(((pl.col("r") - 1.0) / (pl.col("r") + 1.0)).alias("imb"))
                    .select(["ts_ms", "imb"])
                    .sort("ts_ms")
                )
                out[sym] = df
            except Exception:
                continue
    return out


def window_mean(ts: np.ndarray, imb: np.ndarray, t_start: int, t_end: int, min_buckets: int) -> float | None:
    """Mean bucket imbalance for end-stamped buckets in (t_start, t_end]."""
    i0 = np.searchsorted(ts, t_start, side="right")
    i1 = np.searchsorted(ts, t_end, side="right")
    vals = imb[i0:i1]
    if len(vals) < min_buckets:
        return None
    return float(np.mean(vals))


def compute_features(events: pl.DataFrame, flow: dict[str, pl.DataFrame],
                     oi: dict[str, pl.DataFrame]) -> pl.DataFrame:
    rows = []
    for r in events.to_dicts():
        t0 = int(r["entry_signal_ts_ms"])
        f: dict[str, float | None] = {k: None for k in WINDOWS}
        f["d_oi_6h"] = None
        ser = flow.get(r["symbol"])
        if ser is not None and ser.height:
            ts = ser["ts_ms"].to_numpy()
            imb = ser["imb"].to_numpy()
            for name, (h_start, h_end, min_b) in WINDOWS.items():
                f[name] = window_mean(
                    ts, imb, t0 - h_start * MS_H, t0 - h_end * MS_H - MS_5M, min_b
                )
        oser = oi.get(r["symbol"])
        if oser is not None:
            ots = oser["ts_ms"].to_numpy()
            ooi = oser["open_interest"].to_numpy()
            a = oi_scout.oi_at(ots, ooi, t0)
            b = oi_scout.oi_at(ots, ooi, t0 - 6 * MS_H)
            if a is not None and b is not None:
                f["d_oi_6h"] = a / b - 1.0
        rows.append({**r, **f})
    return pl.DataFrame(rows, infer_schema_length=None)


def partial_spearman(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> tuple[float, int]:
    rxy = oi_scout.spearman(x, y)[0]
    rxz = oi_scout.spearman(x, z)[0]
    rzy = oi_scout.spearman(z, y)[0]
    den = math.sqrt(max(1e-12, (1 - rxz**2) * (1 - rzy**2)))
    return (rxy - rxz * rzy) / den, len(x)


def venue_stats(df: pl.DataFrame) -> dict:
    out: dict = {"n_events": df.height}
    d = df.drop_nulls(["flow_support_6h", "net_return"])
    x = d["flow_support_6h"].to_numpy()
    y = d["net_return"].to_numpy()
    ic6, p6, n6 = oi_scout.spearman(x, y)
    out["coverage_6h"] = round(d.height / max(df.height, 1), 3)
    out["ic_6h"], out["p_6h"], out["n_6h"] = round(ic6, 4), round(p6, 5), n6

    d24 = df.drop_nulls(["flow_support_24h", "net_return"])
    ic24, p24, n24 = oi_scout.spearman(d24["flow_support_24h"].to_numpy(), d24["net_return"].to_numpy())
    out["ic_24h"], out["p_24h"], out["n_24h"] = round(ic24, 4), round(p24, 5), n24
    out["coverage_24h"] = round(d24.height / max(df.height, 1), 3)

    dl = df.drop_nulls(["flow_support_6h_lag1", "net_return"])
    icl, pl_, nl = oi_scout.spearman(dl["flow_support_6h_lag1"].to_numpy(), dl["net_return"].to_numpy())
    out["ic_6h_lag1"], out["n_lag1"] = round(icl, 4), nl

    # terciles on the primary
    if len(x) >= 30:
        q = np.quantile(x, [1 / 3, 2 / 3])
        out["tercile_top_mean"] = round(float(y[x >= q[1]].mean()), 5)
        out["tercile_bot_mean"] = round(float(y[x <= q[0]].mean()), 5)
        out["tercile_spread"] = round(out["tercile_top_mean"] - out["tercile_bot_mean"], 5)

    # falsifier 6: trigger-score proxy
    out["diag_corr_flow_score"] = round(oi_scout.spearman(x, d["score"].to_numpy())[0], 4)

    # falsifier 7: OI-proxy / incremental value on the joint-covered subset
    dj = df.drop_nulls(["flow_support_6h", "d_oi_6h", "net_return"])
    if dj.height >= 30:
        xj = dj["flow_support_6h"].to_numpy()
        yj = dj["net_return"].to_numpy()
        zj = dj["d_oi_6h"].to_numpy()
        out["diag_corr_flow_oi"] = round(oi_scout.spearman(xj, zj)[0], 4)
        pic, npj = partial_spearman(xj, yj, zj)
        out["partial_ic_6h_given_oi"], out["n_joint"] = round(pic, 4), npj
        # secondary (descriptive only): 2x2 mean net by sign(flow) x sign(dOI)
        cells = {}
        for fs, os_ in [(1, 1), (1, -1), (-1, 1), (-1, -1)]:
            m = (np.sign(xj) == fs) & (np.sign(zj) == os_)
            cells[f"flow{'+' if fs > 0 else '-'}_oi{'+' if os_ > 0 else '-'}"] = {
                "mean_net": round(float(yj[m].mean()), 5) if m.sum() >= 10 else None,
                "n": int(m.sum()),
            }
        out["two_by_two_descriptive"] = cells

    # per-year ICs (recency)
    yrs = {}
    if "entry_date" in d.columns and len(x) >= 30:
        yr = np.array([s[:4] for s in d["entry_date"].to_list()])
        for yv in sorted(set(yr)):
            m = yr == yv
            if m.sum() >= 30:
                r_, _, n_ = oi_scout.spearman(x[m], y[m])
                yrs[yv] = {"ic": round(r_, 4), "n": int(n_)}
    out["per_year_ic"] = yrs

    # falsifier 9: coverage-selection diagnostic
    cov_m = df.filter(pl.col("flow_support_6h").is_not_null())["net_return"]
    unc_m = df.filter(pl.col("flow_support_6h").is_null())["net_return"]
    out["coverage_selection_diag"] = {
        "covered_mean_net": round(float(cov_m.mean() or 0.0), 5),
        "uncovered_mean_net": round(float(unc_m.mean() or 0.0), 5) if unc_m.len() else None,
        "n_uncovered": unc_m.len(),
    }
    return out


def coverage_only(df: pl.DataFrame) -> dict:
    return {
        "n_events": df.height,
        "coverage_6h": round(1.0 - df["flow_support_6h"].null_count() / max(df.height, 1), 3),
        "coverage_24h": round(1.0 - df["flow_support_24h"].null_count() / max(df.height, 1), 3),
        "coverage_lag1": round(1.0 - df["flow_support_6h_lag1"].null_count() / max(df.height, 1), 3),
        "coverage_oi_joint": round(
            df.drop_nulls(["flow_support_6h", "d_oi_6h"]).height / max(df.height, 1), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage-only", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "preregistration": "docs/preregistration/continuous-taker-flow-scout-2026-06-12.md",
        "mode": "coverage_only" if args.coverage_only else "full",
    }
    for venue in ["bybit", "binance"]:
        ev = oi_scout.load_events(venue)
        flow = bybit_flow_lookup(ev) if venue == "bybit" else binance_flow_lookup(ev)
        oi = oi_scout.bybit_oi_lookup(ev) if venue == "bybit" else oi_scout.binance_oi_lookup(ev)
        feats = compute_features(ev, flow, oi)
        feats.write_parquet(OUT / f"events_{venue}.parquet")
        report[venue] = coverage_only(feats) if args.coverage_only else venue_stats(feats)
        print(f"[{venue}] {json.dumps(report[venue], indent=2)}", flush=True)

    if not args.coverage_only:
        b, n = report["bybit"], report["binance"]
        conds = {
            "primary_ic_both": bool(abs(b["ic_6h"]) >= IC_BAR and abs(n["ic_6h"]) >= IC_BAR
                                    and b["p_6h"] < 0.05 and n["p_6h"] < 0.05),
            "primary_sign_agreement": bool(np.sign(b["ic_6h"]) == np.sign(n["ic_6h"])),
            "coverage_gate": bool(b["coverage_6h"] >= COVERAGE_GATE and n["coverage_6h"] >= COVERAGE_GATE),
            "confirm_24h_sign": bool(np.sign(b["ic_24h"]) == np.sign(b["ic_6h"])
                                     and np.sign(n["ic_24h"]) == np.sign(n["ic_6h"])),
            "confirm_lag_sign": bool(np.sign(b["ic_6h_lag1"]) == np.sign(b["ic_6h"])
                                     and np.sign(n["ic_6h_lag1"]) == np.sign(n["ic_6h"])),
            "not_trigger_proxy": bool(abs(b["diag_corr_flow_score"]) < 0.3
                                      and abs(n["diag_corr_flow_score"]) < 0.3),
            "incremental_vs_oi": bool(
                np.sign(b.get("partial_ic_6h_given_oi", 0.0)) == np.sign(b["ic_6h"])
                and np.sign(n.get("partial_ic_6h_given_oi", 0.0)) == np.sign(n["ic_6h"])),
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
