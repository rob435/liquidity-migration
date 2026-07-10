"""Focused synthetic tests for strict, outcome-blind CONTINUOUS S02 assembly."""

from __future__ import annotations

import datetime as dt
import dataclasses
import tempfile
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import continuous_population_scout as continuous_scout
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig, apply_continuous_demo_profile
from liquidity_migration.strategy_overhaul_config_identity import (
    CONTINUOUS_PROFILE_INPUTS,
    JsonValue,
    canonical_json_bytes,
    canonical_json_sha256,
    derive_continuous_a0_config_identity,
    registered_scope_bounds_ms,
)
from liquidity_migration.strategy_overhaul_expected_population import (
    BoundIdentityReceipt,
    ExpectedPopulationError,
    VerifiedExpectedPopulation,
    build_expected_population_artifacts,
    verify_expected_population_artifacts,
)
from liquidity_migration.strategy_overhaul_phase0 import InstrumentMapEntry
from liquidity_migration.strategy_overhaul_population_keys import (
    HOURLY_KEY_SCHEMA,
    MANIFEST_KEY_SCHEMA,
)
from liquidity_migration.strategy_overhaul_projection import artifact_polars_schema
from liquidity_migration.strategy_overhaul_s02 import (
    CONTINUOUS_S02_EVIDENCE_STATUS,
    EXPECTED_POPULATION_KEY_SCHEMA,
    EXPECTED_SOURCE_KEY_SCHEMA,
    HOURLY_KLINE_SCHEMA,
    RMOM_CAUSAL_AVAILABILITY_SCHEMA,
    ContinuousS02Error,
    build_continuous_s02_feature_tape,
)
from liquidity_migration.strategy_overhaul_schemas import CONTINUOUS_SIGNAL_SCHEMA_ID

UTC = dt.timezone.utc
_MANIFEST_SCHEMA = {
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
}


def _ms(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _hourly(
    *,
    hours: int = 1,
    symbols: tuple[str, ...] = ("AAAUSDT", "BTCUSDT", "ETHUSDT"),
    mutate_after_hour: int | None = None,
    start_ts_ms: int | None = None,
) -> pl.DataFrame:
    start = _ms("2026-01-01T00:00:00+00:00") if start_ts_ms is None else start_ts_ms
    bases = {"AAAUSDT": 10.0, "BTCUSDT": 40_000.0, "ETHUSDT": 2_000.0}
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        for hour in range(hours):
            factor = 1.0 + 0.001 * hour
            if mutate_after_hour is not None and hour >= mutate_after_hour:
                factor *= 3.0 + 0.01 * (hour - mutate_after_hour)
            close = bases.get(symbol, 25.0) * factor
            rows.append(
                {
                    "symbol": symbol,
                    "ts_ms": start + hour * MS_PER_HOUR,
                    "open": close * 0.999,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "turnover_quote": 1_000_000.0 + hour * 1_000.0,
                }
            )
    if not rows:
        return pl.DataFrame(schema=dict(HOURLY_KLINE_SCHEMA))
    return pl.DataFrame(rows, schema=dict(HOURLY_KLINE_SCHEMA)).sort(["symbol", "ts_ms"])


def _expected_keys(hourly: pl.DataFrame) -> pl.DataFrame:
    return hourly.select(
        "symbol",
        pl.col("ts_ms").alias("signal_ts_ms"),
    ).sort(["symbol", "signal_ts_ms"])


def _manifest(hourly: pl.DataFrame) -> pl.DataFrame:
    if hourly.is_empty():
        return pl.DataFrame(schema=_MANIFEST_SCHEMA)
    keys = (
        hourly.select(
            "symbol",
            pl.from_epoch("ts_ms", time_unit="ms").dt.date().alias("manifest_date"),
        )
        .unique()
        .sort(["symbol", "manifest_date"])
    )
    launch = _ms("2024-01-01T00:00:00+00:00")
    return keys.with_columns(
        pl.lit("bybit", dtype=pl.String).alias("venue"),
        pl.lit("bybit_public_trading_archive", dtype=pl.String).alias("membership_source"),
        pl.lit(False, dtype=pl.Boolean).alias("membership_inferred"),
        pl.lit(dt.date(2024, 1, 1), dtype=pl.Date).alias("first_archive_observed_date"),
        pl.lit(launch, dtype=pl.Int64).alias("reported_launch_time_ms"),
        pl.lit(launch, dtype=pl.Int64).alias("root_first_bar_ts_ms"),
        pl.lit("archive coverage is not universal tradability", dtype=pl.String).alias("provenance_limitation"),
        pl.lit("manifest_and_kline_pair_covered", dtype=pl.String).alias("coverage_state"),
    ).select(*_MANIFEST_SCHEMA)


def _instrument_map(symbols: list[str]) -> list[InstrumentMapEntry]:
    if not symbols:
        symbols = ["AAAUSDT"]
    return [
        InstrumentMapEntry(
            canonical_instrument=f"{symbol.removesuffix('USDT')}-USDT-LINEAR-PERP",
            venue="bybit",
            symbol=symbol,
            valid_from_date="2020-01-01",
            base_asset=symbol.removesuffix("USDT"),
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_type="linear_perpetual",
            contract_multiplier=1.0,
            mapping_source="reviewed synthetic contract metadata",
            review_status="reviewed",
        )
        for symbol in symbols
    ]


def _self_hashed(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result["artifact_sha256"] = canonical_json_sha256(result)
    return result


def _bound_identity(root: Path, name: str, payload: object) -> BoundIdentityReceipt:
    path = root / name
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    return BoundIdentityReceipt(name, path)


def _fully_verified_population(
    hourly: pl.DataFrame,
    *,
    manifest_pairs: pl.DataFrame,
    instrument_map: list[InstrumentMapEntry],
    instrument_map_version: str,
    config: ContinuousDemoCycleConfig,
    config_identity: dict[str, JsonValue],
) -> VerifiedExpectedPopulation:
    hourly_keys = hourly.select("symbol", "ts_ms").cast(dict(HOURLY_KEY_SCHEMA))
    manifest_keys = manifest_pairs.select("symbol", "manifest_date").cast(dict(MANIFEST_KEY_SCHEMA))
    scope = config_identity["scope"]
    assert isinstance(scope, dict)
    root_payload = _self_hashed(
        {
            "artifact_type": "strategy_overhaul_root_snapshot",
            "venue": "bybit",
            "window": {
                "identity_history_start_date": scope["causal_read_start_date"],
                "causal_read_start_date": scope["causal_read_start_date"],
                "signal_end_date_exclusive": scope["signal_end_date_exclusive"],
            },
            "numeric_values_decoded": False,
            "returns_calculated": False,
            "labels_calculated": False,
            "outcome_run_authorized": False,
            "real_money_authorized": False,
        }
    )
    pit_payload = _self_hashed(
        {
            "artifact_type": "strategy_overhaul_phase0_pit_provenance",
            "collapsed_membership_key": ["venue", "symbol", "date"],
            "venues": {"bybit": {"membership_pair_count": manifest_keys.height}},
            "outcome_values_read": False,
            "outcome_run_authorized": False,
            "real_money_authorized": False,
        }
    )
    map_payload = _self_hashed(
        {
            "artifact_type": "strategy_overhaul_venue_local_instrument_map",
            "version": instrument_map_version,
            "map_sha256": canonical_json_sha256([dataclasses.asdict(entry) for entry in instrument_map]),
            "entry_count": len(instrument_map),
            "outcome_values_read": False,
            "outcome_run_authorized": False,
            "real_money_authorized": False,
        }
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_receipt = _bound_identity(root, "config.json", config_identity)
        root_receipt = _bound_identity(root, "root.json", root_payload)
        pit_receipt = _bound_identity(root, "pit.json", pit_payload)
        map_receipt = _bound_identity(root, "map.json", map_payload)
        artifacts = build_expected_population_artifacts(
            hourly_keys,
            manifest_keys,
            manifest_pairs,
            sleeve="continuous",
            venue="bybit",
            config=config,
            config_identity=config_identity,
            config_identity_receipt=config_receipt,
            root_identity_receipt=root_receipt,
            pit_identity_receipt=pit_receipt,
            instrument_map=instrument_map,
            instrument_map_version=instrument_map_version,
            instrument_map_identity_receipt=map_receipt,
        )
        return verify_expected_population_artifacts(
            artifacts,
            hourly_keys,
            manifest_keys,
            manifest_pairs,
            config=config,
            config_identity=config_identity,
            config_identity_receipt=config_receipt,
            root_identity_receipt=root_receipt,
            pit_identity_receipt=pit_receipt,
            instrument_map=instrument_map,
            instrument_map_version=instrument_map_version,
            instrument_map_identity_receipt=map_receipt,
        )


def _build(
    hourly: pl.DataFrame,
    *,
    stable_rmom: pl.DataFrame | None = None,
    source_keys: pl.DataFrame | None = None,
    expected_keys: pl.DataFrame | None = None,
    manifest: pl.DataFrame | None = None,
    lookback: int = 30,
) -> pl.DataFrame:
    symbols = sorted(str(value) for value in hourly["symbol"].unique().to_list())
    config = apply_continuous_demo_profile(ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS))
    config_identity = derive_continuous_a0_config_identity()
    source = source_keys
    expected = expected_keys
    runtime_manifest = manifest if manifest is not None else _manifest(hourly)
    runtime_instrument_map = _instrument_map(symbols)
    runtime_map_version = "reviewed-synthetic-map-v1"
    try:
        verified_population = _fully_verified_population(
            hourly,
            manifest_pairs=runtime_manifest,
            instrument_map=runtime_instrument_map,
            instrument_map_version=runtime_map_version,
            config=config,
            config_identity=config_identity,
        )
    except ExpectedPopulationError:
        # Invalid-boundary cases still enter the public S02 guard with an
        # authentically verified baseline object, then fail closed when the
        # requested runtime inputs or tampered frames disagree with its seal.
        baseline_hourly = _hourly()
        baseline_manifest = _manifest(baseline_hourly)
        baseline_map = _instrument_map(sorted(str(value) for value in baseline_hourly["symbol"].unique().to_list()))
        verified_population = _fully_verified_population(
            baseline_hourly,
            manifest_pairs=baseline_manifest,
            instrument_map=baseline_map,
            instrument_map_version=runtime_map_version,
            config=config,
            config_identity=config_identity,
        )
        if source is None:
            source = _expected_keys(hourly)
        if expected is None:
            expected = _expected_keys(hourly)
    if source is not None and not verified_population.source_keys.equals(source):
        object.__setattr__(verified_population, "source_keys", source)
    if expected is not None and not verified_population.expected_population.equals(expected):
        object.__setattr__(verified_population, "expected_population", expected)
    return build_continuous_s02_feature_tape(
        hourly,
        config=config,
        config_identity=config_identity,
        stable_rmom=stable_rmom,
        verified_population=verified_population,
        venue="bybit",
        manifest_pairs=runtime_manifest,
        instrument_map=runtime_instrument_map,
        instrument_map_version=runtime_map_version,
        current_age_source="root_first_bar_ts_ms",
        btc_uptrend_lookback_days=lookback,
    )


def test_nonempty_s02_has_exact_196_columns_order_dtypes_and_keys() -> None:
    hourly = _hourly()
    output = _build(hourly)
    expected_schema = artifact_polars_schema(CONTINUOUS_SIGNAL_SCHEMA_ID)

    assert len(expected_schema) == 196
    assert output.columns == list(expected_schema)
    assert output.schema == dict(expected_schema)
    assert output.height == hourly.height
    assert output.select("venue", "symbol", "decision_ts_ms").n_unique() == output.height
    assert (
        output.select(
            pl.any_horizontal(
                pl.col("venue").is_null(),
                pl.col("symbol").is_null(),
                pl.col("decision_ts_ms").is_null(),
            ).any()
        ).item()
        is False
    )
    assert output["rmom_data_available_ts_ms"].null_count() == output.height
    assert output["rmom_rank_denominator_count"].to_list() == [0, 0, 0]
    assert output["rmom_tie_method"].to_list() == ["average"] * output.height
    assert output["rmom_rank_denominator_rule"].to_list() == ["rankable_peers_minus_one_clamped_1"] * output.height
    assert output["data_available_ts_ms"].to_list() == output["decision_ts_ms"].to_list()
    assert "DIAGNOSTIC_ONLY" in CONTINUOUS_S02_EVIDENCE_STATUS


def test_empty_s02_has_exact_196_columns_order_and_dtypes() -> None:
    output = _build(_hourly(hours=0))
    expected_schema = artifact_polars_schema(CONTINUOUS_SIGNAL_SCHEMA_ID)

    assert output.is_empty()
    assert len(output.columns) == 196
    assert output.columns == list(expected_schema)
    assert output.schema == dict(expected_schema)


def test_outcome_like_source_column_is_refused_before_builder_projection() -> None:
    hourly = _hourly().with_columns(pl.lit(0.42).alias("sentinel_future_return_72h"))

    with pytest.raises(ContinuousS02Error, match="outcome-like.*sentinel_future_return_72h"):
        _build(hourly, expected_keys=_expected_keys(hourly))


@pytest.mark.parametrize("source_case", ["missing_input", "unexpected_input"])
def test_source_key_omission_or_extra_fails_closed(source_case: str) -> None:
    complete = _hourly()
    source = _expected_keys(complete)
    hourly = complete
    if source_case == "missing_input":
        hourly = complete.slice(1)
    else:
        source = source.slice(1)

    with pytest.raises(ContinuousS02Error, match="source/retained artifact identity drifted"):
        _build(hourly, source_keys=source, expected_keys=source)


def test_retained_population_subset_excludes_warmup_but_keeps_its_history() -> None:
    hourly = _hourly(hours=3)
    source = _expected_keys(hourly)
    retained_ts = hourly["ts_ms"].max()
    retained = source.filter(pl.col("signal_ts_ms") == retained_ts)

    output = _build(hourly)
    retained_output = output.filter(pl.col("signal_ts_ms") == retained_ts)

    assert retained_output.height == len(hourly["symbol"].unique()) == 3
    assert retained_output["signal_ts_ms"].unique().to_list() == [retained_ts]
    assert retained_output["ret1"].null_count() == 0
    assert retained_output["btc_ret1"].null_count() == 0
    assert retained_output["eth_ret1"].null_count() == 0
    assert retained_output.select("symbol", "signal_ts_ms").sort(["symbol", "signal_ts_ms"]).equals(retained)


def test_expected_population_key_outside_source_fails_closed() -> None:
    hourly = _hourly()
    source = _expected_keys(hourly)
    outside = pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "signal_ts_ms": [source["signal_ts_ms"].max() + MS_PER_HOUR],
        },
        schema=dict(EXPECTED_POPULATION_KEY_SCHEMA),
    )

    with pytest.raises(ContinuousS02Error, match="source/retained artifact identity drifted"):
        _build(hourly, source_keys=source, expected_keys=outside)


def test_empty_signal_window_with_nonempty_warmup_emits_no_rows() -> None:
    identity = derive_continuous_a0_config_identity()
    bounds = registered_scope_bounds_ms(identity)
    hourly = _hourly(
        hours=3,
        start_ts_ms=bounds["signal_start_date_ms"] - 3 * MS_PER_HOUR,
    )

    output = _build(hourly)

    assert output.is_empty()
    assert len(output.columns) == 196
    assert output.schema == dict(artifact_polars_schema(CONTINUOUS_SIGNAL_SCHEMA_ID))


def test_missing_pit_identity_row_fails_closed() -> None:
    hourly = _hourly()
    incomplete_manifest = _manifest(hourly).filter(pl.col("symbol") != "AAAUSDT")

    with pytest.raises(ContinuousS02Error, match="does not equal expected_source_keys"):
        _build(hourly, manifest=incomplete_manifest)


def test_rmom_causal_computability_replaces_unrecorded_publication_time() -> None:
    hourly = _hourly(symbols=("AAAUSDT",))
    day_ts = hourly["ts_ms"].item()
    stable = pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "day_ts": [day_ts],
            "residual_momentum": [-0.25],
            "is_provisional": [False],
        },
        schema={
            "symbol": pl.String,
            "day_ts": pl.Int64,
            "residual_momentum": pl.Float64,
            "is_provisional": pl.Boolean,
        },
    )
    output = _build(hourly, stable_rmom=stable)

    assert output["rmom_stable_available"].item() is True
    assert output["rmom_data_available_ts_ms"].item() == (day_ts - MS_PER_DAY + MS_PER_HOUR)
    assert output["data_available_ts_ms"].item() == output["decision_ts_ms"].item()


def test_provisional_rmom_never_emits_stable_causal_availability() -> None:
    hourly = _hourly(symbols=("AAAUSDT",))
    day_ts = hourly["ts_ms"].item()
    provisional = pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "day_ts": [day_ts],
            "residual_momentum": [-0.25],
            "is_provisional": [True],
        },
        schema={
            "symbol": pl.String,
            "day_ts": pl.Int64,
            "residual_momentum": pl.Float64,
            "is_provisional": pl.Boolean,
        },
    )
    output = _build(hourly, stable_rmom=provisional)

    assert output["rmom_is_provisional"].item() is True
    assert output["rmom_stable_available"].item() is False
    assert output["rmom_data_available_ts_ms"].item() is None
    assert output["data_available_ts_ms"].item() == output["feature_data_available_ts_ms"].item()


def test_causal_availability_is_derived_only_for_retained_rmom_source_rows() -> None:
    hourly = _hourly(hours=25, symbols=("AAAUSDT",))
    source = _expected_keys(hourly)
    retained = source.tail(1)
    first_day = hourly["ts_ms"].min()
    second_day = first_day + MS_PER_DAY
    rmom = pl.DataFrame(
        {
            "symbol": ["AAAUSDT", "AAAUSDT"],
            "day_ts": [first_day, second_day],
            "residual_momentum": [-0.10, -0.25],
            "is_provisional": [True, False],
        },
        schema={
            "symbol": pl.String,
            "day_ts": pl.Int64,
            "residual_momentum": pl.Float64,
            "is_provisional": pl.Boolean,
        },
    )
    output = _build(hourly, stable_rmom=rmom).filter(pl.col("signal_ts_ms") == retained["signal_ts_ms"].item())

    assert output.height == 1
    assert output["signal_ts_ms"].item() == second_day
    assert output["rmom_stable_available"].item() is True
    assert output["rmom_data_available_ts_ms"].item() == (second_day - MS_PER_DAY + MS_PER_HOUR)


def test_stable_rmom_requires_explicit_provisional_provenance() -> None:
    hourly = _hourly(symbols=("AAAUSDT",))
    day_ts = hourly["ts_ms"].item()
    stable = pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "day_ts": [day_ts],
            "residual_momentum": [-0.25],
        },
        schema={
            "symbol": pl.String,
            "day_ts": pl.Int64,
            "residual_momentum": pl.Float64,
        },
    )

    with pytest.raises(ContinuousS02Error, match=r"missing=.*is_provisional"):
        _build(hourly, stable_rmom=stable)


def test_future_source_mutation_cannot_change_an_earlier_s02_row() -> None:
    original = _hourly(hours=100)
    mutated = _hourly(hours=100, mutate_after_hour=80)
    source = _expected_keys(original)
    target_ts = original["ts_ms"].min() + 60 * MS_PER_HOUR
    first = _build(original, source_keys=source).filter(
        (pl.col("symbol") == "AAAUSDT") & (pl.col("signal_ts_ms") == target_ts)
    )
    second = _build(mutated, source_keys=source).filter(
        (pl.col("symbol") == "AAAUSDT") & (pl.col("signal_ts_ms") == target_ts)
    )

    assert first.height == second.height == 1
    assert first.equals(second)
    assert (
        original.filter(pl.col("ts_ms") >= target_ts + 20 * MS_PER_HOUR)["close"].to_list()
        != mutated.filter(pl.col("ts_ms") >= target_ts + 20 * MS_PER_HOUR)["close"].to_list()
    )


def test_noncanonical_btc_uptrend_lookback_is_refused() -> None:
    with pytest.raises(ContinuousS02Error, match="canonical registered value 30"):
        _build(_hourly(), lookback=29)


def test_runtime_guard_rejects_monkeypatched_population_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(continuous_scout, "CURRENT_RMOM_QUANTILE", 0.99)

    with pytest.raises(ContinuousS02Error, match="selection-profile parity failed"):
        _build(_hourly())


def test_registered_scope_rejects_out_of_window_signal_keys() -> None:
    outside = _hourly().with_columns((pl.col("ts_ms") - 10 * 365 * MS_PER_DAY).alias("ts_ms"))

    with pytest.raises(ContinuousS02Error, match="expected-population receipt failed"):
        _build(outside)


def test_availability_and_expected_key_schemas_are_exact_and_outcome_free() -> None:
    assert tuple(EXPECTED_SOURCE_KEY_SCHEMA) == ("symbol", "signal_ts_ms")
    assert tuple(EXPECTED_POPULATION_KEY_SCHEMA) == ("symbol", "signal_ts_ms")
    assert tuple(RMOM_CAUSAL_AVAILABILITY_SCHEMA) == (
        "symbol",
        "day_ts",
        "rmom_data_available_ts_ms",
    )
    assert MS_PER_DAY > MS_PER_HOUR
