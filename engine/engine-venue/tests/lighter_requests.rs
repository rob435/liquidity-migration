//! What the Lighter gateway actually puts on the wire, checked against a local
//! server. No network, no credentials.
//!
//! The signing itself is pinned layer by layer against the venue's own Go
//! reference in `src/venues/lighter/crypto/vectors.rs`. What is checked here is
//! everything around it: which endpoint each method reaches, that a
//! transaction goes out form-encoded with its type beside it, that the signed
//! fields carry the market's own integers rather than decimals, and that a
//! reply saying `code` anything but 200 is read as a refusal.

mod support;

use engine_types::{
    AmendSpec, OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce,
    VenueError, VenueGateway,
};
use engine_venue::{LighterGateway, LighterRealm};
use serde_json::Value;
use support::{Recorded, TestServer};

/// Forty bytes of hex; obviously not a real key.
const KEY: &str =
    "0101010101010101010101010101010101010101010101010101010101010101010101010101010f";
const ACCOUNT: &str = "42:3";

fn gateway(server: &TestServer) -> LighterGateway {
    LighterGateway::for_test(
        &server.base_url(),
        LighterRealm::Testnet,
        LighterRealm::Testnet.credentials_for_test(ACCOUNT, KEY),
        vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()],
    )
    .expect("test credentials")
}

fn entry(kind: OrderKind, stop: Option<StopSpec>) -> OrderRequest {
    OrderRequest {
        client_order_id: "eng-1700000000000-1".to_string(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        kind,
        stop,
        reduce_only: false,
        close_position: false,
    }
}

const MARKETS: &str = r#"{"code":200,"order_book_details":[
    {"symbol":"BTC","market_id":0,"status":"active","supported_size_decimals":5,
     "supported_price_decimals":1,"min_base_amount":"0.0001","min_quote_amount":"10"},
    {"symbol":"ETH","market_id":1,"status":"active","supported_size_decimals":4,
     "supported_price_decimals":2,"min_base_amount":"0.001","min_quote_amount":"10"}
]}"#;

fn answer(request: &Recorded) -> (u16, String) {
    let payload = match request.path.as_str() {
        "/api/v1/orderBookDetails" => MARKETS.to_string(),
        "/api/v1/nextNonce" => r#"{"code":200,"nonce":7}"#.to_string(),
        "/api/v1/sendTx" => {
            r#"{"code":200,"message":"","tx_hash":"0xabc","predicted_execution_time_ms":5}"#
                .to_string()
        }
        "/api/v1/account" => r#"{"code":200,"accounts":[{"collateral":"1500.25",
            "available_balance":"1200.5","positions":[
              {"market_id":0,"symbol":"BTC","sign":1,"position":"0.01",
               "avg_entry_price":"95000","initial_margin_fraction":"0.05"}
            ]}]}"#
            .to_string(),
        "/api/v1/accountActiveOrders" => r#"{"code":200,"orders":[]}"#.to_string(),
        "/api/v1/trades" => r#"{"code":200,"trades":[]}"#.to_string(),
        _ => r#"{"code":200}"#.to_string(),
    };
    (200, payload)
}

/// The transaction out of a recorded `sendTx` body.
fn transaction(request: &Recorded) -> (u8, Value) {
    let mut tx_type = 0u8;
    let mut info = String::new();
    for pair in request.body.split('&') {
        let Some((key, value)) = pair.split_once('=') else {
            continue;
        };
        match key {
            "tx_type" => tx_type = value.parse().expect("a transaction type"),
            "tx_info" => info = percent_decode(value),
            _ => (),
        }
    }
    (
        tx_type,
        serde_json::from_str(&info).unwrap_or_else(|e| panic!("tx_info is not JSON ({e}): {info}")),
    )
}

fn percent_decode(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).expect("hex");
            out.push(u8::from_str_radix(hex, 16).expect("hex"));
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).expect("utf8")
}

#[tokio::test]
async fn an_order_goes_out_form_encoded_with_its_transaction_type() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.send_order(&entry(
        OrderKind::Limit {
            px: 95_000.15,
            tif: TimeInForce::PostOnly,
        },
        None,
    ))
    .await
    .unwrap();

    let sent = server.to_path("/api/v1/sendTx");
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0].method, "POST");
    assert_eq!(
        sent[0].header("content-type"),
        Some("application/x-www-form-urlencoded")
    );
    let (tx_type, tx) = transaction(&sent[0]);
    assert_eq!(tx_type, 14, "the venue's create-order transaction type");
    assert_eq!(tx["AccountIndex"], 42);
    assert_eq!(tx["ApiKeyIndex"], 3);
    assert_eq!(tx["MarketIndex"], 0);
    assert_eq!(tx["Nonce"], 7, "the nonce the venue handed out");
    // Post-only, a buy, and not reduce-only.
    assert_eq!(tx["TimeInForce"], 2);
    assert_eq!(tx["Type"], 0, "a limit order");
    assert_eq!(tx["IsAsk"], 0);
    assert_eq!(tx["ReduceOnly"], 0);
}

#[tokio::test]
async fn prices_and_sizes_are_the_markets_own_integers() {
    // The signature covers the integer, so a decimal anywhere here would be a
    // rounding the venue never agreed to.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.send_order(&entry(
        OrderKind::Limit {
            px: 95_000.15,
            tif: TimeInForce::Gtc,
        },
        None,
    ))
    .await
    .unwrap();

    let (_, tx) = transaction(&server.to_path("/api/v1/sendTx")[0]);
    // One price decimal, so 95000.15 rounds toward the passive side for a buy.
    assert_eq!(tx["Price"], 950_001);
    // Five size decimals.
    assert_eq!(tx["BaseAmount"], 1_000);
    assert!(tx["Price"].is_number(), "the price went out as a decimal");
    assert!(tx["BaseAmount"].is_number());
}

#[tokio::test]
async fn every_transaction_carries_a_signature() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.send_order(&entry(
        OrderKind::Limit {
            px: 95_000.0,
            tif: TimeInForce::Gtc,
        },
        None,
    ))
    .await
    .unwrap();
    let (_, tx) = transaction(&server.to_path("/api/v1/sendTx")[0]);
    let signature = tx["Sig"].as_str().expect("a signature");
    // Eighty bytes, base64.
    assert_eq!(signature.len(), 108, "{signature}");
    assert_ne!(
        signature.trim_matches('A'),
        "",
        "the signature is all zeros"
    );
}

#[tokio::test]
async fn a_stop_is_a_second_transaction_on_the_other_side() {
    // This venue takes one order per transaction, so an entry and its stop
    // cannot travel together the way they do on Bybit and Hyperliquid.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.send_order(&entry(
        OrderKind::Limit {
            px: 95_000.0,
            tif: TimeInForce::Gtc,
        },
        Some(StopSpec {
            trigger_px: 93_000.0,
        }),
    ))
    .await
    .unwrap();

    let sent = server.to_path("/api/v1/sendTx");
    assert_eq!(sent.len(), 2, "the entry and its stop are two transactions");
    let (_, stop) = transaction(&sent[1]);
    assert_eq!(stop["Type"], 2, "a stop-loss order");
    assert_eq!(stop["IsAsk"], 1, "the stop on a long must sell");
    assert_eq!(stop["ReduceOnly"], 1);
    assert_eq!(stop["TriggerPrice"], 930_000);
    assert_eq!(stop["TimeInForce"], 0, "a stop fires and takes");
    assert_eq!(
        stop["OrderExpiry"], 0,
        "an immediate-or-cancel order carries no expiry, the same rule the entry follows"
    );
    // The price field bounds how far the stop may fill once it fires. Set at
    // the trigger it would fill nothing through a gap — which is the move it
    // exists to catch. A selling stop takes the lowest the field allows.
    assert_eq!(stop["Price"], 1, "the stop's fill bound, not its trigger");
    assert_ne!(stop["Price"], stop["TriggerPrice"]);
    // And its nonce is the next one, not a repeat.
    let (_, first) = transaction(&sent[0]);
    assert_ne!(
        stop["Nonce"], first["Nonce"],
        "two transactions shared a nonce"
    );
}

#[tokio::test]
async fn a_stop_the_venue_refuses_does_not_unsay_the_entry_it_accepted() {
    // The entry is already live by the time the stop is sent. Reporting the
    // stop's refusal as the entry's would tell the engine no order exists,
    // free its reservation, and leave a position at the venue that nothing in
    // the log accounts for.
    let sends = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counted = sends.clone();
    let server = TestServer::start(move |request, _| {
        if request.path == "/api/v1/sendTx" {
            let n = counted.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            if n == 1 {
                return (
                    200,
                    r#"{"code":21120,"message":"trigger price out of range"}"#.to_string(),
                );
            }
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let ack = gw
        .send_order(&entry(
            OrderKind::Limit {
                px: 95_000.0,
                tif: TimeInForce::Gtc,
            },
            Some(StopSpec {
                trigger_px: 93_000.0,
            }),
        ))
        .await
        .expect("the entry was accepted, so the order exists");
    assert_eq!(ack.client_order_id, "eng-1700000000000-1");
    assert_eq!(
        server.to_path("/api/v1/sendTx").len(),
        2,
        "both were attempted"
    );
}

#[tokio::test]
async fn an_exit_never_carries_a_stop_even_when_handed_one() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let mut exit = entry(
        OrderKind::Limit {
            px: 96_000.0,
            tif: TimeInForce::Gtc,
        },
        Some(StopSpec {
            trigger_px: 93_000.0,
        }),
    );
    exit.reduce_only = true;
    exit.side = Side::Sell;
    gw.send_order(&exit).await.unwrap();
    assert_eq!(server.to_path("/api/v1/sendTx").len(), 1);
    let (_, tx) = transaction(&server.to_path("/api/v1/sendTx")[0]);
    assert_eq!(tx["ReduceOnly"], 1);
    assert_eq!(tx["IsAsk"], 1);
}

#[tokio::test]
async fn a_cancel_names_the_order_by_the_index_the_engine_minted() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.cancel_order(SymbolId(0), "eng-1700000000000-1")
        .await
        .unwrap();

    let (tx_type, tx) = transaction(&server.to_path("/api/v1/sendTx")[0]);
    assert_eq!(tx_type, 15, "the venue's cancel transaction type");
    assert_eq!(tx["MarketIndex"], 0);
    let index = tx["Index"].as_i64().expect("a client order index");
    assert!(
        index > 0 && index < (1 << 48),
        "outside the venue's range: {index}"
    );
}

#[tokio::test]
async fn the_nonce_is_asked_for_once_and_then_counted() {
    // Asking before every order would be a round trip in front of every order,
    // and the venue takes nonces strictly in order anyway.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    for _ in 0..3 {
        gw.send_order(&entry(
            OrderKind::Limit {
                px: 95_000.0,
                tif: TimeInForce::Gtc,
            },
            None,
        ))
        .await
        .unwrap();
    }
    assert_eq!(
        server.to_path("/api/v1/nextNonce").len(),
        1,
        "the venue was asked for a nonce more than once"
    );
    let sent = server.to_path("/api/v1/sendTx");
    let nonces: Vec<i64> = sent
        .iter()
        .map(|r| transaction(r).1["Nonce"].as_i64().expect("a nonce"))
        .collect();
    assert_eq!(nonces, vec![7, 8, 9], "nonces must climb without a gap");
}

#[tokio::test]
async fn a_refused_transaction_makes_the_next_one_ask_again() {
    // A stale counter refuses every order after it, and the venue may or may
    // not have consumed the nonce of the one it refused.
    let server = TestServer::start(|request, count| {
        if request.path == "/api/v1/sendTx" && count == 0 {
            return (
                200,
                r#"{"code":21120,"message":"invalid nonce"}"#.to_string(),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let refused = gw
        .send_order(&entry(
            OrderKind::Limit {
                px: 95_000.0,
                tif: TimeInForce::Gtc,
            },
            None,
        ))
        .await;
    match refused {
        Err(VenueError::Rejected { code, message }) => {
            assert_eq!(code, 21120);
            assert!(message.contains("nonce"), "{message}");
        }
        other => panic!("expected a rejection, got {other:?}"),
    }
    gw.send_order(&entry(
        OrderKind::Limit {
            px: 95_000.0,
            tif: TimeInForce::Gtc,
        },
        None,
    ))
    .await
    .unwrap();
    assert_eq!(
        server.to_path("/api/v1/nextNonce").len(),
        2,
        "the counter was not re-read after a refusal"
    );
}

#[tokio::test]
async fn a_busy_window_is_walked_rather_than_truncated() {
    // This read is the ONLY way a fill is ever learned on this venue, so a
    // page that came back full and was taken as complete is a fill the log
    // never gets.
    let page = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counted = page.clone();
    let server = TestServer::start(move |request, _| {
        if request.path == "/api/v1/trades" {
            let n = counted.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            // A full first page, then a short second one.
            let rows: Vec<String> = if n == 0 {
                (0..100)
                    .map(|i| {
                        format!(
                            r#"{{"trade_id":{i},"market_id":0,"size":"0.01","price":"95000",
                                 "timestamp":{},"fee":"0.01","is_maker_ask":true,
                                 "ask_account_id":99,"bid_account_id":42,
                                 "bid_client_order_index":1,"ask_client_order_index":2}}"#,
                            1_000 + i
                        )
                    })
                    .collect()
            } else {
                vec![
                    r#"{"trade_id":9001,"market_id":0,"size":"0.01","price":"95000",
                         "timestamp":1200,"fee":"0.01","is_maker_ask":true,
                         "ask_account_id":99,"bid_account_id":42,
                         "bid_client_order_index":1,"ask_client_order_index":2}"#
                        .to_string(),
                ]
            };
            return (
                200,
                format!(r#"{{"code":200,"trades":[{}]}}"#, rows.join(",")),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let fills = gw.executions(0, 100_000).await.expect("a walked history");
    assert_eq!(
        server.to_path("/api/v1/trades").len(),
        2,
        "the full page was taken as the end"
    );
    assert_eq!(fills.len(), 101, "a fill past the first page was lost");
    assert!(
        fills.iter().any(|f| f.exec_id == "9001"),
        "the second page never arrived"
    );
}

#[tokio::test]
async fn a_signed_read_carries_the_auth_token() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.working_orders().await.unwrap();

    let sent = server.to_path("/api/v1/accountActiveOrders");
    assert_eq!(sent.len(), 1);
    let token = sent[0].header("authorization").expect("an auth token");
    let parts: Vec<&str> = token.split(':').collect();
    assert_eq!(parts.len(), 4, "deadline:account:key:signature — {token}");
    assert_eq!(parts[1], "42");
    assert_eq!(parts[2], "3");
    // No market named, so the venue answers for every market — the point of
    // this read is to find orders nobody here placed.
    assert!(!sent[0].query.contains("market_id"), "{}", sent[0].query);
}

#[tokio::test]
async fn the_account_read_and_the_order_read_go_out_together() {
    // Two reads because this venue keeps no stop on the position row; issued
    // together so the picture is of one moment.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let view = gw.account_view().await.unwrap();
    assert_eq!(view.equity_usdt, 1500.25);
    assert_eq!(view.available_usdt, 1200.5);
    assert_eq!(view.positions.len(), 1);
    assert!(
        !view.positions[0].stop_attached,
        "a position with no stop order read as protected"
    );
    assert_eq!(view.positions[0].leverage, Some(20.0));
    assert_eq!(server.to_path("/api/v1/account").len(), 1);
    assert_eq!(server.to_path("/api/v1/accountActiveOrders").len(), 1);
}

#[tokio::test]
async fn instrument_rules_come_from_the_venues_own_market_list() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let rules = gw.instrument_rules().await.unwrap();
    let (_, rule) = rules.iter().find(|(n, _)| n == "BTCUSDT").expect("BTC");
    assert!((rule.tick_size - 0.1).abs() < 1e-12);
    assert!((rule.qty_step - 1e-5).abs() < 1e-15);
    assert_eq!(rule.min_qty, 0.0001);
    assert_eq!(rule.min_notional, 10.0);
}

#[tokio::test]
async fn an_amend_is_refused_rather_than_turned_into_a_different_trade() {
    // Declared false in caps. Cancel-and-replace is a new order at the back of
    // the queue, and the caller decides whether to make it.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let refused = gw
        .amend_order(
            SymbolId(0),
            "eng-1",
            AmendSpec {
                px: Some(1.0),
                qty: None,
            },
        )
        .await;
    assert!(
        matches!(refused, Err(VenueError::BadRequest(_))),
        "{refused:?}"
    );
    assert!(server.to_path("/api/v1/sendTx").is_empty());
    assert!(!gw.caps().amend_in_place);
}

#[tokio::test]
async fn an_account_the_venue_does_not_know_stops_the_engine_at_the_door() {
    let server = TestServer::start(|request, _| {
        if request.path == "/api/v1/account" {
            return (200, r#"{"code":200,"accounts":[]}"#.to_string());
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    match gw.account_identity().await {
        Err(VenueError::Credentials(said)) => assert!(said.contains("42"), "{said}"),
        other => panic!("an unknown account was accepted: {other:?}"),
    }
}

#[tokio::test]
async fn the_account_identity_names_the_venue_the_account_and_the_realm() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let who = gw.account_identity().await.unwrap();
    assert_eq!(who.venue, "lighter");
    assert_eq!(who.realm, "lighter_testnet");
    assert_eq!(who.user_id, "42");
    // The lease has to be able to name a file after it.
    assert!(engine_venue::lease::account_key_text(&who.user_id).is_some());
}
