"""Causality tests for the residual-momentum precompute (scripts/data/precompute_residual_momentum.py).

The residual is fit against a forward return that only completes ~2 days late, so
residual_momentum[D] must be a rolling sum shifted far enough that its newest term is
knowable strictly before the live consumer's earliest decision at D 00:00 UTC.
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
        "precompute_residual_momentum", REPO / "scripts" / "data" / "precompute_residual_momentum.py"
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
    """residual_momentum[D] must be invariant to residual_return at d in {D-2, D-1, D}:
    those forward residuals only complete after D's decision.
    """
    n = 16
    d_decision = 12
    base = [float(i) for i in range(n)]
    poisoned = list(base)
    for d in (d_decision - 2, d_decision - 1, d_decision):
        poisoned[d] = 1e9  # a value not knowable at D's decision
    assert _apply(base)[d_decision] == _apply(poisoned)[d_decision]


def test_residual_owner_exposes_stable_and_provisional_tail_explicitly() -> None:
    start_ms = MOD._date_str_to_ms("2025-01-01")
    resid = pl.DataFrame(
        {
            "symbol": ["AAA"] * 10,
            "ts_ms": [start_ms + day * DAY_MS for day in range(10)],
            "residual_return": [float(day) for day in range(10)],
        }
    )

    output = MOD.residual_momentum_from_residuals(resid, end="2025-01-15")
    last_real = start_ms + 9 * DAY_MS
    stable = output.filter(pl.col("ts_ms") <= last_real + 3 * DAY_MS)
    provisional = output.filter(pl.col("ts_ms") > last_real + 3 * DAY_MS)

    assert not stable.is_empty()
    assert stable["is_provisional"].to_list() == [False] * stable.height
    assert not provisional.is_empty()
    assert provisional["is_provisional"].to_list() == [True] * provisional.height


def test_residual_owner_keeps_final_causal_keys_after_symbol_ages_out() -> None:
    """A later global end must not erase a delisted symbol's final signals. With a
    seven-row window, four required samples, and shift three, six null calendar rows
    are enough to emit every final causal value; an end-relative cutoff drops these
    rows and makes full and incremental builds disagree.
    """
    start_ms = MOD._date_str_to_ms("2025-01-01")
    last_real = start_ms + 9 * DAY_MS
    resid = pl.DataFrame(
        {
            "symbol": ["DELISTED"] * 10,
            "ts_ms": [start_ms + day * DAY_MS for day in range(10)],
            "residual_return": [float(day) for day in range(10)],
        }
    )

    while_recent = MOD.residual_momentum_from_residuals(resid, end="2025-01-15")
    after_aging_out = MOD.residual_momentum_from_residuals(resid, end="2025-03-01")

    _assert_existing_keys_allclose(while_recent, after_aging_out)
    assert int(after_aging_out["ts_ms"].max()) == last_real + 6 * DAY_MS
    final = after_aging_out.filter(pl.col("ts_ms") > last_real)
    assert final.height == 6
    assert final["is_provisional"].to_list() == [False, False, False, True, True, True]


def test_residual_owner_rejects_incomplete_input_contract() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        MOD.residual_momentum_from_residuals(
            pl.DataFrame({"symbol": ["AAA"], "ts_ms": [0]}),
            end="2025-01-02",
        )


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
    """Forward targets arrive late: a padded tail value may legitimately change once its
    newest causal residual appears, while stable history stays numerically identical.
    """
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


def test_precompute_explicit_output_does_not_replace_shared_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_residual_inputs(monkeypatch)
    shared_path = tmp_path / "residual_momentum.parquet"
    shared_bytes = b"legacy-shared-rmom"
    shared_path.write_bytes(shared_bytes)
    run_path = tmp_path / "research-run" / "residual_momentum.parquet"

    rows = MOD.precompute(
        tmp_path,
        start="2025-01-01",
        end="2025-02-01",
        klines_dataset="klines_1h",
        append=False,
        output_path=run_path,
    )

    assert rows > 0
    assert run_path.exists()
    assert "is_provisional" in pl.read_parquet(run_path).columns
    assert shared_path.read_bytes() == shared_bytes


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


def test_residual_momentum_uses_the_registered_calendar_window_across_a_gap() -> None:
    """``rolling_sum(7).shift(3)`` is row-positional: for a gapped symbol it reaches the
    10th present row rather than calendar D-9..D-3, so the value stops being the
    registered definition and a later backfill changes an already-stable number.
    """

    day = DAY_MS
    # A contiguous symbol and a symbol missing three interior days, with the same
    # residual on every day that is present.
    contiguous = [(i, 1.0) for i in range(20)]
    gapped = [(i, 1.0) for i in range(20) if i not in (5, 6, 7)]
    frame = pl.DataFrame(
        {
            "symbol": ["AAAUSDT"] * len(contiguous) + ["BBBUSDT"] * len(gapped),
            "ts_ms": [i * day for i, _ in contiguous] + [i * day for i, _ in gapped],
            "residual_return": [value for _, value in contiguous]
            + [value for _, value in gapped],
        }
    )
    out = MOD.residual_momentum_from_residuals(frame, end="1970-01-20")
    by_symbol = {
        symbol: dict(zip(part["ts_ms"].to_list(), part["residual_momentum"].to_list()))
        for symbol, part in (
            (str(key[0] if isinstance(key, tuple) else key), value)
            for key, value in out.partition_by("symbol", as_dict=True).items()
        )
    }
    # Day 15 window is [D-9..D-3] = days 6..12. The gapped symbol is missing
    # days 6 and 7, so it must report 5, not the contiguous symbol's 7 --- and
    # certainly not a window silently stretched back to day 3.
    assert by_symbol["AAAUSDT"][15 * day] == pytest.approx(7.0)
    assert by_symbol["BBBUSDT"][15 * day] == pytest.approx(5.0)
    # Day 12 window is days 3..9: two of the three missing days fall inside.
    assert by_symbol["AAAUSDT"][12 * day] == pytest.approx(7.0)
    assert by_symbol["BBBUSDT"][12 * day] == pytest.approx(4.0)


def test_append_overlap_verify_catches_a_changed_signal_definition() -> None:
    """The calendar-window definition changes stable residual-momentum values for GAPPED
    symbols, so the append path must fail closed rather than mix two definitions in
    one artifact. The daily refresh is unaffected: it runs --full-rewrite.
    """

    import liquidity_migration.residual_momentum as rm

    gapped = [(index, 1.0) for index in range(30) if index not in (5, 6, 7)]
    resid = pl.DataFrame(
        {
            "symbol": ["BBBUSDT"] * len(gapped),
            "ts_ms": [index * DAY_MS for index, _ in gapped],
            "residual_return": [value for _, value in gapped],
        }
    )
    rebuilt = MOD.residual_momentum_from_residuals(resid, end="1970-02-05")

    # The row-positional definition, for contrast.
    original = rm._densify_daily_grid
    rm._densify_daily_grid = lambda frame: frame
    try:
        existing = MOD.residual_momentum_from_residuals(resid, end="1970-02-05")
    finally:
        rm._densify_daily_grid = original

    assert not existing.equals(rebuilt), "fixture must exercise a real definition change"
    with pytest.raises(RuntimeError, match="append overlap values changed") as excinfo:
        MOD._assert_append_overlap_matches(
            existing, rebuilt, overlap_start_ms=0, overlap_end_ms=30 * DAY_MS
        )
    message = str(excinfo.value)
    # The message must distinguish a deliberate definition change from source drift.
    assert "--full-rewrite" in message
    assert "DELIBERATE" in message
    assert "run_continuous_rmom_refresh.sh" in message


def test_deployed_rmom_refresh_uses_full_rewrite() -> None:
    """Pins the reason M21 cannot break the operational path."""

    script = (REPO / "scripts" / "runtime" / "run_continuous_rmom_refresh.sh").read_text(encoding="utf-8")
    assert "--full-rewrite" in script
