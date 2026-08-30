"""Point-in-time (PIT) membership + full-PIT universe validation.

This is the methodology-critical no-look-ahead / no-survivorship gate consumed
by the backtest engines, with shared frame helpers imported from
_common / trade_lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from liquidity_migration.core._common import MS_PER_HOUR, _date_symbol_set, _symbol_set
from liquidity_migration.data.trade_lifecycle import _has_columns


def filter_klines_to_pit_membership(
    klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Semi-join hourly bars to contemporaneous manifest membership.

    Must be applied *before* any per-symbol rolling feature or cross-sectional
    rank: a later membership gate can stop an ineligible symbol from trading, but
    cannot undo its earlier influence on ranks, cut-offs, or rolling state.

    The returned frame preserves the input schema and row order; the receipt
    carries structural counts only.
    """

    required_manifest = {"date", "symbol"}
    missing_manifest = sorted(required_manifest - set(archive_manifest.columns))
    if archive_manifest.is_empty() or missing_manifest:
        detail = "empty" if archive_manifest.is_empty() else f"missing columns {missing_manifest}"
        raise RuntimeError(f"archive_trade_manifest is not usable for PIT filtering: {detail}")
    manifest_keys = (
        archive_manifest.select(
            pl.col("date").cast(pl.String, strict=True).alias("_pit_date"),
            pl.col("symbol").cast(pl.String, strict=True).str.strip_chars().alias("_pit_symbol"),
        )
        .drop_nulls()
    )
    invalid_manifest = manifest_keys.filter(
        (pl.col("_pit_symbol") == "")
        | ~pl.col("_pit_date").str.contains(r"^\d{4}-\d{2}-\d{2}$")
        | pl.col("_pit_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False).is_null()
    )
    if not invalid_manifest.is_empty():
        raise RuntimeError(
            "archive_trade_manifest contains blank symbols or invalid ISO dates; "
            f"invalid_rows={invalid_manifest.height}"
        )
    manifest_pairs = manifest_keys.unique(["_pit_date", "_pit_symbol"], maintain_order=True)
    if manifest_pairs.is_empty():
        raise RuntimeError("archive_trade_manifest has no non-null (date, symbol) membership pairs")

    if klines.is_empty():
        return klines, {
            "schema_version": 1,
            "pit_membership_applied_before_features": True,
            "date_source": "date+ts_ms_verified" if "date" in klines.columns else "ts_ms",
            "input_rows": 0,
            "output_rows": 0,
            "dropped_rows": 0,
            "input_date_symbol_pairs": 0,
            "output_date_symbol_pairs": 0,
            "dropped_date_symbol_pairs": 0,
            "manifest_rows": archive_manifest.height,
            "manifest_date_symbol_pairs": manifest_pairs.height,
            "duplicate_manifest_rows": archive_manifest.height - manifest_pairs.height,
        }
    missing_klines = sorted({"ts_ms", "symbol"} - set(klines.columns))
    if missing_klines:
        raise RuntimeError(f"klines are not usable for PIT filtering: missing columns {missing_klines}")

    derived_date = pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d")
    prepared = klines.with_row_index("_pit_row_order").with_columns(
        pl.col("symbol").cast(pl.String, strict=True).str.strip_chars().alias("_pit_symbol"),
        derived_date.alias("_pit_derived_date"),
    )
    invalid_klines = prepared.filter(
        pl.col("ts_ms").is_null()
        | pl.col("_pit_symbol").is_null()
        | (pl.col("_pit_symbol") == "")
        | pl.col("_pit_derived_date").is_null()
    )
    if not invalid_klines.is_empty():
        raise RuntimeError(
            "klines contain null timestamps or blank symbols; "
            f"invalid_rows={invalid_klines.height}"
        )

    date_source = "ts_ms"
    if "date" in klines.columns:
        prepared = prepared.with_columns(
            pl.col("date").cast(pl.String, strict=True).alias("_pit_declared_date")
        )
        inconsistent_dates = prepared.filter(
            pl.col("_pit_declared_date").is_null()
            | (pl.col("_pit_declared_date") != pl.col("_pit_derived_date"))
        )
        if not inconsistent_dates.is_empty():
            raise RuntimeError(
                "kline date disagrees with UTC date derived from ts_ms; "
                f"invalid_rows={inconsistent_dates.height}"
            )
        date_source = "date+ts_ms_verified"
    prepared = prepared.with_columns(pl.col("_pit_derived_date").alias("_pit_date"))

    input_pairs = prepared.select("_pit_date", "_pit_symbol").unique()
    filtered = (
        prepared.join(manifest_pairs, on=["_pit_date", "_pit_symbol"], how="semi")
        .sort("_pit_row_order")
        .select(klines.columns)
    )
    output_pairs = (
        filtered.select(
            derived_date.alias("_pit_date"),
            pl.col("symbol").cast(pl.String, strict=True).str.strip_chars().alias("_pit_symbol"),
        )
        .unique()
    )
    return filtered, {
        "schema_version": 1,
        "pit_membership_applied_before_features": True,
        "date_source": date_source,
        "input_rows": klines.height,
        "output_rows": filtered.height,
        "dropped_rows": klines.height - filtered.height,
        "input_date_symbol_pairs": input_pairs.height,
        "output_date_symbol_pairs": output_pairs.height,
        "dropped_date_symbol_pairs": input_pairs.height - output_pairs.height,
        "manifest_rows": archive_manifest.height,
        "manifest_date_symbol_pairs": manifest_pairs.height,
        "duplicate_manifest_rows": archive_manifest.height - manifest_pairs.height,
    }


def _pit_manifest_metadata(
    archive_manifest: pl.DataFrame,
    features: pl.DataFrame,
    klines: pl.DataFrame,
    *,
    full_pit_universe_pass: bool | None = None,
    kline_covered_date_symbols: set[tuple[str, str]] | None = None,
    required_pit_date_symbols: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    # Covered/required sets and the full-PIT pass are expensive over multi-GB
    # klines. The LONG caller computes them once and threads them in; defaults
    # preserve standalone behavior for other callers.
    manifest_symbols = _symbol_set(archive_manifest)
    feature_symbols = _symbol_set(features)
    manifest_date_symbols = _date_symbol_set(archive_manifest)
    if kline_covered_date_symbols is None:
        kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    if required_pit_date_symbols is None:
        required_pit_date_symbols = _required_pit_date_symbols(
            klines,
            archive_manifest,
        )
    if full_pit_universe_pass is None:
        full_pit_universe_pass = _full_pit_universe_pass(
            klines, archive_manifest, kline_covered_date_symbols=kline_covered_date_symbols
        )
    return {
        "rows": archive_manifest.height,
        "symbols": len(manifest_symbols),
        "feature_symbols": len(feature_symbols),
        "feature_symbols_missing_from_manifest": len(feature_symbols - manifest_symbols),
        "manifest_symbols_missing_from_features": len(manifest_symbols - feature_symbols),
        "manifest_date_symbols": len(manifest_date_symbols),
        "kline_covered_date_symbols": len(kline_covered_date_symbols),
        "manifest_date_symbols_missing_from_klines": len(manifest_date_symbols - kline_covered_date_symbols),
        "required_manifest_date_symbols": len(required_pit_date_symbols),
        "required_manifest_date_symbols_missing_from_klines": len(
            required_pit_date_symbols - kline_covered_date_symbols
        ),
        "full_pit_universe_pass": full_pit_universe_pass,
    }


def _required_pit_date_symbols(
    _klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
) -> set[tuple[str, str]]:
    """Manifest pairs whose hourly partitions a full-PIT build must cover.

    The manifest is the independent membership boundary. Kline starts, ends,
    and internal gaps are the data being checked, so none of them may narrow
    the requirement. An empty archive payload records a data-source observation,
    not proof that the venue had no trading, so the pair remains required.
    """

    return _date_symbol_set(archive_manifest)


@dataclass(frozen=True, slots=True)
class FullPitUniverseCoverage:
    manifest_symbols: set[str]
    kline_symbols: set[str]
    required_date_symbols: set[tuple[str, str]]
    covered_date_symbols: set[tuple[str, str]]

    @property
    def missing_symbols(self) -> set[str]:
        return self.manifest_symbols - self.kline_symbols

    @property
    def missing_required_date_symbols(self) -> set[tuple[str, str]]:
        return self.required_date_symbols - self.covered_date_symbols

    @property
    def passed(self) -> bool:
        return (
            bool(self.manifest_symbols)
            and not self.missing_symbols
            and bool(self.required_date_symbols)
            and not self.missing_required_date_symbols
        )


def _full_pit_universe_coverage(
    klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
    *,
    kline_covered_date_symbols: set[tuple[str, str]] | None = None,
) -> FullPitUniverseCoverage:
    if kline_covered_date_symbols is None:
        kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    return FullPitUniverseCoverage(
        manifest_symbols=_symbol_set(archive_manifest),
        kline_symbols=_symbol_set(klines),
        required_date_symbols=_required_pit_date_symbols(klines, archive_manifest),
        covered_date_symbols=kline_covered_date_symbols,
    )


def _full_pit_universe_pass(
    klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
    *,
    kline_covered_date_symbols: set[tuple[str, str]] | None = None,
) -> bool:
    return _full_pit_universe_coverage(
        klines,
        archive_manifest,
        kline_covered_date_symbols=kline_covered_date_symbols,
    ).passed


def _covered_kline_date_symbol_set(klines: pl.DataFrame, *, min_hourly_bars: int = 20) -> set[tuple[str, str]]:
    """Return structurally covered days, including causal nulls before a listing."""

    required = ("date", "symbol", "ts_ms", "open", "high", "low", "close")
    if klines.is_empty() or not _has_columns(klines, *required):
        return set()
    derived_date = pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime(
        "%Y-%m-%d"
    )
    open_price = pl.col("open").cast(pl.Float64, strict=False)
    high_price = pl.col("high").cast(pl.Float64, strict=False)
    low_price = pl.col("low").cast(pl.Float64, strict=False)
    close_price = pl.col("close").cast(pl.Float64, strict=False)
    valid_ohlc = (
        pl.all_horizontal(
            [
                price.is_finite() & (price > 0.0)
                for price in (open_price, high_price, low_price, close_price)
            ]
        )
        & (high_price >= pl.max_horizontal(open_price, low_price, close_price))
        & (low_price <= pl.min_horizontal(open_price, high_price, close_price))
    ).fill_null(False)
    covered = (
        klines.drop_nulls(["date", "symbol", "ts_ms"])
        .filter(
            (pl.col("ts_ms") % MS_PER_HOUR == 0)
            & (pl.col("date").cast(pl.String) == derived_date)
        )
        .with_columns(valid_ohlc.alias("__valid_ohlc"))
        .group_by(["date", "symbol"])
        .agg(
            pl.col("ts_ms").n_unique().alias("aligned_hour_keys"),
            pl.col("__valid_ohlc").sum().alias("valid_ohlc_rows"),
        )
        .filter(
            (pl.col("aligned_hour_keys") >= min_hourly_bars)
            & (pl.col("valid_ohlc_rows") >= 1)
        )
        .select(["date", "symbol"])
    )
    return _date_symbol_set(covered)
