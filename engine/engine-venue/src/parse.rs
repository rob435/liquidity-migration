//! Reading Bybit v5 replies. Pure functions over JSON, so the mapping is
//! testable without a socket.
//!
//! Bybit sends every number as a string. A field we cannot read is a
//! `BadReply` — never a zero, never a default. Guessing here would hand the
//! risk kernel a picture of an account that does not exist.

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{InstrumentRule, OrderAck};
use engine_types::risk::PositionView;
use engine_types::{Side, VenueError};
use serde_json::Value;

/// Unwrap the `retCode` envelope every v5 endpoint shares.
pub(crate) fn venue_result(envelope: Value) -> Result<Value, VenueError> {
    let mut obj = match envelope {
        Value::Object(o) => o,
        other => {
            return Err(VenueError::BadReply(format!(
                "expected a JSON object, got {}",
                kind_of(&other)
            )))
        }
    };
    let code = obj
        .get("retCode")
        .and_then(Value::as_i64)
        .ok_or_else(|| VenueError::BadReply("reply carries no retCode".to_string()))?;
    if code != 0 {
        let message = obj
            .get("retMsg")
            .and_then(Value::as_str)
            .unwrap_or("no retMsg")
            .to_string();
        return Err(VenueError::Rejected { code, message });
    }
    Ok(obj.remove("result").unwrap_or(Value::Null))
}

pub(crate) fn parse_order_ack(
    result: &Value,
    client_order_id: &str,
    ack_ns: u64,
) -> Result<OrderAck, VenueError> {
    let venue_order_id = str_field(result, "orderId")?;
    if venue_order_id.is_empty() {
        return Err(VenueError::BadReply("accepted order has a blank orderId".to_string()));
    }
    Ok(OrderAck {
        client_order_id: client_order_id.to_string(),
        venue_order_id,
        ack_ns,
    })
}

/// Instrument rules for one page, with the cursor for the next one ("" when
/// this was the last).
pub(crate) fn parse_instruments(
    result: &Value,
) -> Result<(Vec<(Symbol, InstrumentRule)>, String), VenueError> {
    let rows = list_field(result)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let symbol = str_field(row, "symbol")?;
        let price_filter = row.get("priceFilter");
        let lot_filter = row.get("lotSizeFilter");
        let (Some(price_filter), Some(lot_filter)) = (price_filter, lot_filter) else {
            tracing::warn!(symbol, "instrument has no price or lot filter; not tradable");
            continue;
        };
        let tick_size = num_field(price_filter, "tickSize")?;
        let qty_step = num_field(lot_filter, "qtyStep")?;
        let min_qty = num_field(lot_filter, "minOrderQty")?;
        // Not every contract publishes a minimum notional; zero means the
        // venue does not impose one.
        let min_notional = opt_num_field(lot_filter, "minNotionalValue")?.unwrap_or(0.0);
        if tick_size <= 0.0 || qty_step <= 0.0 {
            tracing::warn!(symbol, tick_size, qty_step, "instrument has a zero tick or step");
            continue;
        }
        out.push((
            symbol,
            InstrumentRule {
                tick_size,
                qty_step,
                min_qty,
                min_notional,
            },
        ));
    }
    Ok((out, next_cursor(result)))
}

/// Total equity and available balance, in USDT.
///
/// Bybit blanks these totals in some margin modes — the Python fleet was bitten
/// by exactly that on 2026-08-04. A blank is unreadable state and fails here;
/// reading it as zero would look like an emptied account and trip the loss
/// guard.
pub(crate) fn parse_wallet(result: &Value) -> Result<(f64, f64), VenueError> {
    let rows = list_field(result)?;
    let row = rows
        .first()
        .ok_or_else(|| VenueError::BadReply("wallet reply has no account row".to_string()))?;
    let equity = num_field(row, "totalEquity")?;
    let available = num_field(row, "totalAvailableBalance")?;
    Ok((equity, available))
}

/// Open positions, with the cursor for the next page.
///
/// A position in a symbol the engine has not interned cannot be given a
/// `SymbolId`, and dropping it would hide live exposure from the risk kernel.
/// So it fails, naming the symbol: the operator either subscribes it or
/// flattens it.
pub(crate) fn parse_positions(
    result: &Value,
    resolve: &dyn Fn(&str) -> Option<SymbolId>,
) -> Result<(Vec<PositionView>, String), VenueError> {
    let rows = list_field(result)?;
    let mut out = Vec::new();
    for row in rows {
        let symbol = str_field(row, "symbol")?;
        let qty = num_field(row, "size")?;
        let side_raw = str_field(row, "side")?;
        // A flat position comes back with size 0 and a blank side.
        if qty <= 0.0 || side_raw.is_empty() || side_raw == "None" {
            continue;
        }
        let side = match side_raw.as_str() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            other => {
                return Err(VenueError::BadReply(format!(
                    "position in {symbol} has an unknown side {other:?}"
                )))
            }
        };
        let id = resolve(&symbol).ok_or_else(|| {
            VenueError::BadReply(format!(
                "open position in {symbol}, which the engine does not know; \
                 register the symbol or flatten it"
            ))
        })?;
        // Bybit writes "" or "0" when no stop is set on the position.
        let stop_attached = opt_num_field(row, "stopLoss")?.is_some_and(|v| v > 0.0);
        out.push(PositionView {
            symbol: id,
            side,
            qty,
            entry_px: num_field(row, "avgPrice")?,
            stop_attached,
        });
    }
    Ok((out, next_cursor(result)))
}

fn next_cursor(result: &Value) -> String {
    result
        .get("nextPageCursor")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn list_field(result: &Value) -> Result<&Vec<Value>, VenueError> {
    result
        .get("list")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("reply carries no result.list".to_string()))
}

pub(crate) fn str_field(obj: &Value, name: &str) -> Result<String, VenueError> {
    obj.get(name)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| VenueError::BadReply(format!("field {name} is missing or not a string")))
}

pub(crate) fn num_field(obj: &Value, name: &str) -> Result<f64, VenueError> {
    opt_num_field(obj, name)?
        .ok_or_else(|| VenueError::BadReply(format!("field {name} is missing or blank")))
}

/// `None` means present-but-blank or absent; an unparseable value is an error.
pub(crate) fn opt_num_field(obj: &Value, name: &str) -> Result<Option<f64>, VenueError> {
    match obj.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.trim().is_empty() => Ok(None),
        Some(Value::String(s)) => s
            .trim()
            .parse::<f64>()
            .map(Some)
            .map_err(|_| VenueError::BadReply(format!("field {name} is not a number: {s:?}"))),
        Some(Value::Number(n)) => n
            .as_f64()
            .map(Some)
            .ok_or_else(|| VenueError::BadReply(format!("field {name} is not a finite number"))),
        Some(other) => Err(VenueError::BadReply(format!(
            "field {name} is a {}, not a number",
            kind_of(other)
        ))),
    }
}

fn kind_of(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_non_zero_retcode_is_a_rejection() {
        let envelope = json!({"retCode": 110007, "retMsg": "ab not enough for new order"});
        match venue_result(envelope) {
            Err(VenueError::Rejected { code, message }) => {
                assert_eq!(code, 110007);
                assert_eq!(message, "ab not enough for new order");
            }
            other => panic!("expected Rejected, got {other:?}"),
        }
    }

    #[test]
    fn a_reply_without_a_retcode_is_a_bad_reply() {
        let envelope = json!({"result": {"orderId": "1"}});
        assert!(matches!(
            venue_result(envelope),
            Err(VenueError::BadReply(_))
        ));
    }

    #[test]
    fn order_ack_carries_the_venue_id() {
        let result = json!({"orderId": "1a2b", "orderLinkId": "eng-7"});
        let ack = parse_order_ack(&result, "eng-7", 42).unwrap();
        assert_eq!(ack.venue_order_id, "1a2b");
        assert_eq!(ack.client_order_id, "eng-7");
        assert_eq!(ack.ack_ns, 42);
    }

    #[test]
    fn a_blank_order_id_is_a_bad_reply() {
        let result = json!({"orderId": "", "orderLinkId": "eng-7"});
        assert!(matches!(
            parse_order_ack(&result, "eng-7", 1),
            Err(VenueError::BadReply(_))
        ));
    }

    fn instrument_page(cursor: &str) -> Value {
        json!({
            "category": "linear",
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "status": "Trading",
                    "priceFilter": {"minPrice": "0.10", "maxPrice": "1999999.80", "tickSize": "0.10"},
                    "lotSizeFilter": {
                        "maxOrderQty": "1190.000",
                        "minOrderQty": "0.001",
                        "qtyStep": "0.001",
                        "minNotionalValue": "5"
                    }
                },
                {
                    "symbol": "1000PEPEUSDT",
                    "status": "Trading",
                    "priceFilter": {"tickSize": "0.0000001"},
                    "lotSizeFilter": {"minOrderQty": "100", "qtyStep": "10"}
                }
            ],
            "nextPageCursor": cursor
        })
    }

    #[test]
    fn instruments_parse_with_tick_step_and_minimums() {
        let (rules, cursor) = parse_instruments(&instrument_page("")).unwrap();
        assert_eq!(cursor, "");
        assert_eq!(rules.len(), 2);

        let (name, btc) = &rules[0];
        assert_eq!(name, "BTCUSDT");
        assert_eq!(btc.tick_size, 0.10);
        assert_eq!(btc.qty_step, 0.001);
        assert_eq!(btc.min_qty, 0.001);
        assert_eq!(btc.min_notional, 5.0);

        // No minNotionalValue published: zero, meaning the venue sets none.
        let (name, pepe) = &rules[1];
        assert_eq!(name, "1000PEPEUSDT");
        assert_eq!(pepe.tick_size, 0.0000001);
        assert_eq!(pepe.qty_step, 10.0);
        assert_eq!(pepe.min_qty, 100.0);
        assert_eq!(pepe.min_notional, 0.0);
    }

    #[test]
    fn a_page_hands_back_the_cursor_for_the_next_one() {
        let (_, cursor) = parse_instruments(&instrument_page("page-2-token")).unwrap();
        assert_eq!(cursor, "page-2-token");
    }

    #[test]
    fn instruments_without_filters_are_skipped_not_fatal() {
        let page = json!({
            "list": [
                {"symbol": "GOODUSDT", "priceFilter": {"tickSize": "0.5"},
                 "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "1"}},
                {"symbol": "NOFILTERUSDT"},
                {"symbol": "ZEROTICKUSDT", "priceFilter": {"tickSize": "0"},
                 "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "1"}}
            ],
            "nextPageCursor": ""
        });
        let (rules, _) = parse_instruments(&page).unwrap();
        assert_eq!(rules.len(), 1);
        assert_eq!(rules[0].0, "GOODUSDT");
    }

    #[test]
    fn wallet_totals_are_read_from_the_first_account_row() {
        let result = json!({"list": [{
            "accountType": "UNIFIED",
            "totalEquity": "1234.5678",
            "totalAvailableBalance": "1000.0",
            "totalWalletBalance": "1234.5678",
            "coin": []
        }]});
        assert_eq!(parse_wallet(&result).unwrap(), (1234.5678, 1000.0));
    }

    #[test]
    fn blanked_wallet_totals_are_an_error_not_a_zero() {
        // Seen on the live account: some margin modes return "" here.
        let result = json!({"list": [{
            "accountType": "UNIFIED",
            "totalEquity": "",
            "totalAvailableBalance": ""
        }]});
        assert!(matches!(parse_wallet(&result), Err(VenueError::BadReply(_))));
    }

    fn resolver() -> impl Fn(&str) -> Option<SymbolId> {
        |name: &str| match name {
            "BTCUSDT" => Some(SymbolId(0)),
            "ETHUSDT" => Some(SymbolId(1)),
            _ => None,
        }
    }

    #[test]
    fn positions_parse_and_flat_rows_are_dropped() {
        let result = json!({"list": [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.01", "avgPrice": "95000.5",
             "stopLoss": "93000", "positionIdx": 0},
            {"symbol": "ETHUSDT", "side": "Sell", "size": "1.5", "avgPrice": "3000",
             "stopLoss": "0", "positionIdx": 0},
            {"symbol": "ETHUSDT", "side": "", "size": "0", "avgPrice": "0",
             "stopLoss": "", "positionIdx": 0}
        ], "nextPageCursor": ""});

        let (positions, cursor) = parse_positions(&result, &resolver()).unwrap();
        assert_eq!(cursor, "");
        assert_eq!(positions.len(), 2);

        assert_eq!(positions[0].symbol, SymbolId(0));
        assert_eq!(positions[0].side, Side::Buy);
        assert_eq!(positions[0].qty, 0.01);
        assert_eq!(positions[0].entry_px, 95000.5);
        assert!(positions[0].stop_attached);

        // "0" is Bybit for "no stop", not for "a stop at zero".
        assert!(!positions[1].stop_attached);
        assert_eq!(positions[1].side, Side::Sell);
    }

    #[test]
    fn a_position_the_engine_cannot_name_fails_closed() {
        let result = json!({"list": [
            {"symbol": "VANRYUSDT", "side": "Buy", "size": "100", "avgPrice": "0.1",
             "stopLoss": ""}
        ]});
        let err = parse_positions(&result, &resolver()).unwrap_err();
        match err {
            VenueError::BadReply(msg) => assert!(msg.contains("VANRYUSDT"), "{msg}"),
            other => panic!("expected BadReply, got {other:?}"),
        }
    }

    #[test]
    fn a_reply_missing_its_list_is_a_bad_reply() {
        assert!(matches!(
            parse_wallet(&json!({"nothing": true})),
            Err(VenueError::BadReply(_))
        ));
        assert!(matches!(
            parse_instruments(&json!({"nothing": true})),
            Err(VenueError::BadReply(_))
        ));
    }
}
