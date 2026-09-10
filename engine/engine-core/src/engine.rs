//! The loop.
//!
//! The current-thread runtime owns account, risk and durable state. Registered
//! callbacks run embedded on the loop thread. Venue mutations and independent
//! status lookups return through bounded completion channels.
//!
//! What the loop waits on, all in one `select!`:
//!
//! - the market feed
//! - the private order feed
//! - durable public-signal observations
//! - the next due strategy timer
//! - the group-flush tick (also the moment the account reading is refreshed)
//! - shutdown
//!
//! **Feeds must be cancel-safe.** `select!` drops the futures of the branches
//! that did not win, so `MarketFeed::next_event` and `OrderFeed::next_update`
//! must lose nothing when dropped part-way — the same contract
//! `tokio::sync::mpsc::Receiver::recv` keeps. A feed that reads a socket must
//! park partial reads in its own buffer, not in the future.
//!
//! Each placement durably records its exact request before marking an attempt
//! and dispatching. Queued work resumes with its original ID; attempted work
//! requires authoritative venue disposition. Callback state commits with its
//! ordered effects, whose suffix remains owned until every disposition commits.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::future::Future;
use std::task::Poll;
use std::time::Duration;

#[cfg(test)]
use engine_types::{quantize, StopSpec};

use engine_types::{
    AccountView, Action, AmendSpec, DenyReason, EngineEvent, Feed, FeedError, InstrumentRule,
    Intent, MarketEvent, MarketFeed, MarketState, OrderFeed, OrderKind, OrderRequest, OrderUpdate,
    RiskKernel, RiskVerdict, RuntimeControlError, RuntimeControlFeed, RuntimeControlRequest, Side,
    SignalError, SignalFeed, SignalObservation, Strategy, StrategyCheckpoint, StrategyEvent,
    StrategyGlobalCheckpointState, StrategyId, Subscription, SymbolId, SymbolTable, TimeInForce,
    VenueError, VenueGateway, Wal, WalError, WalRecord, WorkPolicy,
};

use crate::attribution::Attribution;
use crate::clock;
use crate::config::EngineSection;
use crate::covers::CoverBook;
use crate::ctx::{Books, PendingAction, StrategyHost, Timers};
use crate::execution::{self, Fills};
use crate::execution_ids::{ExecutionIds, RECOVERY_PAD_MS, RECOVERY_REACH_MS};
use crate::heartbeat::{self, Heartbeat};
use crate::inflight::{self, LedgerOfOrders, OrderRegistry};
use crate::ledger::{LatencyLedger, Segment};
use crate::reconcile;
use crate::routing::Routing;
use crate::trades::Trades;
use crate::venue_runtime::{MutationCompletion, VenueClient};
use crate::working::{self, WorkingOrders};

/// A strategy that emits from every order update it hears could keep the loop
/// busy forever. One wake handles this many actions; past that only the ones
/// that reduce risk keep flowing, and the loop goes back to reading the
/// market.
pub const MAX_INTENTS_PER_WAKE: usize = 64;

pub(crate) const MAX_TIMER_CALLBACKS_PER_TURN: usize = 64;

#[derive(Clone, Copy)]
enum OrdinaryLane {
    Tick,
    Timer,
    Control,
    Signal,
    Market,
}

impl OrdinaryLane {
    fn next(self) -> Self {
        match self {
            Self::Tick => Self::Timer,
            Self::Timer => Self::Control,
            Self::Control => Self::Signal,
            Self::Signal => Self::Market,
            Self::Market => Self::Tick,
        }
    }
}

// Keep the bounded market payload inline; boxing would allocate for every market event.
#[allow(clippy::large_enum_variant)]
enum OrdinaryInput {
    Tick,
    Timer,
    Control(Result<RuntimeControlRequest, RuntimeControlError>),
    Signal(Result<engine_types::SignalFeedEvent, SignalError>),
    Market(Result<MarketEvent, FeedError>),
}

impl OrdinaryInput {
    fn lane(&self) -> OrdinaryLane {
        match self {
            Self::Tick => OrdinaryLane::Tick,
            Self::Timer => OrdinaryLane::Timer,
            Self::Control(_) => OrdinaryLane::Control,
            Self::Signal(_) => OrdinaryLane::Signal,
            Self::Market(_) => OrdinaryLane::Market,
        }
    }
}

const MUTATION_DRAIN_TIMEOUT: Duration = Duration::from_secs(10);

/// Latch opening off in the log, and say so in the journal.
///
/// The latch outlives the process and only an operator clears it, so a
/// `Reconciled { may_open: false }` that reaches the WAL and nothing else
/// leaves whoever is called out with a reduce-only funded engine, a watchdog
/// page, and no line to read. Boot says it on the next start; a running engine
/// has to say it when it happens.
pub(crate) fn record_latch<W: Wal>(
    wal: &mut W,
    wall_ts_ms: i64,
    findings: Vec<String>,
) -> Result<u64, WalError> {
    if findings.is_empty() {
        tracing::error!("this engine will not open new positions until an operator clears it");
    }
    for finding in &findings {
        tracing::error!(
            "this engine will not open new positions until an operator clears it: {finding}"
        );
    }
    wal.append(&WalRecord::Reconciled {
        wall_ts_ms,
        findings,
        may_open: false,
    })
}

/// Record a dispatch the venue never stated the outcome of.
///
/// The first such dispatch stops the engine opening, so an opening already
/// queued in the venue task loses the permission it was admitted under and is
/// refused at the send boundary rather than sent. Free-standing because the
/// callers hold other fields of the engine while they say this.
pub(crate) fn note_unresolved(
    dispatches: &mut crate::order_dispatch::OrderDispatches,
    authority: &engine_types::AuthorityEpoch,
    client_order_id: String,
    reason: String,
) {
    if dispatches.unresolved.is_empty() {
        authority.advance();
    }
    dispatches.unresolved.insert(client_order_id, reason);
}

/// How long the loop stands off a feed that erred without closing.
const HICCUP_PAUSE: Duration = Duration::from_millis(1);

/// Largest set of placements that may share one risk reservation, WAL
/// barrier, and concurrent venue submission. It matches Bybit's conservative
/// per-UID create-order window. A larger strategy burst is re-evaluated in
/// successive groups, after every acknowledgement from the preceding group;
/// no order may sit durably reserved behind several HTTP timeout waves.
pub const MAX_ORDERS_PER_BATCH: usize = 10;
/// Bybit charges cancel-batch quota per order, and its default linear window
/// admits ten per second. Other adapters receive the same bounded groups
/// through the trait's serial default.
pub const MAX_CANCELS_PER_BATCH: usize = 10;

#[cfg(not(test))]
const HALT_CANCEL_CONFIRM_NS: u64 = 5_000_000_000;
#[cfg(test)]
const HALT_CANCEL_CONFIRM_NS: u64 = 25_000_000;

/// How long a refused or unanswered halt cancel waits between two reads of
/// the order's status at the venue.
#[cfg(not(test))]
const HALT_LOOKUP_RETRY_NS: u64 = 500_000_000;
#[cfg(test)]
const HALT_LOOKUP_RETRY_NS: u64 = 5_000_000;

/// How long an accepted amend may go unexplained before the order is pulled.
///
/// The venue answers `order.amend` by saying it took the request, never by
/// saying what price it left the order at. It states the price separately,
/// by republishing the order on the private stream, and that arrives in
/// single-digit milliseconds. This is the outer bound: past it, an order
/// resting at a price the engine cannot name is worth less than the queue
/// position holding it saves.
#[cfg(not(test))]
const AMEND_CONFIRM_NS: u64 = 2_000_000_000;
#[cfg(test)]
const AMEND_CONFIRM_NS: u64 = 25_000_000;

mod order_epoch;
#[cfg(test)]
pub(crate) use free_helpers::mint_unused;

mod free_helpers;
mod portfolio_runtime;
use free_helpers::*;
pub(crate) use free_helpers::{
    durable_risk_verdict, forget_leverage_where_flat, named_entry_blockers, named_strategy_errors,
    venue_minus_local_ms,
};

pub const ENGINE_VERSION: &str = concat!("engine-core ", env!("CARGO_PKG_VERSION"));
/// The git commit this binary was built from (build.rs), "-dirty" when the
/// tree had uncommitted tracked changes.
pub const ENGINE_COMMIT: &str = env!("ENGINE_GIT_COMMIT");

/// How many recently journaled fills to remember for gap-recovery dedup.
/// A gap plus its pads spans minutes; this covers hours of fills.
const RECENT_FILLS_KEPT: usize = 2048;

/// A quiet account renews its execution-history proof daily, well inside the
/// shortest supported venue history window.
const HISTORY_CHECKPOINT_INTERVAL_MS: i64 = 86_400_000;

/// Why a run ended without being asked to. The supervisor restarts the
/// unit on any of these; the class says what a restart can settle.
#[derive(Debug)]
pub enum EngineError {
    Wal(WalError),
    Venue(VenueError),
    /// Boot cannot continue from this log, config, and venue. A restart
    /// into the same state fails the same way.
    Boot(String),
    /// A task the engine cannot run without has ended.
    TaskStopped {
        task: EngineTask,
        detail: &'static str,
    },
    /// A bounded wait ran out; the string is what did not arrive.
    TimedOut(String),
    /// The venue's account and the engine's view disagree in a way only a
    /// fresh boot's reconciliation settles.
    Reconcile(String),
    /// An invariant on the engine's own state failed.
    State(String),
}

/// A task the engine depends on. Which one died is the difference between
/// a venue outage and a fault in the log writer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EngineTask {
    /// Sends, cancels, amends, and account reads.
    Venue,
    /// Makes order dispatches durable before they leave.
    DispatchDurability,
}

impl std::fmt::Display for EngineTask {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            EngineTask::Venue => "venue task",
            EngineTask::DispatchDurability => "dispatch durability task",
        })
    }
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::Wal(e) => write!(f, "log: {e}"),
            EngineError::Venue(e) => write!(f, "venue: {e}"),
            EngineError::Boot(m) => write!(f, "boot: {m}"),
            EngineError::TaskStopped { task, detail: "" } => write!(f, "{task} stopped"),
            EngineError::TaskStopped { task, detail } => write!(f, "{task} stopped {detail}"),
            EngineError::TimedOut(waiting_for) => write!(f, "timed out waiting for {waiting_for}"),
            EngineError::Reconcile(m) => write!(f, "venue reconciliation needed: {m}"),
            EngineError::State(m) => write!(f, "state: {m}"),
        }
    }
}

impl std::error::Error for EngineError {}

impl From<WalError> for EngineError {
    fn from(e: WalError) -> Self {
        EngineError::Wal(e)
    }
}

impl From<VenueError> for EngineError {
    fn from(e: VenueError) -> Self {
        EngineError::Venue(e)
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum StopReason {
    Shutdown,
    FeedClosed,
}

/// Where the loop's two time-driven waits come from: the group-flush tick
/// and the sleep to the next strategy timer. Live, Tokio's own timers. A
/// replay driver hands in a clock it advances itself, so those two waits
/// fire in the tape's time and not the wall's. Monomorphised: the live loop
/// pays nothing for the seam.
pub trait LoopTimer {
    type Sleep: Future<Output = ()>;
    type Interval: LoopInterval;
    fn sleep(&self, duration: Duration) -> Self::Sleep;
    fn interval(&self, period: Duration) -> Self::Interval;
}

/// A repeating tick. The first tick is due at once; after a tick fires late
/// the next is a full period after it (Tokio's `MissedTickBehavior::Delay`).
#[allow(async_fn_in_trait)]
pub trait LoopInterval {
    async fn tick(&mut self);
}

/// The wall's timers, which is what `run` uses.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemTimer;

impl LoopTimer for SystemTimer {
    type Sleep = tokio::time::Sleep;
    type Interval = tokio::time::Interval;

    fn sleep(&self, duration: Duration) -> Self::Sleep {
        tokio::time::sleep(duration)
    }

    fn interval(&self, period: Duration) -> Self::Interval {
        let mut interval = tokio::time::interval(period);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        interval
    }
}

impl LoopInterval for tokio::time::Interval {
    async fn tick(&mut self) {
        tokio::time::Interval::tick(self).await;
    }
}

#[derive(Debug)]
pub struct RunOutcome {
    pub stopped_by: StopReason,
    pub market_events: u64,
    pub orders_sent: u64,
}

struct PreparedOrder {
    intent: Intent,
    request: OrderRequest,
    decided_ns: u64,
    origin_ns: u64,
}

enum PendingMutation {
    Leverage {
        symbol: SymbolId,
        want: f64,
        orders: Vec<String>,
        account: Box<AccountView>,
        queued_ns: u64,
    },
    SetStop {
        stop: stop_runtime::DurableStop,
        queued_ns: u64,
    },
    Orders {
        requests: Vec<OrderRequest>,
        timings: Vec<Option<crate::ctx::CallbackTiming>>,
        queued_ns: u64,
        authority: Option<engine_types::CommandAuthority>,
    },
    Cancels {
        requests: Vec<(SymbolId, String)>,
        queued_ns: u64,
    },
    Amend {
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
        existing: Box<crate::inflight::OrderRec>,
        amended_intent: Box<Intent>,
        remaining_qty: f64,
        old_px: f64,
        tif: TimeInForce,
        queued_ns: u64,
    },
}

/// One strategy wake suspended at a clean venue-mutation boundary.
///
/// The counters stay live across the cooperative turn so returning to the
/// feeds cannot reset the per-wake flood limit. `origin_ns` likewise keeps
/// every remaining sibling on the latency clock of the event that emitted it.
#[derive(Clone)]
struct DrainProgress {
    origin_ns: u64,
    handled: usize,
    adding_dropped: usize,
}

/// One not-yet-subscribed symbol and every strategy/feed pair waiting for it.
/// Grouping by symbol keeps concurrent native sleeves from admitting the same
/// name twice while retaining every listener that must be routed afterward.
struct WantedSymbol {
    name: String,
    listeners: Vec<(StrategyId, Feed)>,
}

/// One opening order an account-level halt is pulling. The deadline is set by
/// the first cancel reply and kept through every later state: the halt has
/// `HALT_CANCEL_CONFIRM_NS` from that reply to see the order end, however many
/// cancels and status reads that takes.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum HaltCancelState {
    Submitting {
        deadline_ns: Option<u64>,
    },
    AwaitingPrivate {
        deadline_ns: u64,
    },
    /// The cancel came back refused or unanswered, which is the venue not
    /// saying what the order is now. Its status is read instead: a working
    /// order is cancelled again, an ended one is closed from the answer.
    Resolving {
        deadline_ns: u64,
        retry_after_ns: u64,
    },
}

/// An amend the venue took, held until the private stream says what price it
/// left the order at. Everything needed to settle the reservation either way
/// is here, because the completion that opened it is long gone by then.
struct AwaitingAmend {
    symbol: SymbolId,
    amended_intent: Box<Intent>,
    remaining_qty: f64,
    tif: TimeInForce,
    deadline_ns: u64,
}

/// What was last written about a repeating refusal, and how many identical
/// ones have happened since.
struct Refusal {
    why: String,
    at_ns: u64,
    suppressed: u64,
}

/// How long an unchanged refusal stays collapsed before it is written again,
/// so a condition that never clears still leaves a periodic trace.
const REFUSAL_REPEAT_NS: u64 = 60_000_000_000;

/// What one loop turn decided about the next.
enum Turn {
    Continue,
    /// The private stream erred without closing. The turn's follow-up work
    /// still runs, but a suspended drain is not resumed on a stream that
    /// just broke.
    Hiccup,
    Stop(StopReason),
}

pub struct Engine<W: Wal, R: RiskKernel, V: VenueGateway> {
    pub wal: W,
    pub risk: R,
    venue: VenueClient,
    venue_completions: tokio::sync::mpsc::Receiver<MutationCompletion>,
    // Ordered maps throughout: anything the engine iterates can reach the
    // log, and two runs of one input must write one log. A hash seed must
    // never decide the order of two records.
    pending_mutations: BTreeMap<u64, PendingMutation>,
    busy_symbols: BTreeMap<SymbolId, usize>,
    order_lineage: order_lineage::OrderLineage,
    /// The last refusal recorded for each strategy, symbol and tag. A
    /// strategy that re-proposes a doomed order on every quote refuses just
    /// the same; only the record of it is collapsed, so one stuck position
    /// cannot bury the log the fill and latency reports read.
    refusals: BTreeMap<(StrategyId, SymbolId, String), Refusal>,
    deferred_actions: BTreeMap<SymbolId, VecDeque<(PendingAction, u64)>>,
    /// Actions released by a completed symbol mutation, retaining the market
    /// wake that produced each one. The per-wake flood budget and latency
    /// origin therefore survive a slow venue round trip.
    ready_actions: VecDeque<(PendingAction, u64)>,
    _venue: std::marker::PhantomData<V>,
    /// The strategies and what the engine holds on their behalf: timers,
    /// pending actions, checkpoints, cross-sleeve events, entry overrides.
    host: StrategyHost,
    /// What every strategy reads and none may edit: the market, the account
    /// reading, instrument rules, and the books about orders and ownership.
    books: Books,
    instrument_specs:
        std::collections::BTreeMap<SymbolId, engine_types::numeric::ExactInstrumentSpec>,
    require_exact_instruments: bool,
    identities: engine_types::identity::IdentityState,
    routing: Routing,
    /// Present only after a venue mutation has completed while the same
    /// strategy wake still has actions. The run loop polls the private stream
    /// and a due account-refresh tick before resuming it.
    drain_progress: Option<DrainProgress>,
    suspended_wakes: BTreeMap<u64, DrainProgress>,
    signals: crate::signal_state::SignalState,
    signal_dependencies: Vec<Vec<StrategyId>>,
    /// Every accepted operator command, retained for request-id idempotence
    /// across WAL rotation.
    runtime_control_requests: Vec<engine_types::RuntimeControlRequest>,
    /// Replayable commands a reducer has durably completed.
    runtime_control_consumed: std::collections::BTreeSet<(StrategyId, String)>,
    /// Validated observations held until every requested symbol/feed/rule is
    /// admitted. They are not delivered or cursor-advanced before then.
    pending_signal_deliveries: VecDeque<SignalObservation>,
    /// The resting entries this engine is advancing. Empty unless a strategy
    /// asked for one to be worked.
    working: WorkingOrders,
    /// Opening orders already handed to cancellation after any durable
    /// opening halt. A successful REST acknowledgement is asynchronous, so the order
    /// remains in the ledger until the private stream ends it; this set keeps
    /// each refresh tick from submitting the same cancel again meanwhile. An
    /// order that ends by any route leaves the set on the next halt pass.
    halt_cancels: BTreeMap<String, HaltCancelState>,
    amends_awaiting_price: BTreeMap<String, AwaitingAmend>,
    /// Since boot: amends whose price the venue stated, and amends pulled
    /// because it never did. The pair is the health of the confirmation —
    /// pulls climbing against confirmations is the venue not republishing,
    /// and every such pull pays the queue-position cost the confirmation
    /// exists to avoid.
    amends_confirmed: u64,
    amends_pulled_unconfirmed: u64,
    /// Since boot: private-stream resets, including the initial subscription.
    /// Each one is a gap the engine had to recover across.
    stream_resets: u64,
    dispatches: crate::order_dispatch::OrderDispatches,
    portfolio_controls: crate::portfolio_control::PortfolioControls,
    portfolio_dirty: bool,
    portfolio_cursor: u64,
    portfolio_physical_after: BTreeMap<SymbolId, u64>,
    /// Halt pulls bypass the ordinary per-wake action drain. One native-sized
    /// group is submitted per main-loop turn, with private order updates
    /// biased ahead of the next group.
    halt_cancel_queue: VecDeque<(SymbolId, String)>,
    /// Signal-requested symbol/feed subscriptions that are not live yet,
    /// together with every strategy waiting to hear them.
    ///
    /// Filled while a signal is validated and drained by the run loop, which
    /// is the only place that holds the feeds.
    wanted_symbols: Vec<WantedSymbol>,
    symbol_admission: symbol_admission::SymbolAdmission,
    /// What leverage each symbol was last set to by this engine.
    ///
    /// A symbol keeps its leverage at the venue until somebody changes it, so
    /// re-sending the same number before every entry would buy a round trip
    /// per order for nothing. What makes the cache safe is forgetting a symbol
    /// the moment the account reading shows it flat: the owner trades this
    /// account by hand, and a symbol that has been closed and reopened may
    /// have been set to anything in between.
    ///
    /// Under SOLE authority (an account this engine exclusively leases and
    /// nobody hand-trades) the forgetting stops: what this engine set stays
    /// trusted across flat spells, entries from flat skip the confirmation
    /// round trip, and every held position's
    /// leverage is instead read back off the venue's own position rows — a
    /// mismatch alarms and evicts the trust, so the next entry confirms
    /// inline again.
    leverage_at: BTreeMap<SymbolId, f64>,
    leverage_authority: crate::config::LeverageAuthority,
    execution_limits: Option<crate::config::ExecutionLimits>,
    recent_rejections: VecDeque<(u64, String)>,
    ledger: LatencyLedger,
    /// What the fills cost. The latency ledger beside it measures our own side
    /// of the wire; this one measures the price, which is the half that
    /// actually shows up in the account.
    fills: Fills,
    /// The heartbeat file, when one was configured. Telemetry only: nothing
    /// in the loop reads it and nothing in the loop waits on it.
    heartbeat: Option<Heartbeat>,
    /// Where closed round trips are written, when one was configured. Also
    /// telemetry: an engine that cannot say what a trade made still made it.
    trades: Option<Trades>,
    /// Whether boot's comparison against the venue left this engine free to
    /// add exposure. False latches: it is written into the log and read back
    /// on the next boot, so a restart cannot quietly clear it.
    may_open: bool,
    /// Ephemeral proof that the private account channel is usable. The
    /// runner establishes the first subscription before boot; any later feed
    /// error or reset clears this until both a fresh account view and history
    /// recovery succeed. Unlike `may_open`, a healthy reconnect may restore
    /// it without operator action.
    private_stream_ready: bool,
    /// What every opening queued right now is authorized by. Advanced the
    /// moment an engine-wide or per-strategy permission an opening was
    /// admitted under goes away, which is how a command still waiting in the
    /// venue queue is refused instead of sent. The venue task and the paced
    /// adapters read the same counter.
    authority: engine_types::AuthorityEpoch,
    /// How long an opening may wait in the venue queue before it is refused
    /// unsent. From `engine.opening_dispatch_ttl_ms`. Exits, cancels and
    /// stops never expire.
    opening_dispatch_ttl_ns: u64,
    /// What the risk kernel's rolling-loss window last reported, so its trip
    /// is seen once rather than polled.
    rolling_loss_tripped: bool,
    /// Monotonic stamp of the transition into unready, `None` while ready.
    /// It is what separates a venue's paced re-read, which clears readiness
    /// and restores it within one sweep, from a private stream that never
    /// comes back: only the age distinguishes them, and only a watcher
    /// reading the age can tell a design from a fault.
    private_stream_unready_since_ns: Option<u64>,
    recovery: account_recovery::Recovery,
    /// Signed quantity per symbol over every fill this log ever held —
    /// strangers' included, because it mirrors the log's records, not the
    /// strategies. It is what reconcile compares the venue's positions
    /// against, seeded at boot by the same scan reconcile uses and kept
    /// live by the same arithmetic, so a rotation can restate it exactly.
    logged_exposure: crate::reconcile::PhysicalExposure,
    /// The stop belonging to each trusted filled position, kept live for the
    /// same reason: a stop the venue drops after a rotation must still be
    /// repairable at the level and direction the log proved. Unfilled
    /// opposite-side siblings never enter this map.
    intended_stops: std::collections::BTreeMap<SymbolId, reconcile::IntendedPositionStop>,
    /// Stop moves the venue accepted since the latest account reading. This
    /// closes the short gap before that reading reflects the new stop without
    /// confusing a durable intent with a successful API call.
    stop_repairs_pending: std::collections::BTreeSet<SymbolId>,
    confirmed_native_stops:
        std::collections::BTreeMap<SymbolId, engine_types::order_terms::ExactStopTerms>,
    confirmed_stop_moves: std::collections::BTreeMap<SymbolId, reconcile::IntendedPositionStop>,
    /// Everything the venue traded before this wall time is in the log —
    /// delivered by the stream or recovered from the venue's history.
    /// Advanced only when a recovery pass completes, and it is where the
    /// next gap recovery starts reading.
    recovered_until_ms: i64,
    /// Next wall time at which a quiet run renews the durable history proof.
    next_history_checkpoint_ms: i64,
    /// Venue execution ids inside the history window, so overlapping
    /// recovery cannot write the same fill twice.
    recovered_exec_ids: ExecutionIds,
    /// Recently journaled delivered fills (order id, venue stamp, qty) —
    /// the other half of that dedup: a fill the stream DID deliver near a
    /// gap's edge must not come back as recovered.
    recent_fills: std::collections::VecDeque<(String, i64, f64)>,
    group_flush: Duration,
    strategy_barrier_pending: bool,
    strategy_runtime_retirements: BTreeSet<StrategyId>,
    refresh_after_ns: u64,
    account_refresh_requested_after: Option<u64>,
    account_refresh_started_ns: u64,
    /// Rotate the log once the current segment passes this, checked on the
    /// group-flush tick. Zero means never.
    rotate_after_bytes: u64,
    /// Refuse entries decided against a quote older than this. Exits flow.
    max_quote_age_ns: u64,
    next_order_n: u64,
    order_id_epoch_ms: i64,
    orders_sent: u64,
    /// Counted for the whole run. The ledger's own count clears every minute.
    events_seen: u64,
    subscriptions: Vec<Subscription>,
    portfolio_subscriptions: Vec<Subscription>,
}

mod account_recovery;
mod boot_recovery;
mod execution_controls;
mod history_recovery;
mod intent_admission;
mod order_dispatch;
mod order_lineage;
#[cfg(test)]
mod physical_exposure_tests;
mod portfolio_routes;
#[cfg(test)]
mod portfolio_routes_tests;
#[cfg(test)]
mod refusal_code_tests;
mod scheduling;
mod signal_intake;
mod signal_routes;
pub(crate) mod stop_runtime;
mod strategy_callbacks;
mod strategy_effects;
pub(crate) mod symbol_admission;
mod telemetry;
mod venue_completion;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub fn subscriptions(&self) -> &[Subscription] {
        &self.subscriptions
    }

    /// Say how this engine is, in a file. Optional: with no heartbeat the
    /// engine writes nothing about itself and nothing outside the process can
    /// tell whether it is well.
    pub fn write_trades(&mut self, trades: Trades) {
        self.trades = Some(trades);
    }

    pub fn write_heartbeat(&mut self, heartbeat: Heartbeat) {
        self.heartbeat = Some(heartbeat);
    }

    pub fn in_flight_ids(&self) -> Vec<&str> {
        self.books.orders.in_flight_ids()
    }

    pub fn ledger(&self) -> &LatencyLedger {
        &self.ledger
    }

    pub fn market(&self) -> &MarketState {
        &self.books.market
    }

    pub fn account(&self) -> &AccountView {
        &self.books.account
    }

    /// Every sleeve reporting a health error, or a latched callback fault, by
    /// configured name — the heartbeat's own reading, for a stopped engine.
    pub fn strategy_health(&self) -> Vec<(String, String)> {
        named_strategy_errors(
            &self.host.strategies,
            &self.host.names,
            &self.host.callbacks.faults,
        )
    }

    /// Run until shutdown resolves or the market feed closes.
    pub async fn run<M, O, S>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
        shutdown: S,
    ) -> Result<RunOutcome, EngineError>
    where
        M: MarketFeed,
        O: OrderFeed,
        S: Future<Output = ()>,
    {
        let mut signals = crate::signals::NoSignals;
        let mut controls = crate::controls::NoControls;
        self.run_with_inputs(
            market_feed,
            order_feed,
            &mut signals,
            &mut controls,
            shutdown,
        )
        .await
    }

    /// Run with a lossless credential-free signal source beside the market and
    /// private-order feeds. Signal filesystem/network work belongs to the feed
    /// task; the core sees one already-normalized envelope at a time.
    pub async fn run_with_signals<M, O, F, S>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
        signal_feed: &mut F,
        shutdown: S,
    ) -> Result<RunOutcome, EngineError>
    where
        M: MarketFeed,
        O: OrderFeed,
        F: SignalFeed,
        S: Future<Output = ()>,
    {
        let mut controls = crate::controls::NoControls;
        self.run_with_inputs(
            market_feed,
            order_feed,
            signal_feed,
            &mut controls,
            shutdown,
        )
        .await
    }

    /// Run with both durable external signals and live operator commands.
    pub async fn run_with_inputs<M, O, F, C, S>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
        signal_feed: &mut F,
        control_feed: &mut C,
        shutdown: S,
    ) -> Result<RunOutcome, EngineError>
    where
        M: MarketFeed,
        O: OrderFeed,
        F: SignalFeed,
        C: RuntimeControlFeed,
        S: Future<Output = ()>,
    {
        self.run_with_inputs_on(
            market_feed,
            order_feed,
            signal_feed,
            control_feed,
            shutdown,
            SystemTimer,
        )
        .await
    }

    /// `run_with_inputs` with the loop's timers supplied by the caller. The
    /// live runner never calls this; the replay driver does, with a clock it
    /// advances from the tape.
    pub async fn run_with_inputs_on<M, O, F, C, S, T>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
        signal_feed: &mut F,
        control_feed: &mut C,
        shutdown: S,
        timer: T,
    ) -> Result<RunOutcome, EngineError>
    where
        M: MarketFeed,
        O: OrderFeed,
        F: SignalFeed,
        C: RuntimeControlFeed,
        S: Future<Output = ()>,
        T: LoopTimer,
    {
        for (symbol, spec) in &self.instrument_specs {
            order_feed.learn_instrument(*symbol, spec);
        }
        tokio::pin!(shutdown);
        let mut flush_tick = timer.interval(self.group_flush);
        let mut signals_open = true;
        let mut controls_open = true;
        let mut ordinary_lane = OrdinaryLane::Tick;
        if self.signals.readiness_required() {
            if self.identities.scope.is_some() {
                signal_feed
                    .set_sleeve_keys(self.identities.sleeves.clone())
                    .map_err(|error| EngineError::State(error.to_string()))?;
            }
            self.signals.begin_readiness_request();
            signal_feed
                .request_lifecycle(
                    self.signals.producers().cloned().collect(),
                    self.signals.lifecycle_legacy_sources(),
                )
                .map_err(|error| EngineError::State(error.to_string()))?;
        }
        self.update_signal_requests(signal_feed)?;

        // Boot-restored cross-sleeve events and external observations were
        // delivered into this FIFO only after all checkpoints were restored.
        self.drain(clock::now_ns()).await?;

        let stopped_by = loop {
            let timer_deadline = self.host.next_timer_deadline();
            let timer_wait = timer_deadline
                .map(|deadline| Duration::from_nanos(deadline.saturating_sub(clock::now_ns())));

            let halt_confirmation_pending = self.halt_cancels.values().any(|state| {
                matches!(
                    state,
                    HaltCancelState::AwaitingPrivate { .. } | HaltCancelState::Resolving { .. }
                )
            });
            let halt_wake = self
                .next_halt_wake_ns(clock::now_ns())
                .map(|at| Duration::from_nanos(at.saturating_sub(clock::now_ns())));
            let halt_mode = !self.halt_cancel_queue.is_empty() || halt_confirmation_pending;
            let drain_mode = !halt_mode && self.drain_progress.is_some();
            let strategy_sleep =
                (!drain_mode).then(|| timer.sleep(timer_wait.unwrap_or(Duration::MAX)));
            let (halt_sleep, ordinary_sleep) = if halt_mode {
                (strategy_sleep, None)
            } else {
                (None, strategy_sleep)
            };
            let halt_deadline = halt_mode.then(|| timer.sleep(halt_wake.unwrap_or(Duration::MAX)));
            let (halt_tick, ordinary_tick) = if halt_mode {
                (Some(&mut flush_tick), None)
            } else {
                (None, Some(&mut flush_tick))
            };
            let signals_enabled = signals_open && self.pending_signal_deliveries.is_empty();
            tokio::select! {
                biased;
                _ = &mut shutdown, if self.drain_progress.is_none() => break StopReason::Shutdown,
                update = order_feed.next_update(), if !drain_mode && !self.order_lineage.waiting() => {
                    if let Turn::Stop(reason) = self.on_order_feed(update, true, &timer).await? {
                        break reason;
                    }
                }
                lineage = self.order_lineage.completed.recv(), if self.order_lineage.running() => {
                    self.on_order_lineage(lineage.ok_or_else(|| EngineError::State("order lineage reader stopped".into()))?).await?;
                }
                recovery = self.recovery.completed.recv(), if self.recovery.waiting() => {
                    self.on_recovery_completion(recovery.ok_or_else(|| EngineError::State("recovery task stopped".into()))?).await?;
                }
                _ = std::future::ready(()), if self.recovery.applying() && !self.order_lineage.waiting() => {
                    self.service_account_recovery().await?;
                }
                lookup = self.dispatches.lookups.recv(), if !self.dispatches.lookup_pending.is_empty() => {
                    if let Some((id, result)) = lookup { self.on_order_lookup(id, result).await?; }
                }
                dispatch = self.dispatches.durable.recv(), if self.dispatches.write.is_some() => {
                    self.on_order_dispatch_durable(dispatch).await?;
                }

                completion = self.venue_completions.recv(), if !self.pending_mutations.is_empty() => {
                    if drain_mode {
                        let completion = completion.ok_or(EngineError::TaskStopped { task: EngineTask::Venue, detail: "with mutations outstanding" })?;
                        self.take_venue_completion(completion).await?;
                    } else {
                        self.on_completion(completion, order_feed).await?;
                    }
                }
                // Halt deadlines follow ready private updates and precede new market input.
                _ = async { if let Some(tick) = halt_tick { tick.tick().await; } }, if halt_mode => self.on_tick().await?,
                _ = async { if let Some(sleep) = halt_sleep { sleep.await; } }, if halt_mode && timer_wait.is_some() => {
                    self.on_timers().await?;
                }
                _ = std::future::ready(()), if halt_mode && !self.halt_cancel_queue.is_empty() => {
                    self.dispatch_halt_cancel_group().await?;
                }
                _ = std::future::ready(()), if halt_mode && self.halt_lookup_due(clock::now_ns()) => {
                    self.start_halt_lookup(clock::now_ns())?;
                }
                _ = async { if let Some(sleep) = halt_deadline { sleep.await; } }, if halt_mode && halt_wake.is_some() => {
                    self.queue_halted_entry_cancels()?;
                }
                _ = std::future::ready(()), if halt_mode && self.drain_progress.is_some() => {
                    self.drain(clock::now_ns()).await?;
                }
                _ = std::future::ready(()), if drain_mode => {}
                ordinary = async {
                    let market = market_feed.next_event();
                    let signal = signal_feed.next_event();
                    let control = control_feed.next_request();
                    let tick = async {
                        if let Some(tick) = ordinary_tick {
                            tick.tick().await;
                        } else {
                            std::future::pending().await
                        }
                    };
                    let sleep = async {
                        if let Some(sleep) = ordinary_sleep {
                            sleep.await;
                        } else {
                            std::future::pending().await
                        }
                    };
                    tokio::pin!(market, signal, control, tick, sleep);
                    std::future::poll_fn(|cx| {
                        let mut lane = ordinary_lane;
                        for _ in 0..5 {
                            match lane {
                                OrdinaryLane::Tick if !halt_mode => {
                                    if tick.as_mut().poll(cx).is_ready() {
                                        return Poll::Ready(OrdinaryInput::Tick);
                                    }
                                }
                                OrdinaryLane::Timer if !halt_mode => {
                                    if timer_deadline.is_some_and(|at| at <= clock::now_ns())
                                        || (timer_wait.is_some() && sleep.as_mut().poll(cx).is_ready())
                                    {
                                        return Poll::Ready(OrdinaryInput::Timer);
                                    }
                                }
                                OrdinaryLane::Control if controls_open => {
                                    if let Poll::Ready(request) = control.as_mut().poll(cx) {
                                        return Poll::Ready(OrdinaryInput::Control(request));
                                    }
                                }
                                OrdinaryLane::Signal if signals_enabled => {
                                    if let Poll::Ready(event) = signal.as_mut().poll(cx) {
                                        return Poll::Ready(OrdinaryInput::Signal(event));
                                    }
                                }
                                OrdinaryLane::Market => {
                                    if let Poll::Ready(event) = market.as_mut().poll(cx) {
                                        return Poll::Ready(OrdinaryInput::Market(event));
                                    }
                                }
                                _ => {}
                            }
                            lane = lane.next();
                        }
                        Poll::Pending
                    }).await
                }, if !drain_mode => {
                    ordinary_lane = ordinary.lane().next();
                    match ordinary {
                        OrdinaryInput::Market(event) => {
                            if let Turn::Stop(reason) = self.on_market_feed(&event, order_feed, &timer).await? {
                                break reason;
                            }
                        }
                        OrdinaryInput::Signal(observation) => {
                            self.on_signal_feed(observation, signal_feed, &mut signals_open)?;
                        }
                        OrdinaryInput::Control(request) => {
                            self.on_control_feed(request, control_feed, &mut controls_open).await?;
                        }
                        OrdinaryInput::Tick => self.on_tick().await?,
                        OrdinaryInput::Timer => self.on_timers().await?,
                    }
                }
            }
            if drain_mode {
                // A completed venue mutation is the cooperative boundary.
                // Poll one private update without waiting, then independently
                // refresh a stale account view, and only then resume the wake.
                // Keeping those phases separate means a ready private update
                // cannot hide a simultaneously due account refresh.
                let private_update = if self.order_lineage.waiting() {
                    None
                } else {
                    let update = order_feed.next_update();
                    tokio::pin!(update);
                    std::future::poll_fn(|cx| {
                        Poll::Ready(match update.as_mut().poll(cx) {
                            Poll::Ready(update) => Some(update),
                            Poll::Pending => None,
                        })
                    })
                    .await
                };
                if let Some(update) = private_update {
                    match self.on_order_feed(update, false, &timer).await? {
                        Turn::Stop(reason) => break reason,
                        Turn::Hiccup => {
                            self.after_turn(market_feed, order_feed, signal_feed)
                                .await?;
                            continue;
                        }
                        Turn::Continue => {}
                    }
                }

                // Do not infer account freshness from timer readiness. An
                // overdue interval polled for the first time can register
                // with Tokio's timer driver and return Pending for this turn;
                // an immediate drain would then skip a refresh the monotonic
                // account stamp already says is due.
                let now = clock::now_ns();
                self.refresh_account_if_due(now).await?;
                self.queue_halted_entry_cancels()?;

                // Preserve the ordinary group-tick work when its timer is
                // already ready. Both branches call `drain` exactly once.
                let tick_ready = {
                    let tick = flush_tick.tick();
                    tokio::pin!(tick);
                    std::future::poll_fn(|cx| Poll::Ready(tick.as_mut().poll(cx).is_ready())).await
                };
                if tick_ready {
                    self.on_tick().await?;
                } else {
                    self.drain(now).await?;
                }
            }

            // Outside the select!, where the feeds are borrowable again.
            self.after_turn(market_feed, order_feed, signal_feed)
                .await?;
        };

        self.finish().await?;
        Ok(RunOutcome {
            stopped_by,
            market_events: self.events_seen,
            orders_sent: self.orders_sent,
        })
    }

    /// One private-stream result. `drain_after` says whether the actions the
    /// update released are drained at once or left for the caller's own
    /// drain step.
    /// One private-feed result. The pause after a hiccup is on the loop's
    /// timer, not the wall clock: under a virtual clock a wall-clock pause is
    /// a hole the simulated world runs through unobserved.
    async fn on_order_feed<T: LoopTimer>(
        &mut self,
        update: Result<OrderUpdate, FeedError>,
        drain_after: bool,
        timer: &T,
    ) -> Result<Turn, EngineError> {
        match update {
            Ok(update) => {
                let now = clock::now_ns();
                self.take_update(update).await?;
                if drain_after {
                    self.drain(now).await?;
                }
                Ok(Turn::Continue)
            }
            Err(FeedError::Closed) => {
                tracing::error!("order feed closed; stopping for supervised recovery");
                Ok(Turn::Stop(StopReason::FeedClosed))
            }
            Err(e) => {
                self.invalidate_private_stream()?;
                tracing::warn!(error = %e, "order feed hiccup");
                timer.sleep(HICCUP_PAUSE).await;
                Ok(Turn::Hiccup)
            }
        }
    }

    /// One public-feed result. A feed that errors without closing is
    /// expected to be reconnecting inside; the pause keeps a broken one from
    /// spinning the loop.
    async fn on_market_feed<O: OrderFeed, T: LoopTimer>(
        &mut self,
        event: &Result<MarketEvent, FeedError>,
        order_feed: &mut O,
        timer: &T,
    ) -> Result<Turn, EngineError> {
        match event {
            Ok(event) => {
                self.on_market(event).await?;
                Ok(Turn::Continue)
            }
            Err(FeedError::Closed) => {
                self.settle_after_market_close(order_feed).await?;
                Ok(Turn::Stop(StopReason::FeedClosed))
            }
            Err(e) => {
                tracing::warn!(error = %e, "market feed hiccup");
                timer.sleep(HICCUP_PAUSE).await;
                Ok(Turn::Continue)
            }
        }
    }

    /// The venue task answered a mutation. A closed channel with mutations
    /// still outstanding is the task having died mid-flight.
    async fn on_completion<O: OrderFeed>(
        &mut self,
        completion: Option<MutationCompletion>,
        order_feed: &mut O,
    ) -> Result<(), EngineError> {
        let completion = completion.ok_or(EngineError::TaskStopped {
            task: EngineTask::Venue,
            detail: "with mutations still outstanding",
        })?;
        self.take_completion_turn(completion, order_feed).await
    }

    fn on_signal_feed<F: SignalFeed>(
        &mut self,
        observation: Result<engine_types::SignalFeedEvent, SignalError>,
        signal_feed: &mut F,
        signals_open: &mut bool,
    ) -> Result<(), EngineError> {
        match observation {
            Ok(engine_types::SignalFeedEvent::Observation(observation)) => {
                self.queue_signal_observation(observation, signal_feed)
            }
            Ok(engine_types::SignalFeedEvent::Ready(frontiers)) => {
                self.accept_signal_frontiers(frontiers, signal_feed)
            }
            Ok(engine_types::SignalFeedEvent::LifecycleReady(response)) => {
                self.accept_signal_lifecycle(response, signal_feed)
            }
            Ok(engine_types::SignalFeedEvent::ReadinessUnavailable { reason }) => {
                self.refuse_signal_readiness(&reason, signal_feed)
            }
            Err(SignalError::Closed) => {
                *signals_open = false;
                self.signals.clear_readiness();
                self.queue_halted_entry_cancels()?;
                Ok(())
            }
            Err(error) => Err(EngineError::State(error.to_string())),
        }
    }

    /// A refused request is retired, never fatal: the spool is durable, so a
    /// request the engine will never accept would otherwise poison every
    /// restart.
    async fn on_control_feed<C: RuntimeControlFeed>(
        &mut self,
        request: Result<RuntimeControlRequest, RuntimeControlError>,
        control_feed: &mut C,
        controls_open: &mut bool,
    ) -> Result<(), EngineError> {
        match request {
            Ok(request) => match self.admit_runtime_control(&request) {
                Err(refusal) => {
                    tracing::error!(
                        request_id = %request.request_id,
                        strategy = %request.strategy_name,
                        refusal,
                        "refusing durable runtime control request"
                    );
                    control_feed
                        .reject_last()
                        .await
                        .map_err(|error| EngineError::State(error.to_string()))
                }
                Ok(fresh) => {
                    if fresh {
                        self.apply_runtime_control(request)?;
                    }
                    self.drain(clock::now_ns()).await
                }
            },
            Err(RuntimeControlError::Closed) => {
                *controls_open = false;
                Ok(())
            }
            Err(error) => Err(EngineError::State(error.to_string())),
        }
    }

    /// What every turn does once the feeds are borrowable again: follow the
    /// symbols a durable signal named, then deliver the signals that were
    /// waiting on them.
    async fn after_turn<M, O, F>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
        signal_feed: &mut F,
    ) -> Result<(), EngineError>
    where
        M: MarketFeed,
        O: OrderFeed,
        F: SignalFeed,
    {
        self.service_order_lineage().await?;
        self.service_account_recovery().await?;
        self.trim_order_lineage_cache()?;
        if !self.wanted_symbols.is_empty() || self.symbol_admission.busy() {
            self.admit_wanted(market_feed, order_feed).await?;
        }
        if !self.pending_signal_deliveries.is_empty() {
            self.accept_pending_signals(signal_feed)?;
            self.drain(clock::now_ns()).await?;
        }
        self.deliver_pending_signal_callbacks();
        self.maintain_signal_routes(market_feed)?;
        self.advance_signal_lifecycles(signal_feed)?;
        self.update_signal_requests(signal_feed)?;
        Ok(())
    }

    /// Last ledger line on the way out, and the whole tail forced to disk:
    /// a graceful stop that leaves its closing updates in the page cache
    /// tells the next boot's audit a lie.
    pub async fn finish(&mut self) -> Result<(), EngineError> {
        self.recovery.stop_read();
        while self.recovery.uncommitted() || self.order_lineage.waiting() {
            if self.order_lineage.waiting() {
                self.settle_order_lineage().await?;
                continue;
            }
            if self.recovery.applying() {
                let account_recovery::Phase::Applying(batch) =
                    std::mem::replace(&mut self.recovery.phase, account_recovery::Phase::Idle)
                else {
                    unreachable!()
                };
                self.apply_history_batch(*batch)?;
            } else {
                let completion =
                    tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.recovery.completed.recv())
                        .await
                        .map_err(|_| {
                            EngineError::State(
                                "graceful stop timed out settling execution history".into(),
                            )
                        })?
                        .ok_or_else(|| EngineError::State("recovery task stopped".into()))?;
                self.on_recovery_completion(completion).await?;
            }
        }
        while self.dispatches.write.is_some()
            || !self.pending_mutations.is_empty()
            || !self.ready_actions.is_empty()
        {
            if self.dispatches.write.is_some() {
                let result =
                    tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.dispatches.durable.recv())
                        .await
                        .map_err(|_| {
                            EngineError::TimedOut(
                                "order dispatch durability during graceful stop".into(),
                            )
                        })?;
                self.on_order_dispatch_durable(result).await?;
                self.drain(clock::now_ns()).await?;
                continue;
            }
            if self.pending_mutations.is_empty() {
                self.drain(clock::now_ns()).await?;
                continue;
            }
            let completion =
                tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.venue_completions.recv())
                    .await
                    .map_err(|_| {
                        EngineError::TimedOut(format!(
                            "{} venue mutations during graceful stop",
                            self.pending_mutations.len()
                        ))
                    })?
                    .ok_or(EngineError::TaskStopped {
                        task: EngineTask::Venue,
                        detail: "during graceful mutation drain",
                    })?;
            self.take_venue_completion(completion).await?;
            self.drain(clock::now_ns()).await?;
        }
        if !self.deferred_actions.is_empty() || !self.ready_actions.is_empty() {
            return Err(EngineError::State(
                "graceful stop found deferred actions without a live venue mutation".to_string(),
            ));
        }
        // A trip that closed since the last tick is still a closed trip.
        self.record_trades();
        let now = clock::now_ns();
        let record = self.ledger.record_for_wal(now);
        self.wal.append(&record)?;
        self.wal.barrier()?;
        tracing::info!("latency, {}", self.ledger.plain_line(now));
        Ok(())
    }

    /// Take a fresh account reading.
    ///
    /// The one place a reading is adopted, so what has to happen with it
    /// cannot be done in one path and forgotten in the other.
    fn adopt_view(&mut self, view: AccountView) {
        self.risk.observe_account_view(&view);
        // The wall clock, not the engine's monotonic one: the rolling loss
        // window is stamped in the venue's milliseconds and has to age even
        // when nothing closes.
        self.risk.observe_wall_clock_ms(clock::wall_ms());
        self.confirmed_stop_moves.clear();
        self.confirmed_native_stops.clear();
        match self.leverage_authority {
            crate::config::LeverageAuthority::Shared => {
                forget_leverage_where_flat(&mut self.leverage_at, &view.positions)
            }
            // Sole authority keeps the cache across flat spells and verifies
            // the held positions instead: the venue's own row says what
            // leverage a position actually runs at, and that answer beats a
            // pre-send confirmation — it is measured on the position itself,
            // after every race a confirm could lose.
            crate::config::LeverageAuthority::Sole => {
                self.verify_leverage_against_view(&view.positions)
            }
        }
        self.books.account = view;
        // A cover the fresh reading has caught up with is released, so the
        // strategies woken after this read one truthful in-flight number.
        self.books.covers.absorb(&self.books.account);
    }

    /// Stop trusting the account snapshot as soon as the private stream says
    /// continuity is gone. `observed_ns = 0` also makes the risk kernel's
    /// ordinary freshness check fail closed; the explicit readiness bit keeps
    /// a periodic REST refresh from re-enabling entries before execution
    /// history has closed the stream gap.
    fn invalidate_private_stream(&mut self) -> Result<(), EngineError> {
        self.clear_private_stream_ready();
        self.recovery.disconnected();
        self.books.account.observed_ns = 0;
        self.queue_halted_entry_cancels()
    }

    /// Both readiness transitions live here so no call site can move the bit
    /// without the stamp. Clearing while already unready keeps the original
    /// stamp: a stream that resets again before it recovers has not started
    /// a fresh outage, and the age must keep running against the first loss.
    fn clear_private_stream_ready(&mut self) {
        if self.private_stream_ready {
            self.supersede_openings();
        }
        self.private_stream_ready = false;
        self.private_stream_unready_since_ns
            .get_or_insert_with(clock::now_ns);
    }

    /// Retire every opening still waiting in the venue queue: the permission
    /// it was admitted under has gone away, and a cancel after the fact is
    /// not the same thing as never sending it.
    fn supersede_openings(&mut self) -> u64 {
        self.authority.advance()
    }

    /// The authority a command queued now carries.
    fn mint_authority(&self) -> engine_types::CommandAuthority {
        let queued_ns = clock::now_ns();
        engine_types::CommandAuthority {
            epoch: self.authority.current(),
            queued_ns,
            expires_at_ns: queued_ns.saturating_add(self.opening_dispatch_ttl_ns),
        }
    }

    /// Boot's comparison against the venue, or a live control, says this
    /// engine may no longer add exposure. The latch is written into the log
    /// by the caller; what happens here is the queued openings.
    fn latch_closed(&mut self) {
        if self.may_open {
            self.supersede_openings();
        }
        self.may_open = false;
    }

    fn restore_private_stream_ready(&mut self) {
        self.private_stream_ready = true;
        self.private_stream_unready_since_ns = None;
    }

    fn is_live_halt_order(&self, client_order_id: &str) -> bool {
        self.books
            .orders
            .orders
            .get(client_order_id)
            .is_some_and(|order| order.in_flight())
    }

    /// Under sole leverage authority: hold what we set against what the venue
    /// says each held position actually runs at. A mismatch means somebody
    /// else wrote leverage on an account we believed only we write — say so
    /// loudly and evict the trust, which makes the next entry in that symbol
    /// confirm with the venue before the next order can be dispatched.
    fn verify_leverage_against_view(&mut self, positions: &[engine_types::risk::PositionView]) {
        for position in positions {
            let (Some(venue_says), Some(we_set)) = (
                position.leverage,
                self.leverage_at.get(&position.symbol).copied(),
            ) else {
                continue;
            };
            if (venue_says - we_set).abs() > 1e-9 {
                tracing::error!(
                    symbol = self.books.market.table.name(position.symbol),
                    we_set,
                    venue_says,
                    "a held position's leverage is not what this engine set — \
                     sole leverage authority looks wrong on this account; \
                     re-confirming before the next entry"
                );
                let _ = self.wal.append(&WalRecord::Note {
                    source: "leverage-authority".to_string(),
                    text: format!(
                        "position {} runs at {venue_says}x, engine set {we_set}x; \
                         trust evicted, next entry re-confirms",
                        self.books.market.table.name(position.symbol)
                    ),
                });
                self.leverage_at.remove(&position.symbol);
            }
        }
    }

    fn validate_leverage_request(&self, want: f64) -> Result<(), String> {
        if !want.is_finite() || want <= 0.0 {
            return Err(format!(
                "the decision asks for leverage {want}, which is not a leverage"
            ));
        }
        if !self.venue.caps().set_leverage {
            return Err(format!(
                "this decision was sized at leverage {want}, and this venue cannot be told                  what leverage to use — the margin it would post is not the margin it was                  sized at"
            ));
        }
        Ok(())
    }

    /// Everything a fresh log segment must restate: the state boot rebuilds
    /// from the log, as this engine holds it right now.
    ///
    /// Each field is maintained by the same arithmetic the boot-time scan
    /// for it uses — the order ledger and attribution apply every record as
    /// it is written, the exposure and stop maps go through `reconcile`'s
    /// own helpers, and recent execution ids come from the same bounded dedup
    /// set used live — so
    /// replaying the old segments and replaying this record recover the same
    /// engine. The equivalence test in `tests/rotation.rs` holds the two sides
    /// together.
    ///
    /// Deliberately NOT restated, because boot does not rebuild them either:
    /// covers and working-order supervision (boot starts them empty and
    /// trusts the venue comparison instead), and the run's own latency ledger
    /// and cost score.
    pub(crate) fn rotation_base(&self, wall_ts_ms: i64) -> WalRecord {
        WalRecord::SegmentBase {
            order_id_epoch_ms: Some(self.order_id_epoch_ms),
            open_trade_lots: Some(self.fills.open_trade_lots()),
            legacy_signal_source_retirements: self
                .signals
                .legacy_source_retirements()
                .cloned()
                .collect(),
            portfolio_control: self.portfolio_controls.snapshot(),
            strategy_processes: self
                .host
                .callbacks
                .state
                .committed
                .values()
                .cloned()
                .collect(),
            strategy_callback_queues: self.host.callbacks.pages.slots.values().cloned().collect(),
            strategy_callback_sources: self.host.callbacks.order_news.snapshot(),
            signal_callback_deliveries: self.signals.callback_deliveries(),
            strategy_callbacks: if self.host.callbacks.pages.enabled() {
                Vec::new()
            } else {
                self.host.callbacks.state.inputs.values().cloned().collect()
            },
            portfolio: Some(self.books.attribution.snapshot()),
            wall_ts_ms,
            strategies: self.host.names.clone(),
            symbols: (0..self.books.market.table.len())
                .map(|i| self.books.market.table.name(SymbolId(i as u16)).to_string())
                .collect(),
            may_open: self.may_open,
            // Older WALs may contain anchors from the retired daily-loss
            // feature. Reading remains compatible; rotation scrubs them.
            control_anchors: Vec::new(),
            attribution: self
                .books
                .attribution
                .rows()
                .into_iter()
                .map(|(strategy, symbol, signed_qty)| engine_types::FilledTotal {
                    strategy,
                    symbol,
                    signed_qty,
                })
                .collect(),
            logged_exposure: crate::reconcile::snapshot_exposure(&self.logged_exposure),
            intended_stops: self
                .intended_stops
                .iter()
                .map(|(symbol, stop)| engine_types::IntendedStop {
                    symbol: *symbol,
                    side: Some(stop.side),
                    trigger_px: stop.trigger_px,
                })
                .collect(),
            recent_execution_ids: self.recovered_exec_ids.rows(wall_ts_ms),
            execution_history_through_ms: Some(self.recovered_until_ms),
            target_book_latches: Vec::new(),
            strategy_checkpoints: self
                .host
                .checkpoints
                .iter()
                .map(
                    |((strategy, symbol), checkpoint)| engine_types::StrategyCheckpointState {
                        strategy: *strategy,
                        symbol: *symbol,
                        checkpoint: checkpoint.clone(),
                    },
                )
                .collect(),
            strategy_global_checkpoints: self.host.global_checkpoints.values().cloned().collect(),
            strategy_events: self.host.events.values().cloned().collect(),
            signal_observations: self.signals.observations().cloned().collect(),
            signal_cursors: self.signals.cursors().cloned().collect(),
            signal_subscriptions: self.signals.subscriptions().cloned().collect(),
            signal_gaps: self.signals.gaps().cloned().collect(),
            pending_order_dispatches: self
                .dispatches
                .orders
                .values()
                .map(|order| order.state.clone())
                .collect(),
            signal_producers: self.signals.producers().cloned().collect(),
            identities: Some(self.identities.clone()),
            instrument_catalog: self.symbol_admission.checkpoint.clone(),
            signal_suspensions: self.signals.suspensions().collect(),
            strategy_effects: self.host.effects.snapshot(),
            runtime_control_requests: self.runtime_control_requests.clone(),
            runtime_control_consumed: self.runtime_control_consumed.iter().cloned().collect(),
            open_orders: self
                .books
                .orders
                .orders
                .values()
                .filter(|order| order.retain_at(wall_ts_ms, self.recovered_until_ms))
                .map(|order| order.snapshot(wall_ts_ms))
                .collect(),
            rolling_loss_rows: self.risk.rolling_loss_rows(),
            owed_markouts: self.fills.owed_markouts(),
        }
    }
}
