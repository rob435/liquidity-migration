use std::fs;
use std::path::Path;

use engine_types::{numeric::Exact, Side, StrategyId, SymbolId};
use engine_wal::{replay, replay_scan, Wal, WalError, WalRecord, WalWriter};
use proptest::prelude::*;

fn record() -> impl Strategy<Value = WalRecord> {
    prop_oneof![
        any::<i64>().prop_map(|epoch_ms| WalRecord::OrderIdEpoch { epoch_ms }),
        ".{0,64}".prop_map(|client_order_id| WalRecord::OrderDispatchAttempted { client_order_id }),
        ".{0,64}".prop_map(|client_order_id| WalRecord::OrderDispatchCompleted { client_order_id }),
        (
            any::<i64>(),
            prop::collection::vec(".{0,32}", 0..4),
            any::<bool>()
        )
            .prop_map(|(wall_ts_ms, findings, may_open)| WalRecord::Reconciled {
                wall_ts_ms,
                findings,
                may_open
            }),
        (
            any::<u16>(),
            any::<u16>(),
            any::<bool>(),
            1u64..=u64::MAX,
            1u32..=u32::MAX,
            any::<i64>()
        )
            .prop_map(|(strategy, symbol, buy, n, d, wall_ts_ms)| {
                WalRecord::SleeveStopSet {
                    strategy: StrategyId(strategy),
                    symbol: SymbolId(symbol),
                    side: if buy { Side::Buy } else { Side::Sell },
                    trigger_price: Exact::from_ratio(&n.to_string(), &d.to_string()).unwrap(),
                    wall_ts_ms,
                }
            }),
    ]
}

fn write(path: &Path, records: &[WalRecord]) -> Vec<u8> {
    let (mut writer, prior) = WalWriter::open_unsynced(path).unwrap();
    assert!(prior.is_empty());
    for (index, record) in records.iter().enumerate() {
        assert_eq!(writer.append(record).unwrap(), index as u64 + 1);
    }
    writer.flush().unwrap();
    drop(writer);
    fs::read(path).unwrap()
}

// Independent framing oracle: no engine codec helper is used to locate a cut.
fn ends(bytes: &[u8]) -> Vec<usize> {
    assert_eq!(&bytes[..8], b"EWAL0001");
    let mut position = 8;
    let mut result = Vec::new();
    while position < bytes.len() {
        let length = u32::from_le_bytes(bytes[position..position + 4].try_into().unwrap()) as usize;
        position += 8 + length;
        result.push(position);
    }
    assert_eq!(position, bytes.len());
    result
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(128))]

    #[test]
    fn real_writer_roundtrips_record_sequences(records in prop::collection::vec(record(), 1..12)) {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("engine.wal");
        let bytes = write(&path, &records);
        prop_assert_eq!(ends(&bytes).len(), records.len());
        let expected: Vec<_> = records.into_iter().enumerate().map(|(i, record)| (i as u64 + 1, record)).collect();
        prop_assert_eq!(&replay(&path).unwrap(), &expected);
        let (writer, restored) = WalWriter::open_unsynced(&path).unwrap();
        prop_assert_eq!(restored, expected);
        drop(writer);
        prop_assert_eq!(fs::read(path).unwrap(), bytes);
    }

    #[test]
    fn arbitrary_crash_cut_recovers_only_complete_frames(records in prop::collection::vec(record(), 1..12), choice in any::<usize>()) {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("engine.wal");
        let bytes = write(&path, &records);
        let cut = 8 + choice % (bytes.len() - 7);
        let boundaries = ends(&bytes);
        let count = boundaries.iter().take_while(|end| **end <= cut).count();
        let good_end = count.checked_sub(1).map_or(8, |i| boundaries[i]);
        fs::write(&path, &bytes[..cut]).unwrap();
        let expected: Vec<_> = records.into_iter().take(count).enumerate().map(|(i, record)| (i as u64 + 1, record)).collect();
        let (recovered, torn) = replay_scan(&path).unwrap();
        prop_assert_eq!(&recovered, &expected);
        prop_assert_eq!(torn, cut != good_end);
        prop_assert_eq!(fs::read(&path).unwrap(), &bytes[..cut]);
        let (mut writer, restored) = WalWriter::open_unsynced(&path).unwrap();
        prop_assert_eq!(&restored, &expected);
        prop_assert_eq!(fs::metadata(&path).unwrap().len(), good_end as u64);
        let marker = WalRecord::OrderIdEpoch { epoch_ms: 999 };
        prop_assert_eq!(writer.append(&marker).unwrap(), count as u64 + 1);
        writer.flush().unwrap();
        drop(writer);
        let mut expected = expected;
        expected.push((count as u64 + 1, marker));
        prop_assert_eq!(replay(&path).unwrap(), expected);
    }

    #[test]
    fn corrupted_payload_is_refused_without_truncation(records in prop::collection::vec(record(), 1..12), frame in any::<usize>(), byte in any::<usize>(), bit in 0u8..8) {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("engine.wal");
        let mut bytes = write(&path, &records);
        let boundaries = ends(&bytes);
        let frame = frame % boundaries.len();
        let start = if frame == 0 { 8 } else { boundaries[frame - 1] };
        let offset = start + 8 + byte % (boundaries[frame] - start - 8);
        bytes[offset] ^= 1 << bit;
        fs::write(&path, &bytes).unwrap();
        prop_assert!(matches!(replay(&path), Err(WalError::Corrupt { .. })), "corrupt frame accepted");
        prop_assert!(matches!(WalWriter::open_unsynced(&path), Err(WalError::Corrupt { .. })), "corrupt frame accepted");
        prop_assert_eq!(fs::read(path).unwrap(), bytes);
    }

    #[test]
    fn checksum_valid_unknown_records_are_refused_without_truncation(suffix in "[a-z]{1,32}") {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("engine.wal");
        let payload = format!("{{\"kind\":\"unsupported_{suffix}\"}}").into_bytes();
        let mut bytes = b"EWAL0001".to_vec();
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
        fs::write(&path, &bytes).unwrap();
        prop_assert!(matches!(replay(&path), Err(WalError::Corrupt { .. })), "corrupt frame accepted");
        prop_assert!(matches!(WalWriter::open_unsynced(&path), Err(WalError::Corrupt { .. })), "corrupt frame accepted");
        prop_assert_eq!(fs::read(path).unwrap(), bytes);
    }
}
