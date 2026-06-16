#!/usr/bin/env python3
"""W6 — exhaustion/continuation entry screen (EXPLORATORY, bybit; local 5m).

Motivation: the book's losers are an IDENTIFIABLE failure mode, not a diffuse tilt. mae/mfe:
winning short fades go ~-5.6% underwater before reverting, LOSERS go -19% underwater (runaway
pumps) and barely revert. The B1 chase-limit result (t=-10): entries still pumping at +1h fade
terribly (win 8-13%). So intra-window CONTINUATION (price still rising into the +1h fill) should
predict the losers — a distinct prior from the failed diffuse levers (OI/path-shape/liquidity).

This SCREEN (no engine) asks: does causal intra-window continuation predict a WORSE fade on the
EXISTING (fixed +1h) entries? Continuation features from LOCAL bybit 5m klines over the +1h entry
window [signal_ts, signal_ts+1h]:
  - cont_ret  = close(+1h)/close(signal) - 1     (price move during the window; + = still pumping)
  - rollover  = (window_high - close(+1h))/window_high   (how far price rolled off its high; + = exhaustion)
  - new_high  = close(+1h) >= 0.999*window_high   (making highs at fill = continuation)
Statistic = within-symbol partial rank-IC over the production composite vs gross_trade_return
(the price-fade), with the symbol-hash degeneracy control + thirds (reuses the W6 screen helper).
A NEGATIVE cont_ret IC / POSITIVE rollover IC ⇒ continuation predicts a worse fade ⇒ a
continuation-conditioned entry filter/delay is worth an engine test.

Run:
    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python scripts/w6_exhaustion_entry_screen.py \
        --venue bybit --out ~/SHARED_DATA/w6_exhaustion_entry_screen_2026-06-16
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

import scripts.w6_squeeze_proxy_screen as scr  # loads s7b (within-symbol IC) with the w4-stub  # noqa: E402

s7b = scr.s7b
from scripts._w5_shared import MS_PER_HOUR, ROOTS, SHARED  # noqa: E402

STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
OUT_TAG = "w6_exhaustion_entry_screen_2026-06-16"
COMPONENTS = ("turn3p3", "turn4p3", "turn4p5", "age210tp14")
FEATURES = ("cont_ret", "rollover", "new_high")
P_THRESH = 0.025
COVERAGE_FLOOR = 300


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _hash_bucket(symbol: str) -> float:
    import hashlib
    return (int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16) % 1000) / 999.0


def _load_5m(root: Path, symbols: set[str], lo_ms: int, hi_ms: int) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Per-symbol (ts_open, high, close) 5m arrays in [lo, hi]."""
    base = root / "klines_5m"
    df = (
        pl.scan_parquet(str(base / "**" / "*.parquet"))
        .select("ts_ms", "symbol", "high", "close")
        .filter(pl.col("symbol").is_in(list(symbols)) & pl.col("ts_ms").is_between(lo_ms, hi_ms))
        .sort("ts_ms").collect()
    )
    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for sym, sub in df.group_by("symbol"):
        s = str(sym[0] if isinstance(sym, tuple) else sym)
        out[s] = (sub["ts_ms"].to_numpy(), sub["high"].to_numpy(), sub["close"].to_numpy())
    return out


def _population(root: Path, venue: str, stage0: Path) -> list[dict[str, Any]]:
    """Executed trades (unique symbol,signal_ts) with composite + gross_trade_return + mae."""
    tape = pl.read_parquet(stage0 / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    comp_map = {(str(s), int(t)): (float(c) if c is not None else None)
                for s, t, c in tape.select("symbol", "signal_ts_ms", "composite").iter_rows()}
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for comp in COMPONENTS:
        csv = root / "reports" / STAGE0_TAG / comp / "continuous_trades.csv"
        if not csv.exists():
            continue
        led = pl.read_csv(csv).select("symbol", "entry_signal_ts_ms", "entry_ts_ms",
                                      "gross_trade_return", "mae")
        for s, sig, ets, g, mae in led.iter_rows():
            key = (str(s), int(sig))
            if key in rows:
                continue  # unique entry (continuation is component-independent)
            comp_v = comp_map.get(key)
            if comp_v is None:
                continue
            rows[key] = {"symbol": str(s), "signal_ts_ms": int(sig), "entry_ts_ms": int(ets),
                         "composite": comp_v, "symbol_hash_bucket": _hash_bucket(str(s)),
                         "gross_trade_return": float(g), "mae": float(mae)}
    out = list(rows.values())
    out.sort(key=lambda r: (r["signal_ts_ms"], r["symbol"]))
    return out


def _attach_continuation(rows: list[dict[str, Any]], root: Path) -> int:
    symbols = {r["symbol"] for r in rows}
    lo = min(r["signal_ts_ms"] for r in rows) - MS_PER_HOUR
    hi = max(r["entry_ts_ms"] for r in rows) + MS_PER_HOUR
    series = _load_5m(root, symbols, lo, hi)
    n = 0
    for r in rows:
        s = r["symbol"]
        sig, ent = r["signal_ts_ms"], r["entry_ts_ms"]
        pair = series.get(s)
        for f in FEATURES:
            r[f] = None
        if pair is None:
            continue
        ts, high, close = pair
        # signal close = 5m close at/just-before signal_ts; window = (signal_ts, entry_ts]
        i_sig = int(np.searchsorted(ts, sig, side="right")) - 1
        win = (ts > sig) & (ts <= ent)
        if i_sig < 0 or not win.any():
            continue
        c_sig = float(close[i_sig])
        c_end = float(close[win][-1])  # 5m close nearest the +1h fill
        w_high = float(high[win].max())
        if c_sig <= 0 or w_high <= 0:
            continue
        r["cont_ret"] = c_end / c_sig - 1.0
        r["rollover"] = (w_high - c_end) / w_high
        r["new_high"] = 1.0 if c_end >= 0.999 * w_high else 0.0
        n += 1
    return n


def _ic(rows: list[dict[str, Any]], feat: str, perms: int, seed: int) -> dict[str, Any]:
    sub = [r for r in rows if r.get(feat) is not None and r.get("composite") is not None]
    if len(sub) < COVERAGE_FLOOR:
        return {"covered": len(sub), "ic_obs": None, "p_value": None, "degenerate": None, "thirds": []}
    sym = [r["symbol"] for r in sub]
    counts = Counter(sym)
    kept = np.array([i for i in range(len(sub)) if counts[sym[i]] >= 2])
    score = np.array([float(r[feat]) for r in sub])
    ret = np.array([float(r["gross_trade_return"]) for r in sub])
    comp = np.array([float(r["composite"]) for r in sub])
    ts = np.array([int(r["signal_ts_ms"]) for r in sub])
    return s7b._arm_within_symbol(score, ret, comp, kept, sym, ts, perms, seed)


def _buckets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sub = [(r["cont_ret"], r["gross_trade_return"], r["mae"]) for r in rows
           if r.get("cont_ret") is not None]
    if len(sub) < 30:
        return {"n": len(sub)}
    sub.sort(key=lambda x: x[0])
    n = len(sub)
    lo, hi = sub[: n // 3], sub[2 * n // 3:]
    def stat(b):
        g = np.array([x[1] for x in b])
        m = np.array([x[2] for x in b])
        return {"gross_fade_mean": float(g.mean()), "win_rate": float((g > 0).mean()), "mae_mean": float(m.mean())}
    return {"n": n, "low_cont": stat(lo), "high_cont": stat(hi)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venue = args.venue
    root = ROOTS[venue]
    rows = _population(root, venue, Path(args.stage0).expanduser())
    n_cont = _attach_continuation(rows, root)
    ics = {f: _ic(rows, f, args.permutations, args.seed) for f in FEATURES}
    ctrl = _ic(rows, "symbol_hash_bucket", args.permutations, args.seed)
    buckets = _buckets(rows)
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "git_head": _git_head(),
        "run_label": "exploratory", "venue": venue, "n_entries": len(rows), "n_continuation": n_cont,
        "ics": ics, "symbol_hash_control": ctrl, "buckets": buckets,
    }
    # decision: continuation predicts a worse fade (cont_ret IC<0 OR rollover IC>0), control degenerate
    cr, ro = ics["cont_ret"], ics["rollover"]
    sig = []
    if cr.get("ic_obs") is not None and cr.get("p_value") is not None and cr["p_value"] < P_THRESH and cr["ic_obs"] < 0:
        sig.append("cont_ret<0")
    if ro.get("ic_obs") is not None and ro.get("p_value") is not None and ro["p_value"] < P_THRESH and ro["ic_obs"] > 0:
        sig.append("rollover>0")
    payload["continuation_predicts_losers"] = bool(sig) and ctrl.get("degenerate") is True
    payload["signals"] = sig
    (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "summary.md")
    print(json.dumps({"out": str(out), "n": len(rows), "n_cont": n_cont,
                      "continuation_predicts_losers": payload["continuation_predicts_losers"],
                      "ics": {f: {"ic": ics[f].get("ic_obs"), "p": ics[f].get("p_value")} for f in FEATURES},
                      "ctrl_degenerate": ctrl.get("degenerate"), "buckets": buckets}, indent=2, default=str))
    return payload


def _f(x, fmt="{:+.4f}") -> str:
    return "" if x is None else (fmt.format(x) if isinstance(x, (int, float)) else str(x))


def _write_md(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W6 — Exhaustion/continuation entry screen (EXPLORATORY, bybit, local 5m)",
        "",
        f"- Generated: `{payload['generated_utc']}`  Git: `{payload['git_head']}`",
        f"- n_entries `{payload['n_entries']}`  n_continuation `{payload['n_continuation']}`  "
        f"control degenerate: `{payload['symbol_hash_control'].get('degenerate')}`",
        "",
        f"## Continuation predicts losers: **{payload['continuation_predicts_losers']}**  signals={payload['signals']}",
        "",
        "Within-symbol partial rank-IC over composite vs gross_trade_return (the price-fade):",
        "",
        "| feature | IC | p | covered | thirds |",
        "|---|---:|---:|---:|---|",
    ]
    for f in FEATURES:
        d = payload["ics"][f]
        th = ";".join(s7b._fmt(t) for t in d.get("thirds", [])) if d.get("thirds") else ""
        lines.append(f"| `{f}` | {_f(d.get('ic_obs'), '{:+.3f}')} | {_f(d.get('p_value'), '{:.3f}')} | {d.get('covered')} | {th} |")
    b = payload["buckets"]
    if "low_cont" in b:
        lines += ["", "## cont_ret terciles (low = exhausted at fill, high = still pumping):", "",
                  "| bucket | gross fade mean | win rate | mae mean |", "|---|---:|---:|---:|",
                  f"| low cont | {_f(b['low_cont']['gross_fade_mean'])} | {_f(b['low_cont']['win_rate'], '{:.3f}')} | {_f(b['low_cont']['mae_mean'])} |",
                  f"| high cont | {_f(b['high_cont']['gross_fade_mean'])} | {_f(b['high_cont']['win_rate'], '{:.3f}')} | {_f(b['high_cont']['mae_mean'])} |"]
    lines += ["", "Screen only (`exploratory`). If continuation predicts worse fades AND separates the",
              "high-mae losers, a continuation-conditioned entry filter/delay earns an engine test",
              "(multi-seed controls, cost stress, thirds, bybit-robust). Selection shrinks breadth —",
              "the distinct prior is the IDENTIFIABLE runaway-loser population, not a diffuse tilt.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--stage0", default=str(SHARED / STAGE0_TAG))
    ap.add_argument("--permutations", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=str(SHARED / OUT_TAG))
    args = ap.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
