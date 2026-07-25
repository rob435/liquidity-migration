"""Focused contracts for the active continuous historical equity engine."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.account_kernel import AccountRiskPolicy
from liquidity_migration.continuous_events import (
    ContinuousEventConfig,
    _btc_trend_returns,
    _fresh_entries,
    _notional_weight,
    _round_trip_bps,
    _run_trades,
    build_continuous_panel,
    cross_sectional_decile,
    run_continuous_equity_component,
)
from liquidity_migration.execution_adapters import ExecutionTwinConfig, LatencyProfile
from liquidity_migration.historical_account_replay import (
    HistoricalAccountSession,
    synthetic_historical_rules_for_symbols,
)
from liquidity_migration.storage import write_dataset
from liquidity_migration.trade_lifecycle import _indexed_price_bars_by_symbol


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _grid_klines(symbols: list[str], hours: int, *, start_ms: int = 0) -> pl.DataFrame:
    rows = []
    for symbol_index, symbol in enumerate(symbols):
        for hour in range(hours):
            price = 100.0 + symbol_index + hour * 0.1
            rows.append(
                {
                    "ts_ms": start_ms + hour * MS_PER_HOUR,
                    "symbol": symbol,
                    "open": price,
                    "high": price * 1.001,
                    "low": price * 0.999,
                    "close": price,
                }
            )
    return pl.DataFrame(rows)


def _active_test_config(**updates: object) -> ContinuousEventConfig:
    values = {
        "age_days_min": 0,
        "btc_trend_gate": "off",
        "sizing_mode": "flat",
        "take_profit_pct": 0.0,
        # Noise-free fixture: each test enables exactly the exit it exercises.
        "stop_loss_pct": 0.0,
        "use_funding": False,
    }
    values.update(updates)
    return ContinuousEventConfig(**values)


def test_config_hash_ignores_routing_and_margin_only() -> None:
    base = ContinuousEventConfig()
    routed = ContinuousEventConfig(execution_strategy_id="component", execution_leverage=4.0)

    assert base.config_hash() == routed.config_hash()
    assert base.kernel_strategy_id != routed.kernel_strategy_id


def test_panel_requires_explicit_end_date(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicit end_date"):
        build_continuous_panel(tmp_path, ContinuousEventConfig(), cache=False)


def test_round_trip_cost_is_capacity_aware() -> None:
    config = _active_test_config(gross_exposure=0.5, max_active=25)
    weight = config.notional_weight
    participation = weight * config.deploy_capital_usd / 1_000_000.0
    expected = 2.0 * (config.taker_fee_bps + config.spread_bps)
    expected += 2.0 * config.impact_coef_bps * participation**config.impact_exponent

    assert _round_trip_bps(config, 1_000_000.0) == pytest.approx(expected)
    assert _round_trip_bps(config, 4_000_000.0) < expected


def test_inverse_vol_sizing_is_bounded() -> None:
    config = _active_test_config(
        sizing_mode="inverse_vol",
        target_vol_per_name=0.01,
        vol_weight_clamp=2.0,
    )
    close = pl.Series([100.0 * (1.001**index) for index in range(200)]).to_numpy()

    weight = _notional_weight(config, close, 199)

    assert config.notional_weight / 2.0 <= weight <= config.notional_weight * 2.0


def test_btc_trend_uses_only_returns_before_signal_day() -> None:
    rows = []
    for day, close in enumerate((100.0, 110.0, 121.0, 60.5)):
        rows.append(
            {
                "ts_ms": day * MS_PER_DAY + 23 * MS_PER_HOUR,
                "symbol": "BTCUSDT",
                "close": close,
            }
        )
    trend = _btc_trend_returns(pl.DataFrame(rows), lookback_days=2)

    assert trend[3 * MS_PER_DAY] == pytest.approx(0.2)


def test_fresh_entries_filters_liquidity_after_spell_detection() -> None:
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "A", "B"],
            "ts_ms": [0, MS_PER_HOUR, 3 * MS_PER_HOUR, 0],
            "decile": [9, 9, 9, 8],
            "composite": [1.0, 1.0, 1.0, 0.5],
            "turnover_quote": [100.0, 1_000_000.0, 1_000_000.0, 1_000_000.0],
        }
    )
    entries = _fresh_entries(panel, _active_test_config(liq_turnover_min=500_000.0))

    assert entries.select("symbol", "ts_ms").rows() == [("A", 3 * MS_PER_HOUR)]


def test_run_trades_uses_fixed_market_entry_and_hold() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 12))
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1_000_000.0]}
    )
    config = _active_test_config(entry_delay_hours=1, hold_hours=3)

    trades, skips = _run_trades(entries, bars, None, config)

    assert skips["skipped_no_bar"] == 0
    assert trades.height == 1
    assert trades["entry_ts_ms"][0] == 2 * MS_PER_HOUR
    assert trades["exit_ts_ms"][0] == 5 * MS_PER_HOUR
    assert trades["side"][0] == "short"


def test_data_end_exit_is_finalized_before_later_account_decisions(tmp_path: Path) -> None:
    klines = pl.concat(
        [
            _grid_klines(["A"], 3),
            _grid_klines(["B"], 12),
        ]
    )
    bars = _indexed_price_bars_by_symbol(klines)
    entries = pl.DataFrame(
        {
            "symbol": ["A", "B"],
            "ts_ms": [0, 4 * MS_PER_HOUR],
            "composite": [0.9, 0.8],
            "turnover_quote": [1_000_000.0, 1_000_000.0],
        }
    )
    config = _active_test_config(
        execution_strategy_id="continuous-data-end-order-test",
        hold_hours=4,
        max_active=10,
    )
    session = HistoricalAccountSession(
        tmp_path / "account",
        account_id="continuous-data-end-order-test",
        risk_policy=AccountRiskPolicy(1e12, 1e12, 1e12, 1e12, 1.0),
        instrument_rules=synthetic_historical_rules_for_symbols(
            ["A", "B"], max_leverage=1.0, observed_ts_ns=1
        ),
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="continuous-data-end-order-test",
    )

    trades, skips = _run_trades(entries, bars, None, config, kernel_session=session)

    a_trade = trades.filter(pl.col("symbol") == "A").row(0, named=True)
    assert skips["skipped_account_kernel"] == 0
    assert trades.height == 2
    assert a_trade["exit_reason"] == "data_end"
    assert a_trade["exit_ts_ms"] == 3 * MS_PER_HOUR
    assert session._last_wall_ts_ns == int(trades["exit_ts_ms"].max()) * 1_000_000


def test_run_trades_applies_daily_btc_gate_and_crowding() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A", "B"], 12))
    entries = pl.DataFrame(
        {
            "symbol": ["A", "B"],
            "ts_ms": [0, 0],
            "composite": [0.9, 0.8],
            "turnover_quote": [1_000_000.0, 1_000_000.0],
        }
    )
    crowded = _active_test_config(
        btc_trend_gate="uptrend",
        entry_crowding_max_fresh=1,
        hold_hours=3,
    )

    trades, skips = _run_trades(entries, bars, None, crowded, {0: 0.1})

    assert trades.is_empty()
    assert skips["skipped_crowding"] == 2


def test_account_kernel_rejection_does_not_create_trade(tmp_path: Path) -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 12))
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1_000_000.0]}
    )
    config = _active_test_config(
        execution_strategy_id="continuous-feedback-test",
        execution_leverage=10.0,
        gross_exposure=0.5,
        max_active=1,
        hold_hours=3,
    )
    session = HistoricalAccountSession(
        tmp_path / "account",
        account_id="continuous-feedback-test",
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 100.0, 10.0),
        instrument_rules=synthetic_historical_rules_for_symbols(
            ["A"], max_leverage=10.0, observed_ts_ns=1
        ),
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="continuous-feedback-test",
    )

    trades, skips = _run_trades(entries, bars, None, config, kernel_session=session)

    assert trades.is_empty()
    assert skips["skipped_account_kernel"] == 1


def test_take_profit_reference_owns_decision_symbol_book(tmp_path: Path) -> None:
    klines = _grid_klines(["A"], 8).with_columns(
        pl.when(pl.col("ts_ms") == 2 * MS_PER_HOUR)
        .then(pl.lit(80.0))
        .otherwise(pl.col("low"))
        .alias("low"),
        pl.when(pl.col("ts_ms") == 2 * MS_PER_HOUR)
        .then(pl.lit(95.0))
        .otherwise(pl.col("close"))
        .alias("close"),
    )
    bars = _indexed_price_bars_by_symbol(klines)
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1_000_000.0]}
    )
    config = _active_test_config(
        execution_strategy_id="continuous-tp-reference-test",
        execution_leverage=10.0,
        take_profit_pct=0.10,
        hold_hours=4,
    )
    session = HistoricalAccountSession(
        tmp_path / "account",
        account_id="continuous-tp-reference-test",
        risk_policy=AccountRiskPolicy(1e12, 1e12, 1e12, 1e12, 10.0),
        instrument_rules=synthetic_historical_rules_for_symbols(
            ["A"], max_leverage=10.0, observed_ts_ns=1
        ),
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="continuous-tp-reference-test",
    )

    trades, skips = _run_trades(entries, bars, None, config, kernel_session=session)

    assert skips["skipped_account_kernel"] == 0
    assert trades.height == 1
    assert trades["exit_reason"][0] == "take_profit"
    assert trades["exit_price"][0] == pytest.approx(trades["entry_price"][0] * 0.90)
    assert all(output.target_result.accepted for output in session.outputs)


def test_declared_stop_loss_exits_short_at_capped_slippage() -> None:
    # A short breaches its declared stop when the bar HIGH crosses
    # entry * (1 + stop_loss_pct); the fill is the bar extreme capped at 10%
    # beyond the trigger (stop_fill_mode="bar_extreme_capped"), so a single
    # thin wick cannot dictate the fill. §16.3/§20 parity: this is the modeled
    # twin of the venue stop the account places from metadata stop_loss_pct.
    klines = _grid_klines(["A"], 8).with_columns(
        pl.when(pl.col("ts_ms") == 2 * MS_PER_HOUR)
        .then(pl.lit(150.0))
        .otherwise(pl.col("high"))
        .alias("high"),
    )
    bars = _indexed_price_bars_by_symbol(klines)
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1_000_000.0]}
    )
    config = _active_test_config(stop_loss_pct=0.20, hold_hours=4)

    trades, _skips = _run_trades(entries, bars, None, config)

    assert trades.height == 1
    entry_price = trades["entry_price"][0]
    assert trades["exit_reason"][0] == "stop_loss"
    assert trades["stop_price"][0] == pytest.approx(entry_price * 1.20)
    # Bar high 150 is far beyond the trigger, so the fill is the slippage cap.
    assert trades["exit_price"][0] == pytest.approx(entry_price * 1.20 * 1.10)


def test_stop_disabled_still_runs_to_max_hold() -> None:
    # stop_loss_pct=0.0 must reproduce the pre-sl35 no-stop reconstruction.
    klines = _grid_klines(["A"], 8).with_columns(
        pl.when(pl.col("ts_ms") == 2 * MS_PER_HOUR)
        .then(pl.lit(150.0))
        .otherwise(pl.col("high"))
        .alias("high"),
    )
    bars = _indexed_price_bars_by_symbol(klines)
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1_000_000.0]}
    )
    config = _active_test_config(stop_loss_pct=0.0, hold_hours=4)

    trades, _skips = _run_trades(entries, bars, None, config)

    assert trades.height == 1
    assert trades["exit_reason"][0] == "max_hold"
    assert trades["stop_price"][0] is None


def test_cross_sectional_decile_keeps_singleton() -> None:
    klines = pl.DataFrame(
        {
            "symbol": ["LONE"],
            "ts_ms": [3 * MS_PER_HOUR],
            "turnover_quote": [1_000_000.0],
            "ret168": [0.01],
            "ret72": [0.0],
            "rv_168h": [0.02],
            "vov": [0.001],
            "dist_low": [0.5],
        }
    )
    rmom = pl.DataFrame(
        {"symbol": ["LONE"], "day_ts": [0], "residual_momentum": [-0.005]}
    )

    panel = cross_sectional_decile(klines, rmom, rmom_quantile=0.5)

    assert panel.height == 1
    assert panel["composite"][0] == pytest.approx(0.0)


def _synthetic_root(tmp_path: Path, *, symbols: int = 60, hours: int = 720) -> tuple[Path, int]:
    root = tmp_path / "full_pit"
    root.mkdir()
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    rows = []
    for symbol_index in range(symbols):
        price = 100.0 + symbol_index
        amplitude = 0.005 + 0.02 * symbol_index / max(symbols - 1, 1)
        for hour in range(hours):
            wobble = 1.0 + amplitude * ((symbol_index * 7 + hour * 13) % 11 - 5) / 5.0
            price = max(1.0, price * wobble)
            rows.append(
                {
                    "ts_ms": start + hour * MS_PER_HOUR,
                    "symbol": f"S{symbol_index:02d}",
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "volume_base": 1_000.0,
                    "turnover_quote": 1_000_000.0,
                }
            )
    klines = pl.DataFrame(rows)
    write_dataset(klines, root, "klines_1h")
    funding = (
        klines.select("ts_ms", "symbol")
        .filter((pl.col("ts_ms") // MS_PER_HOUR) % 8 == 0)
        .with_columns(
            pl.lit(0.0001).alias("funding_rate"),
            pl.lit(480).alias("funding_interval_min"),
        )
    )
    write_dataset(funding, root, "funding")
    days = sorted({(start + hour * MS_PER_HOUR) // MS_PER_DAY * MS_PER_DAY for hour in range(hours)})
    rmom = pl.DataFrame(
        [
            {
                "symbol": f"S{symbol_index:02d}",
                "ts_ms": day,
                "residual_momentum": (symbol_index % 13) * 0.001 - 0.006,
                "is_provisional": False,
            }
            for day in days
            for symbol_index in range(symbols)
        ]
    )
    rmom.write_parquet(root / "residual_momentum.parquet")
    return root, start


def test_active_run_writes_equity_and_account_receipt(tmp_path: Path) -> None:
    root, start = _synthetic_root(tmp_path)
    config = _active_test_config(
        start_date=_iso(start + 8 * MS_PER_DAY),
        end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6,
        max_active=10,
        split_date=_iso(start + 16 * MS_PER_DAY),
        use_funding=True,
    )

    panel = build_continuous_panel(root, config, cache=False)
    assert 9 in set(panel["decile"].to_list())

    report_dir = tmp_path / "report"
    payload = run_continuous_equity_component(root, config=config, report_dir=report_dir)

    assert payload["n_trades"] > 0
    assert payload["account_journal"]["strategy_targets"] == payload["n_trades"] * 2
    assert payload["account_journal"]["account_kernel_feedback_online"] is True
    assert (report_dir / "continuous_report.json").exists()
    assert (report_dir / "continuous_trades.csv").exists()
    assert (report_dir / "continuous_mtm_equity.csv").exists()
