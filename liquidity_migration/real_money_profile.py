"""Render the real-money operational profile from one env file of dials.

The owner asked for the deployment knobs — leverage, sleeve shares, the stop
distance — to live in the environment file they already have to edit, next to
the credentials, instead of in a JSON document they have to hand-calibrate.
That is an ergonomics change and deliberately **not** a safety change, so it is
built as a renderer rather than as a runtime override:

``env dials  ->  rendered profile bytes  ->  the same load-time proof  ->  SHA-256 in the receipt``

Every property the guards depend on survives that shape:

* The account owner and every producer still read *one* profile file, and its
  bytes are still hashed into the authority receipt. Limits cannot change
  without invalidating authority — changing a dial changes the rendered bytes,
  which is exactly what re-issuance is for.
* No dial reaches the kernel directly. A combination that produces an envelope
  the proof rejects is refused at render time with the proof's own message, so
  the failure lands on the owner's terminal instead of on a live book.
* The committed ``configs/operational.mainnet.json`` is the render of the
  defaults below, checked by a test. The file and the renderer cannot drift.

The dials are ratios, never money. The capital reference tracks observed venue
equity (B4), so "how much" is answered by the wallet and these answer "in what
proportion".
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from typing import Any, Mapping

from .operational_profile import (
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

#: Every dial is one environment variable named ``RM_<FIELD>`` in upper case.
REAL_MONEY_DIAL_PREFIX = "RM_"

#: The retired CONTINUOUS sleeve has no mainnet unit and no share to spend, but
#: the profile schema still requires its block and the block cannot be sized to
#: exactly zero. It therefore gets a token partition rather than an
#: unpartitioned exemption: bounded at a fraction of a percent instead of at
#: the whole envelope.
_CONTINUOUS_NOTIONAL_MULTIPLIER = 0.001
_CONTINUOUS_GROSS_SHARE = 0.01

#: Hard ceiling on the leverage dial. Matches the real-money authority
#: receipt's own ``account_equity_multiple`` bound: above 2x the wallet, what
#: stops the book is the venue's margin engine rather than anything recorded
#: here, and a limit that does not bind is theatre.
MAX_REAL_MONEY_LEVERAGE = 2.0


@dataclass(frozen=True, slots=True)
class RealMoneyDials:
    """Owner-facing deployment dials, all dimensionless.

    ``*_share`` values are fractions of the account gross cap and must leave the
    partition summing inside it. ``*_fraction`` values are fractions of the
    capital reference, which tracks observed equity.
    """

    #: Fraction of observed wallet equity the whole envelope is scaled to.
    equity_fraction: float = 1.0
    #: Reference floor, in USDT, so a momentarily unreadable or near-zero
    #: balance cannot produce a degenerate envelope. The one dial that is a
    #: money amount, because a floor has no ratio to be a fraction of.
    equity_floor_usdt: float = 100.0
    #: Expansion-only dead band; contraction always follows equity down.
    expand_dead_band_fraction: float = 0.05

    #: Hard leverage ceiling the owner enforces on every target.
    max_leverage: float = 2.0
    #: Leverage the producers actually request. Never above ``max_leverage``.
    entry_leverage: float = 2.0

    #: Account gross notional cap, as a multiple of the reference.
    account_gross_multiple: float = 2.0
    #: Per-symbol notional cap, as a fraction of the reference.
    symbol_notional_fraction: float = 0.5
    #: Initial-margin cap, as a fraction of the reference. At 1.0 the book's
    #: margin can reach but not exceed the wallet.
    initial_margin_fraction: float = 1.0
    #: Daily realised-loss halt, as a fraction of the reference.
    daily_loss_fraction: float = 0.1

    #: B3 partition. Shares of the account gross cap; CARRY plus LONG plus the
    #: token CONTINUOUS share must not exceed 1.
    carry_gross_share: float = 0.55
    long_gross_share: float = 0.40

    carry_notional_multiplier: float = 1.0
    carry_stop_loss_fraction: float = 0.35
    carry_max_new_entries_per_cycle: int = 10

    long_notional_multiplier: float = 0.4
    long_max_new_entries_per_cycle: int = 5
    #: Worst-case full-book initial margin the LONG producer will accept for
    #: itself, as a fraction of equity. The owner's partition binds first; this
    #: is the producer's own refusal.
    long_max_projected_initial_margin_pct_equity: float = 0.5


def dial_environment_keys() -> tuple[str, ...]:
    """Every ``RM_*`` variable this renderer reads, in declaration order."""

    return tuple(f"{REAL_MONEY_DIAL_PREFIX}{field.name.upper()}" for field in fields(RealMoneyDials))


def parse_real_money_dials(environment: Mapping[str, str]) -> RealMoneyDials:
    """Read the dials out of one environment mapping.

    An absent variable takes the committed default. A *present but unreadable*
    one is an error rather than a silent fall back to the default: an operator
    who typed ``RM_MAX_LEVERAGE=2x`` meant to change the leverage, and running
    at the default while believing otherwise is the failure mode this whole
    module exists to avoid.
    """

    values: dict[str, Any] = {}
    for field in fields(RealMoneyDials):
        key = f"{REAL_MONEY_DIAL_PREFIX}{field.name.upper()}"
        if key not in environment:
            continue
        raw = str(environment[key]).strip()
        if not raw:
            raise ValueError(f"{key} is present but empty; remove it to take the default")
        if field.type is int or field.type == "int":
            try:
                values[field.name] = int(raw)
            except ValueError as exc:
                raise ValueError(f"{key} must be an integer; got {raw!r}") from exc
            continue
        try:
            number = float(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be numeric; got {raw!r}") from exc
        if not math.isfinite(number):
            raise ValueError(f"{key} must be finite; got {raw!r}")
        values[field.name] = number
    return RealMoneyDials(**values)


def _validate_dials(dials: RealMoneyDials) -> None:
    """Refuse a dial set before it can produce a profile at all.

    These are the checks the profile proof cannot make for us, because by the
    time it runs the dials have already been turned into absolute numbers and
    the *intent* — "this is a partition", "producers stay under the ceiling" —
    is no longer visible.
    """

    positive = {
        "equity_fraction": dials.equity_fraction,
        "equity_floor_usdt": dials.equity_floor_usdt,
        "max_leverage": dials.max_leverage,
        "entry_leverage": dials.entry_leverage,
        "account_gross_multiple": dials.account_gross_multiple,
        "symbol_notional_fraction": dials.symbol_notional_fraction,
        "initial_margin_fraction": dials.initial_margin_fraction,
        "daily_loss_fraction": dials.daily_loss_fraction,
        "carry_gross_share": dials.carry_gross_share,
        "long_gross_share": dials.long_gross_share,
        "carry_notional_multiplier": dials.carry_notional_multiplier,
        "carry_stop_loss_fraction": dials.carry_stop_loss_fraction,
        "long_notional_multiplier": dials.long_notional_multiplier,
        "long_max_projected_initial_margin_pct_equity": (
            dials.long_max_projected_initial_margin_pct_equity
        ),
    }
    for name, value in sorted(positive.items()):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{REAL_MONEY_DIAL_PREFIX}{name.upper()} must be finite and positive")
    for name, value in (
        ("carry_max_new_entries_per_cycle", dials.carry_max_new_entries_per_cycle),
        ("long_max_new_entries_per_cycle", dials.long_max_new_entries_per_cycle),
    ):
        if type(value) is not int or value <= 0:
            raise ValueError(
                f"{REAL_MONEY_DIAL_PREFIX}{name.upper()} must be a positive integer"
            )
    if dials.equity_fraction > 1.0:
        raise ValueError("RM_EQUITY_FRACTION cannot exceed 1: the wallet is the envelope")
    if dials.max_leverage > MAX_REAL_MONEY_LEVERAGE:
        # Every other cap is expressed as a multiple of the reference and
        # bounded by this one, so an unbounded leverage dial is an unbounded
        # notional envelope wearing ratios. Above this the venue's liquidation
        # engine, not the profile, is what stops the book.
        raise ValueError(
            f"RM_MAX_LEVERAGE cannot exceed {MAX_REAL_MONEY_LEVERAGE:g} on a funded account"
        )
    if dials.expand_dead_band_fraction < 0.0 or dials.expand_dead_band_fraction >= 1.0:
        raise ValueError("RM_EXPAND_DEAD_BAND_FRACTION must sit in [0, 1)")
    if dials.entry_leverage > dials.max_leverage:
        raise ValueError("RM_ENTRY_LEVERAGE cannot exceed RM_MAX_LEVERAGE")
    if dials.initial_margin_fraction > 1.0:
        raise ValueError(
            "RM_INITIAL_MARGIN_FRACTION cannot exceed 1: margin above the wallet is the "
            "venue's liquidation engine, not a limit"
        )
    if dials.account_gross_multiple > dials.max_leverage:
        raise ValueError(
            "RM_ACCOUNT_GROSS_MULTIPLE cannot exceed RM_MAX_LEVERAGE; gross above "
            "reference x leverage is not reachable within the margin the account has"
        )
    if dials.account_gross_multiple > dials.entry_leverage * dials.initial_margin_fraction + 1e-12:
        # Holding this much gross at this leverage would post more margin than
        # the margin cap allows. The sleeve-level proof would catch it, but it
        # would name a sleeve rather than the dial the owner has to move.
        raise ValueError(
            f"RM_ACCOUNT_GROSS_MULTIPLE ({dials.account_gross_multiple:g}) cannot exceed "
            f"RM_ENTRY_LEVERAGE x RM_INITIAL_MARGIN_FRACTION "
            f"({dials.entry_leverage:g} x {dials.initial_margin_fraction:g}); holding that "
            "much gross at that leverage would post more margin than the cap allows"
        )
    if dials.daily_loss_fraction > 1.0:
        raise ValueError("RM_DAILY_LOSS_FRACTION cannot exceed 1")
    if not 0.0 < dials.carry_stop_loss_fraction < 1.0:
        raise ValueError("RM_CARRY_STOP_LOSS_FRACTION must sit in (0, 1)")
    if dials.long_max_projected_initial_margin_pct_equity > 1.0:
        raise ValueError("RM_LONG_MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY cannot exceed 1")
    shares = dials.carry_gross_share + dials.long_gross_share + _CONTINUOUS_GROSS_SHARE
    if shares > 1.0 + 1e-12:
        raise ValueError(
            "RM_CARRY_GROSS_SHARE + RM_LONG_GROSS_SHARE must leave room for the "
            f"{_CONTINUOUS_GROSS_SHARE:g} retired-CONTINUOUS share and sum to at most 1; "
            f"got {shares:g}"
        )


def render_real_money_profile_json(
    dials: RealMoneyDials | None = None,
    *,
    capital_reference_usdt: float = 2_500.0,
) -> dict[str, Any]:
    """Build the profile document. ``capital_reference_usdt`` is only a scale.

    The reference is a declared starting scale for the load-time proof, not a
    limit: ``capital_reference.mode = account_equity`` makes the runtime
    reference track the wallet, and every number below is a ratio of it.
    """

    dials = RealMoneyDials() if dials is None else dials
    _validate_dials(dials)
    reference = float(capital_reference_usdt)
    if not math.isfinite(reference) or reference <= 0.0:
        raise ValueError("capital_reference_usdt must be finite and positive")
    if dials.equity_floor_usdt > reference:
        raise ValueError("RM_EQUITY_FLOOR_USDT cannot exceed the declared capital reference")

    account_gross = reference * dials.account_gross_multiple
    margin_cap = reference * dials.initial_margin_fraction

    def _share(gross_share: float) -> dict[str, float]:
        # The same fraction of each account cap. Deriving the margin share as
        # ``gross / entry_leverage`` instead looks natural and is wrong: below
        # ``account_gross_multiple`` leverage the shares then sum *above* the
        # account margin cap, so what the profile calls a partition would not
        # be one. Taking the same fraction of both caps makes "the shares sum
        # inside the account" true by construction, at any leverage.
        return {
            "max_gross_notional_usdt": account_gross * gross_share,
            "max_initial_margin_usdt": margin_cap * gross_share,
        }

    return {
        "schema_version": OPERATIONAL_PROFILE_SCHEMA_VERSION,
        "kind": OPERATIONAL_PROFILE_KIND,
        "capital_reference_usdt": reference,
        "capital_reference": {
            "mode": "account_equity",
            "equity_fraction": dials.equity_fraction,
            "floor_usdt": dials.equity_floor_usdt,
            "expand_dead_band_fraction": dials.expand_dead_band_fraction,
        },
        "account_risk": {
            "max_component_gross_notional_usdt": account_gross,
            "max_account_gross_notional_usdt": account_gross,
            "max_symbol_notional_usdt": reference * dials.symbol_notional_fraction,
            "max_initial_margin_usdt": margin_cap,
            "max_leverage": dials.max_leverage,
            "quantity_tolerance": 1e-12,
            "max_daily_loss_usdt": reference * dials.daily_loss_fraction,
            "sleeve_limits": {
                "carry": _share(dials.carry_gross_share),
                "continuous": _share(_CONTINUOUS_GROSS_SHARE),
                "long": _share(dials.long_gross_share),
            },
        },
        "long": {
            "notional_multiplier": dials.long_notional_multiplier,
            "entry_leverage": dials.entry_leverage,
            "max_projected_initial_margin_pct_equity": (
                dials.long_max_projected_initial_margin_pct_equity
            ),
            "max_order_notional_pct_equity": 0.0,
            "max_new_entries_per_cycle": dials.long_max_new_entries_per_cycle,
        },
        "continuous": {
            "max_active": 1,
            "max_new_entries_per_cycle": 1,
            "btc_trend_gate": "uptrend",
            "entry_leverage": dials.entry_leverage,
            "notional_multiplier": _CONTINUOUS_NOTIONAL_MULTIPLIER,
            "per_position_notional_pct_equity": 2.0,
        },
        "carry": {
            "notional_multiplier": dials.carry_notional_multiplier,
            "entry_leverage": dials.entry_leverage,
            "declared_stop_loss_fraction": dials.carry_stop_loss_fraction,
            "max_new_entries_per_cycle": dials.carry_max_new_entries_per_cycle,
        },
        "hedge": {"entry_leverage": dials.entry_leverage},
    }


def render_real_money_profile(
    dials: RealMoneyDials | None = None,
    *,
    capital_reference_usdt: float = 2_500.0,
) -> tuple[bytes, OperationalProfile]:
    """Render and immediately prove. Returns the exact bytes to install.

    Rendering without proving would let a dial set reach disk and fail later,
    at owner start-up, over a funded account. It fails here instead.
    """

    document = render_real_money_profile_json(
        dials, capital_reference_usdt=capital_reference_usdt
    )
    data = (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8")
    return data, load_operational_profile_bytes(data)
