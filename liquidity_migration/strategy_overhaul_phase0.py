"""Outcome-blind Phase-0 inventory primitives for the strategy overhaul.

The functions in this module deliberately inspect only parquet footers plus
identity/provenance columns.  They never read OHLCV, residual-momentum values,
future labels, returns, excursions, PnL, or ranks.  The returned structures are
deterministic and JSON-ready: there is no wall-clock timestamp in an artifact.

This is an inventory surface, not a backtest and not an outcome runner.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pyarrow import parquet as pq

from liquidity_migration.strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS as CHILD_ARTIFACT_SCHEMAS,
    PROPOSED_SCHEMAS as CHILD_PROPOSED_SCHEMAS,
    registry_payload as child_schema_registry_payload,
    registry_sha256 as child_schema_registry_sha256,
)


DAY_MS = 86_400_000
HOUR_MS = 3_600_000
EPOCH_DATE = dt.date(1970, 1, 1)
MISSING = "<missing>"
NULL = "<null>"
BLANK = "<blank>"

# Only these columns may be materialised.  Other fields are visible by name and
# type through parquet schemas, but their values are never read.
_IDENTITY_COLUMNS = frozenset({"venue", "symbol", "date", "url", "ts_ms"})
_ALLOWED_PROVENANCE_COLUMNS = frozenset(
    {
        "source",
        "membership_source",
        "membership_inferred",
        "first_archive_observed_date",
        "reported_launch_time",
        "reported_launch_time_ms",
        "is_provisional",
        "feature_source",
    }
)

_KNOWN_ARCHIVE_OBSERVED_SOURCES = frozenset(
    {
        "bybit_public_trading_archive",
        "binance_vision_archive",
        "binance_public_data_archive",
    }
)
_KNOWN_INFERRED_SOURCES = frozenset({"bybit_v5_listing"})
INSTRUMENT_MAP_REVIEW_STATUSES = frozenset({"reviewed", "mechanically_derived_venue_local"})
INSTRUMENT_MAP_AUTHORITIES = frozenset(
    {
        "not_provided",
        "mechanically_derived_venue_local",
        "external_untrusted",
    }
)
_VENUE_SOURCE_LABELS = {
    "bybit": {
        # These are persisted by the current archive/ingestion and REST kline
        # builders.  Matching one is only a venue-label sanity check; callers
        # can forge every string in this registry.
        "klines_1h": frozenset(
            {
                "bybit_public_trades",
                "bybit_public_trading_archive",
                "bybit_rest",
                "bybit_v5_market_kline",
            }
        ),
        "archive_trade_manifest": frozenset({"bybit_public_trading_archive", "bybit_v5_listing"}),
    },
    "binance": {
        "klines_1h": frozenset(
            {"binance_vision_um_1h", "binance_public_data_archive", "binance_vision_archive"}
        ),
        "archive_trade_manifest": frozenset({"binance_public_data_archive", "binance_vision_archive"}),
    },
}
_SOURCE_LABEL_FAILURE_SAMPLE_LIMIT_PER_REASON = 5
_SOURCE_LABEL_FAILURE_FILE_SAMPLE_LIMIT = 20
_ROOT_BUILD_RECEIPT_CANDIDATES = (
    "root_build_receipt.json",
    "_root_build_receipt.json",
    "reports/root_build_receipt.json",
)


class Phase0IntegrityError(RuntimeError):
    """Raised when Phase-0 cannot produce a trustworthy inventory."""


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """Describe one parquet dataset without authorising value reads.

    ``relative_path`` may name either a parquet file or a partitioned directory.
    ``temporal_kind`` controls deterministic UTC month assignment and windowing.
    Partition columns absent from a parquet payload may be recovered from
    ``name=value`` path segments.
    """

    name: str
    relative_path: str
    key_columns: tuple[str, ...]
    temporal_column: str
    temporal_kind: Literal["date", "epoch_ms"]
    required_fields: tuple[str, ...] = ()
    provenance_columns: tuple[str, ...] = ()
    role: Literal["population", "membership", "feature"] = "population"
    required: bool = True
    require_daily_partitions: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.relative_path.strip():
            raise ValueError("dataset name and relative_path must be non-blank")
        relative = Path(self.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"{self.name}: relative_path must remain inside the declared venue root")
        if not self.key_columns or len(set(self.key_columns)) != len(self.key_columns):
            raise ValueError(f"{self.name}: key_columns must be non-empty and unique")
        if self.temporal_column not in self.key_columns:
            raise ValueError(f"{self.name}: temporal_column must be part of key_columns")
        disallowed = set(self.key_columns) - _IDENTITY_COLUMNS
        if disallowed:
            raise ValueError(f"{self.name}: non-identity key columns are forbidden in Phase-0: {sorted(disallowed)}")
        bad_provenance = set(self.provenance_columns) - _ALLOWED_PROVENANCE_COLUMNS
        if bad_provenance:
            raise ValueError(
                f"{self.name}: value-bearing/non-provenance columns are forbidden in Phase-0: {sorted(bad_provenance)}"
            )


@dataclass(frozen=True, slots=True)
class ProposedField:
    """One proposed child-tape field; Phase-0 records but never computes it."""

    name: str
    dtype: str
    unit: str
    nullable: bool
    null_semantics: str
    available_at: str

    def __post_init__(self) -> None:
        for label, value in (
            ("name", self.name),
            ("dtype", self.dtype),
            ("unit", self.unit),
            ("null_semantics", self.null_semantics),
            ("available_at", self.available_at),
        ):
            if not value.strip():
                raise ValueError(f"proposed field {label} must be non-blank")


@dataclass(frozen=True, slots=True)
class SleeveWindow:
    """Separate causal read warmup from the signal population window."""

    sleeve: str
    causal_read_start_date: str
    signal_start_date: str
    signal_end_date_exclusive: str

    def __post_init__(self) -> None:
        if not self.sleeve.strip():
            raise ValueError("sleeve-window sleeve must be non-blank")
        try:
            read_start = dt.date.fromisoformat(self.causal_read_start_date)
            signal_start = dt.date.fromisoformat(self.signal_start_date)
            signal_end = dt.date.fromisoformat(self.signal_end_date_exclusive)
        except ValueError as exc:
            raise ValueError("sleeve-window dates must be ISO YYYY-MM-DD") from exc
        if not read_start <= signal_start < signal_end:
            raise ValueError("sleeve window must satisfy causal_read_start <= signal_start < signal_end")


@dataclass(frozen=True, slots=True)
class InstrumentMapEntry:
    """A half-open instrument identity mapping with explicit evidence scope.

    For ``reviewed`` entries, ``contract_multiplier`` converts one venue contract
    unit to the canonical base-asset unit. Product identity fields are explicit
    so ticker equality cannot silently equate different settlement or contract
    types. ``mechanically_derived_venue_local`` entries use a local identity unit
    and never establish portability or economic-unit equivalence.
    """

    canonical_instrument: str
    venue: str
    symbol: str
    valid_from_date: str
    base_asset: str
    quote_asset: str
    settlement_asset: str
    contract_type: str
    contract_multiplier: float
    mapping_source: str
    review_status: str
    valid_to_date_exclusive: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("canonical_instrument", self.canonical_instrument),
            ("venue", self.venue),
            ("symbol", self.symbol),
            ("valid_from_date", self.valid_from_date),
            ("base_asset", self.base_asset),
            ("quote_asset", self.quote_asset),
            ("settlement_asset", self.settlement_asset),
            ("contract_type", self.contract_type),
            ("mapping_source", self.mapping_source),
            ("review_status", self.review_status),
        ):
            if not value.strip():
                raise ValueError(f"instrument-map {label} must be non-blank")
        start = _parse_date(self.valid_from_date, label="instrument-map valid_from_date")
        if self.valid_to_date_exclusive is not None:
            end = _parse_date(self.valid_to_date_exclusive, label="instrument-map valid_to_date_exclusive")
            if end <= start:
                raise ValueError("instrument-map valid_to_date_exclusive must be after valid_from_date")
        if not math.isfinite(self.contract_multiplier) or self.contract_multiplier <= 0:
            raise ValueError("instrument-map contract_multiplier must be finite and positive")
        if self.review_status != self.review_status.strip().lower():
            raise ValueError("instrument-map review_status must be normalized lowercase")
        if self.review_status not in INSTRUMENT_MAP_REVIEW_STATUSES:
            raise ValueError(f"instrument-map review_status must be one of {sorted(INSTRUMENT_MAP_REVIEW_STATUSES)}")


@dataclass(frozen=True, slots=True)
class ResourceModel:
    """Declared planning assumptions, not measured performance."""

    continuous_output_bytes_per_row: int = 512
    long_output_bytes_per_row: int = 384
    audit_scan_bytes_per_row: int = 48
    working_set_multiplier: float = 2.5
    audit_rows_per_second: int = 25_000
    audit_files_per_second: int = 250
    stress_rows_per_second: int = 10_000
    stress_files_per_second: int = 50

    def __post_init__(self) -> None:
        integer_values = (
            self.continuous_output_bytes_per_row,
            self.long_output_bytes_per_row,
            self.audit_scan_bytes_per_row,
            self.audit_rows_per_second,
            self.audit_files_per_second,
            self.stress_rows_per_second,
            self.stress_files_per_second,
        )
        if any(value <= 0 for value in integer_values):
            raise ValueError("resource-model byte/rate assumptions must be positive")
        if not math.isfinite(self.working_set_multiplier) or self.working_set_multiplier < 1:
            raise ValueError("resource-model working_set_multiplier must be finite and >= 1")


DEFAULT_DATASET_SPECS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        name="klines_1h",
        relative_path="klines_1h",
        key_columns=("symbol", "ts_ms"),
        temporal_column="ts_ms",
        temporal_kind="epoch_ms",
        required_fields=(
            "symbol",
            "ts_ms",
            "open",
            "high",
            "low",
            "close",
            "volume_base",
            "turnover_quote",
        ),
        provenance_columns=("source",),
        role="population",
        require_daily_partitions=True,
    ),
    DatasetSpec(
        name="archive_trade_manifest",
        relative_path="archive_trade_manifest",
        # The persisted storage key intentionally permits multiple URLs/sources
        # for one membership pair.  Membership is collapsed to (symbol, date)
        # only after exact storage-key validation.
        key_columns=("symbol", "date", "url"),
        temporal_column="date",
        temporal_kind="date",
        required_fields=("symbol", "date", "url"),
        provenance_columns=(
            "source",
            "membership_source",
            "membership_inferred",
            "first_archive_observed_date",
            "reported_launch_time",
            "reported_launch_time_ms",
        ),
        role="membership",
        require_daily_partitions=True,
    ),
    DatasetSpec(
        name="residual_momentum",
        relative_path="residual_momentum.parquet",
        key_columns=("symbol", "ts_ms"),
        temporal_column="ts_ms",
        temporal_kind="epoch_ms",
        required_fields=("symbol", "ts_ms", "residual_momentum", "is_provisional"),
        provenance_columns=("is_provisional", "source", "feature_source"),
        role="feature",
        required=False,
    ),
)


DEFAULT_PROPOSED_SCHEMAS: Mapping[str, tuple[ProposedField, ...]] = {
    schema_id: tuple(
        ProposedField(
            field.name,
            field.dtype,
            field.unit,
            field.nullable,
            field.null_semantics,
            field.available_at,
        )
        for field in fields
    )
    for schema_id, fields in CHILD_PROPOSED_SCHEMAS.items()
}


REGISTERED_SLEEVE_WINDOWS: tuple[SleeveWindow, ...] = (
    SleeveWindow(
        sleeve="continuous",
        causal_read_start_date="2023-02-23",
        signal_start_date="2023-04-01",
        signal_end_date_exclusive="2026-07-10",
    ),
    SleeveWindow(
        sleeve="long",
        causal_read_start_date="2023-03-16",
        signal_start_date="2023-06-15",
        signal_end_date_exclusive="2026-07-10",
    ),
)


@dataclass(slots=True)
class _ManifestStorageRow:
    symbol: str
    date: str
    url: str
    source: str | None
    membership_source: str | None
    membership_inferred: bool | None
    first_archive_observed_date: str | None
    reported_launch_time: object | None


@dataclass(slots=True)
class _DatasetScan:
    report: dict[str, Any]
    manifest_rows: list[_ManifestStorageRow]
    population_symbol_days: set[tuple[str, str]]
    population_symbol_day_bar_counts: dict[tuple[str, str], int]
    feature_symbol_days_by_status: dict[str, set[tuple[str, str]]]


def _parse_date(value: str, *, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_scalar(value: object) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if hasattr(value, "as_py"):
        return _json_scalar(value.as_py())
    return str(value)


def _normalise_bucket(value: object) -> str:
    scalar = _json_scalar(value)
    if scalar is None:
        return NULL
    if isinstance(scalar, str) and not scalar.strip():
        return BLANK
    if isinstance(scalar, bool):
        return "true" if scalar else "false"
    return str(scalar)


def _path_partitions(path: Path, base: Path) -> dict[str, str]:
    parent = path.parent
    try:
        relative_parts = parent.relative_to(base).parts if base.is_dir() else ()
    except ValueError:
        relative_parts = ()
    values: dict[str, str] = {}
    for part in relative_parts:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        previous = values.get(name)
        if previous is not None and previous != value:
            raise Phase0IntegrityError(f"conflicting {name}= path partitions in {path}")
        values[name] = value
    return values


def _discover_files(base: Path, *, start: dt.date, end: dt.date) -> list[Path]:
    """Discover only files capable of containing rows in ``[start, end)``.

    The full-PIT roots use a high-cardinality
    ``date=YYYY-MM-DD/symbol=.../part.parquet`` layout.  Recursing from the
    dataset root before applying the date filter turns a bounded inventory into
    a walk of the entire history.  Detect that canonical top-level date layout
    cheaply, then recurse only inside the requested date directories.  The
    recursive fallback is retained for genuinely unpartitioned/custom specs.
    """
    if base.is_file():
        candidates = [base] if base.suffix == ".parquet" else []
    elif base.is_dir():
        try:
            has_top_level_date_partitions = any(path.is_dir() for path in base.glob("date=*"))
        except OSError as exc:
            raise Phase0IntegrityError(f"cannot inspect parquet dataset {base}: {exc}") from exc
        if has_top_level_date_partitions:
            candidates = []
            day = start
            while day < end:
                partition = base / f"date={day.isoformat()}"
                if partition.is_dir():
                    candidates.extend(path for path in partition.rglob("*.parquet") if path.is_file())
                day += dt.timedelta(days=1)
            # Direct parquet files alongside date partitions are an ambiguous
            # mixed layout.  Include them so the existing grouping check fails
            # closed instead of silently ignoring them.
            candidates.extend(path for path in base.glob("*.parquet") if path.is_file())
            candidates.sort()
        else:
            candidates = sorted(path for path in base.rglob("*.parquet") if path.is_file())
    else:
        return []
    selected: list[Path] = []
    for path in candidates:
        partitions = _path_partitions(path, base)
        partition_date = partitions.get("date")
        if partition_date is not None:
            day = _parse_date(partition_date, label="date partition")
            if not start <= day < end:
                continue
        selected.append(path)
    return selected


def _date_and_month(value: object, kind: Literal["date", "epoch_ms"]) -> tuple[str, str]:
    scalar = _json_scalar(value)
    if scalar is None:
        raise Phase0IntegrityError("null temporal key")
    if kind == "date":
        if not isinstance(scalar, str):
            scalar = str(scalar)
        day = _parse_date(scalar, label="temporal date")
    else:
        try:
            millis = int(scalar)
        except (TypeError, ValueError, OverflowError) as exc:
            raise Phase0IntegrityError(f"invalid epoch-ms temporal key: {scalar!r}") from exc
        if millis < 0:
            raise Phase0IntegrityError(f"negative epoch-ms temporal key: {millis}")
        day_number = millis // DAY_MS
        try:
            day = EPOCH_DATE + dt.timedelta(days=day_number)
        except OverflowError as exc:
            raise Phase0IntegrityError(f"out-of-range epoch-ms temporal key: {millis}") from exc
    rendered = day.isoformat()
    return rendered, rendered[:7]


def _field_layout(parquet_file: pq.ParquetFile) -> tuple[tuple[str, str, bool], ...]:
    schema = parquet_file.schema_arrow
    return tuple((field.name, str(field.type), bool(field.nullable)) for field in schema)


def _scan_dataset(
    venue: str,
    root: Path,
    spec: DatasetSpec,
    *,
    start: dt.date,
    end: dt.date,
    batch_size: int,
) -> _DatasetScan:
    base = root / spec.relative_path
    registered_source_labels = (_VENUE_SOURCE_LABELS.get(venue) or {}).get(spec.name)
    try:
        base.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise Phase0IntegrityError(
            f"{venue}/{spec.name}: dataset path escapes the declared venue root: {base}"
        ) from exc
    files = _discover_files(base, start=start, end=end)
    if not files:
        return _DatasetScan(
            report={
                "dataset": spec.name,
                "role": spec.role,
                "required": spec.required,
                "exists": base.exists(),
                "ready": False,
                "failure_reasons": ["no parquet files in the requested window"],
                "file_count": 0,
                "row_count": 0,
                "monthly_counts": [],
                "fields": [],
                "schema_variants": [],
                "key_audit": {
                    "key_columns": list(spec.key_columns),
                    "duplicate_key_count": 0,
                    "null_or_blank_key_count": 0,
                    "status": "not_run",
                },
                "provenance_value_counts": {},
                "source_label_sanity": {
                    "registered_compatible_labels": sorted(registered_source_labels or ()),
                    "failure_observation_count": 0,
                    "failure_file_count": 0,
                    "failure_file_sample_limit": _SOURCE_LABEL_FAILURE_FILE_SAMPLE_LIMIT,
                    "failure_relative_file_sample": [],
                    "failure_reason_counts": [],
                    "sample_limit_per_reason": _SOURCE_LABEL_FAILURE_SAMPLE_LIMIT_PER_REASON,
                    "samples": [],
                    "source_labels_are_authentication": False,
                },
                "value_columns_read": [],
                "key_provenance_projection_sha256": _sha256_json([]),
            },
            manifest_rows=[],
            population_symbol_days=set(),
            population_symbol_day_bar_counts={},
            feature_symbol_days_by_status={
                "declared_non_provisional": set(),
                "declared_provisional": set(),
                "provisional_status_unknown": set(),
            },
        )

    grouped_files: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        partition_date = _path_partitions(path, base).get("date")
        group = partition_date[:7] if partition_date is not None else "<unpartitioned>"
        grouped_files[group].append(path)
    if "<unpartitioned>" in grouped_files and len(grouped_files) > 1:
        raise Phase0IntegrityError(f"{venue}/{spec.name}: mixed date-partitioned and unpartitioned parquet layouts")

    month_rows: dict[str, int] = defaultdict(int)
    month_symbols: dict[str, set[str]] = defaultdict(set)
    provenance_counts: dict[str, dict[str, int]] = {column: defaultdict(int) for column in spec.provenance_columns}
    field_state: dict[str, dict[str, Any]] = {}
    schema_variants: dict[str, dict[str, Any]] = {}
    manifest_rows: list[_ManifestStorageRow] = []
    population_symbol_days: set[tuple[str, str]] = set()
    feature_symbol_days_by_status: dict[str, set[tuple[str, str]]] = {
        "declared_non_provisional": set(),
        "declared_provisional": set(),
        "provisional_status_unknown": set(),
    }
    population_symbol_day_bars: dict[tuple[str, str], int] = defaultdict(int)
    source_label_failure_reason_counts: dict[str, int] = defaultdict(int)
    source_label_failure_files: set[str] = set()
    source_label_failure_samples_by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    off_grid_timestamp_count = 0
    materialised_columns: set[str] = set()
    selected_row_count = 0
    scanned_files = 0
    footer_row_count = 0
    key_provenance_projection = hashlib.sha256()

    for file_group in sorted(grouped_files):
        seen_by_month: dict[str, set[tuple[object, ...]]] = defaultdict(set)
        for path in sorted(grouped_files[file_group]):
            partitions = _path_partitions(path, base)
            try:
                parquet_file = pq.ParquetFile(path)
            except Exception as exc:  # noqa: BLE001 - malformed input must stop this audit
                raise Phase0IntegrityError(f"{venue}/{spec.name}: unreadable parquet {path}: {exc}") from exc
            layout = _field_layout(parquet_file)
            footer_row_count += int(parquet_file.metadata.num_rows)
            physical_names = {name for name, _dtype, _nullable in layout}
            missing_keys = [
                column for column in spec.key_columns if column not in physical_names and column not in partitions
            ]
            if missing_keys:
                raise Phase0IntegrityError(f"{venue}/{spec.name}: cannot validate key columns {missing_keys} in {path}")
            layout_payload = [
                {"name": name, "dtype": dtype, "nullable_declared": nullable} for name, dtype, nullable in layout
            ]
            variant_hash = _sha256_json(layout_payload)
            variant = schema_variants.setdefault(
                variant_hash,
                {"schema_sha256": variant_hash, "file_count": 0, "fields": layout_payload},
            )
            variant["file_count"] += 1

            for name, dtype, nullable in layout:
                state = field_state.setdefault(
                    name,
                    {
                        "name": name,
                        "dtypes": set(),
                        "nullable_declarations": set(),
                        "files_with_field": 0,
                        "rows_with_field": 0,
                    },
                )
                state["dtypes"].add(dtype)
                state["nullable_declarations"].add(nullable)
                state["files_with_field"] += 1

            physical_scan_columns = sorted((set(spec.key_columns) | set(spec.provenance_columns)) & physical_names)
            materialised_columns.update(physical_scan_columns)
            file_selected_rows = 0
            try:
                batches = parquet_file.iter_batches(batch_size=batch_size, columns=physical_scan_columns)
                for batch in batches:
                    columns = {name: batch.column(name).to_pylist() for name in physical_scan_columns}
                    for row_index in range(batch.num_rows):
                        values: dict[str, object] = {}
                        for column in set(spec.key_columns) | set(spec.provenance_columns):
                            if column in columns:
                                values[column] = columns[column][row_index]
                            elif column in partitions:
                                values[column] = partitions[column]
                            else:
                                values[column] = None

                        raw_temporal = values[spec.temporal_column]
                        date_value, month = _date_and_month(raw_temporal, spec.temporal_kind)
                        day = _parse_date(date_value, label="row temporal key")
                        partition_date = partitions.get("date")
                        if partition_date is not None and date_value != partition_date:
                            raise Phase0IntegrityError(
                                f"{venue}/{spec.name}: row temporal key {date_value} disagrees with "
                                f"date={partition_date} in {path}"
                            )
                        partition_symbol = partitions.get("symbol")
                        row_symbol = values.get("symbol")
                        if partition_symbol is not None and str(row_symbol) != partition_symbol:
                            raise Phase0IntegrityError(
                                f"{venue}/{spec.name}: row symbol {row_symbol!r} disagrees with "
                                f"symbol={partition_symbol} in {path}"
                            )
                        if not start <= day < end:
                            continue

                        key: list[object] = []
                        for column in spec.key_columns:
                            value = _json_scalar(values[column])
                            if value is None or (isinstance(value, str) and not value.strip()):
                                raise Phase0IntegrityError(
                                    f"{venue}/{spec.name}: null or blank key column {column} in {path}"
                                )
                            key.append(value)
                        key_tuple = tuple(key)
                        if key_tuple in seen_by_month[month]:
                            raise Phase0IntegrityError(
                                f"{venue}/{spec.name}: duplicate key {key_tuple!r} in month {month}"
                            )
                        seen_by_month[month].add(key_tuple)
                        key_provenance_projection.update(
                            (
                                _canonical_json(
                                    {
                                        "key": list(key_tuple),
                                        "provenance": {
                                            column: _json_scalar(values.get(column))
                                            for column in sorted(spec.provenance_columns)
                                        },
                                    }
                                )
                                + "\n"
                            ).encode("utf-8")
                        )

                        selected_row_count += 1
                        file_selected_rows += 1
                        month_rows[month] += 1
                        if row_symbol is not None:
                            month_symbols[month].add(str(row_symbol))
                        for column in spec.provenance_columns:
                            provenance_counts[column][_normalise_bucket(values.get(column))] += 1

                        if registered_source_labels is not None:
                            relative_file = path.relative_to(root).as_posix()
                            for column in ("source", "membership_source"):
                                if column not in spec.provenance_columns:
                                    continue
                                scalar = _json_scalar(values.get(column))
                                reason: str | None = None
                                if column == "source" and column not in physical_names:
                                    reason = "source_field_missing"
                                elif scalar is None:
                                    if column == "source":
                                        reason = "source_value_null"
                                elif isinstance(scalar, str) and not scalar.strip():
                                    if column == "source":
                                        reason = "source_value_blank"
                                elif str(scalar) not in registered_source_labels:
                                    reason = "incompatible_label"
                                if reason is None:
                                    continue
                                source_label_failure_reason_counts[reason] += 1
                                source_label_failure_files.add(relative_file)
                                samples = source_label_failure_samples_by_reason[reason]
                                if len(samples) < _SOURCE_LABEL_FAILURE_SAMPLE_LIMIT_PER_REASON:
                                    samples.append(
                                        {
                                            "column": column,
                                            "key": {
                                                key_column: _json_scalar(values[key_column])
                                                for key_column in spec.key_columns
                                            },
                                            "observed_value": _normalise_bucket(values.get(column)),
                                            "reason": reason,
                                            "relative_file": relative_file,
                                        }
                                    )

                        if spec.role == "population" and row_symbol is not None:
                            symbol_day = (str(row_symbol), date_value)
                            population_symbol_days.add(symbol_day)
                            population_symbol_day_bars[symbol_day] += 1
                            if spec.temporal_kind == "epoch_ms":
                                timestamp = int(_json_scalar(raw_temporal))
                                if timestamp % HOUR_MS != 0:
                                    off_grid_timestamp_count += 1

                        if spec.role == "feature" and row_symbol is not None:
                            provisional = _json_scalar(values.get("is_provisional"))
                            if provisional is False or provisional == 0:
                                status = "declared_non_provisional"
                            elif provisional is True or provisional == 1:
                                status = "declared_provisional"
                            else:
                                status = "provisional_status_unknown"
                            feature_symbol_days_by_status[status].add((str(row_symbol), date_value))

                        if spec.role == "membership":
                            inferred_raw = _json_scalar(values.get("membership_inferred"))
                            inferred: bool | None
                            if isinstance(inferred_raw, bool):
                                inferred = inferred_raw
                            elif inferred_raw in (0, 1):
                                inferred = bool(inferred_raw)
                            else:
                                inferred = None
                            first_observed = _json_scalar(values.get("first_archive_observed_date"))
                            source = _json_scalar(values.get("source"))
                            membership_source = _json_scalar(values.get("membership_source"))
                            reported_launch = _json_scalar(values.get("reported_launch_time"))
                            if reported_launch is None:
                                reported_launch = _json_scalar(values.get("reported_launch_time_ms"))
                            manifest_rows.append(
                                _ManifestStorageRow(
                                    symbol=str(values["symbol"]),
                                    date=date_value,
                                    url=str(values["url"]),
                                    source=str(source) if source is not None else None,
                                    membership_source=(
                                        str(membership_source) if membership_source is not None else None
                                    ),
                                    membership_inferred=inferred,
                                    first_archive_observed_date=(
                                        str(first_observed) if first_observed is not None else None
                                    ),
                                    reported_launch_time=reported_launch,
                                )
                            )
            except Phase0IntegrityError:
                raise
            except Exception as exc:  # noqa: BLE001 - a partial key audit is not valid
                raise Phase0IntegrityError(f"{venue}/{spec.name}: failed key/provenance scan of {path}: {exc}") from exc

            if file_selected_rows:
                scanned_files += 1
                for name, _dtype, _nullable in layout:
                    field_state[name]["rows_with_field"] += file_selected_rows

    fields: list[dict[str, Any]] = []
    for name in sorted(field_state):
        state = field_state[name]
        fields.append(
            {
                "name": name,
                "dtypes": sorted(state["dtypes"]),
                "nullable_declarations": sorted(state["nullable_declarations"]),
                "files_with_field": int(state["files_with_field"]),
                "rows_with_field": int(state["rows_with_field"]),
                "present_in_all_scanned_files": state["files_with_field"] == len(files),
                "present_for_all_rows": state["rows_with_field"] == selected_row_count,
            }
        )
    available_fields = set(field_state)
    missing_required = sorted(set(spec.required_fields) - available_fields)
    inconsistent_required = sorted(
        field["name"] for field in fields if field["name"] in spec.required_fields and not field["present_for_all_rows"]
    )
    required_dtype_drift = sorted(
        field["name"] for field in fields if field["name"] in spec.required_fields and len(field["dtypes"]) > 1
    )
    varying_field_availability = sorted(field["name"] for field in fields if not field["present_for_all_rows"])
    failure_reasons: list[str] = []
    if selected_row_count == 0:
        failure_reasons.append("no rows in the requested window")
    if missing_required:
        failure_reasons.append(f"required fields absent: {missing_required}")
    if inconsistent_required:
        failure_reasons.append(f"required fields are not available for every row: {inconsistent_required}")
    if required_dtype_drift:
        failure_reasons.append(f"required fields have dtype drift: {required_dtype_drift}")
    if off_grid_timestamp_count:
        failure_reasons.append(f"{off_grid_timestamp_count} population timestamps are off the 1h UTC grid")

    expected_dates = {(start + dt.timedelta(days=offset)).isoformat() for offset in range((end - start).days)}
    present_partition_dates = {
        partitions["date"] for path in files if "date" in (partitions := _path_partitions(path, base))
    }
    missing_partition_dates = sorted(expected_dates - present_partition_dates)
    if spec.require_daily_partitions and missing_partition_dates:
        failure_reasons.append(f"missing {len(missing_partition_dates)} required daily partitions")

    bar_count_distribution: dict[int, int] = defaultdict(int)
    for bar_count in population_symbol_day_bars.values():
        bar_count_distribution[bar_count] += 1

    report = {
        "dataset": spec.name,
        "role": spec.role,
        "required": spec.required,
        "exists": base.exists(),
        "ready": not failure_reasons,
        "failure_reasons": failure_reasons,
        "file_count": len(files),
        "files_with_rows_in_window": scanned_files,
        "row_count": selected_row_count,
        "footer_row_count_in_selected_files": footer_row_count,
        "footer_count_scope": (
            "selected partition files; unpartitioned files may include rows outside the requested window"
        ),
        "monthly_counts": [
            {
                "month": month,
                "row_count": month_rows[month],
                "symbol_count": len(month_symbols[month]),
            }
            for month in sorted(month_rows)
        ],
        "fields": fields,
        "schema_variants": sorted(schema_variants.values(), key=lambda row: row["schema_sha256"]),
        "schema_drift": {
            "detected": len(schema_variants) > 1 or bool(varying_field_availability) or bool(required_dtype_drift),
            "schema_variant_count": len(schema_variants),
            "fields_not_available_for_all_rows": varying_field_availability,
            "required_fields_with_dtype_drift": required_dtype_drift,
        },
        "partition_coverage": {
            "daily_partitions_required": spec.require_daily_partitions,
            "expected_date_count": len(expected_dates) if spec.require_daily_partitions else None,
            "present_date_count": len(present_partition_dates),
            "missing_date_count": len(missing_partition_dates) if spec.require_daily_partitions else None,
            "missing_date_sample": missing_partition_dates[:20] if spec.require_daily_partitions else [],
        },
        "grid_integrity": {
            "applicable": spec.role == "population" and spec.temporal_kind == "epoch_ms",
            "off_grid_timestamp_count": off_grid_timestamp_count,
            "symbol_day_count": len(population_symbol_day_bars),
            "complete_24h_symbol_day_count": bar_count_distribution.get(24, 0),
            "partial_symbol_day_count": sum(count for bars, count in bar_count_distribution.items() if bars < 24),
            "overfull_symbol_day_count": sum(count for bars, count in bar_count_distribution.items() if bars > 24),
            "bar_count_distribution": [
                {"bars": bars, "symbol_day_count": count} for bars, count in sorted(bar_count_distribution.items())
            ],
        },
        "key_audit": {
            "key_columns": list(spec.key_columns),
            "duplicate_key_count": 0,
            "null_or_blank_key_count": 0,
            "status": "passed",
        },
        "provenance_value_counts": {
            column: [{"value": value, "row_count": counts[value]} for value in sorted(counts)]
            for column, counts in sorted(provenance_counts.items())
        },
        "source_label_sanity": {
            "registered_compatible_labels": sorted(registered_source_labels or ()),
            "failure_observation_count": sum(source_label_failure_reason_counts.values()),
            "failure_file_count": len(source_label_failure_files),
            "failure_file_sample_limit": _SOURCE_LABEL_FAILURE_FILE_SAMPLE_LIMIT,
            "failure_relative_file_sample": sorted(source_label_failure_files)[
                :_SOURCE_LABEL_FAILURE_FILE_SAMPLE_LIMIT
            ],
            "failure_reason_counts": [
                {"reason": reason, "observation_count": source_label_failure_reason_counts[reason]}
                for reason in sorted(source_label_failure_reason_counts)
            ],
            "sample_limit_per_reason": _SOURCE_LABEL_FAILURE_SAMPLE_LIMIT_PER_REASON,
            "samples": [
                sample
                for reason in sorted(source_label_failure_samples_by_reason)
                for sample in source_label_failure_samples_by_reason[reason]
            ],
            "source_labels_are_authentication": False,
        },
        "value_columns_read": sorted(materialised_columns),
        "key_provenance_projection_sha256": key_provenance_projection.hexdigest(),
    }
    return _DatasetScan(
        report=report,
        manifest_rows=manifest_rows,
        population_symbol_days=population_symbol_days,
        population_symbol_day_bar_counts=dict(population_symbol_day_bars),
        feature_symbol_days_by_status=feature_symbol_days_by_status,
    )


def inspect_dataset(
    venue: str,
    root: str | Path,
    spec: DatasetSpec,
    *,
    start_date: str,
    end_date_exclusive: str,
    batch_size: int = 65_536,
) -> dict[str, Any]:
    """Return one deterministic field/key/provenance inventory.

    Duplicate or null identity keys raise :class:`Phase0IntegrityError`.
    Value-bearing columns are never materialised.
    """

    start, end = _validated_window(start_date, end_date_exclusive)
    _validate_venue(venue)
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return _scan_dataset(
        venue.strip().lower(),
        Path(root).expanduser(),
        spec,
        start=start,
        end=end,
        batch_size=batch_size,
    ).report


def _validated_window(start_date: str, end_date_exclusive: str) -> tuple[dt.date, dt.date]:
    start = _parse_date(start_date, label="start_date")
    end = _parse_date(end_date_exclusive, label="end_date_exclusive")
    if end <= start:
        raise ValueError("end_date_exclusive must be after start_date")
    return start, end


def _validate_venue(venue: str) -> None:
    if not venue.strip():
        raise ValueError("venue must be non-blank")


def canonicalize_phase0_roots(
    roots: Mapping[str, str | Path],
    *,
    require_registered_venues: bool = False,
) -> dict[str, Path]:
    """Resolve venue roots once and reject aliases or overlapping trees.

    String inequality is insufficient: ``/var`` and ``/private/var`` aliases,
    bind mounts, and repeated directory objects can otherwise be labelled as two
    independent venues.  The canonical mapping is safe to pass to every later
    plan, scan, map, and receipt derivation.
    """

    if not roots:
        raise ValueError("at least one venue root is required")
    canonical: dict[str, Path] = {}
    identities: dict[str, tuple[int, int]] = {}
    for raw_venue, raw_root in roots.items():
        _validate_venue(raw_venue)
        venue = raw_venue.strip().lower()
        if venue in canonical:
            raise Phase0IntegrityError(f"duplicate venue after normalization: {venue}")
        try:
            root = Path(raw_root).expanduser().resolve(strict=True)
            observed = root.stat()
        except OSError as exc:
            raise Phase0IntegrityError(f"{venue} root cannot be resolved: {raw_root}: {exc}") from exc
        if not root.is_dir():
            raise Phase0IntegrityError(f"{venue} root must be a directory: {root}")
        canonical[venue] = root
        identities[venue] = (observed.st_dev, observed.st_ino)
    if require_registered_venues and set(canonical) != {"bybit", "binance"}:
        raise Phase0IntegrityError("registered Phase-0 requires exactly the bybit and binance venue roots")
    venues = sorted(canonical)
    for index, left in enumerate(venues):
        for right in venues[index + 1 :]:
            left_root = canonical[left]
            right_root = canonical[right]
            if identities[left] == identities[right]:
                raise Phase0IntegrityError(
                    f"venue roots resolve to the same physical directory: {left}={left_root}, {right}={right_root}"
                )
            if left_root.is_relative_to(right_root) or right_root.is_relative_to(left_root):
                raise Phase0IntegrityError(
                    f"venue roots must not overlap: {left}={left_root}, {right}={right_root}"
                )
    return canonical


def _source_for(row: _ManifestStorageRow) -> str | None:
    return row.membership_source or row.source


def _row_is_archive_observed(row: _ManifestStorageRow) -> bool:
    source = _source_for(row)
    # ``membership_inferred=False`` is a caller-controlled consistency flag,
    # not provenance.  Only a recognized archive source label can support the
    # narrow archive-observed classification, and root-lineage authentication
    # remains a separate unresolved gate.
    return source in _KNOWN_ARCHIVE_OBSERVED_SOURCES


def _row_is_explicitly_inferred(row: _ManifestStorageRow) -> bool:
    source = _source_for(row)
    if row.membership_inferred is True:
        return True
    return source in _KNOWN_INFERRED_SOURCES or row.url in _KNOWN_INFERRED_SOURCES


def _build_pit_provenance(
    manifests: Mapping[str, Sequence[_ManifestStorageRow]],
    *,
    inventory_start_date: str,
) -> tuple[dict[str, Any], dict[str, list[tuple[str, str]]]]:
    report: dict[str, Any] = {
        "membership_storage_key": ["venue", "symbol", "date", "url"],
        "collapsed_membership_key": ["venue", "symbol", "date"],
        "venues": {},
    }
    collapsed_by_venue: dict[str, list[tuple[str, str]]] = {}
    for venue in sorted(manifests):
        rows = manifests[venue]
        grouped: dict[tuple[str, str], list[_ManifestStorageRow]] = defaultdict(list)
        for row in rows:
            grouped[(row.symbol, row.date)].append(row)
        collapsed_by_venue[venue] = sorted(grouped)
        first_observed_derived: dict[str, str] = {}
        for row in rows:
            if _row_is_archive_observed(row):
                previous = first_observed_derived.get(row.symbol)
                if previous is None or row.date < previous:
                    first_observed_derived[row.symbol] = row.date

        monthly: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "storage_rows": 0,
                "membership_pairs": 0,
                "symbols": set(),
                "archive_observed_pairs": 0,
                "inferred_pairs": 0,
                "unknown_observation_status_pairs": 0,
                "mixed_observed_inferred_pairs": 0,
                "contradictory_provenance_pairs": 0,
                "source_counts": defaultdict(int),
            }
        )
        status_counts: dict[str, int] = defaultdict(int)
        pairs_with_reported_launch = 0
        pairs_with_explicit_first_observed = 0
        mixed_observed_inferred_pairs = 0
        contradictory_provenance_pairs = 0
        internally_conflicting_storage_rows = 0
        for (symbol, date_value), pair_rows in sorted(grouped.items()):
            month = date_value[:7]
            bucket = monthly[month]
            bucket["membership_pairs"] += 1
            bucket["symbols"].add(symbol)
            bucket["storage_rows"] += len(pair_rows)
            observed = any(_row_is_archive_observed(row) for row in pair_rows)
            explicitly_inferred = any(_row_is_explicitly_inferred(row) for row in pair_rows)
            mixed_evidence = observed and explicitly_inferred
            row_conflicts = sum(
                (row.membership_inferred is True and _source_for(row) in _KNOWN_ARCHIVE_OBSERVED_SOURCES)
                or (
                    row.membership_inferred is False
                    and (_source_for(row) in _KNOWN_INFERRED_SOURCES or row.url in _KNOWN_INFERRED_SOURCES)
                )
                for row in pair_rows
            )
            if mixed_evidence:
                mixed_observed_inferred_pairs += 1
                bucket["mixed_observed_inferred_pairs"] += 1
            if row_conflicts:
                contradictory_provenance_pairs += 1
                internally_conflicting_storage_rows += row_conflicts
                bucket["contradictory_provenance_pairs"] += 1
            if observed:
                status = "archive_observed"
                bucket["archive_observed_pairs"] += 1
            elif explicitly_inferred:
                status = "inferred"
                bucket["inferred_pairs"] += 1
            else:
                status = "unknown"
                bucket["unknown_observation_status_pairs"] += 1
            status_counts[status] += 1
            if any(row.reported_launch_time is not None for row in pair_rows):
                pairs_with_reported_launch += 1
            if any(row.first_archive_observed_date is not None for row in pair_rows):
                pairs_with_explicit_first_observed += 1
            for row in pair_rows:
                source = _source_for(row) or MISSING
                bucket["source_counts"][source] += 1

        symbols = sorted({symbol for symbol, _date_value in grouped})
        report["venues"][venue] = {
            "storage_row_count": len(rows),
            "membership_pair_count": len(grouped),
            "collapsed_duplicate_source_row_count": len(rows) - len(grouped),
            "symbol_count": len(symbols),
            "observation_status_counts": [
                {"status": status, "membership_pair_count": status_counts[status]}
                for status in ("archive_observed", "inferred", "unknown")
            ],
            "observed_wins_collapse_policy": True,
            "mixed_observed_inferred_membership_pair_count": mixed_observed_inferred_pairs,
            "contradictory_provenance_membership_pair_count": contradictory_provenance_pairs,
            "internally_conflicting_storage_row_count": internally_conflicting_storage_rows,
            "first_archive_observed": {
                "derived_symbol_count": len(first_observed_derived),
                "explicit_membership_pair_count": pairs_with_explicit_first_observed,
                "inventory_start_date": inventory_start_date,
                "derived_value_scope": "earliest archive-observed row inside this inventory window",
                "left_censoring_possible": True,
                "symbols_observed_on_left_boundary_count": sum(
                    date_value == inventory_start_date for date_value in first_observed_derived.values()
                ),
                "limitation": (
                    "the derived date is not asserted to be the instrument's first-ever archive "
                    "observation; missing does not prove the instrument was absent"
                ),
            },
            "reported_launch_time_membership_pair_count": pairs_with_reported_launch,
            "monthly_counts": [
                {
                    "month": month,
                    "storage_row_count": monthly[month]["storage_rows"],
                    "membership_pair_count": monthly[month]["membership_pairs"],
                    "symbol_count": len(monthly[month]["symbols"]),
                    "archive_observed_membership_pair_count": monthly[month]["archive_observed_pairs"],
                    "inferred_membership_pair_count": monthly[month]["inferred_pairs"],
                    "unknown_observation_status_membership_pair_count": monthly[month][
                        "unknown_observation_status_pairs"
                    ],
                    "mixed_observed_inferred_membership_pair_count": monthly[month]["mixed_observed_inferred_pairs"],
                    "contradictory_provenance_membership_pair_count": monthly[month]["contradictory_provenance_pairs"],
                    "source_storage_row_counts": [
                        {"source": source, "storage_row_count": count}
                        for source, count in sorted(monthly[month]["source_counts"].items())
                    ],
                }
                for month in sorted(monthly)
            ],
            "limitations": [
                "manifest coverage is not universal hourly tradability authority",
                "unknown provenance is retained as unknown rather than promoted to observed or inferred",
                "mixed observed/inferred evidence is counted explicitly even though observed wins the collapsed status",
            ],
        }
    return report, collapsed_by_venue


def _build_manifest_kline_coverage(
    collapsed: Mapping[str, Sequence[tuple[str, str]]],
    population_symbol_days: Mapping[str, set[tuple[str, str]]],
    population_symbol_day_bar_counts: Mapping[
        str,
        Mapping[tuple[str, str], int],
    ],
) -> dict[str, Any]:
    """Compare identity-only symbol/day support; never inspect bar values."""

    venues: dict[str, Any] = {}
    for venue in sorted(collapsed):
        membership = set(collapsed[venue])
        klines = population_symbol_days.get(venue, set())
        covered = membership & klines
        membership_only = membership - klines
        kline_only = klines - membership
        bar_counts = population_symbol_day_bar_counts.get(venue, {})
        months = sorted({date_value[:7] for _symbol, date_value in membership | klines})
        venues[venue] = {
            "status": "complete" if not membership_only else "partial",
            "membership_symbol_day_count": len(membership),
            "kline_symbol_day_count": len(klines),
            "covered_membership_symbol_day_count": len(covered),
            "covered_kline_row_count": sum(bar_counts.get(key, 0) for key in covered),
            "membership_without_kline_count": len(membership_only),
            "kline_without_membership_count": len(kline_only),
            "membership_coverage_fraction": len(covered) / len(membership) if membership else 0.0,
            "membership_without_kline_sample": [
                {"symbol": symbol, "date": date_value} for symbol, date_value in sorted(membership_only)[:20]
            ],
            "kline_without_membership_sample": [
                {"symbol": symbol, "date": date_value} for symbol, date_value in sorted(kline_only)[:20]
            ],
            "monthly_counts": [
                {
                    "month": month,
                    "membership_symbol_day_count": sum(
                        date_value.startswith(month) for _symbol, date_value in membership
                    ),
                    "kline_symbol_day_count": sum(date_value.startswith(month) for _symbol, date_value in klines),
                    "covered_membership_symbol_day_count": sum(
                        date_value.startswith(month) for _symbol, date_value in covered
                    ),
                    "covered_kline_row_count": sum(
                        bar_counts.get((symbol, date_value), 0)
                        for symbol, date_value in covered
                        if date_value.startswith(month)
                    ),
                    "membership_without_kline_count": sum(
                        date_value.startswith(month) for _symbol, date_value in membership_only
                    ),
                }
                for month in months
            ],
            "daily_counts": [
                {
                    "date": day,
                    "membership_symbol_day_count": sum(date_value == day for _symbol, date_value in membership),
                    "kline_symbol_day_count": sum(date_value == day for _symbol, date_value in klines),
                    "covered_membership_symbol_day_count": sum(date_value == day for _symbol, date_value in covered),
                    "covered_kline_row_count": sum(
                        bar_counts.get((symbol, date_value), 0) for symbol, date_value in covered if date_value == day
                    ),
                }
                for day in sorted({date_value for _symbol, date_value in membership | klines})
            ],
            "limitation": (
                "coverage proves only that at least one identity-keyed 1h row exists for a manifest symbol/day; "
                "grid completeness is reported separately and tradability is not inferred"
            ),
        }
    return {
        "join_key": ["venue", "symbol", "date"],
        "venues": venues,
        "all_venues_complete": bool(venues) and all(row["status"] == "complete" for row in venues.values()),
    }


def _build_rmom_population_coverage(
    collapsed: Mapping[str, Sequence[tuple[str, str]]],
    population_symbol_days: Mapping[str, set[tuple[str, str]]],
    feature_symbol_days_by_status: Mapping[
        str,
        Mapping[str, set[tuple[str, str]]],
    ],
) -> dict[str, Any]:
    """Compare RMOM identity/provisional flags with manifest-covered kline days.

    Phase 0 deliberately does not read ``residual_momentum`` values, so a
    non-provisional identity row is not called numerically valid or stable.
    """

    venues: dict[str, Any] = {}
    for venue in sorted(collapsed):
        eligible = set(collapsed[venue]) & population_symbol_days.get(venue, set())
        statuses = feature_symbol_days_by_status.get(venue, {})
        non_provisional = set(statuses.get("declared_non_provisional", set()))
        provisional = set(statuses.get("declared_provisional", set()))
        unknown = set(statuses.get("provisional_status_unknown", set()))
        covered = eligible & (non_provisional | provisional | unknown)
        mixed = eligible & ((non_provisional & provisional) | (non_provisional & unknown) | (provisional & unknown))
        non_provisional_only = (eligible & non_provisional) - provisional - unknown
        provisional_only = (eligible & provisional) - non_provisional - unknown
        unknown_only = (eligible & unknown) - non_provisional - provisional
        missing = eligible - covered
        months = sorted({date_value[:7] for _symbol, date_value in eligible})
        venues[venue] = {
            "population_definition": "manifest membership intersect hourly-kline symbol/day identity",
            "population_symbol_day_count": len(eligible),
            "rmom_identity_covered_symbol_day_count": len(covered),
            "rmom_identity_coverage_fraction": len(covered) / len(eligible) if eligible else 0.0,
            "declared_non_provisional_only_symbol_day_count": len(non_provisional_only),
            "declared_provisional_only_symbol_day_count": len(provisional_only),
            "provisional_status_unknown_only_symbol_day_count": len(unknown_only),
            "mixed_provisional_status_symbol_day_count": len(mixed),
            "missing_rmom_identity_symbol_day_count": len(missing),
            "missing_rmom_identity_sample": [
                {"symbol": symbol, "date": date_value} for symbol, date_value in sorted(missing)[:20]
            ],
            "rmom_rows_outside_population_symbol_day_count": len((non_provisional | provisional | unknown) - eligible),
            "monthly_counts": [
                {
                    "month": month,
                    "population_symbol_day_count": sum(
                        date_value.startswith(month) for _symbol, date_value in eligible
                    ),
                    "declared_non_provisional_only_symbol_day_count": sum(
                        date_value.startswith(month) for _symbol, date_value in non_provisional_only
                    ),
                    "declared_provisional_only_symbol_day_count": sum(
                        date_value.startswith(month) for _symbol, date_value in provisional_only
                    ),
                    "provisional_status_unknown_only_symbol_day_count": sum(
                        date_value.startswith(month) for _symbol, date_value in unknown_only
                    ),
                    "mixed_provisional_status_symbol_day_count": sum(
                        date_value.startswith(month) for _symbol, date_value in mixed
                    ),
                    "missing_rmom_identity_symbol_day_count": sum(
                        date_value.startswith(month) for _symbol, date_value in missing
                    ),
                }
                for month in months
            ],
            "daily_counts": [
                {
                    "date": day,
                    "population_symbol_day_count": sum(date_value == day for _symbol, date_value in eligible),
                    "declared_non_provisional_only_symbol_day_count": sum(
                        date_value == day for _symbol, date_value in non_provisional_only
                    ),
                    "declared_provisional_only_symbol_day_count": sum(
                        date_value == day for _symbol, date_value in provisional_only
                    ),
                    "provisional_status_unknown_only_symbol_day_count": sum(
                        date_value == day for _symbol, date_value in unknown_only
                    ),
                    "mixed_provisional_status_symbol_day_count": sum(
                        date_value == day for _symbol, date_value in mixed
                    ),
                    "missing_rmom_identity_symbol_day_count": sum(date_value == day for _symbol, date_value in missing),
                }
                for day in sorted({date_value for _symbol, date_value in eligible})
            ],
            "numeric_residual_momentum_validity": "DEFERRED_TO_S02",
            "limitation": (
                "declared non-provisional means only is_provisional=false; Phase 0 did not read "
                "the residual_momentum value and cannot prove finite or usable features; this "
                "day-grain identity join is feasibility-only and S02 must enforce exact source "
                "and availability-timestamp causality"
            ),
        }
    return {
        "join_key": ["venue", "symbol", "date"],
        "venues": venues,
        "numeric_values_read": False,
        "s02_stable_value_readiness": "DEFERRED",
    }


def _provenance_values(report: Mapping[str, Any], column: str) -> set[str]:
    raw = report.get("provenance_value_counts") or {}
    rows = raw.get(column) or []
    return {
        str(row["value"])
        for row in rows
        if isinstance(row, dict) and row.get("value") not in {None, MISSING, NULL, BLANK}
    }


def _root_receipt_diagnostic(
    root: Path,
    *,
    venue: str,
    start_date: str,
    signal_end_date_exclusive: str,
) -> dict[str, Any]:
    candidates = [root / relative for relative in _ROOT_BUILD_RECEIPT_CANDIDATES]
    existing = [path for path in candidates if path.is_file()]
    if len(existing) > 1:
        return {
            "status": "AMBIGUOUS",
            "present": True,
            "candidate_paths": [str(path) for path in existing],
            "current_content_fingerprint_recomputed": False,
            "upstream_authenticity_proven": False,
            "canonical_s01_root_lineage_ready": False,
            "blocker": "multiple root-build receipt candidates exist",
        }
    if not existing:
        return {
            "status": "ABSENT",
            "present": False,
            "candidate_paths": [str(path) for path in candidates],
            "current_content_fingerprint_recomputed": False,
            "upstream_authenticity_proven": False,
            "canonical_s01_root_lineage_ready": False,
            "blocker": "no canonical root-build receipt is present",
        }
    path = existing[0]
    data = path.read_bytes()
    failures: list[str] = []
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        payload = None
        failures.append(f"receipt is not readable JSON: {exc}")
    if isinstance(payload, dict):
        expected = {
            "schema_version": 1,
            "receipt_type": "full_pit_root_verification",
            "status": "passed",
            "venue": venue,
            "root": str(root),
            "start_date": start_date,
            "signal_end_date_exclusive": signal_end_date_exclusive,
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                failures.append(f"{field} does not match the registered root")
        fingerprint = payload.get("data_fingerprint_sha256")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            failures.append("data_fingerprint_sha256 is absent or malformed")
    elif payload is not None:
        failures.append("receipt payload is not a JSON object")
    return {
        "status": "SELF_ASSERTED_COMPATIBLE" if not failures else "INCOMPATIBLE",
        "present": True,
        "path": str(path),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "file_bytes": len(data),
        "declared_fields_compatible": not failures,
        "failures": failures,
        "current_content_fingerprint_recomputed": False,
        "upstream_authenticity_proven": False,
        "canonical_s01_root_lineage_ready": False,
        "blocker": (
            "the receipt is self-authored/unsigned and Phase 0 does not recompute its outcome-bearing content "
            "fingerprint; presence and matching labels do not prove current root authenticity"
        ),
    }


def _build_root_lineage(
    roots: Mapping[str, Path],
    field_availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    start_date: str,
    signal_end_date_exclusive: str,
) -> dict[str, Any]:
    venues: dict[str, Any] = {}
    for venue, root in sorted(roots.items()):
        expected_by_dataset = _VENUE_SOURCE_LABELS.get(venue, {})
        dataset_rows: dict[str, Any] = {}
        dataset_compatibility: list[bool] = []
        for dataset in ("klines_1h", "archive_trade_manifest"):
            report = (field_availability.get(venue) or {}).get(dataset) or {}
            observed = _provenance_values(report, "source") | _provenance_values(report, "membership_source")
            expected = set(expected_by_dataset.get(dataset, frozenset()))
            incompatible = sorted(observed - expected)
            compatible = sorted(observed & expected)
            source_field = next(
                (field for field in report.get("fields", []) if isinstance(field, dict) and field.get("name") == "source"),
                None,
            )
            source_label_sanity = report.get("source_label_sanity") or {}
            failure_reason_counts = {
                str(row["reason"]): int(row["observation_count"])
                for row in source_label_sanity.get("failure_reason_counts", [])
                if isinstance(row, dict) and "reason" in row and "observation_count" in row
            }
            source_field_present_for_all_rows = bool(source_field and source_field.get("present_for_all_rows"))
            source_value_present_for_all_rows = bool(report.get("row_count")) and not any(
                failure_reason_counts.get(reason, 0)
                for reason in ("source_field_missing", "source_value_blank", "source_value_null")
            )
            source_present_for_all_rows = source_field_present_for_all_rows and source_value_present_for_all_rows
            failure_observation_count = int(source_label_sanity.get("failure_observation_count", 0))
            compatibility_ready = (
                bool(observed)
                and not incompatible
                and source_present_for_all_rows
                and failure_observation_count == 0
            )
            dataset_compatibility.append(compatibility_ready)
            dataset_rows[dataset] = {
                "observed_source_labels": sorted(observed),
                "compatible_source_labels": compatible,
                "incompatible_source_labels": incompatible,
                "registered_compatible_labels": sorted(expected),
                "source_field_present_for_all_rows": source_field_present_for_all_rows,
                "source_value_present_for_all_rows": source_value_present_for_all_rows,
                "source_present_for_all_rows": source_present_for_all_rows,
                "source_label_failure_observation_count": failure_observation_count,
                "source_label_failure_file_count": int(source_label_sanity.get("failure_file_count", 0)),
                "source_label_failure_file_sample_limit": source_label_sanity.get(
                    "failure_file_sample_limit",
                    _SOURCE_LABEL_FAILURE_FILE_SAMPLE_LIMIT,
                ),
                "source_label_failure_relative_file_sample": source_label_sanity.get(
                    "failure_relative_file_sample",
                    [],
                ),
                "source_label_failure_reason_counts": source_label_sanity.get("failure_reason_counts", []),
                "source_label_failure_sample_limit_per_reason": source_label_sanity.get(
                    "sample_limit_per_reason",
                    _SOURCE_LABEL_FAILURE_SAMPLE_LIMIT_PER_REASON,
                ),
                "source_label_failure_samples": source_label_sanity.get("samples", []),
                "source_label_compatibility_ready": compatibility_ready,
            }
        if any(row["incompatible_source_labels"] for row in dataset_rows.values()):
            compatibility_status = "INCOMPATIBLE"
        elif dataset_compatibility and all(dataset_compatibility):
            compatibility_status = "COMPATIBLE_SELF_REPORTED"
        else:
            compatibility_status = "UNOBSERVED"
        observed = root.stat()
        receipt = _root_receipt_diagnostic(
            root,
            venue=venue,
            start_date=start_date,
            signal_end_date_exclusive=signal_end_date_exclusive,
        )
        venues[venue] = {
            "canonical_root": str(root),
            "physical_directory_identity": {"st_dev": observed.st_dev, "st_ino": observed.st_ino},
            "source_label_compatibility_status": compatibility_status,
            "source_label_compatibility_ready": compatibility_status == "COMPATIBLE_SELF_REPORTED",
            "datasets": dataset_rows,
            "source_labels_are_authentication": False,
            "source_label_limitation": (
                "compatible persisted strings are a necessary venue-label sanity check only; callers can forge "
                "them and they do not authenticate an upstream archive"
            ),
            "root_build_receipt": receipt,
            "upstream_authenticity_proven": False,
            "canonical_s01_root_lineage_ready": False,
        }
    return {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_phase0_root_lineage_diagnostic",
        "venues": venues,
        "all_venue_source_labels_compatible": bool(venues)
        and all(row["source_label_compatibility_ready"] for row in venues.values()),
        "all_upstream_authenticity_proven": False,
        "canonical_s01_root_lineage_ready": False,
        "limitations": [
            "venue/source labels are self-reported and reject obvious swaps but do not authenticate roots",
            "legacy root-build receipts are unsigned and their content fingerprints are not recomputed in Phase 0",
            "a canonical stage-specific root lineage receipt remains required before S01",
        ],
    }


def _normalise_map_entries(
    instrument_map: Sequence[InstrumentMapEntry | Mapping[str, object]],
) -> list[InstrumentMapEntry]:
    entries: list[InstrumentMapEntry] = []
    for raw in instrument_map:
        if isinstance(raw, InstrumentMapEntry):
            entry = raw
        else:
            required = (
                "canonical_instrument",
                "venue",
                "symbol",
                "valid_from_date",
                "base_asset",
                "quote_asset",
                "settlement_asset",
                "contract_type",
                "contract_multiplier",
                "mapping_source",
                "review_status",
            )
            null_required = [name for name in required if raw.get(name) is None or not str(raw.get(name)).strip()]
            if null_required:
                raise Phase0IntegrityError(f"instrument-map record has null or blank key fields: {null_required}")
            try:
                entry = InstrumentMapEntry(
                    canonical_instrument=str(raw["canonical_instrument"]),
                    venue=str(raw["venue"]),
                    symbol=str(raw["symbol"]),
                    valid_from_date=str(raw["valid_from_date"]),
                    base_asset=str(raw["base_asset"]),
                    quote_asset=str(raw["quote_asset"]),
                    settlement_asset=str(raw["settlement_asset"]),
                    contract_type=str(raw["contract_type"]),
                    contract_multiplier=float(str(raw["contract_multiplier"])),
                    mapping_source=str(raw["mapping_source"]),
                    review_status=str(raw["review_status"]),
                    valid_to_date_exclusive=(
                        str(raw["valid_to_date_exclusive"]) if raw.get("valid_to_date_exclusive") is not None else None
                    ),
                )
            except KeyError as exc:
                raise ValueError(f"instrument-map record missing field {exc.args[0]}") from exc
        entries.append(
            InstrumentMapEntry(
                canonical_instrument=entry.canonical_instrument.strip(),
                venue=entry.venue.strip().lower(),
                symbol=entry.symbol.strip(),
                valid_from_date=entry.valid_from_date,
                base_asset=entry.base_asset.strip().upper(),
                quote_asset=entry.quote_asset.strip().upper(),
                settlement_asset=entry.settlement_asset.strip().upper(),
                contract_type=entry.contract_type.strip().lower(),
                contract_multiplier=entry.contract_multiplier,
                mapping_source=entry.mapping_source.strip(),
                review_status=entry.review_status.strip().lower(),
                valid_to_date_exclusive=entry.valid_to_date_exclusive,
            )
        )
    entries.sort(
        key=lambda entry: (
            entry.venue,
            entry.symbol,
            entry.valid_from_date,
            entry.valid_to_date_exclusive or "9999-12-31",
            entry.canonical_instrument,
        )
    )
    seen: set[tuple[str, str, str]] = set()
    by_symbol: dict[tuple[str, str], list[InstrumentMapEntry]] = defaultdict(list)
    for entry in entries:
        key = (entry.venue, entry.symbol, entry.valid_from_date)
        if key in seen:
            raise Phase0IntegrityError(f"duplicate instrument-map key {key!r}")
        seen.add(key)
        by_symbol[(entry.venue, entry.symbol)].append(entry)
    for symbol_key, symbol_entries in sorted(by_symbol.items()):
        previous_end: dt.date | None = None
        for entry in symbol_entries:
            start = _parse_date(entry.valid_from_date, label="instrument-map valid_from_date")
            if previous_end is not None and start < previous_end:
                raise Phase0IntegrityError(f"overlapping instrument-map intervals for {symbol_key!r}")
            previous_end = (
                _parse_date(entry.valid_to_date_exclusive, label="instrument-map valid_to_date_exclusive")
                if entry.valid_to_date_exclusive is not None
                else dt.date.max
            )
    canonical_products: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
    for entry in entries:
        canonical_products[entry.canonical_instrument].add(
            (
                entry.base_asset,
                entry.quote_asset,
                entry.settlement_asset,
                entry.contract_type,
            )
        )
    conflicts = {canonical: sorted(products) for canonical, products in canonical_products.items() if len(products) > 1}
    if conflicts:
        raise Phase0IntegrityError(f"canonical instruments have conflicting product identities: {conflicts}")
    return entries


def _resolve_map(
    entries_by_symbol: Mapping[tuple[str, str], Sequence[InstrumentMapEntry]],
    venue: str,
    symbol: str,
    date_value: str,
) -> InstrumentMapEntry | None:
    day = _parse_date(date_value, label="membership date")
    for entry in entries_by_symbol.get((venue, symbol), ()):
        start = _parse_date(entry.valid_from_date, label="instrument-map valid_from_date")
        end = (
            _parse_date(entry.valid_to_date_exclusive, label="instrument-map valid_to_date_exclusive")
            if entry.valid_to_date_exclusive is not None
            else dt.date.max
        )
        if start <= day < end:
            return entry
    return None


def _build_instrument_map_coverage(
    collapsed: Mapping[str, Sequence[tuple[str, str]]],
    *,
    instrument_map: Sequence[InstrumentMapEntry | Mapping[str, object]],
    instrument_map_version: str | None,
    instrument_map_authority: str = "external_untrusted",
) -> dict[str, Any]:
    if instrument_map_authority not in INSTRUMENT_MAP_AUTHORITIES:
        raise ValueError(f"instrument_map_authority must be one of {sorted(INSTRUMENT_MAP_AUTHORITIES)}")
    entries = _normalise_map_entries(instrument_map)
    raw_symbols = {venue: {symbol for symbol, _date_value in pairs} for venue, pairs in collapsed.items()}
    venue_names = sorted(collapsed)
    raw_candidates = sorted(set.intersection(*(raw_symbols[venue] for venue in venue_names))) if venue_names else []
    if not entries:
        return {
            "status": "not_provided",
            "map_version": None,
            "map_authority": "not_provided",
            "venue_local_identity_ready": False,
            "portable_matching_ready": False,
            "portable_matching_unready_reasons": ["no trusted instrument map was supplied"],
            "external_review_status_trusted": False,
            "trusted_reviewer_bound_receipt_present": False,
            "raw_ticker_candidates": {
                "count": len(raw_candidates),
                "symbols": raw_candidates,
                "authoritative": False,
                "limitation": (
                    "raw ticker equality is only a candidate list; it does not resolve migrations, "
                    "multipliers, or contract lifecycle"
                ),
            },
            "venues": {
                venue: {
                    "membership_pair_count": len(collapsed[venue]),
                    "mapped_membership_pair_count": 0,
                    "unmapped_membership_pair_count": len(collapsed[venue]),
                    "row_coverage_fraction": 0.0,
                    "membership_symbol_count": len(raw_symbols[venue]),
                    "mapped_symbol_count": 0,
                    "unmapped_symbols": sorted(raw_symbols[venue]),
                    "monthly_counts": [
                        {
                            "month": month,
                            "membership_pair_count": sum(
                                date_value.startswith(month) for _symbol, date_value in collapsed[venue]
                            ),
                            "mapped_membership_pair_count": 0,
                            "unmapped_membership_pair_count": sum(
                                date_value.startswith(month) for _symbol, date_value in collapsed[venue]
                            ),
                        }
                        for month in sorted({date_value[:7] for _symbol, date_value in collapsed[venue]})
                    ],
                }
                for venue in venue_names
            },
            "cross_venue": {
                "all_venue_matched_canonical_instrument_days": 0,
                "reason_unready": "no versioned instrument map was supplied",
            },
        }
    if instrument_map_version is None or not instrument_map_version.strip():
        raise ValueError("instrument_map_version must be non-blank when map entries are supplied")

    entries_by_symbol: dict[tuple[str, str], list[InstrumentMapEntry]] = defaultdict(list)
    for entry in entries:
        entries_by_symbol[(entry.venue, entry.symbol)].append(entry)

    by_venue_canonical_days: dict[str, set[tuple[str, str]]] = defaultdict(set)
    by_venue_assignments: dict[
        str,
        dict[tuple[str, str], list[tuple[str, float]]],
    ] = defaultdict(lambda: defaultdict(list))
    venue_reports: dict[str, Any] = {}
    for venue in venue_names:
        mapped = 0
        unmapped_symbols: set[str] = set()
        mapped_symbols: set[str] = set()
        monthly: dict[str, dict[str, int]] = defaultdict(
            lambda: {"membership_pair_count": 0, "mapped": 0, "unmapped": 0}
        )
        for symbol, date_value in collapsed[venue]:
            month = date_value[:7]
            monthly[month]["membership_pair_count"] += 1
            match = _resolve_map(entries_by_symbol, venue, symbol, date_value)
            if match is None:
                monthly[month]["unmapped"] += 1
                unmapped_symbols.add(symbol)
                continue
            mapped += 1
            monthly[month]["mapped"] += 1
            mapped_symbols.add(symbol)
            canonical_day = (match.canonical_instrument, date_value)
            by_venue_canonical_days[venue].add(canonical_day)
            by_venue_assignments[venue][canonical_day].append((symbol, match.contract_multiplier))
        total = len(collapsed[venue])
        alias_collisions = {
            canonical_day: assignments
            for canonical_day, assignments in by_venue_assignments[venue].items()
            if len({symbol for symbol, _multiplier in assignments}) > 1
        }
        venue_reports[venue] = {
            "membership_pair_count": total,
            "mapped_membership_pair_count": mapped,
            "unmapped_membership_pair_count": total - mapped,
            "row_coverage_fraction": mapped / total if total else 0.0,
            "membership_symbol_count": len(raw_symbols[venue]),
            "mapped_symbol_count": len(mapped_symbols),
            "unmapped_symbols": sorted(unmapped_symbols),
            "same_venue_canonical_day_alias_collision_count": len(alias_collisions),
            "same_venue_canonical_day_alias_collision_sample": [
                {
                    "canonical_instrument": canonical,
                    "date": date_value,
                    "assignments": [
                        {"symbol": symbol, "contract_multiplier": multiplier}
                        for symbol, multiplier in sorted(assignments)
                    ],
                }
                for (canonical, date_value), assignments in sorted(alias_collisions.items())[:20]
            ],
            "monthly_counts": [
                {
                    "month": month,
                    "membership_pair_count": monthly[month]["membership_pair_count"],
                    "mapped_membership_pair_count": monthly[month]["mapped"],
                    "unmapped_membership_pair_count": monthly[month]["unmapped"],
                }
                for month in sorted(monthly)
            ],
        }

    matched = set.intersection(*(by_venue_canonical_days[venue] for venue in venue_names)) if venue_names else set()
    pairwise: list[dict[str, Any]] = []
    for index, left in enumerate(venue_names):
        for right in venue_names[index + 1 :]:
            left_set = by_venue_canonical_days[left]
            right_set = by_venue_canonical_days[right]
            pairwise.append(
                {
                    "left_venue": left,
                    "right_venue": right,
                    "matched_canonical_instrument_days": len(left_set & right_set),
                    "union_canonical_instrument_days": len(left_set | right_set),
                }
            )
    all_mapped = all(report["unmapped_membership_pair_count"] == 0 for report in venue_reports.values())
    alias_collision_count = sum(
        report["same_venue_canonical_day_alias_collision_count"] for report in venue_reports.values()
    )
    portable_unready_reasons: list[str] = []
    self_asserted_cross_venue_review = all(entry.review_status == "reviewed" for entry in entries)
    if not all_mapped:
        portable_unready_reasons.append("one or more membership rows are unmapped")
    if len(venue_names) < 2:
        portable_unready_reasons.append("fewer than two venues were inventoried")
    if alias_collision_count:
        portable_unready_reasons.append("same-venue aliases map to the same canonical instrument-day")
    if not matched:
        portable_unready_reasons.append("no canonical instrument-day is jointly present across all venues")
    portable_unready_reasons.append(
        "no separate trusted reviewer-bound receipt authenticates product identity, lifecycle, or multiplier semantics"
    )
    trusted_mechanical_venue_local = instrument_map_authority == "mechanically_derived_venue_local" and all(
        entry.review_status == "mechanically_derived_venue_local" for entry in entries
    )
    if not trusted_mechanical_venue_local:
        portable_unready_reasons.append(
            "external map content and review_status are untrusted diagnostics, not canonical venue-local identity"
        )
    portable_ready = False
    venue_local_identity_ready = all_mapped and not alias_collision_count and trusted_mechanical_venue_local
    return {
        "status": (
            "complete"
            if venue_local_identity_ready
            else "diagnostic_untrusted"
            if instrument_map_authority == "external_untrusted"
            else "partial"
        ),
        "map_version": instrument_map_version.strip(),
        "map_authority": instrument_map_authority,
        "map_entry_count": len(entries),
        "map_sha256": _sha256_json([dataclasses.asdict(entry) for entry in entries]),
        "venue_local_identity_ready": venue_local_identity_ready,
        "all_entries_cross_venue_reviewed": False,
        "self_asserted_all_entries_reviewed": self_asserted_cross_venue_review,
        "external_review_status_trusted": False,
        "trusted_reviewer_bound_receipt_present": False,
        "portable_matching_ready": portable_ready,
        "portable_matching_unready_reasons": portable_unready_reasons,
        "raw_ticker_candidates": {
            "count": len(raw_candidates),
            "symbols": raw_candidates,
            "authoritative": False,
            "limitation": "raw equality is diagnostic only; canonical matching uses the versioned map",
        },
        "venues": venue_reports,
        "cross_venue": {
            "all_venue_matched_canonical_instrument_days": len(matched),
            "daily_all_venue_matched_counts": [
                {
                    "date": day,
                    "matched_canonical_instrument_count": sum(date_value == day for _canonical, date_value in matched),
                }
                for day in sorted({date_value for _canonical, date_value in matched})
            ],
            "pairwise": pairwise,
            "contract_multiplier_semantics": (
                "untrusted diagnostic input only; no cross-venue economic-unit equivalence is asserted"
            ),
        },
    }


def _normalise_proposed_schemas(
    proposed_schemas: Mapping[str, Sequence[ProposedField]],
) -> dict[str, Any]:
    allowed = {
        schema_name: tuple(field.name for field in fields) for schema_name, fields in DEFAULT_PROPOSED_SCHEMAS.items()
    }
    if set(proposed_schemas) != set(allowed):
        raise Phase0IntegrityError(
            "proposed schemas are a static Phase-0 allowlist; schema names may not be added or removed"
        )
    output: dict[str, Any] = {}
    for schema_name in sorted(proposed_schemas):
        if not schema_name.strip():
            raise ValueError("proposed schema names must be non-blank")
        fields = list(proposed_schemas[schema_name])
        names = [field.name for field in fields]
        if len(names) != len(set(names)):
            raise Phase0IntegrityError(f"duplicate proposed field in schema {schema_name}")
        if tuple(names) != allowed[schema_name]:
            raise Phase0IntegrityError(
                f"proposed schema {schema_name} does not match the static Phase-0 field allowlist"
            )
        if tuple(fields) != tuple(DEFAULT_PROPOSED_SCHEMAS[schema_name]):
            raise Phase0IntegrityError(
                f"proposed schema {schema_name} metadata does not match the static Phase-0 allowlist"
            )
        output[schema_name] = {
            "status": "proposal_only_child_contract_must_freeze",
            "calculated_in_phase0": False,
            "outcome_bearing_artifact": CHILD_ARTIFACT_SCHEMAS[schema_name].outcome_bearing,
            "outcome_values_calculated_in_phase0": False,
            "fields": [dataclasses.asdict(field) for field in fields],
        }
    return output


def _resource_estimate(
    field_availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    pit_provenance: Mapping[str, Any],
    model: ResourceModel,
) -> dict[str, Any]:
    venue_rows: dict[str, Any] = {}
    total_audit_rows = 0
    total_audit_files = 0
    total_continuous_rows = 0
    total_long_rows = 0
    max_continuous_month_rows = 0
    max_long_month_rows = 0
    for venue in sorted(field_availability):
        datasets = field_availability[venue]
        kline = datasets.get("klines_1h", {})
        continuous_rows = int(kline.get("row_count", 0))
        pit_venue = (pit_provenance.get("venues") or {}).get(venue, {})
        long_rows = int(pit_venue.get("membership_pair_count", 0))
        audit_rows = sum(int(report.get("row_count", 0)) for report in datasets.values())
        audit_files = sum(int(report.get("file_count", 0)) for report in datasets.values())
        total_audit_rows += audit_rows
        total_audit_files += audit_files
        total_continuous_rows += continuous_rows
        total_long_rows += long_rows
        continuous_months = {row["month"]: int(row["row_count"]) for row in kline.get("monthly_counts", [])}
        long_months = {row["month"]: int(row["membership_pair_count"]) for row in pit_venue.get("monthly_counts", [])}
        max_continuous_month_rows = max(max_continuous_month_rows, *(continuous_months.values() or [0]))
        max_long_month_rows = max(max_long_month_rows, *(long_months.values() or [0]))
        months = sorted(set(continuous_months) | set(long_months))
        venue_rows[venue] = {
            "audit_rows": audit_rows,
            "audit_parquet_files": audit_files,
            "continuous_population_row_upper_bound": continuous_rows,
            "long_population_row_upper_bound": long_rows,
            "monthly_counts": [
                {
                    "month": month,
                    "continuous_population_row_upper_bound": continuous_months.get(month, 0),
                    "long_population_row_upper_bound": long_months.get(month, 0),
                }
                for month in months
            ],
        }
    continuous_bytes = total_continuous_rows * model.continuous_output_bytes_per_row
    long_bytes = total_long_rows * model.long_output_bytes_per_row
    peak_continuous = math.ceil(
        max_continuous_month_rows * model.continuous_output_bytes_per_row * model.working_set_multiplier
    )
    peak_long = math.ceil(max_long_month_rows * model.long_output_bytes_per_row * model.working_set_multiplier)
    row_scan_seconds = math.ceil(total_audit_rows / model.audit_rows_per_second)
    file_open_seconds = math.ceil(total_audit_files / model.audit_files_per_second)
    stress_row_seconds = math.ceil(total_audit_rows / model.stress_rows_per_second)
    stress_file_seconds = math.ceil(total_audit_files / model.stress_files_per_second)
    return {
        "estimate_type": "declared_linear_upper_bound_not_benchmark",
        "assumptions": dataclasses.asdict(model),
        "venues": venue_rows,
        "totals": {
            "audit_rows": total_audit_rows,
            "audit_parquet_files": total_audit_files,
            "continuous_population_row_upper_bound": total_continuous_rows,
            "long_population_row_upper_bound": total_long_rows,
            "estimated_audit_scan_bytes": total_audit_rows * model.audit_scan_bytes_per_row,
            "estimated_continuous_output_bytes": continuous_bytes,
            "estimated_long_output_bytes": long_bytes,
            "estimated_total_output_bytes": continuous_bytes + long_bytes,
            "estimated_row_scan_seconds": row_scan_seconds,
            "estimated_parquet_file_overhead_seconds": file_open_seconds,
            "estimated_audit_runtime_seconds": row_scan_seconds + file_open_seconds,
            "stress_audit_runtime_seconds": stress_row_seconds + stress_file_seconds,
            "estimated_peak_continuous_month_memory_bytes": peak_continuous,
            "estimated_peak_long_month_memory_bytes": peak_long,
        },
        "partition_checkpoint_plan": {
            "partition_key": ["venue", "month"],
            "checkpoint_after_each_partition": True,
            "applies_to": "future S02/S03 child runner; Phase-0 inventory itself is read-only",
            "resume_requires": [
                "same frozen child contract and analysis-manifest hashes",
                "same exact numeric root/content receipts for every consumed partition",
                "same config, source, environment, and instrument-map hashes",
                "quiescence check proving completed input partitions did not mutate",
            ],
            "phase0_inventory_sha256_is_sufficient_for_numeric_resume": False,
            "reason": (
                "Phase-0 hashes only identity/provenance projections and deliberately ignores "
                "OHLCV, residual-momentum, and future-label values"
            ),
        },
        "concurrency_plan": {
            "worker_processes": 1,
            "inflight_venue_month_partitions": 1,
            "write_ownership": "one exclusive writer per venue/month partition",
            "merge_order": ["venue", "month"],
            "reason": (
                "single-worker execution is the deterministic safe default until a measured "
                "big-PC benchmark supports a separately frozen parallel plan"
            ),
        },
        "calibration_reference": {
            "status": "single_warm_cache_reference_not_cold_cache_benchmark",
            "machine_scope": "2026-07-10 local workstation",
            "dataset_slice": "Bybit klines_1h, 2026-07-02",
            "parquet_files": 607,
            "identity_rows": 14_568,
            "elapsed_seconds": 0.256,
            "observed_files_per_second": 2_371,
            "observed_rows_per_second": 56_906,
            "planning_rates_apply_material_haircuts": True,
            "cold_cache_or_big_pc_measurement_required_before_feasibility_claim": True,
        },
        "limitations": [
            "row counts are feasibility bounds, not selected-trade counts",
            "runtime and bytes are model assumptions, not measured throughput or parquet compression",
            "runtime includes separate row-scan and per-parquet-file overhead because the roots store one file per symbol-day",
            "the measured reference is warm-cache and cannot establish a cold-cache or big-PC runtime bound",
            "no label tail, funding, execution, or portfolio state is included",
        ],
    }


def build_phase0_artifacts(
    roots: Mapping[str, str | Path],
    *,
    start_date: str,
    end_date_exclusive: str,
    dataset_specs: Sequence[DatasetSpec] = DEFAULT_DATASET_SPECS,
    proposed_schemas: Mapping[str, Sequence[ProposedField]] = DEFAULT_PROPOSED_SCHEMAS,
    instrument_map: Sequence[InstrumentMapEntry | Mapping[str, object]] = (),
    instrument_map_version: str | None = None,
    instrument_map_authority: str = "external_untrusted",
    resource_model: ResourceModel = ResourceModel(),
    sleeve_windows: Sequence[SleeveWindow] = (),
    batch_size: int = 65_536,
) -> dict[str, Any]:
    """Build the complete deterministic, outcome-blind Phase-0 artifact.

    The function is read-only.  It raises on duplicate/null storage keys,
    partition/key disagreement, unreadable parquet, or an invalid instrument
    map.  Missing datasets and fields are retained as explicit readiness
    failures instead of being fabricated.
    """

    start, end = _validated_window(start_date, end_date_exclusive)
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    names = [spec.name for spec in dataset_specs]
    if len(names) != len(set(names)):
        raise Phase0IntegrityError("dataset spec names must be unique")
    membership_specs = [spec for spec in dataset_specs if spec.role == "membership"]
    if len(membership_specs) != 1:
        raise ValueError("exactly one membership DatasetSpec is required")
    normalised_sleeve_windows = sorted(sleeve_windows, key=lambda row: row.sleeve)
    if len({row.sleeve for row in normalised_sleeve_windows}) != len(normalised_sleeve_windows):
        raise Phase0IntegrityError("sleeve-window names must be unique")
    for row in normalised_sleeve_windows:
        causal_start = _parse_date(row.causal_read_start_date, label="causal_read_start_date")
        signal_end = _parse_date(row.signal_end_date_exclusive, label="signal_end_date_exclusive")
        if causal_start < start or signal_end > end:
            raise Phase0IntegrityError(f"sleeve window {row.sleeve!r} is not contained in the inventory read window")

    registered_windows = sorted(REGISTERED_SLEEVE_WINDOWS, key=lambda row: row.sleeve)
    contract_scope_failures: list[str] = []
    if start.isoformat() != min(row.causal_read_start_date for row in REGISTERED_SLEEVE_WINDOWS):
        contract_scope_failures.append("inventory start is not the registered causal read start")
    if end.isoformat() != max(row.signal_end_date_exclusive for row in REGISTERED_SLEEVE_WINDOWS):
        contract_scope_failures.append("inventory end is not the registered signal boundary")
    if normalised_sleeve_windows != registered_windows:
        contract_scope_failures.append("sleeve windows do not equal REGISTERED_SLEEVE_WINDOWS")
    if tuple(dataset_specs) != DEFAULT_DATASET_SPECS:
        contract_scope_failures.append("dataset specs do not equal DEFAULT_DATASET_SPECS")

    normalised_roots = canonicalize_phase0_roots(roots)
    if set(normalised_roots) != {"bybit", "binance"}:
        contract_scope_failures.append("registered Phase-0 requires exactly the bybit and binance venue roots")

    field_availability: dict[str, dict[str, dict[str, Any]]] = {}
    manifests: dict[str, list[_ManifestStorageRow]] = {}
    population_symbol_days: dict[str, set[tuple[str, str]]] = {}
    population_symbol_day_bar_counts: dict[
        str,
        dict[tuple[str, str], int],
    ] = {}
    feature_symbol_days_by_status: dict[
        str,
        dict[str, set[tuple[str, str]]],
    ] = {}
    scanned_columns: dict[str, dict[str, list[str]]] = {}
    for venue in sorted(normalised_roots):
        field_availability[venue] = {}
        scanned_columns[venue] = {}
        manifests[venue] = []
        population_symbol_days[venue] = set()
        population_symbol_day_bar_counts[venue] = {}
        feature_symbol_days_by_status[venue] = {
            "declared_non_provisional": set(),
            "declared_provisional": set(),
            "provisional_status_unknown": set(),
        }
        for spec in dataset_specs:
            scan = _scan_dataset(
                venue,
                normalised_roots[venue],
                spec,
                start=start,
                end=end,
                batch_size=batch_size,
            )
            field_availability[venue][spec.name] = scan.report
            scanned_columns[venue][spec.name] = scan.report["value_columns_read"]
            if spec.role == "membership":
                manifests[venue] = scan.manifest_rows
            if spec.name == "klines_1h":
                population_symbol_days[venue] = scan.population_symbol_days
                population_symbol_day_bar_counts[venue] = scan.population_symbol_day_bar_counts
            if spec.role == "feature":
                for status, symbol_days in scan.feature_symbol_days_by_status.items():
                    feature_symbol_days_by_status[venue][status].update(symbol_days)

    pit_provenance, collapsed = _build_pit_provenance(
        manifests,
        inventory_start_date=start.isoformat(),
    )
    manifest_kline_coverage = _build_manifest_kline_coverage(
        collapsed,
        population_symbol_days,
        population_symbol_day_bar_counts,
    )
    rmom_population_coverage = _build_rmom_population_coverage(
        collapsed,
        population_symbol_days,
        feature_symbol_days_by_status,
    )
    root_lineage = _build_root_lineage(
        normalised_roots,
        field_availability,
        start_date=start.isoformat(),
        signal_end_date_exclusive=end.isoformat(),
    )
    map_coverage = _build_instrument_map_coverage(
        collapsed,
        instrument_map=instrument_map,
        instrument_map_version=instrument_map_version,
        instrument_map_authority=instrument_map_authority,
    )
    schemas = _normalise_proposed_schemas(proposed_schemas)
    schema_registry = child_schema_registry_payload()
    schema_registry["artifact_sha256"] = child_schema_registry_sha256()
    resources = _resource_estimate(field_availability, pit_provenance, resource_model)
    venue_readiness: dict[str, Any] = {}
    for venue in sorted(field_availability):
        datasets_ready = all(report["ready"] for report in field_availability[venue].values() if report["required"])
        coverage_ready = bool((manifest_kline_coverage.get("venues") or {}).get(venue, {}).get("status") == "complete")
        venue_pit = (pit_provenance.get("venues") or {}).get(venue, {})
        status_counts = {
            row.get("status"): int(row.get("membership_pair_count", 0))
            for row in venue_pit.get("observation_status_counts", [])
        }
        unknown_provenance_count = status_counts.get("unknown", 0)
        contradictory_provenance_count = int(venue_pit.get("contradictory_provenance_membership_pair_count", 0))
        provenance_ready = unknown_provenance_count == 0 and contradictory_provenance_count == 0
        venue_lineage = (root_lineage.get("venues") or {}).get(venue, {})
        source_label_compatibility_ready = bool(venue_lineage.get("source_label_compatibility_ready"))
        ready = datasets_ready and coverage_ready and provenance_ready and source_label_compatibility_ready
        failure_reasons: list[str] = []
        if not datasets_ready:
            failure_reasons.append("one or more required datasets are not ready")
        if not coverage_ready:
            failure_reasons.append("manifest-to-kline coverage is incomplete")
        if unknown_provenance_count:
            failure_reasons.append(f"{unknown_provenance_count} membership pairs have unknown observation provenance")
        if contradictory_provenance_count:
            failure_reasons.append(f"{contradictory_provenance_count} membership pairs have contradictory provenance")
        if not source_label_compatibility_ready:
            failure_reasons.append(
                "persisted source labels are absent or incompatible with the registered venue; labels remain "
                "necessary but not sufficient lineage evidence"
            )
        venue_readiness[venue] = {
            "status": "READY" if ready else "NOT_READY",
            "required_datasets_ready": datasets_ready,
            "manifest_kline_coverage_ready": coverage_ready,
            "pit_provenance_ready": provenance_ready,
            "source_label_compatibility_ready": source_label_compatibility_ready,
            "upstream_root_authenticity_proven": False,
            "canonical_s01_root_lineage_ready": False,
            "unknown_observation_provenance_membership_pair_count": unknown_provenance_count,
            "contradictory_provenance_membership_pair_count": contradictory_provenance_count,
            "failure_reasons": failure_reasons,
        }
    ready_venue_count = sum(row["status"] == "READY" for row in venue_readiness.values())
    if ready_venue_count == len(venue_readiness):
        data_status = "READY"
    elif ready_venue_count:
        data_status = "PARTIAL"
    else:
        data_status = "NOT_READY"
    contract_scope_ready = not contract_scope_failures
    overall_status = "NOT_READY" if data_status == "READY" and not contract_scope_ready else data_status
    required_ready = overall_status == "READY"

    sleeve_window_payload = [
        {
            **dataclasses.asdict(row),
            "causal_warmup_days": (
                _parse_date(row.signal_start_date, label="signal_start_date")
                - _parse_date(row.causal_read_start_date, label="causal_read_start_date")
            ).days,
        }
        for row in normalised_sleeve_windows
    ]
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_phase0_outcome_blind_inventory",
        "dataset_specs": {
            "specs": [dataclasses.asdict(spec) for spec in dataset_specs],
            "sha256": _sha256_json([dataclasses.asdict(spec) for spec in dataset_specs]),
        },
        "window": {
            "inventory_read_start_date": start.isoformat(),
            "inventory_read_end_date_exclusive": end.isoformat(),
            "sleeve_windows": sleeve_window_payload,
            "sleeve_windows_status": "declared" if sleeve_window_payload else "not_supplied",
        },
        "roots": {venue: str(path.resolve(strict=False)) for venue, path in sorted(normalised_roots.items())},
        "readiness": {
            "status": overall_status,
            "status_semantics": (
                "outcome-blind structural/key/provenance-label readiness only; not source authentication, "
                "canonical root lineage, S01 readiness, or outcome authorization"
            ),
            "data_status": data_status,
            "required_datasets_ready": required_ready,
            "registered_contract_scope_ready": contract_scope_ready,
            "registered_contract_scope_failures": contract_scope_failures,
            "venues": venue_readiness,
            "portable_cross_venue_matching_ready": map_coverage["portable_matching_ready"],
            "registered_root_lineage_ready": root_lineage["canonical_s01_root_lineage_ready"],
            "root_lineage_blockers": root_lineage["limitations"],
            "canonical_s01_ready": False,
            "source_authenticity_proven": False,
            "full_process_environment_identity_proven": False,
            "sleeve_windows_declared": bool(sleeve_window_payload),
            "outcome_run_authorized": False,
        },
        "field_availability": field_availability,
        "pit_provenance": pit_provenance,
        "manifest_kline_coverage": manifest_kline_coverage,
        "rmom_population_coverage": rmom_population_coverage,
        "root_lineage": root_lineage,
        "resource_estimate": resources,
        "proposed_schemas": schemas,
        "child_schema_registry": schema_registry,
        "instrument_map_coverage": map_coverage,
        "outcome_blind_audit": {
            "phase": 0,
            "outcome_values_read": False,
            "ohlcv_values_read": False,
            "residual_momentum_values_read": False,
            "returns_calculated": False,
            "mfe_calculated": False,
            "mae_calculated": False,
            "pnl_calculated": False,
            "ranks_calculated": False,
            "labels_calculated": False,
            "wall_clock_fields_emitted": False,
            "value_columns_read": scanned_columns,
            "parquet_footer_properties_used": ["num_rows", "schema"],
            "permitted_outputs": [
                "field availability",
                "identity/provenance counts",
                "RMOM identity/provisional-flag population coverage",
                "resource feasibility estimates",
                "proposed schemas",
                "schema-to-builder mismatch ledger",
                "instrument-map coverage",
            ],
            "non_authorizations": [
                "no strategy selection",
                "no gate ranking",
                "no threshold choice",
                "no outcome run",
                "no deployment",
                "no real-money enablement",
            ],
        },
    }
    artifact["artifact_sha256"] = _sha256_json(artifact)
    return artifact


__all__ = [
    "DEFAULT_DATASET_SPECS",
    "DEFAULT_PROPOSED_SCHEMAS",
    "INSTRUMENT_MAP_AUTHORITIES",
    "REGISTERED_SLEEVE_WINDOWS",
    "DatasetSpec",
    "InstrumentMapEntry",
    "Phase0IntegrityError",
    "ProposedField",
    "ResourceModel",
    "SleeveWindow",
    "build_phase0_artifacts",
    "canonicalize_phase0_roots",
    "inspect_dataset",
]
