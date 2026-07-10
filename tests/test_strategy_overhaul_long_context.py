from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native_event_demo import _v11a_long_native_config
from liquidity_migration.long_population_scout import build_long_feature_tape
from liquidity_migration.strategy_overhaul_long_context import (
    BTC_MONTH_CONTEXT_SCHEMA,
    LONG_SOURCE_CONTEXT_DIAGNOSTICS,
    LONG_SOURCE_CONTEXT_EVIDENCE_STATUS,
    RANK_METADATA_COLUMNS,
    REGIME_CONTEXT_SCHEMA,
    SOURCE_AVAILABILITY_SCHEMA,
    LongSourceContextError,
    attach_long_source_context,
)


SIGNAL_TS = 100 * MS_PER_DAY


def _feature_tape() -> pl.DataFrame:
    today_turnover = {
        "AAAUSDT": 100.0,
        "BBBUSDT": 100.0,
        "CCCUSDT": None,
    }
    today_rank = {"AAAUSDT": 1, "BBBUSDT": 2, "CCCUSDT": None}
    trailing_turnover = {"AAAUSDT": 10.0, "BBBUSDT": 20.0, "CCCUSDT": 20.0}
    universe_rank = {"AAAUSDT": 3, "BBBUSDT": 1, "CCCUSDT": 2}
    rows: list[dict[str, object]] = []
    hourly_rows: list[dict[str, object]] = []
    for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        rows.append(
            {
                "symbol": symbol,
                "ts_ms": SIGNAL_TS,
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "close": 100.0,
                "turnover_quote": today_turnover[symbol],
                "hourly_bars": 24,
                "turnover_median_90d": trailing_turnover[symbol],
                "today_volume_rank": today_rank[symbol],
                "universe_rank": universe_rank[symbol],
                "symbol_age_days": 100,
                "in_universe": True,
                "regime_on": True,
                "eth_regime_on": False,
                "btc_sma_dist": 0.2,
                "log_return": 0.2,
                "sigma_daily_30d": 0.04,
                "close_location": 0.8,
                "pump_3d_log": 0.3,
                "close_loc_3d": 0.8,
                "pump_7d_log": 0.4,
                "close_loc_7d": 0.8,
                "atr_14d_pct": 0.04,
            }
        )
        hourly_rows.append(
            {
                "symbol": symbol,
                "ts_ms": SIGNAL_TS - MS_PER_HOUR,
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
            }
        )
    daily = pl.DataFrame(
        rows,
        schema_overrides={
            "symbol": pl.String,
            "ts_ms": pl.Int64,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "turnover_quote": pl.Float64,
            "hourly_bars": pl.UInt32,
            "turnover_median_90d": pl.Float64,
            "today_volume_rank": pl.UInt32,
            "universe_rank": pl.UInt32,
            "symbol_age_days": pl.Int64,
        },
    )
    hourly = pl.DataFrame(
        hourly_rows,
        schema_overrides={
            "symbol": pl.String,
            "ts_ms": pl.Int64,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
    )
    return build_long_feature_tape(daily, hourly, _v11a_long_native_config())


def _availability(feature_tape: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for symbol, signal_ts_ms in feature_tape.select("symbol", "signal_ts_ms").iter_rows():
        rows.append(
            {
                "symbol": symbol,
                "signal_ts_ms": signal_ts_ms,
                "signal_feature_available_ts_ms": signal_ts_ms,
                "daily_bar_available_ts_ms": signal_ts_ms,
                "btc_context_available_ts_ms": signal_ts_ms - 3,
                "eth_context_available_ts_ms": None,
                "btc_month_context_available_ts_ms": signal_ts_ms - 2,
            }
        )
    return pl.DataFrame(rows, schema=dict(SOURCE_AVAILABILITY_SCHEMA))


def _regime_context(*, btc_available: bool = True) -> pl.DataFrame:
    row = {
        "signal_ts_ms": SIGNAL_TS,
        "btc_close": 120.0 if btc_available else None,
        "btc_sma_30d": 100.0 if btc_available else None,
        "btc_sma_dist": 0.2 if btc_available else None,
        "btc_regime_available": btc_available,
        "btc_regime_pass": True if btc_available else None,
        "eth_close": None,
        "eth_sma_30d": None,
        "eth_sma_dist": None,
        "eth_regime_available": False,
        "eth_regime_pass": None,
    }
    return pl.DataFrame([row], schema=dict(REGIME_CONTEXT_SCHEMA))


def _btc_month_context(*, available: bool = True) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "signal_ts_ms": SIGNAL_TS,
                "btc_month_regime_value": 0.05 if available else None,
                "btc_month_regime_available": available,
                "btc_month_regime_pass": True if available else None,
            }
        ],
        schema=dict(BTC_MONTH_CONTEXT_SCHEMA),
    )


def _run(
    feature_tape: pl.DataFrame | None = None,
    *,
    availability: pl.DataFrame | None = None,
    regime_context: pl.DataFrame | None = None,
    btc_month_context: pl.DataFrame | None = None,
) -> pl.DataFrame:
    feature_tape = _feature_tape() if feature_tape is None else feature_tape
    return attach_long_source_context(
        feature_tape,
        source_availability=(_availability(feature_tape) if availability is None else availability),
        regime_context=_regime_context() if regime_context is None else regime_context,
        btc_month_context=(_btc_month_context() if btc_month_context is None else btc_month_context),
    )


def test_attaches_registered_context_and_exact_rank_receipts() -> None:
    feature = _feature_tape()
    output = _run(feature)

    assert output.select("symbol", "signal_ts_ms").equals(feature.select("symbol", "signal_ts_ms"))
    assert output.height == feature.height
    assert set(RANK_METADATA_COLUMNS) <= set(output.columns)

    rows = {row["symbol"]: row for row in output.iter_rows(named=True)}
    for row in rows.values():
        assert row["today_volume_rank_population_peer_count"] == 3
        assert row["today_volume_rank_rankable_peer_count"] == 2
        assert row["today_volume_rank_missing_peer_count"] == 1
        assert row["universe_rank_population_peer_count"] == 3
        assert row["universe_rank_rankable_peer_count"] == 3
        assert row["universe_rank_missing_peer_count"] == 0
        assert row["today_volume_rank_tie_method"] == "ordinal_descending"
        assert row["universe_rank_tie_method"] == "ordinal_descending"
        assert row["today_volume_rank_denominator_rule"] == "supplied_signal_ts_population"
        assert row["universe_rank_denominator_rule"] == "supplied_signal_ts_population"
        assert row["btc_regime_available"] is True
        assert row["regime_on"] is True
        assert row["btc_sma_dist"] == pytest.approx(0.2)
        assert row["eth_regime_available"] is False
        assert row["eth_regime_on"] is False
        assert row["eth_sma_dist"] is None
        assert row["btc_month_regime_value"] == pytest.approx(0.05)
        assert row["btc_month_regime_available"] is True
        assert row["btc_month_regime_pass"] is True

    assert rows["AAAUSDT"]["today_volume_rank_tie_count"] == 2
    assert rows["BBBUSDT"]["today_volume_rank_tie_count"] == 2
    assert rows["CCCUSDT"]["today_volume_rank_tie_count"] is None
    assert rows["AAAUSDT"]["universe_rank_tie_count"] == 1
    assert rows["BBBUSDT"]["universe_rank_tie_count"] == 2
    assert rows["CCCUSDT"]["universe_rank_tie_count"] == 2

    assert "btc_close" not in output.columns
    assert "btc_sma_30d" not in output.columns
    assert "eth_close" not in output.columns
    assert "eth_sma_30d" not in output.columns
    assert LONG_SOURCE_CONTEXT_EVIDENCE_STATUS.startswith("DIAGNOSTIC_ONLY")
    assert LONG_SOURCE_CONTEXT_DIAGNOSTICS["sidecar_provenance_verified"] is False
    assert LONG_SOURCE_CONTEXT_DIAGNOSTICS["pit_population_completeness_verified"] is False


def test_unknown_regime_and_month_context_are_not_turned_into_observed_fails() -> None:
    feature = _feature_tape()
    availability = _availability(feature).with_columns(
        pl.lit(None, dtype=pl.Int64).alias("btc_context_available_ts_ms"),
        pl.lit(None, dtype=pl.Int64).alias("btc_month_context_available_ts_ms"),
    )
    output = _run(
        feature,
        availability=availability,
        regime_context=_regime_context(btc_available=False),
        btc_month_context=_btc_month_context(available=False),
    )

    assert output["btc_regime_available"].to_list() == [False, False, False]
    assert output["regime_on"].to_list() == [False, False, False]
    assert output["btc_sma_dist"].to_list() == [None, None, None]
    assert output["btc_month_regime_available"].to_list() == [False, False, False]
    assert output["btc_month_regime_pass"].to_list() == [None, None, None]
    assert output["gate_btc_month_regime"].to_list() == [True, True, True]


def test_rank_parity_fails_closed() -> None:
    feature = _feature_tape().with_columns(
        pl.when(pl.col("symbol") == "AAAUSDT")
        .then(pl.lit(2, dtype=pl.UInt32))
        .otherwise(pl.col("today_volume_rank"))
        .alias("today_volume_rank")
    )
    with pytest.raises(LongSourceContextError, match="today_volume_rank.*parity failed"):
        _run(feature)


def test_future_availability_and_incomplete_key_coverage_fail_closed() -> None:
    feature = _feature_tape()
    future = _availability(feature).with_columns((pl.col("signal_ts_ms") + 1).alias("daily_bar_available_ts_ms"))
    with pytest.raises(LongSourceContextError, match="availability timestamp"):
        _run(feature, availability=future)

    with pytest.raises(LongSourceContextError, match="keys must exactly equal"):
        _run(feature, availability=_availability(feature).head(2))


@pytest.mark.parametrize(
    "column",
    ("daily_bar_available_ts_ms", "signal_feature_available_ts_ms"),
)
def test_required_availability_timestamps_cannot_be_null(column: str) -> None:
    feature = _feature_tape()
    availability = _availability(feature).with_columns(pl.lit(None, dtype=pl.Int64).alias(column))
    with pytest.raises(LongSourceContextError, match="must be non-null"):
        _run(feature, availability=availability)


def test_signal_feature_availability_must_equal_declared_source_max() -> None:
    feature = _feature_tape()
    availability = _availability(feature).with_columns(
        (pl.col("signal_feature_available_ts_ms") - 1).alias("signal_feature_available_ts_ms")
    )
    with pytest.raises(LongSourceContextError, match="maximum non-null declared"):
        _run(feature, availability=availability)


def test_regime_and_month_semantics_fail_closed() -> None:
    bad_distance = _regime_context().with_columns(pl.lit(0.1, dtype=pl.Float64).alias("btc_sma_dist"))
    with pytest.raises(LongSourceContextError, match="distance parity"):
        _run(regime_context=bad_distance)

    bad_month = _btc_month_context().with_columns(pl.lit(False, dtype=pl.Boolean).alias("btc_month_regime_pass"))
    with pytest.raises(LongSourceContextError, match="gate is off"):
        _run(btc_month_context=bad_month)


def test_outcome_columns_and_noncanonical_feature_types_are_refused() -> None:
    feature = _feature_tape().with_columns(pl.lit(0.1, dtype=pl.Float64).alias("future_return_24h"))
    with pytest.raises(LongSourceContextError, match="outcome-like"):
        _run(feature)

    wrong_rank_type = _feature_tape().with_columns(pl.col("today_volume_rank").cast(pl.Float64))
    with pytest.raises(LongSourceContextError, match="invalid dtypes"):
        _run(wrong_rank_type)


def test_strict_typed_empty_output() -> None:
    feature = _feature_tape().head(0)
    output = attach_long_source_context(
        feature,
        source_availability=pl.DataFrame(schema=dict(SOURCE_AVAILABILITY_SCHEMA)),
        regime_context=pl.DataFrame(schema=dict(REGIME_CONTEXT_SCHEMA)),
        btc_month_context=pl.DataFrame(schema=dict(BTC_MONTH_CONTEXT_SCHEMA)),
    )

    assert output.is_empty()
    assert output.schema["signal_feature_available_ts_ms"] == pl.Int64
    assert output.schema["btc_regime_available"] == pl.Boolean
    assert output.schema["eth_sma_dist"] == pl.Float64
    assert output.schema["btc_month_regime_pass"] == pl.Boolean
    assert output.schema["today_volume_rank"] == pl.UInt32
    assert output.schema["today_volume_rank_population_peer_count"] == pl.Int64
    assert output.schema["today_volume_rank_tie_count"] == pl.Int64
    assert output.schema["today_volume_rank_tie_method"] == pl.String
    assert output.schema["universe_rank_denominator_rule"] == pl.String


def test_sidecars_are_exact_projections() -> None:
    feature = _feature_tape()
    availability = _availability(feature).with_columns(pl.lit(1, dtype=pl.Int64).alias("future_pnl"))
    with pytest.raises(LongSourceContextError, match="projection mismatch"):
        _run(feature, availability=availability)
