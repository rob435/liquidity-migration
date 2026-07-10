"""Tests for conservative venue-local A0 identity maps."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from liquidity_migration.strategy_overhaul_instrument_map import (
    VENUE_LOCAL_MANIFEST_SCHEMA,
    VENUE_LOCAL_REVIEW_STATUS,
    VenueLocalInstrumentMapError,
    build_venue_local_instrument_map,
    derive_venue_local_instrument_map_from_roots,
    load_venue_local_manifest_projection,
)
from liquidity_migration.strategy_overhaul_phase0 import (
    REGISTERED_SLEEVE_WINDOWS,
    _build_instrument_map_coverage,
)


def _pairs() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "venue": venue,
                "symbol": symbol,
                "manifest_date": day,
            }
            for venue in ("bybit", "binance")
            for symbol in ("AAAUSDT", "BBBUSDT")
            for day in (dt.date(2026, 1, 1), dt.date(2026, 1, 2))
        ],
        schema=dict(VENUE_LOCAL_MANIFEST_SCHEMA),
    )


def _manifest_roots(tmp_path, *, days=("2026-01-01", "2026-01-02")) -> dict[str, object]:
    roots = {venue: tmp_path / venue for venue in ("bybit", "binance")}
    for venue, root in roots.items():
        for day in days:
            path = root / "archive_trade_manifest" / f"date={day}" / "part.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pl.DataFrame(
                {
                    "symbol": ["AAAUSDT", "AAAUSDT"],
                    "date": [day, day],
                    "url": [f"fixture://{venue}/{day}/one", f"fixture://{venue}/{day}/two"],
                    "source": [f"{venue}_archive", f"{venue}_archive"],
                    "outcome_sentinel": [1.0, -999.0],
                }
            ).write_parquet(path)
    return roots


def test_map_is_deterministic_complete_locally_and_never_claims_portability() -> None:
    first = build_venue_local_instrument_map(_pairs())
    second = build_venue_local_instrument_map(_pairs().reverse())

    assert first.version == second.version
    assert first.entries == second.entries
    assert first.receipt == second.receipt
    assert first.receipt["venue_local_identity_ready"] is True
    assert first.receipt["cross_venue_portability_ready"] is False
    assert all(entry.review_status == VENUE_LOCAL_REVIEW_STATUS for entry in first.entries)
    assert all(entry.canonical_instrument.startswith(entry.venue.upper()) for entry in first.entries)

    collapsed = {
        venue: [
            (symbol, day.isoformat())
            for symbol in ("AAAUSDT", "BBBUSDT")
            for day in (dt.date(2026, 1, 1), dt.date(2026, 1, 2))
        ]
        for venue in ("bybit", "binance")
    }
    report = _build_instrument_map_coverage(  # noqa: SLF001 - coverage integration
        collapsed,
        instrument_map=first.entries,
        instrument_map_version=first.version,
        instrument_map_authority="mechanically_derived_venue_local",
    )
    assert report["status"] == "complete"
    assert report["venue_local_identity_ready"] is True
    assert report["portable_matching_ready"] is False
    assert report["all_entries_cross_venue_reviewed"] is False


@pytest.mark.parametrize("malformation", ["duplicate", "non_usdt", "unknown_column"])
def test_invalid_manifest_identity_fails_closed(malformation: str) -> None:
    pairs = _pairs()
    if malformation == "duplicate":
        pairs = pl.concat([pairs, pairs.head(1)])
    elif malformation == "non_usdt":
        pairs = pairs.with_columns(
            pl.when(pl.int_range(pl.len()) == 0).then(pl.lit("AAAUSDC")).otherwise(pl.col("symbol")).alias("symbol")
        )
    else:
        pairs = pairs.with_columns(pl.lit(1).alias("outcome"))

    with pytest.raises(VenueLocalInstrumentMapError):
        build_venue_local_instrument_map(pairs)


def test_registered_windows_remain_unrelated_to_map_identity() -> None:
    # Guard against a future map generator accidentally deriving validity from
    # one sleeve's research window instead of observed per-symbol membership.
    result = build_venue_local_instrument_map(_pairs())
    assert len(REGISTERED_SLEEVE_WINDOWS) == 2
    assert all(entry.valid_from_date == "2026-01-01" for entry in result.entries)


def test_manifest_loader_binds_strict_collapsed_projection_without_outcomes(tmp_path) -> None:
    roots = _manifest_roots(tmp_path)

    projection = load_venue_local_manifest_projection(
        roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
    )
    derived = derive_venue_local_instrument_map_from_roots(
        roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
    )

    assert projection.rows.height == 4
    assert projection.receipt["source_projection_row_count"] == 4
    assert projection.receipt["registered_window_complete"] is True
    assert projection.receipt["outcome_values_read"] is False
    assert "outcome_sentinel" not in projection.receipt["storage_validation_columns_read"]
    for venue in ("bybit", "binance"):
        receipt = projection.receipt["venues"][venue]
        assert receipt["storage_row_count_in_window"] == 4
        assert receipt["source_projection_row_count"] == 2
        assert receipt["storage_key_audit"]["status"] == "passed"
    assert len(derived.entries) == 2
    assert derived.receipt["venue_local_identity_ready"] is True
    assert derived.receipt["cross_venue_portability_ready"] is False
    assert derived.receipt["source_projection_identity_sha256"]
    assert "-source-" in derived.version

    before = dict(derived.receipt)
    for root in roots.values():
        path = root / "archive_trade_manifest" / "date=2026-01-01" / "part.parquet"
        pl.read_parquet(path).with_columns((pl.col("outcome_sentinel") * 123).alias("outcome_sentinel")).write_parquet(
            path
        )
    after = derive_venue_local_instrument_map_from_roots(
        roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
    )
    assert dict(after.receipt) == before


def test_manifest_loader_rejects_storage_and_partition_malformations(tmp_path) -> None:
    roots = _manifest_roots(tmp_path)
    path = roots["bybit"] / "archive_trade_manifest" / "date=2026-01-01" / "part.parquet"
    frame = pl.read_parquet(path)
    pl.concat([frame, frame.head(1)]).write_parquet(path)
    with pytest.raises(VenueLocalInstrumentMapError, match="duplicate key"):
        load_venue_local_manifest_projection(
            roots,
            start_date="2026-01-01",
            end_date_exclusive="2026-01-03",
        )

    _manifest_roots(tmp_path)
    path = roots["bybit"] / "archive_trade_manifest" / "date=2026-01-01" / "part.parquet"
    pl.read_parquet(path).with_columns(pl.lit("2025-12-31").alias("date")).write_parquet(path)
    with pytest.raises(VenueLocalInstrumentMapError, match="disagrees with date=2026-01-01"):
        load_venue_local_manifest_projection(
            roots,
            start_date="2026-01-01",
            end_date_exclusive="2026-01-03",
        )


def test_manifest_loader_rejects_normalized_duplicate_venues_and_symlinks(tmp_path) -> None:
    roots = _manifest_roots(tmp_path)
    with pytest.raises(VenueLocalInstrumentMapError, match="duplicate venue after normalization"):
        load_venue_local_manifest_projection(
            {**roots, "BYBIT": roots["bybit"]},
            start_date="2026-01-01",
            end_date_exclusive="2026-01-03",
        )

    with pytest.raises(VenueLocalInstrumentMapError, match="same physical directory"):
        load_venue_local_manifest_projection(
            {"bybit": roots["bybit"], "binance": roots["bybit"]},
            start_date="2026-01-01",
            end_date_exclusive="2026-01-03",
        )

    path = roots["bybit"] / "archive_trade_manifest" / "date=2026-01-01" / "part.parquet"
    target = tmp_path / "symlink-target.parquet"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(VenueLocalInstrumentMapError, match="non-symlink"):
        load_venue_local_manifest_projection(
            roots,
            start_date="2026-01-01",
            end_date_exclusive="2026-01-03",
        )


def test_manifest_loader_ignores_files_outside_window_but_rejects_mixed_layout(tmp_path) -> None:
    roots = _manifest_roots(tmp_path)
    outside = roots["bybit"] / "archive_trade_manifest" / "date=2025-12-31" / "broken.parquet"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not parquet")

    projection = load_venue_local_manifest_projection(
        roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
    )
    assert projection.receipt["registered_window_complete"] is True

    direct = roots["bybit"] / "archive_trade_manifest" / "direct.parquet"
    pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "date": ["2026-01-01"],
            "url": ["fixture://mixed-layout"],
            "source": ["fixture"],
        }
    ).write_parquet(direct)
    with pytest.raises(VenueLocalInstrumentMapError, match="mixed date-partitioned and unpartitioned"):
        load_venue_local_manifest_projection(
            roots,
            start_date="2026-01-01",
            end_date_exclusive="2026-01-03",
        )


def test_incomplete_window_never_marks_auto_map_locally_ready(tmp_path) -> None:
    roots = _manifest_roots(tmp_path, days=("2026-01-01",))

    derived = derive_venue_local_instrument_map_from_roots(
        roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
    )

    assert derived.receipt["source_registered_window_complete"] is False
    assert derived.receipt["venue_local_identity_ready"] is False
    assert derived.receipt["cross_venue_portability_ready"] is False


def test_empty_manifest_roots_return_an_explicit_unready_projection(tmp_path) -> None:
    roots = {venue: tmp_path / venue for venue in ("bybit", "binance")}
    for root in roots.values():
        root.mkdir()

    derived = derive_venue_local_instrument_map_from_roots(
        roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
    )

    assert derived.entries == ()
    assert derived.receipt["source_projection_row_count"] == 0
    assert derived.receipt["source_registered_window_complete"] is False
    assert derived.receipt["venue_local_identity_ready"] is False


def test_membership_calendar_change_updates_source_bound_map_version(tmp_path) -> None:
    roots = _manifest_roots(tmp_path)
    before = derive_venue_local_instrument_map_from_roots(
        roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
    )
    for venue, root in roots.items():
        path = root / "archive_trade_manifest" / "date=2026-01-02" / "part.parquet"
        frame = pl.read_parquet(path).with_columns(
            pl.lit("BBBUSDT").alias("symbol"),
            pl.col("url").str.replace("/2026-01-02/", f"/2026-01-02/{venue}-changed-").alias("url"),
        )
        frame.write_parquet(path)
    after = derive_venue_local_instrument_map_from_roots(
        roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
    )

    assert [entry.valid_from_date for entry in before.entries] == ["2026-01-01", "2026-01-01"]
    assert after.receipt["source_projection_row_count"] == before.receipt["source_projection_row_count"]
    assert after.version != before.version
    assert after.receipt["source_projection_identity_sha256"] != before.receipt["source_projection_identity_sha256"]
