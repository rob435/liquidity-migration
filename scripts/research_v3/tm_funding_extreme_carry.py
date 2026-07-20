#!/usr/bin/env python3
"""T-M: funding-extreme carry study (Lane-1, exploratory).

Claim under test: episodes of extreme perp funding (8h-equivalent settled
rate beyond declared thresholds) identify a carry trade -- position against
the paying crowd, optionally hedged with an explicit BTC leg -- whose
collected funding survives price drift, hedge costs, and the era split.

Declared design, frozen in this file:

- Venues: bybit / binance full-PIT roots. Entries floored at 2021-05-01 for
  BOTH venues. This deliberately narrows the queue's "Binance 2019-09 ->"
  span: reading pre-2021-05 Binance funding would feature-touch the frozen
  G1 window and further touch G2
  (``docs/preregistration/untouched_slice_provenance_2026-07-20.md``);
  both stay unread by this family. Stated as a non-conclusion.
- Settlement cadence is derived per settlement from observed spacing
  (median of trailing 3 gaps, snapped to the nearest of {60,120,240,480}
  min; the first settlement uses the following gap). rate8h = rate * 480 /
  interval_min. ``funding_interval_min`` labels are never read.
- Episode (per symbol, sign s in {+1,-1}, threshold X in {0.15%, 0.30%,
  0.50%} per 8h): maximal run of consecutive settlements with
  s*rate8h >= X, where consecutive also requires the gap to the previous
  settlement be <= 2x its snapped interval (tape gaps break runs).
- Arms: X x sign x persistence P in {1,2}. Entry after the P-th settlement
  of a run (first 1h close following it; entry excluded+counted if no bar
  within 3h). Single declared exit: first 1h close after the settlement
  that ends the run (s*rate8h < X), capped at entry+14d; unresolved runs at
  tape end are right-censored (excluded+counted). No stops, no targets --
  the funding state IS the trade; per-trade price-exit variants remain a
  closed line.
- Economics: alt leg position -s (short the crowd that pays); per-settlement
  funding transfer to the trade = s*rate_raw on the alt leg. Optional hedge:
  BTC leg position +s, transfer -s*rate_btc; explicit costs 45 bp round
  trip PER LEG (90 bp hedged), stress line 90/180 bp. Gross uses entry/exit
  1h closes; no intraday path stats are claimed (missing, not zero).
- Reporting: episode inventory (counts x era x age bucket x persistence,
  duration/peak distributions) and per-arm P&L with every component
  (alt gross, alt funding, BTC gross, BTC funding, costs) x era, hedged and
  unhedged, 7d-block cluster bootstrap (2000, seed 20260720). Age buckets
  {<30d, 30-240d, >=240d} from first archive membership; left-censored ages
  are labelled, never guessed. All cells reported.

Usage:
  .venv/Scripts/python.exe scripts/research_v3/tm_funding_extreme_carry.py \
      --venue bybit [--out-date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob as globmod
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

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 24 * MS_PER_HOUR

ENTRY_FLOOR = dt.date(2021, 5, 1)
THRESHOLDS = (0.0015, 0.0030, 0.0050)
SIGNS = (1, -1)
PERSISTENCE = (1, 2)
MAX_HOLD_DAYS = 14
ENTRY_BAR_MAX_LAG_MS = 3 * MS_PER_HOUR
COST_LEG = 0.0045
SNAP_INTERVALS_MIN = (60.0, 120.0, 240.0, 480.0)
ERA_BOUNDS = (dt.date(2023, 1, 1), dt.date(2025, 1, 1))
BOOT_N = 2000
BOOT_SEED = 20260720
AGE_EDGES_DAYS = (30, 240)
AGE_LABELS = ("age_lt30", "age_30_240", "age_ge240")


def era_of(day: dt.date) -> str:
    if day < ERA_BOUNDS[0]:
        return "e2122"
    if day < ERA_BOUNDS[1]:
        return "e2324"
    return "e2526"


def read_funding_tape(root: Path, dataset: str, start: dt.date) -> pl.DataFrame:
    pattern = str(root / dataset / "date=*")
    dirs = sorted(d for d in globmod.glob(pattern) if d[-10:] >= start.isoformat())
    files: list[str] = []
    for d in dirs:
        files.extend(globmod.glob(d + "/**/*.parquet", recursive=True))
    if not files:
        raise RuntimeError(f"no funding files under {root / dataset} from {start}")
    frame = pl.read_parquet(files, columns=["ts_ms", "symbol", "funding_rate"])
    frame = frame.drop_nulls().filter(pl.col("funding_rate").is_finite())
    conflicts = (
        frame.group_by(["symbol", "ts_ms"])
        .agg(pl.col("funding_rate").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    if not conflicts.is_empty():
        raise RuntimeError(f"conflicting duplicate funding rates: {conflicts.height} pairs")
    return frame.unique(["symbol", "ts_ms"], keep="first").sort(["symbol", "ts_ms"])


def snap_interval(gap_min: float) -> float:
    return min(SNAP_INTERVALS_MIN, key=lambda v: abs(v - gap_min))


def per_symbol_rate8h(ts: list[int], rates: list[float]) -> tuple[list[float], list[float]]:
    """Return (rate8h, snapped_interval_min) per settlement."""
    n = len(ts)
    gaps = [(ts[i] - ts[i - 1]) / 60000.0 for i in range(1, n)]
    out_rate: list[float] = []
    out_iv: list[float] = []
    for i in range(n):
        if n == 1:
            iv = 480.0
        elif i == 0:
            iv = snap_interval(gaps[0])
        else:
            window = gaps[max(0, i - 3): i]
            iv = snap_interval(float(np.median(window)))
        out_iv.append(iv)
        out_rate.append(rates[i] * (480.0 / iv))
    return out_rate, out_iv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", choices=sorted(VENUES), required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    args = parser.parse_args()
    cfg = VENUES[args.venue]
    root: Path = cfg["root"]
    funding_ds: str = cfg["funding_dataset"]

    out_dir = common.REPORT_ROOT / "t-m" / args.out_date
    if args.venue != "bybit":
        out_dir = out_dir / args.venue
    out_dir.mkdir(parents=True, exist_ok=True)

    kline_dates = partition_dates(root, "klines_1h")
    data_end = kline_dates[-1]
    tape = read_funding_tape(root, funding_ds, ENTRY_FLOOR - dt.timedelta(days=7))
    print(f"[{args.venue}] funding rows {tape.height}, symbols {tape['symbol'].n_unique()}", flush=True)

    membership = read_membership(root)
    first_member: dict[str, dt.date] = {
        str(r["symbol"]): dt.date.fromisoformat(min(r["dates"]))
        for r in membership.group_by("symbol").agg(pl.col("date").alias("dates")).iter_rows(named=True)
    }
    root_first_day = min(first_member.values())

    btc_ts, btc_px = read_btc_closes(root, ENTRY_FLOOR - dt.timedelta(days=2), data_end)
    btc_part = tape.filter(pl.col("symbol") == "BTCUSDT").sort("ts_ms")
    btc_fund_ts = [int(v) for v in btc_part["ts_ms"].to_list()]
    btc_fund_rate = [float(v) for v in btc_part["funding_rate"].to_list()]

    floor_ms = int(dt.datetime.combine(ENTRY_FLOOR, dt.time(), tzinfo=dt.timezone.utc).timestamp() * 1000)
    kline_cache: dict[tuple[str, str], dict[str, Any] | None] = {}

    def hour_closes(symbol: str, day: dt.date) -> tuple[list[int], list[float]]:
        key = (symbol, day.isoformat())
        if key not in kline_cache:
            frame = symbol_day_frame(root, "klines_1h", symbol, day)
            if frame is None:
                kline_cache[key] = None
            else:
                valid = frame.filter(
                    pl.col("close").is_not_null() & pl.col("close").is_finite() & (pl.col("close") > 0.0)
                ).sort("ts_ms")
                kline_cache[key] = {
                    "ts": [int(v) + MS_PER_HOUR for v in valid["ts_ms"].to_list()],
                    "px": [float(v) for v in valid["close"].to_list()],
                }
        entry = kline_cache[key]
        if entry is None:
            return [], []
        return entry["ts"], entry["px"]

    def first_close_after(symbol: str, ts_min: int, max_lag_ms: int) -> tuple[int, float] | None:
        day = dt.datetime.fromtimestamp(ts_min / 1000, tz=dt.timezone.utc).date()
        for probe in (day, day + dt.timedelta(days=1)):
            if probe > data_end:
                return None
            bar_ts, bar_px = hour_closes(symbol, probe)
            idx = bisect.bisect_right(bar_ts, ts_min)
            if idx < len(bar_ts):
                if bar_ts[idx] - ts_min > max_lag_ms:
                    return None
                return bar_ts[idx], bar_px[idx]
        return None

    episode_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    counters = {
        "entry_bar_missing": 0,
        "exit_bar_missing": 0,
        "right_censored_run": 0,
        "pre_floor_entry": 0,
    }

    tape_end_ms = int(tape["ts_ms"].max())
    for key, part in tape.partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        if symbol == "BTCUSDT":
            continue
        ts = [int(v) for v in part["ts_ms"].to_list()]
        rates = [float(v) for v in part["funding_rate"].to_list()]
        rate8h, iv_min = per_symbol_rate8h(ts, rates)
        member_day = first_member.get(symbol)
        for sign in SIGNS:
            for x in THRESHOLDS:
                i = 0
                n = len(ts)
                while i < n:
                    if sign * rate8h[i] < x:
                        i += 1
                        continue
                    j = i
                    while (
                        j + 1 < n
                        and sign * rate8h[j + 1] >= x
                        and (ts[j + 1] - ts[j]) / 60000.0 <= 2.0 * iv_min[j + 1]
                    ):
                        j += 1
                    run_ts = ts[i: j + 1]
                    run_rates8h = rate8h[i: j + 1]
                    start_day = dt.datetime.fromtimestamp(run_ts[0] / 1000, tz=dt.timezone.utc).date()
                    resolved = j + 1 < n
                    age_days = (start_day - member_day).days if member_day else None
                    if age_days is None:
                        age_cell = "age_unknown"
                    elif member_day == root_first_day and age_days < AGE_EDGES_DAYS[1]:
                        age_cell = "age_censored"
                    else:
                        age_cell = AGE_LABELS[bisect.bisect_right(list(AGE_EDGES_DAYS), age_days)]
                    if run_ts[0] >= floor_ms:
                        episode_rows.append(
                            {
                                "symbol": symbol,
                                "sign": sign,
                                "threshold": x,
                                "start": start_day.isoformat(),
                                "era": era_of(start_day),
                                "age_cell": age_cell,
                                "n_settlements": len(run_ts),
                                "duration_h": (run_ts[-1] - run_ts[0]) / MS_PER_HOUR,
                                "peak_rate8h": float(max(sign * r for r in run_rates8h) * sign),
                                "sum_rate_raw": float(sum(rates[i: j + 1])),
                                "resolved": resolved,
                            }
                        )
                    for p in PERSISTENCE:
                        if len(run_ts) < p:
                            continue
                        entry_settle_ts = run_ts[p - 1]
                        if entry_settle_ts < floor_ms:
                            counters["pre_floor_entry"] += 1
                            continue
                        if not resolved:
                            cap_ts = entry_settle_ts + MAX_HOLD_DAYS * MS_PER_DAY
                            if cap_ts > tape_end_ms:
                                counters["right_censored_run"] += 1
                                continue
                        entry = first_close_after(symbol, entry_settle_ts, ENTRY_BAR_MAX_LAG_MS)
                        if entry is None:
                            counters["entry_bar_missing"] += 1
                            continue
                        entry_ts, entry_px = entry
                        end_settle_ts = ts[j + 1] if resolved else None
                        cap_ts = entry_settle_ts + MAX_HOLD_DAYS * MS_PER_DAY
                        exit_trigger_ts = min(end_settle_ts, cap_ts) if end_settle_ts is not None else cap_ts
                        exit_bar = first_close_after(symbol, exit_trigger_ts, 30 * MS_PER_DAY)
                        if exit_bar is None or exit_bar[0] <= entry_ts:
                            counters["exit_bar_missing"] += 1
                            continue
                        exit_ts, exit_px = exit_bar

                        lo = bisect.bisect_right(ts, entry_ts)
                        hi = bisect.bisect_right(ts, exit_ts)
                        alt_funding = sign * float(sum(rates[lo:hi]))
                        gross_alt = (-sign) * (exit_px / entry_px - 1.0)

                        bi = bisect.bisect_right(btc_ts, entry_ts) - 1
                        bj = bisect.bisect_right(btc_ts, exit_ts) - 1
                        if bi < 0 or bj < 0:
                            counters["exit_bar_missing"] += 1
                            continue
                        gross_btc = sign * (btc_px[bj] / btc_px[bi] - 1.0)
                        fl = bisect.bisect_right(btc_fund_ts, entry_ts)
                        fh = bisect.bisect_right(btc_fund_ts, exit_ts)
                        btc_funding = (-sign) * float(sum(btc_fund_rate[fl:fh]))

                        entry_day = dt.datetime.fromtimestamp(entry_ts / 1000, tz=dt.timezone.utc).date()
                        net_unhedged = gross_alt + alt_funding - COST_LEG
                        net_hedged = gross_alt + alt_funding + gross_btc + btc_funding - 2 * COST_LEG
                        trade_rows.append(
                            {
                                "symbol": symbol,
                                "sign": sign,
                                "threshold": x,
                                "persistence": p,
                                "entry": entry_day.isoformat(),
                                "era": era_of(entry_day),
                                "age_cell": age_cell,
                                "week_block": entry_day.toordinal() // 7,
                                "hold_h": (exit_ts - entry_ts) / MS_PER_HOUR,
                                "n_alt_settlements_hold": hi - lo,
                                "gross_alt": gross_alt,
                                "alt_funding": alt_funding,
                                "gross_btc": gross_btc,
                                "btc_funding": btc_funding,
                                "net_unhedged": net_unhedged,
                                "net_hedged": net_hedged,
                                "capped": bool(exit_trigger_ts == cap_ts),
                            }
                        )
                    i = j + 1

    episodes = pl.DataFrame(episode_rows)
    trades = pl.DataFrame(trade_rows)
    print(f"episodes {episodes.height}, trades {trades.height}, counters {counters}", flush=True)

    inventory = (
        episodes.group_by(["threshold", "sign", "era", "age_cell"])
        .agg(
            pl.len().alias("n_episodes"),
            pl.col("n_settlements").median().alias("med_settlements"),
            pl.col("duration_h").median().alias("med_duration_h"),
            (pl.col("peak_rate8h").abs().median() * 1e4).alias("med_peak_rate8h_bp"),
            pl.col("symbol").n_unique().alias("n_symbols"),
        )
        .sort(["threshold", "sign", "era", "age_cell"])
    )

    rng = np.random.default_rng(BOOT_SEED)

    def boot_se(values: np.ndarray, weeks: np.ndarray) -> float | None:
        unique_weeks = np.unique(weeks)
        if len(unique_weeks) < 2:
            return None
        groups = [values[weeks == w] for w in unique_weeks]
        means = np.empty(BOOT_N)
        for b in range(BOOT_N):
            pick = rng.integers(0, len(groups), size=len(groups))
            means[b] = np.concatenate([groups[g] for g in pick]).mean()
        return float(means.std(ddof=1))

    arm_rows: list[dict[str, Any]] = []
    for x in THRESHOLDS:
        for sign in SIGNS:
            for p in PERSISTENCE:
                base = trades.filter(
                    (pl.col("threshold") == x) & (pl.col("sign") == sign) & (pl.col("persistence") == p)
                )
                for era in ("pooled", "e2122", "e2324", "e2526", "pre2025", "post2025"):
                    if era == "pooled":
                        sub = base
                    elif era == "pre2025":
                        sub = base.filter(pl.col("era") != "e2526")
                    elif era == "post2025":
                        sub = base.filter(pl.col("era") == "e2526")
                    else:
                        sub = base.filter(pl.col("era") == era)
                    row: dict[str, Any] = {
                        "threshold": x,
                        "sign": sign,
                        "persistence": p,
                        "era": era,
                        "n_trades": sub.height,
                    }
                    if sub.height:
                        weeks = sub["week_block"].to_numpy()
                        for col in (
                            "gross_alt",
                            "alt_funding",
                            "gross_btc",
                            "btc_funding",
                            "net_unhedged",
                            "net_hedged",
                        ):
                            row[f"mean_{col}_bp"] = float(sub[col].mean()) * 1e4
                        row["mean_hold_h"] = float(sub["hold_h"].mean())
                        row["n_weeks"] = int(len(np.unique(weeks)))
                        row["capped_share"] = float(sub["capped"].mean())
                        se_h = boot_se(sub["net_hedged"].to_numpy(), weeks)
                        se_u = boot_se(sub["net_unhedged"].to_numpy(), weeks)
                        row["se_net_hedged_bp_wk"] = se_h * 1e4 if se_h is not None else None
                        row["se_net_unhedged_bp_wk"] = se_u * 1e4 if se_u is not None else None
                        row["median_net_hedged_bp"] = float(sub["net_hedged"].median()) * 1e4
                        row["win_rate_hedged"] = float((sub["net_hedged"] > 0).mean())
                    arm_rows.append(row)

    outputs = {
        "episodes.csv": episodes,
        "inventory_summary.csv": inventory,
        "trades.csv": trades,
        "arms_summary.csv": pl.DataFrame(arm_rows),
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
        kind="tm_funding_extreme_carry",
        inputs={
            "venue": args.venue,
            "funding_rows": tape.height,
            "funding_symbols": tape["symbol"].n_unique(),
            "kline_end": data_end.isoformat(),
        },
        params={
            "entry_floor": ENTRY_FLOOR.isoformat(),
            "thresholds_8h": list(THRESHOLDS),
            "signs": list(SIGNS),
            "persistence": list(PERSISTENCE),
            "max_hold_days": MAX_HOLD_DAYS,
            "cost_per_leg_rt": COST_LEG,
            "snap_intervals_min": list(SNAP_INTERVALS_MIN),
            "era_bounds": [d.isoformat() for d in ERA_BOUNDS],
            "age_edges_days": list(AGE_EDGES_DAYS),
            "boot": {"n": BOOT_N, "seed": BOOT_SEED, "cluster": "7d-ordinal-block"},
        },
        output_files=output_paths,
        extra={
            "data_root": str(root),
            "study": "t-m",
            "counters": counters,
            "g1_g2_note": "entries and inventory floored at 2021-05-01; pre-2021-05 funding unread by this family",
        },
    )
    print(f"wrote {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
