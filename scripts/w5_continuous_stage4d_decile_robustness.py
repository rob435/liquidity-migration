#!/usr/bin/env python3
"""W5 Stage 4d - Liquidity-sniper drop-decile robustness (k in {5,20}% brackets k=10%).

Stage 4 found dropping the least-liquid decile (k=10%) improves pooled MAR +0.407 (both
venues, beats a random-drop control). k=10% was pre-registered, but a single drop level can
still be a lucky cut. This stage brackets it: re-run the sniper at k=5% and k=20% (each with
a per-day count-matched random-drop control), both venues, 1x cost. The liquidity selection
is robust iff at BOTH k the turnover-drop beats its random-drop control AND improves MAR vs
the frozen control on BOTH venues (a smooth, both-sided effect, not a k=10 spike).

Reuses the Stage 4 engine machinery (`w5_continuous_stage4_sniper.py`): same causal
trailing-180d turnover percentile, same `size_mult=0` drop via the Stage 5 hook, same
count-matched random control. Engine re-run per arm.

Pre-registration: docs/preregistration/2026-06-15-w5-continuous-stage4d-decile-robustness.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage4d_decile_robustness.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
        --out ~/SHARED_DATA/w5_continuous_stage4d_decile_robustness_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location("w5_stage4", str(REPO / "scripts" / "w5_continuous_stage4_sniper.py"))
s4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s4)  # type: ignore[union-attr]

SWEEP_TAG = "w5_continuous_stage4d_decile_robustness_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage4d-decile-robustness.md"
K_VALUES = [0.05, 0.20]
# point the reused Stage 4 _run_arm at THIS stage's report tag / receipt
s4.SWEEP_TAG = SWEEP_TAG
s4.PREREG = PREREG


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "stage4d_decile_robustness", "out": str(out),
        "window": {"start": args.start, "end_exclusive": args.end}, "sweep_tag": SWEEP_TAG,
        "git_head": s4._git_head(), "frozen_forward_config_hash": s4.frozen_config_hash(),
        "run_label": "exploratory", "k_values": K_VALUES, "venues": {}, "drop_meta": {},
    }
    start_ms = int(dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    end_ms = int(dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    for venue in venues:
        root = s4.ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate_ok"] = True
        entries = s4._unique_entries(stage0_dir, venue)
        venue_block: dict[str, Any] = {"arms": {}}
        dmeta: dict[str, Any] = {}
        # T0 control (lookup None)
        venue_block["arms"]["T0_control"] = s4._run_arm(root, venue, "T0_control", None, 1.0, args.start, args.end)
        for k in K_VALUES:
            s4.DROP_K = k  # _turnover_drop_set reads the module global at call time
            tdrop, meta = s4._turnover_drop_set(entries)
            rdrop = s4._random_drop_set(entries, tdrop, s4.SEED)
            kk = f"{int(round(k * 100)):02d}"
            dmeta[kk] = {**meta, "n_random": len(rdrop),
                         "turnover_conc": s4._concentration(tdrop, start_ms, end_ms),
                         "random_conc": s4._concentration(rdrop, start_ms, end_ms)}
            venue_block["arms"][f"T1_turnover_k{kk}"] = s4._run_arm(
                root, venue, f"T1_turnover_k{kk}", {(s, t): 0.0 for (s, t) in tdrop}, 1.0, args.start, args.end)
            venue_block["arms"][f"T2_random_k{kk}"] = s4._run_arm(
                root, venue, f"T2_random_k{kk}", {(s, t): 0.0 for (s, t) in rdrop}, 1.0, args.start, args.end)
        payload["drop_meta"][venue] = dmeta
        payload["venues"][venue] = venue_block

    _decide(payload, venues)
    (out / "stage4d_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage4d_summary.md")
    return payload


def _mar(payload, v, arm):
    return payload["venues"][v]["arms"].get(arm, {}).get("metrics", {}).get("mar")


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    valid = [v for v in venues if "arms" in payload["venues"].get(v, {})]
    per_k: dict[str, Any] = {}
    robust_all = True
    for k in K_VALUES:
        kk = f"{int(round(k * 100)):02d}"
        fails = []
        for v in valid:
            t0, t1, t2 = _mar(payload, v, "T0_control"), _mar(payload, v, f"T1_turnover_k{kk}"), _mar(payload, v, f"T2_random_k{kk}")
            if None in (t0, t1, t2):
                fails.append(f"missing {v}")
                continue
            if t1 <= t0:
                fails.append(f"T1<=T0 {v}")
            if t1 <= t2:
                fails.append(f"T1<=random {v}")
        pooled_t1 = [(_mar(payload, v, f"T1_turnover_k{kk}") - _mar(payload, v, "T0_control")) for v in valid
                     if _mar(payload, v, f"T1_turnover_k{kk}") is not None]
        pooled_sel = [(_mar(payload, v, f"T1_turnover_k{kk}") - _mar(payload, v, f"T2_random_k{kk}")) for v in valid
                      if _mar(payload, v, f"T2_random_k{kk}") is not None]
        per_k[kk] = {"robust": not fails, "reasons": fails or ["robust"],
                     "pooled_mar_delta": (sum(pooled_t1) / len(pooled_t1) if pooled_t1 else None),
                     "pooled_selection": (sum(pooled_sel) / len(pooled_sel) if pooled_sel else None)}
        robust_all = robust_all and not fails
    payload["decision"] = {"robust_all_k": robust_all, "per_k": per_k}


def _fmt(x, f="{:+.3f}"):
    return "" if x is None else f.format(x)


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]
    lines = [
        "# W5 Continuous Stage 4d — Liquidity-Sniper Drop-Decile Robustness",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- k values: {payload['k_values']} (brackets the Stage 4 k=10%). Pre-reg: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ROBUST across deciles' if d['robust_all_k'] else 'NOT robust at some k'}",
        "",
    ]
    for kk, info in d["per_k"].items():
        lines.append(f"- **k={int(kk)}%**: pooled ΔMAR vs T0 **{_fmt(info['pooled_mar_delta'])}**, "
                     f"pooled selection (T1−random) **{_fmt(info['pooled_selection'])}** — "
                     f"{'robust' if info['robust'] else '; '.join(info['reasons'][:3])}.")
    lines.extend(["", "## Per-arm MAR", "", "| Venue | Arm | MAR | Return | MaxDD | Nonzero |",
                  "|---|---|---:|---:|---:|---:|"])
    for v, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            m = a["metrics"]
            lines.append(f"| `{v}` | `{arm}` | {_fmt(m.get('mar'))} | {_fmt(m.get('total_return'), '{:.4f}')} | "
                         f"{_fmt(m.get('max_drawdown'), '{:.4f}')} | {a.get('n_nonzero')} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--stage0", default=str(s4.SHARED / s4.STAGE0_TAG))
    parser.add_argument("--out", default=str(s4.SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
