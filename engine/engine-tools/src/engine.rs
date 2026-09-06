//! The funded runtime. Operator commands execute in the companion tools binary.

use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, ExitCode};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.as_slice() == ["--strategy-worker"] {
        return match engine_core::strategy_process::worker::run_stdio() {
            Ok(()) => ExitCode::SUCCESS,
            Err(_) => ExitCode::FAILURE,
        };
    }
    if args.is_empty() || matches!(args[0].as_str(), "help" | "--help" | "-h") {
        println!("engine run --config PATH\nRun the execution engine.\n\nOperator commands: engine-tools --help\nExisting engine COMMAND invocations execute the companion engine-tools binary.");
        return ExitCode::SUCCESS;
    }
    if args[0] != "run" {
        let executable = match std::env::current_exe() {
            Ok(path) => path.with_file_name("engine-tools"),
            Err(error) => {
                eprintln!("engine: cannot locate companion tools: {error}");
                return ExitCode::FAILURE;
            }
        };
        let error = Command::new(&executable).args(args).exec();
        eprintln!(
            "engine: cannot execute {}: {error}; build/install both engine-tools package binaries",
            executable.display()
        );
        return ExitCode::FAILURE;
    }
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_target(false)
        .with_ansi(std::io::IsTerminal::is_terminal(&std::io::stdout()))
        .init();
    let config = args
        .windows(2)
        .find(|pair| pair[0] == "--config")
        .map(|pair| PathBuf::from(&pair[1]))
        .unwrap_or_else(|| "engine.toml".into());
    let result = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| error.to_string())
        .and_then(|runtime| {
            runtime
                .block_on(engine_core::runner::run(&config))
                .map_err(|error| error.to_string())
        });
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("engine: {error}");
            ExitCode::FAILURE
        }
    }
}
