//! MEXC as the worker's public data source: the contract futures REST surface
//! and the `edge` public WebSocket, converted into the Bybit wire shape
//! [`crate::venue`] documents.
//!
//! # Contracts, not coins
//!
//! MEXC states every quantity in CONTRACTS, and one contract is
//! `contractSize` of the base coin — 0.0001 BTC, 1 XRP, 10000000 PEPE. The
//! wire is in base coin, so every quantity here is multiplied by that size
//! through [`round_clean`], the conversion `engine-public`'s own contract
//! table uses. No read works before the contract table is loaded, so the
//! table is read on the instrument lane and lazily on first use by any other.
//!
//! # What each wire field is read from
//!
//! | Wire | MEXC | Rule |
//! | --- | --- | --- |
//! | `symbol` | `baseCoin` + `quoteCoin` | the engine's spelling, built as `engine-public/src/venues/mexc/contracts.rs` builds it; the venue's own `BTC_USDT` is kept only to talk to it |
//! | `contractType` | `futureType`, `settleCoin`, `quoteCoin` | `LinearPerpetual` when `futureType == 1` and the contract settles in its quote coin; `InversePerpetual` when it settles in something else |
//! | `status` | `state`, `apiAllowed` | `Trading` when `state == 0` and `apiAllowed`; every other row is `Closed` |
//! | `launchTime` | `createTime` | ms |
//! | `priceFilter.tickSize` | `priceUnit` | quote per tick |
//! | `lotSizeFilter.qtyStep` | `volUnit` × `contractSize` | base coin |
//! | `lotSizeFilter.minOrderQty` | `minVol` × `contractSize` | base coin |
//! | `lotSizeFilter.maxOrderQty` | `limitMaxVol` × `contractSize` | the LIMIT ceiling, as `Contract::vol_for` reads it |
//! | `lotSizeFilter.maxMktOrderQty` | `maxVol` × `contractSize` | the MARKET ceiling, the same reading |
//! | `fundingInterval` | `collectCycle` × 60 | minutes; from `contract/funding_rate`, which answers for every contract in one request |
//! | `lastPrice`, `indexPrice` | `lastPrice`, `indexPrice` | verbatim |
//! | `markPrice` | `fairPrice` | the price the venue liquidates against |
//! | `bid1Price`, `ask1Price` | `bid1`, `ask1` | verbatim |
//! | `turnover24h` | `amount24` | quote, verbatim |
//! | `volume24h` | `volume24` × `contractSize` | base |
//! | `openInterest` | `holdVol` × `contractSize` | base |
//! | `fundingRate` | `fundingRate` | verbatim, per settlement, never rescaled |
//! | `nextFundingTime` | `nextSettleTime` rolled forward by `collectCycle` | the venue's own phase; see [`next_settlement_ms`] |
//! | kline `[0]` | `time` × 1000 | the venue answers in SECONDS |
//! | kline `[1..=4]` | `realOpen`, `realHigh`, `realLow`, `realClose` | the traded bar; `open/high/low/close` are the stitched series whose open is the previous bar's close |
//! | kline `[5]` | `vol` × `contractSize` | base |
//! | kline `[6]` | `amount` | quote, verbatim |
//! | funding `fundingRateTimestamp` | `settleTime` | ms, on the hour |
//! | funding `fundingIntervalHour` | that row's own `collectCycle` | the venue states the cycle per settlement |
//!
//! # What MEXC does not state, and what absence means
//!
//! | Wire | Why | Consequence |
//! | --- | --- | --- |
//! | `symbolType` | no product-class label a reader can trust; `type` is undocumented | absent reads as the venue's ordinary crypto product in `universe.rs`, so equity and index perps sit in the same domain as crypto ones |
//! | `deliveryTime` | `contract/detail` lists perpetuals only | absent passes the perpetual gate |
//! | `lotSizeFilter.minNotionalValue` | the venue's minimum is in contracts and is already in `minOrderQty` | `None`, no floor claimed |
//! | `bid1Size`, `ask1Size` | the ticker states a touch price and no size | `None`; nothing reads them |
//! | `openInterestValue` | the venue states open interest in contracts only | `None`; it is not a product this module invents, and no reducer reads it |
//! | `nextFundingTime` for a contract the funding page did not name | no settlement clock at all | `None`, so such a row reaches the reducers only on its mark price |

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use engine_public::MexcRealm;
use engine_types::quantize::round_clean;
use serde_json::Value;
use tokio::sync::Semaphore;

use crate::config::SignalWorkerConfig;
use crate::http::{percent_encode, wall_ms, PublicHttpClient};
use crate::model::{BybitFundingWire, BybitInstrumentWire, BybitTickerWire};
use crate::normalize::{normalize_funding_rows, normalize_kline_rows, value_f64, value_i64};
use crate::worker::WorkerError;
use crate::HOUR_MS;

use super::{
    source_grid_slots, validate_fetched_instruments, validate_fetched_tickers,
    validate_source_grid_timestamp, validate_source_page_rows, BoxFuture, FetchedFundingRows,
    FetchedInstruments, FetchedKlineRows, FetchedTickers, PublicStream, PublicVenue,
    PublicVenueKind, StreamContinuity,
};

mod stream;

pub use stream::MexcPublicStream;

const PATH_DETAIL: &str = "/api/v1/contract/detail";
const PATH_FUNDING: &str = "/api/v1/contract/funding_rate";
const PATH_FUNDING_HISTORY: &str = "/api/v1/contract/funding_rate/history";
const PATH_TICKER: &str = "/api/v1/contract/ticker";
const PATH_KLINE: &str = "/api/v1/contract/kline";

/// The venue's name for the hourly bar.
const KLINE_INTERVAL: &str = "Min60";

/// One `contract/kline` reply carries at most this many bars, newest first, so
/// a wider window silently loses its oldest end.
const MAX_KLINE_ROWS: usize = 2_000;

/// The venue's rate-limit refusal. It arrives inside an HTTP 200 envelope, so
/// the status alone never reveals it.
const RATE_LIMITED: i64 = 510;

/// The REST host without its scheme, which is what [`PublicHttpClient`] takes.
/// No venue address is written down here: it comes from the realm table in
/// `engine-public`, and REST and the WebSocket are deliberately on different
/// hosts.
pub fn rest_host() -> &'static str {
    MexcRealm::Mainnet
        .rest_base()
        .trim_start_matches("https://")
}

/// One contract in the fields every other read needs.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct ContractRow {
    /// The venue's spelling, `BTC_USDT`. What goes on the wire.
    pub(crate) venue_symbol: String,
    /// Base coin per contract. Every quantity conversion is this multiplier.
    pub(crate) contract_size: f64,
    /// The settlement interval in whole hours, the venue's `collectCycle`.
    pub(crate) cycle_hours: Option<i64>,
    /// The venue's own next settlement stamp, ms. Kept because it carries the
    /// phase: the cycles run 1, 4, 8 and 24 hours and one daily contract
    /// settles off the 24-hour grid, so no boundary can be derived from the
    /// epoch.
    pub(crate) next_settle_ms: Option<i64>,
}

/// Every contract the venue lists, in both spellings.
#[derive(Clone, Debug, Default)]
pub(crate) struct ContractTable {
    by_symbol: BTreeMap<String, ContractRow>,
    by_venue_symbol: BTreeMap<String, String>,
}

impl ContractTable {
    pub(crate) fn row(&self, symbol: &str) -> Option<&ContractRow> {
        self.by_symbol.get(symbol)
    }

    pub(crate) fn engine_symbol(&self, venue_symbol: &str) -> Option<&str> {
        self.by_venue_symbol.get(venue_symbol).map(String::as_str)
    }

    pub(crate) fn venue_symbol(&self, symbol: &str) -> Option<&str> {
        self.by_symbol
            .get(symbol)
            .map(|row| row.venue_symbol.as_str())
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.by_symbol.is_empty()
    }

    #[cfg(test)]
    pub(crate) fn len(&self) -> usize {
        self.by_symbol.len()
    }

    fn insert(&mut self, symbol: String, row: ContractRow) {
        self.by_venue_symbol
            .insert(row.venue_symbol.clone(), symbol.clone());
        self.by_symbol.insert(symbol, row);
    }
}

pub struct MexcPublicVenue {
    client: PublicHttpClient,
    request_timeout_ms: u64,
    retry_base_ms: u64,
    /// The contract sizes, venue spellings and settlement cycles. Read on the
    /// instrument lane; nothing else here converts a quantity without it.
    contracts: Mutex<Arc<ContractTable>>,
    /// One table read at a time, so a cold ticker lane and the instrument lane
    /// do not both page the venue.
    refresh: tokio::sync::Mutex<()>,
}

impl MexcPublicVenue {
    /// MEXC publishes one futures realm, so `live.public_market_realm` picks
    /// nothing here.
    pub fn open(
        config: &SignalWorkerConfig,
        request_budget: Arc<Semaphore>,
    ) -> Result<Self, WorkerError> {
        Ok(Self {
            client: PublicHttpClient::new(
                rest_host(),
                config.live.request_timeout_ms,
                config.live.request_retries,
                config.live.retry_base_ms,
                request_budget,
            )?,
            request_timeout_ms: config.live.request_timeout_ms,
            retry_base_ms: config.live.retry_base_ms,
            contracts: Mutex::new(Arc::new(ContractTable::default())),
            refresh: tokio::sync::Mutex::new(()),
        })
    }

    #[cfg(test)]
    pub(crate) fn for_http_test(client: PublicHttpClient) -> Self {
        Self {
            client,
            request_timeout_ms: 1_000,
            retry_base_ms: 1,
            contracts: Mutex::new(Arc::new(ContractTable::default())),
            refresh: tokio::sync::Mutex::new(()),
        }
    }

    fn cached_table(&self) -> Arc<ContractTable> {
        Arc::clone(
            &self
                .contracts
                .lock()
                .expect("MEXC contract table lock poisoned"),
        )
    }

    /// The table, read once on first use. The ticker lane can reach the venue
    /// before the instrument lane does, and a contract size is not optional.
    async fn contract_table(&self) -> Result<Arc<ContractTable>, WorkerError> {
        let held = self.cached_table();
        if !held.is_empty() {
            return Ok(held);
        }
        let _refresh = self.refresh.lock().await;
        let held = self.cached_table();
        if !held.is_empty() {
            return Ok(held);
        }
        self.read_contract_table().await.map(|read| read.table)
    }

    /// `contract/detail` plus the whole funding page. The contract list
    /// carries no settlement cycle and no settlement clock, and the funding
    /// page answers for every contract the venue prices in one request.
    async fn read_contract_table(&self) -> Result<ContractRead, WorkerError> {
        let observed_ts_ms = wall_ms()?;
        let (detail, detail_at_ms) = self.client.get(PATH_DETAIL, "").await?;
        let (funding, funding_at_ms) = self.client.get(PATH_FUNDING, "").await?;
        let cycles = read_funding_cycles(mexc_data(&funding, "funding rate")?)?;
        let (table, rows) = read_contract_detail(mexc_data(&detail, "contract detail")?, &cycles)?;
        let table = Arc::new(table);
        *self
            .contracts
            .lock()
            .expect("MEXC contract table lock poisoned") = Arc::clone(&table);
        Ok(ContractRead {
            table,
            rows,
            observed_ts_ms,
            available_at_ms: detail_at_ms.max(funding_at_ms),
        })
    }

    async fn fetch_instrument_snapshot(&self) -> Result<FetchedInstruments, WorkerError> {
        let _refresh = self.refresh.lock().await;
        let read = self.read_contract_table().await?;
        let fetched = FetchedInstruments {
            observed_ts_ms: read.observed_ts_ms,
            available_at_ms: read.available_at_ms,
            rows: read.rows,
        };
        validate_fetched_instruments(&fetched)?;
        Ok(fetched)
    }

    async fn fetch_tickers(
        &self,
        allowed: Option<BTreeSet<String>>,
    ) -> Result<FetchedTickers, WorkerError> {
        let table = self.contract_table().await?;
        let request_started_at_ms = wall_ms()?;
        let (payload, available_at_ms) = self.client.get(PATH_TICKER, "").await?;
        let rows = mexc_list(mexc_data(&payload, "ticker")?, "ticker")?
            .iter()
            .filter_map(|row| ticker_wire(&table, row, available_at_ms))
            .filter(|row| {
                allowed
                    .as_ref()
                    .is_none_or(|allowed| allowed.contains(&row.symbol))
            })
            .collect::<Vec<_>>();
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
        &self,
        symbol: &str,
        start: i64,
        end: i64,
        page_limit: usize,
    ) -> Result<FetchedKlineRows, WorkerError> {
        let table = self.contract_table().await?;
        let contract = table
            .row(symbol)
            .ok_or_else(|| WorkerError::input(format!("MEXC lists no contract for {symbol}")))?;
        let retained_row_cap = source_grid_slots(start, end, HOUR_MS, false)?;
        let page_rows = page_limit.min(MAX_KLINE_ROWS);
        let span = HOUR_MS * i64::try_from(page_rows.saturating_sub(1)).unwrap_or(0);
        let mut cursor = start;
        let mut by_time = BTreeMap::<i64, Vec<Value>>::new();
        let mut available = start;
        while cursor < end {
            let window_end = (cursor.saturating_add(span)).min(end - HOUR_MS);
            let path = format!("{PATH_KLINE}/{}", percent_encode(&contract.venue_symbol));
            // The venue's kline window is in SECONDS and its end is
            // inclusive; the wire's is in milliseconds and exclusive.
            let query = format!(
                "interval={KLINE_INTERVAL}&start={}&end={}",
                cursor / 1_000,
                window_end / 1_000
            );
            let (payload, received) = self.client.get(&path, &query).await?;
            available = available.max(received);
            let rows = read_kline_columns(mexc_data(&payload, "kline")?, contract.contract_size)?;
            validate_source_page_rows(rows.len(), page_rows, "MEXC kline")?;
            for (ts, row) in rows {
                validate_source_grid_timestamp(ts, start, end, HOUR_MS, false, "MEXC kline bar")?;
                match by_time.get(&ts) {
                    Some(existing) if existing != &row => {
                        return Err(WorkerError::network(
                            "MEXC kline pagination returned conflicting duplicate",
                        ));
                    }
                    Some(_) => {}
                    None => {
                        if by_time.len() >= retained_row_cap {
                            return Err(WorkerError::network(
                                "MEXC kline response exceeded the requested grid cardinality",
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

    /// The venue's funding history is newest-first and takes no time bound, so
    /// this walks pages back until one ends before `start`.
    async fn fetch_funding(
        &self,
        symbol: &str,
        start: i64,
        end: i64,
        page_limit: usize,
        interval_hours: Option<i64>,
    ) -> Result<FetchedFundingRows, WorkerError> {
        let table = self.contract_table().await?;
        let venue_symbol = table
            .venue_symbol(symbol)
            .ok_or_else(|| WorkerError::input(format!("MEXC lists no contract for {symbol}")))?
            .to_owned();
        let retained_row_cap = source_grid_slots(start, end, HOUR_MS, true)?;
        let mut by_time = BTreeMap::<i64, BybitFundingWire>::new();
        let mut available = start;
        let mut page = 1_u64;
        loop {
            let query = format!(
                "symbol={}&page_num={page}&page_size={page_limit}",
                percent_encode(&venue_symbol)
            );
            let (payload, received) = self.client.get(PATH_FUNDING_HISTORY, &query).await?;
            available = available.max(received);
            let data = mexc_data(&payload, "funding history")?;
            let rows = mexc_list(
                data.get("resultList")
                    .ok_or_else(|| WorkerError::network("MEXC funding history lacks resultList"))?,
                "funding history",
            )?;
            validate_source_page_rows(rows.len(), page_limit, "MEXC funding")?;
            let mut oldest_on_page = i64::MAX;
            for row in rows {
                let settlement = value_i64(
                    row.get("settleTime")
                        .ok_or_else(|| WorkerError::network("MEXC funding row lacks settleTime"))?,
                    "MEXC funding settleTime",
                )?;
                oldest_on_page = oldest_on_page.min(settlement);
                if settlement < start || settlement > end {
                    continue;
                }
                validate_source_grid_timestamp(
                    settlement,
                    start,
                    end,
                    HOUR_MS,
                    true,
                    "MEXC funding settleTime",
                )?;
                let rate = row
                    .get("fundingRate")
                    .cloned()
                    .ok_or_else(|| WorkerError::network("MEXC funding row lacks fundingRate"))?;
                // The venue states the cycle on the settlement itself, so the
                // instrument table's value is not stamped over it.
                let hours = row
                    .get("collectCycle")
                    .and_then(|value| value_i64(value, "MEXC collectCycle").ok())
                    .filter(|hours| *hours > 0)
                    .or(interval_hours);
                let wire = BybitFundingWire {
                    funding_rate_timestamp: Value::from(settlement),
                    funding_rate: rate,
                    funding_interval_hour: hours.map(Value::from),
                };
                if !by_time.contains_key(&settlement) && by_time.len() >= retained_row_cap {
                    return Err(WorkerError::network(
                        "MEXC funding response exceeded the requested grid cardinality",
                    ));
                }
                if let Some(existing) = by_time.insert(settlement, wire.clone()) {
                    if existing != wire {
                        return Err(WorkerError::network(
                            "MEXC funding pagination returned conflicting duplicate",
                        ));
                    }
                }
            }
            let total_pages = value_i64(
                data.get("totalPage")
                    .ok_or_else(|| WorkerError::network("MEXC funding history lacks totalPage"))?,
                "MEXC funding totalPage",
            )?;
            if oldest_on_page <= start || page >= u64::try_from(total_pages.max(0)).unwrap_or(0) {
                break;
            }
            page = page.saturating_add(1);
        }
        let rows = by_time.into_values().collect::<Vec<_>>();
        normalize_funding_rows(symbol, available, &rows)?;
        Ok((rows, available))
    }

    /// The table the stream subscribes and converts against. A symbol the
    /// table does not name is quarantined by the stream, not refused here: a
    /// contract leaving the venue mid-run must not take the worker down.
    /// `open_stream` is only reached after the universe resolves, which is
    /// after an instrument read, so an empty table here is not a venue state.
    fn stream_contracts(&self) -> Result<Arc<ContractTable>, WorkerError> {
        let table = self.cached_table();
        if table.is_empty() {
            return Err(WorkerError::state(
                "MEXC stream needs the contract table, which no lane has read yet",
            ));
        }
        Ok(table)
    }
}

/// One contract-table read: the table, the instrument rows it produced, and
/// the clocks of the two requests behind them.
struct ContractRead {
    table: Arc<ContractTable>,
    rows: Vec<BybitInstrumentWire>,
    observed_ts_ms: i64,
    available_at_ms: i64,
}

impl PublicVenue for MexcPublicVenue {
    fn kind(&self) -> PublicVenueKind {
        PublicVenueKind::Mexc
    }

    /// `contract/detail` answers whole, so the config's page bound has nothing
    /// to bound here.
    fn instruments(
        &self,
        _max_pages: usize,
    ) -> BoxFuture<'_, Result<FetchedInstruments, WorkerError>> {
        Box::pin(self.fetch_instrument_snapshot())
    }

    fn ticker_page(&self) -> BoxFuture<'_, Result<FetchedTickers, WorkerError>> {
        Box::pin(self.fetch_tickers(None))
    }

    fn ticker_snapshot(
        &self,
        allowed: BTreeSet<String>,
    ) -> BoxFuture<'_, Result<FetchedTickers, WorkerError>> {
        Box::pin(self.fetch_tickers(Some(allowed)))
    }

    fn klines(
        &self,
        symbol: &str,
        start_ms: i64,
        end_ms: i64,
        page_limit: usize,
    ) -> BoxFuture<'_, Result<FetchedKlineRows, WorkerError>> {
        let symbol = symbol.to_owned();
        Box::pin(async move {
            self.fetch_klines(&symbol, start_ms, end_ms, page_limit)
                .await
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
        let symbol = symbol.to_owned();
        Box::pin(async move {
            self.fetch_funding(&symbol, start_ms, end_ms, page_limit, interval_hours)
                .await
        })
    }

    fn open_stream(&self, symbols: Vec<String>) -> Result<Box<dyn PublicStream>, WorkerError> {
        let table = self.stream_contracts()?;
        Ok(Box::new(MexcPublicStream::spawn(
            symbols,
            table,
            self.request_timeout_ms,
            self.retry_base_ms,
        )?))
    }

    fn open_stream_continuing(
        &self,
        symbols: Vec<String>,
        continuity: StreamContinuity,
    ) -> Result<Box<dyn PublicStream>, WorkerError> {
        let table = self.stream_contracts()?;
        Ok(Box::new(MexcPublicStream::spawn_continuing(
            symbols,
            table,
            self.request_timeout_ms,
            self.retry_base_ms,
            continuity,
        )?))
    }
}

/// The envelope every one of these endpoints answers in: `{success, code,
/// data}`, `code` 0 being the only success. Code 510 is the venue's rate limit
/// arriving inside an HTTP 200, and reads as a retryable network failure —
/// the same reading `engine-venue/src/venues/mexc/parse.rs` gives it.
pub(crate) fn mexc_data<'a>(payload: &'a Value, label: &str) -> Result<&'a Value, WorkerError> {
    let code = payload.get("code").and_then(Value::as_i64);
    if code == Some(RATE_LIMITED) {
        return Err(WorkerError::network(format!(
            "MEXC {label} is rate limited (code {RATE_LIMITED})"
        )));
    }
    if code != Some(0) || payload.get("success").and_then(Value::as_bool) != Some(true) {
        return Err(WorkerError::network(format!(
            "MEXC {label} code={} message={}",
            code.map_or_else(|| "absent".to_owned(), |code| code.to_string()),
            payload
                .get("message")
                .or_else(|| payload.get("msg"))
                .unwrap_or(&Value::Null)
        )));
    }
    payload
        .get("data")
        .ok_or_else(|| WorkerError::network(format!("MEXC {label} lacks data")))
}

fn mexc_list<'a>(value: &'a Value, label: &str) -> Result<&'a Vec<Value>, WorkerError> {
    value
        .as_array()
        .ok_or_else(|| WorkerError::network(format!("MEXC {label} is not a list")))
}

/// The settlement cycle and the venue's next settlement clock for every
/// contract it prices, keyed by the venue's spelling. `contract/detail`
/// carries neither, and the cycles are not all the same: 1, 4, 8 and 24 hours
/// are all live.
fn read_funding_cycles(data: &Value) -> Result<BTreeMap<String, (i64, i64)>, WorkerError> {
    let mut out = BTreeMap::new();
    for row in mexc_list(data, "funding rate")? {
        let Some(symbol) = row.get("symbol").and_then(Value::as_str) else {
            continue;
        };
        let cycle = row
            .get("collectCycle")
            .and_then(|value| value_i64(value, "collectCycle").ok())
            .filter(|hours| *hours > 0);
        let settle = row
            .get("nextSettleTime")
            .and_then(|value| value_i64(value, "nextSettleTime").ok())
            .filter(|stamp| *stamp > 0 && stamp.rem_euclid(HOUR_MS) == 0);
        if let (Some(cycle), Some(settle)) = (cycle, settle) {
            out.insert(symbol.to_owned(), (cycle, settle));
        }
    }
    Ok(out)
}

/// `contract/detail` into the table and the wire rows, in one pass. A row
/// whose base coin, quote coin, contract size or tick is missing or
/// non-positive is left out rather than defaulted: a contract size guessed at
/// 1 would misstate every quantity on that symbol by its real multiplier.
fn read_contract_detail(
    data: &Value,
    cycles: &BTreeMap<String, (i64, i64)>,
) -> Result<(ContractTable, Vec<BybitInstrumentWire>), WorkerError> {
    let mut table = ContractTable::default();
    let mut rows = BTreeMap::new();
    for row in mexc_list(data, "contract detail")? {
        let Some((symbol, contract, wire)) = read_contract_row(row, cycles) else {
            continue;
        };
        table.insert(symbol.clone(), contract);
        rows.insert(symbol, wire);
    }
    if table.is_empty() {
        return Err(WorkerError::network(
            "MEXC contract detail listed no readable contract",
        ));
    }
    Ok((table, rows.into_values().collect()))
}

fn read_contract_row(
    row: &Value,
    cycles: &BTreeMap<String, (i64, i64)>,
) -> Option<(String, ContractRow, BybitInstrumentWire)> {
    let text = |key: &str| {
        row.get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
    };
    let venue_symbol = text("symbol")?.to_owned();
    let base = text("baseCoin")?;
    let quote = text("quoteCoin")?;
    let settle = text("settleCoin")?;
    let symbol = format!("{base}{quote}").to_ascii_uppercase();
    let positive = |key: &str| {
        row.get(key)
            .and_then(|value| value_f64(value, key).ok())
            .filter(|value| *value > 0.0)
    };
    let contract_size = positive("contractSize")?;
    let tick_size = positive("priceUnit")?;
    let future_type = row.get("futureType").and_then(Value::as_i64);
    let base_qty = |key: &str| {
        positive(key)
            .map(|contracts| Value::from(round_clean(contracts * contract_size, contract_size)))
    };
    let mut lot_size_filter = BTreeMap::new();
    // `volUnit` is the venue's order step in contracts; it publishes 1 for
    // every contract today, which makes this step the contract size.
    lot_size_filter.insert(
        "qtyStep".to_owned(),
        base_qty("volUnit").unwrap_or_else(|| Value::from(contract_size)),
    );
    if let Some(minimum) = base_qty("minVol") {
        lot_size_filter.insert("minOrderQty".to_owned(), minimum);
    }
    // The venue publishes two ceilings and they differ: `limitMaxVol` bounds a
    // limit order, `maxVol` a market one.
    if let Some(maximum) = base_qty("limitMaxVol") {
        lot_size_filter.insert("maxOrderQty".to_owned(), maximum);
    }
    if let Some(maximum) = base_qty("maxVol") {
        lot_size_filter.insert("maxMktOrderQty".to_owned(), maximum);
    }
    let (cycle_hours, next_settle_ms) = match cycles.get(&venue_symbol) {
        Some((cycle, settle)) => (Some(*cycle), Some(*settle)),
        None => (None, None),
    };
    let wire = BybitInstrumentWire {
        symbol: symbol.clone(),
        contract_type: (future_type == Some(1)).then(|| {
            if settle == quote {
                "LinearPerpetual".to_owned()
            } else {
                "InversePerpetual".to_owned()
            }
        }),
        symbol_type: None,
        status: Some(
            if row.get("state").and_then(Value::as_i64) == Some(0)
                && row.get("apiAllowed").and_then(Value::as_bool) == Some(true)
            {
                "Trading".to_owned()
            } else {
                "Closed".to_owned()
            },
        ),
        base_coin: Some(base.to_owned()),
        quote_coin: Some(quote.to_owned()),
        settle_coin: Some(settle.to_owned()),
        launch_time: row
            .get("createTime")
            .and_then(|value| value_i64(value, "createTime").ok())
            .filter(|stamp| *stamp > 0)
            .map(Value::from),
        delivery_time: None,
        price_filter: BTreeMap::from([("tickSize".to_owned(), Value::from(tick_size))]),
        lot_size_filter,
        funding_interval: cycle_hours
            .and_then(|hours| hours.checked_mul(60))
            .map(Value::from),
        is_pre_listing: false,
    };
    Some((
        symbol,
        ContractRow {
            venue_symbol,
            contract_size,
            cycle_hours,
            next_settle_ms,
        },
        wire,
    ))
}

/// One ticker row. A contract the venue prices but does not list has no
/// contract size, so no quantity on it can be converted and the row is left
/// out; the live page carries eight of those.
pub(crate) fn ticker_wire(
    table: &ContractTable,
    row: &Value,
    now_ms: i64,
) -> Option<BybitTickerWire> {
    let venue_symbol = row.get("symbol").and_then(Value::as_str)?;
    let symbol = table.engine_symbol(venue_symbol)?.to_owned();
    let contract = table.row(&symbol)?;
    Some(BybitTickerWire {
        symbol,
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: row.get("lastPrice").cloned(),
        mark_price: row.get("fairPrice").cloned(),
        index_price: row.get("indexPrice").cloned(),
        bid1_price: row.get("bid1").cloned(),
        ask1_price: row.get("ask1").cloned(),
        bid1_size: None,
        ask1_size: None,
        open_interest: base_quantity(row.get("holdVol"), contract.contract_size),
        open_interest_value: None,
        turnover24h: row.get("amount24").cloned(),
        volume24h: base_quantity(row.get("volume24"), contract.contract_size),
        funding_rate: row.get("fundingRate").cloned(),
        next_funding_time: next_settlement_ms(contract, now_ms).map(Value::from),
    })
}

/// Contracts to base coin, through the shave `engine-public`'s contract table
/// uses: `3 * 0.0001` is 0.00030000000000000003.
pub(crate) fn base_quantity(value: Option<&Value>, contract_size: f64) -> Option<Value> {
    let contracts = value.and_then(|value| value_f64(value, "MEXC contract quantity").ok())?;
    if contracts < 0.0 {
        return None;
    }
    Some(Value::from(round_clean(
        contracts * contract_size,
        contract_size,
    )))
}

/// The next settlement strictly after `now_ms`, on the phase the venue states.
///
/// The ticker carries no settlement clock, so it is rolled forward from the
/// venue's own `nextSettleTime` by its own `collectCycle`. It is not derived
/// from the epoch: the cycles run 1, 4, 8 and 24 hours and the daily contract
/// does not settle on a 24-hour boundary from the epoch, so an epoch-derived
/// grid is the wrong clock for most of this venue.
pub(crate) fn next_settlement_ms(contract: &ContractRow, now_ms: i64) -> Option<i64> {
    let cycle = contract
        .cycle_hours
        .filter(|hours| *hours > 0)?
        .checked_mul(HOUR_MS)?;
    let stated = contract.next_settle_ms.filter(|stamp| *stamp > 0)?;
    if stated > now_ms {
        return Some(stated);
    }
    let elapsed = now_ms.checked_sub(stated)?;
    let periods = elapsed.checked_div(cycle)?.checked_add(1)?;
    stated.checked_add(periods.checked_mul(cycle)?)
}

/// The venue answers a kline window column by column, not row by row, in
/// seconds, with the traded bar in the `real*` columns. Returns
/// `(bar_open_ms, wire_row)` per bar.
fn read_kline_columns(
    data: &Value,
    contract_size: f64,
) -> Result<Vec<(i64, Vec<Value>)>, WorkerError> {
    let column = |key: &str| -> Result<&Vec<Value>, WorkerError> {
        data.get(key)
            .and_then(Value::as_array)
            .ok_or_else(|| WorkerError::network(format!("MEXC kline lacks the {key} column")))
    };
    let time = column("time")?;
    let open = column("realOpen")?;
    let high = column("realHigh")?;
    let low = column("realLow")?;
    let close = column("realClose")?;
    let volume = column("vol")?;
    let turnover = column("amount")?;
    for other in [open, high, low, close, volume, turnover] {
        if other.len() != time.len() {
            return Err(WorkerError::network(
                "MEXC kline columns have different lengths",
            ));
        }
    }
    let mut out = Vec::with_capacity(time.len());
    for index in 0..time.len() {
        let seconds = value_i64(&time[index], "MEXC kline time")?;
        let open_ts_ms = seconds
            .checked_mul(1_000)
            .ok_or_else(|| WorkerError::network("MEXC kline time overflowed"))?;
        let base_volume = base_quantity(Some(&volume[index]), contract_size)
            .ok_or_else(|| WorkerError::network("MEXC kline volume is not a quantity"))?;
        out.push((
            open_ts_ms,
            vec![
                Value::from(open_ts_ms),
                open[index].clone(),
                high[index].clone(),
                low[index].clone(),
                close[index].clone(),
                base_volume,
                turnover[index].clone(),
            ],
        ));
    }
    Ok(out)
}

#[cfg(test)]
mod tests;
