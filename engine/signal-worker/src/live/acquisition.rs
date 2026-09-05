use super::*;

pub(super) fn spawn_instrument_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    instrument_client: PublicHttpClient,
    ticker_client: PublicHttpClient,
    category: String,
    max_pages: usize,
) {
    tokio::spawn(async move {
        let result =
            fetch_universe_inputs(instrument_client, ticker_client, category, max_pages).await;
        let _ = lane_tx.send(LaneCompletion::Instruments(result)).await;
    });
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
    instrument_client: PublicHttpClient,
    ticker_client: PublicHttpClient,
    category: String,
    max_pages: usize,
) -> Result<FetchedUniverseInputs, WorkerError> {
    let instruments =
        fetch_instrument_snapshot(instrument_client, category.clone(), max_pages).await?;
    let tickers = fetch_ticker_page(ticker_client, category).await?;
    Ok(FetchedUniverseInputs {
        instruments,
        tickers,
    })
}

/// The whole ticker page, unfiltered: the universe ranks every listed name.
pub(super) async fn fetch_ticker_page(
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

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn_funding_fetch_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    client: PublicHttpClient,
    category: String,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<FundingJob>,
    instruments: Arc<BTreeMap<String, crate::model::InstrumentObservation>>,
) {
    tokio::spawn(async move {
        let mut succeeded = true;
        for chunk in funding_job_chunks(&jobs) {
            let result = fetch_funding_batches(
                client.clone(),
                category.clone(),
                page_limit,
                max_parallel,
                chunk.to_vec(),
                Arc::clone(&instruments),
            )
            .await;
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
    max_parallel: usize,
    jobs: Vec<WhaleJob>,
) {
    tokio::spawn(async move {
        for chunk in whale_job_chunks(&jobs) {
            let result =
                fetch_whale_batch(client.clone(), page_limit, max_parallel, chunk.to_vec()).await;
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

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn_repair_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    client: PublicHttpClient,
    category: String,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<(String, i64, i64)>,
    end_ms: i64,
    epoch: Option<u64>,
) {
    tokio::spawn(async move {
        for chunk in kline_job_chunks(&jobs) {
            let result = fetch_kline_jobs_bounded(
                client.clone(),
                category.clone(),
                page_limit,
                max_parallel,
                chunk.to_vec(),
            )
            .await;
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

pub(super) async fn fetch_instrument_snapshot(
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

pub(super) async fn fetch_ticker_snapshot(
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

pub(super) async fn fetch_kline_jobs_bounded(
    client: PublicHttpClient,
    category: String,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<(String, i64, i64)>,
) -> Result<FetchedKlineJobs, WorkerError> {
    if jobs.len() > KLINE_FETCH_CHUNK_SIZE {
        return Err(WorkerError::state(format!(
            "kline fetch retained {} jobs; maximum chunk is {KLINE_FETCH_CHUNK_SIZE}",
            jobs.len()
        )));
    }
    let limiter = Arc::new(Semaphore::new(max_parallel));
    let mut tasks = JoinSet::new();
    for (symbol, start, end) in jobs {
        let limiter = Arc::clone(&limiter);
        let client = client.clone();
        let category = category.clone();
        tasks.spawn(async move {
            let result = match limiter.acquire_owned().await {
                Ok(_permit) => {
                    fetch_klines(client, &category, page_limit, &symbol, start, end).await
                }
                Err(_) => Err(WorkerError::state(
                    "public request concurrency limiter closed",
                )),
            };
            (symbol, start, end, result)
        });
    }
    let mut fetched = Vec::new();
    let mut failures = Vec::new();
    while let Some(joined) = tasks.join_next().await {
        let (symbol, start, end, result) = joined
            .map_err(|error| WorkerError::state(format!("public fetch task failed: {error}")))?;
        match result {
            Ok((rows, available_at_ms)) => {
                fetched.push((
                    symbol,
                    FetchedKlineBatch {
                        rows,
                        available_at_ms,
                        checked_from_ms: Some(start),
                        checked_through_ms: Some(end),
                    },
                ));
            }
            Err(error) if error.is_lane_local_source_failure() => {
                failures.push((symbol, error.to_string()));
            }
            Err(error) => return Err(error),
        }
    }
    fetched.sort_by(|left, right| {
        (&left.0, left.1.checked_from_ms).cmp(&(&right.0, right.1.checked_from_ms))
    });
    failures.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(FetchedKlineJobs {
        batches: fetched,
        failures,
    })
}

#[allow(clippy::too_many_arguments)]
pub(super) async fn fetch_funding_batches(
    client: PublicHttpClient,
    category: String,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<FundingJob>,
    instruments: Arc<BTreeMap<String, crate::model::InstrumentObservation>>,
) -> Result<FetchedFunding, WorkerError> {
    if jobs.len() > FUNDING_FETCH_CHUNK_SIZE {
        return Err(WorkerError::state(format!(
            "funding fetch retained {} jobs; maximum chunk is {FUNDING_FETCH_CHUNK_SIZE}",
            jobs.len()
        )));
    }
    let limiter = Arc::new(Semaphore::new(max_parallel));
    let mut tasks = JoinSet::new();
    for (symbol, start_ms, end_ms, emit_lifecycle) in jobs {
        let interval_hours = instruments
            .get(&symbol)
            .and_then(|row| row.funding_interval_min)
            .filter(|minutes| *minutes > 0 && *minutes % 60 == 0)
            .map(|minutes| minutes / 60);
        let limiter = Arc::clone(&limiter);
        let client = client.clone();
        let category = category.clone();
        tasks.spawn(async move {
            let result = match limiter.acquire_owned().await {
                Ok(_permit) => {
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
                }
                Err(_) => Err(WorkerError::state(
                    "public request concurrency limiter closed",
                )),
            };
            (
                symbol,
                start_ms,
                end_ms,
                emit_lifecycle,
                interval_hours,
                result,
            )
        });
    }
    let mut batches = Vec::new();
    let mut failures = Vec::new();
    while let Some(joined) = tasks.join_next().await {
        let (symbol, checked_from_ms, checked_through_ms, emit_lifecycle, interval_hours, result) =
            joined
                .map_err(|error| WorkerError::state(format!("public fetch task failed: {error}")))?;
        match result {
            Ok((rows, available_at_ms)) => {
                let intervals = interval_hours
                    .map(|hours| hours.saturating_mul(HOUR_MS))
                    .filter(|interval_ms| *interval_ms > 0)
                    .map(|interval_ms| {
                        complete_funding_coverage(
                            checked_from_ms,
                            checked_through_ms,
                            interval_ms,
                            &rows,
                        )
                    })
                    .transpose()?
                    .unwrap_or_default();
                if intervals.is_empty() {
                    batches.push((
                        symbol,
                        FetchedFundingBatch {
                            rows,
                            available_at_ms,
                            checked_from_ms: None,
                            checked_through_ms: None,
                            emit_lifecycle,
                        },
                    ));
                } else {
                    let mut rows = Some(rows);
                    for (index, (from, through)) in intervals.into_iter().enumerate() {
                        batches.push((
                            symbol.clone(),
                            FetchedFundingBatch {
                                rows: rows.take().unwrap_or_default(),
                                available_at_ms,
                                checked_from_ms: Some(from),
                                checked_through_ms: Some(through),
                                emit_lifecycle: emit_lifecycle && index == 0,
                            },
                        ));
                    }
                }
            }
            Err(error) if error.is_lane_local_source_failure() => {
                failures.push((symbol, error.to_string()));
            }
            Err(error) => return Err(error),
        }
    }
    batches.sort_by(|left, right| {
        (&left.0, left.1.checked_from_ms).cmp(&(&right.0, right.1.checked_from_ms))
    });
    failures.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(FetchedFunding { batches, failures })
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

pub(super) async fn fetch_whale_batch(
    client: PublicHttpClient,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<(String, i64, i64)>,
) -> Result<FetchedWhales, WorkerError> {
    if jobs.len() > WHALE_FETCH_CHUNK_SIZE {
        return Err(WorkerError::state(format!(
            "whale fetch retained {} jobs; maximum chunk is {WHALE_FETCH_CHUNK_SIZE}",
            jobs.len()
        )));
    }
    let limiter = Arc::new(Semaphore::new(max_parallel));
    let mut tasks = JoinSet::new();
    let mut available_at_ms = 0;
    for (symbol, start_ms, end_ms) in jobs {
        available_at_ms = available_at_ms.max(end_ms);
        let limiter = Arc::clone(&limiter);
        let client = client.clone();
        tasks.spawn(async move {
            let result = match limiter.acquire_owned().await {
                Ok(_permit) => {
                    fetch_whale_symbol(client, page_limit, &symbol, start_ms, end_ms).await
                }
                Err(_) => Err(WorkerError::state(
                    "public request concurrency limiter closed",
                )),
            };
            (symbol, start_ms, end_ms, result)
        });
    }
    let mut rows = Vec::new();
    let mut coverage = Vec::new();
    while let Some(joined) = tasks.join_next().await {
        let (symbol, start_ms, end_ms, result) = joined
            .map_err(|error| WorkerError::state(format!("public fetch task failed: {error}")))?;
        match result {
            Ok((mut found, received)) => {
                coverage.extend(complete_whale_coverage(&symbol, start_ms, end_ms, &found)?);
                rows.append(&mut found);
                available_at_ms = available_at_ms.max(received);
            }
            Err(error) => return Err(error),
        }
    }
    rows.sort_by(|left, right| {
        let left_ts =
            wire_i64(Some(&left.day_end_ms), "Binance whale timestamp").unwrap_or(i64::MAX);
        let right_ts =
            wire_i64(Some(&right.day_end_ms), "Binance whale timestamp").unwrap_or(i64::MAX);
        (&left.symbol, left_ts).cmp(&(&right.symbol, right_ts))
    });
    Ok(FetchedWhales {
        available_at_ms,
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

pub(super) fn source_grid_slots(
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

pub(super) fn validate_source_grid_timestamp(
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

pub(super) fn validate_source_page_rows(
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

pub(super) fn whale_fetch_bounds(
    start_ms: i64,
    end_ms: i64,
) -> Result<(i64, i64, usize), WorkerError> {
    let query_start_ms = start_ms - start_ms.rem_euclid(FIVE_MIN_MS);
    let query_end_ms = end_ms - end_ms.rem_euclid(FIVE_MIN_MS);
    let retained_row_cap = source_grid_slots(start_ms, end_ms, FIVE_MIN_MS, true)?;
    Ok((query_start_ms, query_end_ms, retained_row_cap))
}

pub(super) async fn fetch_klines(
    client: PublicHttpClient,
    category: &str,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
) -> Result<(Vec<Vec<Value>>, i64), WorkerError> {
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
pub(super) async fn fetch_funding(
    client: PublicHttpClient,
    category: &str,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
    interval_hours: Option<i64>,
) -> Result<(Vec<BybitFundingWire>, i64), WorkerError> {
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

pub(super) fn bybit_result(payload: &Value) -> Result<&Value, WorkerError> {
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

pub(super) fn result_list(result: &Value) -> Result<&Vec<Value>, WorkerError> {
    result
        .get("list")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::network("Bybit result lacks list"))
}

pub(super) fn instrument_wire(value: &Value) -> Result<BybitInstrumentWire, WorkerError> {
    let symbol = value
        .get("symbol")
        .and_then(Value::as_str)
        .ok_or_else(|| WorkerError::network("Bybit instrument lacks symbol"))?
        .to_owned();
    Ok(BybitInstrumentWire {
        symbol,
        contract_type: text(value, "contractType"),
        symbol_type: text(value, "symbolType"),
        status: text(value, "status"),
        base_coin: text(value, "baseCoin"),
        quote_coin: text(value, "quoteCoin"),
        settle_coin: text(value, "settleCoin"),
        launch_time: value.get("launchTime").cloned(),
        delivery_time: value.get("deliveryTime").cloned(),
        price_filter: object_map(value, "priceFilter")?,
        lot_size_filter: object_map(value, "lotSizeFilter")?,
        funding_interval: value.get("fundingInterval").cloned(),
        is_pre_listing: value
            .get("isPreListing")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

pub(super) fn object_map(value: &Value, key: &str) -> Result<BTreeMap<String, Value>, WorkerError> {
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

pub(super) fn text(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(str::to_owned)
}

pub(super) fn wire_i64(value: Option<&Value>, label: &str) -> Result<i64, WorkerError> {
    match value {
        Some(Value::Number(number)) => number.as_i64(),
        Some(Value::String(text)) => text.parse().ok(),
        _ => None,
    }
    .ok_or_else(|| WorkerError::network(format!("{label} is not an integer")))
}
