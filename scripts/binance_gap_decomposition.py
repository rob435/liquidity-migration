#!/usr/bin/env python3
"""Decompose the cross-venue short-sleeve gap: why does the same profile earn far less
on Binance than on Bybit?

Three competing hypotheses, each with a different consequence:
  A. SELECTION — events fire on Binance but the fade doesn't happen there
     (edge is Bybit-microstructure; the strategy is venue-idiosyncratic).
  B. EXECUTION — the fade exists (gross price P&L comparable) but costs/funding eat it
     (survivable: cost work or venue-specific sizing).
  C. UNIVERSE — events fire on different names (listing overlap is low; the venues
     aren't trading the same phenomenon).

Plus the funding-coverage artifact (2026-06-09 audit): the local Binance funding dataset
covers only ~51 symbols; every other trade books funding_mode=missing => zero funding.
Empirically this strategy's shorts PAY funding on net (Bybit baseline: funding sums to
-11.6% over 596 trades), so missing coverage usually omits a COST (local Binance results
optimistic). This script quantifies the bias band either way, using Bybit's complete
per-trade funding distribution as the proxy.

Inputs: the two venues' baseline trade ledgers from the same sweep (e.g. the Tier-2
battery 00_baseline cells). Analysis-only.

    POLARS_MAX_THREADS=4 .venv/bin/python scripts/binance_gap_decomposition.py \
        --bybit-trades   ~/SHARED_DATA/bybit_full_pit/reports/btc_gate_tier2_2026-06-09/00_baseline/volume_event_best_trades.csv \
        --binance-trades ~/SHARED_DATA/binance_full_pit/reports/btc_gate_tier2_2026-06-09/00_baseline/volume_event_best_trades.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl


def _per_venue_stats(t: pl.DataFrame) -> dict:
    cols = t.columns
    g = "gross_return" if "gross_return" in cols else "gross_trade_return"
    out = {
        "trades": t.height,
        "symbols": t["symbol"].n_unique(),
        "mean_gross": float(t[g].mean()) if g in cols else None,
        "median_gross": float(t[g].median()) if g in cols else None,
        "win_rate_gross": float((t[g] > 0).mean()) if g in cols else None,
        "mean_cost": float(t["cost_return"].mean()) if "cost_return" in cols else None,
        "mean_funding": float(t["funding_return"].mean()) if "funding_return" in cols else None,
        "mean_net": float(t["net_return"].mean()) if "net_return" in cols else None,
        "sum_net": float(t["net_return"].sum()) if "net_return" in cols else None,
    }
    if "funding_mode" in cols:
        vc = {r["funding_mode"]: r["count"] for r in t["funding_mode"].value_counts().to_dicts()}
        out["funding_modes"] = vc
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bybit-trades", required=True)
    ap.add_argument("--binance-trades", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    by = pl.read_csv(Path(args.bybit_trades).expanduser())
    bn = pl.read_csv(Path(args.binance_trades).expanduser())
    s_by, s_bn = _per_venue_stats(by), _per_venue_stats(bn)

    lines: list[str] = []
    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    emit("=== BINANCE GAP DECOMPOSITION (baseline ungated short profile, both venues) ===\n")
    emit("| metric | bybit | binance |")
    emit("|---|---:|---:|")
    for k in ("trades", "symbols", "mean_gross", "median_gross", "win_rate_gross",
              "mean_cost", "mean_funding", "mean_net", "sum_net"):
        def f(v):
            return f"{v:+.4%}" if isinstance(v, float) and abs(v) < 1 else f"{v}"
        emit(f"| {k} | {f(s_by.get(k))} | {f(s_bn.get(k))} |")
    emit(f"\nfunding modes — bybit: {s_by.get('funding_modes')}   binance: {s_bn.get('funding_modes')}")

    # A vs B: is the gross fade there on Binance?
    if s_by["mean_gross"] is not None and s_bn["mean_gross"] is not None:
        gross_ratio = s_bn["mean_gross"] / s_by["mean_gross"] if s_by["mean_gross"] else float("nan")
        emit(f"\nSELECTION read: binance mean gross fade = {gross_ratio:.0%} of bybit's.")
        emit("  > ~70%  => the fade EXISTS on Binance (selection transfers; gap is execution/universe).")
        emit("  < ~40%  => the fade itself is weak there (venue-idiosyncratic edge — hypothesis A).")

    # C: universe overlap
    sy_by, sy_bn = set(by["symbol"].to_list()), set(bn["symbol"].to_list())
    inter = sy_by & sy_bn
    emit(f"\nUNIVERSE read: traded-symbol overlap = {len(inter)} "
         f"(bybit-only {len(sy_by - sy_bn)}, binance-only {len(sy_bn - sy_by)})")
    # head-to-head on shared symbols
    g_by = "gross_return" if "gross_return" in by.columns else "gross_trade_return"
    shared_by = by.filter(pl.col("symbol").is_in(sorted(inter)))
    shared_bn = bn.filter(pl.col("symbol").is_in(sorted(inter)))
    if shared_by.height and shared_bn.height:
        emit(f"  shared-symbol mean gross: bybit {float(shared_by[g_by].mean()):+.4%} "
             f"({shared_by.height} tr) vs binance {float(shared_bn[g_by].mean()):+.4%} ({shared_bn.height} tr)")
        emit("  (same names, both venues — isolates microstructure from universe composition)")

    # Funding-income bias band for Binance missing-funding trades
    if "funding_mode" in bn.columns and "funding_return" in by.columns:
        bn_missing = bn.filter(pl.col("funding_mode") == "missing")
        by_modeled = by.filter(pl.col("funding_mode") == "modeled")
        if bn_missing.height and by_modeled.height:
            # proxy: Bybit's per-trade funding_return distribution (same strategy, same
            # event types, similar hold times) applied to Binance's missing trades.
            proxy_mean = float(by_modeled["funding_return"].mean())
            proxy_p25 = float(by_modeled["funding_return"].quantile(0.25))
            proxy_p75 = float(by_modeled["funding_return"].quantile(0.75))
            est = proxy_mean * bn_missing.height
            emit("\nFUNDING-COVERAGE bias (local binance funding covers ~51 symbols):")
            emit(f"  binance trades with funding MISSING: {bn_missing.height}/{bn.height}")
            emit(f"  bybit modeled funding per trade: mean {proxy_mean:+.4%} (IQR {proxy_p25:+.4%}..{proxy_p75:+.4%})")
            emit(f"  => estimated UNBOOKED binance funding P&L ~= {est:+.2%} total "
                 f"(band {proxy_p25 * bn_missing.height:+.2%} .. {proxy_p75 * bn_missing.height:+.2%})")
            emit("  (sign read: a NEGATIVE estimate means missing coverage omitted a real COST —")
            emit("   local Binance results are then OPTIMISTIC; a positive estimate means missed income)")

    out_path = Path(args.out) if args.out else Path(args.binance_trades).expanduser().parent / "binance_gap_decomposition.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
