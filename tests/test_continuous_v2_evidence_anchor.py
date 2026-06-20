"""Phase 0 of the continuous v2 next-level plan: the two frozen baseline objects.

Receipt: docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md

Guards the small, deployment-sensitive wiring that lets the research runner
reproduce BOTH baseline objects from the SAME frozen component book without
touching the frozen config hash or the live forward ledger:

  * V2_CONTROL (= V2_LIVE_RESEARCH_CONTROL): TP 0.12, daily vol-target adjuster OFF.
  * V2_EVIDENCE_ANCHOR: TP 0.10, daily vol-target adjuster ON (prior max4 wiring).

The load-bearing package change is the new ``rebalance_rule`` kwarg on
``build_full_ledger`` (default None -> frozen rule, byte-identical for the live
forward clock and every existing caller).
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    build_full_ledger,
    frozen_rebalance_rule,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceComponents,
)

import continuous_v2_ab_research_runner as abr  # noqa: E402

MS_PER_DAY = 86_400_000


def _synthetic_pieces(n_days: int = 120) -> dict[str, ContinuousRebalanceComponents]:
    """A small multi-day component shared by all three frozen weights.

    Returns swing wide enough (~2%/day) that the enabled vol-target scale departs
    from 1.0 once the 90-day window fills, so an enabled rule must diverge from the
    frozen (disabled) rule.
    """
    base = 1_704_067_200_000  # 2024-01-01T00:00:00Z, arbitrary but deterministic
    days = [base + i * MS_PER_DAY for i in range(n_days)]
    raw = {d: (0.02 if i % 2 == 0 else -0.02) for i, d in enumerate(days)}
    comp = ContinuousRebalanceComponents(
        days=days,
        raw_by_day=dict(raw),
        gross_by_day=dict(raw),
        cost_events={d: [] for d in days},
        funding_by_day={d: 0.0 for d in days},
        active_gross_start={d: 1.0 for d in days},
        impact_exponent=0.5,
    )
    # build_full_ledger weights the three frozen component keys.
    return {key: comp for key in ("turn3p3", "turn4p3", "turn4p5")}


def _zero_hedge(pieces: dict[str, ContinuousRebalanceComponents]) -> dict[int, float]:
    days = sorted({d for p in pieces.values() for d in p.days})
    return {d: 0.0 for d in days}


def test_tp_override_selects_object_take_profit() -> None:
    assert abr.tp_override_for(abr.ANCHOR_ARM) == pytest.approx(0.10)
    assert abr.tp_override_for(abr.CONTROL_ARM) is None
    # Pre-existing F2 exit-alpha TP variants are unchanged by the anchor wiring.
    assert abr.tp_override_for(abr.EXIT_TP12_ARM) == pytest.approx(0.12)
    assert abr.tp_override_for(abr.EXIT_TP15_ARM) == pytest.approx(0.15)


def test_arm_rebalance_rule_anchor_vs_control() -> None:
    anchor = abr._arm_rebalance_rule(abr.ANCHOR_ARM)
    control = abr._arm_rebalance_rule(abr.CONTROL_ARM)
    frozen = frozen_rebalance_rule()
    # Anchor flips ONLY enabled; every other param stays the frozen pre-override object.
    assert anchor.enabled is True
    assert control.enabled is False
    assert frozen.enabled is False  # operator-override default OFF
    assert (anchor.max_scale, anchor.target_daily_vol, anchor.realized_vol_window_days) == (
        4.0,
        0.045,
        90,
    )
    assert replace(anchor, enabled=False) == frozen


def test_anchor_is_registered_phase0_baseline() -> None:
    assert abr.ARM_DEFINITIONS[abr.ANCHOR_ARM].implemented is True
    assert abr.ANCHOR_ARM in abr.PHASE0_ARMS
    assert abr.amendment_for(abr.ANCHOR_ARM) == abr.AMENDMENT_NEXT_LEVEL


def test_build_full_ledger_default_is_frozen_rule() -> None:
    pieces = _synthetic_pieces()
    h = _zero_hedge(pieces)
    default = build_full_ledger(pieces, h, h)
    explicit = build_full_ledger(pieces, h, h, rebalance_rule=frozen_rebalance_rule())
    # Default None must be byte-identical to passing the frozen rule explicitly:
    # this is the live forward clock's path and must never silently change.
    assert default.equals(explicit)


def test_build_full_ledger_enabled_rule_diverges() -> None:
    pieces = _synthetic_pieces()
    h = _zero_hedge(pieces)
    off = build_full_ledger(pieces, h, h, rebalance_rule=frozen_rebalance_rule())
    on = build_full_ledger(
        pieces, h, h, rebalance_rule=replace(frozen_rebalance_rule(), enabled=True)
    )
    # The vol-target adjuster ON must actually change the ledger (proves the kwarg
    # flows through to apply_rebalance_rule, i.e. the anchor really differs from the
    # control beyond TP).
    assert not off.equals(on)
