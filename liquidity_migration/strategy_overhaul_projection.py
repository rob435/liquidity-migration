"""Strict runtime projection for proposed strategy-overhaul artifacts.

The declarative registry is useful only if runtime frames are forced through it.
This module supplies that mechanical boundary: exact ordered columns, exact
Polars dtypes, declared non-nullability, and unique/non-null registered keys.
It does not calculate a feature or label and it does not make a proposed schema
canonical.
"""

from __future__ import annotations

from types import MappingProxyType

import polars as pl

from .strategy_overhaul_schemas import ARTIFACT_SCHEMAS


class ArtifactProjectionError(ValueError):
    """A runtime frame cannot be represented by its registered artifact schema."""


_POLARS_DTYPES = MappingProxyType(
    {
        "bool": pl.Boolean,
        "date32": pl.Date,
        "float64": pl.Float64,
        "int64": pl.Int64,
        "int8": pl.Int8,
        "list<utf8>": pl.List(pl.String),
        "uint32": pl.UInt32,
        "utf8": pl.String,
    }
)


def artifact_polars_schema(schema_id: str) -> MappingProxyType[str, pl.DataType]:
    """Return the exact ordered Polars schema for one registered artifact."""

    try:
        artifact = ARTIFACT_SCHEMAS[schema_id]
    except KeyError as exc:
        raise ArtifactProjectionError(f"unknown artifact schema: {schema_id}") from exc
    try:
        schema = {field.name: _POLARS_DTYPES[field.dtype] for field in artifact.fields}
    except KeyError as exc:  # pragma: no cover - registry validation should catch expansion drift
        raise ArtifactProjectionError(f"artifact {schema_id} has unsupported dtype {exc.args[0]!r}") from exc
    return MappingProxyType(schema)


def empty_artifact_frame(schema_id: str) -> pl.DataFrame:
    """Construct a zero-row artifact with every registered column and dtype."""

    return pl.DataFrame(schema=dict(artifact_polars_schema(schema_id)))


def project_artifact_frame(frame: pl.DataFrame, schema_id: str) -> pl.DataFrame:
    """Validate and cast ``frame`` to one exact registered artifact.

    Unknown columns are refused instead of silently discarded. Callers that
    intentionally own a broader intermediate frame must select the complete
    artifact field set before crossing this boundary.
    """

    try:
        artifact = ARTIFACT_SCHEMAS[schema_id]
    except KeyError as exc:
        raise ArtifactProjectionError(f"unknown artifact schema: {schema_id}") from exc
    expected = artifact_polars_schema(schema_id)
    expected_names = tuple(expected)
    actual_names = tuple(frame.columns)
    missing = sorted(set(expected_names) - set(actual_names))
    unknown = sorted(set(actual_names) - set(expected_names))
    if missing or unknown or len(actual_names) != len(expected_names):
        raise ArtifactProjectionError(f"artifact {schema_id} projection mismatch; missing={missing}, unknown={unknown}")

    expressions: list[pl.Expr] = []
    for name, dtype in expected.items():
        expressions.append(pl.col(name).cast(dtype, strict=True).alias(name))
    try:
        projected = frame.select(expressions)
    except (TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise ArtifactProjectionError(
            f"artifact {schema_id} contains a value incompatible with its registered dtype"
        ) from exc

    nonnullable = [field.name for field in artifact.fields if not field.nullable]
    if nonnullable and not projected.is_empty():
        null_counts = projected.select(pl.col(name).is_null().sum().alias(name) for name in nonnullable).row(
            0, named=True
        )
        violated = sorted(name for name, count in null_counts.items() if int(count) > 0)
        if violated:
            raise ArtifactProjectionError(f"artifact {schema_id} has nulls in non-nullable fields: {violated}")

    keys = list(artifact.key_fields)
    if not projected.is_empty():
        null_key = projected.filter(pl.any_horizontal(pl.col(name).is_null() for name in keys))
        if not null_key.is_empty():  # defensive: key fields should also be non-nullable
            raise ArtifactProjectionError(f"artifact {schema_id} has null registered keys")
        duplicates = projected.group_by(keys).len().filter(pl.col("len") > 1)
        if not duplicates.is_empty():
            raise ArtifactProjectionError(
                f"artifact {schema_id} has duplicate registered keys: "
                f"{duplicates.select(keys + ['len']).head(5).to_dicts()}"
            )
    return projected


__all__ = [
    "ArtifactProjectionError",
    "artifact_polars_schema",
    "empty_artifact_frame",
    "project_artifact_frame",
]
