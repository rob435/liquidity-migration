use super::*;

pub(super) fn dispatch(args: &[String]) -> Result<(), Box<dyn Error>> {
    let Some(command) = args.first().map(String::as_str) else {
        print!("{USAGE}");
        return Ok(());
    };
    match command {
        "run" => run(args),
        "backtest" => backtest(args),
        "sim" => sim(args),
        "bench" => bench(args),
        "record-equity" => runtime()?.block_on(engine_tools::equity_recorder::run(&args[1..])),
        "execution-study" => engine_tools::execution_study::run(&args[1..]),
        "wal-cost" => wal_cost(args),
        "wal-retention" => wal_retention(args),
        "wal-convert-v5" => wal_convert_v5(args),
        "venue-key" => venue_key(args),
        "venues" => venues(args),
        "strategies" => strategies(args),
        "render-native-config" => render_native_config(args),
        "attest-flat" => attest_flat(args),
        "verify-account-identity" => verify_account_identity(args),
        "canary-order" => canary_order(args),
        "replay" => replay(args),
        "fills" => fills(args),
        "latency" => latency(args),
        "cohort" => cohort(args),
        "reconcile-clear" => reconcile_clear(args),
        "initialize-native-strategy-state" => initialize_native_strategy_state(args),
        "verify-native-strategy-state" => verify_native_strategy_state(args),
        "rebind-native-strategy-state" => rebind_native_strategy_state(args),
        "retire-legacy-signal-sources" => retire_legacy_signal_sources(args),
        "set-strategy-entry-permission" => set_strategy_entry_permission(args),
        "flatten-strategy" => flatten_strategy(args),
        "-h" | "--help" | "help" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown command {other}\n\n{USAGE}").into()),
    }
}

/// One subcommand's declared options. Every argument must be claimed by a
/// `value` or `flag` call; `finish` refuses what is left, so a misspelled or
/// misplaced flag stops the command instead of silently running a different
/// one.
pub(super) struct Args {
    args: Vec<String>,
    used: Vec<bool>,
}

impl Args {
    pub(super) fn new(args: &[String]) -> Self {
        let mut used = vec![false; args.len()];
        // args[0] is the subcommand name.
        if let Some(command) = used.first_mut() {
            *command = true;
        }
        Self {
            args: args.to_vec(),
            used,
        }
    }

    pub(super) fn value(&mut self, flag: &str) -> Option<String> {
        let at = self.args.iter().position(|arg| arg == flag)?;
        self.used[at] = true;
        let value = self.args.get(at + 1)?.clone();
        self.used[at + 1] = true;
        Some(value)
    }

    pub(super) fn flag(&mut self, flag: &str) -> bool {
        let Some(at) = self.args.iter().position(|arg| arg == flag) else {
            return false;
        };
        self.used[at] = true;
        true
    }

    pub(super) fn finish(self) -> Result<(), Box<dyn Error>> {
        let left: Vec<&str> = self
            .args
            .iter()
            .zip(&self.used)
            .filter(|(_, used)| !**used)
            .map(|(arg, _)| arg.as_str())
            .collect();
        if left.is_empty() {
            return Ok(());
        }
        let command = self.args.first().map_or("this command", String::as_str);
        Err(format!("{command} does not take {}", left.join(" ")).into())
    }
}

fn run(args: &[String]) -> Result<(), Box<dyn Error>> {
    use std::os::unix::process::CommandExt;
    let executable = std::env::current_exe()?.with_file_name("engine");
    Err(std::process::Command::new(executable)
        .args(args)
        .exec()
        .into())
}

fn backtest(args: &[String]) -> Result<(), Box<dyn Error>> {
    let options = parse_backtest_options(args)?;
    let report = runtime()?.block_on(backtest::run(options))?;
    print!("{}", report.table());
    Ok(())
}

fn sim(args: &[String]) -> Result<(), Box<dyn Error>> {
    let (options, report_path) = parse_sim_options(args)?;
    let report = runtime()?.block_on(engine_tools::sim::run_sweep(options))?;
    print!("{}", report.table());
    if let Some(path) = report_path {
        std::fs::write(&path, serde_json::to_string_pretty(&report)?)?;
    }
    if !report.passed() {
        return Err("simulation checks failed; each seed above reproduces its failure".into());
    }
    Ok(())
}

fn bench(args: &[String]) -> Result<(), Box<dyn Error>> {
    let (options, json) = parse_bench_options(args)?;
    let result = runtime()?.block_on(bench::run(&options))?;
    if json {
        println!("{}", result.as_json());
        return Ok(());
    }
    println!(
        "\nbench: {} quotes in, {} completed submit attempts, against a local synthetic venue",
        result.events, result.orders
    );
    println!(
        "workload: {}",
        if options.contention {
            "contention"
        } else {
            "default"
        }
    );
    println!("{}", result.table());
    println!(
        "  \"market to decision\" includes market handling and the embedded callback.\n  \
         \"decision to dispatch ready\" includes checkpoint handling, admission and the order barrier.\n  \
         \"dispatch barrier observed\" includes the engine resuming after the order barrier.\n  \
         \"API round trip\" uses localhost; real venue network and matching time are not measured."
    );
    Ok(())
}

fn wal_cost(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let path = args.value("--wal");
    let appends = args.value("--appends");
    let barriers = args.value("--barriers");
    let rotations = args.value("--rotations");
    args.finish()?;
    let path = PathBuf::from(path.ok_or("wal-cost needs --wal PATH")?);
    let appends: usize = appends.unwrap_or_else(|| "20000".into()).parse()?;
    let barriers: usize = barriers.unwrap_or_else(|| "200".into()).parse()?;
    let rotations: usize = rotations.unwrap_or_else(|| "20".into()).parse()?;
    let costs = engine_wal::measure(&path, appends, barriers)?;
    println!("wal-cost path={}", path.display());
    println!("{costs}");
    if rotations > 0 {
        for row in engine_wal::measure_rotation(&path, rotations)? {
            println!("{row}");
        }
        println!("  rotation runs on the engine loop, at one base-record size per block.");
    }
    println!("  the barrier measures synchronous fsync; engine bench separates callback, queued-dispatch and attempted-send barriers.");
    println!("  compare against a memory-backed path to bound what faster storage buys.");
    Ok(())
}

fn wal_retention(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let path = args.value("--wal");
    let json = args.flag("--json");
    args.finish()?;
    let path = PathBuf::from(path.ok_or("wal-retention needs --wal PATH")?);
    let report = engine_tools::wal_retention::read(&path)?;
    if json {
        println!("{}", serde_json::to_string_pretty(&report)?);
        return Ok(());
    }
    println!("wal-retention family={}", path.display());
    print!("{}", report.table());
    println!(
        "\n  a segment below retention_floor_segment is an archive; the engine may still \
         open every segment at or above it."
    );
    Ok(())
}

fn wal_convert_v5(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let input = args.value("--wal");
    let output = args.value("--output-dir");
    args.finish()?;
    let input = PathBuf::from(input.ok_or("wal-convert-v5 needs --wal PATH")?);
    let output = PathBuf::from(output.ok_or("wal-convert-v5 needs --output-dir NEW_DIRECTORY")?);
    let result = engine_tools::wal_conversion::convert(&input, &output)?;
    println!(
        "family={} segments={} records={} upgraded_bases={} relocated_bases={}",
        result.family.display(),
        result.segments,
        result.records,
        result.upgraded_bases,
        result.relocated_bases,
    );
    Ok(())
}

fn venue_key(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let path = args.value("--config");
    args.finish()?;
    let path = PathBuf::from(path.unwrap_or_else(|| "engine.toml".into()));
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

fn venues(_args: &[String]) -> Result<(), Box<dyn Error>> {
    println!("name\tvenue\trealm\treal_money\treadiness");
    for chosen in engine_venue::VenueName::ALL
        .into_iter()
        .filter(|name| name.compiled())
    {
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

fn strategies(_args: &[String]) -> Result<(), Box<dyn Error>> {
    for name in engine_strategies::known_strategies() {
        println!("{name}");
    }
    Ok(())
}

/// What [`engine_wal::replay_chain`]'s flag means: some trusted segment of
/// the family ended part-way through a record, not necessarily the newest.
/// The three log readers below say it in one voice.
const TORN_TAIL: &str = "\n  a log segment ends part-way through a record; the records after that \
     point in that segment are not in these numbers.";

/// `--config`, the runtime unit's `ENGINE_CONFIG_FILE`, then the working
/// directory's `engine.toml`.
fn config_with_env(args: &[String]) -> Result<PathBuf, Box<dyn Error>> {
    let mut args = Args::new(args);
    let config = args.value("--config");
    args.finish()?;
    Ok(PathBuf::from(
        config
            .or_else(|| std::env::var("ENGINE_CONFIG_FILE").ok())
            .unwrap_or_else(|| "engine.toml".into()),
    ))
}

fn config_or_default(args: &[String]) -> Result<PathBuf, Box<dyn Error>> {
    let mut args = Args::new(args);
    let config = args.value("--config");
    args.finish()?;
    Ok(PathBuf::from(
        config.unwrap_or_else(|| "engine.toml".into()),
    ))
}

fn attest_flat(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = config_with_env(args)?;
    runtime()?.block_on(engine_tools::flatness::run(&config))
}

fn verify_account_identity(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = config_with_env(args)?;
    runtime()?.block_on(engine_tools::flatness::verify_account_identity(&config))
}

#[cfg(not(any(feature = "bybit", feature = "mexc", feature = "hyperliquid")))]
fn canary_order(_args: &[String]) -> Result<(), Box<dyn Error>> {
    Err("canary-order requires the bybit, mexc or hyperliquid Cargo feature".into())
}

#[cfg(any(feature = "bybit", feature = "mexc", feature = "hyperliquid"))]
fn canary_order(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let config = args.value("--config");
    let symbol = args.value("--symbol");
    let expected_user_id = args.value("--expected-user-id");
    let execute = args.flag("--execute");
    args.finish()?;
    let config = PathBuf::from(
        config
            .or_else(|| std::env::var("ENGINE_CONFIG_FILE").ok())
            .unwrap_or_else(|| "engine.toml".into()),
    );
    let symbol = symbol.ok_or("canary-order needs --symbol SYMBOL")?;
    let expected_user_id =
        expected_user_id.ok_or("canary-order needs --expected-user-id USER_ID")?;
    runtime()?.block_on(engine_tools::canary::run(
        &config,
        &symbol,
        &expected_user_id,
        execute,
    ))
}

fn replay(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let path = args.value("--wal");
    args.finish()?;
    let path = path.ok_or("replay needs --wal PATH")?;
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

fn fills(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let path = args.value("--wal");
    args.finish()?;
    let path = path.ok_or("fills needs --wal PATH")?;
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
        println!("{TORN_TAIL}");
    }
    Ok(())
}

fn latency(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let path = args.value("--wal");
    args.finish()?;
    let path = path.ok_or("latency needs --wal PATH")?;
    // The whole family, one record at a time: the table is a running fold,
    // so nothing here holds the log.
    let mut timings = engine_tools::timing::Timings::default();
    let mut records = 0_usize;
    let torn = engine_wal::replay_chain_visit(Path::new(&path), |_, record| {
        records += 1;
        timings.push(&record);
        Ok(())
    })?;
    let segments = engine_wal::segments(Path::new(&path))?.len();
    print!("{}", engine_tools::timing::report(&timings));
    println!("\n  {records} record(s), from {segments} log segment(s) under {path}.");
    if torn {
        println!("{TORN_TAIL}");
    }
    Ok(())
}

fn cohort(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let path = args.value("--wal");
    let json = args.flag("--json");
    args.finish()?;
    let path = path.ok_or("cohort needs --wal PATH")?;
    let (replayed, torn) = engine_wal::replay_chain(Path::new(&path))?;
    let segments = engine_wal::segments(Path::new(&path))?.len();
    let records: Vec<_> = replayed.into_iter().map(|(_, r)| r).collect();
    let report = engine_tools::cohort::of_log(&records);
    if json {
        println!("{}", serde_json::to_string_pretty(&report)?);
        return Ok(());
    }
    print!("{}", report.table());
    println!(
        "\n  {} record(s), from {} log segment(s) under {path}.",
        records.len(),
        segments
    );
    if torn {
        println!("{TORN_TAIL}");
    }
    Ok(())
}

fn reconcile_clear(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let config = args.value("--config");
    let note = args.value("--note");
    let execute = args.flag("--execute");
    args.finish()?;
    let config = PathBuf::from(config.unwrap_or_else(|| "engine.toml".into()));
    let note = note.unwrap_or_else(|| "operator reconcile-clear".into());
    runtime()?.block_on(engine_core::clear::run(&config, &note, execute))
}

fn initialize_native_strategy_state(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = config_or_default(args)?;
    runtime()?.block_on(engine_tools::takeover::initialize_native_strategy_state(
        &config,
    ))
}

fn verify_native_strategy_state(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = config_or_default(args)?;
    engine_tools::takeover::verify_native_strategy_state(&config)
}

fn rebind_native_strategy_state(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let previous = args.value("--previous-config");
    let config = args.value("--config");
    let execute = args.flag("--execute");
    args.finish()?;
    let previous = PathBuf::from(previous.ok_or("checkpoint rebind needs --previous-config PATH")?);
    let config = PathBuf::from(config.ok_or("checkpoint rebind needs --config PATH")?);
    runtime()?.block_on(engine_tools::takeover::rebind_native_strategy_state(
        &previous, &config, execute,
    ))
}

fn retire_legacy_signal_sources(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let config = args.value("--config");
    let plan = args.value("--plan");
    let execute = args.flag("--execute");
    args.finish()?;
    let config = PathBuf::from(config.ok_or("retire-legacy-signal-sources needs --config PATH")?);
    let plan = PathBuf::from(plan.ok_or("retire-legacy-signal-sources needs --plan PATH")?);
    let requests = serde_json::from_reader::<_, Vec<engine_core::legacy_signals::RetirementRequest>>(
        std::io::BufReader::new(std::fs::File::open(plan)?),
    )?;
    if requests.is_empty() {
        return Err("legacy source retirement plan is empty".into());
    }
    let retired = engine_core::legacy_signals::retire(&config, &requests, execute)?;
    println!("{}", serde_json::to_string_pretty(&retired)?);
    println!("legacy-source-retirement execute={execute}");
    Ok(())
}

fn set_strategy_entry_permission(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let config = args.value("--config");
    let strategy = args.value("--strategy");
    let entries_enabled = args.value("--entries-enabled");
    let request_id = args.value("--request-id");
    let wait_ms = args.value("--wait-ms");
    args.finish()?;
    let config = PathBuf::from(config.unwrap_or_else(|| "engine.toml".into()));
    let strategy = strategy.ok_or("set-strategy-entry-permission needs --strategy SLEEVE")?;
    let enabled = match entries_enabled.as_deref() {
        Some("true") => true,
        Some("false") => false,
        _ => return Err("--entries-enabled must be true or false".into()),
    };
    let request_id = request_id.ok_or("set-strategy-entry-permission needs --request-id ID")?;
    let wait_ms = wait_ms.unwrap_or_else(|| "30000".into()).parse::<u64>()?;
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

fn flatten_strategy(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut args = Args::new(args);
    let config = args.value("--config");
    let strategy = args.value("--strategy");
    let request_id = args.value("--request-id");
    let wait_ms = args.value("--wait-ms");
    args.finish()?;
    let config = PathBuf::from(config.unwrap_or_else(|| "engine.toml".into()));
    let strategy = strategy.ok_or("flatten-strategy needs --strategy SLEEVE")?;
    let request_id = request_id.ok_or("flatten-strategy needs --request-id ID")?;
    let wait_ms = wait_ms.unwrap_or_else(|| "30000".into()).parse::<u64>()?;
    runtime()?.block_on(submit_runtime_control(
        &config,
        &strategy,
        &request_id,
        engine_types::RuntimeControlCommand::FlattenDirectional,
        wait_ms,
    ))
}

pub(super) fn parse_backtest_options(args: &[String]) -> Result<BacktestOptions, Box<dyn Error>> {
    let mut args = Args::new(args);
    let required = |args: &mut Args, flag: &str| -> Result<PathBuf, Box<dyn Error>> {
        args.value(flag)
            .map(PathBuf::from)
            .ok_or_else(|| format!("engine backtest needs {flag} PATH").into())
    };
    let mut options = BacktestOptions {
        engine_config_path: required(&mut args, "--config")?,
        tape_path: required(&mut args, "--tape")?,
        instruments_path: required(&mut args, "--instruments")?,
        wal_path: required(&mut args, "--wal")?,
        ..BacktestOptions::default()
    };
    options.source_format = match args.value("--source").as_deref().unwrap_or("tape") {
        "tape" => engine_tools::backtest::source::SourceFormat::Tape,
        "normalized" => engine_tools::backtest::source::SourceFormat::Normalized,
        other => return Err(format!("unsupported historical source {other}").into()),
    };
    options.execution = match args.value("--execution").as_deref().unwrap_or("books") {
        "books" => engine_tools::backtest::execution::ExecutionModel::Books,
        mode @ ("trades" | "bars") => {
            let number = |args: &mut Args, flag: &str| -> Result<f64, Box<dyn Error>> {
                Ok(args
                    .value(flag)
                    .ok_or_else(|| format!("{mode} execution requires explicit {flag}"))?
                    .parse()?)
            };
            let spread_bps = number(&mut args, "--spread-bps")?;
            let slippage_bps = number(&mut args, "--slippage-bps")?;
            let participation = number(&mut args, "--participation")?;
            if mode == "trades" {
                engine_tools::backtest::execution::ExecutionModel::Trades {
                    spread_bps,
                    slippage_bps,
                    participation,
                }
            } else {
                engine_tools::backtest::execution::ExecutionModel::Bars {
                    spread_bps,
                    slippage_bps,
                    participation,
                }
            }
        }
        other => return Err(format!("unsupported execution mode {other}").into()),
    };
    options.execution.validate()?;
    options.signals_path = args.value("--signals").map(PathBuf::from);
    options.trades_path = args.value("--trades").map(PathBuf::from);
    options.equity_path = args.value("--equity").map(PathBuf::from);
    options.report_path = args.value("--report").map(PathBuf::from);
    if let Some(v) = args.value("--capital") {
        options.initial_capital_usdt = v.parse()?;
    }
    if let Some(v) = args.value("--taker-fee") {
        options.taker_fee_rate = Some(v.parse()?);
    }
    if let Some(v) = args.value("--maker-fee") {
        options.maker_fee_rate = Some(v.parse()?);
    }
    if let Some(v) = args.value("--rtt-ms") {
        options.order_rtt_ms = v.parse()?;
    }
    if let Some(v) = args.value("--private-latency-ms") {
        options.private_latency_ms = v.parse()?;
    }
    if let Some(v) = args.value("--mmr") {
        options.maintenance_margin_rate = v.parse()?;
    }
    options.durable_log = args.flag("--durable-log");
    args.finish()?;
    Ok(options)
}

pub(super) fn parse_sim_options(
    args: &[String],
) -> Result<(engine_tools::sim::SweepOptions, Option<PathBuf>), Box<dyn Error>> {
    let mut args = Args::new(args);
    let dir = args
        .value("--out")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::temp_dir().join(format!("engine-sim-{}", std::process::id())));
    let seed: u64 = args.value("--seed").unwrap_or_else(|| "1".into()).parse()?;
    let mut base = match args.value("--strategies") {
        None => engine_tools::sim::SimOptions::new(seed, dir),
        Some(name) => match engine_tools::sim::SimStrategies::parse(&name) {
            Some(engine_tools::sim::SimStrategies::Quoter) => {
                engine_tools::sim::SimOptions::new(seed, dir)
            }
            Some(engine_tools::sim::SimStrategies::Realm(realm)) => {
                engine_tools::sim::SimOptions::realm(seed, dir, realm)
            }
            None => {
                return Err(format!(
                    "--strategies takes quoter, demo, mainnet, mexc or hyperliquid, not {name:?}"
                )
                .into())
            }
        },
    };
    if let Some(v) = args.value("--hours") {
        base.hours(v.parse()?);
    }
    if let Some(v) = args.value("--seconds") {
        base.seconds = v.parse()?;
    }
    if let Some(v) = args.value("--symbols") {
        base.symbols = v.parse()?;
    }
    if let Some(v) = args.value("--tape-step-s") {
        base.tape_step_s = v.parse()?;
    }
    if let Some(v) = args.value("--capital") {
        base.capital = v.parse()?;
    }
    if let Some(v) = args.value("--shock") {
        base.shock = match v.as_str() {
            "on" => true,
            "off" => false,
            other => return Err(format!("--shock takes on or off, not {other:?}").into()),
        };
    }
    if let Some(v) = args.value("--pump") {
        base.pump_probability = v.parse()?;
    }
    base.gate = args.flag("--gate");
    if let Some(v) = args.value("--crashes") {
        base.crashes = v.parse()?;
    }
    if let Some(v) = args.value("--faults") {
        base.faults = engine_tools::sim::FaultRates::named(&v)
            .ok_or_else(|| format!("--faults takes none, light or heavy, not {v:?}"))?;
    }
    base.keep = args.flag("--keep");
    let seeds: u64 = args
        .value("--seeds")
        .unwrap_or_else(|| "1".into())
        .parse()?;
    let twice = args.flag("--twice");
    let report = args.value("--report").map(PathBuf::from);
    args.finish()?;
    Ok((
        engine_tools::sim::SweepOptions { base, seeds, twice },
        report,
    ))
}

pub(super) fn parse_bench_options(args: &[String]) -> Result<(BenchOptions, bool), Box<dyn Error>> {
    let mut args = Args::new(args);
    // The contention workload's rate, symbols and venue delay are chosen
    // together to keep openings queued, so it starts from its own defaults and
    // the flags below narrow them.
    let mut options = if args.flag("--contention") {
        BenchOptions::contention()
    } else {
        BenchOptions::default()
    };
    if let Some(v) = args.value("--cancel-after") {
        options.cancel_after = v.parse()?;
    }
    if let Some(v) = args.value("--ttl-ms") {
        options.ttl_ms = v.parse()?;
    }
    if let Some(v) = args.value("--events") {
        options.events = v.parse()?;
    }
    if let Some(v) = args.value("--rate") {
        options.rate = v.parse()?;
    }
    if let Some(v) = args.value("--every") {
        options.every_nth = v.parse()?;
    }
    if let Some(v) = args.value("--symbols") {
        options.symbols = v.split(',').map(|s| s.trim().to_string()).collect();
    }
    if let Some(v) = args.value("--wal") {
        options.wal_path = PathBuf::from(v);
    }
    options.quota = args.flag("--quota");
    if options.quota && !options.contention {
        return Err("--quota is a --contention dial".into());
    }
    if options.quota {
        options.venue_delay = crate::bench::QUOTA_VENUE_DELAY;
    }
    options.fills = args.flag("--fills");
    if let Some(ms) = args.value("--venue-delay-ms") {
        options.venue_delay = std::time::Duration::from_millis(
            ms.parse()
                .map_err(|_| "--venue-delay-ms wants whole milliseconds")?,
        );
    }
    let json = args.flag("--json");
    args.finish()?;
    Ok((options, json))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).into()).collect()
    }

    #[test]
    fn backtest_parser_preserves_required_paths_and_all_execution_options() {
        let options = parse_backtest_options(&args(&[
            "backtest",
            "--config",
            "config.toml",
            "--tape",
            "quotes",
            "--instruments",
            "rules",
            "--wal",
            "out.wal",
            "--signals",
            "inputs",
            "--trades",
            "trades",
            "--equity",
            "equity",
            "--report",
            "report",
            "--capital",
            "1234",
            "--taker-fee",
            "0.001",
            "--maker-fee",
            "0.0002",
            "--rtt-ms",
            "123",
            "--private-latency-ms",
            "45",
            "--mmr",
            "0.006",
            "--durable-log",
        ]))
        .unwrap();
        assert_eq!(options.engine_config_path, PathBuf::from("config.toml"));
        assert_eq!(options.tape_path, PathBuf::from("quotes"));
        assert_eq!(options.instruments_path, PathBuf::from("rules"));
        assert_eq!(options.wal_path, PathBuf::from("out.wal"));
        assert_eq!(options.signals_path, Some(PathBuf::from("inputs")));
        assert_eq!(options.trades_path, Some(PathBuf::from("trades")));
        assert_eq!(options.equity_path, Some(PathBuf::from("equity")));
        assert_eq!(options.report_path, Some(PathBuf::from("report")));
        assert_eq!(options.initial_capital_usdt, 1234.0);
        assert_eq!(options.taker_fee_rate, Some(0.001));
        assert_eq!(options.maker_fee_rate, Some(0.0002));
        assert_eq!(options.order_rtt_ms, 123);
        assert_eq!(options.private_latency_ms, 45);
        assert_eq!(options.maintenance_margin_rate, 0.006);
        assert!(options.durable_log);
    }

    #[test]
    fn parser_errors_and_dispatch_routing_do_not_need_a_runtime() {
        for (command, expected) in [
            ("backtest", "engine backtest needs --config PATH"),
            ("wal-cost", "wal-cost needs --wal PATH"),
            ("wal-retention", "wal-retention needs --wal PATH"),
            ("replay", "replay needs --wal PATH"),
            ("fills", "fills needs --wal PATH"),
            ("latency", "latency needs --wal PATH"),
            ("cohort", "cohort needs --wal PATH"),
            ("canary-order", "canary-order needs --symbol SYMBOL"),
            (
                "flatten-strategy",
                "flatten-strategy needs --strategy SLEEVE",
            ),
        ] {
            assert_eq!(
                dispatch(&args(&[command])).unwrap_err().to_string(),
                expected
            );
        }
        assert!(dispatch(&args(&["unknown-command"]))
            .unwrap_err()
            .to_string()
            .starts_with("unknown command unknown-command\n"));
        assert!(dispatch(&args(&["import-strategy-state"]))
            .unwrap_err()
            .to_string()
            .starts_with("unknown command import-strategy-state\n"));
        assert!(parse_bench_options(&args(&["bench", "--events", "no"])).is_err());
        assert_eq!(
            parse_bench_options(&args(&["bench", "--venue-delay-ms", "0.5"]))
                .err()
                .unwrap()
                .to_string(),
            "--venue-delay-ms wants whole milliseconds"
        );
    }

    #[test]
    fn an_undeclared_argument_stops_the_command_it_was_meant_for() {
        assert_eq!(
            parse_bench_options(&args(&["bench", "--contention", "--jsn"]))
                .unwrap_err()
                .to_string(),
            "bench does not take --jsn"
        );
        assert_eq!(
            dispatch(&args(&["latency", "--wal", "engine.wal", "--tail", "20"]))
                .unwrap_err()
                .to_string(),
            "latency does not take --tail 20"
        );
        assert_eq!(
            dispatch(&args(&["cohort", "--wal", "engine.wal", "spare"]))
                .unwrap_err()
                .to_string(),
            "cohort does not take spare"
        );
        let (options, json) = parse_bench_options(&args(&["bench", "--json"])).unwrap();
        assert!(json);
        assert!(!options.contention);
    }

    #[test]
    fn bench_parser_preserves_symbols_fills_and_delay() {
        let (options, json) = parse_bench_options(&args(&[
            "bench",
            "--events",
            "4",
            "--rate",
            "2",
            "--every",
            "3",
            "--symbols",
            " BTCUSDT , ETHUSDT ",
            "--wal",
            "sample.wal",
            "--fills",
            "--venue-delay-ms",
            "7",
        ]))
        .unwrap();
        assert_eq!(options.events, 4);
        assert_eq!(options.rate, 2);
        assert_eq!(options.every_nth, 3);
        assert_eq!(options.symbols, ["BTCUSDT", "ETHUSDT"]);
        assert_eq!(options.wal_path, PathBuf::from("sample.wal"));
        assert!(options.fills);
        assert_eq!(options.venue_delay, std::time::Duration::from_millis(7));
        assert!(!json, "the table unless asked for JSON");
        assert!(!options.contention, "off unless asked");
        assert_eq!(options.ttl_ms, 10_000, "the engine's own default");
    }

    #[test]
    fn the_local_quota_is_a_contention_dial_and_nothing_else() {
        let (options, _) =
            parse_bench_options(&args(&["bench", "--contention", "--quota"])).unwrap();
        assert!(options.quota);
        assert_eq!(options.venue_delay, crate::bench::QUOTA_VENUE_DELAY);
        let (slow, _) = parse_bench_options(&args(&[
            "bench",
            "--contention",
            "--quota",
            "--venue-delay-ms",
            "200",
        ]))
        .unwrap();
        assert_eq!(
            slow.venue_delay,
            std::time::Duration::from_millis(200),
            "a stated delay is not overridden by the dial's own"
        );
        let (plain, _) = parse_bench_options(&args(&["bench", "--contention"])).unwrap();
        assert!(!plain.quota, "off unless asked");
        assert_eq!(
            plain.venue_delay,
            std::time::Duration::from_millis(200),
            "the contention default is what it was"
        );
        assert_eq!(
            parse_bench_options(&args(&["bench", "--quota"]))
                .unwrap_err()
                .to_string(),
            "--quota is a --contention dial"
        );
    }

    #[test]
    fn the_contention_flag_brings_its_own_defaults_and_the_flags_narrow_them() {
        let (options, _) = parse_bench_options(&args(&["bench", "--contention"])).unwrap();
        assert!(options.contention);
        assert_eq!(options.rate, 200);
        assert_eq!(options.every_nth, 1);
        assert_eq!(options.cancel_after, 3);
        assert_eq!(options.venue_delay, std::time::Duration::from_millis(200));
        assert_eq!(
            options.symbols.len(),
            4,
            "three openings queue behind the one being answered"
        );
        let (narrowed, _) = parse_bench_options(&args(&[
            "bench",
            "--contention",
            "--cancel-after",
            "5",
            "--ttl-ms",
            "250",
            "--events",
            "800",
            "--venue-delay-ms",
            "20",
        ]))
        .unwrap();
        assert_eq!(narrowed.cancel_after, 5);
        assert_eq!(narrowed.ttl_ms, 250);
        assert_eq!(narrowed.events, 800);
        assert_eq!(narrowed.venue_delay, std::time::Duration::from_millis(20));
        assert_eq!(narrowed.rate, 200, "the contention default survives");
    }
}
