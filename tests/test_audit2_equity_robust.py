"""Regression tests for two crash-on-edge defects (audit2: equity_robust).

(1) scripts/equity_curves.py: the start-date year shift used
    ``today.replace(year=today.year - years)`` which raises ValueError when
    ``today`` is Feb 29 and the target year is not a leap year.
(2) scripts/continuous_deployed_equity.py stats(): ``mar`` divided by the
    absolute max drawdown, raising ZeroDivisionError for a no-drawdown curve.
    Fixed to return ``mar=None`` (matching reconciliation._calendar_metrics).

Both fixes must leave normal (finite) inputs byte-identical.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

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
deployed = _load("continuous_deployed_equity")


def _eq_df(returns: list[float]) -> pl.DataFrame:
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(len(returns))]
    ts = [
        int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
        for d in days
    ]
    return pl.DataFrame({"ts_ms": ts, "basket_return": returns})


# ---- defect (1): Feb-29 start-date shift ------------------------------------

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
    # The line under test (equity_curves.main ~line 295) computes this.
    start = equity_curves._shift_years(today, 3).isoformat()
    assert start == "2021-02-28"


# ---- defect (2): mar on a no-drawdown curve ---------------------------------

def test_stats_no_drawdown_returns_none_mar():
    # Monotonically increasing equity -> zero drawdown -> old code divided by 0.
    out = deployed.stats(_eq_df([0.01] * 10))
    assert out["mar"] is None
    assert out["max_drawdown_pct"] == 0.0


def test_stats_with_drawdown_mar_unchanged():
    # A path WITH drawdown must yield the exact same finite mar as before the
    # fix (recompute the legacy expression and compare).
    import numpy as np

    rets = [0.05, 0.05, -0.20, 0.03, 0.04, -0.02, 0.06]
    df = _eq_df(rets)
    out = deployed.stats(df)

    # Reconstruct the pre-fix computation independently.
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
    years = ncal / deployed.ANN
    expected_mar = round((total / years) / abs(float(dd.min())), 2)

    assert out["mar"] == expected_mar
    assert out["mar"] is not None
    assert out["max_drawdown_pct"] < 0.0
