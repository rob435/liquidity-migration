from __future__ import annotations

from scripts.research.run_engine_backtest import build_metrics, print_summary


def _report(*, fills_without_book: int, agrees: bool | None) -> dict:
    return {
        "start_wall_ms": 0,
        "end_wall_ms": 2 * 86_400_000,
        "market_events": 10,
        "orders_sent": 2,
        "reconciliation": {"engine_closed_net_usdt": 1.0, "venue_closed_net_usdt": 1.0, "agrees": agrees},
        "venue": {
            "initial_cash_usdt": 100.0,
            "equity_usdt": 101.0,
            "fills": 3,
            "maker_fills": 1,
            "stop_fills": 1,
            "liquidation_fills": 0,
            "rejected_orders": 0,
            "funding_paid_usdt": 0.0,
            "funding_settlements": 0,
            "fills_without_book": fills_without_book,
        },
        "engine": {},
        "tape": {},
    }


def test_missing_book_forced_fills_mark_the_run_unqualified(capsys) -> None:
    metrics = build_metrics(_report(fills_without_book=2, agrees=True), [], [], 100.0)
    evidence = metrics["evidence"]
    assert evidence["unqualified_forced_fills"] == 2
    assert evidence["economics_qualified"] is False
    assert "no book side" in evidence["reasons"][0]
    print_summary(metrics)
    out = capsys.readouterr().out
    assert "UNQUALIFIED economics" in out
    assert "not strategy evidence" in out


def test_a_full_tape_with_agreeing_ledgers_is_qualified(capsys) -> None:
    metrics = build_metrics(_report(fills_without_book=0, agrees=True), [], [], 100.0)
    assert metrics["evidence"] == {
        "unqualified_forced_fills": 0,
        "reconciliation_agrees": True,
        "economics_qualified": True,
        "reasons": [],
    }
    print_summary(metrics)
    assert "UNQUALIFIED" not in capsys.readouterr().out


def test_a_ledger_disagreement_is_its_own_unqualified_reason() -> None:
    metrics = build_metrics(_report(fills_without_book=0, agrees=False), [], [], 100.0)
    assert metrics["evidence"]["economics_qualified"] is False
    assert metrics["evidence"]["reasons"] == ["engine and venue closed-trip ledgers disagree"]
