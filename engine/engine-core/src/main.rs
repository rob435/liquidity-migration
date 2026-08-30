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

  engine attest-flat --config engine.toml
      Read every account position and open order surface known by the venue
      adapter. Succeeds only when the credential-wide inventory is fresh and
      empty. Sends no orders and changes no venue state.

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
        "attest-flat" => {
            let config = PathBuf::from(
                value(args, "--config")
                    .or_else(|| std::env::var("ENGINE_CONFIG_FILE").ok())
                    .unwrap_or("engine.toml".into()),
            );
            runtime()?.block_on(engine_core::flatness::run(&config))
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
            print!("{}", execution::report::of_log(&records));
            // Naming a numbered segment reads that segment alone, and the
            // table looks exactly the same either way.
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
