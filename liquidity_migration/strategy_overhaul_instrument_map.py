"""Deterministic venue-local identity maps for A0 artifacts.

The A0 hypotheses are now venue-scoped.  They need stable identity within each
venue, but they do not need aliases across venues to be asserted equivalent.
This module builds a conservative map that keeps every venue symbol distinct.
It never merges renames, normalizes economic contract units, or authorizes a
cross-venue portability claim.  A separately reviewed cross-venue map can replace
it when portability is the estimand.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import polars as pl

from .strategy_overhaul_identity_adapter import SUPPORTED_VENUES
from .strategy_overhaul_phase0 import (
    DEFAULT_DATASET_SPECS,
    InstrumentMapEntry,
    _discover_files,
    _scan_dataset,
    _validated_window,
    canonicalize_phase0_roots,
)


VENUE_LOCAL_MANIFEST_SCHEMA = MappingProxyType(
    {
        "venue": pl.String,
        "symbol": pl.String,
        "manifest_date": pl.Date,
    }
)
VENUE_LOCAL_REVIEW_STATUS = "mechanically_derived_venue_local"
VENUE_LOCAL_MAPPING_SOURCE = "full_pit_manifest_symbol_identity_no_alias_merge_no_economic_unit_normalization"


class VenueLocalInstrumentMapError(ValueError):
    """The finite manifest projection cannot produce a venue-local map."""


@dataclass(frozen=True, slots=True)
class VenueLocalInstrumentMap:
    version: str
    entries: tuple[InstrumentMapEntry, ...]
    receipt: MappingProxyType[str, Any]


@dataclass(frozen=True, slots=True)
class VenueLocalManifestProjection:
    """Exact outcome-blind manifest symbol/day input to a venue-local map."""

    rows: pl.DataFrame
    receipt: MappingProxyType[str, Any]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _observe_manifest_files(root: Path, *, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    base = root / "archive_trade_manifest"
    if base.is_symlink():
        raise VenueLocalInstrumentMapError(f"manifest dataset root must not be a symlink: {base}")
    rows: list[dict[str, Any]] = []
    for path in _discover_files(base, start=start, end=end):
        try:
            observed = path.lstat()
            relative = path.relative_to(root).as_posix()
            relative_to_base = path.relative_to(base)
        except (OSError, ValueError) as exc:
            raise VenueLocalInstrumentMapError(f"cannot observe manifest source file {path}: {exc}") from exc
        for depth in range(1, len(relative_to_base.parts)):
            parent = base.joinpath(*relative_to_base.parts[:depth])
            try:
                parent_mode = parent.lstat().st_mode
            except OSError as exc:
                raise VenueLocalInstrumentMapError(f"cannot observe manifest source parent {parent}: {exc}") from exc
            if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
                raise VenueLocalInstrumentMapError(f"manifest source parents must be non-symlink directories: {parent}")
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            raise VenueLocalInstrumentMapError(f"manifest source must be a regular non-symlink file: {path}")
        rows.append(
            {
                "relative_path": relative,
                "bytes": observed.st_size,
                "mode": stat.S_IMODE(observed.st_mode),
                "mtime_ns": observed.st_mtime_ns,
                "inode": observed.st_ino,
            }
        )
    return rows


def load_venue_local_manifest_projection(
    roots: Mapping[str, str | Path],
    *,
    start_date: str,
    end_date_exclusive: str,
    batch_size: int = 65_536,
) -> VenueLocalManifestProjection:
    """Validate manifest storage, then collapse it to venue/symbol/day only."""

    start, end = _validated_window(start_date, end_date_exclusive)
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    try:
        normalized_roots = canonicalize_phase0_roots(roots, require_registered_venues=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise VenueLocalInstrumentMapError(str(exc)) from exc
    if set(normalized_roots) != set(SUPPORTED_VENUES):  # pragma: no cover - shared canonicalizer invariant
        raise VenueLocalInstrumentMapError(f"requires exactly {sorted(SUPPORTED_VENUES)} roots")
    membership_specs = [spec for spec in DEFAULT_DATASET_SPECS if spec.role == "membership"]
    if len(membership_specs) != 1:
        raise VenueLocalInstrumentMapError("Phase-0 must define exactly one membership dataset")
    membership_spec = membership_specs[0]

    all_pairs: set[tuple[str, str, str]] = set()
    venues: dict[str, Any] = {}
    storage_validation_columns: set[str] = set()
    for venue in sorted(normalized_roots):
        root = normalized_roots[venue]
        files_before = _observe_manifest_files(root, start=start, end=end)
        try:
            scan = _scan_dataset(
                venue,
                root,
                membership_spec,
                start=start,
                end=end,
                batch_size=batch_size,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise VenueLocalInstrumentMapError(
                f"failed strict {venue} manifest storage/projection scan: {exc}"
            ) from exc
        files_after = _observe_manifest_files(root, start=start, end=end)
        if files_after != files_before:
            raise VenueLocalInstrumentMapError(f"{venue} manifest source files changed during projection scan")
        if len(files_before) != scan.report["file_count"]:
            raise VenueLocalInstrumentMapError(f"{venue} manifest file inventory disagrees with strict scan")
        venue_pairs = {(row.symbol, row.date) for row in scan.manifest_rows}
        ordered_venue_rows = [
            {"venue": venue, "symbol": symbol, "manifest_date": day} for symbol, day in sorted(venue_pairs)
        ]
        partition_coverage = scan.report.get("partition_coverage") or {
            "present_date_count": 0,
            "missing_date_count": (end - start).days,
            "missing_date_sample": [
                (start + dt.timedelta(days=offset)).isoformat() for offset in range(min((end - start).days, 20))
            ],
        }
        venues[venue] = {
            "root": str(root.resolve()),
            "dataset": membership_spec.relative_path,
            "file_count": scan.report["file_count"],
            "storage_row_count_in_window": scan.report["row_count"],
            "storage_key_provenance_projection_sha256": scan.report["key_provenance_projection_sha256"],
            "storage_key_audit": scan.report["key_audit"],
            "source_projection_row_count": len(ordered_venue_rows),
            "source_projection_sha256": _sha256_json(ordered_venue_rows),
            "storage_validation_columns_read": scan.report["value_columns_read"],
            "logical_projection_columns": ["symbol", "date"],
            "present_partition_date_count": partition_coverage["present_date_count"],
            "missing_partition_date_count": partition_coverage["missing_date_count"],
            "missing_partition_date_sample": partition_coverage["missing_date_sample"],
            "registered_window_complete": bool(scan.report["ready"]),
            "strict_storage_validation_failures": scan.report["failure_reasons"],
        }
        storage_validation_columns.update(scan.report["value_columns_read"])
        all_pairs.update((venue, symbol, day) for symbol, day in venue_pairs)

    ordered_rows = [
        {"venue": venue, "symbol": symbol, "manifest_date": day} for venue, symbol, day in sorted(all_pairs)
    ]
    frame = pl.DataFrame(
        [
            {
                "venue": row["venue"],
                "symbol": row["symbol"],
                "manifest_date": dt.date.fromisoformat(row["manifest_date"]),
            }
            for row in ordered_rows
        ],
        schema=dict(VENUE_LOCAL_MANIFEST_SCHEMA),
    )
    receipt_payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_venue_local_manifest_projection",
        "source_kind": "auto_derived_archive_trade_manifest_symbol_date_projection",
        "window": {
            "start_date": start.isoformat(),
            "end_date_exclusive": end.isoformat(),
        },
        "source_projection_row_count": len(ordered_rows),
        "source_projection_sha256": _sha256_json(ordered_rows),
        "venues": venues,
        "logical_projection_columns": ["venue", "symbol", "manifest_date"],
        "storage_validation_columns_read": sorted(storage_validation_columns),
        "registered_window_complete": all(row["registered_window_complete"] for row in venues.values()),
        "ohlcv_values_read": False,
        "rmom_values_read": False,
        "outcome_values_read": False,
    }
    projection_identity_payload = {
        "schema_version": receipt_payload["schema_version"],
        "artifact_type": receipt_payload["artifact_type"],
        "window": receipt_payload["window"],
        "logical_projection_columns": receipt_payload["logical_projection_columns"],
        "source_projection_row_count": receipt_payload["source_projection_row_count"],
        "source_projection_sha256": receipt_payload["source_projection_sha256"],
        "registered_window_complete": receipt_payload["registered_window_complete"],
        "venues": {
            venue: {
                key: row[key]
                for key in (
                    "storage_row_count_in_window",
                    "storage_key_provenance_projection_sha256",
                    "source_projection_row_count",
                    "source_projection_sha256",
                    "missing_partition_date_count",
                    "registered_window_complete",
                )
            }
            for venue, row in venues.items()
        },
    }
    receipt_payload["source_projection_identity_sha256"] = _sha256_json(projection_identity_payload)
    receipt_payload["artifact_sha256"] = _sha256_json(receipt_payload)
    return VenueLocalManifestProjection(rows=frame, receipt=MappingProxyType(receipt_payload))


def derive_venue_local_instrument_map_from_roots(
    roots: Mapping[str, str | Path],
    *,
    start_date: str,
    end_date_exclusive: str,
    batch_size: int = 65_536,
) -> VenueLocalInstrumentMap:
    """Derive a non-portable map and bind its exact manifest projection source."""

    projection = load_venue_local_manifest_projection(
        roots,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        batch_size=batch_size,
    )
    base = build_venue_local_instrument_map(projection.rows)
    projection_identity = str(projection.receipt["source_projection_identity_sha256"])
    version = f"{base.version}-source-{projection_identity[:16]}"
    receipt_payload = dict(base.receipt)
    receipt_payload.pop("artifact_sha256", None)
    receipt_payload.update(
        {
            "derivation_source_kind": "auto_derived_venue_local_manifest_projection",
            "version": version,
            "source_projection": dict(projection.receipt),
            "source_projection_sha256": projection.receipt["source_projection_sha256"],
            "source_projection_row_count": projection.receipt["source_projection_row_count"],
            "source_window": projection.receipt["window"],
            "source_registered_window_complete": projection.receipt["registered_window_complete"],
            "source_projection_identity_sha256": projection_identity,
            "venue_local_identity_ready": bool(
                base.receipt["venue_local_identity_ready"] and projection.receipt["registered_window_complete"]
            ),
            "cross_venue_portability_ready": False,
        }
    )
    receipt_payload["artifact_sha256"] = _sha256_json(receipt_payload)
    return VenueLocalInstrumentMap(
        version=version,
        entries=base.entries,
        receipt=MappingProxyType(receipt_payload),
    )


def build_venue_local_instrument_map(
    manifest_pairs: pl.DataFrame,
) -> VenueLocalInstrumentMap:
    """Map each normalized venue symbol to a distinct, non-portable identity."""

    expected = tuple(VENUE_LOCAL_MANIFEST_SCHEMA)
    missing = sorted(set(expected) - set(manifest_pairs.columns))
    unknown = sorted(set(manifest_pairs.columns) - set(expected))
    if missing or unknown or len(manifest_pairs.columns) != len(expected):
        raise VenueLocalInstrumentMapError(f"manifest_pairs projection mismatch; missing={missing}, unknown={unknown}")
    dtype_mismatch = {
        name: {
            "expected": str(dtype),
            "actual": str(manifest_pairs.schema[name]),
        }
        for name, dtype in VENUE_LOCAL_MANIFEST_SCHEMA.items()
        if manifest_pairs.schema[name] != dtype
    }
    if dtype_mismatch:
        raise VenueLocalInstrumentMapError(f"manifest_pairs has invalid dtypes: {dtype_mismatch}")
    invalid = manifest_pairs.filter(
        pl.col("venue").is_null()
        | ~pl.col("venue").is_in(sorted(SUPPORTED_VENUES))
        | (pl.col("venue") != pl.col("venue").str.strip_chars().str.to_lowercase())
        | pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | (pl.col("symbol") != pl.col("symbol").str.strip_chars().str.to_uppercase())
        | ~pl.col("symbol").str.ends_with("USDT")
        | (pl.col("symbol").str.len_chars() <= 4)
        | pl.col("manifest_date").is_null()
    )
    if not invalid.is_empty():
        raise VenueLocalInstrumentMapError("manifest_pairs contains invalid venue/USDT-symbol/date identity")
    duplicates = manifest_pairs.group_by(["venue", "symbol", "manifest_date"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise VenueLocalInstrumentMapError("manifest_pairs contains duplicate (venue,symbol,manifest_date) keys")

    lifecycles = (
        manifest_pairs.group_by(["venue", "symbol"])
        .agg(pl.col("manifest_date").min().alias("valid_from_date"))
        .sort(["venue", "symbol"])
    )
    entries = tuple(
        InstrumentMapEntry(
            canonical_instrument=(f"{row['venue'].upper()}::{row['symbol']}::USDT_LINEAR_PERPETUAL"),
            venue=str(row["venue"]),
            symbol=str(row["symbol"]),
            valid_from_date=row["valid_from_date"].isoformat(),
            valid_to_date_exclusive=None,
            base_asset=str(row["symbol"])[:-4],
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_type="linear_perpetual",
            contract_multiplier=1.0,
            mapping_source=VENUE_LOCAL_MAPPING_SOURCE,
            review_status=VENUE_LOCAL_REVIEW_STATUS,
        )
        for row in lifecycles.iter_rows(named=True)
    )
    entry_payload = [dataclasses.asdict(entry) for entry in entries]
    map_sha256 = hashlib.sha256(_canonical_json(entry_payload)).hexdigest()
    version = f"strategy-overhaul-venue-local-v1-{map_sha256[:16]}"
    receipt_payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_venue_local_instrument_map",
        "version": version,
        "map_sha256": map_sha256,
        "entry_count": len(entries),
        "venues": sorted({entry.venue for entry in entries}),
        "review_status": VENUE_LOCAL_REVIEW_STATUS,
        "venue_local_identity_ready": bool(entries),
        "cross_venue_portability_ready": False,
        "alias_merges_performed": False,
        "economic_unit_normalization_performed": False,
        "limitation": (
            "canonical IDs are venue-qualified; ticker equality across venues, "
            "renames, migrations, and contract-unit equivalence are not asserted"
        ),
        "outcome_values_read": False,
    }
    receipt_payload["artifact_sha256"] = hashlib.sha256(_canonical_json(receipt_payload)).hexdigest()
    return VenueLocalInstrumentMap(
        version=version,
        entries=entries,
        receipt=MappingProxyType(receipt_payload),
    )


__all__ = [
    "VENUE_LOCAL_MANIFEST_SCHEMA",
    "VENUE_LOCAL_MAPPING_SOURCE",
    "VENUE_LOCAL_REVIEW_STATUS",
    "VenueLocalManifestProjection",
    "VenueLocalInstrumentMap",
    "VenueLocalInstrumentMapError",
    "build_venue_local_instrument_map",
    "derive_venue_local_instrument_map_from_roots",
    "load_venue_local_manifest_projection",
]
