from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from breadth_power import daily_sharpe, days_to_t  # noqa: E402


def test_independent_bets_scale_sharpe_by_sqrt_n() -> None:
    one = daily_sharpe(15.0, 300.0, 1.0, 0.0)
    four = daily_sharpe(15.0, 300.0, 4.0, 0.0)
    assert four == pytest.approx(2.0 * one)


def test_days_shrink_linearly_in_n_when_uncorrelated() -> None:
    assert days_to_t(15.0, 300.0, 4.0, 0.0, 2.0) == pytest.approx(
        days_to_t(15.0, 300.0, 1.0, 0.0, 2.0) / 4.0
    )


def test_full_correlation_limit_removes_breadth_benefit() -> None:
    rho = 0.999999
    one = daily_sharpe(15.0, 300.0, 1.0, rho)
    many = daily_sharpe(15.0, 300.0, 40.0, rho)
    assert many == pytest.approx(one, rel=1e-3)


def test_single_bet_matches_textbook_t() -> None:
    # SR_d = edge/vol; T = (t/SR)^2
    assert days_to_t(15.0, 300.0, 1.0, 0.0, 2.0) == pytest.approx(
        (2.0 / (15.0 / 300.0)) ** 2
    )
    assert daily_sharpe(15.0, 300.0, 1.0, 0.0) == pytest.approx(0.05)


def test_invalid_inputs_rejected() -> None:
    with pytest.raises(ValueError):
        daily_sharpe(0.0, 300.0, 1.0, 0.0)
    with pytest.raises(ValueError):
        daily_sharpe(15.0, 300.0, 1.0, 1.0)


def test_annualization_sanity() -> None:
    sr_annual = daily_sharpe(15.0, 300.0, 10.0, 0.15) * math.sqrt(365)
    assert 1.5 < sr_annual < 2.5
