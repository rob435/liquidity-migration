#![cfg(feature = "bybit")]

use super::*;
use engine_types::numeric::Exact;
use engine_venue::{BybitGateway, BybitOrderFeed, RealmCredentials, VenueRealm};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

#[path = "../../../engine-venue/tests/venue/support/mod.rs"]
mod http_support;
use http_support::{Recorded, TestServer};

const CREATE: &str = "/v5/order/create";
const CANCEL: &str = "/v5/order/cancel";

#[tokio::test]
async fn queued_repricings_reach_bybit_before_any_ack_and_keep_their_results() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        socket.next().await.unwrap().unwrap();
        socket
            .send(Message::text(r#"{"op":"auth","retCode":0}"#))
            .await
            .unwrap();
        let mut requests = Vec::new();
        for _ in 0..10 {
            let message = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let request: Value = serde_json::from_str(&message).unwrap();
            assert_eq!(request["op"], "order.amend");
            requests.push(request);
        }
        for request in requests.into_iter().rev() {
            let rejected = request["args"][0]["orderLinkId"] == "reprice-4";
            socket
                .send(Message::text(
                    json!({ "reqId": request["reqId"], "op": "order.amend",
                "retCode": if rejected { 110001 } else { 0 }, "retMsg": "fixture",
                "data": {}, "retExtInfo": {} })
                    .to_string(),
                ))
                .await
                .unwrap();
        }
        std::future::pending::<()>().await;
    });
    let wire = BybitGateway::for_test_with_trade_transport(
        "http://127.0.0.1:1",
        &format!("ws://{address}"),
        VenueRealm::Mainnet,
        VenueRealm::Mainnet.credentials_for_test("fixture", "fixture"),
        vec!["BTCUSDT".into()],
    );
    let (mut client, mut completions) = crate::venue_runtime::VenueClient::spawn(
        engine_venue::Venue::Bybit(wire),
        engine_types::AuthorityEpoch::new(),
    );
    let ids: Vec<_> = (0..10)
        .map(|index| {
            client
                .dispatch_amend(
                    SymbolId(0),
                    format!("reprice-{index}"),
                    AmendSpec {
                        px: Some(100.0 + index as f64),
                        qty: None,
                        exact_terms: None,
                    },
                    None,
                )
                .unwrap()
        })
        .collect();
    tokio::time::timeout(Duration::from_secs(3), async {
        for (index, expected) in ids.into_iter().enumerate() {
            let crate::venue_runtime::MutationCompletion::Amend {
                command_id, reply, ..
            } = completions.recv().await.unwrap()
            else {
                panic!("wrong completion");
            };
            assert_eq!(command_id, expected);
            if index == 4 {
                assert!(matches!(
                    reply,
                    Err(VenueError::Rejected { code: 110001, .. })
                ));
            } else {
                reply.unwrap();
            }
        }
    })
    .await
    .expect("ten queued reprices must not wait for ten serial round trips");
    server.abort();
}

// Account boot is controlled; create/cancel requests and private frames use Bybit's real adapters.
struct BoundaryVenue {
    account: MockVenue,
    wire: BybitGateway,
}

#[engine_types::async_trait]
impl VenueGateway for BoundaryVenue {
    fn caps(&self) -> VenueCaps {
        self.wire.caps()
    }
    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        self.account.account_identity().await
    }
    async fn send_order(&mut self, request: &OrderRequest) -> Result<OrderAck, VenueError> {
        self.account.sends.lock().unwrap().push(request.clone());
        self.wire.send_order(request).await
    }
    async fn cancel_order(&mut self, symbol: SymbolId, id: &str) -> Result<(), VenueError> {
        self.wire.cancel_order(symbol, id).await
    }
    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.wire.amend_order(symbol, id, spec).await
    }
    fn take_rate_wait_ns(&mut self) -> Option<u64> {
        self.wire.take_rate_wait_ns()
    }
    async fn set_stop(&mut self, symbol: SymbolId, px: f64) -> Result<(), VenueError> {
        self.account.set_stop(symbol, px).await
    }
    async fn set_stop_exact(
        &mut self,
        symbol: SymbolId,
        terms: &engine_types::order_terms::ExactStopTerms,
    ) -> Result<(), VenueError> {
        self.account.set_stop_exact(symbol, terms).await
    }
    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        self.account.account_view().await
    }
    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.account.instrument_rules().await
    }
    async fn instrument_specs(
        &mut self,
    ) -> Result<Vec<(Symbol, engine_types::numeric::ExactInstrumentSpec)>, VenueError> {
        self.account.instrument_specs().await
    }
    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        self.account.working_orders().await
    }
    fn account_recovery_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::AccountRecoveryClient>> {
        self.account.account_recovery_client()
    }
    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        self.account.executions(start_ms, end_ms).await
    }
}

fn gateway(server: &TestServer) -> BybitGateway {
    BybitGateway::for_test(
        &server.base_url(),
        VenueRealm::Demo,
        VenueRealm::Demo.credentials_for_test("boundary-key", "boundary-secret"),
        vec!["BTCUSDT".into()],
    )
}

fn accepted(request: &Recorded, _: usize) -> (u16, String) {
    assert!(matches!(request.path.as_str(), CREATE | CANCEL));
    let id = request.json()["orderLinkId"].as_str().unwrap().to_string();
    (
        200,
        json!({"retCode":0,"retMsg":"OK","result":{"orderId":"venue-order","orderLinkId":id}})
            .to_string(),
    )
}

type BoundaryEngine = Engine<MockWal, MockRisk, BoundaryVenue>;
type Records = Arc<Mutex<Vec<WalRecord>>>;
type Sends = Arc<Mutex<Vec<OrderRequest>>>;

async fn boot(wire: BybitGateway, cancel_on_fill: bool) -> (BoundaryEngine, Records, Sends) {
    let (wal, records) = MockWal::new(tape());
    let (mut account, _) = MockVenue::new(tape(), &["BTCUSDT"]);
    let sends = account.sends.clone();
    let mut spec = shared_sleeves::spec();
    spec.tick_size = Some(d("0.5"));
    spec.qty_step = Some(d("0.001"));
    spec.market_qty_step = Some(d("0.001"));
    spec.min_qty = Some(d("0.001"));
    spec.market_min_qty = Some(d("0.001"));
    account.exact_specs = Some(vec![("BTCUSDT".into(), spec)]);
    let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);
    let (risk, _) = MockRisk::with(allow_all());
    let engine = Engine::boot(
        &settings(),
        "boundary",
        wal,
        risk,
        BoundaryVenue { account, wire },
        vec![Box::new(CancelOnFill {
            buyer,
            enabled: cancel_on_fill,
            cancelled: false,
        })],
        &[],
    )
    .await
    .unwrap();
    (engine, records, sends)
}

fn d(value: &str) -> Exact {
    Exact::parse_decimal(value).unwrap()
}

struct CancelOnFill {
    buyer: Buyer,
    enabled: bool,
    cancelled: bool,
}
impl Strategy for CancelOnFill {
    fn name(&self) -> &str {
        self.buyer.name()
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        self.buyer.subscriptions()
    }
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        self.buyer.on_event(event, ctx);
        if let EngineEvent::Order(OrderUpdate::Fill {
            symbol,
            client_order_id,
            ..
        }) = event
        {
            if self.enabled && !self.cancelled {
                self.cancelled = true;
                ctx.cancel(*symbol, client_order_id);
            }
        }
    }
}

async fn submit(engine: &mut BoundaryEngine) {
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
}

struct PrivateSocket {
    _io: crate::test_io::IoProgress,
    feed: BybitOrderFeed,
    frames: mpsc::UnboundedSender<String>,
    server: tokio::task::JoinHandle<()>,
}
impl Drop for PrivateSocket {
    fn drop(&mut self) {
        self.server.abort();
    }
}

async fn private_socket() -> PrivateSocket {
    let io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let (frames, mut rx) = mpsc::unbounded_channel::<String>();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(socket).await.unwrap();
        for operation in ["auth", "subscribe"] {
            let frame = socket.next().await.unwrap().unwrap();
            let request: Value = serde_json::from_str(frame.to_text().unwrap()).unwrap();
            assert_eq!(request["op"], operation);
            socket
                .send(Message::text(
                    json!({"op":operation,"success":true}).to_string(),
                ))
                .await
                .unwrap();
        }
        loop {
            tokio::select! {
                frame = rx.recv() => match frame {
                    Some(frame) => if socket.send(Message::text(frame)).await.is_err() { break; },
                    None => break,
                },
                frame = socket.next() => match frame {
                    Some(Ok(Message::Ping(payload))) => { let _ = socket.send(Message::Pong(payload)).await; },
                    Some(Ok(_)) => {},
                    _ => break,
                },
            }
        }
    });
    let mut feed = BybitOrderFeed::for_test(
        &url,
        VenueRealm::Demo.credentials_for_test("boundary-key", "boundary-secret"),
        vec!["BTCUSDT".into()],
    );
    assert!(matches!(
        feed.next_update().await.unwrap(),
        OrderUpdate::StreamReset { .. }
    ));
    PrivateSocket {
        _io: io,
        feed,
        frames,
        server,
    }
}

fn execution(id: &str, exec: &str, quantity: &str) -> String {
    let mut frame: Value =
        serde_json::from_str(include_str!("../../tests/fixtures/bybit/execution.json")).unwrap();
    frame["data"][0]["orderLinkId"] = id.into();
    frame["data"][0]["execId"] = exec.into();
    frame["data"][0]["execQty"] = quantity.into();
    frame["data"][0]["execTime"] = clock::wall_ms().to_string().into();
    frame.to_string()
}

fn cancelled(id: &str) -> String {
    json!({"topic":"order","data":[{"orderLinkId":id,"orderId":"venue-order","orderStatus":"Cancelled"}]}).to_string()
}

async fn until(mut condition: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !condition() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("boundary event never arrived");
}

fn fill_count(records: &Records) -> usize {
    records
        .lock()
        .unwrap()
        .iter()
        .filter(|record| {
            matches!(
                record,
                WalRecord::OrderUpdate {
                    update: OrderUpdate::Fill { .. },
                    ..
                }
            )
        })
        .count()
}

fn assert_accounted(records: &Records, id: &str, expected: &str) {
    let bytes = serde_json::to_vec(&*records.lock().unwrap()).unwrap();
    let replay: Vec<WalRecord> = serde_json::from_slice(&bytes).unwrap();
    let orders = crate::inflight::LedgerOfOrders::try_from_records(&replay).unwrap();
    assert_eq!(
        orders.orders[id].fill_quantity,
        engine_types::wal::OrderFillQuantity::Exact {
            quantity: d(expected)
        }
    );
    let attribution = crate::attribution::Attribution::try_from_records(&replay).unwrap();
    assert_eq!(
        attribution.signed_exact(StrategyId(0), SymbolId(0)),
        d(expected)
    );
}

#[tokio::test(start_paused = true)]
async fn http_timeout_keeps_exposure_until_the_late_bybit_execution_settles_once() {
    crate::test_clock::with_engine_clock(async {
        let server = TestServer::start_with_delay(accepted, |request, _| {
            if request.path == CREATE {
                Duration::from_secs(30)
            } else {
                Duration::ZERO
            }
        })
        .await;
        let (mut engine, records, _) = boot(gateway(&server), false).await;
        {
            let mut market = ScriptFeed::quotes(SymbolId(0), 1, false);
            let mut private = ScriptOrderFeed::empty();
            let stop = async {
                while !records.lock().unwrap().iter().any(|record| {
                    matches!(record,
                WalRecord::Note { text, .. } if text.contains("still counted as in flight"))
                }) {
                    tokio::task::yield_now().await;
                }
            };
            let submission = engine.run(&mut market, &mut private, stop);
            let expire_http = async {
                until(|| !server.to_path(CREATE).is_empty()).await;
                tokio::time::advance(Duration::from_secs(10)).await;
            };
            let (result, ()) = tokio::join!(submission, expire_http);
            result.unwrap();
        }
        let id = server.only(CREATE).json()["orderLinkId"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(engine.in_flight_ids(), [id.as_str()]);
        assert!(records
            .lock()
            .unwrap()
            .iter()
            .any(|record| matches!(record, WalRecord::Note { text, .. }
        if text.contains("did not complete") && text.contains("still counted as in flight"))));
        let mut socket = private_socket().await;
        let late_fill = async {
            until(|| !server.to_path(CANCEL).is_empty()).await;
            tokio::time::advance(Duration::from_millis(100)).await;
            socket.frames.send(execution(&id, "late", "0.01")).unwrap();
            socket.frames.send(execution(&id, "late", "0.01")).unwrap();
            socket.frames.send(cancelled(&id)).unwrap();
            until(|| {
                records.lock().unwrap().iter().any(|record| {
                    matches!(
                        record,
                        WalRecord::OrderUpdate {
                            update: OrderUpdate::Cancelled { .. },
                            ..
                        }
                    )
                })
            })
            .await;
        };
        engine
            .run(
                &mut ScriptFeed::quotes(SymbolId(0), 0, false),
                &mut socket.feed,
                late_fill,
            )
            .await
            .unwrap();
        assert_eq!(fill_count(&records), 1);
        assert!(engine.in_flight_ids().is_empty());
        assert_accounted(&records, &id, "0.01");
        assert_eq!(server.to_path(CREATE).len(), 1);
        assert_eq!(server.to_path(CANCEL).len(), 1);
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn bybit_partial_fill_racing_cancel_preserves_each_execution_in_both_orders() {
    crate::test_clock::with_engine_clock(async {
        for cancel_first in [true, false] {
            let server = TestServer::start(accepted).await;
            let (mut engine, records, _) = boot(gateway(&server), true).await;
            submit(&mut engine).await;
            let id = server.only(CREATE).json()["orderLinkId"]
                .as_str()
                .unwrap()
                .to_owned();
            let mut socket = private_socket().await;
            socket
                .frames
                .send(execution(&id, "partial-1", "0.004"))
                .unwrap();
            let choreography = async {
                until(|| !server.to_path(CANCEL).is_empty()).await;
                let frames = [cancelled(&id), execution(&id, "partial-2", "0.002")];
                for index in if cancel_first { [0, 1] } else { [1, 0] } {
                    socket.frames.send(frames[index].clone()).unwrap();
                }
                until(|| {
                    fill_count(&records) == 2
                        && records.lock().unwrap().iter().any(|record| {
                            matches!(
                                record,
                                WalRecord::OrderUpdate {
                                    update: OrderUpdate::Cancelled { .. },
                                    ..
                                }
                            )
                        })
                })
                .await;
            };
            engine
                .run(
                    &mut ScriptFeed::quotes(SymbolId(0), 0, false),
                    &mut socket.feed,
                    choreography,
                )
                .await
                .unwrap();
            assert!(engine.in_flight_ids().is_empty());
            assert_accounted(&records, &id, "0.006");
            assert_eq!(server.to_path(CREATE).len(), 1);
            assert_eq!(server.only(CANCEL).json()["orderLinkId"], id);
        }
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn bybit_rate_and_clock_rejections_reach_core_without_becoming_ambiguous_sends() {
    crate::test_clock::with_engine_clock(async {

    for code in [10006, 10002] {
        let server = TestServer::start(move |request, _| {
            assert_eq!(request.path, CREATE);
            let timestamp: i64 = request.header("x-bapi-timestamp").unwrap().parse().unwrap();
            let recv_window: i64 = request
                .header("x-bapi-recv-window")
                .unwrap()
                .parse()
                .unwrap();
            assert_eq!(recv_window, 5000);
            assert!((clock::wall_ms() - timestamp).abs() < recv_window);
            if code == 10002 {
                let server_time = clock::wall_ms() + 60_000;
                assert!(timestamp < server_time - recv_window);
            }
            (
                200,
                json!({"retCode":code,"retMsg":"scripted quota or receive-window refusal"})
                    .to_string(),
            )
        })
        .await;
        let (mut engine, records, _) = boot(gateway(&server), false).await;
        submit(&mut engine).await;
        assert!(engine.in_flight_ids().is_empty());
        assert!(records.lock().unwrap().iter().any(|record| matches!(record,
            WalRecord::OrderUpdate { update: OrderUpdate::Reject { code: found, .. }, .. } if *found == code)));
        assert_eq!(server.to_path(CREATE).len(), 1);
        assert_eq!(
            engine
                .ledger()
                .quantiles(crate::ledger::Segment::QuotaHold)
                .count,
            1
        );
    }

    }).await;
}

#[tokio::test(start_paused = true)]
async fn bybit_position_topics_do_not_duplicate_execution_accounting() {
    crate::test_clock::with_engine_clock(async {

    let mut socket = private_socket().await;
    socket
        .frames
        .send(include_str!("../../tests/fixtures/bybit/position.json").into())
        .unwrap();
    socket
        .frames
        .send(execution("position-boundary", "only-execution", "0.004"))
        .unwrap();
    let update = tokio::time::timeout(Duration::from_secs(3), socket.feed.next_update())
        .await
        .unwrap()
        .unwrap();
    assert!(
        matches!(update, OrderUpdate::Fill { ref exec_id, qty: 0.004, .. } if exec_id == "only-execution")
    );

    }).await;
}

#[tokio::test(start_paused = true)]
async fn bybit_quota_wait_is_recorded_once_by_the_engine() {
    crate::test_clock::with_engine_clock(async {
        let server = TestServer::start(accepted).await;
        let (mut warm_engine, warm_records, _) = boot(gateway(&server), false).await;
        submit(&mut warm_engine).await;
        let request = warm_records
            .lock()
            .unwrap()
            .iter()
            .find_map(|record| match record {
                WalRecord::OrderSent { request, .. } => Some(request.clone()),
                _ => None,
            })
            .unwrap();
        let mut wire = gateway(&server);
        for index in 0..10 {
            let mut warm = request.clone();
            warm.client_order_id = format!("warm-{index}");
            wire.send_order(&warm).await.unwrap();
        }
        let (mut engine, records, sends) = boot(wire, false).await;
        let release_quota = async {
            until(|| !sends.lock().unwrap().is_empty()).await;
            tokio::time::advance(Duration::from_millis(1001)).await;
        };
        tokio::join!(submit(&mut engine), release_quota);
        let quota = engine.ledger().quantiles(crate::ledger::Segment::QuotaHold);
        assert_eq!(quota.count, 1);
        assert!(
            quota.max_ns >= 100_000_000,
            "full create quota was not observed: {quota:?}"
        );
        let rows = records.lock().unwrap();
        let waits: Vec<_> = rows
            .iter()
            .filter_map(|row| match row {
                WalRecord::VenueTiming {
                    operation,
                    rate_wait_ns,
                    ..
                } if operation == "place" => *rate_wait_ns,
                _ => None,
            })
            .collect();
        assert_eq!(waits.len(), 1);
        assert!(waits[0] >= 100_000_000);
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn working_reprices_gain_exact_terms_before_the_durable_amend_and_wire() {
    crate::test_clock::with_engine_clock(async {
        let (wal, records) = MockWal::new(tape());
        let (mut venue, _) = MockVenue::new(tape(), &["BTCUSDT"]);
        let mut spec = shared_sleeves::spec();
        spec.tick_size = Some(d("0.5"));
        spec.qty_step = Some(d("0.001"));
        spec.market_qty_step = Some(d("0.001"));
        spec.min_qty = Some(d("0.001"));
        spec.market_min_qty = Some(d("0.001"));
        venue.exact_specs = Some(vec![("BTCUSDT".into(), spec)]);
        let amends = venue.amends.clone();
        let (buyer, _) = Buyer::working(
            "BTCUSDT",
            1,
            0.01,
            WorkPolicy {
                reprice_ms: 1,
                ..Default::default()
            },
        );
        let (risk, _) = MockRisk::with(allow_all());
        let mut config = settings();
        config.group_flush_ms = 5;
        let mut engine = Engine::boot(
            &config,
            "working-exact",
            wal,
            risk,
            venue,
            vec![Box::new(buyer)],
            &[],
        )
        .await
        .unwrap();
        engine
            .run(
                &mut ScriptFeed::wide_quotes(SymbolId(0), 2, false),
                &mut ScriptOrderFeed::empty(),
                async {
                    until(|| {
                        records.lock().unwrap().iter().any(|row| {
                            matches!(
                                row,
                                WalRecord::OrderUpdate {
                                    update: OrderUpdate::Ack(_),
                                    ..
                                }
                            )
                        })
                    })
                    .await;
                    tokio::time::advance(Duration::from_millis(5)).await;
                    until(|| !amends.lock().unwrap().is_empty()).await;
                },
            )
            .await
            .unwrap();
        let sent = amends.lock().unwrap();
        assert_eq!(sent.len(), 1);
        let spec = &sent[0].2;
        spec.exact_terms
            .as_ref()
            .unwrap()
            .validate_projection(spec)
            .unwrap();
        assert!(spec.qty.is_none());
        assert!(records.lock().unwrap().iter().any(
            |row| matches!(row, WalRecord::AmendSent { spec: durable, .. }
        if durable == spec)
        ));
    })
    .await;
}
