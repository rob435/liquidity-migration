use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::PathBuf;

use engine_types::strategy_process::{
    CallbackEvent, CallbackPreparation, StrategyCallbackInput, StrategyRuntimeState,
};
use engine_types::{EngineEvent, Strategy, StrategyId, WalRecord};

use super::{state::CallbackState, CallbackProposal, StrategyProcess, CALLBACK_TIMEOUT};

pub const MAX_STRATEGY_PROCESSES: usize = 4;

pub enum CallbackExecution {
    Embedded,
    Isolated { executable: PathBuf },
}

#[derive(Debug)]
pub enum EnqueueError {
    Deferred,
    Fault(String),
}

impl From<String> for EnqueueError {
    fn from(error: String) -> Self {
        Self::Fault(error)
    }
}

impl From<&str> for EnqueueError {
    fn from(error: &str) -> Self {
        Self::Fault(error.into())
    }
}

pub struct CallbackCompletion {
    pub strategy: StrategyId,
    pub input_id: u64,
    pub result: Result<(StrategyProcess, CallbackProposal), String>,
}

pub enum CallbackWrite {
    Accept(
        Vec<(
            StrategyCallbackInput,
            engine_types::strategy_process::CallbackWalCursor,
        )>,
    ),
    Prepare(
        (
            StrategyCallbackInput,
            engine_types::strategy_process::CallbackWalCursor,
        ),
    ),
    Commit {
        input_id: u64,
        transition: Option<engine_types::StrategyTransitionState>,
        process: engine_types::strategy_process::StrategyProcessState,
        worker: StrategyProcess,
    },
}

pub struct CallbackHost {
    pub state: CallbackState,
    pub pages: super::paging::CallbackPages,
    pub order_news: super::order_news::OrderNews,
    pub pending_boot: BTreeSet<StrategyId>,
    pub retry_inputs: super::retry::RetryInputs,
    pub refused_orders: BTreeSet<String>,
    pub unwritten: VecDeque<StrategyCallbackInput>,
    pub volatile: BTreeSet<u64>,
    pub completions: tokio::sync::mpsc::Receiver<CallbackCompletion>,
    pub deferred_completions: VecDeque<CallbackCompletion>,
    pub faults: BTreeMap<StrategyId, String>,
    pub write: Option<CallbackWrite>,
    pub durable: tokio::sync::mpsc::Receiver<Result<(), engine_types::WalError>>,
    durability_result: tokio::sync::mpsc::Sender<Result<(), engine_types::WalError>>,
    initial: BTreeMap<StrategyId, StrategyRuntimeState>,
    initial_bytes: BTreeMap<StrategyId, usize>,
    active: BTreeSet<StrategyId>,
    closing: bool,
    pub last_launched: Option<StrategyId>,
    executable: Option<PathBuf>,
    processes: BTreeMap<StrategyId, StrategyProcess>,
    running: BTreeMap<StrategyId, u64>,
    completed: tokio::sync::mpsc::Sender<CallbackCompletion>,
    unwritten_bytes: BTreeMap<StrategyId, usize>,
    retry_at: BTreeMap<StrategyId, std::time::Instant>,
    tasks: BTreeMap<StrategyId, tokio::task::JoinHandle<()>>,
}

impl CallbackHost {
    pub fn new(
        execution: CallbackExecution,
        strategies: &[Box<dyn Strategy>],
        records: &[WalRecord],
    ) -> Result<Self, String> {
        let state = CallbackState::replay(records, strategies.len())?;
        Self::build(execution, strategies, records, state, Default::default())
    }

    pub fn new_paged(
        execution: CallbackExecution,
        strategies: &[Box<dyn Strategy>],
        records: &[WalRecord],
        reader: Box<dyn engine_types::strategy_process::CallbackWalReader>,
    ) -> Result<Self, String> {
        let (state, mut pages) = super::paging::CallbackPages::replay(
            records,
            strategies.len(),
            reader.start().segment,
        )?;
        pages.attach(reader);
        Self::build(execution, strategies, records, state, pages)
    }

    fn build(
        execution: CallbackExecution,
        strategies: &[Box<dyn Strategy>],
        records: &[WalRecord],
        state: CallbackState,
        pages: super::paging::CallbackPages,
    ) -> Result<Self, String> {
        let executable = match execution {
            CallbackExecution::Embedded => None,
            CallbackExecution::Isolated { executable } => Some(executable),
        };
        let mut initial = BTreeMap::new();
        let mut initial_bytes = BTreeMap::new();
        let mut retained = state.retained_process_bytes(None);
        let mut active = BTreeSet::new();
        for (index, strategy) in strategies.iter().enumerate() {
            if strategy.callback_enabled() {
                active.insert(StrategyId(
                    u16::try_from(index).map_err(|_| "too many strategy processes")?,
                ));
            }
        }
        if executable.is_some() {
            for (index, strategy) in strategies.iter().enumerate() {
                let id =
                    StrategyId(u16::try_from(index).map_err(|_| "too many strategy processes")?);
                if !active.contains(&id) {
                    continue;
                }
                let runtime = strategy.runtime_state()?.ok_or_else(|| {
                    format!(
                        "strategy {} cannot run in an isolated process",
                        strategy.name()
                    )
                })?;
                if let Some(previous) = state.committed.get(&id) {
                    if previous.runtime.kind != runtime.kind
                        || previous.runtime.configuration_sha256 != runtime.configuration_sha256
                    {
                        return Err(format!(
                            "strategy {} process state belongs to another configuration",
                            strategy.name()
                        ));
                    }
                }
                if !state.committed.contains_key(&id) {
                    let size = CallbackState::encoded_size(&runtime)?;
                    retained = retained.saturating_add(size);
                    if retained > engine_types::strategy_process::MAX_PROCESS_PROPOSAL_BYTES {
                        return Err("strategy initial and committed process budget is full".into());
                    }
                    initial_bytes.insert(id, size);
                    initial.insert(id, runtime);
                }
            }
        }
        let effects = crate::effects::Effects::replay(records, strategies.len())?;
        let mut refused_orders = BTreeSet::new();
        for record in records {
            let WalRecord::StrategyCallbackSource {
                placement: Some(id),
                strategy,
                event,
            } = record
            else {
                continue;
            };
            let pending = effects.transitions.values().find_map(|transition| {
                transition
                    .order_ids
                    .iter()
                    .position(|known| known.as_ref() == Some(id))
                    .map(|index| (&transition.effects[index], transition.strategy))
            });
            let Some((engine_types::Action::Place(intent), owner)) = pending else {
                continue;
            };
            if !matches!(event, CallbackEvent::IntentRefused { symbol, reduce_only, .. } if *symbol == intent.symbol && *reduce_only == intent.reduce_only)
                || *strategy != owner
                || !refused_orders.insert(id.clone())
            {
                return Err("durable refusal changes or repeats its placement authority".into());
            }
        }
        let (completed, completions) = tokio::sync::mpsc::channel(strategies.len().max(1));
        let (durability_result, durable) = tokio::sync::mpsc::channel(1);
        Ok(Self {
            last_launched: None,
            closing: false,
            write: None,
            durable,
            durability_result,
            state,
            pages,
            order_news: super::order_news::OrderNews::default(),
            pending_boot: BTreeSet::new(),
            retry_inputs: Default::default(),
            refused_orders,
            unwritten: VecDeque::new(),
            volatile: BTreeSet::new(),
            completions,
            deferred_completions: VecDeque::new(),
            faults: BTreeMap::new(),
            initial,
            initial_bytes,
            active,
            executable,
            processes: BTreeMap::new(),
            running: BTreeMap::new(),
            completed,
            unwritten_bytes: BTreeMap::new(),
            retry_at: BTreeMap::new(),
            tasks: BTreeMap::new(),
        })
    }

    pub fn begin_write(
        &mut self,
        write: CallbackWrite,
        barrier: engine_types::wal::PendingBarrier,
    ) {
        assert!(
            self.write.is_none(),
            "callback durability already has an owner"
        );
        self.write = Some(write);
        let completed = self.durability_result.clone();
        if barrier.outstanding() {
            tokio::task::spawn_blocking(move || {
                let _ = completed.blocking_send(barrier.wait());
            });
        } else {
            let _ = completed.try_send(barrier.wait());
        }
    }

    pub fn is_active(&self, strategy: StrategyId) -> bool {
        self.active.contains(&strategy)
    }

    pub fn start_page_load(&mut self) {
        if self.write.is_none() {
            self.pages.start_load(&self.state, &self.active);
        }
    }
    pub fn unwritten_size(&self) -> usize {
        self.unwritten_bytes.values().sum()
    }
    pub fn pending_for(&self, strategy: StrategyId) -> bool {
        self.pages.owner_pending(strategy)
            || self
                .state
                .inputs
                .values()
                .any(|input| input.strategy == strategy)
            || self
                .unwritten
                .iter()
                .any(|input| input.strategy == strategy)
            || matches!(&self.write, Some(CallbackWrite::Accept(inputs)) if inputs.iter().any(|(input, _)| input.strategy == strategy))
    }

    pub fn isolated(&self) -> bool {
        self.executable.is_some()
    }
    pub fn retry_ready(&self, strategy: StrategyId) -> bool {
        self.retry_at
            .get(&strategy)
            .is_none_or(|deadline| *deadline <= std::time::Instant::now())
    }
    pub fn running(&self) -> bool {
        !self.running.is_empty()
    }
    pub fn pending(&self) -> bool {
        self.running() || !self.unwritten.is_empty() || !self.state.inputs.is_empty()
    }

    pub fn enqueue(
        &mut self,
        strategy: StrategyId,
        event: &EngineEvent,
    ) -> Result<(), EnqueueError> {
        if matches!(event, EngineEvent::Market(_))
            && (self.pending_for(strategy)
                || self.order_news.unread_for(strategy)
                || self.retry_inputs.blocks(strategy, event))
        {
            self.retry_inputs.remember(strategy, event);
            return Ok(());
        }
        let boot = matches!(event, EngineEvent::Boot);
        if boot {
            self.pending_boot.insert(strategy);
        }
        let result =
            if self.order_news.unread_for(strategy) || self.retry_inputs.blocks(strategy, event) {
                Err(EnqueueError::Deferred)
            } else {
                self.enqueue_inner(strategy, event, None)
            };
        if result.is_ok() {
            self.retry_inputs.forget(strategy, event);
        } else {
            self.retry_inputs.remember(strategy, event);
        }
        if boot && result.is_ok() {
            self.pending_boot.remove(&strategy);
        }
        result
    }

    pub fn enqueue_order(
        &mut self,
        strategy: StrategyId,
        update: engine_types::OrderUpdate,
        origin: engine_types::strategy_process::CallbackOrderOrigin,
    ) -> Result<(), EnqueueError> {
        self.enqueue_source(strategy, CallbackEvent::Order { update }, origin)
    }

    pub fn enqueue_source(
        &mut self,
        strategy: StrategyId,
        event: CallbackEvent,
        origin: engine_types::strategy_process::CallbackOrderOrigin,
    ) -> Result<(), EnqueueError> {
        self.enqueue_inner(strategy, &EngineEvent::try_from(&event)?, Some(origin))?;
        self.order_news.accepted(strategy, origin);
        Ok(())
    }

    fn enqueue_inner(
        &mut self,
        strategy: StrategyId,
        event: &EngineEvent,
        order_origin: Option<engine_types::strategy_process::CallbackOrderOrigin>,
    ) -> Result<(), EnqueueError> {
        let event: CallbackEvent = event.into();
        let durable = matches!(
            event,
            CallbackEvent::Boot
                | CallbackEvent::Signal { .. }
                | CallbackEvent::StrategyEvent { .. }
                | CallbackEvent::EntryPermission { .. }
                | CallbackEvent::FlattenDirectional { .. }
        );
        if (durable || order_origin.is_some())
            && self
                .state
                .inputs
                .values()
                .chain(self.unwritten.iter())
                .any(|input| {
                    input.strategy == strategy
                        && input.event == event
                        && input.order_origin == order_origin
                })
        {
            return Ok(());
        }
        if (durable || order_origin.is_some())
            && matches!(&self.write, Some(CallbackWrite::Accept(inputs)) if inputs.iter().any(|(input, _)| input.strategy == strategy && input.event == event && input.order_origin == order_origin))
        {
            return Ok(());
        }
        let input = StrategyCallbackInput {
            order_origin,
            callback_id: self.state.next_id,
            strategy,
            event,
            preparation: CallbackPreparation::Queued,
        };
        if self
            .state
            .inputs
            .values()
            .chain(self.unwritten.iter())
            .any(|pending| {
                pending.strategy == strategy
                    && matches!(
                        pending.event,
                        CallbackEvent::Quote { .. }
                            | CallbackEvent::Depth { .. }
                            | CallbackEvent::Trades { .. }
                            | CallbackEvent::Ticker { .. }
                            | CallbackEvent::FeedReset { .. }
                    )
            })
        {
            return Err(EnqueueError::Deferred);
        }
        if self.pages.enabled() {
            let hash = super::paging::CallbackPages::hash(&input)?;
            if (durable || order_origin.is_some())
                && self
                    .pages
                    .slots
                    .values()
                    .any(|slot| slot.strategy == strategy && slot.event_sha256 == hash)
            {
                return Ok(());
            }
            if !self.is_active(strategy) || self.pending_for(strategy) {
                return Err(EnqueueError::Deferred);
            }
        }
        let used = self.unwritten_bytes.values().sum();
        self.state.capacity_for(&input, used)?;
        self.state.next_id = self
            .state
            .next_id
            .checked_add(1)
            .ok_or("strategy callback id exhausted")?;
        *self.unwritten_bytes.entry(strategy).or_default() += CallbackState::size(&input)?;
        self.unwritten.push_back(input);
        Ok(())
    }

    pub fn accepted(
        &mut self,
        input: StrategyCallbackInput,
        cursor: engine_types::strategy_process::CallbackWalCursor,
    ) -> Result<(), String> {
        self.release_unwritten(&input)?;
        if self.pages.enabled() {
            self.pages.queued(&input, cursor)?;
        }
        self.state.accept(input)
    }

    pub fn accept_volatile(&mut self, input: StrategyCallbackInput) -> Result<(), String> {
        self.release_unwritten(&input)?;
        let id = input.callback_id;
        self.state.accept(input)?;
        self.volatile.insert(id);
        Ok(())
    }

    fn release_unwritten(&mut self, input: &StrategyCallbackInput) -> Result<(), String> {
        let bytes = CallbackState::size(input)?;
        let used = self
            .unwritten_bytes
            .get_mut(&input.strategy)
            .ok_or("unwritten callback has no byte owner")?;
        *used = used
            .checked_sub(bytes)
            .ok_or("unwritten callback byte ownership underflow")?;
        Ok(())
    }

    pub fn unchanged(
        &self,
        process: &engine_types::strategy_process::StrategyProcessState,
    ) -> bool {
        if let Some(prior) = self.state.committed.get(&process.strategy) {
            prior.runtime == process.runtime
                && prior.timers == process.timers
                && prior.retained_signal_subscriptions == process.retained_signal_subscriptions
        } else {
            self.initial.get(&process.strategy) == Some(&process.runtime)
                && process.timers.is_empty()
                && process.retained_signal_subscriptions.is_none()
        }
    }

    pub fn recycle(&mut self, strategy: StrategyId, process: StrategyProcess) {
        self.faults.remove(&strategy);
        self.retry_at.remove(&strategy);
        self.processes.insert(strategy, process);
    }

    pub fn launch(&mut self, strategy: StrategyId) -> Result<(), String> {
        if self.closing
            || !self.is_active(strategy)
            || self.running.contains_key(&strategy)
            || !self.retry_ready(strategy)
            || self.running.len() >= MAX_STRATEGY_PROCESSES
        {
            return Ok(());
        }
        let Some(input) = self
            .state
            .inputs
            .values()
            .find(|input| input.strategy == strategy)
        else {
            return Ok(());
        };
        let runtime = self
            .state
            .committed
            .get(&strategy)
            .map(|state| &state.runtime)
            .or_else(|| self.initial.get(&strategy))
            .ok_or("isolated strategy has no committed runtime")?
            .clone();
        let request = input.request(runtime)?;
        let input_id = input.callback_id;
        if !self.processes.contains_key(&strategy)
            && self.processes.len() + self.running.len() >= MAX_STRATEGY_PROCESSES
        {
            self.processes.pop_first();
        }
        let process = match self.processes.remove(&strategy) {
            Some(process) => process,
            None => StrategyProcess::spawn(
                self.executable
                    .as_deref()
                    .ok_or("embedded callback cannot launch a process")?,
            )?,
        };
        self.running.insert(strategy, input_id);
        self.last_launched = Some(strategy);
        let completed = self.completed.clone();
        let task = tokio::spawn(async move {
            let result = process.call(request, CALLBACK_TIMEOUT).await;
            let _ = completed
                .send(CallbackCompletion {
                    strategy,
                    input_id,
                    result,
                })
                .await;
        });
        self.tasks.insert(strategy, task);
        Ok(())
    }

    pub fn completed(&mut self, strategy: StrategyId, input_id: u64) -> Result<(), String> {
        if self.running.remove(&strategy) != Some(input_id) {
            return Err("strategy process completion has no matching owner".into());
        }
        self.tasks.remove(&strategy);
        Ok(())
    }

    pub fn can_commit(
        &self,
        input_id: u64,
        process: &engine_types::strategy_process::StrategyProcessState,
    ) -> Result<(), String> {
        let proposed = self.state.can_commit(input_id, process)?;
        let initial: usize = self
            .initial_bytes
            .iter()
            .filter(|(strategy, _)| **strategy != process.strategy)
            .map(|(_, bytes)| bytes)
            .sum();
        if initial
            .saturating_add(self.state.retained_process_bytes(Some(process.strategy)))
            .saturating_add(proposed)
            > engine_types::strategy_process::MAX_PROCESS_PROPOSAL_BYTES
        {
            return Err("strategy initial and committed process budget is full".into());
        }
        Ok(())
    }

    pub fn succeeded(&mut self, strategy: StrategyId, process: StrategyProcess) {
        self.initial.remove(&strategy);
        self.initial_bytes.remove(&strategy);
        self.faults.remove(&strategy);
        self.retry_at.remove(&strategy);
        self.processes.insert(strategy, process);
    }

    pub fn failed(&mut self, strategy: StrategyId, error: String) {
        self.faults.insert(strategy, error);
        self.retry_at.insert(
            strategy,
            std::time::Instant::now() + std::time::Duration::from_secs(1),
        );
    }

    pub async fn stop(&mut self) {
        self.closing = true;
        for task in self.tasks.values() {
            task.abort();
        }
        for (_, task) in std::mem::take(&mut self.tasks) {
            let _ = task.await;
        }
        self.running.clear();
        self.processes.clear();
        while self.completions.try_recv().is_ok() {}
    }
}

impl Drop for CallbackHost {
    fn drop(&mut self) {
        for task in self.tasks.values() {
            task.abort();
        }
    }
}

#[cfg(test)]
impl CallbackHost {
    pub(crate) fn expect_test_completion(&mut self, strategy: StrategyId, input_id: u64) {
        assert!(self.running.insert(strategy, input_id).is_none());
    }
}

#[cfg(test)]
mod process_capacity_tests {
    use super::*;
    use engine_types::strategy_process::CallbackSnapshot;

    #[tokio::test(start_paused = true)]
    async fn simultaneous_callbacks_share_a_fixed_process_pool() {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let strategies: Vec<_> = (0..5)
            .map(|id| engine_strategies::build_strategy("probe", StrategyId(id), &params).unwrap())
            .collect();
        let mut host = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/sleep".into(),
            },
            &strategies,
            &[],
        )
        .unwrap();
        for id in 0..5 {
            let strategy = StrategyId(id);
            host.state
                .accept(StrategyCallbackInput {
                    order_origin: None,
                    callback_id: id.into(),
                    strategy,
                    event: CallbackEvent::Boot,
                    preparation: CallbackPreparation::Prepared {
                        snapshot: CallbackSnapshot {
                            strategy,
                            now_ns: 1,
                            wall_ms: 1,
                            entries_enabled: true,
                            account: engine_types::StrategyAccountSummary {
                                equity_usdt: 1.0,
                                available_margin_usdt: 1.0,
                                observed_ns: 1,
                            },
                            symbols: Vec::new(),
                            orders: Vec::new(),
                            global_checkpoint: None,
                            strategy_names: Vec::new(),
                            strategy_events: Vec::new(),
                        },
                    },
                })
                .unwrap();
            host.launch(strategy).unwrap();
        }
        assert_eq!(
            host.running.len(),
            4,
            "configured sleeve count multiplied the worker resource budget"
        );
        assert_eq!(
            host.state.inputs.len(),
            5,
            "waiting for a process slot discarded a callback"
        );
        assert!(!host.running.contains_key(&StrategyId(4)));
        host.stop().await;
    }
}

#[cfg(test)]
mod initial_runtime_budget_tests {
    use super::*;
    struct LargeRuntime;
    impl Strategy for LargeRuntime {
        fn name(&self) -> &str {
            "large-runtime"
        }
        fn subscriptions(&self) -> Vec<engine_types::Subscription> {
            Vec::new()
        }
        fn on_event(&mut self, _: &EngineEvent, _: &mut dyn engine_types::StrategyCtx) {}
        fn runtime_state(&self) -> Result<Option<StrategyRuntimeState>, String> {
            Ok(Some(StrategyRuntimeState {
                schema_version: 1,
                kind: "large-runtime".into(),
                configuration_sha256: "0".repeat(64),
                payload: vec![0; engine_types::strategy_process::MAX_PROCESS_PROPOSAL_BYTES / 4],
            }))
        }
    }
    #[test]
    fn configured_initial_runtimes_share_the_committed_process_budget() {
        let strategies: Vec<Box<dyn Strategy>> = (0..3)
            .map(|_| Box::new(LargeRuntime) as Box<dyn Strategy>)
            .collect();
        assert!(
            CallbackHost::new(
                CallbackExecution::Isolated {
                    executable: "/bin/false".into()
                },
                &strategies,
                &[]
            )
            .is_err(),
            "initial runtime copies escaped the aggregate retained-state budget"
        );
    }
}

#[cfg(test)]
mod admission_ownership_tests {
    use super::*;
    use engine_types::strategy_process::{CallbackWalCursor, CallbackWalReader, CallbackWalRecord};
    struct EmptyReader;
    impl CallbackWalReader for EmptyReader {
        fn start(&self) -> CallbackWalCursor {
            CallbackWalCursor {
                segment: 1,
                sequence: 1,
                offset: 0,
            }
        }
        fn next(
            &mut self,
            _: CallbackWalCursor,
        ) -> Result<Option<CallbackWalRecord>, engine_types::WalError> {
            Ok(None)
        }
    }

    #[tokio::test(start_paused = true)]
    async fn callback_acceptance_barrier_owns_the_head_and_deduplicates_redelivery() {
        let params = toml::from_str("symbol='BTCUSDT'\nevery_s=60\nenabled=false").unwrap();
        let strategies =
            vec![engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap()];
        let mut host = CallbackHost::new_paged(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &strategies,
            &[],
            Box::new(EmptyReader),
        )
        .unwrap();
        host.enqueue(StrategyId(0), &EngineEvent::Boot).unwrap();
        let input = host.unwritten.pop_front().unwrap();
        let cursor = CallbackWalCursor {
            segment: 1,
            sequence: 1,
            offset: 0,
        };
        host.begin_write(
            CallbackWrite::Accept(vec![(input, cursor)]),
            engine_types::wal::PendingBarrier::settled(),
        );
        assert!(
            host.pending_for(StrategyId(0)),
            "the barrier lost ownership of its not-yet-published callback"
        );
        host.enqueue(StrategyId(0), &EngineEvent::Boot).unwrap();
        assert!(
            host.unwritten.is_empty(),
            "redelivery during fsync duplicated the callback input"
        );
        let later = EngineEvent::Timer {
            id: engine_types::TimerId(99),
            now_ns: 1,
        };
        assert!(
            host.enqueue(StrategyId(0), &later).is_err(),
            "a later callback overtook the durable acceptance barrier"
        );
        assert!(host.unwritten.is_empty());
    }
}
