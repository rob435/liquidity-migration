#!/usr/bin/env python3
"""W6 A4 — squeeze x hedge-intensity overlay (compose with the live BTC-vol regime-hedge).

A1 (squeeze SIZING) was REJECTED (admissible-not-harvestable; the diffuse-edge root cause).
A4 tests the orderflow squeeze signal in the ONLY mode W5 found harvestable: a non-selection
OVERLAY that keeps the whole book and hedges the squeeze tail. We scale the deployed BTC-vol
regime hedge intensity ALSO by a causal, mean-1 AGGREGATE BOOK-SQUEEZE intensity — hedge MORE
on days the book is collectively short crowded (high aggregate OI-buildup) squeezes.

Pre-registration: docs/preregistration/2026-06-15-w6-squeeze-hedge-intensity.md

Component reuse only (Stage-0 frozen component ledgers + the merged hedge layer); no engine
backtests, no data-root writes. Mirrors the Stage-8c robustness harness.

Run:
    POLARS_MAX_THREADS=8 PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python \
        scripts/w6_squeeze_hedge_intensity.py --venues bybit,binance \
        --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
        --out ~/SHARED_DATA/w6_squeeze_hedge_intensity_2026-06-15
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
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# The Stage-8 regime-hedge module imports the erased scripts.w4_continuous_stop_exit_realism
# (COMPONENTS/_btc_inputs/_component_config/_load_component/_monthly_returns/_pit_partition_gate/
# _write_csv). Those now live in scripts._w5_shared — alias it so the load resolves.
import scripts._w5_shared as _w5_shared  # noqa: E402

sys.modules.setdefault("scripts.w4_continuous_stop_exit_realism", _w5_shared)

# A1 squeeze-score machinery (also installs the corrected funding guard — harmless here).
from scripts.w6_squeeze_proxy_sizing import (  # noqa: E402
    MS_PER_DAY,
    _attach_raw_features,
    _thirds,
    _within_symbol_z,
)

_spec = importlib.util.spec_from_file_location(
    "w5_stage8", str(REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py")
)
s8 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s8)  # type: ignore[union-attr]

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
SWEEP_TAG = "w6_squeeze_hedge_intensity_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w6-squeeze-hedge-intensity.md"
BTCVOL_LAM = 0.5            # the LIVE deployed regime (FROZEN_BTCVOL_REGIME)
VOL_WINDOW, PCT_WINDOW = 30, 250
SQ_LAMBDAS = [0.25, 0.5]
SQ_PCT_WINDOW = 250
SQ_WARMUP = 50
COST_1X, COST_15X = 5.0, 7.5  # frozen hedge cost is 5 bps; 7.5 = 1.5x stress
SEEDS = list(range(8))


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w6_squeeze_hedge_intensity.py",
        REPO / "scripts" / "w6_squeeze_proxy_sizing.py",
        REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
    ]
    return hashlib.sha256(
        "|".join(f"{p.name}:{s8._sha256_file(p)}" for p in paths if p.exists()).encode()
    ).hexdigest()


# ----------------------------- aggregate book-squeeze -----------------------------
def _entry_squeeze(stage0_dir: Path, root: Path, venue: str) -> list[tuple[int, int, float]]:
    """(signal_ts, exit_ts, squeeze_z) for unique selected entries with a causal OI squeeze z."""
    tape = pl.read_parquet(stage0_dir / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    df = (
        tape.select("symbol", "signal_ts_ms", "exit_ts_ms")
        .unique(["symbol", "signal_ts_ms"], keep="first")
        .sort(["signal_ts_ms", "symbol"])
    )
    rows = df.to_dicts()
    _attach_raw_features(rows, root, venue)  # attaches oi_chg_24h (causal asof)
    z = _within_symbol_z(rows, "oi_chg_24h")  # causal expanding within-symbol z
    out: list[tuple[int, int, float]] = []
    for i, r in enumerate(rows):
        if z[i] is None or r.get("exit_ts_ms") is None:
            continue
        out.append((int(r["signal_ts_ms"]), int(r["exit_ts_ms"]), float(z[i])))
    return out


def _daily_agg(days: list[int], entries: list[tuple[int, int, float]]) -> dict[int, float | None]:
    """Per-day equal-weighted mean squeeze z over fades ACTIVE that day (causal: entries<=d)."""
    ent = sorted(entries, key=lambda e: e[0])
    starts = np.array([e[0] for e in ent], dtype=np.int64)
    agg: dict[int, float | None] = {}
    for d in days:
        d_end = d + MS_PER_DAY
        hi = int(np.searchsorted(starts, d_end, side="left"))  # entries with signal_ts < d_end
        active = [ent[j][2] for j in range(hi) if ent[j][1] > d]  # exit_ts > d_start
        agg[d] = (sum(active) / len(active)) if active else None
    return agg


def _intensity_from_agg(days: list[int], agg: dict[int, float | None], lam: float,
                        pct_window: int = SQ_PCT_WINDOW, warmup: int = SQ_WARMUP) -> dict[int, float]:
    """Mean-1 percentile intensity 1 + lam*(2*pct-1), causal trailing percentile (like BTC-vol)."""
    dq: deque[float] = deque(maxlen=pct_window)
    out: dict[int, float] = {}
    for d in days:
        v = agg.get(d)
        if v is None or len(dq) < warmup:
            out[d] = 1.0
        else:
            pct = sum(1 for x in dq if x <= v) / len(dq)
            out[d] = 1.0 + lam * (2.0 * pct - 1.0)
        if v is not None:
            dq.append(v)
    return out


def _shuffle_agg(days: list[int], agg: dict[int, float | None], seed: int) -> dict[int, float | None]:
    keys = [d for d in days if agg.get(d) is not None]
    vals = [agg[d] for d in keys]
    perm = np.random.default_rng(seed).permutation(len(vals))
    shuf: dict[int, float | None] = dict(agg)
    for i, d in enumerate(keys):
        shuf[d] = vals[int(perm[i])]
    return shuf


def _compose(a: dict[int, float], b: dict[int, float], days: list[int]) -> dict[int, float]:
    return {d: float(a.get(d, 1.0)) * float(b.get(d, 1.0)) for d in days}


# ----------------------------- run -----------------------------
def _arm_metrics(d: dict[str, Any], intensity: dict[int, float] | None, cost: float) -> dict[str, Any]:
    ledger = s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], intensity, hedge_cost_bps=cost)
    m = s8._metrics(ledger)
    mean_int = float(np.mean([intensity[x] for x in d["days"]])) if intensity else 1.0
    return {"mar": m.get("mar"), "return": m.get("total_return"), "max_dd": m.get("max_drawdown"),
            "mean_intensity": mean_int, "thirds": _thirds(ledger)}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "w6_a4_squeeze_hedge_intensity", "out": str(out),
        "git_head": _git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": s8.frozen_config_hash(), "run_label": "exploratory",
        "btcvol_lam": BTCVOL_LAM, "sq_lambdas": SQ_LAMBDAS, "seeds": SEEDS,
        "stage0_tag": args.stage0_tag, "venues": {}, "coverage": {},
    }
    for venue in venues:
        root = s8.ROOTS[venue]
        pieces = {}
        for comp in s8.COMPONENTS:
            c, _t, _r = s8._load_component(root / "reports" / args.stage0_tag / comp)
            pieces[comp] = c
        days = sorted({x for c in pieces.values() for x in c.days})
        btc_rets, btc_fund = s8._btc_inputs(root, venue, days)
        dat = {"pieces": pieces, "days": days, "btc_rets": btc_rets, "btc_fund": btc_fund}

        btcvol = s8._btcvol_intensity(days, btc_rets, BTCVOL_LAM, VOL_WINDOW, PCT_WINDOW)
        entries = _entry_squeeze(stage0_dir, root, venue)
        agg = _daily_agg(days, entries)
        n_cov = sum(1 for d in days if agg.get(d) is not None)
        payload["coverage"][venue] = {"n_entries": len(entries), "n_days": len(days),
                                      "n_days_with_squeeze": n_cov}

        arms: dict[str, dict[int, float] | None] = {
            "C0_none": None,
            "H_btcvol": btcvol,
        }
        for lam in SQ_LAMBDAS:
            arms[f"SH_lam{lam:g}"] = _compose(btcvol, _intensity_from_agg(days, agg, lam), days)
        for seed in SEEDS:
            arms[f"shuffle_s{seed}"] = _compose(
                btcvol, _intensity_from_agg(days, _shuffle_agg(days, agg, seed), 0.5), days)
        arms["hash"] = _compose(btcvol, s8._hash_intensity(days, 0.5), days)

        block: dict[str, Any] = {"arms": {}}
        for name, inten in arms.items():
            block["arms"][name] = _arm_metrics(dat, inten, COST_1X)
        # cost stress (1.5x) on H and the SH cells
        for name in ["H_btcvol", "SH_lam0.25", "SH_lam0.5"]:
            block["arms"][f"{name}_x1.5"] = _arm_metrics(dat, arms[name], COST_15X)
        payload["venues"][venue] = block
        print(f"[{venue}] C0={block['arms']['C0_none']['mar']:.3f} H={block['arms']['H_btcvol']['mar']:.3f} "
              f"SH0.25={block['arms']['SH_lam0.25']['mar']:.3f} SH0.5={block['arms']['SH_lam0.5']['mar']:.3f} "
              f"(squeeze cov {n_cov}/{len(days)} days)", flush=True)

    _decide(payload, venues)
    (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "summary.md", venues)
    return payload


def _mar(payload, venue, arm) -> float | None:
    a = payload["venues"].get(venue, {}).get("arms", {}).get(arm)
    return None if not a or a.get("mar") is None else float(a["mar"])


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    d: dict[str, Any] = {}
    # vs the H (BTC-vol) baseline
    def dvh(venue, arm, htag=""):  # delta SH - H
        return None if (_mar(payload, venue, arm) is None or _mar(payload, venue, f"H_btcvol{htag}") is None) \
            else _mar(payload, venue, arm) - _mar(payload, venue, f"H_btcvol{htag}")
    primary = "SH_lam0.5"
    # gate 1: bybit SH vs H > 0 for both lam
    if "bybit" in venues:
        d["bybit_sh_vs_h"] = {f"SH_lam{lam:g}": dvh("bybit", f"SH_lam{lam:g}") for lam in SQ_LAMBDAS}
        d["gate1_bybit_both_lam_pos"] = all(v is not None and v > 0 for v in d["bybit_sh_vs_h"].values())
    # gate 2: beats shuffle dist + hash on bybit (primary)
    real = dvh("bybit", primary) if "bybit" in venues else None
    sh = [dvh("bybit", f"shuffle_s{s}") for s in payload["seeds"]] if "bybit" in venues else []
    sh = [x for x in sh if x is not None]
    hashd = dvh("bybit", "hash") if "bybit" in venues else None
    d["ctrl_shuffle"] = {"n": len(sh), "mean": (st.mean(sh) if sh else None), "max": (max(sh) if sh else None),
                         "real": real, "beats_max": (None if (real is None or not sh) else real > max(sh))}
    d["ctrl_hash"] = {"real": real, "hash": hashd,
                      "beats": (None if (real is None or hashd is None) else real > hashd)}
    d["gate2_beats_controls"] = bool(d["ctrl_shuffle"]["beats_max"] and d["ctrl_hash"]["beats"])
    # gate 3: >=2/3 thirds SH MAR >= H MAR on bybit
    if "bybit" in venues:
        a = payload["venues"]["bybit"]["arms"].get(primary, {})
        h = payload["venues"]["bybit"]["arms"].get("H_btcvol", {})
        signs = []
        for ta, th in zip(a.get("thirds", []), h.get("thirds", [])):
            if ta.get("mar") is None or th.get("mar") is None:
                signs.append(0)
            else:
                signs.append(1 if ta["mar"] >= th["mar"] else -1)
        d["bybit_thirds_signs"] = signs
        d["gate3_thirds_2of3"] = (sum(1 for s in signs if s > 0) >= 2) if signs else None
    # gate 4: 1.5x hedge cost, bybit SH vs H still > 0
    d["bybit_cost_stress"] = dvh("bybit", "SH_lam0.5_x1.5", "_x1.5") if "bybit" in venues else None
    d["gate4_cost_stress_pos"] = (d["bybit_cost_stress"] is not None and d["bybit_cost_stress"] > 0)
    # gate 5: binance not losing
    if "binance" in venues:
        d["binance_sh_vs_h"] = dvh("binance", primary)
        d["gate5_binance_not_losing"] = (d["binance_sh_vs_h"] is not None and d["binance_sh_vs_h"] >= -0.05)
    # gate 6: mean composed intensity in band
    mis = []
    for v in venues:
        for lam in SQ_LAMBDAS:
            mi = payload["venues"][v]["arms"].get(f"SH_lam{lam:g}", {}).get("mean_intensity")
            if mi is not None:
                mis.append((v, lam, mi))
    d["mean_intensities"] = mis
    d["gate6_intensity_band"] = all(0.95 <= mi <= 1.05 for _, _, mi in mis) if mis else None
    verdict = bool(d.get("gate1_bybit_both_lam_pos") and d.get("gate2_beats_controls")
                   and d.get("gate3_thirds_2of3") and d.get("gate4_cost_stress_pos")
                   and d.get("gate6_intensity_band"))
    if "binance" in venues:
        verdict = verdict and bool(d.get("gate5_binance_not_losing"))
    d["verdict_pass"] = verdict
    payload["decision"] = d
    payload["verdict_pass"] = verdict


def _f(x, fmt="{:+.3f}") -> str:
    return "" if x is None else fmt.format(x)


def _write_markdown(payload: dict[str, Any], path: Path, venues: list[str]) -> None:
    d = payload["decision"]
    lines = [
        "# W6 A4 — Squeeze × hedge-intensity overlay (compose with BTC-vol regime-hedge)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Frozen cfg: `{payload['frozen_forward_config_hash']}`",
        f"- BTC-vol λ `{payload['btcvol_lam']}`  squeeze λ `{payload['sq_lambdas']}`  seeds `{payload['seeds']}`",
        f"- Coverage: `{payload['coverage']}`",
        f"- Run label: `{payload['run_label']}`  Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'PASS (squeeze adds beyond BTC-vol)' if payload.get('verdict_pass') else 'NULL (no add beyond BTC-vol)'}",
        "",
        "## MAR by arm (vs H = BTC-vol baseline; C0 = no regime)",
        "",
        "| Venue | Arm | MAR | ΔMAR vs H | return | maxDD | mean_int |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for v in venues:
        hmar = _mar(payload, v, "H_btcvol")
        for arm, a in payload["venues"][v]["arms"].items():
            dvh = (a["mar"] - hmar) if (a.get("mar") is not None and hmar is not None) else None
            lines.append(f"| `{v}` | `{arm}` | {_f(a.get('mar'), '{:.3f}')} | {_f(dvh)} | "
                         f"{_f(a.get('return'), '{:.4f}')} | {_f(a.get('max_dd'), '{:.4f}')} | "
                         f"{_f(a.get('mean_intensity'), '{:.3f}')} |")
    lines += ["", "## Decision (bybit-primary, vs H baseline)", ""]
    lines.append(f"- gate1 bybit SH>H both λ: **{d.get('gate1_bybit_both_lam_pos')}**  {{ {', '.join(f'{k}={_f(v)}' for k,v in (d.get('bybit_sh_vs_h') or {}).items())} }}")
    lines.append(f"- gate2 beats controls: **{d.get('gate2_beats_controls')}**  shuffle(real={_f(d['ctrl_shuffle'].get('real'))} max={_f(d['ctrl_shuffle'].get('max'))} mean={_f(d['ctrl_shuffle'].get('mean'))} beats_max={d['ctrl_shuffle'].get('beats_max')}); hash(real>{_f(d['ctrl_hash'].get('hash'))}={d['ctrl_hash'].get('beats')})")
    lines.append(f"- gate3 thirds ≥2/3 SH≥H: **{d.get('gate3_thirds_2of3')}**  signs={d.get('bybit_thirds_signs')}")
    lines.append(f"- gate4 1.5× hedge cost SH>H: **{d.get('gate4_cost_stress_pos')}**  Δ={_f(d.get('bybit_cost_stress'))}")
    if "binance" in venues:
        lines.append(f"- gate5 binance not losing: **{d.get('gate5_binance_not_losing')}**  Δ={_f(d.get('binance_sh_vs_h'))}")
    lines.append(f"- gate6 mean intensity in band: **{d.get('gate6_intensity_band')}**")
    lines += ["", "Component reuse (no engine); hedge-only, all trades kept. A pass nominates a",
              "demo/paper forward-watch overlay on top of the live BTC-vol hedge; Tier-3 UNCHANGED.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--stage0", default=str(SHARED / "w5_continuous_stage0_candidate_tape_2026-06-14"))
    parser.add_argument("--stage0-tag", default="w5_continuous_stage0_candidate_tape_2026-06-14", dest="stage0_tag")
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict_pass": payload.get("verdict_pass"),
                      "gate1": payload["decision"].get("gate1_bybit_both_lam_pos"),
                      "bybit_sh_vs_h": payload["decision"].get("bybit_sh_vs_h")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
