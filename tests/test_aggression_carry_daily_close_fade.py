from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from aggression_carry.config import CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig
from aggression_carry.daily_close_fade import run_daily_close_fade, run_daily_close_fade_grid
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
            trailing_stop_pcts=(0.0,),
            trailing_activation_pcts=(0.0,),
            cost_multipliers=(1.0,),
        ),
        cost_config=CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0),
        max_workers=1,
    )

    grid = read_dataset(tmp_path, "daily_close_fade_grid")

    assert payload["rows"] == 4
    assert grid.height == 4
    assert grid["total_return"].max() > 0.0


def _write_close_fade_fixture(tmp_path, *, rebound: bool = False) -> None:
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
