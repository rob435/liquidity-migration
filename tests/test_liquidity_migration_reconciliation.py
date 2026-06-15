from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.reconciliation import (
    MS_PER_DAY,
    SNIPER_TRADE_SUFFIX,
    _continuous_beats_daily_mar,
    _fee_adjusted_return,
    _trade_ledger_daily_returns,
    _write_pairs_csv,
    format_reconciliation_report,
    reconcile_paper_demo,
)


def test_reconcile_pairs_trades_and_measures_slippage() -> None:
    paper = pl.DataFrame(
        [
            {"trade_id": "p1", "symbol": "AAAUSDT", "side": "short", "entry_ts_ms": 1000,
             "entry_price": 100.0, "qty": 1.0, "status": "closed", "exit_price": 90.0},
            {"trade_id": "p2", "symbol": "BBBUSDT", "side": "long", "entry_ts_ms": 2000,
             "entry_price": 50.0, "qty": 2.0, "status": "open", "exit_price": 0.0},
            {"trade_id": "p3", "symbol": "CCCUSDT", "side": "short", "entry_ts_ms": 3000,
             "entry_price": 200.0, "qty": 1.0, "status": "open", "exit_price": 0.0},
        ]
    )
    demo = pl.DataFrame(
        [
            {"trade_id": "d1", "symbol": "AAAUSDT", "side": "short", "entry_ts_ms": 1100,
             "entry_price": 99.0, "qty": 1.0, "status": "closed", "exit_price": 91.0},
            {"trade_id": "d2", "symbol": "BBBUSDT", "side": "long", "entry_ts_ms": 2050,
             "entry_price": 50.5, "qty": 2.0, "status": "open", "exit_price": 0.0},
            {"trade_id": "d3", "symbol": "DDDUSDT", "side": "short", "entry_ts_ms": 4000,
             "entry_price": 10.0, "qty": 1.0, "status": "open", "exit_price": 0.0},
        ]
    )

    result = reconcile_paper_demo(paper, demo)
    summary = result["summary"]
    assert summary["paper_trades"] == 3
    assert summary["demo_trades"] == 3
    assert summary["paired"] == 2
    assert summary["paper_only"] == 1
    assert summary["demo_only"] == 1
    assert summary["closed_pairs"] == 1
    assert summary["entry_slippage_bps_mean"] == pytest.approx(100.0)
    assert summary["entry_slippage_bps_worst"] == pytest.approx(100.0)
    assert summary["exit_slippage_bps_mean"] == pytest.approx(111.1111, rel=1e-4)

    pairs = {pair["symbol"]: pair for pair in result["pairs"]}
    assert pairs["AAAUSDT"]["entry_slippage_bps"] == pytest.approx(100.0)
    assert pairs["AAAUSDT"]["exit_slippage_bps"] == pytest.approx(111.1111, rel=1e-4)
    assert pairs["AAAUSDT"]["paper_return_pct"] == pytest.approx(10.0)
    assert pairs["BBBUSDT"]["exit_slippage_bps"] is None
    # Richer report: the pair carries raw entry+exit prices, not just bps gaps.
    assert pairs["AAAUSDT"]["paper_entry_price"] == pytest.approx(100.0)
    assert pairs["AAAUSDT"]["demo_entry_price"] == pytest.approx(99.0)
    assert pairs["AAAUSDT"]["paper_exit_price"] == pytest.approx(90.0)
    assert pairs["AAAUSDT"]["demo_exit_price"] == pytest.approx(91.0)
    report = format_reconciliation_report(result)
    assert "Paper vs Demo Reconciliation" in report
    assert "paper entry" in report and "demo entry" in report
    assert "paper exit" in report and "demo exit" in report
    assert "| 100 | 99 |" in report  # closed pair's raw entry prices render
    assert "| 90 | 91 |" in report  # ...and its raw exit prices


def test_reconcile_empty_ledgers() -> None:
    result = reconcile_paper_demo(pl.DataFrame(), pl.DataFrame())
    summary = result["summary"]
    assert summary["paper_trades"] == 0
    assert summary["demo_trades"] == 0
    assert summary["paired"] == 0
    assert summary["entry_slippage_bps_mean"] == 0.0
    assert "No paired trades yet" in format_reconciliation_report(result)


def test_reconcile_tolerance_excludes_far_apart_entries() -> None:
    paper = pl.DataFrame(
        [
            {"trade_id": "p1", "symbol": "AAAUSDT", "side": "short", "entry_ts_ms": 1_000_000,
             "entry_price": 100.0, "qty": 1.0, "status": "open", "exit_price": 0.0},
        ]
    )
    demo = pl.DataFrame(
        [
            {"trade_id": "d1", "symbol": "AAAUSDT", "side": "short", "entry_ts_ms": 5_000_000,
             "entry_price": 99.0, "qty": 1.0, "status": "open", "exit_price": 0.0},
        ]
    )

    result = reconcile_paper_demo(paper, demo, entry_tolerance_ms=600_000)
    assert result["summary"]["paired"] == 0
    assert result["summary"]["paper_only"] == 1
    assert result["summary"]["demo_only"] == 1


def test_reconcile_surfaces_exit_ts_gap_and_reason_divergence() -> None:
    """The reconciliation report must surface exit-time skew and exit-reason
    divergence per pair — these are the most useful execution-quality signals
    and missing them hides a class of bugs where paper and demo close trades
    for different reasons at noticeably different times."""
    paper = pl.DataFrame(
        [
            {
                "trade_id": "p1", "symbol": "AAAUSDT", "side": "short",
                "entry_ts_ms": 1_000_000, "entry_exec_time_ms": 1_000_500,
                "entry_price": 100.0, "entry_fee_usdt": 0.0,
                "qty": 1.0, "status": "closed",
                "exit_price": 90.0, "exit_ts_ms": 2_000_000,
                "exit_exec_time_ms": 2_000_400,
                "exit_reason": "take_profit", "exit_fee_usdt": 0.0,
            }
        ]
    )
    demo = pl.DataFrame(
        [
            {
                "trade_id": "d1", "symbol": "AAAUSDT", "side": "short",
                "entry_ts_ms": 1_000_100, "entry_exec_time_ms": 1_000_650,
                "entry_price": 99.5, "entry_fee_usdt": 0.05,
                "qty": 1.0, "status": "closed",
                # Demo exited 60s later and for a DIFFERENT reason (failed_fade
                # instead of take_profit). The reconciliation must flag both.
                "exit_price": 88.5, "exit_ts_ms": 2_060_000,
                "exit_exec_time_ms": 2_060_400,
                "exit_reason": "failed_fade", "exit_fee_usdt": 0.07,
            }
        ]
    )
    result = reconcile_paper_demo(paper, demo)
    summary = result["summary"]
    assert summary["paired"] == 1
    # exit_gap_ms = |2_060_400 - 2_000_400| = 60_000 ms = 60 s
    assert summary["exit_gap_ms_worst"] == 60_000
    assert summary["exit_gap_ms_median"] == 60_000
    # one pair, one exit_reason known, one divergent
    assert summary["exit_reason_compared"] == 1
    assert summary["exit_reason_divergent"] == 1
    # fee residual = (0.05+0.07) - 0 = 0.12 USDT
    assert summary["fee_gap_usdt_total"] == pytest.approx(0.12)
    pair = result["pairs"][0]
    assert pair["exit_gap_ms"] == 60_000
    assert pair["paper_exit_reason"] == "take_profit"
    assert pair["demo_exit_reason"] == "failed_fade"
    assert pair["exit_reason_match"] is False
    assert pair["fee_gap_usdt"] == pytest.approx(0.12)
    # Report rendering: new sections must appear
    report = format_reconciliation_report(result)
    assert "Exit-time skew" in report
    assert "Exit-reason divergence" in report
    assert "Fee residual" in report
    assert "take_profit" in report
    assert "failed_fade" in report


def test_reconcile_paper_demo_pairs_via_signal_ts_when_entry_ts_diverges() -> None:
    """Regression for the May-25 recovery-backfill case: demo's WAVES had
    entry_ts_ms ~3h earlier than paper's (recovery backfilled to original
    signal-bar time, while paper's entry_ts_ms was its later first-cycle
    entry). They share the same signal_ts and trade_id should pair them.
    BUT: legacy/empty trade_id rows have to fall through to signal_ts,
    NOT to entry_ts (which would miss the pair because the gap is 3h ≫
    entry_tolerance_ms default 10 min). Confirms the new Pass 1.5
    signal-ts pairing closes that gap."""
    paper = pl.DataFrame(
        [
            {
                "trade_id": "",  # legacy: no trade_id, must pair via signal_ts
                "symbol": "WAVESUSDT", "side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "entry_ts_ms": 1_700_010_795_773,  # 2:59 later than demo
                "entry_exec_time_ms": 1_700_010_795_500,
                "entry_price": 0.4007, "entry_fee_usdt": 0.0,
                "qty": 8318.7, "status": "closed",
                "exit_price": 0.3652, "exit_ts_ms": 1_700_100_000_000,
                "exit_exec_time_ms": 1_700_100_000_500,
                "exit_reason": "take_profit", "exit_fee_usdt": 0.0,
            }
        ]
    )
    demo = pl.DataFrame(
        [
            {
                "trade_id": "",
                "symbol": "WAVESUSDT", "side": "short",
                "signal_ts_ms": 1_700_000_000_000,  # SAME signal_ts as paper
                "entry_ts_ms": 1_700_000_000_000,  # recovery-backfilled to signal-bar
                "entry_exec_time_ms": 1_700_000_001_000,
                "entry_price": 0.4058, "entry_fee_usdt": 0.5,
                "qty": 8053.6, "status": "closed",
                "exit_price": 0.3982, "exit_ts_ms": 1_700_090_000_000,
                "exit_exec_time_ms": 1_700_090_000_500,
                "exit_reason": "take_profit", "exit_fee_usdt": 0.4,
            }
        ]
    )
    # entry_tolerance default 600_000 ms is FAR smaller than the 3h entry-ts
    # gap; only signal-ts pairing (60s default tolerance) can close this.
    result = reconcile_paper_demo(paper, demo)
    assert result["summary"]["paired"] == 1, (
        "signal_ts pairing must close the 3h entry_ts gap that the legacy "
        "entry_ts pass alone would have missed"
    )
    pair = result["pairs"][0]
    assert pair["symbol"] == "WAVESUSDT"
    # Fee residual should pick up both demo legs' fees (0.5 + 0.4 = 0.9)
    assert pair["fee_gap_usdt"] == pytest.approx(0.9)


def test_combined_book_summary_uses_fees() -> None:
    """combined-book-telegram-report's _ledger_pnl must subtract fees when
    entry_fee_usdt / exit_fee_usdt are present so the headline matches
    Bybit's net closedPnl. Without this, the report over-reports realized
    PnL by ~fees (which compounds quickly across many trades)."""
    import tempfile
    from liquidity_migration.long_native_event_demo import _ledger_pnl
    from liquidity_migration.storage import write_dataset

    with tempfile.TemporaryDirectory() as root_str:
        root = Path(root_str)
        trades = pl.DataFrame(
            [
                {
                    "trade_id": "t1", "symbol": "AAAUSDT", "side": "short",
                    "status": "closed",
                    "entry_price": 100.0, "exit_price": 90.0, "qty": 1.0,
                    "entry_fee_usdt": 0.05, "exit_fee_usdt": 0.07,
                },
                {
                    "trade_id": "t2", "symbol": "BBBUSDT", "side": "long",
                    "status": "open",
                    "entry_price": 50.0, "exit_price": 0.0, "qty": 2.0,
                    "entry_fee_usdt": 0.03, "exit_fee_usdt": 0.0,
                },
            ]
        )
        write_dataset(trades, root, "event_demo_trades", partition_by=())
        count, realized, open_notional = _ledger_pnl(root, "event_demo_trades")
        assert count == 2
        # Gross PnL on the closed short: (100-90)*1 = 10
        # Net = 10 - 0.05 - 0.07 = 9.88
        assert realized == pytest.approx(9.88)
        # Open notional from t2 = 2 * 50 = 100
        assert open_notional == pytest.approx(100.0)


def test_write_pairs_csv_emits_machine_readable_companion(tmp_path: Path) -> None:
    """The per-pair CSV is the machine-readable companion to the markdown report
    (sortable/filterable per-trade reconciliation detail). It sits next to the .md
    as <stem>_pairs.csv; no paired trades => no file."""
    report_path = tmp_path / "paper_demo_reconciliation.md"
    pairs = [
        {
            "symbol": "AAAUSDT", "side": "short",
            "paper_entry_price": 100.0, "demo_entry_price": 99.0, "entry_slippage_bps": 100.0,
            "paper_exit_price": 90.0, "demo_exit_price": 91.0, "exit_slippage_bps": 111.11,
        }
    ]
    csv_path = _write_pairs_csv(report_path, pairs)
    assert csv_path is not None and csv_path.endswith("paper_demo_reconciliation_pairs.csv")
    df = pl.read_csv(csv_path)
    assert df.height == 1
    assert {"symbol", "paper_entry_price", "demo_entry_price", "paper_exit_price", "demo_exit_price"} <= set(df.columns)
    assert df["paper_entry_price"][0] == pytest.approx(100.0)
    assert df["demo_exit_price"][0] == pytest.approx(91.0)
    # No paired trades -> no companion file.
    assert _write_pairs_csv(report_path, []) is None


def test_absent_net_return_uses_notional_weighted_gross() -> None:
    """audit2c: gross_trade_return is weighted by notional/equity before fees.

    notional/equity = 5000/1000 = 5x. The gross return is scaled by that factor,
    THEN the equity-fractional fee is subtracted — both terms now share the
    notional-weighted, equity-fractional basis.
    """
    row = {
        "gross_trade_return": 0.02,
        "notional_usdt": 5000.0,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.5,
        "exit_fee_usdt": 0.5,
    }
    out = _fee_adjusted_return(row, net_of_cost=False)

    weight = 5000.0 / 1000.0
    expected = 0.02 * weight - (0.5 + 0.5) / 1000.0
    assert out == pytest.approx(expected)  # 0.1 - 0.001 = 0.099

    # The old (unweighted) value subtracted fees from the RAW gross return.
    old_unweighted = 0.02 - (0.5 + 0.5) / 1000.0  # 0.019
    assert out != pytest.approx(old_unweighted)
    # Delta vs the old basis: +0.08 (the gross part scaled by 5x).
    assert out - old_unweighted == pytest.approx(0.02 * (weight - 1.0))


def test_present_net_return_unchanged() -> None:
    """audit2c guard: a trade WITH net_return (already notional-weighted) keeps the
    exact same fee-adjusted return — only the gross_trade_return branch is touched."""
    row = {
        "net_return": 0.010,  # = gross_trade_return * notional_weight already
        "notional_usdt": 5000.0,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.6,
        "exit_fee_usdt": 0.6,
    }
    out = _fee_adjusted_return(row, net_of_cost=False)
    assert out == pytest.approx(0.010 - (0.6 + 0.6) / 1000.0)


def test_gross_without_notional_falls_back_to_raw() -> None:
    """audit2c: with no usable notional/equity the weighting is a safe no-op, so the
    raw gross return is used (no spurious zeroing) before fees are applied."""
    row = {
        "gross_trade_return": 0.02,
        "equity_usdt": 1000.0,  # notional_usdt absent -> weight skipped
        "entry_fee_usdt": 0.5,
        "exit_fee_usdt": 0.5,
    }
    out = _fee_adjusted_return(row, net_of_cost=False)
    assert out == pytest.approx(0.02 - (0.5 + 0.5) / 1000.0)


# ---------------------------------------------------------------------------
# audit2
# [8] reconciliation: a zero-drawdown continuous book (mar=None) is the BEST
# drawdown case, not a MAR failure.
# ---------------------------------------------------------------------------

def test_zero_drawdown_continuous_beats_finite_daily_mar() -> None:
    continuous = {"mar": None, "max_drawdown": 0.0, "total_return": 0.20}
    daily = {"mar": 3.0, "max_drawdown": -0.10, "total_return": 0.15}
    assert _continuous_beats_daily_mar(continuous, daily) is True  # was False (false alarm)


def test_zero_drawdown_continuous_with_lower_return_does_not_beat() -> None:
    continuous = {"mar": None, "max_drawdown": 0.0, "total_return": 0.05}
    daily = {"mar": 3.0, "max_drawdown": -0.10, "total_return": 0.15}
    assert _continuous_beats_daily_mar(continuous, daily) is False


def test_finite_mar_comparison_unchanged() -> None:
    assert _continuous_beats_daily_mar(
        {"mar": 5.0, "max_drawdown": -0.04, "total_return": 0.2},
        {"mar": 3.0, "max_drawdown": -0.10, "total_return": 0.15},
    ) is True
    assert _continuous_beats_daily_mar(
        {"mar": 2.0, "max_drawdown": -0.08, "total_return": 0.1},
        {"mar": 3.0, "max_drawdown": -0.10, "total_return": 0.15},
    ) is False


# --------------------------------------------------------------------------
# code-quality-4 + cost-funding-2: _fee_adjusted_return source-convention guard
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def test_fee_adjusted_return_live_subtracts_fees_only_once() -> None:
    """code-quality-4: a LIVE (gross-of-cost) row has fees subtracted exactly once."""
    row = {
        "net_return": 0.010,  # gross-of-cost: gtr * notional_weight
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.6,
        "exit_fee_usdt": 0.6,
    }
    out = _fee_adjusted_return(row, net_of_cost=False)
    assert out == pytest.approx(0.010 - (0.6 + 0.6) / 1000.0)


def test_fee_adjusted_return_backtest_does_not_double_subtract_fees() -> None:
    """code-quality-4: a BACKTEST (net-of-cost) row must NOT have fees re-subtracted.

    Original bug: _fee_adjusted_return ALWAYS subtracted fees, so a net-of-cost
    frame had its fees double-counted, understating the return. With net_of_cost=True
    the stored net return passes through unchanged.
    """
    net_of_cost_return = 0.008  # gross + cost_return + funding_return already applied
    row = {
        "net_return": net_of_cost_return,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.6,
        "exit_fee_usdt": 0.6,
    }
    out = _fee_adjusted_return(row, net_of_cost=True)
    assert out == pytest.approx(net_of_cost_return)
    # And demonstrably different from the (wrong) double-subtracted value.
    assert out != pytest.approx(net_of_cost_return - (0.6 + 0.6) / 1000.0)


def test_fee_adjusted_return_folds_in_funding_when_present_usdt() -> None:
    """cost-funding-2: when the live ledger carries a per-trade funding term it is
    folded into the return. A received funding credit (+usdt) improves the return."""
    base = {
        "net_return": 0.010,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.5,
        "exit_fee_usdt": 0.5,
    }
    without = _fee_adjusted_return(dict(base), net_of_cost=False)
    with_funding = _fee_adjusted_return({**base, "funding_usdt": 2.0}, net_of_cost=False)
    assert with_funding == pytest.approx(without + 2.0 / 1000.0)


def test_fee_adjusted_return_folds_in_funding_when_present_fractional() -> None:
    """cost-funding-2: a fractional funding_return term is folded in directly."""
    base = {
        "net_return": 0.010,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.5,
        "exit_fee_usdt": 0.5,
    }
    without = _fee_adjusted_return(dict(base), net_of_cost=False)
    with_funding = _fee_adjusted_return({**base, "funding_return": 0.0015}, net_of_cost=False)
    assert with_funding == pytest.approx(without + 0.0015)


def test_trade_ledger_daily_returns_forwards_net_of_cost_flag() -> None:
    """code-quality-4: the daily aggregator forwards net_of_cost so a backtest frame
    is not silently fee-double-subtracted on its way into the daily series."""
    frame = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "net_return": 0.008,
                "equity_usdt": 1000.0,
                "entry_fee_usdt": 0.6,
                "exit_fee_usdt": 0.6,
                "exit_ts_ms": 5 * MS_PER_DAY + 1,
            }
        ]
    )
    live = _trade_ledger_daily_returns(frame, net_of_cost=False)
    backtest = _trade_ledger_daily_returns(frame, net_of_cost=True)
    day = 5 * MS_PER_DAY
    assert backtest[day] == pytest.approx(0.008)
    assert live[day] == pytest.approx(0.008 - (0.6 + 0.6) / 1000.0)
    assert backtest[day] != pytest.approx(live[day])


# --------------------------------------------------------------------------
# sniper-2: snipe rows excluded from paper/demo pairing, reported separately
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def _recon_trade(trade_id: str, *, symbol: str, entry_ts_ms: int) -> dict:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": "short",
        "signal_ts_ms": entry_ts_ms - 1000,
        "entry_ts_ms": entry_ts_ms,
        "entry_price": 100.0,
        "qty": 1.0,
        "status": "closed",
        "exit_price": 99.0,
        "exit_ts_ms": entry_ts_ms + 3600_000,
        "exit_reason": "tp",
    }


def test_snipe_rows_excluded_from_demo_only_count() -> None:
    """sniper-2: a filled demo snipe has no paper twin BY DESIGN. It must NOT inflate
    demo_only (the slippage/divergence diagnostic) and must be reported separately."""
    paper = pl.DataFrame([_recon_trade("BTC-1", symbol="BTCUSDT", entry_ts_ms=1_000_000)])
    demo = pl.DataFrame(
        [
            _recon_trade("BTC-1", symbol="BTCUSDT", entry_ts_ms=1_000_000),
            _recon_trade("ETH-1" + SNIPER_TRADE_SUFFIX, symbol="ETHUSDT", entry_ts_ms=2_000_000),
        ]
    )
    result = reconcile_paper_demo(paper, demo)
    summary = result["summary"]
    # The shared BTC trade pairs; the snipe is pulled out, so demo_only stays 0.
    assert summary["paired"] == 1
    assert summary["demo_only"] == 0
    assert summary["snipe_demo_only"] == 1
    # The snipe must not be counted in the pairing population.
    assert summary["demo_trades"] == 1


def test_snipe_exclusion_does_not_drop_real_demo_only() -> None:
    """sniper-2 guard: a genuine (non-snipe) demo-only row is still counted."""
    paper = pl.DataFrame([_recon_trade("BTC-1", symbol="BTCUSDT", entry_ts_ms=1_000_000)])
    demo = pl.DataFrame(
        [
            _recon_trade("BTC-1", symbol="BTCUSDT", entry_ts_ms=1_000_000),
            _recon_trade("ETH-1", symbol="ETHUSDT", entry_ts_ms=2_000_000),  # real demo-only
            _recon_trade("XRP-1" + SNIPER_TRADE_SUFFIX, symbol="XRPUSDT", entry_ts_ms=3_000_000),
        ]
    )
    result = reconcile_paper_demo(paper, demo)
    summary = result["summary"]
    assert summary["paired"] == 1
    assert summary["demo_only"] == 1  # ETH-1 only
    assert summary["snipe_demo_only"] == 1  # XRP snipe reported apart

