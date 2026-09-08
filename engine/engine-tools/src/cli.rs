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
    let options = parse_bench_options(args)?;
    let result = runtime()?.block_on(bench::run(&options))?;
    println!(
        "\nbench: {} quotes in, {} completed submit attempts, against a local synthetic venue",
        result.events, result.orders
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
    let path = PathBuf::from(value(args, "--wal").ok_or("wal-cost needs --wal PATH")?);
    let appends: usize = value(args, "--appends")
        .unwrap_or_else(|| "20000".into())
        .parse()?;
    let barriers: usize = value(args, "--barriers")
        .unwrap_or_else(|| "200".into())
        .parse()?;
    let costs = engine_wal::measure(&path, appends, barriers)?;
    println!("wal-cost path={}", path.display());
    println!("{costs}");
    println!("  the barrier measures synchronous fsync; engine bench separates callback, queued-dispatch and attempted-send barriers.");
    println!("  compare against a memory-backed path to bound what faster storage buys.");
    Ok(())
}

fn wal_convert_v5(args: &[String]) -> Result<(), Box<dyn Error>> {
    let input = PathBuf::from(value(args, "--wal").ok_or("wal-convert-v5 needs --wal PATH")?);
    let output = PathBuf::from(
        value(args, "--output-dir").ok_or("wal-convert-v5 needs --output-dir NEW_DIRECTORY")?,
    );
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
    let path = PathBuf::from(value(args, "--config").unwrap_or_else(|| "engine.toml".into()));
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

fn attest_flat(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(
        value(args, "--config")
            .or_else(|| std::env::var("ENGINE_CONFIG_FILE").ok())
            .unwrap_or_else(|| "engine.toml".into()),
    );
    runtime()?.block_on(engine_tools::flatness::run(&config))
}

fn verify_account_identity(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(
        value(args, "--config")
            .or_else(|| std::env::var("ENGINE_CONFIG_FILE").ok())
            .unwrap_or_else(|| "engine.toml".into()),
    );
    runtime()?.block_on(engine_tools::flatness::verify_account_identity(&config))
}

#[cfg(not(feature = "bybit"))]
fn canary_order(_args: &[String]) -> Result<(), Box<dyn Error>> {
    Err("canary-order requires the bybit Cargo feature".into())
}

#[cfg(feature = "bybit")]
fn canary_order(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(value(args, "--config").unwrap_or_else(|| "engine.toml".into()));
    let symbol = value(args, "--symbol").ok_or("canary-order needs --symbol SYMBOL")?;
    let expected_user_id =
        value(args, "--expected-user-id").ok_or("canary-order needs --expected-user-id USER_ID")?;
    let execute = args.iter().any(|arg| arg == "--execute");
    runtime()?.block_on(engine_tools::canary::run(
        &config,
        &symbol,
        &expected_user_id,
        execute,
    ))
}

fn replay(args: &[String]) -> Result<(), Box<dyn Error>> {
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

fn fills(args: &[String]) -> Result<(), Box<dyn Error>> {
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

fn latency(args: &[String]) -> Result<(), Box<dyn Error>> {
    let path = value(args, "--wal").ok_or("latency needs --wal PATH")?;
    let (replayed, torn) = engine_wal::replay_chain(Path::new(&path))?;
    let segments = engine_wal::segments(Path::new(&path))?.len();
    let records: Vec<_> = replayed.into_iter().map(|(_, r)| r).collect();
    print!("{}", engine_tools::timing::of_log(&records));
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

fn reconcile_clear(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(value(args, "--config").unwrap_or_else(|| "engine.toml".into()));
    let note = value(args, "--note").unwrap_or_else(|| "operator reconcile-clear".into());
    let execute = args.iter().any(|a| a == "--execute");
    runtime()?.block_on(engine_core::clear::run(&config, &note, execute))
}

fn initialize_native_strategy_state(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(value(args, "--config").unwrap_or_else(|| "engine.toml".into()));
    runtime()?.block_on(engine_tools::takeover::initialize_native_strategy_state(
        &config,
    ))
}

fn verify_native_strategy_state(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(value(args, "--config").unwrap_or_else(|| "engine.toml".into()));
    engine_tools::takeover::verify_native_strategy_state(&config)
}

fn rebind_native_strategy_state(args: &[String]) -> Result<(), Box<dyn Error>> {
    let previous = PathBuf::from(
        value(args, "--previous-config").ok_or("checkpoint rebind needs --previous-config PATH")?,
    );
    let config =
        PathBuf::from(value(args, "--config").ok_or("checkpoint rebind needs --config PATH")?);
    runtime()?.block_on(engine_tools::takeover::rebind_native_strategy_state(
        &previous,
        &config,
        args.iter().any(|arg| arg == "--execute"),
    ))
}

fn retire_legacy_signal_sources(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(
        value(args, "--config").ok_or("retire-legacy-signal-sources needs --config PATH")?,
    );
    let plan = PathBuf::from(
        value(args, "--plan").ok_or("retire-legacy-signal-sources needs --plan PATH")?,
    );
    let requests = serde_json::from_reader::<_, Vec<engine_core::legacy_signals::RetirementRequest>>(
        std::io::BufReader::new(std::fs::File::open(plan)?),
    )?;
    if requests.is_empty() {
        return Err("legacy source retirement plan is empty".into());
    }
    let execute = args.iter().any(|arg| arg == "--execute");
    let retired = engine_core::legacy_signals::retire(&config, &requests, execute)?;
    println!("{}", serde_json::to_string_pretty(&retired)?);
    println!("legacy-source-retirement execute={execute}");
    Ok(())
}

fn set_strategy_entry_permission(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(value(args, "--config").unwrap_or_else(|| "engine.toml".into()));
    let strategy =
        value(args, "--strategy").ok_or("set-strategy-entry-permission needs --strategy SLEEVE")?;
    let enabled = match value(args, "--entries-enabled").as_deref() {
        Some("true") => true,
        Some("false") => false,
        _ => return Err("--entries-enabled must be true or false".into()),
    };
    let request_id =
        value(args, "--request-id").ok_or("set-strategy-entry-permission needs --request-id ID")?;
    let wait_ms = value(args, "--wait-ms")
        .unwrap_or_else(|| "30000".into())
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

fn flatten_strategy(args: &[String]) -> Result<(), Box<dyn Error>> {
    let config = PathBuf::from(value(args, "--config").unwrap_or_else(|| "engine.toml".into()));
    let strategy = value(args, "--strategy").ok_or("flatten-strategy needs --strategy SLEEVE")?;
    let request_id = value(args, "--request-id").ok_or("flatten-strategy needs --request-id ID")?;
    let wait_ms = value(args, "--wait-ms")
        .unwrap_or_else(|| "30000".into())
        .parse::<u64>()?;
    runtime()?.block_on(submit_runtime_control(
        &config,
        &strategy,
        &request_id,
        engine_types::RuntimeControlCommand::FlattenDirectional,
        wait_ms,
    ))
}

pub(super) fn parse_backtest_options(args: &[String]) -> Result<BacktestOptions, Box<dyn Error>> {
    let required = |flag: &str| -> Result<PathBuf, Box<dyn Error>> {
        value(args, flag)
            .map(PathBuf::from)
            .ok_or_else(|| format!("engine backtest needs {flag} PATH").into())
    };
    let mut options = BacktestOptions {
        engine_config_path: required("--config")?,
        tape_path: required("--tape")?,
        instruments_path: required("--instruments")?,
        wal_path: required("--wal")?,
        ..BacktestOptions::default()
    };
    options.source_format = match value(args, "--source").as_deref().unwrap_or("tape") {
        "tape" => engine_tools::backtest::source::SourceFormat::Tape,
        "normalized" => engine_tools::backtest::source::SourceFormat::Normalized,
        other => return Err(format!("unsupported historical source {other}").into()),
    };
    options.execution = match value(args, "--execution").as_deref().unwrap_or("books") {
        "books" => engine_tools::backtest::execution::ExecutionModel::Books,
        mode @ ("trades" | "bars") => {
            let number = |flag| -> Result<f64, Box<dyn Error>> {
                Ok(value(args, flag)
                    .ok_or_else(|| format!("{mode} execution requires explicit {flag}"))?
                    .parse()?)
            };
            let spread_bps = number("--spread-bps")?;
            let slippage_bps = number("--slippage-bps")?;
            let participation = number("--participation")?;
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
    options.signals_path = value(args, "--signals").map(PathBuf::from);
    options.trades_path = value(args, "--trades").map(PathBuf::from);
    options.equity_path = value(args, "--equity").map(PathBuf::from);
    options.report_path = value(args, "--report").map(PathBuf::from);
    if let Some(v) = value(args, "--capital") {
        options.initial_capital_usdt = v.parse()?;
    }
    if let Some(v) = value(args, "--taker-fee") {
        options.taker_fee_rate = Some(v.parse()?);
    }
    if let Some(v) = value(args, "--maker-fee") {
        options.maker_fee_rate = Some(v.parse()?);
    }
    if let Some(v) = value(args, "--rtt-ms") {
        options.order_rtt_ms = v.parse()?;
    }
    if let Some(v) = value(args, "--private-latency-ms") {
        options.private_latency_ms = v.parse()?;
    }
    if let Some(v) = value(args, "--mmr") {
        options.maintenance_margin_rate = v.parse()?;
    }
    options.durable_log = args.iter().any(|a| a == "--durable-log");
    Ok(options)
}

pub(super) fn parse_sim_options(
    args: &[String],
) -> Result<(engine_tools::sim::SweepOptions, Option<PathBuf>), Box<dyn Error>> {
    let dir = value(args, "--out")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::temp_dir().join(format!("engine-sim-{}", std::process::id())));
    let seed: u64 = value(args, "--seed")
        .unwrap_or_else(|| "1".into())
        .parse()?;
    let mut base = engine_tools::sim::SimOptions::new(seed, dir);
    if let Some(v) = value(args, "--seconds") {
        base.seconds = v.parse()?;
    }
    if let Some(v) = value(args, "--symbols") {
        base.symbols = v.parse()?;
    }
    if let Some(v) = value(args, "--crashes") {
        base.crashes = v.parse()?;
    }
    if let Some(v) = value(args, "--faults") {
        base.faults = engine_tools::sim::FaultRates::named(&v)
            .ok_or_else(|| format!("--faults takes none, light or heavy, not {v:?}"))?;
    }
    base.keep = args.iter().any(|a| a == "--keep");
    let seeds: u64 = value(args, "--seeds")
        .unwrap_or_else(|| "1".into())
        .parse()?;
    let twice = args.iter().any(|a| a == "--twice");
    let report = value(args, "--report").map(PathBuf::from);
    Ok((
        engine_tools::sim::SweepOptions { base, seeds, twice },
        report,
    ))
}

pub(super) fn parse_bench_options(args: &[String]) -> Result<BenchOptions, Box<dyn Error>> {
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
    Ok(options)
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
            ("replay", "replay needs --wal PATH"),
            ("fills", "fills needs --wal PATH"),
            ("latency", "latency needs --wal PATH"),
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
    fn bench_parser_preserves_symbols_fills_and_delay() {
        let options = parse_bench_options(&args(&[
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
    }
}
