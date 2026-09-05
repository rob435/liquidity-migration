use std::collections::BTreeMap;
use std::io;

use engine_types::strategy_process::{
    CallbackEvent, CallbackPreparation, StrategyCallbackInput, StrategyProcessState,
    MAX_PROCESS_PROPOSAL_BYTES,
};
use engine_types::{StrategyId, WalRecord};

#[derive(Default)]
pub struct CallbackState {
    pub committed: BTreeMap<StrategyId, StrategyProcessState>,
    pub inputs: BTreeMap<u64, StrategyCallbackInput>,
    pub next_id: u64,
    bytes: BTreeMap<StrategyId, usize>,
    prepared_bytes: BTreeMap<StrategyId, usize>,
    committed_bytes: BTreeMap<StrategyId, usize>,
}

struct BoundedCount(usize);

impl io::Write for BoundedCount {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0 = self
            .0
            .checked_add(bytes.len())
            .filter(|size| *size <= MAX_PROCESS_PROPOSAL_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "strategy callback inbox is full",
                )
            })?;
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl CallbackState {
    pub fn encoded_size<T: serde::Serialize>(value: &T) -> Result<usize, String> {
        let mut count = BoundedCount(0);
        serde_json::to_writer(&mut count, value).map_err(|error| error.to_string())?;
        Ok(count.0)
    }

    pub fn size(input: &StrategyCallbackInput) -> Result<usize, String> {
        #[derive(serde::Serialize)]
        struct Queued<'a> {
            callback_id: u64,
            #[serde(skip_serializing_if = "Option::is_none")]
            order_origin: Option<engine_types::strategy_process::CallbackOrderOrigin>,
            strategy: StrategyId,
            event: &'a CallbackEvent,
            preparation: CallbackPreparation,
        }
        Self::encoded_size(&Queued {
            callback_id: input.callback_id,
            order_origin: input.order_origin,
            strategy: input.strategy,
            event: &input.event,
            preparation: CallbackPreparation::Queued,
        })
    }

    pub fn capacity_for(
        &self,
        input: &StrategyCallbackInput,
        unwritten_bytes: usize,
    ) -> Result<(), String> {
        let bytes = Self::size(input)?;
        let used: usize = self.bytes.values().sum();
        if used.saturating_add(unwritten_bytes).saturating_add(bytes) > MAX_PROCESS_PROPOSAL_BYTES {
            return Err("strategy callback inbox is full".into());
        }
        Ok(())
    }

    pub fn accept(&mut self, input: StrategyCallbackInput) -> Result<(), String> {
        self.accept_resident(input, true)
    }

    pub(super) fn load(&mut self, input: StrategyCallbackInput) -> Result<(), String> {
        self.accept_resident(input, false)
    }

    pub(super) fn load_capacity(
        &self,
        input: &StrategyCallbackInput,
        unwritten_bytes: usize,
    ) -> Result<(), String> {
        self.capacity_for(input, unwritten_bytes)?;
        let size = Self::preparation_size(input)?;
        if size > 0 {
            self.preparation_capacity(input.strategy, size)?;
        }
        Ok(())
    }

    fn accept_resident(
        &mut self,
        input: StrategyCallbackInput,
        retire_timer: bool,
    ) -> Result<(), String> {
        self.capacity_for(&input, 0)?;
        let prepared = Self::preparation_size(&input)?;
        if prepared > 0 {
            self.preparation_capacity(input.strategy, prepared)?;
        }
        let next_id = input
            .callback_id
            .checked_add(1)
            .ok_or("strategy callback id exhausted")?;
        if self.inputs.contains_key(&input.callback_id) {
            return Err("strategy callback id is repeated".into());
        }
        self.next_id = self.next_id.max(next_id);
        if retire_timer {
            self.retire_timer(&input)?;
        }
        *self.bytes.entry(input.strategy).or_default() += Self::size(&input)?;
        if prepared > 0 {
            self.prepared_bytes.insert(input.strategy, prepared);
        }
        self.inputs.insert(input.callback_id, input);
        Ok(())
    }

    pub fn can_prepare(&self, input: &StrategyCallbackInput) -> Result<usize, String> {
        let previous = self
            .inputs
            .get(&input.callback_id)
            .ok_or("prepared invocation has no queued input")?;
        if previous.order_origin != input.order_origin
            || previous.strategy != input.strategy
            || previous.event != input.event
            || previous.snapshot().is_some()
            || input.snapshot().is_none()
        {
            return Err("invalid strategy invocation preparation".into());
        }
        if self
            .inputs
            .values()
            .find(|known| known.strategy == input.strategy)
            .map(|known| known.callback_id)
            != Some(input.callback_id)
        {
            return Err("strategy invocation overtakes an earlier input".into());
        }
        if input
            .snapshot()
            .is_some_and(|snapshot| snapshot.strategy != input.strategy)
        {
            return Err("prepared invocation escapes its owner".into());
        }
        let size = Self::preparation_size(input)?;
        self.preparation_capacity(input.strategy, size)?;
        Ok(size)
    }

    pub fn prepared(&mut self, input: StrategyCallbackInput) -> Result<(), String> {
        let next = self.can_prepare(&input)?;
        self.prepared_bytes.insert(input.strategy, next);
        self.inputs.insert(input.callback_id, input);
        Ok(())
    }

    fn preparation_size(input: &StrategyCallbackInput) -> Result<usize, String> {
        if input.snapshot().is_none() {
            return Ok(0);
        }
        Self::encoded_size(&input.preparation)
    }

    fn preparation_capacity(&self, strategy: StrategyId, size: usize) -> Result<(), String> {
        if self.prepared_bytes.contains_key(&strategy) {
            return Err("strategy has more than one prepared invocation".into());
        }
        let used: usize = self.prepared_bytes.values().sum();
        if used.saturating_add(size) > MAX_PROCESS_PROPOSAL_BYTES {
            return Err("strategy prepared invocation budget is full".into());
        }
        Ok(())
    }

    fn process_size(state: &StrategyProcessState) -> Result<usize, String> {
        state.runtime.validate()?;
        if state.timers.len() > engine_types::strategy_process::MAX_PROCESS_TIMERS {
            return Err("strategy committed timer budget is full".into());
        }
        let mut timers = std::collections::BTreeSet::new();
        if state.timers.iter().any(|timer| !timers.insert(timer.id)) {
            return Err("strategy committed timer identity is repeated".into());
        }
        Self::encoded_size(state)
    }

    pub fn retained_process_bytes(&self, except: Option<StrategyId>) -> usize {
        self.committed_bytes
            .iter()
            .filter(|(strategy, _)| Some(**strategy) != except)
            .map(|(_, bytes)| bytes)
            .sum()
    }

    fn process_capacity(&self, state: &StrategyProcessState) -> Result<usize, String> {
        let size = Self::process_size(state)?;
        let used: usize = self
            .committed_bytes
            .iter()
            .filter(|(strategy, _)| **strategy != state.strategy)
            .map(|(_, bytes)| bytes)
            .sum();
        if used.saturating_add(size) > MAX_PROCESS_PROPOSAL_BYTES {
            return Err("strategy committed process budget is full".into());
        }
        Ok(size)
    }

    pub fn can_commit(&self, input_id: u64, state: &StrategyProcessState) -> Result<usize, String> {
        let input = self
            .inputs
            .get(&input_id)
            .ok_or("strategy commit has no accepted callback")?;
        if input.snapshot().is_none()
            || state.strategy != input.strategy
            || state.last_callback_id != input_id
        {
            return Err("strategy commit belongs to another callback".into());
        }
        if self
            .inputs
            .values()
            .find(|pending| pending.strategy == state.strategy)
            .is_some_and(|pending| pending.callback_id != input_id)
        {
            return Err("strategy commit overtakes an earlier callback".into());
        }
        self.process_capacity(state)
    }

    pub fn commit(&mut self, input_id: u64, state: StrategyProcessState) -> Result<(), String> {
        let committed_size = self.can_commit(input_id, &state)?;
        let input = self
            .inputs
            .get(&input_id)
            .ok_or("strategy commit has no accepted callback")?;
        let size = Self::size(input)?;
        let used = self
            .bytes
            .get_mut(&state.strategy)
            .ok_or("strategy callback byte ownership is absent")?;
        *used = used
            .checked_sub(size)
            .ok_or("strategy callback byte ownership underflow")?;
        self.prepared_bytes.remove(&state.strategy);
        self.committed_bytes.insert(state.strategy, committed_size);
        self.inputs.remove(&input_id);
        self.committed.insert(state.strategy, state);
        Ok(())
    }

    pub(super) fn retire_timer(&mut self, input: &StrategyCallbackInput) -> Result<(), String> {
        if let CallbackEvent::Timer { id, .. } = input.event {
            if let Some(state) = self.committed.get_mut(&input.strategy) {
                state.timers.retain(|timer| timer.id != id);
                self.committed_bytes
                    .insert(input.strategy, Self::encoded_size(state)?);
            }
        }
        Ok(())
    }

    pub(super) fn restore_process(
        &mut self,
        process: StrategyProcessState,
        count: usize,
    ) -> Result<(), String> {
        if process.strategy.idx() >= count {
            return Err("process restatement escapes its owner".into());
        }
        let size = self.process_capacity(&process)?;
        self.next_id = self.next_id.max(
            process
                .last_callback_id
                .checked_add(1)
                .ok_or("callback identity exhausted")?,
        );
        self.committed_bytes.insert(process.strategy, size);
        self.committed.insert(process.strategy, process);
        Ok(())
    }

    pub fn replay(records: &[WalRecord], strategy_count: usize) -> Result<Self, String> {
        let mut state = Self::default();
        for record in records {
            match record {
                WalRecord::SegmentBase {
                    strategy_processes,
                    strategy_callbacks,
                    strategy_callback_queues,
                    ..
                } => {
                    if !strategy_callback_queues.is_empty() {
                        return Err("callback cursor restatement requires paged replay".into());
                    }
                    state = Self::default();
                    for process in strategy_processes {
                        let size = state.process_capacity(process)?;
                        if process.strategy.idx() >= strategy_count
                            || state
                                .committed
                                .insert(process.strategy, process.clone())
                                .is_some()
                        {
                            return Err("invalid strategy process restatement".into());
                        }
                        state.committed_bytes.insert(process.strategy, size);
                        state.next_id = state.next_id.max(
                            process
                                .last_callback_id
                                .checked_add(1)
                                .ok_or("strategy callback id exhausted")?,
                        );
                    }
                    for input in strategy_callbacks {
                        state.validate_input(input, strategy_count)?;
                        if input.snapshot().is_some()
                            && state
                                .inputs
                                .values()
                                .any(|known| known.strategy == input.strategy)
                        {
                            return Err("prepared restatement overtakes an earlier input".into());
                        }
                        state.accept(input.clone())?;
                    }
                }
                WalRecord::StrategyCallbackQueued { input } => {
                    state.validate_input(input, strategy_count)?;
                    if !matches!(input.preparation, CallbackPreparation::Queued) {
                        return Err("queued callback already contains a prepared invocation".into());
                    }
                    if input.callback_id < state.next_id {
                        return Err("strategy callback id is reused".into());
                    }
                    state.accept(input.clone())?;
                }
                WalRecord::StrategyCallbackPrepared { input } => {
                    state.prepared(input.clone())?;
                }
                WalRecord::StrategyProcessTransitionQueued {
                    input_id, process, ..
                } => {
                    state.commit(*input_id, process.clone())?;
                }
                _ => {}
            }
        }
        Ok(state)
    }

    pub(super) fn validate_input(
        &self,
        input: &StrategyCallbackInput,
        strategy_count: usize,
    ) -> Result<(), String> {
        if input.strategy.idx() >= strategy_count
            || input
                .snapshot()
                .is_some_and(|snapshot| snapshot.strategy != input.strategy)
        {
            return Err("strategy callback escapes its owner".into());
        }
        Ok(())
    }
}

#[cfg(test)]
mod retained_budget_tests {
    use super::*;

    #[test]
    fn callback_inbox_capacity_is_shared_across_sleeves() {
        let mut state = CallbackState::default();
        let input = |id, strategy| StrategyCallbackInput {
            order_origin: None,
            callback_id: id,
            strategy: StrategyId(strategy),
            event: CallbackEvent::IntentRefused {
                symbol: engine_types::SymbolId(0),
                reduce_only: false,
                reason: "x".repeat(MAX_PROCESS_PROPOSAL_BYTES / 2),
            },
            preparation: CallbackPreparation::Queued,
        };
        state.accept(input(0, 0)).unwrap();
        assert!(
            state.accept(input(1, 1)).is_err(),
            "each sleeve obtained another full callback inbox budget"
        );
        assert_eq!(state.inputs.len(), 1);
        assert_eq!(state.inputs[&0].strategy, StrategyId(0));
    }
}

#[cfg(test)]
mod complete_process_budget_tests {
    use super::*;
    use engine_types::strategy_process::{CallbackSnapshot, StrategyRuntimeState};

    fn input(id: u64, strategy: u16) -> StrategyCallbackInput {
        let strategy = StrategyId(strategy);
        StrategyCallbackInput {
            order_origin: None,
            callback_id: id,
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
        }
    }

    #[test]
    fn retained_route_manifests_share_the_complete_process_budget() {
        let mut state = CallbackState::default();
        let process = |id, strategy| StrategyProcessState {
            strategy: StrategyId(strategy),
            last_callback_id: id,
            runtime: StrategyRuntimeState {
                schema_version: 1,
                kind: "test".into(),
                configuration_sha256: "0".repeat(64),
                payload: Vec::new(),
            },
            timers: Vec::new(),
            retained_signal_subscriptions: Some(vec![engine_types::Subscription {
                symbol: "x".repeat(MAX_PROCESS_PROPOSAL_BYTES / 2),
                feed: engine_types::Feed::Quote,
            }]),
        };
        state.accept(input(0, 0)).unwrap();
        state.commit(0, process(0, 0)).unwrap();
        state.accept(input(1, 1)).unwrap();
        assert!(
            state.commit(1, process(1, 1)).is_err(),
            "route manifests escaped the aggregate committed-state budget"
        );
        assert_eq!(state.committed.len(), 1);
        assert!(state.inputs.contains_key(&1));
    }

    #[test]
    fn prepared_snapshots_share_a_budget_separate_from_the_inbox() {
        let mut state = CallbackState::default();
        let mut first = input(0, 0);
        let mut second = input(1, 1);
        for input in [&mut first, &mut second] {
            let CallbackPreparation::Prepared { snapshot } = &mut input.preparation else {
                unreachable!()
            };
            snapshot.strategy_names = vec!["x".repeat(MAX_PROCESS_PROPOSAL_BYTES / 2)];
        }
        state.accept(first).unwrap();
        assert!(
            state.accept(second).is_err(),
            "prepared snapshots each obtained a new aggregate budget"
        );
        assert_eq!(state.inputs.len(), 1);
    }
}
