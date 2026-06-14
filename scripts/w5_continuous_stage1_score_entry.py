#!/usr/bin/env python3
"""W5 Stage 1 score-as-entry-priority harness (constant breadth).

Tests whether ordering contending candidates by a priority score — instead of the
control's symbol-alphabetical FCFS — improves risk-adjusted return at the SAME
breadth, on both venues. The engine, gates, capacity heap, cooldown, sizing,
hedge, and rebalance object are byte-identical across arms; only the
within-signal_ts consideration order changes (causal: no cross-ts reordering).

Arms (this run): A0 control (fcfs), A1 composite priority, A5 symbol-hash
negative control. Path-shape arms A2/A3/A4 are gated on Stage 7 and not run here.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage1-score-entry.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage1_score_entry.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --out ~/SHARED_DATA/w5_continuous_stage1_score_entry_2026-06-14
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from liquidity_migration.continuous_events import (  # noqa: E402
    _daily_pnl_metrics,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    build_full_ledger,
    frozen_config_hash,
)
from scripts.w4_continuous_stop_exit_realism import (  # noqa: E402
    COMPONENTS,
    _btc_inputs,
    _component_config,
    _load_component,
    _monthly_returns,
    _pit_partition_gate,
    _write_csv,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
SWEEP_TAG = "w5_continuous_stage1_score_entry_2026-06-14"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage1-score-entry.md"
ARMS = {
    "A0_frozen_control": "fcfs",
    "A1_current_score_priority": "composite",
    "A5_negative_control_priority": "symbol_hash",
}
CONTROL = "A0_frozen_control"
NEG_CONTROL = "A5_negative_control_priority"
BREADTH_TOL = 0.02  # |Δ trade count| per (venue, component) allowed before "disguised filter"
MS_PER_DAY = 86_400_000


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage1_score_entry.py",
        REPO / "scripts" / "w4_continuous_stop_exit_realism.py",
        REPO / "scripts" / "rebuild_winner_base_component_ledgers.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
        REPO / "liquidity_migration" / "trade_lifecycle.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selected_keys(trades: pl.DataFrame) -> set[tuple[str, int]]:
    if trades.is_empty():
        return set()
    return {(str(s), int(t)) for s, t in trades.select("symbol", "entry_signal_ts_ms").iter_rows()}


def _mar_from_monthly(rets: list[float]) -> dict[str, Any]:
    """Monthly-resolution total/annualized/maxDD/MAR (matches r1_robustness conventions)."""
    if not rets:
        return {"total": 0.0, "annualized": 0.0, "max_dd": 0.0, "mar": None, "months": 0}
    eq = 1.0
    peak = 1.0
    maxdd = 0.0
    for r in rets:
        eq *= 1.0 + r
        peak = max(peak, eq)
        if eq / peak - 1.0 < maxdd:
            maxdd = eq / peak - 1.0
    total = eq - 1.0
    years = max(len(rets) / 12.0, 1e-9)
    ann = (eq ** (1.0 / years) - 1.0) if eq > 0 else -1.0
    mar = (ann / abs(maxdd)) if abs(maxdd) > 1e-9 else None
    return {"total": total, "annualized": ann, "max_dd": maxdd, "mar": mar, "months": len(rets)}


def _run_arm(
    venue: str, root: Path, arm: str, entry_order: str, start: str, end: str,
    cost_mult: float = 1.0, tag_suffix: str = "",
) -> dict[str, Any]:
    """Run all 4 components under one arm, build the ensemble-hedged ledger + R1 monthly."""
    arm_dir_name = f"{arm}{tag_suffix}"
    pieces: dict[str, Any] = {}
    per_component: dict[str, Any] = {}
    overrides = {"round_trip_cost_multiplier": cost_mult} if cost_mult != 1.0 else {}
    for component, (_artifact_root, cell) in COMPONENTS.items():
        report_dir = root / "reports" / SWEEP_TAG / arm_dir_name / component
        cfg = _component_config(component, cell, start=start, end=end, arm_overrides=overrides)
        t0 = time.time()
        run_continuous_event_research(root, config=cfg, report_dir=report_dir, entry_order=entry_order)
        comp, trades, _report = _load_component(report_dir)
        pieces[component] = comp
        per_component[component] = {
            "n_trades": int(trades.height),
            "selected_keys": _selected_keys(trades),
            "config_hash": cfg.config_hash(),
            "elapsed_s": round(time.time() - t0, 1),
        }
    all_days = sorted({day for comp in pieces.values() for day in comp.days})
    btc_rets, btc_funding = _btc_inputs(root, venue, all_days)
    ledger = build_full_ledger(pieces, btc_rets, btc_funding)
    metrics = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    arm_dir = root / "reports" / SWEEP_TAG / arm_dir_name
    arm_dir.mkdir(parents=True, exist_ok=True)
    ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
    monthly = _monthly_returns(ledger)
    monthly.write_csv(arm_dir / "volume_event_best_monthly.csv")
    n_trades = sum(c["n_trades"] for c in per_component.values())
    report = {
        "preregistration": PREREG,
        "run_label": "exploratory",
        "arm": arm_dir_name,
        "entry_order": entry_order,
        "cost_multiplier": cost_mult,
        "date_range": {"start": start, "end": end},
        "best_scenario": {
            "total_return": metrics.get("total_return", 0.0),
            "max_drawdown": metrics.get("max_drawdown", 0.0),
            "mar": metrics.get("mar"),
            "trades": n_trades,
        },
        "data_root": str(root),
        "venue": venue,
        "ledger": str(arm_dir / "ensemble_hedged_ledger.csv"),
    }
    (arm_dir / "volume_event_research_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    monthly_map = {str(m): float(r) for m, r in monthly.iter_rows()} if not monthly.is_empty() else {}
    return {
        "arm": arm_dir_name,
        "entry_order": entry_order,
        "cost_multiplier": cost_mult,
        "n_trades": n_trades,
        "metrics": metrics,
        "monthly": monthly_map,
        "per_component": per_component,
        "report_dir": str(arm_dir),
    }


def _replacement_audit(a0: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any]:
    """Per-component selected-set difference vs control (the contention the priority exploits)."""
    out: dict[str, Any] = {"components": {}, "total_arm_only": 0, "total_a0_only": 0, "total_shared": 0}
    for component in COMPONENTS:
        a0_keys = a0["per_component"][component]["selected_keys"]
        arm_keys = arm["per_component"][component]["selected_keys"]
        shared = len(a0_keys & arm_keys)
        arm_only = len(arm_keys - a0_keys)
        a0_only = len(a0_keys - arm_keys)
        n0 = len(a0_keys)
        out["components"][component] = {
            "a0_selected": n0,
            "arm_selected": len(arm_keys),
            "shared": shared,
            "arm_only": arm_only,
            "a0_only": a0_only,
            "replacements": (arm_only + a0_only) // 2,
            "breadth_delta_pct": (len(arm_keys) - n0) / max(n0, 1),
        }
        out["total_arm_only"] += arm_only
        out["total_a0_only"] += a0_only
        out["total_shared"] += shared
    out["total_replacements"] = (out["total_arm_only"] + out["total_a0_only"]) // 2
    out["max_breadth_delta_pct"] = max(
        abs(c["breadth_delta_pct"]) for c in out["components"].values()
    )
    return out


def _pooled_monthly(arm_by_venue: dict[str, dict[str, Any]]) -> list[tuple[str, float]]:
    """Equal-weight two-venue pooled monthly return series (mean across venues present that month)."""
    acc: dict[str, list[float]] = {}
    for venue_arm in arm_by_venue.values():
        for month, r in venue_arm["monthly"].items():
            acc.setdefault(month, []).append(r)
    return [(m, sum(v) / len(v)) for m, v in sorted(acc.items())]


def _thirds_direction(rets: list[float]) -> dict[str, Any]:
    n = len(rets)
    if n < 3:
        return {"thirds": [], "same_direction_count": 0}
    out = []
    for i in range(3):
        seg = rets[i * n // 3:(i + 1) * n // 3]
        cum = 1.0
        for r in seg:
            cum *= 1.0 + r
        out.append(cum - 1.0)
    overall = sum(out)
    direction = overall > 0
    same = sum(1 for x in out if (x > 0) == direction)
    return {"thirds": out, "same_direction_count": same, "overall_positive": direction}


def _block_bootstrap_delta(delta_rets: list[float], *, block: int = 3, reps: int = 2000) -> dict[str, Any]:
    """Deterministic moving-block bootstrap of the per-month (arm - A0) pooled return delta:
    fraction of resamples whose summed delta > 0. Seed is fixed (no Math.random/Date)."""
    import numpy as np

    n = len(delta_rets)
    if n < block:
        return {"p_sum_gt_0": None, "reps": 0}
    arr = np.asarray(delta_rets, dtype=float)
    n_blocks = max(n - block + 1, 1)
    rng = np.random.RandomState(20260614)
    n_draw = (n + block - 1) // block
    wins = 0
    for _ in range(reps):
        starts = rng.randint(0, n_blocks, size=n_draw)
        sample = np.concatenate([arr[s:s + block] for s in starts])[:n]
        if float(sample.sum()) > 0.0:
            wins += 1
    return {"p_sum_gt_0": wins / reps, "reps": reps, "block": block}


def _arm_summary_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for venue, vb in payload["venues"].items():
        if "arms" not in vb:
            continue
        a0 = vb["arms"][CONTROL]["metrics"]
        for arm, ab in vb["arms"].items():
            m = ab["metrics"]
            rows.append(
                {
                    "venue": venue,
                    "arm": arm,
                    "entry_order": ab["entry_order"],
                    "cost_multiplier": ab["cost_multiplier"],
                    "n_trades": ab["n_trades"],
                    "total_return": float(m.get("total_return", 0.0)),
                    "mar": m.get("mar"),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                    "worst_day_return": float(m.get("worst_day_return", 0.0)),
                    "sharpe_like": float(m.get("sharpe_like", 0.0)),
                    "delta_return_vs_a0": float(m.get("total_return", 0.0)) - float(a0.get("total_return", 0.0)),
                    "delta_mar_vs_a0": (
                        None if m.get("mar") is None or a0.get("mar") is None
                        else float(m["mar"]) - float(a0["mar"])
                    ),
                    "total_replacements": ab.get("replacement", {}).get("total_replacements"),
                    "max_breadth_delta_pct": ab.get("replacement", {}).get("max_breadth_delta_pct"),
                }
            )
    return rows


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    requested = [a.strip() for a in args.arms.split(",") if a.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")

    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG,
        "stage": "stage1_score_entry",
        "window": {"start": args.start, "end_exclusive": args.end},
        "out": str(out),
        "sweep_tag": SWEEP_TAG,
        "git_head": _git_head(),
        "code_hash": code_hash,
        "frozen_forward_config_hash": frozen_config_hash(),
        "roots": {v: str(ROOTS[v]) for v in venues},
        "arms_requested": requested,
        "breadth_tolerance": BREADTH_TOL,
        "pit_gate": {},
        "venues": {},
    }

    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        vb: dict[str, Any] = {"arms": {}}
        for arm in requested:
            if arm not in ARMS:
                raise ValueError(f"unknown arm {arm!r}; known: {list(ARMS)}")
            res = _run_arm(venue, root, arm, ARMS[arm], args.start, args.end)
            vb["arms"][arm] = res
        # A0 sanity + replacement audit vs control
        a0 = vb["arms"].get(CONTROL)
        if a0 is not None:
            for arm, ab in vb["arms"].items():
                if arm == CONTROL:
                    continue
                ab["replacement"] = _replacement_audit(a0, ab)
        payload["venues"][venue] = vb

    # ---- A0 reconciliation vs Stage 0 control (sanity: fcfs reproduces Stage 0) ----
    payload["a0_reconcile"] = {}
    for venue in venues:
        vb = payload["venues"].get(venue, {})
        if "arms" not in vb or CONTROL not in vb["arms"]:
            continue
        s0_dir = ROOTS[venue] / "reports" / "w5_continuous_stage0_candidate_tape_2026-06-14"
        s0_led = s0_dir / "ensemble_hedged_ledger.csv"
        rec: dict[str, Any] = {"stage0_ledger": str(s0_led), "available": s0_led.exists()}
        if s0_led.exists():
            s0 = pl.read_csv(s0_led)
            a1 = pl.read_csv(vb["arms"][CONTROL]["report_dir"] + "/ensemble_hedged_ledger.csv")
            rec["rows_match"] = int(s0.height) == int(a1.height)
            if "equity" in s0.columns and "equity" in a1.columns and s0.height == a1.height:
                import math
                diffs = [
                    abs(float(x) - float(y))
                    for x, y in zip(s0["equity"].to_list(), a1["equity"].to_list())
                ]
                rec["max_equity_abs_diff"] = max(diffs) if diffs else 0.0
                rec["equity_allclose_1e-9"] = all(d < 1e-9 or math.isnan(d) for d in diffs)
        payload["a0_reconcile"][venue] = rec

    # ---- pooled metrics + per-arm verdict ----
    eval_arms = [a for a in requested if a != CONTROL]
    pooled: dict[str, Any] = {}
    for arm in [CONTROL] + eval_arms:
        arm_by_venue = {
            v: payload["venues"][v]["arms"][arm]
            for v in venues
            if "arms" in payload["venues"].get(v, {}) and arm in payload["venues"][v]["arms"]
        }
        pm = _pooled_monthly(arm_by_venue)
        pooled[arm] = {"monthly": pm, "mar": _mar_from_monthly([r for _, r in pm])}
    payload["pooled"] = {a: {"mar": pooled[a]["mar"]} for a in pooled}

    a0_pooled_mar = pooled[CONTROL]["mar"]["mar"]
    a5_pooled_mar = pooled.get(NEG_CONTROL, {}).get("mar", {}).get("mar")
    verdicts: dict[str, Any] = {}
    for arm in eval_arms:
        ap = pooled[arm]
        pooled_mar = ap["mar"]["mar"]
        pooled_mar_delta = (
            None if pooled_mar is None or a0_pooled_mar is None else pooled_mar - a0_pooled_mar
        )
        # per-venue return + MAR delta
        venue_rows = []
        per_venue_ok = True
        for v in venues:
            vb = payload["venues"].get(v, {})
            if "arms" not in vb or arm not in vb["arms"]:
                continue
            m = vb["arms"][arm]["metrics"]
            m0 = vb["arms"][CONTROL]["metrics"]
            mar_d = None if m.get("mar") is None or m0.get("mar") is None else float(m["mar"]) - float(m0["mar"])
            ret_pos = float(m.get("total_return", 0.0)) > 0.0
            venue_rows.append(
                {"venue": v, "return": float(m.get("total_return", 0.0)), "return_positive": ret_pos,
                 "mar_delta_vs_a0": mar_d}
            )
            if not ret_pos or (mar_d is not None and mar_d < -0.5):
                per_venue_ok = False
        # breadth + replacement across venues
        max_breadth = max(
            (payload["venues"][v]["arms"][arm]["replacement"]["max_breadth_delta_pct"]
             for v in venues if arm in payload["venues"].get(v, {}).get("arms", {})),
            default=0.0,
        )
        total_repl = sum(
            payload["venues"][v]["arms"][arm]["replacement"]["total_replacements"]
            for v in venues if arm in payload["venues"].get(v, {}).get("arms", {})
        )
        thirds = _thirds_direction([r for _, r in ap["monthly"]])
        # delta vs A0 monthly series for bootstrap
        a0_m = dict(pooled[CONTROL]["monthly"])
        delta_series = [r - a0_m.get(m, 0.0) for m, r in ap["monthly"]]
        boot = _block_bootstrap_delta(delta_series)
        beats_neg = (
            pooled_mar_delta is not None and a5_pooled_mar is not None and a0_pooled_mar is not None
            and (pooled_mar_delta - (a5_pooled_mar - a0_pooled_mar)) >= 0.1
        )
        gates = {
            "return_positive_both_venues": per_venue_ok and len(venue_rows) == len(venues),
            "pooled_mar_delta_gt_0.1": pooled_mar_delta is not None and pooled_mar_delta > 0.1,
            "no_venue_mar_delta_below_-0.5": all(
                (r["mar_delta_vs_a0"] is None or r["mar_delta_vs_a0"] >= -0.5) for r in venue_rows
            ),
            "beats_negative_control_by_0.1": bool(beats_neg) if arm != NEG_CONTROL else None,
            "breadth_within_2pct": max_breadth <= BREADTH_TOL,
            "two_thirds_same_direction": thirds["same_direction_count"] >= 2,
            "has_contention": total_repl > 0,
        }
        advance = arm != NEG_CONTROL and all(
            v for k, v in gates.items() if v is not None and k != "has_contention"
        )
        verdicts[arm] = {
            "pooled_mar": pooled_mar,
            "pooled_mar_delta_vs_a0": pooled_mar_delta,
            "per_venue": venue_rows,
            "max_breadth_delta_pct": max_breadth,
            "total_replacements": total_repl,
            "thirds": thirds,
            "bootstrap": boot,
            "gates": gates,
            "advances_to_stage6": advance,
            "structural_null": total_repl == 0,
        }
    payload["verdicts"] = verdicts

    # ---- conditional 2x-cost stress for any arm clearing the base bar ----
    payload["cost_stress"] = {}
    for arm in eval_arms:
        if not verdicts[arm]["advances_to_stage6"]:
            payload["cost_stress"][arm] = "skipped (did not clear base pass bar)"
            continue
        stress_by_venue = {}
        for venue in venues:
            if "arms" not in payload["venues"].get(venue, {}):
                continue
            res = _run_arm(
                venue, ROOTS[venue], arm, ARMS[arm], args.start, args.end,
                cost_mult=2.0, tag_suffix="_2xcost",
            )
            stress_by_venue[venue] = res
        pm = _pooled_monthly(stress_by_venue)
        stress_mar = _mar_from_monthly([r for _, r in pm])["mar"]
        a0_stress = {}
        for venue in venues:
            if "arms" not in payload["venues"].get(venue, {}):
                continue
            a0_stress[venue] = _run_arm(
                venue, ROOTS[venue], CONTROL, "fcfs", args.start, args.end,
                cost_mult=2.0, tag_suffix="_2xcost",
            )
        a0_pm = _pooled_monthly(a0_stress)
        a0_stress_mar = _mar_from_monthly([r for _, r in a0_pm])["mar"]
        delta = None if stress_mar is None or a0_stress_mar is None else stress_mar - a0_stress_mar
        payload["cost_stress"][arm] = {
            "pooled_mar": stress_mar, "a0_pooled_mar": a0_stress_mar,
            "pooled_mar_delta": delta, "survives": delta is not None and delta > 0.1,
        }

    # ---- overall verdict ----
    advancing = [a for a in eval_arms if verdicts[a]["advances_to_stage6"]
                 and (payload["cost_stress"].get(a) == "skipped (did not clear base pass bar)"
                      or (isinstance(payload["cost_stress"].get(a), dict)
                          and payload["cost_stress"][a].get("survives")))]
    payload["verdict"] = {
        "arms_advancing_to_stage6": advancing,
        "a1_structural_null": verdicts.get("A1_current_score_priority", {}).get("structural_null"),
        "interpretation": (
            f"Arms advancing: {advancing}."
            if advancing
            else "No arm cleared the same-breadth score-entry pass bar on both venues. "
            "Bank as a NULL for this mechanism; the replacement-count diagnostic shows whether the "
            "contention room even exists (structural null) — if so the lever moves to Stage 7/8."
        ),
    }

    _write_csv(out / "stage1_summary.csv", _arm_summary_rows(payload))
    repl_rows = []
    for venue, vb in payload["venues"].items():
        for arm, ab in vb.get("arms", {}).items():
            r = ab.get("replacement")
            if not r:
                continue
            for component, c in r["components"].items():
                repl_rows.append({"venue": venue, "arm": arm, "component": component, **c})
    _write_csv(out / "stage1_replacement_audit.csv", repl_rows)
    (out / "stage1_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage1_summary.md")
    return payload


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W5 Continuous Stage 1 — Score As Entry Priority (constant breadth)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Arms: {payload['arms_requested']}",
        f"- Pre-registration: `{payload['preregistration']}`",
        "",
        "## A0 reconciliation vs Stage 0 control",
        "",
    ]
    for venue, rec in payload.get("a0_reconcile", {}).items():
        lines.append(
            f"- `{venue}`: rows_match={rec.get('rows_match')} "
            f"equity_allclose_1e-9={rec.get('equity_allclose_1e-9')} "
            f"max_equity_abs_diff={rec.get('max_equity_abs_diff')}"
        )
    lines.extend(["", "## Per-arm / per-venue", "",
                  "| Venue | Arm | Trades | Return | MAR | MaxDD | ΔRet vs A0 | ΔMAR vs A0 | Replacements |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for row in _arm_summary_rows(payload):
        lines.append(
            f"| `{row['venue']}` | `{row['arm']}` | {row['n_trades']} | {row['total_return']:.4f} | "
            f"{row['mar']} | {row['max_drawdown']:.4f} | {row['delta_return_vs_a0']:.4f} | "
            f"{row['delta_mar_vs_a0']} | {row['total_replacements']} |"
        )
    lines.extend(["", "## Pooled verdict", ""])
    for arm, vd in payload.get("verdicts", {}).items():
        lines.append(f"### `{arm}`")
        lines.append("")
        lines.append(f"- pooled MAR delta vs A0: `{vd['pooled_mar_delta_vs_a0']}`")
        lines.append(f"- total replacements vs A0: `{vd['total_replacements']}` "
                     f"(structural_null={vd['structural_null']})")
        lines.append(f"- max breadth Δ%: `{vd['max_breadth_delta_pct']:.4f}`")
        lines.append(f"- thirds same-direction: `{vd['thirds']['same_direction_count']}/3`")
        lines.append(f"- bootstrap P(Δ>0): `{vd['bootstrap'].get('p_sum_gt_0')}`")
        lines.append(f"- gates: `{vd['gates']}`")
        lines.append(f"- advances to Stage 6: **{vd['advances_to_stage6']}**")
        lines.append("")
    lines.extend(["## Overall", "", payload["verdict"]["interpretation"], "",
                  "Stage 1 changes only within-cycle priority at constant breadth. Path-shape arms "
                  "A2/A3/A4 are gated on Stage 7. Not promotion evidence.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument(
        "--arms",
        default="A0_frozen_control,A1_current_score_priority,A5_negative_control_priority",
    )
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict": payload["verdict"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
