//! Bounded memory of venue execution ids used by fill recovery.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;

use engine_types::{RecentExecutionId, WalRecord};

/// Longest execution-history request the Bybit adapter can serve.
pub(crate) const RECOVERY_REACH_MS: i64 = 7 * 86_400_000;
/// Clock and boundary overlap applied to consecutive recovery requests.
pub(crate) const RECOVERY_PAD_MS: i64 = 120_000;

/// Bybit serves about seven days of executions. The extra two minutes match
/// the overlap on each recovery request, so an id cannot expire while the
/// venue can still return it in a requested window.
pub(crate) const RETENTION_MS: i64 = RECOVERY_REACH_MS + RECOVERY_PAD_MS;

/// Just over 104 executions a minute for the full retention window. Reaching
/// this stops the engine instead of evicting an id that could still prevent a
/// duplicate fill.
pub(crate) const CAPACITY: usize = 1 << 20;
pub(crate) const ID_BYTES: usize = 64 * 1024 * 1024;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Full {
    capacity: usize,
    retention_ms: i64,
    byte_capacity: usize,
}

impl std::fmt::Display for Full {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "execution-id dedup reached its {}-id / {}-byte cap inside the {} ms recovery window",
            self.capacity, self.byte_capacity, self.retention_ms
        )
    }
}

#[derive(Debug)]
pub(crate) struct ExecutionIds {
    ids: HashSet<Arc<str>>,
    oldest_first: VecDeque<(i64, Arc<str>)>,
    newest_ms: i64,
    capacity: usize,
    retention_ms: i64,
    byte_capacity: usize,
    retained_bytes: usize,
}

impl ExecutionIds {
    pub(crate) fn from_records(records: &[WalRecord], now_ms: i64) -> Result<Self, Full> {
        Self::restore(records, now_ms, Self::with_limits(CAPACITY, RETENTION_MS))
    }

    fn restore(records: &[WalRecord], now_ms: i64, mut ids: Self) -> Result<Self, Full> {
        let mut entries: HashMap<Arc<str>, (i64, usize)> = HashMap::new();
        let mut ordinal = 0usize;
        let cutoff = now_ms.saturating_sub(ids.retention_ms);
        let mut remember = |exec_id: &str, seen_ms: i64| -> Result<(), Full> {
            let position = ordinal;
            ordinal += 1;
            let seen_ms = seen_ms.min(now_ms);
            if seen_ms < cutoff {
                return Ok(());
            }
            if let Some(prior) = entries.get_mut(exec_id) {
                if seen_ms < prior.0 {
                    *prior = (seen_ms, position);
                }
            } else {
                if entries.len() >= ids.capacity
                    || exec_id.len() > ids.byte_capacity.saturating_sub(ids.retained_bytes)
                {
                    return Err(ids.full());
                }
                ids.retained_bytes += exec_id.len();
                entries.insert(Arc::from(exec_id), (seen_ms, position));
            }
            Ok(())
        };
        let start = records
            .iter()
            .rposition(|record| matches!(record, WalRecord::SegmentBase { .. }))
            .unwrap_or(0);
        for record in &records[start..] {
            match record {
                WalRecord::SegmentBase {
                    recent_execution_ids,
                    ..
                } => {
                    for row in recent_execution_ids {
                        remember(&row.exec_id, row.seen_ms)?;
                    }
                }
                WalRecord::RecoveredFill {
                    exec_id,
                    recovered_wall_ts_ms,
                    ..
                } if !exec_id.is_empty() => remember(exec_id, *recovered_wall_ts_ms)?,
                WalRecord::OrderUpdate {
                    update:
                        engine_types::OrderUpdate::Fill {
                            exec_id,
                            venue_ts_ms,
                            ..
                        },
                    ..
                } if !exec_id.is_empty() => remember(exec_id, *venue_ts_ms)?,
                _ => {}
            }
        }
        let mut entries: Vec<_> = entries.into_iter().collect();
        entries.sort_unstable_by_key(|(_, key)| *key);
        for (exec_id, (seen_ms, _)) in entries {
            ids.ids.insert(Arc::clone(&exec_id));
            ids.oldest_first.push_back((seen_ms, exec_id));
        }
        ids.newest_ms = now_ms;
        Ok(ids)
    }

    /// False means the id is already present. True reserves no state; the
    /// caller writes the fill first, then calls [`ExecutionIds::insert`].
    pub(crate) fn can_insert(&mut self, exec_id: &str, now_ms: i64) -> Result<bool, Full> {
        if self.contains(exec_id, now_ms) {
            return Ok(false);
        }
        if self.ids.len() >= self.capacity
            || exec_id.len() > self.byte_capacity.saturating_sub(self.retained_bytes)
        {
            return Err(self.full());
        }
        Ok(true)
    }

    pub(crate) fn contains(&mut self, exec_id: &str, now_ms: i64) -> bool {
        self.prune(now_ms);
        self.ids.contains(exec_id)
    }

    pub(crate) fn insert(&mut self, exec_id: String, now_ms: i64) {
        debug_assert!(!self.ids.contains(exec_id.as_str()));
        debug_assert!(self.ids.len() < self.capacity);
        let seen_ms = self.newest_ms.max(now_ms);
        self.insert_at(exec_id, seen_ms);
    }

    pub(crate) fn rows(&self, now_ms: i64) -> Vec<RecentExecutionId> {
        let cutoff = now_ms.saturating_sub(self.retention_ms);
        self.oldest_first
            .iter()
            .filter(|(seen_ms, _)| *seen_ms >= cutoff)
            .map(|(seen_ms, exec_id)| RecentExecutionId {
                exec_id: exec_id.to_string(),
                seen_ms: *seen_ms,
            })
            .collect()
    }

    fn insert_at(&mut self, exec_id: String, seen_ms: i64) {
        self.retained_bytes += exec_id.len();
        let exec_id: Arc<str> = Arc::from(exec_id);
        self.ids.insert(Arc::clone(&exec_id));
        self.oldest_first.push_back((seen_ms, exec_id));
        self.newest_ms = self.newest_ms.max(seen_ms);
    }

    fn prune(&mut self, now_ms: i64) {
        self.newest_ms = self.newest_ms.max(now_ms);
        let cutoff = self.newest_ms.saturating_sub(self.retention_ms);
        while self
            .oldest_first
            .front()
            .is_some_and(|(seen_ms, _)| *seen_ms < cutoff)
        {
            let (_, exec_id) = self.oldest_first.pop_front().expect("front exists");
            self.retained_bytes -= exec_id.len();
            self.ids.remove(exec_id.as_ref());
        }
    }

    fn full(&self) -> Full {
        Full {
            capacity: self.capacity,
            retention_ms: self.retention_ms,
            byte_capacity: self.byte_capacity,
        }
    }

    pub(crate) fn with_limits(capacity: usize, retention_ms: i64) -> Self {
        Self {
            ids: HashSet::new(),
            oldest_first: VecDeque::new(),
            newest_ms: i64::MIN,
            capacity,
            retention_ms,
            byte_capacity: ID_BYTES,
            retained_bytes: 0,
        }
    }

    pub(crate) fn len(&self) -> usize {
        self.ids.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn remember(ids: &mut ExecutionIds, exec_id: &str, now_ms: i64) -> Result<bool, Full> {
        let new = ids.can_insert(exec_id, now_ms)?;
        if new {
            ids.insert(exec_id.to_string(), now_ms);
        }
        Ok(new)
    }

    #[test]
    fn duplicates_stay_duplicates_for_the_whole_window() {
        let mut ids = ExecutionIds::with_limits(2, 100);
        assert_eq!(remember(&mut ids, "a", 10), Ok(true));
        assert_eq!(remember(&mut ids, "a", 110), Ok(false));
        assert_eq!(ids.rows(110).len(), 1);
    }

    #[test]
    fn variable_length_ids_cannot_exceed_the_resident_byte_budget() {
        let mut ids = ExecutionIds::with_limits(1000, 100);
        ids.byte_capacity = 16;
        assert_eq!(remember(&mut ids, "123456789", 10), Ok(true));
        assert_eq!(remember(&mut ids, "123456789", 20), Ok(false));
        assert!(remember(&mut ids, "abcdefgh", 20).is_err());
        assert_eq!(ids.retained_bytes, 9);
        assert_eq!(remember(&mut ids, "abcdefgh", 111), Ok(true));
        assert_eq!(ids.retained_bytes, 8);
    }

    #[test]
    fn replay_bounds_only_live_ids_and_retains_earliest_duplicate_timestamp() {
        let fill = |id: &str, time| WalRecord::OrderUpdate {
            callbacks: None,
            update: engine_types::OrderUpdate::Fill {
                client_order_id: "order".into(),
                exec_id: id.into(),
                allocation: None,
                symbol: engine_types::SymbolId(0),
                side: engine_types::Side::Buy,
                qty: 1.0,
                px: 1.0,
                fee: Some(0.0),
                is_maker: false,
                venue_ts_ms: time,
                amounts: None,
                forced_close: None,
                recv_ns: 1,
            },
        };
        let records = vec![
            fill("expired enormous id", 0),
            fill("b", 90),
            fill("a", 95),
            fill("a", 50),
        ];
        let mut limits = ExecutionIds::with_limits(2, 100);
        limits.byte_capacity = 2;
        let mut ids = ExecutionIds::restore(&records, 101, limits).unwrap();
        assert_eq!(
            ids.rows(101)
                .iter()
                .map(|row| (&*row.exec_id, row.seen_ms))
                .collect::<Vec<_>>(),
            vec![("a", 50), ("b", 90)]
        );
        assert!(!ids.contains("a", 151));
        assert!(ids.contains("b", 151));
        let mut limits = ExecutionIds::with_limits(1000, 100);
        limits.byte_capacity = 1;
        assert!(ExecutionIds::restore(&records, 101, limits).is_err());
    }

    #[test]
    fn ids_expire_only_after_the_window() {
        let mut ids = ExecutionIds::with_limits(2, 100);
        assert_eq!(remember(&mut ids, "a", 10), Ok(true));
        assert_eq!(remember(&mut ids, "b", 50), Ok(true));
        assert_eq!(remember(&mut ids, "c", 111), Ok(true));
        let rows = ids.rows(111);
        assert_eq!(
            rows.iter()
                .map(|row| row.exec_id.as_str())
                .collect::<Vec<_>>(),
            vec!["b", "c"]
        );
    }

    #[test]
    fn capacity_refuses_instead_of_forgetting_a_live_id() {
        let mut ids = ExecutionIds::with_limits(2, 100);
        assert_eq!(remember(&mut ids, "a", 10), Ok(true));
        assert_eq!(remember(&mut ids, "b", 20), Ok(true));
        assert_eq!(remember(&mut ids, "a", 30), Ok(false));
        assert_eq!(remember(&mut ids, "c", 30), Err(ids.full()));
        assert_eq!(
            ids.rows(30)
                .iter()
                .map(|row| row.exec_id.as_str())
                .collect::<Vec<_>>(),
            vec!["a", "b"]
        );
    }

    #[test]
    fn a_rotation_base_restores_duplicate_memory() {
        let record = WalRecord::SegmentBase {
            order_id_epoch_ms: None,
            open_trade_lots: Some(Vec::new()),
            legacy_signal_source_retirements: Vec::new(),
            portfolio_control: Default::default(),
            pending_order_dispatches: Vec::new(),
            signal_producers: Vec::new(),
            identities: None,
            instrument_catalog: None,
            signal_suspensions: Vec::new(),
            portfolio: Some(Default::default()),
            strategy_processes: Vec::new(),
            strategy_callback_queues: Vec::new(),
            strategy_callback_sources: Vec::new(),
            signal_callback_deliveries: Vec::new(),
            strategy_callbacks: Vec::new(),
            wall_ts_ms: 20,
            strategies: Vec::new(),
            symbols: Vec::new(),
            may_open: true,
            control_anchors: Vec::new(),
            attribution: Vec::new(),
            logged_exposure: Vec::new(),
            intended_stops: Vec::new(),
            recent_execution_ids: vec![RecentExecutionId {
                exec_id: "kept".to_string(),
                seen_ms: 10,
            }],
            execution_history_through_ms: Some(20),
            target_book_latches: Vec::new(),
            strategy_checkpoints: Vec::new(),
            strategy_global_checkpoints: Vec::new(),
            strategy_events: Vec::new(),
            signal_observations: Vec::new(),
            signal_cursors: Vec::new(),
            signal_subscriptions: Vec::new(),
            signal_gaps: Vec::new(),
            strategy_effects: Default::default(),
            runtime_control_requests: Vec::new(),
            runtime_control_consumed: Vec::new(),
            open_orders: Vec::new(),
            rolling_loss_rows: Vec::new(),
        };
        let mut restored = ExecutionIds::from_records(&[record], 20).unwrap();
        assert_eq!(restored.can_insert("kept", 30), Ok(false));
        assert_eq!(restored.can_insert("new", 30), Ok(true));
    }
}
