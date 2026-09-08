use std::collections::BTreeMap;

use engine_types::strategy_process::{
    CallbackPreparation, CallbackQueueSlot, CallbackWalCursor, CallbackWalReader,
    StrategyCallbackInput,
};
use engine_types::{StrategyId, WalRecord};
use sha2::{Digest, Sha256};

use super::state::CallbackState;

// Legacy queued JSON includes an ID, owner, event and preparation (at least 64 bytes).
pub const MAX_CALLBACK_QUEUE_SLOTS: usize =
    engine_types::strategy_process::MAX_PROCESS_PROPOSAL_BYTES / 64;

pub struct PageCompletion {
    reader: Box<dyn CallbackWalReader>,
    pub callback_id: u64,
    pub input: Result<StrategyCallbackInput, engine_types::WalError>,
}

pub struct CallbackPages {
    pub slots: BTreeMap<u64, CallbackQueueSlot>,
    reader: Option<Box<dyn CallbackWalReader>>,
    loading: Option<u64>,
    retry_after: BTreeMap<StrategyId, std::time::Instant>,
    last_owner: Option<StrategyId>,
    sender: tokio::sync::mpsc::Sender<PageCompletion>,
    pub completed: tokio::sync::mpsc::Receiver<PageCompletion>,
}

impl Default for CallbackPages {
    fn default() -> Self {
        let (sender, completed) = tokio::sync::mpsc::channel(1);
        Self {
            slots: BTreeMap::new(),
            reader: None,
            loading: None,
            retry_after: BTreeMap::new(),
            last_owner: None,
            sender,
            completed,
        }
    }
}

impl CallbackPages {
    pub(crate) fn replay_committed(
        records: &[WalRecord],
        count: usize,
    ) -> Result<BTreeMap<StrategyId, engine_types::strategy_process::StrategyProcessState>, String>
    {
        // Assembly needs private state only; synthetic inline cursors never leave this projection.
        let (state, _) = Self::replay(records, count, 1)?;
        Ok(state.committed)
    }

    pub fn hash(input: &StrategyCallbackInput) -> Result<[u8; 32], String> {
        struct HashWriter(Sha256);
        impl std::io::Write for HashWriter {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                self.0.update(bytes);
                Ok(bytes.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let mut writer = HashWriter(Sha256::new());
        serde_json::to_writer(&mut writer, &(&input.event, input.order_origin))
            .map_err(|error| error.to_string())?;
        Ok(writer.0.finalize().into())
    }

    pub fn enabled(&self) -> bool {
        self.reader.is_some() || self.loading.is_some()
    }
    pub fn loading(&self) -> bool {
        self.loading.is_some()
    }
    pub fn owner_pending(&self, owner: StrategyId) -> bool {
        self.slots.values().any(|slot| slot.strategy == owner)
    }
    pub fn attach(&mut self, reader: Box<dyn CallbackWalReader>) {
        self.reader = Some(reader);
    }

    pub fn queued(
        &mut self,
        input: &StrategyCallbackInput,
        cursor: CallbackWalCursor,
    ) -> Result<(), String> {
        let slot = CallbackQueueSlot {
            callback_id: input.callback_id,
            strategy: input.strategy,
            queued: cursor,
            prepared: input.snapshot().map(|_| cursor),
            event_sha256: Self::hash(input)?,
        };
        self.insert(slot)
    }

    fn insert(&mut self, slot: CallbackQueueSlot) -> Result<(), String> {
        if self.slots.contains_key(&slot.callback_id) {
            return Err("callback queue slot is repeated".into());
        }
        if self.slots.len() >= MAX_CALLBACK_QUEUE_SLOTS {
            return Err("callback disk queue index budget is full".into());
        }
        self.slots.insert(slot.callback_id, slot);
        Ok(())
    }

    pub fn remove(&mut self, id: u64) {
        self.slots.remove(&id);
    }

    pub fn prepared(
        &mut self,
        input: &StrategyCallbackInput,
        cursor: CallbackWalCursor,
    ) -> Result<(), String> {
        let first = self
            .slots
            .values()
            .find(|slot| slot.strategy == input.strategy)
            .ok_or("prepared callback has no durable queue slot")?;
        if first.callback_id != input.callback_id
            || first.prepared.is_some()
            || input.snapshot().is_none()
            || first.event_sha256 != Self::hash(input)?
        {
            return Err("prepared callback changes or overtakes its queued input".into());
        }
        let slot = self
            .slots
            .get_mut(&input.callback_id)
            .expect("checked slot");
        slot.prepared = Some(cursor);
        Ok(())
    }

    pub fn start_load(
        &mut self,
        state: &CallbackState,
        active: &std::collections::BTreeSet<StrategyId>,
    ) {
        if self.loading.is_some() || self.reader.is_none() {
            return;
        }
        let mut heads = BTreeMap::new();
        for slot in self.slots.values() {
            heads.entry(slot.strategy).or_insert(slot);
        }
        let mut choices: Vec<_> = heads
            .values()
            .filter(|slot| {
                active.contains(&slot.strategy)
                    && self
                        .retry_after
                        .get(&slot.strategy)
                        .is_none_or(|after| *after <= std::time::Instant::now())
                    && !state
                        .inputs
                        .values()
                        .any(|input| input.strategy == slot.strategy)
            })
            .copied()
            .collect();
        if let Some(previous) = self.last_owner {
            let next = choices.partition_point(|slot| slot.strategy <= previous);
            choices.rotate_left(next);
        }
        let Some(slot) = choices.first() else {
            return;
        };
        let id = slot.callback_id;
        let cursor = slot.prepared.unwrap_or(slot.queued);
        let mut reader = self.reader.take().expect("available callback page reader");
        self.loading = Some(id);
        self.last_owner = Some(slot.strategy);
        let sender = self.sender.clone();
        tokio::task::spawn_blocking(move || {
            let input = reader.read_callback(cursor, id);
            let _ = sender.blocking_send(PageCompletion {
                reader,
                callback_id: id,
                input,
            });
        });
    }

    pub fn returned(
        &mut self,
        completion: PageCompletion,
        state: &mut CallbackState,
        unwritten_bytes: usize,
    ) -> Result<Option<(StrategyId, String)>, String> {
        if self.loading.take() != Some(completion.callback_id) || self.reader.is_some() {
            return Err("callback page completion has no reader owner".into());
        }
        self.reader = Some(completion.reader);
        let input = completion.input.map_err(|error| error.to_string())?;
        let slot = self
            .slots
            .get(&completion.callback_id)
            .ok_or("loaded callback lost its queue slot")?;
        if input.callback_id != slot.callback_id
            || input.strategy != slot.strategy
            || Self::hash(&input)? != slot.event_sha256
            || input.snapshot().is_some() != slot.prepared.is_some()
        {
            return Err("callback page differs from its durable queue authority".into());
        }
        if let Err(error) = state.load_capacity(&input, unwritten_bytes) {
            self.retry_after.insert(
                input.strategy,
                std::time::Instant::now() + std::time::Duration::from_secs(1),
            );
            return Ok(Some((input.strategy, error)));
        }
        self.retry_after.remove(&input.strategy);
        state.load(input)?;
        Ok(None)
    }

    pub fn replay(
        records: &[WalRecord],
        count: usize,
        segment: u64,
    ) -> Result<(CallbackState, Self), String> {
        let mut state = CallbackState::default();
        let mut pages = Self::default();
        for (index, record) in records.iter().enumerate() {
            let cursor = CallbackWalCursor {
                segment,
                sequence: index as u64 + 1,
                offset: 0,
            };
            match record {
                WalRecord::StrategyRuntimeReconfigured {
                    strategy,
                    previous_configuration_sha256,
                    runtime,
                } => state.reconfigure_runtime(
                    *strategy,
                    previous_configuration_sha256,
                    runtime,
                    count,
                )?,
                WalRecord::SegmentBase {
                    strategy_processes,
                    strategy_callbacks,
                    strategy_callback_queues,
                    ..
                } => {
                    state = CallbackState::default();
                    pages.slots.clear();
                    for process in strategy_processes {
                        if state.committed.contains_key(&process.strategy) {
                            return Err("invalid strategy process restatement".into());
                        }
                        state.restore_process(process.clone(), count)?;
                    }
                    for slot in strategy_callback_queues {
                        if slot.strategy.idx() >= count
                            || slot.queued.segment == 0
                            || slot.queued.sequence == 0
                            || slot.prepared.is_some_and(|prepared| {
                                prepared.segment == 0 || prepared.sequence == 0
                            })
                        {
                            return Err("invalid callback queue restatement".into());
                        }
                        state.next_id = state.next_id.max(
                            slot.callback_id
                                .checked_add(1)
                                .ok_or("callback identity exhausted")?,
                        );
                        pages.insert(slot.clone())?;
                    }
                    let mut legacy_bytes = 0usize;
                    for input in strategy_callbacks {
                        legacy_bytes = legacy_bytes.saturating_add(CallbackState::size(input)?);
                        if legacy_bytes > engine_types::strategy_process::MAX_PROCESS_PROPOSAL_BYTES
                        {
                            return Err(
                                "legacy callback restatement exceeds its inbox budget".into()
                            );
                        }
                        state.validate_input(input, count)?;
                        state.retire_timer(input)?;
                        state.next_id = state.next_id.max(
                            input
                                .callback_id
                                .checked_add(1)
                                .ok_or("callback identity exhausted")?,
                        );
                        pages.queued(input, cursor)?;
                    }
                }
                WalRecord::Retained(
                    engine_types::wal::RetainedWalRecord::StrategyCallbackQueued { input },
                ) => {
                    state.validate_input(input, count)?;
                    CallbackState::size(input)?;
                    if input.callback_id < state.next_id
                        || !matches!(input.preparation, CallbackPreparation::Queued)
                    {
                        return Err("callback queue reuses an identity or arrives prepared".into());
                    }
                    state.next_id = input
                        .callback_id
                        .checked_add(1)
                        .ok_or("callback identity exhausted")?;
                    state.retire_timer(input)?;
                    pages.queued(input, cursor)?;
                }
                WalRecord::Retained(
                    engine_types::wal::RetainedWalRecord::StrategyCallbackPrepared { input },
                ) => {
                    state.validate_input(input, count)?;
                    pages.prepared(input, cursor)?;
                }
                WalRecord::StrategyTransitionQueued { transition }
                    if matches!(
                        transition.origin,
                        engine_types::wal::StrategyTransitionOrigin::Embedded
                    ) && transition.effects.iter().any(|action| {
                        matches!(
                            action,
                            engine_types::Action::SetStrategyGlobalCheckpoint { .. }
                        )
                    }) =>
                {
                    state.forget_process(transition.strategy);
                }
                WalRecord::Retained(
                    engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued {
                        input_id,
                        process,
                        ..
                    },
                ) => {
                    let slot = pages
                        .slots
                        .values()
                        .find(|slot| slot.strategy == process.strategy)
                        .ok_or("process commit has no queued callback")?;
                    if slot.callback_id != *input_id
                        || process.last_callback_id != *input_id
                        || slot.prepared.is_none()
                    {
                        return Err("process commit changes or overtakes callback authority".into());
                    }
                    pages.remove(*input_id);
                    state.restore_process(process.clone(), count)?;
                }
                _ => (),
            }
        }
        let mut owners = std::collections::BTreeSet::new();
        for slot in pages.slots.values() {
            let first = owners.insert(slot.strategy);
            if (!first && slot.prepared.is_some())
                || state
                    .committed
                    .get(&slot.strategy)
                    .is_some_and(|process| process.last_callback_id >= slot.callback_id)
            {
                return Err(
                    "callback queue restatement repeats committed input or prepares a later input"
                        .into(),
                );
            }
        }
        Ok((state, pages))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::strategy_process::{CallbackEvent, CallbackSnapshot, StrategyProcessState};
    use engine_types::{Strategy, Wal};

    fn probe(id: u16) -> Box<dyn Strategy> {
        engine_strategies::build_strategy(
            "probe",
            StrategyId(id),
            &toml::from_str("symbol='BTCUSDT'\nevery_s=60\nenabled=false").unwrap(),
        )
        .unwrap()
    }
    async fn base() -> WalRecord {
        let (engine, _) = crate::tests::callback_test_fixture(vec![probe(0), probe(1)]).await;
        engine.rotation_base(1)
    }
    fn input(id: u64, owner: u16, event: CallbackEvent) -> StrategyCallbackInput {
        StrategyCallbackInput {
            callback_id: id,
            strategy: StrategyId(owner),
            order_origin: None,
            event,
            preparation: CallbackPreparation::Queued,
        }
    }
    fn prepare(mut input: StrategyCallbackInput) -> StrategyCallbackInput {
        input.preparation = CallbackPreparation::Prepared {
            snapshot: CallbackSnapshot {
                strategy: input.strategy,
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
        };
        input
    }
    fn restate(base: &mut WalRecord, state: &CallbackState, pages: &CallbackPages) {
        let WalRecord::SegmentBase {
            strategy_callbacks,
            strategy_processes,
            strategy_callback_queues,
            ..
        } = base
        else {
            unreachable!()
        };
        strategy_callbacks.clear();
        *strategy_processes = state.committed.values().cloned().collect();
        *strategy_callback_queues = pages.slots.values().cloned().collect();
    }
    fn process(input: &StrategyCallbackInput) -> StrategyProcessState {
        StrategyProcessState {
            strategy: input.strategy,
            last_callback_id: input.callback_id,
            runtime: probe(input.strategy.0).runtime_state().unwrap().unwrap(),
            timers: Vec::new(),
            retained_signal_subscriptions: None,
        }
    }

    #[tokio::test(start_paused = true)]
    async fn registry_assembly_preserves_inactive_runtime_with_paged_callbacks() {
        let path = crate::testpath::temp_path("registry-paged-callbacks");
        let mut base = base().await;
        let mut committed = process(&input(1, 0, CallbackEvent::Boot));
        committed
            .timers
            .push(engine_types::strategy_process::StrategyTimerState {
                id: engine_types::TimerId(7),
                deadline_ns: 10,
                deadline_wall_ms: 10,
            });
        committed.retained_signal_subscriptions = Some(vec![engine_types::Subscription {
            symbol: "BTCUSDT".into(),
            feed: engine_types::Feed::Quote,
        }]);
        let mut state = CallbackState::default();
        state.restore_process(committed.clone(), 2).unwrap();
        let mut pages = CallbackPages::default();
        restate(&mut base, &state, &pages);
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        wal.append(&base).unwrap();
        let queued = input(2, 0, CallbackEvent::Boot);
        let sequence = crate::testpath::append_history(
            &mut wal,
            &path,
            &WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
                    input: queued.clone(),
                },
            ),
        )
        .unwrap();
        pages
            .queued(
                &queued,
                CallbackWalCursor {
                    segment: 1,
                    sequence,
                    offset: 0,
                },
            )
            .unwrap();
        restate(&mut base, &state, &pages);
        assert!(wal.rotate(&base).unwrap());
        drop(wal);
        let (_, rows) = engine_wal::open_current(&path).unwrap();
        let records: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        for (_, segment) in engine_wal::segments(&path).unwrap() {
            std::fs::remove_file(segment).unwrap();
        }
        assert_eq!(records.len(), 1);
        let plan =
            crate::identities::plan_identities(&records, &[], None, &Default::default(), &[])
                .unwrap();
        let built = crate::assembly::strategies_for_registry(&[], &plan, &records).unwrap_or_else(
            |error| panic!("valid v7 queued callback prevents strategy assembly: {error}"),
        );
        assert!(!built[0].callback_enabled());
        assert_eq!(
            built[0].runtime_state().unwrap(),
            Some(committed.runtime.clone())
        );
        assert_eq!(
            built[0].retained_signal_subscriptions(),
            committed.retained_signal_subscriptions.clone()
        );
        assert_eq!(
            CallbackPages::replay_committed(&records, 2).unwrap()[&StrategyId(0)],
            committed
        );
        let mut duplicate_process = records.clone();
        let WalRecord::SegmentBase {
            strategy_processes, ..
        } = &mut duplicate_process[0]
        else {
            panic!()
        };
        strategy_processes.push(strategy_processes[0].clone());
        assert!(
            crate::assembly::strategies_for_registry(&[], &plan, &duplicate_process).is_err(),
            "duplicate process owners must remain invalid during registry assembly"
        );
        let mut bad_queue = records.clone();
        let WalRecord::SegmentBase {
            strategy_callback_queues,
            ..
        } = &mut bad_queue[0]
        else {
            panic!()
        };
        strategy_callback_queues[0].queued.sequence = 0;
        assert!(
            crate::assembly::strategies_for_registry(&[], &plan, &bad_queue)
                .err()
                .unwrap()
                .to_string()
                .contains("invalid callback queue restatement")
        );

        let mut bad_commit = records.clone();
        bad_commit.push(WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued {
                input_id: queued.callback_id,
                process: process(&queued),
                transition: None,
            },
        ));
        assert!(
            crate::assembly::strategies_for_registry(&[], &plan, &bad_commit)
                .err()
                .unwrap()
                .to_string()
                .contains("process commit changes or overtakes callback authority")
        );

        let mut timer = records;
        timer.push(WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
                input: input(
                    3,
                    0,
                    CallbackEvent::Timer {
                        id: engine_types::TimerId(7),
                        now_ns: 10,
                    },
                ),
            },
        ));
        assert!(
            CallbackPages::replay_committed(&timer, 2).unwrap()[&StrategyId(0)]
                .timers
                .is_empty()
        );
    }

    #[test]
    fn disk_queue_count_bound_covers_the_legacy_minimum_callback_encoding() {
        let smallest = input(0, 0, CallbackEvent::Boot);
        assert!(CallbackState::size(&smallest).unwrap() >= 64);
        assert!(std::mem::size_of::<CallbackQueueSlot>() <= 128);
    }

    #[tokio::test(start_paused = true)]
    async fn inactive_callback_pages_leave_the_resident_budget_for_active_sleeves() {
        let path = crate::testpath::temp_path("inactive-callback-pages");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let inactive = input(
            0,
            0,
            CallbackEvent::IntentRefused {
                symbol: engine_types::SymbolId(0),
                reduce_only: false,
                reason: "x".repeat(
                    engine_types::strategy_process::MAX_PROCESS_PROPOSAL_BYTES - 1024 * 1024,
                ),
            },
        );
        let active = input(1, 1, CallbackEvent::Boot);
        for input in [&inactive, &active] {
            crate::testpath::append_history(
                &mut wal,
                &path,
                &WalRecord::Retained(
                    engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
                        input: input.clone(),
                    },
                ),
            )
            .unwrap();
        }
        wal.barrier().unwrap();
        drop(wal);
        let (mut wal, rows) = engine_wal::WalWriter::open(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let (mut state, mut pages) = CallbackPages::replay(&rows, 2, 1).unwrap();
        assert!(
            state.inputs.is_empty(),
            "inactive durable payloads occupy the active callback budget after replay"
        );
        assert_eq!(pages.slots.len(), 2);
        pages.attach(wal.callback_reader().unwrap().unwrap());
        pages.start_load(&state, &std::collections::BTreeSet::from([StrategyId(1)]));
        let completion = pages.completed.recv().await.unwrap();
        assert!(pages.returned(completion, &mut state, 0).unwrap().is_none());
        assert_eq!(state.inputs.values().collect::<Vec<_>>(), [&active]);
        assert!(pages.owner_pending(StrategyId(0)));
        let prepared = prepare(active);
        let sequence = crate::testpath::append_history(
            &mut wal,
            &path,
            &WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackPrepared {
                    input: prepared.clone(),
                },
            ),
        )
        .unwrap();
        wal.barrier().unwrap();
        pages
            .prepared(
                &prepared,
                CallbackWalCursor {
                    segment: 1,
                    sequence,
                    offset: 0,
                },
            )
            .unwrap();
        state.prepared(prepared.clone()).unwrap();
        let committed = process(&prepared);
        crate::testpath::append_history(
            &mut wal,
            &path,
            &WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued {
                    input_id: prepared.callback_id,
                    transition: None,
                    process: committed.clone(),
                },
            ),
        )
        .unwrap();
        state.commit(prepared.callback_id, committed).unwrap();
        pages.remove(prepared.callback_id);
        let mut base = base().await;
        restate(&mut base, &state, &pages);
        wal.rotate(&base).unwrap();
        drop(wal);
        let (mut wal, rows) = engine_wal::open_current(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let reader = wal.callback_reader().unwrap().unwrap();
        let (mut state, mut pages) =
            CallbackPages::replay(&rows, 2, reader.start().segment).unwrap();
        assert_eq!(pages.slots.keys().copied().collect::<Vec<_>>(), [0]);
        assert!(state.inputs.is_empty());
        pages.attach(reader);
        pages.start_load(&state, &std::collections::BTreeSet::from([StrategyId(0)]));
        let completion = pages.completed.recv().await.unwrap();
        assert!(
            pages
                .returned(completion, &mut state, 2 * 1024 * 1024)
                .unwrap()
                .is_some(),
            "callback paging ignored another input's reserved durability bytes"
        );
        assert!(state.inputs.is_empty());
        assert_eq!(pages.slots.len(), 1);
        pages.retry_after.clear();
        pages.start_load(&state, &std::collections::BTreeSet::from([StrategyId(0)]));
        let completion = pages.completed.recv().await.unwrap();
        assert!(pages.returned(completion, &mut state, 0).unwrap().is_none());
        assert_eq!(
            state.inputs[&0], inactive,
            "reactivation changed the paused callback payload"
        );
        assert_eq!(state.committed[&StrategyId(1)].last_callback_id, 1);
    }

    #[tokio::test(start_paused = true)]
    async fn callback_pages_keep_preparation_and_completion_across_two_rotations() {
        let path = crate::testpath::temp_path("callback-page-preparation");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let queued = input(0, 0, CallbackEvent::Boot);
        let prepared = prepare(queued.clone());
        let rows = vec![
            WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackQueued { input: queued },
            ),
            WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackPrepared {
                    input: prepared.clone(),
                },
            ),
        ];
        for row in &rows {
            crate::testpath::append_history(&mut wal, &path, row).unwrap();
        }
        let (state, pages) = CallbackPages::replay(&rows, 2, 1).unwrap();
        let mut base = base().await;
        restate(&mut base, &state, &pages);
        wal.rotate(&base).unwrap();
        drop(wal);
        let (mut wal, rows) = engine_wal::open_current(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let reader = wal.callback_reader().unwrap().unwrap();
        let (mut state, mut pages) =
            CallbackPages::replay(&rows, 2, reader.start().segment).unwrap();
        pages.attach(reader);
        pages.start_load(&state, &std::collections::BTreeSet::from([StrategyId(0)]));
        let completion = pages.completed.recv().await.unwrap();
        pages.returned(completion, &mut state, 0).unwrap();
        assert_eq!(state.inputs[&0], prepared);
        let committed = process(&prepared);
        crate::testpath::append_history(
            &mut wal,
            &path,
            &WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued {
                    input_id: 0,
                    transition: None,
                    process: committed.clone(),
                },
            ),
        )
        .unwrap();
        wal.barrier().unwrap();
        drop(wal);
        let (mut wal, rows) = engine_wal::open_current(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let (state, pages) = CallbackPages::replay(&rows, 2, 2).unwrap();
        assert!(
            pages.slots.is_empty(),
            "completed callback was queued again after restart"
        );
        assert_eq!(state.committed[&StrategyId(0)], committed);
        restate(&mut base, &state, &pages);
        wal.rotate(&base).unwrap();
        drop(wal);
        let (_, rows) = engine_wal::open_current(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let (state, pages) = CallbackPages::replay(&rows, 2, 3).unwrap();
        assert_eq!(state.next_id, 1);
        assert!(pages.slots.is_empty());
    }
}
