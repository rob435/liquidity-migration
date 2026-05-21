"""Cross-sectional predictiveness test for the signed-flow imbalance feature.

Builds a daily panel of taker-imbalance features from signed_flow_1h and measures
their per-day cross-sectional Spearman IC against forward SHORT returns, using the
repo's own audited methodology (ic_diagnostic.cross_sectional_ic and
add_forward_short_returns). It does NOT pool observations across days.

Causal alignment: a feature aggregated over calendar day X is known at X's close,
which is midnight of X+1. reversion_alpha/ic_diagnostic stamp a panel row's `date`
at the signal-close moment and enter +1h after it, so the panel `date` is X+1.
The feature therefore uses only day-X trades and the entry is 1h after day X ends.

A shuffled `random_control` feature is included; its IC must sit near zero.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.ic_diagnostic import add_forward_short_returns, cross_sectional_ic

FEATURES = ("taker_imbalance_1d", "taker_imbalance_3d", "random_control")
HORIZONS = (1, 3, 5)


def _load_parquets(root: Path, dataset: str, start: str, end: str) -> pl.DataFrame:
    files: list[str] = []
    base = root / dataset
    for date_dir in sorted(base.glob("date=*")):
        day = date_dir.name.split("=", 1)[1]
        if start <= day <= end:
            files += glob.glob(str(date_dir) + "/**/*.parquet", recursive=True)
    if not files:
        return pl.DataFrame()
    return pl.read_parquet(files)


def build_panel(flow: pl.DataFrame, *, min_hours: int, seed: int) -> pl.DataFrame:
    """Daily taker-imbalance panel, dated at the signal close (feature day + 1)."""
    daily = (
        flow.group_by(["symbol", "date"])
        .agg(
            signed_quote_1d=pl.col("signed_quote").sum(),
            total_quote_1d=pl.col("total_quote").sum(),
            hours=pl.len(),
        )
        .filter((pl.col("hours") >= min_hours) & (pl.col("total_quote_1d") > 0))
        .sort(["symbol", "date"])
        .with_columns(taker_imbalance_1d=pl.col("signed_quote_1d") / pl.col("total_quote_1d"))
        .with_columns(
            sq3=pl.col("signed_quote_1d").rolling_sum(3, min_periods=3).over("symbol"),
            tq3=pl.col("total_quote_1d").rolling_sum(3, min_periods=3).over("symbol"),
        )
        .with_columns(taker_imbalance_3d=pl.col("sq3") / pl.col("tq3"))
    )
    rng = np.random.default_rng(seed)
    return daily.with_columns(
        pl.Series("random_control", rng.standard_normal(daily.height)),
        date=(pl.col("date").str.to_date() + pl.duration(days=1)).dt.strftime("%Y-%m-%d"),
    ).select(["date", "symbol", *FEATURES])


def ic_table(panel: pl.DataFrame, *, min_names: int, label: str) -> list[dict]:
    rows = []
    for feat in FEATURES:
        for h in HORIZONS:
            res = cross_sectional_ic(panel, feat, f"fwd_short_return_{h}d", min_names=min_names)
            rows.append({"window": label, **res.as_row()})
    return rows


def verdict_lines(rows: list[dict]) -> list[str]:
    """Decide, per real feature, whether the IC evidence clears the bar.

    A feature is `predictive` only if some horizon has full-window |IC| >= 0.03,
    |t| >= 2.0, the same sign in both the early and late halves, and |IC| at
    least 2x the shuffled control's |IC| at that horizon.
    """
    idx = {(r["window"], r["signal"], r["forward"]): r for r in rows}
    windows = sorted({r["window"] for r in rows})
    full = next(w for w in windows if w == "full")
    early = next(w for w in windows if w.startswith("early"))
    late = next(w for w in windows if w.startswith("late"))
    lines = ["## Verdict", ""]
    any_pred = False
    for feat in ("taker_imbalance_1d", "taker_imbalance_3d"):
        best = None
        for h in HORIZONS:
            fwd = f"fwd_short_return_{h}d"
            f_row = idx[(full, feat, fwd)]
            e_ic = idx[(early, feat, fwd)]["mean_ic"]
            l_ic = idx[(late, feat, fwd)]["mean_ic"]
            c_ic = abs(idx[(full, "random_control", fwd)]["mean_ic"])
            sign_stable = (e_ic > 0) == (l_ic > 0) == (f_row["mean_ic"] > 0)
            ok = (
                abs(f_row["mean_ic"]) >= 0.03
                and abs(f_row["t_stat"]) >= 2.0
                and sign_stable
                and abs(f_row["mean_ic"]) >= 2.0 * max(c_ic, 1e-9)
            )
            cand = (abs(f_row["t_stat"]), h, f_row, e_ic, l_ic, c_ic, sign_stable, ok)
            if best is None or cand[0] > best[0]:
                best = cand
        _, h, f_row, e_ic, l_ic, c_ic, sign_stable, ok = best
        any_pred = any_pred or ok
        lines.append(
            f"- `{feat}`: **{'PREDICTIVE' if ok else 'not predictive'}** — "
            f"best horizon {h}d: full IC {f_row['mean_ic']:+.4f} (t {f_row['t_stat']:+.2f}), "
            f"early {e_ic:+.4f} / late {l_ic:+.4f} "
            f"({'sign stable' if sign_stable else 'SIGN FLIPS'}), "
            f"control |IC| {c_ic:.4f}."
        )
    lines += [
        "",
        f"**Overall: signed-flow taker imbalance is "
        f"{'PREDICTIVE' if any_pred else 'NOT predictive'} on this exploratory panel.** "
        + (
            ""
            if any_pred
            else "Full-window IC sits inside the shuffled-control noise band and the "
            "sign is not stable across the early/late split — no robust standalone "
            "cross-sectional edge at the daily/multi-day horizon."
        ),
        "",
    ]
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="~/SHARED_DATA/bybit_fullpit_1h")
    ap.add_argument("--start", required=True, help="Inclusive feature-day start YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="Inclusive feature-day end YYYY-MM-DD")
    ap.add_argument("--min-hours", type=int, default=20, help="Min traded hours for a valid daily feature")
    ap.add_argument("--min-names", type=int, default=20, help="Min names per day for a scored cross-section")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--output", default=None, help="Markdown report path")
    args = ap.parse_args()

    root = Path(args.data_root).expanduser()
    flow = _load_parquets(root, "signed_flow_1h", args.start, args.end)
    if flow.is_empty():
        print(f"no signed_flow_1h rows in {args.start}..{args.end}", flush=True)
        return 1
    panel = build_panel(flow, min_hours=args.min_hours, seed=args.seed)

    panel_start = panel["date"].min()
    panel_end = panel["date"].max()
    klines = _load_parquets(root, "klines_1h", args.start, "2099-01-01").select(
        ["symbol", "ts_ms", "open", "close"]
    )
    panel = add_forward_short_returns(panel, klines, HORIZONS, entry_delay_hours=1)

    dates = sorted(panel["date"].unique().to_list())
    mid = dates[len(dates) // 2]
    full = ic_table(panel, min_names=args.min_names, label="full")
    early = ic_table(panel.filter(pl.col("date") < mid), min_names=args.min_names, label=f"early_<{mid}")
    late = ic_table(panel.filter(pl.col("date") >= mid), min_names=args.min_names, label=f"late_>={mid}")
    all_rows = full + early + late
    table = pl.DataFrame(all_rows)

    flow_days = flow["date"].n_unique()
    flow_syms = flow["symbol"].n_unique()
    lines = [
        "# Signed-Flow Predictiveness — Cross-Sectional IC",
        "",
        "**Run label: `exploratory`** — recent-window panel only (not full PIT history,",
        "no 2023/2024 train splits); causal feature alignment; klines forward returns;",
        "negative control included. Sufficient to accept/kill the hypothesis, not to promote.",
        "",
        f"- signed_flow_1h source: feature days `{args.start}`..`{args.end}` "
        f"({flow_days} days, {flow_syms} symbols)",
        f"- Panel (signal-close dated): `{panel_start}`..`{panel_end}`",
        f"- Forward return: short return, entry +1h after signal close, horizons {HORIZONS}d",
        f"- min traded hours/day: {args.min_hours}; min names/cross-section: {args.min_names}",
        "- IC sign: positive => high taker-buy imbalance precedes price falling (reversion;",
        "  favourable for a short). Negative => buying precedes further upside (momentum).",
        "",
        "| Window | Signal | Forward | Days | Obs | Mean IC | IC Std | t-stat | Hit Rate |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in all_rows:
        lines.append(
            f"| {r['window']} | `{r['signal']}` | {r['forward']} | {r['n_days']} | {r['n_obs']} | "
            f"{r['mean_ic']:+.4f} | {r['ic_std']:.4f} | {r['t_stat']:+.2f} | {r['hit_rate']:.3f} |"
        )
    lines += ["", *verdict_lines(all_rows)]
    lines += [
        "## Reading this",
        "",
        "- A feature is predictive only if mean IC is non-trivial (|IC| >~ 0.02-0.03),",
        "  |t-stat| is large (>~2-3), the sign is stable across early/late windows, and",
        "  it clearly beats `random_control`.",
        "- `random_control` is a shuffled feature: its IC band shows the no-signal noise floor.",
        "",
    ]
    report = "\n".join(lines)
    out = Path(args.output).expanduser() if args.output else (
        root / "reports" / "signed_flow_predictiveness" / "signed_flow_ic_report.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    table.write_csv(out.with_name("signed_flow_ic.csv"))
    print(report, flush=True)
    print(f"\nreport: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
