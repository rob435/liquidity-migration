#!/usr/bin/env python3
"""Orderflow squeeze-proxy screen — EXPLORATORY within-symbol IC of OI/funding/premium
dislocation features on the continuous fade book's selected entries.

Motivation: W5 closed the price/return lever space (PROGRAM_REPORT), but explicitly
left the OI/liquidation/depth "squeeze proxy" as the credible untested orderflow leg
(P10 found RAW taker-flow composition null; the informative leg is squeeze context).
Thesis: the book SHORTS the pumped name; a pump built on a CROWDED long (OI buildup +
extreme funding + stretched premium) is a squeeze that should fade harder/cleaner, so
these causal pre-entry features should carry a positive within-symbol selection signal
BEYOND the production composite.

This is a SCREEN, not a decision (run_label=exploratory). Statistic is exactly the
Stage 7b / Stage 4-screen one: within-symbol partial rank-IC vs realized per-notional
return, controlling for composite, with a within-symbol permutation null + the
degenerate symbol-hash negative control + chronological thirds. A feature is worth a
downstream engine intervention ONLY if it carries a same-sign, p<0.025, >=2/3-thirds
within-symbol IC with a degenerate symbol-hash control. Admissibility != harvestable
(Stage 7b path-shape was admissible but NOT harvestable in Stage 5 sizing).

Data reality (2026-06-15): bybit OI/funding/premium are deep (2021->); binance OI is
recent-window only (~2026-04 onward), so binance OI features will be low-coverage and
are reported, not relied upon. Causal: every feature reads the last layer value at or
before the signal bar; lookbacks use values strictly earlier.

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w6_squeeze_proxy_screen.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
        --permutations 1000 --seed 0 \
        --out ~/SHARED_DATA/w6_squeeze_proxy_screen_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def _load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, str(REPO / "scripts" / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# The W5 stage7 helper imports scripts.w4_continuous_stop_exit_realism._pit_partition_gate,
# but the W4 scripts were purged (erasure order, commit 8aef4b7). That PIT-gate helper is
# NOT used by the within-symbol IC helpers we reuse here, so stub the deleted module to
# unblock the import without resurrecting dead code.
import types  # noqa: E402

_w4_stub = types.ModuleType("scripts.w4_continuous_stop_exit_realism")
_w4_stub._pit_partition_gate = lambda *a, **k: {}  # type: ignore[attr-defined]
sys.modules.setdefault("scripts.w4_continuous_stop_exit_realism", _w4_stub)

s7 = _load_module("w5_stage7", "w5_continuous_stage7_path_shape_neutralized.py")
s7b = _load_module("w5_stage7b", "w5_continuous_stage7b_within_symbol.py")

from liquidity_migration.continuous_forward_replay import frozen_config_hash  # noqa: E402

ROOTS = s7.ROOTS
COMPONENTS = s7.COMPONENTS
MS_PER_HOUR = s7.MS_PER_HOUR
MS_PER_DAY = 24 * MS_PER_HOUR
P_THRESH = 0.025
COVERAGE_FLOOR = 500

# Per-venue layer dir + value column for each raw series.
LAYERS = {
    "bybit": {
        "oi": ("open_interest", "open_interest"),
        "funding": ("funding", "funding_rate_8h_equiv"),
        "premium": ("premium_index_1h", "close"),
    },
    "binance": {
        "oi": ("binance_usdm_open_interest", "open_interest"),
        "funding": ("binance_usdm_funding", "funding_rate"),
        "premium": ("binance_usdm_premium_index_1h", "close"),
    },
}
FEATURES = ("oi_chg_24h", "oi_chg_6h", "funding_level", "premium_level", "premium_chg_24h")


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _load_series(root: Path, layer_dir: str, value_col: str, symbols: set[str],
                 lo_ms: int, hi_ms: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per-symbol (sorted ts, value) arrays for one raw layer, bounded to the window."""
    base = root / layer_dir
    if not base.is_dir():
        return {}
    lf = (
        pl.scan_parquet(str(base / "**" / "*.parquet"))
        .select("ts_ms", "symbol", pl.col(value_col).alias("v"))
        .filter(pl.col("symbol").is_in(list(symbols)) & pl.col("ts_ms").is_between(lo_ms, hi_ms))
        .drop_nulls("v")
        .sort("ts_ms")
    )
    df = lf.collect()
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sym, sub in df.group_by("symbol"):
        ts = sub["ts_ms"].to_numpy()
        out[str(sym[0] if isinstance(sym, tuple) else sym)] = (ts, sub["v"].to_numpy())
    return out


def _asof(series: dict[str, tuple[np.ndarray, np.ndarray]], symbol: str, anchor: int) -> float | None:
    """Last value at or before ``anchor`` for ``symbol`` (causal); None if none exists."""
    pair = series.get(symbol)
    if pair is None:
        return None
    ts, val = pair
    i = int(np.searchsorted(ts, anchor, side="right")) - 1
    if i < 0:
        return None
    return float(val[i])


def _population(stage0_dir: Path, root: Path, venue: str) -> list[dict[str, Any]]:
    """Selected entries with composite, symbol-hash control, and realized per-notional return."""
    tape = pl.read_parquet(stage0_dir / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    rows: list[dict[str, Any]] = []
    for component in COMPONENTS:
        comp = tape.filter(pl.col("component") == component)
        if comp.is_empty():
            continue
        ledger = pl.read_csv(s7._find_trades_csv(root, component))
        ret_map: dict[tuple[str, int], tuple[float, float]] = {}
        for sym, sig, nr, nw in ledger.select(
            "symbol", "entry_signal_ts_ms", "net_return", "notional_weight"
        ).iter_rows():
            ret_map[(str(sym), int(sig))] = (float(nr), abs(float(nw)))
        for r in comp.iter_rows(named=True):
            key = (str(r["symbol"]), int(r["signal_ts_ms"]))
            if key not in ret_map or r.get("composite") is None or r.get("symbol_hash_bucket") is None:
                continue
            nr, nw = ret_map[key]
            rows.append({
                "symbol": str(r["symbol"]), "signal_ts_ms": int(r["signal_ts_ms"]),
                "composite": float(r["composite"]), "symbol_hash_bucket": float(r["symbol_hash_bucket"]),
                "return_per_notional": nr / max(nw, 1e-12),
            })
    rows.sort(key=lambda d: (d["signal_ts_ms"], d["symbol"]))
    return rows


def _attach_features(rows: list[dict[str, Any]], root: Path, venue: str) -> None:
    symbols = {r["symbol"] for r in rows}
    lo = min(r["signal_ts_ms"] for r in rows) - 25 * MS_PER_HOUR
    hi = max(r["signal_ts_ms"] for r in rows) + MS_PER_HOUR
    cfg = LAYERS[venue]
    oi = _load_series(root, *cfg["oi"], symbols, lo, hi)
    fund = _load_series(root, *cfg["funding"], symbols, lo, hi)
    prem = _load_series(root, *cfg["premium"], symbols, lo, hi)
    for r in rows:
        s, t = r["symbol"], r["signal_ts_ms"]
        oi_now, oi_6, oi_24 = _asof(oi, s, t), _asof(oi, s, t - 6 * MS_PER_HOUR), _asof(oi, s, t - MS_PER_DAY)
        r["oi_chg_24h"] = (oi_now / oi_24 - 1.0) if (oi_now and oi_24 and oi_24 > 0) else None
        r["oi_chg_6h"] = (oi_now / oi_6 - 1.0) if (oi_now and oi_6 and oi_6 > 0) else None
        r["funding_level"] = _asof(fund, s, t)
        p_now, p_24 = _asof(prem, s, t), _asof(prem, s, t - MS_PER_DAY)
        r["premium_level"] = p_now
        r["premium_chg_24h"] = (p_now - p_24) if (p_now is not None and p_24 is not None) else None


def _arm(rows: list[dict[str, Any]], feature: str, perms: int, seed: int) -> dict[str, Any]:
    """Within-symbol partial rank-IC for one feature on its own clean subset."""
    sub = [r for r in rows if r.get(feature) is not None]
    if not sub:
        return {"covered": 0, "ic_obs": None, "p_value": None, "null_sd": None,
                "degenerate": None, "thirds": []}
    sym = [r["symbol"] for r in sub]
    counts = Counter(sym)
    kept = np.array([i for i in range(len(sub)) if counts[sym[i]] >= 2])
    score = np.array([float(r[feature]) for r in sub])
    ret = np.array([float(r["return_per_notional"]) for r in sub])
    comp = np.array([float(r["composite"]) for r in sub])
    ts = np.array([int(r["signal_ts_ms"]) for r in sub])
    return s7b._arm_within_symbol(score, ret, comp, kept, sym, ts, perms, seed)


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "out": str(out),
        "stage": "w6_squeeze_proxy_screen", "run_label": "exploratory",
        "window": {"start": args.start, "end_exclusive": args.end},
        "git_head": _git_head(), "frozen_forward_config_hash": frozen_config_hash(),
        "statistic": "within_symbol_partial_spearman_over_composite",
        "features": list(FEATURES), "permutations": args.permutations, "seed": args.seed,
        "venues": {},
    }
    ic_rows: list[dict[str, Any]] = []
    per_venue: dict[str, dict[str, Any]] = {}
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        rows = _population(stage0_dir, root, venue)
        _attach_features(rows, root, venue)
        block: dict[str, Any] = {"n_selected": len(rows), "arms": {}}
        for feat in (*FEATURES, "Q_symbol_hash"):
            res = _arm(rows, "symbol_hash_bucket" if feat == "Q_symbol_hash" else feat,
                       args.permutations, args.seed)
            block["arms"][feat] = res
            ic_rows.append({"venue": venue, "feature": feat, **{
                k: res.get(k) for k in ("covered", "ic_obs", "p_value", "null_sd", "degenerate")},
                "thirds": ";".join(s7b._fmt(t) for t in res.get("thirds", []))})
        per_venue[venue] = block
        payload["venues"][venue] = block

    decisions: dict[str, Any] = {}
    valid = [v for v in venues if v in per_venue]
    for feat in FEATURES:
        stats = {v: per_venue[v]["arms"].get(feat, {}) for v in valid}
        fails: list[str] = []
        covered = {v: (stats[v].get("covered") or 0) for v in valid}
        ics = {v: stats[v].get("ic_obs") for v in valid}
        ps = {v: stats[v].get("p_value") for v in valid}
        venues_ok = [v for v in valid if covered[v] >= COVERAGE_FLOOR]
        if not venues_ok:
            fails.append("no venue clears coverage>=500")
        sig_venues = [v for v in venues_ok if ics[v] is not None and ps[v] is not None
                      and ps[v] < P_THRESH]
        if not sig_venues:
            fails.append(f"no covered venue with perm p<{P_THRESH}")
        both = [v for v in venues_ok if ics[v] is not None]
        if len(both) >= 2 and len({ics[v] > 0 for v in both}) > 1:
            fails.append("IC sign mismatch across covered venues")
        decisions[feat] = {
            "covered": covered, "ic": ics, "p_value": ps,
            "signal_on": sig_venues, "lead": bool(sig_venues),
        }
    payload["control_degenerate"] = {
        v: per_venue[v]["arms"]["Q_symbol_hash"].get("degenerate") for v in valid}
    payload["decision"] = decisions
    payload["any_lead"] = any(d["lead"] for d in decisions.values())

    s7._write_csv(out / "squeeze_proxy_ic.csv", ic_rows)
    (out / "squeeze_proxy_screen.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "squeeze_proxy_screen.md")
    return payload


def _write_md(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# Orderflow squeeze-proxy screen (EXPLORATORY)",
        "",
        f"- Generated: `{payload['generated_utc']}`  Git: `{payload['git_head']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Statistic: `{payload['statistic']}`  perms `{payload['permutations']}`  label `{payload['run_label']}`",
        f"- Symbol-hash control degenerate (must hold): `{payload.get('control_degenerate')}`",
        f"- Any lead: **{payload['any_lead']}**",
        "",
        "## Within-symbol partial rank-IC over composite",
        "",
        "| Feature | bybit IC | bybit p | bybit n | binance IC | binance p | binance n | signal_on |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for feat in FEATURES:
        d = payload["decision"][feat]
        ic, p, cov = d["ic"], d["p_value"], d["covered"]
        lines.append(
            f"| `{feat}` | {s7b._fmt(ic.get('bybit'))} | {s7b._fmt(p.get('bybit'))} | {cov.get('bybit', 0)} "
            f"| {s7b._fmt(ic.get('binance'))} | {s7b._fmt(p.get('binance'))} | {cov.get('binance', 0)} "
            f"| {','.join(d['signal_on']) or '-'} |")
    lines += ["", "Screen only (`exploratory`) — a lead must still beat the frozen control on pooled",
              "MAR AND a negative control in a downstream engine intervention, both venues where data",
              "allows, before it is a demo/paper candidate. Admissibility != harvestable.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--start", default="2023-04-01")
    ap.add_argument("--end", default="2026-05-01")
    ap.add_argument("--stage0", default=str(s7.SHARED / s7.STAGE0_TAG))
    ap.add_argument("--permutations", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=str(s7.SHARED / "w6_squeeze_proxy_screen_2026-06-15"))
    args = ap.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "any_lead": payload["any_lead"],
                      "control_degenerate": payload["control_degenerate"],
                      "decision": {k: {"ic": v["ic"], "signal_on": v["signal_on"]}
                                   for k, v in payload["decision"].items()}}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
