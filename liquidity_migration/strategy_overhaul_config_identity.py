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
from typing import Any, TypeAlias, TypedDict

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
        output[f"{name}_ms"] = int(
            dt.datetime.combine(date, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000
        )
    if not (
        output["causal_read_start_date_ms"]
        <= output["signal_start_date_ms"]
        < output["signal_end_date_exclusive_ms"]
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


def derive_s02_parity_status(targets: list[JsonValue]) -> str:
    """Derive the aggregate status without trusting per-target declarations."""

    if not targets:
        return "UNWIRED"
    for target in targets:
        if not isinstance(target, dict):
            return "UNWIRED"
        unresolved = target.get("unresolved_consumers")
        if not isinstance(unresolved, list) or unresolved:
            return "UNWIRED"
        if target.get("observed") != target.get("expected"):
            return "UNWIRED"
    return "WIRED"


def s02_config_parity_manifest(
    identities: dict[str, dict[str, JsonValue]] | None = None,
) -> dict[str, JsonValue]:
    """Derive config-parity status from canonical values and live S02 guards.

    ``expected`` always comes from the canonical factories. ``observed`` comes
    from the same guard functions invoked by the S02 builders and therefore
    reflects the module globals and config flow actually used at runtime.  A
    target is ``WIRED`` only when its values match and every listed consumer has
    a mechanical check.  Future population builders and downstream receipt
    propagation remain explicit unresolved consumers rather than declarations.
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
                "future receipt-bound CONTINUOUS expected-population builder",
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
                "future receipt-bound LONG expected-population builder",
                "strategy_overhaul_schemas LONG S02 turnover_median_90d/realized_vol metadata",
                "strategy_overhaul_long_context universe-rank reconstruction",
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
    guard_surfaces: dict[str, dict[str, dict[str, JsonValue]]] = {}
    guard_errors: dict[str, str] = {}
    try:
        from .strategy_overhaul_s02 import continuous_s02_runtime_parity_surface

        continuous_config = apply_continuous_demo_profile(ContinuousDemoCycleConfig(**CONTINUOUS_PROFILE_INPUTS))
        guard_surfaces["continuous"] = continuous_s02_runtime_parity_surface(
            continuous_config,
            resolved["continuous"],
        )
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        guard_errors["continuous"] = f"{type(exc).__name__}: {exc}"
    try:
        from .strategy_overhaul_long_s02 import long_s02_runtime_parity_surface

        guard_surfaces["long"] = long_s02_runtime_parity_surface(
            _v11a_long_native_config(),
            resolved["long"],
        )
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        guard_errors["long"] = f"{type(exc).__name__}: {exc}"

    checked_consumers: dict[tuple[str, str], set[str]] = {
        (
            "continuous",
            "full_config_and_scope_identity",
        ): {"strategy_overhaul_s02.build_continuous_s02_feature_tape"},
        ("continuous", "selection_profile"): {
            "continuous_population_scout.CURRENT_RMOM_QUANTILE",
            "continuous_population_scout.CURRENT_LIQUIDITY_FLOOR",
            "continuous_population_scout.build_continuous_feature_tape decile=9 literals",
            "continuous_population_scout.build_continuous_feature_tape max_ret168 literals",
        },
        ("continuous", "decision_and_btc_gate"): {
            "continuous_population_scout.build_continuous_feature_tape +1h decision literals",
            "strategy_overhaul_s02.CANONICAL_BTC_UPTREND_LOOKBACK_DAYS",
            "strategy_overhaul_context.attach_continuous_market_context",
            "strategy_overhaul_context.attach_continuous_static_diagnostics BTC-uptrend pass",
        },
        ("continuous", "component_identity"): {
            "continuous_population_scout.COMPONENT_BITS",
            "continuous_population_scout.COMPONENT_WEIGHTS",
            "continuous_population_scout.build_continuous_feature_tape trigger/tag/mask literals",
            "strategy_overhaul_context.attach_continuous_static_diagnostics component map",
            "strategy_overhaul_identity_adapter._attach_current_ages >=240 literal",
            "strategy_overhaul_s02 component-field loops",
        },
        ("long", "full_config_and_scope_identity"): {
            "long_population_scout._require_frozen_v11a_config",
            "strategy_overhaul_long_s02.build_long_s02_feature_tape",
        },
        ("long", "regime_context"): {
            "strategy_overhaul_long_context.REGIME_CONTEXT_SCHEMA *_sma_30d fields",
            "strategy_overhaul_long_context._validate_regime_context",
            "strategy_overhaul_long_context._require_feature_columns BTC-month gate-off assumption",
            "strategy_overhaul_long_context._validate_btc_month_context gate-off pass semantics",
        },
        ("long", "classifier_and_exit_shape"): {
            "long_population_scout.build_long_feature_tape classifier_selected=fomo_chase",
            "long_population_scout.build_long_feature_tape fc_exit_max_hold_hours",
        },
        ("long", "trigger_and_exit_profile"): {
            "long_population_scout._trigger_diagnostics",
            "long_population_scout._fc_gate_diagnostics",
            "long_population_scout.build_long_feature_tape ATR/fallback exit fields",
        },
        ("long", "tier_c_forced_null_gates"): {
            "strategy_overhaul_long_s02._A0_FORCED_NULL_TIER_C_COLUMNS",
        },
    }
    target_statuses: list[str] = []
    for raw in targets:
        assert isinstance(raw, dict)
        sleeve = str(raw["sleeve"])
        target = str(raw["target"])
        consumers = raw["consumers"]
        assert isinstance(consumers, list)
        observed = (guard_surfaces.get(sleeve) or {}).get(target)
        checked = checked_consumers.get((sleeve, target), set()) if observed is not None else set()
        unresolved = [consumer for consumer in consumers if consumer not in checked]
        values_match = observed == raw["expected"]
        status = "WIRED" if values_match and not unresolved else "UNWIRED"
        raw["observed"] = observed
        raw["values_match"] = values_match
        raw["checked_consumers"] = [consumer for consumer in consumers if consumer in checked]
        raw["unresolved_consumers"] = unresolved
        raw["status"] = status
        if sleeve in guard_errors:
            raw["guard_error"] = guard_errors[sleeve]
        target_statuses.append(status)
    overall_status = derive_s02_parity_status(targets)
    return {
        "schema_version": CONFIG_IDENTITY_SCHEMA_VERSION,
        "artifact_type": "strategy_overhaul_a0_s02_config_parity_manifest",
        "status": overall_status,
        "status_derivation": {
            "rule": "WIRED iff every target has exact expected/observed parity and no unresolved consumer",
            "target_count": len(target_statuses),
            "wired_target_count": target_statuses.count("WIRED"),
            "unwired_target_count": target_statuses.count("UNWIRED"),
            "guard_errors": guard_errors,
        },
        "targets": targets,
    }


__all__ = [
    "A0ConfigIdentityError",
    "CANONICAL_CONFIG_ARTIFACT_TYPE",
    "COMPONENT_CONFIG_ARTIFACT_TYPE",
    "CONFIG_IDENTITY_SCHEMA_VERSION",
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
