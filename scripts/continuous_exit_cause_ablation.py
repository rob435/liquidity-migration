#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import heapq
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[1]
SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
FULL_LIVE_ARTIFACT = Path(
    os.environ.get("FULL_LIVE_ARTIFACT", str(SHARED / "full_live_system_backtest_2026-06-18"))
)

sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(FULL_LIVE_ARTIFACT))

import continuous_live_feature_ablation_runner as broad  # noqa: E402
import continuous_deployed_equity_refresh as refresh  # noqa: E402

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR  # noqa: E402
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _build_lifecycle_config,
    _compute_size_and_stop,
    _plan_exit,
    _portfolio_mtm_equity,
    _round_trip_bps,
    build_continuous_panel,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    apply_rebalance_rule,
    decompose_continuous_components,
)
from liquidity_migration.trade_lifecycle import _empty_trades, _simulate_indexed_trade  # noqa: E402


EXIT_RUNG_DEFS: list[dict[str, Any]] = [
    {
        "id": "00_fixed_tp_baseline",
        "group": "baseline",
        "state_exit": False,
        "stop_approach": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "01_left_decile_only",
        "group": "individual",
        "state_exit": True,
        "stop_approach": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "02_stop_approach_only",
        "group": "individual",
        "state_exit": False,
        "stop_approach": True,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "03_failed_fade_only",
        "group": "individual",
        "state_exit": False,
        "stop_approach": False,
        "failed_fade": True,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "04_breakeven_only",
        "group": "individual",
        "state_exit": False,
        "stop_approach": False,
        "failed_fade": False,
        "breakeven": True,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "05_exit_cooldown_only",
        "group": "individual",
        "state_exit": False,
        "stop_approach": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": True,
        "adverse_breaker": False,
    },
    {
        "id": "06_adverse_breaker_only",
        "group": "individual",
        "state_exit": False,
        "stop_approach": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": True,
    },
    {
        "id": "10_cumulative_left_decile",
        "group": "cumulative",
        "state_exit": True,
        "stop_approach": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "11_cumulative_left_stop",
        "group": "cumulative",
        "state_exit": True,
        "stop_approach": True,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "12_cumulative_left_stop_failed",
        "group": "cumulative",
        "state_exit": True,
        "stop_approach": True,
        "failed_fade": True,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "13_cumulative_left_stop_failed_be",
        "group": "cumulative",
        "state_exit": True,
        "stop_approach": True,
        "failed_fade": True,
        "breakeven": True,
        "exit_cooldown": False,
        "adverse_breaker": False,
    },
    {
        "id": "14_cumulative_left_stop_failed_be_cooldown",
        "group": "cumulative",
        "state_exit": True,
        "stop_approach": True,
        "failed_fade": True,
        "breakeven": True,
        "exit_cooldown": True,
        "adverse_breaker": False,
    },
    {
        "id": "15_full_live_exit_lifecycle",
        "group": "cumulative",
        "state_exit": True,
        "stop_approach": True,
        "failed_fade": True,
        "breakeven": True,
        "exit_cooldown": True,
        "adverse_breaker": True,
    },
]


def iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def make_cfg(start: str, end: str, opts: dict[str, Any]) -> ContinuousEventConfig:
    sizing = str(opts.get("sizing", "flat"))
    cfg = broad.base_cfg(start, end, gate=str(opts.get("gate", "off")), sizing=sizing, live_exits=False)
    state_exit = bool(opts["state_exit"])
    server_stop = bool(opts.get("server_stop", False))
    stop_approach = bool(opts["stop_approach"])
    return replace(
        cfg,
        exit_mode="state" if state_exit else "fixed",
        exit_decile_buffer=1 if state_exit else 0,
        hold_hours=24,
        max_hold_hours=24 if state_exit else 48,
        take_profit_pct=0.10,
        stop_loss_pct=0.25 if (stop_approach or server_stop) else 0.0,
        stop_approach_frac=0.8 if stop_approach else 0.0,
        failed_fade_hours=6 if opts["failed_fade"] else 0,
        failed_fade_loss_pct=0.04 if opts["failed_fade"] else 0.0,
        failed_fade_min_mfe_pct=0.01 if opts["failed_fade"] else 0.0,
        breakeven_arm_pct=0.10 if opts["breakeven"] else 0.0,
        entry_pause_after_adverse_exits=8 if opts["adverse_breaker"] else 0,
        entry_pause_window_hours=24,
    )


def exit_reason_stats(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty() or "exit_reason" not in trades.columns:
        return pl.DataFrame()
    return (
        trades.group_by("exit_reason")
        .agg(
            [
                pl.len().alias("trades"),
                pl.col("net_return").sum().alias("net_return_sum"),
                pl.col("net_return").mean().alias("net_return_mean"),
                pl.col("gross_return").sum().alias("gross_return_sum"),
                pl.col("cost_return").sum().alias("cost_return_sum"),
                pl.col("funding_return").sum().alias("funding_return_sum"),
                pl.col("hold_hours").mean().alias("hold_hours_mean"),
                pl.col("hold_hours").median().alias("hold_hours_median"),
            ]
        )
        .sort("net_return_sum")
    )


def _sum_or_zero(df: pl.DataFrame, col: str) -> float:
    if df.is_empty() or col not in df.columns:
        return 0.0
    return float(df[col].sum() or 0.0)


def matched_trade_delta(
    *,
    venue: str,
    rung: str,
    baseline: pl.DataFrame,
    trades: pl.DataFrame,
    out_dir: Path,
) -> dict[str, Any]:
    keys = ["symbol", "entry_signal_ts_ms", "component"]
    if baseline.is_empty() and trades.is_empty():
        return {"venue": venue, "rung": rung, "matched_trades": 0}
    base_cols = keys + ["exit_reason", "net_return", "hold_hours", "exit_ts_ms"]
    rung_cols = keys + ["exit_reason", "net_return", "hold_hours", "exit_ts_ms"]
    b = baseline.select([c for c in base_cols if c in baseline.columns]).rename(
        {
            "exit_reason": "base_exit_reason",
            "net_return": "base_net_return",
            "hold_hours": "base_hold_hours",
            "exit_ts_ms": "base_exit_ts_ms",
        }
    )
    r = trades.select([c for c in rung_cols if c in trades.columns]).rename(
        {
            "exit_reason": "rung_exit_reason",
            "net_return": "rung_net_return",
            "hold_hours": "rung_hold_hours",
            "exit_ts_ms": "rung_exit_ts_ms",
        }
    )
    joined = r.join(b, on=keys, how="inner").with_columns(
        (pl.col("rung_net_return") - pl.col("base_net_return")).alias("delta_net_return"),
        (pl.col("rung_hold_hours") - pl.col("base_hold_hours")).alias("delta_hold_hours"),
    )
    base_only = b.join(r.select(keys), on=keys, how="anti")
    rung_only = r.join(b.select(keys), on=keys, how="anti")
    if not joined.is_empty():
        joined.write_csv(out_dir / "matched_trade_delta.csv")
        reason_delta = (
            joined.group_by(["rung_exit_reason", "base_exit_reason"])
            .agg(
                [
                    pl.len().alias("matched_trades"),
                    pl.col("delta_net_return").sum().alias("delta_net_return_sum"),
                    pl.col("delta_net_return").mean().alias("delta_net_return_mean"),
                    pl.col("base_net_return").sum().alias("base_net_return_sum"),
                    pl.col("rung_net_return").sum().alias("rung_net_return_sum"),
                    pl.col("delta_hold_hours").mean().alias("delta_hold_hours_mean"),
                ]
            )
            .sort("delta_net_return_sum")
        )
        reason_delta.write_csv(out_dir / "matched_trade_delta_by_reason.csv")
    else:
        reason_delta = pl.DataFrame()
    worst_reason = None
    if not reason_delta.is_empty():
        row = reason_delta.row(0, named=True)
        worst_reason = f"{row['base_exit_reason']}->{row['rung_exit_reason']}"
    return {
        "venue": venue,
        "rung": rung,
        "base_trades": int(b.height),
        "rung_trades": int(r.height),
        "matched_trades": int(joined.height),
        "matched_delta_net_return_sum": _sum_or_zero(joined, "delta_net_return"),
        "base_only_trades": int(base_only.height),
        "base_only_net_return_sum": _sum_or_zero(base_only, "base_net_return"),
        "rung_only_trades": int(rung_only.height),
        "rung_only_net_return_sum": _sum_or_zero(rung_only, "rung_net_return"),
        "trade_sum_delta_net_return": (
            _sum_or_zero(joined, "delta_net_return")
            - _sum_or_zero(base_only, "base_net_return")
            + _sum_or_zero(rung_only, "rung_net_return")
        ),
        "worst_matched_reason_transition": worst_reason,
    }


def run_exit_rung(
    *,
    venue: str,
    cfg: ContinuousEventConfig,
    opts: dict[str, Any],
    candidates: pl.DataFrame,
    bars: dict[str, Any],
    funding_lookup: dict[str, dict[str, Any]] | None,
    klines: pl.DataFrame,
    btc_trend: dict[int, float] | None,
    ages: dict[str, int],
    out_dir: Path,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, dict[str, Any]]:
    delay_ms = (1 + cfg.entry_delay_hours) * MS_PER_HOUR
    hold_ms = cfg.hold_hours * MS_PER_HOUR
    max_hold_ms = cfg.max_hold_hours * MS_PER_HOUR
    stop_approach_on = cfg.stop_loss_pct > 0.0 and cfg.stop_approach_frac > 0.0
    server_stop_on = bool(opts.get("server_stop", False)) and cfg.stop_loss_pct > 0.0
    stop_pct = (
        cfg.stop_loss_pct * cfg.stop_approach_frac
        if stop_approach_on
        else (cfg.stop_loss_pct if server_stop_on else None)
    )
    breaker_window_ms = cfg.entry_pause_window_hours * MS_PER_HOUR
    breaker_on = cfg.entry_pause_after_adverse_exits > 0 and breaker_window_ms > 0
    cooldown_on = bool(opts["exit_cooldown"])
    state_mode = cfg.exit_mode == "state"

    active: list[tuple[int, str, int]] = []
    recent_exit: dict[str, int] = {}
    adverse_exit_ts: list[int] = []
    rows: list[dict[str, Any]] = []
    tape: list[dict[str, Any]] = []
    skips = {
        k: 0
        for k in (
            "skipped_breaker",
            "skipped_btc_trend",
            "skipped_capacity",
            "skipped_max_new",
            "skipped_held_symbol",
            "skipped_exit_cooldown",
            "skipped_age",
            "skipped_no_bar",
            "skipped_no_fill",
        )
    }
    seq = 0
    if not candidates.is_empty() and bars:
        for idx, group in enumerate(candidates.partition_by("ts_ms", maintain_order=True), start=1):
            if idx % 1000 == 0:
                print(f"    {venue} {opts['id']} groups={idx} trades={len(rows)}", flush=True)
            sig_ts = int(group["ts_ms"][0])
            entry_ts = sig_ts + delay_ms
            while active and active[0][0] <= entry_ts:
                exit_ts, sym, _seq = heapq.heappop(active)
                recent_exit[sym] = exit_ts
            held = {sym for _exit, sym, _seq in active}
            trend = None
            btc_ok = True
            if cfg.btc_trend_gate != "off":
                trend = (btc_trend or {}).get((sig_ts // MS_PER_DAY) * MS_PER_DAY)
                btc_ok = trend is not None and (
                    (cfg.btc_trend_gate == "uptrend" and trend > 0.0)
                    or (cfg.btc_trend_gate == "downtrend" and trend <= 0.0)
                )
            breaker_ok = True
            if breaker_on:
                lo = bisect.bisect_left(adverse_exit_ts, entry_ts - breaker_window_ms)
                hi = bisect.bisect_left(adverse_exit_ts, entry_ts)
                breaker_ok = (hi - lo) < cfg.entry_pause_after_adverse_exits
            selected = 0
            batch: list[dict[str, Any]] = []
            for r in group.to_dicts():
                sym = str(r["symbol"])
                reason = "selected"
                if not btc_ok:
                    reason = "btc_trend"
                    skips["skipped_btc_trend"] += 1
                elif not breaker_ok:
                    reason = "breaker"
                    skips["skipped_breaker"] += 1
                elif selected >= 5:
                    reason = "max_new"
                    skips["skipped_max_new"] += 1
                elif len(active) + len(batch) >= cfg.max_active:
                    reason = "capacity"
                    skips["skipped_capacity"] += 1
                elif sym in held:
                    reason = "held_symbol"
                    skips["skipped_held_symbol"] += 1
                elif cooldown_on and entry_ts - recent_exit.get(sym, -10**18) < 30 * 60_000:
                    reason = "exit_cooldown"
                    skips["skipped_exit_cooldown"] += 1
                elif entry_ts - int(ages.get(sym, entry_ts)) < int(cfg.age_days_min) * MS_PER_DAY:
                    reason = "age"
                    skips["skipped_age"] += 1
                audit = {
                    "symbol": sym,
                    "signal_ts_ms": sig_ts,
                    "entry_bar_end_ts_ms": entry_ts,
                    "component": str(r["component"]),
                    "component_weight": float(r["component_weight"]),
                    "composite": float(r["composite"]),
                    "turnover_quote": float(r["turnover_quote"]),
                    "selected": reason == "selected",
                    "reason": reason,
                    "active_count": len(active),
                    "selected_this_signal": selected,
                    "btc_trend": trend,
                }
                tape.append(audit)
                if reason != "selected":
                    continue
                sym_bars = bars.get(sym)
                entry_bar = sym_bars["by_end"].get(entry_ts) if sym_bars is not None else None
                if sym_bars is None or entry_bar is None:
                    skips["skipped_no_bar"] += 1
                    audit.update({"selected": False, "reason": "no_bar_entry"})
                    continue
                cc = replace(cfg, entry_event_trigger=str(r["entry_event_trigger"]), take_profit_pct=0.10)
                nw, trade_stop = _compute_size_and_stop(
                    cc,
                    sym_bars["close"],
                    int(entry_bar),
                    base_nw=cfg.notional_weight * float(r["component_weight"]),
                    inverse_vol=cc.sizing_mode == "inverse_vol",
                    clamp=max(cc.vol_weight_clamp, 1.0),
                    regime_size_mult=1.0,
                    stop_pct=stop_pct,
                )
                planned_exit = _plan_exit(
                    state_mode=state_mode,
                    spell_end=int(r["spell_end_ts"]),
                    entry_bar_end=entry_ts,
                    delay_ms=delay_ms,
                    max_hold_ms=max_hold_ms,
                    hold_ms=hold_ms,
                )
                trade = _simulate_indexed_trade(
                    symbol=sym,
                    side=cc.side,
                    score=float(r["composite"]),
                    rank=int(cc.decile),
                    basket_id=iso(entry_ts),
                    signal_ts_ms=sig_ts,
                    entry_bar=int(entry_bar),
                    symbol_bars=sym_bars,
                    planned_exit_ts_ms=planned_exit,
                    notional_weight=nw,
                    position_weight=1.0,
                    config=_build_lifecycle_config(cc),
                    round_trip_cost_bps=_round_trip_bps(cc, float(r["turnover_quote"]), notional_weight=nw),
                    stop_pct=trade_stop,
                    rank_lookup={},
                    event_decay_threshold=0.0,
                    funding_lookup=funding_lookup if cc.use_funding else None,
                    stop_fill_mode=cc.stop_fill_mode,
                    stop_slippage_cap_pct=cc.stop_slippage_cap_pct,
                )
                if trade is None:
                    skips["skipped_no_fill"] += 1
                    audit.update({"selected": False, "reason": "no_fill"})
                    continue
                if stop_approach_on and trade.get("exit_reason") == "stop_loss":
                    trade["exit_reason"] = "stop_approach"
                elif server_stop_on and trade.get("exit_reason") == "stop_loss":
                    trade["exit_reason"] = "server_stop"
                if (
                    state_mode
                    and trade.get("exit_reason") == "max_hold"
                    and planned_exit < entry_ts + max_hold_ms
                    and int(trade["exit_ts_ms"]) == int(planned_exit)
                ):
                    trade["exit_reason"] = "left_decile"
                trade.update(
                    {
                        "component": str(r["component"]),
                        "component_weight": float(r["component_weight"]),
                        "spell_end_ts_ms": int(r["spell_end_ts"]),
                        "planned_exit_ts_ms": int(planned_exit),
                    }
                )
                batch.append(trade)
                selected += 1
                audit.update({"exit_ts_ms": int(trade["exit_ts_ms"]), "notional_weight": float(nw)})
            for trade in batch:
                rows.append(trade)
                seq += 1
                heapq.heappush(active, (int(trade["exit_ts_ms"]), str(trade["symbol"]), seq))
                if breaker_on and (
                    str(trade.get("exit_reason")) in {"stop_approach", "failed_fade"}
                    or float(trade.get("net_return") or 0.0) < 0.0
                ):
                    bisect.insort(adverse_exit_ts, int(trade["exit_ts_ms"]))
    trades = pl.DataFrame(rows) if rows else _empty_trades()
    mtm = _portfolio_mtm_equity(trades, klines)
    comp = decompose_continuous_components(trades, mtm.select("ts_ms", "basket_return"), asdict(cfg))
    rebalanced = apply_rebalance_rule(comp, refresh.winner_rule())
    tape_df = pl.DataFrame(tape) if tape else pl.DataFrame({"symbol": [], "reason": []})
    tape_df.write_parquet(out_dir / "candidate_tape.parquet")
    return trades, mtm, rebalanced, {"candidate_rows": int(candidates.height), "skips": skips}


def run_venue(
    venue: str,
    *,
    start_date: str,
    end_date: str,
    out_root: Path,
    scratch_base: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    real_root = broad.ROOTS[venue]
    scratch = broad.resolve_scratch(real_root, out_root, end_date, scratch_base)
    print(f"[{venue}] scratch={scratch}", flush=True)
    panel_cfg = broad.base_cfg(start_date, end_date, gate="off", sizing="flat", live_exits=False)
    panel = build_continuous_panel(scratch, panel_cfg)
    print(f"[{venue}] panel rows={panel.height}", flush=True)

    fixed_cfg = make_cfg(start_date, end_date, EXIT_RUNG_DEFS[0])
    state_seed = make_cfg(start_date, end_date, EXIT_RUNG_DEFS[1])
    fixed_candidates = broad.shared_candidates(panel, fixed_cfg)
    state_candidates = broad.shared_candidates(panel, state_seed)
    all_symbols = set()
    for candidates in (fixed_candidates, state_candidates):
        if not candidates.is_empty():
            all_symbols.update(str(s) for s in candidates["symbol"].unique().to_list())
    full_cfg = make_cfg(start_date, end_date, EXIT_RUNG_DEFS[-1])
    bars, funding_lookup, klines, btc_trend = broad.load_market(scratch, full_cfg, all_symbols)
    ages = broad.live.listing_ts(scratch)

    rows: list[dict[str, Any]] = []
    deltas: list[dict[str, Any]] = []
    baseline_trades: pl.DataFrame | None = None
    previous_return: float | None = None
    for rung in EXIT_RUNG_DEFS:
        rid = str(rung["id"])
        cfg = make_cfg(start_date, end_date, rung)
        candidates = state_candidates if rung["state_exit"] else fixed_candidates
        out_dir = out_root / venue / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"[{venue}] rung={rid}", flush=True)
        trades, mtm, rebalanced, meta = run_exit_rung(
            venue=venue,
            cfg=cfg,
            opts=rung,
            candidates=candidates,
            bars=bars,
            funding_lookup=funding_lookup,
            klines=klines,
            btc_trend=btc_trend,
            ages=ages,
            out_dir=out_dir,
        )
        trades_path = out_dir / "trades.csv"
        mtm_path = out_dir / "mtm.csv"
        rb_path = out_dir / "rebalanced_unhedged.csv"
        trades.write_csv(trades_path)
        mtm.write_csv(mtm_path)
        rebalanced.write_csv(rb_path)
        reason_stats = exit_reason_stats(trades)
        if not reason_stats.is_empty():
            reason_stats.write_csv(out_dir / "exit_reason_stats.csv")

        metrics = broad.stats(rebalanced)
        raw_metrics = broad.stats(mtm)
        if baseline_trades is None:
            baseline_trades = trades
        delta = matched_trade_delta(
            venue=venue,
            rung=rid,
            baseline=baseline_trades,
            trades=trades,
            out_dir=out_dir,
        )
        deltas.append(delta)
        summary = {
            "venue": venue,
            "rung": rid,
            "run_label": "exploratory_diagnostic",
            "config": asdict(cfg),
            "exit_switches": rung,
            "data_root": str(real_root),
            "scratch_root": str(scratch),
            "metrics": metrics,
            "raw_mtm_metrics": raw_metrics,
            "n_trades": int(trades.height),
            "exit_reasons": broad.counts(trades, "exit_reason"),
            "component_trades": broad.counts(trades, "component"),
            "meta": meta,
            "matched_trade_delta": delta,
            "paths": {
                "trades": str(trades_path),
                "mtm": str(mtm_path),
                "rebalanced": str(rb_path),
                "candidate_tape": str(out_dir / "candidate_tape.parquet"),
            },
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        row = {
            "venue": venue,
            "rung": rid,
            "group": rung["group"],
            "state_exit": bool(rung["state_exit"]),
            "stop_approach": bool(rung["stop_approach"]),
            "failed_fade": bool(rung["failed_fade"]),
            "breakeven": bool(rung["breakeven"]),
            "exit_cooldown": bool(rung["exit_cooldown"]),
            "adverse_breaker": bool(rung["adverse_breaker"]),
            "total_return": metrics["total_return"],
            "max_drawdown": metrics["max_drawdown"],
            "mar": metrics["mar"],
            "sharpe_like": metrics["sharpe_like"],
            "worst_day_return": metrics["worst_day_return"],
            "n_trades": int(trades.height),
            "candidate_rows": int(meta["candidate_rows"]),
            "delta_return_vs_prev_rung": None if previous_return is None else metrics["total_return"] - previous_return,
        }
        previous_return = metrics["total_return"]
        row.update({k: v for k, v in delta.items() if k not in {"venue", "rung"}})
        rows.append(row)
        print(
            f"[{venue}] {rid} ret={metrics['total_return']:+.4f} "
            f"dd={metrics['max_drawdown']:+.4f} sr={metrics['sharpe_like']:+.2f} trades={trades.height}",
            flush=True,
        )
    return rows, deltas


def write_pooled_tables(out_root: Path, rows: list[dict[str, Any]]) -> dict[str, str]:
    table_path = out_root / "exit_cause_table.csv"
    pooled_path = out_root / "pooled_exit_cause_table.csv"
    df = pl.DataFrame(rows)
    if df.is_empty():
        return {"exit_cause_table": str(table_path), "pooled_exit_cause_table": str(pooled_path)}
    df = df.sort(["venue", "rung"])
    df.write_csv(table_path)
    pooled = (
        df.group_by("rung", "group", maintain_order=True)
        .agg(
            [
                pl.col("total_return").mean().alias("mean_total_return"),
                pl.col("total_return").min().alias("min_total_return"),
                pl.col("total_return").max().alias("max_total_return"),
                pl.col("max_drawdown").mean().alias("mean_max_drawdown"),
                pl.col("n_trades").sum().alias("total_trades"),
                pl.col("matched_delta_net_return_sum").mean().alias("mean_matched_delta_net_return_sum"),
                pl.col("trade_sum_delta_net_return").mean().alias("mean_trade_sum_delta_net_return"),
            ]
        )
        .sort("rung")
    )
    pooled = pooled.with_columns(
        (pl.col("mean_total_return") - pl.col("mean_total_return").shift(1)).alias("delta_mean_return_vs_prev_rung")
    )
    pooled.write_csv(pooled_path)
    return {"exit_cause_table": str(table_path), "pooled_exit_cause_table": str(pooled_path)}


def primary_diagnosis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pl.DataFrame(rows)
    if df.is_empty():
        return {}
    base = df.filter(pl.col("rung") == "00_fixed_tp_baseline")
    if base.is_empty():
        return {}
    joined = df.join(
        base.select(["venue", pl.col("total_return").alias("baseline_total_return")]),
        on="venue",
        how="left",
    ).with_columns((pl.col("total_return") - pl.col("baseline_total_return")).alias("delta_vs_baseline"))
    individual = joined.filter(pl.col("group") == "individual").sort("delta_vs_baseline")
    cumulative = joined.filter(pl.col("group") == "cumulative").sort("rung")
    worst = individual.row(0, named=True) if not individual.is_empty() else None
    full = joined.filter(pl.col("rung") == "15_full_live_exit_lifecycle")
    full_row = full.row(0, named=True) if full.height == 1 else None
    return {
        "baseline_mean_return": float(base["total_return"].mean()),
        "full_live_exit_mean_return": float(full["total_return"].mean()) if not full.is_empty() else None,
        "worst_individual_switch": worst,
        "individual_deltas_vs_baseline": individual.select(
            ["venue", "rung", "total_return", "delta_vs_baseline", "n_trades", "worst_matched_reason_transition"]
        ).to_dicts(),
        "cumulative_path": cumulative.select(
            ["venue", "rung", "total_return", "delta_vs_baseline", "n_trades", "worst_matched_reason_transition"]
        ).to_dicts(),
        "single_venue_full_row_if_only_one_venue": full_row,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-date", default="2023-04-01")
    ap.add_argument("--end-date", required=True)
    ap.add_argument("--venues", nargs="+", default=["bybit", "binance"], choices=["bybit", "binance"])
    ap.add_argument("--out-root", default="backtest-runs/continuous_exit_cause_ablation_2026-06-18")
    ap.add_argument(
        "--scratch-base",
        default=str(FULL_LIVE_ARTIFACT),
        help="Artifact root containing existing scratch_*_full_live_fwd1d_s3 roots; set empty to rebuild.",
    )
    args = ap.parse_args()
    out_root = Path(args.out_root).expanduser()
    if not out_root.is_absolute():
        out_root = REPO / out_root
    out_root.mkdir(parents=True, exist_ok=True)
    scratch_base = Path(args.scratch_base).expanduser() if args.scratch_base else None

    rows: list[dict[str, Any]] = []
    deltas: list[dict[str, Any]] = []
    for venue in args.venues:
        venue_rows, venue_deltas = run_venue(
            venue,
            start_date=args.start_date,
            end_date=args.end_date,
            out_root=out_root,
            scratch_base=scratch_base,
        )
        rows.extend(venue_rows)
        deltas.extend(venue_deltas)

    paths = write_pooled_tables(out_root, rows)
    if deltas:
        pl.DataFrame(deltas).sort(["venue", "rung"]).write_csv(out_root / "matched_trade_delta_table.csv")
        paths["matched_trade_delta_table"] = str(out_root / "matched_trade_delta_table.csv")
    summary = {
        "run_label": "exploratory_diagnostic",
        "purpose": "Split the live continuous exit/lifecycle bundle into atomic switches from the strong shared-max-new baseline.",
        "window": {"start_date": args.start_date, "end_date_exclusive": args.end_date},
        "methodology_timestamps": {
            "decision_ts": "component signal bar close",
            "data_available_ts": "closed-bar features plus causal residual_momentum shift(3)",
            "order_submit_ts": "entry_bar_end_ts_ms = signal_ts_ms + 2h",
            "fill_window": "historical hourly bar high/low/close model",
            "exit_activation_ts": "fixed TP baseline plus one or more daemon lifecycle switches",
        },
        "rungs": EXIT_RUNG_DEFS,
        "venues": args.venues,
        "scratch_base": str(scratch_base) if scratch_base else None,
        "artifact_dependency": str(FULL_LIVE_ARTIFACT),
        "table": rows,
        "primary_diagnosis": primary_diagnosis(rows),
        "paths": paths,
        "known_omissions": [
            "Sniper PostOnly add-on is not replayed.",
            "Ticker protective exits are approximated on hourly bars.",
            "This is an attribution diagnostic, not promotion evidence.",
        ],
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"summary: {out_root / 'summary.json'}", flush=True)
    print(f"exit_cause_table: {paths['exit_cause_table']}", flush=True)
    print(f"pooled_exit_cause_table: {paths['pooled_exit_cause_table']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
