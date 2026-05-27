"""Aggregate analysis of sweep_2026-05-28_summary.csv.

Applies the pre-registered decision rule:
  - Sharpe Δ ≥ +0.5 vs baseline on BOTH venues
  - Max DD Δ ≤ +5pp on BOTH venues
  - Sign(return) ≥ 0 on both venues
  - Trade count ≥ 30 on Bybit
"""
from __future__ import annotations

from pathlib import Path

import polars as pl


SUMMARY = Path.home() / "SHARED_DATA/sweep_2026-05-28_summary.csv"


def main() -> int:
    if not SUMMARY.exists():
        print(f"NO SUMMARY at {SUMMARY}; run scripts/sweep_cells.py first")
        return 1
    df = pl.read_csv(SUMMARY, infer_schema_length=None)
    # Cast numeric columns
    for col in ("trades", "total_return", "max_drawdown", "avg_split_sharpe", "sharpe_like", "worst_90d"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

    print(f"=== sweep summary: {df.height} rows ===\n")

    # Per-venue pivot
    for venue in ("bybit", "binance"):
        sub = df.filter(pl.col("venue") == venue).sort("cell_id")
        if sub.is_empty():
            print(f"--- {venue.upper()}: (no rows) ---\n")
            continue
        print(f"--- {venue.upper()} ({sub.height} cells) ---")
        print(f"  {'cell':40s}  {'trades':>7}  {'return':>10}  {'max_dd':>9}  {'sharpe':>7}  desc")
        for r in sub.to_dicts():
            print(
                f"  {r['cell_id']:40s}  "
                f"{int(r.get('trades') or 0):>7}  "
                f"{(r.get('total_return') or 0.0)*100:>+9.1f}%  "
                f"{(r.get('max_drawdown') or 0.0)*100:>+8.1f}%  "
                f"{(r.get('sharpe_like') or 0.0):>7.2f}  "
                f"{r.get('description', '')}"
            )
        print()

    # Decision rule
    print("=== Decision rule: candidate improvements (Sharpe Δ ≥ +0.5 on BOTH, DD Δ ≤ +5pp on both, sign ≥ 0 on both, Bybit trades ≥ 30) ===\n")
    pivot = df.pivot(
        index=["cell_id", "description"],
        on="venue",
        values=["sharpe_like", "max_drawdown", "total_return", "trades"],
    )
    baseline = pivot.filter(pl.col("cell_id") == "00_baseline")
    if baseline.is_empty():
        print("no baseline row — aborting decision-rule pass")
        return 2
    bl = baseline.row(0, named=True)
    bl_sharpe_bybit = float(bl.get("sharpe_like_bybit") or 0.0)
    bl_sharpe_binance = float(bl.get("sharpe_like_binance") or 0.0)
    bl_dd_bybit = float(bl.get("max_drawdown_bybit") or 0.0)
    bl_dd_binance = float(bl.get("max_drawdown_binance") or 0.0)
    bl_ret_bybit = float(bl.get("total_return_bybit") or 0.0)
    bl_ret_binance = float(bl.get("total_return_binance") or 0.0)

    print(f"baseline: Bybit Sharpe {bl_sharpe_bybit:.2f}  DD {bl_dd_bybit*100:.1f}%  ret {bl_ret_bybit*100:+.1f}%")
    print(f"          Binance Sharpe {bl_sharpe_binance:.2f}  DD {bl_dd_binance*100:.1f}%  ret {bl_ret_binance*100:+.1f}%\n")

    candidates: list[dict[str, object]] = []
    print(f"  {'cell':40s}  ΔSharpe(by, bi)  ΔDD(by, bi)  ret(by, bi)  trades(by, bi)  verdict")
    for r in pivot.iter_rows(named=True):
        if r["cell_id"] == "00_baseline":
            continue
        sb = float(r.get("sharpe_like_bybit") or 0.0)
        si = float(r.get("sharpe_like_binance") or 0.0)
        ddb = float(r.get("max_drawdown_bybit") or 0.0)
        ddi = float(r.get("max_drawdown_binance") or 0.0)
        rb = float(r.get("total_return_bybit") or 0.0)
        ri = float(r.get("total_return_binance") or 0.0)
        tb = int(r.get("trades_bybit") or 0)
        ti = int(r.get("trades_binance") or 0)
        d_sharpe_bybit = sb - bl_sharpe_bybit
        d_sharpe_binance = si - bl_sharpe_binance
        # DD is negative; "increase" means more negative; +5pp ≤ baseline means new DD - baseline DD ≥ -0.05
        d_dd_bybit = ddb - bl_dd_bybit
        d_dd_binance = ddi - bl_dd_binance
        sharpe_pass = d_sharpe_bybit >= 0.5 and d_sharpe_binance >= 0.5
        dd_pass = d_dd_bybit >= -0.05 and d_dd_binance >= -0.05
        sign_pass = rb >= 0 and ri >= 0
        trade_pass = tb >= 30
        verdict_parts = []
        if sharpe_pass:
            verdict_parts.append("ΔS✓")
        else:
            verdict_parts.append("ΔS✗")
        if dd_pass:
            verdict_parts.append("ΔD✓")
        else:
            verdict_parts.append("ΔD✗")
        if sign_pass:
            verdict_parts.append("sign✓")
        else:
            verdict_parts.append("sign✗")
        if trade_pass:
            verdict_parts.append("n≥30✓")
        else:
            verdict_parts.append("n<30✗")
        verdict = " ".join(verdict_parts)
        passing = sharpe_pass and dd_pass and sign_pass and trade_pass
        marker = "🎯 CANDIDATE" if passing else "  "
        print(
            f"  {r['cell_id']:40s}  "
            f"{d_sharpe_bybit:+.2f}/{d_sharpe_binance:+.2f}  "
            f"{d_dd_bybit*100:+.1f}/{d_dd_binance*100:+.1f}pp  "
            f"{rb*100:+.0f}/{ri*100:+.0f}%  "
            f"{tb}/{ti}  "
            f"{verdict}  {marker}"
        )
        if passing:
            candidates.append({
                "cell_id": r["cell_id"], "description": r.get("description"),
                "d_sharpe_bybit": d_sharpe_bybit, "d_sharpe_binance": d_sharpe_binance,
                "d_dd_bybit": d_dd_bybit, "d_dd_binance": d_dd_binance,
            })

    print()
    if candidates:
        print(f"=== {len(candidates)} CANDIDATE CELL(S) PASS PRE-REG DECISION RULE ===")
        for c in candidates:
            print(f"  {c['cell_id']}: ΔSharpe Bybit {c['d_sharpe_bybit']:+.2f}, Binance {c['d_sharpe_binance']:+.2f}")
    else:
        print("=== NO CELLS PASS PRE-REG DECISION RULE — NULL RESULT ===")
        print("    Per pre-reg honesty rules, the current production parameters stand.")
    return 0


if __name__ == "__main__":
    main()
