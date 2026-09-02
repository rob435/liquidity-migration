//! The tradable universe, derived on the instrument cadence from the venue's
//! own instrument and ticker pages. Nothing is frozen: a symbol that starts
//! trading, stops trading, or crosses a rank boundary enters or leaves at the
//! next refresh. Rank boundaries carry hysteresis (enter at `enter_rank`, stay
//! until past `leave_rank`) so a name at the edge does not flap in and out.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::config::sha256_hex;
use crate::model::{InstrumentObservation, TickerObservation, UniverseIdentity, UniverseMode};
use crate::normalize::normalized_symbol;
use crate::worker::WorkerError;
use crate::DAY_MS;

/// Bybit returns non-crypto linear perpetuals through the same category. The
/// empty label is its ordinary crypto product and `innovation` its innovation
/// zone; every other `symbolType` is outside the strategy domain.
pub const CRYPTO_SYMBOL_TYPES: [&str; 2] = ["", "innovation"];

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct UniverseRules {
    pub exclude_symbols: Vec<String>,
    pub long: SleeveUniverseRule,
    pub carry: SleeveUniverseRule,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SleeveUniverseRule {
    /// Names below this 24-hour quote turnover never enter.
    pub min_turnover_24h_usdt: f64,
    /// Names younger than this never enter; an unknown launch time fails the
    /// gate whenever it is on.
    pub min_listing_age_days: i64,
    /// A name enters when its 24-hour turnover rank is at most this.
    pub enter_rank: usize,
    /// A member stays until its rank falls past this.
    pub leave_rank: usize,
}

impl UniverseRules {
    pub fn validate(&self) -> Result<(), WorkerError> {
        let excluded: BTreeSet<&str> = self.exclude_symbols.iter().map(String::as_str).collect();
        if excluded.len() != self.exclude_symbols.len()
            || self
                .exclude_symbols
                .iter()
                .any(|symbol| normalized_symbol(symbol).map_or(true, |clean| &clean != symbol))
        {
            return Err(WorkerError::config(
                "universe exclusions must be unique uppercase alphanumeric symbols",
            ));
        }
        for (label, rule) in [("long", &self.long), ("carry", &self.carry)] {
            if !rule.min_turnover_24h_usdt.is_finite()
                || rule.min_turnover_24h_usdt < 0.0
                || rule.min_listing_age_days < 0
                || rule.enter_rank == 0
                || rule.leave_rank < rule.enter_rank
            {
                return Err(WorkerError::config(format!(
                    "{label} universe rule needs a finite turnover floor, a nonnegative age, and enter_rank <= leave_rank"
                )));
            }
        }
        Ok(())
    }
}

pub struct UniverseInputs<'a> {
    pub environment: &'a str,
    pub endpoint: &'a str,
    pub snapshot_ts_ms: i64,
    pub available_at_ms: i64,
    pub instruments: &'a [InstrumentObservation],
    pub tickers: &'a [TickerObservation],
    /// The universe in force before this refresh, for the leave-rank
    /// hysteresis. `None` on a cold start.
    pub previous: Option<&'a UniverseIdentity>,
}

/// The identity a worker carries before its first refresh. It publishes
/// nothing while it holds this.
pub fn unresolved_universe(environment: &str, endpoint: &str) -> UniverseIdentity {
    UniverseIdentity {
        mode: UniverseMode::Current,
        environment: environment.to_owned(),
        endpoint: endpoint.to_owned(),
        snapshot_ts_ms: 0,
        available_at_ms: 0,
        artifact_sha256: sha256_hex(b""),
        file_sha256: sha256_hex(b""),
        symbols: Vec::new(),
        long_symbols: Vec::new(),
        carry_symbols: Vec::new(),
    }
}

pub fn universe_is_resolved(universe: &UniverseIdentity) -> bool {
    universe.snapshot_ts_ms > 0 && !universe.symbols.is_empty()
}

/// Whether two identities name the same tradable set and sleeve populations.
/// Clocks and input hashes differ on every refresh; membership is what the
/// worker's owned symbols follow.
pub fn same_membership(left: &UniverseIdentity, right: &UniverseIdentity) -> bool {
    left.symbols == right.symbols
        && left.long_symbols == right.long_symbols
        && left.carry_symbols == right.carry_symbols
}

fn in_crypto_perpetual_domain(row: &InstrumentObservation, excluded: &BTreeSet<&str>) -> bool {
    let symbol_type = row
        .symbol_type
        .as_deref()
        .map(|value| value.trim().to_ascii_lowercase())
        .unwrap_or_default();
    row.status.as_deref() == Some("Trading")
        && row.settle_coin.as_deref() == Some("USDT")
        && row.contract_type.as_deref() == Some("LinearPerpetual")
        && !row.is_prelisting
        && row.delivery_time_ms.is_none_or(|clock| clock <= 0)
        && CRYPTO_SYMBOL_TYPES.contains(&symbol_type.as_str())
        && !excluded.contains(row.symbol.as_str())
}

fn eligible(
    rule: &SleeveUniverseRule,
    ranked: &[(usize, &str, f64)],
    age_days: &BTreeMap<&str, Option<f64>>,
    previous: &BTreeSet<&str>,
) -> Vec<String> {
    let mut out = Vec::new();
    for (rank, symbol, turnover) in ranked {
        if *turnover < rule.min_turnover_24h_usdt {
            continue;
        }
        if rule.min_listing_age_days > 0
            && !age_days
                .get(symbol)
                .copied()
                .flatten()
                .is_some_and(|age| age >= rule.min_listing_age_days as f64)
        {
            continue;
        }
        let inside =
            *rank <= rule.enter_rank || (previous.contains(symbol) && *rank <= rule.leave_rank);
        if inside {
            out.push((*symbol).to_owned());
        }
    }
    out.sort();
    out
}

pub fn derive_universe(
    rules: &UniverseRules,
    inputs: UniverseInputs<'_>,
) -> Result<UniverseIdentity, WorkerError> {
    rules.validate()?;
    if inputs.snapshot_ts_ms <= 0 || inputs.available_at_ms < inputs.snapshot_ts_ms {
        return Err(WorkerError::input("universe refresh clock is invalid"));
    }
    if !matches!(inputs.environment, "demo" | "mainnet") || inputs.endpoint.trim().is_empty() {
        return Err(WorkerError::input("universe realm or endpoint is invalid"));
    }
    let excluded: BTreeSet<&str> = rules.exclude_symbols.iter().map(String::as_str).collect();
    let domain: BTreeMap<&str, &InstrumentObservation> = inputs
        .instruments
        .iter()
        .filter(|row| in_crypto_perpetual_domain(row, &excluded))
        .map(|row| (row.symbol.as_str(), row))
        .collect();
    let mut turnover: BTreeMap<&str, f64> = BTreeMap::new();
    for ticker in inputs.tickers {
        if !domain.contains_key(ticker.symbol.as_str()) {
            continue;
        }
        if let Some(value) = ticker
            .turnover_24h
            .filter(|value| value.is_finite() && *value >= 0.0)
        {
            turnover.insert(ticker.symbol.as_str(), value);
        }
    }
    if turnover.is_empty() {
        return Err(WorkerError::input(
            "universe refresh found no tradable crypto perpetual with a ticker",
        ));
    }
    let mut ranked: Vec<(usize, &str, f64)> = turnover
        .iter()
        .map(|(symbol, value)| (0, *symbol, *value))
        .collect();
    ranked.sort_by(|a, b| b.2.total_cmp(&a.2).then_with(|| a.1.cmp(b.1)));
    for (index, row) in ranked.iter_mut().enumerate() {
        row.0 = index + 1;
    }
    let age_days: BTreeMap<&str, Option<f64>> = turnover
        .keys()
        .map(|symbol| {
            let age = domain[symbol]
                .launch_time_ms
                .filter(|launch| *launch > 0)
                .map(|launch| (inputs.snapshot_ts_ms - launch) as f64 / DAY_MS as f64);
            (*symbol, age)
        })
        .collect();
    let previous_long: BTreeSet<&str> = inputs
        .previous
        .map(|prior| prior.long_symbols.iter().map(String::as_str).collect())
        .unwrap_or_default();
    let previous_carry: BTreeSet<&str> = inputs
        .previous
        .map(|prior| prior.carry_symbols.iter().map(String::as_str).collect())
        .unwrap_or_default();
    let symbols: Vec<String> = turnover.keys().map(|symbol| (*symbol).to_owned()).collect();
    let long_symbols = eligible(&rules.long, &ranked, &age_days, &previous_long);
    let carry_symbols = eligible(&rules.carry, &ranked, &age_days, &previous_carry);
    if long_symbols.is_empty() || carry_symbols.is_empty() {
        return Err(WorkerError::input(
            "universe refresh left a sleeve with no eligible symbol",
        ));
    }
    let input_rows: Vec<serde_json::Value> = turnover
        .keys()
        .map(|symbol| {
            let row = domain[symbol];
            serde_json::json!([
                symbol,
                row.status,
                row.settle_coin,
                row.contract_type,
                row.symbol_type,
                row.launch_time_ms,
                row.delivery_time_ms,
                row.is_prelisting,
                turnover[symbol],
            ])
        })
        .collect();
    let file_sha256 = sha256_hex(
        &serde_json::to_vec(&serde_json::json!({
            "endpoint": inputs.endpoint,
            "rows": input_rows,
        }))
        .map_err(|error| WorkerError::json("encode universe inputs", error))?,
    );
    let artifact_sha256 = sha256_hex(
        &serde_json::to_vec(&serde_json::json!({
            "environment": inputs.environment,
            "endpoint": inputs.endpoint,
            "rules": rules,
            "symbols": symbols,
            "long_symbols": long_symbols,
            "carry_symbols": carry_symbols,
        }))
        .map_err(|error| WorkerError::json("encode universe identity", error))?,
    );
    Ok(UniverseIdentity {
        mode: UniverseMode::Current,
        environment: inputs.environment.to_owned(),
        endpoint: inputs.endpoint.to_owned(),
        snapshot_ts_ms: inputs.snapshot_ts_ms,
        available_at_ms: inputs.available_at_ms,
        artifact_sha256,
        file_sha256,
        symbols,
        long_symbols,
        carry_symbols,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rules() -> UniverseRules {
        UniverseRules {
            exclude_symbols: vec!["USDCUSDT".into()],
            long: SleeveUniverseRule {
                min_turnover_24h_usdt: 2_000_000.0,
                min_listing_age_days: 30,
                enter_rank: 2,
                leave_rank: 3,
            },
            carry: SleeveUniverseRule {
                min_turnover_24h_usdt: 0.0,
                min_listing_age_days: 7,
                enter_rank: 3,
                leave_rank: 4,
            },
        }
    }

    fn instrument(symbol: &str, age_days: i64) -> InstrumentObservation {
        InstrumentObservation {
            symbol: symbol.into(),
            observed_ts_ms: 1000 * DAY_MS,
            available_at_ms: 1000 * DAY_MS,
            contract_type: Some("LinearPerpetual".into()),
            symbol_type: None,
            status: Some("Trading".into()),
            base_coin: None,
            quote_coin: Some("USDT".into()),
            settle_coin: Some("USDT".into()),
            launch_time_ms: Some(1000 * DAY_MS - age_days * DAY_MS),
            delivery_time_ms: None,
            tick_size: None,
            qty_step: None,
            min_order_qty: None,
            min_notional_value: None,
            max_order_qty: None,
            max_market_order_qty: None,
            funding_interval_min: None,
            is_prelisting: false,
        }
    }

    fn ticker(symbol: &str, turnover: f64) -> TickerObservation {
        TickerObservation {
            symbol: symbol.into(),
            observed_ts_ms: 1000 * DAY_MS,
            available_at_ms: 1000 * DAY_MS,
            mark_observed_ts_ms: None,
            funding_observed_ts_ms: None,
            schedule_observed_ts_ms: None,
            last_price: None,
            mark_price: None,
            index_price: None,
            bid1_price: None,
            ask1_price: None,
            bid1_size: None,
            ask1_size: None,
            open_interest: None,
            open_interest_value: None,
            turnover_24h: Some(turnover),
            volume_24h: None,
            funding_rate: None,
            next_funding_time_ms: None,
        }
    }

    fn derive(
        instruments: &[InstrumentObservation],
        tickers: &[TickerObservation],
        previous: Option<&UniverseIdentity>,
    ) -> Result<UniverseIdentity, WorkerError> {
        derive_universe(
            &rules(),
            UniverseInputs {
                environment: "demo",
                endpoint: "api-demo.bybit.com",
                snapshot_ts_ms: 1000 * DAY_MS,
                available_at_ms: 1000 * DAY_MS + 5,
                instruments,
                tickers,
                previous,
            },
        )
    }

    #[test]
    fn domain_gates_and_rank_boundaries_shape_the_populations() {
        let mut stock = instrument("AALUSDT", 400);
        stock.symbol_type = Some("stock".into());
        let mut dated = instrument("BTC-26DEC25", 400);
        dated.contract_type = Some("LinearFutures".into());
        dated.delivery_time_ms = Some(300 * DAY_MS);
        let mut closed = instrument("DEADUSDT", 400);
        closed.status = Some("Closed".into());
        let instruments = vec![
            instrument("AAAUSDT", 400),
            instrument("BBBUSDT", 400),
            instrument("CCCUSDT", 400),
            instrument("YOUNGUSDT", 5),
            instrument("THINUSDT", 400),
            instrument("USDCUSDT", 400),
            instrument("NOTICKUSDT", 400),
            stock,
            dated,
            closed,
        ];
        let tickers = vec![
            ticker("AAAUSDT", 9e6),
            ticker("BBBUSDT", 8e6),
            ticker("YOUNGUSDT", 7e6),
            ticker("CCCUSDT", 6e6),
            ticker("THINUSDT", 1e6),
            ticker("USDCUSDT", 5e9),
            ticker("AALUSDT", 5e9),
            ticker("DEADUSDT", 5e9),
        ];
        let universe = derive(&instruments, &tickers, None).unwrap();
        assert_eq!(
            universe.symbols,
            ["AAAUSDT", "BBBUSDT", "CCCUSDT", "THINUSDT", "YOUNGUSDT"]
        );
        // Ranks by turnover: AAA 1, BBB 2, YOUNG 3, CCC 4, THIN 5. LONG enters
        // at rank 2 with a 30-day age floor; CARRY enters at rank 3 with a
        // 7-day floor, so the 5-day-old name is out of both.
        assert_eq!(universe.long_symbols, ["AAAUSDT", "BBBUSDT"]);
        assert_eq!(universe.carry_symbols, ["AAAUSDT", "BBBUSDT"]);
        assert_eq!(universe.mode, UniverseMode::Current);
        assert!(universe_is_resolved(&universe));
        assert_eq!(universe.artifact_sha256.len(), 64);
        assert_ne!(universe.artifact_sha256, universe.file_sha256);
    }

    #[test]
    fn a_member_stays_until_it_falls_past_the_leave_rank() {
        let instruments = vec![
            instrument("AAAUSDT", 400),
            instrument("BBBUSDT", 400),
            instrument("CCCUSDT", 400),
            instrument("DDDUSDT", 400),
            instrument("EEEUSDT", 400),
        ];
        let first = derive(
            &instruments,
            &[
                ticker("AAAUSDT", 9e6),
                ticker("BBBUSDT", 8e6),
                ticker("CCCUSDT", 7e6),
                ticker("DDDUSDT", 6e6),
                ticker("EEEUSDT", 5e6),
            ],
            None,
        )
        .unwrap();
        assert_eq!(first.long_symbols, ["AAAUSDT", "BBBUSDT"]);
        // BBB slips to rank 3: inside the leave rank, so it stays; DDD at
        // rank 2 enters.
        let second = derive(
            &instruments,
            &[
                ticker("AAAUSDT", 9e6),
                ticker("DDDUSDT", 8e6),
                ticker("BBBUSDT", 7e6),
                ticker("CCCUSDT", 6e6),
                ticker("EEEUSDT", 5e6),
            ],
            Some(&first),
        )
        .unwrap();
        assert_eq!(second.long_symbols, ["AAAUSDT", "BBBUSDT", "DDDUSDT"]);
        // BBB at rank 4 is past the leave rank and goes.
        let third = derive(
            &instruments,
            &[
                ticker("AAAUSDT", 9e6),
                ticker("DDDUSDT", 8e6),
                ticker("CCCUSDT", 7e6),
                ticker("BBBUSDT", 6e6),
                ticker("EEEUSDT", 5e6),
            ],
            Some(&second),
        )
        .unwrap();
        assert_eq!(third.long_symbols, ["AAAUSDT", "DDDUSDT"]);
        // Without the hysteresis the same input would already have dropped BBB
        // in the second refresh.
        let cold = derive(
            &instruments,
            &[
                ticker("AAAUSDT", 9e6),
                ticker("DDDUSDT", 8e6),
                ticker("BBBUSDT", 7e6),
                ticker("CCCUSDT", 6e6),
                ticker("EEEUSDT", 5e6),
            ],
            None,
        )
        .unwrap();
        assert_eq!(cold.long_symbols, ["AAAUSDT", "DDDUSDT"]);
        assert!(!same_membership(&second, &third));
    }

    #[test]
    fn identity_hashes_follow_membership_and_inputs() {
        let instruments = vec![instrument("AAAUSDT", 400), instrument("BBBUSDT", 400)];
        let a = derive(
            &instruments,
            &[ticker("AAAUSDT", 9e6), ticker("BBBUSDT", 8e6)],
            None,
        )
        .unwrap();
        let b = derive(
            &instruments,
            &[ticker("AAAUSDT", 9e6), ticker("BBBUSDT", 8e6)],
            None,
        )
        .unwrap();
        assert_eq!(a.artifact_sha256, b.artifact_sha256);
        assert_eq!(a.file_sha256, b.file_sha256);
        let c = derive(
            &instruments,
            &[ticker("AAAUSDT", 9e6), ticker("BBBUSDT", 7e6)],
            None,
        )
        .unwrap();
        assert_eq!(a.artifact_sha256, c.artifact_sha256);
        assert_ne!(a.file_sha256, c.file_sha256);
    }

    #[test]
    fn an_empty_venue_page_or_a_starved_sleeve_is_refused() {
        assert!(derive(&[], &[], None).is_err());
        let instruments = vec![instrument("AAAUSDT", 1)];
        assert!(derive(&instruments, &[ticker("AAAUSDT", 9e6)], None).is_err());
        assert!(!universe_is_resolved(&unresolved_universe(
            "demo",
            "api-demo.bybit.com"
        )));
    }

    #[test]
    fn rules_refuse_backwards_ranks_and_dirty_exclusions() {
        let mut bad = rules();
        bad.long.leave_rank = 1;
        assert!(bad.validate().is_err());
        let mut dirty = rules();
        dirty.exclude_symbols = vec!["usdc".into()];
        assert!(dirty.validate().is_err());
        assert!(rules().validate().is_ok());
    }
}
