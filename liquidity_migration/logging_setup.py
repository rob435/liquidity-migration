"""Default stderr logging for package entrypoints running under systemd.

Without a handler, Python's last-resort logger drops every record below
WARNING, which leaves long-running owners journald-silent in normal
operation and hides INFO-level transport/receipt evidence.

The handler goes on the ROOT logger, not on ``liquidity_migration``. Every
service entrypoint runs as ``python -m liquidity_migration.<module>``, so its
own module logger is named ``__main__`` and sits outside the package tree: a
package-only handler silently dropped every INFO record the entrypoints emit
(the account owners' Telegram-delivery and request-completion audit lines
among them) and rendered their WARNING/ERROR records through
``logging.lastResort`` with no timestamp, level, or logger name. Observed live
2026-07-29: the demo owner delivered hourly Telegram digests for days without
one journald line to prove it.
"""

from __future__ import annotations

import logging
import os


# Third-party loggers reachable from the root handler that are chatty at INFO
# and carry no evidence value; the journal has a fixed size cap on the VPS.
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
    # Keep the package at the requested level explicitly: a caller that raised
    # the root level later must not silently mute our own evidence lines.
    package_logger.setLevel(level)
    for name in _QUIET_THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        if third_party.level == logging.NOTSET:
            third_party.setLevel(max(level, logging.WARNING))
