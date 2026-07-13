"""Account-level funding, protection-delay, and liquidation model for the twin."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .account_kernel import AccountEvent, AccountExecutionKernel, AccountState
from .deterministic_runtime import VirtualScheduler
from .strategy_runtime import AdaptedIntent, RiskTargetAdapter, SleeveTargetIntent


@dataclass(frozen=True, slots=True)
class AccountValuation:
    wallet_balance_usdt: float
    unrealized_pnl_usdt: float
    equity_usdt: float
    gross_notional_usdt: float
    maintenance_margin_usdt: float
    liquidation_required: bool


@dataclass(frozen=True, slots=True)
class TwinAccountConfig:
    starting_wallet_balance_usdt: float
    maintenance_margin_rate: float
    liquidation_fee_bps: float
    protection_activation_delay_ns: int

    def __post_init__(self) -> None:
        if self.starting_wallet_balance_usdt <= 0.0:
            raise ValueError("starting wallet balance must be positive")
        if not 0.0 <= self.maintenance_margin_rate < 1.0:
            raise ValueError("maintenance_margin_rate must be in [0, 1)")
        if self.liquidation_fee_bps < 0.0 or self.protection_activation_delay_ns < 0:
            raise ValueError("liquidation fee and protection delay cannot be negative")


class ExecutionTwinAccount:
    """Accounting overlay derived exclusively from reconstructable kernel facts."""

    def __init__(self, config: TwinAccountConfig) -> None:
        self.config = config

    def wallet_balance(self, state: AccountState) -> float:
        realized = math.fsum(position.realized_from_fills_usdt for position in state.positions.values())
        fees = math.fsum(float(execution.get("fee_usdt") or 0.0) for execution in state.executions.values())
        funding = math.fsum(
            float(row.get("funding_usdt") or 0.0)
            for row in state.pnl.values()
            if str(row.get("source") or "") == "execution_twin_funding"
        )
        liquidation_cost = math.fsum(
            float(row.get("net_pnl_usdt") or 0.0)
            for row in state.pnl.values()
            if str(row.get("source") or "") == "execution_twin_liquidation_fee"
        )
        return self.config.starting_wallet_balance_usdt + realized - fees + funding + liquidation_cost

    def value(self, state: AccountState, *, mark_prices: Mapping[str, float]) -> AccountValuation:
        wallet = self.wallet_balance(state)
        unrealized = 0.0
        gross = 0.0
        for symbol, position in state.positions.items():
            mark = float(mark_prices.get(symbol) or 0.0)
            if position.signed_qty == 0.0:
                continue
            if mark <= 0.0 or not math.isfinite(mark):
                raise ValueError(f"missing finite mark price for open position {symbol}")
            unrealized += position.signed_qty * (mark - position.average_price)
            gross += abs(position.signed_qty) * mark
        equity = wallet + unrealized
        maintenance = gross * self.config.maintenance_margin_rate
        return AccountValuation(
            wallet_balance_usdt=wallet,
            unrealized_pnl_usdt=unrealized,
            equity_usdt=equity,
            gross_notional_usdt=gross,
            maintenance_margin_usdt=maintenance,
            liquidation_required=bool(gross > 0.0 and equity <= maintenance),
        )

    def record_funding(
        self,
        kernel: AccountExecutionKernel,
        *,
        funding_key: str,
        funding_rates: Mapping[str, float],
        mark_prices: Mapping[str, float],
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
    ) -> tuple[AccountEvent, ...]:
        state = kernel.state()
        events: list[AccountEvent] = []
        for symbol, position in sorted(state.positions.items()):
            if position.signed_qty == 0.0:
                continue
            rate = float(funding_rates.get(symbol) or 0.0)
            mark = float(mark_prices.get(symbol) or 0.0)
            if not math.isfinite(rate) or mark <= 0.0 or not math.isfinite(mark):
                raise ValueError(f"invalid funding input for {symbol}")
            # Positive rates: longs pay and shorts receive.
            payment = -position.signed_qty * mark * rate
            events.extend(kernel.record_pnl(
                pnl_key=f"{funding_key}:{symbol}",
                close_key="",
                symbol=symbol,
                gross_pnl_usdt=0.0,
                fee_usdt=0.0,
                funding_usdt=payment,
                net_pnl_usdt=payment,
                exchange_ts_ns=exchange_ts_ns,
                local_receive_ts_ns=local_receive_ts_ns,
                source="execution_twin_funding",
                metadata={"funding_rate": rate, "mark_price": mark},
            ))
        return tuple(events)

    def record_liquidation_fee(
        self,
        kernel: AccountExecutionKernel,
        *,
        liquidation_key: str,
        mark_prices: Mapping[str, float],
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
    ) -> tuple[AccountEvent, ...]:
        state = kernel.state()
        notional = math.fsum(
            abs(position.signed_qty) * float(mark_prices.get(symbol) or 0.0)
            for symbol, position in state.positions.items()
        )
        fee = notional * self.config.liquidation_fee_bps / 10_000.0
        return kernel.record_pnl(
            pnl_key=f"{liquidation_key}:fee",
            close_key="",
            symbol="ACCOUNT",
            gross_pnl_usdt=0.0,
            fee_usdt=fee,
            funding_usdt=0.0,
            net_pnl_usdt=-fee,
            exchange_ts_ns=exchange_ts_ns,
            local_receive_ts_ns=local_receive_ts_ns,
            source="execution_twin_liquidation_fee",
            metadata={"liquidated_notional_usdt": notional},
        )


def protection_trigger_reason(
    *,
    signed_qty: float,
    mark_price: float,
    stop_price: float | None,
    take_profit_price: float | None,
) -> str:
    if signed_qty == 0.0:
        return ""
    if stop_price is not None and stop_price > 0.0:
        if (signed_qty > 0.0 and mark_price <= stop_price) or (signed_qty < 0.0 and mark_price >= stop_price):
            return "stop_loss"
    if take_profit_price is not None and take_profit_price > 0.0:
        if (signed_qty > 0.0 and mark_price >= take_profit_price) or (
            signed_qty < 0.0 and mark_price <= take_profit_price
        ):
            return "take_profit"
    return ""


class ProtectionActivationQueue:
    """Virtual-scheduler adapter for delayed venue protection activation."""

    def __init__(self, scheduler: VirtualScheduler, *, delay_ns: int) -> None:
        if delay_ns < 0:
            raise ValueError("protection delay cannot be negative")
        self.scheduler = scheduler
        self.delay_ns = delay_ns

    def request(
        self,
        *,
        protection_key: str,
        symbol: str,
        command_id: str,
        stop_price: float | None,
        take_profit_price: float | None,
    ) -> None:
        self.scheduler.schedule_after(
            self.delay_ns,
            kind="protection_activation",
            task_key=protection_key,
            payload={
                "protection_key": protection_key,
                "symbol": symbol,
                "command_id": command_id,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "activation_wall_ns": self.scheduler.clock.wall_time_ns() + self.delay_ns,
            },
        )

    def activate_due(self, kernel: AccountExecutionKernel) -> tuple[AccountEvent, ...]:
        events: list[AccountEvent] = []
        for task in self.scheduler.pop_due():
            if task.kind != "protection_activation":
                continue
            payload = task.payload
            events.extend(kernel.record_protection(
                protection_key=str(payload["protection_key"]),
                symbol=str(payload["symbol"]),
                status="active",
                stop_price=payload.get("stop_price"),
                take_profit_price=payload.get("take_profit_price"),
                exchange_ts_ns=int(payload["activation_wall_ns"]),
                local_receive_ts_ns=int(payload["activation_wall_ns"]),
                command_id=str(payload["command_id"]),
                metadata={"activation_delay_ns": self.delay_ns},
            ))
        return tuple(events)


def forced_flatten_intents(state: AccountState, *, decision_prefix: str, reason: str) -> tuple[AdaptedIntent, ...]:
    """Replace every active component target with zero through the risk adapter."""

    intents: list[AdaptedIntent] = []
    for target_key, target in sorted(state.component_targets.items()):
        if float(target.get("signed_qty") or 0.0) == 0.0:
            continue
        intents.append(AdaptedIntent(
            adapter=RiskTargetAdapter(),
            intent=SleeveTargetIntent(
                decision_key=f"{decision_prefix}:{target_key}",
                target_key=target_key,
                strategy_id="account-risk",
                component_id=str(target.get("component_id") or "risk"),
                symbol=str(target.get("symbol") or ""),
                signed_notional_usdt=0.0,
                leverage=float(target.get("leverage") or 1.0),
                reason=reason,
                metadata={"owner_sleeve": str(target.get("sleeve") or "")},
            ),
        ))
    return tuple(intents)
