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
