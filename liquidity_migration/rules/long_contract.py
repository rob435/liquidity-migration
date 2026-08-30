"""Pure LONG decisions shared by target production and historical replay.

The contract stops at an absolute desired position.  The Rust engine remains
the owner of quoting, dead-band rebalancing, fills, venue stops, and the WAL.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from typing import Any, Mapping

from liquidity_migration.core._common import exact_duration_ms, is_weekend_ms
from liquidity_migration.rules.long_native import (
    LongNativeConfig,
    _classify_entry,
    _safe_float,
    _vol_target_scale,
    resolve_long_strategy_profile,
)


LONG_CONTRACT_SCHEMA_VERSION = 1
LONG_SIGNAL_FRESHNESS_MS = exact_duration_ms(hours=24)
LONG_BOOK_VALIDITY_MS = exact_duration_ms(hours=1)


class DecisionAction(StrEnum):
    """The only state changes the LONG producer can request."""

    WAIT = "wait"
    ENTER = "enter"
    HOLD = "hold"
    EXIT = "exit"
    REJECT = "reject"


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
    # These are Rust planner physics, carried for replay identity.  ``decide``
    # emits a desired target and does not apply them itself.
    entry_floor_usdt: float
    resize_floor_usdt: float
    resize_floor_fraction: float
    engine_entry_cutoff_ms: int
    provenance: tuple[FieldProvenance, ...]

    def provenance_by_field(self) -> dict[str, dict[str, str]]:
        return {item.field: {"source": item.source, "detail": item.detail} for item in self.provenance}

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": LONG_CONTRACT_SCHEMA_VERSION,
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
    # engine-strategies target_book::plan::PlanRules::FLEET
    "entry_floor_usdt": 6.0,
    "resize_floor_usdt": 1.0,
    "resize_floor_fraction": 0.05,
    "engine_entry_cutoff_ms": exact_duration_ms(minutes=15),
}
_EXECUTION_FIELDS = frozenset(
    field.name for field in fields(StrategyConfig) if field.name not in {"profile_name", "rule", "provenance"}
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
    # Resolve the selector even when a caller supplies a dated copy of the
    # rule: the persisted strategy identity may not disagree with its name.
    registered = resolve_long_strategy_profile(normalized_profile)
    if selected_rule.execution_strategy_id != registered.execution_strategy_id:
        raise ValueError("LONG profile selector and execution strategy identity disagree")

    values = {
        **_EXECUTION_DEFAULTS,
    }
    default_source = rule_source or (
        f"supplied_rule:{normalized_profile}" if supplied_rule else f"registered_profile:{normalized_profile}"
    )
    provenance: dict[str, FieldProvenance] = {
        f"rule.{field.name}": FieldProvenance(field=f"rule.{field.name}", source=default_source)
        for field in fields(LongNativeConfig)
    }
    for field in _EXECUTION_FIELDS:
        if field in {
            "entry_floor_usdt",
            "resize_floor_usdt",
            "resize_floor_fraction",
            "engine_entry_cutoff_ms",
        }:
            source = "engine_plan_rules_fleet"
        else:
            source = "long_contract_default"
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
        notional_multiplier=_finite_positive(values["notional_multiplier"], label="notional_multiplier"),
        entry_leverage=_finite_positive(values["entry_leverage"], label="entry_leverage"),
        order_notional_pct_equity=_finite_nonnegative(
            values["order_notional_pct_equity"],
            label="order_notional_pct_equity",
        ),
        wallet_balance_fraction=_finite_positive(values["wallet_balance_fraction"], label="wallet_balance_fraction"),
        max_new_entries_per_cycle=_positive_int(
            values["max_new_entries_per_cycle"],
            label="max_new_entries_per_cycle",
        ),
        round_trip_cost_bps=_finite_nonnegative(values["round_trip_cost_bps"], label="round_trip_cost_bps"),
        signal_freshness_ms=_positive_int(values["signal_freshness_ms"], label="signal_freshness_ms"),
        book_validity_ms=_positive_int(values["book_validity_ms"], label="book_validity_ms"),
        entry_floor_usdt=_finite_nonnegative(values["entry_floor_usdt"], label="entry_floor_usdt"),
        resize_floor_usdt=_finite_nonnegative(values["resize_floor_usdt"], label="resize_floor_usdt"),
        resize_floor_fraction=_finite_nonnegative(values["resize_floor_fraction"], label="resize_floor_fraction"),
        engine_entry_cutoff_ms=_positive_int(values["engine_entry_cutoff_ms"], label="engine_entry_cutoff_ms"),
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
        if resolve_long_strategy_profile(name).execution_strategy_id == rule.execution_strategy_id:
            return name
    raise ValueError(f"unsupported LONG execution strategy identity: {rule.execution_strategy_id!r}")


@dataclass(frozen=True, slots=True)
class DecisionInput:
    decision_ts_ms: int
    symbol: str
    signal_ts_ms: int = 0
    signal_close: float = 0.0
    market_price: float | None = None
    # A historical bar may prove a lower price occurred before its close.  A
    # live ticker event leaves this unset and supplies the observed tick as
    # ``market_price``.  The difference stays explicit in the event tape.
    observed_low: float | None = None
    equity_usdt: float = 1.0
    feature_row: Mapping[str, Any] | None = None

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": LONG_CONTRACT_SCHEMA_VERSION,
            "decision_ts_ms": self.decision_ts_ms,
            "symbol": self.symbol,
            "signal_ts_ms": self.signal_ts_ms,
            "signal_close": self.signal_close,
            "market_price": self.market_price,
            "observed_low": self.observed_low,
            "equity_usdt": self.equity_usdt,
            "feature_row": dict(self.feature_row or {}),
        }


@dataclass(frozen=True, slots=True)
class PriorState:
    requested: bool = False
    filled: bool = False
    entry_ts_ms: int = 0
    entry_price: float = 0.0
    target_notional_usdt: float = 0.0
    stop_loss_fraction: float = 0.0
    stop_decay_after_ms: int = 0
    decayed_stop_loss_fraction: float = 0.0
    max_hold_deadline_ts_ms: int = 0
    entry_valid_until_ms: int = 0
    cooldown_until_ms: int = 0
    attempted_signal_ts_ms: int = 0
    active_positions: int = 0

    def as_json_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DecisionOutput:
    action: DecisionAction
    reason: str
    decision_ts_ms: int
    symbol: str
    signal_ts_ms: int = 0
    entry_reason: str = ""
    position_weight: float = 0.0
    target_fraction_of_equity: float = 0.0
    target_notional_usdt: float = 0.0
    entry_leverage: float = 0.0
    stop_loss_fraction: float = 0.0
    stop_decay_after_ms: int = 0
    decayed_stop_loss_fraction: float = 0.0
    max_hold_duration_ms: int = 0
    entry_valid_until_ms: int = 0
    wake_at_or_below: float | None = None

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": LONG_CONTRACT_SCHEMA_VERSION,
            "action": self.action.value,
            "reason": self.reason,
            "decision_ts_ms": self.decision_ts_ms,
            "symbol": self.symbol,
            "signal_ts_ms": self.signal_ts_ms,
            "entry_reason": self.entry_reason,
            "position_weight": self.position_weight,
            "target_fraction_of_equity": self.target_fraction_of_equity,
            "target_notional_usdt": self.target_notional_usdt,
            "entry_leverage": self.entry_leverage,
            "stop_loss_fraction": self.stop_loss_fraction,
            "stop_decay_after_ms": self.stop_decay_after_ms,
            "decayed_stop_loss_fraction": self.decayed_stop_loss_fraction,
            "max_hold_duration_ms": self.max_hold_duration_ms,
            "entry_valid_until_ms": self.entry_valid_until_ms,
            "wake_at_or_below": self.wake_at_or_below,
        }


def decide(
    decision_input: DecisionInput,
    prior_state: PriorState,
    config: StrategyConfig,
) -> DecisionOutput:
    """Return one deterministic desired-position decision without side effects."""

    _validate_decision_identity(decision_input)
    now_ms = decision_input.decision_ts_ms
    symbol = decision_input.symbol

    if prior_state.requested:
        return _decide_existing(
            decision_input=decision_input,
            prior_state=prior_state,
            config=config,
        )

    row = dict(decision_input.feature_row or {})
    if not row:
        return _output(DecisionAction.REJECT, "no_signal", decision_input)
    row_symbol = str(row.get("symbol") or "").upper()
    row_ts_ms = _int_value(row.get("ts_ms"))
    signal_ts_ms = decision_input.signal_ts_ms or row_ts_ms
    row_close = _safe_float(row.get("close"))
    supplied_signal_close = _safe_float(decision_input.signal_close)
    signal_close = supplied_signal_close if decision_input.signal_close != 0.0 else row_close
    if (
        row_symbol != symbol
        or row_ts_ms != signal_ts_ms
        or signal_close is None
        or signal_close <= 0.0
        or row_close is None
        or row_close <= 0.0
        or signal_close != row_close
    ):
        return _output(DecisionAction.REJECT, "invalid_signal_identity", decision_input)

    pattern, stop_loss_fraction, hold_days = _classify_entry(row, config.rule)
    if pattern != "fomo_chase":
        return _output(
            DecisionAction.REJECT,
            "no_signal",
            decision_input,
            signal_ts_ms=signal_ts_ms,
        )
    age_ms = now_ms - signal_ts_ms
    if age_ms < 0:
        return _output(
            DecisionAction.REJECT,
            "signal_not_available",
            decision_input,
            signal_ts_ms=signal_ts_ms,
        )
    if age_ms >= config.signal_freshness_ms:
        return _output(
            DecisionAction.REJECT,
            "signal_stale",
            decision_input,
            signal_ts_ms=signal_ts_ms,
        )
    if prior_state.cooldown_until_ms > now_ms:
        return _output(
            DecisionAction.REJECT,
            "cooldown",
            decision_input,
            signal_ts_ms=signal_ts_ms,
        )
    if signal_ts_ms <= prior_state.attempted_signal_ts_ms:
        return _output(
            DecisionAction.REJECT,
            "signal_already_attempted",
            decision_input,
            signal_ts_ms=signal_ts_ms,
        )
    if prior_state.active_positions >= config.rule.max_concurrent_positions:
        return _output(
            DecisionAction.REJECT,
            "capacity",
            decision_input,
            signal_ts_ms=signal_ts_ms,
        )

    first_check_ms = signal_ts_ms + exact_duration_ms(hours=max(1, config.rule.entry_delay_hours))
    retrace_threshold = signal_close * (1.0 - config.rule.fc_sniper_retrace_pct)
    if now_ms < first_check_ms:
        return _output(
            DecisionAction.WAIT,
            "entry_delay",
            decision_input,
            signal_ts_ms=signal_ts_ms,
            wake_at_or_below=retrace_threshold,
        )
    market_price = _safe_float(decision_input.market_price)
    observed_low = _safe_float(decision_input.observed_low)
    observed_price = (
        min(value for value in (market_price, observed_low) if value is not None)
        if market_price is not None or observed_low is not None
        else None
    )
    if observed_price is None or observed_price <= 0.0 or market_price is None or market_price <= 0.0:
        return _output(
            DecisionAction.WAIT,
            "no_market_price",
            decision_input,
            signal_ts_ms=signal_ts_ms,
            wake_at_or_below=retrace_threshold,
        )
    deadline_ms = signal_ts_ms + exact_duration_ms(hours=config.rule.fc_sniper_deadline_hours)
    if observed_price <= retrace_threshold:
        entry_reason = "sniper_retrace"
    elif now_ms >= deadline_ms:
        entry_reason = "sniper_deadline_fallthru"
    else:
        return _output(
            DecisionAction.WAIT,
            "awaiting_retrace",
            decision_input,
            signal_ts_ms=signal_ts_ms,
            wake_at_or_below=retrace_threshold,
        )

    scaled_base_notional_fraction, _vol_target = scaled_base_target_fraction(
        config,
        btc_realized_vol=_safe_float(row.get("btc_rv_30")),
    )
    realized_vol = _safe_float(row.get("realized_vol")) or config.rule.vol_floor_annual
    position_weight = _vol_parity_weight(
        realized_vol=realized_vol,
        vol_floor=config.rule.vol_floor_annual,
        max_position_weight=config.rule.max_position_weight,
        notional_weight=(config.rule.gross_exposure / max(config.rule.max_concurrent_positions, 1)),
    )
    if config.rule.weekend_size_mult != 1.0 and is_weekend_ms(now_ms):
        position_weight *= config.rule.weekend_size_mult
    target_fraction = config.wallet_balance_fraction * scaled_base_notional_fraction * position_weight
    target_notional = decision_input.equity_usdt * target_fraction
    if (
        not math.isfinite(target_fraction)
        or target_fraction <= 0.0
        or not math.isfinite(target_notional)
        or target_notional <= 0.0
        or not 0.0 < stop_loss_fraction < 1.0
        or hold_days <= 0
    ):
        return _output(
            DecisionAction.REJECT,
            "invalid_entry_plan",
            decision_input,
            signal_ts_ms=signal_ts_ms,
        )
    atr_pct = _safe_float(row.get("atr_14d_pct")) or 0.0
    stop_decay_after_ms = 0
    decayed_stop_loss_fraction = 0.0
    if config.rule.fc_stop_time_decay_hours > 0 and config.rule.fc_stop_time_decay_atr_mult > 0.0 and atr_pct > 0.0:
        stop_decay_after_ms = exact_duration_ms(hours=config.rule.fc_stop_time_decay_hours)
        decayed_stop_loss_fraction = config.rule.fc_stop_time_decay_atr_mult * atr_pct
    max_hold_duration_ms = exact_duration_ms(days=hold_days)
    entry_valid_until_ms = min(
        now_ms + config.book_validity_ms,
        signal_ts_ms + config.signal_freshness_ms,
    )
    return DecisionOutput(
        action=DecisionAction.ENTER,
        reason=entry_reason,
        decision_ts_ms=now_ms,
        symbol=symbol,
        signal_ts_ms=signal_ts_ms,
        entry_reason=entry_reason,
        position_weight=position_weight,
        target_fraction_of_equity=target_fraction,
        target_notional_usdt=target_notional,
        entry_leverage=config.entry_leverage,
        stop_loss_fraction=stop_loss_fraction,
        stop_decay_after_ms=stop_decay_after_ms,
        decayed_stop_loss_fraction=decayed_stop_loss_fraction,
        max_hold_duration_ms=max_hold_duration_ms,
        entry_valid_until_ms=entry_valid_until_ms,
    )


def _decide_existing(
    *,
    decision_input: DecisionInput,
    prior_state: PriorState,
    config: StrategyConfig,
) -> DecisionOutput:
    now_ms = decision_input.decision_ts_ms
    if not prior_state.filled:
        if prior_state.entry_valid_until_ms > 0 and now_ms >= prior_state.entry_valid_until_ms:
            return _output(
                DecisionAction.EXIT,
                "entry_expired",
                decision_input,
                signal_ts_ms=decision_input.signal_ts_ms,
            )
        return DecisionOutput(
            action=DecisionAction.HOLD,
            reason="entry_pending",
            decision_ts_ms=now_ms,
            symbol=decision_input.symbol,
            signal_ts_ms=decision_input.signal_ts_ms,
            target_notional_usdt=prior_state.target_notional_usdt,
            entry_leverage=config.entry_leverage,
            stop_loss_fraction=prior_state.stop_loss_fraction,
            stop_decay_after_ms=prior_state.stop_decay_after_ms,
            decayed_stop_loss_fraction=prior_state.decayed_stop_loss_fraction,
            entry_valid_until_ms=prior_state.entry_valid_until_ms,
        )
    if prior_state.max_hold_deadline_ts_ms > 0 and now_ms >= prior_state.max_hold_deadline_ts_ms:
        return _output(
            DecisionAction.EXIT,
            "time_stop",
            decision_input,
            signal_ts_ms=decision_input.signal_ts_ms,
        )

    stop_fraction = current_stop_loss_fraction(prior_state, now_ms=now_ms)
    observed = [
        value
        for value in (
            _safe_float(decision_input.market_price),
            _safe_float(decision_input.observed_low),
        )
        if value is not None and value > 0.0
    ]
    if (
        prior_state.entry_price > 0.0
        and 0.0 < stop_fraction < 1.0
        and observed
        and min(observed) <= prior_state.entry_price * (1.0 - stop_fraction)
    ):
        reason = "decayed_stop_loss" if stop_fraction < prior_state.stop_loss_fraction else "stop_loss"
        return DecisionOutput(
            action=DecisionAction.EXIT,
            reason=reason,
            decision_ts_ms=now_ms,
            symbol=decision_input.symbol,
            signal_ts_ms=decision_input.signal_ts_ms,
            stop_loss_fraction=stop_fraction,
        )
    return DecisionOutput(
        action=DecisionAction.HOLD,
        reason="held",
        decision_ts_ms=now_ms,
        symbol=decision_input.symbol,
        signal_ts_ms=decision_input.signal_ts_ms,
        target_notional_usdt=prior_state.target_notional_usdt,
        entry_leverage=config.entry_leverage,
        stop_loss_fraction=stop_fraction,
        stop_decay_after_ms=prior_state.stop_decay_after_ms,
        decayed_stop_loss_fraction=prior_state.decayed_stop_loss_fraction,
        entry_valid_until_ms=prior_state.entry_valid_until_ms,
    )


def current_stop_loss_fraction(prior_state: PriorState, *, now_ms: int) -> float:
    """Return the stop distance an existing target must declare now."""

    if not _stop_decay_armed(prior_state, now_ms=now_ms):
        return prior_state.stop_loss_fraction
    return min(
        prior_state.stop_loss_fraction,
        prior_state.decayed_stop_loss_fraction,
    )


def scaled_base_target_fraction(
    config: StrategyConfig,
    *,
    btc_realized_vol: float | None,
) -> tuple[float, float]:
    """Return base equity fraction after the shared BTC-vol scalar."""

    base = (
        config.order_notional_pct_equity
        if config.order_notional_pct_equity > 0.0
        else (config.rule.gross_exposure / max(config.rule.max_concurrent_positions, 1) * config.notional_multiplier)
    )
    scale = _vol_target_scale(config.rule, btc_realized_vol)
    return base * scale, scale


def _stop_decay_armed(prior_state: PriorState, *, now_ms: int) -> bool:
    return (
        prior_state.stop_decay_after_ms > 0
        and 0.0 < prior_state.decayed_stop_loss_fraction < 1.0
        and prior_state.entry_ts_ms > 0
        and prior_state.entry_price > 0.0
        and now_ms >= prior_state.entry_ts_ms + prior_state.stop_decay_after_ms
    )


def _output(
    action: DecisionAction,
    reason: str,
    decision_input: DecisionInput,
    *,
    signal_ts_ms: int = 0,
    wake_at_or_below: float | None = None,
) -> DecisionOutput:
    return DecisionOutput(
        action=action,
        reason=reason,
        decision_ts_ms=decision_input.decision_ts_ms,
        symbol=decision_input.symbol,
        signal_ts_ms=signal_ts_ms,
        wake_at_or_below=wake_at_or_below,
    )


def _vol_parity_weight(
    *,
    realized_vol: float,
    vol_floor: float,
    max_position_weight: float,
    notional_weight: float,
) -> float:
    vol_used = max(realized_vol, vol_floor)
    weight = min(vol_floor / vol_used, max_position_weight / notional_weight)
    return max(weight, 0.25)


def _validate_decision_identity(value: DecisionInput) -> None:
    if value.decision_ts_ms <= 0:
        raise ValueError("decision_ts_ms must be positive")
    if not value.symbol or value.symbol != value.symbol.upper() or not value.symbol.isalnum():
        raise ValueError("symbol must be a plain upper-case venue symbol")
    if not math.isfinite(value.equity_usdt) or value.equity_usdt < 0.0:
        raise ValueError("equity_usdt must be finite and non-negative")


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


def _int_value(value: object) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
