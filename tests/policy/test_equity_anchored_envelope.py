"""An envelope that is a fraction of the wallet, and an authority that expires."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.policy.equity_anchored_envelope import EquityAnchoredEnvelope
from liquidity_migration.policy.operational_profile import (
    load_operational_profile_bytes,
    profile_at_capital_reference,
)

REPO = Path(__file__).resolve().parents[2]


def _profile_bytes(**overrides: object) -> bytes:
    payload = json.loads((REPO / "configs" / "operational.demo.json").read_text())
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def _equity_profile(**capital_reference: object):
    reference = {
        "mode": "account_equity",
        "equity_fraction": 1.0,
        "floor_usdt": 100.0,
        "expand_dead_band_fraction": 0.05,
    }
    reference.update(capital_reference)
    return load_operational_profile_bytes(_profile_bytes(capital_reference=reference))


def test_a_profile_without_the_block_keeps_the_historical_fixed_reference() -> None:
    profile = load_operational_profile_bytes(_profile_bytes())
    assert not profile.capital_reference.tracks_equity
    envelope = EquityAnchoredEnvelope(profile)
    assert envelope.observe_equity(1.0) is None
    assert envelope.reference_usdt == profile.capital_reference_usdt


def test_rescaling_preserves_every_ratio_and_leaves_scale_free_fields_alone() -> None:
    profile = load_operational_profile_bytes(_profile_bytes())
    rescaled = profile_at_capital_reference(profile, profile.capital_reference_usdt / 100.0)

    assert rescaled.capital_reference_usdt == pytest.approx(2_500.0)
    for name in (
        "max_component_gross_notional_usdt",
        "max_account_gross_notional_usdt",
        "max_symbol_notional_usdt",
        "max_initial_margin_usdt",
    ):
        assert getattr(rescaled.account_risk, name) == pytest.approx(
            getattr(profile.account_risk, name) / 100.0
        )
    # Scale-free by construction, and deliberately untouched.
    assert rescaled.account_risk.max_leverage == profile.account_risk.max_leverage
    assert rescaled.account_risk.quantity_tolerance == profile.account_risk.quantity_tolerance
    # The envelope proof is re-run at the new reference, not argued.
    assert rescaled.carry.notional_multiplier == profile.carry.notional_multiplier


def test_the_reference_follows_equity_down_immediately() -> None:
    envelope = EquityAnchoredEnvelope(_equity_profile())
    start = envelope.reference_usdt

    rebase = envelope.observe_equity(start * 0.99)

    assert rebase is not None
    assert rebase.direction == "contract"
    assert envelope.reference_usdt == pytest.approx(start * 0.99)
    assert envelope.policy().max_account_gross_notional_usdt == pytest.approx(
        start * 0.99 * 2.0
    )


def test_expansion_waits_for_a_move_larger_than_the_dead_band() -> None:
    """Equity wander must not re-scale the caps every cycle."""

    envelope = EquityAnchoredEnvelope(_equity_profile())
    start = envelope.reference_usdt

    assert envelope.observe_equity(start * 1.02) is None
    assert envelope.reference_usdt == start

    rebase = envelope.observe_equity(start * 1.20)
    assert rebase is not None and rebase.direction == "expand"
    assert envelope.reference_usdt == pytest.approx(start * 1.20)


@pytest.mark.parametrize("equity", [None, 0.0, -1.0, float("nan"), float("inf"), "junk"])
def test_unknown_equity_moves_nothing(equity: object) -> None:
    """Contracting on unknown data would be a blind action taken on no evidence."""

    envelope = EquityAnchoredEnvelope(_equity_profile())
    start = envelope.reference_usdt

    assert envelope.observe_equity(equity) is None  # type: ignore[arg-type]
    assert envelope.reference_usdt == start


def test_the_floor_bounds_the_envelope_rather_than_collapsing_it() -> None:
    envelope = EquityAnchoredEnvelope(_equity_profile(floor_usdt=500.0))

    rebase = envelope.observe_equity(1.0)

    assert rebase is not None
    assert envelope.reference_usdt == pytest.approx(500.0)
    assert envelope.policy().max_symbol_notional_usdt > 0.0


def test_an_equity_fraction_can_hold_the_book_below_the_wallet() -> None:
    envelope = EquityAnchoredEnvelope(_equity_profile(equity_fraction=0.25))
    envelope.observe_equity(10_000.0)
    assert envelope.reference_usdt == pytest.approx(2_500.0)


def test_the_profile_refuses_an_unbounded_or_oversized_anchor() -> None:
    with pytest.raises(ValueError, match="floor_usdt must be positive"):
        _equity_profile(floor_usdt=0.0)
    with pytest.raises(ValueError, match="equity_fraction cannot exceed 1"):
        _equity_profile(equity_fraction=1.5)
    with pytest.raises(ValueError, match="mode must be"):
        _equity_profile(mode="whatever")


def test_the_mainnet_profile_is_partitioned_and_equity_anchored() -> None:
    """The committed mainnet envelope holds no hard money amount."""

    from liquidity_migration.policy.operational_profile import load_operational_profile

    profile = load_operational_profile(REPO / "configs" / "operational.mainnet.json")
    reference = profile.capital_reference_usdt

    assert profile.capital_reference.tracks_equity
    assert profile.capital_reference.equity_fraction == 1.0
    # The account cap is exactly what the two sleeve dials plus the retired
    # token share need: (1.0 carry + 0.75 long) / 0.99, inside the 2x ceiling.
    gross_multiple = (1.0 + 0.75) / 0.99
    assert profile.account_risk.max_initial_margin_usdt == pytest.approx(reference)
    assert profile.account_risk.max_account_gross_notional_usdt == pytest.approx(
        gross_multiple * reference
    )
    assert profile.account_risk.max_account_gross_notional_usdt <= 2 * reference
    assert profile.account_risk.max_daily_loss_usdt == pytest.approx(0.1 * reference)
    assert profile.account_risk.max_leverage == 2.0
    # CARRY and LONG both carry real size because the envelope is partitioned.
    # CONTINUOUS is retired and shrunk to a token envelope rather than removed,
    # because the profile schema requires all three blocks.
    assert profile.carry.notional_multiplier == 1.0
    assert profile.long.notional_multiplier > 0.1
    assert profile.continuous.notional_multiplier <= 0.001
    assert profile.continuous.max_active == 1

    # Every cap is a ratio, so the whole envelope follows the wallet.
    for equity in (200.0, 2_500.0, 40_000.0):
        scaled = profile_at_capital_reference(profile, equity)
        assert scaled.account_risk.max_initial_margin_usdt == pytest.approx(equity)
        assert scaled.account_risk.max_account_gross_notional_usdt == pytest.approx(
            gross_multiple * equity
        )


def test_the_mainnet_partition_is_a_real_partition() -> None:
    """B3: no sleeve can spend another's share, and the shares fit the account."""

    from liquidity_migration.policy.operational_profile import load_operational_profile

    profile = load_operational_profile(REPO / "configs" / "operational.mainnet.json")
    risk = profile.account_risk
    shares = {limit.sleeve: limit for limit in risk.sleeve_limits}

    assert set(shares) == {"carry", "continuous", "long"}
    assert sum(limit.max_gross_notional_usdt for limit in risk.sleeve_limits) <= (
        risk.max_account_gross_notional_usdt
    )
    assert sum(limit.max_initial_margin_usdt for limit in risk.sleeve_limits) <= (
        risk.max_initial_margin_usdt
    )
    # Neither funded sleeve may reach the whole envelope on its own.
    for sleeve in ("carry", "long"):
        assert shares[sleeve].max_gross_notional_usdt < risk.max_account_gross_notional_usdt
    # Retired CONTINUOUS has no mainnet unit; a token share, not an exemption.
    assert shares["continuous"].max_gross_notional_usdt < 0.02 * (
        risk.max_account_gross_notional_usdt
    )

    # The partition is a ratio like every other cap, so it follows the wallet.
    scaled = profile_at_capital_reference(profile, 10 * profile.capital_reference_usdt)
    scaled_shares = {limit.sleeve: limit for limit in scaled.account_risk.sleeve_limits}
    for sleeve, limit in shares.items():
        assert scaled_shares[sleeve].max_gross_notional_usdt == pytest.approx(
            10 * limit.max_gross_notional_usdt
        )


def test_rebasing_at_the_exact_gross_cap_boundary_survives_rounding_noise() -> None:
    """2026-08-04 00:03 UTC: the funded owner refused a one-cent contraction.

    The mainnet dials pin the gross cap at exactly reference * max_leverage and
    the margin cap at exactly the reference, and a rebase re-derives both by
    multiplication — so without an allowance, roughly one cent value in ten
    lands a rounding step above its bound and fails the proof.
    """

    from liquidity_migration.policy.operational_profile import load_operational_profile

    profile = load_operational_profile(REPO / "configs" / "operational.mainnet.json")
    for cents in range(141_690, 141_720):
        rescaled = profile_at_capital_reference(profile, cents / 100.0)
        assert rescaled.capital_reference_usdt == pytest.approx(cents / 100.0)


def test_the_producer_clamp_is_disabled_when_the_ceiling_tracks_equity() -> None:
    """Evaluate the branch on both shipped profiles.

    This used to grep ``cli/commands.py`` for the text of the if-expression,
    which passes with the arms swapped — and swapping them ships the fixed
    clamp on the funded mainnet profile, which governs how much notional the
    carry producer may target on real money.
    """

    from liquidity_migration.cli.commands import producer_capital_reference_usdt
    from liquidity_migration.policy.operational_profile import load_operational_profile

    funded = load_operational_profile(REPO / "configs" / "operational.mainnet.json")
    assert funded.capital_reference.tracks_equity is True
    assert producer_capital_reference_usdt(funded) == 0.0

    fixed = load_operational_profile(REPO / "configs" / "operational.demo.json")
    assert fixed.capital_reference.tracks_equity is False
    assert producer_capital_reference_usdt(fixed) == pytest.approx(
        fixed.capital_reference_usdt
    )
    assert producer_capital_reference_usdt(fixed) > 0.0
