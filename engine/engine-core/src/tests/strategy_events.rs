//! Cross-sleeve messages are durable before delivery and scoped to their two
//! participants.

use super::*;
use engine_types::{Action, StrategyEvent};

struct Publisher {
    published: bool,
}

impl Strategy for Publisher {
    fn name(&self) -> &str {
        "carry"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_market(&mut self, _event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        if self.published {
            return;
        }
        self.published = true;
        ctx.emit(Action::PublishStrategyEvent {
            event: StrategyEvent {
                source: StrategyId(99),
                destination: StrategyId(1),
                kind: "carry_fire".into(),
                event_id: "fire-1".into(),
                payload: b"exact-target".to_vec(),
            },
        });
    }
}

struct Consumer {
    received: Rc<RefCell<Vec<StrategyEvent>>>,
}

impl Strategy for Consumer {
    fn name(&self) -> &str {
        "exodus"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "ETHUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_strategy_event(&mut self, event: &StrategyEvent, ctx: &mut dyn StrategyCtx) {
        let mut visible = Vec::new();
        ctx.strategy_events(&mut visible);
        assert_eq!(visible.as_slice(), std::slice::from_ref(event));
        self.received.lock().unwrap().push(event.clone());
        ctx.emit(Action::ConsumeStrategyEvent {
            source: event.source,
            destination: StrategyId(88),
            event_id: event.event_id.clone(),
        });
    }
}

#[tokio::test]
async fn publication_barrier_precedes_destination_delivery_and_consumption() {
    let received = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, h) = build(
        allow_all(),
        vec![
            Box::new(Publisher { published: false }),
            Box::new(Consumer {
                received: received.clone(),
            }),
        ],
        &["BTCUSDT", "ETHUSDT"],
        &[],
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
    assert_eq!(received.lock().unwrap()[0].source, StrategyId(0));
    let start = after_boot(&h.tape);
    let published = after(
        &h.tape,
        &Step::Append("strategy_event_published".into()),
        start,
    )
    .unwrap();
    let barrier = after(&h.tape, &Step::Barrier, published + 1).unwrap();
    let consumed = after(
        &h.tape,
        &Step::Append("strategy_event_consumed".into()),
        published + 1,
    )
    .unwrap();
    assert!(published < barrier && barrier < consumed);
    assert!(h.records.lock().unwrap().iter().any(|record| matches!(
        record,
        WalRecord::StrategyEventConsumed {
            source: StrategyId(0),
            destination: StrategyId(1),
            event_id,
            ..
        } if event_id == "fire-1"
    )));
    let WalRecord::SegmentBase {
        strategy_events, ..
    } = engine.rotation_base(recent_replay_ms())
    else {
        unreachable!()
    };
    assert!(strategy_events.is_empty());
}

struct VisibilityProbe {
    name: &'static str,
    symbol: &'static str,
    visible: Rc<RefCell<Vec<usize>>>,
}

impl Strategy for VisibilityProbe {
    fn name(&self) -> &str {
        self.name
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.into(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if matches!(
            event,
            EngineEvent::Market(_) | EngineEvent::StrategyEvent(_)
        ) {
            let mut events = Vec::new();
            ctx.strategy_events(&mut events);
            self.visible.lock().unwrap().push(events.len());
        }
    }
}

#[tokio::test]
async fn replayed_event_is_visible_to_source_and_destination_but_not_a_third_sleeve() {
    let event = StrategyEvent {
        source: StrategyId(0),
        destination: StrategyId(1),
        kind: "carry_fire".into(),
        event_id: "fire-2".into(),
        payload: vec![2],
    };
    let source = Rc::new(RefCell::new(Vec::new()));
    let destination = Rc::new(RefCell::new(Vec::new()));
    let outsider = Rc::new(RefCell::new(Vec::new()));
    let replayed = vec![
        WalRecord::Names {
            strategies: vec!["carry".into(), "exodus".into(), "maker".into()],
            symbols: vec!["BTCUSDT".into(), "ETHUSDT".into(), "SOLUSDT".into()],
        },
        WalRecord::StrategyEventPublished {
            wall_ts_ms: recent_replay_ms(),
            event,
        },
    ];
    let (mut engine, _) = build(
        allow_all(),
        vec![
            Box::new(VisibilityProbe {
                name: "carry",
                symbol: "BTCUSDT",
                visible: source.clone(),
            }),
            Box::new(VisibilityProbe {
                name: "exodus",
                symbol: "ETHUSDT",
                visible: destination.clone(),
            }),
            Box::new(VisibilityProbe {
                name: "maker",
                symbol: "SOLUSDT",
                visible: outsider.clone(),
            }),
        ],
        &["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        &replayed,
    )
    .await;
    let events = (0..3)
        .map(|symbol| MarketEvent::Quote {
            symbol: SymbolId(symbol),
            quote: Quote::default(),
        })
        .collect();
    engine
        .run(
            &mut ScriptFeed {
                events,
                close_at_end: true,
                admitted: Rc::new(RefCell::new(Vec::new())),
                known: 3,
                admits_wrongly: false,
            },
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(&*source.lock().unwrap(), &[1]);
    assert_eq!(&*destination.lock().unwrap(), &[1, 1]);
    assert_eq!(&*outsider.lock().unwrap(), &[0]);
}
