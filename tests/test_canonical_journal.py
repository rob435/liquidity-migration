from __future__ import annotations

import json
import shutil

import polars as pl
import pytest

from liquidity_migration.canonical_journal import (
    EventSpec,
    EventType,
    JournalIntegrityError,
    LIFECYCLE_SEQUENCE,
    LifecycleTransitionError,
    append_events,
    journal_path,
    read_journal,
    rebuild_all_registered_projections,
    record_archived_paper_epoch_reset,
    record_due_markouts,
    record_verified_flat_snapshot,
    replay_journal,
    verify_journal,
)
from liquidity_migration.lifecycle_bridge import record_ledger_rows
from liquidity_migration.storage import read_dataset, write_dataset


def _spec(
    event_type: EventType,
    *,
    trade_id: str = "t1",
    mode: str = "demo",
    sequence_index: int = 0,
    event_key: str = "",
) -> EventSpec:
    order_version = 0
    position_version = 0
    for item in LIFECYCLE_SEQUENCE[: sequence_index + 1]:
        if item in {EventType.SUBMITTED, EventType.EXIT_REQUESTED}:
            order_version += 1
        if item in {EventType.FILL, EventType.PROTECTION_ACTIVE, EventType.CLOSE_FILL}:
            position_version += 1
    return EventSpec(
        event_type=event_type,
        mode=mode,
        sleeve="continuous",
        strategy_id="s1",
        trade_id=trade_id,
        symbol="BUSDT",
        side="short",
        local_ts_ms=1_700_000_000_000 + sequence_index,
        venue_ts_ms=1_700_000_000_000 + sequence_index,
        order_version=order_version,
        position_version=position_version,
        order_link_id="o1",
        qty=1.0 if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
        decision_price=0.125,
        submission_price=0.126,
        fill_price=0.127 if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
        depth_consumed_quote=0.127 if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
        latency_ms=25.0 if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
        idempotency_key=event_key or f"{mode}:{trade_id}:{event_type.value}",
    )


def test_append_only_journal_is_sequenced_hash_chained_and_idempotent(tmp_path) -> None:
    specs = [_spec(event_type, sequence_index=index) for index, event_type in enumerate(LIFECYCLE_SEQUENCE)]
    appended = append_events(tmp_path, specs)
    assert [event.sequence for event in appended] == list(range(1, 10))
    assert [event.event_type for event in read_journal(tmp_path)] == [item.value for item in LIFECYCLE_SEQUENCE]
    assert append_events(tmp_path, specs) == []

    receipt = verify_journal(tmp_path)
    assert receipt["events"] == 9
    assert receipt["trades"] == 1
    assert receipt["fills"] == 2
    for event in read_journal(tmp_path):
        assert event.event_id
        assert event.sequence > 0
        assert event.local_ts_ms > 0
        assert event.venue_ts_ms > 0
        assert event.order_version >= 0
        assert event.position_version >= 0


def test_journal_tamper_is_detected(tmp_path) -> None:
    append_events(tmp_path, [_spec(EventType.DECISION)])
    path = journal_path(tmp_path)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["symbol"] = "TAMPERUSDT"
    path.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="hash mismatch"):
        read_journal(tmp_path)


def test_reducer_rejects_skipped_lifecycle_stage(tmp_path) -> None:
    append_events(tmp_path, [_spec(EventType.DECISION)])
    with pytest.raises(LifecycleTransitionError, match="illegal transition"):
        append_events(tmp_path, [_spec(EventType.SUBMITTED, sequence_index=2)])


def test_legacy_ledgers_are_rebuildable_projections_not_authority(tmp_path) -> None:
    trade_dataset = "continuous_fade_demo_trades"
    order_dataset = "continuous_fade_demo_orders"
    order = {
        "trade_id": "trade-1",
        "strategy_id": "continuous_fade_v2",
        "symbol": "BUSDT",
        "trade_side": "short",
        "side": "Sell",
        "status": "filled",
        "submit_mode": "submitted",
        "order_link_id": "lm-en-c-b-1",
        "order_id": "venue-1",
        "ts_ms": 1_700_000_000_000,
        "exec_time_ms": 1_700_000_000_025,
        "qty": 10.0,
        "filled_qty": 10.0,
        "price": 0.126,
        "avg_fill_price": 0.127,
        "reduce_only": False,
    }
    trade = {
        "trade_id": "trade-1",
        "strategy_id": "continuous_fade_v2",
        "symbol": "BUSDT",
        "side": "short",
        "status": "open",
        "signal_ts_ms": 1_699_999_999_000,
        "entry_ts_ms": 1_700_000_000_025,
        "entry_price": 0.127,
        "qty": 10.0,
        "notional_usdt": 1.27,
        "take_profit_price": 0.12,
    }
    record_ledger_rows(
        tmp_path,
        order_rows=[order],
        trade_rows=[trade],
        trade_dataset=trade_dataset,
        order_dataset=order_dataset,
        mode="demo",
        sleeve="continuous",
        now_ms=1_700_000_000_100,
    )
    projected_trade = read_dataset(tmp_path, trade_dataset).to_dicts()
    projected_order = read_dataset(tmp_path, order_dataset).to_dicts()
    assert projected_trade[0]["trade_id"] == "trade-1"
    assert projected_trade[0]["canonical_lifecycle_state"] == "protection_active"
    assert projected_order[0]["order_link_id"] == "lm-en-c-b-1"

    # Simulate an operator resetting/corrupting generated ledgers. Rebuild reads
    # only the immutable journal and restores identical row keys/state.
    shutil.rmtree(tmp_path / trade_dataset)
    shutil.rmtree(tmp_path / order_dataset)
    counts = rebuild_all_registered_projections(tmp_path)
    assert counts[trade_dataset] == 1
    assert counts[order_dataset] == 1
    assert read_dataset(tmp_path, trade_dataset).to_dicts() == projected_trade
    assert read_dataset(tmp_path, order_dataset).to_dicts() == projected_order


def test_first_journal_write_bootstraps_existing_ledger_before_rebuild(tmp_path) -> None:
    dataset = "continuous_fade_demo_trades"
    old = {
        "trade_id": "old",
        "strategy_id": "continuous_fade_v2",
        "symbol": "OLDUSDT",
        "side": "short",
        "status": "open",
        "signal_ts_ms": 1_699_000_000_000,
        "entry_ts_ms": 1_699_000_001_000,
        "entry_price": 1.0,
        "qty": 2.0,
    }
    write_dataset(pl.DataFrame([old]), tmp_path, dataset, partition_by=())
    new = {
        "trade_id": "new",
        "strategy_id": "continuous_fade_v2",
        "symbol": "NEWUSDT",
        "side": "short",
        "status": "open",
        "signal_ts_ms": 1_700_000_000_000,
        "entry_ts_ms": 1_700_000_001_000,
        "entry_price": 2.0,
        "qty": 3.0,
    }
    record_ledger_rows(
        tmp_path,
        trade_rows=[new],
        trade_dataset=dataset,
        mode="demo",
        sleeve="continuous",
        now_ms=1_700_000_001_000,
    )
    rows = read_dataset(tmp_path, dataset).sort("trade_id").to_dicts()
    assert [row["trade_id"] for row in rows] == ["new", "old"]


def test_verified_flat_snapshot_stops_open_projection_without_fabricating_pnl(tmp_path) -> None:
    dataset = "continuous_fade_demo_trades"
    record_ledger_rows(
        tmp_path,
        trade_rows=[
            {
                "trade_id": "ghost",
                "strategy_id": "continuous_fade_v2",
                "symbol": "BUSDT",
                "side": "short",
                "status": "open",
                "entry_ts_ms": 1_700_000_000_000,
                "entry_price": 0.125,
                "qty": 10.0,
            }
        ],
        trade_dataset=dataset,
        mode="demo",
        sleeve="continuous",
        now_ms=1_700_000_000_000,
    )
    record_verified_flat_snapshot(
        tmp_path,
        now_ms=1_700_000_060_000,
        verification_id="flat-check-1",
        source="bybit_positions_and_open_orders",
    )
    rebuild_all_registered_projections(tmp_path)
    row = read_dataset(tmp_path, dataset).to_dicts()[0]
    assert row["status"] == "awaiting_pnl"
    assert row["canonical_reconciliation_state"] == "venue_flat_awaiting_pnl"
    state = replay_journal(tmp_path).trades["ghost"]
    assert state.lifecycle_state == "protection_active"
    assert state.closed_qty == 0.0
    assert not any(event.event_type == "close_fill" for event in read_journal(tmp_path))


def test_archived_paper_epoch_reset_is_not_a_venue_flat_fact(tmp_path) -> None:
    dataset = "continuous_fade_paper_trades"
    record_ledger_rows(
        tmp_path,
        trade_rows=[
            {
                "trade_id": "paper-open",
                "strategy_id": "continuous_fade_v2",
                "symbol": "BUSDT",
                "side": "short",
                "status": "open",
                "entry_ts_ms": 1_700_000_000_000,
                "entry_price": 0.125,
                "qty": 10.0,
            }
        ],
        trade_dataset=dataset,
        mode="paper",
        sleeve="continuous",
        now_ms=1_700_000_000_000,
    )

    record_archived_paper_epoch_reset(
        tmp_path,
        now_ms=1_700_000_060_000,
        reset_id="paper-reset-1",
        source="guarded_account_epoch_archive",
    )
    rebuild_all_registered_projections(tmp_path)

    row = read_dataset(tmp_path, dataset).to_dicts()[0]
    assert row["status"] == "archived"
    assert row["canonical_reconciliation_state"] == "paper_epoch_archived"
    assert "canonical_flat_verified_at_ms" not in row
    state = replay_journal(tmp_path).trades["paper-open"]
    assert state.lifecycle_state == "protection_active"
    assert state.closed_qty == 0.0
    reset_event = read_journal(tmp_path)[-1]
    assert reset_event.event_type == "projection_patch"
    assert reset_event.metadata["venue_flat_verified"] is False
    assert not any(event.event_type == "venue_snapshot" for event in read_journal(tmp_path))


def test_tca_records_fill_inputs_and_due_markouts(tmp_path) -> None:
    for index, event_type in enumerate(LIFECYCLE_SEQUENCE[:5]):
        append_events(tmp_path, [_spec(event_type, sequence_index=index)])
    tca = replay_journal(tmp_path).tca_rows()
    assert tca == [
        {
            "fill_event_id": tca[0]["fill_event_id"],
            "sequence": 5,
            "event_type": "fill",
            "mode": "demo",
            "sleeve": "continuous",
            "strategy_id": "s1",
            "trade_id": "t1",
            "symbol": "BUSDT",
            "side": "short",
            "order_link_id": "o1",
            "local_ts_ms": 1_700_000_000_004,
            "venue_ts_ms": 1_700_000_000_004,
            "order_version": 1,
            "position_version": 1,
            "exec_id": "",
            "qty": 1.0,
            "decision_price": 0.125,
            "decision_price_source": "",
            "submission_price": 0.126,
            "submission_price_source": "",
            "fill_price": 0.127,
            "depth_consumed_quote": 0.127,
            "depth_source": "",
            "latency_ms": 25.0,
            "latency_source": "",
            "fill_fee_usdt": None,
            "markout_1m_price": None,
            "markout_1m_bps": None,
            "markout_1m_status": "pending",
            "markout_5m_price": None,
            "markout_5m_bps": None,
            "markout_5m_status": "pending",
            "markout_30m_price": None,
            "markout_30m_bps": None,
            "markout_30m_status": "pending",
        }
    ]
    record_due_markouts(
        tmp_path,
        now_ms=1_700_000_000_004 + 30 * 60_000,
        prices={"BUSDT": 0.12},
    )
    updated = replay_journal(tmp_path).tca_rows()[0]
    assert updated["markout_1m_price"] == 0.12
    assert updated["markout_5m_price"] == 0.12
    assert updated["markout_30m_price"] == 0.12
    assert updated["markout_1m_bps"] > 0.0  # lower is favorable for a short
    assert updated["markout_1m_status"] == "observed"


def test_each_venue_execution_becomes_its_own_fill_and_tca_row(tmp_path) -> None:
    # The lifecycle bridge accepts one canonical detail per venue execution;
    # constructing that public row contract directly avoids retaining the
    # deleted direct-execution summarizer merely as a test helper.
    summary = {
        "avg_price": (0.125 + 2 * 0.126) / 3,
        "exec_time_ms": 1_700_000_000_020,
        "fill_details": [
            {
                "exec_id": "exec-a",
                "qty": 1.0,
                "price": 0.125,
                "value": 0.125,
                "fee": 0.0001,
                "venue_ts_ms": 1_700_000_000_010,
            },
            {
                "exec_id": "exec-b",
                "qty": 2.0,
                "price": 0.126,
                "value": 0.252,
                "fee": 0.0002,
                "venue_ts_ms": 1_700_000_000_020,
            },
        ],
    }
    order = {
        "trade_id": "multi-fill",
        "strategy_id": "continuous_fade_v2",
        "symbol": "BUSDT",
        "trade_side": "short",
        "side": "Sell",
        "status": "filled",
        "submit_mode": "submitted",
        "order_link_id": "multi-order",
        "order_id": "venue-order",
        "ts_ms": 1_700_000_000_000,
        "exec_time_ms": summary["exec_time_ms"],
        "qty": 3.0,
        "filled_qty": 3.0,
        "avg_price": summary["avg_price"],
        "canonical_fill_details": summary["fill_details"],
        "reduce_only": False,
    }
    trade = {
        "trade_id": "multi-fill",
        "strategy_id": "continuous_fade_v2",
        "symbol": "BUSDT",
        "side": "short",
        "status": "open",
        "entry_ts_ms": 1_700_000_000_020,
        "entry_price": summary["avg_price"],
        "qty": 3.0,
    }
    record_ledger_rows(
        tmp_path,
        order_rows=[order],
        trade_rows=[trade],
        trade_dataset="continuous_fade_demo_trades",
        order_dataset="continuous_fade_demo_orders",
        mode="demo",
        sleeve="continuous",
        now_ms=1_700_000_000_100,
    )
    tca = replay_journal(tmp_path).tca_rows()
    assert [(row["exec_id"], row["qty"], row["fill_price"]) for row in tca] == [
        ("exec-a", 1.0, 0.125),
        ("exec-b", 2.0, 0.126),
    ]
    assert [row["fill_fee_usdt"] for row in tca] == [0.0001, 0.0002]
    assert all(row["decision_price"] is not None for row in tca)
    assert all(row["submission_price"] is not None for row in tca)
    state = replay_journal(tmp_path).trades["multi-fill"]
    assert state.entry_filled_qty == 3.0
    projected_order = read_dataset(tmp_path, "continuous_fade_demo_orders").to_dicts()[0]
    assert "canonical_fill_details" not in projected_order


def test_cumulative_partial_close_rows_journal_only_fill_deltas(tmp_path) -> None:
    trade_dataset = "continuous_fade_demo_trades"
    order_dataset = "continuous_fade_demo_orders"
    record_ledger_rows(
        tmp_path,
        trade_rows=[{
            "trade_id": "partial-close",
            "strategy_id": "continuous_fade_v2",
            "symbol": "BUSDT",
            "side": "short",
            "status": "open",
            "entry_ts_ms": 1_700_000_000_000,
            "entry_price": 1.0,
            "qty": 1.0,
        }],
        trade_dataset=trade_dataset,
        order_dataset=order_dataset,
        mode="demo",
        sleeve="continuous",
        now_ms=1_700_000_000_000,
    )
    base_order = {
        "trade_id": "partial-close",
        "strategy_id": "continuous_fade_v2",
        "symbol": "BUSDT",
        "trade_side": "short",
        "side": "Buy",
        "submit_mode": "submitted",
        "order_link_id": "partial-exit",
        "order_id": "venue-exit",
        "ts_ms": 1_700_000_001_000,
        "exec_time_ms": 1_700_000_001_010,
        "qty": 1.0,
        "avg_price": 0.9,
        "reduce_only": True,
    }
    record_ledger_rows(
        tmp_path,
        order_rows=[{**base_order, "status": "partial", "filled_qty": 0.4}],
        trade_dataset=trade_dataset,
        order_dataset=order_dataset,
        mode="demo",
        sleeve="continuous",
        now_ms=1_700_000_001_020,
    )
    record_ledger_rows(
        tmp_path,
        order_rows=[{**base_order, "status": "filled", "filled_qty": 1.0, "exec_time_ms": 1_700_000_001_030}],
        trade_dataset=trade_dataset,
        order_dataset=order_dataset,
        mode="demo",
        sleeve="continuous",
        now_ms=1_700_000_001_040,
    )
    state = replay_journal(tmp_path).trades["partial-close"]
    close_rows = [row for row in replay_journal(tmp_path).tca_rows() if row["event_type"] == "close_fill"]
    assert [row["qty"] for row in close_rows] == [0.4, 0.6]
    assert state.closed_qty == pytest.approx(1.0)


@pytest.mark.parametrize("mode", ["historical", "paper", "demo"])
def test_all_modes_use_the_same_lifecycle_reducer(tmp_path, mode: str) -> None:
    root = tmp_path / mode
    specs = [
        _spec(event_type, trade_id=f"{mode}-1", mode=mode, sequence_index=index)
        for index, event_type in enumerate(LIFECYCLE_SEQUENCE)
    ]
    append_events(root, specs)
    state = replay_journal(root).trades[f"{mode}-1"]
    assert state.lifecycle_state == "pnl_confirmed"
    assert state.lifecycle_index == len(LIFECYCLE_SEQUENCE) - 1
    assert state.closed_qty == 1.0
