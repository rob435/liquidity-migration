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
    assert config.volume_backtest.quantile == 0.30
    assert config.volume_backtest.hold_days == 3
    assert config.volume_backtest.stop_mode == "volatility"
    assert config.volume_backtest.stop_loss_pct == 0.05
    assert config.volume_backtest.rank_exit_enabled is True
    assert config.volume_grid.hold_days == (3, 7)
    assert config.volume_grid.fixed_stop_loss_pcts == (0.0, 0.20)
    assert config.volume_grid.rank_exit_modes == (False, True)
    assert config.universe.min_turnover_24h == 5_000_000.0
    assert config.universe.rank_start == 21
    assert config.universe.rank_end == 80
    assert config.universe.exclude_symbols == ("BTCUSDT", "ETHUSDT")
    assert config.costs.maker_fee_bps == 1.0
