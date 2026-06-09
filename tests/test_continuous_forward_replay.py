"""Tests for the no-order forward signal-replay collector (R3, live-readiness)."""

from __future__ import annotations

import json

import pytest

from liquidity_migration.continuous_forward_replay import (
    FROZEN_FORWARD_CONFIG,
    ForwardUpdateResult,
    build_full_ledger,
    forward_readiness_summary,
    frozen_config_hash,
    init_or_check_state,
    update_forward_ledger,
)
from liquidity_migration.continuous_rebalance import ContinuousRebalanceComponents

MS_PER_DAY = 86_400_000
T0 = 1_680_307_200_000  # 2023-04-01


def _pieces(n: int = 80) -> tuple[dict, dict, dict]:
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


def test_config_hash_pinning(tmp_path) -> None:
    init_or_check_state(tmp_path)
    init_or_check_state(tmp_path)  # idempotent under the same config
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    cfg["config_hash"] = "deadbeef"
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(RuntimeError, match="config hash mismatch"):
        init_or_check_state(tmp_path)


def test_append_then_idempotent_then_extend(tmp_path) -> None:
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    cut = T0 + 69 * MS_PER_DAY

    r1 = update_forward_ledger(tmp_path, "bybit", full, end_day_ms=cut)
    assert isinstance(r1, ForwardUpdateResult)
    assert r1.appended_days == 70 and r1.verified_overlap_days == 0

    r2 = update_forward_ledger(tmp_path, "bybit", full, end_day_ms=cut)
    assert r2.appended_days == 0 and r2.verified_overlap_days == 70  # idempotent

    r3 = update_forward_ledger(tmp_path, "bybit", full)  # extend to all 80 days
    assert r3.appended_days == 10 and r3.total_days == 80
    assert r3.last_day_ms == T0 + 79 * MS_PER_DAY


def test_overlap_drift_alarm(tmp_path) -> None:
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full, end_day_ms=T0 + 69 * MS_PER_DAY)
    # perturb one input day inside the stored window -> rebuilt ledger drifts
    pieces["turn3p3"].raw_by_day[T0 + 40 * MS_PER_DAY] += 0.01
    pieces["turn3p3"].gross_by_day[T0 + 40 * MS_PER_DAY] += 0.01
    drifted = build_full_ledger(pieces, h, fund)
    with pytest.raises(RuntimeError, match="drift"):
        update_forward_ledger(tmp_path, "bybit", drifted)


def test_readiness_summary_gates(tmp_path) -> None:
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)
    start = T0 + 40 * MS_PER_DAY
    s = forward_readiness_summary(tmp_path, "bybit", forward_start_ms=start)
    assert s["forward_days"] == 40
    assert s["tier3_days_gate_30"] is True
    assert s["tier3_mar_positive"] == (s["forward_return_pct"] > 0)
    # empty venue -> zero days
    s2 = forward_readiness_summary(tmp_path, "binance", forward_start_ms=start)
    assert s2["forward_days"] == 0


def test_hash_changes_with_config() -> None:
    base = frozen_config_hash()
    tweaked = dict(FROZEN_FORWARD_CONFIG)
    tweaked["weights"] = {**FROZEN_FORWARD_CONFIG["weights"], "turn3p3": 0.31}
    assert frozen_config_hash(tweaked) != base
