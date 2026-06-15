#!/usr/bin/env python3
"""W5 Stage 4 (screen) - causal pre-entry feature selection signal for an entry sniper.

EXPLORATORY admissibility screen, NOT a decision. Before spending a ~60-min engine
sweep on a "sniper" (drop the worst-EV selected entries to cut drawdown / lift MAR),
ask the cheap question first: does any *untested* causal pre-entry feature carry a
within-symbol selection signal beyond the production composite?

A robust sniper must key off a WITHIN-symbol signal (the same symbol's better vs worse
entries), not cross-symbol selection (which Stage 5 showed is just symbol-identity luck,
beaten by a random per-symbol tilt). So the screen statistic is exactly Stage 7b's:
within-symbol partial rank-IC of the feature vs realized per-notional return, controlling
for composite, with a within-symbol permutation null + a degenerate symbol-hash control.

Stage 7b already screened the path-shape trio (pre_6h_return / pre_24h_return /
pre_24h_realized_vol: admissible, IC ~0.10) and Stage 5 showed they are NOT harvestable
via sizing. This screen adds the UNTESTED features:
  - signal_bar_close_location : entry microstructure (where the trigger bar closed in its
    range) — distinct from the vol-profit confound that sank Stage 9 sizing-down.
  - pre_6h_upper_extension    : dislocation magnitude (how stretched the move is).
  - turnover_quote (log)      : symbol liquidity at entry.
  - crowding_count / active_count : concurrent-book state at entry.
The path-shape trio is re-run here as a reference (must reproduce ~0.10).

Output is a per-feature within-symbol partial IC table (both venues, perm p, thirds).
A feature is a sniper candidate iff it carries a both-venue, same-sign, p<0.025 within
-symbol IC with >=2/3 thirds agreeing AND a degenerate symbol-hash control — only then is
an engine down-sniper worth the cost. No MAR claim here; admissibility != harvestable.

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage4_sniper_screen.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
        --permutations 1000 --seed 0 \
        --out ~/SHARED_DATA/w5_continuous_stage4_sniper_screen_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
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


s7 = _load_module("w5_stage7", "w5_continuous_stage7_path_shape_neutralized.py")
s7b = _load_module("w5_stage7b", "w5_continuous_stage7b_within_symbol.py")

from liquidity_migration.continuous_forward_replay import frozen_config_hash  # noqa: E402
from scripts.w4_continuous_stop_exit_realism import _pit_partition_gate  # noqa: E402

SWEEP_TAG = "w5_continuous_stage4_sniper_screen_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage4-sniper.md"
ROOTS = s7.ROOTS
COMPONENTS = s7.COMPONENTS
MS_PER_HOUR = s7.MS_PER_HOUR
P_THRESH = 0.025
COVERAGE_FLOOR = 500

# Feature -> (tape column, transform). Reference trio first, then the untested features.
REFERENCE = ("pre_6h_return", "pre_24h_return", "pre_24h_realized_vol")
UNTESTED = ("signal_bar_close_location", "pre_6h_upper_extension",
            "log_turnover_quote", "crowding_count", "active_count")
ALL_FEATURES = REFERENCE + UNTESTED


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage4_sniper_screen.py",
        REPO / "scripts" / "w5_continuous_stage7b_within_symbol.py",
        REPO / "scripts" / "w5_continuous_stage7_path_shape_neutralized.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{s7._sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_population_full(stage0_dir: Path, root: Path, venue: str) -> list[dict[str, Any]]:
    """Selected entries with ALL candidate features joined to the realized ledger return."""
    tape_path = stage0_dir / f"candidate_tape_{venue}.parquet"
    if not tape_path.exists():
        raise FileNotFoundError(f"missing Stage 0 candidate tape: {tape_path}")
    tape = pl.read_parquet(tape_path).filter(pl.col("selected"))
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
            if key not in ret_map:
                continue
            net_return, notional = ret_map[key]
            tq = r.get("turnover_quote")
            rows.append(
                {
                    "venue": venue,
                    "component": component,
                    "symbol": str(r["symbol"]),
                    "signal_ts_ms": int(r["signal_ts_ms"]),
                    "composite": r.get("composite"),
                    "symbol_hash_bucket": r.get("symbol_hash_bucket"),
                    "pre_6h_return": r.get("pre_6h_return"),
                    "pre_24h_return": r.get("pre_24h_return"),
                    "pre_24h_realized_vol": r.get("pre_24h_realized_vol"),
                    "signal_bar_close_location": r.get("signal_bar_close_location"),
                    "pre_6h_upper_extension": r.get("pre_6h_upper_extension"),
                    "log_turnover_quote": (math.log(float(tq)) if tq is not None and float(tq) > 0 else None),
                    "crowding_count": r.get("crowding_count"),
                    "active_count": r.get("active_count"),
                    "return_per_notional": net_return / max(notional, 1e-12),
                }
            )
    rows.sort(key=lambda d: (d["signal_ts_ms"], d["symbol"]))
    return rows


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG,
        "stage": "stage4_sniper_screen",
        "window": {"start": args.start, "end_exclusive": args.end},
        "out": str(out), "stage0_dir": str(stage0_dir), "sweep_tag": SWEEP_TAG,
        "git_head": _git_head(), "code_hash": _code_hash(),
        "frozen_forward_config_hash": frozen_config_hash(),
        "roots": {v: str(ROOTS[v]) for v in venues},
        "run_label": "exploratory", "permutations": args.permutations, "seed": args.seed,
        "statistic": "within_symbol_partial_spearman_over_composite",
        "features": list(ALL_FEATURES), "pit_gate": {}, "venues": {},
    }
    ic_rows: list[dict[str, Any]] = []
    per_venue: dict[str, dict[str, Any]] = {}
    arm_names = list(ALL_FEATURES) + ["Q_symbol_hash"]
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        rows = _load_population_full(stage0_dir, root, venue)
        rows = [r for r in rows if all(
            r.get(k) is not None for k in ("composite", "symbol_hash_bucket",
                                           "return_per_notional", *ALL_FEATURES))]
        ret = np.array([float(r["return_per_notional"]) for r in rows])
        comp = np.array([float(r["composite"]) for r in rows])
        ts = np.array([int(r["signal_ts_ms"]) for r in rows])
        sym = [str(r["symbol"]) for r in rows]
        # full-population within-symbol screen: keep symbols with >=2 selected entries
        counts = Counter(sym)
        kept = np.array([i for i in range(len(rows)) if counts[sym[i]] >= 2])
        n_symbols_ge2 = sum(1 for _s, c in counts.items() if c >= 2)
        feat_arrays = {f: np.array([float(r[f]) for r in rows]) for f in ALL_FEATURES}
        feat_arrays["Q_symbol_hash"] = np.array([float(r["symbol_hash_bucket"]) for r in rows])
        venue_block: dict[str, Any] = {
            "n_selected_clean": len(rows), "n_kept": int(kept.size),
            "n_symbols_ge2": int(n_symbols_ge2), "arms": {},
        }
        for arm in arm_names:
            res = s7b._arm_within_symbol(feat_arrays[arm], ret, comp, kept, sym, ts,
                                         args.permutations, args.seed)
            venue_block["arms"][arm] = res
            ic_rows.append({"venue": venue, "arm": arm, "covered": res["covered"],
                            "ic_obs": res["ic_obs"], "p_value": res["p_value"],
                            "null_sd": res["null_sd"], "degenerate": res["degenerate"],
                            "thirds": ";".join(s7b._fmt(t) for t in res.get("thirds", []))})
        per_venue[venue] = venue_block
        payload["venues"][venue] = venue_block

    valid = [v for v in venues if v in per_venue]
    decisions: dict[str, Any] = {}
    for arm in ALL_FEATURES:
        fails: list[str] = []
        if len(valid) < 2:
            fails.append("missing a venue")
        stats = {v: per_venue[v]["arms"].get(arm, {}) for v in valid}
        if any((stats[v].get("covered") or 0) < COVERAGE_FLOOR for v in valid):
            fails.append("coverage<500 on a venue")
        ics = [stats[v].get("ic_obs") for v in valid]
        if any(i is None for i in ics):
            fails.append("undefined IC on a venue")
        elif len({i > 0 for i in ics}) > 1:
            fails.append("IC sign mismatch across venues")
        ps = [stats[v].get("p_value") for v in valid]
        if any(p is None or p >= P_THRESH for p in ps):
            fails.append(f"perm p>={P_THRESH} on a venue")
        for v in valid:
            thirds = [t for t in stats[v].get("thirds", []) if t is not None]
            ic_v = stats[v].get("ic_obs")
            if ic_v is not None and sum(1 for t in thirds if (t > 0) == (ic_v > 0)) < 2:
                fails.append(f"<2/3 thirds same sign on {v}")
        decisions[arm] = {"candidate": not fails, "reasons": fails or ["candidate"],
                          "ic": {v: stats[v].get("ic_obs") for v in valid},
                          "p_value": {v: stats[v].get("p_value") for v in valid}}
    payload["control_degenerate"] = {
        v: per_venue[v]["arms"]["Q_symbol_hash"]["degenerate"] for v in valid}
    payload["decision"] = decisions
    payload["any_untested_candidate"] = any(
        decisions[f]["candidate"] for f in UNTESTED) and all(
        payload["control_degenerate"].get(v) for v in valid)

    s7._write_csv(out / "stage4_sniper_screen_ic.csv", ic_rows)
    (out / "stage4_sniper_screen.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage4_sniper_screen.md")
    return payload


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W5 Continuous Stage 4 (screen) — causal pre-entry selection signal for a sniper",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Frozen cfg: `{payload['frozen_forward_config_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Statistic: `{payload['statistic']}`  perms `{payload['permutations']}`  "
        f"seed `{payload['seed']}`  label `{payload['run_label']}`",
        f"- Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Any UNTESTED feature a both-venue candidate: "
        f"**{payload['any_untested_candidate']}**",
        "",
        f"Symbol-hash control degenerate (must be true): `{payload.get('control_degenerate')}`.",
        "",
        "## Within-symbol partial rank-IC over composite (full selected population)",
        "",
        "| Feature | bybit IC | bybit p | binance IC | binance p | both-venue candidate | reasons |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for arm in ALL_FEATURES:
        info = payload["decision"][arm]
        ic, p = info["ic"], info["p_value"]
        tag = "UNTESTED" if arm in UNTESTED else "ref"
        lines.append(
            f"| `{arm}` ({tag}) | {s7b._fmt(ic.get('bybit'))} | {s7b._fmt(p.get('bybit'))} | "
            f"{s7b._fmt(ic.get('binance'))} | {s7b._fmt(p.get('binance'))} | "
            f"{info['candidate']} | {'; '.join(info['reasons'])} |")
    lines.extend(["", "## Per-venue detail (IC, perm p, null SD, thirds, coverage)", "",
                  "| Venue | Feature | IC | p | null SD | thirds | covered | sym>=2 |",
                  "|---|---|---:|---:|---:|---|---:|---:|"])
    for venue, block in payload["venues"].items():
        if "arms" not in block:
            continue
        for arm, r in block["arms"].items():
            lines.append(
                f"| `{venue}` | `{arm}` | {s7b._fmt(r['ic_obs'])} | {s7b._fmt(r['p_value'])} | "
                f"{s7b._fmt(r['null_sd'])} | {';'.join(s7b._fmt(t) for t in r.get('thirds', []))} | "
                f"{r['covered']} | {block['n_symbols_ge2']} |")
    lines.extend([
        "", "Screen only (`exploratory`). A both-venue candidate feature must still beat the",
        "frozen control on pooled MAR (both venues) AND a random-drop control in a downstream",
        "engine down-sniper before it is a demo/paper candidate. Admissibility != harvestable",
        "(Stage 7b admissible path-shape was NOT harvestable in Stage 5 sizing).", ""])
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
    print(json.dumps({"out": payload["out"],
                      "any_untested_candidate": payload["any_untested_candidate"],
                      "control_degenerate": payload.get("control_degenerate"),
                      "decision": {k: {"candidate": v["candidate"], "ic": v["ic"]}
                                   for k, v in payload["decision"].items()}},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
