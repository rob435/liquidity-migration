"""Point-in-time (PIT) membership + full-PIT universe validation.

This is the methodology-critical no-look-ahead / no-survivorship gate consumed
by the long/continuous engines, with shared frame helpers imported from
_common / trade_lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from ._common import _date_symbol_set, _symbol_set
from .archive_manifest import V5_LISTING_SOURCE, V5_LISTING_URL_SENTINEL
from .trade_lifecycle import _has_columns


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

    Survivorship IS preserved: any date within [first, last] — including a genuine
    mid-history gap where a real trading day's klines are simply missing (observed:
    FHEUSDT 2025-08-29..10-21, a 54-day archive-download gap, correctly still flagged and
    then backfilled) — is still required and will still trip the gate. A
    `bybit_v5_listing` pair is also required at or after the first observed kline even when
    it lies beyond the current kline tail: that provenance independently records a symbol
    reported `Trading` through the manifest build boundary. Inferring that upper boundary
    from the klines being validated would let an incomplete active-symbol tail redefine
    itself as a completed lifespan and false-pass."""
    bounds = _symbol_kline_date_bounds(klines)
    current_listing_pairs = _current_listing_derived_date_symbols(archive_manifest)
    out: set[tuple[str, str]] = set()
    for d, s in _date_symbol_set(archive_manifest):
        span = bounds.get(s)
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
    # Counts all rows, including densified padding -- intentionally. This is a
    # data-PRESENCE gate ("is the archive kline partition downloaded?"), not a
    # liquidity gate: densify only runs on dates that have a real archive file,
    # so a 24-row day means the data exists. Counting only real-volume bars
    # instead fails every symbol's genuine partial listing day (~1 per symbol),
    # which the strategy already excludes via its 30-day min-age filter -- so
    # that stricter count breaks the full-PIT gate for no real risk reduction.
    covered = (
        klines.group_by(["date", "symbol"])
        .agg(pl.len().alias("hourly_bars"))
        .filter(pl.col("hourly_bars") >= min_hourly_bars)
        .select(["date", "symbol"])
    )
    return _date_symbol_set(covered)
