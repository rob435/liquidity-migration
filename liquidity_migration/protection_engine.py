"""Component protection adapter which emits zero targets, never venue orders."""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .account_contracts import (
    AccountEvent,
    AccountState,
    InstrumentRules,
    MarketInputRef,
)
from .account_kernel import (
    AccountExecutionKernel,
    reduce_account_events,
)
from .account_service import (
    AccountIntentInbox,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from .account_strategy_state import (
    CanonicalComponentExecutionAnchor,
    component_execution_anchors_from_snapshot,
)
from .deterministic_serialization import canonical_json
from .strategy_runtime import SleeveTargetIntent

if TYPE_CHECKING:
    from .venue_protection import NativeProtectionBreach


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
                raise ValueError(f"{symbol} protection rule must match its symbol and have a positive tick_size")
        self.kernel = kernel
        self.inbox = inbox
        self.instrument_rules = rules

    def evaluate(
        self,
        market_inputs: Mapping[str, MarketInputRef],
        *,
        account_events: Sequence[AccountEvent] | None = None,
        verified_execution_anchors: (Mapping[str, CanonicalComponentExecutionAnchor] | None) = None,
        trusted_account_state: AccountState | None = None,
    ) -> tuple[AccountTargetRequest, ...]:
        requests: list[AccountTargetRequest] = []
        if account_events is None:
            if trusted_account_state is not None:
                raise ValueError("trusted protection state requires its account event snapshot")
            if verified_execution_anchors is not None:
                raise ValueError("verified protection anchors require their account event snapshot")
            events, state = self.kernel._snapshot_ref()
        else:
            events = tuple(account_events)
            expected_state_hash = events[-1].state_hash if events else AccountState().rolling_state_hash
            if trusted_account_state is None:
                state = reduce_account_events(events)
            else:
                if (
                    trusted_account_state.events_applied != len(events)
                    or trusted_account_state.rolling_state_hash != expected_state_hash
                ):
                    raise RuntimeError("trusted protection state does not match its account event snapshot")
                state = trusted_account_state
        if verified_execution_anchors is None:
            anchors = {
                anchor.target_key: anchor
                for anchor in component_execution_anchors_from_snapshot(
                    events,
                    state=state,
                )
            }
        else:
            anchors = dict(verified_execution_anchors)
            for target_key, projected_anchor in anchors.items():
                if target_key != projected_anchor.target_key:
                    raise ValueError("verified protection anchor key does not match its projection")
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
            # commands to be beyond racing before a software component close
            # can supersede them: either completely filled, or terminal with a
            # real observed fill (a partially-filled-then-cancelled entry holds
            # a live position that must not lose stop/TP evaluation forever —
            # no working remainder can trade after a terminal status).
            if (
                anchor is None
                or not (
                    anchor.entry_fill_complete
                    or (anchor.entry_commands_terminal and abs(anchor.entry_observed_signed_qty) > 0.0)
                )
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
                    target_key_parts[1] if len(target_key_parts) == 4 and target_key_parts[1] else "account-protection"
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
                intents=(
                    RequestedIntent(
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
                    ),
                ),
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

    def evaluate_native_breaches(
        self,
        breaches: Sequence[NativeProtectionBreach],
    ) -> tuple[AccountTargetRequest, ...]:
        """Publish atomic software flats for authenticated native-stop breaches.

        Include both accepted components and not-yet-processed nonzero intents
        for the breached symbol. The latter is essential: a queued newer
        component entry must receive a still-newer zero revision or it could
        reopen the symbol after the emergency close.
        """

        published: list[AccountTargetRequest] = []
        state = self.kernel._state_ref()
        unresolved = self.inbox.unresolved_requests()
        for breach in sorted(breaches, key=lambda item: item.plan.symbol):
            symbol = breach.plan.symbol.upper()
            candidates: dict[str, dict[str, Any]] = {}
            covered_revisions: dict[str, int] = {}

            for target_key, target in state.component_target_desires.items():
                if str(target.get("symbol") or "").upper() != symbol:
                    continue
                metadata = target.get("metadata") or {}
                revision = max(
                    int(metadata.get("account_request_created_ts_ns") or 0),
                    breach.observed_ts_ns,
                    1,
                )
                if float(target.get("signed_qty") or 0.0) == 0.0:
                    if str(metadata.get("native_protection_plan_key") or "") == breach.plan.protection_key:
                        covered_revisions[target_key] = max(
                            int(metadata.get("source_revision_ns") or revision),
                            covered_revisions.get(target_key, 0),
                        )
                    continue
                candidates[target_key] = {
                    "target_key": target_key,
                    "strategy_id": str(target.get("strategy_id") or "account-protection"),
                    "component_id": str(target.get("component_id") or "native-disaster"),
                    "owner_sleeve": str(target.get("sleeve") or target_key.split("/", 1)[0]),
                    "leverage": float(target.get("leverage") or 1.0),
                    "source_decision_key": str(target.get("decision_key") or target_key),
                    "source_request_id": str(metadata.get("account_request_id") or ""),
                    "source_revision_ns": revision,
                }

            for pending in unresolved:
                for item in pending.intents:
                    intent = item.intent
                    if (
                        intent.symbol.upper() != symbol
                        or float(intent.signed_notional_usdt) == 0.0
                        or pending.created_ts_ns <= covered_revisions.get(intent.target_key, 0)
                    ):
                        continue
                    prior = candidates.get(intent.target_key)
                    prior_revision = int(prior.get("source_revision_ns") or 0) if prior else -1
                    identity = (pending.created_ts_ns, pending.request_id)
                    prior_identity = (
                        prior_revision,
                        str(prior.get("source_request_id") or "") if prior else "",
                    )
                    if prior is not None and identity <= prior_identity:
                        continue
                    candidates[intent.target_key] = {
                        "target_key": intent.target_key,
                        "strategy_id": intent.strategy_id,
                        "component_id": intent.component_id,
                        "owner_sleeve": str(intent.metadata.get("owner_sleeve") or intent.target_key.split("/", 1)[0]),
                        "leverage": float(intent.leverage),
                        "source_decision_key": intent.decision_key,
                        "source_request_id": pending.request_id,
                        "source_revision_ns": pending.created_ts_ns,
                    }

            canonical_flat_in_flight = abs(float(state.aggregate_targets.get(symbol, 0.0))) <= 1e-12 and any(
                state.orders[command_id].symbol == symbol and state.orders[command_id].reduce_only
                for command_id in state.working_order_ids
            )
            if not candidates and (covered_revisions or canonical_flat_in_flight):
                continue
            if not candidates:
                synthetic_key = f"account_risk/native-disaster/{symbol}"
                candidates[synthetic_key] = {
                    "target_key": synthetic_key,
                    "strategy_id": "account-protection",
                    "component_id": "native-disaster",
                    "owner_sleeve": "account_risk",
                    "leverage": 1.0,
                    "source_decision_key": breach.plan.protection_key,
                    "source_request_id": "",
                    "source_revision_ns": breach.observed_ts_ns,
                }

            ordered = [candidates[key] for key in sorted(candidates)]
            material = {
                "symbol": symbol,
                "protection_plan_key": breach.plan.protection_key,
                "targets": [
                    {
                        "target_key": row["target_key"],
                        "source_request_id": row["source_request_id"],
                        "source_revision_ns": row["source_revision_ns"],
                    }
                    for row in ordered
                ],
            }
            suffix = hashlib.sha256(canonical_json(material)).hexdigest()[:20]
            request_id = f"protection:native-breach:{symbol}:{suffix}"
            created_ts_ns = max(
                breach.observed_ts_ns,
                *(int(row["source_revision_ns"]) for row in ordered),
                1,
            )
            request = AccountTargetRequest(
                request_id=request_id,
                batch_id=request_id,
                created_ts_ns=created_ts_ns,
                route_id=self.inbox.route.route_id,
                account_id=self.inbox.route.account_id,
                environment=self.inbox.route.environment,
                intents=tuple(
                    RequestedIntent(
                        adapter_kind=SleeveAdapterKind.RISK,
                        intent=SleeveTargetIntent(
                            decision_key=f"risk:{request_id}:{row['target_key']}",
                            target_key=str(row["target_key"]),
                            strategy_id=str(row["strategy_id"]),
                            component_id=str(row["component_id"]),
                            symbol=symbol,
                            signed_notional_usdt=0.0,
                            leverage=float(row["leverage"]),
                            reason="native_disaster_stop_breached",
                            metadata={
                                "owner_sleeve": str(row["owner_sleeve"]),
                                "requested_by_strategy_id": "account-protection",
                                "native_protection_plan_key": breach.plan.protection_key,
                                "native_stop_price": breach.plan.stop_price,
                                "breached_signed_qty": breach.plan.signed_qty,
                                "authenticated_breach_mark": breach.observed_mark,
                                "breach_evidence_source": breach.evidence_source,
                                "breach_observed_ts_ns": breach.observed_ts_ns,
                                "source_request_id": str(row["source_request_id"]),
                                "source_revision_ns": int(row["source_revision_ns"]),
                            },
                        ),
                    )
                    for row in ordered
                ),
            )
            existed = self.inbox.contains(request_id)
            if not existed:
                self.inbox.submit(request)
                published.append(request)
            if request_id not in self.kernel._state_ref().protections:
                request_hash = request.content_hash()
                self.kernel.record_protection(
                    protection_key=request_id,
                    symbol=symbol,
                    status="software_flat_requested",
                    stop_price=breach.plan.stop_price,
                    take_profit_price=None,
                    exchange_ts_ns=0,
                    local_receive_ts_ns=breach.observed_ts_ns,
                    metadata={
                        "native_exchange": False,
                        "native_protection_plan_key": breach.plan.protection_key,
                        "reason": "native_disaster_stop_breached",
                        "request_hash": request_hash,
                        "authenticated_breach_mark": breach.observed_mark,
                        "breach_evidence_source": breach.evidence_source,
                        "breach_observed_ts_ns": breach.observed_ts_ns,
                        "breached_signed_qty": breach.plan.signed_qty,
                        "native_stop_price": breach.plan.stop_price,
                        "target_keys": [str(row["target_key"]) for row in ordered],
                        "source_revision_by_target": {
                            str(row["target_key"]): int(row["source_revision_ns"])
                            for row in ordered
                        },
                    },
                )
            else:
                authorization = self.kernel._state_ref().protections[request_id]
                authorization_metadata = authorization.get("metadata") or {}
                if (
                    str(authorization.get("status") or "") != "software_flat_requested"
                    or not isinstance(authorization_metadata, Mapping)
                    or str(authorization_metadata.get("request_hash") or "")
                    != request.content_hash()
                ):
                    raise RuntimeError(
                        f"native breach recovery authorization {request_id!r} changed content"
                    )
        return tuple(published)


def _protection_trigger_reason(
    *,
    signed_qty: float,
    mark_price: float,
    stop_price: float | None,
    take_profit_price: float | None,
) -> str:
    if signed_qty == 0.0:
        return ""
    if (
        stop_price is not None
        and stop_price > 0.0
        and ((signed_qty > 0.0 and mark_price <= stop_price) or (signed_qty < 0.0 and mark_price >= stop_price))
    ):
        return "stop_loss"
    if (
        take_profit_price is not None
        and take_profit_price > 0.0
        and (
            (signed_qty > 0.0 and mark_price >= take_profit_price)
            or (signed_qty < 0.0 and mark_price <= take_profit_price)
        )
    ):
        return "take_profit"
    return ""


def _optional_fraction(value: object) -> float | None:
    """Absent means no configured protection; present-but-invalid fails closed.

    Silently mapping an out-of-range value (e.g. ``8`` meaning 8 percent) to
    None disabled the intended component stop/TP with no operator-visible
    signal anywhere in the chain. The raise surfaces through protection
    evaluation as blocked owner health, never as a crash.
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"protection fraction has unsupported type {type(value).__name__}")
    if isinstance(value, str) and not value.strip():
        return None
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"protection fraction is not numeric: {value!r}") from exc
    if not math.isfinite(output) or not 0.0 < output < 1.0:
        raise ValueError(f"protection fraction must be a fraction in (0, 1), got {value!r}")
    return output


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
