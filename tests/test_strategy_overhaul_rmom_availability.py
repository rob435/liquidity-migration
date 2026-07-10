"""Tests for honest RMOM causal-computability semantics."""

from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.strategy_overhaul_rmom_availability import (
    RMOM_CAUSAL_AVAILABILITY_SCHEMA,
    RMOM_PROVENANCE_KEY_SCHEMA,
    RmomAvailabilityError,
    causal_computable_ts_ms,
    derive_rmom_causal_availability,
)


DAY = 20_000 * MS_PER_DAY


def _keys() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AAAUSDT", "BBBUSDT"],
            "day_ts": [DAY, DAY],
            "is_provisional": [False, True],
        },
        schema=dict(RMOM_PROVENANCE_KEY_SCHEMA),
    )


def test_stable_rows_use_frozen_causal_completion_and_provisional_stays_null() -> None:
    artifact = derive_rmom_causal_availability(_keys())
    rows = {row["symbol"]: row for row in artifact.frame.iter_rows(named=True)}

    assert artifact.frame.schema == dict(RMOM_CAUSAL_AVAILABILITY_SCHEMA)
    assert rows["AAAUSDT"]["rmom_data_available_ts_ms"] == DAY - MS_PER_DAY + MS_PER_HOUR
    assert rows["BBBUSDT"]["rmom_data_available_ts_ms"] is None
    assert artifact.receipt["semantics"] == ("causal_computability_not_actual_publication")
    assert artifact.receipt["actual_publication_time_claimed"] is False
    assert artifact.receipt["stable_row_count"] == 1
    assert artifact.receipt["provisional_row_count"] == 1


def test_scalar_formula_and_artifact_are_deterministic() -> None:
    first = derive_rmom_causal_availability(_keys())
    second = derive_rmom_causal_availability(_keys().reverse())

    assert causal_computable_ts_ms(DAY) == DAY - MS_PER_DAY + MS_PER_HOUR
    assert first.frame.equals(second.frame)
    assert first.receipt == second.receipt


@pytest.mark.parametrize("malformation", ["duplicate", "off_grid", "unknown"])
def test_malformed_provenance_keys_fail_closed(malformation: str) -> None:
    keys = _keys()
    if malformation == "duplicate":
        keys = pl.concat([keys, keys.head(1)])
    elif malformation == "off_grid":
        keys = keys.with_columns(
            pl.when(pl.int_range(pl.len()) == 0).then(pl.col("day_ts") + 1).otherwise(pl.col("day_ts")).alias("day_ts")
        )
    else:
        keys = keys.with_columns(pl.lit(1.0).alias("future_return"))

    with pytest.raises(RmomAvailabilityError):
        derive_rmom_causal_availability(keys)
