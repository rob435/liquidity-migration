//! Symbols, asset numbers, and the venue's rules about how a number may be
//! written.
//!
//! **Symbols.** The engine's tables and the research system's target books
//! name a perpetual `BTCUSDT`, Bybit's spelling, and that spelling is what a
//! book written for one venue has to keep to stay readable on another.
//! Hyperliquid names the same contract `BTC` and addresses it on the wire by
//! its position in the venue's asset list. So the mapping is one function,
//! here: strip the quote currency to get the coin, then look the coin up.
//!
//! **Numbers.** Hyperliquid refuses a price that carries too much precision
//! rather than rounding it, and the rule is not a tick size: a price may have
//! at most five significant figures, *and* at most `6 - szDecimals` decimal
//! places, except that a whole number is always allowed. A single tick cannot
//! say that, so [`instrument_rule`] reports the decimal-places half — the
//! finest tick that is ever legal — and [`venue_px`] applies the significant
//! figures on the way out.
//!
//! Both roundings go the same way the engine's own quantizer goes: buys down,
//! sells up. A price that moves is never made more aggressive than the
//! strategy asked for, so a post-only order cannot be rounded into crossing.

use std::collections::HashMap;

use engine_types::orders::InstrumentRule;
use engine_types::quantize::shave_dust;
use engine_types::{Side, Symbol, VenueError};

/// What the venue enforces as a minimum order value. Below this the order is
/// refused, so it is reported as the instrument's minimum notional and the
/// engine's own check catches it before a round trip.
const MIN_ORDER_NOTIONAL_USD: f64 = 10.0;

/// Perp prices may carry this many significant figures. A whole number is
/// exempt, which is what lets BTC trade at 95_000.
const MAX_SIGNIFICANT_FIGURES: i32 = 5;

/// Perp price decimals are this less the asset's size decimals.
const PRICE_DECIMAL_BUDGET: i32 = 6;

/// The quote currencies a book's symbol may end in. Hyperliquid perps are all
/// USD-margined, so this is only ever stripped, never used.
const QUOTE_SUFFIXES: &[&str] = &["USDT", "USDC", "USD"];

/// One tradable asset as the venue lists it.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Asset {
    pub(crate) coin: String,
    /// Position in the venue's asset list. This is the number that goes on the
    /// wire, and it is assigned by the venue, not by us.
    pub(crate) index: u32,
    pub(crate) sz_decimals: u32,
    pub(crate) max_leverage: f64,
}

/// The venue's asset list, keyed both ways.
#[derive(Clone, Debug, Default)]
pub(crate) struct Assets {
    by_coin: HashMap<String, Asset>,
}

impl Assets {
    /// Keyed upper-case, because the venue's own spelling is not: `kPEPE`,
    /// `kBONK`, `kSHIB` and the rest carry a lower-case prefix, while a book's
    /// symbol arrives upper-cased from every other part of the engine. Keying
    /// by the venue's spelling published those assets as tradable and then
    /// refused every order for them. [`Asset::coin`] keeps the venue's
    /// spelling; only the key is folded.
    pub(crate) fn from_rows(rows: Vec<Asset>) -> Self {
        Assets {
            by_coin: rows
                .into_iter()
                .map(|a| (a.coin.to_ascii_uppercase(), a))
                .collect(),
        }
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.by_coin.is_empty()
    }

    /// The asset a symbol names, or an error saying the venue does not list
    /// it. Never a guess: an order on the wrong asset number is an order on
    /// somebody else's contract.
    pub(crate) fn for_symbol(&self, symbol: &str) -> Result<&Asset, VenueError> {
        let coin = coin_of(symbol);
        self.by_coin.get(&coin).ok_or_else(|| {
            VenueError::BadRequest(format!(
                "this venue does not list {symbol} (looked for the coin {coin})"
            ))
        })
    }

    /// Every listed asset as an instrument rule, under the engine's spelling
    /// of the symbol.
    pub(crate) fn instrument_rules(&self) -> Vec<(Symbol, InstrumentRule)> {
        let mut out: Vec<(Symbol, InstrumentRule)> = self
            .by_coin
            .values()
            .map(|asset| (symbol_of(&asset.coin), instrument_rule(asset)))
            .collect();
        // A stable order, so two reads of the same venue produce the same list
        // and a diff of them means something changed at the venue.
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }
}

/// `BTCUSDT` -> `BTC`. A symbol that names no quote currency is already a coin.
pub(crate) fn coin_of(symbol: &str) -> String {
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

/// `BTC` -> `BTCUSDT`. The engine's spelling, so one target book reads the
/// same on every venue.
pub(crate) fn symbol_of(coin: &str) -> Symbol {
    format!("{}USDT", coin.trim().to_ascii_uppercase())
}

pub(crate) fn instrument_rule(asset: &Asset) -> InstrumentRule {
    let step = 10f64.powi(-(asset.sz_decimals as i32));
    InstrumentRule {
        // The finest legal tick. The significant-figure rule bites above it
        // and is applied by `venue_px` on the way out.
        tick_size: 10f64.powi(-(PRICE_DECIMAL_BUDGET - asset.sz_decimals as i32)),
        qty_step: step,
        min_qty: step,
        min_notional: MIN_ORDER_NOTIONAL_USD,
    }
}

/// A size as the venue will accept it: rounded down to the asset's step, never
/// up, and written as a plain decimal.
pub(crate) fn venue_sz(qty: f64, sz_decimals: u32) -> Result<String, VenueError> {
    if !qty.is_finite() || qty <= 0.0 {
        return Err(VenueError::BadRequest(format!(
            "{qty} is not a positive finite size"
        )));
    }
    let scale = 10f64.powi(sz_decimals as i32);
    let stepped = shave_dust(qty * scale).floor() / scale;
    if stepped <= 0.0 {
        return Err(VenueError::BadRequest(format!(
            "{qty} is below this asset's smallest tradable size"
        )));
    }
    Ok(decimal_string(stepped, sz_decimals as usize))
}

/// A price as the venue will accept it: both precision rules applied, rounded
/// toward the passive side.
pub(crate) fn venue_px(px: f64, side: Side, sz_decimals: u32) -> Result<String, VenueError> {
    if !px.is_finite() || px <= 0.0 {
        return Err(VenueError::BadRequest(format!(
            "{px} is not a positive finite price"
        )));
    }
    let decimals = price_decimals(px, sz_decimals);
    let scale = 10f64.powi(decimals);
    let scaled = shave_dust(px * scale);
    let snapped = match side {
        Side::Buy => scaled.floor() / scale,
        Side::Sell => scaled.ceil() / scale,
    };
    if snapped <= 0.0 {
        return Err(VenueError::BadRequest(format!(
            "{px} rounds away to nothing at this asset's precision"
        )));
    }
    Ok(decimal_string(snapped, decimals.max(0) as usize))
}

/// How many decimal places this price may carry: the tighter of the venue's
/// decimal budget and what five significant figures leaves, and never below
/// zero because a whole number is always allowed.
fn price_decimals(px: f64, sz_decimals: u32) -> i32 {
    let budget = PRICE_DECIMAL_BUDGET - sz_decimals as i32;
    let magnitude = px.abs().log10().floor() as i32;
    let for_significant_figures = MAX_SIGNIFICANT_FIGURES - 1 - magnitude;
    budget.min(for_significant_figures).max(0)
}

/// A plain decimal string with the trailing zeros shaved — the venue's own
/// normalization, and it never uses exponent notation.
fn decimal_string(value: f64, decimals: usize) -> String {
    let rendered = format!("{value:.decimals$}");
    if !rendered.contains('.') {
        return rendered;
    }
    let trimmed = rendered.trim_end_matches('0').trim_end_matches('.');
    if trimmed.is_empty() {
        "0".to_string()
    } else {
        trimmed.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn asset(coin: &str, index: u32, sz_decimals: u32) -> Asset {
        Asset {
            coin: coin.to_string(),
            index,
            sz_decimals,
            max_leverage: 20.0,
        }
    }

    #[test]
    fn a_book_symbol_finds_the_venues_coin() {
        assert_eq!(coin_of("BTCUSDT"), "BTC");
        assert_eq!(coin_of("ETHUSDC"), "ETH");
        assert_eq!(coin_of("SOLUSD"), "SOL");
        assert_eq!(coin_of("btcusdt"), "BTC");
        // Already a coin.
        assert_eq!(coin_of("kPEPE"), "KPEPE");
        // The suffix is not stripped down to nothing.
        assert_eq!(coin_of("USDT"), "USDT");
    }

    #[test]
    fn the_engines_spelling_round_trips() {
        for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"] {
            assert_eq!(symbol_of(&coin_of(symbol)), symbol);
        }
    }

    #[test]
    fn an_asset_the_venue_spells_with_a_lower_case_prefix_is_still_tradable() {
        // kPEPE, kBONK, kSHIB and their kin are the venue's own spelling, and
        // they are exactly the high-funding perps a carry book wants. Keyed by
        // that spelling they were published as tradable and then refused by
        // every order, stop and exit.
        let assets = Assets::from_rows(vec![asset("kPEPE", 5, 0), asset("BTC", 0, 5)]);
        assert_eq!(assets.for_symbol("KPEPEUSDT").unwrap().index, 5);
        assert_eq!(assets.for_symbol("kPEPEUSDT").unwrap().index, 5);
        assert_eq!(
            assets.for_symbol("KPEPEUSDT").unwrap().coin,
            "kPEPE",
            "the venue's own spelling is what is kept"
        );
        // Every symbol the rules advertise can be looked up again.
        for (symbol, _) in assets.instrument_rules() {
            assert!(assets.for_symbol(&symbol).is_ok(), "{symbol} is advertised and unreachable");
        }
    }

    #[test]
    fn an_unlisted_symbol_is_an_error_and_never_asset_zero() {
        // Asset numbers are positional, so a missing symbol falling back to a
        // default would send the order to whatever contract happens to be
        // first in the venue's list.
        let assets = Assets::from_rows(vec![asset("BTC", 0, 5)]);
        assert_eq!(assets.for_symbol("BTCUSDT").unwrap().index, 0);
        let missing = assets.for_symbol("DOGEUSDT").unwrap_err();
        assert!(missing.to_string().contains("DOGE"), "{missing}");
    }

    #[test]
    fn a_price_and_a_size_already_on_the_venues_grid_are_left_where_they_are() {
        // `0.29 * 100` is 28.999999999999996 and `1.1 * 100` is
        // 110.00000000000001. Scaling and rounding without shaving that dust
        // moves a buy a tick down, a sell a tick up, and a size a step short.
        assert_eq!(venue_sz(0.29, 2).unwrap(), "0.29");
        assert_eq!(venue_sz(8.2, 1).unwrap(), "8.2");
        assert_eq!(venue_px(1.1, Side::Sell, 4).unwrap(), "1.1");
        assert_eq!(venue_px(0.29, Side::Buy, 4).unwrap(), "0.29");
        assert_eq!(venue_px(2.9, Side::Buy, 4).unwrap(), "2.9");
        // A real fraction of a tick still rounds the passive way.
        assert_eq!(venue_px(1.00005, Side::Buy, 5).unwrap(), "1");
        assert_eq!(venue_px(1.00005, Side::Sell, 5).unwrap(), "1.1");
    }

    #[test]
    fn a_size_rounds_down_and_never_up() {
        // Rounding a size up would send more risk than the kernel approved.
        assert_eq!(venue_sz(0.123456, 4).unwrap(), "0.1234");
        assert_eq!(venue_sz(1.0, 4).unwrap(), "1");
        assert_eq!(venue_sz(1.5, 0).unwrap(), "1");
        assert!(venue_sz(0.00004, 4).is_err(), "a size that rounds to nothing");
        for bad in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert!(venue_sz(bad, 4).is_err(), "{bad} was accepted as a size");
        }
    }

    #[test]
    fn a_price_keeps_five_significant_figures_and_the_decimal_budget() {
        // szDecimals 5 leaves one decimal place, and five figures binds first
        // above 10000.
        assert_eq!(venue_px(1670.123, Side::Buy, 5).unwrap(), "1670.1");
        assert_eq!(venue_px(1670.123, Side::Sell, 5).unwrap(), "1670.2");
        // Whole numbers are always allowed, however many digits they carry.
        assert_eq!(venue_px(95_123.4, Side::Buy, 5).unwrap(), "95123");
        assert_eq!(venue_px(95_123.4, Side::Sell, 5).unwrap(), "95124");
        // A small price is bounded by the decimal budget, not the figures.
        assert_eq!(venue_px(0.000012345, Side::Buy, 0).unwrap(), "0.000012");
    }

    #[test]
    fn rounding_a_price_never_makes_the_order_more_aggressive() {
        // The property that matters for a post-only entry: a buy that rounded
        // up could cross the spread and pay the taker fee, on an order the
        // strategy asked to rest.
        for (px, sz_decimals) in [(1670.123, 5), (0.0001234, 0), (12.3456, 2), (99_999.9, 5)] {
            let buy: f64 = venue_px(px, Side::Buy, sz_decimals).unwrap().parse().unwrap();
            let sell: f64 = venue_px(px, Side::Sell, sz_decimals).unwrap().parse().unwrap();
            assert!(buy <= px + 1e-12, "a buy at {px} rounded up to {buy}");
            assert!(sell >= px - 1e-12, "a sell at {px} rounded down to {sell}");
        }
    }

    #[test]
    fn a_price_never_comes_out_in_exponent_notation() {
        // The venue reads these as decimal strings and rejects exponents.
        for px in [1e-6, 1e-5, 1e9, 0.000001234] {
            let text = venue_px(px, Side::Buy, 0).unwrap();
            assert!(!text.contains('e') && !text.contains('E'), "{px} rendered as {text}");
        }
        assert!(!venue_sz(1e-4, 8).unwrap().contains('e'));
    }

    #[test]
    fn the_instrument_rule_reports_the_finest_legal_tick() {
        let rule = instrument_rule(&asset("BTC", 0, 5));
        assert_eq!(rule.qty_step, 1e-5);
        assert_eq!(rule.min_qty, 1e-5);
        assert_eq!(rule.min_notional, MIN_ORDER_NOTIONAL_USD);
        assert!((rule.tick_size - 0.1).abs() < 1e-12, "{}", rule.tick_size);
        // A whole-unit asset gets six decimals of price.
        let coarse = instrument_rule(&asset("kPEPE", 1, 0));
        assert!((coarse.tick_size - 1e-6).abs() < 1e-15);
        assert_eq!(coarse.qty_step, 1.0);
    }

    #[test]
    fn the_rules_list_is_in_a_stable_order_under_the_engines_symbols() {
        let assets = Assets::from_rows(vec![asset("ETH", 1, 4), asset("BTC", 0, 5)]);
        let rules = assets.instrument_rules();
        let names: Vec<&str> = rules.iter().map(|(s, _)| s.as_str()).collect();
        assert_eq!(names, vec!["BTCUSDT", "ETHUSDT"]);
    }
}
