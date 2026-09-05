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
            strategy: StrategyId,
            event: &'a CallbackEvent,
            preparation: CallbackPreparation,
        }
        Self::encoded_size(&Queued {
            callback_id: input.callback_id,
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
        let used = self.bytes.get(&input.strategy).copied().unwrap_or_default();
        if used.saturating_add(unwritten_bytes).saturating_add(bytes) > MAX_PROCESS_PROPOSAL_BYTES {
            return Err("strategy callback inbox is full".into());
        }
        Ok(())
    }

    pub fn accept(&mut self, input: StrategyCallbackInput) -> Result<(), String> {
        self.capacity_for(&input, 0)?;
        let next_id = input
            .callback_id
            .checked_add(1)
            .ok_or("strategy callback id exhausted")?;
        if self.inputs.contains_key(&input.callback_id) {
            return Err("strategy callback id is repeated".into());
        }
        self.next_id = self.next_id.max(next_id);
        if let CallbackEvent::Timer { id, .. } = input.event {
            if let Some(state) = self.committed.get_mut(&input.strategy) {
                state.timers.retain(|timer| timer.id != id);
            }
        }
        *self.bytes.entry(input.strategy).or_default() += Self::size(&input)?;
        self.inputs.insert(input.callback_id, input);
        Ok(())
    }

    pub fn can_prepare(&self, input: &StrategyCallbackInput) -> Result<usize, String> {
        let previous = self
            .inputs
            .get(&input.callback_id)
            .ok_or("prepared invocation has no queued input")?;
        if previous.strategy != input.strategy
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
        Self::encoded_size(input)?;
        self.bytes
            .get(&input.strategy)
            .copied()
            .ok_or_else(|| "callback byte owner absent".into())
    }

    pub fn prepared(&mut self, input: StrategyCallbackInput) -> Result<(), String> {
        let next = self.can_prepare(&input)?;
        self.bytes.insert(input.strategy, next);
        self.inputs.insert(input.callback_id, input);
        Ok(())
    }

    pub fn commit(&mut self, input_id: u64, state: StrategyProcessState) -> Result<(), String> {
        state.runtime.validate()?;
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
        let size = Self::size(input)?;
        let used = self
            .bytes
            .get_mut(&state.strategy)
            .ok_or("strategy callback byte ownership is absent")?;
        *used = used
            .checked_sub(size)
            .ok_or("strategy callback byte ownership underflow")?;
        self.inputs.remove(&input_id);
        self.committed.insert(state.strategy, state);
        Ok(())
    }

    pub fn replay(records: &[WalRecord], strategy_count: usize) -> Result<Self, String> {
        let mut state = Self::default();
        for record in records {
            match record {
                WalRecord::SegmentBase {
                    strategy_processes,
                    strategy_callbacks,
                    ..
                } => {
                    state = Self::default();
                    for process in strategy_processes {
                        process.runtime.validate()?;
                        if process.strategy.idx() >= strategy_count
                            || state
                                .committed
                                .insert(process.strategy, process.clone())
                                .is_some()
                        {
                            return Err("invalid strategy process restatement".into());
                        }
                        state.next_id = state.next_id.max(
                            process
                                .last_callback_id
                                .checked_add(1)
                                .ok_or("strategy callback id exhausted")?,
                        );
                    }
                    for input in strategy_callbacks {
                        state.validate_input(input, strategy_count)?;
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

    fn validate_input(
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
