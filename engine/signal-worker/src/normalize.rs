use std::collections::BTreeSet;

use serde_json::Value;

use crate::config::is_sha256;
use crate::model::{
    BinanceWhaleObservation, BinanceWhaleWire, BybitFundingWire, BybitInstrumentWire,
    BybitTickerWire, HourlyKline, InstrumentObservation, SettledFunding, TickerObservation,
    UniverseIdentity,
};
use crate::worker::WorkerError;
use crate::HOUR_MS;

pub fn normalize_kline_rows(
    symbol: &str,
    available_at_ms: i64,
    rows: &[Vec<Value>],
) -> Result<Vec<HourlyKline>, WorkerError> {
    let symbol = normalized_symbol(symbol)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        if row.len() != 7 {
            return Err(WorkerError::input(format!(
                "Bybit kline for {symbol} has {} fields, expected 7",
                row.len()
            )));
        }
        let open_ts_ms = value_i64(&row[0], "kline startTime")?;
        let open = value_f64(&row[1], "kline open")?;
        let high = value_f64(&row[2], "kline high")?;
        let low = value_f64(&row[3], "kline low")?;
        let close = value_f64(&row[4], "kline close")?;
        let volume_base = value_f64(&row[5], "kline volume")?;
        let turnover_quote = value_f64(&row[6], "kline turnover")?;
        if open_ts_ms <= 0
            || open_ts_ms % HOUR_MS != 0
            || available_at_ms < open_ts_ms + HOUR_MS
            || ![open, high, low, close].iter().all(|v| *v > 0.0)
            || high < open.max(close)
            || low > open.min(close)
            || low > high
            || volume_base < 0.0
            || turnover_quote < 0.0
        {
            return Err(WorkerError::input(format!(
                "invalid or not-yet-closed Bybit kline for {symbol} at {open_ts_ms}"
            )));
        }
        out.push(HourlyKline {
            symbol: symbol.clone(),
            open_ts_ms,
            available_at_ms,
            open,
            high,
            low,
            close,
            volume_base,
            turnover_quote,
        });
    }
    out.sort_by_key(|row| row.open_ts_ms);
    reject_duplicate_times(out.iter().map(|row| row.open_ts_ms), "kline")?;
    Ok(out)
}

pub fn normalize_funding_rows(
    symbol: &str,
    available_at_ms: i64,
    rows: &[BybitFundingWire],
) -> Result<Vec<SettledFunding>, WorkerError> {
    let symbol = normalized_symbol(symbol)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let settlement_ts_ms = value_i64(&row.funding_rate_timestamp, "funding timestamp")?;
        let rate = value_f64(&row.funding_rate, "funding rate")?;
        if settlement_ts_ms <= 0 || settlement_ts_ms > available_at_ms {
            return Err(WorkerError::input(format!(
                "funding print for {symbol} is unavailable at observation time"
            )));
        }
        let hours = row
            .funding_interval_hour
            .as_ref()
            .and_then(|value| value_i64(value, "funding interval").ok())
            .filter(|value| *value > 0)
            .unwrap_or(8);
        out.push(SettledFunding {
            symbol: symbol.clone(),
            settlement_ts_ms,
            available_at_ms,
            rate,
            funding_interval_min: hours * 60,
        });
    }
    out.sort_by_key(|row| row.settlement_ts_ms);
    reject_duplicate_times(out.iter().map(|row| row.settlement_ts_ms), "funding")?;
    Ok(out)
}

/// Rows the venue's instrument list carries that are not perpetual contracts
/// this worker can hold, or that fail a field check, with the reason. They
/// are left out of the table rather than costing the whole snapshot.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RejectedInstruments {
    pub rows: Vec<(String, String)>,
}

impl RejectedInstruments {
    pub fn summary(&self) -> Option<String> {
        if self.rows.is_empty() {
            return None;
        }
        let shown = self
            .rows
            .iter()
            .take(3)
            .map(|(symbol, reason)| format!("{symbol}: {reason}"))
            .collect::<Vec<_>>()
            .join("; ");
        Some(format!(
            "{} instrument row(s) left out of the table ({shown}{})",
            self.rows.len(),
            if self.rows.len() > 3 { "; …" } else { "" }
        ))
    }
}

pub fn normalize_instruments(
    observed_ts_ms: i64,
    available_at_ms: i64,
    rows: &[BybitInstrumentWire],
) -> Result<Vec<InstrumentObservation>, WorkerError> {
    normalize_instruments_reporting(observed_ts_ms, available_at_ms, rows).map(|(rows, _)| rows)
}

/// The table plus what was left out of it. Bybit's linear list carries dated
/// futures (`BTC-01DEC23`, `BTCUSDT-04SEP26`) beside the perpetuals, and
/// Closed contracts with zeroed filters; one such row must never refuse the
/// snapshot, because a refused snapshot leaves the worker with no table.
pub fn normalize_instruments_reporting(
    observed_ts_ms: i64,
    available_at_ms: i64,
    rows: &[BybitInstrumentWire],
) -> Result<(Vec<InstrumentObservation>, RejectedInstruments), WorkerError> {
    validate_observation_clock(observed_ts_ms, available_at_ms)?;
    let mut out = Vec::with_capacity(rows.len());
    let mut rejected = RejectedInstruments::default();
    for row in rows {
        match normalize_instrument_row(observed_ts_ms, available_at_ms, row) {
            Ok(normalized) => out.push(normalized),
            Err(error) => rejected.rows.push((row.symbol.clone(), error.to_string())),
        }
    }
    out.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    reject_duplicate_symbols(out.iter().map(|row| row.symbol.as_str()), "instrument")?;
    Ok((out, rejected))
}

fn normalize_instrument_row(
    observed_ts_ms: i64,
    available_at_ms: i64,
    row: &BybitInstrumentWire,
) -> Result<InstrumentObservation, WorkerError> {
    let symbol = normalized_symbol(&row.symbol)?;
    let price = &row.price_filter;
    let lot = &row.lot_size_filter;
    Ok(InstrumentObservation {
        symbol,
        observed_ts_ms,
        available_at_ms,
        contract_type: clean_text(row.contract_type.as_deref()),
        symbol_type: clean_text(row.symbol_type.as_deref()).map(|v| v.to_ascii_lowercase()),
        status: clean_text(row.status.as_deref()),
        base_coin: clean_text(row.base_coin.as_deref()),
        quote_coin: clean_text(row.quote_coin.as_deref()),
        settle_coin: clean_text(row.settle_coin.as_deref()),
        launch_time_ms: optional_i64(row.launch_time.as_ref(), "launchTime")?,
        delivery_time_ms: optional_i64(row.delivery_time.as_ref(), "deliveryTime")?,
        tick_size: positive_optional(price.get("tickSize"), "tickSize")?,
        qty_step: positive_optional(lot.get("qtyStep"), "qtyStep")?,
        min_order_qty: nonnegative_optional(lot.get("minOrderQty"), "minOrderQty")?,
        min_notional_value: nonnegative_optional(lot.get("minNotionalValue"), "minNotionalValue")?,
        max_order_qty: published_maximum(lot.get("maxOrderQty"), "maxOrderQty")?,
        max_market_order_qty: published_maximum(lot.get("maxMktOrderQty"), "maxMktOrderQty")?,
        funding_interval_min: optional_i64(row.funding_interval.as_ref(), "fundingInterval")?,
        is_prelisting: row.is_pre_listing,
    })
}

pub fn normalize_tickers(
    observed_ts_ms: i64,
    available_at_ms: i64,
    rows: &[BybitTickerWire],
) -> Result<Vec<TickerObservation>, WorkerError> {
    validate_observation_clock(observed_ts_ms, available_at_ms)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        out.push(TickerObservation {
            symbol: normalized_symbol(&row.symbol)?,
            observed_ts_ms,
            available_at_ms,
            mark_observed_ts_ms: ticker_field_clock(
                row.mark_observed_ts_ms,
                row.mark_price.is_some(),
                observed_ts_ms,
                available_at_ms,
            )?,
            funding_observed_ts_ms: ticker_field_clock(
                row.funding_observed_ts_ms,
                row.funding_rate.is_some(),
                observed_ts_ms,
                available_at_ms,
            )?,
            schedule_observed_ts_ms: ticker_field_clock(
                row.schedule_observed_ts_ms,
                row.next_funding_time.is_some(),
                observed_ts_ms,
                available_at_ms,
            )?,
            last_price: positive_optional(row.last_price.as_ref(), "lastPrice")?,
            mark_price: positive_optional(row.mark_price.as_ref(), "markPrice")?,
            index_price: positive_optional(row.index_price.as_ref(), "indexPrice")?,
            bid1_price: positive_optional(row.bid1_price.as_ref(), "bid1Price")?,
            ask1_price: positive_optional(row.ask1_price.as_ref(), "ask1Price")?,
            bid1_size: nonnegative_optional(row.bid1_size.as_ref(), "bid1Size")?,
            ask1_size: nonnegative_optional(row.ask1_size.as_ref(), "ask1Size")?,
            open_interest: nonnegative_optional(row.open_interest.as_ref(), "openInterest")?,
            open_interest_value: nonnegative_optional(
                row.open_interest_value.as_ref(),
                "openInterestValue",
            )?,
            turnover_24h: nonnegative_optional(row.turnover24h.as_ref(), "turnover24h")?,
            volume_24h: nonnegative_optional(row.volume24h.as_ref(), "volume24h")?,
            funding_rate: optional_f64(row.funding_rate.as_ref(), "fundingRate")?,
            next_funding_time_ms: optional_i64(row.next_funding_time.as_ref(), "nextFundingTime")?,
        });
    }
    out.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    reject_duplicate_symbols(out.iter().map(|row| row.symbol.as_str()), "ticker")?;
    Ok(out)
}

fn ticker_field_clock(
    explicit: Option<i64>,
    present: bool,
    fallback: i64,
    available_at_ms: i64,
) -> Result<Option<i64>, WorkerError> {
    if !present {
        return Ok(None);
    }
    let value = explicit.unwrap_or(fallback);
    if value <= 0 || value > available_at_ms {
        return Err(WorkerError::input(
            "ticker field freshness has an invalid clock",
        ));
    }
    Ok(Some(value))
}

pub fn normalize_whales(
    available_at_ms: i64,
    rows: &[BinanceWhaleWire],
) -> Result<Vec<BinanceWhaleObservation>, WorkerError> {
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let day_end_ms = value_i64(&row.day_end_ms, "whale day end")?;
        if day_end_ms <= 0 || day_end_ms % crate::DAY_MS != 0 || day_end_ms > available_at_ms {
            return Err(WorkerError::input(
                "whale day is not complete at availability time",
            ));
        }
        let ratio = optional_f64(row.long_short_ratio.as_ref(), "longShortRatio")?;
        if ratio.is_some_and(|value| value < 0.0) {
            return Err(WorkerError::input("negative whale long/short ratio"));
        }
        out.push(BinanceWhaleObservation {
            symbol: normalized_symbol(&row.symbol)?,
            day_end_ms,
            available_at_ms,
            long_short_ratio: ratio,
        });
    }
    out.sort_by(|a, b| (&a.symbol, a.day_end_ms).cmp(&(&b.symbol, b.day_end_ms)));
    let mut seen = BTreeSet::new();
    for row in &out {
        if !seen.insert((&row.symbol, row.day_end_ms)) {
            return Err(WorkerError::input("duplicate whale symbol-day"));
        }
    }
    Ok(out)
}

pub fn validate_universe(
    mut universe: UniverseIdentity,
    observed_ts_ms: i64,
) -> Result<UniverseIdentity, WorkerError> {
    if universe.snapshot_ts_ms <= 0
        || universe.available_at_ms < universe.snapshot_ts_ms
        || universe.available_at_ms > observed_ts_ms
        || !is_sha256(&universe.artifact_sha256)
        || !is_sha256(&universe.file_sha256)
        || universe.symbols.is_empty()
        || !matches!(universe.environment.as_str(), "demo" | "mainnet")
        || universe.endpoint.trim().is_empty()
    {
        return Err(WorkerError::input(
            "invalid universe identity or knowledge time",
        ));
    }
    universe.symbols = universe
        .symbols
        .iter()
        .map(|symbol| normalized_symbol(symbol))
        .collect::<Result<Vec<_>, _>>()?;
    universe.symbols.sort();
    universe.symbols.dedup();
    universe.long_symbols = universe
        .long_symbols
        .iter()
        .map(|symbol| normalized_symbol(symbol))
        .collect::<Result<Vec<_>, _>>()?;
    universe.carry_symbols = universe
        .carry_symbols
        .iter()
        .map(|symbol| normalized_symbol(symbol))
        .collect::<Result<Vec<_>, _>>()?;
    universe.long_symbols.sort();
    universe.long_symbols.dedup();
    universe.carry_symbols.sort();
    universe.carry_symbols.dedup();
    let all: BTreeSet<&str> = universe.symbols.iter().map(String::as_str).collect();
    if universe.long_symbols.is_empty()
        || universe.carry_symbols.is_empty()
        || universe
            .long_symbols
            .iter()
            .chain(&universe.carry_symbols)
            .any(|symbol| !all.contains(symbol.as_str()))
    {
        return Err(WorkerError::input(
            "universe profile symbols are empty or outside strategy instruments",
        ));
    }
    Ok(universe)
}

pub fn normalized_symbol(value: &str) -> Result<String, WorkerError> {
    let symbol = value.trim().to_ascii_uppercase();
    if symbol.is_empty() || !symbol.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
        return Err(WorkerError::input(format!("invalid symbol {value:?}")));
    }
    Ok(symbol)
}

fn validate_observation_clock(
    observed_ts_ms: i64,
    available_at_ms: i64,
) -> Result<(), WorkerError> {
    if observed_ts_ms <= 0 || available_at_ms < observed_ts_ms {
        return Err(WorkerError::input(
            "available_at_ms must be at or after a positive observed_ts_ms",
        ));
    }
    Ok(())
}

pub(crate) fn value_f64(value: &Value, label: &str) -> Result<f64, WorkerError> {
    let number = match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) if !text.trim().is_empty() => text.parse::<f64>().ok(),
        _ => None,
    }
    .ok_or_else(|| WorkerError::input(format!("{label} is not numeric")))?;
    if !number.is_finite() {
        return Err(WorkerError::input(format!("{label} is non-finite")));
    }
    Ok(number)
}

pub(crate) fn value_i64(value: &Value, label: &str) -> Result<i64, WorkerError> {
    match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) if !text.trim().is_empty() => text.parse::<i64>().ok(),
        _ => None,
    }
    .ok_or_else(|| WorkerError::input(format!("{label} is not an integer")))
}

fn optional_f64(value: Option<&Value>, label: &str) -> Result<Option<f64>, WorkerError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) if text.is_empty() => Ok(None),
        Some(value) => value_f64(value, label).map(Some),
    }
}

fn optional_i64(value: Option<&Value>, label: &str) -> Result<Option<i64>, WorkerError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) if text.is_empty() => Ok(None),
        Some(value) => value_i64(value, label).map(Some),
    }
}

fn positive_optional(value: Option<&Value>, label: &str) -> Result<Option<f64>, WorkerError> {
    let number = optional_f64(value, label)?;
    if number.is_some_and(|value| value <= 0.0) {
        return Err(WorkerError::input(format!("{label} is not positive")));
    }
    Ok(number)
}

/// An order-size maximum. Bybit publishes `"0"` here on Closed and Delivering
/// contracts; a zero maximum is no maximum.
fn published_maximum(value: Option<&Value>, label: &str) -> Result<Option<f64>, WorkerError> {
    Ok(optional_f64(value, label)?.filter(|value| *value > 0.0))
}

fn nonnegative_optional(value: Option<&Value>, label: &str) -> Result<Option<f64>, WorkerError> {
    let number = optional_f64(value, label)?;
    if number.is_some_and(|value| value < 0.0) {
        return Err(WorkerError::input(format!("{label} is negative")));
    }
    Ok(number)
}

fn clean_text(value: Option<&str>) -> Option<String> {
    value
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn reject_duplicate_times(
    times: impl IntoIterator<Item = i64>,
    label: &str,
) -> Result<(), WorkerError> {
    let mut seen = BTreeSet::new();
    for value in times {
        if !seen.insert(value) {
            return Err(WorkerError::input(format!("duplicate {label} timestamp")));
        }
    }
    Ok(())
}

fn reject_duplicate_symbols<'a>(
    symbols: impl IntoIterator<Item = &'a str>,
    label: &str,
) -> Result<(), WorkerError> {
    let mut seen = BTreeSet::new();
    for value in symbols {
        if !seen.insert(value) {
            return Err(WorkerError::input(format!("duplicate {label} symbol")));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        normalize_instruments, normalize_instruments_reporting, normalize_whales,
        validate_observation_clock,
    };
    use crate::model::{BinanceWhaleWire, BybitInstrumentWire};
    use crate::DAY_MS;
    use serde_json::Value;
    use std::collections::BTreeMap;

    fn instrument(symbol: &str, status: &str, delivery_time: &str) -> BybitInstrumentWire {
        BybitInstrumentWire {
            symbol: symbol.into(),
            contract_type: Some("LinearPerpetual".into()),
            symbol_type: None,
            status: Some(status.into()),
            base_coin: Some(symbol.trim_end_matches("USDT").into()),
            quote_coin: Some("USDT".into()),
            settle_coin: Some("USDT".into()),
            launch_time: Some(Value::from("1")),
            delivery_time: Some(Value::from(delivery_time)),
            price_filter: BTreeMap::from([("tickSize".to_owned(), Value::from("0.01"))]),
            lot_size_filter: BTreeMap::from([
                ("qtyStep".to_owned(), Value::from("0.001")),
                ("minOrderQty".to_owned(), Value::from("0.001")),
                ("maxOrderQty".to_owned(), Value::from("100")),
                ("maxMktOrderQty".to_owned(), Value::from("10")),
            ]),
            funding_interval: Some(Value::from("480")),
            is_pre_listing: false,
        }
    }

    /// The venue's own shape: a perpetual carries `deliveryTime: "0"`, and a
    /// Closed contract zeroes its maximums. Neither may cost the snapshot.
    #[test]
    fn a_closed_contract_with_zero_maximums_is_kept_beside_the_perpetuals() {
        let mut closed = instrument("OLDUSDT", "Closed", "1756684800000");
        closed
            .lot_size_filter
            .insert("maxOrderQty".to_owned(), Value::from("0"));
        closed
            .lot_size_filter
            .insert("maxMktOrderQty".to_owned(), Value::from("0"));
        let rows = normalize_instruments(
            DAY_MS,
            DAY_MS + 1,
            &[instrument("BTCUSDT", "Trading", "0"), closed],
        )
        .expect("a zero maximum is no maximum");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].symbol, "BTCUSDT");
        assert_eq!(rows[0].delivery_time_ms, Some(0));
        assert_eq!(rows[0].max_market_order_qty, Some(10.0));
        assert_eq!(rows[1].symbol, "OLDUSDT");
        assert_eq!(rows[1].max_order_qty, None);
        assert_eq!(rows[1].max_market_order_qty, None);
    }

    /// One bad row costs that row, named, and never the snapshot.
    #[test]
    fn a_dated_contract_or_a_broken_row_is_left_out_and_named() {
        let mut broken = instrument("ETHUSDT", "Trading", "0");
        broken
            .price_filter
            .insert("tickSize".to_owned(), Value::from("0"));
        let (rows, rejected) = normalize_instruments_reporting(
            DAY_MS,
            DAY_MS + 1,
            &[
                instrument("BTC-01DEC23", "Closed", "1701388800000"),
                instrument("BTCUSDT-04SEP26", "Trading", "1788508800000"),
                broken,
                instrument("BTCUSDT", "Trading", "0"),
            ],
        )
        .unwrap();
        assert_eq!(
            rows.iter()
                .map(|row| row.symbol.as_str())
                .collect::<Vec<_>>(),
            vec!["BTCUSDT"]
        );
        assert_eq!(rejected.rows.len(), 3);
        assert!(
            rejected.rows[0].1.contains("invalid symbol"),
            "{:?}",
            rejected.rows[0]
        );
        assert!(rejected.rows[2].1.contains("tickSize is not positive"));
        let summary = rejected.summary().unwrap();
        assert!(
            summary.starts_with("3 instrument row(s) left out"),
            "{summary}"
        );
        assert!(summary.contains("BTC-01DEC23"));
    }

    #[test]
    fn observation_precedes_response_availability() {
        validate_observation_clock(100, 110).expect("request observation is causal");
        assert!(validate_observation_clock(110, 100).is_err());
        assert!(validate_observation_clock(0, 100).is_err());
    }

    #[test]
    fn whale_rows_require_a_complete_utc_day_boundary() {
        let aligned = BinanceWhaleWire {
            symbol: "BTCUSDT".into(),
            day_end_ms: Value::from(2 * DAY_MS),
            long_short_ratio: Some(Value::from("1.2")),
        };
        assert!(normalize_whales(2 * DAY_MS + 1, std::slice::from_ref(&aligned)).is_ok());
        let mut misaligned = aligned;
        misaligned.day_end_ms = Value::from(2 * DAY_MS - 1);
        assert!(normalize_whales(2 * DAY_MS + 1, &[misaligned]).is_err());
    }
}
