"""Take one account to zero exposure through its own account owner.

Flatten publishes a zero replacement target for every component that still
holds exposure, then watches the journal until the owner has converged. It
places no order itself: every close is an ordinary owner-side reduce-only
command, so risk accounting, protection cleanup, and the journal read exactly
as they do for a strategy exit.

Zero targets are strictly risk-reducing, which is why this works when nothing
else does -- the kernel exempts a reducing batch from the capital, leverage,
freshness, and partition checks that would otherwise refuse an order on a
degraded account, and it retries a reduction without limit.

Two residuals are expected rather than failures:

``orphan``
    Exposure with no component owner. Publishing zeros makes every remaining
    position an orphan, and owner convergence drives an orphan to flat on its
    own. Flatten reports them; it does not need to name them.
``dust``
    A residual below the venue's minimum order size cannot be sold. The kernel
    journals a ``below_min_qty`` rejection for it, which flatten reads back and
    treats as terminal rather than waiting out the deadline. Only rejections
    from batches decided after this flatten began count, so an old rejection
    against a since-resized position cannot be mistaken for present dust.

Producers are not this module's business. A running producer may publish a new
nonzero target while flatten is converging, so flatten records the targets it
saw at plan time and reports any that reappeared. Stopping producers first is
the caller's job -- ``scripts/deploy_vps_live.sh flatten`` does it.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from liquidity_migration.account.account_intent_client import (
    AccountTargetPublisher,
    ExitFirstPublication,
    publish_exit_first_target_requests,
    requested_target,
)
from liquidity_migration.account.account_kernel import AccountJournalCursor, AccountState
from liquidity_migration.account.account_route import require_account_route
from liquidity_migration.account.account_service import SleeveAdapterKind
from liquidity_migration.account.execution_environment import (
    EXECUTION_ENVIRONMENT_CHOICES,
    account_id_for_environment,
    execution_environment,
)

__all__ = [
    "FlattenComponent",
    "FlattenOutcome",
    "FlattenPlan",
    "FlattenResidual",
    "build_flatten_plan",
    "flatten_account",
    "flatten_intents",
    "main",
    "observe_residual",
]

#: Quantity below which a signed size is treated as zero. Matches the tolerance
#: the rollout readiness proof uses, so a flatten that reports flat satisfies it.
QUANTITY_TOLERANCE = 1e-12

#: Kernel rejection reason for a residual no venue-admissible order can express.
#: Journaled in the RISK_DECISION payload as
#: ``account-risk:{batch_id}:below_min_qty:{SYMBOL}``.
DUST_REJECTION_REASON = "below_min_qty"

_DEFAULT_WAIT_SECONDS = 600.0
_DEFAULT_POLL_SECONDS = 5.0


class FlattenRefused(RuntimeError):
    """Flatten cannot proceed on the state it was given."""


@dataclass(frozen=True, slots=True)
class FlattenComponent:
    """One component target that still owns exposure."""

    target_key: str
    symbol: str
    sleeve: str
    strategy_id: str
    component_id: str
    signed_qty: float
    leverage: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_key": self.target_key,
            "symbol": self.symbol,
            "sleeve": self.sleeve,
            "strategy_id": self.strategy_id,
            "component_id": self.component_id,
            "signed_qty": self.signed_qty,
            "leverage": self.leverage,
        }


@dataclass(frozen=True, slots=True)
class FlattenPlan:
    """What flatten would publish, and what it cannot reach."""

    route_id: str
    environment: str
    account_id: str
    journal_sequence: int
    components: tuple[FlattenComponent, ...]
    orphan_positions: tuple[tuple[str, float], ...]
    skipped_components: tuple[FlattenComponent, ...] = ()

    @property
    def already_flat(self) -> bool:
        return not self.components and not self.orphan_positions

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "journal_sequence": self.journal_sequence,
            "components": [item.as_dict() for item in self.components],
            "orphan_positions": [
                {"symbol": symbol, "signed_qty": qty} for symbol, qty in self.orphan_positions
            ],
            "skipped_components": [item.as_dict() for item in self.skipped_components],
            "already_flat": self.already_flat,
        }


@dataclass(frozen=True, slots=True)
class FlattenResidual:
    """What the account still holds at one observation."""

    journal_sequence: int
    positions: tuple[tuple[str, float], ...]
    aggregate_targets: tuple[tuple[str, float], ...]
    working_symbols: tuple[str, ...]
    dust_symbols: tuple[str, ...]
    republished_target_keys: tuple[str, ...]

    @property
    def flat(self) -> bool:
        return not self.positions and not self.aggregate_targets and not self.working_symbols

    @property
    def dust_limited(self) -> bool:
        """Every residual position is dust, and nothing is still in flight."""

        if self.flat or self.working_symbols or self.aggregate_targets:
            return False
        dust = set(self.dust_symbols)
        return bool(self.positions) and all(symbol in dust for symbol, _ in self.positions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "journal_sequence": self.journal_sequence,
            "positions": [{"symbol": s, "signed_qty": q} for s, q in self.positions],
            "aggregate_targets": [{"symbol": s, "signed_qty": q} for s, q in self.aggregate_targets],
            "working_symbols": list(self.working_symbols),
            "dust_symbols": list(self.dust_symbols),
            "republished_target_keys": list(self.republished_target_keys),
            "flat": self.flat,
            "dust_limited": self.dust_limited,
        }


@dataclass(frozen=True, slots=True)
class FlattenOutcome:
    """The whole operation: what was planned, published, and reached."""

    plan: FlattenPlan
    executed: bool
    published_request_ids: tuple[str, ...] = ()
    publication_errors: tuple[Mapping[str, Any], ...] = ()
    residual: FlattenResidual | None = None
    status: str = "planned"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"already_flat", "flat", "planned"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "executed": self.executed,
            "detail": self.detail,
            "plan": self.plan.as_dict(),
            "published_request_ids": list(self.published_request_ids),
            "publication_errors": [dict(item) for item in self.publication_errors],
            "residual": None if self.residual is None else self.residual.as_dict(),
        }


def _finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise FlattenRefused(f"account state carries a non-finite quantity: {value!r}")
    return number


def _nonzero(value: float, *, tolerance: float) -> bool:
    return abs(value) > tolerance


def build_flatten_plan(
    state: AccountState,
    *,
    route_id: str,
    environment: str,
    account_id: str,
    journal_sequence: int,
    symbols: Sequence[str] | None = None,
    sleeves: Sequence[str] | None = None,
    tolerance: float = QUANTITY_TOLERANCE,
) -> FlattenPlan:
    """Enumerate every component target that still owns exposure.

    ``symbols``/``sleeves`` narrow the plan. A narrowed plan is a partial
    flatten and will not satisfy a rollout, which wants the whole account flat.
    """

    if tolerance < 0.0:
        raise ValueError("tolerance cannot be negative")
    symbol_filter = {str(item).strip().upper() for item in symbols} if symbols else None
    sleeve_filter = {str(item).strip().lower() for item in sleeves} if sleeves else None

    components: list[FlattenComponent] = []
    skipped: list[FlattenComponent] = []
    owned_symbols: set[str] = set()
    for target_key, payload in sorted(state.component_targets.items()):
        symbol = str(payload.get("symbol") or "").strip().upper()
        signed_qty = _finite(payload.get("signed_qty") or 0.0)
        if not symbol:
            raise FlattenRefused(f"component target {target_key!r} has no symbol")
        owned_symbols.add(symbol)
        if not _nonzero(signed_qty, tolerance=tolerance):
            continue
        sleeve = str(payload.get("sleeve") or target_key.split("/", 1)[0]).strip().lower()
        leverage = _finite(payload.get("leverage") or 1.0)
        component = FlattenComponent(
            target_key=target_key,
            symbol=symbol,
            sleeve=sleeve,
            strategy_id=str(payload.get("strategy_id") or "").strip(),
            component_id=str(payload.get("component_id") or "").strip(),
            signed_qty=signed_qty,
            leverage=leverage if leverage > 0.0 else 1.0,
        )
        if symbol_filter is not None and symbol not in symbol_filter:
            skipped.append(component)
            continue
        if sleeve_filter is not None and sleeve not in sleeve_filter:
            skipped.append(component)
            continue
        components.append(component)

    orphans: list[tuple[str, float]] = []
    for symbol, position in sorted(state.positions.items()):
        signed_qty = _finite(position.signed_qty)
        if not _nonzero(signed_qty, tolerance=tolerance):
            continue
        if symbol in owned_symbols:
            continue
        if symbol_filter is not None and symbol not in symbol_filter:
            continue
        orphans.append((symbol, signed_qty))

    return FlattenPlan(
        route_id=route_id,
        environment=environment,
        account_id=account_id,
        journal_sequence=journal_sequence,
        components=tuple(components),
        orphan_positions=tuple(orphans),
        skipped_components=tuple(skipped),
    )


def flatten_intents(plan: FlattenPlan, *, operator: str, reason: str) -> tuple[Any, ...]:
    """Build one zero-notional RISK intent per planned component.

    ``RiskTargetAdapter`` rewrites the target's sleeve back to the component's
    owning sleeve, so a flatten replaces the original component rather than
    creating a second owner beside it.
    """

    clean_operator = str(operator).strip()
    clean_reason = str(reason).strip()
    if not clean_operator:
        raise ValueError("operator is required")
    if not clean_reason:
        raise ValueError("reason is required")
    intents = []
    for component in plan.components:
        intents.append(
            requested_target(
                adapter_kind=SleeveAdapterKind.RISK,
                decision_key=f"account-flatten/{component.target_key}",
                target_key=component.target_key,
                strategy_id=component.strategy_id or "account-flatten",
                component_id=component.component_id or "flatten",
                symbol=component.symbol,
                signed_notional_usdt=0.0,
                leverage=component.leverage,
                reason=f"operator flatten: {clean_reason}",
                metadata={
                    "owner_sleeve": component.sleeve,
                    "account_flatten": True,
                    "account_flatten_operator": clean_operator,
                    "prior_signed_qty": component.signed_qty,
                },
            )
        )
    return tuple(intents)


def observe_residual(
    state: AccountState,
    *,
    journal_sequence: int,
    planned_target_keys: Sequence[str] = (),
    baseline_batch_ids: frozenset[str] = frozenset(),
    tolerance: float = QUANTITY_TOLERANCE,
) -> FlattenResidual:
    """Reduce one journal state to the numbers a flatten is waiting on."""

    positions = tuple(
        (symbol, _finite(position.signed_qty))
        for symbol, position in sorted(state.positions.items())
        if _nonzero(_finite(position.signed_qty), tolerance=tolerance)
    )
    aggregates = tuple(
        (symbol, _finite(quantity))
        for symbol, quantity in sorted(state.aggregate_targets.items())
        if _nonzero(_finite(quantity), tolerance=tolerance)
    )
    working = tuple(sorted(state.working_symbols(tolerance=tolerance)))
    dust = tuple(sorted(_dust_symbols(state, baseline_batch_ids=baseline_batch_ids)))
    planned = set(planned_target_keys)
    republished = tuple(
        sorted(
            target_key
            for target_key, payload in state.component_targets.items()
            if target_key in planned and _nonzero(_finite(payload.get("signed_qty") or 0.0), tolerance=tolerance)
        )
    )
    return FlattenResidual(
        journal_sequence=journal_sequence,
        positions=positions,
        aggregate_targets=aggregates,
        working_symbols=working,
        dust_symbols=dust,
        republished_target_keys=republished,
    )


def _dust_symbols(
    state: AccountState,
    *,
    baseline_batch_ids: frozenset[str] = frozenset(),
) -> set[str]:
    """Symbols the kernel has refused to reduce because no order is admissible.

    Taken from the kernel's own journaled ``rejection_keys`` rather than
    recomputed against instrument rules here, so flatten cannot disagree with
    the account about what is closeable. Batches present before the flatten
    began are ignored: a rejection against a position that has since been
    resized says nothing about the residual now.
    """

    symbols: set[str] = set()
    for batch_id, payload in state.risk_decisions.items():
        if batch_id in baseline_batch_ids or not isinstance(payload, Mapping):
            continue
        for key in payload.get("rejection_keys") or ():
            text = str(key)
            marker = f":{DUST_REJECTION_REASON}:"
            index = text.find(marker)
            if index < 0:
                continue
            symbol = text[index + len(marker) :].strip().upper()
            if symbol:
                symbols.add(symbol)
    return symbols


def flatten_account(
    *,
    account_root: str | Path,
    inbox_root: str | Path,
    environment: str,
    account_id: str | None = None,
    operator: str,
    reason: str,
    execute: bool = False,
    symbols: Sequence[str] | None = None,
    sleeves: Sequence[str] | None = None,
    wait_seconds: float = _DEFAULT_WAIT_SECONDS,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
    tolerance: float = QUANTITY_TOLERANCE,
    now_ns: int | None = None,
    sleep=time.sleep,
) -> FlattenOutcome:
    """Plan, optionally publish, and then watch one account to flat."""

    selected = execution_environment(environment)
    resolved_account_id = str(account_id or account_id_for_environment(selected))
    if wait_seconds < 0.0:
        raise ValueError("wait_seconds cannot be negative")
    if poll_seconds <= 0.0:
        raise ValueError("poll_seconds must be positive")

    route = require_account_route(
        account_id=resolved_account_id,
        environment=selected.value,
        account_root=account_root,
        inbox_root=inbox_root,
    )
    cursor = AccountJournalCursor(verify=True)
    digest = cursor.read(route.account_path)
    # Rejections already on record predate this flatten and are not evidence
    # about the residual it is about to create.
    baseline_batch_ids = frozenset(digest.state.risk_decisions)
    plan = build_flatten_plan(
        digest.state,
        route_id=route.route_id,
        environment=route.environment,
        account_id=route.account_id,
        journal_sequence=len(digest.events),
        symbols=symbols,
        sleeves=sleeves,
        tolerance=tolerance,
    )

    if plan.already_flat:
        return FlattenOutcome(
            plan=plan,
            executed=False,
            residual=observe_residual(
                digest.state,
                journal_sequence=len(digest.events),
                tolerance=tolerance,
            ),
            status="already_flat",
            detail="account holds no exposure and no nonzero targets",
        )

    if not execute:
        return FlattenOutcome(
            plan=plan,
            executed=False,
            residual=observe_residual(
                digest.state,
                journal_sequence=len(digest.events),
                tolerance=tolerance,
            ),
            status="planned",
            detail=(
                f"would publish {len(plan.components)} zero target(s); "
                f"{len(plan.orphan_positions)} orphan position(s) converge without a target"
            ),
        )

    publication: ExitFirstPublication | None = None
    if plan.components:
        publisher = AccountTargetPublisher(route)
        publication = publish_exit_first_target_requests(
            publisher,
            batch_prefix="account-flatten",
            exit_intents=flatten_intents(plan, operator=operator, reason=reason),
            entry_intents=(),
            created_ts_ns=int(time.time_ns() if now_ns is None else now_ns),
        )

    published_ids = () if publication is None else publication.exit_request_ids
    errors: tuple[Mapping[str, Any], ...] = (
        ()
        if publication is None
        else tuple(
            {
                "stage": item.stage,
                "target_key": item.target_key,
                "error_type": item.error_type,
                "message": item.message,
            }
            for item in publication.errors
        )
    )

    planned_keys = tuple(component.target_key for component in plan.components)
    residual, status, detail = _await_flat(
        cursor,
        route.account_path,
        planned_target_keys=planned_keys,
        baseline_batch_ids=baseline_batch_ids,
        wait_seconds=wait_seconds,
        poll_seconds=poll_seconds,
        tolerance=tolerance,
        sleep=sleep,
    )
    if errors and status != "flat":
        detail = f"{detail}; {len(errors)} target(s) failed to publish"

    return FlattenOutcome(
        plan=plan,
        executed=True,
        published_request_ids=tuple(published_ids),
        publication_errors=errors,
        residual=residual,
        status="publication_failed" if errors and status != "flat" else status,
        detail=detail,
    )


def _await_flat(
    cursor: AccountJournalCursor,
    account_path: Path,
    *,
    planned_target_keys: Sequence[str],
    baseline_batch_ids: frozenset[str],
    wait_seconds: float,
    poll_seconds: float,
    tolerance: float,
    sleep,
) -> tuple[FlattenResidual, str, str]:
    """Poll the journal until the owner has converged, or the deadline passes."""

    deadline = time.monotonic() + wait_seconds
    residual: FlattenResidual | None = None
    while True:
        digest = cursor.read(account_path)
        residual = observe_residual(
            digest.state,
            journal_sequence=len(digest.events),
            planned_target_keys=planned_target_keys,
            baseline_batch_ids=baseline_batch_ids,
            tolerance=tolerance,
        )
        if residual.flat:
            return residual, "flat", "account is flat: no positions, targets, or working orders"
        if residual.dust_limited:
            rendered = ", ".join(f"{symbol}={qty:g}" for symbol, qty in residual.positions)
            return (
                residual,
                "dust_limited",
                (
                    "every residual is below the venue minimum order size and cannot "
                    f"be closed: {rendered}"
                ),
            )
        if time.monotonic() >= deadline:
            break
        sleep(min(poll_seconds, max(deadline - time.monotonic(), 0.0)) or poll_seconds)

    assert residual is not None  # loop always observes at least once
    reasons: list[str] = []
    if residual.positions:
        reasons.append(
            "positions remain: " + ", ".join(f"{s}={q:g}" for s, q in residual.positions)
        )
    if residual.aggregate_targets:
        reasons.append(
            "nonzero targets remain: "
            + ", ".join(f"{s}={q:g}" for s, q in residual.aggregate_targets)
        )
    if residual.working_symbols:
        reasons.append("orders still working: " + ", ".join(residual.working_symbols))
    if residual.republished_target_keys:
        reasons.append(
            "a producer republished: " + ", ".join(residual.republished_target_keys)
        )
    return residual, "timed_out", "; ".join(reasons) or "account did not reach flat"


# Operator entry point


def _parser() -> Any:
    import argparse  # noqa: PLC0415 - only the CLI path needs it

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--inbox-root", required=True)
    parser.add_argument(
        "--environment",
        required=True,
        choices=EXECUTION_ENVIRONMENT_CHOICES,
        help="Which account owner to flatten. Named explicitly; there is no default.",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help="Defaults to the canonical account id for the environment.",
    )
    parser.add_argument("--operator", required=True, help="Who is authorizing this.")
    parser.add_argument("--reason", required=True, help="Why the account is being flattened.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Publish the zero targets. Without it this reports the plan and exits.",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        help="Limit to this symbol; repeatable. A narrowed flatten will not satisfy a rollout.",
    )
    parser.add_argument(
        "--sleeve",
        action="append",
        default=None,
        help="Limit to this sleeve; repeatable. A narrowed flatten will not satisfy a rollout.",
    )
    parser.add_argument("--wait-seconds", type=float, default=_DEFAULT_WAIT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=_DEFAULT_POLL_SECONDS)
    return parser


_EXIT_CODES = {
    "already_flat": 0,
    "flat": 0,
    "planned": 0,
    "dust_limited": 4,
    "timed_out": 5,
    "publication_failed": 6,
}


def main(argv: Any = None) -> int:
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415

    args = _parser().parse_args(argv)
    try:
        outcome = flatten_account(
            account_root=args.account_root,
            inbox_root=args.inbox_root,
            environment=args.environment,
            account_id=args.account_id,
            operator=args.operator,
            reason=args.reason,
            execute=bool(args.execute),
            symbols=args.symbol,
            sleeves=args.sleeve,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
        )
    except FlattenRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(outcome.as_dict(), indent=2, sort_keys=True))
    if not outcome.ok:
        print(f"flatten did not reach flat: {outcome.detail}", file=sys.stderr)
    return _EXIT_CODES.get(outcome.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
