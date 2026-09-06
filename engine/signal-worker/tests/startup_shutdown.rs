#![cfg(unix)]

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use engine_types::{SignalLifecycleRequest, SignalLifecycleResponse};
use serde_json::Value;
use signal_worker::store::AtomicJsonStore;
use signal_worker::SignalWorkerConfig;

struct Fixture {
    root: PathBuf,
    child: Option<Child>,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn snapshot(path: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut files = BTreeMap::new();
    for entry in fs::read_dir(path).unwrap() {
        let path = entry.unwrap().path();
        if path.is_file() {
            files.insert(path.clone(), fs::read(path).unwrap());
        } else if path.is_dir() {
            files.extend(snapshot(&path));
        }
    }
    files
}

fn shutdown_while_waiting_for_engine(sealed: bool) {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let config_paths = [
        ("--signal-config", "configs/signal-worker.demo.json"),
        ("--long-rule", "configs/long_native_v12.json"),
        ("--carry-config", "configs/lane2_carry_hold_v7.json"),
        ("--operational-config", "configs/operational.json"),
        ("--engine-config", "deploy/engine.demo.toml.template"),
    ];
    let config = SignalWorkerConfig::load(
        repo.join(config_paths[0].1),
        repo.join(config_paths[1].1),
        repo.join(config_paths[2].1),
        repo.join(config_paths[3].1),
        repo.join(config_paths[4].1),
    )
    .unwrap();
    let root = std::env::temp_dir().join(format!(
        "worker-startup-stop-{}-{}",
        std::process::id(),
        engine_types::clock::mono_ns()
    ));
    let state = root.join("state");
    let spool = root.join("spool");
    let heartbeat = root.join("heartbeat.json");
    fs::create_dir_all(&spool).unwrap();
    if sealed {
        AtomicJsonStore::new(spool.join(engine_types::SIGNAL_READINESS_REQUEST_FILE))
            .save(&SignalLifecycleRequest {
                schema_version: 2,
                boot_nonce: "await-successor".into(),
                sleeve_keys: [&config.routing.carry_sleeve, &config.routing.long_sleeve]
                    .into_iter()
                    .map(|key| engine_types::identity::SleeveKey::new(key.clone()).unwrap())
                    .collect(),
                producers: Vec::new(),
                legacy_sources: Vec::new(),
            })
            .unwrap();
    }
    let mut fixture = Fixture { root, child: None };
    let mut generation = None;
    for restart in 0..2 {
        let log_path = fixture.root.join(format!("run-{restart}.log"));
        let log = fs::File::create(&log_path).unwrap();
        let mut command = Command::new(env!("CARGO_BIN_EXE_signal-worker"));
        command.arg("live");
        for (flag, path) in config_paths {
            command.arg(flag).arg(repo.join(path));
        }
        command
            .arg("--state-dir")
            .arg(&state)
            .arg("--spool-dir")
            .arg(&spool)
            .arg("--heartbeat")
            .arg(&heartbeat)
            .stdout(Stdio::null())
            .stderr(log);
        fixture.child = Some(command.spawn().unwrap());
        let child = fixture.child.as_mut().unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        let starting = loop {
            assert!(
                child.try_wait().unwrap().is_none(),
                "{}",
                fs::read_to_string(&log_path).unwrap()
            );
            if let Ok(bytes) = fs::read(&heartbeat) {
                let current: Value = serde_json::from_slice(&bytes).unwrap();
                if current["pid"] == child.id()
                    && current["status"] == "starting"
                    && current["source_generation"]
                        .as_str()
                        .is_some_and(|id| !id.is_empty())
                    && (!sealed
                        || spool
                            .join(engine_types::SIGNAL_READINESS_RESPONSE_FILE)
                            .exists())
                {
                    break current;
                }
            }
            assert!(
                Instant::now() < deadline,
                "worker did not reach engine readiness wait"
            );
            std::thread::sleep(Duration::from_millis(10));
        };
        if let Some(expected) = &generation {
            assert_eq!(&starting["source_generation"], expected);
        } else {
            generation = Some(starting["source_generation"].clone());
        }
        if sealed {
            let response: SignalLifecycleResponse =
                AtomicJsonStore::new(spool.join(engine_types::SIGNAL_READINESS_RESPONSE_FILE))
                    .load()
                    .unwrap()
                    .unwrap();
            assert!(response.producer.sealed);
            assert!(response
                .producer
                .sources
                .iter()
                .all(|source| source.published_through == 0));
        }
        let original = snapshot(&state);
        let sent_at = Instant::now();
        assert!(Command::new("/bin/kill")
            .args(["-TERM", &child.id().to_string()])
            .status()
            .unwrap()
            .success());
        let status = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            assert!(sent_at.elapsed() < Duration::from_secs(2), "SIGTERM was ignored while engine readiness was pending (sealed={sealed}, restart={restart}); heartbeat={}; log={}", fs::read_to_string(&heartbeat).unwrap(), fs::read_to_string(&log_path).unwrap());
            std::thread::sleep(Duration::from_millis(10));
        };
        assert!(status.success(), "worker did not exit cleanly: {status}");
        let stopped: Value = serde_json::from_slice(&fs::read(&heartbeat).unwrap()).unwrap();
        assert_eq!(stopped["status"], "stopped");
        assert_eq!(stopped["source_generation"], starting["source_generation"]);
        assert_eq!(stopped["last_input_sequence"], 0);
        assert_eq!(stopped["long_output_sequence"], 0);
        assert_eq!(stopped["carry_output_sequence"], 0);
        assert_eq!(snapshot(&state), original, "shutdown changed durable state");
        eprintln!(
            "worker_shutdown_ms={} sealed={sealed} restart={restart}",
            sent_at.elapsed().as_millis()
        );
    }
}

#[test]
fn sigterm_stops_worker_waiting_for_the_engine_registry_and_after_restart() {
    shutdown_while_waiting_for_engine(false);
}

#[test]
fn sigterm_stops_worker_waiting_for_a_successor_grant_and_after_restart() {
    shutdown_while_waiting_for_engine(true);
}

#[test]
fn shutdown_received_after_recovery_remains_pending_until_run() {
    const CHILD: &str = "SIGNAL_WORKER_SHUTDOWN_HANDOFF_CHILD";
    if std::env::var_os(CHILD).is_none() {
        let output = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "shutdown_received_after_recovery_remains_pending_until_run",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let config = SignalWorkerConfig::load(
        repo.join("configs/signal-worker.demo.json"),
        repo.join("configs/long_native_v12.json"),
        repo.join("configs/lane2_carry_hold_v7.json"),
        repo.join("configs/operational.json"),
        repo.join("deploy/engine.demo.toml.template"),
    )
    .unwrap();
    let fixture = Fixture {
        root: std::env::temp_dir().join(format!("worker-shutdown-handoff-{}", std::process::id())),
        child: None,
    };
    let heartbeat = fixture.root.join("heartbeat.json");
    tokio::runtime::Runtime::new().unwrap().block_on(async {
        let runner = signal_worker::live::LiveRunner::open_responsive(
            config,
            signal_worker::live::LiveRunOptions {
                state_dir: fixture.root.join("state"),
                spool_dir: fixture.root.join("spool"),
                heartbeat: heartbeat.clone(),
            },
        )
        .await
        .unwrap()
        .unwrap();
        assert!(Command::new("/bin/kill")
            .args(["-TERM", &std::process::id().to_string()])
            .status()
            .unwrap()
            .success());
        tokio::time::sleep(Duration::from_millis(50)).await;
        tokio::time::timeout(Duration::from_secs(2), runner.run())
            .await
            .expect("the recovery-to-run handoff discarded SIGTERM")
            .unwrap();
    });
    let stopped: Value = serde_json::from_slice(&fs::read(heartbeat).unwrap()).unwrap();
    assert_eq!(stopped["status"], "stopped");
}
