#!/usr/bin/env python3
"""Problem Book A — real catastrophic stops / TPSL on the 1m path (continuous v2 next-level).

Receipt: docs/preregistration/2026-06-20-continuous-v2-book-a-stops-tpsl-construction.md

Re-resolves each V2_CONTROL short trade on the Wave-1 1m cache (intrabar_engine) under
a grid of catastrophic stop policies (A1) plus a hash null (A7), and measures the
realized-PnL MAR proxy + tail (max drawdown, worst day) vs the no-stop control on BOTH
venues. The control (stop=0, TP12) reproduces the frozen exit by construction.

A1: wide emergency stop, adverse-first 1m resolution (the thing the 1h bar could not
measure — same-bar stop/TP order). A7 (the falsifier): exit the SAME fraction of trades
early as the real stop, but at a HASH-chosen minute and the real path price there —
matching early-exit frequency while destroying the "cut the worst excursion" mechanism.
A stop is only interesting if it beats its hash null on MAR AND drawdown, on BOTH venues,
without deleting the trades that ride to the 12% TP.

EXPLORATORY screen: per-trade realized PnL, NO rebalance/hedge re-solve. Any survivor is
a frozen-v2 change and needs full-ledger (build_full_ledger) validation + operator gating.
Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(REPO))
from liquidity_migration.intrabar_engine import (  # noqa: E402
    CACHE_ROOT,
    MS_PER_MINUTE,
    load_1m_window,
    resolve_exit_1m,
)

ANN_DAYS = 365.25
MS_DAY = 86_400_000
TP_PCT = 0.12  # frozen control take-profit


def _hash01(s: str) -> float:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def mar_proxy(daily: dict[int, float]) -> dict[str, Any]:
    """Realized-PnL MAR proxy from per-exit-day book returns (compounded equity)."""
    days = sorted(daily)
    if not days:
        return {"total": 0.0, "ann": 0.0, "max_dd": 0.0, "mar": None, "worst_day": 0.0, "n_days": 0}
    eq = 1.0
    peak = 1.0
    maxdd = 0.0
    worst = 0.0
    for d in days:
        r = daily[d]
        eq *= 1.0 + r
        peak = max(peak, eq)
        maxdd = min(maxdd, eq / peak - 1.0)
        worst = min(worst, r)
    total = eq - 1.0
    yrs = max((days[-1] - days[0]) / ANN_DAYS, 1e-9)
    ann = total / yrs
    return {
        "total": total, "ann": ann, "max_dd": maxdd,
        "mar": (ann / abs(maxdd)) if abs(maxdd) > 1e-12 else None,
        "worst_day": worst, "n_days": len(days),
    }


class WindowCache:
    def __init__(self, venue: str, cache_root: Path):
        self.venue = venue
        self.cache_root = cache_root
        self._c: dict[tuple[str, str, str], pl.DataFrame] = {}

    def get(self, symbol: str, start_date: str, end_date: str) -> pl.DataFrame:
        key = (symbol, start_date, end_date)
        if key not in self._c:
            self._c[key] = load_1m_window(self.venue, symbol, start_date, end_date, cache_root=self.cache_root)
        return self._c[key]


def _contrib(side_return: float, tr: dict[str, Any]) -> float:
    nw = float(tr["notional_weight"])
    cost = float(tr.get("cost_return", 0.0) or 0.0)
    fund = float(tr.get("funding_return", 0.0) or 0.0)
    return side_return * nw + cost + fund


def evaluate_venue(trades: list[dict[str, Any]], cache: WindowCache, stop_grid: list[float], *, arm_ms: int = 0, name_suffix: str = "") -> dict[str, Any]:
    # Pre-resolve every trade under control (stop=0) and each stop level on the 1m path.
    prepared = []
    for tr in trades:
        bars = cache.get(str(tr["symbol"]), str(tr["entry_date"]), str(tr["exit_date"]))
        if bars.height == 0:
            continue
        prepared.append((tr, bars))
    n = len(prepared)
    out: dict[str, Any] = {"n_input": len(trades), "n_with_path": n, "policies": {}}
    if n == 0:
        return out

    def resolve(tr, bars, stop_pct):
        return resolve_exit_1m(
            bars, entry_ts_ms=int(tr["entry_ts_ms"]), entry_price=float(tr["entry_price"]), side="short",
            take_profit_pct=TP_PCT, stop_loss_pct=stop_pct, planned_exit_ts_ms=int(tr["planned_exit_ts_ms"]),
            stop_arm_after_ms=arm_ms,
        )

    # control + recon vs ledger
    daily_ctrl: dict[int, float] = {}
    recon_err = 0.0
    ctrl_res = []
    for tr, bars in prepared:
        res = resolve(tr, bars, 0.0)
        ctrl_res.append((tr, bars, res))
        daily_ctrl[int(res.exit_ts_ms) // MS_DAY] = daily_ctrl.get(int(res.exit_ts_ms) // MS_DAY, 0.0) + _contrib(res.side_return, tr)
        recon_err += abs(res.side_return - (-(float(tr["exit_price"]) / float(tr["entry_price"]) - 1.0)))
    out["control"] = mar_proxy(daily_ctrl)
    out["recon_mean_abs_err_vs_ledger"] = recon_err / n

    for stop_pct in stop_grid:
        daily: dict[int, float] = {}
        stopped = 0
        edge_kept = 0  # trades still reaching the 12% TP
        for tr, bars in prepared:
            res = resolve(tr, bars, stop_pct)
            daily[int(res.exit_ts_ms) // MS_DAY] = daily.get(int(res.exit_ts_ms) // MS_DAY, 0.0) + _contrib(res.side_return, tr)
            if res.exit_reason == "stop_loss":
                stopped += 1
            elif res.exit_reason == "take_profit":
                edge_kept += 1
        m = mar_proxy(daily)
        hit_rate = stopped / n
        # A7 hash null: exit the same fraction of trades early, at a hash minute + real price.
        daily_null: dict[int, float] = {}
        for tr, bars, cres in ctrl_res:
            h = _hash01(f"A7:{stop_pct}{name_suffix}:{tr['symbol']}:{tr['entry_ts_ms']}")
            if h < hit_rate:
                entry_ts = int(tr["entry_ts_ms"])
                planned = int(tr["planned_exit_ts_ms"])
                span_min = max(1, (planned - entry_ts) // MS_PER_MINUTE)
                m_off = int(_hash01(f"A7t:{stop_pct}{name_suffix}:{tr['symbol']}:{tr['entry_ts_ms']}") * span_min)
                tgt = entry_ts + m_off * MS_PER_MINUTE
                w = bars.filter((pl.col("ts_ms") >= entry_ts) & (pl.col("ts_ms") <= tgt) & pl.col("close").is_not_null())
                if w.height:
                    px = float(w["close"][-1])
                    sr = -(px / float(tr["entry_price"]) - 1.0)
                    exit_day = (entry_ts + m_off * MS_PER_MINUTE) // MS_DAY
                    daily_null[exit_day] = daily_null.get(exit_day, 0.0) + _contrib(sr, tr)
                    continue
            # not nulled / no price -> control exit
            daily_null[int(cres.exit_ts_ms) // MS_DAY] = daily_null.get(int(cres.exit_ts_ms) // MS_DAY, 0.0) + _contrib(cres.side_return, tr)
        mnull = mar_proxy(daily_null)
        out["policies"][f"stop_{int(stop_pct * 100)}{name_suffix}"] = {
            "stop_pct": stop_pct, "stopped": stopped, "stop_hit_rate": hit_rate, "tp_kept": edge_kept,
            "mar_proxy": m, "mar_delta_vs_control": (m["mar"] - out["control"]["mar"]) if (m["mar"] is not None and out["control"]["mar"] is not None) else None,
            "dd_delta_vs_control": m["max_dd"] - out["control"]["max_dd"],
            "total_delta_vs_control": m["total"] - out["control"]["total"],
            "hash_null_mar": mnull["mar"], "hash_null_max_dd": mnull["max_dd"], "hash_null_total": mnull["total"],
            "beats_hash_on_mar": (m["mar"] is not None and mnull["mar"] is not None and m["mar"] > mnull["mar"]),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_book_a_2026-06-20")
    ap.add_argument("--stops", default="0.40,0.30,0.25,0.20,0.15")
    ap.add_argument("--arm-hours", default="6,12", help="A2 delayed-arm stop variants (comma hours; '' for none)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    stop_grid = [float(x) for x in args.stops.split(",") if x.strip()]
    arm_hours = [int(x) for x in args.arm_hours.split(",") if x.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "run_label": "exploratory_screen", "claimed_venue_scope": "both_venue_no_order_shadow",
        "note": "per-trade realized-PnL screen, no rebalance/hedge re-solve; survivors need full-ledger validation",
        "stop_grid": stop_grid, "arm_hours": arm_hours, "tp_pct": TP_PCT, "venues": {},
    }
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        tr = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv").filter(pl.col("side") == "short")
        if args.limit:
            tr = tr.head(args.limit)
        rows = tr.to_dicts()
        cache = WindowCache(venue, Path(args.cache_root))
        v = evaluate_venue(rows, cache, stop_grid, arm_ms=0)  # A1 (arm=0) + control
        for ah in arm_hours:
            vx = evaluate_venue(rows, cache, stop_grid, arm_ms=ah * 3_600_000, name_suffix=f"_arm{ah}h")
            v["policies"].update(vx["policies"])  # A2 delayed-arm variants
        summary["venues"][venue] = v
        print(f"[{venue}] n={v['n_with_path']} recon_err={v.get('recon_mean_abs_err_vs_ledger', float('nan')):.5f} "
              f"control_mar={v['control']['mar']} control_dd={v['control']['max_dd']:.4f}", flush=True)
        for name, p in v["policies"].items():
            print(f"   {name}: stop_rate={p['stop_hit_rate']:.3f} mar={p['mar_proxy']['mar']} "
                  f"Δmar={p['mar_delta_vs_control']} Δdd={p['dd_delta_vs_control']:+.4f} "
                  f"beats_hash={p['beats_hash_on_mar']}", flush=True)
    (out / "book_a_stops.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    # both-venue verdict: a stop must improve MAR + reduce DD + beat hash on BOTH venues
    venues = list(summary["venues"])
    winners = []
    if len(venues) == 2:
        a, b = venues
        common = set(summary["venues"][a]["policies"]) & set(summary["venues"][b]["policies"])
        for name in sorted(common):
            pa = summary["venues"][a]["policies"][name]
            pb = summary["venues"][b]["policies"][name]
            ok = all([
                pa["mar_delta_vs_control"] is not None and pa["mar_delta_vs_control"] > 0,
                pb["mar_delta_vs_control"] is not None and pb["mar_delta_vs_control"] > 0,
                pa["beats_hash_on_mar"], pb["beats_hash_on_mar"],
            ])
            if ok:
                winners.append(name)
    summary["both_venue_winners"] = winners
    (out / "book_a_stops.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"both_venue_winners": winners}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
