"""Detect order commands that can no longer make progress, and escalate them.

The account kernel suppresses all command generation for a symbol while any order
for it is working, which is correct while a submission is genuinely in flight.
Two events make "in flight" permanent:

* ``BybitSubmissionUncertain`` on the exposure-creating ``place_order`` — the
  attempt is journaled *before* the call, so ``submission_attempts >= 1`` and
  every retry raises ``AmbiguousExposureSubmission`` rather than resending.
* ``StaleUnsubmittedExposureCommand`` — a ``commanded`` order with
  ``submission_attempts == 0``, left by a kill between journal commit and submit.

Either way the symbol is frozen: no producer request, no convergence pass, and no
exit, while the real position sits at the venue behind only its native stop.

This module reports the wedge; it does not resolve it. Resolution needs an
operator-authorized journal transition recording what actually happened at the
venue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "WEDGE_AMBIGUOUS_SUBMISSION",
    "WEDGE_NEVER_SUBMITTED",
    "WedgedCommand",
    "wedged_commands",
]

#: Journaled an attempt, then lost the answer. Cannot be resent, cannot be
#: assumed dead: the venue may hold a live order.
WEDGE_AMBIGUOUS_SUBMISSION = "ambiguous_submission"

#: Committed to the journal but provably never dispatched. Safe to abandon, but
#: only an operator-authorized transition may say so.
WEDGE_NEVER_SUBMITTED = "never_submitted"

#: How long a ``commanded`` order may sit before it counts as wedged rather than
#: in flight. Healthy submissions resolve in under a second; minutes here so
#: ordinary venue slowness and a single restart do not page anyone.
DEFAULT_WEDGE_AFTER_NS = 300 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class WedgedCommand:
    """One command that cannot progress, and the exposure it is freezing."""

    command_id: str
    symbol: str
    kind: str
    age_ns: int
    signed_qty: float
    reduce_only: bool

    @property
    def blocks_exit(self) -> bool:
        """Whether this wedge is also preventing the position from being closed.

        A non-reduce-only wedge froze a symbol whose position is still open.
        """

        return not self.reduce_only

    def describe(self) -> str:
        age_s = self.age_ns / 1_000_000_000
        detail = (
            f"{self.symbol} command {self.command_id} {self.kind} "
            f"for {age_s:,.0f}s (qty {self.signed_qty:+g})"
        )
        if self.blocks_exit:
            detail += "; symbol frozen with an open position and no exit path"
        return detail


def wedged_commands(
    orders: Iterable[Any],
    *,
    now_ns: int,
    wedge_after_ns: int = DEFAULT_WEDGE_AFTER_NS,
) -> tuple[WedgedCommand, ...]:
    """Return every ``commanded`` order too old to still be in flight.

    ``orders`` are journal order records; only ``status == "commanded"`` can
    wedge, because every other status is terminal or already reconciled.
    """

    bound = max(int(wedge_after_ns), 0)
    found: list[WedgedCommand] = []
    for order in orders:
        if str(getattr(order, "status", "")) != "commanded":
            continue
        attempts = int(getattr(order, "submission_attempts", 0) or 0)
        # Age from the attempt when there was one: a command that waited in a
        # queue before dispatch is not wedged for the time it spent waiting.
        started = int(getattr(order, "last_submission_started_ts_ns", 0) or 0)
        created = int(getattr(order, "created_ts_ns", 0) or 0)
        anchor = started if attempts > 0 and started > 0 else created
        if anchor <= 0:
            continue
        age_ns = int(now_ns) - anchor
        if age_ns < bound:
            continue
        found.append(
            WedgedCommand(
                command_id=str(getattr(order, "command_id", "")),
                symbol=str(getattr(order, "symbol", "")).upper(),
                kind=(
                    WEDGE_AMBIGUOUS_SUBMISSION if attempts > 0 else WEDGE_NEVER_SUBMITTED
                ),
                age_ns=age_ns,
                signed_qty=float(getattr(order, "signed_qty", 0.0) or 0.0),
                reduce_only=bool(getattr(order, "reduce_only", False)),
            )
        )
    # Worst first: a frozen open position outranks a stuck exit, then by age.
    found.sort(key=lambda w: (not w.blocks_exit, -w.age_ns, w.symbol))
    return tuple(found)
