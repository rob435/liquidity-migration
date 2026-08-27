//! Reading Lighter's replies. Pure functions over JSON, so the mapping is
//! testable without a socket.
//!
//! Every reply carries a `code`, and 200 is the only one that means the venue
//! did what was asked — an HTTP 200 with `code` anything else is a refusal,
//! which is the same trap Hyperliquid sets in a different shape.

use engine_types::orders::{VenueExecution, VenueOrder};
use engine_types::risk::PositionView;
use engine_types::{Side, SymbolId, VenueError};
use serde_json::Value;

use super::markets::{engine_symbol, Market, Markets};
use super::order_index;
use crate::json::{int_field, num_field, opt_num_field, str_field};

/// The only `code` that means success.
const CODE_OK: i64 = 200;

/// Check the envelope every reply shares.
pub(crate) fn venue_result(reply: Value) -> Result<Value, VenueError> {
    let code = reply
        .get("code")
        .and_then(Value::as_i64)
        .ok_or_else(|| VenueError::BadReply("reply carries no code".to_string()))?;
    if code != CODE_OK {
        let message = reply
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("no message")
            .to_string();
        return Err(VenueError::Rejected { code, message });
    }
    Ok(reply)
}

/// The venue's tradable markets out of an `orderBookDetails` reply.
pub(crate) fn parse_markets(reply: &Value) -> Result<Vec<Market>, VenueError> {
    let rows = reply
        .get("order_book_details")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("no order_book_details in the reply".to_string()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        // A market the venue is not currently running is not one to trade.
        let status = row.get("status").and_then(Value::as_str).unwrap_or("active");
        if !status.eq_ignore_ascii_case("active") {
            continue;
        }
        out.push(Market {
            symbol: str_field(row, "symbol")?,
            index: i16::try_from(int_field(row, "market_id")?).map_err(|_| {
                VenueError::BadReply("a market index does not fit the venue's own field".to_string())
            })?,
            size_decimals: u32::try_from(int_field(row, "supported_size_decimals")?)
                .map_err(|_| VenueError::BadReply("negative size decimals".to_string()))?,
            price_decimals: u32::try_from(int_field(row, "supported_price_decimals")?)
                .map_err(|_| VenueError::BadReply("negative price decimals".to_string()))?,
            min_base_amount: num_field(row, "min_base_amount")?,
            min_quote_amount: num_field(row, "min_quote_amount")?,
        });
    }
    if out.is_empty() {
        return Err(VenueError::BadReply(
            "the venue listed no active markets".to_string(),
        ));
    }
    Ok(out)
}

/// The account row out of an `account` reply.
fn account_row(reply: &Value) -> Result<&Value, VenueError> {
    reply
        .get("accounts")
        .and_then(Value::as_array)
        .and_then(|rows| rows.first())
        .ok_or_else(|| VenueError::BadReply("the account reply carries no account".to_string()))
}

/// Equity and free margin.
pub(crate) fn parse_margin(reply: &Value) -> Result<(f64, f64), VenueError> {
    let account = account_row(reply)?;
    let equity = num_field(account, "collateral")?;
    let available = num_field(account, "available_balance")?;
    Ok((equity, available))
}

/// Open positions.
///
/// This venue keeps no stop on a position row either — a stop is a separate
/// trigger order — so `stops` is read off the open orders, exactly as on
/// Hyperliquid, and a position with none comes back unprotected.
pub(crate) fn parse_positions(
    reply: &Value,
    _markets: &Markets,
    stops: &std::collections::HashMap<i16, Stops>,
    resolve: &impl Fn(&str) -> Option<SymbolId>,
) -> Result<Vec<PositionView>, VenueError> {
    let account = account_row(reply)?;
    let rows = account
        .get("positions")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("the account carries no positions".to_string()))?;
    let mut out = Vec::new();
    for row in rows {
        let size = num_field(row, "position")?;
        if size == 0.0 {
            continue;
        }
        // The venue states the size unsigned and the direction separately.
        let sign = int_field(row, "sign")?;
        let market_index = i16::try_from(int_field(row, "market_id")?)
            .map_err(|_| VenueError::BadReply("a market index out of range".to_string()))?;
        let name = engine_symbol(&str_field(row, "symbol")?);
        let symbol = resolve(&name).ok_or_else(|| {
            VenueError::BadReply(format!(
                "nonzero position in {name} is absent from the configured symbol table"
            ))
        })?;
        let side = if sign >= 0 { Side::Buy } else { Side::Sell };
        let stop_px = stops.get(&market_index).map(|held| held.nearest(side));
        out.push(PositionView {
            symbol,
            side,
            qty: size.abs(),
            entry_px: num_field(row, "avg_entry_price")?,
            stop_attached: stop_px.is_some(),
            stop_px: stop_px.unwrap_or(0.0),
            // The venue states a margin fraction rather than a leverage. One
            // over it is the leverage, and a zero fraction says nothing.
            leverage: opt_num_field(row, "initial_margin_fraction")?
                .filter(|fraction| *fraction > 0.0)
                .map(|fraction| 1.0 / fraction),
        });
    }
    Ok(out)
}

/// Every order the venue is working on this account.
pub(crate) fn parse_working_orders(
    reply: &Value,
    markets: &Markets,
) -> Result<Vec<VenueOrder>, VenueError> {
    let rows = reply
        .get("orders")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("the order reply carries no orders".to_string()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let market_index = i16::try_from(int_field(row, "market_index")?)
            .map_err(|_| VenueError::BadReply("a market index out of range".to_string()))?;
        let symbol = markets
            .by_index(market_index)
            .map(|m| engine_symbol(&m.symbol))
            .unwrap_or_else(|| format!("market-{market_index}"));
        let original = num_field(row, "initial_base_amount")?;
        let remaining = num_field(row, "remaining_base_amount")?;
        let client_order_index = int_field(row, "client_order_index")?;
        let client_order_id = match order_index::from_index(client_order_index) {
            Some(id) => id,
            None if is_supported_native_stop(row)? => String::new(),
            None => {
                return Err(VenueError::BadReply(format!(
                    "working order in {symbol} has undecodable client order index {client_order_index} and is not a supported native stop"
                )))
            }
        };
        out.push(VenueOrder {
            client_order_id,
            symbol,
            side: if row.get("is_ask").and_then(Value::as_bool).unwrap_or(false) {
                Side::Sell
            } else {
                Side::Buy
            },
            qty: original,
            filled_qty: (original - remaining).max(0.0),
            reduce_only: row.get("reduce_only").and_then(Value::as_bool).unwrap_or(false),
        });
    }
    Ok(out)
}

fn is_supported_native_stop(row: &Value) -> Result<bool, VenueError> {
    if row.get("reduce_only").and_then(Value::as_bool) != Some(true)
        || !row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .eq_ignore_ascii_case("stop_loss")
    {
        return Ok(false);
    }
    Ok(opt_num_field(row, "trigger_price")?.is_some_and(|trigger| trigger > 0.0))
}

/// The stops standing against each market, read off the open orders.
///
/// Only a reduce-only stop-loss trigger counts. A take-profit is not
/// protection, and an order that could increase a position is not either.
///
/// Both ends are kept because which of two stops fires first depends on the
/// position's side, which this reply does not carry: a long is taken out by
/// the higher trigger and a short by the lower. [`parse_positions`] knows the
/// side and picks.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Stops {
    pub(crate) lowest: f64,
    pub(crate) highest: f64,
}

impl Stops {
    pub(crate) fn nearest(self, side: Side) -> f64 {
        match side {
            Side::Buy => self.highest,
            Side::Sell => self.lowest,
        }
    }
}

pub(crate) fn stops_by_market(
    reply: &Value,
) -> Result<std::collections::HashMap<i16, Stops>, VenueError> {
    let rows = reply
        .get("orders")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("the order reply carries no orders".to_string()))?;
    let mut out: std::collections::HashMap<i16, Stops> = std::collections::HashMap::new();
    for row in rows {
        if !row.get("reduce_only").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        let kind = row.get("type").and_then(Value::as_str).unwrap_or_default();
        if !kind.to_ascii_lowercase().contains("stop") {
            continue;
        }
        let Some(trigger) = opt_num_field(row, "trigger_price")? else { continue };
        if trigger <= 0.0 {
            continue;
        }
        let market_index = i16::try_from(int_field(row, "market_index")?)
            .map_err(|_| VenueError::BadReply("a market index out of range".to_string()))?;
        // The decimal the venue states, like every other price in this reply.
        // Deciding by magnitude whether it "must be" an integer tick would put
        // the stop out by a factor of ten for the markets on the wrong side of
        // the threshold, and the risk kernel judges positions against it.
        out.entry(market_index)
            .and_modify(|held| {
                held.lowest = held.lowest.min(trigger);
                held.highest = held.highest.max(trigger);
            })
            .or_insert(Stops { lowest: trigger, highest: trigger });
    }
    Ok(out)
}

/// Fills out of a `trades` reply.
pub(crate) fn parse_executions(
    reply: &Value,
    account_index: i64,
    markets: &Markets,
) -> Result<Vec<VenueExecution>, VenueError> {
    let rows = reply
        .get("trades")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("the trade reply carries no trades".to_string()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let market_index = i16::try_from(int_field(row, "market_id")?)
            .map_err(|_| VenueError::BadReply("a market index out of range".to_string()))?;
        let symbol = markets
            .by_index(market_index)
            .map(|m| engine_symbol(&m.symbol))
            .unwrap_or_else(|| format!("market-{market_index}"));
        // A trade names both sides, and this reply is account-wide. Which side
        // was ours decides the fill's direction and which client order index
        // to read, so it is read rather than assumed: defaulting the account
        // id would make every fill a buy, and the exposure ledger is a running
        // sum of signed fills.
        let ask_account = int_field(row, "ask_account_id")?;
        let bid_account = int_field(row, "bid_account_id")?;
        let we_sold = if ask_account == account_index {
            true
        } else if bid_account == account_index {
            false
        } else {
            // Neither side is us. Not an error — the venue answers for the
            // account, and a trade that names neither is one to leave alone
            // rather than record as a buy.
            continue;
        };
        let we_made = row
            .get("is_maker_ask")
            .and_then(Value::as_bool)
            .map(|maker_was_ask| maker_was_ask == we_sold)
            .unwrap_or(false);
        let client_index = if we_sold {
            row.get("ask_client_order_index")
        } else {
            row.get("bid_client_order_index")
        }
        .and_then(Value::as_i64)
        .unwrap_or(0);
        out.push(VenueExecution {
            exec_id: int_field(row, "trade_id")?.to_string(),
            client_order_id: order_index::from_index(client_index).unwrap_or_default(),
            symbol,
            side: if we_sold { Side::Sell } else { Side::Buy },
            qty: num_field(row, "size")?,
            px: num_field(row, "price")?,
            // The row states one fee, and it is the taker's — the side that
            // crossed. Charging it to a fill we made would be recording the
            // counterparty's cost as ours, which is the wrong number in every
            // execution-cost table the engine produces.
            fee: if we_made {
                0.0
            } else {
                opt_num_field(row, "fee")?.unwrap_or(0.0)
            },
            is_maker: we_made,
            venue_ts_ms: int_field(row, "timestamp")?,
        });
    }
    Ok(out)
}

/// The next nonce for this account's key.
pub(crate) fn parse_nonce(reply: &Value) -> Result<i64, VenueError> {
    int_field(reply, "nonce")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::collections::HashMap;

    fn markets() -> Markets {
        Markets::from_rows(vec![Market {
            symbol: "BTC".to_string(),
            index: 0,
            size_decimals: 5,
            price_decimals: 1,
            min_base_amount: 0.0001,
            min_quote_amount: 10.0,
        }])
    }

    #[test]
    fn a_reply_that_is_not_code_200_is_a_rejection() {
        let refused = venue_result(json!({"code": 21120, "message": "invalid nonce"}));
        match refused {
            Err(VenueError::Rejected { code, message }) => {
                assert_eq!(code, 21120);
                assert!(message.contains("nonce"), "{message}");
            }
            other => panic!("expected a rejection, got {other:?}"),
        }
        assert!(venue_result(json!({"code": 200})).is_ok());
        assert!(venue_result(json!({"nope": 1})).is_err());
    }

    #[test]
    fn the_market_list_keeps_the_venues_own_indices_and_drops_inactive_markets() {
        let reply = json!({"code": 200, "order_book_details": [
            {"symbol": "BTC", "market_id": 0, "status": "active",
             "supported_size_decimals": 5, "supported_price_decimals": 1,
             "min_base_amount": "0.0001", "min_quote_amount": "10"},
            {"symbol": "GONE", "market_id": 3, "status": "inactive",
             "supported_size_decimals": 2, "supported_price_decimals": 2,
             "min_base_amount": "1", "min_quote_amount": "10"},
            {"symbol": "ETH", "market_id": 1, "status": "active",
             "supported_size_decimals": 4, "supported_price_decimals": 2,
             "min_base_amount": "0.001", "min_quote_amount": "10"}
        ]});
        let markets = parse_markets(&reply).unwrap();
        assert_eq!(markets.len(), 2);
        assert_eq!(markets[0].symbol, "BTC");
        assert_eq!(markets[0].index, 0);
        assert_eq!(markets[1].symbol, "ETH");
        assert_eq!(markets[1].index, 1, "the venue's index, not a position in this list");
    }

    #[test]
    fn equity_and_free_margin_come_off_the_account() {
        let reply = json!({"code": 200, "accounts": [
            {"collateral": "1500.25", "available_balance": "1200.5", "positions": []}
        ]});
        assert_eq!(parse_margin(&reply).unwrap(), (1500.25, 1200.5));
    }

    #[test]
    fn a_position_is_unprotected_until_a_stop_order_says_otherwise() {
        let reply = json!({"code": 200, "accounts": [{"collateral": "1", "available_balance": "1",
            "positions": [
                {"market_id": 0, "symbol": "BTC", "sign": 1, "position": "0.01",
                 "avg_entry_price": "95000", "initial_margin_fraction": "0.05"}
            ]}]});
        let resolve = |name: &str| (name == "BTCUSDT").then_some(SymbolId(0));

        let bare = parse_positions(&reply, &markets(), &HashMap::new(), &resolve).unwrap();
        assert_eq!(bare.len(), 1);
        assert!(!bare[0].stop_attached, "a position with no stop read as protected");
        assert_eq!(bare[0].side, Side::Buy);
        assert_eq!(bare[0].qty, 0.01);
        assert_eq!(bare[0].leverage, Some(20.0), "one over the margin fraction");

        let stops = HashMap::from([(0i16, Stops { lowest: 93_000.0, highest: 93_000.0 })]);
        let guarded = parse_positions(&reply, &markets(), &stops, &resolve).unwrap();
        assert!(guarded[0].stop_attached);
        assert_eq!(guarded[0].stop_px, 93_000.0);
    }

    #[test]
    fn a_short_reads_as_a_sell() {
        let reply = json!({"code": 200, "accounts": [{"collateral": "1", "available_balance": "1",
            "positions": [
                {"market_id": 0, "symbol": "BTC", "sign": -1, "position": "0.5",
                 "avg_entry_price": "95000", "initial_margin_fraction": "0"}
            ]}]});
        let resolve = |_: &str| Some(SymbolId(0));
        let rows = parse_positions(&reply, &markets(), &HashMap::new(), &resolve).unwrap();
        assert_eq!(rows[0].side, Side::Sell);
        assert_eq!(rows[0].qty, 0.5);
        assert_eq!(rows[0].leverage, None, "a zero margin fraction says nothing");
    }

    #[test]
    fn an_unconfigured_nonzero_position_invalidates_the_account_snapshot() {
        let resolve = |name: &str| (name == "BTCUSDT").then_some(SymbolId(0));
        let held = json!({"code": 200, "accounts": [{"positions": [{
            "market_id": 1, "symbol": "ETH", "sign": 1, "position": "0.5",
            "avg_entry_price": "3000"
        }]}]});
        let err = parse_positions(&held, &markets(), &HashMap::new(), &resolve).unwrap_err();
        assert!(err.to_string().contains("ETHUSDT"), "{err}");

        let flat = json!({"code": 200, "accounts": [{"positions": [{
            "symbol": "ETH", "position": "0"
        }]}]});
        assert!(parse_positions(&flat, &markets(), &HashMap::new(), &resolve).unwrap().is_empty());
    }

    #[test]
    fn a_working_order_carries_the_engine_id_it_was_sent_with() {
        let ours = order_index::to_index("eng-1700000000000-4");
        let reply = json!({"code": 200, "orders": [
            {"market_index": 0, "client_order_index": ours, "is_ask": false,
             "initial_base_amount": "0.01", "remaining_base_amount": "0.004",
             "reduce_only": false, "type": "limit"},
            {"market_index": 0, "client_order_index": 5, "is_ask": true,
             "initial_base_amount": "1", "remaining_base_amount": "1",
             "reduce_only": true, "type": "stop_loss", "trigger_price": "93000"}
        ]});
        let rows = parse_working_orders(&reply, &markets()).unwrap();
        assert_eq!(rows[0].client_order_id, "eng-1700000000000-4");
        assert_eq!(rows[0].symbol, "BTCUSDT");
        assert_eq!(rows[0].side, Side::Buy);
        assert!((rows[0].filled_qty - 0.006).abs() < 1e-12);
        // The adapter's hashed native stop is positively identified by shape.
        assert_eq!(rows[1].client_order_id, "");
        assert_eq!(rows[1].side, Side::Sell);
    }

    #[test]
    fn an_undecodable_non_stop_order_invalidates_the_snapshot() {
        let reply = json!({"code": 200, "orders": [{
            "market_index": 0, "client_order_index": 5, "is_ask": false,
            "initial_base_amount": "1", "remaining_base_amount": "1",
            "reduce_only": false, "type": "limit"
        }]});
        assert!(matches!(
            parse_working_orders(&reply, &markets()),
            Err(VenueError::BadReply(_))
        ));
    }

    #[test]
    fn only_a_reduce_only_stop_counts_as_protection() {
        let reply = json!({"code": 200, "orders": [
            {"market_index": 0, "reduce_only": true, "type": "stop_loss",
             "trigger_price": "93000", "client_order_index": 1},
            {"market_index": 1, "reduce_only": true, "type": "take_profit",
             "trigger_price": "99000", "client_order_index": 2},
            {"market_index": 2, "reduce_only": false, "type": "stop_loss",
             "trigger_price": "1", "client_order_index": 3}
        ]});
        let stops = stops_by_market(&reply).unwrap();
        assert_eq!(stops.get(&0).map(|s| s.nearest(Side::Buy)), Some(93_000.0));
        assert_eq!(stops.get(&1), None, "a take-profit is not protection");
        assert_eq!(stops.get(&2), None, "a stop that could open is not protection");
    }

    #[test]
    fn a_trigger_is_the_decimal_the_venue_states_at_any_size() {
        // Not scaled by anything, and not decided by how large it is: this
        // reply states prices as decimals throughout, and a stop read a factor
        // of ten out is a position the risk kernel believes is protected
        // somewhere it is not.
        let reply = json!({"code": 200, "orders": [
            {"market_index": 0, "reduce_only": true, "type": "stop_loss",
             "trigger_price": "9300000", "client_order_index": 1}
        ]});
        let stops = stops_by_market(&reply).unwrap();
        assert_eq!(stops.get(&0).map(|s| s.nearest(Side::Buy)), Some(9_300_000.0));
    }

    #[test]
    fn two_stops_on_one_market_give_the_one_that_fires_first_for_the_side_held() {
        let reply = json!({"code": 200, "orders": [
            {"market_index": 0, "reduce_only": true, "type": "stop_loss",
             "trigger_price": "90000", "client_order_index": 1},
            {"market_index": 0, "reduce_only": true, "type": "stop_loss",
             "trigger_price": "93000", "client_order_index": 2}
        ]});
        let held = stops_by_market(&reply).unwrap().get(&0).copied().expect("a stop");
        assert_eq!(held.nearest(Side::Buy), 93_000.0, "a long stops at the higher trigger");
        assert_eq!(held.nearest(Side::Sell), 90_000.0, "a short stops at the lower one");
    }

    #[test]
    fn a_fill_says_which_side_we_were_on_and_whether_we_made_the_price() {
        let reply = json!({"code": 200, "trades": [
            {"trade_id": 11, "market_id": 0, "size": "0.01", "price": "95000",
             "timestamp": 1700, "fee": "0.02", "is_maker_ask": true,
             "ask_account_id": 99, "bid_account_id": 42,
             "bid_client_order_index": 7, "ask_client_order_index": 8},
            {"trade_id": 12, "market_id": 0, "size": "0.02", "price": "95100",
             "timestamp": 1701, "fee": "0.03", "is_maker_ask": true,
             "ask_account_id": 42, "bid_account_id": 99,
             "bid_client_order_index": 9, "ask_client_order_index": 10}
        ]});
        let rows = parse_executions(&reply, 42, &markets()).unwrap();
        // We were the bidder on the first: a buy. The maker was the ask, so
        // we crossed and took.
        assert_eq!(rows[0].side, Side::Buy);
        assert!(!rows[0].is_maker);
        assert_eq!(rows[0].exec_id, "11");
        // We were the asker on the second, and the maker was the ask — us —
        // so we earned the spread.
        assert_eq!(rows[1].side, Side::Sell);
        assert!(rows[1].is_maker);
    }

    #[test]
    fn a_fill_we_made_is_not_charged_the_takers_fee() {
        // The row carries one fee and it belongs to the side that crossed.
        let reply = json!({"code": 200, "trades": [
            {"trade_id": 21, "market_id": 0, "size": "0.01", "price": "95000",
             "timestamp": 1700, "fee": "0.02", "is_maker_ask": true,
             "ask_account_id": 42, "bid_account_id": 99,
             "bid_client_order_index": 7, "ask_client_order_index": 8},
            {"trade_id": 22, "market_id": 0, "size": "0.01", "price": "95000",
             "timestamp": 1701, "fee": "0.03", "is_maker_ask": true,
             "ask_account_id": 99, "bid_account_id": 42,
             "bid_client_order_index": 7, "ask_client_order_index": 8}
        ]});
        let rows = parse_executions(&reply, 42, &markets()).unwrap();
        assert!(rows[0].is_maker, "we were the ask and the maker was the ask");
        assert_eq!(rows[0].fee, 0.0, "a fill we made was charged the taker's fee");
        assert!(!rows[1].is_maker);
        assert_eq!(rows[1].fee, 0.03, "a fill we took carries its fee");
    }

    #[test]
    fn a_fill_that_does_not_say_whose_it_is_is_refused_not_guessed() {
        // Defaulting the account id made every fill a buy, and the exposure
        // ledger is a running sum of signed fills — so a shape change would
        // have flipped the sign of the whole book without a word.
        let missing = json!({"code": 200, "trades": [
            {"trade_id": 11, "market_id": 0, "size": "0.01", "price": "95000",
             "timestamp": 1700, "fee": "0.02", "is_maker_ask": true,
             "bid_account_id": 42,
             "bid_client_order_index": 7, "ask_client_order_index": 8}
        ]});
        assert!(parse_executions(&missing, 42, &markets()).is_err());

        // And a trade between two other accounts is left alone rather than
        // recorded as ours.
        let strangers = json!({"code": 200, "trades": [
            {"trade_id": 12, "market_id": 0, "size": "0.01", "price": "95000",
             "timestamp": 1700, "fee": "0.02", "is_maker_ask": true,
             "ask_account_id": 98, "bid_account_id": 99,
             "bid_client_order_index": 7, "ask_client_order_index": 8}
        ]});
        assert!(parse_executions(&strangers, 42, &markets()).unwrap().is_empty());
    }

    #[test]
    fn a_nonce_is_read_as_a_whole_number() {
        assert_eq!(parse_nonce(&json!({"code": 200, "nonce": 42})).unwrap(), 42);
        assert!(parse_nonce(&json!({"code": 200})).is_err());
    }
}
