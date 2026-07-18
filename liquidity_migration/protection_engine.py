"""Component protection adapter which emits zero targets, never venue orders."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping, Sequence

from .account_kernel import AccountEvent, AccountExecutionKernel, InstrumentRules, MarketInputRef
from .account_service import (
    AccountIntentInbox,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from .account_strategy_state import (
    CanonicalComponentExecutionAnchor,
    canonical_component_execution_anchors,
)
from .strategy_runtime import SleeveTargetIntent


class AccountProtectionEngine:
    """Translate component stop/TP triggers into durable account target requests."""

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        inbox: AccountIntentInbox,
        instrument_rules: Mapping[str, InstrumentRules],
    ) -> None:
        kernel_root = str(kernel.journal.root.expanduser().resolve(strict=False))
        if kernel.account_id != inbox.route.account_id:
            raise ValueError("protection kernel account_id does not match inbox route")
        if kernel_root != inbox.route.account_root:
            raise ValueError("protection kernel root does not match inbox route")
        rules = {str(symbol).upper(): rule for symbol, rule in instrument_rules.items()}
        if not rules:
            raise ValueError("protection engine requires verified instrument rules")
        for symbol, rule in rules.items():
            if symbol != rule.symbol.upper() or rule.tick_size <= 0.0:
                raise ValueError(
                    f"{symbol} protection rule must match its symbol and have a positive tick_size"
                )
        self.kernel = kernel
        self.inbox = inbox
        self.instrument_rules = rules

    def evaluate(
        self,
        market_inputs: Mapping[str, MarketInputRef],
        *,
        account_events: Sequence[AccountEvent] | None = None,
        verified_execution_anchors: (
            Mapping[str, CanonicalComponentExecutionAnchor] | None
        ) = None,
    ) -> tuple[AccountTargetRequest, ...]:
        requests: list[AccountTargetRequest] = []
        state = self.kernel.state()
        if verified_execution_anchors is None:
            anchors = {
                anchor.target_key: anchor
                for anchor in canonical_component_execution_anchors(
                    self.kernel.journal.root,
                    account_events=account_events,
                )
            }
        else:
            if account_events is None:
                raise ValueError(
                    "verified protection anchors require their account event snapshot"
                )
            anchors = dict(verified_execution_anchors)
            for target_key, projected_anchor in anchors.items():
                if target_key != projected_anchor.target_key:
                    raise ValueError(
                        "verified protection anchor key does not match its projection"
                    )
        for target_key, target in sorted(state.component_targets.items()):
            signed_qty = float(target.get("signed_qty") or 0.0)
            symbol = str(target.get("symbol") or "").upper()
            metadata = target.get("metadata") or {}
            if signed_qty == 0.0 or not symbol or not isinstance(metadata, Mapping):
                continue
            market = market_inputs.get(symbol)
            if market is None:
                continue
            if bool(market.metadata.get("sequence_gap")):
                continue
            anchor = anchors.get(target_key)
            # A strategy TARGET is not a fill. Require the component's entry
            # batch to be completely filled before a software component close
            # can supersede it; otherwise a racing entry remainder could trade
            # after the zero target was published.
            if (
                anchor is None
                or not anchor.entry_fill_complete
                or anchor.entry_fill_vwap is None
                or anchor.entry_fill_vwap <= 0.0
                or anchor.entry_attribution_scope == "none"
            ):
                continue
            rule = self.instrument_rules.get(symbol)
            if rule is None or rule.tick_size <= 0.0:
                continue
            stop_pct = _optional_fraction(metadata.get("stop_loss_pct"))
            take_profit_pct = _optional_fraction(metadata.get("take_profit_pct"))
            stop = _protection_price(
                entry_fill_price=anchor.entry_fill_vwap,
                signed_qty=signed_qty,
                fraction=stop_pct,
                tick_size=rule.tick_size,
                is_stop=True,
            )
            take_profit = _protection_price(
                entry_fill_price=anchor.entry_fill_vwap,
                signed_qty=signed_qty,
                fraction=take_profit_pct,
                tick_size=rule.tick_size,
                is_stop=False,
            )
            reason = _protection_trigger_reason(
                signed_qty=signed_qty,
                mark_price=market.reference_price,
                stop_price=stop,
                take_profit_price=take_profit,
            )
            if not reason:
                continue
            owner = str(target.get("sleeve") or target_key.split("/", 1)[0])
            owner_strategy_id = str(target.get("strategy_id") or "").strip()
            if not owner_strategy_id:
                target_key_parts = target_key.split("/")
                owner_strategy_id = (
                    target_key_parts[1]
                    if len(target_key_parts) == 4 and target_key_parts[1]
                    else "account-protection"
                )
            source_decision = str(target.get("decision_key") or target_key)
            request_id = f"protection:{source_decision}:{reason}"
            if self.inbox.contains(request_id):
                continue
            request = AccountTargetRequest(
                request_id=request_id,
                batch_id=request_id,
                # Inbox scheduling is based on its own durable arrival sequence,
                # while the target revision records actual local request
                # creation. Never reuse the older exchange tick as the
                # revision of this safety-flat decision.
                created_ts_ns=max(
                    self.kernel.clock.wall_time_ns(),
                    market.local_receive_ts_ns,
                    1,
                ),
                route_id=self.inbox.route.route_id,
                account_id=self.inbox.route.account_id,
                environment=self.inbox.route.environment,
                intents=(RequestedIntent(
                    adapter_kind=SleeveAdapterKind.RISK,
                    intent=SleeveTargetIntent(
                        decision_key=f"risk:{source_decision}:{reason}",
                        target_key=target_key,
                        # The zero target is a risk-authored decision, but it is
                        # still a lifecycle replacement for the original
                        # strategy component. Keeping the owner's strategy id
                        # lets canonical sleeve projections apply the close to
                        # the row they opened instead of manufacturing a second
                        # account-protection lifecycle.
                        strategy_id=owner_strategy_id,
                        component_id=str(target.get("component_id") or "risk"),
                        symbol=symbol,
                        signed_notional_usdt=0.0,
                        leverage=float(target.get("leverage") or 1.0),
                        reason=reason,
                        metadata={
                            "owner_sleeve": owner,
                            "requested_by_strategy_id": "account-protection",
                            "trigger_market_input_key": market.input_key,
                            "trigger_exchange_ts_ns": market.exchange_ts_ns,
                            "trigger_local_receive_ts_ns": market.local_receive_ts_ns,
                            "trigger_price": market.reference_price,
                            "entry_fill_execution_id": anchor.entry_fill_execution_id,
                            "entry_fill_vwap": anchor.entry_fill_vwap,
                            "entry_attribution_scope": anchor.entry_attribution_scope,
                            "entry_attribution_basis": anchor.entry_attribution_basis,
                            "stop_loss_pct": stop_pct,
                            "take_profit_pct": take_profit_pct,
                            "stop_price": stop,
                            "take_profit_price": take_profit,
                        },
                    ),
                ),),
            )
            self.inbox.submit(request)
            self.kernel.record_protection(
                protection_key=request_id,
                symbol=symbol,
                status="triggered",
                stop_price=stop,
                take_profit_price=take_profit,
                exchange_ts_ns=market.exchange_ts_ns,
                local_receive_ts_ns=market.local_receive_ts_ns,
                metadata={
                    "target_key": target_key,
                    "reason": reason,
                    "trigger_market_input_key": market.input_key,
                    "trigger_exchange_ts_ns": market.exchange_ts_ns,
                    "trigger_local_receive_ts_ns": market.local_receive_ts_ns,
                    "trigger_price": market.reference_price,
                    "entry_fill_execution_id": anchor.entry_fill_execution_id,
                    "entry_fill_vwap": anchor.entry_fill_vwap,
                    "entry_attribution_scope": anchor.entry_attribution_scope,
                    "entry_attribution_basis": anchor.entry_attribution_basis,
                    "stop_loss_pct": stop_pct,
                    "take_profit_pct": take_profit_pct,
                },
            )
            requests.append(request)
        return tuple(requests)


def _protection_trigger_reason(
    *,
    signed_qty: float,
    mark_price: float,
    stop_price: float | None,
    take_profit_price: float | None,
) -> str:
    if signed_qty == 0.0:
        return ""
    if stop_price is not None and stop_price > 0.0 and (
        (signed_qty > 0.0 and mark_price <= stop_price)
        or (signed_qty < 0.0 and mark_price >= stop_price)
    ):
        return "stop_loss"
    if take_profit_price is not None and take_profit_price > 0.0 and (
        (signed_qty > 0.0 and mark_price >= take_profit_price)
        or (signed_qty < 0.0 and mark_price <= take_profit_price)
    ):
        return "take_profit"
    return ""


def _optional_fraction(value: object) -> float | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if 0.0 < output < 1.0 else None


def _protection_price(
    *,
    entry_fill_price: float,
    signed_qty: float,
    fraction: float | None,
    tick_size: float,
    is_stop: bool,
) -> float | None:
    """Derive the executable trigger grid from fill price, never decision price."""

    if fraction is None or signed_qty == 0.0:
        return None
    entry = Decimal(str(entry_fill_price))
    pct = Decimal(str(fraction))
    tick = Decimal(str(tick_size))
    if signed_qty > 0.0:
        raw = entry * (Decimal("1") - pct if is_stop else Decimal("1") + pct)
        rounding = ROUND_FLOOR if is_stop else ROUND_CEILING
    else:
        raw = entry * (Decimal("1") + pct if is_stop else Decimal("1") - pct)
        rounding = ROUND_CEILING if is_stop else ROUND_FLOOR
    units = raw / tick
    return float(units.to_integral_value(rounding=rounding) * tick)
