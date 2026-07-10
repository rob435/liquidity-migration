"""Exact, separately materializable LONG-A0 S03 and S04 boundaries.

The broad primitives in :mod:`liquidity_migration.long_population_scout` are
useful reconstruction implementations, but they retain their upstream columns.
This module makes the proposed stage split mechanical:

* S03 accepts one exact registered S02 artifact, reads only the frozen entry
  decision prefix from raw hourly OHLC, and emits the exact 30-field S03 schema.
* S04 accepts exact S02 and S03 artifacts, joins only the signal/exit geometry
  needed to reconstruct S03, verifies that reconstruction against raw hourly
  OHLC, and emits the exact 71-field frozen label schema.

These functions establish schema, key, and local causal-geometry invariants.
They do not bind the artifacts to immutable source/config receipts, prove PIT
population provenance, support an alpha claim, or authorize deployment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

import polars as pl

from .long_native import LongNativeConfig
from .long_population_scout import (
    _reject_outcome_like_columns,
    append_long_entry_policy,
    append_long_path_labels,
)
from .strategy_overhaul_long_s02 import HOURLY_BAR_SCHEMA
from .strategy_overhaul_projection import (
    ArtifactProjectionError,
    artifact_polars_schema,
    project_artifact_frame,
)
from .strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    LONG_ENTRY_SCHEMA_ID,
    LONG_LABEL_SCHEMA_ID,
    LONG_SIGNAL_SCHEMA_ID,
)


class LongStageBoundaryError(ValueError):
    """An exact stage, key-parity, source, or venue invariant failed."""


_KEY_COLUMNS = ("venue", "symbol", "signal_ts_ms")
_IDENTITY_COLUMNS = (*_KEY_COLUMNS, "canonical_instrument_id")

_S02_SCHEMA = artifact_polars_schema(LONG_SIGNAL_SCHEMA_ID)
_S03_SCHEMA = artifact_polars_schema(LONG_ENTRY_SCHEMA_ID)
_S04_SCHEMA = artifact_polars_schema(LONG_LABEL_SCHEMA_ID)

_S02_TO_S04_GEOMETRY_COLUMNS = (
    "atr_14d_pct",
    "signal_bar_present",
    "signal_bar_complete",
    "signal_close_hourly",
    "fc_exit_stop_pct",
    "fc_exit_take_profit_pct",
    "fc_exit_max_hold_hours",
    "long_feature_tape_schema_version",
)
_S02_TO_S03_GEOMETRY_COLUMNS = (
    *_S02_TO_S04_GEOMETRY_COLUMNS[:4],
    "classifier_selected",
    *_S02_TO_S04_GEOMETRY_COLUMNS[4:],
)
_S03_DERIVED_COLUMNS = tuple(
    field.name for field in ARTIFACT_SCHEMAS[LONG_ENTRY_SCHEMA_ID].fields if field.name not in _IDENTITY_COLUMNS
)

S03_INPUT_SCHEMA = MappingProxyType(dict(_S02_SCHEMA))
S04_S02_GEOMETRY_SCHEMA = MappingProxyType(
    {
        **{name: _S02_SCHEMA[name] for name in _IDENTITY_COLUMNS},
        **{name: _S02_SCHEMA[name] for name in _S02_TO_S04_GEOMETRY_COLUMNS},
    }
)


def _require_exact_artifact(
    frame: pl.DataFrame,
    *,
    schema_id: str,
    name: str,
) -> pl.DataFrame:
    """Require exact registered names/dtypes, then apply central validation."""

    expected = artifact_polars_schema(schema_id)
    expected_names = tuple(expected)
    missing = sorted(set(expected_names) - set(frame.columns))
    unknown = sorted(set(frame.columns) - set(expected_names))
    if missing or unknown or len(frame.columns) != len(expected_names):
        raise LongStageBoundaryError(
            f"{name} is not the exact {schema_id} artifact; missing={missing}, unknown={unknown}"
        )
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise LongStageBoundaryError(f"{name} has invalid registered dtypes: {mismatched}")
    try:
        return project_artifact_frame(frame, schema_id)
    except ArtifactProjectionError as exc:
        raise LongStageBoundaryError(f"{name} failed central projection: {exc}") from exc


def _project_hourly_bars(hourly_bars: pl.DataFrame) -> pl.DataFrame:
    """Finite raw-OHLC projection shared by S03 and S04.

    Benign source metadata may accompany a raw hourly frame, but caller-derived
    outcome columns are refused rather than silently ignored.  The broad
    primitives then validate every key and lazily validate only consumed OHLC
    values, preserving the intended causal read boundary.
    """

    _reject_outcome_like_columns(hourly_bars, name="hourly_bars")
    missing = sorted(set(HOURLY_BAR_SCHEMA) - set(hourly_bars.columns))
    if missing:
        raise LongStageBoundaryError(f"hourly_bars missing required raw OHLC columns: {missing}")
    mismatched = {
        column: {"expected": str(dtype), "actual": str(hourly_bars.schema[column])}
        for column, dtype in HOURLY_BAR_SCHEMA.items()
        if hourly_bars.schema[column] != dtype
    }
    if mismatched:
        raise LongStageBoundaryError(f"hourly_bars has invalid raw OHLC dtypes: {mismatched}")
    return hourly_bars.select(tuple(HOURLY_BAR_SCHEMA))


def _require_single_venue(frame: pl.DataFrame, *, name: str) -> None:
    """Fail closed when a venue-less hourly frame could be ambiguous."""

    if frame.is_empty():
        return
    venues = frame.get_column("venue").unique().sort().to_list()
    if len(venues) != 1:
        raise LongStageBoundaryError(
            f"{name} must contain exactly one venue because hourly_bars has no venue key; got {venues}"
        )


def _assert_identity_parity(
    expected: pl.DataFrame,
    actual: pl.DataFrame,
    *,
    expected_name: str,
    actual_name: str,
) -> None:
    expected_identity = expected.select(_IDENTITY_COLUMNS).sort(_KEY_COLUMNS)
    actual_identity = actual.select(_IDENTITY_COLUMNS).sort(_KEY_COLUMNS)
    if expected_identity.height == actual_identity.height and expected_identity.equals(actual_identity):
        return

    expected_keys = expected_identity.select(_KEY_COLUMNS)
    actual_keys = actual_identity.select(_KEY_COLUMNS)
    missing = expected_keys.join(actual_keys, on=_KEY_COLUMNS, how="anti")
    unexpected = actual_keys.join(expected_keys, on=_KEY_COLUMNS, how="anti")
    canonical_mismatch = (
        expected_identity.rename({"canonical_instrument_id": "__expected_canonical_instrument_id"})
        .join(actual_identity, on=_KEY_COLUMNS, how="inner")
        .filter(pl.col("__expected_canonical_instrument_id") != pl.col("canonical_instrument_id"))
        .select(
            *_KEY_COLUMNS,
            "__expected_canonical_instrument_id",
            "canonical_instrument_id",
        )
    )
    raise LongStageBoundaryError(
        f"{actual_name} identity does not exactly equal {expected_name}; "
        f"missing={missing.head(5).to_dicts()}, "
        f"unexpected={unexpected.head(5).to_dicts()}, "
        f"canonical_mismatch={canonical_mismatch.head(5).to_dicts()}"
    )


def _strict_select(
    frame: pl.DataFrame,
    columns: Sequence[str],
    *,
    name: str,
) -> pl.DataFrame:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise LongStageBoundaryError(f"{name} missing required columns: {missing}")
    return frame.select(columns)


def _project_output(
    frame: pl.DataFrame,
    *,
    schema_id: str,
    columns: Mapping[str, pl.DataType],
) -> pl.DataFrame:
    selected = _strict_select(
        frame,
        tuple(columns),
        name=f"{schema_id} builder output",
    )
    try:
        return project_artifact_frame(selected, schema_id).sort(_KEY_COLUMNS)
    except ArtifactProjectionError as exc:
        raise LongStageBoundaryError(f"{schema_id} failed central projection: {exc}") from exc


def build_long_s03_entry_policy(
    s02_feature_tape: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    *,
    config: LongNativeConfig,
) -> pl.DataFrame:
    """Build one exact LONG-A0 S03 artifact from exact registered S02.

    S03 reads only the frozen h1..deadline entry-decision prefix used by the
    existing LONG entry implementation.  Arbitrary S02 columns and precomputed
    path outcomes cannot cross this boundary because S02 must match its exact
    registered schema and the output is centrally projected to S03.
    """

    s02 = _require_exact_artifact(
        s02_feature_tape,
        schema_id=LONG_SIGNAL_SCHEMA_ID,
        name="s02_feature_tape",
    )
    _require_single_venue(s02, name="s02_feature_tape")
    hourly = _project_hourly_bars(hourly_bars)

    # Do not hand the compatibility primitive any unrelated S02 fields.  This
    # freezes the S03 dependency surface to identity, ATR-dependent entry/exit
    # geometry, the signal bar, and the classifier selection flag its current
    # public contract requires.
    narrow_feature = s02.select(
        *_IDENTITY_COLUMNS,
        *_S02_TO_S03_GEOMETRY_COLUMNS,
    )
    broad_entry = append_long_entry_policy(narrow_feature, hourly, config)
    s03 = _project_output(
        broad_entry,
        schema_id=LONG_ENTRY_SCHEMA_ID,
        columns=_S03_SCHEMA,
    )
    _assert_identity_parity(
        s02,
        s03,
        expected_name="S02",
        actual_name="S03",
    )
    return s03


def build_long_s04_path_labels(
    s02_feature_tape: pl.DataFrame,
    s03_entry_policy: pl.DataFrame,
    hourly_bars: pl.DataFrame,
    *,
    config: LongNativeConfig,
) -> pl.DataFrame:
    """Build exact frozen LONG-A0 S04 labels from exact S02 and S03.

    The broad label implementation receives neither the whole S02 artifact nor
    an S03-with-upstream-columns convenience frame.  It receives only identity,
    signal/exit geometry, and the exact S03-derived fields.  Before calculating
    labels it reconstructs signal-bar, exit, and entry-policy geometry from the
    frozen config and raw hourly bars and rejects any disagreement.
    """

    s02 = _require_exact_artifact(
        s02_feature_tape,
        schema_id=LONG_SIGNAL_SCHEMA_ID,
        name="s02_feature_tape",
    )
    s03 = _require_exact_artifact(
        s03_entry_policy,
        schema_id=LONG_ENTRY_SCHEMA_ID,
        name="s03_entry_policy",
    )
    _require_single_venue(s02, name="s02_feature_tape")
    _require_single_venue(s03, name="s03_entry_policy")
    _assert_identity_parity(
        s02,
        s03,
        expected_name="S02",
        actual_name="S03",
    )
    hourly = _project_hourly_bars(hourly_bars)

    geometry = s02.select(
        *_IDENTITY_COLUMNS,
        *_S02_TO_S04_GEOMETRY_COLUMNS,
    )
    anchors = s03.select(*_IDENTITY_COLUMNS, *_S03_DERIVED_COLUMNS)
    narrow_entry = geometry.join(
        anchors,
        on=list(_IDENTITY_COLUMNS),
        how="inner",
        validate="1:1",
    ).sort(_KEY_COLUMNS)
    if narrow_entry.height != s02.height:
        # Identity parity above should make this unreachable, but retain the
        # explicit row-count boundary around future join changes.
        raise LongStageBoundaryError("narrow S04 reconstruction join did not preserve the exact S02/S03 population")

    broad_labels = append_long_path_labels(narrow_entry, hourly, config)
    s04 = _project_output(
        broad_labels,
        schema_id=LONG_LABEL_SCHEMA_ID,
        columns=_S04_SCHEMA,
    )
    _assert_identity_parity(
        s03,
        s04,
        expected_name="S03",
        actual_name="S04",
    )
    return s04


__all__ = [
    "LongStageBoundaryError",
    "S03_INPUT_SCHEMA",
    "S04_S02_GEOMETRY_SCHEMA",
    "build_long_s03_entry_policy",
    "build_long_s04_path_labels",
]
