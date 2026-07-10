"""Focused synthetic checks for the pre-filter CONTINUOUS population tape."""

from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.continuous_population_scout import (
    FROZEN_FORWARD_HORIZONS_HOURS,
    MINIMAL_EXCURSION_HORIZONS_HOURS,
    MINIMAL_RETURN_HORIZONS_HOURS,
    append_continuous_extended_path_atlas,
    append_continuous_path_labels,
    build_continuous_entry_anchor,
    build_continuous_feature_tape,
)
from liquidity_migration.continuous_events import compute_continuous_decile_panel
from liquidity_migration.strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    CONTINUOUS_ENTRY_SCHEMA_ID,
    CONTINUOUS_LABEL_SCHEMA_ID,
)


_POLARS_SCHEMA_DTYPES = {
    "utf8": pl.String,
    "int64": pl.Int64,
    "float64": pl.Float64,
    "bool": pl.Boolean,
}


def _expected_schema_signature(schema_id: str) -> list[tuple[str, pl.DataType]]:
    return [
        (field.name, _POLARS_SCHEMA_DTYPES[field.dtype])
        for field in ARTIFACT_SCHEMAS[schema_id].fields
    ]


def _schema_signature(frame: pl.DataFrame) -> list[tuple[str, pl.DataType]]:
    return list(frame.schema.items())


def _flat_hourly(symbols: list[str], n_hours: int, *, turnover: float = 100.0) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for symbol_index, symbol in enumerate(symbols):
        base = 100.0 + 10.0 * symbol_index
        for hour in range(n_hours):
            rows.append(
                {
                    "venue": "test",
                    "canonical_instrument_id": f"test:{symbol}",
                    "symbol": symbol,
                    "ts_ms": hour * MS_PER_HOUR,
                    "open": base,
                    "high": base,
                    "low": base,
                    "close": base,
                    "turnover_quote": turnover,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("ts_ms").cast(pl.Int64))


def _rmom_for_days(
    symbols: list[str],
    n_hours: int,
    *,
    provisional_symbols: set[str] | None = None,
) -> pl.DataFrame:
    provisional_symbols = provisional_symbols or set()
    days = range((n_hours - 1) * MS_PER_HOUR // MS_PER_DAY + 1)
    rows = [
        {
            "symbol": symbol,
            "ts_ms": day * MS_PER_DAY,
            "residual_momentum": float(symbol_index),
            "is_provisional": symbol in provisional_symbols,
        }
        for day in days
        for symbol_index, symbol in enumerate(symbols)
    ]
    return pl.DataFrame(rows).with_columns(pl.col("ts_ms").cast(pl.Int64))


def _replace_bar(
    frame: pl.DataFrame,
    *,
    symbol: str,
    hour: int,
    close_return: float,
    turnover: float,
) -> pl.DataFrame:
    ts_ms = hour * MS_PER_HOUR
    rows = frame.to_dicts()
    prior_close = next(
        float(row["close"])
        for row in rows
        if row["symbol"] == symbol and int(row["ts_ms"]) == ts_ms - MS_PER_HOUR
    )
    close = prior_close * (1.0 + close_return)
    for row in rows:
        if row["symbol"] == symbol and int(row["ts_ms"]) == ts_ms:
            row.update(
                {
                    "open": prior_close,
                    "high": max(prior_close, close),
                    "low": min(prior_close, close),
                    "close": close,
                    "turnover_quote": turnover,
                }
            )
            break
    return pl.DataFrame(rows).with_columns(pl.col("ts_ms").cast(pl.Int64))


def test_population_features_are_causal_under_future_price_changes() -> None:
    symbols = ["A", "B", "C"]
    klines = _flat_hourly(symbols, 90, turnover=1_000_000.0)
    rmom = _rmom_for_days(symbols, 90)
    target_hour = 60
    baseline = build_continuous_feature_tape(klines, rmom)

    changed = klines.with_columns(
        pl.when(pl.col("ts_ms") > target_hour * MS_PER_HOUR)
        .then(pl.col("close") * 1.5)
        .otherwise(pl.col("close"))
        .alias("close")
    ).with_columns(
        pl.max_horizontal("open", "close").alias("high"),
        pl.min_horizontal("open", "close").alias("low"),
    )
    rebuilt = build_continuous_feature_tape(changed, rmom)
    causal_columns = [
        "ret1",
        "rv_168h",
        "max_ret168",
        "prior_max_ret168_lag1",
        "turnover_spike_168h",
        "residual_momentum_rank",
        "current_q25_pass",
        "full_population_score",
        "full_population_decile",
        "current_q25_score",
        "current_q25_decile",
        "component_mask",
        "trigger_spell_id",
        "event_wave_id",
    ]
    left = baseline.filter(pl.col("ts_ms") == target_hour * MS_PER_HOUR).select(["symbol", *causal_columns])
    right = rebuilt.filter(pl.col("ts_ms") == target_hour * MS_PER_HOUR).select(["symbol", *causal_columns])
    assert left.equals(right)
    assert not any(column.startswith(("ideal_entry_", "path_", "first_")) for column in baseline.columns)


def test_source_projection_drops_caller_derived_and_outcome_columns() -> None:
    source = _flat_hourly(["A"], 60, turnover=1_000_000.0).with_columns(
        pl.lit(-1, dtype=pl.Int64).alias("decision_ts_ms"),
        pl.lit(123.0).alias("future_return"),
        pl.lit(456.0).alias("entry_price"),
        pl.lit(789.0).alias("path_72h_underlying_return"),
    )
    tape = build_continuous_feature_tape(source)

    assert {"venue", "canonical_instrument_id"} <= set(tape.columns)
    assert "future_return" not in tape.columns
    assert "entry_price" not in tape.columns
    assert "path_72h_underlying_return" not in tape.columns
    assert tape.filter(pl.col("signal_ts_ms") == 10 * MS_PER_HOUR)["decision_ts_ms"].item() == (
        11 * MS_PER_HOUR
    )


def test_signal_features_restart_after_an_interior_hourly_gap() -> None:
    source = _replace_bar(
        _flat_hourly(["A"], 220, turnover=1_000_000.0),
        symbol="A",
        hour=101,
        close_return=0.04,
        turnover=4_000_000.0,
    ).filter(pl.col("ts_ms") != 100 * MS_PER_HOUR)
    tape = build_continuous_feature_tape(source)

    first_after_gap = tape.filter(pl.col("ts_ms") == 101 * MS_PER_HOUR).row(0, named=True)
    assert first_after_gap["ret1"] is None
    assert first_after_gap["rv_168h"] is None
    assert first_after_gap["max_ret168"] is None
    assert first_after_gap["prior6_ret1_max"] is None
    assert first_after_gap["turnover_spike_168h"] is None
    assert first_after_gap["trigger_any_current_component"] is False

    rewarmed = tape.filter(pl.col("ts_ms") == 150 * MS_PER_HOUR).row(0, named=True)
    assert rewarmed["max_ret168"] == pytest.approx(0.0)
    assert rewarmed["prior_max_ret168_lag1"] == pytest.approx(0.0)
    assert rewarmed["ret72"] is None
    assert rewarmed["ret168"] is None


def test_nested_component_flags_and_implied_tier_weights() -> None:
    klines = _flat_hourly(["P3", "P4P3", "P4P5"], 60)
    klines = _replace_bar(klines, symbol="P3", hour=59, close_return=0.04, turnover=350.0)
    klines = _replace_bar(klines, symbol="P4P3", hour=59, close_return=0.04, turnover=450.0)
    klines = _replace_bar(klines, symbol="P4P5", hour=59, close_return=0.06, turnover=500.0)
    tape = build_continuous_feature_tape(klines, _rmom_for_days(["P3", "P4P3", "P4P5"], 60))
    rows = {
        row["symbol"]: row
        for row in tape.filter(pl.col("ts_ms") == 59 * MS_PER_HOUR).select(
            "symbol",
            "trigger_turn3_pop3",
            "trigger_turn4_pop3",
            "trigger_turn4_pop5",
            "component_mask",
            "component_tags",
            "component_membership_count",
            "implied_tier_weight",
            "raw_trigger_spell_head",
            "simultaneous_trigger_decision_count",
        ).to_dicts()
    }

    assert (rows["P3"]["trigger_turn3_pop3"], rows["P3"]["trigger_turn4_pop3"]) == (True, False)
    assert rows["P3"]["component_mask"] == 1
    assert rows["P3"]["component_tags"] == "p3"
    assert rows["P3"]["component_membership_count"] == 1
    assert rows["P3"]["implied_tier_weight"] == pytest.approx(1.0 / 3.0)

    assert rows["P4P3"]["trigger_turn3_pop3"] is True
    assert rows["P4P3"]["trigger_turn4_pop3"] is True
    assert rows["P4P3"]["trigger_turn4_pop5"] is False
    assert rows["P4P3"]["component_mask"] == 3
    assert rows["P4P3"]["implied_tier_weight"] == pytest.approx(5.0 / 9.0)

    assert rows["P4P5"]["trigger_turn3_pop3"] is True
    assert rows["P4P5"]["trigger_turn4_pop3"] is True
    assert rows["P4P5"]["trigger_turn4_pop5"] is True
    assert rows["P4P5"]["component_mask"] == 7
    assert rows["P4P5"]["implied_tier_weight"] == pytest.approx(1.0)
    assert all(row["raw_trigger_spell_head"] for row in rows.values())
    assert {row["simultaneous_trigger_decision_count"] for row in rows.values()} == {3}


def test_missing_and_provisional_rmom_rows_remain_in_population() -> None:
    symbols = ["STABLE", "MISSING", "PROVISIONAL"]
    klines = _flat_hourly(symbols, 60, turnover=1_000_000.0)
    rmom = _rmom_for_days(["STABLE", "PROVISIONAL"], 60, provisional_symbols={"PROVISIONAL"})
    features = build_continuous_feature_tape(klines, rmom)
    assert features.height == klines.height
    rows = {
        row["symbol"]: row
        for row in features.filter(pl.col("ts_ms") == 59 * MS_PER_HOUR).select(
            "symbol",
            "rmom_present",
            "rmom_is_provisional",
            "rmom_stable_available",
            "current_q25_pass",
            "current_q25_score",
            "full_population_score",
            "rmom_population_peer_count",
            "rmom_rankable_peer_count",
            "rmom_missing_peer_count",
        ).to_dicts()
    }
    assert rows["STABLE"]["rmom_present"] is True
    assert rows["STABLE"]["rmom_stable_available"] is True
    assert rows["STABLE"]["current_q25_pass"] is True
    assert rows["MISSING"]["rmom_present"] is False
    assert rows["MISSING"]["current_q25_score"] is None
    assert rows["PROVISIONAL"]["rmom_present"] is True
    assert rows["PROVISIONAL"]["rmom_is_provisional"] is True
    assert rows["PROVISIONAL"]["rmom_stable_available"] is False
    assert rows["PROVISIONAL"]["current_q25_score"] is None
    assert rows["MISSING"]["full_population_score"] is not None
    assert rows["STABLE"]["rmom_population_peer_count"] == 3
    assert rows["STABLE"]["rmom_rankable_peer_count"] == 1
    assert rows["STABLE"]["rmom_missing_peer_count"] == 2


def test_legacy_rmom_provenance_is_visible_but_never_stable() -> None:
    klines = _flat_hourly(["LEGACY"], 60, turnover=1_000_000.0)
    legacy = _rmom_for_days(["LEGACY"], 60).drop("is_provisional")
    row = (
        build_continuous_feature_tape(klines, legacy)
        .filter(pl.col("ts_ms") == 59 * MS_PER_HOUR)
        .row(0, named=True)
    )

    assert row["rmom_source_row_present"] is True
    assert row["rmom_present"] is True
    assert row["rmom_provenance_declared"] is False
    assert row["rmom_is_provisional"] is True
    assert row["rmom_stable_available"] is False
    assert row["residual_momentum_rank"] is None
    assert row["current_q25_pass"] is False


def test_prior_max_ret168_lag1_excludes_the_signal_row() -> None:
    base = _flat_hourly(["A"], 220, turnover=1_000_000.0)
    prior_pump = _replace_bar(base, symbol="A", hour=100, close_return=0.10, turnover=1_000_000.0)
    current_pump = _replace_bar(
        prior_pump,
        symbol="A",
        hour=219,
        close_return=0.20,
        turnover=1_000_000.0,
    )
    changed_current = _replace_bar(
        prior_pump,
        symbol="A",
        hour=219,
        close_return=0.40,
        turnover=1_000_000.0,
    )

    row = build_continuous_feature_tape(current_pump).filter(
        pl.col("ts_ms") == 219 * MS_PER_HOUR
    ).row(0, named=True)
    changed = build_continuous_feature_tape(changed_current).filter(
        pl.col("ts_ms") == 219 * MS_PER_HOUR
    ).row(0, named=True)
    assert row["max_ret168"] == pytest.approx(0.20)
    assert changed["max_ret168"] == pytest.approx(0.40)
    assert row["prior_max_ret168_lag1"] == pytest.approx(0.10)
    assert changed["prior_max_ret168_lag1"] == pytest.approx(0.10)


def test_full_population_and_current_q25_rank_support_are_both_emitted() -> None:
    symbols = [f"S{i:02d}" for i in range(40)]
    klines = _flat_hourly(symbols, 60, turnover=1_000_000.0)
    for index, symbol in enumerate(symbols):
        klines = _replace_bar(
            klines,
            symbol=symbol,
            hour=59,
            close_return=0.001 + index * 0.001,
            turnover=1_000_000.0,
        )
    rmom = _rmom_for_days(symbols, 60)
    rows = build_continuous_feature_tape(klines, rmom).filter(pl.col("ts_ms") == 59 * MS_PER_HOUR)
    assert rows.height == 40
    assert rows["full_population_score"].null_count() == 0
    assert rows.filter(pl.col("current_q25_pass")).height == 10
    assert rows["current_q25_score"].drop_nulls().len() == 10
    assert rows.filter(~pl.col("current_q25_pass"))["current_q25_score"].null_count() == 30
    assert rows["full_population_rankable_peer_count"].unique().to_list() == [40]
    assert rows["current_q25_rankable_peer_count"].unique().to_list() == [10]
    assert rows["current_q25_decile"].max() == 9


def test_current_q25_rank_and_decile_match_production_pipeline() -> None:
    symbols = [f"S{i:02d}" for i in range(40)]
    klines = _flat_hourly(symbols, 60, turnover=1_000_000.0)
    for index, symbol in enumerate(symbols):
        klines = _replace_bar(
            klines,
            symbol=symbol,
            hour=59,
            close_return=0.001 + index * 0.001,
            turnover=1_000_000.0 + index,
        )
    rmom = _rmom_for_days(symbols, 60)
    scout = (
        build_continuous_feature_tape(klines, rmom)
        .filter((pl.col("ts_ms") == 59 * MS_PER_HOUR) & pl.col("current_q25_pass"))
        .select(
            "symbol",
            "current_q25_score",
            "current_q25_decile",
            "residual_momentum_rank",
            "liquidity_rank",
        )
        .sort("symbol")
    )
    production = (
        compute_continuous_decile_panel(
            klines.select("symbol", "ts_ms", "close", "turnover_quote"),
            rmom.select(
                "symbol",
                pl.col("ts_ms").alias("day_ts"),
                "residual_momentum",
            ),
            rmom_quantile=0.25,
            start_ms=59 * MS_PER_HOUR,
            feature_set=("max_ret168",),
        )
        .select(
            "symbol",
            pl.col("composite").alias("current_q25_score"),
            pl.col("decile").alias("current_q25_decile"),
            "residual_momentum_rank",
            "liquidity_rank",
        )
        .sort("symbol")
    )
    assert scout.equals(production)


def test_trigger_spell_ids_split_on_an_hourly_gap() -> None:
    klines = _flat_hourly(["PUMP"], 65)
    for hour in (60, 61, 63):
        klines = _replace_bar(klines, symbol="PUMP", hour=hour, close_return=0.04, turnover=1_000.0)
    rows = (
        build_continuous_feature_tape(klines)
        .filter(pl.col("trigger_turn3_pop3"))
        .select("ts_ms", "trigger_spell_id", "trigger_spell_head", "trigger_spell_hour_index")
        .sort("ts_ms")
        .to_dicts()
    )
    assert [row["ts_ms"] // MS_PER_HOUR for row in rows] == [60, 61, 63]
    assert rows[0]["trigger_spell_id"] == rows[1]["trigger_spell_id"]
    assert rows[0]["trigger_spell_head"] is True
    assert rows[1]["trigger_spell_head"] is False
    assert rows[1]["trigger_spell_hour_index"] == 1
    assert rows[2]["trigger_spell_id"] != rows[1]["trigger_spell_id"]
    assert rows[2]["trigger_spell_head"] is True


def test_event_waves_use_six_hour_gap_and_strict_72h_cap_per_venue() -> None:
    early_klines = _flat_hourly(["A", "B"], 145)
    chained_hours = (60, 66, 72, 78, 84, 90, 96, 102, 108, 114, 120, 126, 131, 132, 139)
    for hour in (60, 66):
        early_klines = _replace_bar(
            early_klines,
            symbol="A",
            hour=hour,
            close_return=0.04,
            turnover=1_000.0,
        )
    early_klines = _replace_bar(
        early_klines,
        symbol="B",
        hour=66,
        close_return=0.04,
        turnover=1_000.0,
    )
    early_tape = build_continuous_feature_tape(early_klines)
    klines = early_klines
    for hour in chained_hours[2:]:
        klines = _replace_bar(klines, symbol="A", hour=hour, close_return=0.04, turnover=1_000.0)
    tape = build_continuous_feature_tape(klines)
    triggers = tape.filter(pl.col("trigger_turn3_pop3")).select(
        "symbol", "ts_ms", "event_wave_id"
    )

    wave_60 = f"test|wave|{60 * MS_PER_HOUR}"
    wave_132 = f"test|wave|{132 * MS_PER_HOUR}"
    wave_139 = f"test|wave|{139 * MS_PER_HOUR}"
    a_rows = {
        int(row["ts_ms"] // MS_PER_HOUR): row["event_wave_id"]
        for row in triggers.filter(pl.col("symbol") == "A").to_dicts()
    }
    assert all(a_rows[hour] == wave_60 for hour in chained_hours if hour < 132)
    assert a_rows[132] == wave_132  # strict < wave_start+72h cap
    assert a_rows[139] == wave_139  # seven-hour adjacent gap starts a wave
    assert (
        early_tape.filter(
            (pl.col("symbol") == "A")
            & pl.col("ts_ms").is_in([60 * MS_PER_HOUR, 66 * MS_PER_HOUR])
        )
        .select("ts_ms", "event_wave_id")
        .sort("ts_ms")
        .equals(
            tape.filter(
                (pl.col("symbol") == "A")
                & pl.col("ts_ms").is_in([60 * MS_PER_HOUR, 66 * MS_PER_HOUR])
            )
            .select("ts_ms", "event_wave_id")
            .sort("ts_ms")
        )
    )
    assert (
        triggers.filter((pl.col("symbol") == "B") & (pl.col("ts_ms") == 66 * MS_PER_HOUR))
        ["event_wave_id"]
        .item()
        == wave_60
    )
    assert (
        tape.filter((pl.col("symbol") == "B") & (pl.col("ts_ms") == 60 * MS_PER_HOUR))
        ["event_wave_id"]
        .item()
        is None
    )


def test_entry_anchor_reads_only_the_exact_following_bar() -> None:
    hourly = _flat_hourly(["A"], 30, turnover=1_000_000.0)
    feature = build_continuous_feature_tape(hourly).filter(
        pl.col("signal_ts_ms") == 10 * MS_PER_HOUR
    )
    changed_later = hourly.with_columns(
        pl.when(pl.col("ts_ms") > 11 * MS_PER_HOUR)
        .then(pl.lit(10_000.0))
        .otherwise(pl.col(column))
        .alias(column)
        for column in ("open", "high", "low", "close")
    )

    before = build_continuous_entry_anchor(feature, hourly)
    after = build_continuous_entry_anchor(feature, changed_later)
    assert before.equals(after)
    assert before["entry_anchor_available"].item() is True


def test_entry_anchor_rejects_null_or_tampered_s02_stage_fields() -> None:
    hourly = _flat_hourly(["A"], 30, turnover=1_000_000.0)
    feature = build_continuous_feature_tape(hourly).filter(
        pl.col("signal_ts_ms") == 10 * MS_PER_HOUR
    )
    invalid_frames = (
        feature.with_columns(pl.lit(None, dtype=pl.Int64).alias("signal_ts_ms")),
        feature.with_columns((pl.col("decision_ts_ms") + MS_PER_HOUR).alias("decision_ts_ms")),
        feature.with_columns(pl.lit(999.0).alias("entry_price")),
        feature.with_columns(pl.lit("forged").alias("missing_anchor_reason")),
    )
    for invalid in invalid_frames:
        with pytest.raises(ValueError):
            build_continuous_entry_anchor(invalid, hourly)


def test_path_labels_reject_tampered_or_non_exact_s03_stage() -> None:
    hourly = _flat_hourly(["A"], 30, turnover=1_000_000.0)
    feature = build_continuous_feature_tape(hourly).filter(
        pl.col("signal_ts_ms") == 10 * MS_PER_HOUR
    )
    anchor = build_continuous_entry_anchor(feature, hourly)
    invalid_frames = (
        anchor.with_columns(pl.lit(None, dtype=pl.Int64).alias("decision_ts_ms")),
        anchor.with_columns(
            (pl.col("entry_bar_start_ts_ms") + MS_PER_HOUR).alias("entry_bar_start_ts_ms")
        ),
        anchor.with_columns((pl.col("entry_anchor_ts_ms") + MS_PER_HOUR).alias("entry_anchor_ts_ms")),
        anchor.with_columns((pl.col("entry_price") + 1.0).alias("entry_price")),
        anchor.with_columns(pl.lit(None, dtype=pl.Boolean).alias("entry_anchor_available")),
        anchor.with_columns(pl.lit("forged").alias("missing_anchor_reason")),
        anchor.with_columns(pl.lit(1.0).alias("unregistered_outcome")),
    )
    for invalid in invalid_frames:
        with pytest.raises((ValueError, RuntimeError)):
            append_continuous_path_labels(invalid, hourly)


def test_s03_s04_empty_and_nonempty_outputs_have_exact_order_and_dtypes() -> None:
    hourly = _flat_hourly(["A"], 30, turnover=1_000_000.0)
    features = build_continuous_feature_tape(hourly)
    anchors = build_continuous_entry_anchor(features, hourly)
    labels = append_continuous_path_labels(anchors, hourly)
    empty_anchors = build_continuous_entry_anchor(features.head(0), hourly.head(0))
    empty_labels = append_continuous_path_labels(anchors.head(0), hourly.head(0))

    expected_entry = _expected_schema_signature(CONTINUOUS_ENTRY_SCHEMA_ID)
    expected_labels = _expected_schema_signature(CONTINUOUS_LABEL_SCHEMA_ID)
    assert _schema_signature(anchors) == expected_entry
    assert _schema_signature(empty_anchors) == expected_entry
    assert _schema_signature(labels) == expected_labels
    assert _schema_signature(empty_labels) == expected_labels


def test_path_labels_censor_at_the_same_interior_gap_as_signal_features() -> None:
    rows: list[dict[str, object]] = []
    for hour in range(90):
        close = 100.0 + hour
        prior_close = 100.0 + max(hour - 1, 0)
        rows.append(
            {
                "venue": "test",
                "canonical_instrument_id": "test:UP",
                "symbol": "UP",
                "ts_ms": hour * MS_PER_HOUR,
                "open": prior_close,
                "high": max(prior_close, close),
                "low": min(prior_close, close),
                "close": close,
                "turnover_quote": 1_000_000.0,
            }
        )
    hourly = pl.DataFrame(rows).with_columns(pl.col("ts_ms").cast(pl.Int64)).filter(
        pl.col("ts_ms") != 20 * MS_PER_HOUR
    )
    features = build_continuous_feature_tape(hourly)
    anchors = build_continuous_entry_anchor(features, hourly)
    labels = append_continuous_path_labels(anchors, hourly)
    row = labels.filter(pl.col("decision_ts_ms") == 11 * MS_PER_HOUR).row(0, named=True)

    assert row["path_1h_complete"] is True
    assert row["path_24h_observed_hours"] == 8
    assert row["path_24h_available"] is False
    assert row["path_24h_complete"] is False
    assert row["path_24h_underlying_return"] is None
    assert row["path_24h_missing_reason"] == "endpoint_unavailable"
    assert row["missing_path_reason"] == "incomplete_24h_path"


def test_minimal_labels_are_separate_and_use_next_executable_close() -> None:
    rows: list[dict[str, object]] = []
    for hour in range(90):
        close = 100.0 + hour
        prior_close = 100.0 + max(hour - 1, 0)
        rows.append(
            {
                "venue": "test",
                "canonical_instrument_id": "test:UP",
                "symbol": "UP",
                "ts_ms": hour * MS_PER_HOUR,
                "open": prior_close,
                "high": max(prior_close, close),
                "low": min(prior_close, close),
                "close": close,
                "turnover_quote": 1_000_000.0,
            }
        )
    hourly = pl.DataFrame(rows).with_columns(pl.col("ts_ms").cast(pl.Int64))
    features = build_continuous_feature_tape(hourly)
    anchors = build_continuous_entry_anchor(features, hourly)
    tape = append_continuous_path_labels(anchors, hourly)
    assert anchors.height == features.height
    assert tape.height == anchors.height
    anchor = anchors.filter(pl.col("signal_ts_ms") == 10 * MS_PER_HOUR).row(0, named=True)
    signal = tape.filter(pl.col("decision_ts_ms") == 11 * MS_PER_HOUR).row(0, named=True)
    assert FROZEN_FORWARD_HORIZONS_HOURS == (1, 2, 4, 8, 12, 24, 48, 72)
    assert MINIMAL_RETURN_HORIZONS_HOURS == (1, 24, 72)
    assert MINIMAL_EXCURSION_HORIZONS_HOURS == (24, 72)
    assert anchors.columns == [
        field.name for field in ARTIFACT_SCHEMAS[CONTINUOUS_ENTRY_SCHEMA_ID].fields
    ]
    assert tape.columns == [
        field.name for field in ARTIFACT_SCHEMAS[CONTINUOUS_LABEL_SCHEMA_ID].fields
    ]
    assert not any(column.startswith("path_") for column in anchors.columns)
    assert "entry_price" not in tape.columns
    assert anchor["entry_bar_start_ts_ms"] == 11 * MS_PER_HOUR
    assert anchor["entry_anchor_ts_ms"] == 12 * MS_PER_HOUR
    assert anchor["entry_price"] == pytest.approx(111.0)
    assert anchor["entry_anchor_available"] is True
    assert anchor["missing_anchor_reason"] is None
    assert signal["path_1h_close_ts_ms"] == 13 * MS_PER_HOUR
    assert signal["path_1h_underlying_return"] == pytest.approx(112.0 / 111.0 - 1.0)
    assert signal["path_1h_short_directional_return"] == pytest.approx(-(112.0 / 111.0 - 1.0))
    assert signal["path_24h_short_mfe"] == pytest.approx(0.0)
    assert signal["path_24h_short_mae"] == pytest.approx(135.0 / 111.0 - 1.0)
    assert signal["path_24h_observed_hours"] == 24
    assert signal["path_24h_available"] is True
    assert signal["path_24h_missing_reason"] is None
    assert signal["path_24h_hourly_extrema_interval_censored"] is True
    assert signal["path_72h_underlying_return"] == pytest.approx(183.0 / 111.0 - 1.0)
    assert signal["path_72h_complete"] is True
    assert signal["path_all_minimal_labels_complete"] is True
    assert signal["missing_path_reason"] is None
    assert "path_2h_underlying_return" not in tape.columns
    assert "path_1h_short_mark_return" not in tape.columns
    assert "path_1h_mfe" not in tape.columns
    assert not any(column.startswith("first_") for column in tape.columns)

    incomplete = tape.filter(pl.col("decision_ts_ms") == 18 * MS_PER_HOUR).row(0, named=True)
    assert incomplete["path_72h_complete"] is False
    assert incomplete["path_72h_underlying_return"] is None
    assert incomplete["path_72h_observed_hours"] == 71
    assert incomplete["path_72h_available"] is False
    assert incomplete["path_72h_missing_reason"] == "endpoint_unavailable"
    assert incomplete["path_all_minimal_labels_complete"] is False
    assert incomplete["missing_path_reason"] == "incomplete_72h_path"

    unavailable = anchors.filter(pl.col("signal_ts_ms") == 89 * MS_PER_HOUR).row(0, named=True)
    assert unavailable["entry_anchor_available"] is False
    assert unavailable["entry_anchor_ts_ms"] is None
    assert unavailable["entry_price"] is None
    assert unavailable["missing_anchor_reason"] == "no_next_entry_bar"
    unavailable_labels = tape.filter(pl.col("decision_ts_ms") == 90 * MS_PER_HOUR).row(
        0, named=True
    )
    assert unavailable_labels["path_1h_close_ts_ms"] is None
    assert unavailable_labels["path_1h_observed_hours"] == 0
    assert unavailable_labels["path_1h_available"] is False
    assert unavailable_labels["path_1h_missing_reason"] == "no_entry_anchor"


def test_extended_path_atlas_is_explicit_opt_in() -> None:
    features = build_continuous_feature_tape(_flat_hourly(["A"], 90))
    atlas = append_continuous_extended_path_atlas(features)
    assert "path_2h_underlying_return" in atlas.columns
    assert "path_1h_short_mark_return" in atlas.columns
    assert "first_adverse_10pct_hours" in atlas.columns
    assert "first_favorable_10pct_hours" in atlas.columns


def test_duplicate_hourly_and_rmom_keys_fail_loudly() -> None:
    klines = _flat_hourly(["A"], 60)
    duplicate_klines = pl.concat([klines, klines.head(1)])
    with pytest.raises(ValueError, match=r"duplicate \(symbol,ts_ms\)"):
        build_continuous_feature_tape(duplicate_klines)

    rmom = _rmom_for_days(["A"], 60)
    duplicate_rmom = pl.concat([rmom, rmom.head(1)])
    with pytest.raises(ValueError, match=r"duplicate \(symbol,day_ts\)"):
        build_continuous_feature_tape(klines, duplicate_rmom)
