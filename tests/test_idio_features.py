"""Pins for the source-agnostic chart features: they are causal, and invariant to the
arbitrary base level of an idio path (otherwise the idio arm would be a function of
each symbol's listing date).
"""

from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.core._common import MS_PER_DAY
from liquidity_migration.research.panels.idio_features import CHART_FEATURES, chart_features

BASE_DAY = 20_000


def _ms(day_index: int) -> int:
    return day_index * MS_PER_DAY


def _path(values: list[float], *, symbol: str = "AAA", first_day: int = BASE_DAY) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(values),
            "ts_ms": [_ms(first_day + i) for i in range(len(values))],
            "close": [float(v) for v in values],
        }
    )


def _ramp(n: int, *, start: float = 100.0, step: float = 1.0, symbol: str = "AAA") -> pl.DataFrame:
    return _path([start + step * i for i in range(n)], symbol=symbol)


# ---------------------------------------------------------------------------
# causality
# ---------------------------------------------------------------------------


def test_features_are_invariant_to_appended_future_prices() -> None:
    early = chart_features(_ramp(60))
    late = chart_features(_ramp(120)).filter(pl.col("ts_ms") < _ms(BASE_DAY + 60))
    assert not early.is_empty()
    assert early.sort("ts_ms").equals(late.sort("ts_ms"))


def test_a_fresh_high_reads_exactly_zero_and_a_fresh_low_reads_exactly_one() -> None:
    rising = chart_features(_ramp(40)).sort("ts_ms")
    warm = rising.drop_nulls("dist_from_30d_high")
    # A monotonically rising path prints a new 30d high every day.
    assert warm["dist_from_30d_high"].to_list() == [pytest.approx(0.0)] * warm.height
    assert warm["range_pos_30d"].to_list() == [pytest.approx(1.0)] * warm.height

    falling = chart_features(_path([100.0 - i for i in range(40)])).sort("ts_ms")
    warm_down = falling.drop_nulls("range_pos_30d")
    assert warm_down["range_pos_30d"].to_list() == [pytest.approx(0.0)] * warm_down.height


# ---------------------------------------------------------------------------
# scale invariance -- the property the idio arm depends on
# ---------------------------------------------------------------------------


def test_every_feature_is_invariant_to_the_paths_base_level() -> None:
    """An idio path's base is arbitrary; a level-sensitive feature in CHART_FEATURES
    would make the idio arm a function of each symbol's listing date.
    """
    values = [100.0 * (1.0 + 0.01 * ((i * 7) % 11 - 5)) for i in range(60)]
    base_one = chart_features(_path(values)).sort("ts_ms")
    rescaled = chart_features(_path([v * 37.5 for v in values])).sort("ts_ms")

    for name in CHART_FEATURES:
        lhs = base_one[name].to_list()
        rhs = rescaled[name].to_list()
        for a, b in zip(lhs, rhs):
            if a is None or b is None:
                assert a is None and b is None, name
            else:
                assert a == pytest.approx(b, rel=1e-9, abs=1e-12), name


def test_prefix_namespaces_every_output_column() -> None:
    out = chart_features(_ramp(40), prefix="idio_")
    assert out.columns == ["symbol", "ts_ms", *[f"idio_{n}" for n in CHART_FEATURES]]


# ---------------------------------------------------------------------------
# gap safety
# ---------------------------------------------------------------------------


def test_momentum_is_null_across_a_gap_rather_than_measuring_a_longer_lookback() -> None:
    present = _path([100.0 + i for i in range(20)], first_day=BASE_DAY)
    after_gap = _path([200.0 + i for i in range(20)], first_day=BASE_DAY + 40)
    out = chart_features(pl.concat([present, after_gap], how="vertical")).sort("ts_ms")

    # The first post-gap row has no partner exactly 7 calendar days back.
    first_after = out.filter(pl.col("ts_ms") == _ms(BASE_DAY + 40))
    assert first_after["ret_7d"].item() is None
    assert first_after["ret_30d"].item() is None
    # Seven days later the partner exists again and the value returns.
    assert out.filter(pl.col("ts_ms") == _ms(BASE_DAY + 47))["ret_7d"].item() is not None


def test_vol_of_vol_requires_real_observations_not_grid_rows() -> None:
    """``_abs_logret`` is null wherever the previous calendar day is absent, so without
    an explicit observation count a sparse path would satisfy ``min_samples`` on row
    count alone.
    """
    sparse = _path([100.0, 101.0, 99.0, 103.0], first_day=BASE_DAY).with_columns(
        pl.Series("ts_ms", [_ms(BASE_DAY + d) for d in (0, 7, 14, 21)])
    )
    out = chart_features(sparse)
    assert out["vol_of_vol_30d"].drop_nulls().is_empty()


def test_a_flat_window_has_no_range_position_rather_than_a_fabricated_midpoint() -> None:
    out = chart_features(_path([100.0] * 40)).sort("ts_ms")
    assert out["range_pos_30d"].drop_nulls().is_empty()
    # The high is still well defined and the path is sitting on it.
    assert out.drop_nulls("dist_from_30d_high")["dist_from_30d_high"].to_list() == [
        pytest.approx(0.0)
    ] * out.drop_nulls("dist_from_30d_high").height


# ---------------------------------------------------------------------------
# cross-symbol isolation and validation
# ---------------------------------------------------------------------------


def test_symbols_do_not_contaminate_each_other() -> None:
    alone = chart_features(_ramp(40, symbol="AAA"))
    together = chart_features(
        pl.concat([_ramp(40, symbol="AAA"), _ramp(40, start=5.0, step=-0.1, symbol="BBB")], how="vertical")
    ).filter(pl.col("symbol") == "AAA")
    assert alone.sort("ts_ms").equals(together.sort("ts_ms"))


def test_non_positive_prices_are_dropped_not_divided_by() -> None:
    out = chart_features(_path([100.0, 0.0, -5.0, 103.0]))
    assert out.height == 2
    assert out["ts_ms"].to_list() == [_ms(BASE_DAY), _ms(BASE_DAY + 3)]


def test_missing_price_column_fails_loudly() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        chart_features(_ramp(10), price_col="idio_close")


def test_empty_input_returns_the_typed_empty_frame() -> None:
    out = chart_features(
        pl.DataFrame(schema={"symbol": pl.String, "ts_ms": pl.Int64, "close": pl.Float64})
    )
    assert out.is_empty()
    assert out.columns == ["symbol", "ts_ms", *CHART_FEATURES]
