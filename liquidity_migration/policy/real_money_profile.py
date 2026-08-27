"""Render the real-money operational profile.

Sizing lives in three env dials the producers read directly
(``CARRY_NOTIONAL_MULTIPLIER``, ``LONG_NOTIONAL_MULTIPLIER``,
``EXODUS_NOTIONAL_MULTIPLIER``). They sit in the file each producer unit loads:
``producer-demo.env`` on demo, ``producer-mainnet.env``
on the funded fleet — never ``bybit-mainnet.env``, which no producer loads.
This module only builds the account document: caps, partition, entry leverage,
and each strategy's default multiplier. It is static — no dial math — so the
committed ``configs/operational.mainnet.json`` is its exact output, held to that
by a test.

The capital reference tracks observed venue equity, so every cap below is a
ratio of the wallet and the declared 100 USDT scale is only a floor. The one
live dial here is ``RM_CARRY_STOP_LOSS_FRACTION``; any other ``RM_*`` line in
an env file is refused by name.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from typing import Any, Mapping

from liquidity_migration.policy.operational_profile import (
    OPERATIONAL_PROFILE_KIND,
    OPERATIONAL_PROFILE_SCHEMA_VERSION,
    OperationalProfile,
    load_operational_profile_bytes,
)

__all__ = [
    "REAL_MONEY_DIAL_PREFIX",
    "RealMoneyDials",
    "dial_environment_keys",
    "parse_real_money_dials",
    "render_real_money_profile",
    "render_real_money_profile_json",
]

#: Every dial this module reads is one environment variable named
#: ``RM_<FIELD>`` in upper case.
REAL_MONEY_DIAL_PREFIX = "RM_"

# Fixed constants, not dials.
_ENTRY_LEVERAGE = 5.0  # venue margin leverage requested per order
_EQUITY_FLOOR_USDT = 100.0  # reference floor against unreadable balances
_EXPAND_DEAD_BAND_FRACTION = 0.05  # envelope expands only past this band
_CARRY_MAX_NEW_ENTRIES_PER_CYCLE = 10
_LONG_MAX_NEW_ENTRIES_PER_CYCLE = 5


@dataclass(frozen=True, slots=True)
class RealMoneyDials:
    """The one live dial on this surface: the carry disaster-stop distance."""

    #: Venue-native disaster-stop distance on carry entries, armed with the
    #: entry. Wide on purpose: the funding-normalisation exit is the intended
    #: exit; this only covers the case where nothing local is running.
    carry_stop_loss_fraction: float = 0.35


def dial_environment_keys() -> tuple[str, ...]:
    """Every ``RM_*`` variable this renderer reads, in declaration order."""

    return tuple(f"{REAL_MONEY_DIAL_PREFIX}{field.name.upper()}" for field in fields(RealMoneyDials))


def parse_real_money_dials(environment: Mapping[str, str]) -> RealMoneyDials:
    """Read the dial out of one environment mapping.

    An absent variable takes the committed default; a present but unparseable
    one raises rather than silently falling back to it, and a retired ``RM_*``
    variable is refused by name so an old env file cannot keep steering by a
    dial that no longer exists.
    """

    known = {f"{REAL_MONEY_DIAL_PREFIX}{field.name.upper()}": field.name for field in fields(RealMoneyDials)}
    retired = sorted(
        key
        for key in environment
        if key.startswith(REAL_MONEY_DIAL_PREFIX) and key not in known
    )
    if retired:
        raise ValueError(
            "retired real-money dial(s) in the env file: "
            + ", ".join(retired)
            + "; sizing is now CARRY_NOTIONAL_MULTIPLIER, LONG_NOTIONAL_MULTIPLIER, "
            "and EXODUS_NOTIONAL_MULTIPLIER in the fleet env files - delete the old lines"
        )
    values: dict[str, Any] = {}
    for key, name in known.items():
        if key not in environment:
            continue
        raw = str(environment[key]).strip()
        if not raw:
            raise ValueError(f"{key} is present but empty; remove it to take the default")
        try:
            number = float(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be numeric; got {raw!r}") from exc
        if not math.isfinite(number):
            raise ValueError(f"{key} must be finite; got {raw!r}")
        values[name] = number
    return RealMoneyDials(**values)


def render_real_money_profile_json(
    dials: RealMoneyDials | None = None,
    *,
    capital_reference_usdt: float = _EQUITY_FLOOR_USDT,
) -> dict[str, Any]:
    """Build the profile document. ``capital_reference_usdt`` is only a scale.

    The declared number sizes the caps in the instant before the first equity
    read; at the floor those caps are the smallest the runtime can ever hold.

    The multipliers below are defaults, not ceilings: the producers' env dials
    override them per fleet without touching this file. The account caps are
    what bounds the BOOK; a book the dials build past them is refused per
    entry by the engine's runtime admission, never silently resized.
    """

    dials = RealMoneyDials() if dials is None else dials
    if not 0.0 < dials.carry_stop_loss_fraction < 1.0:
        raise ValueError("RM_CARRY_STOP_LOSS_FRACTION must sit in (0, 1)")
    reference = float(capital_reference_usdt)
    if not math.isfinite(reference) or reference <= 0.0:
        raise ValueError("capital_reference_usdt must be finite and positive")
    if _EQUITY_FLOOR_USDT > reference:
        raise ValueError("the equity floor cannot exceed the declared capital reference")

    # The gross cap is what the reference funds at entry leverage — reachable,
    # so no load-time proof can call it scenery — and the margin cap is the
    # wallet itself: margin above the wallet is the venue's business.
    account_gross = reference * _ENTRY_LEVERAGE
    margin_cap = reference

    return {
        "schema_version": OPERATIONAL_PROFILE_SCHEMA_VERSION,
        "kind": OPERATIONAL_PROFILE_KIND,
        "capital_reference_usdt": reference,
        "capital_reference": {
            "mode": "account_equity",
            "equity_fraction": 1.0,
            "floor_usdt": _EQUITY_FLOOR_USDT,
            "expand_dead_band_fraction": _EXPAND_DEAD_BAND_FRACTION,
        },
        "account_risk": {
            "max_component_gross_notional_usdt": account_gross,
            "max_account_gross_notional_usdt": account_gross,
            "max_initial_margin_usdt": margin_cap,
            "max_leverage": _ENTRY_LEVERAGE,
            "quantity_tolerance": 1e-12,
        },
        "long": {
            "notional_multiplier": 6.0,
            "entry_leverage": _ENTRY_LEVERAGE,
            "order_notional_pct_equity": 0.0,
            "max_new_entries_per_cycle": _LONG_MAX_NEW_ENTRIES_PER_CYCLE,
        },
        "carry": {
            "notional_multiplier": 3.0,
            "entry_leverage": _ENTRY_LEVERAGE,
            "declared_stop_loss_fraction": dials.carry_stop_loss_fraction,
            "max_new_entries_per_cycle": _CARRY_MAX_NEW_ENTRIES_PER_CYCLE,
        },
        "hedge": {"entry_leverage": _ENTRY_LEVERAGE},
    }


def render_real_money_profile(
    dials: RealMoneyDials | None = None,
    *,
    capital_reference_usdt: float = _EQUITY_FLOOR_USDT,
) -> tuple[bytes, OperationalProfile]:
    """Render, prove, and return the exact bytes to install."""

    document = render_real_money_profile_json(
        dials, capital_reference_usdt=capital_reference_usdt
    )
    data = (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8")
    return data, load_operational_profile_bytes(data)
