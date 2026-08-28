//! The loop.
//!
//! One process, one thread, one loop. The runtime is tokio's
//! `new_current_thread`, and nothing on the hot path is spawned, sent to a
//! pool, or shared with another thread: a market message is parsed by the
//! feed, applied to the shared picture, handed to the strategies that asked
//! for it, and the intent that comes back is logged, judged, made durable and
//! sent — all on the same thread, so there is no lock and no hand-off to
//! account for in the latency numbers. The only threads in the whole binary
//! belong to the bench's pretend venue, which stands in for a machine that is
//! genuinely somewhere else.
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
//! intent recorded, verdict recorded, order recorded **and forced to disk**,
//! and only then the bytes leave. A crash between the barrier and the reply
//! leaves an order the log knows about and no reply for it, which is exactly
//! what `engine replay` shows as in flight.

use std::collections::VecDeque;
use std::future::Future;
use std::time::Duration;

use engine_types::{
    quantize, AccountView, Action, AmendSpec, DenyReason, EngineEvent, InstrumentRule, Intent,
    MarketEvent, MarketFeed, MarketState, OrderFeed, OrderKind, OrderRequest, OrderUpdate,
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

fn wall_clock_ns() -> u64 {
    u64::try_from(clock::wall_ms())
        .unwrap_or(0)
        .saturating_mul(1_000_000)
}

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

/// The newest wall-clock stamp a log carries, of any kind. What fill
/// recovery measures its window from.
fn newest_stamp_ms(replayed: &[WalRecord]) -> Option<i64> {
    let mut newest: Option<i64> = None;
    for record in replayed {
        let stamp = match record {
            WalRecord::Boot { wall_ts_ms, .. }
            | WalRecord::Reconciled { wall_ts_ms, .. }
            | WalRecord::SegmentBase { wall_ts_ms, .. }
            | WalRecord::LatchCleared { wall_ts_ms, .. } => Some(*wall_ts_ms),
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill { venue_ts_ms, .. },
            } => Some(*venue_ts_ms),
            WalRecord::RecoveredFill { venue_ts_ms, .. } => Some(*venue_ts_ms),
            WalRecord::Markout { fill_ts_ms, .. } => Some(*fill_ts_ms),
            _ => None,
        };
        if let Some(stamp) = stamp {
            if newest.is_none_or(|n| stamp > n) {
                newest = Some(stamp);
            }
        }
    }
    newest
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

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum HaltCancelState {
    Submitting,
    AwaitingPrivate { deadline_ns: u64 },
}

pub struct Engine<W: Wal, R: RiskKernel, V: VenueGateway> {
    pub wal: W,
    pub risk: R,
    pub venue: V,
    strategies: Vec<Box<dyn Strategy>>,
    names: Vec<String>,
    market: MarketState,
    routing: Routing,
    rules: Vec<Option<InstrumentRule>>,
    timers: Timers,
    pending: VecDeque<Action>,
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
    /// Halt pulls bypass the ordinary per-wake action drain. One native-sized
    /// group is submitted per main-loop turn, with private order updates
    /// biased ahead of the next group.
    halt_cancel_queue: VecDeque<(SymbolId, String)>,
    /// Symbols a book has named that the engine does not follow yet.
    ///
    /// Filled while a book is being handled and drained by the run loop, which
    /// is the only place that holds the feeds. Admitting from inside
    /// `on_targets` would mean borrowing them out of the `select!` they are
    /// waiting in.
    wanted_symbols: Vec<String>,
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
    /// The newest control anchor per source, mirroring what was written to
    /// the log, so a rotation can restate it without re-reading the file.
    control_anchors: std::collections::BTreeMap<String, String>,
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
        if !sleeves.is_empty() && !prior_names.is_empty() && prior_names != names {
            return Err(EngineError::Boot(format!(
                "configured strategy identity/order {:?} does not match the WAL {:?}",
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

        // The newest control anchor per source, restored before anything is
        // judged. The risk anchor carries the UTC opening equity and any
        // daily-loss trip across restarts. A segment restatement carries the
        // same anchors and counts the same way — set, then overridden by
        // anything written after it.
        let mut control_anchors: std::collections::BTreeMap<String, String> = Default::default();
        for record in replayed {
            match record {
                WalRecord::ControlAnchor { source, state } => {
                    control_anchors.insert(source.clone(), state.clone());
                }
                WalRecord::SegmentBase {
                    control_anchors: anchors,
                    ..
                } => {
                    control_anchors = anchors
                        .iter()
                        .map(|anchor| (anchor.source.clone(), anchor.state.clone()))
                        .collect();
                }
                _ => {}
            }
        }
        if let Some(state) = control_anchors.get("risk") {
            risk.restore_control_anchor(state).map_err(|error| {
                EngineError::Boot(format!(
                    "cannot restore durable risk control anchor: {error}"
                ))
            })?;
        }

        let account = venue.account_view().await?;
        risk.observe_wall_clock_ns(wall_clock_ns());
        risk.observe_account_view(&account);
        if let Some(state) = risk.take_control_anchor() {
            wal.append(&WalRecord::ControlAnchor {
                source: "risk".to_string(),
                state: state.clone(),
            })?;
            wal.barrier()?;
            control_anchors.insert("risk".to_string(), state);
        }

        // Fills the venue saw and this log never heard: a stop that fired
        // during a deploy window, an execution inside a private-stream gap.
        // Recovered from the venue's own history and made durable before the
        // log is compared to the venue, so what actually traded is a fill in
        // the log rather than a finding against it.
        let mut recovered_exec_ids = ExecutionIds::from_records(replayed, boot_ms)
            .map_err(|e| EngineError::State(e.to_string()))?;
        let recovered_fills = Self::recover_missed_fills(
            &mut wal,
            &mut venue,
            replayed,
            &market.table,
            &mut recovered_exec_ids,
        )
        .await?;
        let effective_owned: Vec<WalRecord>;
        let effective: &[WalRecord] = if recovered_fills.is_empty() {
            replayed
        } else {
            effective_owned = replayed.iter().cloned().chain(recovered_fills).collect();
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
        let mut engine = Engine {
            wal,
            risk,
            venue,
            strategies,
            names,
            market,
            routing,
            rules,
            timers: Timers::default(),
            pending: VecDeque::new(),
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
            halt_cancel_queue: VecDeque::new(),
            wanted_symbols: Vec::new(),
            leverage_at: std::collections::HashMap::new(),
            may_open,
            private_stream_ready: true,
            control_anchors,
            logged_exposure,
            intended_stops,
            // Boot recovery just read the venue's history up to now; the
            // next gap starts here.
            recovered_until_ms: clock::wall_ms(),
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
    ) -> Result<Vec<WalRecord>, EngineError> {
        let now_ms = clock::wall_ms();
        let Some(newest) = newest_stamp_ms(replayed) else {
            // A fresh log has nothing to be behind on. Whatever the account
            // already holds predates this engine, and reconcile is what says
            // so.
            return Ok(Vec::new());
        };
        let since = newest - RECOVERY_PAD_MS;
        if since < now_ms - RECOVERY_REACH_MS {
            return Err(EngineError::Boot(format!(
                "the log is {} ms behind, beyond the venue execution-history reach of {} ms",
                now_ms - newest,
                RECOVERY_REACH_MS
            )));
        }
        if since >= now_ms {
            return Ok(Vec::new());
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
        if !out.is_empty() {
            tracing::warn!(
                count = recovered,
                "recovered fills the private stream never delivered"
            );
            wal.barrier()?;
        }
        Ok(out)
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
                    _ = flush_tick.tick() => self.on_tick().await?,
                    _ = tokio::time::sleep(timer_wait.unwrap_or_default()), if timer_wait.is_some() => {
                        self.on_timers().await?;
                    },
                    _ = std::future::ready(()), if !self.halt_cancel_queue.is_empty() => {
                        self.dispatch_halt_cancel_group().await?;
                    }
                    event = market_feed.next_event() => match event {
                        Ok(event) => self.on_market(event).await?,
                        Err(engine_types::FeedError::Closed) => {
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
                event = market_feed.next_event() => match event {
                    Ok(event) => self.on_market(event).await?,
                    Err(engine_types::FeedError::Closed) => {
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

    /// Last ledger line on the way out, and the whole tail forced to disk:
    /// a graceful stop that leaves its closing updates in the page cache
    /// tells the next boot's audit a lie.
    pub async fn finish(&mut self) -> Result<(), EngineError> {
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
        for name in wanted {
            let core_id = self.market.add_symbol(&name);
            let feed_id = market_feed.admit(&name, engine_types::Feed::Quote);
            let venue_id = self.venue.add_symbol(&name);
            if feed_id != Some(core_id) || venue_id != Some(core_id) {
                tracing::error!(
                    symbol = %name,
                    ?core_id,
                    ?feed_id,
                    ?venue_id,
                    "the parts of the engine disagree about this symbol's id; it will not be \
                     traded. Nothing else is affected — the ids already handed out do not move."
                );
                continue;
            }
            order_feed.learn(&name, core_id);
            self.routing.size_to(self.market.table.len());
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
        self.risk.observe_wall_clock_ns(wall_clock_ns());
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

    /// Pull every still-live opening order once either the account-level risk
    /// breaker or reconciliation's durable opening latch is set. The durable
    /// state is written by the caller before this queue reaches the venue.
    /// Foreign and reduce-only orders are left alone: cancelling another
    /// writer's order or a protective exit is not a safe guess.
    fn queue_halted_entry_cancels(&mut self) -> Result<(), EngineError> {
        if self.may_open && self.private_stream_ready && !self.risk.entries_halted() {
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
        self.process_cancels(requests).await
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
            if self.market.table.get(&target.symbol).is_none()
                && !self.wanted_symbols.contains(&target.symbol)
            {
                self.wanted_symbols.push(target.symbol.clone());
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
        if now.saturating_sub(self.account.observed_ns) >= self.refresh_after_ns {
            match self.venue.account_view().await {
                Ok(view) => {
                    self.adopt_view(view);
                    self.persist_control_anchor()?;
                    self.enforce_position_stop_intent().await?;
                }
                // Keeping the old reading is not the same as trusting it: it
                // ages, and the risk kernel refuses on an old reading.
                Err(e) => tracing::warn!(error = %e, "could not refresh the account reading"),
            }
        }
        self.queue_halted_entry_cancels()?;

        // Every resting entry gets one look. Read the clock again: the
        // account refresh above is a venue round trip, and the stamp from
        // before it is old by the time we get here.
        let now = clock::now_ns();
        if self.may_open && self.private_stream_ready && !self.risk.entries_halted() {
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
        heartbeat.write(
            now_ns,
            &heartbeat::Facts {
                may_open: effective_may_open,
                market_events: *events_seen,
                orders_sent: *orders_sent,
                strategies: names,
                decide: ledger.quantiles(Segment::Decide),
                wire: ledger.quantiles(Segment::Wire),
                equity_usdt: account.equity_usdt,
                available_usdt: account.available_usdt,
                // The age, not the stamp: this engine's clock is monotonic
                // and means nothing outside this process.
                account_age_ns: (account.observed_ns != 0)
                    .then(|| now_ns.saturating_sub(account.observed_ns)),
                holdings: &holdings,
                entry_blockers: &blockers,
                costs: &costs,
            },
        );
    }

    async fn drain(&mut self, origin_ns: u64) -> Result<(), EngineError> {
        let mut handled = 0usize;
        let mut adding_dropped = 0usize;
        let mut placements = Vec::new();
        let mut cancellations = Vec::new();
        let mut hard_cap_hit = false;
        loop {
            while let Some(action) = self.pending.pop_front() {
                handled += 1;
                // Past the cap, whatever adds risk is dropped but whatever sheds
                // it still flows: an exit or a cancel queued behind a flood must
                // get out, or its strategy is stranded holding a position — or an
                // order — it believes it is rid of. An amend counts as adding: it
                // can raise the size of a resting order. The hard cap bounds even
                // the de-risking ones against a runaway loop.
                if handled > MAX_INTENTS_PER_WAKE && !action.is_risk_reducing() {
                    adding_dropped += 1;
                    continue;
                }
                if handled > MAX_INTENTS_PER_WAKE * 4 {
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
                match action {
                    Action::Place(intent) => {
                        self.process_cancels(std::mem::take(&mut cancellations))
                            .await?;
                        placements.push(intent);
                        if placements.len() == MAX_ORDERS_PER_BATCH {
                            self.process_intents(std::mem::take(&mut placements), origin_ns)
                                .await?;
                        }
                    }
                    Action::Cancel {
                        symbol,
                        client_order_id,
                    } => {
                        self.process_intents(std::mem::take(&mut placements), origin_ns)
                            .await?;
                        if self.risk.entries_halted() && self.is_live_opening(&client_order_id) {
                            self.process_cancels(std::mem::take(&mut cancellations))
                                .await?;
                            self.enqueue_halt_cancel(symbol, client_order_id);
                        } else {
                            cancellations.push((symbol, client_order_id));
                            if cancellations.len() == MAX_CANCELS_PER_BATCH {
                                self.process_cancels(std::mem::take(&mut cancellations))
                                    .await?;
                            }
                        }
                    }
                    Action::Amend {
                        symbol,
                        client_order_id,
                        spec,
                    } => {
                        self.process_intents(std::mem::take(&mut placements), origin_ns)
                            .await?;
                        self.process_cancels(std::mem::take(&mut cancellations))
                            .await?;
                        let taken = self
                            .process_amend(symbol, &client_order_id, spec, origin_ns)
                            .await?;
                        self.working
                            .amended(&client_order_id, spec.px, taken, clock::now_ns());
                    }
                    Action::SetStop { symbol, trigger_px } => {
                        self.process_intents(std::mem::take(&mut placements), origin_ns)
                            .await?;
                        self.process_cancels(std::mem::take(&mut cancellations))
                            .await?;
                        self.process_set_stop(symbol, trigger_px).await?;
                    }
                }
            }
            self.process_intents(std::mem::take(&mut placements), origin_ns)
                .await?;
            self.process_cancels(std::mem::take(&mut cancellations))
                .await?;
            if hard_cap_hit || self.pending.is_empty() {
                break;
            }
        }
        if adding_dropped > 0 {
            tracing::error!(
                adding_dropped,
                "too many actions in one wake; entries and amends were dropped"
            );
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "dropped {adding_dropped} entries and amends: more than {MAX_INTENTS_PER_WAKE} actions in one wake (exits and cancels still flowed)"
                ),
            })?;
        }
        self.persist_control_anchor()
    }

    /// Write a changed control anchor down and force it to disk. State that
    /// outlives a process has to be durable the moment it changes, or a
    /// crash-loop refreshes the daily loss budget.
    fn persist_control_anchor(&mut self) -> Result<(), EngineError> {
        if self.append_control_anchor()? {
            self.wal.barrier()?;
        }
        Ok(())
    }

    /// Append a changed anchor without its own barrier. The order batch path
    /// folds this record into the same durability barrier as its sibling
    /// `OrderSent` records, so the first order of a UTC day cannot reach the
    /// venue before that day's opening-equity anchor reaches stable storage.
    fn append_control_anchor(&mut self) -> Result<bool, EngineError> {
        let Some(state) = self.risk.take_control_anchor() else {
            return Ok(false);
        };
        // Mirrored so a rotation can restate the newest anchor without
        // re-reading the log.
        self.control_anchors.insert("risk".into(), state.clone());
        self.wal.append(&WalRecord::ControlAnchor {
            source: "risk".into(),
            state,
        })?;
        Ok(true)
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

        // UTC can roll between account polls. Advance the risk clock at the
        // decision boundary so the first new-day order cannot use yesterday's
        // budget; the batch barrier below makes any changed anchor durable.
        self.risk.observe_wall_clock_ns(wall_clock_ns());
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
        let Some(qty) = quantize::quantize_qty(allowed_qty, &rule) else {
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
        let kind = match intent.kind {
            OrderKind::Market => OrderKind::Market,
            OrderKind::Limit { px, tif } => OrderKind::Limit {
                px: quantize::quantize_px(px, intent.side, &rule),
                tif,
            },
        };
        if let Some(reference_px) = self.reference_px(intent.symbol, &kind) {
            let notional = qty * reference_px;
            if notional + 1e-9 < rule.min_notional {
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
        self.covers.register(
            intent.strategy,
            request.symbol,
            request.side,
            qty,
            &self.account,
        );
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
    ) -> Result<(), EngineError> {
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
        for order in self.orders.in_flight() {
            let request = &order.request;
            let Some(stop) = request.stop.filter(|_| !request.reduce_only) else {
                continue;
            };
            let key = stop_key(request.symbol, request.side);
            batch_protection
                .entry(key)
                .and_modify(|protected| {
                    *protected = tighter_stop(request.side, *protected, stop.trigger_px)
                })
                .or_insert(stop.trigger_px);
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
            // An assessment can roll or trip the durable loss guard even when
            // another control refuses the order.
            self.persist_control_anchor()?;
            return Ok(());
        }

        self.append_control_anchor()?;
        self.wal.barrier()?;
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

        let send_started_ns = clock::now_ns();
        let replies = self.venue.send_orders(&requests).await;
        let returned_ns = clock::now_ns();
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

        let mut replies = replies.into_iter();
        for (request, (decided_ns, origin_ns)) in requests.into_iter().zip(timings) {
            self.ledger
                .record(Segment::Wire, returned_ns.saturating_sub(decided_ns));
            let reply = replies.next().unwrap_or_else(|| {
                Err(VenueError::BadReply(
                    "the venue omitted this order from its batch reply".to_string(),
                ))
            });
            let update = match reply {
                Ok(ack) => {
                    let ack_ns = if ack.ack_ns > send_started_ns {
                        ack.ack_ns
                    } else {
                        returned_ns
                    };
                    self.ledger
                        .record(Segment::Ack, ack_ns.saturating_sub(send_started_ns));
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
        Ok(())
    }

    /// Pull a resting order.
    ///
    /// Not the order path in miniature: there is no barrier before the wire.
    /// A cancel adds no exposure, and an order the log still shows working is
    /// recovered at the next boot whether or not the cancel survived a crash
    /// — so the fsync would buy nothing. `origin_ns` is taken but not
    /// recorded: the latency ledger measures the order path, and mixing a
    /// barrier-free cancel into "out the door" would flatter that number.
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
    ) -> Result<(), EngineError> {
        if requests.is_empty() {
            return Ok(());
        }
        if let [(symbol, client_order_id)] = requests.as_slice() {
            let symbol = *symbol;
            let client_order_id = client_order_id.clone();
            let taken = self
                .process_cancel(symbol, &client_order_id, clock::now_ns())
                .await?;
            self.working.cancelled(&client_order_id, taken);
            if let Some(state) = self.halt_cancels.get_mut(&client_order_id) {
                if taken {
                    *state = HaltCancelState::AwaitingPrivate {
                        deadline_ns: clock::now_ns().saturating_add(HALT_CANCEL_CONFIRM_NS),
                    };
                } else {
                    return Err(EngineError::State(format!(
                        "account-level halt could not cancel opening order {client_order_id}; restarting for venue reconciliation"
                    )));
                }
            }
            return Ok(());
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

        let replies = self.venue.cancel_orders(&requests).await;
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
        let mut replies = replies.into_iter();
        let mut halt_failure = None;
        let accepted_deadline = clock::now_ns().saturating_add(HALT_CANCEL_CONFIRM_NS);
        for (_, client_order_id) in requests {
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
                        halt_failure = Some(format!("{client_order_id}: {code}: {message}"));
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
        if let Some(detail) = halt_failure {
            return Err(EngineError::State(format!(
                "account-level halt left at least one opening cancel unconfirmed ({detail}); restarting for venue reconciliation"
            )));
        }
        Ok(())
    }

    async fn process_cancel(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        _origin_ns: u64,
    ) -> Result<bool, EngineError> {
        self.wal.append(&WalRecord::CancelSent {
            symbol,
            client_order_id: client_order_id.to_string(),
            wire_ns: clock::now_ns(),
        })?;

        match self.venue.cancel_order(symbol, client_order_id).await {
            Ok(()) => Ok(true),
            // A request that could not be built never left the box: the
            // resting order is untouched, which is certainty rather than
            // doubt. No synthetic reject — that would end an order at the
            // venue's expense in our own book only.
            Err(VenueError::BadRequest(detail)) => {
                tracing::error!(id = client_order_id, detail, "cancel never sent");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!("cancel of {client_order_id} never sent: {detail}"),
                })?;
                Ok(false)
            }
            Err(other) => {
                // We do not know whether the venue got it. The order stays in
                // flight either way, and the private stream will say if it
                // went; a retry from here is how one gets cancelled twice.
                tracing::error!(id = client_order_id, error = %other, "cancel failed with no answer");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "cancel of {client_order_id} sent with no answer ({other}); the order is still counted as working"
                    ),
                })?;
                Ok(false)
            }
        }
    }

    /// True when the change went through; see [`process_cancel`] for what the
    /// answer means.
    ///
    /// [`process_cancel`]: Engine::process_cancel
    async fn process_amend(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        mut spec: AmendSpec,
        _origin_ns: u64,
    ) -> Result<bool, EngineError> {
        if !self.may_open || self.risk.entries_halted() || !self.private_stream_ready {
            let halt = if !self.may_open {
                "reconciliation opening latch is set"
            } else if self.risk.entries_halted() {
                "account-level entry halt is latched"
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
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: its prior amend outcome is still ambiguous; cancellation queued"
                ),
            })?;
            self.pending.push_front(Action::Cancel {
                symbol,
                client_order_id: client_order_id.to_string(),
            });
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
            self.risk.observe_wall_clock_ns(wall_clock_ns());
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
                    self.persist_control_anchor()?;
                    return Ok(false);
                }
                RiskVerdict::Deny { reason } => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!(
                            "{client_order_id} not amended at {requested_px}: {reason:?}"
                        ),
                    })?;
                    self.persist_control_anchor()?;
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
            self.append_control_anchor()?;
            self.wal.barrier()?;
        }

        match self.venue.amend_order(symbol, client_order_id, spec).await {
            Ok(()) => {
                // Bybit's REST success acknowledges only that an asynchronous
                // amend request was accepted. No private update in the common
                // contract proves its effective price/version yet, so old and
                // requested prices both remain possible. Keep the range
                // charged and cancel rather than falsely resolving it.
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "amend of {client_order_id} was accepted asynchronously; its price remains ambiguous and cancellation is queued"
                    ),
                })?;
                self.pending.push_front(Action::Cancel {
                    symbol,
                    client_order_id: client_order_id.to_string(),
                });
                Ok(false)
            }
            Err(VenueError::BadRequest(detail)) => {
                tracing::error!(id = client_order_id, detail, "amend never sent");
                let resolved = WalRecord::AmendResolved {
                    client_order_id: client_order_id.to_string(),
                    effective_px: old_px,
                };
                self.wal.append(&resolved)?;
                self.orders.apply(&resolved);
                if !existing.request.reduce_only {
                    let mut old = amended_intent.clone();
                    old.kind = OrderKind::Limit { px: old_px, tif };
                    self.risk
                        .register_order(client_order_id, &old, remaining_qty);
                }
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!("amend of {client_order_id} never sent: {detail}"),
                })?;
                Ok(false)
            }
            Err(VenueError::Rejected { code, message }) => {
                let resolved = WalRecord::AmendResolved {
                    client_order_id: client_order_id.to_string(),
                    effective_px: old_px,
                };
                self.wal.append(&resolved)?;
                self.orders.apply(&resolved);
                if !existing.request.reduce_only {
                    let mut old = amended_intent.clone();
                    old.kind = OrderKind::Limit { px: old_px, tif };
                    self.risk
                        .register_order(client_order_id, &old, remaining_qty);
                }
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "amend of {client_order_id} rejected by venue ({code}: {message})"
                    ),
                })?;
                Ok(false)
            }
            Err(other) => {
                // The order is still working; we just do not know at which
                // price. Keep both outcomes charged and cancel it.
                tracing::error!(id = client_order_id, error = %other, "amend failed with no answer");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "amend of {client_order_id} sent with no answer ({other}); its price and size are unconfirmed"
                    ),
                })?;
                self.pending.push_front(Action::Cancel {
                    symbol,
                    client_order_id: client_order_id.to_string(),
                });
                Ok(false)
            }
        }
    }

    /// After a private-stream gap: ask the venue what traded while the
    /// stream was away and fold anything the log missed into the same books
    /// a delivered fill feeds. Failure stops the run because the missing
    /// interval makes the exposure state unknown.
    async fn recover_gap_fills(&mut self) -> Result<(), EngineError> {
        let now_ms = clock::wall_ms();
        let since = (self.recovered_until_ms - RECOVERY_PAD_MS).max(now_ms - RECOVERY_REACH_MS);
        if since >= now_ms {
            return Ok(());
        }
        let mut execs = match self.venue.executions(since, now_ms).await {
            Ok(execs) => execs,
            Err(error) => {
                self.may_open = false;
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: now_ms,
                    findings: vec![format!(
                        "execution history is unavailable after a private-stream gap: {error}"
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
            tracing::warn!(count = recovered, "recovered fills from the stream gap");
            if !foreign.is_empty() {
                self.may_open = false;
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: now_ms,
                    findings: foreign,
                    may_open: false,
                })?;
            }
            self.wal.barrier()?;
        }
        self.recovered_until_ms = now_ms;
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

    /// Every order update, wherever it came from, goes through here.
    async fn take_update(&mut self, update: OrderUpdate) -> Result<(), EngineError> {
        let stream_reset = matches!(&update, OrderUpdate::StreamReset { .. });
        if stream_reset {
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
        self.wal.append(&WalRecord::OrderUpdate {
            update: update.clone(),
        })?;
        if let Some(exec_id) = delivered_exec_id {
            self.recovered_exec_ids.insert(exec_id, dedup_seen_ms);
        }
        self.risk.on_update(&update);
        self.orders.apply_update(&update);
        if let Some(client_order_id) = inflight::client_order_id(&update) {
            let still_live = self
                .orders
                .orders
                .get(client_order_id)
                .is_some_and(|order| order.in_flight());
            if !still_live {
                self.halt_cancels.remove(client_order_id);
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
                    self.persist_control_anchor()?;
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
        Ok(())
    }

    fn refuse(
        &mut self,
        client_order_id: &str,
        intent: &Intent,
        why: &str,
    ) -> Result<(), EngineError> {
        tracing::warn!(id = client_order_id, tag = %intent.tag, why, "order not sent");
        self.wal.append(&WalRecord::Note {
            source: "engine".into(),
            text: format!("{client_order_id} not sent ({}): {why}", intent.tag),
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
    /// own helpers, the anchor map mirrors every anchor written, and recent
    /// execution ids come from the same bounded dedup set used live — so
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
            control_anchors: self
                .control_anchors
                .iter()
                .map(|(source, state)| engine_types::AnchorState {
                    source: source.clone(),
                    state: state.clone(),
                })
                .collect(),
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
