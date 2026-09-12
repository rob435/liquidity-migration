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
//! Opening eligibility is distinct from retained identity for cleanup.

use std::collections::HashMap;

use crate::numeric_wire::DecimalField;
use engine_types::ids::Symbol;
use engine_types::orders::InstrumentRule;
use engine_types::{quantize, VenueError};
use serde::Deserialize;
use serde_json::Value;

/// One tradable contract, as the venue describes it.
#[derive(Clone, Debug, PartialEq)]
pub struct Contract {
    /// The venue's spelling, e.g. `BTC_USDT`. What goes on the wire.
    pub venue_symbol: String,
    /// Base coin per contract. The multiplier everything here exists for.
    pub contract_size: f64,
    pub exact_contract_size: engine_types::numeric::ExactNumber,
    pub settlement_asset: engine_types::numeric::AssetId,
    pub exact_spec: engine_types::numeric::ExactInstrumentSpec,
    /// Price tick.
    pub price_unit: f64,
    /// Smallest order, in contracts.
    pub min_vol: f64,
    /// Largest order, in contracts. The venue publishes two ceilings and they
    /// are not the same: `maxVol` applies to a market order and `limitMaxVol`
    /// to a limit one. They differ on three contracts today, BTC and ETH among
    /// them — clamping everything to the smaller silently caps a BTC limit
    /// order at 40 BTC with no venue error to show for it.
    pub max_vol: f64,
    pub limit_max_vol: f64,
    pub max_leverage: f64,
    /// Whether the venue permits API trading on this contract at all.
    pub api_allowed: bool,
    pub execution: ExecutionCapabilities,
}

/// Native metadata, not inferred from API permission or a symbol's spelling.
#[derive(Clone, Debug, PartialEq)]
pub struct ExecutionCapabilities {
    pub volume_unit: Option<engine_types::numeric::ExactNumber>,
    pub lifecycle_state: Option<i64>,
    pub position_open_type: Option<i64>,
    pub stop_only_fair: Option<bool>,
    pub future_type: Option<i64>,
    pub min_leverage: Option<f64>,
    pub max_leverage: Option<f64>,
    pub retained: bool,
    /// Version 1 preserves historical grids; it never authorizes a new entry.
    pub metadata_version: u8,
}

/// Which of the venue's two size ceilings applies.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Ceiling {
    Limit,
    Market,
}

impl Contract {
    /// The instrument rule the engine quantizes against.
    ///
    /// Missing execution metadata retains a read-only legacy projection.
    /// `require_order_capability` must succeed before using it for an order.
    pub fn rule(&self) -> InstrumentRule {
        InstrumentRule {
            tick_size: self.price_unit,
            qty_step: self
                .exact_spec
                .qty_step
                .as_ref()
                .and_then(|step| step.to_f64().ok())
                .unwrap_or(self.contract_size),
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
    pub fn vol_for(&self, base_qty: f64, ceiling: Ceiling) -> Result<u64, VenueError> {
        let volume_unit = self.require_order_capability()?;
        let max_vol = match ceiling {
            Ceiling::Limit => self.limit_max_vol,
            Ceiling::Market => self.max_vol,
        };
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
        if !whole.is_finite() || whole >= u64::MAX as f64 {
            return Err(VenueError::BadRequest(format!(
                "{} quantity cannot be represented as native contracts",
                self.venue_symbol
            )));
        }
        if !(whole as u64).is_multiple_of(volume_unit) {
            return Err(VenueError::BadRequest(format!(
                "{whole} contracts of {} are off the volUnit={volume_unit} grid",
                self.venue_symbol
            )));
        }
        if whole < self.min_vol {
            return Err(VenueError::BadRequest(format!(
                "{base_qty} of {} is {whole} contracts, under the venue minimum of {}",
                self.venue_symbol, self.min_vol
            )));
        }
        if whole > max_vol {
            return Err(VenueError::BadRequest(format!(
                "{base_qty} of {} is {whole} contracts, over the venue maximum of {max_vol} \
                 for a {ceiling:?} order",
                self.venue_symbol
            )));
        }
        Ok(whole as u64)
    }

    pub fn require_order_capability(&self) -> Result<u64, VenueError> {
        use engine_types::numeric::Exact;
        if !matches!(self.execution.position_open_type, Some(2 | 3)) {
            return Err(VenueError::BadRequest(format!(
                "{} has unsupported or unknown positionOpenType {:?}; cross margin is required",
                self.venue_symbol, self.execution.position_open_type
            )));
        }
        let unit = self.execution.volume_unit.as_ref().ok_or_else(|| {
            VenueError::BadRequest(format!("{} has no qualified volUnit", self.venue_symbol))
        })?;
        if !unit.value.is_positive()
            || !unit.value.is_multiple_of(&Exact::one()).unwrap_or(false)
            || unit.value.to_u64_exact().is_err()
        {
            return Err(VenueError::BadRequest(format!(
                "{} has an unsupported native volume step",
                self.venue_symbol
            )));
        }
        unit.value
            .to_u64_exact()
            .map_err(|e| VenueError::BadRequest(e.to_string()))
    }

    /// MEXC lossTrend: 1 last price, 2 fair price. Unknown is not last price.
    pub fn stop_trend(&self) -> Result<i64, VenueError> {
        match self.execution.stop_only_fair {
            Some(false) => Ok(1),
            Some(true) => Ok(2),
            None => Err(VenueError::BadRequest(format!(
                "{} has no qualified stopOnlyFair metadata",
                self.venue_symbol
            ))),
        }
    }

    /// Contracts back to base coin, for a position, a fill, or a book level
    /// the venue has just reported.
    ///
    /// Through the same dust shave the outbound direction uses: `3 * 0.0001`
    /// is 0.00030000000000000003, and a position size carrying that would not
    /// compare equal to the one the engine's own log holds.
    pub fn base_for(&self, contracts: f64) -> f64 {
        quantize::round_clean(contracts * self.contract_size, self.contract_size)
    }
}

/// Every contract the venue lists, by the engine's spelling of its symbol.
#[derive(Clone, Debug, Default)]
pub struct Contracts {
    by_symbol: HashMap<Symbol, Contract>,
}

impl Contracts {
    /// Read `GET /api/v1/contract/detail`.
    pub fn parse(body: &Value) -> Result<Self, VenueError> {
        Self::parse_raw(&body.to_string())
    }

    pub fn parse_raw(raw: &str) -> Result<Self, VenueError> {
        Self::parse_version(raw, 2)
    }

    /// Rebuild the historical checkpoint's original grid without making it
    /// eligible for a new opening. Fresh metadata is required for promotion.
    pub fn parse_checkpoint_v1(raw: &str) -> Result<Self, VenueError> {
        Self::parse_version(raw, 1)
    }

    fn parse_version(raw: &str, version: u8) -> Result<Self, VenueError> {
        #[derive(Deserialize)]
        struct Reply {
            data: Box<serde_json::value::RawValue>,
        }
        let body: Reply = crate::numeric_wire::decode_object(raw)
            .map_err(|e| VenueError::BadReply(format!("contract detail: {e}")))?;
        let rows: Vec<Box<serde_json::value::RawValue>> = match body.data.get().as_bytes().first() {
            Some(b'[') => serde_json::from_str(body.data.get())
                .map_err(|e| VenueError::BadReply(format!("contract rows: {e}")))?,
            Some(b'{') => vec![body.data],
            _ => {
                return Err(VenueError::BadReply(
                    "contract data must be an object or array".into(),
                ))
            }
        };
        let mut by_symbol = HashMap::with_capacity(rows.len());
        for raw in rows {
            if let Some((symbol, contract)) = read_row(raw.get(), version) {
                if by_symbol.insert(symbol.clone(), contract).is_some() {
                    return Err(VenueError::BadReply(format!(
                        "duplicate MEXC contract identity {symbol}"
                    )));
                }
            }
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
        let contract = self.existing(symbol)?;
        if !contract.api_allowed {
            return Err(VenueError::BadRequest(format!(
                "MEXC does not permit API trading on {} (apiAllowed is false)",
                contract.venue_symbol
            )));
        }
        if contract.execution.metadata_version != 2 || contract.execution.retained {
            return Err(VenueError::BadRequest(format!(
                "{} is retained historical metadata, not permission to open",
                contract.venue_symbol
            )));
        }
        if contract.execution.lifecycle_state != Some(0) {
            return Err(VenueError::BadRequest(format!(
                "{} is not enabled (state={:?})",
                contract.venue_symbol, contract.execution.lifecycle_state
            )));
        }
        if contract.execution.future_type != Some(1) {
            return Err(VenueError::BadRequest(format!(
                "{} is not a qualified perpetual contract (futureType={:?})",
                contract.venue_symbol, contract.execution.future_type
            )));
        }
        contract.require_order_capability()?;
        contract.stop_trend()?;
        Ok(contract)
    }

    /// Identity and conversion metadata for existing exposure, independently
    /// of permission to open. Unknown metadata never licenses guessed sizing.
    pub fn existing(&self, symbol: &str) -> Result<&Contract, VenueError> {
        self.by_symbol.get(symbol).ok_or_else(|| {
            VenueError::BadRequest(format!(
                "MEXC has no retained contract metadata for {symbol}"
            ))
        })
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

    /// Sorted by symbol: the checkpoint compares these rows as a sequence,
    /// and a map's iteration order differs between two tables built from the
    /// same page.
    pub fn rules(&self) -> Vec<(Symbol, InstrumentRule)> {
        let mut rows = self
            .by_symbol
            .iter()
            .map(|(symbol, contract)| (symbol.clone(), contract.rule()))
            .collect::<Vec<_>>();
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        rows
    }

    /// Every contract as (the engine's spelling, the venue's), sorted by
    /// symbol, for a caller that needs only the mapping — the price feed,
    /// which holds no key.
    pub fn symbol_pairs(&self) -> Vec<(Symbol, String)> {
        let mut rows = self
            .by_symbol
            .iter()
            .map(|(symbol, c)| (symbol.clone(), c.venue_symbol.clone()))
            .collect::<Vec<_>>();
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        rows
    }

    pub fn instrument_specs(&self) -> Vec<(Symbol, engine_types::numeric::ExactInstrumentSpec)> {
        let mut rows = self
            .by_symbol
            .iter()
            .map(|(symbol, contract)| (symbol.clone(), contract.exact_spec.clone()))
            .collect::<Vec<_>>();
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        rows
    }

    pub fn is_empty(&self) -> bool {
        self.by_symbol.is_empty()
    }
}

/// One row of `contract/detail`. A row missing anything load-bearing is
/// skipped rather than defaulted: a contract size guessed at 1 would size
/// every order on that symbol wrong by its real multiplier.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ContractRow {
    symbol: String,
    base_coin: String,
    quote_coin: String,
    settle_coin: String,
    contract_size: DecimalField,
    price_unit: DecimalField,
    #[serde(default)]
    vol_unit: DecimalField,
    #[serde(default)]
    state: Value,
    #[serde(default)]
    position_open_type: Value,
    #[serde(default)]
    stop_only_fair: Value,
    #[serde(default)]
    future_type: Value,
    #[serde(default)]
    min_leverage: DecimalField,
    /// Local checkpoint provenance, never a venue capability.
    #[serde(default, rename = "__lm_retained")]
    retained: bool,
    #[serde(default)]
    min_vol: DecimalField,
    #[serde(default)]
    max_vol: DecimalField,
    #[serde(default)]
    limit_max_vol: DecimalField,
    #[serde(default)]
    max_leverage: DecimalField,
    #[serde(default)]
    api_allowed: Option<bool>,
}

fn read_row(raw: &str, metadata_version: u8) -> Option<(Symbol, Contract)> {
    let row: ContractRow = crate::numeric_wire::decode_object(raw).ok()?;
    if row.settle_coin != row.quote_coin {
        return None;
    }
    let contract_size = row.contract_size.legacy("contractSize").ok()?;
    let price_unit = row.price_unit.legacy("priceUnit").ok()?;
    if contract_size <= 0.0 || price_unit <= 0.0 {
        return None;
    }
    for (field, name) in [
        (&row.min_vol, "minVol"),
        (&row.max_vol, "maxVol"),
        (&row.limit_max_vol, "limitMaxVol"),
    ] {
        if let Some(number) = field.optional(name).ok()? {
            if !number.value.is_positive()
                || !number
                    .value
                    .is_multiple_of(&engine_types::numeric::Exact::one())
                    .ok()?
            {
                return None;
            }
            number.value.to_f64().ok()?;
        }
    }
    let max_vol = row.max_vol.legacy("maxVol").unwrap_or(f64::MAX);
    // A leverage the venue did not send is a fact about the row; one it sent
    // malformed is a row this table cannot vouch for, so the row is refused
    // rather than read as absent and defaulted.
    let optional_f64 = |field: &DecimalField, name: &str| -> Option<Option<f64>> {
        match field.optional(name).ok()? {
            None => Some(None),
            Some(number) => Some(Some(number.value.to_f64().ok()?)),
        }
    };
    let min_leverage = optional_f64(&row.min_leverage, "minLeverage")?;
    let max_leverage = optional_f64(&row.max_leverage, "maxLeverage")?;
    use engine_types::numeric::{AssetId, ExactInstrumentSpec, PricePrecision};
    let multiplier = row.contract_size.required("contractSize").ok()?.value;
    let volume_unit = row.vol_unit.optional("volUnit").ok().flatten();
    let qty_step = if metadata_version == 1 {
        Some(multiplier.clone())
    } else {
        volume_unit
            .as_ref()
            .filter(|unit| unit.value.is_positive())
            .map(|unit| &unit.value * &multiplier)
    };
    let base_qty = |field: &DecimalField, name: &str| {
        field
            .optional(name)
            .ok()
            .flatten()
            .map(|number| &number.value * &multiplier)
    };
    let exact_spec = ExactInstrumentSpec {
        native_symbol: row.symbol.clone(),
        base_asset: AssetId::Named(row.base_coin.clone()),
        quote_asset: AssetId::Named(row.quote_coin.clone()),
        settlement_asset: AssetId::Named(row.settle_coin.clone()),
        tick_size: Some(row.price_unit.required("priceUnit").ok()?.value),
        min_price: None,
        max_price: None,
        price_precision: PricePrecision::Tick,
        qty_step: qty_step.clone(),
        market_qty_step: qty_step,
        min_qty: base_qty(&row.min_vol, "minVol"),
        market_min_qty: base_qty(&row.min_vol, "minVol"),
        max_qty: base_qty(&row.limit_max_vol, "limitMaxVol"),
        max_market_qty: base_qty(&row.max_vol, "maxVol"),
        min_notional: None,
        contract_multiplier: Some(multiplier),
        fee_assets: None,
        fee_step: None,
    };
    Some((
        format!("{}{}", row.base_coin, row.quote_coin),
        Contract {
            venue_symbol: row.symbol,
            contract_size,
            exact_contract_size: row.contract_size.required("contractSize").ok()?,
            exact_spec,
            settlement_asset: engine_types::numeric::AssetId::Named(row.settle_coin),
            price_unit,
            min_vol: row.min_vol.legacy("minVol").unwrap_or(1.0),
            max_vol,
            limit_max_vol: row.limit_max_vol.legacy("limitMaxVol").unwrap_or(max_vol),
            max_leverage: max_leverage.unwrap_or(1.0),
            api_allowed: row.api_allowed.unwrap_or(false),
            execution: ExecutionCapabilities {
                volume_unit,
                lifecycle_state: row.state.as_i64(),
                position_open_type: row.position_open_type.as_i64(),
                stop_only_fair: row.stop_only_fair.as_bool(),
                future_type: row.future_type.as_i64(),
                min_leverage,
                max_leverage,
                retained: row.retained,
                metadata_version,
            },
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Recorded from `GET /api/v1/contract/detail` on the realm's own host.
    /// Four real rows, chosen for their contract sizes: 0.0001, 1 and 100,
    /// plus one the venue will not accept API orders on. Real bytes, so a
    /// renamed field fails here rather than on a live order.
    const DETAIL: &str = r#"{"success":true,"code":0,"data":[
      {"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT",
       "contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":400000,"limitMaxVol":2500000,
       "maxLeverage":500,"apiAllowed":true,"state":0,"amountScale":4,"priceScale":1},
      {"symbol":"BTC_USD","baseCoin":"BTC","quoteCoin":"USD","settleCoin":"BTC",
       "contractSize":100,"priceUnit":0.1,"minVol":1,"maxVol":1000000,"futureType":1,
       "maxLeverage":200,"apiAllowed":true,"state":0,"amountScale":4,"priceScale":1},
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
        // Synthetic capability overlay; DETAIL remains the original recording.
        let mut page: Value = serde_json::from_str(DETAIL).unwrap();
        for row in page["data"].as_array_mut().unwrap() {
            row["volUnit"] = serde_json::json!(1);
            row["positionOpenType"] = serde_json::json!(3);
            row["stopOnlyFair"] = serde_json::json!(false);
            row["futureType"] = serde_json::json!(1);
        }
        Contracts::parse(&page).unwrap()
    }

    /// A page the size of the live one, in shape: enough rows that two maps
    /// built from it never happen to iterate in the same order.
    fn wide_page(rows: usize) -> String {
        let body = (0..rows)
            .map(|i| {
                format!(
                    r#"{{"symbol":"C{i}_USDT","baseCoin":"C{i}","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.01,"priceUnit":0.001,"minVol":1,"maxVol":1000,"maxLeverage":50,"apiAllowed":true,"state":0,"amountScale":2,"priceScale":3}}"#
                )
            })
            .collect::<Vec<_>>()
            .join(",");
        format!(r#"{{"success":true,"code":0,"data":[{body}]}}"#)
    }

    #[test]
    fn two_tables_from_one_page_enumerate_the_same_rows_in_the_same_order() {
        // Observed live on 2026-09-08: the catalog checkpoint compares rule rows
        // as a sequence, and a table's map order differs between two parses of
        // one page, so the engine refused its own checkpoint every second.
        let page = wide_page(200);
        let first = Contracts::parse_raw(&page).unwrap();
        let second = Contracts::parse_raw(&page).unwrap();
        assert_eq!(first.rules(), second.rules());
        assert_eq!(first.symbol_pairs(), second.symbol_pairs());
        assert_eq!(first.instrument_specs(), second.instrument_specs());
        let symbols: Vec<_> = first.rules().into_iter().map(|(s, _)| s).collect();
        let mut sorted = symbols.clone();
        sorted.sort();
        assert_eq!(symbols, sorted, "rules are not sorted by symbol");
        assert_eq!(symbols.len(), 200);
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
        assert_eq!(
            t.tradable("BTCUSDT")
                .unwrap()
                .vol_for(1.0, Ceiling::Limit)
                .unwrap(),
            10_000
        );
        // The only case where the naive pass-through would have been right.
        assert_eq!(
            t.tradable("XRPUSDT")
                .unwrap()
                .vol_for(250.0, Ceiling::Limit)
                .unwrap(),
            250
        );
        // 300 TUT at 100 per contract is 3 contracts.
        assert_eq!(
            t.tradable("TUTUSDT")
                .unwrap()
                .vol_for(300.0, Ceiling::Limit)
                .unwrap(),
            3
        );
    }

    #[test]
    fn the_float_dust_that_eats_a_contract_is_shaved() {
        // 0.0003 / 0.0001 is 2.9999999999999996 in binary floating point.
        // Truncating sends two contracts where the kernel approved three.
        let t = table();
        assert_eq!(
            t.tradable("BTCUSDT")
                .unwrap()
                .vol_for(0.0003, Ceiling::Limit)
                .unwrap(),
            3
        );
        assert_eq!(
            t.tradable("BTCUSDT")
                .unwrap()
                .vol_for(0.0007, Ceiling::Limit)
                .unwrap(),
            7
        );
    }

    #[test]
    fn a_size_that_is_not_a_whole_number_of_contracts_is_refused_not_rounded() {
        // Rounding here would send a different size from the one the risk
        // kernel approved. The engine quantizes to `qty_step`, which IS the
        // contract size, so reaching this means something upstream skipped it.
        let t = table();
        let err = t
            .tradable("TUTUSDT")
            .unwrap()
            .vol_for(150.0, Ceiling::Limit)
            .unwrap_err();
        assert!(err.to_string().contains("not a whole number"), "{err}");
    }

    #[test]
    fn a_size_under_the_venue_minimum_or_over_its_maximum_is_refused() {
        let t = table();
        // Half a contract of TUT.
        assert!(t
            .tradable("TUTUSDT")
            .unwrap()
            .vol_for(50.0, Ceiling::Limit)
            .is_err());
        // maxVol is 500 contracts = 50,000 TUT.
        assert!(t
            .tradable("TUTUSDT")
            .unwrap()
            .vol_for(60_000.0, Ceiling::Limit)
            .is_err());
        assert!(t
            .tradable("TUTUSDT")
            .unwrap()
            .vol_for(50_000.0, Ceiling::Limit)
            .is_ok());
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
    fn an_inverse_contract_is_not_listed_at_all() {
        // BTC_USD settles in BTC and its contractSize of 100 is 100 USD, not
        // 100 BTC. Listing it and converting with the linear rule would size a
        // position out by the price of a coin. `futureType` is 1 on both kinds
        // so it cannot be the discriminator; settling in something other than
        // the quote currency is what makes a contract inverse.
        let t = table();
        assert!(t.any("BTCUSD").is_none(), "an inverse contract was listed");
        assert!(t.tradable("BTCUSD").is_err());
        assert_eq!(t.tradable("BTCUSDT").unwrap().venue_symbol, "BTC_USDT");
    }

    #[test]
    fn a_limit_order_gets_the_limit_ceiling_and_a_market_order_the_market_one() {
        // The venue publishes two and they differ on BTC. Clamping a limit
        // order to maxVol caps it at 40 BTC with no venue error to say so.
        let t = table();
        let btc = t.tradable("BTCUSDT").unwrap();
        assert_eq!(btc.max_vol, 400_000.0);
        assert_eq!(btc.limit_max_vol, 2_500_000.0);
        assert!(btc.vol_for(100.0, Ceiling::Market).is_err());
        assert_eq!(btc.vol_for(100.0, Ceiling::Limit).unwrap(), 1_000_000);
    }

    #[test]
    fn a_contract_with_only_one_published_ceiling_uses_it_for_both() {
        let t = table();
        let tut = t.tradable("TUTUSDT").unwrap();
        assert_eq!(tut.max_vol, tut.limit_max_vol);
    }

    #[test]
    fn a_row_missing_its_contract_size_is_skipped_rather_than_assumed_to_be_one() {
        let body: Value = serde_json::from_str(
            r#"{"data":[{"symbol":"X_USDT","baseCoin":"X","quoteCoin":"USDT","settleCoin":"USDT",
                 "priceUnit":0.1,"minVol":1,"apiAllowed":true},
                {"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT",
                 "contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":400000,"apiAllowed":true}]}"#,
        )
        .unwrap();
        let t = Contracts::parse(&body).unwrap();
        assert!(
            t.any("XUSDT").is_none(),
            "a row with no contract size was kept"
        );
        assert!(t.any("BTCUSDT").is_some());
    }

    #[test]
    fn a_row_that_does_not_say_whether_api_trading_is_allowed_is_read_as_not_allowed() {
        let body: Value = serde_json::from_str(
            r#"{"data":[{"symbol":"Y_USDT","baseCoin":"Y","quoteCoin":"USDT","settleCoin":"USDT",
                 "contractSize":1,"priceUnit":0.1,"minVol":1}]}"#,
        )
        .unwrap();
        let t = Contracts::parse(&body).unwrap();
        assert!(
            t.tradable("YUSDT").is_err(),
            "a silent row was treated as tradable"
        );
    }

    #[test]
    fn malformed_volume_capability_is_refused_instead_of_becoming_unlimited() {
        for (field, value) in [
            ("maxVol", "\"broken\""),
            ("limitMaxVol", "true"),
            ("minVol", "-1"),
        ] {
            let raw = format!(
                r#"{{"data":[{{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"apiAllowed":true,"{field}":{value}}}]}}"#
            );
            assert!(
                Contracts::parse_raw(&raw).is_err(),
                "invalid {field} became a permissive capability"
            );
        }
    }

    #[test]
    fn an_empty_or_shapeless_reply_is_an_error_not_an_empty_table() {
        // An empty table would make every symbol "not listed", which reads
        // like a delisting rather than like a failed read.
        assert!(Contracts::parse(&serde_json::json!({"data": []})).is_err());
        assert!(Contracts::parse(&serde_json::json!({"success": true})).is_err());
    }

    #[test]
    fn a_leverage_the_venue_left_out_is_absent_and_one_it_garbled_refuses_the_row() {
        let mut page: Value = serde_json::from_str(DETAIL).unwrap();
        let rows = page["data"].as_array_mut().unwrap();
        for row in rows.iter_mut() {
            row["volUnit"] = serde_json::json!(1);
            row["positionOpenType"] = serde_json::json!(3);
            row["stopOnlyFair"] = serde_json::json!(false);
            row["futureType"] = serde_json::json!(1);
        }
        // BTC_USDT: no leverage fields at all.
        rows[0].as_object_mut().unwrap().remove("maxLeverage");
        // ETH_USDT: a maxLeverage that is not a number.
        rows[1]["maxLeverage"] = serde_json::json!("plenty");
        // XRP_USDT: a minLeverage that is not a number.
        rows[2]["minLeverage"] = serde_json::json!("1x");
        let table = Contracts::parse(&page).unwrap();

        let btc = table.tradable("BTCUSDT").unwrap();
        assert_eq!(
            btc.max_leverage, 1.0,
            "absent falls back to the venue's floor"
        );
        assert_eq!(btc.execution.max_leverage, None);
        assert_eq!(btc.execution.min_leverage, None);
        assert!(
            table.tradable("ETHUSDT").is_err(),
            "a malformed maxLeverage is not read as absent"
        );
        assert!(
            table.tradable("XRPUSDT").is_err(),
            "a malformed minLeverage is not read as absent"
        );
    }
}
