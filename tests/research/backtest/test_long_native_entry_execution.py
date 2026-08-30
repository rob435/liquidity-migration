from __future__ import annotations

from dataclasses import replace

import polars as pl

import liquidity_migration.research.backtest.long_native as long_backtest
from liquidity_migration.core._common import MS_PER_HOUR
from liquidity_migration.core.config import CostConfig
from liquidity_migration.rules.long_native import long_v11a_profile


def _symbol_bars(symbol: str, *, next_open: float = 103.0) -> list[dict[str, object]]:
    return [
        {
            "ts_ms": 0,
            "symbol": symbol,
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.0,
        },
        {
            "ts_ms": MS_PER_HOUR,
            "symbol": symbol,
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 101.0,
        },
        {
            "ts_ms": 2 * MS_PER_HOUR,
            "symbol": symbol,
            "open": next_open,
            "high": next_open + 1.0,
            "low": next_open - 1.0,
            "close": next_open,
        },
    ]


def test_historical_touch_uses_closed_low_then_next_open_and_keeps_score_priority(
    monkeypatch,
) -> None:
    features = pl.DataFrame(
        [
            {"ts_ms": MS_PER_HOUR, "symbol": "AAAUSDT", "log_return": 0.20},
            {"ts_ms": MS_PER_HOUR, "symbol": "ZZZUSDT", "log_return": 0.40},
        ]
    )
    klines = pl.DataFrame(
        _symbol_bars("AAAUSDT", next_open=102.0)
        + _symbol_bars("ZZZUSDT", next_open=103.0)
    )
    monkeypatch.setattr(
        long_backtest,
        "_classify_entry",
        lambda _row, _config: ("fomo_chase", 0.20, 0.50, 3),
    )

    trades, stats, _events = long_backtest._run_long_pipeline(
        features=features,
        bars_by_symbol=long_backtest._bars_by_symbol(klines),
        funding_lookup=None,
        config=replace(long_v11a_profile(), max_concurrent_positions=1),
        costs=CostConfig(),
    )

    assert trades.height == 1
    trade = trades.to_dicts()[0]
    assert trade["symbol"] == "ZZZUSDT"
    assert trade["entry_ts_ms"] == 2 * MS_PER_HOUR
    assert trade["entry_price"] == 103.0
    assert stats["skipped_capacity"] == 1


def test_historical_entry_requires_a_contiguous_next_hour() -> None:
    klines = pl.DataFrame(
        [
            *_symbol_bars("AAAUSDT")[:2],
            {
                "ts_ms": 3 * MS_PER_HOUR,
                "symbol": "AAAUSDT",
                "open": 103.0,
                "high": 104.0,
                "low": 102.0,
                "close": 103.0,
            },
        ]
    )
    bars = long_backtest._bars_by_symbol(klines)["AAAUSDT"]

    assert (
        long_backtest._entry_at_next_hour_open(
            bars,
            observed_bar_idx=1,
            window_end_ts_ms=None,
        )
        is None
    )


def test_historical_deadline_fallthrough_also_enters_at_the_next_open(monkeypatch) -> None:
    features = pl.DataFrame(
        [{"ts_ms": MS_PER_HOUR, "symbol": "AAAUSDT", "log_return": 0.20}]
    )
    rows = _symbol_bars("AAAUSDT", next_open=107.0)
    rows[1]["low"] = 100.0
    monkeypatch.setattr(
        long_backtest,
        "_classify_entry",
        lambda _row, _config: ("fomo_chase", 0.20, 0.50, 3),
    )

    trades, _stats, _events = long_backtest._run_long_pipeline(
        features=features,
        bars_by_symbol=long_backtest._bars_by_symbol(pl.DataFrame(rows)),
        funding_lookup=None,
        config=replace(long_v11a_profile(), fc_sniper_deadline_hours=1),
        costs=CostConfig(),
    )

    assert trades["entry_ts_ms"].to_list() == [2 * MS_PER_HOUR]
    assert trades["entry_price"].to_list() == [107.0]
