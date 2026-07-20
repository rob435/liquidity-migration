#!/usr/bin/env python3
"""T-L: conditional listing study (Lane-1, exploratory).

Claim under test: newly listed perp symbols carry a systematic post-listing
drift in their first week (short d1/d2 -> d7 after the initial pump; long
mirror where the data says so) that is conditionable on entry-time state to
clear the program admission bar (era-stable net >= +40 bp/trade after the
frozen 45 bp round-trip hurdle and realized funding).

Declared design, frozen in this file (the commit carrying it is the record):

- Population: every symbol whose first archive-observed membership date d0 is
  on or after 2021-05-01 (preserves the frozen G1/G2/G3 grading windows of
  ``docs/preregistration/untouched_slice_provenance_2026-07-20.md`` unread by
  this family) and early enough that d0+7 has kline AND funding coverage.
  Symbols with a membership gap >= 30 days are quarantined as relist/reused
  tickers (counted, excluded). Missing exit/entry bars exclude the event from
  the affected arm and are counted, never zero-filled.
- Arms (no exit variants -- per-trade exit research is a closed line):
  side in {short, long} x entry day in {d1, d2} (entry at that UTC day's last
  1h close), single exit at d7's last 1h close.
- Economics: gross next to the frozen 45 bp round-trip cost and a 90 bp
  listing-week stress line; realized funding settlements in (entry, exit]
  signed per side (short receives positive rates). Settlement cadence is
  derived from observed spacing, never from ``funding_interval_min``.
- Entry-time conditioning features (all causal at the entry close):
  pump01   = close_d1 / open_d0 - 1        bins (-inf,0], (0,.25], (.25,1], (1,inf)
  tdecay   = per-hour turnover entry-day / per-hour turnover d0
                                           bins [0,.3), [.3,.7), [.7,inf)
  fund8h   = last settled rate <= entry, scaled to 8h by observed cadence
                                           bins (-inf,-5e-4), [-5e-4,5e-4], (5e-4,inf)
                                           plus 'fund_none' when no settlement yet
  crowd7d  = population listings in [d0-6d, d0] excluding self
                                           bins {0,1}, {2..4}, {>=5}
  btc30d   = BTC close at entry vs 720h earlier   bins down (<0), up (>=0)
  The queue's literal "turnover-decay d1->d3 vs d0" is NOT causal at a d2
  entry; the causal per-hour variant above replaces it (deviation stated).
- One declared two-way interaction only: pump01 x fund8h (the economic
  thesis: post-pump shorts financed by positive funding). No other crosses.
- Eras by d0: e2122 [2021-05-01, 2023-01-01), e2324 [2023-01-01, 2025-01-01),
  e2526 [2025-01-01, ...). Primary stability read: pre-2025 vs post-2025.
- Uncertainty: 7-calendar-day listing blocks are the cluster unit
  (taxonomy item 29); cluster bootstrap (2000 resamples, seed 20260720).
- The reserved V2 label tape is never read; raw 2025-2026 klines/funding are
  the same seen surface the R1/T-A generation used (Lane-1).

Usage:
  .venv/Scripts/python.exe scripts/research_v3/tl_listing_conditional.py \
      --venue bybit [--out-date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.research_v3 import common  # noqa: E402

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 24 * MS_PER_HOUR

EVENT_FLOOR = dt.date(2021, 5, 1)
GAP_QUARANTINE_DAYS = 30
EXIT_DAY = 7
ENTRY_DAYS = (1, 2)
SIDES = ("short", "long")
COST_RT = 0.0045
COST_RT_STRESS = 0.0090
BOOT_N = 2000
BOOT_SEED = 20260720
ERA_BOUNDS = (dt.date(2023, 1, 1), dt.date(2025, 1, 1))

PUMP_EDGES = (0.0, 0.25, 1.0)
PUMP_LABELS = ("pump_le0", "pump_0_25", "pump_25_100", "pump_gt100")
TDECAY_EDGES = (0.3, 0.7)
TDECAY_LABELS = ("tdec_lt30", "tdec_30_70", "tdec_ge70")
FUND_EDGES = (-5e-4, 5e-4)
FUND_LABELS = ("fund_neg", "fund_flat", "fund_pos")
CROWD_EDGES = (2, 5)
CROWD_LABELS = ("crowd_01", "crowd_24", "crowd_ge5")
BTC_LABELS = ("btc_down", "btc_up")

VENUES: dict[str, dict[str, Any]] = {
    "bybit": {
        "root": Path.home() / "SHARED_DATA" / "bybit_full_pit",
        "funding_dataset": "funding",
    },
    "binance": {
        "root": Path.home() / "SHARED_DATA" / "binance_full_pit",
        "funding_dataset": "binance_usdm_funding",
    },
}


@dataclass
class Event:
    symbol: str
    d0: dt.date
    daily: dict[int, dict[str, float]] = field(default_factory=dict)
    funding_ts: list[int] = field(default_factory=list)
    funding_rate: list[float] = field(default_factory=list)
    features: dict[str, str] = field(default_factory=dict)
    numeric: dict[str, float] = field(default_factory=dict)


def partition_dates(root: Path, dataset: str) -> list[dt.date]:
    dates = []
    for entry in (root / dataset).iterdir():
        if entry.is_dir() and entry.name.startswith("date="):
            dates.append(dt.date.fromisoformat(entry.name[5:]))
    return sorted(dates)


def read_membership(root: Path) -> pl.DataFrame:
    files: list[Path] = []
    for entry in sorted((root / "archive_trade_manifest").iterdir()):
        if entry.is_dir() and entry.name.startswith("date="):
            files.extend(sorted(entry.glob("*.parquet")))
    if not files:
        raise RuntimeError(f"no manifest files under {root}")
    return pl.read_parquet(files, columns=["date", "symbol", "first_archive_observed_date"])


def symbol_day_frame(root: Path, dataset: str, symbol: str, day: dt.date) -> pl.DataFrame | None:
    part = root / dataset / f"date={day.isoformat()}" / f"symbol={symbol}"
    if not part.is_dir():
        return None
    files = sorted(part.rglob("*.parquet"))
    if not files:
        return None
    return pl.read_parquet(files)


def daily_bar(frame: pl.DataFrame) -> dict[str, float] | None:
    valid = frame.filter(
        pl.all_horizontal(
            [
                pl.col(c).is_not_null() & pl.col(c).is_finite() & (pl.col(c) > 0.0)
                for c in ("open", "high", "low", "close")
            ]
        )
    ).sort("ts_ms")
    if valid.is_empty():
        return None
    return {
        "open": float(valid["open"][0]),
        "close": float(valid["close"][-1]),
        "high": float(valid["high"].max()),
        "low": float(valid["low"].min()),
        "turnover": float(valid["turnover_quote"].sum()),
        "n_bars": float(valid.height),
        "last_bar_end_ms": float(int(valid["ts_ms"][-1]) + MS_PER_HOUR),
    }


def read_btc_closes(root: Path, start: dt.date, end_inclusive: dt.date) -> tuple[list[int], list[float]]:
    ts: list[int] = []
    px: list[float] = []
    day = start
    while day <= end_inclusive:
        frame = symbol_day_frame(root, "klines_1h", "BTCUSDT", day)
        if frame is not None:
            valid = frame.filter(
                pl.col("close").is_not_null() & pl.col("close").is_finite() & (pl.col("close") > 0.0)
            ).sort("ts_ms")
            ts.extend(int(v) + MS_PER_HOUR for v in valid["ts_ms"].to_list())
            px.extend(float(v) for v in valid["close"].to_list())
        day += dt.timedelta(days=1)
    if not ts:
        raise RuntimeError("no BTC closes read")
    return ts, px


def read_event_funding(root: Path, dataset: str, symbol: str, d0: dt.date, days: int) -> tuple[list[int], list[float]]:
    frames: list[pl.DataFrame] = []
    for offset in range(days + 1):
        frame = symbol_day_frame(root, dataset, symbol, d0 + dt.timedelta(days=offset))
        if frame is not None:
            frames.append(frame.select(["ts_ms", "funding_rate"]))
    if not frames:
        return [], []
    merged = pl.concat(frames, how="vertical").drop_nulls()
    merged = merged.filter(pl.col("funding_rate").is_finite())
    conflicts = (
        merged.group_by("ts_ms").agg(pl.col("funding_rate").n_unique().alias("n")).filter(pl.col("n") > 1)
    )
    if not conflicts.is_empty():
        raise RuntimeError(f"conflicting funding duplicates for {symbol} near {d0}")
    merged = merged.unique("ts_ms").sort("ts_ms")
    return [int(v) for v in merged["ts_ms"].to_list()], [float(v) for v in merged["funding_rate"].to_list()]


def bin_label(value: float, edges: tuple[float, ...], labels: tuple[str, ...]) -> str:
    return labels[bisect.bisect_right(list(edges), value)]


def era_of(d0: dt.date) -> str:
    if d0 < ERA_BOUNDS[0]:
        return "e2122"
    if d0 < ERA_BOUNDS[1]:
        return "e2324"
    return "e2526"


def cell_stats(rows: list[dict[str, Any]], rng: np.random.Generator) -> dict[str, Any]:
    net45 = np.array([r["net45"] for r in rows])
    gross = np.array([r["gross"] for r in rows])
    fund = np.array([r["funding"] for r in rows])
    net90 = np.array([r["net90"] for r in rows])
    weeks = np.array([r["week_block"] for r in rows])
    unique_weeks = np.unique(weeks)
    if len(unique_weeks) > 1:
        by_week = [net45[weeks == w] for w in unique_weeks]
        means = np.empty(BOOT_N)
        for i in range(BOOT_N):
            pick = rng.integers(0, len(by_week), size=len(by_week))
            means[i] = np.concatenate([by_week[j] for j in pick]).mean()
        se = float(means.std(ddof=1))
    else:
        se = float("nan")
    return {
        "n_events": int(len(rows)),
        "n_weeks": int(len(unique_weeks)),
        "mean_gross_bp": float(gross.mean() * 1e4),
        "mean_funding_bp": float(fund.mean() * 1e4),
        "mean_net45_bp": float(net45.mean() * 1e4),
        "mean_net90_bp": float(net90.mean() * 1e4),
        "se_net45_bp_wk": se * 1e4 if np.isfinite(se) else None,
        "median_net45_bp": float(np.median(net45) * 1e4),
        "win_rate_net45": float((net45 > 0).mean()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", choices=sorted(VENUES), required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    args = parser.parse_args()
    cfg = VENUES[args.venue]
    root: Path = cfg["root"]
    funding_ds: str = cfg["funding_dataset"]

    out_dir = common.REPORT_ROOT / "t-l" / args.out_date
    if args.venue != "bybit":
        out_dir = out_dir / args.venue
    out_dir.mkdir(parents=True, exist_ok=True)

    kline_dates = partition_dates(root, "klines_1h")
    funding_dates = partition_dates(root, funding_ds)
    data_end = min(kline_dates[-1], funding_dates[-1])
    last_d0 = data_end - dt.timedelta(days=EXIT_DAY + 1)
    print(f"[{args.venue}] kline end {kline_dates[-1]}, funding end {funding_dates[-1]}, last d0 {last_d0}", flush=True)

    membership = read_membership(root)
    per_symbol = (
        membership.group_by("symbol")
        .agg(pl.col("date").sort().alias("dates"), pl.col("first_archive_observed_date").min().alias("first_obs"))
        .sort("symbol")
    )

    exclusions: dict[str, list[str]] = {
        "pre_floor": [],
        "right_censored": [],
        "membership_gap_relist": [],
        "kline_gap_d0": [],
        "manifest_vs_kline_d0_mismatch": [],
    }
    candidates: list[tuple[str, dt.date]] = []
    for row in per_symbol.iter_rows(named=True):
        symbol = str(row["symbol"])
        dates = [dt.date.fromisoformat(d) for d in row["dates"]]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        d0 = dates[0]
        if any(g >= GAP_QUARANTINE_DAYS for g in gaps):
            exclusions["membership_gap_relist"].append(symbol)
            continue
        if d0 < EVENT_FLOOR:
            exclusions["pre_floor"].append(symbol)
            continue
        if d0 > last_d0:
            exclusions["right_censored"].append(symbol)
            continue
        first_obs = str(row["first_obs"])
        if first_obs and first_obs != d0.isoformat():
            # first_archive_observed_date should equal the first membership day
            exclusions["manifest_vs_kline_d0_mismatch"].append(f"{symbol}:{first_obs}!={d0}")
            continue
        candidates.append((symbol, d0))
    print(f"candidates {len(candidates)}; exclusions " + str({k: len(v) for k, v in exclusions.items()}), flush=True)

    all_d0 = sorted(d for _, d in candidates)
    d0_ordinals = [d.toordinal() for d in all_d0]

    btc_start = EVENT_FLOOR - dt.timedelta(days=31)
    btc_ts, btc_px = read_btc_closes(root, btc_start, data_end)
    print(f"btc closes {len(btc_ts)}", flush=True)

    events: list[Event] = []
    arm_exclusions = {"entry_bar_missing": 0, "exit_bar_missing": 0}
    for symbol, d0 in candidates:
        event = Event(symbol=symbol, d0=d0)
        missing_d0 = True
        for offset in range(EXIT_DAY + 1):
            frame = symbol_day_frame(root, "klines_1h", symbol, d0 + dt.timedelta(days=offset))
            if frame is None:
                continue
            bar = daily_bar(frame)
            if bar is None:
                continue
            event.daily[offset] = bar
            if offset == 0:
                missing_d0 = False
        if missing_d0:
            exclusions["kline_gap_d0"].append(symbol)
            continue
        event.funding_ts, event.funding_rate = read_event_funding(root, funding_ds, symbol, d0, EXIT_DAY)
        events.append(event)
    print(f"events with d0 bars: {len(events)}; kline_gap_d0 {len(exclusions['kline_gap_d0'])}", flush=True)

    trade_rows: list[dict[str, Any]] = []
    feature_rows: list[dict[str, Any]] = []
    for event in events:
        d0_bar = event.daily[0]
        bar1 = event.daily.get(1)
        pump01 = (bar1["close"] / d0_bar["open"] - 1.0) if bar1 else None
        lo = bisect.bisect_left(d0_ordinals, event.d0.toordinal() - 6)
        hi = bisect.bisect_right(d0_ordinals, event.d0.toordinal())
        crowd = hi - lo - 1
        feature_rows.append(
            {
                "symbol": event.symbol,
                "d0": event.d0.isoformat(),
                "era": era_of(event.d0),
                "pump01": pump01,
                "crowd7d": crowd,
                "d0_bars": int(d0_bar["n_bars"]),
                "funding_events_d0_d7": len(event.funding_ts),
            }
        )
        for entry_day in ENTRY_DAYS:
            entry_bar = event.daily.get(entry_day)
            exit_bar = event.daily.get(EXIT_DAY)
            if entry_bar is None or pump01 is None:
                arm_exclusions["entry_bar_missing"] += 1
                continue
            if exit_bar is None:
                arm_exclusions["exit_bar_missing"] += 1
                continue
            entry_ts = int(entry_bar["last_bar_end_ms"])
            exit_ts = int(exit_bar["last_bar_end_ms"])
            entry_px = entry_bar["close"]
            exit_px = exit_bar["close"]

            tph_entry = entry_bar["turnover"] / max(entry_bar["n_bars"], 1.0)
            tph_d0 = d0_bar["turnover"] / max(d0_bar["n_bars"], 1.0)
            tdecay = tph_entry / tph_d0 if tph_d0 > 0 else float("inf")

            settled_idx = bisect.bisect_right(event.funding_ts, entry_ts)
            if settled_idx == 0:
                fund_label = "fund_none"
                fund8h = None
            else:
                last_rate = event.funding_rate[settled_idx - 1]
                prior = event.funding_ts[:settled_idx]
                if len(prior) >= 2:
                    spacings = np.diff(prior)
                    interval_min = float(np.median(spacings)) / 60000.0
                else:
                    interval_min = 480.0
                fund8h = last_rate * (480.0 / interval_min) if interval_min > 0 else last_rate
                fund_label = bin_label(fund8h, FUND_EDGES, FUND_LABELS)

            btc_i = bisect.bisect_right(btc_ts, entry_ts) - 1
            btc_j = bisect.bisect_right(btc_ts, entry_ts - 720 * MS_PER_HOUR) - 1
            btc30 = (btc_px[btc_i] / btc_px[btc_j] - 1.0) if (btc_i >= 0 and btc_j >= 0) else None

            hold_lo = bisect.bisect_right(event.funding_ts, entry_ts)
            hold_hi = bisect.bisect_right(event.funding_ts, exit_ts)
            hold_rates = event.funding_rate[hold_lo:hold_hi]

            for side in SIDES:
                sign = 1.0 if side == "long" else -1.0
                gross = sign * (exit_px - entry_px) / entry_px
                funding = (-sign) * float(sum(hold_rates))
                trade_rows.append(
                    {
                        "symbol": event.symbol,
                        "d0": event.d0.isoformat(),
                        "era": era_of(event.d0),
                        "week_block": event.d0.toordinal() // 7,
                        "side": side,
                        "entry_day": entry_day,
                        "gross": gross,
                        "funding": funding,
                        "net45": gross + funding - COST_RT,
                        "net90": gross + funding - COST_RT_STRESS,
                        "n_funding_events_hold": len(hold_rates),
                        "pump_cell": bin_label(pump01, PUMP_EDGES, PUMP_LABELS),
                        "tdecay_cell": bin_label(tdecay, TDECAY_EDGES, TDECAY_LABELS),
                        "fund_cell": fund_label,
                        "crowd_cell": bin_label(float(crowd), tuple(float(e) for e in CROWD_EDGES), CROWD_LABELS),
                        "btc_cell": (BTC_LABELS[1] if btc30 >= 0 else BTC_LABELS[0]) if btc30 is not None else "btc_none",
                        "pump01": pump01,
                        "tdecay": tdecay,
                        "fund8h": fund8h,
                        "btc30d": btc30,
                    }
                )

    trades = pl.DataFrame(trade_rows)
    features = pl.DataFrame(feature_rows)
    rng = np.random.default_rng(BOOT_SEED)

    def era_groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {"pooled": rows}
        for era in ("e2122", "e2324", "e2526"):
            groups[era] = [r for r in rows if r["era"] == era]
        groups["pre2025"] = [r for r in rows if r["era"] != "e2526"]
        groups["post2025"] = groups["e2526"]
        return groups

    def emit_tables(cell_key: str | None, cells: list[str] | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for side in SIDES:
            for entry_day in ENTRY_DAYS:
                base = [r for r in trade_rows if r["side"] == side and r["entry_day"] == entry_day]
                cell_values = cells if cells is not None else [None]
                for cell in cell_values:
                    subset = base if cell is None else [r for r in base if r[cell_key] == cell]
                    for era_name, rows in era_groups(subset).items():
                        record: dict[str, Any] = {
                            "side": side,
                            "entry_day": entry_day,
                            "cell": cell or "all",
                            "era": era_name,
                        }
                        if rows:
                            record.update(cell_stats(rows, rng))
                        else:
                            record["n_events"] = 0
                        out.append(record)
        return out

    unconditional = emit_tables(None, None)
    conditioned: list[dict[str, Any]] = []
    for key, labels in (
        ("pump_cell", list(PUMP_LABELS)),
        ("tdecay_cell", list(TDECAY_LABELS)),
        ("fund_cell", [*FUND_LABELS, "fund_none"]),
        ("crowd_cell", list(CROWD_LABELS)),
        ("btc_cell", [*BTC_LABELS, "btc_none"]),
    ):
        for record in emit_tables(key, labels):
            record["feature"] = key
            conditioned.append(record)

    interaction: list[dict[str, Any]] = []
    for pump in PUMP_LABELS:
        for fund in [*FUND_LABELS, "fund_none"]:
            for side in SIDES:
                for entry_day in ENTRY_DAYS:
                    subset = [
                        r
                        for r in trade_rows
                        if r["side"] == side
                        and r["entry_day"] == entry_day
                        and r["pump_cell"] == pump
                        and r["fund_cell"] == fund
                    ]
                    for era_name, rows in era_groups(subset).items():
                        record = {
                            "side": side,
                            "entry_day": entry_day,
                            "pump_cell": pump,
                            "fund_cell": fund,
                            "era": era_name,
                        }
                        if rows:
                            record.update(cell_stats(rows, rng))
                        else:
                            record["n_events"] = 0
                        interaction.append(record)

    outputs = {
        "population.csv": features,
        "trades.csv": trades,
        "arms_unconditional.csv": pl.DataFrame(unconditional),
        "arms_conditioned.csv": pl.DataFrame(conditioned),
        "arms_interaction_pump_x_funding.csv": pl.DataFrame(interaction),
        "exclusions.csv": pl.DataFrame(
            {
                "reason": [*exclusions.keys(), *arm_exclusions.keys()],
                "count": [*(len(v) for v in exclusions.values()), *arm_exclusions.values()],
                "detail": [
                    *((";".join(v[:50]) if v else "") for v in exclusions.values()),
                    *("" for _ in arm_exclusions),
                ],
            }
        ),
    }
    output_paths: dict[str, Path] = {}
    for name, frame in outputs.items():
        path = out_dir / name
        frame.write_csv(path)
        output_paths[name] = path

    common.write_manifest(
        out_dir,
        kind="tl_listing_conditional",
        inputs={
            "venue": args.venue,
            "manifest_partitions": len(partition_dates(root, "archive_trade_manifest")),
            "kline_end": kline_dates[-1].isoformat(),
            "funding_end": funding_dates[-1].isoformat(),
        },
        params={
            "event_floor": EVENT_FLOOR.isoformat(),
            "last_d0": last_d0.isoformat(),
            "gap_quarantine_days": GAP_QUARANTINE_DAYS,
            "entry_days": list(ENTRY_DAYS),
            "exit_day": EXIT_DAY,
            "cost_rt": COST_RT,
            "cost_rt_stress": COST_RT_STRESS,
            "era_bounds": [d.isoformat() for d in ERA_BOUNDS],
            "boot": {"n": BOOT_N, "seed": BOOT_SEED, "cluster": "7d-ordinal-block"},
            "bins": {
                "pump01": list(PUMP_EDGES),
                "tdecay": list(TDECAY_EDGES),
                "fund8h": list(FUND_EDGES),
                "crowd7d": list(CROWD_EDGES),
            },
        },
        output_files=output_paths,
        extra={
            "data_root": str(root),
            "study": "t-l",
            "grading_windows_preserved": ["G1 binance [2021-01-01,2021-05-01)", "G2 binance 2020", "G3 bybit [2021-01-01,2021-05-01)"],
            "n_candidates": len(candidates),
            "n_events": len(events),
            "n_trade_rows": len(trade_rows),
        },
    )
    print(f"wrote {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
