"""Point-in-time (PIT) membership + full-PIT universe validation.

This is the methodology-critical no-look-ahead / no-survivorship gate consumed
by the long/continuous engines, with shared frame helpers imported from
_common / trade_lifecycle.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

import polars as pl

from liquidity_migration.core._common import _date_symbol_set, _symbol_set
from liquidity_migration.data.archive_manifest import V5_LISTING_SOURCE, V5_LISTING_URL_SENTINEL
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


def _symbol_kline_date_bounds(klines: pl.DataFrame) -> dict[str, tuple[str, str]]:
    """Per-symbol (first, last) kline `date` — the coin's traded lifespan in our archive."""
    if klines.is_empty() or "symbol" not in klines.columns or "date" not in klines.columns:
        return {}
    agg = klines.group_by("symbol").agg(
        pl.col("date").min().alias("first_date"),
        pl.col("date").max().alias("last_date"),
    )
    return {
        str(r["symbol"]): (str(r["first_date"]), str(r["last_date"]))
        for r in agg.drop_nulls(["first_date", "last_date"]).to_dicts()
    }


def _current_listing_derived_date_symbols(
    archive_manifest: pl.DataFrame,
) -> set[tuple[str, str]]:
    """Manifest pairs independently inferred from a currently-Trading listing."""

    predicates: list[pl.Expr] = []
    if "source" in archive_manifest.columns:
        predicates.append(pl.col("source") == V5_LISTING_SOURCE)
    if "url" in archive_manifest.columns:
        predicates.append(pl.col("url") == V5_LISTING_URL_SENTINEL)
    if not predicates:
        return set()
    predicate = predicates[0]
    for extra in predicates[1:]:
        predicate = predicate | extra
    return _date_symbol_set(archive_manifest.filter(predicate.fill_null(False)))


def _observed_v5_launch_dates(
    archive_manifest: pl.DataFrame,
) -> dict[str, tuple[str, ...]]:
    """Observed v5 listing starts, including a later reused-symbol incarnation."""

    column = "v5_observed_launch_date"
    if (
        archive_manifest.is_empty()
        or "symbol" not in archive_manifest.columns
        or column not in archive_manifest.columns
    ):
        return {}
    observed = (
        archive_manifest.select("symbol", column)
        .drop_nulls(["symbol", column])
        .unique()
    )
    launches: dict[str, set[str]] = {}
    for symbol, launch_date in observed.iter_rows():
        value = str(launch_date)
        if len(value) == 10:
            launches.setdefault(str(symbol), set()).add(value)
    return {
        symbol: tuple(sorted(values))
        for symbol, values in launches.items()
    }


def _incarnation_segment_bounds(
    klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
    global_bounds: dict[str, tuple[str, str]],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[tuple[str, int], tuple[str, str]],
]:
    """Return relisting boundaries and traded bounds inside each incarnation.

    Symbols whose observed v5 launch is not later than their first stored kline
    need no special treatment. For a reused ticker, only its distinct dates are
    materialized here; the common one-incarnation path retains the cheaper
    global min/max aggregation.
    """

    observed = _observed_v5_launch_dates(archive_manifest)
    boundaries = {
        symbol: tuple(day for day in days if day > bounds[0])
        for symbol, days in observed.items()
        if (bounds := global_bounds.get(symbol)) is not None
        and any(day > bounds[0] for day in days)
    }
    boundaries = {symbol: days for symbol, days in boundaries.items() if days}
    if not boundaries:
        return {}, {}

    date_rows = (
        klines.filter(pl.col("symbol").is_in(list(boundaries)))
        .select("symbol", "date")
        .drop_nulls()
        .unique()
    )
    segment_bounds: dict[tuple[str, int], tuple[str, str]] = {}
    for symbol_raw, day_raw in date_rows.iter_rows():
        symbol = str(symbol_raw)
        day = str(day_raw)
        segment = bisect_right(boundaries[symbol], day)
        key = (symbol, segment)
        current = segment_bounds.get(key)
        if current is None:
            segment_bounds[key] = (day, day)
        else:
            segment_bounds[key] = (min(current[0], day), max(current[1], day))
    return boundaries, segment_bounds


def _required_pit_date_symbols(klines: pl.DataFrame, archive_manifest: pl.DataFrame) -> set[tuple[str, str]]:
    """Archive-manifest (date, symbol) pairs that the klines are REQUIRED to cover for
    a full-PIT universe — i.e. only within each symbol's traded lifespan
    [first_kline_date, last_kline_date].

    The archive trade manifest (derived from the public *trade* archive) can list a coin
    OUTSIDE the span of its 1h *kline* archive at both ends:

    - PRE-LISTING (date < first kline): listing/announcement precedes the first trade bar;
      the trade-archive file is empty (0 trades), so no klines exist or can be built.
    - POST-DELISTING (date > last kline): an isolated EMPTY trade-archive file (0 trades —
      a settlement/marker artifact) can land weeks-to-months after the coin stopped
      trading. Verified on the FTX/Terra collapse cohort — FTT/SRM/RAY/LUNA/UST/ANC/GST/
      KEEP/BTT — all claimed on 2022-12-12, one date each, long after their real last bar;
      re-downloading those nine partitions returns Empty (0 rows) every time.

    Both archive-only cases are untradable — a coin with no trades on a date has no
    price/volume and cannot pass the universe turnover / >=20-bar filters, so it could
    never be selected. Requiring klines for them is a false tripwire, NOT survivorship
    protection.

    Reused tickers are split at every persisted v5 ``launchTime`` observed after
    their first stored kline. Bounds are then derived separately inside each
    incarnation, so an empty post-delisting/pre-relisting interval cannot be
    mistaken for a mid-history download hole.

    Survivorship IS preserved: any date within an incarnation's [first, last] —
    including a genuine mid-history gap where a real trading day's klines are
    simply missing (observed: FHEUSDT 2025-08-29..10-21, a 54-day
    archive-download gap, correctly still flagged and then backfilled) — is
    still required and will still trip the gate. A
    `bybit_v5_listing` pair is also required at or after the first observed kline even when
    it lies beyond the current kline tail: that provenance independently records a symbol
    reported `Trading` through the manifest build boundary. Inferring that upper boundary
    from the klines being validated would let an incomplete active-symbol tail redefine
    itself as a completed lifespan and false-pass."""
    bounds = _symbol_kline_date_bounds(klines)
    current_listing_pairs = _current_listing_derived_date_symbols(archive_manifest)
    incarnation_starts, segment_bounds = _incarnation_segment_bounds(
        klines,
        archive_manifest,
        bounds,
    )
    out: set[tuple[str, str]] = set()
    for d, s in _date_symbol_set(archive_manifest):
        starts = incarnation_starts.get(s)
        if starts is None:
            span = bounds.get(s)
        else:
            span = segment_bounds.get((s, bisect_right(starts, d)))
        if span is not None and span[0] <= d and (
            d <= span[1] or (d, s) in current_listing_pairs
        ):
            out.add((d, s))
    return out


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
    if klines.is_empty() or not _has_columns(klines, "date", "symbol"):
        return set()
    # Counts all rows, densified padding included: this is a data-presence gate
    # ("is the archive kline partition downloaded?"), not a liquidity gate, and
    # densify only runs on dates that have a real archive file. Counting only
    # real-volume bars would fail every symbol's genuine partial listing day.
    covered = (
        klines.group_by(["date", "symbol"])
        .agg(pl.len().alias("hourly_bars"))
        .filter(pl.col("hourly_bars") >= min_hourly_bars)
        .select(["date", "symbol"])
    )
    return _date_symbol_set(covered)
