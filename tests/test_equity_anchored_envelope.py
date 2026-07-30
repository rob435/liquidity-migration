"""An envelope that is a fraction of the wallet, and an authority that expires."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.equity_anchored_envelope import EquityAnchoredEnvelope
from liquidity_migration.operational_profile import (
    load_operational_profile_bytes,
    profile_at_capital_reference,
)
from liquidity_migration.operational_runtime_authority import (
    REAL_MONEY_MAX_AUTHORITY_SECONDS,
    REAL_MONEY_PROFILE,
    real_money_gross_ceiling_usdt,
    require_book_within_receipt_ceiling,
    require_real_money_authority_unexpired,
)

REPO = Path(__file__).resolve().parents[1]


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


# --------------------------------------------------------------------------
# Real-money authority: a mandatory ceiling and a mandatory expiry.
# --------------------------------------------------------------------------


def _receipt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "profile": REAL_MONEY_PROFILE,
        "created_ts_ns": 1_000_000_000_000,
        "expires_ts_ns": 1_000_000_000_000 + 7 * 24 * 3600 * 10**9,
        "capital_ceiling": {"mode": "account_equity_multiple", "value": 1.0},
    }
    payload.update(overrides)
    return payload


def test_an_equity_multiple_ceiling_tracks_the_wallet() -> None:
    receipt = _receipt()
    assert real_money_gross_ceiling_usdt(receipt, equity_usdt=4_000.0) == pytest.approx(4_000.0)
    assert real_money_gross_ceiling_usdt(receipt, equity_usdt=9_000.0) == pytest.approx(9_000.0)

    require_book_within_receipt_ceiling(
        receipt, gross_notional_usdt=3_999.0, equity_usdt=4_000.0
    )
    with pytest.raises(RuntimeError, match="exceeds the authorized ceiling"):
        require_book_within_receipt_ceiling(
            receipt, gross_notional_usdt=4_001.0, equity_usdt=4_000.0
        )


def test_a_fixed_ceiling_ignores_equity() -> None:
    receipt = _receipt(capital_ceiling={"mode": "fixed_usdt", "value": 2_500.0})
    assert real_money_gross_ceiling_usdt(receipt, equity_usdt=1_000_000.0) == pytest.approx(2_500.0)


def test_there_is_no_form_of_the_ceiling_that_means_unbounded() -> None:
    for bogus in (
        None,
        {},
        {"mode": "fixed_usdt"},
        {"mode": "none", "value": 1.0},
        {"mode": "fixed_usdt", "value": 0.0},
        {"mode": "fixed_usdt", "value": -1.0},
        {"mode": "account_equity_multiple", "value": 5.0},
        {"mode": "fixed_usdt", "value": 1.0, "extra": True},
    ):
        with pytest.raises(ValueError):
            real_money_gross_ceiling_usdt(_receipt(capital_ceiling=bogus), equity_usdt=1_000.0)


def test_real_money_authority_is_never_indefinite() -> None:
    receipt = _receipt()
    require_real_money_authority_unexpired(receipt, now_ns=receipt["expires_ts_ns"] - 1)  # type: ignore[operator]
    with pytest.raises(RuntimeError, match="expired"):
        require_real_money_authority_unexpired(receipt, now_ns=receipt["expires_ts_ns"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive expires_ts_ns"):
        require_real_money_authority_unexpired(_receipt(expires_ts_ns=0))
    assert REAL_MONEY_MAX_AUTHORITY_SECONDS <= 30 * 24 * 60 * 60


def test_the_checks_are_inert_on_a_demo_receipt() -> None:
    demo = {"profile": "operational"}
    require_real_money_authority_unexpired(demo)
    require_book_within_receipt_ceiling(demo, gross_notional_usdt=1e12, equity_usdt=1.0)


def test_the_mainnet_profile_is_partitioned_and_equity_anchored() -> None:
    """The committed mainnet envelope holds no hard money amount."""

    from liquidity_migration.operational_profile import load_operational_profile

    profile = load_operational_profile(REPO / "configs" / "operational.mainnet.json")
    reference = profile.capital_reference_usdt

    assert profile.capital_reference.tracks_equity
    assert profile.capital_reference.equity_fraction == 1.0
    # The owner's decision: initial margin <= equity, gross up to 2x.
    assert profile.account_risk.max_initial_margin_usdt == pytest.approx(reference)
    assert profile.account_risk.max_account_gross_notional_usdt == pytest.approx(2 * reference)
    assert profile.account_risk.max_daily_loss_usdt == pytest.approx(0.1 * reference)
    assert profile.account_risk.max_leverage == 2.0
    # CARRY and LONG both carry real size now that B3 partitions the envelope.
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
        assert scaled.account_risk.max_account_gross_notional_usdt == pytest.approx(2 * equity)


def test_the_mainnet_partition_is_a_real_partition() -> None:
    """B3: no sleeve can spend another's share, and the shares fit the account."""

    from liquidity_migration.operational_profile import load_operational_profile

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
    # Neither funded sleeve may reach the whole envelope on its own -- that is
    # the entire content of B3.
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


def test_the_producer_clamp_is_disabled_when_the_ceiling_tracks_equity() -> None:
    source = (REPO / "liquidity_migration" / "cli.py").read_text(encoding="utf-8")
    assert "if operational_profile.capital_reference.tracks_equity" in source
