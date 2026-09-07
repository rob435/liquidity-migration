//! One seed, start to finish: the world is built, the engine boots from an
//! empty log, runs against the seeded market and faults, dies when the seed
//! says so, boots again from what the log holds, and is judged when the
//! tape ends.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_types::{AccountView, Subscription, Symbol, WalRecord};
use engine_wal::WalWriter;
use sha2::{Digest, Sha256};

use super::faults::{
    shared_rng, FaultLog, FaultRates, FaultyGateway, FaultyMarketFeed, FaultyOrderFeed,
    SharedFaultLog, SharedRng,
};
use super::invariants::{self, Check, Evidence};
use super::market::{self, MarketPlan};
use super::rng::Rng;
use crate::assembly;
use crate::backtest::feed::{pump, Cursor, SharedCursor, TapeFeed};
use crate::backtest::runner::{read_engine_ledger, BacktestOptions};
use crate::backtest::scheduler::{Scheduler, VirtualTimer, WaiterKind, YieldNow};
use crate::backtest::signals::SignalReplayFeed;
use crate::backtest::tape::{read_instruments, TapeReader};
use crate::backtest::venue::{
    Accounting, SimOrderFeed, SimVenueGateway, SimulatedVenue, VenueParams,
};
use crate::config::{self, EngineSection, LoadedConfig};
use crate::controls::NoControls;
use crate::engine::{Engine, EngineError, RunOutcome};
use crate::trades::Trades;

/// Deaths are drawn from the middle of the tape, so every segment has a
/// market to trade and the last one has time to catch up.
const DEATH_WINDOW: (f64, f64) = (0.1, 0.9);

#[derive(Clone, Debug)]
pub struct SimOptions {
    pub seed: u64,
    /// Length of the synthetic tape.
    pub seconds: u64,
    /// How many of the catalogue's symbols the quoter trades (1 to 3).
    pub symbols: usize,
    /// Process deaths to inject.
    pub crashes: u32,
    pub faults: FaultRates,
    /// Where this seed's files go. Removed afterwards unless `keep`.
    pub dir: PathBuf,
    pub keep: bool,
}

impl SimOptions {
    pub fn new(seed: u64, dir: PathBuf) -> Self {
        SimOptions {
            seed,
            seconds: 600,
            symbols: 2,
            crashes: 1,
            faults: FaultRates::LIGHT,
            dir,
            keep: false,
        }
    }
}

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct SimReport {
    pub callback_execution: &'static str,
    pub seed: u64,
    pub seconds: u64,
    pub symbols: usize,
    pub crashes_injected: u32,
    /// Times the engine exited on its own for its supervisor to boot it again.
    pub restarts: u32,
    /// Why, one line per restart.
    pub restart_reasons: Vec<String>,
    /// Boots, including the first.
    pub segments: u32,
    pub market_events: u64,
    pub orders_sent: u64,
    pub stopped_by: String,
    pub faults: BTreeMap<String, u64>,
    pub venue: Accounting,
    pub checks: Vec<Check>,
    /// Observations that do not fail the run but the owner should see.
    pub notes: Vec<String>,
    pub wal_records: usize,
    pub wal_sha256: String,
}

impl SimReport {
    pub fn passed(&self) -> bool {
        self.checks.iter().all(|c| c.passed)
    }

    pub fn failures(&self) -> Vec<&Check> {
        self.checks.iter().filter(|c| !c.passed).collect()
    }

    /// One line for the sweep table.
    pub fn line(&self) -> String {
        let faults: u64 = self.faults.values().sum();
        format!(
            "seed {:>6}  embedded  {}  events {:>7}  orders {:>5}  fills {:>5}  deaths {}  restarts {}  faults {:>4}  wal {}",
            self.seed,
            if self.passed() { "ok  " } else { "FAIL" },
            self.market_events,
            self.orders_sent,
            self.venue.fills,
            self.crashes_injected,
            self.restarts,
            faults,
            &self.wal_sha256[..12],
        )
    }
}

struct Paths {
    dir: PathBuf,
    tape: PathBuf,
    instruments: PathBuf,
    config: PathBuf,
    wal: PathBuf,
    trades: PathBuf,
}

impl Paths {
    fn create(dir: &Path) -> Result<Self, EngineError> {
        let _ = std::fs::remove_dir_all(dir);
        std::fs::create_dir_all(dir).map_err(|e| boot(format!("{}: {e}", dir.display())))?;
        Ok(Paths {
            dir: dir.to_path_buf(),
            tape: dir.join("tape.jsonl"),
            instruments: dir.join("instruments.json"),
            config: dir.join("engine.toml"),
            wal: dir.join("run.wal"),
            trades: dir.join("trades.jsonl"),
        })
    }
}

fn boot(message: impl Into<String>) -> EngineError {
    EngineError::Boot(message.into())
}

fn state(message: impl Into<String>) -> EngineError {
    EngineError::State(message.into())
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// The parts that outlive a process death: the market, the venue, the log
/// on disk, and the seed's remaining decisions.
struct World {
    opts: SimOptions,
    paths: Paths,
    loaded: LoadedConfig,
    settings: EngineSection,
    sleeves: Vec<String>,
    scheduler: Scheduler,
    venue: Arc<Mutex<SimulatedVenue>>,
    cursor: SharedCursor,
    venue_rng: SharedRng,
    private_rng: SharedRng,
    market_rng: SharedRng,
    log: SharedFaultLog,
    deaths: Vec<u64>,
    rtt: Duration,
    private_latency: Duration,
}

/// How one boot of the engine ended.
enum SegmentEnd {
    Stopped {
        outcome: RunOutcome,
        in_flight: Vec<String>,
        account: AccountView,
    },
    /// The seeded death.
    Died,
    /// The engine exited with an error, which is what the live unit does
    /// when a halt cancel's fate cannot be learned in time or a boot-time
    /// venue read fails; the supervisor boots it again.
    Restart(String),
}

/// More restarts than this in one run is a crash loop, which is a fault.
const MAX_RESTARTS: u32 = 8;

impl World {
    fn build(
        opts: SimOptions,
    ) -> Result<(Self, engine_types::clock::VirtualClockGuard), EngineError> {
        let paths = Paths::create(&opts.dir)?;
        let plan = MarketPlan::new(opts.symbols, opts.seconds);
        let mut seed = Rng::new(opts.seed);
        let market_walk = seed.fork(0x4d41_524b);
        let io = |e: std::io::Error| boot(format!("writing the world: {e}"));
        market::write_tape(&paths.tape, &plan, market_walk).map_err(io)?;
        market::write_instruments(&paths.instruments, &plan).map_err(io)?;
        market::write_engine_config(&paths.config, &plan).map_err(io)?;

        let loaded = config::load(&paths.config).map_err(|e| boot(format!("config: {e}")))?;
        let mut settings = loaded.config.engine.clone();
        settings.wal_path = paths.wal.clone();
        settings.trades_path = Some(paths.trades.clone());
        settings.heartbeat_path = None;
        settings.signal_spool_path = None;
        settings.control_spool_path = None;
        let sleeves: Vec<String> = loaded
            .config
            .strategies
            .iter()
            .map(|s| s.sleeve_name().to_string())
            .collect();
        let probe =
            assembly::strategies(&loaded.config.strategies).map_err(|e| boot(e.to_string()))?;
        let wanted: Vec<Subscription> = probe.iter().flat_map(|s| s.subscriptions()).collect();
        drop(probe);
        let catalog =
            read_instruments(&paths.instruments).map_err(|e| boot(format!("instruments: {e}")))?;
        let symbols: Vec<Symbol> = assembly::symbol_order(&[], &wanted)
            .map_err(|error| boot(format!("symbol order: {error}")))?;

        let defaults = BacktestOptions::default();
        let rtt = Duration::from_millis(defaults.order_rtt_ms);
        let private_latency = Duration::from_millis(defaults.private_latency_ms);
        let scheduler = Scheduler::default();
        let venue = Arc::new(Mutex::new(SimulatedVenue::new(
            VenueParams {
                initial_cash_usdt: defaults.initial_capital_usdt,
                taker_fee_rate: defaults.taker_fee_rate,
                maker_fee_rate: defaults.maker_fee_rate,
                order_rtt_ns: rtt.as_nanos() as u64,
                private_latency_ns: private_latency.as_nanos() as u64,
                default_leverage: loaded
                    .config
                    .risk
                    .get("leverage")
                    .and_then(toml::Value::as_float)
                    .unwrap_or(1.0),
                maintenance_margin_rate: defaults.maintenance_margin_rate,
            },
            symbols.clone(),
            &catalog,
            scheduler.clone(),
        )));
        let reader = TapeReader::open(&paths.tape).map_err(|e| boot(format!("tape: {e}")))?;
        let subscriptions = assembly::boot_subscriptions(&symbols, &wanted);
        let cursor: SharedCursor = Arc::new(Mutex::new(Cursor::new(
            reader,
            venue.clone(),
            &symbols,
            &subscriptions,
        )));
        let start_ns = lock(&cursor)
            .next_row_at()
            .map_err(|e| boot(format!("tape: {e}")))?
            .ok_or_else(|| boot("the synthetic tape has no rows"))?;
        let clock = engine_types::clock::install_virtual(start_ns, start_ns)
            .map_err(|e| boot(format!("virtual clock: {e}")))?;
        scheduler.advance_to(start_ns);

        let span = (plan.end_ns() - start_ns) as f64;
        let mut death_rng = seed.fork(0x4445_4154);
        let mut deaths: Vec<u64> = (0..opts.crashes)
            .map(|_| start_ns + (span * death_rng.between(DEATH_WINDOW.0, DEATH_WINDOW.1)) as u64)
            .collect();
        deaths.sort_unstable();

        Ok((
            World {
                venue_rng: shared_rng(seed.fork(0x5645_4e55)),
                private_rng: shared_rng(seed.fork(0x5052_4956)),
                market_rng: shared_rng(seed.fork(0x4645_4544)),
                log: FaultLog::shared(),
                opts,
                paths,
                loaded,
                settings,
                sleeves,
                scheduler,
                venue,
                cursor,
                deaths,
                rtt,
                private_latency,
            },
            clock,
        ))
    }

    /// One boot of the engine, to a clean stop or to the seeded death.
    async fn run_segment(
        &self,
        death_at: Option<u64>,
        reconnecting: bool,
    ) -> Result<SegmentEnd, EngineError> {
        let _claim = engine_wal::lock(&self.paths.wal).map_err(|e| boot(e.to_string()))?;
        let (wal, replayed) = WalWriter::open_unsynced(&self.paths.wal)?;
        let replayed: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
        let strategies = assembly::strategies(&self.loaded.config.strategies)
            .map_err(|e| boot(e.to_string()))?;
        let risk = assembly::risk(&self.loaded.config.risk).map_err(|e| boot(e.to_string()))?;

        let gateway = FaultyGateway::new(
            SimVenueGateway::new(
                self.venue.clone(),
                self.scheduler.clone(),
                self.rtt.as_nanos() as u64,
            ),
            self.opts.faults,
            self.venue_rng.clone(),
            self.scheduler.clone(),
            self.log.clone(),
            self.rtt * 3,
        );
        let mut order_feed = FaultyOrderFeed::new(
            SimOrderFeed::new(self.venue.clone(), self.scheduler.clone()),
            self.opts.faults,
            self.private_rng.clone(),
            self.scheduler.clone(),
            self.log.clone(),
            self.private_latency * 4,
            reconnecting,
        );
        let mut market_feed = FaultyMarketFeed::new(
            TapeFeed::new(self.cursor.clone(), self.scheduler.clone()),
            self.opts.faults,
            self.market_rng.clone(),
            self.scheduler.clone(),
            self.log.clone(),
        );
        let mut signal_feed = SignalReplayFeed::empty(self.scheduler.clone());
        let mut controls = NoControls;

        // The pump runs before boot: after a death the clock is already
        // pumping, and boot's venue reads wait on it like every other reply.
        let pump_task = tokio::spawn(pump(self.cursor.clone(), self.scheduler.clone()));
        let mut engine = match Engine::boot_as_exact(
            &self.settings,
            &self.loaded.sha256,
            wal,
            risk,
            gateway,
            strategies,
            &self.sleeves,
            &replayed,
        )
        .await
        {
            Ok(engine) => engine,
            Err(error) => {
                pump_task.abort();
                lock(&self.venue).drop_private_queue();
                FaultLog::note(&self.log, "engine.restart");
                return Ok(SegmentEnd::Restart(format!("boot: {error}")));
            }
        };
        engine.write_trades(Trades::new(self.paths.trades.clone()));

        let death = death_at_instant(self.scheduler.clone(), death_at);
        tokio::pin!(death);
        let outcome = tokio::select! {
            biased;
            result = engine.run_with_inputs_on(
                &mut market_feed,
                &mut order_feed,
                &mut signal_feed,
                &mut controls,
                std::future::pending::<()>(),
                VirtualTimer::new(self.scheduler.clone()),
            ) => Some(result),
            () = &mut death => None,
        };
        pump_task.abort();
        match outcome {
            Some(Ok(outcome)) => Ok(SegmentEnd::Stopped {
                outcome,
                in_flight: engine
                    .in_flight_ids()
                    .iter()
                    .map(|s| (*s).to_string())
                    .collect(),
                account: engine.account().clone(),
            }),
            Some(Err(error)) => {
                // A non-zero exit, whatever the reason: the supervisor boots
                // the unit again. Persistent reasons show up as a restart
                // loop, which the judge fails.
                drop(engine);
                lock(&self.venue).drop_private_queue();
                FaultLog::note(&self.log, "engine.restart");
                Ok(SegmentEnd::Restart(error.to_string()))
            }
            None => {
                // The process is gone: its memory, its private socket, and
                // whatever the venue was about to tell it.
                drop(engine);
                let lost = lock(&self.venue).drop_private_queue();
                FaultLog::note(&self.log, "process.death");
                if lost > 0 {
                    FaultLog::note(&self.log, "process.death_lost_private_updates");
                }
                Ok(SegmentEnd::Died)
            }
        }
    }
}

/// Resolves at the seeded virtual instant, once the tape is pumping the
/// clock; never, when there is no death left to inject.
async fn death_at_instant(scheduler: Scheduler, at: Option<u64>) {
    let Some(at) = at else {
        std::future::pending::<()>().await;
        return;
    };
    while !scheduler.is_pumped() {
        YieldNow::new().await;
    }
    scheduler.sleep_until(at, WaiterKind::World).await;
}

fn sha256_of(path: &Path) -> Result<String, EngineError> {
    let bytes = std::fs::read(path).map_err(|e| state(format!("{}: {e}", path.display())))?;
    Ok(hex::encode(Sha256::digest(&bytes)))
}

/// Run one seed to its verdict.
pub async fn run_seed(opts: SimOptions) -> Result<SimReport, EngineError> {
    let keep = opts.keep;
    let (world, _clock) = World::build(opts)?;

    let mut deaths_done = 0usize;
    let mut restarts = 0u32;
    let mut restart_reasons: Vec<String> = Vec::new();
    let mut segments = 0u32;
    let mut stopped: Option<(RunOutcome, Vec<String>, AccountView)> = None;
    let mut engine_error: Option<String> = None;
    loop {
        segments += 1;
        let death_at = world.deaths.get(deaths_done).copied();
        match world.run_segment(death_at, segments > 1).await {
            Ok(SegmentEnd::Stopped {
                outcome,
                in_flight,
                account,
            }) => {
                stopped = Some((outcome, in_flight, account));
                break;
            }
            Ok(SegmentEnd::Died) => {
                deaths_done += 1;
            }
            Ok(SegmentEnd::Restart(message)) => {
                restarts += 1;
                restart_reasons.push(message.clone());
                if restarts > MAX_RESTARTS {
                    engine_error = Some(format!(
                        "restart loop: the engine asked to be restarted {restarts} times; last: {message}"
                    ));
                    break;
                }
            }
            Err(error) => {
                engine_error = Some(error.to_string());
                break;
            }
        }
    }

    let records: Vec<WalRecord> = engine_wal::replay(&world.paths.wal)?
        .into_iter()
        .map(|(_, r)| r)
        .collect();
    // Counted from what survives every death: the tape and the log.
    let market_events = lock(&world.cursor).stats().rows;
    let orders_sent = records
        .iter()
        .filter(|r| matches!(r, WalRecord::OrderSent { .. }))
        .count() as u64;
    // The ledger as `engine fills` reads it, from the log. The trades file
    // is judged separately: a process writes only the trips that close
    // while it is alive.
    let log_ledger = crate::execution::Fills::try_from_records(&records).map_err(state)?;
    let closed_in_log: Vec<f64> = log_ledger
        .closed()
        .iter()
        .filter_map(|trade| trade.round_trip.as_ref())
        .map(|trip| trip.net_usdt)
        .collect();
    let ledger_net_usdt = (!closed_in_log.is_empty()).then(|| closed_in_log.iter().sum());
    let mut notes = Vec::new();
    let file_ledger = if world.paths.trades.exists() {
        Some(read_engine_ledger(&world.paths.trades)?)
    } else {
        None
    };
    if let Some(file) = &file_ledger {
        let in_log = log_ledger.closed().len() as u64;
        if file.closed_trips != in_log {
            notes.push(format!(
                "trades file holds {} of the {} round trips the log closes; trips closed while no process was alive are never written",
                file.closed_trips, in_log
            ));
        }
    }
    let wal_sha256 = sha256_of(&world.paths.wal)?;
    let (venue_view, venue_orders, venue_executions, venue_accounting) = {
        let venue = lock(&world.venue);
        (
            venue.account_view(),
            venue.working_orders(),
            venue.executions().to_vec(),
            venue.accounting(),
        )
    };
    let stopped_by = stopped
        .as_ref()
        .map(|(o, _, _)| format!("{:?}", o.stopped_by));
    let checks = invariants::all(&Evidence {
        records: &records,
        venue_view: &venue_view,
        venue_orders: &venue_orders,
        venue_executions: &venue_executions,
        venue_accounting: &venue_accounting,
        engine_in_flight: stopped.as_ref().map(|(_, ids, _)| ids.as_slice()),
        engine_account: stopped.as_ref().map(|(_, _, account)| account),
        stopped_by: stopped_by.as_deref(),
        engine_error: engine_error.as_deref(),
        ledger_net_usdt,
    });
    let report = SimReport {
        callback_execution: "embedded",
        seed: world.opts.seed,
        seconds: world.opts.seconds,
        symbols: world.opts.symbols,
        crashes_injected: deaths_done as u32,
        restarts,
        restart_reasons,
        segments,
        market_events,
        orders_sent,
        stopped_by: stopped_by.unwrap_or_else(|| "never".to_string()),
        faults: FaultLog::snapshot(&world.log),
        venue: venue_accounting,
        checks,
        notes,
        wal_records: records.len(),
        wal_sha256,
    };
    if !keep {
        let _ = std::fs::remove_dir_all(&world.paths.dir);
    }
    Ok(report)
}

#[derive(Clone, Debug)]
pub struct SweepOptions {
    /// The first seed and the shape every seed shares; `dir` is the parent.
    pub base: SimOptions,
    pub seeds: u64,
    /// Run every seed twice and compare the logs byte for byte.
    pub twice: bool,
}

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct Replay {
    pub seed: u64,
    pub identical: bool,
    pub first_wal_sha256: String,
    pub second_wal_sha256: String,
}

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct SweepReport {
    pub runs: Vec<SimReport>,
    pub replays: Vec<Replay>,
}

impl SweepReport {
    pub fn passed(&self) -> bool {
        self.runs.iter().all(SimReport::passed) && self.replays.iter().all(|r| r.identical)
    }

    pub fn table(&self) -> String {
        let mut out = String::new();
        for run in &self.runs {
            out.push_str(&run.line());
            out.push('\n');
            for check in run.failures() {
                let _ = writeln!(out, "    {}: {}", check.name, check.detail);
            }
            for reason in &run.restart_reasons {
                let _ = writeln!(out, "    restarted: {reason}");
            }
            for note in &run.notes {
                let _ = writeln!(out, "    note: {note}");
            }
            if !run.faults.is_empty() {
                let faults: Vec<String> =
                    run.faults.iter().map(|(k, v)| format!("{k}={v}")).collect();
                let _ = writeln!(out, "    injected: {}", faults.join(" "));
            }
        }
        for replay in &self.replays {
            if replay.identical {
                let _ = writeln!(out, "seed {:>6}  replay identical", replay.seed);
            } else {
                let _ = writeln!(
                    out,
                    "seed {:>6}  REPLAY DIFFERS  {} vs {}",
                    replay.seed,
                    &replay.first_wal_sha256[..12],
                    &replay.second_wal_sha256[..12]
                );
            }
        }
        let failed = self.runs.iter().filter(|r| !r.passed()).count()
            + self.replays.iter().filter(|r| !r.identical).count();
        let _ = writeln!(out, "{} seeds, {} failed", self.runs.len(), failed);
        out
    }
}

/// Seeds `base.seed..base.seed + seeds`, one after another, each in its own
/// directory under `base.dir`.
pub async fn run_sweep(opts: SweepOptions) -> Result<SweepReport, EngineError> {
    let mut runs = Vec::new();
    let mut replays = Vec::new();
    for offset in 0..opts.seeds {
        let seed = opts.base.seed + offset;
        let mut one = opts.base.clone();
        one.seed = seed;
        one.dir = opts.base.dir.join(format!("seed-{seed}"));
        let first = run_seed(one.clone()).await?;
        if opts.twice {
            one.dir = opts.base.dir.join(format!("seed-{seed}-again"));
            let second = run_seed(one).await?;
            replays.push(Replay {
                seed,
                identical: first.wal_sha256 == second.wal_sha256 && first.venue == second.venue,
                first_wal_sha256: first.wal_sha256.clone(),
                second_wal_sha256: second.wal_sha256,
            });
        }
        runs.push(first);
    }
    Ok(SweepReport { runs, replays })
}
