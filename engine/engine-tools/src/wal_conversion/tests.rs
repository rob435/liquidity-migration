use std::fs;
use std::path::Path;

use engine_core::execution::Fills;
use engine_types::numeric::{AssetId, Exact};
use engine_types::portfolio::{PortfolioPosition, PortfolioState};
use engine_types::strategy_process::{
    CallbackEvent, CallbackOrderOrigin, CallbackPreparation, CallbackQueueSlot, CallbackSnapshot,
    CallbackSourceFrontier, CallbackWalCursor, StrategyCallbackInput,
};
use engine_types::wal::RetainedWalRecord;
use engine_types::{
    OrderKind, OrderRequest, OrderUpdate, Side, StrategyId, SymbolId, Wal, WalRecord,
};
use serde_json::{json, Value};

use super::convert;

fn base() -> Value {
    let record: WalRecord = serde_json::from_value(json!({
        "kind": "segment_base", "wall_ts_ms": 50,
        "strategies": ["owner"], "symbols": ["BTCUSDT"], "may_open": false,
        "control_anchors": [], "attribution": [], "logged_exposure": [],
        "intended_stops": [], "open_orders": [], "open_trade_lots": [],
        "portfolio": PortfolioState::default(),
    }))
    .unwrap();
    serde_json::to_value(record).unwrap()
}

fn v5_holding() -> Value {
    let mut value = base();
    value["kind"] = "segment_base_v5".into();
    value.as_object_mut().unwrap().remove("open_trade_lots");
    value
        .as_object_mut()
        .unwrap()
        .remove("legacy_signal_source_retirements");
    value["portfolio"] = serde_json::to_value(PortfolioState {
        positions: vec![PortfolioPosition {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            signed_qty: Exact::parse_decimal("0.1000000000000000000000000001").unwrap(),
            entry_value: None,
            stop_px: Some(Exact::parse_decimal("123.0000000000000000001").unwrap()),
            settlement_asset: AssetId::Named("USDT".into()),
        }],
        ..PortfolioState::default()
    })
    .unwrap();
    value["attribution"] = json!([{"strategy": 0, "symbol": 0, "signed_qty": 0.1}]);
    value
}

fn write_segment(path: &Path, values: &[Value]) -> Vec<u64> {
    let mut bytes = b"EWAL0001".to_vec();
    let mut offsets = Vec::new();
    for value in values {
        let payload = serde_json::to_vec(value).unwrap();
        offsets.push(bytes.len() as u64);
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
    }
    fs::write(path, bytes).unwrap();
    offsets
}

fn records(path: &Path) -> Vec<WalRecord> {
    let (records, torn) = engine_wal::replay_chain(path).unwrap();
    assert!(!torn);
    records.into_iter().map(|(_, record)| record).collect()
}

fn expected_v5(mut value: Value) -> WalRecord {
    value["kind"] = "segment_base".into();
    serde_json::from_value(value).unwrap()
}

fn callback() -> StrategyCallbackInput {
    StrategyCallbackInput {
        order_origin: Some(CallbackOrderOrigin {
            segment: 1,
            sequence: 3,
        }),
        callback_id: 42,
        strategy: StrategyId(0),
        event: CallbackEvent::Timer {
            id: engine_types::TimerId(7),
            now_ns: 300,
        },
        preparation: CallbackPreparation::Queued,
    }
}

fn order() -> Value {
    let mut value = serde_json::to_value(WalRecord::OrderSent {
        dispatch: None,
        wire_ns: 100,
        arrival_mid: 100.0,
        request: OrderRequest {
            exact_terms: None,
            sleeve_effect: None,
            client_order_id: "kept".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            close_position: false,
        },
    })
    .unwrap();
    value["kind"] = "order_sent".into();
    value
}

#[test]
fn conversion_preserves_unknown_basis_exact_positions_and_retirement_outcomes() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("engine.wal");
    let mut value = v5_holding();
    let retirement = json!({"source":"legacy", "destination":0,
        "accepted_through":10, "published_through":12, "reason":"tail unavailable"});
    value["legacy_signal_source_retirements"] = json!([retirement]);
    value["signal_cursors"] = json!([{"source":"legacy", "sequence":10,
        "content_sha256":"a".repeat(64)}]);
    value["open_orders"] = json!([{
        "request":order()["request"], "wire_ns":100, "acked":true,
        "filled_qty":0.0, "fill_quantity":{"kind":"legacy_binary64", "quantity":0.0},
        "terminal":{"ending":"Cancelled", "retained_since_ms":1788702340615_i64}
    }]);
    let terminal = json!({"kind":"legacy_signal_source_retired", "wall_ts_ms":60,
        "retirement":retirement});
    let before = vec![
        expected_v5(value.clone()),
        serde_json::from_value(terminal.clone()).unwrap(),
    ];
    write_segment(&input, &[value, terminal]);
    let original = fs::read(&input).unwrap();
    let output = dir.path().join("converted");
    let converted = convert(&input, &output).unwrap();
    assert_eq!(
        (
            converted.segments,
            converted.records,
            converted.upgraded_bases
        ),
        (1, 2, 1)
    );
    let after = records(&converted.family);
    let old_lots = Fills::try_from_records(&before).unwrap().open_trade_lots();
    let new_lots = Fills::try_from_records(&after).unwrap().open_trade_lots();
    assert_eq!(new_lots, old_lots);
    assert_eq!(new_lots.len(), 1);
    assert!(!new_lots[0].priced);
    assert_eq!(
        new_lots[0].signed_qty,
        Exact::parse_decimal("0.1000000000000000000000000001").unwrap()
    );
    assert_eq!(new_lots[0].cash, Exact::zero());
    let mut expected = before;
    let WalRecord::SegmentBase {
        open_trade_lots, ..
    } = &mut expected[0]
    else {
        panic!()
    };
    *open_trade_lots = Some(new_lots);
    assert_eq!(after, expected);
    let old_orders = engine_core::inflight::LedgerOfOrders::try_from_records(&expected).unwrap();
    let new_orders = engine_core::inflight::LedgerOfOrders::try_from_records(&after).unwrap();
    assert_eq!(
        old_orders.orders["kept"].snapshot(60),
        new_orders.orders["kept"].snapshot(60)
    );
    assert_eq!(fs::read(&input).unwrap(), original);
    assert!(
        String::from_utf8_lossy(&fs::read(&converted.family).unwrap()).contains("segment_base_v7")
    );
    let repeated = convert(&converted.family, &dir.path().join("repeated")).unwrap();
    assert_eq!((repeated.upgraded_bases, repeated.relocated_bases), (0, 0));
    assert_eq!(
        fs::read(&converted.family).unwrap(),
        fs::read(&repeated.family).unwrap()
    );
}

#[test]
fn conversion_preserves_known_cost_basis_already_carried_by_a_v5_base() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("engine.wal");
    let mut value = v5_holding();
    let record = expected_v5(value.clone());
    let mut lots = Fills::try_from_records(&[record])
        .unwrap()
        .open_trade_lots();
    let lot = &mut lots[0];
    lot.priced = true;
    lot.in_qty = lot.signed_qty.clone();
    lot.in_value = Exact::parse_decimal("25.1234567890123456789").unwrap();
    lot.cash = -lot.in_value.clone();
    lot.fees = None;
    value["open_trade_lots"] = serde_json::to_value(&lots).unwrap();
    write_segment(&input, &[value]);
    let converted = convert(&input, &dir.path().join("converted")).unwrap();
    assert_eq!(
        Fills::try_from_records(&records(&converted.family))
            .unwrap()
            .open_trade_lots(),
        lots
    );
}

#[test]
fn conversion_relocates_callbacks_and_preserves_archived_order_lineage() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("engine.wal");
    let mut first = base();
    first["kind"] = "segment_base".into();
    let cancelled = serde_json::to_value(WalRecord::OrderUpdate {
        callbacks: Some(vec![StrategyId(0)]),
        update: OrderUpdate::Cancelled {
            client_order_id: "kept".into(),
            recv_ns: 200,
        },
    })
    .unwrap();
    let queued = callback();
    let offsets = write_segment(
        &input,
        &[
            first,
            order(),
            cancelled.clone(),
            serde_json::to_value(RetainedWalRecord::StrategyCallbackQueued {
                input: queued.clone(),
            })
            .unwrap(),
        ],
    );
    let mut prepared = queued.clone();
    prepared.preparation = CallbackPreparation::Prepared {
        snapshot: CallbackSnapshot {
            strategy: StrategyId(0),
            now_ns: 300,
            wall_ms: 5,
            entries_enabled: false,
            account: engine_types::StrategyAccountSummary {
                equity_usdt: 100.0,
                available_margin_usdt: 90.0,
                observed_ns: 10,
            },
            symbols: vec![],
            orders: vec![],
            global_checkpoint: None,
            strategy_names: vec!["owner".into()],
            strategy_events: vec![],
        },
    };
    let second = dir.path().join("engine.wal.000002");
    let offsets2 = write_segment(
        &second,
        &[
            v5_holding(),
            serde_json::to_value(RetainedWalRecord::StrategyCallbackPrepared {
                input: prepared.clone(),
            })
            .unwrap(),
            cancelled,
        ],
    );
    let mut third = base();
    third["strategy_callback_queues"] = serde_json::to_value(vec![CallbackQueueSlot {
        callback_id: 42,
        strategy: StrategyId(0),
        queued: CallbackWalCursor {
            segment: 1,
            sequence: 4,
            offset: offsets[3],
        },
        prepared: Some(CallbackWalCursor {
            segment: 2,
            sequence: 2,
            offset: offsets2[1],
        }),
        event_sha256: [7; 32],
    }])
    .unwrap();
    third["strategy_callback_sources"] = serde_json::to_value(vec![CallbackSourceFrontier {
        strategy: StrategyId(0),
        cursor: CallbackWalCursor {
            segment: 2,
            sequence: 4,
            offset: fs::metadata(&second).unwrap().len(),
        },
        accepted: None,
        latest: CallbackOrderOrigin {
            segment: 2,
            sequence: 3,
        },
    }])
    .unwrap();
    write_segment(&dir.path().join("engine.wal.000003"), &[third]);
    let original: Vec<_> = engine_wal::segments(&input)
        .unwrap()
        .iter()
        .map(|(_, path)| fs::read(path).unwrap())
        .collect();
    let converted = convert(&input, &dir.path().join("converted")).unwrap();
    assert_eq!(
        (
            converted.segments,
            converted.records,
            converted.upgraded_bases,
            converted.relocated_bases
        ),
        (3, 8, 1, 1)
    );
    let (mut new, latest) = engine_wal::open_current(&converted.family).unwrap();
    let WalRecord::SegmentBase {
        strategy_callback_queues,
        strategy_callback_sources,
        ..
    } = &latest[0].1
    else {
        panic!()
    };
    let slot = &strategy_callback_queues[0];
    assert_eq!(slot.queued.offset, 0);
    assert_eq!(slot.prepared.unwrap().offset, 0);
    let mut callbacks = new.callback_reader().unwrap().unwrap();
    assert_eq!(callbacks.read_callback(slot.queued, 42).unwrap(), queued);
    assert_eq!(
        callbacks.read_callback(slot.prepared.unwrap(), 42).unwrap(),
        prepared
    );
    let cursor = strategy_callback_sources[0].cursor;
    assert_eq!(cursor.offset, 0);
    let following_base = callbacks.next(cursor).unwrap().unwrap();
    assert_eq!(
        (
            following_base.cursor.segment,
            following_base.cursor.sequence
        ),
        (3, 1)
    );
    assert!(following_base.source.is_none());
    assert!(callbacks.next(following_base.next).unwrap().is_none());
    let source = callbacks
        .next(CallbackWalCursor {
            segment: 2,
            sequence: 3,
            offset: 0,
        })
        .unwrap()
        .unwrap()
        .source
        .unwrap();
    assert_eq!(source.0, vec![StrategyId(0)]);
    assert!(matches!(
        source.1,
        CallbackEvent::Order {
            update: OrderUpdate::Cancelled { .. }
        }
    ));
    let mut new_lineage = new.order_lineage_reader("kept").unwrap().unwrap();
    let cancellation = WalRecord::OrderUpdate {
        callbacks: Some(vec![StrategyId(0)]),
        update: OrderUpdate::Cancelled {
            client_order_id: "kept".into(),
            recv_ns: 200,
        },
    };
    for expected in [
        serde_json::from_value(order()).unwrap(),
        cancellation.clone(),
        cancellation,
    ] {
        assert_eq!(new_lineage.next().unwrap(), Some(expected));
    }
    assert_eq!(new_lineage.next().unwrap(), None);
    drop(new);
    for ((_, path), bytes) in engine_wal::segments(&input).unwrap().iter().zip(original) {
        assert_eq!(fs::read(path).unwrap(), bytes);
    }
    assert_eq!(
        fs::read(&converted.family).unwrap(),
        fs::read(&input).unwrap(),
        "unchanged segment must be byte-identical"
    );
}

#[test]
fn conversion_refuses_existing_or_locked_paths_without_changing_input() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("engine.wal");
    write_segment(&input, &[v5_holding()]);
    let original = fs::read(&input).unwrap();
    assert!(convert(&input, &input).is_err());
    assert!(convert(&input, dir.path()).is_err());
    assert_eq!(fs::read(&input).unwrap(), original);
    let claim = engine_wal::lock(&input).unwrap();
    let output = dir.path().join("converted");
    assert!(convert(&input, &output).is_err());
    assert!(!output.exists());
    assert_eq!(fs::read(&input).unwrap(), original);
    drop(claim);
    convert(&input, &output).unwrap();
    let prior_output = fs::read(output.join("engine.wal")).unwrap();
    assert!(convert(&input, &output).is_err());
    assert_eq!(fs::read(output.join("engine.wal")).unwrap(), prior_output);
}

#[test]
fn conversion_refuses_to_relabel_a_numbered_segment_as_a_complete_family() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("engine.wal.000026");
    write_segment(&input, &[v5_holding()]);
    let original = fs::read(&input).unwrap();
    let output = dir.path().join("converted");
    let result = convert(&input, &output);
    assert!(
        result.is_err(),
        "a quarantine suffix cannot become segment 1: {result:?}"
    );
    assert!(!output.exists());
    assert_eq!(fs::read(&input).unwrap(), original);
}

#[test]
fn conversion_keeps_legacy_attribution_without_a_portfolio_unpriced() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("engine.wal");
    let mut legacy = v5_holding();
    legacy["kind"] = "segment_base".into();
    legacy["portfolio"] = Value::Null;
    write_segment(&input, &[legacy]);
    let before = Fills::try_from_records(&records(&input))
        .unwrap()
        .open_trade_lots();
    assert_eq!(before.len(), 1);
    assert!(!before[0].priced);
    assert!(!before[0].exact_quantity);
    let converted = convert(&input, &dir.path().join("converted")).unwrap();
    assert_eq!(converted.upgraded_bases, 0);
    assert_eq!(
        fs::read(&input).unwrap(),
        fs::read(&converted.family).unwrap()
    );
    assert_eq!(
        Fills::try_from_records(&records(&converted.family))
            .unwrap()
            .open_trade_lots(),
        before
    );
}

#[test]
fn conversion_refuses_missing_or_mismatched_callback_sources() {
    for missing in [false, true] {
        let dir = tempfile::tempdir().unwrap();
        let input = dir.path().join("engine.wal");
        let mut value = v5_holding();
        value["strategy_callback_sources"] = serde_json::to_value(vec![CallbackSourceFrontier {
            strategy: StrategyId(0),
            cursor: CallbackWalCursor {
                segment: if missing { 9 } else { 1 },
                sequence: 1,
                offset: 17,
            },
            accepted: None,
            latest: CallbackOrderOrigin {
                segment: 1,
                sequence: 1,
            },
        }])
        .unwrap();
        write_segment(&input, &[value]);
        let original = fs::read(&input).unwrap();
        let output = dir.path().join("converted");
        let error = convert(&input, &output).unwrap_err().to_string();
        if !missing {
            assert!(error.contains("callback byte offset disagrees"), "{error}");
        }
        assert!(!output.exists());
        assert_eq!(fs::read(&input).unwrap(), original);
    }
}

#[test]
fn conversion_refuses_missing_torn_corrupt_untrusted_and_invalid_state() {
    for defect in [
        "missing",
        "torn",
        "checksum",
        "untrusted",
        "base",
        "lots",
        "null_portfolio",
        "missing_portfolio",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let input = dir.path().join("engine.wal");
        let mut value = v5_holding();
        if defect == "base" {
            value
                .as_object_mut()
                .unwrap()
                .remove("strategy_callback_sources");
        }
        if defect == "lots" {
            value["open_trade_lots"] = json!([]);
        }
        if defect == "null_portfolio" {
            value["portfolio"] = Value::Null;
        }
        if defect == "missing_portfolio" {
            value.as_object_mut().unwrap().remove("portfolio");
        }
        write_segment(&input, &[value]);
        match defect {
            "missing" => {
                write_segment(&dir.path().join("engine.wal.000003"), &[base()]);
            }
            "torn" => {
                let mut bytes = fs::read(&input).unwrap();
                bytes.pop();
                fs::write(&input, bytes).unwrap();
            }
            "checksum" => {
                let mut bytes = fs::read(&input).unwrap();
                bytes[12] ^= 1;
                fs::write(&input, bytes).unwrap();
            }
            "untrusted" => {
                write_segment(
                    &dir.path().join("engine.wal.000002"),
                    &[json!({"kind":"note", "source":"test", "text":"no base"})],
                );
            }
            _ => {}
        }
        let original: Vec<_> = engine_wal::segments(&input)
            .unwrap()
            .iter()
            .map(|(_, path)| fs::read(path).unwrap())
            .collect();
        let output = dir.path().join("converted");
        assert!(convert(&input, &output).is_err(), "accepted {defect}");
        assert!(!output.exists(), "partial output survived {defect}");
        for ((_, path), bytes) in engine_wal::segments(&input).unwrap().iter().zip(original) {
            assert_eq!(fs::read(path).unwrap(), bytes, "input changed for {defect}");
        }
    }
}

#[test]
fn conversion_reads_callback_inputs_embedded_in_original_v5_bases() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("engine.wal");
    let mut queued = callback();
    queued.order_origin = None;
    let mut original = v5_holding();
    original["strategy_callbacks"] = serde_json::to_value(vec![queued.clone()]).unwrap();
    write_segment(&input, &[original]);
    let mut head = base();
    head["strategy_callback_queues"] = serde_json::to_value(vec![CallbackQueueSlot {
        callback_id: queued.callback_id,
        strategy: queued.strategy,
        queued: CallbackWalCursor {
            segment: 1,
            sequence: 1,
            offset: 8,
        },
        prepared: None,
        event_sha256: [7; 32],
    }])
    .unwrap();
    write_segment(&dir.path().join("engine.wal.000002"), &[head]);
    let before = fs::read(&input).unwrap();
    assert!(engine_wal::replay_chain(&input).is_err());
    let converted = convert(&input, &dir.path().join("converted")).unwrap();
    let (mut wal, _) = engine_wal::open_current(&converted.family).unwrap();
    let mut reader = wal.callback_reader().unwrap().unwrap();
    assert_eq!(
        reader
            .read_callback(
                CallbackWalCursor {
                    segment: 1,
                    sequence: 1,
                    offset: 0
                },
                queued.callback_id
            )
            .unwrap(),
        queued
    );
    assert_eq!(fs::read(&input).unwrap(), before);
}

#[test]
fn conversion_keeps_v5_mandatory_fields_and_rejects_other_removed_versions() {
    for missing in [
        "strategy_callback_queues",
        "strategy_callback_sources",
        "signal_callback_deliveries",
        "identities",
        "instrument_catalog",
        "portfolio_control",
        "open_orders",
        "signal_producers",
        "signal_suspensions",
        "strategy_processes",
        "strategy_callbacks",
        "pending_order_dispatches",
        "portfolio",
        "strategy_effects",
        "signal_gaps",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let input = dir.path().join("engine.wal");
        let mut value = v5_holding();
        value.as_object_mut().unwrap().remove(missing);
        write_segment(&input, &[value]);
        let before = fs::read(&input).unwrap();
        let output = dir.path().join("converted");
        let error = convert(&input, &output).unwrap_err().to_string();
        assert!(error.contains(missing), "{missing}: {error}");
        assert!(!output.exists());
        assert_eq!(fs::read(&input).unwrap(), before);
    }
    for kind in [
        "segment_base_v2",
        "segment_base_v3",
        "segment_base_v4",
        "segment_base_v6",
        "segment_base_v99",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let input = dir.path().join("engine.wal");
        let mut value = v5_holding();
        value["kind"] = kind.into();
        write_segment(&input, &[value]);
        let before = fs::read(&input).unwrap();
        let output = dir.path().join("converted");
        let error = convert(&input, &output).unwrap_err().to_string();
        assert!(
            error.contains("unsupported WAL segment kind"),
            "{kind}: {error}"
        );
        assert!(!output.exists());
        assert_eq!(fs::read(&input).unwrap(), before);
    }
}

#[test]
fn conversion_refuses_duplicate_v5_fields_instead_of_overwriting_them() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("engine.wal");
    let value = serde_json::to_string(&v5_holding()).unwrap();
    let payload = format!("{{\"portfolio\":null,{}", &value[1..]);
    let mut before = b"EWAL0001".to_vec();
    before.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    before.extend_from_slice(&crc32c::crc32c(payload.as_bytes()).to_le_bytes());
    before.extend_from_slice(payload.as_bytes());
    fs::write(&input, &before).unwrap();
    let output = dir.path().join("converted");
    let error = convert(&input, &output).unwrap_err().to_string();
    assert!(error.contains("duplicate field"), "{error}");
    assert!(!output.exists());
    assert_eq!(fs::read(&input).unwrap(), before);
}
