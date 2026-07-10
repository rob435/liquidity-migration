from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from liquidity_migration.strategy_overhaul_projection import (
    ArtifactProjectionError,
    artifact_polars_schema,
    empty_artifact_frame,
    project_artifact_frame,
)
from liquidity_migration.strategy_overhaul_schemas import ARTIFACT_SCHEMAS


def _valid_value(dtype: pl.DataType) -> object:
    if dtype == pl.Boolean:
        return False
    if dtype == pl.Date:
        return dt.date(2026, 1, 1)
    if dtype == pl.Float64:
        return 1.0
    if dtype in {pl.Int64, pl.Int8, pl.UInt32}:
        return 1
    if dtype == pl.String:
        return "x"
    if dtype == pl.List(pl.String):
        return ["x"]
    raise AssertionError(f"test helper lacks dtype {dtype}")


@pytest.mark.parametrize("schema_id", tuple(ARTIFACT_SCHEMAS))
def test_empty_artifact_frame_has_exact_order_and_dtypes(schema_id: str) -> None:
    expected = artifact_polars_schema(schema_id)
    frame = empty_artifact_frame(schema_id)

    assert frame.columns == list(expected)
    assert frame.schema == dict(expected)
    assert frame.is_empty()


@pytest.mark.parametrize("schema_id", tuple(ARTIFACT_SCHEMAS))
def test_projector_orders_casts_and_validates_a_complete_row(schema_id: str) -> None:
    expected = artifact_polars_schema(schema_id)
    values = {name: [_valid_value(dtype)] for name, dtype in reversed(expected.items())}
    projected = project_artifact_frame(pl.DataFrame(values), schema_id)

    assert projected.columns == list(expected)
    assert projected.schema == dict(expected)


def test_projector_refuses_unknown_missing_null_and_duplicate_contract_rows() -> None:
    schema_id = next(iter(ARTIFACT_SCHEMAS))
    expected = artifact_polars_schema(schema_id)
    values = {name: [_valid_value(dtype)] for name, dtype in expected.items()}

    with pytest.raises(ArtifactProjectionError, match="unknown=.*sentinel_future_return"):
        project_artifact_frame(
            pl.DataFrame({**values, "sentinel_future_return": [0.5]}),
            schema_id,
        )
    with pytest.raises(ArtifactProjectionError, match="missing="):
        project_artifact_frame(pl.DataFrame(values).drop(next(iter(expected))), schema_id)

    artifact = ARTIFACT_SCHEMAS[schema_id]
    nonnullable = next(field.name for field in artifact.fields if not field.nullable)
    null_frame = pl.DataFrame(values).with_columns(pl.lit(None).alias(nonnullable))
    with pytest.raises(ArtifactProjectionError, match="non-nullable"):
        project_artifact_frame(null_frame, schema_id)

    duplicated = pl.concat([pl.DataFrame(values), pl.DataFrame(values)])
    with pytest.raises(ArtifactProjectionError, match="duplicate registered keys"):
        project_artifact_frame(duplicated, schema_id)
