use std::fs::{self, OpenOptions};
use std::io::{Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use engine_types::ids::{StrategyId, SymbolId};
use engine_types::orders::{
    Intent, OrderKind, OrderRequest, OrderUpdate, Side, StopSpec, TimeInForce,
};
use engine_types::risk::{DenyReason, RiskVerdict};
use engine_wal::{measure, replay, Wal, WalError, WalRecord, WalWriter};
use tempfile::TempDir;

fn log_path(dir: &TempDir) -> PathBuf {
    dir.path().join("engine.wal")
}

/// One of each record variant, so the framing is exercised over every shape
/// the engine writes.
fn every_variant() -> Vec<WalRecord> {
    vec![
        WalRecord::Boot {
            version: "0.1.0-test".to_string(),
            config_sha256: "a".repeat(64),
            wall_ts_ms: 1_770_000_000_000,
            commit: String::new(),
        },
        WalRecord::Intent {
            intent: Intent {
                exact_prices: None,
                exact_quantity: None,
                strategy: StrategyId(2),
                symbol: SymbolId(11),
                side: Side::Sell,
                qty: 1.25,
                kind: OrderKind::Limit {
                    px: 3120.75,
                    tif: TimeInForce::PostOnly,
                },
                stop: Some(StopSpec { trigger_px: 3200.0 }),
                reduce_only: false,
                tag: "entry".to_string(),
                decided_ns: 99_000_111_222,
                work: Some(engine_types::WorkPolicy::default()),
                leverage: None,
            },
            cause: Some(Box::new(engine_types::DecisionCause {
                callback_wall_ms: 1_770_000_000_001,
                callback_id: Some(3),
                causes: vec![engine_types::Cause::Signal {
                    source: "worker.long".to_string(),
                    sequence: 12,
                    observation_id: "obs-12".to_string(),
                }],
            })),
        },
        WalRecord::IntentRefused {
            wall_ts_ms: 1_770_000_000_002,
            strategy: StrategyId(2),
            symbol: SymbolId(11),
            tag: "entry".to_string(),
            client_order_id: Some("eng-0001".to_string()),
            code: "stop_would_loosen_position".to_string(),
            detail: "stop 3200 would loosen the whole Sell position from 3210".to_string(),
        },
        WalRecord::Verdict {
            client_order_id: Some("eng-0001".to_string()),
            verdict: RiskVerdict::Allow { qty: 1.25 },
        },
        WalRecord::Verdict {
            client_order_id: None,
            verdict: RiskVerdict::Deny {
                reason: DenyReason::StaleAccountView {
                    age_ns: 9_000_000_000,
                    max_age_ns: 2_000_000_000,
                },
            },
        },
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: "eng-0001".to_string(),
                strategy: StrategyId(2),
                symbol: SymbolId(11),
                side: Side::Sell,
                qty: 1.25,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: true,
                exact_terms: None,
                sleeve_effect: None,
                close_position: false,
            },
            wire_ns: 99_000_555_000,
            arrival_mid: 0.0,
        },
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: String::new(),
                client_order_id: "eng-0001".to_string(),
                symbol: SymbolId(11),
                side: Side::Sell,
                qty: 1.25,
                px: 3120.5,
                fee: Some(0.0021),
                is_maker: true,
                forced_close: None,
                venue_ts_ms: 1_770_000_000_500,
                recv_ns: 99_000_999_000,
            },
        },
        WalRecord::LatencyLedger {
            window_s: 60,
            events: 12_345,
            decide_p50_ns: 4_100,
            decide_p99_ns: 22_800,
            decide_p999_ns: Some(22_800),
            durable_p50_ns: 10_000,
            durable_p99_ns: 20_000,
            durable_p999_ns: Some(20_000),
            barrier_wait_p50_ns: 1_500,
            barrier_wait_p99_ns: 9_000,
            barrier_wait_p999_ns: Some(9_000),
            wire_p50_ns: 700_000,
            wire_p99_ns: 2_900_000,
            wire_p999_ns: Some(2_900_000),
            ack_p50_ns: 600_000,
            ack_p99_ns: 2_800_000,
            ack_p999_ns: Some(2_800_000),
            dispatch_queue_p50_ns: 1_000,
            dispatch_queue_p99_ns: 2_000,
            dispatch_queue_p999_ns: Some(2_000),
            venue_task_p50_ns: 650_000,
            venue_task_p99_ns: 2_850_000,
            venue_task_p999_ns: Some(2_850_000),
            core_resume_p50_ns: 2_000,
            core_resume_p99_ns: 4_000,
            core_resume_p999_ns: Some(4_000),
            end_to_end_p50_ns: 720_000,
            end_to_end_p99_ns: 3_000_000,
            end_to_end_p999_ns: Some(3_000_000),
        },
        WalRecord::Note {
            source: "test".to_string(),
            text: "unicode ok: µs ✓".to_string(),
        },
    ]
}

fn note(text: &str) -> WalRecord {
    WalRecord::Note {
        source: "test".to_string(),
        text: text.to_string(),
    }
}

/// Walk the raw file and report every frame as (start offset, payload length).
fn frame_spans(path: &Path) -> Vec<(u64, u32)> {
    let bytes = fs::read(path).unwrap();
    let mut spans = Vec::new();
    let mut at = 8usize;
    while at + 8 <= bytes.len() {
        let len = u32::from_le_bytes(bytes[at..at + 4].try_into().unwrap());
        if len == 0 || at + 8 + len as usize > bytes.len() {
            break;
        }
        spans.push((at as u64, len));
        at += 8 + len as usize;
    }
    spans
}

fn frame_payloads(path: &Path) -> Vec<Vec<u8>> {
    let bytes = fs::read(path).unwrap();
    frame_spans(path)
        .into_iter()
        .map(|(start, len)| {
            let payload_start = start as usize + 8;
            bytes[payload_start..payload_start + len as usize].to_vec()
        })
        .collect()
}

fn flip_byte(path: &Path, offset: u64) {
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    let mut byte = [0u8; 1];
    file.seek(SeekFrom::Start(offset)).unwrap();
    std::io::Read::read_exact(&mut file, &mut byte).unwrap();
    byte[0] ^= 0xff;
    file.seek(SeekFrom::Start(offset)).unwrap();
    file.write_all(&byte).unwrap();
}

fn write_records(path: &Path, records: &[WalRecord]) {
    let (mut wal, _) = WalWriter::open(path).unwrap();
    for record in records {
        wal.append(record).unwrap();
    }
    wal.barrier().unwrap();
}

#[test]
fn roundtrip_every_variant() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let written = every_variant();

    let (mut wal, replayed) = WalWriter::open(&path).unwrap();
    assert!(replayed.is_empty());
    for (i, record) in written.iter().enumerate() {
        assert_eq!(wal.append(record).unwrap(), i as u64 + 1);
    }
    wal.barrier().unwrap();
    drop(wal);

    let (wal, read_back) = WalWriter::open(&path).unwrap();
    let seqs: Vec<u64> = read_back.iter().map(|(seq, _)| *seq).collect();
    let records: Vec<WalRecord> = read_back.into_iter().map(|(_, r)| r).collect();
    assert_eq!(seqs, (1..=written.len() as u64).collect::<Vec<_>>());
    assert_eq!(records, written);
    assert_eq!(wal.next_seq(), written.len() as u64 + 1);
}

#[test]
fn current_tags_encode_checkpoints_and_unknown_fees_without_rewrites() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let records = vec![
        WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: 1_770_000_000_000,
        },
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: "unknown-stream-fee".to_string(),
                client_order_id: "eng-1".to_string(),
                symbol: SymbolId(1),
                side: Side::Buy,
                qty: 1.0,
                px: 100.0,
                fee: None,
                is_maker: true,
                forced_close: None,
                venue_ts_ms: 1_770_000_000_001,
                recv_ns: 2,
            },
        },
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: "explicit-zero-stream-fee".to_string(),
                client_order_id: "eng-2".to_string(),
                symbol: SymbolId(1),
                side: Side::Sell,
                qty: 1.0,
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: 1_770_000_000_002,
                recv_ns: 3,
            },
        },
        WalRecord::RecoveredFill {
            callbacks: None,
            allocation: None,
            amounts: None,
            exec_id: "unknown-recovered-fee".to_string(),
            client_order_id: "eng-3".to_string(),
            symbol: SymbolId(1),
            side: Side::Buy,
            qty: 1.0,
            px: 100.0,
            fee: None,
            is_maker: true,
            forced_close: None,
            venue_ts_ms: 1_770_000_000_003,
            recovered_wall_ts_ms: 1_770_000_000_004,
        },
        WalRecord::RecoveredFill {
            callbacks: None,
            allocation: None,
            amounts: None,
            exec_id: "explicit-zero-recovered-fee".to_string(),
            client_order_id: "eng-4".to_string(),
            symbol: SymbolId(1),
            side: Side::Sell,
            qty: 1.0,
            px: 100.0,
            fee: Some(0.0),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: 1_770_000_000_005,
            recovered_wall_ts_ms: 1_770_000_000_006,
        },
    ];
    write_records(&path, &records);

    let payloads = frame_payloads(&path);
    let direct: Vec<WalRecord> = payloads
        .iter()
        .map(|payload| serde_json::from_slice(payload).unwrap())
        .collect();
    assert_eq!(
        direct, records,
        "current serde tags preserve complete semantics without a rewrite"
    );
    let unknown_stream: serde_json::Value = serde_json::from_slice(&payloads[1]).unwrap();
    let zero_stream: serde_json::Value = serde_json::from_slice(&payloads[2]).unwrap();
    assert!(unknown_stream["update"]["Fill"]["fee"].is_null());
    assert_eq!(zero_stream["update"]["Fill"]["fee"], 0.0);
    assert!(unknown_stream["update"]["Fill"].get("fee_known").is_none());
    assert!(!String::from_utf8_lossy(&payloads[0]).contains("execution_history_through_ms"));

    let restored: Vec<WalRecord> = replay(&path)
        .unwrap()
        .into_iter()
        .map(|(_, record)| record)
        .collect();
    assert_eq!(
        restored, records,
        "the current reader restores full semantics"
    );
}

#[test]
fn an_old_order_record_defaults_to_an_ordinary_order() {
    let sent = every_variant()
        .into_iter()
        .find(|record| matches!(record, WalRecord::OrderSent { .. }))
        .expect("the fixture holds an order record");
    let mut old = serde_json::to_value(&sent).unwrap();
    old["request"]
        .as_object_mut()
        .unwrap()
        .remove("close_position");

    let decoded: WalRecord = serde_json::from_value(old).unwrap();
    let WalRecord::OrderSent { request, .. } = decoded else {
        panic!("the fixture is an order record");
    };
    assert!(!request.close_position);
}

#[test]
fn torn_tail_is_cut_and_appending_resumes() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let records: Vec<WalRecord> = (1..=5).map(|i| note(&format!("r{i}"))).collect();
    write_records(&path, &records);

    let spans = frame_spans(&path);
    assert_eq!(spans.len(), 5);
    let last_start = spans[4].0;
    let full_len = fs::metadata(&path).unwrap().len();
    // Cut the last frame in half: a crash between two writes.
    let torn_at = last_start + 8 + u64::from(spans[4].1) / 2;
    assert!(torn_at < full_len);
    OpenOptions::new()
        .write(true)
        .open(&path)
        .unwrap()
        .set_len(torn_at)
        .unwrap();

    let (mut wal, replayed) = WalWriter::open(&path).unwrap();
    assert_eq!(replayed.len(), 4);
    assert_eq!(fs::metadata(&path).unwrap().len(), last_start);
    assert_eq!(wal.next_seq(), 5);

    assert_eq!(wal.append(&note("r5-again")).unwrap(), 5);
    wal.barrier().unwrap();
    drop(wal);

    let seen = replay(&path).unwrap();
    assert_eq!(seen.len(), 5);
    assert_eq!(seen[4], (5, note("r5-again")));
}

#[test]
fn short_header_only_tail_is_cut() {
    // Only part of a frame header survived: fewer than 8 bytes past the last
    // good frame.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    write_records(&path, &[note("a"), note("b")]);
    let good_len = fs::metadata(&path).unwrap().len();

    let mut file = OpenOptions::new().write(true).open(&path).unwrap();
    file.seek(SeekFrom::End(0)).unwrap();
    file.write_all(&[0x11, 0x22, 0x33]).unwrap();
    drop(file);

    let (mut wal, replayed) = WalWriter::open(&path).unwrap();
    assert_eq!(replayed.len(), 2);
    assert_eq!(fs::metadata(&path).unwrap().len(), good_len);
    assert_eq!(wal.append(&note("c")).unwrap(), 3);
}

#[test]
fn zero_filled_tail_is_cut() {
    // A torn write often leaves zeros. A zero-length frame would otherwise
    // checksum as valid, so it must read as the end of the log.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    write_records(&path, &[note("a")]);
    let good_len = fs::metadata(&path).unwrap().len();

    let mut file = OpenOptions::new().write(true).open(&path).unwrap();
    file.seek(SeekFrom::End(0)).unwrap();
    file.write_all(&[0u8; 64]).unwrap();
    drop(file);

    let (_wal, replayed) = WalWriter::open(&path).unwrap();
    assert_eq!(replayed.len(), 1);
    assert_eq!(fs::metadata(&path).unwrap().len(), good_len);
}

#[test]
fn corrupt_checksum_in_last_frame_is_refused_and_left_untouched() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let records: Vec<WalRecord> = (1..=3).map(|i| note(&format!("r{i}"))).collect();
    write_records(&path, &records);

    let spans = frame_spans(&path);
    // Flip a bit in the stored checksum of the last frame; the payload is
    // untouched, so only the checksum can catch this.
    flip_byte(&path, spans[2].0 + 4);

    let before = fs::read(&path).unwrap();
    assert!(matches!(
        WalWriter::open(&path),
        Err(WalError::Corrupt { offset, .. }) if offset == spans[2].0
    ));
    assert_eq!(fs::read(&path).unwrap(), before);
}

#[test]
fn corrupt_checksum_in_middle_frame_is_refused_and_left_untouched() {
    // Documented behaviour: the log is read front to back, so a bad frame in
    // the middle ends the replay. Every record after it is dropped, even
    // though those bytes are still on disk — there is no way to trust a
    // sequence that has a hole in it.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let records: Vec<WalRecord> = (1..=5).map(|i| note(&format!("r{i}"))).collect();
    write_records(&path, &records);

    let spans = frame_spans(&path);
    // Flip a byte inside frame 2's payload: the JSON stays readable, the
    // checksum does not match.
    flip_byte(&path, spans[1].0 + 8 + 2);

    let before = fs::read(&path).unwrap();
    assert!(matches!(
        WalWriter::open(&path),
        Err(WalError::Corrupt { offset, .. }) if offset == spans[1].0
    ));
    assert_eq!(fs::read(&path).unwrap(), before);
}

#[test]
fn barrier_makes_the_record_visible_to_a_fresh_handle() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();

    wal.append(&note("buffered")).unwrap();
    // Still in our buffer: nothing outside this process can see it yet.
    assert!(replay(&path).unwrap().is_empty());

    wal.append(&note("durable")).unwrap();
    wal.barrier().unwrap();

    let seen = replay(&path).unwrap();
    assert_eq!(seen, vec![(1, note("buffered")), (2, note("durable"))]);
    drop(wal);
}

#[test]
fn flush_pushes_to_the_os_without_a_barrier() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("x")).unwrap();
    wal.flush().unwrap();
    assert_eq!(replay(&path).unwrap(), vec![(1, note("x"))]);
    drop(wal);
}

#[test]
fn a_started_barrier_has_already_written_the_bytes_before_it_returns() {
    // The whole point of starting one without waiting: the order of writes is
    // fixed the moment it returns, and only the disk's answer is outstanding.
    // A reader that opens the file now sees the record whether or not the
    // barrier has finished.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("in flight")).unwrap();
    let pending = wal.barrier_begin().unwrap();
    assert_eq!(replay(&path).unwrap(), vec![(1, note("in flight"))]);
    assert!(
        pending.outstanding(),
        "a real log ran this one off the writer"
    );
    pending.wait().unwrap();
    drop(wal);
}

#[test]
fn every_record_appended_before_a_started_barrier_survives_waiting_on_it() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    let written: Vec<_> = (0..64).map(|i| note(&format!("r{i}"))).collect();
    for record in &written {
        wal.append(record).unwrap();
    }
    wal.barrier_begin().unwrap().wait().unwrap();

    let (_reopened, read_back) = WalWriter::open(&path).unwrap();
    assert_eq!(
        read_back.into_iter().map(|(_, r)| r).collect::<Vec<_>>(),
        written
    );
    drop(wal);
}

#[test]
fn a_started_barrier_can_be_waited_on_after_more_appends() {
    // The handle is the answer for the bytes that were already out, not a
    // lock on the log. Appending while one is outstanding is ordinary.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("first")).unwrap();
    let pending = wal.barrier_begin().unwrap();
    wal.append(&note("second")).unwrap();
    pending.wait().unwrap();
    wal.flush().unwrap();
    assert_eq!(
        replay(&path).unwrap(),
        vec![(1, note("first")), (2, note("second"))]
    );
    drop(wal);
}

#[test]
fn a_barrier_after_a_rotation_covers_the_new_segment() {
    // The thread that runs the barrier holds its own descriptor. A rotation
    // replaces the file underneath it, and a thread left pointing at the
    // archive would sync that instead — passing every barrier while saying
    // nothing about the segment actually being written.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("before rotation")).unwrap();
    let base = WalRecord::SegmentBase {
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
        wall_ts_ms: 1_770_000_000_000,
        strategies: Vec::new(),
        symbols: Vec::new(),
        may_open: true,
        control_anchors: Vec::new(),
        attribution: Vec::new(),
        logged_exposure: Vec::new(),
        intended_stops: Vec::new(),
        recent_execution_ids: Vec::new(),
        execution_history_through_ms: Some(1_770_000_000_000),
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
    };
    assert!(wal.rotate(&base).unwrap(), "the file-backed log rotates");

    wal.append(&note("after rotation")).unwrap();
    let pending = wal.barrier_begin().unwrap();
    assert!(pending.outstanding(), "the rotation left no thread to ask");
    pending.wait().unwrap();

    let (_reopened, read_back) = engine_wal::open_current(&path).unwrap();
    let records: Vec<_> = read_back.into_iter().map(|(_, r)| r).collect();
    assert_eq!(records, vec![base, note("after rotation")]);
    drop(wal);
}

#[test]
fn a_settled_barrier_waits_for_nothing() {
    let settled = engine_wal::PendingBarrier::settled();
    assert!(!settled.outstanding());
    settled.wait().unwrap();
}

#[test]
fn a_durability_thread_that_dies_without_answering_is_a_failed_barrier() {
    // Never "it must have worked". The thread that owed the answer is gone,
    // so nothing can say the bytes reached the disk, and the order path has
    // to hear that as a failure.
    let (sender, receiver) = std::sync::mpsc::channel();
    let pending = engine_wal::PendingBarrier::running(receiver);
    assert!(pending.outstanding());
    drop(sender);
    assert!(pending.wait().is_err(), "a vanished answer read as success");
}

#[test]
fn empty_file_opens_clean() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    fs::write(&path, b"").unwrap();

    let (mut wal, replayed) = WalWriter::open(&path).unwrap();
    assert!(replayed.is_empty());
    assert_eq!(wal.next_seq(), 1);
    assert_eq!(wal.append(&note("first")).unwrap(), 1);
    wal.flush().unwrap();
    assert_eq!(fs::read(&path).unwrap()[..8], *b"EWAL0001");
}

#[test]
fn header_only_file_opens_clean() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    // Opening a missing file creates it with just the header.
    let (wal, replayed) = WalWriter::open(&path).unwrap();
    assert!(replayed.is_empty());
    drop(wal);
    assert_eq!(fs::metadata(&path).unwrap().len(), 8);

    let (mut wal, replayed) = WalWriter::open(&path).unwrap();
    assert!(replayed.is_empty());
    assert_eq!(wal.append(&note("first")).unwrap(), 1);
    assert!(replay(&path).unwrap().is_empty());
}

#[test]
fn bad_header_is_an_error_not_a_truncation() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    fs::write(&path, b"NOTAWAL!some other file's bytes").unwrap();
    let before = fs::read(&path).unwrap();

    match WalWriter::open(&path) {
        Err(WalError::Corrupt { offset, .. }) => assert_eq!(offset, 0),
        other => panic!("expected a corrupt-header error, got {other:?}"),
    }
    assert_eq!(
        fs::read(&path).unwrap(),
        before,
        "the file must be left alone"
    );
}

#[test]
fn header_shorter_than_the_magic_is_an_error() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    fs::write(&path, b"EWA").unwrap();
    assert!(matches!(
        WalWriter::open(&path),
        Err(WalError::Corrupt { offset: 0, .. })
    ));
}

#[test]
fn sequences_continue_across_reopens() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    for round in 0..3u64 {
        let (mut wal, replayed) = WalWriter::open(&path).unwrap();
        assert_eq!(replayed.len() as u64, round * 2);
        assert_eq!(wal.append(&note("a")).unwrap(), round * 2 + 1);
        assert_eq!(wal.append(&note("b")).unwrap(), round * 2 + 2);
        wal.barrier().unwrap();
    }
    assert_eq!(replay(&path).unwrap().len(), 6);
}

#[test]
fn many_appends_survive_the_buffer_high_water_mark() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    for i in 0..5_000u64 {
        assert_eq!(wal.append(&note(&format!("n{i}"))).unwrap(), i + 1);
    }
    wal.barrier().unwrap();
    drop(wal);

    let seen = replay(&path).unwrap();
    assert_eq!(seen.len(), 5_000);
    assert_eq!(seen[4_999], (5_000, note("n4999")));
}

#[test]
fn prints_append_and_barrier_cost() {
    // Run with `cargo test -p engine-wal -- --nocapture` to read the numbers.
    let dir = TempDir::new().unwrap();
    let costs = measure(&log_path(&dir), 20_000, 100).unwrap();
    println!("{costs}");
    assert_eq!(costs.appends, 20_000);
    assert_eq!(costs.barriers, 100);
    assert!(costs.append_p50_us > 0.0);
    assert!(costs.barrier_p50_us > 0.0);
}

// ------------------------------------------------------------- one writer

#[test]
fn a_second_engine_cannot_claim_the_same_log() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);

    let first = engine_wal::lock(&path).expect("the first claim");
    assert_eq!(first.path(), path);

    // flock lives on the open file description, so a second claim in this
    // same process contends exactly as a second engine would.
    let second = engine_wal::lock(&path);
    assert!(
        matches!(second, Err(engine_wal::WalLockError::AlreadyHeld { .. })),
        "two engines were allowed onto one log: {second:?}"
    );
}

#[test]
fn letting_go_of_a_log_lets_the_next_engine_have_it() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);

    drop(engine_wal::lock(&path).expect("the first claim"));
    assert!(
        engine_wal::lock(&path).is_ok(),
        "the log was never handed back"
    );
}

#[test]
fn claiming_a_log_does_not_disturb_a_byte_of_it() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);

    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("before the claim")).unwrap();
    wal.barrier().unwrap();
    drop(wal);
    let before = fs::read(&path).unwrap();

    let held = engine_wal::lock(&path).unwrap();
    assert_eq!(
        fs::read(&path).unwrap(),
        before,
        "the claim wrote into the log"
    );
    // And the writer that follows the claim reads back what was there.
    let (_, replayed) = WalWriter::open(&path).unwrap();
    assert_eq!(replayed.len(), 1);
    drop(held);
}

#[test]
fn a_log_that_does_not_exist_yet_can_still_be_claimed() {
    // The claim comes before the writer opens the file, so on a fresh box
    // there is nothing there to lock yet.
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("not-yet.wal");
    let held = engine_wal::lock(&path).expect("a fresh log could not be claimed");
    assert!(path.exists());
    // Empty, so the writer still lays down its own header.
    assert_eq!(fs::metadata(&path).unwrap().len(), 0);
    drop(held);
}

#[test]
#[cfg(debug_assertions)]
fn a_number_that_is_not_a_number_is_refused_instead_of_bricking_the_log() {
    // The venue's own fields are not screened before they reach a record, and
    // an f64 that is not a number is written as `null`. The reader refuses a
    // frame it cannot turn back into a record — deleting real bytes is not its
    // call — so one such record would make the whole log unopenable at the next
    // boot, for good, on a live account.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);

    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("before")).unwrap();
    for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        let err = wal
            .append(&WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Fill {
                    allocation: None,
                    amounts: None,
                    exec_id: String::new(),
                    client_order_id: "eng-1-1".to_string(),
                    symbol: SymbolId(3),
                    side: Side::Buy,
                    qty: 1.0,
                    px: bad,
                    fee: Some(0.1),
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: 1_770_000_000_000,
                    recv_ns: 7,
                },
            })
            .expect_err("a record nothing can read back must not be written");
        assert!(
            err.to_string().contains("does not read back"),
            "{bad}: {err}"
        );
    }
    wal.append(&note("after")).unwrap();
    wal.barrier().unwrap();
    drop(wal);

    // The log still opens, and holds exactly the two good records — the refused
    // ones left nothing behind, not even a sequence number.
    let (wal, read_back) = WalWriter::open(&path).unwrap();
    let records: Vec<WalRecord> = read_back.into_iter().map(|(_, r)| r).collect();
    assert_eq!(records, vec![note("before"), note("after")]);
    assert_eq!(wal.next_seq(), 3);
}

#[test]
fn an_absent_optional_number_is_still_written() {
    // `null` in a payload is not proof of a number that is not a number: every
    // absent Option writes one. If the check could not tell them apart, every
    // denied intent (its client order id is None) would stop the engine.
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let records = vec![
        WalRecord::Verdict {
            client_order_id: None,
            verdict: RiskVerdict::Deny {
                reason: DenyReason::MissingStop,
            },
        },
        WalRecord::Markout {
            client_order_id: "eng-1-1".to_string(),
            strategy: StrategyId(0),
            symbol: SymbolId(3),
            fill_ts_ms: 1_770_000_000_000,
            horizon_ms: 60_000,
            mid: None,
            signed_markout_bps: None,
            actual_horizon_ms: 60_250,
            notional_usdt: 12.5,
        },
    ];
    write_records(&path, &records);

    let (_wal, read_back) = WalWriter::open(&path).unwrap();
    assert_eq!(
        read_back.into_iter().map(|(_, r)| r).collect::<Vec<_>>(),
        records
    );
}

#[test]
#[cfg(debug_assertions)]
fn a_nonfinite_known_fee_cannot_be_replayed_as_an_unknown_fee() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    wal.append(&note("before")).unwrap();
    for callbacks in [None, Some(vec![StrategyId(0)])] {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let mut record = every_variant()
                .into_iter()
                .find(|record| matches!(record, WalRecord::OrderUpdate { .. }))
                .unwrap();
            let WalRecord::OrderUpdate {
                callbacks: owners,
                update: OrderUpdate::Fill { fee, .. },
            } = &mut record
            else {
                unreachable!()
            };
            *owners = callbacks.clone();
            *fee = Some(bad);
            assert!(wal.append(&record).is_err(), "known fee {bad} was accepted");
        }
    }
    for legacy in [false, true] {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let mut record = recovered_callback_record();
            let WalRecord::RecoveredFill { callbacks, fee, .. } = &mut record else {
                unreachable!()
            };
            if legacy {
                *callbacks = None;
            }
            *fee = Some(bad);
            assert!(
                wal.append(&record).is_err(),
                "recovered fee {bad} was accepted"
            );
        }
    }
    wal.append(&note("after")).unwrap();
    wal.barrier().unwrap();
    drop(wal);
    let (wal, records) = WalWriter::open(&path).unwrap();
    assert_eq!(wal.next_seq(), 3);
    assert_eq!(
        records.into_iter().map(|(_, row)| row).collect::<Vec<_>>(),
        [note("before"), note("after")]
    );
}

#[test]
fn a_record_written_before_a_field_existed_still_replays() {
    // The shape of a `work` policy as the engine wrote it before
    // `hold_decision_px` and `give_up_instead_of_crossing` were added. A
    // required field on a WAL record is an engine that cannot boot on its own
    // history: the live fleet crash-looped on exactly this, replaying a log
    // whose frames passed their checksum and then failed to parse.
    let old_shape = r#"{"window_ms":120000,"reprice_ms":15000,"cross_grace_ms":20000,
        "max_amends":8,"improve_lean":0.15,"back_lean":0.15,"urgency_join_frac":0.5,
        "urgency_improve_frac":0.85,"drift_cross_fee_bp":0.0}"#;
    let policy: engine_types::orders::WorkPolicy =
        serde_json::from_str(old_shape).expect("a record from before the field must still read");
    assert!(!policy.hold_decision_px);
    assert!(!policy.give_up_instead_of_crossing);
    // The fields that were always there are unchanged by the default.
    assert_eq!(policy.window_ms, 120_000);
    assert_eq!(policy.max_amends, 8);
}

fn atomic_queued_order() -> WalRecord {
    let mut record = every_variant()
        .into_iter()
        .find(|record| matches!(record, WalRecord::OrderSent { .. }))
        .unwrap();
    let WalRecord::OrderSent {
        request, dispatch, ..
    } = &mut record
    else {
        unreachable!()
    };
    *dispatch = Some(Box::new(
        engine_types::order_dispatch::QueuedOrderDispatch {
            intent: Intent {
                exact_prices: None,
                exact_quantity: None,
                strategy: request.strategy,
                symbol: request.symbol,
                side: request.side,
                qty: request.qty,
                kind: request.kind,
                stop: request.stop,
                reduce_only: true,
                tag: "atomic-exit".into(),
                decided_ns: 7,
                work: None,
                leverage: None,
            },
            origin_ns: 6,
        },
    ));
    record
}

#[test]
fn a_partial_atomic_order_frame_never_replays_an_order_without_its_dispatch() {
    let dir = TempDir::new().unwrap();
    let source = dir.path().join("complete.wal");
    let before = note("checkpoint already committed");
    let order = atomic_queued_order();
    write_records(&source, &[before.clone(), order.clone()]);
    let complete = fs::read(&source).unwrap();
    let start = frame_spans(&source)[1].0 as usize;
    let path = dir.path().join("cut.wal");
    for cut in start..complete.len() {
        fs::write(&path, &complete[..cut]).unwrap();
        let (writer, records) = WalWriter::open(&path).unwrap();
        assert_eq!(
            records
                .into_iter()
                .map(|(_, record)| record)
                .collect::<Vec<_>>(),
            std::slice::from_ref(&before),
            "cut at byte {cut} exposed a partial order/outbox pair"
        );
        drop(writer);
        assert_eq!(
            fs::read(&path).unwrap().len(),
            start,
            "cut at byte {cut} was not repaired to the prior complete frame"
        );
    }
    fs::write(&path, &complete).unwrap();
    let (_, records) = WalWriter::open(&path).unwrap();
    assert_eq!(
        records
            .into_iter()
            .map(|(_, record)| record)
            .collect::<Vec<_>>(),
        [before, order]
    );
}

#[test]
fn atomic_order_has_a_new_required_tag_and_legacy_order_bytes_still_replay() {
    let record = atomic_queued_order();
    let mut value = serde_json::to_value(&record).unwrap();
    assert_eq!(value["kind"], "order_sent_v2");
    assert!(value["dispatch"].is_object());
    value["kind"] = "order_sent".into();
    value.as_object_mut().unwrap().remove("dispatch");
    let payload = serde_json::to_vec(&value).unwrap();
    let mut bytes = b"EWAL0001".to_vec();
    bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
    bytes.extend_from_slice(&payload);
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    fs::write(&path, &bytes).unwrap();
    let (_, records) = WalWriter::open(&path).unwrap();
    assert!(matches!(
        &records[0].1,
        WalRecord::OrderSent { dispatch: None, .. }
    ));
    assert_eq!(fs::read(path).unwrap(), bytes);
}

#[test]
fn an_atomic_order_missing_dispatch_authority_is_refused_without_truncation() {
    for null in [false, true] {
        let mut value = serde_json::to_value(atomic_queued_order()).unwrap();
        if null {
            value["dispatch"] = serde_json::Value::Null;
        } else {
            value.as_object_mut().unwrap().remove("dispatch");
        }
        let payload = serde_json::to_vec(&value).unwrap();
        let mut bytes = b"EWAL0001".to_vec();
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        fs::write(&path, &bytes).unwrap();
        assert!(
            matches!(WalWriter::open(&path), Err(WalError::Corrupt { .. })),
            "new atomic record silently became a legacy order"
        );
        assert_eq!(fs::read(path).unwrap(), bytes);
    }
}

#[test]
fn writing_a_legacy_order_preserves_its_legacy_tag() {
    let record = every_variant()
        .into_iter()
        .find(|record| matches!(record, WalRecord::OrderSent { dispatch: None, .. }))
        .unwrap();
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    write_records(&path, std::slice::from_ref(&record));
    let bytes = fs::read(&path).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&bytes[16..]).unwrap();
    assert_eq!(value["kind"], "order_sent");
    assert!(value.get("dispatch").is_none());
    assert_eq!(replay(&path).unwrap(), [(1, record)]);
}

#[test]
fn callback_cursor_reads_parent_owner_and_unknown_fee_without_moving_the_writer() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let mut parent = every_variant()
        .into_iter()
        .find(|record| {
            matches!(
                record,
                WalRecord::OrderUpdate {
                    update: OrderUpdate::Fill { .. },
                    ..
                }
            )
        })
        .unwrap();
    let WalRecord::OrderUpdate {
        callbacks,
        update: OrderUpdate::Fill { fee, .. },
    } = &mut parent
    else {
        unreachable!()
    };
    *callbacks = Some(vec![StrategyId(0)]);
    *fee = None;
    let (mut writer, _) = WalWriter::open(&path).unwrap();
    writer.append(&note(&"x".repeat(1024 * 1024))).unwrap();
    writer.append(&parent).unwrap();
    let mut reader = writer.callback_reader().unwrap().unwrap();
    let first = reader.next(reader.start()).unwrap().unwrap();
    assert!(first.source.is_none());
    writer
        .append(&note("writer remains at the append frontier"))
        .unwrap();
    writer.flush().unwrap();
    let second = reader.next(first.next).unwrap().unwrap();
    let (owners, update) = second.source.unwrap();
    assert_eq!(owners, [StrategyId(0)]);
    assert!(matches!(
        update,
        engine_types::strategy_process::CallbackEvent::Order {
            update: OrderUpdate::Fill { fee: None, .. }
        }
    ));
    assert_eq!(second.next.sequence, 3);
    let third = reader.next(second.next).unwrap().unwrap();
    assert!(third.source.is_none());
    assert!(reader.next(third.next).unwrap().is_none());
    assert_eq!(replay(&path).unwrap()[1].1, parent);
    assert_eq!(
        replay(path).unwrap()[2].1,
        note("writer remains at the append frontier")
    );
}

#[test]
fn retained_v2_order_news_cannot_omit_its_callback_ownership() {
    for missing in [false, true] {
        let mut value = serde_json::to_value(
            every_variant()
                .into_iter()
                .find(|record| matches!(record, WalRecord::OrderUpdate { .. }))
                .unwrap(),
        )
        .unwrap();
        value["kind"] = "order_update_v2".into();
        if !missing {
            value["callbacks"] = serde_json::Value::Null;
        }
        let payload = serde_json::to_vec(&value).unwrap();
        let mut bytes = b"EWAL0001".to_vec();
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        fs::write(&path, &bytes).unwrap();
        assert!(matches!(
            WalWriter::open(&path),
            Err(WalError::Corrupt { .. })
        ));
        assert_eq!(fs::read(path).unwrap(), bytes);
    }
}

#[test]
fn exact_amend_records_require_typed_values_and_legacy_bytes_still_decode() {
    use engine_types::numeric::ExactNumber;
    let record = WalRecord::AmendResolved {
        client_order_id: "eng-1".into(),
        effective_px: 100.1,
        exact_effective_px: Some(ExactNumber::venue_decimal("100.1").unwrap()),
    };
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    write_records(&path, std::slice::from_ref(&record));
    assert_eq!(
        replay(&path)
            .unwrap()
            .into_iter()
            .map(|(_, r)| r)
            .collect::<Vec<_>>(),
        vec![record.clone()]
    );
    for field in [None, Some(serde_json::Value::Null)] {
        let mut value = serde_json::to_value(&record).unwrap();
        if let Some(field) = field {
            value["exact_effective_px"] = field;
        } else {
            value.as_object_mut().unwrap().remove("exact_effective_px");
        }
        for legacy in [false, true] {
            value["kind"] = if legacy {
                "amend_resolved"
            } else {
                "amend_resolved_v2"
            }
            .into();
            let payload = serde_json::to_vec(&value).unwrap();
            let mut bytes = b"EWAL0001".to_vec();
            bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
            bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
            bytes.extend_from_slice(&payload);
            fs::write(&path, &bytes).unwrap();
            if legacy {
                assert!(WalWriter::open(&path).is_ok());
            } else {
                assert!(matches!(
                    WalWriter::open(&path),
                    Err(WalError::Corrupt { .. })
                ));
            }
            assert_eq!(fs::read(&path).unwrap(), bytes);
        }
    }
}

fn recovered_callback_record() -> WalRecord {
    WalRecord::RecoveredFill {
        callbacks: Some(engine_types::wal::RecoveredCallbacks {
            owners: vec![StrategyId(0), StrategyId(1)],
            recv_ns: 123,
        }),
        allocation: Some(Box::new(
            engine_types::execution_allocation::ExecutionAllocation {
                policy: engine_types::execution_allocation::AllocationPolicy::EmergencyNetFifo,
                legacy_quantity_step: None,
                slices: [(0, "a", "0.25"), (1, "b", "0.75")]
                    .into_iter()
                    .map(|(strategy, key, qty)| {
                        engine_types::execution_allocation::ExecutionSlice {
                            strategy: StrategyId(strategy),
                            strategy_key: key.into(),
                            quantity: qty.parse().unwrap(),
                            fee: None,
                        }
                    })
                    .collect(),
            },
        )),
        amounts: None,
        exec_id: "recovered-owned-parent".into(),
        client_order_id: String::new(),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty: 1.0,
        px: 100.0,
        fee: None,
        is_maker: false,
        forced_close: Some(engine_types::ForcedClose::StopLoss),
        venue_ts_ms: 10,
        recovered_wall_ts_ms: 20,
    }
}

#[test]
fn recovered_callback_ownership_and_fill_share_every_partial_frame_restart_cut() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let record = recovered_callback_record();
    write_records(&path, std::slice::from_ref(&record));
    let bytes = fs::read(&path).unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&bytes[16..]).unwrap()["kind"],
        "recovered_fill_v3"
    );
    for cut in 8..=bytes.len() {
        fs::write(&path, &bytes[..cut]).unwrap();
        let (mut writer, rows) = WalWriter::open(&path).unwrap();
        if cut < bytes.len() {
            assert!(
                rows.is_empty(),
                "partial parent exposed a fill without its callback at byte {cut}"
            );
            assert_eq!(fs::read(&path).unwrap(), &bytes[..8]);
        } else {
            assert_eq!(rows, [(1, record.clone())]);
            let mut reader = writer.callback_reader().unwrap().unwrap();
            let source = reader
                .next(reader.start())
                .unwrap()
                .unwrap()
                .source
                .unwrap();
            assert_eq!(source.0, [StrategyId(0), StrategyId(1)]);
            assert!(matches!(
                source.1,
                engine_types::strategy_process::CallbackEvent::Order {
                    update: OrderUpdate::Fill {
                        fee: None,
                        recv_ns: 123,
                        ..
                    }
                }
            ));
        }
    }
}

#[test]
fn retained_v2_recovered_callback_metadata_remains_required() {
    let record = recovered_callback_record();
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    for legacy in [false, true] {
        for missing in [false, true] {
            let mut value = serde_json::to_value(&record).unwrap();
            value["kind"] = if legacy {
                "recovered_fill"
            } else {
                "recovered_fill_v2"
            }
            .into();
            if missing {
                value.as_object_mut().unwrap().remove("callbacks");
            } else {
                value["callbacks"] = serde_json::Value::Null;
            }
            let payload = serde_json::to_vec(&value).unwrap();
            let mut bytes = b"EWAL0001".to_vec();
            bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
            bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
            bytes.extend_from_slice(&payload);
            fs::write(&path, &bytes).unwrap();
            if legacy {
                assert!(WalWriter::open(&path).is_ok());
            } else {
                assert!(matches!(
                    WalWriter::open(&path),
                    Err(WalError::Corrupt { .. })
                ));
            }
            assert_eq!(fs::read(&path).unwrap(), bytes);
        }
    }
    let mut legacy = record;
    let WalRecord::RecoveredFill { callbacks, .. } = &mut legacy else {
        unreachable!()
    };
    *callbacks = None;
    fs::remove_file(&path).unwrap();
    write_records(&path, std::slice::from_ref(&legacy));
    let bytes = fs::read(&path).unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&bytes[16..]).unwrap()["kind"],
        "recovered_fill_v3"
    );
    assert_eq!(replay(&path).unwrap(), [(1, legacy)]);
}

#[test]
fn callback_cursor_skips_published_strategy_events_in_a_heterogeneous_archive() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let unrelated = WalRecord::StrategyEventPublished {
        wall_ts_ms: 1,
        event: engine_types::StrategyEvent {
            source: StrategyId(0),
            destination: StrategyId(1),
            kind: "sleeve_closed".into(),
            event_id: "closed-1".into(),
            payload: vec![1, 2, 3],
        },
    };
    let callback = WalRecord::StrategyCallbackSource {
        placement: None,
        strategy: StrategyId(1),
        event: engine_types::strategy_process::CallbackEvent::Boot,
    };
    let (mut writer, _) = WalWriter::open(&path).unwrap();
    writer.append(&unrelated).unwrap();
    writer.append(&callback).unwrap();
    writer.barrier().unwrap();
    drop(writer);
    let original = fs::read(&path).unwrap();
    let (mut writer, _) = WalWriter::open(&path).unwrap();
    let mut reader = writer.callback_reader().unwrap().unwrap();
    let first = reader.next(reader.start()).unwrap().unwrap();
    assert!(first.source.is_none());
    let second = reader.next(first.next).unwrap().unwrap();
    assert_eq!(
        second.source,
        Some((
            vec![StrategyId(1)],
            engine_types::strategy_process::CallbackEvent::Boot
        ))
    );
    assert!(reader.next(second.next).unwrap().is_none());
    assert_eq!(fs::read(&path).unwrap(), original);
}

#[test]
fn archive_projections_accept_heterogeneous_record_shapes() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut writer, _) = WalWriter::open(&path).unwrap();
    for record in every_variant() {
        writer.append(&record).unwrap();
    }
    writer.barrier().unwrap();
    let original = fs::read(&path).unwrap();
    let mut callbacks = writer.callback_reader().unwrap().unwrap();
    let mut cursor = callbacks.start();
    while let Some(record) = callbacks.next(cursor).unwrap() {
        cursor = record.next;
    }
    let mut lineage = writer
        .order_lineage_reader("not-an-order")
        .unwrap()
        .unwrap();
    assert!(lineage.next().unwrap().is_none());
    assert!(writer
        .order_epoch_reader()
        .unwrap()
        .unwrap()
        .max_order_epoch_ms()
        .unwrap()
        .is_some());
    assert_eq!(fs::read(&path).unwrap(), original);
}

#[test]
fn callback_projection_rejects_malformed_relevant_fields_and_bad_unrelated_checksums() {
    for (value, corrupt_crc) in [
        (
            serde_json::json!({"kind":"strategy_callback_source","strategy":0,"event":{"kind":{"Boot":{}}}}),
            false,
        ),
        (
            serde_json::json!({"kind":"strategy_callback_source","strategy":{},"event":{"kind":"boot"}}),
            false,
        ),
        (
            serde_json::json!({"kind":"order_update_v2","callbacks":null,"update":{"Cancelled":{"client_order_id":"owned","recv_ns":2}}}),
            false,
        ),
        (
            serde_json::json!({"kind":"order_update_v2","callbacks":[0],"update":{"Unknown":{}}}),
            false,
        ),
        (
            serde_json::json!({"kind":"note","source":"unrelated","message":"still checksummed"}),
            true,
        ),
    ] {
        let dir = TempDir::new().unwrap();
        let path = log_path(&dir);
        let (mut writer, _) = WalWriter::open(&path).unwrap();
        let payload = serde_json::to_vec(&value).unwrap();
        let mut bytes = b"EWAL0001".to_vec();
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&(crc32c::crc32c(&payload) ^ u32::from(corrupt_crc)).to_le_bytes());
        bytes.extend_from_slice(&payload);
        fs::write(&path, &bytes).unwrap();
        let mut reader = writer.callback_reader().unwrap().unwrap();
        assert!(reader.next(reader.start()).is_err());
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}

#[test]
fn finite_fill_fields_round_trip_for_seeded_binary64_values() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let (mut wal, _) = WalWriter::open(&path).unwrap();
    let mut expected = Vec::new();
    let mut seed = 0x6a09_e667_f3bc_c909_u64;
    for index in 0..1024 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        let value = f64::from_bits(seed);
        if !value.is_finite() {
            continue;
        }
        let mut record = every_variant()
            .into_iter()
            .find(|record| matches!(record, WalRecord::OrderUpdate { .. }))
            .unwrap();
        let WalRecord::OrderUpdate {
            callbacks,
            update:
                OrderUpdate::Fill {
                    qty,
                    px,
                    fee,
                    exec_id,
                    ..
                },
        } = &mut record
        else {
            unreachable!()
        };
        *callbacks = (index % 2 == 0).then(|| vec![StrategyId(2)]);
        *qty = value.abs();
        *px = value.abs();
        *fee = (index % 3 != 0).then_some(value);
        *exec_id = format!("property-{index}-null-fee_known");
        wal.append(&record).unwrap();
        expected.push(record);
    }
    wal.barrier().unwrap();
    drop(wal);
    let (_, records) = WalWriter::open(&path).unwrap();
    assert_eq!(
        records.into_iter().map(|(_, row)| row).collect::<Vec<_>>(),
        expected
    );
}

#[test]
fn retained_fee_markers_and_note_checkpoints_keep_their_original_meaning() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let payloads = [
        r#"{"kind":"order_update","update":{"Fill":{"exec_id":"old-fill","client_order_id":"eng-old-1","symbol":0,"side":"Buy","qty":1.0,"px":2.0,"fee":0.0,"fee_known":false,"is_maker":false,"venue_ts_ms":1788000000000,"recv_ns":1}}}"#,
        r#"{"kind":"recovered_fill","exec_id":"old-recovered","client_order_id":"eng-old-1","symbol":0,"side":"Buy","qty":1.0,"px":2.0,"fee":0.0,"fee_known":false,"is_maker":false,"venue_ts_ms":1788000000000,"recovered_wall_ts_ms":1788000000001}"#,
        r#"{"kind":"note","source":"engine.execution_history_checkpoint.v1","text":"history","execution_history_through_ms":1788000000002}"#,
    ];
    let mut bytes = b"EWAL0001".to_vec();
    for payload in payloads {
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(payload.as_bytes()).to_le_bytes());
        bytes.extend_from_slice(payload.as_bytes());
    }
    fs::write(&path, &bytes).unwrap();
    let records = replay(&path).unwrap();
    assert!(matches!(
        records[0].1,
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill { fee: None, .. },
            ..
        }
    ));
    assert!(matches!(
        records[1].1,
        WalRecord::RecoveredFill { fee: None, .. }
    ));
    assert_eq!(
        records[2].1,
        WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: 1_788_000_000_002
        }
    );
    assert_eq!(fs::read(&path).unwrap(), bytes);
}

#[test]
fn duplicate_record_fields_are_corrupt_without_truncating_the_family() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    for payload in [
        r#"{"kind":"note","source":"one","source":"two","text":"retained"}"#,
        r#"{"kind":"order_update","update":{"Fill":{"exec_id":"old-fill","client_order_id":"eng-old-1","symbol":0,"side":"Buy","qty":1.0,"qty":2.0,"px":2.0,"fee":0.0,"is_maker":false,"venue_ts_ms":1788000000000,"recv_ns":1}}}"#,
    ] {
        let mut bytes = b"EWAL0001".to_vec();
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(payload.as_bytes()).to_le_bytes());
        bytes.extend_from_slice(payload.as_bytes());
        fs::write(&path, &bytes).unwrap();
        let error = match WalWriter::open(&path) {
            Err(error) => error,
            Ok(_) => panic!("duplicate fields must stay corrupt"),
        };
        assert!(error.to_string().contains("duplicate field"), "{error}");
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}

#[test]
fn retired_wire_kinds_replay_but_the_current_writer_cannot_append_them() {
    use engine_types::strategy_process::{
        CallbackEvent, CallbackPreparation, CallbackSnapshot, StrategyCallbackInput,
        StrategyProcessState, StrategyRuntimeState,
    };
    use engine_types::wal::RetainedWalRecord as Retained;

    let queued = StrategyCallbackInput {
        order_origin: None,
        callback_id: 1,
        strategy: StrategyId(0),
        event: CallbackEvent::Boot,
        preparation: CallbackPreparation::Queued,
    };
    let prepared = StrategyCallbackInput {
        preparation: CallbackPreparation::Prepared {
            snapshot: CallbackSnapshot {
                strategy: StrategyId(0),
                now_ns: 1,
                wall_ms: 1,
                entries_enabled: true,
                account: engine_types::StrategyAccountSummary {
                    equity_usdt: 100.0,
                    available_margin_usdt: 100.0,
                    observed_ns: 1,
                },
                symbols: vec![],
                orders: vec![],
                global_checkpoint: None,
                strategy_names: vec!["fixture".into()],
                strategy_events: vec![],
            },
        },
        ..queued.clone()
    };
    let records = [
        (
            "control_anchor",
            Retained::ControlAnchor {
                source: "risk".into(),
                state: "{}".into(),
            },
        ),
        (
            "target_book_latch",
            Retained::TargetBookLatch {
                wall_ts_ms: 1,
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                latched: true,
            },
        ),
        (
            "claims_dropped",
            Retained::ClaimsDropped {
                wall_ts_ms: 1,
                rows: vec![],
            },
        ),
        (
            "strategy_callback_queued",
            Retained::StrategyCallbackQueued { input: queued },
        ),
        (
            "strategy_callback_prepared",
            Retained::StrategyCallbackPrepared { input: prepared },
        ),
        (
            "strategy_process_transition_queued",
            Retained::StrategyProcessTransitionQueued {
                input_id: 1,
                transition: None,
                process: StrategyProcessState {
                    strategy: StrategyId(0),
                    last_callback_id: 1,
                    runtime: StrategyRuntimeState {
                        schema_version: 1,
                        kind: "fixture".into(),
                        configuration_sha256: "a".repeat(64),
                        payload: vec![],
                    },
                    timers: vec![],
                    retained_signal_subscriptions: None,
                },
            },
        ),
        (
            "names",
            Retained::Names {
                strategies: vec!["fixture".into()],
                symbols: vec!["BTCUSDT".into()],
            },
        ),
        (
            "fast_execution",
            Retained::FastExecution {
                exec_id: "old-fast".into(),
                client_order_id: "eng-old-1".into(),
                venue_order_id: "venue-old-1".into(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                px: 2.0,
                is_maker: false,
                venue_ts_ms: 1,
                recv_ns: 2,
            },
        ),
    ];
    let dir = TempDir::new().unwrap();
    let history = dir.path().join("history.wal");
    let path = log_path(&dir);
    let (mut writer, _) = WalWriter::open(&path).unwrap();
    writer.append(&note("before")).unwrap();
    let mut bytes = b"EWAL0001".to_vec();
    let mut expected = Vec::new();
    for (kind, retained) in records {
        let record = WalRecord::Retained(retained);
        let payload = serde_json::to_vec(&record).unwrap();
        let value: serde_json::Value = serde_json::from_slice(&payload).unwrap();
        assert_eq!(value["kind"], kind);
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
        assert!(writer
            .append(&record)
            .unwrap_err()
            .to_string()
            .contains("read-only"));
        expected.push(record);
    }
    fs::write(&history, &bytes).unwrap();
    assert_eq!(
        replay(&history)
            .unwrap()
            .into_iter()
            .map(|(_, row)| row)
            .collect::<Vec<_>>(),
        expected
    );
    assert_eq!(fs::read(&history).unwrap(), bytes);
    assert_eq!(writer.append(&note("after")).unwrap(), 2);
    writer.barrier().unwrap();
    assert_eq!(
        replay(&path)
            .unwrap()
            .into_iter()
            .map(|(_, row)| row)
            .collect::<Vec<_>>(),
        [note("before"), note("after")]
    );
}
