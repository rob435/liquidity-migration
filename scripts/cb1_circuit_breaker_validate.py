"""cb1 — validate the continuous-fade ENTRY CIRCUIT BREAKER in the execution-grade engine.

Mirrors the live sleeve's config (state exit, max_hold 48, ff6, breakeven@10%, age 30d, 25% server
stop, gross 0.5 / max_active 25, liq>=500k, rmom-low, +1h honest entry) and ablates the portfolio
circuit breaker (pause new entries when >= N net-negative covers close within the trailing window)
across (window_hours x threshold). Loads each venue's klines/panel/funding ONCE, then sweeps configs
in-memory. Reports the MTM-drawdown-aware metrics (the inheritance-doc methodology: MTM-MAR/DD/worst-day)
so "beneficial" means: improves MTM-MAR and/or cuts MTM-DD without killing return, on BOTH venues.

Dispatch (one venue per process to bound memory on the 17GB box):
  POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/cb1_circuit_breaker_validate.py --root ~/SHARED_DATA/bybit_full_pit --label bybit
  POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/cb1_circuit_breaker_validate.py --root ~/SHARED_DATA/binance_full_pit --label binance
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from itertools import product
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import polars as pl  # noqa: E402

from liquidity_migration._common import MS_PER_HOUR  # noqa: E402
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _additive_summary,
    _daily_pnl_metrics,
    _fresh_entries,
    _market_daily_returns,
    _portfolio_mtm_equity,
    _run_trades,
    build_continuous_panel,
)
from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)
from liquidity_migration.trade_lifecycle import _funding_lookup  # noqa: E402
from liquidity_migration.volume_events import _indexed_price_bars_by_symbol  # noqa: E402

# Live-sleeve-matching base (continuous_demo.ContinuousDemoCycleConfig -> engine equivalents).
BASE = ContinuousEventConfig(
    start_date="2023-04-01", end_date="2026-05-28",
    side="short", decile=9, rmom_quantile=0.5, liq_turnover_min=500_000.0,
    entry_delay_hours=1, exit_mode="state", max_hold_hours=48,
    stop_loss_pct=0.25, stop_fill_mode="bar_extreme_capped", stop_slippage_cap_pct=0.10,
    gross_exposure=0.5, max_active=25,
    failed_fade_hours=6, failed_fade_loss_pct=0.04, failed_fade_min_mfe_pct=0.01,
    breakeven_arm_pct=0.10, age_days_min=30, use_funding=True,
)
WINDOWS_H = [12, 24, 48]
THRESHOLDS = [3, 5, 8]


def _int_list(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def _metrics(trades: pl.DataFrame, klines: pl.DataFrame, cfg: ContinuousEventConfig, skips: dict) -> dict:
    add = _additive_summary(trades, cfg)
    mtm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
    return {
        "n_trades": int(trades.height),
        "total_return": add.get("total_return", 0.0),
        "early_return": add.get("early_return", 0.0),
        "recent_return": add.get("recent_return", 0.0),
        "mtm_mar": mtm.get("mar"),
        "mtm_maxdd": mtm.get("max_drawdown", 0.0),
        "mtm_sharpe": mtm.get("sharpe_like", 0.0),
        "mtm_worst_day": mtm.get("worst_day_return", 0.0),
        "skipped_breaker": int(skips.get("skipped_breaker", 0)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", default=None, help="write the result rows as JSON here")
    ap.add_argument("--windows", type=_int_list, default=None, help="comma list of window hours (overrides default grid)")
    ap.add_argument("--thresholds", type=_int_list, default=None, help="comma list of adverse-exit thresholds")
    args = ap.parse_args()
    windows = args.windows or WINDOWS_H
    thresholds = args.thresholds or THRESHOLDS
    root = Path(args.root).expanduser()

    print(f"[{args.label}] build/load panel ...", flush=True)
    panel = build_continuous_panel(root, BASE)
    entries = _fresh_entries(panel, BASE)
    print(f"[{args.label}] {entries.height} fresh entries", flush=True)

    kname = _autodetect_dataset_names(root)["klines_dataset"]
    start_ms, end_ms = _date_str_to_ms(BASE.start_date), _date_str_to_ms(BASE.end_date)
    pad = (BASE.max_hold_hours + BASE.entry_delay_hours + 4) * MS_PER_HOUR
    print(f"[{args.label}] read klines (this is the heavy step) ...", flush=True)
    klines = _read_window(root, kname, start_ms=start_ms, end_ms=end_ms + pad,
                          columns=["ts_ms", "symbol", "open", "high", "low", "close"])
    if BASE.exclude_symbols:
        klines = klines.filter(~pl.col("symbol").is_in(list(BASE.exclude_symbols)))
    symbol_bars = _indexed_price_bars_by_symbol(klines)
    fname = _autodetect_dataset_names(root)["funding_dataset"]
    funding = _read_window(root, fname, start_ms=start_ms - 10 * 24 * MS_PER_HOUR, end_ms=end_ms + pad)
    funding_lookup = _funding_lookup(funding)
    market_daily = _market_daily_returns(klines)
    print(f"[{args.label}] loaded {len(symbol_bars)} symbols; sweeping ...", flush=True)

    configs = [("off", 0, 0)] + [(f"w{w}/n{n}", n, w) for w, n in product(windows, thresholds)]
    rows = []
    for name, thr, win in configs:
        cfg = replace(BASE, entry_pause_after_adverse_exits=thr, entry_pause_window_hours=win or 24)
        trades, skips = _run_trades(entries, symbol_bars, funding_lookup, cfg, market_daily)
        m = _metrics(trades, klines, cfg, skips)
        m.update({"cfg": name, "threshold": thr, "window_h": win})
        rows.append(m)
        print(
            f"  {name:9} trades={m['n_trades']:6d} ret={m['total_return']*100:7.1f}% "
            f"MTM-MAR={('%.2f'%m['mtm_mar']) if m['mtm_mar'] is not None else 'NA':>6} "
            f"MTM-DD={abs(m['mtm_maxdd'])*100:5.1f}% wd={m['mtm_worst_day']*100:6.2f}% "
            f"paused={m['skipped_breaker']:4d} (early={m['early_return']*100:.0f}/recent={m['recent_return']*100:.0f})",
            flush=True,
        )
    out = {"label": args.label, "base": rows[0], "rows": rows}
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    print(f"[{args.label}] DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
