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
        },
        WalRecord::Intent {
            intent: Intent {
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
            request: OrderRequest {
                client_order_id: "eng-0001".to_string(),
                strategy: StrategyId(2),
                symbol: SymbolId(11),
                side: Side::Sell,
                qty: 1.25,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: true,
                close_position: false,
            },
            wire_ns: 99_000_555_000,
            arrival_mid: 0.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: String::new(),
                client_order_id: "eng-0001".to_string(),
                symbol: SymbolId(11),
                side: Side::Sell,
                qty: 1.25,
                px: 3120.5,
                fee: Some(0.0021),
                is_maker: true,
                venue_ts_ms: 1_770_000_000_500,
                recv_ns: 99_000_999_000,
            },
        },
        WalRecord::LatencyLedger {
            window_s: 60,
            events: 12_345,
            decide_p50_ns: 4_100,
            decide_p99_ns: 22_800,
            durable_p50_ns: 10_000,
            durable_p99_ns: 20_000,
            barrier_wait_p50_ns: 1_500,
            barrier_wait_p99_ns: 9_000,
            wire_p50_ns: 700_000,
            wire_p99_ns: 2_900_000,
            ack_p50_ns: 600_000,
            ack_p99_ns: 2_800_000,
            dispatch_queue_p50_ns: 1_000,
            dispatch_queue_p99_ns: 2_000,
            venue_task_p50_ns: 650_000,
            venue_task_p99_ns: 2_850_000,
            core_resume_p50_ns: 2_000,
            core_resume_p99_ns: 4_000,
            end_to_end_p50_ns: 720_000,
            end_to_end_p99_ns: 3_000_000,
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
fn new_checkpoints_and_unknown_fees_are_readable_by_the_previous_shape() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let records = vec![
        WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: 1_770_000_000_000,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: "unknown-stream-fee".to_string(),
                client_order_id: "eng-1".to_string(),
                symbol: SymbolId(1),
                side: Side::Buy,
                qty: 1.0,
                px: 100.0,
                fee: None,
                is_maker: true,
                venue_ts_ms: 1_770_000_000_001,
                recv_ns: 2,
            },
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: "explicit-zero-stream-fee".to_string(),
                client_order_id: "eng-2".to_string(),
                symbol: SymbolId(1),
                side: Side::Sell,
                qty: 1.0,
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                venue_ts_ms: 1_770_000_000_002,
                recv_ns: 3,
            },
        },
        WalRecord::RecoveredFill {
            exec_id: "unknown-recovered-fee".to_string(),
            client_order_id: "eng-3".to_string(),
            symbol: SymbolId(1),
            side: Side::Buy,
            qty: 1.0,
            px: 100.0,
            fee: None,
            is_maker: true,
            venue_ts_ms: 1_770_000_000_003,
            recovered_wall_ts_ms: 1_770_000_000_004,
        },
        WalRecord::RecoveredFill {
            exec_id: "explicit-zero-recovered-fee".to_string(),
            client_order_id: "eng-4".to_string(),
            symbol: SymbolId(1),
            side: Side::Sell,
            qty: 1.0,
            px: 100.0,
            fee: Some(0.0),
            is_maker: false,
            venue_ts_ms: 1_770_000_000_005,
            recovered_wall_ts_ms: 1_770_000_000_006,
        },
    ];
    write_records(&path, &records);

    let payloads = frame_payloads(&path);
    let legacy_view: Vec<WalRecord> = payloads
        .iter()
        .map(|payload| {
            serde_json::from_slice(payload)
                .expect("the additive wire shape reads without the current WAL decoder")
        })
        .collect();
    assert!(matches!(
        &legacy_view[0],
        WalRecord::Note { source, text }
            if source == "engine.execution_history_checkpoint.v1"
                && text == "through_wall_ts_ms=1770000000000"
    ));
    assert!(matches!(
        &legacy_view[1],
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill { fee: Some(0.0), .. }
        }
    ));
    assert!(matches!(
        &legacy_view[2],
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill { fee: Some(0.0), .. }
        }
    ));
    assert!(matches!(
        &legacy_view[3],
        WalRecord::RecoveredFill { fee: Some(0.0), .. }
    ));
    assert!(matches!(
        &legacy_view[4],
        WalRecord::RecoveredFill { fee: Some(0.0), .. }
    ));

    let unknown_stream: serde_json::Value = serde_json::from_slice(&payloads[1]).unwrap();
    let explicit_zero_stream: serde_json::Value = serde_json::from_slice(&payloads[2]).unwrap();
    assert_eq!(unknown_stream["update"]["Fill"]["fee_known"], false);
    assert!(explicit_zero_stream["update"]["Fill"]
        .get("fee_known")
        .is_none());
    let unknown_recovered: serde_json::Value = serde_json::from_slice(&payloads[3]).unwrap();
    let explicit_zero_recovered: serde_json::Value = serde_json::from_slice(&payloads[4]).unwrap();
    assert_eq!(unknown_recovered["fee_known"], false);
    assert!(explicit_zero_recovered.get("fee_known").is_none());

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
    let mut old = serde_json::to_value(&every_variant()[4]).unwrap();
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
        open_orders: Vec::new(),
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
                update: OrderUpdate::Fill {
                    exec_id: String::new(),
                    client_order_id: "eng-1-1".to_string(),
                    symbol: SymbolId(3),
                    side: Side::Buy,
                    qty: 1.0,
                    px: bad,
                    fee: Some(0.1),
                    is_maker: false,
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
