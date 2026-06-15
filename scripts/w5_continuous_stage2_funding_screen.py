#!/usr/bin/env python3
"""W5 Stage 2 (screen) - causal entry-funding as a fade-selection signal.

The continuous book SHORTS the most-pumped alts; pumped alts usually carry HIGH POSITIVE
funding (crowded longs pay shorts), so the fade short receives carry AND high funding may
proxy crowding → stronger reversion. Funding is causal (known at entry) and time-varying
(so, unlike path-shape, it is NOT a symbol-identity proxy). Before any engine test, ask the
cheap question: does the causal entry-funding rate predict realized per-notional return
WITHIN symbol, BEYOND the production composite, on BOTH venues?

Statistic = Stage 7b's within-symbol partial rank-IC over composite (1000-perm within-symbol
null, degenerate symbol-hash control). Funding at entry = the last funding observation at or
before `signal_ts` (causal asof-join). `exploratory`. Admissible (both venues, same sign,
p<0.025, ≥2/3 thirds, control degenerate) ⇒ worth an engine funding-weighted-sizing test
(constant breadth, gross-neutral, vs a symbol-identity control + multi-λ robustness); else
funding-selection is closed.

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage2_funding_screen.py \
        --venues bybit,binance --permutations 1000 --seed 0 \
        --out ~/SHARED_DATA/w5_continuous_stage2_funding_screen_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, str(REPO / "scripts" / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


s7 = _load("w5_stage7", "w5_continuous_stage7_path_shape_neutralized.py")
s7b = _load("w5_stage7b", "w5_continuous_stage7b_within_symbol.py")

from liquidity_migration.continuous_forward_replay import frozen_config_hash  # noqa: E402

SWEEP_TAG = "w5_continuous_stage2_funding_screen_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage2-funding.md"
ROOTS = s7.ROOTS
FUNDING_DIR = {"bybit": "funding", "binance": "binance_usdm_funding"}
P_THRESH = 0.025
COVERAGE_FLOOR = 500


def _entry_funding(root: Path, venue: str, entries: list[dict[str, Any]]) -> dict[tuple[str, int], float]:
    """Causal asof: last funding_rate at or before signal_ts, per (symbol, signal_ts)."""
    fdir = root / FUNDING_DIR[venue]
    syms = sorted({str(r["symbol"]) for r in entries})
    lo = min(int(r["signal_ts_ms"]) for r in entries) - 30 * 86_400_000
    hi = max(int(r["signal_ts_ms"]) for r in entries) + 86_400_000
    fund = (pl.scan_parquet(str(fdir / "**" / "*.parquet"), hive_partitioning=True)
            .select("ts_ms", "symbol", "funding_rate")
            .filter((pl.col("ts_ms") >= lo) & (pl.col("ts_ms") <= hi) & pl.col("symbol").is_in(syms))
            .collect()
            .with_columns(pl.col("ts_ms").cast(pl.Int64), pl.col("funding_rate").cast(pl.Float64))
            .drop_nulls("funding_rate").sort("ts_ms"))
    ent = (pl.DataFrame({"symbol": [str(r["symbol"]) for r in entries],
                         "signal_ts_ms": [int(r["signal_ts_ms"]) for r in entries]})
           .unique().sort("signal_ts_ms"))
    joined = ent.join_asof(fund, left_on="signal_ts_ms", right_on="ts_ms", by="symbol", strategy="backward")
    out: dict[tuple[str, int], float] = {}
    for s, t, fr in joined.select("symbol", "signal_ts_ms", "funding_rate").iter_rows():
        if fr is not None:
            out[(str(s), int(t))] = float(fr)
    return out


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "stage2_funding_screen", "out": str(out), "sweep_tag": SWEEP_TAG,
        "frozen_forward_config_hash": frozen_config_hash(), "run_label": "exploratory",
        "permutations": args.permutations, "seed": args.seed,
        "statistic": "within_symbol_partial_spearman_over_composite", "venues": {},
    }
    ic_rows: list[dict[str, Any]] = []
    per_venue: dict[str, Any] = {}
    arms = ["F_funding", "Q_symbol_hash"]
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        rows = s7._load_population(stage0_dir, root, venue)
        fmap = _entry_funding(root, venue, rows)
        rows = [r for r in rows if (str(r["symbol"]), int(r["signal_ts_ms"])) in fmap
                and r.get("composite") is not None and r.get("return_per_notional") is not None]
        for r in rows:
            r["funding"] = fmap[(str(r["symbol"]), int(r["signal_ts_ms"]))]
        ret = np.array([float(r["return_per_notional"]) for r in rows])
        comp = np.array([float(r["composite"]) for r in rows])
        ts = np.array([int(r["signal_ts_ms"]) for r in rows])
        sym = [str(r["symbol"]) for r in rows]
        counts = Counter(sym)
        kept = np.array([i for i in range(len(rows)) if counts[sym[i]] >= 2])
        n_sym = sum(1 for _s, c in counts.items() if c >= 2)
        feat = {"F_funding": np.array([float(r["funding"]) for r in rows]),
                "Q_symbol_hash": np.array([float(r["symbol_hash_bucket"]) for r in rows])}
        block: dict[str, Any] = {"n_clean": len(rows), "n_kept": int(kept.size), "n_symbols_ge2": n_sym,
                                 "funding_mean": float(np.mean(feat["F_funding"])), "arms": {}}
        for arm in arms:
            res = s7b._arm_within_symbol(feat[arm], ret, comp, kept, sym, ts, args.permutations, args.seed)
            block["arms"][arm] = res
            ic_rows.append({"venue": venue, "arm": arm, "ic_obs": res["ic_obs"], "p_value": res["p_value"],
                            "covered": res["covered"], "null_sd": res["null_sd"], "degenerate": res["degenerate"],
                            "thirds": ";".join(s7b._fmt(t) for t in res.get("thirds", []))})
        per_venue[venue] = block
        payload["venues"][venue] = block

    valid = [v for v in venues if v in per_venue]
    fails: list[str] = []
    if len(valid) < 2:
        fails.append("missing a venue")
    stats = {v: per_venue[v]["arms"].get("F_funding", {}) for v in valid}
    ics = [stats[v].get("ic_obs") for v in valid]
    if any((stats[v].get("covered") or 0) < COVERAGE_FLOOR for v in valid):
        fails.append("coverage<500 a venue")
    if any(i is None for i in ics):
        fails.append("undefined IC a venue")
    elif len({i > 0 for i in ics}) > 1:
        fails.append("IC sign mismatch across venues")
    if any((stats[v].get("p_value") is None or stats[v]["p_value"] >= P_THRESH) for v in valid):
        fails.append(f"perm p>={P_THRESH} a venue")
    for v in valid:
        thirds = [t for t in stats[v].get("thirds", []) if t is not None]
        ic_v = stats[v].get("ic_obs")
        if ic_v is not None and sum(1 for t in thirds if (t > 0) == (ic_v > 0)) < 2:
            fails.append(f"<2/3 thirds {v}")
    payload["control_degenerate"] = {v: per_venue[v]["arms"]["Q_symbol_hash"]["degenerate"] for v in valid}
    payload["admissible"] = (not fails) and all(payload["control_degenerate"].get(v) for v in valid)
    payload["reasons"] = fails or ["admissible"]
    payload["funding_ic"] = {v: stats[v].get("ic_obs") for v in valid}
    payload["funding_p"] = {v: stats[v].get("p_value") for v in valid}

    s7._write_csv(out / "stage2_funding_ic.csv", ic_rows)
    (out / "stage2_funding_screen.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "stage2_funding_screen.md")
    return payload


def _write_md(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W5 Continuous Stage 2 (screen) — causal entry-funding as a fade-selection signal",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  perms `{payload['permutations']}`  seed `{payload['seed']}`",
        f"- Statistic: `{payload['statistic']}`  Pre-reg: `{payload['preregistration']}`",
        "",
        f"## Admissible (both-venue within-symbol funding IC): **{payload['admissible']}** — "
        f"{'; '.join(payload['reasons'])}",
        "",
        f"Symbol-hash control degenerate (must be true): `{payload.get('control_degenerate')}`.",
        f"Funding IC: `{payload.get('funding_ic')}`  p: `{payload.get('funding_p')}`.",
        "",
        "| Venue | Arm | IC | p | null SD | thirds | covered | sym≥2 | funding_mean |",
        "|---|---|---:|---:|---:|---|---:|---:|---:|",
    ]
    for v, b in payload["venues"].items():
        if "arms" not in b:
            continue
        for arm, r in b["arms"].items():
            lines.append(f"| `{v}` | `{arm}` | {s7b._fmt(r['ic_obs'])} | {s7b._fmt(r['p_value'])} | "
                         f"{s7b._fmt(r['null_sd'])} | {';'.join(s7b._fmt(t) for t in r.get('thirds', []))} | "
                         f"{r['covered']} | {b['n_symbols_ge2']} | {b['funding_mean']:.5f} |")
    lines.extend(["", "Screen only (`exploratory`); admissible ⇒ engine funding-weighted-sizing test next.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--stage0", default=str(s7.SHARED / s7.STAGE0_TAG))
    parser.add_argument("--permutations", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=str(s7.SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "admissible": payload["admissible"],
                      "funding_ic": payload["funding_ic"], "funding_p": payload["funding_p"],
                      "reasons": payload["reasons"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
