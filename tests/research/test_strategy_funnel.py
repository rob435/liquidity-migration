from __future__ import annotations

from liquidity_migration.research.strategy_funnel import finalize_funnel_row


def test_required_not_applicable_gate_is_not_accepted() -> None:
    row = finalize_funnel_row(
        {
            "sleeve": "long",
            "venue": "bybit",
            "symbol": "ABCUSDT",
            "signal_ts_ms": 1_700_000_000_000,
            "gate_pit_tradable": "not_applicable",
            "gate_source_decile_9": "pass",
        },
        required_gate_order=("pit_tradable", "source_decile_9"),
    )

    assert row["first_rejection"] is None
    assert row["barebones_accepted"] is False
