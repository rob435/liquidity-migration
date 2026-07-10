"""Focused tests for strict outcome-blind LONG S02 orchestration."""

from __future__ import annotations

import datetime as dt
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from liquidity_migration import long_population_scout as long_scout
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native_event_demo import _v11a_long_native_config
from liquidity_migration.strategy_overhaul_config_identity import (
    JsonValue,
    canonical_json_bytes,
    canonical_json_sha256,
    derive_long_a0_config_identity,
)
from liquidity_migration.strategy_overhaul_expected_population import (
    BoundIdentityReceipt,
    VerifiedExpectedPopulation,
    build_expected_population_artifacts,
    verify_expected_population_artifacts,
)
from liquidity_migration.strategy_overhaul_identity_adapter import (
    MANIFEST_PAIR_COLUMNS,
)
from liquidity_migration.strategy_overhaul_long_context import (
    BTC_MONTH_CONTEXT_SCHEMA,
    REGIME_CONTEXT_SCHEMA,
    SOURCE_AVAILABILITY_SCHEMA,
)
from liquidity_migration.strategy_overhaul_long_s02 import (
    DAILY_PREBUILDER_SCHEMA,
    EXPECTED_POPULATION_SCHEMA,
    HOURLY_BAR_SCHEMA,
    LONG_S02_DIAGNOSTICS,
    LONG_S02_EVIDENCE_STATUS,
    LongS02Error,
    build_long_s02_feature_tape,
)
from liquidity_migration.strategy_overhaul_phase0 import InstrumentMapEntry
from liquidity_migration.strategy_overhaul_population_keys import (
    HOURLY_KEY_SCHEMA,
    MANIFEST_KEY_SCHEMA,
)
from liquidity_migration.strategy_overhaul_projection import artifact_polars_schema
from liquidity_migration.strategy_overhaul_schemas import LONG_SIGNAL_SCHEMA_ID


UTC = dt.timezone.utc
SIGNAL_TS_MS = int(dt.datetime(2026, 1, 2, tzinfo=UTC).timestamp() * 1000)
MANIFEST_DATE = dt.date(2026, 1, 1)
SYMBOL = "AAAUSDT"

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


def _daily_features() -> pl.DataFrame:
    row: dict[str, object] = {name: None for name in DAILY_PREBUILDER_SCHEMA}
    row.update(
        {
            "symbol": SYMBOL,
            "ts_ms": SIGNAL_TS_MS,
            "open": 99.0,
            "high": 101.0,
            "low": 98.0,
            "close": 100.0,
            "turnover_quote": 100.0,
            "hourly_bars": 24,
            "log_return": 0.20,
            "pump_3d_log": 0.30,
            "pump_7d_log": 0.40,
            "intra_max_Nh_pump_log": None,
            "close_location": 0.80,
            "close_loc_3d": 0.80,
            "close_loc_7d": 0.80,
            "realized_vol": 0.60,
            "sigma_daily_30d": 0.04,
            "turnover_median_90d": 10.0,
            "turnover_median_30d": 20.0,
            "vol_vs_30d_median": 5.0,
            "today_volume_rank": 1,
            "universe_rank": 1,
            "symbol_age_days": 101,
            "in_universe": True,
            "true_range": 3.0,
            "atr_14d_pct": 0.04,
            "coin_30d_return": 0.10,
            "coin_60d_return": 0.20,
            "regime_on": True,
            "eth_regime_on": True,
            "btc_sma_dist": 0.20,
            "coin_fc_sma": 90.0,
            "btc_high_proximity": 0.90,
            "own_atr_quantile_90d": 0.06,
            "global_lsr": 1.0,
            "oi_chg_7d": 0.05,
        }
    )
    return pl.DataFrame([row], schema=dict(DAILY_PREBUILDER_SCHEMA))


def _hourly_bars() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": SYMBOL,
                "ts_ms": SIGNAL_TS_MS - MS_PER_HOUR,
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
            },
            {
                # A post-signal bar whose values must never affect S02.
                "symbol": SYMBOL,
                "ts_ms": SIGNAL_TS_MS,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
            },
        ],
        schema=dict(HOURLY_BAR_SCHEMA),
    )


def _expected_population(*, age: int = 101) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [SYMBOL],
            "signal_ts_ms": [SIGNAL_TS_MS],
            "symbol_age_days": [age],
        },
        schema=dict(EXPECTED_POPULATION_SCHEMA),
    )


def _source_availability() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": SYMBOL,
                "signal_ts_ms": SIGNAL_TS_MS,
                "signal_feature_available_ts_ms": SIGNAL_TS_MS,
                "daily_bar_available_ts_ms": SIGNAL_TS_MS,
                "btc_context_available_ts_ms": SIGNAL_TS_MS - 2,
                "eth_context_available_ts_ms": SIGNAL_TS_MS - 3,
                "btc_month_context_available_ts_ms": SIGNAL_TS_MS - 1,
            }
        ],
        schema=dict(SOURCE_AVAILABILITY_SCHEMA),
    )


def _regime_context() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "signal_ts_ms": SIGNAL_TS_MS,
                "btc_close": 120.0,
                "btc_sma_30d": 100.0,
                "btc_sma_dist": 0.20,
                "btc_regime_available": True,
                "btc_regime_pass": True,
                "eth_close": 110.0,
                "eth_sma_30d": 100.0,
                "eth_sma_dist": 0.10,
                "eth_regime_available": True,
                "eth_regime_pass": True,
            }
        ],
        schema=dict(REGIME_CONTEXT_SCHEMA),
    )


def _btc_month_context() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "signal_ts_ms": SIGNAL_TS_MS,
                "btc_month_regime_value": 0.05,
                "btc_month_regime_available": True,
                "btc_month_regime_pass": True,
            }
        ],
        schema=dict(BTC_MONTH_CONTEXT_SCHEMA),
    )


def _manifest(*, manifest_date: dt.date = MANIFEST_DATE) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "venue": ["bybit"],
            "symbol": [SYMBOL],
            "manifest_date": [manifest_date],
            "membership_source": ["bybit_public_trading_archive"],
            "membership_inferred": [False],
            "first_archive_observed_date": [dt.date(2025, 1, 1)],
            "reported_launch_time_ms": [int(dt.datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)],
            "root_first_bar_ts_ms": [int(dt.datetime(2025, 2, 1, tzinfo=UTC).timestamp() * 1000)],
            "provenance_limitation": ["archive coverage is not universal tradability"],
            "coverage_state": ["manifest_and_kline_pair_covered"],
        },
        schema=_MANIFEST_SCHEMA,
    )


def _instrument_map(*, symbol: str = SYMBOL) -> list[InstrumentMapEntry]:
    return [
        InstrumentMapEntry(
            canonical_instrument="AAA-USDT-LINEAR-PERP",
            venue="bybit",
            symbol=symbol,
            valid_from_date="2025-01-01",
            valid_to_date_exclusive=None,
            base_asset="AAA",
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_type="linear_perpetual",
            contract_multiplier=1.0,
            mapping_source="reviewed official contract metadata",
            review_status="reviewed",
        )
    ]


def _self_hashed(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result["artifact_sha256"] = canonical_json_sha256(result)
    return result


def _bound_identity(root: Path, name: str, payload: object) -> BoundIdentityReceipt:
    path = root / name
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    return BoundIdentityReceipt(name, path)


def _long_population_hourly_keys() -> pl.DataFrame:
    current_bar_day = SIGNAL_TS_MS - MS_PER_DAY
    first_bar_day = SIGNAL_TS_MS - 101 * MS_PER_DAY
    return pl.DataFrame(
        [
            {"symbol": SYMBOL, "ts_ms": day + hour * MS_PER_HOUR}
            for day in (first_bar_day, current_bar_day)
            for hour in range(24)
        ],
        schema=dict(HOURLY_KEY_SCHEMA),
    )


def _fully_verified_population(
    *,
    manifest_pairs: pl.DataFrame,
    instrument_map: list[InstrumentMapEntry],
    instrument_map_version: str,
    config_identity: dict[str, JsonValue],
) -> VerifiedExpectedPopulation:
    config = _v11a_long_native_config()
    hourly_keys = _long_population_hourly_keys()
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
            "map_sha256": canonical_json_sha256([asdict(entry) for entry in instrument_map]),
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
            sleeve="long",
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


def _run(
    *,
    daily_features: pl.DataFrame | None = None,
    hourly_bars: pl.DataFrame | None = None,
    config: object | None = None,
    expected_population: pl.DataFrame | None = None,
    source_availability: pl.DataFrame | None = None,
    regime_context: pl.DataFrame | None = None,
    btc_month_context: pl.DataFrame | None = None,
    manifest_pairs: pl.DataFrame | None = None,
    instrument_map: list[InstrumentMapEntry] | None = None,
    verified_manifest_pairs: pl.DataFrame | None = None,
    verified_instrument_map: list[InstrumentMapEntry] | None = None,
) -> pl.DataFrame:
    runtime_config = _v11a_long_native_config() if config is None else config
    config_identity = derive_long_a0_config_identity()
    runtime_manifest = _manifest() if manifest_pairs is None else manifest_pairs
    runtime_instrument_map = _instrument_map() if instrument_map is None else instrument_map
    runtime_map_version = "reviewed-map-v1"
    population_manifest = runtime_manifest if verified_manifest_pairs is None else verified_manifest_pairs
    population_map = runtime_instrument_map if verified_instrument_map is None else verified_instrument_map
    verified_population = _fully_verified_population(
        manifest_pairs=population_manifest,
        instrument_map=population_map,
        instrument_map_version=runtime_map_version,
        config_identity=config_identity,
    )
    if expected_population is not None and not verified_population.expected_population.equals(expected_population):
        object.__setattr__(
            verified_population,
            "expected_population",
            expected_population,
        )
    return build_long_s02_feature_tape(
        _daily_features() if daily_features is None else daily_features,
        _hourly_bars() if hourly_bars is None else hourly_bars,
        config=runtime_config,  # type: ignore[arg-type]
        config_identity=config_identity,
        verified_population=verified_population,
        source_availability=(_source_availability() if source_availability is None else source_availability),
        regime_context=_regime_context() if regime_context is None else regime_context,
        btc_month_context=(_btc_month_context() if btc_month_context is None else btc_month_context),
        venue="bybit",
        manifest_pairs=runtime_manifest,
        instrument_map=runtime_instrument_map,
        instrument_map_version=runtime_map_version,
    )


def test_nonempty_projection_has_exact_registered_order_and_dtypes() -> None:
    daily = _daily_features().with_columns(
        pl.lit("binance", dtype=pl.String).alias("venue"),
        pl.lit("UNTRUSTED", dtype=pl.String).alias("canonical_instrument_id"),
        pl.lit(7, dtype=pl.Int64).alias("unregistered_diagnostic"),
    )
    output = _run(daily_features=daily)
    expected_schema = artifact_polars_schema(LONG_SIGNAL_SCHEMA_ID)

    assert output.height == 1
    assert output.columns == list(expected_schema)
    assert dict(output.schema) == dict(expected_schema)
    assert len(output.columns) == 138
    assert output["venue"].item() == "bybit"
    assert output["canonical_instrument_id"].item() == "AAA-USDT-LINEAR-PERP"
    assert output["global_lsr"].item() is None
    assert output["oi_chg_7d"].item() is None
    assert output["gate_lsr"].item() is True
    assert output["gate_oi_rising"].item() is True
    assert "unregistered_diagnostic" not in output.columns
    assert LONG_S02_EVIDENCE_STATUS.startswith("DIAGNOSTIC_ONLY_")
    assert LONG_S02_DIAGNOSTICS["population_receipt_identity_bound"] is True
    assert LONG_S02_DIAGNOSTICS["config_hash_bound"] is True


def test_typed_empty_projection_has_exact_registered_order_and_dtypes() -> None:
    empty_daily = pl.DataFrame(schema=dict(DAILY_PREBUILDER_SCHEMA))
    empty_hourly = pl.DataFrame(schema=dict(HOURLY_BAR_SCHEMA))
    empty_expected = pl.DataFrame(schema=dict(EXPECTED_POPULATION_SCHEMA))
    empty_manifest = pl.DataFrame(schema=_MANIFEST_SCHEMA)

    output = _run(
        daily_features=empty_daily,
        hourly_bars=empty_hourly,
        expected_population=empty_expected,
        source_availability=pl.DataFrame(schema=dict(SOURCE_AVAILABILITY_SCHEMA)),
        regime_context=pl.DataFrame(schema=dict(REGIME_CONTEXT_SCHEMA)),
        btc_month_context=pl.DataFrame(schema=dict(BTC_MONTH_CONTEXT_SCHEMA)),
        manifest_pairs=empty_manifest,
    )
    expected_schema = artifact_polars_schema(LONG_SIGNAL_SCHEMA_ID)

    assert output.is_empty()
    assert output.columns == list(expected_schema)
    assert dict(output.schema) == dict(expected_schema)


def test_requires_exact_v11a_config_even_for_diagnostic_tape() -> None:
    bad = replace(_v11a_long_native_config(), cost_multiplier=99.0)
    with pytest.raises(ValueError, match=r"exact _v11a_long_native_config.*cost_multiplier"):
        _run(config=bad)


def test_runtime_guard_rejects_monkeypatched_classifier_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(long_scout, "LONG_S02_CLASSIFIER_PATTERN", "reversal")

    with pytest.raises(LongS02Error, match="classifier parity failed"):
        _run()


def test_registered_scope_rejects_out_of_window_population() -> None:
    outside_ts = SIGNAL_TS_MS - 10 * 365 * 24 * MS_PER_HOUR
    daily = _daily_features().with_columns(pl.lit(outside_ts, dtype=pl.Int64).alias("ts_ms"))
    expected = _expected_population().with_columns(pl.lit(outside_ts, dtype=pl.Int64).alias("signal_ts_ms"))

    with pytest.raises(LongS02Error, match="source/retained artifact identity drifted"):
        _run(daily_features=daily, expected_population=expected)


def test_rejects_outcome_sentinel_before_finite_prebuilder_projection() -> None:
    contaminated = _daily_features().with_columns(pl.lit(0.25, dtype=pl.Float64).alias("future_return_24h"))
    with pytest.raises(LongS02Error, match=r"outcome-like.*future_return_24h"):
        _run(daily_features=contaminated)


def test_tier_c_lsr_and_oi_inputs_cannot_cross_the_a0_boundary() -> None:
    first = _run(
        daily_features=_daily_features().with_columns(
            pl.lit(999.0).alias("global_lsr"),
            pl.lit(-999.0).alias("oi_chg_7d"),
        )
    )
    second = _run(
        daily_features=_daily_features().with_columns(
            pl.lit(-999.0).alias("global_lsr"),
            pl.lit(999.0).alias("oi_chg_7d"),
        )
    )

    assert_frame_equal(first, second)
    assert first["global_lsr"].item() is None
    assert first["oi_chg_7d"].item() is None


def test_expected_population_requires_keys_and_root_reconstructed_age() -> None:
    missing_age = _expected_population().drop("symbol_age_days")
    with pytest.raises(LongS02Error, match=r"missing=.*symbol_age_days"):
        _run(expected_population=missing_age)

    with pytest.raises(LongS02Error, match="source/retained artifact identity drifted"):
        _run(expected_population=_expected_population(age=102))

    missing_registered_input = _daily_features().drop("coin_60d_return")
    with pytest.raises(LongS02Error, match=r"missing registered prebuilder columns.*coin_60d_return"):
        _run(daily_features=missing_registered_input)


def test_pit_and_reviewed_map_fail_closed() -> None:
    wrong_date = _manifest(manifest_date=dt.date(2025, 12, 31))
    with pytest.raises(LongS02Error, match="runtime PIT/map identity drifted"):
        _run(manifest_pairs=wrong_date, verified_manifest_pairs=_manifest())

    with pytest.raises(LongS02Error, match="runtime PIT/map identity drifted"):
        _run(
            instrument_map=_instrument_map(symbol="BBBUSDT"),
            verified_instrument_map=_instrument_map(),
        )


def test_context_availability_and_rank_receipts_propagate_exactly() -> None:
    output = _run().row(0, named=True)

    assert output["signal_feature_available_ts_ms"] == SIGNAL_TS_MS
    assert output["daily_bar_available_ts_ms"] == SIGNAL_TS_MS
    assert output["btc_context_available_ts_ms"] == SIGNAL_TS_MS - 2
    assert output["eth_context_available_ts_ms"] == SIGNAL_TS_MS - 3
    assert output["btc_month_context_available_ts_ms"] == SIGNAL_TS_MS - 1
    assert output["btc_regime_available"] is True
    assert output["eth_regime_available"] is True
    assert output["regime_on"] is True
    assert output["eth_regime_on"] is True
    assert output["btc_sma_dist"] == pytest.approx(0.20)
    assert output["eth_sma_dist"] == pytest.approx(0.10)
    assert output["btc_month_regime_value"] == pytest.approx(0.05)
    assert output["btc_month_regime_available"] is True
    assert output["btc_month_regime_pass"] is True
    assert output["today_volume_rank_population_peer_count"] == 1
    assert output["today_volume_rank_rankable_peer_count"] == 1
    assert output["today_volume_rank_tie_count"] == 1
    assert output["symbol_age_days"] == 101
    assert output["symbol_age_source"] == "loaded_root_first_daily_row_plus_one"


def test_every_post_signal_hourly_value_is_outside_s02_information_set() -> None:
    baseline = _hourly_bars()
    mutated = baseline.with_columns(
        pl.when(pl.col("ts_ms") == SIGNAL_TS_MS).then(pl.lit(999.0)).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("ts_ms") == SIGNAL_TS_MS).then(pl.lit(1000.0)).otherwise(pl.col("high")).alias("high"),
        pl.when(pl.col("ts_ms") == SIGNAL_TS_MS).then(pl.lit(50.0)).otherwise(pl.col("low")).alias("low"),
    )

    assert_frame_equal(_run(hourly_bars=baseline), _run(hourly_bars=mutated))


def test_test_fixture_manifest_schema_tracks_identity_contract() -> None:
    assert tuple(_MANIFEST_SCHEMA) == MANIFEST_PAIR_COLUMNS
