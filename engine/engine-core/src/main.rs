//! The engine binary: `run`, `bench`, `replay`.
//!
//! One thread. The runtime is tokio's current-thread build on purpose — the
//! whole point of the design is that a market message is turned into an order
//! without ever leaving the thread it arrived on.

use std::error::Error;
use std::path::PathBuf;
use std::process::ExitCode;

use engine_core::bench::{self, BenchOptions};
use engine_core::execution;
use engine_core::replay;
use engine_core::runner;

const USAGE: &str = "\
engine — the execution loop

  engine run --config engine.toml [--live]
      Run the engine. Shadow (compute and log, send nothing) unless --live.

  engine bench [--events N] [--rate PER_SEC] [--every N] [--symbols A,B]
               [--wal PATH]
      Measure the whole chain on this box against a pretend venue.

  engine replay --wal PATH
      Print the log in words, and what was still in flight at each point.

  engine fills --wal PATH
      What the trading cost: maker share, fee, how far each fill landed from
      the price on the screen when its order left, and where the market went
      afterwards. Per sleeve and symbol.
";

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_target(false)
        .init();

    let args: Vec<String> = std::env::args().skip(1).collect();
    match dispatch(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("engine: {e}");
            ExitCode::FAILURE
        }
    }
}

fn dispatch(args: &[String]) -> Result<(), Box<dyn Error>> {
    let Some(command) = args.first().map(String::as_str) else {
        print!("{USAGE}");
        return Ok(());
    };
    match command {
        "run" => {
            let config = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            let live = args.iter().any(|a| a == "--live");
            runtime()?.block_on(runner::run(&config, live))
        }
        "bench" => {
            let mut options = BenchOptions::default();
            if let Some(v) = value(args, "--events") {
                options.events = v.parse()?;
            }
            if let Some(v) = value(args, "--rate") {
                options.rate = v.parse()?;
            }
            if let Some(v) = value(args, "--every") {
                options.every_nth = v.parse()?;
            }
            if let Some(v) = value(args, "--symbols") {
                options.symbols = v.split(',').map(|s| s.trim().to_string()).collect();
            }
            if let Some(v) = value(args, "--wal") {
                options.wal_path = PathBuf::from(v);
            }
            let result = runtime()?.block_on(bench::run(&options))?;
            println!(
                "\nbench: {} quotes in, {} orders out, against a pretend venue on this box",
                result.events, result.orders
            );
            println!("{}", result.table());
            println!(
                "  the fsync is inside \"write it down\"; \"venue answers\" is a local socket,\n  \
                 so the real venue's ~175ms round trip is not in these numbers."
            );
            Ok(())
        }
        "replay" => {
            let path = value(args, "--wal").ok_or("replay needs --wal PATH")?;
            let report = replay::read(&PathBuf::from(path))?;
            for line in &report.lines {
                println!("{line}");
            }
            println!(
                "\n{} records; {} still out there at the end: {}",
                report.records,
                report.in_flight.len(),
                replay::listed(&report.in_flight)
            );
            Ok(())
        }
        "fills" => {
            let path = value(args, "--wal").ok_or("fills needs --wal PATH")?;
            let (replayed, torn) = engine_wal::replay_scan(&PathBuf::from(path))?;
            let records: Vec<_> = replayed.into_iter().map(|(_, r)| r).collect();
            print!("{}", execution::report::of_log(&records));
            if torn {
                println!(
                    "\n  the log ends part-way through a record; anything after that point is \
                     not in these numbers."
                );
            }
            Ok(())
        }
        "-h" | "--help" | "help" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown command {other}\n\n{USAGE}").into()),
    }
}

fn runtime() -> Result<tokio::runtime::Runtime, Box<dyn Error>> {
    Ok(tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?)
}

fn value(args: &[String], flag: &str) -> Option<String> {
    let at = args.iter().position(|a| a == flag)?;
    args.get(at + 1).cloned()
}
