use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};

use serde::Serialize;
use signal_worker::live::{LiveRunOptions, LiveRunner};
use signal_worker::universe::load_candidate_universe;
use signal_worker::{SignalWorker, SignalWorkerConfig, WireEvent, WorkerError};

const USAGE: &str = "\
usage:
  signal-worker check-config --signal-config PATH --long-rule PATH --carry-config PATH --operational-config PATH --engine-config PATH --universe PATH
  signal-worker replay --signal-config PATH --long-rule PATH --carry-config PATH --operational-config PATH --engine-config PATH --universe PATH --input PATH|- --output PATH|-
  signal-worker live --signal-config PATH --long-rule PATH --carry-config PATH --operational-config PATH --engine-config PATH --universe PATH --spool-dir PATH --state-dir PATH --heartbeat PATH";

#[derive(Clone, Debug)]
struct CommonArgs {
    signal_config: PathBuf,
    long_rule: PathBuf,
    carry_config: PathBuf,
    operational_config: PathBuf,
    engine_config: PathBuf,
    universe: PathBuf,
}

#[derive(Debug)]
enum Command {
    Check(CommonArgs),
    Replay {
        common: CommonArgs,
        input: String,
        output: String,
    },
    Live {
        common: CommonArgs,
        spool_dir: PathBuf,
        state_dir: PathBuf,
        heartbeat: PathBuf,
    },
}

#[derive(Serialize)]
struct ConfigCheck<'a> {
    schema_version: u32,
    kind: &'static str,
    status: &'static str,
    credential_free: bool,
    environment: &'a str,
    public_market_realm: &'a str,
    public_bybit_host: &'a str,
    long_destination: u16,
    carry_destination: u16,
    live_long_source_pattern: String,
    live_carry_source_pattern: String,
    config: &'a signal_worker::ConfigIdentity,
    universe: &'a signal_worker::model::UniverseIdentity,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("signal-worker: {error}");
        std::process::exit(2);
    }
}

async fn run() -> Result<(), WorkerError> {
    let command = parse_args(std::env::args().skip(1))?;
    match command {
        Command::Check(common) => {
            let (config, universe) = load_common(&common)?;
            let check = ConfigCheck {
                schema_version: signal_worker::SCHEMA_VERSION,
                kind: "liquidity_migration_signal_worker_config_check",
                status: "ok",
                credential_free: true,
                environment: &config.live.environment,
                public_market_realm: &config.live.public_market_realm,
                public_bybit_host: &config.sources.bybit_mainnet_host,
                long_destination: config.long_destination,
                carry_destination: config.carry_destination,
                live_long_source_pattern: format!(
                    "{}.g{{source_generation}}.long",
                    config.routing.source
                ),
                live_carry_source_pattern: format!(
                    "{}.g{{source_generation}}.carry",
                    config.routing.source
                ),
                config: &config.identity,
                universe: &universe,
            };
            println!(
                "{}",
                serde_json::to_string(&check)
                    .map_err(|error| WorkerError::json("encode config check", error))?
            );
            Ok(())
        }
        Command::Replay {
            common,
            input,
            output,
        } => {
            let (config, universe) = load_common(&common)?;
            replay(config, universe, &input, &output)
        }
        Command::Live {
            common,
            spool_dir,
            state_dir,
            heartbeat,
        } => {
            let (config, universe) = load_common(&common)?;
            reject_overlapping_paths(&spool_dir, &state_dir, &heartbeat)?;
            let runner = LiveRunner::open_responsive(
                config,
                universe,
                LiveRunOptions {
                    state_dir,
                    spool_dir,
                    heartbeat,
                },
            )
            .await?;
            match runner {
                Some(runner) => runner.run().await,
                None => Ok(()),
            }
        }
    }
}

fn load_common(
    args: &CommonArgs,
) -> Result<(SignalWorkerConfig, signal_worker::model::UniverseIdentity), WorkerError> {
    let config = SignalWorkerConfig::load(
        &args.signal_config,
        &args.long_rule,
        &args.carry_config,
        &args.operational_config,
        &args.engine_config,
    )?;
    let universe = load_candidate_universe(&args.universe, &config.live.environment)?;
    Ok((config, universe))
}

fn replay(
    config: SignalWorkerConfig,
    universe: signal_worker::model::UniverseIdentity,
    input: &str,
    output: &str,
) -> Result<(), WorkerError> {
    let reader: Box<dyn BufRead> = if input == "-" {
        Box::new(BufReader::new(io::stdin().lock()))
    } else {
        Box::new(BufReader::new(
            File::open(input).map_err(|error| WorkerError::io("open replay input", error))?,
        ))
    };
    let mut writer: Box<dyn Write> = if output == "-" {
        Box::new(BufWriter::new(io::stdout().lock()))
    } else {
        Box::new(BufWriter::new(File::create(output).map_err(|error| {
            WorkerError::io("create replay output", error)
        })?))
    };
    let mut worker = SignalWorker::new(config, universe)?;
    for (index, line) in reader.lines().enumerate() {
        let line = line.map_err(|error| WorkerError::io("read replay input", error))?;
        if line.trim().is_empty() {
            continue;
        }
        let event: WireEvent = serde_json::from_str(&line).map_err(|error| {
            WorkerError::input(format!(
                "replay line {} is invalid JSON: {error}",
                index + 1
            ))
        })?;
        for observation in worker.apply(event)? {
            serde_json::to_writer(&mut writer, &observation)
                .map_err(|error| WorkerError::json("encode replay observation", error))?;
            writer
                .write_all(b"\n")
                .map_err(|error| WorkerError::io("write replay output", error))?;
        }
    }
    writer
        .flush()
        .map_err(|error| WorkerError::io("flush replay output", error))
}

fn parse_args(args: impl IntoIterator<Item = String>) -> Result<Command, WorkerError> {
    let mut args = args.into_iter();
    let mode = args.next().ok_or_else(|| WorkerError::config(USAGE))?;
    if mode == "--help" || mode == "-h" {
        println!("{USAGE}");
        std::process::exit(0);
    }
    let mut values = BTreeMap::new();
    while let Some(flag) = args.next() {
        if !flag.starts_with("--") {
            return Err(WorkerError::config(format!(
                "unexpected argument {flag}\n{USAGE}"
            )));
        }
        let value = args
            .next()
            .ok_or_else(|| WorkerError::config(format!("{flag} requires a value")))?;
        if values.insert(flag.clone(), value).is_some() {
            return Err(WorkerError::config(format!("duplicate option {flag}")));
        }
    }
    let common = CommonArgs {
        signal_config: take_path(&mut values, "--signal-config")?,
        long_rule: take_path(&mut values, "--long-rule")?,
        carry_config: take_path(&mut values, "--carry-config")?,
        operational_config: take_path(&mut values, "--operational-config")?,
        engine_config: take_path(&mut values, "--engine-config")?,
        universe: take_path(&mut values, "--universe")?,
    };
    let command = match mode.as_str() {
        "check-config" => Command::Check(common),
        "replay" => Command::Replay {
            common,
            input: take(&mut values, "--input")?,
            output: take(&mut values, "--output")?,
        },
        "live" => Command::Live {
            common,
            spool_dir: take_path(&mut values, "--spool-dir")?,
            state_dir: take_path(&mut values, "--state-dir")?,
            heartbeat: take_path(&mut values, "--heartbeat")?,
        },
        _ => return Err(WorkerError::config(format!("unknown mode {mode}\n{USAGE}"))),
    };
    if let Some(flag) = values.keys().next() {
        return Err(WorkerError::config(format!("unknown option {flag}")));
    }
    Ok(command)
}

fn take(values: &mut BTreeMap<String, String>, flag: &str) -> Result<String, WorkerError> {
    values
        .remove(flag)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| WorkerError::config(format!("missing {flag}\n{USAGE}")))
}

fn take_path(values: &mut BTreeMap<String, String>, flag: &str) -> Result<PathBuf, WorkerError> {
    take(values, flag).map(PathBuf::from)
}

fn reject_overlapping_paths(
    spool: &Path,
    state: &Path,
    heartbeat: &Path,
) -> Result<(), WorkerError> {
    if spool == state
        || heartbeat.starts_with(spool)
        || spool.starts_with(state)
        || state.starts_with(spool)
    {
        return Err(WorkerError::config(
            "spool, state, and heartbeat paths must not overlap",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{parse_args, Command};

    #[test]
    fn live_requires_every_durable_path() {
        let args = [
            "live",
            "--signal-config",
            "s",
            "--long-rule",
            "l",
            "--carry-config",
            "c",
            "--operational-config",
            "o",
            "--engine-config",
            "e",
            "--universe",
            "u",
            "--spool-dir",
            "spool",
            "--state-dir",
            "state",
            "--heartbeat",
            "health",
        ];
        assert!(matches!(
            parse_args(args.into_iter().map(str::to_owned)).unwrap(),
            Command::Live { .. }
        ));
    }
}
