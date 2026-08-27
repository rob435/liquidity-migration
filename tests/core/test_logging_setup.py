"""Entrypoint logging must reach stderr with level and logger name.

Service entrypoints run as ``python -m liquidity_migration.<module>``, so their module
logger is ``__main__`` and sits outside the package tree; a package-only handler drops
their INFO records and renders WARNING/ERROR through ``logging.lastResort`` unformatted.
These run in child processes because logging state is process-global and pytest installs
its own root handler, so only a fresh interpreter reproduces what systemd starts.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(body: str, *, env_extra: dict[str, str] | None = None) -> str:
    program = textwrap.dedent(
        """
        import logging
        from liquidity_migration.core.logging_setup import ensure_default_log_handler
        """
    ) + textwrap.dedent(body)
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=None if env_extra is None else {**__import__("os").environ, **env_extra},
    )
    return completed.stderr


def test_entrypoint_main_logger_info_records_are_emitted() -> None:
    # ``__main__`` is exactly what ``getLogger(__name__)`` resolves to in a
    # module started with ``python -m``.
    stderr = _run(
        """
        ensure_default_log_handler()
        logging.getLogger("__main__").info("account Telegram delivered page=%d/%d", 1, 1)
        """
    )
    assert "account Telegram delivered page=1/1" in stderr
    assert "[INFO]" in stderr
    assert "__main__" in stderr


def test_package_logger_records_keep_their_formatting() -> None:
    stderr = _run(
        """
        ensure_default_log_handler()
        logging.getLogger("liquidity_migration.venue.bybit.account_reader").info("stream connected")
        """
    )
    assert "[INFO] liquidity_migration.venue.bybit.account_reader: stream connected" in stderr


def test_existing_logging_setup_is_never_overridden() -> None:
    stderr = _run(
        """
        logging.basicConfig(level=logging.INFO, format="CUSTOM %(message)s")
        ensure_default_log_handler()
        assert len(logging.getLogger().handlers) == 1
        logging.getLogger("__main__").info("caller owns the format")
        """
    )
    assert "CUSTOM caller owns the format" in stderr


def test_noisy_third_party_loggers_stay_at_warning() -> None:
    stderr = _run(
        """
        ensure_default_log_handler()
        logging.getLogger("pybit.unified_trading").info("chatty connection detail")
        logging.getLogger("pybit.unified_trading").warning("connection lost")
        """
    )
    assert "chatty connection detail" not in stderr
    assert "connection lost" in stderr


def test_log_level_override_is_honoured() -> None:
    stderr = _run(
        """
        ensure_default_log_handler()
        logging.getLogger("__main__").info("suppressed detail")
        logging.getLogger("__main__").warning("kept detail")
        """,
        env_extra={"LIQMIG_LOG_LEVEL": "WARNING"},
    )
    assert "suppressed detail" not in stderr
    assert "kept detail" in stderr
