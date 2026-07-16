"""Causality tests for the residual-momentum precompute (scripts/precompute_residual_momentum.py).

These pin the fix for the CRITICAL rmom look-ahead (audit 2026-06-03): the residual is fit against a
FORWARD return that only completes ~2 days late, so residual_momentum[D] must be a rolling sum shifted
far enough that its NEWEST term is knowable strictly before the live consumer's earliest decision at
D 00:00 UTC. They fail under the old shift(1).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import polars as pl
import pytest

REPO = Path(__file__).resolve().parent.parent
DAY_MS = 86_400_000


def _load():
    spec = importlib.util.spec_from_file_location(
        "precompute_residual_momentum", REPO / "scripts" / "precompute_residual_momentum.py"
    )
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["precompute_residual_momentum"] = m
    spec.loader.exec_module(m)
    return m


MOD = _load()


def _apply(residual_returns: list[float | None]) -> list[float | None]:
    return (
        pl.DataFrame(
            {
                "symbol": ["AAA"] * len(residual_returns),
                "ts_ms": list(range(len(residual_returns))),
                "residual_return": residual_returns,
            }
        )
        .with_columns(MOD.residual_momentum_expr())["residual_momentum"]
        .to_list()
    )


def test_residual_momentum_is_causal_shift3() -> None:
    """residual_momentum[D] = sum residual_return[D-9 .. D-3] (rolling 7, shift 3)."""
    assert (MOD.RMOM_WINDOW, MOD.RMOM_CAUSAL_SHIFT) == (7, 3)
    rr = [float(i) for i in range(16)]  # residual_return[i] = i
    out = _apply(rr)
    assert out[12] == float(sum(range(3, 10)))   # D=12 -> sum(3..9) = 42
    assert out[15] == float(sum(range(6, 13)))   # D=15 -> sum(6..12) = 63


def test_residual_momentum_does_not_read_future_residuals() -> None:
    """Look-ahead guard: residual_momentum[D] must be INVARIANT to residual_return at d in
    {D-2, D-1, D} — those forward residuals only complete AFTER D's decision. (Fails under shift(1),
    which summed residual_return[D-1].)"""
    n = 16
    d_decision = 12
    base = [float(i) for i in range(n)]
    poisoned = list(base)
    for d in (d_decision - 2, d_decision - 1, d_decision):
        poisoned[d] = 1e9  # a value not knowable at D's decision
    assert _apply(base)[d_decision] == _apply(poisoned)[d_decision]


def _fake_factor_panel(start: str, end: str, *, offset: float = 0.0) -> pl.DataFrame:
    start_ms = MOD._date_str_to_ms(start)
    end_ms = MOD._date_str_to_ms(end)
    rows = []
    for ts_ms in range(start_ms, end_ms, DAY_MS):
        day = ts_ms // DAY_MS
        rows.append({"symbol": "AAA", "ts_ms": ts_ms, "residual_return": day * 0.001 + offset})
        rows.append({"symbol": "BBB", "ts_ms": ts_ms, "residual_return": day * -0.001 - offset})
    return pl.DataFrame(rows)


def _patch_residual_inputs(monkeypatch: pytest.MonkeyPatch, *, offset: float = 0.0, calls: list[tuple[str, str]] | None = None) -> None:
    def fake_build_factor_panel(root: Path, *, start: str, end: str, klines_dataset: str | None = None) -> pl.DataFrame:
        del root, klines_dataset
        if calls is not None:
            calls.append((start, end))
        return _fake_factor_panel(start, end, offset=offset)

    def fake_fit_factor_returns(panel: pl.DataFrame, *, factor_cols: list[str] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
        del factor_cols
        return pl.DataFrame(), panel.select(["symbol", "ts_ms", "residual_return"])

    monkeypatch.setattr(MOD, "build_factor_panel", fake_build_factor_panel)
    monkeypatch.setattr(MOD, "fit_factor_returns", fake_fit_factor_returns)


def _assert_existing_keys_allclose(left: pl.DataFrame, right: pl.DataFrame) -> None:
    compared = left.rename({"residual_momentum": "left_residual_momentum"}).join(
        right.rename({"residual_momentum": "right_residual_momentum"}), on=["symbol", "ts_ms"], how="inner"
    )
    assert compared.height == left.height
    left_vals = compared["left_residual_momentum"].to_numpy()
    right_vals = compared["right_residual_momentum"].to_numpy()
    assert np.array_equal(np.isnan(left_vals), np.isnan(right_vals))
    finite = ~np.isnan(left_vals)
    assert np.allclose(left_vals[finite], right_vals[finite], rtol=1e-10, atol=1e-12)


def test_precompute_appends_only_new_rmom_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    _patch_residual_inputs(monkeypatch, calls=calls)

    full_rows = MOD.precompute(
        tmp_path,
        start="2025-01-01",
        end="2025-02-01",
        klines_dataset="klines_1h",
        append=True,
        append_overlap_days=5,
    )
    assert full_rows > 0
    original = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    original_max = int(original["ts_ms"].max())

    appended_rows = MOD.precompute(
        tmp_path,
        start="2025-01-01",
        end="2025-02-06",
        klines_dataset="klines_1h",
        append=True,
        append_overlap_days=5,
    )

    updated = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    preserved = updated.filter(pl.col("ts_ms") <= original_max)
    _assert_existing_keys_allclose(original, preserved)
    assert appended_rows == updated.filter(pl.col("ts_ms") > original_max).height
    assert int(updated["ts_ms"].max()) > original_max
    assert calls[1][0] > "2025-01-01"


def test_precompute_append_refreshes_overlap_when_new_keys_appear(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def fake_build_factor_panel(root: Path, *, start: str, end: str, klines_dataset: str | None = None) -> pl.DataFrame:
        del root, klines_dataset
        calls.append((start, end))
        start_ms = MOD._date_str_to_ms(start)
        end_ms = MOD._date_str_to_ms(end)
        rows = []
        symbols = ["AAA", "BBB"] if len(calls) == 1 else ["AAA", "BBB", "CCC"]
        for ts_ms in range(start_ms, end_ms, DAY_MS):
            day = ts_ms // DAY_MS
            for idx, symbol in enumerate(symbols):
                rows.append({"symbol": symbol, "ts_ms": ts_ms, "residual_return": day * 0.001 + idx})
        return pl.DataFrame(rows)

    def fake_fit_factor_returns(panel: pl.DataFrame, *, factor_cols: list[str] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
        del factor_cols
        return pl.DataFrame(), panel.select(["symbol", "ts_ms", "residual_return"])

    monkeypatch.setattr(MOD, "build_factor_panel", fake_build_factor_panel)
    monkeypatch.setattr(MOD, "fit_factor_returns", fake_fit_factor_returns)

    MOD.precompute(
        tmp_path,
        start="2025-01-01",
        end="2025-02-01",
        klines_dataset="klines_1h",
        append=True,
        append_overlap_days=5,
    )
    original = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    original_max = int(original["ts_ms"].max())
    overlap_start = original_max - 5 * DAY_MS

    MOD.precompute(
        tmp_path,
        start="2025-01-01",
        end="2025-02-06",
        klines_dataset="klines_1h",
        append=True,
        append_overlap_days=5,
    )

    updated = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    assert updated.filter((pl.col("symbol") == "CCC") & (pl.col("ts_ms") < overlap_start)).is_empty()
    assert not updated.filter((pl.col("symbol") == "CCC") & (pl.col("ts_ms") >= overlap_start)).is_empty()
    _assert_existing_keys_allclose(original, updated)


def test_precompute_append_refreshes_overlap_when_output_already_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_build_factor_panel(root: Path, *, start: str, end: str, klines_dataset: str | None = None) -> pl.DataFrame:
        del root, klines_dataset
        calls.append((start, end))
        start_ms = MOD._date_str_to_ms(start)
        end_ms = MOD._date_str_to_ms(end)
        rows = []
        symbols = ["AAA", "BBB"] if len(calls) == 1 else ["AAA", "BBB", "CCC"]
        for ts_ms in range(start_ms, end_ms, DAY_MS):
            day = ts_ms // DAY_MS
            for idx, symbol in enumerate(symbols):
                rows.append({"symbol": symbol, "ts_ms": ts_ms, "residual_return": day * 0.001 + idx})
        return pl.DataFrame(rows)

    def fake_fit_factor_returns(panel: pl.DataFrame, *, factor_cols: list[str] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
        del factor_cols
        return pl.DataFrame(), panel.select(["symbol", "ts_ms", "residual_return"])

    monkeypatch.setattr(MOD, "build_factor_panel", fake_build_factor_panel)
    monkeypatch.setattr(MOD, "fit_factor_returns", fake_fit_factor_returns)

    MOD.precompute(
        tmp_path,
        start="2025-01-01",
        end="2025-02-01",
        klines_dataset="klines_1h",
        append=True,
        append_overlap_days=5,
    )
    original = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    original_max = int(original["ts_ms"].max())
    overlap_start = original_max - 5 * DAY_MS

    rows_added = MOD.precompute(
        tmp_path,
        start="2025-01-01",
        end="2025-02-01",
        klines_dataset="klines_1h",
        append=True,
        append_overlap_days=5,
    )

    updated = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    assert rows_added > 0
    assert int(updated["ts_ms"].max()) == original_max
    assert updated.filter((pl.col("symbol") == "CCC") & (pl.col("ts_ms") < overlap_start)).is_empty()
    assert not updated.filter((pl.col("symbol") == "CCC") & (pl.col("ts_ms") >= overlap_start)).is_empty()
    _assert_existing_keys_allclose(original, updated)


def test_precompute_append_allows_provisional_tail_to_mature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Forward targets arrive late: a padded tail value may legitimately
    change once its newest causal residual appears, while stable history must
    remain numerically identical."""
    def fake_build_factor_panel(
        root: Path, *, start: str, end: str, klines_dataset: str | None = None
    ) -> pl.DataFrame:
        del root, klines_dataset
        start_ms = MOD._date_str_to_ms(start)
        # Simulate the real fwd-return lag: the newest three daily rows are not
        # available to the residual fit yet.
        available_end_ms = MOD._date_str_to_ms(end) - 3 * DAY_MS
        rows = []
        for ts_ms in range(start_ms, available_end_ms, DAY_MS):
            day = ts_ms // DAY_MS
            rows.extend([
                {"symbol": "AAA", "ts_ms": ts_ms, "residual_return": day * 0.001},
                {"symbol": "BBB", "ts_ms": ts_ms, "residual_return": day * -0.001},
            ])
        return pl.DataFrame(rows)

    def fake_fit_factor_returns(
        panel: pl.DataFrame, *, factor_cols: list[str] | None = None
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        del factor_cols
        return pl.DataFrame(), panel.select(["symbol", "ts_ms", "residual_return"])

    monkeypatch.setattr(MOD, "build_factor_panel", fake_build_factor_panel)
    monkeypatch.setattr(MOD, "fit_factor_returns", fake_fit_factor_returns)
    MOD.precompute(
        tmp_path, start="2025-01-01", end="2025-02-01",
        klines_dataset="klines_1h", append=True, append_overlap_days=10,
    )
    original = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    edge_ts = int(original["ts_ms"].max())
    old_edge = original.filter(pl.col("ts_ms") == edge_ts)
    assert old_edge["is_provisional"].all()

    MOD.precompute(
        tmp_path, start="2025-01-01", end="2025-02-06",
        klines_dataset="klines_1h", append=True, append_overlap_days=10,
    )
    updated = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    new_edge = updated.filter(pl.col("ts_ms") == edge_ts)
    assert (~new_edge["is_provisional"]).all()
    assert not np.allclose(
        old_edge["residual_momentum"].to_numpy(),
        new_edge["residual_momentum"].to_numpy(),
    )
    stable_original = original.filter(~pl.col("is_provisional"))
    _assert_existing_keys_allclose(stable_original, updated)


def test_precompute_append_refuses_table_without_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_residual_inputs(monkeypatch)
    MOD.precompute(
        tmp_path, start="2025-01-01", end="2025-02-01",
        klines_dataset="klines_1h", append=True, append_overlap_days=10,
    )
    path = tmp_path / "residual_momentum.parquet"
    pl.read_parquet(path).drop("is_provisional").write_parquet(path)

    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="missing required residual_momentum columns"):
        MOD.precompute(
            tmp_path, start="2025-01-01", end="2025-02-06",
            klines_dataset="klines_1h", append=True, append_overlap_days=10,
        )
    assert path.read_bytes() == before


def test_precompute_append_refuses_overlap_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_residual_inputs(monkeypatch)
    MOD.precompute(
        tmp_path,
        start="2025-01-01",
        end="2025-02-01",
        klines_dataset="klines_1h",
        append=True,
        append_overlap_days=5,
    )
    original = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])

    _patch_residual_inputs(monkeypatch, offset=10.0)
    with pytest.raises(RuntimeError, match="overlap values changed"):
        MOD.precompute(
            tmp_path,
            start="2025-01-01",
            end="2025-02-06",
            klines_dataset="klines_1h",
            append=True,
            append_overlap_days=5,
        )

    after = pl.read_parquet(tmp_path / "residual_momentum.parquet").sort(["symbol", "ts_ms"])
    assert after.equals(original)
