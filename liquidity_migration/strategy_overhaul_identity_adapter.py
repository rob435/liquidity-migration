"""Strict, outcome-blind identity/PIT annotation for strategy-overhaul S02 tapes.

This module owns only the shared identity boundary.  It does not load a data
root, inspect OHLC/feature values, build a population, calculate a rank, or read
an entry/path outcome.  Callers must declare the exact feature payload columns
that may pass through; every declared payload column must already be registered
as an S02 source column and every undeclared input column is refused.

Manifest input is already collapsed to one reviewed row per
``(venue, symbol, manifest_date)``.  Null provenance remains unknown: this
adapter never turns a missing source, observed/inferred flag, launch timestamp,
or first-bar timestamp into an observed fact.  The two age diagnostics are
derived independently and remain null when their respective anchor is absent.

The manifest contract does not currently carry publication timestamps for its
retrospective audit annotations.  Consequently these columns are identity/PIT
annotations, not decision-usable alpha features.  ``feature_data_available`` and
``data_available`` checks apply only to the caller-supplied feature tape.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Sequence
from types import MappingProxyType
from typing import Literal

import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .strategy_overhaul_phase0 import (
    INSTRUMENT_MAP_REVIEW_STATUSES,
    InstrumentMapEntry,
)
from .strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    CONTINUOUS_SIGNAL_SCHEMA_ID,
    LONG_SIGNAL_SCHEMA_ID,
)

SUPPORTED_VENUES = frozenset({"bybit", "binance"})
CONTINUOUS_CURRENT_AGE_DAYS_MIN = 240
SUPPORTED_COVERAGE_STATES = frozenset({"manifest_and_kline_pair_covered"})

MANIFEST_PAIR_COLUMNS = (
    "venue",
    "symbol",
    "manifest_date",
    "membership_source",
    "membership_inferred",
    "first_archive_observed_date",
    "reported_launch_time_ms",
    "root_first_bar_ts_ms",
    "provenance_limitation",
    "coverage_state",
)

CONTINUOUS_FEATURE_KEY_COLUMNS = (
    "symbol",
    "signal_ts_ms",
    "decision_ts_ms",
    "signal_bar_close_ts_ms",
    "feature_data_available_ts_ms",
    "data_available_ts_ms",
)
LONG_FEATURE_KEY_COLUMNS = ("symbol", "signal_ts_ms", "symbol_age_days")

COMMON_IDENTITY_COLUMNS = (
    "manifest_date",
    "membership_source",
    "membership_inferred",
    "first_archive_observed_date",
    "reported_launch_time_ms",
    "root_first_bar_ts_ms",
    "provenance_limitation",
    "coverage_state",
    "age_days_reported_launch",
    "age_days_root_first_bar",
)

IDENTITY_NULL_SEMANTICS = MappingProxyType(
    {
        "membership_source": "null means the collapsed PIT row did not establish a source",
        "membership_inferred": "null means observed-versus-inferred status is unknown",
        "first_archive_observed_date": (
            "null means no archive-observed date was established; it does not prove absence"
        ),
        "reported_launch_time_ms": ("null means the reviewed source did not persist a reported launch timestamp"),
        "root_first_bar_ts_ms": ("null means the reviewed root inventory did not establish a first 1h bar"),
        "age_days_reported_launch": ("null exactly when reported_launch_time_ms is unavailable"),
        "age_days_root_first_bar": ("null exactly when root_first_bar_ts_ms is unavailable"),
    }
)


class S02IdentityAdapterError(ValueError):
    """A strict input, PIT, mapping, timing, or cardinality invariant failed."""


def _registered_s02_payload_columns(schema_id: str) -> frozenset[str]:
    """Return exact post-builder S02 field names eligible for pass-through.

    ``FieldSpec.source_columns`` documents upstream derivation sources and can
    differ from the column emitted by the population builder (for example,
    ``simple_return_1d`` is derived from ``log_return``).  The adapter consumes
    the post-builder tape, so its allowlist must validate target field names.
    """

    return frozenset(
        field.name
        for field in ARTIFACT_SCHEMAS[schema_id].fields
        if field.implementation in {"builder", "passthrough", "projection", "semantic_mismatch"}
    )


_REGISTERED_SOURCES = {
    "continuous": _registered_s02_payload_columns(CONTINUOUS_SIGNAL_SCHEMA_ID),
    "long": _registered_s02_payload_columns(LONG_SIGNAL_SCHEMA_ID),
}


def _strict_columns(frame: pl.DataFrame, expected: Sequence[str], *, name: str) -> None:
    expected_set = set(expected)
    actual_set = set(frame.columns)
    missing = sorted(expected_set - actual_set)
    unknown = sorted(actual_set - expected_set)
    if missing or unknown or len(frame.columns) != len(expected):
        raise S02IdentityAdapterError(f"{name} projection mismatch; missing={missing}, unknown={unknown}")


def _require_dtype(
    frame: pl.DataFrame,
    column: str,
    expected: pl.DataType,
    *,
    name: str,
) -> None:
    actual = frame.schema[column]
    if actual != expected:
        raise S02IdentityAdapterError(f"{name}.{column} dtype must be {expected}, got {actual}")


def _validate_payload_allowlist(
    sleeve: Literal["continuous", "long"],
    feature_payload_allowlist: Sequence[str],
    *,
    structural_columns: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(feature_payload_allowlist, (str, bytes)):
        raise S02IdentityAdapterError("feature_payload_allowlist must be a sequence of columns")
    payload = tuple(feature_payload_allowlist)
    if any(not isinstance(name, str) or not name.strip() or name != name.strip() for name in payload):
        raise S02IdentityAdapterError("feature_payload_allowlist contains a non-string, blank, or untrimmed column")
    if len(payload) != len(set(payload)):
        raise S02IdentityAdapterError("feature_payload_allowlist contains duplicate columns")

    reserved = (
        set(structural_columns)
        | set(MANIFEST_PAIR_COLUMNS)
        | set(COMMON_IDENTITY_COLUMNS)
        | {
            "venue",
            "canonical_instrument_id",
            "current_age_source",
            "current_age_source_available",
            "current_age_240_pass",
            "symbol_age_source",
            "signal_ts_ms" if sleeve == "long" else "ts_ms",
        }
    )
    collisions = sorted(set(payload) & reserved)
    if collisions:
        raise S02IdentityAdapterError(f"feature payload collides with structural/identity columns: {collisions}")
    unregistered = sorted(set(payload) - _REGISTERED_SOURCES[sleeve])
    if unregistered:
        raise S02IdentityAdapterError(f"feature payload contains non-registered S02 source columns: {unregistered}")
    return payload


def _nonblank_string_expr(column: str, *, nullable: bool) -> pl.Expr:
    value = pl.col(column)
    valid = value.is_not_null() & (value.str.strip_chars() != "") & (value == value.str.strip_chars())
    return valid | value.is_null() if nullable else valid


def _reject_duplicate_keys(frame: pl.DataFrame, keys: Sequence[str], *, name: str) -> None:
    if frame.is_empty():
        return
    duplicates = frame.group_by(list(keys)).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise S02IdentityAdapterError(
            f"{name} has ambiguous duplicate {tuple(keys)} keys: "
            f"{duplicates.select(list(keys) + ['len']).head(5).to_dicts()}"
        )


def _validate_venue(venue: str) -> str:
    if not isinstance(venue, str) or venue != venue.strip().lower() or venue not in SUPPORTED_VENUES:
        raise S02IdentityAdapterError(f"venue must be one of {sorted(SUPPORTED_VENUES)} in normalized lowercase form")
    return venue


def _validate_continuous_feature_tape(frame: pl.DataFrame) -> None:
    _require_dtype(frame, "symbol", pl.String, name="continuous feature tape")
    for column in CONTINUOUS_FEATURE_KEY_COLUMNS[1:]:
        _require_dtype(frame, column, pl.Int64, name="continuous feature tape")
    invalid = frame.filter(
        ~_nonblank_string_expr("symbol", nullable=False)
        | pl.col("signal_ts_ms").is_null()
        | (pl.col("signal_ts_ms") < 0)
        | ((pl.col("signal_ts_ms") % MS_PER_HOUR) != 0)
        | pl.col("decision_ts_ms").is_null()
        | (pl.col("decision_ts_ms") != pl.col("signal_ts_ms") + MS_PER_HOUR)
        | (pl.col("signal_bar_close_ts_ms") != pl.col("decision_ts_ms"))
        | (pl.col("feature_data_available_ts_ms") != pl.col("decision_ts_ms"))
        | pl.col("data_available_ts_ms").is_null()
        | (pl.col("data_available_ts_ms") > pl.col("decision_ts_ms"))
        | (pl.col("data_available_ts_ms") < 0)
    )
    if not invalid.is_empty():
        raise S02IdentityAdapterError(
            "continuous feature tape violates key/grid/close-time/availability semantics: "
            f"{invalid.select(CONTINUOUS_FEATURE_KEY_COLUMNS).head(5).to_dicts()}"
        )
    _reject_duplicate_keys(
        frame,
        ("symbol", "decision_ts_ms"),
        name="continuous feature tape",
    )
    _reject_duplicate_keys(
        frame,
        ("symbol", "signal_ts_ms"),
        name="continuous feature tape",
    )


def _validate_long_feature_tape(frame: pl.DataFrame) -> None:
    _require_dtype(frame, "symbol", pl.String, name="LONG feature tape")
    _require_dtype(frame, "signal_ts_ms", pl.Int64, name="LONG feature tape")
    _require_dtype(frame, "symbol_age_days", pl.Int64, name="LONG feature tape")
    invalid = frame.filter(
        ~_nonblank_string_expr("symbol", nullable=False)
        | pl.col("signal_ts_ms").is_null()
        | (pl.col("signal_ts_ms") <= 0)
        | ((pl.col("signal_ts_ms") % MS_PER_DAY) != 0)
        | pl.col("symbol_age_days").is_null()
        | (pl.col("symbol_age_days") <= 0)
    )
    if not invalid.is_empty():
        raise S02IdentityAdapterError(
            "LONG feature tape violates key/daily-close/current-age semantics: "
            f"{invalid.select(LONG_FEATURE_KEY_COLUMNS).head(5).to_dicts()}"
        )
    _reject_duplicate_keys(frame, ("symbol", "signal_ts_ms"), name="LONG feature tape")


def validate_manifest_pairs(frame: pl.DataFrame, *, venue: str) -> None:
    """Validate the exact pair-grain PIT projection consumed by S02."""

    _strict_columns(frame, MANIFEST_PAIR_COLUMNS, name="manifest pair-grain provenance")
    for column, dtype in (
        ("venue", pl.String),
        ("symbol", pl.String),
        ("manifest_date", pl.Date),
        ("membership_source", pl.String),
        ("membership_inferred", pl.Boolean),
        ("first_archive_observed_date", pl.Date),
        ("reported_launch_time_ms", pl.Int64),
        ("root_first_bar_ts_ms", pl.Int64),
        ("provenance_limitation", pl.String),
        ("coverage_state", pl.String),
    ):
        _require_dtype(frame, column, dtype, name="manifest pair-grain provenance")

    invalid = frame.filter(
        ~_nonblank_string_expr("venue", nullable=False)
        | (pl.col("venue") != venue)
        | ~_nonblank_string_expr("symbol", nullable=False)
        | pl.col("manifest_date").is_null()
        | ~_nonblank_string_expr("membership_source", nullable=True)
        | ~_nonblank_string_expr("provenance_limitation", nullable=False)
        | ~_nonblank_string_expr("coverage_state", nullable=False)
        | ~pl.col("coverage_state").is_in(sorted(SUPPORTED_COVERAGE_STATES))
        | (pl.col("reported_launch_time_ms").is_not_null() & (pl.col("reported_launch_time_ms") < 0))
        | (
            pl.col("root_first_bar_ts_ms").is_not_null()
            & ((pl.col("root_first_bar_ts_ms") < 0) | ((pl.col("root_first_bar_ts_ms") % MS_PER_HOUR) != 0))
        )
    )
    if not invalid.is_empty():
        raise S02IdentityAdapterError(
            "manifest pair-grain provenance contains invalid venue/key/source/coverage/anchor values: "
            f"{invalid.head(5).to_dicts()}"
        )
    _reject_duplicate_keys(
        frame,
        ("venue", "symbol", "manifest_date"),
        name="manifest pair-grain provenance",
    )


def _parse_map_date(value: str | None, *, field: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise S02IdentityAdapterError(f"instrument map {field} must be ISO YYYY-MM-DD") from exc


def _validate_instrument_map(
    entries: Sequence[InstrumentMapEntry],
    *,
    version: str,
    venue: str,
) -> pl.DataFrame:
    if not isinstance(version, str) or not version.strip() or version != version.strip():
        raise S02IdentityAdapterError("instrument_map_version must be a non-blank trimmed string")
    if not entries:
        raise S02IdentityAdapterError("reviewed instrument map must not be empty")

    grouped: dict[tuple[str, str], list[tuple[dt.date, dt.date, InstrumentMapEntry]]] = defaultdict(list)
    canonical_intervals: dict[
        tuple[str, str],
        list[tuple[dt.date, dt.date, str]],
    ] = defaultdict(list)
    canonical_products: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
    for entry in entries:
        if not isinstance(entry, InstrumentMapEntry):
            raise S02IdentityAdapterError("instrument_map entries must be reviewed InstrumentMapEntry instances")
        if (
            entry.venue != entry.venue.strip().lower()
            or entry.symbol != entry.symbol.strip()
            or entry.canonical_instrument != entry.canonical_instrument.strip()
            or entry.base_asset != entry.base_asset.strip().upper()
            or entry.quote_asset != entry.quote_asset.strip().upper()
            or entry.settlement_asset != entry.settlement_asset.strip().upper()
            or entry.contract_type != entry.contract_type.strip().lower()
            or entry.mapping_source != entry.mapping_source.strip()
            or entry.review_status not in INSTRUMENT_MAP_REVIEW_STATUSES
        ):
            raise S02IdentityAdapterError(
                "instrument map must use normalized identity/product/source fields and an allowed review status"
            )
        start = _parse_map_date(entry.valid_from_date, field="valid_from_date")
        end = _parse_map_date(
            entry.valid_to_date_exclusive,
            field="valid_to_date_exclusive",
        )
        assert start is not None
        grouped[(entry.venue, entry.symbol)].append((start, end or dt.date.max, entry))
        canonical_intervals[(entry.venue, entry.canonical_instrument)].append((start, end or dt.date.max, entry.symbol))
        canonical_products[entry.canonical_instrument].add(
            (
                entry.base_asset.strip().upper(),
                entry.quote_asset.strip().upper(),
                entry.settlement_asset.strip().upper(),
                entry.contract_type.strip().lower(),
            )
        )

    conflicts = {canonical: sorted(products) for canonical, products in canonical_products.items() if len(products) > 1}
    if conflicts:
        raise S02IdentityAdapterError(f"canonical instruments have conflicting product identities: {conflicts}")

    for symbol_key, intervals in sorted(grouped.items()):
        intervals.sort(key=lambda item: (item[0], item[1], item[2].canonical_instrument))
        previous_end: dt.date | None = None
        for start, end, _entry in intervals:
            if previous_end is not None and start < previous_end:
                raise S02IdentityAdapterError(f"instrument map has ambiguous overlapping intervals for {symbol_key!r}")
            previous_end = end

    for canonical_key, intervals in sorted(canonical_intervals.items()):
        for index, (left_start, left_end, left_symbol) in enumerate(intervals):
            for right_start, right_end, right_symbol in intervals[index + 1 :]:
                if left_symbol == right_symbol:
                    continue
                if max(left_start, right_start) < min(left_end, right_end):
                    raise S02IdentityAdapterError(
                        "instrument map has a same-venue canonical alias collision for "
                        f"{canonical_key!r}: {sorted({left_symbol, right_symbol})}"
                    )

    venue_entries = [entry for entry in entries if entry.venue == venue]
    if not venue_entries:
        raise S02IdentityAdapterError(f"instrument map has no reviewed entries for venue {venue!r}")
    return pl.DataFrame(
        {
            "venue": [entry.venue for entry in venue_entries],
            "symbol": [entry.symbol for entry in venue_entries],
            "__map_valid_from": [
                _parse_map_date(entry.valid_from_date, field="valid_from_date") for entry in venue_entries
            ],
            "__map_valid_to": [
                _parse_map_date(
                    entry.valid_to_date_exclusive,
                    field="valid_to_date_exclusive",
                )
                for entry in venue_entries
            ],
            "canonical_instrument_id": [entry.canonical_instrument for entry in venue_entries],
        },
        schema={
            "venue": pl.String,
            "symbol": pl.String,
            "__map_valid_from": pl.Date,
            "__map_valid_to": pl.Date,
            "canonical_instrument_id": pl.String,
        },
    )


def _attach_map(frame: pl.DataFrame, map_frame: pl.DataFrame) -> pl.DataFrame:
    key_frame = frame.select("__row_id", "venue", "symbol", "manifest_date")
    candidates = key_frame.join(map_frame, on=["venue", "symbol"], how="left")
    matches = candidates.filter(
        pl.col("__map_valid_from").is_not_null()
        & (pl.col("__map_valid_from") <= pl.col("manifest_date"))
        & (pl.col("__map_valid_to").is_null() | (pl.col("manifest_date") < pl.col("__map_valid_to")))
    )
    match_counts = matches.group_by("__row_id").len()
    ambiguous = match_counts.filter(pl.col("len") != 1)
    if not ambiguous.is_empty():
        raise S02IdentityAdapterError(
            f"instrument map resolved ambiguously for feature rows: {ambiguous.head(5).to_dicts()}"
        )
    missing = key_frame.join(
        matches.select("__row_id").unique(),
        on="__row_id",
        how="anti",
    )
    if not missing.is_empty():
        raise S02IdentityAdapterError(
            "instrument map is missing an active reviewed mapping for feature rows: "
            f"{missing.select('venue', 'symbol', 'manifest_date').head(5).to_dicts()}"
        )
    resolved = matches.select("__row_id", "canonical_instrument_id")
    return frame.join(resolved, on="__row_id", how="left")


def _annotate_identity(
    feature_tape: pl.DataFrame,
    *,
    sleeve: Literal["continuous", "long"],
    venue: str,
    manifest_pairs: pl.DataFrame,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
    feature_payload_allowlist: Sequence[str],
    continuous_age_source: Literal["reported_launch_time_ms", "root_first_bar_ts_ms"] | None,
) -> pl.DataFrame:
    venue = _validate_venue(venue)
    structural = CONTINUOUS_FEATURE_KEY_COLUMNS if sleeve == "continuous" else LONG_FEATURE_KEY_COLUMNS
    payload = _validate_payload_allowlist(
        sleeve,
        feature_payload_allowlist,
        structural_columns=structural,
    )
    _strict_columns(
        feature_tape,
        (*structural, *payload),
        name=f"{sleeve} feature tape",
    )
    if sleeve == "continuous":
        _validate_continuous_feature_tape(feature_tape)
    else:
        _validate_long_feature_tape(feature_tape)
    validate_manifest_pairs(manifest_pairs, venue=venue)
    map_frame = _validate_instrument_map(
        instrument_map,
        version=instrument_map_version,
        venue=venue,
    )

    frame = feature_tape.with_row_index("__row_id").with_columns(pl.lit(venue, dtype=pl.String).alias("venue"))
    if sleeve == "continuous":
        frame = frame.with_columns(pl.from_epoch("signal_ts_ms", time_unit="ms").dt.date().alias("manifest_date"))
        age_reference = "decision_ts_ms"
    else:
        frame = frame.with_columns(
            pl.from_epoch(pl.col("signal_ts_ms") - 1, time_unit="ms").dt.date().alias("manifest_date"),
        )
        age_reference = "signal_ts_ms"

    pit = manifest_pairs.with_columns(pl.lit(True).alias("__pit_row_present"))
    frame = frame.join(
        pit,
        on=["venue", "symbol", "manifest_date"],
        how="left",
    )
    missing_pit = frame.filter(~pl.col("__pit_row_present").fill_null(False))
    if not missing_pit.is_empty():
        raise S02IdentityAdapterError(
            "PIT manifest row is missing for feature keys: "
            f"{missing_pit.select('venue', 'symbol', 'manifest_date').head(5).to_dicts()}"
        )
    if frame.height != feature_tape.height:
        raise S02IdentityAdapterError("PIT join changed feature-tape row cardinality")

    frame = _attach_map(frame, map_frame)
    future_anchor = frame.filter(
        (pl.col("reported_launch_time_ms").is_not_null() & (pl.col("reported_launch_time_ms") > pl.col(age_reference)))
        | (pl.col("root_first_bar_ts_ms").is_not_null() & (pl.col("root_first_bar_ts_ms") > pl.col(age_reference)))
    )
    if not future_anchor.is_empty():
        raise S02IdentityAdapterError(
            "launch/root-first-bar anchor occurs after the S02 decision: "
            f"{future_anchor.select('venue', 'symbol', 'manifest_date', age_reference, 'reported_launch_time_ms', 'root_first_bar_ts_ms').head(5).to_dicts()}"
        )

    frame = frame.with_columns(
        pl.when(pl.col("reported_launch_time_ms").is_not_null())
        .then((pl.col(age_reference) - pl.col("reported_launch_time_ms")) / float(MS_PER_DAY))
        .otherwise(None)
        .cast(pl.Float64)
        .alias("age_days_reported_launch"),
        pl.when(pl.col("root_first_bar_ts_ms").is_not_null())
        .then((pl.col(age_reference) - pl.col("root_first_bar_ts_ms")) / float(MS_PER_DAY))
        .otherwise(None)
        .cast(pl.Float64)
        .alias("age_days_root_first_bar"),
    )

    if sleeve == "continuous":
        if continuous_age_source not in {
            "reported_launch_time_ms",
            "root_first_bar_ts_ms",
        }:
            raise S02IdentityAdapterError(
                "continuous_age_source must explicitly select reported_launch_time_ms or root_first_bar_ts_ms"
            )
        selected_age = (
            "age_days_reported_launch"
            if continuous_age_source == "reported_launch_time_ms"
            else "age_days_root_first_bar"
        )
        frame = frame.with_columns(
            pl.lit(continuous_age_source).alias("current_age_source"),
            pl.col(continuous_age_source).is_not_null().alias("current_age_source_available"),
            pl.when(pl.col(continuous_age_source).is_not_null())
            .then(pl.col(selected_age) >= float(CONTINUOUS_CURRENT_AGE_DAYS_MIN))
            .otherwise(None)
            .cast(pl.Boolean)
            .alias("current_age_240_pass"),
        )
        output_columns = (
            "venue",
            "canonical_instrument_id",
            *CONTINUOUS_FEATURE_KEY_COLUMNS,
            *COMMON_IDENTITY_COLUMNS,
            "current_age_source",
            "current_age_source_available",
            "current_age_240_pass",
            *payload,
        )
        key_columns = ("venue", "symbol", "decision_ts_ms")
    else:
        frame = frame.with_columns(pl.lit("loaded_root_first_daily_row_plus_one").alias("symbol_age_source"))
        output_columns = (
            "venue",
            "canonical_instrument_id",
            "symbol",
            "signal_ts_ms",
            *COMMON_IDENTITY_COLUMNS,
            "symbol_age_days",
            "symbol_age_source",
            *payload,
        )
        key_columns = ("venue", "symbol", "signal_ts_ms")

    output = frame.sort("__row_id").select(output_columns)
    if output.height != feature_tape.height:
        raise S02IdentityAdapterError("identity adapter changed feature-tape row cardinality")
    _reject_duplicate_keys(output, key_columns, name=f"annotated {sleeve} S02 tape")
    return output


def annotate_continuous_s02_identity(
    feature_tape: pl.DataFrame,
    *,
    venue: str,
    manifest_pairs: pl.DataFrame,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
    feature_payload_allowlist: Sequence[str],
    current_age_source: Literal["reported_launch_time_ms", "root_first_bar_ts_ms"],
) -> pl.DataFrame:
    """Attach strict CONTINUOUS S02 identity/PIT fields.

    ``manifest_date`` is derived from the hourly signal/kline-open stamp, not the
    close/decision date.  This distinction matters for the 23:00 UTC signal bar.
    The current 240-day source must be selected explicitly because research-root
    and live-runtime age anchors are not silently equated.
    """

    return _annotate_identity(
        feature_tape,
        sleeve="continuous",
        venue=venue,
        manifest_pairs=manifest_pairs,
        instrument_map=instrument_map,
        instrument_map_version=instrument_map_version,
        feature_payload_allowlist=feature_payload_allowlist,
        continuous_age_source=current_age_source,
    )


def annotate_long_s02_identity(
    feature_tape: pl.DataFrame,
    *,
    venue: str,
    manifest_pairs: pl.DataFrame,
    instrument_map: Sequence[InstrumentMapEntry],
    instrument_map_version: str,
    feature_payload_allowlist: Sequence[str],
) -> pl.DataFrame:
    """Attach strict LONG S02 identity/PIT fields.

    The upstream feature builder must already have canonicalized the daily-close
    key to ``signal_ts_ms``. Membership is joined on
    ``UTC date(signal_ts_ms - 1ms)`` so a midnight signal is attached to the
    daily bar it summarizes.
    """

    return _annotate_identity(
        feature_tape,
        sleeve="long",
        venue=venue,
        manifest_pairs=manifest_pairs,
        instrument_map=instrument_map,
        instrument_map_version=instrument_map_version,
        feature_payload_allowlist=feature_payload_allowlist,
        continuous_age_source=None,
    )


__all__ = [
    "COMMON_IDENTITY_COLUMNS",
    "CONTINUOUS_CURRENT_AGE_DAYS_MIN",
    "CONTINUOUS_FEATURE_KEY_COLUMNS",
    "IDENTITY_NULL_SEMANTICS",
    "LONG_FEATURE_KEY_COLUMNS",
    "MANIFEST_PAIR_COLUMNS",
    "S02IdentityAdapterError",
    "validate_manifest_pairs",
    "SUPPORTED_COVERAGE_STATES",
    "annotate_continuous_s02_identity",
    "annotate_long_s02_identity",
]
