"""Pins for the two mechanics the idio-chart conclusion rests on: ``_lagged`` carries
exactly the residual availability shift, and the Bonferroni family is counted right.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.core._common import MS_PER_DAY
from liquidity_migration.research.panels.idio_features import CHART_FEATURES, chart_features
from liquidity_migration.research.panels.residual_price import RESIDUAL_AVAILABILITY_SHIFT_DAYS

REPO = Path(__file__).resolve().parents[2]


def _load(group: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / group / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_idio_panel = _load("data", "build_idio_panel")
screen_idio_charts = _load("research", "screen_idio_charts")

BASE_DAY = 20_000


def _ms(day_index: int) -> int:
    return day_index * MS_PER_DAY


# ---------------------------------------------------------------------------
# the information-age control
# ---------------------------------------------------------------------------


def test_lagged_matches_the_residual_availability_shift_exactly() -> None:
    """``rawlag`` exists so a win for ``idio`` cannot be explained by the idio path
    being three days stale; that only holds if the lag is the same three days the
    residual path carries.
    """
    frame = pl.DataFrame({"symbol": ["A", "A"], "ts_ms": [_ms(BASE_DAY), _ms(BASE_DAY + 1)], "v": [1.0, 2.0]})
    out = build_idio_panel._lagged(frame, days=RESIDUAL_AVAILABILITY_SHIFT_DAYS)
    assert out["ts_ms"].to_list() == [
        _ms(BASE_DAY + RESIDUAL_AVAILABILITY_SHIFT_DAYS),
        _ms(BASE_DAY + 1 + RESIDUAL_AVAILABILITY_SHIFT_DAYS),
    ]
    assert RESIDUAL_AVAILABILITY_SHIFT_DAYS == 3


def test_lagged_control_carries_the_value_the_raw_arm_had_three_days_earlier() -> None:
    prices = pl.DataFrame(
        {
            "symbol": ["A"] * 40,
            "ts_ms": [_ms(BASE_DAY + i) for i in range(40)],
            "close": [100.0 + i for i in range(40)],
        }
    )
    raw = chart_features(prices, price_col="close", prefix="raw_")
    lagged = build_idio_panel._lagged(chart_features(prices, price_col="close", prefix="rawlag_"), days=3)

    at_d = raw.filter(pl.col("ts_ms") == _ms(BASE_DAY + 33))["raw_ret_7d"].item()
    at_d_lag = lagged.filter(pl.col("ts_ms") == _ms(BASE_DAY + 36))["rawlag_ret_7d"].item()
    assert at_d == pytest.approx(at_d_lag)


# ---------------------------------------------------------------------------
# the multiple-testing family
# ---------------------------------------------------------------------------


def test_bonferroni_family_counts_the_whole_predeclared_grid() -> None:
    n_cells = (
        len(CHART_FEATURES)
        * len(screen_idio_charts.ALL_SOURCES)
        * len(screen_idio_charts.TARGETS)
    )
    assert n_cells == 48
    family = screen_idio_charts.PRIOR_MECHANISMS + n_cells
    assert family == 92
    # The same approximation reproduces the program's standing threshold for
    # its 44 prior mechanisms, so this screen is scored on the repo's own
    # convention rather than a private one.
    assert screen_idio_charts.PROGRAM_T == pytest.approx(2.5), "docs/research/governance.md 2 owns the bar"
    assert screen_idio_charts.bonferroni_t(screen_idio_charts.PRIOR_MECHANISMS) == pytest.approx(3.25, abs=0.01)
    t_crit = screen_idio_charts.bonferroni_t(family)
    # Adding tests must raise the bar, never lower it.
    assert t_crit == pytest.approx(3.4584, abs=0.001)
    assert t_crit > 3.25


def test_bonferroni_t_rises_with_the_number_of_tests() -> None:
    assert screen_idio_charts.bonferroni_t(1) < screen_idio_charts.bonferroni_t(44)
    assert screen_idio_charts.bonferroni_t(44) < screen_idio_charts.bonferroni_t(92)
    assert screen_idio_charts.bonferroni_t(1) == pytest.approx(1.959963985, rel=1e-6)


# ---------------------------------------------------------------------------
# scoring plumbing
# ---------------------------------------------------------------------------


def test_score_returns_none_on_an_unscorable_cell_rather_than_a_fabricated_summary() -> None:
    empty = pl.DataFrame(
        schema={"ts_ms": pl.Int64, "symbol": pl.String, "sig": pl.Float64, "ret": pl.Float64}
    )
    result, n = screen_idio_charts.score(
        empty, signal="sig", target="ret", cost_bp=0.0, cut=0.1, min_names=20
    )
    assert result is None and n == 0


def test_cost_conventions_are_ordered_gross_then_1x_then_2x() -> None:
    rng = [float(i % 17) - 8.0 for i in range(400)]
    frame = pl.DataFrame(
        {
            "ts_ms": [_ms(BASE_DAY + i // 20) for i in range(400)],
            "symbol": [f"S{i % 20:02d}" for i in range(400)],
            "sig": rng,
            "ret": [v / 1000.0 for v in rng],
        }
    )
    gross, _ = screen_idio_charts.score(frame, signal="sig", target="ret", cost_bp=0.0, cut=0.1, min_names=5)
    one_x, _ = screen_idio_charts.score(
        frame, signal="sig", target="ret",
        cost_bp=screen_idio_charts.MEASURED_ROUND_TRIP_BP, cut=0.1, min_names=5,
    )
    two_x, _ = screen_idio_charts.score(
        frame, signal="sig", target="ret",
        cost_bp=2.0 * screen_idio_charts.MEASURED_ROUND_TRIP_BP, cut=0.1, min_names=5,
    )
    assert gross.mean_bp > one_x.mean_bp > two_x.mean_bp
    assert one_x.mean_bp - two_x.mean_bp == pytest.approx(screen_idio_charts.MEASURED_ROUND_TRIP_BP)


def test_price_sources_and_features_are_declared_consistently_across_modules() -> None:
    assert set(screen_idio_charts.ALL_SOURCES) == set(build_idio_panel.PRICE_SOURCES)
    assert set(screen_idio_charts.PRIMARY_SOURCES) <= set(build_idio_panel.PRICE_SOURCES)
    # The primary read is the information-matched pair, not raw-vs-idio.
    assert set(screen_idio_charts.PRIMARY_SOURCES) == {"rawlag", "idio"}
