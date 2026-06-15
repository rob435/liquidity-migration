#!/usr/bin/env python3
"""W5 Stage 8b - Lower-turnover regime hedge.

Stage 8's continuous daily-percentile hedge intensity improved pooled MAR +0.078 on
both venues and beat the hash control by +0.69, but missed the +0.1 bar and the
2x-hedge-cost arm collapsed it — the binding constraint is hedge turnover (the daily
intensity resizes the hedge every day). Stage 8b uses the SAME causal BTC-vol regime
but a discrete, persistent, hysteretic BANDED intensity (0.7/1.0/1.3) that resizes
the hedge only on regime transitions, so it adds far less hedge churn while holding
the Stage-8 tilt amplitude (Stage 8 at pct~0.167/0.833 gives ~0.67/1.33).

Reuses the Stage 0 component ledgers (V0 entries, frozen) and the additive
`hedge_intensity` engine hook. No engine backtests — fast ensemble rebuilds.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage8b-lowturnover-regime-hedge.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage8b_lowturnover_hedge.py \
        --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
        --vol-window 30 --pct-window 250 \
        --out ~/SHARED_DATA/w5_continuous_stage8b_lowturnover_hedge_2026-06-14
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "w5_stage8", str(REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py")
)
s8 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s8)  # type: ignore[union-attr]

from liquidity_migration.continuous_forward_replay import frozen_config_hash  # noqa: E402
from scripts.w4_continuous_stop_exit_realism import (  # noqa: E402
    COMPONENTS,
    _btc_inputs,
    _load_component,
    _monthly_returns,
    _write_csv,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = s8.ROOTS
SWEEP_TAG = "w5_continuous_stage8b_lowturnover_hedge_2026-06-14"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage8b-lowturnover-regime-hedge.md"
LOW, MID, HIGH = 0.7, 1.0, 1.3
MS_PER_DAY = 86_400_000
MAX_TURNOVER_CHANGES = 100  # gate #9: must be far below the ~660 daily-continuous changes


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage8b_lowturnover_hedge.py",
        REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{s8._sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------- intensity builders -----------------------------
def _vol_percentiles(days, btc_rets, vol_window, pct_window) -> list[float | None]:
    vols = s8._btc_vol_series(days, btc_rets, vol_window)
    dq: deque[float] = deque(maxlen=pct_window)
    pcts: list[float | None] = []
    for i in range(len(days)):
        v = vols[i]
        if v is None or len(dq) < s8.PCT_WARMUP:
            pcts.append(None)
        else:
            pcts.append(sum(1 for x in dq if x <= v) / len(dq))
        if v is not None:
            dq.append(v)
    return pcts


def _banded_intensity(days, btc_rets, vol_window, pct_window) -> dict[int, float]:
    """Discrete, persistent, hysteretic band by causal vol percentile."""
    pcts = _vol_percentiles(days, btc_rets, vol_window, pct_window)
    level = {"low": LOW, "mid": MID, "high": HIGH}
    band = "mid"
    out: dict[int, float] = {}
    for i, d in enumerate(days):
        p = pcts[i]
        if p is None:
            band = "mid"
        elif band == "high":
            if p < 0.55:
                band = "low" if p <= 0.333 else "mid"
        elif band == "low":
            if p > 0.45:
                band = "high" if p >= 0.667 else "mid"
        else:  # mid
            if p >= 0.667:
                band = "high"
            elif p <= 0.333:
                band = "low"
        out[int(d)] = level[band]
    return out


def _hashweek_banded(days) -> dict[int, float]:
    """Negative control: band by hash(week), held within each 7-day week (no market content)."""
    levels = [LOW, MID, HIGH]
    day0 = days[0]
    out: dict[int, float] = {}
    for d in days:
        wk = (int(d) - int(day0)) // (7 * MS_PER_DAY)
        h = int(hashlib.sha256(str(int(wk)).encode()).hexdigest()[:8], 16) % 3
        out[int(d)] = levels[h]
    return out


def _turnover(intensity: dict[int, float] | None, days: list[int]) -> dict[str, Any]:
    if intensity is None:
        return {"n_changes": 0, "sum_abs_delta": 0.0, "mean_intensity": 1.0}
    seq = [intensity[d] for d in days]
    n_changes = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
    sum_abs = float(sum(abs(seq[i] - seq[i - 1]) for i in range(1, len(seq))))
    return {"n_changes": n_changes, "sum_abs_delta": sum_abs, "mean_intensity": float(np.mean(seq))}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    arms = ["V0_control", "R4b_banded_hedge", "R4b_banded_2xcost", "R5b_hashweek_banded"]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "stage8b_lowturnover_hedge", "out": str(out),
        "sweep_tag": SWEEP_TAG, "git_head": s8._git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": frozen_config_hash(), "run_label": "exploratory",
        "vol_window": args.vol_window, "pct_window": args.pct_window, "bands": [LOW, MID, HIGH],
        "stage0_tag": args.stage0_tag, "arms": arms, "venues": {},
    }
    metric_rows: list[dict[str, Any]] = []
    turnover_rows: list[dict[str, Any]] = []
    tercile_rows: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        pieces = {}
        for comp, (_a, _cell) in COMPONENTS.items():
            c, _t, _r = _load_component(root / "reports" / args.stage0_tag / comp)
            pieces[comp] = c
        days = sorted({d for c in pieces.values() for d in c.days})
        btc_rets, btc_fund = _btc_inputs(root, venue, days)
        vols = s8._btc_vol_series(days, btc_rets, args.vol_window)
        banded = _banded_intensity(days, btc_rets, args.vol_window, args.pct_window)
        hashweek = _hashweek_banded(days)
        intens = {"V0_control": None, "R4b_banded_hedge": banded,
                  "R4b_banded_2xcost": banded, "R5b_hashweek_banded": hashweek}
        _write_csv(out / f"hedge_intensity_{venue}.csv",
                   [{"ts_ms": d, "btc_vol": vols[i], "R4b_intensity": banded[d],
                     "R5b_intensity": hashweek[d]} for i, d in enumerate(days)])
        venue_block: dict[str, Any] = {"n_ledger_days": len(days), "arms": {}}
        ledgers: dict[str, pl.DataFrame] = {}
        for arm in arms:
            cost = 10.0 if arm == "R4b_banded_2xcost" else None
            led = s8._build_ledger(pieces, btc_rets, btc_fund, intens[arm], hedge_cost_bps=cost)
            ledgers[arm] = led
            arm_dir = root / "reports" / SWEEP_TAG / arm
            arm_dir.mkdir(parents=True, exist_ok=True)
            led.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
            monthly = _monthly_returns(led)
            monthly.write_csv(arm_dir / "volume_event_best_monthly.csv")
            m = s8._metrics(led)
            tov = _turnover(intens[arm], days)
            (arm_dir / "volume_event_research_report.json").write_text(json.dumps({
                "preregistration": PREREG, "run_label": "exploratory", "arm": arm, "venue": venue,
                "best_scenario": {"total_return": m.get("total_return"), "max_drawdown": m.get("max_drawdown"),
                                  "mar": m.get("mar")}, "data_root": str(root)}, indent=2, default=str),
                encoding="utf-8")
            venue_block["arms"][arm] = {**m, "mean_intensity": tov["mean_intensity"],
                                        "n_intensity_changes": tov["n_changes"], "n_months": int(monthly.height)}
            metric_rows.append({"venue": venue, "arm": arm, "return": m.get("total_return"),
                                "mar": m.get("mar"), "max_dd": m.get("max_drawdown"),
                                "mean_intensity": tov["mean_intensity"],
                                "total_hedge_cost": m.get("total_hedge_cost")})
            turnover_rows.append({"venue": venue, "arm": arm, **tov,
                                  "total_hedge_cost": m.get("total_hedge_cost")})
        # vol-tercile attribution of R4b vs V0
        v0 = ledgers["V0_control"].select("ts_ms", "basket_return").rename({"basket_return": "v0"})
        r4 = ledgers["R4b_banded_hedge"].select("ts_ms", "basket_return").rename({"basket_return": "r4"})
        joined = v0.join(r4, on="ts_ms", how="inner").to_dicts()
        vol_by_day = {int(days[i]): vols[i] for i in range(len(days)) if vols[i] is not None}
        clean = sorted(((j["r4"] - j["v0"], vol_by_day.get(int(j["ts_ms"])))
                        for j in joined if vol_by_day.get(int(j["ts_ms"])) is not None),
                       key=lambda r: r[1])
        n = len(clean)
        for t in range(3):
            seg = clean[t * n // 3:(t + 1) * n // 3]
            tercile_rows.append({"venue": venue, "vol_tercile": t + 1,
                                 "sum_r4b_minus_v0_return": float(sum(r[0] for r in seg)), "n_days": len(seg)})
        payload["venues"][venue] = venue_block

    _decide(payload)
    _write_csv(out / "stage8b_metrics.csv", metric_rows)
    _write_csv(out / "turnover.csv", turnover_rows)
    _write_csv(out / "vol_tercile.csv", tercile_rows)
    (out / "stage8b_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage8b_summary.md")
    return payload


def _pooled_mar_delta(payload, arm) -> float | None:
    deltas = []
    for _v, b in payload["venues"].items():
        if "arms" not in b:
            continue
        a = b["arms"].get(arm, {}).get("mar")
        z = b["arms"].get("V0_control", {}).get("mar")
        if a is None or z is None:
            return None
        deltas.append(float(a) - float(z))
    return float(sum(deltas) / len(deltas)) if deltas else None


def _decide(payload: dict[str, Any]) -> None:
    venues = [v for v, b in payload["venues"].items() if "arms" in b]
    r5 = _pooled_mar_delta(payload, "R5b_hashweek_banded")
    r4_2x = _pooled_mar_delta(payload, "R4b_banded_2xcost")
    full = _pooled_mar_delta(payload, "R4b_banded_hedge")
    fails: list[str] = []
    for venue in venues:
        a = payload["venues"][venue]["arms"]["R4b_banded_hedge"]
        z = payload["venues"][venue]["arms"]["V0_control"]
        if (a.get("total_return") or 0) <= 0:
            fails.append(f"non-positive return on {venue}")
        if a.get("mar") is not None and z.get("mar") is not None and (a["mar"] - z["mar"]) < -0.5:
            fails.append(f"MAR delta < -0.5 on {venue}")
        if a.get("max_drawdown") is not None and z.get("max_drawdown") is not None and abs(a["max_drawdown"]) > 1.1 * abs(z["max_drawdown"]):
            fails.append(f"drawdown worse >10% on {venue}")
        mi = a.get("mean_intensity")
        if mi is not None and not (0.95 <= mi <= 1.05):
            fails.append(f"mean intensity {mi:.3f} outside [0.95,1.05] on {venue}")
        nc = a.get("n_intensity_changes")
        if nc is not None and nc >= MAX_TURNOVER_CHANGES:
            fails.append(f"intensity changes {nc} not below {MAX_TURNOVER_CHANGES} on {venue} (not low-turnover)")
    if full is None or full <= 0.1:
        fails.append("pooled MAR delta <= +0.1")
    if r4_2x is None or r4_2x <= 0.1:
        fails.append("2x-hedge-cost arm pooled MAR delta <= +0.1")
    if r5 is not None and full is not None and full <= r5:
        fails.append("not stronger than R5b hashweek control")
    payload["decision"] = {
        "R4b_banded_hedge": {"admissible": not fails, "reasons": fails or ["admissible"],
                             "pooled_mar_delta": full, "pooled_mar_delta_2xcost": r4_2x,
                             "r5_pooled_mar_delta": r5}
    }
    payload["verdict_pass"] = not fails


def _fmt(x, fmt="{:.3f}"):
    return "" if x is None else fmt.format(x)


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]["R4b_banded_hedge"]
    lines = [
        "# W5 Continuous Stage 8b — Lower-Turnover Regime Hedge",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Frozen config hash: `{payload['frozen_forward_config_hash']}`",
        f"- Bands `{payload['bands']}`  vol_window `{payload['vol_window']}`  pct_window `{payload['pct_window']}`",
        f"- Run label: `{payload['run_label']}`  Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ADMISSIBLE' if payload.get('verdict_pass') else 'NULL'}",
        "",
        f"R4b pooled ΔMAR vs V0: **{_fmt(d['pooled_mar_delta'])}** "
        f"(2x-cost {_fmt(d['pooled_mar_delta_2xcost'])}; R5b hashweek control {_fmt(d['r5_pooled_mar_delta'])}). "
        f"Reasons: {'; '.join(d['reasons'])}.",
        "",
        "| Venue | Arm | Return | MAR | MaxDD | Mean int | Int changes | Hedge cost |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for venue, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            lines.append(
                f"| `{venue}` | `{arm}` | {_fmt(a.get('total_return'), '{:.4f}')} | {_fmt(a.get('mar'))} | "
                f"{_fmt(a.get('max_drawdown'), '{:.4f}')} | {_fmt(a.get('mean_intensity'))} | "
                f"{a.get('n_intensity_changes')} | {_fmt(a.get('total_hedge_cost'), '{:.4f}')} |")
    lines.extend(["", "Banded hysteretic BTC-vol hedge intensity (0.7/1.0/1.3), resized only on regime",
                  "transitions — same Stage-8 amplitude, far fewer hedge resizes. `exploratory`.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--stage0-tag", default="w5_continuous_stage0_candidate_tape_2026-06-14", dest="stage0_tag")
    parser.add_argument("--vol-window", type=int, default=30, dest="vol_window")
    parser.add_argument("--pct-window", type=int, default=250, dest="pct_window")
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict_pass": payload.get("verdict_pass"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
