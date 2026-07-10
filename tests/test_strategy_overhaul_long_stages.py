"""Focused synthetic tests for exact LONG-A0 S03/S04 stage boundaries."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native_event_demo import _v11a_long_native_config
from liquidity_migration.long_population_scout import build_long_feature_tape
from liquidity_migration.strategy_overhaul_long_s02 import HOURLY_BAR_SCHEMA
from liquidity_migration.strategy_overhaul_long_stages import (
    LongStageBoundaryError,
    build_long_s03_entry_policy,
    build_long_s04_path_labels,
)
from liquidity_migration.strategy_overhaul_projection import (
    artifact_polars_schema,
    empty_artifact_frame,
    project_artifact_frame,
)
from liquidity_migration.strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    LONG_ENTRY_SCHEMA_ID,
    LONG_LABEL_SCHEMA_ID,
    LONG_SIGNAL_SCHEMA_ID,
)


SIGNAL_TS_MS = 20 * MS_PER_DAY
SYMBOL = "AAAUSDT"
CANONICAL_ID = "AAA-USDT-LINEAR-PERP"


def _hourly(
    *,
    symbol: str = SYMBOL,
    close_by_hour: dict[int, float] | None = None,
    through_hour: int = 85,
) -> pl.DataFrame:
    close_by_hour = close_by_hour or {
        0: 100.0,
        1: 100.0,
        2: 99.4,
        3: 98.9,
        4: 97.9,
        7: 101.5,
    }
    rows: list[dict[str, object]] = []
    for hour in range(through_hour + 1):
        close = close_by_hour.get(hour, 100.0)
        rows.append(
            {
                # ``hour`` denotes the bar-end offset; source ts_ms is bar open.
                "symbol": symbol,
                "ts_ms": SIGNAL_TS_MS + (hour - 1) * MS_PER_HOUR,
                "open": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
            }
        )
    return pl.DataFrame(rows, schema=dict(HOURLY_BAR_SCHEMA))


def _raw_feature(*, symbol: str = SYMBOL) -> dict[str, object]:
    return {
        "venue": "bybit",
        "symbol": symbol,
        "canonical_instrument_id": CANONICAL_ID,
        "ts_ms": SIGNAL_TS_MS,
        "close": 100.0,
        "in_universe": True,
        "regime_on": True,
        "eth_regime_on": True,
        "today_volume_rank": 1.0,
        "log_return": 0.20,
        "sigma_daily_30d": 0.04,
        "close_location": 0.80,
        "pump_3d_log": 0.30,
        "close_loc_3d": 0.80,
        "pump_7d_log": 0.40,
        "close_loc_7d": 0.80,
        "atr_14d_pct": 0.04,
    }


def _nonnull_fixture_value(dtype: pl.DataType) -> object:
    if dtype == pl.Boolean:
        return False
    if dtype == pl.Date:
        return dt.date(2026, 1, 1)
    if dtype == pl.Float64:
        return 1.0
    if dtype in {pl.Int64, pl.Int8, pl.UInt32}:
        return 1
    if dtype == pl.String:
        return "fixture"
    if dtype == pl.List(pl.String):
        return []
    raise AssertionError(f"unhandled synthetic dtype: {dtype}")


def _exact_s02(
    hourly: pl.DataFrame,
    *,
    symbol: str = SYMBOL,
    canonical_id: str = CANONICAL_ID,
    venue: str = "bybit",
) -> pl.DataFrame:
    """Lift valid broad signal geometry into a complete exact S02 fixture."""

    broad = build_long_feature_tape(
        pl.DataFrame([_raw_feature(symbol=symbol)]),
        hourly,
        _v11a_long_native_config(),
    ).row(0, named=True)
    artifact = ARTIFACT_SCHEMAS[LONG_SIGNAL_SCHEMA_ID]
    schema = artifact_polars_schema(LONG_SIGNAL_SCHEMA_ID)
    row = {
        field.name: (None if field.nullable else _nonnull_fixture_value(schema[field.name]))
        for field in artifact.fields
    }
    row.update(
        {
            "venue": venue,
            "symbol": symbol,
            "signal_ts_ms": SIGNAL_TS_MS,
            "canonical_instrument_id": canonical_id,
            "atr_14d_pct": broad["atr_14d_pct"],
            "signal_bar_present": broad["signal_bar_present"],
            "signal_bar_complete": broad["signal_bar_complete"],
            "signal_close_hourly": broad["signal_close_hourly"],
            "classifier_selected": broad["classifier_selected"],
            "fc_exit_stop_pct": broad["fc_exit_stop_pct"],
            "fc_exit_take_profit_pct": broad["fc_exit_take_profit_pct"],
            "fc_exit_max_hold_hours": broad["fc_exit_max_hold_hours"],
            "long_feature_tape_schema_version": broad["long_feature_tape_schema_version"],
        }
    )
    return project_artifact_frame(
        pl.DataFrame([row], schema=dict(schema)),
        LONG_SIGNAL_SCHEMA_ID,
    )


def _s03(
    s02: pl.DataFrame,
    hourly: pl.DataFrame,
) -> pl.DataFrame:
    return build_long_s03_entry_policy(
        s02,
        hourly,
        config=_v11a_long_native_config(),
    )


def _s04(
    s02: pl.DataFrame,
    s03: pl.DataFrame,
    hourly: pl.DataFrame,
) -> pl.DataFrame:
    return build_long_s04_path_labels(
        s02,
        s03,
        hourly,
        config=_v11a_long_native_config(),
    )


def test_s03_is_exact_separate_and_contains_no_path_outcomes() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)
    output = _s03(s02, hourly)
    expected = artifact_polars_schema(LONG_ENTRY_SCHEMA_ID)

    assert output.columns == list(expected)
    assert dict(output.schema) == dict(expected)
    assert output.height == 1
    assert len(output.columns) == 30
    assert output.select("venue", "symbol", "signal_ts_ms").row(0) == (
        "bybit",
        SYMBOL,
        SIGNAL_TS_MS,
    )
    assert output["canonical_instrument_id"].item() == CANONICAL_ID
    assert output["common_entry_hour"].item() == 1
    assert output["current_entry_hour"].item() == 3
    assert output["current_entry_price"].item() == pytest.approx(98.9)
    assert output["long_entry_policy_schema_version"].item() == ("long_a0_entry_policy_v1")
    assert not {
        column
        for column in output.columns
        if "point_return" in column or "mfe" in column or "mae" in column or "adverse_magnitude" in column
    }
    assert "atr_14d_pct" not in output.columns
    assert "signal_close_hourly" not in output.columns


def test_s04_reconstructs_s03_then_emits_only_frozen_exact_labels() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)
    s03 = _s03(s02, hourly)
    output = _s04(s02, s03, hourly)
    expected = artifact_polars_schema(LONG_LABEL_SCHEMA_ID)
    row = output.row(0, named=True)

    assert output.columns == list(expected)
    assert dict(output.schema) == dict(expected)
    assert len(output.columns) == 71
    assert row["venue"] == "bybit"
    assert row["canonical_instrument_id"] == CANONICAL_ID
    assert row["common_1h_point_return"] == pytest.approx(99.4 / 100.0 - 1.0)
    assert row["current_1h_point_return"] == pytest.approx(97.9 / 98.9 - 1.0)
    assert row["common_stop_price"] == pytest.approx(100.0 * 0.94)
    assert row["current_take_profit_price"] == pytest.approx(98.9 * 1.16)
    assert row["common_72h_path_complete"] is True
    assert row["current_72h_path_complete"] is True
    assert row["long_label_schema_version"] == "long_a0_minimal_labels_v1"
    assert row["long_label_point_horizons"] == "1|24|72"
    assert row["long_label_excursion_horizons"] == "24|72"
    assert "atr_14d_pct" not in output.columns
    assert "common_entry_price" not in output.columns


def test_s03_and_s04_typed_empty_outputs_are_materializable() -> None:
    empty_s02 = empty_artifact_frame(LONG_SIGNAL_SCHEMA_ID)
    empty_s03 = empty_artifact_frame(LONG_ENTRY_SCHEMA_ID)
    empty_hourly = pl.DataFrame(schema=dict(HOURLY_BAR_SCHEMA))

    built_s03 = _s03(empty_s02, empty_hourly)
    built_s04 = _s04(empty_s02, empty_s03, empty_hourly)

    assert built_s03.is_empty()
    assert built_s03.schema == dict(artifact_polars_schema(LONG_ENTRY_SCHEMA_ID))
    assert built_s04.is_empty()
    assert built_s04.schema == dict(artifact_polars_schema(LONG_LABEL_SCHEMA_ID))


def test_stage_inputs_require_exact_names_dtypes_nullability_and_unique_keys() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)

    with pytest.raises(LongStageBoundaryError, match=r"unknown=.*future_return_24h"):
        _s03(
            s02.with_columns(pl.lit(0.5, dtype=pl.Float64).alias("future_return_24h")),
            hourly,
        )
    with pytest.raises(LongStageBoundaryError, match="invalid registered dtypes"):
        _s03(s02.with_columns(pl.col("signal_ts_ms").cast(pl.Float64)), hourly)
    with pytest.raises(LongStageBoundaryError, match="non-nullable"):
        _s03(
            s02.with_columns(pl.lit(None, dtype=pl.String).alias("venue")),
            hourly,
        )
    with pytest.raises(LongStageBoundaryError, match="duplicate registered keys"):
        _s03(pl.concat([s02, s02]), hourly)

    s03 = _s03(s02, hourly)
    with pytest.raises(LongStageBoundaryError, match="duplicate registered keys"):
        _s04(s02, pl.concat([s03, s03]), hourly)
    with pytest.raises(LongStageBoundaryError, match="invalid registered dtypes"):
        _s04(
            s02,
            s03.with_columns(pl.col("current_entry_scan_missing_hour_bitmask").cast(pl.Int64)),
            hourly,
        )
    with pytest.raises(LongStageBoundaryError, match="non-nullable"):
        _s04(
            s02,
            s03.with_columns(pl.lit(None, dtype=pl.String).alias("long_entry_policy_schema_version")),
            hourly,
        )


def test_s03_refuses_hourly_outcome_columns_and_mixed_venue_ambiguity() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)
    contaminated = hourly.with_columns(pl.lit(0.9, dtype=pl.Float64).alias("future_return_24h"))
    with pytest.raises(ValueError, match=r"outcome-like.*future_return_24h"):
        _s03(s02, contaminated)

    other_venue = s02.with_columns(
        pl.lit("binance", dtype=pl.String).alias("venue"),
        pl.lit("AAA-USDT-BINANCE-PERP", dtype=pl.String).alias("canonical_instrument_id"),
    )
    with pytest.raises(LongStageBoundaryError, match="exactly one venue"):
        _s03(pl.concat([s02, other_venue]), hourly)


def test_s04_requires_exact_key_and_canonical_identity_parity() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)
    s03 = _s03(s02, hourly)
    tampered_identity = s03.with_columns(pl.lit("OTHER-CANONICAL", dtype=pl.String).alias("canonical_instrument_id"))

    with pytest.raises(LongStageBoundaryError, match="canonical_mismatch"):
        _s04(s02, tampered_identity, hourly)


def test_s04_rejects_s03_geometry_that_does_not_reconstruct() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)
    s03 = _s03(s02, hourly)
    tampered = s03.with_columns((pl.col("current_entry_price") + 1.0).alias("current_entry_price"))

    with pytest.raises(ValueError, match=r"current_entry_price.*reconstructed"):
        _s04(s02, tampered, hourly)


def test_s03_does_not_read_after_decision_prefix_and_s04_stops_at_frozen_paths() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)
    baseline_s03 = _s03(s02, hourly)

    after_entry_mutation = hourly.with_columns(
        pl.when(pl.col("ts_ms") >= SIGNAL_TS_MS + 10 * MS_PER_HOUR)
        .then(pl.lit(999.0))
        .otherwise(pl.col("open"))
        .alias("open"),
        pl.when(pl.col("ts_ms") >= SIGNAL_TS_MS + 10 * MS_PER_HOUR)
        .then(pl.lit(1000.0))
        .otherwise(pl.col("high"))
        .alias("high"),
        pl.when(pl.col("ts_ms") >= SIGNAL_TS_MS + 10 * MS_PER_HOUR)
        .then(pl.lit(998.0))
        .otherwise(pl.col("low"))
        .alias("low"),
        pl.when(pl.col("ts_ms") >= SIGNAL_TS_MS + 10 * MS_PER_HOUR)
        .then(pl.lit(999.0))
        .otherwise(pl.col("close"))
        .alias("close"),
    )
    assert_frame_equal(baseline_s03, _s03(s02, after_entry_mutation))

    baseline_s04 = _s04(s02, baseline_s03, hourly)
    after_frozen_path_mutation = hourly.with_columns(
        pl.when(pl.col("ts_ms") >= SIGNAL_TS_MS + 79 * MS_PER_HOUR)
        .then(pl.lit(777.0))
        .otherwise(pl.col("open"))
        .alias("open"),
        pl.when(pl.col("ts_ms") >= SIGNAL_TS_MS + 79 * MS_PER_HOUR)
        .then(pl.lit(778.0))
        .otherwise(pl.col("high"))
        .alias("high"),
        pl.when(pl.col("ts_ms") >= SIGNAL_TS_MS + 79 * MS_PER_HOUR)
        .then(pl.lit(776.0))
        .otherwise(pl.col("low"))
        .alias("low"),
        pl.when(pl.col("ts_ms") >= SIGNAL_TS_MS + 79 * MS_PER_HOUR)
        .then(pl.lit(777.0))
        .otherwise(pl.col("close"))
        .alias("close"),
    )
    assert_frame_equal(
        baseline_s04,
        _s04(s02, baseline_s03, after_frozen_path_mutation),
    )


def test_s03_broad_primitive_receives_only_its_finite_s02_dependency_surface() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)
    baseline = _s03(s02, hourly)
    mutated_irrelevant_signal_fields = s02.with_columns(
        pl.lit(999.0, dtype=pl.Float64).alias("log_return"),
        pl.lit("caller_diagnostic", dtype=pl.String).alias("first_sequential_rejection_reason"),
        pl.lit(False, dtype=pl.Boolean).alias("fc_detector_selected"),
    )

    assert_frame_equal(baseline, _s03(mutated_irrelevant_signal_fields, hourly))


def test_stage_outputs_are_deterministically_sorted() -> None:
    hourly_a = _hourly(symbol="AAAUSDT")
    hourly_b = _hourly(symbol="BBBUSDT")
    s02_a = _exact_s02(
        hourly_a,
        symbol="AAAUSDT",
        canonical_id="AAA-USDT-LINEAR-PERP",
    )
    s02_b = _exact_s02(
        hourly_b,
        symbol="BBBUSDT",
        canonical_id="BBB-USDT-LINEAR-PERP",
    )
    s02 = pl.concat([s02_b, s02_a])
    hourly = pl.concat([hourly_b, hourly_a])

    s03 = _s03(s02, hourly)
    s04 = _s04(s02, s03, hourly)

    assert s03["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]
    assert s04["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]


def test_frozen_config_is_required_for_both_exact_stages() -> None:
    hourly = _hourly()
    s02 = _exact_s02(hourly)
    s03 = _s03(s02, hourly)
    mutated = replace(_v11a_long_native_config(), cost_multiplier=99.0)

    with pytest.raises(ValueError, match=r"exact _v11a_long_native_config.*cost_multiplier"):
        build_long_s03_entry_policy(s02, hourly, config=mutated)
    with pytest.raises(ValueError, match=r"exact _v11a_long_native_config.*cost_multiplier"):
        build_long_s04_path_labels(s02, s03, hourly, config=mutated)
