#!/usr/bin/env python3
"""T-J: deployed-book conditioning search (V5 iteration cycle, Lane 1).

Owner-directed follow-up to the V4 program ("iterate and be creative to find
something that works").  Three candidate mechanisms are generated and judged
on the deployed-shape T-A render books with the controls the repository's
prior sizing closure demanded (era split, per-component consistency,
label-permutation controls, tail arm, concentration/trim robustness):

1. exit-geometry hypothesis — why is deep-negative funding the render books'
   best bucket but the barebones book's worst?  Anatomy tables decide whether
   the difference is exit shape or entry selection.
2. gate-override decomposition — the gate-off-minus-gate-on blocked mass,
   bucketed by freshness and funding state, cross-checked on the barebones
   ledger (fresh x BTC-trend x era) with the deployed gate's own trend value.
3. freshness sizing tilt — budget-neutral at-high upweight per book, judged
   against N seeded label permutations (a sizing rule must beat hash-style
   controls, not just baseline).

Everything is exploratory post-processing of already-rendered/cached
surfaces; no engine or runtime change.  The one surviving lead (blocked
at-high entries, newest render era only) is frozen as a forward-ledger
prototype rule spec in the output directory — its evidence is the run of
post-commit forward days only.  No alpha or promotion claim.

Run through the POSIX shim (imports the deployed trend function):
  .venv\\Scripts\\python.exe scripts/research_v3/run_with_stub.py \\
      scripts/research_v3/tj_deployed_conditioning.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.research_v3 import common, v4_shared  # noqa: E402
from scripts.research_v3 import tc_pump_deceleration as tc  # noqa: E402
from scripts.research_v3.ta_gate_ablation_report import NAMED_TAIL_DATE, common_loss_dates  # noqa: E402
from scripts.research_v3.te_fresh_high import render_arm_features  # noqa: E402

TILT_MULTS: tuple[float, ...] = (1.25, 1.5)
N_PERMUTATIONS = 500
PERMUTATION_SEED = 20260720
BTC_KLINE_START = dt.date(2021, 3, 1)
FRESH_OVERRIDE_MAX_HOURS = 1.0


def blocked_set(books: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Gate-off entries absent from the same component's gate-on book."""
    frames = []
    for comp in v4_shared.RENDER_COMPONENTS:
        on_ids = set(books["gate_on"].filter(pl.col("component") == comp)["trade_id"].to_list())
        off = books["gate_off"].filter(pl.col("component") == comp)
        frames.append(off.filter(~pl.col("trade_id").is_in(sorted(on_ids))))
    return pl.concat(frames, how="vertical")


def anatomy(frame: pl.DataFrame, label: str) -> pl.DataFrame:
    return (
        frame.group_by("exit_reason")
        .agg(
            pl.len().alias("n"),
            pl.col("hold_hours").mean().alias("mean_hold_hours"),
            (100.0 * pl.col("gross_return").sum()).alias("gross_pct"),
            (100.0 * pl.col("funding_return").sum()).alias("funding_pct"),
            (100.0 * pl.col("net_return").sum()).alias("net_pct"),
            pl.col("mfe").mean().alias("mean_mfe"),
        )
        .with_columns(pl.lit(label).alias("book"))
        .sort("exit_reason")
    )


def tilt_rows(
    name: str, frame: pl.DataFrame, midpoint: int, tail_dates: set[str], rng: np.random.Generator
) -> list[dict[str, Any]]:
    mask = (frame["fresh_bucket"] == "at_high_le1h").to_numpy()
    w = frame["notional_weight"].to_numpy()
    net = frame["net_return"].to_numpy()
    late = frame["entry_ts_ms"].to_numpy() >= midpoint
    tail_mask = (
        frame["entry_date"].is_in(sorted(tail_dates)) | frame["exit_date"].is_in(sorted(tail_dates))
    ).to_numpy()

    def delta_for(m: np.ndarray, mult: float) -> tuple[float, float]:
        w_ah = w[m].sum()
        w_rest = w[~m].sum()
        c = (w.sum() - mult * w_ah) / w_rest if w_rest > 0 else 1.0
        return float((mult - 1.0) * net[m].sum() + (c - 1.0) * net[~m].sum()), c

    rows = []
    for mult in TILT_MULTS:
        delta, c = delta_for(mask, mult)
        w_delta = np.where(mask, mult - 1.0, c - 1.0)
        perm = np.empty(N_PERMUTATIONS)
        n_ah = int(mask.sum())
        for i in range(N_PERMUTATIONS):
            pm = np.zeros(len(mask), dtype=bool)
            pm[rng.choice(len(mask), size=n_ah, replace=False)] = True
            perm[i], _ = delta_for(pm, mult)
        rows.append(
            {
                "book": name,
                "mult": mult,
                "c_rest": c,
                "delta_net_pct": 100.0 * delta,
                "delta_early_pct": 100.0 * float((w_delta * net)[~late].sum()),
                "delta_late_pct": 100.0 * float((w_delta * net)[late].sum()),
                "delta_tail_pct": 100.0 * float((w_delta * net)[tail_mask].sum()),
                "at_high_notional_share": float(w[mask].sum() / w.sum()),
                "perm_percentile": float((perm < delta).mean()),
                "perm_mean_pct": 100.0 * float(perm.mean()),
                "perm_p95_pct": 100.0 * float(np.quantile(perm, 0.95)),
            }
        )
    return rows


def main() -> int:
    from liquidity_migration.continuous_events import _btc_trend_returns

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = common.REPORT_ROOT / "t-j" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)
    v2_identity = common.verify_v2_inputs()
    rng = np.random.default_rng(PERMUTATION_SEED)
    tail_dates = set(common_loss_dates()) | {NAMED_TAIL_DATE}

    # Books with freshness + funding features.
    render_klines, render_kline_sha = v4_shared.render_kline_cache(args.data_root)
    render_funding, render_funding_sha = v4_shared.render_funding_cache(args.data_root)
    render_series = common.funding_series_by_symbol(render_funding)
    books: dict[str, pl.DataFrame] = {}
    for arm in ("gate_on", "gate_off"):
        book = v4_shared.load_render_book(arm)
        rates = [
            v4_shared.known_prev_rate(str(t["symbol"]), int(t["entry_ts_ms"]), render_series)
            for t in book.iter_rows(named=True)
        ]
        book = book.with_columns(pl.Series("known_rate_prev", rates, dtype=pl.Float64)).with_columns(
            v4_shared.funding_bucket_expr()
        )
        book, _statuses = render_arm_features(book, render_klines)
        books[arm] = book

    ledger = common.load_ledger("continuous")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    series = common.funding_series_by_symbol(funding)
    panel = tc.compute_features(ledger, tc.ohlc_series_by_symbol(klines)).with_columns(
        v4_shared.freshness_bucket_expr()
    )
    known = [
        v4_shared.known_prev_rate(str(t["symbol"]), int(t["entry_ts_ms"]), series)
        for t in panel.iter_rows(named=True)
    ]
    panel = panel.with_columns(pl.Series("known_rate_prev", known, dtype=pl.Float64)).with_columns(
        v4_shared.funding_bucket_expr()
    )
    ledger_mid = common.era_midpoint_ts_ms(ledger)

    # 1. Deep-neg anatomy: exit-shape hypothesis test.
    anatomy_table = pl.concat(
        [
            anatomy(panel.filter(pl.col("fund_bucket") == "deep_neg"), "barebones_deep_neg"),
            anatomy(books["gate_on"].filter(pl.col("fund_bucket") == "deep_neg"), "gate_on_deep_neg"),
            anatomy(books["gate_off"].filter(pl.col("fund_bucket") == "deep_neg"), "gate_off_deep_neg"),
        ],
        how="vertical",
    )
    tp_distance = {
        arm: float(
            books[arm]
            .select(((pl.col("entry_price") - pl.col("take_profit_price")) / pl.col("entry_price")).mean())
            .item()
        )
        for arm in books
    }
    anatomy_path = out_dir / "tj_deepneg_anatomy.csv"
    anatomy_table.write_csv(anatomy_path)

    # 2. Blocked-set decomposition + barebones cross-check.
    blocked = blocked_set(books)
    render_mid = v4_shared.render_era_midpoint_ms(books["gate_off"])
    blocked_tables = []
    for col in ("fund_bucket", "fresh_bucket"):
        blocked_tables.append(
            v4_shared.render_bucket_table(blocked, col, render_mid).rename({col: "bucket"}).with_columns(
                pl.lit(col).alias("axis")
            )
        )
    blocked_path = out_dir / "tj_blocked_decomposition.csv"
    pl.concat(blocked_tables, how="vertical").write_csv(blocked_path)

    btc_klines = common.read_kline_slice(
        args.data_root, start=BTC_KLINE_START, end_exclusive=common.KLINE_END_EXCLUSIVE, symbols={"BTCUSDT"}
    )
    trend_lookup = _btc_trend_returns(btc_klines, lookback_days=30)
    trends = [
        trend_lookup.get(common.utc_day_ms(int(t))) for t in panel["entry_signal_ts_ms"].to_list()
    ]
    cross = panel.with_columns(pl.Series("btc_trend_30d", trends, dtype=pl.Float64)).with_columns(
        (pl.col("entry_ts_ms") >= ledger_mid).alias("late"),
        (pl.col("btc_trend_30d") <= 0.0).alias("downtrend"),
    )
    cross_table = (
        cross.group_by("fresh_bucket", "downtrend", "late")
        .agg(
            pl.len().alias("n"),
            (100.0 * pl.col("net_return").sum()).alias("net_pct"),
            (10_000.0 * pl.col("net_return").mean()).alias("mean_net_bps"),
            (pl.col("exit_reason") == "take_profit").mean().alias("tp_rate"),
        )
        .sort("fresh_bucket", "downtrend", "late")
    )
    cross_path = out_dir / "tj_barebones_crosscheck.csv"
    cross_table.write_csv(cross_path)
    deepneg_cross = (
        cross.filter(pl.col("fund_bucket") == "deep_neg")
        .group_by("downtrend", "late")
        .agg(pl.len().alias("n"), (100.0 * pl.col("net_return").sum()).alias("net_pct"))
        .sort("downtrend", "late")
    )

    # 3. Freshness sizing tilt with permutation controls.
    tilt_out: list[dict[str, Any]] = []
    for arm in ("gate_on", "gate_off"):
        mid = v4_shared.render_era_midpoint_ms(books[arm])
        tilt_out.extend(tilt_rows(arm, books[arm], mid, tail_dates, rng))
        for comp in v4_shared.RENDER_COMPONENTS:
            part = books[arm].filter(pl.col("component") == comp)
            tilt_out.extend(tilt_rows(f"{arm}/{comp}", part, mid, tail_dates, rng))
    tilt_out.extend(tilt_rows("barebones", panel, ledger_mid, tail_dates, rng))
    tilt_path = out_dir / "tj_tilt_controls.csv"
    pl.from_dicts(tilt_out, infer_schema_length=None).write_csv(tilt_path)

    # 4. Lead robustness: blocked at-high, newest era.
    bah = blocked.filter(pl.col("fresh_bucket") == "at_high_le1h")
    bah_late = bah.filter(pl.col("entry_ts_ms") >= render_mid)
    by_month = (
        bah_late.group_by("exit_month")
        .agg(pl.len().alias("n"), (100.0 * pl.col("net_return").sum()).alias("net_pct"))
        .sort("exit_month")
    )
    month_path = out_dir / "tj_lead_by_month.csv"
    by_month.write_csv(month_path)
    by_symbol = (
        bah_late.group_by("symbol")
        .agg(pl.len().alias("n"), (100.0 * pl.col("net_return").sum()).alias("net_pct"))
        .sort("net_pct", descending=True)
    )
    tail_trades = bah.filter(
        pl.col("entry_date").is_in(sorted(tail_dates)) | pl.col("exit_date").is_in(sorted(tail_dates))
    )
    per_component_late = {
        comp: 100.0
        * float(bah_late.filter(pl.col("component") == comp)["net_return"].sum())
        for comp in v4_shared.RENDER_COMPONENTS
    }
    lead_robustness = {
        "blocked_at_high_full": {"n": bah.height, "net_pct": 100.0 * float(bah["net_return"].sum())},
        "late_era": {
            "n": bah_late.height,
            "net_pct": 100.0 * float(bah_late["net_return"].sum()),
            "unique_signals": bah_late.unique("trade_id").height,
            "unique_net_pct": 100.0 * float(bah_late.unique("trade_id")["net_return"].sum()),
            "win_rate": float((bah_late["net_return"] > 0).mean()),
            "symbols": by_symbol.height,
            "top3_symbol_net_pct": float(by_symbol.head(3)["net_pct"].sum()),
            "net_pct_after_dropping_top5_trades": 100.0
            * float(bah_late.sort("net_return", descending=True)[5:]["net_return"].sum()),
            "positive_months": by_month.filter(pl.col("net_pct") > 0).height,
            "months": by_month.height,
            "per_component_net_pct": per_component_late,
        },
        "tail_arm_full": {
            "trades_touching_tail_days": tail_trades.height,
            "net_pct": 100.0 * float(tail_trades["net_return"].sum()),
            "negative_trades": tail_trades.filter(pl.col("net_return") < 0).height,
            "negative_net_pct": 100.0
            * float(tail_trades.filter(pl.col("net_return") < 0)["net_return"].sum()),
        },
        "deep_neg_barebones_crosscheck": deepneg_cross.to_dicts(),
    }
    robustness_path = out_dir / "tj_lead_robustness.json"
    robustness_path.write_text(json.dumps(lead_robustness, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # 5. Frozen prototype rule spec (registration = the commit containing it).
    prototype = {
        "prototype": "continuous_fresh_high_gate_override",
        "status": "forward-ledger prototype (Lane 2); newest-render-era lead only; no promotion",
        "rule": {
            "base": "deployed CONTINUOUS chain, bybit, unchanged",
            "override": "admit an otherwise-gate-blocked entry iff hours_since_high_168h <= "
            f"{FRESH_OVERRIDE_MAX_HOURS} at the entry bar close (T-C feature definition; "
            "min history 24 bars; unknown does NOT override)",
            "everything_else": "identical to deployed (sizing, TP 12%, 24h max hold, hedge)",
        },
        "known_failure_mode": "2025-03/2025-04 lost -17.7pp (component-summed gate-off units) "
        "before the favorable 2025-08..2026-07 run; tail-day trades net -2.36pp over the render window",
        "grading": "rolling forward ledger only: days after this file's git commit date; "
        "spent-surface numbers in tj_* artifacts are context, not evidence",
        "declared_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
    }
    prototype_path = out_dir / "prototype_freshness_gate_override.json"
    prototype_path.write_text(json.dumps(prototype, indent=1) + "\n", encoding="utf-8")

    common.write_manifest(
        out_dir,
        kind="strategy_research_v5_tj_deployed_conditioning",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "render_caches": {
                "render_kline_slice_1h.parquet": render_kline_sha,
                "render_funding_events.parquet": render_funding_sha,
            },
            "ta_render_books": str(v4_shared.TA_DIR),
        },
        params={
            "tilt_mults": list(TILT_MULTS),
            "tilt_budget_rule": "budget-neutral per book: at_high x mult, rest x c with total notional fixed",
            "permutations": N_PERMUTATIONS,
            "permutation_seed": PERMUTATION_SEED,
            "blocked_set": "gate_off component trades absent from the same component's gate_on book",
            "fresh_override_max_hours": FRESH_OVERRIDE_MAX_HOURS,
            "tp_distance_check": tp_distance,
            "tail_definition": "V2 common-loss dates (156) plus 2024-08-06",
        },
        output_files={
            "tj_deepneg_anatomy.csv": anatomy_path,
            "tj_blocked_decomposition.csv": blocked_path,
            "tj_barebones_crosscheck.csv": cross_path,
            "tj_tilt_controls.csv": tilt_path,
            "tj_lead_by_month.csv": month_path,
            "tj_lead_robustness.json": robustness_path,
            "prototype_freshness_gate_override.json": prototype_path,
        },
        extra={"explicit_non_conclusions": [
            "exploratory post-processing of already-rendered/cached spent surfaces; no alpha claim",
            "the prototype's evidence is exclusively post-commit forward days",
            "no engine, runtime, or deployment change; promotion is a separate owner decision",
        ]},
    )
    print(json.dumps({"outputs": sorted(p.name for p in out_dir.iterdir())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
