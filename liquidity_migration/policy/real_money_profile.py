"""Render the real-money operational profile from one env file of dials.

The owner surface is two numbers: a leverage multiple per sleeve. Everything
else in the profile — the account caps, the sleeve partition, margin ceilings,
the daily loss halt, stops — is derived from them here and still walks the
full load-time proof. A retired dial left in the env file is refused by name;
nothing is silently ignored.

The dials are ratios, never money: the capital reference tracks observed venue
equity, so the wallet answers "how much" and these answer "in what proportion".
``configs/operational.mainnet.json`` is the render of the defaults below, held
to that by a test.
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
    "MAX_REAL_MONEY_LEVERAGE",
    "RealMoneyDials",
    "dial_environment_keys",
    "long_worst_case_upscale",
    "parse_real_money_dials",
    "render_real_money_profile",
    "render_real_money_profile_json",
]

#: Every dial is one environment variable named ``RM_<FIELD>`` in upper case.
REAL_MONEY_DIAL_PREFIX = "RM_"

#: The retired CONTINUOUS sleeve has no mainnet unit, but the profile schema
#: requires its block and the block cannot be sized to exactly zero.
_CONTINUOUS_NOTIONAL_MULTIPLIER = 0.001
_CONTINUOUS_GROSS_SHARE = 0.01

#: Ceiling on total account gross, as a multiple of the wallet: the two
#: sleeve dials may sum to at most ``10.0 * (1 - _CONTINUOUS_GROSS_SHARE)``
#: = 9.9. Owner's dial, owner's risk — but be clear what protects the book
#: up there: the loss halt fires on realised loss and the envelope on
#: observed equity, while an open position's drawdown meets the venue's
#: liquidation engine first. At 10x gross a ~10% adverse move on the book
#: is the wallet.
MAX_REAL_MONEY_LEVERAGE = 10.0

# Derived constants the old dial surface used to expose. Fixed on purpose:
# none of them is a sizing decision, and every one is still enforced.
_ENTRY_LEVERAGE = 2.0  # venue margin leverage floor requested per order
_EQUITY_FLOOR_USDT = 100.0  # reference floor against unreadable balances
_EXPAND_DEAD_BAND_FRACTION = 0.05  # envelope expands only past this band
_SYMBOL_NOTIONAL_FRACTION = 0.5  # largest single-symbol position
_CARRY_MAX_NEW_ENTRIES_PER_CYCLE = 10
_LONG_MAX_NEW_ENTRIES_PER_CYCLE = 5


@dataclass(frozen=True, slots=True)
class RealMoneyDials:
    """Owner-facing dials: one leverage multiple per sleeve, two protections.

    Each leverage dial is the most that sleeve's book can reach, as a multiple
    of account equity, with the strategy's own worst-case upscaling
    (volatility and weekend size multipliers) already inside the number.
    Together they may total at most 9.9; past a total of ~2 the venue margin
    leverage the producers request rises with the dials, and the venue's
    liquidation engine becomes the binding backstop on an open book.
    """

    #: Carry book ceiling, x equity. Each name takes up to one tenth of the
    #: dial: at 1.0 a name is up to 10% of equity and the book up to 100%.
    carry_leverage: float = 1.0
    #: LONG book ceiling, x equity, across its 10 slots with the vol/weekend
    #: upscaling included. Each entry is the dial / 18.75 of equity nominally:
    #: 0.75 = 4% per entry, 1.875 = 10% per entry.
    long_leverage: float = 0.75
    #: Daily realised-loss halt, as a fraction of equity. Trips a flatten.
    daily_loss_fraction: float = 0.1
    #: Venue-native disaster-stop distance on carry entries, armed with the
    #: entry. Wide on purpose: the funding-normalisation exit is the intended
    #: exit; this only covers the case where nothing local is running.
    carry_stop_loss_fraction: float = 0.35


def dial_environment_keys() -> tuple[str, ...]:
    """Every ``RM_*`` variable this renderer reads, in declaration order."""

    return tuple(f"{REAL_MONEY_DIAL_PREFIX}{field.name.upper()}" for field in fields(RealMoneyDials))


def long_worst_case_upscale() -> float:
    """The LONG strategy's own worst-case size upscaling over a nominal entry."""

    from liquidity_migration.research.backtest.long_native import long_v11a_profile  # noqa: PLC0415

    strategy = long_v11a_profile()
    return float(strategy.vol_target_max_scale) * max(1.0, float(strategy.weekend_size_mult))


def parse_real_money_dials(environment: Mapping[str, str]) -> RealMoneyDials:
    """Read the dials out of one environment mapping.

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
            + "; sizing is now RM_CARRY_LEVERAGE and RM_LONG_LEVERAGE only - "
            "delete the old lines"
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


def _validate_dials(dials: RealMoneyDials) -> None:
    """Refuse a dial pair before it can produce a profile."""

    for name, value in (
        ("carry_leverage", dials.carry_leverage),
        ("long_leverage", dials.long_leverage),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(
                f"{REAL_MONEY_DIAL_PREFIX}{name.upper()} must be finite and positive; "
                "mute a sleeve with its sleeves.env toggle, not a zero dial"
            )
    total = dials.carry_leverage + dials.long_leverage
    ceiling = MAX_REAL_MONEY_LEVERAGE * (1.0 - _CONTINUOUS_GROSS_SHARE)
    if total > ceiling + 1e-12:
        raise ValueError(
            f"RM_CARRY_LEVERAGE + RM_LONG_LEVERAGE ({total:g}) cannot exceed {ceiling:g}: "
            f"{MAX_REAL_MONEY_LEVERAGE:g}x the wallet is the ceiling on a funded account, "
            "and the retired-CONTINUOUS token share keeps its 1%"
        )
    if not math.isfinite(dials.daily_loss_fraction) or not 0.0 < dials.daily_loss_fraction <= 1.0:
        raise ValueError("RM_DAILY_LOSS_FRACTION must sit in (0, 1]")
    if not math.isfinite(dials.carry_stop_loss_fraction) or not 0.0 < dials.carry_stop_loss_fraction < 1.0:
        raise ValueError("RM_CARRY_STOP_LOSS_FRACTION must sit in (0, 1)")


def render_real_money_profile_json(
    dials: RealMoneyDials | None = None,
    *,
    capital_reference_usdt: float = 2_500.0,
) -> dict[str, Any]:
    """Build the profile document. ``capital_reference_usdt`` is only a scale.

    It is the starting scale for the load-time proof, not a limit:
    ``capital_reference.mode = account_equity`` makes the runtime reference
    track the wallet, and every number below is a ratio of it.
    """

    dials = RealMoneyDials() if dials is None else dials
    _validate_dials(dials)
    reference = float(capital_reference_usdt)
    if not math.isfinite(reference) or reference <= 0.0:
        raise ValueError("capital_reference_usdt must be finite and positive")
    if _EQUITY_FLOOR_USDT > reference:
        raise ValueError("the equity floor cannot exceed the declared capital reference")

    # The account cap is exactly what the two sleeves plus the retired token
    # share need: sleeve cap = account cap x share = the dial itself.
    account_multiple = (dials.carry_leverage + dials.long_leverage) / (
        1.0 - _CONTINUOUS_GROSS_SHARE
    )
    account_gross = reference * account_multiple
    margin_cap = reference  # margin above the wallet is the venue's business

    # Venue margin leverage scales with the dials: gross above
    # entry-leverage x wallet is physically unreachable, so a book dialled
    # past 2x raises the per-order leverage it requests along with it.
    leverage = max(_ENTRY_LEVERAGE, account_multiple)

    upscale = long_worst_case_upscale()
    long_multiplier = dials.long_leverage / upscale
    # The producer's own refusal sits exactly at the dial: a worst-case full
    # book posts dial / entry-leverage of equity as margin (headroom for the
    # multiplier round-trip above).
    long_margin_cap = min(1.0, dials.long_leverage / leverage + 1e-9)

    # The single-symbol cap must admit each producer's own worst single
    # position, so at high dials it scales with them (never past the account
    # cap; the 0.5 floor keeps the historical bound at modest dials).
    from liquidity_migration.research.backtest.long_native import long_v11a_profile  # noqa: PLC0415
    from liquidity_migration.strategy.carry_demo import load_carry_config  # noqa: PLC0415

    long_strategy = long_v11a_profile()
    long_single = dials.long_leverage * (
        float(long_strategy.gross_exposure)
        / max(int(long_strategy.max_concurrent_positions), 1)
    )
    carry_single = dials.carry_leverage * float(load_carry_config().per_name_cap)
    symbol_fraction = min(
        account_multiple,
        max(_SYMBOL_NOTIONAL_FRACTION, long_single + 1e-9, carry_single + 1e-9),
    )

    def _share(gross_share: float) -> dict[str, float]:
        # The same fraction of *each* account cap, so the shares sum inside the
        # account at any leverage.
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
            "equity_fraction": 1.0,
            "floor_usdt": _EQUITY_FLOOR_USDT,
            "expand_dead_band_fraction": _EXPAND_DEAD_BAND_FRACTION,
        },
        "account_risk": {
            "max_component_gross_notional_usdt": account_gross,
            "max_account_gross_notional_usdt": account_gross,
            "max_symbol_notional_usdt": reference * symbol_fraction,
            "max_initial_margin_usdt": margin_cap,
            "max_leverage": leverage,
            "quantity_tolerance": 1e-12,
            "max_daily_loss_usdt": reference * dials.daily_loss_fraction,
            "sleeve_limits": {
                "carry": _share(dials.carry_leverage / account_multiple),
                "continuous": _share(_CONTINUOUS_GROSS_SHARE),
                "long": _share(dials.long_leverage / account_multiple),
            },
        },
        "long": {
            "notional_multiplier": long_multiplier,
            "entry_leverage": leverage,
            "max_projected_initial_margin_pct_equity": long_margin_cap,
            "max_order_notional_pct_equity": 0.0,
            "max_new_entries_per_cycle": _LONG_MAX_NEW_ENTRIES_PER_CYCLE,
        },
        "continuous": {
            "max_active": 1,
            "max_new_entries_per_cycle": 1,
            "btc_trend_gate": "uptrend",
            "entry_leverage": leverage,
            "notional_multiplier": _CONTINUOUS_NOTIONAL_MULTIPLIER,
            "per_position_notional_pct_equity": 2.0,
        },
        "carry": {
            "notional_multiplier": dials.carry_leverage,
            "entry_leverage": leverage,
            "declared_stop_loss_fraction": dials.carry_stop_loss_fraction,
            "max_new_entries_per_cycle": _CARRY_MAX_NEW_ENTRIES_PER_CYCLE,
        },
        "hedge": {"entry_leverage": leverage},
    }


def render_real_money_profile(
    dials: RealMoneyDials | None = None,
    *,
    capital_reference_usdt: float = 2_500.0,
) -> tuple[bytes, OperationalProfile]:
    """Render, prove, and return the exact bytes to install."""

    document = render_real_money_profile_json(
        dials, capital_reference_usdt=capital_reference_usdt
    )
    data = (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8")
    return data, load_operational_profile_bytes(data)
