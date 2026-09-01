//! Per-symbol running state, and the decision of what a parsed frame means.
//!
//! Everything here is synchronous and socket-free: hand it a frame and it
//! returns the market event, nothing, or the verdict that the stream lost
//! continuity and must be resynced. The socket layer obeys that verdict.

use engine_types::{
    BookLevel, Depth, MarketEvent, Subscription, SymbolId, SymbolTable, Ticker, TradeFlow,
    BOOK_DEPTH,
};

use crate::bybit::parse::{BookFrame, Levels, ParsedFrame, TickerFrame, TradeFrame};

/// What the feed should do with a frame it just parsed.
#[derive(Copy, Clone, Debug, PartialEq)]
// The event owns an inline L50 update so applying a book does not allocate.
#[allow(clippy::large_enum_variant)]
pub enum Applied {
    /// Hand this to the strategies.
    Event(MarketEvent),
    /// Absorbed; there is nothing for a strategy to see.
    Nothing,
    /// Continuity is broken. Reconnect, resubscribe, take fresh snapshots.
    Resync(ResyncReason),
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum ResyncReason {
    /// An update id did not follow the previous one.
    SequenceGap,
    /// A delta arrived before any snapshot, so the book is unknown.
    DeltaBeforeSnapshot,
    /// The venue refused or dropped the subscription.
    SubscriptionLost,
}

#[derive(Copy, Clone, Debug, Default)]
struct BookState {
    depth: Depth,
    last_update_id: u64,
    has_snapshot: bool,
}

#[derive(Copy, Clone, Debug, Default)]
struct TickerState {
    ticker: Ticker,
    has_snapshot: bool,
}

/// The feed's own symbol interning plus the running per-symbol state, held in
/// flat vectors indexed by [`SymbolId`].
#[derive(Debug, Default)]
pub struct FeedState {
    table: SymbolTable,
    books: Vec<BookState>,
    depths: Vec<BookState>,
    tickers: Vec<TickerState>,
}

impl FeedState {
    /// Intern every subscribed symbol, in subscription order. Which feed a
    /// symbol wanted is carried by the topic list, not here: a topic that was
    /// never subscribed never arrives.
    pub fn new(subs: &[Subscription]) -> Self {
        let mut out = FeedState::default();
        for sub in subs {
            out.intern(&sub.symbol);
        }
        out
    }

    pub(crate) fn intern(&mut self, name: &str) -> SymbolId {
        let id = self.table.intern(name);
        let need = self.table.len();
        if self.books.len() < need {
            self.books.resize(need, BookState::default());
            self.depths.resize(need, BookState::default());
            self.tickers.resize(need, TickerState::default());
        }
        id
    }

    /// The feed's symbol table. The engine core seeds its own `MarketState`
    /// from this so both sides agree on what a [`SymbolId`] means.
    pub fn table(&self) -> &SymbolTable {
        &self.table
    }

    /// Keep the interning, drop the running state. The feed builds one of
    /// these to answer `symbols()` while the socket worker keeps its own.
    pub fn into_table(self) -> SymbolTable {
        self.table
    }

    pub fn symbol_id(&self, name: &str) -> Option<SymbolId> {
        self.table.get(name)
    }

    /// Forget every book and ticker. Called on reconnect: the new socket
    /// starts a new epoch, and merging across the seam would invent prices.
    pub fn reset(&mut self) {
        for b in &mut self.books {
            *b = BookState::default();
        }
        for b in &mut self.depths {
            *b = BookState::default();
        }
        for t in &mut self.tickers {
            *t = TickerState::default();
        }
    }

    pub(crate) fn reset_quote(&mut self, symbol: &str) {
        if let Some(id) = self.table.get(symbol) {
            self.books[id.0 as usize] = BookState::default();
        }
    }

    /// Absorb one parsed frame and say what it means.
    pub fn apply(&mut self, frame: &ParsedFrame<'_>, recv_ns: u64) -> Applied {
        match frame {
            ParsedFrame::Book(b) => self.apply_book(b, recv_ns),
            ParsedFrame::Trades(t) => self.apply_trades(t, recv_ns),
            ParsedFrame::Ticker(t) => self.apply_ticker(t, recv_ns),
            ParsedFrame::Ack { success, .. } => {
                if *success {
                    Applied::Nothing
                } else {
                    Applied::Resync(ResyncReason::SubscriptionLost)
                }
            }
            ParsedFrame::Pong | ParsedFrame::Ignored => Applied::Nothing,
        }
    }

    fn apply_book(&mut self, frame: &BookFrame<'_>, recv_ns: u64) -> Applied {
        let Some(id) = self.table.get(frame.symbol) else {
            return Applied::Nothing;
        };
        let state = match frame.depth {
            1 => &mut self.books[id.0 as usize],
            50 => &mut self.depths[id.0 as usize],
            _ => return Applied::Nothing,
        };

        if frame.snapshot {
            state.depth = Depth::default();
            if let Some(bids) = frame.bids {
                replace_side(&mut state.depth.bids, &mut state.depth.bid_len, bids, true);
            }
            if let Some(asks) = frame.asks {
                replace_side(&mut state.depth.asks, &mut state.depth.ask_len, asks, false);
            }
            state.has_snapshot = true;
        } else {
            if !state.has_snapshot {
                return Applied::Resync(ResyncReason::DeltaBeforeSnapshot);
            }
            if frame.update_id != state.last_update_id + 1 {
                return Applied::Resync(ResyncReason::SequenceGap);
            }
            if let Some(bids) = frame.bids {
                if !bids.is_empty() {
                    if frame.depth == 1 {
                        replace_side(&mut state.depth.bids, &mut state.depth.bid_len, bids, true);
                    } else {
                        apply_side(&mut state.depth.bids, &mut state.depth.bid_len, bids, true);
                    }
                }
            }
            if let Some(asks) = frame.asks {
                if !asks.is_empty() {
                    if frame.depth == 1 {
                        replace_side(&mut state.depth.asks, &mut state.depth.ask_len, asks, false);
                    } else {
                        apply_side(&mut state.depth.asks, &mut state.depth.ask_len, asks, false);
                    }
                }
            }
        }
        state.last_update_id = frame.update_id;
        state.depth.update_id = frame.update_id;
        state.depth.seq = if frame.seq == 0 {
            frame.update_id
        } else {
            frame.seq
        };
        state.depth.venue_ts_ms = frame.venue_ts_ms;
        state.depth.recv_ns = recv_ns;

        if state.depth.bid_len == 0 || state.depth.ask_len == 0 {
            return Applied::Nothing;
        }
        if frame.depth == 1 {
            Applied::Event(MarketEvent::Quote {
                symbol: id,
                quote: state.depth.quote(),
            })
        } else {
            Applied::Event(MarketEvent::Depth {
                symbol: id,
                depth: state.depth,
            })
        }
    }

    fn apply_trades(&mut self, frame: &TradeFrame<'_>, recv_ns: u64) -> Applied {
        let Some(id) = self.table.get(frame.symbol) else {
            return Applied::Nothing;
        };
        Applied::Event(MarketEvent::Trades {
            symbol: id,
            trades: TradeFlow {
                buy_qty: frame.buy_qty,
                sell_qty: frame.sell_qty,
                last_px: frame.last_px,
                trade_count: frame.trade_count,
                seq: frame.seq,
                venue_ts_ms: frame.venue_ts_ms,
                recv_ns,
            },
        })
    }

    fn apply_ticker(&mut self, frame: &TickerFrame<'_>, recv_ns: u64) -> Applied {
        let Some(id) = self.table.get(frame.symbol) else {
            return Applied::Nothing;
        };
        let state = &mut self.tickers[id.0 as usize];

        if frame.snapshot {
            state.ticker = Ticker::default();
            state.has_snapshot = true;
        } else if !state.has_snapshot {
            // Nothing to merge onto; wait for the venue's snapshot.
            return Applied::Nothing;
        }

        let t = &mut state.ticker;
        if let Some(v) = frame.last_px {
            t.last_px = v;
        }
        if let Some(v) = frame.mark_px {
            t.mark_px = v;
        }
        if let Some(v) = frame.index_px {
            t.index_px = v;
        }
        if let Some(v) = frame.funding_rate {
            t.funding_rate = v;
        }
        if let Some(v) = frame.next_funding_ms {
            t.next_funding_ms = v;
        }
        t.venue_ts_ms = frame.venue_ts_ms;
        t.recv_ns = recv_ns;

        Applied::Event(MarketEvent::Ticker {
            symbol: id,
            ticker: *t,
        })
    }
}

fn replace_side(out: &mut [BookLevel; BOOK_DEPTH], len: &mut u8, levels: Levels, bids: bool) {
    *out = [BookLevel::default(); BOOK_DEPTH];
    *len = 0;
    apply_side(out, len, levels, bids);
}

fn apply_side(out: &mut [BookLevel; BOOK_DEPTH], len: &mut u8, changes: Levels, bids: bool) {
    for change in changes.iter() {
        let active = *len as usize;
        if let Some(index) = out[..active].iter().position(|level| level.px == change.px) {
            if change.qty > 0.0 {
                out[index].qty = change.qty;
            } else {
                out.copy_within(index + 1..active, index);
                out[active - 1] = BookLevel::default();
                *len -= 1;
            }
            continue;
        }
        if change.qty <= 0.0 {
            continue;
        }
        let insert = out[..active]
            .iter()
            .position(|level| {
                if bids {
                    change.px > level.px
                } else {
                    change.px < level.px
                }
            })
            .unwrap_or(active);
        if insert >= BOOK_DEPTH {
            continue;
        }
        let new_len = (active + 1).min(BOOK_DEPTH);
        if insert + 1 < new_len {
            out.copy_within(insert..new_len - 1, insert + 1);
        }
        out[insert] = BookLevel {
            px: change.px,
            qty: change.qty,
        };
        *len = new_len as u8;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bybit::parse::parse_frame;
    use engine_types::{Feed, Quote};

    fn subs() -> Vec<Subscription> {
        vec![
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Ticker,
            },
        ]
    }

    fn feed_state() -> FeedState {
        FeedState::new(&subs())
    }

    fn ob(update_id: u64, kind: &str, bids: &str, asks: &str) -> String {
        format!(
            r#"{{"topic":"orderbook.1.BTCUSDT","ts":10,"type":"{kind}","data":{{"s":"BTCUSDT","b":{bids},"a":{asks},"u":{update_id}}},"cts":9}}"#
        )
    }

    fn apply(state: &mut FeedState, raw: &str) -> Applied {
        state.apply(&parse_frame(raw).expect("parses"), 42)
    }

    fn quote(applied: Applied) -> Quote {
        match applied {
            Applied::Event(MarketEvent::Quote { quote, .. }) => quote,
            other => panic!("expected a quote, got {other:?}"),
        }
    }

    fn ticker(applied: Applied) -> Ticker {
        match applied {
            Applied::Event(MarketEvent::Ticker { ticker, .. }) => ticker,
            other => panic!("expected a ticker, got {other:?}"),
        }
    }

    fn depth(applied: Applied) -> Depth {
        match applied {
            Applied::Event(MarketEvent::Depth { depth, .. }) => depth,
            other => panic!("expected depth, got {other:?}"),
        }
    }

    fn trades(applied: Applied) -> TradeFlow {
        match applied {
            Applied::Event(MarketEvent::Trades { trades, .. }) => trades,
            other => panic!("expected trades, got {other:?}"),
        }
    }

    #[test]
    fn snapshot_becomes_a_quote() {
        let mut s = feed_state();
        let q = quote(apply(
            &mut s,
            &ob(
                100,
                "snapshot",
                r#"[["10.0","1.5"]]"#,
                r#"[["10.1","2.5"]]"#,
            ),
        ));
        assert_eq!(q.bid_px, 10.0);
        assert_eq!(q.bid_qty, 1.5);
        assert_eq!(q.ask_px, 10.1);
        assert_eq!(q.ask_qty, 2.5);
        assert_eq!(q.venue_ts_ms, 9);
        assert_eq!(q.recv_ns, 42);
        assert_eq!(q.seq, 100);
    }

    #[test]
    fn one_sided_delta_keeps_the_other_side() {
        let mut s = feed_state();
        apply(
            &mut s,
            &ob(
                100,
                "snapshot",
                r#"[["10.0","1.5"]]"#,
                r#"[["10.1","2.5"]]"#,
            ),
        );
        let q = quote(apply(
            &mut s,
            &ob(101, "delta", r#"[["10.05","3.0"]]"#, "[]"),
        ));
        assert_eq!(q.bid_px, 10.05);
        assert_eq!(q.bid_qty, 3.0);
        // Untouched side survives.
        assert_eq!(q.ask_px, 10.1);
        assert_eq!(q.ask_qty, 2.5);

        // A side the message never mentions also survives.
        let raw = r#"{"topic":"orderbook.1.BTCUSDT","ts":10,"type":"delta","data":{"s":"BTCUSDT","a":[["10.2","4.0"]],"u":102},"cts":9}"#;
        let q = quote(apply(&mut s, raw));
        assert_eq!(q.bid_px, 10.05);
        assert_eq!(q.ask_px, 10.2);
    }

    #[test]
    fn zero_quantity_removes_the_level_and_suppresses_the_quote() {
        let mut s = feed_state();
        apply(
            &mut s,
            &ob(
                100,
                "snapshot",
                r#"[["10.0","1.5"]]"#,
                r#"[["10.1","2.5"]]"#,
            ),
        );
        // The best bid is filled or cancelled: no bid, so no publishable quote.
        assert_eq!(
            apply(&mut s, &ob(101, "delta", r#"[["10.0","0"]]"#, "[]")),
            Applied::Nothing
        );
        // The replacement bid restores it.
        let q = quote(apply(
            &mut s,
            &ob(102, "delta", r#"[["9.99","7.0"]]"#, "[]"),
        ));
        assert_eq!(q.bid_px, 9.99);
        assert_eq!(q.ask_px, 10.1);
    }

    #[test]
    fn sequence_gap_asks_for_a_resync() {
        let mut s = feed_state();
        apply(
            &mut s,
            &ob(
                100,
                "snapshot",
                r#"[["10.0","1.5"]]"#,
                r#"[["10.1","2.5"]]"#,
            ),
        );
        // 101 is the only acceptable next id.
        assert!(matches!(
            apply(&mut s, &ob(101, "delta", "[]", "[]")),
            Applied::Event(_)
        ));
        assert_eq!(
            apply(&mut s, &ob(103, "delta", r#"[["10.0","9.0"]]"#, "[]")),
            Applied::Resync(ResyncReason::SequenceGap)
        );
    }

    #[test]
    fn a_snapshot_heals_a_gap_without_resyncing() {
        let mut s = feed_state();
        apply(
            &mut s,
            &ob(
                100,
                "snapshot",
                r#"[["10.0","1.5"]]"#,
                r#"[["10.1","2.5"]]"#,
            ),
        );
        // A snapshot replaces the book, so its id need not follow.
        let q = quote(apply(
            &mut s,
            &ob(
                900,
                "snapshot",
                r#"[["11.0","1.0"]]"#,
                r#"[["11.1","1.0"]]"#,
            ),
        ));
        assert_eq!(q.bid_px, 11.0);
        assert_eq!(q.seq, 900);
        // And the sequence continues from the snapshot.
        assert!(matches!(
            apply(&mut s, &ob(901, "delta", "[]", "[]")),
            Applied::Event(_)
        ));
    }

    #[test]
    fn delta_before_any_snapshot_asks_for_a_resync() {
        let mut s = feed_state();
        assert_eq!(
            apply(&mut s, &ob(50, "delta", r#"[["10.0","1.0"]]"#, "[]")),
            Applied::Resync(ResyncReason::DeltaBeforeSnapshot)
        );
    }

    #[test]
    fn reset_forgets_the_book_so_the_next_delta_resyncs() {
        let mut s = feed_state();
        apply(
            &mut s,
            &ob(
                100,
                "snapshot",
                r#"[["10.0","1.5"]]"#,
                r#"[["10.1","2.5"]]"#,
            ),
        );
        s.reset();
        assert_eq!(
            apply(&mut s, &ob(101, "delta", r#"[["10.0","1.0"]]"#, "[]")),
            Applied::Resync(ResyncReason::DeltaBeforeSnapshot)
        );
    }

    #[test]
    fn resetting_one_quote_preserves_its_ticker_delta_state() {
        let mut state = feed_state();
        apply(
            &mut state,
            &ob(
                100,
                "snapshot",
                r#"[["10.0","1.5"]]"#,
                r#"[["10.1","2.5"]]"#,
            ),
        );
        let ticker_snapshot = r#"{"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","lastPrice":"100.0","markPrice":"100.1","indexPrice":"100.2"},"cs":1,"ts":500}"#;
        apply(&mut state, ticker_snapshot);

        state.reset_quote("BTCUSDT");
        assert_eq!(
            apply(&mut state, &ob(101, "delta", r#"[["10.0","1.0"]]"#, "[]")),
            Applied::Resync(ResyncReason::DeltaBeforeSnapshot)
        );
        let ticker_delta = r#"{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","indexPrice":"200.5"},"cs":2,"ts":600}"#;
        let ticker = ticker(apply(&mut state, ticker_delta));
        assert_eq!(ticker.last_px, 100.0);
        assert_eq!(ticker.mark_px, 100.1);
        assert_eq!(ticker.index_px, 200.5);
    }

    #[test]
    fn ticker_delta_merges_onto_the_snapshot() {
        let mut s = feed_state();
        let snap = r#"{"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","lastPrice":"100.0","markPrice":"100.1","indexPrice":"100.2","fundingRate":"0.0001","nextFundingTime":"1786665600000"},"cs":1,"ts":500}"#;
        let t = ticker(apply(&mut s, snap));
        assert_eq!(t.last_px, 100.0);
        assert_eq!(t.mark_px, 100.1);
        assert_eq!(t.funding_rate, 0.0001);
        assert_eq!(t.next_funding_ms, 1786665600000);
        assert_eq!(t.venue_ts_ms, 500);

        let delta = r#"{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","indexPrice":"200.5"},"cs":2,"ts":600}"#;
        let t = ticker(apply(&mut s, delta));
        // Only the index moved; everything absent held its value.
        assert_eq!(t.index_px, 200.5);
        assert_eq!(t.last_px, 100.0);
        assert_eq!(t.mark_px, 100.1);
        assert_eq!(t.funding_rate, 0.0001);
        assert_eq!(t.next_funding_ms, 1786665600000);
        assert_eq!(t.venue_ts_ms, 600);
    }

    #[test]
    fn ticker_snapshot_replaces_rather_than_merges() {
        let mut s = feed_state();
        let first = r#"{"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","lastPrice":"100.0","markPrice":"100.1"},"cs":1,"ts":500}"#;
        apply(&mut s, first);
        let second = r#"{"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","lastPrice":"7.0"},"cs":2,"ts":600}"#;
        let t = ticker(apply(&mut s, second));
        assert_eq!(t.last_px, 7.0);
        assert_eq!(t.mark_px, 0.0);
    }

    #[test]
    fn ticker_delta_before_a_snapshot_is_dropped() {
        let mut s = feed_state();
        let delta = r#"{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","indexPrice":"200.5"},"cs":2,"ts":600}"#;
        assert_eq!(apply(&mut s, delta), Applied::Nothing);
    }

    #[test]
    fn acks_and_pongs_pass_through_quietly() {
        let mut s = feed_state();
        let ack = r#"{"success":true,"ret_msg":"","conn_id":"x","req_id":"t1","op":"subscribe"}"#;
        assert_eq!(apply(&mut s, ack), Applied::Nothing);
        let pong = r#"{"success":true,"ret_msg":"pong","conn_id":"x","req_id":"p1","op":"ping"}"#;
        assert_eq!(apply(&mut s, pong), Applied::Nothing);
        let refused = r#"{"success":false,"ret_msg":"Invalid symbol","conn_id":"x","req_id":"t1","op":"subscribe"}"#;
        assert_eq!(
            apply(&mut s, refused),
            Applied::Resync(ResyncReason::SubscriptionLost)
        );
    }

    #[test]
    fn frames_for_unsubscribed_symbols_are_ignored() {
        let mut s = feed_state();
        let raw = r#"{"topic":"orderbook.1.ETHUSDT","ts":10,"type":"snapshot","data":{"s":"ETHUSDT","b":[["1","1"]],"a":[["2","1"]],"u":1},"cts":9}"#;
        assert_eq!(apply(&mut s, raw), Applied::Nothing);
    }

    #[test]
    fn symbols_are_interned_once_per_name() {
        let s = feed_state();
        assert_eq!(s.table().len(), 1);
        assert_eq!(s.symbol_id("BTCUSDT"), Some(SymbolId(0)));
        assert_eq!(s.symbol_id("ETHUSDT"), None);
    }

    #[test]
    fn l50_deltas_update_delete_and_insert_without_losing_the_rest() {
        let mut state = feed_state();
        let snapshot = r#"{"topic":"orderbook.50.BTCUSDT","ts":10,"type":"snapshot","data":{"s":"BTCUSDT","b":[["100","1"],["99","2"],["98","3"]],"a":[["101","4"],["102","5"]],"u":100,"seq":1000},"cts":9}"#;
        let first = depth(apply(&mut state, snapshot));
        assert_eq!(first.bid_len, 3);
        assert_eq!(first.ask_len, 2);

        let delta = r#"{"topic":"orderbook.50.BTCUSDT","ts":11,"type":"delta","data":{"s":"BTCUSDT","b":[["100","0"],["99","7"],["99.5","6"]],"a":[["100.5","8"]],"u":101,"seq":1001},"cts":10}"#;
        let next = depth(apply(&mut state, delta));
        assert_eq!(next.bids[0], BookLevel { px: 99.5, qty: 6.0 });
        assert_eq!(next.bids[1], BookLevel { px: 99.0, qty: 7.0 });
        assert_eq!(next.bids[2], BookLevel { px: 98.0, qty: 3.0 });
        assert_eq!(
            next.asks[0],
            BookLevel {
                px: 100.5,
                qty: 8.0
            }
        );
        assert_eq!(
            next.asks[1],
            BookLevel {
                px: 101.0,
                qty: 4.0
            }
        );
        assert_eq!(next.seq, 1001);
        assert_eq!(next.update_id, 101);
    }

    #[test]
    fn public_trade_flow_becomes_a_fixed_market_event() {
        let mut state = feed_state();
        let raw = r#"{"topic":"publicTrade.BTCUSDT","ts":1003,"data":[{"T":1000,"s":"BTCUSDT","S":"Buy","v":"2","p":"100.1","seq":10},{"T":1002,"s":"BTCUSDT","S":"Sell","v":"0.5","p":"100.0","seq":12}]}"#;
        let flow = trades(apply(&mut state, raw));
        assert_eq!(flow.buy_qty, 2.0);
        assert_eq!(flow.sell_qty, 0.5);
        assert_eq!(flow.last_px, 100.0);
        assert_eq!(flow.seq, 12);
        assert_eq!(flow.recv_ns, 42);
    }
}
