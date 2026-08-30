//! Markets, and the integers this venue's transactions carry.
//!
//! **Symbols.** The venue names a market `BTC` and addresses it by a market
//! index. The engine's tables and the research system's books name it
//! `BTCUSDT`, so the mapping is one function here, the same shape as
//! Hyperliquid's.
//!
//! **Numbers are integers, not decimals.** A Lighter order carries `Price` as
//! a `uint32` and `BaseAmount` as an `int64`, both in the market's own units:
//! a price of 2.5 in a market with four price decimals is the integer 25000.
//! That conversion happens here and nowhere else, because the signature covers
//! the integer — a rounding applied after signing is a different order than
//! the one that was signed.

use std::collections::HashMap;

use engine_types::orders::InstrumentRule;
use engine_types::quantize::shave_dust;
use engine_types::{Side, Symbol, VenueError};

const QUOTE_SUFFIXES: &[&str] = &["USDT", "USDC", "USD"];

/// One tradable market as the venue lists it.
#[derive(Clone, Debug, PartialEq)]
pub struct Market {
    /// The venue's own name, e.g. `BTC`.
    pub symbol: String,
    /// What goes in a transaction's `MarketIndex`.
    pub index: i16,
    pub size_decimals: u32,
    pub price_decimals: u32,
    /// The venue's minimums, in ordinary decimal units.
    pub min_base_amount: f64,
    pub min_quote_amount: f64,
}

#[derive(Clone, Debug, Default)]
pub struct Markets {
    by_symbol: HashMap<String, Market>,
}

impl Markets {
    /// Keyed upper-case: a book's symbol arrives upper-cased from every other
    /// part of the engine, and a venue that spells a market with any lower
    /// case would otherwise be published as tradable and then refuse every
    /// order for it. [`Market::symbol`] keeps the venue's own spelling.
    pub fn from_rows(rows: Vec<Market>) -> Self {
        Markets {
            by_symbol: rows
                .into_iter()
                .map(|m| (m.symbol.to_ascii_uppercase(), m))
                .collect(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.by_symbol.is_empty()
    }

    /// The market a symbol names. Never a guess: a wrong market index is an
    /// order on somebody else's contract.
    pub fn for_symbol(&self, symbol: &str) -> Result<&Market, VenueError> {
        let name = venue_symbol(symbol);
        self.by_symbol.get(&name).ok_or_else(|| {
            VenueError::BadRequest(format!(
                "this venue does not list {symbol} (looked for the market {name})"
            ))
        })
    }

    pub fn by_index(&self, index: i16) -> Option<&Market> {
        self.by_symbol.values().find(|m| m.index == index)
    }

    pub fn instrument_rules(&self) -> Vec<(Symbol, InstrumentRule)> {
        let mut out: Vec<(Symbol, InstrumentRule)> = self
            .by_symbol
            .values()
            .map(|market| (engine_symbol(&market.symbol), instrument_rule(market)))
            .collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }
}

/// `BTCUSDT` -> `BTC`.
pub fn venue_symbol(symbol: &str) -> String {
    let upper = symbol.trim().to_ascii_uppercase();
    for suffix in QUOTE_SUFFIXES {
        if let Some(head) = upper.strip_suffix(suffix) {
            if !head.is_empty() {
                return head.to_string();
            }
        }
    }
    upper
}

/// `BTC` -> `BTCUSDT`, the engine's spelling.
pub fn engine_symbol(symbol: &str) -> Symbol {
    format!("{}USDT", symbol.trim().to_ascii_uppercase())
}

pub fn instrument_rule(market: &Market) -> InstrumentRule {
    InstrumentRule {
        tick_size: 10f64.powi(-(market.price_decimals as i32)),
        qty_step: 10f64.powi(-(market.size_decimals as i32)),
        min_qty: market.min_base_amount,
        min_notional: market.min_quote_amount,
    }
}

/// A price as the integer the transaction carries, rounded toward the passive
/// side so quantization never makes an order more aggressive than asked.
pub fn venue_price(px: f64, side: Side, market: &Market) -> Result<u32, VenueError> {
    if !px.is_finite() || px <= 0.0 {
        return Err(VenueError::BadRequest(format!(
            "{px} is not a positive finite price"
        )));
    }
    let scale = 10f64.powi(market.price_decimals as i32);
    let scaled = shave_dust(px * scale);
    let stepped = match side {
        Side::Buy => scaled.floor(),
        Side::Sell => scaled.ceil(),
    };
    if stepped < 1.0 {
        return Err(VenueError::BadRequest(format!(
            "{px} rounds away to nothing at {} price decimals",
            market.price_decimals
        )));
    }
    if stepped > u32::MAX as f64 {
        return Err(VenueError::BadRequest(format!(
            "{px} does not fit this venue's 32-bit price field"
        )));
    }
    Ok(stepped as u32)
}

/// A size as the integer the transaction carries, rounded DOWN — never up,
/// which would send more risk than the kernel approved.
pub fn venue_size(qty: f64, market: &Market) -> Result<i64, VenueError> {
    if !qty.is_finite() || qty <= 0.0 {
        return Err(VenueError::BadRequest(format!(
            "{qty} is not a positive finite size"
        )));
    }
    let scale = 10f64.powi(market.size_decimals as i32);
    let stepped = shave_dust(qty * scale).floor();
    if stepped < 1.0 {
        return Err(VenueError::BadRequest(format!(
            "{qty} is below this market's smallest tradable size"
        )));
    }
    // The venue's own ceiling on a base amount.
    if stepped > ((1i64 << 48) - 1) as f64 {
        return Err(VenueError::BadRequest(format!(
            "{qty} does not fit this venue's base-amount field"
        )));
    }
    Ok(stepped as i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn market() -> Market {
        Market {
            symbol: "BTC".to_string(),
            index: 0,
            size_decimals: 5,
            price_decimals: 1,
            min_base_amount: 0.0001,
            min_quote_amount: 10.0,
        }
    }

    #[test]
    fn a_book_symbol_finds_the_venues_market_and_back() {
        assert_eq!(venue_symbol("BTCUSDT"), "BTC");
        assert_eq!(venue_symbol("1000PEPEUSDT"), "1000PEPE");
        assert_eq!(engine_symbol("BTC"), "BTCUSDT");
        for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"] {
            assert_eq!(engine_symbol(&venue_symbol(symbol)), symbol);
        }
    }

    #[test]
    fn every_market_the_rules_advertise_can_be_looked_up_again() {
        let markets = Markets::from_rows(vec![
            Market {
                symbol: "kPEPE".to_string(),
                index: 7,
                ..market()
            },
            market(),
        ]);
        for (symbol, _) in markets.instrument_rules() {
            assert!(
                markets.for_symbol(&symbol).is_ok(),
                "{symbol} is advertised and unreachable"
            );
        }
        assert_eq!(markets.for_symbol("KPEPEUSDT").unwrap().index, 7);
    }

    #[test]
    fn an_unlisted_symbol_is_an_error_and_never_market_zero() {
        // Market indices are the venue's; a missing symbol falling back to a
        // default would trade whatever market happens to be numbered zero.
        let markets = Markets::from_rows(vec![market()]);
        assert_eq!(markets.for_symbol("BTCUSDT").unwrap().index, 0);
        let missing = markets.for_symbol("DOGEUSDT").unwrap_err();
        assert!(missing.to_string().contains("DOGE"), "{missing}");
    }

    #[test]
    fn a_price_and_a_size_already_on_the_venues_grid_are_left_where_they_are() {
        // `2.9 * 10` is 28.999999999999996, so scaling and flooring without
        // shaving the dust sends a buy one tick below where the strategy put
        // it, and a size one step under what the risk kernel approved.
        let m = Market {
            size_decimals: 2,
            price_decimals: 1,
            ..market()
        };
        assert_eq!(venue_price(2.9, Side::Buy, &m).unwrap(), 29);
        assert_eq!(venue_price(2.9, Side::Sell, &m).unwrap(), 29);
        assert_eq!(venue_size(0.29, &m).unwrap(), 29);
        assert_eq!(venue_size(1.19, &m).unwrap(), 119);
        // And a real fraction of a tick still rounds the passive way.
        assert_eq!(venue_price(2.95, Side::Buy, &m).unwrap(), 29);
        assert_eq!(venue_price(2.95, Side::Sell, &m).unwrap(), 30);
        assert_eq!(venue_size(0.295, &m).unwrap(), 29);
    }

    #[test]
    fn a_price_becomes_the_venues_integer_rounded_toward_the_passive_side() {
        let m = market();
        assert_eq!(venue_price(95_000.15, Side::Buy, &m).unwrap(), 950_001);
        assert_eq!(venue_price(95_000.15, Side::Sell, &m).unwrap(), 950_002);
        assert_eq!(venue_price(1.0, Side::Buy, &m).unwrap(), 10);
        // A buy never rounds up and a sell never rounds down.
        for px in [1.04, 12.345, 99_999.99] {
            let buy = venue_price(px, Side::Buy, &m).unwrap() as f64 / 10.0;
            let sell = venue_price(px, Side::Sell, &m).unwrap() as f64 / 10.0;
            assert!(buy <= px + 1e-9, "a buy at {px} rounded up to {buy}");
            assert!(sell >= px - 1e-9, "a sell at {px} rounded down to {sell}");
        }
    }

    #[test]
    fn a_size_rounds_down_and_never_up() {
        let m = market();
        assert_eq!(venue_size(0.123456, &m).unwrap(), 12345);
        assert_eq!(venue_size(1.0, &m).unwrap(), 100_000);
        assert!(
            venue_size(0.000001, &m).is_err(),
            "a size that rounds to nothing"
        );
    }

    #[test]
    fn nothing_unsendable_gets_through() {
        let m = market();
        for bad in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert!(venue_price(bad, Side::Buy, &m).is_err(), "{bad} as a price");
            assert!(venue_size(bad, &m).is_err(), "{bad} as a size");
        }
        // And a price too big for the venue's 32-bit field is refused rather
        // than wrapped into a different price.
        assert!(venue_price(1e9, Side::Buy, &m).is_err());
    }

    #[test]
    fn the_instrument_rule_states_the_venues_own_minimums() {
        let rule = instrument_rule(&market());
        assert!((rule.tick_size - 0.1).abs() < 1e-12);
        assert!((rule.qty_step - 1e-5).abs() < 1e-15);
        assert_eq!(rule.min_qty, 0.0001);
        assert_eq!(rule.min_notional, 10.0);
    }
}
