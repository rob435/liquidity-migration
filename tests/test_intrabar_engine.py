"""1m intrabar exit resolver tests.

Synthetic 1m paths exercise every resolution branch. The resolver reuses the
deployed engine's price/exit helpers, so these also pin the side-convention
parity (short fade: TP below entry, stop above).
"""
from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.intrabar_engine import (
    MS_PER_MINUTE,
    resolve_dynamic_tp_1m,
    resolve_exit_1m,
)

ENTRY = 100.0


def _bars(rows: list[tuple[float, float, float]], *, entry_ts: int = 0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": [entry_ts + i * MS_PER_MINUTE for i in range(len(rows))],
            "high": [float(r[0]) for r in rows],
            "low": [float(r[1]) for r in rows],
            "close": [float(r[2]) for r in rows],
        }
    )


def _resolve(bars, *, tp=0.10, stop=0.0, planned=10 * MS_PER_MINUTE):
    return resolve_exit_1m(
        bars, entry_ts_ms=0, entry_price=ENTRY, side="short",
        take_profit_pct=tp, stop_loss_pct=stop, planned_exit_ts_ms=planned,
    )


def test_take_profit_first_touch() -> None:
    # short TP at 90 (=100*(1-0.10)); bar 2 low pierces it
    res = _resolve(_bars([(101, 99, 100), (102, 98, 101), (100, 89, 90)]))
    assert res.exit_reason == "take_profit"
    assert res.exit_price == pytest.approx(90.0)
    assert res.side_return == pytest.approx(0.10)
    assert res.first_touch_ts_ms == 2 * MS_PER_MINUTE
    assert res.exit_ts_ms == 2 * MS_PER_MINUTE + MS_PER_MINUTE
    assert res.ambiguous_same_bar is False


def test_stop_first_touch() -> None:
    # short stop at 105 (=100*(1+0.05)); bar 1 high pierces it before any TP
    res = _resolve(_bars([(101, 99, 100), (106, 100, 105), (100, 89, 90)]), stop=0.05)
    assert res.exit_reason == "stop_loss"
    assert res.exit_price == pytest.approx(105.0)
    assert res.side_return == pytest.approx(-0.05)
    assert res.first_touch_ts_ms == 1 * MS_PER_MINUTE


def test_same_bar_stop_and_tp_is_adverse_first() -> None:
    # bar 1 touches BOTH stop (high 106>=105) and TP (low 89<=90) -> adverse-first (stop)
    res = _resolve(_bars([(101, 99, 100), (106, 89, 95)]), stop=0.05)
    assert res.exit_reason == "stop_loss"
    assert res.ambiguous_same_bar is True
    assert res.side_return == pytest.approx(-0.05)


def test_max_hold_when_no_touch() -> None:
    res = _resolve(_bars([(101, 99, 100), (102, 98, 101), (101, 99, 100.5)]), stop=0.05, planned=3 * MS_PER_MINUTE)
    assert res.exit_reason == "max_hold"
    assert res.exit_price == pytest.approx(100.5)  # last window close
    assert res.exit_ts_ms == 3 * MS_PER_MINUTE
    assert res.first_touch_ts_ms is None


def test_control_no_stop_ignores_high_spikes() -> None:
    # stop=0 -> stop_price None: highs above 105 must NOT trigger a stop; TP still resolves
    res = _resolve(_bars([(106, 99, 100), (107, 98, 101), (100, 89, 90)]), stop=0.0)
    assert res.exit_reason == "take_profit"
    assert res.exit_price == pytest.approx(90.0)


def test_data_end_when_window_truncated() -> None:
    # only one bar, no touch, planned far in the future -> data_end (cache ends early)
    res = _resolve(_bars([(101, 99, 100)]), stop=0.05, planned=10 * MS_PER_MINUTE)
    assert res.exit_reason == "data_end"
    assert res.exit_ts_ms == 1 * MS_PER_MINUTE


def test_null_minutes_are_skipped_and_counted() -> None:
    # a no-trade densified minute (null high/low) is skipped for touch and counted
    bars = pl.DataFrame(
        {
            "ts_ms": [0, MS_PER_MINUTE, 2 * MS_PER_MINUTE],
            "high": [101.0, None, 100.0],
            "low": [99.0, None, 89.0],
            "close": [100.0, None, 90.0],
        }
    )
    res = _resolve(bars)
    assert res.exit_reason == "take_profit"
    assert res.null_bars == 1
    assert res.n_bars == 2


def test_delayed_arm_ignores_early_stop() -> None:
    # stop at 105; bars 1 & 2 both spike to 106. No arming -> stops at bar 1;
    # armed after 2 min -> bar 1 ignored, bar 2 stops (A2 delayed-arm).
    bars = _bars([(101, 99, 100), (106, 100, 105), (106, 100, 105), (100, 89, 90)])
    r0 = resolve_exit_1m(
        bars, entry_ts_ms=0, entry_price=ENTRY, side="short", take_profit_pct=0.10,
        stop_loss_pct=0.05, planned_exit_ts_ms=10 * MS_PER_MINUTE, stop_arm_after_ms=0,
    )
    assert r0.exit_reason == "stop_loss"
    assert r0.first_touch_ts_ms == 1 * MS_PER_MINUTE
    r2 = resolve_exit_1m(
        bars, entry_ts_ms=0, entry_price=ENTRY, side="short", take_profit_pct=0.10,
        stop_loss_pct=0.05, planned_exit_ts_ms=10 * MS_PER_MINUTE, stop_arm_after_ms=2 * MS_PER_MINUTE,
    )
    assert r2.exit_reason == "stop_loss"
    assert r2.first_touch_ts_ms == 2 * MS_PER_MINUTE


def _dyn(bars, *, base=0.15, arm=0.12, give=0.02, planned=10 * MS_PER_MINUTE):
    return resolve_dynamic_tp_1m(
        bars, entry_ts_ms=0, entry_price=ENTRY, side="short", base_tp_pct=base,
        trail_arm_pct=arm, trail_give_pct=give, planned_exit_ts_ms=planned,
    )


def test_dynamic_tp_ceiling_hit() -> None:
    # short base TP at 85 (=100*(1-0.15)); bar 2 low pierces it -> take_profit ceiling
    res = _dyn(_bars([(101, 99, 100), (100, 95, 96), (96, 84, 85)]))
    assert res.exit_reason == "take_profit"
    assert res.exit_price == pytest.approx(85.0)
    assert res.side_return == pytest.approx(0.15)


def test_dynamic_tp_trailing_exit_after_arm() -> None:
    # favorable reaches 13% (low 87 -> fav 0.13 >= arm 0.12) then closes back to 90
    # (close_fav 0.10 <= mfe 0.13 - give 0.02 = 0.11) -> trail_tp at that close.
    res = _dyn(_bars([(101, 99, 100), (95, 87, 88), (92, 89, 90)]))
    assert res.exit_reason == "trail_tp"
    assert res.exit_price == pytest.approx(90.0)
    assert res.side_return == pytest.approx(0.10)


def test_dynamic_tp_trail_disabled_is_flat_tp() -> None:
    # arm<=0 disables the trail -> only the base TP ceiling can fire; otherwise max_hold
    res = _dyn(_bars([(101, 99, 100), (95, 88, 90), (94, 90, 92)]), arm=0.0, planned=3 * MS_PER_MINUTE)
    assert res.exit_reason == "max_hold"
    assert res.exit_price == pytest.approx(92.0)
