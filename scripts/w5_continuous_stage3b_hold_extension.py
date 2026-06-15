#!/usr/bin/env python3
"""W5 Stage 3b - Hold-extension exit (does holding LONGER capture more reversion?).

Stage 3 showed that exiting EARLIER (mfe-giveback) destroys return on this
mean-reversion fade book — positions at the fixed 24h hold cap are on average still
favorable. Stage 3b tests the opposite direction: extend the fixed hold (hold_hours
24 -> 48) on the SAME entries and ask whether more reversion + short-funding credit
beats the extra squeeze exposure on pooled MAR vs the frozen control.

Reuses the Stage 3 per-arm engine machinery (full component re-runs + ensemble rebuild
+ exit-reason/hold metrics) via config override; only the hold cap changes, so entries
are unchanged.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage3b-hold-extension.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage3b_hold_extension.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --out ~/SHARED_DATA/w5_continuous_stage3b_hold_extension_2026-06-14
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "w5_stage3", str(REPO / "scripts" / "w5_continuous_stage3_exit_alpha.py")
)
s3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s3)  # type: ignore[union-attr]

from scripts.w4_continuous_stop_exit_realism import _pit_partition_gate, _write_csv  # noqa: E402

SWEEP_TAG = "w5_continuous_stage3b_hold_extension_2026-06-14"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage3b-hold-extension.md"
ARM_OVERRIDES = {
    "X0_control": {},
    "XH_hold48": {"hold_hours": 48},
    "XH_hold48_2xcost": {"hold_hours": 48, "round_trip_cost_multiplier": 2.0},
    "XH_hold48_hashplacebo": {"hold_hours": 48, "hash_exit_prob": 0.018},
}
# point the reused Stage 3 _run_arm at THIS stage's overrides + report tag
s3.ARM_OVERRIDES = ARM_OVERRIDES
s3.SWEEP_TAG = SWEEP_TAG
s3.PREREG = PREREG


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage3b_hold_extension.py",
        REPO / "scripts" / "w5_continuous_stage3_exit_alpha.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "trade_lifecycle.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{s3._sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    arms = list(ARM_OVERRIDES.keys())
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "stage3b_hold_extension", "out": str(out),
        "window": {"start": args.start, "end_exclusive": args.end}, "sweep_tag": SWEEP_TAG,
        "git_head": s3._git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": s3.frozen_config_hash(), "run_label": "exploratory",
        "arm_overrides": ARM_OVERRIDES, "arms": arms, "pit_gate": {}, "venues": {},
    }
    metric_rows: list[dict[str, Any]] = []
    reason_rows: list[dict[str, Any]] = []
    for venue in venues:
        root = s3.ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        venue_block: dict[str, Any] = {"arms": {}}
        for arm in arms:
            res = s3._run_arm(root, venue, arm, args.start, args.end)
            venue_block["arms"][arm] = res
            m = res["metrics"]
            metric_rows.append({"venue": venue, "arm": arm, "n_entries": res["n_entries"],
                                "return": m.get("total_return"), "mar": m.get("mar"),
                                "max_dd": m.get("max_drawdown"), "worst_day": m.get("worst_day_return"),
                                "avg_hold_hours": res["avg_hold_hours"],
                                "funding_return": m.get("funding_return")})
            reason_rows.append({"venue": venue, "arm": arm, **res["exit_reasons"]})
        payload["venues"][venue] = venue_block

    _decide(payload)
    _write_csv(out / "stage3b_metrics.csv", metric_rows)
    _write_csv(out / "exit_reasons.csv", reason_rows)
    (out / "stage3b_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage3b_summary.md")
    return payload


def _decide(payload: dict[str, Any]) -> None:
    venues = [v for v, b in payload["venues"].items() if "arms" in b]
    full = s3._pooled_mar_delta(payload, "XH_hold48")
    r2x = s3._pooled_mar_delta(payload, "XH_hold48_2xcost")
    placebo = s3._pooled_mar_delta(payload, "XH_hold48_hashplacebo")
    fails: list[str] = []
    for venue in venues:
        a = payload["venues"][venue]["arms"]["XH_hold48"]
        z = payload["venues"][venue]["arms"]["X0_control"]
        am, zm = a["metrics"], z["metrics"]
        if (am.get("total_return") or 0) <= 0:
            fails.append(f"non-positive return on {venue}")
        if am.get("mar") is not None and zm.get("mar") is not None and (am["mar"] - zm["mar"]) < -0.5:
            fails.append(f"MAR delta < -0.5 on {venue}")
        if am.get("max_drawdown") is not None and zm.get("max_drawdown") is not None and abs(am["max_drawdown"]) > 1.1 * abs(zm["max_drawdown"]):
            fails.append(f"drawdown worse >10% on {venue}")
        if z["n_entries"] and a["n_entries"] != z["n_entries"]:
            fails.append(f"entry count changed on {venue} ({a['n_entries']} vs {z['n_entries']})")
    if full is None or full <= 0.1:
        fails.append("pooled MAR delta <= +0.1")
    if r2x is None or r2x <= 0.1:
        fails.append("2x-cost arm pooled MAR delta <= +0.1")
    if placebo is not None and full is not None and full <= placebo:
        fails.append("not stronger than XH_hold48_hashplacebo control")
    payload["decision"] = {
        "XH_hold48": {"admissible": not fails, "reasons": fails or ["admissible"],
                      "pooled_mar_delta": full, "pooled_mar_delta_2xcost": r2x,
                      "placebo_pooled_mar_delta": placebo}
    }
    payload["verdict_pass"] = not fails


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]["XH_hold48"]
    f = s3._fmt
    lines = [
        "# W5 Continuous Stage 3b — Hold-Extension Exit (24h → 48h)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Frozen config hash: `{payload['frozen_forward_config_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Run label: `{payload['run_label']}`  Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ADMISSIBLE' if payload.get('verdict_pass') else 'NULL'}",
        "",
        f"XH_hold48 pooled ΔMAR vs X0: **{f(d['pooled_mar_delta'])}** "
        f"(2x-cost {f(d['pooled_mar_delta_2xcost'])}; hash-placebo {f(d['placebo_pooled_mar_delta'])}). "
        f"Reasons: {'; '.join(d['reasons'])}.",
        "",
        "| Venue | Arm | Entries | Return | MAR | MaxDD | Avg hold (h) | Funding |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for venue, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            m = a["metrics"]
            lines.append(
                f"| `{venue}` | `{arm}` | {a['n_entries']} | {f(m.get('total_return'), '{:.4f}')} | "
                f"{f(m.get('mar'))} | {f(m.get('max_drawdown'), '{:.4f}')} | "
                f"{f(a.get('avg_hold_hours'), '{:.1f}')} | {f(m.get('funding_return'), '{:.4f}')} |")
    lines.extend(["", "Same entries; only the fixed hold cap moves 24h→48h. Stage 3b is `exploratory`.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--out", default=str(s3.SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict_pass": payload.get("verdict_pass"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
