from __future__ import annotations

import math

import numpy as np
import polars as pl
import pytest

from liquidity_migration.research.lab.overlay import (
    Pricing,
    StopGeometry,
    alive_daily_stamps,
    alive_exit_candidates,
    bars_by_symbol,
    check_reproduction,
    evaluate_overlay,
    hard_clock,
    settlement_funding,
    simulate_exit,
    state_exit_rule,
)

H = 3_600_000
DAY = 24 * H
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
N_BARS = 24 * 10
GEOMETRY = StopGeometry()
PRICING = Pricing(round_trip_cost_bps=45.0)


def _prices() -> pl.DataFrame:
    rows = []
    for i in range(N_BARS):
        a = 100.0 + 0.01 * i
        b = 50.0 + 0.005 * i
        c = 100.0 + 0.01 * i
        rows.append(dict(ts_ms=T0 + i * H, symbol="AAA", close=a, low=a - 0.05))
        rows.append(dict(ts_ms=T0 + i * H, symbol="BBB", close=b, low=40.0 if i == 30 else b - 0.02))
        # CCC dips after the stop has decayed: bar 74 (ends T0 + 75h) prints a low 2.1 under its close
        rows.append(dict(ts_ms=T0 + i * H, symbol="CCC", close=c, low=c - 2.1 if i == 74 else c - 0.05))
    return pl.DataFrame(rows)


def _ledger(bars, entries: list[tuple[str, int]], *, stop_pct: float = 0.03, weight: float = 0.1) -> pl.DataFrame:
    rows = []
    for symbol, entry_ts in entries:
        entry_px = bars[symbol].close_at(entry_ts)
        assert entry_px is not None
        trade = dict(symbol=symbol, entry_ts_ms=entry_ts, entry_price=entry_px, stop_price=entry_px * (1 - stop_pct),
                     planned_exit_ts_ms=entry_ts + 72 * H, notional_weight=weight)
        ets, epx, reason = simulate_exit(trade, bars[symbol], trade["planned_exit_ts_ms"], GEOMETRY)
        trade.update(exit_ts_ms=ets, exit_price=epx, exit_reason=reason)
        trade["net_return"] = PRICING.net(trade, ets, epx)
        rows.append(trade)
    return pl.DataFrame(rows)


@pytest.fixture()
def bars():
    return bars_by_symbol(_prices())


def test_bars_index_by_bar_end(bars) -> None:
    assert bars["AAA"].ends[0] == T0 + H
    assert bars["AAA"].close_at(T0 + 24 * H) == pytest.approx(100.23)
    assert bars["AAA"].close_at(T0 + 24 * H + 1) is None


def test_stop_replay_full_decayed_and_time_stop(bars) -> None:
    entry = T0 + 24 * H
    stopped = dict(symbol="BBB", entry_ts_ms=entry, entry_price=bars["BBB"].close_at(entry), stop_price=bars["BBB"].close_at(entry) * 0.97)
    ets, epx, reason = simulate_exit(stopped, bars["BBB"], entry + 72 * H, GEOMETRY)
    assert (ets, reason) == (T0 + 31 * H, "stop_loss")
    assert epx == pytest.approx(stopped["entry_price"] * 0.97)
    decayed = dict(symbol="CCC", entry_ts_ms=entry, entry_price=bars["CCC"].close_at(entry), stop_price=bars["CCC"].close_at(entry) * 0.97)
    ets, epx, reason = simulate_exit(decayed, bars["CCC"], entry + 72 * H, GEOMETRY)
    assert (ets, reason) == (T0 + 75 * H, "decayed_stop_loss")
    assert epx == pytest.approx(decayed["entry_price"] * (1 - 0.015))
    # without a geometry the same dip is ignored and the clock decides
    assert simulate_exit(decayed, bars["CCC"], entry + 72 * H)[2] == "time_stop"
    clean = dict(symbol="AAA", entry_ts_ms=entry, entry_price=bars["AAA"].close_at(entry), stop_price=bars["AAA"].close_at(entry) * 0.97)
    assert simulate_exit(clean, bars["AAA"], entry + 72 * H, GEOMETRY) == (entry + 72 * H, pytest.approx(100.95), "time_stop")
    assert simulate_exit(clean, bars["AAA"], T0 + 10 * DAY + H, GEOMETRY)[2] == "data_end"


def test_recorded_exits_are_reproduced_and_the_recorded_rule_changes_nothing(bars) -> None:
    trades = _ledger(bars, [("AAA", T0 + 24 * H), ("BBB", T0 + 24 * H), ("AAA", T0 + 5 * DAY), ("CCC", T0 + 24 * H)])
    assert set(trades["exit_reason"].to_list()) == {"time_stop", "stop_loss", "decayed_stop_loss"}
    assert check_reproduction(trades, bars, pricing=PRICING, geometry=GEOMETRY) == (0, 0.0)
    result = evaluate_overlay(
        trades, bars, lambda t, b: int(t["planned_exit_ts_ms"]), pricing=PRICING, geometry=GEOMETRY, draws=20,
        candidates=alive_exit_candidates(),
    )
    assert result.n_changed == 0 and result.total_delta == 0.0
    assert result.per_trade["changed"].to_list() == [False] * 4
    assert result.per_trade["variant_exit_ts_ms"].to_list() == result.per_trade["exit_ts_ms"].to_list()
    assert result.placebo_deltas.tolist() == [0.0] * 20
    assert result.share_placebo_beating_real == 1.0
    assert result.ledger_max_abs_diff == 0.0
    assert math.isnan(result.t_changed)
    # a ledger priced a different way is reported, not hidden
    off = trades.with_columns((pl.col("net_return") + 0.001).alias("net_return"))
    assert evaluate_overlay(off, bars, lambda t, b: None, pricing=PRICING, geometry=GEOMETRY, draws=0).ledger_max_abs_diff == pytest.approx(0.001)


def test_a_shorter_clock_changes_every_unstopped_trade_and_its_placebo_deals_the_same_horizons(bars) -> None:
    entries = [("AAA", T0 + 24 * H), ("AAA", T0 + 5 * DAY), ("AAA", T0 + 6 * DAY), ("AAA", T0 + 4 * DAY + 7 * H)]
    trades = _ledger(bars, entries)
    result = evaluate_overlay(trades, bars, hard_clock(48), pricing=PRICING, geometry=GEOMETRY, draws=30, name="48h")
    assert result.n_changed == 4
    expected = 0.0
    for symbol, entry in entries:
        entry_px = bars[symbol].close_at(entry)
        expected += 0.1 * (bars[symbol].close_at(entry + 48 * H) - bars[symbol].close_at(entry + 72 * H)) / entry_px
    assert result.total_delta == pytest.approx(expected)
    assert result.per_trade["exit_reason"].to_list() == ["time_stop"] * 4
    assert result.per_trade["hard_exit_ts_ms"].to_list() == sorted(e + 48 * H for _, e in entries)
    assert result.mean_delta_changed_bp == pytest.approx(expected / 4 * 1e4)
    assert result.t_changed < -100  # four nearly identical negative deltas
    assert result.by_year == {2024: pytest.approx(expected)} and result.n_worse_years == 1
    # every trade changed, every horizon is 48h: each placebo draw is the real assignment, summed in another order
    assert np.abs(result.placebo_deltas - expected).max() < 1e-15
    assert result.placebo_mean == pytest.approx(expected) and result.placebo_p90 == pytest.approx(expected)
    assert result.share_placebo_beating_real == (result.placebo_deltas >= result.total_delta).mean()
    assert result.arm.delta == pytest.approx(expected) and result.arm.placebo_share == result.share_placebo_beating_real
    # change one trade only: the placebo deals its 48h horizon to one random trade, so every draw is one of four deltas
    one = evaluate_overlay(
        trades, bars, lambda t, b: int(t["entry_ts_ms"]) + 48 * H if t["entry_ts_ms"] == T0 + 24 * H else None,
        pricing=PRICING, geometry=GEOMETRY, draws=40,
    )
    assert one.n_changed == 1
    assert set(np.round(one.placebo_deltas, 12)) <= set(np.round(result.per_trade["delta"].to_numpy(), 12))
    assert 0.0 <= one.share_placebo_beating_real <= 1.0
    summary = result.summary()
    assert summary["variant"] == "48h" and summary["n_changed"] == 4


def test_a_stopped_trade_is_untouched_by_a_later_clock(bars) -> None:
    trades = _ledger(bars, [("BBB", T0 + 24 * H)])
    result = evaluate_overlay(trades, bars, hard_clock(96), pricing=PRICING, geometry=GEOMETRY, draws=5)
    assert result.n_changed == 0 and result.per_trade["exit_reason"].to_list() == ["stop_loss"]


def test_daily_stamps_and_the_state_exit_placebo(bars) -> None:
    trades = _ledger(bars, [("AAA", T0 + 24 * H), ("AAA", T0 + 5 * DAY), ("CCC", T0 + 24 * H)])
    first = trades.row(0, named=True)
    # signal defaults to the fill; stamps at +1d and +2d land before the 72h exit, +3d does not
    assert alive_daily_stamps(first) == [first["entry_ts_ms"] + DAY, first["entry_ts_ms"] + 2 * DAY]
    with_signal = dict(first, entry_signal_ts_ms=first["entry_ts_ms"] - 6 * H)
    assert alive_daily_stamps(with_signal) == [with_signal["entry_signal_ts_ms"] + k * DAY for k in (1, 2, 3)]
    assert alive_exit_candidates()(first, bars["AAA"]) == [s + H for s in alive_daily_stamps(first)]

    rule = state_exit_rule(lambda t, s: t["symbol"] == "AAA" and s == int(t["entry_ts_ms"]) + DAY)
    result = evaluate_overlay(trades, bars, rule, pricing=PRICING, geometry=GEOMETRY, candidates=alive_exit_candidates(), draws=50)
    per = result.per_trade
    assert per.filter(pl.col("symbol") == "AAA")["variant_exit_ts_ms"].to_list() == [e + 25 * H for e in per.filter(pl.col("symbol") == "AAA")["entry_ts_ms"].to_list()]
    assert per.filter(pl.col("symbol") == "CCC")["changed"].to_list() == [False]
    assert result.n_changed == 2
    assert result.placebo_deltas.shape == (50,) and np.isfinite(result.placebo_deltas).all()
    again = evaluate_overlay(trades, bars, rule, pricing=PRICING, geometry=GEOMETRY, candidates=alive_exit_candidates(), draws=50)
    np.testing.assert_array_equal(result.placebo_deltas, again.placebo_deltas)
    # the CCC trade stopped out after 75h, so both of its stamps are alive too: every draw exits two of three trades
    assert set(np.round(result.placebo_deltas, 12)).issubset(set(np.round(_all_two_of_three_deltas(trades, bars), 12)))


def _all_two_of_three_deltas(trades: pl.DataFrame, bars) -> list[float]:
    rows = trades.sort(["entry_ts_ms", "symbol"]).to_dicts()
    per_trade = []
    for t in rows:
        options = []
        for clock in alive_exit_candidates()(t, bars[t["symbol"]]):
            ets, epx, _ = simulate_exit(t, bars[t["symbol"]], clock, GEOMETRY)
            options.append(PRICING.net(t, ets, epx) - t["net_return"])
        per_trade.append(options)
    totals = []
    for i in range(3):
        for j in range(i + 1, 3):
            for a in per_trade[i]:
                for b in per_trade[j]:
                    totals.append(a + b)
    return totals


def test_prices_are_taken_from_the_bars_when_the_ledger_has_none(bars) -> None:
    entry = T0 + 24 * H
    trades = pl.DataFrame(dict(symbol=["AAA"], entry_ts_ms=[entry], exit_ts_ms=[entry + 72 * H]))
    result = evaluate_overlay(trades, bars, hard_clock(48), pricing=Pricing(0.0), draws=1)
    assert result.per_trade["base_net"][0] == pytest.approx(100.95 / 100.23 - 1)
    assert result.per_trade["variant_net"][0] == pytest.approx(100.71 / 100.23 - 1)
    with pytest.raises(ValueError):
        evaluate_overlay(trades.with_columns(pl.lit(entry + 1).alias("exit_ts_ms")), bars, hard_clock(48), pricing=Pricing(0.0), draws=1)


def test_pricing_weight_cost_side_and_settlement_funding() -> None:
    trade = dict(symbol="AAA", entry_ts_ms=T0 + 24 * H, entry_price=100.0, notional_weight=0.5)
    assert Pricing(45.0).net(trade, T0 + 96 * H, 101.0) == pytest.approx(0.5 * (0.01 - 0.0045))
    assert Pricing(45.0).net(dict(trade, side="short"), T0 + 96 * H, 101.0) == pytest.approx(0.5 * (-0.01 - 0.0045))
    assert Pricing(0.0).net(dict(trade, notional_weight=None), T0 + 96 * H, 101.0) == pytest.approx(0.01)
    funding = pl.DataFrame(dict(ts_ms=[T0 + k * 8 * H for k in range(16)], symbol=["AAA"] * 16,
                                funding_rate=[1e-4 * (k + 1) for k in range(16)]))
    fund = settlement_funding(funding)
    # settlements in (24h, 96h]: k = 4..12 (32h..96h) -> rates 5..13 x 1e-4; a long pays them, the 24h print is excluded
    assert fund("AAA", "long", T0 + 24 * H, T0 + 96 * H) == pytest.approx(-1e-4 * sum(range(5, 14)))
    assert fund("AAA", "short", T0 + 24 * H, T0 + 96 * H) == pytest.approx(1e-4 * sum(range(5, 14)))
    assert fund("AAA", "long", T0 + 24 * H - 1, T0 + 24 * H) == pytest.approx(-4e-4)
    assert fund("ZZZ", "long", T0, T0 + DAY) == 0.0
    priced = Pricing(45.0, funding_return=fund).net(trade, T0 + 96 * H, 101.0)
    assert priced == pytest.approx(0.5 * (0.01 - 0.0045 - 1e-4 * sum(range(5, 14))))
