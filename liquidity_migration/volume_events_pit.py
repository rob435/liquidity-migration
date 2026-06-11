"""Point-in-time (PIT) membership + full-PIT universe validation for the volume-events
backtest engine — the methodology-critical no-look-ahead / no-survivorship gate, isolated
into one heavily-tested module. Re-exported by volume_events (the hub), mirroring how
volume_events_filters / _validation / _features / _charts are split out: the shared frame
helpers stay in the hub and are imported here, and volume_events re-imports these funcs at
its bottom (after the helpers are defined) — the package always loads volume_events first."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import polars as pl

from ._common import trading_day_expr

from ._common import _date_symbol_set, _symbol_set
from .trade_lifecycle import _has_columns


def _pit_membership_pass(trades: pl.DataFrame, *, required: bool) -> bool:
    if not required:
        return True
    if trades.is_empty() or "tradable_membership_flag" not in trades.columns:
        return False
    flags = trades["tradable_membership_flag"].to_list()
    return bool(flags) and all(bool(flag) for flag in flags)


def _pit_manifest_metadata(
    archive_manifest: pl.DataFrame,
    features: pl.DataFrame,
    klines: pl.DataFrame,
    *,
    full_pit_universe_pass: bool | None = None,
    kline_covered_date_symbols: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    # The covered-set group_by and the full-PIT pass are expensive over the multi-GB
    # klines; the caller (run_volume_event_research / long_native) computes them ONCE and
    # threads them in so they are not recomputed here (BAC-1). Both default to recompute
    # so every other call site stays behaviourally identical.
    manifest_symbols = _symbol_set(archive_manifest)
    feature_symbols = _symbol_set(features)
    manifest_date_symbols = _date_symbol_set(archive_manifest)
    if kline_covered_date_symbols is None:
        kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
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

    Both are untradable — a coin with no trades on a date has no price/volume and cannot
    pass the universe turnover / >=20-bar filters, so it could never be selected. Requiring
    klines for them is a false tripwire, NOT survivorship protection.

    Survivorship IS preserved: any date within [first, last] — including a genuine
    mid-history gap where a real trading day's klines are simply missing (observed:
    FHEUSDT 2025-08-29..10-21, a 54-day archive-download gap, correctly still flagged and
    then backfilled) — is still required and will still trip the gate. This relies on the
    pipeline invariant that the kline downloader is run over the FULL archive manifest, so
    `last_kline_date` is the coin's true last real trading day (a real post-last trading
    day would have been downloaded and would extend `last_kline_date`; only empty-file days
    fall beyond it). `last_kline_date` is end-of-data for every still-active coin, so this
    only ever drops empty-file phantom claims, never an active coin's recent dates."""
    bounds = _symbol_kline_date_bounds(klines)
    out: set[tuple[str, str]] = set()
    for d, s in _date_symbol_set(archive_manifest):
        span = bounds.get(s)
        if span is not None and span[0] <= d <= span[1]:
            out.add((d, s))
    return out


def _full_pit_universe_pass(
    klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
    *,
    kline_covered_date_symbols: set[tuple[str, str]] | None = None,
) -> bool:
    manifest_symbols = _symbol_set(archive_manifest)
    kline_symbols = _symbol_set(klines)
    required_date_symbols = _required_pit_date_symbols(klines, archive_manifest)
    if kline_covered_date_symbols is None:
        kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    return (
        bool(manifest_symbols)
        and manifest_symbols.issubset(kline_symbols)
        and bool(required_date_symbols)
        and required_date_symbols.issubset(kline_covered_date_symbols)
    )


def _full_pit_universe_error(
    klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
    *,
    kline_covered_date_symbols: set[tuple[str, str]] | None = None,
) -> str:
    manifest_symbols = _symbol_set(archive_manifest)
    kline_symbols = _symbol_set(klines)
    missing_symbols = sorted(manifest_symbols - kline_symbols)
    required_date_symbols = _required_pit_date_symbols(klines, archive_manifest)
    if kline_covered_date_symbols is None:
        kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    missing_date_symbols = sorted(required_date_symbols - kline_covered_date_symbols)
    if not manifest_symbols:
        return (
            "volume-events requires full PIT archive membership by default, but archive_trade_manifest is empty. "
            "Run archive-manifest and archive-download-klines-1h first, or pass --allow-partial-pit only for explicitly biased diagnostics."
        )
    return (
        "volume-events requires a full PIT universe by default, but klines_1h does not cover every archive manifest symbol/date. "
        f"manifest_symbols={len(manifest_symbols)} kline_symbols={len(kline_symbols)} missing_symbols={len(missing_symbols)} "
        f"required_date_symbols={len(required_date_symbols)} kline_covered_date_symbols={len(kline_covered_date_symbols)} "
        f"missing_date_symbols={len(missing_date_symbols)} missing_symbol_sample={missing_symbols[:20]} "
        f"missing_date_symbol_sample={missing_date_symbols[:20]} (note: pre-listing manifest entries before a symbol's first kline are NOT required — these are genuine gaps from a symbol's first traded day onward). Finish archive-download-klines-1h before running real event backtests."
    )


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


def _pit_membership_diagnostic(
    data_root: str | Path,
    archive_manifest: pl.DataFrame,
    features: pl.DataFrame,
) -> dict[str, Any]:
    """Actionable detail for a `pit_membership_fail`: how far the archive manifest
    lags the signal universe, and the count of feature rows whose TRADING DAY (the
    date of `ts_ms - 1ms`; see `_attach_event_archive_membership`) falls past the
    manifest's coverage end — the "uncovered tail" that the strict gate rejects.

    Cheap: manifest end is read from the data-root's `date=` partitions (no parquet
    read); the uncovered-tail count is a single polars expression over `features`.
    """
    from .pit_coverage import coverage_status

    manifest_end = coverage_status(data_root).manifest_end
    uncovered_tail = 0
    if manifest_end is not None and not features.is_empty() and "ts_ms" in features.columns:
        # Trading day = date of (ts_ms - 1ms); count distinct (symbol, trading day)
        # event rows whose trading day is strictly after the manifest coverage end.
        trading_day = trading_day_expr("ts_ms")
        uncovered_tail = int(
            features.select(trading_day.alias("_td"))
            .filter(pl.col("_td") > pl.lit(manifest_end))
            .height
        )
    return {
        "manifest_end": manifest_end.isoformat() if manifest_end is not None else None,
        "uncovered_tail_event_rows": uncovered_tail,
    }


def _pit_membership_diagnostic_lines(diagnostic: dict[str, Any]) -> list[str]:
    """Concise, actionable remediation block for a membership rejection."""
    manifest_end = diagnostic.get("manifest_end")
    uncovered = diagnostic.get("uncovered_tail_event_rows", 0)
    return [
        "⚠️  pit_membership_fail — the archive manifest does not cover every traded signal day.",
        f"     archive manifest coverage end : {manifest_end if manifest_end is not None else 'MISSING'}",
        f"     event rows past coverage end  : {uncovered} (trading day = date of ts_ms-1ms; the uncovered tail)",
        "     FIX: run `archive-manifest` to refresh PIT membership, or pass "
        "`--pit-membership current-universe` for a labeled biased same-day diagnostic (never promotion evidence).",
    ]
