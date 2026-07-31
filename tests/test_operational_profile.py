from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.policy.account_execution_config import load_risk_policy_bytes
from liquidity_migration.policy.operational_profile import load_operational_profile_bytes


PROFILE_PATH = Path(__file__).resolve().parents[1] / "configs" / "operational.demo.json"


def _payload() -> dict[str, object]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode()


def test_tracked_operational_profile_is_coherent_and_feeds_account_owner() -> None:
    data = PROFILE_PATH.read_bytes()
    profile = load_operational_profile_bytes(data)
    policy = load_risk_policy_bytes(data)

    assert profile.long.entry_leverage == 2.0
    assert profile.continuous.entry_leverage == 2.0
    assert profile.hedge.entry_leverage == 2.0
    assert policy == profile.account_risk.to_policy()


@pytest.mark.parametrize("producer", ("long", "continuous", "hedge"))
def test_profile_rejects_producer_leverage_above_owner_cap(producer: str) -> None:
    payload = _payload()
    section = payload[producer]
    assert isinstance(section, dict)
    section["entry_leverage"] = 3.0

    with pytest.raises(ValueError, match="producer leverage exceeds"):
        load_operational_profile_bytes(_bytes(payload))


def test_profile_rejects_old_10x_continuous_exposure_envelope() -> None:
    payload = _payload()
    continuous = payload["continuous"]
    assert isinstance(continuous, dict)
    continuous["notional_multiplier"] = 10.0

    with pytest.raises(ValueError, match="envelope exceeds"):
        load_operational_profile_bytes(_bytes(payload))


def test_profile_rejects_unknown_fields_instead_of_ignoring_typos() -> None:
    payload = _payload()
    long = payload["long"]
    assert isinstance(long, dict)
    long["entry_leverge"] = long["entry_leverage"]

    with pytest.raises(ValueError, match="unknown fields: entry_leverge"):
        load_operational_profile_bytes(_bytes(payload))
