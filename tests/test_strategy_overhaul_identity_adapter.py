"""Focused tests for the outcome-blind S02 identity/PIT adapter."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.strategy_overhaul_identity_adapter import (
    COMMON_IDENTITY_COLUMNS,
    CONTINUOUS_FEATURE_KEY_COLUMNS,
    IDENTITY_NULL_SEMANTICS,
    LONG_FEATURE_KEY_COLUMNS,
    MANIFEST_PAIR_COLUMNS,
    S02IdentityAdapterError,
    annotate_continuous_s02_identity,
    annotate_long_s02_identity,
)
from liquidity_migration.strategy_overhaul_phase0 import InstrumentMapEntry

UTC = dt.timezone.utc


def _ms(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _manifest(
    manifest_date: dt.date,
    *,
    symbol: str = "AAAUSDT",
    venue: str = "bybit",
    membership_source: str | None = "bybit_public_trading_archive",
    membership_inferred: bool | None = False,
    first_archive_observed_date: dt.date | None = dt.date(2025, 1, 1),
    reported_launch_time_ms: int | None = None,
    root_first_bar_ts_ms: int | None = None,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "venue": [venue],
            "symbol": [symbol],
            "manifest_date": [manifest_date],
            "membership_source": [membership_source],
            "membership_inferred": [membership_inferred],
            "first_archive_observed_date": [first_archive_observed_date],
            "reported_launch_time_ms": [
                reported_launch_time_ms if reported_launch_time_ms is not None else _ms("2025-01-01T00:00:00+00:00")
            ],
            "root_first_bar_ts_ms": [
                root_first_bar_ts_ms if root_first_bar_ts_ms is not None else _ms("2025-02-01T00:00:00+00:00")
            ],
            "provenance_limitation": ["archive coverage is not universal tradability"],
            "coverage_state": ["manifest_and_kline_pair_covered"],
        },
        schema={
            "venue": pl.String,
            "symbol": pl.String,
            "manifest_date": pl.Date,
            "membership_source": pl.String,
            "membership_inferred": pl.Boolean,
            "first_archive_observed_date": pl.Date,
            "reported_launch_time_ms": pl.Int64,
            "root_first_bar_ts_ms": pl.Int64,
            "provenance_limitation": pl.String,
            "coverage_state": pl.String,
        },
    )


def _null_anchor_manifest(manifest_date: dt.date) -> pl.DataFrame:
    frame = _manifest(manifest_date)
    return frame.with_columns(
        pl.lit(None, dtype=pl.String).alias("membership_source"),
        pl.lit(None, dtype=pl.Boolean).alias("membership_inferred"),
        pl.lit(None, dtype=pl.Date).alias("first_archive_observed_date"),
        pl.lit(None, dtype=pl.Int64).alias("reported_launch_time_ms"),
        pl.lit(None, dtype=pl.Int64).alias("root_first_bar_ts_ms"),
    )


def _map(
    *,
    symbol: str = "AAAUSDT",
    venue: str = "bybit",
    canonical: str = "AAA-USDT-LINEAR-PERP",
    start: str = "2025-01-01",
    end: str | None = None,
) -> InstrumentMapEntry:
    return InstrumentMapEntry(
        canonical_instrument=canonical,
        venue=venue,
        symbol=symbol,
        valid_from_date=start,
        valid_to_date_exclusive=end,
        base_asset="AAA",
        quote_asset="USDT",
        settlement_asset="USDT",
        contract_type="linear_perpetual",
        contract_multiplier=1.0,
        mapping_source="reviewed official contract metadata",
        review_status="reviewed",
    )


def _continuous_feature(
    signal_ts_ms: int | None = None,
    *,
    close: float = 1.25,
) -> pl.DataFrame:
    signal = signal_ts_ms or _ms("2026-01-01T23:00:00+00:00")
    decision = signal + MS_PER_HOUR
    return pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "signal_ts_ms": [signal],
            "decision_ts_ms": [decision],
            "signal_bar_close_ts_ms": [decision],
            "feature_data_available_ts_ms": [decision],
            "data_available_ts_ms": [decision],
            "close": [close],
        },
        schema={
            "symbol": pl.String,
            "signal_ts_ms": pl.Int64,
            "decision_ts_ms": pl.Int64,
            "signal_bar_close_ts_ms": pl.Int64,
            "feature_data_available_ts_ms": pl.Int64,
            "data_available_ts_ms": pl.Int64,
            "close": pl.Float64,
        },
    )


def _long_feature(
    signal_ts_ms: int | None = None,
    *,
    close: float = 1.25,
) -> pl.DataFrame:
    signal = signal_ts_ms or _ms("2026-01-02T00:00:00+00:00")
    return pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "signal_ts_ms": [signal],
            "symbol_age_days": [101],
            "close": [close],
        },
        schema={
            "symbol": pl.String,
            "signal_ts_ms": pl.Int64,
            "symbol_age_days": pl.Int64,
            "close": pl.Float64,
        },
    )


def _annotate_continuous(
    feature: pl.DataFrame,
    manifest: pl.DataFrame,
    instrument_map: list[InstrumentMapEntry] | None = None,
) -> pl.DataFrame:
    return annotate_continuous_s02_identity(
        feature,
        venue="bybit",
        manifest_pairs=manifest,
        instrument_map=instrument_map or [_map()],
        instrument_map_version="reviewed-map-v1",
        feature_payload_allowlist=("close",),
        current_age_source="root_first_bar_ts_ms",
    )


def _annotate_long(
    feature: pl.DataFrame,
    manifest: pl.DataFrame,
    instrument_map: list[InstrumentMapEntry] | None = None,
) -> pl.DataFrame:
    return annotate_long_s02_identity(
        feature,
        venue="bybit",
        manifest_pairs=manifest,
        instrument_map=instrument_map or [_map()],
        instrument_map_version="reviewed-map-v1",
        feature_payload_allowlist=("close",),
    )


def test_continuous_uses_kline_stamp_date_across_midnight_and_preserves_payload() -> None:
    feature = _continuous_feature()
    out = _annotate_continuous(feature, _manifest(dt.date(2026, 1, 1)))

    assert out.height == feature.height == 1
    assert out["manifest_date"].to_list() == [dt.date(2026, 1, 1)]
    assert out["decision_ts_ms"].item() == _ms("2026-01-02T00:00:00+00:00")
    assert out["canonical_instrument_id"].item() == "AAA-USDT-LINEAR-PERP"
    assert out["close"].item() == feature["close"].item()
    assert out["age_days_reported_launch"].item() == pytest.approx(366.0)
    assert out["age_days_root_first_bar"].item() == pytest.approx(335.0)
    assert out["current_age_source"].item() == "root_first_bar_ts_ms"
    assert out["current_age_source_available"].item() is True
    assert out["current_age_240_pass"].item() is True
    assert out.columns == [
        "venue",
        "canonical_instrument_id",
        *CONTINUOUS_FEATURE_KEY_COLUMNS,
        *COMMON_IDENTITY_COLUMNS,
        "current_age_source",
        "current_age_source_available",
        "current_age_240_pass",
        "close",
    ]


def test_long_uses_signal_minus_one_ms_date_and_preserves_canonical_key() -> None:
    feature = _long_feature()
    out = _annotate_long(feature, _manifest(dt.date(2026, 1, 1)))

    assert out.height == 1
    assert out["signal_ts_ms"].item() == feature["signal_ts_ms"].item()
    assert out["manifest_date"].item() == dt.date(2026, 1, 1)
    assert out["symbol_age_days"].item() == 101
    assert out["symbol_age_source"].item() == "loaded_root_first_daily_row_plus_one"
    assert out.columns == [
        "venue",
        "canonical_instrument_id",
        "symbol",
        "signal_ts_ms",
        *COMMON_IDENTITY_COLUMNS,
        "symbol_age_days",
        "symbol_age_source",
        "close",
    ]


def test_payload_allowlist_uses_post_builder_field_names_not_derivation_sources() -> None:
    feature = _long_feature().with_columns(pl.lit(0.25).alias("simple_return_1d"))
    out = annotate_long_s02_identity(
        feature,
        venue="bybit",
        manifest_pairs=_manifest(dt.date(2026, 1, 1)),
        instrument_map=[_map()],
        instrument_map_version="reviewed-map-v1",
        feature_payload_allowlist=("close", "simple_return_1d"),
    )

    assert out["simple_return_1d"].item() == pytest.approx(0.25)


def test_unknown_provenance_and_missing_age_anchors_remain_unknown() -> None:
    out = annotate_continuous_s02_identity(
        _continuous_feature(),
        venue="bybit",
        manifest_pairs=_null_anchor_manifest(dt.date(2026, 1, 1)),
        instrument_map=[_map()],
        instrument_map_version="reviewed-map-v1",
        feature_payload_allowlist=("close",),
        current_age_source="reported_launch_time_ms",
    )

    row = out.row(0, named=True)
    assert row["membership_source"] is None
    assert row["membership_inferred"] is None
    assert row["first_archive_observed_date"] is None
    assert row["reported_launch_time_ms"] is None
    assert row["root_first_bar_ts_ms"] is None
    assert row["age_days_reported_launch"] is None
    assert row["age_days_root_first_bar"] is None
    assert row["current_age_source_available"] is False
    assert row["current_age_240_pass"] is None
    assert "does not prove absence" in IDENTITY_NULL_SEMANTICS["first_archive_observed_date"]


@pytest.mark.parametrize("declare_unknown", [False, True])
def test_feature_projection_refuses_every_unknown_or_outcome_column(
    declare_unknown: bool,
) -> None:
    feature = _continuous_feature().with_columns(pl.lit(0.42).alias("future_return_72h"))
    allowlist = ("close", "future_return_72h") if declare_unknown else ("close",)
    with pytest.raises(
        S02IdentityAdapterError,
        match="non-registered S02|projection mismatch",
    ):
        annotate_continuous_s02_identity(
            feature,
            venue="bybit",
            manifest_pairs=_manifest(dt.date(2026, 1, 1)),
            instrument_map=[_map()],
            instrument_map_version="reviewed-map-v1",
            feature_payload_allowlist=allowlist,
            current_age_source="root_first_bar_ts_ms",
        )


def test_manifest_projection_refuses_storage_or_value_passthrough_columns() -> None:
    manifest = _manifest(dt.date(2026, 1, 1)).with_columns(
        pl.lit("archive-url").alias("url"),
        pl.lit(123.0).alias("close"),
    )
    with pytest.raises(S02IdentityAdapterError, match="projection mismatch"):
        _annotate_continuous(_continuous_feature(), manifest)


def test_manifest_coverage_state_is_a_frozen_enum() -> None:
    manifest = _manifest(dt.date(2026, 1, 1)).with_columns(pl.lit("caller_claimed_complete").alias("coverage_state"))
    with pytest.raises(S02IdentityAdapterError, match="coverage"):
        _annotate_continuous(_continuous_feature(), manifest)


def test_missing_and_duplicate_pit_rows_fail_closed() -> None:
    missing = _manifest(dt.date(2026, 1, 2))
    with pytest.raises(S02IdentityAdapterError, match="PIT manifest row is missing"):
        _annotate_continuous(_continuous_feature(), missing)

    duplicate = pl.concat([_manifest(dt.date(2026, 1, 1)), _manifest(dt.date(2026, 1, 1))])
    with pytest.raises(S02IdentityAdapterError, match="ambiguous duplicate"):
        _annotate_continuous(_continuous_feature(), duplicate)


def test_map_must_cover_every_row_and_must_not_overlap() -> None:
    expired = _map(end="2025-12-31")
    with pytest.raises(S02IdentityAdapterError, match="missing an active reviewed mapping"):
        _annotate_continuous(
            _continuous_feature(),
            _manifest(dt.date(2026, 1, 1)),
            [expired],
        )

    first = _map(end="2026-06-01")
    overlap = _map(start="2026-01-01", canonical="AAA-USDT-LINEAR-PERP-V2")
    with pytest.raises(S02IdentityAdapterError, match="ambiguous overlapping intervals"):
        _annotate_continuous(
            _continuous_feature(),
            _manifest(dt.date(2026, 1, 1)),
            [first, overlap],
        )

    alias_collision = _map(
        symbol="BBBUSDT",
        canonical="AAA-USDT-LINEAR-PERP",
    )
    with pytest.raises(S02IdentityAdapterError, match="canonical alias collision"):
        _annotate_continuous(
            _continuous_feature(),
            _manifest(dt.date(2026, 1, 1)),
            [_map(), alias_collision],
        )


def test_map_refuses_unreviewed_or_non_typed_rows() -> None:
    with pytest.raises(S02IdentityAdapterError, match="InstrumentMapEntry"):
        annotate_long_s02_identity(
            _long_feature(),
            venue="bybit",
            manifest_pairs=_manifest(dt.date(2026, 1, 1)),
            instrument_map=[{"symbol": "AAAUSDT"}],  # type: ignore[list-item]
            instrument_map_version="reviewed-map-v1",
            feature_payload_allowlist=("close",),
        )

    with pytest.raises(ValueError, match="review_status"):
        replace(_map(), review_status="draft")


def test_feature_timing_and_availability_semantics_fail_closed() -> None:
    feature = _continuous_feature().with_columns((pl.col("decision_ts_ms") + 1).alias("data_available_ts_ms"))
    with pytest.raises(S02IdentityAdapterError, match="availability semantics"):
        _annotate_continuous(feature, _manifest(dt.date(2026, 1, 1)))

    off_midnight = _long_feature().with_columns((pl.col("signal_ts_ms") + MS_PER_HOUR).alias("signal_ts_ms"))
    with pytest.raises(S02IdentityAdapterError, match="daily-close"):
        _annotate_long(off_midnight, _manifest(dt.date(2026, 1, 1)))


def test_future_age_anchor_is_rejected_instead_of_emitting_negative_age() -> None:
    future = _manifest(
        dt.date(2026, 1, 1),
        root_first_bar_ts_ms=_ms("2026-01-03T00:00:00+00:00"),
    )
    with pytest.raises(S02IdentityAdapterError, match="after the S02 decision"):
        _annotate_continuous(_continuous_feature(), future)


def test_payload_values_cannot_change_identity_or_pit_annotations() -> None:
    manifest = _manifest(dt.date(2026, 1, 1))
    first = _annotate_continuous(_continuous_feature(close=1.0), manifest)
    second = _annotate_continuous(_continuous_feature(close=99_999.0), manifest)

    assert first.drop("close").equals(second.drop("close"))
    assert first["close"].item() != second["close"].item()


def test_row_count_order_and_key_uniqueness_are_preserved() -> None:
    first = _continuous_feature(_ms("2026-01-01T22:00:00+00:00"), close=2.0)
    second = _continuous_feature(_ms("2026-01-01T23:00:00+00:00"), close=3.0)
    feature = pl.concat([second, first])
    manifest = pl.concat(
        [
            _manifest(dt.date(2026, 1, 1)),
        ]
    )
    out = _annotate_continuous(feature, manifest)

    assert out.height == feature.height
    assert out["signal_ts_ms"].to_list() == feature["signal_ts_ms"].to_list()
    assert out["close"].to_list() == feature["close"].to_list()
    assert out.select("venue", "symbol", "decision_ts_ms").n_unique() == out.height


def test_public_input_contracts_are_exact_and_do_not_include_outcomes() -> None:
    assert MANIFEST_PAIR_COLUMNS == (
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
    assert LONG_FEATURE_KEY_COLUMNS == ("symbol", "signal_ts_ms", "symbol_age_days")
    assert not {
        name
        for name in (*MANIFEST_PAIR_COLUMNS, *COMMON_IDENTITY_COLUMNS)
        if any(token in name for token in ("path", "mfe", "mae", "pnl", "entry_price"))
    }
