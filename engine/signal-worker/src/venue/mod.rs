//! The worker's public-data source, one module per venue.
//!
//! Funding, the settlement clock, hourly klines, tickers and the instrument
//! table all come from the venue named by `sources.public_venue` in the realm's
//! worker config. Binance top-trader positioning and the LLM gate file are not
//! part of this seam: the whale feed is a cross-venue positioning signal read
//! from Binance for every realm, and the gate is a file on disk.
//!
//! # Source wire contract
//!
//! Every venue answers in Bybit's wire shape, because that shape is the
//! worker's neutral wire: `WireEvent`s carrying these rows are appended to a
//! durable journal and replayed at boot, so their serialized form is frozen.
//! A venue module converts its own payloads into these rows; it never invents
//! a field the reducers do not read, and it never rescales a value.
//!
//! | Row | Field | Unit and rule |
//! | --- | --- | --- |
//! | kline | `[0]` | bar-open ms, on the hour, positive |
//! | kline | `[1..=4]` | open, high, low, close; every one positive, `high >= max(open, close)`, `low <= min(open, close)`, `low <= high` |
//! | kline | `[5]` | base volume, non-negative |
//! | kline | `[6]` | quote turnover, non-negative |
//! | funding | `fundingRateTimestamp` | settlement ms, on the hour |
//! | funding | `fundingRate` | fraction per settlement as the venue states it, never rescaled to another interval |
//! | funding | `fundingIntervalHour` | hours; the worker stamps it from the instrument table, and an absent one reads as 8 |
//! | instrument | `symbol` | the engine's spelling, ASCII alphanumeric; a row that fails this is left out of the table, not refused |
//! | instrument | `status` | `"Trading"` is tradable; every other value is a listed-but-not-tradable state |
//! | instrument | `settleCoin` | must equal the venue's own `settle_coin()` for the worker to follow the name |
//! | instrument | `launchTime` | listing ms |
//! | instrument | `fundingInterval` | minutes between settlements |
//! | instrument | `priceFilter.tickSize` | positive when present |
//! | instrument | `lotSizeFilter.qtyStep` | positive when present |
//! | instrument | `lotSizeFilter.minOrderQty`, `.minNotionalValue` | non-negative when present |
//! | instrument | `lotSizeFilter.maxOrderQty`, `.maxMktOrderQty` | a published `0` is no maximum |
//! | instrument | `isPreListing` | absent reads as `false` |
//! | ticker | `symbol` | required |
//! | ticker | `lastPrice`, `markPrice`, `indexPrice`, `bid1Price`, `ask1Price` | positive when present |
//! | ticker | `bid1Size`, `ask1Size`, `openInterest`, `openInterestValue`, `turnover24h`, `volume24h` | non-negative when present |
//! | ticker | `fundingRate` | finite, signed, the current running rate |
//! | ticker | `nextFundingTime` | next settlement ms |
//!
//! Timestamps are milliseconds. Numbers may arrive as JSON numbers or as the
//! decimal strings Bybit sends; both read the same.
//!
//! What a venue may leave absent: every instrument field except `symbol`, and
//! every ticker field except `symbol`. `null` and `""` read as absent, and
//! absent means the venue did not publish that field for that observation —
//! `normalize.rs` carries it through as `None` rather than substituting a
//! value. The two consequences worth naming: a ticker row reaches the
//! reducers only when it carries a mark price, or both a funding rate and a
//! next settlement time; and an instrument row with no `settleCoin` is never
//! followed. The instrument fetch does require the `priceFilter` and
//! `lotSizeFilter` objects themselves to be present, empty or not.

use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::Semaphore;

use crate::config::SignalWorkerConfig;
use crate::model::{BybitFundingWire, BybitInstrumentWire, BybitTickerWire};
use crate::normalize::normalize_instruments;
use crate::worker::WorkerError;

pub mod bybit;
pub mod hyperliquid;
pub mod mexc;
mod stream;

pub use stream::{
    ConfirmedKline, PublicStream, StreamContinuity, StreamEvent, StreamHealth, TickerSample,
};

/// An owned future, so `PublicVenue` stays object safe without a proc macro.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Closed bars in the wire's array order, with the first instant they were in
/// this process.
pub type FetchedKlineRows = (Vec<Vec<Value>>, i64);

/// Settled funding rows, with the first instant they were in this process.
pub type FetchedFundingRows = (Vec<BybitFundingWire>, i64);

/// The venues this worker can read public data from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PublicVenueKind {
    Bybit,
    Mexc,
    Hyperliquid,
}

impl PublicVenueKind {
    pub fn parse(value: &str) -> Result<Self, WorkerError> {
        match value {
            "bybit" => Ok(Self::Bybit),
            "mexc" => Ok(Self::Mexc),
            "hyperliquid" => Ok(Self::Hyperliquid),
            other => Err(WorkerError::config(format!(
                "public_venue {other:?} names no venue this worker can read"
            ))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Bybit => "bybit",
            Self::Mexc => "mexc",
            Self::Hyperliquid => "hyperliquid",
        }
    }

    /// The coin the venue margins its perpetuals in. Bybit and MEXC settle the
    /// USDT perpetuals this worker follows in USDT; Hyperliquid settles every
    /// perpetual in USDC while the engine keeps the `...USDT` spelling.
    pub fn settle_coin(self) -> &'static str {
        match self {
            Self::Bybit | Self::Mexc => "USDT",
            Self::Hyperliquid => "USDC",
        }
    }
}

/// One venue's public REST surface plus its stream constructor. Every method
/// answers in the wire shape this module documents.
pub trait PublicVenue: Send + Sync {
    fn kind(&self) -> PublicVenueKind;

    /// The coin the venue margins its perpetuals in. An instrument row whose
    /// `settleCoin` is anything else is not a name this worker follows.
    fn settle_coin(&self) -> &'static str {
        self.kind().settle_coin()
    }

    /// Launch times a checkpoint already carries, by engine symbol. A venue
    /// that reads listing history one coin at a time starts from them instead
    /// of reading every coin again on each boot; a venue whose instrument
    /// table states launch time ignores them.
    fn seed_listing_history(&self, _launch_times_ms: BTreeMap<String, i64>) {}

    /// Every listed instrument of every status, so a name that left the venue
    /// still has a trading interval.
    fn instruments(
        &self,
        max_pages: usize,
    ) -> BoxFuture<'_, Result<FetchedInstruments, WorkerError>>;

    /// The whole ticker page, unfiltered: the universe ranks every listed name.
    fn ticker_page(&self) -> BoxFuture<'_, Result<FetchedTickers, WorkerError>>;

    /// The followed names only.
    fn ticker_snapshot(
        &self,
        allowed: BTreeSet<String>,
    ) -> BoxFuture<'_, Result<FetchedTickers, WorkerError>>;

    /// Closed hourly bars on the `[start_ms, end_ms)` grid, with the first
    /// instant the rows were available to this process.
    fn klines(
        &self,
        symbol: &str,
        start_ms: i64,
        end_ms: i64,
        page_limit: usize,
    ) -> BoxFuture<'_, Result<FetchedKlineRows, WorkerError>>;

    /// Settled funding on the `[start_ms, end_ms]` hourly grid. `interval_hours`
    /// is the instrument's settlement interval, stamped onto every row.
    fn funding(
        &self,
        symbol: &str,
        start_ms: i64,
        end_ms: i64,
        page_limit: usize,
        interval_hours: Option<i64>,
    ) -> BoxFuture<'_, Result<FetchedFundingRows, WorkerError>>;

    fn open_stream(&self, symbols: Vec<String>) -> Result<Box<dyn PublicStream>, WorkerError>;

    /// The successor of a stream this process is replacing: it keeps the
    /// outgoing stream's epoch numbering, gap stamp and fault clocks.
    fn open_stream_continuing(
        &self,
        symbols: Vec<String>,
        continuity: StreamContinuity,
    ) -> Result<Box<dyn PublicStream>, WorkerError>;
}

pub fn open_public_venue(
    kind: PublicVenueKind,
    config: &SignalWorkerConfig,
    request_budget: Arc<Semaphore>,
) -> Result<Arc<dyn PublicVenue>, WorkerError> {
    match kind {
        PublicVenueKind::Bybit => Ok(Arc::new(bybit::BybitPublicVenue::open(
            config,
            request_budget,
        )?)),
        PublicVenueKind::Mexc => Ok(Arc::new(mexc::MexcPublicVenue::open(
            config,
            request_budget,
        )?)),
        PublicVenueKind::Hyperliquid => Ok(Arc::new(hyperliquid::HyperliquidPublicVenue::open(
            config,
            request_budget,
        )?)),
    }
}

/// One instrument-table read: every listed name of every status.
/// `observed_ts_ms` is when the read started, `available_at_ms` the first
/// instant the rows were in this process.
pub struct FetchedInstruments {
    pub observed_ts_ms: i64,
    pub available_at_ms: i64,
    pub rows: Vec<BybitInstrumentWire>,
}

pub struct FetchedTickers {
    pub request_started_at_ms: i64,
    pub observed_ts_ms: i64,
    pub available_at_ms: i64,
    pub rows: Vec<BybitTickerWire>,
}

pub(crate) fn validate_fetched_instruments(
    fetched: &FetchedInstruments,
) -> Result<(), WorkerError> {
    normalize_instruments(
        fetched.observed_ts_ms,
        fetched.available_at_ms,
        &fetched.rows,
    )
    .map(drop)
}

/// A whole ticker page: rows that fail are left out downstream, but a page
/// with rows and nothing usable is a failed fetch.
pub(crate) fn validate_fetched_tickers(fetched: &FetchedTickers) -> Result<(), WorkerError> {
    let (kept, rejected) = crate::normalize::normalize_tickers_reporting(
        fetched.observed_ts_ms,
        fetched.available_at_ms,
        &fetched.rows,
    )?;
    if kept.is_empty() && !rejected.rows.is_empty() {
        return Err(WorkerError::input(format!(
            "no usable ticker rows: {}",
            rejected.summary("ticker").unwrap_or_default()
        )));
    }
    Ok(())
}

/// How many grid slots a `[start, end)` or `[start, end]` request may return,
/// so a venue cannot answer a bounded window with an unbounded page.
pub(crate) fn source_grid_slots(
    start_ms: i64,
    end_ms: i64,
    step_ms: i64,
    end_inclusive: bool,
) -> Result<usize, WorkerError> {
    if start_ms < 0 || end_ms < start_ms || step_ms <= 0 {
        return Err(WorkerError::state("source fetch range is invalid"));
    }
    let remainder = start_ms.rem_euclid(step_ms);
    let first = if remainder == 0 {
        start_ms
    } else {
        start_ms
            .checked_add(step_ms - remainder)
            .ok_or_else(|| WorkerError::state("source fetch range overflowed"))?
    };
    let Some(last_bound) = end_inclusive
        .then_some(end_ms)
        .or_else(|| end_ms.checked_sub(1))
    else {
        return Ok(0);
    };
    if first > last_bound {
        return Ok(0);
    }
    usize::try_from((last_bound - first) / step_ms + 1)
        .map_err(|_| WorkerError::state("source fetch row bound exceeds usize"))
}

pub(crate) fn validate_source_grid_timestamp(
    timestamp_ms: i64,
    start_ms: i64,
    end_ms: i64,
    step_ms: i64,
    end_inclusive: bool,
    label: &str,
) -> Result<(), WorkerError> {
    let in_range = timestamp_ms >= start_ms
        && if end_inclusive {
            timestamp_ms <= end_ms
        } else {
            timestamp_ms < end_ms
        };
    if !in_range || timestamp_ms.rem_euclid(step_ms) != 0 {
        return Err(WorkerError::network(format!(
            "{label} is outside the requested source grid"
        )));
    }
    Ok(())
}

pub(crate) fn validate_source_page_rows(
    actual: usize,
    limit: usize,
    label: &str,
) -> Result<(), WorkerError> {
    if actual > limit {
        return Err(WorkerError::network(format!(
            "{label} response exceeded the requested page limit"
        )));
    }
    Ok(())
}

/// A wire integer: JSON number or decimal string, both milliseconds when the
/// field is a clock.
pub(crate) fn wire_i64(value: Option<&Value>, label: &str) -> Result<i64, WorkerError> {
    match value {
        Some(Value::Number(number)) => number.as_i64(),
        Some(Value::String(text)) => text.parse().ok(),
        _ => None,
    }
    .ok_or_else(|| WorkerError::network(format!("{label} is not an integer")))
}

pub(crate) fn wire_object_map(
    value: &Value,
    key: &str,
) -> Result<BTreeMap<String, Value>, WorkerError> {
    value
        .get(key)
        .and_then(Value::as_object)
        .map(|object| {
            object
                .iter()
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect()
        })
        .ok_or_else(|| WorkerError::network(format!("Bybit instrument lacks {key}")))
}

pub(crate) fn wire_text(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::{open_public_venue, PublicVenueKind};
    use std::sync::Arc;
    use tokio::sync::Semaphore;

    #[test]
    fn the_three_venue_names_round_trip_and_nothing_else_parses() {
        for kind in [
            PublicVenueKind::Bybit,
            PublicVenueKind::Mexc,
            PublicVenueKind::Hyperliquid,
        ] {
            assert_eq!(PublicVenueKind::parse(kind.as_str()).unwrap(), kind);
        }
        for refused in ["", "Bybit", "bybit_mainnet", "binance", "okx"] {
            let error = PublicVenueKind::parse(refused).unwrap_err();
            assert!(error.to_string().contains("public_venue"), "{error}");
        }
    }

    /// Every venue this worker names opens, and each one states the coin its
    /// perpetuals settle in: Hyperliquid margins in USDC while the engine's
    /// spelling of its symbols keeps `USDT`.
    #[test]
    fn every_venue_opens_and_states_its_settle_coin() {
        let mut config = crate::config::tests::checked_realm_config("demo");
        for (named, kind, settle_coin) in [
            ("bybit", PublicVenueKind::Bybit, "USDT"),
            ("mexc", PublicVenueKind::Mexc, "USDT"),
            ("hyperliquid", PublicVenueKind::Hyperliquid, "USDC"),
        ] {
            config.sources.public_venue = named.into();
            let venue = open_public_venue(
                config.public_venue().unwrap(),
                &config,
                Arc::new(Semaphore::new(1)),
            )
            .unwrap();
            assert_eq!(venue.kind(), kind);
            assert_eq!(venue.settle_coin(), settle_coin);
        }
    }
}
