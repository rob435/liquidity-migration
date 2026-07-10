from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from scripts.check_residual_momentum_gate import MS_PER_DAY, inspect_gate


def test_gate_requires_provenance_and_current_stable_rows(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    today_ms = (now_ms // MS_PER_DAY) * MS_PER_DAY
    path = tmp_path / "residual_momentum.parquet"
    symbols = [f"S{i:02d}" for i in range(20)]
    pl.DataFrame(
        {
            "symbol": [*symbols, "S00"],
            "ts_ms": [today_ms] * 20 + [today_ms + MS_PER_DAY],
            "residual_momentum": [i / 1000 for i in range(20)] + [0.2],
            "is_provisional": [False] * 20 + [True],
        }
    ).write_parquet(path)

    result = inspect_gate(path, now_ms=now_ms)
    assert result["rows"] == 21
    assert result["stable_rows"] == 20
    assert result["latest_stable_ts_ms"] == today_ms
    assert result["latest_stable_symbols"] == 20
    assert result["current_stable_symbols"] == 20


def test_gate_rejects_legacy_or_stale_tables(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    path = tmp_path / "residual_momentum.parquet"
    pl.DataFrame({
        "symbol": ["A"],
        "ts_ms": [now_ms],
        "residual_momentum": [0.1],
    }).write_parquet(path)
    with pytest.raises(RuntimeError, match="is_provisional"):
        inspect_gate(path, now_ms=now_ms)

    today_ms = (now_ms // MS_PER_DAY) * MS_PER_DAY
    pl.DataFrame({
        "symbol": ["A"],
        "ts_ms": [today_ms - 11 * MS_PER_DAY],
        "residual_momentum": [0.1],
        "is_provisional": [False],
    }).write_parquet(path)
    with pytest.raises(RuntimeError, match="current-day stable cross-section"):
        inspect_gate(
            path,
            now_ms=now_ms,
            max_stable_age_days=20.0,
            min_current_stable_symbols=1,
        )


def test_gate_rejects_small_duplicate_and_nonfinite_cross_sections(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    today_ms = (now_ms // MS_PER_DAY) * MS_PER_DAY
    path = tmp_path / "residual_momentum.parquet"

    pl.DataFrame({
        "symbol": ["A"],
        "ts_ms": [today_ms],
        "residual_momentum": [0.1],
        "is_provisional": [False],
    }).write_parquet(path)
    with pytest.raises(RuntimeError, match="too small"):
        inspect_gate(path, now_ms=now_ms)

    pl.DataFrame({
        "symbol": ["A", "A"],
        "ts_ms": [today_ms, today_ms],
        "residual_momentum": [0.1, 0.2],
        "is_provisional": [False, False],
    }).write_parquet(path)
    with pytest.raises(RuntimeError, match="duplicate"):
        inspect_gate(path, now_ms=now_ms, min_current_stable_symbols=1)

    pl.DataFrame({
        "symbol": ["A"],
        "ts_ms": [today_ms],
        "residual_momentum": [float("nan")],
        "is_provisional": [False],
    }).write_parquet(path)
    with pytest.raises(RuntimeError, match="non-finite"):
        inspect_gate(path, now_ms=now_ms, min_current_stable_symbols=1)


def test_gate_allows_stable_tomorrow_only_when_today_is_covered(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    today_ms = (now_ms // MS_PER_DAY) * MS_PER_DAY
    path = tmp_path / "residual_momentum.parquet"
    pl.DataFrame({
        "symbol": ["A", "A"],
        "ts_ms": [today_ms, today_ms + MS_PER_DAY],
        "residual_momentum": [0.1, 0.2],
        "is_provisional": [False, False],
    }).write_parquet(path)

    result = inspect_gate(path, now_ms=now_ms, min_current_stable_symbols=1)
    assert result["latest_stable_ts_ms"] == today_ms + MS_PER_DAY
    assert result["current_stable_symbols"] == 1
