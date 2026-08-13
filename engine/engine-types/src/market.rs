use serde::{Deserialize, Serialize};

use crate::ids::{SymbolId, SymbolTable};

/// Which feed a strategy wants for a symbol.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Feed {
    /// Best bid/ask (venue orderbook depth-1 stream).
    Quote,
    /// Ticker: last, mark, index, funding.
    Ticker,
}

/// A strategy's request for market data, collected at boot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Subscription {
    pub symbol: String,
    pub feed: Feed,
}

/// Best bid/ask for one symbol. Copy, fixed-size, no heap.
#[derive(Copy, Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Quote {
    pub bid_px: f64,
    pub bid_qty: f64,
    pub ask_px: f64,
    pub ask_qty: f64,
    /// Venue event time, wall-clock milliseconds.
    pub venue_ts_ms: i64,
    /// Engine monotonic nanoseconds at socket read.
    pub recv_ns: u64,
    /// Venue update sequence for gap detection.
    pub seq: u64,
}

/// Ticker state for one symbol. Copy, fixed-size, no heap.
#[derive(Copy, Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Ticker {
    pub last_px: f64,
    pub mark_px: f64,
    pub index_px: f64,
    pub funding_rate: f64,
    pub next_funding_ms: i64,
    pub venue_ts_ms: i64,
    pub recv_ns: u64,
}

/// One parsed market message, delivered to strategies immediately after the
/// shared [`MarketState`] has been updated with it.
#[derive(Copy, Clone, Debug, PartialEq)]
pub enum MarketEvent {
    Quote { symbol: SymbolId, quote: Quote },
    Ticker { symbol: SymbolId, ticker: Ticker },
    /// The feed reconnected; per-symbol sequences reset. Strategies holding
    /// assumptions keyed to continuity must re-arm.
    FeedReset { recv_ns: u64 },
}

#[derive(Debug, thiserror::Error)]
pub enum FeedError {
    #[error("feed transport: {0}")]
    Transport(String),
    #[error("feed message unreadable: {0}")]
    BadMessage(String),
    #[error("feed closed")]
    Closed,
}

/// A live market data source. Implementations own the socket and the parse;
/// the engine core owns [`MarketState`] and applies the events it is handed.
/// `next_event` resolves with the next parsed message; reconnects happen
/// inside and surface as [`MarketEvent::FeedReset`].
#[allow(async_fn_in_trait)]
pub trait MarketFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError>;
}

/// A live order/fill update source (the venue's private stream).
#[allow(async_fn_in_trait)]
pub trait OrderFeed {
    async fn next_update(&mut self) -> Result<crate::orders::OrderUpdate, FeedError>;
}

/// The single in-memory market picture. The market data crate is the only
/// writer; everything else reads. Flat vectors indexed by [`SymbolId`].
#[derive(Debug, Default)]
pub struct MarketState {
    pub table: SymbolTable,
    pub quotes: Vec<Quote>,
    pub tickers: Vec<Ticker>,
}

impl MarketState {
    /// Intern a symbol and size the flat vectors to cover it.
    pub fn add_symbol(&mut self, name: &str) -> SymbolId {
        let id = self.table.intern(name);
        let need = self.table.len();
        if self.quotes.len() < need {
            self.quotes.resize(need, Quote::default());
            self.tickers.resize(need, Ticker::default());
        }
        id
    }

    pub fn quote(&self, id: SymbolId) -> &Quote {
        &self.quotes[id.0 as usize]
    }

    pub fn ticker(&self, id: SymbolId) -> &Ticker {
        &self.tickers[id.0 as usize]
    }

    pub fn apply(&mut self, event: &MarketEvent) {
        match *event {
            MarketEvent::Quote { symbol, quote } => self.quotes[symbol.0 as usize] = quote,
            MarketEvent::Ticker { symbol, ticker } => self.tickers[symbol.0 as usize] = ticker,
            MarketEvent::FeedReset { .. } => {}
        }
    }
}
