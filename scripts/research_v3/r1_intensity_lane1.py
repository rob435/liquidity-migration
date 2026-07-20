#!/usr/bin/env python3
"""R1 Lane-1: continuous risk intensity vs the deployed binary gate + 0.35 overlay.

Tail-risk program P1.1 (`docs/tail_risk_program.md`). Exploratory Lane-1
counterfactual on two SEEN surfaces — the V2 CONTINUOUS barebones ledger and
the T-A paired render books (Gen-4/5/6 surface; the reserved V2 label-level
holdout partition is NOT touched). Descends from the T-I family (its linear
member is the ancestor): a sixth-generation read on the trend axis; the
risk-score ramp axis is the monotonization of the deployed
``CTRL_BTC_RISK_70_90_35`` band.

Declared grid — SEVEN members, frozen before any result was computed, all
cells reported, era-split, costs next to gross. **MAR is banned** (T-I died
on MAR-at-negative-net; the program grades tails):

  weight(trade) = m_trend(btc_trend_30d at signal day) x m_risk(btc_risk_score)

  m_trend:  binary(t)   = 1 if t > 0 else 0          (deployed gate; None -> 0)
            linear10(t) = clip(t / 0.10, 0, 1)       (T-I linear ancestor; None -> 0)
  m_risk:   none(s)       = 1
            discrete35(s) = 0.35 if (warm and 0.70 <= s < 0.90) else 1.0   (deployed)
            ramp(s)       = 1 above warm-up: 1.0 at s <= 0.70, linear down to
                            0.35 at s >= 0.90 (monotone, continuous, floor 0.35)

  members: baseline_ungated, binary, binary_discrete35 (deployed shape),
           binary_ramp, linear10, linear10_discrete35, linear10_ramp (candidate)

Metrics per member x era x surface: net incl. costs+funding, gross, cost,
funding, max drawdown, ES95/ES99 of daily book net (mean of the worst 5% / 1%
days), worst day, and two frozen common-loss-tail-day definitions: (i) the
registered V2 common-loss set (T-A: 156 dates + 2024-08-06) intersected with
the surface window; (ii) window-native: days where the surface's own
un-intervened reference book (ungated / gate_off) lost <= -1% — plus forgone
upside next to avoided loss for the intervention delta.

BTC-risk score: causal from-origin replay of ``ExpandingBtcRiskState`` over
signal days (one decision per day, day order, warm-up < 50 prior days) from
``btc_context_by_day`` on BTCUSDT 1h closes. This is the registered live
scoring logic replayed from data origin — NOT the live state file's exact
percentile history (which began at its own deployment); stated in the card.

Run through the POSIX shim (imports runtime modules):
  .venv\\Scripts\\python.exe scripts/research_v3/run_with_stub.py \\
      scripts/research_v3/r1_intensity_lane1.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY  # noqa: E402
from scripts.research_v3 import common, v4_shared  # noqa: E402
from scripts.research_v3.ta_gate_ablation_report import NAMED_TAIL_DATE, common_loss_dates  # noqa: E402

BTC_LOOKBACK_DAYS = 30
LINEAR_SCALE = 0.10
RISK_LOW = 0.70
RISK_HIGH = 0.90
RISK_TAIL_MULT = 0.35
BTC_KLINE_START = dt.date(2021, 3, 1)
BTC_KLINE_END_EXCLUSIVE = dt.date(2026, 7, 10)
REFERENCE_TAIL_DAY_RETURN = -0.01

MEMBERS: tuple[str, ...] = (
    "baseline_ungated",
    "binary",
    "binary_discrete35",
    "binary_ramp",
    "linear10",
    "linear10_discrete35",
    "linear10_ramp",
)


def m_trend(kind: str, trend: float | None) -> float:
    if kind == "none":
        return 1.0
    if trend is None:
        return 0.0  # fail closed, deployed None-blocks-entry semantics
    if kind == "binary":
        return 1.0 if trend > 0.0 else 0.0
    if kind == "linear10":
        return min(1.0, max(0.0, trend / LINEAR_SCALE))
    raise ValueError(f"unknown trend member {kind}")


def m_risk(kind: str, score: float | None, warmup: bool) -> float:
    if kind == "none":
        return 1.0
    if score is None or warmup:
        return 1.0  # deployed warm-up semantics: overlay inactive
    if kind == "discrete35":
        return RISK_TAIL_MULT if RISK_LOW <= score < RISK_HIGH else 1.0
    if kind == "ramp":
        if score <= RISK_LOW:
            return 1.0
        if score >= RISK_HIGH:
            return RISK_TAIL_MULT
        return 1.0 - (score - RISK_LOW) / (RISK_HIGH - RISK_LOW) * (1.0 - RISK_TAIL_MULT)
    raise ValueError(f"unknown risk member {kind}")


MEMBER_PARTS: dict[str, tuple[str, str]] = {
    "baseline_ungated": ("none", "none"),
    "binary": ("binary", "none"),
    "binary_discrete35": ("binary", "discrete35"),
    "binary_ramp": ("binary", "ramp"),
    "linear10": ("linear10", "none"),
    "linear10_discrete35": ("linear10", "discrete35"),
    "linear10_ramp": ("linear10", "ramp"),
}


def member_weight(member: str, trend: float | None, score: float | None, warmup: bool) -> float:
    trend_kind, risk_kind = MEMBER_PARTS[member]
    return m_trend(trend_kind, trend) * m_risk(risk_kind, score, warmup)


def es_levels(daily_net: list[float]) -> dict[str, float | None]:
    """ES95/ES99 of daily book net: mean of the worst 5% / 1% of days."""
    if not daily_net:
        return {"es95_daily": None, "es99_daily": None}
    ordered = sorted(daily_net)
    out: dict[str, float | None] = {}
    for label, alpha in (("es95_daily", 0.05), ("es99_daily", 0.01)):
        k = max(1, int(len(ordered) * alpha))
        out[label] = sum(ordered[:k]) / k
    return out


def replay_btc_risk_by_day(btc_klines: pl.DataFrame) -> dict[int, tuple[float | None, bool]]:
    """Causal from-origin replay: day -> (btc_risk_score, score_warmup)."""
    from liquidity_migration.continuous_btc_risk import (
        BTC_RISK_COMPONENTS,
        ExpandingBtcRiskState,
        btc_context_by_day,
    )

    context = btc_context_by_day(btc_klines)
    state = ExpandingBtcRiskState()
    out: dict[int, tuple[float | None, bool]] = {}
    for day in sorted(context):
        raw = {name: context[day].get(name) for name in BTC_RISK_COMPONENTS}
        if all(value is None for value in raw.values()):
            out[day] = (None, True)
            continue
        score = state.score(decision_key=f"lane1-{day}", raw_values=raw)
        out[day] = (float(score["btc_risk_score"]), bool(score["score_warmup"]))
    return out


def curve_tail_metrics(
    curve: pl.DataFrame,
    reference_curve: pl.DataFrame,
    registered_tail_dates: set[str],
) -> dict[str, Any]:
    daily = curve["net_return"].to_list()
    stats = es_levels(daily)
    registered = curve.filter(pl.col("date").is_in(sorted(registered_tail_dates)))
    reference_tail_days = set(
        reference_curve.filter(pl.col("net_return") <= REFERENCE_TAIL_DAY_RETURN)["date"].to_list()
    )
    native = curve.filter(pl.col("date").is_in(sorted(reference_tail_days)))
    return {
        **stats,
        "registered_tail_days_in_window": registered.height,
        "registered_tail_negative_days": registered.filter(pl.col("net_return") < 0).height,
        "registered_tail_sum_return": float(registered["net_return"].sum()) if registered.height else 0.0,
        "native_tail_days": len(reference_tail_days),
        "native_tail_sum_return": float(native["net_return"].sum()) if native.height else 0.0,
        "native_tail_worst": float(native["net_return"].min()) if native.height else None,
    }


def surface_rows(
    surface: str,
    panel: pl.DataFrame,
    series: common.FundingSeries,
    bars: common.BarSeries,
    *,
    midpoint: int,
    start_day: int,
    end_day: int,
    registered_tail_dates: set[str],
    members: tuple[str, ...] = MEMBERS,
) -> list[dict[str, Any]]:
    """All member x era rows for one surface. ``panel`` carries btc_trend_30d,
    btc_risk_score, btc_risk_warmup per trade."""
    rows: list[dict[str, Any]] = []
    reference_curves: dict[str, pl.DataFrame] = {}

    member_curves: dict[str, pl.DataFrame] = {}
    member_metrics: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        weights = [
            member_weight(member, t, s, bool(w))
            for t, s, w in zip(
                panel["btc_trend_30d"].to_list(),
                panel["btc_risk_score"].to_list(),
                panel["btc_risk_warmup"].to_list(),
            )
        ]
        cell_panel = panel.with_columns(pl.Series("weight_factor", weights, dtype=pl.Float64))
        member_metrics[member] = v4_shared.weighted_cell_metrics(
            cell_panel, series, bars, midpoint_ts_ms=midpoint, start_day_ms=start_day, end_day_ms=end_day
        )
        modified = v4_shared.apply_weight_factor(cell_panel)
        contributions = common.trade_daily_contributions(modified, series, bars)
        member_curves[member] = common.daily_curve(
            contributions, sleeve="continuous", start_day_ms=start_day, end_day_ms=end_day
        )
    reference_curves["full"] = member_curves["baseline_ungated"]

    for member in members:
        curve = member_curves[member]
        for era, era_metrics in zip(("full", "early", "late"), member_metrics[member]):
            if era == "early":
                era_curve = curve.filter(pl.col("day_ms") < midpoint)
                ref_curve = reference_curves["full"].filter(pl.col("day_ms") < midpoint)
            elif era == "late":
                era_curve = curve.filter(pl.col("day_ms") >= midpoint)
                ref_curve = reference_curves["full"].filter(pl.col("day_ms") >= midpoint)
            else:
                era_curve = curve
                ref_curve = reference_curves["full"]
            tail = curve_tail_metrics(era_curve, ref_curve, registered_tail_dates)
            rows.append({"surface": surface, "member": member, "era": era, **era_metrics, **tail})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = REPO / "reports" / "tail-risk-program" / f"p11-r1-intensity-lane1-{args.out_date}"
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    registered_tail = set(common_loss_dates()) | {NAMED_TAIL_DATE}

    btc_klines = common.read_kline_slice(
        args.data_root, start=BTC_KLINE_START, end_exclusive=BTC_KLINE_END_EXCLUSIVE, symbols={"BTCUSDT"}
    )
    from liquidity_migration.continuous_events import _btc_trend_returns

    trend_lookup = _btc_trend_returns(btc_klines, lookback_days=BTC_LOOKBACK_DAYS)
    risk_lookup = replay_btc_risk_by_day(btc_klines)
    print(f"trend days={len(trend_lookup)} risk days={len(risk_lookup)}", flush=True)

    def attach(panel: pl.DataFrame) -> pl.DataFrame:
        if "sleeve" not in panel.columns:  # render CSVs are continuous-only
            panel = panel.with_columns(pl.lit("continuous").alias("sleeve"))
        signal_days = [
            (int(t) // MS_PER_DAY) * MS_PER_DAY for t in panel["entry_signal_ts_ms"].to_list()
        ]
        trends = [trend_lookup.get(day) for day in signal_days]
        scores = [risk_lookup.get(day, (None, True))[0] for day in signal_days]
        warm = [bool(risk_lookup.get(day, (None, True))[1]) for day in signal_days]
        return panel.with_columns(
            pl.Series("btc_trend_30d", trends, dtype=pl.Float64),
            pl.Series("btc_risk_score", scores, dtype=pl.Float64),
            pl.Series("btc_risk_warmup", warm, dtype=pl.Boolean),
        )

    grid_rows: list[dict[str, Any]] = []

    # ---- surface A: V2 barebones CONTINUOUS ledger -------------------------
    ledger = common.load_ledger("continuous")
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)
    common.crosscheck_ledger_funding(ledger, series)
    panel_a = attach(ledger)
    grid_rows += surface_rows(
        "barebones",
        panel_a,
        series,
        bars,
        midpoint=common.era_midpoint_ts_ms(ledger),
        start_day=common.utc_day_ms(int(ledger["entry_ts_ms"].min())),
        end_day=common.utc_day_ms(int(ledger["exit_ts_ms"].max())),
        registered_tail_dates=registered_tail,
    )
    print("surface done: barebones", flush=True)

    # ---- surface B: T-A render books (gate_off reference, gate_on comparator)
    gate_off = v4_shared.load_render_book("gate_off")
    gate_on = v4_shared.load_render_book("gate_on")
    render_klines, render_klines_sha = v4_shared.render_kline_cache(args.data_root)
    render_funding, render_funding_sha = v4_shared.render_funding_cache(args.data_root)
    render_series = common.funding_series_by_symbol(render_funding)
    render_bars = common.close_series_by_symbol(render_klines)
    panel_b = attach(gate_off)
    midpoint_b = v4_shared.render_era_midpoint_ms(gate_off)
    start_b = common.utc_day_ms(int(gate_off["entry_ts_ms"].min()))
    end_b = common.utc_day_ms(int(gate_off["exit_ts_ms"].max()))
    grid_rows += surface_rows(
        "render_gate_off",
        panel_b,
        render_series,
        render_bars,
        midpoint=midpoint_b,
        start_day=start_b,
        end_day=end_b,
        registered_tail_dates=registered_tail,
    )
    print("surface done: render_gate_off", flush=True)

    # rendered gate_on comparator (binary gate with real capacity interactions):
    # only the as-is cell is meaningful — re-gating an already-gated book is not
    panel_on = attach(gate_on)
    grid_rows += surface_rows(
        "render_gate_on_asis",
        panel_on,
        render_series,
        render_bars,
        midpoint=midpoint_b,
        start_day=start_b,
        end_day=end_b,
        registered_tail_dates=registered_tail,
        members=("baseline_ungated",),
    )
    print("surface done: render_gate_on_asis", flush=True)

    grid = pl.from_dicts(grid_rows, infer_schema_length=None)
    grid_path = out_dir / "r1_grid.csv"
    grid.write_csv(grid_path)

    # capacity-interaction consistency check: gate_off x binary vs rendered gate_on
    check = {
        "gate_off_binary_full_net": float(
            grid.filter(
                (pl.col("surface") == "render_gate_off")
                & (pl.col("member") == "binary")
                & (pl.col("era") == "full")
            )["net_return"][0]
        ),
        "rendered_gate_on_full_net": float(
            grid.filter(
                (pl.col("surface") == "render_gate_on_asis")
                & (pl.col("member") == "baseline_ungated")
                & (pl.col("era") == "full")
            )["net_return"][0]
        ),
    }
    check["capacity_interaction_delta"] = (
        check["gate_off_binary_full_net"] - check["rendered_gate_on_full_net"]
    )
    print(json.dumps(check), flush=True)

    common.write_manifest(
        out_dir,
        kind="tail_risk_p11_r1_intensity_lane1",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "render_caches": {
                "render_kline_slice_1h.parquet": render_klines_sha,
                "render_funding_events.parquet": render_funding_sha,
            },
            "btc_kline_window": [BTC_KLINE_START.isoformat(), BTC_KLINE_END_EXCLUSIVE.isoformat()],
        },
        params={
            "members": {m: MEMBER_PARTS[m] for m in MEMBERS},
            "m_trend": {"binary": "1 iff trend>0 (deployed; None->0)", "linear10": "clip(trend/0.10,0,1) (T-I ancestor)"},
            "m_risk": {
                "discrete35": "0.35 iff warm and 0.70<=s<0.90 else 1.0 (deployed CTRL_BTC_RISK_70_90_35)",
                "ramp": "monotone: 1.0 at s<=0.70 -> 0.35 at s>=0.90, floor 0.35; warm-up -> 1.0",
            },
            "score_replay": "ExpandingBtcRiskState from-origin causal replay, one decision per day"
            " (NOT the live state file's own percentile history)",
            "metrics": "net/gross/cost/funding, maxDD, ES95/ES99 daily, worst day,"
            " registered V2 tail set (156+1 dates) + window-native reference<=-1% tail days;"
            " MAR banned",
            "surfaces": {
                "barebones": "V2 CONT ledger, weights applied per trade at signal day",
                "render_gate_off": "T-A gate-off book re-weighted per member (reference book)",
                "render_gate_on_asis": "T-A gate-on book as rendered (real capacity interactions)",
            },
            "capacity_interaction_check": check,
        },
        output_files={"r1_grid.csv": grid_path},
        extra={"explicit_non_conclusions": [
            "exploratory Lane-1 on seen surfaces; no alpha or promotion claim",
            "ledger-level weight scaling; capacity/admission/hedge interactions not re-solved"
            " (the gate_off x binary vs rendered gate_on delta quantifies this)",
            "from-origin score replay differs from the live overlay's own warm-up history",
            "the reserved V2 label-level holdout partition was not read",
        ]},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
