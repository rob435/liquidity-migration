"""The record LONG keeps of what it asked the engine to hold.

These are about the seam, not the strategy: the entry screen, the exit planner
and the cooldown are tested where they live, and what is checked here is that
the record reads back as the table they were written against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.core._common import exact_duration_ms
from liquidity_migration.strategy import long_native_event_demo as long_demo
from liquidity_migration.strategy.long_book_state import (
    LongBookEntry,
    LongBookState,
    read_book_state,
    write_book_state,
)

NOW_MS = 1_755_000_000_000


def _entry(symbol: str, **overrides: object) -> LongBookEntry:
    fields: dict[str, object] = {
        "trade_id": f"long-{symbol}-1",
        "symbol": symbol,
        "strategy_id": "long_v12",
        "notional_usdt": 120.0,
        "stop_loss_fraction": 0.18,
        "leverage": 2.0,
        "entered_ts_ms": NOW_MS,
        "entry_price": 10.0,
        "max_hold_deadline_ts_ms": NOW_MS + exact_duration_ms(days=3),
    }
    fields.update(overrides)
    return LongBookEntry(**fields)  # type: ignore[arg-type]


def test_a_written_record_reads_back_field_for_field(tmp_path: Path) -> None:
    path = tmp_path / "book-state.json"
    state = LongBookState(
        held={"KAITOUSDT": _entry("KAITOUSDT", stop_decay_after_ms=172_800_000)},
        left_at_ms={"COTIUSDT": NOW_MS - 1_000},
    )

    write_book_state(path, state)

    back = read_book_state(path)
    assert back.held["KAITOUSDT"] == state.held["KAITOUSDT"]
    assert back.left_at_ms == {"COTIUSDT": NOW_MS - 1_000}


def test_an_unreadable_record_starts_from_nothing_rather_than_raising(tmp_path: Path) -> None:
    """A producer that cannot read its own memory must still be able to run.

    Asking for nothing is safe -- the engine holds what it holds. Raising would
    stop the sleeve over a file that is only ever this producer's own note.
    """

    path = tmp_path / "torn.json"
    path.write_text("{ this is not json")

    assert read_book_state(path) == LongBookState()
    assert read_book_state(tmp_path / "absent.json") == LongBookState()


def test_a_version_this_reader_does_not_know_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"version": 99, "held": [{"symbol": "KAITOUSDT"}]}))

    assert read_book_state(path).held == {}


def test_one_unreadable_row_does_not_take_the_others_with_it(tmp_path: Path) -> None:
    path = tmp_path / "mixed.json"
    good = _entry("KAITOUSDT")
    payload = {
        "version": 1,
        "held": [
            {"symbol": "BROKENUSDT"},  # no trade_id, no size, no stop
            {
                "trade_id": good.trade_id,
                "symbol": good.symbol,
                "strategy_id": good.strategy_id,
                "notional_usdt": good.notional_usdt,
                "stop_loss_fraction": good.stop_loss_fraction,
                "leverage": good.leverage,
                "entered_ts_ms": good.entered_ts_ms,
                "entry_price": good.entry_price,
                "max_hold_deadline_ts_ms": good.max_hold_deadline_ts_ms,
            },
        ],
        "left_at_ms": {},
    }
    path.write_text(json.dumps(payload))

    back = read_book_state(path)
    assert list(back.held) == ["KAITOUSDT"]


def test_the_record_reads_back_as_the_table_the_exit_planner_expects() -> None:
    """The whole point of the shape: `_plan_time_stop_exits` is unchanged."""

    due = _entry("KAITOUSDT", max_hold_deadline_ts_ms=NOW_MS - 1)
    early = _entry("COTIUSDT", max_hold_deadline_ts_ms=NOW_MS + 60_000)
    rows = LongBookState(held={"KAITOUSDT": due, "COTIUSDT": early}).as_trade_rows()

    plans = long_demo._plan_time_stop_exits(rows, now_ms=NOW_MS, price_by_symbol={})

    assert [plan["symbol"] for plan in plans] == ["KAITOUSDT"]
    assert plans[0]["exit_reason"] == "time_stop"


def test_a_departed_name_reads_back_as_a_cooldown() -> None:
    rows = LongBookState(left_at_ms={"KAITOUSDT": NOW_MS - 1_000}).as_trade_rows()

    cooldown = long_demo._cooldown_until_long(rows, cooldown_days=7)

    assert cooldown["KAITOUSDT"] == NOW_MS - 1_000 + exact_duration_ms(days=7)


class _Demo:
    wallet_balance_fraction = 1.0
    entry_leverage = 2.0


def _advance(state: LongBookState, **overrides: object) -> LongBookState:
    kwargs: dict[str, object] = {
        "exit_plans": [],
        "candidates": [],
        "demo": _Demo(),
        "equity_usdt": 10_000.0,
        "order_notional_pct_equity": 0.05,
        "price_by_symbol": {"KAITOUSDT": 10.0},
        "strategy_id": "long_v12",
        "now_ms": NOW_MS,
        "cooldown_days": 7,
    }
    kwargs.update(overrides)
    return long_demo._advance_long_book_state(state, **kwargs)  # type: ignore[arg-type]


def _candidate(symbol: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "trade_id": f"long-{symbol}-1",
        "symbol": symbol,
        "live_price": 10.0,
        "stop_loss_pct": 0.18,
        "position_weight": 1.0,
        "max_hold_days": 3.0,
    }
    row.update(overrides)
    return row


def test_a_candidate_enters_the_record_at_the_size_the_sleeve_decided() -> None:
    after = _advance(LongBookState(), candidates=[_candidate("KAITOUSDT")])

    entry = after.held["KAITOUSDT"]
    # equity 10_000 x wallet fraction 1.0 x 0.05 x weight 1.0
    assert entry.notional_usdt == pytest.approx(500.0)
    assert entry.stop_loss_fraction == 0.18
    assert entry.entered_ts_ms == NOW_MS
    assert entry.max_hold_deadline_ts_ms == NOW_MS + exact_duration_ms(days=3)


def test_a_candidate_with_no_usable_stop_is_not_entered() -> None:
    """`render_target_book` refuses a stop outside (0, 1), and an entry with
    no stop is not admissible anyway. This producer does not invent one."""

    after = _advance(LongBookState(), candidates=[_candidate("KAITOUSDT", stop_loss_pct=0.0)])

    assert after.held == {}


def test_an_exit_leaves_the_record_and_starts_a_cooldown() -> None:
    before = LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT")})

    after = _advance(before, exit_plans=[{"symbol": "KAITOUSDT", "trade_id": "long-KAITOUSDT-1"}])

    assert after.held == {}
    assert after.left_at_ms == {"KAITOUSDT": NOW_MS}


def test_a_name_already_in_the_record_keeps_the_size_it_entered_with() -> None:
    """Re-sizing every open name off today's equity each cycle would move the
    whole book every minute. That is a different strategy, not this one."""

    before = LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT", notional_usdt=120.0)})

    after = _advance(before, candidates=[_candidate("KAITOUSDT")], equity_usdt=50_000.0)

    assert after.held["KAITOUSDT"].notional_usdt == 120.0


def test_a_name_out_of_cooldown_is_forgotten_so_the_file_stays_bounded() -> None:
    stale = NOW_MS - exact_duration_ms(days=30)
    before = LongBookState(left_at_ms={"OLDUSDT": stale, "RECENTUSDT": NOW_MS - 1_000})

    after = _advance(before)

    assert list(after.left_at_ms) == ["RECENTUSDT"]


def test_the_book_written_from_the_record_is_absolute_and_names_every_holding() -> None:
    state = LongBookState(
        held={
            "KAITOUSDT": _entry("KAITOUSDT", notional_usdt=500.0),
            "COTIUSDT": _entry("COTIUSDT", notional_usdt=250.0, stop_loss_fraction=0.2),
        }
    )

    book = json.loads(
        long_demo._long_engine_target_book(
            state, decision_ts_ms=NOW_MS, strategy_profile="long_v12"
        )
    )

    assert book["version"] == 1
    assert book["source"] == "long_v12"
    assert [row["symbol"] for row in book["targets"]] == ["COTIUSDT", "KAITOUSDT"]
    assert book["targets"][1]["notional_usdt"] == 500.0
    assert book["targets"][1]["stop_loss_fraction"] == 0.18
    assert book["targets"][0]["leverage"] == 2.0


def test_the_books_window_clears_the_engines_entry_cutoff() -> None:
    """The engine stops opening `entry_cutoff_ms` before a book expires --
    fifteen minutes, in `plan.rs`. A window shorter than that opens nothing at
    all, ever, and would look exactly like a sleeve that had no signals.
    """

    book = json.loads(
        long_demo._long_engine_target_book(
            LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT")}),
            decision_ts_ms=NOW_MS,
            strategy_profile="long_v12",
        )
    )

    window_ms = book["valid_until_ms"] - book["decision_ts_ms"]
    assert window_ms > 15 * 60 * 1_000, "shorter than the cutoff means no entries"
    assert window_ms == exact_duration_ms(hours=1)


def test_an_empty_record_writes_an_empty_book_not_no_book() -> None:
    """Holding nothing is a decision and the engine acts on it. Writing no
    book at all is *no decision*, and a follower would hold what it holds --
    which is the wrong answer when the sleeve has decided to be flat."""

    book = json.loads(
        long_demo._long_engine_target_book(
            LongBookState(), decision_ts_ms=NOW_MS, strategy_profile="long_v12"
        )
    )

    assert book["targets"] == []
