//! Reading Hyperliquid's replies. Pure functions over JSON, so the mapping is
//! testable without a socket.
//!
//! Two things need care here and both have bitten other adapters.
//!
//! **A reply can say `ok` and still be a rejection.** The envelope's `status`
//! covers the request, not the orders inside it: a well-formed request whose
//! order was refused comes back `{"status":"ok"}` with an `error` string in
//! the per-order status. Reading only the envelope would log an order as
//! placed that the venue never accepted.
//!
//! **A position carries no stop.** Hyperliquid keeps a stop as a separate
//! reduce-only trigger order, not as a field on the position. So whether a
//! position is protected is answered by looking at the open orders, and a
//! position with no stop order against it is reported unprotected — which is
//! what makes the risk kernel's existing stop discipline mean the same thing
//! here as it does on Bybit.

use std::collections::HashMap;

use engine_types::orders::{OrderAck, VenueExecution, VenueOrder};
use engine_types::risk::PositionView;
use engine_types::{Side, SymbolId, VenueError};
use serde_json::Value;

use super::assets::{symbol_of, Asset};
use super::cloid;
use crate::json::{int_field, num_field, opt_num_field, str_field};

/// Unwrap the `{"status": ..., "response": ...}` envelope every `/exchange`
/// reply shares.
pub(crate) fn venue_result(envelope: Value) -> Result<Value, VenueError> {
    let status = envelope
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if status != "ok" {
        // The failure body is a bare string on this venue, not an object.
        let message = match envelope.get("response") {
            Some(Value::String(text)) => text.clone(),
            Some(other) => other.to_string(),
            None => "no response field".to_string(),
        };
        // Hyperliquid sends no numeric codes; zero is "the venue said no and
        // did not number it", which is what `Rejected` means without a code.
        return Err(VenueError::Rejected { code: 0, message });
    }
    Ok(envelope
        .get("response")
        .and_then(|r| r.get("data"))
        .cloned()
        .unwrap_or(Value::Null))
}

/// The per-order status inside an accepted request. This is where a refused
/// order actually says so.
pub(crate) fn first_status(data: &Value) -> Result<Value, VenueError> {
    let statuses = data
        .get("statuses")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("reply carries no statuses".to_string()))?;
    let first = statuses
        .first()
        .ok_or_else(|| VenueError::BadReply("reply carries an empty statuses list".to_string()))?;
    if let Some(message) = first.get("error").and_then(Value::as_str) {
        return Err(VenueError::Rejected {
            code: 0,
            message: message.to_string(),
        });
    }
    Ok(first.clone())
}

/// Every status in an accepted request, refusing if any one of them is an
/// error. Used where one action carried more than one order — an entry with
/// its stop — so a stop the venue refused cannot pass unnoticed.
pub(crate) fn all_accepted(data: &Value) -> Result<Vec<Value>, VenueError> {
    let statuses = data
        .get("statuses")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("reply carries no statuses".to_string()))?;
    if statuses.is_empty() {
        return Err(VenueError::BadReply(
            "reply carries an empty statuses list".to_string(),
        ));
    }
    for status in statuses {
        if let Some(message) = status.get("error").and_then(Value::as_str) {
            return Err(VenueError::Rejected {
                code: 0,
                message: message.to_string(),
            });
        }
    }
    Ok(statuses.clone())
}

/// The venue's order number out of one accepted status. `resting` for an order
/// that is now on the book, `filled` for one that traded on arrival.
pub(crate) fn parse_order_ack(
    status: &Value,
    client_order_id: &str,
    ack_ns: u64,
) -> Result<OrderAck, VenueError> {
    let body = status
        .get("resting")
        .or_else(|| status.get("filled"))
        .ok_or_else(|| {
            VenueError::BadReply(format!(
                "an accepted order is neither resting nor filled: {status}"
            ))
        })?;
    let oid = int_field(body, "oid")?;
    Ok(OrderAck {
        client_order_id: client_order_id.to_string(),
        venue_order_id: oid.to_string(),
        sent_ns: 0,
        ack_ns,
    })
}

/// The venue's asset list out of a `meta` reply. Position in the list is the
/// asset number that goes on the wire.
pub(crate) fn parse_meta(result: &Value) -> Result<Vec<Asset>, VenueError> {
    let universe = result
        .get("universe")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("meta carries no universe".to_string()))?;
    let mut out = Vec::with_capacity(universe.len());
    for (index, row) in universe.iter().enumerate() {
        // A delisted asset keeps its position in the list — the numbers are
        // positional, so it cannot be skipped — but it must not be offered as
        // tradable.
        let delisted = row
            .get("isDelisted")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if delisted {
            continue;
        }
        out.push(Asset {
            coin: str_field(row, "name")?,
            index: u32::try_from(index).map_err(|_| {
                VenueError::BadReply("the venue lists more assets than fit an index".to_string())
            })?,
            sz_decimals: u32::try_from(int_field(row, "szDecimals")?)
                .map_err(|_| VenueError::BadReply("szDecimals is negative".to_string()))?,
            max_leverage: opt_num_field(row, "maxLeverage")?.unwrap_or(1.0),
        });
    }
    if out.is_empty() {
        return Err(VenueError::BadReply(
            "the venue listed no tradable assets".to_string(),
        ));
    }
    Ok(out)
}

/// Equity and free margin out of a `clearinghouseState` reply.
pub(crate) fn parse_margin(result: &Value) -> Result<(f64, f64), VenueError> {
    let summary = result
        .get("marginSummary")
        .ok_or_else(|| VenueError::BadReply("no marginSummary in the account reply".to_string()))?;
    let equity = num_field(summary, "accountValue")?;
    // What the venue says can leave the account is the honest reading of free
    // margin: `accountValue` minus margin used would double-count unrealized
    // P&L on this venue's cross accounting.
    let available = num_field(result, "withdrawable")?;
    Ok((equity, available))
}

/// Open positions out of a `clearinghouseState` reply, with each one's stop
/// taken from the open trigger orders rather than from the position row.
///
/// `resolve` maps the engine's spelling of a symbol to its configured id. Any
/// non-flat position it cannot resolve invalidates the account snapshot:
/// silently dropping exposure is never a safe representation of the account.
pub(crate) fn parse_positions(
    result: &Value,
    stops: &HashMap<String, Stops>,
    resolve: &impl Fn(&str) -> Option<SymbolId>,
) -> Result<Vec<PositionView>, VenueError> {
    let rows = result
        .get("assetPositions")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            VenueError::BadReply("no assetPositions in the account reply".to_string())
        })?;
    let mut out = Vec::new();
    for row in rows {
        let position = row.get("position").ok_or_else(|| {
            VenueError::BadReply("an assetPosition carries no position".to_string())
        })?;
        let coin = str_field(position, "coin")?;
        // Signed: negative is short. A zero row is a position the venue has
        // closed but still lists.
        let signed = num_field(position, "szi")?;
        if signed == 0.0 {
            continue;
        }
        let engine_name = symbol_of(&coin);
        let symbol = resolve(&engine_name).ok_or_else(|| {
            VenueError::BadReply(format!(
                "nonzero position in {engine_name} is absent from the configured symbol table"
            ))
        })?;
        let side = if signed > 0.0 { Side::Buy } else { Side::Sell };
        let stop_px = stops.get(&coin).map(|held| held.nearest(side));
        out.push(PositionView {
            symbol,
            side,
            qty: signed.abs(),
            entry_px: num_field(position, "entryPx")?,
            stop_attached: stop_px.is_some(),
            stop_px: stop_px.unwrap_or(0.0),
            leverage: position
                .get("leverage")
                .and_then(|l| opt_num_field(l, "value").transpose())
                .transpose()?,
        });
    }
    Ok(out)
}

/// The stops standing against each coin, read off the open orders.
///
/// Only a reduce-only stop trigger counts. A take-profit is not protection,
/// and an order that could increase the position is not either.
///
/// Both ends are kept because which of two stops fires first depends on the
/// position's side, and this reply does not carry it: a long is taken out by
/// the higher trigger, a short by the lower. [`parse_positions`] knows the
/// side and picks.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Stops {
    pub(crate) lowest: f64,
    pub(crate) highest: f64,
}

impl Stops {
    /// The one that fires first for a position on this side.
    pub(crate) fn nearest(self, side: Side) -> f64 {
        match side {
            Side::Buy => self.highest,
            Side::Sell => self.lowest,
        }
    }
}

pub(crate) fn stops_by_coin(orders: &Value) -> Result<HashMap<String, Stops>, VenueError> {
    let rows = orders
        .as_array()
        .ok_or_else(|| VenueError::BadReply("the open-order reply is not a list".to_string()))?;
    let mut out: HashMap<String, Stops> = HashMap::new();
    for row in rows {
        if !row
            .get("isTrigger")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        if !row
            .get("reduceOnly")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let kind = row
            .get("orderType")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if !kind.eq_ignore_ascii_case("Stop Market") && !kind.eq_ignore_ascii_case("Stop Limit") {
            continue;
        }
        let Some(trigger) = opt_num_field(row, "triggerPx")? else {
            continue;
        };
        let coin = str_field(row, "coin")?;
        out.entry(coin)
            .and_modify(|held| {
                held.lowest = held.lowest.min(trigger);
                held.highest = held.highest.max(trigger);
            })
            .or_insert(Stops {
                lowest: trigger,
                highest: trigger,
            });
    }
    Ok(out)
}

/// Every order the venue is working, whoever placed it.
pub(crate) fn parse_working_orders(orders: &Value) -> Result<Vec<VenueOrder>, VenueError> {
    let rows = orders
        .as_array()
        .ok_or_else(|| VenueError::BadReply("the open-order reply is not a list".to_string()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let coin = str_field(row, "coin")?;
        let remaining = num_field(row, "sz")?;
        let original = opt_num_field(row, "origSz")?.unwrap_or(remaining);
        // A foreign cloid remains visible to reconciliation. No readable
        // cloid is allowed only on the exact native stop shape this adapter
        // places; otherwise an unattributable live order invalidates the read.
        let client_order_id = match row.get("cloid") {
            Some(Value::String(raw)) if !raw.trim().is_empty() => {
                cloid::from_cloid(raw).unwrap_or_else(|| raw.to_string())
            }
            None | Some(Value::Null) | Some(Value::String(_)) if is_supported_native_stop(row)? => {
                String::new()
            }
            Some(Value::String(_)) | None | Some(Value::Null) => {
                return Err(VenueError::BadReply(format!(
                    "working order in {coin} has no readable cloid and is not a supported native stop"
                )))
            }
            Some(_) => {
                return Err(VenueError::BadReply(format!(
                    "working order in {coin} has a cloid that is not a string"
                )))
            }
        };
        out.push(VenueOrder {
            client_order_id,
            symbol: symbol_of(&coin),
            side: side_of(row)?,
            qty: original,
            filled_qty: (original - remaining).max(0.0),
            reduce_only: row
                .get("reduceOnly")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        });
    }
    Ok(out)
}

fn is_supported_native_stop(row: &Value) -> Result<bool, VenueError> {
    if row.get("isTrigger").and_then(Value::as_bool) != Some(true)
        || row.get("reduceOnly").and_then(Value::as_bool) != Some(true)
    {
        return Ok(false);
    }
    let kind = row
        .get("orderType")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let Some(trigger) = opt_num_field(row, "triggerPx")? else {
        return Ok(false);
    };
    Ok(
        (kind.eq_ignore_ascii_case("Stop Market") || kind.eq_ignore_ascii_case("Stop Limit"))
            && trigger > 0.0,
    )
}

/// Fills out of a `userFillsByTime` reply.
pub(crate) fn parse_executions(fills: &Value) -> Result<Vec<VenueExecution>, VenueError> {
    let rows = fills
        .as_array()
        .ok_or_else(|| VenueError::BadReply("the fill history reply is not a list".to_string()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        out.push(parse_execution(row)?);
    }
    Ok(out)
}

pub(crate) fn parse_execution(row: &Value) -> Result<VenueExecution, VenueError> {
    let coin = str_field(row, "coin")?;
    Ok(VenueExecution {
        // The venue's own trade id. Unique per fill, and the dedup key when
        // history is read twice.
        exec_id: int_field(row, "tid")?.to_string(),
        client_order_id: row
            .get("cloid")
            .and_then(Value::as_str)
            .and_then(cloid::from_cloid)
            .unwrap_or_default(),
        symbol: symbol_of(&coin),
        side: side_of(row)?,
        qty: num_field(row, "sz")?,
        px: num_field(row, "px")?,
        fee: Some(num_field(row, "fee")?),
        // `crossed` says we took liquidity, so the maker share is its opposite.
        is_maker: !row.get("crossed").and_then(Value::as_bool).unwrap_or(true),
        venue_ts_ms: int_field(row, "time")?,
    })
}

/// `A` is the ask side and `B` the bid. Spelled out rather than guessed at:
/// reading these the wrong way round would record every fill on the wrong
/// side of the book.
fn side_of(row: &Value) -> Result<Side, VenueError> {
    match row.get("side").and_then(Value::as_str) {
        Some("A") | Some("a") => Ok(Side::Sell),
        Some("B") | Some("b") => Ok(Side::Buy),
        other => Err(VenueError::BadReply(format!(
            "side is {other:?}, and this venue writes A for ask or B for bid"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_failed_request_is_a_rejection_carrying_the_venues_words() {
        let envelope = json!({"status": "err", "response": "Insufficient margin to place order."});
        match venue_result(envelope) {
            Err(VenueError::Rejected { message, .. }) => {
                assert!(message.contains("Insufficient margin"), "{message}");
            }
            other => panic!("expected a rejection, got {other:?}"),
        }
    }

    #[test]
    fn an_ok_envelope_with_a_refused_order_is_still_a_rejection() {
        // The trap this venue sets: the request succeeded, the order did not.
        // Reading only `status` would log an order that never existed.
        let data = json!({"statuses": [{"error": "Order price cannot be more than 95% away"}]});
        match first_status(&data) {
            Err(VenueError::Rejected { message, .. }) => {
                assert!(message.contains("95%"), "{message}");
            }
            other => panic!("expected a rejection, got {other:?}"),
        }
        // And the same when it is the second order of the pair that failed —
        // an entry accepted with its stop refused.
        let pair = json!({"statuses": [{"resting": {"oid": 7}}, {"error": "bad trigger"}]});
        assert!(
            all_accepted(&pair).is_err(),
            "a refused stop passed unnoticed"
        );
    }

    #[test]
    fn an_ack_reads_a_resting_order_and_a_filled_one_alike() {
        let resting = json!({"resting": {"oid": 12345, "cloid": "0x01"}});
        let ack = parse_order_ack(&resting, "eng-1-1", 99).unwrap();
        assert_eq!(ack.venue_order_id, "12345");
        assert_eq!(ack.client_order_id, "eng-1-1");
        assert_eq!(ack.ack_ns, 99);

        let filled = json!({"filled": {"oid": 777, "totalSz": "0.01", "avgPx": "95000"}});
        assert_eq!(
            parse_order_ack(&filled, "eng-1-2", 1)
                .unwrap()
                .venue_order_id,
            "777"
        );

        // Anything else is unreadable rather than assumed acknowledged.
        assert!(parse_order_ack(&json!({"waitingForFill": {}}), "x", 1).is_err());
    }

    #[test]
    fn the_asset_list_keeps_positional_numbers_and_drops_delisted_names() {
        let meta = json!({"universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "GONE", "szDecimals": 2, "maxLeverage": 3, "isDelisted": true},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}
        ]});
        let assets = parse_meta(&meta).unwrap();
        assert_eq!(assets.len(), 2);
        assert_eq!(assets[0].coin, "BTC");
        assert_eq!(assets[0].index, 0);
        assert_eq!(assets[0].sz_decimals, 5);
        // ETH keeps index 2, not 1: the numbers are the venue's positions and
        // closing the gap would trade the wrong contract.
        assert_eq!(assets[1].coin, "ETH");
        assert_eq!(assets[1].index, 2);
    }

    #[test]
    fn equity_is_the_account_value_and_free_margin_is_what_can_leave() {
        let state = json!({
            "marginSummary": {"accountValue": "1500.25", "totalMarginUsed": "300"},
            "withdrawable": "1200.5",
            "assetPositions": []
        });
        assert_eq!(parse_margin(&state).unwrap(), (1500.25, 1200.5));
    }

    #[test]
    fn a_position_is_unprotected_until_a_stop_order_says_otherwise() {
        // The whole reason stops are read off the open orders: this venue puts
        // no stop on the position row, so a position with no stop order
        // against it must come back unprotected and hold new risk back.
        let state = json!({"assetPositions": [
            {"position": {"coin": "BTC", "szi": "0.01", "entryPx": "95000",
                          "leverage": {"type": "cross", "value": 20}}}
        ]});
        let resolve = |name: &str| (name == "BTCUSDT").then_some(SymbolId(0));

        let bare = parse_positions(&state, &HashMap::new(), &resolve).unwrap();
        assert_eq!(bare.len(), 1);
        assert!(
            !bare[0].stop_attached,
            "a position with no stop read as protected"
        );
        assert_eq!(bare[0].stop_px, 0.0);
        assert_eq!(bare[0].side, Side::Buy);
        assert_eq!(bare[0].qty, 0.01);
        assert_eq!(bare[0].leverage, Some(20.0));

        let stops = HashMap::from([(
            "BTC".to_string(),
            Stops {
                lowest: 93_000.0,
                highest: 93_000.0,
            },
        )]);
        let guarded = parse_positions(&state, &stops, &resolve).unwrap();
        assert!(guarded[0].stop_attached);
        assert_eq!(guarded[0].stop_px, 93_000.0);
    }

    #[test]
    fn a_short_reads_as_a_sell_of_its_absolute_size() {
        let state = json!({"assetPositions": [
            {"position": {"coin": "ETH", "szi": "-1.5", "entryPx": "3000"}}
        ]});
        let resolve = |name: &str| (name == "ETHUSDT").then_some(SymbolId(1));
        let rows = parse_positions(&state, &HashMap::new(), &resolve).unwrap();
        assert_eq!(rows[0].side, Side::Sell);
        assert_eq!(rows[0].qty, 1.5);
    }

    #[test]
    fn an_unconfigured_nonzero_position_invalidates_the_account_snapshot() {
        let resolve = |name: &str| (name == "BTCUSDT").then_some(SymbolId(0));
        let held = json!({"assetPositions": [
            {"position": {"coin": "SOL", "szi": "2", "entryPx": "150"}}
        ]});
        let err = parse_positions(&held, &HashMap::new(), &resolve).unwrap_err();
        assert!(err.to_string().contains("SOLUSDT"), "{err}");

        let flat = json!({"assetPositions": [
            {"position": {"coin": "SOL", "szi": "0"}}
        ]});
        assert!(parse_positions(&flat, &HashMap::new(), &resolve)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn only_a_reduce_only_stop_trigger_counts_as_protection() {
        let orders = json!([
            {"coin": "BTC", "isTrigger": true, "reduceOnly": true,
             "orderType": "Stop Market", "triggerPx": "93000", "sz": "0.01", "side": "A"},
            {"coin": "ETH", "isTrigger": true, "reduceOnly": true,
             "orderType": "Take Profit Market", "triggerPx": "4000", "sz": "1", "side": "A"},
            {"coin": "SOL", "isTrigger": false, "reduceOnly": true,
             "orderType": "Limit", "sz": "1", "side": "A"},
            {"coin": "DOGE", "isTrigger": true, "reduceOnly": false,
             "orderType": "Stop Market", "triggerPx": "0.1", "sz": "10", "side": "A"}
        ]);
        let stops = stops_by_coin(&orders).unwrap();
        assert_eq!(
            stops.get("BTC").map(|s| s.nearest(Side::Buy)),
            Some(93_000.0)
        );
        assert_eq!(stops.get("ETH"), None, "a take-profit is not protection");
        assert_eq!(stops.get("SOL"), None, "a plain limit is not a stop");
        assert_eq!(
            stops.get("DOGE"),
            None,
            "a stop that could open is not protection"
        );
    }

    #[test]
    fn two_stops_on_one_coin_give_the_one_that_fires_first_for_the_side_held() {
        // A long is taken out by the higher trigger and a short by the lower,
        // and this reply does not say which is held — so both ends are kept
        // and the position picks. Reporting the wrong one tells the risk
        // kernel a position is stopped further away than it is.
        let orders = json!([
            {"coin": "BTC", "isTrigger": true, "reduceOnly": true,
             "orderType": "Stop Market", "triggerPx": "90000", "sz": "0.01", "side": "A"},
            {"coin": "BTC", "isTrigger": true, "reduceOnly": true,
             "orderType": "Stop Market", "triggerPx": "93000", "sz": "0.01", "side": "A"}
        ]);
        let stops = stops_by_coin(&orders).unwrap();
        let held = stops.get("BTC").copied().expect("a stop");
        assert_eq!(
            held.nearest(Side::Buy),
            93_000.0,
            "a long stops at the higher trigger"
        );
        assert_eq!(
            held.nearest(Side::Sell),
            90_000.0,
            "a short stops at the lower one"
        );

        // And the order the venue lists them in does not decide it.
        let reversed = json!([
            {"coin": "BTC", "isTrigger": true, "reduceOnly": true,
             "orderType": "Stop Market", "triggerPx": "93000", "sz": "0.01", "side": "A"},
            {"coin": "BTC", "isTrigger": true, "reduceOnly": true,
             "orderType": "Stop Market", "triggerPx": "90000", "sz": "0.01", "side": "A"}
        ]);
        assert_eq!(
            stops_by_coin(&reversed).unwrap().get("BTC").copied(),
            Some(held)
        );
    }

    #[test]
    fn an_order_named_by_somebody_else_is_carried_through_as_theirs() {
        // Reconcile treats an empty id as "the venue's own" and trades
        // through it. A stranger's order flattened to empty would be a second
        // writer on the account that nothing reports.
        let orders = json!([
            {"coin": "BTC", "side": "B", "sz": "1", "origSz": "1", "reduceOnly": false,
             "cloid": "0x0102030405060708090a0b0c0d0e0f10"},
            {"coin": "ETH", "side": "A", "sz": "1", "origSz": "1", "reduceOnly": true,
             "isTrigger": true, "orderType": "Stop Market", "triggerPx": "3000"}
        ]);
        let rows = parse_working_orders(&orders).unwrap();
        assert_eq!(
            rows[0].client_order_id, "0x0102030405060708090a0b0c0d0e0f10",
            "a cloid that is not ours must not read as no cloid at all"
        );
        assert_eq!(
            rows[1].client_order_id, "",
            "an order with no cloid is the venue's own, which is what a stop here is"
        );
    }

    #[test]
    fn an_unattributable_non_stop_order_invalidates_the_snapshot() {
        for row in [
            json!({"coin": "ETH", "side": "A", "sz": "1", "reduceOnly": false}),
            // Even an otherwise valid native-stop shape cannot excuse a cloid
            // of the wrong JSON type.
            json!({"coin": "ETH", "side": "A", "sz": "1", "reduceOnly": true,
                   "isTrigger": true, "orderType": "Stop Market", "triggerPx": "3000",
                   "cloid": 7}),
            json!({"coin": "ETH", "side": "A", "sz": "1", "reduceOnly": false,
                   "cloid": ""}),
        ] {
            assert!(matches!(
                parse_working_orders(&json!([row])),
                Err(VenueError::BadReply(_))
            ));
        }
    }

    #[test]
    fn a_working_order_carries_the_engine_id_it_was_sent_with() {
        let our_cloid = cloid::to_cloid("eng-1700000000000-4");
        let orders = json!([
            {"coin": "BTC", "side": "B", "sz": "0.004", "origSz": "0.01",
             "cloid": our_cloid, "reduceOnly": false},
            {"coin": "ETH", "side": "A", "sz": "1", "origSz": "1", "reduceOnly": true,
             "isTrigger": true, "orderType": "Stop Market", "triggerPx": "3000"}
        ]);
        let rows = parse_working_orders(&orders).unwrap();
        assert_eq!(rows[0].client_order_id, "eng-1700000000000-4");
        assert_eq!(rows[0].symbol, "BTCUSDT");
        assert_eq!(rows[0].side, Side::Buy);
        assert_eq!(rows[0].qty, 0.01);
        assert!((rows[0].filled_qty - 0.006).abs() < 1e-12);
        // An order nobody here placed has no id of ours, which is how the
        // engine tells a hand trade from its own.
        assert_eq!(rows[1].client_order_id, "");
        assert!(rows[1].reduce_only);
    }

    #[test]
    fn a_fill_records_which_side_of_the_spread_we_were_on() {
        // `crossed` true means we paid the spread. Reading it backwards would
        // flatter every maker-share number computed from the log.
        let fills = json!([
            {"coin": "BTC", "px": "95000", "sz": "0.01", "side": "B", "time": 1700,
             "fee": "0.02", "tid": 42, "crossed": true, "oid": 9},
            {"coin": "ETH", "px": "3000", "sz": "1", "side": "A", "time": 1701,
             "fee": "-0.01", "tid": 43, "crossed": false, "oid": 10}
        ]);
        let rows = parse_executions(&fills).unwrap();
        assert_eq!(rows[0].exec_id, "42");
        assert!(!rows[0].is_maker);
        assert_eq!(rows[0].side, Side::Buy);
        assert_eq!(rows[0].venue_ts_ms, 1700);
        assert!(rows[1].is_maker);
        assert_eq!(rows[1].side, Side::Sell);
        assert_eq!(rows[1].fee, Some(-0.01));
    }

    #[test]
    fn an_unreadable_side_stops_the_read_rather_than_guessing() {
        let bad = json!([{"coin": "BTC", "px": "1", "sz": "1", "side": "buy",
                          "time": 1, "fee": "0", "tid": 1, "crossed": true}]);
        assert!(parse_executions(&bad).is_err());
    }
}
