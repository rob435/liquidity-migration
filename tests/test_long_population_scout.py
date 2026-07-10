from __future__ import annotations

import math
from dataclasses import replace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native import LongNativeConfig, _classify_entry
from liquidity_migration.long_native_event_demo import _v11a_long_native_config
from liquidity_migration.long_population_scout import (
    EXPLORATORY_LABEL_SCHEMA_VERSION,
    _append_long_first_passage_diagnostics,
    append_long_entry_policy,
    append_long_exploratory_path_labels,
    append_long_path_labels,
    build_long_feature_tape,
    build_long_population_tape,
)
from liquidity_migration.strategy_overhaul_schemas import ARTIFACT_SCHEMAS, LONG_LABEL_SCHEMA_ID

SIGNAL_TS = 20 * MS_PER_DAY


def _feature(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "venue": "bybit",
        "symbol": "AAAUSDT",
        "canonical_instrument_id": "asset_a",
        "ts_ms": SIGNAL_TS,
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
    row.update(overrides)
    return row


def _hourly(
    *,
    close_by_hour: dict[int, float] | None = None,
    range_by_hour: dict[int, tuple[float, float]] | None = None,
    skip_hours: set[int] | None = None,
    through_hour: int = 80,
) -> pl.DataFrame:
    close_by_hour = close_by_hour or {}
    range_by_hour = range_by_hour or {}
    skip_hours = skip_hours or set()
    rows: list[dict[str, object]] = []
    for hour in range(0, through_hour + 1):
        if hour in skip_hours:
            continue
        close = close_by_hour.get(hour, 100.0)
        high, low = range_by_hour.get(hour, (close + 0.1, close - 0.1))
        rows.append(
            {
                # ``hour`` is the bar-end offset; source timestamps are bar opens.
                "ts_ms": SIGNAL_TS + (hour - 1) * MS_PER_HOUR,
                "symbol": "AAAUSDT",
                "open": close,
                "high": high,
                "low": low,
                "close": close,
            }
        )
    return pl.DataFrame(rows)


def _feature_tape(feature: dict[str, object], hourly: pl.DataFrame) -> pl.DataFrame:
    result = build_long_feature_tape(
        pl.DataFrame([feature]),
        hourly,
        _v11a_long_native_config(),
    )
    assert result.height == 1
    return result


def _run_feature(feature: dict[str, object], hourly: pl.DataFrame) -> dict[str, object]:
    return _feature_tape(feature, hourly).row(0, named=True)


def _entry_tape(feature: dict[str, object], hourly: pl.DataFrame) -> pl.DataFrame:
    return append_long_entry_policy(
        _feature_tape(feature, hourly),
        hourly,
        _v11a_long_native_config(),
    )


def _run_entry(feature: dict[str, object], hourly: pl.DataFrame) -> dict[str, object]:
    return _entry_tape(feature, hourly).row(0, named=True)


def test_trigger_overlap_timing_retraces_and_both_anchors() -> None:
    closes = {
        0: 100.0,
        1: 100.0,
        2: 99.4,
        3: 98.9,
        4: 97.9,
        5: 98.5,
        6: 99.2,
        7: 101.5,
        9: 102.0,
    }
    hourly = _hourly(close_by_hour=closes)
    feature_row = _run_feature(_feature(), hourly)

    assert feature_row["fc_trigger_identities"] == ["1d", "3d", "7d"]
    assert feature_row["fc_trigger_identity"] == "1d+3d+7d"
    assert feature_row["fc_detector_selected"] is True
    assert feature_row["classifier_selected"] is True
    assert feature_row["classifier_eligible"] is True
    assert "selected_flag" not in feature_row
    assert feature_row["signal_ts_ms"] == SIGNAL_TS
    assert "ts_ms" not in feature_row
    assert feature_row["simple_return_1d"] == pytest.approx(math.expm1(0.20))
    assert feature_row["simple_return_3d"] == pytest.approx(math.expm1(0.30))
    assert feature_row["simple_return_7d"] == pytest.approx(math.expm1(0.40))
    assert feature_row["ratio_1d"] == pytest.approx(2.0)
    assert feature_row["ratio_3d"] == pytest.approx(0.30 / (0.10 * math.sqrt(3.0)))
    assert feature_row["ratio_7d"] == pytest.approx(0.40 / (0.10 * math.sqrt(7.0)))
    assert feature_row["fc_trigger_bitmask"] == 7
    assert feature_row["trigger_strength_ratio"] == pytest.approx(2.0)
    assert feature_row["active_trigger_close_location"] == pytest.approx(0.80)
    assert feature_row["intraday_feature_available"] is False
    assert feature_row["first_sequential_rejection_reason"] == "selected"
    assert not any(
        column.startswith(("common_entry", "current_entry")) or "entry_scan" in column
        for column in feature_row
    )

    row = _run_entry(_feature(), hourly)
    assert row["current_entry_scan_first_hour"] == 1
    assert row["current_entry_scan_end_hour"] == 3
    assert row["current_entry_scan_missing_hour_bitmask"] == 0
    assert row["current_entry_scan_prefix_complete"] is True
    assert row["current_entry_policy_available"] is True
    assert row["current_entry_missing_reason"] is None
    assert row["current_entry_close_triggered"] is True
    assert row["current_entry_close_trigger_first_hour"] == 3
    assert row["current_entry_intrabar_low_touch_nonfill"] is True
    assert row["current_entry_intrabar_low_first_hour_nonfill"] == 3
    assert not any(column.startswith("retrace_0_5pct") for column in row)
    assert not any(column.startswith("retrace_2pct") for column in row)
    assert not any(column.startswith("entry_path_h") for column in row)
    assert "current_entry_scan_closes" not in row
    assert "current_entry_scan_lows_nonfill" not in row

    assert row["common_entry_ts_ms"] == SIGNAL_TS + MS_PER_HOUR
    assert row["common_entry_price"] == pytest.approx(100.0)
    assert row["current_entry_reason"] == "sniper_retrace"
    assert row["current_entry_hour"] == 3
    assert row["current_entry_ts_ms"] == SIGNAL_TS + 3 * MS_PER_HOUR
    assert row["current_entry_price"] == pytest.approx(98.9)

    labeled = append_long_exploratory_path_labels(
        _entry_tape(_feature(), hourly),
        hourly,
        _v11a_long_native_config(),
        point_horizons=(6,),
        excursion_horizons=(6,),
    ).row(0, named=True)
    assert labeled["long_label_schema_version"] == EXPLORATORY_LABEL_SCHEMA_VERSION
    assert labeled["common_6h_point_return"] == pytest.approx(0.015)
    assert labeled["current_6h_point_return"] == pytest.approx(102.0 / 98.9 - 1.0)


def test_independent_gate_flags_and_first_sequential_rejection() -> None:
    feature = _feature(
        in_universe=False,
        regime_on=False,
        eth_regime_on=False,
        today_volume_rank=11.0,
        atr_14d_pct=0.20,
    )
    row = _run_feature(feature, _hourly())

    assert row["gate_in_universe"] is False
    assert row["gate_btc_regime"] is False
    assert row["gate_eth_regime"] is False
    assert row["gate_volume_rank"] is False
    assert row["gate_atr_cap"] is False
    # Trigger attribution is marginal: it remains true despite earlier failures.
    assert row["gate_any_trigger"] is True
    assert row["first_sequential_rejection_reason"] == "not_in_universe"
    assert row["classifier_selected"] is False
    assert row["classifier_eligible"] is False
    assert row["classified_pattern"] is None
    assert _classify_entry(feature, _v11a_long_native_config())[0] is None


def test_trigger_strength_and_active_close_location_follow_frozen_definitions() -> None:
    strongest_1d = _run_feature(
        _feature(
            close_location=0.71,
            pump_3d_log=0.18,
            close_loc_3d=0.95,
            pump_7d_log=0.27,
            close_loc_7d=0.85,
        ),
        _hourly(),
    )
    assert strongest_1d["fc_trigger_bitmask"] == 7
    assert strongest_1d["trigger_strength_ratio"] == pytest.approx(2.0)
    # This is the maximum location among fired families, not the location of
    # the family with the strongest threshold-relative ratio.
    assert strongest_1d["active_trigger_close_location"] == pytest.approx(0.95)

    below_threshold = _run_feature(
        _feature(
            log_return=0.05,
            pump_3d_log=0.10,
            pump_7d_log=0.10,
        ),
        _hourly(),
    )
    assert below_threshold["fc_trigger_bitmask"] == 0
    assert below_threshold["fc_all_trigger"] is False
    assert below_threshold["trigger_strength_ratio"] == pytest.approx(
        0.10 / (0.10 * math.sqrt(3.0))
    )
    assert below_threshold["active_trigger_close_location"] is None


def test_current_one_percent_six_hour_fallthrough_anchor() -> None:
    closes = {hour: 99.2 + hour / 100.0 for hour in range(1, 7)}
    row = _run_entry(_feature(), _hourly(close_by_hour=closes))

    assert row["current_entry_close_triggered"] is False
    assert row["current_entry_close_trigger_first_hour"] is None
    assert row["current_entry_scan_first_hour"] == 1
    assert row["current_entry_scan_end_hour"] == 6
    assert row["current_entry_scan_missing_hour_bitmask"] == 0
    assert row["current_entry_reason"] == "sniper_deadline_fallthrough"
    assert row["current_entry_hour"] == 6
    assert row["current_entry_ts_ms"] == SIGNAL_TS + 6 * MS_PER_HOUR
    assert row["current_entry_price"] == pytest.approx(closes[6])
    assert row["current_entry_scan_prefix_complete"] is True


def test_stop_first_same_bar_ambiguity_and_atr_geometry() -> None:
    # h1 is both anchors.  The next bar reaches the 6% ATR stop and 16% ATR TP;
    # the engine resolves that unknowable intrabar order to the stop.
    hourly = _hourly(
        close_by_hour={1: 98.5, 2: 100.0},
        range_by_hour={2: (115.0, 92.0)},
    )
    features = build_long_feature_tape(
        pl.DataFrame([_feature()]),
        hourly,
        _v11a_long_native_config(),
    )
    entries = append_long_entry_policy(features, hourly, _v11a_long_native_config())
    row = entries.row(0, named=True)

    assert row["fc_exit_param_source"] == "atr"
    assert row["fc_atr_fallback_used"] is False
    assert row["fc_exit_stop_pct"] == pytest.approx(0.06)
    assert row["fc_exit_take_profit_pct"] == pytest.approx(0.16)
    assert "current_stop_price" not in row
    assert "current_take_profit_price" not in row
    assert not any("first_passage" in column for column in features.columns)

    diagnostic = _append_long_first_passage_diagnostics(
        entries,
        hourly,
        _v11a_long_native_config(),
    ).row(0, named=True)
    assert diagnostic["diagnostic_current_stop_first_hour"] == 1
    assert diagnostic["diagnostic_current_take_profit_first_hour"] == 1
    assert diagnostic["diagnostic_current_first_passage_reason"] == "stop"
    assert diagnostic["diagnostic_current_first_passage_hour"] == 1
    assert diagnostic["diagnostic_current_first_passage_ts_ms"] == SIGNAL_TS + 2 * MS_PER_HOUR
    assert diagnostic["diagnostic_current_same_bar_ambiguity"] is True
    assert diagnostic["diagnostic_current_first_passage_prefix_complete"] is True

    minimal = append_long_path_labels(entries, hourly, _v11a_long_native_config()).row(0, named=True)
    assert minimal["current_same_bar_stop_tp_ambiguity"] is True
    assert minimal["current_stop_price"] == pytest.approx(98.5 * 0.94)
    assert minimal["current_take_profit_price"] == pytest.approx(98.5 * 1.16)
    assert not any(
        "first_passage" in column
        for column in append_long_path_labels(entries, hourly, _v11a_long_native_config()).columns
    )


def test_missing_path_is_retained_with_completeness_flags() -> None:
    # The h3 bar after the h1 common anchor is absent, but the h6 endpoint exists.
    hourly = _hourly(skip_hours={4})
    features = build_long_feature_tape(
        pl.DataFrame([_feature()]),
        hourly,
        _v11a_long_native_config(),
    )
    entries = append_long_entry_policy(features, hourly, _v11a_long_native_config())
    row = append_long_exploratory_path_labels(
        entries,
        hourly,
        _v11a_long_native_config(),
        point_horizons=(6,),
        excursion_horizons=(6,),
    ).row(0, named=True)

    assert entries["current_entry_scan_first_hour"].item() == 1
    assert entries["current_entry_scan_end_hour"].item() == 6
    assert entries["current_entry_scan_missing_hour_bitmask"].item() == 1 << 3
    assert entries["current_entry_scan_missing_hour_bitmask"].dtype == pl.Int8
    assert entries["current_entry_scan_prefix_complete"].item() is False
    assert row["common_6h_path_complete"] is False
    assert row["common_6h_observed_bars"] == 5
    assert row["common_6h_endpoint_ts_ms"] == SIGNAL_TS + 7 * MS_PER_HOUR
    assert row["common_6h_point_available"] is True
    assert row["common_6h_hourly_extrema_interval_censored"] is True
    assert row["common_6h_point_return"] == pytest.approx(0.0)
    assert row["common_6h_mfe"] is None
    assert row["common_6h_signed_mae"] is None
    assert row["common_6h_adverse_magnitude"] is None
    assert "path_incomplete:5/6" in row["common_6h_missing_reason"]
    assert row["common_label_complete"] is False
    assert "6h:path_incomplete:5/6" in row["common_missing_path_reason"]

    # Exact engine timing: the ordinary delayed h1 bar is resolved before the
    # sniper scan, so a later retrace cannot rescue an absent h1 bar.
    missing_initial = _run_entry(
        _feature(),
        _hourly(close_by_hour={2: 98.0}, skip_hours={1}),
    )
    assert missing_initial["current_entry_available"] is False
    assert missing_initial["current_entry_reason"] == "initial_entry_bar_missing"
    assert missing_initial["current_entry_policy_available"] is False
    assert missing_initial["current_entry_scan_prefix_complete"] is False
    assert missing_initial["current_entry_missing_reason"] == "initial_entry_bar_missing"


def test_missing_atr_surfaces_fixed_exit_fallback() -> None:
    row = _run_feature(_feature(atr_14d_pct=None), _hourly())

    assert row["gate_atr_cap"] is False
    assert row["first_sequential_rejection_reason"] == "atr_missing"
    assert row["classifier_selected"] is False
    assert row["fc_atr_exit_available"] is False
    assert row["fc_atr_fallback_used"] is True
    assert row["fc_exit_param_source"] == "fixed_fallback_missing_atr"


def test_duplicate_feature_and_hourly_keys_fail_closed() -> None:
    feature = pl.DataFrame([_feature(), _feature()])
    hourly = _hourly()
    with pytest.raises(ValueError, match="daily_features has duplicate"):
        build_long_feature_tape(feature, hourly, _v11a_long_native_config())

    duplicate_hourly = pl.concat([hourly, hourly.head(1)])
    with pytest.raises(ValueError, match="hourly_bars has duplicate"):
        build_long_feature_tape(
            pl.DataFrame([_feature()]),
            duplicate_hourly,
            _v11a_long_native_config(),
        )


def test_signal_feature_tape_is_invariant_to_every_post_signal_bar() -> None:
    cfg = _v11a_long_native_config()
    baseline = _hourly(close_by_hour={1: 100.0, 2: 99.4, 3: 98.9})
    changed_outcomes = baseline.with_columns(
        # A bar with open timestamp SIGNAL_TS ends at h1.  Every such value is
        # post-signal and must be unread by the signal-time feature stage.
        pl.when(pl.col("ts_ms") >= SIGNAL_TS)
        .then(pl.lit(10_000.0))
        .otherwise(pl.col(column))
        .alias(column)
        for column in ("open", "high", "low", "close")
    )

    before = build_long_feature_tape(pl.DataFrame([_feature()]), baseline, cfg)
    after = build_long_feature_tape(pl.DataFrame([_feature()]), changed_outcomes, cfg)
    assert_frame_equal(before, after)
    assert not any(
        token in column
        for column in before.columns
        for token in (
            "common_entry",
            "current_entry",
            "entry_scan",
            "next_hour",
            "point_return",
            "_mfe",
            "_mae",
            "first_passage",
        )
    )


def test_entry_policy_tape_stops_reading_after_current_entry() -> None:
    baseline = _hourly(close_by_hour={1: 100.0, 2: 99.4, 3: 98.9})
    changed_after_entry = baseline.with_columns(
        # Bar end h4 has open timestamp signal+h3.  The close retrace enters at
        # h3, so h4 and every later OHLC value must be unread.
        pl.when(pl.col("ts_ms") >= SIGNAL_TS + 3 * MS_PER_HOUR)
        .then(pl.lit(10_000.0))
        .otherwise(pl.col(column))
        .alias(column)
        for column in ("open", "high", "low", "close")
    )
    before = _entry_tape(_feature(), baseline)
    after = _entry_tape(_feature(), changed_after_entry)
    assert before["current_entry_hour"].item() == 3
    assert before["current_entry_scan_first_hour"].item() == 1
    assert before["current_entry_scan_end_hour"].item() == 3
    assert_frame_equal(before, after)


def test_default_minimal_labels_and_legacy_wrapper_fail_closed() -> None:
    cfg = _v11a_long_native_config()
    hourly = _hourly()
    features = build_long_feature_tape(pl.DataFrame([_feature()]), hourly, cfg)
    with pytest.raises(ValueError, match="entry_tape missing required columns"):
        append_long_path_labels(features, hourly, cfg)
    entries = append_long_entry_policy(features, hourly, cfg)
    labeled = append_long_path_labels(entries, hourly, cfg)

    with pytest.raises(ValueError, match="only permits frozen horizons"):
        append_long_path_labels(
            entries,
            hourly,
            cfg,
            point_horizons=(6,),
            excursion_horizons=(6,),
        )

    for prefix in ("common", "current"):
        assert f"{prefix}_1h_point_return" in labeled.columns
        assert f"{prefix}_24h_point_return" in labeled.columns
        assert f"{prefix}_72h_point_return" in labeled.columns
        assert f"{prefix}_24h_mfe" in labeled.columns
        assert f"{prefix}_72h_signed_mae" in labeled.columns
        assert f"{prefix}_72h_adverse_magnitude" in labeled.columns
        assert labeled[f"{prefix}_72h_adverse_magnitude"].item() == pytest.approx(
            -labeled[f"{prefix}_72h_signed_mae"].item()
        )
        for horizon in (1, 24, 72):
            assert labeled[f"{prefix}_{horizon}h_endpoint_ts_ms"].item() is not None
            assert labeled[f"{prefix}_{horizon}h_observed_bars"].item() == horizon
            assert labeled[f"{prefix}_{horizon}h_point_available"].item() is True
            assert labeled[f"{prefix}_{horizon}h_path_complete"].item() is True
            assert labeled[f"{prefix}_{horizon}h_missing_reason"].item() is None
            assert labeled[f"{prefix}_{horizon}h_hourly_extrema_interval_censored"].item() is True
        assert f"{prefix}_6h_point_return" not in labeled.columns
    assert not any("first_passage" in column for column in labeled.columns)
    assert "fc_exit_stop_pct" not in labeled.columns
    assert "current_entry_price" not in labeled.columns
    assert labeled.columns == [field.name for field in ARTIFACT_SCHEMAS[LONG_LABEL_SCHEMA_ID].fields]

    with pytest.raises(TypeError, match="no longer appends labels implicitly"):
        build_long_population_tape(pl.DataFrame([_feature()]), hourly, cfg)


def test_signal_key_alias_must_match_and_stage_empty_schemas_are_typed() -> None:
    cfg = _v11a_long_native_config()
    with pytest.raises(ValueError, match="signal_ts_ms must equal ts_ms"):
        build_long_feature_tape(
            pl.DataFrame([_feature(signal_ts_ms=SIGNAL_TS + 1)]),
            _hourly(),
            cfg,
        )

    daily_empty = pl.DataFrame(
        schema={"symbol": pl.String, "ts_ms": pl.Int64, "close": pl.Float64}
    )
    hourly_empty = pl.DataFrame(
        schema={
            "symbol": pl.String,
            "ts_ms": pl.Int64,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        }
    )
    features = build_long_feature_tape(daily_empty, hourly_empty, cfg)
    assert "ts_ms" not in features.columns
    assert features.schema["signal_ts_ms"] == pl.Int64
    assert features.schema["simple_return_1d"] == pl.Float64
    assert features.schema["fc_trigger_bitmask"] == pl.Int8
    assert features.schema["fc_trigger_identities"] == pl.List(pl.String)
    assert features.schema["classifier_eligible"] == pl.Boolean
    assert features.schema["intraday_feature_available"] == pl.Boolean

    entries = append_long_entry_policy(features, hourly_empty, cfg)
    assert entries.schema["current_entry_scan_missing_hour_bitmask"] == pl.Int8
    assert entries.schema["current_entry_scan_prefix_complete"] == pl.Boolean
    assert entries.schema["current_entry_policy_available"] == pl.Boolean

    labels = append_long_path_labels(entries, hourly_empty, cfg)
    assert labels.schema["common_1h_endpoint_ts_ms"] == pl.Int64
    assert labels.schema["common_1h_observed_bars"] == pl.Int64
    assert labels.schema["common_1h_point_available"] == pl.Boolean
    assert labels.schema["common_1h_path_complete"] == pl.Boolean
    assert labels.schema["common_1h_hourly_extrema_interval_censored"] == pl.Boolean
    assert labels.schema["common_24h_signed_mae"] == pl.Float64
    assert labels.schema["common_24h_adverse_magnitude"] == pl.Float64
    assert labels.columns[:2] == ["symbol", "signal_ts_ms"]


def test_every_frozen_stage_requires_the_exact_runtime_v11a_config() -> None:
    cfg = _v11a_long_native_config()
    mutated = replace(cfg, cost_multiplier=cfg.cost_multiplier + 1.0)
    hourly = _hourly()
    daily = pl.DataFrame([_feature()])

    with pytest.raises(ValueError, match="exact _v11a_long_native_config.*cost_multiplier"):
        build_long_feature_tape(daily, hourly, mutated)
    with pytest.raises(ValueError, match="exact _v11a_long_native_config"):
        build_long_feature_tape(daily, hourly, LongNativeConfig())
    with pytest.raises(TypeError):
        build_long_feature_tape(daily, hourly)  # type: ignore[call-arg]

    features = build_long_feature_tape(daily, hourly, cfg)
    with pytest.raises(ValueError, match="exact _v11a_long_native_config.*cost_multiplier"):
        append_long_entry_policy(features, hourly, mutated)
    entries = append_long_entry_policy(features, hourly, cfg)
    with pytest.raises(ValueError, match="exact _v11a_long_native_config.*cost_multiplier"):
        append_long_path_labels(entries, hourly, mutated)


def test_consumed_keys_enforce_grid_ohlc_and_daily_close_parity_only_when_read() -> None:
    cfg = _v11a_long_native_config()
    hourly = _hourly(close_by_hour={1: 100.0, 2: 99.4, 3: 98.9})

    within_tolerance = build_long_feature_tape(
        pl.DataFrame([_feature(close=100.0 + 5e-11)]),
        hourly,
        cfg,
    )
    assert within_tolerance.height == 1
    with pytest.raises(ValueError, match="close does not match the exact signal-hour close"):
        build_long_feature_tape(
            pl.DataFrame([_feature(close=100.0 + 1e-6)]),
            hourly,
            cfg,
        )

    with pytest.raises(ValueError, match="not aligned"):
        build_long_feature_tape(
            pl.DataFrame([_feature(ts_ms=SIGNAL_TS + 1)]),
            hourly,
            cfg,
        )
    with pytest.raises(ValueError, match="must be non-negative"):
        build_long_feature_tape(
            pl.DataFrame([_feature(ts_ms=-MS_PER_DAY)]),
            hourly,
            cfg,
        )
    with pytest.raises(ValueError, match="non-empty canonical string"):
        build_long_feature_tape(
            pl.DataFrame([_feature(symbol=" AAAUSDT")]),
            hourly,
            cfg,
        )
    with pytest.raises(ValueError, match="integer millisecond timestamp"):
        build_long_feature_tape(
            pl.DataFrame([_feature()]),
            hourly.with_columns(pl.col("ts_ms").cast(pl.Float64)),
            cfg,
        )

    malformed_signal_bar = hourly.with_columns(
        pl.when(pl.col("ts_ms") == SIGNAL_TS - MS_PER_HOUR)
        .then(pl.lit(99.0))
        .otherwise(pl.col("high"))
        .alias("high")
    )
    with pytest.raises(ValueError, match="OHLC outside"):
        build_long_feature_tape(pl.DataFrame([_feature()]), malformed_signal_bar, cfg)

    # Key integrity is global, but OHLC validity is consumed-key-only.  This
    # unrelated row has a valid unique key and intentionally malformed values.
    unrelated_bad_bar = pl.DataFrame(
        [
            {
                "ts_ms": SIGNAL_TS + 100 * MS_PER_HOUR,
                "symbol": "OTHERUSDT",
                "open": None,
                "high": None,
                "low": None,
                "close": None,
            }
        ],
        schema={
            "ts_ms": pl.Int64,
            "symbol": pl.String,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
    )
    with_unrelated_bad = pl.concat([hourly, unrelated_bad_bar])
    features = build_long_feature_tape(pl.DataFrame([_feature()]), with_unrelated_bad, cfg)
    assert features.height == 1

    # The current anchor fires at h3, so malformed h4 OHLC is outside S03's
    # consumed prefix and cannot invalidate or alter the entry reconstruction.
    malformed_after_entry = hourly.with_columns(
        pl.when(pl.col("ts_ms") == SIGNAL_TS + 3 * MS_PER_HOUR)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("high"))
        .alias("high")
    )
    entry = append_long_entry_policy(features, malformed_after_entry, cfg)
    assert entry["current_entry_hour"].item() == 3


def test_stage_identity_hold_and_anchor_geometry_fail_closed_on_null_or_mutation() -> None:
    cfg = _v11a_long_native_config()
    hourly = _hourly()
    features = build_long_feature_tape(pl.DataFrame([_feature()]), hourly, cfg)

    null_version = features.with_columns(
        pl.lit(None, dtype=pl.String).alias("long_feature_tape_schema_version")
    )
    with pytest.raises(ValueError, match="long_feature_tape_schema_version must equal"):
        append_long_entry_policy(null_version, hourly, cfg)
    null_hold = features.with_columns(
        pl.lit(None, dtype=pl.Int64).alias("fc_exit_max_hold_hours")
    )
    with pytest.raises(ValueError, match="fc_exit_max_hold_hours must equal"):
        append_long_entry_policy(null_hold, hourly, cfg)
    null_key = features.with_columns(
        pl.lit(None, dtype=pl.Int64).alias("signal_ts_ms")
    )
    with pytest.raises(ValueError, match="integer millisecond timestamp"):
        append_long_entry_policy(null_key, hourly, cfg)

    bad_exit = features.with_columns(
        (pl.col("fc_exit_stop_pct") + 0.001).alias("fc_exit_stop_pct")
    )
    with pytest.raises(ValueError, match="exit percentages do not match"):
        append_long_entry_policy(bad_exit, hourly, cfg)

    entries = append_long_entry_policy(features, hourly, cfg)
    null_entry_version = entries.with_columns(
        pl.lit(None, dtype=pl.String).alias("long_entry_policy_schema_version")
    )
    with pytest.raises(ValueError, match="long_entry_policy_schema_version must equal"):
        append_long_path_labels(null_entry_version, hourly, cfg)

    mutated_anchor = entries.with_columns(
        (pl.col("current_entry_price") + 1.0).alias("current_entry_price")
    )
    with pytest.raises(ValueError, match="current_entry_price does not match reconstructed"):
        append_long_path_labels(mutated_anchor, hourly, cfg)


def test_hourly_key_integrity_and_duplicates_are_global_but_ohlc_is_lazy() -> None:
    cfg = _v11a_long_native_config()
    daily = pl.DataFrame([_feature()])
    hourly = _hourly()
    schema = {
        "ts_ms": pl.Int64,
        "symbol": pl.String,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
    }

    def key_only_row(*, symbol: str | None, ts_ms: int | None) -> pl.DataFrame:
        return pl.DataFrame(
            [
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": None,
                }
            ],
            schema=schema,
        )

    invalid_cases = (
        (key_only_row(symbol="OTHERUSDT", ts_ms=SIGNAL_TS + 1), "not aligned"),
        (key_only_row(symbol="OTHERUSDT", ts_ms=None), "integer millisecond timestamp"),
        (key_only_row(symbol="", ts_ms=SIGNAL_TS), "non-empty canonical string"),
        (key_only_row(symbol=None, ts_ms=SIGNAL_TS), "non-empty canonical string"),
        (key_only_row(symbol="OTHERUSDT", ts_ms=-MS_PER_HOUR), "must be non-negative"),
    )
    for invalid, message in invalid_cases:
        with pytest.raises(ValueError, match=message):
            build_long_feature_tape(daily, pl.concat([hourly, invalid]), cfg)

    duplicate_unrelated = key_only_row(
        symbol="OTHERUSDT",
        ts_ms=SIGNAL_TS + 100 * MS_PER_HOUR,
    )
    with pytest.raises(ValueError, match="hourly_bars has duplicate"):
        build_long_feature_tape(
            daily,
            pl.concat([hourly, duplicate_unrelated, duplicate_unrelated]),
            cfg,
        )

    # One unrelated valid key with null OHLC is accepted because no stage reads
    # its values; global validation is intentionally limited to key integrity.
    accepted = build_long_feature_tape(
        daily,
        pl.concat([hourly, duplicate_unrelated]),
        cfg,
    )
    assert accepted.height == 1


def test_outcome_like_caller_columns_are_rejected_at_each_boundary() -> None:
    cfg = _v11a_long_native_config()
    hourly = _hourly()
    with pytest.raises(ValueError, match="outcome-like caller columns.*forward_72h_return"):
        build_long_feature_tape(
            pl.DataFrame([_feature(forward_72h_return=0.5)]),
            hourly,
            cfg,
        )
    with pytest.raises(ValueError, match="outcome-like caller columns.*72h_return"):
        build_long_feature_tape(
            pl.DataFrame([_feature(**{"72h_return": 0.5})]),
            hourly,
            cfg,
        )

    features = build_long_feature_tape(pl.DataFrame([_feature()]), hourly, cfg)
    with pytest.raises(ValueError, match="outcome-like caller columns.*next_hour_close"):
        append_long_entry_policy(
            features.with_columns(pl.lit(101.0).alias("next_hour_close")),
            hourly,
            cfg,
        )

    entries = append_long_entry_policy(features, hourly, cfg)
    with pytest.raises(ValueError, match="outcome-like caller columns.*future_return"):
        append_long_path_labels(
            entries.with_columns(pl.lit(0.1).alias("future_return")),
            hourly,
            cfg,
        )


def test_intraday_feature_availability_is_explicit_even_when_trigger_disabled() -> None:
    unavailable = _run_feature(_feature(intra_max_Nh_pump_log=None), _hourly())
    available = _run_feature(_feature(intra_max_Nh_pump_log=0.25), _hourly())

    assert unavailable["intraday_feature_available"] is False
    assert available["intraday_feature_available"] is True
    assert available["fc_trigger_intraday"] is False
