//! Bounded memory of venue execution ids used by fill recovery.

use std::collections::{HashSet, VecDeque};
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

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Full {
    capacity: usize,
    retention_ms: i64,
}

impl std::fmt::Display for Full {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "execution-id dedup reached its {}-id cap inside the {} ms recovery window",
            self.capacity, self.retention_ms
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
}

impl ExecutionIds {
    pub(crate) fn from_records(records: &[WalRecord], now_ms: i64) -> Result<Self, Full> {
        let mut entries = Vec::new();
        for record in records {
            match record {
                WalRecord::SegmentBase {
                    recent_execution_ids,
                    ..
                } => {
                    entries.clear();
                    entries.extend(
                        recent_execution_ids
                            .iter()
                            .map(|row| (row.seen_ms, row.exec_id.clone())),
                    );
                }
                WalRecord::RecoveredFill {
                    exec_id,
                    recovered_wall_ts_ms,
                    ..
                } if !exec_id.is_empty() => entries.push((*recovered_wall_ts_ms, exec_id.clone())),
                WalRecord::OrderUpdate {
                    update:
                        engine_types::OrderUpdate::Fill {
                            exec_id,
                            venue_ts_ms,
                            ..
                        },
                } if !exec_id.is_empty() => entries.push((*venue_ts_ms, exec_id.clone())),
                _ => {}
            }
        }
        entries.sort_by_key(|(seen_ms, _)| *seen_ms);

        let mut ids = Self::with_limits(CAPACITY, RETENTION_MS);
        let cutoff = now_ms.saturating_sub(ids.retention_ms);
        for (seen_ms, exec_id) in entries {
            let seen_ms = seen_ms.min(now_ms);
            if seen_ms < cutoff || ids.ids.contains(exec_id.as_str()) {
                continue;
            }
            if ids.ids.len() >= ids.capacity {
                return Err(ids.full());
            }
            ids.insert_at(exec_id, seen_ms);
        }
        ids.newest_ms = ids.newest_ms.max(now_ms);
        Ok(ids)
    }

    /// False means the id is already present. True reserves no state; the
    /// caller writes the fill first, then calls [`ExecutionIds::insert`].
    pub(crate) fn can_insert(&mut self, exec_id: &str, now_ms: i64) -> Result<bool, Full> {
        if self.contains(exec_id, now_ms) {
            return Ok(false);
        }
        if self.ids.len() >= self.capacity {
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
            self.ids.remove(exec_id.as_ref());
        }
    }

    fn full(&self) -> Full {
        Full {
            capacity: self.capacity,
            retention_ms: self.retention_ms,
        }
    }

    pub(crate) fn with_limits(capacity: usize, retention_ms: i64) -> Self {
        Self {
            ids: HashSet::new(),
            oldest_first: VecDeque::new(),
            newest_ms: i64::MIN,
            capacity,
            retention_ms,
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
            open_orders: Vec::new(),
        };
        let mut restored = ExecutionIds::from_records(&[record], 20).unwrap();
        assert_eq!(restored.can_insert("kept", 30), Ok(false));
        assert_eq!(restored.can_insert("new", 30), Ok(true));
    }
}
