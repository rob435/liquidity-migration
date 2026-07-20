"""R3b cluster-cap decision layer tests (staged; not wired to runtime)."""

from __future__ import annotations

import hashlib

from liquidity_migration.cluster_cap import (
    CLUSTER_CAP_K,
    CLUSTER_RHO_MIN,
    cluster_arm_for_trade,
    cluster_cap_decision,
)


def _arm(trade_id: str) -> str:
    return "B" if hashlib.sha256(trade_id.encode()).digest()[-1] & 1 else "A"


def _id_with_arm(arm: str) -> str:
    i = 0
    while True:
        candidate = f"trade-{i}"
        if _arm(candidate) == arm:
            return candidate
        i += 1


class TestArmAssignment:
    def test_matches_passive_exec_convention(self) -> None:
        for trade_id in ("2023-04-05-s-AGLDUSDT", "x", "y", "abc-123"):
            assert cluster_arm_for_trade(trade_id) == _arm(trade_id)

    def test_both_arms_reachable(self) -> None:
        arms = {cluster_arm_for_trade(f"t{i}") for i in range(64)}
        assert arms == {"A", "B"}


class TestDecision:
    def test_below_cap_passes_on_both_arms(self) -> None:
        for arm in ("A", "B"):
            decision = cluster_cap_decision(_id_with_arm(arm), [0.9, 0.8], cap_k=3)
            assert decision.action == "pass"
            assert decision.correlated_open_count == 2

    def test_at_cap_vetoes_only_on_arm_b(self) -> None:
        rhos = [0.75, 0.72, 0.71, 0.2]
        veto = cluster_cap_decision(_id_with_arm("B"), rhos)
        shadow = cluster_cap_decision(_id_with_arm("A"), rhos)
        assert veto.action == "veto" and veto.arm == "B"
        assert shadow.action == "shadow_veto" and shadow.arm == "A"
        assert veto.correlated_open_count == 3

    def test_threshold_boundary_is_inclusive(self) -> None:
        decision = cluster_cap_decision(_id_with_arm("B"), [CLUSTER_RHO_MIN] * CLUSTER_CAP_K)
        assert decision.action == "veto"

    def test_uncorrelatable_pairs_never_count_but_are_reported(self) -> None:
        decision = cluster_cap_decision(_id_with_arm("B"), [None, None, None, 0.9])
        assert decision.action == "pass"
        assert decision.uncorrelatable_open_count == 3
        assert decision.correlated_open_count == 1

    def test_empty_book_passes(self) -> None:
        assert cluster_cap_decision("t", []).action == "pass"

    def test_invalid_params_rejected(self) -> None:
        for kwargs in ({"cap_k": 0}, {"rho_min": 0.0}, {"rho_min": 1.0}):
            try:
                cluster_cap_decision("t", [0.9], **kwargs)  # type: ignore[arg-type]
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {kwargs}")
