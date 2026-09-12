//! The venue's contract list and funding schedule, read without credentials.
//!
//! The market-data feed needs to turn `BTCUSDT` into the `BTC_USDT` its
//! subscription names, and that mapping is public. It lives here rather than on
//! the gateway so the price side of this venue needs no key — which matters
//! more here than on the other venues, because MEXC's only realm is funded and
//! holding a key for it is not something a data-only unit should have to do.
//!
//! The mapping is read from the venue rather than derived by cutting the
//! engine's symbol at a known quote suffix: `USD`, `USDT`, `USDC` and `USD1`
//! are all live quote currencies here, and `PEPE_USDT`, `PEPE_USDC` and
//! `PEPE_USD1` all exist, so there is no cut that is right.
//!
//! The settlement schedule is here for the same reason: `push.ticker` carries a
//! funding rate but neither a settlement time nor a cycle, and the endpoint
//! that states both needs no key either.

use engine_types::ids::Symbol;
use engine_types::VenueError;
use serde::Deserialize;

use super::contracts::Contracts;
use super::realm::MexcRealm;
use crate::http::HttpClient;
use crate::numeric_wire::DecimalField;

const PATH_CONTRACT_DETAIL: &str = "/api/v1/contract/detail";
/// Asked with no symbol this answers every contract, as an array. The
/// per-symbol form `/funding_rate/{symbol}` answers one object instead, which
/// [`parse_funding`] does not read.
const PATH_FUNDING_RATE: &str = "/api/v1/contract/funding_rate";

const MS_PER_HOUR: f64 = 60.0 * 60.0 * 1000.0;

/// Every contract the venue lists, as (the engine's spelling, the venue's).
pub async fn symbol_map(realm: MexcRealm) -> Result<Vec<(Symbol, String)>, VenueError> {
    symbol_map_from(realm.rest_base()).await
}

/// The same read against a named host. Tests only.
pub async fn symbol_map_from(base_url: &str) -> Result<Vec<(Symbol, String)>, VenueError> {
    let http = HttpClient::new(base_url);
    let reply: Box<serde_json::value::RawValue> =
        http.get_as(PATH_CONTRACT_DETAIL, "", &[]).await?;
    Ok(Contracts::parse_raw(reply.get())?.symbol_pairs())
}

/// When one contract settles funding, as the venue states it.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct FundingSchedule {
    /// This contract's settlement period. `collectCycle` is published in
    /// hours, and 8, 4, 1 and 24 are all live, so there is no venue-wide one.
    pub collect_cycle_ms: i64,
    /// The venue's own next settlement, ms since the epoch. It is not an epoch
    /// multiple of the cycle: `US30_USDT` is a 24 h contract settling at 16:00
    /// UTC.
    pub next_settle_ms: i64,
}

/// Every contract's settlement schedule, by the venue's spelling of its symbol
/// — the spelling `push.ticker` names.
pub async fn funding_schedule(
    realm: MexcRealm,
) -> Result<Vec<(String, FundingSchedule)>, VenueError> {
    funding_schedule_from(realm.rest_base()).await
}

/// The same read against a named host. Tests only.
pub async fn funding_schedule_from(
    base_url: &str,
) -> Result<Vec<(String, FundingSchedule)>, VenueError> {
    let http = HttpClient::new(base_url);
    let reply: Box<serde_json::value::RawValue> = http.get_as(PATH_FUNDING_RATE, "", &[]).await?;
    parse_funding(reply.get())
}

/// Read `GET /api/v1/contract/funding_rate`, sorted by the venue's symbol.
pub fn parse_funding(raw: &str) -> Result<Vec<(String, FundingSchedule)>, VenueError> {
    #[derive(Deserialize)]
    struct Reply {
        data: Vec<Box<serde_json::value::RawValue>>,
    }
    let body: Reply = crate::numeric_wire::decode_object(raw)
        .map_err(|e| VenueError::BadReply(format!("funding rate: {e}")))?;
    let mut rows: Vec<(String, FundingSchedule)> = body
        .data
        .iter()
        .filter_map(|row| read_funding_row(row.get()))
        .collect();
    if rows.is_empty() {
        return Err(VenueError::BadReply(
            "funding rate stated no readable settlement times".into(),
        ));
    }
    rows.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(rows)
}

/// One row of `funding_rate`. Only the schedule is read; the rate itself
/// reaches the engine on the ticker channel.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct FundingRow {
    symbol: String,
    #[serde(default)]
    collect_cycle: DecimalField,
    #[serde(default)]
    next_settle_time: DecimalField,
}

/// A row missing its cycle or its settlement stamp is skipped rather than
/// defaulted. The feed reports 0 — "not stated" — for a symbol with no row,
/// and there is no venue-wide cycle a guess could come from.
fn read_funding_row(raw: &str) -> Option<(String, FundingSchedule)> {
    let row: FundingRow = crate::numeric_wire::decode_object(raw).ok()?;
    let hours = row.collect_cycle.compat_f64("collectCycle").ok()?;
    let next_settle = row.next_settle_time.compat_f64("nextSettleTime").ok()?;
    if !hours.is_finite() || hours <= 0.0 || !next_settle.is_finite() || next_settle <= 0.0 {
        return None;
    }
    let cycle = (hours * MS_PER_HOUR).round();
    if cycle > i64::MAX as f64 || next_settle > i64::MAX as f64 {
        return None;
    }
    Some((
        row.symbol,
        FundingSchedule {
            collect_cycle_ms: cycle as i64,
            next_settle_ms: next_settle as i64,
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Recorded from `GET /api/v1/contract/funding_rate` on the realm's own
    /// host, no symbol. Real bytes across every cycle the venue lists: 8 h,
    /// 4 h, 1 h and the one 24 h contract.
    const FUNDING: &str = r#"{"success":true,"code":0,"data":[
      {"symbol":"BTC_USDT","fundingRate":6.2e-05,"maxFundingRate":0.0018,"minFundingRate":-0.0018,
       "collectCycle":8,"nextSettleTime":1788969600000,"timestamp":1788950082772,
       "idxPrice":79129.8,"fairPrice":79090},
      {"symbol":"ETH_USDT","fundingRate":5.2e-05,"maxFundingRate":0.0018,"minFundingRate":-0.0018,
       "collectCycle":8,"nextSettleTime":1788969600000,"timestamp":1788950082772,
       "idxPrice":2492.89,"fairPrice":2491.66},
      {"symbol":"XAU_USDT","fundingRate":3.4e-05,"maxFundingRate":0.03,"minFundingRate":-0.03,
       "collectCycle":4,"nextSettleTime":1788955200000,"timestamp":1788950082772,
       "idxPrice":4397.34,"fairPrice":4399.08},
      {"symbol":"USOIL_USDT","fundingRate":-0.001084,"maxFundingRate":0.03,"minFundingRate":-0.03,
       "collectCycle":4,"nextSettleTime":1788955200000,"timestamp":1788950082772,
       "idxPrice":94.57,"fairPrice":94.28},
      {"symbol":"FORM_USDT","fundingRate":5e-05,"maxFundingRate":0.03,"minFundingRate":-0.03,
       "collectCycle":4,"nextSettleTime":1788955200000,"timestamp":1788950082772,
       "idxPrice":0.3031,"fairPrice":0.303},
      {"symbol":"SOPH_USDT","fundingRate":-0.00013,"maxFundingRate":0.03,"minFundingRate":-0.03,
       "collectCycle":1,"nextSettleTime":1788951600000,"timestamp":1788950082772,
       "idxPrice":0.00543,"fairPrice":0.00542},
      {"symbol":"NGAS_USDT","fundingRate":0.000211,"maxFundingRate":0.03,"minFundingRate":-0.03,
       "collectCycle":1,"nextSettleTime":1788951600000,"timestamp":1788950082772,
       "idxPrice":2.922,"fairPrice":2.922},
      {"symbol":"SPX500_USD1","fundingRate":6e-06,"maxFundingRate":0.03,"minFundingRate":-0.03,
       "collectCycle":1,"nextSettleTime":1788951600000,"timestamp":1788950082773,
       "idxPrice":7658.1,"fairPrice":7655.1},
      {"symbol":"US30_USDT","fundingRate":0,"maxFundingRate":0,"minFundingRate":0,
       "collectCycle":24,"nextSettleTime":1788969600000,"timestamp":1788950082772,
       "idxPrice":52548.46,"fairPrice":52551.36}]}"#;

    fn schedule(symbol: &str) -> FundingSchedule {
        parse_funding(FUNDING)
            .unwrap()
            .into_iter()
            .find(|(name, _)| name == symbol)
            .unwrap_or_else(|| panic!("{symbol} is not in the fixture"))
            .1
    }

    const HOUR: i64 = 60 * 60 * 1000;

    #[test]
    fn the_cycle_is_the_contracts_own_and_not_eight_hours_for_all_of_them() {
        assert_eq!(schedule("BTC_USDT").collect_cycle_ms, 8 * HOUR);
        assert_eq!(schedule("XAU_USDT").collect_cycle_ms, 4 * HOUR);
        assert_eq!(schedule("SOPH_USDT").collect_cycle_ms, HOUR);
        assert_eq!(schedule("US30_USDT").collect_cycle_ms, 24 * HOUR);
    }

    #[test]
    fn the_settlement_stamp_is_the_venues_own_not_an_epoch_multiple() {
        // US30_USDT settles every 24 h at 16:00 UTC, so a stamp derived from
        // the epoch and the cycle would be eight hours out.
        let us30 = schedule("US30_USDT");
        assert_eq!(us30.next_settle_ms, 1_788_969_600_000);
        assert_ne!(us30.next_settle_ms % us30.collect_cycle_ms, 0);
        // The 4 h contracts settle four hours before the 8 h ones do.
        assert_eq!(schedule("XAU_USDT").next_settle_ms, 1_788_955_200_000);
        assert_eq!(schedule("BTC_USDT").next_settle_ms, 1_788_969_600_000);
    }

    #[test]
    fn every_recorded_row_is_read_and_the_page_comes_back_sorted() {
        let rows = parse_funding(FUNDING).unwrap();
        assert_eq!(rows.len(), 9);
        let symbols: Vec<_> = rows.iter().map(|(symbol, _)| symbol.clone()).collect();
        let mut sorted = symbols.clone();
        sorted.sort();
        assert_eq!(symbols, sorted, "the page is not sorted by symbol");
    }

    #[test]
    fn a_stringified_number_is_read_and_a_duplicate_key_keeps_the_last_value() {
        let rows = parse_funding(
            r#"{"data":[{"symbol":"BTC_USDT","collectCycle":"4","nextSettleTime":"1788955200000"},
                 {"symbol":"ETH_USDT","collectCycle":8,"collectCycle":4,
                  "nextSettleTime":1788955200000}]}"#,
        )
        .unwrap();
        assert_eq!(rows[0].1.collect_cycle_ms, 4 * HOUR);
        assert_eq!(rows[0].1.next_settle_ms, 1_788_955_200_000);
        assert_eq!(rows[1].1.collect_cycle_ms, 4 * HOUR);
    }

    #[test]
    fn a_row_with_no_cycle_or_no_stamp_is_skipped_rather_than_assumed() {
        let rows = parse_funding(
            r#"{"data":[{"symbol":"A_USDT","nextSettleTime":1788955200000},
                 {"symbol":"B_USDT","collectCycle":4},
                 {"symbol":"C_USDT","collectCycle":0,"nextSettleTime":1788955200000},
                 {"symbol":"D_USDT","collectCycle":4,"nextSettleTime":0},
                 {"symbol":"E_USDT","collectCycle":"broken","nextSettleTime":1788955200000},
                 {"symbol":"BTC_USDT","collectCycle":8,"nextSettleTime":1788969600000}]}"#,
        )
        .unwrap();
        assert_eq!(
            rows.iter().map(|(s, _)| s.as_str()).collect::<Vec<_>>(),
            ["BTC_USDT"]
        );
    }

    #[test]
    fn a_page_with_nothing_readable_is_an_error_not_an_empty_schedule() {
        // An empty schedule reads as "the venue states no settlement time for
        // anything", which is a failed read rather than a venue fact.
        assert!(parse_funding(r#"{"success":true,"code":0,"data":[]}"#).is_err());
        assert!(parse_funding(r#"{"success":true,"code":0}"#).is_err());
        // The per-symbol form answers one object; this reader wants the array.
        assert!(parse_funding(
            r#"{"data":{"symbol":"BTC_USDT","collectCycle":8,"nextSettleTime":1788969600000}}"#
        )
        .is_err());
    }
}
