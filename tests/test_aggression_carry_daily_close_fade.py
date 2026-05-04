from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from aggression_carry import daily_close_fade as daily_close_fade_module
from aggression_carry.config import CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig
from aggression_carry.daily_close_fade import (
    DailyCloseFadeDiagnosticsConfig,
    _short_return,
    run_daily_close_fade,
    run_daily_close_fade_diagnostics,
    run_daily_close_fade_grid,
    run_daily_close_fade_sleeves,
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
    assert trades["symbol"].to_list() == ["OLD1USDT", "OLD2USDT"]
    assert "YOUNGUSDT" not in trades["symbol"].to_list()
    assert set(trades["exit_reason"].to_list()) == {"max_hold"}
    assert trades["net_return"].min() > 0.0
    young = features.filter(pl.col("symbol") == "YOUNGUSDT").row(0, named=True)
    assert young["eligible"] is False
    assert young["age_days"] < 10.0


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
            cost_multipliers=(1.0,),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
        max_workers=1,
    )

    grid = read_dataset(tmp_path, "daily_close_fade_grid")

    assert payload["rows"] == 8
    assert grid.height == 8
    assert grid["total_return"].max() > 0.0


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
