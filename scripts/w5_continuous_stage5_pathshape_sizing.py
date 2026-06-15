#!/usr/bin/env python3
"""W5 Stage 5 - Path-shape sizing (Z2), gross-neutral, causal.

Sizes the SAME selected trades of the frozen continuous control by the admissible
within-symbol path-shape signal (Stage 7b), via the additive `size_mult_lookup`
hook in continuous_events. Entries / exits / breadth / gates are byte-identical to
the control (the multiplier is applied after every gate); only each trade's
notional changes, and resize/impact cost is recomputed at the new size. The
multiplier is causal (frozen-train within-symbol residual standardization) and
gross-neutral (bounded mean-1 tilt). Tests whether the within-symbol path-shape
signal is harvestable as a risk reallocation on pooled MAR vs the control.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage5-pathshape-sizing.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage5_pathshape_sizing.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
        --kappa 0.25 --clamp 2.0 \
        --out ~/SHARED_DATA/w5_continuous_stage5_pathshape_sizing_2026-06-14
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
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
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
STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
SWEEP_TAG = "w5_continuous_stage5_pathshape_sizing_2026-06-14"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage5-pathshape-sizing.md"
PATH_FEATURES = ("pre_6h_return", "pre_24h_return", "pre_24h_realized_vol")
TRAIN_FRACTION = 0.60
# arm -> (feature_spec, kind)
ARMS = {
    "Z0_control_size": (None, "control"),
    "Z2_pre24_within_symbol": ("pre_24h_return", "within_symbol"),
    "Z2_combined_within_symbol": ("Q_combined", "within_symbol"),
    "Z6_symbol_identity": ("symbol_hash_bucket", "cross_sectional"),
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
        REPO / "scripts" / "w5_continuous_stage5_pathshape_sizing.py",
        REPO / "scripts" / "w5_continuous_stage0_candidate_tape.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _date_to_ms(date: str) -> int:
    return int(dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


# ----------------------------- size lookup builders -----------------------------
def _selected_rows(stage0_dir: Path, venue: str) -> list[dict[str, Any]]:
    tape = pl.read_parquet(stage0_dir / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    cols = ["symbol", "signal_ts_ms", "composite", "symbol_hash_bucket", *PATH_FEATURES]
    rows = tape.select([c for c in cols if c in tape.columns]).to_dicts()
    rows.sort(key=lambda r: (int(r["signal_ts_ms"]), str(r["symbol"])))
    return rows


def _train_mask(n: int) -> list[bool]:
    cut = int(round(n * TRAIN_FRACTION))
    return [i < cut for i in range(n)]


def _feature_values(rows: list[dict[str, Any]], feature_spec: str, train: list[bool]) -> list[float | None]:
    if feature_spec in PATH_FEATURES or feature_spec == "symbol_hash_bucket":
        return [None if r.get(feature_spec) is None else float(r[feature_spec]) for r in rows]
    if feature_spec == "Q_combined":
        # equal-weight average of the three features' frozen-train cross-sectional z-scores
        zs: list[list[float | None]] = []
        for feat in PATH_FEATURES:
            vals = [None if r.get(feat) is None else float(r[feat]) for r in rows]
            tr = [vals[i] for i in range(len(rows)) if train[i] and vals[i] is not None]
            mu = st.mean(tr) if tr else 0.0
            sd = st.pstdev(tr) if len(tr) > 1 else 1.0
            sd = sd if sd > 0 else 1.0
            zs.append([None if v is None else (v - mu) / sd for v in vals])
        out: list[float | None] = []
        for i in range(len(rows)):
            triplet = [zs[k][i] for k in range(3)]
            out.append(None if any(t is None for t in triplet) else float(np.mean(triplet)))
        return out
    raise ValueError(f"unknown feature_spec {feature_spec!r}")


def _month_of(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def _normalize_prior_month(
    raw: dict[tuple[str, int], tuple[float, bool]], meta: dict[str, Any]
) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    """Divide every raw multiplier by the PRIOR calendar month's mean raw multiplier
    (fully causal — no within-month look-ahead) so the deployed gross tracks ~1 even as
    the path-shape feature trends. The first month uses 1.0. MAR is scale-invariant, so
    the small residual creep a causal normalizer leaves against a real feature uptrend is
    not material leverage; the binding check is the realized ledger notional ratio."""
    items = list(raw.items())  # dict insertion order == (ts, symbol) order
    month_ms: dict[str, list[float]] = defaultdict(list)
    for key, (m, _t) in items:
        month_ms[_month_of(key[1])].append(m)
    months = sorted(month_ms)
    mean_by = {mon: (st.mean(month_ms[mon]) if month_ms[mon] else 1.0) for mon in months}
    prior = {months[i]: mean_by[months[i - 1]] for i in range(1, len(months))}
    if months:
        prior[months[0]] = 1.0
    lookup: dict[tuple[str, int], float] = {}
    for key, (m, _t) in items:
        norm = prior.get(_month_of(key[1]), 1.0)
        lookup[key] = m / (norm if norm > 0 else 1.0)
    all_m = list(lookup.values())
    test_m = [lookup[k] for k, (_m, t) in items if not t]
    meta.update({
        "n_tilted": len(lookup), "mean_mult": (st.mean(all_m) if all_m else 1.0),
        "test_mean_mult": (st.mean(test_m) if test_m else 1.0),
        "min_mult": (min(all_m) if all_m else 1.0), "max_mult": (max(all_m) if all_m else 1.0),
    })
    return lookup, meta


def _within_symbol_lookup(rows, fvals, train, kappa, clamp) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    sym_train: dict[str, list[float]] = defaultdict(list)
    for i, r in enumerate(rows):
        if train[i] and fvals[i] is not None:
            sym_train[str(r["symbol"])].append(fvals[i])
    mu_sym = {s: st.mean(v) for s, v in sym_train.items() if v}
    resid = [fvals[i] - mu_sym[str(rows[i]["symbol"])] for i in range(len(rows))
             if train[i] and fvals[i] is not None and str(rows[i]["symbol"]) in mu_sym]
    sigma = st.pstdev(resid) if len(resid) > 1 else 1.0
    sigma = sigma if sigma > 0 else 1.0
    raw: dict[tuple[str, int], tuple[float, bool]] = {}
    for i, r in enumerate(rows):
        s = str(r["symbol"])
        if fvals[i] is None or s not in mu_sym:
            continue  # no within-symbol baseline -> engine default m=1
        z = (fvals[i] - mu_sym[s]) / sigma
        m = min(max(1.0 + kappa * z, 1.0 / clamp), clamp)
        raw[(s, int(r["signal_ts_ms"]))] = (m, bool(train[i]))
    return _normalize_prior_month(raw, {"sigma": sigma, "n_train_symbols": len(mu_sym)})


def _cross_sectional_lookup(rows, fvals, train, kappa, clamp) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    tr = [fvals[i] for i in range(len(rows)) if train[i] and fvals[i] is not None]
    mu = st.mean(tr) if tr else 0.0
    sigma = st.pstdev(tr) if len(tr) > 1 else 1.0
    sigma = sigma if sigma > 0 else 1.0
    raw: dict[tuple[str, int], tuple[float, bool]] = {}
    for i, r in enumerate(rows):
        if fvals[i] is None:
            continue
        z = (fvals[i] - mu) / sigma
        m = min(max(1.0 + kappa * z, 1.0 / clamp), clamp)
        raw[(str(r["symbol"]), int(r["signal_ts_ms"]))] = (m, bool(train[i]))
    return _normalize_prior_month(raw, {"mu": mu, "sigma": sigma})


def _build_lookup(rows, arm, kappa, clamp) -> tuple[dict[tuple[str, int], float] | None, dict[str, Any]]:
    feature_spec, kind = ARMS[arm]
    if kind == "control":
        return None, {"kind": "control"}
    train = _train_mask(len(rows))
    fvals = _feature_values(rows, feature_spec, train)
    if kind == "within_symbol":
        lk, meta = _within_symbol_lookup(rows, fvals, train, kappa, clamp)
    else:
        lk, meta = _cross_sectional_lookup(rows, fvals, train, kappa, clamp)
    meta["kind"] = kind
    meta["feature"] = feature_spec
    return lk, meta


# ----------------------------- metrics helpers -----------------------------
def _subperiod_metrics(ledger: pl.DataFrame, cut_ts: int) -> dict[str, Any]:
    sub = ledger.filter(pl.col("ts_ms") >= cut_ts).sort("ts_ms")
    if sub.is_empty():
        return {"mar": None, "total_return": 0.0, "max_drawdown": 0.0, "n_days": 0}
    sub = sub.with_columns((1.0 + pl.col("basket_return").cum_sum()).alias("equity"))
    sub = sub.with_columns((pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown"))
    m = _daily_pnl_metrics(sub.select("ts_ms", "equity", "drawdown", "basket_return"))
    m["n_days"] = int(sub.height)
    return m


def _concentration(pieces_trades: dict[str, pl.DataFrame]) -> dict[str, Any]:
    sym_notional: dict[str, float] = defaultdict(float)
    total = 0.0
    for trades in pieces_trades.values():
        if trades.is_empty():
            continue
        for sym, nw in trades.select("symbol", "notional_weight").iter_rows():
            w = abs(float(nw))
            sym_notional[str(sym)] += w
            total += w
    if total <= 0:
        return {"total_notional": 0.0, "hhi": None, "top5_share": None, "n_symbols": 0, "top_symbol_share": None}
    shares = sorted((v / total for v in sym_notional.values()), reverse=True)
    return {"total_notional": total, "hhi": float(sum(s * s for s in shares)),
            "top5_share": float(sum(shares[:5])), "top_symbol_share": float(shares[0]),
            "n_symbols": len(shares)}


def _run_arm(root: Path, venue: str, arm: str, lookup, start: str, end: str, cut_ts: int) -> dict[str, Any]:
    pieces: dict[str, Any] = {}
    pieces_trades: dict[str, pl.DataFrame] = {}
    cfg_hashes: dict[str, str] = {}
    t0 = time.time()
    for component, (_artifact_root, cell) in COMPONENTS.items():
        report_dir = root / "reports" / SWEEP_TAG / arm / component
        cfg = _component_config(component, cell, start=start, end=end, arm_overrides={})
        cfg_hashes[component] = cfg.config_hash()
        run_continuous_event_research(root, config=cfg, report_dir=report_dir, size_mult_lookup=lookup)
        comp, trades, _report = _load_component(report_dir)
        pieces[component] = comp
        pieces_trades[component] = trades
    all_days = sorted({day for comp in pieces.values() for day in comp.days})
    btc_rets, btc_funding = _btc_inputs(root, venue, all_days)
    ledger = build_full_ledger(pieces, btc_rets, btc_funding)
    arm_dir = root / "reports" / SWEEP_TAG / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
    monthly = _monthly_returns(ledger)
    monthly.write_csv(arm_dir / "volume_event_best_monthly.csv")
    full = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    test = _subperiod_metrics(ledger, cut_ts)
    conc = _concentration(pieces_trades)
    report = {
        "preregistration": PREREG, "run_label": "exploratory", "arm": arm, "venue": venue,
        "date_range": {"start": start, "end": end},
        "best_scenario": {"total_return": full.get("total_return", 0.0), "max_drawdown": full.get("max_drawdown", 0.0),
                          "sharpe_like": full.get("sharpe_like", 0.0), "mar": full.get("mar"),
                          "trades": int(sum(int(t.height) for t in pieces_trades.values()))},
        "data_root": str(root), "ledger": str(arm_dir / "ensemble_hedged_ledger.csv"),
    }
    (arm_dir / "volume_event_research_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return {
        "arm": arm, "venue": venue, "elapsed_s": round(time.time() - t0, 1),
        "n_trades": int(sum(int(t.height) for t in pieces_trades.values())),
        "n_ledger_days": int(ledger.height), "n_months": int(monthly.height),
        "config_hashes": cfg_hashes, "full": full, "test_fold": test, "concentration": conc,
    }


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "stage5_pathshape_sizing",
        "window": {"start": args.start, "end_exclusive": args.end}, "out": str(out),
        "sweep_tag": SWEEP_TAG, "git_head": _git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": frozen_config_hash(), "run_label": "exploratory",
        "kappa": args.kappa, "clamp": args.clamp, "gross_normalizer": "prior_month_causal",
        "train_fraction": TRAIN_FRACTION,
        "roots": {v: str(ROOTS[v]) for v in venues}, "arms": arms,
        "pit_gate": {}, "venues": {}, "lookup_meta": {},
    }
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        rows = _selected_rows(stage0_dir, venue)
        # test-fold cut: 60th-percentile signal_ts of selected entries
        ts_sorted = sorted(int(r["signal_ts_ms"]) for r in rows)
        cut_ts = ts_sorted[int(round(len(ts_sorted) * TRAIN_FRACTION))] if ts_sorted else _date_to_ms(args.start)
        payload["venues"][venue] = {"n_selected": len(rows), "test_cut_ts_ms": cut_ts, "arms": {}}
        payload["lookup_meta"].setdefault(venue, {})
        for arm in arms:
            lookup, meta = _build_lookup(rows, arm, args.kappa, args.clamp)
            payload["lookup_meta"][venue][arm] = meta
            if lookup:
                _write_csv(out / f"size_mult_{arm}_{venue}.csv",
                           [{"symbol": k[0], "signal_ts_ms": k[1], "mult": v} for k, v in lookup.items()])
            res = _run_arm(root, venue, arm, lookup, args.start, args.end, cut_ts)
            payload["venues"][venue]["arms"][arm] = res

    _decide(payload)
    (out / "config_hashes.json").write_text(json.dumps(
        {v: b.get("arms", {}).get(arms[0], {}).get("config_hashes", {}) for v, b in payload["venues"].items()},
        indent=2, default=str), encoding="utf-8")
    _write_tables(payload, out)
    (out / "stage5_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage5_summary.md")
    return payload


def _pooled_mar_delta(payload, arm, field) -> float | None:
    deltas = []
    for venue, block in payload["venues"].items():
        if "arms" not in block:
            continue
        a = block["arms"].get(arm, {}).get(field, {})
        z0 = block["arms"].get("Z0_control_size", {}).get(field, {})
        if a.get("mar") is None or z0.get("mar") is None:
            return None
        deltas.append(float(a["mar"]) - float(z0["mar"]))
    return float(sum(deltas) / len(deltas)) if deltas else None


def _decide(payload: dict[str, Any]) -> None:
    decisions: dict[str, Any] = {}
    venues = [v for v, b in payload["venues"].items() if "arms" in b]
    z6_pooled = _pooled_mar_delta(payload, "Z6_symbol_identity", "full")
    for arm in payload["arms"]:
        if arm in ("Z0_control_size", "Z6_symbol_identity"):
            continue
        fails: list[str] = []
        full_pooled = _pooled_mar_delta(payload, arm, "full")
        test_pooled = _pooled_mar_delta(payload, arm, "test_fold")
        per_v = {}
        for venue in venues:
            a = payload["venues"][venue]["arms"][arm]
            z0 = payload["venues"][venue]["arms"]["Z0_control_size"]
            mar_d = (None if a["full"].get("mar") is None or z0["full"].get("mar") is None
                     else float(a["full"]["mar"]) - float(z0["full"]["mar"]))
            ret = a["full"].get("total_return")
            dd_a, dd_0 = a["full"].get("max_drawdown"), z0["full"].get("max_drawdown")
            lm = payload["lookup_meta"].get(venue, {}).get(arm, {})
            a_notional = a["concentration"].get("total_notional")
            z0_notional = z0["concentration"].get("total_notional")
            gross_ratio = (a_notional / z0_notional) if (a_notional and z0_notional) else None
            per_v[venue] = {"mar_delta": mar_d, "return": ret, "dd_arm": dd_a, "dd_ctrl": dd_0,
                            "gross_ratio": gross_ratio, "mean_mult": lm.get("mean_mult"),
                            "test_mean_mult": lm.get("test_mean_mult")}
            if ret is not None and ret <= 0:
                fails.append(f"non-positive return on {venue}")
            if mar_d is not None and mar_d < -0.5:
                fails.append(f"MAR delta < -0.5 on {venue}")
            if dd_a is not None and dd_0 is not None and abs(dd_a) > 1.2 * abs(dd_0):
                fails.append(f"drawdown worse >20% on {venue}")
            if gross_ratio is not None and not (0.95 <= gross_ratio <= 1.05):
                fails.append(f"realized gross ratio {gross_ratio:.3f} outside [0.95,1.05] on {venue} (leverage)")
        if full_pooled is None or full_pooled <= 0.1:
            fails.append("full-window pooled MAR delta <= +0.1")
        if test_pooled is None or test_pooled <= 0.0:
            fails.append("test-fold (OOS) pooled MAR delta <= 0")
        if z6_pooled is not None and full_pooled is not None and full_pooled <= z6_pooled:
            fails.append("not stronger than Z6 symbol-identity control")
        decisions[arm] = {"admissible": not fails, "reasons": fails or ["admissible"],
                          "full_pooled_mar_delta": full_pooled, "test_pooled_mar_delta": test_pooled,
                          "z6_pooled_mar_delta": z6_pooled, "per_venue": per_v}
    payload["decision"] = decisions
    payload["verdict_pass"] = any(d["admissible"] for d in decisions.values())


def _write_tables(payload: dict[str, Any], out: Path) -> None:
    rows = []
    for venue, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            rows.append({"venue": venue, "arm": arm, "n_trades": a["n_trades"],
                         "full_return": a["full"].get("total_return"), "full_mar": a["full"].get("mar"),
                         "full_maxdd": a["full"].get("max_drawdown"),
                         "test_return": a["test_fold"].get("total_return"), "test_mar": a["test_fold"].get("mar"),
                         "mean_mult": payload["lookup_meta"].get(venue, {}).get(arm, {}).get("mean_mult"),
                         "total_notional": a["concentration"].get("total_notional"),
                         "hhi": a["concentration"].get("hhi"), "top5_share": a["concentration"].get("top5_share")})
    _write_csv(out / "stage5_metrics.csv", rows)
    _write_csv(out / "gross_exposure.csv", [
        {"venue": v, "arm": arm, "mean_mult": payload["lookup_meta"].get(v, {}).get(arm, {}).get("mean_mult"),
         "total_notional": a["concentration"].get("total_notional")}
        for v, b in payload["venues"].items() for arm, a in b.get("arms", {}).items()])
    _write_csv(out / "concentration.csv", [
        {"venue": v, "arm": arm, **a["concentration"]}
        for v, b in payload["venues"].items() for arm, a in b.get("arms", {}).items()])


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W5 Continuous Stage 5 — Path-Shape Sizing (Z2, gross-neutral, causal)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Git HEAD: `{payload['git_head']}`  Code hash: `{payload['code_hash']}`",
        f"- Frozen forward config hash: `{payload['frozen_forward_config_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- kappa `{payload['kappa']}`  clamp `{payload['clamp']}`  train frac `{payload['train_fraction']}`",
        f"- Run label: `{payload['run_label']}`  Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ADMISSIBLE (>=1 arm)' if payload.get('verdict_pass') else 'NULL (no arm admissible)'}",
        "",
        "## Per-arm decision (vs Z0 control)",
        "",
        "| Arm | Full pooled ΔMAR | Test-fold pooled ΔMAR | Z6 pooled ΔMAR | Admissible | Reasons |",
        "|---|---:|---:|---:|---|---|",
    ]

    def _f(x):
        return "" if x is None else f"{x:+.3f}"
    for arm, d in payload.get("decision", {}).items():
        lines.append(f"| `{arm}` | {_f(d['full_pooled_mar_delta'])} | {_f(d['test_pooled_mar_delta'])} | "
                     f"{_f(d['z6_pooled_mar_delta'])} | {d['admissible']} | {'; '.join(d['reasons'])} |")
    def _g(x: float | None, fmt: str = "{:.3f}") -> str:
        return "" if x is None else fmt.format(x)
    lines.extend(["", "## Per-venue per-arm (full window)", "",
                  "| Venue | Arm | Trades | Return | MAR | MaxDD | Test MAR | Mean mult | Top5 share |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for venue, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            mm = payload["lookup_meta"].get(venue, {}).get(arm, {}).get("mean_mult")
            fm = a["full"]
            tf = a["test_fold"]
            cc = a["concentration"]
            lines.append(
                f"| `{venue}` | `{arm}` | {a['n_trades']} | "
                f"{_g(fm.get('total_return'), '{:.4f}')} | {_g(fm.get('mar'))} | "
                f"{_g(fm.get('max_drawdown'), '{:.4f}')} | {_g(tf.get('mar'))} | "
                f"{_g(mm)} | {_g(cc.get('top5_share'))} |")
    lines.extend(["", "Z2 sizes the SAME trades by the causal within-symbol path-shape residual,",
                  "gross-neutral. Stage 5 is `exploratory`; a pass nominates a demo/paper shadow only.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--stage0", default=str(SHARED / STAGE0_TAG))
    parser.add_argument("--arms", default=",".join(ARMS.keys()))
    parser.add_argument("--kappa", type=float, default=0.25)
    parser.add_argument("--clamp", type=float, default=2.0)
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict_pass": payload.get("verdict_pass"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
