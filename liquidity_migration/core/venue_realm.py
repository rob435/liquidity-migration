"""Which Bybit public-data realm Python research addresses."""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "MAINNET_REST_ENDPOINT",
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
