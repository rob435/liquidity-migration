//! MEXC replies to the engine's contract types. No sockets, no state.
//!
//! Three things here are venue-specific enough to be worth saying out loud.
//!
//! **Sizes arrive in contracts.** Every quantity the venue reports — position
//! size, order size, fill size — is a contract count, so each one is put back
//! through [`super::contracts`] before it becomes a number the engine uses.
//!
//! **`side` is not a buy/sell axis.** MEXC encodes direction and intent in one
//! field: 1 open long, 2 close short, 3 open short, 4 close long. Buy is 1 and
//! 2; sell is 3 and 4; and the even values are the closes, which is also the
//! only place a reduce-only flag comes from on a read.
//!
//! **Ids come back as quoted strings.** `orderId` and `positionId` are typed
//! `long` in the venue's field tables and printed as JSON strings in its own
//! examples, while `id` beside them is a bare number. Anything reading an id
//! here accepts both rather than trusting either.

use std::collections::HashMap;

use engine_types::ids::SymbolId;
use engine_types::orders::{Side, VenueExecution, VenueOrder};
use engine_types::risk::PositionView;
use engine_types::VenueError;
use serde_json::Value;

use crate::json::{int_field, num_field, opt_num_field, str_field};

use super::contracts::Contracts;

/// The settle currency this engine trades. The assets call answers for every
/// currency the account holds and only this one is margin.
pub(crate) const SETTLE_CURRENCY: &str = "USDT";

/// Unwrap the `{"success": …, "code": …, "data": …}` envelope every endpoint
/// uses, turning the venue's own refusal into a typed rejection.
pub(crate) fn venue_result(body: &Value) -> Result<&Value, VenueError> {
    let ok = body
        .get("success")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let code = body.get("code").and_then(Value::as_i64).unwrap_or(-1);
    if ok && code == 0 {
        return body
            .get("data")
            .ok_or_else(|| VenueError::BadReply("a successful reply carried no data".into()));
    }
    let message = body
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("(no message)")
        .to_string();
    Err(VenueError::Rejected { code, message })
}

/// An id that may arrive as a JSON string or as a JSON number.
pub(crate) fn id_text(obj: &Value, name: &str) -> Option<String> {
    match obj.get(name)? {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// Which way an order or a fill points, from MEXC's four-way `side`.
///
/// Returns the side and whether it is a close. There is no separate
/// reduce-only field on a read: 2 and 4 ARE the closes.
pub(crate) fn side_of(side: i64) -> Option<(Side, bool)> {
    match side {
        1 => Some((Side::Buy, false)),  // open long
        2 => Some((Side::Buy, true)),   // close short
        3 => Some((Side::Sell, false)), // open short
        4 => Some((Side::Sell, true)),  // close long
        _ => None,
    }
}

/// Equity and spendable margin, from the settle currency's row.
///
/// The reply lists every currency the account holds, so the row is chosen by
/// name rather than by position — an account that acquires any other balance
/// would otherwise start reporting that balance as its margin.
pub(crate) fn parse_assets(data: &Value) -> Result<(f64, f64), VenueError> {
    let rows = data
        .as_array()
        .ok_or_else(|| VenueError::BadReply("account assets was not a list".into()))?;
    let row = rows
        .iter()
        .find(|row| row.get("currency").and_then(Value::as_str) == Some(SETTLE_CURRENCY))
        .ok_or_else(|| {
            VenueError::BadReply(format!(
                "the account holds no {SETTLE_CURRENCY} balance row"
            ))
        })?;
    let equity = row
        .get("equity")
        .and_then(Value::as_f64)
        .ok_or_else(|| VenueError::BadReply("the balance row carried no equity".into()))?;
    let available = row
        .get("availableBalance")
        .and_then(Value::as_f64)
        .ok_or_else(|| {
            VenueError::BadReply("the balance row carried no availableBalance".into())
        })?;
    Ok((equity, available))
}

/// The live stop for each position, keyed by the venue's position id.
///
/// MEXC's position object carries no stop-loss field at all, so "is this
/// position protected" cannot be answered from the position. It is answered by
/// joining this in — and a record whose `orderId` is non-zero is bound to a
/// resting limit order rather than to the position, so it is not the position's
/// stop and is not counted as one.
pub(crate) fn parse_position_stops(data: &Value) -> HashMap<String, f64> {
    let mut stops = HashMap::new();
    let rows = match rows_of(data) {
        Some(rows) => rows,
        None => return stops,
    };
    for row in rows {
        let Some(position_id) = id_text(row, "positionId") else {
            continue;
        };
        let order_id = id_text(row, "orderId").unwrap_or_default();
        // "Limit order id; if placed by position, this value is 0."
        if !(order_id.is_empty() || order_id == "0") {
            continue;
        }
        let Some(px) = row.get("stopLossPrice").and_then(Value::as_f64) else {
            continue;
        };
        if px > 0.0 {
            stops.insert(position_id, px);
        }
    }
    stops
}

/// Open positions, with each size converted out of contracts and each stop
/// joined in from the separate take-profit/stop-loss book.
pub(crate) fn parse_positions(
    data: &Value,
    contracts: &Contracts,
    ids: &HashMap<String, SymbolId>,
    stops: &HashMap<String, f64>,
) -> Result<Vec<PositionView>, VenueError> {
    let rows = data
        .as_array()
        .ok_or_else(|| VenueError::BadReply("open positions was not a list".into()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let hold_vol = num_field(row, "holdVol")?;
        if hold_vol == 0.0 {
            continue;
        }
        let venue_symbol = row.get("symbol").and_then(Value::as_str).ok_or_else(|| {
            VenueError::BadReply("a nonzero position has no readable symbol".into())
        })?;
        let symbol = contracts.symbol_of(venue_symbol).ok_or_else(|| {
            VenueError::BadReply(format!(
                "nonzero position in {venue_symbol} cannot be mapped into the configured symbol table"
            ))
        })?;
        let &id = ids.get(symbol.as_str()).ok_or_else(|| {
            VenueError::BadReply(format!(
                "nonzero position in {symbol} is absent from the configured symbol table"
            ))
        })?;
        let contract = contracts.any(symbol).ok_or_else(|| {
            VenueError::BadReply(format!(
                "contract metadata vanished for position in {symbol}"
            ))
        })?;
        if hold_vol < 0.0 {
            return Err(VenueError::BadReply(format!(
                "position in {venue_symbol} has a negative contract size {hold_vol}"
            )));
        }
        let side = match row.get("positionType").and_then(Value::as_i64) {
            Some(1) => Side::Buy,
            Some(2) => Side::Sell,
            other => {
                return Err(VenueError::BadReply(format!(
                    "nonzero position in {venue_symbol} has unknown positionType {other:?}"
                )))
            }
        };
        let stop_px = id_text(row, "positionId")
            .and_then(|pid| stops.get(&pid).copied())
            .unwrap_or(0.0);
        out.push(PositionView {
            symbol: id,
            side,
            qty: contract.base_for(hold_vol),
            entry_px: row
                .get("holdAvgPrice")
                .and_then(Value::as_f64)
                .unwrap_or(0.0),
            stop_attached: stop_px > 0.0,
            stop_px,
            leverage: row.get("leverage").and_then(Value::as_f64),
        });
    }
    Ok(out)
}

/// Every order the venue is currently working, whoever placed it.
pub(crate) fn parse_open_orders(
    data: &Value,
    contracts: &Contracts,
) -> Result<(Vec<VenueOrder>, usize), VenueError> {
    let rows =
        rows_of(data).ok_or_else(|| VenueError::BadReply("open orders carried no rows".into()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let client_order_id = str_field(row, "externalOid")?;
        if client_order_id.trim().is_empty() {
            return Err(VenueError::BadReply(
                "a working order has no attributable externalOid".into(),
            ));
        }
        let venue_symbol = str_field(row, "symbol")?;
        let symbol = contracts.symbol_of(&venue_symbol).ok_or_else(|| {
            VenueError::BadReply(format!(
                "working order names unknown contract {venue_symbol}"
            ))
        })?;
        let contract = contracts.any(symbol).ok_or_else(|| {
            VenueError::BadReply(format!("contract metadata vanished for {symbol}"))
        })?;
        let side_raw = row.get("side").and_then(Value::as_i64).ok_or_else(|| {
            VenueError::BadReply(format!(
                "working order {client_order_id} has no integer side"
            ))
        })?;
        let (side, is_close) = side_of(side_raw).ok_or_else(|| {
            VenueError::BadReply(format!(
                "working order {client_order_id} has unknown side {side_raw}"
            ))
        })?;
        let vol = num_field(row, "vol")?;
        let deal_vol = num_field(row, "dealVol")?;
        if vol <= 0.0 || deal_vol < 0.0 || deal_vol > vol {
            return Err(VenueError::BadReply(format!(
                "working order {client_order_id} has invalid volume {vol} or filled volume {deal_vol}"
            )));
        }
        out.push(VenueOrder {
            client_order_id,
            symbol: venue_symbol,
            side,
            qty: contract.base_for(vol),
            filled_qty: contract.base_for(deal_vol),
            reduce_only: is_close,
        });
    }
    Ok((out, rows.len()))
}

/// One page of the venue's own fill history.
pub(crate) fn parse_deals(
    data: &Value,
    contracts: &Contracts,
) -> Result<(Vec<VenueExecution>, usize), VenueError> {
    let rows =
        rows_of(data).ok_or_else(|| VenueError::BadReply("order deals carried no rows".into()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let venue_symbol = str_field(row, "symbol")?;
        let symbol = contracts.symbol_of(&venue_symbol).ok_or_else(|| {
            VenueError::BadReply(format!("execution names unknown contract {venue_symbol}"))
        })?;
        let contract = contracts.any(symbol).ok_or_else(|| {
            VenueError::BadReply(format!(
                "contract metadata vanished for execution in {symbol}"
            ))
        })?;
        let side_raw = int_field(row, "side")?;
        let (side, _) = side_of(side_raw).ok_or_else(|| {
            VenueError::BadReply(format!(
                "execution in {venue_symbol} has unknown side {side_raw}"
            ))
        })?;
        let exec_id = id_text(row, "id")
            .filter(|id| !id.trim().is_empty())
            .ok_or_else(|| {
                VenueError::BadReply(format!("execution in {venue_symbol} has no readable id"))
            })?;
        let client_order_id = match row.get("externalOid") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(value)) => value.clone(),
            Some(_) => {
                return Err(VenueError::BadReply(format!(
                    "execution {exec_id} in {venue_symbol} has a non-string externalOid"
                )))
            }
        };
        let contracts_qty = num_field(row, "vol")?;
        let qty = contract.base_for(contracts_qty);
        let px = num_field(row, "price")?;
        let fee = opt_num_field(row, "fee")?;
        let taker = row.get("taker").and_then(Value::as_bool).ok_or_else(|| {
            VenueError::BadReply(format!(
                "execution {exec_id} in {venue_symbol} has no boolean taker flag"
            ))
        })?;
        let venue_ts_ms = int_field(row, "timestamp")?;
        if contracts_qty <= 0.0 || !qty.is_finite() || qty <= 0.0 || px <= 0.0 || venue_ts_ms <= 0 {
            return Err(VenueError::BadReply(format!(
                "execution {exec_id} in {venue_symbol} has non-positive quantity, price, or timestamp"
            )));
        }
        out.push(VenueExecution {
            exec_id,
            client_order_id,
            symbol: symbol.to_string(),
            side,
            qty,
            px,
            fee,
            is_maker: !taker,
            // MEXC states no reason for a close on this row.
            forced_close: None,
            venue_ts_ms,
        });
    }
    Ok((out, rows.len()))
}

/// The venue's own id for an order it just accepted.
pub(crate) fn parse_order_ack(data: &Value) -> Result<String, VenueError> {
    // The reply became an object carrying `orderId` and `ts`; it used to be a
    // bare id. Both are read, because a venue that changed this shape once can
    // change it back and an adapter that only knew the new one would lose the
    // id of an order that is already live.
    if let Some(id) = id_text(data, "orderId") {
        return Ok(id);
    }
    match data {
        Value::String(s) => Ok(s.clone()),
        Value::Number(n) => Ok(n.to_string()),
        other => Err(VenueError::BadReply(format!(
            "an accepted order carried no order id: {other}"
        ))),
    }
}

/// Rows from either a bare array or a paged `resultList` envelope.
fn rows_of(data: &Value) -> Option<&Vec<Value>> {
    data.as_array()
        .or_else(|| data.get("resultList").and_then(Value::as_array))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn contracts() -> Contracts {
        Contracts::parse(&json!({"data": [
            {"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT",
             "contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":400000,"apiAllowed":true}
        ]}))
        .unwrap()
    }

    fn ids() -> HashMap<String, SymbolId> {
        HashMap::from([("BTCUSDT".to_string(), SymbolId(0))])
    }

    #[test]
    fn a_refusal_becomes_a_typed_rejection_carrying_the_venues_own_code() {
        let err = venue_result(&json!({"success": false, "code": 1002, "message": "bad param"}))
            .unwrap_err();
        match err {
            VenueError::Rejected { code, message } => {
                assert_eq!(code, 1002);
                assert_eq!(message, "bad param");
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn success_false_with_code_zero_is_still_a_refusal() {
        // The venue prints exactly this shape in one of its own examples, so
        // trusting `code` alone would read a refusal as an empty success.
        assert!(venue_result(&json!({"success": false, "code": 0, "data": []})).is_err());
    }

    #[test]
    fn the_settle_currency_row_is_chosen_by_name_not_by_position() {
        // Recorded shape: the reply lists every currency the account holds.
        let data = json!([
            {"currency":"MX","equity":0.99647149,"availableBalance":0.996_471_495_977_248_6},
            {"currency":"USDT","equity":32.80984793,"availableBalance":32.80984793658,
             "positionMargin":0,"frozenBalance":0,"unrealized":0}
        ]);
        let (equity, available) = parse_assets(&data).unwrap();
        assert_eq!(equity, 32.80984793);
        assert_eq!(available, 32.80984793658);
    }

    #[test]
    fn an_account_with_no_settle_balance_is_an_error_not_a_zero_equity() {
        // Zero equity is what the envelope reads as "no room for anything",
        // which is a different statement from "this read did not work".
        let data = json!([{"currency": "MX", "equity": 1.0, "availableBalance": 1.0}]);
        assert!(parse_assets(&data).is_err());
    }

    #[test]
    fn the_four_way_side_becomes_a_direction_and_whether_it_closes() {
        assert_eq!(side_of(1), Some((Side::Buy, false)));
        assert_eq!(side_of(2), Some((Side::Buy, true)));
        assert_eq!(side_of(3), Some((Side::Sell, false)));
        assert_eq!(side_of(4), Some((Side::Sell, true)));
        assert_eq!(side_of(0), None);
        assert_eq!(side_of(5), None);
    }

    #[test]
    fn a_position_size_is_converted_out_of_contracts() {
        // The venue's own worked example: holdVol 5 at contractSize 0.0001 is
        // 0.0005 BTC, and its published margin of 27.444375 at leverage 2 only
        // reconciles on that reading.
        let data = json!([{
            "positionId": 1109973831, "symbol": "BTC_USDT", "holdVol": 5, "positionType": 1,
            "holdAvgPrice": 109777.5, "leverage": 2, "im": 27.444375, "state": 1
        }]);
        let out = parse_positions(&data, &contracts(), &ids(), &HashMap::new()).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].qty, 0.0005);
        assert_eq!(out[0].side, Side::Buy);
        assert_eq!(out[0].entry_px, 109777.5);
        assert_eq!(out[0].leverage, Some(2.0));
        // No stop was joined in, so the position reads as unprotected.
        assert!(!out[0].stop_attached);
        assert_eq!(out[0].stop_px, 0.0);
    }

    #[test]
    fn a_positions_stop_is_joined_in_from_the_separate_stop_book() {
        // MEXC's position object has no stop field at all, so this join is the
        // only way "is this position protected" can be answered.
        let stop_rows = json!([{
            "id": 357859177, "orderId": "0", "symbol": "BTC_USDT", "positionId": "1109973831",
            "stopLossPrice": 101000.0, "takeProfitPrice": 0, "state": 1
        }]);
        let stops = parse_position_stops(&stop_rows);
        let data = json!([{
            "positionId": "1109973831", "symbol": "BTC_USDT", "holdVol": 5,
            "positionType": 1, "holdAvgPrice": 109777.5, "leverage": 2
        }]);
        let out = parse_positions(&data, &contracts(), &ids(), &stops).unwrap();
        assert!(out[0].stop_attached);
        assert_eq!(out[0].stop_px, 101000.0);
    }

    #[test]
    fn a_stop_bound_to_a_resting_order_is_not_the_positions_stop() {
        // "Limit order id; if placed by position, this value is 0." A record
        // naming an order guards that order's fill, not the position, and
        // counting it would report a position as protected when it is not.
        let stops = parse_position_stops(&json!([{
            "id": 1, "orderId": "720733527158642176", "positionId": "42",
            "stopLossPrice": 101000.0
        }]));
        assert!(stops.is_empty());
    }

    #[test]
    fn an_id_is_read_whether_the_venue_quotes_it_or_not() {
        // The venue's own example returns `id` as a bare number and
        // `positionId` beside it as a quoted string, in one object.
        let row = json!({"id": 357859177, "positionId": "1027177055"});
        assert_eq!(id_text(&row, "id").as_deref(), Some("357859177"));
        assert_eq!(id_text(&row, "positionId").as_deref(), Some("1027177055"));
    }

    #[test]
    fn an_accepted_order_gives_up_its_id_in_either_shape() {
        // Recorded current shape.
        let data = json!({"orderId": "739113577038255616", "ts": 1761888808839i64});
        assert_eq!(parse_order_ack(&data).unwrap(), "739113577038255616");
        // The shape it had before, kept because an id that cannot be read is
        // an order that is live and unattributable.
        assert_eq!(parse_order_ack(&json!("7391135770")).unwrap(), "7391135770");
    }

    #[test]
    fn a_fill_is_converted_out_of_contracts_and_its_maker_flag_read_from_the_current_name() {
        let data = json!([{
            "id": 998, "symbol": "BTC_USDT", "side": 1, "vol": 10, "price": 77500.1,
            "fee": 0.046, "taker": false, "timestamp": 1787492334852i64,
            "externalOid": "eng-1"
        }]);
        let (out, raw_count) = parse_deals(&data, &contracts()).unwrap();
        assert_eq!(raw_count, 1);
        assert_eq!(out[0].qty, 0.001);
        assert_eq!(out[0].client_order_id, "eng-1");
        assert_eq!(out[0].symbol, "BTCUSDT");
        assert_eq!(out[0].fee, Some(0.046));
        assert!(out[0].is_maker);
    }

    #[test]
    fn a_fill_without_the_current_taker_flag_invalidates_the_page() {
        let base = json!({
            "id": 1, "symbol": "BTC_USDT", "side": 1, "vol": 1,
            "price": 1.0, "timestamp": 1
        });
        assert!(parse_deals(&json!([base.clone()]), &contracts()).is_err());
        let mut legacy = base;
        legacy["isTaker"] = json!(false);
        assert!(
            parse_deals(&json!([legacy]), &contracts()).is_err(),
            "the retired field name was treated as current evidence"
        );
    }

    #[test]
    fn a_missing_fee_stays_unknown_and_an_explicit_zero_stays_zero() {
        let base = json!({
            "id": 1, "symbol": "BTC_USDT", "side": 1, "vol": 1,
            "price": 1.0, "taker": true, "timestamp": 1
        });
        let (without, _) = parse_deals(&json!([base.clone()]), &contracts()).unwrap();
        assert_eq!(without[0].fee, None);

        let mut zero = base;
        zero["fee"] = json!(0.0);
        let (with_zero, _) = parse_deals(&json!([zero]), &contracts()).unwrap();
        assert_eq!(with_zero[0].fee, Some(0.0));
    }

    #[test]
    fn one_malformed_deal_invalidates_the_whole_page() {
        let valid = json!({
            "id": 1, "symbol": "BTC_USDT", "side": 1, "vol": 1,
            "price": 1.0, "fee": 0.01, "taker": true, "timestamp": 1,
            "externalOid": "eng-1"
        });
        let mut bad_rows = Vec::new();
        for field in ["id", "symbol", "side", "vol", "price", "taker", "timestamp"] {
            let mut row = valid.clone();
            row.as_object_mut().unwrap().remove(field);
            bad_rows.push(row);
        }
        for (field, value) in [
            ("symbol", json!("UNKNOWN_USDT")),
            ("side", json!(9)),
            ("vol", json!(0)),
            ("price", json!(0)),
            ("fee", json!("NaN")),
            ("taker", json!("yes")),
            ("timestamp", json!(0)),
            ("externalOid", json!(7)),
        ] {
            let mut row = valid.clone();
            row[field] = value;
            bad_rows.push(row);
        }
        for bad in bad_rows {
            assert!(
                parse_deals(&json!([valid.clone(), bad]), &contracts()).is_err(),
                "a malformed row was silently skipped"
            );
        }
    }

    #[test]
    fn a_full_deal_page_reports_raw_venue_cardinality() {
        let rows: Vec<Value> = (0..100)
            .map(|id| {
                json!({
                    "id": id, "symbol": "BTC_USDT", "side": 1, "vol": 1,
                    "price": 1.0, "fee": 0.0, "taker": true,
                    "timestamp": 1 + id
                })
            })
            .collect();
        let (parsed, raw_count) = parse_deals(&json!({"resultList": rows}), &contracts()).unwrap();
        assert_eq!(raw_count, 100);
        assert_eq!(parsed.len(), raw_count);
    }

    #[test]
    fn an_open_order_carries_its_client_id_and_whether_it_closes() {
        let data = json!({"resultList": [{
            "orderId": "739113577038255616", "symbol": "BTC_USDT", "side": 4,
            "vol": 10, "dealVol": 3, "price": 77500.1, "externalOid": "eng-7", "state": 2
        }]});
        let (out, raw_count) = parse_open_orders(&data, &contracts()).unwrap();
        assert_eq!(raw_count, 1);
        assert_eq!(out[0].client_order_id, "eng-7");
        assert_eq!(out[0].qty, 0.001);
        assert_eq!(out[0].filled_qty, 0.0003);
        assert_eq!(out[0].side, Side::Sell);
        assert!(out[0].reduce_only, "side 4 is a close");
    }

    #[test]
    fn an_unattributable_open_order_invalidates_the_snapshot() {
        for external_oid in [Value::Null, json!(7), json!("")] {
            let data = json!({"resultList": [{
                "symbol": "BTC_USDT", "side": 1, "vol": 1, "dealVol": 0,
                "externalOid": external_oid
            }]});
            assert!(matches!(
                parse_open_orders(&data, &contracts()),
                Err(VenueError::BadReply(_))
            ));
        }
    }

    #[test]
    fn a_full_open_order_page_reports_raw_venue_cardinality() {
        let rows: Vec<Value> = (0..100)
            .map(|id| {
                json!({
                    "symbol": "BTC_USDT", "side": 1, "vol": 1, "dealVol": 0,
                    "externalOid": format!("eng-{id}")
                })
            })
            .collect();
        let (parsed, raw_count) =
            parse_open_orders(&json!({"resultList": rows}), &contracts()).unwrap();
        assert_eq!(raw_count, 100, "a full page was mistaken for the end");
        assert_eq!(
            parsed.len(),
            raw_count,
            "the strict parser silently dropped a row"
        );
    }

    #[test]
    fn an_unconfigured_nonzero_position_invalidates_the_account_snapshot() {
        let configured_elsewhere = json!([{
            "symbol": "BTC_USDT", "holdVol": 5, "positionType": 1
        }]);
        let err = parse_positions(
            &configured_elsewhere,
            &contracts(),
            &HashMap::new(),
            &HashMap::new(),
        )
        .unwrap_err();
        assert!(err.to_string().contains("BTCUSDT"), "{err}");

        let data = json!([{"symbol": "NOTLISTED_USDT", "holdVol": 5, "positionType": 1}]);
        let err = parse_positions(&data, &contracts(), &ids(), &HashMap::new()).unwrap_err();
        assert!(err.to_string().contains("NOTLISTED_USDT"), "{err}");

        let flat = json!([{"symbol": "NOTLISTED_USDT", "holdVol": 0}]);
        assert!(
            parse_positions(&flat, &contracts(), &ids(), &HashMap::new())
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn a_nonzero_position_with_unknown_direction_invalidates_the_snapshot() {
        for position_type in [Value::Null, json!(3), json!("1")] {
            let data = json!([{
                "symbol": "BTC_USDT", "holdVol": 5, "positionType": position_type
            }]);
            assert!(matches!(
                parse_positions(&data, &contracts(), &ids(), &HashMap::new()),
                Err(VenueError::BadReply(_))
            ));
        }
    }
}
