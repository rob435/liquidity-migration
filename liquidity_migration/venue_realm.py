"""Which Bybit venue a private credential authenticates against: ``demo`` or ``mainnet``.

Distinct from ``ExecutionEnvironment``, which names the owner process a producer
publishes to (``paper`` is an owner with no venue credentials at all).

The realm is always named explicitly — credential resolution takes it as a
required argument — and the endpoint the transport resolved to is read back and
compared to it after construction (``bybit._require_realm_endpoint``).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "MAINNET_REST_ENDPOINT",
    "REALM_CREDENTIAL_VARIABLES",
    "REALM_REST_ENDPOINTS",
    "VenueRealm",
    "venue_realm",
]


class VenueRealm(StrEnum):
    DEMO = "demo"
    MAINNET = "mainnet"


DEMO_REST_ENDPOINT = "https://api-demo.bybit.com"
MAINNET_REST_ENDPOINT = "https://api.bybit.com"

REALM_REST_ENDPOINTS: dict[VenueRealm, str] = {
    VenueRealm.DEMO: DEMO_REST_ENDPOINT,
    VenueRealm.MAINNET: MAINNET_REST_ENDPOINT,
}

#: The two realms read different variables on purpose, so a key left in the
#: environment for one can never authenticate the other.
REALM_CREDENTIAL_VARIABLES: dict[VenueRealm, tuple[str, str]] = {
    VenueRealm.DEMO: ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"),
    VenueRealm.MAINNET: ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET"),
}


def venue_realm(value: object) -> VenueRealm:
    """Parse one explicit ``demo|mainnet`` value and reject every fallback."""

    if isinstance(value, VenueRealm):
        return value
    text = str(value or "").strip().lower()
    try:
        return VenueRealm(text)
    except ValueError as exc:
        raise ValueError(
            "venue realm must be explicitly set to 'demo' or 'mainnet'"
        ) from exc
