//! Offline account-state scaling measurements.
//!
//! This module deliberately leaves the venue and disk out. The steady-state
//! soak keeps the execution-id set at a fixed size while advancing synthetic
//! time. The recovery sweep hands the real boot path an already-decoded venue
//! history and a counting log. Together they separate engine CPU and heap
//! growth from HTTP, JSON, disk, and the venue's own account-history query.

use std::cell::Cell;
use std::fmt;
use std::hint::black_box;
use std::path::PathBuf;
use std::rc::Rc;
use std::time::Instant;

use engine_types::{
    AccountIdentity, AccountView, AmendSpec, Feed, InstrumentRule, Intent, OrderAck, OrderKind,
    OrderRequest, OrderUpdate, PositionView, RiskKernel, RiskVerdict, Side, StopSpec, Strategy,
    StrategyId, Subscription, SymbolId, VenueCaps, VenueError, VenueExecution, VenueGateway,
    VenueOrder, Wal, WalError, WalRecord,
};
use serde::Serialize;

use crate::clock;
use crate::config::{EngineSection, LeverageAuthority};
use crate::engine::{Engine, EngineError};
use crate::execution_ids::{ExecutionIds, CAPACITY};

const SYMBOL: &str = "BTCUSDT";
const ORDER_ID: &str = "eng-account-soak-order";
const MAX_OPERATIONS: usize = 5_000_000;
const MAX_REPEATS: usize = 31;

#[derive(Clone, Debug)]
pub struct Options {
    /// New executions to fold through the steady-state dedup set.
    pub operations: usize,
    /// Execution ids retained at every measured window boundary.
    pub live_ids: usize,
    /// Operations measured under one clock sample.
    pub sample_ops: usize,
    /// Already-decoded venue-history sizes handed to the real boot path.
    pub history_rows: Vec<usize>,
    /// Measured cold boots per history size. One extra warm-up is discarded.
    pub repeats: usize,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            operations: 2_000_000,
            live_ids: 65_536,
            sample_ops: 4_096,
            history_rows: vec![0, 1_000, 10_000, 100_000],
            repeats: 3,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ResultSet {
    pub scope: &'static str,
    pub build: Build,
    pub steady_state: SteadyState,
    pub cold_recovery: Vec<RecoveryTier>,
}

impl ResultSet {
    pub fn table(&self) -> String {
        let mut out = format!(
            "account-state CPU/heap only; no network, JSON parse, or disk\n\
             build: engine-core {} on {}-{} ({})\n\
             steady state: {} new ids, {} retained, {} operations per timing window\n\
             phase       windows   p50 avg ns/op   p99 avg ns/op   max avg ns/op\n",
            self.build.engine_core,
            self.build.arch,
            self.build.os,
            if self.build.optimized {
                "optimized"
            } else {
                "debug"
            },
            self.steady_state.operations,
            self.steady_state.live_ids,
            self.steady_state.sample_ops,
        );
        for phase in &self.steady_state.phases {
            out.push_str(&format!(
                "{:<12} {:>7} {:>15} {:>15} {:>15}\n",
                phase.name,
                phase.windows,
                phase.p50_window_mean_ns_per_op,
                phase.p99_window_mean_ns_per_op,
                phase.max_window_mean_ns_per_op
            ));
        }
        out.push_str(
            "\ncold boot from already-decoded venue execution history; counting log\n\
             history rows  repeats          p50 ms          p99 ms       ns/row p50\n",
        );
        for tier in &self.cold_recovery {
            let per_row = tier
                .p50_ns_per_row
                .map(|value| value.to_string())
                .unwrap_or_else(|| "n/a".to_string());
            out.push_str(&format!(
                "{:>12} {:>8} {:>15.3} {:>15.3} {:>16}\n",
                tier.history_rows,
                tier.repeats,
                tier.p50_ns as f64 / 1_000_000.0,
                tier.p99_ns as f64 / 1_000_000.0,
                per_row,
            ));
        }
        out
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Build {
    pub engine_core: &'static str,
    pub os: &'static str,
    pub arch: &'static str,
    pub optimized: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct SteadyState {
    pub operations: usize,
    pub live_ids: usize,
    pub sample_ops: usize,
    pub final_ids: usize,
    pub phases: Vec<Phase>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Phase {
    pub name: &'static str,
    pub windows: usize,
    pub p50_window_mean_ns_per_op: u64,
    pub p99_window_mean_ns_per_op: u64,
    pub max_window_mean_ns_per_op: u64,
}

#[derive(Clone, Debug, Serialize)]
pub struct RecoveryTier {
    pub history_rows: usize,
    pub repeats: usize,
    pub recovered_rows: usize,
    pub p50_ns: u64,
    pub p99_ns: u64,
    pub max_ns: u64,
    pub p50_ns_per_row: Option<u64>,
}

#[derive(Debug)]
pub enum Error {
    Invalid(String),
    Engine(EngineError),
    Invariant(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(message) | Self::Invariant(message) => f.write_str(message),
            Self::Engine(error) => error.fmt(f),
        }
    }
}

impl std::error::Error for Error {}

impl From<EngineError> for Error {
    fn from(error: EngineError) -> Self {
        Self::Engine(error)
    }
}

pub async fn run(options: &Options) -> Result<ResultSet, Error> {
    validate(options)?;
    let steady_state = steady_state(options)?;
    let mut cold_recovery = Vec::with_capacity(options.history_rows.len());
    for &rows in &options.history_rows {
        cold_recovery.push(recovery_tier(rows, options.repeats).await?);
    }
    Ok(ResultSet {
        scope:
            "engine CPU/heap after venue response decode; network, JSON, and durable WAL excluded",
        build: Build {
            engine_core: env!("CARGO_PKG_VERSION"),
            os: std::env::consts::OS,
            arch: std::env::consts::ARCH,
            optimized: !cfg!(debug_assertions),
        },
        steady_state,
        cold_recovery,
    })
}

fn validate(options: &Options) -> Result<(), Error> {
    if options.operations == 0 || options.operations > MAX_OPERATIONS {
        return Err(Error::Invalid(format!(
            "operations must be between 1 and {MAX_OPERATIONS}"
        )));
    }
    if options.live_ids == 0 || options.live_ids > CAPACITY {
        return Err(Error::Invalid(format!(
            "live ids must be between 1 and {CAPACITY}"
        )));
    }
    if options.sample_ops == 0 || options.operations / options.sample_ops < 3 {
        return Err(Error::Invalid(
            "the workload must contain at least three complete timing windows".to_string(),
        ));
    }
    if options.repeats == 0 || options.repeats > MAX_REPEATS {
        return Err(Error::Invalid(format!(
            "repeats must be between 1 and {MAX_REPEATS}"
        )));
    }
    if options.history_rows.is_empty() {
        return Err(Error::Invalid(
            "at least one history size is required".to_string(),
        ));
    }
    if let Some(rows) = options.history_rows.iter().find(|&&rows| rows > CAPACITY) {
        return Err(Error::Invalid(format!(
            "history size {rows} exceeds the execution-id cap {CAPACITY}"
        )));
    }
    Ok(())
}

fn steady_state(options: &Options) -> Result<SteadyState, Error> {
    let retention_ms = i64::try_from(options.live_ids.saturating_sub(1)).map_err(|_| {
        Error::Invalid("live id count does not fit the benchmark clock".to_string())
    })?;
    let mut ids = ExecutionIds::with_limits(CAPACITY, retention_ms);
    for n in 0..options.live_ids {
        let id = format!("warm-{n:016x}");
        if !ids.can_insert(&id, n as i64).map_err(full)? {
            return Err(Error::Invariant(
                "warm-up id was already present".to_string(),
            ));
        }
        ids.insert(id, n as i64);
    }

    // Build ids before the clock starts. This measurement is the fixed-size
    // set's lookup, expiry, clone, and insert work, not integer formatting.
    let new_ids: Vec<String> = (0..options.operations)
        .map(|n| format!("soak-{n:016x}"))
        .collect();
    let mut next = new_ids.into_iter();
    let mut samples = Vec::new();
    let mut now_ms = options.live_ids as i64;
    let mut remaining = options.operations;
    while remaining > 0 {
        let count = remaining.min(options.sample_ops);
        let started = Instant::now();
        for _ in 0..count {
            let id = next.next().expect("the workload owns one id per operation");
            if !ids.can_insert(&id, now_ms).map_err(full)? {
                return Err(Error::Invariant(
                    "new soak id was already present".to_string(),
                ));
            }
            ids.insert(id.clone(), now_ms);
            if ids.can_insert(&id, now_ms).map_err(full)? {
                return Err(Error::Invariant(
                    "inserted soak id was not remembered".to_string(),
                ));
            }
            now_ms += 1;
        }
        black_box(ids.len());
        let elapsed = nanos(started.elapsed().as_nanos());
        samples.push(elapsed / count as u64);
        remaining -= count;
    }
    if ids.len() != options.live_ids {
        return Err(Error::Invariant(format!(
            "fixed-size soak ended with {} ids instead of {}",
            ids.len(),
            options.live_ids
        )));
    }

    let one_third = samples.len() / 3;
    let two_thirds = one_third * 2;
    let phases = vec![
        phase("early", &samples[..one_third]),
        phase("middle", &samples[one_third..two_thirds]),
        phase("late", &samples[two_thirds..]),
    ];
    Ok(SteadyState {
        operations: options.operations,
        live_ids: options.live_ids,
        sample_ops: options.sample_ops,
        final_ids: ids.len(),
        phases,
    })
}

fn phase(name: &'static str, values: &[u64]) -> Phase {
    let sorted = sorted(values);
    Phase {
        name,
        windows: sorted.len(),
        p50_window_mean_ns_per_op: percentile(&sorted, 50),
        p99_window_mean_ns_per_op: percentile(&sorted, 99),
        max_window_mean_ns_per_op: *sorted.last().unwrap_or(&0),
    }
}

async fn recovery_tier(history_rows: usize, repeats: usize) -> Result<RecoveryTier, Error> {
    // The first boot pays allocator and code-page warm-up. It is intentionally
    // visible work but not a comparable sample, so every tier discards one.
    let _ = recovery_once(history_rows).await?;
    let mut samples = Vec::with_capacity(repeats);
    for _ in 0..repeats {
        samples.push(recovery_once(history_rows).await?);
    }
    let sorted = sorted(&samples);
    let p50_ns = percentile(&sorted, 50);
    Ok(RecoveryTier {
        history_rows,
        repeats,
        recovered_rows: history_rows,
        p50_ns,
        p99_ns: percentile(&sorted, 99),
        max_ns: *sorted.last().unwrap_or(&0),
        p50_ns_per_row: if history_rows == 0 {
            None
        } else {
            Some(p50_ns / history_rows as u64)
        },
    })
}

async fn recovery_once(history_rows: usize) -> Result<u64, Error> {
    let wall_ms = clock::wall_ms();
    let (replayed, account) = prior_state(history_rows, wall_ms);
    let executions = history(history_rows, wall_ms);
    let recovered = Rc::new(Cell::new(0usize));
    let wal = CountingWal {
        recovered: Rc::clone(&recovered),
        sequence: 0,
    };
    let venue = HistoryVenue {
        executions,
        account,
    };
    let settings = EngineSection {
        wal_path: PathBuf::from("account-state-soak.wal"),
        venue: "offline-soak".to_string(),
        group_flush_ms: 250,
        wal_rotate_mb: 0,
        account_view_max_age_ms: 60_000,
        max_quote_age_ms: 60_000,
        leverage_authority: LeverageAuthority::Shared,
        target_book_path: None,
        heartbeat_path: None,
        trades_path: None,
    };

    let started = Instant::now();
    let engine = Engine::boot(
        &settings,
        "account-state-soak",
        wal,
        PermitAll,
        venue,
        vec![Box::new(SoakStrategy)],
        &replayed,
    )
    .await?;
    black_box(&engine);
    let elapsed = nanos(started.elapsed().as_nanos());
    if recovered.get() != history_rows {
        return Err(Error::Invariant(format!(
            "boot recovered {} of {history_rows} synthetic executions",
            recovered.get()
        )));
    }
    Ok(elapsed)
}

fn prior_state(history_rows: usize, now_ms: i64) -> (Vec<WalRecord>, AccountView) {
    let mut replayed = vec![
        WalRecord::Boot {
            version: "account-state-soak".to_string(),
            config_sha256: "account-state-soak".to_string(),
            wall_ts_ms: now_ms - 1_000,
        },
        WalRecord::Names {
            strategies: vec!["account-state-soak".to_string()],
            symbols: vec![SYMBOL.to_string()],
        },
    ];
    let positions = if history_rows == 0 {
        Vec::new()
    } else {
        replayed.push(WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: ORDER_ID.to_string(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: history_rows as f64,
                kind: OrderKind::Market,
                stop: Some(StopSpec { trigger_px: 99.0 }),
                reduce_only: false,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 100.0,
        });
        vec![PositionView {
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: history_rows as f64,
            entry_px: 100.0,
            stop_attached: true,
            stop_px: 99.0,
            leverage: None,
        }]
    };
    (
        replayed,
        AccountView {
            equity_usdt: 1_000_000.0,
            available_usdt: 1_000_000.0,
            positions,
            observed_ns: clock::now_ns(),
        },
    )
}

fn history(rows: usize, now_ms: i64) -> Vec<VenueExecution> {
    (0..rows)
        .rev()
        .map(|n| VenueExecution {
            exec_id: format!("venue-exec-{n:016x}"),
            client_order_id: ORDER_ID.to_string(),
            symbol: SYMBOL.to_string(),
            side: Side::Buy,
            qty: 1.0,
            px: 100.0,
            fee: Some(0.02),
            is_maker: true,
            venue_ts_ms: now_ms - 500 + (n % 100) as i64,
        })
        .collect()
}

fn sorted(values: &[u64]) -> Vec<u64> {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    sorted
}

fn percentile(sorted: &[u64], percent: usize) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let rank = ((sorted.len() - 1) * percent).div_ceil(100);
    sorted[rank]
}

fn nanos(value: u128) -> u64 {
    value.min(u64::MAX as u128) as u64
}

fn full(error: crate::execution_ids::Full) -> Error {
    Error::Invariant(error.to_string())
}

struct CountingWal {
    recovered: Rc<Cell<usize>>,
    sequence: u64,
}

impl Wal for CountingWal {
    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
        self.sequence += 1;
        if matches!(record, WalRecord::RecoveredFill { .. }) {
            self.recovered.set(self.recovered.get() + 1);
        }
        Ok(self.sequence)
    }

    fn barrier(&mut self) -> Result<(), WalError> {
        Ok(())
    }

    fn flush(&mut self) -> Result<(), WalError> {
        Ok(())
    }
}

struct SoakStrategy;

impl Strategy for SoakStrategy {
    fn name(&self) -> &str {
        "account-state-soak"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: SYMBOL.to_string(),
            feed: Feed::Quote,
        }]
    }
}

struct PermitAll;

impl RiskKernel for PermitAll {
    fn assess(&mut self, intent: &Intent, _account: &AccountView) -> RiskVerdict {
        RiskVerdict::Allow { qty: intent.qty }
    }

    fn on_update(&mut self, _update: &OrderUpdate) {}
}

struct HistoryVenue {
    executions: Vec<VenueExecution>,
    account: AccountView,
}

#[engine_types::async_trait]
impl VenueGateway for HistoryVenue {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            native_position_stop: true,
            amend_in_place: true,
            set_leverage: false,
            close_position_below_minimum: false,
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        Ok(AccountIdentity {
            venue: "offline".to_string(),
            user_id: "account-state-soak".to_string(),
            realm: "offline".to_string(),
        })
    }

    async fn send_order(&mut self, _req: &OrderRequest) -> Result<OrderAck, VenueError> {
        Err(VenueError::BadRequest(
            "offline benchmark never sends orders".to_string(),
        ))
    }

    async fn cancel_order(
        &mut self,
        _symbol: SymbolId,
        _client_order_id: &str,
    ) -> Result<(), VenueError> {
        Err(VenueError::BadRequest(
            "offline benchmark never cancels orders".to_string(),
        ))
    }

    async fn amend_order(
        &mut self,
        _symbol: SymbolId,
        _client_order_id: &str,
        _spec: AmendSpec,
    ) -> Result<(), VenueError> {
        Err(VenueError::BadRequest(
            "offline benchmark never amends orders".to_string(),
        ))
    }

    async fn set_stop(&mut self, _symbol: SymbolId, _trigger_px: f64) -> Result<(), VenueError> {
        Err(VenueError::BadRequest(
            "offline benchmark needs no stop repair".to_string(),
        ))
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        Ok(self.account.clone())
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(String, InstrumentRule)>, VenueError> {
        Ok(vec![(
            SYMBOL.to_string(),
            InstrumentRule {
                tick_size: 0.5,
                qty_step: 0.001,
                min_qty: 0.001,
                min_notional: 5.0,
            },
        )])
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        Ok(Vec::new())
    }

    async fn executions(
        &mut self,
        _start_ms: i64,
        _end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        Ok(std::mem::take(&mut self.executions))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn fixed_state_soak_and_recovery_sweep_cover_the_declared_work() {
        let result = run(&Options {
            operations: 96,
            live_ids: 8,
            sample_ops: 8,
            history_rows: vec![0, 16],
            repeats: 2,
        })
        .await
        .unwrap();

        assert_eq!(result.steady_state.final_ids, 8);
        assert_eq!(result.steady_state.phases.len(), 3);
        assert!(result
            .steady_state
            .phases
            .iter()
            .all(|phase| phase.windows == 4));
        assert_eq!(
            result
                .cold_recovery
                .iter()
                .map(|tier| (tier.history_rows, tier.recovered_rows, tier.repeats))
                .collect::<Vec<_>>(),
            vec![(0, 0, 2), (16, 16, 2)]
        );
        assert_eq!(result.cold_recovery[0].p50_ns_per_row, None);
        assert!(result.cold_recovery[1].p50_ns_per_row.is_some());
    }

    #[tokio::test]
    async fn workload_bounds_are_rejected_before_measurement() {
        let result = run(&Options {
            operations: 2,
            live_ids: 1,
            sample_ops: 1,
            history_rows: vec![0],
            repeats: 1,
        })
        .await;
        assert!(matches!(result, Err(Error::Invalid(message)) if message.contains("three")));
    }

    #[test]
    fn a_sustained_workload_does_not_grow_the_live_set() {
        let result = steady_state(&Options {
            operations: 100_000,
            live_ids: 1_024,
            sample_ops: 1_000,
            history_rows: vec![0],
            repeats: 1,
        })
        .unwrap();

        assert_eq!(result.final_ids, 1_024);
        assert_eq!(
            result
                .phases
                .iter()
                .map(|phase| phase.windows)
                .sum::<usize>(),
            100
        );
    }
}
