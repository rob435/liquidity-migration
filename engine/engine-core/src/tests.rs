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
        }
    }
}

impl MarketFeed for ScriptFeed {
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
}

impl ScriptOrderFeed {
    fn empty() -> Self {
        ScriptOrderFeed {
            updates: VecDeque::new(),
        }
    }
}

impl OrderFeed for ScriptOrderFeed {
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

fn allow_all() -> RiskVerdict {
    RiskVerdict::Allow { qty: f64::NAN }
}

// ------------------------------------------------------------------- tests

#[tokio::test]
async fn the_log_is_written_in_order_and_the_barrier_comes_before_the_send() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut feed = ScriptFeed::quotes(symbol, 1, true);
    let mut orders = ScriptOrderFeed::empty();
    engine
        .run(&mut feed, &mut orders, std::future::pending::<()>())
        .await
        .unwrap();

    let kinds = appends(&h.tape);
    let want = [
        "boot",
        "note", // boot says which mode it is in
        "reconciled", // and what the venue said, against the log
        "intent",
        "verdict",
        "order_sent",
        "order_update",
        "latency_ledger",
    ];
    assert_eq!(kinds, want, "log records in order");

    let sent_at = at(&h.tape, &Step::Append("order_sent".into())).unwrap();
    // The first barrier on the tape is boot's; the one this test is about is
    // the order's, which comes after the order was written down.
    let barrier_at = after(&h.tape, &Step::Barrier, sent_at).unwrap();
    let send_at = at(
        &h.tape,
        &Step::Send(h.sends.borrow()[0].client_order_id.clone()),
    )
    .unwrap();
    assert!(sent_at < barrier_at, "the record is written before the fsync");
    assert!(
        barrier_at < send_at,
        "the order is on disk before it is on the wire ({barrier_at} vs {send_at})"
    );

    let id = &h.sends.borrow()[0].client_order_id;
    assert!(id.starts_with("eng-"), "id shape: {id}");
    assert!(id.len() <= 36, "id is short enough for the venue: {id}");
    assert_eq!(h.risk_saw.borrow().len(), 1, "risk hears the reply");
}

#[tokio::test]
async fn the_verdict_record_names_the_order_it_approved() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let records = h.records.borrow();
    let verdict = records
        .iter()
        .find_map(|r| match r {
            WalRecord::Verdict {
                client_order_id, ..
            } => Some(client_order_id.clone()),
            _ => None,
        })
        .unwrap();
    assert_eq!(verdict, Some(h.sends.borrow()[0].client_order_id.clone()));
}

#[tokio::test]
async fn shadow_mode_sends_nothing_and_says_so() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(true, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    assert!(engine.shadow());
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(h.sends.borrow().is_empty(), "no order left the box");
    assert!(
        !h.tape
            .borrow()
            .iter()
            .any(|s| matches!(s, Step::Send(_))),
        "the gateway was never called"
    );
    let notes: Vec<String> = h
        .records
        .borrow()
        .iter()
        .filter_map(|r| match r {
            WalRecord::Note { source, text } if source == "shadow" => Some(text.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(notes.len(), 1, "one note per skipped send");
    assert!(notes[0].contains("no send"), "{}", notes[0]);
    // No order fsync in shadow: nothing leaves the box, so durability buys
    // nothing — and a barrier BETWEEN the order record and its "no send"
    // note opened a crash window where replay reported a phantom in-flight
    // order on a run that never sent anything. The two records travel
    // together, and the only fsync is the final shutdown one.
    assert!(
        only_the_shutdown_barrier(&h.tape),
        "shadow mode paid for an order fsync"
    );
    assert_eq!(appends(&h.tape).iter().filter(|k| *k == "order_sent").count(), 1);
    assert!(engine.in_flight_ids().is_empty(), "nothing ever left the box");
    let replayed = crate::replay::describe(&h.records.borrow(), false);
    assert!(replayed.in_flight.is_empty(), "and the log reads back that way");
}

#[tokio::test]
async fn a_refusal_stops_before_the_order_is_written() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let deny = RiskVerdict::Deny {
        reason: DenyReason::MissingStop,
    };
    let (mut engine, h) = build(false, deny, vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 3, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let kinds = appends(&h.tape);
    assert!(!kinds.contains(&"order_sent".to_string()), "{kinds:?}");
    assert!(h.sends.borrow().is_empty(), "nothing reached the venue");
    assert!(
        only_the_shutdown_barrier(&h.tape),
        "no fsync for an order that does not exist (the shutdown one aside)"
    );
    assert_eq!(kinds.iter().filter(|k| *k == "intent").count(), 3);
    assert_eq!(kinds.iter().filter(|k| *k == "verdict").count(), 3);
    assert!(engine.in_flight_ids().is_empty());
}

#[tokio::test]
async fn a_size_below_the_venue_minimum_is_refused_with_a_note() {
    // 0.0004 rounds down to nothing at a step of 0.001.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.0004);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let kinds = appends(&h.tape);
    assert_eq!(
        kinds,
        ["boot", "note", "reconciled", "intent", "verdict", "note", "latency_ledger"]
    );
    assert!(h.sends.borrow().is_empty());
    let note = note_saying(&h.records, "not sent");
    assert!(note.contains("smallest tradable size"), "{note}");
}

#[tokio::test]
async fn the_newest_control_anchor_in_the_log_is_restored_at_boot() {
    // A restart must never hand the day a fresh loss budget or clear a
    // latched trip — the newest anchor in the log is the guard's memory.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let replayed = vec![
        WalRecord::ControlAnchor { source: "risk".into(), state: "{\"old\":true}".into() },
        WalRecord::Note { source: "engine".into(), text: "unrelated".into() },
        WalRecord::ControlAnchor { source: "risk".into(), state: "{\"newest\":true}".into() },
    ];
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let restored = risk.restored.clone();
    let engine = Engine::boot(
        &settings(true),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &replayed,
    )
    .await
    .expect("boot");
    drop(engine);
    assert_eq!(*restored.borrow(), vec!["{\"newest\":true}".to_string()]);
}

#[tokio::test]
async fn a_recovered_in_flight_order_is_registered_with_the_kernel() {
    // After a restart the partition would otherwise believe every share is
    // free while last boot's orders are still working at the venue.
    let replayed = vec![WalRecord::OrderSent {
        request: OrderRequest {
            client_order_id: "eng-1700000000000-4".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.25,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
        },
        wire_ns: 3,
    }];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let registered = risk.registered.clone();
    let engine = Engine::boot(
        &settings(true),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &replayed,
    )
    .await
    .expect("boot");
    drop(engine);
    assert_eq!(
        *registered.borrow(),
        vec![("eng-1700000000000-4".to_string(), 0.25)]
    );
}

#[test]
fn minting_skips_an_id_the_log_already_knows() {
    // boot_ms is a wall clock; a clock stepped back can repeat a previous
    // boot's prefix, and overwriting a recovered order's ledger entry makes
    // a real working order invisible.
    let taken = ["eng-99-1".to_string(), "eng-99-2".to_string()];
    let mut n = 0;
    let id = crate::engine::mint_unused("eng-99-", &mut n, |candidate| {
        taken.contains(&candidate.to_string())
    });
    assert_eq!(id, "eng-99-3");
    assert_eq!(n, 3);
}

#[tokio::test]
async fn a_changed_control_anchor_is_written_and_made_durable() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(true, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    engine.risk.anchor_script.push_back("{\"day\":1}".to_string());
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let records = h.records.borrow();
    let anchor_at = records.iter().position(
        |r| matches!(r, WalRecord::ControlAnchor { state, .. } if state == "{\"day\":1}"),
    );
    assert!(anchor_at.is_some(), "the changed anchor never reached the log");
    let tape = h.tape.borrow();
    let write = tape
        .iter()
        .position(|s| matches!(s, Step::Append(k) if k == "control_anchor"))
        .expect("append step");
    assert!(
        tape[write..].contains(&Step::Barrier),
        "the anchor was written but never made durable"
    );
}

#[tokio::test]
async fn a_clean_shutdown_forces_its_tail_to_disk() {
    // Power loss right after a graceful stop must not lose the closing
    // updates and ledger line: completed orders reading back as in flight
    // is the conservative direction, but it is still a lie in the audit.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let tape = h.tape.borrow();
    let last_append = tape
        .iter()
        .rposition(|s| matches!(s, Step::Append(_)))
        .expect("something was appended");
    let last_barrier = tape.iter().rposition(|s| matches!(s, Step::Barrier));
    assert!(
        last_barrier.is_some_and(|b| b > last_append),
        "the log's tail was never forced to disk on the way out"
    );
}

/// Emits a burst of entries with exits at the back, all in one wake.
struct BurstEmitter {
    symbol: String,
    entries: usize,
    exits: usize,
    fired: bool,
}

impl Strategy for BurstEmitter {
    fn name(&self) -> &str {
        "burst"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event {
            if self.fired {
                return;
            }
            self.fired = true;
            for i in 0..(self.entries + self.exits) {
                let exit = i >= self.entries;
                ctx.place(Intent {
                    strategy: StrategyId(0),
                    symbol: *symbol,
                    side: if exit { Side::Sell } else { Side::Buy },
                    qty: 0.01,
                    kind: OrderKind::Market,
                    stop: if exit {
                        None
                    } else {
                        Some(StopSpec { trigger_px: quote.bid_px * 0.99 })
                    },
                    reduce_only: exit,
                    tag: if exit { "burst-exit".into() } else { "burst-entry".into() },
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
        }
    }
}

#[tokio::test]
async fn a_flooded_wake_drops_entries_but_never_exits() {
    // The cap exists so a runaway strategy cannot wedge the loop — but a
    // de-risking order queued behind the flood must still get out, or the
    // strategy is stranded holding a position it believes it exited.
    let burst = BurstEmitter { symbol: "BTCUSDT".into(), entries: 68, exits: 2, fired: false };
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let sends = h.sends.borrow();
    let exits = sends.iter().filter(|s| s.reduce_only).count();
    let entries = sends.iter().filter(|s| !s.reduce_only).count();
    assert_eq!(exits, 2, "both exits reach the venue");
    assert!(entries <= 64, "the flood of entries is capped: {entries}");
    let note = note_saying(&h.records, "dropped");
    assert!(note.contains("entries"), "the drop is named: {note}");
}

/// Emits one reduce-only exit that (wrongly) still carries a stop.
struct SloppyExiter {
    symbol: String,
    sent: bool,
}

impl Strategy for SloppyExiter {
    fn name(&self) -> &str {
        "sloppy-exiter"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event {
            if !self.sent {
                self.sent = true;
                ctx.place(Intent {
                    strategy: StrategyId(0),
                    symbol: *symbol,
                    side: Side::Sell,
                    qty: 0.01,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec {
                        trigger_px: quote.bid_px * 0.99,
                    }),
                    reduce_only: true,
                    tag: "sloppy-exit".into(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
        }
    }
}

#[tokio::test]
async fn an_exit_sheds_its_stop_before_the_log_and_the_wire() {
    // The venue rejects a reduce-only order carrying stop fields, and the
    // log must record what was actually sent — so the stop comes off at
    // request build, not just at the venue boundary.
    let exiter = SloppyExiter { symbol: "BTCUSDT".into(), sent: false };
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(exiter)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let sends = h.sends.borrow();
    assert_eq!(sends.len(), 1);
    assert!(sends[0].reduce_only);
    assert!(sends[0].stop.is_none(), "the wire saw a stop on an exit");
    for record in h.records.borrow().iter() {
        if let WalRecord::OrderSent { request, .. } = record {
            assert!(request.stop.is_none(), "the log saw a stop on an exit");
        }
    }
}

#[tokio::test]
async fn an_intent_with_an_unreal_number_never_reaches_the_log() {
    // serde_json writes a non-finite float as null, which cannot be read
    // back into f64 — so a NaN quantity appended to the log would make that
    // frame unreadable and stop the next boot's replay. The refusal must
    // come before any log write.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, f64::NAN);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let kinds = appends(&h.tape);
    assert!(
        !kinds.contains(&"intent".to_string()),
        "a NaN intent was written to the log: {kinds:?}"
    );
    assert!(h.sends.borrow().is_empty(), "nothing reached the venue");
    let note = note_saying(&h.records, "not a finite");
    assert!(note.contains("buy"), "the note names the intent's tag: {note}");
    for record in h.records.borrow().iter() {
        let bytes = serde_json::to_vec(record).expect("serializable");
        let _: WalRecord = serde_json::from_slice(&bytes)
            .expect("every log record must survive a read-back");
    }
}

#[tokio::test]
async fn an_order_under_the_minimum_value_is_refused() {
    // 0.001 of a 30k coin is 30 dollars; ask for a minimum of 5 and it passes,
    // so raise the size floor by asking for a tiny quantity of a cheap symbol.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.001);
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.rules[0].1.min_notional = 1_000_000.0;
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(sends.borrow().is_empty());
    let note = note_saying(&records, "not sent");
    assert!(note.contains("smallest order value"), "{note}");
}

#[tokio::test]
async fn a_send_with_no_answer_leaves_the_order_in_flight() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.reply = Some(VenueError::Transport("connection reset".into()));
    let (risk, risk_saw) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(sends.borrow().len(), 1, "it did go out");
    assert_eq!(engine.in_flight_ids().len(), 1, "and we do not know its fate");
    assert!(risk_saw.borrow().is_empty(), "no reply, so nothing to tell risk");
    assert!(note_saying(&records, "no answer").contains(&sends.borrow()[0].client_order_id));
}

#[tokio::test]
async fn a_rejected_order_is_over() {
    let (buyer, heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (mut venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.reply = Some(VenueError::Rejected {
        code: 110007,
        message: "not enough balance".into(),
    });
    let (risk, risk_saw) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(engine.in_flight_ids().is_empty());
    assert_eq!(risk_saw.borrow().len(), 1);
    assert_eq!(heard.borrow().len(), 1, "the strategy hears its rejection");
    assert!(heard.borrow()[0].contains("110007"), "{:?}", heard.borrow());
}

#[tokio::test]
async fn an_order_left_in_flight_by_the_last_run_comes_back_and_is_not_resent() {
    let stale = OrderRequest {
        client_order_id: "eng-1700000000000-4".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
    };
    let finished = OrderRequest {
        client_order_id: "eng-1700000000000-3".into(),
        ..stale.clone()
    };
    let replayed = vec![
        WalRecord::Boot {
            version: "old".into(),
            config_sha256: "abc".into(),
            wall_ts_ms: 1,
        },
        WalRecord::OrderSent {
            request: finished.clone(),
            wire_ns: 1,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                client_order_id: finished.client_order_id.clone(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 0.01,
                px: 30_000.0,
                fee: 0.1,
                venue_ts_ms: 2,
                recv_ns: 2,
            },
        },
        WalRecord::OrderSent {
            request: stale.clone(),
            wire_ns: 3,
        },
    ];

    let (buyer, heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &replayed,
    )
    .await;
    assert_eq!(
        engine.in_flight_ids(),
        vec!["eng-1700000000000-4"],
        "the unanswered one, and only it"
    );

    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut orders = ScriptOrderFeed {
        updates: VecDeque::from(vec![OrderUpdate::Fill {
            client_order_id: stale.client_order_id.clone(),
            symbol,
            side: Side::Buy,
            qty: 0.01,
            px: 30_000.0,
            fee: 0.1,
            venue_ts_ms: 4,
            recv_ns: 4,
        }]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    assert!(h.sends.borrow().is_empty(), "boot never re-sends anything");
    assert!(
        engine.in_flight_ids().is_empty(),
        "the late fill closes the recovered order"
    );
    assert_eq!(
        heard.borrow().len(),
        1,
        "and the strategy that placed it hears the news"
    );
}

#[tokio::test]
async fn timers_fire_for_the_strategy_that_armed_them() {
    let (one, fired_one) = Ticker::new("BTCUSDT", 11, 3_000_000);
    let (two, fired_two) = Ticker::new("ETHUSDT", 22, 5_000_000);
    let (mut engine, _h) = build(
        false,
        allow_all(),
        vec![Box::new(one), Box::new(two)],
        &["BTCUSDT", "ETHUSDT"],
        &[],
    )
    .await;
    let btc = engine.market().table.get("BTCUSDT").unwrap();
    let eth = engine.market().table.get("ETHUSDT").unwrap();
    let mut feed = ScriptFeed {
        events: VecDeque::from(vec![
            MarketEvent::Quote {
                symbol: btc,
                quote: Quote {
                    bid_px: 30_000.0,
                    ask_px: 30_000.5,
                    recv_ns: clock::now_ns(),
                    ..Quote::default()
                },
            },
            MarketEvent::Quote {
                symbol: eth,
                quote: Quote {
                    bid_px: 2_000.0,
                    ask_px: 2_000.5,
                    recv_ns: clock::now_ns(),
                    ..Quote::default()
                },
            },
        ]),
        close_at_end: false,
    };
    engine
        .run(
            &mut feed,
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(80)),
        )
        .await
        .unwrap();

    assert_eq!(*fired_one.borrow(), vec![TimerId(11)], "each hears its own");
    assert_eq!(*fired_two.borrow(), vec![TimerId(22)]);
}

#[tokio::test]
async fn a_market_message_only_reaches_the_strategies_that_asked_for_it() {
    let (btc_buyer, btc_heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (eth_buyer, eth_heard) = Buyer::new("ETHUSDT", 1, 0.01);
    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(btc_buyer), Box::new(eth_buyer)],
        &["BTCUSDT", "ETHUSDT"],
        &[],
    )
    .await;
    let btc = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(btc, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.sends.borrow().len(), 1, "one quote, one order");
    assert_eq!(h.sends.borrow()[0].strategy, StrategyId(0));
    assert_eq!(btc_heard.borrow().len(), 1, "its owner hears the reply");
    assert!(eth_heard.borrow().is_empty(), "the other strategy hears nothing");
}

#[tokio::test]
async fn the_group_flush_tick_pushes_the_log_out() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut small_flush = settings(false);
    small_flush.group_flush_ms = 5;
    let mut engine = Engine::boot(&small_flush, "0", wal, risk, venue, vec![Box::new(buyer)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    let flushes = tape.borrow().iter().filter(|s| **s == Step::Flush).count();
    assert!(flushes >= 3, "the tick keeps flushing, saw {flushes}");
}

#[tokio::test]
async fn the_account_reading_is_refreshed_before_it_goes_stale() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut quick = settings(false);
    quick.group_flush_ms = 5;
    quick.account_view_max_age_ms = 20; // refreshed at half of this
    let mut engine = Engine::boot(&quick, "0", wal, risk, venue, vec![Box::new(buyer)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    let reads = tape
        .borrow()
        .iter()
        .filter(|s| **s == Step::ReadAccount)
        .count();
    assert!(reads >= 3, "one at boot and more as it ages, saw {reads}");
}

#[tokio::test]
async fn boot_reads_the_rules_and_the_account_before_anything_else() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (_engine, h) = build(true, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let steps = h.tape.borrow().clone();
    assert_eq!(steps[0], Step::Append("boot".into()));
    assert_eq!(steps[1], Step::Append("note".into()), "which mode it is in");
    assert_eq!(steps[2], Step::ReadRules);
    assert_eq!(steps[3], Step::ReadAccount);
}

#[tokio::test]
async fn the_bench_runs_the_real_loop_and_fills_the_histograms() {
    let path = temp_path("bench-smoke");
    let options = BenchOptions {
        events: 300,
        rate: 0,
        every_nth: 10,
        symbols: vec!["BTCUSDT".to_string()],
        wal_path: path.path().to_path_buf(),
    };
    let result = bench::run(&options).await.expect("bench");
    assert_eq!(result.events, 300);
    assert_eq!(result.orders, 30, "one order every tenth quote");
    for (segment, q) in &result.segments {
        assert!(q.count > 0, "{segment:?} recorded nothing");
        assert!(q.p50_ns > 0, "{segment:?} p50 is zero");
        assert!(q.max_ns >= q.p50_ns);
    }
    // The log is real and reads back.
    let report = crate::replay::read(path.path()).unwrap();
    assert!(report.records > 30, "records: {}", report.records);
    assert!(!report.torn_tail);
    assert!(result.table().contains("write it down"));
    assert!(result.as_json().contains("\"orders\":30"));
}

// ------------------------------------------- pulling and moving what rests

/// Pulls one named order on its first quote, and nothing after.
struct Canceller {
    symbol: String,
    order: String,
    fired: bool,
}

impl Canceller {
    fn new(symbol: &str, order: &str) -> Self {
        Canceller {
            symbol: symbol.into(),
            order: order.into(),
            fired: false,
        }
    }
}

impl Strategy for Canceller {
    fn name(&self) -> &str {
        "canceller"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            if !self.fired {
                self.fired = true;
                ctx.cancel(*symbol, &self.order);
            }
        }
    }
}

/// Moves one named order's price on its first quote.
struct Amender {
    symbol: String,
    order: String,
    fired: bool,
}

impl Strategy for Amender {
    fn name(&self) -> &str {
        "amender"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event {
            if !self.fired {
                self.fired = true;
                ctx.amend(
                    *symbol,
                    &self.order,
                    AmendSpec {
                        px: Some(quote.bid_px),
                        qty: None,
                    },
                );
            }
        }
    }
}

#[tokio::test]
async fn a_cancel_is_written_down_and_reaches_the_venue_without_an_fsync() {
    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(Canceller::new("BTCUSDT", "eng-old-1"))],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(
        *h.cancels.borrow(),
        vec![(symbol, "eng-old-1".to_string())],
        "the cancel reached the gateway"
    );
    let record = h
        .records
        .borrow()
        .iter()
        .find_map(|r| match r {
            WalRecord::CancelSent {
                symbol,
                client_order_id,
                wire_ns,
            } => Some((*symbol, client_order_id.clone(), *wire_ns)),
            _ => None,
        })
        .expect("a cancel_sent record");
    assert_eq!(record.0, symbol);
    assert_eq!(record.1, "eng-old-1");
    assert!(record.2 > 0, "the record stamps when it went out");

    let written = at(&h.tape, &Step::Append("cancel_sent".into())).unwrap();
    let sent = at(&h.tape, &Step::Cancel("eng-old-1".into())).unwrap();
    assert!(written < sent, "written down before it went out");
    // A cancel adds no exposure, so it does not pay for an fsync: the only
    // barrier on the tape is the shutdown one.
    assert!(
        only_the_shutdown_barrier(&h.tape),
        "a cancel paid for a barrier"
    );
}

#[tokio::test]
async fn shadow_mode_writes_the_cancel_down_and_sends_nothing() {
    let (mut engine, h) = build(
        true,
        allow_all(),
        vec![Box::new(Canceller::new("BTCUSDT", "eng-old-1"))],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(h.cancels.borrow().is_empty(), "the gateway was never called");
    assert!(
        !h.tape.borrow().iter().any(|s| matches!(s, Step::Cancel(_))),
        "nothing left the box"
    );
    assert!(
        appends(&h.tape).contains(&"cancel_sent".to_string()),
        "the record is written exactly as it would be live"
    );
    let note = note_saying(&h.records, "no send");
    assert!(note.contains("cancel of eng-old-1"), "{note}");
    // The never-sent marker ends the order it names, so a shadow cancel must
    // not name the order it would have pulled.
    assert!(
        !note.starts_with("no send: eng-old-1"),
        "the note reads back as an ending for the order itself: {note}"
    );
}

/// Fires a burst of entries with cancels behind them, all in one wake.
struct MixedBurst {
    symbol: String,
    entries: usize,
    cancels: usize,
    fired: bool,
}

impl Strategy for MixedBurst {
    fn name(&self) -> &str {
        "mixed-burst"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event {
            if self.fired {
                return;
            }
            self.fired = true;
            for _ in 0..self.entries {
                ctx.place(Intent {
                    strategy: StrategyId(0),
                    symbol: *symbol,
                    side: Side::Buy,
                    qty: 0.01,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec {
                        trigger_px: quote.bid_px * 0.99,
                    }),
                    reduce_only: false,
                    tag: "flood-entry".into(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
            for i in 0..self.cancels {
                ctx.cancel(*symbol, &format!("eng-old-{i}"));
            }
        }
    }
}

#[tokio::test]
async fn a_flooded_wake_drops_entries_but_never_cancels() {
    // Same reason exits are spared: a strategy that cannot pull its resting
    // orders is stranded with a book it believes it has already cleared.
    let burst = MixedBurst {
        symbol: "BTCUSDT".into(),
        entries: 70,
        cancels: 3,
        fired: false,
    };
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(
        h.cancels.borrow().len(),
        3,
        "every cancel reached the venue, queued behind the flood"
    );
    assert_eq!(
        h.sends.borrow().len(),
        64,
        "the flood of entries stopped at the cap"
    );
    let note = note_saying(&h.records, "dropped");
    assert!(note.contains("entries"), "the drop is named: {note}");
}

#[tokio::test]
async fn an_amend_is_refused_where_the_venue_cannot_move_a_resting_order() {
    // Cancel-and-replace is a different trade — a new order at the back of
    // the queue — so the engine says no rather than substituting one.
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.caps.amend_in_place = false;
    let amends = venue.amends.clone();
    let cancels = venue.cancels.clone();
    let (risk, _seen) = MockRisk::with(allow_all());
    let amender = Amender {
        symbol: "BTCUSDT".into(),
        order: "eng-old-1".into(),
        fired: false,
    };
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(amender)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(amends.borrow().is_empty(), "nothing reached the venue");
    assert!(
        cancels.borrow().is_empty(),
        "and it was not quietly turned into a cancel"
    );
    assert!(
        !appends(&tape).contains(&"amend_sent".to_string()),
        "nothing was written down as sent"
    );
    let note = note_saying(&records, "not amended");
    assert!(note.contains("eng-old-1"), "{note}");
    assert!(note.contains("cancel-and-replace"), "{note}");
}

#[tokio::test]
async fn an_amend_reaches_the_venue_where_it_can_be_honoured() {
    let amender = Amender {
        symbol: "BTCUSDT".into(),
        order: "eng-old-1".into(),
        fired: false,
    };
    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(amender)],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let amends = h.amends.borrow();
    assert_eq!(amends.len(), 1);
    assert_eq!(amends[0].0, symbol);
    assert_eq!(amends[0].1, "eng-old-1");
    assert_eq!(amends[0].2.px, Some(30_000.0), "the new price, unchanged");
    assert_eq!(amends[0].2.qty, None, "and no size change was asked for");
    let written = at(&h.tape, &Step::Append("amend_sent".into())).unwrap();
    let sent = at(&h.tape, &Step::Amend("eng-old-1".into())).unwrap();
    assert!(written < sent, "written down before it went out");
    // Repricing cannot create exposure, so it rides the group flush.
    assert!(
        only_the_shutdown_barrier(&h.tape),
        "a reprice paid for a barrier"
    );
}

/// Raises one named order's size on its first quote.
struct Grower {
    symbol: String,
    order: String,
    qty: f64,
    fired: bool,
}

impl Strategy for Grower {
    fn name(&self) -> &str {
        "grower"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            if !self.fired {
                self.fired = true;
                ctx.amend(
                    *symbol,
                    &self.order,
                    AmendSpec {
                        px: None,
                        qty: Some(self.qty),
                    },
                );
            }
        }
    }
}

#[tokio::test]
async fn an_amend_that_raises_the_size_is_durable_before_it_goes_out() {
    // Growing a resting order adds exposure, so it earns the same fsync a
    // send does: a crash in between must never leave the log describing a
    // smaller order than the one the venue is now working.
    let grower = Grower {
        symbol: "BTCUSDT".into(),
        order: "eng-old-1".into(),
        qty: 99.0,
        fired: false,
    };
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(grower)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let start = after_boot(&h.tape);
    let written = at(&h.tape, &Step::Append("amend_sent".into())).unwrap();
    let sent = at(&h.tape, &Step::Amend("eng-old-1".into())).unwrap();
    let barrier = h
        .tape
        .borrow()
        .iter()
        .enumerate()
        .find(|(i, s)| *i >= start && matches!(s, Step::Barrier))
        .map(|(i, _)| i)
        .expect("a size-raising amend must be made durable");
    assert!(written < barrier, "the record is written before the fsync");
    assert!(barrier < sent, "it is on disk before it is on the wire");
}

#[tokio::test]
async fn an_entry_carrying_a_stop_is_refused_where_the_venue_keeps_none() {
    // The kernel demands a stop on an entry. A venue that cannot hold one
    // would leave that rule unenforced while the log still showed a stop, so
    // the order does not go at all.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.caps.native_position_stop = false;
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(sends.borrow().is_empty(), "nothing reached the venue");
    assert!(
        !appends(&tape).contains(&"order_sent".to_string()),
        "and nothing was written down as sent"
    );
    let note = note_saying(&records, "not sent");
    assert!(note.contains("keeps none"), "{note}");
    assert!(engine.in_flight_ids().is_empty());
}

/// Writes down its own working orders every time the engine wakes it, and
/// optionally places one entry first so the book has something it owns.
struct Watcher {
    symbol: String,
    place: Option<f64>,
    placed: bool,
    seen: Rc<RefCell<Vec<Vec<String>>>>,
}

impl Watcher {
    fn new(symbol: &str) -> (Self, Rc<RefCell<Vec<Vec<String>>>>) {
        let seen = Rc::new(RefCell::new(Vec::new()));
        (
            Watcher {
                symbol: symbol.to_string(),
                place: None,
                placed: false,
                seen: seen.clone(),
            },
            seen,
        )
    }

    fn buying(symbol: &str, qty: f64) -> (Self, Rc<RefCell<Vec<Vec<String>>>>) {
        let (mut me, seen) = Watcher::new(symbol);
        me.place = Some(qty);
        (me, seen)
    }
}

impl Strategy for Watcher {
    fn name(&self) -> &str {
        "watcher"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let mut mine = Vec::new();
        ctx.resting(&mut mine);
        self.seen
            .borrow_mut()
            .push(mine.iter().map(|o| o.client_order_id.to_string()).collect());

        if let (EngineEvent::Market(MarketEvent::Quote { symbol, quote }), Some(qty), false) =
            (event, self.place, self.placed)
        {
            self.placed = true;
            ctx.place(Intent {
                strategy: StrategyId(0),
                symbol: *symbol,
                side: Side::Buy,
                qty,
                kind: OrderKind::Market,
                stop: Some(StopSpec {
                    trigger_px: quote.bid_px * 0.99,
                }),
                reduce_only: false,
                tag: "watch".into(),
                decided_ns: ctx.now_ns(),
                work: None,
                leverage: None,
            });
        }
    }
}

#[tokio::test]
async fn each_strategy_reads_only_its_own_working_orders() {
    // Two strategies, one book. A strategy that could see another's orders
    // could cancel them.
    let mine = OrderRequest {
        client_order_id: "eng-1700000000000-1".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.25,
        kind: OrderKind::Limit {
            px: 29_000.0,
            tif: engine_types::TimeInForce::Gtc,
        },
        stop: None,
        reduce_only: false,
    };
    let theirs = OrderRequest {
        client_order_id: "eng-1700000000000-2".into(),
        strategy: StrategyId(1),
        ..mine.clone()
    };
    let finished = OrderRequest {
        client_order_id: "eng-1700000000000-3".into(),
        ..mine.clone()
    };
    let replayed = vec![
        WalRecord::OrderSent {
            request: mine.clone(),
            wire_ns: 1,
        },
        WalRecord::OrderSent {
            request: theirs.clone(),
            wire_ns: 2,
        },
        WalRecord::OrderSent {
            request: finished.clone(),
            wire_ns: 3,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                client_order_id: finished.client_order_id.clone(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 0.25,
                px: 29_000.0,
                fee: 0.0,
                venue_ts_ms: 4,
                recv_ns: 4,
            },
        },
    ];

    let (one, seen_one) = Watcher::new("BTCUSDT");
    let (two, seen_two) = Watcher::new("BTCUSDT");
    let (mut engine, _h) = build(
        false,
        allow_all(),
        vec![Box::new(one), Box::new(two)],
        &["BTCUSDT"],
        &replayed,
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(
        *seen_one.borrow(),
        vec![vec!["eng-1700000000000-1".to_string()]],
        "its own working order, and nothing else the log knows about"
    );
    assert_eq!(
        *seen_two.borrow(),
        vec![vec!["eng-1700000000000-2".to_string()]],
        "the other strategy's book is its own"
    );
}

#[tokio::test]
async fn an_order_this_strategy_placed_is_in_its_book_until_it_ends() {
    let (watcher, seen) = Watcher::buying("BTCUSDT", 0.01);
    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(watcher)],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let id = h.sends.borrow()[0].client_order_id.clone();
    let seen = seen.borrow();
    assert_eq!(seen[0], Vec::<String>::new(), "nothing placed yet");
    // An ack is not an ending, so the order is still the strategy's to pull.
    assert_eq!(seen.last().unwrap(), &vec![id], "acked and still working");
}

#[tokio::test]
async fn an_order_the_log_has_ended_leaves_the_strategys_book() {
    // Ownership outlives the order — the engine still routes its news here —
    // so only the ending keeps a dead order out of the strategy's book. A
    // strategy that saw it would try to cancel or move something gone.
    let (watcher, seen) = Watcher::buying("BTCUSDT", 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.reply = Some(VenueError::Rejected {
        code: 110007,
        message: "not enough balance".into(),
    });
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(watcher)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(sends.borrow().len(), 1, "one order really was placed");
    assert!(engine.in_flight_ids().is_empty(), "and the venue refused it");
    for (n, snapshot) in seen.borrow().iter().enumerate() {
        assert!(
            snapshot.is_empty(),
            "a rejected order was still in the book at wake {n}: {snapshot:?}"
        );
    }
    assert!(seen.borrow().len() >= 3, "it was woken after the rejection");
}

// ------------------------------------------------- the target book seam

/// Writes down every book the loop hands it, and places nothing.
struct BookListener {
    symbol: String,
    heard: Rc<RefCell<Vec<String>>>,
}

impl BookListener {
    fn new(symbol: &str) -> (Self, Rc<RefCell<Vec<String>>>) {
        let heard = Rc::new(RefCell::new(Vec::new()));
        (
            BookListener {
                symbol: symbol.to_string(),
                heard: heard.clone(),
            },
            heard,
        )
    }
}

impl Strategy for BookListener {
    fn name(&self) -> &str {
        "book_listener"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, _ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Targets(book) = event {
            self.heard
                .borrow_mut()
                .push(format!("{} x{}", book.source, book.targets.len()));
        }
    }
}

const BOOK_JSON: &str = r#"{"version":1,"source":"carry","decision_ts_ms":1700000000000,
"valid_until_ms":1700086400000,"targets":[{"symbol":"BTCUSDT","notional_usdt":50.0,
"stop_loss_fraction":0.35,"leverage":2.0}]}"#;

/// Stop the loop as soon as the strategy has heard something, or give up
/// after a while so a failure reads as an assertion and not a hung test.
async fn until_heard(heard: Rc<RefCell<Vec<String>>>) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while heard.borrow().is_empty() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
}

/// A buyer whose entries were sized at a leverage, so the engine has to make
/// the venue agree before the order goes.
fn levered_buyer(symbol: &str, leverage: f64) -> (Buyer, Rc<RefCell<Vec<String>>>) {
    let (mut buyer, heard) = Buyer::new(symbol, 1, 0.01);
    buyer.leverage = Some(leverage);
    (buyer, heard)
}

#[tokio::test]
async fn an_entry_states_its_leverage_before_the_order_goes() {
    // Margin posted is notional divided by leverage. An order sized at one
    // and filled at another does not commit the capital the kernel priced.
    let (buyer, _heard) = levered_buyer("BTCUSDT", 2.0);
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.leverages.borrow().as_slice(), [(SymbolId(0), 2.0)]);
    assert_eq!(h.sends.borrow().len(), 1, "the order still went");
    // Order matters: the venue heard about leverage before it heard the order.
    let tape = h.tape.borrow();
    assert!(
        tape.iter().any(|step| matches!(step, Step::Send(_))),
        "no send on the tape: {tape:?}"
    );
}

#[tokio::test]
async fn a_second_entry_on_the_same_symbol_does_not_pay_for_leverage_twice() {
    // A symbol keeps its leverage at the venue. Re-stating it before every
    // order would buy a round trip per order, measured at ~190 ms on the
    // Python fleet, for no change at all.
    let (buyer, _heard) = levered_buyer("BTCUSDT", 2.0);
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 3, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.sends.borrow().len(), 3, "three entries went");
    assert_eq!(
        h.leverages.borrow().len(),
        1,
        "leverage was stated more than once for one symbol: {:?}",
        h.leverages.borrow()
    );
}

#[tokio::test]
async fn an_exit_does_not_wait_on_leverage() {
    // An exit at the wrong leverage is still an exit. Making it wait on a
    // venue round trip would be the wrong trade to make.
    let (mut seller, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    seller.leverage = Some(2.0);
    seller.reduce_only = true;
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(seller)], &["BTCUSDT"], &[]).await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(
        h.leverages.borrow().is_empty(),
        "an exit stopped to set leverage: {:?}",
        h.leverages.borrow()
    );
}

#[tokio::test]
async fn an_order_whose_leverage_will_not_set_is_not_sent() {
    // Fails closed. Sending anyway would post margin nobody priced, and the
    // whole reason for stating leverage is that we do not know what the
    // symbol currently carries.
    let (buyer, _heard) = levered_buyer("BTCUSDT", 2.0);
    let mut settings = settings(false);
    settings.shadow = false;
    let (mut engine, h) = build_with_refusing_leverage(
        &settings,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
    )
    .await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.leverages.borrow().len(), 1, "it tried");
    assert!(
        h.sends.borrow().is_empty(),
        "an order went at a leverage the venue would not accept"
    );
}

#[test]
fn a_symbol_that_goes_flat_forgets_its_leverage() {
    // The cache is only safe because of this. The owner trades the funded
    // account by hand, so a symbol that has been closed and reopened may have
    // been set to anything in between -- and a remembered value would make
    // the next entry skip the call that would have corrected it.
    use engine_types::risk::PositionView;
    let mut at = std::collections::HashMap::new();
    at.insert(SymbolId(0), 2.0);
    at.insert(SymbolId(1), 3.0);

    let still_open = vec![PositionView {
        symbol: SymbolId(1),
        side: Side::Buy,
        qty: 1.0,
        entry_px: 100.0,
        stop_attached: true,
    }];
    crate::engine::forget_leverage_where_flat(&mut at, &still_open);

    assert_eq!(at.get(&SymbolId(1)), Some(&3.0), "an open symbol keeps it");
    assert!(at.get(&SymbolId(0)).is_none(), "a flat symbol forgets it");
}

#[tokio::test]
async fn a_book_on_disk_reaches_the_strategies_through_the_loop() {
    let path = temp_path("book-seam");
    std::fs::write(&path, BOOK_JSON).expect("writes the book");
    let (listener, heard) = BookListener::new("BTCUSDT");
    let (mut engine, _h) =
        build(true, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[]).await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        crate::targets::TargetBookWatcher::with_poll(
            path.path().to_path_buf(),
            Duration::from_millis(5),
        ),
    )]));

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            until_heard(heard.clone()),
        )
        .await
        .unwrap();

    assert_eq!(
        heard.borrow().as_slice(),
        ["carry x1"],
        "the book reached the strategy whole"
    );
}

/// A second sleeve's book, on a different symbol and from a different
/// producer, so a crossover shows up as the wrong name in the wrong place.
const LONG_BOOK_JSON: &str = r#"{"version":1,"source":"long","decision_ts_ms":1700000000000,
"valid_until_ms":1700086400000,"targets":[{"symbol":"ETHUSDT","notional_usdt":40.0,
"stop_loss_fraction":0.35,"leverage":2.0},{"symbol":"BTCUSDT","notional_usdt":10.0,
"stop_loss_fraction":0.35,"leverage":2.0}]}"#;

fn book_watcher(path: &crate::testpath::TempPath) -> crate::targets::TargetBookWatcher {
    crate::targets::TargetBookWatcher::with_poll(
        path.path().to_path_buf(),
        Duration::from_millis(5),
    )
}

/// Wait until both sleeves have heard something, or give up.
async fn until_both_heard(a: Rc<RefCell<Vec<String>>>, b: Rc<RefCell<Vec<String>>>) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while (a.borrow().is_empty() || b.borrow().is_empty())
        && tokio::time::Instant::now() < deadline
    {
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
    // A crossover would arrive on the same poll as the right book, so give the
    // loop a moment to deliver a second one before deciding nothing did.
    tokio::time::sleep(Duration::from_millis(40)).await;
}

#[tokio::test]
async fn each_sleeve_hears_only_its_own_book() {
    // The whole reason books are routed rather than broadcast. Two sleeves
    // share one account and one position per symbol, so a follower handed the
    // other's book would start holding the other's positions -- and both books
    // below name BTCUSDT, which is exactly where that would show.
    let carry_path = temp_path("book-two-carry");
    let long_path = temp_path("book-two-long");
    std::fs::write(&carry_path, BOOK_JSON).expect("writes the carry book");
    std::fs::write(&long_path, LONG_BOOK_JSON).expect("writes the long book");

    let (carry, carry_heard) = BookListener::new("BTCUSDT");
    let (long, long_heard) = BookListener::new("ETHUSDT");
    let (mut engine, _h) = build(
        true,
        allow_all(),
        vec![Box::new(carry), Box::new(long)],
        &["BTCUSDT", "ETHUSDT"],
        &[],
    )
    .await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![
        (StrategyId(0), book_watcher(&carry_path)),
        (StrategyId(1), book_watcher(&long_path)),
    ]));

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            until_both_heard(carry_heard.clone(), long_heard.clone()),
        )
        .await
        .unwrap();

    assert_eq!(
        carry_heard.borrow().as_slice(),
        ["carry x1"],
        "the carry sleeve heard something other than its own book"
    );
    assert_eq!(
        long_heard.borrow().as_slice(),
        ["long x2"],
        "the long sleeve heard something other than its own book"
    );
}

#[tokio::test]
async fn an_unreadable_book_wakes_nobody() {
    // No decision, not an empty one. A strategy that were woken here with
    // nothing in hand would have to guess, and the guess that empties an
    // account is the one it would make.
    let path = temp_path("book-seam-torn");
    std::fs::write(&path, "{ not json at all").expect("writes the file");
    let (listener, heard) = BookListener::new("BTCUSDT");
    let (mut engine, _h) =
        build(true, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[]).await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        crate::targets::TargetBookWatcher::with_poll(
            path.path().to_path_buf(),
            Duration::from_millis(5),
        ),
    )]));

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            async {
                tokio::time::sleep(Duration::from_millis(200)).await;
            },
        )
        .await
        .unwrap();

    assert!(heard.borrow().is_empty(), "nothing was delivered");
}

#[tokio::test]
async fn with_no_watcher_the_loop_runs_as_it_always_did() {
    let (listener, heard) = BookListener::new("BTCUSDT");
    let (mut engine, h) =
        build(true, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 3, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert!(heard.borrow().is_empty());
    assert!(h.sends.borrow().is_empty());
}

// ------------------------------------------------ working a resting entry

#[tokio::test]
async fn an_entry_asked_to_be_worked_rests_at_the_touch_instead_of_crossing() {
    let (buyer, _heard) = Buyer::working("BTCUSDT", 1, 0.01, WorkPolicy::default());
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::wide_quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let sends = h.sends.borrow();
    assert_eq!(sends.len(), 1);
    assert_eq!(
        sends[0].kind,
        OrderKind::Limit {
            px: 30_000.0,
            tif: engine_types::TimeInForce::Gtc
        },
        "a limit at the bid, not a market order across the spread"
    );
}

#[tokio::test]
async fn a_spread_too_thin_to_pay_for_still_sends_the_market_order() {
    // The one-tick book. Below two ticks the taker cost is already near the
    // maker floor, so the order goes out exactly as it did before any of this
    // existed.
    let (buyer, _heard) = Buyer::working("BTCUSDT", 1, 0.01, WorkPolicy::default());
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.sends.borrow()[0].kind, OrderKind::Market);
}

#[tokio::test]
async fn an_exit_is_sent_as_written_even_when_it_asks_to_be_worked() {
    // A resting exit that does not fill is exposure nobody wanted, still on
    // the book.
    struct Exiter {
        sent: bool,
    }
    impl Strategy for Exiter {
        fn name(&self) -> &str {
            "exiter"
        }
        fn subscriptions(&self) -> Vec<Subscription> {
            vec![Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }]
        }
        fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
            let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event else {
                return;
            };
            if self.sent {
                return;
            }
            self.sent = true;
            ctx.place(Intent {
                strategy: StrategyId(0),
                symbol: *symbol,
                side: Side::Sell,
                qty: 0.01,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: true,
                tag: "exit".into(),
                decided_ns: ctx.now_ns(),
                work: Some(WorkPolicy::default()),
                leverage: None,
            });
        }
    }

    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(Exiter { sent: false })],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::wide_quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(
        h.sends.borrow()[0].kind,
        OrderKind::Market,
        "the spread was wide enough to rest in; it is an exit that stops it"
    );
}

#[tokio::test]
async fn the_group_flush_tick_walks_a_resting_entry_after_the_market() {
    // The supervisor has no clock of its own: it rides the tick the loop
    // already had.
    let quick = WorkPolicy {
        reprice_ms: 1,
        ..WorkPolicy::default()
    };
    let (buyer, _heard) = Buyer::working("BTCUSDT", 1, 0.01, quick);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let amends = venue.amends.clone();
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut fast = settings(false);
    fast.group_flush_ms = 5;
    let mut engine = Engine::boot(&fast, "0", wal, risk, venue, vec![Box::new(buyer)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    // Two quotes: the first places the entry at 30_000, the second moves the
    // bid up to 30_001 and overtakes it.
    engine
        .run(
            &mut ScriptFeed::wide_quotes(symbol, 2, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    let id = sends.borrow()[0].client_order_id.clone();
    let amends = amends.borrow();
    assert!(!amends.is_empty(), "the overtaken order was not moved");
    assert_eq!(amends[0].1, id);
    assert_eq!(
        amends[0].2,
        AmendSpec {
            px: Some(30_001.0),
            qty: None
        },
        "back to the touch that overtook it, price only"
    );
}

#[tokio::test]
async fn repricing_a_resting_entry_never_costs_an_fsync() {
    // Only an amend that raises the size adds exposure. If a reprice ever
    // barriered, every worked order would pay a disk sync several times a
    // minute — which is the whole latency budget.
    let quick = WorkPolicy {
        reprice_ms: 1,
        ..WorkPolicy::default()
    };
    let (buyer, _heard) = Buyer::working("BTCUSDT", 1, 0.01, quick);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let amends = venue.amends.clone();
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut fast = settings(false);
    fast.group_flush_ms = 5;
    let mut engine = Engine::boot(&fast, "0", wal, risk, venue, vec![Box::new(buyer)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::wide_quotes(symbol, 2, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();
    assert!(!amends.borrow().is_empty(), "there was a reprice to measure");

    // Between writing the reprice down and putting it on the wire is exactly
    // where a barrier would sit if one were added.
    let steps = tape.borrow();
    let mut checked = 0;
    for (i, step) in steps.iter().enumerate() {
        if *step != Step::Append("amend_sent".into()) {
            continue;
        }
        let wire = i + steps[i..]
            .iter()
            .position(|s| matches!(s, Step::Amend(_)))
            .expect("the reprice reaches the venue");
        assert!(
            !steps[i..wire].contains(&Step::Barrier),
            "a reprice forced the log to disk"
        );
        checked += 1;
    }
    assert!(checked > 0, "no reprice was written down");
}

// ---------------------------------------------------------------------------
// Boot compares the log against the venue
// ---------------------------------------------------------------------------

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

#[tokio::test]
async fn a_quiet_account_leaves_the_engine_free_to_trade() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(h.sends.borrow().len(), 1, "nothing was in the way");
    let latched = h.records.borrow().iter().any(|r| {
        matches!(r, WalRecord::Reconciled { may_open, .. } if !may_open)
    });
    assert!(!latched, "there was nothing to latch on");
}

#[tokio::test]
async fn an_order_this_engine_never_placed_stops_it_opening() {
    // Another writer on the account makes every number the kernel works from
    // measure somebody else's trading as well as its own.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_venue_orders(
        false,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        vec![someone_elses_order("BTCUSDT")],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(h.sends.borrow().is_empty(), "no order should have left the box");
    let records = h.records.borrow();
    assert!(
        records.iter().any(|r| matches!(r, WalRecord::Reconciled { may_open: false, .. })),
        "boot must write down that it stopped opening"
    );
    // The intent is still recorded, and refused. A strategy that is never
    // told no is a strategy nobody can debug.
    assert!(records.iter().any(|r| matches!(r, WalRecord::Intent { .. })));
    assert!(records.iter().any(|r| matches!(
        r,
        WalRecord::Verdict { client_order_id: None, verdict: RiskVerdict::Deny { .. } }
    )));
}

#[tokio::test]
async fn a_hand_trade_in_a_symbol_nobody_here_trades_does_not_stop_it() {
    // The owner trades this account by hand. Stopping for an order in a
    // symbol no strategy can even address would mean stopping most days.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_venue_orders(
        false,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        vec![someone_elses_order("DOGEUSDT")],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(h.sends.borrow().len(), 1, "it should still be trading");
}

#[tokio::test]
async fn a_latch_from_an_earlier_boot_survives_the_restart() {
    // The whole point of writing it down. A restart that cleared the latch
    // would turn "stop and tell somebody" into "stop until the next crash",
    // and something restarts this process automatically.
    let earlier = vec![WalRecord::Reconciled {
        wall_ts_ms: 1,
        findings: vec!["someone else was working an order".to_string()],
        may_open: false,
    }];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    // The venue is quiet now: whatever it was has gone. The latch still holds.
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &earlier).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert!(h.sends.borrow().is_empty(), "the latch did not survive the restart");
}

// ---------------------------------------------------------------------------
// The heartbeat file
// ---------------------------------------------------------------------------

/// Every log line written while this is installed, as "target: message".
/// `tracing` keeps no reader of its own, and one of the things worth proving
/// about the heartbeat is that an engine with none configured says nothing at
/// all — which needs a reader that would have heard it.
#[derive(Clone, Default)]
struct Heard(Arc<Mutex<Vec<String>>>);

impl Heard {
    fn lines(&self) -> Vec<String> {
        self.0.lock().expect("nothing panicked while holding this").clone()
    }

    fn about_the_heartbeat(&self) -> Vec<String> {
        self.lines()
            .into_iter()
            .filter(|line| line.contains("heartbeat"))
            .collect()
    }
}

impl tracing::Subscriber for Heard {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }

    fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }

    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}

    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}

    fn event(&self, event: &tracing::Event<'_>) {
        let mut said = Said(String::new());
        event.record(&mut said);
        self.0
            .lock()
            .expect("nothing panicked while holding this")
            .push(format!("{}: {}", event.metadata().target(), said.0));
    }

    fn enter(&self, _: &tracing::span::Id) {}

    fn exit(&self, _: &tracing::span::Id) {}
}

/// The words of one log line. Everything else about the event is dropped.
struct Said(String);

impl tracing::field::Visit for Said {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            self.0 = format!("{value:?}");
        }
    }
}

/// A tick every few milliseconds, so a test does not sit through the shipping
/// quarter-second.
fn quick_tick(shadow: bool) -> EngineSection {
    let mut settings = settings(shadow);
    settings.group_flush_ms = 5;
    settings
}

/// A heartbeat that beats on every tick, for the same reason.
fn every_tick(path: &std::path::Path) -> Heartbeat {
    Heartbeat::with_every(
        path.to_path_buf(),
        Some(AccountIdentity { user_id: "6039967".into(), realm: "demo".into() }),
        Some(std::path::PathBuf::from(
            "/run/lock/liquidity-migration/bybit-demo-user-6039967.lock",
        )),
        Duration::from_millis(1),
    )
}

fn heartbeat_at(path: &std::path::Path) -> serde_json::Map<String, serde_json::Value> {
    let raw = std::fs::read_to_string(path).expect("a heartbeat was written");
    serde_json::from_str::<serde_json::Value>(&raw)
        .unwrap_or_else(|e| panic!("the heartbeat is not JSON ({e}): {raw}"))
        .as_object()
        .expect("the heartbeat is an object")
        .clone()
}

#[tokio::test]
async fn a_running_engine_leaves_a_heartbeat_saying_how_it_is() {
    let path = temp_path("heartbeat-running");
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with(
        &quick_tick(false),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    engine.write_heartbeat(every_tick(path.path()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    let fields = heartbeat_at(path.path());
    assert_eq!(fields["mode"], "live", "this run was sending orders");
    assert_eq!(fields["may_open"].as_bool(), Some(true));
    assert_eq!(fields["account_user_id"], "6039967");
    assert_eq!(fields["realm"], "demo");
    assert_eq!(
        fields["lease_path"],
        "/run/lock/liquidity-migration/bybit-demo-user-6039967.lock"
    );
    assert_eq!(fields["strategies"][0], "buyer");
    assert_eq!(fields["market_events"], 2, "both quotes were seen");
    assert_eq!(
        fields["orders_sent"].as_u64(),
        Some(h.sends.borrow().len() as u64),
        "the count in the file is the count that went out"
    );
    // An outside reader has no access to the engine's monotonic clock, so the
    // stamp has to be a wall clock it can age against its own. Spelled the
    // way the log's own Boot and Reconciled records spell it.
    let wall_ts_ms = fields["wall_ts_ms"].as_i64().expect("a wall-clock stamp");
    let age_ms = crate::clock::wall_ms() - wall_ts_ms;
    assert!((0..60_000).contains(&age_ms), "the stamp is not a live wall clock: {age_ms}ms old");
}

#[tokio::test]
async fn a_shadow_engine_says_shadow() {
    let path = temp_path("heartbeat-shadow");
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, _h) = build_with(
        &quick_tick(true),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    engine.write_heartbeat(every_tick(path.path()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();
    assert_eq!(heartbeat_at(path.path())["mode"], "shadow");
}

#[tokio::test]
async fn an_engine_latched_out_of_opening_says_so_in_its_heartbeat() {
    // The field the whole file is for. This engine is running, reading the
    // market, and refusing every entry; from the outside it is indistinguish-
    // able from a healthy one unless it says this.
    let path = temp_path("heartbeat-latched");
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with(
        &quick_tick(false),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        vec![someone_elses_order("BTCUSDT")],
    )
    .await;
    engine.write_heartbeat(every_tick(path.path()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    assert!(h.sends.borrow().is_empty(), "it is latched, so nothing went out");
    let fields = heartbeat_at(path.path());
    assert_eq!(fields["may_open"].as_bool(), Some(false));
    assert!(
        fields["market_events"].as_u64().unwrap_or(0) > 0,
        "and it looks alive, which is the point: {fields:?}"
    );
}

#[tokio::test]
async fn a_heartbeat_that_cannot_be_written_does_not_stop_the_engine() {
    // Telemetry. The loop does not depend on it, and a full disk or a wrong
    // path must not be the reason an order does not go out.
    let nowhere = temp_path("heartbeat-unwritable");
    let path = nowhere.path().join("no-such-directory").join("beat.json");
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with(
        &quick_tick(false),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    engine.write_heartbeat(every_tick(&path));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    let heard = Heard::default();
    let guard = tracing::subscriber::set_default(heard.clone());
    let outcome = engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .expect("a heartbeat it cannot write is not a reason to stop");
    drop(guard);

    assert_eq!(outcome.market_events, 1, "the loop kept running");
    assert_eq!(h.sends.borrow().len(), 1, "and the order still went out");
    assert!(!path.exists(), "nothing was written");
    // Said once, not on every one of the several ticks in that window: this
    // path is wrong every time it is tried.
    let complaints = heard.about_the_heartbeat();
    assert_eq!(complaints.len(), 1, "{complaints:?}");
    assert!(complaints[0].contains("cannot write the heartbeat"), "{complaints:?}");
}

#[tokio::test]
async fn no_heartbeat_configured_writes_nothing_and_says_nothing() {
    // Silence has to be genuine silence. A warning every few seconds about a
    // file nobody asked for would be noise in every log the fleet keeps, and
    // the test above proves these ears can hear a heartbeat complaining.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, _h) = build_with(
        &quick_tick(false),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    let heard = Heard::default();
    let guard = tracing::subscriber::set_default(heard.clone());
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();
    drop(guard);

    let said = heard.about_the_heartbeat();
    assert!(said.is_empty(), "an engine with no heartbeat still talked about one: {said:?}");
}

#[test]
fn no_configured_path_means_no_heartbeat_writer_at_all() {
    // The other half of staying quiet: nothing is built, so nothing can be
    // said and no file can appear.
    let mut named = settings(true);
    assert!(crate::assembly::heartbeat(&named, None, None).is_none());
    named.heartbeat_path = Some("var/engine-heartbeat.json".into());
    let built = crate::assembly::heartbeat(&named, None, None).expect("a path means a heartbeat");
    assert_eq!(built.path(), std::path::Path::new("var/engine-heartbeat.json"));
}
