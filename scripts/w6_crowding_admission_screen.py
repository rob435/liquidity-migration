#!/usr/bin/env python3
"""W6 — crowding-ADMISSION screen (EXPLORATORY).

A1 (sizing), A4 (hedge-regime), A5 (gross-scaler) all NULL/weak: the orderflow squeeze
signal does not harvest by selecting/sizing/timing the EXISTING book. The one untested,
diffuse-edge-FRIENDLY direction is to ADD deployment: admit high-squeeze fades the crowding
gate currently REJECTS (entry_crowding_max_fresh=2 blocks the 3rd+ fresh fade per cycle).

This SCREEN (no engine, no deploy) asks the gating question BEFORE building an engine hook:
do the crowding-rejected entries fade profitably, and do HIGH-squeeze rejected entries fade
BETTER (so squeeze-conditioned admission beats blanket admission)?

Method (causal):
- For each crowding-rejected entry: hypothetical SHORT fade — enter at the entry bar close
  (entry_bar_end_ts_ms), exit at the per-component take-profit (intrabar low <= entry*(1-tp))
  or after the 24h hold, from klines_1h. Gross + net (flat round-trip cost).
- VALIDATION: the same model on SELECTED entries must reproduce the ledger net_return
  (entry-price match + return correlation), else the hypothetical is untrustworthy.
- Squeeze score = causal within-symbol z of oi_chg_24h (the A1 score).
- Statistic = within-symbol partial rank-IC of squeeze z vs gross fade return over composite
  (reuses the W6 screen's s7b helper: permutation null + symbol-hash degeneracy control),
  plus mean net fade return by squeeze tercile, vs the selected book's per-fade economics.

Run:
    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python scripts/w6_crowding_admission_screen.py \
        --venues bybit,binance --out ~/SHARED_DATA/w6_crowding_admission_screen_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
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

import scripts.w6_squeeze_proxy_screen as scr  # loads s7/s7b with the w4-stub  # noqa: E402

s7b = scr.s7b
from scripts._w5_shared import (  # noqa: E402
    MS_PER_HOUR,
    ROOTS,
    SHARED,
    _bars,
)
from scripts.w6_squeeze_proxy_sizing import (  # noqa: E402  (also installs corrected funding guard)
    _attach_raw_features,
    _within_symbol_z,
)

STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
OUT_TAG = "w6_crowding_admission_screen_2026-06-15"
HOLD_MS = 24 * MS_PER_HOUR
TP_BY_COMPONENT = {"turn3p3": 0.10, "turn4p3": 0.10, "turn4p5": 0.10, "age210tp14": 0.14}
FLAT_COST = 0.0015  # representative round-trip (book pays ~15-20 bp); IC is cost-invariant
P_THRESH = 0.025
COVERAGE_FLOOR = 200


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _gather_bars(venue: str, symbol: str, lo_ms: int, hi_ms: int) -> list[tuple[int, float, float, float]]:
    """Klines_1h (ts_open, high, low, close) for symbol over [lo, hi], across date partitions."""
    out: list[tuple[int, float, float, float]] = []
    d0 = dt.datetime.fromtimestamp(lo_ms / 1000, tz=dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp(hi_ms / 1000, tz=dt.timezone.utc).date()
    day = d0
    while day <= d1:
        for bar in _bars(venue, symbol, day.isoformat()):
            if lo_ms <= bar[0] <= hi_ms:
                out.append(bar)
        day += dt.timedelta(days=1)
    out.sort(key=lambda b: b[0])
    return out


def _fade_return(venue: str, symbol: str, entry_end_ts: int, tp: float) -> tuple[float, str] | None:
    """Causal SHORT fade gross return: enter at the bar closing at entry_end_ts; exit at the
    intrabar TP (low <= entry*(1-tp)) or the close after HOLD_MS. Bar ts_ms = bar OPEN."""
    bars = _gather_bars(venue, symbol, entry_end_ts - MS_PER_HOUR, entry_end_ts + HOLD_MS + MS_PER_HOUR)
    if not bars:
        return None
    # entry = close of the bar whose OPEN == entry_end_ts - 1h (i.e. the bar that closes at entry_end_ts)
    entry_bar = next((b for b in bars if b[0] == entry_end_ts - MS_PER_HOUR), None)
    if entry_bar is None:
        return None
    entry_price = entry_bar[3]
    if entry_price <= 0:
        return None
    tp_level = entry_price * (1.0 - tp)
    exit_bars = [b for b in bars if entry_end_ts <= b[0] < entry_end_ts + HOLD_MS]
    if not exit_bars:
        return None
    for b in exit_bars:  # chronological; first TP touch wins
        if b[2] <= tp_level:  # low <= TP level -> short take-profit
            return (entry_price - tp_level) / entry_price, "take_profit"
    exit_price = exit_bars[-1][3]  # max-hold: close of last in-window bar
    return (entry_price - exit_price) / entry_price, "max_hold"


def _rejected_rows(tape: pl.DataFrame) -> list[dict[str, Any]]:
    cr = tape.filter((~pl.col("selected")) & (pl.col("reason") == "crowding"))
    cols = ["symbol", "signal_ts_ms", "entry_bar_end_ts_ms", "component", "composite", "symbol_hash_bucket"]
    rows = cr.select([c for c in cols if c in cr.columns]).to_dicts()
    rows.sort(key=lambda r: (int(r["signal_ts_ms"]), str(r["symbol"])))
    return rows


def _attach_fade(rows: list[dict[str, Any]], venue: str) -> int:
    n = 0
    for r in rows:
        tp = TP_BY_COMPONENT.get(str(r.get("component")), 0.10)
        fr = _fade_return(venue, str(r["symbol"]), int(r["entry_bar_end_ts_ms"]), tp)
        if fr is None:
            r["fade_gross"], r["fade_exit"] = None, None
        else:
            r["fade_gross"], r["fade_exit"] = fr[0], fr[1]
            n += 1
    return n


def _validate_against_ledger(venue: str, root: Path) -> dict[str, Any]:
    """Run the fade model on SELECTED entries and compare to the actual ledger net_return."""
    pairs: list[tuple[float, float]] = []
    entry_px_err: list[float] = []
    for comp, tp in TP_BY_COMPONENT.items():
        csv = root / "reports" / STAGE0_TAG / comp / "continuous_trades.csv"
        if not csv.exists():
            continue
        led = pl.read_csv(csv)
        sub = led.select("symbol", "entry_ts_ms", "entry_price", "net_return", "notional_weight").head(400)
        for sym, ets, epx, nr, nw in sub.iter_rows():
            fr = _fade_return(venue, str(sym), int(ets), tp)
            if fr is None:
                continue
            # ledger net_return is per deploy-capital (scaled by notional_weight); compare per-notional
            per_notional = float(nr) / max(abs(float(nw)), 1e-9)
            pairs.append((fr[0] - FLAT_COST, per_notional))
            bars = _gather_bars(venue, str(sym), int(ets) - MS_PER_HOUR, int(ets) + MS_PER_HOUR)
            eb = next((b for b in bars if b[0] == int(ets) - MS_PER_HOUR), None)
            if eb and float(epx) > 0:
                entry_px_err.append(abs(eb[3] - float(epx)) / float(epx))
    if not pairs:
        return {"n": 0}
    a = np.array([p[0] for p in pairs])
    b = np.array([p[1] for p in pairs])
    corr = float(np.corrcoef(a, b)[0, 1]) if len(a) > 2 else None
    return {"n": len(pairs), "model_mean": float(a.mean()), "ledger_mean": float(b.mean()),
            "return_corr": corr, "entry_price_mape": (float(np.mean(entry_px_err)) if entry_px_err else None)}


def _tercile_means(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sub = [(r["_z"], r["fade_gross"]) for r in rows if r.get("_z") is not None and r.get("fade_gross") is not None]
    if len(sub) < 30:
        return {"n": len(sub)}
    sub.sort(key=lambda x: x[0])
    n = len(sub)
    lo = [s[1] for s in sub[: n // 3]]
    hi = [s[1] for s in sub[2 * n // 3:]]
    return {"n": n,
            "low_sq_gross_mean": float(np.mean(lo)), "high_sq_gross_mean": float(np.mean(hi)),
            "low_sq_net_mean": float(np.mean(lo) - FLAT_COST), "high_sq_net_mean": float(np.mean(hi) - FLAT_COST),
            "all_gross_mean": float(np.mean([s[1] for s in sub])),
            "all_net_mean": float(np.mean([s[1] for s in sub]) - FLAT_COST)}


def _ic(rows: list[dict[str, Any]], feature_key: str, perms: int, seed: int) -> dict[str, Any]:
    sub = [r for r in rows if r.get(feature_key) is not None and r.get("fade_gross") is not None
           and r.get("composite") is not None]
    if len(sub) < COVERAGE_FLOOR:
        return {"covered": len(sub), "ic_obs": None, "p_value": None, "degenerate": None, "thirds": []}
    sym = [str(r["symbol"]) for r in sub]
    counts = Counter(sym)
    kept = np.array([i for i in range(len(sub)) if counts[sym[i]] >= 2])
    score = np.array([float(r[feature_key]) for r in sub])
    ret = np.array([float(r["fade_gross"]) for r in sub])
    comp = np.array([float(r["composite"]) for r in sub])
    ts = np.array([int(r["signal_ts_ms"]) for r in sub])
    return s7b._arm_within_symbol(score, ret, comp, kept, sym, ts, perms, seed)


def run(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0 = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "stage": "w6_crowding_admission_screen",
        "run_label": "exploratory", "git_head": _git_head(), "flat_cost": FLAT_COST,
        "hold_h": HOLD_MS // MS_PER_HOUR, "tp_by_component": TP_BY_COMPONENT, "venues": {},
    }
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        tape = pl.read_parquet(stage0 / f"candidate_tape_{venue}.parquet")
        rows = _rejected_rows(tape)
        n_fade = _attach_fade(rows, venue)
        _attach_raw_features(rows, root, venue)
        z = _within_symbol_z(rows, "oi_chg_24h")
        for i, r in enumerate(rows):
            r["_z"] = z[i]
        n_sq = sum(1 for r in rows if r.get("_z") is not None)
        valid = _validate_against_ledger(venue, root)
        terc = _tercile_means(rows)
        ic_sq = _ic(rows, "_z", args.permutations, args.seed)
        ic_ctrl = _ic(rows, "symbol_hash_bucket", args.permutations, args.seed)
        payload["venues"][venue] = {
            "n_crowding_rejected": len(rows), "n_fade_modeled": n_fade, "n_squeeze_scored": n_sq,
            "ledger_validation": valid, "tercile_means": terc,
            "ic_squeeze": ic_sq, "ic_symbol_hash_control": ic_ctrl,
        }
        v = valid
        print(f"[{venue}] rejected={len(rows)} fade={n_fade} sq={n_sq} | "
              f"valid(corr={v.get('return_corr')}, px_mape={v.get('entry_price_mape')}) | "
              f"net all={terc.get('all_net_mean')} hi={terc.get('high_sq_net_mean')} lo={terc.get('low_sq_net_mean')} | "
              f"IC={ic_sq.get('ic_obs')} p={ic_sq.get('p_value')} ctrl_degen={ic_ctrl.get('degenerate')}", flush=True)

    _decide(payload, venues)
    (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "summary.md", venues)
    return payload


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    d: dict[str, Any] = {}
    for venue in venues:
        b = payload["venues"].get(venue, {})
        if "error" in b:
            continue
        terc, ic, ctrl, val = b["tercile_means"], b["ic_squeeze"], b["ic_symbol_hash_control"], b["ledger_validation"]
        model_ok = (val.get("return_corr") is not None and val["return_corr"] >= 0.6
                    and (val.get("entry_price_mape") is None or val["entry_price_mape"] <= 0.02))
        rejected_profitable = terc.get("all_net_mean") is not None and terc["all_net_mean"] > 0
        squeeze_helps = (ic.get("ic_obs") is not None and ic["ic_obs"] > 0
                         and ic.get("p_value") is not None and ic["p_value"] < P_THRESH
                         and ctrl.get("degenerate") is True)
        high_ge_low = (terc.get("high_sq_net_mean") is not None and terc.get("low_sq_net_mean") is not None
                       and terc["high_sq_net_mean"] >= terc["low_sq_net_mean"])
        d[venue] = {"model_validated": model_ok, "rejected_profitable_net": rejected_profitable,
                    "squeeze_helps_ic": squeeze_helps, "high_ge_low_net": high_ge_low,
                    "admission_prior": bool(model_ok and rejected_profitable and (squeeze_helps or high_ge_low))}
    payload["decision"] = d
    payload["bybit_admission_prior"] = bool(d.get("bybit", {}).get("admission_prior"))


def _f(x, fmt="{:+.4f}") -> str:
    return "" if x is None else (fmt.format(x) if isinstance(x, (int, float)) else str(x))


def _write_md(payload: dict[str, Any], path: Path, venues: list[str]) -> None:
    lines = [
        "# W6 — Crowding-ADMISSION screen (EXPLORATORY)",
        "",
        f"- Generated: `{payload['generated_utc']}`  Git: `{payload['git_head']}`",
        f"- Hypothetical SHORT fade: entry@entry_bar close, exit TP(per-comp)/24h; flat cost {payload['flat_cost']}",
        f"- Run label: `{payload['run_label']}`",
        "",
        f"## bybit admission prior: **{payload.get('bybit_admission_prior')}**",
        "",
        "| Venue | rejected | fade | sq | val corr | px MAPE | net all | net hi-sq | net lo-sq | IC(sq) | p | ctrl degen |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for venue in venues:
        b = payload["venues"].get(venue, {})
        if "error" in b:
            lines.append(f"| `{venue}` | ERROR {b['error']} |")
            continue
        t, ic, ctrl, val = b["tercile_means"], b["ic_squeeze"], b["ic_symbol_hash_control"], b["ledger_validation"]
        lines.append(
            f"| `{venue}` | {b['n_crowding_rejected']} | {b['n_fade_modeled']} | {b['n_squeeze_scored']} | "
            f"{_f(val.get('return_corr'), '{:.3f}')} | {_f(val.get('entry_price_mape'), '{:.4f}')} | "
            f"{_f(t.get('all_net_mean'))} | {_f(t.get('high_sq_net_mean'))} | {_f(t.get('low_sq_net_mean'))} | "
            f"{_f(ic.get('ic_obs'), '{:.3f}')} | {_f(ic.get('p_value'), '{:.3f}')} | {ctrl.get('degenerate')} |")
    lines += ["", "## Decision (per venue)", ""]
    for venue, dd in payload.get("decision", {}).items():
        lines.append(f"- `{venue}`: model_validated={dd['model_validated']} rejected_profitable_net={dd['rejected_profitable_net']} "
                     f"squeeze_helps_ic={dd['squeeze_helps_ic']} high_ge_low_net={dd['high_ge_low_net']} "
                     f"→ **admission_prior={dd['admission_prior']}**")
    lines += ["", "Screen only (`exploratory`). A positive bybit prior motivates an additive engine hook",
              "(squeeze-conditioned crowding admission, default off → byte-identical) + the A1-harness",
              "decision rule (multi-seed controls, cost stress, thirds, bybit-robust). Admissibility != harvestable.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--stage0", default=str(SHARED / STAGE0_TAG))
    ap.add_argument("--permutations", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=str(SHARED / OUT_TAG))
    args = ap.parse_args()
    payload = run(args)
    print(json.dumps({"out": str(Path(args.out).expanduser()),
                      "bybit_admission_prior": payload.get("bybit_admission_prior"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
