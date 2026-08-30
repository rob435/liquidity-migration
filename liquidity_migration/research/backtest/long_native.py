"""Historical runner and report writer for the registered LONG rule.

The rule itself — profiles, features, signal — lives in
``liquidity_migration/rules/long_native.py``; this module replays its
chronological lifecycle and renders the equity evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from bisect import bisect_right
from datetime import date as dt_date
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.core._common import (
    MS_PER_HOUR,
    _date_range,
    _exclude_symbols,
    _iso_date,
    _iso_month,
    date_ms,
    exact_duration_ms,
    finite_float,
    is_weekend_ms,
    pct,
)
from liquidity_migration.core.config import CostConfig, TradeLifecycleConfig
from liquidity_migration.rules.long_native import (
    LongNativeConfig,
    _classify_entry,
    _safe_float,
    _vol_target_scale,
    build_long_features,
    long_v11a_profile,
)
from liquidity_migration.research.backtest.run_diagnostics import diagnose, is_tainted, render
from liquidity_migration.data.storage import read_dataset, read_dataset_columns
from liquidity_migration.data.trade_lifecycle import (
    _empty_trades as _lifecycle_empty_trades,
    _funding_lookup,
    _perp_funding_return,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
from liquidity_migration.rules.long_identity import SUPPORTED_LONG_STRATEGY_IDS
from liquidity_migration.research.backtest.volume_events_charts import _write_equity_benchmark_chart
from liquidity_migration.data.volume_events_pit import (
    _covered_kline_date_symbol_set,
    _full_pit_universe_coverage,
    _pit_manifest_metadata,
    filter_klines_to_pit_membership,
)

def _diagnostic_data_end(exclusive_end: str) -> str | None:
    """Translate the strategy's exclusive end into an inclusive data date."""

    if not exclusive_end:
        return None
    return (
        dt.date.fromisoformat(exclusive_end) - dt.timedelta(days=1)
    ).isoformat()


def build_long_research_inputs(data_root: str | Path, *, config: LongNativeConfig | None = None) -> dict[str, Any]:
    """Build the active FC-v11a feature, bar, PIT, and funding inputs."""

    cfg = config or long_v11a_profile()
    root = Path(data_root).expanduser()
    raw_klines = read_dataset_columns(
        root,
        "klines_1h",
        columns=[
            "ts_ms",
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "turnover_quote",
            "volume_base",
        ],
    )
    if raw_klines.is_empty():
        raise RuntimeError("klines_1h is empty; run download-data first")
    funding = read_dataset(root, "funding")
    archive_manifest = read_dataset(root, "archive_trade_manifest")

    coverage_klines = _exclude_symbols(raw_klines, cfg.exclude_symbols)
    funding = _exclude_symbols(funding, cfg.exclude_symbols)
    archive_manifest = _exclude_symbols(archive_manifest, cfg.exclude_symbols)

    pit_covered_date_symbols = _covered_kline_date_symbol_set(coverage_klines)
    pit_coverage = _full_pit_universe_coverage(
        coverage_klines,
        archive_manifest,
        kline_covered_date_symbols=pit_covered_date_symbols,
    )
    full_pit_universe_pass = pit_coverage.passed
    klines, pit_filter_receipt = filter_klines_to_pit_membership(
        coverage_klines,
        archive_manifest,
    )
    if klines.is_empty():
        raise RuntimeError("No PIT-member klines remain after archive manifest filtering")

    features = build_long_features(klines, config=cfg)
    features = _filter_signal_window(features, start=cfg.start_date, end=cfg.end_date)
    if features.is_empty():
        raise RuntimeError("No features generated")

    return {
        "raw_klines": raw_klines,
        "klines": klines,
        "archive_manifest": archive_manifest,
        "features": features,
        "bars_by_symbol": _bars_by_symbol(klines),
        "funding_lookup": (
            _funding_lookup(funding)
            if funding is not None and not funding.is_empty()
            else None
        ),
        "full_pit_universe_pass": full_pit_universe_pass,
        "pit_covered_date_symbols": pit_covered_date_symbols,
        "pit_required_date_symbols": pit_coverage.required_date_symbols,
        "pit_filter_receipt": pit_filter_receipt,
    }


def _mtm_daily_curve(trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    """Honest daily mark-to-market book curve.

    The engine books P&L on exit day only, which renders the sparse FC book as a
    step function. This marks every open trade daily off the symbol's daily close
    (entry day: close vs entry price; exit day: exit price vs prior close, with
    costs and funding booked there). Flat days are zero. Columns: date,
    mtm_return, equity, drawdown.

    The daily marks do NOT telescope to the booked per-trade total once
    ``notional_weight < 1``: the contributions are arithmetic returns scaled by
    the weight, so they sum to the per-trade total only to O(nw*r^2), which is
    material at 15-20% daily moves. This is the daily book path, not a per-trade
    reconciliation; distributing log-returns would tie the two out.
    """
    if trades.is_empty():
        return pl.DataFrame(
            {
                "date": pl.Series([], dtype=pl.Date),
                "mtm_return": pl.Series([], dtype=pl.Float64),
                "equity": pl.Series([], dtype=pl.Float64),
                "drawdown": pl.Series([], dtype=pl.Float64),
            }
        )
    daily_close = klines.sort("ts_ms").group_by(["symbol", "date"], maintain_order=True).agg(pl.col("close").last())
    closes: dict[str, dict[str, float]] = {}
    for sym, date_s, close in daily_close.iter_rows():
        closes.setdefault(sym, {})[str(date_s)] = float(close)

    by_day: dict[dt_date, float] = {}
    min_d: dt_date | None = None
    max_d: dt_date | None = None
    for row in trades.to_dicts():
        sym = str(row["symbol"])
        nw = float(row.get("notional_weight") or 0.0)
        entry_px = float(row.get("entry_price") or 0.0)
        exit_px = float(row.get("exit_price") or 0.0)
        costfund = float(row.get("cost_return") or 0.0) + float(row.get("funding_return") or 0.0)
        d0 = dt.datetime.fromtimestamp(int(row["entry_ts_ms"]) / 1000, tz=dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp(int(row["exit_ts_ms"]) / 1000, tz=dt.timezone.utc).date()
        if entry_px <= 0 or exit_px <= 0 or nw == 0.0 or d1 < d0:
            continue
        sym_closes = closes.get(sym, {})
        prev_px = entry_px
        d = d0
        while d <= d1:
            if d == d1:
                px = exit_px
            else:
                c = sym_closes.get(d.isoformat())
                px = c if c is not None and c > 0 else prev_px
            pnl = nw * (px / prev_px - 1.0)
            if d == d1:
                pnl += costfund
            by_day[d] = by_day.get(d, 0.0) + pnl
            prev_px = px
            d += dt.timedelta(days=1)
        min_d = d0 if min_d is None or d0 < min_d else min_d
        max_d = d1 if max_d is None or d1 > max_d else max_d

    if min_d is None or max_d is None:
        return pl.DataFrame(
            {
                "date": pl.Series([], dtype=pl.Date),
                "mtm_return": pl.Series([], dtype=pl.Float64),
                "equity": pl.Series([], dtype=pl.Float64),
                "drawdown": pl.Series([], dtype=pl.Float64),
            }
        )
    days: list[dt_date] = []
    d = min_d
    while d <= max_d:
        days.append(d)
        d += dt.timedelta(days=1)
    rets = [by_day.get(d, 0.0) for d in days]
    eq: list[float] = []
    acc = 1.0
    for r in rets:
        acc *= 1.0 + r
        eq.append(acc)
    peak = 0.0
    dd: list[float] = []
    for e in eq:
        peak = max(peak, e)
        dd.append(e / peak - 1.0)
    return pl.DataFrame({"date": days, "mtm_return": rets, "equity": eq, "drawdown": dd})


def _mtm_summary(mtm: pl.DataFrame) -> dict[str, Any]:
    if mtm.is_empty():
        return {}
    rets = np.asarray(mtm["mtm_return"].to_list())
    eq = np.asarray(mtm["equity"].to_list())
    n_days = len(rets)
    total = float(eq[-1] - 1.0)
    years = n_days / 365.25
    maxdd = min(finite_float(mtm["drawdown"].min(), default=0.0) or 0.0, 0.0)
    std = float(rets.std())
    return {
        "mtm_total_return": round(total, 6),
        "mtm_max_drawdown": round(maxdd, 6),
        "mtm_mar": round((total / years) / abs(maxdd), 3) if maxdd < 0 and years > 0 else None,
        "mtm_daily_sharpe": round(float(rets.mean() / std * math.sqrt(365.25)), 3) if std > 0 else None,
        "mtm_active_day_frac": round(float((rets != 0.0).mean()), 4),
    }


def _write_mtm_chart(
    output_dir: Path, mtm: pl.DataFrame, png_name: str = "long_native_equity_mtm.png"
) -> dict[str, Any]:
    """Write the daily mark-to-market equity and drawdown chart."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def _as_dt(values: list[Any]) -> list[dt.datetime]:
        out = []
        for v in values:
            if isinstance(v, str):
                out.append(dt.datetime.fromisoformat(v[:10]))
            elif isinstance(v, dt.datetime):
                out.append(v)
            elif isinstance(v, dt_date):
                out.append(dt.datetime(v.year, v.month, v.day))
            else:
                out.append(v)
        return out

    s = _mtm_summary(mtm)
    fig, (ax, axd) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    days = _as_dt(mtm["date"].to_list())
    ax.plot(
        days,
        mtm["equity"].to_list(),
        color="#1f77b4",
        lw=1.4,
        label=(
            f"daily MTM view: ret {s['mtm_total_return']:+.1%} "
            f"DD {s['mtm_max_drawdown']:.1%} Sharpe {s['mtm_daily_sharpe']}"
        ),
    )
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.25)
    ax.set_ylabel("equity (x)")
    ax.set_title("long_native — daily mark-to-market")
    axd.fill_between(days, [d * 100 for d in mtm["drawdown"].to_list()], 0.0, color="#2c3e50", alpha=0.55)
    axd.set_ylabel("MTM drawdown (%)")
    axd.grid(alpha=0.25)
    fig.tight_layout()
    out = output_dir / png_name
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return {"mtm_png": str(out), **s}


def _long_equity_chart_metrics(summary: dict[str, Any], equity: pl.DataFrame) -> dict[str, Any]:
    def finite(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    years: float | None = None
    if not equity.is_empty() and "date" in equity.columns:
        dates: list[dt_date] = []
        for raw in equity["date"].to_list():
            try:
                dates.append(dt_date.fromisoformat(str(raw)[:10]))
            except ValueError:
                continue
        if dates:
            years = ((max(dates) - min(dates)).days + 1) / 365.25

    total = finite(summary.get("total_return"))
    max_dd = finite(summary.get("max_drawdown"))
    annualized = None
    if total is not None and years is not None and years > 0.0 and total > -1.0:
        annualized = (1.0 + total) ** (1.0 / years) - 1.0
    mar = None
    if total is not None and years is not None and years > 0.0 and max_dd is not None and abs(max_dd) > 1e-12:
        mar = (total / years) / abs(max_dd)

    metrics: dict[str, Any] = {}
    if total is not None:
        metrics["total_return_pct"] = total * 100.0
    if annualized is not None:
        metrics["annualized_pct"] = annualized * 100.0
    if max_dd is not None:
        metrics["max_drawdown_pct"] = max_dd * 100.0
    if (worst_day := finite(summary.get("worst_day_return"))) is not None:
        metrics["worst_day_pct"] = worst_day * 100.0
    if (sharpe := finite(summary.get("sharpe_like"))) is not None:
        metrics["sharpe_daily_ann"] = sharpe
    if mar is not None:
        metrics["mar"] = mar
    if years is not None:
        metrics["years"] = years
    return metrics


def _long_strategy_id(config: LongNativeConfig) -> str:
    strategy_id = config.execution_strategy_id.strip()
    if strategy_id not in SUPPORTED_LONG_STRATEGY_IDS:
        raise ValueError(
            "LONG execution_strategy_id must be a registered identity: "
            f"{sorted(SUPPORTED_LONG_STRATEGY_IDS)}"
        )
    return strategy_id


def run_long_native_research(
    data_root: str | Path,
    *,
    config: LongNativeConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    cfg = config or long_v11a_profile()
    costs = cost_config or CostConfig()
    root = Path(data_root).expanduser()
    output_dir = Path(report_dir) if report_dir else root / "reports" / "long_native_research"
    output_dir.mkdir(parents=True, exist_ok=True)

    inputs = build_long_research_inputs(root, config=cfg)
    raw_klines = inputs["raw_klines"]
    klines = inputs["klines"]
    archive_manifest = inputs["archive_manifest"]
    features = inputs["features"]
    bars_by_symbol = inputs["bars_by_symbol"]
    funding_lookup = inputs["funding_lookup"]
    full_pit_universe_pass = inputs["full_pit_universe_pass"]
    pit_covered_date_symbols = inputs["pit_covered_date_symbols"]
    pit_required_date_symbols = inputs["pit_required_date_symbols"]
    pit_filter_receipt = inputs["pit_filter_receipt"]

    lifecycle_strategy_id = _long_strategy_id(cfg)
    trades, lifecycle_stats, event_counts = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars_by_symbol,
        funding_lookup=funding_lookup,
        config=cfg,
        costs=costs,
    )

    bt_config = TradeLifecycleConfig(
        score="long_native",
        hold_days=cfg.fc_max_hold_days,
        rebalance_days=7,
        gross_exposure=cfg.gross_exposure,
        entry_delay_hours=cfg.entry_delay_hours,
        cost_multiplier=cfg.cost_multiplier,
        side_mode="long_high_short_low",
    )
    baskets = summarize_baskets(trades, config=bt_config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=bt_config)
    monthly = _monthly_returns(baskets)
    funding_mode = summary.get("funding_mode", "missing")
    execution_evidence = {
        "strategy_id": lifecycle_strategy_id,
        "evidence_label": "chronological_native_strategy_replay",
        "historical_strategy_runtime_is_sequential": True,
        "same_timestamp_strategy_batching": True,
        "entry_capacity_evaluated_at_actual_entry_boundary": True,
        "strategy_runtime_shared_across_environments": False,
        "market_tape_shared_across_environments": False,
        "cross_environment_strategy_parity": False,
        "venue_rule_parity": False,
    }

    if not trades.is_empty():
        trades.write_csv(output_dir / "long_native_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "long_native_baskets.csv")
    if not equity.is_empty():
        equity.write_csv(output_dir / "long_native_equity.csv")
    if not monthly.is_empty():
        monthly.write_csv(output_dir / "long_native_monthly.csv")

    # The exit-booked curve is a step function on this sparse book; this view
    # marks held trades daily without claiming live execution parity.
    mtm_metadata: dict[str, Any] = {}
    try:
        mtm = _mtm_daily_curve(trades, klines)
        if not mtm.is_empty():
            mtm.write_csv(output_dir / "long_native_equity_mtm.csv")
            mtm_metadata = _write_mtm_chart(output_dir, mtm)
    except Exception:  # noqa: BLE001 - rendering must not fail the run
        mtm_metadata = {}

    # Equity-vs-BTC benchmark PNG. The sleeve fires sparse setups, so its line
    # is near-flat against BTC by design; the CSVs show strategy P&L alone.
    chart_metadata: dict[str, Any] = {}
    if not equity.is_empty() and not raw_klines.is_empty():
        try:
            chart_metadata = _write_equity_benchmark_chart(
                output_dir,
                equity=equity,
                raw_klines=raw_klines,
                monthly=monthly if not monthly.is_empty() else None,
                png_name="long_native_equity_btc.png",
                metrics=_long_equity_chart_metrics(summary, equity),
            )
        except Exception:  # noqa: BLE001 - chart failure must not fail the run
            chart_metadata = {}

    data_start = klines["date"].min() if ("date" in klines.columns and klines.height) else None
    data_end = klines["date"].max() if ("date" in klines.columns and klines.height) else None
    warnings = diagnose(
        full_pit_universe_pass=full_pit_universe_pass,
        funding_mode=funding_mode,
        archive_manifest_empty=archive_manifest.is_empty(),
        requested_start=cfg.start_date or None,
        requested_end=_diagnostic_data_end(cfg.end_date),
        data_start=str(data_start) if data_start is not None else None,
        data_end=str(data_end) if data_end is not None else None,
        n_features=features.height,
        n_trades=trades.height,
    )

    metadata = {
        "config": asdict(cfg),
        "rows": {"features": features.height, "trades": trades.height, "baskets": baskets.height},
        "date_range": _date_range(features),
        "pit_manifest": _pit_manifest_metadata(
            archive_manifest,
            features,
            klines,
            full_pit_universe_pass=full_pit_universe_pass,
            kline_covered_date_symbols=pit_covered_date_symbols,
            required_pit_date_symbols=pit_required_date_symbols,
        ),
        "pit_filter": pit_filter_receipt,
        "cost_model": {
            **asdict(costs),
            "base_round_trip_cost_bps": costs.base_entry_exit_cost_bps,
            "cost_multiplier": cfg.cost_multiplier,
            "effective_round_trip_cost_bps": costs.base_entry_exit_cost_bps * cfg.cost_multiplier,
        },
        "summary": summary,
        "lifecycle": lifecycle_stats,
        "event_counts": event_counts,
        "run_label": _run_label(
            full_pit_universe_pass=full_pit_universe_pass,
            funding_mode=funding_mode,
            archive_manifest_empty=archive_manifest.is_empty(),
            funding_modeled_fraction=float(summary.get("funding_modeled_fraction", 1.0)),
        ),
        "methodology_run_label": _methodology_run_label(
            full_pit_universe_pass=full_pit_universe_pass,
            archive_manifest_empty=archive_manifest.is_empty(),
            tainted=is_tainted(warnings),
        ),
        "warnings": [w.as_dict() for w in warnings],
        "tainted": is_tainted(warnings),
        "equity_chart": chart_metadata,
        "equity_mtm": mtm_metadata,
        "execution_evidence": execution_evidence,
    }
    (output_dir / "long_native_research_report.json").write_text(
        json.dumps(metadata, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "long_native_research_report.md").write_text(format_long_native_report(metadata), encoding="utf-8")
    print(render(warnings, title=f"long_native {root.name} {cfg.start_date or '*'}..{cfg.end_date or '*'}"), flush=True)
    return {**metadata, "report_dir": str(output_dir)}


def _bars_by_symbol(klines: pl.DataFrame) -> dict[str, dict[str, Any]]:
    required = {"ts_ms", "symbol", "open", "high", "low", "close"}
    missing = required - set(klines.columns)
    if missing:
        raise RuntimeError(f"klines missing columns: {sorted(missing)}")
    output: dict[str, dict[str, Any]] = {}
    prepared = klines.with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
    for key, part in prepared.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        ends = part["bar_end_ts_ms"].to_numpy()
        output[symbol] = {
            "ends": ends.tolist(),
            "by_end": {int(e): i for i, e in enumerate(ends)},
            "bar_end_ts_ms": ends,
            "open": part["open"].to_numpy(),
            "high": part["high"].to_numpy(),
            "low": part["low"].to_numpy(),
            "close": part["close"].to_numpy(),
        }
    return output


def _filter_signal_window(features: pl.DataFrame, *, start: str, end: str) -> pl.DataFrame:
    if features.is_empty():
        return features
    if start:
        features = features.filter(pl.col("ts_ms") >= date_ms(start))
    if end:
        features = features.filter(pl.col("ts_ms") < date_ms(end))
    return features


def _entry_at_next_hour_open(
    bars: dict[str, Any],
    *,
    observed_bar_idx: int,
    window_end_ts_ms: int | None,
) -> tuple[int, int, float] | None:
    """Enter only after the completed bar that established a retrace or deadline."""

    observation_end_ts_ms = int(bars["bar_end_ts_ms"][observed_bar_idx])
    if window_end_ts_ms is not None and observation_end_ts_ms >= window_end_ts_ms:
        return None
    entry_bar_idx = observed_bar_idx + 1
    if entry_bar_idx >= len(bars["bar_end_ts_ms"]):
        return None
    if int(bars["bar_end_ts_ms"][entry_bar_idx]) != observation_end_ts_ms + MS_PER_HOUR:
        return None
    entry_price = float(bars["open"][entry_bar_idx])
    if not math.isfinite(entry_price) or entry_price <= 0.0:
        return None
    # The next bar opens at the instant the observed bar closes. Keep the
    # observed index so exit scanning includes every bar after entry.
    return observation_end_ts_ms, observed_bar_idx, entry_price


def _run_long_pipeline(
    *,
    features: pl.DataFrame,
    bars_by_symbol: dict[str, dict[str, Any]],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: LongNativeConfig,
    costs: CostConfig,
) -> tuple[pl.DataFrame, dict[str, int], dict[str, int]]:
    dates_all = sorted(int(ts) for ts in features["ts_ms"].unique().to_list())
    features_by_date: dict[int, list[dict[str, Any]]] = {}
    for part in features.partition_by("ts_ms", maintain_order=True):
        features_by_date[int(part["ts_ms"][0])] = part.to_dicts()

    open_positions: dict[str, dict[str, Any]] = {}
    cooldown_until: dict[str, int] = {}
    trade_rows: list[dict[str, Any]] = []
    stats = {
        "candidates_total": 0,
        "skipped_capacity": 0,
        "skipped_cooldown": 0,
        "skipped_already_held": 0,
        "skipped_no_entry_bar": 0,
        "exits_stop": 0,
        "exits_take_profit": 0,
        "exits_time": 0,
    }
    event_counts = {"fomo_chase": 0}
    round_trip_cost_bps = costs.base_entry_exit_cost_bps * config.cost_multiplier
    notional_weight = config.gross_exposure / max(config.max_concurrent_positions, 1)
    if not math.isfinite(config.execution_leverage) or config.execution_leverage <= 0.0:
        raise ValueError("long-native execution_leverage must be finite and positive")
    window_end_ts_ms = date_ms(config.end_date) if config.end_date else None
    pending_exits: list[tuple[dict[str, Any], int, str]] = []

    def _record_exit_target(
        pos: dict[str, Any],
        *,
        exit_ts_ms: int,
        exit_price: float,
        reason: str,
    ) -> dict[str, Any]:
        trade = _finalize_trade(
            pos,
            exit_ts_ms=exit_ts_ms,
            exit_price=exit_price,
            reason=reason,
            notional_weight=notional_weight,
            round_trip_cost_bps=round_trip_cost_bps,
            funding_lookup=funding_lookup,
            notional_multiplier=config.notional_multiplier,
        )
        pending_exits.append((trade, int(exit_ts_ms), str(pos["symbol"])))
        return trade

    def _scan_position_exit(symbol: str, pos: dict[str, Any], through_ts: int) -> bool:
        bars = bars_by_symbol.get(symbol)
        if bars is None or through_ts <= int(pos["entry_ts_ms"]):
            return False
        start_after = max(
            int(pos.get("last_exit_scan_ts_ms", pos["entry_ts_ms"])),
            int(pos["entry_ts_ms"]),
        )
        start_idx = bisect_right(bars["ends"], start_after)
        end_idx = bisect_right(bars["ends"], through_ts)
        for idx in range(max(start_idx, int(pos["entry_bar_idx"]) + 1), end_idx):
            bar_high = float(bars["high"][idx])
            bar_low = float(bars["low"][idx])
            bar_close = float(bars["close"][idx])
            bar_end_ts = int(bars["bar_end_ts_ms"][idx])
            if (
                config.fc_stop_time_decay_hours > 0
                and bar_end_ts - int(pos["entry_ts_ms"])
                >= config.fc_stop_time_decay_hours * MS_PER_HOUR
            ):
                pos["stop_price"] = max(
                    float(pos["stop_price"]),
                    float(pos["entry_price"])
                    * (1.0 - config.fc_stop_time_decay_atr_mult * float(pos["atr_pct"])),
                )
            if bar_low <= pos["stop_price"]:
                _record_exit_target(
                    pos,
                    exit_ts_ms=bar_end_ts,
                    exit_price=pos["stop_price"],
                    reason="stop_loss",
                )
                cooldown_until[symbol] = bar_end_ts + exact_duration_ms(days=config.cooldown_days)
                stats["exits_stop"] += 1
                return True
            if bar_high >= pos["take_profit_price"]:
                _record_exit_target(
                    pos,
                    exit_ts_ms=bar_end_ts,
                    exit_price=pos["take_profit_price"],
                    reason="take_profit",
                )
                cooldown_until[symbol] = bar_end_ts + exact_duration_ms(days=config.cooldown_days)
                stats["exits_take_profit"] += 1
                return True
            if bar_end_ts >= pos["planned_exit_ts_ms"]:
                _record_exit_target(
                    pos,
                    exit_ts_ms=bar_end_ts,
                    exit_price=bar_close,
                    reason="time_stop",
                )
                cooldown_until[symbol] = bar_end_ts + exact_duration_ms(days=config.cooldown_days)
                stats["exits_time"] += 1
                return True
        pos["last_exit_scan_ts_ms"] = through_ts
        return False

    def _flush_exits() -> None:
        if not pending_exits:
            return
        pending_exits.sort(key=lambda item: (item[1], item[2]))
        trade_rows.extend(trade for trade, _, _ in pending_exits)
        pending_exits.clear()

    def _scan_all_positions(through_ts: int) -> None:
        exited: list[str] = []
        for symbol in list(open_positions):
            if _scan_position_exit(symbol, open_positions[symbol], through_ts):
                exited.append(symbol)
        # Keep just-exited positions available as mark sources until the
        # chronological exit groups are recorded: a wide scan can discover
        # exits at several timestamps.
        _flush_exits()
        for symbol in exited:
            del open_positions[symbol]

    for ts in dates_all:
        _scan_all_positions(int(ts))
        candidates: list[tuple[dict[str, Any], float, float, int]] = []
        for row in features_by_date.get(ts, []):
            pattern, stop_pct, take_profit_pct, hold_days = _classify_entry(row, config)
            if pattern is None:
                continue
            stats["candidates_total"] += 1
            event_counts["fomo_chase"] += 1
            candidates.append((row, stop_pct, take_profit_pct, hold_days))

        _scan_all_positions(int(ts + exact_duration_ms(hours=config.entry_delay_hours)))
        candidates.sort(key=lambda candidate: -(_safe_float(candidate[0].get("log_return")) or 0.0))
        pending_entries: list[tuple[str, dict[str, Any]]] = []
        for row, stop_pct, take_profit_pct, hold_days in candidates:
            symbol = str(row["symbol"])
            bars = bars_by_symbol.get(symbol)
            if bars is None:
                stats["skipped_no_entry_bar"] += 1
                continue
            signal_idx = bars["by_end"].get(ts)
            if signal_idx is None:
                stats["skipped_no_entry_bar"] += 1
                continue
            signal_close = float(bars["close"][signal_idx])
            retrace_threshold = signal_close * (1.0 - config.fc_sniper_retrace_pct)
            first_hour = max(1, config.entry_delay_hours)
            deadline_hour = max(first_hour, config.fc_sniper_deadline_hours)
            observed_idx: int | None = None
            for hour in range(first_hour, deadline_hour + 1):
                candidate_ts = ts + exact_duration_ms(hours=hour)
                candidate_idx = bars["by_end"].get(candidate_ts)
                if candidate_idx is None:
                    continue
                if float(bars["low"][candidate_idx]) <= retrace_threshold:
                    observed_idx = int(candidate_idx)
                    break
            if observed_idx is None:
                deadline_ts = ts + exact_duration_ms(hours=deadline_hour)
                deadline_idx = bars["by_end"].get(deadline_ts)
                if deadline_idx is None:
                    stats["skipped_no_entry_bar"] += 1
                    continue
                observed_idx = int(deadline_idx)
            entry = _entry_at_next_hour_open(
                bars,
                observed_bar_idx=observed_idx,
                window_end_ts_ms=window_end_ts_ms,
            )
            if entry is None:
                stats["skipped_no_entry_bar"] += 1
                continue
            entry_ts_ms, entry_idx, entry_price = entry

            vol_estimate = _safe_float(row.get("realized_vol")) or config.vol_floor_annual
            vol_used = max(vol_estimate, config.vol_floor_annual)
            position_weight = min(
                config.vol_floor_annual / vol_used,
                config.max_position_weight / notional_weight,
            )
            position_weight = max(position_weight, 0.25)
            position_weight *= _vol_target_scale(config, _safe_float(row.get("btc_rv_30")))
            if is_weekend_ms(int(entry_ts_ms)):
                position_weight *= config.weekend_size_mult

            planned_exit_ts_ms = entry_ts_ms + exact_duration_ms(days=hold_days)
            position = {
                "symbol": symbol,
                "pattern": "fomo_chase",
                "entry_signal_ts_ms": int(ts),
                "entry_ts_ms": int(entry_ts_ms),
                "last_exit_scan_ts_ms": int(entry_ts_ms),
                "entry_bar_idx": int(entry_idx),
                "entry_price": entry_price,
                "stop_price": entry_price * (1.0 - stop_pct),
                "take_profit_price": entry_price * (1.0 + take_profit_pct),
                "planned_exit_ts_ms": int(planned_exit_ts_ms),
                "position_weight": float(position_weight),
                "stop_pct": float(stop_pct),
                "tp_pct": float(take_profit_pct),
                # kept so a time-decaying stop can be re-derived from ATR rather
                # than from the (possibly range-based) initial stop
                "atr_pct": float(_safe_float(row.get("atr_14d_pct")) or 0.0),
                "max_hold_days": int(hold_days),
                "basket_id": f"native-{_iso_date(int(ts))}-{symbol}",
            }
            pending_entries.append((symbol, position))

        grouped_entries: dict[int, list[tuple[str, dict[str, Any]]]] = {}
        for item in pending_entries:
            grouped_entries.setdefault(int(item[1]["entry_ts_ms"]), []).append(item)
        for entry_ts_ms, items in sorted(grouped_entries.items()):
            _scan_all_positions(entry_ts_ms)
            for symbol, position in items:
                if symbol in open_positions:
                    stats["skipped_already_held"] += 1
                    continue
                if cooldown_until.get(symbol, 0) > entry_ts_ms:
                    stats["skipped_cooldown"] += 1
                    continue
                if len(open_positions) >= config.max_concurrent_positions:
                    stats["skipped_capacity"] += 1
                    continue
                open_positions[symbol] = position

    if open_positions:
        # Same mark-source invariant as `_scan_all_positions`: collect the scan
        # exits, flush every recorded exit once, and only then drop them from
        # `open_positions`.
        force_closed: list[str] = []
        for symbol, pos in list(open_positions.items()):
            bars = bars_by_symbol.get(symbol)
            if bars is None or len(bars["close"]) == 0:
                continue
            force_close_through_ts = (
                window_end_ts_ms if window_end_ts_ms is not None else int(bars["bar_end_ts_ms"][-1])
            )
            if _scan_position_exit(symbol, pos, int(force_close_through_ts)):
                force_closed.append(symbol)
                continue
            exit_idx = bisect_right(bars["ends"], int(force_close_through_ts)) - 1
            if exit_idx < 0 or int(bars["bar_end_ts_ms"][exit_idx]) < int(pos["entry_ts_ms"]):
                continue
            _record_exit_target(
                pos,
                exit_ts_ms=int(bars["bar_end_ts_ms"][exit_idx]),
                exit_price=float(bars["close"][exit_idx]),
                reason="data_end",
            )
            force_closed.append(symbol)
            stats["exits_time"] += 1
        _flush_exits()
        for symbol in force_closed:
            open_positions.pop(symbol, None)

    trades = (
        pl.DataFrame(trade_rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"])
        if trade_rows
        else _empty_trades()
    )
    return trades, stats, event_counts


def _finalize_trade(
    pos,
    *,
    exit_ts_ms,
    exit_price,
    reason,
    notional_weight,
    round_trip_cost_bps,
    funding_lookup,
    notional_multiplier=1.0,
):
    side = "long"
    entry_price = float(pos["entry_price"])
    gross_trade_return = (exit_price / entry_price) - 1.0
    raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
        funding_lookup,
        symbol=pos["symbol"],
        side=side,
        entry_ts_ms=int(pos["entry_ts_ms"]),
        exit_ts_ms=int(exit_ts_ms),
    )
    # H1: scale the per-position gross by the deployed notional multiplier
    # (applied AFTER the B.3 per-symbol cap, matching the live semantics).
    # Default 1.0 leaves the historical backtest gross unchanged.
    effective_weight = abs(notional_weight * float(pos["position_weight"])) * notional_multiplier
    funding_return = effective_weight * raw_funding_return
    cost_return = -effective_weight * round_trip_cost_bps / 10_000.0
    gross_return = effective_weight * gross_trade_return
    net_return = gross_return + cost_return + funding_return
    return {
        "trade_id": f"{pos['basket_id']}-l-{pos['symbol']}",
        "basket_id": pos["basket_id"],
        "entry_signal_ts_ms": int(pos["entry_signal_ts_ms"]),
        "entry_ts_ms": int(pos["entry_ts_ms"]),
        "exit_ts_ms": int(exit_ts_ms),
        "entry_date": _iso_date(int(pos["entry_ts_ms"])),
        "exit_date": _iso_date(int(exit_ts_ms)),
        "exit_month": _iso_month(int(exit_ts_ms)),
        "symbol": pos["symbol"],
        "side": side,
        "score": 0.0,
        "rank": 0,
        "entry_price": entry_price,
        "exit_price": float(exit_price),
        "exit_reason": reason,
        "planned_exit_ts_ms": int(pos["planned_exit_ts_ms"]),
        "stop_price": float(pos["stop_price"]),
        "take_profit_price": float(pos["take_profit_price"]),
        "notional_weight": effective_weight,
        "position_weight": float(pos["position_weight"]),
        "gross_trade_return": gross_trade_return,
        "gross_return": gross_return,
        "cost_return": cost_return,
        "funding_return": funding_return,
        "funding_mode": funding_mode,
        "funding_event_count": int(funding_event_count),
        # No intra-hold price path is tracked, so MAE/MFE are unmeasured. NaN,
        # not 0.0: consumers drop non-finite values, but a zero would read as
        # "no adverse excursion ever".
        "net_return": net_return,
        "mae": float("nan"),
        "mfe": float("nan"),
        "bars_held": int(round((int(exit_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR)),
        "hold_hours": (int(exit_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR,
        "actual_entry_delay_hours": (int(pos["entry_ts_ms"]) - int(pos["entry_signal_ts_ms"])) / MS_PER_HOUR,
        "pattern": pos["pattern"],
    }


def _empty_trades() -> pl.DataFrame:
    """The shared lifecycle trade schema plus the two LONG-only columns."""
    return _lifecycle_empty_trades().with_columns(
        pl.Series("actual_entry_delay_hours", [], dtype=pl.Float64),
        pl.Series("pattern", [], dtype=pl.String),
    )


# A coarse ``partial`` label can hide material unmodeled funding. Down-label a
# report when less than this share of traded notional has complete funding.
FUNDING_MODELED_FRACTION_THRESHOLD = 0.95


def _monthly_returns(baskets: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame(
            {
                "month": pl.Series([], dtype=pl.String),
                "strategy_return": pl.Series([], dtype=pl.Float64),
                "baskets": pl.Series([], dtype=pl.Int64),
            }
        )
    return (
        baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.col("long_return").sum().alias("long_return"),
                pl.col("cost_return").sum().alias("cost_return"),
                pl.col("funding_return").sum().alias("funding_return"),
                pl.len().alias("baskets"),
            ]
        )
        .sort("month")
    )


def _run_label(
    *,
    full_pit_universe_pass: bool,
    funding_mode: str,
    archive_manifest_empty: bool,
    funding_modeled_fraction: float = 1.0,
) -> str:
    if archive_manifest_empty:
        return "pit_required_missing_manifest"
    if not full_pit_universe_pass:
        return "pit_membership_filtered_current_universe"
    if funding_mode == "missing":
        return "full_pit_universe_funding_missing"
    if funding_mode == "partial":
        # Distinguish a small coverage edge from material unmodeled funding.
        if funding_modeled_fraction < FUNDING_MODELED_FRACTION_THRESHOLD:
            return "full_pit_universe_funding_coverage_low"
        return "full_pit_universe_funding_partial"
    return "full_pit_universe"


def _methodology_run_label(
    *,
    full_pit_universe_pass: bool,
    archive_manifest_empty: bool,
    tainted: bool,
) -> str:
    """Conservative backtest-integrity label for raw long-native reports.

    A raw run cannot prove an untouched decision process or operational
    authorization; those claims require separately scoped evidence.
    """
    if tainted:
        return "invalid"
    if archive_manifest_empty or not full_pit_universe_pass:
        return "biased_benchmark"
    return "exploratory"


def format_long_native_report(metadata: dict[str, Any]) -> str:
    cfg = metadata.get("config", {})
    summary = metadata.get("summary", {})
    lifecycle = metadata.get("lifecycle", {})
    event_counts = metadata.get("event_counts", {})
    pit = metadata.get("pit_manifest", {})
    date_range = metadata.get("date_range", {})
    lines = [
        "# Long-Native Long-Only Sleeve",
        "",
        "Registered FC-v11a crypto-native long-only strategy.",
        "",
        "## Inputs",
        f"- Run label: `{metadata.get('methodology_run_label', 'exploratory')}`",
        f"- Data integrity label: `{metadata.get('run_label')}`",
        f"- Date range: {date_range.get('start')} to {date_range.get('end')}",
        f"- Feature rows: {metadata.get('rows', {}).get('features', 0)}",
        f"- Trades: {metadata.get('rows', {}).get('trades', 0)}",
        f"- Universe: top {cfg.get('universe_size')} by {cfg.get('universe_volume_window_days')}d turnover",
        f"- BTC regime SMA: {cfg.get('regime_sma_days')}d",
        "- Setup: fomo_chase",
        f"- Max concurrent: {cfg.get('max_concurrent_positions')}  Cooldown: {cfg.get('cooldown_days')}d",
        f"- Cost multiplier: {cfg.get('cost_multiplier')}x",
        f"- Full PIT: {pit.get('full_pit_universe_pass', False)}",
        "",
        "## Event counts (signals fired before capacity/cooldown gates)",
        f"- fomo_chase: {event_counts.get('fomo_chase', 0)}",
        f"- TOTAL signal-firings: {lifecycle.get('candidates_total', 0)}",
        f"- Skipped: capacity={lifecycle.get('skipped_capacity', 0)}, cooldown={lifecycle.get('skipped_cooldown', 0)}, held={lifecycle.get('skipped_already_held', 0)}, no_entry_bar={lifecycle.get('skipped_no_entry_bar', 0)}",
        "",
        "## Headline metrics",
        f"- Total return: {pct(summary.get('total_return'))}",
        f"- Sharpe-like: {summary.get('sharpe_like', 0.0):.2f}",
        f"- Max drawdown: {pct(summary.get('max_drawdown'))}",
        f"- Max underwater days: {summary.get('max_underwater_days', 0)}",
        f"- Worst 30d/60d/90d/120d: {pct(summary.get('worst_30d_return'))} / {pct(summary.get('worst_60d_return'))} / {pct(summary.get('worst_90d_return'))} / {pct(summary.get('worst_120d_return'))}",
        f"- Trade win rate: {pct(summary.get('trade_win_rate'))}",
        f"- Profit factor: {summary.get('profit_factor', 0.0):.2f}",
        f"- Gross: {pct(summary.get('gross_return'))} | cost: {pct(summary.get('cost_return'))} | funding: {pct(summary.get('funding_return'))} ({summary.get('funding_mode')})",
        "",
        "## Exits",
        f"- stop_loss: {lifecycle.get('exits_stop', 0)}",
        f"- take_profit: {lifecycle.get('exits_take_profit', 0)}",
        f"- time/data_end: {lifecycle.get('exits_time', 0)}",
        "",
    ]
    return "\n".join(lines)
