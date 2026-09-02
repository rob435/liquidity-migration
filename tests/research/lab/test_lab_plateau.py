from __future__ import annotations

import math

import pytest

from liquidity_migration.research.lab.plateau import (
    Arm,
    concentration_check,
    lag_check,
    mirror_check,
    neighbour_check,
    persistence_check,
    plateau_checks,
    top_share,
)

WIN = Arm(delta=0.018, placebo_share=0.01)
LOSE = Arm(delta=-0.005, placebo_share=0.6)
NOISE = Arm(delta=0.001, placebo_share=0.3)


def test_neighbours_pass_when_both_sides_keep_the_sign() -> None:
    ok = neighbour_check(10, 0.0186, {9: 0.004, 10: 0.0186, 11: 0.0188, 12: 0.0136})
    assert ok.passes and ok.value == pytest.approx(0.004) and "9 (+0.0040)" in ok.note
    # the recorded funding cell: 9 bp loses, 10 wins, 11 wins
    spike = neighbour_check(10, 0.0186, {6: -0.0177, 9: -0.0067, 11: 0.0188})
    assert not spike.passes and "sign flips at 9" in spike.note
    one_side = neighbour_check(10, 0.0186, {11: 0.0188})
    assert one_side.passes
    none = neighbour_check(10, 0.0186, {10: 0.0186})
    assert not none.passes and math.isnan(none.value)
    negative_cell = neighbour_check(2, -0.01, {1: -0.02, 3: -0.005})
    assert negative_cell.passes and negative_cell.value == pytest.approx(-0.005)


def test_lag_and_persistence_need_the_same_sign_and_a_beaten_placebo() -> None:
    assert lag_check(WIN, Arm(0.012, 0.02)).passes
    assert not lag_check(WIN, NOISE).passes
    assert not lag_check(WIN, Arm(0.012, 0.2)).passes
    assert not lag_check(WIN, Arm(-0.012, 0.0)).passes
    assert lag_check(WIN, Arm(0.012, 0.09), alpha=0.1).passes
    assert persistence_check(WIN, Arm(0.010, 0.03)).passes
    assert not persistence_check(WIN, Arm(-0.0025, 0.535)).passes
    assert persistence_check(WIN, Arm(0.010, 0.03)).name == "persistence"


def test_mirror_fails_only_when_the_turned_around_rule_also_wins() -> None:
    assert mirror_check(WIN, LOSE).passes
    assert mirror_check(WIN, NOISE).passes
    assert not mirror_check(WIN, Arm(0.02, 0.01)).passes
    assert mirror_check(WIN, Arm(-0.02, 0.0)).passes
    assert mirror_check(WIN, LOSE).value == LOSE.delta


def test_concentration_is_the_top_three_share_of_the_gain() -> None:
    spread = [4.0, 3.0, 3.0, 3.0, 3.0, 2.0, 2.0]
    assert top_share(spread) == pytest.approx(10 / 20)
    assert concentration_check(spread).passes
    knife = [62.6, 44.6, 27.6, 23.1, 22.9, 16.4, 15.2, -9.0, -17.4, -19.0, -25.5]
    share = top_share(knife)
    assert share == pytest.approx((62.6 + 44.6 + 27.6) / sum(knife))
    assert share > 0.5 and not concentration_check(knife).passes
    assert "top 3 of 11 trades" in concentration_check(knife).note
    # the recorded funding cell: three trades carry more than the whole gain (195.6 of 175.1)
    over = concentration_check([106.7, 45.2, 43.7, -20.5])
    assert over.value == pytest.approx(195.6 / 175.1) and not over.passes
    assert concentration_check([106.7, 45.2, 43.7, -20.5], max_share=1.2).passes
    no_gain = concentration_check([-1.0, -2.0])
    assert not no_gain.passes and math.isnan(no_gain.value)
    assert not concentration_check([]).passes
    assert concentration_check([1.0, 1.0, 1.0, 1.0], top=2).value == pytest.approx(0.5)


def test_plateau_checks_compose_and_report_rows() -> None:
    passing = plateau_checks(
        WIN, cell_threshold=10, neighbours={9: 0.01, 11: 0.012}, lagged=Arm(0.012, 0.02),
        persistent=Arm(0.010, 0.03), mirror=LOSE, per_trade_deltas=[1.0] * 10,
    )
    assert passing.passes
    assert [c.name for c in passing.checks] == ["neighbours", "lag", "persistence", "mirror", "concentration"]
    assert [r["check"] for r in passing.rows()] == ["neighbours", "lag", "persistence", "mirror", "concentration"]
    assert all(r["passes"] for r in passing.rows())
    failing = plateau_checks(
        WIN, cell_threshold=10, neighbours={9: -0.0067, 11: 0.0188}, lagged=Arm(0.001, 0.295),
        persistent=Arm(-0.0025, 0.535), mirror=LOSE, per_trade_deltas=[62.6, 44.6, 27.6, 23.1, 22.9, 16.4],
    )
    assert not failing.passes
    assert failing.mirror.passes
    assert not failing.neighbours.passes and not failing.lag.passes and not failing.persistence.passes
    assert not failing.concentration.passes
