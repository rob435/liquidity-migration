//! The engine's in-flight accounting, driven by real order flow and read
//! back the way a strategy reads it: `ctx.in_flight`, sampled at every wake.
//!
//! Three of these re-express scenarios that used to be pinned as mock-fed
//! follower tests (a refused entry, a refused exit, a cancel with a partial
//! fill); the mechanism moved into the engine, so the pins moved with it.
//! The bench these run on — the tape, the mocks and the helpers — is
//! [`super`]; the arithmetic itself is unit-tested in `covers.rs`.

use super::*;
use engine_types::PositionView;

/// What one wake saw: what kind of event it was, and what `ctx.in_flight`
/// answered for the probe's symbol at that moment.
type Wakes = Rc<RefCell<Vec<(&'static str, f64)>>>;

/// Places one scripted intent per quote and writes down the in-flight
/// reading at every wake, sampled BEFORE it places anything.
struct CoverProbe {
    symbol: String,
    /// One entry per quote, oldest first: signed quantity (positive buys)
    /// and whether the intent is reduce-only. `NaN` is a deliberately unreal
    /// number — the engine refuses it before anything else happens, which is
    /// the cheapest refusal a test can order up for either intent kind.
    plan: VecDeque<(f64, bool)>,
    seen: Wakes,
}

impl CoverProbe {
    fn new(symbol: &str, plan: Vec<(f64, bool)>) -> (Self, Wakes) {
        let seen = Rc::new(RefCell::new(Vec::new()));
        (
            CoverProbe {
                symbol: symbol.to_string(),
                plan: plan.into(),
                seen: seen.clone(),
            },
            seen,
        )
    }
}

impl Strategy for CoverProbe {
    fn name(&self) -> &str {
        "cover-probe"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    // The probe overrides the routing itself, deliberately: its whole job is
    // to sample `ctx.in_flight` at EVERY wake, whatever kind it is.
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let Some(symbol) = ctx.symbol_id(&self.symbol) else {
            return;
        };
        let tag = match event {
            EngineEvent::Boot => "boot",
            EngineEvent::Market(_) => "quote",
            EngineEvent::Timer { .. } => "timer",
            EngineEvent::Order(OrderUpdate::Ack(_)) => "ack",
            EngineEvent::Order(OrderUpdate::Fill { .. }) => "fill",
            EngineEvent::Order(OrderUpdate::FastFill { .. }) => "fast-fill",
            EngineEvent::Order(OrderUpdate::Cancelled { .. }) => "cancelled",
            EngineEvent::Order(OrderUpdate::Amended { .. }) => "amended",
            EngineEvent::Order(OrderUpdate::Reject { .. }) => "reject",
            EngineEvent::Order(OrderUpdate::StopAttached { .. }) => "stop",
            EngineEvent::Order(OrderUpdate::StreamReset { .. }) => "reset",
            EngineEvent::Signal(_) => "signal",
            EngineEvent::StrategyEvent(_) => "strategy-event",
            EngineEvent::IntentRefused { .. } => "refused",
            EngineEvent::EntryPermission { .. } => "entry-permission",
            EngineEvent::FlattenDirectional { .. } => "flatten-directional",
        };
        self.seen.lock().unwrap().push((tag, ctx.in_flight(symbol)));
        if let EngineEvent::Market(MarketEvent::Quote { quote, .. }) = event {
            if let Some((signed_qty, reduce_only)) = self.plan.pop_front() {
                let side = if signed_qty >= 0.0 {
                    Side::Buy
                } else {
                    Side::Sell
                };
                ctx.place(Intent {
                    strategy: StrategyId(0),
                    symbol,
                    side,
                    qty: signed_qty.abs(),
                    kind: OrderKind::Market,
                    stop: (!reduce_only).then_some(StopSpec {
                        trigger_px: quote.bid_px * 0.99,
                    }),
                    reduce_only,
                    tag: "probe".into(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
        }
    }
}

/// The newest in-flight reading recorded at a wake of this kind.
fn read_at(seen: &Wakes, tag: &str) -> f64 {
    seen.lock()
        .unwrap()
        .iter()
        .rev()
        .find(|(t, _)| *t == tag)
        .map(|(_, v)| *v)
        .unwrap_or_else(|| panic!("no {tag} wake was recorded: {:?}", seen.lock().unwrap()))
}

/// Stop the loop once the probe has been woken by this kind of event, or
/// give up so a failure reads as an assertion and not a hung test.
async fn until_woken_by(seen: Wakes, tag: &'static str) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !seen.lock().unwrap().iter().any(|(t, _)| *t == tag)
        && tokio::time::Instant::now() < deadline
    {
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9
}

#[tokio::test]
async fn a_send_is_covered_at_its_quantized_size_until_the_reading_shows_it() {
    // The probe asks for 0.0105; the venue's step is 0.001, so 0.010 is what
    // actually goes out — and 0.010, not the ask, is what must be covered.
    // Registering at the send is what makes that true without any ack-time
    // clamp: the engine knows the real size before the wire.
    let (probe, seen) = CoverProbe::new("BTCUSDT", vec![(0.0105, false)]);
    let (mut engine, _h) = build(allow_all(), vec![Box::new(probe)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(
        close(read_at(&seen, "ack"), 0.010),
        "covered at the quantized size, got {}",
        read_at(&seen, "ack")
    );
    let last_quote = read_at(&seen, "quote");
    assert!(
        close(last_quote, 0.010),
        "still covered on the next quote — the reading has shown nothing yet, got {last_quote}"
    );
}

#[tokio::test]
async fn the_reading_catching_up_part_way_shrinks_the_cover_to_the_remainder() {
    // 0.010 went out; the next account reading shows 0.004 of it. The cover
    // must come down to exactly the 0.006 the reading has not shown —
    // dropping the whole record here was the old double-entry window.
    let (probe, seen) = CoverProbe::new("BTCUSDT", vec![(0.01, false)]);
    let (mut engine, h) = build(allow_all(), vec![Box::new(probe)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    // First: the send happens (quote, place, inline ack), then the feed ends.
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert!(close(read_at(&seen, "ack"), 0.010));

    // Then: a stream reset forces a fresh reading, which reports 0.004. The
    // stop-attach news right behind it is just a wake the probe can sample
    // the post-reading number at.
    h.account_readings
        .lock()
        .unwrap()
        .push_back(vec![PositionView {
            symbol,
            side: Side::Buy,
            qty: 0.004,
            entry_px: 30_000.0,
            stop_attached: true,
            stop_px: 0.0,
            leverage: None,
        }]);
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![
                OrderUpdate::StreamReset { recv_ns: 1 },
                OrderUpdate::StopAttached {
                    symbol,
                    trigger_px: 29_000.0,
                    recv_ns: 2,
                },
            ]),
            until_woken_by(seen.clone(), "stop"),
        )
        .await
        .unwrap();

    let after = read_at(&seen, "stop");
    assert!(
        close(after, 0.006),
        "the shown 0.004 is absorbed and the unseen 0.006 stays covered, got {after}"
    );
}

#[tokio::test]
async fn a_refused_entry_frees_the_symbol_and_leaves_older_covers_alone() {
    // Two halves of one fact: a refused entry was never booked, so it holds
    // no phantom cover (a follower is free to retry the entry at once), and
    // refusing it must not release the cover an EARLIER send is still
    // holding against a fill the reading has not shown.
    let (probe, seen) = CoverProbe::new("BTCUSDT", vec![(0.01, false), (f64::NAN, false)]);
    let (mut engine, _h) = build(allow_all(), vec![Box::new(probe)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let at_refusal = read_at(&seen, "refused");
    assert!(
        close(at_refusal, 0.010),
        "the earlier send stays covered and the refused one adds nothing, got {at_refusal}"
    );
}

#[tokio::test]
async fn a_refused_exit_drops_every_cover_for_its_symbol() {
    // The engine refusing an exit means the covers and the account reading
    // disagree about what is held, and the reading is the fact. Anything
    // short of dropping them all re-plans the same doomed exit on every
    // quote for as long as the disagreement lasts.
    let (probe, seen) = CoverProbe::new("BTCUSDT", vec![(0.01, false), (f64::NAN, true)]);
    let (mut engine, _h) = build(allow_all(), vec![Box::new(probe)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(
        close(read_at(&seen, "ack"), 0.010),
        "the entry was covered first"
    );
    let at_refusal = read_at(&seen, "refused");
    assert!(
        close(at_refusal, 0.0),
        "every cover on the symbol goes with the refused exit, got {at_refusal}"
    );
}

#[tokio::test]
async fn a_cancel_releases_only_the_unfilled_remainder() {
    // A worked order was pulled after filling 0.004 of its 0.010: the 0.006
    // that never happened is freed, the filled 0.004 stays covered until the
    // account reading shows it.
    let (probe, seen) = CoverProbe::new("BTCUSDT", vec![(0.01, false)]);
    let (mut engine, h) = build(allow_all(), vec![Box::new(probe)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let id = h.sends.lock().unwrap()[0].client_order_id.clone();

    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![
                OrderUpdate::Fill {
                    exec_id: String::new(),
                    client_order_id: id.clone(),
                    symbol,
                    side: Side::Buy,
                    qty: 0.004,
                    px: 30_000.0,
                    fee: Some(0.0),
                    is_maker: false,
                    venue_ts_ms: 1,
                    recv_ns: 1,
                },
                OrderUpdate::Cancelled {
                    client_order_id: id,
                    recv_ns: 2,
                },
            ]),
            until_woken_by(seen.clone(), "cancelled"),
        )
        .await
        .unwrap();

    assert!(
        close(read_at(&seen, "fill"), 0.010),
        "a fill releases nothing; it stays covered until the reading shows it, got {}",
        read_at(&seen, "fill")
    );
    let after = read_at(&seen, "cancelled");
    assert!(
        close(after, 0.004),
        "only the 0.006 that never filled is released, got {after}"
    );
}

#[tokio::test]
async fn a_reject_releases_the_whole_send() {
    // Rejected at the venue: it never worked and nothing of it filled, so
    // nothing of it can ever show in the reading. The whole cover goes.
    let (probe, seen) = CoverProbe::new("BTCUSDT", vec![(0.01, false)]);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (mut venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.reply = Some(VenueError::Rejected {
        code: 110007,
        message: "not enough balance".into(),
    });
    let (risk, _saw) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(probe)],
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

    let at_reject = read_at(&seen, "reject");
    assert!(
        close(at_reject, 0.0),
        "a rejected send leaves nothing in flight, got {at_reject}"
    );
}
