#!/usr/bin/env python3
"""R3b Lane-1: correlated-cluster caps — cluster structure + cap replay.

Tail-risk program P1.3 (`docs/tail_risk_program.md`): the 2026-06-20
disaster-stop study's own unbuilt recommendation ("correlated-squeeze cap").
Exploratory Lane-1 on the SEEN T-A render gate_on book (deployed CONTINUOUS
shape; all entries are shorts, so same-direction is structural).

Cluster method (crude by design, documented here):
  at each recorded entry, the candidate's trailing 720h hourly log-return
  Pearson correlation against every position OPEN at that instant
  (entry_ts <= t < exit_ts of the same book), computed from the render 1h
  kline cache with >= 240 overlapping bars; pairs with less overlap do not
  count toward the cluster (young listings are un-clusterable and stated
  as such).

Cap rule replayed: an entry is VETOED when the count of open positions with
rho >= RHO_MIN already equals/exceeds K (i.e. the cap allows at most K
correlated same-direction positions; the K+1-th is refused).

Declared cells: rho_min ∈ {0.6, 0.7} × K ∈ {2, 3} — the REGISTERED cell is
**rho_min = 0.7, K = 3** (chosen from the deployed book's max_active=25 and
the study's squeeze framing before results were computed); flankers are
sensitivity cells, reported always, never used to reselect.

Grading: insurance metrics (item 27) — veto rate, vetoed entries' subsequent
net (forgone upside next to avoided loss), interaction with native tail days
(reference-book day net <= -1%) and the registered V2 tail set, era-split.
NOT return improvement.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/r3b_cluster_caps_lane1.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from bisect import bisect_left
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_HOUR  # noqa: E402
from scripts.research_v3 import common, v4_shared  # noqa: E402
from scripts.research_v3.ta_gate_ablation_report import NAMED_TAIL_DATE, common_loss_dates  # noqa: E402

RHO_WINDOW_HOURS = 720
RHO_MIN_OVERLAP = 240
CELLS: tuple[tuple[float, int], ...] = ((0.6, 2), (0.6, 3), (0.7, 2), (0.7, 3))
REGISTERED_CELL: tuple[float, int] = (0.7, 3)
REFERENCE_TAIL_DAY_RETURN = -0.01


def hourly_log_returns(klines: pl.DataFrame) -> dict[str, tuple[list[int], list[float]]]:
    """symbol -> (bar_end_ts list, log return list), sorted by ts."""
    out: dict[str, tuple[list[int], list[float]]] = {}
    for key, part in klines.sort(["symbol", "bar_end_ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        ends = [int(v) for v in part["bar_end_ts_ms"].to_list()]
        closes = [float(v) for v in part["close"].to_list()]
        rets: list[float] = []
        ret_ends: list[int] = []
        for i in range(1, len(ends)):
            if closes[i - 1] > 0 and closes[i] > 0 and ends[i] - ends[i - 1] == MS_PER_HOUR:
                ret_ends.append(ends[i])
                rets.append(math.log(closes[i] / closes[i - 1]))
        out[symbol] = (ret_ends, rets)
    return out


def trailing_corr(
    a: tuple[list[int], list[float]],
    b: tuple[list[int], list[float]],
    *,
    at_ts: int,
    window_hours: int = RHO_WINDOW_HOURS,
    min_overlap: int = RHO_MIN_OVERLAP,
) -> float | None:
    """Pearson rho of the two symbols' hourly log returns over (at_ts - window, at_ts]."""
    lo_ts = at_ts - window_hours * MS_PER_HOUR
    a_ends, a_rets = a
    b_ends, b_rets = b
    ai = bisect_left(a_ends, lo_ts + 1)
    aj = bisect_left(a_ends, at_ts + 1)
    a_map = {a_ends[i]: a_rets[i] for i in range(ai, aj)}
    bi = bisect_left(b_ends, lo_ts + 1)
    bj = bisect_left(b_ends, at_ts + 1)
    xs: list[float] = []
    ys: list[float] = []
    for i in range(bi, bj):
        val = a_map.get(b_ends[i])
        if val is not None:
            xs.append(val)
            ys.append(b_rets[i])
    n = len(xs)
    if n < min_overlap:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    parser.add_argument(
        "--book",
        default="render_gate_on",
        choices=("render_gate_on", "barebones"),
        help="render_gate_on = declared surface; barebones = labelled supplementary"
        " stacking surface (the disaster-stop study's own book shape)",
    )
    args = parser.parse_args()

    suffix = "" if args.book == "render_gate_on" else f"-{args.book}"
    out_dir = REPO / "reports" / "tail-risk-program" / f"p13-r3b-cluster-caps-lane1-{args.out_date}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)

    registered_tail = set(common_loss_dates()) | {NAMED_TAIL_DATE}
    if args.book == "render_gate_on":
        book = v4_shared.load_render_book("gate_on").sort("entry_ts_ms")
        render_klines, render_klines_sha = v4_shared.render_kline_cache(args.data_root)
        render_funding, render_funding_sha = v4_shared.render_funding_cache(args.data_root)
        midpoint_ms = v4_shared.render_era_midpoint_ms(book)
    else:
        common.verify_v2_inputs()
        shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
        book = (
            common.load_ledger("continuous")
            .with_columns(pl.lit("merged").alias("component"))
            .sort("entry_ts_ms")
        )
        render_klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
        if "bar_end_ts_ms" not in render_klines.columns:
            render_klines = render_klines.with_columns(
                (pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms")
            )
        render_funding = pl.read_parquet(shared_dir / "funding_events.parquet")
        render_klines_sha = common.sha256_file(shared_dir / "kline_slice_1h.parquet")
        render_funding_sha = common.sha256_file(shared_dir / "funding_events.parquet")
        midpoint_ms = common.era_midpoint_ts_ms(book)
    returns = hourly_log_returns(render_klines)

    # reference daily curve for native tail days
    ref = book.with_columns(pl.lit("book").alias("sleeve"))
    series = common.funding_series_by_symbol(render_funding)
    bars = common.close_series_by_symbol(render_klines)
    start_day = common.utc_day_ms(int(book["entry_ts_ms"].min()))
    end_day = common.utc_day_ms(int(book["exit_ts_ms"].max()))
    curve = common.daily_curve(
        common.trade_daily_contributions(ref, series, bars),
        sleeve="book", start_day_ms=start_day, end_day_ms=end_day,
    )
    native_tail_days = set(
        curve.filter(pl.col("net_return") <= REFERENCE_TAIL_DAY_RETURN)["date"].to_list()
    )

    # entry-time correlated-open counts (unique per (trade path); component rows share them)
    rows = book.select(
        "trade_id", "component", "symbol", "entry_ts_ms", "exit_ts_ms", "net_return", "entry_date"
    ).rows(named=True)
    entry_records: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []  # sweep by entry order; drop exited
    pair_cache: dict[tuple[str, str, int], float | None] = {}
    for row in rows:
        ts = int(row["entry_ts_ms"])
        active = [t for t in active if int(t["exit_ts_ms"]) > ts]
        sym = str(row["symbol"])
        sym_ret = returns.get(sym)
        rho_values: list[float] = []
        uncorrelatable = 0
        open_syms = {str(t["symbol"]) for t in active if str(t["symbol"]) != sym}
        for other in sorted(open_syms):
            other_ret = returns.get(other)
            if sym_ret is None or other_ret is None:
                uncorrelatable += 1
                continue
            key = (min(sym, other), max(sym, other), ts // MS_PER_HOUR)
            if key in pair_cache:
                rho = pair_cache[key]
            else:
                rho = trailing_corr(sym_ret, other_ret, at_ts=ts)
                pair_cache[key] = rho
            if rho is None:
                uncorrelatable += 1
            else:
                rho_values.append(rho)
        entry_records.append(
            {
                **{k: row[k] for k in ("trade_id", "component", "symbol", "entry_ts_ms", "net_return", "entry_date")},
                "n_open": len(open_syms),
                "n_uncorrelatable": uncorrelatable,
                "rho_values": rho_values,
            }
        )
        active.append(row)

    # cap replay per declared cell
    grid_rows: list[dict[str, Any]] = []
    detail: dict[str, Any] = {}
    for rho_min, cap_k in CELLS:
        vetoed: list[dict[str, Any]] = []
        for rec in entry_records:
            corr_count = sum(1 for rho in rec["rho_values"] if rho >= rho_min)
            if corr_count >= cap_k:
                vetoed.append({**rec, "corr_count": corr_count})
        for era in ("full", "early", "late"):
            if era == "early":
                sel = [v for v in vetoed if int(v["entry_ts_ms"]) < midpoint_ms]
                pool = [r for r in entry_records if int(r["entry_ts_ms"]) < midpoint_ms]
            elif era == "late":
                sel = [v for v in vetoed if int(v["entry_ts_ms"]) >= midpoint_ms]
                pool = [r for r in entry_records if int(r["entry_ts_ms"]) >= midpoint_ms]
            else:
                sel, pool = vetoed, entry_records
            forgone = sum(max(float(v["net_return"]), 0.0) for v in sel)
            avoided = sum(min(float(v["net_return"]), 0.0) for v in sel)
            on_native_tail = [v for v in sel if v["entry_date"] in native_tail_days]
            on_registered_tail = [v for v in sel if v["entry_date"] in registered_tail]
            grid_rows.append(
                {
                    "rho_min": rho_min,
                    "cap_k": cap_k,
                    "registered": (rho_min, cap_k) == REGISTERED_CELL,
                    "era": era,
                    "entries": len(pool),
                    "vetoed": len(sel),
                    "veto_rate": len(sel) / len(pool) if pool else None,
                    "forgone_upside": forgone,
                    "avoided_loss": avoided,
                    "vetoed_net_sum": forgone + avoided,
                    "vetoed_on_native_tail_days": len(on_native_tail),
                    "vetoed_net_on_native_tail_days": sum(float(v["net_return"]) for v in on_native_tail),
                    "vetoed_on_registered_tail_days": len(on_registered_tail),
                    "vetoed_net_on_registered_tail_days": sum(float(v["net_return"]) for v in on_registered_tail),
                }
            )
        detail[f"rho{rho_min}_k{cap_k}"] = [
            {k: v for k, v in item.items() if k != "rho_values"} for item in vetoed
        ]

    # cluster-structure descriptives at entry times
    counts_07 = [sum(1 for rho in r["rho_values"] if rho >= 0.7) for r in entry_records]
    counts_06 = [sum(1 for rho in r["rho_values"] if rho >= 0.6) for r in entry_records]
    structure = {
        "entries": len(entry_records),
        "mean_open_at_entry": sum(r["n_open"] for r in entry_records) / max(len(entry_records), 1),
        "share_uncorrelatable_pairs": (
            sum(r["n_uncorrelatable"] for r in entry_records)
            / max(sum(r["n_open"] for r in entry_records), 1)
        ),
        "corr_count_hist_rho07": {str(k): counts_07.count(k) for k in sorted(set(counts_07))},
        "corr_count_hist_rho06": {str(k): counts_06.count(k) for k in sorted(set(counts_06))},
        "native_tail_days": len(native_tail_days),
    }

    grid = pl.from_dicts(grid_rows, infer_schema_length=None)
    grid_path = out_dir / "r3b_grid.csv"
    grid.write_csv(grid_path)
    detail_path = out_dir / "r3b_vetoed_entries.json"
    detail_path.write_text(json.dumps(detail, indent=1, default=str), encoding="utf-8")
    structure_path = out_dir / "r3b_cluster_structure.json"
    structure_path.write_text(json.dumps(structure, indent=1), encoding="utf-8")
    print(json.dumps(structure), flush=True)
    print(grid.filter(pl.col("registered")).write_json(), flush=True)

    common.write_manifest(
        out_dir,
        kind="tail_risk_p13_r3b_cluster_caps_lane1",
        inputs={
            "render_caches": {
                "render_kline_slice_1h.parquet": render_klines_sha,
                "render_funding_events.parquet": render_funding_sha,
            },
        },
        params={
            "cluster_method": f"trailing {RHO_WINDOW_HOURS}h hourly log-return Pearson rho vs open"
            f" positions at entry; >= {RHO_MIN_OVERLAP} overlapping bars required",
            "cells": [{"rho_min": r, "cap_k": k} for r, k in CELLS],
            "registered_cell": {"rho_min": REGISTERED_CELL[0], "cap_k": REGISTERED_CELL[1]},
            "grading": "insurance metrics (item 27); era-split; tail-day interaction",
            "surface": (
                "T-A render gate_on book (deployed CONTINUOUS shape; shorts only) [DECLARED]"
                if args.book == "render_gate_on"
                else "V2 barebones CONTINUOUS ledger [LABELLED SUPPLEMENTARY: stacking surface]"
            ),
        },
        output_files={
            "r3b_grid.csv": grid_path,
            "r3b_vetoed_entries.json": detail_path,
            "r3b_cluster_structure.json": structure_path,
        },
        extra={"explicit_non_conclusions": [
            "exploratory Lane-1 on a seen surface; no deployment implication",
            "component rows share entry decisions; veto counterfactual removes whole rows",
            "no capacity backfill: a vetoed slot is left empty, not refilled",
            "young listings (insufficient overlap) cannot join a cluster and are counted separately",
        ]},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
