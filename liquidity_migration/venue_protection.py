"""Account-owned exchange-native disaster stops and external-fill adoption."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
from functools import wraps
from threading import RLock
from typing import Any, Concatenate, Mapping, ParamSpec, Sequence, TypeVar

from .account_kernel import (
    AccountExecutionKernel,
    AccountState,
    AccountTransitionError,
    InstrumentRules,
    PositionState,
)
from .account_strategy_state import canonical_component_execution_anchors
from .deterministic_serialization import canonical_json
from .deterministic_runtime import Clock, SystemClock


_LockOwner = TypeVar("_LockOwner")
_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")


def _serialized_manager_method(
    method: Callable[Concatenate[_LockOwner, _Params], _Result],
) -> Callable[Concatenate[_LockOwner, _Params], _Result]:
    """Hold one reentrant manager lock across a complete public operation."""

    @wraps(method)
    def wrapped(
        self: _LockOwner,
        /,
        *args: _Params.args,
        **kwargs: _Params.kwargs,
    ) -> _Result:
        with getattr(self, "_lock"):
            return method(self, *args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class NativeProtectionPlan:
    protection_key: str
    symbol: str
    signed_qty: float
    stop_price: float
    stop_source: str
    target_keys: tuple[str, ...]


class AccountHealthChain:
    """Require every account-owner health check to pass."""

    def __init__(self, providers: Sequence[Any]) -> None:
        self.providers = tuple(providers)

    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        for provider in self.providers:
            provider.require_recent_healthy(max_age_ns=max_age_ns)


class BybitNativeProtectionManager:
    """Install one full-position disaster stop per reconstructed net symbol.

    Component stop/TP decisions remain in the account protection engine.  This
    native stop is the process-death seatbelt.  Bybit owns the resulting order,
    so its executions are adopted back through the kernel's external protection
    transaction instead of being mistaken for ledger drift.
    """

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        client: Any,
        instrument_rules: Mapping[str, InstrumentRules],
        fallback_stop_fraction: float,
        clock: Clock | None = None,
    ) -> None:
        if not bool(getattr(client, "demo", False)):
            raise ValueError("native protection manager refuses a non-demo client")
        fraction = float(fallback_stop_fraction)
        if not math.isfinite(fraction) or not 0.0 < fraction < 1.0:
            raise ValueError("fallback_stop_fraction must be explicitly set in (0, 1)")
        self.kernel = kernel
        self.client = client
        self.rules = {symbol.upper(): rule for symbol, rule in instrument_rules.items()}
        self.fallback_stop_fraction = fraction
        self.clock = clock or SystemClock()
        # Private WS callbacks and the owner/reconciliation loop share this
        # manager. Hold one lock across planning, the Bybit mutation, journal
        # activation, and every adoption/observation transition so callers can
        # never install or bind two revisions from the same pre-mutation state.
        # Public methods call one another, hence a reentrant lock is required.
        self._lock = RLock()
        self.last_sync_ns = 0
        self.last_sync_ns_by_symbol: dict[str, int] = {}
        self.last_error = ""
        self.observed_native_order_ids: dict[str, str] = {}

    @_serialized_manager_method
    def plan(self, symbol: str) -> NativeProtectionPlan | None:
        symbol = symbol.upper()
        state = self.kernel.state()
        position = state.positions.get(symbol)
        if position is None or position.signed_qty == 0.0:
            return None
        rule = self.rules.get(symbol)
        if rule is None or rule.tick_size <= 0.0:
            raise RuntimeError(f"{symbol} lacks a verified positive demo tick_size")
        targets = [
            (key, target)
            for key, target in state.component_targets.items()
            if str(target.get("symbol") or "").upper() == symbol
            and float(target.get("signed_qty") or 0.0) * position.signed_qty > 0.0
        ]
        if not targets:
            retained = self._plan_for_canonical_close(
                symbol,
                state=state,
                position=position,
            )
            if retained is not None:
                return retained
            raise RuntimeError(f"{symbol} position has no same-direction component target owner")
        anchors = {
            anchor.target_key: anchor
            for anchor in canonical_component_execution_anchors(
                self.kernel.journal.root,
            )
        }
        explicit = []
        for target_key, target in targets:
            metadata = target.get("metadata") or {}
            if not isinstance(metadata, Mapping):
                continue
            anchor = anchors.get(target_key)
            if (
                anchor is None
                or anchor.entry_fill_vwap is None
                or anchor.entry_fill_vwap <= 0.0
                or anchor.entry_attribution_scope == "none"
            ):
                continue
            try:
                stop_fraction = float(metadata.get("stop_loss_pct") or 0.0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(stop_fraction) or not 0.0 < stop_fraction < 1.0:
                continue
            fill_price = float(anchor.entry_fill_vwap)
            explicit.append(
                fill_price
                * (
                    1.0 - stop_fraction
                    if position.signed_qty > 0.0
                    else 1.0 + stop_fraction
                )
            )
        if explicit:
            # This is a Full-position process-death seatbelt, not a component
            # exit. It must sit outside every software component stop or the
            # first ordinary component trigger would flatten all same-symbol
            # owners at the venue. Component-level stop/TP decisions remain
            # target replacements in AccountProtectionEngine.
            raw_stop = min(explicit) if position.signed_qty > 0.0 else max(explicit)
            source = "fill_anchored_outermost_component_stop"
        else:
            reference = position.average_price
            if reference <= 0.0:
                market = state.latest_market_inputs.get(symbol) or {}
                reference = float(market.get("reference_price") or 0.0)
            if reference <= 0.0:
                raise RuntimeError(f"{symbol} cannot derive disaster stop without a reference price")
            raw_stop = reference * (
                1.0 - self.fallback_stop_fraction
                if position.signed_qty > 0.0
                else 1.0 + self.fallback_stop_fraction
            )
            source = "explicit_account_fallback_fraction"
        stop = _round_stop(raw_stop, rule.tick_size, long_position=position.signed_qty > 0.0)
        market = state.latest_market_inputs.get(symbol) or {}
        mark = float(market.get("reference_price") or position.average_price or 0.0)
        if mark <= 0.0:
            raise RuntimeError(f"{symbol} cannot validate native stop without a mark")
        if (position.signed_qty > 0.0 and stop >= mark) or (
            position.signed_qty < 0.0 and stop <= mark
        ):
            raise RuntimeError(
                f"{symbol} native stop {stop:g} is already crossed at mark {mark:g}"
            )
        target_keys = tuple(sorted(key for key, _target in targets))
        material = {
            "symbol": symbol,
            "signed_qty": position.signed_qty,
            "stop_price": stop,
            "stop_source": source,
            "target_keys": target_keys,
        }
        protection_key = (
            f"native-disaster:{symbol}:"
            + hashlib.sha256(canonical_json(material)).hexdigest()[:20]
        )
        return NativeProtectionPlan(
            protection_key=protection_key,
            symbol=symbol,
            signed_qty=position.signed_qty,
            stop_price=stop,
            stop_source=source,
            target_keys=target_keys,
        )

    def _plan_for_canonical_close(
        self,
        symbol: str,
        *,
        state: AccountState,
        position: PositionState,
    ) -> NativeProtectionPlan | None:
        """Retain an installed stop across the journal/venue close transition.

        An accepted target-flat replacement removes the component owner before
        its reduce-only market order can update the reconstructed position. The
        position is not orphaned during that bounded interval, but only when the
        journal proves every remaining unit is covered by canonical reduce-only
        work. Never derive a new stop here: absence of a previously installed,
        active stop remains a hard failure.
        """

        tolerance = max(abs(position.signed_qty) * 1e-12, 1e-12)
        if symbol not in state.aggregate_targets:
            return None
        if abs(float(state.aggregate_targets[symbol])) > tolerance:
            return None

        symbol_desires = [
            target
            for target in state.component_target_desires.values()
            if str(target.get("symbol") or "").upper() == symbol
        ]
        if not symbol_desires or any(
            abs(float(target.get("signed_qty") or 0.0)) > tolerance
            for target in symbol_desires
        ):
            return None
        if any(
            str(target.get("symbol") or "").upper() == symbol
            and abs(float(target.get("signed_qty") or 0.0)) > tolerance
            for target in state.component_targets.values()
        ):
            return None

        working_orders = [
            state.orders[command_id]
            for command_id in state.working_order_ids
            if state.orders[command_id].symbol == symbol
            and abs(state.orders[command_id].remaining_signed_qty) > tolerance
        ]
        if not working_orders:
            return None
        if any(
            not order.reduce_only
            or order.remaining_signed_qty * position.signed_qty >= 0.0
            for order in working_orders
        ):
            return None
        projected_qty = math.fsum(
            [position.signed_qty]
            + [order.remaining_signed_qty for order in working_orders]
        )
        if abs(projected_qty) > tolerance:
            return None

        active = self._active_from_state(state, symbol)
        if active is None or str(active[1].get("status") or "") != "active":
            return None
        metadata = active[1].get("metadata") or {}
        if not isinstance(metadata, Mapping):
            return None
        active_qty = _finite_or_zero(metadata.get("signed_qty"))
        if active_qty * position.signed_qty <= 0.0:
            return None
        stop = _optional_float(active[1].get("stop_price"))
        if stop is None:
            return None
        market = state.latest_market_inputs.get(symbol) or {}
        mark = float(market.get("reference_price") or position.average_price or 0.0)
        if mark <= 0.0:
            return None
        if (position.signed_qty > 0.0 and stop >= mark) or (
            position.signed_qty < 0.0 and stop <= mark
        ):
            return None
        protection_key = self._plan_key(active)
        if not protection_key:
            return None
        return NativeProtectionPlan(
            protection_key=protection_key,
            symbol=symbol,
            signed_qty=position.signed_qty,
            stop_price=stop,
            stop_source=str(metadata.get("stop_source") or "retained_canonical_close"),
            target_keys=tuple(
                sorted(str(key) for key in (metadata.get("target_keys") or ()) if str(key))
            ),
        )

    @_serialized_manager_method
    def active(self, symbol: str) -> tuple[str, Mapping[str, Any]] | None:
        symbol = symbol.upper()
        return self._active_from_state(self.kernel.state(), symbol)

    @staticmethod
    def _active_from_state(
        state: AccountState,
        symbol: str,
    ) -> tuple[str, Mapping[str, Any]] | None:
        matches = [
            (key, protection)
            for key, protection in state.protections.items()
            if str(protection.get("status") or "") in {"active", "triggering"}
            and bool((protection.get("metadata") or {}).get("native_exchange"))
            and str((protection.get("metadata") or {}).get("symbol") or symbol).upper() == symbol
        ]
        return matches[-1] if matches else None

    @staticmethod
    def _plan_key(active: tuple[str, Mapping[str, Any]]) -> str:
        return str(
            (active[1].get("metadata") or {}).get("protection_plan_key")
            or active[0]
        )

    @_serialized_manager_method
    def _next_activation(self, plan: NativeProtectionPlan) -> tuple[str, int]:
        revisions = [
            int((protection.get("metadata") or {}).get("activation_revision") or 1)
            for key, protection in self.kernel.state().protections.items()
            if key == plan.protection_key
            or str((protection.get("metadata") or {}).get("protection_plan_key") or "")
            == plan.protection_key
        ]
        revision = max(revisions, default=0) + 1
        key = (
            plan.protection_key
            if revision == 1
            else f"{plan.protection_key}:activation:{revision:04d}"
        )
        return key, revision

    @_serialized_manager_method
    def sync(self, symbol: str, *, force: bool = False) -> NativeProtectionPlan | None:
        symbol = symbol.upper()
        plan = self.plan(symbol)
        if plan is None:
            active = self.active(symbol)
            observed_ns = self.clock.wall_time_ns()
            if active is not None:
                prior = active[1]
                self.kernel.record_protection(
                    protection_key=active[0],
                    symbol=symbol,
                    status="position_flat",
                    stop_price=_optional_float(prior.get("stop_price")),
                    take_profit_price=_optional_float(prior.get("take_profit_price")),
                    exchange_ts_ns=0,
                    local_receive_ts_ns=observed_ns,
                    metadata=dict(prior.get("metadata") or {}),
                )
            self.observed_native_order_ids.pop(symbol, None)
            self.last_sync_ns = observed_ns
            self.last_sync_ns_by_symbol[symbol] = observed_ns
            self.last_error = ""
            return None
        active = self.active(symbol)
        if active is not None and self._plan_key(active) == plan.protection_key and not force:
            self.last_sync_ns = self.clock.wall_time_ns()
            self.last_sync_ns_by_symbol[symbol] = self.last_sync_ns
            self.last_error = ""
            return plan
        try:
            self.client.set_trading_stop(
                symbol=symbol,
                tpsl_mode="Full",
                position_idx=0,
                stop_loss=_decimal_text(plan.stop_price),
                take_profit="0",
                sl_trigger_by="MarkPrice",
                tp_trigger_by=None,
            )
            superseded_venue_order_ids = {
                str(value)
                for value in (
                    ((active[1].get("metadata") or {}).get("superseded_venue_order_ids") or ())
                    if active is not None
                    else ()
                )
                if str(value)
            }
            prior_observed_order_id = self.observed_native_order_ids.get(symbol, "")
            if prior_observed_order_id:
                superseded_venue_order_ids.add(prior_observed_order_id)
            # A replacement/repair creates a new exchange-owned conditional
            # order identity. Never let an order id observed for the prior plan
            # authenticate an execution against the replacement.
            self.observed_native_order_ids.pop(symbol, None)
            observed_ns = self.clock.wall_time_ns()
            activation_key, activation_revision = self._next_activation(plan)
            if active is not None:
                prior = active[1]
                self.kernel.record_protection(
                    protection_key=active[0],
                    symbol=symbol,
                    status="replaced",
                    stop_price=_optional_float(prior.get("stop_price")),
                    take_profit_price=_optional_float(prior.get("take_profit_price")),
                    exchange_ts_ns=0,
                    local_receive_ts_ns=observed_ns,
                    metadata={
                        **dict(prior.get("metadata") or {}),
                        "replaced_by": activation_key,
                    },
                )
            if active is None or force or self._plan_key(active) != plan.protection_key:
                self.kernel.record_protection(
                    protection_key=activation_key,
                    symbol=symbol,
                    status="active",
                    stop_price=plan.stop_price,
                    take_profit_price=None,
                    exchange_ts_ns=0,
                    local_receive_ts_ns=observed_ns,
                    metadata={
                        "native_exchange": True,
                        "protection_plan_key": plan.protection_key,
                        "activation_revision": activation_revision,
                        "superseded_venue_order_ids": sorted(superseded_venue_order_ids),
                        "symbol": symbol,
                        "signed_qty": plan.signed_qty,
                        "stop_source": plan.stop_source,
                        "target_keys": list(plan.target_keys),
                        "trigger_by": "MarkPrice",
                    },
                )
            self.last_sync_ns = observed_ns
            self.last_sync_ns_by_symbol[symbol] = observed_ns
            self.last_error = ""
            return plan
        except Exception as exc:
            self.last_error = f"native protection sync failed for {symbol}: {exc}"[:1000]
            raise

    @_serialized_manager_method
    def reconcile_venue_positions(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Verify every reconstructed open position's native stop from REST truth.

        A local ``active`` event is not proof that the exchange still owns the
        stop. The position snapshot carries Bybit's current ``stopLoss``; a
        missing or different value is repaired before health is refreshed.
        """

        venue_rows: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").lower()
            size = _optional_float(row.get("size"))
            if symbol and side in {"buy", "sell"} and size is not None:
                venue_rows[symbol] = row

        state = self.kernel.state()
        for symbol, position in sorted(state.positions.items()):
            if position.signed_qty == 0.0:
                self.sync(symbol)
                continue
            plan = self.plan(symbol)
            if plan is None:  # pragma: no cover - guarded by signed_qty above
                raise RuntimeError(f"{symbol} cannot derive native protection plan")
            venue_row = venue_rows.get(symbol)
            if venue_row is None:
                raise RuntimeError(f"{symbol} open reconstructed position is absent from venue snapshot")
            venue_stop = _optional_float(venue_row.get("stopLoss") or venue_row.get("stop_loss"))
            rule = self.rules[symbol]
            tolerance = max(rule.tick_size / 2.0, 1e-12)
            active = self.active(symbol)
            local_matches = (
                active is not None
                and self._plan_key(active) == plan.protection_key
            )
            if (
                venue_stop is None
                or abs(venue_stop - plan.stop_price) > tolerance
                or not local_matches
            ):
                self.sync(symbol, force=True)
                continue
            observed_ns = self.clock.wall_time_ns()
            self.last_sync_ns = observed_ns
            self.last_sync_ns_by_symbol[symbol] = observed_ns
            self.last_error = ""

    @_serialized_manager_method
    def sync_symbols(self, symbols: Sequence[str]) -> tuple[NativeProtectionPlan, ...]:
        plans = []
        for symbol in sorted(set(symbols)):
            plan = self.sync(symbol)
            if plan is not None:
                plans.append(plan)
        return tuple(plans)

    @_serialized_manager_method
    def adopt_execution(
        self,
        row: Mapping[str, Any],
        *,
        local_receive_ts_ns: int,
    ) -> tuple[Any, ...]:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            raise AccountTransitionError("external execution lacks a symbol")
        active = self.active(symbol)
        identity_evidence = self.native_execution_identity_evidence(row)
        side = str(row.get("side") or "").lower()
        qty = _required_float(row.get("execQty") or row.get("exec_qty"), "execQty")
        signed_qty = qty if side == "buy" else -qty if side == "sell" else 0.0
        execution_id = str(row.get("execId") or row.get("exec_id") or "")
        if not execution_id or signed_qty == 0.0:
            raise AccountTransitionError("external execution lacks an execution id or side")
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if not venue_order_id:
            raise AccountTransitionError("external execution lacks venue orderId")
        execution_origin = (
            "bybit_stop_loss_unbound"
            if identity_evidence == "bybit_stop_provenance_unbound"
            else "verified_native_stop"
            if identity_evidence
            else _external_execution_origin(row)
        )
        reason = {
            "verified_native_stop": "native_protection_triggered",
            "bybit_stop_loss_unbound": "bybit_stop_loss_identity_unverified",
            "venue_liquidation": "venue_liquidation_reduction",
            "venue_adl": "venue_adl_reduction",
            "unattributed_external_reduction": "external_unattributed_reduction",
        }[execution_origin]
        protection_key = (
            active[0]
            if active is not None
            else f"external-reduction:{symbol}:{venue_order_id}"
        )
        fee_observed = row.get("execFee") not in (None, "") or row.get(
            "exec_fee"
        ) not in (None, "")
        try:
            return self.kernel.adopt_external_protection_fill(
                protection_key=protection_key,
                venue_order_id=venue_order_id,
                execution_id=execution_id,
                symbol=symbol,
                signed_qty=signed_qty,
                price=_required_float(
                    row.get("execPrice") or row.get("exec_price"),
                    "execPrice",
                ),
                fee_usdt=float(row.get("execFee") or row.get("exec_fee") or 0.0),
                exchange_ts_ns=_timestamp_ns(row.get("execTime") or row.get("exec_time")),
                local_receive_ts_ns=local_receive_ts_ns,
                reason=reason,
                execution_origin=execution_origin,
                metadata={
                    "source": "bybit_private_execution_ws",
                    "external_order_link_id": str(
                        row.get("orderLinkId") or row.get("order_link_id") or ""
                    ),
                    "exec_type": str(row.get("execType") or row.get("exec_type") or ""),
                    "create_type": str(row.get("createType") or row.get("create_type") or ""),
                    "stop_order_type": str(
                        row.get("stopOrderType") or row.get("stop_order_type") or ""
                    ),
                    "native_identity": identity_evidence,
                    "fee_observed": fee_observed,
                    "fee_status": (
                        "observed_execution_fee"
                        if fee_observed
                        else "pending_missing_execution_fee"
                    ),
                    "fee_source": "bybit_private_execution.execFee",
                },
            )
        except AccountTransitionError as exc:
            self.last_error = (
                f"external execution could not be adopted for {symbol}: {exc}"
            )[:1000]
            raise

    @staticmethod
    def is_position_execution(row: Mapping[str, Any]) -> bool:
        """Exclude cash-flow-only private execution rows from position adoption."""

        exec_type = str(
            row.get("execType") or row.get("exec_type") or ""
        ).lower().replace("_", "")
        return exec_type != "funding"

    @_serialized_manager_method
    def note_adoption_failure(
        self,
        row: Mapping[str, Any],
        exc: AccountTransitionError,
    ) -> None:
        symbol = str(row.get("symbol") or "missing-symbol").upper()
        execution_id = str(row.get("execId") or row.get("exec_id") or "missing-exec-id")
        self.last_error = (
            f"unaccounted position execution for {symbol} execId={execution_id}: {exc}"
        )[:1000]

    @_serialized_manager_method
    def observe_order(self, row: Mapping[str, Any]) -> bool:
        """Remember a verified native order id before its first execution arrives."""

        if not self.is_verified_native_order(row):
            return False
        symbol = str(row.get("symbol") or "").upper()
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if symbol and venue_order_id:
            self.observed_native_order_ids[symbol] = venue_order_id
            raw_status = str(
                row.get("orderStatus") or row.get("order_status") or ""
            ).lower().replace("_", "")
            cumulative = _finite_or_zero(row.get("cumExecQty") or row.get("cum_exec_qty"))
            if raw_status in {"cancelled", "canceled", "deactivated", "rejected"} and (
                cumulative <= 0.0
            ):
                active = self.active(symbol)
                if active is not None:
                    prior = active[1]
                    status = "rejected_unfilled" if raw_status == "rejected" else "cancelled_unfilled"
                    self.kernel.record_protection(
                        protection_key=active[0],
                        symbol=symbol,
                        status=status,
                        stop_price=_optional_float(prior.get("stop_price")),
                        take_profit_price=_optional_float(prior.get("take_profit_price")),
                        exchange_ts_ns=_timestamp_ns(
                            row.get("updatedTime") or row.get("updated_time")
                        ),
                        local_receive_ts_ns=self.clock.wall_time_ns(),
                        metadata={
                            **dict(prior.get("metadata") or {}),
                            "venue_order_id": venue_order_id,
                            "terminal_order_status": str(row.get("orderStatus") or ""),
                        },
                    )
                    self.last_error = (
                        f"native protection {status.replace('_', ' ')} for {symbol}: "
                        f"orderId={venue_order_id}"
                    )[:1000]
            elif raw_status in {"cancelled", "canceled", "deactivated", "filled"} and (
                cumulative > 0.0
            ):
                self.last_error = (
                    f"native protection terminal execution recovery pending for {symbol}: "
                    f"orderId={venue_order_id} cumulative_qty={cumulative:g}"
                )[:1000]
            return True
        return False

    @_serialized_manager_method
    def observe_terminal_status(self, *, command_id: str, status: str) -> None:
        """Persist loss of a partially-filled native stop's working remainder."""

        if status != "partially_filled_cancelled":
            state = self.kernel.state()
            order = state.orders.get(command_id)
            position = state.positions.get(order.symbol) if order is not None else None
            if order is not None and (position is None or abs(position.signed_qty) <= 1e-12):
                self.last_error = ""
            return
        state = self.kernel.state()
        order = state.orders.get(command_id)
        if order is None or not order.batch_id.startswith("external-protection/"):
            return
        position = state.positions.get(order.symbol)
        if position is None or abs(position.signed_qty) <= 1e-12:
            return
        matches = [
            (key, protection)
            for key, protection in state.protections.items()
            if str(protection.get("command_id") or "") == command_id
            and str(protection.get("status") or "") in {"active", "triggering"}
        ]
        if not matches:
            return
        protection_key, prior = matches[-1]
        observed_ns = self.clock.wall_time_ns()
        self.kernel.record_protection(
            protection_key=protection_key,
            symbol=order.symbol,
            status="cancelled_with_residual",
            stop_price=_optional_float(prior.get("stop_price")),
            take_profit_price=_optional_float(prior.get("take_profit_price")),
            exchange_ts_ns=0,
            local_receive_ts_ns=observed_ns,
            command_id=command_id,
            metadata={
                **dict(prior.get("metadata") or {}),
                "venue_order_id": order.venue_order_id,
                "residual_signed_qty": position.signed_qty,
                "terminal_order_status": status,
            },
        )
        self.last_error = (
            f"native protection partially filled then cancelled for {order.symbol}; "
            f"residual position {position.signed_qty:g} requires deterministic close recovery"
        )[:1000]

    @_serialized_manager_method
    def observe_adopted_fill_progress(
        self,
        *,
        command_id: str,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
    ) -> None:
        """Finish protection provenance when later fills join by venue order id."""

        state = self.kernel.state()
        order = state.orders.get(command_id)
        if order is None or order.status != "filled":
            return
        native = order.batch_id.startswith("external-protection/")
        external = order.batch_id.startswith("external-reduction/")
        if not native and not external:
            return
        position = state.positions.get(order.symbol)
        if position is not None and abs(position.signed_qty) > 1e-12:
            return
        matches = [
            (key, protection)
            for key, protection in state.protections.items()
            if str(protection.get("command_id") or "") == command_id
            and str(protection.get("status") or "")
            in {"triggering", "external_reduction_partial"}
        ]
        if not matches:
            return
        protection_key, prior = matches[-1]
        final_status = "triggered" if native else "external_reduction_flat"
        self.kernel.record_protection(
            protection_key=protection_key,
            symbol=order.symbol,
            status=final_status,
            stop_price=_optional_float(prior.get("stop_price")),
            take_profit_price=_optional_float(prior.get("take_profit_price")),
            exchange_ts_ns=exchange_ts_ns,
            local_receive_ts_ns=local_receive_ts_ns,
            command_id=command_id,
            metadata={
                **dict(prior.get("metadata") or {}),
                "venue_order_id": order.venue_order_id,
                "completed_via_joined_venue_order_id": True,
            },
        )

    @_serialized_manager_method
    def is_verified_native_order(self, row: Mapping[str, Any]) -> bool:
        symbol = str(row.get("symbol") or "").upper()
        active = self.active(symbol)
        if not symbol or active is None or not _has_native_stop_provenance(row):
            return False
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if not venue_order_id:
            return False
        expected = str((active[1].get("metadata") or {}).get("venue_order_id") or "")
        observed = self.observed_native_order_ids.get(symbol, "")
        superseded = {
            str(value)
            for value in (
                (active[1].get("metadata") or {}).get("superseded_venue_order_ids") or ()
            )
            if str(value)
        }
        if venue_order_id in superseded:
            return False
        if expected:
            return venue_order_id == expected
        if observed:
            return venue_order_id == observed
        trigger = _optional_float(row.get("triggerPrice") or row.get("trigger_price"))
        active_stop = _optional_float(active[1].get("stop_price"))
        rule = self.rules.get(symbol)
        if trigger is None or active_stop is None or rule is None:
            return False
        return abs(trigger - active_stop) <= max(rule.tick_size / 2.0, 1e-12)

    @_serialized_manager_method
    def is_verified_native_execution(self, row: Mapping[str, Any]) -> bool:
        return bool(self.native_execution_identity_evidence(row))

    @_serialized_manager_method
    def native_execution_identity_evidence(self, row: Mapping[str, Any]) -> str:
        symbol = str(row.get("symbol") or "").upper()
        active = self.active(symbol)
        if not symbol or active is None:
            return ""
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if not venue_order_id:
            return ""
        expected = str((active[1].get("metadata") or {}).get("venue_order_id") or "")
        observed = self.observed_native_order_ids.get(symbol, "")
        if expected:
            return "matched_installed_venue_order_id" if venue_order_id == expected else ""
        if observed:
            return "matched_verified_native_order_event" if venue_order_id == observed else ""
        return "bybit_stop_provenance_unbound" if _has_native_stop_provenance(row) else ""

    @_serialized_manager_method
    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        if self.last_error:
            raise RuntimeError(self.last_error)
        state = self.kernel.state()
        open_symbols = [
            symbol for symbol, position in state.positions.items() if position.signed_qty != 0.0
        ]
        active_by_symbol = {symbol: self.active(symbol) for symbol in open_symbols}
        missing = [symbol for symbol, active in active_by_symbol.items() if active is None]
        if missing:
            raise RuntimeError(
                "open reconstructed positions lack active native disaster protection: "
                + ", ".join(sorted(missing))
            )
        triggering = [
            symbol
            for symbol, active in active_by_symbol.items()
            if active is not None
            and str(active[1].get("status") or "") == "triggering"
        ]
        if triggering:
            raise RuntimeError(
                "native protection trigger is unresolved for "
                + ", ".join(sorted(triggering))
            )
        now = self.clock.wall_time_ns()
        stale = [
            symbol
            for symbol in open_symbols
            if self.last_sync_ns_by_symbol.get(symbol, 0) <= 0
            or now - self.last_sync_ns_by_symbol.get(symbol, 0) > max(int(max_age_ns), 1)
        ]
        if stale:
            raise RuntimeError(
                "native protection health is stale for " + ", ".join(sorted(stale))
            )


def _round_stop(value: float, tick_size: float, *, long_position: bool) -> float:
    tick = Decimal(str(tick_size))
    units = Decimal(str(value)) / tick
    nearest = units.to_integral_value(rounding=ROUND_HALF_EVEN)
    if abs(units - nearest) <= Decimal("1e-12"):
        units = nearest
    rounding = ROUND_FLOOR if long_position else ROUND_CEILING
    return float(units.to_integral_value(rounding=rounding) * tick)


def _decimal_text(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _optional_float(value: object) -> float | None:
    try:
        output = float(str(value))
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) and output > 0.0 else None


def _required_float(value: object, label: str) -> float:
    try:
        output = float(str(value))
    except (TypeError, ValueError) as exc:
        raise AccountTransitionError(f"external execution {label} is missing") from exc
    if not math.isfinite(output) or output <= 0.0:
        raise AccountTransitionError(f"external execution {label} must be positive")
    return output


def _finite_or_zero(value: object) -> float:
    try:
        output = float(str(value))
    except (TypeError, ValueError):
        return 0.0
    return output if math.isfinite(output) else 0.0


def _timestamp_ns(value: object) -> int:
    try:
        output = float(str(value))
    except (TypeError, ValueError):
        return 0
    return int(output * 1_000_000) if math.isfinite(output) and output > 0.0 else 0


def _has_native_stop_provenance(row: Mapping[str, Any]) -> bool:
    stop_type = str(
        row.get("stopOrderType") or row.get("stop_order_type") or ""
    ).lower().replace("_", "")
    create_type = str(
        row.get("createType") or row.get("create_type") or ""
    ).lower().replace("_", "")
    return stop_type in {"stoploss", "partialstoploss"} or create_type in {
        "createbystoploss",
        "createbypartialstoploss",
    }


def _external_execution_origin(row: Mapping[str, Any]) -> str:
    create_type = str(
        row.get("createType") or row.get("create_type") or ""
    ).lower().replace("_", "")
    exec_type = str(
        row.get("execType") or row.get("exec_type") or ""
    ).lower().replace("_", "")
    if exec_type == "adltrade":
        return "venue_adl"
    if create_type == "createbyliq" or exec_type == "busttrade":
        return "venue_liquidation"
    return "unattributed_external_reduction"
