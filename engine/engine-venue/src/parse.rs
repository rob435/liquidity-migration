//! Reading Bybit v5 replies. Pure functions over JSON, so the mapping is
//! testable without a socket.
//!
//! Bybit sends every number as a string. A field we cannot read is a
//! `BadReply` — never a zero, never a default. Guessing here would hand the
//! risk kernel a picture of an account that does not exist.

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{InstrumentRule, OrderAck, VenueOrder};
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
/// A position in a symbol no strategy subscribed to belongs to somebody else
/// — the owner's own hand-trading, or another process on the same venue
/// account. It is named in the log and skipped: the engine can only place an
/// order on a symbol a strategy subscribed to, so a position it cannot even
/// address is not exposure it can create, hide, or reduce. Refusing to read
/// the account over one would mean an account holding anything unfamiliar
/// stops the engine dead.
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
        let Some(id) = resolve(&symbol) else {
            report_foreign_once(&symbol, qty);
            continue;
        };
        // Bybit writes "" or "0" when no stop is set on the position.
        let stop_attached = opt_num_field(row, "stopLoss")?.is_some_and(|v| v > 0.0);
        // The leverage the venue says this position runs at. Zero or absent
        // reads as unknown (cross-margin rows can blank it), never as a fact.
        let leverage = opt_num_field(row, "leverage")?.filter(|v| *v > 0.0);
        out.push(PositionView {
            symbol: id,
            side,
            qty,
            entry_px: num_field(row, "avgPrice")?,
            stop_attached,
            leverage,
        });
    }
    Ok((out, next_cursor(result)))
}

/// Every order the venue says is working, whoever placed it.
///
/// Unlike positions, nothing is skipped here. A position in a symbol no
/// strategy subscribed to cannot be addressed and so is not the engine's
/// business; an order is different — it is evidence about who else is on this
/// account, and boot has to be able to say so. Symbols stay in the venue's own
/// spelling for the same reason: the caller decides what it recognises.
pub(crate) fn parse_working_orders(
    result: &Value,
) -> Result<(Vec<VenueOrder>, String), VenueError> {
    let rows = list_field(result)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let symbol = str_field(row, "symbol")?;
        let side = match str_field(row, "side")?.as_str() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            other => {
                return Err(VenueError::BadReply(format!(
                    "working order in {symbol} has an unknown side {other:?}"
                )))
            }
        };
        out.push(VenueOrder {
            // The exchange's own orders — the stop it attaches to a position —
            // carry no id of ours. Kept rather than dropped: an empty id is
            // how the caller tells them apart from a second writer's order.
            client_order_id: str_field(row, "orderLinkId").unwrap_or_default(),
            symbol,
            side,
            qty: num_field(row, "qty")?,
            filled_qty: opt_num_field(row, "cumExecQty")?.unwrap_or(0.0),
            reduce_only: row.get("reduceOnly").and_then(Value::as_bool).unwrap_or(false),
        });
    }
    Ok((out, next_cursor(result)))
}

/// Say once that somebody else's position is there, not on every account
/// refresh. The account is read every couple of seconds, so a line per read
/// is tens of thousands of lines a day that bury everything worth seeing.
fn report_foreign_once(symbol: &str, qty: f64) {
    thread_local! {
        static SAID: std::cell::RefCell<std::collections::BTreeSet<String>> =
            const { std::cell::RefCell::new(std::collections::BTreeSet::new()) };
    }
    let first = SAID.with(|said| said.borrow_mut().insert(symbol.to_string()));
    if first {
        tracing::warn!(
            symbol,
            qty,
            "position in a symbol no strategy trades; left alone as somebody else's"
        );
    }
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
             "stopLoss": "93000", "leverage": "3", "positionIdx": 0},
            {"symbol": "ETHUSDT", "side": "Sell", "size": "1.5", "avgPrice": "3000",
             "stopLoss": "0", "leverage": "", "positionIdx": 0},
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
        assert_eq!(
            positions[0].leverage,
            Some(3.0),
            "the venue's own leverage answer rides along on the row"
        );
        assert_eq!(
            positions[1].leverage, None,
            "a blank leverage reads as unknown, never as a number"
        );

        // "0" is Bybit for "no stop", not for "a stop at zero".
        assert!(!positions[1].stop_attached);
        assert_eq!(positions[1].side, Side::Sell);
    }

    #[test]
    fn somebody_elses_position_is_skipped_and_does_not_stop_the_read() {
        // The owner hand-trades the same venue account, so a position in a
        // symbol no strategy subscribed to is the normal case, not a fault.
        // The engine cannot place an order on an unsubscribed symbol, so it
        // reads past it and keeps the positions that are its own.
        let result = json!({"list": [
            {"symbol": "VANRYUSDT", "side": "Buy", "size": "100", "avgPrice": "0.1",
             "stopLoss": ""},
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.5", "avgPrice": "95000.5",
             "stopLoss": "90000"}
        ]});
        let (positions, _) = parse_positions(&result, &resolver()).unwrap();
        assert_eq!(positions.len(), 1, "only the engine's own symbol survives");
        assert_eq!(positions[0].qty, 0.5);
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
