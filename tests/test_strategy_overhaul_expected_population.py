"""Synthetic tests for canonical, identity-bound expected S02 populations."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig, apply_continuous_demo_profile
from liquidity_migration.long_native_event_demo import _v11a_long_native_config
from liquidity_migration.strategy_overhaul_config_identity import (
    CONTINUOUS_PROFILE_INPUTS,
    JsonValue,
    canonical_json_bytes,
    canonical_json_sha256,
    derive_continuous_a0_config_identity,
    derive_long_a0_config_identity,
    registered_scope_bounds_ms,
)
from liquidity_migration.strategy_overhaul_expected_population import (
    CONTINUOUS_REGISTERED_S02_KEY_SCHEMA,
    EXPECTED_POPULATION_FILENAME,
    LONG_EXPECTED_POPULATION_SCHEMA,
    LONG_MIN_HOURLY_BARS,
    LONG_REGISTERED_S02_KEY_SCHEMA,
    BoundIdentityReceipt,
    ExpectedPopulationArtifacts,
    ExpectedPopulationError,
    VerifiedExpectedPopulation,
    build_expected_population_artifacts,
    canonical_manifest_pair_identity,
    continuous_population_exclusions_parity_surface,
    load_expected_population_artifacts,
    long_population_and_rolling_windows_parity_surface,
    parse_expected_population_jsonl,
    registered_s02_key_sha256,
    render_expected_population_jsonl,
    render_expected_population_receipt,
    verify_expected_population_artifacts,
    verify_expected_population_receipt_identity,
    verified_expected_population_s02_inputs,
    write_expected_population_artifacts,
)
from liquidity_migration.strategy_overhaul_phase0 import InstrumentMapEntry
from liquidity_migration.strategy_overhaul_population_keys import HOURLY_KEY_SCHEMA, MANIFEST_KEY_SCHEMA


UTC = dt.timezone.utc
SYMBOL = "AAAUSDT"
EXCLUDED = "USDCUSDT"
MAP_VERSION = "synthetic-venue-local-v1"


@dataclass(frozen=True)
class _Case:
    sleeve: str
    config: object
    config_identity: dict[str, JsonValue]
    hourly: pl.DataFrame
    manifest: pl.DataFrame
    bindings: dict[str, BoundIdentityReceipt]
    instrument_map: tuple[InstrumentMapEntry, ...]


def _date_ms(value: str) -> int:
    return int(dt.datetime.combine(dt.date.fromisoformat(value), dt.time.min, tzinfo=UTC).timestamp() * 1000)


def _self_hashed(payload: dict[str, object]) -> dict[str, object]:
    output = dict(payload)
    output["artifact_sha256"] = canonical_json_sha256(output)
    return output


def _write_json(path: Path, payload: object) -> BoundIdentityReceipt:
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    return BoundIdentityReceipt(path.name, path)


def _instrument_map() -> tuple[InstrumentMapEntry, ...]:
    return (
        InstrumentMapEntry(
            canonical_instrument="BYBIT::AAAUSDT::USDT_LINEAR_PERPETUAL",
            venue="bybit",
            symbol=SYMBOL,
            valid_from_date="2023-01-01",
            valid_to_date_exclusive=None,
            base_asset="AAA",
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_type="linear_perpetual",
            contract_multiplier=1.0,
            mapping_source="synthetic reviewed venue-local identity",
            review_status="reviewed",
        ),
    )


def _manifest_pairs(manifest: pl.DataFrame) -> pl.DataFrame:
    return manifest.with_columns(
        pl.lit("bybit", dtype=pl.String).alias("venue"),
        pl.lit("synthetic_archive_membership", dtype=pl.String).alias("membership_source"),
        pl.lit(False, dtype=pl.Boolean).alias("membership_inferred"),
        pl.col("manifest_date").alias("first_archive_observed_date"),
        pl.lit(None, dtype=pl.Int64).alias("reported_launch_time_ms"),
        pl.lit(None, dtype=pl.Int64).alias("root_first_bar_ts_ms"),
        pl.lit("synthetic PIT provenance fixture", dtype=pl.String).alias("provenance_limitation"),
        pl.lit("manifest_and_kline_pair_covered", dtype=pl.String).alias("coverage_state"),
    ).select(
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


def _bindings(
    tmp_path: Path,
    *,
    sleeve: str,
    config_identity: dict[str, JsonValue],
    identity_history_start_date: str,
    manifest_row_count: int,
    instrument_map: tuple[InstrumentMapEntry, ...],
) -> dict[str, BoundIdentityReceipt]:
    scope = config_identity["scope"]
    assert isinstance(scope, dict)
    root = _self_hashed(
        {
            "schema_version": 2,
            "artifact_type": "strategy_overhaul_root_snapshot",
            "venue": "bybit",
            "window": {
                "identity_history_start_date": identity_history_start_date,
                "causal_read_start_date": scope["causal_read_start_date"],
                "signal_end_date_exclusive": scope["signal_end_date_exclusive"],
                "label_end_date_exclusive": scope["signal_end_date_exclusive"],
            },
            "registered_scope_verified": False,
            "earliest_root_history_proven": False,
            "source_authenticity_proven": False,
            "numeric_values_decoded": False,
            "returns_calculated": False,
            "labels_calculated": False,
            "outcome_run_authorized": False,
            "real_money_authorized": False,
        }
    )
    pit = _self_hashed(
        {
            "schema_version": 1,
            "artifact_type": "strategy_overhaul_phase0_pit_provenance",
            "membership_storage_key": ["venue", "symbol", "date", "url"],
            "collapsed_membership_key": ["venue", "symbol", "date"],
            "venues": {"bybit": {"membership_pair_count": manifest_row_count}},
            "outcome_values_read": False,
            "outcome_run_authorized": False,
            "real_money_authorized": False,
        }
    )
    entry_payload = [dataclasses.asdict(entry) for entry in instrument_map]
    map_identity = _self_hashed(
        {
            "schema_version": 1,
            "artifact_type": "strategy_overhaul_venue_local_instrument_map",
            "version": MAP_VERSION,
            "map_sha256": canonical_json_sha256(entry_payload),
            "entry_count": len(instrument_map),
            "outcome_values_read": False,
            "outcome_run_authorized": False,
            "real_money_authorized": False,
        }
    )
    return {
        "config": _write_json(tmp_path / f"{sleeve}_config_identity.json", config_identity),
        "root": _write_json(tmp_path / f"{sleeve}_root_identity.json", root),
        "pit": _write_json(tmp_path / f"{sleeve}_pit_identity.json", pit),
        "map": _write_json(tmp_path / f"{sleeve}_map_identity.json", map_identity),
    }


def _continuous_case(tmp_path: Path) -> _Case:
    identity = derive_continuous_a0_config_identity()
    config = apply_continuous_demo_profile(ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS))
    bounds = registered_scope_bounds_ms(identity)
    causal = bounds["causal_read_start_date_ms"]
    signal = bounds["signal_start_date_ms"]
    hourly = pl.DataFrame(
        {
            "symbol": [SYMBOL, SYMBOL, SYMBOL, EXCLUDED],
            "ts_ms": [causal, signal, signal + MS_PER_HOUR, signal],
        },
        schema=dict(HOURLY_KEY_SCHEMA),
    )
    manifest = pl.DataFrame(
        {
            "symbol": [SYMBOL, SYMBOL, EXCLUDED],
            "manifest_date": [
                dt.datetime.fromtimestamp(causal / 1000, tz=UTC).date(),
                dt.datetime.fromtimestamp(signal / 1000, tz=UTC).date(),
                dt.datetime.fromtimestamp(signal / 1000, tz=UTC).date(),
            ],
        },
        schema=dict(MANIFEST_KEY_SCHEMA),
    )
    instrument_map = _instrument_map()
    return _Case(
        sleeve="continuous",
        config=config,
        config_identity=identity,
        hourly=hourly,
        manifest=manifest,
        bindings=_bindings(
            tmp_path,
            sleeve="continuous",
            config_identity=identity,
            identity_history_start_date=str(identity["scope"]["causal_read_start_date"]),
            manifest_row_count=manifest.height,
            instrument_map=instrument_map,
        ),
        instrument_map=instrument_map,
    )


def _long_case(tmp_path: Path) -> _Case:
    identity = derive_long_a0_config_identity()
    config = _v11a_long_native_config()
    bounds = registered_scope_bounds_ms(identity)
    causal = bounds["causal_read_start_date_ms"]
    signal = bounds["signal_start_date_ms"]
    history_day = causal - MS_PER_DAY
    signal_day = signal - MS_PER_DAY
    rows = [
        {"symbol": symbol, "ts_ms": day + hour * MS_PER_HOUR}
        for symbol, day in ((SYMBOL, history_day), (SYMBOL, signal_day), (EXCLUDED, signal_day))
        for hour in range(24)
    ]
    hourly = pl.DataFrame(rows, schema=dict(HOURLY_KEY_SCHEMA))
    manifest = pl.DataFrame(
        {
            "symbol": [SYMBOL, SYMBOL, EXCLUDED],
            "manifest_date": [
                dt.datetime.fromtimestamp(history_day / 1000, tz=UTC).date(),
                dt.datetime.fromtimestamp(signal_day / 1000, tz=UTC).date(),
                dt.datetime.fromtimestamp(signal_day / 1000, tz=UTC).date(),
            ],
        },
        schema=dict(MANIFEST_KEY_SCHEMA),
    )
    instrument_map = _instrument_map()
    return _Case(
        sleeve="long",
        config=config,
        config_identity=identity,
        hourly=hourly,
        manifest=manifest,
        bindings=_bindings(
            tmp_path,
            sleeve="long",
            config_identity=identity,
            identity_history_start_date=dt.datetime.fromtimestamp(history_day / 1000, tz=UTC).date().isoformat(),
            manifest_row_count=manifest.height,
            instrument_map=instrument_map,
        ),
        instrument_map=instrument_map,
    )


def _build(case: _Case) -> ExpectedPopulationArtifacts:
    return build_expected_population_artifacts(
        case.hourly,
        case.manifest,
        _manifest_pairs(case.manifest),
        sleeve=case.sleeve,  # type: ignore[arg-type]
        venue="bybit",
        config=case.config,  # type: ignore[arg-type]
        config_identity=case.config_identity,
        config_identity_receipt=case.bindings["config"],
        root_identity_receipt=case.bindings["root"],
        pit_identity_receipt=case.bindings["pit"],
        instrument_map=case.instrument_map,
        instrument_map_version=MAP_VERSION,
        instrument_map_identity_receipt=case.bindings["map"],
    )


def _verify(case: _Case, artifacts: ExpectedPopulationArtifacts):
    return verify_expected_population_artifacts(
        artifacts,
        case.hourly,
        case.manifest,
        _manifest_pairs(case.manifest),
        config=case.config,  # type: ignore[arg-type]
        config_identity=case.config_identity,
        config_identity_receipt=case.bindings["config"],
        root_identity_receipt=case.bindings["root"],
        pit_identity_receipt=case.bindings["pit"],
        instrument_map=case.instrument_map,
        instrument_map_version=MAP_VERSION,
        instrument_map_identity_receipt=case.bindings["map"],
    )


def test_continuous_artifacts_bind_exact_source_population_and_registered_s02_keys(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    artifacts = _build(case)
    verified = _verify(case, artifacts)
    signal = registered_scope_bounds_ms(case.config_identity)["signal_start_date_ms"]

    assert verified.source_keys.to_dicts() == [
        {
            "symbol": SYMBOL,
            "signal_ts_ms": registered_scope_bounds_ms(case.config_identity)["causal_read_start_date_ms"],
        },
        {"symbol": SYMBOL, "signal_ts_ms": signal},
        {"symbol": SYMBOL, "signal_ts_ms": signal + MS_PER_HOUR},
    ]
    assert verified.expected_population.to_dicts() == [
        {"symbol": SYMBOL, "signal_ts_ms": signal},
        {"symbol": SYMBOL, "signal_ts_ms": signal + MS_PER_HOUR},
    ]
    assert EXCLUDED not in verified.source_keys["symbol"].to_list()
    assert artifacts.receipt["config_exclusions"]["signal_rows_removed"] == 1
    assert artifacts.receipt["long_min_hourly_bars"] is None
    assert artifacts.receipt["exact_supplied_keys_and_ages_verified"] is True
    assert "exact_keys_and_ages_verified" not in artifacts.receipt
    projection = pl.DataFrame(
        {
            "venue": ["bybit", "bybit"],
            "symbol": [SYMBOL, SYMBOL],
            "decision_ts_ms": [signal + MS_PER_HOUR, signal + 2 * MS_PER_HOUR],
        },
        schema=dict(CONTINUOUS_REGISTERED_S02_KEY_SCHEMA),
    )
    assert verified.receipt_identity.registered_s02_key_sha256 == registered_s02_key_sha256(
        projection,
        sleeve="continuous",
    )
    assert verified.receipt_identity.registered_s02_key_columns == (
        "venue",
        "symbol",
        "decision_ts_ms",
    )
    assert continuous_population_exclusions_parity_surface(
        case.config,  # type: ignore[arg-type]
        case.config_identity,
    ) == {"exclude_symbols": list(case.config.exclude_symbols)}  # type: ignore[union-attr]


def test_long_artifact_preserves_source_count_but_projects_exact_age_for_s02(tmp_path: Path) -> None:
    case = _long_case(tmp_path)
    artifacts = _build(case)
    verified = _verify(case, artifacts)
    signal = registered_scope_bounds_ms(case.config_identity)["signal_start_date_ms"]
    history_close = registered_scope_bounds_ms(case.config_identity)["causal_read_start_date_ms"]
    expected_age = (signal - history_close) // MS_PER_DAY + 1

    assert verified.source_keys.schema == dict(
        {
            "symbol": pl.String,
            "signal_ts_ms": pl.Int64,
            "symbol_age_days": pl.Int64,
            "hourly_bar_count": pl.UInt32,
        }
    )
    assert verified.source_keys.to_dicts() == [
        {
            "symbol": SYMBOL,
            "signal_ts_ms": signal,
            "symbol_age_days": expected_age,
            "hourly_bar_count": 24,
        }
    ]
    assert verified.expected_population.schema == dict(LONG_EXPECTED_POPULATION_SCHEMA)
    assert verified.expected_population.to_dicts() == [
        {"symbol": SYMBOL, "signal_ts_ms": signal, "symbol_age_days": expected_age}
    ]
    assert "hourly_bar_count" not in verified.expected_population.columns
    assert artifacts.receipt["long_min_hourly_bars"] == LONG_MIN_HOURLY_BARS
    projection = pl.DataFrame(
        {"venue": ["bybit"], "symbol": [SYMBOL], "signal_ts_ms": [signal]},
        schema=dict(LONG_REGISTERED_S02_KEY_SCHEMA),
    )
    assert verified.receipt_identity.registered_s02_key_sha256 == registered_s02_key_sha256(
        projection,
        sleeve="long",
    )
    parity = long_population_and_rolling_windows_parity_surface(
        case.config,  # type: ignore[arg-type]
        case.config_identity,
    )
    assert parity == {
        "exclude_symbols": list(case.config.exclude_symbols),  # type: ignore[union-attr]
        "universe_size": case.config.universe_size,  # type: ignore[union-attr]
        "universe_volume_window_days": case.config.universe_volume_window_days,  # type: ignore[union-attr]
        "min_listing_history_days": case.config.min_listing_history_days,  # type: ignore[union-attr]
        "vol_estimate_window_days": case.config.vol_estimate_window_days,  # type: ignore[union-attr]
    }


def test_receipt_identity_exposes_stage_comparable_bound_records(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    artifacts = _build(case)
    receipt_bytes = render_expected_population_receipt(artifacts.receipt)
    identity = verify_expected_population_receipt_identity(receipt_bytes)

    for kind, source_kind in (("config", "config"), ("root", "root"), ("pit", "pit"), ("instrument_map", "map")):
        record = identity.identity_bindings[kind]
        source = case.bindings[source_kind]
        source_bytes = source.path.read_bytes()
        source_payload = __import__("json").loads(source_bytes)
        assert record["logical_path"] == source.logical_path
        assert record["file_sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert record["bytes"] == len(source_bytes)
        assert record["identity_sha256"] == canonical_json_sha256(source_payload)
    assert identity.receipt_file_sha256 == hashlib.sha256(receipt_bytes).hexdigest()
    assert identity.receipt_identity_sha256 == canonical_json_sha256(dict(artifacts.receipt))
    assert identity.source_keys_file_sha256 == hashlib.sha256(artifacts.source_keys_jsonl).hexdigest()
    assert identity.source_keys_row_count == artifacts.receipt["artifacts"]["source_keys"]["row_count"]
    assert identity.expected_population_file_sha256 == hashlib.sha256(artifacts.expected_population_jsonl).hexdigest()
    assert identity.expected_population_row_count == artifacts.receipt["artifacts"]["expected_population"]["row_count"]
    pair_rows, pair_sha256 = canonical_manifest_pair_identity(
        _manifest_pairs(case.manifest),
        venue="bybit",
    )
    assert identity.manifest_pairs_row_count == pair_rows
    assert identity.manifest_pairs_canonical_jsonl_sha256 == pair_sha256
    assert identity.instrument_map_version == MAP_VERSION
    assert identity.instrument_map_sha256 == canonical_json_sha256(
        [dataclasses.asdict(entry) for entry in case.instrument_map]
    )


def test_verified_s02_inputs_reject_in_place_source_and_age_mutation(tmp_path: Path) -> None:
    continuous = _continuous_case(tmp_path)
    continuous_verified = _verify(continuous, _build(continuous))
    source, expected = verified_expected_population_s02_inputs(
        continuous_verified,
        sleeve="continuous",
        venue="bybit",
        config=continuous.config,  # type: ignore[arg-type]
        config_identity=continuous.config_identity,
        manifest_pairs=_manifest_pairs(continuous.manifest),
        instrument_map=continuous.instrument_map,
        instrument_map_version=MAP_VERSION,
    )
    assert source.height == 3
    assert expected.height == 2
    last_source_ts = int(continuous_verified.source_keys["signal_ts_ms"].max())
    continuous_verified.source_keys.vstack(
        pl.DataFrame(
            {"symbol": [SYMBOL], "signal_ts_ms": [last_source_ts + MS_PER_HOUR]},
            schema=continuous_verified.source_keys.schema,
        ),
        in_place=True,
    )
    with pytest.raises(ExpectedPopulationError, match="source/retained artifact identity drifted"):
        verified_expected_population_s02_inputs(
            continuous_verified,
            sleeve="continuous",
            venue="bybit",
            config=continuous.config,  # type: ignore[arg-type]
            config_identity=continuous.config_identity,
            manifest_pairs=_manifest_pairs(continuous.manifest),
            instrument_map=continuous.instrument_map,
            instrument_map_version=MAP_VERSION,
        )
    long = _long_case(tmp_path)
    long_verified = _verify(long, _build(long))
    age_index = long_verified.expected_population.get_column_index("symbol_age_days")
    assert age_index is not None
    long_verified.expected_population.replace_column(
        age_index,
        (long_verified.expected_population["symbol_age_days"] + 1).alias("symbol_age_days"),
    )
    with pytest.raises(ExpectedPopulationError, match="source/retained artifact identity drifted"):
        verified_expected_population_s02_inputs(
            long_verified,
            sleeve="long",
            venue="bybit",
            config=long.config,  # type: ignore[arg-type]
            config_identity=long.config_identity,
            manifest_pairs=_manifest_pairs(long.manifest),
            instrument_map=long.instrument_map,
            instrument_map_version=MAP_VERSION,
        )


def test_verified_population_has_no_public_constructor_or_factory() -> None:
    with pytest.raises(TypeError):
        VerifiedExpectedPopulation()  # type: ignore[call-arg]
    assert not hasattr(VerifiedExpectedPopulation, "_from_full_reconstruction")


def test_verified_s02_inputs_reject_unregistered_object_new_copy(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    verified = _verify(case, _build(case))
    forged = object.__new__(VerifiedExpectedPopulation)
    for field in (
        "sleeve",
        "venue",
        "source_keys",
        "expected_population",
        "receipt_sha256",
        "receipt_identity",
    ):
        object.__setattr__(forged, field, getattr(verified, field))

    with pytest.raises(ExpectedPopulationError, match="produced by full reconstruction"):
        verified_expected_population_s02_inputs(
            forged,
            sleeve="continuous",
            venue="bybit",
            config=case.config,  # type: ignore[arg-type]
            config_identity=case.config_identity,
            manifest_pairs=_manifest_pairs(case.manifest),
            instrument_map=case.instrument_map,
            instrument_map_version=MAP_VERSION,
        )


def test_verified_s02_inputs_reject_mutated_registered_proof_object(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    verified = _verify(case, _build(case))
    reduced = verified.expected_population.head(1)
    reduced_bytes = render_expected_population_jsonl(
        reduced,
        sleeve="continuous",
        artifact_kind="expected_population",
    )
    assert isinstance(case.config, ContinuousDemoCycleConfig)
    registered = reduced.select(
        pl.lit("bybit", dtype=pl.String).alias("venue"),
        "symbol",
        (pl.col("signal_ts_ms") + case.config.entry_confirm_delay_hours * MS_PER_HOUR).alias("decision_ts_ms"),
    )
    forged_identity = dataclasses.replace(
        verified.receipt_identity,
        expected_population_file_sha256=hashlib.sha256(reduced_bytes).hexdigest(),
        expected_population_row_count=reduced.height,
        registered_s02_key_sha256=registered_s02_key_sha256(registered, sleeve="continuous"),
        registered_s02_key_row_count=reduced.height,
    )
    object.__setattr__(verified, "expected_population", reduced)
    object.__setattr__(verified, "receipt_identity", forged_identity)

    with pytest.raises(ExpectedPopulationError, match="proof object mutated"):
        verified_expected_population_s02_inputs(
            verified,
            sleeve="continuous",
            venue="bybit",
            config=case.config,
            config_identity=case.config_identity,
            manifest_pairs=_manifest_pairs(case.manifest),
            instrument_map=case.instrument_map,
            instrument_map_version=MAP_VERSION,
        )


def test_verified_s02_inputs_reject_runtime_pit_or_map_drift(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    verified = _verify(case, _build(case))
    drifted_pairs = _manifest_pairs(case.manifest).with_columns(
        pl.lit("different_reviewed_source", dtype=pl.String).alias("membership_source")
    )
    with pytest.raises(ExpectedPopulationError, match="PIT/map identity drifted"):
        verified_expected_population_s02_inputs(
            verified,
            sleeve="continuous",
            venue="bybit",
            config=case.config,  # type: ignore[arg-type]
            config_identity=case.config_identity,
            manifest_pairs=drifted_pairs,
            instrument_map=case.instrument_map,
            instrument_map_version=MAP_VERSION,
        )

    drifted_map = tuple(
        dataclasses.replace(entry, mapping_source="different reviewed map source") for entry in case.instrument_map
    )
    with pytest.raises(ExpectedPopulationError, match="PIT/map identity drifted"):
        verified_expected_population_s02_inputs(
            verified,
            sleeve="continuous",
            venue="bybit",
            config=case.config,  # type: ignore[arg-type]
            config_identity=case.config_identity,
            manifest_pairs=_manifest_pairs(case.manifest),
            instrument_map=drifted_map,
            instrument_map_version=MAP_VERSION,
        )


def test_long_daily_eligibility_is_fixed_and_receipt_bound_at_twenty_hours(tmp_path: Path) -> None:
    case = _long_case(tmp_path)
    latest_aaa_day = int(case.hourly.filter(pl.col("symbol") == SYMBOL)["ts_ms"].max()) // MS_PER_DAY * MS_PER_DAY
    partial_latest_day = case.hourly.filter(
        (pl.col("symbol") != SYMBOL) | (pl.col("ts_ms") < latest_aaa_day) | (pl.col("ts_ms") == latest_aaa_day)
    )
    partial_case = dataclasses.replace(case, hourly=partial_latest_day)
    artifacts = _build(partial_case)
    verified = _verify(partial_case, artifacts)

    assert LONG_MIN_HOURLY_BARS == 20
    assert artifacts.receipt["long_min_hourly_bars"] == LONG_MIN_HOURLY_BARS
    assert verified.expected_population.is_empty()


def test_population_map_rejects_s02_product_identity_conflicts(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    first = case.instrument_map[0]
    conflicting = dataclasses.replace(
        first,
        venue="binance",
        symbol="BBBUSDT",
        base_asset="BBB",
    )
    entries = (first, conflicting)
    map_payload = _self_hashed(
        {
            "schema_version": 1,
            "artifact_type": "strategy_overhaul_venue_local_instrument_map",
            "version": MAP_VERSION,
            "map_sha256": canonical_json_sha256([dataclasses.asdict(entry) for entry in entries]),
            "entry_count": len(entries),
            "outcome_values_read": False,
            "outcome_run_authorized": False,
            "real_money_authorized": False,
        }
    )
    case.bindings["map"].path.write_bytes(canonical_json_bytes(map_payload) + b"\n")
    conflicting_case = dataclasses.replace(case, instrument_map=entries)

    with pytest.raises(ExpectedPopulationError, match="conflicting product identities"):
        _build(conflicting_case)


@pytest.mark.parametrize("identity_kind", ["config", "root", "pit", "map"])
def test_bound_identity_byte_drift_fails_reconstruction(tmp_path: Path, identity_kind: str) -> None:
    case = _continuous_case(tmp_path)
    artifacts = _build(case)
    path = case.bindings[identity_kind].path
    original = path.read_bytes()
    path.write_bytes(original + b" ")

    with pytest.raises(ExpectedPopulationError, match="receipt does not equal|payload does not exactly|identity"):
        _verify(case, artifacts)


def test_pit_count_and_map_content_drift_fail_closed(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    pit_payload = __import__("json").loads(case.bindings["pit"].path.read_bytes())
    pit_payload.pop("artifact_sha256")
    pit_payload["venues"]["bybit"]["membership_pair_count"] += 1
    pit_payload = _self_hashed(pit_payload)
    case.bindings["pit"].path.write_bytes(canonical_json_bytes(pit_payload) + b"\n")
    with pytest.raises(ExpectedPopulationError, match="membership_pair_count"):
        _build(case)

    case = _continuous_case(tmp_path)
    map_payload = __import__("json").loads(case.bindings["map"].path.read_bytes())
    map_payload.pop("artifact_sha256")
    map_payload["map_sha256"] = "0" * 64
    map_payload = _self_hashed(map_payload)
    case.bindings["map"].path.write_bytes(canonical_json_bytes(map_payload) + b"\n")
    with pytest.raises(ExpectedPopulationError, match="version/content identity mismatch"):
        _build(case)


def test_canonical_jsonl_is_deterministic_typed_and_strict(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    first = _build(case)
    shuffled = _Case(
        **{
            **dataclasses.asdict(case),
            "config": case.config,
            "hourly": case.hourly.reverse(),
            "manifest": case.manifest.reverse(),
            "bindings": case.bindings,
            "instrument_map": case.instrument_map,
        }
    )
    second = _build(shuffled)
    assert first.source_keys_jsonl == second.source_keys_jsonl
    assert first.expected_population_jsonl == second.expected_population_jsonl
    assert first.receipt == second.receipt

    parsed = parse_expected_population_jsonl(
        first.expected_population_jsonl,
        sleeve="continuous",
        artifact_kind="expected_population",
    )
    assert (
        render_expected_population_jsonl(
            parsed,
            sleeve="continuous",
            artifact_kind="expected_population",
        )
        == first.expected_population_jsonl
    )
    with pytest.raises(ExpectedPopulationError, match="end with one newline"):
        parse_expected_population_jsonl(
            first.expected_population_jsonl.rstrip(b"\n"),
            sleeve="continuous",
            artifact_kind="expected_population",
        )
    with pytest.raises(ExpectedPopulationError, match="sorted"):
        render_expected_population_jsonl(
            parsed.reverse(),
            sleeve="continuous",
            artifact_kind="expected_population",
        )


@pytest.mark.parametrize("signal_ts_ms", [0, MS_PER_HOUR + 1])
def test_continuous_population_jsonl_rejects_nonpositive_or_off_grid_timestamps(
    signal_ts_ms: int,
) -> None:
    frame = pl.DataFrame(
        {"symbol": [SYMBOL], "signal_ts_ms": [signal_ts_ms]},
        schema={"symbol": pl.String, "signal_ts_ms": pl.Int64},
    )
    with pytest.raises(ExpectedPopulationError, match="invalid keys"):
        render_expected_population_jsonl(
            frame,
            sleeve="continuous",
            artifact_kind="expected_population",
        )


@pytest.mark.parametrize("hourly_bar_count", [1, 19, 25])
def test_long_source_population_rejects_counts_outside_registered_daily_range(
    hourly_bar_count: int,
) -> None:
    frame = pl.DataFrame(
        {
            "symbol": [SYMBOL],
            "signal_ts_ms": [MS_PER_DAY],
            "symbol_age_days": [1],
            "hourly_bar_count": [hourly_bar_count],
        },
        schema={
            "symbol": pl.String,
            "signal_ts_ms": pl.Int64,
            "symbol_age_days": pl.Int64,
            "hourly_bar_count": pl.UInt32,
        },
    )
    with pytest.raises(ExpectedPopulationError, match="between 20 and 24"):
        render_expected_population_jsonl(frame, sleeve="long", artifact_kind="source_keys")


def test_immutable_writer_reuses_identical_and_refuses_different_bytes(tmp_path: Path) -> None:
    case = _long_case(tmp_path)
    artifacts = _build(case)
    output = tmp_path / "population"
    first = write_expected_population_artifacts(output, artifacts)
    second = write_expected_population_artifacts(output, artifacts)
    loaded = load_expected_population_artifacts(output)

    assert first.reused is False
    assert second.reused is True
    assert_frame_equal(_verify(case, loaded).expected_population, _verify(case, artifacts).expected_population)
    (output / EXPECTED_POPULATION_FILENAME).write_bytes(b'{"different":true}\n')
    with pytest.raises(ExpectedPopulationError, match="refusing to overwrite non-identical"):
        write_expected_population_artifacts(output, artifacts)


def test_tampered_key_bytes_or_registered_hash_fail_closed(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    artifacts = _build(case)
    tampered = ExpectedPopulationArtifacts(
        sleeve=artifacts.sleeve,
        venue=artifacts.venue,
        source_keys_jsonl=artifacts.source_keys_jsonl,
        expected_population_jsonl=artifacts.expected_population_jsonl.replace(SYMBOL.encode(), b"BBBUSDT", 1),
        receipt=artifacts.receipt,
    )
    with pytest.raises(ExpectedPopulationError, match="expected_population bytes"):
        _verify(case, tampered)

    receipt = dict(artifacts.receipt)
    projection = dict(receipt["registered_s02_key_projection"])
    projection["canonical_jsonl_sha256"] = "0" * 64
    receipt["registered_s02_key_projection"] = projection
    receipt.pop("artifact_sha256")
    receipt["artifact_sha256"] = canonical_json_sha256(receipt)
    resigned = ExpectedPopulationArtifacts(
        sleeve=artifacts.sleeve,
        venue=artifacts.venue,
        source_keys_jsonl=artifacts.source_keys_jsonl,
        expected_population_jsonl=artifacts.expected_population_jsonl,
        receipt=receipt,
    )
    with pytest.raises(ExpectedPopulationError, match="receipt does not equal"):
        _verify(case, resigned)


def test_root_scope_or_unmapped_expected_key_fails_before_artifact_creation(tmp_path: Path) -> None:
    case = _continuous_case(tmp_path)
    root_payload = __import__("json").loads(case.bindings["root"].path.read_bytes())
    root_payload.pop("artifact_sha256")
    root_payload["window"]["causal_read_start_date"] = "2023-02-24"
    root_payload = _self_hashed(root_payload)
    case.bindings["root"].path.write_bytes(canonical_json_bytes(root_payload) + b"\n")
    with pytest.raises(ExpectedPopulationError, match="causal-read boundary"):
        _build(case)

    case = _continuous_case(tmp_path)
    missing_map = tuple(
        dataclasses.replace(entry, symbol="BBBUSDT", canonical_instrument="BYBIT::BBBUSDT::USDT_LINEAR_PERPETUAL")
        for entry in case.instrument_map
    )
    missing_map_payload = _self_hashed(
        {
            "schema_version": 1,
            "artifact_type": "strategy_overhaul_venue_local_instrument_map",
            "version": MAP_VERSION,
            "map_sha256": canonical_json_sha256([dataclasses.asdict(entry) for entry in missing_map]),
            "entry_count": len(missing_map),
            "outcome_values_read": False,
            "outcome_run_authorized": False,
            "real_money_authorized": False,
        }
    )
    case.bindings["map"].path.write_bytes(canonical_json_bytes(missing_map_payload) + b"\n")
    with pytest.raises(ExpectedPopulationError, match="resolve exactly once"):
        build_expected_population_artifacts(
            case.hourly,
            case.manifest,
            _manifest_pairs(case.manifest),
            sleeve="continuous",
            venue="bybit",
            config=case.config,  # type: ignore[arg-type]
            config_identity=case.config_identity,
            config_identity_receipt=case.bindings["config"],
            root_identity_receipt=case.bindings["root"],
            pit_identity_receipt=case.bindings["pit"],
            instrument_map=missing_map,
            instrument_map_version=MAP_VERSION,
            instrument_map_identity_receipt=case.bindings["map"],
        )
