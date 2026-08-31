use serde::{Deserialize, Serialize};

use crate::ids::{SymbolId, SymbolTable};

/// Which feed a strategy wants for a symbol.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Feed {
    /// Best bid/ask (venue orderbook depth-1 stream).
    Quote,
    /// Reconstructed order book through level 50.
    Depth,
    /// Aggressor-side public trades.
    Trades,
    /// Ticker: last, mark, index, funding.
    Ticker,
}

pub const BOOK_DEPTH: usize = 50;

/// One price level. Copy, fixed-size, no heap.
#[derive(Copy, Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct BookLevel {
    pub px: f64,
    pub qty: f64,
}

/// A reconstructed 50-level book. Only the first `bid_len` and `ask_len`
/// entries are populated; bids descend and asks ascend.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct Depth {
    pub bids: [BookLevel; BOOK_DEPTH],
    pub asks: [BookLevel; BOOK_DEPTH],
    pub bid_len: u8,
    pub ask_len: u8,
    /// Bybit's book update id.
    pub update_id: u64,
    /// Venue cross-sequence, used to order book and trade events.
    pub seq: u64,
    pub venue_ts_ms: i64,
    pub recv_ns: u64,
}

impl Default for Depth {
    fn default() -> Self {
        Self {
            bids: [BookLevel::default(); BOOK_DEPTH],
            asks: [BookLevel::default(); BOOK_DEPTH],
            bid_len: 0,
            ask_len: 0,
            update_id: 0,
            seq: 0,
            venue_ts_ms: 0,
            recv_ns: 0,
        }
    }
}

impl Quote {
    /// Whether this touch should replace the one already held.
    ///
    /// The touch arrives on more than one topic, and each topic sequences
    /// itself, so `seq` cannot order them against each other. The socket read
    /// stamp can, and it is the only field that can. Equal stamps let the
    /// later arrival win, which is what an unordered pair within the same
    /// nanosecond amounts to.
    pub fn supersedes(&self, held: &Quote) -> bool {
        self.recv_ns >= held.recv_ns
    }
}

impl Depth {
    pub fn best_bid(&self) -> Option<BookLevel> {
        (self.bid_len > 0).then(|| self.bids[0])
    }

    pub fn best_ask(&self) -> Option<BookLevel> {
        (self.ask_len > 0).then(|| self.asks[0])
    }

    pub fn quote(&self) -> Quote {
        let bid = self.best_bid().unwrap_or_default();
        let ask = self.best_ask().unwrap_or_default();
        Quote {
            bid_px: bid.px,
            bid_qty: bid.qty,
            ask_px: ask.px,
            ask_qty: ask.qty,
            venue_ts_ms: self.venue_ts_ms,
            recv_ns: self.recv_ns,
            seq: self.seq,
        }
    }
}

/// Public trades aggregated over one venue message. `buy_qty` means buyers
/// crossed the spread; `sell_qty` means sellers did.
#[derive(Copy, Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct TradeFlow {
    pub buy_qty: f64,
    pub sell_qty: f64,
    pub last_px: f64,
    pub trade_count: u16,
    pub seq: u64,
    pub venue_ts_ms: i64,
    pub recv_ns: u64,
}

/// A strategy's request for market data, collected at boot.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
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
// L50 stays inline so every 20 ms book update remains allocation-free.
#[allow(clippy::large_enum_variant)]
#[derive(Copy, Clone, Debug, PartialEq)]
pub enum MarketEvent {
    Quote {
        symbol: SymbolId,
        quote: Quote,
    },
    Depth {
        symbol: SymbolId,
        depth: Depth,
    },
    Trades {
        symbol: SymbolId,
        trades: TradeFlow,
    },
    Ticker {
        symbol: SymbolId,
        ticker: Ticker,
    },
    /// The feed reconnected; per-symbol sequences reset. Strategies holding
    /// assumptions keyed to continuity must re-arm.
    FeedReset {
        recv_ns: u64,
    },
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

    /// Start following a symbol this feed was not built with, and return the
    /// id it will use for it.
    ///
    /// Every component that holds a symbol table has to grow in the same
    /// order, because a `SymbolId` is an index assigned by position — two
    /// components disagreeing about one would put orders on the wrong symbol.
    /// The engine is the only caller and admits in one place, which is what
    /// keeps that order the same everywhere.
    ///
    /// The default refuses, so a feed that cannot grow says so rather than
    /// handing back an id it will never deliver prices for.
    fn admit(&mut self, symbol: &str, feed: crate::market::Feed) -> Option<SymbolId> {
        let _ = (symbol, feed);
        None
    }
}

/// A live order/fill update source (the venue's private stream).
#[allow(async_fn_in_trait)]
pub trait OrderFeed {
    async fn next_update(&mut self) -> Result<crate::orders::OrderUpdate, FeedError>;

    /// Teach this feed what id a symbol has, so a fill on it can be decoded.
    ///
    /// Bybit's private stream is subscribed per account, not per symbol, so
    /// updates for a newly followed name already arrive — what is missing is
    /// the name-to-id mapping to read them with. A fill that cannot be
    /// resolved is a fill the engine does not see.
    fn learn(&mut self, symbol: &str, id: SymbolId) {
        let _ = (symbol, id);
    }
}

/// The single in-memory market picture. The market data crate is the only
/// writer; everything else reads. Flat vectors indexed by [`SymbolId`].
#[derive(Debug, Default)]
pub struct MarketState {
    pub table: SymbolTable,
    pub quotes: Vec<Quote>,
    pub depths: Vec<Depth>,
    pub trades: Vec<TradeFlow>,
    pub tickers: Vec<Ticker>,
}

impl MarketState {
    /// Intern a symbol and size the flat vectors to cover it.
    pub fn add_symbol(&mut self, name: &str) -> SymbolId {
        let id = self.table.intern(name);
        let need = self.table.len();
        if self.quotes.len() < need {
            self.quotes.resize(need, Quote::default());
            self.depths.resize(need, Depth::default());
            self.trades.resize(need, TradeFlow::default());
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

    pub fn depth(&self, id: SymbolId) -> &Depth {
        &self.depths[id.0 as usize]
    }

    pub fn trade_flow(&self, id: SymbolId) -> &TradeFlow {
        &self.trades[id.0 as usize]
    }

    pub fn apply(&mut self, event: &MarketEvent) {
        match *event {
            // The touch reaches this slot from two streams. A venue that
            // publishes the top of book on its own faster topic sends it
            // there first, and the deeper book's copy of the same touch
            // arrives later carrying an older price. Whichever was read off
            // the socket last is the current one, whichever topic it came
            // on; `recv_ns` is the only stamp comparable across topics,
            // because each topic sequences itself.
            MarketEvent::Quote { symbol, quote } => {
                let slot = &mut self.quotes[symbol.0 as usize];
                if quote.supersedes(slot) {
                    *slot = quote;
                }
            }
            MarketEvent::Depth { symbol, depth } => {
                let touch = depth.quote();
                let slot = &mut self.quotes[symbol.0 as usize];
                if touch.supersedes(slot) {
                    *slot = touch;
                }
                self.depths[symbol.0 as usize] = depth;
            }
            MarketEvent::Trades { symbol, trades } => self.trades[symbol.0 as usize] = trades,
            MarketEvent::Ticker { symbol, ticker } => self.tickers[symbol.0 as usize] = ticker,
            // The reset arrives before the new epoch's first price. Clearing
            // here means nobody reads a pre-gap price as current — a zeroed
            // quote is visibly absent, a stale one lies.
            MarketEvent::FeedReset { .. } => {
                self.quotes.fill(Quote::default());
                self.depths.fill(Depth::default());
                self.trades.fill(TradeFlow::default());
                self.tickers.fill(Ticker::default());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn book(bid_px: f64, ask_px: f64, recv_ns: u64) -> Depth {
        let mut depth = Depth::default();
        depth.bids[0] = BookLevel {
            px: bid_px,
            qty: 1.0,
        };
        depth.asks[0] = BookLevel {
            px: ask_px,
            qty: 1.0,
        };
        depth.bid_len = 1;
        depth.ask_len = 1;
        depth.recv_ns = recv_ns;
        depth
    }

    #[test]
    fn the_deep_books_older_copy_of_the_touch_does_not_replace_a_fresher_one() {
        // Both topics carry the top of book. Depth 1 publishes twice as
        // often, so its price is regularly the newer one, and letting the
        // deeper book overwrite it would age every price a strategy reads.
        let mut market = MarketState::default();
        let id = market.add_symbol("BTCUSDT");
        market.apply(&MarketEvent::Depth {
            symbol: id,
            depth: book(99.0, 101.0, 100),
        });
        market.apply(&MarketEvent::Quote {
            symbol: id,
            quote: Quote {
                bid_px: 99.5,
                ask_px: 100.5,
                recv_ns: 200,
                ..Quote::default()
            },
        });
        // The deeper book's next push was read off the socket before the
        // fresh touch: same book state, older stamp.
        market.apply(&MarketEvent::Depth {
            symbol: id,
            depth: book(99.0, 101.0, 150),
        });
        assert_eq!(market.quote(id).bid_px, 99.5, "the fresher touch was lost");
        assert_eq!(market.quote(id).ask_px, 100.5);
        assert_eq!(market.quote(id).recv_ns, 200);
        // The deep book itself is still the newest one delivered: only the
        // touch is arbitrated, never the depth a strategy sizes against.
        assert_eq!(market.depth(id).recv_ns, 150);
    }

    #[test]
    fn the_deep_book_still_carries_the_touch_when_it_is_the_only_stream() {
        // The common case, and the one the arbitration must not break: with
        // no separate top-of-book topic every touch arrives on the deep book
        // and every one of them is the freshest thing there is.
        let mut market = MarketState::default();
        let id = market.add_symbol("BTCUSDT");
        for (step, px) in [(10u64, 99.0), (20, 99.1), (30, 99.2)] {
            market.apply(&MarketEvent::Depth {
                symbol: id,
                depth: book(px, px + 2.0, step),
            });
            assert_eq!(market.quote(id).bid_px, px);
        }
    }

    #[test]
    fn a_price_from_before_a_reset_cannot_come_back() {
        // The reset zeroes the stamp as well as the price, so the freshness
        // test cannot be what keeps a pre-gap price alive.
        let mut market = MarketState::default();
        let id = market.add_symbol("BTCUSDT");
        market.apply(&MarketEvent::Quote {
            symbol: id,
            quote: Quote {
                bid_px: 10.0,
                recv_ns: 900,
                ..Quote::default()
            },
        });
        market.apply(&MarketEvent::FeedReset { recv_ns: 901 });
        market.apply(&MarketEvent::Depth {
            symbol: id,
            depth: book(20.0, 21.0, 902),
        });
        assert_eq!(market.quote(id).bid_px, 20.0);
    }

    #[test]
    fn a_feed_reset_clears_every_price() {
        let mut market = MarketState::default();
        let id = market.add_symbol("BTCUSDT");
        market.apply(&MarketEvent::Quote {
            symbol: id,
            quote: Quote {
                bid_px: 10.0,
                ask_px: 10.5,
                ..Quote::default()
            },
        });
        assert_eq!(market.quote(id).bid_px, 10.0);
        market.apply(&MarketEvent::FeedReset { recv_ns: 1 });
        assert_eq!(*market.quote(id), Quote::default());
        assert_eq!(*market.depth(id), Depth::default());
        assert_eq!(*market.trade_flow(id), TradeFlow::default());
        assert_eq!(*market.ticker(id), Ticker::default());
    }
}
