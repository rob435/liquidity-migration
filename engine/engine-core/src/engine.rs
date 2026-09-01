//! The loop.
//!
//! One process and one deterministic state owner. Market data, strategy
//! decisions, risk and the durable ledger stay on the current-thread runtime.
//! A bounded actor owns venue I/O, so a slow API round trip cannot stop market
//! events, private fills or timers. Every queue and resume boundary is stamped
//! into the latency ledger.
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
//! The order of the intent pipeline is the part that a crash reads back:
//! intent recorded, verdict recorded, order recorded **and handed to the
//! operating system**, and only then the bytes leave. The disk barrier starts
//! at the same moment and runs beside the flight to the venue; what waits for
//! it is the first news that the order traded, never the send. A crash between
//! the send and the reply leaves an order the log knows about and no reply for
//! it, which is exactly what `engine replay` shows as in flight.
//!
//! What that trades: a machine that dies inside the barrier — not a process
//! that dies, whose bytes are already with the operating system — can leave an
//! order at the venue the log does not name. Reconciliation reads that as an
//! order it cannot account for and latches opening off.

use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::time::Duration;

use engine_types::{
    quantize, AccountView, Action, AmendSpec, DenyReason, EngineEvent, Feed, InstrumentRule,
    Intent, MarketEvent, MarketFeed, MarketState, OrderFeed, OrderKind, OrderRequest, OrderUpdate,
    RiskKernel, RiskVerdict, RuntimeControlFeed, Side, SignalCursor, SignalFeed, SignalObservation,
    SignalSubscriptionState, StopSpec, Strategy, StrategyCheckpoint, StrategyEvent,
    StrategyGlobalCheckpointState, StrategyId, Subscription, SymbolId, SymbolTable, TimeInForce,
    VenueError, VenueGateway, Wal, WalError, WalRecord, WorkPolicy,
};

use crate::attribution::{self, Attribution};
use crate::clock;
use crate::config::EngineSection;
use crate::covers::CoverBook;
use crate::ctx::{Ctx, Timers};
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

include!("engine/free_helpers.inc.rs");

pub const ENGINE_VERSION: &str = concat!("engine-core ", env!("CARGO_PKG_VERSION"));

/// How many recently journaled fills to remember for gap-recovery dedup.
/// A gap plus its pads spans minutes; this covers hours of fills.
const RECENT_FILLS_KEPT: usize = 2048;

/// A quiet account renews its execution-history proof daily, well inside the
/// shortest supported venue history window.
const HISTORY_CHECKPOINT_INTERVAL_MS: i64 = 86_400_000;

struct RecoveryOutcome {
    records: Vec<WalRecord>,
    through_ms: i64,
}

#[derive(Debug)]
pub enum EngineError {
    Wal(WalError),
    Venue(VenueError),
    Boot(String),
    State(String),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::Wal(e) => write!(f, "log: {e}"),
            EngineError::Venue(e) => write!(f, "venue: {e}"),
            EngineError::Boot(m) => write!(f, "boot: {m}"),
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

#[derive(Debug)]
pub struct RunOutcome {
    pub stopped_by: StopReason,
    pub market_events: u64,
    pub orders_sent: u64,
}

struct PreparedOrder {
    request: OrderRequest,
    decided_ns: u64,
    origin_ns: u64,
}

enum PendingMutation {
    Orders {
        requests: Vec<OrderRequest>,
        timings: Vec<(u64, u64)>,
        queued_ns: u64,
    },
    Cancels {
        requests: Vec<(SymbolId, String)>,
        queued_ns: u64,
    },
    Amend {
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
        existing: crate::inflight::OrderRec,
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

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum HaltCancelState {
    Submitting,
    AwaitingPrivate { deadline_ns: u64 },
}

/// An amend the venue took, held until the private stream says what price it
/// left the order at. Everything needed to settle the reservation either way
/// is here, because the completion that opened it is long gone by then.
struct AwaitingAmend {
    symbol: SymbolId,
    existing: crate::inflight::OrderRec,
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

pub struct Engine<W: Wal, R: RiskKernel, V: VenueGateway> {
    pub wal: W,
    pub risk: R,
    venue: VenueClient,
    venue_completions: tokio::sync::mpsc::Receiver<MutationCompletion>,
    pending_mutations: HashMap<u64, PendingMutation>,
    busy_symbols: HashMap<SymbolId, usize>,
    /// The last refusal recorded for each strategy, symbol and tag. A
    /// strategy that re-proposes a doomed order on every quote refuses just
    /// the same; only the record of it is collapsed, so one stuck position
    /// cannot bury the log the fill and latency reports read.
    refusals: HashMap<(StrategyId, SymbolId, String), Refusal>,
    deferred_actions: HashMap<SymbolId, VecDeque<(Action, u64)>>,
    /// Actions released by a completed symbol mutation, retaining the market
    /// wake that produced each one. The per-wake flood budget and latency
    /// origin therefore survive a slow venue round trip.
    ready_actions: VecDeque<(Action, u64)>,
    _venue: std::marker::PhantomData<V>,
    strategies: Vec<Box<dyn Strategy>>,
    names: Vec<String>,
    market: MarketState,
    routing: Routing,
    rules: Vec<Option<InstrumentRule>>,
    timers: Timers,
    pending: VecDeque<Action>,
    /// Present only after a venue mutation has completed while the same
    /// strategy wake still has actions. The run loop polls the private stream
    /// and a due account-refresh tick before resuming it.
    drain_progress: Option<DrainProgress>,
    account: AccountView,
    registry: OrderRegistry,
    orders: LedgerOfOrders,
    /// Whose each position is. The account reading is per symbol and
    /// carries no strategy on it, so this is summed from the fills of the
    /// orders each strategy placed, and rebuilt from the log at boot.
    attribution: Attribution,
    /// Strategy-owned state, persisted before the action it guards and
    /// restated through rotation. The engine stores bytes, not meaning.
    strategy_checkpoints: std::collections::BTreeMap<(u16, u16), StrategyCheckpoint>,
    /// Whole-sleeve reducer state. Separate key space: no sentinel symbol can
    /// collide with a venue name admitted later.
    strategy_global_checkpoints: std::collections::BTreeMap<u16, StrategyGlobalCheckpointState>,
    /// Cross-sleeve events waiting for the addressed strategy to consume them.
    strategy_events: std::collections::BTreeMap<(u16, String), StrategyEvent>,
    /// External observations waiting for the addressed strategy to consume
    /// them, keyed by source and contiguous sequence.
    signal_observations: std::collections::BTreeMap<(String, u64), SignalObservation>,
    /// Highest contiguous signal sequence durably accepted per source.
    signal_cursors: std::collections::BTreeMap<String, SignalCursor>,
    /// Monotonic subscription union for each external source and destination.
    /// Consumption and later observations do not clear market-data needs.
    signal_subscriptions: std::collections::BTreeMap<(String, u16), SignalSubscriptionState>,
    /// Every accepted operator command, retained for request-id idempotence
    /// across WAL rotation.
    runtime_control_requests: Vec<engine_types::RuntimeControlRequest>,
    /// Replayable commands a reducer has durably completed.
    runtime_control_consumed: std::collections::BTreeSet<(u16, String)>,
    /// The newest durable runtime entry override per strategy.
    runtime_entries_enabled: std::collections::BTreeMap<u16, bool>,
    /// Validated observations held until every requested symbol/feed/rule is
    /// admitted. They are not delivered or cursor-advanced before then.
    pending_signal_deliveries: VecDeque<SignalObservation>,
    /// What each strategy has sent that the account reading has not yet
    /// absorbed, per (strategy, symbol). Booked at the send, released by
    /// rejects, cancels, refused exits, and the reading catching up; read by
    /// strategies as `ctx.in_flight`. The rules live in `covers.rs`.
    covers: CoverBook,
    /// The resting entries this engine is advancing. Empty unless a strategy
    /// asked for one to be worked.
    working: WorkingOrders,
    /// Opening orders already handed to cancellation after any durable
    /// opening halt. A successful REST acknowledgement is asynchronous, so the order
    /// remains in the ledger until the private stream ends it; this set keeps
    /// each refresh tick from submitting the same cancel again meanwhile.
    halt_cancels: std::collections::HashMap<String, HaltCancelState>,
    amends_awaiting_price: HashMap<String, AwaitingAmend>,
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
    /// A durability barrier the order path started and has not confirmed.
    ///
    /// The bytes are with the operating system; the disk has not said so yet.
    /// Held here because the thing that must wait for it is not the send —
    /// it is the first news that an order traded.
    pending_barrier: Option<engine_types::wal::PendingBarrier>,
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
    /// round trip (~172 ms measured on the box), and every held position's
    /// leverage is instead read back off the venue's own position rows — a
    /// mismatch alarms and evicts the trust, so the next entry confirms
    /// inline again.
    leverage_at: std::collections::HashMap<SymbolId, f64>,
    leverage_authority: crate::config::LeverageAuthority,
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
    /// Signed quantity per symbol over every fill this log ever held —
    /// strangers' included, because it mirrors the log's records, not the
    /// strategies. It is what reconcile compares the venue's positions
    /// against, seeded at boot by the same scan reconcile uses and kept
    /// live by the same arithmetic, so a rotation can restate it exactly.
    logged_exposure: std::collections::BTreeMap<u16, f64>,
    /// The stop belonging to each trusted filled position, kept live for the
    /// same reason: a stop the venue drops after a rotation must still be
    /// repairable at the level and direction the log proved. Unfilled
    /// opposite-side siblings never enter this map.
    intended_stops: std::collections::BTreeMap<u16, reconcile::IntendedPositionStop>,
    /// Stop moves the venue accepted since the latest account reading. This
    /// closes the short gap before that reading reflects the new stop without
    /// confusing a durable intent with a successful API call.
    confirmed_stop_moves: std::collections::BTreeMap<u16, reconcile::IntendedPositionStop>,
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
    refresh_after_ns: u64,
    /// Rotate the log once the current segment passes this, checked on the
    /// group-flush tick. Zero means never.
    rotate_after_bytes: u64,
    /// Refuse entries decided against a quote older than this. Exits flow.
    max_quote_age_ns: u64,
    next_order_n: u64,
    orders_sent: u64,
    /// Counted for the whole run. The ledger's own count clears every minute.
    events_seen: u64,
    subscriptions: Vec<Subscription>,
}

include!("engine/boot_recovery.inc.rs");
include!("engine/scheduling.inc.rs");
include!("engine/telemetry.inc.rs");
include!("engine/intent_admission.inc.rs");
include!("engine/venue_completion.inc.rs");

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
        self.orders.in_flight_ids()
    }

    pub fn ledger(&self) -> &LatencyLedger {
        &self.ledger
    }

    pub fn market(&self) -> &MarketState {
        &self.market
    }

    pub fn account(&self) -> &AccountView {
        &self.account
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
        tokio::pin!(shutdown);
        let mut flush_tick = tokio::time::interval(self.group_flush);
        flush_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut stopped_by = StopReason::Shutdown;
        let mut signals_open = true;
        let mut controls_open = true;

        // Boot-restored cross-sleeve events and external observations were
        // delivered into this FIFO only after all checkpoints were restored.
        if !self.pending.is_empty() {
            self.drain(clock::now_ns()).await?;
        }

        loop {
            let timer_wait = self
                .timers
                .next_deadline()
                .map(|deadline| Duration::from_nanos(deadline.saturating_sub(clock::now_ns())));

            let halt_confirmation_pending = self
                .halt_cancels
                .values()
                .any(|state| matches!(state, HaltCancelState::AwaitingPrivate { .. }));
            if !self.halt_cancel_queue.is_empty() || halt_confirmation_pending {
                // During a halt, consume every already-ready private update
                // before the next cancel group or a confirmation-deadline
                // tick. Keep this priority until the final accepted cancel
                // is terminal: if its update and deadline are ready together,
                // observing the update first avoids a false timeout.
                tokio::select! {
                    biased;
                    _ = &mut shutdown, if self.drain_progress.is_none() => break,
                    update = order_feed.next_update() => match update {
                        Ok(update) => {
                            let now = clock::now_ns();
                            self.take_update(update).await?;
                            self.drain(now).await?;
                        }
                        Err(engine_types::FeedError::Closed) => {
                            tracing::error!("order feed closed; stopping for supervised recovery");
                            stopped_by = StopReason::FeedClosed;
                            break;
                        }
                        Err(e) => {
                            self.invalidate_private_stream()?;
                            tracing::warn!(error = %e, "order feed hiccup");
                            tokio::time::sleep(Duration::from_millis(1)).await;
                        }
                    },
                    completion = self.venue_completions.recv(), if !self.pending_mutations.is_empty() => {
                        let completion = completion.ok_or_else(|| EngineError::State(
                            "venue task stopped with mutations still outstanding".to_string()
                        ))?;
                        self.take_completion_turn(completion, order_feed).await?;
                    },
                    _ = flush_tick.tick() => self.on_tick().await?,
                    _ = tokio::time::sleep(timer_wait.unwrap_or_default()), if timer_wait.is_some() => {
                        self.on_timers().await?;
                    },
                    _ = std::future::ready(()), if !self.halt_cancel_queue.is_empty() => {
                        self.dispatch_halt_cancel_group().await?;
                    }
                    _ = std::future::ready(()), if self.drain_progress.is_some() => {
                        self.drain(clock::now_ns()).await?;
                    }
                    event = market_feed.next_event() => match event {
                        Ok(event) => self.on_market(event).await?,
                        Err(engine_types::FeedError::Closed) => {
                            self.settle_after_market_close(order_feed).await?;
                            stopped_by = StopReason::FeedClosed;
                            break;
                        }
                        Err(e) => {
                            tracing::warn!(error = %e, "market feed hiccup");
                            tokio::time::sleep(Duration::from_millis(1)).await;
                        }
                    },
                    observation = signal_feed.next_observation(), if signals_open => match observation {
                        Ok(observation) => self.queue_signal_observation(observation)?,
                        Err(engine_types::SignalError::Closed) => signals_open = false,
                        Err(error) => return Err(EngineError::State(error.to_string())),
                    },
                    request = control_feed.next_request(), if controls_open => match request {
                        // A refused request is retired, never fatal: the spool
                        // is durable, so a request the engine will never
                        // accept would otherwise poison every restart.
                        Ok(request) => match self.admit_runtime_control(&request) {
                            Err(refusal) => {
                                tracing::error!(
                                    request_id = %request.request_id,
                                    strategy = %request.strategy_name,
                                    refusal,
                                    "refusing durable runtime control request"
                                );
                                control_feed.reject_last().await.map_err(|error| {
                                    EngineError::State(error.to_string())
                                })?;
                            }
                            Ok(fresh) => {
                                if fresh {
                                    self.apply_runtime_control(request)?;
                                }
                                self.drain(clock::now_ns()).await?;
                            }
                        },
                        Err(engine_types::RuntimeControlError::Closed) => controls_open = false,
                        Err(error) => return Err(EngineError::State(error.to_string())),
                    }
                }

                if !self.wanted_symbols.is_empty() {
                    self.admit_wanted(market_feed, order_feed).await?;
                }
                if !self.pending_signal_deliveries.is_empty() {
                    self.accept_pending_signals()?;
                    self.drain(clock::now_ns()).await?;
                }
                continue;
            }

            if self.drain_progress.is_some() {
                // A completed venue mutation is the cooperative boundary.
                // Poll one private update without waiting, then independently
                // refresh a stale account view, and only then resume the wake.
                // Keeping those phases separate means a ready private update
                // cannot hide a simultaneously due account refresh.
                let private_update = tokio::select! {
                    biased;
                    update = order_feed.next_update() => Some(update),
                    _ = std::future::ready(()) => None,
                };
                match private_update {
                    Some(Ok(update)) => self.take_update(update).await?,
                    Some(Err(engine_types::FeedError::Closed)) => {
                        tracing::error!("order feed closed; stopping for supervised recovery");
                        stopped_by = StopReason::FeedClosed;
                        break;
                    }
                    Some(Err(e)) => {
                        self.invalidate_private_stream()?;
                        tracing::warn!(error = %e, "order feed hiccup");
                        tokio::time::sleep(Duration::from_millis(1)).await;
                        if !self.wanted_symbols.is_empty() {
                            self.admit_wanted(market_feed, order_feed).await?;
                        }
                        if !self.pending_signal_deliveries.is_empty() {
                            self.accept_pending_signals()?;
                            self.drain(clock::now_ns()).await?;
                        }
                        continue;
                    }
                    None => {}
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
                tokio::select! {
                    biased;
                    _ = flush_tick.tick() => self.on_tick().await?,
                    _ = std::future::ready(()) => self.drain(now).await?,
                }

                if !self.wanted_symbols.is_empty() {
                    self.admit_wanted(market_feed, order_feed).await?;
                }
                if !self.pending_signal_deliveries.is_empty() {
                    self.accept_pending_signals()?;
                    self.drain(clock::now_ns()).await?;
                }
                continue;
            }

            tokio::select! {
                biased;
                _ = &mut shutdown => break,
                update = order_feed.next_update() => match update {
                    Ok(update) => {
                        let now = clock::now_ns();
                        self.take_update(update).await?;
                        self.drain(now).await?;
                    }
                    Err(engine_types::FeedError::Closed) => {
                        tracing::error!("order feed closed; stopping for supervised recovery");
                        stopped_by = StopReason::FeedClosed;
                        break;
                    }
                    Err(e) => {
                        self.invalidate_private_stream()?;
                        tracing::warn!(error = %e, "order feed hiccup");
                        tokio::time::sleep(Duration::from_millis(1)).await;
                    }
                },
                completion = self.venue_completions.recv(), if !self.pending_mutations.is_empty() => {
                    let completion = completion.ok_or_else(|| EngineError::State(
                        "venue task stopped with mutations still outstanding".to_string()
                    ))?;
                    self.take_completion_turn(completion, order_feed).await?;
                },
                event = market_feed.next_event() => match event {
                    Ok(event) => self.on_market(event).await?,
                    Err(engine_types::FeedError::Closed) => {
                        self.settle_after_market_close(order_feed).await?;
                        stopped_by = StopReason::FeedClosed;
                        break;
                    }
                    // A feed that errors without closing is expected to be
                    // reconnecting inside. Wait a moment so a broken one
                    // cannot spin the loop.
                    Err(e) => {
                        tracing::warn!(error = %e, "market feed hiccup");
                        tokio::time::sleep(Duration::from_millis(1)).await;
                    }
                },
                observation = signal_feed.next_observation(), if signals_open => match observation {
                    Ok(observation) => self.queue_signal_observation(observation)?,
                    Err(engine_types::SignalError::Closed) => signals_open = false,
                    Err(error) => return Err(EngineError::State(error.to_string())),
                },
                request = control_feed.next_request(), if controls_open => match request {
                    // A refused request is retired, never fatal: the spool is
                    // durable, so a request the engine will never accept
                    // would otherwise poison every restart.
                    Ok(request) => match self.admit_runtime_control(&request) {
                        Err(refusal) => {
                            tracing::error!(
                                request_id = %request.request_id,
                                strategy = %request.strategy_name,
                                refusal,
                                "refusing durable runtime control request"
                            );
                            control_feed.reject_last().await.map_err(|error| {
                                EngineError::State(error.to_string())
                            })?;
                        }
                        Ok(fresh) => {
                            if fresh {
                                self.apply_runtime_control(request)?;
                            }
                            self.drain(clock::now_ns()).await?;
                        }
                    },
                    Err(engine_types::RuntimeControlError::Closed) => controls_open = false,
                    Err(error) => return Err(EngineError::State(error.to_string())),
                },
                _ = tokio::time::sleep(timer_wait.unwrap_or_default()), if timer_wait.is_some() => {
                    self.on_timers().await?;
                }
                _ = flush_tick.tick() => self.on_tick().await?,
            }

            // Outside the select!, where the feeds are borrowable again.
            if !self.wanted_symbols.is_empty() {
                self.admit_wanted(market_feed, order_feed).await?;
            }
            if !self.pending_signal_deliveries.is_empty() {
                self.accept_pending_signals()?;
                self.drain(clock::now_ns()).await?;
            }
        }

        self.finish().await?;
        Ok(RunOutcome {
            stopped_by,
            market_events: self.events_seen,
            orders_sent: self.orders_sent,
        })
    }

    /// Last ledger line on the way out, and the whole tail forced to disk:
    /// a graceful stop that leaves its closing updates in the page cache
    /// tells the next boot's audit a lie.
    pub async fn finish(&mut self) -> Result<(), EngineError> {
        while !self.pending_mutations.is_empty() {
            let completion =
                tokio::time::timeout(Duration::from_secs(10), self.venue_completions.recv())
                    .await
                    .map_err(|_| {
                        EngineError::State(format!(
                            "graceful stop timed out with {} venue mutations outstanding",
                            self.pending_mutations.len()
                        ))
                    })?
                    .ok_or_else(|| {
                        EngineError::State(
                            "venue task stopped during graceful mutation drain".to_string(),
                        )
                    })?;
            self.take_venue_completion(completion).await?;
            self.drain(clock::now_ns()).await?;
        }
        if !self.deferred_actions.is_empty() || !self.ready_actions.is_empty() {
            return Err(EngineError::State(
                "graceful stop found deferred actions without a live venue mutation".to_string(),
            ));
        }
        // The barrier below covers these bytes too, but an outstanding one
        // carries an answer, and a stop that dropped it would be a failed
        // barrier nobody heard about.
        self.settle_barrier()?;
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
        self.confirmed_stop_moves.clear();
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
        self.account = view;
        // A cover the fresh reading has caught up with is released, so the
        // strategies woken after this read one truthful in-flight number.
        self.covers.absorb(&self.account);
    }

    /// Keep a venue-global position stop at least as protective as the
    /// fill-owned durable intent. This runs on every fresh account reading,
    /// closing the loop if a venue applies a later entry's Full TP/SL to the
    /// entire position or silently drops an attached stop.
    async fn enforce_position_stop_intent(&mut self) -> Result<(), EngineError> {
        let mut repairs = Vec::new();
        let mut failures = Vec::new();
        for position in &self.account.positions {
            let wanted = self
                .intended_stops
                .get(&position.symbol.0)
                .filter(|stop| stop.side == position.side)
                .map(|stop| stop.trigger_px);
            let venue =
                (position.stop_attached && position.stop_px.is_finite() && position.stop_px > 0.0)
                    .then_some(position.stop_px);
            let tolerance = self
                .rules
                .get(position.symbol.0 as usize)
                .and_then(|rule| rule.as_ref())
                .map(|rule| rule.tick_size / 2.0)
                .unwrap_or(1e-9);
            match (wanted, venue) {
                (Some(wanted), None) => repairs.push((position.symbol, position.side, wanted)),
                (Some(wanted), Some(venue))
                    if stop_is_looser(position.side, venue, wanted, tolerance) =>
                {
                    repairs.push((position.symbol, position.side, wanted));
                }
                (None, None) => failures.push(format!(
                    "{}: held {:?} position has no venue stop and no fill-owned durable stop intent",
                    self.market.table.name(position.symbol),
                    position.side
                )),
                _ => {}
            }
        }

        for (symbol, side, trigger_px) in repairs {
            match self.venue.set_stop(symbol, trigger_px).await {
                Ok(()) => {
                    if let Some(position) = self
                        .account
                        .positions
                        .iter_mut()
                        .find(|position| position.symbol == symbol && position.side == side)
                    {
                        position.stop_attached = true;
                        position.stop_px = trigger_px;
                    }
                    self.wal.append(&WalRecord::Note {
                        source: "stop-supervisor".into(),
                        text: format!(
                            "restored {} {:?} position stop to durable level {trigger_px}",
                            self.market.table.name(symbol),
                            side
                        ),
                    })?;
                }
                Err(error) => failures.push(format!(
                    "{}: failed to restore {:?} position stop {trigger_px}: {error}",
                    self.market.table.name(symbol),
                    side
                )),
            }
        }

        if failures.is_empty() {
            return Ok(());
        }
        self.may_open = false;
        for finding in &failures {
            tracing::error!(%finding, "position-stop supervision latched opening off");
        }
        self.wal.append(&WalRecord::Reconciled {
            wall_ts_ms: clock::wall_ms(),
            findings: failures,
            may_open: false,
        })?;
        self.wal.barrier()?;
        Ok(())
    }

    /// Stop trusting the account snapshot as soon as the private stream says
    /// continuity is gone. `observed_ns = 0` also makes the risk kernel's
    /// ordinary freshness check fail closed; the explicit readiness bit keeps
    /// a periodic REST refresh from re-enabling entries before execution
    /// history has closed the stream gap.
    fn invalidate_private_stream(&mut self) -> Result<(), EngineError> {
        self.private_stream_ready = false;
        self.account.observed_ns = 0;
        self.queue_halted_entry_cancels()
    }

    fn is_live_opening(&self, client_order_id: &str) -> bool {
        self.orders
            .orders
            .get(client_order_id)
            .is_some_and(|order| !order.request.reduce_only && order.in_flight())
    }

    /// Under sole leverage authority: hold what we set against what the venue
    /// says each held position actually runs at. A mismatch means somebody
    /// else wrote leverage on an account we believed only we write — say so
    /// loudly and evict the trust, which makes the next entry in that symbol
    /// confirm with the venue inline, exactly as shared authority always does.
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
                    symbol = self.market.table.name(position.symbol),
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
                        self.market.table.name(position.symbol)
                    ),
                });
                self.leverage_at.remove(&position.symbol);
            }
        }
    }

    /// Make the venue agree that this symbol sits at this leverage, before an
    /// order that would post margin against it.
    ///
    /// Margin is notional divided by leverage, so an order sized at one
    /// leverage and filled at another does not commit the capital the risk
    /// kernel priced. Unknown is not "probably fine": every failure here
    /// refuses the order rather than sending it and hoping.
    async fn ensure_leverage(&mut self, symbol: SymbolId, want: f64) -> Result<(), String> {
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
        if self.leverage_at.get(&symbol).is_some_and(|at| *at == want) {
            return Ok(());
        }
        match self.venue.set_leverage(symbol, want).await {
            Ok(()) => {
                self.leverage_at.insert(symbol, want);
                Ok(())
            }
            Err(e) => Err(format!("could not set leverage to {want}: {e}")),
        }
    }

    fn mint_id(&mut self) -> String {
        let orders = &self.orders;
        mint_unused(
            self.registry.prefix(),
            &mut self.next_order_n,
            |candidate| orders.contains(candidate),
        )
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
    /// trusts the venue comparison instead), the run's own latency ledger
    /// and cost score, and markout horizons still owed (a restart already
    /// ends those; the marks written so far are in the archived segments).
    pub(crate) fn rotation_base(&self, wall_ts_ms: i64) -> WalRecord {
        WalRecord::SegmentBase {
            wall_ts_ms,
            strategies: self.names.clone(),
            symbols: (0..self.market.table.len())
                .map(|i| self.market.table.name(SymbolId(i as u16)).to_string())
                .collect(),
            may_open: self.may_open,
            // Older WALs may contain anchors from the retired daily-loss
            // feature. Reading remains compatible; rotation scrubs them.
            control_anchors: Vec::new(),
            attribution: self
                .attribution
                .rows()
                .into_iter()
                .map(|(strategy, symbol, signed_qty)| engine_types::FilledTotal {
                    strategy,
                    symbol,
                    signed_qty,
                })
                .collect(),
            logged_exposure: self
                .logged_exposure
                .iter()
                .map(|(symbol, signed_qty)| engine_types::SymbolTotal {
                    symbol: SymbolId(*symbol),
                    signed_qty: *signed_qty,
                })
                .collect(),
            intended_stops: self
                .intended_stops
                .iter()
                .map(|(symbol, stop)| engine_types::IntendedStop {
                    symbol: SymbolId(*symbol),
                    side: Some(stop.side),
                    trigger_px: stop.trigger_px,
                })
                .collect(),
            recent_execution_ids: self.recovered_exec_ids.rows(wall_ts_ms),
            execution_history_through_ms: Some(self.recovered_until_ms),
            target_book_latches: Vec::new(),
            strategy_checkpoints: self
                .strategy_checkpoints
                .iter()
                .map(
                    |((strategy, symbol), checkpoint)| engine_types::StrategyCheckpointState {
                        strategy: StrategyId(*strategy),
                        symbol: SymbolId(*symbol),
                        checkpoint: checkpoint.clone(),
                    },
                )
                .collect(),
            strategy_global_checkpoints: self
                .strategy_global_checkpoints
                .values()
                .cloned()
                .collect(),
            strategy_events: self.strategy_events.values().cloned().collect(),
            signal_observations: self.signal_observations.values().cloned().collect(),
            signal_cursors: self.signal_cursors.values().cloned().collect(),
            signal_subscriptions: self.signal_subscriptions.values().cloned().collect(),
            runtime_control_requests: self.runtime_control_requests.clone(),
            runtime_control_consumed: self
                .runtime_control_consumed
                .iter()
                .map(|(strategy, request_id)| (StrategyId(*strategy), request_id.clone()))
                .collect(),
            open_orders: self
                .orders
                .in_flight()
                .into_iter()
                .map(|order| engine_types::OpenOrderState {
                    request: order.request.clone(),
                    wire_ns: order.wire_ns,
                    arrival_mid: order.arrival_mid,
                    acked: order.acked,
                    filled_qty: order.filled_qty,
                    reservation_low_px: order.reservation_low_px,
                    reservation_high_px: order.reservation_high_px,
                })
                .collect(),
        }
    }
}
