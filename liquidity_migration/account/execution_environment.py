"""Which account owner a producer's targets are routed to.

``demo``
    Bybit's api-demo realm: real prices, simulated fills, no capital.
``mainnet``
    The funded Bybit account. Selecting it is not sufficient; ``REAL_MONEY`` is
    the separate arming switch.

The credential-free ``paper`` twin was retired 2026-08-03; its journals remain
on disk but nothing routes to it.

Import ``EXECUTION_ENVIRONMENT_VALUES`` instead of restating the member set, so
adding a member does not leave stale literals behind.
"""

from __future__ import annotations

from enum import StrEnum

from liquidity_migration.core.venue_realm import VenueRealm


class ExecutionEnvironment(StrEnum):
    DEMO = "demo"
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
    ExecutionEnvironment.MAINNET: "bybit-mainnet-unified",
}

_VENUE_REALMS: dict[ExecutionEnvironment, VenueRealm] = {
    ExecutionEnvironment.DEMO: VenueRealm.DEMO,
    ExecutionEnvironment.MAINNET: VenueRealm.MAINNET,
}


def execution_environment(value: object) -> ExecutionEnvironment:
    """Parse one explicit ``demo|mainnet`` value and reject every fallback."""

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


def venue_realm_for_environment(value: object) -> VenueRealm:
    """The venue realm this environment authenticates against."""

    return _VENUE_REALMS[execution_environment(value)]


def candidate_universe_realm(value: object) -> VenueRealm:
    """The realm whose frozen candidate universe this environment reads."""

    return _VENUE_REALMS[execution_environment(value)]
