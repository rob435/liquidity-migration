from __future__ import annotations

import datetime as dt
from dataclasses import replace

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native import build_long_features
from liquidity_migration.long_native_event_demo import _v11a_long_native_config
from liquidity_migration.long_population_scout import build_long_feature_tape
from liquidity_migration.strategy_overhaul_long_context import (
    BTC_MONTH_CONTEXT_SCHEMA,
    REGIME_CONTEXT_SCHEMA,
    SOURCE_AVAILABILITY_SCHEMA,
    attach_long_source_context,
)
from liquidity_migration.strategy_overhaul_long_sidecars import (
    LONG_A0_SIDECAR_AVAILABILITY_SEMANTICS,
    LongA0SidecarError,
    build_long_a0_sidecars,
)


START_TS_MS = 20_000 * MS_PER_DAY


def _hourly_source(
    *,
    days: int = 42,
    symbols: tuple[str, ...] = ("AAAUSDT", "BTCUSDT", "ETHUSDT"),
) -> pl.DataFrame:
    bases = {"AAAUSDT": 10.0, "BTCUSDT": 100.0, "ETHUSDT": 50.0}
    rows: list[dict[str, object]] = []
    for day in range(days):
        day_start = START_TS_MS + day * MS_PER_DAY
        date = dt.datetime.fromtimestamp(day_start / 1000, tz=dt.UTC).date().isoformat()
        for symbol in symbols:
            base = bases[symbol]
            for hour in range(24):
                close = base + day * (0.30 if symbol == "BTCUSDT" else 0.15) + hour * 0.001
                rows.append(
                    {
                        "symbol": symbol,
                        "ts_ms": day_start + hour * MS_PER_HOUR,
                        "date": date,
                        "open": close - 0.002,
                        "high": close + 0.01,
                        "low": close - 0.01,
                        "close": close,
                        "turnover_quote": 1_000.0 + day * 10.0 + hour + base,
                        "volume_base": 10.0 + hour / 10.0,
                    }
                )
    return pl.DataFrame(
        rows,
        schema_overrides={
            "symbol": pl.String,
            "ts_ms": pl.Int64,
            "date": pl.String,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "turnover_quote": pl.Float64,
            "volume_base": pl.Float64,
        },
    )


def _built_features(hourly: pl.DataFrame) -> pl.DataFrame:
    return build_long_features(
        hourly,
        funding=pl.DataFrame(),
        config=_v11a_long_native_config(),
    )


@pytest.fixture(scope="module")
def source_and_features() -> tuple[pl.DataFrame, pl.DataFrame]:
    # Retain enough warmup for the production 90d universe fields so the
    # downstream source-context adapter sees its ordinary non-null rank dtypes.
    hourly = _hourly_source(days=100)
    all_features = _built_features(hourly)
    features = all_features.filter(
        (pl.col("ts_ms") >= START_TS_MS + 93 * MS_PER_DAY) & (pl.col("ts_ms") < START_TS_MS + 96 * MS_PER_DAY)
    ).sort(["ts_ms", "symbol"])
    assert features.height == 9
    return hourly, features


def test_builds_exact_source_sidecars_and_integrates_with_context_adapter(
    source_and_features: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    hourly, features = source_and_features
    config = _v11a_long_native_config()
    bundle = build_long_a0_sidecars(hourly, features, config=config)

    assert bundle.source_availability.schema == dict(SOURCE_AVAILABILITY_SCHEMA)
    assert bundle.regime_context.schema == dict(REGIME_CONTEXT_SCHEMA)
    assert bundle.btc_month_context.schema == dict(BTC_MONTH_CONTEXT_SCHEMA)
    assert bundle.source_availability.height == features.height
    assert bundle.regime_context.height == features["ts_ms"].n_unique()
    assert bundle.btc_month_context.height == features["ts_ms"].n_unique()

    assert bundle.source_availability.select("symbol", "signal_ts_ms").equals(
        features.select("symbol", pl.col("ts_ms").alias("signal_ts_ms"))
    )
    assert bundle.source_availability["signal_feature_available_ts_ms"].equals(
        bundle.source_availability["signal_ts_ms"]
    )
    assert bundle.source_availability["daily_bar_available_ts_ms"].equals(bundle.source_availability["signal_ts_ms"])
    assert bundle.source_availability["btc_context_available_ts_ms"].equals(bundle.source_availability["signal_ts_ms"])
    assert bundle.source_availability["eth_context_available_ts_ms"].equals(bundle.source_availability["signal_ts_ms"])
    assert bundle.source_availability["btc_month_context_available_ts_ms"].equals(
        bundle.source_availability["signal_ts_ms"]
    )
    assert bundle.regime_context["btc_regime_available"].to_list() == [True, True, True]
    assert bundle.regime_context["eth_regime_available"].to_list() == [True, True, True]
    assert bundle.btc_month_context["btc_month_regime_available"].to_list() == [True, True, True]
    assert bundle.btc_month_context["btc_month_regime_pass"].to_list() == [True, True, True]

    feature_month = features.select("ts_ms", "btc_month_ret_30d").unique().sort("ts_ms")["btc_month_ret_30d"].to_list()
    assert bundle.btc_month_context["btc_month_regime_value"].to_list() == pytest.approx(feature_month)

    feature_tape = build_long_feature_tape(
        features,
        hourly.select("symbol", "ts_ms", "open", "high", "low", "close"),
        config,
    )
    contextual = attach_long_source_context(
        feature_tape,
        config=config,
        source_availability=bundle.source_availability,
        regime_context=bundle.regime_context,
        btc_month_context=bundle.btc_month_context,
    )
    assert contextual.height == features.height
    assert contextual["btc_regime_available"].to_list() == [True] * features.height
    assert contextual["eth_regime_available"].to_list() == [True] * features.height

    receipt = bundle.receipt
    assert receipt["availability_semantics"] == LONG_A0_SIDECAR_AVAILABILITY_SEMANTICS
    assert receipt["feature_population_row_count"] == features.height
    assert receipt["btc_month_regime_mode"] == "daily_30d"
    assert receipt["actual_vendor_publication_time_claimed"] is False
    assert receipt["historical_ingestion_time_claimed"] is False
    assert receipt["operational_latency_claimed"] is False
    assert receipt["post_latest_signal_bars_read"] is False
    assert receipt["post_row_signal_bar_values_used"] is False
    assert receipt["raw_ohlc_across_registered_s02_window_read_and_hashed"] is True
    assert receipt["outcome_labels_or_metrics_calculated"] is False
    assert receipt["root_snapshot_identity_bound"] is False
    assert all(
        len(str(receipt[name])) == 64
        for name in (
            "canonical_config_sha256",
            "registered_scope_sha256",
            "config_identity_sha256",
        )
    )
    assert len(str(receipt["artifact_sha256"])) == 64


def test_missing_btc_and_eth_remain_null_with_false_availability() -> None:
    hourly = _hourly_source(symbols=("AAAUSDT",))
    features = _built_features(hourly).filter(
        (pl.col("symbol") == "AAAUSDT") & (pl.col("ts_ms") == START_TS_MS + 35 * MS_PER_DAY)
    )
    bundle = build_long_a0_sidecars(
        hourly,
        features,
        config=_v11a_long_native_config(),
    )

    regime = bundle.regime_context.row(0, named=True)
    assert regime["btc_regime_available"] is False
    assert regime["btc_close"] is None
    assert regime["btc_sma_30d"] is None
    assert regime["btc_sma_dist"] is None
    assert regime["btc_regime_pass"] is None
    assert regime["eth_regime_available"] is False
    assert regime["eth_close"] is None
    assert regime["eth_sma_30d"] is None
    assert regime["eth_sma_dist"] is None
    assert regime["eth_regime_pass"] is None

    month = bundle.btc_month_context.row(0, named=True)
    assert month["btc_month_regime_available"] is False
    assert month["btc_month_regime_value"] is None
    assert month["btc_month_regime_pass"] is None
    availability = bundle.source_availability.row(0, named=True)
    assert availability["daily_bar_available_ts_ms"] == availability["signal_ts_ms"]
    assert availability["btc_context_available_ts_ms"] is None
    assert availability["eth_context_available_ts_ms"] is None
    assert availability["btc_month_context_available_ts_ms"] is None


def test_calendar_gap_nulls_regime_instead_of_stretching_the_30d_window() -> None:
    hourly = _hourly_source()
    missing_btc_day_start = START_TS_MS + 20 * MS_PER_DAY
    hourly = hourly.filter(
        ~(
            (pl.col("symbol") == "BTCUSDT")
            & (pl.col("ts_ms") >= missing_btc_day_start)
            & (pl.col("ts_ms") < missing_btc_day_start + MS_PER_DAY)
        )
    )
    signal_ts_ms = START_TS_MS + 35 * MS_PER_DAY
    features = _built_features(hourly).filter((pl.col("symbol") == "AAAUSDT") & (pl.col("ts_ms") == signal_ts_ms))
    assert features["regime_on"].to_list() == [False]
    assert features["btc_sma_dist"].to_list() == [0.0]

    bundle = build_long_a0_sidecars(
        hourly,
        features,
        config=_v11a_long_native_config(),
    )
    regime = bundle.regime_context.row(0, named=True)
    assert regime["btc_regime_available"] is False
    assert regime["btc_close"] is None
    assert regime["btc_sma_30d"] is None
    assert regime["btc_sma_dist"] is None
    assert regime["btc_regime_pass"] is None


@pytest.mark.parametrize(
    ("column", "expression", "message"),
    (
        ("regime_on", ~pl.col("regime_on"), "regime_on parity"),
        ("eth_regime_on", ~pl.col("eth_regime_on"), "eth_regime_on parity"),
        ("btc_sma_dist", pl.col("btc_sma_dist") + 0.01, "btc_sma_dist parity"),
        (
            "btc_month_ret_30d",
            pl.col("btc_month_ret_30d") + 0.01,
            "btc_month_ret_30d parity",
        ),
    ),
)
def test_feature_context_mutations_fail_parity(
    source_and_features: tuple[pl.DataFrame, pl.DataFrame],
    column: str,
    expression: pl.Expr,
    message: str,
) -> None:
    hourly, features = source_and_features
    mutated = features.with_columns(expression.alias(column))
    with pytest.raises(LongA0SidecarError, match=message):
        build_long_a0_sidecars(
            hourly,
            mutated,
            config=_v11a_long_native_config(),
        )


def test_relevant_daily_source_mutation_fails_parity(
    source_and_features: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    hourly, features = source_and_features
    signal_ts_ms = int(features["ts_ms"].min())
    last_hour_open = signal_ts_ms - MS_PER_HOUR
    mutated = hourly.with_columns(
        pl.when((pl.col("symbol") == "AAAUSDT") & (pl.col("ts_ms") == last_hour_open))
        .then(pl.col("close") + 1.0)
        .otherwise(pl.col("close"))
        .alias("close"),
        pl.when((pl.col("symbol") == "AAAUSDT") & (pl.col("ts_ms") == last_hour_open))
        .then(pl.col("high") + 1.0)
        .otherwise(pl.col("high"))
        .alias("high"),
    )
    with pytest.raises(LongA0SidecarError, match=r"daily_bars .* parity"):
        build_long_a0_sidecars(
            mutated,
            features,
            config=_v11a_long_native_config(),
        )


def test_post_signal_bars_are_ignored_and_receipt_is_deterministic(
    source_and_features: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    hourly, features = source_and_features
    cutoff = int(features["ts_ms"].max())
    baseline = build_long_a0_sidecars(
        hourly,
        features,
        config=_v11a_long_native_config(),
    )
    future_mutated = hourly.with_columns(
        *(
            pl.when((pl.col("symbol") == "BTCUSDT") & (pl.col("ts_ms") >= cutoff))
            .then(pl.col(column) * 10.0)
            .otherwise(pl.col(column))
            .alias(column)
            for column in ("open", "high", "low", "close")
        )
    )
    rerun = build_long_a0_sidecars(
        future_mutated,
        features,
        config=_v11a_long_native_config(),
    )

    assert rerun.source_availability.equals(baseline.source_availability)
    assert rerun.regime_context.equals(baseline.regime_context)
    assert rerun.btc_month_context.equals(baseline.btc_month_context)
    assert dict(rerun.receipt) == dict(baseline.receipt)


def test_wrong_config_and_outcome_columns_fail_closed(
    source_and_features: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    hourly, features = source_and_features
    wrong = replace(_v11a_long_native_config(), regime_sma_days=31)
    with pytest.raises(LongA0SidecarError, match="exact _v11a_long_native_config"):
        build_long_a0_sidecars(hourly, features, config=wrong)

    feature_with_outcome = features.with_columns(pl.lit(0.1, dtype=pl.Float64).alias("future_return_24h"))
    with pytest.raises(LongA0SidecarError, match="outcome-like"):
        build_long_a0_sidecars(
            hourly,
            feature_with_outcome,
            config=_v11a_long_native_config(),
        )

    hourly_with_outcome = hourly.with_columns(pl.lit(1.0, dtype=pl.Float64).alias("next_hour_close"))
    with pytest.raises(LongA0SidecarError, match="outcome-like"):
        build_long_a0_sidecars(
            hourly_with_outcome,
            features,
            config=_v11a_long_native_config(),
        )
