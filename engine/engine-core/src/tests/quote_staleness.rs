//! The venue-stall bound: entries are refused against a quote older than the
//! configured age — or against no quote at all — and exits flow whatever the
//! age. The mirror of the account-view bound, on the market side.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

/// Every refusal the opener saw: the symbol, and whether it was reduce-only.
type Refusals = Rc<RefCell<Vec<(SymbolId, bool)>>>;

/// Places one intent on the first quote it hears — for whatever symbol the
/// test aimed it at, which is how an entry gets decided against a price that
/// never arrived — and writes down every refusal.
struct Opener {
    subscribe: Vec<String>,
    target: String,
    reduce_only: bool,
    placed: bool,
    refused: Refusals,
}

impl Opener {
    fn new(
        subscribe: &[&str],
        target: &str,
        reduce_only: bool,
    ) -> (Self, Refusals) {
        let refused = Rc::new(RefCell::new(Vec::new()));
        (
            Opener {
                subscribe: subscribe.iter().map(|s| s.to_string()).collect(),
                target: target.to_string(),
                reduce_only,
                placed: false,
                refused: refused.clone(),
            },
            refused,
        )
    }
}

impl Strategy for Opener {
    fn name(&self) -> &str {
        "opener"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        self.subscribe
            .iter()
            .map(|symbol| Subscription { symbol: symbol.clone(), feed: Feed::Quote })
            .collect()
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        match event {
            EngineEvent::Market(MarketEvent::Quote { .. }) if !self.placed => {
                self.placed = true;
                let symbol = ctx.symbol_id(&self.target).expect("the target is subscribed");
                ctx.place(Intent {
                    strategy: StrategyId(0),
                    symbol,
                    side: Side::Buy,
                    qty: 0.5,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec { trigger_px: 90.0 }),
                    reduce_only: self.reduce_only,
                    tag: "open".into(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
            EngineEvent::IntentRefused { symbol, reduce_only } => {
                self.refused.borrow_mut().push((*symbol, *reduce_only));
            }
            _ => {}
        }
    }
}

/// One quote for symbol 0 whose receive stamp the test chooses.
fn feed_with_stamp(recv_ns: u64) -> ScriptFeed {
    ScriptFeed {
        events: vec![MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: Quote {
                bid_px: 100.0,
                bid_qty: 1.0,
                ask_px: 100.5,
                ask_qty: 1.0,
                venue_ts_ms: 1,
                recv_ns,
                seq: 1,
            },
        }]
        .into(),
        close_at_end: true,
        admitted: Rc::new(RefCell::new(Vec::new())),
        known: 1,
        admits_wrongly: false,
    }
}

/// Settings with a quote bound tight enough to test against a hand-aged
/// stamp, and everything else as the bench has it.
fn tight(max_quote_age_ms: u64) -> EngineSection {
    EngineSection { max_quote_age_ms, ..settings(false) }
}

/// The engine's clock origin is the process's first call; make sure enough
/// has elapsed that a stamp of 1 ns is older than the tightest bound used
/// here, whichever test runs first.
fn age_the_clock() {
    let t0 = clock::now_ns();
    while clock::now_ns() < t0 + 12_000_000 {
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn stale_quote_denials(records: &Rc<RefCell<Vec<WalRecord>>>) -> Vec<(u64, u64)> {
    records
        .borrow()
        .iter()
        .filter_map(|record| match record {
            WalRecord::Verdict {
                verdict: RiskVerdict::Deny { reason: DenyReason::StaleQuote { age_ns, max_age_ns } },
                ..
            } => Some((*age_ns, *max_age_ns)),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn an_entry_against_a_fresh_quote_passes() {
    let (opener, refused) = Opener::new(&["BTCUSDT"], "BTCUSDT", false);
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(opener)], &["BTCUSDT"], &[]).await;
    let mut feed = feed_with_stamp(clock::now_ns());
    engine
        .run(&mut feed, &mut ScriptOrderFeed::empty(), std::future::pending::<()>())
        .await
        .unwrap();
    assert_eq!(h.sends.borrow().len(), 1, "a fresh quote opens");
    assert!(refused.borrow().is_empty());
    assert!(stale_quote_denials(&h.records).is_empty());
}

#[tokio::test]
async fn an_entry_against_a_quote_past_the_bound_is_refused_and_the_strategy_hears_it() {
    age_the_clock();
    let (opener, refused) = Opener::new(&["BTCUSDT"], "BTCUSDT", false);
    let (mut engine, h) = build_with(
        &tight(5),
        allow_all(),
        vec![Box::new(opener)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    // A stamp from the dawn of the process: the price stood while the world
    // moved — the silent-socket shape.
    let mut feed = feed_with_stamp(1);
    engine
        .run(&mut feed, &mut ScriptOrderFeed::empty(), std::future::pending::<()>())
        .await
        .unwrap();

    assert!(h.sends.borrow().is_empty(), "nothing reaches the venue");
    let denials = stale_quote_denials(&h.records);
    assert_eq!(denials.len(), 1, "the refusal is written down as a verdict");
    let (age_ns, max_age_ns) = denials[0];
    assert_eq!(max_age_ns, 5_000_000, "the bound is the configured one");
    assert!(age_ns > max_age_ns, "and the age really was past it: {age_ns}");
    assert_eq!(
        refused.borrow().as_slice(),
        &[(SymbolId(0), false)],
        "the strategy hears IntentRefused for its symbol"
    );
}

#[tokio::test]
async fn an_exit_under_the_same_staleness_flows() {
    age_the_clock();
    let (opener, refused) = Opener::new(&["BTCUSDT"], "BTCUSDT", true);
    let (mut engine, h) = build_with(
        &tight(5),
        allow_all(),
        vec![Box::new(opener)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    let mut feed = feed_with_stamp(1);
    engine
        .run(&mut feed, &mut ScriptOrderFeed::empty(), std::future::pending::<()>())
        .await
        .unwrap();
    assert_eq!(
        h.sends.borrow().len(),
        1,
        "taking risk off never waits on a fresh price"
    );
    assert!(h.sends.borrow()[0].reduce_only);
    assert!(refused.borrow().is_empty());
    assert!(stale_quote_denials(&h.records).is_empty());
}

#[tokio::test]
async fn a_symbol_that_never_quoted_is_refused_for_entries() {
    // BTC quotes, ETH never has. The entry aimed at ETH is refused even
    // though every quote on the tape is fresh: the absence of a price is the
    // stalest price there is.
    let (opener, refused) = Opener::new(&["BTCUSDT", "ETHUSDT"], "ETHUSDT", false);
    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(opener)],
        &["BTCUSDT", "ETHUSDT"],
        &[],
    )
    .await;
    let eth = engine.market().table.get("ETHUSDT").unwrap();
    let mut feed = feed_with_stamp(clock::now_ns());
    engine
        .run(&mut feed, &mut ScriptOrderFeed::empty(), std::future::pending::<()>())
        .await
        .unwrap();
    assert!(h.sends.borrow().is_empty());
    assert_eq!(stale_quote_denials(&h.records).len(), 1);
    assert_eq!(refused.borrow().as_slice(), &[(eth, false)]);
}
