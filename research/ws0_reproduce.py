"""WS-0c — reproduce the reversion_alpha harness numbers, and re-baseline after
the entry-lag fix.

Usage:  python -m research.ws0_reproduce <window> <mode>
  window: is_train | is_valid | oos_bybit | oos_binance | all
  mode:   legacy  -> entry_delay_hours=24, reproduces docs/reversion_alpha_report.md
                     (the report's backtests ran with the +23h entry-lag bug)
          fixed   -> entry_delay_hours=1, the corrected harness (new baseline)

`legacy` is checked against the report's table (±5 pp / ±5% tolerance, pre-
registered in research/RESEARCH_LOG.md). `fixed` has no prior expectation — it
is a fresh measurement.
"""
from __future__ import annotations

import sys
import time

from liquidity_migration.reversion_alpha import ReversionConfig, run_reversion_backtest

# window -> (data_root, start, end, expected dict from docs/reversion_alpha_report.md)
WINDOWS = {
    "is_train": (
        "~/SHARED_DATA/bybit_fullpit_1h", "2023-09-01", "2024-09-01",
        {"trades": 661, "total_return": -0.285, "max_drawdown": -0.415,
         "sharpe": -0.76, "win_rate": 0.504},
    ),
    "is_valid": (
        "~/SHARED_DATA/bybit_fullpit_1h", "2024-09-01", "2026-05-18",
        {"trades": 1154, "total_return": 1.154, "max_drawdown": -0.254,
         "sharpe": 1.37, "win_rate": 0.511},
    ),
    "oos_bybit": (
        "~/SHARED_DATA/bybit_oos_pre2023", "2022-04-01", "2023-05-03",
        {"trades": 681, "total_return": 0.337, "max_drawdown": -0.412,
         "sharpe": 0.76, "win_rate": 0.526},
    ),
    "oos_binance": (
        "~/SHARED_DATA/binance_oos_pit", "2020-09-01", "2023-05-01",
        {"trades": 1606, "total_return": -0.775, "max_drawdown": -0.870,
         "sharpe": -0.54, "win_rate": 0.447},
    ),
}


def run_window(name: str, mode: str) -> dict:
    root, start, end, expected = WINDOWS[name]
    delay = 24 if mode == "legacy" else 1
    cfg = ReversionConfig(start=start, end=end, entry_delay_hours=delay)
    t0 = time.time()
    result = run_reversion_backtest(root, cfg)
    elapsed = time.time() - t0
    m = result["metrics"]

    print(f"\n=== {name}  ({start} -> {end})  mode={mode} entry_delay={delay}h  [{elapsed:.0f}s] ===")
    print(f"  scored_rows={result['scored_rows']}  book_rows={result['book_rows']}")
    if mode == "legacy":
        ret_ok = abs(m["total_return"] - expected["total_return"]) <= 0.05
        trd_ok = abs(m["trades"] - expected["trades"]) <= max(1, 0.05 * expected["trades"])
        verdict = "PASS" if (ret_ok and trd_ok) else "FAIL"
        print(f"  {'metric':<16}{'reproduced':>14}{'reported':>14}")
        for k in ("trades", "total_return", "max_drawdown", "sharpe", "win_rate"):
            print(f"  {k:<16}{m[k]:>14.4f}{expected[k]:>14.4f}")
        print(f"  return_ok={ret_ok}  trades_ok={trd_ok}  -> {verdict}")
    else:
        verdict = "MEASURED"
        for k in ("trades", "total_return", "max_drawdown", "sharpe", "win_rate"):
            print(f"  {k:<16}{m[k]:>14.4f}")
    return {"window": name, "mode": mode, "metrics": m, "verdict": verdict}


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    mode = sys.argv[2] if len(sys.argv) > 2 else "legacy"
    names = list(WINDOWS) if which == "all" else [which]
    results = []
    for n in names:
        try:
            results.append(run_window(n, mode))
        except Exception as exc:  # noqa: BLE001 — surface, do not swallow
            print(f"\n=== {n} FAILED TO RUN ===\n  {type(exc).__name__}: {exc}")
            results.append({"window": n, "verdict": "ERROR"})
    print("\n--- summary ---")
    for r in results:
        ret = r.get("metrics", {}).get("total_return")
        ret_s = f"{ret:+.1%}" if ret is not None else "  --"
        print(f"  {r['window']:<14} {mode:<7} {r['verdict']:<9} {ret_s}")


if __name__ == "__main__":
    main()
