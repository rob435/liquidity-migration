#!/usr/bin/env python3
"""W5 Stage 4 - Liquidity entry sniper (drop the least-liquid fades).

The Stage 4 screen found `turnover_quote` is the unique untested both-venue within-symbol
selection signal (partial rank-IC over composite +0.081 bybit / +0.134 binance, p=0.001;
positive ⇒ low-turnover fades realize worse returns). This stage tests whether DROPPING the
bottom-decile causal-trailing-turnover entries improves pooled MAR vs the frozen control on
both venues, BEYOND a count-matched random drop (liquidity selection vs mere de-leveraging).

Drop = `size_mult_lookup` (Stage 5 hook) set to 0.0 for dropped entries. Each trade is a
fixed independent slot and the hook is applied after the concurrency gate, so 0.0 removes
that slot's PnL+cost and lowers gross without touching any kept trade (kept trades identical
to V0; the BTC hedge resizes to the reduced exposure). Entry count unchanged.

Pre-registration: docs/preregistration/2026-06-15-w5-continuous-stage4-sniper.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage4_sniper.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
        --out ~/SHARED_DATA/w5_continuous_stage4_sniper_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
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
from liquidity_migration.continuous_forward_replay import build_full_ledger, frozen_config_hash  # noqa: E402
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
SWEEP_TAG = "w5_continuous_stage4_sniper_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage4-sniper.md"
MS_PER_DAY = 86_400_000
TRAIL_DAYS = 180
TRAIL_MS = TRAIL_DAYS * MS_PER_DAY
MIN_POOL = 20
DROP_K = 0.10
SEED = 0
# arm -> (drop_kind, cost_mult); drop_kind None=control
ARMS = {
    "T0_control": (None, 1.0),
    "T1_turnover_drop": ("turnover", 1.0),
    "T2_random_drop": ("random", 1.0),
    "T1_turnover_drop_2xcost": ("turnover", 2.0),
    "T2_random_drop_2xcost": ("random", 2.0),
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
    paths = [REPO / "scripts" / "w5_continuous_stage4_sniper.py",
             REPO / "liquidity_migration" / "continuous_events.py"]
    return hashlib.sha256("|".join(
        f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths if p.exists()).encode()).hexdigest()


def _day_floor(ts_ms: int) -> int:
    return (int(ts_ms) // MS_PER_DAY) * MS_PER_DAY


def _month_of(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def _unique_entries(stage0_dir: Path, venue: str) -> list[tuple[str, int, float]]:
    """Unique selected (symbol, signal_ts, turnover_quote), turnover is a (sym,ts) property."""
    tape = pl.read_parquet(stage0_dir / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    seen: dict[tuple[str, int], float] = {}
    for s, t, tq in tape.select("symbol", "signal_ts_ms", "turnover_quote").iter_rows():
        if tq is None:
            continue
        seen.setdefault((str(s), int(t)), float(tq))
    return sorted(((s, t, tq) for (s, t), tq in seen.items()), key=lambda r: (r[1], r[0]))


def _turnover_drop_set(entries: list[tuple[str, int, float]]) -> tuple[set[tuple[str, int]], dict[str, Any]]:
    """Drop entries whose turnover is in the bottom DROP_K of the causal trailing-180d pool."""
    ts = np.array([e[1] for e in entries], dtype=np.int64)
    tq = np.array([e[2] for e in entries], dtype=float)
    drop: set[tuple[str, int]] = set()
    pcts: list[float | None] = []
    lo = 0
    for i in range(len(entries)):
        t = ts[i]
        while ts[lo] < t - TRAIL_MS:  # advance trailing-window start
            lo += 1
        pool = tq[lo:i]  # strictly-before entries within 180d (sorted by ts, so contiguous)
        if pool.size < MIN_POOL:
            pcts.append(None)
            continue
        pct = float((pool < tq[i]).mean())
        pcts.append(pct)
        if pct < DROP_K:
            drop.add((entries[i][0], int(t)))
    eligible = sum(1 for p in pcts if p is not None)
    meta = {"n_entries": len(entries), "n_pool_eligible": eligible,
            "n_dropped": len(drop), "drop_frac": (len(drop) / eligible if eligible else 0.0)}
    return drop, meta


def _random_drop_set(entries: list[tuple[str, int, float]], turnover_drop: set[tuple[str, int]],
                     seed: int) -> set[tuple[str, int]]:
    """Per-day, drop the same COUNT as the turnover rule, chosen uniformly at random (seeded)."""
    by_day: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for s, t, _tq in entries:
        by_day[_day_floor(t)].append((s, int(t)))
    n_drop_by_day: dict[int, int] = defaultdict(int)
    for (s, t) in turnover_drop:
        n_drop_by_day[_day_floor(t)] += 1
    rng = np.random.RandomState(seed)
    drop: set[tuple[str, int]] = set()
    for day in sorted(by_day):
        k = n_drop_by_day.get(day, 0)
        if k <= 0:
            continue
        pool = by_day[day]
        idx = rng.choice(len(pool), size=min(k, len(pool)), replace=False)
        for j in idx:
            drop.add(pool[j])
    return drop


def _concentration(drop: set[tuple[str, int]], start_ms: int, end_ms: int) -> dict[str, Any]:
    if not drop:
        return {"distinct_symbols": 0, "top_symbol_share": 0.0, "thirds": [0, 0, 0]}
    sym_counts = Counter(s for s, _t in drop)
    top_share = max(sym_counts.values()) / len(drop)
    edges = [start_ms + (end_ms - start_ms) * k // 3 for k in range(4)]
    thirds = [0, 0, 0]
    for _s, t in drop:
        for k in range(3):
            if edges[k] <= t < edges[k + 1] or (k == 2 and t >= edges[3] - 1):
                thirds[k] += 1
                break
    return {"distinct_symbols": len(sym_counts), "top_symbol_share": float(top_share),
            "thirds": thirds}


def _run_arm(root: Path, venue: str, arm: str, lookup, cost_mult: float,
             start: str, end: str) -> dict[str, Any]:
    pieces: dict[str, Any] = {}
    trades_all: list[pl.DataFrame] = []
    overrides = {"round_trip_cost_multiplier": cost_mult} if cost_mult != 1.0 else {}
    t0 = time.time()
    for component, (_artifact_root, cell) in COMPONENTS.items():
        report_dir = root / "reports" / SWEEP_TAG / arm / component
        cfg = _component_config(component, cell, start=start, end=end, arm_overrides=overrides)
        run_continuous_event_research(root, config=cfg, report_dir=report_dir, size_mult_lookup=lookup)
        comp, trades, _r = _load_component(report_dir)
        pieces[component] = comp
        trades_all.append(trades)
    all_days = sorted({d for c in pieces.values() for d in c.days})
    btc_rets, btc_fund = _btc_inputs(root, venue, all_days)
    ledger = build_full_ledger(pieces, btc_rets, btc_fund)
    arm_dir = root / "reports" / SWEEP_TAG / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
    _monthly_returns(ledger).write_csv(arm_dir / "volume_event_best_monthly.csv")
    m = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    trades = pl.concat(trades_all) if trades_all else pl.DataFrame()
    nw = trades["notional_weight"].abs() if trades.height else pl.Series([], dtype=pl.Float64)
    total_notional = float(nw.sum()) if trades.height else 0.0
    n_nonzero = int((nw > 1e-12).sum()) if trades.height else 0
    (arm_dir / "volume_event_research_report.json").write_text(json.dumps(
        {"preregistration": PREREG, "run_label": "exploratory", "arm": arm, "venue": venue,
         "best_scenario": {"total_return": m.get("total_return"), "max_drawdown": m.get("max_drawdown"),
                           "mar": m.get("mar"), "trades": int(trades.height),
                           "nonzero_trades": n_nonzero}}, indent=2, default=str), encoding="utf-8")
    return {"arm": arm, "venue": venue, "elapsed_s": round(time.time() - t0, 1), "metrics": m,
            "n_entries": int(trades.height), "n_nonzero": n_nonzero, "total_notional": total_notional,
            "thirds_return": _thirds_return(ledger)}


def _thirds_return(ledger: pl.DataFrame) -> list[float]:
    if ledger.is_empty():
        return []
    br = ledger.select("ts_ms", "basket_return").sort("ts_ms")
    ts = br["ts_ms"].to_numpy()
    r = br["basket_return"].to_numpy()
    if ts.size == 0:
        return []
    edges = [ts.min() + (ts.max() - ts.min()) * k // 3 for k in range(4)]
    out = []
    for k in range(3):
        mask = (ts >= edges[k]) & (ts < edges[k + 1]) if k < 2 else (ts >= edges[2])
        out.append(float(np.nansum(r[mask])))
    return out


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    start_ms = int(dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    end_ms = int(dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "stage4_sniper", "out": str(out), "window": {"start": args.start, "end_exclusive": args.end},
        "sweep_tag": SWEEP_TAG, "git_head": _git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": frozen_config_hash(), "run_label": "exploratory",
        "params": {"trail_days": TRAIL_DAYS, "min_pool": MIN_POOL, "drop_k": DROP_K, "seed": SEED},
        "arms": list(ARMS.keys()), "pit_gate": {}, "venues": {}, "drop_meta": {},
    }
    metric_rows: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        entries = _unique_entries(stage0_dir, venue)
        turnover_drop, dmeta = _turnover_drop_set(entries)
        random_drop = _random_drop_set(entries, turnover_drop, SEED)
        lookups = {
            "turnover": {(s, t): 0.0 for (s, t) in turnover_drop},
            "random": {(s, t): 0.0 for (s, t) in random_drop},
        }
        payload["drop_meta"][venue] = {
            **dmeta,
            "n_random_dropped": len(random_drop),
            "turnover_concentration": _concentration(turnover_drop, start_ms, end_ms),
            "random_concentration": _concentration(random_drop, start_ms, end_ms),
        }
        venue_block: dict[str, Any] = {"arms": {}}
        for arm, (kind, cost) in ARMS.items():
            lookup = None if kind is None else lookups[kind]
            res = _run_arm(root, venue, arm, lookup, cost, args.start, args.end)
            venue_block["arms"][arm] = res
            metric_rows.append({"venue": venue, "arm": arm, "n_entries": res["n_entries"],
                                "n_nonzero": res["n_nonzero"], "return": res["metrics"].get("total_return"),
                                "mar": res["metrics"].get("mar"), "max_dd": res["metrics"].get("max_drawdown"),
                                "total_notional": res["total_notional"]})
        payload["venues"][venue] = venue_block

    _decide(payload, venues)
    _write_csv(out / "stage4_metrics.csv", metric_rows)
    (out / "stage4_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage4_summary.md")
    return payload


def _delta(payload, venue, arm) -> float | None:
    a = payload["venues"][venue]["arms"].get(arm, {}).get("metrics", {}).get("mar")
    z = payload["venues"][venue]["arms"].get("T0_control", {}).get("metrics", {}).get("mar")
    return None if a is None or z is None else a - z


def _pooled(payload, venues, arm) -> float | None:
    ds = [_delta(payload, v, arm) for v in venues if "arms" in payload["venues"].get(v, {})]
    ds = [d for d in ds if d is not None]
    return float(sum(ds) / len(ds)) if ds else None


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    t1 = _pooled(payload, venues, "T1_turnover_drop")
    t2 = _pooled(payload, venues, "T2_random_drop")
    t1x = _pooled(payload, venues, "T1_turnover_drop_2xcost")
    t2x = _pooled(payload, venues, "T2_random_drop_2xcost")
    fails: list[str] = []
    for venue in venues:
        if "arms" not in payload["venues"].get(venue, {}):
            continue
        a = payload["venues"][venue]["arms"]["T1_turnover_drop"]
        z = payload["venues"][venue]["arms"]["T0_control"]
        am, zm = a["metrics"], z["metrics"]
        d1 = _delta(payload, venue, "T1_turnover_drop")
        d2 = _delta(payload, venue, "T2_random_drop")
        if (am.get("total_return") or 0) <= 0:
            fails.append(f"non-positive return {venue}")
        if d1 is None or d1 <= 0:
            fails.append(f"MAR delta <=0 on {venue}")
        if d1 is not None and d2 is not None and d1 <= d2:
            fails.append(f"does not beat random-drop on {venue}")
        if am.get("max_drawdown") is not None and zm.get("max_drawdown") is not None and \
                abs(am["max_drawdown"]) > 1.1 * abs(zm["max_drawdown"]):
            fails.append(f"drawdown worse >10% {venue}")
        dm = payload["drop_meta"].get(venue, {})
        if not (0.08 <= dm.get("drop_frac", 0) <= 0.12):
            fails.append(f"drop fraction out of band on {venue}")
        conc = dm.get("turnover_concentration", {})
        if conc.get("top_symbol_share", 1.0) > 0.15:
            fails.append(f"dropped set symbol-concentrated on {venue}")
        if min(conc.get("thirds", [0, 0, 0])) <= 0:
            fails.append(f"dropped set misses a third on {venue}")
    # cost stress: both venues T1_2x MAR delta > 0 and beats T2_2x pooled
    for venue in venues:
        if "arms" not in payload["venues"].get(venue, {}):
            continue
        d1x = _delta(payload, venue, "T1_turnover_drop_2xcost")
        if d1x is None or d1x <= 0:
            fails.append(f"2x-cost MAR delta <=0 on {venue}")
    if t1 is not None and t2 is not None and t1 <= t2:
        fails.append("pooled: does not beat random-drop")
    if t1x is not None and t2x is not None and t1x <= t2x:
        fails.append("2x-cost pooled: does not beat random-drop")
    payload["decision"] = {"T1_turnover_drop": {
        "robust": not fails, "reasons": fails or ["robust"],
        "pooled_mar_delta": t1, "pooled_random": t2, "pooled_2xcost": t1x, "pooled_random_2xcost": t2x}}
    payload["robust"] = not fails


def _fmt(x, fmt="{:+.3f}"):
    return "" if x is None else fmt.format(x)


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]["T1_turnover_drop"]
    lines = [
        "# W5 Continuous Stage 4 — Liquidity Entry Sniper (drop least-liquid fades)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Pre-registration: `{payload['preregistration']}`",
        f"- Params: trail `{payload['params']['trail_days']}`d  min_pool `{payload['params']['min_pool']}`  "
        f"drop_k `{payload['params']['drop_k']}`  seed `{payload['params']['seed']}`",
        "",
        f"## Verdict: {'ROBUST liquidity-sniper candidate' if payload.get('robust') else 'NULL'}",
        "",
        f"T1_turnover_drop pooled ΔMAR vs T0: **{_fmt(d['pooled_mar_delta'])}** "
        f"(random-drop {_fmt(d['pooled_random'])}; 2×cost {_fmt(d['pooled_2xcost'])}, "
        f"random 2×cost {_fmt(d['pooled_random_2xcost'])}). "
        f"Reasons: {'; '.join(d['reasons'][:5])}.",
        "",
        "## Drop-set diagnostics",
        "",
        "| Venue | entries | pool-eligible | dropped | drop frac | rand dropped | distinct sym | top-sym share | thirds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for venue, dm in payload.get("drop_meta", {}).items():
        c = dm.get("turnover_concentration", {})
        lines.append(
            f"| `{venue}` | {dm.get('n_entries')} | {dm.get('n_pool_eligible')} | {dm.get('n_dropped')} | "
            f"{_fmt(dm.get('drop_frac'), '{:.3f}')} | {dm.get('n_random_dropped')} | "
            f"{c.get('distinct_symbols')} | {_fmt(c.get('top_symbol_share'), '{:.3f}')} | "
            f"{c.get('thirds')} |")
    lines.extend(["", "## Per-arm metrics", "",
                  "| Venue | Arm | Entries | Nonzero | Return | MAR | MaxDD | Notional |",
                  "|---|---|---:|---:|---:|---:|---:|---:|"])
    for venue, block in payload["venues"].items():
        for arm, a in block.get("arms", {}).items():
            m = a["metrics"]
            lines.append(f"| `{venue}` | `{arm}` | {a['n_entries']} | {a['n_nonzero']} | "
                         f"{_fmt(m.get('total_return'), '{:.4f}')} | {_fmt(m.get('mar'))} | "
                         f"{_fmt(m.get('max_drawdown'), '{:.4f}')} | {_fmt(a['total_notional'], '{:.2f}')} |")
    lines.extend(["", "Drop = `size_mult=0` (de-lever the slot; kept trades identical to V0). T1 beats T2",
                  "(random-drop) ⇒ liquidity SELECTION, not de-leveraging. Stage 4 `exploratory`.", ""])
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
    print(json.dumps({"out": payload["out"], "robust": payload.get("robust"),
                      "decision": payload.get("decision"),
                      "drop_meta": payload.get("drop_meta")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
