//! Bybit as the worker's public data source: the v5 market REST endpoints and
//! the public linear WebSocket. Its wire shape is the neutral one every other
//! venue module converts into; see [`crate::venue`].

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::Semaphore;

use crate::config::SignalWorkerConfig;
use crate::http::{percent_encode, wall_ms, PublicHttpClient};
use crate::model::{BybitFundingWire, BybitInstrumentWire, BybitTickerWire};
use crate::normalize::{normalize_funding_rows, normalize_kline_rows};
use crate::worker::WorkerError;
use crate::HOUR_MS;

use super::{
    source_grid_slots, validate_fetched_instruments, validate_fetched_tickers,
    validate_source_grid_timestamp, validate_source_page_rows, wire_i64, wire_object_map,
    wire_text, BoxFuture, FetchedFundingRows, FetchedInstruments, FetchedKlineRows, FetchedTickers,
    PublicStream, PublicVenue, PublicVenueKind, StreamContinuity,
};

mod stream;

pub use stream::BybitPublicStream;

pub struct BybitPublicVenue {
    /// Market data: tickers, klines and funding history from the public
    /// mainnet host. Demo execution deliberately observes mainnet.
    market: PublicHttpClient,
    /// The realm's own venue host, whose instrument list bounds what the
    /// account may trade. Mainnet's is the public host; demo's is the demo
    /// venue.
    instruments: PublicHttpClient,
    category: String,
    request_timeout_ms: u64,
    retry_base_ms: u64,
}

impl BybitPublicVenue {
    pub fn open(
        config: &SignalWorkerConfig,
        request_budget: Arc<Semaphore>,
    ) -> Result<Self, WorkerError> {
        let market_host = match config.live.public_market_realm.as_str() {
            "mainnet" => &config.sources.bybit_mainnet_host,
            _ => return Err(WorkerError::config("unsupported public market realm")),
        };
        Ok(Self {
            market: PublicHttpClient::new(
                market_host,
                config.live.request_timeout_ms,
                config.live.request_retries,
                config.live.retry_base_ms,
                Arc::clone(&request_budget),
            )?,
            instruments: PublicHttpClient::new(
                crate::worker::realm_endpoint(config),
                config.live.request_timeout_ms,
                config.live.request_retries,
                config.live.retry_base_ms,
                request_budget,
            )?,
            category: config.sources.bybit_category.clone(),
            request_timeout_ms: config.live.request_timeout_ms,
            retry_base_ms: config.live.retry_base_ms,
        })
    }

    /// Both hosts pointed at one local stand-in venue.
    #[cfg(test)]
    pub(crate) fn for_http_test(client: PublicHttpClient, category: &str) -> Self {
        Self {
            market: client.clone(),
            instruments: client,
            category: category.to_owned(),
            request_timeout_ms: 1_000,
            retry_base_ms: 1,
        }
    }
}

impl PublicVenue for BybitPublicVenue {
    fn kind(&self) -> PublicVenueKind {
        PublicVenueKind::Bybit
    }

    fn instruments(
        &self,
        max_pages: usize,
    ) -> BoxFuture<'_, Result<FetchedInstruments, WorkerError>> {
        let client = self.instruments.clone();
        let category = self.category.clone();
        Box::pin(async move { fetch_instrument_snapshot(client, category, max_pages).await })
    }

    fn ticker_page(&self) -> BoxFuture<'_, Result<FetchedTickers, WorkerError>> {
        let client = self.market.clone();
        let category = self.category.clone();
        Box::pin(async move { fetch_ticker_page(client, category).await })
    }

    fn ticker_snapshot(
        &self,
        allowed: BTreeSet<String>,
    ) -> BoxFuture<'_, Result<FetchedTickers, WorkerError>> {
        let client = self.market.clone();
        let category = self.category.clone();
        Box::pin(async move { fetch_ticker_snapshot(client, category, allowed).await })
    }

    fn klines(
        &self,
        symbol: &str,
        start_ms: i64,
        end_ms: i64,
        page_limit: usize,
    ) -> BoxFuture<'_, Result<FetchedKlineRows, WorkerError>> {
        let client = self.market.clone();
        let category = self.category.clone();
        let symbol = symbol.to_owned();
        Box::pin(async move {
            fetch_klines(client, &category, page_limit, &symbol, start_ms, end_ms).await
        })
    }

    fn funding(
        &self,
        symbol: &str,
        start_ms: i64,
        end_ms: i64,
        page_limit: usize,
        interval_hours: Option<i64>,
    ) -> BoxFuture<'_, Result<FetchedFundingRows, WorkerError>> {
        let client = self.market.clone();
        let category = self.category.clone();
        let symbol = symbol.to_owned();
        Box::pin(async move {
            fetch_funding(
                client,
                &category,
                page_limit,
                &symbol,
                start_ms,
                end_ms,
                interval_hours,
            )
            .await
        })
    }

    fn open_stream(&self, symbols: Vec<String>) -> Result<Box<dyn PublicStream>, WorkerError> {
        Ok(Box::new(BybitPublicStream::spawn(
            symbols,
            self.request_timeout_ms,
            self.retry_base_ms,
        )?))
    }

    fn open_stream_continuing(
        &self,
        symbols: Vec<String>,
        continuity: StreamContinuity,
    ) -> Result<Box<dyn PublicStream>, WorkerError> {
        Ok(Box::new(BybitPublicStream::spawn_continuing(
            symbols,
            self.request_timeout_ms,
            self.retry_base_ms,
            continuity,
        )?))
    }
}

async fn fetch_instrument_snapshot(
    client: PublicHttpClient,
    category: String,
    max_pages: usize,
) -> Result<FetchedInstruments, WorkerError> {
    let observed_ts_ms = wall_ms()?;
    let mut by_symbol = BTreeMap::new();
    let mut available_at_ms = observed_ts_ms;
    for status in ["Closed", "Delivering", "Trading"] {
        let mut cursor: Option<String> = None;
        for _ in 0..max_pages {
            let mut query = format!(
                "category={}&status={status}&limit=1000",
                percent_encode(&category)
            );
            if let Some(value) = &cursor {
                query.push_str("&cursor=");
                query.push_str(&percent_encode(value));
            }
            let (payload, received) = client.get("/v5/market/instruments-info", &query).await?;
            available_at_ms = available_at_ms.max(received);
            let result = bybit_result(&payload)?;
            for value in result_list(result)? {
                let row = instrument_wire(value)?;
                by_symbol.insert(row.symbol.to_ascii_uppercase(), row);
            }
            let next = result
                .get("nextPageCursor")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_owned);
            if next.is_none() {
                cursor = None;
                break;
            }
            if next == cursor {
                return Err(WorkerError::network(format!(
                    "Bybit {status} instruments cursor did not advance"
                )));
            }
            cursor = next;
        }
        if cursor.is_some() {
            return Err(WorkerError::network(format!(
                "Bybit {status} instruments pagination exceeded configured page bound"
            )));
        }
    }
    let fetched = FetchedInstruments {
        observed_ts_ms,
        available_at_ms,
        rows: by_symbol.into_values().collect(),
    };
    validate_fetched_instruments(&fetched)?;
    Ok(fetched)
}

/// The whole ticker page, unfiltered: the universe ranks every listed name.
async fn fetch_ticker_page(
    client: PublicHttpClient,
    category: String,
) -> Result<FetchedTickers, WorkerError> {
    let request_started_at_ms = wall_ms()?;
    let query = format!("category={}", percent_encode(&category));
    let (payload, available_at_ms) = client.get("/v5/market/tickers", &query).await?;
    let rows = result_list(bybit_result(&payload)?)?
        .iter()
        .map(ticker_wire)
        .collect::<Result<Vec<_>, _>>()?;
    let fetched = FetchedTickers {
        request_started_at_ms,
        observed_ts_ms: available_at_ms,
        available_at_ms,
        rows,
    };
    validate_fetched_tickers(&fetched)?;
    Ok(fetched)
}

async fn fetch_ticker_snapshot(
    client: PublicHttpClient,
    category: String,
    allowed: BTreeSet<String>,
) -> Result<FetchedTickers, WorkerError> {
    let request_started_at_ms = wall_ms()?;
    let query = format!("category={}", percent_encode(&category));
    let (payload, available_at_ms) = client.get("/v5/market/tickers", &query).await?;
    let rows = result_list(bybit_result(&payload)?)?
        .iter()
        .filter(|value| {
            value
                .get("symbol")
                .and_then(Value::as_str)
                .is_some_and(|symbol| allowed.contains(&symbol.to_ascii_uppercase()))
        })
        .map(ticker_wire)
        .collect::<Result<Vec<_>, _>>()?;
    let fetched = FetchedTickers {
        request_started_at_ms,
        observed_ts_ms: available_at_ms,
        available_at_ms,
        rows,
    };
    validate_fetched_tickers(&fetched)?;
    Ok(fetched)
}

async fn fetch_klines(
    client: PublicHttpClient,
    category: &str,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
) -> Result<FetchedKlineRows, WorkerError> {
    let page_row_cap = page_limit;
    let retained_row_cap = source_grid_slots(start, end, HOUR_MS, false)?;
    let limit = i64::try_from(page_limit)
        .map_err(|_| WorkerError::config("kline page limit exceeds i64"))?;
    let span = (limit - 1).max(0) * HOUR_MS;
    let mut cursor = start;
    let mut by_time = BTreeMap::<i64, Vec<Value>>::new();
    let mut available = start;
    while cursor < end {
        let window_end = (cursor + span).min(end - HOUR_MS);
        let query = format!(
            "category={}&symbol={}&interval=60&start={cursor}&end={window_end}&limit={limit}",
            percent_encode(category),
            percent_encode(symbol),
        );
        let mut list = Vec::new();
        for _ in 0..2 {
            let (payload, received) = client.get("/v5/market/kline", &query).await?;
            available = available.max(received);
            list = result_list(bybit_result(&payload)?)?.to_vec();
            validate_source_page_rows(list.len(), page_row_cap, "Bybit kline")?;
            if !list.is_empty() {
                break;
            }
        }
        for value in list {
            let row = value
                .as_array()
                .ok_or_else(|| WorkerError::network("Bybit kline row is not an array"))?
                .clone();
            let ts = wire_i64(row.first(), "Bybit kline timestamp")?;
            validate_source_grid_timestamp(
                ts,
                start,
                end,
                HOUR_MS,
                false,
                "Bybit kline timestamp",
            )?;
            match by_time.get(&ts) {
                Some(existing) if existing != &row => {
                    return Err(WorkerError::network(
                        "Bybit kline pagination returned conflicting duplicate",
                    ));
                }
                Some(_) => {}
                None => {
                    if by_time.len() >= retained_row_cap {
                        return Err(WorkerError::network(
                            "Bybit kline response exceeded the requested grid cardinality",
                        ));
                    }
                    by_time.insert(ts, row);
                }
            }
        }
        cursor = window_end.saturating_add(HOUR_MS);
    }
    let rows = by_time.into_values().collect::<Vec<_>>();
    normalize_kline_rows(symbol, available, &rows)?;
    Ok((rows, available))
}

#[allow(clippy::too_many_arguments)]
async fn fetch_funding(
    client: PublicHttpClient,
    category: &str,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
    interval_hours: Option<i64>,
) -> Result<FetchedFundingRows, WorkerError> {
    let page_row_cap = page_limit;
    let retained_row_cap = source_grid_slots(start, end, HOUR_MS, true)?;
    let interval = interval_hours.map(Value::from);
    let mut cursor = start;
    let mut available = start;
    let mut by_time = BTreeMap::new();
    let page_limit = i64::try_from(page_limit)
        .map_err(|_| WorkerError::config("funding page limit exceeds i64"))?;
    let window_span = (page_limit - 1).max(0) * HOUR_MS;
    while cursor <= end {
        let window_end = cursor.saturating_add(window_span).min(end);
        let query = format!(
            "category={}&symbol={}&startTime={cursor}&endTime={window_end}&limit={page_limit}",
            percent_encode(category),
            percent_encode(symbol),
        );
        let (payload, received) = client.get("/v5/market/funding/history", &query).await?;
        available = available.max(received);
        let list = result_list(bybit_result(&payload)?)?;
        validate_source_page_rows(list.len(), page_row_cap, "Bybit funding")?;
        for value in list {
            let timestamp = value
                .get("fundingRateTimestamp")
                .cloned()
                .ok_or_else(|| WorkerError::network("Bybit funding row lacks timestamp"))?;
            let rate = value
                .get("fundingRate")
                .cloned()
                .ok_or_else(|| WorkerError::network("Bybit funding row lacks rate"))?;
            let key = wire_i64(Some(&timestamp), "Bybit funding timestamp")?;
            let row = BybitFundingWire {
                funding_rate_timestamp: timestamp,
                funding_rate: rate,
                funding_interval_hour: interval.clone(),
            };
            validate_source_grid_timestamp(
                key,
                start,
                end,
                HOUR_MS,
                true,
                "Bybit funding timestamp",
            )?;
            if !by_time.contains_key(&key) && by_time.len() >= retained_row_cap {
                return Err(WorkerError::network(
                    "Bybit funding response exceeded the requested grid cardinality",
                ));
            }
            if let Some(existing) = by_time.insert(key, row.clone()) {
                if existing != row {
                    return Err(WorkerError::network(
                        "Bybit funding pagination returned conflicting duplicate",
                    ));
                }
            }
        }
        if window_end == end {
            break;
        }
        cursor = window_end.saturating_add(1);
    }
    let rows = by_time.into_values().collect::<Vec<_>>();
    normalize_funding_rows(symbol, available, &rows)?;
    Ok((rows, available))
}

pub(crate) fn bybit_result(payload: &Value) -> Result<&Value, WorkerError> {
    if payload.get("retCode").and_then(Value::as_i64) != Some(0) {
        return Err(WorkerError::network(format!(
            "Bybit retCode={} retMsg={}",
            payload.get("retCode").unwrap_or(&Value::Null),
            payload.get("retMsg").unwrap_or(&Value::Null)
        )));
    }
    payload
        .get("result")
        .ok_or_else(|| WorkerError::network("Bybit response lacks result"))
}

pub(crate) fn result_list(result: &Value) -> Result<&Vec<Value>, WorkerError> {
    result
        .get("list")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::network("Bybit result lacks list"))
}

pub(crate) fn instrument_wire(value: &Value) -> Result<BybitInstrumentWire, WorkerError> {
    let symbol = value
        .get("symbol")
        .and_then(Value::as_str)
        .ok_or_else(|| WorkerError::network("Bybit instrument lacks symbol"))?
        .to_owned();
    Ok(BybitInstrumentWire {
        symbol,
        contract_type: wire_text(value, "contractType"),
        symbol_type: wire_text(value, "symbolType"),
        status: wire_text(value, "status"),
        base_coin: wire_text(value, "baseCoin"),
        quote_coin: wire_text(value, "quoteCoin"),
        settle_coin: wire_text(value, "settleCoin"),
        launch_time: value.get("launchTime").cloned(),
        delivery_time: value.get("deliveryTime").cloned(),
        price_filter: wire_object_map(value, "priceFilter")?,
        lot_size_filter: wire_object_map(value, "lotSizeFilter")?,
        funding_interval: value.get("fundingInterval").cloned(),
        is_pre_listing: value
            .get("isPreListing")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

pub(crate) fn ticker_wire(value: &Value) -> Result<BybitTickerWire, WorkerError> {
    let symbol = value
        .get("symbol")
        .and_then(Value::as_str)
        .ok_or_else(|| WorkerError::network("Bybit ticker lacks symbol"))?
        .to_ascii_uppercase();
    Ok(BybitTickerWire {
        symbol,
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: value.get("lastPrice").cloned(),
        mark_price: value.get("markPrice").cloned(),
        index_price: value.get("indexPrice").cloned(),
        bid1_price: value.get("bid1Price").cloned(),
        ask1_price: value.get("ask1Price").cloned(),
        bid1_size: value.get("bid1Size").cloned(),
        ask1_size: value.get("ask1Size").cloned(),
        open_interest: value.get("openInterest").cloned(),
        open_interest_value: value.get("openInterestValue").cloned(),
        turnover24h: value.get("turnover24h").cloned(),
        volume24h: value.get("volume24h").cloned(),
        funding_rate: value.get("fundingRate").cloned(),
        next_funding_time: value.get("nextFundingTime").cloned(),
    })
}
