"""Causal-computability timestamps for stable residual momentum.

Historical roots record the RMOM source day and provisional state, not the wall
clock time at which an operational refresh published each row.  A0 therefore
must not fabricate publication timestamps.  It uses the conservative time at
which the frozen shift-3 construction is mathematically computable from closed
source bars.  This establishes an offline causal information boundary only;
live refresh latency remains a separate forward-execution measurement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import polars as pl

from scripts.precompute_residual_momentum import (
    RMOM_CAUSAL_SHIFT,
    RMOM_FIRST_BAR_CLOSE_OFFSET_HOURS,
    RMOM_FORWARD_TARGET_COMPLETION_DAYS,
    RMOM_WINDOW,
)

from ._common import MS_PER_DAY, MS_PER_HOUR


RMOM_PROVENANCE_KEY_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "day_ts": pl.Int64,
        "is_provisional": pl.Boolean,
    }
)
RMOM_CAUSAL_AVAILABILITY_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "day_ts": pl.Int64,
        "rmom_data_available_ts_ms": pl.Int64,
    }
)


class RmomAvailabilityError(ValueError):
    """An RMOM key, provisional-state, or frozen timing invariant failed."""


@dataclass(frozen=True, slots=True)
class RmomAvailabilityArtifact:
    frame: pl.DataFrame
    receipt: MappingProxyType[str, Any]


def causal_computable_ts_ms(day_ts: int) -> int:
    """Return exact completion time for the newest residual used by RMOM[D]."""

    if isinstance(day_ts, bool) or not isinstance(day_ts, int):
        raise TypeError("day_ts must be an integer millisecond timestamp")
    if day_ts < 0 or day_ts % MS_PER_DAY:
        raise ValueError("day_ts must be a non-negative UTC midnight")
    return (
        day_ts
        + (RMOM_FORWARD_TARGET_COMPLETION_DAYS - RMOM_CAUSAL_SHIFT) * MS_PER_DAY
        + RMOM_FIRST_BAR_CLOSE_OFFSET_HOURS * MS_PER_HOUR
    )


def derive_rmom_causal_availability(
    provenance_keys: pl.DataFrame,
) -> RmomAvailabilityArtifact:
    """Derive stable-row availability; provisional rows remain explicitly null."""

    expected = tuple(RMOM_PROVENANCE_KEY_SCHEMA)
    missing = sorted(set(expected) - set(provenance_keys.columns))
    unknown = sorted(set(provenance_keys.columns) - set(expected))
    if missing or unknown or len(provenance_keys.columns) != len(expected):
        raise RmomAvailabilityError(f"provenance_keys projection mismatch; missing={missing}, unknown={unknown}")
    dtype_mismatch = {
        name: {
            "expected": str(dtype),
            "actual": str(provenance_keys.schema[name]),
        }
        for name, dtype in RMOM_PROVENANCE_KEY_SCHEMA.items()
        if provenance_keys.schema[name] != dtype
    }
    if dtype_mismatch:
        raise RmomAvailabilityError(f"provenance_keys has invalid dtypes: {dtype_mismatch}")
    invalid = provenance_keys.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("day_ts").is_null()
        | (pl.col("day_ts") < 0)
        | ((pl.col("day_ts") % MS_PER_DAY) != 0)
        | pl.col("is_provisional").is_null()
    )
    if not invalid.is_empty():
        raise RmomAvailabilityError("provenance_keys contains invalid symbol/day/provisional values")
    duplicates = provenance_keys.group_by(["symbol", "day_ts"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RmomAvailabilityError("provenance_keys contains duplicate (symbol,day_ts) keys")

    offset_ms = (
        RMOM_FORWARD_TARGET_COMPLETION_DAYS - RMOM_CAUSAL_SHIFT
    ) * MS_PER_DAY + RMOM_FIRST_BAR_CLOSE_OFFSET_HOURS * MS_PER_HOUR
    frame = (
        provenance_keys.with_columns(
            pl.when(~pl.col("is_provisional"))
            .then(pl.col("day_ts") + offset_ms)
            .otherwise(None)
            .cast(pl.Int64)
            .alias("rmom_data_available_ts_ms")
        )
        .select(tuple(RMOM_CAUSAL_AVAILABILITY_SCHEMA))
        .sort(["symbol", "day_ts"])
    )
    frame_digest = hashlib.sha256()
    for row in frame.iter_rows(named=True):
        frame_digest.update((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    receipt_payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_rmom_causal_availability",
        "semantics": "causal_computability_not_actual_publication",
        "rmom_window_days": RMOM_WINDOW,
        "rmom_causal_shift_days": RMOM_CAUSAL_SHIFT,
        "forward_target_completion_days": RMOM_FORWARD_TARGET_COMPLETION_DAYS,
        "first_bar_close_offset_hours": RMOM_FIRST_BAR_CLOSE_OFFSET_HOURS,
        "availability_offset_ms_from_source_day": offset_ms,
        "row_count": frame.height,
        "stable_row_count": frame.filter(pl.col("rmom_data_available_ts_ms").is_not_null()).height,
        "provisional_row_count": frame.filter(pl.col("rmom_data_available_ts_ms").is_null()).height,
        "frame_sha256": frame_digest.hexdigest(),
        "actual_publication_time_claimed": False,
        "operational_latency_in_scope": False,
        "outcome_values_read": False,
    }
    receipt_payload["artifact_sha256"] = hashlib.sha256(
        json.dumps(
            receipt_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return RmomAvailabilityArtifact(
        frame=frame,
        receipt=MappingProxyType(receipt_payload),
    )


__all__ = [
    "RMOM_CAUSAL_AVAILABILITY_SCHEMA",
    "RMOM_PROVENANCE_KEY_SCHEMA",
    "RmomAvailabilityArtifact",
    "RmomAvailabilityError",
    "causal_computable_ts_ms",
    "derive_rmom_causal_availability",
]
