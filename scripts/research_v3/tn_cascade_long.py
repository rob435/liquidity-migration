#!/usr/bin/env python3
"""T-N: cascade-riding long — C-H1/C-H2 estimands + long-arm read (Lane-1).

Executes the frozen backlog estimands (docs/strategy_overhaul_lessons.md,
"Untested Backlog") under their frozen multiplicity rule, then reads the
T-N long arms (the deployed short book inverted) against the admission bar.

Declared design, frozen in this file:

- Population: engine-owned deciled panel rows (rmom-low-quartile universe;
  the exact production panel cache, exclusions baked in) that are
  "otherwise current-static-eligible": turnover_24h >= 500,000 USD
  (production liq gate), symbol age >= 240 d (archive membership), decile
  in {7,8,9}. Event formation: first eligible bar per symbol, then a 24 h
  per-symbol cooldown (the deployed engine has no separate event trigger;
  the cooldown collapses consecutive-hour repeats of one excursion into
  one decision — taxonomy item 29).
- Label: the 24-hour plain-hold path. Entry at the first 1h close at/after
  decision_ts + 1h (production entry delay; missing within 3h -> excluded
  + counted); exit at the first close >= entry + 24h (missing within 30h ->
  excluded + counted). No TP/stop variants — closed lines stay closed; the
  deployed TP12/24h shape is not touched.
- Primary tests (frozen family: max 4 per sleeve, family-alpha 0.05,
  Bonferroni per-test alpha 0.0125; dependence: unique decisions -> waves
  -> 28-day calendar blocks, block bootstrap 2000 draws, seed 20260720):
    1. C-H1 Bybit:  E[short path | D9] - E[short path | D7/8], stratified
       by terciles of ret1 x turnover_spike_168h x max_ret168 (categorical
       controls; tercile edges from the venue's event population; strata
       need >= 5 events per side), stratum-size weighted.
    2. C-H2 Bybit:  among D9 events, BTC-uptrend pass vs fail (BTC 30d
       return sign at decision time — the production gate), same controls.
    3. C-H1 Binance (same design; correlated robustness surface, never
       pooled).
    4. C-H2 Binance.
- T-N long arms (descriptive, ALL cells reported, no further tests):
  side long, net45 = long return + signed realized funding - 45 bp, cells
  {D9, D7/8} x {btc_up, btc_down} x era. Era split at 2025-01-01 (panel
  starts 2023-04 — no e2122 era exists here; stated). Admission bar:
  era-stable net >= +40 bp/trade, or >= 5 independent bets/day.
- Funding: realized settlements in (entry, exit], signed (long pays
  positive rates); cadence facts from the tape itself, never
  funding_interval_min.

Usage:
  .venv/Scripts/python.exe scripts/research_v3/tn_cascade_long.py \
      --venue bybit [--out-date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.research_v3 import common  # noqa: E402
from scripts.research_v3.tl_listing_conditional import (  # noqa: E402
    VENUES,
    partition_dates,
    read_btc_closes,
    read_membership,
    symbol_day_frame,
)
from scripts.research_v3.tm_funding_extreme_carry import read_funding_tape  # noqa: E402

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 24 * MS_PER_HOUR

PANEL_FILES = {
    "bybit": "_continuous_engine_panel_v4_rmom25_feat4a91acf4_excl1916e9b4_dc35f0ca.parquet",
    "binance": "_continuous_engine_panel_v3_rmom25_feat4a91acf4_excl1916e9b4_8ceeb21d.parquet",
}
LIQ_TURNOVER_MIN = 500_000.0
AGE_DAYS_MIN = 240
DECILES = (7, 8, 9)
COOLDOWN_MS = 24 * MS_PER_HOUR
ENTRY_DELAY_MS = MS_PER_HOUR
ENTRY_MAX_LAG_MS = 3 * MS_PER_HOUR
HOLD_MS = 24 * MS_PER_HOUR
EXIT_MAX_LAG_MS = 30 * MS_PER_HOUR
COST_RT = 0.0045
BTC_LOOKBACK_MS = 720 * MS_PER_HOUR
ERA_BOUNDARY = dt.date(2025, 1, 1)
BLOCK_DAYS = 28
BOOT_N = 2000
BOOT_SEED = 20260720
MIN_STRATUM_SIDE = 5
CONTROL_COLS = ("ret1", "turnover_spike_168h", "max_ret168")
FAMILY_ALPHA = 0.05
PRIMARY_TESTS = 4


def block_of(ts_ms: int) -> int:
    return int(ts_ms // (BLOCK_DAYS * MS_PER_DAY))


def era_of_ts(ts_ms: int) -> str:
    day = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date()
    return "e2526" if day >= ERA_BOUNDARY else "e2324"


def stratified_delta(
    frame: pl.DataFrame, group_col: str, value_col: str
) -> tuple[float | None, int, int]:
    """Stratum-size-weighted mean difference (group==1 minus group==0)."""
    total = 0
    used = 0
    acc = 0.0
    for _, part in frame.group_by("stratum"):
        a = part.filter(pl.col(group_col) == 1)[value_col]
        b = part.filter(pl.col(group_col) == 0)[value_col]
        if a.len() >= MIN_STRATUM_SIDE and b.len() >= MIN_STRATUM_SIDE:
            n = part.height
            acc += n * (float(a.mean()) - float(b.mean()))
            used += n
        total += part.height
    if used == 0:
        return None, used, total
    return acc / used, used, total


def block_bootstrap_delta(
    frame: pl.DataFrame, group_col: str, value_col: str, rng: np.random.Generator
) -> tuple[float | None, float | None, float | None]:
    """Point estimate, block-bootstrap s.e., and two-sided bootstrap p."""
    point, used, _ = stratified_delta(frame, group_col, value_col)
    if point is None:
        return None, None, None
    blocks = frame["block"].unique().to_list()
    if len(blocks) < 3:
        return point, None, None
    by_block = {b: frame.filter(pl.col("block") == b) for b in blocks}
    draws: list[float] = []
    for _ in range(BOOT_N):
        pick = rng.choice(len(blocks), size=len(blocks), replace=True)
        sample = pl.concat([by_block[blocks[i]] for i in pick], how="vertical")
        d, _, _ = stratified_delta(sample, group_col, value_col)
        if d is not None:
            draws.append(d)
    if len(draws) < BOOT_N // 2:
        return point, None, None
    arr = np.array(draws)
    se = float(arr.std(ddof=1))
    centered = arr - arr.mean()
    p = float(min(1.0, 2.0 * min((centered >= point).mean(), (centered <= point).mean())))
    return point, se, p


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", choices=sorted(VENUES), required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    args = parser.parse_args()
    cfg = VENUES[args.venue]
    root: Path = cfg["root"]
    funding_ds: str = cfg["funding_dataset"]

    out_dir = common.REPORT_ROOT / "t-n" / args.out_date
    if args.venue != "bybit":
        out_dir = out_dir / args.venue
    out_dir.mkdir(parents=True, exist_ok=True)

    panel_path = root / PANEL_FILES[args.venue]
    panel = pl.read_parquet(
        panel_path,
        columns=[
            "symbol",
            "ts_ms",
            "decile",
            "decision_ts_ms",
            "turnover_24h",
            *CONTROL_COLS,
        ],
    )
    print(f"[{args.venue}] panel {panel.height} rows from {panel_path.name}", flush=True)

    membership = read_membership(root)
    first_member_ms: dict[str, int] = {}
    for r in membership.group_by("symbol").agg(pl.col("date").min().alias("d0")).iter_rows(named=True):
        day = dt.date.fromisoformat(str(r["d0"]))
        first_member_ms[str(r["symbol"])] = int(
            dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).timestamp() * 1000
        )

    eligible = (
        panel.filter(
            pl.col("decile").is_in(list(DECILES))
            & (pl.col("turnover_24h") >= LIQ_TURNOVER_MIN)
            & pl.all_horizontal([pl.col(c).is_not_null() & pl.col(c).is_finite() for c in CONTROL_COLS])
        )
        .with_columns(
            pl.col("symbol").replace_strict(first_member_ms, default=None).alias("member_ms")
        )
        .drop_nulls("member_ms")
        .filter((pl.col("ts_ms") - pl.col("member_ms")) >= AGE_DAYS_MIN * MS_PER_DAY)
        .sort(["symbol", "ts_ms"])
    )
    print(f"eligible D7-D9 bars: {eligible.height}", flush=True)

    events: list[dict[str, Any]] = []
    for key, part in eligible.partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        last_event_ts = -(10**18)
        for row in part.iter_rows(named=True):
            ts = int(row["ts_ms"])
            if ts - last_event_ts < COOLDOWN_MS:
                continue
            last_event_ts = ts
            events.append({"symbol": symbol, **row})
    print(f"events after 24h cooldown: {len(events)}", flush=True)

    kline_dates = partition_dates(root, "klines_1h")
    data_end = kline_dates[-1]
    btc_ts, btc_px = read_btc_closes(root, dt.date(2023, 2, 20), data_end)
    tape = read_funding_tape(root, funding_ds, dt.date(2023, 3, 25))
    fund_series: dict[str, tuple[list[int], list[float]]] = {}
    for key, part in tape.partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        fund_series[symbol] = (
            [int(v) for v in part["ts_ms"].to_list()],
            [float(v) for v in part["funding_rate"].to_list()],
        )

    kline_cache: dict[tuple[str, str], tuple[list[int], list[float]] | None] = {}

    def closes(symbol: str, day: dt.date) -> tuple[list[int], list[float]]:
        key = (symbol, day.isoformat())
        if key not in kline_cache:
            frame = symbol_day_frame(root, "klines_1h", symbol, day)
            if frame is None:
                kline_cache[key] = None
            else:
                valid = frame.filter(
                    pl.col("close").is_not_null() & pl.col("close").is_finite() & (pl.col("close") > 0.0)
                ).sort("ts_ms")
                kline_cache[key] = (
                    [int(v) + MS_PER_HOUR for v in valid["ts_ms"].to_list()],
                    [float(v) for v in valid["close"].to_list()],
                )
        entry = kline_cache[key]
        return entry if entry is not None else ([], [])

    def close_at_or_after(symbol: str, ts_min: int, max_lag: int) -> tuple[int, float] | None:
        day = dt.datetime.fromtimestamp(ts_min / 1000, tz=dt.timezone.utc).date()
        for probe in (day, day + dt.timedelta(days=1), day + dt.timedelta(days=2)):
            if probe > data_end:
                return None
            bar_ts, bar_px = closes(symbol, probe)
            idx = bisect.bisect_left(bar_ts, ts_min)
            if idx < len(bar_ts):
                if bar_ts[idx] - ts_min > max_lag:
                    return None
                return bar_ts[idx], bar_px[idx]
        return None

    rows: list[dict[str, Any]] = []
    counters = {"entry_bar_missing": 0, "exit_bar_missing": 0, "btc_history_missing": 0}
    for event in events:
        decision_ts = int(event["decision_ts_ms"])
        entry = close_at_or_after(event["symbol"], decision_ts + ENTRY_DELAY_MS, ENTRY_MAX_LAG_MS)
        if entry is None:
            counters["entry_bar_missing"] += 1
            continue
        entry_ts, entry_px = entry
        exit_bar = close_at_or_after(event["symbol"], entry_ts + HOLD_MS, EXIT_MAX_LAG_MS - HOLD_MS)
        if exit_bar is None:
            counters["exit_bar_missing"] += 1
            continue
        exit_ts, exit_px = exit_bar

        bi = bisect.bisect_right(btc_ts, decision_ts) - 1
        bj = bisect.bisect_right(btc_ts, decision_ts - BTC_LOOKBACK_MS) - 1
        if bi < 0 or bj < 0:
            counters["btc_history_missing"] += 1
            continue
        btc_up = 1 if btc_px[bi] >= btc_px[bj] else 0

        ts_list, rate_list = fund_series.get(event["symbol"], ([], []))
        lo = bisect.bisect_right(ts_list, entry_ts)
        hi = bisect.bisect_right(ts_list, exit_ts)
        fund_sum = float(sum(rate_list[lo:hi]))

        long_ret = exit_px / entry_px - 1.0
        rows.append(
            {
                "symbol": event["symbol"],
                "decision_ts_ms": decision_ts,
                "date": dt.datetime.fromtimestamp(decision_ts / 1000, tz=dt.timezone.utc).date().isoformat(),
                "era": era_of_ts(decision_ts),
                "block": block_of(decision_ts),
                "decile": int(event["decile"]),
                "is_d9": 1 if int(event["decile"]) == 9 else 0,
                "btc_up": btc_up,
                "ret1": float(event["ret1"]),
                "turnover_spike_168h": float(event["turnover_spike_168h"]),
                "max_ret168": float(event["max_ret168"]),
                "hold_h": (exit_ts - entry_ts) / MS_PER_HOUR,
                "n_settlements": hi - lo,
                "long_gross": long_ret,
                "long_funding": -fund_sum,
                "short_path": -long_ret,
                "long_net45": long_ret - fund_sum - COST_RT,
            }
        )

    trades = pl.DataFrame(rows)
    print(f"labelled events: {trades.height}, counters {counters}", flush=True)

    edges = {
        c: (
            float(trades[c].quantile(1 / 3)),
            float(trades[c].quantile(2 / 3)),
        )
        for c in CONTROL_COLS
    }
    trades = trades.with_columns(
        pl.concat_str(
            [
                pl.col(c)
                .map_elements(
                    lambda v, e=edges[c]: 0 if v <= e[0] else (1 if v <= e[1] else 2),
                    return_dtype=pl.Int64,
                )
                .cast(pl.String)
                for c in CONTROL_COLS
            ],
            separator="-",
        ).alias("stratum")
    )

    rng = np.random.default_rng(BOOT_SEED)
    per_test_alpha = FAMILY_ALPHA / PRIMARY_TESTS
    estimand_rows: list[dict[str, Any]] = []
    ch1_point, ch1_se, ch1_p = block_bootstrap_delta(trades, "is_d9", "short_path", rng)
    estimand_rows.append(
        {
            "test": f"C-H1_{args.venue}",
            "contrast": "D9 minus D7/8, short-directional 24h path, stratified",
            "point_bp": ch1_point * 1e4 if ch1_point is not None else None,
            "se_bp": ch1_se * 1e4 if ch1_se is not None else None,
            "p_two_sided": ch1_p,
            "per_test_alpha": per_test_alpha,
            "significant": (ch1_p is not None and ch1_p < per_test_alpha),
            "n": trades.height,
        }
    )
    d9 = trades.filter(pl.col("is_d9") == 1)
    ch2_point, ch2_se, ch2_p = block_bootstrap_delta(d9, "btc_up", "short_path", rng)
    estimand_rows.append(
        {
            "test": f"C-H2_{args.venue}",
            "contrast": "BTC-uptrend pass minus fail among D9, short path, stratified",
            "point_bp": ch2_point * 1e4 if ch2_point is not None else None,
            "se_bp": ch2_se * 1e4 if ch2_se is not None else None,
            "p_two_sided": ch2_p,
            "per_test_alpha": per_test_alpha,
            "significant": (ch2_p is not None and ch2_p < per_test_alpha),
            "n": d9.height,
        }
    )

    arm_rows: list[dict[str, Any]] = []
    for d9_flag in (1, 0):
        for btc in (1, 0):
            base = trades.filter((pl.col("is_d9") == d9_flag) & (pl.col("btc_up") == btc))
            for era in ("pooled", "e2324", "e2526"):
                sub = base if era == "pooled" else base.filter(pl.col("era") == era)
                row: dict[str, Any] = {
                    "cell": f"{'D9' if d9_flag else 'D7_8'}_{'btc_up' if btc else 'btc_down'}",
                    "era": era,
                    "n": sub.height,
                }
                if sub.height:
                    blocks = sub["block"].to_numpy()
                    vals = sub["long_net45"].to_numpy()
                    unique_blocks = np.unique(blocks)
                    row["mean_long_gross_bp"] = float(sub["long_gross"].mean()) * 1e4
                    row["mean_long_funding_bp"] = float(sub["long_funding"].mean()) * 1e4
                    row["mean_long_net45_bp"] = float(sub["long_net45"].mean()) * 1e4
                    row["median_long_net45_bp"] = float(sub["long_net45"].median()) * 1e4
                    row["n_blocks"] = int(len(unique_blocks))
                    if len(unique_blocks) > 2:
                        groups = [vals[blocks == b] for b in unique_blocks]
                        means = np.empty(BOOT_N)
                        for i in range(BOOT_N):
                            pick = rng.integers(0, len(groups), size=len(groups))
                            means[i] = np.concatenate([groups[g] for g in pick]).mean()
                        row["se_long_net45_bp_blk"] = float(means.std(ddof=1)) * 1e4
                    events_per_day = sub.height / max(1, sub["date"].n_unique())
                    row["events_per_active_day"] = round(events_per_day, 2)
                arm_rows.append(row)

    outputs = {
        "events.csv": trades,
        "estimands.csv": pl.DataFrame(estimand_rows),
        "long_arms.csv": pl.DataFrame(arm_rows),
        "exclusions.csv": pl.DataFrame(
            {"reason": list(counters.keys()), "count": list(counters.values())}
        ),
    }
    output_paths: dict[str, Path] = {}
    for name, frame in outputs.items():
        path = out_dir / name
        frame.write_csv(path)
        output_paths[name] = path

    common.write_manifest(
        out_dir,
        kind="tn_cascade_long",
        inputs={
            "venue": args.venue,
            "panel": panel_path.name,
            "panel_rows": panel.height,
            "funding_rows": tape.height,
        },
        params={
            "deciles": list(DECILES),
            "liq_turnover_min": LIQ_TURNOVER_MIN,
            "age_days_min": AGE_DAYS_MIN,
            "cooldown_h": COOLDOWN_MS // MS_PER_HOUR,
            "entry_delay_h": ENTRY_DELAY_MS // MS_PER_HOUR,
            "hold_h": HOLD_MS // MS_PER_HOUR,
            "cost_rt": COST_RT,
            "era_boundary": ERA_BOUNDARY.isoformat(),
            "block_days": BLOCK_DAYS,
            "boot": {"n": BOOT_N, "seed": BOOT_SEED},
            "min_stratum_side": MIN_STRATUM_SIDE,
            "control_tercile_edges": edges,
            "family": {"alpha": FAMILY_ALPHA, "primary_tests": PRIMARY_TESTS},
        },
        output_files=output_paths,
        extra={
            "data_root": str(root),
            "study": "t-n",
            "counters": counters,
            "n_events": trades.height,
        },
    )
    print(f"wrote {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
