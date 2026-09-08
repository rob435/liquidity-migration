//! What the Hyperliquid gateway actually puts on the wire, checked against a
//! local server. No network, no credentials.
//!
//! The signature itself is pinned by unit tests against the venue's own
//! published vectors. What is checked here is everything around it: which
//! endpoint each method reaches, what the action says, that the entry and its
//! stop travel in one signed request, and that a refusal buried in an
//! otherwise-successful reply is read as a refusal.

use engine_venue::RealmCredentials;

use crate::support::{Recorded, TestServer};
use engine_types::{
    AmendSpec, OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce,
    VenueError, VenueGateway,
};
use engine_venue::{HyperliquidGateway, HyperliquidInventoryProbe, HyperliquidRealm};
use serde_json::Value;

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
        exact_terms: None,
        sleeve_effect: None,
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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
        exact_terms: None,
        sleeve_effect: None,
        close_position: false,
    })
    .await
    .expect("a market order priced off allMids");
}

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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
            exact_terms: None,
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

#[tokio::test(start_paused = true)]
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
                exact_terms: None,
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

#[tokio::test(start_paused = true)]
async fn an_amend_that_changes_nothing_is_refused_before_a_round_trip() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let refused = gw
        .amend_order(
            SymbolId(0),
            "eng-1",
            AmendSpec {
                exact_terms: None,
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
async fn exact_terms_reach_signed_action_and_illegal_grid_is_refused_before_send() {
    use engine_types::numeric::Exact;
    use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let mut request = entry(
        OrderKind::Limit {
            px: 94000.0,
            tif: TimeInForce::Gtc,
        },
        Some(StopSpec {
            trigger_px: 93000.0,
        }),
    );
    let terms = ExactOrderTerms {
        quantity: Exact::parse_decimal("1.23456").unwrap(),
        limit_price: Some(Exact::from_u64(94000)),
        stop_trigger_price: Some(Exact::from_u64(93000)),
        physical_stop_trigger_price: Some(Exact::from_u64(93000)),
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    };
    terms.apply_projection(&mut request).unwrap();
    gw.send_order(&request).await.unwrap();
    let signed = action(&server.to_path("/exchange")[0]);
    assert_eq!(signed["orders"][0]["s"], "1.23456");
    assert_eq!(signed["orders"][0]["p"], "94000");
    assert_eq!(signed["orders"][1]["t"]["trigger"]["triggerPx"], "93000");
    let mut illegal = terms;
    illegal.quantity = Exact::parse_decimal("1.234567").unwrap();
    illegal.apply_projection(&mut request).unwrap();
    assert!(gw.send_order(&request).await.is_err());
    assert_eq!(server.to_path("/exchange").len(), 1);
}

#[tokio::test(start_paused = true)]
async fn independent_catalog_installs_asset_ids_before_a_mutation_without_another_read() {
    let server = TestServer::start(|request, _| answer(request)).await;
    let mut gw = gateway(&server);
    let catalog = gw
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    assert_eq!(catalog.rules.len(), 2);
    assert_eq!(catalog.specs.len(), 2);
    gw.install_instrument_catalog(&catalog).unwrap();
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
        server
            .requests()
            .iter()
            .filter(|r| r.path == "/info" && r.json()["type"] == "meta")
            .count(),
        1
    );
    let other = TestServer::start(|request, _| answer(request)).await;
    assert!(gateway(&other)
        .install_instrument_catalog(&catalog)
        .is_err());
    assert!(other.requests().is_empty());
}

#[tokio::test(start_paused = true)]
async fn exact_market_slippage_uses_the_venue_mid_lexeme_before_rounding() {
    use engine_types::numeric::Exact;
    use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
    let server = TestServer::start(|request, _| {
        if request.path == "/info" {
            match request.json()["type"].as_str().unwrap() {
                "meta" => {
                    return (
                        200,
                        r#"{"universe":[{"name":"BTC","szDecimals":0,"maxLeverage":40}]}"#.into(),
                    )
                }
                "allMids" => return (200, r#"{"BTC":"1.0000000000000000000001"}"#.into()),
                _ => {}
            }
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let mut request = entry(OrderKind::Market, None);
    ExactOrderTerms {
        quantity: Exact::one(),
        limit_price: None,
        stop_trigger_price: None,
        physical_stop_trigger_price: None,
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    }
    .apply_projection(&mut request)
    .unwrap();
    gw.send_order(&request).await.unwrap();
    assert_eq!(
        action(&server.only("/exchange"))["orders"][0]["p"],
        "1.0501",
        "market bound lost the native mid precision before crossing-side rounding"
    );
}

#[tokio::test(start_paused = true)]
async fn exact_amend_requires_the_current_reduce_only_flag_and_keeps_remaining_quantity() {
    use engine_types::numeric::Exact;
    use engine_types::order_terms::{ExactAmendTerms, OrderInputPolicy};
    for present in [false, true] {
        let server=TestServer::start(move|request,_|{
            if request.path=="/exchange"{return(200,resting(88));}
            if request.json()["type"]=="frontendOpenOrders" {
                return(200,format!(r#"[{{"coin":"BTC","side":"B","sz":"0.00401","origSz":"0.01","oid":77,"limitPx":"94000","isTrigger":false,"orderType":"Limit","tif":"Alo","cloid":"{}"{}}}]"#,cloid_of("eng-1700000000000-1"),if present{",\"reduceOnly\":true"}else{""}));
            }
            answer(request)
        }).await;
        let mut gw = gateway(&server);
        let mut spec = AmendSpec {
            px: None,
            qty: None,
            exact_terms: None,
        };
        ExactAmendTerms {
            quantity: None,
            limit_price: Some(Exact::from_i64(93500)),
            input_policy: OrderInputPolicy::StrategyShortestDecimal,
        }
        .apply_projection(&mut spec)
        .unwrap();
        let result = gw
            .amend_order(SymbolId(0), "eng-1700000000000-1", spec)
            .await;
        if present {
            result.unwrap();
            let sent = action(&server.only("/exchange"));
            let order = &sent["modifies"][0]["order"];
            assert_eq!(order["r"], true);
            assert_eq!(order["s"], "0.00401");
            assert_eq!(order["p"], "93500");
            assert_eq!(order["t"]["limit"]["tif"], "Alo");
        } else {
            assert!(
                result.is_err(),
                "unknown reduce-only state became an unrestricted replacement"
            );
            assert!(server.to_path("/exchange").is_empty());
        }
    }
}

#[tokio::test(start_paused = true)]
async fn independent_account_recovery_uses_requested_ids_and_preserves_protection() {
    // This venue keeps no stop on the position row, so the account read has to
    // look at the open orders — and a position with no stop must come back
    // unprotected, which is what holds new risk back.
    let server = TestServer::start(|request, _| answer(request)).await;
    let gw = gateway(&server);
    let view = gw
        .account_recovery_client()
        .expect("independent account recovery client")
        .account_view(&["ETHUSDT".into(), "BTCUSDT".into()])
        .await
        .unwrap();
    assert_eq!(view.positions[0].symbol, SymbolId(1));
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
    let gw = gateway(&bare);
    let view = gw
        .account_recovery_client()
        .expect("independent account recovery client")
        .account_view(&["ETHUSDT".into(), "BTCUSDT".into()])
        .await
        .unwrap();
    assert_eq!(view.positions[0].symbol, SymbolId(1));
    assert!(
        !view.positions[0].stop_attached,
        "a position with no stop order read as protected"
    );
}

#[tokio::test(start_paused = true)]
async fn the_venue_clock_comes_from_exchange_status_and_a_missing_one_is_never_a_zero() {
    let server = TestServer::start(|request, count| {
        let body: Value = request.json();
        if request.path == "/info" && body["type"] == "exchangeStatus" {
            // Observed live: the reply is the bare object, not the
            // `{"status":...,"response":...}` envelope `/exchange` uses.
            return if count == 0 {
                (
                    200,
                    r#"{"specialStatuses":null,"time":1788899787840}"#.to_string(),
                )
            } else {
                (200, r#"{"specialStatuses":null}"#.to_string())
            };
        }
        answer(request)
    })
    .await;
    let gw = gateway(&server);
    assert_eq!(gw.venue_time_ms().await.unwrap(), 1_788_899_787_840);
    // A clock the venue did not state is never a zero the caller can subtract.
    assert!(gw.venue_time_ms().await.is_err());
    let sent = server.to_path("/info");
    assert_eq!(sent.len(), 2);
    assert_eq!(sent[0].method, "POST");
}

/// Answers the three reads one account scan makes. Everything else panics, so
/// a scan that grew a fourth endpoint fails here rather than silently.
fn inventory_answer(request: &Recorded) -> (u16, String) {
    let body: Value = request.json();
    let payload = match body.get("type").and_then(Value::as_str).unwrap_or_default() {
        "clearinghouseState" => {
            r#"{
            "marginSummary":{"accountValue":"1500.25","totalMarginUsed":"300"},
            "withdrawable":"1200.5",
            "assetPositions":[
                {"position":{"coin":"BTC","szi":"0.01","entryPx":"95000"}},
                {"position":{"coin":"kPEPE","szi":"-40","entryPx":"0.01"}},
                {"position":{"coin":"SOL","szi":"0.0","entryPx":"0"}}
            ]
        }"#
        }
        "frontendOpenOrders" => {
            r#"[
            {"coin":"BTC","side":"B","sz":"0.01","origSz":"0.01","oid":77,
             "reduceOnly":false,"isTrigger":false,"orderType":"Limit",
             "cloid":"0x0100000000010000000000010000000000"},
            {"coin":"kPEPE","side":"B","sz":"40","origSz":"40","oid":91,
             "reduceOnly":true,"isTrigger":true,"orderType":"Stop Market",
             "triggerPx":"0.02"}
        ]"#
        }
        "spotClearinghouseState" => {
            r#"{"balances":[
            {"coin":"USDC","token":0,"hold":"0.0","total":"14.6"},
            {"coin":"HYPE","token":1,"hold":"0.0","total":"3.5"},
            {"coin":"PURR","token":2,"hold":"0.0","total":"0"}
        ]}"#
        }
        other => panic!("the account scan must not read {other}"),
    };
    (200, payload.to_string())
}

#[tokio::test(start_paused = true)]
async fn the_account_scan_covers_perps_working_orders_standing_stops_and_spot() {
    let server = TestServer::start(|request, _| inventory_answer(request)).await;
    let mut gw = gateway(&server);
    let scan = VenueGateway::account_inventory(&mut gw).await.unwrap();

    // Both perpetual positions, including the coin no config named. The flat
    // row the venue still lists is not a position.
    let perps: Vec<_> = scan
        .positions
        .iter()
        .filter(|p| p.product == "linear")
        .collect();
    assert_eq!(perps.len(), 2);
    assert_eq!(perps[0].symbol, "BTCUSDT");
    assert_eq!(perps[0].side, Side::Buy);
    assert_eq!(perps[0].qty, 0.01);
    assert_eq!(perps[1].symbol, "KPEPEUSDT");
    assert_eq!(perps[1].side, Side::Sell);
    assert_eq!(perps[1].qty, 40.0);

    // A stop on this venue is a standing reduce-only trigger order, so it is
    // an open order and blocks a flatness claim like any other.
    assert_eq!(scan.open_orders.len(), 2);
    assert_eq!(scan.open_orders[0].product, "linear");
    assert_eq!(scan.open_orders[0].symbol, "BTCUSDT");
    assert_eq!(scan.open_orders[1].product, "trigger");
    assert_eq!(scan.open_orders[1].symbol, "KPEPEUSDT");
    // No cloid on the stop, so the venue's own order number names it.
    assert_eq!(scan.open_orders[1].client_order_id, "oid-91");

    // Spot sits on the same address. Settle cash is not exposure; a token is.
    let wallet: Vec<_> = scan
        .positions
        .iter()
        .filter(|p| p.product == "wallet_asset")
        .collect();
    assert_eq!(wallet.len(), 1);
    assert_eq!(wallet[0].symbol, "HYPE");
    assert_eq!(wallet[0].qty, 3.5);

    assert!(!scan.is_flat());
    assert!(scan.scope.contains("perpetual"));
    assert!(scan.scope.contains("trigger"));
    assert!(scan.scope.contains("spot"));
    assert!(scan.observed_ms > 0);
}

#[tokio::test(start_paused = true)]
async fn an_empty_account_scans_flat_and_a_negative_account_value_does_not() {
    let server = TestServer::start(|request, _| {
        let body: Value = request.json();
        let payload = match body.get("type").and_then(Value::as_str).unwrap_or_default() {
            "clearinghouseState" => {
                r#"{"marginSummary":{"accountValue":"0"},"withdrawable":"0","assetPositions":[]}"#
            }
            "frontendOpenOrders" => "[]",
            "spotClearinghouseState" => r#"{"balances":[]}"#,
            other => panic!("the account scan must not read {other}"),
        };
        (200, payload.to_string())
    })
    .await;
    let mut gw = gateway(&server);
    assert!(VenueGateway::account_inventory(&mut gw)
        .await
        .unwrap()
        .is_flat());

    // Money owed is exposure a flatness proof has to show.
    let owed = TestServer::start(|request, _| {
        let body: Value = request.json();
        let payload = match body.get("type").and_then(Value::as_str).unwrap_or_default() {
            "clearinghouseState" => {
                r#"{"marginSummary":{"accountValue":"-4.5"},"withdrawable":"0","assetPositions":[]}"#
            }
            "frontendOpenOrders" => "[]",
            "spotClearinghouseState" => r#"{"balances":[]}"#,
            other => panic!("the account scan must not read {other}"),
        };
        (200, payload.to_string())
    })
    .await;
    let mut gw = gateway(&owed);
    let scan = VenueGateway::account_inventory(&mut gw).await.unwrap();
    assert!(!scan.is_flat());
    assert_eq!(scan.positions.len(), 1);
    assert_eq!(scan.positions[0].product, "wallet_asset");
    assert_eq!(scan.positions[0].symbol, "USDC");
    assert_eq!(scan.positions[0].side, Side::Sell);
    assert_eq!(scan.positions[0].qty, 4.5);
}

#[tokio::test(start_paused = true)]
async fn the_inventory_probe_reads_the_same_account_and_never_signs_a_mutation() {
    let agent = {
        let idle = TestServer::start(|request, _| answer(request)).await;
        gateway(&idle).signer_address()
    };
    let server = TestServer::start(move |request, _| {
        let body: Value = request.json();
        if body["type"] == "extraAgents" {
            return (
                200,
                format!(r#"[{{"address":"{agent}","name":"engine","validUntil":0}}]"#),
            );
        }
        if body["type"] == "exchangeStatus" || request.path == "/exchange" {
            panic!("the probe must not reach {}", request.path);
        }
        inventory_answer(request)
    })
    .await;
    let creds = HyperliquidRealm::Testnet.credentials_for_test(ACCOUNT, WALLET_KEY);
    let mut probe =
        HyperliquidInventoryProbe::for_test(&server.base_url(), HyperliquidRealm::Testnet, creds)
            .expect("test credentials");

    let who = probe.account_identity().await.unwrap();
    assert_eq!(who.venue, "hyperliquid");
    assert_eq!(who.realm, "hyperliquid_testnet");
    // The account is its address, lower-case `0x` and forty hex digits. This
    // is what EXPECTED_ENGINE_ACCOUNT_USER_ID holds.
    assert_eq!(who.user_id, ACCOUNT);
    assert_eq!(who.user_id.len(), 42);
    assert_eq!(who.user_id, who.user_id.to_ascii_lowercase());

    let scan = probe.account_inventory().await.unwrap();
    assert_eq!(scan.open_orders.len(), 2);
    assert!(server.to_path("/exchange").is_empty());
}

#[tokio::test(start_paused = true)]
async fn a_canary_client_id_hashes_into_the_cloid_and_still_looks_itself_up() {
    // The canary mints `lmcan-…`, not `eng-<ms>-<n>`, so it takes the hashed
    // half of the cloid scheme rather than the packed one. The derivation is
    // still a function of the id, which is all the lookup needs: what the
    // packed form buys — recognising the engine's own orders across a restart
    // — no canary order lives long enough to want.
    let canary_id = "lmcan-1997d1b5cc0-1a2b-0000";
    let server = TestServer::start(move |request, count| {
        let body: Value = request.json();
        if body["type"] == "orderStatus" {
            let cloid = body["oid"].as_str().expect("a cloid, not an oid").to_string();
            assert!(cloid.starts_with("0x02"), "canary ids hash: {cloid}");
            assert_eq!(cloid.len(), 34);
            return (
                200,
                format!(
                    r#"{{"status":"order","order":{{"status":"{}","order":{{"coin":"BTC","cloid":"{cloid}","oid":77,"sz":"0.0","origSz":"0.00012"}}}}}}"#,
                    if count == 0 { "open" } else { "canceled" }
                ),
            );
        }
        answer(request)
    })
    .await;
    let mut gw = gateway(&server);
    let working = VenueGateway::order_status(&mut gw, SymbolId(0), canary_id)
        .await
        .unwrap();
    assert!(matches!(
        working,
        engine_types::orders::OrderLookup::Working(_)
    ));
    let cancelled = VenueGateway::order_status(&mut gw, SymbolId(0), canary_id)
        .await
        .unwrap();
    match cancelled {
        engine_types::orders::OrderLookup::Terminal { status, row } => {
            assert_eq!(status, engine_types::orders::TerminalOrderStatus::Cancelled);
            assert_eq!(row.client_order_id, canary_id);
        }
        other => panic!("a cancelled canary read as {other:?}"),
    }
}
