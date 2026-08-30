//! The action values, built in the venue's own field order.
//!
//! Every function here returns an [`Mp`], not JSON, because the action is
//! hashed before it is sent and the hash is over the msgpack bytes. The same
//! value is then rendered as JSON for the request body, so what is signed and
//! what is sent are one object built once.
//!
//! Field order is copied from Hyperliquid's Python SDK — `{a, b, p, s, r, t}`
//! and then `c` only when there is a client id, `{type, orders, grouping}` for
//! the action itself. It is not a style choice: reorder any of it and the
//! signature stops verifying.

use engine_types::TimeInForce;
use serde_json::{Map, Value};

use super::msgpack::Mp;

/// The one-letter fields are the venue's: `a` asset, `b` is-buy, `p` price,
/// `s` size, `r` reduce-only, `t` type, `c` client order id.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct OrderWire {
    pub(crate) asset: u32,
    pub(crate) is_buy: bool,
    /// Already rendered to the venue's decimal form. A string, not a float:
    /// the hash has to be reproducible and a float never is.
    pub(crate) px: String,
    pub(crate) sz: String,
    pub(crate) reduce_only: bool,
    pub(crate) kind: OrderKindWire,
    pub(crate) cloid: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) enum OrderKindWire {
    Limit {
        tif: TimeInForce,
    },
    /// A stop or take-profit. `is_market` true crosses when it fires, which is
    /// what a protective stop wants: a limit stop that cannot fill is not a
    /// stop.
    Trigger {
        is_market: bool,
        trigger_px: String,
        tpsl: &'static str,
    },
}

/// Take-profit / stop-loss, as the venue spells them.
pub(crate) const TPSL_STOP: &str = "sl";

/// The venue's spelling read back, for an amend that must not change what it
/// was not asked to change. An unknown spelling is `None` rather than a guess:
/// guessing `Gtc` is what turns a maker order into a taker one.
pub(crate) fn tif_of(spelled: &str) -> Option<TimeInForce> {
    match spelled {
        "Gtc" => Some(TimeInForce::Gtc),
        "Ioc" => Some(TimeInForce::Ioc),
        "Alo" => Some(TimeInForce::PostOnly),
        _ => None,
    }
}

/// `Alo` — "add liquidity only" — is Hyperliquid's post-only.
pub(crate) fn tif_str(tif: TimeInForce) -> &'static str {
    match tif {
        TimeInForce::Gtc => "Gtc",
        TimeInForce::Ioc => "Ioc",
        TimeInForce::PostOnly => "Alo",
    }
}

fn order_wire(order: &OrderWire) -> Mp {
    let kind = match &order.kind {
        OrderKindWire::Limit { tif } => Mp::Map(vec![(
            "limit",
            Mp::Map(vec![("tif", Mp::str(tif_str(*tif)))]),
        )]),
        OrderKindWire::Trigger {
            is_market,
            trigger_px,
            tpsl,
        } => Mp::Map(vec![(
            "trigger",
            Mp::Map(vec![
                ("isMarket", Mp::Bool(*is_market)),
                ("triggerPx", Mp::str(trigger_px.clone())),
                ("tpsl", Mp::str(*tpsl)),
            ]),
        )]),
    };
    let mut fields = vec![
        ("a", Mp::Int(i64::from(order.asset))),
        ("b", Mp::Bool(order.is_buy)),
        ("p", Mp::str(order.px.clone())),
        ("s", Mp::str(order.sz.clone())),
        ("r", Mp::Bool(order.reduce_only)),
        ("t", kind),
    ];
    if let Some(cloid) = &order.cloid {
        fields.push(("c", Mp::str(cloid.clone())));
    }
    Mp::Map(fields)
}

/// `grouping` is how the venue is told what the orders mean together:
/// `na` for independent orders, `normalTpsl` for a parent order with the
/// stop that arms when it fills, `positionTpsl` for a stop against the whole
/// position.
pub(crate) fn order_action(orders: Vec<OrderWire>, grouping: &'static str) -> Mp {
    Mp::Map(vec![
        ("type", Mp::str("order")),
        ("orders", Mp::Arr(orders.iter().map(order_wire).collect())),
        ("grouping", Mp::str(grouping)),
    ])
}

pub(crate) const GROUPING_NONE: &str = "na";
pub(crate) const GROUPING_ORDER_TPSL: &str = "normalTpsl";
pub(crate) const GROUPING_POSITION_TPSL: &str = "positionTpsl";

/// Cancel by the client id the engine minted, never by the venue's own order
/// number: the engine knows its own ids without a lookup, and a lookup is a
/// round trip in front of a cancel.
pub(crate) fn cancel_by_cloid_action(asset: u32, cloid: &str) -> Mp {
    Mp::Map(vec![
        ("type", Mp::str("cancelByCloid")),
        (
            "cancels",
            Mp::Arr(vec![Mp::Map(vec![
                ("asset", Mp::Int(i64::from(asset))),
                ("cloid", Mp::str(cloid)),
            ])]),
        ),
    ])
}

/// Cancel by the venue's own order number. Used only where the engine holds
/// an order it did not name — above all the stop it is about to replace,
/// which the venue created itself and which therefore carries no client id.
pub(crate) fn cancel_action(asset: u32, oid: i64) -> Mp {
    Mp::Map(vec![
        ("type", Mp::str("cancel")),
        (
            "cancels",
            Mp::Arr(vec![Mp::Map(vec![
                ("a", Mp::Int(i64::from(asset))),
                ("o", Mp::Int(oid)),
            ])]),
        ),
    ])
}

/// Reprice or resize in place. The order is named by its client id, which the
/// venue accepts wherever it accepts an order number.
pub(crate) fn modify_action(cloid: &str, order: OrderWire) -> Mp {
    Mp::Map(vec![
        ("type", Mp::str("batchModify")),
        (
            "modifies",
            Mp::Arr(vec![Mp::Map(vec![
                ("oid", Mp::str(cloid)),
                ("order", order_wire(&order)),
            ])]),
        ),
    ])
}

pub(crate) fn update_leverage_action(asset: u32, is_cross: bool, leverage: i64) -> Mp {
    Mp::Map(vec![
        ("type", Mp::str("updateLeverage")),
        ("asset", Mp::Int(i64::from(asset))),
        ("isCross", Mp::Bool(is_cross)),
        ("leverage", Mp::Int(leverage)),
    ])
}

/// The same value again as JSON, for the request body.
///
/// One value produces both the hash and the bytes, so the two cannot describe
/// different orders. JSON key order does not matter to the venue — only the
/// msgpack order does — but it comes out in the same order anyway because it
/// is built from the same vector.
pub(crate) fn to_json(value: &Mp) -> Value {
    match value {
        Mp::Str(text) => Value::String(text.clone()),
        Mp::Int(n) => Value::Number((*n).into()),
        Mp::Bool(b) => Value::Bool(*b),
        Mp::Arr(items) => Value::Array(items.iter().map(to_json).collect()),
        Mp::Map(entries) => {
            let mut map = Map::new();
            for (key, item) in entries {
                map.insert((*key).to_string(), to_json(item));
            }
            Value::Object(map)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::venues::hyperliquid::msgpack::encode;

    /// The fixmap byte that opens the first order inside an order action:
    /// `0x80 | field count`. Found by stepping past the action's own header
    /// and its first two keys, which is stable because this module writes the
    /// action in one fixed shape.
    fn order_map_header(bytes: &[u8]) -> u8 {
        let orders_at = bytes
            .windows(7)
            .position(|w| w == [0xa6, b'o', b'r', b'd', b'e', b'r', b's'])
            .expect("the action carries an `orders` key");
        // key, then the array header, then the first order's map header.
        bytes[orders_at + 7 + 1]
    }

    fn limit(cloid: Option<&str>) -> OrderWire {
        OrderWire {
            asset: 4,
            is_buy: true,
            px: "1670.1".to_string(),
            sz: "0.0147".to_string(),
            reduce_only: false,
            kind: OrderKindWire::Limit {
                tif: TimeInForce::Ioc,
            },
            cloid: cloid.map(str::to_string),
        }
    }

    #[test]
    fn post_only_is_spelled_alo() {
        // Not a synonym the venue also accepts: "PostOnly" is silently not a
        // time-in-force here, and an order carrying it would rest as GTC and
        // pay the taker fee when it crossed.
        assert_eq!(tif_str(TimeInForce::PostOnly), "Alo");
        assert_eq!(tif_str(TimeInForce::Gtc), "Gtc");
        assert_eq!(tif_str(TimeInForce::Ioc), "Ioc");
    }

    #[test]
    fn a_client_id_is_absent_rather_than_null_when_there_is_none() {
        // An extra key changes the hash, so "c": null is not the same request
        // as no "c" at all.
        // Counted off the map header rather than searched for in the bytes:
        // "Ioc" carries a c of its own, and a substring search would pass
        // whatever the encoder did.
        let with = encode(&order_action(vec![limit(Some("0xab"))], GROUPING_NONE));
        let without = encode(&order_action(vec![limit(None)], GROUPING_NONE));
        assert_eq!(order_map_header(&with), 0x80 | 7);
        assert_eq!(order_map_header(&without), 0x80 | 6);
        assert!(with.len() > without.len());
    }

    #[test]
    fn the_json_body_says_the_same_thing_as_the_hashed_bytes() {
        let action = order_action(vec![limit(Some("0xabc"))], GROUPING_ORDER_TPSL);
        let json = to_json(&action);
        assert_eq!(json["type"], "order");
        assert_eq!(json["grouping"], "normalTpsl");
        assert_eq!(json["orders"][0]["a"], 4);
        assert_eq!(json["orders"][0]["b"], true);
        assert_eq!(json["orders"][0]["p"], "1670.1");
        assert_eq!(json["orders"][0]["s"], "0.0147");
        assert_eq!(json["orders"][0]["r"], false);
        assert_eq!(json["orders"][0]["c"], "0xabc");
        assert_eq!(json["orders"][0]["t"]["limit"]["tif"], "Ioc");
    }

    #[test]
    fn a_stop_carries_the_trigger_price_and_says_it_crosses() {
        let stop = OrderWire {
            asset: 0,
            is_buy: false,
            px: "90000".to_string(),
            sz: "0.01".to_string(),
            reduce_only: true,
            kind: OrderKindWire::Trigger {
                is_market: true,
                trigger_px: "90000".to_string(),
                tpsl: TPSL_STOP,
            },
            cloid: None,
        };
        let json = to_json(&order_action(vec![stop], GROUPING_POSITION_TPSL));
        let trigger = &json["orders"][0]["t"]["trigger"];
        assert_eq!(trigger["isMarket"], true);
        assert_eq!(trigger["triggerPx"], "90000");
        assert_eq!(trigger["tpsl"], "sl");
        assert_eq!(json["orders"][0]["r"], true, "a stop may only reduce");
        assert_eq!(json["grouping"], "positionTpsl");
    }

    #[test]
    fn the_other_three_actions_carry_their_own_names() {
        assert_eq!(
            to_json(&cancel_by_cloid_action(3, "0x1"))["type"],
            "cancelByCloid"
        );
        let by_oid = to_json(&cancel_action(3, 99));
        assert_eq!(by_oid["type"], "cancel");
        assert_eq!(by_oid["cancels"][0]["a"], 3);
        assert_eq!(by_oid["cancels"][0]["o"], 99);
        assert_eq!(
            to_json(&cancel_by_cloid_action(3, "0x1"))["cancels"][0]["asset"],
            3
        );
        assert_eq!(
            to_json(&modify_action("0x1", limit(None)))["type"],
            "batchModify"
        );
        let lev = to_json(&update_leverage_action(2, true, 5));
        assert_eq!(lev["type"], "updateLeverage");
        assert_eq!(lev["asset"], 2);
        assert_eq!(lev["isCross"], true);
        assert_eq!(lev["leverage"], 5);
    }
}
