"""Cross-file completion regression tests for bucket iC.

forward-replay-5: opt-in self-heal for a LEGITIMATE historical data revision in
``continuous_forward_replay.update_forward_ledger``. A late-arriving/backfilled
partition that the operator has confirmed (via ``allow_history_revision=True``)
must RE-BASE the drifting overlap days to the rebuilt values and log the rewrite,
instead of the permanent forward-clock stall a hard ``RuntimeError`` would cause.
The default (``allow_history_revision=False``) must keep the hard-drift contract
unchanged, and a vanished stored day must never be auto-healed.
"""

from __future__ import annotations

import logging

import polars as pl
import pytest

from liquidity_migration.continuous_forward_replay import (
    FROZEN_FORWARD_CONFIG,
    ForwardUpdateResult,
    _ledger_path,
    build_full_ledger,
    update_forward_ledger,
)
from liquidity_migration.continuous_rebalance import ContinuousRebalanceComponents

MS_PER_DAY = 86_400_000
T0 = 1_680_307_200_000  # 2023-04-01


def _pieces(n: int = 80) -> tuple[dict, dict, dict]:
    """Mirror tests/test_continuous_forward_replay.py::_pieces."""
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h = {d: (0.01 if i % 2 == 0 else -0.01) for i, d in enumerate(days)}
    pieces = {}
    for j, name in enumerate(FROZEN_FORWARD_CONFIG["weights"]):
        raw = {d: -0.4 * h[d] + 0.0008 + 0.0001 * j for d in days}
        pieces[name] = ContinuousRebalanceComponents(
            days=days,
            raw_by_day=raw,
            gross_by_day=dict(raw),
            cost_events={},
            funding_by_day={},
            active_gross_start={d: 0.0 for d in days},
            impact_exponent=0.5,
        )
    return pieces, h, {d: 0.0001 for d in days}


def test_default_keeps_hard_drift_contract(tmp_path) -> None:
    """allow_history_revision defaults False -> drift still hard-errors, nothing healed."""
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full, end_day_ms=T0 + 69 * MS_PER_DAY)
    before = _ledger_path(tmp_path, "bybit").read_text(encoding="utf-8")

    # Revise an input day inside the stored window -> rebuilt ledger drifts.
    pieces["turn3p3"].raw_by_day[T0 + 40 * MS_PER_DAY] += 0.01
    pieces["turn3p3"].gross_by_day[T0 + 40 * MS_PER_DAY] += 0.01
    drifted = build_full_ledger(pieces, h, fund)

    with pytest.raises(RuntimeError, match="drift"):
        update_forward_ledger(tmp_path, "bybit", drifted)
    # Stored ledger must be untouched on the hard-error path.
    assert _ledger_path(tmp_path, "bybit").read_text(encoding="utf-8") == before


def test_history_revision_rebases_and_logs(tmp_path, caplog) -> None:
    """allow_history_revision=True re-bases drifting overlap days + logs the rewrite."""
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    cut = T0 + 69 * MS_PER_DAY
    r0 = update_forward_ledger(tmp_path, "bybit", full, end_day_ms=cut)
    assert r0.appended_days == 70 and r0.rebased_days == 0

    # Legitimately revise a historical day (simulating a backfilled partition that
    # changes that day's rebuilt basket_return), then extend to all 80 days.
    revised_day = T0 + 40 * MS_PER_DAY
    pieces["turn3p3"].raw_by_day[revised_day] += 0.02
    pieces["turn3p3"].gross_by_day[revised_day] += 0.02
    revised = build_full_ledger(pieces, h, fund)

    with caplog.at_level(logging.WARNING, logger="liquidity_migration.continuous_forward_replay"):
        res = update_forward_ledger(tmp_path, "bybit", revised, allow_history_revision=True)

    assert isinstance(res, ForwardUpdateResult)
    # At least the revised day re-based; equity compounds forward so downstream
    # overlap days also drift and re-base. No day is silently dropped.
    assert res.rebased_days >= 1
    assert res.appended_days == 10  # the new days (70..79) still append
    assert res.total_days == 80
    assert res.verified_overlap_days == 70 - res.rebased_days
    assert res.verified_overlap_days + res.rebased_days == 70

    # The rewrite is observable (WARNING log carries the day + the re-base note).
    text = caplog.text
    assert "HISTORY REVISION re-base" in text
    assert "re-based" in text
    assert str(revised_day) in text

    # The stored ledger now reflects the revised inputs: re-reading it and
    # re-running with the SAME revised ledger is idempotent (no further re-base).
    stored = pl.read_csv(_ledger_path(tmp_path, "bybit"))
    fresh = {int(r["ts_ms"]): r for r in revised.filter(pl.col("ts_ms") <= cut).to_dicts()}
    stored_revised = next(r for r in stored.to_dicts() if int(r["ts_ms"]) == revised_day)
    assert stored_revised["basket_return"] == pytest.approx(
        fresh[revised_day]["basket_return"], rel=1e-12, abs=1e-12
    )

    r_again = update_forward_ledger(tmp_path, "bybit", revised, allow_history_revision=True)
    assert r_again.rebased_days == 0
    assert r_again.appended_days == 0
    assert r_again.verified_overlap_days == 80


def test_clean_overlap_does_not_rebase_even_when_allowed(tmp_path) -> None:
    """With the flag on but no drift, behavior is identical: verify + append, no re-base."""
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full, end_day_ms=T0 + 69 * MS_PER_DAY)
    res = update_forward_ledger(tmp_path, "bybit", full, allow_history_revision=True)
    assert res.rebased_days == 0
    assert res.appended_days == 10
    assert res.verified_overlap_days == 70
    assert res.total_days == 80


def test_vanished_stored_day_never_auto_healed(tmp_path) -> None:
    """A stored day missing from the rebuilt ledger is a coverage loss -> always raises,
    even under allow_history_revision (it is not a value revision)."""
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)  # store all 80 days

    # Rebuild with fewer days so a previously-stored day vanishes from the rebuild.
    pieces_short, h_short, fund_short = _pieces(70)
    short = build_full_ledger(pieces_short, h_short, fund_short)
    with pytest.raises(RuntimeError, match="missing from the rebuilt ledger"):
        update_forward_ledger(tmp_path, "bybit", short, allow_history_revision=True)
