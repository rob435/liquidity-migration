#!/usr/bin/env python3
"""W5 Stage 10 - Cross-sectional dispersion regime hedge.

Last targeted shot at firming the BTC-vol regime-hedge candidate's thin binance cost
headroom: a hedge-regime signal built from cross-sectional alt-return DISPERSION (a
market-structure squeeze-risk measure that may be more both-venue-consistent than the
venue-split book-drawdown signal). Same causal mean-1 percentile hedge-intensity
mechanism (hedge_intensity hook); reuses Stage 0 components (no engine backtests).

Pre-registration: docs/preregistration/2026-06-15-w5-continuous-stage10-dispersion-hedge.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage10_dispersion_hedge.py \
        --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
        --out ~/SHARED_DATA/w5_continuous_stage10_dispersion_hedge_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import statistics as st
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "w5_stage8d", str(REPO / "scripts" / "w5_continuous_stage8d_regime_hedge_signals.py")
)
s8d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s8d)  # type: ignore[union-attr]
s8 = s8d.s8

from scripts.w4_continuous_stop_exit_realism import _write_csv  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
SWEEP_TAG = "w5_continuous_stage10_dispersion_hedge_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage10-dispersion-hedge.md"
PANEL = "_continuous_engine_panel_v2_rmom25_feat4a91acf4_616aea03.parquet"
MS_PER_DAY = 86_400_000
LAM = 0.5
COSTS = [5.0, 7.5, 10.0]
TRAIL = 10


def _code_hash() -> str:
    paths = [REPO / "scripts" / "w5_continuous_stage10_dispersion_hedge.py",
             REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py"]
    return hashlib.sha256("|".join(f"{p.relative_to(REPO)}:{s8._sha256_file(p)}" for p in paths if p.exists()).encode()).hexdigest()


def _dispersion_by_day(root: Path) -> dict[int, float]:
    panel = pl.read_parquet(root / PANEL, columns=["symbol", "ts_ms", "ret1"])
    panel = panel.with_columns((pl.col("ts_ms") // MS_PER_DAY * MS_PER_DAY).alias("day"))
    daily = panel.group_by(["symbol", "day"]).agg(((pl.col("ret1") + 1.0).product() - 1.0).alias("dret"))
    disp = daily.group_by("day").agg(pl.col("dret").std().alias("disp"))
    return {int(d): float(x) for d, x in disp.iter_rows() if x is not None}


def _dispersion_signal(days: list[int], disp_by_day: dict[int, float]) -> list[float | None]:
    """Trailing-TRAIL-day mean of daily dispersion ending at d-1 (causal), aligned to days."""
    out: list[float | None] = []
    for d in days:
        prior = [disp_by_day[d - k * MS_PER_DAY] for k in range(1, TRAIL + 1) if (d - k * MS_PER_DAY) in disp_by_day]
        out.append(st.mean(prior) if len(prior) >= 3 else None)
    return out


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "stage10_dispersion_hedge", "out": str(out), "git_head": s8._git_head(),
        "code_hash": code_hash, "frozen_forward_config_hash": s8.frozen_config_hash(),
        "run_label": "exploratory", "lam": LAM, "costs": COSTS, "panel": PANEL, "trail": TRAIL,
        "stage0_tag": args.stage0_tag, "venues": {}, "grid": [], "reference": {"S_btcvol": [], "R5_hash": []},
    }
    vd: dict[str, dict[str, Any]] = {}
    for venue in venues:
        root = s8.ROOTS[venue]
        pieces = {c: s8._load_component(root / "reports" / args.stage0_tag / c)[0] for c in s8.COMPONENTS}
        days = sorted({d for c in pieces.values() for d in c.days})
        btc_rets, btc_fund = s8._btc_inputs(root, venue, days)
        disp_sig = _dispersion_signal(days, _dispersion_by_day(root))
        disp_pct = s8d._pcts(days, disp_sig)
        disp_int = s8d._intensity(days, disp_pct, LAM)
        v0 = s8._metrics(s8._build_ledger(pieces, btc_rets, btc_fund, None))
        cov = sum(1 for p in disp_pct if p is not None)
        vd[venue] = {"pieces": pieces, "days": days, "btc_rets": btc_rets, "btc_fund": btc_fund,
                     "disp_int": disp_int, "v0_mar": v0.get("mar")}
        payload["venues"][venue] = {"v0_mar": v0.get("mar"), "disp_coverage_days": cov, "n_days": len(days),
                                    "mean_disp_intensity": float(st.mean([disp_int[d] for d in days]))}

    for cost in COSTS:
        per_venue = {}
        for venue in venues:
            d = vd[venue]
            m = s8._metrics(s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], d["disp_int"], hedge_cost_bps=cost))
            per_venue[venue] = {"mar_delta": (m.get("mar") - d["v0_mar"]) if (m.get("mar") is not None and d["v0_mar"] is not None) else None,
                                "mar": m.get("mar")}
        deltas = [per_venue[v]["mar_delta"] for v in venues if per_venue[v]["mar_delta"] is not None]
        payload["grid"].append({"cost_bps": cost, "pooled_mar_delta": (float(sum(deltas) / len(deltas)) if deltas else None),
                                "per_venue": per_venue})
        # references
        for name, intf in (("S_btcvol", lambda d: s8._btcvol_intensity(d["days"], d["btc_rets"], LAM, s8d.VOL_WINDOW, 250)),
                           ("R5_hash", lambda d: s8._hash_intensity(d["days"], LAM))):
            pv = {}
            for venue in venues:
                d = vd[venue]
                m = s8._metrics(s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], intf(d), hedge_cost_bps=cost))
                pv[venue] = (m.get("mar") - d["v0_mar"]) if (m.get("mar") is not None and d["v0_mar"] is not None) else None
            dd = [pv[v] for v in venues if pv[v] is not None]
            payload["reference"][name].append({"cost_bps": cost, "pooled_mar_delta": (float(sum(dd) / len(dd)) if dd else None), **pv})

    _decide(payload, venues)
    _write_csv(out / "stage10_grid.csv", [
        {"cost_bps": g["cost_bps"], "pooled_mar_delta": g["pooled_mar_delta"],
         **{f"{v}_mar_delta": g["per_venue"][v]["mar_delta"] for v in venues}} for g in payload["grid"]])
    (out / "stage10_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage10_summary.md", venues)
    return payload


def _cell(payload, cost):
    for g in payload["grid"]:
        if g["cost_bps"] == cost:
            return g
    return None


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    r5 = {h["cost_bps"]: h["pooled_mar_delta"] for h in payload["reference"]["R5_hash"]}
    fails: list[str] = []
    for cost in (5.0, 7.5):
        g = _cell(payload, cost)
        for v in venues:
            dv = g["per_venue"][v]["mar_delta"]
            if dv is None or dv <= 0:
                fails.append(f"{v} MAR delta {None if dv is None else round(dv, 4)} not >0 at {cost}bps")
    for cost in COSTS:
        g = _cell(payload, cost)
        if g["pooled_mar_delta"] is not None and r5.get(cost) is not None and g["pooled_mar_delta"] <= r5[cost]:
            fails.append(f"not > R5 at {cost}bps")
    for venue in venues:
        mi = payload["venues"][venue]["mean_disp_intensity"]
        if mi is not None and not (0.95 <= mi <= 1.05):
            fails.append(f"mean intensity {round(mi,3)} out of band ({venue})")
    payload["decision"] = {"robust_both_venues_through_1.5x": not fails, "reasons": fails or ["robust"],
                           "pooled_1x": (_cell(payload, 5.0) or {}).get("pooled_mar_delta"),
                           "pooled_1.5x": (_cell(payload, 7.5) or {}).get("pooled_mar_delta")}
    payload["robust"] = not fails


def _write_markdown(payload: dict[str, Any], path: Path, venues: list[str]) -> None:
    def f(x, fmt="{:+.3f}"):
        return "" if x is None else fmt.format(x)
    d = payload["decision"]
    lines = [
        "# W5 Continuous Stage 10 — Cross-Sectional Dispersion Regime Hedge",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`  λ `{payload['lam']}`",
        f"- Panel: `{payload['panel']}`  trail `{payload['trail']}`d  Pre-registration: `{payload['preregistration']}`",
        "",
        "V0 MAR: " + ", ".join(f"{v}={f(payload['venues'][v]['v0_mar'], '{:.3f}')} (disp int {f(payload['venues'][v]['mean_disp_intensity'], '{:.3f}')})" for v in venues),
        "",
        f"## Verdict: {'ROBUST dispersion-hedge (both venues through 1.5x)' if payload.get('robust') else 'NOT robust through 1.5x both venues'}",
        "",
        f"DISP pooled ΔMAR: 1× **{f(d['pooled_1x'])}**, 1.5× **{f(d['pooled_1.5x'])}**. Reasons: {'; '.join(d['reasons'][:4])}.",
        "",
        "## Dispersion per-venue MAR delta vs V0",
        "",
        "| Venue | 1.0× | 1.5× | 2.0× |", "|---|---:|---:|---:|",
    ]
    for v in venues:
        cells = [f(_cell(payload, c)["per_venue"][v]["mar_delta"]) for c in payload["costs"]]
        lines.append(f"| `{v}` | {cells[0]} | {cells[1]} | {cells[2]} |")
    lines.extend(["", "## Reference: BTC-vol baseline & R5 hash (pooled ΔMAR)", "",
                  "| cost | BTC-vol | R5 hash |", "|---|---:|---:|"])
    for i, cost in enumerate(payload["costs"]):
        lines.append(f"| {cost} | {f(payload['reference']['S_btcvol'][i]['pooled_mar_delta'])} | "
                     f"{f(payload['reference']['R5_hash'][i]['pooled_mar_delta'])} |")
    lines.extend(["", "Dispersion = cross-sectional std of PIT-universe daily returns (trailing-10d, causal),",
                  "percentile→mean-1 hedge intensity. Hedge-only (all trades kept). Robust ⇒ firms binance.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--stage0-tag", default="w5_continuous_stage0_candidate_tape_2026-06-14", dest="stage0_tag")
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "robust": payload.get("robust"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
