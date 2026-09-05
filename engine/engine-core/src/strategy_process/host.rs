use std::collections::{BTreeMap, VecDeque};
use std::path::PathBuf;

use engine_types::strategy_process::{
    CallbackEvent, CallbackPreparation, StrategyCallbackInput, StrategyRuntimeState,
};
use engine_types::{EngineEvent, Strategy, StrategyId, WalRecord};

use super::{state::CallbackState, CallbackProposal, StrategyProcess, CALLBACK_TIMEOUT};

pub enum CallbackExecution {
    Embedded,
    Isolated { executable: PathBuf },
}

pub struct CallbackCompletion {
    pub strategy: StrategyId,
    pub input_id: u64,
    pub result: Result<(StrategyProcess, CallbackProposal), String>,
}

pub enum CallbackWrite {
    Accept(Vec<StrategyCallbackInput>),
    Prepare(StrategyCallbackInput),
    Commit {
        input_id: u64,
        transition: Option<engine_types::StrategyTransitionState>,
        process: engine_types::strategy_process::StrategyProcessState,
        worker: StrategyProcess,
    },
}

pub struct CallbackHost {
    pub state: CallbackState,
    pub unwritten: VecDeque<StrategyCallbackInput>,
    pub completions: tokio::sync::mpsc::Receiver<CallbackCompletion>,
    pub faults: BTreeMap<StrategyId, String>,
    pub write: Option<CallbackWrite>,
    pub durable: tokio::sync::mpsc::Receiver<Result<(), engine_types::WalError>>,
    durability_result: tokio::sync::mpsc::Sender<Result<(), engine_types::WalError>>,
    initial: BTreeMap<StrategyId, StrategyRuntimeState>,
    closing: bool,
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
        let executable = match execution {
            CallbackExecution::Embedded => None,
            CallbackExecution::Isolated { executable } => Some(executable),
        };
        let state = CallbackState::replay(records, strategies.len())?;
        let mut initial = BTreeMap::new();
        if executable.is_some() {
            for (index, strategy) in strategies.iter().enumerate() {
                let id =
                    StrategyId(u16::try_from(index).map_err(|_| "too many strategy processes")?);
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
                initial.insert(id, runtime);
            }
        }
        let (completed, completions) = tokio::sync::mpsc::channel(strategies.len().max(1));
        let (durability_result, durable) = tokio::sync::mpsc::channel(1);
        Ok(Self {
            closing: false,
            write: None,
            durable,
            durability_result,
            state,
            unwritten: VecDeque::new(),
            completions,
            faults: BTreeMap::new(),
            initial,
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

    pub fn enqueue(&mut self, strategy: StrategyId, event: &EngineEvent) -> Result<(), String> {
        let event: CallbackEvent = event.into();
        let durable = matches!(
            event,
            CallbackEvent::Signal { .. }
                | CallbackEvent::StrategyEvent { .. }
                | CallbackEvent::EntryPermission { .. }
                | CallbackEvent::FlattenDirectional { .. }
        );
        if durable
            && self
                .state
                .inputs
                .values()
                .chain(self.unwritten.iter())
                .any(|input| input.strategy == strategy && input.event == event)
        {
            return Ok(());
        }
        let input = StrategyCallbackInput {
            callback_id: self.state.next_id,
            strategy,
            event,
            preparation: CallbackPreparation::Queued,
        };
        let used = self
            .unwritten_bytes
            .get(&strategy)
            .copied()
            .unwrap_or_default();
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

    pub fn accepted(&mut self, input: StrategyCallbackInput) -> Result<(), String> {
        let bytes = CallbackState::size(&input)?;
        let used = self
            .unwritten_bytes
            .get_mut(&input.strategy)
            .ok_or("unwritten callback has no byte owner")?;
        *used = used
            .checked_sub(bytes)
            .ok_or("unwritten callback byte ownership underflow")?;
        self.state.accept(input)
    }

    pub fn launch(&mut self, strategy: StrategyId) -> Result<(), String> {
        if self.closing || self.running.contains_key(&strategy) || !self.retry_ready(strategy) {
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
        let process = match self.processes.remove(&strategy) {
            Some(process) => process,
            None => StrategyProcess::spawn(
                self.executable
                    .as_deref()
                    .ok_or("embedded callback cannot launch a process")?,
            )?,
        };
        self.running.insert(strategy, input_id);
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

    pub fn succeeded(&mut self, strategy: StrategyId, process: StrategyProcess) {
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
