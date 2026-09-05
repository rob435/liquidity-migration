//! Rotation: numbered segments, the trusted-segment rule, and what boot
//! recovers after a crash at any byte of a rotation.

use std::fs;
use std::path::PathBuf;

use engine_types::wal::AnchorState;
use engine_wal::{open_current, replay, replay_chain, segments, Wal, WalRecord, WalWriter};
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
    for kind in ["segment_base", "segment_base_v2"] {
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        let mut value = serde_json::to_value(base("required-gap-state")).unwrap();
        value["kind"] = kind.into();
        value.as_object_mut().unwrap().remove("signal_gaps");
        let bytes = write_raw_record(&path, &value);
        let result = WalWriter::open(&path);
        if kind == "segment_base_v2" {
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
    assert_eq!(payload["kind"], "segment_base_v3");
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
    for missing in ["strategy_effects", "signal_gaps"] {
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        let mut value = serde_json::to_value(base("required-effects")).unwrap();
        assert_eq!(value["kind"], "segment_base_v3");
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
    for kind in ["segment_base", "segment_base_v2"] {
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        let mut value = serde_json::to_value(base("legacy-effects")).unwrap();
        value["kind"] = kind.into();
        value.as_object_mut().unwrap().remove("strategy_effects");
        let bytes = write_raw_record(&path, &value);
        let (_, rows) = WalWriter::open(&path).unwrap();
        assert!(
            matches!(&rows[0].1, WalRecord::SegmentBase { strategy_effects, .. } if strategy_effects.transitions.is_empty())
        );
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}

#[test]
fn durable_effect_identity_and_suffix_survive_rotation() {
    use engine_types::{Action, StrategyEffectsState, StrategyId, StrategyTransitionState};
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let transition = StrategyTransitionState {
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
