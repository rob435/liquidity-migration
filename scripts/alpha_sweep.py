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
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR, calendar_shift  # noqa: E402
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
from liquidity_migration.trade_lifecycle import _indexed_price_bars_by_symbol  # noqa: E402

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
    if name == "delay":  # live-fidelity — live sleeve enters at 0h; only +1h is validated
        return [("base", {})] + [(f"d{d}", {"entry_delay_hours": d}) for d in (0, 1, 2, 3, 6)]
    if name == "ff6":  # exit — sub-sweep the adopted failed-fade (hours x loss%)
        out = [("base", {})]
        for h in (3, 6, 9, 12):
            for loss in (0.03, 0.04, 0.06):
                out.append((f"h{h}/l{int(loss*100)}", {"failed_fade_hours": h, "failed_fade_loss_pct": loss}))
        return out
    if name == "bkeven":  # exit — sweep the breakeven arm (currently 0.10)
        return [("base", {})] + [(f"a{int(a*100)}", {"breakeven_arm_pct": a}) for a in (0.05, 0.08, 0.10, 0.15, 0.20)]
    if name == "rmom":  # entry alpha — tighten the rmom-low gate (rebuilds the panel per quantile)
        return [(f"q{int(q*100)}", {"rmom_quantile": q}) for q in (0.50, 0.40, 0.33, 0.25)]
    if name == "best":  # combined validated wins at the validated d1: rmom33 + ff6 h9/l6
        ff = {"failed_fade_hours": 9, "failed_fade_loss_pct": 0.06}
        return [("base", {}), ("rmom33", {"rmom_quantile": 0.33}), ("ff96", ff),
                ("rmom33+ff96", {"rmom_quantile": 0.33, **ff})]
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
    raise SystemExit(f"unknown experiment: {name}")


def _max_swept_max_hold_hours(experiment_arg: str) -> int:
    """Largest ``max_hold_hours`` any cell in the requested experiment(s) will run.

    The forward klines pad must cover the LONGEST hold actually swept, not just
    BASE.max_hold_hours — otherwise the longest-hold cells in the `maxhold` sweep
    (up to 168h) read against a window padded for only 48h, so their tail trades
    are clipped to a shorter effective hold and exit early on a `data_end` mark,
    biasing the very hold-horizon comparison the experiment exists to make
    (alpha-scripts-4). Special-branch experiments never override max_hold_hours,
    so they fall through to BASE.max_hold_hours. Unknown experiments raise inside
    _experiment, which is the existing behavior; we swallow that here and let the
    main dispatch surface it.
    """
    best = int(BASE.max_hold_hours)
    for exp in experiment_arg.split(","):
        try:
            cells = _experiment(exp.strip())
        except SystemExit:
            continue  # special-branch / unknown — handled by the main dispatch
        for _label, ov in cells:
            mh = ov.get("max_hold_hours")
            if mh is not None:
                best = max(best, int(mh))
    return best


def _max_swept_entry_delay_hours(experiment_arg: str) -> int:
    """Largest ``entry_delay_hours`` any cell in the requested experiment(s) will run.
    The forward klines pad must cover the longest (delay + hold) actually swept, else
    the `delay` sweep's long-delay cells get more tail-truncation than short-delay
    cells (audit-iter3 backlog scripts). Mirrors _max_swept_max_hold_hours."""
    best = int(BASE.entry_delay_hours)
    for exp in experiment_arg.split(","):
        try:
            cells = _experiment(exp.strip())
        except SystemExit:
            continue  # special-branch / unknown — handled by the main dispatch
        for _label, ov in cells:
            ed = ov.get("entry_delay_hours")
            if ed is not None:
                best = max(best, int(ed))
    return best


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


def _apply_vol_target(mtm_equity: pl.DataFrame, *, target_vol_annual: float, lookback: int, floor: float) -> pl.DataFrame:
    """De-risk-only volatility-target overlay on the daily MTM book return: scale day t's return by
    s_t = clip(target_daily / trailing_vol_{t-1}, floor, 1.0) — NEVER lever up (cap 1.0). Causal: the
    trailing vol uses the lookback days through t-1 (rolling_std + shift(1)). Approximate screen of the
    long sleeve's `div` risk overlay (a faithful version would scale notional_weight in-engine)."""
    if mtm_equity.is_empty():
        return mtm_equity
    target_daily = target_vol_annual / (365.0 ** 0.5)
    df = mtm_equity.select("ts_ms", "basket_return").with_columns(
        pl.col("basket_return").rolling_std(window_size=lookback, min_samples=max(5, lookback // 2))
        .shift(1).alias("_vol"))
    df = df.with_columns(
        pl.when(pl.col("_vol").is_null() | (pl.col("_vol") <= 0)).then(pl.lit(1.0))
        .otherwise((pl.lit(target_daily) / pl.col("_vol")).clip(floor, 1.0)).alias("_s"))
    df = df.with_columns((pl.col("basket_return") * pl.col("_s")).alias("basket_return"))
    return df.select("ts_ms", "basket_return").with_columns(
        (1.0 + pl.col("basket_return").cum_sum()).alias("equity")
    ).with_columns((pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown")).select(
        "ts_ms", "equity", "drawdown", "basket_return")


def _portfolio_mtm_equity_hourly(trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    """HOURLY-marked portfolio MTM equity (the audit's intraday-DD metric; all other sweeps used DAILY
    marks, which under-measure the squeeze tail). Each open position is marked to every HOURLY close it's
    held through; concurrent correlated moves aggregate into the hourly book path → the real intraday
    drawdown. Memory-efficient (explode held-hours + join, no giant dict). Cost on the entry hour, funding
    on the exit hour, exit hour marked at exit_price; telescopes to the trade's realized gross."""
    if trades.is_empty():
        return pl.DataFrame({"ts_ms": pl.Series([], dtype=pl.Int64), "equity": pl.Series([], dtype=pl.Float64),
                             "drawdown": pl.Series([], dtype=pl.Float64), "basket_return": pl.Series([], dtype=pl.Float64)})
    H = MS_PER_HOUR
    t = (trades.select("symbol", "side", "entry_ts_ms", "exit_ts_ms", "entry_price", "exit_price",
                       "notional_weight", "cost_return", "funding_return")
         .filter(pl.col("entry_price") > 0).with_row_index("tid")
         .with_columns(((pl.col("entry_ts_ms") // H) * H).alias("e_h"), ((pl.col("exit_ts_ms") // H) * H).alias("x_h")))
    t = t.with_columns(pl.int_ranges(pl.col("e_h"), pl.col("x_h") + H, H).alias("hour"))
    e = t.explode("hour")
    kc = klines.select("symbol", pl.col("ts_ms").alias("hour"), "close").unique(["symbol", "hour"], keep="last")
    e = e.join(kc, on=["symbol", "hour"], how="left").sort(["tid", "hour"])
    e = e.with_columns(pl.when(pl.col("hour") == pl.col("x_h")).then(pl.col("exit_price"))
                       .otherwise(pl.col("close")).alias("mark"))
    e = e.with_columns(pl.col("mark").forward_fill().over("tid"))
    e = e.with_columns(pl.col("mark").shift(1).over("tid").fill_null(pl.col("entry_price")).alias("prev"))
    sign = pl.when(pl.col("side") == "short").then(-1.0).otherwise(1.0)
    e = e.with_columns(
        (pl.col("notional_weight") * sign * (pl.col("mark") - pl.col("prev")) / pl.col("entry_price")
         + pl.when(pl.col("hour") == pl.col("e_h")).then(pl.col("cost_return")).otherwise(0.0)
         + pl.when(pl.col("hour") == pl.col("x_h")).then(pl.col("funding_return")).otherwise(0.0)).alias("incr"))
    book = (e.group_by("hour").agg(pl.col("incr").sum().alias("basket_return")).sort("hour")
            .rename({"hour": "ts_ms"}))
    return book.with_columns((1.0 + pl.col("basket_return").cum_sum()).alias("equity")).with_columns(
        (pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown")).select("ts_ms", "equity", "drawdown", "basket_return")


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
    # Pad the forward klines window for the LONGEST max_hold actually swept in
    # this run (not just BASE.max_hold_hours) so the maxhold sweep's long-hold
    # cells aren't tail-truncated against a too-short window (alpha-scripts-4).
    max_hold = _max_swept_max_hold_hours(args.experiment)
    pad = (max_hold + _max_swept_entry_delay_hours(args.experiment) + 4) * MS_PER_HOUR
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

    # --- REGIME-VALIDATE: is the trend-regime win a PLATEAU (robust to the trend window) or a tuned spike?
    #     Sweep trend windows {3,7,14}d x {strong/weak}; bootstrap-p5 the mkt_strong config. Cross-venue. ---
    if args.experiment == "regimevalidate":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        ent = _fresh_entries(panel, cfg33)
        mbase = (klines.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
                 .group_by(["symbol", "day"]).agg(pl.col("close").last().alias("c")).sort(["symbol", "day"])
                 .with_columns((pl.col("c") / calendar_shift(pl.col("c"), 1, time_col="day") - 1.0).alias("r"))
                 .group_by("day").agg(pl.col("r").mean().alias("mret")).drop_nulls().sort("day"))
        rng = np.random.default_rng(0)

        def _boot_p5(trades: pl.DataFrame) -> float:
            mtm = _portfolio_mtm_equity(trades, klines)
            r = np.asarray(mtm["basket_return"].to_list(), dtype=float)
            ts = mtm["ts_ms"].to_list()
            if len(r) < 30:
                return 0.0
            years = max((int(ts[-1]) - int(ts[0])) / (365.25 * 24 * 3_600_000), 1e-9)
            block, nb, mars = 21, int(np.ceil(len(r) / 21)), []
            for _ in range(2000):
                # audit2: upper bound is len(r)-block+1 (integers() is half-open, so
                # max start = len(r)-block) — the old len(r)-block dropped the final
                # block position and never resampled the last observation (off-by-one).
                # Mirrors r1_robustness._resample_block_indices max_start.
                samp = np.concatenate([r[s:s + block] for s in rng.integers(0, max(1, len(r) - block + 1), nb)])[:len(r)]
                eq = 1.0 + np.cumsum(samp)
                maxdd = float(-(eq - np.maximum.accumulate(eq)).min())
                if maxdd > 1e-9:
                    mars.append((float(eq[-1] - 1.0) / years) / maxdd)
            return float(np.percentile(mars, 5)) if mars else 0.0

        for W in (3, 7, 14):
            md = mbase.with_columns(pl.col("mret").rolling_sum(W).shift(1).alias("trend"))
            e2 = (ent.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
                  .join(md.select("day", "trend"), on="day", how="left").drop_nulls("trend"))
            sel = ["symbol", "ts_ms", "composite", "turnover_quote", "spell_end_ts"]
            ts_s, _ = _run_trades(e2.filter(pl.col("trend") > 0).select(sel), symbol_bars, funding_lookup, cfg33, market_daily)
            ts_w, _ = _run_trades(e2.filter(pl.col("trend") <= 0).select(sel), symbol_bars, funding_lookup, cfg33, market_daily)
            ms = _daily_pnl_metrics(_portfolio_mtm_equity(ts_s, klines))
            mw = _daily_pnl_metrics(_portfolio_mtm_equity(ts_w, klines))
            p5 = _boot_p5(ts_s)
            print(f"  trend{W:>2}d: STRONG MAR={ms['mar']:6.1f} (boot-p5={p5:5.1f}, n={ts_s.height})  "
                  f"WEAK MAR={mw['mar']:6.1f} (n={ts_w.height})  ratio={ms['mar']/max(mw['mar'],0.1):4.1f}x", flush=True)
        print(f"[{args.label}/regimevalidate] DONE", flush=True)
        return 0

    # --- REGIME conditioning (structural, untested): the era-split showed a 2x MAR swing across regimes.
    #     Does a CAUSAL market-regime signal (trailing trend / vol) time the fade? Short prior: weak/falling
    #     tape = tailwind, ripping/high-vol = squeeze risk. Era-split MAR per gate guards regime-overfit. ---
    if args.experiment == "regime":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        ent = _fresh_entries(panel, cfg33)
        # equal-weight market daily index -> trailing trend (7d) + vol (14d), CAUSAL (shifted by 1 day).
        md = (klines.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
              .group_by(["symbol", "day"]).agg(pl.col("close").last().alias("c")).sort(["symbol", "day"])
              .with_columns((pl.col("c") / calendar_shift(pl.col("c"), 1, time_col="day") - 1.0).alias("r"))
              .group_by("day").agg(pl.col("r").mean().alias("mret")).drop_nulls().sort("day"))
        md = md.with_columns(pl.col("mret").rolling_sum(7).shift(1).alias("trend7"),
                             pl.col("mret").rolling_std(14).shift(1).alias("vol14"))
        e2 = (ent.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
              .join(md.select("day", "trend7", "vol14"), on="day", how="left").drop_nulls(["trend7", "vol14"]))
        vmed = float(e2["vol14"].median())
        split = _date_str_to_ms(BASE.split_date)
        gates = {
            "all": e2,
            "mkt_weak(trend7<=0)": e2.filter(pl.col("trend7") <= 0),
            "mkt_strong(trend7>0)": e2.filter(pl.col("trend7") > 0),
            "lowvol(<=med)": e2.filter(pl.col("vol14") <= vmed),
            "highvol(>med)": e2.filter(pl.col("vol14") > vmed),
        }

        def _emar(tr: pl.DataFrame) -> float:
            return _daily_pnl_metrics(_portfolio_mtm_equity(tr, klines))["mar"] if not tr.is_empty() else 0.0
        rows = []
        for name, ge in gates.items():
            g = ge.select("symbol", "ts_ms", "composite", "turnover_quote", "spell_end_ts")
            trades, _ = _run_trades(g, symbol_bars, funding_lookup, cfg33, market_daily)
            dm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
            bps = (dm["total_return"] / max(trades.height, 1)) * 1e4
            e1 = _emar(trades.filter(pl.col("entry_ts_ms") < split))
            e2m = _emar(trades.filter(pl.col("entry_ts_ms") >= split))
            rows.append({"gate": name, "mar": dm["mar"], "max_drawdown": abs(dm["max_drawdown"]),
                         "bps_per_trade": bps, "era1_mar": e1, "era2_mar": e2m, "n": int(trades.height)})
            print(f"  {name:22} MAR={dm['mar']:6.1f}  DD={abs(dm['max_drawdown'])*100:4.1f}%  bps={bps:5.1f}  "
                  f"era1={e1:5.1f} era2={e2m:5.1f}  n={trades.height}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "regime", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/regime] DONE", flush=True)
        return 0

    # --- VOL-SCALED STOP (dreg #2): replace the arbitrary fixed 25% stop with k * trailing hourly vol
    #     (tighter for calm names, wider for wild ones). Key: does it beat the fixed stop on MAR + hourly-DD? ---
    if args.experiment == "volstop":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        ent = _fresh_entries(panel, cfg33)
        variants = [("fixed25%(base)", replace(cfg33, stop_vol_mult=0.0))]
        variants += [(f"volstop_k{k:g}", replace(cfg33, stop_vol_mult=float(k))) for k in (3, 5, 8)]
        rows = []
        for name, cfg in variants:
            trades, _ = _run_trades(ent, symbol_bars, funding_lookup, cfg, market_daily)
            dm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
            he = _portfolio_mtm_equity_hourly(trades, klines)
            hdd = abs(float(he["drawdown"].min())) if not he.is_empty() else 0.0
            stopped = int(trades.filter(pl.col("exit_reason") == "stop").height) if "exit_reason" in trades.columns else -1
            rows.append({"variant": name, "mar": dm["mar"], "max_drawdown": abs(dm["max_drawdown"]), "hourly_dd": hdd,
                         "total_return": dm["total_return"], "n_stopped": stopped, "n": int(trades.height)})
            print(f"  {name:16} MAR={dm['mar']:6.1f}  DD={abs(dm['max_drawdown'])*100:4.1f}%  hDD={hdd*100:4.1f}%  "
                  f"ret={dm['total_return']*100:5.0f}%  stopped={stopped}  n={trades.height}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "volstop", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/volstop] DONE", flush=True)
        return 0

    # --- CONFIRMED-FADE entry (thesis-central, untested): the strategy is "short the CONFIRMED fade (pop
    #     then giveback), NOT the top". Entry is currently time-based (+1h). Test a PRICE-ACTION gate: only
    #     enter if the name has already given back >=g% from its rolling-high at the signal bar. ---
    if args.experiment == "fadeconfirm":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        ent = _fresh_entries(panel, cfg33)
        L = 48  # rolling-high lookback (hours) = the recent pop peak
        kh = (klines.select("symbol", "ts_ms", "high", "close").sort(["symbol", "ts_ms"])
              .with_columns(pl.col("high").rolling_max(window_size=L, min_samples=6).over("symbol").alias("rhigh")))
        kh = kh.with_columns(((pl.col("rhigh") - pl.col("close")) / pl.col("rhigh")).alias("gb")).select("symbol", "ts_ms", "gb")
        e2 = ent.join(kh, on=["symbol", "ts_ms"], how="left").drop_nulls("gb")
        gates = {
            "all(time-only)": e2,
            "giveback>=2%": e2.filter(pl.col("gb") >= 0.02),
            "giveback>=5%": e2.filter(pl.col("gb") >= 0.05),
            "giveback>=10%": e2.filter(pl.col("gb") >= 0.10),
            "near_high<2%(top)": e2.filter(pl.col("gb") < 0.02),
        }
        # Each gate is evaluated full-sample with only a pooled MAR; carry an
        # era1/era2 MAR split (split on entry_ts_ms vs BASE.split_date, as
        # `regime`/`funding` do) so a giveback-gate verdict shows split stability
        # and isn't an in-sample multiple-testing read (#19, alpha-scripts-6).
        split = _date_str_to_ms(BASE.split_date)

        def _emar(tr: pl.DataFrame) -> float:
            return _daily_pnl_metrics(_portfolio_mtm_equity(tr, klines))["mar"] if not tr.is_empty() else 0.0
        rows = []
        for name, ge in gates.items():
            ge2 = ge.select("symbol", "ts_ms", "composite", "turnover_quote", "spell_end_ts")
            trades, _ = _run_trades(ge2, symbol_bars, funding_lookup, cfg33, market_daily)
            dm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
            he = _portfolio_mtm_equity_hourly(trades, klines)
            hdd = abs(float(he["drawdown"].min())) if not he.is_empty() else 0.0
            bps = (dm["total_return"] / max(trades.height, 1)) * 1e4
            e1 = _emar(trades.filter(pl.col("entry_ts_ms") < split))
            e2m = _emar(trades.filter(pl.col("entry_ts_ms") >= split))
            rows.append({"gate": name, "mar": dm["mar"], "max_drawdown": abs(dm["max_drawdown"]), "hourly_dd": hdd,
                         "total_return": dm["total_return"], "ret_bps_per_trade": bps,
                         "era1_mar": e1, "era2_mar": e2m, "n": int(trades.height)})
            print(f"  {name:20} MAR={dm['mar']:6.1f}  DD={abs(dm['max_drawdown'])*100:4.1f}%  hDD={hdd*100:4.1f}%  "
                  f"ret={dm['total_return']*100:5.0f}%  bps/trade={bps:5.1f}  era1={e1:5.1f} era2={e2m:5.1f}  n={trades.height}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "fadeconfirm", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/fadeconfirm] DONE", flush=True)
        return 0

    # --- IMPACT-SENSITIVITY: the de-gross win comes from convex (sqrt) impact. Show its dependence on the
    #     impact-convexity assumption: exp=0 (size-independent => de-gross should be MAR-neutral), 0.5 (the
    #     standard sqrt law, baseline), 1.0 (linear impact => strongly convex). gross 0.5 vs 0.3. ---
    if args.experiment == "impactsens":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        ent = _fresh_entries(panel, cfg33)
        rows = []
        for exp in (0.0, 0.5, 1.0):
            mar_by_g = {}
            for g in (0.5, 0.3):
                cfg = replace(cfg33, impact_exponent=exp, gross_exposure=g)
                trades, _ = _run_trades(ent, symbol_bars, funding_lookup, cfg, market_daily)
                dm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
                mar_by_g[g] = dm["mar"]
            delta = mar_by_g[0.3] - mar_by_g[0.5]
            rows.append({"impact_exp": exp, "mar_g050": mar_by_g[0.5], "mar_g030": mar_by_g[0.3], "degross_delta": delta})
            tag = "(standard sqrt law)" if exp == 0.5 else ("(size-independent)" if exp == 0.0 else "(linear impact)")
            print(f"  impact_exp={exp:.1f} {tag:20}: MAR g0.5={mar_by_g[0.5]:5.1f}  g0.3={mar_by_g[0.3]:5.1f}  "
                  f"de-gross delta={delta:+5.1f}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "impactsens", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/impactsens] DONE", flush=True)
        return 0

    # --- RISK-VALIDATE: do the two iter-13 risk findings (concentration cap + lower gross) hold across eras
    #     + block-bootstrap, and do they STACK? Gate before recommending any control. ---
    if args.experiment == "riskvalidate":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        base_ent = _fresh_entries(panel, cfg33).with_columns(
            pl.col("composite").rank(method="ordinal", descending=True).over("ts_ms").alias("_hr"))
        split = _date_str_to_ms(BASE.split_date)
        rng = np.random.default_rng(0)

        def _era_mar(tr: pl.DataFrame) -> float:
            if tr.is_empty():
                return 0.0
            return _daily_pnl_metrics(_portfolio_mtm_equity(tr, klines))["mar"]

        def _run_cfg(name: str, cap_k: int | None, g: float) -> dict:
            cfg = replace(cfg33, gross_exposure=g)
            ent = base_ent if cap_k is None else base_ent.filter(pl.col("_hr") <= cap_k)
            ent = ent.select("symbol", "ts_ms", "composite", "turnover_quote", "spell_end_ts")
            trades, _ = _run_trades(ent, symbol_bars, funding_lookup, cfg, market_daily)
            mtm = _portfolio_mtm_equity(trades, klines)
            dm = _daily_pnl_metrics(mtm)
            he = _portfolio_mtm_equity_hourly(trades, klines)
            hdd = abs(float(he["drawdown"].min())) if not he.is_empty() else 0.0
            r = np.asarray(mtm["basket_return"].to_list(), dtype=float)
            ts = mtm["ts_ms"].to_list()
            years = max((int(ts[-1]) - int(ts[0])) / (365.25 * 24 * 3_600_000), 1e-9)
            block, nb = 21, int(np.ceil(len(r) / 21))
            mars = []
            for _ in range(3000):
                # audit2: see note above — len(r)-block+1 so the final observation
                # is reachable as a block start (off-by-one fix).
                samp = np.concatenate([r[s:s + block] for s in rng.integers(0, max(1, len(r) - block + 1), nb)])[:len(r)]
                eq = 1.0 + np.cumsum(samp)
                maxdd = float(-(eq - np.maximum.accumulate(eq)).min())
                if maxdd > 1e-9:
                    mars.append((float(eq[-1] - 1.0) / years) / maxdd)
            p5 = float(np.percentile(mars, 5)) if mars else 0.0
            e1 = _era_mar(trades.filter(pl.col("entry_ts_ms") < split))
            e2 = _era_mar(trades.filter(pl.col("entry_ts_ms") >= split))
            out = {"cfg": name, "mar": dm["mar"], "daily_dd": abs(dm["max_drawdown"]), "hourly_dd": hdd,
                   "boot_p5": p5, "era1_mar": e1, "era2_mar": e2, "total_return": dm["total_return"], "n": int(trades.height)}
            print(f"  {name:16} MAR={dm['mar']:5.1f}  dDD={abs(dm['max_drawdown'])*100:4.1f}%  hDD={hdd*100:4.1f}%  "
                  f"boot-p5={p5:5.1f}  era1={e1:5.1f} era2={e2:5.1f}  ret={dm['total_return']*100:4.0f}%  n={int(trades.height)}", flush=True)
            return out
        rows = [_run_cfg("baseline", None, 0.5), _run_cfg("cap3", 3, 0.5),
                _run_cfg("gross0.3", None, 0.3), _run_cfg("cap3+gross0.3", 3, 0.3)]
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "riskvalidate", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/riskvalidate] DONE", flush=True)
        return 0

    # --- DE-GROSS frontier: scaling gross_exposure is a pure leverage dial (return & DD scale together =>
    #     hourly-MAR flat). If so, the concentration cap's hourly-MAR lift is a genuinely NEW lever, not
    #     re-parameterized de-grossing. ---
    if args.experiment == "degross":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        ent = _fresh_entries(panel, cfg33)
        rows = []
        for g in (0.5, 0.4, 0.3, 0.25, 0.2):
            cfg = replace(cfg33, gross_exposure=g)
            trades, _ = _run_trades(ent, symbol_bars, funding_lookup, cfg, market_daily)
            dm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
            he = _portfolio_mtm_equity_hourly(trades, klines)
            hdd = abs(float(he["drawdown"].min())) if not he.is_empty() else 0.0
            ann = dm["mar"] * abs(dm["max_drawdown"])
            hmar = ann / hdd if hdd > 0 else 0.0
            rows.append({"gross": g, "mar": dm["mar"], "max_drawdown": abs(dm["max_drawdown"]),
                         "hourly_dd": hdd, "hourly_mar": hmar, "total_return": dm["total_return"]})
            print(f"  gross={g:.2f}: MAR={dm['mar']:6.1f}  daily-DD={abs(dm['max_drawdown'])*100:4.1f}%  "
                  f"HOURLY-DD={hdd*100:4.1f}%  hourly-MAR={hmar:5.1f}  ret={dm['total_return']*100:5.0f}%", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "degross", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/degross] DONE", flush=True)
        return 0

    # --- CONCENTRATION cap (dreg #1): cap entries/hour to top-K by composite -> trim the correlated squeeze
    #     burst. Key metric = intraday (hourly) DD, the squeeze tail measured in iter 10. ---
    if args.experiment == "concentration":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        ent = _fresh_entries(panel, cfg33).with_columns(
            pl.col("composite").rank(method="ordinal", descending=True).over("ts_ms").alias("_hr"))
        rows = []
        for K in (10 ** 9, 8, 5, 3, 2):
            ge = ent.filter(pl.col("_hr") <= K).select("symbol", "ts_ms", "composite", "turnover_quote", "spell_end_ts")
            trades, _ = _run_trades(ge, symbol_bars, funding_lookup, cfg33, market_daily)
            dm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
            he = _portfolio_mtm_equity_hourly(trades, klines)
            hdd = abs(float(he["drawdown"].min())) if not he.is_empty() else 0.0
            klbl = "none" if K > 10 ** 6 else str(K)
            rows.append({"cap_per_hour": klbl, "mar": dm["mar"], "max_drawdown": abs(dm["max_drawdown"]),
                         "hourly_dd": hdd, "total_return": dm["total_return"], "n": int(trades.height)})
            print(f"  cap/hr={klbl:>4}: MAR={dm['mar']:6.1f}  DD={abs(dm['max_drawdown'])*100:4.1f}%  "
                  f"HOURLY-DD={hdd*100:4.1f}%  ret={dm['total_return']*100:5.0f}%  n={trades.height}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "concentration", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/concentration] DONE", flush=True)
        return 0

    # --- FUNDING-conditioned entry: is funding (short-crowding/carry) an entry signal or risk filter? ---
    # Sign: funding>0 => longs pay shorts (long-crowded; we collect carry + a long-driven pump = good fade);
    #       funding<0 => shorts pay (short-crowded => squeeze risk for our short). Causal asof at signal bar.
    if args.experiment == "funding":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        ent = _fresh_entries(panel, cfg33)
        fraw = _read_window(root, fname, start_ms=start_ms - 240 * MS_PER_HOUR, end_ms=end_ms + pad)
        rate_col = "funding_rate_8h_equiv" if "funding_rate_8h_equiv" in fraw.columns else "funding_rate"
        fdf = (fraw.select("symbol", "ts_ms", pl.col(rate_col).alias("frate"))
               .drop_nulls().sort(["symbol", "ts_ms"]))
        e2 = (ent.sort(["symbol", "ts_ms"]).join_asof(fdf, on="ts_ms", by="symbol", strategy="backward")
              .drop_nulls("frate"))
        q10, q33, q66 = (float(e2["frate"].quantile(q)) for q in (0.10, 1 / 3, 2 / 3))
        gates = {
            "all(baseline)": e2,
            "pos_carry(f>0)": e2.filter(pl.col("frate") > 0),
            "excl_neg_decile(>=q10)": e2.filter(pl.col("frate") >= q10),
            "top_tercile(>=q66)": e2.filter(pl.col("frate") >= q66),
            "bottom_tercile(<=q33)": e2.filter(pl.col("frate") <= q33),
        }
        # The gate thresholds (q10/q33/q66) are read off the FULL sample, so a
        # best-gate read with only a pooled MAR is the multiple-testing trap
        # (#19). Carry an era1/era2 MAR split (split on entry_ts_ms vs
        # BASE.split_date, as `regime` does) so any gate verdict shows whether
        # the edge is stable out-of-sample before it is cited (alpha-scripts-6).
        split = _date_str_to_ms(BASE.split_date)

        def _emar(tr: pl.DataFrame) -> float:
            return _daily_pnl_metrics(_portfolio_mtm_equity(tr, klines))["mar"] if not tr.is_empty() else 0.0
        rows = []
        for name, ge in gates.items():
            ge2 = ge.select("symbol", "ts_ms", "composite", "turnover_quote", "spell_end_ts")
            trades, _ = _run_trades(ge2, symbol_bars, funding_lookup, cfg33, market_daily)
            dm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
            e1 = _emar(trades.filter(pl.col("entry_ts_ms") < split))
            e2m = _emar(trades.filter(pl.col("entry_ts_ms") >= split))
            rows.append({"gate": name, "mar": dm["mar"], "max_drawdown": abs(dm["max_drawdown"]),
                         "total_return": dm["total_return"], "era1_mar": e1, "era2_mar": e2m,
                         "n": int(trades.height)})
            print(f"  {name:24} MAR={dm['mar']:6.1f}  DD={abs(dm['max_drawdown'])*100:4.1f}%  "
                  f"ret={dm['total_return']*100:5.0f}%  era1={e1:5.1f} era2={e2m:5.1f}  n={trades.height}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "funding", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/funding] DONE", flush=True)
        return 0

    # --- COST-STRESS: does the recommended edge survive 1.5-3x the (uncalibrated) impact/slippage cost? ---
    if args.experiment == "coststress":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        trades, _ = _run_trades(_fresh_entries(panel, cfg33), symbol_bars, funding_lookup, cfg33, market_daily)
        base_cost_bps = abs(float(trades["cost_return"].mean())) * 1e4 if trades.height else 0.0
        print(f"  base modeled cost ~{base_cost_bps:.1f} bps/trade, n={trades.height}", flush=True)
        rows = []
        for k in (1.0, 1.5, 2.0, 3.0):
            tk = trades.with_columns((pl.col("cost_return") * k).alias("cost_return"))
            dm = _daily_pnl_metrics(_portfolio_mtm_equity(tk, klines))
            rows.append({"cost_mult": k, "mar": dm["mar"], "max_drawdown": abs(dm["max_drawdown"]),
                         "total_return": dm["total_return"]})
            print(f"  cost x{k:>3}: MAR={dm['mar']:6.1f}  DD={abs(dm['max_drawdown'])*100:4.1f}%  "
                  f"ret={dm['total_return']*100:5.0f}%", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "coststress",
                                                  "base_cost_bps": base_cost_bps, "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/coststress] DONE", flush=True)
        return 0

    # --- INTRADAY (hourly-MTM) drawdown: the true squeeze risk, vs daily-MTM; d0 (legacy) vs d1 (fix) ---
    if args.experiment == "intraday":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)  # rmom33 panel (entry_delay-independent)
        rows = []
        for lbl, dcfg in [("d1_rmom33(fix)", cfg33), ("d0_rmom33(legacy)", replace(cfg33, entry_delay_hours=0))]:
            trades, _ = _run_trades(_fresh_entries(panel, dcfg), symbol_bars, funding_lookup, dcfg, market_daily)
            dm = _daily_pnl_metrics(_portfolio_mtm_equity(trades, klines))
            he = _portfolio_mtm_equity_hourly(trades, klines)
            hdd = abs(float(he["drawdown"].min())) if not he.is_empty() else 0.0
            worst_hr = float(he["basket_return"].min()) if not he.is_empty() else 0.0
            m = {"cfg": lbl, "mar": dm["mar"], "daily_dd": abs(dm["max_drawdown"]), "hourly_dd": hdd,
                 "worst_hour": worst_hr, "total_return": dm["total_return"], "n_trades": int(trades.height)}
            rows.append(m)
            print(f"  {lbl:18} MAR={dm['mar']:.1f}  daily-DD={abs(dm['max_drawdown'])*100:.1f}%  "
                  f"HOURLY-DD={hdd*100:.1f}%  worst-hr={worst_hr*100:.2f}%  ret={dm['total_return']*100:.0f}%", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "intraday", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/intraday] DONE", flush=True)
        return 0

    # --- robustness/fragility of the recommended config (d1 + rmom33): block-bootstrap MAR + era split ---
    if args.experiment == "fragility":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        trades, _ = _run_trades(_fresh_entries(panel, cfg33), symbol_bars, funding_lookup, cfg33, market_daily)
        mtm = _portfolio_mtm_equity(trades, klines)
        pt = _daily_pnl_metrics(mtm)
        r = np.asarray(mtm["basket_return"].to_list(), dtype=float)
        ts = mtm["ts_ms"].to_list()
        years = max((int(ts[-1]) - int(ts[0])) / (365.25 * 24 * 3_600_000), 1e-9)
        rng = np.random.default_rng(0)
        block, n_boot = 21, 5000
        nb = int(np.ceil(len(r) / block))
        mars = []
        for _ in range(n_boot):
            # audit2: len(r)-block+1 upper bound (half-open) so max start = len(r)-block
            # and the final observation is reachable — fixes the off-by-one that dropped
            # the last data point from every resample. Mirrors r1_robustness max_start.
            starts = rng.integers(0, max(1, len(r) - block + 1), nb)
            samp = np.concatenate([r[s:s + block] for s in starts])[:len(r)]
            eq = 1.0 + np.cumsum(samp)
            maxdd = float(-(eq - np.maximum.accumulate(eq)).min())
            mars.append((float(eq[-1] - 1.0) / years) / maxdd if maxdd > 1e-9 else np.nan)
        mars = np.array([m for m in mars if not np.isnan(m)])
        # era split
        split = _date_str_to_ms(BASE.split_date)
        early = trades.filter(pl.col("entry_ts_ms") < split)
        recent = trades.filter(pl.col("entry_ts_ms") >= split)
        e_ret = float(early["net_return"].sum()) if not early.is_empty() else 0.0
        r_ret = float(recent["net_return"].sum()) if not recent.is_empty() else 0.0
        out = {"label": args.label, "point_mar": pt["mar"], "point_dd": abs(pt["max_drawdown"]),
               "boot_mar_p5": float(np.percentile(mars, 5)), "boot_mar_p50": float(np.percentile(mars, 50)),
               "boot_mar_p95": float(np.percentile(mars, 95)), "early_ret": e_ret, "recent_ret": r_ret,
               "n_trades": int(trades.height)}
        print(f"  [{args.label}] d1+rmom33  point MAR={pt['mar']:.1f} DD={abs(pt['max_drawdown'])*100:.1f}%  "
              f"block-boot MAR p5={out['boot_mar_p5']:.1f} p50={out['boot_mar_p50']:.1f} p95={out['boot_mar_p95']:.1f}  "
              f"early_ret={e_ret*100:.0f}% recent_ret={r_ret*100:.0f}%", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps(out, indent=2, default=str))
        print(f"[{args.label}/fragility] DONE", flush=True)
        return 0

    # --- IC diagnostic: which features predict the fade, and is equal-weighting suboptimal? (rmom33) ---
    if args.experiment == "ic":
        from liquidity_migration.continuous_events import per_symbol_timeseries_features
        rmom = pl.read_parquet(root / "residual_momentum.parquet").rename({"ts_ms": "day_ts"})
        feats = per_symbol_timeseries_features(klines.select("ts_ms", "symbol", "close", "turnover_quote"))
        # 12h fade outcome from the +1h entry (d1): close[t+13]/close[t+1]-1, per symbol (causal target)
        feats = feats.with_columns(
            (pl.col("close").shift(-13).over("symbol") / pl.col("close").shift(-1).over("symbol") - 1.0).alias("fwd"))
        feats = feats.with_columns(((pl.col("ts_ms") // (24 * MS_PER_HOUR)) * (24 * MS_PER_HOUR)).alias("day_ts"))
        feats = feats.join(rmom, on=["symbol", "day_ts"], how="left").filter(pl.col("residual_momentum").is_not_null())
        feats = feats.with_columns(
            ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_rr")
        ).filter(pl.col("_rr") <= 0.33).filter(pl.col("fwd").is_not_null())
        # short fade outcome (profit when price falls) = -fwd; rank-IC = corr of feature-rank vs (-fwd)-rank per ts
        feats = feats.with_columns(pl.col("fwd").mul(-1.0).rank().over("ts_ms").alias("r_short"))
        flist = ["rv_168h", "vov", "dist_low", "max_ret168", "ret168", "ret72"]
        ranks = {f: f"r_{f}" for f in flist}
        feats = feats.with_columns([pl.col(f).rank().over("ts_ms").alias(rn) for f, rn in ranks.items()])
        # equal-weight composite of the 5 default features (rv,vov,dist_low,xsret7=ret168,xsret3=ret72)
        comp5 = ["r_rv_168h", "r_vov", "r_dist_low", "r_ret168", "r_ret72"]
        feats = feats.with_columns(pl.mean_horizontal([pl.col(c) for c in comp5]).alias("r_comp5"))
        print(f"  [{args.label}] rank-IC vs the 12h short-fade outcome (rmom33 universe, n={feats.height}):", flush=True)
        for f in flist:
            ic = feats.select(pl.corr(f"r_{f}", "r_short")).item()
            print(f"    {f:12} IC={ic:+.4f}", flush=True)
        comp_ic = feats.select(pl.corr("r_comp5", "r_short")).item()
        print(f"    {'COMPOSITE5':12} IC={comp_ic:+.4f}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "ic",
                "feature_ic": {f: feats.select(pl.corr(f'r_{f}', 'r_short')).item() for f in flist},
                "composite5_ic": comp_ic, "n": int(feats.height)}, indent=2, default=str))
        print(f"[{args.label}/ic] DONE", flush=True)
        return 0

    # --- seasonality diagnostic: does entry hour-of-day (UTC) predict trade quality? (at rmom33) ---
    if args.experiment == "session":
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        trades, _ = _run_trades(_fresh_entries(panel, cfg33), symbol_bars, funding_lookup, cfg33, market_daily)
        t = trades.with_columns(((pl.col("entry_ts_ms") // 3_600_000) % 24).alias("hod"))
        gs = t.group_by((pl.col("hod") // 6).alias("session")).agg(
            pl.len().alias("n"), (pl.col("net_return").mean() * 1e4).alias("mean_bps"),
            (pl.col("net_return").sum() * 100).alias("sum_pct")).sort("session")
        print("  session (UTC 6h buckets): 0=0-5 1=6-11 2=12-17 3=18-23", flush=True)
        for r in gs.iter_rows(named=True):
            print(f"    s{r['session']}  n={r['n']:6d}  mean={r['mean_bps']:7.2f}bps  sum={r['sum_pct']:7.1f}%", flush=True)
        gh = t.group_by("hod").agg(pl.len().alias("n"), (pl.col("net_return").mean() * 1e4).alias("mean_bps")).sort("hod")
        print("  by hour:", " ".join(f"{r['hod']:02d}:{r['mean_bps']:.1f}" for r in gh.iter_rows(named=True)), flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "session",
                "session": gs.to_dicts(), "hour": gh.to_dicts()}, indent=2, default=str))
        print(f"[{args.label}/session] DONE", flush=True)
        return 0

    # --- beta-neutral L/S construction: long D0 + short D9 (50/50), at the adopted rmom33 ---
    if args.experiment == "ls":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        cfg33 = replace(BASE, rmom_quantile=0.33)
        panel = build_continuous_panel(root, cfg33)
        rows, legs = [], {}
        for leg, lcfg in [("short_d9", cfg33), ("long_d0", replace(cfg33, side="long", decile=0))]:
            ent = _fresh_entries(panel, lcfg)
            trades, _ = _run_trades(ent, symbol_bars, funding_lookup, lcfg, market_daily)
            mtm = _portfolio_mtm_equity(trades, klines)
            legs[leg] = mtm
            m = _daily_pnl_metrics(mtm)
            m.update({"book": leg, "n_trades": int(trades.height)})
            rows.append(m)
        comb = pl.concat([
            legs["short_d9"].select("ts_ms", (pl.col("basket_return") * 0.5).alias("br")),
            legs["long_d0"].select("ts_ms", (pl.col("basket_return") * 0.5).alias("br")),
        ]).group_by("ts_ms").agg(pl.col("br").sum().alias("basket_return")).sort("ts_ms")
        comb = comb.with_columns((1.0 + pl.col("basket_return").cum_sum()).alias("equity")).with_columns(
            (pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown")).select("ts_ms", "equity", "drawdown", "basket_return")
        mc = _daily_pnl_metrics(comb)
        mc["book"] = "LS_5050"
        rows.append(mc)
        for m in rows:
            mar = m.get("mar")
            print(f"  {m['book']:10} MAR={('%.2f'%mar) if mar is not None else 'NA':>6} "
                  f"DD={abs(m['max_drawdown'])*100:5.1f}% ret={m['total_return']*100:7.1f}% "
                  f"sharpe={m['sharpe_like']:.1f} wd={m['worst_day_return']*100:6.2f}%", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "ls", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/ls] DONE", flush=True)
        return 0

    # --- feature-set variants (special branch: rebuilds the panel per composite feature set) ---
    if args.experiment == "features":
        from liquidity_migration.continuous_events import FEATURES, compute_continuous_decile_panel
        rmom = pl.read_parquet(root / "residual_momentum.parquet").rename({"ts_ms": "day_ts"})
        kp = klines.select("ts_ms", "symbol", "close", "turnover_quote")
        cfg33 = replace(BASE, rmom_quantile=0.33)
        start = _date_str_to_ms(BASE.start_date)
        variants = {
            "base5": FEATURES,
            "drop_vov": tuple(f for f in FEATURES if f != "vov"),
            "add_max": FEATURES + ("max_ret168",),
            "dropvov_addmax": tuple(f for f in FEATURES if f != "vov") + ("max_ret168",),
            "max_for_rv": tuple("max_ret168" if f == "rv_168h" else f for f in FEATURES),
        }
        rows = []
        for name, fs in variants.items():
            panel = compute_continuous_decile_panel(kp, rmom, rmom_quantile=0.33, start_ms=start, feature_set=fs)
            ent = _fresh_entries(panel, cfg33)
            trades, _ = _run_trades(ent, symbol_bars, funding_lookup, cfg33, market_daily)
            m = _metrics(trades, klines)
            m.update({"variant": name, "features": list(fs)})
            rows.append(m)
            mar = m.get("mtm_mar")
            print(f"  {name:16} trades={m['n_trades']:6d} ret={m['total_return']*100:7.1f}% "
                  f"MAR={('%.2f'%mar) if mar is not None else 'NA':>6} DD={abs(m['mtm_maxdd'])*100:5.1f}% "
                  f"wd={m['mtm_worst_day']*100:6.2f}%", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "features", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/features] DONE", flush=True)
        return 0

    # --- vol-target overlay (special branch: overlays the MTM equity, not a config sweep) ---
    if args.experiment == "voltarget":
        from liquidity_migration.continuous_events import _portfolio_mtm_equity
        rows = []
        for sl, scfg in [("base", BASE), ("rmom33", replace(BASE, rmom_quantile=0.33))]:
            ent = _fresh_entries(build_continuous_panel(root, scfg), scfg) if sl == "rmom33" else entries
            trades, _ = _run_trades(ent, symbol_bars, funding_lookup, scfg, market_daily)
            mtm = _portfolio_mtm_equity(trades, klines)
            variants = [("none", mtm)]
            for lb in (10, 20):
                for t in (0.05, 0.08, 0.12):  # below the book's ~10% ann vol so the overlay actually bites
                    variants.append((f"vt{int(t*100)}/lb{lb}", _apply_vol_target(mtm, target_vol_annual=t, lookback=lb, floor=0.1)))
            for tv, ov in variants:
                m = _daily_pnl_metrics(ov)
                m.update({"sel": sl, "vt": tv})
                rows.append(m)
                mar = m.get("mar")
                print(f"  {sl:7}/{tv:5} MAR={('%.2f'%mar) if mar is not None else 'NA':>6} "
                      f"DD={abs(m['max_drawdown'])*100:5.1f}% ret={m['total_return']*100:7.1f}% "
                      f"sharpe={m['sharpe_like']:.1f} wd={m['worst_day_return']*100:6.2f}%", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"label": args.label, "experiment": "voltarget", "rows": rows}, indent=2, default=str))
        print(f"[{args.label}/voltarget] DONE", flush=True)
        return 0

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
    # Harness-only override keys the main loop's entry-resolution actually consumes
    # (they are intentionally NOT ContinuousEventConfig fields). Any override key
    # that is neither a config field nor one of these is a dead parameter: the
    # cfg_fields filter drops it AND no entry-resolution branch reads it, so the
    # cell would silently run base-equivalent and manufacture a fake null result
    # (alpha-scripts-3). Fail fast rather than emit a misleading flat finding.
    ENTRY_RESOLVED_KEYS = {"rmom_quantile", "liq_turnover_min", "turnover_surge_min"}
    for label, ov in plan:
        dead = [k for k in ov if k not in cfg_fields and k not in ENTRY_RESOLVED_KEYS]
        if dead:
            raise SystemExit(
                f"dead override key(s) {dead} in cell '{label}': not a ContinuousEventConfig "
                f"field and not consumed by any entry-resolution branch — this cell would run "
                f"base-equivalent and fabricate a null result. Wire the parameter through (config "
                f"field + engine support, or a new entry-resolution branch) or remove the experiment."
            )
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
