"""Ports/adapters runtime joining strategy intents to the account kernel.

Sleeve code is allowed to compute signals and explicit desired notionals.  It is
not allowed to call a venue client, mutate a ledger, or reserve account margin.
This module converts all sleeve intents together, then submits one atomic batch
to :class:`AccountExecutionKernel`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping, Protocol, Sequence

from liquidity_migration.account.account_contracts import (
    AccountEvent,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
    TargetBatchResult,
)
from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.account.execution_adapters import KernelExecutionDriver


@dataclass(frozen=True, slots=True)
class SleeveTargetIntent:
    decision_key: str
    target_key: str
    strategy_id: str
    component_id: str
    symbol: str
    signed_notional_usdt: float
    leverage: float
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class TargetAdapter(Protocol):
    @property
    def sleeve(self) -> str: ...

    def desired_target(
        self,
        intent: SleeveTargetIntent,
        market: MarketInputRef,
        instrument_rules: InstrumentRules,
    ) -> DesiredTarget: ...


@dataclass(frozen=True, slots=True)
class _SignedNotionalAdapter:
    sleeve: str
    allowed_sign: int | None

    def desired_target(
        self,
        intent: SleeveTargetIntent,
        market: MarketInputRef,
        instrument_rules: InstrumentRules,
    ) -> DesiredTarget:
        notional = float(intent.signed_notional_usdt)
        leverage = float(intent.leverage)
        price = float(market.reference_price)
        if not all(math.isfinite(value) for value in (notional, leverage, price)):
            raise ValueError("target notional, leverage, and market price must be finite")
        if leverage <= 0.0 or price <= 0.0:
            raise ValueError("target leverage and market price must be positive")
        if self.allowed_sign == 1 and notional < 0.0:
            raise ValueError(f"{self.sleeve} adapter refuses a short target")
        if self.allowed_sign == -1 and notional > 0.0:
            raise ValueError(f"{self.sleeve} adapter refuses a long target")
        raw_qty = notional / price
        step = float(instrument_rules.qty_step)
        if step <= 0.0 or not math.isfinite(step):
            raise ValueError("instrument qty_step must be finite and positive")
        units = (Decimal(str(abs(raw_qty))) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)
        signed_qty = math.copysign(float(units * Decimal(str(step))), raw_qty) if raw_qty else 0.0
        # Preserve a sub-step nonzero intent so the account risk decision emits
        # an explicit qty-step rejection instead of silently accepting zero.
        if raw_qty and signed_qty == 0.0:
            signed_qty = raw_qty
        return DesiredTarget(
            decision_key=intent.decision_key,
            target_key=intent.target_key,
            sleeve=self.sleeve,
            strategy_id=intent.strategy_id,
            component_id=intent.component_id,
            symbol=intent.symbol.upper(),
            signed_qty=signed_qty,
            reference_price=price,
            leverage=leverage,
            reason=intent.reason,
            metadata={
                **dict(intent.metadata),
                "signed_notional_usdt": notional,
                "raw_signed_qty": raw_qty,
                "quantity_rounding": "toward_zero_to_venue_step",
                "market_input_key": market.input_key,
            },
        )


class LongTargetAdapter(_SignedNotionalAdapter):
    def __init__(self) -> None:
        super().__init__(sleeve="long", allowed_sign=1)


class ContinuousTargetAdapter(_SignedNotionalAdapter):
    def __init__(self) -> None:
        super().__init__(sleeve="continuous", allowed_sign=-1)


class CarryTargetAdapter(_SignedNotionalAdapter):
    """Long-only funding-carry sleeve (registered config lane2_carry_hold_v3)."""

    def __init__(self) -> None:
        super().__init__(sleeve="carry", allowed_sign=1)


class HedgeTargetAdapter(_SignedNotionalAdapter):
    def __init__(self) -> None:
        super().__init__(sleeve="hedge", allowed_sign=None)


class RiskTargetAdapter(_SignedNotionalAdapter):
    """Risk exits express a replacement target, commonly zero; never an order side."""

    def __init__(self) -> None:
        super().__init__(sleeve="risk", allowed_sign=None)

    def desired_target(
        self,
        intent: SleeveTargetIntent,
        market: MarketInputRef,
        instrument_rules: InstrumentRules,
    ) -> DesiredTarget:
        proposed = super().desired_target(intent, market, instrument_rules)
        owner = str(intent.metadata.get("owner_sleeve") or intent.target_key.split("/", 1)[0]).strip()
        if not owner or owner == "risk":
            raise ValueError("risk target requires the owning sleeve in target_key or owner_sleeve")
        return replace(
            proposed,
            sleeve=owner,
            metadata={**dict(proposed.metadata), "requested_by": "account_risk"},
        )


@dataclass(frozen=True, slots=True)
class AdaptedIntent:
    adapter: TargetAdapter
    intent: SleeveTargetIntent


@dataclass(frozen=True, slots=True)
class AccountCycleResult:
    target_result: TargetBatchResult
    execution_events: tuple[AccountEvent, ...]


class AccountKernelRuntime:
    """The only operational path shared by historical, paper, and demo."""

    def __init__(self, kernel: AccountExecutionKernel) -> None:
        self.kernel = kernel
        self.driver = KernelExecutionDriver(kernel)

    def process_cycle(
        self,
        *,
        batch_id: str,
        intents: Sequence[AdaptedIntent],
        market_inputs: Mapping[str, MarketInputRef],
        risk_snapshot: AccountRiskSnapshot,
        risk_policy: AccountRiskPolicy,
        instrument_rules: Mapping[str, InstrumentRules],
        execution_adapter: Any | None,
        native_protection_policy: NativeDisasterProtectionPolicy | None = None,
        command_symbols: set[str] | frozenset[str] | None = None,
        require_strict_risk_reduction: bool = False,
        request_content_hash: str | None = None,
    ) -> AccountCycleResult:
        targets: list[DesiredTarget] = []
        for adapted in intents:
            symbol = adapted.intent.symbol.upper()
            market = market_inputs.get(symbol)
            if market is None:
                raise ValueError(f"missing market input for {symbol}")
            rules = instrument_rules.get(symbol)
            if rules is None:
                raise ValueError(f"missing instrument rules for {symbol}")
            targets.append(adapted.adapter.desired_target(adapted.intent, market, rules))
        result = self.kernel.submit_targets(
            batch_id=batch_id,
            market_inputs=tuple(market_inputs.values()),
            targets=targets,
            risk_snapshot=risk_snapshot,
            risk_policy=risk_policy,
            instrument_rules=instrument_rules,
            native_protection_policy=native_protection_policy,
            command_symbols=command_symbols,
            require_strict_risk_reduction=require_strict_risk_reduction,
            request_content_hash=request_content_hash,
        )
        execution_events: tuple[AccountEvent, ...] = ()
        if result.accepted and result.commands and execution_adapter is not None:
            execution_events = self.driver.execute_batch(
                result,
                market_inputs=market_inputs,
                adapter=execution_adapter,
            )
        return AccountCycleResult(target_result=result, execution_events=execution_events)
