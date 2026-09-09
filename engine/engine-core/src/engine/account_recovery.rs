use std::collections::HashMap;

use super::*;
use engine_types::orders::AccountRecoveryClient;
use std::sync::Arc;

const RETRY_NS: u64 = 1_000_000_000;
pub(super) const HISTORY_ROWS_PER_TURN: usize = 32;

pub(super) fn history_account_matches(
    account: &AccountView,
    logged: &reconcile::PhysicalExposure,
) -> Result<bool, EngineError> {
    let mut actual = std::collections::BTreeMap::new();
    for position in &account.positions {
        let quantity = position
            .quantity()
            .map_err(|error| EngineError::State(error.to_string()))?;
        let signed = if position.side == Side::Buy {
            quantity
        } else {
            -quantity
        };
        let prior = actual
            .entry(position.symbol)
            .or_insert_with(engine_types::numeric::Exact::zero);
        *prior = &*prior + &signed;
    }
    actual.retain(|_, quantity| !quantity.is_zero());
    Ok(actual.len()
        == logged
            .values()
            .filter(|quantity| !quantity.is_zero())
            .count()
        && actual
            .iter()
            .all(|(symbol, quantity)| logged.get(symbol) == Some(quantity)))
}

#[derive(Clone, Copy)]
pub(super) struct Query {
    pub started_ns: u64,
    pub generation: u64,
    pub history: Option<(i64, i64)>,
}

pub(super) struct ReadResult {
    pub account: Result<AccountView, VenueError>,
    pub history: Option<Result<engine_types::ExecutionHistory, VenueError>>,
}

pub(super) struct HistoryBatch {
    pub query: Query,
    pub account: Result<AccountView, VenueError>,
    pub rows: engine_types::ExecutionHistory,
    pub resume: Option<engine_types::VenueExecution>,
    pub untrusted: bool,
    pub delivered: HashMap<(String, i64, u64), usize>,
    pub recovered: usize,
    pub foreign: Vec<String>,
}

pub(super) enum Completion {
    Read(ReadResult),
    Durable(Result<(), WalError>),
}

pub(super) enum Phase {
    Idle,
    Reading {
        query: Query,
        task: tokio::task::JoinHandle<()>,
    },
    Applying(Box<HistoryBatch>),
    Publishing {
        query: Query,
        account: Result<AccountView, VenueError>,
        through_ms: Option<i64>,
    },
}

pub(super) struct Recovery {
    client: Option<Arc<dyn AccountRecoveryClient>>,
    pub phase: Phase,
    pub completed: tokio::sync::mpsc::Receiver<Completion>,
    send: tokio::sync::mpsc::Sender<Completion>,
    pub history_requested: bool,
    pub generation: u64,
    pub connected: bool,
    pub history_generation: Option<u64>,
    pub retry_after_ns: u64,
}

impl Recovery {
    pub fn new(client: Option<Box<dyn AccountRecoveryClient>>) -> Self {
        let (send, completed) = tokio::sync::mpsc::channel(1);
        Self {
            client: client.map(Arc::from),
            phase: Phase::Idle,
            completed,
            send,
            history_requested: false,
            generation: 0,
            connected: true,
            history_generation: Some(0),
            retry_after_ns: 0,
        }
    }
    pub fn install_catalog(
        &self,
        catalog: &engine_types::orders::InstrumentCatalog,
    ) -> Result<(), VenueError> {
        self.client
            .as_ref()
            .map_or(Ok(()), |client| client.install_instrument_catalog(catalog))
    }
    pub fn waiting(&self) -> bool {
        matches!(self.phase, Phase::Reading { .. } | Phase::Publishing { .. })
    }
    pub fn applying(&self) -> bool {
        matches!(self.phase, Phase::Applying(_))
    }
    pub fn uncommitted(&self) -> bool {
        matches!(self.phase, Phase::Applying(_) | Phase::Publishing { .. })
    }
    pub fn disconnected(&mut self) {
        self.generation = self
            .generation
            .checked_add(1)
            .expect("private stream generation exhausted");
        self.connected = false;
        self.history_generation = None;
        self.history_requested = true;
    }
    pub fn reconnected(&mut self) {
        self.disconnected();
        self.connected = true;
        self.retry_after_ns = 0;
    }
    fn start(&mut self, query: Query, symbols: Vec<String>) {
        let client = self.client.clone();
        let send = self.send.clone();
        let task = tokio::spawn(async move {
            let result = if let Some(client) = client {
                let account = async {
                    match tokio::time::timeout(
                        MUTATION_DRAIN_TIMEOUT,
                        client.account_view(&symbols),
                    )
                    .await
                    {
                        Ok(result) => result,
                        Err(_) => Err(VenueError::Transport("account recovery timed out".into())),
                    }
                };
                let history = async {
                    if let Some((since, through)) = query.history {
                        if since < through.saturating_sub(RECOVERY_REACH_MS) {
                            return Some(Err(VenueError::BadRequest(
                                "execution history boundary exceeds venue recovery reach".into(),
                            )));
                        }
                        let progress = client.execution_history_progress();
                        let sample = || {
                            progress
                                .as_ref()
                                .map(|progress| progress.load(std::sync::atomic::Ordering::Relaxed))
                        };
                        let mut observed = sample();
                        let read = client.executions(&symbols, since, through);
                        tokio::pin!(read);
                        loop {
                            match tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, &mut read).await {
                                Ok(result) => break Some(result),
                                Err(_) if sample() != observed => {
                                    observed = sample();
                                }
                                Err(_) => {
                                    break Some(Err(VenueError::Transport(
                                        "execution history recovery stopped making progress".into(),
                                    )))
                                }
                            }
                        }
                    } else {
                        None
                    }
                };
                let (account, history) = tokio::join!(account, history);
                ReadResult { account, history }
            } else {
                ReadResult {
                    account: Err(VenueError::Unsupported(
                        "independent account recovery client".into(),
                    )),
                    history: query.history.map(|_| {
                        Err(VenueError::Unsupported(
                            "independent execution history client".into(),
                        ))
                    }),
                }
            };
            let _ = send.send(Completion::Read(result)).await;
        });
        self.phase = Phase::Reading { query, task };
    }
    pub fn stop_read(&mut self) {
        if matches!(self.phase, Phase::Reading { .. }) {
            if let Phase::Reading { task, .. } = std::mem::replace(&mut self.phase, Phase::Idle) {
                task.abort();
            }
            while self.completed.try_recv().is_ok() {}
        }
    }
}
impl Drop for Recovery {
    fn drop(&mut self) {
        self.stop_read();
    }
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) async fn service_account_recovery(&mut self) -> Result<(), EngineError> {
        if let Ok(completion) = self.recovery.completed.try_recv() {
            self.on_recovery_completion(completion).await?;
        }
        if self.order_lineage.waiting() {
            return Ok(());
        }
        if self.recovery.applying() {
            let Phase::Applying(batch) = std::mem::replace(&mut self.recovery.phase, Phase::Idle)
            else {
                unreachable!()
            };
            self.apply_history_batch(*batch)?;
        }
        self.launch_account_recovery(self.account_refresh_due(clock::now_ns()));
        Ok(())
    }

    pub(super) fn launch_account_recovery(&mut self, account_due: bool) {
        if !matches!(self.recovery.phase, Phase::Idle)
            || clock::now_ns() < self.recovery.retry_after_ns
            || !(account_due || self.recovery.history_requested)
        {
            return;
        }
        let now_ms = clock::wall_ms();
        let history = self.recovery.history_requested.then(|| {
            (
                self.recovered_until_ms.saturating_sub(RECOVERY_PAD_MS),
                now_ms,
            )
        });
        let query = Query {
            started_ns: clock::now_ns(),
            generation: self.recovery.generation,
            history,
        };
        let symbols = (0..self.books.market.table.len())
            .map(|index| {
                self.books
                    .market
                    .table
                    .name(SymbolId(index as u16))
                    .to_string()
            })
            .collect();
        self.recovery.start(query, symbols);
    }

    pub(super) async fn on_recovery_completion(
        &mut self,
        completion: Completion,
    ) -> Result<(), EngineError> {
        match (
            std::mem::replace(&mut self.recovery.phase, Phase::Idle),
            completion,
        ) {
            (Phase::Reading { query, .. }, Completion::Read(result)) => match result.history {
                Some(Ok(rows)) => {
                    self.recovery.phase = Phase::Applying(Box::new(HistoryBatch {
                        query,
                        account: result.account,
                        rows,
                        resume: None,
                        untrusted: false,
                        delivered: HashMap::new(),
                        recovered: 0,
                        foreign: Vec::new(),
                    }));
                }
                Some(Err(error)) if matches!(&error, VenueError::Transport(_)) => {
                    self.clear_private_stream_ready();
                    self.recovery.history_requested = true;
                    self.recovery.history_generation = None;
                    tracing::warn!(%error, "execution history recovery retained for retry");
                    // The account read cannot confirm drift against a history request that failed.
                    self.publish_history(
                        Query {
                            history: None,
                            ..query
                        },
                        result.account,
                        None,
                    )?;
                }
                Some(Err(error)) => {
                    self.latch_closed();
                    record_latch(
                        &mut self.wal,
                        clock::wall_ms(),
                        vec![format!(
                            "execution history is unavailable during recovery: {error}"
                        )],
                    )?;
                    self.publish_history(query, result.account, None)?;
                }
                None => self.adopt_recovery_account(query, result.account).await?,
            },
            (
                Phase::Publishing {
                    query,
                    account,
                    through_ms,
                },
                Completion::Durable(result),
            ) => {
                result?;
                if let Some(through_ms) = through_ms {
                    self.recovered_until_ms = through_ms;
                    self.next_history_checkpoint_ms = query
                        .history
                        .expect("published history interval")
                        .1
                        .saturating_add(HISTORY_CHECKPOINT_INTERVAL_MS);
                    if query.generation == self.recovery.generation {
                        self.recovery.history_requested = false;
                        self.recovery.history_generation = Some(query.generation);
                    }
                } else {
                    self.recovery.retry_after_ns = clock::now_ns().saturating_add(RETRY_NS);
                }
                self.adopt_recovery_account(query, account).await?;
            }
            (phase, _) => {
                self.recovery.phase = phase;
                return Err(EngineError::State(
                    "recovery completion has no matching phase".into(),
                ));
            }
        }
        Ok(())
    }

    pub(super) fn publish_history(
        &mut self,
        query: Query,
        account: Result<AccountView, VenueError>,
        through_ms: Option<i64>,
    ) -> Result<(), EngineError> {
        let barrier = self.wal.barrier_begin()?;
        let send = self.recovery.send.clone();
        self.recovery.phase = Phase::Publishing {
            query,
            account,
            through_ms,
        };
        // A settled barrier completes on this thread. A thread hop for a
        // barrier that has nothing left to wait for is a race against the
        // simulator's idle clock, and two runs of one seed then disagree.
        if barrier.outstanding() {
            tokio::task::spawn_blocking(move || {
                let _ = send.blocking_send(Completion::Durable(barrier.wait()));
            });
        } else if let Err(unsent) = send.try_send(Completion::Durable(barrier.wait())) {
            let completion = unsent.into_inner();
            tokio::task::spawn_blocking(move || {
                let _ = send.blocking_send(completion);
            });
        }
        Ok(())
    }

    async fn adopt_recovery_account(
        &mut self,
        query: Query,
        account: Result<AccountView, VenueError>,
    ) -> Result<(), EngineError> {
        match account {
            Ok(view) => {
                let frontier = self
                    .portfolio_physical_after
                    .values()
                    .copied()
                    .max()
                    .unwrap_or(0);
                if query.generation != self.recovery.generation || query.started_ns <= frontier {
                    self.request_account_refresh_after(frontier.max(clock::now_ns()));
                    return Ok(());
                }
                if self.may_open && !history_account_matches(&view, &self.logged_exposure)? {
                    if query.history.is_none() {
                        self.recovery.history_requested = true;
                        self.recovery.history_generation = None;
                        self.clear_private_stream_ready();
                    } else {
                        self.latch_closed();
                        record_latch(
                            &mut self.wal,
                            clock::wall_ms(),
                            vec![
                                "venue position drift remains after execution-history recovery"
                                    .into(),
                            ],
                        )?;
                    }
                }
                self.adopt_view(view);
                self.account_refresh_started_ns = query.started_ns;
                if self
                    .account_refresh_requested_after
                    .is_some_and(|frontier| query.started_ns > frontier)
                {
                    self.account_refresh_requested_after = None;
                }
                if self.recovery.connected
                    && self.recovery.history_generation == Some(self.recovery.generation)
                {
                    self.restore_private_stream_ready();
                }
                self.enforce_position_stop_intent().await?;
                self.queue_halted_entry_cancels()?;
            }
            Err(error) => {
                self.recovery.retry_after_ns = clock::now_ns().saturating_add(RETRY_NS);
                tracing::warn!(%error, "account recovery retained for retry");
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    #[tokio::test]
    async fn routine_account_refresh_detects_position_drift_before_the_daily_checkpoint() {
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        let mut account = engine.account().clone();
        account.positions.push(engine_types::PositionView {
            exact_amounts: None,
            exact_stop_px: None,
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            entry_px: 100.0,
            stop_attached: true,
            stop_px: 90.0,
            leverage: Some(5.0),
        });
        let query = Query {
            started_ns: clock::now_ns().saturating_add(1),
            generation: engine.recovery.generation,
            history: None,
        };
        engine
            .adopt_recovery_account(query, Ok(account))
            .await
            .unwrap();
        assert!(
            engine.recovery.history_requested,
            "ordinary account refresh missed position drift"
        );
        assert!(
            !engine.private_stream_ready,
            "entries cannot trust unexplained physical exposure"
        );
    }

    struct ReadClient {
        delay_ms: AtomicU64,
        fail_history: bool,
        started: tokio::sync::Notify,
    }
    #[derive(Clone)]
    struct SharedReadClient(Arc<ReadClient>);
    #[engine_types::async_trait]
    impl AccountRecoveryClient for SharedReadClient {
        async fn account_view(&self, _: &[String]) -> Result<AccountView, VenueError> {
            let observed_ns = clock::now_ns();
            self.0.started.notify_one();
            tokio::time::sleep(Duration::from_millis(
                self.0.delay_ms.load(Ordering::Relaxed),
            ))
            .await;
            Ok(AccountView {
                exact_amounts: None,
                equity_usdt: 1234.0,
                available_usdt: 1000.0,
                positions: Vec::new(),
                observed_ns,
            })
        }
        async fn executions(
            &self,
            _: &[String],
            _: i64,
            _: i64,
        ) -> Result<engine_types::ExecutionHistory, VenueError> {
            tokio::time::sleep(Duration::from_millis(
                self.0.delay_ms.load(Ordering::Relaxed),
            ))
            .await;
            if self.0.fail_history {
                Err(VenueError::Transport("history disconnected".into()))
            } else {
                Ok(engine_types::ExecutionHistory::default())
            }
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_query_started_before_a_physical_update_cannot_overwrite_the_account() {
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        let client = Arc::new(ReadClient {
            delay_ms: AtomicU64::new(30),
            fail_history: false,
            started: Default::default(),
        });
        engine.recovery = Recovery::new(Some(Box::new(SharedReadClient(client.clone()))));
        let original = engine.account().clone();
        engine.launch_account_recovery(true);
        client.started.notified().await;
        let frontier = clock::now_ns();
        engine
            .portfolio_physical_after
            .insert(SymbolId(0), frontier);
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();
        assert_eq!(engine.account().equity_usdt, original.equity_usdt);
        assert_eq!(engine.account().observed_ns, original.observed_ns);
        assert!(engine
            .account_refresh_requested_after
            .is_some_and(|required| required >= frontier));
        client.delay_ms.store(0, Ordering::Relaxed);
        engine.service_account_recovery().await.unwrap();
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();
        assert_eq!(engine.account().equity_usdt, 1234.0);
        assert!(engine.account_refresh_started_ns > frontier);
        assert!(engine.account_refresh_requested_after.is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn transient_history_failure_retries_without_persisting_an_opening_latch() {
        for account_drift in [false, true] {
            let (mut engine, records) = crate::tests::callback_test_fixture(Vec::new()).await;
            assert!(engine.may_open);
            engine.recovery =
                Recovery::new(Some(Box::new(SharedReadClient(Arc::new(ReadClient {
                    delay_ms: AtomicU64::new(0),
                    fail_history: true,
                    started: Default::default(),
                })))));
            let before = engine.recovered_until_ms;
            let prior_records = records.lock().unwrap().len();
            engine.recovery.reconnected();
            engine.launch_account_recovery(true);
            let mut completion = engine.recovery.completed.recv().await.unwrap();
            if account_drift {
                let Completion::Read(result) = &mut completion else {
                    unreachable!()
                };
                result
                    .account
                    .as_mut()
                    .unwrap()
                    .positions
                    .push(engine_types::PositionView {
                        exact_amounts: None,
                        exact_stop_px: None,
                        symbol: SymbolId(0),
                        side: Side::Buy,
                        qty: 1.0,
                        entry_px: 100.0,
                        stop_attached: true,
                        stop_px: 90.0,
                        leverage: Some(5.0),
                    });
            }
            engine.on_recovery_completion(completion).await.unwrap();
            let completion = engine.recovery.completed.recv().await.unwrap();
            engine.on_recovery_completion(completion).await.unwrap();
            assert!(
                engine.may_open,
                "a transient history timeout became a permanent latch"
            );
            assert!(!engine.private_stream_ready && engine.recovery.history_requested);
            assert_eq!(engine.recovered_until_ms, before);
            assert!(engine.recovery.retry_after_ns > clock::now_ns());
            engine.launch_account_recovery(true);
            assert!(matches!(engine.recovery.phase, Phase::Idle));
            assert!(!records.lock().unwrap()[prior_records..]
                .iter()
                .any(|r| matches!(
                    r,
                    WalRecord::Reconciled {
                        may_open: false,
                        ..
                    }
                )));
            engine.recovery.client = Some(Arc::new(SharedReadClient(Arc::new(ReadClient {
                delay_ms: AtomicU64::new(0),
                fail_history: false,
                started: Default::default(),
            }))));
            engine.recovery.retry_after_ns = 0;
            engine.renew_execution_history().await.unwrap();
            assert!(engine.may_open && engine.private_stream_ready);
            assert!(!engine.recovery.history_requested);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn failed_history_preserves_a_prior_latch_and_private_updates_until_retry() {
        let (mut engine, records) = crate::tests::callback_test_fixture(Vec::new()).await;
        engine.may_open = false;
        engine
            .wal
            .append(&WalRecord::Reconciled {
                wall_ts_ms: clock::wall_ms(),
                findings: vec!["unexplained foreign exposure".into()],
                may_open: false,
            })
            .unwrap();
        engine.recovery = Recovery::new(Some(Box::new(SharedReadClient(Arc::new(ReadClient {
            delay_ms: AtomicU64::new(0),
            fail_history: true,
            started: Default::default(),
        })))));
        let before = engine.recovered_until_ms;
        engine.recovery.reconnected();
        engine.private_stream_ready = false;
        engine.launch_account_recovery(true);
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();
        assert!(matches!(
            engine.recovery.phase,
            Phase::Publishing {
                through_ms: None,
                ..
            }
        ));
        engine
            .take_update(OrderUpdate::Ack(engine_types::OrderAck {
                client_order_id: "private-during-history-failure".into(),
                venue_order_id: "actual".into(),
                sent_ns: 1,
                ack_ns: clock::now_ns(),
            }))
            .await
            .unwrap();
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();
        assert_eq!(engine.recovered_until_ms, before);
        assert!(
            engine.recovery.history_requested && !engine.private_stream_ready && !engine.may_open
        );
        assert!(engine.recovery.retry_after_ns > clock::now_ns());
        assert!(records.lock().unwrap().iter().any(|row| matches!(row, WalRecord::OrderUpdate { update: OrderUpdate::Ack(ack), .. } if ack.client_order_id == "private-during-history-failure")));
        engine.recovery.client = Some(Arc::new(SharedReadClient(Arc::new(ReadClient {
            delay_ms: AtomicU64::new(0),
            fail_history: false,
            started: Default::default(),
        }))));
        engine.recovery.retry_after_ns = 0;
        engine.renew_execution_history().await.unwrap();
        assert!(!engine.recovery.history_requested && engine.private_stream_ready);
        assert!(
            !engine.may_open,
            "history repair must preserve the durable reconciliation latch"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_recovery_timeout_releases_the_read_owner_without_advancing_history() {
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        assert!(engine.may_open);
        let client = Arc::new(ReadClient {
            delay_ms: AtomicU64::new(20_000),
            fail_history: false,
            started: Default::default(),
        });
        engine.recovery = Recovery::new(Some(Box::new(SharedReadClient(client.clone()))));
        engine.recovery.reconnected();
        let before = engine.recovered_until_ms;
        engine.launch_account_recovery(true);
        client.started.notified().await;
        tokio::time::advance(MUTATION_DRAIN_TIMEOUT).await;
        let completion = engine.recovery.completed.recv().await.unwrap();
        assert!(
            matches!(
                &completion,
                Completion::Read(ReadResult {
                    history: Some(Err(_)),
                    ..
                })
            ),
            "timeout became a successful history interval"
        );
        engine.on_recovery_completion(completion).await.unwrap();
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();
        assert!(matches!(engine.recovery.phase, Phase::Idle));
        assert!(engine.recovery.history_requested);
        assert_eq!(engine.recovered_until_ms, before);
        assert!(engine.may_open && !engine.private_stream_ready);
    }

    #[tokio::test(start_paused = true)]
    async fn reconciled_empty_scans_keep_idle_accounts_recoverable_beyond_seven_days() {
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        let origin = clock::wall_ms();
        let _clock = engine_types::clock::install_virtual(origin as u64 * 1_000_000, 1).unwrap();
        engine.recovery = Recovery::new(Some(Box::new(SharedReadClient(Arc::new(ReadClient {
            delay_ms: AtomicU64::new(0),
            fail_history: false,
            started: Default::default(),
        })))));
        for day in 1..=12 {
            engine_types::clock::advance_virtual_to(1 + day * 86_400_000_000_000).unwrap();
            engine.renew_execution_history().await.unwrap();
            assert_eq!(
                engine.recovered_until_ms,
                clock::wall_ms(),
                "idle day {day}"
            );
            assert!(!engine.recovery.history_requested);
            assert!(engine.private_stream_ready);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn long_history_reads_retain_ownership_only_while_progressing() {
        struct ProgressClient {
            progress: Arc<AtomicU64>,
            stall: bool,
        }
        #[engine_types::async_trait]
        impl AccountRecoveryClient for ProgressClient {
            fn execution_history_progress(&self) -> Option<Arc<AtomicU64>> {
                Some(self.progress.clone())
            }
            async fn account_view(&self, _: &[String]) -> Result<AccountView, VenueError> {
                Ok(AccountView {
                    exact_amounts: None,
                    equity_usdt: 1.0,
                    available_usdt: 1.0,
                    positions: vec![],
                    observed_ns: clock::now_ns(),
                })
            }
            async fn executions(
                &self,
                _: &[String],
                _: i64,
                _: i64,
            ) -> Result<engine_types::ExecutionHistory, VenueError> {
                for page in 0..4 {
                    tokio::time::sleep(Duration::from_secs(8)).await;
                    if self.stall && page > 0 {
                        std::future::pending::<()>().await;
                    }
                    self.progress.fetch_add(1, Ordering::Relaxed);
                }
                Ok(Default::default())
            }
        }
        for stall in [false, true] {
            let mut recovery = Recovery::new(Some(Box::new(ProgressClient {
                progress: Default::default(),
                stall,
            })));
            let now = clock::wall_ms();
            recovery.start(
                Query {
                    started_ns: clock::now_ns(),
                    generation: 0,
                    history: Some((now - 100, now)),
                },
                vec![],
            );
            let Completion::Read(result) = recovery.completed.recv().await.unwrap() else {
                panic!("unexpected barrier");
            };
            assert_eq!(result.history.unwrap().is_err(), stall);
            recovery.stop_read();
            assert!(matches!(recovery.phase, Phase::Idle));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_slow_history_checkpoint_barrier_keeps_private_news_selectable() {
        let (mut engine, records) = crate::tests::callback_test_fixture(Vec::new()).await;
        engine
            .wal
            .delay_metadata_barriers(Duration::from_millis(150));
        engine.recovery.history_requested = true;
        let before = engine.recovered_until_ms;
        engine.launch_account_recovery(true);
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();
        let apply_started = std::time::Instant::now();
        engine.service_account_recovery().await.unwrap();
        assert!(
            apply_started.elapsed() < Duration::from_millis(100),
            "history checkpoint fsync ran on the core loop"
        );
        assert!(engine.recovery.uncommitted());
        assert_eq!(engine.recovered_until_ms, before);
        let started = std::time::Instant::now();
        engine
            .take_update(OrderUpdate::Ack(engine_types::OrderAck {
                client_order_id: "private-before-history-fsync".into(),
                venue_order_id: "actual".into(),
                sent_ns: 1,
                ack_ns: clock::now_ns(),
            }))
            .await
            .unwrap();
        assert!(started.elapsed() < Duration::from_millis(100));
        assert!(records.lock().unwrap().iter().any(|row| matches!(row, WalRecord::OrderUpdate { update: OrderUpdate::Ack(ack), .. } if ack.client_order_id == "private-before-history-fsync")));
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();
        assert!(!engine.recovery.uncommitted());
    }
}
