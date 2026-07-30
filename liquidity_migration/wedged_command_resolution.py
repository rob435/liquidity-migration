"""The missing exit from a wedged ``commanded`` order (B15b).

``wedged_command_watch`` names and ranks a command that can no longer progress.
It deliberately stops there: resolving one needs an operator-authorized journal
transition recording what actually happened at the venue, and inventing that
silently would be exactly the blind resend the design refuses.

This module supplies the transition, and the evidence standard it must clear.

**The no-blind-resend rule is untouched.** Nothing here resubmits, retries, or
recreates an order. The only journal effect is terminalizing a command that the
venue demonstrably does not hold, which unfreezes the symbol so ordinary
convergence can act on the position again.

The evidence standard, in order of strictness:

``live``
    The venue reports a working order under this ``orderLinkId``. Resolution is
    **refused**. The command is not wedged in any sense that a journal edit can
    fix — the order is real and its executions are still coming.
``unreadable``
    Any of the three queries failed. Refused: an absent answer is not evidence
    of an absent order, and this is precisely the state that fails closed.
``unreconstructed_fills``
    Executions exist for this command that the journal has not reduced yet.
    Refused until reconciliation catches up; terminalizing first would lose
    fills and corrupt position truth.
``terminal``
    The venue's own order history reports Cancelled/Rejected/Filled/
    PartiallyFilledCanceled. The venue has already answered; the resolution
    simply records the answer it gave.
``absent``
    Nothing at the venue under this id, anywhere, and the command is older than
    Bybit's own visibility lag. This is the genuinely ambiguous case and the
    only one that consumes operator authorization: a human states, on the
    record, that they have checked the account and the order does not exist.

A ``never_submitted`` wedge (``submission_attempts == 0``) is journal-proven to
have never left the process — the attempt is committed *before* the call — but
it is still probed, because a durable claim is cheap and a wrong one is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .wedged_command_watch import (
    DEFAULT_WEDGE_AFTER_NS,
    WEDGE_AMBIGUOUS_SUBMISSION,
    WEDGE_NEVER_SUBMITTED,
    wedged_commands,
)

__all__ = [
    "RESOLUTION_REJECTION_KEY",
    "WedgeEvidence",
    "WedgedCommandResolutionRefused",
    "describe_wedges",
    "main",
    "probe_wedged_command",
    "resolve_wedged_command",
]

_logger = logging.getLogger(__name__)

#: Recorded on the terminal event so a resolved wedge is never mistaken for an
#: ordinary venue cancellation in later accounting.
RESOLUTION_REJECTION_KEY = "operator_resolved_wedged_command"

#: Order statuses that mean the venue still holds this order.
_LIVE_STATUSES = frozenset({"new", "created", "untriggered", "triggered", "partiallyfilled"})

#: Order statuses that mean the venue has finished with it.
_TERMINAL_STATUSES = frozenset(
    {"cancelled", "canceled", "rejected", "filled", "partiallyfilledcanceled", "deactivated", "expired"}
)


class WedgedCommandResolutionRefused(RuntimeError):
    """The evidence does not support terminalizing this command."""


@dataclass(frozen=True, slots=True)
class WedgeEvidence:
    """Read-only venue truth about one ``orderLinkId``."""

    command_id: str
    symbol: str
    classification: str
    venue_order_status: str = ""
    venue_order_id: str = ""
    observed_execution_ids: tuple[str, ...] = ()
    observed_filled_qty: float = 0.0
    query_errors: tuple[str, ...] = ()
    detail: str = ""

    @property
    def resolvable_without_authorization(self) -> bool:
        return self.classification == "terminal"

    def as_metadata(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "venue_order_status": self.venue_order_status,
            "venue_order_id": self.venue_order_id,
            "observed_execution_ids": list(self.observed_execution_ids),
            "observed_filled_qty": self.observed_filled_qty,
            "query_errors": list(self.query_errors),
            "detail": self.detail,
        }


@dataclass(slots=True)
class _Probe:
    rows: list[Mapping[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _query(label: str, call: Any, **params: Any) -> _Probe:
    probe = _Probe()
    if not callable(call):
        probe.errors.append(f"{label}:unavailable")
        return probe
    try:
        rows = call(**params)
    except Exception as exc:  # noqa: BLE001 - a failed query is evidence of nothing
        probe.errors.append(f"{label}:{type(exc).__name__}:{exc}"[:300])
        return probe
    probe.rows = [row for row in (rows or ()) if isinstance(row, Mapping)]
    return probe


def probe_wedged_command(
    *,
    client: Any,
    command_id: str,
    symbol: str,
) -> WedgeEvidence:
    """Read the venue three ways for one ``orderLinkId``. Mutates nothing."""

    symbol = symbol.upper()
    open_orders = _query(
        "open_orders",
        getattr(client, "get_open_orders", None),
        symbol=symbol,
        order_link_id=command_id,
    )
    history = _query(
        "order_history",
        getattr(client, "get_order_history", None),
        symbol=symbol,
        order_link_id=command_id,
    )
    executions = _query(
        "trade_history",
        getattr(client, "get_trade_history", None),
        symbol=symbol,
        order_link_id=command_id,
    )
    errors = tuple(open_orders.errors + history.errors + executions.errors)

    execution_ids: list[str] = []
    observed_filled = 0.0
    for row in executions.rows:
        if str(row.get("orderLinkId") or row.get("order_link_id") or "") != command_id:
            continue
        execution_id = str(row.get("execId") or row.get("exec_id") or "")
        if execution_id:
            execution_ids.append(execution_id)
        try:
            observed_filled += abs(float(row.get("execQty") or row.get("exec_qty") or 0.0))
        except (TypeError, ValueError):
            pass

    matched = [
        row
        for row in (*open_orders.rows, *history.rows)
        if str(row.get("orderLinkId") or row.get("order_link_id") or "") == command_id
    ]
    live = [
        row
        for row in matched
        if _status(row) in _LIVE_STATUSES
    ]
    terminal = [row for row in matched if _status(row) in _TERMINAL_STATUSES]

    if live:
        row = live[0]
        return WedgeEvidence(
            command_id=command_id,
            symbol=symbol,
            classification="live",
            venue_order_status=_status(row),
            venue_order_id=str(row.get("orderId") or row.get("order_id") or ""),
            observed_execution_ids=tuple(execution_ids),
            observed_filled_qty=observed_filled,
            query_errors=errors,
            detail="the venue still holds a working order under this command id",
        )
    if errors:
        return WedgeEvidence(
            command_id=command_id,
            symbol=symbol,
            classification="unreadable",
            observed_execution_ids=tuple(execution_ids),
            observed_filled_qty=observed_filled,
            query_errors=errors,
            detail="venue truth is incomplete; an absent answer is not an absent order",
        )
    if terminal:
        row = terminal[0]
        return WedgeEvidence(
            command_id=command_id,
            symbol=symbol,
            classification="terminal",
            venue_order_status=_status(row),
            venue_order_id=str(row.get("orderId") or row.get("order_id") or ""),
            observed_execution_ids=tuple(execution_ids),
            observed_filled_qty=observed_filled,
            query_errors=errors,
            detail="the venue already answered for this command",
        )
    if matched:
        row = matched[0]
        return WedgeEvidence(
            command_id=command_id,
            symbol=symbol,
            classification="unreadable",
            venue_order_status=_status(row),
            venue_order_id=str(row.get("orderId") or row.get("order_id") or ""),
            observed_execution_ids=tuple(execution_ids),
            observed_filled_qty=observed_filled,
            query_errors=errors,
            detail="the venue reports a status this repository does not classify",
        )
    return WedgeEvidence(
        command_id=command_id,
        symbol=symbol,
        classification="absent",
        observed_execution_ids=tuple(execution_ids),
        observed_filled_qty=observed_filled,
        query_errors=errors,
        detail="no open order, no order history, and no execution under this command id",
    )


def _status(row: Mapping[str, Any]) -> str:
    return str(row.get("orderStatus") or row.get("order_status") or "").strip().lower()


def resolve_wedged_command(
    *,
    kernel: Any,
    client: Any,
    command_id: str,
    operator: str,
    reason: str,
    authorize_absent: bool = False,
    now_ns: int | None = None,
    wedge_after_ns: int = DEFAULT_WEDGE_AFTER_NS,
) -> WedgeEvidence:
    """Terminalize one wedged command, on evidence, under a named operator.

    Returns the evidence that justified it. Raises
    :class:`WedgedCommandResolutionRefused` when the evidence does not support
    the transition — which is the common case and is meant to be.
    """

    operator = str(operator).strip()
    reason = str(reason).strip()
    if not operator:
        raise WedgedCommandResolutionRefused("resolution requires a named operator")
    if not reason:
        raise WedgedCommandResolutionRefused("resolution requires an explicit reason")

    state = kernel._state_ref()
    order = state.orders.get(command_id)
    if order is None:
        raise WedgedCommandResolutionRefused(f"unknown command {command_id!r}")
    if order.status != "commanded":
        raise WedgedCommandResolutionRefused(
            f"command {command_id} is {order.status}, not commanded; there is no wedge to resolve"
        )
    observed_ns = int(now_ns if now_ns is not None else kernel.clock.wall_time_ns())
    wedges = {
        wedge.command_id: wedge
        for wedge in wedged_commands(
            [order],
            now_ns=observed_ns,
            wedge_after_ns=wedge_after_ns,
        )
    }
    wedge = wedges.get(command_id)
    if wedge is None:
        raise WedgedCommandResolutionRefused(
            f"command {command_id} is younger than the wedge bound; it may still be in flight"
        )

    evidence = probe_wedged_command(client=client, command_id=command_id, symbol=order.symbol)
    if evidence.classification == "live":
        raise WedgedCommandResolutionRefused(
            f"refusing to resolve {command_id}: {evidence.detail} "
            f"(status {evidence.venue_order_status!r})"
        )
    if evidence.classification == "unreadable":
        raise WedgedCommandResolutionRefused(
            f"refusing to resolve {command_id}: {evidence.detail} "
            f"({'; '.join(evidence.query_errors) or 'unclassified status'})"
        )
    unreconstructed = evidence.observed_filled_qty - abs(order.filled_signed_qty)
    if unreconstructed > _fill_tolerance(order.signed_qty):
        raise WedgedCommandResolutionRefused(
            f"refusing to resolve {command_id}: the venue reports "
            f"{evidence.observed_filled_qty:g} filled against {abs(order.filled_signed_qty):g} "
            "reconstructed; let reconciliation reduce the executions first"
        )
    if evidence.classification == "absent" and not authorize_absent:
        raise WedgedCommandResolutionRefused(
            f"refusing to resolve {command_id}: {evidence.detail}. "
            "Absence at the venue is strong evidence, not proof — confirm the account by hand "
            "and re-run with explicit absent-order authorization"
        )

    status = "partially_filled_cancelled" if abs(order.filled_signed_qty) > 0.0 else "cancelled"
    kernel.record_order_status(
        command_id=command_id,
        status=status,
        cumulative_filled_qty=abs(order.filled_signed_qty),
        exchange_ts_ns=0,
        local_receive_ts_ns=observed_ns,
        rejection_key=RESOLUTION_REJECTION_KEY,
        metadata={
            "wedged_command_resolution": True,
            "wedge_kind": wedge.kind,
            "wedge_age_ns": wedge.age_ns,
            "blocked_exit": wedge.blocks_exit,
            "operator": operator,
            "reason": reason,
            "authorized_absent_order": bool(
                evidence.classification == "absent" and authorize_absent
            ),
            "venue_evidence": evidence.as_metadata(),
        },
    )
    _logger.warning(
        "resolved wedged command %s (%s) as %s on %s evidence, authorized by %s: %s",
        command_id,
        wedge.kind,
        status,
        evidence.classification,
        operator,
        reason,
    )
    return evidence


def _fill_tolerance(signed_qty: float) -> float:
    return max(abs(float(signed_qty)) * 1e-9, 1e-12)


def describe_wedges(
    orders: Sequence[Any],
    *,
    now_ns: int,
    wedge_after_ns: int = DEFAULT_WEDGE_AFTER_NS,
) -> tuple[str, ...]:
    """Operator-readable lines, worst first, for a read-only report."""

    return tuple(
        f"{wedge.describe()} [{_KIND_ADVICE[wedge.kind]}]"
        for wedge in wedged_commands(orders, now_ns=now_ns, wedge_after_ns=wedge_after_ns)
    )


_KIND_ADVICE = {
    WEDGE_AMBIGUOUS_SUBMISSION: (
        "the answer was lost; the venue may hold a live order, so probe before resolving"
    ),
    WEDGE_NEVER_SUBMITTED: (
        "journal-proven never dispatched; still probed before any resolution"
    ),
}


# --------------------------------------------------------------------------
# Operator entry point
# --------------------------------------------------------------------------


def _parser() -> Any:
    import argparse  # noqa: PLC0415 - only the CLI path needs it

    from .venue_realm import VenueRealm  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument(
        "--realm",
        default=VenueRealm.DEMO.value,
        choices=tuple(realm.value for realm in VenueRealm),
        help="Realm to probe. Omitting it selects demo and never mainnet.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("report", help="List wedged commands. Reads only.")
    probe = commands.add_parser("probe", help="Read venue truth for one command. Reads only.")
    probe.add_argument("--command-id", required=True)
    resolve = commands.add_parser(
        "resolve", help="Terminalize one wedged command on evidence."
    )
    resolve.add_argument("--command-id", required=True)
    resolve.add_argument("--operator", required=True, help="Who is authorizing this.")
    resolve.add_argument("--reason", required=True, help="What they checked, and what they saw.")
    resolve.add_argument(
        "--authorize-absent-order",
        action="store_true",
        help=(
            "Required only when the venue reports nothing at all under this id. "
            "Absence is strong evidence, not proof: confirm the account by hand first."
        ),
    )
    return parser


def main(argv: Any = None) -> int:
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import time  # noqa: PLC0415

    from .account_kernel import AccountExecutionKernel  # noqa: PLC0415
    from .bybit import BybitPrivateClient, resolve_private_credentials  # noqa: PLC0415
    from .logging_setup import ensure_default_log_handler  # noqa: PLC0415
    from .venue_realm import VenueRealm, venue_realm  # noqa: PLC0415

    ensure_default_log_handler()
    args = _parser().parse_args(argv)
    realm = venue_realm(args.realm)
    kernel = AccountExecutionKernel(args.account_root, account_id=args.account_id)
    now_ns = time.time_ns()

    if args.command == "report":
        lines = describe_wedges(list(kernel._state_ref().orders.values()), now_ns=now_ns)
        print(json.dumps({"wedged": list(lines)}, indent=2, sort_keys=True))
        return 0

    api_key, api_secret = resolve_private_credentials(realm=realm)
    if not api_key or not api_secret:
        print("realm credentials are not present in this environment", file=sys.stderr)
        return 2
    # Read-only: no mutation lease is passed, so this client cannot place,
    # cancel, or amend anything even if a later edit tried to.
    client = BybitPrivateClient(
        category="linear",
        testnet=False,
        demo=realm is VenueRealm.DEMO,
        realm=realm,
        api_key=api_key,
        api_secret=api_secret,
    )

    if args.command == "probe":
        order = kernel._state_ref().orders.get(args.command_id)
        if order is None:
            print(f"unknown command {args.command_id!r}", file=sys.stderr)
            return 2
        evidence = probe_wedged_command(
            client=client, command_id=args.command_id, symbol=order.symbol
        )
        print(json.dumps(evidence.as_metadata(), indent=2, sort_keys=True))
        return 0

    try:
        evidence = resolve_wedged_command(
            kernel=kernel,
            client=client,
            command_id=args.command_id,
            operator=args.operator,
            reason=args.reason,
            authorize_absent=args.authorize_absent_order,
            now_ns=now_ns,
        )
    except WedgedCommandResolutionRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"resolved": args.command_id, **evidence.as_metadata()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
