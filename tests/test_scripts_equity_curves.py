"""Tests for scripts/equity_curves.py (+ scripts/continuous_deployed_equity_refresh.py).

Covers two crash-on-edge fixes and the continuous frozen-start routing (audit2):
(1) start-date year shift no longer raises on Feb 29 (clamps to Feb 28);
(2) continuous_deployed_equity_refresh.stats() returns mar=None on a no-drawdown curve
    instead of dividing by zero;
(3) the continuous sleeve inherits the frozen deployed start (start_date=None)
    unless --start/--years explicitly asks for a window.
Normal (finite) inputs stay byte-identical.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


equity_curves = _load("equity_curves")
continuous_refresh = _load("continuous_deployed_equity_refresh")


def _eq_df(returns: list[float]) -> pl.DataFrame:
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(len(returns))]
    ts = [
        int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
        for d in days
    ]
    return pl.DataFrame({"ts_ms": ts, "basket_return": returns})


# ---- audit2: defect (1) — Feb-29 start-date shift ---------------------------

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


# ---- audit2: defect (2) — mar on a no-drawdown curve ------------------------

def test_stats_no_drawdown_returns_none_mar():
    # Monotonically increasing equity -> zero drawdown -> old code divided by 0.
    out = continuous_refresh.stats(_eq_df([0.01] * 10))
    assert out["mar"] is None
    assert out["max_drawdown_pct"] == 0.0


def test_stats_with_drawdown_mar_unchanged():
    # A path WITH drawdown must yield the exact same finite mar as before the fix.
    import numpy as np

    rets = [0.05, 0.05, -0.20, 0.03, 0.04, -0.02, 0.06]
    df = _eq_df(rets)
    out = continuous_refresh.stats(df)

    dates = [
        dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).date()
        for t in df["ts_ms"].to_list()
    ]
    series_rets = df["basket_return"].to_numpy()
    ncal = (dates[-1] - dates[0]).days + 1
    series = np.zeros(ncal)
    for d, r in zip(dates, series_rets):
        series[(d - dates[0]).days] = r
    eq = np.cumprod(1.0 + series)
    dd = eq / np.maximum.accumulate(eq) - 1.0
    total = float(eq[-1] - 1.0)
    years = ncal / continuous_refresh.ANN
    expected_mar = round((total / years) / abs(float(dd.min())), 2)

    assert out["mar"] == expected_mar
    assert out["mar"] is not None
    assert out["max_drawdown_pct"] < 0.0


# ---- audit2c: defect (3) — continuous inherits the frozen deployed start ----

def _drive_main(monkeypatch, tmp_path: Path, argv: list[str]) -> dict[str, object]:
    """Run ``main`` for the continuous sleeve and capture run_venue's kwargs."""
    captured: dict[str, object] = {}

    def fake_run_venue(venue, **kwargs):
        captured["venue"] = venue
        captured.update(kwargs)
        return {"1x": {"total_return_pct": 10.0, "max_drawdown_pct": -5.0, "mar": 2.0}}

    monkeypatch.setitem(
        sys.modules,
        "continuous_deployed_equity_refresh",
        SimpleNamespace(run_venue=fake_run_venue),
    )
    monkeypatch.setattr(equity_curves, "load_config", lambda _path: SimpleNamespace(costs=object()))
    monkeypatch.setattr(equity_curves, "_find_png", lambda _out: None)
    monkeypatch.setattr(equity_curves, "_plot_equity_csv", lambda _out, _sleeve: None)

    root = tmp_path / "bybit_full_pit"
    out = tmp_path / "reports"
    full_argv = [
        "equity_curves", "--sleeves", "continuous", "--root", str(root),
        "--out", str(out), "--end", "2026-06-12", *argv,
    ]
    monkeypatch.setattr(sys, "argv", full_argv)
    assert equity_curves.main() == 0
    return captured


def test_continuous_inherits_frozen_start_when_window_unset(monkeypatch, tmp_path: Path) -> None:
    captured = _drive_main(monkeypatch, tmp_path, argv=[])
    # The frozen deployed start (~2023-04) is preserved, NOT a rolling 3y override.
    assert captured["start_date"] is None
    assert captured["venue"] == "bybit"


def test_continuous_uses_explicit_start_when_given(monkeypatch, tmp_path: Path) -> None:
    captured = _drive_main(monkeypatch, tmp_path, argv=["--start", "2024-01-15"])
    assert captured["start_date"] == "2024-01-15"


def test_continuous_uses_rolling_start_when_years_given(monkeypatch, tmp_path: Path) -> None:
    # Explicit --years opts into the rolling window: a concrete date flows through.
    captured = _drive_main(monkeypatch, tmp_path, argv=["--years", "2"])
    start = captured["start_date"]
    assert isinstance(start, str)
    assert start != "" and start[:2] == "20"
