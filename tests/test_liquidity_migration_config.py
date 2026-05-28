from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.config import (
    CostConfig,
    DEFAULT_EXCLUDED_SYMBOLS,
    DEFAULT_RESEARCH_DATA_ROOT,
    _merge_dataclass,
    ensure_data_root_exists,
    load_config,
)


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

    assert config.data_root == DEFAULT_RESEARCH_DATA_ROOT.expanduser()
    assert config.universe.exclude_symbols == DEFAULT_EXCLUDED_SYMBOLS
    assert {"USDCUSDT", "USDEUSDT", "USD1USDT", "USTCUSDT"}.issubset(
        set(config.universe.exclude_symbols)
    )
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "TRXUSDT"}.isdisjoint(
        set(config.universe.exclude_symbols)
    )


def test_merge_dataclass_rejects_unknown_keys() -> None:
    with pytest.raises(TypeError, match="Unknown CostConfig keys"):
        _merge_dataclass(CostConfig, {"maker_fee_bps": 1.0, "not_a_real_field": 99})


def test_cost_config_default_base_cost_unchanged_and_e3_e4_knobs() -> None:
    # Default (60% maker blend, symmetric legs) must equal the legacy 2*blended
    # so existing/running cells are byte-identical.
    blended = 0.60 * (2.0 + 1.0) + 0.40 * (5.5 + 2.0)  # 4.8
    assert CostConfig().base_entry_exit_cost_bps == pytest.approx(2.0 * blended)  # 9.6
    assert CostConfig(exit_cost_multiplier=1.0).base_entry_exit_cost_bps == pytest.approx(9.6)
    # E3: model the live 100%-taker market execution exactly (no maker blend).
    assert CostConfig(maker_fill_probability=0.0).base_entry_exit_cost_bps == pytest.approx(
        2.0 * (5.5 + 2.0)  # 15.0 bps round-trip
    )
    # E4: a costlier cover (exit) leg — only the exit leg scales.
    assert CostConfig(exit_cost_multiplier=2.0).base_entry_exit_cost_bps == pytest.approx(
        blended * 3.0  # entry(1) + exit(2) legs
    )


def test_ensure_data_root_exists(tmp_path: Path) -> None:
    assert ensure_data_root_exists(tmp_path) == tmp_path
    with pytest.raises(FileNotFoundError, match="does not exist"):
        ensure_data_root_exists(tmp_path / "missing")
