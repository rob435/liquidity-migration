"""Tests for scripts/research/equity_curves.py: a start-date year shift clamps to
Feb 28 on Feb 29 instead of raising. Finite inputs stay byte-identical.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str):
    path = REPO / "scripts" / "research" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


equity_curves = _load("equity_curves")


# ---- Feb-29 start-date shift ------------------------------------------------

def test_old_inline_shift_crashes_on_feb29():
    # Proves the original expression is defective: replace(year=...) on Feb 29
    # to a non-leap target year raises ValueError.
    feb29 = dt.date(2024, 2, 29)
    with pytest.raises(ValueError):
        feb29.replace(year=feb29.year - 3)  # 2021 is not a leap year


def test_shift_years_no_raise_on_feb29():
    feb29 = dt.date(2024, 2, 29)
    # 3-year shift -> 2021 (not leap): must clamp to Feb 28, not raise.
    assert equity_curves._shift_years(feb29, 3) == dt.date(2021, 2, 28)
    # 4-year shift -> 2020 (leap): Feb 29 is valid and preserved.
    assert equity_curves._shift_years(feb29, 4) == dt.date(2020, 2, 29)


def test_shift_years_unchanged_for_normal_date():
    # Happy path: a non-Feb-29 date shifts exactly like the old expression.
    base = dt.date(2023, 6, 15)
    assert equity_curves._shift_years(base, 3) == base.replace(year=base.year - 3)


def test_main_start_computation_does_not_raise_on_feb29(monkeypatch):
    # Exercise the actual start-date path with an injected Feb-29 "today".
    feb29 = dt.date(2024, 2, 29)
    monkeypatch.setattr(equity_curves, "_today", lambda: feb29)
    today = equity_curves._today()
    start = equity_curves._shift_years(today, 3).isoformat()
    assert start == "2021-02-28"


# ---- stale replay state is always rebuilt -----------------------------------

def test_prepare_sleeve_output_clears_stale_replay_state(tmp_path: Path):
    # A kernel replay tape binds to the window that wrote it; a rerun with a
    # different window resumed onto it dies with "strategy event clock cannot
    # move backward" (hit 2026-08-19 against the 2026-07-24 tape). The prep
    # must rebuild the replay state even without --fresh-output, while other
    # artifacts stay for comparison until overwritten.
    out = tmp_path / "long"
    replay = out / "common_kernel_execution"
    replay.mkdir(parents=True)
    (replay / "strategy_event_tape.jsonl").write_text("{}\n")
    keep = out / "long_native_equity.csv"
    keep.write_text("date\n")
    equity_curves._prepare_sleeve_output(out, fresh=False)
    assert not replay.exists()
    assert keep.exists()
