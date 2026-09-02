use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const SCHEMA_VERSION: u32 = 2;

pub const KIND_BOOK_SNAPSHOT: &str = "orderbook_snapshot";
pub const KIND_BOOK_DELTA: &str = "orderbook_delta";
pub const KIND_TRADE: &str = "public_trade";
pub const KIND_TICKER: &str = "ticker";
pub const KIND_LIQUIDATION: &str = "liquidation";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "kind")]
pub enum TapeRecord {
    #[serde(rename = "orderbook_snapshot")]
    BookSnapshot(BookRow),
    #[serde(rename = "orderbook_delta")]
    BookDelta(BookRow),
    #[serde(rename = "public_trade")]
    Trade(TradeRow),
    #[serde(rename = "ticker")]
    Ticker(TickerRow),
    #[serde(rename = "liquidation")]
    Liquidation(LiquidationRow),
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct BookRow {
    pub venue: String,
    pub symbol: String,
    pub depth: u32,
    pub local_receive_ts_ns: i64,
    pub exchange_system_ts_ns: i64,
    pub exchange_engine_ts_ns: i64,
    pub bids: Vec<[String; 2]>,
    pub asks: Vec<[String; 2]>,
    pub update_id: i64,
    pub previous_update_id: i64,
    pub first_update_id: i64,
    pub cross_sequence: i64,
    pub previous_cross_sequence: i64,
    pub restart_snapshot: bool,
    pub sequence_gap: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct TradeRow {
    pub venue: String,
    pub symbol: String,
    pub local_receive_ts_ns: i64,
    pub exchange_ts_ns: i64,
    pub trade_id: String,
    pub price: f64,
    pub qty: f64,
    pub side: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct TickerRow {
    pub venue: String,
    pub symbol: String,
    pub local_receive_ts_ns: i64,
    pub exchange_system_ts_ns: i64,
    pub message_type: String,
    pub cross_sequence: i64,
    pub values: BTreeMap<String, f64>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct LiquidationRow {
    pub venue: String,
    pub symbol: String,
    pub local_receive_ts_ns: i64,
    pub exchange_system_ts_ns: i64,
    pub exchange_ts_ns: i64,
    pub position_side: String,
    pub qty: f64,
    pub bankruptcy_price: f64,
}

pub fn book_row_from_event(
    venue: &str,
    symbol: &str,
    depth: engine_types::Depth,
    now_ns: i64,
) -> BookRow {
    let mut bids = Vec::with_capacity(depth.bid_len as usize);
    for i in 0..depth.bid_len as usize {
        bids.push([depth.bids[i].px.to_string(), depth.bids[i].qty.to_string()]);
    }
    let mut asks = Vec::with_capacity(depth.ask_len as usize);
    for i in 0..depth.ask_len as usize {
        asks.push([depth.asks[i].px.to_string(), depth.asks[i].qty.to_string()]);
    }
    BookRow {
        venue: venue.to_string(),
        symbol: symbol.to_string(),
        depth: depth.bid_len.max(depth.ask_len) as u32,
        local_receive_ts_ns: now_ns,
        exchange_system_ts_ns: depth.venue_ts_ms * 1_000_000,
        exchange_engine_ts_ns: depth.venue_ts_ms * 1_000_000,
        bids,
        asks,
        update_id: depth.update_id as i64,
        previous_update_id: 0,
        first_update_id: 0,
        cross_sequence: depth.seq as i64,
        previous_cross_sequence: 0,
        restart_snapshot: false,
        sequence_gap: false,
    }
}

pub fn trade_row_from_event(
    venue: &str,
    symbol: &str,
    trades: engine_types::TradeFlow,
    now_ns: i64,
) -> TradeRow {
    let side = if trades.buy_qty >= trades.sell_qty {
        "Buy".to_string()
    } else {
        "Sell".to_string()
    };
    let qty = trades.buy_qty + trades.sell_qty;
    TradeRow {
        venue: venue.to_string(),
        symbol: symbol.to_string(),
        local_receive_ts_ns: now_ns,
        exchange_ts_ns: trades.venue_ts_ms * 1_000_000,
        trade_id: format!("{}-{}", trades.seq, trades.venue_ts_ms),
        price: trades.last_px,
        qty,
        side,
    }
}

pub fn ticker_row_from_event(
    venue: &str,
    symbol: &str,
    ticker: engine_types::Ticker,
    now_ns: i64,
) -> TickerRow {
    let mut values = BTreeMap::new();
    values.insert("last_price".to_string(), ticker.last_px);
    values.insert("mark_price".to_string(), ticker.mark_px);
    values.insert("index_price".to_string(), ticker.index_px);
    values.insert("funding_rate".to_string(), ticker.funding_rate);
    values.insert(
        "next_funding_time_ms".to_string(),
        ticker.next_funding_ms as f64,
    );
    TickerRow {
        venue: venue.to_string(),
        symbol: symbol.to_string(),
        local_receive_ts_ns: now_ns,
        exchange_system_ts_ns: ticker.venue_ts_ms * 1_000_000,
        message_type: "snapshot".to_string(),
        cross_sequence: 0,
        values,
    }
}
