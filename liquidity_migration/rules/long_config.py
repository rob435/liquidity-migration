"""Resolved LONG configuration consumed by native Rust replays."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Mapping

from liquidity_migration.core._common import exact_duration_ms
from liquidity_migration.rules.long_models import LONG_MODEL_SCHEMA_VERSION
from liquidity_migration.rules.long_native import (
    LongNativeConfig,
    resolve_long_strategy_profile,
)


LONG_SIGNAL_FRESHNESS_MS = exact_duration_ms(hours=24)
LONG_BOOK_VALIDITY_MS = exact_duration_ms(hours=1)


@dataclass(frozen=True, slots=True)
class ConfigLayer:
    """One ordered configuration layer and the source that supplied it."""

    source: str
    values: Mapping[str, object]
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    field: str
    source: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """One effective LONG rule, sizing contract, and execution assumptions."""

    profile_name: str
    rule: LongNativeConfig
    notional_multiplier: float
    entry_leverage: float
    order_notional_pct_equity: float
    wallet_balance_fraction: float
    max_new_entries_per_cycle: int
    round_trip_cost_bps: float
    signal_freshness_ms: int
    book_validity_ms: int
    entry_floor_usdt: float
    resize_floor_usdt: float
    resize_floor_fraction: float
    engine_entry_cutoff_ms: int
    provenance: tuple[FieldProvenance, ...]

    def provenance_by_field(self) -> dict[str, dict[str, str]]:
        return {
            item.field: {"source": item.source, "detail": item.detail}
            for item in self.provenance
        }

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": LONG_MODEL_SCHEMA_VERSION,
            "profile_name": self.profile_name,
            "rule": asdict(self.rule),
            "execution": {
                "notional_multiplier": self.notional_multiplier,
                "entry_leverage": self.entry_leverage,
                "order_notional_pct_equity": self.order_notional_pct_equity,
                "wallet_balance_fraction": self.wallet_balance_fraction,
                "max_new_entries_per_cycle": self.max_new_entries_per_cycle,
                "round_trip_cost_bps": self.round_trip_cost_bps,
                "signal_freshness_ms": self.signal_freshness_ms,
                "book_validity_ms": self.book_validity_ms,
                "entry_floor_usdt": self.entry_floor_usdt,
                "resize_floor_usdt": self.resize_floor_usdt,
                "resize_floor_fraction": self.resize_floor_fraction,
                "engine_entry_cutoff_ms": self.engine_entry_cutoff_ms,
            },
            "provenance": self.provenance_by_field(),
        }


_EXECUTION_DEFAULTS: dict[str, object] = {
    "notional_multiplier": 1.0,
    "entry_leverage": 10.0,
    "order_notional_pct_equity": 0.0,
    "wallet_balance_fraction": 1.0,
    "max_new_entries_per_cycle": 5,
    "round_trip_cost_bps": 0.0,
    "signal_freshness_ms": LONG_SIGNAL_FRESHNESS_MS,
    "book_validity_ms": LONG_BOOK_VALIDITY_MS,
    "entry_floor_usdt": 6.0,
    "resize_floor_usdt": 1.0,
    "resize_floor_fraction": 0.05,
    "engine_entry_cutoff_ms": exact_duration_ms(minutes=15),
}
_EXECUTION_FIELDS = frozenset(
    field.name
    for field in fields(StrategyConfig)
    if field.name not in {"profile_name", "rule", "provenance"}
)


def resolve_strategy_config(
    profile_name: str,
    *,
    rule: LongNativeConfig | None = None,
    layers: tuple[ConfigLayer, ...] = (),
    rule_source: str | None = None,
) -> StrategyConfig:
    """Resolve ordered low-to-high layers once and retain every winning source."""

    supplied_rule = rule is not None
    selected_rule = rule or resolve_long_strategy_profile(profile_name)
    normalized_profile = str(profile_name).strip().lower()
    registered = resolve_long_strategy_profile(normalized_profile)
    if selected_rule.execution_strategy_id != registered.execution_strategy_id:
        raise ValueError("LONG profile selector and execution strategy identity disagree")

    values = dict(_EXECUTION_DEFAULTS)
    default_source = rule_source or (
        f"supplied_rule:{normalized_profile}"
        if supplied_rule
        else f"registered_profile:{normalized_profile}"
    )
    provenance: dict[str, FieldProvenance] = {
        f"rule.{field.name}": FieldProvenance(
            field=f"rule.{field.name}",
            source=default_source,
        )
        for field in fields(LongNativeConfig)
    }
    for field in _EXECUTION_FIELDS:
        source = (
            "engine_plan_rules_fleet"
            if field
            in {
                "entry_floor_usdt",
                "resize_floor_usdt",
                "resize_floor_fraction",
                "engine_entry_cutoff_ms",
            }
            else "long_config_default"
        )
        provenance[field] = FieldProvenance(field=field, source=source)

    for layer in layers:
        if not layer.source.strip():
            raise ValueError("LONG config layer source cannot be empty")
        unknown = sorted(set(layer.values) - _EXECUTION_FIELDS)
        if unknown:
            raise ValueError(f"unknown effective LONG config fields: {unknown}")
        for field, value in layer.values.items():
            values[field] = value
            provenance[field] = FieldProvenance(
                field=field,
                source=layer.source,
                detail=layer.detail,
            )

    output = StrategyConfig(
        profile_name=normalized_profile,
        rule=selected_rule,
        notional_multiplier=_finite_positive(
            values["notional_multiplier"],
            label="notional_multiplier",
        ),
        entry_leverage=_finite_positive(
            values["entry_leverage"],
            label="entry_leverage",
        ),
        order_notional_pct_equity=_finite_nonnegative(
            values["order_notional_pct_equity"],
            label="order_notional_pct_equity",
        ),
        wallet_balance_fraction=_finite_positive(
            values["wallet_balance_fraction"],
            label="wallet_balance_fraction",
        ),
        max_new_entries_per_cycle=_positive_int(
            values["max_new_entries_per_cycle"],
            label="max_new_entries_per_cycle",
        ),
        round_trip_cost_bps=_finite_nonnegative(
            values["round_trip_cost_bps"],
            label="round_trip_cost_bps",
        ),
        signal_freshness_ms=_positive_int(
            values["signal_freshness_ms"],
            label="signal_freshness_ms",
        ),
        book_validity_ms=_positive_int(
            values["book_validity_ms"],
            label="book_validity_ms",
        ),
        entry_floor_usdt=_finite_nonnegative(
            values["entry_floor_usdt"],
            label="entry_floor_usdt",
        ),
        resize_floor_usdt=_finite_nonnegative(
            values["resize_floor_usdt"],
            label="resize_floor_usdt",
        ),
        resize_floor_fraction=_finite_nonnegative(
            values["resize_floor_fraction"],
            label="resize_floor_fraction",
        ),
        engine_entry_cutoff_ms=_positive_int(
            values["engine_entry_cutoff_ms"],
            label="engine_entry_cutoff_ms",
        ),
        provenance=tuple(provenance[field] for field in sorted(provenance)),
    )
    if output.order_notional_pct_equity > 10.0:
        raise ValueError("order_notional_pct_equity cannot exceed 10")
    if output.wallet_balance_fraction > 1.0:
        raise ValueError("wallet_balance_fraction cannot exceed 1")
    if output.resize_floor_fraction >= 1.0:
        raise ValueError("resize_floor_fraction must be below 1")
    if output.book_validity_ms <= output.engine_entry_cutoff_ms:
        raise ValueError("book validity must clear the engine entry cutoff")
    return output


def profile_name_for_rule(rule: LongNativeConfig) -> str:
    """Return the registered selector for a rule's persisted identity."""

    for name in ("v11a", "v12"):
        if (
            resolve_long_strategy_profile(name).execution_strategy_id
            == rule.execution_strategy_id
        ):
            return name
    raise ValueError(
        f"unsupported LONG execution strategy identity: {rule.execution_strategy_id!r}"
    )


def _finite_positive(value: object, *, label: str) -> float:
    output = _finite_nonnegative(value, label=label)
    if output <= 0.0:
        raise ValueError(f"{label} must be positive")
    return output


def _finite_nonnegative(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        output = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(output) or output < 0.0:
        raise ValueError(f"{label} must be finite and non-negative")
    return output


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


__all__ = [
    "ConfigLayer",
    "FieldProvenance",
    "LONG_BOOK_VALIDITY_MS",
    "LONG_SIGNAL_FRESHNESS_MS",
    "StrategyConfig",
    "profile_name_for_rule",
    "resolve_strategy_config",
]
