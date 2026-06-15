#!/usr/bin/env python3
"""W5 Stage 9 - Regime-conditioned book sizing (constant breadth).

Sizes the SAME book (V0 entries) DOWN in high-BTC-vol regimes via a causal, mean-1,
per-day regime size multiplier (size_mult_lookup hook). The sizing analogue of the
regime-hedge, with no hedge turnover cost — may help binance where the hedge is thin.
Distinct from path-shape sizing (Stage 5; symbol-cross-sectional) since the tilt is a
market-wide DAILY regime identical across symbols, and from E2 V1/V2 since breadth is
unchanged (same entries, mean-1 notional reallocation).

Pre-registration: docs/preregistration/2026-06-15-w5-continuous-stage9-regime-sizing.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage9_regime_sizing.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --out ~/SHARED_DATA/w5_continuous_stage9_regime_sizing_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import statistics as st
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "w5_stage8d", str(REPO / "scripts" / "w5_continuous_stage8d_regime_hedge_signals.py")
)
s8d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s8d)  # type: ignore[union-attr]

from liquidity_migration.continuous_events import (  # noqa: E402
    _daily_pnl_metrics,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import build_full_ledger, frozen_config_hash  # noqa: E402
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
STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
SWEEP_TAG = "w5_continuous_stage9_regime_sizing_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage9-regime-sizing.md"
LAM = 0.40
MS_PER_DAY = 86_400_000
# arm -> (regime_kind, cost_mult); regime_kind None=control
ARMS = {
    "Z0_control": (None, 1.0),
    "ZR_btcvol_size": ("btcvol", 1.0),
    "ZR_btcvol_size_2xcost": ("btcvol", 2.0),
    "ZR_hash_size": ("hash", 1.0),
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
    paths = [REPO / "scripts" / "w5_continuous_stage9_regime_sizing.py",
             REPO / "liquidity_migration" / "continuous_events.py"]
    return hashlib.sha256("|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths if p.exists()).encode()).hexdigest()


def _day_floor(ts_ms: int) -> int:
    return (int(ts_ms) // MS_PER_DAY) * MS_PER_DAY


def _month_of(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def _prior_month_norm(days, raw: dict[int, float]) -> dict[int, float]:
    month_vals: dict[str, list[float]] = defaultdict(list)
    for d in days:
        month_vals[_month_of(d)].append(raw[d])
    months = sorted(month_vals)
    mean_by = {m: (st.mean(v) if v else 1.0) for m, v in month_vals.items()}
    prior = {months[i]: mean_by[months[i - 1]] for i in range(1, len(months))}
    if months:
        prior[months[0]] = 1.0
    return {d: raw[d] / (prior[_month_of(d)] or 1.0) for d in days}


def _daily_regime(days, btc_rets, kind) -> dict[int, float]:
    if kind == "btcvol":
        pct = s8d._pcts(days, s8d._trailing_vol(days, btc_rets, s8d.VOL_WINDOW))
        raw = {int(d): (1.0 if pct[i] is None else 1.0 - LAM * (2.0 * pct[i] - 1.0)) for i, d in enumerate(days)}
    else:  # hash-week, no market content (mean-1)
        day0 = days[0]
        raw = {}
        for d in days:
            wk = (int(d) - int(day0)) // (7 * MS_PER_DAY)
            h = int(hashlib.sha256(str(int(wk)).encode()).hexdigest()[:8], 16) % 1000 / 999.0
            raw[int(d)] = 1.0 - LAM * (2.0 * h - 1.0)
    return _prior_month_norm(days, raw)


def _selected_entries(stage0_dir: Path, venue: str) -> list[tuple[str, int]]:
    tape = pl.read_parquet(stage0_dir / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    return [(str(s), int(t)) for s, t in tape.select("symbol", "signal_ts_ms").iter_rows()]


def _run_arm(root: Path, venue: str, arm: str, lookup, cost_mult: float, start: str, end: str) -> dict[str, Any]:
    pieces: dict[str, Any] = {}
    trades_all: list[pl.DataFrame] = []
    overrides = {"round_trip_cost_multiplier": cost_mult} if cost_mult != 1.0 else {}
    t0 = time.time()
    for component, (_artifact_root, cell) in COMPONENTS.items():
        report_dir = root / "reports" / SWEEP_TAG / arm / component
        cfg = _component_config(component, cell, start=start, end=end, arm_overrides=overrides)
        run_continuous_event_research(root, config=cfg, report_dir=report_dir, size_mult_lookup=lookup)
        comp, trades, _r = _load_component(report_dir)
        pieces[component] = comp
        trades_all.append(trades)
    all_days = sorted({d for c in pieces.values() for d in c.days})
    btc_rets, btc_fund = _btc_inputs(root, venue, all_days)
    ledger = build_full_ledger(pieces, btc_rets, btc_fund)
    arm_dir = root / "reports" / SWEEP_TAG / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
    _monthly_returns(ledger).write_csv(arm_dir / "volume_event_best_monthly.csv")
    m = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    trades = pl.concat(trades_all) if trades_all else pl.DataFrame()
    total_notional = float(trades["notional_weight"].abs().sum()) if trades.height else 0.0
    (arm_dir / "volume_event_research_report.json").write_text(json.dumps(
        {"preregistration": PREREG, "run_label": "exploratory", "arm": arm, "venue": venue,
         "best_scenario": {"total_return": m.get("total_return"), "max_drawdown": m.get("max_drawdown"),
                           "mar": m.get("mar"), "trades": int(trades.height)}}, indent=2, default=str), encoding="utf-8")
    return {"arm": arm, "venue": venue, "elapsed_s": round(time.time() - t0, 1), "metrics": m,
            "n_entries": int(trades.height), "total_notional": total_notional,
            "exit_reasons": dict(Counter(str(x) for x in trades["exit_reason"].to_list())) if trades.height else {}}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "stage9_regime_sizing", "out": str(out), "window": {"start": args.start, "end_exclusive": args.end},
        "sweep_tag": SWEEP_TAG, "git_head": _git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": frozen_config_hash(), "run_label": "exploratory", "lam": LAM,
        "arms": list(ARMS.keys()), "pit_gate": {}, "venues": {}, "lookup_meta": {},
    }
    metric_rows: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        # build daily regime intensities from this venue's btc returns over the component days
        pieces0 = {c: _load_component(root / "reports" / STAGE0_TAG / c)[0] for c in COMPONENTS}
        days = sorted({d for c in pieces0.values() for d in c.days})
        btc_rets, _bf = _btc_inputs(root, venue, days)
        regimes = {"btcvol": _daily_regime(days, btc_rets, "btcvol"), "hash": _daily_regime(days, btc_rets, "hash")}
        entries = _selected_entries(stage0_dir, venue)
        lookups = {"btcvol": {}, "hash": {}}
        for kind in ("btcvol", "hash"):
            reg = regimes[kind]
            lookups[kind] = {(s, t): reg.get(_day_floor(t), 1.0) for (s, t) in entries}
        payload["lookup_meta"][venue] = {
            kind: {"mean_mult": float(np.mean(list(lookups[kind].values()))) if lookups[kind] else 1.0,
                   "n": len(lookups[kind])} for kind in ("btcvol", "hash")}
        venue_block: dict[str, Any] = {"arms": {}}
        for arm, (kind, cost) in ARMS.items():
            lookup = None if kind is None else lookups[kind]
            res = _run_arm(root, venue, arm, lookup, cost, args.start, args.end)
            venue_block["arms"][arm] = res
            mm = (None if kind is None else payload["lookup_meta"][venue][kind]["mean_mult"])
            metric_rows.append({"venue": venue, "arm": arm, "n_entries": res["n_entries"],
                                "return": res["metrics"].get("total_return"), "mar": res["metrics"].get("mar"),
                                "max_dd": res["metrics"].get("max_drawdown"), "mean_mult": mm,
                                "total_notional": res["total_notional"]})
        payload["venues"][venue] = venue_block

    _decide(payload, venues)
    _write_csv(out / "stage9_metrics.csv", metric_rows)
    (out / "stage9_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage9_summary.md", venues)
    return payload


def _pooled(payload, arm) -> float | None:
    ds = []
    for _v, b in payload["venues"].items():
        if "arms" not in b:
            continue
        a = b["arms"].get(arm, {}).get("metrics", {}).get("mar")
        z = b["arms"].get("Z0_control", {}).get("metrics", {}).get("mar")
        if a is None or z is None:
            return None
        ds.append(a - z)
    return float(sum(ds) / len(ds)) if ds else None


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    full = _pooled(payload, "ZR_btcvol_size")
    r2x = _pooled(payload, "ZR_btcvol_size_2xcost")
    rhash = _pooled(payload, "ZR_hash_size")
    fails: list[str] = []
    for venue in venues:
        a = payload["venues"][venue]["arms"]["ZR_btcvol_size"]
        z = payload["venues"][venue]["arms"]["Z0_control"]
        am, zm = a["metrics"], z["metrics"]
        if (am.get("total_return") or 0) <= 0:
            fails.append(f"non-positive return {venue}")
        if am.get("mar") is not None and zm.get("mar") is not None and (am["mar"] - zm["mar"]) <= 0:
            fails.append(f"MAR delta <=0 on {venue}")
        if am.get("mar") is not None and zm.get("mar") is not None and (am["mar"] - zm["mar"]) < -0.5:
            fails.append(f"MAR delta < -0.5 on {venue}")
        if am.get("max_drawdown") is not None and zm.get("max_drawdown") is not None and abs(am["max_drawdown"]) > 1.1 * abs(zm["max_drawdown"]):
            fails.append(f"drawdown worse >10% {venue}")
        if z["total_notional"] and abs(a["total_notional"] - z["total_notional"]) / z["total_notional"] > 0.05:
            fails.append(f"gross ratio outside +/-5% on {venue}")
        if z["n_entries"] and a["n_entries"] != z["n_entries"]:
            fails.append(f"entry count changed on {venue}")
    if r2x is not None and any(payload["venues"][v]["arms"]["ZR_btcvol_size_2xcost"]["metrics"].get("mar", 0) - payload["venues"][v]["arms"]["Z0_control"]["metrics"].get("mar", 0) <= 0 for v in venues):
        fails.append("2x-cost: a venue MAR delta <= 0")
    if rhash is not None and full is not None and full <= rhash:
        fails.append("not stronger than hash-regime control")
    payload["decision"] = {"ZR_btcvol_size": {"robust": not fails, "reasons": fails or ["robust"],
                           "pooled_mar_delta": full, "pooled_2xcost": r2x, "hash_pooled": rhash}}
    payload["robust"] = not fails


def _fmt(x, fmt="{:+.3f}"):
    return "" if x is None else fmt.format(x)


def _write_markdown(payload: dict[str, Any], path: Path, venues: list[str]) -> None:
    d = payload["decision"]["ZR_btcvol_size"]
    lines = [
        "# W5 Continuous Stage 9 — Regime-Conditioned Book Sizing",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`  λ `{payload['lam']}`",
        f"- Code hash: `{payload['code_hash']}`  Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ROBUST regime-sizing candidate' if payload.get('robust') else 'NULL'}",
        "",
        f"ZR_btcvol_size pooled ΔMAR vs Z0: **{_fmt(d['pooled_mar_delta'])}** "
        f"(2x-cost {_fmt(d['pooled_2xcost'])}; hash-regime {_fmt(d['hash_pooled'])}). "
        f"Reasons: {'; '.join(d['reasons'][:4])}.",
        "",
        "| Venue | Arm | Entries | Return | MAR | MaxDD | Mean mult | Notional |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for venue, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            kind = ARMS[arm][0]
            mm = None if kind is None else payload["lookup_meta"][venue][kind]["mean_mult"]
            m = a["metrics"]
            lines.append(f"| `{venue}` | `{arm}` | {a['n_entries']} | {_fmt(m.get('total_return'), '{:.4f}')} | "
                         f"{_fmt(m.get('mar'))} | {_fmt(m.get('max_drawdown'), '{:.4f}')} | "
                         f"{_fmt(mm, '{:.3f}')} | {_fmt(a['total_notional'], '{:.2f}')} |")
    lines.extend(["", "Same entries; book sized down in high-BTC-vol regimes, mean-1. Stage 9 `exploratory`.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--stage0", default=str(SHARED / STAGE0_TAG))
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "robust": payload.get("robust"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
