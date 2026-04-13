from __future__ import annotations

import asyncio
import pickle
import sys
from pathlib import Path

import pytest

import backtest
from backtest import (
    BacktestTrade,
    BacktestVariantRunResult,
    BacktestVariantSummary,
    BacktestVariantSpec,
    HistoricalBacktestSimulator,
    InMemorySignalDatabase,
    MinuteReplayPlan,
    ReplayProgressTracker,
    SimulatedPosition,
    export_variant_run_result,
    fetch_minute_replay_plan,
    format_variant_run_result,
    parse_args,
    _progress_bar,
    _build_stress_variant_specs,
    _resolve_window_universe,
    _safe_variant_worker_count,
    _select_best_variant,
    _build_variant_specs,
    _combine_variant_specs,
    _build_sweep_window_end_times,
    _load_plan_snapshot,
    _utc_day,
    _variant_setting_stability_rows,
    _variant_stability_summary,
    _write_plan_snapshot,
    run_backtest_plan,
    run_comprehensive_backtest_plan,
    run_comprehensive_backtest_variants,
)
from config import Settings
from database import SignalRecord
from exchange import HistoricalCandle, interval_to_milliseconds
from replay import build_replay_plan
from signal_engine import RankedSignal


def test_backtest_plan_generates_closed_trades(tmp_path: Path) -> None:
    asyncio.run(_exercise_backtest_plan(tmp_path))


def test_backtest_intraday_regime_filter_can_block_trades(tmp_path: Path) -> None:
    asyncio.run(_exercise_backtest_filter_toggle(tmp_path))


def test_comprehensive_backtest_allows_unchanged_provisional_minute_closes(tmp_path: Path) -> None:
    asyncio.run(_exercise_comprehensive_backtest_with_unchanged_provisional(tmp_path))


def test_comprehensive_backtest_research_fast_skips_signal_rows(tmp_path: Path) -> None:
    asyncio.run(_exercise_comprehensive_backtest_research_fast(tmp_path))


def test_in_memory_signal_summary_database_builds_report_summary() -> None:
    asyncio.run(_exercise_in_memory_signal_summary_database())


def test_comprehensive_backtest_variants_reuse_one_plan(tmp_path: Path) -> None:
    asyncio.run(_exercise_comprehensive_backtest_variants(tmp_path))


def test_comprehensive_backtest_variants_can_resume_from_checkpoint(tmp_path: Path) -> None:
    asyncio.run(_exercise_comprehensive_backtest_variant_resume(tmp_path))


def test_export_variant_run_result_writes_ranked_outputs(tmp_path: Path) -> None:
    db_path = tmp_path / "variant.sqlite3"
    _seed_trade_analytics_db(db_path)
    result = BacktestVariantRunResult(
        variants=[
            BacktestVariantSummary(
                name="a",
                database_path=str(db_path),
                run_seconds=12.5,
                trade_count=3,
                wins=2,
                losses=1,
                net_pnl_usd=200.0,
                total_return_pct=0.02,
                max_drawdown_pct=0.01,
                profit_factor=1.5,
                entry_ready_signals=5,
                entries_filled=3,
            ),
            BacktestVariantSummary(
                name="b",
                database_path=str(db_path),
                run_seconds=10.0,
                trade_count=2,
                wins=1,
                losses=1,
                net_pnl_usd=50.0,
                total_return_pct=0.005,
                max_drawdown_pct=0.01,
                profit_factor=1.1,
                entry_ready_signals=4,
                entries_filled=2,
            ),
        ],
        best_variant=BacktestVariantSummary(
            name="a",
            database_path=str(db_path),
            run_seconds=12.5,
            trade_count=3,
            wins=2,
            losses=1,
            net_pnl_usd=200.0,
            total_return_pct=0.02,
            max_drawdown_pct=0.01,
            profit_factor=1.5,
            entry_ready_signals=5,
            entries_filled=3,
        ),
        variants_requested=2,
        variants_completed_now=2,
        variants_resumed=0,
        total_elapsed_seconds=22.5,
        avg_variant_seconds=11.25,
    )

    export_variant_run_result(result, export_dir=str(tmp_path / "variant-export"))

    assert (tmp_path / "variant-export" / "variant_summary.csv").exists()
    assert (tmp_path / "variant-export" / "variant_ranked_summary.csv").exists()
    assert (tmp_path / "variant-export" / "variant_best_summary.csv").exists()
    assert (tmp_path / "variant-export" / "best_variant_trades.csv").exists()


def test_format_variant_run_result_includes_elapsed_and_runtime() -> None:
    row = BacktestVariantSummary(
        name="a",
        database_path="a.sqlite3",
        run_seconds=12.5,
        trade_count=3,
        wins=2,
        losses=1,
        net_pnl_usd=200.0,
        total_return_pct=0.02,
        max_drawdown_pct=0.01,
        profit_factor=1.5,
        entry_ready_signals=5,
        entries_filled=3,
    )
    result = BacktestVariantRunResult(
        variants=[row],
        best_variant=row,
        variants_requested=1,
        variants_completed_now=1,
        variants_resumed=0,
        total_elapsed_seconds=12.5,
        avg_variant_seconds=12.5,
    )

    output = format_variant_run_result(result)

    assert "elapsed=12.5s" in output
    assert "avg_variant=12.5s" in output
    assert "runtime=12.5s" in output


def test_safe_variant_worker_count_caps_workers_on_low_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backtest, "_available_memory_bytes", lambda: 5 * 1024 * 1024 * 1024)

    workers, note = _safe_variant_worker_count(
        requested_workers=4,
        pending_variants=6,
        plan_snapshot_bytes=900 * 1024 * 1024,
    )

    assert workers == 1
    assert note is not None
    assert "reducing workers from 4 to 1" in note


def test_safe_variant_worker_count_keeps_workers_when_memory_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backtest, "_available_memory_bytes", lambda: 32 * 1024 * 1024 * 1024)

    workers, note = _safe_variant_worker_count(
        requested_workers=4,
        pending_variants=6,
        plan_snapshot_bytes=300 * 1024 * 1024,
    )

    assert workers == 4
    assert note is None


def test_progress_bar_formats_completion_ratio() -> None:
    assert _progress_bar(completed=0, total=10, width=10) == "[----------]"
    assert _progress_bar(completed=5, total=10, width=10) == "[#####-----]"
    assert _progress_bar(completed=10, total=10, width=10) == "[##########]"


def test_replay_progress_tracker_reports_percent_and_eta(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    monotonic_values = iter([10.0, 16.0])

    monkeypatch.setattr(backtest, "_progress", lambda message: messages.append(message))
    monkeypatch.setattr(backtest.time, "monotonic", lambda: next(monotonic_values))

    tracker = ReplayProgressTracker(
        label="backtest",
        total_bars=100,
        started_at=0.0,
        last_reported_at=0.0,
        report_interval_seconds=5.0,
    )
    tracker.maybe_report(25)

    assert len(messages) == 1
    assert "[backtest] replay" in messages[0]
    assert "25.0%" in messages[0]
    assert "bars=25/100" in messages[0]
    assert "eta=" in messages[0]


def test_plan_snapshot_round_trip_is_compacter_than_raw_pickle(tmp_path: Path) -> None:
    settings = Settings(
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        state_window=24,
        btc_daily_lookback=10,
        btc_vol_lookback=5,
        btcdom_history_lookback=8,
        momentum_lookback=6,
        momentum_skip=1,
        curvature_ma_window=3,
        curvature_signal_window=2,
        hurst_window=24,
        hurst_cutoff=-1.0,
        top_n=1,
        watchlist_top_n=3,
        emerging_top_n=1,
        entry_ready_top_n=1,
        emerging_min_observations=1,
        emerging_min_rank_improvement=0,
        entry_ready_min_observations=1,
        entry_ready_min_rank_improvement=0,
        entry_ready_min_composite_gain=0.0,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
        emerging_cooldown_minutes=0,
        watchlist_cooldown_minutes=0,
        entry_ready_cooldown_minutes=0,
        max_open_positions=1,
        take_profit_pct=0.02,
        stop_loss_pct=0.02,
        risk_per_trade_pct=0.01,
        intraday_regime_filter_enabled=False,
        analytics_enabled=False,
        backtest_intrabar_interval="1",
        backtest_research_fast=True,
    )
    plan = _build_sample_minute_plan(settings, base_ms=97 * interval_to_milliseconds("D"))
    raw_path = tmp_path / "raw-plan.pkl"
    with raw_path.open("wb") as handle:
        pickle.dump(plan, handle, protocol=pickle.HIGHEST_PROTOCOL)
    compact_path = Path(_write_plan_snapshot(plan))
    loaded = _load_plan_snapshot(str(compact_path))
    try:
        assert compact_path.stat().st_size < raw_path.stat().st_size
        assert loaded.confirmed_plan.replay_timestamps == plan.confirmed_plan.replay_timestamps
        assert loaded.confirmed_plan.history_by_symbol == plan.confirmed_plan.history_by_symbol
        assert loaded.btcdom_history == plan.btcdom_history
        assert loaded.active_universe == plan.active_universe
        sample_symbol = settings.tracked_symbols[0]
        sample_bucket = plan.confirmed_plan.replay_timestamps[0]
        original_candles = plan.intrabar_by_symbol[sample_symbol][sample_bucket]
        restored_candles = loaded.intrabar_by_symbol[sample_symbol][sample_bucket]
        assert [c.close_price for c in restored_candles] == [c.close_price for c in original_candles]
    finally:
        compact_path.unlink(missing_ok=True)


def test_post_exit_tracking_populates_trade_follow_through_metrics() -> None:
    settings = Settings(analytics_post_exit_bars=2)
    simulator = HistoricalBacktestSimulator(settings)
    simulator.trades.append(
        BacktestTrade(
            ticker="AAAUSDT",
            side="LONG",
            opened_at="2026-04-11T00:00:00+00:00",
            closed_at="2026-04-11T00:15:00+00:00",
            entry_stage="emerging",
            entry_signal_kind="entry_ready",
            cluster_label="corr:AAAUSDT",
            entry_diagnostics="ref=cluster_relative:corr:AAAUSDT",
            exit_signal_kind="entry_ready",
            exit_rank=1,
            exit_composite_score=1.4,
            exit_momentum_z=2.0,
            exit_curvature=0.2,
            exit_hurst=0.7,
            exit_reason="take_profit",
            quantity=1.0,
            entry_price=99.0,
            exit_price=100.0,
            take_profit_price_at_exit=100.98,
            stop_loss_price_at_exit=97.02,
            profit_protection_adjustments=0,
            notional_usd=99.0,
            gross_pnl_usd=1.0,
            net_pnl_usd=1.0,
            pnl_pct=0.01,
            entry_fee_usd=0.0,
            exit_fee_usd=0.0,
            entry_slippage_usd=0.0,
            exit_slippage_usd=0.0,
            holding_minutes=15.0,
            minutes_to_first_profit_50bps=1.0,
            minutes_to_first_profit_100bps=2.0,
            mfe_pct=0.02,
            mae_pct=0.01,
        )
    )
    simulator._track_post_exit_trade(0, "AAAUSDT", 100.0)

    simulator.update_post_exit_trackers(
        {
            "AAAUSDT": HistoricalCandle(
                start_time_ms=0,
                open_price=100.0,
                high_price=103.0,
                low_price=99.0,
                close_price=101.0,
            )
        }
    )
    assert simulator.trades[0].post_exit_best_pct is None
    assert simulator.trades[0].post_exit_worst_pct is None
    assert simulator.trades[0].volatility_pct is None

    simulator.update_post_exit_trackers(
        {
            "AAAUSDT": HistoricalCandle(
                start_time_ms=60_000,
                open_price=101.0,
                high_price=102.0,
                low_price=98.0,
                close_price=99.0,
            )
        }
    )

    trade = simulator.trades[0]
    assert trade.post_exit_best_pct == pytest.approx(0.03)
    assert trade.post_exit_worst_pct == pytest.approx(-0.02)
    assert trade.volatility_pct is not None
    assert trade.volatility_pct > 0.0
    assert simulator.post_exit_trackers == []


def test_profit_protection_locks_profit_in_simulator() -> None:
    settings = Settings(
        profit_protection_enabled=True,
        profit_protection_trigger_pct=0.015,
        profit_protection_tp_extension_pct=0.01,
        profit_protection_sl_lock_pct=0.005,
        profit_protection_max_adjustments=1,
        profit_protection_max_rank=2,
    )
    simulator = HistoricalBacktestSimulator(settings)
    simulator.positions["AAAUSDT"] = SimulatedPosition(
        ticker="AAAUSDT",
        quantity=1.0,
        entry_price=100.0,
        raw_entry_price=100.0,
        notional_usd=100.0,
        opened_at_ms=0,
        entry_stage="emerging",
        entry_signal_kind="entry_ready",
        cluster_label="manual:layer1",
        entry_diagnostics="ref=cluster_relative:manual:layer1",
        take_profit_price=102.0,
        stop_loss_price=98.0,
        entry_fee_usd=0.0,
        entry_slippage_usd=0.0,
        profit_protection_adjustments=0,
    )

    simulator.adjust_profit_protection(
        ranked_signals=[
            RankedSignal(
                stage="emerging",
                signal_kind="entry_ready",
                ticker="AAAUSDT",
                current_price=101.6,
                momentum_z=2.2,
                curvature=0.2,
                hurst=0.7,
                regime_score=2,
                composite_score=1.5,
                rank=1,
                persistence_hits=1,
                alerted=False,
            )
        ],
        mark_prices={"AAAUSDT": 101.6},
    )

    position = simulator.positions["AAAUSDT"]
    assert position.take_profit_price == pytest.approx(103.0)
    assert position.stop_loss_price == pytest.approx(100.5)
    assert position.profit_protection_adjustments == 1
    assert simulator.profit_protection_adjustments == 1

    closed_tickers = simulator.process_intrabar_exits(
        timestamp_ms=60_000,
        intrabar_candles={
            "AAAUSDT": HistoricalCandle(
                start_time_ms=0,
                open_price=101.2,
                high_price=101.3,
                low_price=100.4,
                close_price=100.4,
            )
        },
        mark_prices={"AAAUSDT": 100.4},
        ranked_signals=[],
    )

    assert closed_tickers == {"AAAUSDT"}
    assert simulator.trades[-1].exit_reason == "protected_profit"


def test_reentry_cooldown_blocks_immediate_reentry_in_simulator() -> None:
    settings = Settings(
        reentry_cooldown_after_profit_minutes=90,
        take_profit_pct=0.02,
        stop_loss_pct=0.02,
    )
    simulator = HistoricalBacktestSimulator(settings)
    simulator.last_profitable_exit_ms_by_ticker["AAAUSDT"] = 0
    simulator.process_entries(
        timestamp_ms=30 * 60_000,
        ranked_signals=[
            RankedSignal(
                stage="emerging",
                signal_kind="entry_ready",
                ticker="AAAUSDT",
                current_price=100.0,
                momentum_z=2.0,
                curvature=0.2,
                hurst=0.7,
                regime_score=2,
                composite_score=1.4,
                rank=1,
                persistence_hits=0,
                alerted=False,
            )
        ],
        mark_prices={"AAAUSDT": 100.0},
    )
    assert "AAAUSDT" not in simulator.positions
    assert simulator.skipped_reentry_cooldown == 1


def test_reentry_after_profit_requires_fresh_improvement_in_simulator() -> None:
    settings = Settings(
        reentry_cooldown_after_profit_minutes=0,
        reentry_after_profit_min_rank_improvement=1,
        reentry_after_profit_min_composite_improvement=0.10,
    )
    simulator = HistoricalBacktestSimulator(settings)
    simulator.last_profitable_exit_state_by_ticker["AAAUSDT"] = (0, 2, 1.40)
    simulator.process_entries(
        timestamp_ms=60_000,
        ranked_signals=[
            RankedSignal(
                stage="emerging",
                signal_kind="entry_ready",
                ticker="AAAUSDT",
                current_price=100.0,
                momentum_z=2.0,
                curvature=0.2,
                hurst=0.7,
                regime_score=2,
                composite_score=1.45,
                rank=2,
                persistence_hits=0,
                alerted=False,
            )
        ],
        mark_prices={"AAAUSDT": 100.0},
    )
    assert "AAAUSDT" not in simulator.positions
    assert simulator.skipped_reentry_no_improvement == 1

    simulator.process_entries(
        timestamp_ms=120_000,
        ranked_signals=[
            RankedSignal(
                stage="emerging",
                signal_kind="entry_ready",
                ticker="AAAUSDT",
                current_price=100.0,
                momentum_z=2.0,
                curvature=0.2,
                hurst=0.7,
                regime_score=2,
                composite_score=1.55,
                rank=2,
                persistence_hits=0,
                alerted=False,
            )
        ],
        mark_prices={"AAAUSDT": 100.0},
    )
    assert "AAAUSDT" in simulator.positions


def test_ticker_daily_loss_limit_blocks_new_entries_in_simulator() -> None:
    settings = Settings(max_ticker_losing_trades_per_day=1)
    simulator = HistoricalBacktestSimulator(settings)
    simulator.ticker_losses_by_day["AAAUSDT"][_utc_day(0)] = 1
    simulator.process_entries(
        timestamp_ms=1,
        ranked_signals=[
            RankedSignal(
                stage="emerging",
                signal_kind="entry_ready",
                ticker="AAAUSDT",
                current_price=100.0,
                momentum_z=2.0,
                curvature=0.2,
                hurst=0.7,
                regime_score=2,
                composite_score=1.4,
                rank=1,
                persistence_hits=0,
                alerted=False,
            )
        ],
        mark_prices={"AAAUSDT": 100.0},
    )
    assert "AAAUSDT" not in simulator.positions
    assert simulator.skipped_ticker_daily_loss_limit == 1


def test_stale_winner_exit_closes_faded_profitable_trade_in_simulator() -> None:
    settings = Settings(
        stale_winner_exit_enabled=True,
        stale_winner_min_profit_pct=0.006,
        stale_winner_min_hold_minutes=360,
        stale_winner_max_rank=3,
        stale_winner_require_profit_protection=True,
    )
    simulator = HistoricalBacktestSimulator(settings)
    simulator.positions["AAAUSDT"] = SimulatedPosition(
        ticker="AAAUSDT",
        quantity=1.0,
        entry_price=100.0,
        raw_entry_price=100.0,
        notional_usd=100.0,
        opened_at_ms=0,
        entry_stage="emerging",
        entry_signal_kind="entry_ready",
        cluster_label="manual:layer1",
        entry_diagnostics="ref=cluster_relative:manual:layer1",
        take_profit_price=103.0,
        stop_loss_price=100.5,
        entry_fee_usd=0.0,
        entry_slippage_usd=0.0,
        profit_protection_adjustments=1,
    )
    closed_tickers = simulator.process_intrabar_exits(
        timestamp_ms=360 * 60_000,
        intrabar_candles={
            "AAAUSDT": HistoricalCandle(
                start_time_ms=0,
                open_price=100.9,
                high_price=101.0,
                low_price=100.7,
                close_price=100.8,
            )
        },
        mark_prices={"AAAUSDT": 100.8},
        ranked_signals=[],
    )
    assert closed_tickers == {"AAAUSDT"}
    assert simulator.trades[-1].exit_reason == "stale_winner"


def test_profit_timing_fields_populate_on_trade_close() -> None:
    settings = Settings()
    simulator = HistoricalBacktestSimulator(settings)
    simulator.positions["AAAUSDT"] = SimulatedPosition(
        ticker="AAAUSDT",
        quantity=1.0,
        entry_price=100.0,
        raw_entry_price=100.0,
        notional_usd=100.0,
        opened_at_ms=0,
        entry_stage="emerging",
        entry_signal_kind="entry_ready",
        cluster_label="manual:layer1",
        entry_diagnostics="ref=cluster_relative:manual:layer1",
        take_profit_price=103.0,
        stop_loss_price=98.0,
        entry_fee_usd=0.0,
        entry_slippage_usd=0.0,
    )
    simulator.process_intrabar_exits(
        timestamp_ms=60_000,
        intrabar_candles={
            "AAAUSDT": HistoricalCandle(
                start_time_ms=0,
                open_price=100.0,
                high_price=101.2,
                low_price=99.8,
                close_price=100.8,
            )
        },
        mark_prices={"AAAUSDT": 100.8},
        ranked_signals=[],
    )
    simulator.force_close_all(timestamp_ms=120_000, mark_prices={"AAAUSDT": 100.8})
    trade = simulator.trades[-1]
    assert trade.minutes_to_first_profit_50bps == 1.0
    assert trade.minutes_to_first_profit_100bps == 1.0


def test_build_sweep_window_end_times_honors_spacing_and_limit() -> None:
    settings = Settings()
    windows = _build_sweep_window_end_times(
        settings=settings,
        lookback_days=400,
        step_days=90,
        end_date="2026-04-10",
        max_windows=3,
    )
    day_ms = interval_to_milliseconds("D")
    assert len(windows) == 3
    assert windows[0] > windows[1] > windows[2]
    assert windows[0] - windows[1] == 90 * day_ms
    assert windows[1] - windows[2] == 90 * day_ms


def test_parse_args_accepts_general_end_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["backtest.py", "--cycles", "96", "--end-date", "2026-04-11"],
    )
    args = parse_args()
    assert args.end_date == "2026-04-11"


def test_fetch_minute_replay_plan_uses_explicit_end_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    sentinel = MinuteReplayPlan(
        confirmed_plan=build_replay_plan(
            history_by_symbol={
                "BTCUSDT": [(0, 1.0), (settings.ticker_interval_ms, 1.1)],
            },
            btc_daily_history=[(0, 20_000.0)],
            state_window=1,
            replay_cycles=1,
        ),
        intrabar_by_symbol={},
        btc_daily_history=[],
        btcdom_history=[],
    )
    captured: dict[str, int] = {}

    async def _fake_fetch_for_window(client, settings_arg, replay_cycles, *, replay_end_ms):
        captured["replay_end_ms"] = replay_end_ms
        return sentinel

    monkeypatch.setattr(backtest, "fetch_minute_replay_plan_for_window", _fake_fetch_for_window)
    result = asyncio.run(
        fetch_minute_replay_plan(
            client=None,
            settings=settings,
            replay_cycles=1,
            replay_end_ms=123_456_000,
        )
    )
    assert captured["replay_end_ms"] == 123_456_000
    assert result is sentinel


def test_parse_args_accepts_resume_and_reconcile_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backtest.py",
            "--grid-setting",
            "hurst_cutoff=0.5,0.6",
            "--resume-variants",
            "--export-dir",
            "out",
            "--reconcile-telegram-html",
            "telegram.html",
            "--reconcile-tolerance-minutes",
            "15",
        ],
    )
    args = parse_args()
    assert args.resume_variants is True
    assert args.reconcile_telegram_html == "telegram.html"
    assert args.reconcile_tolerance_minutes == 15


def test_build_variant_specs_creates_cartesian_product() -> None:
    settings = Settings()
    variants = _build_variant_specs(
        settings,
        [
            "hurst_cutoff=0.45,0.55",
            "intraday_regime_filter_enabled=true,false",
        ],
    )
    names = [variant.name for variant in variants]
    assert len(variants) == 4
    assert "hurst_cutoff=0.45,intraday_regime_filter_enabled=True" in names
    assert "hurst_cutoff=0.55,intraday_regime_filter_enabled=False" in names


def test_stress_variant_specs_and_combination_work() -> None:
    settings = Settings()
    stress = _build_stress_variant_specs(settings, ["costly", "hostile"])
    assert [row.name for row in stress] == ["stress=costly", "stress=hostile"]
    grid = _build_variant_specs(settings, ["hurst_cutoff=0.45,0.55"])
    combined = _combine_variant_specs(grid, stress)
    assert len(combined) == 4
    assert "hurst_cutoff=0.45,stress=costly" in [row.name for row in combined]


def test_resolve_window_universe_keeps_only_complete_symbols() -> None:
    settings = Settings(universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"], backtest_universe_policy="window_available")
    interval_ms = settings.ticker_interval_ms
    history = {
        "AAAUSDT": [(0, 1.0), (interval_ms, 1.1), (interval_ms * 2, 1.2)],
        "BBBUSDT": [(0, 2.0), (interval_ms, 2.1)],
        "CCCUSDT": [(0, 3.0), (interval_ms, 3.1), (interval_ms * 2, 3.2)],
    }
    active = _resolve_window_universe(
        settings,
        history_by_symbol=history,
        start_ms=0,
        end_ms=interval_ms * 3,
    )
    assert active == ["AAAUSDT", "CCCUSDT"]


def test_select_best_variant_prefers_higher_pnl_then_lower_drawdown() -> None:
    selected = _select_best_variant(
        [
            BacktestVariantSummary(
                name="a",
                database_path="a.sqlite3",
                run_seconds=9.0,
                trade_count=5,
                wins=3,
                losses=2,
                net_pnl_usd=100.0,
                total_return_pct=0.01,
                max_drawdown_pct=0.05,
                profit_factor=1.5,
                entry_ready_signals=8,
                entries_filled=5,
            ),
            BacktestVariantSummary(
                name="b",
                database_path="b.sqlite3",
                run_seconds=8.0,
                trade_count=5,
                wins=3,
                losses=2,
                net_pnl_usd=100.0,
                total_return_pct=0.01,
                max_drawdown_pct=0.03,
                profit_factor=1.4,
                entry_ready_signals=8,
                entries_filled=5,
            ),
        ]
    )
    assert selected.name == "b"


async def _exercise_backtest_plan(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "unused.sqlite3"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        state_window=48,
        momentum_lookback=8,
        momentum_skip=1,
        curvature_ma_window=3,
        curvature_signal_window=2,
        hurst_window=48,
        hurst_cutoff=-1.0,
        top_n=1,
        watchlist_top_n=3,
        emerging_top_n=1,
        entry_ready_top_n=1,
        emerging_min_observations=1,
        emerging_min_rank_improvement=0,
        entry_ready_min_observations=1,
        entry_ready_min_rank_improvement=0,
        entry_ready_min_composite_gain=0.0,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
        emerging_cooldown_minutes=0,
        watchlist_cooldown_minutes=0,
        entry_ready_cooldown_minutes=0,
        max_open_positions=1,
        take_profit_pct=0.02,
        stop_loss_pct=0.02,
        entry_notional_usd=100.0,
        intraday_regime_filter_enabled=False,
        analytics_enabled=True,
    )
    total_cycles = settings.state_window + 20
    timestamps = [idx * settings.ticker_interval_ms for idx in range(total_cycles)]
    aaa_prices = []
    bbb_prices = []
    ccc_prices = []
    for idx in range(total_cycles):
        if idx < settings.state_window + 2:
            aaa_prices.append(100.0 + (idx * 0.15) + ((idx % 3) * 0.03))
        else:
            anchor = 100.0 + ((settings.state_window + 1) * 0.15)
            aaa_prices.append(anchor + ((idx - (settings.state_window + 1)) * 2.2))
        bbb_prices.append(100.0 + (idx * 0.45) + ((idx % 2) * 0.04))
        ccc_prices.append(100.0 - (idx * 0.12) + ((idx % 4) * 0.02))
    history = {
        "AAAUSDT": list(zip(timestamps, aaa_prices)),
        "BBBUSDT": list(zip(timestamps, bbb_prices)),
        "CCCUSDT": list(zip(timestamps, ccc_prices)),
    }
    plan = build_replay_plan(
        history_by_symbol=history,
        btc_daily_history=[(idx, 20_000.0 + idx) for idx in range(220)],
        state_window=settings.state_window,
        replay_cycles=12,
    )

    result = await run_backtest_plan(
        settings,
        plan,
        sqlite_path=str(tmp_path / "backtest.sqlite3"),
    )

    assert result.summary.trade_overview is not None
    assert result.summary.trade_overview.trade_count >= 1
    assert result.summary.trade_overview.take_profits >= 1


async def _exercise_backtest_filter_toggle(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "unused-filter.sqlite3"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        state_window=48,
        momentum_lookback=8,
        momentum_skip=1,
        curvature_ma_window=3,
        curvature_signal_window=2,
        hurst_window=48,
        hurst_cutoff=-1.0,
        top_n=1,
        watchlist_top_n=3,
        emerging_top_n=1,
        entry_ready_top_n=1,
        emerging_min_observations=1,
        emerging_min_rank_improvement=0,
        entry_ready_min_observations=1,
        entry_ready_min_rank_improvement=0,
        entry_ready_min_composite_gain=0.0,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
        emerging_cooldown_minutes=0,
        watchlist_cooldown_minutes=0,
        entry_ready_cooldown_minutes=0,
        max_open_positions=1,
        take_profit_pct=0.02,
        stop_loss_pct=0.02,
        entry_notional_usd=100.0,
        intraday_regime_filter_enabled=True,
        intraday_regime_min_basket_return=0.5,
        intraday_regime_min_pass_count=4,
        analytics_enabled=True,
    )
    total_cycles = settings.state_window + 20
    timestamps = [idx * settings.ticker_interval_ms for idx in range(total_cycles)]
    aaa_prices = []
    bbb_prices = []
    ccc_prices = []
    for idx in range(total_cycles):
        if idx < settings.state_window + 2:
            aaa_prices.append(100.0 + (idx * 0.10) + ((idx % 3) * 0.02))
        else:
            anchor = 100.0 + ((settings.state_window + 1) * 0.10)
            aaa_prices.append(anchor + ((idx - (settings.state_window + 1)) * 1.2))
        bbb_prices.append(100.0 + (idx * 0.30) + ((idx % 2) * 0.03))
        ccc_prices.append(100.0 - (idx * 0.10) + ((idx % 4) * 0.01))
    history = {
        "AAAUSDT": list(zip(timestamps, aaa_prices)),
        "BBBUSDT": list(zip(timestamps, bbb_prices)),
        "CCCUSDT": list(zip(timestamps, ccc_prices)),
    }
    plan = build_replay_plan(
        history_by_symbol=history,
        btc_daily_history=[(idx, 20_000.0 + idx) for idx in range(220)],
        state_window=settings.state_window,
        replay_cycles=12,
    )

    blocked = await run_backtest_plan(
        settings,
        plan,
        sqlite_path=str(tmp_path / "blocked.sqlite3"),
        intraday_regime_filter_enabled=True,
    )
    allowed = await run_backtest_plan(
        settings,
        plan,
        sqlite_path=str(tmp_path / "allowed.sqlite3"),
        intraday_regime_filter_enabled=False,
    )

    blocked_count = blocked.summary.trade_overview.trade_count if blocked.summary.trade_overview else 0
    allowed_count = allowed.summary.trade_overview.trade_count if allowed.summary.trade_overview else 0
    assert blocked_count == 0
    assert allowed_count >= 1


async def _exercise_comprehensive_backtest_with_unchanged_provisional(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "unused-comprehensive.sqlite3"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        state_window=24,
        btc_daily_lookback=10,
        btc_vol_lookback=5,
        btcdom_history_lookback=8,
        momentum_lookback=6,
        momentum_skip=1,
        curvature_ma_window=3,
        curvature_signal_window=2,
        hurst_window=24,
        hurst_cutoff=-1.0,
        top_n=1,
        watchlist_top_n=3,
        emerging_top_n=1,
        entry_ready_top_n=1,
        emerging_min_observations=1,
        emerging_min_rank_improvement=0,
        entry_ready_min_observations=1,
        entry_ready_min_rank_improvement=0,
        entry_ready_min_composite_gain=0.0,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
        emerging_cooldown_minutes=0,
        watchlist_cooldown_minutes=0,
        entry_ready_cooldown_minutes=0,
        max_open_positions=1,
        take_profit_pct=0.02,
        stop_loss_pct=0.02,
        risk_per_trade_pct=0.01,
        intraday_regime_filter_enabled=False,
        analytics_enabled=False,
        backtest_intrabar_interval="1",
    )
    day_ms = interval_to_milliseconds("D")
    minute_ms = interval_to_milliseconds("1")
    base_ms = 50 * day_ms
    total_cycles = settings.state_window + 1
    timestamps = [base_ms + (idx * settings.ticker_interval_ms) for idx in range(total_cycles)]
    history = {
        "AAAUSDT": list(zip(timestamps, [100.0 + (idx * 0.5) for idx in range(total_cycles)])),
        "BBBUSDT": list(zip(timestamps, [100.0 + (idx * 0.2) for idx in range(total_cycles)])),
        "CCCUSDT": list(zip(timestamps, [100.0 - (idx * 0.1) for idx in range(total_cycles)])),
        "BTCUSDT": list(zip(timestamps, [40_000.0 + (idx * 20.0) for idx in range(total_cycles)])),
    }
    btc_daily_history = [
        (idx * day_ms, 20_000.0 + (idx * 15.0))
        for idx in range(settings.btc_daily_lookback + 80)
    ]
    confirmed_plan = build_replay_plan(
        history_by_symbol=history,
        btc_daily_history=btc_daily_history,
        state_window=settings.state_window,
        replay_cycles=1,
    )
    bar_start_ms = confirmed_plan.replay_timestamps[0]
    intrabar_by_symbol: dict[str, dict[int, list[HistoricalCandle]]] = {}
    for symbol in settings.tracked_symbols:
        last_close = history[symbol][-1][1]
        candles: list[HistoricalCandle] = []
        for minute_idx in range(settings.ticker_interval_ms // minute_ms):
            if symbol == "AAAUSDT" and minute_idx in {0, 1}:
                close_price = last_close
            else:
                close_price = last_close + (minute_idx * 0.01)
            candles.append(
                HistoricalCandle(
                    start_time_ms=bar_start_ms + (minute_idx * minute_ms),
                    open_price=close_price,
                    high_price=close_price,
                    low_price=close_price,
                    close_price=close_price,
                )
            )
        intrabar_by_symbol[symbol] = {bar_start_ms: candles}
    btcdom_interval_ms = interval_to_milliseconds(settings.btcdom_interval)
    btcdom_history = [
        (idx * btcdom_interval_ms, 1_000.0 + idx)
        for idx in range((base_ms // btcdom_interval_ms) + settings.btcdom_history_lookback + 4)
    ]
    plan = MinuteReplayPlan(
        confirmed_plan=confirmed_plan,
        intrabar_by_symbol=intrabar_by_symbol,
        btc_daily_history=btc_daily_history,
        btcdom_history=btcdom_history,
    )

    result = await run_comprehensive_backtest_plan(
        settings,
        plan,
        sqlite_path=str(tmp_path / "comprehensive.sqlite3"),
    )

    assert result.summary.mode == "1m intrabar replay"
    assert result.summary.trade_count >= 0


async def _exercise_comprehensive_backtest_research_fast(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "unused-comprehensive-fast.sqlite3"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        state_window=24,
        btc_daily_lookback=10,
        btc_vol_lookback=5,
        btcdom_history_lookback=8,
        momentum_lookback=6,
        momentum_skip=1,
        curvature_ma_window=3,
        curvature_signal_window=2,
        hurst_window=24,
        hurst_cutoff=-1.0,
        top_n=1,
        watchlist_top_n=3,
        emerging_top_n=1,
        entry_ready_top_n=1,
        emerging_min_observations=1,
        emerging_min_rank_improvement=0,
        entry_ready_min_observations=1,
        entry_ready_min_rank_improvement=0,
        entry_ready_min_composite_gain=0.0,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
        emerging_cooldown_minutes=0,
        watchlist_cooldown_minutes=0,
        entry_ready_cooldown_minutes=0,
        max_open_positions=1,
        take_profit_pct=0.02,
        stop_loss_pct=0.02,
        risk_per_trade_pct=0.01,
        intraday_regime_filter_enabled=False,
        analytics_enabled=False,
        backtest_intrabar_interval="1",
        backtest_research_fast=True,
    )
    plan = _build_sample_minute_plan(settings, base_ms=80 * interval_to_milliseconds("D"))

    result = await run_comprehensive_backtest_plan(
        settings,
        plan,
        sqlite_path=str(tmp_path / "comprehensive-fast.sqlite3"),
    )

    assert result.summary.mode == "1m intrabar replay [research-fast]"
    assert result.summary.signal_summary.total_rows == 0


async def _exercise_in_memory_signal_summary_database() -> None:
    sink = InMemorySignalDatabase()
    await sink.log_signals(
        [
            SignalRecord(
                timestamp="2026-04-12T00:00:00+00:00",
                stage="emerging",
                signal_kind="entry_ready",
                ticker="AAAUSDT",
                momentum_z=1.0,
                curvature=0.2,
                hurst=0.5,
                regime_score=1,
                composite_score=1.2,
                alerted=True,
                price=101.0,
                rank=1,
                persistence_hits=0,
                dom_falling=False,
                dom_state="neutral",
                dom_change_pct=0.0,
            ),
            SignalRecord(
                timestamp="2026-04-12T00:01:00+00:00",
                stage="emerging",
                signal_kind="none",
                ticker="BBBUSDT",
                momentum_z=0.1,
                curvature=0.0,
                hurst=0.4,
                regime_score=1,
                composite_score=0.1,
                alerted=False,
                price=99.0,
                rank=2,
                persistence_hits=0,
                dom_falling=False,
                dom_state="neutral",
                dom_change_pct=0.0,
            ),
        ]
    )
    summary = sink.to_report_summary(top_n=10)
    assert summary.total_rows == 2
    assert summary.alerted_rows == 1
    assert summary.first_timestamp == "2026-04-12T00:00:00+00:00"
    assert summary.last_timestamp == "2026-04-12T00:01:00+00:00"


async def _exercise_comprehensive_backtest_variants(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "unused-variants.sqlite3"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        state_window=24,
        btc_daily_lookback=10,
        btc_vol_lookback=5,
        btcdom_history_lookback=8,
        momentum_lookback=6,
        momentum_skip=1,
        curvature_ma_window=3,
        curvature_signal_window=2,
        hurst_window=24,
        hurst_cutoff=-1.0,
        top_n=1,
        watchlist_top_n=3,
        emerging_top_n=1,
        entry_ready_top_n=1,
        emerging_min_observations=1,
        emerging_min_rank_improvement=0,
        entry_ready_min_observations=1,
        entry_ready_min_rank_improvement=0,
        entry_ready_min_composite_gain=0.0,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
        emerging_cooldown_minutes=0,
        watchlist_cooldown_minutes=0,
        entry_ready_cooldown_minutes=0,
        max_open_positions=1,
        take_profit_pct=0.02,
        stop_loss_pct=0.02,
        risk_per_trade_pct=0.01,
        intraday_regime_filter_enabled=False,
        analytics_enabled=False,
        backtest_intrabar_interval="1",
        backtest_research_fast=True,
    )
    plan = _build_sample_minute_plan(settings, base_ms=95 * interval_to_milliseconds("D"))
    variants = [
        BacktestVariantSpec(name="tp_2pct", overrides={"take_profit_pct": 0.02}),
        BacktestVariantSpec(name="tp_4pct", overrides={"take_profit_pct": 0.04}),
    ]

    result = await run_comprehensive_backtest_variants(
        settings,
        plan=plan,
        sqlite_path=str(tmp_path / "variants.sqlite3"),
        variants=variants,
        max_workers=2,
    )

    assert {row.name for row in result.variants} == {"tp_2pct", "tp_4pct"}
    assert len(result.variants) == 2
    assert all(row.database_path.endswith(".sqlite3") for row in result.variants)
    assert result.best_variant is not None


async def _exercise_comprehensive_backtest_variant_resume(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "unused-variants-resume.sqlite3"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        state_window=24,
        btc_daily_lookback=10,
        btc_vol_lookback=5,
        btcdom_history_lookback=8,
        momentum_lookback=6,
        momentum_skip=1,
        curvature_ma_window=3,
        curvature_signal_window=2,
        hurst_window=24,
        hurst_cutoff=-1.0,
        top_n=1,
        watchlist_top_n=3,
        emerging_top_n=1,
        entry_ready_top_n=1,
        emerging_min_observations=1,
        emerging_min_rank_improvement=0,
        entry_ready_min_observations=1,
        entry_ready_min_rank_improvement=0,
        entry_ready_min_composite_gain=0.0,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
        emerging_cooldown_minutes=0,
        watchlist_cooldown_minutes=0,
        entry_ready_cooldown_minutes=0,
        max_open_positions=1,
        take_profit_pct=0.02,
        stop_loss_pct=0.02,
        risk_per_trade_pct=0.01,
        intraday_regime_filter_enabled=False,
        analytics_enabled=False,
        backtest_intrabar_interval="1",
        backtest_research_fast=True,
    )
    plan = _build_sample_minute_plan(settings, base_ms=96 * interval_to_milliseconds("D"))
    variants = [
        BacktestVariantSpec(name="tp_2pct", overrides={"take_profit_pct": 0.02}),
        BacktestVariantSpec(name="tp_4pct", overrides={"take_profit_pct": 0.04}),
    ]
    checkpoint_path = tmp_path / "resume" / "variant_summary.csv"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        "name,database_path,trade_count,wins,losses,net_pnl_usd,total_return_pct,max_drawdown_pct,profit_factor,entry_ready_signals,entries_filled\n"
        "tp_2pct,/tmp/tp_2pct.sqlite3,1,1,0,10.0,0.001,0.002,1.5,2,1\n",
        encoding="utf-8",
    )

    result = await run_comprehensive_backtest_variants(
        settings,
        plan=plan,
        sqlite_path=str(tmp_path / "variants-resume.sqlite3"),
        variants=variants,
        max_workers=1,
        checkpoint_path=str(checkpoint_path),
    )

    assert result.variants_requested == 2
    assert result.variants_resumed == 1
    assert result.variants_completed_now == 1
    assert {row.name for row in result.variants} == {"tp_2pct", "tp_4pct"}
    checkpoint_text = checkpoint_path.read_text(encoding="utf-8")
    assert "tp_2pct" in checkpoint_text
    assert "tp_4pct" in checkpoint_text


def _build_sample_minute_plan(settings: Settings, *, base_ms: int) -> MinuteReplayPlan:
    day_ms = interval_to_milliseconds("D")
    minute_ms = interval_to_milliseconds("1")
    total_cycles = settings.state_window + 1
    timestamps = [base_ms + (idx * settings.ticker_interval_ms) for idx in range(total_cycles)]
    history = {
        "AAAUSDT": list(zip(timestamps, [100.0 + (idx * 0.6) for idx in range(total_cycles)])),
        "BBBUSDT": list(zip(timestamps, [100.0 + (idx * 0.2) for idx in range(total_cycles)])),
        "CCCUSDT": list(zip(timestamps, [100.0 - (idx * 0.1) for idx in range(total_cycles)])),
        "BTCUSDT": list(zip(timestamps, [40_000.0 + (idx * 30.0) for idx in range(total_cycles)])),
    }
    btc_daily_history = [
        (idx * day_ms, 20_000.0 + (idx * 15.0))
        for idx in range(settings.btc_daily_lookback + 80)
    ]
    confirmed_plan = build_replay_plan(
        history_by_symbol=history,
        btc_daily_history=btc_daily_history,
        state_window=settings.state_window,
        replay_cycles=1,
    )
    bar_start_ms = confirmed_plan.replay_timestamps[0]
    intrabar_by_symbol: dict[str, dict[int, list[HistoricalCandle]]] = {}
    for symbol in settings.tracked_symbols:
        last_close = history[symbol][-1][1]
        candles = []
        for minute_idx in range(settings.ticker_interval_ms // minute_ms):
            close_price = last_close + (minute_idx * 0.02)
            high_price = close_price
            if symbol == "AAAUSDT" and minute_idx == 14:
                high_price = last_close * 1.05
            candles.append(
                HistoricalCandle(
                    start_time_ms=bar_start_ms + (minute_idx * minute_ms),
                    open_price=close_price,
                    high_price=high_price,
                    low_price=close_price,
                    close_price=close_price,
                )
            )
        intrabar_by_symbol[symbol] = {bar_start_ms: candles}
    btcdom_interval_ms = interval_to_milliseconds(settings.btcdom_interval)
    btcdom_history = [
        (idx * btcdom_interval_ms, 1_000.0 + idx)
        for idx in range((base_ms // btcdom_interval_ms) + settings.btcdom_history_lookback + 4)
    ]
    return MinuteReplayPlan(
        confirmed_plan=confirmed_plan,
        intrabar_by_symbol=intrabar_by_symbol,
        btc_daily_history=btc_daily_history,
        btcdom_history=btcdom_history,
        active_universe=[symbol for symbol in settings.tracked_symbols if symbol != "BTCUSDT"],
    )


def _seed_trade_analytics_db(path: Path) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE trade_analytics (
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                exit_reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO trade_analytics (ticker, side, opened_at, closed_at, exit_reason)
            VALUES ('AAAUSDT', 'LONG', '2026-04-11T00:00:00+00:00', '2026-04-11T01:00:00+00:00', 'take_profit')
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_variant_stability_summary_reports_robustness_metrics() -> None:
    rows = [
        BacktestVariantSummary(
            name="take_profit_pct=0.02,stop_loss_pct=0.02",
            database_path="a.sqlite3",
            run_seconds=1.0,
            trade_count=10,
            wins=6,
            losses=4,
            net_pnl_usd=120.0,
            total_return_pct=0.012,
            max_drawdown_pct=0.03,
            profit_factor=1.2,
            entry_ready_signals=20,
            entries_filled=10,
        ),
        BacktestVariantSummary(
            name="take_profit_pct=0.025,stop_loss_pct=0.02",
            database_path="b.sqlite3",
            run_seconds=1.0,
            trade_count=10,
            wins=5,
            losses=5,
            net_pnl_usd=-20.0,
            total_return_pct=-0.002,
            max_drawdown_pct=0.04,
            profit_factor=0.9,
            entry_ready_signals=20,
            entries_filled=10,
        ),
        BacktestVariantSummary(
            name="take_profit_pct=0.015,stop_loss_pct=0.02",
            database_path="c.sqlite3",
            run_seconds=1.0,
            trade_count=10,
            wins=6,
            losses=4,
            net_pnl_usd=60.0,
            total_return_pct=0.006,
            max_drawdown_pct=0.025,
            profit_factor=1.1,
            entry_ready_signals=20,
            entries_filled=10,
        ),
    ]
    summary = _variant_stability_summary(rows)
    assert summary["variant_count"] == 3
    assert summary["profitable_variants"] == 2
    assert summary["pf_gt_one_variants"] == 2
    assert summary["median_net_pnl_usd"] == 60.0
    assert summary["worst_max_drawdown_pct"] == 0.04


def test_variant_setting_stability_rows_group_by_setting_value() -> None:
    rows = [
        BacktestVariantSummary(
            name="take_profit_pct=0.02,stop_loss_pct=0.02",
            database_path="a.sqlite3",
            run_seconds=1.0,
            trade_count=10,
            wins=6,
            losses=4,
            net_pnl_usd=100.0,
            total_return_pct=0.01,
            max_drawdown_pct=0.03,
            profit_factor=1.2,
            entry_ready_signals=20,
            entries_filled=10,
        ),
        BacktestVariantSummary(
            name="take_profit_pct=0.02,stop_loss_pct=0.025",
            database_path="b.sqlite3",
            run_seconds=1.0,
            trade_count=10,
            wins=5,
            losses=5,
            net_pnl_usd=40.0,
            total_return_pct=0.004,
            max_drawdown_pct=0.05,
            profit_factor=1.05,
            entry_ready_signals=20,
            entries_filled=10,
        ),
        BacktestVariantSummary(
            name="take_profit_pct=0.03,stop_loss_pct=0.02",
            database_path="c.sqlite3",
            run_seconds=1.0,
            trade_count=10,
            wins=4,
            losses=6,
            net_pnl_usd=-10.0,
            total_return_pct=-0.001,
            max_drawdown_pct=0.04,
            profit_factor=0.95,
            entry_ready_signals=20,
            entries_filled=10,
        ),
    ]
    setting_rows = _variant_setting_stability_rows(rows)
    tp_rows = [row for row in setting_rows if row["setting"] == "take_profit_pct"]
    assert any(row["value"] == "0.02" and row["variant_count"] == 2 for row in tp_rows)
    assert any(row["value"] == "0.03" and row["profitable_variants"] == 0 for row in tp_rows)
