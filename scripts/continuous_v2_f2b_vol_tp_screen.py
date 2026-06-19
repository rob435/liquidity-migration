#!/usr/bin/env python3
"""Continuous V2 Exit-Alpha Phase 2b — volatility-scaled take-profit screen (both venues).

Pre-registration:
docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-construction.md (vol-scaled TP arm).

The flat raised-TP lifecycle was a venue split (Bybit MAR +; Binance DD doubled). The TP optimum
looks venue/volatility dependent, so this screens a volatility-NORMALIZED TP:
  T_i = clip(0.10 * (rv168_i / median_rv168) ** p, 0.05, 0.20)
(p=0 reproduces the flat 10% control; p>0 gives higher-vol names a wider target, lower-vol names a
tighter one). Because the per-trade contribution SUM is proven misleading for exits (it missed the
Binance drawdown blow-up), this screen is DD-AWARE: it builds a daily equity from per-trade
contributions (placed on the exit day) and reports an MAR proxy (annualized return / |max DD|). It is
VALIDATED by re-running the flat tp10/12/15 policies and checking the proxy reproduces the lifecycle
venue split before any vol-scaled conclusion is trusted.

SCREEN ONLY (no daily vol-target rebalance / concurrency re-solve — the proxy is an approximation of
the lifecycle MAR, used only to decide whether a per-trade-TP engine hook + full lifecycle is worth
building). Not a candidate; operator-gated; not real-money evidence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

import polars as pl

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
PATH_HOURS = 49  # 24h hold + buffer (vol TP capped at 0.20, still resolves within the hold)
ANN_DAYS = 365.25


def _date_str(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def _make_path_fetcher(klines1h_root: Path) -> Callable[[str, int], list[tuple[int, float, float]] | None]:
    cache: dict[tuple[str, str], pl.DataFrame | None] = {}

    def _day(symbol: str, date: str) -> pl.DataFrame | None:
        key = (symbol, date)
        if key not in cache:
            part = klines1h_root / f"date={date}" / f"symbol={symbol}"
            try:
                cache[key] = pl.read_parquet(part).select("ts_ms", "low", "close") if part.exists() else None
            except Exception:  # noqa: BLE001
                cache[key] = None
        return cache[key]

    def fetch(symbol: str, entry_ts_ms: int) -> list[tuple[int, float, float]] | None:
        end = entry_ts_ms + PATH_HOURS * HOUR_MS
        dates = sorted({_date_str(entry_ts_ms + k * 12 * HOUR_MS) for k in range((PATH_HOURS // 12) + 2)})
        frames = [d for d in (_day(symbol, dd) for dd in dates) if d is not None]
        if not frames:
            return None
        df = pl.concat(frames).unique(subset=["ts_ms"]).filter(
            (pl.col("ts_ms") >= entry_ts_ms) & (pl.col("ts_ms") < end)
        ).sort("ts_ms")
        if df.is_empty():
            return None
        return [(int((int(r["ts_ms"]) - entry_ts_ms) // HOUR_MS) + 1, float(r["low"]), float(r["close"])) for r in df.iter_rows(named=True)]

    return fetch


def simulate_tp(path: list[tuple[int, float, float]], entry: float, T: float, H: int) -> tuple[float, int]:
    """Short TP at entry*(1-T) (fills intrabar when low<=level) else time exit at hold H.
    Returns (raw_short_return, exit_hour)."""
    if entry <= 0 or not path:
        return 0.0, 0
    tp_price = entry * (1.0 - T)
    last_h, last_close = path[-1][0], path[-1][2]
    for (h, low, close) in path:
        if h > H:
            break
        if low <= tp_price:
            return T, h
        last_h, last_close = h, close
    return (entry - last_close) / entry, last_h


def evaluate(trades: list[dict[str, Any]], fetch: Callable[[str, int], list[tuple[int, float, float]] | None],
             policies: list[dict[str, Any]], *, median_rv: float, control_name: str = "control_10") -> dict[str, Any]:
    prepared = []
    for tr in trades:
        path = fetch(str(tr["symbol"]), int(tr["entry_ts_ms"]))
        if path:
            prepared.append((tr, path))
    n = len(prepared)
    out: dict[str, Any] = {"n_with_path": n, "median_rv168": median_rv, "policies": {}}
    if n == 0:
        return out

    def mar_proxy(daily: dict[int, float]) -> dict[str, float]:
        if not daily:
            return {"total": 0.0, "max_dd": 0.0, "mar": 0.0}
        days = sorted(daily)
        eq = 1.0
        peak = 1.0
        maxdd = 0.0
        for d in days:
            eq *= 1.0 + daily[d]
            peak = max(peak, eq)
            maxdd = min(maxdd, eq / peak - 1.0)
        total = eq - 1.0
        # days are day-indices (ts // DAY_MS), so the span is already in days.
        years = max((days[-1] - days[0]) / ANN_DAYS, 1e-9)
        mar = (total / years) / abs(maxdd) if abs(maxdd) > 1e-12 else 0.0
        return {"total": total, "max_dd": maxdd, "mar": mar}

    results: dict[str, dict[str, float]] = {}
    for pol in policies:
        daily: dict[int, float] = {}
        for (tr, path) in prepared:
            entry = float(tr["entry_price"])
            nw = float(tr["notional_weight"])
            cost = float(tr.get("cost_return", 0.0) or 0.0)
            fund = float(tr.get("funding_return", 0.0) or 0.0)
            if pol["kind"] == "flat":
                T = float(pol["T"])
            else:  # vol_norm
                rv = float(tr.get("rv168") or median_rv)
                ratio = (rv / median_rv) if median_rv > 1e-12 else 1.0
                T = 0.10 * (ratio ** float(pol["p"]))
                T = min(0.20, max(0.05, T))
            raw, exit_h = simulate_tp(path, entry, T, int(pol["H"]))
            exit_day = (int(tr["entry_ts_ms"]) + exit_h * HOUR_MS) // DAY_MS
            daily[exit_day] = daily.get(exit_day, 0.0) + raw * nw + cost + fund
        results[pol["name"]] = mar_proxy(daily)
    ctrl = results.get(control_name, {"mar": 0.0})
    for name, m in results.items():
        out["policies"][name] = {**m, "mar_delta_vs_control": m["mar"] - ctrl["mar"]}
    return out


POLICIES = [
    {"name": "control_10", "kind": "flat", "T": 0.10, "H": 24},
    {"name": "flat_12", "kind": "flat", "T": 0.12, "H": 24},   # validation vs lifecycle
    {"name": "flat_15", "kind": "flat", "T": 0.15, "H": 24},   # validation vs lifecycle
    {"name": "vol_norm_p05", "kind": "vol_norm", "p": 0.5, "H": 24},
    {"name": "vol_norm_p10", "kind": "vol_norm", "p": 1.0, "H": 24},
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ab-root", required=True)
    parser.add_argument("--almanac-root", required=True, help="feature almanac with path_rv_168h per (component,symbol,signal_ts)")
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--shared-root", default=str(Path.home() / "SHARED_DATA"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    roots = {"bybit": Path(args.shared_root) / "bybit_full_pit", "binance": Path(args.shared_root) / "binance_full_pit"}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_screen", "claimed_venue_scope": "both_venue_no_order_screen", "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        trades = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv").filter(pl.col("side") == "short")
        tape = pl.read_parquet(Path(args.almanac_root) / f"feature_tape_{venue}.parquet")
        rv = tape.select(["component", "symbol", "signal_ts_ms", "path_rv_168h"]).rename({"signal_ts_ms": "entry_signal_ts_ms"})
        joined = trades.join(rv, on=["component", "symbol", "entry_signal_ts_ms"], how="left")
        median_rv = float(joined["path_rv_168h"].drop_nulls().median() or 1.0)
        rows = []
        for r in joined.iter_rows(named=True):
            rows.append({"symbol": r["symbol"], "entry_ts_ms": r["entry_ts_ms"], "entry_price": r["entry_price"],
                         "exit_price": r["exit_price"], "notional_weight": r["notional_weight"],
                         "cost_return": r["cost_return"], "funding_return": r["funding_return"], "rv168": r.get("path_rv_168h")})
        fetch = _make_path_fetcher(roots[venue] / "klines_1h")
        summary["venues"][venue] = evaluate(rows, fetch, POLICIES, median_rv=median_rv)
    (out / "f2b_vol_tp_screen.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = ["# Continuous V2 Exit-Alpha 2b — vol-scaled TP screen (DD-aware proxy, both-venue)", "",
             "SCREEN ONLY (no rebalance); validated vs the flat-TP lifecycle. Pre-reg: "
             "`docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-construction.md`", ""]
    for v, r in summary["venues"].items():
        lines += [f"## {v} (n={r['n_with_path']}, median_rv168={r['median_rv168']:.4f})", "",
                  "| policy | proxy MAR | max DD | total | MARΔ vs control |", "| --- | ---: | ---: | ---: | ---: |"]
        for name, m in r["policies"].items():
            lines.append(f"| {name} | {m['mar']:.3f} | {m['max_dd']:+.4f} | {m['total']:+.4f} | {m['mar_delta_vs_control']:+.3f} |")
        lines.append("")
    (out / "f2b_vol_tp_screen_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary["venues"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
