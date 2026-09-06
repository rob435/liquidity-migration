//! The engine binary. `USAGE` below is the list of subcommands.
//!
//! One thread. The runtime is tokio's current-thread build on purpose — the
//! whole point of the design is that a market message is turned into an order
//! without ever leaving the thread it arrived on.

use std::error::Error;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use engine_core::backtest::{self, BacktestOptions};
use engine_core::bench::{self, BenchOptions};
use engine_core::execution;
use engine_core::replay;
use engine_core::runner;

const USAGE: &str = "\
engine — the execution loop

  engine run --config engine.toml
      Run the engine. It sends orders; REAL_MONEY gates the funded venue.

  engine backtest --config PATH --tape PATH --instruments PATH --wal PATH
                  [--signals DIR] [--trades PATH] [--equity PATH] [--report PATH]
                  [--capital USDT] [--taker-fee RATE] [--maker-fee RATE]
                  [--rtt-ms MS] [--private-latency-ms MS] [--mmr FRACTION] [--durable-log]
      Run the loop against a recorded market_tape, in the tape's own time,
      on a simulated venue. The log must be new. Prints the report; --report
      writes it as JSON.

  engine sim [--seed N] [--seeds K] [--seconds S] [--symbols M] [--crashes C]
             [--faults none|light|heavy] [--twice] [--out DIR] [--keep] [--report PATH]
      Run the loop on a seeded synthetic market against the simulated venue,
      with venue replies lost, private updates dropped and duplicated, feed
      hiccups, and C process deaths with a boot from the log after each. At
      the end the venue's books, the log and the engine are checked against
      each other. One seed is one run, byte for byte; --twice proves it.
      Exit status is non-zero when any check fails; the seed reproduces it.

  engine bench [--events N] [--rate PER_SEC] [--every N] [--symbols A,B]
               [--wal PATH] [--fills] [--venue-delay-ms MS]
      Measure the real loop through a local submit response on this box.
      --venue-delay-ms holds the pretend venue's reply for that long, which is
      the one thing a localhost socket cannot model: whether work on this
      side is hidden by the flight to the venue or added to it depends on
      which of the two is longer. Set it to the venue's measured round trip.
      --fills has that venue fill what it accepts, so the log it writes can be
      read by `engine fills`. Off by default: the published latency table was
      measured without it.

  engine replay --wal PATH
      Print the log in words, and what was still in flight at each point.

  engine fills --wal PATH
      What the trading cost: maker share, fee, how far each fill landed from
      the price on the screen when its order left, and where the market went
      afterwards. Per sleeve and symbol. Then what the positions made: every
      round trip that closed, with its P&L after fees. The crowd fee (funding)
      is in neither -- the venue never tells the engine about it.

  engine latency --wal PATH
      How long each step of the order path took, per operation, at p50, p90,
      p99 and p99.9. Reads the exact stamps every order, cancel and amend
      wrote, so the venue's round trip, the time this engine held the command
      back to stay inside the request limit, and its own work are separate
      numbers rather than one span.

  engine venue-key --config engine.toml
      What this host signs as at the config's venue, so it can be registered
      there: an API wallet's address on Hyperliquid, a public key on Lighter.
      Reads the host's credentials and touches no network. Never prints a
      secret.

  engine wal-cost --wal PATH [--appends N] [--barriers N]
      What one buffered append and one durability barrier cost on the
      filesystem holding PATH. The barrier is the fsync the order path waits
      for before a send, so this is the storage's share of the order path.
      Point --wal at the real state directory and again at a memory-backed
      one to bound what faster storage would buy.

  engine venues
      List every compiled venue/realm and its live-evidence gate.

  engine strategies
      List every strategy plug that a [[strategy]] config block can load.

  engine render-native-config --realm demo|mainnet
             --signal-config PATH --long-rule PATH --carry-rule PATH
             --exodus-rule PATH --operational-config PATH [--maker-rule PATH]
             --long-entries-enabled true|false
             --carry-entries-enabled true|false
             --exodus-entries-enabled true|false
             [--template PATH] --output PATH [--check]
      Derive the native LONG, CARRY, and Exodus config blobs and stable
      fingerprints from their machine authorities. With --template, replace
      only its marked native-directional region. --check changes nothing and
      fails when PATH does not contain the exact rendered bytes.

  engine attest-flat --config engine.toml
      Read every account position and open order surface known by the venue
      adapter. Succeeds only when the credential-wide inventory is fresh and
      empty. Sends no orders and changes no venue state.

  engine verify-account-identity --config engine.toml
      Authenticate the narrow inventory reader and bind it to the config's
      venue, realm, and EXPECTED_ENGINE_ACCOUNT_USER_ID. Reads no WAL or
      account inventory and changes no venue state.

  engine canary-order --config engine.toml --symbol XRPUSDT
                      --expected-user-id 579580669 --execute
      On Bybit Demo only, take the account lease, rest one minimum-value
      post-only order away from the touch with an attached stop, cancel it,
      and prove the derivative account clean twice. Any fill is closed in full
      and makes the command fail after cleanup. Without --execute, no
      credential or network is touched.

  engine reconcile-clear --config engine.toml [--note TEXT] [--execute]
      The deliberate look the may-open latch waits for. Stop the engine
      first (this takes the log's own lock). Shows the standing findings;
      with --execute, restates the exposure ledger to the venue's positions
      and resets the latch, keeping the findings in the log as the receipt.
      The next boot still runs its own comparison.

  engine import-strategy-state --config engine.toml --strategy SLEEVE
                               --source-format FORMAT --source NAME=PATH
                               [--source NAME=PATH ...]
      Stop the engine, lock its WAL and venue account, verify the config's
      strategy ids against WAL Names, ask that strategy's strict legacy codec
      to translate the source, then append its canonical state.
      An exact retry is a no-op; any different state or source proof is refused.

  engine initialize-native-strategy-state --config engine.toml
      On a truly empty WAL only, lock the WAL and configured venue account,
      bind the authenticated user to EXPECTED_ENGINE_ACCOUNT_USER_ID, and
      durably seed every native reducer's strict canonical empty checkpoint.

  engine verify-native-strategy-state --config engine.toml
      Lock and read the stopped engine WAL, then verify exact strategy names,
      current checkpoint identities and payloads, and completed provenance.

  engine retire-legacy-signal-sources --config engine.toml --plan PATH [--execute]
      Read an operator retirement plan for permanently stopped legacy sources.
      With --execute, journal each unprocessed suffix under the WAL lock;
      accepted cursors and recovered input payloads are not rewritten.

  engine set-strategy-entry-permission --config engine.toml --strategy SLEEVE
             --entries-enabled true|false --request-id ID [--wait-ms MS]
      Submit one idempotent live command to the engine and wait until its WAL
      barrier and in-memory apply are complete. False blocks entries and
      growing resizes only; signal delivery and exits continue.

  engine flatten-strategy --config engine.toml --strategy SLEEVE
             --request-id ID [--wait-ms MS]
      Submit a durable replayable flatten wake. The same sleeve must first
      have a durable entries-disabled runtime override.

";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.as_slice() == ["--strategy-worker"] {
        return match engine_core::strategy_process::worker::run_stdio() {
            Ok(()) => ExitCode::SUCCESS,
            Err(_) => ExitCode::FAILURE,
        };
    }
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_target(false)
        // journald stores colour escapes as literal bytes and rsyslog widens
        // each one to the four characters `#033`, so they are written only for
        // a human at a terminal.
        .with_ansi(std::io::IsTerminal::is_terminal(&std::io::stdout()))
        .init();

    match dispatch(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("engine: {e}");
            ExitCode::FAILURE
        }
    }
}

mod cli;
use cli::dispatch;

fn render_native_config(args: &[String]) -> Result<(), Box<dyn Error>> {
    let realm = value(args, "--realm").ok_or("render-native-config needs --realm")?;
    let source_path = |flag: &str| -> Result<PathBuf, Box<dyn Error>> {
        Ok(PathBuf::from(value(args, flag).ok_or_else(|| {
            format!("render-native-config needs {flag} PATH")
        })?))
    };
    let signal_path = source_path("--signal-config")?;
    let long_path = source_path("--long-rule")?;
    let carry_path = source_path("--carry-rule")?;
    let exodus_path = source_path("--exodus-rule")?;
    let operational_path = source_path("--operational-config")?;
    let output_path = source_path("--output")?;
    let parse_switch = |flag: &str| -> Result<bool, Box<dyn Error>> {
        match value(args, flag).as_deref() {
            Some("true") => Ok(true),
            Some("false") => Ok(false),
            _ => Err(format!("{flag} must be true or false").into()),
        }
    };
    let signal = std::fs::read(&signal_path)?;
    let long = std::fs::read(&long_path)?;
    let carry = std::fs::read(&carry_path)?;
    let exodus = std::fs::read(&exodus_path)?;
    let operational = std::fs::read(&operational_path)?;
    let rendered = engine_strategies::native_config::render_native_config(
        engine_strategies::native_config::NativeConfigSources {
            realm: &realm,
            signal_config: &signal,
            long_rule: &long,
            carry_rule: &carry,
            exodus_rule: &exodus,
            operational_config: &operational,
            long_entries_enabled: parse_switch("--long-entries-enabled")?,
            carry_entries_enabled: parse_switch("--carry-entries-enabled")?,
            exodus_entries_enabled: parse_switch("--exodus-entries-enabled")?,
        },
    )?;
    let template_path = value(args, "--template");
    if realm == "mainnet" && value(args, "--maker-rule").is_none() {
        return Err("mainnet render-native-config needs --maker-rule PATH".into());
    }
    if value(args, "--maker-rule").is_some() && template_path.is_none() {
        return Err("--maker-rule requires --template so the maker slot is preserved".into());
    }
    let mut output = if let Some(template_path) = template_path {
        let template = std::fs::read_to_string(template_path)?;
        engine_strategies::native_config::insert_native_blocks(&template, &rendered.toml_blocks)?
    } else {
        rendered.toml_blocks.clone()
    };
    if let Some(maker_path) = value(args, "--maker-rule") {
        let maker = std::fs::read(maker_path)?;
        let generated = engine_strategies::native_config::render_maker_rule(&maker)?;
        output = engine_strategies::native_config::insert_maker_rule(&output, &generated)?;
    }
    if args.iter().any(|arg| arg == "--check") {
        let existing = std::fs::read(&output_path)?;
        if existing != output.as_bytes() {
            return Err(format!(
                "{} is not the exact rendered native config",
                output_path.display()
            )
            .into());
        }
    } else {
        std::fs::write(&output_path, output.as_bytes())?;
    }
    println!("output                         {}", output_path.display());
    println!(
        "long_decision_fingerprint      {}",
        rendered.long_decision_fingerprint
    );
    println!(
        "carry_decision_fingerprint     {}",
        rendered.carry_decision_fingerprint
    );
    println!(
        "exodus_decision_fingerprint    {}",
        rendered.exodus_decision_fingerprint
    );
    Ok(())
}

async fn submit_runtime_control(
    config_path: &Path,
    strategy_name: &str,
    request_id: &str,
    command: engine_types::RuntimeControlCommand,
    wait_ms: u64,
) -> Result<(), Box<dyn Error>> {
    let loaded = engine_core::config::load(config_path)?;
    let matches: Vec<_> = loaded
        .config
        .strategies
        .iter()
        .enumerate()
        .filter(|(_, row)| row.sleeve_name() == strategy_name)
        .collect();
    let [(at, _)] = matches.as_slice() else {
        return Err(
            format!("strategy {strategy_name:?} must appear exactly once in this config").into(),
        );
    };
    let spool = loaded
        .config
        .engine
        .control_spool_path
        .as_deref()
        .ok_or("engine.control_spool_path is required for live runtime controls")?;
    let (records, torn) = engine_wal::replay_current(&loaded.config.engine.wal_path)?;
    if torn {
        return Err("runtime control waits for a complete WAL identity frame".into());
    }
    let replayed = records
        .into_iter()
        .map(|(_, record)| record)
        .collect::<Vec<_>>();
    let keys = loaded
        .config
        .strategies
        .iter()
        .map(|strategy| strategy.sleeve_name().to_string())
        .collect::<Vec<_>>();
    let plan =
        engine_core::identities::plan_identities(&replayed, &keys, None, &Default::default(), &[])?;
    let mut request = engine_types::RuntimeControlRequest {
        schema_version: engine_types::STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
        strategy: plan.configured_ids[*at],
        strategy_name: strategy_name.to_string(),
        request_id: request_id.to_string(),
        command,
        content_sha256: String::new(),
    };
    request.content_sha256 = engine_core::controls::content_sha256(&request);
    engine_core::controls::submit_and_wait(
        spool,
        &request,
        std::time::Duration::from_millis(wait_ms),
    )
    .await?;
    println!("strategy   {} ({})", strategy_name, request.strategy.0);
    println!("request    {}", request_id);
    println!("command    {:?}", request.command);
    println!("result     durable and applied");
    Ok(())
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

fn values(args: &[String], flag: &str) -> Vec<String> {
    args.iter()
        .enumerate()
        .filter(|(_, value)| value.as_str() == flag)
        .filter_map(|(at, _)| args.get(at + 1).cloned())
        .collect()
}
