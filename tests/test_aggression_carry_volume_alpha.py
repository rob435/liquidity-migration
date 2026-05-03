from __future__ import annotations

from pathlib import Path

from aggression_carry.ingestion import generate_fixture_data
from aggression_carry.storage import read_dataset
from aggression_carry.config import VolumeBacktestConfig, VolumeGridConfig
from aggression_carry.volume_alpha import build_volume_features, run_volume_alpha
from aggression_carry.volume_backtest import iter_grid_configs, run_volume_grid, run_volume_trade_backtest


def test_volume_alpha_isolated_daily_research_path(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")

    features = build_volume_features(klines)
    assert "volume_change_1d_z" in features.columns
    assert "volume_composite" in features.columns
    assert "liquidity_rank" in features.columns
    assert "liquidity_rank_pct" in features.columns

    payload = run_volume_alpha(tmp_path, horizons_d=(1, 3), quantiles=(0.50,))

    signal_names = {item["signal"] for item in payload["metrics"]}
    portfolio_scores = {item["score"] for item in payload["portfolios"]}
    assert "volume_change_1d" in signal_names
    assert "volume_composite" in signal_names
    assert "volume_composite" in portfolio_scores
    assert payload["best_base_portfolio"]
    assert (tmp_path / "reports" / "volume_alpha_report.md").exists()
    assert (tmp_path / "volume_alpha_features").exists()
    assert (tmp_path / "volume_alpha_metrics").exists()
    assert (tmp_path / "volume_alpha_portfolios").exists()


def test_volume_backtest_writes_trade_ledger(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    payload = run_volume_trade_backtest(
        tmp_path,
        backtest_config=VolumeBacktestConfig(hold_days=1, rebalance_days=1, stop_loss_pct=0.08),
    )

    trades = read_dataset(tmp_path, "volume_backtest_trades")
    baskets = read_dataset(tmp_path, "volume_backtest_baskets")
    equity = read_dataset(tmp_path, "volume_backtest_equity")

    assert payload["rows"]["trades"] > 0
    assert {"entry_ts_ms", "exit_ts_ms", "exit_reason", "net_return"}.issubset(set(trades.columns))
    assert baskets.height > 0
    assert equity.height == baskets.height
    assert (tmp_path / "reports" / "volume_backtest_report.md").exists()
    assert (tmp_path / "reports" / "volume_backtest_trades.csv").exists()
    assert (tmp_path / "reports" / "volume_backtest_equity_vs_btc.csv").exists()
    assert (tmp_path / "reports" / "volume_backtest_monthly_vs_btc.csv").exists()
    assert (tmp_path / "reports" / "volume_backtest_equity_curve.svg").exists()
    assert (tmp_path / "reports" / "volume_backtest_monthly_vs_btc.svg").exists()
    assert (tmp_path / "volume_backtest_monthly").exists()
    assert (tmp_path / "volume_backtest_equity_vs_btc").exists()


def test_volume_backtest_can_filter_daily_liquidity_bucket(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    payload = run_volume_trade_backtest(
        tmp_path,
        backtest_config=VolumeBacktestConfig(
            hold_days=1,
            rebalance_days=1,
            stop_loss_pct=0.0,
            universe_rank_min=1,
            universe_rank_max=4,
        ),
    )

    trades = read_dataset(tmp_path, "volume_backtest_trades")
    assert payload["rows"]["trades"] > 0
    assert trades["symbol"].n_unique() <= 4


def test_volume_backtest_records_stop_loss_exit_reason(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    payload = run_volume_trade_backtest(
        tmp_path,
        backtest_config=VolumeBacktestConfig(hold_days=1, rebalance_days=1, stop_loss_pct=0.0001),
    )

    exit_reasons = {item["exit_reason"] for item in payload["exit_reasons"]}
    assert "stop_loss" in exit_reasons


def test_volume_backtest_zero_stop_disables_stop_loss(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    payload = run_volume_trade_backtest(
        tmp_path,
        backtest_config=VolumeBacktestConfig(hold_days=1, rebalance_days=1, stop_loss_pct=0.0),
    )

    exit_reasons = {item["exit_reason"] for item in payload["exit_reasons"]}
    assert "stop_loss" not in exit_reasons


def test_volume_grid_runs_parallel_fixture(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    payload = run_volume_grid(
        tmp_path,
        grid_config=VolumeGridConfig(
            quantiles=(0.50,),
            hold_days=(1,),
            fixed_stop_loss_pcts=(0.0, 0.0001),
            vol_stop_multipliers=(),
            rank_exit_modes=(False, True),
        ),
        base_backtest_config=VolumeBacktestConfig(hold_days=1, rebalance_days=1),
        max_workers=2,
    )

    assert payload["rows"] == 4
    assert payload["workers"] == 2
    assert payload["best_total_return"]
    assert (tmp_path / "reports" / "volume_grid_report.md").exists()
    assert (tmp_path / "reports" / "volume_grid_results.csv").exists()
    assert (tmp_path / "volume_backtest_grid").exists()


def test_iter_grid_configs_includes_fixed_vol_and_rank_variants() -> None:
    configs = iter_grid_configs(
        VolumeGridConfig(
            quantiles=(0.50,),
            hold_days=(3,),
            fixed_stop_loss_pcts=(0.0, 0.20),
            vol_stop_multipliers=(3.0,),
            rank_exit_modes=(False, True),
            include_reverse_side=True,
        )
    )

    assert len(configs) == 12
    assert {config.stop_mode for config in configs} == {"none", "fixed", "volatility"}
    assert {config.rank_exit_enabled for config in configs} == {False, True}
    assert {config.side_mode for config in configs} == {"long_high_short_low", "short_high_long_low"}


def test_iter_grid_configs_preserves_universe_bucket() -> None:
    configs = iter_grid_configs(
        VolumeGridConfig(quantiles=(0.50,), hold_days=(3,), fixed_stop_loss_pcts=(0.0,), vol_stop_multipliers=()),
        VolumeBacktestConfig(universe_rank_min=21, universe_rank_max=80, exclude_symbols=("BTCUSDT",)),
    )

    assert len(configs) == 2
    assert {config.universe_rank_min for config in configs} == {21}
    assert {config.universe_rank_max for config in configs} == {80}
    assert {config.exclude_symbols for config in configs} == {("BTCUSDT",)}
