from __future__ import annotations

from dataclasses import replace

import polars as pl
import pytest

import liquidity_migration.research.backtest.long_native as long_backtest
from liquidity_migration.core._common import MS_PER_HOUR
from liquidity_migration.core.config import CostConfig
from liquidity_migration.rules.long_config import ConfigLayer, resolve_strategy_config
from liquidity_migration.rules.long_native import long_v11a_profile


def _effective(rule):
    costs = CostConfig()
    return resolve_strategy_config(
        "v11a",
        rule=rule,
        layers=(
            ConfigLayer(
                source="test",
                values={
                    "round_trip_cost_bps": (
                        costs.base_entry_exit_cost_bps * rule.cost_multiplier
                    )
                },
            ),
        ),
    )


def _signal_feature(symbol: str, *, score: float) -> dict[str, object]:
    return {
        "ts_ms": MS_PER_HOUR,
        "symbol": symbol,
        "close": 100.0,
        "in_universe": True,
        "regime_on": True,
        "eth_regime_on": True,
        "today_volume_rank": 1,
        "log_return": score,
        "pump_3d_log": 0.10,
        "pump_7d_log": 0.20,
        "sigma_daily_30d": 0.05,
        "close_location": 0.85,
        "close_loc_3d": 0.70,
        "close_loc_7d": 0.70,
        "atr_14d_pct": 0.05,
        "realized_vol": 0.60,
        "btc_rv_30": 0.60,
    }


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


def test_historical_touch_uses_closed_low_then_next_open_and_keeps_score_priority() -> None:
    features = pl.DataFrame(
        [
            _signal_feature("AAAUSDT", score=0.20),
            _signal_feature("ZZZUSDT", score=0.40),
        ]
    )
    klines = pl.DataFrame(
        _symbol_bars("AAAUSDT", next_open=102.0)
        + _symbol_bars("ZZZUSDT", next_open=103.0)
    )
    rule = replace(long_v11a_profile(), max_concurrent_positions=1)
    trades, stats, _events = long_backtest._run_long_pipeline(
        features=features,
        bars_by_symbol=long_backtest._bars_by_symbol(klines),
        funding_lookup=None,
        config=rule,
        effective_config=_effective(rule),
    )

    assert trades.height == 1
    trade = trades.to_dicts()[0]
    assert trade["symbol"] == "ZZZUSDT"
    assert trade["entry_ts_ms"] == 2 * MS_PER_HOUR
    assert trade["entry_price"] == 103.0
    assert stats["skipped_capacity"] == 1


def test_standard_pipeline_uses_one_persistent_rust_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_contract = long_backtest.RustStrategyContract
    instances: list[object] = []

    class RecordingContract:
        def __init__(self) -> None:
            self.inner = real_contract()
            self.entered = 0
            self.exited = 0
            self.requests = 0
            instances.append(self)

        def __enter__(self) -> RecordingContract:
            self.inner.__enter__()
            self.entered += 1
            return self

        def request(self, payload: dict[str, object]) -> dict[str, object]:
            self.requests += 1
            assert payload["operation"] == "long_decide"
            return self.inner.request(payload)

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            self.exited += 1
            self.inner.__exit__(exc_type, exc, traceback)

    monkeypatch.setattr(long_backtest, "RustStrategyContract", RecordingContract)
    features = pl.DataFrame([_signal_feature("AAAUSDT", score=0.20)])
    rows = _symbol_bars("AAAUSDT", next_open=100.0)
    rule = long_v11a_profile()

    trades, _stats, _events = long_backtest._run_long_pipeline(
        features=features,
        bars_by_symbol=long_backtest._bars_by_symbol(pl.DataFrame(rows)),
        funding_lookup=None,
        config=rule,
        effective_config=_effective(rule),
    )

    assert trades.height == 1
    assert len(instances) == 1
    instance = instances[0]
    assert instance.entered == 1
    assert instance.exited == 1
    assert instance.requests >= 2


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


def test_historical_deadline_fallthrough_also_enters_at_the_next_open() -> None:
    features = pl.DataFrame([_signal_feature("AAAUSDT", score=0.20)])
    rows = _symbol_bars("AAAUSDT", next_open=107.0)
    rows[1]["low"] = 100.0
    rule = replace(long_v11a_profile(), fc_sniper_deadline_hours=1)
    trades, _stats, _events = long_backtest._run_long_pipeline(
        features=features,
        bars_by_symbol=long_backtest._bars_by_symbol(pl.DataFrame(rows)),
        funding_lookup=None,
        config=rule,
        effective_config=_effective(rule),
    )

    assert trades["entry_ts_ms"].to_list() == [2 * MS_PER_HOUR]
    assert trades["entry_price"].to_list() == [107.0]


def test_historical_high_does_not_reintroduce_the_removed_take_profit() -> None:
    features = pl.DataFrame([_signal_feature("AAAUSDT", score=0.20)])
    rows = _symbol_bars("AAAUSDT", next_open=100.0)
    rows[2]["high"] = 150.0
    rule = long_v11a_profile()
    trades, stats, _events = long_backtest._run_long_pipeline(
        features=features,
        bars_by_symbol=long_backtest._bars_by_symbol(pl.DataFrame(rows)),
        funding_lookup=None,
        config=rule,
        effective_config=_effective(rule),
    )

    assert trades["exit_reason"].to_list() == ["data_end"]
    assert "take_profit_price" not in trades.columns
    assert "exits_take_profit" not in stats
