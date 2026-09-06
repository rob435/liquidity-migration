//! The loop, tested against mocks that write down what happened and when.
//!
//! The mocks all share one tape, so a test can assert not just that the log
//! was written and the order sent, but that they happened in that order —
//! which is the whole promise of the durability barrier.
//!
//! Tokio's clock starts paused (`start_paused = true`): a stop future of
//! `sleep(40 ms)` resolves as soon as the engine has nothing left to do, and
//! one input gives one interleaving. A test stays on the wall clock only when
//! it drives a real socket, or when it waits on an engine timer or deadline,
//! because those read `clock::now_ns`, which paused tokio time does not move.

use std::collections::VecDeque;
use std::sync::{Arc as Rc, Mutex as RefCell};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_types::risk::{ClosedTradeRow, RollingLossView};
use engine_types::{
    AccountIdentity, AccountView, AmendSpec, DenyReason, EngineEvent, Feed, FeedError,
    InstrumentRule, Intent, MarketEvent, MarketFeed, OrderAck, OrderFeed, OrderKind, OrderRequest,
    OrderUpdate, Quote, RiskKernel, RiskVerdict, Side, StopSpec, Strategy, StrategyCheckpoint,
    StrategyCheckpointIdentity, StrategyCtx, StrategyId, Subscription, Symbol, SymbolId,
    TimeInForce, TimerId, VenueCaps, VenueError, VenueExecution, VenueGateway, VenueOrder, Wal,
    WalError, WalRecord, WorkPolicy,
};

use crate::bench::{self, BenchOptions};
use crate::clock;
use crate::config::EngineSection;
use crate::engine::{durable_risk_verdict, Engine, EngineError, StopReason, ENGINE_VERSION};
use crate::heartbeat::Heartbeat;
use crate::testpath::temp_path;

// ------------------------------------------------------------------ the tape

#[derive(Clone, Debug, PartialEq)]
enum Step {
    Append(String),
    Barrier,
    Flush,
    Send(String),
    Cancel(String),
    Amend(String),
    ReadAccount,
    ReadRules,
    PrivateUpdate,
}

type Tape = Rc<RefCell<Vec<Step>>>;

fn tape() -> Tape {
    Rc::new(RefCell::new(Vec::new()))
}

/// A stable, recent wall timestamp for records that tests replay through boot.
///
/// These fixtures model a prior run, not a log older than the venue's bounded
/// execution-history reach. Tests for an actually stale log choose their own
/// timestamp explicitly.
fn recent_replay_ms() -> i64 {
    static FIXTURE_MS: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
    *FIXTURE_MS.get_or_init(|| clock::wall_ms() - 1_000)
}

fn kind_of(record: &WalRecord) -> String {
    match record {
        WalRecord::PortfolioExitChanged { .. } => "portfolio_exit_changed",
        WalRecord::PortfolioExitCompleted { .. } => "portfolio_exit_completed",
        WalRecord::PortfolioEmergencyChanged { .. } => "portfolio_emergency_changed",
        WalRecord::PortfolioEmergencyCompleted { .. } => "portfolio_emergency_completed",
        WalRecord::PortfolioOffsetSettled { .. } => "portfolio_offset_settled",
        WalRecord::SleeveStopSet { .. } => "sleeve_stop_set",
        WalRecord::SignalAdmissionChanged { .. } => "signal_admission_changed",
        WalRecord::StrategyCallbackSource { .. } => "strategy_callback_source",
        WalRecord::StrategyCallbackQueued { .. } => "strategy_callback_queued",
        WalRecord::OrderDispatchQueued { .. } => "order_dispatch_queued",
        WalRecord::OrderDispatchAttempted { .. } => "order_dispatch_attempted",
        WalRecord::OrderDispatchCompleted { .. } => "order_dispatch_completed",
        WalRecord::StrategyCallbackPrepared { .. } => "strategy_callback_prepared",
        WalRecord::StrategyProcessTransitionQueued { .. } => "strategy_process_transition_queued",
        WalRecord::InstrumentCatalogCheckpoint { .. } => "instrument_catalog_checkpoint",
        WalRecord::IdentityState { .. } => "identity_state",
        WalRecord::SignalProducerLifecycle { .. } => "signal_producer_lifecycle",
        WalRecord::Boot { .. } => "boot",
        WalRecord::Intent { .. } => "intent",
        WalRecord::Verdict { .. } => "verdict",
        WalRecord::OrderSent { .. } => "order_sent",
        WalRecord::OrderUpdate { .. } => "order_update",
        WalRecord::Markout { .. } => "markout",
        WalRecord::QuoteFill { .. } => "quote_fill",
        WalRecord::Names { .. } => "names",
        WalRecord::StopSet { .. } => "stop_set",
        WalRecord::CancelSent { .. } => "cancel_sent",
        WalRecord::AmendSent { .. } => "amend_sent",
        WalRecord::AmendResolved { .. } => "amend_resolved",
        WalRecord::LatencyLedger { .. } => "latency_ledger",
        WalRecord::VenueTiming { .. } => "venue_timing",
        WalRecord::FastExecution { .. } => "fast_execution",
        WalRecord::Note { .. } => "note",
        WalRecord::ControlAnchor { .. } => "control_anchor",
        WalRecord::Reconciled { .. } => "reconciled",
        WalRecord::ExecutionPrecisionV1 => "execution_precision_v1",
        WalRecord::OrderIdEpoch { .. } => "order_id_epoch",
        WalRecord::OrderLineageRestored { .. } => "order_lineage_restored",
        WalRecord::SegmentBase { .. } => "segment_base",
        WalRecord::RecoveredFill { .. } => "recovered_fill",
        WalRecord::ExecutionHistoryCheckpoint { .. } => "execution_history_checkpoint",
        WalRecord::LatchCleared { .. } => "latch_cleared",
        WalRecord::ClaimsDropped { .. } => "claims_dropped",
        WalRecord::TargetBookLatch { .. } => "target_book_latch",
        WalRecord::StrategyTransitionQueued { .. } => "strategy_transition_queued",
        WalRecord::StrategyEffectCompleted { .. } => "strategy_effect_completed",
        WalRecord::StrategyCheckpoint { .. } => "strategy_checkpoint",
        WalRecord::StrategyGlobalCheckpoint { .. } => "strategy_global_checkpoint",
        WalRecord::StrategyEventPublished { .. } => "strategy_event_published",
        WalRecord::StrategyEventConsumed { .. } => "strategy_event_consumed",
        WalRecord::SignalObservation { .. } => "signal_observation",
        WalRecord::SignalObservationConsumed { .. } => "signal_observation_consumed",
        WalRecord::SignalObservationRejected { .. } => "signal_observation_rejected",
        WalRecord::SignalGapRecorded { .. } => "signal_gap_recorded",
        WalRecord::LegacyQuantityGridAdopted { .. } => "legacy_quantity_grid_adopted_v2",
        WalRecord::LegacySignalSourceRetired { .. } => "legacy_signal_source_retired",
        WalRecord::RuntimeControlAccepted { .. } => "runtime_control_accepted",
        WalRecord::RuntimeControlConsumed { .. } => "runtime_control_consumed",
    }
    .to_string()
}

/// True when the only fsync that trading paid for is the final one after the
/// last append — the shutdown barrier that makes the log's tail durable on the
/// way out.
///
/// Boot's own barrier does not count. It makes the reconciliation record
/// durable before a single order can be judged, so that a crash cannot lose a
/// latch that has just been set; it happens once, before any of this runs, and
/// it is not on any order's path.
fn only_the_shutdown_barrier(tape: &Tape) -> bool {
    let start = after_boot(tape);
    let tape = tape.lock().unwrap();
    let barriers: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter(|(i, s)| *i >= start && matches!(s, Step::Barrier))
        .map(|(i, _)| i)
        .collect();
    let last_append = tape.iter().rposition(|s| matches!(s, Step::Append(_)));
    barriers.len() == 1 && last_append.is_some_and(|a| barriers[0] > a)
}

/// The first tape index that belongs to trading rather than to coming up.
///
/// Boot writes the reconciliation record and fsyncs it, so that a crash
/// cannot lose a latch it has just set. That fsync is not on any order's
/// path, and a test measuring what an order costs has to start after it.
fn after_boot(tape: &Tape) -> usize {
    let tape = tape.lock().unwrap();
    let reconciled = tape
        .iter()
        .position(|s| matches!(s, Step::Append(kind) if kind == "reconciled"));
    match reconciled {
        Some(at) => tape
            .iter()
            .skip(at)
            .position(|s| matches!(s, Step::Barrier))
            .map(|i| at + i + 1)
            .unwrap_or(at + 1),
        None => 0,
    }
}

/// Where a step first appears on the tape.
fn at(tape: &Tape, step: &Step) -> Option<usize> {
    tape.lock().unwrap().iter().position(|s| s == step)
}

/// Where a step first appears after some earlier point. Boot writes and
/// fsyncs its own records, so a test about an order's barrier has to look
/// past them.
fn after(tape: &Tape, step: &Step, from: usize) -> Option<usize> {
    tape.lock()
        .unwrap()
        .iter()
        .skip(from)
        .position(|s| s == step)
        .map(|i| i + from)
}

/// The first note whose text contains `needle`. Boot writes a note of its
/// own, so tests look for the one they mean.
fn note_saying(records: &Rc<RefCell<Vec<WalRecord>>>, needle: &str) -> String {
    records
        .lock()
        .unwrap()
        .iter()
        .find_map(|r| match r {
            WalRecord::Note { text, .. } if text.contains(needle) => Some(text.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no note containing {needle:?}"))
}

fn appends(tape: &Tape) -> Vec<String> {
    tape.lock()
        .unwrap()
        .iter()
        .filter_map(|s| match s {
            Step::Append(kind) => Some(kind.clone()),
            _ => None,
        })
        .collect()
}

// ------------------------------------------------------------------- mocks

pub(crate) struct MockWal {
    tape: Tape,
    records: Rc<RefCell<Vec<WalRecord>>>,
    seq: u64,
    fail_on: Option<String>,
    pub(crate) fail_barrier_after: Option<&'static str>,
    /// A tape the barrier's own thread can also write to. The ordinary tape
    /// is an `Rc` and cannot leave this thread, and the whole point of a
    /// barrier that runs beside the send is that something else finishes it.
    /// Set by `defer_barriers`; `None` keeps barriers synchronous.
    crossing_tape: Option<Arc<Mutex<Vec<&'static str>>>>,
    /// How long the deferred barrier takes. Long enough that a caller which
    /// does not wait for it visibly does not.
    barrier_takes: Duration,
    sync_barrier_takes: Duration,
}

impl MockWal {
    pub(crate) fn fail_append(&mut self, kind: &str) {
        self.fail_on = Some(kind.into());
    }

    pub(crate) fn snapshot_records(&self) -> Vec<WalRecord> {
        self.records.lock().unwrap().clone()
    }

    fn new(tape: Tape) -> (Self, Rc<RefCell<Vec<WalRecord>>>) {
        let records = Rc::new(RefCell::new(Vec::new()));
        (
            MockWal {
                tape,
                records: records.clone(),
                seq: 0,
                fail_on: None,
                fail_barrier_after: None,
                crossing_tape: None,
                barrier_takes: Duration::from_millis(30),
                sync_barrier_takes: Duration::ZERO,
            },
            records,
        )
    }
}

impl MockVenue {
    /// Record this venue's sends onto the same tape the log's barrier writes
    /// to, so their order is one readable sequence.
    fn watch_with(&mut self, crossing: Arc<Mutex<Vec<&'static str>>>) {
        self.crossing_tape = Some(crossing);
    }
}

impl MockWal {
    /// Run barriers on their own thread, the way a real log does, and record
    /// on the shared tape both when a barrier finishes and when order news is
    /// written down. The order of those two is the whole question.
    fn defer_barriers(&mut self) -> Arc<Mutex<Vec<&'static str>>> {
        let crossing = Arc::new(Mutex::new(Vec::new()));
        self.crossing_tape = Some(crossing.clone());
        crossing
    }
}

struct MockCallbackReader(Arc<Mutex<Vec<WalRecord>>>);

impl engine_types::strategy_process::CallbackWalReader for MockCallbackReader {
    fn start(&self) -> engine_types::strategy_process::CallbackWalCursor {
        engine_types::strategy_process::CallbackWalCursor {
            segment: 1,
            sequence: 1,
            offset: 0,
        }
    }
    fn read_callback(
        &mut self,
        cursor: engine_types::strategy_process::CallbackWalCursor,
        callback_id: u64,
    ) -> Result<engine_types::strategy_process::StrategyCallbackInput, WalError> {
        let records = self.0.lock().unwrap();
        let input = match records.get(cursor.sequence as usize - 1) {
            Some(
                WalRecord::StrategyCallbackQueued { input }
                | WalRecord::StrategyCallbackPrepared { input },
            ) if input.callback_id == callback_id => Some(input),
            Some(WalRecord::SegmentBase {
                strategy_callbacks, ..
            }) => strategy_callbacks
                .iter()
                .find(|input| input.callback_id == callback_id),
            _ => None,
        };
        input.cloned().ok_or_else(|| WalError::Corrupt {
            offset: cursor.offset,
            detail: "mock callback slot has no durable input".into(),
        })
    }
    fn next(
        &mut self,
        cursor: engine_types::strategy_process::CallbackWalCursor,
    ) -> Result<Option<engine_types::strategy_process::CallbackWalRecord>, WalError> {
        let records = self.0.lock().unwrap();
        let Some(record) = records.get(cursor.sequence as usize - 1) else {
            return Ok(None);
        };
        let source = match record {
            WalRecord::OrderUpdate {
                callbacks: Some(owners),
                update,
            } => Some((
                owners.clone(),
                engine_types::strategy_process::CallbackEvent::Order {
                    update: update.clone(),
                },
            )),
            record @ WalRecord::RecoveredFill {
                callbacks: Some(_), ..
            } => record.recovered_callback().map(|(owners, update)| {
                (
                    owners,
                    engine_types::strategy_process::CallbackEvent::Order { update },
                )
            }),
            WalRecord::StrategyCallbackSource {
                strategy, event, ..
            } => Some((vec![*strategy], event.clone())),
            _ => None,
        };
        Ok(Some(engine_types::strategy_process::CallbackWalRecord {
            cursor,
            next: engine_types::strategy_process::CallbackWalCursor {
                segment: cursor.segment,
                sequence: cursor.sequence + 1,
                offset: cursor.offset + 1,
            },
            source,
        }))
    }
}

impl Wal for MockWal {
    fn callback_reader(
        &mut self,
    ) -> Result<Option<Box<dyn engine_types::strategy_process::CallbackWalReader>>, WalError> {
        Ok(Some(Box::new(MockCallbackReader(self.records.clone()))))
    }

    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
        let kind = kind_of(record);
        if self.fail_on.as_deref() == Some(kind.as_str()) {
            return Err(WalError::Io(std::io::Error::other("test failure")));
        }
        if kind == "order_update" {
            if let Some(crossing) = &self.crossing_tape {
                crossing.lock().unwrap().push("order news written down");
            }
        }
        self.seq += 1;
        self.tape.lock().unwrap().push(Step::Append(kind));
        self.records.lock().unwrap().push(record.clone());
        Ok(self.seq)
    }

    fn barrier(&mut self) -> Result<(), WalError> {
        std::thread::sleep(self.sync_barrier_takes);
        self.tape.lock().unwrap().push(Step::Barrier);
        if self.fail_barrier_after.is_some_and(|kind| {
            self.records
                .lock()
                .unwrap()
                .last()
                .is_some_and(|record| kind_of(record) == kind)
        }) {
            return Err(WalError::Io(std::io::Error::other("test barrier failure")));
        }
        Ok(())
    }

    fn barrier_begin(&mut self) -> Result<engine_types::wal::PendingBarrier, WalError> {
        let Some(crossing) = self.crossing_tape.clone() else {
            self.barrier()?;
            return Ok(engine_types::wal::PendingBarrier::settled());
        };
        self.tape.lock().unwrap().push(Step::Barrier);
        let (answer, done) = std::sync::mpsc::channel();
        let takes = self.barrier_takes;
        std::thread::spawn(move || {
            std::thread::sleep(takes);
            crossing.lock().unwrap().push("disk confirmed");
            let _ = answer.send(Ok(()));
        });
        Ok(engine_types::wal::PendingBarrier::running(done))
    }

    fn flush(&mut self) -> Result<(), WalError> {
        self.tape.lock().unwrap().push(Step::Flush);
        Ok(())
    }
}

/// What a venue can do, unless a test says otherwise: the shipping gateway's
/// own answers, so a test that changes one is visibly about that capability.
fn bybit_like_caps() -> VenueCaps {
    VenueCaps {
        native_position_stop: true,
        amend_in_place: true,
        set_leverage: true,
        close_position_below_minimum: true,
    }
}

#[derive(Default)]
struct MockRecoveryControl {
    account_delay_ms: std::sync::atomic::AtomicU64,
    history_delay_ms: std::sync::atomic::AtomicU64,
    account_started: tokio::sync::Notify,
    history_started: tokio::sync::Notify,
}

#[derive(Clone)]
struct MockRecoveryClient {
    tape: Tape,
    account_readings: Rc<RefCell<VecDeque<Vec<engine_types::PositionView>>>>,
    account_view_fails: Rc<RefCell<bool>>,
    executions: Rc<RefCell<Option<Vec<VenueExecution>>>>,
    control: Arc<MockRecoveryControl>,
}

#[engine_types::async_trait]
impl engine_types::orders::AccountRecoveryClient for MockRecoveryClient {
    async fn account_view(&self, _: &[Symbol]) -> Result<AccountView, VenueError> {
        self.tape.lock().unwrap().push(Step::ReadAccount);
        let observed_ns = clock::now_ns();
        let failed = *self.account_view_fails.lock().unwrap();
        let positions = if failed {
            Vec::new()
        } else {
            self.account_readings
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or_default()
        };
        let delay = self
            .control
            .account_delay_ms
            .load(std::sync::atomic::Ordering::Relaxed);
        if delay > 0 {
            self.control.account_started.notify_one();
            tokio::time::sleep(Duration::from_millis(delay)).await;
        }
        if failed {
            return Err(VenueError::Transport(
                "scripted account-view failure".into(),
            ));
        }
        Ok(AccountView {
            exact_amounts: None,
            equity_usdt: 10_000.0,
            available_usdt: 9_000.0,
            positions,
            observed_ns,
        })
    }
    async fn executions(
        &self,
        _: &[Symbol],
        _: i64,
        _: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        let rows = self.executions.lock().unwrap().clone();
        let delay = self
            .control
            .history_delay_ms
            .load(std::sync::atomic::Ordering::Relaxed);
        if delay > 0 {
            self.control.history_started.notify_one();
            tokio::time::sleep(Duration::from_millis(delay)).await;
        }
        engine_types::ExecutionHistory::from_rows(rows.ok_or_else(|| {
            VenueError::BadRequest("this venue cannot list its execution history".into())
        })?)
    }
}

pub(crate) struct MockVenue {
    spooled_history: Option<engine_types::ExecutionHistory>,
    recovery_reads: Arc<MockRecoveryControl>,
    tape: Tape,
    /// Shared with the log's deferred barrier, so one ordered list holds the
    /// send, the disk's answer, and the news that follows. Set by
    /// `watch_with`; `None` records nothing.
    crossing_tape: Option<Arc<Mutex<Vec<&'static str>>>>,
    rules: Vec<(Symbol, InstrumentRule)>,
    exact_specs: Option<Vec<(Symbol, engine_types::numeric::ExactInstrumentSpec)>>,
    catalog_client: Option<Arc<dyn engine_types::orders::InstrumentCatalogClient>>,
    sends: Rc<RefCell<Vec<OrderRequest>>>,
    cancels: Rc<RefCell<Vec<(SymbolId, String)>>>,
    amends: Rc<RefCell<Vec<(SymbolId, String, AmendSpec)>>>,
    stops: Rc<RefCell<Vec<(SymbolId, f64)>>>,
    stop_failures_remaining: Rc<RefCell<usize>>,
    stop_delay: Duration,
    exact_stops: Arc<Mutex<Vec<(SymbolId, engine_types::order_terms::ExactStopTerms)>>>,
    caps: VenueCaps,
    reply: Option<VenueError>,
    lookup_started: Option<Arc<tokio::sync::Notify>>,
    send_delay: Duration,
    /// What the venue would say it is working. Seeded by a test that wants
    /// boot to find an order the log does not know about.
    working: Vec<VenueOrder>,
    /// Positions for each `account_view` call to report, oldest first; an
    /// exhausted (or never seeded) script reads flat. Boot takes the first
    /// reading, so a test that wants a mid-run change seeds AFTER build and
    /// forces a refresh (a stream reset is the cheap way).
    account_readings: Rc<RefCell<VecDeque<Vec<engine_types::PositionView>>>>,
    /// Make subsequent account reads fail, for stream-gap fail-closed tests.
    account_view_fails: Rc<RefCell<bool>>,
    /// Every leverage the engine actually told the venue about, in order.
    leverages: Rc<RefCell<Vec<(SymbolId, f64)>>>,
    /// What the venue's execution history reports. `None` makes the read fail.
    executions: Rc<RefCell<Option<Vec<VenueExecution>>>>,
    /// What each cancel is answered with, in order; an exhausted script
    /// accepts every cancel.
    cancel_replies: Rc<RefCell<VecDeque<Result<(), VenueError>>>>,
    /// Whether the venue offers a status lookup at all, and what it answers,
    /// in order; an exhausted script answers `Unknown`.
    lookup_scripted: bool,
    lookups: Rc<RefCell<VecDeque<engine_types::orders::OrderLookup>>>,
}

impl MockVenue {
    fn recovery_client(&self) -> MockRecoveryClient {
        MockRecoveryClient {
            tape: self.tape.clone(),
            account_readings: self.account_readings.clone(),
            account_view_fails: self.account_view_fails.clone(),
            executions: self.executions.clone(),
            control: self.recovery_reads.clone(),
        }
    }
    fn new(tape: Tape, symbols: &[&str]) -> (Self, Rc<RefCell<Vec<OrderRequest>>>) {
        let sends = Rc::new(RefCell::new(Vec::new()));
        let rules = symbols
            .iter()
            .map(|s| {
                (
                    s.to_string(),
                    InstrumentRule {
                        tick_size: 0.5,
                        qty_step: 0.001,
                        min_qty: 0.001,
                        min_notional: 5.0,
                    },
                )
            })
            .collect();
        (
            MockVenue {
                spooled_history: None,
                recovery_reads: Arc::new(MockRecoveryControl::default()),
                tape,
                crossing_tape: None,
                rules,
                exact_specs: None,
                catalog_client: None,
                sends: sends.clone(),
                cancels: Rc::new(RefCell::new(Vec::new())),
                amends: Rc::new(RefCell::new(Vec::new())),
                stops: Rc::new(RefCell::new(Vec::new())),
                stop_failures_remaining: Rc::new(RefCell::new(0)),
                stop_delay: Duration::ZERO,
                exact_stops: Arc::new(Mutex::new(Vec::new())),
                caps: bybit_like_caps(),
                reply: None,
                lookup_started: None,
                send_delay: Duration::ZERO,
                working: Vec::new(),
                account_readings: Rc::new(RefCell::new(VecDeque::new())),
                account_view_fails: Rc::new(RefCell::new(false)),
                leverages: Rc::new(RefCell::new(Vec::new())),
                executions: Rc::new(RefCell::new(Some(Vec::new()))),
                cancel_replies: Rc::new(RefCell::new(VecDeque::new())),
                lookup_scripted: false,
                lookups: Rc::new(RefCell::new(VecDeque::new())),
            },
            sends,
        )
    }
}

#[engine_types::async_trait]
impl VenueGateway for MockVenue {
    fn account_recovery_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::AccountRecoveryClient>> {
        Some(Box::new(self.recovery_client()))
    }

    fn instrument_catalog_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::InstrumentCatalogClient>> {
        self.catalog_client.as_ref().map(|client| {
            Box::new(SharedCatalogClient(client.clone()))
                as Box<dyn engine_types::orders::InstrumentCatalogClient>
        })
    }
    fn restore_instrument_catalog(
        &self,
        checkpoint: &engine_types::orders::InstrumentCatalogCheckpoint,
    ) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        checkpoint.validate_bounds()?;
        if checkpoint.cache.kind != "mock-native-map" {
            return Err(VenueError::BadReply("wrong mock catalog owner".into()));
        }
        let names: Vec<String> = serde_json::from_slice(&checkpoint.cache.payload)
            .map_err(|error| VenueError::BadReply(error.to_string()))?;
        Ok(engine_types::orders::InstrumentCatalog {
            cache: Some(Arc::new(TestCatalogCache(names))),
            rules: checkpoint.rules.clone(),
            specs: checkpoint.specs.clone(),
        })
    }
    fn install_instrument_catalog(
        &mut self,
        catalog: &engine_types::orders::InstrumentCatalog,
    ) -> Result<(), VenueError> {
        let cache = catalog
            .cache
            .as_ref()
            .and_then(|cache| cache.as_any().downcast_ref::<TestCatalogCache>())
            .ok_or_else(|| VenueError::BadReply("wrong mock catalog owner".into()))?;
        if self
            .rules
            .iter()
            .enumerate()
            .any(|(index, (name, _))| cache.0.get(index) != Some(name))
        {
            return Err(VenueError::BadReply(
                "mock native symbol ids changed".into(),
            ));
        }
        self.rules = cache
            .0
            .iter()
            .map(|name| {
                catalog
                    .rules
                    .iter()
                    .find(|(symbol, _)| symbol == name)
                    .cloned()
                    .ok_or_else(|| VenueError::BadReply("mock catalog lacks a native rule".into()))
            })
            .collect::<Result<_, _>>()?;
        self.exact_specs = Some(catalog.specs.clone());
        Ok(())
    }

    fn order_lookup_client(&self) -> Option<Box<dyn engine_types::orders::OrderLookupClient>> {
        if let Some(started) = &self.lookup_started {
            return Some(Box::new(StalledLookup(started.clone())));
        }
        self.lookup_scripted.then(|| {
            Box::new(ScriptedLookup(self.lookups.clone()))
                as Box<dyn engine_types::orders::OrderLookupClient>
        })
    }

    async fn order_status(
        &mut self,
        _symbol: SymbolId,
        _client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        if let Some(started) = &self.lookup_started {
            started.notify_one();
            std::future::pending::<()>().await;
        }
        Ok(engine_types::orders::OrderLookup::Unavailable)
    }

    fn caps(&self) -> VenueCaps {
        self.caps
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        Ok(AccountIdentity {
            venue: "mock".to_string(),
            user_id: "7000001".to_string(),
            realm: "demo".to_string(),
        })
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        self.tape
            .lock()
            .unwrap()
            .push(Step::Send(req.client_order_id.clone()));
        if let Some(crossing) = &self.crossing_tape {
            crossing.lock().unwrap().push("order on the wire");
        }
        self.sends.lock().unwrap().push(req.clone());
        if !self.send_delay.is_zero() {
            tokio::time::sleep(self.send_delay).await;
        }
        if let Some(e) = &self.reply {
            return Err(match e {
                VenueError::Rejected { code, message } => VenueError::Rejected {
                    code: *code,
                    message: message.clone(),
                },
                other => VenueError::Transport(other.to_string()),
            });
        }
        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id: format!("v-{}", req.client_order_id),
            sent_ns: 0,
            ack_ns: clock::now_ns(),
        })
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        if let Some(history) = self.spooled_history.take() {
            return Ok(history);
        }
        engine_types::orders::AccountRecoveryClient::executions(
            &self.recovery_client(),
            &[],
            start_ms,
            end_ms,
        )
        .await
    }

    async fn cancel_order(&mut self, symbol: SymbolId, id: &str) -> Result<(), VenueError> {
        self.tape.lock().unwrap().push(Step::Cancel(id.to_string()));
        self.cancels.lock().unwrap().push((symbol, id.to_string()));
        self.cancel_replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or(Ok(()))
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.tape.lock().unwrap().push(Step::Amend(id.to_string()));
        self.amends
            .lock()
            .unwrap()
            .push((symbol, id.to_string(), spec));
        Ok(())
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        self.stops.lock().unwrap().push((symbol, trigger_px));
        tokio::time::sleep(self.stop_delay).await;
        let mut failures_remaining = self.stop_failures_remaining.lock().unwrap();
        if *failures_remaining > 0 {
            *failures_remaining -= 1;
            return Err(VenueError::Transport("scripted stop failure".into()));
        }
        Ok(())
    }

    async fn set_stop_exact(
        &mut self,
        symbol: SymbolId,
        terms: &engine_types::order_terms::ExactStopTerms,
    ) -> Result<(), VenueError> {
        self.exact_stops
            .lock()
            .unwrap()
            .push((symbol, terms.clone()));
        self.set_stop(symbol, terms.trigger_price.to_f64().unwrap())
            .await
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        if let Some(index) = self.rules.iter().position(|(known, _)| known == symbol) {
            return Some(SymbolId(index as u16));
        }
        let id = SymbolId(self.rules.len() as u16);
        self.rules.push((
            symbol.to_string(),
            InstrumentRule {
                tick_size: 0.5,
                qty_step: 0.001,
                min_qty: 0.001,
                min_notional: 5.0,
            },
        ));
        Some(id)
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        self.leverages.lock().unwrap().push((symbol, leverage));
        Ok(())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        engine_types::orders::AccountRecoveryClient::account_view(&self.recovery_client(), &[])
            .await
    }

    async fn instrument_specs(
        &mut self,
    ) -> Result<Vec<(Symbol, engine_types::numeric::ExactInstrumentSpec)>, VenueError> {
        self.exact_specs
            .clone()
            .ok_or_else(|| VenueError::BadReply("no scripted exact instrument catalog".into()))
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.tape.lock().unwrap().push(Step::ReadRules);
        Ok(self.rules.clone())
    }

    /// Whatever a test seeded. Empty by default, which is a venue working
    /// nothing — the ordinary case for a mock that has never been told
    /// otherwise.
    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        Ok(self.working.clone())
    }
}

/// One call on the rolling loss window's hooks, in the order it arrived.
#[derive(Clone, Debug, PartialEq)]
enum RollingLossCall {
    Restored(Vec<ClosedTradeRow>),
    Closed(ClosedTradeRow),
    Clock(i64),
}

/// The rolling loss window's side of the mock: the tape of hook calls, the
/// rows a rotation would be handed, and what the heartbeat is told.
#[derive(Clone, Default)]
struct MockRolling {
    calls: Rc<RefCell<Vec<RollingLossCall>>>,
    rows: Rc<RefCell<Vec<ClosedTradeRow>>>,
    view: Rc<RefCell<Option<RollingLossView>>>,
}

impl MockRolling {
    fn calls(&self) -> Vec<RollingLossCall> {
        self.calls.lock().unwrap().clone()
    }

    fn closes(&self) -> Vec<ClosedTradeRow> {
        self.calls()
            .into_iter()
            .filter_map(|call| match call {
                RollingLossCall::Closed(row) => Some(row),
                _ => None,
            })
            .collect()
    }
}

pub(crate) struct MockRisk {
    verdict: RiskVerdict,
    amend_verdict: Option<RiskVerdict>,
    seen: Rc<RefCell<Vec<OrderUpdate>>>,
    registered: Rc<RefCell<Vec<(String, f64)>>>,
    rolling: MockRolling,
}

impl MockRisk {
    /// `Allow { qty: NaN }` means "whatever was asked for".
    pub(crate) fn with(verdict: RiskVerdict) -> (Self, Rc<RefCell<Vec<OrderUpdate>>>) {
        let seen = Rc::new(RefCell::new(Vec::new()));
        (
            MockRisk {
                verdict,
                amend_verdict: None,
                seen: seen.clone(),
                registered: Rc::new(RefCell::new(Vec::new())),
                rolling: MockRolling::default(),
            },
            seen,
        )
    }
}

impl RiskKernel for MockRisk {
    fn assess_portfolio(
        &mut self,
        intent: &Intent,
        account: &AccountView,
        _portfolio: &engine_types::portfolio::PortfolioState,
    ) -> engine_types::risk::PortfolioRiskVerdict {
        match self.assess(intent, account) {
            RiskVerdict::Allow { qty } => match engine_types::order_terms::strategy_decimal(qty) {
                Ok(quantity) => engine_types::risk::PortfolioRiskVerdict::Allow {
                    qty: if qty == intent.qty {
                        intent.quantity().unwrap_or(quantity)
                    } else {
                        quantity
                    },
                    venue_reduce_only: intent.reduce_only,
                },
                Err(error) => engine_types::risk::PortfolioRiskVerdict::Deny {
                    reason: engine_types::DenyReason::UnknownState {
                        detail: error.to_string(),
                    },
                },
            },
            RiskVerdict::Deny { reason } => {
                engine_types::risk::PortfolioRiskVerdict::Deny { reason }
            }
        }
    }
    fn reassess_portfolio_order(
        &mut self,
        _id: &str,
        intent: &Intent,
        account: &AccountView,
        portfolio: &engine_types::portfolio::PortfolioState,
    ) -> engine_types::risk::PortfolioRiskVerdict {
        self.assess_portfolio(intent, account, portfolio)
    }
    fn physical_exposure_interval_excluding(
        &mut self,
        _id: &str,
        symbol: SymbolId,
        account: &AccountView,
    ) -> Result<engine_types::risk::PhysicalExposureInterval, DenyReason> {
        self.physical_exposure_interval(symbol, account)
    }
    fn physical_exposure_interval(
        &mut self,
        symbol: SymbolId,
        account: &AccountView,
    ) -> Result<engine_types::risk::PhysicalExposureInterval, DenyReason> {
        let net = account
            .positions
            .iter()
            .filter(|row| row.symbol == symbol)
            .map(|row| {
                if row.side == Side::Buy {
                    row.qty
                } else {
                    -row.qty
                }
            })
            .sum();
        engine_types::risk::PhysicalExposureInterval::try_new(net, net)
    }

    fn assess(&mut self, intent: &Intent, _account: &AccountView) -> RiskVerdict {
        match &self.verdict {
            RiskVerdict::Allow { qty } if qty.is_nan() => RiskVerdict::Allow { qty: intent.qty },
            other => other.clone(),
        }
    }

    fn on_update(&mut self, update: &OrderUpdate) {
        self.seen.lock().unwrap().push(update.clone());
    }

    fn assess_price_amend(
        &mut self,
        _client_order_id: &str,
        intent: &Intent,
        account: &AccountView,
    ) -> RiskVerdict {
        self.amend_verdict
            .clone()
            .unwrap_or_else(|| self.assess(intent, account))
    }

    fn register_order(&mut self, client_order_id: &str, _intent: &Intent, approved_qty: f64) {
        self.registered
            .lock()
            .unwrap()
            .push((client_order_id.to_string(), approved_qty));
    }

    fn observe_closed_trade(&mut self, row: ClosedTradeRow) {
        self.rolling
            .calls
            .lock()
            .unwrap()
            .push(RollingLossCall::Closed(row.clone()));
        self.rolling.rows.lock().unwrap().push(row);
    }

    fn observe_wall_clock_ms(&mut self, wall_ms: i64) {
        self.rolling
            .calls
            .lock()
            .unwrap()
            .push(RollingLossCall::Clock(wall_ms));
    }

    fn rolling_loss(&self) -> Option<RollingLossView> {
        *self.rolling.view.lock().unwrap()
    }

    fn rolling_loss_rows(&self) -> Vec<ClosedTradeRow> {
        self.rolling.rows.lock().unwrap().clone()
    }

    fn restore_rolling_loss_rows(&mut self, rows: &[ClosedTradeRow]) {
        self.rolling
            .calls
            .lock()
            .unwrap()
            .push(RollingLossCall::Restored(rows.to_vec()));
        *self.rolling.rows.lock().unwrap() = rows.to_vec();
    }
}

/// Plays a script, then either closes or waits forever.
struct ScriptFeed {
    events: VecDeque<MarketEvent>,
    close_at_end: bool,
    /// Symbols admitted after boot, in order, with the ids handed back.
    admitted: Rc<RefCell<Vec<(String, SymbolId)>>>,
    symbols: Vec<String>,
    /// Hand back the wrong id, to prove the engine notices.
    admits_wrongly: bool,
}

impl ScriptFeed {
    fn quotes(symbol: SymbolId, count: usize, close_at_end: bool) -> Self {
        let events = (0..count)
            .map(|i| MarketEvent::Quote {
                symbol,
                quote: Quote {
                    bid_px: 30_000.0 + i as f64,
                    bid_qty: 1.0,
                    ask_px: 30_000.5 + i as f64,
                    ask_qty: 1.0,
                    venue_ts_ms: 1,
                    recv_ns: clock::now_ns(),
                    seq: i as u64,
                },
            })
            .collect();
        ScriptFeed {
            events,
            close_at_end,
            admitted: Rc::new(RefCell::new(Vec::new())),
            symbols: vec!["BTCUSDT".into()],
            admits_wrongly: false,
        }
    }

    /// The same walk, wide enough for a resting entry to be worth placing:
    /// eight ticks, and more than a hundredth of a percent of the price.
    /// `quotes` above is one tick wide on purpose and falls back to a market
    /// order.
    fn wide_quotes(symbol: SymbolId, count: usize, close_at_end: bool) -> Self {
        let events = (0..count)
            .map(|i| MarketEvent::Quote {
                symbol,
                quote: Quote {
                    bid_px: 30_000.0 + i as f64,
                    bid_qty: 1.0,
                    ask_px: 30_004.0 + i as f64,
                    ask_qty: 1.0,
                    venue_ts_ms: 1,
                    recv_ns: clock::now_ns(),
                    seq: i as u64,
                },
            })
            .collect();
        ScriptFeed {
            events,
            close_at_end,
            admitted: Rc::new(RefCell::new(Vec::new())),
            symbols: vec!["BTCUSDT".into()],
            admits_wrongly: false,
        }
    }
}

impl MarketFeed for ScriptFeed {
    fn admit(&mut self, symbol: &str, _feed: engine_types::Feed) -> Option<SymbolId> {
        if let Some(index) = self.symbols.iter().position(|name| name == symbol) {
            return Some(SymbolId(u16::try_from(index).unwrap()));
        }
        let id = SymbolId(
            u16::try_from(self.symbols.len()).unwrap() + if self.admits_wrongly { 7 } else { 0 },
        );
        self.symbols.push(symbol.to_string());
        self.admitted.lock().unwrap().push((symbol.to_string(), id));
        Some(id)
    }

    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        match self.events.pop_front() {
            Some(event) => Ok(event),
            None if self.close_at_end => Err(FeedError::Closed),
            None => std::future::pending().await,
        }
    }
}

struct ScriptOrderFeed {
    updates: VecDeque<OrderUpdate>,
    learned: Rc<RefCell<Vec<(String, SymbolId)>>>,
}

impl ScriptOrderFeed {
    fn empty() -> Self {
        ScriptOrderFeed {
            updates: VecDeque::new(),
            learned: Rc::new(RefCell::new(Vec::new())),
        }
    }

    /// Delivers these updates in order, then waits forever. Feed them to a
    /// second `run` call when an update has to name an order id the first
    /// run minted — the id is not known before the send happens.
    fn playing(updates: Vec<OrderUpdate>) -> Self {
        ScriptOrderFeed {
            updates: updates.into(),
            learned: Rc::new(RefCell::new(Vec::new())),
        }
    }
}

impl OrderFeed for ScriptOrderFeed {
    fn learn(&mut self, symbol: &str, id: SymbolId) {
        self.learned.lock().unwrap().push((symbol.to_string(), id));
    }

    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        match self.updates.pop_front() {
            Some(update) => Ok(update),
            None => std::future::pending().await,
        }
    }
}

/// Emits a buy on every Nth quote it sees.
struct Buyer {
    symbol: String,
    every_nth: u64,
    qty: f64,
    seen: u64,
    heard: Rc<RefCell<Vec<String>>>,
    /// Asks the engine to rest and work the entry instead of crossing.
    work: Option<WorkPolicy>,
    /// What leverage its entries were sized at. None means no opinion.
    leverage: Option<f64>,
    /// Send exits instead of entries.
    reduce_only: bool,
}

impl Buyer {
    fn new(symbol: &str, every_nth: u64, qty: f64) -> (Self, Rc<RefCell<Vec<String>>>) {
        let heard = Rc::new(RefCell::new(Vec::new()));
        (
            Buyer {
                symbol: symbol.to_string(),
                every_nth,
                qty,
                seen: 0,
                heard: heard.clone(),
                work: None,
                leverage: None,
                reduce_only: false,
            },
            heard,
        )
    }

    /// The same buyer, but asking for its entry to be worked.
    fn working(
        symbol: &str,
        every_nth: u64,
        qty: f64,
        work: WorkPolicy,
    ) -> (Self, Rc<RefCell<Vec<String>>>) {
        let (mut buyer, heard) = Buyer::new(symbol, every_nth, qty);
        buyer.work = Some(work);
        (buyer, heard)
    }
}

impl Strategy for Buyer {
    fn name(&self) -> &str {
        "buyer"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        match event {
            EngineEvent::Market(MarketEvent::Quote { symbol, quote }) => {
                self.seen += 1;
                if !self.seen.is_multiple_of(self.every_nth) {
                    return;
                }
                ctx.place(Intent {
                    exact_prices: None,
                    exact_quantity: None,
                    strategy: StrategyId(0),
                    symbol: *symbol,
                    side: Side::Buy,
                    qty: self.qty,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec {
                        trigger_px: quote.bid_px * 0.99,
                    }),
                    reduce_only: self.reduce_only,
                    tag: "buy".into(),
                    decided_ns: ctx.now_ns(),
                    work: self.work,
                    leverage: self.leverage,
                });
            }
            EngineEvent::Order(update) => {
                self.heard.lock().unwrap().push(format!("{update:?}"));
            }
            _ => {}
        }
    }
}

/// Arms one timer on its first quote and writes down every timer it hears.
struct Ticker {
    symbol: String,
    timer: TimerId,
    after_ns: u64,
    armed: bool,
    fired: Rc<RefCell<Vec<TimerId>>>,
}

impl Ticker {
    fn new(symbol: &str, timer: u32, after_ns: u64) -> (Self, Rc<RefCell<Vec<TimerId>>>) {
        let fired = Rc::new(RefCell::new(Vec::new()));
        (
            Ticker {
                symbol: symbol.to_string(),
                timer: TimerId(timer),
                after_ns,
                armed: false,
                fired: fired.clone(),
            },
            fired,
        )
    }
}

impl Strategy for Ticker {
    fn name(&self) -> &str {
        "ticker"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        match event {
            EngineEvent::Market(_) if !self.armed => {
                self.armed = true;
                ctx.arm_timer(self.timer, self.after_ns);
            }
            EngineEvent::Timer { id, .. } => self.fired.lock().unwrap().push(*id),
            _ => {}
        }
    }
}

fn owned_exit_fixture(
    strategy: &str,
    side: Side,
    qty: f64,
) -> (Vec<WalRecord>, Vec<engine_types::PositionView>) {
    let stop_px = if side == Side::Buy {
        27_000.0
    } else {
        33_000.0
    };
    let id = "eng-owned-exit-fixture".to_string();
    let records = vec![
        WalRecord::Names {
            strategies: vec![strategy.into()],
            symbols: vec!["BTCUSDT".into()],
        },
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: id.clone(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side,
                qty,
                kind: OrderKind::Market,
                stop: Some(StopSpec {
                    trigger_px: stop_px,
                }),
                reduce_only: false,
                close_position: false,
                sleeve_effect: None,
                exact_terms: None,
            },
            wire_ns: 1,
            arrival_mid: 30_000.0,
        },
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                client_order_id: id,
                exec_id: "owned-exit-fixture-fill".into(),
                allocation: None,
                symbol: SymbolId(0),
                side,
                qty,
                px: 30_000.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: recent_replay_ms(),
                recv_ns: 2,
                amounts: None,
            },
        },
    ];
    let held = vec![engine_types::PositionView {
        exact_amounts: None,
        exact_stop_px: None,
        symbol: SymbolId(0),
        side,
        qty,
        entry_px: 30_000.0,
        stop_attached: true,
        stop_px,
        leverage: None,
    }];
    (records, held)
}

// ------------------------------------------------------------------ helpers

fn settings() -> EngineSection {
    EngineSection {
        wal_path: "unused-in-mocks.wal".into(),
        // Named but unused: these tests hand the engine a mock venue
        // directly rather than going through assembly.
        venue: engine_venue::BYBIT_DEMO.to_string(),
        group_flush_ms: 250,
        wal_rotate_mb: 256,
        account_view_max_age_ms: 60_000,
        // Wide enough that no scripted quote in these tests ever counts as
        // stale; the staleness tests tighten it themselves.
        max_quote_age_ms: 60_000,
        // Shared is the default used by this engine test bench.
        leverage_authority: crate::config::LeverageAuthority::Shared,
        // A test that wants a book, or a heartbeat, hands the engine one
        // itself.
        signal_spool_path: None,
        control_spool_path: None,
        heartbeat_path: None,
        trades_path: None,
    }
}

struct Harness {
    recovery_reads: Arc<MockRecoveryControl>,
    tape: Tape,
    records: Rc<RefCell<Vec<WalRecord>>>,
    sends: Rc<RefCell<Vec<OrderRequest>>>,
    cancels: Rc<RefCell<Vec<(SymbolId, String)>>>,
    amends: Rc<RefCell<Vec<(SymbolId, String, AmendSpec)>>>,
    stops: Rc<RefCell<Vec<(SymbolId, f64)>>>,
    stop_failures_remaining: Rc<RefCell<usize>>,
    risk_saw: Rc<RefCell<Vec<OrderUpdate>>>,
    /// The rolling loss window's hooks, as the kernel heard them.
    risk_rolling: MockRolling,
    leverages: Rc<RefCell<Vec<(SymbolId, f64)>>>,
    /// Positions the venue's next account readings will report; see
    /// `MockVenue::account_readings`.
    account_readings: Rc<RefCell<VecDeque<Vec<engine_types::PositionView>>>>,
    account_view_fails: Rc<RefCell<bool>>,
    /// The venue's execution history; see `MockVenue::executions`.
    executions: Rc<RefCell<Option<Vec<VenueExecution>>>>,
    /// Scripted cancel replies and status-read answers; see `MockVenue`.
    cancel_replies: Rc<RefCell<VecDeque<Result<(), VenueError>>>>,
    lookups: Rc<RefCell<VecDeque<engine_types::orders::OrderLookup>>>,
}

async fn build(
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_with_venue_orders(verdict, strategies, symbols, replayed, Vec::new()).await
}

/// The same, with the venue already working some orders — which is how a boot
/// finds out somebody else is on the account.
async fn build_with_venue_orders(
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_with(&settings(), verdict, strategies, symbols, replayed, working).await
}

/// The same again, on settings the test chose — a quicker tick, say.
async fn build_with(
    settings: &EngineSection,
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_inner(
        settings,
        verdict,
        strategies,
        symbols,
        replayed,
        working,
        BuildOptions::default(),
    )
    .await
}

async fn build_with_amend_verdict(
    amend_verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_inner(
        &settings(),
        allow_all(),
        strategies,
        symbols,
        replayed,
        working,
        BuildOptions {
            amend_verdict: Some(amend_verdict),
            ..BuildOptions::default()
        },
    )
    .await
}

/// The same, with the venue already holding positions when boot reads it —
/// the shape of every restart on an account that was trading.
async fn build_with_venue_state(
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
    held: Vec<engine_types::PositionView>,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_with_venue_state_and_rule(verdict, strategies, symbols, replayed, working, held, None)
        .await
}

async fn build_with_venue_state_and_rule(
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
    held: Vec<engine_types::PositionView>,
    rule: Option<InstrumentRule>,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_holding(
        &settings(),
        verdict,
        strategies,
        symbols,
        replayed,
        working,
        held,
        rule,
    )
    .await
}

/// The same again, on settings the test chose — a trades file, say.
#[allow(clippy::too_many_arguments)]
async fn build_holding(
    settings: &EngineSection,
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
    held: Vec<engine_types::PositionView>,
    rule: Option<InstrumentRule>,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), symbols);
    if let Some(rule) = rule {
        venue.rules[0].1 = rule;
    }
    venue.working = working;
    venue.account_readings.lock().unwrap().push_back(held);
    let cancels = venue.cancels.clone();
    let amends = venue.amends.clone();
    let stops = venue.stops.clone();
    let stop_failures_remaining = venue.stop_failures_remaining.clone();
    let leverages = venue.leverages.clone();
    let recovery_reads = venue.recovery_reads.clone();
    let account_readings = venue.account_readings.clone();
    let account_view_fails = venue.account_view_fails.clone();
    let executions = venue.executions.clone();
    let cancel_replies = venue.cancel_replies.clone();
    let lookups = venue.lookups.clone();
    let (risk, risk_saw) = MockRisk::with(verdict);
    let risk_rolling = risk.rolling.clone();
    let (strategies, sleeves, replayed) = assemble_fixture_names(strategies, symbols, replayed);
    let engine = Engine::boot_as(
        settings,
        "0000000000000000",
        wal,
        risk,
        venue,
        strategies,
        &sleeves,
        &replayed,
    )
    .await
    .expect("boot");
    (
        engine,
        Harness {
            recovery_reads,
            tape,
            records,
            sends,
            cancels,
            amends,
            stops,
            stop_failures_remaining,
            risk_saw,
            risk_rolling,
            leverages,
            account_readings,
            account_view_fails,
            executions,
            cancel_replies,
            lookups,
        },
    )
}

#[derive(Default)]
struct BuildOptions {
    amend_verdict: Option<RiskVerdict>,
    /// The venue answers status reads from `Harness::lookups`.
    lookups: bool,
}

/// A venue that answers status reads from a script, for the orders whose
/// cancel reply is not the confirmation.
async fn build_with_lookups(
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_inner(
        &settings(),
        allow_all(),
        strategies,
        symbols,
        &[],
        Vec::new(),
        BuildOptions {
            lookups: true,
            ..BuildOptions::default()
        },
    )
    .await
}

async fn build_inner(
    settings: &EngineSection,
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
    options: BuildOptions,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), symbols);
    venue.working = working;
    venue.lookup_scripted = options.lookups;
    let cancels = venue.cancels.clone();
    let amends = venue.amends.clone();
    let stops = venue.stops.clone();
    let stop_failures_remaining = venue.stop_failures_remaining.clone();
    let leverages = venue.leverages.clone();
    let recovery_reads = venue.recovery_reads.clone();
    let account_readings = venue.account_readings.clone();
    let account_view_fails = venue.account_view_fails.clone();
    let executions = venue.executions.clone();
    let cancel_replies = venue.cancel_replies.clone();
    let lookups = venue.lookups.clone();
    let (mut risk, risk_saw) = MockRisk::with(verdict);
    risk.amend_verdict = options.amend_verdict;
    let risk_rolling = risk.rolling.clone();
    let (strategies, sleeves, replayed) = assemble_fixture_names(strategies, symbols, replayed);
    let engine = Engine::boot_as(
        settings,
        "0000000000000000",
        wal,
        risk,
        venue,
        strategies,
        &sleeves,
        &replayed,
    )
    .await
    .expect("boot");
    (
        engine,
        Harness {
            recovery_reads,
            tape,
            records,
            sends,
            cancels,
            amends,
            stops,
            stop_failures_remaining,
            risk_saw,
            risk_rolling,
            leverages,
            account_readings,
            account_view_fails,
            executions,
            cancel_replies,
            lookups,
        },
    )
}

/// Test fixtures often name only the records relevant to their assertion.
/// A real pre-checkpoint WAL still starts with Boot, which is the compatible
/// recovery boundary. Supply that omitted framing without weakening boot's
/// refusal of an actually unbounded existing log.
fn assemble_fixture_names(
    mut strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
) -> (Vec<Box<dyn Strategy>>, Vec<String>, Vec<WalRecord>) {
    let mut names = replayed
        .iter()
        .rev()
        .find_map(|record| match record {
            WalRecord::Names { strategies, .. } | WalRecord::SegmentBase { strategies, .. } => {
                Some(strategies.clone())
            }
            _ => None,
        })
        .unwrap_or_default();
    for (index, strategy) in strategies.iter().enumerate().skip(names.len()) {
        let candidate = strategy.name().to_string();
        names.push(if names.contains(&candidate) {
            format!("{candidate}-{index}")
        } else {
            candidate
        });
    }
    while strategies.len() < names.len() {
        strategies.push(Box::new(crate::identities::InactiveStrategy::new(
            engine_types::identity::SleeveKey::new(names[strategies.len()].clone()).unwrap(),
            None,
        )));
    }
    let mut framed = replayed.to_vec();
    if !framed.is_empty()
        && !framed.iter().any(|record| {
            matches!(
                record,
                WalRecord::Names { .. }
                    | WalRecord::SegmentBase { .. }
                    | WalRecord::IdentityState { .. }
            )
        })
    {
        framed.insert(
            0,
            WalRecord::Names {
                strategies: names.clone(),
                symbols: symbols.iter().map(|symbol| (*symbol).to_string()).collect(),
            },
        );
    }
    (strategies, names, replay_with_history_boundary(&framed))
}

fn replay_with_history_boundary(replayed: &[WalRecord]) -> Vec<WalRecord> {
    if replayed.is_empty()
        || replayed.iter().any(|record| {
            matches!(
                record,
                WalRecord::Boot { .. } | WalRecord::ExecutionHistoryCheckpoint { .. }
            ) || matches!(
                record,
                WalRecord::SegmentBase {
                    execution_history_through_ms: Some(_),
                    ..
                }
            )
        })
    {
        return replayed.to_vec();
    }
    let mut bounded = Vec::with_capacity(replayed.len() + 1);
    bounded.push(WalRecord::Boot {
        version: ENGINE_VERSION.into(),
        config_sha256: "test-fixture".into(),
        wall_ts_ms: recent_replay_ms(),
        commit: String::new(),
    });
    bounded.extend_from_slice(replayed);
    bounded
}

/// The venue's own row for an order this engine's log sent and the venue is
/// still working — the boot case where a recovered order is genuinely alive.
/// An in-flight order the venue does NOT confirm is reaped at boot instead.
fn still_working(id: &str, symbol: &str, qty: f64) -> VenueOrder {
    VenueOrder {
        client_order_id: id.into(),
        symbol: symbol.into(),
        side: Side::Buy,
        qty,
        filled_qty: 0.0,
        reduce_only: false,
    }
}

/// An order the venue is working that this engine's log has no record of
/// sending. Read by the reconciliation tests and by the heartbeat's, which
/// is why it lives on the bench rather than in either.
fn someone_elses_order(symbol: &str) -> VenueOrder {
    VenueOrder {
        client_order_id: "not-ours-1".into(),
        symbol: symbol.into(),
        side: Side::Buy,
        qty: 1.0,
        filled_qty: 0.0,
        reduce_only: false,
    }
}

pub(crate) fn allow_all() -> RiskVerdict {
    RiskVerdict::Allow { qty: f64::NAN }
}

#[test]
fn venue_clock_offset_is_venue_minus_the_local_receive_clock() {
    assert_eq!(
        crate::engine::venue_minus_local_ms(10_050, 1_000_000_000, 1_005_000_000, 10_030),
        25
    );
}

mod boot_rules;
mod covers;
mod durable_signals;
mod fill_costs;
mod forced_close;
mod gap_recovery;
mod halt_cancels;
mod heartbeat;
mod live_legacy_fixture;
mod order_path;
mod ownership;
mod quote_staleness;
mod reconciliation;
mod recovery_liveness;
mod resting_orders;
mod retained_archive_boot;
mod rolling_loss;
mod rotation;
mod runtime_controls;
mod scheduler_fairness;
pub(crate) mod shared_sleeves;
mod signal_availability;
mod standalone_stops;
mod strategy_checkpoints;
mod strategy_events;
mod update_contract;
mod worked_entries;

pub(crate) async fn lifecycle_test_fixture(
    strategies: Vec<Box<dyn Strategy>>,
) -> (
    Engine<MockWal, MockRisk, MockVenue>,
    Arc<Mutex<Vec<WalRecord>>>,
) {
    let (engine, harness) = build(allow_all(), strategies, &[], &[]).await;
    (engine, harness.records)
}

pub(crate) async fn callback_test_fixture(
    strategies: Vec<Box<dyn Strategy>>,
) -> (
    Engine<MockWal, MockRisk, MockVenue>,
    Arc<Mutex<Vec<WalRecord>>>,
) {
    let (engine, harness) = build(allow_all(), strategies, &["BTCUSDT"], &[]).await;
    (engine, harness.records)
}

impl MockWal {
    pub(crate) fn delay_metadata_barriers(&mut self, duration: Duration) {
        self.sync_barrier_takes = duration;
        self.delay_callback_barriers(duration);
    }

    pub(crate) fn delay_callback_barriers(&mut self, duration: Duration) {
        self.defer_barriers();
        self.barrier_takes = duration;
    }
}

#[tokio::test(start_paused = true)]
async fn a_stalled_order_lookup_cannot_hold_a_cancel_or_a_reduction() {
    let (mut venue, sends) = MockVenue::new(tape(), &["BTCUSDT"]);
    let started = Arc::new(tokio::sync::Notify::new());
    venue.lookup_started = Some(started.clone());
    let (mut client, mut completions) = crate::venue_runtime::VenueClient::spawn(venue);
    let lookup = client
        .dispatch_order_status("BTCUSDT", "ambiguous-order")
        .unwrap();
    tokio::time::timeout(Duration::from_secs(1), started.notified())
        .await
        .unwrap();
    client
        .dispatch_cancels(vec![(SymbolId(0), "working-opening".into())])
        .unwrap();
    let cancellation = tokio::time::timeout(Duration::from_millis(100), completions.recv())
        .await
        .expect("lookup held the urgent cancel")
        .unwrap();
    assert!(
        matches!(cancellation, crate::venue_runtime::MutationCompletion::Cancels { replies, .. } if replies.len() == 1 && replies[0].is_ok())
    );
    client
        .dispatch_orders(vec![OrderRequest {
            client_order_id: "protective-reduction".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.1,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            close_position: false,
            sleeve_effect: None,
            exact_terms: None,
        }])
        .unwrap();
    let reduction = tokio::time::timeout(Duration::from_millis(100), completions.recv())
        .await
        .expect("lookup held the protective reduction")
        .unwrap();
    assert!(
        matches!(reduction, crate::venue_runtime::MutationCompletion::Orders { replies, .. } if replies.len() == 1 && replies[0].is_ok())
    );
    assert_eq!(sends.lock().unwrap().len(), 1);
    drop(lookup);
}

struct StalledLookup(Arc<tokio::sync::Notify>);
#[engine_types::async_trait]
impl engine_types::orders::OrderLookupClient for StalledLookup {
    async fn lookup(
        &self,
        _symbol: &str,
        _client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        self.0.notify_one();
        std::future::pending().await
    }
}

/// Answers status reads in the scripted order; an exhausted script says the
/// venue could not find the order.
struct ScriptedLookup(Rc<RefCell<VecDeque<engine_types::orders::OrderLookup>>>);
#[engine_types::async_trait]
impl engine_types::orders::OrderLookupClient for ScriptedLookup {
    async fn lookup(
        &self,
        _symbol: &str,
        client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        Ok(self.0.lock().unwrap().pop_front().unwrap_or_else(|| {
            engine_types::orders::OrderLookup::Unknown {
                reason: format!("the mock venue has no answer for {client_order_id}"),
            }
        }))
    }
}

pub(crate) async fn symbol_admission_test_fixture(
    strategy: Box<dyn Strategy>,
) -> (
    Engine<MockWal, MockRisk, MockVenue>,
    Arc<Mutex<Vec<WalRecord>>>,
    Arc<Mutex<Vec<OrderRequest>>>,
) {
    let (prior, held) = owned_exit_fixture(strategy.name(), Side::Buy, 0.01);
    let (engine, harness) = build_with_venue_state(
        allow_all(),
        vec![strategy],
        &["BTCUSDT"],
        &prior,
        Vec::new(),
        held,
    )
    .await;
    (engine, harness.records, harness.sends)
}

#[derive(Debug)]
struct TestCatalogCache(Vec<String>);
impl engine_types::orders::InstrumentCatalogCache for TestCatalogCache {
    fn retain_previous(
        &self,
        checkpoint: &engine_types::orders::InstrumentCatalogCheckpoint,
    ) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        if checkpoint.cache.kind != "mock-native-map" {
            return Err(VenueError::BadReply("wrong retained mock map".into()));
        }
        let mut names: Vec<String> = serde_json::from_slice(&checkpoint.cache.payload)
            .map_err(|error| VenueError::BadReply(error.to_string()))?;
        for name in &self.0 {
            if !names.contains(name) {
                names.push(name.clone());
            }
        }
        Ok(test_instrument_catalog(
            &names.iter().map(String::as_str).collect::<Vec<_>>(),
        ))
    }

    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
    fn checkpoint(
        &self,
    ) -> Result<engine_types::orders::InstrumentCatalogCacheSnapshot, VenueError> {
        Ok(engine_types::orders::InstrumentCatalogCacheSnapshot {
            kind: "mock-native-map".into(),
            payload: serde_json::to_vec(&self.0).unwrap(),
        })
    }
}
struct SharedCatalogClient(Arc<dyn engine_types::orders::InstrumentCatalogClient>);
#[engine_types::async_trait]
impl engine_types::orders::InstrumentCatalogClient for SharedCatalogClient {
    async fn fetch(&self) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        self.0.fetch().await
    }
}

pub(crate) fn test_instrument_catalog(names: &[&str]) -> engine_types::orders::InstrumentCatalog {
    use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, PricePrecision};
    let rule = InstrumentRule {
        tick_size: 0.5,
        qty_step: 0.001,
        min_qty: 0.001,
        min_notional: 5.0,
    };
    let decimal = |value| Some(Exact::parse_decimal(value).unwrap());
    let specs = names
        .iter()
        .map(|name| {
            (
                (*name).into(),
                ExactInstrumentSpec {
                    native_symbol: (*name).into(),
                    base_asset: AssetId::Named(name.trim_end_matches("USDT").into()),
                    quote_asset: AssetId::Named("USDT".into()),
                    settlement_asset: AssetId::Named("USDT".into()),
                    tick_size: decimal("0.5"),
                    min_price: None,
                    max_price: None,
                    price_precision: PricePrecision::Tick,
                    qty_step: decimal("0.001"),
                    min_qty: decimal("0.001"),
                    market_qty_step: decimal("0.001"),
                    market_min_qty: decimal("0.001"),
                    max_qty: None,
                    max_market_qty: None,
                    min_notional: decimal("5"),
                    contract_multiplier: decimal("1"),
                    fee_assets: None,
                    fee_step: None,
                },
            )
        })
        .collect();
    engine_types::orders::InstrumentCatalog {
        cache: Some(Arc::new(TestCatalogCache(
            names.iter().map(|name| (*name).into()).collect(),
        ))),
        rules: names.iter().map(|name| ((*name).into(), rule)).collect(),
        specs,
    }
}

pub(crate) async fn catalog_restart_test_fixture(
    strategy: Box<dyn Strategy>,
    prior: Option<Vec<WalRecord>>,
    client: Arc<dyn engine_types::orders::InstrumentCatalogClient>,
) -> (
    Engine<MockWal, MockRisk, MockVenue>,
    Arc<Mutex<Vec<WalRecord>>>,
    Arc<Mutex<Vec<OrderRequest>>>,
) {
    let (history, held) = owned_exit_fixture(strategy.name(), Side::Buy, 0.01);
    let mut prior = prior.unwrap_or(history);
    if !prior.iter().any(|record| {
        matches!(
            record,
            WalRecord::InstrumentCatalogCheckpoint { .. }
                | WalRecord::SegmentBase {
                    instrument_catalog: Some(_),
                    ..
                }
        )
    }) {
        prior.push(WalRecord::InstrumentCatalogCheckpoint {
            wall_ts_ms: clock::wall_ms(),
            checkpoint: Box::new(test_instrument_catalog(&["BTCUSDT"]).checkpoint().unwrap()),
        });
    }
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.catalog_client = Some(client);
    venue.account_readings.lock().unwrap().push_back(held);
    let (risk, _) = MockRisk::with(allow_all());
    let (strategies, sleeves, prior) = assemble_fixture_names(vec![strategy], &["BTCUSDT"], &prior);
    let engine = Engine::boot_as(
        &settings(),
        "catalog-restart",
        wal,
        risk,
        venue,
        strategies,
        &sleeves,
        &prior,
    )
    .await
    .unwrap();
    (engine, records, sends)
}

pub(crate) async fn physical_recovery_test_fixture(
    strategy: Box<dyn Strategy>,
    prior: &[WalRecord],
    qty: f64,
) -> (
    Engine<MockWal, MockRisk, MockVenue>,
    Arc<Mutex<Vec<WalRecord>>>,
) {
    let held = engine_types::PositionView {
        exact_amounts: None,
        exact_stop_px: None,
        symbol: SymbolId(0),
        side: Side::Buy,
        qty,
        entry_px: 100.0,
        stop_px: 90.0,
        stop_attached: true,
        leverage: None,
    };
    let (engine, harness) = build_with_venue_state(
        allow_all(),
        vec![strategy],
        &["BTCUSDT"],
        prior,
        vec![],
        vec![held],
    )
    .await;
    (engine, harness.records)
}

pub(crate) async fn portfolio_route_test_fixture(
    prior: Option<Vec<WalRecord>>,
) -> (
    Engine<MockWal, MockRisk, MockVenue>,
    Arc<Mutex<Vec<WalRecord>>>,
) {
    let prior = prior.unwrap_or_else(|| shared_sleeves::owned_records("1", "1"));
    let strategies: Vec<Box<dyn Strategy>> = ["left", "right"]
        .into_iter()
        .map(|name| {
            Box::new(crate::identities::InactiveStrategy::new(
                engine_types::identity::SleeveKey::new(name).unwrap(),
                None,
            )) as Box<dyn Strategy>
        })
        .collect();
    let (engine, harness) = build_with_venue_state(
        allow_all(),
        strategies,
        &["BTCUSDT"],
        &prior,
        vec![],
        vec![],
    )
    .await;
    (engine, harness.records)
}

pub(crate) fn recovery_venue_fixture(rows: Vec<VenueExecution>) -> MockVenue {
    let (venue, _) = MockVenue::new(tape(), &["BTCUSDT"]);
    *venue.executions.lock().unwrap() = Some(rows);
    venue
}

pub(crate) fn recovery_spool_fixture(history: engine_types::ExecutionHistory) -> MockVenue {
    let (mut venue, _) = MockVenue::new(tape(), &["BTCUSDT"]);
    venue.spooled_history = Some(history);
    venue
}

pub(crate) async fn recovery_inventory_fixture() -> (
    Engine<MockWal, MockRisk, MockVenue>,
    Arc<Mutex<Vec<WalRecord>>>,
) {
    let (buyer, _) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (engine, harness) = order_path::build_exit_inventory(
        vec![Box::new(buyer)],
        &[(StrategyId(0), Side::Buy, 0.01)],
        None,
    )
    .await;
    (engine, harness.records)
}

mod legacy_accounting_boot;
