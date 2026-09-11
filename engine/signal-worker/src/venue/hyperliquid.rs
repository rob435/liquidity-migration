//! Hyperliquid as the worker's public data source: the `/info` POST reads and
//! the public WebSocket. Its payloads are converted into Bybit's wire shape,
//! which is the worker's neutral wire; see [`crate::venue`].
//!
//! # Where each wire field comes from
//!
//! | Wire field | Hyperliquid source |
//! | --- | --- |
//! | instrument `symbol` | `meta.universe[].name` plus `USDT`, upper-cased: `kPEPE` is `KPEPEUSDT` |
//! | instrument `status` | `Closed` when `isDelisted`, else `Trading` |
//! | instrument `settleCoin` | `USDC`: the venue margins every perpetual in USDC while the engine's spelling keeps `USDT` |
//! | instrument `launchTime` | the earliest daily bar `candleSnapshot` publishes for the coin |
//! | instrument `fundingInterval` | 60: the venue settles funding every hour, on the hour |
//! | instrument `priceFilter.tickSize` | `10^-(6 - szDecimals)` |
//! | instrument `lotSizeFilter.qtyStep`, `.minOrderQty` | `10^-szDecimals` |
//! | instrument `lotSizeFilter.minNotionalValue` | 10, the venue's minimum order value in USD |
//! | ticker `lastPrice` | `midPx` |
//! | ticker `markPrice`, `indexPrice` | `markPx`, `oraclePx` |
//! | ticker `openInterest`, `volume24h`, `turnover24h` | `openInterest`, `dayBaseVlm`, `dayNtlVlm` |
//! | ticker `openInterestValue` | `openInterest * markPx` |
//! | ticker `fundingRate` | `funding`, the hourly rate as the venue states it |
//! | ticker `nextFundingTime` | the next whole hour |
//! | kline `[0..=5]` | `candleSnapshot` `t`, `o`, `h`, `l`, `c`, `v` |
//! | kline `[6]` | `v * (o + h + l + c) / 4` |
//! | funding `fundingRateTimestamp` | `fundingHistory` `time`, floored to its hour |
//! | funding `fundingRate` | `fundingRate`, the hourly rate, never rescaled |
//!
//! # What the venue does not state
//!
//! - **Quote turnover per bar.** `candleSnapshot` carries base volume only, so
//!   kline `[6]` is `volume * mean(open, high, low, close)` — an
//!   approximation, and the number LONG ranks on and CARRY's `adv24` reads.
//! - **The touch.** `activeAssetCtx` and `metaAndAssetCtxs` carry `impactPxs`,
//!   which are the funding impact prices, not the best bid and offer, so
//!   `bid1Price`, `ask1Price`, `bid1Size` and `ask1Size` are left absent.
//! - **A last trade price.** `midPx` stands in for `lastPrice`, and is absent
//!   on a coin with no resting book.
//! - **A listing timestamp.** The earliest daily bar is the closest thing the
//!   venue publishes; for the oldest coins that bar predates the exchange. It
//!   is one read per coin, so a cold process reads the whole listed set beside
//!   the instrument table rather than in front of it, and a coin whose read has
//!   not landed fails `min_listing_age_days` until the refresh after it does.
//! - **A product class.** `symbolType` is left absent, which reads as the
//!   ordinary crypto perpetual in [`crate::universe::CRYPTO_SYMBOL_TYPES`].
//! - **Order-size maximums or a delivery clock.** Both absent; every listed
//!   contract is a perpetual.
//!
//! The tick size is informational: the worker prices no order, and the venue
//! caps a price at five significant figures as well as at this many decimals.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::Semaphore;
use tokio::time::Instant;

use crate::config::SignalWorkerConfig;
use crate::http::{wall_ms, PublicHttpClient};
use crate::model::{BybitFundingWire, BybitInstrumentWire, BybitTickerWire};
use crate::normalize::{normalize_funding_rows, normalize_kline_rows, value_f64};
use crate::worker::WorkerError;
use crate::HOUR_MS;

use super::{
    source_grid_slots, validate_fetched_instruments, validate_fetched_tickers,
    validate_source_grid_timestamp, validate_source_page_rows, wire_i64, BoxFuture,
    FetchedFundingRows, FetchedInstruments, FetchedKlineRows, FetchedTickers, PublicStream,
    PublicVenue, PublicVenueKind, StreamContinuity,
};

mod stream;

pub use stream::HyperliquidPublicStream;

/// The venue margins every perpetual in USDC.
const SETTLE_COIN: &str = "USDC";

/// Every `/info` read costs 20 of the venue's 1200-per-minute request weight
/// for this IP, so 60 reads a minute is the whole budget and one read a second
/// is the pace that fits inside it. Every read in this module waits its turn.
const INFO_READ_INTERVAL_MS: u64 = 1_000;

/// The most rows `candleSnapshot` and `fundingHistory` answer with, whatever
/// window or page limit the caller asks for.
const MAX_CANDLE_ROWS: usize = 5_000;
const MAX_FUNDING_ROWS: usize = 500;

/// A price carries `6 - szDecimals` decimals and a size carries `szDecimals`.
const PRICE_DECIMAL_BUDGET: u32 = 6;

/// The venue takes no order below this many USD.
const MIN_ORDER_NOTIONAL_USD: i64 = 10;

/// Minutes between settlements. The venue pays funding every hour.
const FUNDING_INTERVAL_MIN: i64 = 60;

/// The REST host without its scheme, which is what [`PublicHttpClient`] takes.
/// No venue address is written down here: it comes from the realm table in
/// `engine-public`. [`HyperliquidPublicVenue::open`] refuses any public market
/// realm but mainnet, so that is the only host a worker reads.
pub fn rest_host() -> &'static str {
    engine_public::HyperliquidRealm::Mainnet
        .rest_base()
        .trim_start_matches("https://")
}

pub struct HyperliquidPublicVenue {
    info: PublicHttpClient,
    pacer: Arc<RequestPacer>,
    listings: Arc<Mutex<ListingHistory>>,
    /// Engine symbol to the venue's own coin spelling, from the last `meta`.
    /// `kPEPE` is not `KPEPE` on the wire, and every request and subscription
    /// names the coin.
    coins: Arc<Mutex<BTreeMap<String, String>>>,
    websocket_url: String,
    request_timeout_ms: u64,
    retry_base_ms: u64,
}

impl HyperliquidPublicVenue {
    pub fn open(
        config: &SignalWorkerConfig,
        request_budget: Arc<Semaphore>,
    ) -> Result<Self, WorkerError> {
        let realm = match config.live.public_market_realm.as_str() {
            "mainnet" => engine_public::HyperliquidRealm::Mainnet,
            _ => return Err(WorkerError::config("unsupported public market realm")),
        };
        Ok(Self {
            info: PublicHttpClient::new(
                realm.rest_base().trim_start_matches("https://"),
                config.live.request_timeout_ms,
                config.live.request_retries,
                config.live.retry_base_ms,
                request_budget,
            )?,
            pacer: Arc::new(RequestPacer::new(Duration::from_millis(
                INFO_READ_INTERVAL_MS,
            ))),
            listings: Arc::default(),
            coins: Arc::default(),
            websocket_url: realm.websocket().to_owned(),
            request_timeout_ms: config.live.request_timeout_ms,
            retry_base_ms: config.live.retry_base_ms,
        })
    }

    /// One local stand-in venue, read as fast as the socket answers.
    #[cfg(test)]
    pub(crate) fn for_http_test(client: PublicHttpClient) -> Self {
        Self {
            info: client,
            pacer: Arc::new(RequestPacer::new(Duration::ZERO)),
            listings: Arc::default(),
            coins: Arc::default(),
            websocket_url: "ws://127.0.0.1:1".to_owned(),
            request_timeout_ms: 1_000,
            retry_base_ms: 1,
        }
    }

    async fn read(&self, body: &Value) -> Result<(Value, i64), WorkerError> {
        self.pacer.wait().await;
        self.info.post_json("/info", body).await
    }

    /// The venue's spelling of a symbol the last `meta` carried. Every `meta`
    /// read fills the table; a process that restored its universe from the
    /// checkpoint has made none yet, so the first lookup reads `meta` itself.
    async fn coin_of(&self, symbol: &str) -> Result<String, WorkerError> {
        let empty = self
            .coins
            .lock()
            .expect("Hyperliquid coin table lock poisoned")
            .is_empty();
        if empty {
            let (payload, _) = self.read(&json!({"type": "meta"})).await?;
            let coins = meta_universe(&payload)?
                .iter()
                .map(coin_names)
                .collect::<Result<BTreeMap<_, _>, _>>()?;
            self.remember_coins(coins);
        }
        self.coins
            .lock()
            .expect("Hyperliquid coin table lock poisoned")
            .get(symbol)
            .cloned()
            .ok_or_else(|| WorkerError::network(format!("Hyperliquid lists no coin for {symbol}")))
    }

    fn remember_coins(&self, coins: BTreeMap<String, String>) {
        *self
            .coins
            .lock()
            .expect("Hyperliquid coin table lock poisoned") = coins;
    }

    async fn fetch_instruments(&self) -> Result<FetchedInstruments, WorkerError> {
        let observed_ts_ms = wall_ms()?;
        let (payload, available_at_ms) = self.read(&json!({"type": "meta"})).await?;
        let universe = meta_universe(&payload)?;
        let mut coins = BTreeMap::new();
        let mut listed = Vec::new();
        for row in universe {
            let (symbol, coin) = coin_names(row)?;
            if !is_delisted(row) {
                listed.push(coin.clone());
            }
            coins.insert(symbol, coin);
        }
        self.remember_coins(coins);
        let (first_bar_ms, unread) = {
            let state = self
                .listings
                .lock()
                .expect("Hyperliquid listing history lock poisoned");
            let unread = listed
                .iter()
                .filter(|coin| !state.first_bar_ms.contains_key(*coin))
                .cloned()
                .collect::<Vec<_>>();
            (state.first_bar_ms.clone(), unread)
        };
        if !unread.is_empty() {
            self.read_listing_history(unread);
        }
        let fetched = FetchedInstruments {
            observed_ts_ms,
            available_at_ms,
            rows: universe
                .iter()
                .map(|row| instrument_wire(row, &first_bar_ms))
                .collect::<Result<Vec<_>, _>>()?,
        };
        validate_fetched_instruments(&fetched)?;
        Ok(fetched)
    }

    /// Read the earliest daily bar of every coin this process has not read one
    /// for, one coin at a time on the venue's request pace, beside the caller
    /// rather than in front of it: a whole pass is one read per listed coin,
    /// which is minutes at that pace, and the instrument table is not held
    /// back for it. A coin whose launch time is not in yet fails
    /// `min_listing_age_days` and enters its sleeve at the refresh after the
    /// read lands. A coin is read once and never again; a coin whose read
    /// failed is read again by the next instrument refresh.
    fn read_listing_history(&self, coins: Vec<String>) {
        {
            let mut state = self
                .listings
                .lock()
                .expect("Hyperliquid listing history lock poisoned");
            if state.reading {
                return;
            }
            state.reading = true;
        }
        let client = self.info.clone();
        let pacer = Arc::clone(&self.pacer);
        let listings = Arc::clone(&self.listings);
        tokio::spawn(async move {
            for coin in coins {
                let read = first_daily_bar_ms(&client, &pacer, &coin).await;
                let mut state = listings
                    .lock()
                    .expect("Hyperliquid listing history lock poisoned");
                match read {
                    Ok(Some(open_ts_ms)) => {
                        state.first_bar_ms.insert(coin, open_ts_ms);
                    }
                    Ok(None) => {}
                    Err(error) => {
                        eprintln!("signal-worker: hyperliquid listing history for {coin}: {error}")
                    }
                }
            }
            listings
                .lock()
                .expect("Hyperliquid listing history lock poisoned")
                .reading = false;
        });
    }

    async fn fetch_tickers(
        &self,
        allowed: Option<&BTreeSet<String>>,
    ) -> Result<FetchedTickers, WorkerError> {
        let request_started_at_ms = wall_ms()?;
        let (payload, available_at_ms) = self.read(&json!({"type": "metaAndAssetCtxs"})).await?;
        let (universe, contexts) = meta_and_asset_contexts(&payload)?;
        let next_funding_time_ms = next_settlement_ms(available_at_ms);
        let mut coins = BTreeMap::new();
        let mut rows = Vec::new();
        for (row, context) in universe.iter().zip(contexts) {
            let (symbol, coin) = coin_names(row)?;
            let wanted = allowed.is_none_or(|allowed| allowed.contains(&symbol));
            coins.insert(symbol.clone(), coin);
            if wanted {
                rows.push(ticker_wire(symbol, context, next_funding_time_ms)?);
            }
        }
        self.remember_coins(coins);
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
        let coin = self.coin_of(symbol).await?;
        let page_row_cap = page_limit.min(MAX_CANDLE_ROWS);
        let retained_row_cap = source_grid_slots(start, end, HOUR_MS, false)?;
        let limit = i64::try_from(page_row_cap)
            .map_err(|_| WorkerError::config("kline page limit exceeds i64"))?;
        let span = (limit - 1).max(0) * HOUR_MS;
        let mut cursor = start;
        let mut by_time = BTreeMap::<i64, Vec<Value>>::new();
        let mut available = start;
        while cursor < end {
            // `endTime` is inclusive on a bar's open, so the last bar a window
            // may carry opens one hour before `end`.
            let window_end = (cursor + span).min(end - HOUR_MS);
            let (payload, received) = self
                .read(&json!({"type": "candleSnapshot", "req": {
                    "coin": coin,
                    "interval": "1h",
                    "startTime": cursor,
                    "endTime": window_end,
                }}))
                .await?;
            available = available.max(received);
            let list = candle_list(&payload)?;
            validate_source_page_rows(list.len(), page_row_cap, "Hyperliquid candle")?;
            for item in list {
                let row = kline_row(item)?;
                let ts = wire_i64(row.first(), "Hyperliquid candle open")?;
                validate_source_grid_timestamp(
                    ts,
                    start,
                    end,
                    HOUR_MS,
                    false,
                    "Hyperliquid candle open",
                )?;
                match by_time.get(&ts) {
                    Some(existing) if existing != &row => {
                        return Err(WorkerError::network(
                            "Hyperliquid candle pagination returned conflicting duplicate",
                        ));
                    }
                    Some(_) => {}
                    None => {
                        if by_time.len() >= retained_row_cap {
                            return Err(WorkerError::network(
                                "Hyperliquid candle response exceeded the requested grid cardinality",
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

    async fn fetch_funding(
        &self,
        symbol: &str,
        start: i64,
        end: i64,
        page_limit: usize,
        interval_hours: Option<i64>,
    ) -> Result<FetchedFundingRows, WorkerError> {
        let coin = self.coin_of(symbol).await?;
        let page_row_cap = page_limit.min(MAX_FUNDING_ROWS);
        let retained_row_cap = source_grid_slots(start, end, HOUR_MS, true)?;
        let interval = interval_hours.map(Value::from);
        let limit = i64::try_from(page_row_cap)
            .map_err(|_| WorkerError::config("funding page limit exceeds i64"))?;
        let window_span = (limit - 1).max(0) * HOUR_MS;
        let mut cursor = start;
        let mut available = start;
        let mut by_time = BTreeMap::new();
        while cursor <= end {
            let window_end = cursor.saturating_add(window_span).min(end);
            // A print is stamped a few milliseconds after the hour it settled,
            // so a window has to reach to just before the settlement that
            // follows its last one.
            let request_end = window_end.saturating_add(HOUR_MS - 1);
            let (payload, received) = self
                .read(&json!({
                    "type": "fundingHistory",
                    "coin": coin,
                    "startTime": cursor,
                    "endTime": request_end,
                }))
                .await?;
            available = available.max(received);
            let list = funding_list(&payload)?;
            validate_source_page_rows(list.len(), page_row_cap, "Hyperliquid funding")?;
            for item in list {
                let printed_ms = wire_i64(item.get("time"), "Hyperliquid funding time")?;
                let key = printed_ms - printed_ms.rem_euclid(HOUR_MS);
                let rate = item
                    .get("fundingRate")
                    .cloned()
                    .ok_or_else(|| WorkerError::network("Hyperliquid funding row lacks rate"))?;
                let row = BybitFundingWire {
                    funding_rate_timestamp: Value::from(key),
                    funding_rate: rate,
                    funding_interval_hour: interval.clone(),
                };
                validate_source_grid_timestamp(
                    key,
                    start,
                    end,
                    HOUR_MS,
                    true,
                    "Hyperliquid funding settlement",
                )?;
                if !by_time.contains_key(&key) && by_time.len() >= retained_row_cap {
                    return Err(WorkerError::network(
                        "Hyperliquid funding response exceeded the requested grid cardinality",
                    ));
                }
                if let Some(existing) = by_time.insert(key, row.clone()) {
                    if existing != row {
                        return Err(WorkerError::network(
                            "Hyperliquid funding pagination returned conflicting duplicate",
                        ));
                    }
                }
            }
            if window_end == end {
                break;
            }
            cursor = window_end.saturating_add(HOUR_MS);
        }
        let rows = by_time.into_values().collect::<Vec<_>>();
        normalize_funding_rows(symbol, available, &rows)?;
        Ok((rows, available))
    }

    fn stream_coins(&self, symbols: &[String]) -> BTreeMap<String, String> {
        let table = self
            .coins
            .lock()
            .expect("Hyperliquid coin table lock poisoned");
        symbols
            .iter()
            .map(|symbol| {
                let coin = table
                    .get(symbol)
                    .cloned()
                    .unwrap_or_else(|| default_coin(symbol));
                (symbol.clone(), coin)
            })
            .collect()
    }
}

impl PublicVenue for HyperliquidPublicVenue {
    fn kind(&self) -> PublicVenueKind {
        PublicVenueKind::Hyperliquid
    }

    fn seed_listing_history(&self, launch_times_ms: BTreeMap<String, i64>) {
        let mut state = self
            .listings
            .lock()
            .expect("Hyperliquid listing history lock poisoned");
        for (symbol, open_ts_ms) in launch_times_ms {
            if open_ts_ms > 0 {
                state
                    .first_bar_ms
                    .entry(default_coin(&symbol))
                    .or_insert(open_ts_ms);
            }
        }
    }

    fn settle_coin(&self) -> &'static str {
        SETTLE_COIN
    }

    fn instruments(
        &self,
        _max_pages: usize,
    ) -> BoxFuture<'_, Result<FetchedInstruments, WorkerError>> {
        // One read carries the whole universe, so there is nothing to page.
        Box::pin(self.fetch_instruments())
    }

    fn ticker_page(&self) -> BoxFuture<'_, Result<FetchedTickers, WorkerError>> {
        Box::pin(self.fetch_tickers(None))
    }

    fn ticker_snapshot(
        &self,
        allowed: BTreeSet<String>,
    ) -> BoxFuture<'_, Result<FetchedTickers, WorkerError>> {
        Box::pin(async move { self.fetch_tickers(Some(&allowed)).await })
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
        self.open_stream_continuing(symbols, StreamContinuity::default())
    }

    fn open_stream_continuing(
        &self,
        symbols: Vec<String>,
        continuity: StreamContinuity,
    ) -> Result<Box<dyn PublicStream>, WorkerError> {
        let coins = self.stream_coins(&symbols);
        Ok(Box::new(HyperliquidPublicStream::spawn_continuing(
            &self.websocket_url,
            coins,
            self.request_timeout_ms,
            self.retry_base_ms,
            continuity,
        )?))
    }
}

/// Every coin's earliest daily bar, and whether a pass is reading the ones
/// this process has not read yet. One pass at a time, so a refresh arriving
/// mid-pass does not start a second one.
#[derive(Default)]
struct ListingHistory {
    first_bar_ms: BTreeMap<String, i64>,
    reading: bool,
}

/// The venue's request pace, shared by every read this venue makes.
struct RequestPacer {
    interval: Duration,
    next_at: tokio::sync::Mutex<Option<Instant>>,
}

impl RequestPacer {
    fn new(interval: Duration) -> Self {
        Self {
            interval,
            next_at: tokio::sync::Mutex::new(None),
        }
    }

    /// Take the next slot and wait for it. Callers queue in the order they
    /// arrive, and the lock is released before the wait so the next caller can
    /// take the slot after it.
    async fn wait(&self) {
        if self.interval.is_zero() {
            return;
        }
        let at = {
            let mut next_at = self.next_at.lock().await;
            let now = Instant::now();
            let at = next_at.map_or(now, |at| at.max(now));
            *next_at = Some(at + self.interval);
            at
        };
        tokio::time::sleep_until(at).await;
    }
}

async fn first_daily_bar_ms(
    client: &PublicHttpClient,
    pacer: &RequestPacer,
    coin: &str,
) -> Result<Option<i64>, WorkerError> {
    pacer.wait().await;
    let (payload, _) = client
        .post_json(
            "/info",
            &json!({"type": "candleSnapshot", "req": {
                "coin": coin,
                "interval": "1d",
                "startTime": 0,
            }}),
        )
        .await?;
    let mut earliest = None;
    for item in candle_list(&payload)? {
        let open_ts_ms = wire_i64(item.get("t"), "Hyperliquid daily candle open")?;
        if open_ts_ms <= 0 {
            return Err(WorkerError::network(
                "Hyperliquid daily candle open is not positive",
            ));
        }
        earliest = Some(earliest.map_or(open_ts_ms, |held: i64| held.min(open_ts_ms)));
    }
    Ok(earliest)
}

/// Funding settles every hour, on the hour. The venue publishes a running rate
/// and no settlement clock, so the next settlement is the next whole hour.
fn next_settlement_ms(now_ms: i64) -> i64 {
    now_ms - now_ms.rem_euclid(HOUR_MS) + HOUR_MS
}

/// The engine's spelling of a coin: `BTC` is `BTCUSDT`, `kPEPE` is
/// `KPEPEUSDT`, the same rule as
/// `engine-venue/src/venues/hyperliquid/assets.rs::symbol_of`.
fn engine_symbol(coin: &str) -> String {
    format!("{}USDT", coin.to_ascii_uppercase())
}

/// The coin a symbol names when the venue's own table has not been read. It is
/// right for every coin the venue spells in upper case, and a coin it does not
/// is refused by the venue and quarantined by the stream.
fn default_coin(symbol: &str) -> String {
    symbol
        .strip_suffix("USDT")
        .unwrap_or(symbol)
        .to_ascii_uppercase()
}

fn coin_names(row: &Value) -> Result<(String, String), WorkerError> {
    let coin = row
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .ok_or_else(|| WorkerError::network("Hyperliquid meta row lacks name"))?;
    Ok((engine_symbol(coin), coin.to_owned()))
}

fn is_delisted(row: &Value) -> bool {
    row.get("isDelisted")
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn meta_universe(payload: &Value) -> Result<&Vec<Value>, WorkerError> {
    payload
        .get("universe")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::network("Hyperliquid meta lacks universe"))
}

/// The universe and its contexts, which the venue pairs by index. A pair of
/// different lengths is refused rather than zipped short: the shorter one would
/// hand every context after the gap to the wrong coin.
fn meta_and_asset_contexts(payload: &Value) -> Result<(&Vec<Value>, &Vec<Value>), WorkerError> {
    let pair = payload
        .as_array()
        .filter(|pair| pair.len() == 2)
        .ok_or_else(|| WorkerError::network("Hyperliquid metaAndAssetCtxs is not a pair"))?;
    let universe = meta_universe(&pair[0])?;
    let contexts = pair[1]
        .as_array()
        .ok_or_else(|| WorkerError::network("Hyperliquid asset contexts are not a list"))?;
    if universe.len() != contexts.len() {
        return Err(WorkerError::network(
            "Hyperliquid asset contexts do not line up with the universe",
        ));
    }
    Ok((universe, contexts))
}

fn candle_list(payload: &Value) -> Result<&Vec<Value>, WorkerError> {
    payload
        .as_array()
        .ok_or_else(|| WorkerError::network("Hyperliquid candle snapshot is not a list"))
}

fn funding_list(payload: &Value) -> Result<&Vec<Value>, WorkerError> {
    payload
        .as_array()
        .ok_or_else(|| WorkerError::network("Hyperliquid funding history is not a list"))
}

fn instrument_wire(
    row: &Value,
    first_bar_ms: &BTreeMap<String, i64>,
) -> Result<BybitInstrumentWire, WorkerError> {
    let (symbol, coin) = coin_names(row)?;
    let size_decimals = wire_i64(row.get("szDecimals"), "Hyperliquid szDecimals")?;
    let size_decimals = u32::try_from(size_decimals)
        .ok()
        .filter(|decimals| *decimals <= PRICE_DECIMAL_BUDGET)
        .ok_or_else(|| WorkerError::network("Hyperliquid szDecimals is out of range"))?;
    Ok(BybitInstrumentWire {
        symbol,
        contract_type: Some("LinearPerpetual".to_owned()),
        symbol_type: None,
        status: Some(
            if is_delisted(row) {
                "Closed"
            } else {
                "Trading"
            }
            .to_owned(),
        ),
        base_coin: Some(coin.clone()),
        quote_coin: Some("USD".to_owned()),
        settle_coin: Some(SETTLE_COIN.to_owned()),
        launch_time: first_bar_ms.get(&coin).copied().map(Value::from),
        delivery_time: None,
        price_filter: BTreeMap::from([(
            "tickSize".to_owned(),
            Value::from(decimal_step(PRICE_DECIMAL_BUDGET - size_decimals)),
        )]),
        lot_size_filter: BTreeMap::from([
            (
                "qtyStep".to_owned(),
                Value::from(decimal_step(size_decimals)),
            ),
            (
                "minOrderQty".to_owned(),
                Value::from(decimal_step(size_decimals)),
            ),
            (
                "minNotionalValue".to_owned(),
                Value::from(MIN_ORDER_NOTIONAL_USD),
            ),
        ]),
        funding_interval: Some(Value::from(FUNDING_INTERVAL_MIN)),
        is_pre_listing: false,
    })
}

/// `10^-decimals` written out, so no float rounding reaches the wire.
fn decimal_step(decimals: u32) -> String {
    if decimals == 0 {
        return "1".to_owned();
    }
    let mut step = String::with_capacity(decimals as usize + 2);
    step.push_str("0.");
    for _ in 1..decimals {
        step.push('0');
    }
    step.push('1');
    step
}

fn ticker_wire(
    symbol: String,
    context: &Value,
    next_funding_time_ms: i64,
) -> Result<BybitTickerWire, WorkerError> {
    Ok(BybitTickerWire {
        symbol,
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: stated(context, "midPx"),
        mark_price: stated(context, "markPx"),
        index_price: stated(context, "oraclePx"),
        bid1_price: None,
        ask1_price: None,
        bid1_size: None,
        ask1_size: None,
        open_interest: stated(context, "openInterest"),
        open_interest_value: open_interest_value(context)?,
        turnover24h: stated(context, "dayNtlVlm"),
        volume24h: stated(context, "dayBaseVlm"),
        funding_rate: stated(context, "funding"),
        next_funding_time: Some(Value::from(next_funding_time_ms)),
    })
}

/// A field the venue published a value for. `null` is the venue saying it has
/// none, and reads as absent.
fn stated(value: &Value, key: &str) -> Option<Value> {
    value.get(key).filter(|held| !held.is_null()).cloned()
}

fn open_interest_value(context: &Value) -> Result<Option<Value>, WorkerError> {
    let (Some(size), Some(mark)) = (stated(context, "openInterest"), stated(context, "markPx"))
    else {
        return Ok(None);
    };
    let notional =
        value_f64(&size, "Hyperliquid openInterest")? * value_f64(&mark, "Hyperliquid markPx")?;
    json_number(notional, "Hyperliquid open interest value").map(Some)
}

/// One closed bar in the wire's array order. The venue's own decimal strings
/// go through untouched; the quote turnover it does not publish is derived.
fn kline_row(item: &Value) -> Result<Vec<Value>, WorkerError> {
    let field = |key: &str| -> Result<Value, WorkerError> {
        item.get(key)
            .filter(|held| !held.is_null())
            .cloned()
            .ok_or_else(|| WorkerError::network(format!("Hyperliquid candle lacks {key}")))
    };
    let open_ts_ms = wire_i64(item.get("t"), "Hyperliquid candle open")?;
    let (open, high, low, close, volume) = (
        field("o")?,
        field("h")?,
        field("l")?,
        field("c")?,
        field("v")?,
    );
    let mean_price = (value_f64(&open, "Hyperliquid candle open price")?
        + value_f64(&high, "Hyperliquid candle high")?
        + value_f64(&low, "Hyperliquid candle low")?
        + value_f64(&close, "Hyperliquid candle close")?)
        / 4.0;
    let turnover = value_f64(&volume, "Hyperliquid candle volume")? * mean_price;
    Ok(vec![
        Value::from(open_ts_ms),
        open,
        high,
        low,
        close,
        volume,
        json_number(turnover, "Hyperliquid candle turnover")?,
    ])
}

fn json_number(value: f64, label: &str) -> Result<Value, WorkerError> {
    serde_json::Number::from_f64(value)
        .map(Value::Number)
        .ok_or_else(|| WorkerError::network(format!("{label} is non-finite")))
}

#[cfg(test)]
mod tests;
