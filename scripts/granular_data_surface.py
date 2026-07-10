#!/usr/bin/env python3
r"""Audit and explicitly backfill the granular/alternative-data research surface.

The default mode is read-only: it performs a bounded, point-in-time-manifest-
anchored coverage audit and prints a JSON receipt. Network downloads require
``--execute`` plus an explicit dataset list, date window, symbol authority, and
checkpoint receipt path.

Examples
--------
Read-only seven-day tail audit (default)::

    .venv/bin/python scripts/granular_data_surface.py

Read-only registered-window audit::

    .venv/bin/python scripts/granular_data_surface.py \
      --start 2023-04-01 --end 2026-07-10 \
      --output research/granular_adverse_risk/data_readiness.json

Explicit, resume-safe ancillary refresh for every PIT member in the window::

    .venv/bin/python scripts/granular_data_surface.py --execute \
      --venue both --datasets funding,open_interest,premium_index_1h \
      --start 2026-07-01 --end 2026-07-10 --all-pit-symbols \
      --output research/granular_adverse_risk/download_receipt.json

This script is an operator surface, not a strategy verdict. A coverage receipt
cannot be labelled alpha/candidate/promotion evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402


DEFAULT_LOGICAL_DATASETS = (
    "klines_5m",
    "tick_ohlc_1m",
    "funding",
    "open_interest",
    "premium_index_1h",
    "taker_flow",
    "metrics_5m",
    "bookdepth_1h",
)
EXECUTABLE_DATASETS = frozenset(
    {
        "klines_5m",
        "funding",
        "open_interest",
        "premium_index_1h",
        "taker_flow",
        "metrics_5m",
        "bookdepth_1h",
    }
)
DEFAULT_AUDIT_DAYS = 7
DEFAULT_MAX_SYMBOL_DAYS = 250_000


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    logical_name: str
    storage_name: str
    layout: str
    min_rows_per_symbol_day: int
    frequency: str
    source: str
    evidence_use: str
    executable: bool = True
    exact_daily_grid: bool = False
    expected_unique_timestamps: int | None = None
    interval_ms: int | None = None
    observation_discriminator: str | None = None
    required_columns: tuple[str, ...] = ()
    finite_columns: tuple[str, ...] = ()
    positive_columns: tuple[str, ...] = ()
    nonnegative_columns: tuple[str, ...] = ()
    bounded_columns: tuple[tuple[str, float, float], ...] = ()
    nonempty_columns: tuple[str, ...] = ()
    constant_columns: tuple[str, ...] = ()
    observations_per_timestamp: int | None = None
    ohlc: bool = False


BYBIT_SPECS = (
    DatasetSpec(
        "klines_5m",
        "klines_5m",
        "partitioned_or_flat",
        288,
        "5m",
        "Bybit v5 historical kline REST, PIT-manifest gated",
        "historical price-path input after exact 288-row/day gate",
        exact_daily_grid=True,
        expected_unique_timestamps=288,
        interval_ms=5 * 60_000,
        required_columns=("open", "high", "low", "close"),
        finite_columns=("open", "high", "low", "close"),
        positive_columns=("open", "high", "low", "close"),
        ohlc=True,
    ),
    DatasetSpec(
        "tick_ohlc_1m",
        "tick_ohlc_1m",
        "partitioned_or_flat",
        1_440,
        "1m",
        "Bybit public-trade archive derived",
        "historical path refinement only where exact coverage passes",
        executable=False,
        exact_daily_grid=True,
        expected_unique_timestamps=1_440,
        interval_ms=60_000,
        required_columns=("open", "high", "low", "close"),
        finite_columns=("open", "high", "low", "close"),
        positive_columns=("open", "high", "low", "close"),
        ohlc=True,
    ),
    DatasetSpec(
        "funding",
        "funding",
        "partitioned_or_flat",
        1,
        "settlement",
        "Bybit v5 funding history",
        "cost input; partition presence is not a settlement-gap proof",
        required_columns=("funding_rate",),
        finite_columns=("funding_rate",),
    ),
    DatasetSpec(
        "open_interest",
        "open_interest",
        "partitioned_or_flat",
        20,
        "1h_or_finer",
        "Bybit v5 open-interest history",
        "feature input only inside the exact audited window",
        required_columns=("open_interest", "open_interest_value", "open_interest_interval"),
        finite_columns=("open_interest", "open_interest_value"),
        nonnegative_columns=("open_interest", "open_interest_value"),
        nonempty_columns=("open_interest_interval",),
        constant_columns=("open_interest_interval",),
    ),
    DatasetSpec(
        "premium_index_1h",
        "premium_index_1h",
        "partitioned_or_flat",
        20,
        "1h",
        "Bybit v5 premium-index klines",
        "feature input only inside the exact audited window",
        required_columns=("open", "high", "low", "close"),
        finite_columns=("open", "high", "low", "close"),
        ohlc=True,
    ),
    DatasetSpec(
        "taker_flow",
        "taker_flow_5m",
        "partitioned_or_flat",
        288,
        "5m",
        "Bybit public-trade archive derived",
        "feature input only where exact coverage passes",
        executable=False,
        exact_daily_grid=True,
        expected_unique_timestamps=288,
        interval_ms=5 * 60_000,
        required_columns=("taker_buy_quote", "taker_sell_quote", "n_buy", "n_sell"),
        finite_columns=("taker_buy_quote", "taker_sell_quote", "n_buy", "n_sell"),
        nonnegative_columns=("taker_buy_quote", "taker_sell_quote", "n_buy", "n_sell"),
    ),
)


BINANCE_SPECS = (
    DatasetSpec(
        "klines_5m",
        "klines_5m",
        "partitioned_or_flat",
        288,
        "5m",
        "Binance Vision USD-M kline archive",
        "historical price-path input after exact 288-row/day gate",
        exact_daily_grid=True,
        expected_unique_timestamps=288,
        interval_ms=5 * 60_000,
        required_columns=("open", "high", "low", "close"),
        finite_columns=("open", "high", "low", "close"),
        positive_columns=("open", "high", "low", "close"),
        ohlc=True,
    ),
    DatasetSpec(
        "funding",
        "binance_usdm_funding",
        "partitioned_or_flat",
        1,
        "settlement",
        "Binance Vision/FAPI USD-M funding",
        "cost input; partition presence is not a settlement-gap proof",
        required_columns=("funding_rate",),
        finite_columns=("funding_rate",),
    ),
    DatasetSpec(
        "open_interest",
        "binance_usdm_open_interest",
        "partitioned_or_flat",
        20,
        "1h_or_finer",
        "Binance FAPI recent-history proxy",
        "recent-window feature input only; not long-history proof",
        required_columns=("open_interest", "open_interest_value", "open_interest_interval"),
        finite_columns=("open_interest", "open_interest_value"),
        nonnegative_columns=("open_interest", "open_interest_value"),
        nonempty_columns=("open_interest_interval",),
        constant_columns=("open_interest_interval",),
    ),
    DatasetSpec(
        "premium_index_1h",
        "binance_usdm_premium_index_1h",
        "partitioned_or_flat",
        20,
        "1h",
        "Binance FAPI premium-index klines",
        "feature input only inside the exact audited window",
        required_columns=("open", "high", "low", "close"),
        finite_columns=("open", "high", "low", "close"),
        ohlc=True,
    ),
    DatasetSpec(
        "taker_flow",
        "binance_usdm_taker_flow_1h",
        "partitioned_or_flat",
        20,
        "1h",
        "Binance FAPI recent-history taker-flow proxy",
        "recent-window context only; not long-history proof",
        required_columns=(
            "buy_volume_base",
            "sell_volume_base",
            "signed_volume_base",
            "taker_imbalance",
            "buy_sell_ratio",
            "flow_interval",
        ),
        finite_columns=(
            "buy_volume_base",
            "sell_volume_base",
            "signed_volume_base",
            "taker_imbalance",
            "buy_sell_ratio",
        ),
        nonnegative_columns=("buy_volume_base", "sell_volume_base", "buy_sell_ratio"),
        bounded_columns=(("taker_imbalance", -1.0, 1.0),),
        nonempty_columns=("flow_interval",),
        constant_columns=("flow_interval",),
    ),
    DatasetSpec(
        "metrics_5m",
        "binance_usdm_metrics_5m",
        "flat_symbol",
        288,
        "5m",
        "Binance Vision USD-M metrics archive",
        "historical OI/positioning/flow input; cross-venue claims still required",
        exact_daily_grid=True,
        expected_unique_timestamps=288,
        interval_ms=5 * 60_000,
        required_columns=(
            "sum_open_interest",
            "sum_open_interest_value",
            "sum_taker_long_short_vol_ratio",
        ),
        finite_columns=(
            "sum_open_interest",
            "sum_open_interest_value",
            "sum_taker_long_short_vol_ratio",
        ),
        nonnegative_columns=(
            "sum_open_interest",
            "sum_open_interest_value",
            "sum_taker_long_short_vol_ratio",
        ),
    ),
    DatasetSpec(
        "bookdepth_1h",
        "binance_usdm_bookdepth_1h",
        "flat_symbol",
        240,
        "1h_x_10_bands",
        "Binance Vision USD-M bookDepth archive",
        "capacity/slippage calibration; one-venue signal evidence is insufficient",
        exact_daily_grid=True,
        expected_unique_timestamps=24,
        interval_ms=60 * 60_000,
        observation_discriminator="percentage",
        required_columns=(
            "depth_mean",
            "notional_mean",
            "depth_last",
            "notional_last",
            "n_snaps",
            "percentage",
        ),
        finite_columns=("depth_mean", "notional_mean", "depth_last", "notional_last", "n_snaps"),
        nonnegative_columns=("depth_mean", "notional_mean", "depth_last", "notional_last"),
        positive_columns=("n_snaps",),
        nonempty_columns=("percentage",),
        observations_per_timestamp=10,
    ),
)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def _day_range(start: date, end_exclusive: date) -> list[str]:
    days: list[str] = []
    cursor = start
    while cursor < end_exclusive:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def _manifest_days(root: Path) -> list[str]:
    manifest = root / "archive_trade_manifest"
    if not manifest.is_dir():
        return []
    days: list[str] = []
    for item in manifest.iterdir():
        if not item.is_dir() or not item.name.startswith("date="):
            continue
        day = item.name.split("=", 1)[1]
        try:
            _parse_day(day)
        except ValueError:
            continue
        if any(path.is_file() and path.stat().st_size > 0 for path in item.glob("*.parquet")):
            days.append(day)
    return sorted(set(days))


def _resolve_window(root: Path, start: str | None, end: str | None) -> tuple[str, str]:
    days = _manifest_days(root)
    if not days:
        raise RuntimeError(f"no readable archive_trade_manifest/date=* coverage under {root}")
    end_day = _parse_day(end) if end else _parse_day(days[-1]) + timedelta(days=1)
    start_day = _parse_day(start) if start else end_day - timedelta(days=DEFAULT_AUDIT_DAYS)
    if start_day >= end_day:
        raise ValueError(f"start must be before end: {start_day} >= {end_day}")
    return start_day.isoformat(), end_day.isoformat()


def load_manifest_pairs(
    root: Path,
    *,
    start: str,
    end: str,
    symbols: Sequence[str] = (),
    max_symbol_days: int = DEFAULT_MAX_SYMBOL_DAYS,
) -> pl.DataFrame:
    """Load a complete, readable, path/content-identified PIT denominator.

    Every requested UTC day must have at least one readable, non-empty parquet
    fragment. Stored ``date`` values must equal the ``date=YYYY-MM-DD`` path
    identity. Missing/unreadable days are fatal rather than silently disappearing
    from the denominator.
    """
    manifest = root / "archive_trade_manifest"
    frames: list[pl.DataFrame] = []
    for day in _day_range(_parse_day(start), _parse_day(end)):
        day_root = manifest / f"date={day}"
        if not day_root.is_dir():
            raise RuntimeError(f"missing PIT manifest date partition: {day_root}")
        files = sorted(path for path in day_root.glob("*.parquet") if path.is_file())
        if not files:
            raise RuntimeError(f"empty PIT manifest date partition: {day_root}")
        zero_byte = [str(path) for path in files if path.stat().st_size <= 0]
        if zero_byte:
            raise RuntimeError(f"unreadable zero-byte PIT manifest fragment(s) for {day}: {zero_byte}")
        try:
            lf = pl.scan_parquet(
                [str(path) for path in files],
                hive_partitioning=False,
                include_file_paths="_fragment_path",
                missing_columns="raise",
                extra_columns="raise",
            )
            columns = set(lf.collect_schema().names())
            required = {"symbol", "date"}
            if missing := sorted(required - columns):
                raise RuntimeError(f"missing required column(s) {missing}")
            frame = lf.select(
                pl.col("symbol").cast(pl.String),
                pl.col("date").cast(pl.String),
                pl.col("_fragment_path").cast(pl.String),
            ).collect()
        except Exception as exc:
            raise RuntimeError(f"unreadable PIT manifest partition {day_root}: {exc}") from exc
        if frame.is_empty():
            raise RuntimeError(f"PIT manifest partition has zero rows: {day_root}")
        invalid = frame.filter(
            pl.col("symbol").is_null()
            | (pl.col("symbol").str.strip_chars() == "")
            | pl.col("date").is_null()
            | (pl.col("date") != day)
        )
        if invalid.height:
            raise RuntimeError(f"PIT manifest path/content identity mismatch for {day}: {invalid.head(10).to_dicts()}")
        duplicates = frame.group_by(["symbol", "date"]).len().filter(pl.col("len") > 1)
        if duplicates.height:
            raise RuntimeError(f"duplicate PIT manifest symbol-day rows for {day}: {duplicates.head(10).to_dicts()}")
        frames.append(frame.select(["symbol", "date"]))
    if not frames:
        raise RuntimeError(f"no PIT manifest days requested in [{start}, {end}) under {root}")
    pairs = pl.concat(frames, how="vertical").sort(["symbol", "date"])
    wanted = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol))
    if wanted:
        pairs = pairs.filter(pl.col("symbol").is_in(list(wanted)))
    if pairs.height > max_symbol_days:
        raise RuntimeError(
            f"audit would inspect {pairs.height:,} PIT symbol-days, above --max-symbol-days "
            f"{max_symbol_days:,}; narrow the window/symbols or explicitly raise the cap"
        )
    return pairs


def _layout(path: Path) -> str:
    if not path.exists():
        return "missing"
    has_partitioned = any(item.is_dir() and item.name.startswith("date=") for item in path.iterdir())
    has_flat = any(item.is_file() and item.suffix == ".parquet" for item in path.iterdir())
    if has_partitioned and has_flat:
        return "mixed"
    if has_partitioned:
        return "partitioned"
    if has_flat:
        return "flat_symbol"
    return "empty"


def _files_for_pairs(path: Path, layout: str, pairs: pl.DataFrame) -> tuple[list[Path], int]:
    files: list[Path] = []
    zero_byte = 0
    if layout == "partitioned":
        for symbol, day in pairs.iter_rows():
            pair_root = path / f"date={day}" / f"symbol={symbol}"
            candidates = sorted(pair_root.glob("*.parquet")) if pair_root.is_dir() else []
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                if candidate.stat().st_size <= 0:
                    zero_byte += 1
                    continue
                files.append(candidate)
    elif layout == "flat_symbol":
        for symbol in pairs["symbol"].unique().sort().to_list():
            candidate = path / f"{symbol}.parquet"
            if not candidate.is_file():
                continue
            if candidate.stat().st_size <= 0:
                zero_byte += 1
                continue
            files.append(candidate)
    return sorted(set(files)), zero_byte


def _count_symbol_days(
    files: Sequence[Path],
    *,
    spec: DatasetSpec,
    layout: str,
    start: str,
    end: str,
) -> pl.DataFrame:
    if not files:
        return pl.DataFrame(
            {
                "symbol": [],
                "date": [],
                "rows": [],
                "unique_observations": [],
                "unique_timestamps": [],
                "off_grid_rows": [],
                "invalid_symbol_identity_rows": [],
                "invalid_date_identity_rows": [],
                "invalid_content_rows": [],
                "mixed_constant_groups": [],
                "invalid_observations_per_timestamp": [],
            },
            schema={
                "symbol": pl.String,
                "date": pl.String,
                "rows": pl.UInt32,
                "unique_observations": pl.UInt32,
                "unique_timestamps": pl.UInt32,
                "off_grid_rows": pl.UInt32,
                "invalid_symbol_identity_rows": pl.UInt32,
                "invalid_date_identity_rows": pl.UInt32,
                "invalid_content_rows": pl.UInt32,
                "mixed_constant_groups": pl.UInt32,
                "invalid_observations_per_timestamp": pl.UInt32,
            },
        )
    lf = pl.scan_parquet(
        [str(path) for path in files],
        hive_partitioning=False,
        include_file_paths="_fragment_path",
        missing_columns="raise",
        extra_columns="raise",
    )
    columns = set(lf.collect_schema().names())
    required = {"symbol", "ts_ms", *spec.required_columns}
    if missing := sorted(required - columns):
        raise RuntimeError(f"dataset missing required column(s) {missing}; got columns={sorted(columns)}")
    if spec.observation_discriminator and spec.observation_discriminator not in columns:
        raise RuntimeError(
            f"dataset requires observation discriminator {spec.observation_discriminator!r}; "
            f"got columns={sorted(columns)}"
        )
    selected = [pl.col(column) for column in sorted(required)]
    selected.append(pl.col("_fragment_path").cast(pl.String))
    if "date" in columns:
        selected.append(pl.col("date").cast(pl.String).alias("_stored_date"))
    observation_columns = ["ts_ms"]
    if spec.observation_discriminator:
        observation_columns.append(spec.observation_discriminator)
    off_grid = (
        ((pl.col("ts_ms") % spec.interval_ms) != 0).sum().alias("off_grid_rows")
        if spec.interval_ms
        else pl.lit(0, dtype=pl.UInt32).alias("off_grid_rows")
    )
    invalid_content_terms: list[pl.Expr] = []
    for column in spec.finite_columns:
        value = pl.col(column).cast(pl.Float64, strict=False)
        invalid_content_terms.extend((value.is_null(), ~value.is_finite()))
    for column in spec.positive_columns:
        value = pl.col(column).cast(pl.Float64, strict=False)
        invalid_content_terms.extend((value.is_null(), value <= 0))
    for column in spec.nonnegative_columns:
        value = pl.col(column).cast(pl.Float64, strict=False)
        invalid_content_terms.extend((value.is_null(), value < 0))
    for column, lower, upper in spec.bounded_columns:
        value = pl.col(column).cast(pl.Float64, strict=False)
        invalid_content_terms.extend((value.is_null(), value < lower, value > upper))
    for column in spec.nonempty_columns:
        value = pl.col(column).cast(pl.String, strict=False)
        invalid_content_terms.extend((value.is_null(), value.str.strip_chars() == ""))
    if spec.ohlc:
        open_ = pl.col("open").cast(pl.Float64, strict=False)
        high = pl.col("high").cast(pl.Float64, strict=False)
        low = pl.col("low").cast(pl.Float64, strict=False)
        close = pl.col("close").cast(pl.Float64, strict=False)
        invalid_content_terms.extend(
            (
                high < low,
                high < open_,
                high < close,
                low > open_,
                low > close,
            )
        )
    invalid_content = (
        pl.any_horizontal(invalid_content_terms).fill_null(True) if invalid_content_terms else pl.lit(False)
    )
    normalized_path = pl.col("_fragment_path").str.replace_all(r"\\", "/")
    frame = (
        lf.select(selected)
        .with_columns(
            pl.col("symbol").cast(pl.String),
            pl.col("ts_ms").cast(pl.Int64, strict=False),
            normalized_path.alias("_normalized_fragment_path"),
        )
        .with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"))
    )
    if layout == "partitioned":
        frame = frame.with_columns(
            pl.col("_normalized_fragment_path").str.extract(r"/date=([^/]+)/", 1).alias("_expected_date"),
            pl.col("_normalized_fragment_path").str.extract(r"/symbol=([^/]+)/", 1).alias("_expected_symbol"),
        )
    else:
        frame = frame.with_columns(
            pl.col("date").alias("_expected_date"),
            pl.col("_normalized_fragment_path").str.extract(r"/([^/]+)\.parquet$", 1).alias("_expected_symbol"),
        ).filter(
            (pl.col("ts_ms") >= int(datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp() * 1000))
            & (pl.col("ts_ms") < int(datetime.fromisoformat(end).replace(tzinfo=UTC).timestamp() * 1000))
        )
    invalid_date_identity = (
        pl.col("date").is_null() | pl.col("_expected_date").is_null() | (pl.col("date") != pl.col("_expected_date"))
    )
    if "date" in columns:
        invalid_date_identity |= pl.col("_stored_date").is_null() | (pl.col("_stored_date") != pl.col("date"))
    constant_mixed = (
        pl.any_horizontal([pl.col(column).n_unique() > 1 for column in spec.constant_columns])
        .cast(pl.UInt32)
        .alias("mixed_constant_groups")
        if spec.constant_columns
        else pl.lit(0, dtype=pl.UInt32).alias("mixed_constant_groups")
    )
    daily = frame.group_by(["symbol", "date"]).agg(
        pl.len().alias("rows"),
        pl.struct(observation_columns).n_unique().alias("unique_observations"),
        pl.col("ts_ms").n_unique().alias("unique_timestamps"),
        off_grid,
        (
            pl.col("symbol").is_null()
            | pl.col("_expected_symbol").is_null()
            | (pl.col("symbol") != pl.col("_expected_symbol"))
        )
        .sum()
        .alias("invalid_symbol_identity_rows"),
        invalid_date_identity.sum().alias("invalid_date_identity_rows"),
        invalid_content.sum().alias("invalid_content_rows"),
        constant_mixed,
    )
    if spec.observations_per_timestamp is not None:
        assert spec.observation_discriminator is not None
        per_timestamp = frame.group_by(["symbol", "date", "ts_ms"]).agg(
            pl.col(spec.observation_discriminator).n_unique().alias("observation_count")
        )
        timestamp_validity = per_timestamp.group_by(["symbol", "date"]).agg(
            (pl.col("observation_count") != spec.observations_per_timestamp)
            .sum()
            .alias("invalid_observations_per_timestamp")
        )
        daily = daily.join(timestamp_validity, on=["symbol", "date"], how="left")
    else:
        daily = daily.with_columns(pl.lit(0, dtype=pl.UInt32).alias("invalid_observations_per_timestamp"))
    return daily.sort(["symbol", "date"]).collect()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _content_identity(files: Sequence[Path], manifest_path: Path | None = None) -> str:
    """Exact path+byte identity; size/mtime metadata is not evidence identity."""
    digest = hashlib.sha256()
    if manifest_path is not None and manifest_path.is_file():
        digest.update(f"manifest\0{manifest_path}\0{_sha256_file(manifest_path)}\n".encode())
    for path in sorted(files):
        digest.update(f"file\0{path}\0{_sha256_file(path)}\n".encode())
    return digest.hexdigest()


def audit_dataset(
    root: Path,
    spec: DatasetSpec,
    *,
    pairs: pl.DataFrame,
    start: str,
    end: str,
    min_complete_coverage: float,
) -> dict[str, Any]:
    path = root / spec.storage_name
    layout = _layout(path)
    result: dict[str, Any] = {
        "logical_name": spec.logical_name,
        "storage_name": spec.storage_name,
        "path": str(path),
        "layout": layout,
        "frequency": spec.frequency,
        "source": spec.source,
        "evidence_use": spec.evidence_use,
        "min_rows_per_symbol_day": spec.min_rows_per_symbol_day,
        "exact_daily_grid": spec.exact_daily_grid,
        "expected_unique_timestamps": spec.expected_unique_timestamps,
        "reference_symbol_days": int(pairs.height),
        "executable": spec.executable,
    }
    if pairs.is_empty():
        return {**result, "status": "NO_PIT_REFERENCE", "reason": "zero PIT symbol-days in window"}
    if layout in {"missing", "empty"}:
        return {
            **result,
            "status": "MISSING",
            "complete_symbol_days": 0,
            "partial_symbol_days": 0,
            "missing_symbol_days": int(pairs.height),
            "complete_coverage": 0.0,
        }
    if layout == "mixed":
        return {
            **result,
            "status": "INVALID_LAYOUT",
            "reason": "flat and date-partitioned parquet coexist; normalize before reading or extending",
        }
    files, zero_byte = _files_for_pairs(path, layout, pairs)
    manifest_path = path / "_manifest.json"
    identity_before = _content_identity(files, manifest_path)
    try:
        counts = _count_symbol_days(files, spec=spec, layout=layout, start=start, end=end)
    except Exception as exc:  # noqa: BLE001 - the receipt must record unreadable data, not disappear
        return {
            **result,
            "status": "INVALID_DATA",
            "files": len(files),
            "zero_byte_files": zero_byte,
            "reason": repr(exc),
        }
    files_after, zero_byte_after = _files_for_pairs(path, layout, pairs)
    identity_after = _content_identity(files_after, manifest_path)
    if (
        identity_after != identity_before
        or files_after != files
        or zero_byte_after != zero_byte
    ):
        return {
            **result,
            "status": "INVALID_DATA",
            "files": len(files),
            "zero_byte_files": zero_byte,
            "reason": "dataset bytes changed during audit",
            "content_identity_before_sha256": identity_before,
            "content_identity_after_sha256": identity_after,
        }
    unexpected = counts.join(pairs, on=["symbol", "date"], how="anti")
    joined = (
        pairs.join(counts, on=["symbol", "date"], how="left")
        .with_columns(
            pl.col("rows").fill_null(0).cast(pl.UInt32),
            pl.col("unique_observations").fill_null(0).cast(pl.UInt32),
            pl.col("unique_timestamps").fill_null(0).cast(pl.UInt32),
            pl.col("off_grid_rows").fill_null(0).cast(pl.UInt32),
            pl.col("invalid_symbol_identity_rows").fill_null(0).cast(pl.UInt32),
            pl.col("invalid_date_identity_rows").fill_null(0).cast(pl.UInt32),
            pl.col("invalid_content_rows").fill_null(0).cast(pl.UInt32),
            pl.col("mixed_constant_groups").fill_null(0).cast(pl.UInt32),
            pl.col("invalid_observations_per_timestamp").fill_null(0).cast(pl.UInt32),
        )
        .with_columns((pl.col("rows") - pl.col("unique_observations")).alias("duplicate_rows"))
    )
    complete_expr = (
        (pl.col("unique_observations") >= spec.min_rows_per_symbol_day)
        & (pl.col("duplicate_rows") == 0)
        & (pl.col("off_grid_rows") == 0)
        & (pl.col("invalid_symbol_identity_rows") == 0)
        & (pl.col("invalid_date_identity_rows") == 0)
        & (pl.col("invalid_content_rows") == 0)
        & (pl.col("mixed_constant_groups") == 0)
        & (pl.col("invalid_observations_per_timestamp") == 0)
    )
    if spec.exact_daily_grid:
        complete_expr &= pl.col("unique_observations") == spec.min_rows_per_symbol_day
        if spec.expected_unique_timestamps is not None:
            complete_expr &= pl.col("unique_timestamps") == spec.expected_unique_timestamps
    complete = joined.filter(complete_expr)
    partial = joined.filter((pl.col("rows") > 0) & ~complete_expr)
    missing = joined.filter(pl.col("rows") == 0)
    complete_coverage = complete.height / pairs.height
    duplicate_rows = int((counts["rows"] - counts["unique_observations"]).sum() or 0)
    off_grid_rows = int(counts["off_grid_rows"].sum() or 0)
    invalid_symbol_identity_rows = int(counts["invalid_symbol_identity_rows"].sum() or 0)
    invalid_date_identity_rows = int(counts["invalid_date_identity_rows"].sum() or 0)
    invalid_content_rows = int(counts["invalid_content_rows"].sum() or 0)
    mixed_constant_groups = int(counts["mixed_constant_groups"].sum() or 0)
    invalid_observations_per_timestamp = int(counts["invalid_observations_per_timestamp"].sum() or 0)
    invalid_reasons = []
    if zero_byte:
        invalid_reasons.append("zero_byte_parquet_fragment")
    if unexpected.height:
        invalid_reasons.append("unexpected_symbol_day_content")
    if duplicate_rows:
        invalid_reasons.append("duplicate_observation_key")
    if off_grid_rows:
        invalid_reasons.append("off_grid_timestamp")
    if invalid_symbol_identity_rows:
        invalid_reasons.append("path_symbol_identity_mismatch")
    if invalid_date_identity_rows:
        invalid_reasons.append("path_or_stored_date_identity_mismatch")
    if invalid_content_rows:
        invalid_reasons.append("invalid_required_content")
    if mixed_constant_groups:
        invalid_reasons.append("mixed_interval_or_constant_identity")
    if invalid_observations_per_timestamp:
        invalid_reasons.append("invalid_observations_per_timestamp")
    status = (
        "INVALID_DATA"
        if invalid_reasons
        else "READY"
        if complete_coverage >= min_complete_coverage and partial.is_empty()
        else "PARTIAL"
    )
    return {
        **result,
        "status": status,
        "files": len(files),
        "zero_byte_files": zero_byte,
        "complete_symbol_days": int(complete.height),
        "partial_symbol_days": int(partial.height),
        "missing_symbol_days": int(missing.height),
        "complete_coverage": complete_coverage,
        "min_observed_rows": int(joined["rows"].min() or 0),
        "max_observed_rows": int(joined["rows"].max() or 0),
        "duplicate_rows": duplicate_rows,
        "off_grid_rows": off_grid_rows,
        "invalid_symbol_identity_rows": invalid_symbol_identity_rows,
        "invalid_date_identity_rows": invalid_date_identity_rows,
        "invalid_content_rows": invalid_content_rows,
        "mixed_constant_groups": mixed_constant_groups,
        "invalid_observations_per_timestamp": invalid_observations_per_timestamp,
        "invalid_reasons": invalid_reasons,
        "unexpected_symbol_day_examples": unexpected.head(20).to_dicts(),
        "partial_examples": partial.head(20).to_dicts(),
        "missing_examples": missing.head(20).select(["symbol", "date"]).to_dicts(),
        "content_identity_sha256": identity_after,
        "manifest_present": manifest_path.is_file(),
    }


def _audit_forward_tape(path: Path, name: str) -> dict[str, Any]:
    files = (
        sorted(
            item
            for item in path.glob("*")
            if item.is_file() and (item.name.endswith(".jsonl") or item.name.endswith(".jsonl.gz"))
        )
        if path.is_dir()
        else []
    )
    days = sorted({item.name[:10] for item in files if len(item.name) >= 10})
    return {
        "name": name,
        "path": str(path),
        "status": "PRESENT" if files else "MISSING",
        "files": len(files),
        "bytes": sum(item.stat().st_size for item in files),
        "min_day": days[0] if days else None,
        "max_day": days[-1] if days else None,
        "source": "Bybit forward public capture",
        "audit_scope": "local filesystem path only; a missing local path says nothing about VPS service health",
        "evidence_use": "shadow/context only; never historical acceptance or alpha evidence",
        "content_identity_sha256": _content_identity(files),
    }


def _specs_for(venue: str, logical_names: Sequence[str]) -> tuple[DatasetSpec, ...]:
    available = BYBIT_SPECS if venue == "bybit" else BINANCE_SPECS
    wanted = set(logical_names)
    return tuple(spec for spec in available if spec.logical_name in wanted)


def _validate_executable_selection(
    venues: Sequence[str], logical_names: Sequence[str]
) -> None:
    for venue in venues:
        supported = {
            spec.logical_name: spec.executable
            for spec in (BYBIT_SPECS if venue == "bybit" else BINANCE_SPECS)
        }
        invalid = [
            name
            for name in logical_names
            if name not in supported or not supported[name]
        ]
        if invalid:
            raise ValueError(
                f"no maintained resume-safe {venue} downloader for: {', '.join(invalid)}; "
                "audit those datasets read-only or select the supporting venue"
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve(strict=False)
    right = right.resolve(strict=False)
    if left == right or left in right.parents or right in left.parents:
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _root_scope_paths(root: Path, venue: str, logical_names: Sequence[str]) -> tuple[Path, ...]:
    paths = {
        root.resolve(strict=False),
        (root / "archive_trade_manifest").resolve(strict=False),
        (root / "_download_markers").resolve(strict=False),
    }
    for spec in _specs_for(venue, logical_names):
        paths.add((root / spec.storage_name).resolve(strict=False))
    return tuple(sorted(paths, key=str))


def validate_disjoint_roots(bybit_root: Path, binance_root: Path, logical_names: Sequence[str]) -> None:
    """Fail when two venue roots or any selected data scopes overlap.

    ``resolve`` catches symlink aliases; ancestor checks catch nesting; selected
    dataset/manifest path comparisons catch otherwise-disjoint roots whose child
    directories resolve to a shared external store.
    """
    bybit_paths = _root_scope_paths(bybit_root, "bybit", logical_names)
    binance_paths = _root_scope_paths(binance_root, "binance", logical_names)
    for bybit_path in bybit_paths:
        for binance_path in binance_paths:
            if _paths_overlap(bybit_path, binance_path):
                raise RuntimeError(
                    "Bybit and Binance roots/scopes must be disjoint; overlap detected: "
                    f"{bybit_path} <-> {binance_path}"
                )


def validate_receipt_path(value: str | Path, *, data_roots: Sequence[Path]) -> Path:
    """Return a new, outside-root JSON receipt path or fail without writing."""
    raw = Path(value).expanduser()
    if raw.suffix.lower() != ".json":
        raise ValueError("--output must be a .json receipt")
    resolved = raw.resolve(strict=False)
    protected_parts = {"archive_trade_manifest", "_download_markers"}
    if resolved.name == "_manifest.json" or protected_parts.intersection(resolved.parts):
        raise ValueError("--output cannot target a data/downloader manifest path")
    for root in data_roots:
        if _paths_overlap(resolved, root.resolve(strict=False)):
            raise ValueError(f"--output must be outside every data root: {resolved} overlaps {root}")
    if resolved.exists() or resolved.is_symlink():
        raise FileExistsError(f"receipt already exists and is immutable: {resolved}")
    return resolved


def _manifest_files_for_window(root: Path, *, start: str, end: str) -> list[Path]:
    files: list[Path] = []
    manifest = root / "archive_trade_manifest"
    for day in _day_range(_parse_day(start), _parse_day(end)):
        files.extend(
            sorted(
                path
                for path in (manifest / f"date={day}").glob("*.parquet")
                if path.is_file()
            )
        )
    return files


def audit_venue(
    venue: str,
    root: Path,
    *,
    logical_names: Sequence[str],
    start: str | None,
    end: str | None,
    symbols: Sequence[str],
    max_symbol_days: int,
    min_complete_coverage: float,
) -> dict[str, Any]:
    resolved_start, resolved_end = _resolve_window(root, start, end)
    manifest_files = _manifest_files_for_window(
        root,
        start=resolved_start,
        end=resolved_end,
    )
    manifest_identity_before = _content_identity(manifest_files)
    pairs = load_manifest_pairs(
        root,
        start=resolved_start,
        end=resolved_end,
        symbols=symbols,
        max_symbol_days=max_symbol_days,
    )
    manifest_files_after = _manifest_files_for_window(
        root,
        start=resolved_start,
        end=resolved_end,
    )
    manifest_identity_after = _content_identity(manifest_files_after)
    if (
        manifest_identity_after != manifest_identity_before
        or manifest_files_after != manifest_files
    ):
        raise RuntimeError(
            f"PIT manifest bytes changed during {venue} audit: "
            f"{manifest_identity_before} -> {manifest_identity_after}"
        )
    datasets = [
        audit_dataset(
            root,
            spec,
            pairs=pairs,
            start=resolved_start,
            end=resolved_end,
            min_complete_coverage=min_complete_coverage,
        )
        for spec in _specs_for(venue, logical_names)
    ]
    return {
        "venue": venue,
        "root": str(root),
        "start": resolved_start,
        "end_exclusive": resolved_end,
        "pit_symbol_days": int(pairs.height),
        "pit_symbols": int(pairs["symbol"].n_unique()) if pairs.height else 0,
        "pit_manifest_files": len(manifest_files),
        "pit_manifest_content_sha256": manifest_identity_after,
        "datasets": datasets,
    }


def _logical_names(value: str | None, *, execute: bool) -> tuple[str, ...]:
    if value is None:
        if execute:
            raise ValueError("--execute requires an explicit --datasets list")
        return DEFAULT_LOGICAL_DATASETS
    names = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    unknown = sorted(set(names) - set(DEFAULT_LOGICAL_DATASETS))
    if unknown:
        raise ValueError(
            f"unknown logical dataset(s): {', '.join(unknown)}; known={','.join(DEFAULT_LOGICAL_DATASETS)}"
        )
    if execute:
        non_executable = sorted(set(names) - EXECUTABLE_DATASETS)
        if non_executable:
            raise ValueError("no supported resume-safe downloader for: " + ", ".join(non_executable))
    return names


def _parse_symbols(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))


def _execution_symbols(
    root: Path,
    *,
    start: str,
    end: str,
    explicit_symbols: Sequence[str],
    all_pit_symbols: bool,
    max_symbol_days: int,
) -> tuple[str, ...]:
    pairs = load_manifest_pairs(
        root,
        start=start,
        end=end,
        symbols=explicit_symbols,
        max_symbol_days=max_symbol_days,
    )
    available = tuple(pairs["symbol"].unique().sort().to_list())
    if explicit_symbols:
        missing = sorted(set(explicit_symbols) - set(available))
        if missing:
            raise RuntimeError(f"requested symbol(s) absent from {root}'s PIT manifest in [{start},{end}): {missing}")
        return tuple(explicit_symbols)
    if all_pit_symbols:
        return available
    raise ValueError("--execute requires --symbols or --all-pit-symbols")


def _manifest_end_exclusive(root: Path) -> str:
    days = _manifest_days(root)
    if not days:
        raise RuntimeError(f"no PIT manifest tail under {root}")
    return (_parse_day(days[-1]) + timedelta(days=1)).isoformat()


def build_download_commands(
    venue: str,
    root: Path,
    *,
    logical_names: Sequence[str],
    start: str,
    end: str,
    symbols: Sequence[str],
    workers: int,
    python_bin: str,
    bybit_oi_interval: str,
) -> list[list[str]]:
    """Build argv-only commands. No shell interpolation is used."""
    selected = set(logical_names)
    symbol_csv = ",".join(symbols)
    commands: list[list[str]] = []
    if "klines_5m" in selected:
        current_layout = _layout(root / "klines_5m")
        if current_layout in {"flat_symbol", "mixed"}:
            raise RuntimeError(
                f"refusing {venue} klines_5m extension: layout={current_layout}; "
                "the canonical backfiller writes date partitions and must not be mixed with flat files"
            )
        commands.append(
            [
                python_bin,
                str(REPO / "scripts" / "backfill_5m_klines.py"),
                "--venue",
                venue,
                "--start",
                start,
                "--end",
                end,
                f"--{venue}-root",
                str(root),
                "--workers",
                str(workers),
                "--symbols",
                symbol_csv,
            ]
        )

    ancillary = selected & {"funding", "open_interest", "premium_index_1h", "taker_flow"}
    if venue == "bybit":
        if "taker_flow" in ancillary:
            raise RuntimeError("Bybit taker_flow_5m has no maintained resume-safe downloader; audit it read-only")
        names = sorted(ancillary)
        if names:
            commands.append(
                [
                    python_bin,
                    "-m",
                    "liquidity_migration",
                    "--data-root",
                    str(root),
                    "download-data",
                    "--symbols",
                    symbol_csv,
                    "--start",
                    start,
                    "--end",
                    end,
                    "--datasets",
                    ",".join(names),
                    "--workers",
                    str(workers),
                    "--open-interest-interval",
                    bybit_oi_interval,
                ]
            )
    else:
        proxy_names = sorted("taker_flow_1h" if name == "taker_flow" else name for name in ancillary)
        if proxy_names:
            commands.append(
                [
                    python_bin,
                    "-m",
                    "liquidity_migration",
                    "--data-root",
                    str(root),
                    "download-binance-proxy",
                    "--symbols",
                    symbol_csv,
                    "--start",
                    start,
                    "--end",
                    end,
                    "--datasets",
                    ",".join(proxy_names),
                    "--workers",
                    str(workers),
                ]
            )
        tail = _manifest_end_exclusive(root)
        vision_selected = selected & {"metrics_5m", "bookdepth_1h"}
        if vision_selected and end != tail:
            raise RuntimeError(
                "Binance Vision metrics/bookdepth writers currently run from --start through the root's "
                f"manifest tail ({tail}); requested end={end}. Use that exact end or narrow with --symbols."
            )
        if "metrics_5m" in selected:
            commands.append(
                [
                    python_bin,
                    str(REPO / "scripts" / "backfill_binance_metrics_vision.py"),
                    "--root",
                    str(root),
                    "--start",
                    start,
                    "--workers",
                    str(workers),
                    "--symbols",
                    symbol_csv,
                ]
            )
        if "bookdepth_1h" in selected:
            commands.append(
                [
                    python_bin,
                    str(REPO / "scripts" / "backfill_binance_bookdepth_vision.py"),
                    "--root",
                    str(root),
                    "--start",
                    start,
                    "--workers",
                    str(workers),
                    "--symbols",
                    symbol_csv,
                ]
            )
    return commands


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink receipt: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(temp, path)


def _create_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    """Create the initial receipt without replacing any existing filesystem object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _run_commands(commands: Sequence[Sequence[str]], receipt: dict[str, Any], output: Path) -> int:
    receipt["commands"] = []
    _create_json_exclusive(output, receipt)
    for index, command in enumerate(commands):
        row = {
            "index": index,
            "argv": list(command),
            "started_at_utc": datetime.now(tz=UTC).isoformat(),
            "status": "running",
        }
        receipt["commands"].append(row)
        _atomic_write_json(output, receipt)
        completed = subprocess.run(list(command), cwd=REPO, check=False)  # noqa: S603 - fixed argv, explicit operator action
        row["finished_at_utc"] = datetime.now(tz=UTC).isoformat()
        row["returncode"] = int(completed.returncode)
        row["status"] = "complete" if completed.returncode == 0 else "failed"
        _atomic_write_json(output, receipt)
        if completed.returncode != 0:
            receipt["status"] = "failed"
            receipt["failed_command_index"] = index
            _atomic_write_json(output, receipt)
            return int(completed.returncode) or 1
    receipt["status"] = "complete"
    receipt["finished_at_utc"] = datetime.now(tz=UTC).isoformat()
    _atomic_write_json(output, receipt)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--venue", choices=("bybit", "binance", "both"), default="both")
    parser.add_argument("--bybit-root", default=str(Path.home() / "SHARED_DATA" / "bybit_full_pit"))
    parser.add_argument("--binance-root", default=str(Path.home() / "SHARED_DATA" / "binance_full_pit"))
    parser.add_argument("--start", help="Inclusive YYYY-MM-DD; audit defaults to seven days before each manifest tail.")
    parser.add_argument("--end", help="Exclusive YYYY-MM-DD; audit defaults to each manifest tail + one day.")
    parser.add_argument("--datasets", help="Comma-separated logical datasets; audit defaults to all known datasets.")
    parser.add_argument("--symbols", default="", help="Comma-separated PIT symbol filter.")
    parser.add_argument(
        "--all-pit-symbols",
        action="store_true",
        help="Execution authority for every PIT manifest symbol in the explicit window.",
    )
    parser.add_argument("--execute", action="store_true", help="Perform network downloads. Omit for read-only audit.")
    parser.add_argument("--output", help="Atomic JSON receipt path; required with --execute.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-symbol-days", type=int, default=DEFAULT_MAX_SYMBOL_DAYS)
    parser.add_argument("--min-complete-coverage", type=float, default=0.995)
    parser.add_argument(
        "--bybit-oi-interval",
        choices=("5min", "15min", "30min", "1h", "4h", "1d"),
        default="1h",
    )
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument(
        "--forward-liquidations-root",
        default=str(REPO / "data" / "liquidations" / "bybit"),
    )
    parser.add_argument("--forward-depth-root", default=str(REPO / "data" / "depth" / "bybit"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.workers <= 32:
        raise ValueError("--workers must be between 1 and 32")
    if args.max_symbol_days <= 0:
        raise ValueError("--max-symbol-days must be positive")
    if not 0.0 < args.min_complete_coverage <= 1.0:
        raise ValueError("--min-complete-coverage must be in (0,1]")
    logical_names = _logical_names(args.datasets, execute=args.execute)
    symbols = _parse_symbols(args.symbols)
    venues = ("bybit", "binance") if args.venue == "both" else (args.venue,)
    if args.execute:
        _validate_executable_selection(venues, logical_names)
    roots = {
        "bybit": Path(args.bybit_root).expanduser().resolve(),
        "binance": Path(args.binance_root).expanduser().resolve(),
    }
    if args.venue == "both":
        validate_disjoint_roots(roots["bybit"], roots["binance"], logical_names)
    if args.execute and not args.output:
        raise ValueError("--execute requires --output for checkpoint/resume auditability")
    output_path = (
        validate_receipt_path(args.output, data_roots=tuple(roots[venue] for venue in venues)) if args.output else None
    )
    started = datetime.now(tz=UTC).isoformat()

    if not args.execute:
        reports = [
            audit_venue(
                venue,
                roots[venue],
                logical_names=logical_names,
                start=args.start,
                end=args.end,
                symbols=symbols,
                max_symbol_days=args.max_symbol_days,
                min_complete_coverage=args.min_complete_coverage,
            )
            for venue in venues
        ]
        payload = {
            "schema_version": 1,
            "mode": "audit",
            "network_used": False,
            "run_label": "data_readiness_only",
            "started_at_utc": started,
            "finished_at_utc": datetime.now(tz=UTC).isoformat(),
            "venues": reports,
            "forward_context": [
                _audit_forward_tape(Path(args.forward_liquidations_root).expanduser(), "bybit_liquidations"),
                _audit_forward_tape(Path(args.forward_depth_root).expanduser(), "bybit_depth"),
            ],
            "warning": "Coverage is not strategy evidence. Forward liquidation/depth is shadow/context only.",
        }
        if output_path is not None:
            _create_json_exclusive(output_path, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not args.start or not args.end:
        raise ValueError("--execute requires explicit --start and --end boundaries")
    if bool(symbols) == bool(args.all_pit_symbols):
        raise ValueError("--execute requires exactly one of --symbols or --all-pit-symbols")
    start = _parse_day(args.start).isoformat()
    end = _parse_day(args.end).isoformat()
    if start >= end:
        raise ValueError("--start must precede --end")

    commands: list[list[str]] = []
    execution_scope: list[dict[str, Any]] = []
    for venue in venues:
        venue_symbols = _execution_symbols(
            roots[venue],
            start=start,
            end=end,
            explicit_symbols=symbols,
            all_pit_symbols=args.all_pit_symbols,
            max_symbol_days=args.max_symbol_days,
        )
        venue_commands = build_download_commands(
            venue,
            roots[venue],
            logical_names=logical_names,
            start=start,
            end=end,
            symbols=venue_symbols,
            workers=args.workers,
            python_bin=args.python_bin,
            bybit_oi_interval=args.bybit_oi_interval,
        )
        commands.extend(venue_commands)
        execution_scope.append(
            {
                "venue": venue,
                "root": str(roots[venue]),
                "symbols": list(venue_symbols),
                "symbol_count": len(venue_symbols),
                "commands": len(venue_commands),
            }
        )
    if not commands:
        raise RuntimeError("selected datasets produced no executable commands")
    receipt = {
        "schema_version": 1,
        "mode": "execute",
        "network_used": True,
        "run_label": "data_download_only",
        "status": "running",
        "started_at_utc": started,
        "start": start,
        "end_exclusive": end,
        "logical_datasets": list(logical_names),
        "scope": execution_scope,
        "warning": "A successful download is data readiness, not strategy evidence.",
    }
    assert output_path is not None
    return _run_commands(commands, receipt, output_path)


if __name__ == "__main__":
    raise SystemExit(main())
