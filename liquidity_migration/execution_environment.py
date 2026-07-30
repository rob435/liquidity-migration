"""Explicit forward-execution environment contract.

Strategy producers never decide whether they may submit orders.  They publish
targets to exactly one account owner selected by this value; the owner adapter
is the only layer with venue-mutation authority.

Three owners exist, and they are not three points on one scale:

``demo``
    Bybit's api-demo realm. Real prices, simulated fills, no capital.
``paper``
    A credential-free twin of the demo owner. It has no venue at all; its
    cycles are routing/lifecycle evidence, never fill evidence.
``mainnet``
    The funded Bybit account. Selecting it is necessary but never sufficient:
    ``REAL_MONEY`` is the arming switch and belongs to the owner alone.

``EXECUTION_ENVIRONMENT_VALUES`` exists so a fourth member can never again be
added while a dozen ``{"demo", "paper"}`` literals silently keep the old
arity — which is exactly the defect (B10) that made adding the third one a
project rather than a line.
"""

from __future__ import annotations

from enum import StrEnum

from .venue_realm import VenueRealm


class ExecutionEnvironment(StrEnum):
    DEMO = "demo"
    PAPER = "paper"
    MAINNET = "mainnet"


#: Every valid environment value. Import this rather than restating a literal.
EXECUTION_ENVIRONMENT_VALUES: frozenset[str] = frozenset(
    member.value for member in ExecutionEnvironment
)

#: Ordered form for argparse ``choices`` and stable rendering.
EXECUTION_ENVIRONMENT_CHOICES: tuple[str, ...] = tuple(
    member.value for member in ExecutionEnvironment
)

_ACCOUNT_IDS: dict[ExecutionEnvironment, str] = {
    ExecutionEnvironment.DEMO: "bybit-demo-unified",
    ExecutionEnvironment.PAPER: "bybit-paper-unified",
    ExecutionEnvironment.MAINNET: "bybit-mainnet-unified",
}

#: The venue realm an environment authenticates against. ``paper`` maps to
#: nothing on purpose: it holds no credentials and addresses no venue, so a
#: caller that needs a realm must handle its absence rather than be handed a
#: plausible-looking default.
_VENUE_REALMS: dict[ExecutionEnvironment, VenueRealm] = {
    ExecutionEnvironment.DEMO: VenueRealm.DEMO,
    ExecutionEnvironment.MAINNET: VenueRealm.MAINNET,
}


def execution_environment(value: object) -> ExecutionEnvironment:
    """Parse one explicit ``demo|paper|mainnet`` value and reject every fallback."""

    text = str(value or "").strip().lower()
    try:
        return ExecutionEnvironment(text)
    except ValueError as exc:
        raise ValueError(
            "execution_environment must be explicitly set to one of "
            + ", ".join(repr(choice) for choice in EXECUTION_ENVIRONMENT_CHOICES)
        ) from exc


def account_id_for_environment(value: object) -> str:
    return _ACCOUNT_IDS[execution_environment(value)]


def venue_realm_for_environment(value: object) -> VenueRealm | None:
    """The realm this environment authenticates against, or None for ``paper``."""

    return _VENUE_REALMS.get(execution_environment(value))
