//! The tape as the engine's market feed, and the pump that moves time.
//!
//! Two things read the tape through one shared [`Cursor`]:
//!
//! - [`TapeFeed`], the loop's market feed. On every poll it first hands the
//!   loop back once, so the branches after it register their waits and the
//!   venue task gets a turn; then it decides what the loop may see next:
//!   a waiter the clock already woke and that has not run (hand back again),
//!   a waiter due before the next row (move the clock to it, hand back), or
//!   the next row (move the clock to its receive stamp, let the venue see
//!   it, deliver what the engine subscribed to).
//! - [`pump`], a task that runs only when nothing else can. The loop
//!   sometimes waits on a venue reply outside its `select!` — the account
//!   refresh on a tick, a stop or leverage change — and while it does, the
//!   feed is not polled and the clock has no pump. On a single-threaded
//!   runtime that is detectable exactly: the feed was not polled between two
//!   of the pump's own turns. Then the pump applies to the venue every row
//!   the venue would have seen before the earliest waiter, moves the clock
//!   to that waiter, and stops. The rows it applied reach the engine
//!   afterwards, with their own earlier stamps — which is what a live engine
//!   sees when it returns from a venue call to a buffered socket.
//!
//! So a fill queued for `t + hop`, a tick due at `t + 250 ms`, and a row
//! received at `t + 300 ms` reach the loop in exactly that order, whatever
//! the wall clock does. At the tape's end the clock is closed and the feed
//! reports itself closed; the loop's settle and stop then run in one burst.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};

use engine_types::{
    Feed, FeedError, MarketEvent, MarketFeed, Subscription, Symbol, SymbolId, Ticker, TradeFlow,
};

use super::scheduler::{Scheduler, WaiterKind, YieldNow};
use super::tape::{BookBuilder, TapeError, TapeReader, TapeRow, TapeStats};
use super::venue::SimulatedVenue;

const ALL_KINDS: [WaiterKind; 4] = [
    WaiterKind::Timer,
    WaiterKind::Venue,
    WaiterKind::Private,
    WaiterKind::Signal,
];
/// What the tape's end still waits for: replies in flight and updates on
/// their hop. Never a periodic tick, which would run forever.
const DRAIN_KINDS: [WaiterKind; 2] = [WaiterKind::Venue, WaiterKind::Private];

/// The tape, the venue-side state it drives, and the engine-visible events
/// waiting to be delivered.
pub struct Cursor {
    reader: TapeReader,
    venue: Arc<Mutex<SimulatedVenue>>,
    /// Engine ids by position; the same order the venue and the engine use.
    symbols: Vec<Symbol>,
    /// Feeds each symbol's strategies asked for. `Feed` has no ordering, so
    /// a small vector stands in for a set.
    subscribed: Vec<Vec<Feed>>,
    books: HashMap<(u16, u32), BookBuilder>,
    /// The shallowest book depth seen per symbol — the one its quotes come
    /// from, as the live feed's quotes come from `orderbook.1`.
    quote_depth: Vec<Option<u32>>,
    /// The deepest book depth seen per symbol — the one the venue matches
    /// against.
    venue_depth: Vec<Option<u32>>,
    tickers: Vec<Ticker>,
    trade_seq: Vec<u64>,
    peeked: Option<(u64, TapeRow)>,
    ready: VecDeque<MarketEvent>,
    exhausted: bool,
    /// Rows for symbols the tape names but nothing follows. Counted, so a
    /// misnamed config is visible in the report.
    pub unknown_symbol_rows: u64,
}

impl Cursor {
    pub fn new(
        reader: TapeReader,
        venue: Arc<Mutex<SimulatedVenue>>,
        symbols: &[Symbol],
        subscriptions: &[Subscription],
    ) -> Self {
        let mut cursor = Cursor {
            reader,
            venue,
            symbols: Vec::new(),
            subscribed: Vec::new(),
            books: HashMap::new(),
            quote_depth: Vec::new(),
            venue_depth: Vec::new(),
            tickers: Vec::new(),
            trade_seq: Vec::new(),
            peeked: None,
            ready: VecDeque::new(),
            exhausted: false,
            unknown_symbol_rows: 0,
        };
        for symbol in symbols {
            cursor.intern(symbol);
        }
        for sub in subscriptions {
            let id = cursor.intern(&sub.symbol);
            cursor.subscribe(id, sub.feed);
        }
        cursor
    }

    fn intern(&mut self, symbol: &str) -> SymbolId {
        if let Some(i) = self.symbols.iter().position(|s| s == symbol) {
            return SymbolId(i as u16);
        }
        self.symbols.push(symbol.to_string());
        self.subscribed.push(Vec::new());
        self.quote_depth.push(None);
        self.venue_depth.push(None);
        self.tickers.push(Ticker::default());
        self.trade_seq.push(0);
        SymbolId((self.symbols.len() - 1) as u16)
    }

    fn subscribe(&mut self, id: SymbolId, feed: Feed) {
        let subs = &mut self.subscribed[id.0 as usize];
        if !subs.contains(&feed) {
            subs.push(feed);
        }
    }

    pub fn stats(&self) -> &TapeStats {
        &self.reader.stats
    }

    /// The receive stamp of the next row, without consuming it.
    pub fn next_row_at(&mut self) -> Result<Option<u64>, TapeError> {
        if self.peeked.is_none() && !self.exhausted {
            match self.reader.next_row()? {
                Some(row) => self.peeked = Some(row),
                None => self.exhausted = true,
            }
        }
        Ok(self.peeked.as_ref().map(|(t, _)| *t))
    }

    /// Consume the peeked row: the venue sees it, then whatever the engine
    /// subscribed to is queued for delivery. The caller has already moved
    /// the clock to its stamp.
    pub fn absorb_next(&mut self) {
        let Some((recv_ns, row)) = self.peeked.take() else {
            return;
        };
        match row {
            TapeRow::Book(book) => {
                let Some(id) = self.known(&book.symbol) else {
                    return;
                };
                let i = id.0 as usize;
                let depth_key = book.depth;
                let quote_depth = *self.quote_depth[i].get_or_insert(depth_key);
                if depth_key < quote_depth {
                    self.quote_depth[i] = Some(depth_key);
                }
                let venue_depth = *self.venue_depth[i].get_or_insert(depth_key);
                if depth_key > venue_depth {
                    self.venue_depth[i] = Some(depth_key);
                }
                let builder = self.books.entry((id.0, depth_key)).or_default();
                let Some(depth) = builder.apply(&book).copied() else {
                    return;
                };
                if self.venue_depth[i] == Some(depth_key) {
                    self.venue
                        .lock()
                        .unwrap_or_else(|p| p.into_inner())
                        .on_book(id, &depth);
                }
                let subs = &self.subscribed[i];
                if self.quote_depth[i] == Some(depth_key) && subs.contains(&Feed::Quote) {
                    self.ready.push_back(MarketEvent::Quote {
                        symbol: id,
                        quote: depth.quote(),
                    });
                }
                if depth_key > 1 && subs.contains(&Feed::Depth) {
                    self.ready
                        .push_back(MarketEvent::Depth { symbol: id, depth });
                }
            }
            TapeRow::Trade(trade) => {
                let Some(id) = self.known(&trade.symbol) else {
                    return;
                };
                let i = id.0 as usize;
                self.venue
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .on_trade(id, trade.price, trade.qty, trade.buyer_aggressor);
                if self.subscribed[i].contains(&Feed::Trades) {
                    self.trade_seq[i] += 1;
                    let (buy_qty, sell_qty) = if trade.buyer_aggressor {
                        (trade.qty, 0.0)
                    } else {
                        (0.0, trade.qty)
                    };
                    self.ready.push_back(MarketEvent::Trades {
                        symbol: id,
                        trades: TradeFlow {
                            buy_qty,
                            sell_qty,
                            last_px: trade.price,
                            trade_count: 1,
                            seq: self.trade_seq[i],
                            venue_ts_ms: (trade.exchange_ts_ns / 1_000_000) as i64,
                            recv_ns,
                        },
                    });
                }
            }
            TapeRow::Ticker(row) => {
                let Some(id) = self.known(&row.symbol) else {
                    return;
                };
                let i = id.0 as usize;
                self.venue
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .on_ticker(id, &row);
                let t = &mut self.tickers[i];
                if let Some(v) = row.last_price {
                    t.last_px = v;
                }
                if let Some(v) = row.mark_price {
                    t.mark_px = v;
                }
                if let Some(v) = row.index_price {
                    t.index_px = v;
                }
                if let Some(v) = row.funding_rate {
                    t.funding_rate = v;
                }
                if let Some(v) = row.next_funding_time_ms {
                    t.next_funding_ms = v;
                }
                t.venue_ts_ms = (row.exchange_ts_ns / 1_000_000) as i64;
                t.recv_ns = recv_ns;
                if self.subscribed[i].contains(&Feed::Ticker) {
                    self.ready.push_back(MarketEvent::Ticker {
                        symbol: id,
                        ticker: *t,
                    });
                }
            }
        }
    }

    fn known(&mut self, symbol: &str) -> Option<SymbolId> {
        match self.symbols.iter().position(|s| s == symbol) {
            Some(i) => Some(SymbolId(i as u16)),
            None => {
                self.unknown_symbol_rows += 1;
                None
            }
        }
    }
}

pub type SharedCursor = Arc<Mutex<Cursor>>;

fn lock(cursor: &SharedCursor) -> std::sync::MutexGuard<'_, Cursor> {
    cursor.lock().unwrap_or_else(|p| p.into_inner())
}

/// The loop's market feed over the shared cursor.
pub struct TapeFeed {
    cursor: SharedCursor,
    scheduler: Scheduler,
}

impl TapeFeed {
    pub fn new(cursor: SharedCursor, scheduler: Scheduler) -> Self {
        TapeFeed { cursor, scheduler }
    }
}

impl MarketFeed for TapeFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        self.scheduler.note_feed_poll();
        self.scheduler.open();
        // Hand the loop back once before deciding anything. The branches
        // after this one in the loop's `select!` register their waits only
        // when polled, and the venue task only runs when the loop's task
        // yields; a tape that is always ready would otherwise let neither
        // happen, and the clock would have nothing to order.
        YieldNow::new().await;
        loop {
            if let Some(event) = lock(&self.cursor).ready.pop_front() {
                return Ok(event);
            }
            if self.scheduler.has_fired_unconsumed() {
                YieldNow::new().await;
                continue;
            }
            let next_row_at = lock(&self.cursor)
                .next_row_at()
                .map_err(|e| FeedError::Transport(e.to_string()))?;
            let Some(row_at) = next_row_at else {
                if let Some(due) = self.scheduler.earliest_pending(&DRAIN_KINDS) {
                    self.scheduler.advance_to(due);
                    YieldNow::new().await;
                    continue;
                }
                self.scheduler.close();
                return Err(FeedError::Closed);
            };
            if let Some(due) = self.scheduler.earliest_pending(&ALL_KINDS) {
                if due <= row_at {
                    self.scheduler.advance_to(due);
                    YieldNow::new().await;
                    continue;
                }
            }
            self.scheduler.advance_to(row_at);
            lock(&self.cursor).absorb_next();
        }
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        let mut cursor = lock(&self.cursor);
        let id = cursor.intern(symbol);
        cursor.subscribe(id, feed);
        Some(id)
    }
}

/// The idle pump. Runs beside the loop; acts only when the loop is not
/// polling its feed — see the module note.
pub async fn pump(cursor: SharedCursor, scheduler: Scheduler) {
    let mut seen = scheduler.feed_polls();
    loop {
        tokio::task::yield_now().await;
        if scheduler.is_closed() || !scheduler.is_pumped() {
            continue;
        }
        let polls = scheduler.feed_polls();
        let idle = polls == seen;
        seen = polls;
        if !idle || scheduler.has_fired_unconsumed() {
            continue;
        }
        let Some(due) = scheduler.earliest_pending(&ALL_KINDS) else {
            continue;
        };
        // The venue sees every row that arrived before the waiter's instant.
        loop {
            let row_at = match lock(&cursor).next_row_at() {
                Ok(Some(t)) if t <= due => t,
                _ => break,
            };
            scheduler.advance_to(row_at);
            lock(&cursor).absorb_next();
        }
        scheduler.advance_to(due);
    }
}
