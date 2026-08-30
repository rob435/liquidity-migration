//! What the Hyperliquid gateway actually puts on the wire, checked against a
//! local server. No network, no credentials.
//!
//! The signature itself is pinned by unit tests against the venue's own
//! published vectors. What is checked here is everything around it: which
//! endpoint each method reaches, what the action says, that the entry and its
//! stop travel in one signed request, and that a refusal buried in an
//! otherwise-successful reply is read as a refusal.

mod support;

use engine_types::{
    AmendSpec, OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce,
    VenueError, VenueGateway,
};
use engine_venue::{HyperliquidGateway, HyperliquidRealm};
use serde_json::Value;
use support::{Recorded, TestServer};

/// The published test key from the venue's own SDK, and an address that is
/// plainly not a real account.
const WALLET_KEY: &str = "0x0123456789012345678901234567890123456789012345678901234567890123";
const ACCOUNT: &str = "0x0000000000000000000000000000000000000001";

fn gateway(server: &TestServer) -> HyperliquidGateway {
    HyperliquidGateway::for_test(
        &server.base_url(),
        HyperliquidRealm::Testnet,
        HyperliquidRealm::Testnet.credentials_for_test(ACCOUNT, WALLET_KEY),
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

const META: &str = r#"{"universe":[
    {"name":"BTC","szDecimals":5,"maxLeverage":40},
    {"name":"ETH","szDecimals":4,"maxLeverage":25}
]}"#;

fn resting(oid: i64) -> String {
    format!(
        r#"{{"status":"ok","response":{{"type":"order","data":{{"statuses":[{{"resting":{{"oid":{oid}}}}}]}}}}}}"#
    )
}

/// Answers every endpoint the gateway reaches. `/info` is one path for many
/// questions, so the answer is chosen by the body's `type`.
fn answer(request: &Recorded) -> (u16, String) {
    if request.path == "/exchange" {
        return (200, resting(4242));
    }
    let body: Value = request.json();
    let kind = body.get("type").and_then(Value::as_str).unwrap_or_default();
    let payload = match kind {
        "meta" => META.to_string(),
        "allMids" => r#"{"BTC":"95000.0","ETH":"3000.0"}"#.to_string(),
        "clearinghouseState" => r#"{
            "marginSummary":{"accountValue":"1500.25","totalMarginUsed":"300"},
            "withdrawable":"1200.5",
            "assetPositions":[{"position":{"coin":"BTC","szi":"0.01","entryPx":"95000",
                               "leverage":{"type":"cross","value":20}}}]
        }"#
        .to_string(),
        "frontendOpenOrders" => r#"[
            {"coin":"BTC","side":"A","sz":"0.01","origSz":"0.01","oid":77,"reduceOnly":true,
             "isTrigger":true,"orderType":"Stop Market","triggerPx":"93000"}
        ]"#
        .to_string(),
        "userFillsByTime" => "[]".to_string(),
        "extraAgents" => "[]".to_string(),
        _ => "{}".to_string(),
    };
    (200, payload)
}

/// The action out of a recorded `/exchange` request.
fn action(request: &Recorded) -> Value {
    request
        .json()
        .get("action")
        .cloned()
        .expect("every exchange request carries an action")
}

#[tokio::test]
async fn an_entry_and_its_stop_travel_in_one_signed_action() {
    // One request, so a filled entry is never briefly unprotected. The venue
    // arms the stop when the parent fills, which is what `normalTpsl` means.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.send_order(&entry(
        OrderKind::Limit {
            px: 94_000.0,
            tif: TimeInForce::PostOnly,
        },
        Some(StopSpec {
            trigger_px: 93_000.0,
        }),
    ))
    .await
    .unwrap();

    let sent = server.to_path("/exchange");
    assert_eq!(sent.len(), 1, "the entry and the stop were sent separately");
    let action = action(&sent[0]);
    assert_eq!(action["type"], "order");
    assert_eq!(action["grouping"], "normalTpsl");
    let orders = action["orders"].as_array().expect("orders");
    assert_eq!(orders.len(), 2);

    // The entry: asset 0 (BTC is first in the venue's list), buy, post-only.
    assert_eq!(orders[0]["a"], 0);
    assert_eq!(orders[0]["b"], true);
    assert_eq!(orders[0]["p"], "94000");
    assert_eq!(orders[0]["s"], "0.01");
    assert_eq!(orders[0]["r"], false);
    assert_eq!(orders[0]["t"]["limit"]["tif"], "Alo");

    // The stop: the other side, reduce-only, and it crosses when it fires.
    assert_eq!(orders[1]["b"], false);
    assert_eq!(orders[1]["r"], true);
    assert_eq!(orders[1]["t"]["trigger"]["isMarket"], true);
    assert_eq!(orders[1]["t"]["trigger"]["tpsl"], "sl");
    assert_eq!(orders[1]["t"]["trigger"]["triggerPx"], "93000");
}

#[tokio::test]
async fn the_body_carries_the_nonce_and_the_signature_beside_the_action() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.send_order(&entry(
        OrderKind::Limit {
            px: 94_000.0,
            tif: TimeInForce::Gtc,
        },
        None,
    ))
    .await
    .unwrap();

    let body = server.to_path("/exchange")[0].json();
    assert!(body["nonce"].as_u64().unwrap() > 0);
    let signature = &body["signature"];
    assert!(signature["r"].as_str().unwrap().starts_with("0x"));
    assert!(signature["s"].as_str().unwrap().starts_with("0x"));
    // The venue reads 27 and 28, not 0 and 1.
    let v = signature["v"].as_u64().unwrap();
    assert!(v == 27 || v == 28, "recovery id {v}");
}

#[tokio::test]
async fn a_market_intent_asks_for_a_mid_and_crosses_from_it() {
    // This venue has no market order, so one becomes an immediate-or-cancel
    // limit priced through the book.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.send_order(&entry(OrderKind::Market, None))
        .await
        .unwrap();

    let mids: Vec<Recorded> = server
        .to_path("/info")
        .into_iter()
        .filter(|r| r.json()["type"] == "allMids")
        .collect();
    assert_eq!(mids.len(), 1, "a market order read no mid price");

    let orders = action(&server.to_path("/exchange")[0]);
    let order = &orders["orders"][0];
    assert_eq!(order["t"]["limit"]["tif"], "Ioc");
    let px: f64 = order["p"].as_str().unwrap().parse().unwrap();
    assert!(px > 95_000.0, "a market buy must cross the mid: {px}");
}

#[tokio::test]
async fn a_limit_order_never_asks_for_a_mid() {
    // The round trip is only paid where it is needed. A limit order already
    // carries its price.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    gw.send_order(&entry(
        OrderKind::Limit {
            px: 94_000.0,
            tif: TimeInForce::Gtc,
        },
        None,
    ))
    .await
    .unwrap();
    assert!(server
        .to_path("/info")
        .iter()
        .all(|r| r.json()["type"] != "allMids"));
}

#[tokio::test]
async fn a_reduce_only_order_never_carries_a_stop_even_when_handed_one() {
    // An exit has nothing left to protect, and the venue refuses the pair.
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

    let action = action(&server.to_path("/exchange")[0]);
    assert_eq!(action["grouping"], "na");
    assert_eq!(action["orders"].as_array().unwrap().len(), 1);
    assert_eq!(action["orders"][0]["r"], true);
}

#[tokio::test]
async fn a_cancel_names_the_order_by_the_id_the_engine_minted() {
    let server = TestServer::start(|request, _| {
        if request.path == "/exchange" {
            return (
                200,
                r#"{"status":"ok","response":{"type":"cancel","data":{"statuses":["success"]}}}"#
                    .to_string(),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    gw.cancel_order(SymbolId(0), "eng-1700000000000-1")
        .await
        .unwrap();

    let action = action(&server.to_path("/exchange")[0]);
    assert_eq!(action["type"], "cancelByCloid");
    assert_eq!(action["cancels"][0]["asset"], 0);
    let cloid = action["cancels"][0]["cloid"].as_str().unwrap();
    assert_eq!(cloid.len(), 34, "a client id is 16 bytes of hex: {cloid}");
}

#[tokio::test]
async fn an_asset_the_venue_spells_in_lower_case_can_be_stopped_and_priced() {
    // kPEPE, kBONK and their kin are the venue's own spelling, and every
    // symbol reaching the engine is upper-cased. A coin folded back up from
    // the symbol matches nothing the venue wrote, which leaves a position that
    // can be seen and neither protected nor exited.
    let server = TestServer::start(|request, _| {
        let body: Value = request.json();
        if request.path == "/info" && body["type"] == "meta" {
            return (
                200,
                r#"{"universe":[{"name":"kPEPE","szDecimals":0,"maxLeverage":10}]}"#.to_string(),
            );
        }
        if request.path == "/info" && body["type"] == "clearinghouseState" {
            return (
                200,
                r#"{"marginSummary":{"accountValue":"1000","totalMarginUsed":"100"},
                    "withdrawable":"900",
                    "assetPositions":[{"position":{"coin":"kPEPE","szi":"1000","entryPx":"0.02"}}]}"#
                    .to_string(),
            );
        }
        if request.path == "/info" && body["type"] == "frontendOpenOrders" {
            return (200, "[]".to_string());
        }
        if request.path == "/info" && body["type"] == "allMids" {
            return (200, r#"{"kPEPE":"0.02"}"#.to_string());
        }
        if request.path == "/exchange" {
            return (200, resting(91));
        }
        answer(request)
    })
    .await;
    let mut gw = HyperliquidGateway::for_test(
        &server.base_url(),
        HyperliquidRealm::Testnet,
        HyperliquidRealm::Testnet.credentials_for_test(ACCOUNT, WALLET_KEY),
        vec!["KPEPEUSDT".to_string()],
    )
    .expect("test credentials");

    // The instrument the engine is offered is reachable again.
    let rules = gw.instrument_rules().await.expect("rules");
    assert!(rules.iter().any(|(symbol, _)| symbol == "KPEPEUSDT"));

    gw.set_stop(SymbolId(0), 0.018)
        .await
        .expect("the stop found its position");
    let placed = action(&server.to_path("/exchange")[0]);
    assert_eq!(placed["type"], "order");

    // And a market order finds a reference price under the venue's spelling.
    gw.send_order(&OrderRequest {
        client_order_id: "eng-1700000000000-2".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 1000.0,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
        close_position: false,
    })
    .await
    .expect("a market order priced off allMids");
}

#[tokio::test]
async fn a_moved_stop_is_placed_before_the_old_one_is_pulled() {
    // The other order leaves the position bare for a round trip, and bare for
    // good if the placement then fails — which is the state this call exists
    // to prevent.
    let server = TestServer::start(|request, _| {
        let body: Value = request.json();
        if request.path == "/info" && body["type"] == "clearinghouseState" {
            return (
                200,
                r#"{"marginSummary":{"accountValue":"1000","totalMarginUsed":"100"},
                    "withdrawable":"900",
                    "assetPositions":[{"position":{"coin":"BTC","szi":"0.01","entryPx":"94000"}}]}"#
                    .to_string(),
            );
        }
        if request.path == "/info" && body["type"] == "frontendOpenOrders" {
            return (
                200,
                r#"[{"coin":"BTC","side":"A","sz":"0.01","origSz":"0.01","oid":55,
                     "reduceOnly":true,"isTrigger":true,"orderType":"Stop Market",
                     "triggerPx":"90000"}]"#
                    .to_string(),
            );
        }
        if request.path == "/exchange" {
            return (200, resting(88));
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    gw.set_stop(SymbolId(0), 92_000.0)
        .await
        .expect("the stop moved");

    let sent = server.to_path("/exchange");
    assert_eq!(sent.len(), 2, "a place and a cancel");
    assert_eq!(
        action(&sent[0])["type"],
        "order",
        "the replacement must go out before the old stop is pulled"
    );
    assert_eq!(action(&sent[1])["type"], "cancel");
    assert_eq!(
        action(&sent[1])["cancels"][0]["o"],
        55,
        "the old stop, by its own id"
    );
}

#[tokio::test]
async fn an_amend_keeps_the_half_it_was_not_asked_to_change() {
    // The venue's modify replaces the whole order, so an amend that only moves
    // the price still has to say what the size is — and it reads it back off
    // the venue rather than assuming.
    let server = TestServer::start(|request, _| {
        if request.path == "/exchange" {
            return (200, resting(88));
        }
        let body: Value = request.json();
        if body["type"] == "frontendOpenOrders" {
            let ours = format!(
                r#"[{{"coin":"BTC","side":"B","sz":"0.004","origSz":"0.01","oid":77,
                     "limitPx":"94000","reduceOnly":false,"isTrigger":false,
                     "orderType":"Limit","tif":"Alo","cloid":"{}"}}]"#,
                // The same derivation the gateway uses; a mismatch here would
                // make the amend say the order is not working.
                cloid_of("eng-1700000000000-1")
            );
            return (200, ours);
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    gw.amend_order(
        SymbolId(0),
        "eng-1700000000000-1",
        AmendSpec {
            px: Some(93_500.0),
            qty: None,
        },
    )
    .await
    .unwrap();

    let action = action(&server.to_path("/exchange")[0]);
    assert_eq!(action["type"], "batchModify");
    let order = &action["modifies"][0]["order"];
    assert_eq!(order["p"], "93500", "the new price");
    assert_eq!(order["s"], "0.004", "the size the venue still has working");
    assert_eq!(order["b"], true, "the side it was already on");
    assert_eq!(
        order["t"]["limit"]["tif"], "Alo",
        "the resting order was post-only; re-quoting it must not let it cross"
    );
}

#[tokio::test]
async fn an_amend_refuses_rather_than_guess_a_time_in_force() {
    // A row without a time-in-force is a row that cannot be replaced without
    // deciding whether the order may cross, and that is not a decision to make
    // silently on the way past.
    let server = TestServer::start(|request, _| {
        if request.path == "/exchange" {
            return (200, resting(88));
        }
        let body: Value = request.json();
        if body["type"] == "frontendOpenOrders" {
            let ours = format!(
                r#"[{{"coin":"BTC","side":"B","sz":"0.004","origSz":"0.01","oid":77,
                     "limitPx":"94000","reduceOnly":false,"isTrigger":false,
                     "orderType":"Limit","cloid":"{}"}}]"#,
                cloid_of("eng-1700000000000-1")
            );
            return (200, ours);
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let refused = gw
        .amend_order(
            SymbolId(0),
            "eng-1700000000000-1",
            AmendSpec {
                px: Some(93_500.0),
                qty: None,
            },
        )
        .await
        .unwrap_err();
    assert!(refused.to_string().contains("time-in-force"), "{refused}");
    assert!(
        server.to_path("/exchange").is_empty(),
        "an order went out anyway"
    );
}

#[tokio::test]
async fn an_amend_that_changes_nothing_is_refused_before_a_round_trip() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let refused = gw
        .amend_order(
            SymbolId(0),
            "eng-1",
            AmendSpec {
                px: None,
                qty: None,
            },
        )
        .await;
    assert!(
        matches!(refused, Err(VenueError::BadRequest(_))),
        "{refused:?}"
    );
    assert!(server.to_path("/exchange").is_empty());
}

#[tokio::test]
async fn a_position_is_unprotected_until_a_stop_order_stands_against_it() {
    // This venue keeps no stop on the position row, so the account read has to
    // look at the open orders — and a position with no stop must come back
    // unprotected, which is what holds new risk back.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let view = gw.account_view().await.unwrap();
    assert_eq!(view.equity_usdt, 1500.25);
    assert_eq!(view.available_usdt, 1200.5);
    assert_eq!(view.positions.len(), 1);
    assert!(view.positions[0].stop_attached);
    assert_eq!(view.positions[0].stop_px, 93_000.0);
    assert_eq!(view.positions[0].leverage, Some(20.0));

    let bare = TestServer::start(|request, _| {
        let body: Value = request.json();
        if request.path == "/info" && body["type"] == "frontendOpenOrders" {
            return (200, "[]".to_string());
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&bare);
    let view = gw.account_view().await.unwrap();
    assert!(
        !view.positions[0].stop_attached,
        "a position with no stop order read as protected"
    );
}

#[tokio::test]
async fn instrument_rules_come_from_the_venues_own_asset_list() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let rules = gw.instrument_rules().await.unwrap();
    let (name, rule) = rules
        .iter()
        .find(|(n, _)| n == "BTCUSDT")
        .expect("the venue lists BTC");
    assert_eq!(name, "BTCUSDT");
    assert_eq!(rule.qty_step, 1e-5);
    assert_eq!(rule.min_notional, 10.0);
}

#[tokio::test]
async fn a_refusal_buried_in_a_successful_reply_is_still_a_refusal() {
    // The trap this venue sets: the request succeeded and the order did not.
    // Reading only the envelope would log an order that never existed.
    let server = TestServer::start(|request, _| {
        if request.path == "/exchange" {
            return (
                200,
                r#"{"status":"ok","response":{"type":"order","data":{"statuses":[
                    {"error":"Order price cannot be more than 95% away from the reference price"}
                ]}}}"#
                    .to_string(),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let refused = gw
        .send_order(&entry(
            OrderKind::Limit {
                px: 1.0,
                tif: TimeInForce::Gtc,
            },
            None,
        ))
        .await;
    match refused {
        Err(VenueError::Rejected { message, .. }) => assert!(message.contains("95%"), "{message}"),
        other => panic!("expected a rejection, got {other:?}"),
    }
}

#[tokio::test]
async fn an_entry_accepted_with_its_stop_refused_is_a_refusal() {
    // The half-accepted case. Recording this as a placed order would leave the
    // engine believing a position is protected when nothing is watching it.
    let server = TestServer::start(|request, _| {
        if request.path == "/exchange" {
            return (
                200,
                r#"{"status":"ok","response":{"type":"order","data":{"statuses":[
                    {"resting":{"oid":1}},
                    {"error":"Invalid trigger price"}
                ]}}}"#
                    .to_string(),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let refused = gw
        .send_order(&entry(
            OrderKind::Limit {
                px: 94_000.0,
                tif: TimeInForce::Gtc,
            },
            Some(StopSpec {
                trigger_px: 93_000.0,
            }),
        ))
        .await;
    assert!(
        matches!(refused, Err(VenueError::Rejected { .. })),
        "{refused:?}"
    );
}

#[tokio::test]
async fn a_whole_request_failure_carries_the_venues_words() {
    let server = TestServer::start(|request, _| {
        if request.path == "/exchange" {
            return (
                200,
                r#"{"status":"err","response":"Insufficient margin to place order."}"#.to_string(),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let refused = gw
        .send_order(&entry(
            OrderKind::Limit {
                px: 94_000.0,
                tif: TimeInForce::Gtc,
            },
            None,
        ))
        .await;
    match refused {
        Err(VenueError::Rejected { message, .. }) => {
            assert!(message.contains("Insufficient margin"), "{message}")
        }
        other => panic!("expected a rejection, got {other:?}"),
    }
}

#[tokio::test]
async fn leverage_is_a_whole_number_and_is_capped_at_the_assets_maximum() {
    let server = TestServer::start(|request, _| {
        if request.path == "/exchange" {
            return (
                200,
                r#"{"status":"ok","response":{"type":"default"}}"#.to_string(),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    gw.set_leverage(SymbolId(0), 2.9).await.unwrap();
    let asked = action(&server.to_path("/exchange")[0]);
    assert_eq!(asked["type"], "updateLeverage");
    assert_eq!(asked["asset"], 0);
    assert_eq!(asked["isCross"], true);
    // Rounded DOWN: asking for 2.9 and getting 3 would post less margin than
    // the risk kernel priced the position at.
    assert_eq!(asked["leverage"], 2);

    gw.set_leverage(SymbolId(0), 500.0).await.unwrap();
    let capped = action(&server.to_path("/exchange")[1]);
    assert_eq!(capped["leverage"], 40, "the asset's own maximum");
}

#[tokio::test]
async fn the_account_identity_names_the_venue_the_account_and_the_realm() {
    // The account here has approved the key this host signs with, which is the
    // ordinary arrangement: an API wallet trading for an account it cannot
    // withdraw from.
    let agent = {
        let idle = TestServer::start(|request, _| answer(request)).await;
        gateway(&idle).signer_address()
    };
    let server = TestServer::start(move |request, _| {
        let body: Value = request.json();
        if request.path == "/info" && body["type"] == "extraAgents" {
            return (
                200,
                format!(r#"[{{"address":"{agent}","name":"engine","validUntil":0}}]"#),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let who = gw.account_identity().await.unwrap();
    assert_eq!(who.venue, "hyperliquid");
    assert_eq!(who.realm, "hyperliquid_testnet");
    assert_eq!(who.user_id, ACCOUNT);
}

#[tokio::test]
async fn a_key_the_account_never_approved_stops_the_engine_before_it_trades() {
    // The key here signs as an address the account has not approved, and
    // `extraAgents` comes back empty. Every order it sent would be refused by
    // the venue; saying so at boot is the difference between a clear failure
    // and an engine that looks healthy and places nothing.
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    match gw.account_identity().await {
        Err(VenueError::Credentials(said)) => {
            assert!(said.contains("API wallet"), "{said}");
            assert!(
                said.contains(ACCOUNT),
                "the refusal should name the account: {said}"
            );
        }
        other => panic!("an unapproved key was accepted: {other:?}"),
    }
}

/// The same client-id derivation the gateway uses, so a fixture can name an
/// order the gateway will recognise.
fn cloid_of(client_order_id: &str) -> String {
    // `eng-<boot ms>-<n>` packs into the venue's sixteen bytes: a version
    // byte, six bytes of milliseconds, five of counter, four spare.
    let rest = client_order_id.strip_prefix("eng-").expect("an engine id");
    let (boot, counter) = rest.split_once('-').expect("an engine id");
    let boot: u64 = boot.parse().unwrap();
    let counter: u64 = counter.parse().unwrap();
    let mut bytes = [0u8; 16];
    bytes[0] = 0x01;
    bytes[1..7].copy_from_slice(&boot.to_be_bytes()[2..]);
    bytes[7..12].copy_from_slice(&counter.to_be_bytes()[3..]);
    format!("0x{}", hex::encode(bytes))
}
