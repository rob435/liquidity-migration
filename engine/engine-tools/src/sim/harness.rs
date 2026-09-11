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
    shared_parked_signals, shared_rng, FaultLog, FaultRates, FaultyGateway, FaultyMarketFeed,
    FaultyOrderFeed, FaultySignalFeed, SharedFaultLog, SharedParkedSignals, SharedRng,
};
use super::invariants::{self, Check, Evidence};
use super::market::{self, MarketPlan, Realm, Shock, SimStrategies, TapeSummary};
use super::rng::Rng;
use super::signals::{self, Producer};
use crate::assembly;
use crate::backtest::feed::{pump, Cursor, SharedCursor, TapeFeed};
use crate::backtest::instruments::read_instruments;
use crate::backtest::runner::{read_engine_ledger, BacktestOptions};
use crate::backtest::scheduler::{Scheduler, VirtualTimer, WaiterKind, YieldNow};
use crate::backtest::signals::SignalReplayFeed;
use crate::backtest::tape::TapeReader;
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

/// The shock starts after this many seconds: the first hour boundary carries
/// the day the tape opened in, whose entry deadline has long passed, so a
/// position is already held by then.
const SHOCK_START_S: u64 = 5_400;

#[derive(Clone, Debug)]
pub struct SimOptions {
    pub seed: u64,
    /// Length of the synthetic tape.
    pub seconds: u64,
    /// How many of the catalogue's symbols trade (1 to 3).
    pub symbols: usize,
    /// Process deaths to inject.
    pub crashes: u32,
    pub faults: FaultRates,
    /// Where this seed's files go. Removed afterwards unless `keep`.
    pub dir: PathBuf,
    pub keep: bool,
    /// Whose strategy blocks run: the quoter, or a deployed realm's own.
    pub strategies: SimStrategies,
    /// Seconds between tape rows. Must stay inside the config's
    /// `max_quote_age_ms`, or every entry is refused on a stale quote.
    pub tape_step_s: u64,
    /// The venue's starting cash, which is the account's whole equity.
    pub capital: f64,
    /// One symbol falls 20 % and holds there, so a native stop triggers.
    pub shock: bool,
    /// Publish one LLM gate candidature as well as the feature batches.
    pub gate: bool,
    /// Chance per symbol and UTC day that the producer's features carry an
    /// entry trigger.
    pub pump_probability: f64,
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
            strategies: SimStrategies::Quoter,
            tape_step_s: 1,
            capital: BacktestOptions::default().initial_capital_usdt,
            shock: false,
            gate: false,
            pump_probability: 0.5,
        }
    }

    /// A deployed realm's own blocks, on the horizon and the tape density the
    /// producer's daily and hourly grids need, and at a capital where the
    /// LONG target clears every catalogue minimum and the profile's
    /// per-symbol cap.
    pub fn realm(seed: u64, dir: PathBuf, realm: Realm) -> Self {
        SimOptions {
            strategies: SimStrategies::Realm(realm),
            symbols: 3,
            seconds: 12 * 3_600,
            tape_step_s: 10,
            capital: 500.0,
            shock: true,
            ..SimOptions::new(seed, dir)
        }
    }

    pub fn hours(&mut self, hours: u64) {
        self.seconds = hours.saturating_mul(3_600);
    }
}

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct SimReport {
    pub callback_execution: &'static str,
    pub seed: u64,
    pub seconds: u64,
    pub symbols: usize,
    pub strategies: &'static str,
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
    pub signals_published: u64,
    pub signals_consumed: u64,
    pub signals_rejected: u64,
    /// Every sleeve reporting a health error when the loop stopped.
    pub strategy_errors: Vec<(String, String)>,
    /// Every `intent_refused` record in the log, by code.
    pub refusals_by_code: BTreeMap<String, u64>,
    pub orders_by_sleeve: BTreeMap<String, u64>,
    pub fills_by_sleeve: BTreeMap<String, u64>,
    /// The authenticated fee snapshot the run priced its fills with. It comes
    /// from `configs/bybit_fee_rates.json`, outside `engine/`, and moves
    /// `wal_sha256` without changing a single record: a pinned log hash is
    /// only pinned against this snapshot.
    pub fee_snapshot_sha256: Option<String>,
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
    profile: PathBuf,
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
            profile: dir.join("operational-profile.json"),
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
    config_identity: String,
    settings: EngineSection,
    sleeves: Vec<String>,
    scheduler: Scheduler,
    venue: Arc<Mutex<SimulatedVenue>>,
    cursor: SharedCursor,
    venue_rng: SharedRng,
    private_rng: SharedRng,
    market_rng: SharedRng,
    signal_rng: SharedRng,
    log: SharedFaultLog,
    deaths: Vec<u64>,
    rtt: Duration,
    private_latency: Duration,
    fee_snapshot_sha256: Option<String>,
    /// The spool: durable rows, read across every boot of this seed.
    signal_feed: SignalReplayFeed,
    /// Rows the fault wrapper took and owes the engine; they survive a death
    /// exactly as the spool's bytes do.
    parked_signals: SharedParkedSignals,
    published: Vec<engine_types::SignalObservation>,
    producer: Producer,
    tape_end_ms: i64,
}

/// What the engine had to say when its loop stopped.
struct StoppedEngine {
    outcome: RunOutcome,
    in_flight: Vec<String>,
    account: AccountView,
    strategy_health: Vec<(String, String)>,
}

/// How one boot of the engine ended.
enum SegmentEnd {
    Stopped(Box<StoppedEngine>),
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
        let mut plan = MarketPlan::new(opts.symbols, opts.seconds);
        plan.step_s = opts.tape_step_s.max(1);
        // An independent stream: drawing the shock from the seed's own chain
        // would move every fork after it and no quoter log would replay.
        plan.shock = opts.shock.then(|| Shock {
            symbol_index: Rng::new(opts.seed)
                .fork(0x5348_4f43)
                .below(plan.symbols.len() as u64) as usize,
            start_s: SHOCK_START_S,
            fall_fraction: 0.20,
            fall_over_s: 60,
            hold_s: 3_600,
        });
        let mut seed = Rng::new(opts.seed);
        let market_walk = seed.fork(0x4d41_524b);
        let io = |e: std::io::Error| boot(format!("writing the world: {e}"));
        let tape: TapeSummary = market::write_tape(&paths.tape, &plan, market_walk).map_err(io)?;
        market::write_instruments(&paths.instruments, &plan).map_err(io)?;
        std::fs::write(&paths.profile, market::OPERATIONAL_PROFILE).map_err(io)?;
        market::write_engine_config(&paths.config, &plan, opts.strategies, &paths.profile)
            .map_err(io)?;

        let loaded = config::load(&paths.config).map_err(|e| boot(format!("config: {e}")))?;
        // The Boot record's config identity. The written config names this
        // seed's scratch directory in `operational_profile_path`, and two runs
        // of one seed live in two directories, so the identity is taken with
        // that path abstracted: `--twice` compares what the seed decided.
        let config_identity = {
            let text = std::fs::read_to_string(&paths.config).map_err(io)?;
            let dir = paths.dir.display().to_string();
            hex::encode(Sha256::digest(text.replace(&dir, "<dir>").as_bytes()))
        };
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
        // A native sleeve declares no subscriptions of its own: it learns its
        // symbols from the durable observations that name them. The venue has
        // to list its whole catalogue, as a real one does, or runtime symbol
        // admission finds no instrument rule and refuses every name.
        let symbols: Vec<Symbol> = match opts.strategies {
            SimStrategies::Quoter => assembly::symbol_order(&[], &wanted)
                .map_err(|error| boot(format!("symbol order: {error}")))?,
            SimStrategies::Realm(_) => plan.names(),
        };

        let defaults = BacktestOptions::default();
        let fees =
            crate::backtest::fees::resolve(None, None, crate::backtest::fees::default_source())
                .map_err(boot)?;
        let rtt = Duration::from_millis(defaults.order_rtt_ms);
        let private_latency = Duration::from_millis(defaults.private_latency_ms);
        let scheduler = Scheduler::default();
        let venue = Arc::new(Mutex::new(SimulatedVenue::new(
            VenueParams {
                initial_cash_usdt: opts.capital,
                taker_fee_rate: fees.taker,
                maker_fee_rate: fees.maker,
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
        // In realm mode the cursor interns every catalogue name so runtime
        // admission can resolve one, and follows none until the engine asks:
        // an event for a symbol the engine has not admitted indexes past its
        // market table.
        let subscriptions = match opts.strategies {
            SimStrategies::Quoter => assembly::boot_subscriptions(&symbols, &wanted),
            SimStrategies::Realm(_) => Vec::new(),
        };
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

        // Every fork below is drawn after the three the quoter already used,
        // so adding the producer left every quoter log where it was.
        let venue_rng = shared_rng(seed.fork(0x5645_4e55));
        let private_rng = shared_rng(seed.fork(0x5052_4956));
        let market_rng = shared_rng(seed.fork(0x4645_4544));
        let signal_rng = shared_rng(seed.fork(0x5349_474e));
        let mut producer = Producer::bind(&loaded.config.strategies)
            .map_err(|e| boot(format!("producer: {e}")))?;
        producer.pump_probability = opts.pump_probability;
        producer.gate = opts.gate;
        let published = signals::publish(seed.fork(0x5052_4f44), &plan, &tape, &producer);

        Ok((
            World {
                venue_rng,
                private_rng,
                market_rng,
                signal_rng,
                log: FaultLog::shared(),
                signal_feed: {
                    let feed =
                        SignalReplayFeed::from_observations(published.clone(), scheduler.clone());
                    match signals::lifecycle(&producer) {
                        Some(lifecycle) => feed.with_lifecycle(lifecycle),
                        None => feed,
                    }
                },
                parked_signals: shared_parked_signals(),
                published,
                producer,
                tape_end_ms: plan.end_ms(),
                opts,
                paths,
                loaded,
                config_identity,
                settings,
                sleeves,
                scheduler,
                venue,
                cursor,
                deaths,
                rtt,
                private_latency,
                fee_snapshot_sha256: fees.snapshot_sha256,
            },
            clock,
        ))
    }

    /// One boot of the engine, to a clean stop or to the seeded death.
    async fn run_segment(
        &mut self,
        death_at: Option<u64>,
        reconnecting: bool,
    ) -> Result<SegmentEnd, EngineError> {
        let _claim = engine_wal::lock(&self.paths.wal).map_err(|e| boot(e.to_string()))?;
        // The working set `engine run` boots from, so a death and its restart
        // are judged on what production replays.
        let (wal, replayed) =
            WalWriter::open_unsynced_with(&self.paths.wal, assembly::boot_filter())?;
        let replayed = assembly::BootReplay::from_pairs(replayed);
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
        self.signal_feed.rebooted();
        let mut signal_feed = FaultySignalFeed::new(
            &mut self.signal_feed,
            self.opts.faults,
            self.signal_rng.clone(),
            self.scheduler.clone(),
            self.log.clone(),
            self.parked_signals.clone(),
            Duration::from_secs(60),
        );
        let mut controls = NoControls;

        // The pump runs before boot: after a death the clock is already
        // pumping, and boot's venue reads wait on it like every other reply.
        let pump_task = tokio::spawn(pump(self.cursor.clone(), self.scheduler.clone()));
        let mut engine = match Engine::boot_replay_exact(
            &self.settings,
            &self.config_identity,
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
            Some(Ok(outcome)) => Ok(SegmentEnd::Stopped(Box::new(StoppedEngine {
                outcome,
                in_flight: engine
                    .in_flight_ids()
                    .iter()
                    .map(|s| (*s).to_string())
                    .collect(),
                account: engine.account().clone(),
                strategy_health: engine.strategy_health(),
            }))),
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
    let (mut world, _clock) = World::build(opts)?;

    let mut deaths_done = 0usize;
    let mut restarts = 0u32;
    let mut restart_reasons: Vec<String> = Vec::new();
    let mut segments = 0u32;
    let mut stopped: Option<Box<StoppedEngine>> = None;
    let mut engine_error: Option<String> = None;
    loop {
        segments += 1;
        let death_at = world.deaths.get(deaths_done).copied();
        match world.run_segment(death_at, segments > 1).await {
            Ok(SegmentEnd::Stopped(end)) => {
                stopped = Some(end);
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
        .map(|end| format!("{:?}", end.outcome.stopped_by));
    // Rebuilt from the same config the run booted with, so `validate_checkpoint`
    // is the reducer's own answer about its own durable state.
    let judged = assembly::strategies(&world.loaded.config.strategies)
        .map_err(|e| state(e.to_string()))?
        .into_iter()
        .enumerate()
        .map(|(index, strategy)| {
            (
                engine_types::StrategyId(u16::try_from(index).unwrap_or(u16::MAX)),
                world.sleeves.get(index).cloned().unwrap_or_default(),
                strategy,
            )
        })
        .collect::<Vec<_>>();
    let counts = invariants::signal_counts(&records);
    let by_sleeve = invariants::by_sleeve(&records, &world.sleeves);
    let checks = invariants::all(&Evidence {
        records: &records,
        venue_view: &venue_view,
        venue_orders: &venue_orders,
        venue_executions: &venue_executions,
        venue_accounting: &venue_accounting,
        engine_in_flight: stopped.as_ref().map(|end| end.in_flight.as_slice()),
        engine_account: stopped.as_ref().map(|end| &end.account),
        stopped_by: stopped_by.as_deref(),
        engine_error: engine_error.as_deref(),
        ledger_net_usdt,
        published: &world.published,
        strategy_health: stopped.as_ref().map(|end| end.strategy_health.as_slice()),
        judged: &judged,
        long: world.producer.long.as_ref().map(|b| b.id),
        carry: world.producer.carry.as_ref().map(|b| b.id),
        tape_end_ms: world.tape_end_ms,
    });
    let report = SimReport {
        callback_execution: "embedded",
        seed: world.opts.seed,
        seconds: world.opts.seconds,
        symbols: world.opts.symbols,
        strategies: world.opts.strategies.as_str(),
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
        signals_published: world.published.len() as u64,
        signals_consumed: counts.consumed,
        signals_rejected: counts.rejected,
        strategy_errors: stopped
            .as_ref()
            .map(|end| end.strategy_health.clone())
            .unwrap_or_default(),
        refusals_by_code: invariants::refusals_by_code(&records),
        orders_by_sleeve: by_sleeve.orders,
        fills_by_sleeve: by_sleeve.fills,
        fee_snapshot_sha256: world.fee_snapshot_sha256.clone(),
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
            if !run.refusals_by_code.is_empty() {
                let refused: Vec<String> = run
                    .refusals_by_code
                    .iter()
                    .map(|(code, n)| format!("{code}={n}"))
                    .collect();
                let _ = writeln!(out, "    refused: {}", refused.join(" "));
            }
            if run.signals_published > 0 {
                let sleeves: Vec<String> = run
                    .orders_by_sleeve
                    .iter()
                    .map(|(name, orders)| {
                        format!(
                            "{name} orders={orders} fills={}",
                            run.fills_by_sleeve.get(name).copied().unwrap_or(0)
                        )
                    })
                    .collect();
                let _ = writeln!(
                    out,
                    "    {}: signals published={} consumed={} rejected={}  {}",
                    run.strategies,
                    run.signals_published,
                    run.signals_consumed,
                    run.signals_rejected,
                    sleeves.join("  ")
                );
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
