//! The chosen venue's public market feed, behind one type.
//!
//! Same reasoning as the gateway's registry: `async fn` in trait cannot be a
//! trait object as written, and a match arm per venue is a forwarding the
//! compiler checks. And the same reasoning as the switch itself: this is built
//! from the venue name the gateway was built from, so the engine cannot send
//! orders to one venue while pricing them off another's book.
//!
//! This feed is polled once per loop turn, hundreds of times a second, inside
//! the segment the engine reports in nanoseconds — a different budget from the
//! gateway, which is called once per order.

#[cfg(feature = "binance")]
use engine_public::BinanceRealm;
#[cfg(feature = "hyperliquid")]
use engine_public::HyperliquidRealm;
#[cfg(feature = "lighter")]
use engine_public::LighterRealm;
#[cfg(feature = "mexc")]
use engine_public::MexcRealm;
#[cfg(feature = "variational")]
use engine_public::VariationalRealm;
use engine_public::VenueName;
use engine_types::VenueError;

use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Subscription, SymbolId};

#[cfg(feature = "binance")]
use crate::binance::BinancePublicFeed;
#[cfg(feature = "bybit")]
use crate::bybit::feed::BybitPublicFeed;
#[cfg(feature = "hyperliquid")]
use crate::hyperliquid::HyperliquidPublicFeed;
#[cfg(feature = "lighter")]
use crate::lighter::LighterPublicFeed;
#[cfg(feature = "mexc")]
use crate::mexc::MexcPublicFeed;
#[cfg(feature = "variational")]
use crate::variational::VariationalPublicFeed;

// Built once at boot; keeping the feeds inline preserves static dispatch.
#[allow(clippy::large_enum_variant)]
pub enum MarketFeeds {
    #[cfg(feature = "bybit")]
    Bybit(BybitPublicFeed),
    #[cfg(feature = "hyperliquid")]
    Hyperliquid(HyperliquidPublicFeed),
    #[cfg(feature = "lighter")]
    Lighter(LighterPublicFeed),
    #[cfg(feature = "mexc")]
    Mexc(MexcPublicFeed),
    #[cfg(feature = "binance")]
    Binance(BinancePublicFeed),
    #[cfg(feature = "variational")]
    Variational(VariationalPublicFeed),
}

impl MarketFeeds {
    /// The public feed for the venue this name selects.
    pub fn build(name: VenueName, subs: &[Subscription]) -> Result<Self, VenueError> {
        #[allow(unreachable_patterns)]
        Ok(match name {
            // Bybit publishes one public stream for both realms; the demo
            // account matches against these same prices.
            #[cfg(feature = "bybit")]
            VenueName::BybitDemo | VenueName::BybitMainnet => {
                MarketFeeds::Bybit(BybitPublicFeed::new(subs))
            }
            #[cfg(feature = "hyperliquid")]
            VenueName::HyperliquidTestnet => MarketFeeds::Hyperliquid(HyperliquidPublicFeed::new(
                HyperliquidRealm::Testnet,
                subs,
            )),
            #[cfg(feature = "hyperliquid")]
            VenueName::HyperliquidMainnet => MarketFeeds::Hyperliquid(HyperliquidPublicFeed::new(
                HyperliquidRealm::Mainnet,
                subs,
            )),
            #[cfg(feature = "lighter")]
            VenueName::LighterTestnet => {
                MarketFeeds::Lighter(LighterPublicFeed::new(LighterRealm::Testnet, subs))
            }
            #[cfg(feature = "lighter")]
            VenueName::LighterMainnet => {
                MarketFeeds::Lighter(LighterPublicFeed::new(LighterRealm::Mainnet, subs))
            }
            #[cfg(feature = "mexc")]
            VenueName::MexcMainnet => {
                MarketFeeds::Mexc(MexcPublicFeed::new(MexcRealm::Mainnet, subs))
            }
            #[cfg(feature = "binance")]
            VenueName::BinanceTestnet => {
                MarketFeeds::Binance(BinancePublicFeed::new(BinanceRealm::Testnet, subs))
            }
            #[cfg(feature = "binance")]
            VenueName::BinanceMainnet => {
                MarketFeeds::Binance(BinancePublicFeed::new(BinanceRealm::Mainnet, subs))
            }
            #[cfg(feature = "variational")]
            VenueName::VariationalMainnet => MarketFeeds::Variational(VariationalPublicFeed::new(
                VariationalRealm::Mainnet,
                subs,
            )),
            other => return Err(other.disabled_error()),
        })
    }

    /// The id this feed hands out for a symbol, if it follows it.
    ///
    /// The engine's own table and the feed's have to agree position for
    /// position: a `SymbolId` from a quote is used as a core id directly, with
    /// nothing translating between them. This is what a test can check that
    /// agreement with.
    pub fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        match self {
            #[cfg(feature = "bybit")]
            MarketFeeds::Bybit(feed) => feed.symbols().get(symbol),
            #[cfg(feature = "hyperliquid")]
            MarketFeeds::Hyperliquid(feed) => feed.id_of(symbol),
            #[cfg(feature = "lighter")]
            MarketFeeds::Lighter(feed) => feed.id_of(symbol),
            #[cfg(feature = "mexc")]
            MarketFeeds::Mexc(feed) => feed.id_of(symbol),
            #[cfg(feature = "binance")]
            MarketFeeds::Binance(feed) => feed.id_of(symbol),
            #[cfg(feature = "variational")]
            MarketFeeds::Variational(feed) => feed.id_of(symbol),
        }
    }
}

impl MarketFeed for MarketFeeds {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        match self {
            #[cfg(feature = "bybit")]
            MarketFeeds::Bybit(feed) => feed.next_event().await,
            #[cfg(feature = "hyperliquid")]
            MarketFeeds::Hyperliquid(feed) => feed.next_event().await,
            #[cfg(feature = "lighter")]
            MarketFeeds::Lighter(feed) => feed.next_event().await,
            #[cfg(feature = "mexc")]
            MarketFeeds::Mexc(feed) => feed.next_event().await,
            #[cfg(feature = "binance")]
            MarketFeeds::Binance(feed) => feed.next_event().await,
            #[cfg(feature = "variational")]
            MarketFeeds::Variational(feed) => feed.next_event().await,
        }
    }

    fn retire(&mut self, symbol: &str, feed: Feed) -> bool {
        match self {
            #[cfg(feature = "bybit")]
            MarketFeeds::Bybit(inner) => inner.retire(symbol, feed),
            #[cfg(feature = "hyperliquid")]
            MarketFeeds::Hyperliquid(inner) => inner.retire(symbol, feed),
            #[cfg(feature = "lighter")]
            MarketFeeds::Lighter(inner) => inner.retire(symbol, feed),
            #[cfg(feature = "mexc")]
            MarketFeeds::Mexc(inner) => inner.retire(symbol, feed),
            #[cfg(feature = "binance")]
            MarketFeeds::Binance(inner) => inner.retire(symbol, feed),
            #[cfg(feature = "variational")]
            MarketFeeds::Variational(inner) => inner.retire(symbol, feed),
        }
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        match self {
            #[cfg(feature = "bybit")]
            MarketFeeds::Bybit(inner) => MarketFeed::admit(inner, symbol, feed),
            #[cfg(feature = "hyperliquid")]
            MarketFeeds::Hyperliquid(inner) => MarketFeed::admit(inner, symbol, feed),
            #[cfg(feature = "lighter")]
            MarketFeeds::Lighter(inner) => MarketFeed::admit(inner, symbol, feed),
            #[cfg(feature = "mexc")]
            MarketFeeds::Mexc(inner) => MarketFeed::admit(inner, symbol, feed),
            #[cfg(feature = "binance")]
            MarketFeeds::Binance(inner) => MarketFeed::admit(inner, symbol, feed),
            #[cfg(feature = "variational")]
            MarketFeeds::Variational(inner) => MarketFeed::admit(inner, symbol, feed),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn subs() -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    #[test]
    fn every_venue_name_builds_its_own_feed() {
        // A name that fell through to another venue's feed would be an engine
        // pricing one venue's orders off another venue's book — the exact
        // thing one switch exists to make impossible.
        //
        // The inner match is exhaustive on purpose: a new feed variant does
        // not compile until this test says which name reaches it. A table of
        // booleans let the last venue pass by elimination instead.
        for (name, expected) in [
            (VenueName::BybitDemo, "bybit"),
            (VenueName::BybitMainnet, "bybit"),
            (VenueName::HyperliquidTestnet, "hyperliquid"),
            (VenueName::HyperliquidMainnet, "hyperliquid"),
            (VenueName::LighterTestnet, "lighter"),
            (VenueName::LighterMainnet, "lighter"),
            (VenueName::MexcMainnet, "mexc"),
            (VenueName::BinanceTestnet, "binance"),
            (VenueName::BinanceMainnet, "binance"),
            (VenueName::VariationalMainnet, "variational"),
        ] {
            if !name.compiled() {
                continue;
            }
            let built = match MarketFeeds::build(name, &subs()).unwrap() {
                #[cfg(feature = "bybit")]
                MarketFeeds::Bybit(_) => "bybit",
                #[cfg(feature = "hyperliquid")]
                MarketFeeds::Hyperliquid(_) => "hyperliquid",
                #[cfg(feature = "lighter")]
                MarketFeeds::Lighter(_) => "lighter",
                #[cfg(feature = "mexc")]
                MarketFeeds::Mexc(_) => "mexc",
                #[cfg(feature = "binance")]
                MarketFeeds::Binance(_) => "binance",
                #[cfg(feature = "variational")]
                MarketFeeds::Variational(_) => "variational",
            };
            assert_eq!(built, expected, "{name} built the wrong venue's feed");
        }
    }

    #[test]
    fn a_feed_hands_out_ids_for_the_symbols_it_was_built_with() {
        for name in [
            VenueName::BybitDemo,
            VenueName::HyperliquidTestnet,
            VenueName::LighterTestnet,
            VenueName::BinanceTestnet,
            VenueName::VariationalMainnet,
        ] {
            if !name.compiled() {
                continue;
            }
            let mut feed = MarketFeeds::build(name, &subs()).unwrap();
            assert_eq!(feed.id_of("BTCUSDT"), Some(SymbolId(0)), "{name}");
            assert_eq!(
                MarketFeed::admit(&mut feed, "ETHUSDT", Feed::Quote),
                Some(SymbolId(1)),
                "{name}"
            );
            assert_eq!(feed.id_of("ETHUSDT"), Some(SymbolId(1)), "{name}");
        }
    }
    #[test]
    fn every_feed_retires_only_the_requested_demand_and_preserves_symbol_ids() {
        let mut failures = Vec::new();
        for name in [
            VenueName::BybitDemo,
            VenueName::HyperliquidTestnet,
            VenueName::LighterTestnet,
            VenueName::MexcMainnet,
            VenueName::BinanceTestnet,
            VenueName::VariationalMainnet,
        ] {
            if !name.compiled() {
                continue;
            }
            let mut feed = MarketFeeds::build(name, &subs()).unwrap();
            for _ in 0..64 {
                assert_eq!(feed.admit("BTCUSDT", Feed::Quote), Some(SymbolId(0)));
            }
            assert_eq!(feed.admit("BTCUSDT", Feed::Ticker), Some(SymbolId(0)));
            assert_eq!(feed.admit("ETHUSDT", Feed::Quote), Some(SymbolId(1)));
            let actual = [
                feed.retire("BTCUSDT", Feed::Quote),
                feed.retire("BTCUSDT", Feed::Quote),
                feed.retire("BTCUSDT", Feed::Ticker),
                feed.retire("BTCUSDT", Feed::Ticker),
            ];
            if actual != [true, false, true, false] {
                failures.push(format!("{name}: {actual:?}"));
            }
            assert_eq!(feed.id_of("ETHUSDT"), Some(SymbolId(1)));
            assert_eq!(feed.admit("NEWUSDT", Feed::Quote), Some(SymbolId(2)));
            assert_eq!(feed.admit("BTCUSDT", Feed::Quote), Some(SymbolId(0)));
        }
        assert!(failures.is_empty(), "retirement failures: {failures:?}");
    }
}
