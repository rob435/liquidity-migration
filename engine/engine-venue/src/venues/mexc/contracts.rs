//! The contract table: what one MEXC contract is worth, per symbol.
//!
//! **This is the file that stops every order being the wrong size.** MEXC
//! quotes order quantity in *contracts*, not in coins, and one contract is
//! `contractSize` of the base coin — 0.0001 BTC on `BTC_USDT`, 1 XRP on
//! `XRP_USDT`, 100 TUT on `TUT_USDT`. Of the contracts the venue lists, fewer
//! than a quarter have `contractSize == 1`, so an adapter that passed the
//! engine's base-coin quantity straight through would be wrong on most
//! symbols and right on enough of them to look like it worked.
//!
//! Everything the engine hands down is in base coin and everything the venue
//! says back is in contracts, so both directions are converted here and
//! nowhere else.
//!
//! **The engine's spelling and the venue's are different too.** The engine
//! says `BTCUSDT`; MEXC says `BTC_USDT`. The mapping is read from the venue's
//! own `baseCoin` and `quoteCoin` rather than guessed by cutting the string,
//! because there is no rule that survives `1000PEPE_USDT` and friends.
//!
//! `apiAllowed` is per contract and the venue does set it false — an order on
//! one of those is refused here, by name, rather than at the venue.

use std::collections::HashMap;

use engine_types::ids::Symbol;
use engine_types::orders::InstrumentRule;
use engine_types::{quantize, VenueError};
use serde_json::Value;

/// One tradable contract, as the venue describes it.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Contract {
    /// The venue's spelling, e.g. `BTC_USDT`. What goes on the wire.
    pub venue_symbol: String,
    /// Base coin per contract. The multiplier everything here exists for.
    pub contract_size: f64,
    /// Price tick.
    pub price_unit: f64,
    /// Smallest and largest order, in contracts.
    pub min_vol: f64,
    pub max_vol: f64,
    pub max_leverage: f64,
    /// Whether the venue permits API trading on this contract at all.
    pub api_allowed: bool,
}

impl Contract {
    /// The instrument rule the engine quantizes against.
    ///
    /// The quantity step is the contract size, so a quantity the engine has
    /// already quantized is a whole number of contracts by construction —
    /// which is what makes [`Contract::vol_for`] able to refuse a fraction
    /// rather than round one away.
    pub fn rule(&self) -> InstrumentRule {
        InstrumentRule {
            tick_size: self.price_unit,
            qty_step: self.contract_size,
            min_qty: self.min_vol * self.contract_size,
            // MEXC publishes no per-symbol minimum notional; the minimum is
            // expressed in contracts and is already carried by `min_qty`.
            min_notional: 0.0,
        }
    }

    /// Base coin to whole contracts, for an order going out.
    ///
    /// The division is through [`quantize::steps`], which shaves float dust:
    /// `0.3 / 0.1` is 2.9999999999999996, and truncating that would send one
    /// contract fewer than the risk kernel approved.
    pub fn vol_for(&self, base_qty: f64) -> Result<u64, VenueError> {
        if !base_qty.is_finite() || base_qty <= 0.0 {
            return Err(VenueError::BadRequest(format!(
                "{} cannot be ordered in a quantity of {base_qty}",
                self.venue_symbol
            )));
        }
        let contracts = quantize::steps(base_qty, self.contract_size);
        if (contracts - contracts.round()).abs() > 0.0 {
            return Err(VenueError::BadRequest(format!(
                "{base_qty} of {} is {contracts} contracts, which is not a whole number — one \
                 contract is {}",
                self.venue_symbol, self.contract_size
            )));
        }
        let whole = contracts.round();
        if whole < self.min_vol {
            return Err(VenueError::BadRequest(format!(
                "{base_qty} of {} is {whole} contracts, under the venue minimum of {}",
                self.venue_symbol, self.min_vol
            )));
        }
        if whole > self.max_vol {
            return Err(VenueError::BadRequest(format!(
                "{base_qty} of {} is {whole} contracts, over the venue maximum of {}",
                self.venue_symbol, self.max_vol
            )));
        }
        Ok(whole as u64)
    }

    /// Contracts back to base coin, for a position, a fill, or a book level
    /// the venue has just reported.
    pub fn base_for(&self, contracts: f64) -> f64 {
        contracts * self.contract_size
    }
}

/// Every contract the venue lists, by the engine's spelling of its symbol.
#[derive(Clone, Debug, Default)]
pub(crate) struct Contracts {
    by_symbol: HashMap<Symbol, Contract>,
}

impl Contracts {
    /// Read `GET /api/v1/contract/detail`.
    pub fn parse(body: &Value) -> Result<Self, VenueError> {
        let rows = body
            .get("data")
            .and_then(Value::as_array)
            .ok_or_else(|| VenueError::BadReply("contract detail carried no data array".into()))?;
        let mut by_symbol = HashMap::with_capacity(rows.len());
        for row in rows {
            let Some(contract) = read_row(row) else { continue };
            by_symbol.insert(contract.0, contract.1);
        }
        if by_symbol.is_empty() {
            return Err(VenueError::BadReply(
                "contract detail listed no readable contracts".into(),
            ));
        }
        Ok(Self { by_symbol })
    }

    /// The contract for one of the engine's symbols, refusing a symbol the
    /// venue does not list and one it will not accept API orders on.
    pub fn tradable(&self, symbol: &str) -> Result<&Contract, VenueError> {
        let contract = self.by_symbol.get(symbol).ok_or_else(|| {
            VenueError::BadRequest(format!("MEXC lists no contract for {symbol}"))
        })?;
        if !contract.api_allowed {
            return Err(VenueError::BadRequest(format!(
                "MEXC does not permit API trading on {} (apiAllowed is false)",
                contract.venue_symbol
            )));
        }
        Ok(contract)
    }

    /// The contract, whether or not it may be traded. For reading a position
    /// or a fill back: the venue reports those in contracts whatever its
    /// current API-trading flag says, and refusing to convert one would lose
    /// the size of something already held.
    pub fn any(&self, symbol: &str) -> Option<&Contract> {
        self.by_symbol.get(symbol)
    }

    /// The engine's symbol for one of the venue's, for decoding a reply that
    /// names a contract rather than answering about one.
    pub fn symbol_of(&self, venue_symbol: &str) -> Option<&Symbol> {
        self.by_symbol
            .iter()
            .find(|(_, c)| c.venue_symbol == venue_symbol)
            .map(|(symbol, _)| symbol)
    }

    pub fn rules(&self) -> Vec<(Symbol, InstrumentRule)> {
        self.by_symbol
            .iter()
            .map(|(symbol, contract)| (symbol.clone(), contract.rule()))
            .collect()
    }

    pub fn is_empty(&self) -> bool {
        self.by_symbol.is_empty()
    }
}

/// One row of `contract/detail`. A row missing anything load-bearing is
/// skipped rather than defaulted: a contract size guessed at 1 would size
/// every order on that symbol wrong by its real multiplier.
fn read_row(row: &Value) -> Option<(Symbol, Contract)> {
    let venue_symbol = row.get("symbol")?.as_str()?.to_string();
    let base = row.get("baseCoin")?.as_str()?;
    let quote = row.get("quoteCoin")?.as_str()?;
    let contract_size = row.get("contractSize")?.as_f64()?;
    let price_unit = row.get("priceUnit")?.as_f64()?;
    if !(contract_size.is_finite() && contract_size > 0.0) {
        return None;
    }
    if !(price_unit.is_finite() && price_unit > 0.0) {
        return None;
    }
    Some((
        format!("{base}{quote}"),
        Contract {
            venue_symbol,
            contract_size,
            price_unit,
            min_vol: row.get("minVol").and_then(Value::as_f64).unwrap_or(1.0),
            max_vol: row.get("maxVol").and_then(Value::as_f64).unwrap_or(f64::MAX),
            max_leverage: row.get("maxLeverage").and_then(Value::as_f64).unwrap_or(1.0),
            // Absent reads as "not permitted". A contract whose row does not
            // say is not one to find out about by sending an order.
            api_allowed: row.get("apiAllowed").and_then(Value::as_bool).unwrap_or(false),
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Recorded from `GET https://contract.mexc.com/api/v1/contract/detail`.
    /// Four real rows, chosen for their contract sizes: 0.0001, 1 and 100,
    /// plus one the venue will not accept API orders on. Real bytes, so a
    /// renamed field fails here rather than on a live order.
    const DETAIL: &str = r#"{"success":true,"code":0,"data":[
      {"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT",
       "contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":400000,
       "maxLeverage":500,"apiAllowed":true,"state":0,"amountScale":4,"priceScale":1},
      {"symbol":"XRP_USDT","baseCoin":"XRP","quoteCoin":"USDT","settleCoin":"USDT",
       "contractSize":1,"priceUnit":0.0001,"minVol":1,"maxVol":840000,
       "maxLeverage":300,"apiAllowed":true,"state":0,"amountScale":4,"priceScale":4},
      {"symbol":"TUT_USDT","baseCoin":"TUT","quoteCoin":"USDT","settleCoin":"USDT",
       "contractSize":100,"priceUnit":1e-05,"minVol":1,"maxVol":500,
       "maxLeverage":100,"apiAllowed":true,"state":0,"amountScale":4,"priceScale":5},
      {"symbol":"BULLCOIN_USDT","baseCoin":"BULLCOIN","quoteCoin":"USDT","settleCoin":"USDT",
       "contractSize":100,"priceUnit":1e-06,"minVol":1,"maxVol":300,
       "maxLeverage":20,"apiAllowed":false,"state":0,"amountScale":4,"priceScale":6}]}"#;

    fn table() -> Contracts {
        Contracts::parse(&serde_json::from_str(DETAIL).unwrap()).unwrap()
    }

    #[test]
    fn the_engines_symbol_is_the_base_and_quote_joined_not_the_venues_spelling() {
        let t = table();
        assert_eq!(t.tradable("BTCUSDT").unwrap().venue_symbol, "BTC_USDT");
        assert_eq!(t.tradable("XRPUSDT").unwrap().venue_symbol, "XRP_USDT");
        // The venue's own spelling is not what the engine asks with.
        assert!(t.tradable("BTC_USDT").is_err());
    }

    #[test]
    fn one_contract_is_not_one_coin_and_the_table_says_how_many() {
        let t = table();
        assert_eq!(t.tradable("BTCUSDT").unwrap().contract_size, 0.0001);
        assert_eq!(t.tradable("XRPUSDT").unwrap().contract_size, 1.0);
        assert_eq!(t.tradable("TUTUSDT").unwrap().contract_size, 100.0);
    }

    #[test]
    fn base_coin_becomes_whole_contracts() {
        let t = table();
        // 1 BTC at 0.0001 per contract is 10,000 contracts.
        assert_eq!(t.tradable("BTCUSDT").unwrap().vol_for(1.0).unwrap(), 10_000);
        // The only case where the naive pass-through would have been right.
        assert_eq!(t.tradable("XRPUSDT").unwrap().vol_for(250.0).unwrap(), 250);
        // 300 TUT at 100 per contract is 3 contracts.
        assert_eq!(t.tradable("TUTUSDT").unwrap().vol_for(300.0).unwrap(), 3);
    }

    #[test]
    fn the_float_dust_that_eats_a_contract_is_shaved() {
        // 0.0003 / 0.0001 is 2.9999999999999996 in binary floating point.
        // Truncating sends two contracts where the kernel approved three.
        let t = table();
        assert_eq!(t.tradable("BTCUSDT").unwrap().vol_for(0.0003).unwrap(), 3);
        assert_eq!(t.tradable("BTCUSDT").unwrap().vol_for(0.0007).unwrap(), 7);
    }

    #[test]
    fn a_size_that_is_not_a_whole_number_of_contracts_is_refused_not_rounded() {
        // Rounding here would send a different size from the one the risk
        // kernel approved. The engine quantizes to `qty_step`, which IS the
        // contract size, so reaching this means something upstream skipped it.
        let t = table();
        let err = t.tradable("TUTUSDT").unwrap().vol_for(150.0).unwrap_err();
        assert!(err.to_string().contains("not a whole number"), "{err}");
    }

    #[test]
    fn a_size_under_the_venue_minimum_or_over_its_maximum_is_refused() {
        let t = table();
        // Half a contract of TUT.
        assert!(t.tradable("TUTUSDT").unwrap().vol_for(50.0).is_err());
        // maxVol is 500 contracts = 50,000 TUT.
        assert!(t.tradable("TUTUSDT").unwrap().vol_for(60_000.0).is_err());
        assert!(t.tradable("TUTUSDT").unwrap().vol_for(50_000.0).is_ok());
    }

    #[test]
    fn contracts_convert_back_to_base_for_a_position_or_a_fill() {
        let t = table();
        assert_eq!(t.any("BTCUSDT").unwrap().base_for(10_000.0), 1.0);
        assert_eq!(t.any("TUTUSDT").unwrap().base_for(3.0), 300.0);
    }

    #[test]
    fn a_contract_the_venue_will_not_take_api_orders_on_is_refused_by_name() {
        let t = table();
        let err = t.tradable("BULLCOINUSDT").unwrap_err();
        assert!(err.to_string().contains("apiAllowed"), "{err}");
        // But it is still readable, because a position already held on it has
        // a size that must be converted whatever the flag says.
        assert!(t.any("BULLCOINUSDT").is_some());
    }

    #[test]
    fn the_instrument_rule_makes_the_engine_quantize_onto_whole_contracts() {
        let t = table();
        let rule = t.tradable("TUTUSDT").unwrap().rule();
        assert_eq!(rule.qty_step, 100.0);
        assert_eq!(rule.min_qty, 100.0);
        assert_eq!(rule.tick_size, 1e-05);
        // Which is what makes the refusal above unreachable in the live path:
        // 150 TUT quantizes down to 100 before it ever gets here.
        assert_eq!(quantize::quantize_qty(150.0, &rule), Some(100.0));
    }

    #[test]
    fn a_row_missing_its_contract_size_is_skipped_rather_than_assumed_to_be_one() {
        let body: Value = serde_json::from_str(
            r#"{"data":[{"symbol":"X_USDT","baseCoin":"X","quoteCoin":"USDT","priceUnit":0.1,
                 "minVol":1,"apiAllowed":true},
                {"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","contractSize":0.0001,
                 "priceUnit":0.1,"minVol":1,"maxVol":400000,"apiAllowed":true}]}"#,
        )
        .unwrap();
        let t = Contracts::parse(&body).unwrap();
        assert!(t.any("XUSDT").is_none(), "a row with no contract size was kept");
        assert!(t.any("BTCUSDT").is_some());
    }

    #[test]
    fn a_row_that_does_not_say_whether_api_trading_is_allowed_is_read_as_not_allowed() {
        let body: Value = serde_json::from_str(
            r#"{"data":[{"symbol":"Y_USDT","baseCoin":"Y","quoteCoin":"USDT",
                 "contractSize":1,"priceUnit":0.1,"minVol":1}]}"#,
        )
        .unwrap();
        let t = Contracts::parse(&body).unwrap();
        assert!(t.tradable("YUSDT").is_err(), "a silent row was treated as tradable");
    }

    #[test]
    fn an_empty_or_shapeless_reply_is_an_error_not_an_empty_table() {
        // An empty table would make every symbol "not listed", which reads
        // like a delisting rather than like a failed read.
        assert!(Contracts::parse(&serde_json::json!({"data": []})).is_err());
        assert!(Contracts::parse(&serde_json::json!({"success": true})).is_err());
    }
}
