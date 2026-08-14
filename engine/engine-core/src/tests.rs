//! The loop, tested against mocks that write down what happened and when.
//!
//! The mocks all share one tape, so a test can assert not just that the log
//! was written and the order sent, but that they happened in that order —
//! which is the whole promise of the durability barrier.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_types::{
    AccountIdentity, AccountView, AmendSpec, DenyReason, EngineEvent, Feed, FeedError,
    InstrumentRule, Intent,
    MarketEvent, MarketFeed, OrderAck, OrderFeed, OrderKind, OrderRequest, OrderUpdate, Quote,
    RiskKernel, RiskVerdict, Side, StopSpec, Strategy, StrategyCtx, StrategyId, Subscription,
    Symbol, SymbolId, TimerId, VenueCaps, VenueError, VenueGateway, Wal, WalError, WalRecord,
    WorkPolicy, VenueOrder,};

use crate::bench::{self, BenchOptions};
use crate::clock;
use crate::config::EngineSection;
use crate::engine::Engine;
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
}

type Tape = Rc<RefCell<Vec<Step>>>;

fn tape() -> Tape {
    Rc::new(RefCell::new(Vec::new()))
}

fn kind_of(record: &WalRecord) -> String {
    match record {
        WalRecord::Boot { .. } => "boot",
        WalRecord::Intent { .. } => "intent",
        WalRecord::Verdict { .. } => "verdict",
        WalRecord::OrderSent { .. } => "order_sent",
        WalRecord::OrderUpdate { .. } => "order_update",
        WalRecord::Markout { .. } => "markout",
        WalRecord::Names { .. } => "names",
        WalRecord::CancelSent { .. } => "cancel_sent",
        WalRecord::AmendSent { .. } => "amend_sent",
        WalRecord::LatencyLedger { .. } => "latency_ledger",
        WalRecord::Note { .. } => "note",
        WalRecord::ControlAnchor { .. } => "control_anchor",
        WalRecord::Reconciled { .. } => "reconciled",
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
    let tape = tape.borrow();
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
    let tape = tape.borrow();
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
    tape.borrow().iter().position(|s| s == step)
}

/// Where a step first appears after some earlier point. Boot writes and
/// fsyncs its own records, so a test about an order's barrier has to look
/// past them.
fn after(tape: &Tape, step: &Step, from: usize) -> Option<usize> {
    tape.borrow().iter().skip(from).position(|s| s == step).map(|i| i + from)
}

/// The first note whose text contains `needle`. Boot writes a note of its
/// own, so tests look for the one they mean.
fn note_saying(records: &Rc<RefCell<Vec<WalRecord>>>, needle: &str) -> String {
    records
        .borrow()
        .iter()
        .find_map(|r| match r {
            WalRecord::Note { text, .. } if text.contains(needle) => Some(text.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no note containing {needle:?}"))
}

fn appends(tape: &Tape) -> Vec<String> {
    tape.borrow()
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
            },
            records,
        )
    }
}

impl Wal for MockWal {
    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
        let kind = kind_of(record);
        if self.fail_on.as_deref() == Some(kind.as_str()) {
            return Err(WalError::Io(std::io::Error::other("test failure")));
        }
        self.seq += 1;
        self.tape.borrow_mut().push(Step::Append(kind));
        self.records.borrow_mut().push(record.clone());
        Ok(self.seq)
    }

    fn barrier(&mut self) -> Result<(), WalError> {
        self.tape.borrow_mut().push(Step::Barrier);
        Ok(())
    }

    fn flush(&mut self) -> Result<(), WalError> {
        self.tape.borrow_mut().push(Step::Flush);
        Ok(())
    }
}

/// What a venue can do, unless a test says otherwise: the shipping gateway's
/// own answers, so a test that changes one is visibly about that capability.
fn bybit_like_caps() -> VenueCaps {
    VenueCaps {
        native_position_stop: true,
        amend_in_place: true,
        post_only: true,
        batch_orders: false,
        set_leverage: true,
    }
}

struct MockVenue {
    tape: Tape,
    rules: Vec<(Symbol, InstrumentRule)>,
    sends: Rc<RefCell<Vec<OrderRequest>>>,
    cancels: Rc<RefCell<Vec<(SymbolId, String)>>>,
    amends: Rc<RefCell<Vec<(SymbolId, String, AmendSpec)>>>,
    caps: VenueCaps,
    reply: Option<VenueError>,
    /// What the venue would say it is working. Seeded by a test that wants
    /// boot to find an order the log does not know about.
    working: Vec<VenueOrder>,
    /// Every leverage the engine actually told the venue about, in order.
    leverages: Rc<RefCell<Vec<(SymbolId, f64)>>>,
    /// Make set_leverage refuse, so a test can watch the order not go.
    leverage_refuses: bool,
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
                rules,
                sends: sends.clone(),
                cancels: Rc::new(RefCell::new(Vec::new())),
                amends: Rc::new(RefCell::new(Vec::new())),
                caps: bybit_like_caps(),
                reply: None,
                working: Vec::new(),
                leverages: Rc::new(RefCell::new(Vec::new())),
                leverage_refuses: false,
            },
            sends,
        )
    }
}

impl VenueGateway for MockVenue {
    fn caps(&self) -> VenueCaps {
        self.caps
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        Ok(AccountIdentity {
            user_id: "7000001".to_string(),
            realm: "demo".to_string(),
        })
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        self.tape
            .borrow_mut()
            .push(Step::Send(req.client_order_id.clone()));
        self.sends.borrow_mut().push(req.clone());
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
            ack_ns: clock::now_ns(),
        })
    }

    async fn cancel_order(&mut self, symbol: SymbolId, id: &str) -> Result<(), VenueError> {
        self.tape.borrow_mut().push(Step::Cancel(id.to_string()));
        self.cancels.borrow_mut().push((symbol, id.to_string()));
        Ok(())
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.tape.borrow_mut().push(Step::Amend(id.to_string()));
        self.amends.borrow_mut().push((symbol, id.to_string(), spec));
        Ok(())
    }

    async fn set_stop(&mut self, _symbol: SymbolId, _trigger_px: f64) -> Result<(), VenueError> {
        Ok(())
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
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
        self.leverages.borrow_mut().push((symbol, leverage));
        if self.leverage_refuses {
            return Err(VenueError::Rejected {
                code: 110044,
                message: "leverage limit exceeded".to_string(),
            });
        }
        Ok(())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        self.tape.borrow_mut().push(Step::ReadAccount);
        Ok(AccountView {
            equity_usdt: 10_000.0,
            available_usdt: 9_000.0,
            positions: Vec::new(),
            observed_ns: clock::now_ns(),
        })
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.tape.borrow_mut().push(Step::ReadRules);
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
    seen: Rc<RefCell<Vec<OrderUpdate>>>,
    restored: Rc<RefCell<Vec<String>>>,
    anchor_script: VecDeque<String>,
    registered: Rc<RefCell<Vec<(String, f64)>>>,
}

impl MockRisk {
    /// `Allow { qty: NaN }` means "whatever was asked for".
    fn with(verdict: RiskVerdict) -> (Self, Rc<RefCell<Vec<OrderUpdate>>>) {
        let seen = Rc::new(RefCell::new(Vec::new()));
        (
            MockRisk {
                verdict,
                seen: seen.clone(),
                restored: Rc::new(RefCell::new(Vec::new())),
                anchor_script: VecDeque::new(),
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
        self.seen.borrow_mut().push(update.clone());
    }

    fn take_control_anchor(&mut self) -> Option<String> {
        self.anchor_script.pop_front()
    }

    fn register_order(&mut self, client_order_id: &str, _intent: &Intent, approved_qty: f64) {
        self.registered
            .borrow_mut()
            .push((client_order_id.to_string(), approved_qty));
    }

    fn restore_control_anchor(&mut self, state: &str) {
        self.restored.borrow_mut().push(state.to_string());
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
        let id = if self.admits_wrongly {
            SymbolId(self.known + 7)
        } else {
            SymbolId(self.known)
        };
        self.known += 1;
        self.admitted.borrow_mut().push((symbol.to_string(), id));
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
}

impl OrderFeed for ScriptOrderFeed {
    fn learn(&mut self, symbol: &str, id: SymbolId) {
        self.learned.borrow_mut().push((symbol.to_string(), id));
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
                self.heard.borrow_mut().push(format!("{update:?}"));
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
            EngineEvent::Timer { id, .. } => self.fired.borrow_mut().push(*id),
            _ => {}
        }
    }
}

// ------------------------------------------------------------------ helpers

fn settings(shadow: bool) -> EngineSection {
    EngineSection {
        wal_path: "unused-in-mocks.wal".into(),
        // Named but unused: these tests hand the engine a mock venue
        // directly rather than going through assembly.
        venue: engine_venue::BYBIT_DEMO.to_string(),
        group_flush_ms: 250,
        account_view_max_age_ms: 60_000,
        shadow,
        // A test that wants a book, or a heartbeat, hands the engine one
        // itself.
        target_book_path: None,
        heartbeat_path: None,
    }
}

struct Harness {
    tape: Tape,
    records: Rc<RefCell<Vec<WalRecord>>>,
    sends: Rc<RefCell<Vec<OrderRequest>>>,
    cancels: Rc<RefCell<Vec<(SymbolId, String)>>>,
    amends: Rc<RefCell<Vec<(SymbolId, String, AmendSpec)>>>,
    risk_saw: Rc<RefCell<Vec<OrderUpdate>>>,
    leverages: Rc<RefCell<Vec<(SymbolId, f64)>>>,
}

/// The same as `build_with`, with a venue that refuses every set_leverage.
async fn build_with_refusing_leverage(
    settings: &EngineSection,
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_inner(settings, verdict, strategies, symbols, &[], Vec::new(), true).await
}

async fn build(
    shadow: bool,
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_with_venue_orders(shadow, verdict, strategies, symbols, replayed, Vec::new()).await
}

/// The same, with the venue already working some orders — which is how a boot
/// finds out somebody else is on the account.
async fn build_with_venue_orders(
    shadow: bool,
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    build_with(&settings(shadow), verdict, strategies, symbols, replayed, working).await
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
    build_inner(settings, verdict, strategies, symbols, replayed, working, false).await
}

async fn build_inner(
    settings: &EngineSection,
    verdict: RiskVerdict,
    strategies: Vec<Box<dyn Strategy>>,
    symbols: &[&str],
    replayed: &[WalRecord],
    working: Vec<VenueOrder>,
    leverage_refuses: bool,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), symbols);
    venue.working = working;
    venue.leverage_refuses = leverage_refuses;
    let cancels = venue.cancels.clone();
    let amends = venue.amends.clone();
    let leverages = venue.leverages.clone();
    let (risk, risk_saw) = MockRisk::with(verdict);
    let engine = Engine::boot(
        settings,
        "0000000000000000",
        wal,
        risk,
        venue,
        strategies,
        replayed,
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
            risk_saw,
            leverages,
        },
    )
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

mod order_path;
mod resting_orders;
mod target_books;
mod worked_entries;
mod reconciliation;
mod heartbeat;
mod fill_costs;
