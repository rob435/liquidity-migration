from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from aggression_carry import daily_close_fade as daily_close_fade_module
from aggression_carry.config import CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig
from aggression_carry.daily_close_fade import (
    DailyCloseFadeDiagnosticsConfig,
    _short_return,
    iter_close_fade_grid_configs,
    run_daily_close_fade,
    run_daily_close_fade_diagnostics,
    run_daily_close_fade_grid,
    run_daily_close_fade_sleeves,
    select_close_fade_candidates,
)
from aggression_carry.storage import read_dataset, write_dataset


def test_daily_close_fade_excludes_young_pumps_and_writes_trade_ledger(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=2,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            stop_loss_pct=0.0,
            trailing_stop_pct=0.0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")
    features = read_dataset(tmp_path, "daily_close_fade_features")

    assert payload["rows"]["trades"] == 2
    assert (tmp_path / "reports" / "daily_close_fade_equity.csv").exists()
    assert (tmp_path / "reports" / "daily_close_fade_equity_curve.svg").exists()
    assert payload["backtest_validity"]["label"] == "biased_benchmark"
    assert payload["backtest_validity"]["can_support_promotion"] is False
    assert payload["summary"]["funding_mode"] == "missing"
    assert trades["symbol"].to_list() == ["OLD1USDT", "OLD2USDT"]
    assert "YOUNGUSDT" not in trades["symbol"].to_list()
    assert set(trades["exit_reason"].to_list()) == {"max_hold"}
    assert trades["net_return"].min() > 0.0
    young = features.filter(pl.col("symbol") == "YOUNGUSDT").row(0, named=True)
    assert young["eligible"] is False
    assert young["age_days"] < 10.0
    assert "context_fade_score" in features.columns


def test_daily_close_fade_include_symbols_limits_feature_universe(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=2,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            include_symbols=("OLD2USDT",),
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")
    features = read_dataset(tmp_path, "daily_close_fade_features")

    assert payload["rows"]["trades"] == 1
    assert trades["symbol"].to_list() == ["OLD2USDT"]
    assert features["symbol"].unique().to_list() == ["OLD2USDT"]


def test_daily_close_fade_signal_features_ignore_open_signal_bar(tmp_path) -> None:
    start = datetime(2025, 1, 15, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    signal_minute = 22 * 60
    symbol = "CAUSEUSDT"
    write_dataset(
        pl.DataFrame(
            [
                {
                    "ts_ms": start_ms,
                    "symbol": symbol,
                    "category": "linear",
                    "contract_type": "LinearPerpetual",
                    "status": "Trading",
                    "settle_coin": "USDT",
                    "launch_time_ms": int((start - timedelta(days=40)).timestamp() * 1000),
                    "tick_size": 0.001,
                    "qty_step": 0.001,
                    "min_order_qty": 0.001,
                    "min_notional_value": 5.0,
                    "funding_interval_min": 480,
                    "is_prelisting": False,
                }
            ]
        ),
        tmp_path,
        "instruments",
    )
    rows = []
    for minute in range(signal_minute + 2):
        close = 100.0 + minute / 100.0
        high = close
        if minute == signal_minute:
            close = 999.0
            high = 999.0
        rows.append(
            {
                "ts_ms": start_ms + minute * 60_000,
                "symbol": symbol,
                "open": close,
                "high": high,
                "low": close,
                "close": close,
                "volume_base": 100.0,
                "turnover_quote": close * 100.0,
                "source": "fixture",
            }
        )
    write_dataset(pl.DataFrame(rows), tmp_path, "klines_1m")

    features = daily_close_fade_module.build_daily_close_fade_features(
        tmp_path,
        config=DailyCloseFadeConfig(signal_minute=signal_minute, min_age_days=10, exclude_symbols=()),
        signal_minutes=(signal_minute,),
    )
    row = features.row(0, named=True)

    assert row["signal_ts_ms"] == start_ms + signal_minute * 60_000
    assert row["bar_count"] == signal_minute
    assert row["expected_bar_count"] == signal_minute
    assert row["bar_coverage"] == 1.0
    assert row["signal_close"] == 100.0 + (signal_minute - 1) / 100.0
    assert row["day_high"] < 999.0


def test_daily_close_fade_uses_optional_context_and_funding_carry(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)
    start = datetime(2025, 1, 15, tzinfo=UTC)
    signal_ts_ms = int((start + timedelta(hours=23)).timestamp() * 1000)
    entry_ts_ms = signal_ts_ms

    write_dataset(
        pl.DataFrame(
            [
                {
                    "ts_ms": signal_ts_ms - 8 * 60 * 60 * 1000,
                    "symbol": "OLD1USDT",
                    "funding_rate": 0.001,
                    "funding_interval_min": 480,
                },
                {
                    "ts_ms": entry_ts_ms + 60 * 60 * 1000,
                    "symbol": "OLD1USDT",
                    "funding_rate": 0.002,
                    "funding_interval_min": 480,
                },
            ]
        ),
        tmp_path,
        "funding",
    )
    write_dataset(
        pl.DataFrame(
            [
                {"ts_ms": signal_ts_ms - 24 * 60 * 60 * 1000, "symbol": "OLD1USDT", "open_interest_value": 1_000_000.0},
                {"ts_ms": signal_ts_ms - 60 * 60 * 1000, "symbol": "OLD1USDT", "open_interest_value": 1_200_000.0},
                {"ts_ms": signal_ts_ms, "symbol": "OLD1USDT", "open_interest_value": 1_500_000.0},
            ]
        ),
        tmp_path,
        "open_interest",
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "ts_ms": signal_ts_ms - 60 * 60 * 1000,
                    "symbol": "OLD1USDT",
                    "buy_quote": 800_000.0,
                    "sell_quote": 200_000.0,
                    "buy_base": 8000.0,
                    "sell_base": 2000.0,
                    "trade_count_buy": 80,
                    "trade_count_sell": 20,
                    "total_quote": 1_000_000.0,
                    "signed_quote": 600_000.0,
                    "trade_count": 100,
                    "imbalance": 0.6,
                }
            ]
        ),
        tmp_path,
        "signed_flow_1h",
    )
    write_dataset(
        pl.DataFrame(
            [
                {"ts_ms": signal_ts_ms - 2 * 60 * 60 * 1000, "symbol": "OLD1USDT", "open": 0.004, "high": 0.006, "low": 0.0, "close": 0.005},
                {"ts_ms": signal_ts_ms - 60 * 60 * 1000, "symbol": "OLD1USDT", "open": 0.006, "high": 0.02, "low": 0.0, "close": 0.01},
                {"ts_ms": signal_ts_ms, "symbol": "OLD1USDT", "open": 0.01, "high": 0.03, "low": 0.0, "close": 0.025},
            ]
        ),
        tmp_path,
        "premium_index_1h",
    )

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=1,
            hold_minutes=60,
            score="context_fade_score",
            min_age_days=10,
            stop_loss_pct=0.0,
            trailing_stop_pct=0.0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    features = read_dataset(tmp_path, "daily_close_fade_features").filter(pl.col("symbol") == "OLD1USDT").row(0, named=True)
    trades = read_dataset(tmp_path, "daily_close_fade_trades")

    assert features["funding_rate_8h_equiv"] == 0.001
    assert features["has_open_interest_context"] is True
    assert features["has_premium_context"] is True
    assert features["has_trade_flow_context"] is True
    assert features["oi_change_1h"] > 0.0
    assert features["premium_index_close"] == 0.01
    assert round(features["premium_change_1h"], 6) == 0.005
    assert features["flow_imbalance_60m"] == 0.6
    assert trades["funding_mode"].to_list() == ["modeled"]
    assert trades["has_premium_context"].to_list() == [True]
    assert trades["funding_event_count"].to_list() == [1]
    assert round(trades["funding_return"].item(), 6) == 0.002
    assert payload["summary"]["funding_mode"] == "modeled"
    assert payload["summary"]["all_context_rate"] == 1.0


def test_daily_close_fade_marks_missing_crossed_funding_event(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)
    start = datetime(2025, 1, 15, tzinfo=UTC)
    signal_ts_ms = int((start + timedelta(hours=23)).timestamp() * 1000)

    write_dataset(
        pl.DataFrame(
            [
                {
                    "ts_ms": signal_ts_ms - 8 * 60 * 60 * 1000,
                    "symbol": "OLD1USDT",
                    "funding_rate": 0.001,
                    "funding_interval_min": 480,
                }
            ]
        ),
        tmp_path,
        "funding",
    )

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=1,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            stop_loss_pct=0.0,
            trailing_stop_pct=0.0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")

    assert trades["funding_mode"].to_list() == ["missing_expected"]
    assert trades["funding_event_count"].to_list() == [0]
    assert payload["summary"]["funding_mode"] == "missing"


def test_daily_close_fade_uses_linear_usdt_short_returns() -> None:
    assert _short_return(100.0, 85.0) == 0.15
    assert _short_return(100.0, 115.0) == -0.15


def test_daily_close_fade_trailing_stop_exit_reason(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path, rebound=True)

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=1,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            stop_loss_pct=0.0,
            trailing_stop_pct=0.02,
            trailing_activation_pct=0.02,
            stop_delay_minutes=0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")

    assert payload["rows"]["trades"] == 1
    assert trades["exit_reason"].to_list() == ["trailing_stop"]
    assert trades["symbol"].to_list() == ["OLD1USDT"]
    assert trades["net_return"].item() > 0.0


def test_daily_close_fade_take_profit_exit_reason(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=1,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            stop_loss_pct=0.20,
            take_profit_pct=0.05,
            stop_delay_minutes=0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")

    assert payload["rows"]["trades"] == 1
    assert trades["exit_reason"].to_list() == ["take_profit"]
    assert trades["symbol"].to_list() == ["OLD1USDT"]
    assert round(trades["gross_return"].item(), 6) == 0.05


def test_daily_close_fade_time_decay_take_profit_exits_before_max_hold(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path, rebound=True)

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=1,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            stop_loss_pct=0.20,
            take_profit_pct=0.10,
            time_decay_take_profit_floor_pct=0.05,
            time_decay_take_profit_minutes=1,
            stop_delay_minutes=0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")

    assert payload["rows"]["trades"] == 1
    assert trades["exit_reason"].to_list() == ["time_decay_take_profit"]
    assert round(trades["gross_return"].item(), 6) == 0.05
    assert trades["post_twap_hold_minutes"].item() < 60


def test_daily_close_fade_stop_wins_when_same_bar_hits_stop_and_take_profit(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path, same_bar_stop_and_tp=True)

    run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=1,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            stop_loss_pct=0.20,
            take_profit_pct=0.05,
            stop_delay_minutes=0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")

    assert trades["exit_reason"].to_list() == ["stop_loss"]
    assert round(trades["gross_return"].item(), 6) == -0.20


def test_daily_close_fade_mfe_giveback_exit_reason(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path, rebound=True)

    run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=1,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            mfe_giveback_activation_pct=0.02,
            mfe_giveback_pct=0.20,
            stop_delay_minutes=0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")

    assert trades["exit_reason"].to_list() == ["mfe_giveback"]
    assert trades["net_return"].item() > 0.0


def test_daily_close_fade_vwap_reversion_exit_reason(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=1,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            vwap_reversion_pct=0.25,
            stop_delay_minutes=0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")

    assert trades["exit_reason"].to_list() == ["vwap_reversion"]
    assert trades["net_return"].item() > 0.0


def test_daily_close_fade_twap_averages_entry_and_delays_profit_protection(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=22 * 60,
            top_n=1,
            hold_minutes=60,
            entry_delay_minutes=0,
            entry_twap_minutes=60,
            score="day_return",
            pump_filter="all",
            min_age_days=10,
            stop_loss_pct=0.20,
            vol_trailing_stop_mult=0.25,
            mfe_giveback_activation_pct=0.01,
            mfe_giveback_pct=0.20,
            stop_delay_minutes=15,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")
    trade = trades.row(0, named=True)
    klines = read_dataset(tmp_path, "klines_1m")
    start_ms = int(datetime(2025, 1, 15, 22, 0, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime(2025, 1, 15, 23, 0, tzinfo=UTC).timestamp() * 1000)
    expected_entry = (
        klines.filter((pl.col("symbol") == trade["symbol"]) & (pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))
        .sort("ts_ms")["open"]
        .mean()
    )

    assert trade["entry_fill_count"] == 60
    assert trade["entry_fill_fraction"] == 1.0
    assert trade["entry_time"] == "2025-01-15T22:00:00+00:00"
    assert trade["entry_complete_time"] == "2025-01-15T23:00:00+00:00"
    assert trade["profit_protection_active_time"] == "2025-01-15T23:15:00+00:00"
    assert round(trade["entry_price"], 10) == round(expected_entry, 10)
    assert trade["post_twap_hold_minutes"] <= 60.0


def test_daily_close_fade_profit_protection_does_not_warm_start_before_activation(tmp_path) -> None:
    _write_profit_protection_warm_start_fixture(tmp_path)

    run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=22 * 60,
            top_n=1,
            hold_minutes=60,
            entry_delay_minutes=0,
            entry_twap_minutes=60,
            score="day_return",
            pump_filter="all",
            min_age_days=10,
            stop_loss_pct=0.20,
            vol_trailing_stop_mult=0.0,
            mfe_giveback_activation_pct=0.01,
            mfe_giveback_pct=0.20,
            stop_delay_minutes=0,
            profit_protection_delay_minutes=15,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trade = read_dataset(tmp_path, "daily_close_fade_trades").row(0, named=True)

    assert trade["profit_protection_active_time"] == "2025-01-15T23:15:00+00:00"
    assert trade["mfe"] > 0.05
    assert trade["exit_reason"] == "max_hold"
    assert trade["exit_time"] == "2025-01-16T00:00:00+00:00"
    assert trade["post_twap_hold_minutes"] == 60.0


def test_daily_close_fade_twap_hard_stop_can_be_active_from_first_fill(tmp_path) -> None:
    _write_twap_risk_fixture(tmp_path, spike_minute=5, spike_high=121.0)

    run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=22 * 60,
            top_n=1,
            hold_minutes=60,
            entry_delay_minutes=0,
            entry_twap_minutes=60,
            score="day_return",
            pump_filter="all",
            min_age_days=10,
            stop_loss_pct=0.20,
            stop_delay_minutes=0,
            profit_protection_delay_minutes=15,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trade = read_dataset(tmp_path, "daily_close_fade_trades").row(0, named=True)

    assert trade["exit_reason"] == "stop_loss"
    assert trade["entry_fill_count"] == 6
    assert trade["entry_fill_fraction"] == 0.10
    assert trade["stop_active_time"] == "2025-01-15T22:00:00+00:00"
    assert trade["exit_time"] == "2025-01-15T22:05:00+00:00"
    assert round(trade["gross_return"], 6) == -0.20


def test_daily_close_fade_twap_stop_adding_guard_freezes_partial_entry(tmp_path) -> None:
    _write_twap_risk_fixture(tmp_path, spike_minute=10, spike_high=109.0, fade_after_spike=True)

    run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=22 * 60,
            top_n=1,
            hold_minutes=60,
            entry_delay_minutes=0,
            entry_twap_minutes=60,
            score="day_return",
            pump_filter="all",
            min_age_days=10,
            stop_loss_pct=0.20,
            stop_delay_minutes=0,
            profit_protection_delay_minutes=15,
            twap_stop_adding_pct=0.08,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trade = read_dataset(tmp_path, "daily_close_fade_trades").row(0, named=True)

    assert trade["twap_stopped_adding"] is True
    assert trade["twap_stop_adding_time"] == "2025-01-15T22:10:00+00:00"
    assert trade["entry_complete_time"] == "2025-01-15T22:10:00+00:00"
    assert trade["profit_protection_active_time"] == "2025-01-15T22:25:00+00:00"
    assert trade["entry_fill_count"] == 11
    assert round(trade["entry_fill_fraction"], 6) == round(11 / 60, 6)
    assert trade["exit_reason"] == "max_hold"
    assert trade["exit_time"] == "2025-01-15T23:10:00+00:00"


def test_daily_close_fade_can_require_archive_membership(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "symbol": "OLD2USDT",
                    "date": "2025-01-15",
                    "url": "https://public.bybit.com/trading/OLD2USDT/OLD2USDT2025-01-15.csv.gz",
                    "source": "fixture",
                }
            ]
        ),
        tmp_path,
        "archive_trade_manifest",
        partition_by=("date",),
    )

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=2,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            require_archive_membership=True,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")
    features = read_dataset(tmp_path, "daily_close_fade_features")

    assert payload["rows"]["trades"] == 1
    assert trades["symbol"].to_list() == ["OLD2USDT"]
    assert features.filter(pl.col("symbol") == "OLD1USDT").row(0, named=True)["archive_tradable"] is False
    assert features.filter(pl.col("symbol") == "OLD2USDT").row(0, named=True)["archive_tradable"] is True


def test_daily_close_fade_basket_stop_exits_open_basket(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path, adverse_reversal=True)

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=2,
            hold_minutes=60,
            score="day_return",
            min_age_days=10,
            stop_loss_pct=0.0,
            basket_stop_loss_pct=0.04,
            trailing_stop_pct=0.0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades")
    baskets = read_dataset(tmp_path, "daily_close_fade_baskets")

    assert payload["summary"]["exit_basket_stop"] == 2
    assert set(trades["exit_reason"].to_list()) == {"basket_stop"}
    assert trades["hold_minutes"].max() < 60.0
    assert baskets["basket_return"].item() <= -0.04


def test_daily_close_fade_grid_runs_small_fixture(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade_grid(
        tmp_path,
        base_fade_config=DailyCloseFadeConfig(min_age_days=10, exclude_symbols=()),
        grid_config=DailyCloseFadeGridConfig(
            signal_minutes=(23 * 60,),
            top_ns=(1, 2),
            hold_minutes=(60,),
            scores=("day_return",),
            pump_filters=("all", "pump"),
            stop_loss_pcts=(0.0,),
            take_profit_pcts=(0.0, 0.05),
            trailing_stop_pcts=(0.0,),
            trailing_activation_pcts=(0.0,),
            profit_protection_delay_minutes=(0, 15),
            cost_multipliers=(1.0,),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
        max_workers=1,
    )

    grid = read_dataset(tmp_path, "daily_close_fade_grid")

    assert payload["rows"] == 16
    assert grid.height == 16
    assert set(grid["profit_protection_delay_minutes"].to_list()) == {0, 15}
    assert grid["total_return"].max() > 0.0


def test_daily_close_fade_grid_deduplicates_disabled_time_decay_take_profit() -> None:
    configs = iter_close_fade_grid_configs(
        DailyCloseFadeGridConfig(
            signal_minutes=(23 * 60,),
            top_ns=(1,),
            hold_minutes=(60,),
            scores=("day_return",),
            pump_filters=("all",),
            stop_loss_pcts=(0.0,),
            take_profit_pcts=(0.0, 0.10),
            time_decay_take_profit_floor_pcts=(0.0, 0.05),
            time_decay_take_profit_minutes=(0, 30),
            basket_stop_loss_pcts=(0.0,),
            trailing_stop_pcts=(0.0,),
            trailing_activation_pcts=(0.0,),
            vol_trailing_stop_mults=(0.0,),
            vol_trailing_activation_mults=(0.0,),
            mfe_giveback_activation_pcts=(0.0,),
            mfe_giveback_pcts=(0.0,),
            vwap_reversion_pcts=(0.0,),
            profit_protection_delay_minutes=(0,),
            liquidity_rank_mins=(1,),
            liquidity_rank_maxs=(0,),
            cost_multipliers=(1.0,),
        ),
        DailyCloseFadeConfig(min_age_days=10, exclude_symbols=()),
    )

    assert len(configs) == 3
    assert {(item.take_profit_pct, item.time_decay_take_profit_floor_pct, item.time_decay_take_profit_minutes) for item in configs} == {
        (0.0, 0.0, 0),
        (0.10, 0.0, 0),
        (0.10, 0.05, 30),
    }


def test_daily_close_fade_grid_supports_date_splits(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade_grid(
        tmp_path,
        base_fade_config=DailyCloseFadeConfig(min_age_days=10, exclude_symbols=()),
        grid_config=DailyCloseFadeGridConfig(
            signal_minutes=(23 * 60,),
            top_ns=(1,),
            hold_minutes=(60,),
            scores=("day_return",),
            pump_filters=("all",),
            stop_loss_pcts=(0.0,),
            take_profit_pcts=(0.0,),
            trailing_stop_pcts=(0.0,),
            trailing_activation_pcts=(0.0,),
            cost_multipliers=(1.0,),
            start_ms=int(datetime(2025, 1, 17, tzinfo=UTC).timestamp() * 1000),
            end_ms=int(datetime(2025, 1, 18, tzinfo=UTC).timestamp() * 1000),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
        max_workers=1,
    )

    assert payload["rows"] == 1
    assert payload["best_total_return"]["trade_count"] == 0


def test_daily_close_fade_diagnostics_reports_raw_score_shape(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade_diagnostics(
        tmp_path,
        base_fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            score="day_return",
            pump_filter="all",
            min_age_days=10,
            exclude_symbols=(),
        ),
        diagnostics_config=DailyCloseFadeDiagnosticsConfig(
            signal_minutes=(23 * 60,),
            entry_delay_minutes=(1,),
            horizon_minutes=(60,),
            scores=("day_return",),
            top_ns=(1, 2),
            buckets=2,
            min_obs_per_bucket=1,
        ),
    )

    buckets = read_dataset(tmp_path, "daily_close_fade_diagnostic_buckets").sort("bucket")
    top_baskets = read_dataset(tmp_path, "daily_close_fade_diagnostic_top_baskets")
    ic = read_dataset(tmp_path, "daily_close_fade_diagnostic_ic")
    monthly = read_dataset(tmp_path, "daily_close_fade_diagnostic_monthly")
    consistency = read_dataset(tmp_path, "daily_close_fade_diagnostic_month_consistency")

    assert payload["rows"]["observations"] == 3
    assert payload["rows"]["monthly_rows"] == 2
    assert payload["rows"]["consistency_rows"] == 2
    assert buckets.height == 2
    assert buckets.filter(pl.col("bucket") == 2)["mean_short_return"].item() > buckets.filter(pl.col("bucket") == 1)[
        "mean_short_return"
    ].item()
    assert top_baskets["mean_basket_short_return"].max() > 0.0
    assert "mean_basket_cost_adjusted_short_return" in top_baskets.columns
    assert monthly["month"].to_list() == ["2025-01", "2025-01"]
    assert consistency["cost_positive_month_rate"].max() > 0.0
    assert ic["mean_ic"].item() > 0.0
    assert (tmp_path / "reports" / "daily_close_fade_diagnostics_report.md").exists()


def test_daily_close_fade_diagnostics_handles_sparse_bucket_grid(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade_diagnostics(
        tmp_path,
        base_fade_config=DailyCloseFadeConfig(min_age_days=10, exclude_symbols=()),
        diagnostics_config=DailyCloseFadeDiagnosticsConfig(
            signal_minutes=(23 * 60,),
            entry_delay_minutes=(1,),
            horizon_minutes=(60,),
            scores=("day_return",),
            top_ns=(2,),
            buckets=10,
            min_obs_per_bucket=1,
        ),
    )

    assert payload["rows"]["scenarios"] == 1
    assert payload["top_scenarios"][0]["robust_direction_pass"] is False
    assert payload["top_scenarios"][0]["cost_edge_pass"] is False
    assert payload["top_scenarios"][0]["high_minus_low"] is None


def test_daily_close_fade_diagnostics_supports_date_splits(tmp_path) -> None:
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade_diagnostics(
        tmp_path,
        base_fade_config=DailyCloseFadeConfig(min_age_days=10, exclude_symbols=()),
        diagnostics_config=DailyCloseFadeDiagnosticsConfig(
            signal_minutes=(23 * 60,),
            entry_delay_minutes=(1,),
            horizon_minutes=(60,),
            scores=("day_return",),
            top_ns=(1,),
            buckets=2,
            min_obs_per_bucket=1,
            start_ms=int(datetime(2025, 1, 17, tzinfo=UTC).timestamp() * 1000),
            end_ms=int(datetime(2025, 1, 18, tzinfo=UTC).timestamp() * 1000),
        ),
    )

    assert payload["rows"]["features"] == 0
    assert payload["rows"]["observations"] == 0
    assert payload["rows"]["scenarios"] == 0


def test_daily_close_fade_grid_uses_thread_backend_on_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(daily_close_fade_module.sys, "platform", "win32")
    _write_close_fade_fixture(tmp_path)

    payload = run_daily_close_fade_grid(
        tmp_path,
        base_fade_config=DailyCloseFadeConfig(min_age_days=10, exclude_symbols=()),
        grid_config=DailyCloseFadeGridConfig(
            signal_minutes=(23 * 60,),
            top_ns=(1, 2),
            hold_minutes=(60,),
            scores=("day_return",),
            pump_filters=("all",),
            stop_loss_pcts=(0.0,),
            take_profit_pcts=(0.0,),
            trailing_stop_pcts=(0.0,),
            trailing_activation_pcts=(0.0,),
            cost_multipliers=(1.0,),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
        max_workers=2,
    )

    assert payload["worker_backend"] == "thread"
    assert payload["rows"] == 2


def test_daily_close_fade_filters_top_baseline_liquidity_rank(tmp_path) -> None:
    _write_close_fade_liquidity_fixture(tmp_path)

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=3,
            hold_minutes=60,
            score="day_return",
            pump_filter="pump",
            min_age_days=10,
            liquidity_lookback_days=1,
            liquidity_rank_min=2,
            liquidity_rank_max=3,
            stop_loss_pct=0.0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades").sort("baseline_liquidity_rank")
    features = read_dataset(tmp_path, "daily_close_fade_features")
    big = features.filter((pl.col("symbol") == "BIGUSDT") & (pl.col("date") == "2025-01-15")).row(0, named=True)

    assert payload["rows"]["trades"] == 2
    assert trades["symbol"].to_list() == ["MIDUSDT", "TAILUSDT"]
    assert trades["baseline_liquidity_rank"].to_list() == [2, 3]
    assert big["baseline_liquidity_rank"] == 1
    assert "BIGUSDT" not in trades["symbol"].to_list()


def test_daily_close_fade_capacity_caps_trade_weight(tmp_path) -> None:
    _write_close_fade_liquidity_fixture(tmp_path)

    payload = run_daily_close_fade(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=2,
            hold_minutes=60,
            score="day_return",
            pump_filter="pump",
            min_age_days=10,
            liquidity_lookback_days=1,
            liquidity_rank_min=2,
            liquidity_rank_max=3,
            account_equity=10_000.0,
            max_position_weight=0.10,
            stop_loss_pct=0.0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    trades = read_dataset(tmp_path, "daily_close_fade_trades").sort("baseline_liquidity_rank")
    baskets = read_dataset(tmp_path, "daily_close_fade_baskets")

    assert payload["rows"]["trades"] == 2
    assert trades["target_weight"].to_list() == [0.5, 0.5]
    assert trades["weight"].to_list() == [0.1, 0.1]
    assert trades["capacity_limited"].to_list() == [True, True]
    assert baskets["basket_gross_exposure"].item() == 0.2
    assert payload["summary"]["capacity_limited_trades"] == 2


def test_daily_close_fade_score_capped_sizing_weights_selected_pumps() -> None:
    features = pl.DataFrame(
        [
            _feature_candidate("AAAUSDT", 0.24, 0.12, 0.050, 1.6),
            _feature_candidate("BBBUSDT", 0.12, 0.10, 0.040, 1.3),
            _feature_candidate("CCCUSDT", 0.06, 0.04, 0.040, 1.4),
        ],
        infer_schema_length=None,
    )

    selected = select_close_fade_candidates(
        features,
        DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=5,
            gross_exposure=1.0,
            score="vol_adjusted_day_return",
            pump_filter="pump",
            coin_excess_vs_market_min=0.08,
            coin_vwap_extension_min=0.035,
            coin_late_volume_ratio_min=1.0,
            position_sizing="score_capped",
            max_position_weight=0.80,
            score_weight_power=1.0,
            exclude_symbols=(),
        ),
    ).sort("entry_rank")

    assert selected["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]
    weights = selected["position_target_weight"].to_list()
    assert round(weights[0], 6) == round(0.24 / 0.36, 6)
    assert round(weights[1], 6) == round(0.12 / 0.36, 6)


def test_daily_close_fade_context_quality_filters_reject_risky_candidates() -> None:
    features = pl.DataFrame(
        [
            {
                **_feature_candidate("GOODUSDT", 0.20, 0.12, 0.080, 1.4),
                "market_median_day_return": 0.04,
                "organic_move_score": 0.7,
                "manipulation_risk_score": 0.6,
                "squeeze_risk_score": 0.5,
                "has_open_interest_context": True,
                "has_funding_context": True,
                "has_premium_context": True,
                "has_trade_flow_context": True,
            },
            {
                **_feature_candidate("HOTMKTUSDT", 0.22, 0.15, 0.070, 1.5),
                "market_median_day_return": 0.09,
                "organic_move_score": 0.7,
                "manipulation_risk_score": 0.8,
                "squeeze_risk_score": 0.5,
                "has_open_interest_context": True,
                "has_funding_context": True,
                "has_premium_context": True,
                "has_trade_flow_context": True,
            },
            {
                **_feature_candidate("NOOIUSDT", 0.25, 0.16, 0.090, 1.6),
                "market_median_day_return": 0.04,
                "organic_move_score": 0.7,
                "manipulation_risk_score": 0.8,
                "squeeze_risk_score": 0.5,
                "has_open_interest_context": False,
                "has_funding_context": True,
                "has_premium_context": True,
                "has_trade_flow_context": True,
            },
            {
                **_feature_candidate("SQUEEZEUSDT", 0.24, 0.16, 0.220, 1.6),
                "market_median_day_return": 0.04,
                "organic_move_score": 0.7,
                "manipulation_risk_score": 0.8,
                "squeeze_risk_score": 1.7,
                "has_open_interest_context": True,
                "has_funding_context": True,
                "has_premium_context": True,
                "has_trade_flow_context": True,
            },
        ],
        infer_schema_length=None,
    )

    selected = select_close_fade_candidates(
        features,
        DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=5,
            score="vol_adjusted_day_return",
            pump_filter="pump",
            coin_vwap_extension_min=0.035,
            coin_vwap_extension_max=0.15,
            market_median_day_return_max=0.05,
            organic_move_score_max=1.5,
            manipulation_risk_score_min=0.25,
            squeeze_risk_score_max=1.0,
            require_open_interest_context=True,
            exclude_symbols=(),
        ),
    )

    assert selected["symbol"].to_list() == ["GOODUSDT"]


def test_daily_close_fade_sleeves_compare_major_core_microcap(tmp_path) -> None:
    _write_close_fade_liquidity_fixture(tmp_path)

    payload = run_daily_close_fade_sleeves(
        tmp_path,
        fade_config=DailyCloseFadeConfig(
            signal_minute=23 * 60,
            top_n=3,
            hold_minutes=60,
            score="day_return",
            pump_filter="pump",
            min_age_days=10,
            liquidity_lookback_days=1,
            stop_loss_pct=0.0,
            exclude_symbols=(),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
    )

    results = read_dataset(tmp_path, "daily_close_fade_sleeves")
    trades = read_dataset(tmp_path, "daily_close_fade_sleeve_trades")

    assert payload["rows"]["results"] == 3
    assert set(results["sleeve"].to_list()) == {"major_control", "core", "microcap"}
    assert set(trades["sleeve"].to_list()) == {"major_control"}
    assert results.filter(pl.col("sleeve") == "core")["trade_count"].item() == 0
    assert results.filter(pl.col("sleeve") == "microcap")["trade_count"].item() == 0


def _feature_candidate(
    symbol: str,
    score: float,
    coin_excess: float,
    vwap_extension: float,
    late_volume_ratio: float,
) -> dict:
    return {
        "symbol": symbol,
        "date": "2026-01-01",
        "signal_ts_ms": 1_000,
        "signal_minute": 23 * 60,
        "eligible": True,
        "pump_like": True,
        "vol_adjusted_day_return": score,
        "day_return": score / 10.0,
        "day_turnover": 1_000_000.0,
        "last_60m_turnover": 100_000.0,
        "coin_excess_vs_market": coin_excess,
        "vwap_extension": vwap_extension,
        "late_volume_ratio": late_volume_ratio,
    }


def _write_close_fade_liquidity_fixture(tmp_path) -> None:
    start = datetime(2025, 1, 15, tzinfo=UTC)
    prior = start - timedelta(days=1)
    start_ms = int(start.timestamp() * 1000)
    symbols = {
        "BIGUSDT": {"pump": 0.50, "fade": 0.06, "prior_turnover": 3_000_000.0},
        "MIDUSDT": {"pump": 0.35, "fade": 0.05, "prior_turnover": 1_000_000.0},
        "TAILUSDT": {"pump": 0.30, "fade": 0.04, "prior_turnover": 300_000.0},
    }
    instruments = []
    klines = []
    for index, (symbol, spec) in enumerate(symbols.items(), start=1):
        base_price = 10.0 + index
        launch = start - timedelta(days=40)
        instruments.append(
            {
                "ts_ms": start_ms,
                "symbol": symbol,
                "category": "linear",
                "contract_type": "LinearPerpetual",
                "status": "Trading",
                "settle_coin": "USDT",
                "launch_time_ms": int(launch.timestamp() * 1000),
                "tick_size": 0.001,
                "qty_step": 0.001,
                "min_order_qty": 0.001,
                "min_notional_value": 5.0,
                "funding_interval_min": 480,
                "is_prelisting": False,
            }
        )
        previous = base_price
        for minute in range(24 * 60):
            ts_ms = int(prior.timestamp() * 1000) + minute * 60_000
            klines.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "open": previous,
                    "high": previous * 1.001,
                    "low": previous * 0.999,
                    "close": previous,
                    "volume_base": float(spec["prior_turnover"]) / (24 * 60) / previous,
                    "turnover_quote": float(spec["prior_turnover"]) / (24 * 60),
                    "source": "fixture",
                }
            )
        for minute in range(24 * 60 + 70):
            ts_ms = start_ms + minute * 60_000
            if minute <= 23 * 60:
                close = base_price * (1.0 + float(spec["pump"]) * minute / (23 * 60))
            else:
                minutes_after = minute - 23 * 60
                entry_price = base_price * (1.0 + float(spec["pump"]))
                close = entry_price * (1.0 - float(spec["fade"]) * min(minutes_after, 60) / 60.0)
            open_price = previous
            klines.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "open": open_price,
                    "high": max(open_price, close) * 1.001,
                    "low": min(open_price, close) * 0.999,
                    "close": close,
                    "volume_base": 100.0,
                    "turnover_quote": 100.0 * close,
                    "source": "fixture",
                }
            )
            previous = close

    write_dataset(pl.DataFrame(instruments), tmp_path, "instruments")
    write_dataset(pl.DataFrame(klines), tmp_path, "klines_1m")


def _write_close_fade_fixture(
    tmp_path,
    *,
    rebound: bool = False,
    adverse_reversal: bool = False,
    same_bar_stop_and_tp: bool = False,
) -> None:
    start = datetime(2025, 1, 15, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    symbols = {
        "OLD1USDT": {"pump": 0.30, "fade": 0.12, "launch_days": 40},
        "OLD2USDT": {"pump": 0.24, "fade": 0.08, "launch_days": 40},
        "OLD3USDT": {"pump": 0.08, "fade": 0.01, "launch_days": 40},
        "YOUNGUSDT": {"pump": 0.60, "fade": 0.20, "launch_days": 2},
    }
    instruments = []
    klines = []
    for index, (symbol, spec) in enumerate(symbols.items(), start=1):
        launch = start - timedelta(days=int(spec["launch_days"]))
        instruments.append(
            {
                "ts_ms": start_ms,
                "symbol": symbol,
                "category": "linear",
                "contract_type": "LinearPerpetual",
                "status": "Trading",
                "settle_coin": "USDT",
                "launch_time_ms": int(launch.timestamp() * 1000),
                "tick_size": 0.001,
                "qty_step": 0.001,
                "min_order_qty": 0.001,
                "min_notional_value": 5.0,
                "funding_interval_min": 480,
                "is_prelisting": False,
            }
        )
        base_price = 100.0 + index
        previous = base_price
        for minute in range(0, 24 * 60 + 70):
            ts_ms = start_ms + minute * 60_000
            if minute <= 23 * 60:
                close = base_price * (1.0 + float(spec["pump"]) * minute / (23 * 60))
            else:
                minutes_after = minute - 23 * 60
                entry_price = base_price * (1.0 + float(spec["pump"]))
                fade_price = entry_price * (1.0 - float(spec["fade"]) * min(minutes_after, 60) / 60.0)
                close = fade_price
                if adverse_reversal and symbol in {"OLD1USDT", "OLD2USDT"} and minutes_after >= 2:
                    close = entry_price * 1.10
                if rebound and symbol == "OLD1USDT":
                    if minutes_after == 2:
                        close = entry_price * 0.94
                    elif minutes_after >= 3:
                        close = entry_price * 0.985
            open_price = previous
            high = max(open_price, close) * 1.001
            low = min(open_price, close) * 0.999
            if rebound and symbol == "OLD1USDT" and minute == 23 * 60 + 3:
                high = base_price * (1.0 + float(spec["pump"])) * 0.965
            if same_bar_stop_and_tp and symbol == "OLD1USDT" and minute == 23 * 60 + 2:
                entry_price = base_price * (1.0 + float(spec["pump"]))
                high = entry_price * 1.21
                low = entry_price * 0.94
            klines.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume_base": 100.0,
                    "turnover_quote": 100.0 * close,
                    "source": "fixture",
                }
            )
            previous = close

    write_dataset(pl.DataFrame(instruments), tmp_path, "instruments")
    write_dataset(pl.DataFrame(klines), tmp_path, "klines_1m")


def _write_profit_protection_warm_start_fixture(tmp_path) -> None:
    start = datetime(2025, 1, 15, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    symbol = "WARMUSDT"
    instruments = [
        {
            "ts_ms": start_ms,
            "symbol": symbol,
            "category": "linear",
            "contract_type": "LinearPerpetual",
            "status": "Trading",
            "settle_coin": "USDT",
            "launch_time_ms": int((start - timedelta(days=40)).timestamp() * 1000),
            "tick_size": 0.001,
            "qty_step": 0.001,
            "min_order_qty": 0.001,
            "min_notional_value": 5.0,
            "funding_interval_min": 480,
            "is_prelisting": False,
        }
    ]
    klines = []
    previous = 100.0
    signal_minute = 22 * 60
    for minute in range(24 * 60 + 70):
        ts_ms = start_ms + minute * 60_000
        if minute <= signal_minute:
            close = 100.0 + 20.0 * minute / signal_minute
        elif minute < 23 * 60:
            close = 120.0 - 5.0 * (minute - signal_minute) / 60.0
        elif minute <= 23 * 60 + 5:
            close = 115.0 - 7.0 * (minute - 23 * 60) / 5.0
        elif minute <= 23 * 60 + 15:
            close = 108.0 + 11.0 * (minute - (23 * 60 + 5)) / 10.0
        else:
            close = 119.0
        open_price = previous
        high = max(open_price, close) * 1.001
        low = min(open_price, close) * 0.999
        klines.append(
            {
                "ts_ms": ts_ms,
                "symbol": symbol,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume_base": 100.0,
                "turnover_quote": 100.0 * close,
                "source": "fixture",
            }
        )
        previous = close

    write_dataset(pl.DataFrame(instruments), tmp_path, "instruments")
    write_dataset(pl.DataFrame(klines), tmp_path, "klines_1m")


def _write_twap_risk_fixture(
    tmp_path,
    *,
    spike_minute: int,
    spike_high: float,
    fade_after_spike: bool = False,
) -> None:
    start = datetime(2025, 1, 15, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    symbol = "RISKUSDT"
    instruments = [
        {
            "ts_ms": start_ms,
            "symbol": symbol,
            "category": "linear",
            "contract_type": "LinearPerpetual",
            "status": "Trading",
            "settle_coin": "USDT",
            "launch_time_ms": int((start - timedelta(days=40)).timestamp() * 1000),
            "tick_size": 0.001,
            "qty_step": 0.001,
            "min_order_qty": 0.001,
            "min_notional_value": 5.0,
            "funding_interval_min": 480,
            "is_prelisting": False,
        }
    ]
    klines = []
    previous = 80.0
    signal_minute = 22 * 60
    spike_absolute_minute = signal_minute + spike_minute
    for minute in range(24 * 60 + 240):
        ts_ms = start_ms + minute * 60_000
        if minute <= signal_minute:
            close = 80.0 + 20.0 * minute / signal_minute
        elif fade_after_spike and minute > spike_absolute_minute:
            close = 100.0 - 2.0 * min((minute - spike_absolute_minute) / 60.0, 1.0)
        else:
            close = 100.0
        open_price = previous
        high = max(open_price, close) * 1.001
        low = min(open_price, close) * 0.999
        if minute == spike_absolute_minute:
            high = spike_high
        klines.append(
            {
                "ts_ms": ts_ms,
                "symbol": symbol,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume_base": 100.0,
                "turnover_quote": 100.0 * close,
                "source": "fixture",
            }
        )
        previous = close

    write_dataset(pl.DataFrame(instruments), tmp_path, "instruments")
    write_dataset(pl.DataFrame(klines), tmp_path, "klines_1m")
