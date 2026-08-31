from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.core.operational_profile import load_operational_profile_bytes


PROFILE_PATH = Path(__file__).resolve().parents[2] / "configs" / "operational.demo.json"


def _payload() -> dict[str, object]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode()


def test_tracked_operational_profile_is_coherent_and_feeds_account_owner() -> None:
    data = PROFILE_PATH.read_bytes()
    profile = load_operational_profile_bytes(data)

    # LONG at 6.0 (base slot 0.10 x 6.0 = 60% of sizing equity per entry,
    # before LONG's own vol/weekend scaling), carry at 3.0 (per-name weight
    # 0.10 x 3.0 = 30% per new name) — still levered 5x.
    assert profile.long.entry_leverage == 5.0
    assert profile.carry.entry_leverage == 5.0
    assert profile.hedge.entry_leverage == 5.0
    assert profile.long.notional_multiplier == 6.0
    assert profile.carry.notional_multiplier == 3.0


def test_retired_daily_loss_field_is_refused() -> None:
    payload = _payload()
    risk = payload["account_risk"]
    assert isinstance(risk, dict)
    risk["max_daily_loss_usdt"] = 10.0
    with pytest.raises(ValueError, match="max_daily_loss_usdt"):
        load_operational_profile_bytes(_bytes(payload))


@pytest.mark.parametrize("sleeve", ("long", "carry", "hedge"))
def test_profile_rejects_strategy_leverage_above_owner_cap(sleeve: str) -> None:
    payload = _payload()
    section = payload[sleeve]
    assert isinstance(section, dict)
    section["entry_leverage"] = 6.0

    with pytest.raises(ValueError, match="strategy leverage exceeds"):
        load_operational_profile_bytes(_bytes(payload))


def test_a_10x_strategy_exposure_envelope_loads_without_a_refusal() -> None:
    """Book size is the owner's dial, not a load-time refusal.

    The multiplier scales the strategy's own weights; per-position risk is
    bounded by each position's venue-native stop, so no envelope projection
    stands between the dial and the native reducer.
    """

    payload = _payload()
    carry = payload["carry"]
    assert isinstance(carry, dict)
    carry["notional_multiplier"] = 10.0

    profile = load_operational_profile_bytes(_bytes(payload))
    assert profile.carry.notional_multiplier == 10.0


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


def test_a_profile_still_declaring_sleeve_shares_is_refused() -> None:
    """There is no per-sleeve capital share, so a document claiming one is wrong.

    Loading it and ignoring the shares would leave an operator believing two
    sleeves are fenced from each other when nothing fences them.
    """

    payload = _payload()
    risk = payload["account_risk"]
    assert isinstance(risk, dict)
    risk["sleeve_limits"] = {
        "carry": {"max_gross_notional_usdt": 250_000.0, "max_initial_margin_usdt": 125_000.0},
        "long": {"max_gross_notional_usdt": 234_375.0, "max_initial_margin_usdt": 117_187.5},
    }

    with pytest.raises(ValueError, match="unknown fields: sleeve_limits"):
        load_operational_profile_bytes(_bytes(payload))


def test_profile_rejects_unknown_fields_instead_of_ignoring_typos() -> None:
    payload = _payload()
    long = payload["long"]
    assert isinstance(long, dict)
    long["entry_leverge"] = long["entry_leverage"]

    with pytest.raises(ValueError, match="unknown fields: entry_leverge"):
        load_operational_profile_bytes(_bytes(payload))


def test_a_profile_still_carrying_the_retired_symbol_cap_is_refused() -> None:
    """The key is gone from the schema, and the loader refuses a key it does
    not read rather than ignoring it -- so an old profile still carrying the
    retired per-symbol cap stops the fleet instead of starting with a cap
    nobody enforces. The twin of the Rust check in
    engine/engine-risk/tests/operational_profile.rs."""

    payload = json.loads(PROFILE_PATH.read_bytes())
    payload["account_risk"]["max_symbol_notional_usdt"] = 125_000.0
    with pytest.raises(ValueError, match="max_symbol_notional_usdt"):
        load_operational_profile_bytes(json.dumps(payload).encode())
