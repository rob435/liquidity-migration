#!/usr/bin/env python3
"""W5 Stage 8 - Regime-conditioned hedge (R4), mechanistically distinct from E2's V1/V2.

Keeps the V0 binary-uptrend entry gate and every trade IDENTICAL (reuses the Stage 0
component ledgers), and modulates only the BTC hedge intensity by a causal,
drift-robust, mean-1 BTC-volatility regime signal — hedge more in turbulent regimes,
less in calm, at constant average hedge. Tests whether better hedge *timing* (not
more/less hedging, not leverage, not a trade-population change) beats V0 on pooled
MAR across both venues. E2 closed the entry-gate regime family and pointed the
surviving lever at the hedge; this is that test.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage8-regime-hedge.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage8_regime_hedge.py \
        --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
        --lam 0.5 --vol-window 30 --pct-window 250 \
        --out ~/SHARED_DATA/w5_continuous_stage8_regime_hedge_2026-06-14
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics as st
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from liquidity_migration.continuous_events import _daily_pnl_metrics  # noqa: E402
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    FROZEN_FORWARD_CONFIG,
    frozen_config_hash,
    frozen_hedge_rule,
    frozen_rebalance_rule,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    apply_rebalance_rule,
    combine_continuous_components,
)
from scripts.w4_continuous_stop_exit_realism import (  # noqa: E402
    COMPONENTS,
    _btc_inputs,
    _load_component,
    _monthly_returns,
    _write_csv,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
SWEEP_TAG = "w5_continuous_stage8_regime_hedge_2026-06-14"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage8-regime-hedge.md"
VOL_MIN_OBS = 10
PCT_WARMUP = 50


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
        REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------- regime intensity -----------------------------
def _btc_vol_series(days: list[int], btc_rets: dict[int, float], vol_window: int) -> list[float | None]:
    vols: list[float | None] = []
    for i in range(len(days)):
        prior = [btc_rets[days[j]] for j in range(max(0, i - vol_window), i) if days[j] in btc_rets]
        vols.append(st.pstdev(prior) if len(prior) >= VOL_MIN_OBS else None)
    return vols


def _btcvol_intensity(days, btc_rets, lam, vol_window, pct_window) -> dict[int, float]:
    vols = _btc_vol_series(days, btc_rets, vol_window)
    dq: deque[float] = deque(maxlen=pct_window)
    intensity: dict[int, float] = {}
    for i, d in enumerate(days):
        v = vols[i]
        if v is None or len(dq) < PCT_WARMUP:
            intensity[d] = 1.0
        else:
            pct = sum(1 for x in dq if x <= v) / len(dq)
            intensity[d] = 1.0 + lam * (2.0 * pct - 1.0)
        if v is not None:
            dq.append(v)
    return intensity


def _hash_intensity(days, lam) -> dict[int, float]:
    out: dict[int, float] = {}
    for d in days:
        h = int(hashlib.sha256(str(int(d)).encode()).hexdigest()[:8], 16) % 1000 / 999.0
        out[d] = 1.0 + lam * (2.0 * h - 1.0)
    return out


# ----------------------------- ledger build -----------------------------
def _build_ledger(pieces, btc_rets, btc_fund, intensity, hedge_cost_bps=None) -> pl.DataFrame:
    combined = combine_continuous_components(pieces, FROZEN_FORWARD_CONFIG["weights"])
    hr = frozen_hedge_rule()
    if hedge_cost_bps is not None:
        hr = ContinuousHedgeRule(
            beta_window_days=hr.beta_window_days, beta_min_obs=hr.beta_min_obs,
            hedge_cap=hr.hedge_cap, cost_bps=float(hedge_cost_bps),
        )
    return apply_rebalance_rule(
        combined, frozen_rebalance_rule(), hr, btc_rets, btc_fund, hedge_intensity=intensity
    )


def _metrics(ledger: pl.DataFrame) -> dict[str, Any]:
    m = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    m["mean_hedge_ratio"] = float(ledger["hedge_ratio"].mean()) if "hedge_ratio" in ledger.columns else None
    m["total_hedge_cost"] = float(ledger["hedge_cost_return"].sum()) if "hedge_cost_return" in ledger.columns else None
    return m


def _arm_intensity(arm, days, btc_rets, lam, vol_window, pct_window):
    if arm == "V0_control":
        return None
    if arm in ("R4_btcvol_hedge", "R4_btcvol_hedge_2xcost"):
        return _btcvol_intensity(days, btc_rets, lam, vol_window, pct_window)
    if arm == "R5_hash_hedge":
        return _hash_intensity(days, lam)
    raise ValueError(arm)


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    arms = ["V0_control", "R4_btcvol_hedge", "R4_btcvol_hedge_2xcost", "R5_hash_hedge"]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "stage8_regime_hedge", "out": str(out),
        "sweep_tag": SWEEP_TAG, "git_head": _git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": frozen_config_hash(), "run_label": "exploratory",
        "lam": args.lam, "vol_window": args.vol_window, "pct_window": args.pct_window,
        "stage0_tag": args.stage0_tag, "arms": arms, "venues": {},
    }
    metric_rows: list[dict[str, Any]] = []
    tercile_rows: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        pieces = {}
        for comp, (_a, _cell) in COMPONENTS.items():
            rd = root / "reports" / args.stage0_tag / comp
            c, _trades, _rep = _load_component(rd)
            pieces[comp] = c
        days = sorted({d for c in pieces.values() for d in c.days})
        btc_rets, btc_fund = _btc_inputs(root, venue, days)
        vols = _btc_vol_series(days, btc_rets, args.vol_window)
        intens = {arm: _arm_intensity(arm, days, btc_rets, args.lam, args.vol_window, args.pct_window) for arm in arms}
        _write_csv(out / f"hedge_intensity_{venue}.csv",
                   [{"ts_ms": d, "btc_vol": vols[i],
                     "R4_intensity": (intens["R4_btcvol_hedge"] or {}).get(d),
                     "R5_intensity": (intens["R5_hash_hedge"] or {}).get(d)} for i, d in enumerate(days)])
        venue_block: dict[str, Any] = {"n_ledger_days": len(days), "arms": {}}
        ledgers: dict[str, pl.DataFrame] = {}
        for arm in arms:
            cost = 10.0 if arm == "R4_btcvol_hedge_2xcost" else None
            led = _build_ledger(pieces, btc_rets, btc_fund, intens[arm], hedge_cost_bps=cost)
            ledgers[arm] = led
            arm_dir = root / "reports" / SWEEP_TAG / arm
            arm_dir.mkdir(parents=True, exist_ok=True)
            led.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
            monthly = _monthly_returns(led)
            monthly.write_csv(arm_dir / "volume_event_best_monthly.csv")
            m = _metrics(led)
            it = intens[arm]
            mean_int = (float(np.mean([it[d] for d in days])) if it else 1.0)
            (arm_dir / "volume_event_research_report.json").write_text(json.dumps({
                "preregistration": PREREG, "run_label": "exploratory", "arm": arm, "venue": venue,
                "best_scenario": {"total_return": m.get("total_return"), "max_drawdown": m.get("max_drawdown"),
                                  "mar": m.get("mar"), "sharpe_like": m.get("sharpe_like")},
                "data_root": str(root)}, indent=2, default=str), encoding="utf-8")
            venue_block["arms"][arm] = {**m, "mean_intensity": mean_int, "n_months": int(monthly.height)}
            metric_rows.append({"venue": venue, "arm": arm, "return": m.get("total_return"),
                                "mar": m.get("mar"), "max_dd": m.get("max_drawdown"),
                                "worst_day": m.get("worst_day_return"), "mean_intensity": mean_int,
                                "mean_hedge_ratio": m.get("mean_hedge_ratio"),
                                "total_hedge_cost": m.get("total_hedge_cost")})
        # vol-tercile attribution of R4 vs V0 per-day return delta
        v0 = ledgers["V0_control"].select("ts_ms", "basket_return").rename({"basket_return": "v0"})
        r4 = ledgers["R4_btcvol_hedge"].select("ts_ms", "basket_return").rename({"basket_return": "r4"})
        joined = v0.join(r4, on="ts_ms", how="inner").to_dicts()
        vol_by_day = {int(days[i]): vols[i] for i in range(len(days)) if vols[i] is not None}
        rows_v = [(j["ts_ms"], j["r4"] - j["v0"], vol_by_day.get(int(j["ts_ms"]))) for j in joined]
        clean = [r for r in rows_v if r[2] is not None]
        clean.sort(key=lambda r: r[2])
        n = len(clean)
        for t in range(3):
            seg = clean[t * n // 3:(t + 1) * n // 3]
            tercile_rows.append({"venue": venue, "vol_tercile": t + 1,
                                 "sum_r4_minus_v0_return": float(sum(r[1] for r in seg)),
                                 "n_days": len(seg)})
        payload["venues"][venue] = venue_block

    _decide(payload)
    _write_csv(out / "stage8_metrics.csv", metric_rows)
    _write_csv(out / "vol_tercile.csv", tercile_rows)
    (out / "stage8_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage8_summary.md")
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
    r5 = _pooled_mar_delta(payload, "R5_hash_hedge")
    r4_2x = _pooled_mar_delta(payload, "R4_btcvol_hedge_2xcost")
    fails: list[str] = []
    full = _pooled_mar_delta(payload, "R4_btcvol_hedge")
    for venue in venues:
        a = payload["venues"][venue]["arms"]["R4_btcvol_hedge"]
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
    if full is None or full <= 0.1:
        fails.append("pooled MAR delta <= +0.1")
    if r4_2x is None or r4_2x <= 0.1:
        fails.append("2x-hedge-cost arm pooled MAR delta <= +0.1")
    if r5 is not None and full is not None and full <= r5:
        fails.append("not stronger than R5 hash-regime control")
    payload["decision"] = {
        "R4_btcvol_hedge": {"admissible": not fails, "reasons": fails or ["admissible"],
                            "pooled_mar_delta": full, "pooled_mar_delta_2xcost": r4_2x,
                            "r5_pooled_mar_delta": r5}
    }
    payload["verdict_pass"] = not fails


def _fmt(x, fmt="{:.3f}"):
    return "" if x is None else fmt.format(x)


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]["R4_btcvol_hedge"]
    lines = [
        "# W5 Continuous Stage 8 — Regime-Conditioned Hedge (R4)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Frozen config hash: `{payload['frozen_forward_config_hash']}`",
        f"- λ `{payload['lam']}`  vol_window `{payload['vol_window']}`  pct_window `{payload['pct_window']}`",
        f"- Run label: `{payload['run_label']}`  Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ADMISSIBLE' if payload.get('verdict_pass') else 'NULL'}",
        "",
        f"R4 pooled ΔMAR vs V0: **{_fmt(d['pooled_mar_delta'])}** "
        f"(2x-cost {_fmt(d['pooled_mar_delta_2xcost'])}; R5 hash control {_fmt(d['r5_pooled_mar_delta'])}). "
        f"Reasons: {'; '.join(d['reasons'])}.",
        "",
        "## Per-venue per-arm",
        "",
        "| Venue | Arm | Return | MAR | MaxDD | Worst day | Mean int | Mean hedge | Hedge cost |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for venue, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            lines.append(
                f"| `{venue}` | `{arm}` | {_fmt(a.get('total_return'), '{:.4f}')} | {_fmt(a.get('mar'))} | "
                f"{_fmt(a.get('max_drawdown'), '{:.4f}')} | {_fmt(a.get('worst_day_return'), '{:.4f}')} | "
                f"{_fmt(a.get('mean_intensity'))} | {_fmt(a.get('mean_hedge_ratio'))} | "
                f"{_fmt(a.get('total_hedge_cost'), '{:.4f}')} |")
    lines.extend(["", "R4 reallocates the BTC hedge across regimes at constant average intensity",
                  "(mean ≈ 1) — same trades, only hedge timing. Stage 8 is `exploratory`; a pass",
                  "nominates a demo/paper hedge shadow only.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--stage0-tag", default="w5_continuous_stage0_candidate_tape_2026-06-14", dest="stage0_tag")
    parser.add_argument("--lam", type=float, default=0.5)
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
