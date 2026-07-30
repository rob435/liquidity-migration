"""B3: one sleeve cannot spend another sleeve's capital.

``max_component_gross_notional_usdt`` is account-wide despite its name, so
before this a single sleeve could consume the whole envelope and leave the
others unable to enter at all — which is exactly why LONG could not be funded
alongside CARRY. The partition is what makes running both real.

Two properties matter and are tested separately, because they fail differently:

* the **kernel** refuses an exposure-increasing batch that puts one sleeve over
  its own share, while exits stay available;
* the **profile** refuses at load time a partition that is not a partition, or
  a producer sized outside the share it was given — so the failure lands on an
  operator's terminal, not on a live book.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.account_contracts import SleeveCapitalLimit
from liquidity_migration.account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    read_account_journal,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.operational_profile import load_operational_profile_bytes

REPO = Path(__file__).resolve().parents[1]


def _market(symbol: str = "BUSDT", price: float = 10.0) -> MarketInputRef:
    return MarketInputRef(
        input_key=f"book-{symbol}",
        symbol=symbol,
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        reference_price=price,
        bid_price=price - 0.01,
        ask_price=price + 0.01,
        book_sequence=42,
        source="test_l2",
    )


def _rules(*symbols: str) -> dict[str, InstrumentRules]:
    return {
        symbol: InstrumentRules(
            symbol=symbol,
            qty_step=0.1,
            min_qty=0.1,
            min_notional=1.0,
            max_order_qty=10_000.0,
            max_leverage=20.0,
        )
        for symbol in (symbols or ("BUSDT",))
    }


def _target(
    *,
    sleeve: str,
    qty: float,
    symbol: str = "BUSDT",
    leverage: float = 2.0,
    revision: str = "1",
) -> DesiredTarget:
    return DesiredTarget(
        # A decision is immutable once journaled, so a later batch that moves
        # the same target needs its own decision key.
        decision_key=f"{sleeve}-decision-{symbol}-{revision}",
        target_key=f"{sleeve}/main/{symbol}",
        sleeve=sleeve,
        strategy_id=f"{sleeve}-strategy",
        component_id=f"{sleeve}-component",
        symbol=symbol,
        signed_qty=qty,
        reference_price=10.0,
        leverage=leverage,
        reason="partition test",
    )


def _snapshot() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        equity_usdt=10_000.0,
        available_margin_usdt=9_000.0,
        snapshot_key="wallet-1",
        snapshot_ts_ns=950_000_000,
    )


def _policy(*, partitioned: bool = True) -> AccountRiskPolicy:
    """Room for 1000 USDT of book, split 600 CARRY / 300 LONG."""

    return AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=500.0,
        max_leverage=10.0,
        sleeve_limits=(
            (
                SleeveCapitalLimit("carry", 600.0, 300.0),
                SleeveCapitalLimit("long", 300.0, 150.0),
            )
            if partitioned
            else ()
        ),
    )


def _kernel(root: Path) -> AccountExecutionKernel:
    return AccountExecutionKernel(
        root,
        account_id="bybit-demo-test",
        clock=VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100_000_000),
        id_seed="partition-seed",
    )


#: Every symbol these tests ever use. Supplied on every batch because the risk
#: gate evaluates the *projected* book, which retains targets from prior
#: batches and needs a market input for each.
_SYMBOLS = ("BUSDT", "CUSDT")


def _submit(kernel: AccountExecutionKernel, batch_id: str, targets, policy):
    return kernel.submit_targets(
        batch_id=batch_id,
        market_inputs=[_market(symbol) for symbol in _SYMBOLS],
        targets=list(targets),
        risk_snapshot=_snapshot(),
        risk_policy=policy,
        instrument_rules=_rules(*_SYMBOLS),
    )


# --------------------------------------------------------------------------
# The kernel gate
# --------------------------------------------------------------------------


def test_a_sleeve_inside_its_share_is_accepted(tmp_path: Path) -> None:
    result = _submit(
        _kernel(tmp_path),
        "batch-1",
        [_target(sleeve="carry", qty=50.0)],  # 500 USDT of 600
        _policy(),
    )
    assert result.accepted
    assert result.rejection_keys == ()


def test_a_sleeve_over_its_own_share_is_refused_while_the_account_has_room(
    tmp_path: Path,
) -> None:
    """The whole point: 700 USDT fits the 1000 account cap but not CARRY's 600."""

    unpartitioned = _submit(
        _kernel(tmp_path / "open"),
        "batch-1",
        [_target(sleeve="carry", qty=70.0)],
        _policy(partitioned=False),
    )
    assert unpartitioned.accepted, "the account cap alone would have allowed this"

    result = _submit(
        _kernel(tmp_path / "partitioned"),
        "batch-1",
        [_target(sleeve="carry", qty=70.0)],
        _policy(),
    )
    assert not result.accepted
    assert any(key.endswith("sleeve_gross_limit:carry") for key in result.rejection_keys)


def test_one_sleeve_cannot_crowd_the_other_out(tmp_path: Path) -> None:
    """CARRY at its full share leaves LONG's share entirely intact."""

    kernel = _kernel(tmp_path)
    first = _submit(kernel, "batch-1", [_target(sleeve="carry", qty=60.0)], _policy())
    assert first.accepted

    # Without the partition CARRY's 600 plus LONG's 300 would breach the 1000
    # account cap only at the margin; with it, LONG spends its own share and
    # the account total still fits.
    second = _submit(
        kernel,
        "batch-2",
        [_target(sleeve="long", qty=30.0, symbol="CUSDT")],
        _policy(),
    )
    assert second.accepted, second.rejection_keys


def test_a_sleeve_the_partition_does_not_name_gets_nothing(tmp_path: Path) -> None:
    """Declaring a partition and exempting the unlisted is the same hole."""

    result = _submit(
        _kernel(tmp_path),
        "batch-1",
        [_target(sleeve="continuous", qty=1.0)],
        _policy(),
    )
    assert not result.accepted
    assert any(
        key.endswith("unpartitioned_sleeve:continuous") for key in result.rejection_keys
    )


def test_the_margin_share_binds_independently_of_the_gross_share(tmp_path: Path) -> None:
    """At high leverage gross binds first; at low leverage margin does."""

    policy = AccountRiskPolicy(
        max_component_gross_notional_usdt=10_000.0,
        max_account_gross_notional_usdt=10_000.0,
        max_symbol_notional_usdt=10_000.0,
        max_initial_margin_usdt=10_000.0,
        max_leverage=10.0,
        sleeve_limits=(SleeveCapitalLimit("carry", 5_000.0, 100.0),),
    )
    # 1000 USDT gross at 1x is 1000 of margin: inside the 5000 gross share,
    # far outside the 100 margin share.
    result = _submit(
        _kernel(tmp_path),
        "batch-1",
        [_target(sleeve="carry", qty=100.0, leverage=1.0)],
        policy,
    )
    assert not result.accepted
    assert any(key.endswith("sleeve_margin_limit:carry") for key in result.rejection_keys)
    assert not any(key.endswith("sleeve_gross_limit:carry") for key in result.rejection_keys)


def test_an_exit_is_never_blocked_by_the_partition(tmp_path: Path) -> None:
    """Risk-reducing batches bypass every cap by design; this is one of them."""

    kernel = _kernel(tmp_path)
    opened = _submit(kernel, "batch-1", [_target(sleeve="carry", qty=60.0)], _policy())
    assert opened.accepted

    # Shrink the share below the held position, as an equity contraction would.
    tightened = AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=500.0,
        max_leverage=10.0,
        sleeve_limits=(SleeveCapitalLimit("carry", 1.0, 1.0),),
    )
    flat = _submit(
        kernel,
        "batch-2",
        [_target(sleeve="carry", qty=0.0, revision="2")],
        tightened,
    )
    assert flat.accepted, flat.rejection_keys


def test_an_unpartitioned_policy_keeps_the_historical_behaviour(tmp_path: Path) -> None:
    result = _submit(
        _kernel(tmp_path),
        "batch-1",
        [_target(sleeve="anything-at-all", qty=50.0)],
        _policy(partitioned=False),
    )
    assert result.accepted


def test_the_partition_is_recorded_in_the_journal(tmp_path: Path) -> None:
    """The risk decision carries the exact policy it was evaluated against."""

    kernel = _kernel(tmp_path)
    _submit(kernel, "batch-1", [_target(sleeve="carry", qty=50.0)], _policy())
    risk = next(
        event
        for event in read_account_journal(tmp_path)
        if event.event_type == AccountEventType.RISK_DECISION.value
    )
    recorded = risk.payload["risk_policy"]["sleeve_limits"]
    assert [row["sleeve"] for row in recorded] == ["carry", "long"]
    assert recorded[0]["max_gross_notional_usdt"] == pytest.approx(600.0)


# --------------------------------------------------------------------------
# The load-time proof
# --------------------------------------------------------------------------


def _mainnet_document(**risk_overrides: object) -> dict:
    payload = json.loads((REPO / "configs" / "operational.mainnet.json").read_text())
    payload["account_risk"].update(risk_overrides)
    return payload


def _load(document: dict):
    return load_operational_profile_bytes(json.dumps(document).encode("utf-8"))


def test_the_committed_mainnet_profile_proves(tmp_path: Path) -> None:
    profile = _load(_mainnet_document())
    assert {limit.sleeve for limit in profile.account_risk.sleeve_limits} == {
        "carry",
        "continuous",
        "long",
    }


def test_shares_that_sum_above_the_account_cap_are_not_a_partition() -> None:
    document = _mainnet_document()
    shares = document["account_risk"]["sleeve_limits"]
    shares["carry"]["max_gross_notional_usdt"] = document["account_risk"][
        "max_account_gross_notional_usdt"
    ]
    with pytest.raises(ValueError, match="sum above account_risk.max_account_gross"):
        _load(document)


def test_margin_shares_that_sum_above_the_margin_cap_are_refused() -> None:
    document = _mainnet_document()
    document["account_risk"]["sleeve_limits"]["carry"]["max_initial_margin_usdt"] = (
        document["account_risk"]["max_initial_margin_usdt"]
    )
    with pytest.raises(ValueError, match="sum above account_risk.max_initial_margin"):
        _load(document)


def test_a_producer_sized_outside_its_share_is_refused_at_load_time() -> None:
    """Better a refusal here than a fleet that rejects every entry at runtime."""

    document = _mainnet_document()
    document["account_risk"]["sleeve_limits"]["carry"]["max_gross_notional_usdt"] = 1.0
    with pytest.raises(ValueError, match="'carry' gross envelope exceeds its sleeve_limits"):
        _load(document)


def test_a_funded_sleeve_with_no_share_is_refused() -> None:
    document = _mainnet_document()
    del document["account_risk"]["sleeve_limits"]["long"]
    with pytest.raises(ValueError, match="'long' has an envelope but no sleeve_limits share"):
        _load(document)


def test_an_unknown_sleeve_name_is_refused() -> None:
    document = _mainnet_document()
    document["account_risk"]["sleeve_limits"]["carrry"] = {
        "max_gross_notional_usdt": 1.0,
        "max_initial_margin_usdt": 1.0,
    }
    with pytest.raises(ValueError, match="unknown sleeves: carrry"):
        _load(document)


def test_an_empty_partition_is_refused_rather_than_silently_refusing_everything() -> None:
    document = _mainnet_document()
    document["account_risk"]["sleeve_limits"] = {}
    with pytest.raises(ValueError, match="must name at least one sleeve"):
        _load(document)


def test_the_demo_profile_stays_unpartitioned() -> None:
    """B3 is optional by design: a profile predating it keeps working."""

    profile = load_operational_profile_bytes(
        (REPO / "configs" / "operational.demo.json").read_bytes()
    )
    assert profile.account_risk.sleeve_limits == ()
    assert profile.account_risk.to_policy().sleeve_limits == ()


def test_a_sleeve_over_its_share_cannot_veto_another_sleeve_shrinking(
    tmp_path: Path,
) -> None:
    """The partition must never be the reason a book cannot come down.

    Found by adversarial review. The check judged every sleeve in the projected
    book, so a sleeve already over its share rejected *another* sleeve's
    de-risking batch whenever strict risk reduction could not be proven -- and
    it cannot be proven while a submission is live, because the aggregate then
    sits above the reconstructed position. A partial exit was blocked by a
    limit that exists to bound entries.
    """

    kernel = _kernel(tmp_path)
    opened = _submit(
        kernel,
        "batch-1",
        [_target(sleeve="carry", qty=55.0), _target(sleeve="long", qty=10.0, symbol="CUSDT")],
        _policy(),
    )
    assert opened.accepted, opened.rejection_keys

    # Equity contracts and the envelope rebases the partition down under the
    # book CARRY already holds: 550 USDT against a share that is now 400.
    contracted = AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=500.0,
        max_leverage=10.0,
        sleeve_limits=(
            SleeveCapitalLimit("carry", 400.0, 200.0),
            SleeveCapitalLimit("long", 200.0, 100.0),
        ),
    )

    # LONG halves its own component. It touches nothing CARRY owns.
    reduced = _submit(
        kernel,
        "batch-2",
        [_target(sleeve="long", qty=5.0, symbol="CUSDT", revision="2")],
        contracted,
    )
    assert reduced.accepted, reduced.rejection_keys
    assert not any("carry" in key for key in reduced.rejection_keys)


def test_a_sleeve_over_its_share_still_cannot_grow(tmp_path: Path) -> None:
    """The other half of the same rule: relief is not permission."""

    kernel = _kernel(tmp_path)
    assert _submit(kernel, "batch-1", [_target(sleeve="carry", qty=55.0)], _policy()).accepted

    contracted = AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=500.0,
        max_leverage=10.0,
        sleeve_limits=(SleeveCapitalLimit("carry", 400.0, 200.0),),
    )
    grown = _submit(
        kernel,
        "batch-2",
        [_target(sleeve="carry", qty=56.0, revision="2")],
        contracted,
    )
    assert not grown.accepted
    assert any(key.endswith("sleeve_gross_limit:carry") for key in grown.rejection_keys)

    # Coming down toward the share is allowed even while still above it.
    toward = _submit(
        kernel,
        "batch-3",
        [_target(sleeve="carry", qty=50.0, revision="3")],
        contracted,
    )
    assert toward.accepted, toward.rejection_keys
