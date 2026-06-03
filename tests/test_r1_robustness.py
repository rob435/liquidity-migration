"""Pin the R1 robustness / Tier-2 demo-candidate analyzer's gate integrity.

Audit 2026-05-29 found two gate-integrity holes:
  * zero-drawdown cells produced MAR = +inf, which spuriously cleared the pooled-
    MAR demo-eligibility gate (a degenerate / too-few-trades cell is not
    "infinitely good"). MAR is now NaN there and a non-finite MAR Δ is not
    demo-eligible.
  * an OOM-killed cell's truncated/missing report crashed the whole multi-cell
    run (no per-cell try/except). It now surfaces a per-cell load_error instead.
"""
from __future__ import annotations

import importlib.util
import math
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("r1_robustness", REPO / "scripts" / "r1_robustness.py")
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["r1_robustness"] = m
    spec.loader.exec_module(m)
    return m


MOD = _load()


def test_block_bootstrap_indices_never_straddle_the_boundary() -> None:
    """re-audit rescan-rmom-funding-2: the non-circular moving-block bootstrap must
    never wrap (no block contains both the last and first index) — that wrap was the
    bug that stitched unrelated regimes for a regime-conditional strategy."""
    rng = random.Random(0)
    n, block = 36, 3
    for _ in range(300):
        idx = MOD._resample_block_indices(n, block, rng)
        assert len(idx) == n
        assert all(0 <= i < n for i in idx)
        # Each full block (the resample is laid down in block-length chunks) is a
        # contiguous ascending run — never a high->low wrap across the boundary.
        for k in range(0, n - block + 1, block):
            blk = idx[k:k + block]
            assert all(0 <= b - a <= 1 for a, b in zip(blk, blk[1:])), blk


def test_block_bootstrap_indices_handle_series_shorter_than_block() -> None:
    """The max(n-block+1, 1) guard must not IndexError when n < block."""
    rng = random.Random(0)
    idx = MOD._resample_block_indices(2, 5, rng)
    assert len(idx) == 2
    assert all(0 <= i < 2 for i in idx)


def test_mar_zero_drawdown_is_nan_not_inf() -> None:
    # all-positive monthly returns -> zero monthly-resolution drawdown
    assert math.isnan(MOD._mar([0.1, 0.2, 0.05]))


def test_mar_with_drawdown_is_finite() -> None:
    assert math.isfinite(MOD._mar([0.1, -0.2, 0.15, 0.05]))


def test_engine_mar_zero_drawdown_is_nan_not_inf() -> None:
    assert math.isnan(MOD._engine_mar(0.5, 0.0, 2.0))
    assert math.isclose(MOD._engine_mar(0.5, -0.25, 1.0), 0.5 / 0.25)


def test_tier2_verdict_nonfinite_mar_is_not_demo_eligible() -> None:
    v = MOD._tier2_verdict(
        float("nan"), 0.2, float("nan"),
        by_ret=1.5, bn_ret=1.4, by_dd=-0.1, bn_dd=-0.1, by_tr=50, bn_tr=40,
    )
    assert "DEMO-ELIGIBLE" not in v
    assert "MAR undefined" in v


def test_tier2_verdict_finite_strong_cell_is_demo_eligible() -> None:
    v = MOD._tier2_verdict(
        0.5, 0.5, 0.5,
        by_ret=1.5, bn_ret=1.4, by_dd=-0.1, bn_dd=-0.1, by_tr=50, bn_tr=40,
    )
    assert v == "DEMO-ELIGIBLE"


def test_load_json_metrics_missing_report_returns_load_error(tmp_path) -> None:
    m = MOD._load_json_metrics(tmp_path)  # no report file in this dir
    assert m["load_error"]
    assert m["full_pit_pass"] is False
    assert m["trades"] == 0


def test_load_json_metrics_truncated_json_returns_load_error(tmp_path) -> None:
    (tmp_path / "volume_event_research_report.json").write_text("{ this is not valid json")
    m = MOD._load_json_metrics(tmp_path)
    assert m["load_error"]


def test_load_json_metrics_valid_report_has_no_load_error(tmp_path) -> None:
    (tmp_path / "volume_event_research_report.json").write_text(
        '{"best_scenario": {"total_return": 1.5, "max_drawdown": -0.3, "trades": 99},'
        ' "date_range": {"start": "2023-04-01", "end": "2026-05-28"},'
        ' "pit_manifest": {"full_pit_universe_pass": true}, "run_label": "full_pit_universe"}'
    )
    m = MOD._load_json_metrics(tmp_path)
    assert m["load_error"] is None
    assert m["total_return"] == 1.5
    assert m["full_pit_pass"] is True


def test_annualize_and_engine_mar_stay_real_below_minus_100pct() -> None:
    """A >=100% cumulative loss makes (1+total_return) non-positive; a fractional
    power of it is COMPLEX in Python and crashes the math.isfinite() Tier-2 guard.
    Both helpers must floor to a real -1.0-based value (audit pass2 #3)."""
    ann = MOD._annualize(-1.3, 36)
    assert isinstance(ann, float) and math.isfinite(ann)
    assert ann == -1.0

    mar = MOD._engine_mar(-1.3, max_drawdown=-0.5, years=3.0)
    assert isinstance(mar, float) and math.isfinite(mar)
    # ann floored to -1.0, dd 0.5 -> mar -2.0; the point is it is REAL + finite, not complex.
    assert mar == -2.0

    # exactly -100% (growth == 0) must also stay real, not raise.
    assert MOD._annualize(-1.0, 12) == -1.0
