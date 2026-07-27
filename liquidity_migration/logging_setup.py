"""Default stderr logging for package entrypoints running under systemd.

Without a handler, Python's last-resort logger drops every record below
WARNING, which leaves long-running owners journald-silent in normal
operation and hides INFO-level transport/receipt evidence.
"""

from __future__ import annotations

import logging
import os


def ensure_default_log_handler() -> None:
    """Attach a package stderr handler when the process has no logging setup."""

    package_logger = logging.getLogger("liquidity_migration")
    if package_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    package_logger.addHandler(handler)
    level_name = os.environ.get("LIQMIG_LOG_LEVEL", "INFO").upper()
    package_logger.setLevel(getattr(logging, level_name, logging.INFO))
