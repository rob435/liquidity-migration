from __future__ import annotations

from pathlib import Path


def test_volume_overnight_runner_locks_5950x_defaults() -> None:
    repo = Path(__file__).resolve().parents[1]
    runner = (repo / "scripts" / "run_volume_overnight_5950x.ps1").read_text(encoding="utf-8")

    assert '[string]$Preset = "promotion"' in runner
    assert "[int]$Workers = 16" in runner
    assert '$env:VOLUME_GRID_BACKEND = "thread"' in runner
    assert '$env:POLARS_MAX_THREADS = "1"' in runner
    assert '$env:RAYON_NUM_THREADS = "1"' in runner
    assert '$env:OMP_NUM_THREADS = "1"' in runner
    assert '$env:MKL_NUM_THREADS = "1"' in runner
    assert '@("pull", "--ff-only", "origin", "main")' in runner
    assert "scripts/run_volume_grid_splits.py" in runner
    assert "scripts/evaluate_volume_promotion.py" in runner
    assert '"--max-worst-drawdown", "-0.35"' in runner
    assert '"--min-avg-sharpe", "0.5"' in runner
    assert "$promotionExit -notin @(0, 2)" in runner
