#!/usr/bin/env python3
"""Continuous V2 Exit-Alpha Phase 2 — exit-policy re-simulation sweep (both venues).

Pre-registration:
docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-construction.md

Re-simulates the exit policy for each V2_CONTROL short trade on the causal klines_1h
path from entry out to 72h, for a pre-registered grid of policies that are all either
profit-conditional (never exit a loser early) or symmetric (never cut an eventual TP
winner short of its target): TP-threshold sweep, hold extension, time-decaying TP, and
partial/two-stage exits. The (T=10%, H=24h) policy reproduces the frozen control
(recon validated). EXPLORATORY no-order shadow: any winner is a frozen-v2 parameter
change (operator-gated, voids the forward ledger); per-trade re-sim with no rebalance
re-solve. Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

import polars as pl

HOUR_MS = 3_600_000
PATH_HOURS = 73


def _close_at_hold(path: list[tuple[int, float, float, float, float]], hold_h: int) -> tuple[int, float]:
    """Exit hour/close for a time exit at hold_h (or the last available bar)."""
    last = path[-1]
    for (h, _o, _hi, _lo, close) in path:
        if h >= hold_h:
            return h, close
    return last[0], last[4]


def simulate_policy(path: list[tuple[int, float, float, float, float]], entry: float, policy: dict[str, Any]) -> float:
    """Return the realized raw short return for a policy on the path. path rows are
    (h, open, high, low, close) with h=1.. from entry; a short profits as price falls,
    TP at price entry*(1-T) fills intrabar when low <= that level."""
    if entry <= 0 or not path:
        return 0.0
    kind = policy["kind"]
    if kind in ("tp_hold", "vol_tp"):
        T = float(policy["T"])
        H = int(policy["H"])
        tp_price = entry * (1.0 - T)
        for (h, _o, _hi, low, _c) in path:
            if h > H:
                break
            if low <= tp_price:
                return T
        h_exit, close = _close_at_hold(path, H)
        return (entry - close) / entry
    if kind == "decay_tp":
        T0 = float(policy["T0"])
        Tmin = float(policy["Tmin"])
        h_start = int(policy["h_start"])
        H = int(policy["H"])
        for (h, _o, _hi, low, _c) in path:
            if h > H:
                break
            if h <= h_start:
                t_h = T0
            else:
                frac = min(1.0, (h - h_start) / max(1, H - h_start))
                t_h = T0 + (Tmin - T0) * frac
            if low <= entry * (1.0 - t_h):
                return t_h
        _h, close = _close_at_hold(path, H)
        return (entry - close) / entry
    if kind == "partial":
        f = float(policy["f"])
        T1 = float(policy["T1"])
        T2 = float(policy["T2"])
        H = int(policy["H"])
        _h, close = _close_at_hold(path, H)
        time_raw = (entry - close) / entry

        def leg(T: float) -> float:
            tp_price = entry * (1.0 - T)
            for (h, _o, _hi, low, _c) in path:
                if h > H:
                    break
                if low <= tp_price:
                    return T
            return time_raw

        return f * leg(T1) + (1.0 - f) * leg(T2)
    raise ValueError(f"unknown policy kind: {kind}")


def evaluate_policies(
    trades: list[dict[str, Any]],
    fetch_path: Callable[[str, int], list[tuple[int, float, float, float, float]] | None],
    policies: list[dict[str, Any]],
    *,
    control_name: str = "control_10_24",
) -> dict[str, Any]:
    prepared = []
    for tr in trades:
        path = fetch_path(str(tr["symbol"]), int(tr["entry_ts_ms"]))
        if path:
            month = dt.datetime.fromtimestamp(int(tr["entry_ts_ms"]) / 1000, tz=dt.timezone.utc).strftime("%Y-%m")
            prepared.append((tr, path, month))
    n = len(prepared)
    out: dict[str, Any] = {"n_with_path": n, "n_input": len(trades), "policies": {}}
    if n == 0:
        return out
    recon_err = 0.0
    per_policy_total: dict[str, float] = {}
    per_policy_month: dict[str, dict[str, float]] = {}
    for pol in policies:
        name = pol["name"]
        total = 0.0
        bymonth: dict[str, float] = {}
        for (tr, path, month) in prepared:
            entry = float(tr["entry_price"])
            nw = float(tr["notional_weight"])
            cost = float(tr.get("cost_return", 0.0) or 0.0)
            fund = float(tr.get("funding_return", 0.0) or 0.0)
            raw = simulate_policy(path, entry, pol)
            contrib = raw * nw + cost + fund
            total += contrib
            bymonth[month] = bymonth.get(month, 0.0) + contrib
            if name == control_name:
                recon_err += abs(raw - (entry - float(tr["exit_price"])) / entry)
        per_policy_total[name] = total
        per_policy_month[name] = bymonth
    control_total = per_policy_total.get(control_name, 0.0)
    out["recon_mean_abs_raw_err_vs_ledger"] = recon_err / n
    out["control_total_contrib"] = control_total
    for pol in policies:
        name = pol["name"]
        total = per_policy_total[name]
        delta = total - control_total
        # monthly stability of the delta vs control
        months = sorted(set(per_policy_month[name]) | set(per_policy_month[control_name]))
        month_deltas = [per_policy_month[name].get(m, 0.0) - per_policy_month[control_name].get(m, 0.0) for m in months]
        pos_months = sum(1 for d in month_deltas if d > 0)
        out["policies"][name] = {
            "spec": pol,
            "total_contrib": total,
            "delta_vs_control": delta,
            "delta_pct_of_control": (delta / control_total) if abs(control_total) > 1e-12 else None,
            "months": len(months),
            "delta_positive_month_share": (pos_months / len(months)) if months else None,
        }
    return out


def _date_str(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def _make_path_fetcher(klines1h_root: Path) -> Callable[[str, int], list[tuple[int, float, float, float, float]] | None]:
    cache: dict[tuple[str, str], pl.DataFrame | None] = {}

    def _day(symbol: str, date: str) -> pl.DataFrame | None:
        key = (symbol, date)
        if key not in cache:
            part = klines1h_root / f"date={date}" / f"symbol={symbol}"
            try:
                cache[key] = pl.read_parquet(part).select("ts_ms", "open", "high", "low", "close") if part.exists() else None
            except Exception:  # noqa: BLE001
                cache[key] = None
        return cache[key]

    def fetch(symbol: str, entry_ts_ms: int) -> list[tuple[int, float, float, float, float]] | None:
        end = entry_ts_ms + PATH_HOURS * HOUR_MS
        dates = sorted({_date_str(entry_ts_ms + k * 12 * HOUR_MS) for k in range(0, (PATH_HOURS // 12) + 2)})
        frames = [d for d in (_day(symbol, dd) for dd in dates) if d is not None]
        if not frames:
            return None
        df = pl.concat(frames).unique(subset=["ts_ms"]).filter(
            (pl.col("ts_ms") >= entry_ts_ms) & (pl.col("ts_ms") < end)
        ).sort("ts_ms")
        if df.is_empty():
            return None
        return [
            (int((int(r["ts_ms"]) - entry_ts_ms) // HOUR_MS) + 1, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            for r in df.iter_rows(named=True)
        ]

    return fetch


POLICIES: list[dict[str, Any]] = [
    {"name": "control_10_24", "kind": "tp_hold", "T": 0.10, "H": 24},
    {"name": "tp04_24", "kind": "tp_hold", "T": 0.04, "H": 24},
    {"name": "tp05_24", "kind": "tp_hold", "T": 0.05, "H": 24},
    {"name": "tp06_24", "kind": "tp_hold", "T": 0.06, "H": 24},
    {"name": "tp08_24", "kind": "tp_hold", "T": 0.08, "H": 24},
    {"name": "tp12_24", "kind": "tp_hold", "T": 0.12, "H": 24},
    {"name": "tp15_24", "kind": "tp_hold", "T": 0.15, "H": 24},
    {"name": "tp10_36", "kind": "tp_hold", "T": 0.10, "H": 36},
    {"name": "tp10_48", "kind": "tp_hold", "T": 0.10, "H": 48},
    {"name": "tp10_72", "kind": "tp_hold", "T": 0.10, "H": 72},
    {"name": "decay_10to6_24", "kind": "decay_tp", "T0": 0.10, "Tmin": 0.06, "h_start": 12, "H": 24},
    {"name": "decay_10to4_24", "kind": "decay_tp", "T0": 0.10, "Tmin": 0.04, "h_start": 12, "H": 24},
    {"name": "partial_50_5_10_24", "kind": "partial", "f": 0.5, "T1": 0.05, "T2": 0.10, "H": 24},
    {"name": "partial_50_6_10_24", "kind": "partial", "f": 0.5, "T1": 0.06, "T2": 0.10, "H": 24},
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ab-root", required=True)
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--shared-root", default=str(Path.home() / "SHARED_DATA"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    roots = {"bybit": Path(args.shared_root) / "bybit_full_pit", "binance": Path(args.shared_root) / "binance_full_pit"}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_shadow", "claimed_venue_scope": "both_venue_no_order_shadow", "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        trades = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv").filter(pl.col("side") == "short")
        if args.limit:
            trades = trades.head(args.limit)
        fetch = _make_path_fetcher(roots[venue] / "klines_1h")
        summary["venues"][venue] = evaluate_policies(trades.to_dicts(), fetch, POLICIES)
    (out / "f2_exit_policy_sweep.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    # report + both-venue dominance check
    venues = list(summary["venues"])
    lines = ["# Continuous V2 Exit-Alpha Phase 2 — exit-policy sweep (both-venue no-order shadow)", "",
             "Exploratory; any winner is an operator-gated frozen-v2 parameter change. Pre-reg: "
             "`docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-construction.md`", ""]
    for v in venues:
        r = summary["venues"][v]
        lines += [f"## {v} (n={r.get('n_with_path')}, recon_err={r.get('recon_mean_abs_raw_err_vs_ledger', float('nan')):.5f}, control_contrib={r.get('control_total_contrib', 0):.4f})", "",
                  "| policy | Δ vs control | Δ% | Δ-pos month share |", "| --- | ---: | ---: | ---: |"]
        for name, pr in r["policies"].items():
            pct = pr["delta_pct_of_control"]
            pm = pr["delta_positive_month_share"]
            lines.append(f"| {name} | {pr['delta_vs_control']:+.6f} | {'n/a' if pct is None else f'{pct:+.2%}'} | {'n/a' if pm is None else f'{pm:.0%}'} |")
        lines.append("")
    # both-venue dominators
    dom = []
    if len(venues) == 2:
        a, b = venues
        for name in summary["venues"][a]["policies"]:
            if name == "control_10_24":
                continue
            da = summary["venues"][a]["policies"][name]["delta_vs_control"]
            db = summary["venues"][b]["policies"][name]["delta_vs_control"]
            if da > 0 and db > 0:
                dom.append((name, da, db))
        lines.append("## Both-venue improvers (Δ>0 on both)")
        lines.append("")
        lines += ([f"- {n}: {a} {da:+.4f}, {b} {db:+.4f}" for n, da, db in dom] or ["- NONE — control (10% TP, 24h) not dominated on both venues."])
        lines.append("")
    (out / "f2_exit_policy_sweep_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"both_venue_improvers": [d[0] for d in dom], "venues": {v: {"n": summary["venues"][v]["n_with_path"], "recon_err": summary["venues"][v]["recon_mean_abs_raw_err_vs_ledger"]} for v in venues}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
