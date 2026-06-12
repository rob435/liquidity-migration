#!/usr/bin/env python3
"""rmom latency-delay falsification — does the continuous line's rmom edge survive delay?

BROKEN PENDING GIT-RESTORE (2026-06-12): this harness subprocess-invokes
scripts/continuous_causal_rmom_vs_daily.py, which was deleted in the SHORT-sleeve
erasure (e03e9ab) and itself imported the erased volume_events engine — re-running
the open rmom-latency methodology debt (STATE.md) requires restoring BOTH from git
history (e03e9ab^) into a scratch checkout, not this tree.

Closes methodology debt #3 (residual-momentum causality at the decision timestamp), the
site of the codebase's worst historical failure (the ~25h shift1 look-ahead). Receipt:
docs/preregistration/rmom-latency-falsification-2026-06-09.md (read it; the decision
rule is pre-registered there and mirrored in VERDICT_RULE below).

Design: residual_return does not depend on the rmom shift, so residuals are computed
ONCE per root (exactly as scripts/precompute_residual_momentum.py does), then
residual_momentum variants are emitted per shift:

    shift2  — FORBIDDEN diagnostic (leaks ~1h past the D 00:00 decision; quantifies
              how hot the causality boundary is — reported, never deployable)
    shift3  — causal control (the deployed claim)
    shift4/5/7 — +1/+2/+4 days of extra latency

Per shift, the variant parquet is swapped into <root>/residual_momentum.parquet (the
original is backed up and ALWAYS restored), the continuous panel cache self-invalidates
on the parquet's mtime, and the 06-05 harness (scripts/continuous_causal_rmom_vs_daily.py)
runs ONE cell per venue: q25 / liq500k / btcup / turn3_pop3 / age240 / h24 / fixed exit /
inverse_vol — the merged-candidate selection layer minus TP10/crowd2 exit polish (the
harness predates those; the rmom selection under test is identical).

NOT a sweep: the freeze (STATE.md 2026-06-09) exempts causality audits — this test can
only kill the line, not improve it.

    POLARS_MAX_THREADS=6 .venv/bin/python scripts/rmom_latency_falsification.py \
        [--shifts 2,3,4,5,7] [--start 2023-04-01] [--end 2026-05-28]
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.risk_model import build_factor_panel, fit_factor_returns  # noqa: E402
from scripts.precompute_residual_momentum import (  # noqa: E402
    COMMON4,
    RMOM_WINDOW,
    _resolve_klines_dataset,
)

SHARED = Path.home() / "SHARED_DATA"
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
BACKUP_SUFFIX = ".orig_pre_falsification"

# Pre-registered verdict rule (mirror of the receipt — do not edit one without the other):
#   PASS  — shift4 keeps pooled(short_only_mtm_mar) >= 0.5 * shift3's AND no venue's
#           short_only_mtm_total_return flips sign vs shift3.
#   FAIL  — otherwise: the edge is concentrated at the causality boundary; rmom cannot
#           support deployment-grade claims while debt #4 (day-grid alignment) is open.
#   shift2 is reported as boundary-heat context only; it never affects PASS/FAIL.
VERDICT_RULE = "pass iff pooled MAR(shift4) >= 0.5*pooled MAR(shift3) and no venue return sign-flip"


def _rmom_variant(resid: pl.DataFrame, shift: int) -> pl.DataFrame:
    """residual_momentum[D] = sum residual_return[D-(RMOM_WINDOW+shift-1) .. D-shift].
    Identical to precompute_residual_momentum.residual_momentum_expr() but with a
    parameterized shift (that module pins shift=3 as the production value)."""
    return (
        resid.sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("residual_return")
            .rolling_sum(window_size=RMOM_WINDOW, min_samples=4)
            .shift(shift)
            .over("symbol")
            .alias("residual_momentum")
        )
        .select("symbol", "ts_ms", "residual_momentum")
        .drop_nulls("residual_momentum")
    )


def _selection_jaccard(a: pl.DataFrame, b: pl.DataFrame, quantile: float) -> float | None:
    """Mean per-day Jaccard of the bottom-`quantile` rmom selection sets (the engine's
    rank-fraction gate) between two rmom variants. The membership-stability read."""

    def _sel(df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(
            (
                (pl.col("residual_momentum").rank(method="ordinal").over("ts_ms") - 1)
                / (pl.col("residual_momentum").count().over("ts_ms") - 1).clip(lower_bound=1)
            ).alias("_rr")
        ).filter(pl.col("_rr") <= quantile).select("symbol", "ts_ms")

    sa, sb = _sel(a), _sel(b)
    joined = (
        sa.with_columns(pl.lit(True).alias("in_a"))
        .join(sb.with_columns(pl.lit(True).alias("in_b")), on=["symbol", "ts_ms"], how="full", coalesce=True)
        .with_columns(pl.col("in_a").fill_null(False), pl.col("in_b").fill_null(False))
    )
    per_ts = (
        joined.group_by("ts_ms")
        .agg(
            (pl.col("in_a") & pl.col("in_b")).sum().alias("both"),
            (pl.col("in_a") | pl.col("in_b")).sum().alias("union"),
        )
        .filter(pl.col("union") > 0)
        .with_columns((pl.col("both") / pl.col("union")).alias("jaccard"))
    )
    return float(per_ts["jaccard"].mean()) if per_ts.height else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shifts", default="2,3,4,5,7")
    ap.add_argument("--start", default="2023-04-01", help="backtest window start (harness)")
    ap.add_argument("--end", default="2026-05-28", help="backtest window end, exclusive (harness)")
    ap.add_argument("--panel-start", default="2023-03-01", help="residual panel start (warmup pad)")
    ap.add_argument("--panel-end", default="2026-06-01", help="residual panel end, exclusive")
    ap.add_argument("--quantile", type=float, default=0.25)
    ap.add_argument("--out", default=str(SHARED / "rmom_latency_falsification_2026-06-09"))
    ap.add_argument("--venues", default="bybit,binance")
    args = ap.parse_args()

    shifts = [int(s) for s in args.shifts.split(",") if s.strip()]
    if 3 not in shifts:
        raise SystemExit("shift grid must include the causal control shift=3")
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    # Refuse to start over a stale crashed state: a leftover backup means the root's
    # residual_momentum.parquet may be a shift-variant, not the production table.
    for venue in venues:
        bak = ROOTS[venue] / ("residual_momentum.parquet" + BACKUP_SUFFIX)
        if bak.exists():
            raise SystemExit(
                f"{bak} exists — a previous run did not restore. Restore it manually "
                f"(mv back over residual_momentum.parquet) before running."
            )

    # 1) residuals ONCE per root (shift-independent)
    resid_by_venue: dict[str, pl.DataFrame] = {}
    for venue in venues:
        root = ROOTS[venue]
        kname = _resolve_klines_dataset(root, None)
        print(f"[{venue}] factor panel + {COMMON4} residuals [{args.panel_start}..{args.panel_end}) ...", flush=True)
        panel = build_factor_panel(root, start=args.panel_start, end=args.panel_end, klines_dataset=kname)
        if panel.is_empty():
            raise SystemExit(f"[{venue}] EMPTY factor panel — cannot run falsification")
        _fr, resid = fit_factor_returns(panel, factor_cols=COMMON4)
        resid_by_venue[venue] = resid.sort(["symbol", "ts_ms"]).select("symbol", "ts_ms", "residual_return")
        print(f"[{venue}] residual rows: {resid_by_venue[venue].height:,}", flush=True)

    # sanity: our shift3 should agree with the production table in-window
    rmom_by_shift: dict[int, dict[str, pl.DataFrame]] = {s: {} for s in shifts}
    for venue in venues:
        for s in shifts:
            rmom_by_shift[s][venue] = _rmom_variant(resid_by_venue[venue], s)
        prod = pl.read_parquet(ROOTS[venue] / "residual_momentum.parquet")
        ours = rmom_by_shift[3][venue]
        j = (
            prod.join(ours, on=["symbol", "ts_ms"], how="inner", suffix="_ours")
            .with_columns((pl.col("residual_momentum") - pl.col("residual_momentum_ours")).abs().alias("_d"))
        )
        match = float((j["_d"] < 1e-12).sum() / j.height) if j.height else 0.0
        print(f"[{venue}] shift3 vs production table: {j.height:,} joined rows, exact-match {match:.1%}", flush=True)

    # 2) per shift: swap parquets in, run the harness, restore at the very end
    backups: dict[str, Path] = {}
    try:
        for venue in venues:
            src = ROOTS[venue] / "residual_momentum.parquet"
            bak = src.with_name(src.name + BACKUP_SUFFIX)
            src.rename(bak)
            backups[venue] = bak

        for s in shifts:
            for venue in venues:
                rmom_by_shift[s][venue].write_parquet(ROOTS[venue] / "residual_momentum.parquet")
            shift_out = out_root / f"shift{s}"
            cmd = [
                str(REPO / ".venv" / "bin" / "python"),
                str(REPO / "scripts" / "continuous_causal_rmom_vs_daily.py"),
                "--start", args.start, "--end", args.end,
                "--out", str(shift_out),
                "--rmom-quantiles", str(args.quantile),
                "--liq-turnover-mins", "500000",
                "--hold-hours", "24",
                "--exit-modes", "fixed",
                "--btc-trend-gates", "uptrend",
                "--entry-event-triggers", "turn3_pop3",
                "--age-days-min", "240",
                "--sizing-mode", "inverse_vol",
                "--skip-daily",
                "--venues", ",".join(venues),
                "--pre-registration", "docs/preregistration/rmom-latency-falsification-2026-06-09.md",
            ]
            print(f"\n=== shift{s}: {' '.join(cmd)}", flush=True)
            subprocess.run(cmd, check=True, cwd=REPO)
    finally:
        for venue, bak in backups.items():
            dst = ROOTS[venue] / "residual_momentum.parquet"
            if bak.exists():
                if dst.exists():
                    dst.unlink()
                bak.rename(dst)
                print(f"[{venue}] restored production residual_momentum.parquet", flush=True)

    # 3) aggregate + verdict
    rows: list[dict] = []
    for s in shifts:
        summary = out_root / f"shift{s}" / "summary.csv"
        if not summary.exists():
            print(f"!! shift{s}: missing {summary}")
            continue
        with open(summary) as f:
            for r in csv.DictReader(f):
                rows.append({
                    "shift": s,
                    "venue": r.get("venue"),
                    "short_trades": r.get("short_only_trades"),
                    "short_return": r.get("short_only_mtm_total_return"),
                    "short_mar": r.get("short_only_mtm_mar"),
                    "short_max_dd": r.get("short_only_mtm_max_drawdown"),
                    "jaccard_vs_shift3": _selection_jaccard(
                        rmom_by_shift[3][r["venue"]], rmom_by_shift[s][r["venue"]], args.quantile
                    ) if r.get("venue") in rmom_by_shift[s] else None,
                })
    agg_path = out_root / "falsification_summary.csv"
    if rows:
        with open(agg_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    def _pooled_mar(s: int) -> float | None:
        vals = [float(r["short_mar"]) for r in rows if r["shift"] == s and r["short_mar"] not in (None, "")]
        return sum(vals) / len(vals) if vals else None

    def _returns(s: int) -> dict[str, float]:
        return {
            r["venue"]: float(r["short_return"])
            for r in rows
            if r["shift"] == s and r["short_return"] not in (None, "")
        }

    m3, m4 = _pooled_mar(3), _pooled_mar(4)
    r3, r4 = _returns(3), _returns(4)
    sign_flip = any(v in r4 and r3[v] * r4[v] < 0 for v in r3)
    verdict = None
    if m3 is not None and m4 is not None:
        verdict = "PASS" if (m4 >= 0.5 * m3 and not sign_flip) else "FAIL"
    (out_root / "verdict.json").write_text(json.dumps({
        "rule": VERDICT_RULE,
        "pooled_mar_shift3": m3,
        "pooled_mar_shift4": m4,
        "returns_shift3": r3,
        "returns_shift4": r4,
        "venue_sign_flip_shift4": sign_flip,
        "verdict": verdict,
        "rows": rows,
    }, indent=2), encoding="utf-8")

    print(f"\n=== rmom latency falsification ===\n{'shift':>6} {'venue':>8} {'trades':>7} "
          f"{'return':>9} {'MAR':>7} {'maxDD':>8} {'J(vs s3)':>9}")
    for r in rows:
        jac = f"{r['jaccard_vs_shift3']:.3f}" if r["jaccard_vs_shift3"] is not None else "—"
        print(f"{r['shift']:>6} {r['venue']:>8} {r['short_trades']:>7} {r['short_return']:>9} "
              f"{r['short_mar']:>7} {r['short_max_dd']:>8} {jac:>9}")
    print(f"\nrule    : {VERDICT_RULE}")
    print(f"verdict : {verdict}   (summary: {agg_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
