from __future__ import annotations

import pytest

from liquidity_migration.rules.long_config import ConfigLayer, resolve_strategy_config
from liquidity_migration.rules.long_native import long_v12_profile


def test_effective_config_records_the_winning_source_per_field() -> None:
    config = resolve_strategy_config(
        "v12",
        layers=(
            ConfigLayer(
                source="operational_profile",
                detail="sha256:abc",
                values={
                    "notional_multiplier": 6.0,
                    "entry_leverage": 10.0,
                    "max_new_entries_per_cycle": 5,
                },
            ),
            ConfigLayer(
                source="higher_priority_layer",
                detail="explicit test override",
                values={"notional_multiplier": 7.0},
            ),
            ConfigLayer(
                source="research_cost_model",
                values={"round_trip_cost_bps": 42.0},
            ),
        ),
    )

    assert config.rule == long_v12_profile()
    assert config.notional_multiplier == pytest.approx(7.0)
    assert config.round_trip_cost_bps == pytest.approx(42.0)
    provenance = config.provenance_by_field()
    assert provenance["notional_multiplier"] == {
        "source": "higher_priority_layer",
        "detail": "explicit test override",
    }
    assert provenance["entry_leverage"] == {
        "source": "operational_profile",
        "detail": "sha256:abc",
    }
    assert provenance["resize_floor_fraction"]["source"] == (
        "engine_plan_rules_fleet"
    )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"notional_multiplier": True}, "numeric"),
        ({"book_validity_ms": 1}, "book validity"),
        ({"made_up": 1}, "unknown"),
    ],
)
def test_effective_config_rejects_invalid_layers(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_strategy_config(
            "v12",
            layers=(ConfigLayer(source="test", values=values),),
        )
