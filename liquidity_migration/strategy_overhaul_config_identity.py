"""Canonical, outcome-blind configuration identities for the A0 scout.

The strategy-overhaul stages need one mechanically derived configuration
identity that is independent of the registered data window.  This module owns
that narrow boundary; it does not load market data, run a strategy, write an
artifact, or authorize an S02/S03/S04 stage.

LONG deliberately keeps the three ``LongNativeConfig`` window fields at their
undated factory values.  The registered causal-read and signal dates live in a
separately hashed scope artifact.  CONTINUOUS resolves the named demo profile
with the deployed BTC gate before serializing it.  Its normalized component
artifact is derived only from ``ensemble_components`` and preserves tuple order;
it is not a reconstruction of historical per-component ``ContinuousEventConfig``
receipts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import asdict, fields, is_dataclass
from typing import Any, TypeAlias, TypedDict, cast

from . import promoted
from .continuous_demo import (
    ContinuousDemoCycleConfig,
    apply_continuous_demo_profile,
)
from .long_native import LongNativeConfig
from .long_native_event_demo import _v11a_long_native_config


CONFIG_IDENTITY_SCHEMA_VERSION = "strategy_overhaul_a0_config_identity_v1"
CANONICAL_CONFIG_ARTIFACT_TYPE = "strategy_overhaul_a0_canonical_config"
SCOPE_ARTIFACT_TYPE = "strategy_overhaul_a0_registered_scope"
COMPONENT_CONFIG_ARTIFACT_TYPE = "strategy_overhaul_a0_component_config"
S02_CONFIG_PARITY_MANIFEST_SCHEMA_VERSION = "strategy_overhaul_a0_s02_config_parity_manifest_v2"
# SHA-256 of the ordered target/field/consumer/validator contract projection.
# Any coverage change must update the builder and this value together; otherwise
# a reduced or substituted set of self-attested targets cannot derive WIRED.
_S02_PARITY_CONTRACT_SHA256 = "bb878d7c3ecfdd969036736def792b8a2ce89a14d7ff5d74b53312bb681fade0"

LONG_WINDOW_FIELDS = ("start_date", "end_date", "read_start_date")


class _ContinuousProfileInputs(TypedDict):
    strategy_profile: str
    btc_trend_gate: str


CONTINUOUS_PROFILE_INPUTS: _ContinuousProfileInputs = {
    "strategy_profile": "continuous_ensemble_v2",
    "btc_trend_gate": "uptrend",
}
CONTINUOUS_COMPONENT_FIELDS = (
    "component",
    "entry_event_trigger",
    "age_days_min",
    "take_profit_pct",
    "weight",
)

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class A0ConfigIdentityError(ValueError):
    """A canonical factory, scope, payload, or hash invariant failed."""


def _json_ready(value: Any) -> JsonValue:
    """Return a strict JSON value without stringifying unsupported objects."""

    if is_dataclass(value) and not isinstance(value, type):
        return _json_ready(asdict(value))
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise A0ConfigIdentityError("configuration identity cannot contain NaN or infinity")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise A0ConfigIdentityError("configuration identity mappings require string keys")
            output[key] = _json_ready(item)
        return output
    raise A0ConfigIdentityError(f"configuration identity contains unsupported value type {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize one JSON-ready value with the repository's canonical settings."""

    ready = _json_ready(value)
    return json.dumps(
        ready,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    """Return the full SHA-256 of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _registered_scope(sleeve: str) -> dict[str, JsonValue]:
    # Lazy import keeps this identity module safe for a future Phase-0 caller;
    # importing it from strategy_overhaul_phase0 must not create a module cycle.
    from .strategy_overhaul_phase0 import REGISTERED_SLEEVE_WINDOWS

    matches = [window for window in REGISTERED_SLEEVE_WINDOWS if window.sleeve == sleeve]
    if len(matches) != 1:
        raise A0ConfigIdentityError(f"expected exactly one registered {sleeve!r} sleeve window; found {len(matches)}")
    window = matches[0]
    return {
        "schema_version": CONFIG_IDENTITY_SCHEMA_VERSION,
        "artifact_type": SCOPE_ARTIFACT_TYPE,
        "sleeve": sleeve,
        "causal_read_start_date": window.causal_read_start_date,
        "signal_start_date": window.signal_start_date,
        "signal_end_date_exclusive": window.signal_end_date_exclusive,
        "timezone": "UTC",
    }


def _canonical_config_artifact(
    *,
    sleeve: str,
    config: object,
    config_type: str,
) -> dict[str, JsonValue]:
    if not is_dataclass(config) or isinstance(config, type):
        raise A0ConfigIdentityError("canonical config must be a dataclass instance")
    ready = _json_ready(asdict(config))
    assert isinstance(ready, dict)
    return {
        "schema_version": CONFIG_IDENTITY_SCHEMA_VERSION,
        "artifact_type": CANONICAL_CONFIG_ARTIFACT_TYPE,
        "sleeve": sleeve,
        "config_type": config_type,
        "config": ready,
    }


def _identity_payload(
    *,
    sleeve: str,
    canonical_config: dict[str, JsonValue],
    scope: dict[str, JsonValue],
    derivation: dict[str, JsonValue],
    component_config: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "schema_version": CONFIG_IDENTITY_SCHEMA_VERSION,
        "artifact_type": "strategy_overhaul_a0_config_identity",
        "sleeve": sleeve,
        "derivation": derivation,
        "canonical_config": canonical_config,
        "canonical_config_sha256": canonical_json_sha256(canonical_config),
        "scope": scope,
        "scope_sha256": canonical_json_sha256(scope),
        "component_config": component_config,
        "component_config_sha256": (canonical_json_sha256(component_config) if component_config is not None else None),
    }
    payload["identity_sha256"] = canonical_json_sha256(payload)
    return payload


def derive_long_a0_config_identity() -> dict[str, JsonValue]:
    """Derive LONG's undated canonical config and separately hashed scope."""

    promoted_config = promoted.long_profile()
    factory_config = _v11a_long_native_config()
    if not isinstance(promoted_config, LongNativeConfig) or not isinstance(factory_config, LongNativeConfig):
        raise A0ConfigIdentityError("LONG factories must return LongNativeConfig")
    if promoted_config != factory_config:
        mismatches = [
            field.name
            for field in fields(LongNativeConfig)
            if getattr(promoted_config, field.name) != getattr(factory_config, field.name)
        ]
        raise A0ConfigIdentityError(
            "promoted.long_profile() disagrees with _v11a_long_native_config(): " + ", ".join(mismatches)
        )
    nonempty_windows = {
        name: getattr(factory_config, name) for name in LONG_WINDOW_FIELDS if getattr(factory_config, name) != ""
    }
    if nonempty_windows:
        raise A0ConfigIdentityError(f"undated LONG factory contains scope dates: {nonempty_windows}")

    canonical_config = _canonical_config_artifact(
        sleeve="long",
        config=factory_config,
        config_type="liquidity_migration.long_native.LongNativeConfig",
    )
    scope = _registered_scope("long")
    return _identity_payload(
        sleeve="long",
        canonical_config=canonical_config,
        scope=scope,
        derivation={
            "config_factories": [
                "liquidity_migration.promoted.long_profile",
                "liquidity_migration.long_native_event_demo._v11a_long_native_config",
            ],
            "exact_factory_equality_checked": True,
            "canonical_window_fields_remain_undated": list(LONG_WINDOW_FIELDS),
            "scope_source": ("liquidity_migration.strategy_overhaul_phase0.REGISTERED_SLEEVE_WINDOWS"),
        },
    )


def _normalized_continuous_components(
    config: ContinuousDemoCycleConfig,
) -> dict[str, JsonValue]:
    rows: list[JsonValue] = []
    seen: set[str] = set()
    for ordinal, raw in enumerate(config.ensemble_components):
        if len(raw) != len(CONTINUOUS_COMPONENT_FIELDS):
            raise A0ConfigIdentityError(
                f"continuous ensemble component must contain exactly {len(CONTINUOUS_COMPONENT_FIELDS)} fields"
            )
        component, trigger, age_days, take_profit_pct, weight = raw
        if not isinstance(component, str) or not component.strip() or component != component.strip():
            raise A0ConfigIdentityError("continuous component name must be canonical and non-blank")
        if component in seen:
            raise A0ConfigIdentityError(f"duplicate continuous component {component!r}")
        seen.add(component)
        if not isinstance(trigger, str) or not trigger.strip() or trigger != trigger.strip():
            raise A0ConfigIdentityError(f"continuous component {component!r} trigger must be canonical and non-blank")
        if isinstance(age_days, bool) or not isinstance(age_days, int) or age_days < 0:
            raise A0ConfigIdentityError(
                f"continuous component {component!r} age_days_min must be a non-negative integer"
            )
        for label, value in (("take_profit_pct", take_profit_pct), ("weight", weight)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise A0ConfigIdentityError(f"continuous component {component!r} {label} must be numeric")
            if not math.isfinite(float(value)) or float(value) < 0.0:
                raise A0ConfigIdentityError(
                    f"continuous component {component!r} {label} must be finite and non-negative"
                )
        rows.append(
            {
                "ordinal": ordinal,
                "component_bit": 1 << ordinal,
                "component": component,
                "entry_event_trigger": trigger,
                "age_days_min": age_days,
                "take_profit_pct": float(take_profit_pct),
                "weight": float(weight),
            }
        )

    return {
        "schema_version": CONFIG_IDENTITY_SCHEMA_VERSION,
        "artifact_type": COMPONENT_CONFIG_ARTIFACT_TYPE,
        "sleeve": "continuous",
        "source_field": "ContinuousDemoCycleConfig.ensemble_components",
        "tuple_fields": list(CONTINUOUS_COMPONENT_FIELDS),
        "components": rows,
    }


def derive_continuous_a0_config_identity() -> dict[str, JsonValue]:
    """Resolve the named demo profile and derive its ordered component artifact."""

    base = ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS)
    resolved = apply_continuous_demo_profile(base)
    if not isinstance(resolved, ContinuousDemoCycleConfig):
        raise A0ConfigIdentityError("apply_continuous_demo_profile must return ContinuousDemoCycleConfig")
    for name, expected in CONTINUOUS_PROFILE_INPUTS.items():
        actual = getattr(resolved, name)
        if actual != expected:
            raise A0ConfigIdentityError(
                f"resolved continuous config changed required input {name}: expected {expected!r}, got {actual!r}"
            )

    canonical_config = _canonical_config_artifact(
        sleeve="continuous",
        config=resolved,
        config_type="liquidity_migration.continuous_demo.ContinuousDemoCycleConfig",
    )
    scope = _registered_scope("continuous")
    component_config = _normalized_continuous_components(resolved)
    return _identity_payload(
        sleeve="continuous",
        canonical_config=canonical_config,
        scope=scope,
        component_config=component_config,
        derivation={
            "base_config_type": ("liquidity_migration.continuous_demo.ContinuousDemoCycleConfig"),
            "resolver": ("liquidity_migration.continuous_demo.apply_continuous_demo_profile"),
            "resolver_inputs": _json_ready(CONTINUOUS_PROFILE_INPUTS),
            "component_source": "resolved_config.ensemble_components",
            "scope_source": ("liquidity_migration.strategy_overhaul_phase0.REGISTERED_SLEEVE_WINDOWS"),
        },
    )


def derive_a0_config_identities() -> dict[str, dict[str, JsonValue]]:
    """Return both sleeve identities in deterministic sleeve-name order."""

    return {
        "continuous": derive_continuous_a0_config_identity(),
        "long": derive_long_a0_config_identity(),
    }


def verify_a0_config_identity(payload: dict[str, JsonValue]) -> None:
    """Fail if a generated identity or one of its child hashes was altered."""

    if payload.get("schema_version") != CONFIG_IDENTITY_SCHEMA_VERSION:
        raise A0ConfigIdentityError("unsupported A0 config identity schema version")
    sleeve = payload.get("sleeve")
    if sleeve not in {"continuous", "long"}:
        raise A0ConfigIdentityError("identity sleeve must be continuous or long")
    canonical_config = payload.get("canonical_config")
    scope = payload.get("scope")
    component_config = payload.get("component_config")
    if not isinstance(canonical_config, dict) or not isinstance(scope, dict):
        raise A0ConfigIdentityError("identity requires canonical_config and scope objects")
    for name, artifact, artifact_type in (
        ("canonical_config", canonical_config, CANONICAL_CONFIG_ARTIFACT_TYPE),
        ("scope", scope, SCOPE_ARTIFACT_TYPE),
    ):
        if artifact.get("schema_version") != CONFIG_IDENTITY_SCHEMA_VERSION:
            raise A0ConfigIdentityError(f"{name} has an unsupported schema version")
        if artifact.get("artifact_type") != artifact_type or artifact.get("sleeve") != sleeve:
            raise A0ConfigIdentityError(f"{name} type/sleeve does not match its identity")
    if sleeve == "continuous":
        if not isinstance(component_config, dict):
            raise A0ConfigIdentityError("continuous identity requires a component config object")
        if (
            component_config.get("schema_version") != CONFIG_IDENTITY_SCHEMA_VERSION
            or component_config.get("artifact_type") != COMPONENT_CONFIG_ARTIFACT_TYPE
            or component_config.get("sleeve") != sleeve
        ):
            raise A0ConfigIdentityError("component config type/sleeve does not match its identity")
    elif component_config is not None:
        raise A0ConfigIdentityError("long identity must not contain a component config")
    if payload.get("canonical_config_sha256") != canonical_json_sha256(canonical_config):
        raise A0ConfigIdentityError("canonical config SHA-256 mismatch")
    if payload.get("scope_sha256") != canonical_json_sha256(scope):
        raise A0ConfigIdentityError("registered scope SHA-256 mismatch")
    expected_component_hash = canonical_json_sha256(component_config) if isinstance(component_config, dict) else None
    if payload.get("component_config_sha256") != expected_component_hash:
        raise A0ConfigIdentityError("component config SHA-256 mismatch")
    observed_identity_hash = payload.get("identity_sha256")
    unhashed = dict(payload)
    unhashed.pop("identity_sha256", None)
    if observed_identity_hash != canonical_json_sha256(unhashed):
        raise A0ConfigIdentityError("A0 config identity SHA-256 mismatch")


def assert_stage_config_matches_identity(
    config: LongNativeConfig | ContinuousDemoCycleConfig,
    identity: dict[str, JsonValue],
) -> None:
    """Compare a stage config with the exact canonical object in an identity."""

    verify_a0_config_identity(identity)
    artifact = identity["canonical_config"]
    if not isinstance(artifact, dict) or not isinstance(artifact.get("config"), dict):
        raise A0ConfigIdentityError("identity canonical_config has no config object")
    actual = _json_ready(asdict(config))
    if actual != artifact["config"]:
        raise A0ConfigIdentityError("stage config does not equal canonical A0 config")


def assert_stage_config_identity_is_current(
    config: LongNativeConfig | ContinuousDemoCycleConfig,
    identity: dict[str, JsonValue],
    *,
    sleeve: str,
) -> None:
    """Bind a stage invocation to the current factory-derived identity.

    Internal hash consistency is insufficient: a caller could alter an identity
    and recompute all of its child hashes.  A runtime stage must therefore match
    both its supplied config and a fresh derivation from the current factories.
    """

    if sleeve == "continuous":
        if not isinstance(config, ContinuousDemoCycleConfig):
            raise A0ConfigIdentityError("continuous stage config must be ContinuousDemoCycleConfig")
        expected = derive_continuous_a0_config_identity()
    elif sleeve == "long":
        if not isinstance(config, LongNativeConfig):
            raise A0ConfigIdentityError("long stage config must be LongNativeConfig")
        expected = derive_long_a0_config_identity()
    else:
        raise A0ConfigIdentityError("stage sleeve must be continuous or long")
    verify_a0_config_identity(identity)
    if identity != expected:
        raise A0ConfigIdentityError(f"supplied {sleeve} config identity does not equal the current canonical identity")
    assert_stage_config_matches_identity(config, identity)


def registered_scope_bounds_ms(
    identity: dict[str, JsonValue],
) -> dict[str, int]:
    """Return exact UTC-midnight scope bounds from a verified identity."""

    verify_a0_config_identity(identity)
    scope = identity.get("scope")
    if not isinstance(scope, dict):  # pragma: no cover - verified above
        raise A0ConfigIdentityError("identity scope must be an object")
    output: dict[str, int] = {}
    for name in (
        "causal_read_start_date",
        "signal_start_date",
        "signal_end_date_exclusive",
    ):
        raw = scope.get(name)
        if not isinstance(raw, str):
            raise A0ConfigIdentityError(f"registered scope {name} must be an ISO date")
        try:
            date = dt.date.fromisoformat(raw)
        except ValueError as exc:
            raise A0ConfigIdentityError(f"registered scope {name} must be an ISO date") from exc
        output[f"{name}_ms"] = int(dt.datetime.combine(date, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)
    if not (
        output["causal_read_start_date_ms"] <= output["signal_start_date_ms"] < output["signal_end_date_exclusive_ms"]
    ):
        raise A0ConfigIdentityError("registered scope dates are not strictly ordered")
    return output


def _continuous_parity_values(identity: dict[str, JsonValue]) -> dict[str, JsonValue]:
    artifact = identity["canonical_config"]
    components_artifact = identity["component_config"]
    assert isinstance(artifact, dict)
    assert isinstance(artifact["config"], dict)
    assert isinstance(components_artifact, dict)
    assert isinstance(components_artifact["components"], list)
    cfg = artifact["config"]
    components = components_artifact["components"]
    typed_components = [row for row in components if isinstance(row, dict)]
    return {
        "full_config_sha256": identity["canonical_config_sha256"],
        "registered_scope_sha256": identity["scope_sha256"],
        "strategy_profile": cfg["strategy_profile"],
        "side": cfg["side"],
        "decile": cfg["decile"],
        "feature_set": cfg["feature_set"],
        "rmom_quantile": cfg["rmom_quantile"],
        "liq_turnover_min": cfg["liq_turnover_min"],
        "entry_confirm_delay_hours": cfg["entry_confirm_delay_hours"],
        "btc_trend_gate": cfg["btc_trend_gate"],
        "btc_trend_lookback_days": cfg["btc_trend_lookback_days"],
        "btc_trend_mode": cfg["btc_trend_mode"],
        "exclude_symbols": cfg["exclude_symbols"],
        "component_config_sha256": identity["component_config_sha256"],
        "component_order": [row["component"] for row in typed_components],
        "component_trigger_by_name": {str(row["component"]): row["entry_event_trigger"] for row in typed_components},
        "component_age_days_min_by_name": {str(row["component"]): row["age_days_min"] for row in typed_components},
        "component_bit_by_name": {str(row["component"]): row["component_bit"] for row in typed_components},
        "component_weight_by_name": {str(row["component"]): row["weight"] for row in typed_components},
    }


def _long_parity_values(identity: dict[str, JsonValue]) -> dict[str, JsonValue]:
    artifact = identity["canonical_config"]
    assert isinstance(artifact, dict)
    assert isinstance(artifact["config"], dict)
    cfg = artifact["config"]
    pattern_fields = [
        "enable_capitulation_rebound",
        "enable_volume_resurrection",
        "enable_funding_squeeze",
        "enable_oversold_bounce",
        "enable_uptrend_dip",
        "enable_fomo_chase",
        "enable_xsec_momentum",
        "enable_lowvol",
        "enable_reversal",
        "enable_funding_carry",
        "enable_oi_momentum",
        "enable_metrics_signal",
    ]
    max_hold_days = cfg["fc_max_hold_days"]
    if isinstance(max_hold_days, bool) or not isinstance(max_hold_days, int):
        raise A0ConfigIdentityError("canonical LONG fc_max_hold_days must be an integer")
    return {
        "full_config_sha256": identity["canonical_config_sha256"],
        "registered_scope_sha256": identity["scope_sha256"],
        "undated_window_fields": {name: cfg[name] for name in LONG_WINDOW_FIELDS},
        "exclude_symbols": cfg["exclude_symbols"],
        "universe_size": cfg["universe_size"],
        "universe_volume_window_days": cfg["universe_volume_window_days"],
        "min_listing_history_days": cfg["min_listing_history_days"],
        "regime_symbol": cfg["regime_symbol"],
        "regime_sma_days": cfg["regime_sma_days"],
        "btc_month_regime_gate": cfg["btc_month_regime_gate"],
        "active_pattern_toggles": {name: cfg[name] for name in pattern_fields},
        "trigger_and_exit_profile": {
            name: cfg[name]
            for name in (
                "fc_min_day_return",
                "fc_use_sigma_threshold",
                "fc_sigma_mult",
                "fc_enable_3d_trigger",
                "fc_enable_7d_trigger",
                "fc_enable_intraday_trigger",
                "fc_intraday_window_hours",
                "fc_use_own_pump_quantile",
                "fc_min_close_location",
                "fc_close_loc_multi_day",
                "fc_use_atr_exits",
                "fc_atr_stop_mult",
                "fc_atr_tp_mult",
                "fc_stop_pct",
                "fc_take_profit_pct",
            )
        },
        "vol_estimate_window_days": cfg["vol_estimate_window_days"],
        "fc_max_hold_days": max_hold_days,
        "fc_exit_max_hold_hours": max_hold_days * 24,
        "fc_lsr_filter": cfg["fc_lsr_filter"],
        "fc_require_oi_rising": cfg["fc_require_oi_rising"],
    }


def _s02_parity_contract_projection(targets: list[JsonValue]) -> list[JsonValue] | None:
    projection: list[JsonValue] = []
    for target in targets:
        if not isinstance(target, dict):
            return None
        expected = target.get("expected")
        consumers = target.get("consumers")
        required = target.get("required_validator_fields")
        validations = target.get("consumer_validations")
        if (
            not isinstance(target.get("sleeve"), str)
            or not isinstance(target.get("target"), str)
            or not isinstance(expected, dict)
            or not isinstance(consumers, list)
            or not isinstance(required, dict)
            or not isinstance(validations, list)
        ):
            return None
        validator_projection: list[JsonValue] = []
        for record in validations:
            if not isinstance(record, dict):
                return None
            validator_projection.append(
                {
                    "consumer_validator": record.get("consumer_validator"),
                    "required_fields": record.get("required_fields"),
                    "required_consumers": record.get("required_consumers"),
                }
            )
        projection.append(
            {
                "sleeve": target["sleeve"],
                "target": target["target"],
                "expected_fields": sorted(expected),
                "consumers": consumers,
                "required_validator_fields": required,
                "validators": validator_projection,
            }
        )
    return projection


def derive_s02_parity_status(targets: list[JsonValue]) -> str:
    """Derive aggregate wiring only from values and validator evidence.

    Per-target ``status`` strings are deliberately ignored.  A target is wired
    only when every independently required validator produced a verified
    record, the observed values exactly equal the canonical expectation, and
    every named consumer was covered by one of those verified records.
    """

    contract = _s02_parity_contract_projection(targets)
    if contract is None or canonical_json_sha256(contract) != _S02_PARITY_CONTRACT_SHA256:
        return "UNWIRED"
    for target in targets:
        if not isinstance(target, dict):
            return "UNWIRED"
        expected = target.get("expected")
        observed = target.get("observed")
        if not isinstance(expected, dict) or observed != expected:
            return "UNWIRED"
        consumers = target.get("consumers")
        checked = target.get("checked_consumers")
        unresolved = target.get("unresolved_consumers")
        if (
            not isinstance(consumers, list)
            or not isinstance(checked, list)
            or checked != consumers
            or not isinstance(unresolved, list)
            or unresolved
        ):
            return "UNWIRED"
        required = target.get("required_validator_fields")
        validations = target.get("consumer_validations")
        if not isinstance(required, dict) or not required or not isinstance(validations, list):
            return "UNWIRED"
        records = {
            record.get("consumer_validator"): record
            for record in validations
            if isinstance(record, dict) and isinstance(record.get("consumer_validator"), str)
        }
        if len(records) != len(validations) or set(records) != set(required):
            return "UNWIRED"
        covered_fields: set[str] = set()
        covered_consumers: set[str] = set()
        for validator, field_names in required.items():
            record = records.get(validator)
            if (
                not isinstance(field_names, list)
                or not field_names
                or not isinstance(record, dict)
                or record.get("required_fields") != field_names
                or record.get("metadata_match") is not True
                or record.get("values_match") is not True
                or record.get("status") != "VERIFIED"
            ):
                return "UNWIRED"
            required_consumers = record.get("required_consumers")
            if not isinstance(required_consumers, list) or any(
                not isinstance(consumer, str) or not consumer for consumer in required_consumers
            ):
                return "UNWIRED"
            covered_fields.update(field_names)
            covered_consumers.update(required_consumers)
        if covered_fields != set(expected) or covered_consumers != set(consumers):
            return "UNWIRED"
    return "WIRED"


def s02_config_parity_manifest(
    identities: dict[str, dict[str, JsonValue]] | None = None,
) -> dict[str, JsonValue]:
    """Derive config parity from canonical values and owner-local validators.

    The manifest owns the required coverage matrix, but it does not assert that
    a consumer was checked.  Each named consumer module returns an executable
    validator receipt containing its exact target fields and consumers.  This
    function calls those validators, checks their metadata and values against
    the independent matrix below, and derives checked consumers solely from
    successful validator records.
    """

    resolved = identities or derive_a0_config_identities()
    continuous = _continuous_parity_values(resolved["continuous"])
    long = _long_parity_values(resolved["long"])
    targets: list[JsonValue] = [
        {
            "sleeve": "continuous",
            "target": "full_config_and_scope_identity",
            "expected": {
                "full_config_sha256": continuous["full_config_sha256"],
                "registered_scope_sha256": continuous["registered_scope_sha256"],
                "component_config_sha256": continuous["component_config_sha256"],
            },
            "consumers": [
                "strategy_overhaul_s02.build_continuous_s02_feature_tape",
                "all downstream CONTINUOUS S03/S04 stage receipts",
            ],
        },
        {
            "sleeve": "continuous",
            "target": "selection_profile",
            "expected": {
                name: continuous[name]
                for name in (
                    "strategy_profile",
                    "side",
                    "decile",
                    "feature_set",
                    "rmom_quantile",
                    "liq_turnover_min",
                )
            },
            "consumers": [
                "continuous_population_scout.CURRENT_RMOM_QUANTILE",
                "continuous_population_scout.CURRENT_LIQUIDITY_FLOOR",
                "continuous_population_scout.build_continuous_feature_tape decile=9 literals",
                "continuous_population_scout.build_continuous_feature_tape max_ret168 literals",
                "strategy_overhaul_stage_receipt._validate_continuous_s02 rmom quantile semantics",
            ],
        },
        {
            "sleeve": "continuous",
            "target": "decision_and_btc_gate",
            "expected": {
                name: continuous[name]
                for name in (
                    "entry_confirm_delay_hours",
                    "btc_trend_gate",
                    "btc_trend_lookback_days",
                    "btc_trend_mode",
                )
            },
            "consumers": [
                "continuous_population_scout.build_continuous_feature_tape +1h decision literals",
                "strategy_overhaul_s02.CANONICAL_BTC_UPTREND_LOOKBACK_DAYS",
                "strategy_overhaul_context.attach_continuous_market_context",
                "strategy_overhaul_context.attach_continuous_static_diagnostics BTC-uptrend pass",
                "strategy_overhaul_stage_receipt._validate_continuous_s02/_validate_continuous_s03 config-derived timing",
            ],
        },
        {
            "sleeve": "continuous",
            "target": "component_identity",
            "expected": {
                name: continuous[name]
                for name in (
                    "component_order",
                    "component_trigger_by_name",
                    "component_age_days_min_by_name",
                    "component_bit_by_name",
                    "component_weight_by_name",
                )
            },
            "consumers": [
                "continuous_population_scout.COMPONENT_BITS",
                "continuous_population_scout.COMPONENT_WEIGHTS",
                "continuous_population_scout.build_continuous_feature_tape trigger/tag/mask literals",
                "strategy_overhaul_context.attach_continuous_static_diagnostics component map",
                "strategy_overhaul_identity_adapter._attach_current_ages >=240 literal",
                "strategy_overhaul_s02 component-field loops",
            ],
        },
        {
            "sleeve": "continuous",
            "target": "population_exclusions",
            "expected": {"exclude_symbols": continuous["exclude_symbols"]},
            "consumers": [
                "strategy_overhaul_expected_population.build_expected_population_artifacts",
            ],
        },
        {
            "sleeve": "long",
            "target": "full_config_and_scope_identity",
            "expected": {
                "full_config_sha256": long["full_config_sha256"],
                "registered_scope_sha256": long["registered_scope_sha256"],
                "undated_window_fields": long["undated_window_fields"],
            },
            "consumers": [
                "long_population_scout._require_frozen_v11a_config",
                "strategy_overhaul_long_s02.build_long_s02_feature_tape",
                "all downstream LONG S03/S04 stage receipts",
            ],
        },
        {
            "sleeve": "long",
            "target": "population_and_rolling_windows",
            "expected": {
                name: long[name]
                for name in (
                    "exclude_symbols",
                    "universe_size",
                    "universe_volume_window_days",
                    "min_listing_history_days",
                    "vol_estimate_window_days",
                )
            },
            "consumers": [
                "strategy_overhaul_expected_population.build_expected_population_artifacts",
                "strategy_overhaul_schemas LONG S02 turnover_median_90d/realized_vol metadata",
                "strategy_overhaul_long_context universe-rank reconstruction",
                "strategy_overhaul_stage_receipt._validate_long_s02 rank/membership semantics",
            ],
        },
        {
            "sleeve": "long",
            "target": "regime_context",
            "expected": {
                name: long[name]
                for name in (
                    "regime_symbol",
                    "regime_sma_days",
                    "btc_month_regime_gate",
                )
            },
            "consumers": [
                "strategy_overhaul_long_context.REGIME_CONTEXT_SCHEMA *_sma_30d fields",
                "strategy_overhaul_long_context._validate_regime_context",
                "strategy_overhaul_long_context._require_feature_columns BTC-month gate-off assumption",
                "strategy_overhaul_long_context._validate_btc_month_context gate-off pass semantics",
            ],
        },
        {
            "sleeve": "long",
            "target": "classifier_and_exit_shape",
            "expected": {
                "active_pattern_toggles": long["active_pattern_toggles"],
                "fc_max_hold_days": long["fc_max_hold_days"],
                "fc_exit_max_hold_hours": long["fc_exit_max_hold_hours"],
            },
            "consumers": [
                "long_population_scout.build_long_feature_tape classifier_selected=fomo_chase",
                "long_population_scout.build_long_feature_tape fc_exit_max_hold_hours",
                "strategy_overhaul_schemas LONG S02 classifier and frozen-72h metadata",
                "strategy_overhaul_stage_receipt._validate_long_s02 max-hold semantics",
            ],
        },
        {
            "sleeve": "long",
            "target": "trigger_and_exit_profile",
            "expected": long["trigger_and_exit_profile"],
            "consumers": [
                "long_population_scout._trigger_diagnostics",
                "long_population_scout._fc_gate_diagnostics",
                "long_population_scout.build_long_feature_tape ATR/fallback exit fields",
                "strategy_overhaul_schemas LONG S02 fixed-15pct/trigger/ATR-exit metadata",
                "strategy_overhaul_stage_receipt._validate_long_s02 ATR-fallback semantics",
            ],
        },
        {
            "sleeve": "long",
            "target": "tier_c_forced_null_gates",
            "expected": {
                "fc_lsr_filter": long["fc_lsr_filter"],
                "fc_require_oi_rising": long["fc_require_oi_rising"],
            },
            "consumers": [
                "strategy_overhaul_long_s02._A0_FORCED_NULL_TIER_C_COLUMNS",
                "strategy_overhaul_schemas LONG S02 global_lsr/oi_chg_7d null semantics",
            ],
        },
    ]
    continuous_s02_validator = "liquidity_migration.strategy_overhaul_s02.continuous_s02_runtime_parity_surface"
    continuous_population_validator = (
        "liquidity_migration.strategy_overhaul_expected_population."
        "continuous_expected_population_consumer_parity_surface"
    )
    long_s02_validator = "liquidity_migration.strategy_overhaul_long_s02.long_s02_runtime_parity_surface"
    long_population_builder_validator = (
        "liquidity_migration.strategy_overhaul_expected_population.long_expected_population_consumer_parity_surface"
    )
    long_row_builder_validator = "liquidity_migration.long_population_scout.long_population_runtime_parity_surface"
    long_context_validator = "liquidity_migration.strategy_overhaul_long_context.long_context_runtime_parity_surface"
    long_schema_validator = "liquidity_migration.strategy_overhaul_schemas.long_schema_runtime_parity_surface"
    stage_receipt_validator = (
        "liquidity_migration.strategy_overhaul_stage_receipt.stage_receipt_config_consumer_parity_surface"
    )

    continuous_config = apply_continuous_demo_profile(ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS))
    long_config = _v11a_long_native_config()
    validator_surfaces: dict[str, dict[str, object]] = {}
    validator_errors: dict[str, str] = {}
    guard_errors: dict[str, str] = {}

    def capture(sleeve: str, validator: str, function: Any, *args: object) -> None:
        try:
            surface = function(*args)
            if not isinstance(surface, dict):
                raise TypeError("consumer validator did not return an object")
            validator_surfaces[validator] = surface
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            validator_errors[validator] = error
            guard_errors.setdefault(sleeve, error)

    from .long_population_scout import long_population_runtime_parity_surface
    from .strategy_overhaul_expected_population import (
        continuous_expected_population_consumer_parity_surface,
        long_expected_population_consumer_parity_surface,
    )
    from .strategy_overhaul_long_context import long_context_runtime_parity_surface
    from .strategy_overhaul_long_s02 import long_s02_runtime_parity_surface
    from .strategy_overhaul_s02 import continuous_s02_runtime_parity_surface
    from .strategy_overhaul_schemas import long_schema_runtime_parity_surface
    from .strategy_overhaul_stage_receipt import stage_receipt_config_consumer_parity_surface

    capture(
        "continuous",
        continuous_s02_validator,
        continuous_s02_runtime_parity_surface,
        continuous_config,
        resolved["continuous"],
    )
    capture(
        "continuous",
        continuous_population_validator,
        continuous_expected_population_consumer_parity_surface,
        continuous_config,
        resolved["continuous"],
    )
    capture(
        "continuous",
        stage_receipt_validator,
        stage_receipt_config_consumer_parity_surface,
        resolved["continuous"],
    )
    capture(
        "long",
        long_s02_validator,
        long_s02_runtime_parity_surface,
        long_config,
        resolved["long"],
    )
    capture(
        "long",
        long_population_builder_validator,
        long_expected_population_consumer_parity_surface,
        long_config,
        resolved["long"],
    )
    capture("long", long_row_builder_validator, long_population_runtime_parity_surface, long_config)
    capture("long", long_context_validator, long_context_runtime_parity_surface, long_config)
    capture("long", long_schema_validator, long_schema_runtime_parity_surface, long_config)
    # The same validator implementation is called separately for each sleeve;
    # retain both owner-local surfaces under sleeve-qualified internal keys.
    long_stage_surface_key = f"{stage_receipt_validator}#long"
    capture(
        "long",
        long_stage_surface_key,
        stage_receipt_config_consumer_parity_surface,
        resolved["long"],
    )

    # validator, required fields, consumers proved by that validator
    requirements: dict[
        tuple[str, str],
        list[tuple[str, list[str], list[str]]],
    ] = {
        ("continuous", "full_config_and_scope_identity"): [
            (
                continuous_s02_validator,
                ["full_config_sha256", "registered_scope_sha256", "component_config_sha256"],
                ["strategy_overhaul_s02.build_continuous_s02_feature_tape"],
            ),
            (
                stage_receipt_validator,
                ["full_config_sha256", "registered_scope_sha256", "component_config_sha256"],
                ["all downstream CONTINUOUS S03/S04 stage receipts"],
            ),
        ],
        ("continuous", "selection_profile"): [
            (
                continuous_s02_validator,
                ["strategy_profile", "side", "decile", "feature_set", "rmom_quantile", "liq_turnover_min"],
                [
                    "continuous_population_scout.CURRENT_RMOM_QUANTILE",
                    "continuous_population_scout.CURRENT_LIQUIDITY_FLOOR",
                    "continuous_population_scout.build_continuous_feature_tape decile=9 literals",
                    "continuous_population_scout.build_continuous_feature_tape max_ret168 literals",
                ],
            ),
            (
                stage_receipt_validator,
                ["rmom_quantile"],
                ["strategy_overhaul_stage_receipt._validate_continuous_s02 rmom quantile semantics"],
            ),
        ],
        ("continuous", "decision_and_btc_gate"): [
            (
                continuous_s02_validator,
                ["entry_confirm_delay_hours", "btc_trend_gate", "btc_trend_lookback_days", "btc_trend_mode"],
                [
                    "continuous_population_scout.build_continuous_feature_tape +1h decision literals",
                    "strategy_overhaul_s02.CANONICAL_BTC_UPTREND_LOOKBACK_DAYS",
                    "strategy_overhaul_context.attach_continuous_market_context",
                    "strategy_overhaul_context.attach_continuous_static_diagnostics BTC-uptrend pass",
                ],
            ),
            (
                stage_receipt_validator,
                ["entry_confirm_delay_hours"],
                [
                    "strategy_overhaul_stage_receipt._validate_continuous_s02/_validate_continuous_s03 config-derived timing"
                ],
            ),
        ],
        ("continuous", "component_identity"): [
            (
                continuous_s02_validator,
                [
                    "component_order",
                    "component_trigger_by_name",
                    "component_age_days_min_by_name",
                    "component_bit_by_name",
                    "component_weight_by_name",
                ],
                [
                    "continuous_population_scout.COMPONENT_BITS",
                    "continuous_population_scout.COMPONENT_WEIGHTS",
                    "continuous_population_scout.build_continuous_feature_tape trigger/tag/mask literals",
                    "strategy_overhaul_context.attach_continuous_static_diagnostics component map",
                    "strategy_overhaul_identity_adapter._attach_current_ages >=240 literal",
                    "strategy_overhaul_s02 component-field loops",
                ],
            )
        ],
        ("continuous", "population_exclusions"): [
            (continuous_s02_validator, ["exclude_symbols"], []),
            (
                continuous_population_validator,
                ["exclude_symbols"],
                ["strategy_overhaul_expected_population.build_expected_population_artifacts"],
            ),
        ],
        ("long", "full_config_and_scope_identity"): [
            (
                long_s02_validator,
                ["full_config_sha256", "registered_scope_sha256", "undated_window_fields"],
                [
                    "long_population_scout._require_frozen_v11a_config",
                    "strategy_overhaul_long_s02.build_long_s02_feature_tape",
                ],
            ),
            (
                long_stage_surface_key,
                ["full_config_sha256", "registered_scope_sha256", "undated_window_fields"],
                ["all downstream LONG S03/S04 stage receipts"],
            ),
        ],
        ("long", "population_and_rolling_windows"): [
            (
                long_s02_validator,
                list(
                    cast(
                        dict[str, JsonValue],
                        next(
                            row
                            for row in targets
                            if isinstance(row, dict)
                            and row["sleeve"] == "long"
                            and row["target"] == "population_and_rolling_windows"
                        )["expected"],
                    )
                ),
                [],
            ),
            (
                long_population_builder_validator,
                [
                    "exclude_symbols",
                    "universe_size",
                    "universe_volume_window_days",
                    "min_listing_history_days",
                    "vol_estimate_window_days",
                ],
                ["strategy_overhaul_expected_population.build_expected_population_artifacts"],
            ),
            (
                long_context_validator,
                ["universe_size", "universe_volume_window_days", "min_listing_history_days"],
                ["strategy_overhaul_long_context universe-rank reconstruction"],
            ),
            (
                long_schema_validator,
                ["universe_volume_window_days", "vol_estimate_window_days"],
                ["strategy_overhaul_schemas LONG S02 turnover_median_90d/realized_vol metadata"],
            ),
            (
                long_stage_surface_key,
                ["universe_size", "universe_volume_window_days", "min_listing_history_days"],
                ["strategy_overhaul_stage_receipt._validate_long_s02 rank/membership semantics"],
            ),
        ],
        ("long", "regime_context"): [
            (long_s02_validator, ["regime_symbol", "regime_sma_days", "btc_month_regime_gate"], []),
            (
                long_context_validator,
                ["regime_symbol", "regime_sma_days", "btc_month_regime_gate"],
                [
                    "strategy_overhaul_long_context.REGIME_CONTEXT_SCHEMA *_sma_30d fields",
                    "strategy_overhaul_long_context._validate_regime_context",
                    "strategy_overhaul_long_context._require_feature_columns BTC-month gate-off assumption",
                    "strategy_overhaul_long_context._validate_btc_month_context gate-off pass semantics",
                ],
            ),
        ],
        ("long", "classifier_and_exit_shape"): [
            (long_s02_validator, ["active_pattern_toggles", "fc_max_hold_days", "fc_exit_max_hold_hours"], []),
            (
                long_row_builder_validator,
                ["active_pattern_toggles", "fc_max_hold_days", "fc_exit_max_hold_hours"],
                [
                    "long_population_scout.build_long_feature_tape classifier_selected=fomo_chase",
                    "long_population_scout.build_long_feature_tape fc_exit_max_hold_hours",
                ],
            ),
            (
                long_schema_validator,
                ["fc_max_hold_days", "fc_exit_max_hold_hours"],
                ["strategy_overhaul_schemas LONG S02 classifier and frozen-72h metadata"],
            ),
            (
                long_stage_surface_key,
                ["fc_max_hold_days"],
                ["strategy_overhaul_stage_receipt._validate_long_s02 max-hold semantics"],
            ),
        ],
        ("long", "trigger_and_exit_profile"): [
            (
                long_s02_validator,
                list(
                    cast(
                        dict[str, JsonValue],
                        next(
                            row
                            for row in targets
                            if isinstance(row, dict)
                            and row["sleeve"] == "long"
                            and row["target"] == "trigger_and_exit_profile"
                        )["expected"],
                    )
                ),
                [],
            ),
            (
                long_row_builder_validator,
                list(
                    cast(
                        dict[str, JsonValue],
                        next(
                            row
                            for row in targets
                            if isinstance(row, dict)
                            and row["sleeve"] == "long"
                            and row["target"] == "trigger_and_exit_profile"
                        )["expected"],
                    )
                ),
                [
                    "long_population_scout._trigger_diagnostics",
                    "long_population_scout._fc_gate_diagnostics",
                    "long_population_scout.build_long_feature_tape ATR/fallback exit fields",
                ],
            ),
            (
                long_schema_validator,
                ["fc_min_day_return", "fc_use_atr_exits"],
                ["strategy_overhaul_schemas LONG S02 fixed-15pct/trigger/ATR-exit metadata"],
            ),
            (
                long_stage_surface_key,
                ["fc_use_atr_exits"],
                ["strategy_overhaul_stage_receipt._validate_long_s02 ATR-fallback semantics"],
            ),
        ],
        ("long", "tier_c_forced_null_gates"): [
            (
                long_s02_validator,
                ["fc_lsr_filter", "fc_require_oi_rising"],
                ["strategy_overhaul_long_s02._A0_FORCED_NULL_TIER_C_COLUMNS"],
            ),
            (
                long_schema_validator,
                ["fc_lsr_filter", "fc_require_oi_rising"],
                ["strategy_overhaul_schemas LONG S02 global_lsr/oi_chg_7d null semantics"],
            ),
        ],
    }

    target_statuses: list[str] = []
    for raw in targets:
        assert isinstance(raw, dict)
        sleeve = str(raw["sleeve"])
        target = str(raw["target"])
        consumers = raw["consumers"]
        expected = raw["expected"]
        assert isinstance(consumers, list) and isinstance(expected, dict)
        target_requirements = requirements[(sleeve, target)]
        required_validator_fields = {
            validator.removesuffix("#long"): field_names
            for validator, field_names, _required_consumers in target_requirements
        }
        validation_records: list[JsonValue] = []
        checked: set[str] = set()
        observed_values: dict[str, JsonValue] = {}
        conflicting_fields: set[str] = set()
        for lookup_validator, field_names, required_consumers in target_requirements:
            declared_validator = lookup_validator.removesuffix("#long")
            surface = validator_surfaces.get(lookup_validator)
            fragment = surface.get(target) if surface is not None else None
            declared_targets = surface.get("validated_targets") if surface is not None else None
            declared_fields = surface.get("validated_target_fields") if surface is not None else None
            declared_consumers = surface.get("validated_consumers") if surface is not None else None
            metadata_match = bool(
                surface is not None
                and surface.get("consumer_validator") == declared_validator
                and isinstance(declared_targets, list)
                and target in declared_targets
                and isinstance(declared_fields, dict)
                and declared_fields.get(target) == field_names
                and isinstance(declared_consumers, dict)
                and declared_consumers.get(target) == required_consumers
                and isinstance(fragment, dict)
                and list(fragment) == field_names
            )
            values_match = bool(
                isinstance(fragment, dict) and all(fragment.get(field) == expected.get(field) for field in field_names)
            )
            if isinstance(fragment, dict):
                for field in field_names:
                    value = fragment.get(field)
                    if field in observed_values and observed_values[field] != value:
                        conflicting_fields.add(field)
                    else:
                        observed_values[field] = cast(JsonValue, value)
            status = "VERIFIED" if metadata_match and values_match else "UNVERIFIED"
            if status == "VERIFIED":
                checked.update(required_consumers)
            validation_records.append(
                {
                    "consumer_validator": declared_validator,
                    "required_fields": field_names,
                    "required_consumers": required_consumers,
                    "metadata_match": metadata_match,
                    "values_match": values_match,
                    "status": status,
                    "error": validator_errors.get(lookup_validator),
                }
            )
        complete_fields = set(observed_values) == set(expected) and not conflicting_fields
        observed: JsonValue = observed_values if complete_fields else None
        unresolved = [consumer for consumer in consumers if consumer not in checked]
        values_match = observed == expected
        raw["observed"] = observed
        raw["values_match"] = values_match
        raw["checked_consumers"] = [consumer for consumer in consumers if consumer in checked]
        raw["unresolved_consumers"] = unresolved
        raw["required_validator_fields"] = required_validator_fields
        raw["consumer_validations"] = validation_records
        target_status = (
            "WIRED"
            if values_match
            and not unresolved
            and all(isinstance(record, dict) and record.get("status") == "VERIFIED" for record in validation_records)
            else "UNWIRED"
        )
        raw["status"] = target_status
        if conflicting_fields:
            raw["conflicting_observed_fields"] = sorted(conflicting_fields)
        target_statuses.append(target_status)
    overall_status = derive_s02_parity_status(targets)
    return {
        "schema_version": S02_CONFIG_PARITY_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "strategy_overhaul_a0_s02_config_parity_manifest",
        "status": overall_status,
        "status_derivation": {
            "rule": (
                "WIRED iff every target has exact expected/observed parity, every independently "
                "required owner validator is VERIFIED, and no named consumer is unresolved"
            ),
            "target_count": len(target_statuses),
            "wired_target_count": target_statuses.count("WIRED"),
            "unwired_target_count": target_statuses.count("UNWIRED"),
            "guard_errors": guard_errors,
            "validator_errors": validator_errors,
        },
        "targets": targets,
    }


__all__ = [
    "A0ConfigIdentityError",
    "CANONICAL_CONFIG_ARTIFACT_TYPE",
    "COMPONENT_CONFIG_ARTIFACT_TYPE",
    "CONFIG_IDENTITY_SCHEMA_VERSION",
    "S02_CONFIG_PARITY_MANIFEST_SCHEMA_VERSION",
    "CONTINUOUS_COMPONENT_FIELDS",
    "CONTINUOUS_PROFILE_INPUTS",
    "LONG_WINDOW_FIELDS",
    "JsonValue",
    "SCOPE_ARTIFACT_TYPE",
    "assert_stage_config_matches_identity",
    "assert_stage_config_identity_is_current",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "derive_a0_config_identities",
    "derive_continuous_a0_config_identity",
    "derive_long_a0_config_identity",
    "derive_s02_parity_status",
    "s02_config_parity_manifest",
    "registered_scope_bounds_ms",
    "verify_a0_config_identity",
]
