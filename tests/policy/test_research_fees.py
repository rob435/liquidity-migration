from __future__ import annotations

import json

import pytest

from liquidity_migration.core.config import CostConfig
from liquidity_migration.research.backtest.long_live_physics import LivePhysicsAssumptions


def test_research_defaults_read_snapshot_at_construction(tmp_path, monkeypatch):
    snapshot = tmp_path / "fees.json"
    monkeypatch.setenv("LIQUIDITY_MIGRATION_FEE_SNAPSHOT", str(snapshot))
    for taker, maker in [(0.0012, 0.0007), (0.0013, 0.0008)]:
        snapshot.write_text(json.dumps({"realm": "mainnet", "rates": {
            "BTCUSDT": {"taker": taker, "maker": maker, "observed_ns": 1788829713260644810},
        }}))
        assert CostConfig().taker_fee_bps == pytest.approx(taker * 10_000)
        assert CostConfig().maker_fee_bps == pytest.approx(maker * 10_000)
        assert LivePhysicsAssumptions().taker_fee_bps == pytest.approx(taker * 10_000)


def test_explicit_research_fee_scenarios_do_not_require_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("LIQUIDITY_MIGRATION_FEE_SNAPSHOT", str(tmp_path / "missing.json"))
    assert CostConfig(taker_fee_bps=5.5, maker_fee_bps=2).taker_fee_bps == 5.5
    assert LivePhysicsAssumptions(taker_fee_bps=5.5).taker_fee_bps == 5.5


def test_missing_snapshot_never_falls_back_to_published_fees(tmp_path, monkeypatch):
    monkeypatch.setenv("LIQUIDITY_MIGRATION_FEE_SNAPSHOT", str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        CostConfig()
