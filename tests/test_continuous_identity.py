from __future__ import annotations

from liquidity_migration.strategy.continuous_identity import continuous_trade_id


def test_continuous_trade_id_is_deterministic() -> None:
    assert (
        continuous_trade_id("continuous_fade_v2", "WIFUSDT", 1_700_000_123_456)
        == "continuous_fade_v2-WIFUSDT-1700000123456"
    )
