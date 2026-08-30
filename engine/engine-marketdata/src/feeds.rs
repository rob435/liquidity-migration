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

use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Subscription, SymbolId};
use engine_venue::{
    BinanceRealm, HyperliquidRealm, LighterRealm, MexcRealm, VariationalRealm, VenueName,
};

use crate::binance::BinancePublicFeed;
use crate::bybit::feed::BybitPublicFeed;
use crate::hyperliquid::HyperliquidPublicFeed;
use crate::lighter::LighterPublicFeed;
use crate::mexc::MexcPublicFeed;
use crate::variational::VariationalPublicFeed;

pub enum MarketFeeds {
    Bybit(BybitPublicFeed),
    Hyperliquid(HyperliquidPublicFeed),
    Lighter(LighterPublicFeed),
    Mexc(MexcPublicFeed),
    Binance(BinancePublicFeed),
    Variational(VariationalPublicFeed),
}

impl MarketFeeds {
    /// The public feed for the venue this name selects.
    pub fn build(name: VenueName, subs: &[Subscription]) -> Self {
        match name {
            // Bybit publishes one public stream for both realms; the demo
            // account matches against these same prices.
            VenueName::BybitDemo | VenueName::BybitMainnet => {
                MarketFeeds::Bybit(BybitPublicFeed::new(subs))
            }
            VenueName::HyperliquidTestnet => MarketFeeds::Hyperliquid(HyperliquidPublicFeed::new(
                HyperliquidRealm::Testnet,
                subs,
            )),
            VenueName::HyperliquidMainnet => MarketFeeds::Hyperliquid(HyperliquidPublicFeed::new(
                HyperliquidRealm::Mainnet,
                subs,
            )),
            VenueName::LighterTestnet => {
                MarketFeeds::Lighter(LighterPublicFeed::new(LighterRealm::Testnet, subs))
            }
            VenueName::LighterMainnet => {
                MarketFeeds::Lighter(LighterPublicFeed::new(LighterRealm::Mainnet, subs))
            }
            VenueName::MexcMainnet => {
                MarketFeeds::Mexc(MexcPublicFeed::new(MexcRealm::Mainnet, subs))
            }
            VenueName::BinanceTestnet => {
                MarketFeeds::Binance(BinancePublicFeed::new(BinanceRealm::Testnet, subs))
            }
            VenueName::BinanceMainnet => {
                MarketFeeds::Binance(BinancePublicFeed::new(BinanceRealm::Mainnet, subs))
            }
            VenueName::VariationalMainnet => MarketFeeds::Variational(VariationalPublicFeed::new(
                VariationalRealm::Mainnet,
                subs,
            )),
        }
    }

    /// The id this feed hands out for a symbol, if it follows it.
    ///
    /// The engine's own table and the feed's have to agree position for
    /// position: a `SymbolId` from a quote is used as a core id directly, with
    /// nothing translating between them. This is what a test can check that
    /// agreement with.
    pub fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        match self {
            MarketFeeds::Bybit(feed) => feed.symbols().get(symbol),
            MarketFeeds::Hyperliquid(feed) => feed.id_of(symbol),
            MarketFeeds::Lighter(feed) => feed.id_of(symbol),
            MarketFeeds::Mexc(feed) => feed.id_of(symbol),
            MarketFeeds::Binance(feed) => feed.id_of(symbol),
            MarketFeeds::Variational(feed) => feed.id_of(symbol),
        }
    }
}

impl MarketFeed for MarketFeeds {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        match self {
            MarketFeeds::Bybit(feed) => feed.next_event().await,
            MarketFeeds::Hyperliquid(feed) => feed.next_event().await,
            MarketFeeds::Lighter(feed) => feed.next_event().await,
            MarketFeeds::Mexc(feed) => feed.next_event().await,
            MarketFeeds::Binance(feed) => feed.next_event().await,
            MarketFeeds::Variational(feed) => feed.next_event().await,
        }
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        match self {
            MarketFeeds::Bybit(inner) => MarketFeed::admit(inner, symbol, feed),
            MarketFeeds::Hyperliquid(inner) => MarketFeed::admit(inner, symbol, feed),
            MarketFeeds::Lighter(inner) => MarketFeed::admit(inner, symbol, feed),
            MarketFeeds::Mexc(inner) => MarketFeed::admit(inner, symbol, feed),
            MarketFeeds::Binance(inner) => MarketFeed::admit(inner, symbol, feed),
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
            let built = match MarketFeeds::build(name, &subs()) {
                MarketFeeds::Bybit(_) => "bybit",
                MarketFeeds::Hyperliquid(_) => "hyperliquid",
                MarketFeeds::Lighter(_) => "lighter",
                MarketFeeds::Mexc(_) => "mexc",
                MarketFeeds::Binance(_) => "binance",
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
            let mut feed = MarketFeeds::build(name, &subs());
            assert_eq!(feed.id_of("BTCUSDT"), Some(SymbolId(0)), "{name}");
            assert_eq!(
                MarketFeed::admit(&mut feed, "ETHUSDT", Feed::Quote),
                Some(SymbolId(1)),
                "{name}"
            );
            assert_eq!(feed.id_of("ETHUSDT"), Some(SymbolId(1)), "{name}");
        }
    }
}
