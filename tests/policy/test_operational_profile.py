from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.policy.account_execution_config import load_risk_policy_bytes
from liquidity_migration.policy.operational_profile import load_operational_profile_bytes


PROFILE_PATH = Path(__file__).resolve().parents[2] / "configs" / "operational.demo.json"


def _payload() -> dict[str, object]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode()


def test_tracked_operational_profile_is_coherent_and_feeds_account_owner() -> None:
    data = PROFILE_PATH.read_bytes()
    profile = load_operational_profile_bytes(data)
    policy = load_risk_policy_bytes(data)

    # Risk-on since 2026-08-21 (owner): each new entry sized as large as the
    # envelope admits with both books priced full at worst case — carry 20%
    # of equity per name, LONG ~15% typical — still levered 5x.
    assert profile.long.entry_leverage == 5.0
    assert profile.carry.entry_leverage == 5.0
    assert profile.hedge.entry_leverage == 5.0
    assert profile.long.notional_multiplier == 1.5
    assert profile.carry.notional_multiplier == 2.0
    assert policy == profile.account_risk.to_policy()


@pytest.mark.parametrize("producer", ("long", "carry", "hedge"))
def test_profile_rejects_producer_leverage_above_owner_cap(producer: str) -> None:
    payload = _payload()
    section = payload[producer]
    assert isinstance(section, dict)
    section["entry_leverage"] = 6.0

    with pytest.raises(ValueError, match="producer leverage exceeds"):
        load_operational_profile_bytes(_bytes(payload))


def test_profile_rejects_a_10x_producer_exposure_envelope() -> None:
    payload = _payload()
    carry = payload["carry"]
    assert isinstance(carry, dict)
    carry["notional_multiplier"] = 10.0

    with pytest.raises(ValueError, match="envelope exceeds"):
        load_operational_profile_bytes(_bytes(payload))


def test_profile_refuses_a_retired_sleeve_block() -> None:
    """The retired CONTINUOUS sleeve cannot re-enter the envelope by config.

    An old profile file still carrying the block is refused by name at load
    rather than parsed into a claim on the account caps.
    """

    payload = _payload()
    payload["continuous"] = {
        "max_active": 1,
        "max_new_entries_per_cycle": 1,
        "btc_trend_gate": "uptrend",
        "entry_leverage": 2.0,
        "notional_multiplier": 1.0,
        "per_position_notional_pct_equity": 2.0,
    }

    with pytest.raises(ValueError, match="unknown fields: continuous"):
        load_operational_profile_bytes(_bytes(payload))


def test_a_retired_sleeve_cannot_claim_a_share_of_the_partition() -> None:
    """A sleeve_limits share for the dead sleeve is refused, not honoured."""

    payload = _payload()
    risk = payload["account_risk"]
    assert isinstance(risk, dict)
    risk["sleeve_limits"] = {
        "carry": {"max_gross_notional_usdt": 250_000.0, "max_initial_margin_usdt": 125_000.0},
        "continuous": {"max_gross_notional_usdt": 10_000.0, "max_initial_margin_usdt": 5_000.0},
        "long": {"max_gross_notional_usdt": 234_375.0, "max_initial_margin_usdt": 117_187.5},
    }

    with pytest.raises(ValueError, match="names unknown sleeves: continuous"):
        load_operational_profile_bytes(_bytes(payload))


def test_profile_rejects_unknown_fields_instead_of_ignoring_typos() -> None:
    payload = _payload()
    long = payload["long"]
    assert isinstance(long, dict)
    long["entry_leverge"] = long["entry_leverage"]

    with pytest.raises(ValueError, match="unknown fields: entry_leverge"):
        load_operational_profile_bytes(_bytes(payload))
