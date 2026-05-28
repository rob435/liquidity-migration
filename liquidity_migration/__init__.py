"""Bybit liquidity-migration research package."""

__all__ = ["__version__"]

__version__ = "0.1.0"

# The event_demo and volume_events hubs were each split into sibling modules
# (event_demo_{exits,data,entries,planning,reports}, volume_events_{filters,
# features,charts,validation}). Each hub eagerly imports its siblings at the
# bottom (to re-export their names, and because the hub's own cycle code calls
# them), and each sibling imports shared helpers back from its hub at the top.
# That hub<->sibling cycle is fine when the hub is imported first, but importing
# a SIBLING first in a fresh process deadlocks on the partially-initialized hub
# ("cannot import name ... from partially initialized module"). Preloading the
# hubs here -- in dependency order (volume_events first; event_demo depends on
# it) -- guarantees they finish initializing before any sibling can be imported
# standalone, so `import liquidity_migration.event_demo_exits` (etc.) works cold.
from . import volume_events as _volume_events  # noqa: E402,F401
from . import event_demo as _event_demo  # noqa: E402,F401
