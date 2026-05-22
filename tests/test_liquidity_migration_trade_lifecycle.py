"""Correctness tests for the trade lifecycle module.

The lifecycle helpers convert raw bars and trades into exit decisions,
basket aggregates, and equity curves. Each of those steps is a candidate
for silent accounting bugs, so the tests below pin the behaviour with
hand-built polars fixtures whose right answer is known by inspection.
"""
from __future__ import annotations

import math

import polars as pl
import pytest

from liquidity_migration.config import TradeLifecycleConfig
from liquidity_migration.trade_lifecycle import (
    _bar_excursion,
    _bar_exit_hits,
    _funding_lookup,
    _max_underwater_days,
    _perp_funding_return,
    _position_weight_stats,
    _rank_exit_hit,
    _side_return,
    _stop_price,
    _take_profit_price,
    _worst_rolling_equity_return,
    _worst_volume_day_return,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 24 * MS_PER_HOUR


# --------------------------------------------------------------------------
# Stop / take-profit price construction
# --------------------------------------------------------------------------

def test_stop_and_take_profit_prices_are_side_aware():
    # Long: stop below entry, target above. Short: mirrored.
    assert _stop_price(100.0, side="long", stop_loss_pct=0.08) == pytest.approx(92.0)
    assert _stop_price(100.0, side="short", stop_loss_pct=0.08) == pytest.approx(108.0)
    assert _take_profit_price(100.0, side="long", take_profit_pct=0.10) == pytest.approx(110.0)
    assert _take_profit_price(100.0, side="short", take_profit_pct=0.10) == pytest.approx(90.0)


def test_stop_and_take_profit_disabled_when_pct_non_positive():
    # A zero/negative percentage means the protective order is not placed.
    assert _stop_price(100.0, side="long", stop_loss_pct=0.0) is None
    assert _stop_price(100.0, side="short", stop_loss_pct=-0.01) is None
    assert _take_profit_price(100.0, side="long", take_profit_pct=0.0) is None
    assert _take_profit_price(100.0, side="short", take_profit_pct=0.0) is None


# --------------------------------------------------------------------------
# Intrabar exit detection
# --------------------------------------------------------------------------

def test_bar_exit_hits_long_stop_only():
    # Long stop at 92: low pierces it, high never reaches a 110 target.
    stop_hit, tp_hit = _bar_exit_hits(
        side="long", high=101.0, low=91.0, stop_price=92.0, take_profit_price=110.0
    )
    assert stop_hit is True
    assert tp_hit is False


def test_bar_exit_hits_long_take_profit_only():
    stop_hit, tp_hit = _bar_exit_hits(
        side="long", high=111.0, low=99.0, stop_price=92.0, take_profit_price=110.0
    )
    assert stop_hit is False
    assert tp_hit is True


def test_bar_exit_hits_short_side_uses_opposite_extremes():
    # Short stop at 108 triggers on the high; short target at 90 triggers on the low.
    stop_hit, tp_hit = _bar_exit_hits(
        side="short", high=109.0, low=89.0, stop_price=108.0, take_profit_price=90.0
    )
    assert stop_hit is True
    assert tp_hit is True


def test_bar_exit_hits_no_exit_when_bar_stays_inside_band():
    stop_hit, tp_hit = _bar_exit_hits(
        side="long", high=105.0, low=95.0, stop_price=92.0, take_profit_price=110.0
    )
    assert stop_hit is False
    assert tp_hit is False


def test_bar_exit_hits_stop_and_target_both_touched_in_one_bar():
    # Edge case: a single wide bar pierces BOTH the stop and the target.
    # The helper reports both as hit; conservative resolution is the caller's job.
    stop_hit, tp_hit = _bar_exit_hits(
        side="long", high=115.0, low=90.0, stop_price=92.0, take_profit_price=110.0
    )
    assert stop_hit is True
    assert tp_hit is True


def test_bar_exit_hits_ignores_missing_protective_levels():
    # When a level is None the corresponding side can never be flagged.
    stop_hit, tp_hit = _bar_exit_hits(
        side="long", high=200.0, low=1.0, stop_price=None, take_profit_price=None
    )
    assert stop_hit is False
    assert tp_hit is False


# --------------------------------------------------------------------------
# Excursion + per-side return accounting
# --------------------------------------------------------------------------

def test_bar_excursion_sign_convention_long():
    # Long entry at 100, bar [95, 108]: adverse is the loss leg, favorable the gain leg.
    adverse, favorable = _bar_excursion(100.0, side="long", high=108.0, low=95.0)
    assert adverse == pytest.approx(-0.05)
    assert favorable == pytest.approx(0.08)


def test_bar_excursion_sign_convention_short():
    # Short entry at 100, bar [95, 108]: a higher print is adverse for a short.
    adverse, favorable = _bar_excursion(100.0, side="short", high=108.0, low=95.0)
    assert adverse == pytest.approx(-0.08)
    assert favorable == pytest.approx(0.05)


def test_side_return_flips_sign_for_shorts():
    # Price rose 5%: a long gains it, a short loses it.
    assert _side_return(100.0, 105.0, side="long") == pytest.approx(0.05)
    assert _side_return(100.0, 105.0, side="short") == pytest.approx(-0.05)
    # Price fell 5%: mirrored outcome.
    assert _side_return(100.0, 95.0, side="long") == pytest.approx(-0.05)
    assert _side_return(100.0, 95.0, side="short") == pytest.approx(0.05)


# --------------------------------------------------------------------------
# Rank exit logic
# --------------------------------------------------------------------------

def test_rank_exit_disabled_returns_false():
    # With the feature off, no rank value can ever trigger an exit.
    assert (
        _rank_exit_hit(
            symbol="AAA",
            side="long",
            side_mode="long_high_short_low",
            bar_end_ts_ms=10,
            rank_lookup={("AAA", 10): 0.99},
            enabled=False,
            threshold=0.5,
        )
        is False
    )


def test_rank_exit_missing_lookup_entry_returns_false():
    # No rank snapshot for this (symbol, bar) -> cannot decide -> stay in.
    assert (
        _rank_exit_hit(
            symbol="AAA",
            side="long",
            side_mode="long_high_short_low",
            bar_end_ts_ms=10,
            rank_lookup={},
            enabled=True,
            threshold=0.5,
        )
        is False
    )


def test_rank_exit_long_high_short_low_mode():
    # long_high_short_low: a long is held for HIGH ranks, so it exits once the
    # rank fraction drops below the threshold.
    lookup = {("AAA", 10): 0.30, ("BBB", 10): 0.80}
    assert _rank_exit_hit(
        symbol="AAA", side="long", side_mode="long_high_short_low",
        bar_end_ts_ms=10, rank_lookup=lookup, enabled=True, threshold=0.5,
    )
    assert not _rank_exit_hit(
        symbol="BBB", side="long", side_mode="long_high_short_low",
        bar_end_ts_ms=10, rank_lookup=lookup, enabled=True, threshold=0.5,
    )
    # A short in the same mode is held for LOW ranks; it exits above 1 - threshold.
    assert _rank_exit_hit(
        symbol="BBB", side="short", side_mode="long_high_short_low",
        bar_end_ts_ms=10, rank_lookup=lookup, enabled=True, threshold=0.5,
    )
    assert not _rank_exit_hit(
        symbol="AAA", side="short", side_mode="long_high_short_low",
        bar_end_ts_ms=10, rank_lookup=lookup, enabled=True, threshold=0.5,
    )


def test_rank_exit_inverted_mode_flips_the_comparison():
    # The non-default mode reverses which rank extreme each side is held for.
    lookup = {("AAA", 10): 0.80}
    assert _rank_exit_hit(
        symbol="AAA", side="long", side_mode="long_low_short_high",
        bar_end_ts_ms=10, rank_lookup=lookup, enabled=True, threshold=0.5,
    )
    assert not _rank_exit_hit(
        symbol="AAA", side="short", side_mode="long_low_short_high",
        bar_end_ts_ms=10, rank_lookup=lookup, enabled=True, threshold=0.5,
    )


# --------------------------------------------------------------------------
# Perp funding accounting
# --------------------------------------------------------------------------

def test_funding_lookup_indexes_events_per_symbol():
    funding = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA", "BBB"],
            "ts_ms": [100, 200, 150],
            "funding_rate": [0.001, 0.002, -0.003],
        }
    )
    lookup = _funding_lookup(funding)
    assert lookup is not None
    assert set(lookup) == {"AAA", "BBB"}
    assert lookup["AAA"]["events_ts"] == [100, 200]
    assert lookup["AAA"]["events_rate"] == [0.001, 0.002]
    assert lookup["AAA"]["start_ts_ms"] == 100
    assert lookup["AAA"]["end_ts_ms"] == 200


def test_funding_lookup_returns_none_without_rate_column():
    # No usable rate column means funding cannot be modeled at all.
    funding = pl.DataFrame({"symbol": ["AAA"], "ts_ms": [100]})
    assert _funding_lookup(funding) is None
    assert _funding_lookup(None) is None
    assert _funding_lookup(pl.DataFrame()) is None


def test_perp_funding_return_signs_by_side_and_counts_events():
    funding = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA"],
            "ts_ms": [100, 200, 300],
            "funding_rate": [0.001, 0.002, 0.004],
        }
    )
    lookup = _funding_lookup(funding)
    # Window (100, 300] captures the 200 and 300 events: signed sum = 0.006.
    long_ret, mode, count = _perp_funding_return(
        lookup, symbol="AAA", side="long", entry_ts_ms=100, exit_ts_ms=300
    )
    assert mode == "modeled"
    assert count == 2
    # A long pays positive funding, so its funding return is negative.
    assert long_ret == pytest.approx(-0.006)
    short_ret, _, _ = _perp_funding_return(
        lookup, symbol="AAA", side="short", entry_ts_ms=100, exit_ts_ms=300
    )
    assert short_ret == pytest.approx(0.006)


def test_perp_funding_return_partial_when_window_exceeds_coverage():
    funding = pl.DataFrame(
        {"symbol": ["AAA"], "ts_ms": [200], "funding_rate": [0.001]}
    )
    lookup = _funding_lookup(funding)
    # Exit past the last known funding stamp -> coverage is incomplete, so the
    # trade is flagged "partial" -- but the funding that IS covered (the stamp
    # at ts=200) is still charged rather than the whole trade being zeroed.
    ret, mode, count = _perp_funding_return(
        lookup, symbol="AAA", side="long", entry_ts_ms=100, exit_ts_ms=999
    )
    assert (ret, mode, count) == (pytest.approx(-0.001), "partial", 1)
    # An unknown symbol is reported as missing rather than partial.
    miss = _perp_funding_return(
        lookup, symbol="ZZZ", side="long", entry_ts_ms=100, exit_ts_ms=300
    )
    assert miss == (0.0, "missing", 0)


def test_perp_funding_return_modeled_with_zero_events_in_window():
    funding = pl.DataFrame(
        {"symbol": ["AAA"], "ts_ms": [100], "funding_rate": [0.001]}
    )
    lookup = _funding_lookup(funding)
    # Window fully covered but no funding stamp strictly inside it.
    ret, mode, count = _perp_funding_return(
        lookup, symbol="AAA", side="long", entry_ts_ms=100, exit_ts_ms=100
    )
    assert (ret, mode, count) == (0.0, "modeled", 0)


def test_funding_lookup_collapses_intra_interval_snapshot_rows():
    # Some symbols carry hourly snapshot rows of an 8h funding rate
    # (funding_interval_min=480). Each settlement must be charged once, not once
    # per snapshot row, or a multi-day hold is over-charged up to ~8x.
    hour = 3_600_000
    funding = pl.DataFrame(
        {
            "symbol": ["AAA"] * 24,
            "ts_ms": [h * hour for h in range(24)],
            "funding_rate": [0.001] * 24,
            "funding_interval_min": [480] * 24,
        }
    )
    lookup = _funding_lookup(funding)
    # 24 hourly rows span three 8h settlements -> three events, not 24.
    assert lookup["AAA"]["events_ts"] == [0, 8 * hour, 16 * hour]
    # Coverage span stays the raw first/last stamp.
    assert lookup["AAA"]["start_ts_ms"] == 0
    assert lookup["AAA"]["end_ts_ms"] == 23 * hour
    # A hold spanning two settlements is charged twice, not ~16x.
    _, mode, count = _perp_funding_return(
        lookup, symbol="AAA", side="short", entry_ts_ms=1, exit_ts_ms=23 * hour
    )
    assert (mode, count) == ("modeled", 2)


# --------------------------------------------------------------------------
# Basket summaries
# --------------------------------------------------------------------------

def _trade_row(**overrides):
    base = {
        "basket_id": "B1",
        "side": "long",
        "entry_signal_ts_ms": 0,
        "entry_ts_ms": MS_PER_HOUR,
        "exit_ts_ms": MS_PER_DAY,
        "net_return": 0.0,
        "gross_return": 0.0,
        "cost_return": 0.0,
        "funding_return": 0.0,
    }
    base.update(overrides)
    return base


def test_summarize_baskets_aggregates_returns_and_counts():
    config = TradeLifecycleConfig(score="dollar_volume_rank", quantile=0.5, hold_days=7, rebalance_days=7)
    trades = pl.DataFrame(
        [
            _trade_row(side="long", net_return=0.04, gross_return=0.05, cost_return=-0.01, funding_return=0.0),
            _trade_row(side="short", net_return=-0.02, gross_return=-0.015, cost_return=-0.005, funding_return=0.0),
            _trade_row(
                basket_id="B2",
                side="long",
                net_return=0.01,
                gross_return=0.012,
                cost_return=-0.002,
                entry_ts_ms=2 * MS_PER_DAY,
                exit_ts_ms=3 * MS_PER_DAY,
            ),
        ]
    )
    baskets = summarize_baskets(trades, config=config)
    by_id = {row["basket_id"]: row for row in baskets.to_dicts()}
    # B1 sums one long and one short trade.
    assert by_id["B1"]["basket_return"] == pytest.approx(0.02)
    assert by_id["B1"]["long_return"] == pytest.approx(0.04)
    assert by_id["B1"]["short_return"] == pytest.approx(-0.02)
    assert by_id["B1"]["trades"] == 2
    assert by_id["B1"]["winning_trades"] == 1
    assert by_id["B1"]["cost_return"] == pytest.approx(-0.015)
    # Config values are stamped onto every basket row.
    assert by_id["B2"]["basket_return"] == pytest.approx(0.01)
    assert by_id["B2"]["score"] == "dollar_volume_rank"
    assert by_id["B2"]["hold_days"] == 7
    # Output is ordered by entry timestamp.
    assert baskets["entry_ts_ms"].to_list() == sorted(baskets["entry_ts_ms"].to_list())


def test_summarize_baskets_empty_input_returns_typed_empty_frame():
    config = TradeLifecycleConfig()
    baskets = summarize_baskets(pl.DataFrame(), config=config)
    assert baskets.is_empty()
    # The empty contract still exposes the downstream columns and dtypes.
    assert "basket_return" in baskets.columns
    assert baskets.schema["basket_id"] == pl.String
    assert baskets.schema["basket_return"] == pl.Float64


# --------------------------------------------------------------------------
# Equity curve construction
# --------------------------------------------------------------------------

def test_build_equity_curve_compounds_basket_returns_and_tracks_drawdown():
    baskets = pl.DataFrame(
        {
            "basket_id": ["B1", "B2", "B3"],
            "exit_ts_ms": [2 * MS_PER_DAY, MS_PER_DAY, 3 * MS_PER_DAY],
            "basket_return": [0.10, 0.20, -0.50],
        }
    )
    curve = build_equity_curve(baskets)
    # Rows are processed in exit-time order: +20%, +10%, -50%.
    assert curve["ts_ms"].to_list() == [MS_PER_DAY, 2 * MS_PER_DAY, 3 * MS_PER_DAY]
    equities = curve["equity"].to_list()
    assert equities[0] == pytest.approx(1.20)
    assert equities[1] == pytest.approx(1.20 * 1.10)
    assert equities[2] == pytest.approx(1.20 * 1.10 * 0.50)
    drawdowns = curve["drawdown"].to_list()
    # Peak equity is 1.32; the final point sits at 0.66 -> 50% drawdown.
    assert drawdowns[0] == pytest.approx(0.0)
    assert drawdowns[1] == pytest.approx(0.0)
    assert drawdowns[2] == pytest.approx(0.66 / 1.32 - 1.0)
    # A human-readable date column is derived from the exit timestamp.
    assert "date" in curve.columns


def test_build_equity_curve_empty_input_returns_typed_empty_frame():
    curve = build_equity_curve(pl.DataFrame())
    assert curve.is_empty()
    assert curve.schema["ts_ms"] == pl.Int64
    assert curve.schema["equity"] == pl.Float64
    assert curve.schema["drawdown"] == pl.Float64


# --------------------------------------------------------------------------
# Underwater / rolling drawdown helpers
# --------------------------------------------------------------------------

def test_max_underwater_days_counts_longest_stretch_below_peak():
    # Peak on day 0, then four days below it before a new high on day 5.
    equity = pl.DataFrame(
        {
            "ts_ms": [day * MS_PER_DAY for day in range(6)],
            "equity": [1.00, 0.95, 0.90, 0.92, 0.97, 1.05],
        }
    )
    assert _max_underwater_days(equity) == 4


def test_max_underwater_days_zero_when_monotonically_rising():
    equity = pl.DataFrame(
        {
            "ts_ms": [0, MS_PER_DAY, 2 * MS_PER_DAY],
            "equity": [1.0, 1.1, 1.2],
        }
    )
    assert _max_underwater_days(equity) == 0
    assert _max_underwater_days(pl.DataFrame()) == 0


def test_worst_rolling_equity_return_finds_largest_window_loss():
    # Daily equity over 5 days; the worst 2-day move is 1.10 -> 0.80.
    equity = pl.DataFrame(
        {
            "ts_ms": [day * MS_PER_DAY for day in range(5)],
            "equity": [1.00, 1.10, 1.00, 0.80, 0.90],
        }
    )
    worst = _worst_rolling_equity_return(equity, 2)
    assert worst == pytest.approx(0.80 / 1.10 - 1.0)
    # A window wider than the available history yields a neutral 0.0.
    assert _worst_rolling_equity_return(equity, 99) == 0.0


def test_worst_volume_day_return_compounds_within_each_exit_date():
    baskets = pl.DataFrame(
        {
            "exit_date": ["2024-01-01", "2024-01-01", "2024-01-02"],
            "basket_return": [0.10, -0.20, 0.05],
        }
    )
    # 2024-01-01 compounds two baskets: 1.10 * 0.80 - 1 = -0.12.
    worst = _worst_volume_day_return(baskets)
    assert worst == pytest.approx(1.10 * 0.80 - 1.0)
    assert _worst_volume_day_return(pl.DataFrame()) == 0.0


# --------------------------------------------------------------------------
# Backtest summary
# --------------------------------------------------------------------------

def test_summarize_trade_backtest_empty_inputs_return_zeroed_payload():
    config = TradeLifecycleConfig()
    summary = summarize_trade_backtest(
        pl.DataFrame(), pl.DataFrame(), pl.DataFrame(), config=config
    )
    assert summary["total_return"] == 0.0
    assert summary["trades"] == 0
    assert summary["baskets"] == 0
    assert summary["profit_factor"] == 0.0
    assert summary["funding_mode"] == "missing"


def test_summarize_trade_backtest_computes_returns_winrate_and_profit_factor():
    config = TradeLifecycleConfig(rebalance_days=7)
    trades = pl.DataFrame(
        [
            _trade_row(side="long", net_return=0.06, gross_return=0.07, cost_return=-0.01),
            _trade_row(side="short", net_return=-0.02, gross_return=-0.015, cost_return=-0.005),
            _trade_row(
                basket_id="B2",
                side="long",
                net_return=0.04,
                gross_return=0.05,
                cost_return=-0.01,
                exit_ts_ms=2 * MS_PER_DAY,
            ),
        ]
    )
    baskets = summarize_baskets(trades, config=config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=config)

    assert summary["trades"] == 3
    assert summary["baskets"] == 2
    # Two of three trades are winners.
    assert summary["trade_win_rate"] == pytest.approx(2.0 / 3.0)
    # Profit factor = gross wins / abs(gross losses) = 0.10 / 0.02.
    assert summary["profit_factor"] == pytest.approx((0.06 + 0.04) / 0.02)
    assert summary["long_return"] == pytest.approx(0.10)
    assert summary["short_return"] == pytest.approx(-0.02)
    # Total return matches the compounded equity curve.
    final_equity = (1.0 + 0.04) * (1.0 + 0.04)
    assert summary["total_return"] == pytest.approx(final_equity - 1.0)
    assert summary["max_drawdown"] <= 0.0
    assert math.isfinite(summary["sharpe_like"])


def test_summarize_trade_backtest_profit_factor_zero_without_losses():
    # No losing trades -> profit factor is reported as 0.0 by definition here.
    config = TradeLifecycleConfig(rebalance_days=7)
    trades = pl.DataFrame(
        [
            _trade_row(side="long", net_return=0.03, gross_return=0.04, cost_return=-0.01),
            _trade_row(
                basket_id="B2",
                side="long",
                net_return=0.02,
                gross_return=0.03,
                cost_return=-0.01,
                exit_ts_ms=2 * MS_PER_DAY,
            ),
        ]
    )
    baskets = summarize_baskets(trades, config=config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=config)
    assert summary["profit_factor"] == 0.0
    assert summary["trade_win_rate"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Position weight stats
# --------------------------------------------------------------------------

def test_position_weight_stats_defaults_when_column_absent() -> None:
    trades = pl.DataFrame({"net_return": [0.01, -0.02]})
    stats = _position_weight_stats(trades)
    assert stats["position_weight_mean"] == pytest.approx(1.0)
    assert stats["position_weight_std"] == pytest.approx(0.0)
    assert stats["position_weight_min"] == pytest.approx(1.0)
    assert stats["position_weight_max"] == pytest.approx(1.0)


def test_position_weight_stats_computes_distribution() -> None:
    trades = pl.DataFrame({"position_weight": [0.5, 1.0, 2.0, 1.5]})
    stats = _position_weight_stats(trades)
    assert stats["position_weight_mean"] == pytest.approx(1.25)
    assert stats["position_weight_min"] == pytest.approx(0.5)
    assert stats["position_weight_max"] == pytest.approx(2.0)
    assert stats["position_weight_std"] > 0.0


def test_summarize_trade_backtest_includes_position_weight_stats() -> None:
    config = TradeLifecycleConfig(rebalance_days=7)
    trades = pl.DataFrame(
        [
            {**_trade_row(net_return=0.04, gross_return=0.05, cost_return=-0.01), "position_weight": 0.5},
            {**_trade_row(basket_id="B2", net_return=0.02, gross_return=0.03, cost_return=-0.01,
                          exit_ts_ms=2 * MS_PER_DAY), "position_weight": 2.0},
        ]
    )
    baskets = summarize_baskets(trades, config=config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=config)
    assert summary["position_weight_mean"] == pytest.approx(1.25)
    assert summary["position_weight_min"] == pytest.approx(0.5)
    assert summary["position_weight_max"] == pytest.approx(2.0)


def test_summarize_trade_backtest_empty_returns_default_position_weight_stats() -> None:
    config = TradeLifecycleConfig()
    summary = summarize_trade_backtest(
        pl.DataFrame(), pl.DataFrame(), pl.DataFrame(), config=config
    )
    assert summary["position_weight_mean"] == pytest.approx(1.0)
    assert summary["position_weight_std"] == pytest.approx(0.0)
