#!/usr/bin/env python3
"""W5 Stage 3 - Exit lifecycle alpha (MFE-giveback trailing gain-lock).

For the SAME frozen entries, adds the causal MFE-giveback trail (exit when a winning
fade gives back half its peak favorable excursion) and asks whether cutting the
gains-that-revert tail improves pooled MAR vs the frozen fixed-TP/max-hold control on
both venues — without repeating the W4-closed stop/failed-fade/breakeven overlay.

Exits change the trade population, so each arm is a full engine re-run of the four
frozen components per venue (config override via _component_config(arm_overrides=...)),
then the frozen ensemble/hedge rebuild.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage3-exit-alpha.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage3_exit_alpha.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --out ~/SHARED_DATA/w5_continuous_stage3_exit_alpha_2026-06-14
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
from collections import Counter
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
SWEEP_TAG = "w5_continuous_stage3_exit_alpha_2026-06-14"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage3-exit-alpha.md"
GIVEBACK = {"mfe_giveback_trigger_pct": 0.05, "mfe_giveback_retain_pct": 0.50}
ARM_OVERRIDES = {
    "X0_control": {},
    "XT_mfe_giveback": dict(GIVEBACK),
    "XT_mfe_giveback_2xcost": {**GIVEBACK, "round_trip_cost_multiplier": 2.0},
    "X5_hash_exit": {"hash_exit_prob": 0.004},
}


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
        REPO / "scripts" / "w5_continuous_stage3_exit_alpha.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "trade_lifecycle.py",
        REPO / "liquidity_migration" / "config.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_arm(root: Path, venue: str, arm: str, start: str, end: str) -> dict[str, Any]:
    pieces: dict[str, Any] = {}
    trades_all: list[pl.DataFrame] = []
    cfg_hashes: dict[str, str] = {}
    t0 = time.time()
    for component, (_artifact_root, cell) in COMPONENTS.items():
        report_dir = root / "reports" / SWEEP_TAG / arm / component
        cfg = _component_config(component, cell, start=start, end=end, arm_overrides=ARM_OVERRIDES[arm])
        cfg_hashes[component] = cfg.config_hash()
        run_continuous_event_research(root, config=cfg, report_dir=report_dir)
        comp, trades, _rep = _load_component(report_dir)
        pieces[component] = comp
        trades_all.append(trades)
    all_days = sorted({d for c in pieces.values() for d in c.days})
    btc_rets, btc_fund = _btc_inputs(root, venue, all_days)
    ledger = build_full_ledger(pieces, btc_rets, btc_fund)
    arm_dir = root / "reports" / SWEEP_TAG / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
    monthly = _monthly_returns(ledger)
    monthly.write_csv(arm_dir / "volume_event_best_monthly.csv")
    m = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    trades = pl.concat(trades_all) if trades_all else pl.DataFrame()
    n_entries = int(trades.height)
    avg_hold = float(trades["hold_hours"].mean()) if n_entries and "hold_hours" in trades.columns else None
    reasons = dict(Counter(str(r) for r in trades["exit_reason"].to_list())) if n_entries and "exit_reason" in trades.columns else {}
    (arm_dir / "volume_event_research_report.json").write_text(json.dumps({
        "preregistration": PREREG, "run_label": "exploratory", "arm": arm, "venue": venue,
        "best_scenario": {"total_return": m.get("total_return"), "max_drawdown": m.get("max_drawdown"),
                          "mar": m.get("mar"), "sharpe_like": m.get("sharpe_like"), "trades": n_entries},
        "data_root": str(root)}, indent=2, default=str), encoding="utf-8")
    return {"arm": arm, "venue": venue, "elapsed_s": round(time.time() - t0, 1), "metrics": m,
            "n_entries": n_entries, "avg_hold_hours": avg_hold, "exit_reasons": reasons,
            "n_months": int(monthly.height), "config_hashes": cfg_hashes}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    arms = list(ARM_OVERRIDES.keys())
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "stage3_exit_alpha", "out": str(out),
        "window": {"start": args.start, "end_exclusive": args.end}, "sweep_tag": SWEEP_TAG,
        "git_head": _git_head(), "code_hash": code_hash, "frozen_forward_config_hash": frozen_config_hash(),
        "run_label": "exploratory", "giveback": GIVEBACK, "arms": arms, "pit_gate": {}, "venues": {},
    }
    metric_rows: list[dict[str, Any]] = []
    reason_rows: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        venue_block: dict[str, Any] = {"arms": {}}
        for arm in arms:
            res = _run_arm(root, venue, arm, args.start, args.end)
            venue_block["arms"][arm] = res
            m = res["metrics"]
            metric_rows.append({"venue": venue, "arm": arm, "n_entries": res["n_entries"],
                                "return": m.get("total_return"), "mar": m.get("mar"),
                                "max_dd": m.get("max_drawdown"), "worst_day": m.get("worst_day_return"),
                                "avg_hold_hours": res["avg_hold_hours"]})
            reason_rows.append({"venue": venue, "arm": arm, **res["exit_reasons"]})
        payload["venues"][venue] = venue_block

    _decide(payload)
    (out / "config_hashes.json").write_text(json.dumps(
        {v: b.get("arms", {}).get("X0_control", {}).get("config_hashes", {}) for v, b in payload["venues"].items()},
        indent=2, default=str), encoding="utf-8")
    _write_csv(out / "stage3_metrics.csv", metric_rows)
    _write_csv(out / "exit_reasons.csv", reason_rows)
    (out / "stage3_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage3_summary.md")
    return payload


def _pooled_mar_delta(payload, arm) -> float | None:
    deltas = []
    for _v, b in payload["venues"].items():
        if "arms" not in b:
            continue
        a = b["arms"].get(arm, {}).get("metrics", {}).get("mar")
        z = b["arms"].get("X0_control", {}).get("metrics", {}).get("mar")
        if a is None or z is None:
            return None
        deltas.append(float(a) - float(z))
    return float(sum(deltas) / len(deltas)) if deltas else None


def _decide(payload: dict[str, Any]) -> None:
    venues = [v for v, b in payload["venues"].items() if "arms" in b]
    full = _pooled_mar_delta(payload, "XT_mfe_giveback")
    r2x = _pooled_mar_delta(payload, "XT_mfe_giveback_2xcost")
    r5 = _pooled_mar_delta(payload, "X5_hash_exit")
    fails: list[str] = []
    dd_improved_any = False
    for venue in venues:
        a = payload["venues"][venue]["arms"]["XT_mfe_giveback"]
        z = payload["venues"][venue]["arms"]["X0_control"]
        am, zm = a["metrics"], z["metrics"]
        if (am.get("total_return") or 0) <= 0:
            fails.append(f"non-positive return on {venue}")
        if am.get("mar") is not None and zm.get("mar") is not None and (am["mar"] - zm["mar"]) < -0.5:
            fails.append(f"MAR delta < -0.5 on {venue}")
        if am.get("max_drawdown") is not None and zm.get("max_drawdown") is not None:
            if abs(am["max_drawdown"]) > 1.1 * abs(zm["max_drawdown"]):
                fails.append(f"drawdown worse >10% on {venue}")
            if abs(am["max_drawdown"]) < abs(zm["max_drawdown"]):
                dd_improved_any = True
        # same-entries check: entry count within 2% of control
        if z["n_entries"] and abs(a["n_entries"] - z["n_entries"]) / z["n_entries"] > 0.02:
            fails.append(f"entry count drift >2% on {venue} (population changed)")
    if full is None or full <= 0.1:
        fails.append("pooled MAR delta <= +0.1")
    if not dd_improved_any:
        fails.append("drawdown did not improve on either venue")
    if r2x is None or r2x <= 0.1:
        fails.append("2x-cost arm pooled MAR delta <= +0.1")
    if r5 is not None and full is not None and full <= r5:
        fails.append("not stronger than X5 hash-exit control")
    payload["decision"] = {
        "XT_mfe_giveback": {"admissible": not fails, "reasons": fails or ["admissible"],
                            "pooled_mar_delta": full, "pooled_mar_delta_2xcost": r2x,
                            "x5_pooled_mar_delta": r5}
    }
    payload["verdict_pass"] = not fails


def _fmt(x, fmt="{:.3f}"):
    return "" if x is None else fmt.format(x)


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]["XT_mfe_giveback"]
    lines = [
        "# W5 Continuous Stage 3 — Exit Lifecycle Alpha (MFE-giveback)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Frozen config hash: `{payload['frozen_forward_config_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Giveback `{payload['giveback']}`  Run label: `{payload['run_label']}`",
        f"- Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ADMISSIBLE' if payload.get('verdict_pass') else 'NULL'}",
        "",
        f"XT_mfe_giveback pooled ΔMAR vs X0: **{_fmt(d['pooled_mar_delta'])}** "
        f"(2x-cost {_fmt(d['pooled_mar_delta_2xcost'])}; X5 hash control {_fmt(d['x5_pooled_mar_delta'])}). "
        f"Reasons: {'; '.join(d['reasons'])}.",
        "",
        "| Venue | Arm | Entries | Return | MAR | MaxDD | Worst day | Avg hold (h) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for venue, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            m = a["metrics"]
            lines.append(
                f"| `{venue}` | `{arm}` | {a['n_entries']} | {_fmt(m.get('total_return'), '{:.4f}')} | "
                f"{_fmt(m.get('mar'))} | {_fmt(m.get('max_drawdown'), '{:.4f}')} | "
                f"{_fmt(m.get('worst_day_return'), '{:.4f}')} | {_fmt(a.get('avg_hold_hours'), '{:.1f}')} |")
    lines.extend(["", "MFE-giveback exits the SAME entries earlier when a winning fade gives back half",
                  "its peak gain — causal, warm-startable. Stage 3 is `exploratory`.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict_pass": payload.get("verdict_pass"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
