"""Which Bybit account a private credential actually addresses.

``ExecutionEnvironment`` answers a different question — *which owner process a
producer publishes to* (``demo`` and ``paper`` are two owners; the paper one
holds no venue credentials at all). This answers *which venue realm a private
credential authenticates against*, and there are exactly two of those.

Keeping them separate matters. ``paper`` is not a venue: it is a
credential-free twin of the demo owner. Folding a third value into
``ExecutionEnvironment`` without this distinction would make ``paper`` and
``mainnet`` look like siblings when one has no venue at all.

Three properties hold everywhere this type is used:

* **The realm is named, never inferred.** Credential resolution takes it as a
  required argument with no default.
* **Mainnet is unreachable by omission.** Every place that can fall back falls
  back to ``demo``; reaching ``mainnet`` always requires someone to have typed
  it, and ``REAL_MONEY`` to be explicitly armed on top of that.
* **The realm is asserted after construction, never before.** The endpoint the
  transport actually resolved to is read back and compared to the realm that
  was selected — for both realms. See ``bybit._require_realm_endpoint``.
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


#: The only REST hosts this repository may address with private credentials.
DEMO_REST_ENDPOINT = "https://api-demo.bybit.com"
MAINNET_REST_ENDPOINT = "https://api.bybit.com"

REALM_REST_ENDPOINTS: dict[VenueRealm, str] = {
    VenueRealm.DEMO: DEMO_REST_ENDPOINT,
    VenueRealm.MAINNET: MAINNET_REST_ENDPOINT,
}

#: Credential variables per realm. The two realms read *different* variables on
#: purpose: a demo key left in the environment can never authenticate a mainnet
#: run by accident, and vice versa.
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
