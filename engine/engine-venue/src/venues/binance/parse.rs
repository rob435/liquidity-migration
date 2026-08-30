//! Binance replies to the engine's contract types. No sockets, no state.
//!
//! Three things here are venue-specific enough to say out loud.
//!
//! **A refusal is an HTTP 400, not a field in a 200.** Bybit and MEXC answer
//! every request with 200 and refuse inside their envelope; Binance puts the
//! refusal in the status code with a `{code, msg}` body. The shared HTTP
//! client reads every non-2xx as transport, so [`refine_rejection`] recovers
//! the venue's own refusal from that error — without it every rejected order
//! would look like a network fault, which retries, instead of a refusal,
//! which does not.
//!
//! **The stop is a separate algo order.** The venue's position object carries
//! no stop field; the position's protection is a `STOP_MARKET` algo order with
//! `closePosition=true`. "Is this position protected" is answered by joining
//! the algo-order book in, and that exact shape maps to an empty client order
//! id — the spelling the engine reserves for the stop attached to a position.
//!
//! **Account-wide trade recovery is not available.** `userTrades` requires a
//! symbol, while the order sources that could discover symbols forget orders
//! based on creation time. The gateway refuses that trait call. Tests still
//! parse the documented REST row beside the private-stream row so their
//! execution ids, fee units, and strict numeric checks stay identical.

use std::collections::HashMap;

use engine_types::ids::SymbolId;
#[cfg(test)]
use engine_types::orders::VenueExecution;
use engine_types::orders::{InstrumentRule, Side, VenueOrder};
use engine_types::risk::PositionView;
use engine_types::VenueError;
use serde_json::Value;

#[cfg(test)]
use crate::json::int_field;
use crate::json::{num_field, opt_num_field, str_field};

/// The client-order-id prefix this adapter's own position stops carry.
/// Anything wearing it was placed by [`super::gateway::BinanceGateway::set_stop`]
/// or beside an entry, and is bookkeeping for the position rather than an
/// order any strategy is waiting on.
pub(crate) const STOP_ID_PREFIX: &str = "engstop-";

/// Client ids the exchange itself writes on orders it creates: forced
/// liquidation, auto-deleverage, and settlement closes.
const EXCHANGE_ID_PREFIXES: &[&str] = &["autoclose-", "adl_autoclose", "settlement_autoclose-"];

/// Whether this client id names an order the exchange or this adapter's stop
/// path created, rather than one a strategy sent and is waiting on.
pub(crate) fn is_exchange_or_stop_id(client_order_id: &str) -> bool {
    client_order_id.starts_with(STOP_ID_PREFIX)
        || EXCHANGE_ID_PREFIXES
            .iter()
            .any(|prefix| client_order_id.starts_with(prefix))
}

/// Recover the venue's own refusal from the transport error the shared HTTP
/// client wraps a non-2xx reply in.
///
/// Only HTTP 400 becomes a rejection: 429 and 418 are the rate limiter and
/// must stay transport so nothing mistakes backoff pressure for a business
/// answer, and 5xx is the venue down. 401 is the key itself being refused.
pub(crate) fn refine_rejection(error: VenueError) -> VenueError {
    let VenueError::Transport(text) = &error else {
        return error;
    };
    let Some((code, message)) = embedded_code_msg(text) else {
        return error;
    };
    if text.starts_with("HTTP 400") {
        return VenueError::Rejected { code, message };
    }
    if text.starts_with("HTTP 401") {
        return VenueError::Credentials(message);
    }
    error
}

fn embedded_code_msg(text: &str) -> Option<(i64, String)> {
    let body = &text[text.find('{')?..];
    let parsed: Value = serde_json::from_str(body).ok()?;
    let code = parsed.get("code")?.as_i64()?;
    let message = parsed.get("msg")?.as_str()?.to_string();
    Some((code, message))
}

/// One accepted order: the client id the venue echoes and its own number.
pub(crate) fn parse_order_ack(data: &Value) -> Result<(String, String), VenueError> {
    let client_order_id = str_field(data, "clientOrderId")?;
    let venue_order_id = id_text(data, "orderId")
        .ok_or_else(|| VenueError::BadReply("an accepted order carried no orderId".to_string()))?;
    Ok((client_order_id, venue_order_id))
}

/// One accepted conditional order from `POST /fapi/v1/algoOrder`.
pub(crate) fn parse_algo_ack(data: &Value) -> Result<(String, String), VenueError> {
    let client_order_id = str_field(data, "clientAlgoId")?;
    let venue_order_id = id_text(data, "algoId").ok_or_else(|| {
        VenueError::BadReply("an accepted algo order carried no algoId".to_string())
    })?;
    Ok((client_order_id, venue_order_id))
}

/// The unique account alias repeated on every asset row returned by
/// `GET /fapi/v3/balance`.
pub(crate) fn parse_account_alias(data: &Value) -> Result<String, VenueError> {
    let rows = data
        .as_array()
        .ok_or_else(|| VenueError::BadReply("the balance reply was not a list".into()))?;
    let mut alias: Option<String> = None;
    for row in rows {
        let current = str_field(row, "accountAlias")?;
        let current = current.trim();
        if current.is_empty() {
            return Err(VenueError::BadReply(
                "a balance row carried an empty accountAlias".into(),
            ));
        }
        if alias.as_deref().is_some_and(|known| known != current) {
            return Err(VenueError::BadReply(
                "the balance rows disagree on accountAlias".into(),
            ));
        }
        alias.get_or_insert_with(|| current.to_string());
    }
    alias.ok_or_else(|| VenueError::BadReply("the balance reply carried no asset rows".into()))
}

/// Whether the signed account is using Binance's USD-valued multi-assets
/// accounting rather than the literal-USDT single-asset mode this engine
/// requires.
pub(crate) fn parse_multi_assets_mode(data: &Value) -> Result<bool, VenueError> {
    data.get("multiAssetsMargin")
        .and_then(Value::as_bool)
        .ok_or_else(|| {
            VenueError::BadReply(
                "the multi-assets-mode reply carried no boolean multiAssetsMargin".into(),
            )
        })
}

/// An id that may arrive as a JSON number or as a quoted string.
pub(crate) fn id_text(obj: &Value, name: &str) -> Option<String> {
    match obj.get(name)? {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// Binance trade numbers are read through symbol-scoped endpoints. Prefixing
/// the venue symbol keeps two symbols' equal native numbers distinct while
/// giving the REST sweep and private stream the same ledger key.
pub(crate) fn scoped_execution_id(symbol: &str, native_id: &str) -> String {
    format!("{}:{native_id}", symbol.to_ascii_uppercase())
}

fn side_of(name: &str) -> Option<Side> {
    match name {
        "BUY" => Some(Side::Buy),
        "SELL" => Some(Side::Sell),
        _ => None,
    }
}

#[derive(Copy, Clone, Debug, PartialEq)]
pub(crate) struct MarketQtyRule {
    pub min_qty: f64,
    pub max_qty: f64,
    pub qty_step: f64,
    pub opening_min_qty: f64,
    pub opening_qty_step: f64,
}

#[derive(Debug, PartialEq)]
pub(crate) struct ExchangeRules {
    pub instruments: Vec<(String, InstrumentRule)>,
    pub market_qty: HashMap<String, MarketQtyRule>,
}

fn whole_step_multiple(larger: f64, smaller: f64) -> bool {
    let count = larger / smaller;
    count.is_finite() && (count - count.round()).abs() <= 1e-9
}

fn common_qty_step(lot_step: f64, market_step: f64) -> Option<f64> {
    if whole_step_multiple(market_step, lot_step) {
        Some(market_step)
    } else if whole_step_multiple(lot_step, market_step) {
        Some(lot_step)
    } else {
        None
    }
}

fn ceil_to_step(value: f64, step: f64) -> f64 {
    let count = engine_types::quantize::steps(value, step);
    let whole = count.round();
    let ceiling = if (count - whole).abs() <= 1e-9 {
        whole
    } else {
        count.ceil()
    };
    engine_types::quantize::round_clean(ceiling * step, step)
}

/// Tick, step and minimums for every USDT-margined perpetual the venue is
/// trading, plus the separate size bounds Binance applies to market orders,
/// from `GET /fapi/v1/exchangeInfo`. Integrated delivery contracts now share
/// this endpoint, so status alone is not enough to identify the instruments
/// this adapter can trade.
pub(crate) fn parse_exchange_info(body: &Value) -> Result<ExchangeRules, VenueError> {
    let rows = body
        .get("symbols")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("exchangeInfo carried no symbols array".into()))?;
    let mut instruments = Vec::with_capacity(rows.len());
    let mut market_qty = HashMap::with_capacity(rows.len());
    for row in rows {
        if row.get("status").and_then(Value::as_str) != Some("TRADING")
            || row.get("contractType").and_then(Value::as_str) != Some("PERPETUAL")
            || row.get("quoteAsset").and_then(Value::as_str) != Some("USDT")
            || row.get("marginAsset").and_then(Value::as_str) != Some("USDT")
        {
            continue;
        }
        let Some(symbol) = row.get("symbol").and_then(Value::as_str) else {
            continue;
        };
        let Some(filters) = row.get("filters").and_then(Value::as_array) else {
            continue;
        };
        let filter = |kind: &str| {
            filters
                .iter()
                .find(|f| f.get("filterType").and_then(Value::as_str) == Some(kind))
        };
        let (Some(price), Some(lot), Some(market_lot), Some(notional)) = (
            filter("PRICE_FILTER"),
            filter("LOT_SIZE"),
            filter("MARKET_LOT_SIZE"),
            filter("MIN_NOTIONAL"),
        ) else {
            continue;
        };
        let (
            Ok(tick_size),
            Ok(qty_step),
            Ok(min_qty),
            Ok(lot_max),
            Ok(min_notional),
            Ok(market_step),
            Ok(market_min),
            Ok(market_max),
        ) = (
            num_field(price, "tickSize"),
            num_field(lot, "stepSize"),
            num_field(lot, "minQty"),
            num_field(lot, "maxQty"),
            num_field(notional, "notional"),
            num_field(market_lot, "stepSize"),
            num_field(market_lot, "minQty"),
            num_field(market_lot, "maxQty"),
        )
        else {
            continue;
        };
        if tick_size <= 0.0
            || qty_step <= 0.0
            || min_qty <= 0.0
            || lot_max < min_qty
            || min_notional <= 0.0
            || market_step <= 0.0
            || market_min <= 0.0
            || market_max < market_min
        {
            continue;
        }
        let Some(opening_qty_step) = common_qty_step(qty_step, market_step) else {
            continue;
        };
        let shared_max = lot_max.min(market_max);
        let opening_min_qty = ceil_to_step(min_qty.max(market_min), opening_qty_step);
        if opening_min_qty > shared_max {
            continue;
        }
        let symbol = symbol.to_string();
        // The submitted total uses a step and minimum valid under both
        // filters; the gateway enforces the lower maximum because
        // InstrumentRule has no maximum field. A partial limit fill can still
        // land below the market minimum, so this is not an exit guarantee.
        instruments.push((
            symbol.clone(),
            InstrumentRule {
                tick_size,
                qty_step: opening_qty_step,
                min_qty: opening_min_qty,
                min_notional,
            },
        ));
        market_qty.insert(
            symbol.to_string(),
            MarketQtyRule {
                min_qty: market_min,
                max_qty: shared_max,
                qty_step: market_step,
                opening_min_qty,
                opening_qty_step,
            },
        );
    }
    if instruments.is_empty() {
        return Err(VenueError::BadReply(
            "exchangeInfo listed no readable trading symbols".into(),
        ));
    }
    Ok(ExchangeRules {
        instruments,
        market_qty,
    })
}

/// The stop triggers standing on one symbol, kept by the position side each
/// order can actually close.
#[derive(Copy, Clone, Debug, Default, PartialEq)]
pub(crate) struct Stops {
    pub long: Option<f64>,
    pub short: Option<f64>,
}

impl Stops {
    pub fn nearest(&self, position_side: Side) -> Option<f64> {
        match position_side {
            Side::Buy => self.long,
            Side::Sell => self.short,
        }
    }
}

/// The live position stops, keyed by symbol, from the open algo orders.
///
/// Only the exact native shape counts: a mark-price `STOP_MARKET` whose
/// `closePosition` is true tracks the whole position however it grows. A
/// fixed-quantity or contract-price stop does not satisfy the protection the
/// gateway promises and remains visible to reconciliation by client id.
pub(crate) fn parse_position_stops(rows: &Value) -> HashMap<String, Stops> {
    let mut out: HashMap<String, Stops> = HashMap::new();
    let Some(rows) = rows.as_array() else {
        return out;
    };
    for row in rows {
        if !is_native_stop(row) {
            continue;
        }
        let Some(trigger) = row
            .get("triggerPrice")
            .and_then(text_or_number)
            .filter(|px| *px > 0.0)
        else {
            continue;
        };
        let Some(symbol) = row.get("symbol").and_then(Value::as_str) else {
            continue;
        };
        let Some(stop_side) = row.get("side").and_then(Value::as_str).and_then(side_of) else {
            continue;
        };
        let held = out.entry(symbol.to_string()).or_default();
        match stop_side {
            // A sell close-position order can only protect a long. The first
            // stop reached from above is the highest trigger.
            Side::Sell => held.long = Some(held.long.map_or(trigger, |px| px.max(trigger))),
            // A buy close-position order can only protect a short. The first
            // stop reached from below is the lowest trigger.
            Side::Buy => held.short = Some(held.short.map_or(trigger, |px| px.min(trigger))),
        }
    }
    out
}

/// The exact order shape this adapter writes a position stop as.
pub(crate) fn is_native_stop(row: &Value) -> bool {
    row.get("algoType").and_then(Value::as_str) == Some("CONDITIONAL")
        && row.get("orderType").and_then(Value::as_str) == Some("STOP_MARKET")
        && row.get("closePosition").and_then(Value::as_bool) == Some(true)
        && row.get("workingType").and_then(Value::as_str) == Some("MARK_PRICE")
}

fn is_stop_for_position(row: &Value, position_side: Side) -> bool {
    if !is_native_stop(row) {
        return false;
    }
    matches!(
        (position_side, row.get("side").and_then(Value::as_str)),
        (Side::Buy, Some("SELL")) | (Side::Sell, Some("BUY"))
    )
}

fn text_or_number(value: &Value) -> Option<f64> {
    match value {
        Value::String(s) => s.trim().parse().ok(),
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

/// Non-flat position sides from an account reply, keyed by venue symbol.
pub(crate) fn parse_position_sides(data: &Value) -> Result<HashMap<String, Side>, VenueError> {
    let rows = data
        .get("positions")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("the account reply carried no positions".into()))?;
    let mut out = HashMap::new();
    for row in rows {
        let amount = num_field(row, "positionAmt")?;
        if amount == 0.0 {
            continue;
        }
        let symbol = str_field(row, "symbol")?;
        let side = if amount > 0.0 { Side::Buy } else { Side::Sell };
        if out
            .insert(symbol.clone(), side)
            .is_some_and(|prior| prior != side)
        {
            return Err(VenueError::BadReply(format!(
                "the account reply carried both position sides for {symbol}"
            )));
        }
    }
    Ok(out)
}

/// Equity, spendable margin, and open positions from `GET /fapi/v2/account`,
/// with each position's stop joined in from the separate algo-order book.
pub(crate) fn parse_account(
    data: &Value,
    ids: &HashMap<String, SymbolId>,
    stops: &HashMap<String, Stops>,
) -> Result<(f64, f64, Vec<PositionView>), VenueError> {
    // Wallet plus unrealized: what the account is worth now, matching what
    // the other adapters report as equity. Identity rejects multi-assets
    // mode before this read, so these supported account totals are USDT.
    let equity = num_field(data, "totalMarginBalance")?;
    let available = num_field(data, "availableBalance")?;
    let rows = data
        .get("positions")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("the account reply carried no positions".into()))?;
    let mut out = Vec::new();
    for row in rows {
        // The venue lists every symbol it has ever touched; a flat row is
        // padding, not a position.
        let amount = num_field(row, "positionAmt")?;
        if amount == 0.0 {
            continue;
        }
        let symbol = str_field(row, "symbol")?;
        let &id = ids.get(&symbol).ok_or_else(|| {
            VenueError::BadReply(format!(
                "nonzero position in {symbol} is absent from the configured symbol table"
            ))
        })?;
        let side = if amount > 0.0 { Side::Buy } else { Side::Sell };
        let stop_px = stops
            .get(&symbol)
            .and_then(|held| held.nearest(side))
            .unwrap_or(0.0);
        out.push(PositionView {
            symbol: id,
            side,
            qty: amount.abs(),
            entry_px: opt_num_field(row, "entryPrice")?.unwrap_or(0.0),
            stop_attached: stop_px > 0.0,
            stop_px,
            leverage: opt_num_field(row, "leverage")?,
        });
    }
    Ok((equity, available, out))
}

/// Every ordinary order the venue is working, whoever placed it, from
/// `GET /fapi/v1/openOrders`.
///
/// The venue fills `clientOrderId` on every order — it invents one when the
/// sender did not — so a foreign id stays visible to reconciliation. The one
pub(crate) fn parse_working_orders(rows: &Value) -> Result<Vec<VenueOrder>, VenueError> {
    let rows = rows
        .as_array()
        .ok_or_else(|| VenueError::BadReply("open orders was not a list".into()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let symbol = str_field(row, "symbol")?;
        let client_order_id = str_field(row, "clientOrderId")?;
        if client_order_id.trim().is_empty() {
            return Err(VenueError::BadReply(format!(
                "a working order in {symbol} has no attributable clientOrderId"
            )));
        }
        let side_raw = str_field(row, "side")?;
        let side = side_of(&side_raw).ok_or_else(|| {
            VenueError::BadReply(format!("working order in {symbol} has side {side_raw:?}"))
        })?;
        let qty = num_field(row, "origQty")?;
        let filled = num_field(row, "executedQty")?;
        if qty <= 0.0 || filled < 0.0 || filled > qty {
            return Err(VenueError::BadReply(format!(
                "working order in {symbol} has invalid quantity {qty} or filled {filled}"
            )));
        }
        let reduce_only = row
            .get("reduceOnly")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        out.push(VenueOrder {
            client_order_id,
            symbol,
            side,
            qty,
            filled_qty: filled,
            reduce_only,
        });
    }
    Ok(out)
}

/// Every working conditional algo. Only a close-position stop is a shape the
/// engine can represent honestly; any other live algo remains visible with
/// its client id and stated quantity for reconciliation.
pub(crate) fn parse_working_algo_orders(
    rows: &Value,
    held_sides: &HashMap<String, Side>,
) -> Result<Vec<VenueOrder>, VenueError> {
    let rows = rows
        .as_array()
        .ok_or_else(|| VenueError::BadReply("open algo orders was not a list".into()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let symbol = str_field(row, "symbol")?;
        let side_raw = str_field(row, "side")?;
        let side = side_of(&side_raw).ok_or_else(|| {
            VenueError::BadReply(format!(
                "working algo order in {symbol} has side {side_raw:?}"
            ))
        })?;
        let close_position = row
            .get("closePosition")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let native_shape = is_native_stop(row);
        let position_stop = held_sides
            .get(&symbol)
            .is_some_and(|held| is_stop_for_position(row, *held));
        let client_order_id = if position_stop {
            String::new()
        } else {
            let id = str_field(row, "clientAlgoId")?;
            if id.trim().is_empty() {
                return Err(VenueError::BadReply(format!(
                    "a working algo order in {symbol} has no attributable clientAlgoId"
                )));
            }
            if native_shape && id.starts_with(STOP_ID_PREFIX) {
                return Err(VenueError::BadReply(format!(
                    "adapter-owned close-position stop {id} in {symbol} does not protect the \
                     position currently held; cancel this orphan Algo order before restart"
                )));
            }
            id
        };
        let qty = opt_num_field(row, "quantity")?.unwrap_or(0.0);
        if qty < 0.0 || (qty == 0.0 && !close_position) {
            return Err(VenueError::BadReply(format!(
                "working algo order in {symbol} has invalid quantity {qty}"
            )));
        }
        out.push(VenueOrder {
            client_order_id,
            symbol,
            side,
            qty,
            filled_qty: 0.0,
            reduce_only: row
                .get("reduceOnly")
                .and_then(Value::as_bool)
                .unwrap_or(false)
                || close_position,
        });
    }
    Ok(out)
}

/// One page of `GET /fapi/v1/userTrades`: each fill, and the venue order id
/// the caller still has to resolve into a client id — the row itself never
/// carries one.
#[cfg(test)]
pub(crate) fn parse_trades(rows: &Value) -> Result<Vec<(i64, VenueExecution)>, VenueError> {
    let rows = rows
        .as_array()
        .ok_or_else(|| VenueError::BadReply("userTrades was not a list".into()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let symbol = str_field(row, "symbol")?;
        let native_exec_id = id_text(row, "id")
            .filter(|id| !id.trim().is_empty())
            .ok_or_else(|| {
                VenueError::BadReply(format!("execution in {symbol} has no readable id"))
            })?;
        let exec_id = scoped_execution_id(&symbol, &native_exec_id);
        let order_id = int_field(row, "orderId")?;
        let side_raw = str_field(row, "side")?;
        let side = side_of(&side_raw).ok_or_else(|| {
            VenueError::BadReply(format!("execution {exec_id} has side {side_raw:?}"))
        })?;
        let qty = num_field(row, "qty")?;
        let px = num_field(row, "price")?;
        let mut fee = opt_num_field(row, "commission")?;
        if fee.is_some() {
            let fee_asset = str_field(row, "commissionAsset")?;
            if fee_asset != "USDT" {
                tracing::warn!(
                    execution = %exec_id,
                    asset = %fee_asset,
                    "a Binance fee is not USDT; preserving the fill with an unknown fee"
                );
                fee = None;
            }
        }
        let is_maker = row.get("maker").and_then(Value::as_bool).ok_or_else(|| {
            VenueError::BadReply(format!("execution {exec_id} has no boolean maker flag"))
        })?;
        let venue_ts_ms = int_field(row, "time")?;
        if qty <= 0.0 || px <= 0.0 || venue_ts_ms <= 0 {
            return Err(VenueError::BadReply(format!(
                "execution {exec_id} in {symbol} has non-positive quantity, price, or timestamp"
            )));
        }
        out.push((
            order_id,
            VenueExecution {
                exec_id,
                // Resolved by the caller through the order the venue names;
                // this row cannot say.
                client_order_id: String::new(),
                symbol,
                side,
                qty,
                px,
                fee,
                is_maker,
                venue_ts_ms,
            },
        ));
    }
    Ok(out)
}

/// The venue's algo numbers of every native position stop in an open-algo
/// reply, so a stop being replaced can be pulled — by number, because a
/// hand-placed stop carries a client id nobody here minted.
pub(crate) fn native_stop_algo_ids(
    rows: &Value,
    position_side: Side,
) -> Result<Vec<String>, VenueError> {
    let rows = rows
        .as_array()
        .ok_or_else(|| VenueError::BadReply("open algo orders was not a list".into()))?;
    let mut out = Vec::new();
    for row in rows {
        if is_stop_for_position(row, position_side) {
            out.push(id_text(row, "algoId").ok_or_else(|| {
                VenueError::BadReply("a native stop carried no readable algoId".into())
            })?);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ids() -> HashMap<String, SymbolId> {
        HashMap::from([
            ("BTCUSDT".to_string(), SymbolId(0)),
            ("ETHUSDT".to_string(), SymbolId(1)),
        ])
    }

    #[test]
    fn a_400_with_the_venues_code_and_msg_becomes_a_typed_rejection() {
        // The shape the shared HTTP client wraps a Binance refusal in,
        // recorded from the live venue: HTTP 400 and {"code","msg"}.
        let err = refine_rejection(VenueError::Transport(
            r#"HTTP 400: {"code":-1121,"msg":"Invalid symbol."}"#.to_string(),
        ));
        match err {
            VenueError::Rejected { code, message } => {
                assert_eq!(code, -1121);
                assert_eq!(message, "Invalid symbol.");
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_refused_key_becomes_a_credentials_error() {
        let err = refine_rejection(VenueError::Transport(
            r#"HTTP 401: {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}"#
                .to_string(),
        ));
        assert!(matches!(err, VenueError::Credentials(_)), "{err:?}");
    }

    #[test]
    fn the_rate_limiter_and_the_venue_being_down_stay_transport() {
        // 429 answered as a rejection would stop the caller backing off, and
        // a 5xx is not the venue answering anything.
        for text in [
            r#"HTTP 429: {"code":-1003,"msg":"Too many requests."}"#,
            r#"HTTP 503: {"code":-1001,"msg":"Internal error."}"#,
            "HTTP 400: not json at all",
        ] {
            let err = refine_rejection(VenueError::Transport(text.to_string()));
            assert!(matches!(err, VenueError::Transport(_)), "{text}");
        }
    }

    #[test]
    fn an_accepted_order_gives_up_both_its_ids() {
        // Recorded ack shape: the venue's order number is a bare integer.
        let data = json!({"clientOrderId": "eng-1", "orderId": 22542179, "status": "NEW",
                          "symbol": "BTCUSDT", "updateTime": 1566818724722i64});
        let (client, venue) = parse_order_ack(&data).unwrap();
        assert_eq!(client, "eng-1");
        assert_eq!(venue, "22542179");
        assert!(parse_order_ack(&json!({"status": "NEW"})).is_err());
    }

    #[test]
    fn an_accepted_algo_order_gives_up_both_its_ids() {
        let data = json!({
            "algoId": 2148713, "clientAlgoId": "engstop-1", "algoType": "CONDITIONAL",
            "algoStatus": "NEW", "symbol": "BTCUSDT", "orderType": "STOP_MARKET"
        });
        let (client, venue) = parse_algo_ack(&data).unwrap();
        assert_eq!(client, "engstop-1");
        assert_eq!(venue, "2148713");
        assert!(parse_algo_ack(&json!({"algoStatus": "NEW"})).is_err());
    }

    #[test]
    fn account_identity_uses_the_one_alias_repeated_on_every_asset_row() {
        let rows = json!([
            {"accountAlias":"SgsR","asset":"USDT","balance":"10"},
            {"accountAlias":"SgsR","asset":"USDC","balance":"0"}
        ]);
        assert_eq!(parse_account_alias(&rows).unwrap(), "SgsR");
    }

    #[test]
    fn account_identity_refuses_missing_empty_or_conflicting_aliases() {
        for rows in [
            json!([]),
            json!([{"asset":"USDT"}]),
            json!([{"accountAlias":"  ","asset":"USDT"}]),
            json!([
                {"accountAlias":"first","asset":"USDT"},
                {"accountAlias":"second","asset":"USDC"}
            ]),
        ] {
            assert!(matches!(
                parse_account_alias(&rows),
                Err(VenueError::BadReply(_))
            ));
        }
        assert!(parse_account_alias(&json!({"accountAlias":"one"})).is_err());
    }

    #[test]
    fn multi_assets_mode_requires_the_documented_boolean() {
        assert!(!parse_multi_assets_mode(&json!({"multiAssetsMargin": false})).unwrap());
        assert!(parse_multi_assets_mode(&json!({"multiAssetsMargin": true})).unwrap());
        for bad in [
            json!({}),
            json!({"multiAssetsMargin": "false"}),
            json!({"multiAssetsMargin": null}),
        ] {
            assert!(matches!(
                parse_multi_assets_mode(&bad),
                Err(VenueError::BadReply(_))
            ));
        }
    }

    #[test]
    fn instrument_and_market_order_rules_keep_their_distinct_size_filters() {
        // Recorded from the live exchangeInfo: tickSize under PRICE_FILTER,
        // ordinary step/minimum under LOT_SIZE, market step/minimum/maximum
        // under MARKET_LOT_SIZE, and MIN_NOTIONAL spells its minimum
        // "notional".
        let body = json!({"symbols": [{
            "symbol": "ARKUSDT", "status": "TRADING", "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT",
            "filters": [
                {"minPrice": "0.001", "tickSize": "0.001", "filterType": "PRICE_FILTER",
                 "maxPrice": "1000"},
                {"filterType": "LOT_SIZE", "maxQty": "900000", "minQty": "3",
                 "stepSize": "3"},
                {"filterType": "MARKET_LOT_SIZE", "maxQty": "30", "stepSize": "1",
                 "minQty": "1"},
                {"limit": 200, "filterType": "MAX_NUM_ORDERS"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                {"filterType": "PERCENT_PRICE", "multiplierDown": "0.9500",
                 "multiplierDecimal": "4", "multiplierUp": "1.0500"}
            ]
        }]});
        let rules = parse_exchange_info(&body).unwrap();
        assert_eq!(rules.instruments.len(), 1);
        let (symbol, rule) = &rules.instruments[0];
        assert_eq!(symbol, "ARKUSDT");
        assert_eq!(rule.tick_size, 0.001);
        assert_eq!(rule.qty_step, 3.0);
        assert_eq!(rule.min_qty, 3.0);
        assert_eq!(rule.min_notional, 5.0);
        assert_eq!(
            rules.market_qty["ARKUSDT"],
            MarketQtyRule {
                min_qty: 1.0,
                max_qty: 30.0,
                qty_step: 1.0,
                opening_min_qty: 3.0,
                opening_qty_step: 3.0,
            }
        );
    }

    #[test]
    fn the_general_quantity_rule_uses_a_grid_and_minimum_valid_under_both_filters() {
        let body = json!({"symbols": [{
            "symbol": "GRIDUSDT", "status": "TRADING", "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "minQty": "2", "maxQty": "1000",
                 "stepSize": "1"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "4", "maxQty": "30",
                 "stepSize": "3"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}
            ]
        }]});
        let rules = parse_exchange_info(&body).unwrap();
        let (_, general) = &rules.instruments[0];
        assert_eq!(general.qty_step, 3.0);
        assert_eq!(general.min_qty, 6.0);
        assert_eq!(rules.market_qty["GRIDUSDT"].min_qty, 4.0);
        assert_eq!(rules.market_qty["GRIDUSDT"].qty_step, 3.0);
        assert_eq!(rules.market_qty["GRIDUSDT"].opening_min_qty, 6.0);
        assert_eq!(rules.market_qty["GRIDUSDT"].opening_qty_step, 3.0);

        let incompatible = json!({"symbols": [{
            "symbol": "BADGRIDUSDT", "status": "TRADING", "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "minQty": "0.003", "maxQty": "1000",
                 "stepSize": "0.003"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.002", "maxQty": "30",
                 "stepSize": "0.002"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}
            ]
        }]});
        assert!(parse_exchange_info(&incompatible).is_err());
    }

    #[test]
    fn a_halted_symbol_or_one_missing_a_filter_gets_no_rule() {
        // A rule invented for either would let the engine quantize an order
        // the venue will refuse — or worse, will not.
        let body = json!({"symbols": [
            {"symbol": "GONEUSDT", "status": "SETTLING", "contractType": "PERPETUAL",
             "quoteAsset": "USDT", "marginAsset": "USDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1",
                 "maxQty": "1000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
            {"symbol": "BAREUSDT", "status": "TRADING", "contractType": "PERPETUAL",
             "quoteAsset": "USDT", "marginAsset": "USDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"}]},
            {"symbol": "OKUSDT", "status": "TRADING", "contractType": "PERPETUAL",
             "quoteAsset": "USDT", "marginAsset": "USDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1",
                 "maxQty": "1000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "1", "minQty": "1",
                 "maxQty": "100"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
        ]});
        let rules = parse_exchange_info(&body).unwrap();
        assert_eq!(rules.instruments.len(), 1);
        assert_eq!(rules.instruments[0].0, "OKUSDT");
        // And a reply with nothing readable is an error, not an empty table.
        assert!(parse_exchange_info(&json!({"symbols": []})).is_err());
    }

    #[test]
    fn only_the_close_position_stop_shape_counts_as_the_positions_stop() {
        let rows = json!([
            {"symbol": "BTCUSDT", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
             "closePosition": true, "triggerPrice": "75000", "side": "SELL",
             "workingType": "MARK_PRICE", "clientAlgoId": "engstop-1"},
            // A fixed-quantity stop guards a quantity, not the position.
            {"symbol": "ETHUSDT", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
             "closePosition": false, "triggerPrice": "2000", "side": "SELL",
             "clientAlgoId": "x-1"},
            // A take-profit is not protection.
            {"symbol": "SOLUSDT", "algoType": "CONDITIONAL",
             "orderType": "TAKE_PROFIT_MARKET", "closePosition": true,
             "triggerPrice": "300", "side": "SELL", "clientAlgoId": "x-2"},
            // A resting limit is not a stop.
            {"symbol": "XRPUSDT", "algoType": "CONDITIONAL", "orderType": "LIMIT",
             "closePosition": false, "triggerPrice": "0", "side": "BUY",
             "clientAlgoId": "x-3"}
        ]);
        let stops = parse_position_stops(&rows);
        assert_eq!(stops.len(), 1);
        assert_eq!(
            stops.get("BTCUSDT"),
            Some(&Stops {
                long: Some(75000.0),
                short: None,
            })
        );
    }

    #[test]
    fn two_stops_on_one_symbol_give_the_one_that_fires_first_for_the_side_held() {
        let rows = json!([
            {"symbol": "BTCUSDT", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
             "closePosition": true, "triggerPrice": "75000", "side": "SELL",
             "workingType": "MARK_PRICE"},
            {"symbol": "BTCUSDT", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
             "closePosition": true, "triggerPrice": "73000", "side": "SELL",
             "workingType": "MARK_PRICE"},
            {"symbol": "BTCUSDT", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
             "closePosition": true, "triggerPrice": "82000", "side": "BUY",
             "workingType": "MARK_PRICE"},
            {"symbol": "BTCUSDT", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
             "closePosition": true, "triggerPrice": "80000", "side": "BUY",
             "workingType": "MARK_PRICE"}
        ]);
        let stops = parse_position_stops(&rows);
        let held = stops.get("BTCUSDT").unwrap();
        assert_eq!(
            held.nearest(Side::Buy),
            Some(75000.0),
            "a long is hit from above"
        );
        assert_eq!(
            held.nearest(Side::Sell),
            Some(80000.0),
            "a short is hit from below"
        );
    }

    #[test]
    fn the_account_reading_is_equity_available_and_the_nonzero_positions() {
        // Documented v2 account shape: balances are account-level strings and
        // the positions list every symbol, flat ones included.
        let data = json!({
            "totalMarginBalance": "126.62311012",
            "availableBalance": "103.12345678",
            "totalWalletBalance": "126.72469206",
            "positions": [
                {"symbol": "BTCUSDT", "positionAmt": "0.004", "entryPrice": "78000.5",
                 "leverage": "6", "positionSide": "BOTH", "unrealizedProfit": "-0.10158194"},
                {"symbol": "ETHUSDT", "positionAmt": "-1.5", "entryPrice": "2800.0",
                 "leverage": "3", "positionSide": "BOTH", "unrealizedProfit": "0"},
                {"symbol": "XRPUSDT", "positionAmt": "0", "entryPrice": "0.0",
                 "leverage": "20", "positionSide": "BOTH", "unrealizedProfit": "0"}
            ]
        });
        let stops = HashMap::from([(
            "BTCUSDT".to_string(),
            Stops {
                long: Some(75000.0),
                short: None,
            },
        )]);
        let (equity, available, positions) = parse_account(&data, &ids(), &stops).unwrap();
        assert_eq!(equity, 126.62311012);
        assert_eq!(available, 103.12345678);
        assert_eq!(positions.len(), 2, "a flat row is padding, not a position");

        let long = &positions[0];
        assert_eq!(long.symbol, SymbolId(0));
        assert_eq!(long.side, Side::Buy);
        assert_eq!(long.qty, 0.004);
        assert_eq!(long.entry_px, 78000.5);
        assert_eq!(long.leverage, Some(6.0));
        assert!(long.stop_attached);
        assert_eq!(long.stop_px, 75000.0);

        let short = &positions[1];
        assert_eq!(short.side, Side::Sell);
        assert_eq!(short.qty, 1.5, "the sign carries the side, not the size");
        assert!(!short.stop_attached, "no stop was joined in");
    }

    #[test]
    fn an_unconfigured_nonzero_position_invalidates_the_snapshot() {
        let data = json!({
            "totalMarginBalance": "1", "availableBalance": "1",
            "positions": [{"symbol": "NOTLISTED", "positionAmt": "5"}]
        });
        let err = parse_account(&data, &ids(), &HashMap::new()).unwrap_err();
        assert!(err.to_string().contains("NOTLISTED"), "{err}");
    }

    #[test]
    fn a_working_order_carries_its_client_id_and_whether_it_closes() {
        let rows = json!([{
            "clientOrderId": "eng-7", "orderId": 1917641, "symbol": "BTCUSDT",
            "side": "SELL", "type": "LIMIT", "origQty": "0.010", "executedQty": "0.003",
            "price": "79000.1", "reduceOnly": true, "closePosition": false,
            "status": "PARTIALLY_FILLED", "stopPrice": "0", "timeInForce": "GTC"
        }]);
        let out = parse_working_orders(&rows).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].client_order_id, "eng-7");
        assert_eq!(out[0].symbol, "BTCUSDT");
        assert_eq!(out[0].side, Side::Sell);
        assert_eq!(out[0].qty, 0.010);
        assert_eq!(out[0].filled_qty, 0.003);
        assert!(out[0].reduce_only);
    }

    #[test]
    fn the_native_stop_reports_an_empty_client_id_like_a_position_stop() {
        // The engine reserves the empty client id for the stop attached to a
        // position; this venue keeps that stop as an order, so the exact
        // shape maps to it — including its venue-written zero quantity, which
        // means "sized by the position".
        let rows = json!([{
            "clientAlgoId": "engstop-btcusdt-1788092000000", "algoId": 1917642,
            "algoType": "CONDITIONAL", "symbol": "BTCUSDT", "side": "SELL",
            "orderType": "STOP_MARKET", "quantity": "0", "reduceOnly": false,
            "closePosition": true, "algoStatus": "NEW", "triggerPrice": "75000",
            "workingType": "MARK_PRICE"
        }]);
        let held = HashMap::from([("BTCUSDT".to_string(), Side::Buy)]);
        let out = parse_working_algo_orders(&rows, &held).unwrap();
        assert_eq!(out[0].client_order_id, "");
        assert_eq!(out[0].qty, 0.0);
        assert!(out[0].reduce_only);
    }

    #[test]
    fn a_wrong_side_close_position_stop_stays_attributable() {
        let rows = json!([{
            "clientAlgoId": "manual-short-stop", "algoId": 1917643,
            "algoType": "CONDITIONAL", "symbol": "BTCUSDT", "side": "BUY",
            "orderType": "STOP_MARKET", "quantity": "0", "reduceOnly": false,
            "closePosition": true, "algoStatus": "NEW", "triggerPrice": "80000",
            "workingType": "MARK_PRICE"
        }]);
        let held = HashMap::from([("BTCUSDT".to_string(), Side::Buy)]);
        let out = parse_working_algo_orders(&rows, &held).unwrap();
        assert_eq!(out[0].client_order_id, "manual-short-stop");
        assert_eq!(out[0].side, Side::Buy);
        assert!(out[0].reduce_only);
    }

    #[test]
    fn a_flat_accounts_orphan_stop_fails_reconciliation_by_name() {
        let rows = json!([{
            "clientAlgoId": "engstop-deadbeef", "algoId": 1917644,
            "algoType": "CONDITIONAL", "symbol": "BTCUSDT", "side": "SELL",
            "orderType": "STOP_MARKET", "timeInForce": "GTC", "quantity": "0",
            "reduceOnly": false, "closePosition": true, "algoStatus": "NEW",
            "triggerPrice": "75000", "workingType": "MARK_PRICE"
        }]);
        let error = parse_working_algo_orders(&rows, &HashMap::new()).unwrap_err();
        assert!(error.to_string().contains("engstop-deadbeef"), "{error}");
        assert!(error.to_string().contains("orphan Algo order"), "{error}");
    }

    #[test]
    fn a_contract_price_close_all_stop_is_visible_and_never_counts_as_protection() {
        let rows = json!([{
            "clientAlgoId": "engstop-contract-price", "algoId": 1917645,
            "algoType": "CONDITIONAL", "symbol": "BTCUSDT", "side": "SELL",
            "orderType": "STOP_MARKET", "quantity": "0", "reduceOnly": false,
            "closePosition": true, "algoStatus": "NEW", "triggerPrice": "75000",
            "workingType": "CONTRACT_PRICE"
        }]);
        assert!(parse_position_stops(&rows).is_empty());
        let held = HashMap::from([("BTCUSDT".to_string(), Side::Buy)]);
        let working = parse_working_algo_orders(&rows, &held).unwrap();
        assert_eq!(working[0].client_order_id, "engstop-contract-price");
        assert!(working[0].reduce_only);
    }

    #[test]
    fn position_sides_refuse_two_nonflat_sides_for_one_symbol() {
        let reply = json!({"positions":[
            {"symbol":"BTCUSDT","positionAmt":"0.1"},
            {"symbol":"ETHUSDT","positionAmt":"-1"},
            {"symbol":"XRPUSDT","positionAmt":"0"}
        ]});
        assert_eq!(
            parse_position_sides(&reply).unwrap(),
            HashMap::from([
                ("BTCUSDT".to_string(), Side::Buy),
                ("ETHUSDT".to_string(), Side::Sell)
            ])
        );
        let ambiguous = json!({"positions":[
            {"symbol":"BTCUSDT","positionAmt":"0.1"},
            {"symbol":"BTCUSDT","positionAmt":"-0.1"}
        ]});
        assert!(parse_position_sides(&ambiguous).is_err());
    }

    #[test]
    fn an_unattributable_working_order_invalidates_the_snapshot() {
        for client in [json!(""), json!(null)] {
            let rows = json!([{
                "clientOrderId": client, "symbol": "BTCUSDT", "side": "BUY",
                "type": "LIMIT", "origQty": "1", "executedQty": "0",
                "closePosition": false
            }]);
            assert!(matches!(
                parse_working_orders(&rows),
                Err(VenueError::BadReply(_))
            ));
        }
    }

    #[test]
    fn a_zero_quantity_is_only_readable_on_the_native_stop_shape() {
        let rows = json!([{
            "clientOrderId": "eng-9", "symbol": "BTCUSDT", "side": "BUY",
            "type": "LIMIT", "origQty": "0", "executedQty": "0", "closePosition": false
        }]);
        assert!(parse_working_orders(&rows).is_err());
    }

    #[test]
    fn a_fill_carries_everything_but_the_client_id_which_the_row_cannot_say() {
        // The documented userTrades row: commission signed as charged, the
        // maker flag stated, and no client order id anywhere on it.
        let rows = json!([{
            "buyer": false, "commission": "0.07819010", "commissionAsset": "USDT",
            "id": 698759, "maker": false, "orderId": 25851813, "price": "7819.01",
            "qty": "0.002", "quoteQty": "15.63802", "realizedPnl": "-0.91539999",
            "side": "SELL", "positionSide": "BOTH", "symbol": "BTCUSDT",
            "time": 1569514978020i64
        }]);
        let out = parse_trades(&rows).unwrap();
        assert_eq!(out.len(), 1);
        let (order_id, fill) = &out[0];
        assert_eq!(*order_id, 25851813);
        assert_eq!(fill.exec_id, "BTCUSDT:698759");
        assert_eq!(fill.client_order_id, "");
        assert_eq!(fill.side, Side::Sell);
        assert_eq!(fill.qty, 0.002);
        assert_eq!(fill.px, 7819.01);
        assert_eq!(fill.fee, Some(0.07819010));
        assert!(!fill.is_maker);
        assert_eq!(fill.venue_ts_ms, 1569514978020);
    }

    #[test]
    fn one_malformed_trade_invalidates_the_whole_page() {
        let valid = json!({
            "id": 1, "orderId": 2, "symbol": "BTCUSDT", "side": "BUY", "price": "1.0",
            "qty": "1", "commission": "0.01", "commissionAsset": "USDT",
            "maker": true, "time": 1
        });
        let mut bad_rows = Vec::new();
        for field in [
            "id", "orderId", "symbol", "side", "price", "qty", "maker", "time",
        ] {
            let mut row = valid.clone();
            row.as_object_mut().unwrap().remove(field);
            bad_rows.push(row);
        }
        for (field, value) in [
            ("side", json!("LONG")),
            ("qty", json!("0")),
            ("price", json!("0")),
            ("time", json!(0)),
            ("maker", json!("yes")),
        ] {
            let mut row = valid.clone();
            row[field] = value;
            bad_rows.push(row);
        }
        for bad in bad_rows {
            assert!(
                parse_trades(&json!([valid.clone(), bad])).is_err(),
                "a malformed row was silently skipped"
            );
        }
    }

    #[test]
    fn a_missing_commission_stays_unknown_and_an_explicit_zero_stays_zero() {
        let base = json!({
            "id": 1, "orderId": 2, "symbol": "BTCUSDT", "side": "BUY", "price": "1.0",
            "qty": "1", "maker": true, "time": 1
        });
        let (_, without) = &parse_trades(&json!([base.clone()])).unwrap()[0];
        assert_eq!(without.fee, None);
        let mut zero = base;
        zero["commission"] = json!("0");
        zero["commissionAsset"] = json!("USDT");
        let (_, with_zero) = &parse_trades(&json!([zero])).unwrap()[0];
        assert_eq!(with_zero.fee, Some(0.0));
    }

    #[test]
    fn trade_ids_are_symbol_scoped_and_non_usdt_fees_stay_unknown() {
        let trade = |symbol: &str, asset: &str| {
            json!({
                "id": 7, "orderId": 2, "symbol": symbol, "side": "BUY", "price": "1",
                "qty": "1", "commission": "0.01", "commissionAsset": asset,
                "maker": true, "time": 1
            })
        };
        let parsed =
            parse_trades(&json!([trade("BTCUSDT", "USDT"), trade("ETHUSDT", "USDT")])).unwrap();
        assert_eq!(parsed[0].1.exec_id, "BTCUSDT:7");
        assert_eq!(parsed[1].1.exec_id, "ETHUSDT:7");
        let bnb = parse_trades(&json!([trade("BTCUSDT", "BNB")])).unwrap();
        assert_eq!(bnb[0].1.fee, None);
    }

    #[test]
    fn exchange_written_ids_and_this_adapters_stop_ids_are_not_strategy_orders() {
        for theirs in [
            "engstop-btcusdt-1788092000000",
            "autoclose-1596107620040000001",
            "adl_autoclose",
            "settlement_autoclose-1596107620040000001",
        ] {
            assert!(is_exchange_or_stop_id(theirs), "{theirs}");
        }
        for ours in ["eng-1788092000000-1", "x-grid-77", ""] {
            assert!(!is_exchange_or_stop_id(ours), "{ours:?}");
        }
    }
}
