use super::*;
use crate::http::percent_encode;

pub(super) fn spawn_instrument_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    venue: Arc<dyn PublicVenue>,
    listing: Option<ListingSource>,
    max_pages: usize,
) {
    tokio::spawn(async move {
        let result = fetch_universe_inputs(venue.as_ref(), listing, max_pages).await;
        let _ = lane_tx.send(LaneCompletion::Instruments(result)).await;
    });
}

/// A venue whose listing bounds the universe.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum ListingVenue {
    Hyperliquid,
    Mexc,
}

impl ListingVenue {
    pub(super) fn parse(value: &str) -> Result<Self, WorkerError> {
        match value {
            "hyperliquid" => Ok(Self::Hyperliquid),
            "mexc" => Ok(Self::Mexc),
            other => Err(WorkerError::config(format!(
                "universe listed_on {other:?} names no venue this worker can ask"
            ))),
        }
    }

    /// The host comes from that venue's realm table in `engine-public`. No
    /// venue host is written down here.
    fn host(self) -> &'static str {
        match self {
            Self::Hyperliquid => engine_public::HyperliquidRealm::Mainnet
                .rest_base()
                .trim_start_matches("https://"),
            Self::Mexc => engine_public::MexcRealm::Mainnet
                .rest_base()
                .trim_start_matches("https://"),
        }
    }
}

/// The listing client, on the same timeout, retry and request budget as every
/// other public source.
#[derive(Clone)]
pub(super) struct ListingSource {
    venue: ListingVenue,
    client: PublicHttpClient,
}

impl ListingSource {
    pub(super) fn new(
        venue: ListingVenue,
        timeout_ms: u64,
        retries: usize,
        retry_base_ms: u64,
        request_budget: Arc<Semaphore>,
    ) -> Result<Self, WorkerError> {
        Ok(Self {
            venue,
            client: PublicHttpClient::new(
                venue.host(),
                timeout_ms,
                retries,
                retry_base_ms,
                request_budget,
            )?,
        })
    }

    #[cfg(test)]
    pub(super) fn venue(&self) -> ListingVenue {
        self.venue
    }

    pub(super) async fn fetch(&self) -> Result<BTreeSet<String>, WorkerError> {
        match self.venue {
            ListingVenue::Hyperliquid => {
                let (payload, _) = self
                    .client
                    .post_json("/info", &serde_json::json!({"type": "meta"}))
                    .await?;
                hyperliquid_listed_symbols(&payload)
            }
            ListingVenue::Mexc => {
                let (payload, _) = self.client.get("/api/v1/contract/detail", "").await?;
                mexc_listed_symbols(&payload)
            }
        }
    }
}

/// Every USDT-settled perpetual MEXC takes API orders on, in the engine's
/// spelling: the venue says `BTC_USDT`, the engine says `BTCUSDT`, the same
/// rule as `engine-public/src/venues/mexc/contracts.rs`. A row the engine's
/// own table reader would drop (inverse, not API-tradable, not in the normal
/// state) is not listed here either, so the two agree on the name set.
pub(super) fn mexc_listed_symbols(payload: &Value) -> Result<BTreeSet<String>, WorkerError> {
    let rows = payload
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::network("MEXC contract detail lacks data"))?;
    let mut listed = BTreeSet::new();
    for row in rows {
        let text = |key: &str| {
            row.get(key)
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
        };
        let (Some(base), Some(quote), Some(settle)) =
            (text("baseCoin"), text("quoteCoin"), text("settleCoin"))
        else {
            continue;
        };
        if quote != "USDT" || settle != quote {
            continue;
        }
        if !row
            .get("apiAllowed")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        if row.get("state").and_then(Value::as_i64).unwrap_or(-1) != 0 {
            continue;
        }
        listed.insert(format!("{}{}", base, quote).to_ascii_uppercase());
    }
    if listed.is_empty() {
        return Err(WorkerError::network(
            "MEXC contract detail listed no tradable USDT perpetual",
        ));
    }
    Ok(listed)
}

/// Every perpetual Hyperliquid lists, in the engine's spelling. The venue says
/// `BTC` and `kPEPE`; the engine says `BTCUSDT` and `KPEPEUSDT`, the same rule
/// as `engine-venue/src/venues/hyperliquid/assets.rs::symbol_of`.
pub(super) fn hyperliquid_listed_symbols(payload: &Value) -> Result<BTreeSet<String>, WorkerError> {
    let rows = payload
        .get("universe")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::network("Hyperliquid meta lacks universe"))?;
    let mut listed = BTreeSet::new();
    for row in rows {
        if row
            .get("isDelisted")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let name = row
            .get("name")
            .and_then(Value::as_str)
            .map(|name| name.trim())
            .filter(|name| !name.is_empty())
            .ok_or_else(|| WorkerError::network("Hyperliquid meta row lacks name"))?;
        listed.insert(format!("{}USDT", name.to_ascii_uppercase()));
    }
    if listed.is_empty() {
        return Err(WorkerError::network("Hyperliquid meta listed no perpetual"));
    }
    Ok(listed)
}

pub(super) fn spawn_gate_lane(lane_tx: mpsc::Sender<LaneCompletion>, path: PathBuf) {
    tokio::spawn(async move {
        let result = tokio::task::spawn_blocking(move || read_gate_candidates(&path))
            .await
            .map_err(|error| WorkerError::state(format!("LLM gate read task stopped: {error}")))
            .and_then(|result| result);
        let _ = lane_tx.send(LaneCompletion::Gate(result)).await;
    });
}

/// Read the ledger's publication whole. An absent file is the steady state
/// before the ledger's first run and reads as nothing; a malformed one is a
/// source fault for the lane, never a worker error.
pub(super) fn read_gate_candidates(path: &Path) -> Result<Option<FetchedGate>, WorkerError> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(WorkerError::io("read LLM gate candidates", error)),
    };
    let read_at_ms = wall_ms()?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| WorkerError::input(format!("LLM gate candidates JSON: {error}")))?;
    let object = value
        .as_object()
        .ok_or_else(|| WorkerError::input("LLM gate candidates must be an object"))?;
    let clock = |key: &str| -> Result<i64, WorkerError> {
        object
            .get(key)
            .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
            .filter(|v| *v > 0)
            .ok_or_else(|| WorkerError::input(format!("LLM gate candidates lack {key}")))
    };
    let decision_ts_ms = clock("decision_ts_ms")?;
    let valid_until_ms = clock("valid_until_ms")?;
    let number = |row: &serde_json::Map<String, Value>, key: &str| -> Option<f64> {
        row.get(key)
            .and_then(Value::as_f64)
            .filter(|v| v.is_finite())
    };
    let mut rows = Vec::new();
    for event in object
        .get("events")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::input("LLM gate candidates lack events"))?
    {
        let Some(row) = event.as_object() else {
            return Err(WorkerError::input("LLM gate event is not an object"));
        };
        let symbol = row
            .get("symbol")
            .and_then(Value::as_str)
            .ok_or_else(|| WorkerError::input("LLM gate event lacks a symbol"))?;
        rows.push(crate::model::LlmGateCandidate {
            symbol: symbol.trim().to_ascii_uppercase(),
            score: number(row, "score").unwrap_or(0.0),
            band: row
                .get("band")
                .and_then(Value::as_str)
                .unwrap_or("core")
                .to_owned(),
            trigger_ts_ms: number(row, "trigger_ts_ms").map_or(0, |v| v as i64),
            trigger_price: number(row, "trigger_price").unwrap_or(0.0),
            atr_pct: number(row, "atr_pct").unwrap_or(0.0),
            sigma_daily_30d: number(row, "sigma_daily_30d"),
            turnover_rank: number(row, "turnover_rank"),
            trigger_window_h: number(row, "trigger_window_h").map(|v| v as i64),
        });
    }
    Ok(Some(FetchedGate {
        read_at_ms,
        decision_ts_ms,
        valid_until_ms,
        rows,
    }))
}

pub(super) async fn fetch_universe_inputs(
    venue: &dyn PublicVenue,
    listing_source: Option<ListingSource>,
    max_pages: usize,
) -> Result<FetchedUniverseInputs, WorkerError> {
    let instruments = venue.instruments(max_pages).await?;
    let tickers = venue.ticker_page().await?;
    let listing = match listing_source {
        Some(source) => match source.fetch().await {
            Ok(listed) => Some(Ok(listed)),
            Err(error) if error.is_lane_local_source_failure() => Some(Err(error)),
            Err(error) => return Err(error),
        },
        None => None,
    };
    Ok(FetchedUniverseInputs {
        instruments,
        tickers,
        listing,
    })
}

pub(super) fn spawn_funding_fetch_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    venue: Arc<dyn PublicVenue>,
    page_limit: usize,
    jobs: Vec<FundingJob>,
    instruments: Arc<BTreeMap<String, crate::model::InstrumentObservation>>,
) {
    tokio::spawn(async move {
        let mut succeeded = true;
        for job in jobs {
            let result = fetch_funding_job(venue.as_ref(), page_limit, job, &instruments).await;
            let fetched_without_failures = result
                .as_ref()
                .is_ok_and(|fetched| fetched.failures.is_empty());
            let (resume_tx, resume_rx) = oneshot::channel();
            if lane_tx
                .send(LaneCompletion::FundingChunk {
                    result,
                    resume: resume_tx,
                })
                .await
                .is_err()
            {
                return;
            }
            match resume_rx.await {
                Ok(true) => {
                    if !fetched_without_failures {
                        succeeded = false;
                    }
                }
                _ => {
                    succeeded = false;
                    break;
                }
            }
        }
        let _ = lane_tx
            .send(LaneCompletion::FundingFinished { succeeded })
            .await;
    });
}

pub(super) fn spawn_whale_fetch_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    client: PublicHttpClient,
    page_limit: usize,
    jobs: Vec<WhaleJob>,
) {
    tokio::spawn(async move {
        for job in jobs {
            let result = fetch_whale_job(client.clone(), page_limit, job).await;
            if !send_whale_chunk_and_wait(&lane_tx, result).await {
                break;
            }
        }
        let _ = lane_tx.send(LaneCompletion::WhaleFinished).await;
    });
}

pub(super) async fn send_whale_chunk_and_wait(
    lane_tx: &mpsc::Sender<LaneCompletion>,
    result: Result<FetchedWhales, WorkerError>,
) -> bool {
    let (resume_tx, resume_rx) = oneshot::channel();
    if lane_tx
        .send(LaneCompletion::WhaleChunk {
            result,
            resume: resume_tx,
        })
        .await
        .is_err()
    {
        return false;
    }
    resume_rx.await == Ok(true)
}

pub(super) fn spawn_repair_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    venue: Arc<dyn PublicVenue>,
    page_limit: usize,
    jobs: Vec<(String, i64, i64)>,
    end_ms: i64,
    epoch: Option<u64>,
) {
    tokio::spawn(async move {
        for job in jobs {
            let result = fetch_kline_job(venue.as_ref(), page_limit, job).await;
            if !send_repair_chunk_and_wait(&lane_tx, result).await {
                break;
            }
        }
        let _ = lane_tx
            .send(LaneCompletion::RepairFinished { end_ms, epoch })
            .await;
    });
}

pub(super) async fn send_repair_chunk_and_wait(
    lane_tx: &mpsc::Sender<LaneCompletion>,
    result: Result<FetchedKlineJobs, WorkerError>,
) -> bool {
    let (resume_tx, resume_rx) = oneshot::channel();
    if lane_tx
        .send(LaneCompletion::RepairChunk {
            result,
            resume: resume_tx,
        })
        .await
        .is_err()
    {
        return false;
    }
    resume_rx.await == Ok(true)
}

pub(super) async fn fetch_kline_job(
    venue: &dyn PublicVenue,
    page_limit: usize,
    (symbol, start, end): KlineJob,
) -> Result<FetchedKlineJobs, WorkerError> {
    let result = venue.klines(&symbol, start, end, page_limit).await;
    match result {
        Ok((rows, available_at_ms)) => Ok(FetchedKlineJobs {
            batches: vec![(
                symbol,
                FetchedKlineBatch {
                    rows,
                    available_at_ms,
                    checked_from_ms: Some(start),
                    checked_through_ms: Some(end),
                },
            )],
            failures: Vec::new(),
        }),
        Err(error) if error.is_lane_local_source_failure() => Ok(FetchedKlineJobs {
            batches: Vec::new(),
            failures: vec![(symbol, error.to_string())],
        }),
        Err(error) => Err(error),
    }
}

pub(super) async fn fetch_funding_job(
    venue: &dyn PublicVenue,
    page_limit: usize,
    (symbol, checked_from_ms, checked_through_ms, emit_lifecycle): FundingJob,
    instruments: &BTreeMap<String, crate::model::InstrumentObservation>,
) -> Result<FetchedFunding, WorkerError> {
    let interval_hours = instruments
        .get(&symbol)
        .and_then(|row| row.funding_interval_min)
        .filter(|minutes| *minutes > 0 && *minutes % 60 == 0)
        .map(|minutes| minutes / 60);
    let result = venue
        .funding(
            &symbol,
            checked_from_ms,
            checked_through_ms,
            page_limit,
            interval_hours,
        )
        .await;
    let (rows, available_at_ms) = match result {
        Ok(fetched) => fetched,
        Err(error) if error.is_lane_local_source_failure() => {
            return Ok(FetchedFunding {
                batches: Vec::new(),
                failures: vec![(symbol, error.to_string())],
            });
        }
        Err(error) => return Err(error),
    };
    let intervals = interval_hours
        .map(|hours| hours.saturating_mul(HOUR_MS))
        .filter(|interval_ms| *interval_ms > 0)
        .map(|interval_ms| {
            complete_funding_coverage(checked_from_ms, checked_through_ms, interval_ms, &rows)
        })
        .transpose()?
        .unwrap_or_default();
    let batches = if intervals.is_empty() {
        vec![(
            symbol,
            FetchedFundingBatch {
                rows,
                available_at_ms,
                checked_from_ms: None,
                checked_through_ms: None,
                emit_lifecycle,
            },
        )]
    } else {
        let mut rows = Some(rows);
        intervals
            .into_iter()
            .enumerate()
            .map(|(index, (from, through))| {
                (
                    symbol.clone(),
                    FetchedFundingBatch {
                        rows: rows.take().unwrap_or_default(),
                        available_at_ms,
                        checked_from_ms: Some(from),
                        checked_through_ms: Some(through),
                        emit_lifecycle: emit_lifecycle && index == 0,
                    },
                )
            })
            .collect()
    };
    Ok(FetchedFunding {
        batches,
        failures: Vec::new(),
    })
}

pub(super) fn complete_funding_coverage(
    start_ms: i64,
    end_ms: i64,
    interval_ms: i64,
    rows: &[BybitFundingWire],
) -> Result<Vec<(i64, i64)>, WorkerError> {
    if start_ms >= end_ms || interval_ms <= 0 || interval_ms % HOUR_MS != 0 {
        return Ok(Vec::new());
    }
    let timestamps = rows
        .iter()
        .map(|row| {
            wire_i64(
                Some(&row.funding_rate_timestamp),
                "Bybit complete funding timestamp",
            )
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    let mut groups = Vec::<(i64, i64)>::new();
    for timestamp in timestamps
        .into_iter()
        .filter(|timestamp| start_ms <= *timestamp && *timestamp <= end_ms)
    {
        if let Some((_, last)) = groups.last_mut() {
            if timestamp == last.saturating_add(interval_ms) {
                *last = timestamp;
                continue;
            }
        }
        groups.push((timestamp, timestamp));
    }
    Ok(groups
        .into_iter()
        .filter_map(|(first, last)| {
            let from = if first.saturating_sub(start_ms) <= interval_ms {
                start_ms
            } else {
                first
            };
            let through = if end_ms.saturating_sub(last) < interval_ms {
                end_ms
            } else {
                last
            };
            (from < through).then_some((from, through))
        })
        .collect())
}

pub(super) async fn fetch_whale_job(
    client: PublicHttpClient,
    page_limit: usize,
    (symbol, start_ms, end_ms): WhaleJob,
) -> Result<FetchedWhales, WorkerError> {
    let (mut rows, received) =
        fetch_whale_symbol(client, page_limit, &symbol, start_ms, end_ms).await?;
    let coverage = complete_whale_coverage(&symbol, start_ms, end_ms, &rows)?;
    rows.sort_by(|left, right| {
        let left_ts =
            wire_i64(Some(&left.day_end_ms), "Binance whale timestamp").unwrap_or(i64::MAX);
        let right_ts =
            wire_i64(Some(&right.day_end_ms), "Binance whale timestamp").unwrap_or(i64::MAX);
        (&left.symbol, left_ts).cmp(&(&right.symbol, right_ts))
    });
    Ok(FetchedWhales {
        available_at_ms: end_ms.max(received),
        rows,
        coverage,
    })
}

pub(super) fn complete_whale_coverage(
    symbol: &str,
    start_ms: i64,
    end_ms: i64,
    rows: &[BinanceWhaleWire],
) -> Result<Vec<SourceCoverage>, WorkerError> {
    rows.iter()
        .filter_map(|row| {
            let day_end_ms = match wire_i64(
                Some(&row.day_end_ms),
                "Binance complete whale day timestamp",
            ) {
                Ok(value) => value,
                Err(error) => return Some(Err(error)),
            };
            (day_end_ms > start_ms && day_end_ms <= end_ms).then_some(Ok(SourceCoverage {
                symbol: symbol.to_owned(),
                checked_from_ms: day_end_ms.saturating_sub(DAY_MS),
                checked_through_ms: day_end_ms,
                replace_coverage: false,
            }))
        })
        .collect()
}

pub(super) fn current_trading_instrument(
    state: &crate::worker::WorkerState,
    symbol: &str,
    settle_coin: &str,
) -> bool {
    state.instruments.get(symbol).is_some_and(|row| {
        row.status.as_deref() == Some("Trading")
            && row.settle_coin.as_deref() == Some(settle_coin)
            && !row.is_prelisting
    })
}

pub(super) fn instrument_trading_at(
    state: &crate::worker::WorkerState,
    symbol: &str,
    timestamp_ms: i64,
) -> bool {
    trading_intervals_contain(
        state
            .instrument_trading_intervals
            .get(symbol)
            .map(Vec::as_slice),
        state
            .instrument_status_unknown_since_ms
            .get(symbol)
            .copied(),
        timestamp_ms,
    )
}

pub(super) fn instrument_source_ranges(
    state: &crate::worker::WorkerState,
    symbol: &str,
    required_from_ms: i64,
    required_through_ms: i64,
    alignment_ms: i64,
) -> Vec<(i64, i64)> {
    bounded_instrument_source_ranges(
        state
            .instrument_trading_intervals
            .get(symbol)
            .map(Vec::as_slice),
        state
            .instrument_status_unknown_since_ms
            .get(symbol)
            .copied(),
        required_from_ms,
        required_through_ms,
        alignment_ms,
    )
}

pub(super) fn trading_intervals_contain(
    intervals: Option<&[InstrumentTradingInterval]>,
    unknown_since_ms: Option<i64>,
    timestamp_ms: i64,
) -> bool {
    if unknown_since_ms.is_some_and(|unknown_since| timestamp_ms >= unknown_since) {
        return false;
    }
    intervals.is_some_and(|intervals| {
        intervals.iter().any(|interval| {
            interval.trading_from_ms <= timestamp_ms
                && interval
                    .trading_through_ms
                    .is_none_or(|through| timestamp_ms < through)
        })
    })
}

pub(super) fn bounded_instrument_source_ranges(
    intervals: Option<&[InstrumentTradingInterval]>,
    unknown_through_ms: Option<i64>,
    required_from_ms: i64,
    required_through_ms: i64,
    alignment_ms: i64,
) -> Vec<(i64, i64)> {
    if required_from_ms >= required_through_ms || alignment_ms <= 0 {
        return Vec::new();
    }
    let mut ranges = intervals
        .into_iter()
        .flatten()
        .filter_map(|interval| {
            let from = required_from_ms.max(align_up(interval.trading_from_ms, alignment_ms));
            let eligible_through_ms = interval
                .trading_through_ms
                .into_iter()
                .chain(unknown_through_ms)
                .min()
                .unwrap_or(required_through_ms);
            let through = align_down(required_through_ms.min(eligible_through_ms), alignment_ms);
            (from < through).then_some((from, through))
        })
        .collect::<Vec<_>>();
    ranges.sort_unstable();
    let mut merged = Vec::<(i64, i64)>::new();
    for range in ranges {
        if let Some(last) = merged.last_mut() {
            if range.0 <= last.1 {
                last.1 = last.1.max(range.1);
                continue;
            }
        }
        merged.push(range);
    }
    merged
}

pub(super) fn align_up(value: i64, alignment: i64) -> i64 {
    let remainder = value.rem_euclid(alignment);
    if remainder == 0 {
        value
    } else {
        value.saturating_add(alignment.saturating_sub(remainder))
    }
}

pub(super) fn align_down(value: i64, alignment: i64) -> i64 {
    value.saturating_sub(value.rem_euclid(alignment))
}

pub(super) fn first_missing_kline_hour(
    state: &crate::worker::WorkerState,
    symbol: &str,
    start_ms: i64,
    end_ms: i64,
) -> Option<i64> {
    let Some(rows) = state.klines.get(symbol) else {
        return (start_ms < end_ms).then_some(start_ms);
    };
    let mut timestamp = start_ms;
    while timestamp < end_ms {
        if !rows.contains_key(&timestamp) {
            return Some(timestamp);
        }
        timestamp = timestamp.saturating_add(HOUR_MS);
    }
    None
}

pub(super) fn closed_kline_end(now_ms: i64) -> i64 {
    let publishable_ms = now_ms.saturating_sub(KLINE_PUBLICATION_LAG_MS);
    publishable_ms - publishable_ms.rem_euclid(HOUR_MS)
}

pub(super) fn whale_fetch_bounds(
    start_ms: i64,
    end_ms: i64,
) -> Result<(i64, i64, usize), WorkerError> {
    let query_start_ms = start_ms - start_ms.rem_euclid(FIVE_MIN_MS);
    let query_end_ms = end_ms - end_ms.rem_euclid(FIVE_MIN_MS);
    let retained_row_cap = source_grid_slots(start_ms, end_ms, FIVE_MIN_MS, true)?;
    Ok((query_start_ms, query_end_ms, retained_row_cap))
}

pub(super) async fn fetch_whale_symbol(
    client: PublicHttpClient,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
) -> Result<(Vec<BinanceWhaleWire>, i64), WorkerError> {
    let page_row_cap = page_limit;
    let (query_start, query_end, retained_row_cap) = whale_fetch_bounds(start, end)?;
    let page_limit = i64::try_from(page_limit)
        .map_err(|_| WorkerError::config("whale page limit exceeds i64"))?;
    let mut cursor = query_start;
    let mut by_time = BTreeMap::<i64, Option<Value>>::new();
    let mut available = start;
    while cursor <= query_end {
        let window_end = cursor
            .saturating_add((page_limit - 1).max(0).saturating_mul(FIVE_MIN_MS))
            .min(query_end);
        let query = format!(
            "symbol={}&period=5m&startTime={cursor}&endTime={window_end}&limit={page_limit}",
            percent_encode(symbol),
        );
        let (payload, received) = client
            .get("/futures/data/topLongShortPositionRatio", &query)
            .await?;
        available = available.max(received);
        let list = payload
            .as_array()
            .ok_or_else(|| WorkerError::network("Binance whale response is not a list"))?;
        validate_source_page_rows(list.len(), page_row_cap, "Binance whale")?;
        for value in list {
            let timestamp = wire_i64(value.get("timestamp"), "Binance whale timestamp")?;
            let ratio = value.get("longShortRatio").cloned();
            validate_source_grid_timestamp(
                timestamp,
                query_start,
                query_end,
                FIVE_MIN_MS,
                true,
                "Binance whale timestamp",
            )?;
            if timestamp < start || timestamp > end {
                continue;
            }
            if !by_time.contains_key(&timestamp) && by_time.len() >= retained_row_cap {
                return Err(WorkerError::network(
                    "Binance whale response exceeded the requested grid cardinality",
                ));
            }
            if let Some(existing) = by_time.insert(timestamp, ratio.clone()) {
                if existing != ratio {
                    return Err(WorkerError::network(
                        "Binance whale pagination returned conflicting duplicate",
                    ));
                }
            }
        }
        if window_end == query_end {
            break;
        }
        cursor = window_end.saturating_add(FIVE_MIN_MS);
    }
    let first_day_end = (start - start.rem_euclid(DAY_MS)).saturating_add(DAY_MS);
    let complete_end = end - end.rem_euclid(DAY_MS);
    let mut rows = Vec::new();
    let mut day_end = first_day_end;
    while day_end <= complete_end {
        let day_start = day_end - DAY_MS;
        let complete = (0..(DAY_MS / FIVE_MIN_MS))
            .all(|offset| by_time.contains_key(&(day_start + offset * FIVE_MIN_MS)));
        if complete {
            rows.push(BinanceWhaleWire {
                symbol: symbol.to_owned(),
                day_end_ms: Value::from(day_end),
                long_short_ratio: by_time.get(&(day_end - FIVE_MIN_MS)).cloned().flatten(),
            });
        }
        day_end = day_end.saturating_add(DAY_MS);
    }
    normalize_whales(available, &rows)?;
    Ok((rows, available))
}

#[cfg(test)]
mod tests;
