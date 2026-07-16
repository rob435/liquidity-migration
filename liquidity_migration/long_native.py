"""Registered FC-v11a long-only strategy and historical runner."""

from __future__ import annotations

import datetime as dt
import json
import math
from bisect import bisect_right
from datetime import date as dt_date
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import (
    MS_PER_HOUR,
    calendar_roll,
    calendar_shift,
    finite_float,
    date_ms,
    exact_duration_ms,
    is_weekend_ms,
    pct,
)
from .account_kernel import AccountRiskPolicy, verify_account_journal
from .account_service import SleeveAdapterKind
from .config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .momentum_signals import daily_bars, add_returns_and_age
from .run_diagnostics import diagnose, is_tainted, render
from .storage import read_dataset, read_dataset_columns
from .trade_lifecycle import (
    _funding_lookup,
    _perp_funding_return,
    build_equity_curve,
    derive_funding_interval_min,
    summarize_baskets,
    summarize_trade_backtest,
)
from .execution_adapters import ExecutionTwinConfig, LatencyProfile
from .historical_account_replay import (
    HistoricalAccountSession,
    HistoricalTargetDecision,
    historical_submission_feedback,
    neutralize_historical_decisions,
    synthetic_historical_rules_for_symbols,
)
from .long_identity import LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID, long_trade_id
from .strategy_targets import component_target_intent
from ._common import _date_range, _exclude_symbols, _iso_date, _iso_month
from .volume_events_charts import _write_equity_benchmark_chart
from .volume_events_pit import (
    _covered_kline_date_symbol_set,
    _full_pit_universe_pass,
    _pit_manifest_metadata,
)

LONG_HISTORICAL_KERNEL_EQUITY_USDT = 1_000_000.0


@dataclass(frozen=True, slots=True)
class LongNativeConfig:
    """The sole supported LONG strategy: FC-v11a div/weekend/vol.

    This is deliberately not a research parameter surface. Fields remain only
    when the active demo/paper runtime or standard equity runner consumes them.
    """

    execution_strategy_id: str = LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
    execution_leverage: float = 10.0
    start_date: str = ""
    end_date: str = ""

    universe_size: int = 50
    universe_volume_window_days: int = 90
    min_listing_history_days: int = 30
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS
    regime_symbol: str = "BTCUSDT"
    regime_sma_days: int = 30

    fc_min_day_return: float = 0.15
    fc_top_volume_rank_max: int = 10
    fc_min_close_location: float = 0.70
    fc_max_hold_days: int = 3
    fc_max_atr_pct: float = 0.12
    fc_atr_stop_mult: float = 1.5
    fc_atr_tp_mult: float = 4.0
    fc_sigma_mult: float = 2.5
    fc_sniper_retrace_pct: float = 0.01
    fc_sniper_deadline_hours: int = 6
    weekend_size_mult: float = 1.5
    fc_close_loc_multi_day: float = 0.6

    max_concurrent_positions: int = 10
    cooldown_days: int = 7
    entry_delay_hours: int = 1
    gross_exposure: float = 1.0
    notional_multiplier: float = 1.0
    vol_estimate_window_days: int = 30
    vol_floor_annual: float = 0.30
    max_position_weight: float = 0.30
    vol_target_annual: float = 0.60
    vol_target_min_scale: float = 0.30
    vol_target_max_scale: float = 1.25
    cost_multiplier: float = 3.0


def long_v11a_profile() -> LongNativeConfig:
    """Return the single registered LONG strategy profile."""

    return LongNativeConfig()


def _vol_target_scale(config: "LongNativeConfig", btc_rv: float | None) -> float:
    """Active v11a BTC-vol book scalar, shared by equity and runtime."""

    rv = btc_rv or config.vol_target_annual  # None/0.0 -> target (scale 1.0); mirrors backtest
    vt = config.vol_target_annual / max(rv, 1e-6)
    return max(config.vol_target_min_scale, min(config.vol_target_max_scale, vt))


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

    klines = _exclude_symbols(raw_klines, cfg.exclude_symbols)
    funding = _exclude_symbols(funding, cfg.exclude_symbols)
    archive_manifest = _exclude_symbols(archive_manifest, cfg.exclude_symbols)

    pit_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    full_pit_universe_pass = _full_pit_universe_pass(
        klines, archive_manifest, kline_covered_date_symbols=pit_covered_date_symbols
    )

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
            _funding_lookup(funding, interval_by_symbol=derive_funding_interval_min(funding))
            if funding is not None and not funding.is_empty()
            else None
        ),
        "full_pit_universe_pass": full_pit_universe_pass,
        "pit_covered_date_symbols": pit_covered_date_symbols,
    }


def _mtm_daily_curve(trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    """Honest daily mark-to-market book curve.

    The engine books P&L on exit day only, which renders the sparse FC book as a
    step function (2026-06-09 finding). This marks every open trade daily off the
    symbol's daily close (entry day: close vs entry price; exit day: exit price vs
    prior close, costs+funding booked there), so each trade's gross telescopes to
    the same per-trade total. Flat days are zero. Columns: date, mtm_return,
    equity, drawdown.
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
            f"daily MTM (deployment-true): ret {s['mtm_total_return']:+.1%} "
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


def _long_kernel_strategy_id(config: LongNativeConfig, costs: CostConfig) -> str:
    del costs
    strategy_id = config.execution_strategy_id.strip()
    if strategy_id != LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID:
        raise ValueError("LONG execution_strategy_id must be the registered v11a identity")
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

    lifecycle_strategy_id = _long_kernel_strategy_id(cfg, costs)
    lifecycle_root = output_dir / "common_kernel_execution"
    replay_policy = AccountRiskPolicy(
        max_component_gross_notional_usdt=LONG_HISTORICAL_KERNEL_EQUITY_USDT * 10.0,
        max_account_gross_notional_usdt=LONG_HISTORICAL_KERNEL_EQUITY_USDT * 100.0,
        max_symbol_notional_usdt=LONG_HISTORICAL_KERNEL_EQUITY_USDT * 10.0,
        max_initial_margin_usdt=LONG_HISTORICAL_KERNEL_EQUITY_USDT * 100.0,
        max_leverage=max(float(cfg.execution_leverage), 1.0),
    )
    execution_config = ExecutionTwinConfig(
        fee_bps=costs.base_entry_exit_cost_bps * cfg.cost_multiplier / 2.0,
        latency=LatencyProfile(0, 0, 0),
        max_decision_age_ns=0,
    )
    observed_ts_ns = max(int(features["ts_ms"].min() or 0) * 1_000_000, 1)
    online_session = HistoricalAccountSession(
        lifecycle_root,
        account_id=lifecycle_strategy_id,
        risk_policy=replay_policy,
        instrument_rules=synthetic_historical_rules_for_symbols(
            list(bars_by_symbol),
            max_leverage=max(float(cfg.execution_leverage), 1.0),
            observed_ts_ns=observed_ts_ns,
        ),
        execution_config=execution_config,
        id_seed=f"{lifecycle_strategy_id}:historical",
    )
    kernel_decisions: list[HistoricalTargetDecision] = []
    trades, lifecycle_stats, event_counts = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars_by_symbol,
        funding_lookup=funding_lookup,
        config=cfg,
        costs=costs,
        kernel_decision_sink=kernel_decisions,
        kernel_session=online_session,
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
    replay_batches = len(online_session.outputs)
    final_state_hash = online_session.final_state_hash
    evidence_label = "chronological_strategy_targets_through_live_common_account_kernel"
    lifecycle_receipt = verify_account_journal(lifecycle_root)
    lifecycle_receipt.update(
        {
            "batches": replay_batches,
            "strategy_targets": len(kernel_decisions),
            "final_state_hash": final_state_hash,
            "evidence_label": evidence_label,
            "historical_strategy_runtime_is_sequential": True,
            "account_kernel_feedback_online": True,
            "same_timestamp_strategy_batching": True,
            "entry_capacity_evaluated_at_actual_entry_boundary": True,
            "strategy_runtime_shared_across_environments": False,
            "market_tape_shared_across_environments": False,
            "cross_environment_strategy_parity": False,
            "venue_rule_parity": False,
        }
    )

    if not trades.is_empty():
        trades.write_csv(output_dir / "long_native_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "long_native_baskets.csv")
    if not equity.is_empty():
        equity.write_csv(output_dir / "long_native_equity.csv")
    if not monthly.is_empty():
        monthly.write_csv(output_dir / "long_native_monthly.csv")

    # Honest daily mark-to-market rendering (the exit-booked curve reads as a step
    # function on this sparse book; the MTM view is the deployment-true one).
    mtm_metadata: dict[str, Any] = {}
    try:
        mtm = _mtm_daily_curve(trades, klines)
        if not mtm.is_empty():
            mtm.write_csv(output_dir / "long_native_equity_mtm.csv")
            mtm_metadata = _write_mtm_chart(output_dir, mtm)
    except Exception:  # noqa: BLE001 - rendering must not fail the run
        mtm_metadata = {}

    # Equity-vs-BTC PNG gives operators a comparable visual benchmark without a
    # side-step renderer.
    # The long-only sleeve fires sparse FOMO-chase setups so the strategy line is
    # near-flat compared to BTC over the same window — that's the design (low DD,
    # uncorrelated returns). For a purer view of strategy P&L use the CSVs.
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
        requested_end=cfg.end_date or None,
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
        ),
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
        "account_journal": lifecycle_receipt,
    }
    (output_dir / "long_native_research_report.json").write_text(
        json.dumps(metadata, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "long_native_research_report.md").write_text(format_long_native_report(metadata), encoding="utf-8")
    print(render(warnings, title=f"long_native {root.name} {cfg.start_date or '*'}..{cfg.end_date or '*'}"), flush=True)
    return {**metadata, "report_dir": str(output_dir)}


def build_long_features(klines_1h: pl.DataFrame, *, config: LongNativeConfig) -> pl.DataFrame:
    """Build only the features consumed by the registered FC-v11a profile."""

    daily = daily_bars(klines_1h)
    if daily.is_empty():
        return daily
    daily = add_returns_and_age(daily).sort(["symbol", "ts_ms"])
    annualization = math.sqrt(365.0)
    daily = daily.with_columns(
        [
            pl.when((pl.col("high") - pl.col("low")) > 1e-12)
            .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
            .otherwise(0.5)
            .alias("close_location"),
            (
                _cal_roll(
                    pl.col("log_return"),
                    "std",
                    config.vol_estimate_window_days,
                    min_samples=config.vol_estimate_window_days,
                ).over("symbol")
                * annualization
            ).alias("realized_vol"),
            _cal_roll(
                pl.col("turnover_quote"),
                "median",
                config.universe_volume_window_days,
                min_samples=config.universe_volume_window_days,
            )
            .over("symbol")
            .alias("turnover_median_90d"),
            (pl.col("close") / calendar_shift(pl.col("close"), 3)).log().alias("pump_3d_log"),
            (pl.col("close") / calendar_shift(pl.col("close"), 7)).log().alias("pump_7d_log"),
            _cal_roll(pl.col("high"), "max", 3, min_samples=3).over("symbol").alias("high_3d"),
            _cal_roll(pl.col("low"), "min", 3, min_samples=3).over("symbol").alias("low_3d"),
            _cal_roll(pl.col("high"), "max", 7, min_samples=7).over("symbol").alias("high_7d"),
            _cal_roll(pl.col("low"), "min", 7, min_samples=7).over("symbol").alias("low_7d"),
            pl.max_horizontal(
                [
                    pl.col("high") - pl.col("low"),
                    (pl.col("high") - calendar_shift(pl.col("close"), 1)).abs(),
                    (pl.col("low") - calendar_shift(pl.col("close"), 1)).abs(),
                ]
            ).alias("true_range"),
        ]
    )
    daily = daily.with_columns(
        [
            (pl.col("realized_vol") / math.sqrt(365.0)).alias("sigma_daily_30d"),
            pl.when((pl.col("high_3d") - pl.col("low_3d")) > 1e-12)
            .then((pl.col("close") - pl.col("low_3d")) / (pl.col("high_3d") - pl.col("low_3d")))
            .otherwise(0.5)
            .alias("close_loc_3d"),
            pl.when((pl.col("high_7d") - pl.col("low_7d")) > 1e-12)
            .then((pl.col("close") - pl.col("low_7d")) / (pl.col("high_7d") - pl.col("low_7d")))
            .otherwise(0.5)
            .alias("close_loc_7d"),
            _cal_roll(pl.col("true_range"), "mean", 14, min_samples=7).over("symbol").alias("atr_14d"),
        ]
    ).with_columns((pl.col("atr_14d") / pl.col("close")).alias("atr_14d_pct"))

    daily = daily.with_columns(
        pl.col("turnover_quote").rank(method="ordinal", descending=True).over("ts_ms").alias("today_volume_rank")
    )
    daily = daily.with_columns(
        pl.col("turnover_median_90d").rank(method="ordinal", descending=True).over("ts_ms").alias("universe_rank")
    ).with_columns(
        (
            (pl.col("universe_rank") <= config.universe_size)
            & (pl.col("symbol_age_days") >= config.min_listing_history_days)
            & pl.col("turnover_median_90d").is_finite()
        ).alias("in_universe")
    )

    btc = daily.filter(pl.col("symbol") == config.regime_symbol).sort("ts_ms")
    if not btc.is_empty():
        btc = (
            btc.with_columns(
                [
                    _cal_roll(
                        pl.col("close"),
                        "mean",
                        config.regime_sma_days,
                        min_samples=config.regime_sma_days,
                    ).alias("regime_sma"),
                    (_cal_roll(pl.col("log_return"), "std", 30, min_samples=20) * math.sqrt(365.0)).alias("btc_rv_30"),
                ]
            )
            .with_columns((pl.col("close") > pl.col("regime_sma")).alias("regime_on"))
            .select(["ts_ms", "regime_on", "btc_rv_30"])
        )
        daily = daily.join(btc, on="ts_ms", how="left").with_columns(
            [
                pl.col("regime_on").fill_null(False),
                pl.col("btc_rv_30").fill_null(0.8),
            ]
        )
    else:
        daily = daily.with_columns(
            [
                pl.lit(False).alias("regime_on"),
                pl.lit(0.8, dtype=pl.Float64).alias("btc_rv_30"),
            ]
        )

    eth = daily.filter(pl.col("symbol") == "ETHUSDT").sort("ts_ms")
    if not eth.is_empty():
        eth = (
            eth.with_columns(
                _cal_roll(
                    pl.col("close"),
                    "mean",
                    config.regime_sma_days,
                    min_samples=config.regime_sma_days,
                ).alias("eth_sma")
            )
            .with_columns((pl.col("close") > pl.col("eth_sma")).alias("eth_regime_on"))
            .select(["ts_ms", "eth_regime_on"])
        )
        daily = daily.join(eth, on="ts_ms", how="left").with_columns(pl.col("eth_regime_on").fill_null(False))
    else:
        daily = daily.with_columns(pl.lit(False).alias("eth_regime_on"))

    return daily.sort(["ts_ms", "symbol"])


def detect_pattern_fomo_chase(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Return whether a closed daily row satisfies the active FC-v11a signal."""

    if not row.get("in_universe") or not row.get("regime_on") or not row.get("eth_regime_on"):
        return False
    today_rank = _safe_float(row.get("today_volume_rank"))
    if today_rank is None or today_rank > cfg.fc_top_volume_rank_max:
        return False
    today_ret = _safe_float(row.get("log_return"))
    if today_ret is None:
        return False
    sigma_d = _safe_float(row.get("sigma_daily_30d"))
    threshold_1d = (
        cfg.fc_sigma_mult * sigma_d if sigma_d is not None and sigma_d > 0.0 else math.log1p(cfg.fc_min_day_return)
    )
    close_location = _safe_float(row.get("close_location"))
    trigger_1d = (
        today_ret >= threshold_1d and close_location is not None and close_location >= cfg.fc_min_close_location
    )

    trigger_3d = False
    pump_3d = _safe_float(row.get("pump_3d_log"))
    close_loc_3d = _safe_float(row.get("close_loc_3d"))
    if pump_3d is not None and close_loc_3d is not None and close_loc_3d >= cfg.fc_close_loc_multi_day:
        threshold_3d = (
            cfg.fc_sigma_mult * sigma_d * math.sqrt(3)
            if sigma_d is not None and sigma_d > 0.0
            else math.log1p(cfg.fc_min_day_return) * math.sqrt(3)
        )
        trigger_3d = pump_3d >= threshold_3d

    trigger_7d = False
    pump_7d = _safe_float(row.get("pump_7d_log"))
    close_loc_7d = _safe_float(row.get("close_loc_7d"))
    if pump_7d is not None and close_loc_7d is not None and close_loc_7d >= cfg.fc_close_loc_multi_day:
        threshold_7d = (
            cfg.fc_sigma_mult * sigma_d * math.sqrt(7)
            if sigma_d is not None and sigma_d > 0.0
            else math.log1p(cfg.fc_min_day_return) * math.sqrt(7)
        )
        trigger_7d = pump_7d >= threshold_7d

    if not (trigger_1d or trigger_3d or trigger_7d):
        return False
    atr_pct = _safe_float(row.get("atr_14d_pct"))
    return atr_pct is not None and 0.0 < atr_pct <= cfg.fc_max_atr_pct


def _fc_exit_params(row: dict[str, Any], cfg: LongNativeConfig) -> tuple[float, float]:
    atr_pct = _safe_float(row.get("atr_14d_pct"))
    if atr_pct is None or atr_pct <= 0.0:
        raise ValueError("active FC-v11a entry requires positive atr_14d_pct")
    return atr_pct * cfg.fc_atr_stop_mult, atr_pct * cfg.fc_atr_tp_mult


def _classify_entry(row: dict[str, Any], cfg: LongNativeConfig) -> tuple[str | None, float, float, int]:
    if not detect_pattern_fomo_chase(row, cfg):
        return None, 0.0, 0.0, 0
    stop_pct, take_profit_pct = _fc_exit_params(row, cfg)
    return "fomo_chase", stop_pct, take_profit_pct, cfg.fc_max_hold_days


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


def _run_long_pipeline(
    *,
    features: pl.DataFrame,
    bars_by_symbol: dict[str, dict[str, Any]],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: LongNativeConfig,
    costs: CostConfig,
    kernel_session: HistoricalAccountSession,
    kernel_decision_sink: list[HistoricalTargetDecision] | None = None,
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
        "skipped_account_kernel": 0,
    }
    event_counts = {"fomo_chase": 0}
    round_trip_cost_bps = costs.base_entry_exit_cost_bps * config.cost_multiplier
    notional_weight = config.gross_exposure / max(config.max_concurrent_positions, 1)
    if not math.isfinite(config.execution_leverage) or config.execution_leverage <= 0.0:
        raise ValueError("long-native execution_leverage must be finite and positive")
    window_end_ts_ms = date_ms(config.end_date) if config.end_date else None
    kernel_strategy_id = _long_kernel_strategy_id(config, costs)

    def _target_component_id(pos: dict[str, Any]) -> str:
        return long_trade_id(
            symbol=str(pos["symbol"]),
            signal_ts_ms=int(pos["entry_signal_ts_ms"]),
        )

    def _entry_target(pos: dict[str, Any]) -> HistoricalTargetDecision:
        trade_id = _target_component_id(pos)
        entry_ts_ms = int(pos["entry_ts_ms"])
        target_notional = (
            LONG_HISTORICAL_KERNEL_EQUITY_USDT
            * notional_weight
            * float(pos["position_weight"])
            * config.notional_multiplier
        )
        return HistoricalTargetDecision(
            wall_ts_ns=entry_ts_ms * 1_000_000,
            reference_price=float(pos["entry_price"]),
            intent=component_target_intent(
                adapter_kind=SleeveAdapterKind.LONG,
                action="entry",
                decision_ts_ms=entry_ts_ms,
                strategy_id=kernel_strategy_id,
                component_id=trade_id,
                symbol=str(pos["symbol"]),
                signed_notional_usdt=target_notional,
                leverage=max(float(config.execution_leverage), 1.0),
                reason="fomo_chase",
                metadata={
                    "source": "long_native_in_engine_target",
                    "signal_ts_ms": int(pos["entry_signal_ts_ms"]),
                    "signal_valid_until_ms": entry_ts_ms + MS_PER_HOUR,
                    "stop_loss_pct": float(pos["stop_pct"]),
                    "take_profit_pct": float(pos["tp_pct"]),
                    "max_hold_duration_ms": int(pos["planned_exit_ts_ms"]) - entry_ts_ms,
                    "position_weight": float(pos["position_weight"]),
                },
            ),
        )

    def _market_prices(through_ts_ms: int) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol, pos in open_positions.items():
            bars = bars_by_symbol.get(symbol)
            price = float(pos["entry_price"])
            if bars is not None:
                index = bisect_right(bars["ends"], int(through_ts_ms)) - 1
                if index >= 0:
                    candidate = float(bars["close"][index])
                    if math.isfinite(candidate) and candidate > 0.0:
                        price = candidate
            prices[symbol] = price
        return prices

    def _submit_kernel_decisions(
        decisions: list[HistoricalTargetDecision],
        *,
        batch_prefix: str,
    ) -> tuple[bool, tuple[str, ...], bool]:
        if not decisions:
            return True, (), False
        if kernel_decision_sink is not None:
            kernel_decision_sink.extend(decisions)
        outputs = kernel_session.submit_decisions(
            decisions,
            equity_usdt=LONG_HISTORICAL_KERNEL_EQUITY_USDT,
            batch_prefix=batch_prefix,
            market_prices=_market_prices(max(decision.wall_ts_ns for decision in decisions) // 1_000_000),
        )
        feedback = historical_submission_feedback(outputs)
        return feedback.accepted, feedback.rejection_keys, feedback.target_committed

    def _neutralize_rejected_entries(decisions: list[HistoricalTargetDecision]) -> None:
        cancellations = neutralize_historical_decisions(
            decisions,
            reason="entry_execution_rejected",
            source="long_native_execution_rejection_compensation",
        )
        accepted, rejection_keys, _ = _submit_kernel_decisions(
            list(cancellations),
            batch_prefix="long-native-entry-compensation",
        )
        if not accepted:
            raise RuntimeError(
                "historical LONG account kernel could not neutralize rejected entries: " + ", ".join(rejection_keys)
            )

    pending_exits: list[tuple[dict[str, Any], HistoricalTargetDecision]] = []

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
        trade_id = _target_component_id(pos)
        decision = HistoricalTargetDecision(
            wall_ts_ns=int(exit_ts_ms) * 1_000_000,
            reference_price=float(exit_price),
            intent=component_target_intent(
                adapter_kind=SleeveAdapterKind.LONG,
                action="exit",
                decision_ts_ms=exit_ts_ms,
                strategy_id=kernel_strategy_id,
                component_id=trade_id,
                symbol=str(pos["symbol"]),
                signed_notional_usdt=0.0,
                leverage=max(float(config.execution_leverage), 1.0),
                reason=reason,
                metadata={
                    "source": "long_native_in_engine_target",
                    "owner_sleeve": "long",
                    "prior_trade_id": trade_id,
                },
            ),
        )
        pending_exits.append((trade, decision))
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
        grouped: dict[int, list[tuple[dict[str, Any], HistoricalTargetDecision]]] = {}
        for trade, decision in pending_exits:
            grouped.setdefault(decision.wall_ts_ns, []).append((trade, decision))
        for items in (grouped[key] for key in sorted(grouped)):
            ordered = sorted(
                items,
                key=lambda item: (
                    item[1].intent.intent.symbol,
                    item[1].intent.intent.target_key,
                ),
            )
            accepted, rejection_keys, _ = _submit_kernel_decisions(
                [decision for _, decision in ordered],
                batch_prefix="long-native-exit",
            )
            if not accepted:
                raise RuntimeError(
                    "historical LONG account kernel rejected a strategy exit batch: " + ", ".join(rejection_keys)
                )
            trade_rows.extend(trade for trade, _ in ordered)
        pending_exits.clear()

    def _scan_all_positions(through_ts: int) -> None:
        for symbol in list(open_positions):
            if _scan_position_exit(symbol, open_positions[symbol], through_ts):
                del open_positions[symbol]
        _flush_exits()

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
        pending_entries: list[tuple[str, dict[str, Any], HistoricalTargetDecision]] = []
        for row, stop_pct, take_profit_pct, hold_days in candidates:
            symbol = str(row["symbol"])
            entry_ts_ms = ts + exact_duration_ms(hours=config.entry_delay_hours)
            if window_end_ts_ms is not None and entry_ts_ms > window_end_ts_ms:
                stats["skipped_no_entry_bar"] += 1
                continue
            bars = bars_by_symbol.get(symbol)
            entry_idx = bars["by_end"].get(entry_ts_ms) if bars else None
            if entry_idx is None or bars is None:
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
            fired = False
            for hour in range(first_hour, deadline_hour + 1):
                candidate_ts = ts + exact_duration_ms(hours=hour)
                candidate_idx = bars["by_end"].get(candidate_ts)
                if candidate_idx is None:
                    continue
                if float(bars["close"][candidate_idx]) <= retrace_threshold:
                    entry_ts_ms = candidate_ts
                    entry_idx = candidate_idx
                    fired = True
                    break
            if not fired:
                deadline_ts = ts + exact_duration_ms(hours=deadline_hour)
                if window_end_ts_ms is not None and deadline_ts > window_end_ts_ms:
                    stats["skipped_no_entry_bar"] += 1
                    continue
                deadline_idx = bars["by_end"].get(deadline_ts)
                if deadline_idx is None:
                    stats["skipped_no_entry_bar"] += 1
                    continue
                entry_ts_ms = deadline_ts
                entry_idx = deadline_idx
            if window_end_ts_ms is not None and entry_ts_ms > window_end_ts_ms:
                stats["skipped_no_entry_bar"] += 1
                continue
            entry_price = float(bars["close"][entry_idx])
            if not math.isfinite(entry_price) or entry_price <= 0.0:
                stats["skipped_no_entry_bar"] += 1
                continue

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
                "max_hold_days": int(hold_days),
                "basket_id": f"native-{_iso_date(int(ts))}-{symbol}",
            }
            pending_entries.append((symbol, position, _entry_target(position)))

        grouped_entries: dict[int, list[tuple[str, dict[str, Any], HistoricalTargetDecision]]] = {}
        for item in pending_entries:
            grouped_entries.setdefault(item[2].wall_ts_ns, []).append(item)
        for wall_ts_ns, items in sorted(grouped_entries.items()):
            entry_ts_ms = int(wall_ts_ns // 1_000_000)
            _scan_all_positions(entry_ts_ms)
            admitted: list[tuple[str, dict[str, Any], HistoricalTargetDecision]] = []
            for symbol, position, decision in items:
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
                admitted.append((symbol, position, decision))
            if not admitted:
                continue
            decisions = [item[2] for item in admitted]
            accepted, _rejection_keys, target_committed = _submit_kernel_decisions(
                decisions,
                batch_prefix="long-native-entry",
            )
            if accepted:
                continue
            if target_committed:
                _neutralize_rejected_entries(decisions)
            stats["skipped_account_kernel"] += len(admitted)
            for symbol, position, _ in admitted:
                if open_positions.get(symbol) is position:
                    del open_positions[symbol]

    if open_positions:
        for symbol, pos in list(open_positions.items()):
            bars = bars_by_symbol.get(symbol)
            if bars is None or len(bars["close"]) == 0:
                continue
            force_close_through_ts = (
                window_end_ts_ms if window_end_ts_ms is not None else int(bars["bar_end_ts_ms"][-1])
            )
            if _scan_position_exit(symbol, pos, int(force_close_through_ts)):
                del open_positions[symbol]
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
            stats["exits_time"] += 1
        _flush_exits()

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
        # The long sleeve does not track intra-hold price path, so MAE/MFE are
        # NOT measured here. Emit NaN (not 0.0 — a fabricated zero reads as "no
        # adverse excursion ever" and silently zeroes the H2 intra-hold MAE
        # diagnostic; the consumer drops non-finite values as not-measured).
        "net_return": net_return,
        "mae": float("nan"),
        "mfe": float("nan"),
        "bars_held": int(round((int(exit_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR)),
        "hold_hours": (int(exit_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR,
        "actual_entry_delay_hours": (int(pos["entry_ts_ms"]) - int(pos["entry_signal_ts_ms"])) / MS_PER_HOUR,
        "pattern": pos["pattern"],
    }


def _empty_trades() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_id": pl.Series([], dtype=pl.String),
            "basket_id": pl.Series([], dtype=pl.String),
            "entry_signal_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_ts_ms": pl.Series([], dtype=pl.Int64),
            "exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_date": pl.Series([], dtype=pl.String),
            "exit_date": pl.Series([], dtype=pl.String),
            "exit_month": pl.Series([], dtype=pl.String),
            "symbol": pl.Series([], dtype=pl.String),
            "side": pl.Series([], dtype=pl.String),
            "score": pl.Series([], dtype=pl.Float64),
            "rank": pl.Series([], dtype=pl.Int64),
            "entry_price": pl.Series([], dtype=pl.Float64),
            "exit_price": pl.Series([], dtype=pl.Float64),
            "exit_reason": pl.Series([], dtype=pl.String),
            "planned_exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "stop_price": pl.Series([], dtype=pl.Float64),
            "take_profit_price": pl.Series([], dtype=pl.Float64),
            "notional_weight": pl.Series([], dtype=pl.Float64),
            "position_weight": pl.Series([], dtype=pl.Float64),
            "gross_trade_return": pl.Series([], dtype=pl.Float64),
            "gross_return": pl.Series([], dtype=pl.Float64),
            "cost_return": pl.Series([], dtype=pl.Float64),
            "funding_return": pl.Series([], dtype=pl.Float64),
            "funding_mode": pl.Series([], dtype=pl.String),
            "funding_event_count": pl.Series([], dtype=pl.Int64),
            "net_return": pl.Series([], dtype=pl.Float64),
            "mae": pl.Series([], dtype=pl.Float64),
            "mfe": pl.Series([], dtype=pl.Float64),
            "bars_held": pl.Series([], dtype=pl.Int64),
            "hold_hours": pl.Series([], dtype=pl.Float64),
            "actual_entry_delay_hours": pl.Series([], dtype=pl.Float64),
            "pattern": pl.Series([], dtype=pl.String),
        }
    )


# A coarse ``partial`` label can hide material unmodeled funding. Down-label a
# report when less than this share of traded notional has complete funding.
FUNDING_MODELED_FRACTION_THRESHOLD = 0.95


def _cal_roll(
    expr: pl.Expr,
    agg: str,
    n_days: int,
    *,
    shifted: bool = False,
    min_samples: int | None = None,
    **kwargs: Any,
) -> pl.Expr:
    return calendar_roll(
        expr,
        agg,
        n_days,
        shifted=shifted,
        min_samples=n_days if min_samples is None else min_samples,
        **kwargs,
    )


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


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


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
