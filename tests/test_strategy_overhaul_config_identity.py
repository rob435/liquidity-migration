from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, fields, replace

import pytest

from liquidity_migration import promoted
from liquidity_migration import continuous_population_scout as continuous_scout
from liquidity_migration import long_population_scout as long_scout
from liquidity_migration import strategy_overhaul_expected_population as expected_population_module
from liquidity_migration.continuous_demo import (
    ContinuousDemoCycleConfig,
    apply_continuous_demo_profile,
)
from liquidity_migration.continuous_events import BTC_TREND_MODE_DAILY_PRIOR
from liquidity_migration.continuous_population_scout import (
    COMPONENT_BITS,
    COMPONENT_WEIGHTS,
    CURRENT_LIQUIDITY_FLOOR,
    CURRENT_RMOM_QUANTILE,
)
from liquidity_migration.long_native import LongNativeConfig
from liquidity_migration.long_native_event_demo import _v11a_long_native_config
from liquidity_migration.strategy_overhaul_s02 import CANONICAL_BTC_UPTREND_LOOKBACK_DAYS
from liquidity_migration.strategy_overhaul_phase0 import REGISTERED_SLEEVE_WINDOWS
import liquidity_migration.strategy_overhaul_config_identity as identity_module
from liquidity_migration.strategy_overhaul_config_identity import (
    A0ConfigIdentityError,
    CONFIG_IDENTITY_SCHEMA_VERSION,
    CONTINUOUS_COMPONENT_FIELDS,
    CONTINUOUS_PROFILE_INPUTS,
    LONG_WINDOW_FIELDS,
    assert_stage_config_matches_identity,
    canonical_json_bytes,
    canonical_json_sha256,
    derive_a0_config_identities,
    derive_continuous_a0_config_identity,
    derive_long_a0_config_identity,
    derive_s02_parity_status,
    s02_config_parity_manifest,
    verify_a0_config_identity,
)


def _jsonable_dataclass(value: object) -> object:
    return json.loads(json.dumps(asdict(value), allow_nan=False))


def _window(sleeve: str):
    return next(row for row in REGISTERED_SLEEVE_WINDOWS if row.sleeve == sleeve)


def _target(manifest: dict[str, object], sleeve: str, name: str) -> dict[str, object]:
    targets = manifest["targets"]
    assert isinstance(targets, list)
    matches = [
        row for row in targets if isinstance(row, dict) and row.get("sleeve") == sleeve and row.get("target") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_canonical_json_and_hash_are_deterministic_and_strict() -> None:
    left = {"z": (3, 2), "a": {"b": True}}
    right = {"a": {"b": True}, "z": [3, 2]}
    expected = b'{"a":{"b":true},"z":[3,2]}'

    assert canonical_json_bytes(left) == expected
    assert canonical_json_bytes(right) == expected
    assert canonical_json_sha256(left) == hashlib.sha256(expected).hexdigest()

    with pytest.raises(A0ConfigIdentityError, match="NaN or infinity"):
        canonical_json_bytes({"bad": float("nan")})
    with pytest.raises(A0ConfigIdentityError, match="unsupported value type"):
        canonical_json_bytes({"bad": object()})


def test_long_identity_is_mechanically_undated_and_scope_is_separate() -> None:
    identity = derive_long_a0_config_identity()
    runtime_factory = _v11a_long_native_config()

    assert promoted.long_profile() == runtime_factory
    assert identity["schema_version"] == CONFIG_IDENTITY_SCHEMA_VERSION
    assert identity["canonical_config"]["config"] == _jsonable_dataclass(runtime_factory)
    assert {name: getattr(runtime_factory, name) for name in LONG_WINDOW_FIELDS} == {
        name: "" for name in LONG_WINDOW_FIELDS
    }

    registered = _window("long")
    assert identity["scope"] == {
        "schema_version": CONFIG_IDENTITY_SCHEMA_VERSION,
        "artifact_type": "strategy_overhaul_a0_registered_scope",
        "sleeve": "long",
        "causal_read_start_date": registered.causal_read_start_date,
        "signal_start_date": registered.signal_start_date,
        "signal_end_date_exclusive": registered.signal_end_date_exclusive,
        "timezone": "UTC",
    }

    scoped = replace(
        promoted.long_profile(
            start=registered.signal_start_date,
            end=registered.signal_end_date_exclusive,
        ),
        read_start_date=registered.causal_read_start_date,
    )
    changed = {
        field.name
        for field in fields(LongNativeConfig)
        if getattr(scoped, field.name) != getattr(runtime_factory, field.name)
    }
    assert changed == set(LONG_WINDOW_FIELDS)
    assert all(
        getattr(scoped, field.name) == getattr(runtime_factory, field.name)
        for field in fields(LongNativeConfig)
        if field.name not in LONG_WINDOW_FIELDS
    )

    original_config_hash = identity["canonical_config_sha256"]
    changed_scope = copy.deepcopy(identity["scope"])
    changed_scope["signal_start_date"] = "2099-01-01"
    assert canonical_json_sha256(changed_scope) != identity["scope_sha256"]
    assert identity["canonical_config_sha256"] == original_config_hash
    verify_a0_config_identity(identity)


def test_long_identity_refuses_factory_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    changed = replace(_v11a_long_native_config(), fc_min_day_return=0.123456)
    monkeypatch.setattr(identity_module.promoted, "long_profile", lambda: changed)

    with pytest.raises(A0ConfigIdentityError, match="disagrees"):
        derive_long_a0_config_identity()


def test_long_identity_reads_factory_values_without_transcription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = derive_long_a0_config_identity()
    changed = replace(_v11a_long_native_config(), fc_min_day_return=0.123456)
    monkeypatch.setattr(identity_module.promoted, "long_profile", lambda: changed)
    monkeypatch.setattr(identity_module, "_v11a_long_native_config", lambda: changed)

    derived = derive_long_a0_config_identity()

    assert derived["canonical_config"]["config"] == _jsonable_dataclass(changed)
    assert derived["canonical_config"]["config"]["fc_min_day_return"] == changed.fc_min_day_return
    assert derived["canonical_config_sha256"] != baseline["canonical_config_sha256"]


def test_continuous_identity_is_exact_resolved_config_and_components() -> None:
    identity = derive_continuous_a0_config_identity()
    resolved = apply_continuous_demo_profile(ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS))

    assert identity["canonical_config"]["config"] == _jsonable_dataclass(resolved)
    assert identity["scope"]["signal_start_date"] == _window("continuous").signal_start_date
    component_artifact = identity["component_config"]
    assert component_artifact["tuple_fields"] == list(CONTINUOUS_COMPONENT_FIELDS)
    rows = component_artifact["components"]
    assert len(rows) == len(resolved.ensemble_components)
    for ordinal, (source, row) in enumerate(zip(resolved.ensemble_components, rows, strict=True)):
        assert row["ordinal"] == ordinal
        assert row["component_bit"] == 1 << ordinal
        assert tuple(row[name] for name in CONTINUOUS_COMPONENT_FIELDS) == source

    verify_a0_config_identity(identity)
    json.dumps(identity, sort_keys=True, allow_nan=False)


def test_continuous_identity_reads_resolver_output_without_transcription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = derive_continuous_a0_config_identity()
    resolved = apply_continuous_demo_profile(ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS))
    changed = replace(resolved, rmom_quantile=0.123456)
    monkeypatch.setattr(identity_module, "apply_continuous_demo_profile", lambda _base: changed)

    derived = derive_continuous_a0_config_identity()

    assert derived["canonical_config"]["config"] == _jsonable_dataclass(changed)
    assert derived["canonical_config"]["config"]["rmom_quantile"] == changed.rmom_quantile
    assert derived["canonical_config_sha256"] != baseline["canonical_config_sha256"]


def test_identity_verification_and_stage_comparison_fail_closed() -> None:
    long_identity = derive_long_a0_config_identity()
    continuous_identity = derive_continuous_a0_config_identity()
    resolved_continuous = apply_continuous_demo_profile(ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS))

    assert_stage_config_matches_identity(_v11a_long_native_config(), long_identity)
    assert_stage_config_matches_identity(resolved_continuous, continuous_identity)

    with pytest.raises(A0ConfigIdentityError, match="does not equal"):
        assert_stage_config_matches_identity(
            replace(_v11a_long_native_config(), start_date="2023-06-15"),
            long_identity,
        )
    with pytest.raises(A0ConfigIdentityError, match="does not equal"):
        assert_stage_config_matches_identity(
            replace(resolved_continuous, rmom_quantile=0.5),
            continuous_identity,
        )

    tampered = copy.deepcopy(continuous_identity)
    tampered["canonical_config"]["config"]["rmom_quantile"] = 0.5
    with pytest.raises(A0ConfigIdentityError, match="canonical config SHA-256"):
        verify_a0_config_identity(tampered)

    missing_components = copy.deepcopy(continuous_identity)
    missing_components["component_config"] = None
    missing_components["component_config_sha256"] = None
    unhashed = dict(missing_components)
    unhashed.pop("identity_sha256")
    missing_components["identity_sha256"] = canonical_json_sha256(unhashed)
    with pytest.raises(A0ConfigIdentityError, match="requires a component config"):
        verify_a0_config_identity(missing_components)


def test_combined_identities_are_stable_json_ready_and_ordered() -> None:
    first = derive_a0_config_identities()
    second = derive_a0_config_identities()

    assert tuple(first) == ("continuous", "long")
    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    for payload in first.values():
        verify_a0_config_identity(payload)


def test_s02_parity_manifest_derives_every_expected_value_from_factories() -> None:
    identities = derive_a0_config_identities()
    manifest = s02_config_parity_manifest(identities)
    resolved_continuous = apply_continuous_demo_profile(ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS))
    long_config = _v11a_long_native_config()

    assert manifest["status"] == "WIRED"
    assert manifest["status"] == derive_s02_parity_status(manifest["targets"])
    assert manifest["status_derivation"]["wired_target_count"] == 11
    assert manifest["status_derivation"]["unwired_target_count"] == 0
    assert manifest["status_derivation"]["guard_errors"] == {}
    assert manifest["status_derivation"]["validator_errors"] == {}
    targets = manifest["targets"]
    assert {(row["sleeve"], row["target"]) for row in targets} == {
        ("continuous", "full_config_and_scope_identity"),
        ("continuous", "selection_profile"),
        ("continuous", "decision_and_btc_gate"),
        ("continuous", "component_identity"),
        ("continuous", "population_exclusions"),
        ("long", "full_config_and_scope_identity"),
        ("long", "population_and_rolling_windows"),
        ("long", "regime_context"),
        ("long", "classifier_and_exit_shape"),
        ("long", "trigger_and_exit_profile"),
        ("long", "tier_c_forced_null_gates"),
    }

    selection = _target(manifest, "continuous", "selection_profile")["expected"]
    for name in (
        "strategy_profile",
        "side",
        "decile",
        "feature_set",
        "rmom_quantile",
        "liq_turnover_min",
    ):
        expected = getattr(resolved_continuous, name)
        if isinstance(expected, tuple):
            expected = list(expected)
        assert selection[name] == expected
    assert selection["rmom_quantile"] == CURRENT_RMOM_QUANTILE
    assert selection["liq_turnover_min"] == CURRENT_LIQUIDITY_FLOOR

    decision_gate = _target(manifest, "continuous", "decision_and_btc_gate")["expected"]
    for name in (
        "entry_confirm_delay_hours",
        "btc_trend_gate",
        "btc_trend_lookback_days",
        "btc_trend_mode",
    ):
        assert decision_gate[name] == getattr(resolved_continuous, name)
    assert decision_gate["btc_trend_lookback_days"] == CANONICAL_BTC_UPTREND_LOOKBACK_DAYS
    assert decision_gate["btc_trend_mode"] == BTC_TREND_MODE_DAILY_PRIOR

    component_expected = _target(manifest, "continuous", "component_identity")["expected"]
    component_rows = identities["continuous"]["component_config"]["components"]
    assert component_expected["component_order"] == [row["component"] for row in component_rows]
    assert component_expected["component_bit_by_name"] == {
        row["component"]: row["component_bit"] for row in component_rows
    }
    assert component_expected["component_bit_by_name"] == COMPONENT_BITS
    assert component_expected["component_weight_by_name"] == COMPONENT_WEIGHTS

    long_population = _target(manifest, "long", "population_and_rolling_windows")["expected"]
    for name in (
        "exclude_symbols",
        "universe_size",
        "universe_volume_window_days",
        "min_listing_history_days",
        "vol_estimate_window_days",
    ):
        expected = getattr(long_config, name)
        if isinstance(expected, tuple):
            expected = list(expected)
        assert long_population[name] == expected

    regime = _target(manifest, "long", "regime_context")["expected"]
    assert regime == {
        name: getattr(long_config, name) for name in ("regime_symbol", "regime_sma_days", "btc_month_regime_gate")
    }
    forced_null = _target(manifest, "long", "tier_c_forced_null_gates")["expected"]
    assert forced_null == {
        "fc_lsr_filter": long_config.fc_lsr_filter,
        "fc_require_oi_rising": long_config.fc_require_oi_rising,
    }
    trigger_exit = _target(manifest, "long", "trigger_and_exit_profile")["expected"]
    assert all(value == getattr(long_config, name) for name, value in trigger_exit.items())
    for target in targets:
        assert target["status"] == "WIRED"
        assert target["observed"] == target["expected"]
        assert target["checked_consumers"] == target["consumers"]
        assert target["unresolved_consumers"] == []
        assert target["consumer_validations"]
        assert all(row["status"] == "VERIFIED" for row in target["consumer_validations"])


def test_s02_parity_status_requires_values_and_every_consumer() -> None:
    targets = copy.deepcopy(s02_config_parity_manifest()["targets"])
    assert derive_s02_parity_status(targets) == "WIRED"
    json_roundtrip = json.loads(json.dumps(targets, sort_keys=True))
    assert derive_s02_parity_status(json_roundtrip) == "WIRED"
    drifted = copy.deepcopy(targets)
    drifted[0]["observed"] = {"value": 2}
    assert derive_s02_parity_status(drifted) == "UNWIRED"
    unresolved = copy.deepcopy(targets)
    unresolved[0]["unresolved_consumers"] = unresolved[0]["consumers"]
    assert derive_s02_parity_status(unresolved) == "UNWIRED"
    unchecked = copy.deepcopy(targets)
    unchecked[0]["checked_consumers"] = []
    assert derive_s02_parity_status(unchecked) == "UNWIRED"
    invalid_validation = copy.deepcopy(targets)
    invalid_validation[0]["consumer_validations"][0]["status"] = "UNVERIFIED"
    assert derive_s02_parity_status(invalid_validation) == "UNWIRED"
    forged_validation = copy.deepcopy(targets)
    forged_validation[0]["consumer_validations"][0]["metadata_match"] = False
    assert derive_s02_parity_status(forged_validation) == "UNWIRED"
    reduced = copy.deepcopy(targets[:1])
    assert derive_s02_parity_status(reduced) == "UNWIRED"
    substituted = copy.deepcopy(targets)
    substituted[0]["target"] = "made_up_target"
    assert derive_s02_parity_status(substituted) == "UNWIRED"
    assert derive_s02_parity_status([]) == "UNWIRED"


def test_manifest_requires_consumer_owned_metadata_not_central_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = expected_population_module.continuous_expected_population_consumer_parity_surface

    def missing_consumer(config: object, identity: object) -> dict[str, object]:
        surface = copy.deepcopy(original(config, identity))
        surface["validated_consumers"]["population_exclusions"] = []
        return surface

    monkeypatch.setattr(
        expected_population_module,
        "continuous_expected_population_consumer_parity_surface",
        missing_consumer,
    )

    manifest = s02_config_parity_manifest(derive_a0_config_identities())

    assert manifest["status"] == "UNWIRED"
    target = _target(manifest, "continuous", "population_exclusions")
    assert target["status"] == "UNWIRED"
    assert target["unresolved_consumers"] == [
        "strategy_overhaul_expected_population.build_expected_population_artifacts"
    ]
    validation = next(
        row
        for row in target["consumer_validations"]
        if row["consumer_validator"].endswith("continuous_expected_population_consumer_parity_surface")
    )
    assert validation["metadata_match"] is False
    assert validation["status"] == "UNVERIFIED"


@pytest.mark.parametrize(
    ("module", "name", "value", "sleeve", "message"),
    [
        (continuous_scout, "CURRENT_RMOM_QUANTILE", 0.99, "continuous", "selection-profile parity failed"),
        (long_scout, "LONG_S02_CLASSIFIER_PATTERN", "reversal", "long", "classifier parity failed"),
    ],
)
def test_manifest_fails_closed_on_live_consumer_drift(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    value: object,
    sleeve: str,
    message: str,
) -> None:
    identities = derive_a0_config_identities()
    monkeypatch.setattr(module, name, value)

    manifest = s02_config_parity_manifest(identities)

    assert manifest["status"] == "UNWIRED"
    error = manifest["status_derivation"]["guard_errors"][sleeve]
    assert message in error
    sleeve_targets = [row for row in manifest["targets"] if row["sleeve"] == sleeve]
    assert sleeve_targets
    assert all(row["status"] == "UNWIRED" for row in sleeve_targets)
