"""Outcome-blind population-key manifests for the A0 sleeve runners.

These primitives consume only hourly identity keys and normalized PIT membership
pairs.  They deliberately do not accept OHLCV, features, ranks, gates, labels,
or PnL.  CONTINUOUS keeps separate causal-source and retained signal-window
keys.  LONG first constructs every >=20-hour daily kline key in the supplied
full root history, derives production-parity age before the PIT-membership gate
and causal-read floor, and only then retains registered source/signal rows.

The returned hashes prove the supplied key frames were transformed
deterministically; they do not prove that the caller supplied a complete root.
An evidence runner needs independently semantically validated canonical inputs;
the diagnostic S00/S01 byte bindings alone do not prove completeness.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .strategy_overhaul_identity_adapter import SUPPORTED_VENUES


HOURLY_KEY_SCHEMA = MappingProxyType({"symbol": pl.String, "ts_ms": pl.Int64})
MANIFEST_KEY_SCHEMA = MappingProxyType({"symbol": pl.String, "manifest_date": pl.Date})
CONTINUOUS_KEY_SCHEMA = MappingProxyType({"symbol": pl.String, "signal_ts_ms": pl.Int64})
LONG_KEY_SCHEMA = MappingProxyType(
    {
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "symbol_age_days": pl.Int64,
        "hourly_bar_count": pl.UInt32,
    }
)


class PopulationKeyError(ValueError):
    """A key schema, window, membership, or population invariant failed."""


@dataclass(frozen=True, slots=True)
class PopulationKeyWindow:
    identity_history_start_ts_ms: int
    causal_read_start_ts_ms: int
    signal_start_ts_ms: int
    signal_end_ts_ms_exclusive: int

    def __post_init__(self) -> None:
        values = (
            self.identity_history_start_ts_ms,
            self.causal_read_start_ts_ms,
            self.signal_start_ts_ms,
            self.signal_end_ts_ms_exclusive,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("population window timestamps must be integer milliseconds")
        if not values[0] <= values[1] <= values[2] < values[3]:
            raise ValueError(
                "population window must satisfy identity_history_start <= causal_read_start <= "
                "signal_start < signal_end"
            )
        if any(value < 0 or value % MS_PER_DAY for value in values):
            raise ValueError("population window timestamps must be non-negative UTC midnights")


@dataclass(frozen=True, slots=True)
class PopulationKeyArtifacts:
    sleeve: Literal["continuous", "long"]
    venue: str
    source_keys: pl.DataFrame
    signal_keys: pl.DataFrame
    receipt: MappingProxyType[str, Any]


def _require_exact_schema(
    frame: pl.DataFrame,
    expected: MappingProxyType[str, pl.DataType],
    *,
    name: str,
) -> None:
    expected_names = tuple(expected)
    missing = sorted(set(expected_names) - set(frame.columns))
    unknown = sorted(set(frame.columns) - set(expected_names))
    if missing or unknown or len(frame.columns) != len(expected_names):
        raise PopulationKeyError(f"{name} projection mismatch; missing={missing}, unknown={unknown}")
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise PopulationKeyError(f"{name} has invalid dtypes: {mismatched}")


def _validate_venue(venue: str) -> str:
    if not isinstance(venue, str) or venue != venue.strip().lower() or venue not in SUPPORTED_VENUES:
        raise PopulationKeyError(f"venue must be one of {sorted(SUPPORTED_VENUES)}")
    return venue


def _validate_hourly_keys(frame: pl.DataFrame) -> pl.DataFrame:
    _require_exact_schema(frame, HOURLY_KEY_SCHEMA, name="hourly_keys")
    invalid = frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("ts_ms").is_null()
        | (pl.col("ts_ms") < 0)
        | ((pl.col("ts_ms") % MS_PER_HOUR) != 0)
    )
    if not invalid.is_empty():
        raise PopulationKeyError("hourly_keys contains null/blank/off-grid keys")
    duplicates = frame.group_by(["symbol", "ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise PopulationKeyError("hourly_keys contains duplicate (symbol,ts_ms) keys")
    return frame.sort(["symbol", "ts_ms"])


def _validate_manifest_keys(frame: pl.DataFrame) -> pl.DataFrame:
    _require_exact_schema(frame, MANIFEST_KEY_SCHEMA, name="manifest_keys")
    invalid = frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars())
        | pl.col("manifest_date").is_null()
    )
    if not invalid.is_empty():
        raise PopulationKeyError("manifest_keys contains null or blank keys")
    duplicates = frame.group_by(["symbol", "manifest_date"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise PopulationKeyError("manifest_keys contains duplicate (symbol,manifest_date) pairs")
    return frame.sort(["symbol", "manifest_date"])


def _json_scalar(value: object) -> object:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _frame_sha256(
    frame: pl.DataFrame,
    *,
    artifact_name: str,
) -> str:
    canonical = frame.sort(frame.columns) if frame.columns and not frame.is_empty() else frame
    digest = hashlib.sha256()
    header = {
        "artifact_name": artifact_name,
        "columns": [{"name": name, "dtype": str(dtype)} for name, dtype in canonical.schema.items()],
    }
    digest.update((json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n").encode())
    for row in canonical.iter_rows(named=True):
        payload = {name: _json_scalar(row[name]) for name in canonical.columns}
        digest.update((json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return digest.hexdigest()


def _receipt(
    *,
    sleeve: Literal["continuous", "long"],
    venue: str,
    window: PopulationKeyWindow,
    source: pl.DataFrame,
    signal: pl.DataFrame,
    hourly_input_row_count: int,
    windowed_hourly_row_count: int,
    manifest_covered_hourly_row_count: int,
    hourly_identity_input: pl.DataFrame,
    manifest_identity_input: pl.DataFrame,
    windowed_hourly: pl.DataFrame,
    covered_hourly: pl.DataFrame,
    age_history_hourly_row_count: int | None = None,
    age_history_start_ts_ms: int | None = None,
    age_left_censored_symbols: tuple[str, ...] = (),
) -> MappingProxyType[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_outcome_blind_population_keys",
        "sleeve": sleeve,
        "venue": venue,
        "window": dataclasses.asdict(window),
        "source_key_columns": list(source.columns),
        "signal_key_columns": list(signal.columns),
        "hourly_input_row_count": hourly_input_row_count,
        "windowed_hourly_row_count": windowed_hourly_row_count,
        "manifest_covered_hourly_row_count": manifest_covered_hourly_row_count,
        "hourly_identity_input_sha256": _frame_sha256(
            hourly_identity_input,
            artifact_name=f"{sleeve}_{venue}_hourly_identity_input",
        ),
        "manifest_identity_input_sha256": _frame_sha256(
            manifest_identity_input,
            artifact_name=f"{sleeve}_{venue}_manifest_identity_input",
        ),
        "windowed_hourly_sha256": _frame_sha256(
            windowed_hourly,
            artifact_name=f"{sleeve}_{venue}_windowed_hourly_keys",
        ),
        "manifest_covered_hourly_sha256": _frame_sha256(
            covered_hourly,
            artifact_name=f"{sleeve}_{venue}_manifest_covered_hourly_keys",
        ),
        "kline_without_membership_row_count": (windowed_hourly_row_count - manifest_covered_hourly_row_count),
        "source_row_count": source.height,
        "signal_row_count": signal.height,
        "source_sha256": _frame_sha256(
            source,
            artifact_name=f"{sleeve}_{venue}_source_keys",
        ),
        "signal_sha256": _frame_sha256(
            signal,
            artifact_name=f"{sleeve}_{venue}_signal_keys",
        ),
        "outcome_values_read": False,
        "numeric_kline_values_read": False,
        "age_history_hourly_row_count": age_history_hourly_row_count,
        "age_history_start_ts_ms": age_history_start_ts_ms,
        "age_left_censored_symbol_count": len(age_left_censored_symbols),
        "age_left_censored_symbol_sample": list(age_left_censored_symbols[:20]),
        "age_left_censor_semantics": (
            "first_eligible_daily_kline_row_equals_registered_identity_history_boundary"
            if sleeve == "long"
            else None
        ),
        "age_anchor_semantics": (
            "first_eligible_daily_kline_row_in_supplied_full_root_history_before_membership_gate"
            if sleeve == "long"
            else None
        ),
        "root_completeness_proven": False,
        "root_completeness_limitation": (
            "the evidence runner must bind hourly_keys and manifest_keys to an independent immutable root receipt"
        ),
    }
    receipt_hash_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload["artifact_sha256"] = hashlib.sha256(receipt_hash_payload).hexdigest()
    return MappingProxyType(payload)


def build_continuous_population_keys(
    hourly_keys: pl.DataFrame,
    manifest_keys: pl.DataFrame,
    *,
    venue: str,
    window: PopulationKeyWindow,
) -> PopulationKeyArtifacts:
    """Build exact CONT source/warmup and retained signal key manifests."""

    venue = _validate_venue(venue)
    hourly = _validate_hourly_keys(hourly_keys)
    manifest = _validate_manifest_keys(manifest_keys)
    history_start_date = dt.datetime.fromtimestamp(
        window.identity_history_start_ts_ms / 1000,
        tz=dt.timezone.utc,
    ).date()
    signal_end_date = dt.datetime.fromtimestamp(
        window.signal_end_ts_ms_exclusive / 1000,
        tz=dt.timezone.utc,
    ).date()
    manifest_identity = manifest.filter(
        (pl.col("manifest_date") >= history_start_date) & (pl.col("manifest_date") < signal_end_date)
    )
    windowed = hourly.filter(
        (pl.col("ts_ms") >= window.causal_read_start_ts_ms) & (pl.col("ts_ms") < window.signal_end_ts_ms_exclusive)
    ).with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.date().alias("manifest_date"))
    covered = windowed.join(
        manifest,
        on=["symbol", "manifest_date"],
        how="inner",
        validate="m:1",
    )
    source = (
        covered.select(
            "symbol",
            pl.col("ts_ms").alias("signal_ts_ms"),
        )
        .sort(["symbol", "signal_ts_ms"])
        .cast(dict(CONTINUOUS_KEY_SCHEMA))
    )
    signal = source.filter(pl.col("signal_ts_ms") >= window.signal_start_ts_ms).sort(["symbol", "signal_ts_ms"])
    receipt = _receipt(
        sleeve="continuous",
        venue=venue,
        window=window,
        source=source,
        signal=signal,
        hourly_input_row_count=hourly.height,
        windowed_hourly_row_count=windowed.height,
        manifest_covered_hourly_row_count=covered.height,
        hourly_identity_input=windowed.select(tuple(HOURLY_KEY_SCHEMA)),
        manifest_identity_input=manifest_identity,
        windowed_hourly=windowed.select(tuple(HOURLY_KEY_SCHEMA)),
        covered_hourly=covered.select(tuple(HOURLY_KEY_SCHEMA)),
    )
    return PopulationKeyArtifacts("continuous", venue, source, signal, receipt)


def build_long_population_keys(
    hourly_keys: pl.DataFrame,
    manifest_keys: pl.DataFrame,
    *,
    venue: str,
    window: PopulationKeyWindow,
    min_hourly_bars: int = 20,
) -> PopulationKeyArtifacts:
    """Build full eligible daily source keys, age, then retained LONG keys."""

    venue = _validate_venue(venue)
    if isinstance(min_hourly_bars, bool) or not isinstance(min_hourly_bars, int):
        raise TypeError("min_hourly_bars must be an integer")
    if min_hourly_bars <= 0 or min_hourly_bars > 24:
        raise ValueError("min_hourly_bars must be in 1..24")
    hourly = _validate_hourly_keys(hourly_keys)
    manifest = _validate_manifest_keys(manifest_keys)
    history_start_date = dt.datetime.fromtimestamp(
        window.identity_history_start_ts_ms / 1000,
        tz=dt.timezone.utc,
    ).date()
    signal_end_date = dt.datetime.fromtimestamp(
        window.signal_end_ts_ms_exclusive / 1000,
        tz=dt.timezone.utc,
    ).date()
    manifest_identity = manifest.filter(
        (pl.col("manifest_date") >= history_start_date) & (pl.col("manifest_date") < signal_end_date)
    )
    # Age must be derived before the causal feature-read floor.  Otherwise every
    # instrument already trading at that floor is silently made "new".  The
    # caller must therefore supply the complete root key history and bind it to
    # an immutable root receipt before this diagnostic can become evidence.
    age_history = hourly.filter(
        (pl.col("ts_ms") >= window.identity_history_start_ts_ms)
        & (pl.col("ts_ms") < window.signal_end_ts_ms_exclusive)
    ).with_columns(
        pl.from_epoch("ts_ms", time_unit="ms").dt.date().alias("manifest_date"),
        (pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)).alias("day_start_ts_ms"),
    )
    daily_history = (
        age_history.group_by(["symbol", "day_start_ts_ms", "manifest_date"])
        .agg(pl.len().cast(pl.UInt32).alias("hourly_bar_count"))
        .filter(pl.col("hourly_bar_count") >= min_hourly_bars)
        .with_columns((pl.col("day_start_ts_ms") + MS_PER_DAY).cast(pl.Int64).alias("signal_ts_ms"))
        .filter(pl.col("signal_ts_ms") < window.signal_end_ts_ms_exclusive)
        .sort(["symbol", "signal_ts_ms"])
        .with_columns(
            ((pl.col("signal_ts_ms") - pl.col("signal_ts_ms").min().over("symbol")) // MS_PER_DAY + 1)
            .cast(pl.Int64)
            .alias("symbol_age_days")
        )
    )
    daily = (
        daily_history.filter(pl.col("signal_ts_ms") > window.causal_read_start_ts_ms)
        .join(
            manifest,
            on=["symbol", "manifest_date"],
            how="inner",
            validate="m:1",
        )
    )
    left_censored_symbols = tuple(
        str(value)
        for value in (
            daily_history.group_by("symbol")
            .agg(pl.col("day_start_ts_ms").min().alias("first_day_start_ts_ms"))
            .filter(pl.col("first_day_start_ts_ms") == window.identity_history_start_ts_ms)
            .sort("symbol")["symbol"]
            .to_list()
        )
    )
    # Retained only for receipt counts; ``daily_history`` above is already
    # membership-gated over the complete age history.
    windowed = age_history.filter(pl.col("ts_ms") >= window.causal_read_start_ts_ms)
    covered = windowed.join(
        manifest,
        on=["symbol", "manifest_date"],
        how="inner",
        validate="m:1",
    )
    source = daily.select(tuple(LONG_KEY_SCHEMA)).cast(dict(LONG_KEY_SCHEMA)).sort(["symbol", "signal_ts_ms"])
    signal = source.filter(pl.col("signal_ts_ms") >= window.signal_start_ts_ms).sort(["symbol", "signal_ts_ms"])
    receipt = _receipt(
        sleeve="long",
        venue=venue,
        window=window,
        source=source,
        signal=signal,
        hourly_input_row_count=hourly.height,
        windowed_hourly_row_count=windowed.height,
        manifest_covered_hourly_row_count=covered.height,
        hourly_identity_input=age_history.select(tuple(HOURLY_KEY_SCHEMA)),
        manifest_identity_input=manifest_identity,
        windowed_hourly=windowed.select(tuple(HOURLY_KEY_SCHEMA)),
        covered_hourly=covered.select(tuple(HOURLY_KEY_SCHEMA)),
        age_history_hourly_row_count=age_history.height,
        age_history_start_ts_ms=(int(age_history["ts_ms"].min()) if not age_history.is_empty() else None),
        age_left_censored_symbols=left_censored_symbols,
    )
    return PopulationKeyArtifacts("long", venue, source, signal, receipt)


def long_expected_population(signal_keys: pl.DataFrame) -> pl.DataFrame:
    """Project a verified LONG signal manifest to the low-level S02 API."""

    _require_exact_schema(signal_keys, LONG_KEY_SCHEMA, name="long_signal_keys")
    return signal_keys.select("symbol", "signal_ts_ms", "symbol_age_days")


__all__ = [
    "CONTINUOUS_KEY_SCHEMA",
    "HOURLY_KEY_SCHEMA",
    "LONG_KEY_SCHEMA",
    "MANIFEST_KEY_SCHEMA",
    "PopulationKeyArtifacts",
    "PopulationKeyError",
    "PopulationKeyWindow",
    "build_continuous_population_keys",
    "build_long_population_keys",
    "long_expected_population",
]
