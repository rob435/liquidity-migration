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


def test_cost_config_default_models_live_100pct_taker() -> None:
    # The dataclass default must model the deployed 100%-taker market execution
    # (maker_fill_probability=0.0), NOT a 0.60 maker blend. A maker-blend default
    # under-costs by ~36% (9.6 vs 15.0 bps) and silently flatters returns on any
    # consumer that does not pass the YAML — the canonical cost-too-low error.
    # (cli-config-2 / cost-funding-1)
    assert CostConfig().maker_fill_probability == pytest.approx(0.0)
    assert CostConfig().base_entry_exit_cost_bps == pytest.approx(
        2.0 * (5.5 + 2.0)  # 15.0 bps round-trip, 100% taker
    )
    assert CostConfig(exit_cost_multiplier=1.0).base_entry_exit_cost_bps == pytest.approx(15.0)
    # A maker blend must still be expressible when explicitly opted into (E3).
    assert CostConfig(maker_fill_probability=0.60).base_entry_exit_cost_bps == pytest.approx(
        2.0 * (0.60 * (2.0 + 1.0) + 0.40 * (5.5 + 2.0))  # 9.6 bps
    )
    # E4: a costlier cover (exit) leg — only the exit leg scales. Symmetric base
    # is now the 100%-taker blend (taker_fee+taker_slippage = 7.5 bps/leg).
    taker_leg = 5.5 + 2.0
    assert CostConfig(exit_cost_multiplier=2.0).base_entry_exit_cost_bps == pytest.approx(
        taker_leg * 3.0  # entry(1) + exit(2) legs
    )


def test_ensure_data_root_exists(tmp_path: Path) -> None:
    assert ensure_data_root_exists(tmp_path) == tmp_path
    with pytest.raises(FileNotFoundError, match="does not exist"):
        ensure_data_root_exists(tmp_path / "missing")
