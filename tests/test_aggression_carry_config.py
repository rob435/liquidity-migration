from __future__ import annotations

from pathlib import Path

from aggression_carry.config import DEFAULT_EXCLUDED_SYMBOLS, load_config


def test_active_system_config_loads_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
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

    assert config.universe.min_turnover_24h == 5_000_000.0
    assert config.universe.rank_start == 21
    assert config.universe.rank_end == 80
    assert config.universe.exclude_symbols == ("BTCUSDT", "ETHUSDT")
    assert config.costs.maker_fee_bps == 1.0


def test_default_config_excludes_only_stable_and_peg_symbols() -> None:
    config = load_config()

    assert config.universe.exclude_symbols == DEFAULT_EXCLUDED_SYMBOLS
    assert {"USDCUSDT", "USDEUSDT", "USD1USDT", "USTCUSDT"}.issubset(
        set(config.universe.exclude_symbols)
    )
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "TRXUSDT"}.isdisjoint(
        set(config.universe.exclude_symbols)
    )
