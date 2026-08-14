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
                kind: OrderKind::Limit { px: 3120.75, tif: TimeInForce::PostOnly },
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
                reason: DenyReason::StaleAccountView { age_ns: 9_000_000_000, max_age_ns: 2_000_000_000 },
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
            },
            wire_ns: 99_000_555_000,
            arrival_mid: 0.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                client_order_id: "eng-0001".to_string(),
                symbol: SymbolId(11),
                side: Side::Sell,
                qty: 1.25,
                px: 3120.5,
                fee: 0.0021,
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
            wire_p50_ns: 700_000,
            wire_p99_ns: 2_900_000,
        },
        WalRecord::Note { source: "test".to_string(), text: "unicode ok: µs ✓".to_string() },
    ]
}

fn note(text: &str) -> WalRecord {
    WalRecord::Note { source: "test".to_string(), text: text.to_string() }
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

fn flip_byte(path: &Path, offset: u64) {
    let mut file = OpenOptions::new().read(true).write(true).open(path).unwrap();
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
    OpenOptions::new().write(true).open(&path).unwrap().set_len(torn_at).unwrap();

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
fn corrupt_checksum_in_last_frame_drops_it() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    let records: Vec<WalRecord> = (1..=3).map(|i| note(&format!("r{i}"))).collect();
    write_records(&path, &records);

    let spans = frame_spans(&path);
    // Flip a bit in the stored checksum of the last frame; the payload is
    // untouched, so only the checksum can catch this.
    flip_byte(&path, spans[2].0 + 4);

    let (mut wal, replayed) = WalWriter::open(&path).unwrap();
    assert_eq!(replayed.len(), 2);
    assert_eq!(fs::metadata(&path).unwrap().len(), spans[2].0);
    assert_eq!(wal.append(&note("r3-again")).unwrap(), 3);
    wal.barrier().unwrap();
    drop(wal);

    let seen = replay(&path).unwrap();
    assert_eq!(seen.len(), 3);
    assert_eq!(seen[2], (3, note("r3-again")));
}

#[test]
fn corrupt_checksum_in_middle_frame_drops_everything_after_it() {
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

    let (mut wal, replayed) = WalWriter::open(&path).unwrap();
    assert_eq!(replayed.len(), 1);
    assert_eq!(replayed[0], (1, note("r1")));
    assert_eq!(fs::metadata(&path).unwrap().len(), spans[1].0);
    assert_eq!(wal.append(&note("fresh")).unwrap(), 2);
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
    assert_eq!(fs::read(&path).unwrap(), before, "the file must be left alone");
}

#[test]
fn header_shorter_than_the_magic_is_an_error() {
    let dir = TempDir::new().unwrap();
    let path = log_path(&dir);
    fs::write(&path, b"EWA").unwrap();
    assert!(matches!(WalWriter::open(&path), Err(WalError::Corrupt { offset: 0, .. })));
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
    assert!(engine_wal::lock(&path).is_ok(), "the log was never handed back");
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
    assert_eq!(fs::read(&path).unwrap(), before, "the claim wrote into the log");
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
