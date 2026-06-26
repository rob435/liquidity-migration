"""1m intrabar path resolution for the continuous v2 research program.

Research overlay: it consumes the Wave-1 trade-window 1m cache
(``~/SHARED_DATA/continuous_v2_1m``) to re-resolve a trade's exit on the ACTUAL
1m path instead of the 1h OHLC adverse-first approximation. It does NOT touch the
deployed forward-replay engine (``trade_lifecycle._simulate_indexed_trade``); it
REUSES that engine's exact price/exit helpers so the only difference is path
granularity (the X1 requirement: "1m changes only path-dependent trades").

Resolution rule (mirrors and refines the 1h engine):
- HIGH/LOW-based exits (stop, take-profit) resolve by 1m first-touch.
- If stop AND take-profit are both touched within the SAME 1m bar, the bar is
  genuinely ambiguous (1m OHLC still hides sub-minute order) -> adverse-first
  (stop wins), flagged in ``ambiguous_same_bar``.
- No touch within the hold window -> max_hold at the window's last 1m close
  (==the 1h max-hold close), or ``data_end`` if the 1m cache ends early.

Close-based soft exits (mfe_giveback, breakeven, failed_fade, event/rank/hash)
stay on the registered hourly cadence in the deployed engine and are NOT
re-resolved here; this overlay is for the path-dependent stop/TP layer that the
1h bar could not measure (Books A, E; entry/exit fill timing for X2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from .trade_lifecycle import (
    _bar_excursion,
    _bar_exit_hits,
    _side_return,
    _stop_fill_price,
    _stop_price,
    _take_profit_price,
)

CACHE_ROOT = Path.home() / "SHARED_DATA" / "continuous_v2_1m"
MS_PER_MINUTE = 60_000
_WIN_COLS = ("ts_ms", "high", "low", "close")


@dataclass(frozen=True)
class ExitResolution:
    """The 1m-path-resolved exit for one trade under a stop/TP/max-hold policy."""

    exit_reason: str  # take_profit | stop_loss | max_hold | data_end
    exit_ts_ms: int
    exit_price: float
    side_return: float  # gross trade return at the resolved exit (side-adjusted, no cost)
    mae: float  # worst adverse excursion on the 1m path (<= 0)
    mfe: float  # best favorable excursion on the 1m path (>= 0)
    n_bars: int  # 1m bars walked (non-null) before the exit
    null_bars: int  # no-trade/densified-null minutes skipped in the window
    ambiguous_same_bar: bool  # stop AND TP both touched in the resolving 1m bar
    first_touch_ts_ms: int | None  # bar-open ts of the resolving touch (None for max_hold/data_end)
    resolution: str = "1m"


def load_1m_window(
    venue: str,
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> pl.DataFrame:
    """Load the trade-window 1m bars (ts_ms/high/low/close) for [start_date, end_date]."""
    d0 = date.fromisoformat(start_date[:10])
    d1 = date.fromisoformat(end_date[:10])
    frames: list[pl.DataFrame] = []
    dd = d0
    while dd <= d1:
        p = cache_root / venue / "klines_1m" / f"date={dd.isoformat()}" / f"symbol={symbol}" / "data.parquet"
        if p.exists():
            frames.append(pl.read_parquet(p, columns=list(_WIN_COLS)))
        dd += timedelta(days=1)
    if not frames:
        return pl.DataFrame(schema={"ts_ms": pl.Int64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64})
    return pl.concat(frames).unique("ts_ms", keep="first").sort("ts_ms")


def resolve_exit_1m(
    bars: pl.DataFrame,
    *,
    entry_ts_ms: int,
    entry_price: float,
    side: str,
    take_profit_pct: float,
    stop_loss_pct: float,
    planned_exit_ts_ms: int,
    stop_fill_mode: str = "stop",
    stop_slippage_cap_pct: float = 0.10,
    stop_arm_after_ms: int = 0,
) -> ExitResolution:
    """Resolve a trade's exit on the 1m path under (take_profit_pct, stop_loss_pct, max_hold).

    ``bars`` must hold ts_ms (1m bar-open), high, low, close for at least the
    [entry_ts_ms, planned_exit_ts_ms) window (see ``load_1m_window``). Window
    semantics match the 1h engine: bars with entry_ts_ms <= ts_ms < planned_exit_ts_ms
    (each 1m bar-open ts covers [ts, ts+60s)); exit ts is the resolving bar's end.

    ``stop_arm_after_ms`` (default 0 -> byte-identical) delays stop arming: stop
    touches before ``entry_ts_ms + stop_arm_after_ms`` are ignored (Book A's A2
    delayed-arm stop, which avoids cutting the initial mean-reversion window). TP
    and max_hold are unaffected.
    """
    tp_price = _take_profit_price(entry_price, side=side, take_profit_pct=take_profit_pct)
    stop_price = _stop_price(entry_price, side=side, stop_loss_pct=stop_loss_pct or 0.0)
    win = bars.filter((pl.col("ts_ms") >= entry_ts_ms) & (pl.col("ts_ms") < planned_exit_ts_ms))
    n = 0
    null_bars = 0
    mae = 0.0
    mfe = 0.0
    if win.height == 0:
        return ExitResolution("data_end", entry_ts_ms, entry_price, 0.0, 0.0, 0, 0, False, None)
    ts_a = win["ts_ms"].to_list()
    hi = win["high"].to_list()
    lo = win["low"].to_list()
    cl = win["close"].to_list()
    last_close = entry_price
    last_ts = entry_ts_ms
    for i in range(len(ts_a)):
        bar_high = hi[i]
        bar_low = lo[i]
        if bar_high is None or bar_low is None:
            null_bars += 1
            continue
        n += 1
        last_ts = int(ts_a[i])
        if cl[i] is not None:
            last_close = float(cl[i])
        adverse, favorable = _bar_excursion(entry_price, side=side, high=bar_high, low=bar_low)
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        armed_stop = stop_price if int(ts_a[i]) >= entry_ts_ms + stop_arm_after_ms else None
        stop_hit, tp_hit = _bar_exit_hits(
            side=side, high=bar_high, low=bar_low, stop_price=armed_stop, take_profit_price=tp_price
        )
        if stop_hit and tp_hit:
            # genuinely ambiguous at 1m -> adverse-first (stop)
            fill = _stop_fill_price(
                side=side, stop_price=stop_price, high=bar_high, low=bar_low,
                mode=stop_fill_mode, cap_pct=stop_slippage_cap_pct,
            )
            return ExitResolution(
                "stop_loss", int(ts_a[i]) + MS_PER_MINUTE, fill,
                _side_return(entry_price, fill, side=side), mae, mfe, n, null_bars, True, int(ts_a[i]),
            )
        if stop_hit:
            fill = _stop_fill_price(
                side=side, stop_price=stop_price, high=bar_high, low=bar_low,
                mode=stop_fill_mode, cap_pct=stop_slippage_cap_pct,
            )
            return ExitResolution(
                "stop_loss", int(ts_a[i]) + MS_PER_MINUTE, fill,
                _side_return(entry_price, fill, side=side), mae, mfe, n, null_bars, False, int(ts_a[i]),
            )
        if tp_hit:
            return ExitResolution(
                "take_profit", int(ts_a[i]) + MS_PER_MINUTE, float(tp_price),
                _side_return(entry_price, float(tp_price), side=side), mae, mfe, n, null_bars, False, int(ts_a[i]),
            )
    # no touch -> max_hold at the last window close (== the 1h max-hold close)
    exit_ts = last_ts + MS_PER_MINUTE
    reason = "max_hold" if exit_ts >= planned_exit_ts_ms else "data_end"
    return ExitResolution(
        reason, exit_ts, last_close, _side_return(entry_price, last_close, side=side),
        mae, mfe, n, null_bars, False, None,
    )


def resolve_dynamic_tp_1m(
    bars: pl.DataFrame,
    *,
    entry_ts_ms: int,
    entry_price: float,
    side: str,
    base_tp_pct: float,
    trail_arm_pct: float,
    trail_give_pct: float,
    planned_exit_ts_ms: int,
) -> ExitResolution:
    """Book E MFE-extension / trailing TP on the 1m path (separate from the validated
    ``resolve_exit_1m`` core so the control-reproduction guarantee is untouched).

    A hard take-profit at ``base_tp_pct`` (the ceiling) PLUS a path-armed trailing
    exit: once the favorable excursion reaches ``trail_arm_pct``, exit when the bar
    CLOSE gives back ``trail_give_pct`` from the best favorable excursion (close-based
    giveback, matching the deployed engine's ``mfe_giveback`` convention). This lets a
    strongly-reverting fade run past the control TP while protecting the armed gain —
    the path-dependent winner-management the 1h bar could not measure. ``trail_arm_pct
    <= 0`` disables the trail (pure ``base_tp_pct`` flat TP). max_hold at the last 1m
    close; data_end if the cache ends early.
    """
    tp_price = _take_profit_price(entry_price, side=side, take_profit_pct=base_tp_pct)
    win = bars.filter((pl.col("ts_ms") >= entry_ts_ms) & (pl.col("ts_ms") < planned_exit_ts_ms))
    n = 0
    null_bars = 0
    mae = 0.0
    mfe = 0.0
    armed = False
    if win.height == 0:
        return ExitResolution("data_end", entry_ts_ms, entry_price, 0.0, 0.0, 0, 0, False, None)
    ts_a = win["ts_ms"].to_list()
    hi = win["high"].to_list()
    lo = win["low"].to_list()
    cl = win["close"].to_list()
    last_close = entry_price
    last_ts = entry_ts_ms
    for i in range(len(ts_a)):
        bar_high = hi[i]
        bar_low = lo[i]
        if bar_high is None or bar_low is None:
            null_bars += 1
            continue
        n += 1
        last_ts = int(ts_a[i])
        if cl[i] is not None:
            last_close = float(cl[i])
        adverse, favorable = _bar_excursion(entry_price, side=side, high=bar_high, low=bar_low)
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        _stop, tp_hit = _bar_exit_hits(side=side, high=bar_high, low=bar_low, stop_price=None, take_profit_price=tp_price)
        if tp_hit:  # ceiling reached -> best outcome, take it first
            return ExitResolution(
                "take_profit", int(ts_a[i]) + MS_PER_MINUTE, float(tp_price),
                _side_return(entry_price, float(tp_price), side=side), mae, mfe, n, null_bars, False, int(ts_a[i]),
            )
        if trail_arm_pct > 0.0 and mfe >= trail_arm_pct:
            armed = True
        close_fav = _side_return(entry_price, last_close, side=side)
        if armed and close_fav <= mfe - trail_give_pct:
            return ExitResolution(
                "trail_tp", int(ts_a[i]) + MS_PER_MINUTE, last_close,
                close_fav, mae, mfe, n, null_bars, False, int(ts_a[i]),
            )
    exit_ts = last_ts + MS_PER_MINUTE
    reason = "max_hold" if exit_ts >= planned_exit_ts_ms else "data_end"
    return ExitResolution(
        reason, exit_ts, last_close, _side_return(entry_price, last_close, side=side),
        mae, mfe, n, null_bars, False, None,
    )
