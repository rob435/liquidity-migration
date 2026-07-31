"""Alignment, causality, and gap-safety pins for the idio-chart builder.

Every failure mode here (a two-day back-dating, a positional window stretched by a gap,
a spliced delist hole) shows up as a chart that changes retroactively, which is how a
residual breakout signal acquires a Sharpe it cannot trade.
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from liquidity_migration._common import MS_PER_DAY
from liquidity_migration.residual_momentum import RMOM_CAUSAL_SHIFT
from liquidity_migration.residual_price import (
    RESIDUAL_AVAILABILITY_SHIFT_DAYS,
    add_log_forward_return,
    build_idio_price,
)

BASE_DAY = 20_000  # ~2024-10-04; any daily-grid origin works


def _ms(day_index: int) -> int:
    return day_index * MS_PER_DAY


def _iso(day_index: int) -> str:
    return datetime.fromtimestamp(_ms(day_index) / 1000, tz=timezone.utc).date().isoformat()


def _residuals(values: list[float], *, symbol: str = "AAA", first_day: int = BASE_DAY) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(values),
            "ts_ms": [_ms(first_day + i) for i in range(len(values))],
            "residual_return": [float(v) for v in values],
        }
    )


def _wave(n: int, *, scale: float = 1.0, first_day: int = BASE_DAY, symbol: str = "AAA") -> pl.DataFrame:
    """Deterministic non-constant residual series (needs a positive stdev)."""
    values = [scale * (0.01 * ((i * 7) % 11 - 5) + 0.002 * ((i * 3) % 5)) for i in range(n)]
    return _residuals(values, symbol=symbol, first_day=first_day)


# ---------------------------------------------------------------------------
# alignment
# ---------------------------------------------------------------------------


def test_availability_shift_matches_the_residual_momentum_owner() -> None:
    # Both constants encode the same fact: residual_return[d] describes the move
    # completing at 01:00 UTC on d+2, so d+3 is the first readable decision day.
    assert RESIDUAL_AVAILABILITY_SHIFT_DAYS == RMOM_CAUSAL_SHIFT


def test_a_spike_lands_three_days_after_its_residual_day() -> None:
    values = [0.0] * 10
    values[5] = 0.5
    chart = build_idio_price(_residuals(values), end=_iso(BASE_DAY + 20))

    assert chart["ts_ms"].min() == _ms(BASE_DAY + RESIDUAL_AVAILABILITY_SHIFT_DAYS)
    moved = chart.filter(pl.col("idio_logret") != 0.0)
    assert moved["ts_ms"].to_list() == [_ms(BASE_DAY + 5 + RESIDUAL_AVAILABILITY_SHIFT_DAYS)]

    # The level steps on that day and not before: a back-dated cumulation would
    # move the chart on BASE_DAY+5 or BASE_DAY+6.
    before = chart.filter(pl.col("ts_ms") < _ms(BASE_DAY + 8))["idio_close"].to_list()
    assert all(value == pytest.approx(1.0) for value in before)
    at_spike = chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 8))["idio_close"].item()
    assert at_spike == pytest.approx(1.5)


def test_close_is_the_exact_compounded_path_not_a_cumsum_of_simple_returns() -> None:
    chart = build_idio_price(_residuals([0.1, -0.1]), end=_iso(BASE_DAY + 10))
    closes = chart.sort("ts_ms")["idio_close"].head(2).to_list()
    # 1.1 * 0.9 = 0.99, not 1 + 0.1 - 0.1 = 1.0.
    assert closes == [pytest.approx(1.1), pytest.approx(0.99)]


# ---------------------------------------------------------------------------
# causality -- the headline test
# ---------------------------------------------------------------------------


def test_chart_is_invariant_to_data_after_the_read_day() -> None:
    early_end = _iso(BASE_DAY + 43)
    early = build_idio_price(_wave(40), end=early_end)

    # Same history, then 40 days of violent future the early build cannot see.
    future = _residuals(
        [0.4 * (-1) ** i for i in range(40)],
        first_day=BASE_DAY + 40,
    )
    late = build_idio_price(
        pl.concat([_wave(40), future], how="vertical"),
        end=_iso(BASE_DAY + 90),
    )

    overlap = late.filter(pl.col("ts_ms") < _ms(BASE_DAY + 43)).sort(["symbol", "ts_ms"])
    assert not overlap.is_empty()
    assert_frame_equal(early.sort(["symbol", "ts_ms"]), overlap, check_exact=True)


def test_row_existence_is_invariant_to_a_later_relist() -> None:
    """Row EXISTENCE must be causal too, not just values.

    Spanning each symbol's ``[first, last]`` residual day keeps every value causal
    while making row existence depend on the future: a symbol dark at the read day
    gains rows for the dark period only because it relisted afterwards, so any signal
    whose universe is "symbols with a row today" selects on future listing status.
    """
    history = _residuals([0.01] * 10, first_day=BASE_DAY)       # readable 3..12
    relist = _residuals([0.01] * 10, first_day=BASE_DAY + 30)   # readable 33..42
    read_day = _ms(BASE_DAY + 20)

    real_time = build_idio_price(history, end=_iso(BASE_DAY + 20))
    hindsight = build_idio_price(
        pl.concat([history, relist], how="vertical"), end=_iso(BASE_DAY + 50)
    ).filter(pl.col("ts_ms") < read_day)

    assert_frame_equal(
        real_time.sort(["symbol", "ts_ms"]),
        hindsight.sort(["symbol", "ts_ms"]),
        check_exact=True,
    )
    # The dark period emits nothing beyond the stale tolerance, in both builds.
    assert real_time["ts_ms"].max() == _ms(BASE_DAY + 17)


def test_one_symbols_future_cannot_move_another_symbols_chart() -> None:
    quiet = _wave(40, symbol="AAA")
    alone = build_idio_price(quiet, end=_iso(BASE_DAY + 60))
    together = build_idio_price(
        pl.concat([quiet, _wave(40, scale=25.0, symbol="BBB")], how="vertical"),
        end=_iso(BASE_DAY + 60),
    ).filter(pl.col("symbol") == "AAA")

    assert_frame_equal(alone, together.sort(["symbol", "ts_ms"]), check_exact=True)


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------


def test_a_short_calendar_gap_is_flat_and_flagged_never_spliced() -> None:
    present = _residuals([0.01] * 5, first_day=BASE_DAY)
    after_gap = _residuals([0.02] * 5, first_day=BASE_DAY + 8)  # days +5,+6,+7 missing
    chart = build_idio_price(
        pl.concat([present, after_gap], how="vertical"),
        end=_iso(BASE_DAY + 30),
    ).sort("ts_ms")

    # Readable days 3..7 and 11..15, each licensing max_stale_days=5 forward.
    days = chart["ts_ms"].to_list()
    assert days == [_ms(d) for d in range(BASE_DAY + 3, BASE_DAY + 21)]

    filled = chart.filter(pl.col("is_filled"))["ts_ms"].to_list()
    assert filled == [_ms(BASE_DAY + d) for d in (8, 9, 10, 16, 17, 18, 19, 20)]

    # Flat across the hole, and the post-gap residual arrives at +3 as usual.
    level_at_gap = chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 7))["idio_close"].item()
    for day in (BASE_DAY + 8, BASE_DAY + 9, BASE_DAY + 10):
        assert chart.filter(pl.col("ts_ms") == _ms(day))["idio_close"].item() == pytest.approx(level_at_gap)
    resumed = chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 11))["idio_close"].item()
    assert resumed == pytest.approx(level_at_gap * 1.02)


def test_a_gap_longer_than_max_stale_days_breaks_the_chart() -> None:
    history = _residuals([0.01] * 10, first_day=BASE_DAY)          # readable 3..12
    resumed = _residuals([0.01] * 5, first_day=BASE_DAY + 33)      # readable 36..40
    chart = build_idio_price(
        pl.concat([history, resumed], how="vertical"),
        end=_iso(BASE_DAY + 60),
        max_stale_days=5,
    ).sort("ts_ms")

    days = chart["ts_ms"].to_list()
    # 3..17 (last readable 12 + 5 stale), then nothing until the chart resumes.
    assert days == [_ms(d) for d in (*range(BASE_DAY + 3, BASE_DAY + 18), *range(BASE_DAY + 36, BASE_DAY + 46))]
    assert chart.filter(
        (pl.col("ts_ms") > _ms(BASE_DAY + 17)) & (pl.col("ts_ms") < _ms(BASE_DAY + 36))
    ).is_empty()


def test_coverage_divides_by_the_window_not_by_the_rows_that_exist() -> None:
    """A young chart must not report 1.0. Dividing real days by the rows present in the
    window makes every symbol's warm-up score 1.0, and a symbol younger than the
    window sits at its own rolling extreme by construction, so a ``coverage > 0.9``
    screen on an idio breakout would fill its top decile with recent listings.
    """
    chart = build_idio_price(
        _residuals([0.01] * 40, first_day=BASE_DAY),
        end=_iso(BASE_DAY + 60),
        coverage_window=30,
    ).sort("ts_ms")

    # First readable day: one real day inside a thirty-day window.
    assert chart["coverage"].head(1).item() == pytest.approx(1 / 30)
    # Fully warm: readable days 13..42 are all real.
    assert chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 42))["coverage"].item() == pytest.approx(1.0)
    # Trailing stale days decay rather than holding at 1.0.
    assert chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 47))["coverage"].item() == pytest.approx(25 / 30)
    assert chart["coverage"].max() <= 1.0


# ---------------------------------------------------------------------------
# volatility normalisation
# ---------------------------------------------------------------------------


def test_zpath_is_scale_invariant_in_log_residual_space() -> None:
    chart = build_idio_price(
        pl.concat([_wave(60, symbol="CALM"), _wave(60, scale=10.0, symbol="WILD")], how="vertical"),
        end=_iso(BASE_DAY + 80),
        residual_scale="log",
    )
    calm = chart.filter(pl.col("symbol") == "CALM").sort("ts_ms")
    wild = chart.filter(pl.col("symbol") == "WILD").sort("ts_ms")

    # Raw paths differ by an order of magnitude; standardised paths do not.
    calm_last = calm.drop_nulls("idio_logret")["idio_logret"].tail(1).item()
    wild_last = wild.drop_nulls("idio_logret")["idio_logret"].tail(1).item()
    assert abs(wild_last) > 5 * abs(calm_last)
    for lhs, rhs in zip(calm["idio_zpath"].to_list(), wild["idio_zpath"].to_list()):
        assert lhs == pytest.approx(rhs, rel=1e-9, abs=1e-9)


def test_declaring_the_residual_scale_matters_more_as_residuals_grow() -> None:
    """``residual_scale`` is explicit because the gap is not small: ``log1p(r) < r`` for
    every ``r != 0``, so reading a series as simple returns yields a path at or below
    reading it as log returns. At ~1% daily idio magnitudes the two agree to well
    under a percent; at 50% daily residuals the drag dominates -- which is also why a
    plain ``cumsum`` of simple residuals is wrong.
    """

    def terminal(residuals: pl.DataFrame, scale: str) -> float:
        chart = build_idio_price(residuals, end=_iso(BASE_DAY + 80), residual_scale=scale)
        return chart.sort("ts_ms")["idio_close"].tail(1).item()

    small = _wave(60, scale=0.2)
    assert terminal(small, "simple") == pytest.approx(terminal(small, "log"), rel=0.01)
    assert terminal(small, "simple") < terminal(small, "log")

    large = _wave(60, scale=10.0)
    assert terminal(large, "simple") < 0.5 * terminal(large, "log")


def test_vol_gate_counts_real_observations_not_grid_rows() -> None:
    """``vol_min_samples`` must mean observations, not calendar-grid age. polars
    ``rolling_std_by`` resolves ``min_samples`` against the ROWS inside the time
    window, so estimating on the densified grid lets a two-observation stdev through a
    ``min_samples=15`` gate and emits 40-sigma z-scores. The estimate is taken on the
    sparse frame, where the rows in the window ARE the observations.
    """
    history = _residuals([0.01 * ((i % 5) - 2) for i in range(10)], first_day=BASE_DAY)
    resumed = _residuals([0.30], first_day=BASE_DAY + 33)  # readable BASE+36
    chart = build_idio_price(
        pl.concat([history, resumed], how="vertical"),
        end=_iso(BASE_DAY + 60),
        vol_window=30,
        vol_min_samples=15,
    )

    # Only six real residual days lie in the strictly-prior 30-day window.
    row = chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 36))
    assert row["is_filled"].item() is False
    assert row["idio_logret"].item() == pytest.approx(0.2623642644674911)  # log1p(0.30)
    assert row["idio_logret_vn"].item() is None

    # And the gate still opens once the observations are genuinely there.
    dense = build_idio_price(_wave(60), end=_iso(BASE_DAY + 80), vol_window=30, vol_min_samples=15)
    assert dense.filter(pl.col("ts_ms") == _ms(BASE_DAY + 40))["idio_logret_vn"].item() is not None


def test_vol_min_samples_above_vol_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsatisfiable"):
        build_idio_price(_wave(40), end=_iso(BASE_DAY + 60), vol_window=10, vol_min_samples=20)


def test_vol_normalisation_uses_a_strictly_prior_window() -> None:
    chart = build_idio_price(_wave(60), end=_iso(BASE_DAY + 80), vol_min_samples=5)
    warm = chart.sort("ts_ms").head(5)
    # Fewer than vol_min_samples strictly-prior observations -> no z-score.
    assert warm["idio_logret_vn"].null_count() == 5
    assert warm["idio_zpath"].to_list() == [0.0] * 5


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def test_duplicate_symbol_day_residuals_fail_loudly() -> None:
    duped = pl.concat([_residuals([0.01]), _residuals([0.02])], how="vertical")
    with pytest.raises(ValueError, match="duplicate"):
        build_idio_price(duped, end=_iso(BASE_DAY + 10))


def test_non_daily_timestamps_fail_loudly() -> None:
    hourly = _residuals([0.01, 0.02]).with_columns(pl.col("ts_ms") + 3_600_000)
    with pytest.raises(ValueError, match="daily grid"):
        build_idio_price(hourly, end=_iso(BASE_DAY + 10))


def test_missing_columns_fail_loudly() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        build_idio_price(pl.DataFrame({"symbol": ["A"], "ts_ms": [_ms(BASE_DAY)]}), end=_iso(BASE_DAY + 10))


def test_total_loss_residual_is_dropped_rather_than_infinite() -> None:
    chart = build_idio_price(_residuals([0.01, -1.0, 0.01]), end=_iso(BASE_DAY + 10))
    assert chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 4))["is_filled"].item() is True
    assert chart["idio_close"].is_finite().all()


def test_nan_and_infinite_residuals_are_dropped_in_both_scales() -> None:
    """The ``is_finite`` guard is the only defence, and in log scale the only one:
    ``NaN > -1.0`` evaluates True in polars, so NaN passes the total-loss check and
    reaches ``log1p`` unchanged, turning the rest of the symbol's path into NaN.
    """
    for scale in ("simple", "log"):
        chart = build_idio_price(
            _residuals([0.01, float("nan"), 0.01, float("inf"), 0.01]),
            end=_iso(BASE_DAY + 20),
            residual_scale=scale,
        )
        assert chart["idio_close"].is_finite().all(), scale
        assert chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 4))["is_filled"].item() is True
        assert chart.filter(pl.col("ts_ms") == _ms(BASE_DAY + 6))["is_filled"].item() is True


def test_symbol_spans_are_independent_when_histories_are_staggered() -> None:
    """A union span would give a short symbol the long symbol's grid."""
    chart = build_idio_price(
        pl.concat([_wave(10, symbol="SHORT"), _wave(60, symbol="LONG")], how="vertical"),
        end=_iso(BASE_DAY + 90),
    )
    short_max = chart.filter(pl.col("symbol") == "SHORT")["ts_ms"].max()
    long_max = chart.filter(pl.col("symbol") == "LONG")["ts_ms"].max()
    # last readable day + max_stale_days, per symbol.
    assert short_max == _ms(BASE_DAY + 9 + 3 + 5)
    assert long_max == _ms(BASE_DAY + 59 + 3 + 5)


def test_empty_input_returns_the_typed_empty_frame() -> None:
    empty = build_idio_price(
        pl.DataFrame(schema={"symbol": pl.String, "ts_ms": pl.Int64, "residual_return": pl.Float64}),
        end=_iso(BASE_DAY + 10),
    )
    assert empty.is_empty()
    assert empty.columns == [
        "symbol", "ts_ms", "idio_logret", "idio_close",
        "idio_logret_vn", "idio_zpath", "is_filled", "coverage",
    ]


# ---------------------------------------------------------------------------
# log target helper
# ---------------------------------------------------------------------------


def test_add_log_forward_return_is_exact_and_nulls_total_loss() -> None:
    panel = pl.DataFrame({"fwd_ret_1d": [0.1, -0.5, -1.0, -1.2, None]})
    out = add_log_forward_return(panel)["fwd_logret_1d"].to_list()
    assert out[0] == pytest.approx(0.09531017980432486)
    assert out[1] == pytest.approx(-0.6931471805599453)
    assert out[2] is None
    assert out[3] is None
    assert out[4] is None


def test_log_scale_residuals_skip_the_conversion() -> None:
    simple = build_idio_price(_residuals([0.1, -0.1]), end=_iso(BASE_DAY + 10))
    as_log = build_idio_price(
        _residuals([0.1, -0.1]), end=_iso(BASE_DAY + 10), residual_scale="log"
    )
    # exp(0.1) != 1.1, so declaring the scale must change the path.
    assert as_log["idio_close"].head(1).item() == pytest.approx(1.1051709180756477)
    assert simple["idio_close"].head(1).item() == pytest.approx(1.1)


def test_unknown_residual_scale_fails_loudly() -> None:
    with pytest.raises(ValueError, match="residual_scale"):
        build_idio_price(_residuals([0.1]), end=_iso(BASE_DAY + 10), residual_scale="pct")
