//! Who hears what: symbol and feed in, strategies out.
//!
//! Built once at boot from the subscriptions, then read-only, so a market
//! message costs one index and a short walk.

use engine_types::{Feed, StrategyId, SymbolId};

#[derive(Default, Debug)]
pub struct Routing {
    quote: Vec<Vec<StrategyId>>,
    ticker: Vec<Vec<StrategyId>>,
}

impl Routing {
    pub fn add(&mut self, symbol: SymbolId, feed: Feed, strategy: StrategyId) {
        let index = symbol.0 as usize;
        let list = match feed {
            Feed::Quote => &mut self.quote,
            Feed::Ticker => &mut self.ticker,
        };
        if list.len() <= index {
            list.resize(index + 1, Vec::new());
        }
        if !list[index].contains(&strategy) {
            list[index].push(strategy);
        }
    }

    pub fn size_to(&mut self, symbols: usize) {
        self.quote.resize(symbols, Vec::new());
        self.ticker.resize(symbols, Vec::new());
    }

    pub fn quote_listeners(&self, symbol: SymbolId) -> &[StrategyId] {
        self.quote
            .get(symbol.0 as usize)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
    }

    pub fn ticker_listeners(&self, symbol: SymbolId) -> &[StrategyId] {
        self.ticker
            .get(symbol.0 as usize)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
    }

    /// Everyone watching this symbol on any feed, each named once.
    pub fn all_listeners(&self, symbol: SymbolId) -> Vec<StrategyId> {
        let mut out = self.quote_listeners(symbol).to_vec();
        for sid in self.ticker_listeners(symbol) {
            if !out.contains(sid) {
                out.push(*sid);
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_feed_has_its_own_listeners() {
        let mut routing = Routing::default();
        routing.add(SymbolId(0), Feed::Quote, StrategyId(0));
        routing.add(SymbolId(0), Feed::Quote, StrategyId(0));
        routing.add(SymbolId(0), Feed::Ticker, StrategyId(1));
        routing.add(SymbolId(2), Feed::Quote, StrategyId(1));
        routing.size_to(3);
        assert_eq!(routing.quote_listeners(SymbolId(0)), &[StrategyId(0)]);
        assert_eq!(routing.ticker_listeners(SymbolId(0)), &[StrategyId(1)]);
        assert!(routing.quote_listeners(SymbolId(1)).is_empty());
        assert_eq!(
            routing.all_listeners(SymbolId(0)),
            vec![StrategyId(0), StrategyId(1)]
        );
    }
}
