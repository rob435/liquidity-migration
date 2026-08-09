"""Account-owned exchange-native disaster stops and external-fill adoption."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Callable, Set as AbstractSet
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
from threading import RLock
from typing import Any, Concatenate, Mapping, ParamSpec, Sequence, TypeVar

from liquidity_migration.account.account_contracts import (
    AccountState,
    AccountTransitionError,
    InstrumentRules,
    PositionState,
    quantity_tolerance,
)
from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.strategy.account_strategy_state import component_execution_anchors_from_snapshot
from liquidity_migration.venue.bybit_execution_adapter import bybit_private_execution_metadata
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock
from liquidity_migration.core.venue_realm import client_venue_realm
from liquidity_migration.account.native_protection_math import round_native_stop


_logger = logging.getLogger(__name__)

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


# Bybit's open-order queries can keep listing a consumed Full-position stop
# (triggered, cancelled on flat, or superseded) for minutes after the
# protection record has left the {active, triggering} statuses. Within this
# window the ownership verifier still recognizes such rows against the latest
# native protection record; a row that lingers longer pages as unowned again.
NATIVE_TERMINAL_ORDER_VISIBILITY_GRACE_NS = 10 * 60 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class NativeProtectionPlan:
    protection_key: str
    symbol: str
    signed_qty: float
    stop_price: float
    stop_source: str
    target_keys: tuple[str, ...]



#: One committed state's native protections, bucketed by symbol, and the state
#: it was built from. A commit publishes a *new* committed state object rather
#: than mutating the old one, so identity is the right key.
#:
#: Worth building: both lookups below filtered every protection the account has
#: ever recorded, per symbol, on every reconcile pass. Protections are never
#: pruned -- 1,391 of them on the demo book after a day of trading, against 200
#: that morning -- and a profile of the reconcile put 16% of its time in these
#: two scans alone, growing.
_NATIVE_INDEX_LOCK = RLock()
_NATIVE_INDEX_STATE: Any = None
_NATIVE_INDEX: dict[str, list[tuple[str, Mapping[str, Any]]]] | None = None


def _native_protection_index(state: AccountState) -> dict[str, list[tuple[str, Mapping[str, Any]]]] | None:
    """Native protections per symbol, in the order the state holds them.

    Returns ``None`` when the index cannot represent this state exactly, and
    the callers fall back to the scan they always did. That happens when a
    native protection carries no symbol of its own: the filters below read such
    a row as matching *whichever* symbol is being asked about, which no
    per-symbol bucket can reproduce. It is not expected to occur, and this
    stays correct if it ever does.
    """

    global _NATIVE_INDEX_STATE, _NATIVE_INDEX
    with _NATIVE_INDEX_LOCK:
        if _NATIVE_INDEX_STATE is state:
            return _NATIVE_INDEX
        index: dict[str, list[tuple[str, Mapping[str, Any]]]] | None = {}
        for key, protection in state.protections.items():
            metadata = protection.get("metadata") or {}
            if not bool(metadata.get("native_exchange")):
                continue
            symbol = str(metadata.get("symbol") or "").upper()
            if not symbol:
                index = None
                break
            index.setdefault(symbol, []).append((key, protection))
        _NATIVE_INDEX_STATE = state
        _NATIVE_INDEX = index
        return index

@dataclass(frozen=True, slots=True)
class NativeProtectionBreach:
    """Authenticated evidence that an absent desired stop is already crossed."""

    plan: NativeProtectionPlan
    observed_mark: float
    evidence_source: str
    detail: str
    observed_ts_ns: int


@dataclass(frozen=True, slots=True)
class _EntryAttachedStopCandidate:
    command_id: str
    symbol: str
    signed_qty: float
    stop_price: float
    stop_fraction: float
    stop_source: str
    trigger_by: str
    created_ts_ns: int
    command_sequence: int


class NativeProtectionBreachError(RuntimeError):
    def __init__(self, breach: NativeProtectionBreach) -> None:
        self.breach = breach
        super().__init__(breach.detail)


class NativeProtectionReconciliationError(RuntimeError):
    """Aggregate every symbol result while preserving breach-only authority."""

    def __init__(self, failures: Sequence[tuple[str, BaseException]], *, operation: str) -> None:
        self.failures = tuple(failures)
        self.breaches_only = bool(self.failures) and all(
            isinstance(error, NativeProtectionBreachError) for _symbol, error in self.failures
        )
        detail = "; ".join(f"{symbol}:{type(error).__name__}:{error}" for symbol, error in self.failures)
        super().__init__(f"native protection {operation} failed: {detail}"[:1000])


class AccountHealthChain:
    """Require every account-owner health check to pass.

    Each provider receives the caller's freshness bound; a provider whose
    evidence moves on a slower timescale may apply a documented domain floor
    to it (funding-settlement recovery does), so the chain bound is the
    minimum strictness, not an exact shared window.
    """

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
        position_stream_cache: Any | None = None,
        entry_stop_push_wait_seconds: float = 0.5,
    ) -> None:
        # The manager is realm-agnostic: it installs a full-position stop for
        # whatever account the client addresses. What it must not accept is a
        # client whose realm and transport disagree, because the stop would then
        # be installed somewhere other than where the exposure is.
        self.realm = client_venue_realm(client, what="native protection manager")
        fraction = float(fallback_stop_fraction)
        if not math.isfinite(fraction) or not 0.0 < fraction < 1.0:
            raise ValueError("fallback_stop_fraction must be explicitly set in (0, 1)")
        self.kernel = kernel
        self.client = client
        self.rules = {symbol.upper(): rule for symbol, rule in instrument_rules.items()}
        self.fallback_stop_fraction = fraction
        self.clock = clock or SystemClock()
        # Optional accelerator for entry-attached stop verification. Absent, or
        # holding nothing newer than the acknowledgement being verified, the
        # REST read runs exactly as it always has. The wait is bounded well
        # under the two round trips it replaces, so the slow path is never
        # slower than what shipped before it.
        self.position_stream_cache = position_stream_cache
        self.entry_stop_push_wait_seconds = max(float(entry_stop_push_wait_seconds), 0.0)
        # WS callbacks and the reconciliation loop share this manager. One lock
        # spans planning, the Bybit mutation, journal activation, and every
        # adoption transition, so two revisions cannot be installed from the same
        # pre-mutation state. Reentrant because public methods call one another.
        self._lock = RLock()
        self.last_sync_ns = 0
        self.last_sync_ns_by_symbol: dict[str, int] = {}
        self.last_error = ""
        self.observed_native_order_ids: dict[str, str] = {}
        # Venue child order id -> durable parent entry command.  A set is not
        # sufficient when Bybit replaces a Full-position stop during scale-in:
        # the same symbol can expose multiple recent child identities.
        self.observed_entry_stop_order_ids: dict[str, dict[str, str]] = {}
        self._breaches: dict[str, NativeProtectionBreach] = {}
        # Symbols whose entry-attached stop the venue did not apply and this
        # process could not repair. Latched here because the fill may not be
        # journaled yet, so there is no plan to breach against; the next
        # reconciliation pass converts the latch into a real breach.
        self._unarmed_entries: dict[str, dict[str, Any]] = {}
        self._restore_persisted_breaches()

    def _restore_persisted_breaches(self) -> None:
        """Rebuild the durable breach latch before any repair can mutate Bybit.

        A restart is not evidence that a crossed, absent stop became safe, so
        every latest unresolved breach is restored from the journal. Only
        authenticated proof of the same installed stop, or an authenticated flat
        position, clears one.
        """

        latest_by_symbol: dict[str, tuple[int, NativeProtectionBreach]] = {}
        for protection_key, protection in self.kernel._state_ref().protections.items():
            if str(protection.get("status") or "") != "breached_unprotected":
                continue
            metadata = protection.get("metadata") or {}
            if not isinstance(metadata, Mapping) or not bool(metadata.get("native_exchange")):
                continue
            symbol = str(metadata.get("symbol") or protection.get("symbol") or "").upper()
            stop_price = _optional_float(protection.get("stop_price"))
            signed_qty = _finite_or_zero(metadata.get("signed_qty"))
            breach_mark = _optional_float(metadata.get("breach_mark"))
            observed_ts_ns = int(protection.get("local_receive_ts_ns") or 0)
            if (
                not symbol
                or stop_price is None
                or signed_qty is None
                or signed_qty == 0.0
                or breach_mark is None
                or observed_ts_ns <= 0
            ):
                self.last_error = (f"persisted native protection breach {protection_key!r} is incomplete")[:1000]
                continue
            crossed = (signed_qty > 0.0 and stop_price >= breach_mark) or (
                signed_qty < 0.0 and stop_price <= breach_mark
            )
            if not crossed:
                self.last_error = (f"persisted native protection breach {protection_key!r} has contradictory prices")[
                    :1000
                ]
                continue
            plan = NativeProtectionPlan(
                protection_key=str(metadata.get("protection_plan_key") or protection_key),
                symbol=symbol,
                signed_qty=signed_qty,
                stop_price=stop_price,
                stop_source=str(metadata.get("stop_source") or "persisted_native_protection_breach"),
                target_keys=tuple(sorted(str(key) for key in (metadata.get("target_keys") or ()) if str(key))),
            )
            detail = str(metadata.get("breach_detail") or "").strip() or (
                f"{symbol} native stop {stop_price:g} was previously absent and crossed "
                f"at authenticated mark {breach_mark:g}"
            )
            breach = NativeProtectionBreach(
                plan=plan,
                observed_mark=breach_mark,
                evidence_source=str(metadata.get("breach_evidence_source") or "persisted_native_protection_breach"),
                detail=detail[:1000],
                observed_ts_ns=observed_ts_ns,
            )
            prior = latest_by_symbol.get(symbol)
            if prior is None or observed_ts_ns > prior[0]:
                latest_by_symbol[symbol] = (observed_ts_ns, breach)
        self._breaches.update((symbol, row[1]) for symbol, row in latest_by_symbol.items())
        if self._breaches:
            self.last_error = "; ".join(self._breaches[symbol].detail for symbol in sorted(self._breaches))[:1000]

    @_serialized_manager_method
    def plan(self, symbol: str) -> NativeProtectionPlan | None:
        symbol = symbol.upper()
        events, state = self.kernel._snapshot_ref()
        position = state.positions.get(symbol)
        if position is None or position.signed_qty == 0.0:
            return None
        rule = self.rules.get(symbol)
        if rule is None or rule.tick_size <= 0.0:
            raise RuntimeError(f"{symbol} lacks a verified positive venue tick_size")
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
            for anchor in component_execution_anchors_from_snapshot(
                events,
                state=state,
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
            raw_stop_fraction = metadata.get("stop_loss_pct")
            if raw_stop_fraction is None or raw_stop_fraction == "":
                continue
            try:
                stop_fraction = float(str(raw_stop_fraction))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"{symbol} stop_loss_pct is not numeric: {raw_stop_fraction!r}") from exc
            if not math.isfinite(stop_fraction) or not 0.0 < stop_fraction < 1.0:
                # Ignoring it would silently substitute the much wider account
                # fallback fraction for the component-anchored stop.
                raise RuntimeError(f"{symbol} stop_loss_pct must be a fraction in (0, 1), got {raw_stop_fraction!r}")
            fill_price = float(anchor.entry_fill_vwap)
            explicit.append(fill_price * (1.0 - stop_fraction if position.signed_qty > 0.0 else 1.0 + stop_fraction))
        if explicit:
            # A Full-position process-death seatbelt, not a component exit: it
            # must sit outside every software component stop, or the first
            # ordinary trigger would flatten all same-symbol owners at the venue.
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
                1.0 - self.fallback_stop_fraction if position.signed_qty > 0.0 else 1.0 + self.fallback_stop_fraction
            )
            source = "explicit_account_fallback_fraction"
        stop = round_native_stop(
            raw_stop,
            rule.tick_size,
            long_position=position.signed_qty > 0.0,
        )
        target_keys = tuple(sorted(key for key, _target in targets))
        material = {
            "symbol": symbol,
            "signed_qty": position.signed_qty,
            "stop_price": stop,
            "stop_source": source,
            "target_keys": target_keys,
        }
        protection_key = f"native-disaster:{symbol}:" + hashlib.sha256(canonical_json(material)).hexdigest()[:20]
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

        tolerance = quantity_tolerance(position.signed_qty)
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
            abs(float(target.get("signed_qty") or 0.0)) > tolerance for target in symbol_desires
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
            not order.reduce_only or order.remaining_signed_qty * position.signed_qty >= 0.0 for order in working_orders
        ):
            return None
        projected_qty = math.fsum([position.signed_qty] + [order.remaining_signed_qty for order in working_orders])
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
        protection_key = self._plan_key(active)
        if not protection_key:
            return None
        return NativeProtectionPlan(
            protection_key=protection_key,
            symbol=symbol,
            signed_qty=position.signed_qty,
            stop_price=stop,
            stop_source=str(metadata.get("stop_source") or "retained_canonical_close"),
            target_keys=tuple(sorted(str(key) for key in (metadata.get("target_keys") or ()) if str(key))),
        )

    @_serialized_manager_method
    def active(self, symbol: str) -> tuple[str, Mapping[str, Any]] | None:
        symbol = symbol.upper()
        return self._active_from_state(self.kernel._state_ref(), symbol)

    @staticmethod
    def _active_from_state(
        state: AccountState,
        symbol: str,
    ) -> tuple[str, Mapping[str, Any]] | None:
        index = _native_protection_index(state)
        if index is not None:
            # Same order the state holds them in, so ``matches[-1]`` still
            # means the same protection it always did.
            matches = [
                item
                for item in index.get(symbol, ())
                if str(item[1].get("status") or "") in {"active", "triggering"}
            ]
        else:
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
        return str((active[1].get("metadata") or {}).get("protection_plan_key") or active[0])

    @_serialized_manager_method
    def _next_activation(self, plan: NativeProtectionPlan) -> tuple[str, int]:
        revisions = [
            int((protection.get("metadata") or {}).get("activation_revision") or 1)
            for key, protection in self.kernel._state_ref().protections.items()
            if key == plan.protection_key
            or str((protection.get("metadata") or {}).get("protection_plan_key") or "") == plan.protection_key
        ]
        revision = max(revisions, default=0) + 1
        key = plan.protection_key if revision == 1 else f"{plan.protection_key}:activation:{revision:04d}"
        return key, revision

    def _record_breach(
        self,
        plan: NativeProtectionPlan,
        *,
        observed_mark: float,
        evidence_source: str,
        detail: str = "",
    ) -> NativeProtectionBreachError:
        prior_breach = self._breaches.get(plan.symbol)
        if prior_breach is None:
            matches = [
                protection
                for protection in self.kernel._state_ref().protections.values()
                if str(protection.get("status") or "") == "breached_unprotected"
                and str(
                    (protection.get("metadata") or {}).get("protection_plan_key")
                    or protection.get("protection_key")
                    or ""
                )
                == plan.protection_key
            ]
            if matches:
                persisted = max(
                    matches,
                    key=lambda row: int(row.get("local_receive_ts_ns") or 0),
                )
                metadata = persisted.get("metadata") or {}
                persisted_mark = _optional_float(metadata.get("breach_mark"))
                if persisted_mark is not None:
                    prior_breach = NativeProtectionBreach(
                        plan=plan,
                        observed_mark=persisted_mark,
                        evidence_source=str(
                            metadata.get("breach_evidence_source") or "persisted_native_protection_breach"
                        ),
                        detail=str(metadata.get("breach_detail") or detail)[:1000],
                        observed_ts_ns=max(
                            int(persisted.get("local_receive_ts_ns") or 0),
                            1,
                        ),
                    )
                    self._breaches[plan.symbol] = prior_breach
        if prior_breach is not None and prior_breach.plan.protection_key == plan.protection_key:
            self.last_error = prior_breach.detail[:1000]
            return NativeProtectionBreachError(prior_breach)
        observed_ns = self.clock.wall_time_ns()
        rendered = detail or (
            f"{plan.symbol} native stop {plan.stop_price:g} is absent and already "
            f"crossed at authenticated mark {observed_mark:g}"
        )
        breach = NativeProtectionBreach(
            plan=plan,
            observed_mark=observed_mark,
            evidence_source=evidence_source,
            detail=rendered[:1000],
            observed_ts_ns=observed_ns,
        )
        active = self.active(plan.symbol)
        protection_key = active[0] if active is not None else plan.protection_key
        current = self.kernel._state_ref().protections.get(protection_key) or {}
        current_metadata = current.get("metadata") or {}
        already_recorded = (
            str(current.get("status") or "") == "breached_unprotected"
            and str(current_metadata.get("protection_plan_key") or protection_key) == plan.protection_key
        )
        if not already_recorded:
            prior_metadata = dict(active[1].get("metadata") or {}) if active else {}
            self.kernel.record_protection(
                protection_key=protection_key,
                symbol=plan.symbol,
                status="breached_unprotected",
                stop_price=plan.stop_price,
                take_profit_price=None,
                exchange_ts_ns=0,
                local_receive_ts_ns=observed_ns,
                metadata={
                    **prior_metadata,
                    "native_exchange": True,
                    "protection_plan_key": plan.protection_key,
                    "symbol": plan.symbol,
                    "signed_qty": plan.signed_qty,
                    "stop_source": plan.stop_source,
                    "target_keys": list(plan.target_keys),
                    "trigger_by": "MarkPrice",
                    "breach_mark": observed_mark,
                    "breach_evidence_source": evidence_source,
                    "breach_detail": rendered[:1000],
                },
            )
        self._breaches[plan.symbol] = breach
        self.last_error = breach.detail
        return NativeProtectionBreachError(breach)

    def _require_repair_not_crossed(
        self,
        plan: NativeProtectionPlan,
        *,
        observed_mark: float,
        evidence_source: str,
    ) -> None:
        if not math.isfinite(observed_mark) or observed_mark <= 0.0:
            raise RuntimeError(
                f"{plan.symbol} cannot repair native protection without a positive "
                f"authenticated mark from {evidence_source}"
            )
        crossed = (plan.signed_qty > 0.0 and plan.stop_price >= observed_mark) or (
            plan.signed_qty < 0.0 and plan.stop_price <= observed_mark
        )
        if crossed:
            raise self._record_breach(
                plan,
                observed_mark=observed_mark,
                evidence_source=evidence_source,
            )

    def _adopt_verified_venue_stop(
        self,
        plan: NativeProtectionPlan,
        *,
        venue_mark: float | None,
    ) -> None:
        """Journal an authenticated matching Full stop without mutating Bybit."""

        active = self.active(plan.symbol)
        observed_ns = self.clock.wall_time_ns()
        activation_key, activation_revision = self._next_activation(plan)
        prior_venue_order_ids: set[str] = set()
        breach_records = [
            (key, row)
            for key, row in self.kernel._state_ref().protections.items()
            if str(row.get("status") or "") == "breached_unprotected"
            and str((row.get("metadata") or {}).get("protection_plan_key") or key) == plan.protection_key
        ]
        if breach_records:
            breach_key, breach_row = max(
                breach_records,
                key=lambda item: int(item[1].get("local_receive_ts_ns") or 0),
            )
            self.kernel.record_protection(
                protection_key=breach_key,
                symbol=plan.symbol,
                status="protection_restored",
                stop_price=_optional_float(breach_row.get("stop_price")),
                take_profit_price=_optional_float(breach_row.get("take_profit_price")),
                exchange_ts_ns=0,
                local_receive_ts_ns=observed_ns,
                metadata={
                    **dict(breach_row.get("metadata") or {}),
                    "breach_recovery": "authenticated_matching_stop",
                },
            )
        if active is not None:
            prior = active[1]
            metadata = prior.get("metadata") or {}
            prior_venue_order_ids.update(
                str(value) for value in metadata.get("native_venue_order_id_lineage") or () if str(value)
            )
            prior_venue_order_ids.update(
                str(value) for value in metadata.get("superseded_venue_order_ids") or () if str(value)
            )
            venue_order_id = str(metadata.get("venue_order_id") or "")
            if venue_order_id:
                prior_venue_order_ids.add(venue_order_id)
            self.kernel.record_protection(
                protection_key=active[0],
                symbol=plan.symbol,
                status="replaced",
                stop_price=_optional_float(prior.get("stop_price")),
                take_profit_price=_optional_float(prior.get("take_profit_price")),
                exchange_ts_ns=0,
                local_receive_ts_ns=observed_ns,
                metadata={**dict(metadata), "replaced_by": activation_key},
            )
        observed_order_id = self.observed_native_order_ids.get(plan.symbol, "")
        if observed_order_id:
            prior_venue_order_ids.add(observed_order_id)
        self.kernel.record_protection(
            protection_key=activation_key,
            symbol=plan.symbol,
            status="active",
            stop_price=plan.stop_price,
            take_profit_price=None,
            exchange_ts_ns=0,
            local_receive_ts_ns=observed_ns,
            metadata={
                "native_exchange": True,
                "protection_plan_key": plan.protection_key,
                "activation_revision": activation_revision,
                "superseded_venue_order_ids": sorted(prior_venue_order_ids),
                "native_venue_order_id_lineage": sorted(prior_venue_order_ids),
                "symbol": plan.symbol,
                "signed_qty": plan.signed_qty,
                "stop_source": plan.stop_source,
                "target_keys": list(plan.target_keys),
                "trigger_by": "MarkPrice",
                "recovered_from_authenticated_position_snapshot": True,
                "authenticated_mark": venue_mark,
            },
        )
        self._breaches.pop(plan.symbol, None)
        self.last_sync_ns = observed_ns
        self.last_sync_ns_by_symbol[plan.symbol] = observed_ns
        self.last_error = ""

    @_serialized_manager_method
    def breaches(self) -> tuple[NativeProtectionBreach, ...]:
        return tuple(self._breaches[symbol] for symbol in sorted(self._breaches))

    @_serialized_manager_method
    def sync(
        self,
        symbol: str,
        *,
        force: bool = False,
        observed_mark: float | None = None,
        mark_source: str = "",
        authenticated_position_flat: bool = False,
        flat_evidence_source: str = "",
    ) -> NativeProtectionPlan | None:
        symbol = symbol.upper()
        plan = self.plan(symbol)
        if plan is None:
            unresolved_breach = self._breaches.get(symbol)
            if unresolved_breach is not None and not authenticated_position_flat:
                # A reconstructed zero can precede or contradict REST truth, so
                # only an authenticated position snapshot clears the latch.
                self.last_error = unresolved_breach.detail
                raise NativeProtectionBreachError(unresolved_breach)
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
            else:
                latest = self._latest_native_protection_from_state(
                    self.kernel._state_ref(),
                    symbol,
                )
                if latest is not None and str(latest[1].get("status") or "") == "breached_unprotected":
                    prior = latest[1]
                    self.kernel.record_protection(
                        protection_key=latest[0],
                        symbol=symbol,
                        status="position_flat_recovered",
                        stop_price=_optional_float(prior.get("stop_price")),
                        take_profit_price=_optional_float(prior.get("take_profit_price")),
                        exchange_ts_ns=0,
                        local_receive_ts_ns=observed_ns,
                        metadata={
                            **dict(prior.get("metadata") or {}),
                            "breach_recovery": "authenticated_position_flat",
                            "breach_recovery_evidence_source": (
                                flat_evidence_source
                                or "authenticated_position_snapshot"
                            ),
                        },
                    )
            self.observed_native_order_ids.pop(symbol, None)
            self.observed_entry_stop_order_ids.pop(symbol, None)
            self._breaches.pop(symbol, None)
            self.last_sync_ns = observed_ns
            self.last_sync_ns_by_symbol[symbol] = observed_ns
            self.last_error = ""
            return None
        unresolved_breach = self._breaches.get(symbol)
        if unresolved_breach is not None:
            # A threshold breach is a latch, not a transient validation error, so
            # a price recovery or target revision cannot re-arm protection. Only
            # proof of the same stop, or a flat venue position, clears it.
            self.last_error = unresolved_breach.detail
            raise NativeProtectionBreachError(unresolved_breach)
        active = self.active(symbol)
        if active is not None and self._plan_key(active) == plan.protection_key and not force:
            self._breaches.pop(symbol, None)
            self.last_sync_ns = self.clock.wall_time_ns()
            self.last_sync_ns_by_symbol[symbol] = self.last_sync_ns
            self.last_error = ""
            return plan
        if observed_mark is not None:
            self._require_repair_not_crossed(
                plan,
                observed_mark=float(observed_mark),
                evidence_source=mark_source or "authenticated_observation",
            )
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
            prior_venue_order_ids = {
                str(value)
                for value in (
                    ((active[1].get("metadata") or {}).get("native_venue_order_id_lineage") or ())
                    if active is not None
                    else ()
                )
                if str(value)
            }
            if active is not None:
                active_metadata = active[1].get("metadata") or {}
                prior_venue_order_ids.update(
                    str(value) for value in (active_metadata.get("superseded_venue_order_ids") or ()) if str(value)
                )
                installed_venue_order_id = str(active_metadata.get("venue_order_id") or "")
                if installed_venue_order_id:
                    prior_venue_order_ids.add(installed_venue_order_id)
            prior_observed_order_id = self.observed_native_order_ids.get(symbol, "")
            if prior_observed_order_id:
                prior_venue_order_ids.add(prior_observed_order_id)
            # Bybit Full-position TP/SL updates modify an existing system order
            # and do not promise a new orderId, so clear the binding until
            # REST/WS proves the post-update row while keeping the lineage.
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
                        # Prior revisions, not an authentication blacklist: Bybit
                        # may reuse one for the active Full stop. The historical
                        # key is retained for replay compatibility.
                        "superseded_venue_order_ids": sorted(prior_venue_order_ids),
                        "native_venue_order_id_lineage": sorted(prior_venue_order_ids),
                        "symbol": symbol,
                        "signed_qty": plan.signed_qty,
                        "stop_source": plan.stop_source,
                        "target_keys": list(plan.target_keys),
                        "trigger_by": "MarkPrice",
                    },
                )
            self.last_sync_ns = observed_ns
            self.last_sync_ns_by_symbol[symbol] = observed_ns
            self._breaches.pop(symbol, None)
            self.last_error = ""
            return plan
        except Exception as exc:
            rejection_mark = _crossed_stop_rejection_mark(
                exc,
                requested_stop=plan.stop_price,
            )
            rejection_crossed = rejection_mark is not None and (
                (plan.signed_qty > 0.0 and plan.stop_price >= rejection_mark)
                or (plan.signed_qty < 0.0 and plan.stop_price <= rejection_mark)
            )
            if rejection_crossed and rejection_mark is not None:
                raise self._record_breach(
                    plan,
                    observed_mark=rejection_mark,
                    evidence_source="bybit_set_trading_stop_rejection",
                    detail=(
                        f"{symbol} native stop {plan.stop_price:g} was rejected as already "
                        f"crossed at Bybit base price {rejection_mark:g}"
                    ),
                ) from exc
            self.last_error = f"native protection sync failed for {symbol}: {exc}"[:1000]
            raise

    @_serialized_manager_method
    def verify_entry_attached_stop(
        self,
        *,
        symbol: str,
        expected_stop_price: float,
        command_id: str,
        attempts: int = 3,
        acknowledged_ts_ns: int = 0,
    ) -> str:
        """Prove the venue actually applied an entry-attached stop.

        Bybit documents that a Market order carrying ``stopLoss`` arms the
        position atomically, but that is a venue promise: if it is dropped, the
        position opens unprotected. This reads position truth back immediately
        and returns one of:

        ``armed``
            The venue reports the exact stop we sent. Nothing to do.
        ``repaired``
            The stop was absent or different and ``set_trading_stop`` installed
            it right here. Reconciliation binds it into the journal next pass.
        ``breached``
            The stop is absent and could not be installed. Latched; the position
            gets flattened through the existing software-flat pipeline.
        ``position_not_visible``
            No position at the venue yet, so there is nothing unprotected. Left
            to reconciliation, which is why ``last_error`` is set: health
            degrades and no new exposure is admitted until a pass confirms it.
        ``unreadable``
            Position truth could not be read at all. Fails closed the same way.

        The stop price is the one the command already carried and the kernel
        already validated; the entry is never resent.
        """

        symbol = symbol.upper()
        stop_price = float(expected_stop_price)
        if not math.isfinite(stop_price) or stop_price <= 0.0:
            raise ValueError("entry-attached stop verification requires a positive stop price")
        rule = self.rules.get(symbol)
        tolerance = max(rule.tick_size / 2.0 if rule is not None else 0.0, 1e-12)
        rounds = max(int(attempts), 1)
        read_error = ""
        # The venue pushes the position, stop included, within milliseconds of
        # the create. A pushed row observed strictly AFTER this order was
        # acknowledged answers the same question as the read below, from the
        # same field, without the round trip -- and usually two, since Bybit
        # lags between accepting an order and making the position readable.
        # Anything older than the acknowledgement says nothing about this
        # order, so it is refused and the REST path runs exactly as before.
        pushed_row = self._pushed_position_row(symbol, acknowledged_ts_ns=acknowledged_ts_ns)
        if pushed_row is not None:
            verdict = self._verdict_from_position_row(
                pushed_row,
                symbol=symbol,
                command_id=command_id,
                stop_price=stop_price,
                tolerance=tolerance,
            )
            if verdict is not None:
                return verdict
        for attempt in range(rounds):
            try:
                rows = self.client.get_positions(symbol=symbol)
            except Exception as exc:  # noqa: BLE001 - a read failure must not orphan the ACK
                read_error = f"{type(exc).__name__}: {exc}"
                continue
            row = self._open_position_row(rows, symbol)
            if row is None:
                # Bybit can lag between the create acknowledgement and the
                # position becoming readable. Only the last attempt concludes.
                if attempt + 1 < rounds:
                    continue
                self.last_error = (
                    f"{symbol} entry-attached stop is unverified: command {command_id} "
                    "was accepted but no venue position is readable yet"
                )[:1000]
                return "position_not_visible"
            venue_stop = _optional_float(row.get("stopLoss") or row.get("stop_loss"))
            if venue_stop is not None and abs(venue_stop - stop_price) <= tolerance:
                self._unarmed_entries.pop(symbol, None)
                return "armed"
            venue_mark = _optional_float(row.get("markPrice") or row.get("mark_price"))
            return self._repair_unapplied_entry_stop(
                symbol=symbol,
                command_id=command_id,
                stop_price=stop_price,
                observed_stop=venue_stop,
                observed_mark=venue_mark,
            )
        self.last_error = (
            f"{symbol} entry-attached stop is unverified: command {command_id} "
            f"was accepted but position truth is unreadable ({read_error or 'no response'})"
        )[:1000]
        return "unreadable"

    def _pushed_position_row(
        self,
        symbol: str,
        *,
        acknowledged_ts_ns: int,
    ) -> Mapping[str, Any] | None:
        """An open pushed position row for this order, or None to read REST."""

        cache = self.position_stream_cache
        if cache is None or acknowledged_ts_ns <= 0:
            return None
        try:
            # A market order is acknowledged before it fills, and the position
            # does not exist until it does -- which is exactly why the REST
            # verifier had to retry. So wait for the push rather than ask for
            # what has not happened yet: the venue takes milliseconds, a round
            # trip takes ~175 ms, and the retry took two.
            row = cache.wait_for(
                symbol,
                after_ts_ns=acknowledged_ts_ns,
                timeout_seconds=self.entry_stop_push_wait_seconds,
            )
        except Exception:  # noqa: BLE001 - an accelerator must never break the verifier
            return None
        if row is None:
            return None
        return row if self._open_position_row([row], symbol) is not None else None

    def _verdict_from_position_row(
        self,
        row: Mapping[str, Any],
        *,
        symbol: str,
        command_id: str,
        stop_price: float,
        tolerance: float,
    ) -> str | None:
        """The same verdict the REST loop reaches from the same two fields."""

        venue_stop = _optional_float(row.get("stopLoss") or row.get("stop_loss"))
        if venue_stop is not None and abs(venue_stop - stop_price) <= tolerance:
            self._unarmed_entries.pop(symbol, None)
            return "armed"
        return self._repair_unapplied_entry_stop(
            symbol=symbol,
            command_id=command_id,
            stop_price=stop_price,
            observed_stop=venue_stop,
            observed_mark=_optional_float(row.get("markPrice") or row.get("mark_price")),
        )

    def _repair_unapplied_entry_stop(
        self,
        *,
        symbol: str,
        command_id: str,
        stop_price: float,
        observed_stop: float | None,
        observed_mark: float | None,
    ) -> str:
        """Install the declared stop the venue did not apply, or latch a breach."""

        observed_text = "absent" if observed_stop is None else f"{observed_stop:g}"
        detail = (
            f"{symbol} entry-attached stop {stop_price:g} was not applied by the venue "
            f"for command {command_id} (venue reports {observed_text})"
        )
        try:
            self.client.set_trading_stop(
                symbol=symbol,
                tpsl_mode="Full",
                position_idx=0,
                stop_loss=_decimal_text(stop_price),
                take_profit="0",
                sl_trigger_by="MarkPrice",
                tp_trigger_by=None,
            )
        except Exception as exc:  # noqa: BLE001 - every failure here is fail-closed
            rejection_mark = _crossed_stop_rejection_mark(exc, requested_stop=stop_price)
            mark = rejection_mark if rejection_mark is not None else observed_mark
            self._latch_unarmed_entry(
                symbol=symbol,
                command_id=command_id,
                stop_price=stop_price,
                observed_mark=mark,
                detail=f"{detail}; repair failed: {type(exc).__name__}: {exc}",
                evidence_source=(
                    "bybit_post_create_stop_repair_rejected_as_crossed"
                    if rejection_mark is not None
                    else "bybit_post_create_stop_repair_failed"
                ),
            )
            return "breached"
        _logger.warning("%s; installed it directly instead", detail)
        self._unarmed_entries.pop(symbol, None)
        self.last_error = ""
        return "repaired"

    def _latch_unarmed_entry(
        self,
        *,
        symbol: str,
        command_id: str,
        stop_price: float,
        observed_mark: float | None,
        detail: str,
        evidence_source: str,
    ) -> None:
        self._unarmed_entries[symbol] = {
            "command_id": command_id,
            "stop_price": stop_price,
            "observed_mark": observed_mark,
            "detail": detail[:1000],
            "evidence_source": evidence_source,
            "observed_ts_ns": self.clock.wall_time_ns(),
        }
        self.last_error = detail[:1000]
        _logger.error("%s", detail)

    @staticmethod
    def _open_position_row(
        rows: Any,
        symbol: str,
    ) -> Mapping[str, Any] | None:
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            if str(row.get("symbol") or "").upper() != symbol:
                continue
            size = _optional_float(row.get("size"))
            if size is not None and size > 0.0:
                return row
        return None

    def _resolve_unarmed_entry(
        self,
        symbol: str,
        *,
        venue_row: Mapping[str, Any] | None,
    ) -> None:
        """Clear or escalate an unarmed-entry latch now that a plan may exist."""

        latch = self._unarmed_entries.get(symbol)
        if latch is None:
            return
        stop_price = float(latch["stop_price"])
        rule = self.rules.get(symbol)
        tolerance = max(rule.tick_size / 2.0 if rule is not None else 0.0, 1e-12)
        venue_stop = (
            None
            if venue_row is None
            else _optional_float(venue_row.get("stopLoss") or venue_row.get("stop_loss"))
        )
        if venue_stop is not None and abs(venue_stop - stop_price) <= tolerance:
            # The stop landed late; clear rather than flatten a live book.
            self._unarmed_entries.pop(symbol, None)
            self.last_error = ""
            return
        plan = self.plan(symbol)
        if plan is None:
            # Still no journal position; keep the latch rather than discard
            # evidence that the venue dropped a declared stop.
            self.last_error = str(latch["detail"])[:1000]
            return
        observed_mark = latch.get("observed_mark")
        if observed_mark is None and venue_row is not None:
            observed_mark = _optional_float(
                venue_row.get("markPrice") or venue_row.get("mark_price")
            )
        if observed_mark is None:
            self.last_error = str(latch["detail"])[:1000]
            return
        self._unarmed_entries.pop(symbol, None)
        raise self._record_breach(
            plan,
            observed_mark=float(observed_mark),
            evidence_source=str(latch["evidence_source"]),
            detail=str(latch["detail"]),
        )

    @_serialized_manager_method
    def reconcile_venue_positions(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        skip_symbols: AbstractSet[str] = frozenset(),
    ) -> None:
        """Verify every reconstructed open position's native stop from REST truth.

        A local ``active`` event is not proof that the exchange still owns the
        stop. The position snapshot carries Bybit's current ``stopLoss``; a
        missing or different value is repaired before health is refreshed.

        ``skip_symbols`` names symbols whose journal position cannot be read
        against the venue (quantity mismatch, dual-side row, ambiguous in-flight
        submission). Their stop plan would derive from a quantity known to be
        wrong, so they are left untouched and not marked fresh —
        ``require_recent_healthy`` still fails them on age. Every other symbol is
        verified normally.
        """

        skipped = {str(symbol).upper() for symbol in skip_symbols}
        venue_rows: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").lower()
            size = _optional_float(row.get("size"))
            if symbol and side in {"buy", "sell"} and size is not None:
                venue_rows[symbol] = row

        state = self.kernel._state_ref()
        failures: list[tuple[str, BaseException]] = []
        # A latched unarmed entry can name a symbol the journal has no position
        # for yet, so resolve it independently of the position walk below.
        for symbol in sorted(set(self._unarmed_entries) - skipped):
            try:
                self._resolve_unarmed_entry(symbol, venue_row=venue_rows.get(symbol))
            except Exception as exc:  # noqa: BLE001 - escalate, do not abort the sweep
                failures.append((symbol, exc))
        for symbol, position in sorted(state.positions.items()):
            if symbol in skipped:
                continue
            try:
                if position.signed_qty == 0.0:
                    venue_row = venue_rows.get(symbol)
                    venue_size = (
                        None
                        if venue_row is None
                        else _optional_float(venue_row.get("size"))
                    )
                    if venue_size is not None and venue_size > 1e-12:
                        raise RuntimeError(
                            f"{symbol} reconstructed flat contradicts authenticated "
                            f"venue size {venue_size:g}"
                        )
                    self.sync(
                        symbol,
                        authenticated_position_flat=True,
                        flat_evidence_source="bybit_authenticated_position_snapshot",
                    )
                    continue
                plan = self.plan(symbol)
                if plan is None:  # pragma: no cover - guarded by signed_qty above
                    raise RuntimeError(f"{symbol} cannot derive native protection plan")
                venue_row = venue_rows.get(symbol)
                if venue_row is None:
                    raise RuntimeError(f"{symbol} open reconstructed position is absent from venue snapshot")
                venue_stop = _optional_float(venue_row.get("stopLoss") or venue_row.get("stop_loss"))
                venue_mark = _optional_float(venue_row.get("markPrice") or venue_row.get("mark_price"))
                rule = self.rules[symbol]
                tolerance = max(rule.tick_size / 2.0, 1e-12)
                active = self.active(symbol)
                local_matches = active is not None and self._plan_key(active) == plan.protection_key
                venue_matches = venue_stop is not None and abs(venue_stop - plan.stop_price) <= tolerance
                if venue_matches:
                    unresolved_breach = self._breaches.get(symbol)
                    if unresolved_breach is not None and unresolved_breach.plan.protection_key != plan.protection_key:
                        raise NativeProtectionBreachError(unresolved_breach)
                    if not local_matches:
                        self._adopt_verified_venue_stop(plan, venue_mark=venue_mark)
                    else:
                        observed_ns = self.clock.wall_time_ns()
                        self._breaches.pop(symbol, None)
                        self.last_sync_ns = observed_ns
                        self.last_sync_ns_by_symbol[symbol] = observed_ns
                        self.last_error = ""
                    continue
                if venue_mark is None:
                    raise RuntimeError(
                        f"{symbol} venue snapshot cannot repair missing or mismatched native stop without markPrice"
                    )
                self.sync(
                    symbol,
                    force=True,
                    observed_mark=venue_mark,
                    mark_source="authenticated_position_snapshot",
                )
            except Exception as exc:  # noqa: BLE001 - attempt every open symbol
                failures.append((symbol, exc))
        if failures:
            error = NativeProtectionReconciliationError(failures, operation="reconciliation")
            self.last_error = str(error)
            raise error

    @_serialized_manager_method
    def sync_symbols(self, symbols: Sequence[str]) -> tuple[NativeProtectionPlan, ...]:
        plans = []
        failures: list[tuple[str, BaseException]] = []
        for symbol in sorted(set(symbols)):
            try:
                plan = self.sync(symbol)
                if plan is not None:
                    plans.append(plan)
            except Exception as exc:  # noqa: BLE001 - one symbol must not starve siblings
                failures.append((symbol, exc))
        if failures:
            error = NativeProtectionReconciliationError(failures, operation="sync")
            self.last_error = str(error)
            raise error
        return tuple(plans)

    @_serialized_manager_method
    def adopt_execution(
        self,
        row: Mapping[str, Any],
        *,
        local_receive_ts_ns: int,
        message_creation_ts_ns: int = 0,
    ) -> tuple[Any, ...]:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            raise AccountTransitionError("external execution lacks a symbol")
        active = self.active(symbol)
        identity_evidence = self.native_execution_identity_evidence(row)
        if identity_evidence == "matched_entry_attached_native_stop":
            candidate = self._entry_attached_stop_candidate(symbol, row=row)
            if candidate is None:  # pragma: no cover - identity helper proved it
                raise AccountTransitionError("entry-attached native execution lost its durable command")
            active = self._activate_entry_attached_stop(
                candidate,
                venue_order_id=str(row.get("orderId") or row.get("order_id") or ""),
                observed_ts_ns=local_receive_ts_ns,
            )
        side = str(row.get("side") or "").lower()
        qty = _required_float(row.get("execQty") or row.get("exec_qty"), "execQty")
        signed_qty = qty if side == "buy" else -qty if side == "sell" else 0.0
        execution_id = str(row.get("execId") or row.get("exec_id") or "")
        if not execution_id or signed_qty == 0.0:
            raise AccountTransitionError("external execution lacks an execution id or side")
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if not venue_order_id:
            raise AccountTransitionError("external execution lacks venue orderId")
        # The venue nets one position per symbol, so its executions include
        # trades against exposure this book never opened. An execution with
        # nothing here to reduce is that foreign exposure moving, not an
        # adoption failure: REST recovery re-offers every unadopted row each
        # pass, and raising turned one manual close into a permanent error loop
        # (ACEUSDT, 2026-08-07, ~250 failures a minute for four hours).
        owned = self.kernel._state_ref().positions.get(symbol)
        owned_qty = owned.signed_qty if owned is not None else 0.0
        if owned_qty == 0.0 or owned_qty * signed_qty >= 0.0:
            _logger.info(
                "ignoring foreign %s execution %s (%+g) with no owned position to reduce",
                symbol,
                execution_id,
                signed_qty,
            )
            return ()
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
        protection_key = active[0] if active is not None else f"external-reduction:{symbol}:{venue_order_id}"
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
                    **bybit_private_execution_metadata(
                        row,
                        message_creation_ts_ns=message_creation_ts_ns,
                    ),
                    "source": "bybit_private_execution_ws",
                    "external_order_link_id": str(row.get("orderLinkId") or row.get("order_link_id") or ""),
                    "exec_type": str(row.get("execType") or row.get("exec_type") or ""),
                    "create_type": str(row.get("createType") or row.get("create_type") or ""),
                    "stop_order_type": str(row.get("stopOrderType") or row.get("stop_order_type") or ""),
                    "native_identity": identity_evidence,
                },
            )
        except AccountTransitionError as exc:
            self.last_error = (f"external execution could not be adopted for {symbol}: {exc}")[:1000]
            raise

    @staticmethod
    def is_position_execution(row: Mapping[str, Any]) -> bool:
        """Exclude cash-flow-only private execution rows from position adoption."""

        exec_type = str(row.get("execType") or row.get("exec_type") or "").lower().replace("_", "")
        return exec_type != "funding"

    @staticmethod
    def has_native_stop_provenance(row: Mapping[str, Any]) -> bool:
        """Identify provider-authored stop rows before client-id resolution."""

        return _has_native_stop_provenance(row)

    @_serialized_manager_method
    def note_adoption_failure(
        self,
        row: Mapping[str, Any],
        exc: AccountTransitionError,
    ) -> None:
        symbol = str(row.get("symbol") or "missing-symbol").upper()
        execution_id = str(row.get("execId") or row.get("exec_id") or "missing-exec-id")
        self.last_error = (f"unaccounted position execution for {symbol} execId={execution_id}: {exc}")[:1000]

    def _entry_attached_stop_candidates(
        self,
        symbol: str,
    ) -> tuple[_EntryAttachedStopCandidate, ...]:
        """Return durable filled-entry stop candidates, newest command first."""

        symbol = symbol.upper()
        state = self.kernel._state_ref()
        position = state.positions.get(symbol)
        if position is None or abs(position.signed_qty) <= 1e-12:
            return ()
        candidates: list[_EntryAttachedStopCandidate] = []
        for order in state.orders.values():
            if (
                order.symbol.upper() != symbol
                or order.reduce_only
                or order.entry_stop_price is None
                or order.entry_stop_fraction is None
                or not order.entry_stop_source
                or order.entry_stop_trigger_by != "MarkPrice"
                or order.filled_signed_qty * position.signed_qty <= 0.0
                or order.status in {"rejected", "cancelled"}
            ):
                continue
            candidates.append(
                _EntryAttachedStopCandidate(
                    command_id=order.command_id,
                    symbol=symbol,
                    signed_qty=position.signed_qty,
                    stop_price=float(order.entry_stop_price),
                    stop_fraction=float(order.entry_stop_fraction),
                    stop_source=order.entry_stop_source,
                    trigger_by=order.entry_stop_trigger_by,
                    created_ts_ns=int(order.created_ts_ns),
                    command_sequence=int(order.command_sequence),
                )
            )
        return tuple(sorted(
            candidates,
            key=lambda item: (
                item.command_sequence,
                item.created_ts_ns,
                item.command_id,
            ),
            reverse=True,
        ))

    def _entry_attached_stop_candidate(
        self,
        symbol: str,
        *,
        row: Mapping[str, Any] | None = None,
    ) -> _EntryAttachedStopCandidate | None:
        """Return the newest candidate, or the exact candidate matching a row."""

        candidates = self._entry_attached_stop_candidates(symbol)
        if row is None:
            return candidates[0] if candidates else None
        return next(
            (
                candidate
                for candidate in candidates
                if self._row_matches_entry_attached_stop(
                    row,
                    candidate=candidate,
                )
            ),
            None,
        )

    def _row_matches_entry_attached_stop(
        self,
        row: Mapping[str, Any],
        *,
        candidate: _EntryAttachedStopCandidate,
    ) -> bool:
        if not _has_full_native_stop_provenance(row):
            return False
        order_link_id = str(row.get("orderLinkId") or row.get("order_link_id") or "")
        if order_link_id and order_link_id != candidate.command_id:
            return False
        side = str(row.get("side") or "").lower()
        expected_side = "sell" if candidate.signed_qty > 0.0 else "buy"
        if side in {"buy", "sell"} and side != expected_side:
            return False
        raw_position_idx = row.get("positionIdx")
        if raw_position_idx is None:
            raw_position_idx = row.get("position_idx")
        if raw_position_idx not in (None, ""):
            assert raw_position_idx is not None
            try:
                if int(raw_position_idx) != 0:
                    return False
            except (TypeError, ValueError):
                return False
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if venue_order_id:
            observed_command = self.observed_entry_stop_order_ids.get(
                candidate.symbol,
                {},
            ).get(venue_order_id)
            if observed_command:
                return observed_command == candidate.command_id
        trigger = _optional_float(
            row.get("triggerPrice") or row.get("trigger_price") or row.get("stopLoss") or row.get("stop_loss")
        )
        rule = self.rules.get(candidate.symbol)
        return bool(
            trigger is not None
            and rule is not None
            and abs(trigger - candidate.stop_price) <= max(rule.tick_size / 2.0, 1e-12)
        )

    def _activate_entry_attached_stop(
        self,
        candidate: _EntryAttachedStopCandidate,
        *,
        venue_order_id: str,
        observed_ts_ns: int,
    ) -> tuple[str, Mapping[str, Any]]:
        """Journal authenticated proof of the stop attached to an entry."""

        active = self.active(candidate.symbol)
        base_key = f"native-entry-attached:{candidate.symbol}:{candidate.command_id}"
        active_metadata = active[1].get("metadata") or {} if active is not None else {}
        same_candidate = bool(
            active is not None
            and str(active_metadata.get("entry_command_id") or "")
            == candidate.command_id
            and _optional_float(active[1].get("stop_price"))
            == candidate.stop_price
        )
        recorded_venue_order_id = str(active_metadata.get("venue_order_id") or "")
        if same_candidate and (
            not venue_order_id or recorded_venue_order_id == venue_order_id
        ):
            assert active is not None
            if venue_order_id:
                self.observed_entry_stop_order_ids.setdefault(
                    candidate.symbol,
                    {},
                )[venue_order_id] = candidate.command_id
                self.observed_native_order_ids[candidate.symbol] = venue_order_id
            return active
        state = self.kernel._state_ref()
        target_keys = sorted(
            key
            for key, target in state.component_targets.items()
            if str(target.get("symbol") or "").upper() == candidate.symbol
            and float(target.get("signed_qty") or 0.0) * candidate.signed_qty > 0.0
        )
        revisions = [
            int((protection.get("metadata") or {}).get("activation_revision") or 1)
            for key, protection in state.protections.items()
            if key == base_key
            or str((protection.get("metadata") or {}).get("protection_plan_key") or "")
            == base_key
        ]
        activation_revision = max(revisions, default=0) + 1
        protection_key = (
            base_key
            if activation_revision == 1
            else f"{base_key}:activation:{activation_revision:04d}"
        )
        prior_venue_order_ids: set[str] = set()
        if active is not None:
            prior_metadata = active[1].get("metadata") or {}
            prior_venue_order_ids.update(
                str(value)
                for value in (
                    prior_metadata.get("native_venue_order_id_lineage") or ()
                )
                if str(value)
            )
            prior_venue_order_ids.update(
                str(value)
                for value in (prior_metadata.get("superseded_venue_order_ids") or ())
                if str(value)
            )
            prior_bound = str(prior_metadata.get("venue_order_id") or "")
            if prior_bound:
                prior_venue_order_ids.add(prior_bound)
            prior_observed = self.observed_native_order_ids.get(
                candidate.symbol,
                "",
            )
            if prior_observed:
                prior_venue_order_ids.add(prior_observed)
            self.kernel.record_protection(
                protection_key=active[0],
                symbol=candidate.symbol,
                status="replaced",
                stop_price=_optional_float(active[1].get("stop_price")),
                take_profit_price=_optional_float(
                    active[1].get("take_profit_price")
                ),
                exchange_ts_ns=0,
                local_receive_ts_ns=max(int(observed_ts_ns), 1),
                command_id=str(active[1].get("command_id") or ""),
                metadata={
                    **dict(prior_metadata),
                    "replaced_by": protection_key,
                },
            )
        self.kernel.record_protection(
            protection_key=protection_key,
            symbol=candidate.symbol,
            status="active",
            stop_price=candidate.stop_price,
            take_profit_price=None,
            exchange_ts_ns=0,
            local_receive_ts_ns=max(int(observed_ts_ns), 1),
            command_id=candidate.command_id,
            metadata={
                "native_exchange": True,
                "protection_plan_key": base_key,
                "activation_revision": activation_revision,
                "superseded_venue_order_ids": sorted(prior_venue_order_ids),
                "native_venue_order_id_lineage": sorted(prior_venue_order_ids),
                "symbol": candidate.symbol,
                "signed_qty": candidate.signed_qty,
                "stop_source": candidate.stop_source,
                "stop_fraction": candidate.stop_fraction,
                "target_keys": target_keys,
                "trigger_by": candidate.trigger_by,
                "venue_order_id": venue_order_id,
                "entry_attached_provisional": True,
                "entry_command_id": candidate.command_id,
                "authenticated_entry_stop_order": True,
            },
        )
        if venue_order_id:
            self.observed_entry_stop_order_ids.setdefault(
                candidate.symbol,
                {},
            )[venue_order_id] = candidate.command_id
            self.observed_native_order_ids[candidate.symbol] = venue_order_id
        self.last_sync_ns = max(int(observed_ts_ns), 1)
        self.last_sync_ns_by_symbol[candidate.symbol] = self.last_sync_ns
        self.last_error = ""
        activated = self.active(candidate.symbol)
        if activated is None:  # pragma: no cover - journal/reducer invariant
            raise RuntimeError("entry-attached native protection did not activate")
        return activated

    @_serialized_manager_method
    def observe_order(self, row: Mapping[str, Any]) -> bool:
        """Remember a verified native order id before its first execution arrives."""

        symbol = str(row.get("symbol") or "").upper()
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if not symbol or not venue_order_id:
            return False
        active_before = self.active(symbol)
        matches_active = bool(
            active_before is not None
            and self._row_matches_native_protection(
                row,
                payload=active_before[1],
                observed=self.observed_native_order_ids.get(symbol, ""),
                venue_order_id=venue_order_id,
                symbol=symbol,
            )
        )
        candidate = self._entry_attached_stop_candidate(symbol, row=row)
        matches_entry = candidate is not None
        if not matches_active and not matches_entry:
            return False
        if matches_entry:
            assert candidate is not None
            self.observed_entry_stop_order_ids.setdefault(symbol, {})[
                venue_order_id
            ] = candidate.command_id
            if not matches_active:
                self._activate_entry_attached_stop(
                    candidate,
                    venue_order_id=venue_order_id,
                    observed_ts_ns=self.clock.wall_time_ns(),
                )
                matches_active = True
        if matches_active:
            self.observed_native_order_ids[symbol] = venue_order_id
        raw_status = str(row.get("orderStatus") or row.get("order_status") or "").lower().replace("_", "")
        cumulative = _finite_or_zero(row.get("cumExecQty") or row.get("cum_exec_qty"))
        if raw_status in {"cancelled", "canceled", "deactivated", "rejected"} and (cumulative <= 0.0):
            active = self.active(symbol)
            if active is not None and matches_active:
                prior = active[1]
                status = "rejected_unfilled" if raw_status == "rejected" else "cancelled_unfilled"
                self.kernel.record_protection(
                    protection_key=active[0],
                    symbol=symbol,
                    status=status,
                    stop_price=_optional_float(prior.get("stop_price")),
                    take_profit_price=_optional_float(prior.get("take_profit_price")),
                    exchange_ts_ns=_timestamp_ns(row.get("updatedTime") or row.get("updated_time")),
                    local_receive_ts_ns=self.clock.wall_time_ns(),
                    metadata={
                        **dict(prior.get("metadata") or {}),
                        "venue_order_id": venue_order_id,
                        "terminal_order_status": str(row.get("orderStatus") or ""),
                    },
                )
                self.last_error = (
                    f"native protection {status.replace('_', ' ')} for {symbol}: orderId={venue_order_id}"
                )[:1000]
        elif raw_status in {"cancelled", "canceled", "deactivated", "filled"} and (cumulative > 0.0):
            self.last_error = (
                f"native protection terminal execution recovery pending for {symbol}: "
                f"orderId={venue_order_id} cumulative_qty={cumulative:g}"
            )[:1000]
        return True

    @_serialized_manager_method
    def observe_terminal_status(self, *, command_id: str, status: str) -> None:
        """Persist loss of a partially-filled native stop's working remainder."""

        if status != "partially_filled_cancelled":
            state = self.kernel._state_ref()
            order = state.orders.get(command_id)
            position = state.positions.get(order.symbol) if order is not None else None
            if order is not None and (position is None or abs(position.signed_qty) <= 1e-12):
                # Only the message this symbol's own flatness disproves.
                # ``last_error`` is one account-wide field written by several
                # unrelated conditions — an adoption failure, an entry whose
                # stop could not be verified, a pending native-terminal
                # recovery — and it gates all new exposure. Blanking it here
                # let any terminal status on any FLAT symbol clear a live
                # warning about a DIFFERENT symbol that still holds an
                # unproven stop, and health then passed.
                symbol = str(order.symbol).upper()
                if symbol and symbol in self.last_error.upper():
                    self.last_error = ""
            return
        state = self.kernel._state_ref()
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

        state = self.kernel._state_ref()
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
            and str(protection.get("status") or "") in {"triggering", "external_reduction_partial"}
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
        if not symbol or not _has_native_stop_provenance(row):
            return False
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if not venue_order_id:
            return False
        active = self.active(symbol)
        if active is not None:
            if self._row_matches_native_protection(
                row,
                payload=active[1],
                observed=self.observed_native_order_ids.get(symbol, ""),
                venue_order_id=venue_order_id,
                symbol=symbol,
            ):
                return True
        candidate = self._entry_attached_stop_candidate(symbol, row=row)
        if candidate is not None:
            return True
        # A consumed Full-position stop can stay visible in Bybit's open-order
        # queries for minutes after its protection record leaves the
        # {active, triggering} statuses, so within a bounded grace window such
        # rows are verified against the latest native protection record. This
        # manager only creates Full-position stops, so partial-stop provenance is
        # never the lingering row. Where any identity evidence exists (recorded
        # venue id, lineage, or the in-memory observed id) the row must match it;
        # the trigger-price fallback covers a stop consumed before its venue id
        # was observable. The record's exchange time is bounded by the same
        # window, so an owner-downtime recovery cannot reopen a dead one.
        if not _has_full_native_stop_provenance(row):
            return False
        latest = self._latest_native_protection_from_state(self.kernel._state_ref(), symbol)
        if latest is None:
            return False
        payload = latest[1]
        recorded_ns = int(payload.get("local_receive_ts_ns") or 0)
        if recorded_ns <= 0:
            return False
        now_ns = self.clock.wall_time_ns()
        age_ns = now_ns - recorded_ns
        if age_ns < 0 or age_ns > NATIVE_TERMINAL_ORDER_VISIBILITY_GRACE_NS:
            return False
        exchange_ns = int(payload.get("exchange_ts_ns") or 0)
        if exchange_ns > 0 and (now_ns - exchange_ns > NATIVE_TERMINAL_ORDER_VISIBILITY_GRACE_NS):
            return False
        return self._row_matches_native_protection(
            row,
            payload=payload,
            observed=self.observed_native_order_ids.get(symbol, ""),
            venue_order_id=venue_order_id,
            symbol=symbol,
            require_identity_when_present=True,
        )

    def _row_matches_native_protection(
        self,
        row: Mapping[str, Any],
        *,
        payload: Mapping[str, Any],
        observed: str,
        venue_order_id: str,
        symbol: str,
        require_identity_when_present: bool = False,
    ) -> bool:
        """Identity contract shared by the active and terminal-grace paths."""

        metadata = payload.get("metadata") or {}
        expected = str(metadata.get("venue_order_id") or "")
        lineage = {str(value) for value in (metadata.get("native_venue_order_id_lineage") or ()) if str(value)}
        lineage.update(str(value) for value in (metadata.get("superseded_venue_order_ids") or ()) if str(value))
        if venue_order_id == expected and expected:
            return True
        if venue_order_id == observed and observed:
            return True
        trigger = _optional_float(row.get("triggerPrice") or row.get("trigger_price"))
        stop_price = _optional_float(payload.get("stop_price"))
        rule = self.rules.get(symbol)
        if trigger is None or stop_price is None or rule is None:
            return False
        trigger_matches = abs(trigger - stop_price) <= max(rule.tick_size / 2.0, 1e-12)
        if venue_order_id in lineage:
            if (expected and venue_order_id != expected) or (observed and venue_order_id != observed):
                return False
            return trigger_matches
        if expected or observed:
            return False
        if require_identity_when_present and lineage:
            return False
        return trigger_matches

    @staticmethod
    def _latest_native_protection_from_state(
        state: AccountState,
        symbol: str,
    ) -> tuple[str, Mapping[str, Any]] | None:
        index = _native_protection_index(state)
        if index is not None:
            matches = index.get(symbol, ())
        else:
            matches = [
                (key, protection)
                for key, protection in state.protections.items()
                if bool((protection.get("metadata") or {}).get("native_exchange"))
                and str((protection.get("metadata") or {}).get("symbol") or symbol).upper() == symbol
            ]
        if not matches:
            return None
        return max(
            matches,
            key=lambda item: int(item[1].get("local_receive_ts_ns") or 0),
        )

    @_serialized_manager_method
    def native_execution_identity_evidence(self, row: Mapping[str, Any]) -> str:
        symbol = str(row.get("symbol") or "").upper()
        active = self.active(symbol)
        if not symbol:
            return ""
        venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
        if not venue_order_id:
            return ""
        expected = ""
        observed = ""
        if active is not None:
            metadata = active[1].get("metadata") or {}
            expected = str(metadata.get("venue_order_id") or "")
            observed = self.observed_native_order_ids.get(symbol, "")
            if venue_order_id == expected and expected:
                return "matched_installed_venue_order_id"
            if venue_order_id == observed and observed:
                return "matched_verified_native_order_event"
            lineage = {str(value) for value in (metadata.get("native_venue_order_id_lineage") or ()) if str(value)}
            lineage.update(str(value) for value in (metadata.get("superseded_venue_order_ids") or ()) if str(value))
            if venue_order_id in lineage and _has_native_stop_provenance(row):
                return "matched_known_native_order_lineage"
        candidate = self._entry_attached_stop_candidate(symbol, row=row)
        if candidate is not None:
            return "matched_entry_attached_native_stop"
        if expected or observed:
            return ""
        return "bybit_stop_provenance_unbound" if _has_native_stop_provenance(row) else ""

    @_serialized_manager_method
    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        if self._breaches:
            raise RuntimeError(
                "native disaster stop breached without venue protection: "
                + "; ".join(self._breaches[symbol].detail for symbol in sorted(self._breaches))
            )
        if self._unarmed_entries:
            raise RuntimeError(
                "venue did not apply an entry-attached stop: "
                + "; ".join(
                    str(self._unarmed_entries[symbol]["detail"])
                    for symbol in sorted(self._unarmed_entries)
                )
            )
        if self.last_error:
            raise RuntimeError(self.last_error)
        state = self.kernel._state_ref()
        open_symbols = [symbol for symbol, position in state.positions.items() if position.signed_qty != 0.0]
        active_by_symbol = {symbol: self.active(symbol) for symbol in open_symbols}
        missing = [symbol for symbol, active in active_by_symbol.items() if active is None]
        if missing:
            raise RuntimeError(
                "open reconstructed positions lack active native disaster protection: " + ", ".join(sorted(missing))
            )
        triggering = [
            symbol
            for symbol, active in active_by_symbol.items()
            if active is not None and str(active[1].get("status") or "") == "triggering"
        ]
        if triggering:
            raise RuntimeError("native protection trigger is unresolved for " + ", ".join(sorted(triggering)))
        now = self.clock.wall_time_ns()
        stale = [
            symbol
            for symbol in open_symbols
            if self.last_sync_ns_by_symbol.get(symbol, 0) <= 0
            or now - self.last_sync_ns_by_symbol.get(symbol, 0) > max(int(max_age_ns), 1)
        ]
        if stale:
            raise RuntimeError("native protection health is stale for " + ", ".join(sorted(stale)))


def _decimal_text(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _optional_float(value: object) -> float | None:
    try:
        output = float(str(value))
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) and output > 0.0 else None


def _crossed_stop_rejection_mark(
    error: BaseException,
    *,
    requested_stop: float,
) -> float | None:
    """Parse only Bybit's definite crossed-StopLoss rejection shape.

    Narrow on purpose. The mark it returns latches `breached_unprotected` and
    flattens, so anything short of ErrCode 10001 carrying both `StopLoss` and
    `base_price` must fall through to the ordinary repair path instead. Bybit
    reports the pair as integer prices (`StopLoss:1291300000 ...
    base_price:1309440000`), which is why the caller normalises them.
    """

    message = str(error)
    lowered = message.lower()
    if (
        "10001" not in lowered
        or "stoploss" not in lowered
        or "base_price" not in lowered
        or not (
            "must be greater than" in lowered
            or "must be less than" in lowered
            or "should greater" in lowered
            or "should less" in lowered
            or "should be greater" in lowered
            or "should be less" in lowered
        )
    ):
        return None
    base_match = re.search(
        r"base_price(?:\[|:)([0-9eE+.-]+)",
        message,
        flags=re.IGNORECASE,
    )
    if base_match is None:
        return None
    base = _optional_float(base_match.group(1))
    stop_match = re.search(
        r"stoploss(?:\[|:)([0-9eE+.-]+)",
        message,
        flags=re.IGNORECASE,
    )
    encoded_stop = _optional_float(stop_match.group(1)) if stop_match is not None else None
    if base is None:
        return None
    # Pybit's InvalidRequestError string can expose Bybit's internal integer
    # price units instead of decimals. Normalize with the requested stop echoed
    # in the same error: the ratio is scale-independent.
    if encoded_stop is not None:
        return base * float(requested_stop) / encoded_stop
    scale = base / float(requested_stop)
    if scale > 1_000_000.0 or scale < 0.000001:
        return None
    return base


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
    stop_type = str(row.get("stopOrderType") or row.get("stop_order_type") or "").lower().replace("_", "")
    create_type = str(row.get("createType") or row.get("create_type") or "").lower().replace("_", "")
    return stop_type in {"stoploss", "partialstoploss"} or create_type in {
        "createbystoploss",
        "createbypartialstoploss",
    }


def _has_full_native_stop_provenance(row: Mapping[str, Any]) -> bool:
    """Full-position stop provenance only — the sole kind this manager creates."""

    stop_type = str(row.get("stopOrderType") or row.get("stop_order_type") or "").lower().replace("_", "")
    create_type = str(row.get("createType") or row.get("create_type") or "").lower().replace("_", "")
    return stop_type == "stoploss" or create_type == "createbystoploss"


def _external_execution_origin(row: Mapping[str, Any]) -> str:
    create_type = str(row.get("createType") or row.get("create_type") or "").lower().replace("_", "")
    exec_type = str(row.get("execType") or row.get("exec_type") or "").lower().replace("_", "")
    if exec_type == "adltrade":
        return "venue_adl"
    if create_type == "createbyliq" or exec_type == "busttrade":
        return "venue_liquidation"
    return "unattributed_external_reduction"
