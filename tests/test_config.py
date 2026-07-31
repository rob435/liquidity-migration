from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from liquidity_migration.config import (
    CostConfig,
    DEFAULT_EXCLUDED_SYMBOLS,
    DEFAULT_RESEARCH_DATA_ROOT,
    ExchangeConfig,
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


def test_unconsumed_top_level_block_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
trade_flow:
  exclude_block_trades: true
  exclude_rpi_trades: true
universe:
  rank_start: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="Unknown top-level config keys.*trade_flow"):
        load_config(config_path)


# _merge_dataclass applies the same numeric coercion UniverseConfig gets; a
# quoted-numeric YAML value otherwise enters the frozen dataclass as a str and
# only crashes later in str-arithmetic.
def test_cost_config_quoted_numeric_is_coerced(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
cost_model:
  maker_fee_bps: "2.0"
  taker_fee_bps: "5.5"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert isinstance(config.costs.maker_fee_bps, float)
    assert config.costs.maker_fee_bps == pytest.approx(2.0)
    assert config.costs.taker_fee_bps == pytest.approx(5.5)
    # Uncoerced, this raises TypeError (str + float) in str-arithmetic.
    assert config.costs.base_entry_exit_cost_bps == pytest.approx(2.0 * (5.5 + 2.0))


def test_merge_dataclass_coerces_quoted_float_directly() -> None:
    merged = _merge_dataclass(CostConfig, {"maker_fee_bps": "2.0"})
    assert isinstance(merged.maker_fee_bps, float)
    assert merged.maker_fee_bps == pytest.approx(2.0)


# Native float/str/bool YAML values must be numerically/byte unchanged by the
# coercion.
def test_merge_dataclass_native_values_unchanged() -> None:
    cost = _merge_dataclass(
        CostConfig, {"maker_fee_bps": 2.0, "taker_fee_bps": 5.5}
    )
    assert cost == CostConfig(maker_fee_bps=2.0, taker_fee_bps=5.5)
    assert cost.base_entry_exit_cost_bps == pytest.approx(15.0)

    exch = _merge_dataclass(
        ExchangeConfig, {"name": "bybit", "settle_coin": "USDT", "testnet": False}
    )
    # String fields stay strings; bool stays bool (no float() applied to them).
    assert exch.name == "bybit"
    assert exch.settle_coin == "USDT"
    assert exch.testnet is False


def test_default_config_load_unchanged() -> None:
    # The zero-arg default load must be wholly unaffected by both fixes.
    default = load_config()
    assert default.costs.base_entry_exit_cost_bps == pytest.approx(15.0)
    assert default.exchange.name == "bybit"


# --------------------------------------------------------------------------- #
# The CostConfig default must model 100% taker.
# --------------------------------------------------------------------------- #
def test_cost_config_default_is_full_taker_not_maker_blend() -> None:
    # The default must NOT be the 0.60 maker blend (9.6 bps) that under-costs by
    # ~36% and silently flatters returns. It must be the deployed 100%-taker cost.
    assert CostConfig().maker_fill_probability == pytest.approx(0.0)
    assert CostConfig().base_entry_exit_cost_bps == pytest.approx(15.0)
    # A maker blend must still be expressible when explicitly opted into.
    assert CostConfig(maker_fill_probability=0.60).base_entry_exit_cost_bps == pytest.approx(9.6)
    # The default must never be cheaper than the explicit full-taker cost.
    assert CostConfig().base_entry_exit_cost_bps == pytest.approx(
        replace(CostConfig(), maker_fill_probability=0.0).base_entry_exit_cost_bps
    )


def test_merge_dataclass_bool_coercion_is_strict() -> None:
    """``bool('false')`` is True, so a quoted YAML bool must parse to the right value,
    native bools pass through, and an ambiguous string must raise.
    """
    assert _merge_dataclass(ExchangeConfig, {"testnet": "false"}).testnet is False
    assert _merge_dataclass(ExchangeConfig, {"testnet": "no"}).testnet is False
    assert _merge_dataclass(ExchangeConfig, {"testnet": "true"}).testnet is True
    assert _merge_dataclass(ExchangeConfig, {"testnet": True}).testnet is True
    with pytest.raises(ValueError):
        _merge_dataclass(ExchangeConfig, {"testnet": "maybe"})


def test_default_research_data_root_is_expanded() -> None:
    """The default must be ``expanduser()``'d so a direct ``ResearchConfig()`` exposes a resolvable path."""
    assert "~" not in str(DEFAULT_RESEARCH_DATA_ROOT)
