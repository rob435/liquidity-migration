use super::*;
use engine_types::numeric::{AssetId, Exact};
use engine_types::portfolio_control::{
    PortfolioExit, PortfolioOffsetSettlement, PortfolioOffsetSlice,
};
use engine_types::{Feed, MarketFeed};

#[derive(Default)]
struct PortfolioFeed {
    retired: Vec<Subscription>,
}
impl MarketFeed for PortfolioFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, engine_types::FeedError> {
        std::future::pending().await
    }
    fn admit(&mut self, symbol: &str, _feed: Feed) -> Option<SymbolId> {
        (symbol == "BTCUSDT").then_some(SymbolId(0))
    }
    fn retire(&mut self, symbol: &str, feed: Feed) -> bool {
        self.retired.push(Subscription {
            symbol: symbol.into(),
            feed,
        });
        true
    }
}
fn routes() -> Vec<Subscription> {
    [Feed::Quote, Feed::Depth]
        .into_iter()
        .map(|feed| Subscription {
            symbol: "BTCUSDT".into(),
            feed,
        })
        .collect()
}

#[tokio::test(start_paused = true)]
async fn inactive_offset_inventory_retains_quote_depth_through_restart_settlement_and_pending_exit()
{
    let (engine, _) = crate::tests::portfolio_route_test_fixture(None).await;
    assert!(!engine.host.callbacks.is_active(StrategyId(0)));
    assert!(!engine.host.callbacks.is_active(StrategyId(1)));
    assert!(engine.books.account.positions.is_empty());
    assert_eq!(engine.books.attribution.snapshot().positions.len(), 2);
    for route in routes() {
        assert!(
            engine.subscriptions().contains(&route),
            "inactive netzero inventory lost engine route {route:?}"
        );
    }
    let base: WalRecord = serde_json::from_slice(
        &serde_json::to_vec(&engine.rotation_base(clock::wall_ms())).unwrap(),
    )
    .unwrap();
    let (mut engine, _) = crate::tests::portfolio_route_test_fixture(Some(vec![base])).await;
    let mut feed = PortfolioFeed::default();
    engine.maintain_signal_routes(&mut feed).unwrap();
    assert!(feed.retired.is_empty());
    for route in routes() {
        assert!(engine.subscriptions().contains(&route));
    }
    let exit = WalRecord::PortfolioExitChanged {
        state: PortfolioExit {
            id: 1,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            position_side: Side::Buy,
            target_remaining: Exact::zero(),
            trigger_price: Some(Exact::parse_decimal("90").unwrap()),
            started_ms: 1,
            attempt: 0,
            order_id: None,
        },
    };
    engine.wal.append(&exit).unwrap();
    engine.wal.barrier().unwrap();
    engine.portfolio_controls.apply(&exit).unwrap();
    engine
        .start_portfolio_emergency(
            SymbolId(0),
            Exact::parse_decimal("100").unwrap(),
            engine_types::portfolio_control::PortfolioEmergencyReason::ExitUnavailable,
        )
        .unwrap();
    let mut emergency = engine.portfolio_controls.emergencies[&SymbolId(0)].clone();
    emergency.phase = engine_types::portfolio_control::PortfolioEmergencyPhase::CloseNet;
    engine
        .append_portfolio_control(WalRecord::PortfolioEmergencyChanged {
            state: emergency.clone(),
        })
        .unwrap();
    emergency.phase = engine_types::portfolio_control::PortfolioEmergencyPhase::SettleOffsets;
    engine
        .append_portfolio_control(WalRecord::PortfolioEmergencyChanged {
            state: emergency.clone(),
        })
        .unwrap();
    let settlement = PortfolioOffsetSettlement {
        emergency_id: emergency.id,
        symbol: SymbolId(0),
        price: Exact::parse_decimal("100").unwrap(),
        settled_ms: 2,
        slices: vec![
            PortfolioOffsetSlice {
                strategy: StrategyId(0),
                signed_quantity: Exact::parse_decimal("-1").unwrap(),
                settlement_asset: AssetId::Named("USDT".into()),
            },
            PortfolioOffsetSlice {
                strategy: StrategyId(1),
                signed_quantity: Exact::parse_decimal("1").unwrap(),
                settlement_asset: AssetId::Named("USDT".into()),
            },
        ],
    };
    engine
        .append_portfolio_control(WalRecord::PortfolioOffsetSettled { settlement })
        .unwrap();
    engine
        .append_portfolio_control(WalRecord::PortfolioEmergencyCompleted {
            id: emergency.id,
            symbol: SymbolId(0),
        })
        .unwrap();
    engine.wal.barrier().unwrap();
    engine.maintain_signal_routes(&mut feed).unwrap();
    assert!(
        feed.retired.is_empty(),
        "pending exit still leases engine routes after balanced settlement"
    );
    let done = WalRecord::PortfolioExitCompleted {
        id: 1,
        strategy: StrategyId(0),
        symbol: SymbolId(0),
    };
    engine.wal.append(&done).unwrap();
    engine.wal.barrier().unwrap();
    engine.portfolio_controls.apply(&done).unwrap();
    engine.maintain_signal_routes(&mut feed).unwrap();
    assert_eq!(feed.retired, routes());
    assert!(engine.subscriptions().is_empty());
    assert_eq!(engine.books.market.table.get("BTCUSDT"), Some(SymbolId(0)));
    let (engine, _) = crate::tests::portfolio_route_test_fixture(Some(vec![
        engine.rotation_base(clock::wall_ms())
    ]))
    .await;
    assert!(
        engine.subscriptions().is_empty(),
        "settled historical symbols must not recreate market demand"
    );
}

#[derive(Clone, Default)]
struct ScriptedPortfolioFeed {
    admissions: std::collections::VecDeque<Option<SymbolId>>,
    attempts: Vec<Subscription>,
    retired: Vec<Subscription>,
}
impl MarketFeed for ScriptedPortfolioFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, engine_types::FeedError> {
        std::future::pending().await
    }
    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        self.attempts.push(Subscription {
            symbol: symbol.into(),
            feed,
        });
        self.admissions.pop_front().unwrap_or(Some(SymbolId(0)))
    }
    fn retire(&mut self, symbol: &str, feed: Feed) -> bool {
        self.retired.push(Subscription {
            symbol: symbol.into(),
            feed,
        });
        true
    }
}

#[tokio::test(start_paused = true)]
async fn membership_sets_match_original_admission_retry_error_and_retirement_order() {
    let (mut actual, _) = crate::tests::portfolio_route_test_fixture(None).await;
    let (mut expected, _) = crate::tests::portfolio_route_test_fixture(None).await;
    for prefix in [0, 270] {
        for retained in 0..4 {
            for answer in [None, Some(SymbolId(0)), Some(SymbolId(7))] {
                let mut subscriptions: Vec<_> = (0..prefix)
                    .map(|index| Subscription {
                        symbol: format!("S{index:03}USDT"),
                        feed: Feed::Quote,
                    })
                    .collect();
                subscriptions.extend(
                    routes()
                        .into_iter()
                        .enumerate()
                        .filter_map(|(index, route)| {
                            (retained & (1 << index) != 0).then_some(route)
                        }),
                );
                for route in &subscriptions {
                    actual.books.market.table.intern(&route.symbol);
                    expected.books.market.table.intern(&route.symbol);
                }
                actual.subscriptions = subscriptions.clone();
                expected.subscriptions = subscriptions;
                actual.portfolio_subscriptions = routes();
                actual.portfolio_subscriptions.push(Subscription {
                    symbol: "BTCUSDT".into(),
                    feed: Feed::Trades,
                });
                expected.portfolio_subscriptions = actual.portfolio_subscriptions.clone();
                let mut feed = ScriptedPortfolioFeed {
                    admissions: if prefix == 0 {
                        [answer, Some(SymbolId(0))].into()
                    } else {
                        [Some(SymbolId(0)), answer].into()
                    },
                    ..Default::default()
                };
                let mut reference = feed.clone();
                for _ in 0..3 {
                    let left = actual
                        .maintain_portfolio_routes(&mut feed)
                        .map_err(|error| error.to_string());
                    let right = expected
                        .reference_maintain_portfolio_routes(&mut reference)
                        .map_err(|error| error.to_string());
                    assert_eq!(left, right);
                    assert_eq!(actual.subscriptions, expected.subscriptions);
                    assert_eq!(
                        actual.portfolio_subscriptions,
                        expected.portfolio_subscriptions
                    );
                    assert_eq!(feed.attempts, reference.attempts);
                    assert_eq!(feed.retired, reference.retired);
                    assert_eq!(feed.admissions, reference.admissions);
                }
            }
        }
    }
}
