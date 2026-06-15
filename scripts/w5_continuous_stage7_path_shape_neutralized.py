#!/usr/bin/env python3
"""W5 Stage 7 - Neutralized path-shape scoring.

Decides whether causal pre-entry path-shape features carry stable, cross-venue
information about per-notional net return AFTER the symbol / component /
time-of-entry mix AND the production composite are removed. W4 Stage 3 found the
raw features admissible but the symbol_hash_bucket negative control had a 97 bps
pooled spread, so the raw effect is confounded by symbol mix. Stage 7 neutralizes
that and decides on the residual.

Population: executed/selected entries of the frozen control from the Stage 0
candidate tape, joined to the rebuilt component ledgers for realized
per-notional net return. Neutralization is fit on a chronological train fold
(earliest 60%), frozen, and applied to the test fold (most recent 40%); all
decisions are on the pooled test fold.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage7-path-shape-neutralized.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage7_path_shape_neutralized.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
        --out ~/SHARED_DATA/w5_continuous_stage7_path_shape_neutralized_2026-06-14
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from liquidity_migration.continuous_forward_replay import frozen_config_hash  # noqa: E402
from scripts.w4_continuous_stop_exit_realism import _pit_partition_gate  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
SWEEP_TAG = "w5_continuous_stage7_path_shape_neutralized_2026-06-14"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage7-path-shape-neutralized.md"
COMPONENTS = ("turn3p3", "turn4p3", "turn4p5", "age210tp14")
COMPONENT_DUMMIES = ("turn4p3", "turn4p5", "age210tp14")  # base = turn3p3
NEUTRALIZED_FEATURES = ("pre_6h_return", "pre_24h_return", "pre_24h_realized_vol")
TRAIN_FRACTION = 0.60
SPREAD_BPS_FLOOR = 25.0
COVERAGE_FLOOR = 500
MS_PER_HOUR = 3_600_000


# ----------------------------- identity helpers -----------------------------
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
        REPO / "scripts" / "w5_continuous_stage7_path_shape_neutralized.py",
        REPO / "scripts" / "w5_continuous_stage0_candidate_tape.py",
        REPO / "scripts" / "w4_continuous_path_shape_measure.py",
        REPO / "liquidity_migration" / "continuous_events.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------- stats helpers --------------------------------
def _avg_rank(values: np.ndarray) -> np.ndarray:
    """Tie-aware average ranks (matches scipy.rankdata 'average')."""
    a = np.asarray(values, dtype=float)
    n = a.size
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(n, dtype=float)
    sa = a[order]
    i = 0
    while i < n:
        j = i + 1
        while j < n and sa[j] == sa[i]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0
        i = j
    return ranks


def _pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    if x.size < 3:
        return None
    sx, sy = x.std(), y.std()
    if sx <= 0.0 or sy <= 0.0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    if x.size < 3:
        return None
    return _pearson(_avg_rank(x), _avg_rank(y))


def _tercile_spread_bps(score: np.ndarray, ret: np.ndarray) -> dict[str, Any]:
    n = score.size
    if n < 9:
        return {"covered": int(n), "spread_bps": None, "bottom_mean_bps": None, "top_mean_bps": None}
    idx = np.argsort(score, kind="mergesort")
    k = max(n // 3, 1)
    bottom = float(ret[idx[:k]].mean()) * 1e4
    top = float(ret[idx[-k:]].mean()) * 1e4
    return {"covered": int(n), "spread_bps": top - bottom, "bottom_mean_bps": bottom, "top_mean_bps": top}


def _winsorized_spread_bps(score: np.ndarray, ret: np.ndarray, p: float = 0.01) -> float | None:
    if score.size < 9:
        return None
    lo, hi = np.quantile(ret, [p, 1 - p])
    rc = np.clip(ret, lo, hi)
    return _tercile_spread_bps(score, rc)["spread_bps"]


def _ols_residual(target: np.ndarray, design: np.ndarray, train_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fit OLS target ~ design on the train rows, return (residual_all, beta)."""
    beta, *_ = np.linalg.lstsq(design[train_mask], target[train_mask], rcond=None)
    residual = target - design @ beta
    return residual, beta


# ----------------------------- population -----------------------------------
def _date_to_ms(date: str) -> int:
    return int(dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _find_trades_csv(root: Path, component: str) -> Path:
    candidate = root / "reports" / STAGE0_TAG / component / "continuous_trades.csv"
    if candidate.exists():
        return candidate
    # fall back to any continuous_trades.csv under the component report dir
    base = root / "reports" / STAGE0_TAG / component
    hits = sorted(base.rglob("continuous_trades.csv"))
    if hits:
        return hits[0]
    raise FileNotFoundError(f"missing component ledger for {component} under {base}")


def _load_population(stage0_dir: Path, root: Path, venue: str) -> list[dict[str, Any]]:
    tape_path = stage0_dir / f"candidate_tape_{venue}.parquet"
    if not tape_path.exists():
        raise FileNotFoundError(f"missing Stage 0 candidate tape: {tape_path}")
    tape = pl.read_parquet(tape_path).filter(pl.col("selected"))
    rows: list[dict[str, Any]] = []
    for component in COMPONENTS:
        comp = tape.filter(pl.col("component") == component)
        if comp.is_empty():
            continue
        ledger = pl.read_csv(_find_trades_csv(root, component))
        ret_map: dict[tuple[str, int], tuple[float, float]] = {}
        for sym, sig, nr, nw in ledger.select(
            "symbol", "entry_signal_ts_ms", "net_return", "notional_weight"
        ).iter_rows():
            ret_map[(str(sym), int(sig))] = (float(nr), abs(float(nw)))
        for r in comp.iter_rows(named=True):
            key = (str(r["symbol"]), int(r["signal_ts_ms"]))
            if key not in ret_map:
                continue  # reconciled at Stage 0; skip defensively
            net_return, notional = ret_map[key]
            sig = int(r["signal_ts_ms"])
            order_submit = sig + MS_PER_HOUR
            rows.append(
                {
                    "venue": venue,
                    "component": component,
                    "symbol": str(r["symbol"]),
                    "signal_ts_ms": sig,
                    "hour": dt.datetime.fromtimestamp(order_submit / 1000, tz=dt.timezone.utc).hour,
                    "month": dt.datetime.fromtimestamp(sig / 1000, tz=dt.timezone.utc).month,
                    "composite": r.get("composite"),
                    "symbol_hash_bucket": r.get("symbol_hash_bucket"),
                    "month_hash_bucket": r.get("month_hash_bucket"),
                    "shuffled_in_fold_composite": r.get("shuffled_in_fold_composite"),
                    "pre_6h_return": r.get("pre_6h_return"),
                    "pre_24h_return": r.get("pre_24h_return"),
                    "pre_24h_realized_vol": r.get("pre_24h_realized_vol"),
                    "return_per_notional": net_return / max(notional, 1e-12),
                }
            )
    rows.sort(key=lambda d: (d["signal_ts_ms"], d["symbol"]))
    return rows


def _design_matrix(rows: list[dict[str, Any]], *, include_symbol_hash: bool) -> np.ndarray:
    n = len(rows)
    cols: list[np.ndarray] = [np.ones(n)]
    for lvl in COMPONENT_DUMMIES:
        cols.append(np.array([1.0 if r["component"] == lvl else 0.0 for r in rows]))
    if include_symbol_hash:
        cols.append(np.array([float(r["symbol_hash_bucket"]) for r in rows]))
    hour = np.array([float(r["hour"]) for r in rows])
    month = np.array([float(r["month"]) for r in rows])
    cols.append(np.sin(2 * np.pi * hour / 24.0))
    cols.append(np.cos(2 * np.pi * hour / 24.0))
    cols.append(np.sin(2 * np.pi * month / 12.0))
    cols.append(np.cos(2 * np.pi * month / 12.0))
    cols.append(np.array([float(r["composite"]) for r in rows]))
    return np.column_stack(cols)


def _clean(rows: list[dict[str, Any]], feature_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    needed = ("composite", "symbol_hash_bucket", "return_per_notional", *feature_keys)
    return [r for r in rows if all(r.get(k) is not None for k in needed)]


def _train_test_masks(rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    n = len(rows)
    cut = int(round(n * TRAIN_FRACTION))
    train = np.zeros(n, dtype=bool)
    train[:cut] = True  # rows are pre-sorted by signal_ts → earliest 60%
    return train, ~train


# ----------------------------- arm residuals --------------------------------
def _residual_scores(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build frozen-train residual scores for every arm on the test fold.

    Returns per-arm dict: {test_residual, test_return, test_ts, beta, train/test sizes}.
    """
    train, test = _train_test_masks(rows)
    ret = np.array([float(r["return_per_notional"]) for r in rows])
    ts = np.array([int(r["signal_ts_ms"]) for r in rows])
    out: dict[str, Any] = {"n_train": int(train.sum()), "n_test": int(test.sum())}
    arms: dict[str, np.ndarray] = {}
    betas: dict[str, list[float]] = {}

    design_full = _design_matrix(rows, include_symbol_hash=True)
    design_nohash = _design_matrix(rows, include_symbol_hash=False)

    # individual neutralized path-shape features (symbol-hash removed)
    feat_resid: dict[str, np.ndarray] = {}
    for feat in NEUTRALIZED_FEATURES:
        f = np.array([float(r[feat]) for r in rows])
        resid, beta = _ols_residual(f, design_full, train)
        feat_resid[feat] = resid
        arms[f"{feat}__neutralized"] = resid
        betas[f"{feat}__neutralized"] = [float(b) for b in beta]

    # P1 pair = average of train-z-scored residuals of the two pre-returns
    def _zavg(keys: tuple[str, ...]) -> np.ndarray:
        zs = []
        for k in keys:
            r = feat_resid[k]
            mu, sd = r[train].mean(), r[train].std()
            zs.append((r - mu) / (sd if sd > 0 else 1.0))
        return np.mean(zs, axis=0)

    arms["P1_pre_return_pair"] = _zavg(("pre_6h_return", "pre_24h_return"))
    arms["P2_runup_vol"] = feat_resid["pre_24h_realized_vol"]
    arms["P3_combined"] = _zavg(NEUTRALIZED_FEATURES)

    # P4 negative control: symbol_hash through identical pipeline minus itself
    sh = np.array([float(r["symbol_hash_bucket"]) for r in rows])
    resid_sh, beta_sh = _ols_residual(sh, design_nohash, train)
    arms["P4_symbol_hash"] = resid_sh
    betas["P4_symbol_hash"] = [float(b) for b in beta_sh]

    # auxiliary controls (reported, not the gate)
    mh = np.array([float(r["month_hash_bucket"]) for r in rows])
    arms["aux_month_hash"], _ = _ols_residual(mh, design_nohash, train)
    if all(r.get("shuffled_in_fold_composite") is not None for r in rows):
        sc = np.array([float(r["shuffled_in_fold_composite"]) for r in rows])
        arms["aux_shuffled_composite"], _ = _ols_residual(sc, design_full, train)
    # P0 reference: raw composite (no neutralization)
    arms["P0_composite_raw"] = np.array([float(r["composite"]) for r in rows])

    out["arms_test"] = {name: score[test] for name, score in arms.items()}
    out["test_return"] = ret[test]
    out["test_ts"] = ts[test]
    out["betas"] = betas
    # marginal-over-composite partial: residualize feature & return on composite only
    comp = np.array([float(r["composite"]) for r in rows])
    comp_design = np.column_stack([np.ones(len(rows)), comp])
    ret_resid_comp, _ = _ols_residual(ret, comp_design, train)
    out["marginal_over_composite"] = {}
    for feat in NEUTRALIZED_FEATURES:
        f = np.array([float(r[feat]) for r in rows])
        f_resid_comp, _ = _ols_residual(f, comp_design, train)
        out["marginal_over_composite"][feat] = {
            "feat_resid_comp_test": f_resid_comp[test],
            "ret_resid_comp_test": ret_resid_comp[test],
        }
    return out


# ----------------------------- raw context ----------------------------------
def _raw_effect(rows: list[dict[str, Any]], feature: str) -> dict[str, Any]:
    clean = [r for r in rows if r.get(feature) is not None]
    x = np.array([float(r[feature]) for r in clean])
    y = np.array([float(r["return_per_notional"]) for r in clean])
    spread = _tercile_spread_bps(x, y) if x.size else {"covered": 0, "spread_bps": None}
    return {"feature": feature, "kind": "raw", "covered": int(x.size),
            "spearman_ic": _spearman(x, y), "spread_bps": spread.get("spread_bps")}


# ----------------------------- run ------------------------------------------
def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    code_hash = _code_hash()

    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG,
        "stage": "stage7_path_shape_neutralized",
        "window": {"start": args.start, "end_exclusive": args.end},
        "out": str(out),
        "stage0_dir": str(stage0_dir),
        "sweep_tag": SWEEP_TAG,
        "git_head": _git_head(),
        "code_hash": code_hash,
        "frozen_forward_config_hash": frozen_config_hash(),
        "roots": {v: str(ROOTS[v]) for v in venues},
        "run_label": "exploratory",
        "train_fraction": TRAIN_FRACTION,
        "pit_gate": {},
        "venues": {},
        "effect_rows": [],
        "third_rows": [],
        "neutralization_coeffs": {},
    }

    per_venue_scores: dict[str, dict[str, Any]] = {}
    raw_event_rows: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        rows = _clean(_load_population(stage0_dir, root, venue), NEUTRALIZED_FEATURES)
        scores = _residual_scores(rows)
        per_venue_scores[venue] = scores
        payload["neutralization_coeffs"][venue] = scores["betas"]
        payload["venues"][venue] = {
            "n_selected_clean": len(rows),
            "n_train": scores["n_train"],
            "n_test": scores["n_test"],
            "raw": {f: _raw_effect(rows, f) for f in NEUTRALIZED_FEATURES},
            "arms": {},
        }
        # per-venue residual IC + spread on the test fold
        ret_t = scores["test_return"]
        for name, score_t in scores["arms_test"].items():
            ic = _spearman(score_t, ret_t)
            spr = _tercile_spread_bps(score_t, ret_t)
            payload["venues"][venue]["arms"][name] = {
                "spearman_ic": ic, "spread_bps": spr["spread_bps"], "covered": spr["covered"]
            }
            payload["effect_rows"].append({
                "venue": venue, "arm": name, "covered": spr["covered"],
                "spearman_ic": ic, "spread_bps": spr["spread_bps"],
                "winsorized_spread_bps": _winsorized_spread_bps(score_t, ret_t),
            })
        # dump test-fold rows for audit
        for i in range(len(scores["test_return"])):
            raw_event_rows.append({
                "venue": venue, "test_ts_ms": int(scores["test_ts"][i]),
                "return_per_notional": float(scores["test_return"][i]),
                **{f"{name}": float(scores["arms_test"][name][i]) for name in scores["arms_test"]},
            })

    # -------- pooled test-fold decision stats --------
    decision_arms = ("P1_pre_return_pair", "P2_runup_vol", "P3_combined")
    pooled: dict[str, Any] = {}
    valid_venues = [v for v in venues if v in per_venue_scores]
    if valid_venues:
        all_ret = np.concatenate([per_venue_scores[v]["test_return"] for v in valid_venues])
        all_ts = np.concatenate([per_venue_scores[v]["test_ts"] for v in valid_venues])
        order = np.argsort(all_ts, kind="mergesort")
        for name in (*decision_arms, "P4_symbol_hash", "P0_composite_raw", "aux_month_hash"):
            if not all(name in per_venue_scores[v]["arms_test"] for v in valid_venues):
                continue
            score = np.concatenate([per_venue_scores[v]["arms_test"][name] for v in valid_venues])
            spr = _tercile_spread_bps(score, all_ret)
            ic = _spearman(score, all_ret)
            # chronological thirds (pooled, by time)
            thirds = []
            s_ord, r_ord = score[order], all_ret[order]
            n = s_ord.size
            for idx in range(3):
                lo, hi = idx * n // 3, (idx + 1) * n // 3
                t_spr = _tercile_spread_bps(s_ord[lo:hi], r_ord[lo:hi])
                thirds.append(t_spr["spread_bps"])
                payload["third_rows"].append({"arm": name, "third": idx + 1, "spread_bps": t_spr["spread_bps"]})
            pooled[name] = {"spearman_ic": ic, "spread_bps": spr["spread_bps"], "covered": spr["covered"], "thirds": thirds}

    # marginal-over-composite (pooled test)
    marg: dict[str, float | None] = {}
    if valid_venues:
        for feat in NEUTRALIZED_FEATURES:
            fr = np.concatenate([per_venue_scores[v]["marginal_over_composite"][feat]["feat_resid_comp_test"] for v in valid_venues])
            rr = np.concatenate([per_venue_scores[v]["marginal_over_composite"][feat]["ret_resid_comp_test"] for v in valid_venues])
            marg[feat] = _spearman(fr, rr)
    payload["marginal_over_composite_pooled"] = marg
    payload["pooled"] = pooled

    # -------- admissibility decision --------
    neg_abs = abs(float((pooled.get("P4_symbol_hash") or {}).get("spread_bps") or 0.0))
    arm_features = {
        "P1_pre_return_pair": ("pre_6h_return", "pre_24h_return"),
        "P2_runup_vol": ("pre_24h_realized_vol",),
        "P3_combined": NEUTRALIZED_FEATURES,
    }
    decisions: dict[str, Any] = {"negative_control_abs_spread_bps": neg_abs, "arms": {}}
    for arm in decision_arms:
        fails: list[str] = []
        venue_ic = {v: payload["venues"][v]["arms"].get(arm, {}).get("spearman_ic") for v in valid_venues}
        venue_cov = {v: payload["venues"][v]["arms"].get(arm, {}).get("covered", 0) for v in valid_venues}
        venue_spread = {v: payload["venues"][v]["arms"].get(arm, {}).get("spread_bps") for v in valid_venues}
        if any((venue_cov.get(v) or 0) < COVERAGE_FLOOR for v in valid_venues) or len(valid_venues) < 2:
            fails.append("coverage <500 on a venue")
        ics = [venue_ic.get(v) for v in valid_venues]
        if any(i is None or i == 0.0 for i in ics) or len({i > 0 for i in ics if i is not None}) > 1:
            fails.append("spearman IC sign mismatch across venues")
        spreads = [venue_spread.get(v) for v in valid_venues]
        if any(s is None or s == 0.0 for s in spreads) or len({s > 0 for s in spreads if s is not None}) > 1:
            fails.append("venue spread sign mismatch")
        pooled_spread = float((pooled.get(arm) or {}).get("spread_bps") or 0.0)
        if abs(pooled_spread) < SPREAD_BPS_FLOOR:
            fails.append("pooled spread <25bps")
        dir_pos = pooled_spread > 0
        thirds = [t for t in (pooled.get(arm) or {}).get("thirds", []) if t is not None]
        if sum(1 for t in thirds if (t > 0) == dir_pos) < 2:
            fails.append("fewer than two thirds same direction")
        if abs(pooled_spread) <= neg_abs:
            fails.append("not stronger than neutralized negative control")
        margs = [marg.get(f) for f in arm_features[arm]]
        if not any((m or 0.0) > 0 for m in margs):
            fails.append("no positive marginal IC over composite")
        decisions["arms"][arm] = {"admissible": not fails, "reasons": fails or ["admissible"],
                                  "pooled_spread_bps": pooled_spread,
                                  "marginal_over_composite": {f: marg.get(f) for f in arm_features[arm]}}
    payload["decision"] = decisions
    payload["verdict_pass"] = any(d["admissible"] for d in decisions["arms"].values())

    # -------- write artifacts --------
    _write_csv(out / "stage7_feature_effects.csv", payload["effect_rows"])
    _write_csv(out / "stage7_thirds.csv", payload["third_rows"])
    for venue in valid_venues:
        vr = [r for r in raw_event_rows if r["venue"] == venue]
        _write_csv(out / f"stage7_events_{venue}.csv", vr)
    (out / "stage7_neutralization_coeffs.json").write_text(
        json.dumps(payload["neutralization_coeffs"], indent=2, default=str), encoding="utf-8")
    (out / "stage7_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage7_summary.md")
    return payload


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]
    lines = [
        "# W5 Continuous Stage 7 — Neutralized Path-Shape Scoring",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`",
        f"- Frozen forward config hash: `{payload['frozen_forward_config_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Train fraction (earliest, by signal_ts): `{payload['train_fraction']}`",
        f"- Run label: `{payload['run_label']}`",
        f"- Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ADMISSIBLE (>=1 arm)' if payload['verdict_pass'] else 'NULL (no arm admissible)'}",
        "",
        f"Neutralized negative-control (P4 symbol-hash) pooled abs spread: "
        f"`{d['negative_control_abs_spread_bps']:.2f}` bps.",
        "",
        "## Per-arm decision (residual, pooled test fold)",
        "",
        "| Arm | Pooled spread bps | Marginal IC > composite | Admissible | Reasons |",
        "|---|---:|---|---|---|",
    ]
    for arm, info in d["arms"].items():
        marg = "; ".join(f"{k}={'' if v is None else f'{v:.4f}'}" for k, v in info["marginal_over_composite"].items())
        lines.append(
            f"| `{arm}` | {info['pooled_spread_bps']:.2f} | {marg} | "
            f"{info['admissible']} | {'; '.join(info['reasons'])} |"
        )
    lines.extend(["", "## Per-venue residual IC / spread (test fold)", "",
                  "| Venue | Arm | Spearman IC | Spread bps | Covered |", "|---|---|---:|---:|---:|"])
    for venue, block in payload["venues"].items():
        if "arms" not in block:
            continue
        for arm in ("P1_pre_return_pair", "P2_runup_vol", "P3_combined", "P4_symbol_hash", "P0_composite_raw"):
            a = block["arms"].get(arm)
            if not a:
                continue
            ic = "" if a["spearman_ic"] is None else f"{a['spearman_ic']:.4f}"
            sp = "" if a["spread_bps"] is None else f"{a['spread_bps']:.2f}"
            lines.append(f"| `{venue}` | `{arm}` | {ic} | {sp} | {a['covered']} |")
    lines.extend(["", "## Raw (un-neutralized) context — per venue", "",
                  "| Venue | Feature | Raw Spearman | Raw spread bps |", "|---|---|---:|---:|"])
    for venue, block in payload["venues"].items():
        if "raw" not in block:
            continue
        for feat, r in block["raw"].items():
            ic = "" if r["spearman_ic"] is None else f"{r['spearman_ic']:.4f}"
            sp = "" if r["spread_bps"] is None else f"{r['spread_bps']:.2f}"
            lines.append(f"| `{venue}` | `{feat}` | {ic} | {sp} |")
    lines.extend(["", "Stage 7 is `exploratory` — an admissibility gate. An admissible residual still",
                  "must beat its control on pooled MAR (both venues) in a downstream engine stage",
                  "before anything is a demo/paper candidate.", ""])
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
    print(json.dumps({
        "out": payload["out"], "verdict_pass": payload["verdict_pass"],
        "decision": payload["decision"]["arms"],
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
