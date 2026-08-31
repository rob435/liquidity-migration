use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;

use crate::config::SignalWorkerConfig;
use crate::http::{percent_encode, wall_ms, PublicHttpClient};
use crate::model::{
    BinanceWhaleWire, BybitFundingWire, BybitInstrumentWire, BybitTickerWire, WireEvent,
};
use crate::store::atomic_write;
use crate::worker::{DurableSignalWorker, WorkerError};
use crate::{DAY_MS, HOUR_MS, SCHEMA_VERSION};

const FIVE_MIN_MS: i64 = 300_000;

#[derive(Clone, Debug)]
pub struct LiveRunOptions {
    pub state_dir: PathBuf,
    pub spool_dir: PathBuf,
    pub heartbeat: PathBuf,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerHeartbeat {
    pub schema_version: u32,
    pub kind: String,
    pub status: String,
    pub pid: u32,
    pub updated_at_ms: i64,
    pub public_market_realm: String,
    pub public_bybit_host: String,
    pub credential_free: bool,
    pub signal_config_sha256: String,
    pub long_rule_sha256: String,
    pub long_feature_contract_sha256: String,
    pub carry_config_sha256: String,
    pub carry_feature_contract_sha256: String,
    pub operational_config_sha256: String,
    pub engine_config_sha256: String,
    pub universe_artifact_sha256: String,
    pub universe_file_sha256: String,
    pub source_generation: String,
    pub last_input_sequence: u64,
    pub long_output_sequence: u64,
    pub carry_output_sequence: u64,
    pub last_observed_ts_ms: i64,
    pub last_long_feature_ts_ms: Option<i64>,
    pub last_carry_decision_ts_ms: Option<i64>,
    pub last_carry_upcoming_ts_ms: Option<i64>,
}

pub struct LiveRunner {
    config: SignalWorkerConfig,
    durable: DurableSignalWorker,
    bybit: PublicHttpClient,
    binance: PublicHttpClient,
    heartbeat_path: PathBuf,
}

impl LiveRunner {
    pub fn new(
        config: SignalWorkerConfig,
        universe: crate::model::UniverseIdentity,
        options: LiveRunOptions,
    ) -> Result<Self, WorkerError> {
        let bybit_host = match config.live.public_market_realm.as_str() {
            "mainnet" => &config.sources.bybit_mainnet_host,
            _ => return Err(WorkerError::config("unsupported public market realm")),
        };
        let bybit = PublicHttpClient::new(
            bybit_host,
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
        )?;
        let binance = PublicHttpClient::new(
            &config.sources.binance_host,
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
        )?;
        let durable = DurableSignalWorker::open(
            config.clone(),
            universe,
            options.state_dir,
            options.spool_dir,
        )?;
        Ok(Self {
            config,
            durable,
            bybit,
            binance,
            heartbeat_path: options.heartbeat,
        })
    }

    pub async fn bootstrap(&mut self) -> Result<(), WorkerError> {
        self.refresh_instruments().await?;
        self.refresh_tickers().await?;
        let now = wall_ms()?;
        let end = now - now.rem_euclid(HOUR_MS);
        let carry_feature_hours = self
            .config
            .carry
            .vol_window_hours
            .saturating_add(self.config.carry.vol_return_lag_hours)
            .max(self.config.carry.turn_growth_lookback_hours + self.config.carry.adv_window_hours);
        let carry_replay_hours = i64::try_from(self.config.carry.minimum_replay_days)
            .unwrap_or(i64::MAX / 24)
            .saturating_mul(24)
            .saturating_add(carry_feature_hours)
            .saturating_add(48);
        let long_hours = i64::try_from(self.config.long.cold_start_lookback_days)
            .unwrap_or(i64::MAX / 24)
            .saturating_mul(24)
            .saturating_add(48);
        let start = end.saturating_sub(long_hours.max(carry_replay_hours) * HOUR_MS);
        self.refresh_klines(start, end).await?;
        self.refresh_funding(start, now).await?;
        let whale_days = i64::try_from(self.config.carry.whale_feed_days)
            .map_err(|_| WorkerError::config("whale feed days exceed i64"))?;
        self.refresh_whales(now.saturating_sub(whale_days * DAY_MS), now)
            .await?;
        self.refresh_tickers().await?;
        self.watermark().await?;
        self.write_heartbeat("ready")?;
        Ok(())
    }

    pub async fn run(mut self) -> Result<(), WorkerError> {
        self.write_heartbeat("starting")?;
        self.bootstrap().await?;
        let now = Instant::now();
        let mut ticker_due = now + Duration::from_millis(self.config.live.ticker_cadence_ms);
        let mut instrument_due =
            now + Duration::from_millis(self.config.live.instrument_cadence_ms);
        let mut funding_due = now + Duration::from_millis(self.config.live.funding_cadence_ms);
        let mut kline_due = now + Duration::from_millis(self.config.live.kline_cadence_ms);
        let mut whale_due = now + Duration::from_millis(self.config.live.whale_cadence_ms);
        loop {
            let next = [
                ticker_due,
                instrument_due,
                funding_due,
                kline_due,
                whale_due,
            ]
            .into_iter()
            .min()
            .expect("fixed non-empty deadline list");
            tokio::select! {
                _ = tokio::time::sleep_until(tokio::time::Instant::from_std(next)) => {}
                signal = shutdown_signal() => {
                    signal?;
                    self.write_heartbeat("stopped")?;
                    return Ok(());
                }
            }
            let instant = Instant::now();
            if instant >= ticker_due {
                self.refresh_tickers().await?;
                ticker_due = instant + Duration::from_millis(self.config.live.ticker_cadence_ms);
            }
            if instant >= instrument_due {
                self.refresh_instruments().await?;
                instrument_due =
                    instant + Duration::from_millis(self.config.live.instrument_cadence_ms);
            }
            if instant >= funding_due {
                let end = wall_ms()?;
                self.refresh_funding(end - 3 * DAY_MS, end).await?;
                funding_due = instant + Duration::from_millis(self.config.live.funding_cadence_ms);
            }
            if instant >= kline_due {
                let now_ms = wall_ms()?;
                let end = now_ms - now_ms.rem_euclid(HOUR_MS);
                self.repair_and_refresh_klines(end).await?;
                self.watermark().await?;
                kline_due = instant + Duration::from_millis(self.config.live.kline_cadence_ms);
            }
            if instant >= whale_due {
                let end = wall_ms()?;
                let whale_days = i64::try_from(self.config.carry.whale_feed_days)
                    .map_err(|_| WorkerError::config("whale feed days exceed i64"))?;
                self.refresh_whales(end - whale_days * DAY_MS, end).await?;
                whale_due = instant + Duration::from_millis(self.config.live.whale_cadence_ms);
            }
            self.write_heartbeat("ready")?;
        }
    }

    async fn refresh_instruments(&mut self) -> Result<(), WorkerError> {
        let observed = wall_ms()?;
        let mut cursor: Option<String> = None;
        let mut rows = Vec::new();
        let mut available = observed;
        for _ in 0..self.config.live.instrument_max_pages {
            let mut query = format!(
                "category={}&limit=1000",
                percent_encode(&self.config.sources.bybit_category)
            );
            if let Some(value) = &cursor {
                query.push_str("&cursor=");
                query.push_str(&percent_encode(value));
            }
            let (payload, received) = self
                .bybit
                .get("/v5/market/instruments-info", &query)
                .await?;
            available = available.max(received);
            let result = bybit_result(&payload)?;
            for value in result_list(result)? {
                rows.push(instrument_wire(value)?);
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
                return Err(WorkerError::network(
                    "Bybit instruments cursor did not advance",
                ));
            }
            cursor = next;
        }
        if cursor.is_some() {
            return Err(WorkerError::network(
                "Bybit instruments pagination exceeded configured page bound",
            ));
        }
        self.commit(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms: observed,
            available_at_ms: available,
            rows,
        })?;
        self.validate_candidate_instruments()
    }

    async fn refresh_tickers(&mut self) -> Result<(), WorkerError> {
        let observed = wall_ms()?;
        let query = format!(
            "category={}",
            percent_encode(&self.config.sources.bybit_category)
        );
        let (payload, available) = self.bybit.get("/v5/market/tickers", &query).await?;
        let rows = result_list(bybit_result(&payload)?)?
            .iter()
            .map(ticker_wire)
            .collect::<Result<Vec<_>, _>>()?;
        self.commit(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms: observed,
            available_at_ms: available,
            rows,
        })
    }

    async fn refresh_klines(&mut self, start: i64, end: i64) -> Result<(), WorkerError> {
        if end <= start {
            return Ok(());
        }
        let jobs = self
            .kline_symbols()
            .into_iter()
            .map(|symbol| (symbol, start, end))
            .collect();
        let fetched = self.fetch_kline_jobs(jobs).await?;
        let mut sequence = self.next_sequence()?;
        let mut events = Vec::with_capacity(fetched.len());
        for (symbol, (rows, available)) in fetched {
            events.push(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol,
                available_at_ms: available,
                rows,
            });
            sequence = sequence
                .checked_add(1)
                .ok_or_else(|| WorkerError::state("input sequence exhausted"))?;
        }
        self.commit_many(events)
    }

    async fn repair_and_refresh_klines(&mut self, end: i64) -> Result<(), WorkerError> {
        let symbols = self.kline_symbols();
        let state = self.durable.worker().state();
        let carry_hours = i64::try_from(self.config.carry.minimum_replay_days)
            .unwrap_or(i64::MAX / 24)
            .saturating_mul(24)
            .saturating_add(self.config.carry.vol_window_hours)
            .saturating_add(self.config.carry.vol_return_lag_hours)
            .saturating_add(48);
        let long_hours = i64::try_from(self.config.long.cold_start_lookback_days)
            .unwrap_or(i64::MAX / 24)
            .saturating_mul(24)
            .saturating_add(48);
        let required_start = end.saturating_sub(carry_hours.max(long_hours) * HOUR_MS);
        let jobs: Vec<(String, i64, i64)> = symbols
            .into_iter()
            .map(|symbol| {
                let rows = state.klines.get(&symbol);
                (symbol, kline_repair_start(rows, required_start, end), end)
            })
            .filter(|(_, start, end)| start < end)
            .collect();
        let fetched = self.fetch_kline_jobs(jobs).await?;
        let mut sequence = self.next_sequence()?;
        let mut events = Vec::with_capacity(fetched.len());
        for (symbol, (rows, available)) in fetched {
            events.push(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol,
                available_at_ms: available,
                rows,
            });
            sequence = sequence
                .checked_add(1)
                .ok_or_else(|| WorkerError::state("input sequence exhausted"))?;
        }
        self.commit_many(events)
    }

    async fn fetch_kline_jobs(
        &self,
        jobs: Vec<(String, i64, i64)>,
    ) -> Result<BTreeMap<String, (Vec<Vec<Value>>, i64)>, WorkerError> {
        let limiter = Arc::new(Semaphore::new(self.config.live.max_parallel_requests));
        let mut tasks = JoinSet::new();
        for (symbol, start, end) in jobs {
            let limiter = Arc::clone(&limiter);
            let client = self.bybit.clone();
            let category = self.config.sources.bybit_category.clone();
            let page_limit = self.config.live.kline_page_limit;
            tasks.spawn(async move {
                let _permit = limiter
                    .acquire_owned()
                    .await
                    .map_err(|_| WorkerError::state("public request concurrency limiter closed"))?;
                let (rows, available) =
                    fetch_klines(client, &category, page_limit, &symbol, start, end).await?;
                Ok::<_, WorkerError>((symbol, rows, available))
            });
        }
        let mut fetched = BTreeMap::new();
        while let Some(joined) = tasks.join_next().await {
            let (symbol, rows, available) = join_fetch(joined)?;
            if fetched.insert(symbol, (rows, available)).is_some() {
                return Err(WorkerError::state("duplicate kline fetch job"));
            }
        }
        Ok(fetched)
    }

    async fn refresh_funding(&mut self, start: i64, end: i64) -> Result<(), WorkerError> {
        let symbols = self.durable.worker().state().universe.carry_symbols.clone();
        let intervals = &self.durable.worker().state().instruments;
        let limiter = Arc::new(Semaphore::new(self.config.live.max_parallel_requests));
        let mut tasks = JoinSet::new();
        for symbol in symbols {
            let interval_hours = intervals
                .get(&symbol)
                .and_then(|row| row.funding_interval_min)
                .filter(|minutes| *minutes > 0 && *minutes % 60 == 0)
                .map(|minutes| minutes / 60);
            let limiter = Arc::clone(&limiter);
            let client = self.bybit.clone();
            let category = self.config.sources.bybit_category.clone();
            let page_limit = self.config.live.funding_page_limit;
            tasks.spawn(async move {
                let _permit = limiter
                    .acquire_owned()
                    .await
                    .map_err(|_| WorkerError::state("public request concurrency limiter closed"))?;
                let (rows, available) = fetch_funding(
                    client,
                    &category,
                    page_limit,
                    &symbol,
                    start,
                    end,
                    interval_hours,
                )
                .await?;
                Ok::<_, WorkerError>((symbol, rows, available))
            });
        }
        let mut fetched = BTreeMap::new();
        while let Some(joined) = tasks.join_next().await {
            let (symbol, rows, available) = join_fetch(joined)?;
            if fetched.insert(symbol, (rows, available)).is_some() {
                return Err(WorkerError::state("duplicate funding fetch job"));
            }
        }
        let mut sequence = self.next_sequence()?;
        let mut events = Vec::with_capacity(fetched.len());
        for (symbol, (rows, available)) in fetched {
            events.push(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol,
                available_at_ms: available,
                rows,
            });
            sequence = sequence
                .checked_add(1)
                .ok_or_else(|| WorkerError::state("input sequence exhausted"))?;
        }
        self.commit_many(events)
    }

    async fn refresh_whales(&mut self, start: i64, end: i64) -> Result<(), WorkerError> {
        let symbols = self.durable.worker().state().universe.carry_symbols.clone();
        let limiter = Arc::new(Semaphore::new(self.config.live.max_parallel_requests));
        let mut tasks = JoinSet::new();
        for symbol in symbols {
            let limiter = Arc::clone(&limiter);
            let client = self.binance.clone();
            let page_limit = self.config.live.whale_page_limit;
            tasks.spawn(async move {
                let result = match limiter.acquire_owned().await {
                    Ok(_permit) => {
                        fetch_whale_symbol(client, page_limit, &symbol, start, end).await
                    }
                    Err(_) => Err(WorkerError::state(
                        "public request concurrency limiter closed",
                    )),
                };
                (symbol, result)
            });
        }
        let mut fetched = BTreeMap::new();
        while let Some(joined) = tasks.join_next().await {
            let (symbol, result) = joined.map_err(|error| {
                WorkerError::state(format!("public fetch task failed: {error}"))
            })?;
            if fetched.insert(symbol, result).is_some() {
                return Err(WorkerError::state("duplicate whale fetch job"));
            }
        }
        let mut rows = Vec::new();
        let mut available = end;
        for (symbol, result) in fetched {
            match result {
                Ok((mut found, received)) => {
                    rows.append(&mut found);
                    available = available.max(received);
                }
                Err(error) => {
                    eprintln!("signal-worker: optional Binance whale {symbol}: {error}");
                }
            }
        }
        rows.sort_by(|left, right| {
            let left_ts =
                wire_i64(Some(&left.day_end_ms), "Binance whale timestamp").unwrap_or(i64::MAX);
            let right_ts =
                wire_i64(Some(&right.day_end_ms), "Binance whale timestamp").unwrap_or(i64::MAX);
            (&left.symbol, left_ts).cmp(&(&right.symbol, right_ts))
        });
        self.commit(WireEvent::BinanceWhaleBatch {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            available_at_ms: available,
            rows,
        })
    }

    async fn watermark(&mut self) -> Result<(), WorkerError> {
        self.commit(WireEvent::Watermark {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms: wall_ms()?,
        })
    }

    fn next_sequence(&self) -> Result<u64, WorkerError> {
        self.durable.worker().next_input_sequence()
    }

    fn commit(&mut self, event: WireEvent) -> Result<(), WorkerError> {
        self.durable.apply_and_commit(event).map(|_| ())
    }

    fn commit_many(&mut self, events: Vec<WireEvent>) -> Result<(), WorkerError> {
        self.durable.apply_many_and_commit(events).map(|_| ())
    }

    fn kline_symbols(&self) -> Vec<String> {
        let state = self.durable.worker().state();
        let mut symbols: BTreeSet<String> = state.universe.symbols.iter().cloned().collect();
        symbols.insert(self.config.long.regime_symbol.clone());
        symbols.insert("ETHUSDT".to_owned());
        symbols.into_iter().collect()
    }

    fn validate_candidate_instruments(&self) -> Result<(), WorkerError> {
        let state = self.durable.worker().state();
        for symbol in &state.universe.symbols {
            let row = state.instruments.get(symbol).ok_or_else(|| {
                WorkerError::network(format!("reviewed candidate {symbol} is absent on Bybit"))
            })?;
            if row.settle_coin.as_deref() != Some(self.config.sources.bybit_settle_coin.as_str())
                || row.status.as_deref() != Some("Trading")
            {
                return Err(WorkerError::network(format!(
                    "reviewed candidate {symbol} is not a trading USDT linear instrument"
                )));
            }
        }
        Ok(())
    }

    fn write_heartbeat(&self, status: &str) -> Result<(), WorkerError> {
        let state = self.durable.worker().state();
        let heartbeat = WorkerHeartbeat {
            schema_version: SCHEMA_VERSION,
            kind: "liquidity_migration_signal_worker_heartbeat".to_owned(),
            status: status.to_owned(),
            pid: std::process::id(),
            updated_at_ms: wall_ms()?,
            public_market_realm: self.config.live.public_market_realm.clone(),
            public_bybit_host: self.config.sources.bybit_mainnet_host.clone(),
            credential_free: true,
            signal_config_sha256: self.config.identity.signal_config_sha256.clone(),
            long_rule_sha256: self.config.identity.long_rule_sha256.clone(),
            long_feature_contract_sha256: self.config.identity.long_feature_contract_sha256.clone(),
            carry_config_sha256: self.config.identity.carry_rule_sha256.clone(),
            carry_feature_contract_sha256: self
                .config
                .identity
                .carry_feature_contract_sha256
                .clone(),
            operational_config_sha256: self.config.identity.operational_profile_sha256.clone(),
            engine_config_sha256: self.config.identity.engine_config_sha256.clone(),
            universe_artifact_sha256: state.universe.artifact_sha256.clone(),
            universe_file_sha256: state.universe.file_sha256.clone(),
            source_generation: state.source_generation.clone(),
            last_input_sequence: state.last_input_sequence,
            long_output_sequence: state.long_output_sequence,
            carry_output_sequence: state.carry_output_sequence,
            last_observed_ts_ms: state.last_observed_ts_ms,
            last_long_feature_ts_ms: state.last_long_feature_ts_ms,
            last_carry_decision_ts_ms: state.last_carry_decision_ts_ms,
            last_carry_upcoming_ts_ms: state.last_carry_upcoming_ts_ms,
        };
        let bytes = serde_json::to_vec(&heartbeat)
            .map_err(|error| WorkerError::json("encode worker heartbeat", error))?;
        atomic_write(&self.heartbeat_path, &bytes)
    }
}

fn join_fetch<T>(
    joined: Result<Result<T, WorkerError>, tokio::task::JoinError>,
) -> Result<T, WorkerError> {
    joined.map_err(|error| WorkerError::state(format!("public fetch task failed: {error}")))?
}

fn kline_repair_start(
    rows: Option<&BTreeMap<i64, crate::model::HourlyKline>>,
    required_start: i64,
    end: i64,
) -> i64 {
    let first_gap = (0..((end - required_start) / HOUR_MS)).find_map(|offset| {
        let ts = required_start + offset * HOUR_MS;
        (!rows.is_some_and(|values| values.contains_key(&ts))).then_some(ts)
    });
    let append_from = rows
        .and_then(|values| values.keys().next_back().copied())
        .map(|value| value.saturating_add(HOUR_MS))
        .unwrap_or(required_start);
    first_gap.unwrap_or(append_from).min(append_from)
}

async fn fetch_klines(
    client: PublicHttpClient,
    category: &str,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
) -> Result<(Vec<Vec<Value>>, i64), WorkerError> {
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
            if start <= ts && ts < end {
                match by_time.get(&ts) {
                    Some(existing) if existing != &row => {
                        return Err(WorkerError::network(
                            "Bybit kline pagination returned conflicting duplicate",
                        ));
                    }
                    Some(_) => {}
                    None => {
                        by_time.insert(ts, row);
                    }
                }
            }
        }
        cursor = window_end.saturating_add(HOUR_MS);
    }
    Ok((by_time.into_values().collect(), available))
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
) -> Result<(Vec<BybitFundingWire>, i64), WorkerError> {
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
        for value in result_list(bybit_result(&payload)?)? {
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
    Ok((by_time.into_values().collect(), available))
}

async fn fetch_whale_symbol(
    client: PublicHttpClient,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
) -> Result<(Vec<BinanceWhaleWire>, i64), WorkerError> {
    let page_limit = i64::try_from(page_limit)
        .map_err(|_| WorkerError::config("whale page limit exceeds i64"))?;
    let mut cursor = start - start.rem_euclid(FIVE_MIN_MS);
    let end = end - end.rem_euclid(FIVE_MIN_MS);
    let mut by_time = BTreeMap::<i64, Option<Value>>::new();
    let mut available = start;
    while cursor <= end {
        let window_end = cursor
            .saturating_add((page_limit - 1).max(0).saturating_mul(FIVE_MIN_MS))
            .min(end);
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
        for value in list {
            let timestamp = wire_i64(value.get("timestamp"), "Binance whale timestamp")?;
            let ratio = value.get("longShortRatio").cloned();
            if timestamp >= start && timestamp <= end {
                if let Some(existing) = by_time.insert(timestamp, ratio.clone()) {
                    if existing != ratio {
                        return Err(WorkerError::network(
                            "Binance whale pagination returned conflicting duplicate",
                        ));
                    }
                }
            }
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
    Ok((rows, available))
}

fn bybit_result(payload: &Value) -> Result<&Value, WorkerError> {
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

fn result_list(result: &Value) -> Result<&Vec<Value>, WorkerError> {
    result
        .get("list")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::network("Bybit result lacks list"))
}

fn instrument_wire(value: &Value) -> Result<BybitInstrumentWire, WorkerError> {
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

fn ticker_wire(value: &Value) -> Result<BybitTickerWire, WorkerError> {
    let symbol = value
        .get("symbol")
        .and_then(Value::as_str)
        .ok_or_else(|| WorkerError::network("Bybit ticker lacks symbol"))?
        .to_owned();
    Ok(BybitTickerWire {
        symbol,
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

fn object_map(value: &Value, key: &str) -> Result<BTreeMap<String, Value>, WorkerError> {
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

fn text(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(str::to_owned)
}

fn wire_i64(value: Option<&Value>, label: &str) -> Result<i64, WorkerError> {
    match value {
        Some(Value::Number(number)) => number.as_i64(),
        Some(Value::String(text)) => text.parse().ok(),
        _ => None,
    }
    .ok_or_else(|| WorkerError::network(format!("{label} is not an integer")))
}

async fn shutdown_signal() -> Result<(), WorkerError> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .map_err(|error| WorkerError::io("install SIGTERM handler", error))?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => result.map_err(|error| WorkerError::io("wait for SIGINT", error)),
            _ = terminate.recv() => Ok(()),
        }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c()
        .await
        .map_err(|error| WorkerError::io("wait for shutdown", error))
}

pub fn heartbeat_path_parent(path: &Path) -> Result<&Path, WorkerError> {
    path.parent()
        .ok_or_else(|| WorkerError::config("heartbeat path has no parent"))
}

#[cfg(test)]
mod tests {
    use super::kline_repair_start;
    use crate::model::HourlyKline;
    use crate::HOUR_MS;
    use std::collections::BTreeMap;

    fn row(open_ts_ms: i64) -> HourlyKline {
        HourlyKline {
            symbol: "BTCUSDT".into(),
            open_ts_ms,
            available_at_ms: open_ts_ms + HOUR_MS,
            open: 1.0,
            high: 1.0,
            low: 1.0,
            close: 1.0,
            volume_base: 1.0,
            turnover_quote: 1.0,
        }
    }

    #[test]
    fn kline_repair_starts_at_first_gap_then_appends_from_tail() {
        let start = 100 * HOUR_MS;
        let end = start + 5 * HOUR_MS;
        assert_eq!(kline_repair_start(None, start, end), start);

        let complete: BTreeMap<_, _> = (0..5)
            .map(|offset| {
                let ts = start + offset * HOUR_MS;
                (ts, row(ts))
            })
            .collect();
        assert_eq!(kline_repair_start(Some(&complete), start, end), end);

        let mut middle_gap = complete.clone();
        middle_gap.remove(&(start + 2 * HOUR_MS));
        assert_eq!(
            kline_repair_start(Some(&middle_gap), start, end),
            start + 2 * HOUR_MS
        );

        let mut missing_tail = complete;
        missing_tail.remove(&(end - HOUR_MS));
        assert_eq!(
            kline_repair_start(Some(&missing_tail), start, end),
            end - HOUR_MS
        );
    }
}
