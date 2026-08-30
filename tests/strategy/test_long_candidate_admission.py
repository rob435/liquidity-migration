from __future__ import annotations

from liquidity_migration.core._common import exact_duration_ms
from liquidity_migration.strategy.long_book_state import LongBookState
from liquidity_migration.strategy.long_native_event_demo import (
    _advance_long_book_state,
)


def _candidate(symbol: str, *, stop_loss_pct: float) -> dict[str, object]:
    return {
        "trade_id": f"long-{symbol}-1",
        "symbol": symbol,
        "signal_ts_ms": 1_700_000_000_000,
        "live_price": 10.0,
        "stop_loss_pct": stop_loss_pct,
        "position_weight": 1.0,
        "target_notional_usdt": 500.0,
        "entry_leverage": 2.0,
        "max_hold_duration_ms": exact_duration_ms(days=3),
        "entry_valid_until_ms": 1_700_003_600_000,
    }


def test_entry_cap_counts_admitted_names_so_a_blocked_leader_backfills() -> None:
    after, _resized = _advance_long_book_state(
        LongBookState(),
        exit_plans=[],
        candidates=[
            _candidate("HIGHUSDT", stop_loss_pct=0.0),
            _candidate("LOWUSDT", stop_loss_pct=0.10),
        ],
        price_by_symbol={"HIGHUSDT": 10.0, "LOWUSDT": 10.0},
        strategy_id="long_v12",
        now_ms=1_700_000_060_000,
        cooldown_days=7,
        held_symbols=None,
        max_new_entries=1,
    )

    assert list(after.held) == ["LOWUSDT"]


def test_position_cap_is_applied_after_same_cycle_exits() -> None:
    state, _resized = _advance_long_book_state(
        LongBookState(),
        exit_plans=[],
        candidates=[_candidate("OLDUSDT", stop_loss_pct=0.10)],
        price_by_symbol={"OLDUSDT": 10.0},
        strategy_id="long_v12",
        now_ms=1_700_000_060_000,
        cooldown_days=7,
        held_symbols=None,
        max_new_entries=1,
        max_total_positions=1,
    )

    after, _resized = _advance_long_book_state(
        state,
        exit_plans=[{"symbol": "OLDUSDT"}],
        candidates=[_candidate("NEWUSDT", stop_loss_pct=0.10)],
        price_by_symbol={"NEWUSDT": 10.0},
        strategy_id="long_v12",
        now_ms=1_700_000_120_000,
        cooldown_days=7,
        held_symbols=None,
        max_new_entries=1,
        max_total_positions=1,
    )

    assert list(after.held) == ["NEWUSDT"]
