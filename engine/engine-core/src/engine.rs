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
//! - the research system's target book, when one is being watched
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
    RiskKernel, RiskVerdict, Side, StopSpec, Strategy, StrategyId, Subscription, SymbolId,
    SymbolTable, TargetBook, TimeInForce, VenueError, VenueGateway, Wal, WalError, WalRecord,
    WorkPolicy,
};

use crate::attribution::Attribution;
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
use crate::targets::TargetBooks;
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

fn stop_key(symbol: SymbolId, side: Side) -> (u16, bool) {
    (symbol.0, side == Side::Sell)
}

fn tighter_stop(side: Side, left: f64, right: f64) -> f64 {
    match side {
        Side::Buy => left.max(right),
        Side::Sell => left.min(right),
    }
}

fn stop_is_looser(side: Side, candidate: f64, protected: f64, tolerance: f64) -> bool {
    match side {
        Side::Buy => candidate + tolerance < protected,
        Side::Sell => candidate - tolerance > protected,
    }
}

pub const ENGINE_VERSION: &str = concat!("engine-core ", env!("CARGO_PKG_VERSION"));

/// How many recently journaled fills to remember for gap-recovery dedup.
/// A gap plus its pads spans minutes; this covers hours of fills.
const RECENT_FILLS_KEPT: usize = 2048;

/// A quiet account renews its execution-history proof daily, well inside the
/// shortest supported venue history window.
const HISTORY_CHECKPOINT_INTERVAL_MS: i64 = 86_400_000;

/// The newest boundary a successful execution-history read proved. No generic
/// wall stamp belongs here: a fill, rotation, or graceful stop does not prove
/// that an otherwise empty interval was scanned.
fn execution_history_through_ms(replayed: &[WalRecord]) -> Option<i64> {
    let mut newest = None;
    for record in replayed {
        let stamp = match record {
            WalRecord::ExecutionHistoryCheckpoint { through_wall_ts_ms } => {
                Some(*through_wall_ts_ms)
            }
            WalRecord::SegmentBase {
                execution_history_through_ms,
                ..
            } => *execution_history_through_ms,
            _ => None,
        };
        if let Some(stamp) = stamp {
            if newest.is_none_or(|n| stamp > n) {
                newest = Some(stamp);
            }
        }
    }
    newest.or_else(|| legacy_boot_ms(replayed))
}

/// Older WALs used their boot stamp as the recovery boundary. Keep that one
/// compatibility path, but never promote a later reconciliation, rotation,
/// fill, or shutdown stamp into a history proof.
fn legacy_boot_ms(replayed: &[WalRecord]) -> Option<i64> {
    let mut newest = None;
    for record in replayed {
        if let WalRecord::Boot { wall_ts_ms, .. } = record {
            if newest.is_none_or(|known| *wall_ts_ms > known) {
                newest = Some(*wall_ts_ms);
            }
        }
    }
    newest
}

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
/// Grouping by symbol keeps concurrent target books from admitting the same
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
    /// Symbol/feed subscriptions a book has requested that are not live yet,
    /// together with every strategy waiting to hear them.
    ///
    /// Filled while a book is being handled and drained by the run loop, which
    /// is the only place that holds the feeds. Admitting from inside
    /// `on_targets` would mean borrowing them out of the `select!` they are
    /// waiting in.
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
    /// The target book watcher, when one was configured. Parked here between
    /// boot and run; `run` takes it out for the length of the run, because
    /// the loop's `select!` needs it as a local — a future borrowing it out
    /// of `self` would lock out every branch that has work to do.
    targets: TargetBooks,
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

pub(crate) fn venue_minus_local_ms(
    venue_ts_ms: i64,
    recv_ns: u64,
    now_ns: u64,
    wall_ts_ms: i64,
) -> i64 {
    let age_ms = (now_ns.saturating_sub(recv_ns) / 1_000_000) as i64;
    venue_ts_ms - (wall_ts_ms - age_ms)
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// Come up: read the log back, say who we are in it, learn what the
    /// strategies want, then ask the venue for the instrument rules and the
    /// account before the first message is allowed in.
    ///
    /// The strategies are named by their plug, which is right for a fleet
    /// where each plug runs once and wrong for this one: both sleeves here are
    /// `target_book`, so a log and a heartbeat that named the plug said
    /// "target_book" twice and left nobody able to tell carry from long. Use
    /// [`Engine::boot_as`] to give them the names their config chose.
    pub async fn boot(
        settings: &EngineSection,
        config_sha256: &str,
        wal: W,
        risk: R,
        venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        Engine::boot_as(
            settings,
            config_sha256,
            wal,
            risk,
            venue,
            strategies,
            &[],
            replayed,
        )
        .await
    }

    /// The same, with each sleeve's own name from its config block.
    ///
    /// `sleeves[i]` names the strategy in position `i`; a short list, or an
    /// entry that is empty, falls back to that strategy's plug name. This is
    /// what goes in the log's id table and in the heartbeat, so `engine fills`
    /// can say which sleeve's trading cost what.
    #[allow(clippy::too_many_arguments)]
    pub async fn boot_as(
        settings: &EngineSection,
        config_sha256: &str,
        mut wal: W,
        mut risk: R,
        mut venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        if !(1..=crate::config::MAX_GROUP_FLUSH_MS).contains(&settings.group_flush_ms) {
            return Err(EngineError::Boot(format!(
                "group_flush_ms must be between 1 and {}",
                crate::config::MAX_GROUP_FLUSH_MS
            )));
        }
        // The order/attribution/exposure scans happen AFTER fill recovery
        // below, so a fill the venue saw while this process was down seeds
        // every book the same way a delivered one would have.

        let boot_ms = clock::wall_ms();
        let mut market = MarketState::default();
        // Ids are interning positions, so the previous run's table is
        // re-interned first, in its own order: every id the replayed records
        // name then means the same symbol in this run. Attribution, the
        // reconcile's exposure accounting, and in-flight recovery all join
        // the OLD run's numbers against this table — a symbol a book
        // admitted at runtime last run would otherwise come back at a
        // different position, or not at all. `assembly::symbol_order` seeds
        // the gateway and the private stream with this same order.
        for name in crate::replay::LogNames::of_log(replayed).symbols {
            market.add_symbol(&name);
        }
        let mut routing = Routing::default();
        let mut names = Vec::with_capacity(strategies.len());
        let mut subscriptions = Vec::new();
        for (index, strategy) in strategies.iter().enumerate() {
            let sid = StrategyId(
                u16::try_from(index)
                    .map_err(|_| EngineError::Boot("more than 65535 strategies".to_string()))?,
            );
            names.push(match sleeves.get(index) {
                Some(sleeve) if !sleeve.is_empty() => sleeve.clone(),
                _ => strategy.name().to_string(),
            });
            for sub in strategy.subscriptions() {
                let symbol = market.add_symbol(&sub.symbol);
                routing.add(symbol, sub.feed, sid);
                if !subscriptions.contains(&sub) {
                    subscriptions.push(sub.clone());
                }
            }
        }
        let prior_names = crate::replay::LogNames::of_log(replayed).strategies;
        if !sleeves.is_empty()
            && !prior_names.is_empty()
            && !names.as_slice().starts_with(prior_names.as_slice())
        {
            return Err(EngineError::Boot(format!(
                "configured strategy identity/order {:?} does not preserve the WAL prefix {:?}",
                names, prior_names
            )));
        }
        let mut distinct = std::collections::HashSet::new();
        if !sleeves.is_empty()
            && names
                .iter()
                .any(|name| name.is_empty() || !distinct.insert(name))
        {
            return Err(EngineError::Boot(
                "strategy sleeve names must be non-empty and unique".to_string(),
            ));
        }
        routing.size_to(market.table.len());
        wal.append(&WalRecord::Boot {
            version: ENGINE_VERSION.to_string(),
            config_sha256: config_sha256.to_string(),
            wall_ts_ms: boot_ms,
        })?;
        wal.append(&WalRecord::Note {
            source: "engine".into(),
            text: "live: orders are sent, each one gated by the risk kernel".to_string(),
        })?;
        // Say what the ids mean before any record uses one. Without this every
        // later line names a number, and a log read a week later cannot say
        // which coin an order was for.
        wal.append(&names_record(&names, &market))?;

        let mut rules = vec![None; market.table.len()];
        for (name, rule) in venue.instrument_rules().await? {
            if let Some(id) = market.table.get(&name) {
                rules[id.0 as usize] = Some(rule);
            }
        }
        let mut missing: Vec<&str> = Vec::new();
        for subscription in &subscriptions {
            let Some(id) = market.table.get(&subscription.symbol) else {
                continue;
            };
            if rules[id.0 as usize].is_none() && !missing.contains(&subscription.symbol.as_str()) {
                missing.push(subscription.symbol.as_str());
            }
        }
        if !missing.is_empty() {
            return Err(EngineError::Boot(format!(
                "venue returned no instrument rules for configured symbols: {}",
                missing.join(", ")
            )));
        }

        let account = venue.account_view().await?;
        risk.observe_account_view(&account);

        // Fills the venue saw and this log never heard: a stop that fired
        // during a deploy window, an execution inside a private-stream gap.
        // Recovered from the venue's own history and made durable before the
        // log is compared to the venue, so what actually traded is a fill in
        // the log rather than a finding against it.
        let mut recovered_exec_ids = ExecutionIds::from_records(replayed, boot_ms)
            .map_err(|e| EngineError::State(e.to_string()))?;
        let recovery = Self::recover_missed_fills(
            &mut wal,
            &mut venue,
            replayed,
            &market.table,
            &mut recovered_exec_ids,
            boot_ms,
        )
        .await?;
        let effective_owned: Vec<WalRecord>;
        let effective: &[WalRecord] = if recovery.records.is_empty() {
            replayed
        } else {
            effective_owned = replayed.iter().cloned().chain(recovery.records).collect();
            &effective_owned
        };

        let mut orders = LedgerOfOrders::from_records(effective);
        // Same records, same join: a restart must not forget whose
        // position is whose, or the other sleeve trades straight into it.
        let mut attribution = Attribution::from_records(effective);
        let mut fills = Fills::default();
        fills.seed_lots(effective);
        // Seeded by the same scans reconcile trusts and kept live from here
        // on, because a rotation restates them into the new segment's first
        // record and must say exactly what a replay would have said.
        let logged_exposure = crate::reconcile::logged_exposure(effective);
        let intended_stops = crate::reconcile::intended_stops(effective);
        // A gap-recovery pass reaches back past this boot, and the venue hands
        // back everything in that window — the last run's ordinary fills
        // included. A delivered fill carries no venue execution id, so only
        // this can tell the pass it already has one.
        let mut recent_fills: VecDeque<(String, i64, f64)> = effective
            .iter()
            .filter_map(|record| match record {
                WalRecord::OrderUpdate {
                    update:
                        OrderUpdate::Fill {
                            client_order_id,
                            venue_ts_ms,
                            qty,
                            ..
                        },
                } => Some((client_order_id.clone(), *venue_ts_ms, *qty)),
                _ => None,
            })
            .collect();
        while recent_fills.len() > RECENT_FILLS_KEPT {
            recent_fills.pop_front();
        }

        // What the log believes against what the venue says. Boot is the one
        // moment the two can be compared: from here on the engine only ever
        // learns about its own orders.
        let (may_open, vanished) = Self::reconcile_with_venue(
            &mut wal,
            &mut venue,
            &orders,
            effective,
            &account,
            &market.table,
            &rules,
        )
        .await?;

        // An order the log shows in flight that the venue is not working
        // ended while the engine was down, and no update for it will ever
        // arrive. Left "in flight" it would charge the kernel's partition on
        // every future boot and hold the one-order-per-symbol gate closed
        // against that symbol — exits included — until somebody hand-fixed
        // the venue. The venue's own working-order listing is evidence, not
        // a guess, so the ending is written down as what it was.
        for client_order_id in vanished {
            tracing::warn!(
                id = %client_order_id,
                "this order ended while the engine was down; recording the ending"
            );
            let ended = WalRecord::OrderUpdate {
                update: OrderUpdate::Cancelled {
                    client_order_id,
                    recv_ns: clock::now_ns(),
                },
            };
            wal.append(&ended)?;
            orders.apply(&ended);
        }
        let recovered = orders.in_flight().len();

        // A sleeve's claim on a symbol the venue holds nothing of is a close
        // this log never got to charge (a venue stop firing, an inherited
        // position wound down), and it would lock every other sleeve out of
        // the name for good. The venue reading is the authority on what is
        // held, so flat clears the claim; a symbol with an order still in
        // flight is left alone.
        let in_flight_symbols: std::collections::HashSet<SymbolId> = orders
            .in_flight()
            .iter()
            .map(|order| order.request.symbol)
            .collect();
        let stale_claims = attribution.drop_where_flat(|symbol| {
            !in_flight_symbols.contains(&symbol)
                && !account
                    .positions
                    .iter()
                    .any(|p| p.symbol == symbol && p.qty > 0.0)
        });
        if !stale_claims.is_empty() {
            let words = stale_claims
                .iter()
                .map(|(strategy, symbol, qty)| {
                    format!(
                        "{} {} {qty}",
                        names
                            .get(strategy.0 as usize)
                            .map(String::as_str)
                            .unwrap_or("unknown"),
                        market.table.name(*symbol)
                    )
                })
                .collect::<Vec<_>>()
                .join(", ");
            tracing::warn!(
                claims = %words,
                "dropping sleeve claims on symbols the venue holds nothing of"
            );
            // Durable, not a note: a later boot replays the drop instead of
            // rebuilding the residue from the old fills — by then another
            // sleeve may hold the symbol, and a venue no longer flat would
            // make the residue undroppable.
            // The same names, out of the position accounting too: a claim the
            // venue does not back has no exit price, so its trip cannot be
            // reported and must not sit waiting for one.
            let gone: std::collections::HashSet<(String, String)> = stale_claims
                .iter()
                .map(|(strategy, symbol, _)| {
                    (
                        names.get(strategy.0 as usize).cloned().unwrap_or_default(),
                        market.table.name(*symbol).to_string(),
                    )
                })
                .collect();
            fills.lots().drop_symbols(|sleeve, symbol| {
                gone.contains(&(sleeve.to_string(), symbol.to_string()))
            });
            wal.append(&WalRecord::ClaimsDropped {
                wall_ts_ms: clock::wall_ms(),
                rows: stale_claims
                    .iter()
                    .map(|(strategy, symbol, qty)| engine_types::FilledTotal {
                        strategy: *strategy,
                        symbol: *symbol,
                        signed_qty: *qty,
                    })
                    .collect(),
            })?;
        }

        // Nothing is lost by the rounding `boot_prefix` does: the stamp only
        // separates one boot's ids from another's, and `mint_unused` already
        // refuses any id the replayed log has seen.
        let mut registry = OrderRegistry::new(OrderRegistry::boot_prefix(boot_ms));
        for order in orders.in_flight() {
            registry.own(&order.request.client_order_id, order.request.strategy);
            // The kernel's partition must keep charging last boot's working
            // orders, or a restart hands every share out twice.
            let request = &order.request;
            let remaining_qty = request.qty - order.filled_qty;
            if !remaining_qty.is_finite() || remaining_qty < -1e-9 {
                return Err(EngineError::Boot(format!(
                    "in-flight order {} has impossible remaining quantity: request {}, filled {}",
                    request.client_order_id, request.qty, order.filled_qty
                )));
            }
            if remaining_qty <= 1e-9 {
                continue;
            }
            risk.register_order_price_range(
                &request.client_order_id,
                &Intent {
                    strategy: request.strategy,
                    symbol: request.symbol,
                    side: request.side,
                    qty: remaining_qty,
                    kind: request.kind,
                    stop: request.stop,
                    reduce_only: request.reduce_only,
                    tag: "recovered".to_string(),
                    decided_ns: 0,
                    // The order is already at the venue; there is nothing
                    // left to decide about how it was placed, and its
                    // leverage was set before it went.
                    work: None,
                    leverage: None,
                },
                remaining_qty,
                order.reservation_low_px,
                order.reservation_high_px,
            );
        }
        if recovered > 0 {
            tracing::warn!(
                count = recovered,
                ids = ?orders.in_flight_ids(),
                "orders were in flight when the engine last stopped; they are not re-sent"
            );
        }

        let now = clock::now_ns();
        let (venue, venue_completions) = VenueClient::spawn(venue);
        let mut engine = Engine {
            refusals: HashMap::new(),
            wal,
            risk,
            venue,
            venue_completions,
            pending_mutations: HashMap::new(),
            busy_symbols: HashMap::new(),
            deferred_actions: HashMap::new(),
            ready_actions: VecDeque::new(),
            _venue: std::marker::PhantomData,
            strategies,
            names,
            market,
            routing,
            rules,
            timers: Timers::default(),
            pending: VecDeque::new(),
            drain_progress: None,
            account,
            registry,
            orders,
            attribution,
            // Empty on purpose, like the follower's own records were across a
            // restart: boot compares the log against the venue directly,
            // which is a better answer than a memory of what was in flight.
            covers: CoverBook::default(),
            // Deliberately not restored from the log. The window is measured
            // from a monotonic clock that does not survive a restart, and the
            // venue's own creation time is not something this engine can ask
            // for — so a recovered order is left alone rather than worked
            // from a made-up deadline.
            working: WorkingOrders::default(),
            halt_cancels: std::collections::HashMap::new(),
            amends_awaiting_price: HashMap::new(),
            amends_confirmed: 0,
            amends_pulled_unconfirmed: 0,
            stream_resets: 0,
            pending_barrier: None,
            halt_cancel_queue: VecDeque::new(),
            wanted_symbols: Vec::new(),
            leverage_at: std::collections::HashMap::new(),
            may_open,
            private_stream_ready: true,
            logged_exposure,
            intended_stops,
            recovered_until_ms: recovery.through_ms,
            next_history_checkpoint_ms: recovery
                .through_ms
                .saturating_add(HISTORY_CHECKPOINT_INTERVAL_MS),
            recovered_exec_ids,
            recent_fills,
            ledger: LatencyLedger::new(now),
            // Its cost rows are a running score for the run in front of you,
            // and the whole history is one `engine fills` away; its open
            // positions were rebuilt above, because a close priced without
            // its entry is a number about nothing.
            fills,
            targets: TargetBooks::new(Vec::new()),
            heartbeat: None,
            trades: None,
            leverage_authority: settings.leverage_authority,
            group_flush: Duration::from_millis(settings.group_flush_ms.max(1)),
            refresh_after_ns: settings.account_view_max_age_ms.saturating_mul(1_000_000) / 2,
            rotate_after_bytes: settings.wal_rotate_mb.saturating_mul(1024 * 1024),
            max_quote_age_ns: settings.max_quote_age_ms.saturating_mul(1_000_000),
            next_order_n: 0,
            orders_sent: 0,
            events_seen: 0,
            subscriptions,
        };
        engine
            .fills
            .learn(&names_record(&engine.names, &engine.market));
        engine.queue_halted_entry_cancels()?;
        Ok(engine)
    }

    /// Ask the venue what traded on this account since the log's newest
    /// stamp, and write down every execution the log has never seen.
    ///
    /// Success is durable before the reconcile that would otherwise have
    /// read what actually traded as somebody else's trading. Failure aborts
    /// boot: without the missing interval the log cannot prove its exposure.
    async fn recover_missed_fills(
        wal: &mut W,
        venue: &mut V,
        replayed: &[WalRecord],
        table: &SymbolTable,
        execution_ids: &mut ExecutionIds,
        fresh_start_ms: i64,
    ) -> Result<RecoveryOutcome, EngineError> {
        let now_ms = clock::wall_ms();
        let newest = match execution_history_through_ms(replayed) {
            Some(stamp) => stamp,
            None if replayed.is_empty() => fresh_start_ms,
            None => {
                return Err(EngineError::Boot(
                    "the existing log has no durable execution-history boundary".to_string(),
                ))
            }
        };
        let since = newest.saturating_sub(RECOVERY_PAD_MS);
        if since < now_ms - RECOVERY_REACH_MS {
            return Err(EngineError::Boot(format!(
                "the log is {} ms behind, beyond the venue execution-history reach of {} ms",
                now_ms - newest,
                RECOVERY_REACH_MS
            )));
        }
        if since >= now_ms {
            return Ok(RecoveryOutcome {
                records: Vec::new(),
                through_ms: newest,
            });
        }
        let mut execs = venue.executions(since, now_ms).await.map_err(|e| {
            EngineError::Boot(format!(
                "cannot read execution history for the recovery interval: {e}"
            ))
        })?;
        let mut delivered: std::collections::HashMap<(String, i64, u64), usize> =
            std::collections::HashMap::new();
        for record in replayed {
            if let WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        exec_id,
                        client_order_id,
                        venue_ts_ms,
                        qty,
                        ..
                    },
            } = record
            {
                if exec_id.is_empty() && *venue_ts_ms >= since {
                    *delivered
                        .entry((client_order_id.clone(), *venue_ts_ms, qty.to_bits()))
                        .or_default() += 1;
                }
            }
        }
        execs.sort_by_key(|exec| exec.venue_ts_ms);
        let mut out = Vec::new();
        let mut recovered = 0usize;
        let mut unknown_findings = Vec::new();
        let mut recovered_orders = LedgerOfOrders::from_records(replayed);
        for exec in execs {
            if execution_ids.contains(&exec.exec_id, now_ms) {
                continue;
            }
            let key = (
                exec.client_order_id.clone(),
                exec.venue_ts_ms,
                exec.qty.to_bits(),
            );
            let same_delivered = delivered.get_mut(&key).is_some_and(|count| {
                if *count == 0 {
                    false
                } else {
                    *count -= 1;
                    true
                }
            });
            if same_delivered {
                continue;
            }
            execution_ids
                .can_insert(&exec.exec_id, now_ms)
                .map_err(|e| EngineError::State(e.to_string()))?;
            let Some(symbol) = table.get(&exec.symbol) else {
                // The configured symbol table cannot safely absorb this
                // quantity, but silently dropping it would make a foreign
                // round trip invisible whenever the final account is flat.
                let finding = Self::foreign_unmapped_execution_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    &exec.symbol,
                    exec.qty,
                );
                let note = WalRecord::Note {
                    source: "fill-recovery".into(),
                    text: finding.clone(),
                };
                wal.append(&note)?;
                execution_ids.insert(exec.exec_id, now_ms);
                out.push(note);
                unknown_findings.push(finding);
                continue;
            };
            let dedup_id = exec.exec_id.clone();
            if let Err(reason) = recovered_orders.validate_fill(
                &exec.client_order_id,
                symbol,
                exec.side,
                exec.qty,
                exec.px,
            ) {
                unknown_findings.push(Self::untrusted_fill_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    symbol,
                    exec.side,
                    exec.qty,
                    exec.px,
                    &reason,
                ));
                execution_ids.insert(dedup_id, now_ms);
                recovered += 1;
                continue;
            }
            let record = WalRecord::RecoveredFill {
                exec_id: exec.exec_id,
                client_order_id: exec.client_order_id,
                symbol,
                side: exec.side,
                qty: exec.qty,
                px: exec.px,
                fee: exec.fee,
                is_maker: exec.is_maker,
                venue_ts_ms: exec.venue_ts_ms,
                recovered_wall_ts_ms: now_ms,
            };
            wal.append(&record)?;
            execution_ids.insert(dedup_id, now_ms);
            recovered_orders.apply(&record);
            out.push(record);
            recovered += 1;
        }
        if !unknown_findings.is_empty() {
            let latch = WalRecord::Reconciled {
                wall_ts_ms: now_ms,
                findings: unknown_findings,
                may_open: false,
            };
            wal.append(&latch)?;
            out.push(latch);
        }
        if recovered > 0 {
            tracing::warn!(
                count = recovered,
                "recovered fills the private stream never delivered"
            );
        }
        let checkpoint = WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: now_ms,
        };
        wal.append(&checkpoint)?;
        out.push(checkpoint);
        wal.barrier()?;
        Ok(RecoveryOutcome {
            records: out,
            through_ms: now_ms,
        })
    }

    /// Compare the log against the venue, write down what was found, and say
    /// whether the engine may open new exposure.
    ///
    /// The latch is durable. If an earlier boot found something it could not
    /// explain and stopped opening, this one starts stopped too — a restart
    /// that cleared it would turn "stop and tell somebody" into "stop until
    /// the next crash", which is no protection at all on a process that gets
    /// restarted by a supervisor.
    ///
    /// Nothing here cancels anything. An order the engine did not place is
    /// not its to take down, and a position it cannot account for is not its
    /// to close. It says so, repairs the stops it has evidence for, and
    /// stops adding.
    #[allow(clippy::too_many_arguments)]
    async fn reconcile_with_venue(
        wal: &mut W,
        venue: &mut V,
        orders: &LedgerOfOrders,
        replayed: &[WalRecord],
        account: &AccountView,
        table: &SymbolTable,
        rules: &[Option<InstrumentRule>],
    ) -> Result<(bool, Vec<String>), EngineError> {
        let latched = replayed.iter().rev().find_map(|record| match record {
            WalRecord::Reconciled { may_open, .. } => Some(*may_open),
            // A rotation restated the latch; nothing between it and the end
            // of the log has said otherwise or the scan would have stopped
            // there first.
            WalRecord::SegmentBase { may_open, .. } => Some(*may_open),
            // An operator ran `reconcile-clear`: the deliberate look the
            // latch waits for. It resets the memory, not the check — the
            // comparison below still latches again on anything that stands.
            WalRecord::LatchCleared { .. } => Some(true),
            _ => None,
        });

        let working = match venue.working_orders().await {
            Ok(rows) => rows,
            Err(e) => {
                // Not knowing what the venue is working is exactly the state
                // this check exists to catch, so it is not something to
                // shrug at and carry on from.
                return Err(EngineError::Boot(format!(
                    "cannot read what the venue is working, so there is no way to tell \
                     whose orders are out there: {e}"
                )));
            }
        };

        let found = reconcile::reconcile(
            orders,
            replayed,
            &working,
            account,
            |name| table.get(name),
            |id| {
                rules
                    .get(id.0 as usize)
                    .and_then(|r| r.as_ref())
                    .map(|r| r.qty_step)
            },
            |id| {
                rules
                    .get(id.0 as usize)
                    .and_then(|r| r.as_ref())
                    .map(|r| r.tick_size)
            },
        );

        let mut finding_lines = found.lines();
        for line in &finding_lines {
            tracing::warn!(finding = %line, "reconciliation");
        }

        // A stop the log says belongs somewhere, that the venue does not have.
        // Putting it back is the one repair the engine can make from evidence
        // rather than from a guess.
        let mut repair_failed = false;
        for (symbol, trigger_px) in found.stop_repairs() {
            match venue.set_stop(symbol, trigger_px).await {
                Ok(()) => tracing::info!(
                    symbol = table.name(symbol),
                    trigger_px,
                    "restored the fill-owned durable position stop"
                ),
                Err(e) => {
                    repair_failed = true;
                    let line = format!(
                        "{}: failed to restore durable stop {trigger_px}: {e}",
                        table.name(symbol)
                    );
                    tracing::error!(
                        symbol = table.name(symbol),
                        trigger_px,
                        error = %e,
                        "could not put the stop back; opening remains latched off"
                    );
                    finding_lines.push(line);
                }
            }
        }

        let may_open = latched.unwrap_or(true) && !found.must_not_open() && !repair_failed;
        if latched == Some(false) && !found.must_not_open() {
            tracing::error!(
                "an earlier boot stopped this engine opening new positions and nothing here \
                 clears that; it will reduce only until somebody looks at the log"
            );
        }
        if !may_open {
            tracing::error!(
                "this engine will not open new positions: the account holds orders or exposure \
                 its own log cannot account for"
            );
        }

        wal.append(&WalRecord::Reconciled {
            wall_ts_ms: clock::wall_ms(),
            findings: finding_lines,
            may_open,
        })?;
        // Durable before trading starts: a crash between here and the first
        // order must not lose a latch that was just set.
        wal.barrier()?;
        Ok((may_open, found.vanished()))
    }

    pub fn subscriptions(&self) -> &[Subscription] {
        &self.subscriptions
    }

    /// Follow a target book. Optional: with no watcher the engine simply
    /// never hears about one, and a follower plug holds whatever it holds.
    pub fn watch_targets(&mut self, books: TargetBooks) {
        self.targets = books;
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
        tokio::pin!(shutdown);
        let mut flush_tick = tokio::time::interval(self.group_flush);
        flush_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut stopped_by = StopReason::Shutdown;
        // Out of `self` for the length of the run: a select! branch waiting
        // on it must not borrow the engine the other branches need.
        let mut targets = std::mem::replace(&mut self.targets, TargetBooks::new(Vec::new()));
        let mut targets_open = !targets.is_empty();

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
                    book = targets.next_book(), if targets_open => match book {
                        Some((strategy, book)) => self.on_targets(strategy, book).await?,
                        None => {
                            tracing::error!("every target book watcher has stopped; no further books will arrive");
                            targets_open = false;
                        }
                    }
                }

                if !self.wanted_symbols.is_empty() {
                    self.admit_wanted(market_feed, order_feed).await?;
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
                book = targets.next_book(), if targets_open => match book {
                    Some((strategy, book)) => self.on_targets(strategy, book).await?,
                    // Every watcher has gone. Each one's own departure was
                    // logged as it happened; this is the last of them.
                    // Nothing else changes: no book means no decision, and
                    // followers hold what they hold.
                    None => {
                        tracing::error!("every target book watcher has stopped; no further books will arrive");
                        targets_open = false;
                    }
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
        }

        self.targets = targets;
        self.finish().await?;
        Ok(RunOutcome {
            stopped_by,
            market_events: self.events_seen,
            orders_sent: self.orders_sent,
        })
    }

    async fn take_completion_turn<O: OrderFeed>(
        &mut self,
        completion: MutationCompletion,
        order_feed: &mut O,
    ) -> Result<(), EngineError> {
        self.take_venue_completion(completion).await?;
        let private_update = tokio::select! {
            biased;
            update = order_feed.next_update() => Some(update),
            _ = std::future::ready(()) => None,
        };
        match private_update {
            Some(Ok(update)) => self.take_update(update).await?,
            Some(Err(engine_types::FeedError::Closed)) => {
                return Err(EngineError::State(
                    "private order feed closed while a venue mutation completed".to_string(),
                ));
            }
            Some(Err(error)) => {
                self.invalidate_private_stream()?;
                tracing::warn!(error = %error, "order feed hiccup after venue mutation");
            }
            None => {}
        }
        self.drain(clock::now_ns()).await
    }

    async fn settle_after_market_close<O: OrderFeed>(
        &mut self,
        order_feed: &mut O,
    ) -> Result<(), EngineError> {
        while !self.pending_mutations.is_empty() {
            let completion =
                tokio::time::timeout(Duration::from_secs(10), self.venue_completions.recv())
                    .await
                    .map_err(|_| {
                        EngineError::State(format!(
                            "market feed closed with {} venue mutations still outstanding",
                            self.pending_mutations.len()
                        ))
                    })?
                    .ok_or_else(|| {
                        EngineError::State(
                            "venue task stopped while the market-close tail was draining"
                                .to_string(),
                        )
                    })?;
            self.take_completion_turn(completion, order_feed).await?;
        }
        Ok(())
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

    async fn on_market(&mut self, event: MarketEvent) -> Result<(), EngineError> {
        let now = clock::now_ns();
        self.market.apply(&event);
        match event {
            MarketEvent::Quote { symbol, quote } if quote.bid_px > 0.0 && quote.ask_px > 0.0 => {
                self.risk
                    .observe_price(symbol, (quote.bid_px + quote.ask_px) / 2.0);
            }
            MarketEvent::Depth { symbol, depth }
                if depth.best_bid().is_some() && depth.best_ask().is_some() =>
            {
                let quote = depth.quote();
                self.risk
                    .observe_price(symbol, (quote.bid_px + quote.ask_px) / 2.0);
            }
            MarketEvent::Trades { symbol, trades } if trades.last_px > 0.0 => {
                self.risk.observe_price(symbol, trades.last_px);
            }
            MarketEvent::Ticker { symbol, ticker } if ticker.last_px > 0.0 => {
                self.risk.observe_price(symbol, ticker.last_px);
            }
            _ => {}
        }
        self.ledger.saw_event();
        self.events_seen += 1;
        let origin_ns = arrival_ns(&event, now);
        let engine_event = EngineEvent::Market(event);
        {
            let Engine {
                strategies,
                market,
                timers,
                pending,
                routing,
                orders,
                registry,
                attribution,
                covers,
                account,
                rules,
                ..
            } = self;
            let count = strategies.len();
            let mut feed = |sid| {
                feed_strategy(
                    strategies,
                    market,
                    account,
                    rules,
                    timers,
                    pending,
                    orders,
                    registry,
                    attribution,
                    covers,
                    sid,
                    &engine_event,
                    now,
                )
            };
            match event {
                MarketEvent::Quote { symbol, .. } => {
                    for sid in routing.quote_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Depth { symbol, .. } => {
                    for sid in routing.depth_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Trades { symbol, .. } => {
                    for sid in routing.trade_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Ticker { symbol, .. } => {
                    for sid in routing.ticker_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::FeedReset { .. } => {
                    for index in 0..count {
                        feed(StrategyId(index as u16));
                    }
                }
            }
        }
        self.drain(origin_ns).await
    }

    async fn on_timers(&mut self) -> Result<(), EngineError> {
        let now = clock::now_ns();
        while let Some((sid, timer)) = self.timers.pop_due(now) {
            let event = EngineEvent::Timer {
                id: timer,
                now_ns: now,
            };
            let Engine {
                strategies,
                market,
                timers,
                pending,
                orders,
                registry,
                attribution,
                covers,
                account,
                rules,
                ..
            } = self;
            feed_strategy(
                strategies,
                market,
                account,
                rules,
                timers,
                pending,
                orders,
                registry,
                attribution,
                covers,
                sid,
                &event,
                now,
            );
        }
        self.drain(now).await
    }

    /// A fresh target book. Every strategy hears it, the way a feed reset is
    /// heard: which of them follows a book is the plug's business, not the
    /// loop's. Nothing arrives here unless a whole book was read, so this is
    /// only ever called with a decision in hand.
    /// One book, to the one strategy that asked for its path.
    ///
    /// Not broadcast. Two sleeves on one account each have their own book, and
    /// the venue holds one position per symbol — so a book delivered to the
    /// wrong follower is that follower trying to hold another sleeve's
    /// positions.
    /// Start following symbols a book named that the engine did not know.
    ///
    /// Every table that maps a name to a `SymbolId` has to gain the symbol in
    /// the same order, because the id is an index assigned by position. Four
    /// of them exist — the engine's own, the public feed's, the venue
    /// gateway's, and the private stream's — and if any two disagreed, an
    /// order meant for one symbol would be sent for another. So this is the
    /// only place that admits, it admits one name at a time, and it checks
    /// that all four agree before the symbol is usable. A disagreement drops
    /// the symbol rather than trading it: the engine carries on with the names
    /// it already had, and says loudly which one it refused.
    async fn admit_wanted<M, O>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
    ) -> Result<(), EngineError>
    where
        M: engine_types::MarketFeed,
        O: engine_types::OrderFeed,
    {
        let wanted = std::mem::take(&mut self.wanted_symbols);
        let mut admitted = 0usize;
        for wanted in wanted {
            let name = wanted.name;
            let core_id = self.market.add_symbol(&name);
            let venue_id = self.venue.add_symbol_async(&name).await?;
            let mut feeds = Vec::new();
            for (_, feed) in &wanted.listeners {
                if !feeds.contains(feed) {
                    feeds.push(*feed);
                }
            }
            let feed_ids: Vec<_> = feeds
                .iter()
                .map(|feed| (*feed, market_feed.admit(&name, *feed)))
                .collect();
            if feed_ids.iter().any(|(_, id)| *id != Some(core_id)) || venue_id != Some(core_id) {
                tracing::error!(
                    symbol = %name,
                    ?core_id,
                    ?feed_ids,
                    ?venue_id,
                    "the parts of the engine disagree about this symbol's id; it will not be \
                     traded. Nothing else is affected — the ids already handed out do not move."
                );
                continue;
            }
            order_feed.learn(&name, core_id);
            self.routing.size_to(self.market.table.len());
            for (strategy, feed) in wanted.listeners {
                self.routing.add(core_id, feed, strategy);
            }
            for feed in feeds {
                let subscription = Subscription {
                    symbol: name.clone(),
                    feed,
                };
                if !self.subscriptions.contains(&subscription) {
                    self.subscriptions.push(subscription);
                }
            }
            admitted += 1;
            tracing::info!(symbol = %name, id = core_id.0, "following a symbol a book named");
        }
        if admitted == 0 {
            return Ok(());
        }
        // The table grew, so say what it is now. Ids are only appended, so
        // this is the earlier one plus the new names.
        let names = names_record(&self.names, &self.market);
        self.wal.append(&names)?;
        self.fills.learn(&names);
        // One venue read covers everything admitted this pass. Without a rule
        // there is no way to quantize, so the symbol is followed but nothing
        // can be sent for it — which is the same state as a symbol whose rule
        // was missing at boot.
        self.rules.resize(self.market.table.len(), None);
        match self.venue.instrument_rules().await {
            Ok(fetched) => {
                for (name, rule) in fetched {
                    if let Some(id) = self.market.table.get(&name) {
                        self.rules[id.0 as usize] = Some(rule);
                    }
                }
            }
            Err(e) => tracing::warn!(
                error = %e,
                "no instrument rules for the symbols just taken on; they cannot trade until \
                 the next attempt"
            ),
        }
        Ok(())
    }

    /// Take a fresh account reading.
    ///
    /// The one place a reading is adopted, so what has to happen with it
    /// cannot be done in one path and forgotten in the other.
    fn adopt_view(&mut self, view: AccountView) {
        self.risk.observe_account_view(&view);
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

    /// Pull every still-live opening order when reconciliation has latched new
    /// exposure off or private-stream continuity is unavailable. The durable
    /// reconciliation state is written before this queue reaches the venue.
    /// Foreign and reduce-only orders are left alone: cancelling another
    /// writer's order or a protective exit is not a safe guess.
    fn queue_halted_entry_cancels(&mut self) -> Result<(), EngineError> {
        if self.may_open && self.private_stream_ready {
            return Ok(());
        }
        let entries: Vec<(SymbolId, String)> = self
            .orders
            .in_flight()
            .into_iter()
            .filter(|order| !order.request.reduce_only)
            .map(|order| (order.request.symbol, order.request.client_order_id.clone()))
            .collect();
        for (symbol, client_order_id) in entries {
            self.enqueue_halt_cancel(symbol, client_order_id);
        }

        let now_ns = clock::now_ns();
        if let Some((client_order_id, _)) = self.halt_cancels.iter().find(|(id, state)| {
            matches!(
                state,
                HaltCancelState::AwaitingPrivate { deadline_ns }
                    if now_ns >= *deadline_ns && self.is_live_opening(id)
            )
        }) {
            return Err(EngineError::State(format!(
                "opening-halt cancellation for {client_order_id} was accepted but not confirmed by the private stream within {} ms; restarting for venue reconciliation",
                HALT_CANCEL_CONFIRM_NS / 1_000_000
            )));
        }
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

    fn enqueue_halt_cancel(&mut self, symbol: SymbolId, client_order_id: String) {
        if self.halt_cancels.contains_key(&client_order_id) {
            return;
        }
        self.halt_cancels
            .insert(client_order_id.clone(), HaltCancelState::Submitting);
        self.halt_cancel_queue.push_back((symbol, client_order_id));
    }

    async fn dispatch_halt_cancel_group(&mut self) -> Result<(), EngineError> {
        let mut requests = Vec::with_capacity(MAX_CANCELS_PER_BATCH);
        while requests.len() < MAX_CANCELS_PER_BATCH {
            let Some((symbol, client_order_id)) = self.halt_cancel_queue.pop_front() else {
                break;
            };
            let live = self.is_live_opening(&client_order_id);
            if live
                && matches!(
                    self.halt_cancels.get(&client_order_id),
                    Some(HaltCancelState::Submitting)
                )
            {
                requests.push((symbol, client_order_id));
            } else if !live {
                self.halt_cancels.remove(&client_order_id);
            }
        }
        self.process_cancels(requests).await.map(|_| ())
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

    async fn on_targets(
        &mut self,
        strategy: StrategyId,
        book: TargetBook,
    ) -> Result<(), EngineError> {
        let now = clock::now_ns();
        // The watcher already logged this book by path and decision stamp;
        // the strategy id here is a per-boot position, not a name.
        tracing::debug!(
            strategy = strategy.0,
            source = %book.source,
            targets = book.targets.len(),
            valid_until_ms = book.valid_until_ms,
            "a target book reached its strategy"
        );
        // A book may name something this engine has never followed. Note it
        // for the run loop, which can reach the feeds; the strategy is still
        // woken now, and will act on the name once it has a price.
        for target in &book.targets {
            let feed = Feed::Quote;
            let subscribed = self
                .subscriptions
                .iter()
                .any(|sub| sub.symbol == target.symbol && sub.feed == feed);
            if let Some(symbol) = self.market.table.get(&target.symbol).filter(|_| subscribed) {
                // The feed is already live (possibly for another sleeve); it
                // is only this requesting strategy's route that is missing.
                self.routing.add(symbol, feed, strategy);
                continue;
            }
            let listener = (strategy, feed);
            if let Some(wanted) = self
                .wanted_symbols
                .iter_mut()
                .find(|wanted| wanted.name == target.symbol)
            {
                if !wanted.listeners.contains(&listener) {
                    wanted.listeners.push(listener);
                }
            } else {
                self.wanted_symbols.push(WantedSymbol {
                    name: target.symbol.clone(),
                    listeners: vec![listener],
                });
            }
        }

        // Pre-arm leverage at book arrival, where nobody is waiting on an
        // order — measured live, the inline confirmation cost entries from
        // flat a ~172 ms round trip (844 ms worst), which was most of the
        // order path's p99. Sole authority only: under shared authority the
        // cache is wiped for flat symbols on every account reading, so an
        // arm here would be forgotten before it could ever be used and
        // re-sent on every book. A failed arm is a warning, not a refusal —
        // the order path still confirms inline before anything is sent.
        if self.leverage_authority == crate::config::LeverageAuthority::Sole {
            for target in &book.targets {
                let Some(id) = self.market.table.get(&target.symbol) else {
                    continue;
                };
                let want = target.leverage;
                if target.notional_usdt == 0.0 || !want.is_finite() || want <= 0.0 {
                    continue;
                }
                if self.leverage_at.get(&id).is_some_and(|at| *at == want) {
                    continue;
                }
                if let Err(reason) = self.ensure_leverage(id, want).await {
                    tracing::warn!(
                        symbol = %target.symbol,
                        want,
                        reason,
                        "could not pre-arm leverage; the entry will confirm inline instead"
                    );
                }
            }
        }

        let event = EngineEvent::Targets(book);
        {
            let Engine {
                strategies,
                market,
                timers,
                pending,
                orders,
                registry,
                attribution,
                covers,
                account,
                rules,
                ..
            } = self;
            feed_strategy(
                strategies,
                market,
                account,
                rules,
                timers,
                pending,
                orders,
                registry,
                attribution,
                covers,
                strategy,
                &event,
                now,
            );
        }
        self.drain(now).await
    }

    async fn on_tick(&mut self) -> Result<(), EngineError> {
        self.wal.flush()?;
        // Rotation is decided here, on the group-flush tick, and nowhere
        // else. The loop is one thread and one task, so this can never fall
        // between an intent's durability barrier and its send — that whole
        // stretch is inside `process_intent`, which has returned before the
        // tick can fire. The restatement is built from live state that the
        // same code as boot's replay maintains, no append can interleave
        // between building it and writing it, and `WalWriter::rotate`
        // carries the byte-level crash-ordering argument: the restatement
        // is durable in the new segment before that segment can be the one
        // boot picks, and a crash anywhere leaves boot on the old segment
        // with nothing invented and nothing lost.
        if self.rotate_after_bytes > 0 && self.wal.segment_size() >= self.rotate_after_bytes {
            let base = self.rotation_base(clock::wall_ms());
            if self.wal.rotate(&base)? {
                tracing::info!(
                    "log rotated: a fresh segment restates the engine's state; the old \
                     segment stays in place as an archive"
                );
            }
        }
        let now = clock::now_ns();
        // First, and on this tick rather than on a market message: it is the
        // cheapest point in the tick, and it is in front of the account
        // refresh below, which is a venue round trip.
        self.beat(now);
        self.record_trades();
        if self.ledger.due(now) {
            let record = self.ledger.record_for_wal(now);
            self.wal.append(&record)?;
            tracing::info!("latency, {}", self.ledger.plain_line(now));
            self.ledger.reset(now);
        }
        // Any markout whose horizon has come round. Written down because a log
        // holds no prices: this is the one execution number that cannot be
        // worked out later from the records already in it.
        for mark in self.fills.due(now, &self.market) {
            self.wal.append(&mark.to_record())?;
        }
        self.checkpoint_history_if_due().await?;
        self.refresh_account_if_due(now).await?;
        self.queue_halted_entry_cancels()?;

        // Every resting entry gets one look. Read the clock again: the
        // account refresh above is a venue round trip, and the stamp from
        // before it is old by the time we get here.
        let now = clock::now_ns();
        if self.may_open && self.private_stream_ready {
            let Engine {
                working,
                market,
                rules,
                orders,
                pending,
                ..
            } = self;
            working.pass(now, market, rules, orders, pending);
        }
        // Through the ordinary queue, so the flood cap counts these too.
        self.drain(now).await
    }

    fn account_refresh_due(&self, now_ns: u64) -> bool {
        now_ns.saturating_sub(self.account.observed_ns) >= self.refresh_after_ns
    }

    async fn refresh_account_if_due(&mut self, now_ns: u64) -> Result<(), EngineError> {
        if !self.account_refresh_due(now_ns) {
            return Ok(());
        }
        match self.venue.account_view().await {
            Ok(view) => {
                self.adopt_view(view);
                self.enforce_position_stop_intent().await?;
            }
            // Keeping the old reading is not the same as trusting it: it
            // ages, and the risk kernel refuses on an old reading.
            Err(e) => tracing::warn!(error = %e, "could not refresh the account reading"),
        }
        Ok(())
    }

    /// Write down what any position that just closed made.
    ///
    /// Drained whether or not a file was configured: the list would otherwise
    /// grow for the life of a process nobody asked to report on itself.
    fn record_trades(&mut self) {
        let closed = self.fills.take_closed();
        if closed.is_empty() {
            return;
        }
        if let Some(trades) = self.trades.as_mut() {
            trades.write(&closed);
        }
    }

    /// Write the heartbeat file, when one was asked for and its own cadence
    /// has come round.
    ///
    /// Nothing here returns an error, because there is nothing an error here
    /// should change: the file is how something outside tells whether this
    /// engine is well, and an engine that stopped trading because it could
    /// not describe itself would be a worse answer than one nobody can see.
    fn beat(&mut self, now_ns: u64) {
        let Engine {
            heartbeat,
            ledger,
            fills,
            names,
            may_open,
            private_stream_ready,
            events_seen,
            orders_sent,
            account,
            market,
            strategies,
            attribution,
            orders,
            covers,
            amends_confirmed,
            amends_pulled_unconfirmed,
            stream_resets,
            ..
        } = self;
        let Some(heartbeat) = heartbeat.as_mut() else {
            return;
        };
        if !heartbeat.due(now_ns) {
            return;
        }
        // Why each asked-for name is not being opened, straight from the
        // strategies. A target producer learns what became of its ask only
        // through this file, and an ask the engine refuses or cannot size is
        // the difference between "on its way" and "squatting on a slot".
        // Strategy identity is part of the key: two sleeves may ask for the
        // same symbol and need their own answer. Within one sleeve the first
        // reason wins, so its kernel refusal still outranks a planner skip.
        let blockers = named_entry_blockers(strategies, names);
        let working_entries: Vec<(String, String)> = orders
            .opening_symbols()
            .chain(covers.opening_symbols())
            .filter_map(|(strategy, symbol)| {
                Some((
                    names.get(usize::from(strategy.0))?.clone(),
                    market.table.name(symbol).to_string(),
                ))
            })
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
        // Named, because the producers that read this file know symbols by
        // name and nothing else. Flat rows are dropped the way every other
        // reader of this view drops them: flat is not a holding.
        let holdings: Vec<(String, engine_types::Side, f64, f64, Option<String>)> = account
            .positions
            .iter()
            .filter(|p| p.qty > 0.0)
            .map(|p| {
                (
                    market.table.name(p.symbol).to_string(),
                    p.side,
                    p.qty,
                    p.entry_px,
                    attribution
                        .sole_owner(p.symbol)
                        .filter(|owner| {
                            let venue_signed = match p.side {
                                engine_types::Side::Buy => p.qty,
                                engine_types::Side::Sell => -p.qty,
                            };
                            (attribution.signed(*owner, p.symbol) - venue_signed).abs() < 1e-9
                        })
                        .and_then(|owner| names.get(usize::from(owner.0)))
                        .cloned(),
                )
            })
            .collect();
        // Rolled up once, here, rather than kept as a running total: the
        // per-sleeve rows are what the ledger is for, and adding them up is
        // cheaper than keeping a second copy correct.
        let costs = fills.total();
        let effective_may_open = *may_open && *private_stream_ready;
        // How far the venue's clock sits from this box's, read off the
        // freshest quote: its venue stamp against the wall clock, minus the
        // time it has spent here since the socket read. Both clocks are
        // sampled together, here, where the number is made. A drifting box
        // makes every venue-stamp comparison quietly wrong, and nothing else
        // measures that.
        let wall_ts_ms = clock::wall_ms();
        let venue_clock_offset_ms = market
            .quotes
            .iter()
            .filter(|quote| quote.venue_ts_ms > 0 && quote.recv_ns > 0)
            .max_by_key(|quote| quote.recv_ns)
            .map(|quote| {
                venue_minus_local_ms(quote.venue_ts_ms, quote.recv_ns, now_ns, wall_ts_ms)
            });
        heartbeat.write(
            now_ns,
            &heartbeat::Facts {
                may_open: effective_may_open,
                market_events: *events_seen,
                orders_sent: *orders_sent,
                strategies: names,
                decide: ledger.quantiles(Segment::Decide),
                durable: ledger.quantiles(Segment::Durable),
                wire: ledger.quantiles(Segment::Wire),
                ack: ledger.quantiles(Segment::Ack),
                dispatch_queue: ledger.quantiles(Segment::DispatchQueue),
                venue_task: ledger.quantiles(Segment::VenueTask),
                core_resume: ledger.quantiles(Segment::CoreResume),
                end_to_end: ledger.quantiles(Segment::EndToEnd),
                barrier_wait: ledger.quantiles(Segment::BarrierWait),
                quota_hold: ledger.quantiles(Segment::QuotaHold),
                amends_confirmed: *amends_confirmed,
                amends_pulled_unconfirmed: *amends_pulled_unconfirmed,
                stream_resets: *stream_resets,
                // The monotonic clock's origin is this process's first tick,
                // so "now" on it is the age of the run.
                uptime_s: now_ns / 1_000_000_000,
                venue_clock_offset_ms,
                equity_usdt: account.equity_usdt,
                available_usdt: account.available_usdt,
                // The age, not the stamp: this engine's clock is monotonic
                // and means nothing outside this process.
                account_age_ns: (account.observed_ns != 0)
                    .then(|| now_ns.saturating_sub(account.observed_ns)),
                holdings: &holdings,
                entry_blockers: &blockers,
                working_entries: &working_entries,
                costs: &costs,
            },
        );
    }

    async fn drain(&mut self, origin_ns: u64) -> Result<(), EngineError> {
        self.pull_unconfirmed_amends()?;
        let mut progress = self.drain_progress.take().unwrap_or(DrainProgress {
            origin_ns,
            handled: 0,
            adding_dropped: 0,
        });
        let mut placements = Vec::new();
        let mut cancellations = Vec::new();
        let mut hard_cap_hit = false;
        loop {
            if self.pending.is_empty() {
                self.load_ready_wake(&mut progress);
            }
            while let Some(action) = self.pending.pop_front() {
                if let Action::RecordQuoteFill { features } = action {
                    self.wal.append(&WalRecord::QuoteFill { features })?;
                    continue;
                }
                progress.handled += 1;
                // Past the cap, whatever adds risk is dropped but whatever sheds
                // it still flows: an exit or a cancel queued behind a flood must
                // get out, or its strategy is stranded holding a position — or an
                // order — it believes it is rid of. An amend counts as adding: it
                // can raise the size of a resting order. The hard cap bounds even
                // the de-risking ones against a runaway loop.
                if progress.handled > MAX_INTENTS_PER_WAKE && !action.is_risk_reducing() {
                    progress.adding_dropped += 1;
                    continue;
                }
                if progress.handled > MAX_INTENTS_PER_WAKE * 4 {
                    let dropped = self.pending.len() + 1;
                    self.pending.clear();
                    hard_cap_hit = true;
                    tracing::error!(
                        dropped,
                        "far too many actions in one wake; the rest were dropped"
                    );
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!(
                            "dropped {dropped} actions, exits included: more than {} in one wake",
                            MAX_INTENTS_PER_WAKE * 4
                        ),
                    })?;
                    break;
                }

                let symbol = action.symbol();
                if self.busy_symbols.contains_key(&symbol) {
                    self.defer_action(action, progress.origin_ns);
                    continue;
                }

                // Do not cross a placement boundary with the next verb
                // already consumed. If a real send happened, put this action
                // back at the front and let the run loop poll account-safety
                // inputs before resuming the same FIFO wake.
                if !matches!(&action, Action::Place(_)) && !placements.is_empty() {
                    let sent = self
                        .process_intents(std::mem::take(&mut placements), progress.origin_ns)
                        .await?;
                    if sent {
                        progress.handled -= 1;
                        self.pending.push_front(action);
                        return self.pause_drain(progress);
                    }
                }

                // Ordinary cancels share the same cooperative boundary. A
                // run of cancels accumulates into one native-sized request;
                // flush it before a different verb, then resume that verb on
                // the next turn.
                let accumulates_cancel = matches!(&action, Action::Cancel { .. });
                if !accumulates_cancel && !cancellations.is_empty() {
                    let sent = self
                        .process_cancels(std::mem::take(&mut cancellations))
                        .await?;
                    if sent {
                        progress.handled -= 1;
                        self.pending.push_front(action);
                        return self.pause_drain(progress);
                    }
                }
                match action {
                    Action::Place(intent) => {
                        placements.push(intent);
                        if placements.len() == MAX_ORDERS_PER_BATCH {
                            let sent = self
                                .process_intents(
                                    std::mem::take(&mut placements),
                                    progress.origin_ns,
                                )
                                .await?;
                            if sent && !self.pending.is_empty() {
                                return self.pause_drain(progress);
                            }
                        }
                    }
                    Action::Cancel {
                        symbol,
                        client_order_id,
                    } => {
                        cancellations.push((symbol, client_order_id));
                        if cancellations.len() == MAX_CANCELS_PER_BATCH {
                            let sent = self
                                .process_cancels(std::mem::take(&mut cancellations))
                                .await?;
                            if sent && !self.pending.is_empty() {
                                return self.pause_drain(progress);
                            }
                        }
                    }
                    Action::Amend {
                        symbol,
                        client_order_id,
                        spec,
                    } => {
                        let taken = self
                            .process_amend(symbol, &client_order_id, spec, progress.origin_ns)
                            .await?;
                        self.working
                            .amended(&client_order_id, spec.px, taken, clock::now_ns());
                        if !self.pending.is_empty() {
                            return self.pause_drain(progress);
                        }
                    }
                    Action::SetStop { symbol, trigger_px } => {
                        self.process_set_stop(symbol, trigger_px).await?;
                        if !self.pending.is_empty() {
                            return self.pause_drain(progress);
                        }
                    }
                    Action::RecordQuoteFill { .. } => {
                        unreachable!("quote-fill receipts are journaled before venue actions")
                    }
                }
            }
            let sent = self
                .process_intents(std::mem::take(&mut placements), progress.origin_ns)
                .await?;
            if sent && !hard_cap_hit && !self.pending.is_empty() {
                return self.pause_drain(progress);
            }
            let cancelled = self
                .process_cancels(std::mem::take(&mut cancellations))
                .await?;
            if cancelled && !hard_cap_hit && !self.pending.is_empty() {
                return self.pause_drain(progress);
            }
            if !hard_cap_hit && self.pending.is_empty() && !self.ready_actions.is_empty() {
                self.load_ready_wake(&mut progress);
                continue;
            }
            if hard_cap_hit || self.pending.is_empty() {
                break;
            }
        }
        if progress.adding_dropped > 0 {
            tracing::error!(
                adding_dropped = progress.adding_dropped,
                "too many actions in one wake; entries and amends were dropped"
            );
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "dropped {} entries and amends: more than {MAX_INTENTS_PER_WAKE} actions in one wake (exits and cancels still flowed)",
                    progress.adding_dropped
                ),
            })?;
        }
        Ok(())
    }

    /// End one venue-mutation turn without ending its strategy wake. The
    /// batch has completed its record/send/reply sequence (and, for entries,
    /// its durability barrier); this only keeps the flood counters and
    /// latency origin while the run loop polls account-safety inputs.
    fn pause_drain(&mut self, progress: DrainProgress) -> Result<(), EngineError> {
        self.drain_progress = Some(progress);
        Ok(())
    }

    fn defer_action(&mut self, action: Action, origin_ns: u64) {
        let queue = self.deferred_actions.entry(action.symbol()).or_default();
        match &action {
            Action::Amend {
                client_order_id, ..
            } => {
                if queue.iter().any(|(queued, _)| {
                    matches!(queued, Action::Cancel { client_order_id: queued_id, .. } if queued_id == client_order_id)
                }) {
                    return;
                }
                queue.retain(|(queued, _)| {
                    !matches!(queued, Action::Amend { client_order_id: queued_id, .. } if queued_id == client_order_id)
                });
            }
            Action::Cancel {
                client_order_id, ..
            } => {
                if queue.iter().any(|(queued, _)| {
                    matches!(queued, Action::Cancel { client_order_id: queued_id, .. } if queued_id == client_order_id)
                }) {
                    return;
                }
                queue.retain(|(queued, _)| {
                    !matches!(queued, Action::Amend { client_order_id: queued_id, .. } if queued_id == client_order_id)
                });
            }
            Action::SetStop { .. } => {
                queue.retain(|(queued, _)| !matches!(queued, Action::SetStop { .. }));
            }
            Action::Place(intent)
                if !intent.reduce_only
                    && intent.tag == "quote"
                    && matches!(
                        intent.kind,
                        OrderKind::Limit {
                            tif: TimeInForce::PostOnly,
                            ..
                        }
                    ) =>
            {
                queue.retain(|(queued, _)| {
                    !matches!(
                        queued,
                        Action::Place(older)
                            if !older.reduce_only
                                && older.strategy == intent.strategy
                                && older.side == intent.side
                                && older.tag == intent.tag
                                && matches!(
                                    older.kind,
                                    OrderKind::Limit {
                                        tif: TimeInForce::PostOnly,
                                        ..
                                    }
                                )
                    )
                });
            }
            Action::Place(_) => {}
            Action::RecordQuoteFill { .. } => {}
        }
        queue.push_back((action, origin_ns));
    }

    fn load_ready_wake(&mut self, progress: &mut DrainProgress) {
        let Some((action, origin_ns)) = self.ready_actions.pop_front() else {
            return;
        };
        *progress = DrainProgress {
            origin_ns,
            handled: 0,
            adding_dropped: 0,
        };
        self.pending.push_back(action);
        while self
            .ready_actions
            .front()
            .is_some_and(|(_, queued_origin)| *queued_origin == origin_ns)
        {
            let (action, _) = self.ready_actions.pop_front().expect("front checked above");
            self.pending.push_back(action);
        }
    }

    fn mark_symbols_busy(&mut self, symbols: impl IntoIterator<Item = SymbolId>) {
        for symbol in symbols {
            *self.busy_symbols.entry(symbol).or_default() += 1;
        }
    }

    fn release_symbols(&mut self, symbols: impl IntoIterator<Item = SymbolId>) {
        let mut ready = Vec::new();
        for symbol in symbols {
            let Some(count) = self.busy_symbols.get_mut(&symbol) else {
                continue;
            };
            *count -= 1;
            if *count == 0 {
                self.busy_symbols.remove(&symbol);
                ready.push(symbol);
            }
        }
        for symbol in ready {
            if let Some(mut queued) = self.deferred_actions.remove(&symbol) {
                while let Some((action, origin_ns)) = queued.pop_front() {
                    self.ready_actions.push_back((action, origin_ns));
                }
            }
        }
    }

    /// Judge and reserve one sibling. The caller makes every accepted sibling
    /// durable together before asking the venue to send any of them.
    async fn prepare_intent(
        &mut self,
        intent: Intent,
        origin_ns: u64,
        batch_protection: &mut std::collections::HashMap<(u16, bool), f64>,
    ) -> Result<Option<PreparedOrder>, EngineError> {
        let decided_ns = if intent.decided_ns > 0 {
            intent.decided_ns
        } else {
            clock::now_ns()
        };
        self.ledger
            .record(Segment::Decide, decided_ns.saturating_sub(origin_ns));

        // A non-finite number would be written to the log as null and stop
        // the next boot's replay dead, so it is refused before any append.
        if let Some(what) = unreal_number(&intent) {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "intent {} refused: {what} is not a finite number",
                    intent.tag
                ),
            })?;
            tracing::error!(tag = %intent.tag, what, "intent carries an unreal number");
            self.tell_refused(&intent, "unreal_number");
            return Ok(None);
        }

        // The strategy's own words, work policy included, before the engine
        // touches anything.
        self.wal.append(&WalRecord::Intent {
            intent: intent.clone(),
        })?;

        // A REST account read cannot replace a private-stream continuity
        // proof: it may predate a fill that the disconnected stream missed.
        // Reconnect handling refreshes the view and recovers execution
        // history before setting this bit again.
        if !self.private_stream_ready && !intent.reduce_only {
            let verdict = RiskVerdict::Deny {
                reason: DenyReason::UnknownState {
                    detail: "private account stream has not completed gap recovery".to_string(),
                },
            };
            self.wal.append(&WalRecord::Verdict {
                client_order_id: None,
                verdict,
            })?;
            tracing::warn!(tag = %intent.tag, "refused: private account stream is not ready");
            self.tell_refused(&intent, "private_stream_unready");
            return Ok(None);
        }

        // Boot found orders or exposure this log cannot account for, which
        // means somebody else is on this account and every number the kernel
        // works from is measuring their trading too. Reducing is still
        // allowed — taking exposure off is safe whoever put it on — but
        // nothing new is added until an operator has looked.
        if !self.may_open && !intent.reduce_only {
            let verdict = RiskVerdict::Deny {
                reason: DenyReason::UnknownState {
                    detail: "boot could not account for what this account holds".to_string(),
                },
            };
            self.wal.append(&WalRecord::Verdict {
                client_order_id: None,
                verdict,
            })?;
            tracing::warn!(
                tag = %intent.tag,
                "refused: this engine is not opening new positions after what boot found"
            );
            self.tell_refused(&intent, "engine_latched");
            return Ok(None);
        }

        // The quote this decision was priced against, bounded the way the
        // account reading already is: a price older than the bound is not
        // evidence about the market now. The feed pings and reconnects on
        // its own, but a silent-but-alive socket, or a reconnect that never
        // lands, leaves the last quote standing — and this is the one place
        // that refuses to OPEN against it. The stamp is the quote's own
        // receive time on the engine's monotonic clock, never a wall-clock
        // guess; a symbol that has never quoted has no stamp at all, and
        // the absence of a price is the stalest price there is (a feed
        // reset clears every stamp for the same reason). Exits flow
        // whatever the age — taking risk off must never wait on a fresh
        // price — and cancels and amends of protective orders never come
        // through here at all.
        if !intent.reduce_only {
            let quote_ns = self
                .market
                .quotes
                .get(intent.symbol.0 as usize)
                .map(|quote| quote.recv_ns)
                .unwrap_or(0);
            let age_ns = decided_ns.saturating_sub(quote_ns);
            if quote_ns == 0 || age_ns > self.max_quote_age_ns {
                let verdict = RiskVerdict::Deny {
                    reason: DenyReason::StaleQuote {
                        age_ns,
                        max_age_ns: self.max_quote_age_ns,
                    },
                };
                self.wal.append(&WalRecord::Verdict {
                    client_order_id: None,
                    verdict,
                })?;
                tracing::warn!(
                    tag = %intent.tag,
                    symbol = self.market.table.name(intent.symbol),
                    age_ms = age_ns / 1_000_000,
                    never_quoted = quote_ns == 0,
                    "refused: the quote this entry was decided against is too old to open on"
                );
                self.tell_refused(&intent, "stale_quote");
                return Ok(None);
            }
        }

        // An entry the strategy asked to have worked starts as a resting
        // limit instead of crossing the spread. Rewritten here, before the
        // kernel judges it, so the kernel judges the order that is actually
        // sent — and before the id is minted, so a refusal still costs
        // nothing.
        let mut intent = intent;
        let work = self.plan_resting_entry(&mut intent);

        let verdict = {
            let Engine { risk, account, .. } = self;
            risk.assess(&intent, account)
        };
        let verdict = durable_risk_verdict(verdict, intent.qty, false);
        let allowed_qty = match &verdict {
            RiskVerdict::Allow { qty } => *qty,
            RiskVerdict::Deny { reason } => {
                let reason = format!("{reason:?}");
                self.wal.append(&WalRecord::Verdict {
                    client_order_id: None,
                    verdict,
                })?;
                tracing::info!(tag = %intent.tag, reason, "risk refused the order");
                self.tell_refused(&intent, &reason);
                return Ok(None);
            }
        };

        // Minting the id here (not a log write) lets the verdict record name
        // the order it approved; a refused intent never burns an id.
        let client_order_id = self.mint_id();
        self.wal.append(&WalRecord::Verdict {
            client_order_id: Some(client_order_id.clone()),
            verdict,
        })?;

        // The risk kernel requires a position-opening intent to carry a stop.
        // A venue that keeps no stop of its own would leave that rule
        // unenforced without ever saying so: the order goes out, the log
        // records a stop, and nothing at the venue is watching the position.
        // An exit sheds its stop below in any case, so it is not held back.
        if intent.stop.is_some() && !intent.reduce_only && !self.venue.caps().native_position_stop {
            self.refuse(
                &client_order_id,
                &intent,
                "the intent carries a stop and this venue keeps none",
            )?;
            return Ok(None);
        }

        let Some(rule) = self.rules.get(intent.symbol.0 as usize).copied().flatten() else {
            self.refuse(
                &client_order_id,
                &intent,
                "no instrument rule for this symbol",
            )?;
            return Ok(None);
        };
        let kind = match intent.kind {
            OrderKind::Market => OrderKind::Market,
            OrderKind::Limit { px, tif } => OrderKind::Limit {
                px: quantize::quantize_px(px, intent.side, &rule),
                tif,
            },
        };
        let mut held = self
            .account
            .positions
            .iter()
            .filter(|position| position.symbol == intent.symbol && position.qty > 0.0);
        let held_position = held.next().map(|position| (position.side, position.qty));
        let one_position = held.next().is_none();
        let close_position_candidate = intent.reduce_only
            && matches!(intent.kind, OrderKind::Market)
            && self.venue.caps().close_position_below_minimum
            && one_position
            && held_position.is_some_and(|(side, qty)| {
                let tolerance = rule.qty_step.max(1e-12) * 1e-9;
                side == intent.side.flipped() && (allowed_qty - qty).abs() <= tolerance
            });
        let held_qty = held_position.map(|(_, qty)| qty).unwrap_or(0.0);
        let close_below_minimum_qty = close_position_candidate && held_qty + 1e-12 < rule.min_qty;
        let mut close_below_minimum_value = false;
        if close_position_candidate {
            if let Some(reference_px) = self.reference_px(intent.symbol, &kind) {
                close_below_minimum_value = held_qty * reference_px + 1e-9 < rule.min_notional;
            }
        }
        let close_position =
            close_position_candidate && (close_below_minimum_qty || close_below_minimum_value);
        let qty = if close_position {
            // Bybit receives qty=0 for this request and closes the whole venue
            // position. The WAL keeps the actual held quantity so its fill can
            // be validated and accounted without inventing one venue step.
            held_qty
        } else if let Some(qty) = quantize::quantize_qty(allowed_qty, &rule) {
            qty
        } else {
            self.refuse(
                &client_order_id,
                &intent,
                &format!(
                    "{allowed_qty} does not reach the smallest tradable size ({} step, {} minimum)",
                    rule.qty_step, rule.min_qty
                ),
            )?;
            return Ok(None);
        };
        if let Some(reference_px) = self.reference_px(intent.symbol, &kind) {
            let notional = qty * reference_px;
            if notional + 1e-9 < rule.min_notional && !close_position {
                self.refuse(
                    &client_order_id,
                    &intent,
                    &format!(
                        "{notional:.4} is under the venue's smallest order value ({})",
                        rule.min_notional
                    ),
                )?;
                return Ok(None);
            }
        }

        let request = OrderRequest {
            client_order_id: client_order_id.clone(),
            strategy: intent.strategy,
            symbol: intent.symbol,
            side: intent.side,
            qty,
            kind,
            // The venue rejects a reduce-only order that carries stop
            // fields, so an exit sheds its stop here — the log records what
            // is actually sent. An entry's stop is quantized against the
            // instrument tick, rounded toward triggering sooner.
            stop: if intent.reduce_only {
                None
            } else {
                intent.stop.map(|s| StopSpec {
                    trigger_px: quantize::quantize_px(s.trigger_px, intent.side.flipped(), &rule),
                })
            },
            reduce_only: intent.reduce_only,
            close_position,
        };

        // Before the durable record, because a leverage that could not be set
        // means this order must not go at all — and an OrderSent record is
        // the engine saying it is about to put one on the wire.
        //
        // Entries only. An exit at the wrong leverage is still an exit, and
        // making it wait on a round trip would be the wrong trade.
        if !intent.reduce_only {
            if let Some(want) = intent.leverage {
                if let Err(reason) = self.ensure_leverage(request.symbol, want).await {
                    self.refuse(&client_order_id, &intent, &reason)?;
                    return Ok(None);
                }
            }
        }

        // Bybit's Full TP/SL belongs to the entire one-way position. A later
        // same-side fill with a looser stop would therefore weaken units that
        // were already protected. Hold each same-side batch chain against
        // both the fresh account view and durable fill-owned intent; only
        // equal or tighter protection may reach the wire.
        if let Some(stop) = request.stop.filter(|_| !request.reduce_only) {
            let key = stop_key(request.symbol, request.side);
            let tolerance = self
                .rules
                .get(request.symbol.0 as usize)
                .and_then(|rule| rule.as_ref())
                .map(|rule| rule.tick_size / 2.0)
                .unwrap_or(1e-9);
            if let Some(protected) = batch_protection.get(&key).copied() {
                if stop_is_looser(request.side, stop.trigger_px, protected, tolerance) {
                    self.refuse(
                        &client_order_id,
                        &intent,
                        &format!(
                            "stop {} would loosen the whole {:?} position from {}",
                            stop.trigger_px, request.side, protected
                        ),
                    )?;
                    return Ok(None);
                }
                batch_protection
                    .insert(key, tighter_stop(request.side, protected, stop.trigger_px));
            } else {
                batch_protection.insert(key, stop.trigger_px);
            }
        }

        // Appended before reservation. The caller forces every accepted
        // sibling to disk together before any request leaves the process.
        let sent_record = WalRecord::OrderSent {
            request: request.clone(),
            wire_ns: clock::now_ns(),
            // `M0`. Read here rather than at the fill because this is the only
            // moment it exists: a worked entry can rest for a minute, and by
            // the time it fills the price it was decided against is gone.
            // Zero when the book was unreadable, which makes every arrival
            // number for this order missing rather than flattering.
            arrival_mid: self.decision_mid(request.symbol),
        };
        self.wal.append(&sent_record)?;
        self.orders.apply(&sent_record);
        self.registry.own(&client_order_id, intent.strategy);
        // The engine's own note of what just went out, at the size that
        // actually went — strategies read it back as `ctx.in_flight`, so the
        // window between a fill and the next account reading cannot look flat.
        if request.reduce_only {
            self.covers.register_reduce(
                intent.strategy,
                request.symbol,
                request.side,
                qty,
                &self.account,
            );
        } else {
            self.covers.register(
                intent.strategy,
                request.symbol,
                request.side,
                qty,
                &self.account,
            );
        }
        self.risk.register_order(&client_order_id, &intent, qty);
        self.orders_sent += 1;

        // Start working it from the price that is actually resting — the
        // quantized one, not the one the planner asked for.
        if let (Some(policy), OrderKind::Limit { px, .. }) = (work, request.kind) {
            let mid = self.decision_mid(request.symbol);
            let state = working::plan::WorkState::new(request.side, px, mid, clock::now_ns());
            self.working
                .take_on(&client_order_id, request.symbol, policy, state);
        }

        Ok(Some(PreparedOrder {
            request,
            decided_ns,
            origin_ns,
        }))
    }

    async fn process_intents(
        &mut self,
        intents: Vec<Intent>,
        origin_ns: u64,
    ) -> Result<bool, EngineError> {
        if intents.len() > MAX_ORDERS_PER_BATCH {
            return Err(EngineError::State(format!(
                "placement batch has {} orders; hard maximum is {MAX_ORDERS_PER_BATCH}",
                intents.len()
            )));
        }
        // Leverage is venue-global per symbol. If two siblings require
        // different valid leverage values, setting A and then B before the
        // concurrent send would put A on the wire at B despite having been
        // sized and approved at A. There is no safe ordering once both are
        // meant to become live together, so refuse every opening sibling for
        // that symbol. Reduce-only exits still flow and never change leverage.
        let mut leverage_by_symbol = std::collections::HashMap::new();
        let mut leverage_conflicts = std::collections::HashSet::new();
        for intent in &intents {
            let Some(want) = intent
                .leverage
                .filter(|value| value.is_finite() && *value > 0.0)
            else {
                continue;
            };
            if intent.reduce_only {
                continue;
            }
            match leverage_by_symbol.insert(intent.symbol, want) {
                Some(previous) if previous != want => {
                    leverage_conflicts.insert(intent.symbol);
                }
                _ => {}
            }
        }

        let mut prepared = Vec::with_capacity(intents.len());
        let mut batch_protection = std::collections::HashMap::new();
        for (symbol, stop) in &self.intended_stops {
            batch_protection.insert(stop_key(SymbolId(*symbol), stop.side), stop.trigger_px);
        }
        for (key, trigger_px) in self.orders.tightest_opening_stops() {
            let side = if key.1 { Side::Sell } else { Side::Buy };
            batch_protection
                .entry(key)
                .and_modify(|protected| *protected = tighter_stop(side, *protected, trigger_px))
                .or_insert(trigger_px);
        }
        for position in &self.account.positions {
            if !position.stop_attached || !position.stop_px.is_finite() || position.stop_px <= 0.0 {
                continue;
            }
            let key = stop_key(position.symbol, position.side);
            batch_protection
                .entry(key)
                .and_modify(|protected| {
                    *protected = tighter_stop(position.side, *protected, position.stop_px)
                })
                .or_insert(position.stop_px);
        }
        for intent in intents {
            if !intent.reduce_only
                && leverage_conflicts.contains(&intent.symbol)
                // Keep non-finite values out of the WAL. `prepare_intent`
                // owns that refusal and performs it before any append.
                && unreal_number(&intent).is_none()
            {
                self.wal.append(&WalRecord::Intent {
                    intent: intent.clone(),
                })?;
                let reason = "same-symbol sibling batch asks for conflicting leverage values";
                self.wal.append(&WalRecord::Note {
                    source: "leverage".to_string(),
                    text: format!("intent {} refused: {reason}", intent.tag),
                })?;
                tracing::error!(
                    symbol = self.market.table.name(intent.symbol),
                    tag = %intent.tag,
                    "refused leverage-conflicting sibling batch"
                );
                self.tell_refused(&intent, "batch_leverage_conflict");
                continue;
            }
            if let Some(order) = self
                .prepare_intent(intent, origin_ns, &mut batch_protection)
                .await?
            {
                prepared.push(order);
            }
        }
        if prepared.is_empty() {
            return Ok(false);
        }

        // The bytes go to the operating system here, and the disk barrier
        // runs beside the send rather than in front of it. What waits for the
        // disk is `settle_barrier`, called before any order news is acted on:
        // a fill cannot reach us until the venue has had a round trip, which
        // is longer than the barrier, and no fill is ever *processed* before
        // the order that earned it is durable.
        //
        // What this gives up, stated plainly: a machine that dies inside the
        // barrier can leave an order at the venue that the log does not name.
        // Boot reconciliation sees that as a foreign order and latches
        // opening off, which is the same answer it gives for any order it
        // cannot account for.
        self.settle_barrier()?;
        self.pending_barrier = Some(self.wal.barrier_begin()?);
        let durable_ns = clock::now_ns();
        for order in &prepared {
            self.ledger.record(
                Segment::Durable,
                durable_ns.saturating_sub(order.decided_ns),
            );
        }

        let mut requests = Vec::with_capacity(prepared.len());
        let mut timings = Vec::with_capacity(prepared.len());
        for order in prepared {
            requests.push(order.request);
            timings.push((order.decided_ns, order.origin_ns));
        }

        let queued_ns = clock::now_ns();
        let command_id = self.venue.dispatch_orders(requests.clone())?;
        self.mark_symbols_busy(requests.iter().map(|request| request.symbol));
        self.pending_mutations.insert(
            command_id,
            PendingMutation::Orders {
                requests,
                timings,
                queued_ns,
            },
        );
        Ok(true)
    }

    async fn take_venue_completion(
        &mut self,
        completion: MutationCompletion,
    ) -> Result<(), EngineError> {
        let command_id = match &completion {
            MutationCompletion::Orders { command_id, .. }
            | MutationCompletion::Cancels { command_id, .. }
            | MutationCompletion::Amend { command_id, .. } => *command_id,
        };
        let pending = self.pending_mutations.remove(&command_id).ok_or_else(|| {
            EngineError::State(format!(
                "venue task returned unknown mutation command {command_id}"
            ))
        })?;

        match (pending, completion) {
            (
                PendingMutation::Orders {
                    requests,
                    timings,
                    queued_ns,
                },
                MutationCompletion::Orders {
                    started_ns,
                    completed_ns,
                    rate_wait_ns,
                    replies,
                    ..
                },
            ) => {
                if let Some(held) = rate_wait_ns {
                    self.ledger.record(Segment::QuotaHold, held);
                }
                if replies.len() != requests.len() {
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!(
                            "venue returned {} answers for {} submitted orders; missing answers remain in flight",
                            replies.len(),
                            requests.len()
                        ),
                    })?;
                }
                tracing::debug!(
                    command_id,
                    queue_ns = started_ns.saturating_sub(queued_ns),
                    venue_ns = completed_ns.saturating_sub(started_ns),
                    "placement command completed"
                );
                let symbols: Vec<_> = requests.iter().map(|request| request.symbol).collect();
                let mut replies = replies.into_iter();
                for (request, (decided_ns, origin_ns)) in requests.into_iter().zip(timings) {
                    let core_handled_ns = clock::now_ns();
                    self.ledger
                        .record(Segment::Wire, completed_ns.saturating_sub(decided_ns));
                    self.ledger
                        .record(Segment::DispatchQueue, started_ns.saturating_sub(queued_ns));
                    self.ledger
                        .record(Segment::VenueTask, completed_ns.saturating_sub(started_ns));
                    self.ledger.record(
                        Segment::CoreResume,
                        core_handled_ns.saturating_sub(completed_ns),
                    );
                    let reply = replies.next().unwrap_or_else(|| {
                        Err(VenueError::BadReply(
                            "the venue omitted this order from its batch reply".to_string(),
                        ))
                    });
                    let (socket_write_ns, ack_timing_ns) = match &reply {
                        Ok(ack) => (
                            (ack.sent_ns > 0).then_some(ack.sent_ns),
                            Some(if ack.ack_ns > started_ns {
                                ack.ack_ns
                            } else {
                                completed_ns
                            }),
                        ),
                        Err(_) => (None, None),
                    };
                    self.wal.append(&WalRecord::VenueTiming {
                        command_id,
                        operation: "place".to_string(),
                        client_order_id: request.client_order_id.clone(),
                        queued_ns,
                        task_started_ns: started_ns,
                        socket_write_ns,
                        ack_ns: ack_timing_ns,
                        rate_wait_ns,
                        task_completed_ns: completed_ns,
                        core_handled_ns,
                        core_handled_wall_ns: clock::wall_ns(),
                    })?;
                    let update = match reply {
                        Ok(ack) => {
                            let ack_ns = if ack.ack_ns > started_ns {
                                ack.ack_ns
                            } else {
                                completed_ns
                            };
                            if ack.sent_ns > 0 {
                                self.ledger
                                    .record(Segment::Ack, ack_ns.saturating_sub(ack.sent_ns));
                            }
                            Some(OrderUpdate::Ack(ack))
                        }
                        Err(VenueError::Rejected { code, message }) => Some(OrderUpdate::Reject {
                            client_order_id: request.client_order_id.clone(),
                            code,
                            reason: message,
                        }),
                        Err(VenueError::BadRequest(detail)) => Some(OrderUpdate::Reject {
                            client_order_id: request.client_order_id.clone(),
                            code: 0,
                            reason: format!("never sent: {detail}"),
                        }),
                        Err(other) => {
                            tracing::error!(id = %request.client_order_id, error = %other, "send failed with no answer");
                            self.wal.append(&WalRecord::Note {
                                source: "engine".into(),
                                text: format!(
                                    "{} sent with no answer ({other}); still counted as in flight",
                                    request.client_order_id
                                ),
                            })?;
                            None
                        }
                    };
                    self.ledger
                        .record(Segment::EndToEnd, clock::now_ns().saturating_sub(origin_ns));
                    if let Some(update) = update {
                        self.take_update(update).await?;
                    }
                }
                self.release_symbols(symbols);
            }
            (
                PendingMutation::Cancels {
                    requests,
                    queued_ns,
                },
                MutationCompletion::Cancels {
                    started_ns,
                    completed_ns,
                    timing,
                    rate_wait_ns,
                    replies,
                    ..
                },
            ) => {
                if let Some(held) = rate_wait_ns {
                    self.ledger.record(Segment::QuotaHold, held);
                }
                if replies.len() != requests.len() {
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!(
                            "venue returned {} answers for {} submitted cancels; missing answers remain in flight",
                            replies.len(),
                            requests.len()
                        ),
                    })?;
                }
                tracing::debug!(
                    command_id,
                    venue_ns = completed_ns.saturating_sub(started_ns),
                    "cancel command completed"
                );
                let symbols: Vec<_> = requests.iter().map(|(symbol, _)| *symbol).collect();
                if let Some(mark) = timing {
                    self.ledger
                        .record(Segment::Ack, mark.ack_ns.saturating_sub(mark.sent_ns));
                }
                let mut replies = replies.into_iter();
                let mut halt_failure = None;
                let accepted_deadline = clock::now_ns().saturating_add(HALT_CANCEL_CONFIRM_NS);
                for (_, client_order_id) in requests {
                    let core_handled_ns = clock::now_ns();
                    self.ledger
                        .record(Segment::DispatchQueue, started_ns.saturating_sub(queued_ns));
                    self.ledger
                        .record(Segment::VenueTask, completed_ns.saturating_sub(started_ns));
                    self.ledger.record(
                        Segment::CoreResume,
                        core_handled_ns.saturating_sub(completed_ns),
                    );
                    self.wal.append(&WalRecord::VenueTiming {
                        command_id,
                        operation: "cancel".to_string(),
                        client_order_id: client_order_id.clone(),
                        queued_ns,
                        task_started_ns: started_ns,
                        socket_write_ns: timing.map(|mark| mark.sent_ns),
                        ack_ns: timing.map(|mark| mark.ack_ns),
                        rate_wait_ns,
                        task_completed_ns: completed_ns,
                        core_handled_ns,
                        core_handled_wall_ns: clock::wall_ns(),
                    })?;
                    let reply = replies.next().unwrap_or_else(|| {
                        Err(VenueError::BadReply(
                            "the venue omitted this order from its cancel-batch reply".to_string(),
                        ))
                    });
                    let taken = match reply {
                        Ok(()) => true,
                        Err(VenueError::BadRequest(detail)) => {
                            tracing::error!(id = client_order_id, detail, "cancel never sent");
                            self.wal.append(&WalRecord::Note {
                                source: "engine".into(),
                                text: format!("cancel of {client_order_id} never sent: {detail}"),
                            })?;
                            if self.halt_cancels.contains_key(&client_order_id) {
                                halt_failure = Some(format!("{client_order_id}: {detail}"));
                            }
                            false
                        }
                        Err(VenueError::Rejected { code, message }) => {
                            tracing::error!(id = client_order_id, code, message, "cancel rejected");
                            self.wal.append(&WalRecord::Note {
                                source: "engine".into(),
                                text: format!(
                                    "cancel of {client_order_id} rejected ({code}: {message}); the order is still counted as working"
                                ),
                            })?;
                            if self.halt_cancels.contains_key(&client_order_id) {
                                halt_failure =
                                    Some(format!("{client_order_id}: {code}: {message}"));
                            }
                            false
                        }
                        Err(other) => {
                            tracing::error!(id = client_order_id, error = %other, "cancel failed with no answer");
                            self.wal.append(&WalRecord::Note {
                                source: "engine".into(),
                                text: format!(
                                    "cancel of {client_order_id} sent with no answer ({other}); the order is still counted as working"
                                ),
                            })?;
                            if self.halt_cancels.contains_key(&client_order_id) {
                                halt_failure = Some(format!("{client_order_id}: {other}"));
                            }
                            false
                        }
                    };
                    self.working.cancelled(&client_order_id, taken);
                    if taken {
                        if let Some(state) = self.halt_cancels.get_mut(&client_order_id) {
                            *state = HaltCancelState::AwaitingPrivate {
                                deadline_ns: accepted_deadline,
                            };
                        }
                    }
                }
                self.release_symbols(symbols);
                if let Some(detail) = halt_failure {
                    return Err(EngineError::State(format!(
                        "account-level halt left at least one opening cancel unconfirmed ({detail}); restarting for venue reconciliation"
                    )));
                }
            }
            (
                PendingMutation::Amend {
                    symbol,
                    client_order_id,
                    spec,
                    existing,
                    amended_intent,
                    remaining_qty,
                    old_px,
                    tif,
                    queued_ns,
                },
                MutationCompletion::Amend {
                    started_ns,
                    completed_ns,
                    timing,
                    rate_wait_ns,
                    reply,
                    ..
                },
            ) => {
                if let Some(held) = rate_wait_ns {
                    self.ledger.record(Segment::QuotaHold, held);
                }
                let core_handled_ns = clock::now_ns();
                self.ledger
                    .record(Segment::DispatchQueue, started_ns.saturating_sub(queued_ns));
                self.ledger
                    .record(Segment::VenueTask, completed_ns.saturating_sub(started_ns));
                self.ledger.record(
                    Segment::CoreResume,
                    core_handled_ns.saturating_sub(completed_ns),
                );
                if let Some(mark) = timing {
                    self.ledger
                        .record(Segment::Ack, mark.ack_ns.saturating_sub(mark.sent_ns));
                }
                self.wal.append(&WalRecord::VenueTiming {
                    command_id,
                    operation: "amend".to_string(),
                    client_order_id: client_order_id.clone(),
                    queued_ns,
                    task_started_ns: started_ns,
                    socket_write_ns: timing.map(|mark| mark.sent_ns),
                    ack_ns: timing.map(|mark| mark.ack_ns),
                    rate_wait_ns,
                    task_completed_ns: completed_ns,
                    core_handled_ns,
                    core_handled_wall_ns: clock::wall_ns(),
                })?;
                tracing::debug!(
                    command_id,
                    venue_ns = completed_ns.saturating_sub(started_ns),
                    "amend command completed"
                );
                match reply {
                    Ok(()) => {
                        // The venue took it and did not say at what price.
                        // It states that by republishing the order on the
                        // private stream, so the ambiguity stays open for
                        // that answer rather than being closed by pulling
                        // the order — which is the whole point of amending
                        // in place instead of replacing.
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!(
                                "amend of {client_order_id} was accepted; waiting for the private stream to say what price it is working at"
                            ),
                        })?;
                        self.amends_awaiting_price.insert(
                            client_order_id.clone(),
                            AwaitingAmend {
                                symbol,
                                existing,
                                amended_intent,
                                remaining_qty,
                                tif,
                                deadline_ns: clock::now_ns().saturating_add(AMEND_CONFIRM_NS),
                            },
                        );
                    }
                    Err(VenueError::BadRequest(detail)) => {
                        tracing::error!(id = client_order_id, detail, "amend never sent");
                        self.resolve_amend(
                            &client_order_id,
                            &existing,
                            &amended_intent,
                            remaining_qty,
                            old_px,
                            tif,
                        )?;
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!("amend of {client_order_id} never sent: {detail}"),
                        })?;
                    }
                    Err(VenueError::Rejected { code, message }) => {
                        self.resolve_amend(
                            &client_order_id,
                            &existing,
                            &amended_intent,
                            remaining_qty,
                            old_px,
                            tif,
                        )?;
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!(
                                "amend of {client_order_id} rejected by venue ({code}: {message})"
                            ),
                        })?;
                    }
                    Err(other) => {
                        tracing::error!(id = client_order_id, error = %other, "amend failed with no answer");
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!(
                                "amend of {client_order_id} sent with no answer ({other}); its price and size are unconfirmed"
                            ),
                        })?;
                        self.pending.push_front(Action::Cancel {
                            symbol,
                            client_order_id: client_order_id.clone(),
                        });
                    }
                }
                self.working
                    .amended(&client_order_id, spec.px, false, clock::now_ns());
                self.release_symbols([symbol]);
            }
            (pending, completion) => {
                let pending_kind = match pending {
                    PendingMutation::Orders { .. } => "orders",
                    PendingMutation::Cancels { .. } => "cancels",
                    PendingMutation::Amend { .. } => "amend",
                };
                let completion_kind = match completion {
                    MutationCompletion::Orders { .. } => "orders",
                    MutationCompletion::Cancels { .. } => "cancels",
                    MutationCompletion::Amend { .. } => "amend",
                };
                return Err(EngineError::State(format!(
                    "venue task returned {completion_kind} for pending {pending_kind} command {command_id}"
                )));
            }
        }
        Ok(())
    }

    /// Narrow an amend's conservative old/new reservation to the one price
    /// the order is actually working at.
    ///
    /// Called with the old price when the amend never took, and with the
    /// venue's own stated price when it did. Both are the same act: the
    /// range was held open because the price was unknown, and this is where
    /// it becomes known.
    /// Wait for the outstanding durability barrier, if there is one.
    ///
    /// Almost always free: the barrier was started when the order was sent,
    /// and the venue's round trip is longer than the disk's. What it costs
    /// when it is not free is recorded, because that is the number that says
    /// whether running the barrier beside the send is buying anything.
    fn settle_barrier(&mut self) -> Result<(), EngineError> {
        let Some(pending) = self.pending_barrier.take() else {
            return Ok(());
        };
        let began_ns = clock::now_ns();
        pending.wait()?;
        self.ledger.record(
            Segment::BarrierWait,
            clock::now_ns().saturating_sub(began_ns),
        );
        Ok(())
    }

    fn resolve_amend(
        &mut self,
        client_order_id: &str,
        existing: &crate::inflight::OrderRec,
        amended_intent: &Intent,
        remaining_qty: f64,
        effective_px: f64,
        tif: TimeInForce,
    ) -> Result<(), EngineError> {
        let resolved = WalRecord::AmendResolved {
            client_order_id: client_order_id.to_string(),
            effective_px,
        };
        self.wal.append(&resolved)?;
        self.orders.apply(&resolved);
        if !existing.request.reduce_only {
            let mut settled = amended_intent.clone();
            settled.kind = OrderKind::Limit {
                px: effective_px,
                tif,
            };
            self.risk
                .register_order(client_order_id, &settled, remaining_qty);
        }
        Ok(())
    }

    /// Pull any order whose accepted amend the private stream never
    /// explained. The fallback is exactly what an unamendable venue gets:
    /// take the order down, because an order resting at a price the engine
    /// cannot name is one it cannot price its own book against.
    fn pull_unconfirmed_amends(&mut self) -> Result<(), EngineError> {
        if self.amends_awaiting_price.is_empty() {
            return Ok(());
        }
        let now_ns = clock::now_ns();
        let overdue: Vec<String> = self
            .amends_awaiting_price
            .iter()
            .filter(|(_, awaiting)| now_ns >= awaiting.deadline_ns)
            .map(|(id, _)| id.clone())
            .collect();
        for client_order_id in overdue {
            let Some(awaiting) = self.amends_awaiting_price.remove(&client_order_id) else {
                continue;
            };
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "amend of {client_order_id} was accepted but its price was never stated within {} ms; cancellation is queued",
                    AMEND_CONFIRM_NS / 1_000_000
                ),
            })?;
            self.amends_pulled_unconfirmed += 1;
            self.pending.push_front(Action::Cancel {
                symbol: awaiting.symbol,
                client_order_id,
            });
        }
        Ok(())
    }

    /// Pull a resting order.
    ///
    /// Not the order path in miniature: there is no barrier before the wire.
    /// A cancel adds no exposure, and an order the log still shows working is
    /// recovered at the next boot whether or not the cancel survived a crash
    /// — so the fsync would buy nothing. `origin_ns` is taken but not
    /// recorded: the latency ledger measures the order path, and mixing a
    /// barrier-free cancel into the submit-result segment would flatter it.
    ///
    /// True means the venue took the change. False means the resting order
    /// is untouched, and whoever asked has to ask again.
    /// Move a held position's venue-native stop, with no order involved.
    ///
    /// The record goes down before the call, as an opening order's does: a
    /// crash between the two must leave the log claiming the tighter stop, so
    /// boot's repair puts that one back rather than the distance the position
    /// opened at. A failed call is logged and dropped -- the old stop is still
    /// standing, the position is still covered, and the next wake asks again.
    async fn process_set_stop(
        &mut self,
        symbol: SymbolId,
        trigger_px: f64,
    ) -> Result<(), EngineError> {
        let symbol_name = self.market.table.name(symbol).to_string();
        let refuse = |reason: &str| WalRecord::Note {
            source: "engine".into(),
            text: format!("stop on {symbol_name} not moved to {trigger_px}: {reason}"),
        };
        if !trigger_px.is_finite() || trigger_px <= 0.0 {
            self.wal
                .append(&refuse("trigger is not a positive finite price"))?;
            return Ok(());
        }
        let mut held = self.account.positions.iter().filter(|p| p.symbol == symbol);
        let Some(position) = held.next() else {
            self.wal
                .append(&refuse("the latest account view has no held position"))?;
            return Ok(());
        };
        if held.next().is_some() || !position.qty.is_finite() || position.qty <= 0.0 {
            self.wal.append(&refuse(
                "the latest position state is ambiguous or unreadable",
            ))?;
            return Ok(());
        }
        let remembered = self
            .intended_stops
            .get(&symbol.0)
            .filter(|stop| stop.side == position.side)
            .map(|stop| stop.trigger_px);
        let venue_stop =
            (position.stop_attached && position.stop_px.is_finite() && position.stop_px > 0.0)
                .then_some(position.stop_px);
        let baseline = match (position.side, remembered, venue_stop) {
            (Side::Buy, Some(a), Some(b)) => Some(a.max(b)),
            (Side::Sell, Some(a), Some(b)) => Some(a.min(b)),
            (_, a, b) => a.or(b),
        };
        let loosens = match (position.side, baseline) {
            (Side::Buy, Some(old)) => trigger_px < old,
            (Side::Sell, Some(old)) => trigger_px > old,
            (_, None) => false,
        };
        if loosens {
            self.wal
                .append(&refuse("the requested stop would loosen protection"))?;
            return Ok(());
        }
        self.wal.append(&WalRecord::StopSet {
            symbol,
            trigger_px,
            wall_ts_ms: clock::wall_ms(),
        })?;
        self.intended_stops.insert(
            symbol.0,
            reconcile::IntendedPositionStop {
                side: position.side,
                trigger_px,
            },
        );
        match self.venue.set_stop(symbol, trigger_px).await {
            Ok(()) => {
                tracing::info!(
                    symbol = self.market.table.name(symbol),
                    trigger_px,
                    "moved this position's stop in"
                );
                Ok(())
            }
            Err(e) => {
                tracing::error!(
                    symbol = self.market.table.name(symbol),
                    trigger_px,
                    error = %e,
                    "could not move this position's stop; the one it opened behind still stands"
                );
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "stop on {} not moved to {trigger_px}: {e}",
                        self.market.table.name(symbol)
                    ),
                })?;
                Ok(())
            }
        }
    }

    /// Record a bounded cancel group, then use the adapter's fastest safe
    /// route. Every answer stays joined to its own client id and the working
    /// supervisor only marks a pull accepted on `Ok`.
    async fn process_cancels(
        &mut self,
        requests: Vec<(SymbolId, String)>,
    ) -> Result<bool, EngineError> {
        if requests.is_empty() {
            return Ok(false);
        }
        if requests.len() > MAX_CANCELS_PER_BATCH {
            return Err(EngineError::State(format!(
                "cancel batch has {} orders; hard maximum is {MAX_CANCELS_PER_BATCH}",
                requests.len()
            )));
        }
        let wire_ns = clock::now_ns();
        for (symbol, client_order_id) in &requests {
            self.wal.append(&WalRecord::CancelSent {
                symbol: *symbol,
                client_order_id: client_order_id.clone(),
                wire_ns,
            })?;
        }
        let queued_ns = clock::now_ns();
        let command_id = self.venue.dispatch_cancels(requests.clone())?;
        self.mark_symbols_busy(requests.iter().map(|(symbol, _)| *symbol));
        self.pending_mutations.insert(
            command_id,
            PendingMutation::Cancels {
                requests,
                queued_ns,
            },
        );
        Ok(true)
    }

    async fn process_amend(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        mut spec: AmendSpec,
        _origin_ns: u64,
    ) -> Result<bool, EngineError> {
        if !self.may_open || !self.private_stream_ready {
            let halt = if !self.may_open {
                "reconciliation opening latch is set"
            } else {
                "private account stream is not ready"
            };
            match self.orders.orders.get(client_order_id) {
                Some(order) if !order.request.reduce_only => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!("{client_order_id} not amended: {halt}"),
                    })?;
                    self.pending.push_front(Action::Cancel {
                        symbol,
                        client_order_id: client_order_id.to_string(),
                    });
                    return Ok(false);
                }
                None => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!(
                            "{client_order_id} not amended while {halt}: order ownership and direction are unknown"
                        ),
                    })?;
                    return Ok(false);
                }
                Some(_) => {}
            }
        }
        if spec.qty.is_some() {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: quantity changes are unsupported until risk and ledger reservations can be resized atomically"),
            })?;
            return Ok(false);
        }
        if spec.px.is_none_or(|px| !px.is_finite() || px <= 0.0) {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: price is not positive and finite"),
            })?;
            return Ok(false);
        }
        if !self.venue.caps().amend_in_place {
            // No quiet fallback to cancel-and-replace. A replaced order is a
            // new order at the back of the queue at a fresh price — a
            // different trade from the one asked for, and the strategy would
            // never learn it had been substituted.
            tracing::warn!(
                id = client_order_id,
                "this venue cannot amend; the order is left alone"
            );
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: this venue cannot change a resting order in place, and cancel-and-replace is a different trade"
                ),
            })?;
            return Ok(false);
        }

        let Some(existing) = self.orders.orders.get(client_order_id).cloned() else {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: order is absent from the durable ledger"
                ),
            })?;
            return Ok(false);
        };
        if !existing.in_flight() || existing.request.symbol != symbol {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: order is terminal or names a different symbol"
                ),
            })?;
            return Ok(false);
        }
        if existing.reservation_low_px.to_bits() != existing.reservation_high_px.to_bits() {
            // An order whose working price is unknown cannot be moved: the
            // next reservation would have to cover the range of a range. If
            // an answer is still owed the wait is measured in milliseconds
            // and the asker can come back; the confirmation deadline is what
            // pulls the order when no answer comes. Ambiguity with nothing
            // owed — a range left open across a restart — has no answer
            // coming, so that one is resolved the only way left.
            let awaited = self.amends_awaiting_price.contains_key(client_order_id);
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: if awaited {
                    format!(
                        "{client_order_id} not amended yet: the venue has not said what price its last amend left it at"
                    )
                } else {
                    format!(
                        "{client_order_id} not amended: its prior amend outcome is still ambiguous; cancellation queued"
                    )
                },
            })?;
            if !awaited {
                self.pending.push_front(Action::Cancel {
                    symbol,
                    client_order_id: client_order_id.to_string(),
                });
            }
            return Ok(false);
        }
        let OrderKind::Limit { px: old_px, tif } = existing.request.kind else {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: only a resting limit order has a price to change"),
            })?;
            return Ok(false);
        };
        let Some(rule) = self.rules.get(symbol.0 as usize).copied().flatten() else {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: instrument rules are unavailable"),
            })?;
            return Ok(false);
        };
        let requested_px = quantize::quantize_px(
            spec.px.expect("positive price checked above"),
            existing.request.side,
            &rule,
        );
        spec.px = Some(requested_px);
        let remaining_qty = existing.request.qty - existing.filled_qty;
        if !remaining_qty.is_finite() || remaining_qty <= 1e-9 {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: no readable remaining quantity is working"
                ),
            })?;
            return Ok(false);
        }

        let amended_intent = Intent {
            strategy: existing.request.strategy,
            symbol,
            side: existing.request.side,
            qty: remaining_qty,
            kind: OrderKind::Limit {
                px: requested_px,
                tif,
            },
            stop: existing.request.stop,
            reduce_only: existing.request.reduce_only,
            tag: format!("amend:{client_order_id}"),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        if !existing.request.reduce_only {
            let verdict =
                self.risk
                    .assess_price_amend(client_order_id, &amended_intent, &self.account);
            let verdict = durable_risk_verdict(verdict, remaining_qty, true);
            self.wal.append(&WalRecord::Verdict {
                client_order_id: Some(client_order_id.to_string()),
                verdict: verdict.clone(),
            })?;
            match verdict {
                RiskVerdict::Allow { qty }
                    if qty.is_finite() && (qty - remaining_qty).abs() <= 1e-9 => {}
                RiskVerdict::Allow { qty } => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!(
                            "{client_order_id} not amended: risk approved {qty}, but an in-place price amend cannot resize remaining quantity {remaining_qty}"
                        ),
                    })?;
                    return Ok(false);
                }
                RiskVerdict::Deny { reason } => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!(
                            "{client_order_id} not amended at {requested_px}: {reason:?}"
                        ),
                    })?;
                    return Ok(false);
                }
            }
        }

        // Repricing can multiply notional and stop distance just as surely as
        // a new order can. Journal and reserve the more expensive of old/new
        // before the wire. A crash or transport ambiguity therefore replays
        // the safe side; a definitive venue answer below narrows it back to
        // the price that is actually working.
        let sent = WalRecord::AmendSent {
            symbol,
            client_order_id: client_order_id.to_string(),
            spec,
            wire_ns: clock::now_ns(),
        };
        self.wal.append(&sent)?;
        self.orders.apply(&sent);
        if !existing.request.reduce_only {
            self.risk.register_order_price_range(
                client_order_id,
                &amended_intent,
                remaining_qty,
                old_px.min(requested_px),
                old_px.max(requested_px),
            );
            self.wal.barrier()?;
        }

        let queued_ns = clock::now_ns();
        let command_id = self
            .venue
            .dispatch_amend(symbol, client_order_id.to_string(), spec)?;
        self.mark_symbols_busy([symbol]);
        self.pending_mutations.insert(
            command_id,
            PendingMutation::Amend {
                symbol,
                client_order_id: client_order_id.to_string(),
                spec,
                existing,
                amended_intent: Box::new(amended_intent),
                remaining_qty,
                old_px,
                tif,
                queued_ns,
            },
        );
        Ok(false)
    }

    async fn checkpoint_history_if_due(&mut self) -> Result<(), EngineError> {
        if clock::wall_ms() < self.next_history_checkpoint_ms {
            return Ok(());
        }
        self.renew_execution_history().await
    }

    pub(crate) async fn renew_execution_history(&mut self) -> Result<(), EngineError> {
        self.recover_history("while renewing the durable execution-history checkpoint")
            .await
    }

    /// After a private-stream gap: ask the venue what traded while the stream
    /// was away. The same pass also renews the quiet-run checkpoint.
    async fn recover_gap_fills(&mut self) -> Result<(), EngineError> {
        self.recover_history("after a private-stream gap").await
    }

    /// Fold one complete execution-history interval through the ordinary fill
    /// books, then write its boundary after every returned row. Failure stops
    /// the run: advancing without the read would make the next boot trust a
    /// hole, and continuing until the venue forgets it would make repair
    /// impossible.
    async fn recover_history(&mut self, context: &str) -> Result<(), EngineError> {
        let now_ms = clock::wall_ms();
        let since = (self.recovered_until_ms - RECOVERY_PAD_MS).max(now_ms - RECOVERY_REACH_MS);
        if since >= now_ms {
            self.next_history_checkpoint_ms = now_ms.saturating_add(HISTORY_CHECKPOINT_INTERVAL_MS);
            return Ok(());
        }
        let mut execs = match self.venue.executions(since, now_ms).await {
            Ok(execs) => execs,
            Err(error) => {
                self.may_open = false;
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: now_ms,
                    findings: vec![format!(
                        "execution history is unavailable {context}: {error}"
                    )],
                    may_open: false,
                })?;
                self.wal.barrier()?;
                return Err(EngineError::Venue(error));
            }
        };
        execs.sort_by_key(|exec| exec.venue_ts_ms);
        let mut delivered_counts: std::collections::HashMap<(String, i64, u64), usize> =
            std::collections::HashMap::new();
        for (id, ts, qty) in &self.recent_fills {
            *delivered_counts
                .entry((id.clone(), *ts, qty.to_bits()))
                .or_default() += 1;
        }
        let mut recovered = 0usize;
        let mut foreign = Vec::new();
        for exec in execs {
            if self.recovered_exec_ids.contains(&exec.exec_id, now_ms) {
                continue;
            }
            let key = (
                exec.client_order_id.clone(),
                exec.venue_ts_ms,
                exec.qty.to_bits(),
            );
            let same_delivered = delivered_counts.get_mut(&key).is_some_and(|count| {
                if *count == 0 {
                    false
                } else {
                    *count -= 1;
                    true
                }
            });
            if same_delivered {
                continue;
            }
            self.recovered_exec_ids
                .can_insert(&exec.exec_id, now_ms)
                .map_err(|e| EngineError::State(e.to_string()))?;
            let Some(symbol) = self.market.table.get(&exec.symbol) else {
                let finding = Self::foreign_unmapped_execution_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    &exec.symbol,
                    exec.qty,
                );
                self.wal.append(&WalRecord::Note {
                    source: "fill-recovery".into(),
                    text: finding.clone(),
                })?;
                self.recovered_exec_ids.insert(exec.exec_id, now_ms);
                foreign.push(finding);
                recovered += 1;
                continue;
            };
            if let Err(reason) = self.orders.validate_fill(
                &exec.client_order_id,
                symbol,
                exec.side,
                exec.qty,
                exec.px,
            ) {
                foreign.push(Self::untrusted_fill_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    symbol,
                    exec.side,
                    exec.qty,
                    exec.px,
                    &reason,
                ));
                self.recovered_exec_ids.insert(exec.exec_id, now_ms);
                recovered += 1;
                continue;
            }
            let record = WalRecord::RecoveredFill {
                exec_id: exec.exec_id.clone(),
                client_order_id: exec.client_order_id.clone(),
                symbol,
                side: exec.side,
                qty: exec.qty,
                px: exec.px,
                fee: exec.fee,
                is_maker: exec.is_maker,
                venue_ts_ms: exec.venue_ts_ms,
                recovered_wall_ts_ms: now_ms,
            };
            self.wal.append(&record)?;
            self.recovered_exec_ids.insert(exec.exec_id.clone(), now_ms);
            let owner = self.orders.owner_of(&exec.client_order_id);
            let owned_request = self
                .orders
                .orders
                .get(&exec.client_order_id)
                .map(|order| order.request.clone());
            self.orders.apply(&record);
            if let Some(sid) = owner {
                reconcile::note_owned_fill(
                    &mut self.logged_exposure,
                    &mut self.intended_stops,
                    owned_request.as_ref(),
                    symbol,
                    exec.side,
                    exec.qty,
                );
                self.attribution.note(sid, symbol, exec.side, exec.qty);
                // What it cost is the same question whichever way it arrived,
                // and the anchor is the book its own order left at.
                let late_ns = now_ms
                    .saturating_sub(exec.venue_ts_ms)
                    .max(0)
                    .saturating_mul(1_000_000) as u64;
                // Dated to when it traded, not to when it was found, or a
                // trade from minutes ago is marked against this minute's book
                // and the number is read as a one-second fact.
                self.fills.on_recovered_fill(
                    &execution::Fill {
                        client_order_id: exec.client_order_id.clone(),
                        strategy: sid,
                        symbol,
                        side: exec.side,
                        qty: exec.qty,
                        px: exec.px,
                        fee: exec.fee,
                        is_maker: exec.is_maker,
                        arrival_mid: self.arrival_mid_of(&exec.client_order_id),
                        venue_ts_ms: exec.venue_ts_ms,
                    },
                    clock::now_ns().checked_sub(late_ns),
                );
            } else {
                foreign.push(Self::foreign_fill_line(&exec.client_order_id, symbol));
            }
            // The kernel reserved this order's size when it approved it, and
            // only a fill releases the reservation. Skipping it here leaves the
            // position counted twice — once as a reservation that never ends,
            // once in the account view — and every later entry judged against
            // the sum.
            self.risk.on_update(&OrderUpdate::Fill {
                exec_id: exec.exec_id.clone(),
                client_order_id: exec.client_order_id.clone(),
                symbol,
                side: exec.side,
                qty: exec.qty,
                px: exec.px,
                fee: exec.fee,
                is_maker: exec.is_maker,
                venue_ts_ms: exec.venue_ts_ms,
                // The engine's own clock, not the venue's: `recv_ns` is what
                // the kernel compares against the account view's stamp, and the
                // two must come from one clock.
                recv_ns: clock::now_ns(),
            });
            recovered += 1;
        }
        if recovered > 0 {
            tracing::warn!(count = recovered, "recovered fills from execution history");
        }
        if !foreign.is_empty() {
            self.may_open = false;
            self.wal.append(&WalRecord::Reconciled {
                wall_ts_ms: now_ms,
                findings: foreign,
                may_open: false,
            })?;
        }
        self.wal.append(&WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: now_ms,
        })?;
        self.wal.barrier()?;
        self.recovered_until_ms = now_ms;
        self.next_history_checkpoint_ms = now_ms.saturating_add(HISTORY_CHECKPOINT_INTERVAL_MS);
        Ok(())
    }

    fn foreign_fill_line(client_order_id: &str, symbol: SymbolId) -> String {
        format!(
            "symbol {}: a fill names an order this engine did not send ({})",
            symbol.0,
            if client_order_id.is_empty() {
                "blank client id"
            } else {
                client_order_id
            }
        )
    }

    fn foreign_unmapped_execution_line(
        exec_id: &str,
        client_order_id: &str,
        symbol: &str,
        qty: f64,
    ) -> String {
        format!(
            "venue symbol {symbol}: execution {} for quantity {qty} cannot be mapped to the configured symbol table (order {})",
            if exec_id.is_empty() { "<blank>" } else { exec_id },
            if client_order_id.is_empty() {
                "<blank>"
            } else {
                client_order_id
            }
        )
    }

    fn untrusted_fill_line(
        exec_id: &str,
        client_order_id: &str,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        reason: &str,
    ) -> String {
        format!(
            "symbol {}: an untrusted fill for order {} (execution {}, side {side:?}, quantity {qty}, price {px}) was not applied: {reason}",
            symbol.0,
            if client_order_id.is_empty() {
                "<blank>"
            } else {
                client_order_id
            },
            if exec_id.is_empty() { "<blank>" } else { exec_id },
        )
    }

    /// Every order update, wherever it came from, goes through here.
    async fn take_update(&mut self, update: OrderUpdate) -> Result<(), EngineError> {
        // Before anything is done with news about an order: the record of the
        // order that earned it is on the disk. This is the wait the send no
        // longer pays.
        self.settle_barrier()?;
        if let OrderUpdate::FastFill {
            exec_id,
            client_order_id,
            venue_order_id,
            symbol,
            side,
            qty,
            px,
            is_maker,
            venue_ts_ms,
            recv_ns,
        } = &update
        {
            self.wal.append(&WalRecord::FastExecution {
                exec_id: exec_id.clone(),
                client_order_id: client_order_id.clone(),
                venue_order_id: venue_order_id.clone(),
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                is_maker: *is_maker,
                venue_ts_ms: *venue_ts_ms,
                recv_ns: *recv_ns,
            })?;
            self.route_order_update(update);
            return Ok(());
        }
        let stream_reset = matches!(&update, OrderUpdate::StreamReset { .. });
        if stream_reset {
            self.stream_resets += 1;
            // No other event is processed while this handler awaits the two
            // recovery reads, so clearing first is an immediate admission
            // barrier without unnecessarily cancelling healthy orders when
            // the resync succeeds.
            self.private_stream_ready = false;
            self.account.observed_ns = 0;
        }
        let fill_owner = match &update {
            OrderUpdate::Fill {
                client_order_id, ..
            } => self.orders.owner_of(client_order_id),
            _ => None,
        };
        let fill_request = match &update {
            OrderUpdate::Fill {
                client_order_id, ..
            } => self
                .orders
                .orders
                .get(client_order_id)
                .map(|order| order.request.clone()),
            _ => None,
        };
        let delivered_exec_id = match &update {
            OrderUpdate::Fill { exec_id, .. } if !exec_id.is_empty() => Some(exec_id.clone()),
            _ => None,
        };
        let dedup_seen_ms = clock::wall_ms();
        if let Some(exec_id) = delivered_exec_id.as_deref() {
            if !self
                .recovered_exec_ids
                .can_insert(exec_id, dedup_seen_ms)
                .map_err(|e| EngineError::State(e.to_string()))?
            {
                tracing::warn!(exec_id, "duplicate fill ignored");
                return Ok(());
            }
        }
        if let OrderUpdate::Fill {
            exec_id,
            client_order_id,
            symbol,
            side,
            qty,
            px,
            ..
        } = &update
        {
            if let Err(reason) =
                self.orders
                    .validate_fill(client_order_id, *symbol, *side, *qty, *px)
            {
                let finding = Self::untrusted_fill_line(
                    exec_id,
                    client_order_id,
                    *symbol,
                    *side,
                    *qty,
                    *px,
                    &reason,
                );
                if let Some(exec_id) = delivered_exec_id {
                    self.recovered_exec_ids.insert(exec_id, dedup_seen_ms);
                }
                self.may_open = false;
                tracing::error!(%finding, "untrusted fill left order and risk state unchanged");
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: dedup_seen_ms,
                    findings: vec![finding],
                    may_open: false,
                })?;
                self.wal.barrier()?;
                return Ok(());
            }
        }
        self.wal.append(&WalRecord::OrderUpdate {
            update: update.clone(),
        })?;
        if let Some(exec_id) = delivered_exec_id {
            self.recovered_exec_ids.insert(exec_id, dedup_seen_ms);
        }
        self.risk.on_update(&update);
        self.orders.apply_update(&update);
        // The venue naming the price a resting order is working at is the
        // answer an accepted amend was waiting for. It ends the ambiguity
        // the way a definitive rejection does, except that the order stays
        // where it is — with whatever queue position the venue left it.
        let stated_price = match &update {
            OrderUpdate::Amended {
                client_order_id,
                px,
                ..
            } => Some((client_order_id.clone(), *px)),
            _ => None,
        };
        if let Some((client_order_id, px)) = stated_price {
            if let Some(awaiting) = self.amends_awaiting_price.remove(&client_order_id) {
                self.amends_confirmed += 1;
                self.resolve_amend(
                    &client_order_id,
                    &awaiting.existing,
                    &awaiting.amended_intent,
                    awaiting.remaining_qty,
                    px,
                    awaiting.tif,
                )?;
                // The supervisor working this entry prices its next move
                // against where the order actually is, spends one of its
                // amend budget, and starts its cross grace from the cross
                // that really happened. Acceptance alone could tell it none
                // of that, because acceptance does not name a price.
                self.working
                    .amended(&client_order_id, Some(px), true, clock::now_ns());
            }
        }
        if let Some(client_order_id) = inflight::client_order_id(&update) {
            let still_live = self
                .orders
                .orders
                .get(client_order_id)
                .is_some_and(|order| order.in_flight());
            if !still_live {
                self.halt_cancels.remove(client_order_id);
                // An order that has ended has no price left to state. Its
                // reservation went with it: the ending is what released it.
                self.amends_awaiting_price.remove(client_order_id);
            }
        }
        // Only fills joined to orders this log sent enter trusted exposure.
        // Foreign fills remain durable records and latch entries off below.
        if let (
            Some(_),
            OrderUpdate::Fill {
                symbol, side, qty, ..
            },
        ) = (fill_owner, &update)
        {
            reconcile::note_owned_fill(
                &mut self.logged_exposure,
                &mut self.intended_stops,
                fill_request.as_ref(),
                *symbol,
                *side,
                *qty,
            );
        }
        // Remembered for gap recovery's dedup: a fill the stream DID deliver
        // near a gap's edge must not come back from the venue's history as a
        // recovered one.
        if let OrderUpdate::Fill {
            client_order_id,
            venue_ts_ms,
            qty,
            ..
        } = &update
        {
            self.recent_fills
                .push_back((client_order_id.clone(), *venue_ts_ms, *qty));
            while self.recent_fills.len() > RECENT_FILLS_KEPT {
                self.recent_fills.pop_front();
            }
        }
        if let OrderUpdate::Fill {
            client_order_id,
            symbol,
            ..
        } = &update
        {
            if fill_owner.is_none() {
                self.may_open = false;
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: dedup_seen_ms,
                    findings: vec![Self::foreign_fill_line(client_order_id, *symbol)],
                    may_open: false,
                })?;
                self.wal.barrier()?;
            }
        }
        // Whose fill it was, before any strategy is woken, so the one that
        // placed the order sees its own position already changed. The ledger
        // is asked rather than the registry: the registry knows only this
        // boot's ids and the ones in flight when it started, and a fill can
        // still arrive for an order older than either.
        if let Some(id) = inflight::client_order_id(&update) {
            match self.orders.owner_of(id) {
                Some(sid) => {
                    self.attribution.on_update(sid, &update);
                    self.price_fill(sid, &update);
                    // Terminal news that ends size without a fill releases
                    // that much cover: the whole send on a reject, the
                    // unfilled remainder on a cancel. A fill releases nothing
                    // here — it stays covered until the account reading
                    // shows it, which is the whole point of the cover.
                    let released = self.orders.orders.get(id).and_then(|order| match &update {
                        OrderUpdate::Reject { .. } => {
                            Some((order.request.symbol, order.request.qty))
                        }
                        OrderUpdate::Cancelled { .. } => Some((
                            order.request.symbol,
                            (order.request.qty - order.filled_qty).max(0.0),
                        )),
                        _ => None,
                    });
                    if let Some((symbol, qty)) = released {
                        self.covers.release_newest(sid, symbol, qty);
                    }
                }
                // Charged to nobody on purpose. `reconcile` is what notices
                // the account holds more than the log accounts for, and it
                // already stops the engine opening on top of it.
                None if matches!(update, OrderUpdate::Fill { .. }) => tracing::warn!(
                    id,
                    "a fill for an order this log never recorded sending; it is charged to \
                     no strategy"
                ),
                None => {}
            }
        }

        // A private-stream gap may have swallowed fills. Refresh the account
        // reading now rather than trusting exposure across the gap.
        if stream_reset {
            self.fills.stream_gap();
            let mut account_refreshed = false;
            match self.venue.account_view().await {
                Ok(view) => {
                    self.adopt_view(view);
                    self.enforce_position_stop_intent().await?;
                    account_refreshed = true;
                }
                Err(e) => {
                    tracing::warn!(error = %e, "no fresh account reading after a stream gap");
                }
            }
            // The fills themselves CAN be repaired from the venue: its
            // execution history is asked for the gap, so the log keeps
            // accounting for what actually traded.
            self.recover_gap_fills().await?;
            if account_refreshed {
                self.private_stream_ready = true;
            }
            self.queue_halted_entry_cancels()?;
        }

        self.route_order_update(update);
        Ok(())
    }

    fn route_order_update(&mut self, update: OrderUpdate) {
        let now = clock::now_ns();
        let event = EngineEvent::Order(update.clone());
        match inflight::client_order_id(&update) {
            Some(id) => match self
                .registry
                .owner_of(id)
                .or_else(|| self.orders.owner_of(id))
            {
                Some(sid) => {
                    let Engine {
                        strategies,
                        market,
                        timers,
                        pending,
                        orders,
                        registry,
                        attribution,
                        covers,
                        account,
                        rules,
                        ..
                    } = self;
                    feed_strategy(
                        strategies,
                        market,
                        account,
                        rules,
                        timers,
                        pending,
                        orders,
                        registry,
                        attribution,
                        covers,
                        sid,
                        &event,
                        now,
                    );
                }
                None => {
                    let ours = self.registry.is_ours(id);
                    tracing::warn!(id, ours, "order update for an order no strategy owns");
                }
            },
            None => {
                // A stop belongs to a symbol, not to an order: tell whoever
                // watches that symbol.
                if let OrderUpdate::StopAttached { symbol, .. } = update {
                    let Engine {
                        strategies,
                        market,
                        timers,
                        pending,
                        routing,
                        orders,
                        registry,
                        attribution,
                        covers,
                        account,
                        rules,
                        ..
                    } = self;
                    for sid in routing.all_listeners(symbol) {
                        feed_strategy(
                            strategies,
                            market,
                            account,
                            rules,
                            timers,
                            pending,
                            orders,
                            registry,
                            attribution,
                            covers,
                            sid,
                            &event,
                            now,
                        );
                    }
                }
            }
        }
    }

    fn refuse(
        &mut self,
        client_order_id: &str,
        intent: &Intent,
        why: &str,
    ) -> Result<(), EngineError> {
        let key = (intent.strategy, intent.symbol, intent.tag.clone());
        let now_ns = clock::now_ns();
        let repeated = self.refusals.get(&key).is_some_and(|last| {
            last.why == why && now_ns.saturating_sub(last.at_ns) < REFUSAL_REPEAT_NS
        });
        if repeated {
            if let Some(last) = self.refusals.get_mut(&key) {
                last.suppressed += 1;
            }
            self.tell_refused(intent, why);
            return Ok(());
        }
        let suppressed = self
            .refusals
            .insert(
                key,
                Refusal {
                    why: why.to_string(),
                    at_ns: now_ns,
                    suppressed: 0,
                },
            )
            .map(|last| last.suppressed)
            .unwrap_or(0);
        let also = if suppressed > 0 {
            format!(" (and {suppressed} more like it)")
        } else {
            String::new()
        };
        tracing::warn!(id = client_order_id, tag = %intent.tag, why, suppressed, "order not sent");
        self.wal.append(&WalRecord::Note {
            source: "engine".into(),
            text: format!("{client_order_id} not sent ({}): {why}{also}", intent.tag),
        })?;
        self.tell_refused(intent, why);
        Ok(())
    }

    /// Settle the in-flight accounting for an intent that died inside the
    /// engine, then tell the strategy. A refused exit means the covers
    /// describe exposure the account reading says is not there, and left
    /// standing they would re-plan the same doomed exit on every quote.
    fn tell_refused(&mut self, intent: &Intent, reason: &str) {
        // Bookkeeping first, so the strategy woken below already reads the
        // truthful in-flight number. A refused exit drops every cover on the
        // symbol; a refused entry has none to drop, because covers are booked
        // at the send and a refusal never reaches it.
        self.covers
            .intent_refused(intent.strategy, intent.symbol, intent.reduce_only);
        let event = EngineEvent::IntentRefused {
            symbol: intent.symbol,
            reduce_only: intent.reduce_only,
            reason: reason.to_string(),
        };
        let now = clock::now_ns();
        let Engine {
            strategies,
            market,
            timers,
            pending,
            orders,
            registry,
            attribution,
            covers,
            account,
            rules,
            ..
        } = self;
        feed_strategy(
            strategies,
            market,
            account,
            rules,
            timers,
            pending,
            orders,
            registry,
            attribution,
            covers,
            intent.strategy,
            &event,
            now,
        );
    }

    /// Turn an entry the strategy asked to have worked into the resting limit
    /// it should start as, and say whether it will be worked at all.
    ///
    /// `None` leaves the intent exactly as the strategy wrote it: no policy,
    /// an exit, a symbol with no instrument rule, or a spread too thin for
    /// resting to pay for itself.
    fn plan_resting_entry(&self, intent: &mut Intent) -> Option<WorkPolicy> {
        let rule = self
            .rules
            .get(intent.symbol.0 as usize)
            .copied()
            .flatten()?;
        let touch = self
            .market
            .quotes
            .get(intent.symbol.0 as usize)
            .map(working::touch_of)
            .unwrap_or_default();
        match working::plan::opening(intent, touch, &rule) {
            working::plan::Opening::AsWritten => None,
            working::plan::Opening::WorkAsPriced { policy } => Some(policy),
            working::plan::Opening::Rest { px, policy } => {
                // Good-till-cancelled, not post-only. The overnight lab that
                // first measured resting ran post-only into the demo realm's
                // pretend internal liquidity, which flattered it; the numbers
                // this recipe is built on are GTC numbers.
                intent.kind = OrderKind::Limit {
                    px,
                    tif: TimeInForce::Gtc,
                };
                Some(policy)
            }
        }
    }

    /// The mid this order was decided against, or zero when the book was not
    /// two-sided. Only the early cross reads it, and it stays off at zero.
    fn decision_mid(&self, symbol: SymbolId) -> f64 {
        let quote = self.market.quote(symbol);
        if quote.bid_px > 0.0 && quote.ask_px > quote.bid_px {
            (quote.bid_px + quote.ask_px) / 2.0
        } else {
            0.0
        }
    }

    fn reference_px(&self, symbol: SymbolId, kind: &OrderKind) -> Option<f64> {
        if let OrderKind::Limit { px, .. } = kind {
            return Some(*px);
        }
        let quote = self.market.quote(symbol);
        if quote.bid_px > 0.0 && quote.ask_px > 0.0 {
            return Some((quote.bid_px + quote.ask_px) / 2.0);
        }
        let ticker = self.market.ticker(symbol);
        [ticker.last_px, ticker.mark_px]
            .into_iter()
            .find(|px| *px > 0.0)
    }

    /// `M0` for an order of ours, off the order ledger. Zero for one the
    /// ledger no longer holds, which makes every arrival number for its fills
    /// missing rather than wrong.
    fn arrival_mid_of(&self, client_order_id: &str) -> f64 {
        self.orders
            .orders
            .get(client_order_id)
            .map(|order| order.arrival_mid)
            .unwrap_or(0.0)
    }

    /// Price one fill against the book that was on the screen when its order
    /// left, and start its markout clock.
    ///
    /// The anchor comes off the order ledger rather than out of memory,
    /// because the ledger is rebuilt from the log at boot: a fill for an order
    /// sent before a restart is still priced against the right midpoint.
    fn price_fill(&mut self, strategy: StrategyId, update: &OrderUpdate) {
        let OrderUpdate::Fill {
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            is_maker,
            venue_ts_ms,
            ..
        } = update
        else {
            return;
        };
        let arrival_mid = self.arrival_mid_of(client_order_id);
        self.fills.on_fill(
            &execution::Fill {
                client_order_id: client_order_id.clone(),
                strategy,
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                fee: *fee,
                is_maker: *is_maker,
                arrival_mid,
                venue_ts_ms: *venue_ts_ms,
            },
            clock::now_ns(),
        );
    }

    fn mint_id(&mut self) -> String {
        let orders = &self.orders;
        mint_unused(
            self.registry.prefix(),
            &mut self.next_order_n,
            |candidate| orders.contains(candidate),
        )
    }

    pub fn strategy_names(&self) -> &[String] {
        &self.names
    }

    /// What the fills have cost so far this run.
    pub fn fills(&self) -> &Fills {
        &self.fills
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

/// Entry blockers with the configured sleeve name that owns each one.
///
/// A symbol is not a unique key on a multi-sleeve account. Deduplication is
/// deliberately per (strategy, symbol), preserving the first reason each
/// strategy reports because target-book followers put kernel refusals before
/// weaker planner skips.
pub(crate) fn named_entry_blockers(
    strategies: &[Box<dyn Strategy>],
    names: &[String],
) -> Vec<(String, String, String)> {
    let mut blockers: Vec<(String, String, String)> = Vec::new();
    for (index, strategy) in strategies.iter().enumerate() {
        let Some(strategy_name) = names.get(index) else {
            tracing::error!(
                index,
                "strategy has no configured name; omitting its entry blockers"
            );
            continue;
        };
        for (symbol, reason) in strategy.entry_blockers() {
            if !blockers.iter().any(|(seen_strategy, seen_symbol, _)| {
                seen_strategy == strategy_name && seen_symbol == &symbol
            }) {
                blockers.push((strategy_name.clone(), symbol, reason));
            }
        }
    }
    blockers.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    blockers
}

#[allow(clippy::too_many_arguments)]
fn feed_strategy(
    strategies: &mut [Box<dyn Strategy>],
    market: &MarketState,
    account: &AccountView,
    rules: &[Option<InstrumentRule>],
    timers: &mut Timers,
    pending: &mut VecDeque<Action>,
    orders: &LedgerOfOrders,
    registry: &OrderRegistry,
    attribution: &Attribution,
    covers: &CoverBook,
    sid: StrategyId,
    event: &EngineEvent,
    now_ns: u64,
) {
    let Some(strategy) = strategies.get_mut(sid.0 as usize) else {
        return;
    };
    let mut ctx = Ctx {
        market,
        account,
        rules,
        now_ns,
        strategy: sid,
        out: pending,
        timers,
        orders,
        registry,
        attribution,
        covers,
    };
    strategy.on_event(event, &mut ctx);
}

/// Drop the remembered leverage of every symbol the reading shows flat.
///
/// A symbol with no position may be reopened at any leverage by anyone holding
/// a key, and the owner trades the funded account by hand. Remembering what we
/// last set it to would then be remembering something that is no longer true,
/// and the next entry would skip the call that would have corrected it.
///
/// A symbol still open keeps its entry: its leverage cannot be changed at the
/// venue while a position is on it.
pub(crate) fn forget_leverage_where_flat(
    leverage_at: &mut std::collections::HashMap<SymbolId, f64>,
    positions: &[engine_types::risk::PositionView],
) {
    leverage_at.retain(|symbol, _| positions.iter().any(|p| p.symbol == *symbol));
}

/// When this message reached us. The whole chain is measured from here.
/// Mint the next client order id, skipping any the log already knows: the
/// boot prefix comes from a wall clock, and a clock stepped back must not
/// let a new order overwrite a recovered one's ledger entry.
pub(crate) fn mint_unused(prefix: &str, next_n: &mut u64, taken: impl Fn(&str) -> bool) -> String {
    loop {
        *next_n += 1;
        let id = format!("{prefix}{next_n}");
        assert!(id.len() <= 36, "client order id too long: {id}");
        if !taken(&id) {
            return id;
        }
    }
}

/// The first non-finite number an intent carries, named, or None.
fn unreal_number(intent: &Intent) -> Option<&'static str> {
    if !intent.qty.is_finite() {
        return Some("quantity");
    }
    if let OrderKind::Limit { px, .. } = intent.kind {
        if !px.is_finite() {
            return Some("limit price");
        }
    }
    if let Some(stop) = intent.stop {
        if !stop.trigger_px.is_finite() {
            return Some("stop price");
        }
    }
    None
}

fn arrival_ns(event: &MarketEvent, fallback: u64) -> u64 {
    let stamp = match event {
        MarketEvent::Quote { quote, .. } => quote.recv_ns,
        MarketEvent::Depth { depth, .. } => depth.recv_ns,
        MarketEvent::Trades { trades, .. } => trades.recv_ns,
        MarketEvent::Ticker { ticker, .. } => ticker.recv_ns,
        MarketEvent::FeedReset { recv_ns } => *recv_ns,
    };
    if stamp == 0 {
        fallback
    } else {
        stamp
    }
}

/// Return a verdict that can be written and that cannot enlarge the request.
/// `serde_json` represents non-finite floats as `null`; round-tripping catches
/// them in every current and future denial field before they poison replay.
pub(crate) fn durable_risk_verdict(
    verdict: RiskVerdict,
    requested_qty: f64,
    exact_qty: bool,
) -> RiskVerdict {
    let valid = match &verdict {
        RiskVerdict::Allow { qty } => {
            requested_qty.is_finite()
                && requested_qty > 0.0
                && qty.is_finite()
                && *qty > 0.0
                && if exact_qty {
                    *qty == requested_qty
                } else {
                    *qty <= requested_qty
                }
        }
        RiskVerdict::Deny { .. } => serde_json::to_vec(&verdict)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<RiskVerdict>(&bytes).ok())
            .is_some(),
    };
    if valid {
        verdict
    } else {
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState {
                detail: "risk kernel returned a non-durable or invalid quantity verdict"
                    .to_string(),
            },
        }
    }
}

/// Both id tables as a log record, so every number in the log can be turned
/// back into a sleeve and a coin.
fn names_record(strategies: &[String], market: &MarketState) -> WalRecord {
    WalRecord::Names {
        strategies: strategies.to_vec(),
        symbols: (0..market.table.len())
            .map(|i| market.table.name(SymbolId(i as u16)).to_string())
            .collect(),
    }
}
