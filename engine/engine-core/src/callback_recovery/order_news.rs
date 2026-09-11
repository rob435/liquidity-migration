use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use engine_types::strategy_process::{
    CallbackOrderOrigin, CallbackWalCursor, CallbackWalReader, CallbackWalRecord,
};
use engine_types::{OrderUpdate, StrategyId, WalError, WalRecord};

pub struct ReadCompletion {
    reader: Box<dyn CallbackWalReader>,
    pub strategy: StrategyId,
    pub cursor: CallbackWalCursor,
    pub result: Result<Option<CallbackWalRecord>, WalError>,
}

pub struct OrderNews {
    reader: Option<Box<dyn CallbackWalReader>>,
    start: Option<CallbackWalCursor>,
    cursors: BTreeMap<StrategyId, CallbackWalCursor>,
    latest: BTreeMap<StrategyId, CallbackOrderOrigin>,
    through: BTreeMap<StrategyId, CallbackOrderOrigin>,
    retry_after: BTreeMap<StrategyId, Instant>,
    last_read: Option<StrategyId>,
    pending: bool,
    pub completed: tokio::sync::mpsc::Receiver<ReadCompletion>,
    completion: tokio::sync::mpsc::Sender<ReadCompletion>,
}

impl Default for OrderNews {
    fn default() -> Self {
        let (completion, completed) = tokio::sync::mpsc::channel(1);
        Self {
            reader: None,
            start: None,
            cursors: BTreeMap::new(),
            latest: BTreeMap::new(),
            through: BTreeMap::new(),
            retry_after: BTreeMap::new(),
            last_read: None,
            pending: false,
            completed,
            completion,
        }
    }
}

impl OrderNews {
    pub fn attach(
        &mut self,
        reader: Box<dyn CallbackWalReader>,
        replayed: &crate::assembly::BootReplay<'_>,
        strategy_count: usize,
    ) -> Result<(), String> {
        if self.pending {
            return Err("cannot replace an owned callback WAL read".into());
        }
        *self = Self::default();
        self.start = Some(reader.start());
        self.reader = Some(reader);
        for record in replayed.iter() {
            if let WalRecord::SegmentBase {
                strategy_callback_sources,
                ..
            } = record
            {
                for source in strategy_callback_sources {
                    if source.strategy.idx() >= strategy_count
                        || source.cursor.segment == 0
                        || source.cursor.sequence == 0
                        || source.latest.segment == 0
                        || source.latest.sequence == 0
                        || source
                            .accepted
                            .is_some_and(|accepted| accepted > source.latest)
                    {
                        return Err("invalid callback source frontier".into());
                    }
                    self.cursors.insert(source.strategy, source.cursor);
                    self.latest.insert(source.strategy, source.latest);
                    if let Some(accepted) = source.accepted {
                        self.through.insert(source.strategy, accepted);
                    }
                }
            }
        }
        for (index, record) in replayed.iter().enumerate() {
            let owners = match record {
                WalRecord::OrderUpdate {
                    callbacks: Some(owners),
                    ..
                } => owners.clone(),
                WalRecord::RecoveredFill {
                    callbacks: Some(callbacks),
                    ..
                } => callbacks.owners.clone(),
                WalRecord::StrategyCallbackSource { strategy, .. } => vec![*strategy],
                _ => continue,
            };
            {
                if owners.iter().any(|owner| owner.idx() >= strategy_count) {
                    return Err("order callback source escapes configured owners".into());
                }
                self.record(replayed.sequence(index), &owners)?;
            }
        }
        for record in replayed.iter() {
            if let WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackQueued { input },
            ) = record
            {
                if let Some(origin) = input.order_origin {
                    if origin.segment == 0
                        || origin.sequence == 0
                        || Some(origin.segment) > self.start.map(|start| start.segment)
                    {
                        return Err(
                            "callback source origin names a future or invalid segment".into()
                        );
                    }
                    if Some(origin.segment) == self.start.map(|start| start.segment) {
                        let source = replayed
                            .by_sequence(origin.sequence)
                            .and_then(|index| replayed.get(index))
                            .ok_or("callback order origin has no parent frame")?;
                        let expected = match source {
                            WalRecord::OrderUpdate {
                                update,
                                callbacks: Some(owners),
                            } if owners.contains(&input.strategy) => {
                                engine_types::strategy_process::CallbackEvent::Order {
                                    update: Self::slice(update, input.strategy)?,
                                }
                            }
                            WalRecord::RecoveredFill {
                                callbacks: Some(callbacks),
                                ..
                            } if callbacks.owners.contains(&input.strategy) => {
                                let (_, update) = source
                                    .recovered_callback()
                                    .expect("recorded callback source");
                                engine_types::strategy_process::CallbackEvent::Order {
                                    update: Self::slice(&update, input.strategy)?,
                                }
                            }
                            WalRecord::StrategyCallbackSource {
                                strategy, event, ..
                            } if *strategy == input.strategy => event.clone(),
                            _ => {
                                return Err(
                                    "callback source origin has no matching durable owner".into()
                                )
                            }
                        };
                        if input.event != expected {
                            return Err("callback view differs from its durable parent".into());
                        }
                    }
                    self.accepted(input.strategy, origin);
                }
            }
        }
        Ok(())
    }

    pub fn slice(update: &OrderUpdate, strategy: StrategyId) -> Result<OrderUpdate, String> {
        match crate::portfolio_allocation::slice_updates(update)? {
            Some(slices) => slices
                .into_iter()
                .find_map(|(owner, update)| (owner == strategy).then_some(update))
                .ok_or_else(|| {
                    "allocated execution has no callback slice for its recorded owner".into()
                }),
            None => Ok(update.clone()),
        }
    }

    pub fn origin(&self, sequence: u64) -> Result<CallbackOrderOrigin, String> {
        Ok(CallbackOrderOrigin {
            segment: self
                .start
                .ok_or("retained callbacks have no WAL source reader")?
                .segment,
            sequence,
        })
    }

    pub fn record(&mut self, sequence: u64, owners: &[StrategyId]) -> Result<(), String> {
        self.record_at(sequence, 0, owners)
    }

    pub fn record_at(
        &mut self,
        sequence: u64,
        offset: u64,
        owners: &[StrategyId],
    ) -> Result<(), String> {
        if sequence == 0 {
            return Err("order callback source has zero sequence".into());
        }
        let origin = self.origin(sequence)?;
        for owner in owners {
            if !self.unread_for(*owner)
                && self.latest.get(owner).is_none_or(|latest| origin > *latest)
            {
                // Only a caught-up owner may skip straight to its new source.
                self.cursors.insert(
                    *owner,
                    CallbackWalCursor {
                        segment: origin.segment,
                        sequence,
                        offset,
                    },
                );
            }
            self.latest
                .entry(*owner)
                .and_modify(|known| *known = (*known).max(origin))
                .or_insert(origin);
        }
        Ok(())
    }

    pub fn unread_for(&self, strategy: StrategyId) -> bool {
        self.latest.get(&strategy) > self.through.get(&strategy)
    }

    pub fn has_reader(&self) -> bool {
        self.start.is_some()
    }
    pub fn source_due(&self, strategy: StrategyId, origin: CallbackOrderOrigin) -> bool {
        Some(&origin) > self.through.get(&strategy)
    }
    pub fn pending(&self) -> bool {
        self.pending
    }
    pub fn unread(&self) -> bool {
        self.pending || self.latest.keys().any(|owner| self.unread_for(*owner))
    }

    pub fn accepted(&mut self, strategy: StrategyId, origin: CallbackOrderOrigin) {
        self.through
            .entry(strategy)
            .and_modify(|through| *through = (*through).max(origin))
            .or_insert(origin);
        self.retry_after.remove(&strategy);
    }

    pub fn snapshot(&self) -> Vec<engine_types::strategy_process::CallbackSourceFrontier> {
        self.latest
            .iter()
            .filter_map(|(strategy, latest)| {
                self.start.map(
                    |start| engine_types::strategy_process::CallbackSourceFrontier {
                        strategy: *strategy,
                        cursor: self.cursors.get(strategy).copied().unwrap_or(start),
                        accepted: self.through.get(strategy).copied(),
                        latest: *latest,
                    },
                )
            })
            .collect()
    }

    pub fn rotated(&mut self, reader: Box<dyn CallbackWalReader>) -> Result<(), String> {
        if self.pending {
            return Err("cannot rotate an owned callback source read".into());
        }
        if let Some(start) = self.start {
            for owner in self.latest.keys() {
                self.cursors.entry(*owner).or_insert(start);
            }
        }
        self.start = Some(reader.start());
        self.reader = Some(reader);
        Ok(())
    }

    pub fn start_read(&mut self) {
        self.start_read_for(|_| true);
    }

    pub fn start_read_for(&mut self, allowed: impl Fn(StrategyId) -> bool) {
        if self.pending || self.reader.is_none() {
            return;
        }
        let now = Instant::now();
        let mut owners: Vec<_> = self
            .latest
            .keys()
            .copied()
            .filter(|owner| {
                allowed(*owner)
                    && self.unread_for(*owner)
                    && self
                        .retry_after
                        .get(owner)
                        .is_none_or(|after| *after <= now)
            })
            .collect();
        if let Some(previous) = self.last_read {
            let next = owners.partition_point(|owner| *owner <= previous);
            owners.rotate_left(next);
        }
        let Some(strategy) = owners.first().copied() else {
            return;
        };
        let cursor = self
            .cursors
            .get(&strategy)
            .copied()
            .or(self.start)
            .expect("configured callback reader");
        let mut reader = self.reader.take().expect("available callback reader");
        self.pending = true;
        self.last_read = Some(strategy);
        let completed = self.completion.clone();
        tokio::task::spawn_blocking(move || {
            let result = reader.next(cursor);
            let _ = completed.blocking_send(ReadCompletion {
                reader,
                strategy,
                cursor,
                result,
            });
        });
    }

    pub fn returned(
        &mut self,
        completion: ReadCompletion,
    ) -> Result<(StrategyId, CallbackWalCursor, CallbackWalRecord), String> {
        if !self.pending || self.reader.is_some() {
            return Err("callback WAL read completion has no owner".into());
        }
        self.pending = false;
        self.reader = Some(completion.reader);
        let record = completion
            .result
            .map_err(|error| error.to_string())?
            .ok_or("callback source disappeared before its recorded frontier")?;
        Ok((completion.strategy, record.cursor, record))
    }

    pub fn advance(&mut self, strategy: StrategyId, cursor: CallbackWalCursor) {
        self.cursors.insert(strategy, cursor);
    }
    pub fn refused(&mut self, strategy: StrategyId) {
        self.retry_after
            .insert(strategy, Instant::now() + Duration::from_secs(1));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::strategy_process::{
        CallbackEvent, CallbackPreparation, StrategyCallbackInput,
    };
    use engine_types::{Side, SymbolId, Wal};

    #[tokio::test(start_paused = true)]
    async fn a_restart_between_slice_admissions_replays_only_the_other_durable_owner() {
        slice_restart(false).await;
    }

    #[tokio::test(start_paused = true)]
    async fn recovered_parent_restarts_before_either_slice_and_between_slice_admissions() {
        slice_restart(true).await;
    }

    async fn slice_restart(recovered: bool) {
        let path = crate::testpath::temp_path("callback-source-restart");
        let parent = OrderUpdate::Fill {
            allocation: Some(Box::new(
                engine_types::execution_allocation::ExecutionAllocation {
                    policy: engine_types::execution_allocation::AllocationPolicy::EmergencyNetFifo,
                    legacy_quantity_step: None,
                    slices: [(0, "a", "0.25"), (1, "b", "0.75")]
                        .into_iter()
                        .map(|(strategy, key, qty)| {
                            engine_types::execution_allocation::ExecutionSlice {
                                strategy: StrategyId(strategy),
                                strategy_key: key.into(),
                                quantity: qty.parse().unwrap(),
                                fee: None,
                            }
                        })
                        .collect(),
                },
            )),
            amounts: None,
            exec_id: "shared-private-parent".into(),
            client_order_id: String::new(),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 1.0,
            px: 100.0,
            fee: None,
            is_maker: false,
            forced_close: Some(engine_types::ForcedClose::StopLoss),
            venue_ts_ms: 1,
            recv_ns: 1,
        };
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let source = if recovered {
            let OrderUpdate::Fill {
                allocation,
                amounts,
                exec_id,
                client_order_id,
                symbol,
                side,
                qty,
                px,
                fee,
                is_maker,
                forced_close,
                venue_ts_ms,
                recv_ns,
            } = &parent
            else {
                unreachable!()
            };
            WalRecord::RecoveredFill {
                callbacks: Some(engine_types::wal::RecoveredCallbacks {
                    owners: vec![StrategyId(0), StrategyId(1)],
                    recv_ns: *recv_ns,
                }),
                allocation: allocation.clone(),
                amounts: amounts.as_deref().cloned(),
                exec_id: exec_id.clone(),
                client_order_id: client_order_id.clone(),
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                fee: *fee,
                is_maker: *is_maker,
                forced_close: *forced_close,
                venue_ts_ms: *venue_ts_ms,
                recovered_wall_ts_ms: 2,
            }
        } else {
            WalRecord::OrderUpdate {
                callbacks: Some(vec![StrategyId(0), StrategyId(1)]),
                update: parent.clone(),
            }
        };
        crate::testpath::append_history(&mut wal, &path, &source).unwrap();
        wal.barrier().unwrap();
        drop(wal);
        let (mut wal, rows) = engine_wal::WalWriter::open(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let mut news = OrderNews::default();
        news.attach(
            wal.callback_reader().unwrap().unwrap(),
            &crate::assembly::BootReplay::dense(&rows),
            2,
        )
        .unwrap();
        assert!(news.unread_for(StrategyId(0)) && news.unread_for(StrategyId(1)));
        news.start_read();
        let completion = news.completed.recv().await.unwrap();
        let (owner, cursor, record) = news.returned(completion).unwrap();
        assert_eq!(owner, StrategyId(0));
        let (owners, event) = record.source.unwrap();
        assert_eq!(owners, [StrategyId(0), StrategyId(1)]);
        let CallbackEvent::Order { update } = event else {
            unreachable!()
        };
        let view = OrderNews::slice(&update, owner).unwrap();
        assert!(matches!(view, OrderUpdate::Fill { qty, .. } if qty == 0.25));
        let origin = news.origin(cursor.sequence).unwrap();
        let input = StrategyCallbackInput {
            callback_id: 1,
            strategy: owner,
            order_origin: Some(origin),
            event: CallbackEvent::Order { update: view },
            preparation: CallbackPreparation::Queued,
        };
        crate::testpath::append_history(
            &mut wal,
            &path,
            &WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackQueued { input },
            ),
        )
        .unwrap();
        wal.barrier().unwrap();
        drop(news);
        drop(wal);
        let (mut wal, rows) = engine_wal::WalWriter::open(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let mut restored = OrderNews::default();
        restored
            .attach(
                wal.callback_reader().unwrap().unwrap(),
                &crate::assembly::BootReplay::dense(&rows),
                2,
            )
            .unwrap();
        assert!(
            !restored.unread_for(StrategyId(0)),
            "committed callback admission was duplicated after restart"
        );
        assert!(
            restored.unread_for(StrategyId(1)),
            "one slice admission erased the other owner's input"
        );
        restored.start_read();
        let completion = restored.completed.recv().await.unwrap();
        let (owner, _, record) = restored.returned(completion).unwrap();
        assert_eq!(owner, StrategyId(1));
        let CallbackEvent::Order { update } = record.source.unwrap().1 else {
            unreachable!()
        };
        let view = OrderNews::slice(&update, owner).unwrap();
        assert!(
            matches!(view, OrderUpdate::Fill { qty, fee: None, allocation: None, .. } if qty == 0.75)
        );
        assert_eq!(
            rows.iter()
                .filter(|row| matches!(
                    row,
                    WalRecord::OrderUpdate { .. } | WalRecord::RecoveredFill { .. }
                ))
                .count(),
            1
        );
        let mut wrong = rows.clone();
        let WalRecord::Retained(engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
            input,
        }) = &mut wrong[1]
        else {
            unreachable!()
        };
        input.event = CallbackEvent::Order { update: parent };
        let mut refused = OrderNews::default();
        assert!(
            refused
                .attach(
                    wal.callback_reader().unwrap().unwrap(),
                    &crate::assembly::BootReplay::dense(&wrong),
                    2,
                )
                .is_err(),
            "whole-parent substitution silently changed a durable sleeve view"
        );
    }
}

#[cfg(test)]
mod paging_tests {
    use super::*;
    use engine_types::strategy_process::{
        CallbackEvent, CallbackPreparation, StrategyCallbackInput,
    };
    use engine_types::Wal;

    #[tokio::test(start_paused = true)]
    async fn unread_inactive_sources_survive_rotation_without_stalling_active_order_news() {
        let path = crate::testpath::temp_path("inactive-callback-source-rotation");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let event = |reason: &str| CallbackEvent::IntentRefused {
            symbol: engine_types::SymbolId(0),
            reduce_only: true,
            reason: reason.into(),
        };
        let rows = vec![
            WalRecord::StrategyCallbackSource {
                strategy: StrategyId(0),
                placement: None,
                event: event("paused-owner"),
            },
            WalRecord::StrategyCallbackSource {
                strategy: StrategyId(1),
                placement: None,
                event: event("active-owner"),
            },
        ];
        for row in &rows {
            crate::testpath::append_history(&mut wal, &path, row).unwrap();
        }
        let mut news = OrderNews::default();
        news.attach(
            wal.callback_reader().unwrap().unwrap(),
            &crate::assembly::BootReplay::dense(&rows),
            2,
        )
        .unwrap();
        loop {
            news.start_read_for(|strategy| strategy == StrategyId(1));
            let completion = news.completed.recv().await.unwrap();
            let (owner, cursor, record) = news.returned(completion).unwrap();
            assert_eq!(owner, StrategyId(1));
            if let Some((owners, source)) = record.source {
                if owners.contains(&owner) {
                    assert_eq!(source, event("active-owner"));
                    let origin = CallbackOrderOrigin {
                        segment: cursor.segment,
                        sequence: cursor.sequence,
                    };
                    crate::testpath::append_history(
                        &mut wal,
                        &path,
                        &WalRecord::Retained(
                            engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
                                input: StrategyCallbackInput {
                                    callback_id: 0,
                                    strategy: owner,
                                    order_origin: Some(origin),
                                    event: source,
                                    preparation: CallbackPreparation::Queued,
                                },
                            },
                        ),
                    )
                    .unwrap();
                    news.accepted(owner, origin);
                }
            }
            news.advance(owner, record.next);
            if !news.unread_for(owner) {
                break;
            }
        }
        assert!(news.unread_for(StrategyId(0)));
        assert!(!news.unread_for(StrategyId(1)));
        let params = toml::from_str("symbol='BTCUSDT'\nevery_s=60\nenabled=false").unwrap();
        let strategies = (0..2)
            .map(|id| engine_strategies::build_strategy("probe", StrategyId(id), &params).unwrap())
            .collect();
        let (engine, _) = crate::tests::callback_test_fixture(strategies).await;
        let mut base = engine.rotation_base(1);
        let WalRecord::SegmentBase {
            strategy_callback_sources,
            ..
        } = &mut base
        else {
            unreachable!()
        };
        *strategy_callback_sources = news.snapshot();
        wal.rotate(&base).unwrap();
        news.rotated(wal.callback_reader().unwrap().unwrap())
            .unwrap();
        assert!(
            news.unread_for(StrategyId(0)),
            "rotation dropped a paused owner's durable source"
        );
        drop(news);
        drop(wal);
        let (mut wal, rows) = engine_wal::open_current(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let mut news = OrderNews::default();
        news.attach(
            wal.callback_reader().unwrap().unwrap(),
            &crate::assembly::BootReplay::dense(&rows),
            2,
        )
        .unwrap();
        assert!(news.unread_for(StrategyId(0)));
        assert!(
            !news.unread_for(StrategyId(1)),
            "restart duplicated the admitted active owner's source"
        );
        news.start_read_for(|strategy| strategy == StrategyId(0));
        let completion = news.completed.recv().await.unwrap();
        let (owner, cursor, record) = news.returned(completion).unwrap();
        assert_eq!(owner, StrategyId(0));
        assert_eq!(cursor.segment, 1);
        assert_eq!(record.source.unwrap(), (vec![owner], event("paused-owner")));
        let origin = CallbackOrderOrigin {
            segment: cursor.segment,
            sequence: cursor.sequence,
        };
        crate::testpath::append_history(
            &mut wal,
            &path,
            &WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
                    input: StrategyCallbackInput {
                        callback_id: 1,
                        strategy: owner,
                        order_origin: Some(origin),
                        event: event("paused-owner"),
                        preparation: CallbackPreparation::Queued,
                    },
                },
            ),
        )
        .unwrap();
        wal.barrier().unwrap();
        drop(wal);
        let (mut wal, rows) = engine_wal::open_current(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let mut news = OrderNews::default();
        news.attach(
            wal.callback_reader().unwrap().unwrap(),
            &crate::assembly::BootReplay::dense(&rows),
            2,
        )
        .unwrap();
        assert!(
            !news.unread(),
            "restarting after admission repeated a paused owner's old-segment source"
        );
    }

    fn unrelated_prefix(wal: &mut engine_wal::WalWriter) {
        for _ in 0..64 {
            wal.append(&WalRecord::Note {
                source: "unrelated-history".into(),
                text: "x".repeat(16 * 1024),
            })
            .unwrap();
        }
    }

    fn ack_source(id: &str, owners: Vec<StrategyId>) -> WalRecord {
        WalRecord::OrderUpdate {
            callbacks: Some(owners),
            update: OrderUpdate::Ack(engine_types::OrderAck {
                client_order_id: id.into(),
                venue_order_id: format!("venue-{id}"),
                sent_ns: 1,
                ack_ns: 2,
            }),
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_deferred_live_source_starts_at_its_frame_after_direct_delivery() {
        let path = crate::testpath::temp_path("live-source-large-prefix");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let owner = StrategyId(0);
        let mut news = OrderNews::default();
        news.attach(
            wal.callback_reader().unwrap().unwrap(),
            &crate::assembly::BootReplay::dense(&[]),
            1,
        )
        .unwrap();
        unrelated_prefix(&mut wal);
        let first =
            crate::testpath::append_history(&mut wal, &path, &ack_source("first", vec![owner]))
                .unwrap();
        news.record(first, &[owner]).unwrap();
        news.accepted(owner, news.origin(first).unwrap());
        unrelated_prefix(&mut wal);
        let second =
            crate::testpath::append_history(&mut wal, &path, &ack_source("second", vec![owner]))
                .unwrap();
        news.record(second, &[owner]).unwrap();
        wal.flush().unwrap();
        news.start_read();
        let completion = news.completed.recv().await.unwrap();
        let (actual_owner, cursor, record) = news.returned(completion).unwrap();
        assert_eq!(actual_owner, owner);
        assert_eq!(
            cursor.sequence, second,
            "live callback scanned unrelated WAL records before its known parent"
        );
        assert!(
            matches!(record.source, Some((owners, CallbackEvent::Order { update: OrderUpdate::Ack(ack) })) if owners == vec![owner] && ack.client_order_id == "second")
        );
    }

    #[tokio::test(start_paused = true)]
    async fn caught_up_owner_moves_to_new_source_while_other_owner_keeps_first_unread() {
        let path = crate::testpath::temp_path("live-source-distinct-owner-frontiers");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let mut news = OrderNews::default();
        news.attach(
            wal.callback_reader().unwrap().unwrap(),
            &crate::assembly::BootReplay::dense(&[]),
            2,
        )
        .unwrap();
        unrelated_prefix(&mut wal);
        let owners = [StrategyId(0), StrategyId(1)];
        let first = crate::testpath::append_history(
            &mut wal,
            &path,
            &ack_source("shared-first", owners.to_vec()),
        )
        .unwrap();
        news.record(first, &owners).unwrap();
        news.accepted(owners[0], news.origin(first).unwrap());
        unrelated_prefix(&mut wal);
        let second = crate::testpath::append_history(
            &mut wal,
            &path,
            &ack_source("shared-second", owners.to_vec()),
        )
        .unwrap();
        news.record(second, &owners).unwrap();
        wal.flush().unwrap();
        for (owner, expected) in [(owners[0], second), (owners[1], first)] {
            news.start_read_for(|candidate| candidate == owner);
            let completion = news.completed.recv().await.unwrap();
            let (actual, cursor, record) = news.returned(completion).unwrap();
            assert_eq!(actual, owner);
            assert_eq!(
                cursor.sequence, expected,
                "source cursor skipped an unread owner or scanned irrelevant history"
            );
            assert!(record.source.unwrap().0.contains(&owner));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn directly_positioned_source_still_checks_its_real_frame_crc() {
        use std::os::unix::fs::FileExt;
        let path = crate::testpath::temp_path("live-source-corrupt-crc");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let owner = StrategyId(0);
        let mut news = OrderNews::default();
        news.attach(
            wal.callback_reader().unwrap().unwrap(),
            &crate::assembly::BootReplay::dense(&[]),
            1,
        )
        .unwrap();
        unrelated_prefix(&mut wal);
        let offset = wal.segment_size();
        let sequence =
            crate::testpath::append_history(&mut wal, &path, &ack_source("bad-crc", vec![owner]))
                .unwrap();
        news.record(sequence, &[owner]).unwrap();
        wal.flush().unwrap();
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap();
        let mut crc_byte = [0];
        file.read_exact_at(&mut crc_byte, offset + 4).unwrap();
        crc_byte[0] ^= 1;
        file.write_all_at(&crc_byte, offset + 4).unwrap();
        news.start_read();
        let completion = news.completed.recv().await.unwrap();
        assert!(
            matches!(completion.result, Err(WalError::Corrupt { .. })),
            "known live source bypassed validation or read unrelated history"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn live_byte_cursor_reads_the_exact_target_and_survives_a_serialized_frontier() {
        struct ExactCursorReader {
            inner: Box<dyn CallbackWalReader>,
            expected: CallbackWalCursor,
        }
        impl CallbackWalReader for ExactCursorReader {
            fn start(&self) -> CallbackWalCursor {
                self.inner.start()
            }
            fn next(
                &mut self,
                cursor: CallbackWalCursor,
            ) -> Result<Option<CallbackWalRecord>, WalError> {
                assert_eq!(
                    cursor, self.expected,
                    "live source must not locate its offset by walking old frame headers"
                );
                self.inner.next(cursor)
            }
        }
        let path = crate::testpath::temp_path("live-source-exact-offset");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let owner = StrategyId(0);
        unrelated_prefix(&mut wal);
        let offset = wal.segment_size();
        let sequence = crate::testpath::append_history(
            &mut wal,
            &path,
            &ack_source("exact-offset", vec![owner]),
        )
        .unwrap();
        let reader = wal.callback_reader().unwrap().unwrap();
        let expected = CallbackWalCursor {
            segment: reader.start().segment,
            sequence,
            offset,
        };
        let mut news = OrderNews::default();
        news.attach(
            Box::new(ExactCursorReader {
                inner: reader,
                expected,
            }),
            &crate::assembly::BootReplay::dense(&[]),
            1,
        )
        .unwrap();
        news.record_at(sequence, offset, &[owner]).unwrap();
        let encoded = serde_json::to_vec(&news.snapshot()).unwrap();
        let restored: Vec<engine_types::strategy_process::CallbackSourceFrontier> =
            serde_json::from_slice(&encoded).unwrap();
        assert_eq!(restored[0].cursor, expected);
        let params = toml::from_str("symbol='BTCUSDT'\nevery_s=60\nenabled=false").unwrap();
        let strategy = engine_strategies::build_strategy("probe", owner, &params).unwrap();
        let (engine, _) = crate::tests::callback_test_fixture(vec![strategy]).await;
        let mut base = engine.rotation_base(1);
        let WalRecord::SegmentBase {
            strategy_callback_sources,
            ..
        } = &mut base
        else {
            unreachable!()
        };
        *strategy_callback_sources = restored;
        wal.rotate(&base).unwrap();
        drop(news);
        drop(wal);
        let (mut wal, rows) = engine_wal::open_current(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let mut news = OrderNews::default();
        news.attach(
            Box::new(ExactCursorReader {
                inner: wal.callback_reader().unwrap().unwrap(),
                expected,
            }),
            &crate::assembly::BootReplay::dense(&rows),
            1,
        )
        .unwrap();
        news.start_read();
        let completion = news.completed.recv().await.unwrap();
        let (actual_owner, cursor, record) = news.returned(completion).unwrap();
        assert_eq!(actual_owner, owner);
        assert_eq!(cursor, expected);
        assert!(
            matches!(record.source, Some((owners, CallbackEvent::Order { update: OrderUpdate::Ack(ack) })) if owners == vec![owner] && ack.client_order_id == "exact-offset")
        );
    }
}
