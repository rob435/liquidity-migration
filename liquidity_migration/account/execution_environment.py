"""Which account owner a producer's targets are routed to.

``demo``
    Bybit's api-demo realm: real prices, simulated fills, no capital.
``paper``
    Credential-free twin of the demo owner. No venue at all, so its cycles are
    routing/lifecycle evidence, never fill evidence.
``mainnet``
    The funded Bybit account. Selecting it is not sufficient; ``REAL_MONEY`` is
    the separate arming switch.

Import ``EXECUTION_ENVIRONMENT_VALUES`` instead of restating the member set, so
adding a member does not leave stale literals behind.
"""

from __future__ import annotations

from enum import StrEnum

from liquidity_migration.core.venue_realm import VenueRealm


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

#: The venue realm an environment authenticates against. ``paper`` is absent on
#: purpose: it has no venue, so callers must handle ``None`` rather than get a
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


def candidate_universe_realm(value: object) -> VenueRealm:
    """The realm whose frozen candidate universe this environment reads.

    ``paper`` has no venue but is the demo owner's twin, so it reads the
    demo-frozen artifact.
    """

    environment = execution_environment(value)
    if environment is ExecutionEnvironment.PAPER:
        return VenueRealm.DEMO
    return _VENUE_REALMS[environment]
