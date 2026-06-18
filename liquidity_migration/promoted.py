"""Machine registry for promoted-in-code demo/paper strategy objects.

For the full trading lifecycle (runtime env overrides, entry, sizing, exits,
paper/demo boundaries, and what the official backtests do or do not reproduce),
read ``docs/promoted_trading_logic.md``. This module intentionally stays narrow:
it exposes only the active promoted profile objects used by tooling and tests.
Do not delete it as "stale" without first replacing every importer of
``promoted.PROFILES``, ``promoted.long_profile()``, and
``promoted.continuous_profile()``.

There are two promoted-in-code sleeves: LONG (v11a) and CONTINUOUS (the deployed
fade book including the BTC-vol regime-hedge). The daily SHORT sleeve was erased
from the system by operator order on 2026-06-11.

The CONTINUOUS-fade sleeve was removed from the promoted set on 2026-06-05, then
re-added on 2026-06-15 by explicit operator override. That was not a demo-arbiter
gate pass. It is demo/paper only; ``REAL_MONEY`` stays false and the Tier-3
real-money gate is unmet.

Historical continuous research candidates do not live here. Keep them in
receipts, reports, or git history so this registry cannot be mistaken for a
research manifest archive.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

PROMOTED_TRADING_LOGIC_DOC = "docs/promoted_trading_logic.md"


def _windowed(cfg: Any, start: str | None, end: str | None) -> Any:
    """Return cfg with start_date/end_date overridden when provided."""
    over: dict[str, str] = {}
    if start:
        over["start_date"] = start
    if end:
        over["end_date"] = end
    return replace(cfg, **over) if over else cfg


def long_profile(*, start: str | None = None, end: str | None = None):
    """Return the promoted-in-code LONG v11a strategy profile.

    Source: ``long_native_event_demo._v11a_long_native_config()``. Live/paper env,
    sizing guards, and exit lifecycle details are documented in
    ``docs/promoted_trading_logic.md``.
    """
    from .long_native_event_demo import _v11a_long_native_config

    return _windowed(_v11a_long_native_config(), start, end)


def continuous_profile(*, start: str | None = None, end: str | None = None):
    """Return the promoted-in-code CONTINUOUS frozen portfolio object.

    Source: ``continuous_forward_replay.FROZEN_FORWARD_CONFIG``. This is the
    three-component continuous ensemble plus BTC+ETH 2-factor hedge and BTC-vol
    regime overlay. Returned as a deep copy so callers cannot mutate the frozen
    config.

    The live daemon lifecycle also depends on
    ``continuous_demo.apply_continuous_demo_profile`` and systemd env. The full
    lifecycle source is ``docs/promoted_trading_logic.md``.

    ``start``/``end`` are accepted for interface parity with ``long_profile``.
    Continuous windowing is applied downstream by the equity runner, so these
    values are surfaced under a non-hashed ``_window`` key for caller reference.
    """
    import copy

    from .continuous_forward_replay import FROZEN_FORWARD_CONFIG

    cfg = copy.deepcopy(FROZEN_FORWARD_CONFIG)
    if start or end:
        cfg["_window"] = {"start": start, "end": end}
    return cfg


PROFILES = {
    "long": long_profile,
    "continuous": continuous_profile,
}
