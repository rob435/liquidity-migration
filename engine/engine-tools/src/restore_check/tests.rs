use std::path::{Path, PathBuf};

use engine_types::numeric::{AssetId, Exact};
use engine_types::portfolio::{PortfolioPosition, PortfolioState};
use engine_types::{
    Cause, DecisionCause, Intent, OrderKind, OrderRequest, Side, StrategyId, SymbolId, Wal,
    WalRecord,
};
use serde_json::{json, Value};

use super::*;

const NOW_MS: i64 = 1_800_000_000_000;

fn options(family: &Path) -> Options {
    Options {
        family: family.to_path_buf(),
        spool: None,
        controls: None,
        max_age_min: DEFAULT_MAX_AGE_MIN,
        now_ms: NOW_MS,
    }
}

fn boot(wall_ts_ms: i64) -> WalRecord {
    WalRecord::Boot {
        version: "test".into(),
        config_sha256: "sha".into(),
        wall_ts_ms,
        commit: "commit".into(),
    }
}

/// A restatement carrying `portfolio`, with the quantity projection the
/// reader insists agrees with it.
fn base(portfolio: PortfolioState, wall_ts_ms: i64, may_open: bool) -> WalRecord {
    let attribution: Vec<Value> = portfolio
        .positions
        .iter()
        .map(|row| {
            json!({
                "strategy": row.strategy.0,
                "symbol": row.symbol.0,
                "signed_qty": row.signed_qty.to_f64().unwrap(),
            })
        })
        .collect();
    serde_json::from_value(json!({
        "kind": "segment_base",
        "wall_ts_ms": wall_ts_ms,
        "strategies": ["long"],
        "symbols": ["BTCUSDT"],
        "may_open": may_open,
        "control_anchors": [],
        "attribution": attribution,
        "logged_exposure": [],
        "intended_stops": [],
        "open_orders": [],
        "open_trade_lots": [],
        "legacy_signal_source_retirements": [],
        "portfolio": portfolio,
    }))
    .unwrap()
}

fn held(signed_qty: &str, stop_px: Option<&str>) -> PortfolioState {
    PortfolioState {
        positions: vec![PortfolioPosition {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            signed_qty: Exact::parse_decimal(signed_qty).unwrap(),
            entry_value: None,
            stop_px: stop_px.map(|px| Exact::parse_decimal(px).unwrap()),
            settlement_asset: AssetId::Named("USDT".into()),
        }],
        ..PortfolioState::default()
    }
}

fn order(client_order_id: &str) -> WalRecord {
    WalRecord::OrderSent {
        dispatch: None,
        wire_ns: 1_000,
        arrival_mid: 100.0,
        request: OrderRequest {
            exact_terms: None,
            sleeve_effect: None,
            client_order_id: client_order_id.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 2.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            close_position: false,
        },
    }
}

/// A decision the log never resolved: no verdict, no refusal, no order.
fn undecided_intent() -> WalRecord {
    WalRecord::Intent {
        intent: Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            tag: "long_native_entry".into(),
            decided_ns: 1,
            work: None,
            leverage: None,
        },
        cause: Some(Box::new(DecisionCause {
            callback_wall_ms: NOW_MS,
            callback_id: Some(1),
            causes: vec![Cause::Timer {
                id: engine_types::TimerId(1),
            }],
        })),
    }
}

struct Family {
    _directory: tempfile::TempDir,
    path: PathBuf,
}

fn family(records: &[WalRecord]) -> Family {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("engine.wal");
    let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
    for record in records {
        wal.append(record).unwrap();
    }
    wal.barrier().unwrap();
    Family {
        _directory: directory,
        path,
    }
}

/// Frames written by hand, so a record kind no reader knows can exist.
fn write_raw(path: &Path, values: &[Value]) {
    let mut bytes = b"EWAL0001".to_vec();
    for value in values {
        let payload = serde_json::to_vec(value).unwrap();
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
    }
    std::fs::write(path, bytes).unwrap();
}

#[test]
fn a_flat_current_family_is_ready_to_reconcile() {
    let log = family(&[
        base(PortfolioState::default(), NOW_MS - 60_000, true),
        boot(NOW_MS - 60_000),
    ]);
    let report = read(&options(&log.path));

    assert_eq!(report.verdict, Verdict::ReadyToReconcile);
    assert_eq!(report.verdict.exit_code(), 0);
    assert!(report.decoded);
    assert_eq!(report.newest_trusted_segment, Some(1));
    assert_eq!(report.next_seq, Some(3), "two frames, so the next is three");
    assert_eq!(report.torn_tail, Some(false));
    assert_eq!(report.may_open, Some(true));
    assert_eq!(report.open_orders.as_deref(), Some(&[][..]));
    assert_eq!(report.positions.as_deref(), Some(&[][..]));
    assert_eq!(report.intended_stops, Some(0));
    assert_eq!(report.unresolved_order_decisions, Some(0));
    assert_eq!(report.newest_wall_ts_ms, Some(NOW_MS - 60_000));
    assert_eq!(report.age_min, Some(1));
    assert_eq!(report.stale, Some(false));
    assert_eq!(report.segments.len(), 1);
    assert_eq!(
        report.segments[0].first_record_kind.as_deref(),
        Some(report.reader_segment_base),
        "the wire kind, which is what the reader's own version names"
    );
    assert_eq!(report.segments[0].trusted, Some(true));
    assert_eq!(report.segments[0].torn_tail, Some(false));
    assert_eq!(report.reader_format, "EWAL0001");
    assert_eq!(report.reader_segment_base, "segment_base_v7");
}

#[test]
fn an_attempted_order_with_no_outcome_is_named_and_needs_the_venue() {
    let log = family(&[
        base(PortfolioState::default(), NOW_MS - 60_000, true),
        order("eng-in-flight"),
    ]);
    let report = read(&options(&log.path));

    assert_eq!(report.verdict, Verdict::ReconcileRequired);
    assert_eq!(report.verdict.exit_code(), 3);
    let orders = report.open_orders.clone().unwrap();
    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].client_order_id, "eng-in-flight");
    assert_eq!(orders[0].symbol.as_deref(), Some("BTCUSDT"));
    assert_eq!(orders[0].side, Side::Buy);
    assert_eq!(orders[0].qty, 2.0);
    assert_eq!(orders[0].remaining, Some(2.0));
    assert!(!orders[0].acked, "the venue never answered the send");
    assert!(
        report.table().contains("eng-in-flight"),
        "{}",
        report.table()
    );
}

#[test]
fn a_position_and_its_intended_stop_are_both_listed() {
    let log = family(&[base(held("1.5", Some("90")), NOW_MS - 60_000, false)]);
    let report = read(&options(&log.path));

    assert_eq!(report.verdict, Verdict::ReconcileRequired);
    assert_eq!(report.may_open, Some(false));
    let positions = report.positions.clone().unwrap();
    assert_eq!(positions.len(), 1);
    assert_eq!(positions[0].strategy.as_deref(), Some("long"));
    assert_eq!(positions[0].symbol.as_deref(), Some("BTCUSDT"));
    assert_eq!(positions[0].signed_qty, "1.5");
    assert_eq!(positions[0].intended_stop_px.as_deref(), Some("90"));
    assert_eq!(report.intended_stops, Some(1));
    let table = report.table();
    assert!(table.contains("long BTCUSDT 1.5 stop=90"), "{table}");
}

#[test]
fn a_position_with_no_stop_reads_absent_rather_than_zero() {
    let log = family(&[base(held("1", None), NOW_MS - 60_000, true)]);
    let report = read(&options(&log.path));

    assert_eq!(report.verdict, Verdict::ReconcileRequired);
    assert_eq!(report.positions.clone().unwrap()[0].intended_stop_px, None);
    assert_eq!(report.intended_stops, Some(0));
    assert!(report.table().contains("stop=absent"), "{}", report.table());
}

#[test]
fn an_unresolved_order_decision_needs_the_venue_too() {
    let log = family(&[
        base(PortfolioState::default(), NOW_MS - 60_000, true),
        undecided_intent(),
    ]);
    let report = read(&options(&log.path));

    assert_eq!(report.unresolved_order_decisions, Some(1));
    assert_eq!(report.open_orders.as_deref(), Some(&[][..]));
    assert_eq!(report.positions.as_deref(), Some(&[][..]));
    assert_eq!(report.verdict, Verdict::ReconcileRequired);
}

#[test]
fn a_torn_tail_is_reported_and_the_records_before_it_still_decode() {
    let log = family(&[
        base(PortfolioState::default(), NOW_MS - 60_000, true),
        boot(NOW_MS - 60_000),
    ]);
    let whole = std::fs::read(&log.path).unwrap();
    std::fs::write(&log.path, &whole[..whole.len() - 3]).unwrap();

    let report = read(&options(&log.path));
    assert!(report.decoded, "a torn tail is where a writer would stop");
    assert_eq!(report.torn_tail, Some(true));
    assert_eq!(report.segments[0].torn_tail, Some(true));
    assert_eq!(report.next_seq, Some(2), "the cut frame is not one of them");
    assert_eq!(report.may_open, Some(true));
    assert_eq!(report.verdict, Verdict::ReadyToReconcile);
}

#[test]
fn an_old_newest_stamp_is_a_stale_backup() {
    let log = family(&[base(PortfolioState::default(), NOW_MS - 90 * 60_000, true)]);
    let report = read(&options(&log.path));

    assert_eq!(report.age_min, Some(90));
    assert_eq!(report.stale, Some(true));
    assert_eq!(report.verdict, Verdict::StaleBackup);
    assert_eq!(report.verdict.exit_code(), 4);

    let generous = Options {
        max_age_min: 120,
        ..options(&log.path)
    };
    assert_eq!(read(&generous).verdict, Verdict::ReadyToReconcile);
}

#[test]
fn a_stale_backup_outranks_the_exposure_it_also_holds() {
    let log = family(&[base(held("1", Some("90")), NOW_MS - 90 * 60_000, true)]);
    let report = read(&options(&log.path));

    assert_eq!(report.verdict, Verdict::StaleBackup);
    assert_eq!(report.positions.unwrap().len(), 1);
}

#[test]
fn a_record_version_this_reader_refuses_is_an_incompatible_reader() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("engine.wal");
    write_raw(
        &path,
        &[json!({
            "kind": "segment_base_v9",
            "wall_ts_ms": NOW_MS,
            "strategies": [],
            "symbols": [],
            "may_open": true,
        })],
    );

    let report = read(&options(&path));
    assert_eq!(report.verdict, Verdict::IncompatibleReader);
    assert_eq!(report.verdict.exit_code(), 2);
    assert!(!report.decoded);
    let error = report.error.clone().unwrap();
    assert!(
        error.contains("unsupported WAL segment kind segment_base_v9"),
        "{error}"
    );
    assert_eq!(report.segments.len(), 1, "the file is still listed");
    assert_eq!(report.segments[0].trusted, None);
    assert_eq!(report.segments[0].first_record_kind, None);
    assert_eq!(report.may_open, None, "unknown, not a default");
    assert_eq!(report.open_orders, None);
    assert_eq!(report.positions, None);
}

#[test]
fn a_family_that_is_not_there_is_unreadable() {
    let directory = tempfile::tempdir().unwrap();
    let report = read(&options(&directory.path().join("engine.wal")));

    assert_eq!(report.verdict, Verdict::Unreadable);
    assert_eq!(report.verdict.exit_code(), 1);
    assert!(report.segments.is_empty());
    assert!(
        report
            .error
            .clone()
            .unwrap()
            .contains("no log segment under"),
        "{:?}",
        report.error
    );
}

#[test]
fn an_absent_spool_is_missing_and_an_unnamed_one_is_not_checked() {
    let log = family(&[base(PortfolioState::default(), NOW_MS - 60_000, true)]);

    let unnamed = read(&options(&log.path));
    assert_eq!(unnamed.spool.directory.state, "not-checked");
    assert_eq!(unnamed.spool.directory.path, None);
    assert_eq!(unnamed.spool.directory.files, None);
    assert_eq!(unnamed.controls.state, "not-checked");

    let gone = log.path.parent().unwrap().join("signals");
    let report = read(&Options {
        spool: Some(gone.clone()),
        controls: Some(gone.clone()),
        ..options(&log.path)
    });
    assert_eq!(report.spool.directory.state, "missing");
    assert_eq!(report.spool.directory.path.as_deref(), Some(gone.as_path()));
    assert_eq!(report.spool.directory.files, None, "missing is not empty");
    assert_eq!(report.spool.newest_sequence, None);
    assert_eq!(report.controls.state, "missing");
    assert_eq!(report.controls.files, None);
}

#[test]
fn the_spool_counts_deliverable_rows_readiness_files_and_a_quarantine() {
    let log = family(&[base(PortfolioState::default(), NOW_MS - 60_000, true)]);
    let root = log.path.parent().unwrap();
    let spool = root.join("signals");
    std::fs::create_dir_all(spool.join("quarantine")).unwrap();
    for sequence in [7_u64, 41] {
        std::fs::write(
            spool.join(format!("{sequence:020}-{:0>64}.json", "a")),
            b"{}",
        )
        .unwrap();
    }
    std::fs::write(
        spool.join(engine_types::SIGNAL_READINESS_REQUEST_FILE),
        b"{}",
    )
    .unwrap();
    std::fs::write(spool.join("quarantine").join("refused.json"), b"{}").unwrap();
    let controls = root.join("controls");
    std::fs::create_dir_all(&controls).unwrap();
    std::fs::write(controls.join("request.json"), b"{}").unwrap();

    let report = read(&Options {
        spool: Some(spool),
        controls: Some(controls),
        ..options(&log.path)
    });
    assert_eq!(report.spool.directory.state, "present");
    assert_eq!(report.spool.directory.files, Some(2));
    assert_eq!(report.spool.readiness_files, Some(1));
    assert_eq!(report.spool.quarantined, Some(1));
    assert_eq!(report.spool.newest_sequence, Some(41));
    assert_eq!(report.controls.state, "present");
    assert_eq!(report.controls.files, Some(1));
    assert!(
        report.table().contains("quarantined=1 newest_sequence=41"),
        "{}",
        report.table()
    );
}

#[test]
fn a_spool_with_no_quarantine_directory_says_unknown_rather_than_none_quarantined() {
    let log = family(&[base(PortfolioState::default(), NOW_MS - 60_000, true)]);
    let spool = log.path.parent().unwrap().join("signals");
    std::fs::create_dir_all(&spool).unwrap();

    let report = read(&Options {
        spool: Some(spool),
        ..options(&log.path)
    });
    assert_eq!(report.spool.directory.files, Some(0));
    assert_eq!(report.spool.quarantined, None);
    assert!(
        report.table().contains("quarantined=unknown"),
        "{}",
        report.table()
    );
}

#[test]
fn the_json_report_round_trips_and_names_the_verdict_the_exit_code_means() {
    let log = family(&[base(held("1", Some("90")), NOW_MS - 60_000, true)]);
    let report = read(&options(&log.path));
    let text = serde_json::to_string_pretty(&report).unwrap();
    let value: Value = serde_json::from_str(&text).unwrap();

    assert_eq!(value["verdict"], "reconcile-required");
    assert_eq!(value["family"], log.path.to_str().unwrap());
    assert_eq!(value["reader_format"], "EWAL0001");
    assert_eq!(value["decoded"], true);
    assert_eq!(value["may_open"], true);
    assert_eq!(value["intended_stops"], 1);
    assert_eq!(value["unresolved_order_decisions"], 0);
    assert_eq!(value["positions"][0]["signed_qty"], "1");
    assert_eq!(value["positions"][0]["intended_stop_px"], "90");
    assert_eq!(value["positions"][0]["symbol"], "BTCUSDT");
    assert_eq!(value["open_orders"].as_array().unwrap().len(), 0);
    assert_eq!(value["segments"][0]["trusted"], true);
    assert_eq!(value["segments"][0]["torn_tail"], false);
    assert_eq!(value["spool"]["state"], "not-checked");
    assert_eq!(value["controls"]["state"], "not-checked");
    assert_eq!(value["max_age_min"], DEFAULT_MAX_AGE_MIN);
    assert_eq!(value["stale"], false);

    for (verdict, code) in [
        (Verdict::ReadyToReconcile, 0),
        (Verdict::Unreadable, 1),
        (Verdict::IncompatibleReader, 2),
        (Verdict::ReconcileRequired, 3),
        (Verdict::StaleBackup, 4),
    ] {
        assert_eq!(verdict.exit_code(), code);
        assert_eq!(
            serde_json::to_value(verdict).unwrap(),
            Value::String(verdict.as_str().into())
        );
    }
}

#[test]
fn an_abandoned_rotation_is_untrusted_and_the_segment_below_it_is_folded() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("engine.wal");
    let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
    wal.append(&base(held("1", Some("90")), NOW_MS - 60_000, true))
        .unwrap();
    wal.barrier().unwrap();
    drop(wal);
    // A rotation that died inside its own header: eight bytes of nothing.
    std::fs::write(directory.path().join("engine.wal.000002"), [0u8; 8]).unwrap();

    let report = read(&options(&path));
    assert_eq!(report.segments.len(), 2);
    assert_eq!(report.segments[1].trusted, Some(false));
    assert_eq!(report.segments[1].first_record_kind, None);
    assert_eq!(report.newest_trusted_segment, Some(1));
    assert_eq!(report.positions.clone().unwrap().len(), 1);
    assert_eq!(report.verdict, Verdict::ReconcileRequired);
}
