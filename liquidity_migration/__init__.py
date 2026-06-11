"""Bybit liquidity-migration research package."""

__all__ = ["__version__"]

__version__ = "0.1.0"

# The event_demo hub is split into sibling modules (event_demo_{exits,data,
# reports}). The hub eagerly imports its siblings at the bottom (to re-export
# their names), and each sibling imports shared helpers back from the hub at the
# top. That hub<->sibling cycle is fine when the hub is imported first, but
# importing a SIBLING first in a fresh process deadlocks on the partially-
# initialized hub. Preloading the hub here guarantees it finishes initializing
# before any sibling can be imported standalone.
# (The volume_events hub — the retired daily-short research engine — was erased
# by operator order 2026-06-11; shared helpers moved to trade_lifecycle/_common.)
from . import event_demo as _event_demo  # noqa: E402,F401
