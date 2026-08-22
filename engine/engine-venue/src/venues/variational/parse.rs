//! Reading Variational's market statistics.
//!
//! One endpoint answers everything this venue publishes: platform totals and,
//! per listing, a mark price, a funding rate and its interval, a base spread,
//! open interest, and indicative two-way quotes at three sizes. There is no
//! order book and no trade tape, so a "quote" here is the venue's own
//! indicative price at a size, not the top of a book — which is why the feed
//! reads the 1k-size quote rather than pretending to a touch it cannot see.

use engine_types::market::{Quote, Ticker};
use engine_types::VenueError;
use serde_json::Value;

use crate::json::{num_field, str_field};

/// One listing, as much of it as the engine has a use for.
#[derive(Clone, Debug, PartialEq)]
pub struct Listing {
    /// The venue's own ticker, e.g. `BTC`.
    pub ticker: String,
    pub mark_px: f64,
    pub funding_rate: f64,
    pub funding_interval_s: i64,
    /// The indicative bid and ask at the smallest published size, when the
    /// venue quotes one.
    pub bid_px: Option<f64>,
    pub ask_px: Option<f64>,
}

impl Listing {
    /// The engine's spelling of this listing's symbol.
    pub fn symbol(&self) -> String {
        format!("{}USDT", self.ticker.trim().to_ascii_uppercase())
    }

    pub fn quote(&self, venue_ts_ms: i64, recv_ns: u64) -> Option<Quote> {
        let (bid_px, ask_px) = (self.bid_px?, self.ask_px?);
        Some(Quote {
            bid_px,
            ask_px,
            // The venue publishes a price at a size, not a resting quantity.
            // Zero says "unknown", which is what it is; a made-up depth would
            // be read as a real one by anything sizing against it.
            bid_qty: 0.0,
            ask_qty: 0.0,
            venue_ts_ms,
            recv_ns,
            seq: 0,
        })
    }

    pub fn ticker_state(&self, venue_ts_ms: i64, recv_ns: u64) -> Ticker {
        Ticker {
            last_px: self.mark_px,
            mark_px: self.mark_px,
            // No index price is published. The mark is the only price the
            // venue states, and copying it into `index_px` would invent an
            // agreement between two numbers that only one of them exists for.
            index_px: 0.0,
            funding_rate: self.funding_rate,
            // The venue gives an interval, not a next-settlement stamp, and
            // the two are not the same thing. Zero is "not stated".
            next_funding_ms: 0,
            venue_ts_ms,
            recv_ns,
        }
    }
}

/// Every listing out of one `/metadata/stats` reply.
pub fn parse_stats(stats: &Value) -> Result<Vec<Listing>, VenueError> {
    let rows = stats
        .get("listings")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("the stats reply carries no listings".to_string()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let quotes = row.get("quotes");
        let (bid_px, ask_px) = smallest_quote(quotes);
        out.push(Listing {
            ticker: str_field(row, "ticker")?,
            mark_px: num_field(row, "mark_price")?,
            funding_rate: num_field(row, "funding_rate")?,
            funding_interval_s: row
                .get("funding_interval_s")
                .and_then(Value::as_i64)
                .unwrap_or(0),
            bid_px,
            ask_px,
        });
    }
    Ok(out)
}

/// The tightest published size's two-way price. Bigger sizes are wider, and a
/// wider quote read as the touch would make every spread look worse than the
/// venue's own smallest ticket.
fn smallest_quote(quotes: Option<&Value>) -> (Option<f64>, Option<f64>) {
    let Some(quotes) = quotes else { return (None, None) };
    for size in ["size_1k", "size_100k", "size_1m"] {
        let Some(at_size) = quotes.get(size) else { continue };
        let bid = num_field(at_size, "bid").ok();
        let ask = num_field(at_size, "ask").ok();
        if bid.is_some() && ask.is_some() {
            return (bid, ask);
        }
    }
    (None, None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn stats() -> Value {
        json!({
            "total_volume_24h": "1000000",
            "num_markets": 2,
            "listings": [
                {"ticker": "BTC", "name": "Bitcoin", "mark_price": "95000.5",
                 "volume_24h": "500000", "funding_rate": "0.0001",
                 "funding_interval_s": 3600,
                 "quotes": {"updated_at": "2026-08-22T00:00:00Z",
                            "size_1k": {"bid": "94990", "ask": "95010"},
                            "size_100k": {"bid": "94900", "ask": "95100"}}},
                {"ticker": "XAU", "name": "Gold", "mark_price": "2400",
                 "volume_24h": "1000", "funding_rate": "-0.00005",
                 "funding_interval_s": 28800}
            ]
        })
    }

    #[test]
    fn every_listing_is_read_under_the_engines_symbol() {
        let rows = parse_stats(&stats()).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].symbol(), "BTCUSDT");
        assert_eq!(rows[0].mark_px, 95000.5);
        assert_eq!(rows[0].funding_rate, 0.0001);
        assert_eq!(rows[0].funding_interval_s, 3600);
        // A real-world asset listing reads the same way as a crypto one.
        assert_eq!(rows[1].symbol(), "XAUUSDT");
        assert_eq!(rows[1].funding_rate, -0.00005);
    }

    #[test]
    fn the_tightest_published_size_is_the_quote() {
        // The 100k quote is wider. Reading it as the touch would make every
        // spread measured from this feed look worse than the venue's own
        // smallest ticket.
        let rows = parse_stats(&stats()).unwrap();
        let quote = rows[0].quote(1700, 42).unwrap();
        assert_eq!(quote.bid_px, 94990.0);
        assert_eq!(quote.ask_px, 95010.0);
        assert_eq!(quote.venue_ts_ms, 1700);
    }

    #[test]
    fn a_listing_with_no_quote_has_no_quote_rather_than_a_zero_one() {
        // A zeroed quote would read as a real bid of nothing, and anything
        // pricing against it would cross the whole book.
        let rows = parse_stats(&stats()).unwrap();
        assert!(rows[1].quote(1700, 42).is_none());
    }

    #[test]
    fn depth_is_zero_because_the_venue_publishes_none() {
        let rows = parse_stats(&stats()).unwrap();
        let quote = rows[0].quote(1700, 42).unwrap();
        assert_eq!(quote.bid_qty, 0.0);
        assert_eq!(quote.ask_qty, 0.0);
    }

    #[test]
    fn the_ticker_states_only_the_prices_the_venue_states() {
        let rows = parse_stats(&stats()).unwrap();
        let ticker = rows[0].ticker_state(1700, 42);
        assert_eq!(ticker.mark_px, 95000.5);
        assert_eq!(ticker.last_px, 95000.5);
        assert_eq!(ticker.index_px, 0.0, "no index price is published");
        assert_eq!(ticker.next_funding_ms, 0, "an interval is not a settlement time");
    }

    #[test]
    fn a_reply_that_is_not_a_stats_reply_is_an_error() {
        assert!(parse_stats(&json!({"error": "nope"})).is_err());
        assert!(parse_stats(&json!({"listings": "not a list"})).is_err());
    }
}
