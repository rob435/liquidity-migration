from __future__ import annotations

from pathlib import Path

from aggression_carry.config import load_config


def test_volume_alpha_controls_load_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
volume_alpha:
  horizons_d: [1, 7]
  quantiles: [0.25, 0.50]
volume_backtest:
  score: dollar_volume_rank
  start_date: "2025-01-01"
  end_date: "2025-12-31"
  quantile: 0.30
  hold_days: 3
  rebalance_days: 3
  stop_mode: volatility
  stop_loss_pct: 0.05
  rank_exit_enabled: true
volume_grid:
  hold_days: [3, 7]
  fixed_stop_loss_pcts: [0.0, 0.20]
  rank_exit_modes: [false, true]
daily_close_fade:
  signal_minute: 1320
  entry_twap_minutes: 60
  liquidity_lookback_days: 9
  liquidity_rank_min: 31
  liquidity_rank_max: 150
  min_baseline_turnover: 123000.0
  account_equity: 25000.0
  max_position_weight: 0.20
  max_trade_notional_pct_of_day_turnover: 0.002
  max_trade_notional_pct_of_baseline_turnover: 0.005
  coin_excess_vs_market_min: 0.08
  coin_vwap_extension_min: 0.035
  coin_late_volume_ratio_min: 1.0
  position_sizing: score_capped
  score_weight_power: 1.0
  stop_loss_pct: 0.20
  take_profit_pct: 0.05
  vol_trailing_stop_mult: 0.5
  vol_trailing_activation_mult: 1.0
  mfe_giveback_activation_pct: 0.03
  mfe_giveback_pct: 0.40
  vwap_reversion_pct: 0.50
  stop_delay_minutes: 0
  profit_protection_delay_minutes: 15
  twap_stop_adding_pct: 0.08
daily_close_fade_grid:
  stop_loss_pcts: [0.0, 0.20]
  take_profit_pcts: [0.0, 0.03, 0.05]
  vol_trailing_stop_mults: [0.0, 0.5]
  vol_trailing_activation_mults: [0.0, 1.0]
  mfe_giveback_activation_pcts: [0.0, 0.03]
  mfe_giveback_pcts: [0.0, 0.40]
  vwap_reversion_pcts: [0.0, 0.50]
  liquidity_lookback_days: [7, 14]
  liquidity_rank_mins: [31, 81]
  liquidity_rank_maxs: [80, 150]
  min_baseline_turnovers: [0.0, 1000000.0]
  account_equities: [10000.0, 25000.0]
  max_position_weights: [0.0, 0.20]
  max_trade_notional_pct_day_turnovers: [0.0, 0.002]
  max_trade_notional_pct_baseline_turnovers: [0.0, 0.005]
forward_test:
  min_turnover_24h: 3000000.0
  max_spread_bps: 50.0
  max_entry_lag_minutes: 15
universe:
  min_turnover_24h: 5000000.0
  rank_start: 21
  rank_end: 80
  exclude_symbols: [BTCUSDT, ETHUSDT]
cost_model:
  maker_fee_bps: 1.0
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.volume_alpha.horizons_d == (1, 7)
    assert config.volume_alpha.quantiles == (0.25, 0.50)
    assert config.volume_backtest.start_date == "2025-01-01"
    assert config.volume_backtest.end_date == "2025-12-31"
    assert config.volume_backtest.quantile == 0.30
    assert config.volume_backtest.hold_days == 3
    assert config.volume_backtest.stop_mode == "volatility"
    assert config.volume_backtest.stop_loss_pct == 0.05
    assert config.volume_backtest.rank_exit_enabled is True
    assert config.volume_grid.hold_days == (3, 7)
    assert config.volume_grid.fixed_stop_loss_pcts == (0.0, 0.20)
    assert config.volume_grid.rank_exit_modes == (False, True)
    assert config.daily_close_fade.signal_minute == 1320
    assert config.daily_close_fade.entry_twap_minutes == 60
    assert config.daily_close_fade.liquidity_lookback_days == 9
    assert config.daily_close_fade.liquidity_rank_min == 31
    assert config.daily_close_fade.liquidity_rank_max == 150
    assert config.daily_close_fade.min_baseline_turnover == 123000.0
    assert config.daily_close_fade.account_equity == 25_000.0
    assert config.daily_close_fade.max_position_weight == 0.20
    assert config.daily_close_fade.max_trade_notional_pct_of_day_turnover == 0.002
    assert config.daily_close_fade.max_trade_notional_pct_of_baseline_turnover == 0.005
    assert config.daily_close_fade.coin_excess_vs_market_min == 0.08
    assert config.daily_close_fade.coin_vwap_extension_min == 0.035
    assert config.daily_close_fade.coin_late_volume_ratio_min == 1.0
    assert config.daily_close_fade.position_sizing == "score_capped"
    assert config.daily_close_fade.score_weight_power == 1.0
    assert config.daily_close_fade.stop_loss_pct == 0.20
    assert config.daily_close_fade.take_profit_pct == 0.05
    assert config.daily_close_fade.vol_trailing_stop_mult == 0.5
    assert config.daily_close_fade.vol_trailing_activation_mult == 1.0
    assert config.daily_close_fade.mfe_giveback_activation_pct == 0.03
    assert config.daily_close_fade.mfe_giveback_pct == 0.40
    assert config.daily_close_fade.vwap_reversion_pct == 0.50
    assert config.daily_close_fade.stop_delay_minutes == 0
    assert config.daily_close_fade.profit_protection_delay_minutes == 15
    assert config.daily_close_fade.twap_stop_adding_pct == 0.08
    assert config.daily_close_fade_grid.stop_loss_pcts == (0.0, 0.20)
    assert config.daily_close_fade_grid.take_profit_pcts == (0.0, 0.03, 0.05)
    assert config.daily_close_fade_grid.vol_trailing_stop_mults == (0.0, 0.5)
    assert config.daily_close_fade_grid.vol_trailing_activation_mults == (0.0, 1.0)
    assert config.daily_close_fade_grid.mfe_giveback_activation_pcts == (0.0, 0.03)
    assert config.daily_close_fade_grid.mfe_giveback_pcts == (0.0, 0.40)
    assert config.daily_close_fade_grid.vwap_reversion_pcts == (0.0, 0.50)
    assert config.daily_close_fade_grid.liquidity_lookback_days == (7, 14)
    assert config.daily_close_fade_grid.liquidity_rank_mins == (31, 81)
    assert config.daily_close_fade_grid.liquidity_rank_maxs == (80, 150)
    assert config.daily_close_fade_grid.min_baseline_turnovers == (0.0, 1_000_000.0)
    assert config.daily_close_fade_grid.account_equities == (10_000.0, 25_000.0)
    assert config.daily_close_fade_grid.max_position_weights == (0.0, 0.20)
    assert config.daily_close_fade_grid.max_trade_notional_pct_day_turnovers == (0.0, 0.002)
    assert config.daily_close_fade_grid.max_trade_notional_pct_baseline_turnovers == (0.0, 0.005)
    assert config.forward_test.min_turnover_24h == 3_000_000.0
    assert config.forward_test.max_spread_bps == 50.0
    assert config.forward_test.max_entry_lag_minutes == 15
    assert config.universe.min_turnover_24h == 5_000_000.0
    assert config.universe.rank_start == 21
    assert config.universe.rank_end == 80
    assert config.universe.exclude_symbols == ("BTCUSDT", "ETHUSDT")
    assert config.costs.maker_fee_bps == 1.0
