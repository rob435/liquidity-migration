"""The record LONG keeps of what it asked the engine to hold.

These are about the seam, not the strategy: the entry screen, the exit planner
and the cooldown are tested where they live, and what is checked here is that
the record reads back as the table they were written against.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from liquidity_migration.core._common import exact_duration_ms
from liquidity_migration.strategy import long_native_event_demo as long_demo
from liquidity_migration.strategy.long_book_state import (
    BookStateError,
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


def test_a_missing_file_starts_from_nothing_rather_than_raising(tmp_path: Path) -> None:
    """The one honest empty: a producer that has never written a record
    genuinely holds nothing yet."""

    assert read_book_state(tmp_path / "absent.json") == LongBookState()


def test_an_unreadable_record_raises_instead_of_reading_as_empty(tmp_path: Path) -> None:
    """The engine reads the book as absolute, so silence about a symbol is an
    instruction to hold none of it. A torn record read as empty would
    market-close every open position at once -- and the old code wrote that
    empty record straight back, making a transient read failure permanent.
    Failing the cycle is the only safe answer; the engine holds what it holds."""

    path = tmp_path / "torn.json"
    path.write_text("{ this is not json")

    with pytest.raises(BookStateError, match="malformed JSON"):
        read_book_state(path)


def test_an_unreadable_path_raises_rather_than_reading_as_empty(tmp_path: Path) -> None:
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises(BookStateError, match="unreadable"):
        read_book_state(directory)


def test_a_version_this_reader_does_not_know_fails_the_read(tmp_path: Path) -> None:
    """A future writer's record must not be read as "hold nothing" -- that is
    the same liquidation with a different trigger."""

    path = tmp_path / "future.json"
    path.write_text(json.dumps({"version": 99, "held": [{"symbol": "KAITOUSDT"}]}))

    with pytest.raises(BookStateError, match="version"):
        read_book_state(path)


def test_one_unreadable_row_fails_the_whole_read(tmp_path: Path) -> None:
    """Dropping the broken row would read as silence about that symbol, and
    the engine answers silence by exiting it. The row's name may well be held;
    the cycle fails instead and the engine keeps everything it has."""

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

    with pytest.raises(BookStateError, match="BROKENUSDT"):
        read_book_state(path)


def test_a_bad_cooldown_stamp_is_skipped_loudly_and_the_record_survives(
    tmp_path: Path,
) -> None:
    """A cooldown stamp gates nothing but re-entry timing; it can be skipped
    without risking a position. The rest of the record stays usable."""

    path = tmp_path / "cooldown.json"
    payload = {
        "version": 1,
        "held": [asdict(_entry("KAITOUSDT"))],
        "left_at_ms": {"COTIUSDT": "not-a-number"},
    }
    path.write_text(json.dumps(payload))

    back = read_book_state(path)
    assert list(back.held) == ["KAITOUSDT"]
    assert back.left_at_ms == {}


def test_venue_truth_written_on_an_entry_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "venue.json"
    state = LongBookState(
        held={
            "KAITOUSDT": _entry(
                "KAITOUSDT",
                seen_held=True,
                venue_qty=12.5,
                venue_avg_entry_px=9.8,
                venue_ts_ms=NOW_MS,
            )
        }
    )

    write_book_state(path, state)

    back = read_book_state(path)
    assert back.held["KAITOUSDT"].venue_qty == 12.5
    assert back.held["KAITOUSDT"].venue_avg_entry_px == 9.8


def test_a_write_survives_being_read_back_from_a_fsynced_file(tmp_path: Path) -> None:
    """The write is temp file, fsync, rename, directory fsync -- so a power
    cut cannot leave half a record where a full one stood. The observable part
    in a test is that the ordinary round trip is unchanged."""

    path = tmp_path / "book-state.json"
    state = LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT")})

    write_book_state(path, state)
    assert not (tmp_path / ".book-state.json.tmp").exists(), "no temp file left behind"
    assert read_book_state(path).held["KAITOUSDT"] == state.held["KAITOUSDT"]


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
        "held_symbols": None,
    }
    kwargs.update(overrides)
    after, _resized = long_demo._advance_long_book_state(state, **kwargs)  # type: ignore[arg-type]
    return after


def _advance_full(state: LongBookState, **overrides: object) -> tuple[LongBookState, list[str]]:
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
        "held_symbols": None,
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


# ---- What the engine says is held ----
#
# The book is a want, not a holding. Before these, a venue stop that fired was
# invisible: LONG went on asking for the name, the engine refused to buy it
# back, and the slot stayed occupied for up to three days.


def test_a_name_the_engine_confirms_is_marked_as_actually_held() -> None:
    before = LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT")})

    after = _advance(before, held_symbols=frozenset({"KAITOUSDT"}))

    assert after.held["KAITOUSDT"].seen_held is True


def test_a_name_that_was_held_and_is_gone_leaves_the_book() -> None:
    """A stop fired, or somebody closed it. Nothing this producer asked for
    did, so the name leaves the record and starts its cooldown."""

    before = LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT", seen_held=True)})

    after = _advance(before, held_symbols=frozenset())

    assert after.held == {}
    assert after.left_at_ms == {"KAITOUSDT": NOW_MS}


def test_an_entry_the_engine_has_never_confirmed_is_left_alone() -> None:
    """The window between writing the book and the entry filling. Dropping
    here would abandon every entry the moment it was written."""

    before = LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT", seen_held=False)})

    after = _advance(before, held_symbols=frozenset())

    assert list(after.held) == ["KAITOUSDT"]
    assert after.left_at_ms == {}


def test_an_engine_that_says_nothing_leaves_the_whole_record_alone() -> None:
    """No heartbeat, a stale one, an engine too old to publish positions. That
    is not "holds nothing", and reading it that way drops the whole book."""

    before = LongBookState(
        held={
            "KAITOUSDT": _entry("KAITOUSDT", seen_held=True),
            "COTIUSDT": _entry("COTIUSDT", seen_held=True),
        }
    )

    after = _advance(before, held_symbols=None)

    assert sorted(after.held) == ["COTIUSDT", "KAITOUSDT"]
    assert after.left_at_ms == {}


def test_being_confirmed_once_is_remembered_across_cycles() -> None:
    # Confirmed, then the engine's reading momentarily omits it while it is
    # still there -- the flag must not flap, or the next cycle reads a
    # never-filled entry and never drops it.
    state = _advance(
        LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT")}),
        held_symbols=frozenset({"KAITOUSDT"}),
    )
    assert state.held["KAITOUSDT"].seen_held is True

    written_and_read = state.held["KAITOUSDT"]
    assert written_and_read.seen_held is True


# ---- What the venue actually holds ----
#
# The engine works each standing position toward the ask at the live mark:
# it trims what ran up and adds to what fell back once the gap clears its
# dead band, and every add re-declares the venue stop from the position's
# average entry, so the stop walks down. The ask stays frozen; the venue
# truth is what gets recorded.


def test_the_venue_reading_is_recorded_on_a_confirmed_entry() -> None:
    before = LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT", seen_held=True)})

    after, resized = _advance_full(
        before,
        held_symbols=frozenset({"KAITOUSDT"}),
        venue_holdings={"KAITOUSDT": ("long", 50.0, 10.0)},
    )

    entry = after.held["KAITOUSDT"]
    assert entry.venue_qty == 50.0
    assert entry.venue_avg_entry_px == 10.0
    assert entry.venue_ts_ms == NOW_MS
    assert resized == [], "a first sighting records, it does not allege a resize"
    # The ask itself is untouched: re-sizing open names off the mark would be
    # a different strategy.
    assert entry.notional_usdt == 120.0


def test_an_engine_move_between_readings_is_reported() -> None:
    from dataclasses import replace

    before = LongBookState(
        held={
            "KAITOUSDT": replace(
                _entry("KAITOUSDT", seen_held=True),
                venue_qty=50.0,
                venue_avg_entry_px=10.0,
                venue_ts_ms=NOW_MS - 60_000,
            )
        }
    )

    after, resized = _advance_full(
        before,
        held_symbols=frozenset({"KAITOUSDT"}),
        # The engine added ~6% more size than last cycle.
        venue_holdings={"KAITOUSDT": ("long", 53.0, 9.5)},
    )

    assert resized == ["KAITOUSDT"]
    assert after.held["KAITOUSDT"].venue_qty == 53.0


def test_an_average_entry_that_walked_down_is_recorded() -> None:
    """Adding to a falling long re-declares the stop from the venue's average
    entry, so the stop walks down with each add. That walk is exactly what
    the record has to show."""

    from dataclasses import replace

    before = LongBookState(
        held={
            "KAITOUSDT": replace(
                _entry("KAITOUSDT", seen_held=True),
                venue_qty=50.0,
                venue_avg_entry_px=10.0,
                venue_ts_ms=NOW_MS - 60_000,
            )
        }
    )

    after, resized = _advance_full(
        before,
        held_symbols=frozenset({"KAITOUSDT"}),
        venue_holdings={"KAITOUSDT": ("long", 50.0, 9.4)},
    )

    assert resized == [], "same size, different average: an add-and-trim, not a resize"
    assert after.held["KAITOUSDT"].venue_avg_entry_px == 9.4


def test_an_unconfirmed_entry_is_not_reconciled_against_the_venue() -> None:
    """The window between writing the book and the fill: there is nothing at
    the venue yet, and inventing a reading would be worse than none."""

    before = LongBookState(held={"KAITOUSDT": _entry("KAITOUSDT", seen_held=False)})

    after, resized = _advance_full(
        before,
        held_symbols=None,
        venue_holdings={},
    )

    assert after.held["KAITOUSDT"].venue_qty == 0.0
    assert resized == []
