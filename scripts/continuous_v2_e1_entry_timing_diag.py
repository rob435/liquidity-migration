#!/usr/bin/env python3
"""E1 Bybit-only exploratory intrabar entry-timing diagnostic.

Pre-registration:
docs/preregistration/2026-06-19-continuous-v2-e1-entry-timing-construction.md

For each Bybit V2_CONTROL short trade with 5m data in its entry hour, simulate a
causal "sell into strength" stop-short at L = entry_price*(1+delta): filled if any
entry-hour 5m high reaches L, else the trade is missed (foregone). Measures the
book-level net effect (in notional-weighted contribution units) of the entry-price
improvement net of missed fills, plus an adverse-selection check and a random-5m-bar
null. Single-venue exploratory; NOT a both-venue candidate; not real-money evidence.

Limitations (stated): this is a per-trade entry-improvement diagnostic, not a full
lifecycle re-sim. It holds the exit fixed (take_profit exits are entry-relative ->
raw return unchanged; time exits recompute (L - exit_price)/L) and does NOT re-trigger
the TP/24h timer on the new entry path, nor re-apply the daily vol-target rebalance.
It is the gate that decides whether a full lifecycle E1 build + a Binance sub-hourly
backfill are worth doing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from pathlib import Path
from typing import Any, Callable

import polars as pl

HOUR_MS = 3_600_000
STOP_DELTAS = (0.0025, 0.005, 0.01)
TP_REASONS = ("take_profit", "profit", "tp")  # entry-relative exit: entry timing cannot change its raw return


def _raw_short_return(entry: float, exit_: float) -> float:
    return (entry - exit_) / entry if entry > 0 else 0.0


def _is_tp(exit_reason: Any) -> bool:
    s = str(exit_reason).lower()
    return any(tok in s for tok in TP_REASONS)


def evaluate_entry_timing(
    trades: list[dict[str, Any]],
    fetch_bars: Callable[[str, int], pl.DataFrame | None],
    *,
    deltas: tuple[float, ...] = STOP_DELTAS,
    seed: int = 0,
) -> dict[str, Any]:
    """Core diagnostic. ``fetch_bars(symbol, entry_ts_ms)`` returns the entry-hour 5m
    bars (columns ``high`` and ``close``) or None when 5m data is unavailable."""
    rng = random.Random(seed)
    prepared: list[dict[str, Any]] = []
    for tr in trades:
        bars = fetch_bars(str(tr["symbol"]), int(tr["entry_ts_ms"]))
        if bars is None or bars.is_empty() or "high" not in bars.columns:
            continue
        prepared.append(
            {
                "entry_price": float(tr["entry_price"]),
                "exit_price": float(tr["exit_price"]),
                "net_return": float(tr["net_return"]),
                "cost_return": float(tr.get("cost_return", 0.0) or 0.0),
                "funding_return": float(tr.get("funding_return", 0.0) or 0.0),
                "notional_weight": float(tr["notional_weight"]),
                "is_tp": _is_tp(tr.get("exit_reason")),
                "hi": float(bars["high"].max()),
                "closes": [float(x) for x in bars["close"].to_list() if x is not None],
            }
        )
    n = len(prepared)
    control_total = sum(r["net_return"] for r in prepared)
    out: dict[str, Any] = {
        "n_trades_input": len(trades),
        "n_with_5m": n,
        "control_total_net_contrib": control_total,
        "deltas": {},
    }
    if n == 0:
        return out
    for delta in deltas:
        filled_delta_sum = 0.0
        missed_foregone = 0.0
        n_filled = n_missed = 0
        filled_ctrl: list[float] = []
        missed_ctrl: list[float] = []
        null_total = 0.0
        for r in prepared:
            ep, xp, nw = r["entry_price"], r["exit_price"], r["notional_weight"]
            cost, fund, netold = r["cost_return"], r["funding_return"], r["net_return"]
            level = ep * (1.0 + delta)
            if r["hi"] >= level:
                n_filled += 1
                filled_ctrl.append(netold)
                raw_new = _raw_short_return(ep, xp) if r["is_tp"] else _raw_short_return(level, xp)
                net_new = raw_new * nw + cost + fund
                filled_delta_sum += net_new - netold
            else:
                n_missed += 1
                missed_ctrl.append(netold)
                missed_foregone += netold
            # random-5m-bar null (E5-style randomized timing within the same hour; always fills)
            ref = float(rng.choice(r["closes"])) if r["closes"] else ep
            raw_null = _raw_short_return(ep, xp) if r["is_tp"] else _raw_short_return(ref, xp)
            null_total += raw_null * nw + cost + fund
        net_effect = filled_delta_sum - missed_foregone
        out["deltas"][f"{delta:.4f}"] = {
            "fill_rate": n_filled / n,
            "n_filled": n_filled,
            "n_missed": n_missed,
            "filled_entry_improve_contrib": filled_delta_sum,
            "missed_foregone_contrib": missed_foregone,
            "net_effect_contrib": net_effect,
            "net_effect_pct_of_control": (net_effect / control_total) if abs(control_total) > 1e-12 else None,
            "adverse_selection_missed_minus_filled_meannet": (
                (sum(missed_ctrl) / len(missed_ctrl) if missed_ctrl else 0.0)
                - (sum(filled_ctrl) / len(filled_ctrl) if filled_ctrl else 0.0)
            ),
            "null_random_bar_net_effect_contrib": null_total - control_total,
        }
    return out


def _date_str(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def _make_fetcher(klines5m_root: Path) -> Callable[[str, int], pl.DataFrame | None]:
    cache: dict[tuple[str, str], pl.DataFrame | None] = {}

    def fetch(symbol: str, entry_ts_ms: int) -> pl.DataFrame | None:
        date = _date_str(entry_ts_ms)
        key = (symbol, date)
        if key not in cache:
            part = klines5m_root / f"date={date}" / f"symbol={symbol}"
            try:
                cache[key] = pl.read_parquet(part).select("ts_ms", "high", "close") if part.exists() else None
            except Exception:  # noqa: BLE001 - a missing/corrupt partition just excludes the trade
                cache[key] = None
        day = cache[key]
        if day is None:
            return None
        return day.filter((pl.col("ts_ms") >= entry_ts_ms) & (pl.col("ts_ms") < entry_ts_ms + HOUR_MS))

    return fetch


def _write_report(path: Path, result: dict[str, Any], meta: dict[str, Any]) -> None:
    lines = [
        "# Continuous V2 E1 Intrabar Entry-Timing Diagnostic (Bybit-only exploratory)",
        "",
        "claimed_venue_scope: `bybit_only_execution_exploratory`. Run label `exploratory`. "
        "Per-trade diagnostic (no lifecycle re-sim / no rebalance). Not a candidate; not real-money evidence.",
        "",
        f"- Control trades: `{meta['trades_path']}`",
        f"- klines_5m root: `{meta['klines5m_root']}`",
        f"- Trades with 5m data: {result['n_with_5m']} / {result['n_trades_input']}",
        f"- Control total net contribution (5m universe): {result['control_total_net_contrib']:.6f}",
        "",
        "| delta | fill rate | net effect (contrib) | % of control | missed foregone | adverse-sel (missed−filled mean net) | null random-bar net |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for d, row in result["deltas"].items():
        pct = row["net_effect_pct_of_control"]
        lines.append(
            f"| {d} | {row['fill_rate']:.1%} | {row['net_effect_contrib']:+.6f} | "
            f"{'n/a' if pct is None else f'{pct:+.2%}'} | {row['missed_foregone_contrib']:+.6f} | "
            f"{row['adverse_selection_missed_minus_filled_meannet']:+.6e} | "
            f"{row['null_random_bar_net_effect_contrib']:+.6f} |"
        )
    lines.append("")
    lines.append(
        "Read: net effect ≤ 0, or not beating the random-bar null, or positive adverse-selection "
        "(missed trades were the better shorts) closes E1. See the construction receipt for falsifiers."
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", required=True, help="V2_CONTROL bybit trades.csv")
    parser.add_argument("--klines5m-root", required=True)
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="limit trades for a smoke run (0 = all)")
    args = parser.parse_args()

    trades_df = pl.read_csv(args.trades).filter(pl.col("side") == "short")
    if args.limit:
        trades_df = trades_df.head(args.limit)
    trades = trades_df.to_dicts()
    fetch = _make_fetcher(Path(args.klines5m_root).expanduser())
    result = evaluate_entry_timing(trades, fetch, seed=args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"trades_path": args.trades, "klines5m_root": args.klines5m_root, "seed": args.seed}
    result["meta"] = meta
    result["run_label"] = "exploratory"
    result["claimed_venue_scope"] = "bybit_only_execution_exploratory"
    (out / "e1_entry_timing.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    _write_report(out / "e1_entry_timing_report.md", result, meta)
    print(json.dumps({"n_with_5m": result["n_with_5m"], "deltas": result["deltas"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
