"""R3b correlated-cluster cap — pure decision layer (staged, not wired).

The 2026-06-20 disaster-stop study's unbuilt recommendation: cap simultaneous
same-direction exposure to correlated clusters. This module holds the frozen
decision function and the registered A/B assignment
(`docs/preregistration/r3b_cluster_cap_experiment_2026-07-20.md`); nothing on
the live path imports it yet — wiring into the entry-admission flow is a
separate deployment with an operator go and a recorded change point.

Frozen registered cell (Lane-1 receipts under
`reports/tail-risk-program/p13-r3b-cluster-caps-lane1-2026-07-20*/`):
rho_min = 0.70 over trailing 720h hourly log-returns (>= 240 overlapping
bars), cap K = 3 — the fourth correlated same-direction position is refused.
A/B: per-entry trade-id hash parity exactly like the passive-execution
experiment (sha256(trade_id) last-byte parity): arm A = shadow-veto (log
only, entry proceeds), arm B = veto (entry refused).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

CLUSTER_RHO_MIN = 0.70
CLUSTER_CAP_K = 3
CLUSTER_ARM_METADATA_KEY = "cluster_cap_arm"


def cluster_arm_for_trade(trade_id: str) -> str:
    """Deterministic arm by trade-id hash parity (passive-exec convention)."""
    digest = hashlib.sha256(trade_id.encode("utf-8")).digest()
    return "B" if digest[-1] & 1 else "A"


@dataclass(frozen=True)
class ClusterCapDecision:
    action: str  # "pass" | "shadow_veto" | "veto"
    arm: str  # "A" | "B"
    correlated_open_count: int
    uncorrelatable_open_count: int
    rho_min: float
    cap_k: int


def cluster_cap_decision(
    trade_id: str,
    rho_to_open_positions: Sequence[float | None],
    *,
    rho_min: float = CLUSTER_RHO_MIN,
    cap_k: int = CLUSTER_CAP_K,
) -> ClusterCapDecision:
    """Decide the cap action for one candidate entry.

    ``rho_to_open_positions``: the candidate's trailing correlation to each
    currently open same-direction position; ``None`` marks an un-correlatable
    pair (insufficient overlap — young listing), which never counts toward
    the cluster (fail-open on the count, reported in the decision so the
    shadow record keeps the honesty visible).
    """
    if cap_k < 1:
        raise ValueError("cluster cap K must be >= 1")
    if not 0.0 < rho_min < 1.0:
        raise ValueError("rho_min must be inside (0, 1)")
    correlated = sum(1 for rho in rho_to_open_positions if rho is not None and rho >= rho_min)
    uncorrelatable = sum(1 for rho in rho_to_open_positions if rho is None)
    arm = cluster_arm_for_trade(trade_id)
    if correlated < cap_k:
        action = "pass"
    elif arm == "B":
        action = "veto"
    else:
        action = "shadow_veto"
    return ClusterCapDecision(
        action=action,
        arm=arm,
        correlated_open_count=correlated,
        uncorrelatable_open_count=uncorrelatable,
        rho_min=rho_min,
        cap_k=cap_k,
    )
