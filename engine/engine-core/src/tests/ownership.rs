use super::*;

#[derive(Default)]
struct Observed {
    foreign: Vec<bool>,
    refused: Vec<String>,
}

struct Proposer {
    name: &'static str,
    side: Side,
    qty: f64,
    reduce_only: bool,
    copies: usize,
    observed: Rc<RefCell<Observed>>,
}

impl Strategy for Proposer {
    fn name(&self) -> &str {
        self.name
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let MarketEvent::Quote { symbol, .. } = event else {
            return;
        };
        self.observed
            .lock()
            .unwrap()
            .foreign
            .push(ctx.foreign_position(*symbol));
        for _ in 0..self.copies {
            ctx.place(Intent {
                strategy: StrategyId(0),
                symbol: *symbol,
                side: self.side,
                qty: self.qty,
                kind: OrderKind::Market,
                stop: (!self.reduce_only).then_some(StopSpec {
                    trigger_px: stop(self.side),
                }),
                reduce_only: self.reduce_only,
                tag: self.name.into(),
                decided_ns: ctx.now_ns(),
                work: None,
                leverage: None,
            });
        }
        self.copies = 0;
    }

    fn on_intent_refused(
        &mut self,
        _symbol: SymbolId,
        _reduce_only: bool,
        reason: &str,
        _ctx: &mut dyn StrategyCtx,
    ) {
        self.observed.lock().unwrap().refused.push(reason.into());
    }
}

fn stop(side: Side) -> f64 {
    match side {
        Side::Buy => 29_000.0,
        Side::Sell => 31_000.0,
    }
}

fn proposer(
    name: &'static str,
    side: Side,
    copies: usize,
    reduce_only: bool,
) -> (Box<dyn Strategy>, Rc<RefCell<Observed>>) {
    let observed = Rc::new(RefCell::new(Observed::default()));
    (
        Box::new(Proposer {
            name,
            side,
            qty: 0.01,
            reduce_only,
            copies,
            observed: observed.clone(),
        }),
        observed,
    )
}

fn prior_order(owner: u16, side: Side) -> Vec<WalRecord> {
    vec![
        WalRecord::Names {
            strategies: vec!["left".into(), "right".into()],
            symbols: vec!["BTCUSDT".into()],
        },
        WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: "eng-prior-1".into(),
                strategy: StrategyId(owner),
                symbol: SymbolId(0),
                side,
                qty: 0.01,
                kind: OrderKind::Market,
                stop: Some(StopSpec {
                    trigger_px: stop(side),
                }),
                reduce_only: false,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 30_000.0,
        },
    ]
}

fn fill(side: Side) -> WalRecord {
    WalRecord::OrderUpdate {
        update: OrderUpdate::Fill {
            exec_id: "prior-fill".into(),
            client_order_id: "eng-prior-1".into(),
            symbol: SymbolId(0),
            side,
            qty: 0.01,
            px: 30_000.0,
            fee: Some(0.01),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: recent_replay_ms(),
            recv_ns: 2,
        },
    }
}

fn held(side: Side) -> Vec<engine_types::PositionView> {
    vec![engine_types::PositionView {
        symbol: SymbolId(0),
        side,
        qty: 0.01,
        entry_px: 30_000.0,
        stop_attached: true,
        stop_px: stop(side),
        leverage: None,
    }]
}

async fn one_quote(engine: &mut Engine<MockWal, MockRisk, MockVenue>) {
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
}

#[tokio::test]
async fn one_batch_cannot_reserve_one_symbol_for_two_strategies() {
    for first_side in [Side::Buy, Side::Sell] {
        for second_side in [Side::Buy, Side::Sell] {
            let (first, _) = proposer("left", first_side, 1, false);
            let (second, observed) = proposer("right", second_side, 1, false);
            let (mut engine, h) = build(allow_all(), vec![first, second], &["BTCUSDT"], &[]).await;
            one_quote(&mut engine).await;
            let sends = h.sends.lock().unwrap();
            assert_eq!(sends.len(), 1, "{first_side:?}/{second_side:?}");
            assert_eq!(sends[0].strategy, StrategyId(0));
            assert_eq!(observed.lock().unwrap().refused, ["foreign_strategy_owner"]);
        }
    }
}

#[tokio::test]
async fn replayed_foreign_fills_block_both_entry_directions() {
    for owned_side in [Side::Buy, Side::Sell] {
        for proposed_side in [Side::Buy, Side::Sell] {
            let mut prior = prior_order(0, owned_side);
            prior.push(fill(owned_side));
            let (idle, _) = proposer("left", owned_side, 0, false);
            let (entry, observed) = proposer("right", proposed_side, 1, false);
            let (mut engine, h) = build_with_venue_state(
                allow_all(),
                vec![idle, entry],
                &["BTCUSDT"],
                &prior,
                vec![],
                held(owned_side),
            )
            .await;
            one_quote(&mut engine).await;
            assert!(h.sends.lock().unwrap().is_empty());
            assert_eq!(observed.lock().unwrap().refused, ["foreign_strategy_owner"]);
        }
    }
}

#[tokio::test]
async fn replayed_foreign_openings_block_before_any_fill() {
    for owned_side in [Side::Buy, Side::Sell] {
        for proposed_side in [Side::Buy, Side::Sell] {
            let prior = prior_order(0, owned_side);
            let mut working = still_working("eng-prior-1", "BTCUSDT", 0.01);
            working.side = owned_side;
            let (idle, _) = proposer("left", owned_side, 0, false);
            let (entry, observed) = proposer("right", proposed_side, 1, false);
            let (mut engine, h) = build_with_venue_orders(
                allow_all(),
                vec![idle, entry],
                &["BTCUSDT"],
                &prior,
                vec![working],
            )
            .await;
            one_quote(&mut engine).await;
            assert!(h.sends.lock().unwrap().is_empty());
            let observed = observed.lock().unwrap();
            assert_eq!(observed.foreign, [true]);
            assert_eq!(observed.refused, ["foreign_strategy_owner"]);
        }
    }
}

#[tokio::test]
async fn one_owner_may_keep_multiple_orders_and_reduce_its_holding() {
    for side in [Side::Buy, Side::Sell] {
        for reduce_only in [false, true] {
            let mut prior = prior_order(1, side);
            prior.push(fill(side));
            let (idle, _) = proposer("left", side, 0, false);
            let proposed_side = if reduce_only { side.flipped() } else { side };
            let copies = if reduce_only { 1 } else { 2 };
            let (entry, observed) = proposer("right", proposed_side, copies, reduce_only);
            let (mut engine, h) = build_with_venue_state(
                allow_all(),
                vec![idle, entry],
                &["BTCUSDT"],
                &prior,
                vec![],
                held(side),
            )
            .await;
            one_quote(&mut engine).await;
            assert_eq!(h.sends.lock().unwrap().len(), copies);
            assert!(observed.lock().unwrap().refused.is_empty());
        }
    }
}

#[tokio::test]
async fn a_cancel_releases_ownership_but_a_late_fill_restores_it_on_replay() {
    for late_fill in [false, true] {
        let mut prior = prior_order(0, Side::Buy);
        prior.push(WalRecord::OrderUpdate {
            update: OrderUpdate::Cancelled {
                client_order_id: "eng-prior-1".into(),
                recv_ns: 2,
            },
        });
        if late_fill {
            prior.push(fill(Side::Buy));
        }
        let (idle, _) = proposer("left", Side::Buy, 0, false);
        let (entry, observed) = proposer("right", Side::Buy, 1, false);
        let (mut engine, h) = build_with_venue_state(
            allow_all(),
            vec![idle, entry],
            &["BTCUSDT"],
            &prior,
            vec![],
            if late_fill { held(Side::Buy) } else { vec![] },
        )
        .await;
        one_quote(&mut engine).await;
        assert_eq!(h.sends.lock().unwrap().len(), usize::from(!late_fill));
        assert_eq!(observed.lock().unwrap().foreign, [late_fill]);
    }
}

struct Editor {
    amend: bool,
    cancel: bool,
    tighten: bool,
}

impl Strategy for Editor {
    fn name(&self) -> &str {
        "right"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let MarketEvent::Quote { symbol, .. } = event else {
            return;
        };
        if std::mem::take(&mut self.amend) {
            ctx.amend(
                *symbol,
                "eng-edit-1",
                AmendSpec {
                    px: Some(30_000.0),
                    qty: None,
                },
            );
        }
        if std::mem::take(&mut self.cancel) {
            ctx.cancel(*symbol, "eng-edit-1");
        }
        if std::mem::take(&mut self.tighten) {
            ctx.emit(engine_types::Action::SetStop {
                symbol: *symbol,
                trigger_px: 29_500.0,
            });
        }
    }
}

async fn editor_engine(
    foreign_filled: bool,
    reduce_only: bool,
    editor: Editor,
) -> (Engine<MockWal, MockRisk, MockVenue>, Harness) {
    let mut prior = prior_order(0, Side::Buy);
    let mut working = vec![];
    if foreign_filled {
        prior.push(fill(Side::Buy));
    } else {
        working.push(still_working("eng-prior-1", "BTCUSDT", 0.01));
    }
    let mut edited = prior_order(1, if reduce_only { Side::Sell } else { Side::Buy })
        .pop()
        .unwrap();
    let WalRecord::OrderSent { request, .. } = &mut edited else {
        unreachable!()
    };
    request.client_order_id = "eng-edit-1".into();
    request.kind = OrderKind::Limit {
        px: 29_999.0,
        tif: TimeInForce::Gtc,
    };
    request.reduce_only = reduce_only;
    if reduce_only {
        request.stop = None;
    }
    let mut venue_edit = still_working("eng-edit-1", "BTCUSDT", 0.01);
    venue_edit.side = request.side;
    venue_edit.reduce_only = reduce_only;
    working.push(venue_edit);
    prior.push(edited);
    let (idle, _) = proposer("left", Side::Buy, 0, false);
    build_with_venue_state(
        allow_all(),
        vec![idle, Box::new(editor)],
        &["BTCUSDT"],
        &prior,
        working,
        if foreign_filled {
            held(Side::Buy)
        } else {
            vec![]
        },
    )
    .await
}

#[tokio::test]
async fn an_opening_amend_cannot_bypass_foreign_ownership() {
    for foreign_filled in [false, true] {
        let (mut engine, h) = editor_engine(
            foreign_filled,
            false,
            Editor {
                amend: true,
                cancel: false,
                tighten: false,
            },
        )
        .await;
        one_quote(&mut engine).await;
        assert!(h.amends.lock().unwrap().is_empty());
        let records = h.records.lock().unwrap();
        assert!(!records
            .iter()
            .any(|r| matches!(r, WalRecord::AmendSent { .. })));
        assert!(records.iter().any(|r| matches!(r,
            WalRecord::Verdict {
                verdict: RiskVerdict::Deny {
                    reason: DenyReason::UnknownState { detail },
                },
                ..
            } if detail.starts_with("foreign_strategy_owner:")
        )));
    }
}

#[tokio::test]
async fn foreign_ownership_preserves_own_exit_orders_but_not_foreign_stop_control() {
    let (mut engine, h) = editor_engine(
        true,
        true,
        Editor {
            amend: true,
            cancel: true,
            tighten: true,
        },
    )
    .await;
    one_quote(&mut engine).await;
    assert_eq!(h.amends.lock().unwrap().len(), 1);
    assert_eq!(h.cancels.lock().unwrap().len(), 1);
    assert!(!h.stops.lock().unwrap().contains(&(SymbolId(0), 29_500.0)));
}

struct ForeignOrderEditor {
    amend: bool,
}
impl Strategy for ForeignOrderEditor {
    fn name(&self) -> &str {
        "right"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let MarketEvent::Quote { symbol, .. } = event else {
            return;
        };
        if self.amend {
            ctx.amend(
                *symbol,
                "eng-prior-1",
                AmendSpec {
                    px: Some(30_000.0),
                    qty: None,
                },
            );
        } else {
            ctx.cancel(*symbol, "eng-prior-1");
        }
    }
}

#[tokio::test]
async fn callback_cannot_cancel_or_amend_another_sleeves_order() {
    let mut observed = Vec::new();
    for amend in [false, true] {
        let (idle, _) = proposer("left", Side::Buy, 0, false);
        let mut prior = prior_order(0, Side::Buy);
        let WalRecord::OrderSent { request, .. } = prior.last_mut().unwrap() else {
            unreachable!()
        };
        request.kind = OrderKind::Limit {
            px: 29_999.0,
            tif: TimeInForce::Gtc,
        };
        let (mut engine, h) = build_with_venue_state(
            allow_all(),
            vec![idle, Box::new(ForeignOrderEditor { amend })],
            &["BTCUSDT"],
            &prior,
            vec![still_working("eng-prior-1", "BTCUSDT", 0.01)],
            vec![],
        )
        .await;
        one_quote(&mut engine).await;
        observed.push((
            h.cancels.lock().unwrap().len(),
            h.amends.lock().unwrap().len(),
        ));
    }
    assert_eq!(
        observed,
        [(0, 0), (0, 0)],
        "foreign cancel or amend reached venue"
    );
}
