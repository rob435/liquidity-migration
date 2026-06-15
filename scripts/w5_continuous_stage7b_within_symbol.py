#!/usr/bin/env python3
"""W5 Stage 7b - Within-symbol path-shape (FE partial rank-IC over composite, permutation null).

Stage 7's registered tercile-spread gate was noise-dominated for this heavy-tailed
per-notional return cross-section, so it could not decide whether the clean
rank-IC signal (path-shape IC ~0.21, ~2x the symbol-hash control) is real
within-symbol timing or merely symbol selection. Stage 7b answers that with the
correct statistic: a within-symbol **partial** rank-IC of path-shape vs return,
controlling for the production composite, with a within-symbol permutation null.

Within-symbol demeaning removes ALL symbol mix by construction (a per-symbol
constant like symbol_hash has zero within-symbol variation), and partialling the
within-symbol composite removes the production score. What survives is only the
harvestable per-symbol, beyond-composite timing component. The symbol_hash arm is
a built-in validity check: it must be degenerate (zero variation -> null IC).

Methodology note: an earlier form pre-removed the composite by a GLOBAL OLS before
within-symbol demeaning. That contaminated the degenerate control (a spurious
train-fold beta re-injected the within-symbol composite into symbol_hash, giving
it a false IC). Controlling for composite *within-symbol* at the IC stage (partial
Spearman) is the correct, stricter fix and makes symbol_hash properly null.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage7b-within-symbol-pathshape.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage7b_within_symbol.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
        --permutations 1000 --seed 0 \
        --out ~/SHARED_DATA/w5_continuous_stage7b_within_symbol_2026-06-14
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "w5_stage7", str(REPO / "scripts" / "w5_continuous_stage7_path_shape_neutralized.py")
)
s7 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s7)  # type: ignore[union-attr]

from liquidity_migration.continuous_forward_replay import frozen_config_hash  # noqa: E402
from scripts.w4_continuous_stop_exit_realism import _pit_partition_gate  # noqa: E402

SWEEP_TAG = "w5_continuous_stage7b_within_symbol_2026-06-14"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage7b-within-symbol-pathshape.md"
NEUTRALIZED_FEATURES = s7.NEUTRALIZED_FEATURES
ROOTS = s7.ROOTS
P_THRESH = 0.025
COVERAGE_FLOOR = 500
DECLARED_SIGN = 1.0  # all three features expected-positive (W4 Stage 3 + Stage 7, cross-venue)


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage7b_within_symbol.py",
        REPO / "scripts" / "w5_continuous_stage7_path_shape_neutralized.py",
        REPO / "scripts" / "w5_continuous_stage0_candidate_tape.py",
        REPO / "liquidity_migration" / "continuous_events.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{s7._sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------- within-symbol partial rank-IC ----------------------
def _corr(x: np.ndarray, y: np.ndarray) -> float:
    sx, sy = x.std(), y.std()
    return 0.0 if sx == 0 or sy == 0 else float(np.corrcoef(x, y)[0, 1])


def _pcorr_from_ranks(rF: np.ndarray, rR: np.ndarray, rC: np.ndarray) -> float | None:
    """Partial Pearson of ranked F,R controlling for ranked C (= partial Spearman)."""
    if rF.std() == 0:
        return None
    rfr, rfc, rcr = _corr(rF, rR), _corr(rF, rC), _corr(rC, rR)
    den = np.sqrt(max(1e-12, (1.0 - rfc**2) * (1.0 - rcr**2)))
    return (rfr - rfc * rcr) / den


def _within_symbol_demean(values: np.ndarray, blocks: list[np.ndarray]) -> np.ndarray:
    out = values.astype(float).copy()
    for idxs in blocks:
        out[idxs] -= out[idxs].mean()
    return out


def _arm_scores(rows: list[dict[str, Any]], train: np.ndarray) -> dict[str, np.ndarray]:
    """Raw feature scores over ALL rows (composite is partialled within-symbol later, not here)."""
    feats = {f: np.array([float(r[f]) for r in rows]) for f in NEUTRALIZED_FEATURES}

    def z(a: np.ndarray) -> np.ndarray:
        mu, sd = a[train].mean(), a[train].std()
        return (a - mu) / (sd if sd > 0 else 1.0)

    return {
        "Q1_pre_6h_return": feats["pre_6h_return"],
        "Q2_pre_24h_return": feats["pre_24h_return"],
        "Q3_pre_24h_realized_vol": feats["pre_24h_realized_vol"],
        "Q_pair": (z(feats["pre_6h_return"]) + z(feats["pre_24h_return"])) / 2.0,
        "Q_combined": sum(z(feats[f]) for f in NEUTRALIZED_FEATURES) / 3.0,
        "Q_symbol_hash": np.array([float(r["symbol_hash_bucket"]) for r in rows]),
    }


def _arm_within_symbol(score: np.ndarray, ret: np.ndarray, comp: np.ndarray, kept: np.ndarray,
                       sym: list[str], ts: np.ndarray, nperm: int, seed: int) -> dict[str, Any]:
    blocks_map: dict[str, list[int]] = defaultdict(list)
    for pos, i in enumerate(kept):
        blocks_map[sym[i]].append(pos)
    blocks = [np.array(v) for v in blocks_map.values() if len(v) >= 2]
    score_k = score[kept]
    # degeneracy by within-block range (robust to float demean residue): a per-symbol
    # constant (e.g. symbol_hash) has zero within-symbol variation -> no information.
    max_within_block_range = max((float(score_k[idxs].max() - score_k[idxs].min()) for idxs in blocks), default=0.0)
    r_wd = _within_symbol_demean(ret[kept], blocks)
    c_wd = _within_symbol_demean(comp[kept], blocks)
    f_wd = _within_symbol_demean(score_k, blocks)
    rR, rC = s7._avg_rank(r_wd), s7._avg_rank(c_wd)
    rF = s7._avg_rank(f_wd)
    ic_obs = None if max_within_block_range < 1e-12 else _pcorr_from_ranks(rF, rR, rC)
    if ic_obs is None:  # degenerate (e.g. symbol_hash: zero within-symbol variation)
        return {"ic_obs": None, "p_value": None, "covered": int(kept.size), "degenerate": True,
                "null_mean": None, "null_sd": None, "null_p975": None, "thirds": []}
    rng = np.random.RandomState(seed)
    null = np.empty(nperm)
    for k in range(nperm):
        f = f_wd.copy()
        for idxs in blocks:
            f[idxs] = f[idxs][rng.permutation(idxs.size)]
        null[k] = _pcorr_from_ranks(s7._avg_rank(f), rR, rC) or 0.0
    p_val = float((1 + int((null >= ic_obs).sum())) / (1 + nperm))
    order = np.argsort(ts[kept], kind="mergesort")
    f_o, r_o, c_o = f_wd[order], r_wd[order], c_wd[order]
    n = f_o.size
    thirds = []
    for idx in range(3):
        lo, hi = idx * n // 3, (idx + 1) * n // 3
        thirds.append(_pcorr_from_ranks(s7._avg_rank(f_o[lo:hi]), s7._avg_rank(r_o[lo:hi]), s7._avg_rank(c_o[lo:hi])))
    return {"ic_obs": ic_obs, "p_value": p_val, "covered": int(kept.size), "degenerate": False,
            "null_mean": float(null.mean()), "null_sd": float(null.std()),
            "null_p975": float(np.percentile(null, 97.5)), "thirds": thirds}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG,
        "stage": "stage7b_within_symbol_partial",
        "window": {"start": args.start, "end_exclusive": args.end},
        "out": str(out), "stage0_dir": str(stage0_dir), "sweep_tag": SWEEP_TAG,
        "git_head": _git_head(), "code_hash": _code_hash(),
        "frozen_forward_config_hash": frozen_config_hash(),
        "roots": {v: str(ROOTS[v]) for v in venues},
        "run_label": "exploratory", "permutations": args.permutations, "seed": args.seed,
        "statistic": "within_symbol_partial_spearman_over_composite",
        "pit_gate": {}, "venues": {},
    }
    ic_rows: list[dict[str, Any]] = []
    third_rows: list[dict[str, Any]] = []
    arm_names = ["Q1_pre_6h_return", "Q2_pre_24h_return", "Q3_pre_24h_realized_vol",
                 "Q_pair", "Q_combined", "Q_symbol_hash"]
    per_venue: dict[str, dict[str, Any]] = {}
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        rows = s7._clean(s7._load_population(stage0_dir, root, venue), NEUTRALIZED_FEATURES)
        train, test = s7._train_test_masks(rows)
        ret = np.array([float(r["return_per_notional"]) for r in rows])
        comp = np.array([float(r["composite"]) for r in rows])
        ts = np.array([int(r["signal_ts_ms"]) for r in rows])
        sym = [str(r["symbol"]) for r in rows]
        test_idx = np.where(test)[0]
        counts = Counter(sym[i] for i in test_idx)
        kept = np.array([i for i in test_idx if counts[sym[i]] >= 2])
        n_symbols_ge2 = sum(1 for _s, c in counts.items() if c >= 2)
        arms = _arm_scores(rows, train)
        venue_block: dict[str, Any] = {
            "n_selected_clean": len(rows), "n_train": int(train.sum()), "n_test": int(test.sum()),
            "n_kept": int(kept.size), "n_symbols_ge2": int(n_symbols_ge2),
            "composite_within_symbol_ic": s7._spearman(
                _within_symbol_demean(comp[kept], [np.array(v) for v in
                                      _blocks(kept, sym).values() if len(v) >= 2]),
                _within_symbol_demean(ret[kept], [np.array(v) for v in
                                      _blocks(kept, sym).values() if len(v) >= 2])),
            "arms": {},
        }
        for arm in arm_names:
            res = _arm_within_symbol(arms[arm], ret, comp, kept, sym, ts, args.permutations, args.seed)
            venue_block["arms"][arm] = res
            ic_rows.append({"venue": venue, "arm": arm, "covered": res["covered"], "ic_obs": res["ic_obs"],
                            "p_value": res["p_value"], "null_mean": res["null_mean"], "null_sd": res["null_sd"],
                            "null_p975": res["null_p975"], "n_symbols_ge2": n_symbols_ge2,
                            "degenerate": res["degenerate"]})
            for k, t in enumerate(res.get("thirds", [])):
                third_rows.append({"venue": venue, "arm": arm, "third": k + 1, "partial_ic": t})
        per_venue[venue] = venue_block
        payload["venues"][venue] = venue_block

    decision_arms = ["Q1_pre_6h_return", "Q2_pre_24h_return", "Q3_pre_24h_realized_vol", "Q_pair", "Q_combined"]
    valid = [v for v in venues if v in per_venue]
    decisions: dict[str, Any] = {}
    for arm in decision_arms:
        fails: list[str] = []
        if len(valid) < 2:
            fails.append("missing a venue")
        stats = {v: per_venue[v]["arms"].get(arm, {}) for v in valid}
        if any((stats[v].get("covered") or 0) < COVERAGE_FLOOR for v in valid):
            fails.append("coverage <500 on a venue")
        ics = [stats[v].get("ic_obs") for v in valid]
        if any(i is None for i in ics):
            fails.append("undefined IC on a venue")
        else:
            if any((i > 0) != (DECLARED_SIGN > 0) for i in ics):
                fails.append("IC not in declared (positive) direction on a venue")
            if len({i > 0 for i in ics}) > 1:
                fails.append("IC sign mismatch across venues")
        ps = [stats[v].get("p_value") for v in valid]
        if any(p is None or p >= P_THRESH for p in ps):
            fails.append(f"permutation p>={P_THRESH} on a venue")
        for v in valid:
            thirds = [t for t in stats[v].get("thirds", []) if t is not None]
            ic_v = stats[v].get("ic_obs")
            if ic_v is not None and sum(1 for t in thirds if (t > 0) == (ic_v > 0)) < 2:
                fails.append(f"<2/3 thirds same sign on {v}")
        decisions[arm] = {"admissible": not fails, "reasons": fails or ["admissible"],
                          "ic": {v: stats[v].get("ic_obs") for v in valid},
                          "p_value": {v: stats[v].get("p_value") for v in valid}}
    # validity check: symbol_hash control must be degenerate on both venues
    payload["control_degenerate"] = {
        v: per_venue[v]["arms"]["Q_symbol_hash"]["degenerate"] for v in valid
    }
    payload["decision"] = decisions
    payload["verdict_pass"] = any(d["admissible"] for d in decisions.values()) and all(
        payload["control_degenerate"].get(v) for v in valid
    )

    s7._write_csv(out / "stage7b_within_symbol_ic.csv", ic_rows)
    s7._write_csv(out / "stage7b_thirds.csv", third_rows)
    (out / "stage7b_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage7b_summary.md")
    return payload


def _blocks(kept: np.ndarray, sym: list[str]) -> dict[str, list[int]]:
    bm: dict[str, list[int]] = defaultdict(list)
    for pos, i in enumerate(kept):
        bm[sym[i]].append(pos)
    return bm


def _fmt(x: float | None) -> str:
    return "" if x is None else f"{x:.4f}"


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W5 Continuous Stage 7b — Within-Symbol Path-Shape (partial rank-IC over composite)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`",
        f"- Frozen forward config hash: `{payload['frozen_forward_config_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Statistic: `{payload['statistic']}`",
        f"- Permutations: `{payload['permutations']}`  seed `{payload['seed']}`  run label `{payload['run_label']}`",
        f"- Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'ADMISSIBLE (>=1 arm, control degenerate)' if payload['verdict_pass'] else 'NULL / INVALID'}",
        "",
        f"Degenerate-control check (`Q_symbol_hash` must be degenerate): `{payload.get('control_degenerate')}`.",
        "",
        "## Per-arm decision (within-symbol partial rank-IC over composite, test fold)",
        "",
        "| Arm | bybit IC | bybit p | binance IC | binance p | Admissible | Reasons |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for arm, info in payload["decision"].items():
        ic = info["ic"]
        p = info["p_value"]
        lines.append(
            f"| `{arm}` | {_fmt(ic.get('bybit'))} | {_fmt(p.get('bybit'))} | "
            f"{_fmt(ic.get('binance'))} | {_fmt(p.get('binance'))} | {info['admissible']} | "
            f"{'; '.join(info['reasons'])} |"
        )
    lines.extend(["", "## Per-venue detail (partial IC, perm p, null SD/97.5pct, coverage)", "",
                  "| Venue | Arm | partial IC | p | null SD | null 97.5% | Covered | Sym>=2 |",
                  "|---|---|---:|---:|---:|---:|---:|---:|"])
    for venue, block in payload["venues"].items():
        if "arms" not in block:
            continue
        for arm, r in block["arms"].items():
            lines.append(
                f"| `{venue}` | `{arm}` | {_fmt(r['ic_obs'])} | {_fmt(r['p_value'])} | "
                f"{_fmt(r['null_sd'])} | {_fmt(r['null_p975'])} | {r['covered']} | {block['n_symbols_ge2']} |"
            )
    lines.extend(["", "Composite within-symbol IC (context): "
                  + ", ".join(f"{v}={_fmt(b.get('composite_within_symbol_ic'))}"
                              for v, b in payload["venues"].items() if "arms" in b),
                  "",
                  "Stage 7b is `exploratory` — an admissibility gate. An admissible within-symbol",
                  "feature must still beat the frozen control on pooled MAR (both venues) in a",
                  "downstream engine stage (Stage 1 priority / Stage 5 sizing) before it is a",
                  "demo/paper candidate.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--stage0", default=str(s7.SHARED / s7.STAGE0_TAG))
    parser.add_argument("--permutations", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=str(s7.SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict_pass": payload["verdict_pass"],
                      "control_degenerate": payload.get("control_degenerate"),
                      "decision": payload["decision"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
