//! The demo gateway: the [`VenueGateway`] contract over Bybit v5.

use std::collections::HashMap;

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AmendSpec, InstrumentRule, OrderAck, OrderKind, OrderRequest, Side, TimeInForce,
};
use engine_types::risk::AccountView;
use engine_types::{VenueCaps, VenueError, VenueGateway};
use serde_json::{Map, Value};

use crate::clock::mono_ns;
use crate::creds::Credentials;
use crate::fmt::venue_num;
use crate::parse::{parse_instruments, parse_order_ack, parse_positions, parse_wallet, venue_result};
use crate::rest::RestClient;
use crate::{CATEGORY, DEMO_REST_BASE};

const PATH_TIME: &str = "/v5/market/time";
const PATH_ORDER_CREATE: &str = "/v5/order/create";
const PATH_ORDER_CANCEL: &str = "/v5/order/cancel";
const PATH_ORDER_AMEND: &str = "/v5/order/amend";
const PATH_TRADING_STOP: &str = "/v5/position/trading-stop";
const PATH_WALLET: &str = "/v5/account/wallet-balance";
const PATH_POSITIONS: &str = "/v5/position/list";
const PATH_INSTRUMENTS: &str = "/v5/market/instruments-info";

/// Enough to cover every linear contract several times over; a cursor that
/// never empties is a venue fault, not a reason to loop forever.
const MAX_PAGES: usize = 20;

pub struct BybitGateway {
    rest: RestClient,
    names: Vec<Symbol>,
    ids: HashMap<Symbol, SymbolId>,
}

impl BybitGateway {
    /// The live gateway: demo host, credentials from the environment. There
    /// is no argument for the host on purpose.
    ///
    /// `symbols` is the engine's symbol table in `SymbolId` order — position
    /// `i` is the name of `SymbolId(i)`.
    pub fn new(symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        Ok(Self::build(DEMO_REST_BASE, Credentials::from_env()?, symbols))
    }

    /// Point the gateway at a local server. Tests and the mock-venue
    /// benchmark only; the live path is [`BybitGateway::new`].
    pub fn for_test(base_url: &str, creds: Credentials, symbols: Vec<Symbol>) -> Self {
        Self::build(base_url, creds, symbols)
    }

    fn build(base_url: &str, creds: Credentials, symbols: Vec<Symbol>) -> Self {
        let ids = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
            .collect();
        Self {
            rest: RestClient::new(base_url, creds),
            names: symbols,
            ids,
        }
    }

    /// Open the TLS session before an order needs it.
    pub async fn warm(&mut self) -> Result<(), VenueError> {
        let envelope = self.rest.get_public(PATH_TIME, "").await?;
        venue_result(envelope)?;
        Ok(())
    }

    /// Add a symbol the engine has since interned. Ids stay in step with the
    /// engine's own table only if it appends in the same order.
    pub fn add_symbol(&mut self, name: &str) -> SymbolId {
        if let Some(id) = self.ids.get(name) {
            return *id;
        }
        let id = SymbolId(u16::try_from(self.names.len()).expect("more than 65535 symbols"));
        self.names.push(name.to_string());
        self.ids.insert(name.to_string(), id);
        id
    }

    pub fn symbols(&self) -> &[Symbol] {
        &self.names
    }

    fn name_of(&self, id: SymbolId) -> Result<&str, VenueError> {
        self.names.get(id.0 as usize).map(String::as_str).ok_or_else(|| {
            VenueError::BadRequest(format!(
                "symbol id {} is not in the gateway's table",
                id.0
            ))
        })
    }
}

impl VenueGateway for BybitGateway {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            // Bybit v5 linear perps: the position carries its own stop-loss
            // (POST /v5/position/trading-stop, and stopLoss on the entry).
            native_position_stop: true,
            // POST /v5/order/amend, by orderLinkId — see amend_order below.
            amend_in_place: true,
            // timeInForce "PostOnly" on a limit order; see tif_str below.
            post_only: true,
            // Bybit does have a batch endpoint, but this crate has no path to
            // it: every order goes out one request at a time. Declared false
            // because a caller that batched on this word would find nothing
            // to call.
            batch_orders: false,
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(req.symbol)?.into());
        body.insert("side".into(), side_str(req.side).into());
        body.insert("qty".into(), venue_num(req.qty)?.into());
        body.insert("reduceOnly".into(), req.reduce_only.into());
        body.insert("orderLinkId".into(), req.client_order_id.as_str().into());
        match req.kind {
            // Market orders take no timeInForce: the venue runs them IOC.
            OrderKind::Market => {
                body.insert("orderType".into(), "Market".into());
            }
            OrderKind::Limit { px, tif } => {
                body.insert("orderType".into(), "Limit".into());
                body.insert("price".into(), venue_num(px)?.into());
                body.insert("timeInForce".into(), tif_str(tif).into());
            }
        }
        // The stop rides along with the entry, so one round trip leaves the
        // position protected rather than two. Never on an exit: Bybit
        // rejects a reduce-only order that carries stop-loss fields, and a
        // rejected exit is the one rejection that hurts.
        if let Some(stop) = req.stop {
            if !req.reduce_only {
                body.insert("tpslMode".into(), "Full".into());
                body.insert("stopLoss".into(), venue_num(stop.trigger_px)?.into());
            }
        }

        let envelope = self.rest.post_signed(PATH_ORDER_CREATE, &Value::Object(body)).await?;
        let ack_ns = mono_ns();
        let result = venue_result(envelope)?;
        parse_order_ack(&result, &req.client_order_id, ack_ns)
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(symbol)?.into());
        body.insert("orderLinkId".into(), client_order_id.into());

        let envelope = self.rest.post_signed(PATH_ORDER_CANCEL, &Value::Object(body)).await?;
        venue_result(envelope)?;
        Ok(())
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        if spec.px.is_none() && spec.qty.is_none() {
            return Err(VenueError::BadRequest(
                "an amend that changes neither price nor size".to_string(),
            ));
        }
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(symbol)?.into());
        body.insert("orderLinkId".into(), client_order_id.into());
        // Only what is changing. Bybit reads an absent field as "leave it",
        // and a price echoed back unchanged still costs the order its place
        // in the queue.
        if let Some(px) = spec.px {
            body.insert("price".into(), venue_num(px)?.into());
        }
        if let Some(qty) = spec.qty {
            body.insert("qty".into(), venue_num(qty)?.into());
        }

        let envelope = self.rest.post_signed(PATH_ORDER_AMEND, &Value::Object(body)).await?;
        venue_result(envelope)?;
        Ok(())
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(symbol)?.into());
        body.insert("stopLoss".into(), venue_num(trigger_px)?.into());
        // Full: the stop closes the whole position. positionIdx 0 is one-way
        // mode, which is how the demo account is configured.
        body.insert("tpslMode".into(), "Full".into());
        body.insert("positionIdx".into(), 0.into());

        let envelope = self.rest.post_signed(PATH_TRADING_STOP, &Value::Object(body)).await?;
        venue_result(envelope)?;
        Ok(())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        // Wallet and positions are separate endpoints, so this is two round
        // trips however it is written — they at least go out together.
        let wallet = self.rest.get_signed(PATH_WALLET, "accountType=UNIFIED");
        let positions = self
            .rest
            .get_signed(PATH_POSITIONS, "category=linear&settleCoin=USDT&limit=200");
        let (wallet, positions) = futures_util::future::try_join(wallet, positions).await?;
        let observed_ns = mono_ns();

        let (equity_usdt, available_usdt) = parse_wallet(&venue_result(wallet)?)?;

        let ids = &self.ids;
        let resolve = |name: &str| ids.get(name).copied();
        let (mut open, mut cursor) = parse_positions(&venue_result(positions)?, &resolve)?;
        let mut pages = 1;
        while !cursor.is_empty() && pages < MAX_PAGES {
            let query = format!(
                "category=linear&settleCoin=USDT&limit=200&cursor={}",
                percent_encode(&cursor)
            );
            let more = self.rest.get_signed(PATH_POSITIONS, &query).await?;
            let (rows, next) = parse_positions(&venue_result(more)?, &resolve)?;
            open.extend(rows);
            cursor = next;
            pages += 1;
        }
        // A truncated position list under-counts exposure and can hide an
        // unprotected position — this read fails closed, like instruments.
        if !cursor.is_empty() {
            return Err(VenueError::BadReply(format!(
                "position listing still had pages after {MAX_PAGES}"
            )));
        }

        Ok(AccountView {
            equity_usdt,
            available_usdt,
            positions: open,
            observed_ns,
        })
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        let mut out = Vec::new();
        let mut cursor = String::new();
        for _ in 0..MAX_PAGES {
            let query = if cursor.is_empty() {
                format!("category={CATEGORY}&limit=1000")
            } else {
                format!("category={CATEGORY}&limit=1000&cursor={}", percent_encode(&cursor))
            };
            // Public listing: unsigned.
            let envelope = self.rest.get_public(PATH_INSTRUMENTS, &query).await?;
            let (rows, next) = parse_instruments(&venue_result(envelope)?)?;
            out.extend(rows);
            if next.is_empty() {
                return Ok(out);
            }
            cursor = next;
        }
        Err(VenueError::BadReply(format!(
            "instrument listing still had pages after {MAX_PAGES}"
        )))
    }
}

fn side_str(side: Side) -> &'static str {
    match side {
        Side::Buy => "Buy",
        Side::Sell => "Sell",
    }
}

fn tif_str(tif: TimeInForce) -> &'static str {
    match tif {
        TimeInForce::Gtc => "GTC",
        TimeInForce::Ioc => "IOC",
        TimeInForce::PostOnly => "PostOnly",
    }
}

/// Cursors come back with characters that mean something in a query string,
/// and the signature covers the query exactly as sent.
fn percent_encode(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for byte in raw.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cursors_are_escaped_for_the_query_string() {
        assert_eq!(percent_encode("next%3D"), "next%253D");
        assert_eq!(percent_encode("a b&c=d"), "a%20b%26c%3Dd");
        assert_eq!(percent_encode("plain-Cursor_1.0~"), "plain-Cursor_1.0~");
    }

    #[test]
    fn sides_and_time_in_force_use_the_venue_spelling() {
        assert_eq!(side_str(Side::Buy), "Buy");
        assert_eq!(side_str(Side::Sell), "Sell");
        assert_eq!(tif_str(TimeInForce::Gtc), "GTC");
        assert_eq!(tif_str(TimeInForce::Ioc), "IOC");
        assert_eq!(tif_str(TimeInForce::PostOnly), "PostOnly");
    }

    #[test]
    fn an_unknown_symbol_id_cannot_become_a_request() {
        let gw = BybitGateway::for_test(
            "http://127.0.0.1:1",
            Credentials::new("k", "s"),
            vec!["BTCUSDT".to_string()],
        );
        assert!(gw.name_of(SymbolId(0)).is_ok());
        assert!(gw.name_of(SymbolId(7)).is_err());
    }

    #[test]
    fn added_symbols_keep_their_position_as_the_id() {
        let mut gw = BybitGateway::for_test(
            "http://127.0.0.1:1",
            Credentials::new("k", "s"),
            vec!["BTCUSDT".to_string()],
        );
        assert_eq!(gw.add_symbol("ETHUSDT"), SymbolId(1));
        assert_eq!(gw.add_symbol("ETHUSDT"), SymbolId(1));
        assert_eq!(gw.name_of(SymbolId(1)).unwrap(), "ETHUSDT");
    }
}
