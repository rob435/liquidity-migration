from __future__ import annotations

import logging
from pathlib import Path

import pytest

from liquidity_migration.config import (
    CostConfig,
    ExchangeConfig,
    _merge_dataclass,
    load_config,
)


# audit2b defect 1: load_config silently dropped unconsumed top-level YAML
# blocks (e.g. the committed `trade_flow` block). It must surface them via a
# warning rather than ignoring them silently.
def test_unconsumed_top_level_block_is_warned(tmp_path: Path, caplog) -> None:
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

    with caplog.at_level(logging.WARNING, logger="liquidity_migration.config"):
        config = load_config(config_path)

    # The unconsumed block is surfaced...
    assert any("trade_flow" in rec.getMessage() for rec in caplog.records)
    assert any("unconsumed" in rec.getMessage() for rec in caplog.records)
    # ...but loading still succeeds and the consumed keys are honoured.
    assert config.universe.rank_start == 1


# audit2b defect 1 (negative): a config whose only blocks ARE consumed must NOT
# emit the unconsumed-keys warning (normal-input-unchanged guard).
def test_fully_consumed_config_emits_no_warning(tmp_path: Path, caplog) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
exchange:
  name: bybit
universe:
  rank_start: 1
cost_model:
  maker_fee_bps: 2.0
""",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="liquidity_migration.config"):
        load_config(config_path)

    assert not any("unconsumed" in rec.getMessage() for rec in caplog.records)


# audit2b defect 2: _merge_dataclass now applies the same numeric coercion that
# UniverseConfig already gets. A quoted-numeric YAML value used to flow into the
# frozen dataclass as a str and only crash later in str-arithmetic.
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
    # On OLD code this raised TypeError (str + float) in str-arithmetic.
    assert config.costs.base_entry_exit_cost_bps == pytest.approx(2.0 * (5.5 + 2.0))


def test_merge_dataclass_coerces_quoted_float_directly() -> None:
    merged = _merge_dataclass(CostConfig, {"maker_fee_bps": "2.0"})
    assert isinstance(merged.maker_fee_bps, float)
    assert merged.maker_fee_bps == pytest.approx(2.0)


# audit2b defect 2 (happy-path guard): native float/str/bool YAML values must be
# numerically/byte unchanged by the coercion.
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
