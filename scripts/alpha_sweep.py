"""alpha_sweep — load a venue's continuous-fade panel+klines ONCE, then sweep a named experiment of
config-overrides in-memory (exit / entry / rebalance alpha). Same load-once pattern + MTM metric as
cb1_circuit_breaker_validate. Anti-overfit discipline: every verdict is read cross-venue (run both
roots) and promising cells get a fine grid (plateau-not-spike) before any adoption.

Experiments reuse the SAME rmom50 panel + klines (loaded once) and vary only the _run_trades walk, so a
whole experiment costs one klines read. (rmom_quantile / feature-weight experiments that change the
PANEL are NOT here — they need a panel rebuild; handled separately.)

Dispatch (one venue/process to bound memory):
  POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/alpha_sweep.py --root ~/SHARED_DATA/bybit_full_pit  --label bybit  --experiment EXP --out /tmp/as_EXP_bybit.json
  POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/alpha_sweep.py --root ~/SHARED_DATA/binance_full_pit --label binance --experiment EXP --out /tmp/as_EXP_binance.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields, replace
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

# Live-sleeve-matching base (same as cb1).
BASE = ContinuousEventConfig(
    start_date="2023-04-01", end_date="2026-05-28",
    side="short", decile=9, rmom_quantile=0.5, liq_turnover_min=500_000.0,
    entry_delay_hours=1, exit_mode="state", max_hold_hours=48,
    stop_loss_pct=0.25, stop_fill_mode="bar_extreme_capped", stop_slippage_cap_pct=0.10,
    gross_exposure=0.5, max_active=25,
    failed_fade_hours=6, failed_fade_loss_pct=0.04, failed_fade_min_mfe_pct=0.01,
    breakeven_arm_pct=0.10, age_days_min=30, use_funding=True,
)


SURGE_LOOKBACK_H = 168  # trailing window for the turnover-surge baseline (1 week)


def _experiment(name: str) -> list[tuple[str, dict]]:
    """Return [(label, overrides), ...] for a named experiment. 'off'/baseline first."""
    if name == "mfe":  # exit alpha — trailing profit-protection (arm at trigger, exit at retain*peak)
        out = [("base", {})]
        for trig in (0.05, 0.10, 0.15):
            for ret in (0.3, 0.5, 0.7):
                out.append((f"t{int(trig*100)}/r{int(ret*100)}",
                            {"mfe_giveback_trigger_pct": trig, "mfe_giveback_retain_pct": ret}))
        return out
    if name == "mfefine":  # fine grid around the t5/r30 hit (plateau-not-spike check)
        out = [("base", {})]
        for trig in (0.03, 0.05, 0.08):
            for ret in (0.2, 0.3, 0.4):
                out.append((f"t{int(trig*100)}/r{int(ret*100)}",
                            {"mfe_giveback_trigger_pct": trig, "mfe_giveback_retain_pct": ret}))
        return out
    if name == "liq":  # entry alpha — raise the liquidity gate off its $500k cliff
        return [("base", {})] + [(f"liq{int(v/1000)}k", {"liq_turnover_min": float(v)})
                                 for v in (500_000, 1_000_000, 2_000_000, 5_000_000)]
    if name == "turnsurge":  # entry alpha — require a real flow surge (signal turnover >= k x trailing median)
        return [("base", {})] + [(f"k{k}", {"turnover_surge_min": k}) for k in (1.25, 1.5, 2.0, 3.0, 5.0)]
    if name == "rmom":  # entry alpha — tighten the rmom-low gate (rebuilds the panel per quantile)
        return [(f"q{int(q*100)}", {"rmom_quantile": q}) for q in (0.50, 0.40, 0.33, 0.25)]
    if name == "stack":  # do the two candidate wins STACK? (entry rmom-tighten + exit mfe-trailing)
        mfe = {"mfe_giveback_trigger_pct": 0.05, "mfe_giveback_retain_pct": 0.30}
        return [
            ("base", {}),
            ("mfe", mfe),
            ("rmom33", {"rmom_quantile": 0.33}),
            ("rmom33+mfe", {"rmom_quantile": 0.33, **mfe}),
            ("rmom25+mfe", {"rmom_quantile": 0.25, **mfe}),
        ]
    if name == "maxhold":  # exit alpha — state-mode force-exit cap
        return [("base", {})] + [(f"mh{h}", {"max_hold_hours": h}) for h in (24, 36, 48, 72, 96, 168)]
    if name == "maxactive":  # rebalance alpha — concentration vs breadth (gross fixed 0.5)
        return [("base", {})] + [(f"ma{m}", {"max_active": m}) for m in (12, 18, 25, 35, 50)]
    if name == "sizing":  # rebalance alpha — signal/inverse-vol sizing knobs
        return [
            ("base", {}),
            ("invvol/c2", {"sizing_mode": "inverse_vol", "vol_weight_clamp": 2.0}),
            ("invvol/c3", {"sizing_mode": "inverse_vol", "vol_weight_clamp": 3.0}),
            ("sigwt/c2", {"sizing_mode": "signal", "vol_weight_clamp": 2.0}),
            ("sigwt/c3", {"sizing_mode": "signal", "vol_weight_clamp": 3.0}),
        ]
    if name == "turnsurge":  # entry alpha — require a real flow surge (turnover >= k x trailing median)
        return [("base", {})] + [(f"k{k}/w{w}", {"turnover_surge_min": k, "turnover_surge_lookback_h": w})
                                 for w in (72, 168) for k in (1.5, 2.0, 3.0)]
    if name == "rotate":  # rebalance alpha — replace the weakest hold with a stronger fresh candidate
        return [("base", {})] + [(f"rot/m{m}", {"rotate_min_composite_edge": m})
                                 for m in (0.02, 0.05, 0.10, 0.20)]
    raise SystemExit(f"unknown experiment: {name}")


def _attach_surge_ratio(entries: pl.DataFrame, klines: pl.DataFrame, lookback_h: int) -> pl.DataFrame:
    """surge_ratio per entry = signal-bar turnover / trailing-median turnover (causal: the median uses
    the lookback_h bars BEFORE the signal bar via shift(1)). Re-injects the liquidity-migration EVENT:
    a high ratio = the pop came with a genuine flow surge vs the name's own recent norm."""
    tov = (
        klines.select("ts_ms", "symbol", "turnover_quote").sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("turnover_quote").rolling_median(window_size=lookback_h, min_samples=24)
            .shift(1).over("symbol").alias("_turn_med"))
        .select("ts_ms", "symbol", "_turn_med")
    )
    e = entries.join(tov, on=["symbol", "ts_ms"], how="left")
    return e.with_columns(
        pl.when(pl.col("_turn_med") > 0)
        .then(pl.col("turnover_quote") / pl.col("_turn_med"))
        .otherwise(None).alias("surge_ratio")
    )


def _metrics(trades: pl.DataFrame, klines: pl.DataFrame) -> dict:
    add = _additive_summary(trades, BASE)
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
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    root = Path(args.root).expanduser()

    print(f"[{args.label}/{args.experiment}] load panel + entries ...", flush=True)
    panel = build_continuous_panel(root, BASE)
    entries = _fresh_entries(panel, BASE)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    start_ms, end_ms = _date_str_to_ms(BASE.start_date), _date_str_to_ms(BASE.end_date)
    pad = (BASE.max_hold_hours + BASE.entry_delay_hours + 4) * MS_PER_HOUR
    print(f"[{args.label}] read klines ...", flush=True)
    klines = _read_window(root, kname, start_ms=start_ms, end_ms=end_ms + pad,
                          columns=["ts_ms", "symbol", "open", "high", "low", "close", "turnover_quote"])
    if BASE.exclude_symbols:
        klines = klines.filter(~pl.col("symbol").is_in(list(BASE.exclude_symbols)))
    symbol_bars = _indexed_price_bars_by_symbol(klines)
    fname = _autodetect_dataset_names(root)["funding_dataset"]
    funding_lookup = _funding_lookup(_read_window(root, fname, start_ms=start_ms - 240 * MS_PER_HOUR, end_ms=end_ms + pad))
    market_daily = _market_daily_returns(klines)
    # turnover panel for the turnsurge experiment (signal-bar turnover per (symbol,ts)); cheap, reused
    print(f"[{args.label}] loaded {len(symbol_bars)} symbols; sweeping {args.experiment} ...", flush=True)

    # one or more experiments (comma-sep) share the single klines load; dedup the shared base.
    plan: list[tuple[str, dict]] = []
    seen_base = False
    for exp in args.experiment.split(","):
        for label, ov in _experiment(exp.strip()):
            if label == "base":
                if seen_base:
                    continue
                seen_base = True
                plan.append(("base", {}))
            else:
                plan.append((f"{exp.strip()}:{label}", ov))

    need_surge = any("turnover_surge_min" in ov for _, ov in plan)
    surge_entries = _attach_surge_ratio(entries, klines, SURGE_LOOKBACK_H) if need_surge else None

    cfg_fields = {f.name for f in fields(ContinuousEventConfig)}
    base_mar = None
    rows = []
    for label, ov in plan:
        cfg = replace(BASE, **{k: v for k, v in ov.items() if k in cfg_fields})  # drop harness-only keys
        # resolve the entry set for this config (most reuse the shared `entries`)
        if "rmom_quantile" in ov:
            ent = _fresh_entries(build_continuous_panel(root, cfg), cfg)      # rebuild panel for this rmom gate
        elif "liq_turnover_min" in ov:
            ent = _fresh_entries(panel, cfg)                                  # re-filter by the new liq gate
        elif "turnover_surge_min" in ov and surge_entries is not None:
            ent = surge_entries.filter(pl.col("surge_ratio") >= ov["turnover_surge_min"])  # flow-surge gate
        else:
            ent = entries
        trades, skips = _run_trades(ent, symbol_bars, funding_lookup, cfg, market_daily)
        m = _metrics(trades, klines)
        m["n_entries"] = int(ent.height)
        m.update({"cfg": label, "overrides": ov, "skips": skips})
        if label == "base":
            base_mar = m["mtm_mar"]
        rows.append(m)
        beats = ""
        if base_mar is not None and m["mtm_mar"] is not None:
            beats = "  >base" if m["mtm_mar"] > base_mar else ""
        print(
            f"  {label:12} trades={m['n_trades']:6d} ret={m['total_return']*100:7.1f}% "
            f"MTM-MAR={('%.2f'%m['mtm_mar']) if m['mtm_mar'] is not None else 'NA':>6} "
            f"MTM-DD={abs(m['mtm_maxdd'])*100:5.1f}% wd={m['mtm_worst_day']*100:6.2f}%"
            f"{beats} (e={m['early_return']*100:.0f}/r={m['recent_return']*100:.0f})",
            flush=True,
        )
    if args.out:
        Path(args.out).write_text(json.dumps({"label": args.label, "experiment": args.experiment, "rows": rows}, indent=2, default=str))
    print(f"[{args.label}/{args.experiment}] DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
