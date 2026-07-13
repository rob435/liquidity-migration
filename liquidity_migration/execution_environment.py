"""Explicit forward-execution environment contract.

Strategy producers never decide whether they may submit orders.  They publish
targets to exactly one account owner selected by this value; the owner adapter
is the only layer with venue-mutation authority.
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionEnvironment(StrEnum):
    DEMO = "demo"
    PAPER = "paper"


def execution_environment(value: object) -> ExecutionEnvironment:
    """Parse one explicit ``demo|paper`` value and reject every fallback."""

    text = str(value or "").strip().lower()
    try:
        return ExecutionEnvironment(text)
    except ValueError as exc:
        raise ValueError(
            "execution_environment must be explicitly set to 'demo' or 'paper'"
        ) from exc


def account_id_for_environment(value: object) -> str:
    environment = execution_environment(value)
    return (
        "bybit-demo-unified"
        if environment is ExecutionEnvironment.DEMO
        else "bybit-paper-unified"
    )
