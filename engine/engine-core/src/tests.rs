//! The loop, tested against mocks that write down what happened and when.
//!
//! The mocks all share one tape, so a test can assert not just that the log
//! was written and the order sent, but that they happened in that order —
//! which is the whole promise of the durability barrier.

use std::collections::VecDeque;
use std::sync::{Arc as Rc, Mutex as RefCell};
use std::sync::{Arc, Mutex};
use std::time::Duration;

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
        WalRecord::SegmentBase { .. } => "segment_base",
        WalRecord::RecoveredFill { .. } => "recovered_fill",
        WalRecord::ExecutionHistoryCheckpoint { .. } => "execution_history_checkpoint",
        WalRecord::LatchCleared { .. } => "latch_cleared",
        WalRecord::ClaimsDropped { .. } => "claims_dropped",
        WalRecord::TargetBookLatch { .. } => "target_book_latch",
        WalRecord::StrategyCheckpoint { .. } => "strategy_checkpoint",
        WalRecord::StrategyGlobalCheckpoint { .. } => "strategy_global_checkpoint",
        WalRecord::StrategyEventPublished { .. } => "strategy_event_published",
        WalRecord::StrategyEventConsumed { .. } => "strategy_event_consumed",
        WalRecord::SignalObservation { .. } => "signal_observation",
        WalRecord::SignalObservationConsumed { .. } => "signal_observation_consumed",
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

struct MockWal {
    tape: Tape,
    records: Rc<RefCell<Vec<WalRecord>>>,
    seq: u64,
    fail_on: Option<String>,
    /// A tape the barrier's own thread can also write to. The ordinary tape
    /// is an `Rc` and cannot leave this thread, and the whole point of a
    /// barrier that runs beside the send is that something else finishes it.
    /// Set by `defer_barriers`; `None` keeps barriers synchronous.
    crossing_tape: Option<Arc<Mutex<Vec<&'static str>>>>,
    /// How long the deferred barrier takes. Long enough that a caller which
    /// does not wait for it visibly does not.
    barrier_takes: Duration,
}

impl MockWal {
    fn new(tape: Tape) -> (Self, Rc<RefCell<Vec<WalRecord>>>) {
        let records = Rc::new(RefCell::new(Vec::new()));
        (
            MockWal {
                tape,
                records: records.clone(),
                seq: 0,
                fail_on: None,
                crossing_tape: None,
                barrier_takes: Duration::from_millis(30),
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

impl Wal for MockWal {
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
        self.tape.lock().unwrap().push(Step::Barrier);
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

struct MockVenue {
    tape: Tape,
    /// Shared with the log's deferred barrier, so one ordered list holds the
    /// send, the disk's answer, and the news that follows. Set by
    /// `watch_with`; `None` records nothing.
    crossing_tape: Option<Arc<Mutex<Vec<&'static str>>>>,
    rules: Vec<(Symbol, InstrumentRule)>,
    sends: Rc<RefCell<Vec<OrderRequest>>>,
    cancels: Rc<RefCell<Vec<(SymbolId, String)>>>,
    amends: Rc<RefCell<Vec<(SymbolId, String, AmendSpec)>>>,
    stops: Rc<RefCell<Vec<(SymbolId, f64)>>>,
    stop_failures_remaining: Rc<RefCell<usize>>,
    caps: VenueCaps,
    reply: Option<VenueError>,
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
}

impl MockVenue {
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
                tape,
                crossing_tape: None,
                rules,
                sends: sends.clone(),
                cancels: Rc::new(RefCell::new(Vec::new())),
                amends: Rc::new(RefCell::new(Vec::new())),
                stops: Rc::new(RefCell::new(Vec::new())),
                stop_failures_remaining: Rc::new(RefCell::new(0)),
                caps: bybit_like_caps(),
                reply: None,
                send_delay: Duration::ZERO,
                working: Vec::new(),
                account_readings: Rc::new(RefCell::new(VecDeque::new())),
                account_view_fails: Rc::new(RefCell::new(false)),
                leverages: Rc::new(RefCell::new(Vec::new())),
                executions: Rc::new(RefCell::new(Some(Vec::new()))),
            },
            sends,
        )
    }
}

#[engine_types::async_trait]
impl VenueGateway for MockVenue {
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
        _start_ms: i64,
        _end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        match self.executions.lock().unwrap().as_ref() {
            Some(execs) => Ok(execs.clone()),
            None => Err(VenueError::BadRequest(
                "this venue cannot list its execution history".to_string(),
            )),
        }
    }

    async fn cancel_order(&mut self, symbol: SymbolId, id: &str) -> Result<(), VenueError> {
        self.tape.lock().unwrap().push(Step::Cancel(id.to_string()));
        self.cancels.lock().unwrap().push((symbol, id.to_string()));
        Ok(())
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
        let mut failures_remaining = self.stop_failures_remaining.lock().unwrap();
        if *failures_remaining > 0 {
            *failures_remaining -= 1;
            return Err(VenueError::Transport("scripted stop failure".into()));
        }
        Ok(())
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
        self.tape.lock().unwrap().push(Step::ReadAccount);
        if *self.account_view_fails.lock().unwrap() {
            return Err(VenueError::Transport(
                "scripted account-view failure".to_string(),
            ));
        }
        let positions = self
            .account_readings
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_default();
        Ok(AccountView {
            equity_usdt: 10_000.0,
            available_usdt: 9_000.0,
            positions,
            observed_ns: clock::now_ns(),
        })
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

struct MockRisk {
    verdict: RiskVerdict,
    amend_verdict: Option<RiskVerdict>,
    seen: Rc<RefCell<Vec<OrderUpdate>>>,
    registered: Rc<RefCell<Vec<(String, f64)>>>,
}

impl MockRisk {
    /// `Allow { qty: NaN }` means "whatever was asked for".
    fn with(verdict: RiskVerdict) -> (Self, Rc<RefCell<Vec<OrderUpdate>>>) {
        let seen = Rc::new(RefCell::new(Vec::new()));
        (
            MockRisk {
                verdict,
                amend_verdict: None,
                seen: seen.clone(),
                registered: Rc::new(RefCell::new(Vec::new())),
            },
            seen,
        )
    }
}

impl RiskKernel for MockRisk {
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
}

/// Plays a script, then either closes or waits forever.
struct ScriptFeed {
    events: VecDeque<MarketEvent>,
    close_at_end: bool,
    /// Symbols admitted after boot, in order, with the ids handed back.
    admitted: Rc<RefCell<Vec<(String, SymbolId)>>>,
    /// How many symbols this feed already knows, so an admission gets the
    /// next id — the same rule the real feed's table follows.
    known: u16,
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
            known: 1,
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
            known: 1,
            admits_wrongly: false,
        }
    }
}

impl MarketFeed for ScriptFeed {
    fn admit(&mut self, symbol: &str, _feed: engine_types::Feed) -> Option<SymbolId> {
        if let Some((_, id)) = self
            .admitted
            .lock()
            .unwrap()
            .iter()
            .find(|(known, _)| known == symbol)
        {
            return Some(*id);
        }
        let id = if self.admits_wrongly {
            SymbolId(self.known + 7)
        } else {
            SymbolId(self.known)
        };
        self.known += 1;
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
    tape: Tape,
    records: Rc<RefCell<Vec<WalRecord>>>,
    sends: Rc<RefCell<Vec<OrderRequest>>>,
    cancels: Rc<RefCell<Vec<(SymbolId, String)>>>,
    amends: Rc<RefCell<Vec<(SymbolId, String, AmendSpec)>>>,
    stops: Rc<RefCell<Vec<(SymbolId, f64)>>>,
    stop_failures_remaining: Rc<RefCell<usize>>,
    risk_saw: Rc<RefCell<Vec<OrderUpdate>>>,
    leverages: Rc<RefCell<Vec<(SymbolId, f64)>>>,
    /// Positions the venue's next account readings will report; see
    /// `MockVenue::account_readings`.
    account_readings: Rc<RefCell<VecDeque<Vec<engine_types::PositionView>>>>,
    account_view_fails: Rc<RefCell<bool>>,
    /// The venue's execution history; see `MockVenue::executions`.
    executions: Rc<RefCell<Option<Vec<VenueExecution>>>>,
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
    let account_readings = venue.account_readings.clone();
    let account_view_fails = venue.account_view_fails.clone();
    let executions = venue.executions.clone();
    let (risk, risk_saw) = MockRisk::with(verdict);
    let replayed = replay_with_history_boundary(replayed);
    let engine = Engine::boot(
        &settings(),
        "0000000000000000",
        wal,
        risk,
        venue,
        strategies,
        &replayed,
    )
    .await
    .expect("boot");
    (
        engine,
        Harness {
            tape,
            records,
            sends,
            cancels,
            amends,
            stops,
            stop_failures_remaining,
            risk_saw,
            leverages,
            account_readings,
            account_view_fails,
            executions,
        },
    )
}

#[derive(Default)]
struct BuildOptions {
    amend_verdict: Option<RiskVerdict>,
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
    let cancels = venue.cancels.clone();
    let amends = venue.amends.clone();
    let stops = venue.stops.clone();
    let stop_failures_remaining = venue.stop_failures_remaining.clone();
    let leverages = venue.leverages.clone();
    let account_readings = venue.account_readings.clone();
    let account_view_fails = venue.account_view_fails.clone();
    let executions = venue.executions.clone();
    let (mut risk, risk_saw) = MockRisk::with(verdict);
    risk.amend_verdict = options.amend_verdict;
    let replayed = replay_with_history_boundary(replayed);
    let engine = Engine::boot(
        settings,
        "0000000000000000",
        wal,
        risk,
        venue,
        strategies,
        &replayed,
    )
    .await
    .expect("boot");
    (
        engine,
        Harness {
            tape,
            records,
            sends,
            cancels,
            amends,
            stops,
            stop_failures_remaining,
            risk_saw,
            leverages,
            account_readings,
            account_view_fails,
            executions,
        },
    )
}

/// Test fixtures often name only the records relevant to their assertion.
/// A real pre-checkpoint WAL still starts with Boot, which is the compatible
/// recovery boundary. Supply that omitted framing without weakening boot's
/// refusal of an actually unbounded existing log.
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

fn allow_all() -> RiskVerdict {
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
mod gap_recovery;
mod heartbeat;
mod order_path;
mod quote_staleness;
mod reconciliation;
mod resting_orders;
mod rotation;
mod runtime_controls;
mod strategy_checkpoints;
mod strategy_events;
mod worked_entries;
