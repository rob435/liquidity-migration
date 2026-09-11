use std::process::{Command, Output};

const ENGINE: &str = env!("CARGO_BIN_EXE_engine");
const TOOLS: &str = env!("CARGO_BIN_EXE_engine-tools");

fn invoke(executable: &str, args: &[&str]) -> Output {
    Command::new(executable)
        .args(args)
        .env("RUST_LOG", "off")
        .output()
        .expect("execute the built binary")
}

#[test]
fn legacy_venue_command_matches_the_companion_tools_output() {
    let direct = invoke(TOOLS, &["venues"]);
    let forwarded = invoke(ENGINE, &["venues"]);
    assert!(direct.status.success(), "{direct:?}");
    assert!(forwarded.status.success(), "{forwarded:?}");
    assert_eq!(forwarded.stdout, direct.stdout);
    assert_eq!(forwarded.stderr, direct.stderr);
    let output = String::from_utf8(direct.stdout).unwrap();
    assert!(output.starts_with("name\tvenue\trealm\treal_money\treadiness\n"));
    for name in engine_venue::VenueName::ALL {
        let listed = output
            .lines()
            .any(|row| row.split('\t').next() == Some(name.as_str()));
        assert_eq!(
            listed,
            name.compiled(),
            "compiled feature mismatch for {name}: {output}"
        );
    }
}

#[test]
fn both_entry_points_report_the_runtime_config_error() {
    let directory = tempfile::tempdir().unwrap();
    let missing = directory.path().join("missing config.toml");
    let path = missing.to_str().unwrap();
    let direct = invoke(ENGINE, &["run", "--config", path]);
    let forwarded = invoke(TOOLS, &["run", "--config", path]);
    assert!(!direct.status.success(), "{direct:?}");
    assert_eq!(direct.status.code(), forwarded.status.code());
    assert_eq!(forwarded.stdout, direct.stdout);
    assert_eq!(forwarded.stderr, direct.stderr);
    let error = String::from_utf8(direct.stderr).unwrap();
    assert!(error.contains(&format!("cannot read {path}:")), "{error}");
    assert!(!missing.exists());
}

#[test]
fn unknown_commands_keep_the_companion_error_and_failure_status() {
    let direct = invoke(TOOLS, &["unknown-routing-command"]);
    let forwarded = invoke(ENGINE, &["unknown-routing-command"]);
    assert!(!direct.status.success(), "{direct:?}");
    assert_eq!(direct.status.code(), forwarded.status.code());
    assert_eq!(forwarded.stdout, direct.stdout);
    assert_eq!(forwarded.stderr, direct.stderr);
    let error = String::from_utf8(direct.stderr).unwrap();
    assert!(
        error.contains("unknown command unknown-routing-command"),
        "{error}"
    );
}

#[test]
fn the_bench_quota_dial_is_refused_without_its_workload_through_both_entry_points() {
    let direct = invoke(TOOLS, &["bench", "--quota"]);
    let forwarded = invoke(ENGINE, &["bench", "--quota"]);
    assert!(!direct.status.success(), "{direct:?}");
    assert_eq!(direct.status.code(), forwarded.status.code());
    assert_eq!(forwarded.stdout, direct.stdout);
    assert_eq!(forwarded.stderr, direct.stderr);
    let error = String::from_utf8(direct.stderr).unwrap();
    assert!(error.contains("--quota is a --contention dial"), "{error}");
}

#[test]
fn wal_conversion_cli_creates_a_separate_family_through_both_entry_points() {
    let directory = tempfile::tempdir().unwrap();
    let input = directory.path().join("engine.wal");
    let base: engine_types::WalRecord = serde_json::from_value(serde_json::json!({
        "kind":"segment_base", "wall_ts_ms":0, "strategies":[], "symbols":[],
        "may_open":false, "control_anchors":[], "attribution":[], "logged_exposure":[],
        "intended_stops":[], "open_orders":[],
        "portfolio":engine_types::portfolio::PortfolioState::default()
    }))
    .unwrap();
    let mut value = serde_json::to_value(base).unwrap();
    value["kind"] = "segment_base_v5".into();
    value.as_object_mut().unwrap().remove("open_trade_lots");
    value
        .as_object_mut()
        .unwrap()
        .remove("legacy_signal_source_retirements");
    let payload = serde_json::to_vec(&value).unwrap();
    let mut bytes = b"EWAL0001".to_vec();
    bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
    bytes.extend_from_slice(&payload);
    std::fs::write(&input, &bytes).unwrap();
    for (executable, name) in [(TOOLS, "direct"), (ENGINE, "forwarded")] {
        let output_dir = directory.path().join(name);
        let output = invoke(
            executable,
            &[
                "wal-convert-v5",
                "--wal",
                input.to_str().unwrap(),
                "--output-dir",
                output_dir.to_str().unwrap(),
            ],
        );
        assert!(output.status.success(), "{output:?}");
        assert!(String::from_utf8(output.stdout)
            .unwrap()
            .contains("upgraded_bases=1"));
        let converted = output_dir.join("engine.wal");
        let (records, torn) = engine_wal::replay_current(&converted).unwrap();
        assert!(!torn);
        assert!(
            matches!(&records[0].1, engine_types::WalRecord::SegmentBase { open_trade_lots: Some(lots), .. } if lots.is_empty())
        );
        assert_eq!(std::fs::read(&input).unwrap(), bytes);
    }
}

#[test]
fn latency_folds_the_whole_family_into_the_library_table() {
    use engine_wal::Wal;

    let timing = |operation: &str, queued_ns: u64| engine_types::WalRecord::VenueTiming {
        command_id: queued_ns,
        operation: operation.to_string(),
        client_order_id: format!("eng-{queued_ns}"),
        queued_ns,
        task_started_ns: queued_ns + 100,
        socket_write_ns: Some(queued_ns + 200),
        ack_ns: Some(queued_ns + 300),
        rate_wait_ns: Some(50),
        task_completed_ns: queued_ns + 400,
        core_handled_ns: queued_ns + 500,
        core_handled_wall_ns: 1_700_000_000_000_000_000,
    };
    let base: engine_types::WalRecord = serde_json::from_value(serde_json::json!({
        "kind":"segment_base", "wall_ts_ms":0, "strategies":[], "symbols":[],
        "may_open":true, "control_anchors":[], "attribution":[], "logged_exposure":[],
        "intended_stops":[], "open_orders":[], "open_trade_lots":[],
        "legacy_signal_source_retirements":[],
        "portfolio":engine_types::portfolio::PortfolioState::default()
    }))
    .unwrap();

    let directory = tempfile::tempdir().unwrap();
    let family = directory.path().join("engine.wal");
    let (mut wal, _) = engine_wal::WalWriter::open(&family).unwrap();
    wal.append(&timing("place", 1_000)).unwrap();
    wal.append(&timing("cancel", 2_000)).unwrap();
    wal.barrier().unwrap();
    assert!(wal.rotate(&base).unwrap());
    wal.append(&timing("place", 3_000)).unwrap();
    wal.barrier().unwrap();
    drop(wal);

    let (replayed, _) = engine_wal::replay_chain(&family).unwrap();
    let records: Vec<_> = replayed.into_iter().map(|(_, record)| record).collect();
    let expected = engine_tools::timing::of_log(&records);

    let output = invoke(TOOLS, &["latency", "--wal", family.to_str().unwrap()]);
    assert!(output.status.success(), "{output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.starts_with(&expected), "{text}\n---\n{expected}");
    assert!(
        text.contains(&format!(
            "{} record(s), from 2 log segment(s)",
            records.len()
        )),
        "{text}"
    );

    // A torn tail on a trusted segment: the whole records still count, and
    // the reader names the segment rather than the end of the log.
    let second = directory.path().join("engine.wal.000002");
    let whole = std::fs::read(&second).unwrap();
    std::fs::write(&second, &whole[..whole.len() - 2]).unwrap();
    let output = invoke(TOOLS, &["latency", "--wal", family.to_str().unwrap()]);
    assert!(output.status.success(), "{output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(
        text.contains(&format!(
            "{} record(s), from 2 log segment(s)",
            records.len() - 1
        )),
        "{text}"
    );
    assert!(
        text.contains("a log segment ends part-way through a record"),
        "{text}"
    );
}
