//! The same gateway lifecycle runs through each enabled adapter's real HTTP client.
use crate::support::{Recorded, TestServer};
use engine_types::{
    AmendSpec, OrderKind, OrderRequest, Side, StrategyId, SymbolId, TimeInForce, VenueError,
    VenueGateway,
};
use engine_venue::{RealmCredentials, Venue, VenueName};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::Duration;

struct AbortOnDrop(tokio::task::JoinHandle<()>);
impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

const CLIENT: &str = "eng-1700000000000-1";
#[cfg(feature = "hyperliquid")]
const HL_KEY: &str = "0x0123456789012345678901234567890123456789012345678901234567890123";
const HL_ACCOUNT: &str = "0x0000000000000000000000000000000000000001";
#[cfg(feature = "lighter")]
const LIGHTER_KEY: &str =
    "0101010101010101010101010101010101010101010101010101010101010101010101010101010f";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Scenario {
    Ack,
    Reject,
    ClockSkew,
    RateLimit,
    PartialCancel,
    Timeout,
    LateFill,
}

fn request() -> OrderRequest {
    OrderRequest {
        client_order_id: CLIENT.into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        kind: OrderKind::Limit {
            px: 95_000.0,
            tif: TimeInForce::Gtc,
        },
        stop: None,
        reduce_only: false,
        close_position: false,
        exact_terms: None,
        sleeve_effect: None,
    }
}

fn build(name: VenueName, server: &TestServer) -> Venue {
    let symbols = vec!["BTCUSDT".into()];
    let url = server.base_url();
    #[allow(unreachable_patterns)]
    match name {
        #[cfg(feature = "bybit")]
        VenueName::BybitDemo => Venue::Bybit(engine_venue::BybitGateway::for_test(
            &url,
            engine_venue::VenueRealm::Demo,
            engine_venue::VenueRealm::Demo.credentials_for_test("key", "secret"),
            symbols,
        )),
        #[cfg(feature = "binance")]
        VenueName::BinanceTestnet => Venue::Binance(engine_venue::BinanceGateway::for_test(
            &url,
            engine_venue::BinanceRealm::Testnet,
            engine_venue::BinanceRealm::Testnet.credentials_for_test("key", "secret"),
            symbols,
        )),
        #[cfg(feature = "hyperliquid")]
        VenueName::HyperliquidTestnet => Venue::Hyperliquid(
            engine_venue::HyperliquidGateway::for_test(
                &url,
                engine_venue::HyperliquidRealm::Testnet,
                engine_venue::HyperliquidRealm::Testnet.credentials_for_test(HL_ACCOUNT, HL_KEY),
                symbols,
            )
            .unwrap(),
        ),
        #[cfg(feature = "lighter")]
        VenueName::LighterTestnet => Venue::Lighter(
            engine_venue::LighterGateway::for_test(
                &url,
                engine_venue::LighterRealm::Testnet,
                engine_venue::LighterRealm::Testnet.credentials_for_test("42:3", LIGHTER_KEY),
                symbols,
            )
            .unwrap(),
        ),
        #[cfg(feature = "mexc")]
        VenueName::MexcMainnet => Venue::Mexc(engine_venue::MexcGateway::for_test(
            &url,
            engine_venue::MexcRealm::Mainnet,
            engine_venue::MexcRealm::Mainnet.credentials_for_test("key", "secret"),
            symbols,
        )),
        #[cfg(feature = "variational")]
        VenueName::VariationalMainnet => {
            Venue::Variational(engine_venue::VariationalGateway::for_test(
                &url,
                engine_venue::VariationalRealm::Mainnet,
                symbols,
            ))
        }
        other => panic!("conformance fixture missing for {other}"),
    }
}

fn query<'a>(r: &'a Recorded, key: &str) -> Option<&'a str> {
    r.query.split('&').find_map(|pair| {
        pair.split_once('=')
            .filter(|(k, _)| *k == key)
            .map(|(_, v)| v)
    })
}
fn bybit(body: Value) -> (u16, String) {
    (
        200,
        json!({"retCode":0,"retMsg":"OK","result":body,"time":1700000000000_i64}).to_string(),
    )
}
/// One contract, at the size that makes a contract count and a coin count
/// different numbers: 100 contracts is the 0.01 BTC this file orders.
fn mexc_contract_detail() -> Value {
    json!({"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":1000,"apiAllowed":true}]})
}
fn mutation(name: VenueName, r: &Recorded) -> bool {
    match name.venue() {
        "bybit" => r.path.starts_with("/v5/order/") && r.method == "POST",
        "binance" => r.path == "/fapi/v1/order" && r.method != "GET",
        "hyperliquid" => r.path == "/exchange",
        "lighter" => r.path == "/api/v1/sendTx",
        "mexc" => r.path.starts_with("/api/v1/private/order/") && r.method == "POST",
        "variational" => false,
        other => panic!("unknown adapter {other}"),
    }
}
fn answer(
    name: VenueName,
    scenario: Scenario,
    cloid: &Mutex<String>,
    r: &Recorded,
) -> (u16, String) {
    if mutation(name, r) {
        if scenario == Scenario::RateLimit {
            return (429, json!({"code":-1003,"msg":"rate limit"}).to_string());
        }
        if matches!(scenario, Scenario::Reject | Scenario::ClockSkew) {
            let message = if scenario == Scenario::ClockSkew {
                "timestamp outside signing window"
            } else {
                "order rejected"
            };
            return match name.venue() {
                "bybit" => (200, json!({"retCode":if scenario == Scenario::ClockSkew {10002} else {110007},"retMsg":message,"result":{}}).to_string()),
                "binance" => (400, json!({"code":if scenario == Scenario::ClockSkew {-1021} else {-2010},"msg":message}).to_string()),
                "hyperliquid" => (200, json!({"status":"err","response":message}).to_string()),
                "lighter" => (200, json!({"code":400,"message":message}).to_string()),
                // 600 is a real refusal ("Parameter error"); 510 is MEXC's rate
                // limit and reads as a transport failure, which is the
                // RateLimit scenario above.
                "mexc" => (200, json!({"success":false,"code":600,"message":message}).to_string()),
                _ => unreachable!(),
            };
        }
    }
    let partial = scenario == Scenario::PartialCancel;
    let filled = scenario == Scenario::LateFill;
    let terminal = partial || filled;
    let filled_qty = if filled {
        "0.01"
    } else if partial {
        "0.004"
    } else {
        "0"
    };
    match name.venue() {
        "bybit" => bybit(match r.path.as_str() {
            "/v5/order/realtime" => json!({"category":"linear","list":[{"symbol":"BTCUSDT","orderId":"41","orderLinkId":CLIENT,"side":"Buy","orderType":"Limit","orderStatus":if filled {"Filled"} else if partial {"Cancelled"} else {"New"},"price":"95000","qty":"0.01","leavesQty":if partial {"0.006"} else {"0.01"},"cumExecQty":filled_qty,"reduceOnly":false}],"nextPageCursor":""}),
            _ => json!({"orderId":"41","orderLinkId":CLIENT}),
        }),
        "binance" => (200, match r.path.as_str() {
            "/fapi/v1/exchangeInfo" => json!({"symbols":[{"symbol":"BTCUSDT","status":"TRADING","contractType":"PERPETUAL","quoteAsset":"USDT","marginAsset":"USDT","filters":[{"filterType":"PRICE_FILTER","tickSize":"0.1"},{"filterType":"LOT_SIZE","minQty":"0.001","maxQty":"100","stepSize":"0.001"},{"filterType":"MARKET_LOT_SIZE","minQty":"0.001","maxQty":"100","stepSize":"0.001"},{"filterType":"MIN_NOTIONAL","notional":"5"}]}]}),
            "/fapi/v1/listenKey" => json!({"listenKey":"fixture"}),
            "/fapi/v1/openOrders" => json!([{"symbol":"BTCUSDT","clientOrderId":CLIENT,"orderId":41,"side":"BUY","type":"LIMIT","status":if partial {"PARTIALLY_FILLED"} else {"NEW"},"price":"95000","origQty":"0.01","executedQty":filled_qty,"reduceOnly":false}]),
            _ => json!({"clientOrderId":CLIENT,"orderId":41,"symbol":"BTCUSDT","side":"BUY","type":"LIMIT","status":if filled {"FILLED"} else if partial || r.method == "DELETE" {"CANCELED"} else {"NEW"},"price":"95000","origQty":"0.01","executedQty":filled_qty}),
        }.to_string()),
        "hyperliquid" => {
            let body=r.json();
            if r.path == "/exchange" {
                if let Some(c)=body["action"]["orders"][0]["c"].as_str() { *cloid.lock().unwrap()=c.into(); }
                return (200, json!({"status":"ok","response":{"type":"order","data":{"statuses":[{"resting":{"oid":41}}]}}}).to_string());
            }
            (200, match body["type"].as_str().unwrap() {
                "orderStatus" => json!({"status":"order","order":{"status":if filled {"filled"} else if partial {"canceled"} else {"open"},"order":{"coin":"BTC","cloid":body["oid"],"oid":41,"origSz":"0.01","sz":if filled {"0"} else if partial {"0.006"} else {"0.01"}}}}),
                "meta" => json!({"universe":[{"name":"BTC","szDecimals":5,"maxLeverage":40}]}),
                "frontendOpenOrders" => json!([{"coin":"BTC","side":"B","sz":if partial {"0.006"} else {"0.01"},"origSz":"0.01","limitPx":"95000","tif":"Gtc","oid":41,"cloid":*cloid.lock().unwrap(),"reduceOnly":false}]),
                other => panic!("unexpected Hyperliquid request {other}"),
            }.to_string())
        },
        "lighter" => (200, match r.path.as_str() {
            "/api/v1/orderBookDetails" => json!({"code":200,"order_book_details":[{"symbol":"BTC","market_id":0,"status":"active","supported_size_decimals":5,"supported_price_decimals":1,"min_base_amount":"0.0001","min_quote_amount":"10"}]}),
            "/api/v1/nextNonce" => json!({"code":200,"nonce":7}),
            "/api/v1/accountOrders" => json!({"code":200,"orders":[{"order_id":"41","client_order_index":query(r,"client_order_indexes").unwrap().parse::<u64>().unwrap(),"market_index":0,"owner_account_index":42,"filled_base_amount":filled_qty,"status":if filled {"filled"} else if partial {"canceled"} else {"open"}}]}),
            "/api/v1/accountActiveOrders" => json!({"code":200,"orders":[]}),
            "/api/v1/sendTx" => json!({"code":200,"tx_hash":"41","predicted_execution_time_ms":5}),
            other => panic!("unexpected Lighter request {other}"),
        }.to_string()),
        "mexc" => (200, match r.path.as_str() {
            path if path.starts_with("/api/v1/private/order/external/") => json!({"success":true,"code":0,"data":{"symbol":"BTC_USDT","externalOid":CLIENT,"orderId":"41","state":if filled {3} else if terminal {4} else {2},"dealVol":if filled {100} else if partial {40} else {0}}}),
            "/api/v1/contract/detail" => mexc_contract_detail(),
            _ => json!({"success":true,"code":0,"data":"41"}),
        }.to_string()),
        "variational" => (200, json!({"listings":[]}).to_string()),
        other => panic!("unknown adapter {other}"),
    }
}

fn assert_request_shape(name: VenueName, r: &Recorded) {
    assert_eq!(r.method, "POST");
    match name.venue() {
        "bybit" => {
            let b = r.json();
            assert_eq!(b["symbol"], "BTCUSDT");
            assert_eq!(b["qty"], "0.01");
            assert_eq!(b["price"], "95000");
            assert_eq!(b["orderLinkId"], CLIENT);
            assert_eq!(r.header("x-bapi-api-key"), Some("key"));
            assert!(!r.header("x-bapi-sign").unwrap().is_empty());
        }
        "binance" => {
            assert_eq!(query(r, "symbol"), Some("BTCUSDT"));
            assert_eq!(query(r, "quantity"), Some("0.01"));
            assert_eq!(query(r, "newClientOrderId"), Some(CLIENT));
            assert_eq!(r.header("x-mbx-apikey"), Some("key"));
            assert!(query(r, "signature").is_some());
        }
        "hyperliquid" => {
            let b = r.json();
            let o = &b["action"]["orders"][0];
            assert_eq!(o["a"], 0);
            assert_eq!(o["s"], "0.01");
            assert_eq!(o["p"], "95000");
            assert!(o["c"].as_str().unwrap().starts_with("0x"));
            assert!(b["signature"]["r"].as_str().unwrap().starts_with("0x"));
        }
        "lighter" => {
            assert_eq!(
                r.header("content-type"),
                Some("application/x-www-form-urlencoded")
            );
            assert!(r.body.contains("tx_type=14"));
            assert!(r.body.contains("tx_info="));
        }
        "mexc" => {
            let b = r.json();
            assert_eq!(b["symbol"], "BTC_USDT");
            assert_eq!(b["vol"], 100);
            assert_eq!(b["externalOid"], CLIENT);
            assert!(r.header("signature").is_some());
        }
        _ => panic!("read-only venue sent a mutation"),
    }
}

async fn lifecycle(name: VenueName) {
    let io_progress = tokio::spawn(async {
        loop {
            tokio::task::yield_now().await;
        }
    });
    let _io_progress = AbortOnDrop(io_progress);
    let scenario = Arc::new(Mutex::new(Scenario::Ack));
    let selected = scenario.clone();
    let cloid = Mutex::new(String::new());
    let delayed = scenario.clone();
    let server = TestServer::start_with_delay(
        move |r, _| answer(name, *selected.lock().unwrap(), &cloid, r),
        move |r, _| {
            if mutation(name, r) && *delayed.lock().unwrap() == Scenario::Timeout {
                Duration::from_secs(20)
            } else {
                Duration::ZERO
            }
        },
    )
    .await;
    let mut venue = build(name, &server);
    if name.venue() == "variational" {
        assert!(matches!(
            venue.send_order(&request()).await,
            Err(VenueError::BadRequest(message)) if message.contains("trading API")
        ));
        assert!(matches!(
            venue.cancel_order(SymbolId(0), CLIENT).await,
            Err(VenueError::BadRequest(message)) if message.contains("trading API")
        ));
        assert!(matches!(
            venue
                .amend_order(
                    SymbolId(0),
                    CLIENT,
                    AmendSpec {
                        px: Some(94_999.0),
                        qty: Some(0.01),
                        exact_terms: None
                    }
                )
                .await,
            Err(VenueError::BadRequest(message)) if message.contains("trading API")
        ));
        assert!(
            server.requests().is_empty(),
            "read-only mutations must not send HTTP"
        );
        #[cfg(feature = "variational")]
        #[allow(irrefutable_let_patterns)]
        if let Venue::Variational(gateway) = &venue {
            gateway.stats().await.unwrap();
            let read = server.only("/metadata/stats");
            assert_eq!(read.method, "GET");
            assert!(read.header("authorization").is_none());
        }
        return;
    }
    let ack = venue.send_order(&request()).await.unwrap();
    assert_eq!(ack.client_order_id, CLIENT);
    assert!(!ack.venue_order_id.is_empty());
    let sent = server
        .requests()
        .into_iter()
        .find(|r| mutation(name, r))
        .unwrap();
    assert_request_shape(name, &sent);
    let before = server.requests().len();
    let amended = venue
        .amend_order(
            SymbolId(0),
            CLIENT,
            AmendSpec {
                px: Some(94_999.0),
                qty: Some(0.01),
                exact_terms: None,
            },
        )
        .await;
    if venue.caps().amend_in_place {
        amended.unwrap();
    } else {
        assert!(
            matches!(amended, Err(VenueError::BadRequest(message)) if message.contains("amend"))
        );
        assert_eq!(server.requests().len(), before);
    }
    venue.cancel_order(SymbolId(0), CLIENT).await.unwrap();
    *scenario.lock().unwrap() = Scenario::PartialCancel;
    venue.cancel_order(SymbolId(0), CLIENT).await.unwrap();
    assert_terminal(
        venue.order_status(SymbolId(0), CLIENT).await.unwrap(),
        false,
        "0.004",
    );
    for selected in [Scenario::Reject, Scenario::ClockSkew, Scenario::RateLimit] {
        *scenario.lock().unwrap() = selected;
        let error = venue.send_order(&request()).await.unwrap_err();
        match selected {
            Scenario::RateLimit => {
                assert!(matches!(error, VenueError::Transport(_)), "{name}: {error}")
            }
            _ => assert!(
                matches!(error, VenueError::Rejected { .. }),
                "{name}: {error}"
            ),
        }
    }
    *scenario.lock().unwrap() = Scenario::Timeout;
    let before = server
        .requests()
        .iter()
        .filter(|r| mutation(name, r))
        .count();
    let result = {
        let request = request();
        let send = venue.send_order(&request);
        tokio::pin!(send);
        loop {
            tokio::select! {
                result=&mut send => panic!("{name} returned before the held reply: {result:?}"),
                ()=tokio::task::yield_now() => {
                    if server.requests().iter().filter(|r|mutation(name,r)).count()>before { break; }
                }
            }
        }
        tokio::time::advance(Duration::from_secs(11)).await;
        send.await
    };
    assert!(
        matches!(result, Err(VenueError::Transport(_))),
        "{name}: {result:?}"
    );
    *scenario.lock().unwrap() = Scenario::LateFill;
    assert_terminal(
        venue.order_status(SymbolId(0), CLIENT).await.unwrap(),
        true,
        "0.01",
    );
    assert_eq!(
        server
            .requests()
            .iter()
            .filter(|r| mutation(name, r))
            .count(),
        before + 1,
        "an ambiguous timeout must not resend"
    );
}

fn assert_terminal(lookup: engine_types::orders::OrderLookup, filled: bool, quantity: &str) {
    use engine_types::orders::{OrderLookup, TerminalOrderStatus};
    let OrderLookup::Terminal { status, row } = lookup else {
        panic!("terminal lifecycle lost: {lookup:?}")
    };
    assert_eq!(
        status,
        if filled {
            TerminalOrderStatus::Filled
        } else {
            TerminalOrderStatus::Cancelled
        }
    );
    assert_eq!(row.client_order_id, CLIENT);
    assert_eq!(row.symbol, "BTCUSDT");
    assert_eq!(
        row.filled_qty.value,
        engine_types::numeric::Exact::parse_decimal(quantity).unwrap()
    );
}

macro_rules! fixture {
    ($feature:literal,$test:ident,$name:ident) => {
        #[cfg(feature=$feature)]
        #[tokio::test(start_paused = true)]
        async fn $test() {
            lifecycle(VenueName::$name).await;
        }
    };
}
fixture!("bybit", conformance_bybit_gateway_lifecycle, BybitDemo);
fixture!(
    "binance",
    conformance_binance_gateway_lifecycle,
    BinanceTestnet
);
fixture!(
    "hyperliquid",
    conformance_hyperliquid_gateway_lifecycle,
    HyperliquidTestnet
);
fixture!(
    "lighter",
    conformance_lighter_gateway_lifecycle,
    LighterTestnet
);
fixture!("mexc", conformance_mexc_gateway_lifecycle, MexcMainnet);
fixture!(
    "variational",
    conformance_variational_gateway_lifecycle,
    VariationalMainnet
);

#[cfg(any(
    feature = "bybit",
    feature = "binance",
    feature = "hyperliquid",
    feature = "mexc"
))]
fn private_frame(name: VenueName, execution: u64, qty: &str) -> Value {
    match name.venue() {
        "bybit" => {
            json!({"topic":"execution","creationTime":1700000000000_i64,"data":[{"symbol":"BTCUSDT","orderLinkId":CLIENT,"orderId":"41","side":"Buy","execId":format!("fill-{execution}"),"execType":"Trade","execQty":qty,"execPrice":"95000","execFee":"0.01","feeCurrency":"USDT","isMaker":true,"execTime":"1700000000000"}]})
        }
        "binance" => {
            json!({"e":"ORDER_TRADE_UPDATE","E":1700000000000_i64,"T":1700000000000_i64,"o":{"s":"BTCUSDT","c":CLIENT,"S":"BUY","o":"LIMIT","f":"GTC","q":"0.01","p":"95000","ap":"95000","sp":"0","x":"TRADE","X":"PARTIALLY_FILLED","i":41,"l":qty,"z":if execution == 1 {"0.004"} else {"0.01"},"L":"95000","n":"0.01","N":"USDT","T":1700000000000_i64,"t":execution,"m":true,"R":false}})
        }
        "hyperliquid" => {
            json!({"channel":"userFills","data":{"user":HL_ACCOUNT,"fills":[{"coin":"BTC","px":"95000","sz":qty,"side":"B","time":1700000000000_i64,"fee":"0.01","tid":execution,"crossed":false,"oid":41,"cloid":"0x01018bcfe56800000000000100000000"}]}})
        }
        // MEXC counts contracts, 0.0001 BTC each, so the same fill is a
        // different number here than on every other venue in this table.
        "mexc" => {
            let vol = (qty.parse::<f64>().unwrap() / 0.0001).round() as i64;
            json!({"channel":"push.personal.order.deal","data":{"id":execution,"symbol":"BTC_USDT","side":1,"vol":vol,"price":95000,"fee":0.01,"feeCurrency":"USDT","profit":0,"isTaker":false,"category":1,"orderId":41,"isSelf":false,"externalOid":CLIENT,"timestamp":1700000000000_i64},"ts":1700000000000_i64})
        }
        _ => unreachable!(),
    }
}

async fn stream_lifecycle(name: VenueName) {
    use engine_types::{OrderFeed, OrderUpdate};
    use engine_venue::OrderFeeds;
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::tungstenite::Message;
    let io_progress = tokio::spawn(async {
        loop {
            tokio::task::yield_now().await;
        }
    });
    let _io_progress = AbortOnDrop(io_progress);
    #[cfg(feature = "lighter")]
    if name.venue() == "lighter" {
        let mut feed = OrderFeeds::Lighter(engine_venue::LighterOrderFeed::with_period(
            Duration::from_secs(5),
        ));
        assert!(matches!(
            feed.next_update().await.unwrap(),
            OrderUpdate::StreamReset { .. }
        ));
        assert!(futures_util::poll!(Box::pin(feed.next_update())).is_pending());
        tokio::time::advance(Duration::from_secs(5)).await;
        assert!(matches!(
            feed.next_update().await.unwrap(),
            OrderUpdate::StreamReset { .. }
        ));
        return;
    }
    if name.venue() == "variational" {
        let mut feed = OrderFeeds::Silent;
        assert!(futures_util::poll!(Box::pin(feed.next_update())).is_pending());
        tokio::time::advance(Duration::from_secs(3600)).await;
        assert!(futures_util::poll!(Box::pin(feed.next_update())).is_pending());
        return;
    }
    #[cfg(any(
        feature = "bybit",
        feature = "binance",
        feature = "hyperliquid",
        feature = "mexc"
    ))]
    {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("ws://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            for connection in 0..2 {
                let (tcp, _) = listener.accept().await.unwrap();
                let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
                if name.venue() == "bybit" {
                    let auth = socket.next().await.unwrap().unwrap();
                    let auth: Value = serde_json::from_str(auth.to_text().unwrap()).unwrap();
                    assert_eq!(auth["op"], "auth");
                    socket
                        .send(Message::Text(
                            json!({"op":"auth","success":true}).to_string().into(),
                        ))
                        .await
                        .unwrap();
                    let subscribe = socket.next().await.unwrap().unwrap();
                    let subscribe: Value =
                        serde_json::from_str(subscribe.to_text().unwrap()).unwrap();
                    assert_eq!(subscribe["op"], "subscribe");
                    socket
                        .send(Message::Text(
                            json!({"op":"subscribe","success":true}).to_string().into(),
                        ))
                        .await
                        .unwrap();
                } else if name.venue() == "hyperliquid" {
                    for topic in ["orderUpdates", "userFills"] {
                        let frame = socket.next().await.unwrap().unwrap();
                        let frame: Value = serde_json::from_str(frame.to_text().unwrap()).unwrap();
                        assert_eq!(frame["subscription"]["type"], topic);
                        assert_eq!(frame["subscription"]["user"], HL_ACCOUNT);
                    }
                    socket.send(Message::Text(json!({"channel":"orderUpdates","data":[{"order":{"coin":"BTC","side":"B","limitPx":"95000","sz":"0.01","oid":41,"cloid":"0x01018bcfe56800000000000100000000"},"status":"open"}]}).to_string().into())).await.unwrap();
                    socket
                        .send(Message::Text(
                            json!({"channel":"userFills","data":{"isSnapshot":true,"fills":[]}})
                                .to_string()
                                .into(),
                        ))
                        .await
                        .unwrap();
                } else if name.venue() == "mexc" {
                    let login = socket.next().await.unwrap().unwrap();
                    let login: Value = serde_json::from_str(login.to_text().unwrap()).unwrap();
                    assert_eq!(login["method"], "login");
                    assert_eq!(login["subscribe"], false);
                    socket
                        .send(Message::Text(
                            json!({"channel":"rs.login","data":"success","ts":"1700000000000"})
                                .to_string()
                                .into(),
                        ))
                        .await
                        .unwrap();
                    let filter = socket.next().await.unwrap().unwrap();
                    let filter: Value = serde_json::from_str(filter.to_text().unwrap()).unwrap();
                    assert_eq!(filter["method"], "personal.filter");
                }
                // Execution 2 occurs during the disconnect and is available only in REST history.
                let execution = if connection == 0 { 1 } else { 3 };
                let frame = private_frame(name, execution, "0.004");
                socket
                    .send(Message::Text(frame.to_string().into()))
                    .await
                    .unwrap();
                if connection == 0 {
                    socket.close(None).await.unwrap();
                } else {
                    while let Some(Ok(message)) = socket.next().await {
                        if matches!(message, Message::Close(_)) {
                            break;
                        }
                    }
                }
            }
        });
        let server = AbortOnDrop(server);
        let rest = TestServer::start(move |request, _| {
            if request.path == "/fapi/v1/listenKey" {
                return (200, json!({"listenKey":"fixture"}).to_string());
            }
            let fills: Vec<_> = [(1, "0.004"), (2, "0.002"), (3, "0.004")]
                .into_iter()
                .map(|(id, qty)| {
                    let frame = private_frame(name, id, qty);
                    match name.venue() {
                        "bybit" => frame["data"][0].clone(),
                        "hyperliquid" => frame["data"]["fills"][0].clone(),
                        // Same fill, and the maker flag changes name between
                        // the two transports: `isTaker` on the socket,
                        // `taker` on the history endpoint.
                        "mexc" => {
                            let mut row = frame["data"].clone();
                            row["taker"] = row["isTaker"].clone();
                            row.as_object_mut().unwrap().remove("isTaker");
                            row
                        }
                        other => panic!("unexpected history transport: {other}"),
                    }
                })
                .collect();
            match name.venue() {
                "bybit" => {
                    assert_eq!(request.path, "/v5/execution/list");
                    bybit(json!({"list":fills,"nextPageCursor":""}))
                }
                "hyperliquid" => {
                    assert_eq!(request.json()["type"], "userFillsByTime");
                    (200, json!(fills).to_string())
                }
                "mexc" => match request.path.as_str() {
                    "/api/v1/contract/detail" => (200, mexc_contract_detail().to_string()),
                    "/api/v1/private/order/list/order_deals/v3" => (
                        200,
                        json!({"success":true,"code":0,"data":{"resultList":fills}}).to_string(),
                    ),
                    other => panic!("unexpected MEXC history request {other}"),
                },
                _ => unreachable!(),
            }
        })
        .await;
        #[allow(unreachable_patterns)]
        let mut feed: OrderFeeds = match name {
            #[cfg(feature = "bybit")]
            VenueName::BybitDemo => OrderFeeds::Bybit(engine_venue::BybitOrderFeed::for_test(
                &url,
                engine_venue::VenueRealm::Demo.credentials_for_test("key", "secret"),
                vec!["BTCUSDT".into()],
            )),
            #[cfg(feature = "binance")]
            VenueName::BinanceTestnet => {
                OrderFeeds::Binance(engine_venue::BinanceOrderFeed::for_test(
                    &rest.base_url(),
                    &url,
                    engine_venue::BinanceRealm::Testnet.credentials_for_test("key", "secret"),
                    vec!["BTCUSDT".into()],
                ))
            }
            #[cfg(feature = "hyperliquid")]
            VenueName::HyperliquidTestnet => OrderFeeds::Hyperliquid(
                engine_venue::HyperliquidOrderFeed::for_test(
                    &url,
                    &engine_venue::HyperliquidRealm::Testnet
                        .credentials_for_test(HL_ACCOUNT, HL_KEY),
                    vec!["BTCUSDT".into()],
                )
                .unwrap(),
            ),
            #[cfg(feature = "mexc")]
            VenueName::MexcMainnet => {
                let mut feed = engine_venue::MexcOrderFeed::for_test(
                    &url,
                    engine_venue::MexcRealm::Mainnet.credentials_for_test("key", "secret"),
                );
                // The socket names `BTC_USDT`; the id and the contract size
                // both reach its decoder through the instrument catalogue.
                let (_, spec) = engine_public::venues::mexc::contracts::Contracts::parse_raw(
                    &mexc_contract_detail().to_string(),
                )
                .unwrap()
                .instrument_specs()
                .pop()
                .unwrap();
                feed.learn_instrument(SymbolId(0), &spec);
                OrderFeeds::Mexc(feed)
            }
            _ => unreachable!(),
        };
        assert!(matches!(
            feed.next_update().await.unwrap(),
            OrderUpdate::StreamReset { .. }
        ));
        let mut executions = std::collections::BTreeMap::new();
        for index in 0..2 {
            let update = loop {
                let update = feed.next_update().await.unwrap();
                match update {
                    OrderUpdate::Ack(ack) => assert_eq!(ack.client_order_id, CLIENT),
                    OrderUpdate::Amended {
                        client_order_id, ..
                    } => assert_eq!(client_order_id, CLIENT),
                    other => break other,
                }
            };
            let OrderUpdate::Fill {
                client_order_id,
                symbol,
                qty,
                exec_id,
                amounts,
                ..
            } = update
            else {
                panic!("{name}: expected fill, got {update:?}")
            };
            assert_eq!(client_order_id, CLIENT);
            assert_eq!(symbol, SymbolId(0));
            assert_eq!(qty, 0.004);
            let exact = amounts.unwrap().quantity.value;
            assert_eq!(
                exact,
                engine_types::numeric::Exact::parse_decimal("0.004").unwrap()
            );
            assert!(executions.insert(exec_id, exact).is_none());
            if index == 0 {
                assert!(
                    feed.next_update().await.is_err(),
                    "{name}: missing disconnect signal"
                );
                for _ in 0..20 {
                    tokio::task::yield_now().await;
                }
                tokio::time::advance(Duration::from_secs(2)).await;
                assert!(
                    matches!(
                        feed.next_update().await.unwrap(),
                        OrderUpdate::StreamReset { .. }
                    ),
                    "{name}: lost recovery watermark"
                );
            }
        }
        assert_eq!(executions.len(), 2);
        let mut venue = build(name, &rest);
        let recovered = venue.executions(1700000000000, 1700000001000).await;
        if name.venue() == "binance" {
            assert!(
                matches!(recovered, Err(VenueError::BadRequest(message)) if message.contains("execution recovery is unavailable"))
            );
            assert!(name.require_engine_run_ready().is_err());
        } else {
            let rows = recovered.unwrap().collect::<Result<Vec<_>, _>>().unwrap();
            assert_eq!(rows.len(), 3);
            for row in rows {
                assert_eq!(row.client_order_id, CLIENT);
                assert_eq!(row.symbol, "BTCUSDT");
                let qty = row.amounts.unwrap().quantity.value;
                if let Some(previous) = executions.insert(row.exec_id, qty.clone()) {
                    assert_eq!(previous, qty, "private/history overlap changed quantity");
                } else {
                    assert_eq!(
                        qty,
                        engine_types::numeric::Exact::parse_decimal("0.002").unwrap()
                    );
                }
            }
            assert_eq!(executions.len(), 3);
            let total = executions
                .values()
                .fold(engine_types::numeric::Exact::zero(), |sum, qty| sum + qty);
            assert_eq!(
                total,
                engine_types::numeric::Exact::parse_decimal("0.01").unwrap()
            );
        }
        drop(feed);
        drop(server);
    }
}

macro_rules! stream_fixture {
    ($feature:literal,$test:ident,$name:ident) => {
        #[cfg(feature=$feature)]
        #[tokio::test(start_paused = true)]
        async fn $test() {
            stream_lifecycle(VenueName::$name).await;
        }
    };
}
stream_fixture!("bybit", conformance_bybit_private_reconnect_gap, BybitDemo);
stream_fixture!(
    "binance",
    conformance_binance_private_reconnect_gap,
    BinanceTestnet
);
stream_fixture!(
    "hyperliquid",
    conformance_hyperliquid_private_reconnect_gap,
    HyperliquidTestnet
);
stream_fixture!(
    "lighter",
    conformance_lighter_private_recovery_only,
    LighterTestnet
);
stream_fixture!("mexc", conformance_mexc_private_reconnect_gap, MexcMainnet);
stream_fixture!(
    "variational",
    conformance_variational_private_silent,
    VariationalMainnet
);
