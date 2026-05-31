#!/usr/bin/env python3
"""CV1 — cross-venue asymmetry decomposition (read-only, EXPLORATORY diagnostic).

Question: the program's standing caveat is "Bybit strong, Binance weak" (e.g. MAR 2.76
vs 0.28 on the E1 baseline). Is that an EDGE-QUALITY asymmetry (the per-trade short edge
is genuinely weaker on Binance) or something else (breadth / universe composition)?

Method (read-only, on the validated age-gated ledgers e2/02_age_min, both venues):
  1. per-venue per-trade net stats (ALL / EARLY / RECENT), trade count, symbol count;
  2. symbol-overlap;
  3. MATCHED (symbol, trading-day) events on BOTH venues — the clean control: if the
     same coin/day gives the same outcome, the edge is venue-general;
  4. matched vs venue-only per-venue (where does any gap live?);
  5. per-trade net by turnover-ratio bucket (tests an intensity-filter sub-hypothesis).

Finding (2026-05-30): the per-trade edge is VENUE-GENERAL (matched corr ~0.89, paired
diff ~0, Binance >= Bybit on shared coins). Binance's lower aggregate = (a) ~half the
events (breadth: fewer mid-liquidity alt perps) + (b) its venue-unique coins are weak
marginal shorts (less liquid, weaker turnover spike, recent). No clean single-feature
filter (turnover-ratio corr ~0.03-0.08 non-monotone; rank-tighten previously rejected)
removes the weak coins without dropping good ones. => the cross-venue gap is a
BREADTH/COMPOSITION effect, NOT an edge-quality or regime asymmetry. EXPLORATORY (a
characterization of validated ledgers; never promotion evidence).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl


def _day(ms: int) -> str:
    return datetime.fromtimestamp((int(ms) - 1) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _load(report_dir: Path) -> pl.DataFrame:
    csv = report_dir / "volume_event_best_trades.csv"
    if not csv.exists():
        raise SystemExit(f"no volume_event_best_trades.csv under {report_dir}")
    df = pl.read_csv(csv).filter(pl.col("side") == "short")
    return df.with_columns(pl.col("entry_signal_ts_ms").map_elements(_day, return_dtype=pl.String).alias("d"))


def _stats(df: pl.DataFrame) -> dict:
    nr = df["net_return"]
    return {"n": df.height, "net_median_pct": round(nr.median() * 100, 3),
            "net_mean_pct": round(nr.mean() * 100, 3), "win_pct": round((nr > 0).mean() * 100, 1),
            "net_sum_pct": round(nr.sum() * 100, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description="CV1 cross-venue decomposition (read-only).")
    ap.add_argument("--bybit-report", required=True)
    ap.add_argument("--binance-report", required=True)
    ap.add_argument("--split-date", default="2025-06-01")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args()

    L = {"bybit": _load(Path(args.bybit_report).expanduser()),
         "binance": _load(Path(args.binance_report).expanduser())}
    out: dict = {"split_date": args.split_date, "per_venue": {}, "matched": {}, "turnover_buckets": {}}

    for v, df in L.items():
        out["per_venue"][v] = {
            "symbols": df["symbol"].n_unique(),
            "ALL": _stats(df),
            "EARLY": _stats(df.filter(pl.col("d") < args.split_date)),
            "RECENT": _stats(df.filter(pl.col("d") >= args.split_date)),
        }

    keys = {v: set(zip(df["symbol"].to_list(), df["d"].to_list())) for v, df in L.items()}
    both = keys["bybit"] & keys["binance"]
    out["matched"]["n_events_on_both"] = len(both)
    b = L["bybit"].select(["symbol", "d", "net_return"]).rename({"net_return": "by"})
    n = L["binance"].select(["symbol", "d", "net_return"]).rename({"net_return": "bn"})
    m = b.join(n, on=["symbol", "d"], how="inner")
    if m.height:
        out["matched"].update({
            "bybit": _stats(m.rename({"by": "net_return"})),
            "binance": _stats(m.rename({"bn": "net_return"})),
            "corr": round(m.select(pl.corr("by", "bn")).item(), 3),
            "paired_diff_by_minus_bn_pct": round((m["by"] - m["bn"]).mean() * 100, 3),
        })
    for v, df in L.items():
        kk = both
        mm = df.with_columns(pl.struct(["symbol", "d"]).map_elements(
            lambda s: (s["symbol"], s["d"]) in kk, return_dtype=pl.Boolean).alias("_m"))
        out["matched"].setdefault("split", {})[v] = {
            "matched": _stats(mm.filter(pl.col("_m"))),
            "venue_only": _stats(mm.filter(~pl.col("_m"))),
        }

    for v, df in L.items():
        if "liquidity_migration_turnover_ratio" in df.columns:
            d2 = df.with_columns(pl.col("liquidity_migration_turnover_ratio").cut(
                [8, 12, 20], labels=["6-8", "8-12", "12-20", "20+"]).alias("tb"))
            g = d2.group_by("tb").agg(pl.col("net_return").mean().alias("m"), pl.len().alias("n")).sort("tb")
            out["turnover_buckets"][v] = {r["tb"]: {"net_mean_pct": round(r["m"] * 100, 3), "n": r["n"]}
                                          for r in g.iter_rows(named=True)}
            out["turnover_buckets"].setdefault("corr", {})[v] = round(
                df.select(pl.corr("liquidity_migration_turnover_ratio", "net_return")).item(), 3)

    print(json.dumps(out, indent=2))
    if args.output_json:
        Path(args.output_json).expanduser().write_text(json.dumps(out, indent=2))
        print(f"-> {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
