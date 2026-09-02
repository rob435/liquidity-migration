from __future__ import annotations

import math
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.research.lab.panel import FROZEN_COLUMNS, build_daily_panel, load_panel

DAY = 86_400_000
H = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC


def _hourly(symbols: list[str], days: int, *, skip: set[tuple[str, int]] = frozenset()) -> pl.DataFrame:
    rows = []
    for si, sym in enumerate(symbols):
        for d in range(days):
            if (sym, d) in skip:
                continue
            base = 100.0 * (si + 1) + d
            for h in range(24):
                rows.append(
                    dict(
                        ts_ms=T0 + d * DAY + h * H, symbol=sym,
                        open=base + h * 0.1, high=base + h * 0.1 + 1.0, low=base + h * 0.1 - 1.0,
                        close=base + h * 0.1 + 0.5, turnover_quote=float(10 * (si + 1)),
                    )
                )
    return pl.DataFrame(rows)


def _write_inputs(tmp_path: Path) -> Path:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    _hourly(["AAA", "BBB", "CCC"], 5, skip={("CCC", 2)}).write_parquet(inputs / "klines_1h.parquet")
    # settlements every 8h from day 0 00:00 to day 5 00:00, symbol AAA only
    pl.DataFrame(
        dict(ts_ms=[T0 + k * 8 * H for k in range(16)], symbol=["AAA"] * 16,
             funding_rate=[1e-4 * (k + 1) for k in range(16)])
    ).write_parquet(inputs / "funding.parquet")
    pl.DataFrame(
        dict(ts_ms=[T0 + d * DAY + h * H for d in range(5) for h in (12, 20)], symbol=["AAA"] * 10,
             open_interest=[1.0] * 10, open_interest_value=[float(d * 1000 + i + 1) for d in range(5) for i in range(2)])
    ).write_parquet(inputs / "open_interest.parquet")
    pl.DataFrame(
        dict(ts_ms=[T0 + d * DAY + h * H for d in range(5) for h in range(24)], symbol=["AAA"] * 120,
             close=[0.001 * h for _ in range(5) for h in range(24)])
    ).write_parquet(inputs / "premium_index_1h.parquet")
    return inputs


def _row(panel: pl.DataFrame, symbol: str, day: int) -> dict:
    return panel.filter((pl.col("symbol") == symbol) & (pl.col("day") == T0 + day * DAY)).to_dicts()[0]


def test_columns_are_frozen_and_days_aggregate_the_hourly_bars(tmp_path: Path) -> None:
    panel = build_daily_panel(_write_inputs(tmp_path))
    assert panel.columns == list(FROZEN_COLUMNS)
    assert panel.height == 3 * 5 - 1
    r = _row(panel, "BBB", 3)
    base = 200.0 + 3
    assert r["open"] == pytest.approx(base)
    assert r["high"] == pytest.approx(base + 2.3 + 1.0)
    assert r["low"] == pytest.approx(base - 1.0)
    assert r["close"] == pytest.approx(base + 2.3 + 0.5)
    assert r["turnover"] == pytest.approx(24 * 20.0)
    assert r["n_bars"] == 24
    assert r["day"] % DAY == 0


def test_funding_at_midnight_belongs_to_the_day_that_ended(tmp_path: Path) -> None:
    panel = build_daily_panel(_write_inputs(tmp_path))
    for d in range(5):
        r = _row(panel, "AAA", d)
        # settlements k = 3d+1, 3d+2, 3d+3: the 00:00 print of day d+1 counts for day d
        assert r["n_settle"] == 3
        assert r["funding_day"] == pytest.approx(1e-4 * (9 * d + 9))
        assert r["funding_last"] == pytest.approx(1e-4 * (3 * d + 4))
    # the day 0 00:00 settlement (k = 0) belongs to a day with no bars and is dropped
    assert _row(panel, "AAA", 0)["funding_day"] == pytest.approx(1e-4 * (2 + 3 + 4))
    # no funding for the other names: zero, not null
    assert _row(panel, "BBB", 2)["funding_day"] == 0.0
    assert _row(panel, "BBB", 2)["n_settle"] == 0
    assert _row(panel, "BBB", 2)["funding_last"] is None


def test_open_interest_and_premium_take_the_day_last_and_mean(tmp_path: Path) -> None:
    panel = build_daily_panel(_write_inputs(tmp_path))
    r = _row(panel, "AAA", 2)
    assert r["oi_value"] == pytest.approx(2002.0)
    assert r["premium_mean"] == pytest.approx(0.001 * 11.5)
    assert r["premium_last"] == pytest.approx(0.023)
    assert _row(panel, "CCC", 1)["oi_value"] is None
    assert _row(panel, "CCC", 1)["premium_mean"] is None


def test_return_is_null_across_a_gap_and_age_counts_seen_days(tmp_path: Path) -> None:
    panel = build_daily_panel(_write_inputs(tmp_path))
    d0, d1, d3, d4 = (_row(panel, "CCC", d) for d in (0, 1, 3, 4))
    assert d0["gap"] is None and d0["ret"] is None and d0["lret"] is None
    assert d1["gap"] == DAY
    assert d1["ret"] == pytest.approx(d1["close"] / d0["close"] - 1.0)
    assert d1["lret"] == pytest.approx(math.log1p(d1["ret"]))
    assert d3["gap"] == 2 * DAY and d3["ret"] is None
    assert d4["gap"] == DAY and d4["ret"] == pytest.approx(d4["close"] / d3["close"] - 1.0)
    assert [_row(panel, "CCC", d)["age_days"] for d in (0, 1, 3, 4)] == [1, 2, 3, 4]
    assert [_row(panel, "AAA", d)["age_days"] for d in range(5)] == [1, 2, 3, 4, 5]
    # fewer than 30 days: no trailing turnover, hence no liquidity rank
    assert panel["adv_30"].null_count() == panel.height
    assert panel["adv_rank"].null_count() == panel.height


def test_trailing_windows_and_rank_on_a_long_series(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    rows = []
    for d in range(40):
        for sym, scale in (("AAA", 1.0), ("BBB", 3.0)):
            close = 100.0 + d * (1.0 if sym == "AAA" else -0.5)
            rows.append(dict(ts_ms=T0 + d * DAY, symbol=sym, open=close, high=close, low=close, close=close,
                             turnover_quote=scale * (d + 1)))
    pl.DataFrame(rows).write_parquet(inputs / "klines_1h.parquet")
    panel = build_daily_panel(inputs)
    a = panel.filter(pl.col("symbol") == "AAA").sort("day")
    assert a["n_bars"].to_list() == [1] * 40
    assert a["adv_30"][28] is None
    assert a["adv_30"][29] == pytest.approx(15.5)
    assert a["adv_90"].null_count() == 40
    assert a["rv_7"][4] is None and a["rv_7"][5] is not None
    assert a["rv_30"][19] is None and a["rv_30"][20] is not None
    assert a["rv_90"].null_count() == 40
    # BBB turns over three times AAA: rank 1 for BBB, 2 for AAA, once adv_30 exists
    assert a["adv_rank"][29] == 2.0
    assert panel.filter(pl.col("symbol") == "BBB").sort("day")["adv_rank"][29] == 1.0
    assert panel["funding_day"].to_list() == [0.0] * panel.height


def test_out_path_round_trips_through_load_panel(tmp_path: Path) -> None:
    inputs = _write_inputs(tmp_path)
    out = tmp_path / "panel" / "daily.parquet"
    panel = build_daily_panel(inputs, out)
    assert out.exists()
    assert load_panel(out).equals(panel)


def test_missing_klines_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "inputs").mkdir()
    with pytest.raises(FileNotFoundError):
        build_daily_panel(tmp_path / "inputs")
