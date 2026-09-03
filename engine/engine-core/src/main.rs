//! The engine binary. `USAGE` below is the list of subcommands.
//!
//! One thread. The runtime is tokio's current-thread build on purpose — the
//! whole point of the design is that a market message is turned into an order
//! without ever leaving the thread it arrived on.

use std::error::Error;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use engine_core::bench::{self, BenchOptions};
use engine_core::execution;
use engine_core::replay;
use engine_core::runner;

const USAGE: &str = "\
engine — the execution loop

  engine run --config engine.toml
      Run the engine. It sends orders; REAL_MONEY gates the funded venue.

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
            runtime()?.block_on(runner::run(&config))
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
            options.fills = args.iter().any(|a| a == "--fills");
            if let Some(ms) = value(args, "--venue-delay-ms") {
                options.venue_delay = std::time::Duration::from_millis(
                    ms.parse()
                        .map_err(|_| "--venue-delay-ms wants whole milliseconds")?,
                );
            }
            let result = runtime()?.block_on(bench::run(&options))?;
            println!(
                "\nbench: {} quotes in, {} orders out, against a pretend venue on this box",
                result.events, result.orders
            );
            println!("{}", result.table());
            println!(
                "  the fsync is inside \"write it down\"; \"API round trip\" is a local socket,\n  \
                 so the real venue's network and matching-engine time is not in these numbers."
            );
            Ok(())
        }
        "wal-cost" => {
            let path = PathBuf::from(value(args, "--wal").ok_or("wal-cost needs --wal PATH")?);
            let appends: usize = value(args, "--appends").unwrap_or("20000".into()).parse()?;
            let barriers: usize = value(args, "--barriers").unwrap_or("200".into()).parse()?;
            let costs = engine_wal::measure(&path, appends, barriers)?;
            println!("wal-cost path={}", path.display());
            println!("{costs}");
            println!("  the barrier is the fsync the order path waits for before a send.");
            println!("  compare against a memory-backed path to bound what faster storage buys.");
            Ok(())
        }
        "venue-key" => {
            let path = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            let loaded = engine_core::config::load(&path)?;
            let chosen = engine_core::assembly::venue_name(&loaded.config.engine.venue)?;
            // No symbols: nothing here sends anything, and the table is only
            // needed to build a request.
            let venue = engine_core::assembly::venue(chosen, Vec::new())?;
            println!("venue   {chosen}");
            println!("realm   {}", chosen.realm());
            match venue.signing_identity() {
                Some(identity) => {
                    println!("signs as {identity}");
                    println!(
                        "\n  register this at the venue against the account in the credential \n  \
                         file, or every order this host sends will be refused."
                    );
                }
                None => println!(
                    "\n  this venue needs nothing registered: it authenticates with the key \n  \
                     itself, or not at all."
                ),
            }
            Ok(())
        }
        "venues" => {
            println!("name\tvenue\trealm\treal_money\treadiness");
            for chosen in engine_venue::VenueName::ALL {
                println!(
                    "{}\t{}\t{}\t{}\t{}",
                    chosen.as_str(),
                    chosen.venue(),
                    chosen.realm(),
                    chosen.is_real_money(),
                    chosen.readiness().as_str()
                );
            }
            Ok(())
        }
        "strategies" => {
            for name in engine_strategies::known_strategies() {
                println!("{name}");
            }
            Ok(())
        }
        "render-native-config" => render_native_config(args),
        "attest-flat" => {
            let config = PathBuf::from(
                value(args, "--config")
                    .or_else(|| std::env::var("ENGINE_CONFIG_FILE").ok())
                    .unwrap_or("engine.toml".into()),
            );
            runtime()?.block_on(engine_core::flatness::run(&config))
        }
        "verify-account-identity" => {
            let config = PathBuf::from(
                value(args, "--config")
                    .or_else(|| std::env::var("ENGINE_CONFIG_FILE").ok())
                    .unwrap_or("engine.toml".into()),
            );
            runtime()?.block_on(engine_core::flatness::verify_account_identity(&config))
        }
        "canary-order" => {
            let config = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            let symbol = value(args, "--symbol").ok_or("canary-order needs --symbol SYMBOL")?;
            let expected_user_id = value(args, "--expected-user-id")
                .ok_or("canary-order needs --expected-user-id USER_ID")?;
            let execute = args.iter().any(|arg| arg == "--execute");
            runtime()?.block_on(engine_core::canary::run(
                &config,
                &symbol,
                &expected_user_id,
                execute,
            ))
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
            // The whole family, oldest segment first. A log that was never
            // rotated is a family of one, so a plain file path still means
            // what it always did.
            let (replayed, torn) = engine_wal::replay_chain(Path::new(&path))?;
            let segments = engine_wal::segments(Path::new(&path))?.len();
            let records: Vec<_> = replayed.into_iter().map(|(_, r)| r).collect();
            // A log that ran in shadow wrote orders down without sending them.
            // The table below cannot tell those apart from venue fills, so it
            // says so rather than presenting one era's numbers as the other's.
            let shadow_records = records
                .iter()
                .filter(|record| {
                    matches!(record, engine_wal::WalRecord::Note { source, .. } if source == "shadow")
                })
                .count();
            print!("{}", execution::report::of_log(&records));
            // Naming a numbered segment reads that segment alone, and the
            // table looks exactly the same either way.
            println!(
                "\n  {} record(s), from {} log segment(s) under {path}.",
                records.len(),
                segments
            );
            if shadow_records > 0 {
                println!(
                    "\n  {shadow_records} shadow record(s) in this log: orders worked out and \
                     never sent. Anything they priced is not a venue fill, and these numbers do \
                     not separate the two eras."
                );
            }
            if torn {
                println!(
                    "\n  the log ends part-way through a record; anything after that point is \
                     not in these numbers."
                );
            }
            Ok(())
        }
        "latency" => {
            let path = value(args, "--wal").ok_or("latency needs --wal PATH")?;
            let (replayed, torn) = engine_wal::replay_chain(Path::new(&path))?;
            let segments = engine_wal::segments(Path::new(&path))?.len();
            let records: Vec<_> = replayed.into_iter().map(|(_, r)| r).collect();
            print!("{}", engine_core::timing::of_log(&records));
            println!(
                "\n  {} record(s), from {} log segment(s) under {path}.",
                records.len(),
                segments
            );
            if torn {
                println!(
                    "\n  the log ends part-way through a record; anything after that point is \
                     not in these numbers."
                );
            }
            Ok(())
        }
        "reconcile-clear" => {
            let config = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            let note = value(args, "--note").unwrap_or("operator reconcile-clear".into());
            let execute = args.iter().any(|a| a == "--execute");
            runtime()?.block_on(engine_core::clear::run(&config, &note, execute))
        }
        "import-strategy-state" => {
            let config = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            let strategy =
                value(args, "--strategy").ok_or("import-strategy-state needs --strategy SLEEVE")?;
            let source_format = value(args, "--source-format")
                .ok_or("import-strategy-state needs --source-format FORMAT")?;
            let source_flags = args.iter().filter(|arg| arg.as_str() == "--source").count();
            let source_values = values(args, "--source");
            if source_values.len() != source_flags {
                return Err("import-strategy-state has --source without NAME=PATH".into());
            }
            let sources: Vec<(String, PathBuf)> = source_values
                .into_iter()
                .map(|source| {
                    let (name, path) = source
                        .split_once('=')
                        .ok_or("import-strategy-state --source must be NAME=PATH")?;
                    if path.is_empty() {
                        return Err("import-strategy-state --source path is empty");
                    }
                    Ok((name.to_string(), PathBuf::from(path)))
                })
                .collect::<Result<_, &str>>()?;
            runtime()?.block_on(engine_core::takeover::run(
                &config,
                &strategy,
                &source_format,
                &sources,
            ))?;
            Ok(())
        }
        "initialize-native-strategy-state" => {
            let config = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            runtime()?.block_on(engine_core::takeover::initialize_native_strategy_state(
                &config,
            ))
        }
        "verify-native-strategy-state" => {
            let config = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            engine_core::takeover::verify_native_strategy_state(&config)
        }
        "set-strategy-entry-permission" => {
            let config = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            let strategy = value(args, "--strategy")
                .ok_or("set-strategy-entry-permission needs --strategy SLEEVE")?;
            let enabled = match value(args, "--entries-enabled").as_deref() {
                Some("true") => true,
                Some("false") => false,
                _ => return Err("--entries-enabled must be true or false".into()),
            };
            let request_id = value(args, "--request-id")
                .ok_or("set-strategy-entry-permission needs --request-id ID")?;
            let wait_ms = value(args, "--wait-ms")
                .unwrap_or("30000".into())
                .parse::<u64>()?;
            runtime()?.block_on(submit_runtime_control(
                &config,
                &strategy,
                &request_id,
                engine_types::RuntimeControlCommand::SetEntriesEnabled {
                    entries_enabled: enabled,
                },
                wait_ms,
            ))
        }
        "flatten-strategy" => {
            let config = PathBuf::from(value(args, "--config").unwrap_or("engine.toml".into()));
            let strategy =
                value(args, "--strategy").ok_or("flatten-strategy needs --strategy SLEEVE")?;
            let request_id =
                value(args, "--request-id").ok_or("flatten-strategy needs --request-id ID")?;
            let wait_ms = value(args, "--wait-ms")
                .unwrap_or("30000".into())
                .parse::<u64>()?;
            runtime()?.block_on(submit_runtime_control(
                &config,
                &strategy,
                &request_id,
                engine_types::RuntimeControlCommand::FlattenDirectional,
                wait_ms,
            ))
        }
        "-h" | "--help" | "help" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown command {other}\n\n{USAGE}").into()),
    }
}

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
    let mut request = engine_types::RuntimeControlRequest {
        schema_version: engine_types::STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
        strategy: engine_types::StrategyId(
            u16::try_from(*at).map_err(|_| "more than 65535 configured strategies")?,
        ),
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
