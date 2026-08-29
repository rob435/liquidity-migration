//! Reading Bybit v5 replies. Pure functions over JSON, so the mapping is
//! testable without a socket.
//!
//! Bybit sends every number as a string. A field we cannot read is a
//! `BadReply` — never a zero, never a default. Guessing here would hand the
//! risk kernel a picture of an account that does not exist.

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AccountOrder, AccountPosition, InstrumentRule, OrderAck, VenueExecution, VenueOrder,
};
use engine_types::risk::PositionView;
use engine_types::{Side, VenueError};
use serde_json::Value;

use crate::json::{int_field, kind_of, num_field, opt_num_field, str_field};

const MAX_ASSET_SNAPSHOT_AGE_MS: i64 = 30_000;
const MAX_ASSET_SNAPSHOT_FUTURE_SKEW_MS: i64 = 5_000;

/// Unwrap the `retCode` envelope every v5 endpoint shares.
pub(crate) fn venue_result(envelope: Value) -> Result<Value, VenueError> {
    let mut obj = match envelope {
        Value::Object(o) => o,
        other => {
            return Err(VenueError::BadReply(format!(
                "expected a JSON object, got {}",
                kind_of(&other)
            )));
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

/// Enforce the funded key's required identity and permission shape.
pub(crate) fn verify_funded_key(
    result: &Value,
    expected_ip: &str,
    backup_ip: Option<&str>,
) -> Result<(), VenueError> {
    if int_field(result, "readOnly")? != 0 {
        return Err(VenueError::Credentials(
            "the funded API key is read-only and cannot run the execution engine".to_string(),
        ));
    }
    let mut expected_ips = vec![("BYBIT_REAL_API_KEY_IP", expected_ip)];
    if let Some(backup_ip) = backup_ip.filter(|value| !value.trim().is_empty()) {
        expected_ips.push(("BYBIT_REAL_API_KEY_BACKUP_IP", backup_ip));
    }
    verify_key_identity(result, &expected_ips, "funded API key")?;
    let contract = key_permissions(result, "ContractTrade")?;
    if !contract.contains(&"Order") || !contract.contains(&"Position") {
        return Err(VenueError::Credentials(
            "the funded API key lacks ContractTrade Order+Position permission".to_string(),
        ));
    }
    if key_permissions(result, "Wallet")?.contains(&"Withdraw") {
        return Err(VenueError::Credentials(
            "the funded API key has withdrawal permission; revoke and replace it".to_string(),
        ));
    }
    Ok(())
}

/// Enforce a physically separate, globally read-only inventory credential.
pub(crate) fn verify_attestation_key(result: &Value, expected_ip: &str) -> Result<(), VenueError> {
    if int_field(result, "readOnly")? != 1 {
        return Err(VenueError::Credentials(
            "the attestation API key is not globally read-only".to_string(),
        ));
    }
    verify_key_identity(
        result,
        &[("BYBIT_ATTEST_API_KEY_IP", expected_ip)],
        "attestation API key",
    )?;
    let contract = key_permissions(result, "ContractTrade")?;
    if !contract.contains(&"Order") || !contract.contains(&"Position") {
        return Err(VenueError::Credentials(
            "the attestation API key lacks ContractTrade Order+Position read scope".to_string(),
        ));
    }
    let wallet = key_permissions(result, "Wallet")?;
    if !wallet.contains(&"AccountTransfer") {
        return Err(VenueError::Credentials(
            "the attestation API key lacks Wallet AccountTransfer read scope".to_string(),
        ));
    }
    if wallet.contains(&"Withdraw") {
        return Err(VenueError::Credentials(
            "the attestation API key exposes Wallet Withdraw; revoke and replace it".to_string(),
        ));
    }
    Ok(())
}

fn verify_key_identity(
    result: &Value,
    expected_ips: &[(&str, &str)],
    label: &str,
) -> Result<(), VenueError> {
    if int_field(result, "uta")? != 1 {
        return Err(VenueError::Credentials(format!(
            "the {label} is not bound to a unified trading account"
        )));
    }
    let ips = result
        .get("ips")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("API-key info has no ips array".to_string()))?;
    let mut expected_addresses = Vec::with_capacity(expected_ips.len());
    for (variable, raw_ip) in expected_ips {
        let address = raw_ip
            .trim()
            .parse::<std::net::IpAddr>()
            .map_err(|_| {
                VenueError::Credentials(format!(
                    "{variable} must be one literal production host IP"
                ))
            })?;
        if address.is_unspecified() || address.is_loopback() || address.is_multicast() {
            return Err(VenueError::Credentials(format!(
                "{variable} must name one production host IP"
            )));
        }
        if expected_addresses.contains(&address) {
            return Err(VenueError::Credentials(
                "the funded API key primary and backup IPs must be different".to_string(),
            ));
        }
        expected_addresses.push(address);
    }
    let listed: Vec<&str> = ips
        .iter()
        .map(|ip| ip.as_str().map(str::trim).unwrap_or_default())
        .collect();
    let exact = |ip: &str| {
        if let Ok(address) = ip.parse::<std::net::IpAddr>() {
            return Some(address);
        }
        let (host, prefix) = ip.rsplit_once('/').unwrap_or((ip, ""));
        let host = host.parse::<std::net::IpAddr>().ok();
        match (host, prefix) {
            (Some(std::net::IpAddr::V4(got)), "32") => Some(got.into()),
            (Some(std::net::IpAddr::V6(got)), "128") => Some(got.into()),
            _ => None,
        }
    };
    let listed_addresses = listed.iter().filter_map(|ip| exact(ip)).collect::<Vec<_>>();
    let exact_set = listed.len() == expected_addresses.len()
        && listed_addresses.len() == listed.len()
        && listed_addresses
            .iter()
            .all(|address| expected_addresses.contains(address))
        && expected_addresses
            .iter()
            .all(|address| listed_addresses.contains(address));
    if !exact_set {
        return Err(VenueError::Credentials(format!(
            "the {label} allowlist must exactly match {expected_addresses:?}; venue reports {listed:?}"
        )));
    }
    Ok(())
}

fn key_permissions<'a>(result: &'a Value, group: &str) -> Result<Vec<&'a str>, VenueError> {
    let permissions = result
        .get("permissions")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            VenueError::BadReply("API-key info has no permissions object".to_string())
        })?;
    permissions
        .get(group)
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply(format!("API-key permissions has no {group} array")))?
        .iter()
        .map(|value| {
            value.as_str().ok_or_else(|| {
                VenueError::BadReply(format!("API-key permissions.{group} contains a non-string"))
            })
        })
        .collect()
}

pub(crate) fn parse_order_ack(
    result: &Value,
    client_order_id: &str,
    ack_ns: u64,
) -> Result<OrderAck, VenueError> {
    let venue_order_id = str_field(result, "orderId")?;
    if venue_order_id.is_empty() {
        return Err(VenueError::BadReply(
            "accepted order has a blank orderId".to_string(),
        ));
    }
    Ok(OrderAck {
        client_order_id: client_order_id.to_string(),
        venue_order_id,
        ack_ns,
    })
}

/// Match Bybit's two parallel cancel-batch lists back to the submitted client
/// ids. `result.list` carries identity while `retExtInfo.list` carries each
/// item's outcome; trusting either list alone can acknowledge the wrong pull.
pub(crate) fn parse_cancel_batch(
    result: &Value,
    ret_ext_info: &Value,
    expected_client_order_ids: &[String],
) -> Result<Vec<Result<(), VenueError>>, VenueError> {
    let identities = list_field(result)?;
    let outcomes = ret_ext_info
        .get("list")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            VenueError::BadReply("cancel-batch reply carries no retExtInfo.list".to_string())
        })?;
    if identities.len() != expected_client_order_ids.len()
        || outcomes.len() != expected_client_order_ids.len()
    {
        return Err(VenueError::BadReply(format!(
            "cancel-batch reply counts differ: submitted {}, result.list {}, retExtInfo.list {}",
            expected_client_order_ids.len(),
            identities.len(),
            outcomes.len()
        )));
    }

    let mut replies = Vec::with_capacity(expected_client_order_ids.len());
    for (index, ((identity, outcome), expected)) in identities
        .iter()
        .zip(outcomes)
        .zip(expected_client_order_ids)
        .enumerate()
    {
        let actual = str_field(identity, "orderLinkId")?;
        if actual != expected.as_str() {
            return Err(VenueError::BadReply(format!(
                "cancel-batch result.list[{index}] names orderLinkId {actual:?}, expected {expected:?}"
            )));
        }
        let code = int_field(outcome, "code")?;
        let message = str_field(outcome, "msg")?;
        if code == 0 {
            replies.push(Ok(()));
        } else {
            replies.push(Err(VenueError::Rejected { code, message }));
        }
    }
    Ok(replies)
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
            tracing::warn!(
                symbol,
                "instrument has no price or lot filter; not tradable"
            );
            continue;
        };
        let tick_size = num_field(price_filter, "tickSize")?;
        let qty_step = num_field(lot_filter, "qtyStep")?;
        let min_qty = num_field(lot_filter, "minOrderQty")?;
        // Not every contract publishes a minimum notional; zero means the
        // venue does not impose one.
        let min_notional = opt_num_field(lot_filter, "minNotionalValue")?.unwrap_or(0.0);
        if tick_size <= 0.0 || qty_step <= 0.0 {
            tracing::warn!(
                symbol,
                tick_size,
                qty_step,
                "instrument has a zero tick or step"
            );
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
/// Bybit blanks these totals in some margin modes. A blank is unreadable
/// state and fails here; reading it as zero would look like an emptied
/// account.
pub(crate) fn parse_wallet(result: &Value) -> Result<(f64, f64), VenueError> {
    let rows = list_field(result)?;
    let row = rows
        .first()
        .ok_or_else(|| VenueError::BadReply("wallet reply has no account row".to_string()))?;
    let equity = num_field(row, "totalEquity")?;
    let available = num_field(row, "totalAvailableBalance")?;
    Ok((equity, available))
}

/// Confirm the position row for one explicitly requested symbol is the
/// single position used by one-way mode.
///
/// A symbol query returns a row even when the position is flat. That makes
/// this stronger than the settle-coin account snapshot, which omits flat
/// symbols and therefore cannot prove the mode the next order will use.
pub(crate) fn verify_one_way_position(
    result: &Value,
    expected_symbol: &str,
) -> Result<(), VenueError> {
    let category = str_field(result, "category")?;
    if category != super::CATEGORY {
        return Err(VenueError::BadReply(format!(
            "position-mode reply for {expected_symbol} has category {category:?}, expected {:?}",
            super::CATEGORY
        )));
    }

    // Bybit returns an opaque cursor for a complete symbol-scoped position
    // reply and following it repeats the same row. The limit-200 request fits
    // both possible hedge legs, so the rows below prove the mode; the cursor
    // remains type-checked but is not distinct-position evidence.
    str_field(result, "nextPageCursor")?;

    let rows = list_field(result)?;
    if rows.len() != 1 {
        return Err(VenueError::BadReply(format!(
            "position-mode reply for {expected_symbol} has {} rows, expected exactly one",
            rows.len()
        )));
    }
    let row = &rows[0];
    let reported_symbol = str_field(row, "symbol")?;
    if reported_symbol != expected_symbol {
        return Err(VenueError::BadReply(format!(
            "position-mode reply asked for {expected_symbol} but names {reported_symbol}"
        )));
    }

    let position_idx = int_field(row, "positionIdx")?;
    if position_idx != 0 {
        return Err(VenueError::BadReply(format!(
            "{expected_symbol} is not in one-way position mode (positionIdx={position_idx})"
        )));
    }
    Ok(())
}

/// Open positions, with the cursor for the next page.
///
/// Every non-flat position must resolve through the configured symbol table.
/// Dropping an unfamiliar one would understate account exposure and let the
/// risk kernel act on a partial account snapshot.
pub(crate) fn parse_positions(
    result: &Value,
    resolve: &dyn Fn(&str) -> Option<SymbolId>,
) -> Result<(Vec<PositionView>, String), VenueError> {
    let rows = list_field(result)?;
    let mut out = Vec::new();
    for row in rows {
        let symbol = str_field(row, "symbol")?;
        let qty = num_field(row, "size")?;
        // A flat position comes back with size 0 and a blank side.
        if qty == 0.0 {
            continue;
        }
        if qty < 0.0 {
            return Err(VenueError::BadReply(format!(
                "position in {symbol} has a negative size {qty}"
            )));
        }
        let id = resolve(&symbol).ok_or_else(|| {
            VenueError::BadReply(format!(
                "nonzero position in {symbol} is absent from the configured symbol table"
            ))
        })?;
        let side_raw = str_field(row, "side")?;
        let side = match side_raw.as_str() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            other => {
                return Err(VenueError::BadReply(format!(
                    "position in {symbol} has an unknown side {other:?}"
                )));
            }
        };
        // Bybit writes "" or "0" when no stop is set on the position.
        let stop_px = opt_num_field(row, "stopLoss")?.filter(|v| *v > 0.0);
        let stop_attached = stop_px.is_some();
        // The leverage the venue says this position runs at. Zero or absent
        // reads as unknown (cross-margin rows can blank it), never as a fact.
        let leverage = opt_num_field(row, "leverage")?.filter(|v| *v > 0.0);
        out.push(PositionView {
            symbol: id,
            side,
            qty,
            entry_px: num_field(row, "avgPrice")?,
            stop_attached,
            stop_px: stop_px.unwrap_or(0.0),
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
        let client_order_id = str_field(row, "orderLinkId")?;
        if client_order_id.is_empty() && !is_supported_native_position_stop(row)? {
            return Err(VenueError::BadReply(format!(
                "working order in {symbol} has no attributable orderLinkId and is not a supported native position stop"
            )));
        }
        let side = match str_field(row, "side")?.as_str() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            other => {
                return Err(VenueError::BadReply(format!(
                    "working order in {symbol} has an unknown side {other:?}"
                )));
            }
        };
        out.push(VenueOrder {
            // A positively identified full-position stop is the one supported
            // order allowed to carry no client id. Reconciliation recognises
            // that empty id as the venue-native protective order.
            client_order_id,
            symbol,
            side,
            qty: num_field(row, "qty")?,
            filled_qty: opt_num_field(row, "cumExecQty")?.unwrap_or(0.0),
            reduce_only: row
                .get("reduceOnly")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        });
    }
    Ok((out, next_cursor(result)))
}

pub(crate) fn parse_inventory_positions(
    result: &Value,
    product: &str,
) -> Result<(Vec<AccountPosition>, String), VenueError> {
    let category = str_field(result, "category")?;
    if category != product {
        return Err(VenueError::BadReply(format!(
            "inventory asked for {product} positions but the venue returned {category}"
        )));
    }
    let cursor = str_field(result, "nextPageCursor")?;
    let mut out = Vec::new();
    for row in list_field(result)? {
        let symbol = str_field(row, "symbol")?;
        let qty = num_field(row, "size")?;
        if qty == 0.0 {
            continue;
        }
        if qty < 0.0 {
            return Err(VenueError::BadReply(format!(
                "inventory position in {symbol} has negative size {qty}"
            )));
        }
        let side = match str_field(row, "side")?.as_str() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            other => {
                return Err(VenueError::BadReply(format!(
                    "inventory position in {symbol} has unknown side {other:?}"
                )));
            }
        };
        out.push(AccountPosition {
            product: product.to_string(),
            symbol,
            side,
            qty,
        });
    }
    Ok((out, cursor))
}

pub(crate) fn parse_inventory_orders(
    result: &Value,
    product: &str,
) -> Result<(Vec<AccountOrder>, String), VenueError> {
    let category = str_field(result, "category")?;
    if category != product {
        return Err(VenueError::BadReply(format!(
            "inventory asked for {product} orders but the venue returned {category}"
        )));
    }
    let cursor = str_field(result, "nextPageCursor")?;
    let mut out = Vec::new();
    for row in list_field(result)? {
        out.push(AccountOrder {
            product: product.to_string(),
            symbol: str_field(row, "symbol")?,
            client_order_id: str_field(row, "orderLinkId")?,
        });
    }
    Ok((out, cursor))
}

/// Every non-final quote this account has sent through Bybit RFQ.
///
/// An inquirer can execute an active quote without another action by this
/// process, so it is latent exposure in exactly the same sense as an open
/// order. The realtime endpoint is intentionally unpaged and documents that
/// it returns all non-final quotes; a malformed row fails the attestation
/// rather than being skipped.
pub(crate) fn parse_rfq_quotes(
    result: &Value,
    role: &str,
) -> Result<Vec<AccountOrder>, VenueError> {
    if !matches!(role, "quote" | "request") {
        return Err(VenueError::BadRequest(format!(
            "invalid RFQ quote role {role:?}"
        )));
    }
    let mut out = Vec::new();
    for row in list_field(result)? {
        let quote_id = str_field(row, "quoteId")?;
        if quote_id.is_empty() {
            return Err(VenueError::BadReply(
                "active RFQ quote has no quoteId".to_string(),
            ));
        }
        let status = str_field(row, "status")?;
        if status.is_empty() {
            return Err(VenueError::BadReply(format!(
                "RFQ quote {quote_id} has no status"
            )));
        }
        let mut legs = Vec::new();
        for field in ["quoteBuyList", "quoteSellList"] {
            let side = row.get(field).and_then(Value::as_array).ok_or_else(|| {
                VenueError::BadReply(format!("RFQ quote {quote_id} has no {field} array"))
            })?;
            for leg in side {
                let category = str_field(leg, "category")?;
                let symbol = str_field(leg, "symbol")?;
                if category.is_empty() || symbol.is_empty() {
                    return Err(VenueError::BadReply(format!(
                        "RFQ quote {quote_id} contains an unnamed product leg"
                    )));
                }
                legs.push(format!("{category}:{symbol}"));
            }
        }
        if legs.is_empty() {
            return Err(VenueError::BadReply(format!(
                "RFQ quote {quote_id} contains no executable legs"
            )));
        }
        let link_id = str_field(row, "quoteLinkId")?;
        out.push(AccountOrder {
            product: format!("rfq_quote:{role}:{status}"),
            symbol: legs.join(","),
            client_order_id: if link_id.is_empty() {
                quote_id
            } else {
                link_id
            },
        });
    }
    Ok(out)
}

/// Active inquiries created by this account. These corroborate RFQ state and,
/// together with the `request` quote role, keep asynchronous executions from
/// hiding between quote acceptance and position creation.
pub(crate) fn parse_rfq_requests(result: &Value) -> Result<Vec<AccountOrder>, VenueError> {
    let mut out = Vec::new();
    for row in list_field(result)? {
        let rfq_id = str_field(row, "rfqId")?;
        if rfq_id.is_empty() {
            return Err(VenueError::BadReply(
                "active RFQ inquiry has no rfqId".to_string(),
            ));
        }
        let status = str_field(row, "status")?;
        let legs = row.get("legs").and_then(Value::as_array).ok_or_else(|| {
            VenueError::BadReply(format!("RFQ inquiry {rfq_id} has no legs array"))
        })?;
        if legs.is_empty() {
            return Err(VenueError::BadReply(format!(
                "RFQ inquiry {rfq_id} contains no legs"
            )));
        }
        let mut symbols = Vec::with_capacity(legs.len());
        for leg in legs {
            let category = str_field(leg, "category")?;
            let symbol = str_field(leg, "symbol")?;
            if category.is_empty() || symbol.is_empty() {
                return Err(VenueError::BadReply(format!(
                    "RFQ inquiry {rfq_id} contains an unnamed product leg"
                )));
            }
            symbols.push(format!("{category}:{symbol}"));
        }
        let link_id = str_field(row, "rfqLinkId")?;
        out.push(AccountOrder {
            product: format!("rfq_inquiry:{status}"),
            symbol: symbols.join(","),
            client_order_id: if link_id.is_empty() { rfq_id } else { link_id },
        });
    }
    Ok(out)
}

/// Active venue-native TWAP, chase, iceberg and POV strategies. A strategy
/// may have no child order at the instant normal open orders are sampled and
/// create one later, so every non-terminated status is latent inventory.
pub(crate) fn parse_active_strategies(
    result: &Value,
    expected_status: i64,
) -> Result<(Vec<AccountOrder>, String), VenueError> {
    if !matches!(expected_status, 2 | 4 | 5 | 6) {
        return Err(VenueError::BadRequest(format!(
            "invalid active strategy status {expected_status}"
        )));
    }
    let cursor = str_field(result, "nextCursor")?;
    let mut out = Vec::new();
    for row in list_field(result)? {
        let status = int_field(row, "status")?;
        if status != expected_status {
            return Err(VenueError::BadReply(format!(
                "strategy inventory asked for status {expected_status} but the venue returned {status}"
            )));
        }
        let strategy_id = str_field(row, "strategyId")?;
        let category = str_field(row, "category")?;
        let symbol = str_field(row, "symbol")?;
        let strategy_type = str_field(row, "strategyType")?;
        if strategy_id.is_empty()
            || category.is_empty()
            || symbol.is_empty()
            || strategy_type.is_empty()
        {
            return Err(VenueError::BadReply(
                "active venue strategy has an empty identity field".to_string(),
            ));
        }
        out.push(AccountOrder {
            product: format!("venue_strategy:{strategy_type}:status-{status}"),
            symbol: format!("{category}:{symbol}"),
            client_order_id: strategy_id,
        });
    }
    Ok((out, cursor))
}

/// Non-cash wallet assets and every explicit spot liability. Positive USDT
/// and USDC are the account's cash, not exposure; a negative cash balance or
/// borrow is still a blocker. Unknown assets are kept by name rather than
/// being filtered through the engine's symbol table.
pub(crate) fn parse_inventory_wallet(result: &Value) -> Result<Vec<AccountPosition>, VenueError> {
    let rows = list_field(result)?;
    if rows.len() != 1 {
        return Err(VenueError::BadReply(format!(
            "wallet inventory has {} account rows, expected exactly one",
            rows.len()
        )));
    }
    let account = &rows[0];
    if str_field(account, "accountType")? != "UNIFIED" {
        return Err(VenueError::BadReply(
            "wallet inventory did not return the UNIFIED account".to_string(),
        ));
    }
    let coins = account
        .get("coin")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("wallet inventory has no coin array".to_string()))?;
    let mut out = Vec::new();
    for row in coins {
        let coin = str_field(row, "coin")?;
        if coin.is_empty() {
            return Err(VenueError::BadReply(
                "wallet inventory contains an unnamed coin".to_string(),
            ));
        }
        let wallet = num_field(row, "walletBalance")?;
        let borrow = num_field(row, "borrowAmount")?;
        if borrow < 0.0 {
            return Err(VenueError::BadReply(format!(
                "wallet inventory has negative borrowAmount for {coin}"
            )));
        }
        let is_cash = matches!(coin.as_str(), "USDT" | "USDC");
        if wallet != 0.0 && (!is_cash || wallet < 0.0) {
            out.push(AccountPosition {
                product: "wallet_asset".to_string(),
                symbol: coin.clone(),
                side: if wallet > 0.0 { Side::Buy } else { Side::Sell },
                qty: wallet.abs(),
            });
        }
        if borrow > 0.0 {
            out.push(AccountPosition {
                product: "spot_liability".to_string(),
                symbol: coin,
                side: Side::Sell,
                qty: borrow,
            });
        }
    }
    Ok(out)
}

/// Holdings outside the unified trading wallet, including the separate
/// accounts used by venue-hosted bots and copy trading.
///
/// A bot/copy account is latent exposure even while its reported equity is
/// zero: the venue-owned strategy can create its next child order after the
/// normal order snapshot. Every such category is therefore represented as an
/// open-order blocker. Nonzero holdings in every product account are retained
/// as positions; only positive USDT/USDC in the ordinary unified and funding
/// wallets is cash. A partial or stale cross-account response therefore cannot
/// be mistaken for a complete proof.
pub(crate) fn parse_asset_overview(
    result: &Value,
    now_ms: i64,
) -> Result<(Vec<AccountPosition>, Vec<AccountOrder>), VenueError> {
    let total_equity = num_field(result, "totalEquity")?;
    let rows = list_field(result)?;
    if rows.is_empty() {
        return Err(VenueError::BadReply(
            "asset overview returned no account rows, so snapshot freshness cannot be proved"
                .to_string(),
        ));
    }

    let mut positions = Vec::new();
    let mut latent = Vec::new();
    let mut saw_nonzero_account_equity = false;
    for row in rows {
        let account_type = str_field(row, "accountType")?;
        if account_type.is_empty() {
            return Err(VenueError::BadReply(
                "asset overview contains an unnamed account type".to_string(),
            ));
        }
        let account_equity = num_field(row, "totalEquity")?;
        saw_nonzero_account_equity |= account_equity != 0.0;
        let valuation = str_field(row, "valuationCurrency")?;
        if valuation != "USD" {
            return Err(VenueError::BadReply(format!(
                "asset overview account {account_type:?} is valued in {valuation:?}, not requested USD"
            )));
        }
        let snapshot_ms = int_field(row, "snapshotTime")?;
        let age_ms = now_ms.saturating_sub(snapshot_ms);
        if snapshot_ms <= 0
            || age_ms > MAX_ASSET_SNAPSHOT_AGE_MS
            || snapshot_ms > now_ms.saturating_add(MAX_ASSET_SNAPSHOT_FUTURE_SKEW_MS)
        {
            return Err(VenueError::BadReply(format!(
                "asset overview account {account_type:?} has a stale or future snapshot: snapshotTime={snapshot_ms} now={now_ms}"
            )));
        }

        let blocks = matches!(account_type.as_str(), "TradingBot" | "CopyTrading");
        // Positive USDT/USDC in the ordinary funding or unified wallet is
        // deployable cash, just as it is in `parse_inventory_wallet`. In every
        // product account (Earn, loans, Alpha, staking, bots, copy trading,
        // and any future account type), a non-zero coin row is inventory and
        // must survive into the flatness proof. Negative cash is a liability
        // everywhere and is never exempted.
        let cash_account = matches!(
            account_type.as_str(),
            "FundingAccount" | "UnifiedTradingAccount"
        );
        let mut account_has_explicit_positive_cash = false;
        let account_position_start = positions.len();
        let direct_coins = row.get("coinDetail").and_then(Value::as_array);
        let categories = row.get("categories").and_then(Value::as_array);
        if direct_coins.is_none() && categories.is_none() {
            return Err(VenueError::BadReply(format!(
                "asset overview account {account_type:?} has neither coinDetail nor categories"
            )));
        }

        if let Some(coins) = direct_coins {
            account_has_explicit_positive_cash |=
                cash_account && asset_rows_have_positive_cash(coins)?;
            parse_asset_holding_rows(
                coins,
                &account_type,
                &account_type,
                cash_account,
                &mut positions,
            )?;
        }

        let mut category_count = 0usize;
        if let Some(categories) = categories {
            for category in categories {
                let name = str_field(category, "category")?;
                if name.is_empty() {
                    return Err(VenueError::BadReply(format!(
                        "asset overview account {account_type:?} contains an unnamed category"
                    )));
                }
                let category_equity = num_field(category, "equity")?;
                let coins = category
                    .get("coinDetail")
                    .and_then(Value::as_array)
                    .ok_or_else(|| {
                        VenueError::BadReply(format!(
                            "asset overview category {account_type}:{name} has no coinDetail array"
                        ))
                    })?;
                let category_has_explicit_positive_cash =
                    cash_account && asset_rows_have_positive_cash(coins)?;
                account_has_explicit_positive_cash |= category_has_explicit_positive_cash;
                let category_position_start = positions.len();
                parse_asset_holding_rows(
                    coins,
                    &account_type,
                    &name,
                    cash_account,
                    &mut positions,
                )?;
                // Some product accounts report only an aggregate valuation
                // and an empty detail array. The aggregate is not a tradable
                // quantity, but a synthetic row is enough for the boolean
                // flatness proof and is safer than attesting the value away.
                if category_equity != 0.0
                    && positions.len() == category_position_start
                    && (!cash_account
                        || category_equity < 0.0
                        || !category_has_explicit_positive_cash)
                {
                    positions.push(AccountPosition {
                        product: format!("asset_account_equity:{account_type}:{name}"),
                        symbol: format!("{account_type}:{name}"),
                        side: if category_equity > 0.0 {
                            Side::Buy
                        } else {
                            Side::Sell
                        },
                        qty: category_equity.abs(),
                    });
                }
                category_count += 1;
                if blocks {
                    latent.push(AccountOrder {
                        product: format!("asset_account:{account_type}"),
                        symbol: name.clone(),
                        client_order_id: format!("{account_type}:{name}"),
                    });
                }
            }
        }
        // The documented bot/copy shape uses categories. Retaining a row-level
        // blocker as a fallback makes a newly introduced direct-holdings shape
        // fail closed without inventing a quantity.
        if blocks && category_count == 0 {
            latent.push(AccountOrder {
                product: format!("asset_account:{account_type}"),
                symbol: account_type.clone(),
                client_order_id: account_type.clone(),
            });
        }
        if account_equity != 0.0
            && positions.len() == account_position_start
            && (!cash_account || account_equity < 0.0 || !account_has_explicit_positive_cash)
        {
            positions.push(AccountPosition {
                product: format!("asset_account_equity:{account_type}"),
                symbol: account_type.clone(),
                side: if account_equity > 0.0 {
                    Side::Buy
                } else {
                    Side::Sell
                },
                qty: account_equity.abs(),
            });
        }
    }
    // Bybit documents each aggregate but does not contract that asynchronously
    // stamped, rounded rows add algebraically to the top-level figure; its own
    // response example does not. Do not reject that valid shape. The one
    // arithmetic fact that is safe for a flatness proof is that a nonzero
    // top-level total cannot disappear behind an all-zero account list.
    if total_equity != 0.0 && !saw_nonzero_account_equity {
        positions.push(AccountPosition {
            product: "asset_overview_unallocated_equity".to_string(),
            symbol: "asset_overview_total".to_string(),
            side: if total_equity > 0.0 {
                Side::Buy
            } else {
                Side::Sell
            },
            qty: total_equity.abs(),
        });
    }
    Ok((positions, latent))
}

fn asset_rows_have_positive_cash(rows: &[Value]) -> Result<bool, VenueError> {
    let mut found = false;
    for row in rows {
        let coin = str_field(row, "coin")?;
        let equity = num_field(row, "equity")?;
        found |= equity > 0.0 && matches!(coin.as_str(), "USDT" | "USDC");
    }
    Ok(found)
}

fn parse_asset_holding_rows(
    rows: &[Value],
    account_type: &str,
    category: &str,
    cash_account: bool,
    out: &mut Vec<AccountPosition>,
) -> Result<(), VenueError> {
    for row in rows {
        let coin = str_field(row, "coin")?;
        if coin.is_empty() {
            return Err(VenueError::BadReply(format!(
                "asset overview category {account_type}:{category} contains an unnamed coin"
            )));
        }
        let equity = num_field(row, "equity")?;
        let ordinary_positive_cash =
            cash_account && equity > 0.0 && matches!(coin.as_str(), "USDT" | "USDC");
        if equity != 0.0 && !ordinary_positive_cash {
            out.push(AccountPosition {
                product: format!("asset_account_holding:{account_type}:{category}"),
                symbol: coin,
                side: if equity > 0.0 { Side::Buy } else { Side::Sell },
                qty: equity.abs(),
            });
        }
    }
    Ok(())
}

/// Settlement coins advertised for linear contracts. USDT and USDC are also
/// always scanned by the caller so a temporarily empty/delisted product class
/// cannot disappear from the account proof.
pub(crate) fn parse_linear_settle_coins(
    result: &Value,
) -> Result<(Vec<String>, String), VenueError> {
    let category = str_field(result, "category")?;
    if category != "linear" {
        return Err(VenueError::BadReply(format!(
            "linear instrument inventory returned category {category:?}"
        )));
    }
    let cursor = str_field(result, "nextPageCursor")?;
    let mut out = Vec::new();
    for row in list_field(result)? {
        let coin = str_field(row, "settleCoin")?;
        if coin.is_empty() {
            return Err(VenueError::BadReply(
                "linear instrument inventory contains an empty settleCoin".to_string(),
            ));
        }
        if !out.contains(&coin) {
            out.push(coin);
        }
    }
    Ok((out, cursor))
}

pub(crate) fn parse_spread_orders(
    result: &Value,
) -> Result<(Vec<AccountOrder>, String), VenueError> {
    let cursor = str_field(result, "nextPageCursor")?;
    let mut out = Vec::new();
    for row in list_field(result)? {
        out.push(AccountOrder {
            product: "spread".to_string(),
            symbol: str_field(row, "symbol")?,
            client_order_id: str_field(row, "orderLinkId")?,
        });
    }
    Ok((out, cursor))
}

fn is_supported_native_position_stop(row: &Value) -> Result<bool, VenueError> {
    let stop_order_type = str_field(row, "stopOrderType")?;
    let tpsl_mode = str_field(row, "tpslMode")?;
    let reduce_only = row
        .get("reduceOnly")
        .and_then(Value::as_bool)
        .ok_or_else(|| {
            VenueError::BadReply(
                "field reduceOnly is missing or not a boolean on an unlinked working order"
                    .to_string(),
            )
        })?;
    Ok(stop_order_type == "StopLoss" && tpsl_mode == "Full" && reduce_only)
}

/// One page of `/v5/execution/list`. Only quantity-moving executions come
/// back: a funding charge appears in this history too, and folding it into a
/// position sum would corrupt the very number this read exists to repair.
pub(crate) fn parse_executions(
    result: &Value,
) -> Result<(Vec<VenueExecution>, String), VenueError> {
    let rows = list_field(result)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        // Trade, AdlTrade, BustTrade and Settle move the position; Funding
        // and the rest do not.
        let exec_type = str_field(row, "execType").unwrap_or_default();
        if !matches!(
            exec_type.as_str(),
            "Trade" | "AdlTrade" | "BustTrade" | "Settle"
        ) {
            continue;
        }
        let symbol = str_field(row, "symbol")?;
        let side = match str_field(row, "side")?.as_str() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            other => {
                return Err(VenueError::BadReply(format!(
                    "execution in {symbol} has an unknown side {other:?}"
                )));
            }
        };
        let exec_id = str_field(row, "execId")?;
        if exec_id.is_empty() {
            return Err(VenueError::BadReply(format!(
                "quantity-moving execution in {symbol} has no execId"
            )));
        }
        let qty = num_field(row, "execQty")?;
        let px = num_field(row, "execPrice")?;
        let venue_ts_ms = int_field(row, "execTime")?;
        if qty <= 0.0 || px <= 0.0 || venue_ts_ms <= 0 {
            return Err(VenueError::BadReply(format!(
                "execution {exec_id} in {symbol} has non-positive quantity, price, or timestamp"
            )));
        }
        out.push(VenueExecution {
            exec_id,
            client_order_id: str_field(row, "orderLinkId").unwrap_or_default(),
            symbol,
            side,
            qty,
            px,
            fee: opt_num_field(row, "execFee")?.unwrap_or(0.0),
            is_maker: row.get("isMaker").and_then(Value::as_bool).unwrap_or(false),
            venue_ts_ms,
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn inventory_keeps_unknown_and_delisted_positions_by_name() {
        let result = json!({
            "category": "linear",
            "nextPageCursor": "next",
            "list": [
                {"symbol": "DELISTEDUSDT", "size": "2.5", "side": "Sell"},
                {"symbol": "FLATUSDT", "size": "0", "side": ""}
            ]
        });
        let (rows, cursor) = parse_inventory_positions(&result, "linear").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].symbol, "DELISTEDUSDT");
        assert_eq!(rows[0].side, Side::Sell);
        assert_eq!(cursor, "next");
    }

    #[test]
    fn inventory_keeps_unlinked_orders_as_flatness_blockers() {
        let result = json!({
            "category": "option",
            "nextPageCursor": "",
            "list": [{"symbol": "BTC-30AUG26-100000-C", "orderLinkId": ""}]
        });
        let (rows, cursor) = parse_inventory_orders(&result, "option").unwrap();
        assert_eq!(rows.len(), 1);
        assert!(rows[0].client_order_id.is_empty());
        assert!(cursor.is_empty());
    }

    #[test]
    fn inventory_refuses_a_category_mismatch_or_missing_cursor() {
        let wrong = json!({"category": "inverse", "nextPageCursor": "", "list": []});
        assert!(parse_inventory_positions(&wrong, "linear").is_err());
        let incomplete = json!({"category": "linear", "list": []});
        assert!(parse_inventory_orders(&incomplete, "linear").is_err());
    }

    fn funded_key() -> Value {
        json!({
            "readOnly": 0,
            "uta": 1,
            "ips": ["203.0.113.7"],
            "permissions": {
                "ContractTrade": ["Order", "Position"],
                "Wallet": []
            }
        })
    }

    #[test]
    fn funded_key_gate_requires_ip_scope_and_safe_permissions() {
        verify_funded_key(&funded_key(), "203.0.113.7", None).unwrap();

        let mut no_ips = funded_key();
        no_ips["ips"] = json!([]);
        let mut withdrawal = funded_key();
        withdrawal["permissions"]["Wallet"] = json!(["Withdraw"]);
        let mut read_only = funded_key();
        read_only["readOnly"] = json!(1);
        let mut classic = funded_key();
        classic["uta"] = json!(0);
        let mut insufficient = funded_key();
        insufficient["permissions"]["ContractTrade"] = json!(["Order"]);
        for refused in [no_ips, withdrawal, read_only, classic, insufficient] {
            assert!(
                verify_funded_key(&refused, "203.0.113.7", None).is_err(),
                "accepted {refused}"
            );
        }
        let mut extra_ip = funded_key();
        extra_ip["ips"] = json!(["203.0.113.7", "198.51.100.2"]);
        assert!(verify_funded_key(&extra_ip, "203.0.113.7", None).is_err());
        verify_funded_key(&extra_ip, "203.0.113.7", Some("198.51.100.2")).unwrap();
        extra_ip["ips"] = json!(["198.51.100.2/32", "203.0.113.7/32"]);
        verify_funded_key(&extra_ip, "203.0.113.7", Some("198.51.100.2")).unwrap();
        assert!(verify_funded_key(&extra_ip, "203.0.113.7", Some("198.51.100.3")).is_err());
        assert!(verify_funded_key(&extra_ip, "203.0.113.7", Some("203.0.113.7")).is_err());
        assert!(verify_funded_key(&funded_key(), "0.0.0.0/0", None).is_err());
        assert!(verify_funded_key(&funded_key(), "not-an-ip", None).is_err());
    }

    #[test]
    fn attestation_key_must_be_separate_globally_read_only_and_inventory_capable() {
        let mut key = funded_key();
        key["readOnly"] = json!(1);
        key["permissions"]["Wallet"] = json!(["AccountTransfer"]);
        verify_attestation_key(&key, "203.0.113.7").unwrap();

        let mut writable = key.clone();
        writable["readOnly"] = json!(0);
        let mut no_inventory = key.clone();
        no_inventory["permissions"]["Wallet"] = json!([]);
        let mut withdrawal = key.clone();
        withdrawal["permissions"]["Wallet"] = json!(["AccountTransfer", "Withdraw"]);
        for refused in [writable, no_inventory, withdrawal] {
            assert!(verify_attestation_key(&refused, "203.0.113.7").is_err());
        }
    }

    #[test]
    fn wallet_inventory_ignores_cash_but_keeps_assets_and_liabilities() {
        let result = json!({"list": [{
            "accountType": "UNIFIED",
            "coin": [
                {"coin": "USDT", "walletBalance": "125", "borrowAmount": "0"},
                {"coin": "USDC", "walletBalance": "10", "borrowAmount": "2"},
                {"coin": "BTC", "walletBalance": "0.25", "borrowAmount": "0.01"},
                {"coin": "ETH", "walletBalance": "0", "borrowAmount": "0"}
            ]
        }]});
        let rows = parse_inventory_wallet(&result).unwrap();
        assert_eq!(rows.len(), 3);
        assert!(rows.iter().any(|row| {
            row.product == "wallet_asset" && row.symbol == "BTC" && row.side == Side::Buy
        }));
        assert!(rows.iter().any(|row| {
            row.product == "spot_liability" && row.symbol == "BTC" && row.side == Side::Sell
        }));
        assert!(rows.iter().any(|row| {
            row.product == "spot_liability" && row.symbol == "USDC" && row.side == Side::Sell
        }));
    }

    #[test]
    fn wallet_inventory_refuses_an_incomplete_debt_picture() {
        let missing_borrow = json!({"list": [{
            "accountType": "UNIFIED",
            "coin": [{"coin": "BTC", "walletBalance": "1"}]
        }]});
        assert!(parse_inventory_wallet(&missing_borrow).is_err());
        let wrong_account = json!({"list": [{
            "accountType": "CONTRACT",
            "coin": []
        }]});
        assert!(parse_inventory_wallet(&wrong_account).is_err());
    }

    #[test]
    fn asset_overview_blocks_every_bot_and_copy_category_even_at_zero() {
        let now_ms = 1_700_000_000_000_i64;
        let result = json!({
            "totalEquity": "1003.75",
            "list": [
                {
                    "accountType": "UnifiedTradingAccount",
                    "totalEquity": "1000",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms.to_string(),
                    "categories": [{
                        "category": "crypto", "equity": "1000",
                        "coinDetail": [{"coin": "USDT", "equity": "1000"}]
                    }]
                },
                {
                    "accountType": "TradingBot",
                    "totalEquity": "4",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms.to_string(),
                    "categories": [
                        {
                            "category": "Futures Grid Bot", "equity": "0",
                            "coinDetail": []
                        },
                        {
                            "category": "Futures Combo Bot", "equity": "4",
                            "coinDetail": [{"coin": "USDT", "equity": "4"}]
                        }
                    ]
                },
                {
                    "accountType": "CopyTrading",
                    "totalEquity": "-0.25",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms.to_string(),
                    "categories": [{
                        "category": "Copy Trading Pro", "equity": "-0.25",
                        "coinDetail": [{"coin": "BTC", "equity": "-0.25"}]
                    }]
                }
            ]
        });

        let (positions, latent) = parse_asset_overview(&result, now_ms).unwrap();
        assert_eq!(latent.len(), 3, "zero-equity bot categories are still live");
        assert!(latent.iter().any(|row| row.symbol == "Futures Grid Bot"));
        assert!(latent.iter().any(|row| row.symbol == "Futures Combo Bot"));
        assert!(latent.iter().any(|row| row.symbol == "Copy Trading Pro"));
        assert_eq!(positions.len(), 2);
        assert!(positions
            .iter()
            .any(|row| { row.symbol == "USDT" && row.side == Side::Buy && row.qty == 4.0 }));
        assert!(positions
            .iter()
            .any(|row| { row.symbol == "BTC" && row.side == Side::Sell && row.qty == 0.25 }));
    }

    #[test]
    fn asset_overview_keeps_non_cash_product_holdings_and_liabilities() {
        let now_ms = 1_700_000_000_000_i64;
        let result = json!({
            "totalEquity": "1210",
            "list": [
                {
                    "accountType": "UnifiedTradingAccount",
                    "totalEquity": "1000",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "categories": [{
                        "category": "crypto", "equity": "1000",
                        "coinDetail": [
                            {"coin": "USDT", "equity": "1000"},
                            {"coin": "BTC", "equity": "0.01"},
                            {"coin": "USDC", "equity": "-2"}
                        ]
                    }]
                },
                {
                    "accountType": "CryptoLoans",
                    "totalEquity": "150",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "coinDetail": [
                        {"coin": "USDT", "equity": "200"},
                        {"coin": "BTC", "equity": "-0.001"}
                    ]
                },
                {
                    "accountType": "Earn",
                    "totalEquity": "50",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "categories": [{
                        "category": "Easy Earn", "equity": "50",
                        "coinDetail": [{"coin": "USDT", "equity": "50"}]
                    }]
                },
                {
                    "accountType": "FundingAccount",
                    "totalEquity": "10",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "coinDetail": [
                        {"coin": "USDC", "equity": "10"},
                        {"coin": "SOL", "equity": "0.5"}
                    ]
                }
            ]
        });

        let (positions, latent) = parse_asset_overview(&result, now_ms).unwrap();
        assert!(latent.is_empty());
        assert_eq!(positions.len(), 6);
        assert!(positions.iter().any(|row| {
            row.product == "asset_account_holding:CryptoLoans:CryptoLoans"
                && row.symbol == "USDT"
                && row.side == Side::Buy
                && row.qty == 200.0
        }));
        assert!(positions.iter().any(|row| {
            row.product == "asset_account_holding:Earn:Easy Earn"
                && row.symbol == "USDT"
                && row.side == Side::Buy
                && row.qty == 50.0
        }));
        assert!(positions.iter().any(|row| {
            row.product == "asset_account_holding:UnifiedTradingAccount:crypto"
                && row.symbol == "USDC"
                && row.side == Side::Sell
                && row.qty == 2.0
        }));
        assert!(positions.iter().any(|row| row.symbol == "SOL"));
        assert!(!positions
            .iter()
            .any(|row| row.symbol == "USDT" && row.qty == 1000.0));
        assert!(!positions
            .iter()
            .any(|row| row.symbol == "USDC" && row.qty == 10.0));
    }

    #[test]
    fn asset_overview_does_not_attest_aggregate_only_product_value_as_flat() {
        let now_ms = 1_700_000_000_000_i64;
        let result = json!({
            "totalEquity": "160",
            "list": [
                {
                    "accountType": "Earn",
                    "totalEquity": "100",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "coinDetail": []
                },
                {
                    "accountType": "CryptoLoans",
                    "totalEquity": "50",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "categories": [{
                        "category": "Collateral", "equity": "50", "coinDetail": []
                    }]
                },
                {
                    "accountType": "FundingAccount",
                    "totalEquity": "10",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "coinDetail": []
                }
            ]
        });

        let (positions, latent) = parse_asset_overview(&result, now_ms).unwrap();
        assert!(latent.is_empty());
        assert_eq!(positions.len(), 3);
        assert!(positions.iter().any(|row| {
            row.product == "asset_account_equity:Earn" && row.side == Side::Buy && row.qty == 100.0
        }));
        assert!(positions.iter().any(|row| {
            row.product == "asset_account_equity:CryptoLoans:Collateral"
                && row.side == Side::Buy
                && row.qty == 50.0
        }));
        assert!(positions.iter().any(|row| {
            row.product == "asset_account_equity:FundingAccount"
                && row.side == Side::Buy
                && row.qty == 10.0
        }));
    }

    #[test]
    fn asset_overview_keeps_aggregate_only_cash_liabilities() {
        let now_ms = 1_700_000_000_000_i64;
        let result = json!({
            "totalEquity": "-3",
            "list": [{
                "accountType": "UnifiedTradingAccount",
                "totalEquity": "-3",
                "valuationCurrency": "USD",
                "snapshotTime": now_ms,
                "coinDetail": []
            }]
        });

        let (positions, _) = parse_asset_overview(&result, now_ms).unwrap();
        assert_eq!(positions.len(), 1);
        assert_eq!(positions[0].side, Side::Sell);
        assert_eq!(positions[0].qty, 3.0);
    }

    #[test]
    fn asset_overview_requires_a_complete_fresh_snapshot() {
        let now_ms = 1_700_000_000_000_i64;
        let page = |snapshot: Value| {
            json!({
                "totalEquity": "0",
                "list": [{
                    "accountType": "TradingBot",
                    "totalEquity": "0",
                    "valuationCurrency": "USD",
                    "snapshotTime": snapshot,
                    "categories": [{
                        "category": "Futures Grid Bot", "equity": "0", "coinDetail": []
                    }]
                }]
            })
        };

        assert!(parse_asset_overview(&page(json!(now_ms - 30_001)), now_ms).is_err());
        assert!(parse_asset_overview(&page(json!(now_ms + 5_001)), now_ms).is_err());
        assert!(parse_asset_overview(&json!({"totalEquity": "0", "list": []}), now_ms).is_err());

        let incomplete = json!({
            "totalEquity": "0",
            "list": [{
                "accountType": "TradingBot", "totalEquity": "0", "snapshotTime": now_ms
            }]
        });
        assert!(parse_asset_overview(&incomplete, now_ms).is_err());

        let mismatched_total = page(json!(now_ms));
        let mut mismatched_total = mismatched_total;
        mismatched_total["totalEquity"] = json!("1");
        let (unallocated, _) = parse_asset_overview(&mismatched_total, now_ms).unwrap();
        assert!(unallocated
            .iter()
            .any(|row| row.product == "asset_overview_unallocated_equity"));

        let wrong_currency = json!({
            "totalEquity": "0",
            "list": [{
                "accountType": "FundingAccount",
                "totalEquity": "0",
                "valuationCurrency": "EUR",
                "snapshotTime": now_ms,
                "coinDetail": []
            }]
        });
        assert!(parse_asset_overview(&wrong_currency, now_ms).is_err());
    }

    #[test]
    fn asset_overview_accepts_documented_nonadditive_aggregates_but_still_blocks() {
        // The venue's official example rounds TradingBot categories to 120.52
        // while reporting 120.53 for the account, and its top-level total is
        // not the sum of the asynchronously stamped account rows. Neither
        // field is an additive-completeness contract.
        let now_ms = 1_700_000_000_000_i64;
        let result = json!({
            "totalEquity": "7457023",
            "list": [
                {
                    "accountType": "TradingBot",
                    "totalEquity": "120.53",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "categories": [
                        {"category": "Futures Grid Bot", "equity": "4", "coinDetail": [{"coin": "USDT", "equity": "4"}]},
                        {"category": "Futures Combo Bot", "equity": "101.53", "coinDetail": [{"coin": "USDT", "equity": "101.5392"}]},
                        {"category": "Futures Martingale Bot", "equity": "14.99", "coinDetail": [{"coin": "USDT", "equity": "14.9923"}]}
                    ]
                },
                {
                    "accountType": "Alpha",
                    "totalEquity": "335.33",
                    "valuationCurrency": "USD",
                    "snapshotTime": now_ms,
                    "categories": [
                        {"category": "trade", "equity": "296.16", "coinDetail": [{"coin": "TSLAx", "equity": "112.46434565"}]},
                        {"category": "farm", "equity": "39.16", "coinDetail": [{"coin": "SOL-USDC", "equity": "3.4268"}]}
                    ]
                }
            ]
        });

        let (positions, latent) = parse_asset_overview(&result, now_ms).unwrap();
        assert_eq!(latent.len(), 3);
        assert!(
            !positions.is_empty(),
            "documented product assets still block flatness"
        );
    }

    #[test]
    fn settlement_discovery_and_spread_orders_are_strict_and_unfiltered() {
        let instruments = json!({
            "category": "linear",
            "nextPageCursor": "more",
            "list": [
                {"symbol": "BTCUSDT", "settleCoin": "USDT"},
                {"symbol": "ETHUSDC", "settleCoin": "USDC"},
                {"symbol": "NEWUSD", "settleCoin": "USD"}
            ]
        });
        let (coins, cursor) = parse_linear_settle_coins(&instruments).unwrap();
        assert_eq!(coins, ["USDT", "USDC", "USD"]);
        assert_eq!(cursor, "more");

        let spreads = json!({
            "nextPageCursor": "",
            "list": [{"symbol": "BTCUSDT-ETHUSDT", "orderLinkId": ""}]
        });
        let (orders, _) = parse_spread_orders(&spreads).unwrap();
        assert_eq!(orders[0].product, "spread");
        assert!(orders[0].client_order_id.is_empty());
    }

    #[test]
    fn active_rfq_quotes_are_latent_order_inventory() {
        let result = json!({"list": [{
            "quoteId": "venue-quote-1",
            "quoteLinkId": "",
            "status": "Active",
            "quoteBuyList": [{"category": "linear", "symbol": "BTCUSDT"}],
            "quoteSellList": [{"category": "spot", "symbol": "BTCUSDT"}]
        }]});
        let rows = parse_rfq_quotes(&result, "quote").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].product, "rfq_quote:quote:Active");
        assert_eq!(rows[0].symbol, "linear:BTCUSDT,spot:BTCUSDT");
        assert_eq!(rows[0].client_order_id, "venue-quote-1");

        let malformed = json!({"list": [{
            "quoteId": "venue-quote-2",
            "quoteLinkId": "",
            "status": "Active",
            "quoteBuyList": []
        }]});
        assert!(parse_rfq_quotes(&malformed, "quote").is_err());
    }

    #[test]
    fn active_venue_strategies_are_latent_order_inventory() {
        let result = json!({
            "nextCursor": "more",
            "list": [{"strategyId": "running", "category": "UTA_USDT", "symbol": "BTCUSDT", "strategyType": "twap", "status": 2}]
        });
        let (rows, cursor) = parse_active_strategies(&result, 2).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].client_order_id, "running");
        assert_eq!(rows[0].product, "venue_strategy:twap:status-2");
        assert_eq!(cursor, "more");

        let wrong_status = json!({"nextCursor": "", "list": [{"status": 3}]});
        assert!(parse_active_strategies(&wrong_status, 2).is_err());
    }

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
    fn cancel_batch_keeps_each_item_rejection_in_request_order() {
        let result = json!({
            "list": [
                {"orderId": "venue-1", "orderLinkId": "eng-1"},
                {"orderId": "", "orderLinkId": "eng-2"}
            ]
        });
        let ext = json!({
            "list": [
                {"code": "0", "msg": "OK"},
                {"code": "110001", "msg": "Order does not exist"}
            ]
        });
        let replies =
            parse_cancel_batch(&result, &ext, &["eng-1".to_string(), "eng-2".to_string()]).unwrap();

        assert!(replies[0].is_ok());
        assert!(matches!(
            &replies[1],
            Err(VenueError::Rejected { code: 110001, message })
                if message == "Order does not exist"
        ));
    }

    #[test]
    fn cancel_batch_rejects_count_identity_and_outcome_structure_mismatches() {
        let ids = ["eng-1".to_string(), "eng-2".to_string()];
        let valid_result = json!({
            "list": [
                {"orderLinkId": "eng-1"},
                {"orderLinkId": "eng-2"}
            ]
        });
        let valid_ext = json!({
            "list": [
                {"code": 0, "msg": "OK"},
                {"code": 0, "msg": "OK"}
            ]
        });
        let cases = [
            (
                json!({"list": [{"orderLinkId": "eng-1"}]}),
                valid_ext.clone(),
            ),
            (
                valid_result.clone(),
                json!({"list": [{"code": 0, "msg": "OK"}]}),
            ),
            (
                json!({
                    "list": [
                        {"orderLinkId": "eng-2"},
                        {"orderLinkId": "eng-1"}
                    ]
                }),
                valid_ext.clone(),
            ),
            (
                valid_result.clone(),
                json!({
                    "list": [
                        {"code": 0, "msg": "OK"},
                        {"code": "not-an-integer", "msg": "bad"}
                    ]
                }),
            ),
        ];

        for (result, ext) in cases {
            assert!(matches!(
                parse_cancel_batch(&result, &ext, &ids),
                Err(VenueError::BadReply(_))
            ));
        }
    }

    #[test]
    fn an_explicit_flat_symbol_proves_one_way_mode_with_any_string_cursor() {
        for cursor in ["", "opaque-demo-cursor"] {
            let result = json!({
                "category": "linear",
                "list": [{
                    "symbol": "BTCUSDT", "positionIdx": 0, "side": "", "size": "0"
                }],
                "nextPageCursor": cursor
            });
            verify_one_way_position(&result, "BTCUSDT").unwrap();
        }
    }

    #[test]
    fn hedge_or_ambiguous_position_mode_fails_closed() {
        let page = |list: Value| json!({"category": "linear", "list": list, "nextPageCursor": ""});

        for result in [
            page(json!([])),
            json!({
                "category": "linear",
                "list": [
                    {"symbol": "BTCUSDT", "positionIdx": 1},
                    {"symbol": "BTCUSDT", "positionIdx": 2}
                ],
                "nextPageCursor": "opaque-hedge-cursor"
            }),
            page(json!([{"symbol": "BTCUSDT", "positionIdx": 3}])),
            page(json!([{"symbol": "ETHUSDT", "positionIdx": 0}])),
            page(json!([{"symbol": "BTCUSDT"}])),
            json!({
                "category": "linear",
                "list": [{"symbol": "BTCUSDT", "positionIdx": 0}]
            }),
            json!({
                "category": "linear",
                "list": [{"symbol": "BTCUSDT", "positionIdx": 0}],
                "nextPageCursor": 7
            }),
        ] {
            assert!(
                matches!(
                    verify_one_way_position(&result, "BTCUSDT"),
                    Err(VenueError::BadReply(_))
                ),
                "ambiguous or hedge-mode reply was trusted: {result}"
            );
        }
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
        assert!(matches!(
            parse_wallet(&result),
            Err(VenueError::BadReply(_))
        ));
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
    fn an_unconfigured_nonzero_position_invalidates_the_account_snapshot() {
        let result = json!({"list": [
            {"symbol": "VANRYUSDT", "side": "Buy", "size": "100", "avgPrice": "0.1",
             "stopLoss": ""},
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.5", "avgPrice": "95000.5",
             "stopLoss": "90000"}
        ]});
        let err = parse_positions(&result, &resolver()).unwrap_err();
        assert!(err.to_string().contains("VANRYUSDT"), "{err}");

        // Flat rows carry no exposure and need no configured id.
        let flat = json!({"list": [{"symbol": "VANRYUSDT", "size": "0"}]});
        assert!(parse_positions(&flat, &resolver()).unwrap().0.is_empty());
    }

    #[test]
    fn an_unattributable_working_order_invalidates_the_snapshot() {
        for row in [
            json!({"symbol": "BTCUSDT", "side": "Buy", "qty": "1", "cumExecQty": "0"}),
            json!({"symbol": "BTCUSDT", "orderLinkId": 7, "side": "Buy", "qty": "1",
                   "cumExecQty": "0"}),
            json!({"symbol": "BTCUSDT", "orderLinkId": "", "side": "Buy", "qty": "1",
                   "cumExecQty": "0", "stopOrderType": "UNKNOWN", "tpslMode": "",
                   "reduceOnly": false}),
        ] {
            assert!(matches!(
                parse_working_orders(&json!({"list": [row]})),
                Err(VenueError::BadReply(_))
            ));
        }
    }

    #[test]
    fn a_full_reduce_only_native_stop_may_have_no_order_link_id() {
        let page = json!({"list": [{
            "symbol": "BTCUSDT", "orderLinkId": "", "side": "Sell", "qty": "0.5",
            "cumExecQty": "0", "stopOrderType": "StopLoss", "tpslMode": "Full",
            "reduceOnly": true
        }]});
        let (orders, _) = parse_working_orders(&page).unwrap();
        assert_eq!(orders.len(), 1);
        assert!(orders[0].client_order_id.is_empty());
        assert!(orders[0].reduce_only);
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

    #[test]
    fn executions_read_the_history_page_and_skip_the_funding_rows() {
        let result = json!({
            "list": [
                {"execId": "e-1", "orderLinkId": "eng-9", "symbol": "ACEUSDT",
                 "side": "Sell", "execQty": "1526", "execPrice": "0.05413",
                 "execFee": "0.0454", "isMaker": false,
                 "execTime": "1787176627876", "execType": "Trade"},
                // The funding charge shows up in the same history and moves
                // no quantity; folding it in would corrupt the position sum.
                {"execId": "e-2", "orderLinkId": "", "symbol": "ACEUSDT",
                 "side": "Buy", "execQty": "1526", "execPrice": "0.05",
                 "execFee": "0.001", "isMaker": false,
                 "execTime": "1787176627877", "execType": "Funding"},
                // A venue stop firing carries no order id of ours and still
                // moves the position: kept.
                {"execId": "e-3", "orderLinkId": "", "symbol": "ACEUSDT",
                 "side": "Sell", "execQty": "10", "execPrice": "0.05",
                 "execFee": "0.0003", "isMaker": true,
                 "execTime": "1787176627878", "execType": "Trade"},
            ],
            "nextPageCursor": ""
        });
        let (rows, cursor) = parse_executions(&result).unwrap();
        assert!(cursor.is_empty());
        assert_eq!(rows.len(), 2, "the funding row is not a fill");
        assert_eq!(rows[0].exec_id, "e-1");
        assert_eq!(rows[0].client_order_id, "eng-9");
        assert_eq!(rows[0].side, Side::Sell);
        assert_eq!(rows[0].qty, 1526.0);
        assert_eq!(rows[0].px, 0.05413);
        assert_eq!(rows[0].venue_ts_ms, 1_787_176_627_876);
        assert!(!rows[0].is_maker);
        assert_eq!(rows[1].client_order_id, "");
        assert!(rows[1].is_maker);
    }

    #[test]
    fn recovery_refuses_a_fill_without_a_stable_identity_or_real_quantity() {
        let page = |exec_id: &str, qty: &str| {
            json!({
                "list": [{
                    "execId": exec_id, "orderLinkId": "eng-9", "symbol": "ACEUSDT",
                    "side": "Sell", "execQty": qty, "execPrice": "0.05413",
                    "execFee": "0", "execTime": "1787176627876", "execType": "Trade"
                }],
                "nextPageCursor": ""
            })
        };
        assert!(parse_executions(&page("", "1")).is_err());
        assert!(parse_executions(&page("e-zero", "0")).is_err());
    }
}
