"""Cross-file integration regression tests for audit bucket iG.

Covers the FOREIGN-file completion half of three findings:

- cost-funding-3: gate-tighten long_native promotion / run-label on the
  notional-weighted ``funding_modeled_fraction`` (emitted by
  trade_lifecycle.summarize_trade_backtest), not just the 3-state
  funding_mode collapse. A 'partial' book where too much notional was charged
  ZERO funding must FAIL / down-label; a coverage-edge 'partial' must still pass.
- long-sleeve-2: observability-only live FC rank-boundary telemetry. A fired
  candidate whose live ``today_volume_rank`` lands within a margin of
  fc_top_volume_rank_max is flagged (the live rank is over the universe superset,
  not the full backtest universe) WITHOUT changing selection/sizing.
- pit-data-1: the inert ``require_pit_membership`` flag is fully removed from
  LongNativeConfig; ``require_full_pit_universe`` is the live universe gate.
"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from liquidity_migration.long_native import (
    FUNDING_MODELED_FRACTION_THRESHOLD,
    LongNativeConfig,
    _evaluate_promotion,
    _run_label,
)
from liquidity_migration.long_native_event_demo import (
    FC_VOLUME_RANK_TELEMETRY_MARGIN,
    _fc_rank_is_near_boundary,
    _log_fc_rank_boundary,
)


# --------------------------------------------------------------------------- #
# cost-funding-3: funding-coverage gate-tightening (foreign half in long_native)
# --------------------------------------------------------------------------- #

def _passing_summary(**overrides):
    """A summary that clears every OTHER gate (Sharpe, DD) so the funding-coverage
    check is the only thing under test."""
    base = {"sharpe_like": 1.5, "max_drawdown": -0.10, "funding_modeled_fraction": 1.0}
    base.update(overrides)
    return base


def test_promotion_fails_when_funding_coverage_below_threshold() -> None:
    """A 'partial' book where a large slice of notional was charged ZERO funding
    (fraction below threshold) must NOT pass the promotion gate."""
    promo = _evaluate_promotion(
        splits=[],
        summary=_passing_summary(funding_modeled_fraction=0.50),
        funding_mode="partial",
        full_pit_universe_pass=True,
    )
    assert promo["promotion_gate_pass"] is False
    assert "funding_coverage_below_threshold" in promo["promotion_reasons"]
    # Distinct from the all-missing case: do not double-report funding_missing.
    assert "funding_missing" not in promo["promotion_reasons"]
    assert promo["funding_modeled_fraction"] == pytest.approx(0.50)
    assert promo["funding_coverage_threshold"] == pytest.approx(FUNDING_MODELED_FRACTION_THRESHOLD)


def test_promotion_passes_at_coverage_edge() -> None:
    """A coverage-edge 'partial' (one funding-free alt, fraction >= threshold) is
    still acceptable and must pass."""
    promo = _evaluate_promotion(
        splits=[],
        summary=_passing_summary(funding_modeled_fraction=0.97),
        funding_mode="partial",
        full_pit_universe_pass=True,
    )
    assert promo["promotion_gate_pass"] is True
    assert "funding_coverage_below_threshold" not in promo["promotion_reasons"]


def test_promotion_all_missing_still_fails_as_funding_missing() -> None:
    """The pre-existing all-missing failure path is preserved and reported as
    funding_missing (not the new coverage reason)."""
    promo = _evaluate_promotion(
        splits=[],
        summary=_passing_summary(funding_modeled_fraction=0.0),
        funding_mode="missing",
        full_pit_universe_pass=True,
    )
    assert promo["promotion_gate_pass"] is False
    assert "funding_missing" in promo["promotion_reasons"]
    assert "funding_coverage_below_threshold" not in promo["promotion_reasons"]


def test_promotion_absent_fraction_defaults_to_full_coverage() -> None:
    """Backward-compat: an older summary WITHOUT funding_modeled_fraction must not
    invent a new failure (defaults to 1.0 == full coverage)."""
    promo = _evaluate_promotion(
        splits=[],
        summary={"sharpe_like": 1.5, "max_drawdown": -0.10},  # no fraction key
        funding_mode="partial",
        full_pit_universe_pass=True,
    )
    assert promo["promotion_gate_pass"] is True
    assert promo["funding_modeled_fraction"] == pytest.approx(1.0)


def test_run_label_down_labels_low_coverage_partial() -> None:
    """The single 'partial' label is split: a low-coverage partial down-labels to a
    distinct run label so an auditor can tell it apart from a coverage-edge partial."""
    low = _run_label(
        full_pit_universe_pass=True,
        funding_mode="partial",
        archive_manifest_empty=False,
        funding_modeled_fraction=0.50,
    )
    assert low == "full_pit_universe_funding_coverage_low"

    edge = _run_label(
        full_pit_universe_pass=True,
        funding_mode="partial",
        archive_manifest_empty=False,
        funding_modeled_fraction=0.99,
    )
    assert edge == "full_pit_universe_funding_partial"


def test_run_label_partial_default_fraction_is_backward_compatible() -> None:
    """Callers that do not pass funding_modeled_fraction keep the prior partial label
    (default 1.0 == coverage OK)."""
    label = _run_label(
        full_pit_universe_pass=True,
        funding_mode="partial",
        archive_manifest_empty=False,
    )
    assert label == "full_pit_universe_funding_partial"


# --------------------------------------------------------------------------- #
# long-sleeve-2: live FC rank-boundary telemetry (observability ONLY)
# --------------------------------------------------------------------------- #

def test_fc_rank_near_boundary_predicate() -> None:
    cutoff = 10
    margin = FC_VOLUME_RANK_TELEMETRY_MARGIN
    # Exactly at the cutoff -> in band.
    assert _fc_rank_is_near_boundary(cutoff, cutoff) is True
    # Within margin of the cutoff -> in band.
    assert _fc_rank_is_near_boundary(cutoff - margin, cutoff) is True
    assert _fc_rank_is_near_boundary(cutoff - 1, cutoff) is True
    # Comfortably inside the top set -> NOT flagged.
    assert _fc_rank_is_near_boundary(cutoff - margin - 1, cutoff) is False
    assert _fc_rank_is_near_boundary(1, cutoff) is False
    # Above the cutoff -> would not have fired the FC gate; not flagged.
    assert _fc_rank_is_near_boundary(cutoff + 1, cutoff) is False
    # Missing rank -> not flagged.
    assert _fc_rank_is_near_boundary(None, cutoff) is False


def test_log_fc_rank_boundary_emits_for_near_boundary_candidate(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="liquidity_migration.long_native_event_demo"):
        _log_fc_rank_boundary(symbol="WIFUSDT", today_volume_rank=9, fc_top_volume_rank_max=10)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("rank-boundary" in m and "WIFUSDT" in m for m in msgs)


def test_log_fc_rank_boundary_silent_for_comfortable_rank(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="liquidity_migration.long_native_event_demo"):
        _log_fc_rank_boundary(symbol="ETHUSDT", today_volume_rank=2, fc_top_volume_rank_max=10)
    assert caplog.records == []


# --------------------------------------------------------------------------- #
# pit-data-1: the inert require_pit_membership flag is fully removed
# --------------------------------------------------------------------------- #

def test_require_pit_membership_field_is_removed() -> None:
    field_names = {f.name for f in dataclasses.fields(LongNativeConfig)}
    assert "require_pit_membership" not in field_names
    # The live universe-completeness gate remains.
    assert "require_full_pit_universe" in field_names


def test_long_native_config_rejects_removed_flag() -> None:
    with pytest.raises(TypeError):
        LongNativeConfig(require_pit_membership=False)  # type: ignore[call-arg]


def test_require_full_pit_universe_still_constructs() -> None:
    cfg = LongNativeConfig(require_full_pit_universe=False)
    assert cfg.require_full_pit_universe is False
    cfg_default = LongNativeConfig()
    assert cfg_default.require_full_pit_universe is True
