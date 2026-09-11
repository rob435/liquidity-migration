//! Rotation: numbered segments, the trusted-segment rule, and what boot
//! recovers after a crash at any byte of a rotation.

use std::fs;
use std::path::PathBuf;

use engine_types::wal::AnchorState;
use engine_wal::{
    open_current, replay, replay_chain, replay_chain_visit, replay_current, segments, Wal,
    WalRecord, WalWriter,
};
use tempfile::TempDir;

fn log_path(dir: &TempDir) -> PathBuf {
    dir.path().join("engine.wal")
}

fn note(text: &str) -> WalRecord {
    WalRecord::Note {
        source: "test".to_string(),
        text: text.to_string(),
    }
}

/// A restatement with something recognizable in it, so a test can tell a
/// replayed base from a replayed record.
fn base(mark: &str) -> WalRecord {
    WalRecord::SegmentBase {
        order_id_epoch_ms: None,
        open_trade_lots: Some(Vec::new()),
        legacy_signal_source_retirements: Vec::new(),
        portfolio_control: Default::default(),
        pending_order_dispatches: Vec::new(),
        signal_producers: Vec::new(),
        identities: None,
        instrument_catalog: None,
        signal_suspensions: Vec::new(),
        portfolio: Some(Default::default()),
        strategy_processes: Vec::new(),
        strategy_callback_queues: Vec::new(),
        strategy_callback_sources: Vec::new(),
        signal_callback_deliveries: Vec::new(),
        strategy_callbacks: Vec::new(),
        wall_ts_ms: 7,
        strategies: vec!["carry".to_string()],
        symbols: vec!["BTCUSDT".to_string()],
        may_open: true,
        control_anchors: vec![AnchorState {
            source: "risk".to_string(),
            state: mark.to_string(),
        }],
        attribution: Vec::new(),
        logged_exposure: Vec::new(),
        intended_stops: Vec::new(),
        recent_execution_ids: Vec::new(),
        execution_history_through_ms: Some(7),
        target_book_latches: Vec::new(),
        strategy_checkpoints: Vec::new(),
        strategy_global_checkpoints: Vec::new(),
        strategy_events: Vec::new(),
        signal_observations: Vec::new(),
        signal_cursors: Vec::new(),
        signal_subscriptions: Vec::new(),
        signal_gaps: Vec::new(),
        strategy_effects: Default::default(),
        runtime_control_requests: Vec::new(),
        runtime_control_consumed: Vec::new(),
        open_orders: Vec::new(),
        rolling_loss_rows: Vec::new(),
        owed_markouts: Vec::new(),
    }
}

/// A family with three records in segment 1, a rotation, and two records in
/// segment 2. Returns the family path.
fn rotated_family(dir: &TempDir) -> PathBuf {
    let path = log_path(dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    for text in ["one", "two", "three"] {
        wal.append(&note(text)).unwrap();
    }
    wal.barrier().unwrap();
    assert!(
        wal.rotate(&base("rotated")).unwrap(),
        "a file-backed log rotates"
    );
    wal.append(&note("four")).unwrap();
    wal.append(&note("five")).unwrap();
    wal.barrier().unwrap();
    path
}

fn texts(records: &[(u64, WalRecord)]) -> Vec<String> {
    records
        .iter()
        .filter_map(|(_, record)| match record {
            WalRecord::Note { text, .. } => Some(text.clone()),
            _ => None,
        })
        .collect()
}

#[test]
fn rotation_starts_a_numbered_segment_and_archives_the_old_one_untouched() {
    let dir = TempDir::new().unwrap();
    let family = rotated_family(&dir);
    let second = PathBuf::from(format!("{}.000002", family.display()));
    assert!(
        second.exists(),
        "the next segment is numbered, in the same directory"
    );

    // The old segment is an archive: still there, still exactly the records
    // it held when rotation happened.
    let old = replay(&family).unwrap();
    assert_eq!(texts(&old), ["one", "two", "three"]);

    // The new segment begins with the restatement, then its own records.
    let new = replay(&second).unwrap();
    assert!(
        matches!(new.first(), Some((1, WalRecord::SegmentBase { .. }))),
        "a segment after the first begins with the restatement"
    );
    assert_eq!(texts(&new), ["four", "five"]);

    let chain = segments(&family).unwrap();
    let indexes: Vec<u64> = chain.iter().map(|(index, _)| *index).collect();
    assert_eq!(indexes, [1, 2]);
}

#[test]
fn open_current_picks_the_newest_trusted_segment_and_appends_there() {
    let dir = TempDir::new().unwrap();
    let family = rotated_family(&dir);
    let (mut wal, replayed) = open_current(&family).unwrap();
    assert!(
        matches!(replayed.first(), Some((_, WalRecord::SegmentBase { .. }))),
        "boot replays the restatement, not the whole history"
    );
    assert_eq!(texts(&replayed), ["four", "five"]);

    wal.append(&note("six")).unwrap();
    wal.barrier().unwrap();
    drop(wal);
    let second = PathBuf::from(format!("{}.000002", family.display()));
    assert_eq!(
        texts(&replay(&second).unwrap()),
        ["four", "five", "six"],
        "appends land in the segment boot picked"
    );
    assert_eq!(
        texts(&replay(&family).unwrap()),
        ["one", "two", "three"],
        "the archive never moves again"
    );
}

#[test]
fn replay_current_reads_only_what_boot_would_replay() {
    let dir = TempDir::new().unwrap();
    let family = rotated_family(&dir);
    let (records, damaged) = replay_current(&family).unwrap();
    assert!(!damaged);
    assert!(
        matches!(records.first(), Some((_, WalRecord::SegmentBase { .. }))),
        "the newest trusted segment, restatement first"
    );
    assert_eq!(texts(&records), ["four", "five"]);
    let (_, replayed) = open_current(&family).unwrap();
    assert_eq!(
        texts(&records),
        texts(&replayed),
        "read-only, and the same records boot gets"
    );
    let (chained, _) = replay_chain(&family).unwrap();
    assert_eq!(texts(&chained), ["one", "two", "three", "four", "five"]);
}

#[test]
fn replay_chain_reads_the_whole_family_in_order() {
    let dir = TempDir::new().unwrap();
    let family = rotated_family(&dir);
    let (records, damaged) = replay_chain(&family).unwrap();
    assert!(!damaged);
    assert_eq!(texts(&records), ["one", "two", "three", "four", "five"]);
    // Renumbered consecutively across the seam, restatement included.
    let seqs: Vec<u64> = records.iter().map(|(seq, _)| *seq).collect();
    assert_eq!(seqs, [1, 2, 3, 4, 5, 6]);
}

#[test]
fn a_family_of_one_reads_exactly_like_a_plain_file() {
    // Backward compatibility: every log written before rotation existed is a
    // family of one, and both the boot open and the chain read must treat it
    // exactly as before.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("only")).unwrap();
    wal.barrier().unwrap();
    drop(wal);

    let (_, replayed) = open_current(&path).unwrap();
    assert_eq!(texts(&replayed), ["only"]);
    let (chained, damaged) = replay_chain(&path).unwrap();
    assert_eq!(texts(&chained), ["only"]);
    assert!(!damaged);
}

fn write_raw_record(path: &std::path::Path, value: &serde_json::Value) -> Vec<u8> {
    let payload = serde_json::to_vec(value).unwrap();
    let mut bytes = b"EWAL0001".to_vec();
    bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
    bytes.extend_from_slice(&payload);
    fs::write(path, &bytes).unwrap();
    bytes
}

#[test]
fn versioned_rotation_requires_gap_state_but_legacy_rotation_still_reads() {
    for kind in ["segment_base", "segment_base_v7"] {
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        let mut value = serde_json::to_value(base("required-gap-state")).unwrap();
        value["kind"] = kind.into();
        value.as_object_mut().unwrap().remove("signal_gaps");
        let bytes = write_raw_record(&path, &value);
        let result = WalWriter::open(&path);
        if kind == "segment_base_v7" {
            assert!(matches!(result, Err(engine_wal::WalError::Corrupt { .. })));
        } else {
            let (_, records) = result.unwrap();
            assert!(
                matches!(&records[0].1, WalRecord::SegmentBase { signal_gaps, .. } if signal_gaps.is_empty())
            );
        }
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}

#[test]
fn gap_record_and_rotation_keep_the_exact_missing_prefix() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let gap = engine_types::SignalGap {
        source: "worker.g1".into(),
        destination: engine_types::StrategyId(0),
        next_sequence: 10,
        observed_sequence: 11,
    };
    let record = WalRecord::SignalGapRecorded {
        wall_ts_ms: 10,
        gap: gap.clone(),
    };
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&record).unwrap();
    wal.barrier().unwrap();
    assert_eq!(replay(&path).unwrap()[0].1, record);
    let mut rotated = base("gap");
    let WalRecord::SegmentBase { signal_gaps, .. } = &mut rotated else {
        panic!()
    };
    signal_gaps.push(gap);
    wal.rotate(&rotated).unwrap();
    drop(wal);
    let (_, records) = open_current(&path).unwrap();
    assert_eq!(records[0].1, rotated);
    let bytes = fs::read(dir.path().join("engine.wal.000002")).unwrap();
    let payload: serde_json::Value = serde_json::from_slice(&bytes[16..]).unwrap();
    assert_eq!(payload["kind"], "segment_base_v7");
}

/// The crash test: a rotation cut off at ANY byte leaves boot replaying the
/// old segment with nothing invented and nothing lost — or, once the
/// restatement is complete on disk, replaying the new segment, which says
/// the same thing.
#[test]
fn a_rotation_truncated_at_any_byte_falls_back_cleanly() {
    let template = TempDir::new().unwrap();
    let family = rotated_family(&template);
    let second_name = "engine.wal.000002";
    let full = fs::read(template.path().join(second_name)).unwrap();

    // The restatement is the first frame: 8 bytes of magic, 8 of frame
    // header, then the payload.
    let base_len = u32::from_le_bytes(full[8..12].try_into().unwrap()) as u64;
    let base_end = 8 + 8 + base_len;

    // Every interesting cut: nothing but the name, half the magic, half the
    // frame header, several points inside the payload, one byte short.
    let cuts = [0, 4, 8, 12, 16, base_end / 2, base_end - 1];
    for cut in cuts {
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        fs::copy(&family, &path).unwrap();
        let torn = dir.path().join(second_name);
        fs::write(&torn, &full[..cut as usize]).unwrap();

        let (_, replayed) = open_current(&path).unwrap();
        assert_eq!(
            texts(&replayed),
            ["one", "two", "three"],
            "cut at byte {cut}: boot must fall back to the old segment"
        );
        assert!(
            !replayed
                .iter()
                .any(|(_, r)| matches!(r, WalRecord::SegmentBase { .. })),
            "cut at byte {cut}: a torn restatement must not replay at all"
        );

        // The chain read sees the same history: the torn leftover holds no
        // records and is skipped without flagging damage.
        let (chained, damaged) = replay_chain(&path).unwrap();
        assert_eq!(
            texts(&chained),
            ["one", "two", "three"],
            "cut at byte {cut}"
        );
        assert!(
            !damaged,
            "cut at byte {cut}: an abandoned rotation is not damage"
        );
    }

    // And the moment the restatement is whole, the new segment is trusted —
    // which recovers the same state, because the base restates the old
    // segment in full and nothing was appended after it.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    fs::copy(&family, &path).unwrap();
    fs::write(dir.path().join(second_name), &full[..base_end as usize]).unwrap();
    let (_, replayed) = open_current(&path).unwrap();
    assert!(
        matches!(replayed.as_slice(), [(1, WalRecord::SegmentBase { .. })]),
        "a complete restatement with no tail is a trusted segment"
    );
}

#[test]
fn a_torn_leftover_is_never_reused_and_the_next_rotation_skips_its_number() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("one")).unwrap();
    wal.barrier().unwrap();

    // A rotation that died half-written: the numbered file exists, its
    // restatement does not parse.
    let torn = dir.path().join("engine.wal.000002");
    fs::write(&torn, b"EWAL0001\x99\x99").unwrap();
    let torn_bytes = fs::read(&torn).unwrap();

    // Boot ignores it...
    drop(wal);
    let (mut wal, replayed) = open_current(&path).unwrap();
    assert_eq!(texts(&replayed), ["one"]);

    // ...and the next rotation writes PAST it rather than appending a fresh
    // restatement after half an old one.
    assert!(wal.rotate(&base("second try")).unwrap());
    wal.append(&note("two")).unwrap();
    wal.barrier().unwrap();
    drop(wal);
    assert!(dir.path().join("engine.wal.000003").exists());
    assert_eq!(
        fs::read(&torn).unwrap(),
        torn_bytes,
        "the leftover is evidence and is not written to"
    );

    let (_, replayed) = open_current(&path).unwrap();
    assert_eq!(
        texts(&replayed),
        ["two"],
        "boot picks the finished rotation"
    );
    let (chained, damaged) = replay_chain(&path).unwrap();
    assert_eq!(texts(&chained), ["one", "two"]);
    assert!(!damaged);
}

#[test]
fn segment_size_counts_the_file_and_the_buffer() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    let empty = wal.segment_size();
    assert_eq!(empty, 8, "a fresh segment is its magic header");
    wal.append(&note("buffered, not yet pushed")).unwrap();
    assert!(
        wal.segment_size() > empty,
        "buffered bytes count toward the threshold"
    );
    let before_flush = wal.segment_size();
    wal.flush().unwrap();
    assert_eq!(
        wal.segment_size(),
        before_flush,
        "flushing moves bytes to the OS without changing what the segment holds"
    );
    // After a rotation the count restarts at the new segment's size.
    wal.rotate(&base("fresh")).unwrap();
    let after = wal.segment_size();
    assert_eq!(
        fs::metadata(dir.path().join("engine.wal.000002"))
            .unwrap()
            .len(),
        after,
        "the count is the new segment's bytes, nothing carried over"
    );
}

#[test]
fn the_lock_on_the_family_path_survives_a_rotation() {
    // The single-writer claim lives on the configured path, not on whichever
    // segment is current, so there is no window during or after a rotation
    // where a second engine could claim the directory.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let held = engine_wal::lock(&path).expect("first claim");
    let (mut wal, _) = open_current(&path).unwrap();
    wal.append(&note("one")).unwrap();
    wal.rotate(&base("rotated")).unwrap();
    assert!(
        matches!(
            engine_wal::lock(&path),
            Err(engine_wal::WalLockError::AlreadyHeld { .. })
        ),
        "the family lock still refuses a second writer after rotation"
    );
    drop(held);
}

#[test]
fn effect_rotation_requires_all_mandatory_state_without_truncating() {
    for missing in [
        "strategy_effects",
        "signal_gaps",
        "portfolio",
        "signal_producers",
        "strategy_processes",
        "strategy_callbacks",
        "identities",
        "portfolio_control",
        "instrument_catalog",
        "strategy_callback_queues",
        "strategy_callback_sources",
        "signal_callback_deliveries",
    ] {
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        let mut value = serde_json::to_value(base("required-effects")).unwrap();
        assert_eq!(value["kind"], "segment_base_v7");
        value.as_object_mut().unwrap().remove(missing);
        let bytes = write_raw_record(&path, &value);
        assert!(
            matches!(
                WalWriter::open(&path),
                Err(engine_wal::WalError::Corrupt { .. })
            ),
            "{missing}"
        );
        assert_eq!(
            fs::read(&path).unwrap(),
            bytes,
            "{missing} must not be repaired as a torn tail"
        );
    }
    for kind in ["segment_base", "segment_base_v7"] {
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        let mut value = serde_json::to_value(base("legacy-effects")).unwrap();
        value["kind"] = kind.into();
        value.as_object_mut().unwrap().remove("strategy_effects");
        let bytes = write_raw_record(&path, &value);
        if kind == "segment_base_v7" {
            assert!(WalWriter::open(&path).is_err());
        } else {
            let (_, rows) = WalWriter::open(&path).unwrap();
            assert!(
                matches!(&rows[0].1, WalRecord::SegmentBase { strategy_effects, .. } if strategy_effects.transitions.is_empty())
            );
        }
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}

#[test]
fn durable_effect_identity_and_suffix_survive_rotation() {
    use engine_types::{Action, StrategyEffectsState, StrategyId, StrategyTransitionState};
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let transition = StrategyTransitionState {
        origin: engine_types::wal::StrategyTransitionOrigin::Embedded,
        id: 17,
        strategy: StrategyId(0),
        effects: vec![Action::Cancel {
            symbol: engine_types::SymbolId(0),
            client_order_id: "order-a".into(),
        }],
        order_ids: vec![None],
        completed: vec![],
    };
    let queued = WalRecord::StrategyTransitionQueued {
        transition: transition.clone(),
    };
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&queued).unwrap();
    wal.barrier().unwrap();
    let mut rotated = base("pending-effect");
    let WalRecord::SegmentBase {
        strategy_effects, ..
    } = &mut rotated
    else {
        unreachable!()
    };
    *strategy_effects = StrategyEffectsState {
        next_transition_id: 18,
        transitions: vec![transition.clone()],
    };
    wal.rotate(&rotated).unwrap();
    wal.append(&WalRecord::StrategyEffectCompleted {
        transition_id: 17,
        effect_index: 0,
    })
    .unwrap();
    wal.barrier().unwrap();
    drop(wal);
    let (_, current) = open_current(&path).unwrap();
    let WalRecord::SegmentBase {
        strategy_effects, ..
    } = &current[0].1
    else {
        panic!("rotation missing")
    };
    assert_eq!(strategy_effects.next_transition_id, 18);
    assert_eq!(strategy_effects.transitions, [transition]);
    assert!(matches!(
        &current[1].1,
        WalRecord::StrategyEffectCompleted {
            transition_id: 17,
            effect_index: 0
        }
    ));
    let (chain, damaged) = replay_chain(&path).unwrap();
    assert!(!damaged);
    assert!(chain.iter().any(|(_, record)| record == &queued));
}

#[test]
fn v5_open_orders_require_typed_fill_progress_without_truncating() {
    use engine_types::{OrderKind, OrderRequest, Side, StrategyId, SymbolId};
    let order = engine_types::wal::OpenOrderState {
        entry_work: None,
        request: OrderRequest {
            client_order_id: "fill-frontier".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            close_position: false,
            sleeve_effect: None,
            exact_terms: None,
        },
        wire_ns: 1,
        arrival_mid: 100.0,
        acked: true,
        filled_qty: 0.003,
        fill_quantity: Some(engine_types::wal::OrderFillQuantity::LegacyBinary64 {
            quantity: 0.003,
        }),
        reservation_low_px: 100.0,
        reservation_high_px: 100.0,
        exact_price_range: None,
        terminal: None,
    };
    let mut snapshot = base("fill-progress");
    let WalRecord::SegmentBase { open_orders, .. } = &mut snapshot else {
        unreachable!()
    };
    open_orders.push(order);
    let complete = serde_json::to_value(snapshot).unwrap();
    assert_eq!(complete["kind"], "segment_base_v7");
    for null in [false, true] {
        let mut value = complete.clone();
        if null {
            value["open_orders"][0]["fill_quantity"] = serde_json::Value::Null;
        } else {
            value["open_orders"][0]
                .as_object_mut()
                .unwrap()
                .remove("fill_quantity");
        }
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        let bytes = write_raw_record(&path, &value);
        assert!(
            matches!(
                WalWriter::open(&path),
                Err(engine_wal::WalError::Corrupt { .. })
            ),
            "current snapshot silently lost its typed fill frontier"
        );
        assert_eq!(fs::read(&path).unwrap(), bytes);
        value["kind"] = "segment_base".into();
        let bytes = write_raw_record(&path, &value);
        let (_, rows) = WalWriter::open(&path).unwrap();
        let WalRecord::SegmentBase { open_orders, .. } = &rows[0].1 else {
            unreachable!()
        };
        assert!(open_orders[0].fill_quantity.is_none());
        assert_eq!(open_orders[0].filled_qty.to_bits(), 0.003_f64.to_bits());
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}

#[test]
fn v1_snapshots_keep_their_original_identity_migration_shape() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let mut value = serde_json::to_value(base("original-v1-shape")).unwrap();
    value["kind"] = "segment_base".into();
    value.as_object_mut().unwrap().remove("identities");
    value.as_object_mut().unwrap().remove("portfolio_control");
    for field in [
        "instrument_catalog",
        "strategy_callback_queues",
        "strategy_callback_sources",
        "signal_callback_deliveries",
    ] {
        value.as_object_mut().unwrap().remove(field);
    }
    let bytes = write_raw_record(&path, &value);
    let (_, rows) =
        WalWriter::open(&path).expect("a v1 snapshot retains its missing identity fields");
    assert!(matches!(
        &rows[0].1,
        WalRecord::SegmentBase {
            identities: None,
            ..
        }
    ));
    assert_eq!(fs::read(&path).unwrap(), bytes);
}

#[test]
fn callback_pages_read_legacy_arrays_and_refuse_damaged_archives_without_repair() {
    use engine_types::strategy_process::{
        CallbackEvent, CallbackPreparation, CallbackWalCursor, StrategyCallbackInput,
    };
    use engine_types::StrategyId;
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    let input = |id| StrategyCallbackInput {
        callback_id: id,
        strategy: StrategyId(0),
        order_origin: None,
        event: CallbackEvent::IntentRefused {
            symbol: engine_types::SymbolId(0),
            reduce_only: true,
            reason: format!("owned-{id}"),
        },
        preparation: CallbackPreparation::Queued,
    };
    let mut legacy = base("legacy-queue-array");
    let WalRecord::SegmentBase {
        strategy_callbacks, ..
    } = &mut legacy
    else {
        unreachable!()
    };
    *strategy_callbacks = vec![input(1), input(2)];
    wal.append(&legacy).unwrap();
    wal.barrier().unwrap();
    wal.rotate(&base("cursor-only")).unwrap();
    let mut reader = wal.callback_reader().unwrap().unwrap();
    let cursor = CallbackWalCursor {
        segment: 1,
        sequence: 1,
        offset: 0,
    };
    assert_eq!(reader.read_callback(cursor, 2).unwrap(), input(2));
    assert!(reader.read_callback(cursor, 3).is_err());
    let bytes = fs::read(&path).unwrap();
    let torn = &bytes[..bytes.len() - 2];
    fs::write(&path, torn).unwrap();
    assert!(
        reader.read_callback(cursor, 2).is_err(),
        "an incomplete archived callback was delivered"
    );
    assert_eq!(
        fs::read(&path).unwrap(),
        torn,
        "read-only callback paging repaired an archive"
    );
    let mut corrupt = bytes;
    corrupt[12] ^= 1;
    fs::write(&path, &corrupt).unwrap();
    assert!(
        reader.read_callback(cursor, 2).is_err(),
        "a checksum-corrupt callback was delivered"
    );
    assert_eq!(fs::read(&path).unwrap(), corrupt);
}

#[test]
fn exact_rotation_requires_cost_basis_but_preserves_legacy_v1_bytes() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let mut value = serde_json::to_value(base("exact-cost-basis")).unwrap();
    assert_eq!(value["kind"], "segment_base_v7");
    value.as_object_mut().unwrap().remove("open_trade_lots");
    let bytes = write_raw_record(&path, &value);
    assert!(
        WalWriter::open(&path).is_err(),
        "current rotation discarded trade cost basis"
    );
    assert_eq!(fs::read(&path).unwrap(), bytes);
    value["open_trade_lots"] = serde_json::Value::Null;
    let bytes = write_raw_record(&path, &value);
    assert!(
        WalWriter::open(&path).is_err(),
        "current rotation accepts legacy unknown cost basis"
    );
    assert_eq!(fs::read(&path).unwrap(), bytes);
    value.as_object_mut().unwrap().remove("open_trade_lots");
    value["kind"] = "segment_base".into();
    let bytes = write_raw_record(&path, &value);
    let (_, rows) = WalWriter::open(&path).unwrap();
    assert!(matches!(
        &rows[0].1,
        WalRecord::SegmentBase {
            open_trade_lots: None,
            ..
        }
    ));
    assert_eq!(fs::read(&path).unwrap(), bytes);
}

#[test]
fn retirement_rotation_requires_outcomes_but_preserves_legacy_v1_bytes() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let mut value = serde_json::to_value(base("retired-source")).unwrap();
    assert_eq!(value["kind"], "segment_base_v7");
    for missing in [false, true] {
        if missing {
            value
                .as_object_mut()
                .unwrap()
                .remove("legacy_signal_source_retirements");
        } else {
            value["legacy_signal_source_retirements"] = serde_json::Value::Null;
        }
        let bytes = write_raw_record(&path, &value);
        assert!(
            WalWriter::open(&path).is_err(),
            "current rotation forgot operator input loss"
        );
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
    value["kind"] = "segment_base".into();
    let bytes = write_raw_record(&path, &value);
    let (_, rows) = WalWriter::open(&path).unwrap();
    assert!(
        matches!(&rows[0].1, WalRecord::SegmentBase { legacy_signal_source_retirements, .. } if legacy_signal_source_retirements.is_empty())
    );
    assert_eq!(fs::read(&path).unwrap(), bytes);
}

fn tagged_payload(mut value: serde_json::Value, kind: &str, kind_last: bool) -> Vec<u8> {
    value.as_object_mut().unwrap().remove("kind");
    let fields = serde_json::to_string(&value).unwrap();
    let fields = &fields[1..fields.len() - 1];
    let tag = serde_json::to_string(kind).unwrap();
    if kind_last {
        format!("{{{fields},\"kind\":{tag}}}").into_bytes()
    } else {
        format!("{{\"kind\":{tag},{fields}}}").into_bytes()
    }
}

fn append_raw_frame(bytes: &mut Vec<u8>, payload: &[u8]) {
    bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    bytes.extend_from_slice(&crc32c::crc32c(payload).to_le_bytes());
    bytes.extend_from_slice(payload);
}

#[test]
fn removed_segment_versions_refuse_complete_frames_without_truncation_or_fallback() {
    for kind in [
        "segment_base_v2",
        "segment_base_v3",
        "segment_base_v4",
        "segment_base_v5",
        "segment_base_v6",
        "segment_base_v99",
    ] {
        for kind_last in [false, true] {
            for numbered in [false, true] {
                let dir = TempDir::new().unwrap();
                let family = log_path(&dir);
                let path = if numbered {
                    write_raw_record(&family, &serde_json::to_value(base("previous")).unwrap());
                    PathBuf::from(format!("{}.000002", family.display()))
                } else {
                    family.clone()
                };
                let payload = tagged_payload(
                    serde_json::to_value(base("unsupported")).unwrap(),
                    kind,
                    kind_last,
                );
                assert!(
                    serde_json::from_slice::<WalRecord>(&payload).is_err(),
                    "serde accepted {kind}"
                );
                let mut bytes = b"EWAL0001".to_vec();
                append_raw_frame(&mut bytes, &payload);
                fs::write(&path, &bytes).unwrap();
                let before_family = fs::read(&family).unwrap();
                assert!(WalWriter::open(&path).is_err(), "open accepted {kind}");
                assert!(
                    engine_wal::replay_scan(&path).is_err(),
                    "scan accepted {kind}"
                );
                assert!(
                    replay_current(&family).is_err(),
                    "current replay skipped {kind}"
                );
                assert!(
                    open_current(&family).is_err(),
                    "current open skipped {kind}"
                );
                assert!(replay_chain(&family).is_err(), "chain skipped {kind}");
                assert_eq!(fs::read(&path).unwrap(), bytes);
                assert_eq!(fs::read(&family).unwrap(), before_family);
            }
        }
    }
}

#[test]
fn streamed_callback_lineage_and_epoch_reads_refuse_removed_tags_without_a_match() {
    use engine_types::strategy_process::{
        CallbackEvent, CallbackPreparation, CallbackWalCursor, StrategyCallbackInput,
    };
    let input = StrategyCallbackInput {
        order_origin: None,
        callback_id: 77,
        strategy: engine_types::StrategyId(0),
        event: CallbackEvent::Timer {
            id: engine_types::TimerId(1),
            now_ns: 10,
        },
        preparation: CallbackPreparation::Queued,
    };
    for (kind, duplicate) in [
        ("segment_base_v2", false),
        ("segment_base_v3", false),
        ("segment_base_v4", false),
        ("segment_base_v5", false),
        ("segment_base_v6", false),
        ("segment_base_v99", false),
        ("segment_base_v5", true),
    ] {
        let expected_error = if duplicate {
            "duplicate field `kind`"
        } else {
            "unsupported WAL segment kind"
        };
        for kind_last in [false, true] {
            for first in [false, true] {
                let dir = TempDir::new().unwrap();
                let family = log_path(&dir);
                let (mut wal, _) = WalWriter::open(&family).unwrap();
                wal.append(&base("initial")).unwrap();
                wal.rotate(&base("archived")).unwrap();
                wal.rotate(&base("current")).unwrap();
                let path = PathBuf::from(format!("{}.000002", family.display()));
                let mut value = serde_json::to_value(base("unsupported")).unwrap();
                value["strategy_callbacks"] = serde_json::json!([input]);
                value["open_orders"] = serde_json::json!([{
                    "request": {
                        "client_order_id":"kept", "strategy":0, "symbol":0, "side":"Buy", "qty":1.0,
                        "kind":{"Limit":{"px":100.0,"tif":"Gtc"}}, "stop":null,
                        "reduce_only":false, "close_position":false
                    },
                    "wire_ns":1, "acked":true, "filled_qty":0.0,
                    "fill_quantity":{"kind":"legacy_binary64","quantity":0.0}
                }]);
                // Validate the fixture independently before assigning an unsupported tag.
                serde_json::from_value::<WalRecord>(value.clone()).unwrap();
                let mut payload = tagged_payload(value, kind, kind_last);
                if duplicate {
                    if kind_last {
                        drop(payload.splice(1..1, br#""kind":"segment_base_v7","#.iter().copied()));
                    } else {
                        payload.pop();
                        payload.extend_from_slice(br#", "kind":"segment_base_v7"}"#);
                    }
                }
                let mut bytes = b"EWAL0001".to_vec();
                if !first {
                    append_raw_frame(&mut bytes, &serde_json::to_vec(&base("preceding")).unwrap());
                }
                append_raw_frame(&mut bytes, &payload);
                fs::write(&path, &bytes).unwrap();
                let cursor = CallbackWalCursor {
                    segment: 2,
                    sequence: if first { 1 } else { 2 },
                    offset: 0,
                };
                let mut callbacks = wal.callback_reader().unwrap().unwrap();
                for wanted in [77, 78] {
                    let error = callbacks
                        .read_callback(cursor, wanted)
                        .unwrap_err()
                        .to_string();
                    assert!(error.contains(expected_error), "{kind}: {error}");
                }
                let error = callbacks
                    .next(cursor)
                    .err()
                    .expect("unsupported callback frame accepted")
                    .to_string();
                assert!(error.contains(expected_error), "{kind}: {error}");
                for wanted in ["kept", "absent"] {
                    let error = wal
                        .order_lineage_reader(wanted)
                        .unwrap()
                        .unwrap()
                        .next()
                        .unwrap_err()
                        .to_string();
                    assert!(error.contains(expected_error), "{kind}: {error}");
                }
                let error = wal
                    .order_epoch_reader()
                    .unwrap()
                    .unwrap()
                    .max_order_epoch_ms()
                    .unwrap_err()
                    .to_string();
                assert!(error.contains(expected_error), "{kind}: {error}");
                assert_eq!(fs::read(&path).unwrap(), bytes);
            }
        }
    }
}

/// A family with a torn tail on the newest trusted segment and an abandoned
/// rotation above it.
fn torn_and_abandoned_family(dir: &TempDir) -> PathBuf {
    let family = rotated_family(dir);
    let second = dir.path().join("engine.wal.000002");
    let whole = fs::read(&second).unwrap();
    fs::write(&second, &whole[..whole.len() - 2]).unwrap();
    fs::write(dir.path().join("engine.wal.000003"), b"EWAL0001\x99\x99").unwrap();
    family
}

#[test]
fn the_visitor_reads_the_family_exactly_as_the_collector_does() {
    let dir = TempDir::new().unwrap();
    let family = torn_and_abandoned_family(&dir);
    let (collected, damaged) = replay_chain(&family).unwrap();

    // Segment 1's three records, the restatement, and the one record the torn
    // tail left whole, renumbered across the seam. The abandoned rotation
    // above contributes nothing; the torn tail is the flag.
    assert_eq!(texts(&collected), ["one", "two", "three", "four"]);
    assert_eq!(
        collected.iter().map(|(seq, _)| *seq).collect::<Vec<_>>(),
        [1, 2, 3, 4, 5]
    );
    assert!(matches!(collected[3].1, WalRecord::SegmentBase { .. }));
    assert!(damaged);

    let mut visited = Vec::new();
    let streamed = replay_chain_visit(&family, |seq, record| {
        visited.push((seq, record));
        Ok(())
    })
    .unwrap();
    assert_eq!(visited, collected);
    assert_eq!(streamed, damaged);
}
