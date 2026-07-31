"""Default stderr logging for package entrypoints running under systemd.

The handler goes on the ROOT logger, not on ``liquidity_migration``: entrypoints
run as ``python -m liquidity_migration.<module>``, so their own logger is named
``__main__`` and a package-only handler would miss every record they emit.
"""

from __future__ import annotations

import logging
import os


# Chatty at INFO; the VPS journal has a fixed size cap.
_QUIET_THIRD_PARTY_LOGGERS = ("pybit", "websocket", "urllib3", "requests")


def ensure_default_log_handler() -> None:
    """Attach a root stderr handler when the process has no logging setup."""

    root_logger = logging.getLogger()
    package_logger = logging.getLogger("liquidity_migration")
    if root_logger.handlers or package_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger.addHandler(handler)
    level_name = os.environ.get("LIQMIG_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger.setLevel(level)
    # Set explicitly so a later root-level change cannot mute the package.
    package_logger.setLevel(level)
    for name in _QUIET_THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        if third_party.level == logging.NOTSET:
            third_party.setLevel(max(level, logging.WARNING))
