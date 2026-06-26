"""Unit tests for the fill-level three-way price cross-check (scripts/reconcile_fills.py).

Covers the pure pieces that back the entry-price comparison: the signed bps delta, the
union join + membership flags, the summary counts/ok semantics, the continuous
notional-weighted (symbol, signal-bar) aggregation of per-component legs, and the LONG
(symbol, side, signal-day) bucketing. The data-read + PIT-panel legs are integration
surfaces exercised by running the tool, not here.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import reconcile_fills as rf  # noqa: E402
from liquidity_migration.storage import write_dataset  # noqa: E402


def _ms(y: int, m: int, d: int, h: int = 0) -> int:
    return int(dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp() * 1000)


# ----------------------------------------------------------------------------- delta_bps
def test_delta_bps_sign_and_magnitude():
    # +100 bps == 1% higher than the reference.
    assert rf.delta_bps(101.0, 100.0) == 100.0
    assert rf.delta_bps(99.0, 100.0) == -100.0


def test_delta_bps_missing_or_zero_ref_is_none():
    assert rf.delta_bps(None, 100.0) is None
    assert rf.delta_bps(100.0, None) is None
    assert rf.delta_bps(100.0, 0.0) is None
    assert rf.delta_bps(100.0, -1.0) is None


# ----------------------------------------------------------------------------- join_three_way
def test_join_three_way_union_membership_and_deltas():
    model = {("AAA", "s", "2026-06-17"): 100.0}
    demo = {("AAA", "s", "2026-06-17"): 101.0, ("BBB", "s", "2026-06-17"): 50.0}
    paper = {("AAA", "s", "2026-06-17"): 100.0}
    rows = {r["key"]: r for r in rf.join_three_way(model, demo, paper)}

    assert set(rows) == {("AAA", "s", "2026-06-17"), ("BBB", "s", "2026-06-17")}
    a = rows[("AAA", "s", "2026-06-17")]
    assert (a["in_model"], a["in_demo"], a["in_paper"]) == (True, True, True)
    assert a["bps_demo_vs_paper"] == 100.0   # demo 101 vs paper 100
    assert a["bps_demo_vs_model"] == 100.0   # demo 101 vs model 100
    assert a["bps_paper_vs_model"] == 0.0

    b = rows[("BBB", "s", "2026-06-17")]
    assert (b["in_model"], b["in_demo"], b["in_paper"]) == (False, True, False)
    assert b["bps_demo_vs_paper"] is None    # no paper leg to compare
    assert b["px_demo"] == 50.0


# ----------------------------------------------------------------------------- summarize
def test_summarize_counts_and_ok():
    rows = rf.join_three_way(
        model={("AAA", "s", "d"): 100.0},
        demo={("AAA", "s", "d"): 101.0, ("BBB", "s", "d"): 50.0},
        paper={("AAA", "s", "d"): 100.0, ("CCC", "s", "d"): 9.0},
    )
    summary, ok = rf.summarize(rows, label="X")
    assert "all3=1" in summary
    assert "demo∩paper=1" in summary
    assert "demo_only=1" in summary    # BBB
    assert "paper_only=1" in summary   # CCC
    assert ok is True                  # the one paired demo&paper key has a finite bps


def test_summarize_ok_false_on_bad_paired_price():
    # demo & paper both present but paper price is zero -> bps None -> data-quality fail.
    rows = rf.join_three_way(
        model={},
        demo={("AAA", "s", "d"): 101.0},
        paper={("AAA", "s", "d"): 0.0},
    )
    _summary, ok = rf.summarize(rows, label="X")
    assert ok is False


# ----------------------------------------------------------------------------- continuous aggregation
def test_aggregate_continuous_notional_weights_component_legs():
    bar = _ms(2026, 6, 17, 11)
    df = pl.DataFrame({
        "symbol": ["SIRENUSDT", "SIRENUSDT"],
        "signal_ts_ms": [bar + 5, bar + 9],          # same hour bar
        "entry_price": [0.040, 0.050],
        "notional_usdt": [30.0, 10.0],               # 3:1 weight -> 0.0425
        "entry_ts_ms": [bar + 7_200_000, bar + 7_200_000],
    })
    price, ebar = rf.aggregate_continuous_prices(df, 0, 9_999_999_999_999)
    key = ("SIRENUSDT", bar)
    assert key in price
    assert abs(price[key] - 0.0425) < 1e-12
    # entry bar is hour-floored from entry_ts_ms (signal bar + 2h here).
    assert ebar[key] == bar + 7_200_000


def test_aggregate_continuous_window_clips_and_floors_bar():
    inside = _ms(2026, 6, 17, 11)
    outside = _ms(2026, 6, 1, 11)
    df = pl.DataFrame({
        "symbol": ["AAA", "BBB"],
        "signal_ts_ms": [inside + 123, outside],
        "entry_price": [1.0, 2.0],
        "notional_usdt": [10.0, 10.0],
        "entry_ts_ms": [inside + 123, outside],
    })
    price, _ = rf.aggregate_continuous_prices(df, _ms(2026, 6, 10), _ms(2026, 6, 30))
    assert ("AAA", inside) in price        # bar floored to the hour
    assert all(k[0] != "BBB" for k in price)  # pre-window row dropped


def test_aggregate_continuous_no_notional_column_equal_weights():
    bar = _ms(2026, 6, 17, 11)
    df = pl.DataFrame({
        "symbol": ["AAA", "AAA"],
        "signal_ts_ms": [bar, bar],
        "entry_price": [1.0, 3.0],           # equal weight -> 2.0
    })
    price, _ = rf.aggregate_continuous_prices(df, 0, 9_999_999_999_999)
    assert abs(price[("AAA", bar)] - 2.0) < 1e-12


def test_continuous_live_prices_filters_v2_strategy_id(tmp_path: Path):
    root = tmp_path / "root"
    dataset = "continuous_fade_paper_trades"
    start = _ms(2026, 6, 18, 19)
    v1_bar = _ms(2026, 6, 18, 18)
    v2_bar = _ms(2026, 6, 18, 20)
    write_dataset(
        pl.DataFrame(
            {
                "symbol": ["OLD", "NEW"],
                "strategy_id": ["retired_continuous_paper", "continuous_fade_v2_paper"],
                "signal_ts_ms": [v1_bar, v2_bar],
                "entry_ts_ms": [v1_bar, v2_bar],
                "entry_price": [1.0, 2.0],
                "notional_usdt": [10.0, 10.0],
            }
        ),
        root,
        dataset,
        partition_by=(),
    )

    prices, _bars = rf.continuous_live_prices(
        str(root),
        dataset,
        start,
        _ms(2026, 6, 19),
        strategy_id="continuous_fade_v2_paper",
    )

    assert prices == {("NEW", v2_bar): 2.0}


# ----------------------------------------------------------------------------- long price map
def test_long_price_map_keys_by_symbol_side_signal_day():
    df = pl.DataFrame({
        "symbol": ["AAA", "AAA"],
        "signal_ts_ms": [_ms(2026, 6, 17, 1), _ms(2026, 6, 17, 23)],  # same day
        "side": ["long", "long"],
        "entry_price": [10.0, 11.0],
    })
    out = rf._long_price_map(df, "signal_ts_ms", 0, 9_999_999_999_999)
    # one (symbol, side, day) bucket; first fill wins.
    assert out == {("AAA", "long", "2026-06-17"): 10.0}


def test_long_price_map_window_and_missing_columns():
    assert rf._long_price_map(pl.DataFrame({"symbol": ["A"]}), "signal_ts_ms", 0, 1) == {}
    df = pl.DataFrame({
        "symbol": ["AAA"],
        "signal_ts_ms": [_ms(2026, 6, 1)],
        "side": ["short"],
        "entry_price": [5.0],
    })
    assert rf._long_price_map(df, "signal_ts_ms", _ms(2026, 6, 10), _ms(2026, 6, 30)) == {}


# ----------------------------------------------------------------------------- backtest candidate assembly
def test_assemble_candidates_unions_components_and_floors_age():
    bar = _ms(2026, 6, 17, 11)
    # AAA generated by two components at the same bar; BBB by one.
    fresh = {
        "p3": pl.DataFrame({"symbol": ["AAA", "BBB"], "ts_ms": [bar + 5, bar + 5]}),
        "p4p5": pl.DataFrame({"symbol": ["AAA"], "ts_ms": [bar + 9]}),
    }
    # AAA listed long ago (eligible); BBB listed yesterday (fails the 240d floor on p3).
    first = {"AAA": _ms(2024, 1, 1), "BBB": _ms(2026, 6, 16)}
    ages = {"p3": 240, "p4p5": 240}
    cand = rf.assemble_candidates(fresh, first, ages, 0, 9_999_999_999_999)
    assert cand[("AAA", bar)] == ["p3", "p4p5"]   # union, sorted
    assert ("BBB", bar) not in cand               # too young for the 240d floor


def test_assemble_candidates_window_clip():
    inside = _ms(2026, 6, 17, 11)
    outside = _ms(2026, 6, 1, 11)
    fresh = {"p4p5": pl.DataFrame({"symbol": ["AAA", "BBB"], "ts_ms": [inside, outside]})}
    cand = rf.assemble_candidates(fresh, {}, {"p4p5": 0}, _ms(2026, 6, 10), _ms(2026, 6, 30))
    assert ("AAA", inside) in cand
    assert all(k[0] != "BBB" for k in cand)


def test_continuous_backtest_candidates_applies_component_age_map(monkeypatch):
    bar = _ms(2026, 6, 17, 11)
    panel = pl.DataFrame(
        {
            "symbol": ["OLDUSDT", "YOUNGUSDT"],
            "ts_ms": [bar, bar],
            "decile": [9, 9],
            "turnover_quote": [1_000_000.0, 1_000_000.0],
        }
    )

    import liquidity_migration.continuous_events as ce

    monkeypatch.setattr(ce, "compute_continuous_decile_panel", lambda *a, **k: panel)
    monkeypatch.setattr(ce, "_entry_event_expr", lambda _trigger: pl.lit(True))

    candidates, _ = rf.continuous_backtest_candidates(
        pl.DataFrame(),
        pl.DataFrame(),
        0,
        9_999_999_999_999,
        listing_ts_by_symbol={
            "OLDUSDT": _ms(2024, 1, 1),
            "YOUNGUSDT": _ms(2026, 6, 16),
        },
    )

    assert ("OLDUSDT", bar) in candidates
    assert ("YOUNGUSDT", bar) not in candidates


# ----------------------------------------------------------------------------- tripwire split
def test_continuous_tripwire_splits_hard_vs_pending():
    covered_bar = _ms(2026, 6, 15, 11)   # within rmom coverage
    recent_bar = _ms(2026, 6, 18, 11)    # after rmom coverage -> pending
    rmom_end = _ms(2026, 6, 16)          # rmom covers through 2026-06-16
    rows = rf.join_three_way(
        model={},                                              # nothing the model generated
        demo={("AAA", covered_bar): 1.0, ("BBB", recent_bar): 2.0},
        paper={},
    )
    trip, pending = rf._continuous_tripwire(rows, rmom_end)
    assert trip == [("AAA", covered_bar)]      # unexplained within coverage -> hard tripwire
    assert pending == [("BBB", recent_bar)]    # after coverage -> pending, not a failure


def test_continuous_tripwire_ignores_confirmed_and_model_only():
    bar = _ms(2026, 6, 15, 11)
    rows = rf.join_three_way(
        model={("AAA", bar): 1.0, ("CCC", bar): 3.0},   # CCC is model-only (capacity) — not a tripwire
        demo={("AAA", bar): 1.0},                        # AAA confirmed by model — not a tripwire
        paper={},
    )
    trip, pending = rf._continuous_tripwire(rows, _ms(2026, 6, 16))
    assert trip == [] and pending == []


# ----------------------------------------------------------------------------- unmatched classification
def test_classify_continuous_unmatched_hard_vs_near_vs_norow():
    bar = _ms(2026, 6, 15, 11)
    trip = [("HARDUSDT", bar), ("EDGEUSDT", bar), ("LIQUSDT", bar), ("GAPUSDT", bar)]
    decile_index = {
        ("HARDUSDT", bar): 5,   # well off the top decile -> real drift -> HARD
        ("EDGEUSDT", bar): 8,   # D8 == target(9)-1 -> benign live-vs-closed-bar flip -> near
        ("LIQUSDT", bar): 9,    # D9 but unmatched (failed the marginal liq gate) -> near, NOT hard
        # GAPUSDT absent -> no panel row -> snapshot gap
    }
    hard, near, norow = rf.classify_continuous_unmatched(trip, decile_index)
    assert hard == [(("HARDUSDT", bar), 5)]
    assert sorted(near) == [("EDGEUSDT", bar), ("LIQUSDT", bar)]
    assert norow == [("GAPUSDT", bar)]


def test_classify_continuous_unmatched_d7_is_hard_d8_is_near():
    bar = _ms(2026, 6, 15, 11)
    hard, near, _ = rf.classify_continuous_unmatched(
        [("A", bar), ("B", bar)], {("A", bar): 7, ("B", bar): 8})
    assert hard == [(("A", bar), 7)] and near == [("B", bar)]


def test_classify_continuous_unmatched_empty():
    assert rf.classify_continuous_unmatched([], {}) == ([], [], [])


def test_continuous_tripwire_no_rmom_all_pending():
    # rmom_end None (panel unavailable) -> nothing can be confirmed -> all pending, no tripwire.
    bar = _ms(2026, 6, 15, 11)
    rows = rf.join_three_way(model={}, demo={("AAA", bar): 1.0}, paper={})
    trip, pending = rf._continuous_tripwire(rows, None)
    assert trip == [] and pending == [("AAA", bar)]
