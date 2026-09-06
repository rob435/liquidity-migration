use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::{Duration, Instant};

use engine_types::{RuntimeControlCommand, RuntimeControlRequest, StrategyId, Wal, WalRecord};

struct Fixture {
    root: PathBuf,
    wal: PathBuf,
    config: PathBuf,
    spool: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "runtime-control-cli-{}-{}",
            std::process::id(),
            engine_types::clock::mono_ns()
        ));
        fs::create_dir_all(&root).unwrap();
        let wal = root.join("engine.wal");
        let config = root.join("engine.toml");
        let spool = root.join("control");
        fs::create_dir(&spool).unwrap();
        fs::write(&config, format!("[engine]\nwal_path = {wal:?}\ncontrol_spool_path = {spool:?}\n[[strategy]]\nname = 'probe'\nsleeve = 'alpha'\n[[strategy]]\nname = 'probe'\nsleeve = 'old'\n")).unwrap();
        Self {
            root,
            wal,
            config,
            spool,
        }
    }
    fn segment(&self, index: usize) -> PathBuf {
        self.root.join(format!("engine.wal.{index:06}"))
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn base() -> WalRecord {
    serde_json::from_value(serde_json::json!({
        "kind":"segment_base", "wall_ts_ms":100, "strategies":["old","alpha"], "symbols":[],
        "may_open":false, "control_anchors":[], "attribution":[], "logged_exposure":[],
        "intended_stops":[], "open_orders":[], "open_trade_lots":[],
        "portfolio":engine_types::portfolio::PortfolioState::default(),
        "identities":{"schema_version":1,"scope":null,"sleeves":["old","alpha"],"instruments":[]}
    }))
    .unwrap()
}

fn cli(fixture: &Fixture, request_id: &str) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_engine"));
    command
        .args(["set-strategy-entry-permission", "--config"])
        .arg(&fixture.config)
        .args([
            "--strategy",
            "alpha",
            "--entries-enabled",
            "false",
            "--request-id",
            request_id,
            "--wait-ms",
            "5000",
        ]);
    command
}

fn acknowledge(spool: &Path, done: &AtomicBool) -> Result<RuntimeControlRequest, String> {
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        for entry in fs::read_dir(spool).map_err(|e| e.to_string())? {
            let path = entry.map_err(|e| e.to_string())?.path();
            if path
                .extension()
                .is_some_and(|extension| extension == "json")
            {
                let request: RuntimeControlRequest =
                    serde_json::from_slice(&fs::read(&path).map_err(|e| e.to_string())?)
                        .map_err(|e| e.to_string())?;
                engine_core::controls::validate(&request)?;
                fs::remove_file(path).map_err(|e| e.to_string())?;
                return Ok(request);
            }
        }
        if done.load(Ordering::Relaxed) || Instant::now() >= deadline {
            return Err("CLI exited or timed out before publishing a request".into());
        }
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn accepted_control_rss(fixture: &Fixture, request_id: &str) -> u64 {
    let cli = cli(fixture, request_id);
    let mut command = Command::new("/usr/bin/time");
    command.arg(if cfg!(target_os = "macos") {
        "-l"
    } else {
        "-v"
    });
    command
        .arg(cli.get_program())
        .args(cli.get_args())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command.spawn().unwrap();
    let done = Arc::new(AtomicBool::new(false));
    let finished = done.clone();
    let spool = fixture.spool.clone();
    let ack = std::thread::spawn(move || acknowledge(&spool, &finished));
    let output = child.wait_with_output().unwrap();
    done.store(true, Ordering::Relaxed);
    let request = ack.join().unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(output.status.success(), "CLI failed: {stderr}");
    let request = request.unwrap();
    assert_eq!(
        request.strategy,
        StrategyId(1),
        "config order replaced the durable sleeve ID"
    );
    assert_eq!(request.strategy_name, "alpha");
    assert_eq!(request.request_id, request_id);
    assert_eq!(
        request.command,
        RuntimeControlCommand::SetEntriesEnabled {
            entries_enabled: false
        }
    );
    let rss = stderr
        .lines()
        .find_map(|line| {
            if cfg!(target_os = "macos") && line.contains("maximum resident set size") {
                return line
                    .split_whitespace()
                    .next()
                    .and_then(|value| value.parse::<u64>().ok());
            }
            if cfg!(target_os = "linux") && line.contains("Maximum resident set size (kbytes)") {
                return line
                    .rsplit(':')
                    .next()
                    .and_then(|value| value.trim().parse::<u64>().ok())
                    .map(|kb| kb * 1024);
            }
            None
        })
        .unwrap_or_else(|| panic!("time did not report child RSS: {stderr}"));
    eprintln!("runtime_control_child_max_rss_bytes={rss} request={request_id}");
    rss
}

#[test]
#[cfg(any(target_os = "macos", target_os = "linux"))]
fn runtime_control_cli_bounds_history_memory_and_keeps_rotated_identity_and_torn_refusal() {
    let fixture = Fixture::new();
    let (mut wal, _) = engine_wal::WalWriter::open(&fixture.wal).unwrap();
    let note = WalRecord::Note {
        source: "retained-archive".into(),
        text: "x".repeat(1024 * 1024),
    };
    for _ in 0..128 {
        wal.append(&note).unwrap();
    }
    wal.barrier().unwrap();
    assert!(wal.rotate(&base()).unwrap());
    drop(wal);
    assert!(fs::metadata(&fixture.wal).unwrap().len() >= 128 * 1024 * 1024);
    assert!(fs::metadata(fixture.segment(2)).unwrap().len() < 64 * 1024);
    let rss = accepted_control_rss(&fixture, "bounded-history");
    assert!(
        rss < 96 * 1024 * 1024,
        "CLI loaded retained history: peak RSS {rss} bytes"
    );

    fs::write(fixture.segment(3), b"EWAL").unwrap();
    let current = fs::read(fixture.segment(2)).unwrap();
    let rss = accepted_control_rss(&fixture, "incomplete-rotation");
    assert!(
        rss < 96 * 1024 * 1024,
        "fallback loaded retained history: peak RSS {rss} bytes"
    );
    assert_eq!(fs::read(fixture.segment(2)).unwrap(), current);
    assert_eq!(fs::read(fixture.segment(3)).unwrap(), b"EWAL");

    fs::OpenOptions::new()
        .append(true)
        .open(fixture.segment(2))
        .unwrap()
        .write_all(&[1, 2])
        .unwrap();
    let torn = fs::read(fixture.segment(2)).unwrap();
    let output = cli(&fixture, "torn-identity").output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .contains("runtime control waits for a complete WAL identity frame"));
    assert!(!fs::read_dir(&fixture.spool).unwrap().any(|entry| entry
        .unwrap()
        .path()
        .extension()
        .is_some_and(|extension| extension == "json")));
    assert_eq!(fs::read(fixture.segment(2)).unwrap(), torn);
    assert_eq!(fs::read(fixture.segment(3)).unwrap(), b"EWAL");
}
