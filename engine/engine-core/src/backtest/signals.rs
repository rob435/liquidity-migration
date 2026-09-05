//! Durable signal observations replayed at the instant each became
//! available.
//!
//! The spool is read with the live feed's own row reader, so every row is
//! validated and checked against its name exactly as the engine would check
//! it live. Delivery waits on the virtual clock for the row's
//! `available_wall_ts_ms`: an observation is never seen before the moment
//! the worker had published it.

use std::path::Path;

use engine_types::{SignalError, SignalFeed, SignalGapRequest, SignalObservation, StrategyId};

use super::scheduler::{Scheduler, WaiterKind};
use crate::signals::{
    ordered_blocked_destinations, ordered_gap_requests, signal_eligible, signal_requested,
    SpoolSignalFeed,
};

pub struct SignalReplayFeed {
    observations: Vec<Option<SignalObservation>>,
    outstanding: Option<usize>,
    ready_observation: Option<SignalObservation>,
    producer_frontiers: std::collections::BTreeMap<String, engine_types::SignalSourceFrontier>,
    gaps: Vec<SignalGapRequest>,
    blocked_destinations: Vec<StrategyId>,
    scheduler: Scheduler,
}

impl SignalReplayFeed {
    pub fn empty(scheduler: Scheduler) -> Self {
        SignalReplayFeed {
            observations: Vec::new(),
            outstanding: None,
            ready_observation: None,
            producer_frontiers: std::collections::BTreeMap::new(),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            scheduler,
        }
    }

    /// Every `.json` row under `directory`, ordered by availability then
    /// sequence. A row the live feed would refuse refuses the run.
    pub fn from_directory(directory: &Path, scheduler: Scheduler) -> Result<Self, SignalError> {
        let entries = std::fs::read_dir(directory).map_err(|error| {
            SignalError::Source(format!(
                "cannot scan signal spool {}: {error}",
                directory.display()
            ))
        })?;
        let mut paths = Vec::new();
        for entry in entries {
            let path = entry
                .map_err(|error| SignalError::Source(error.to_string()))?
                .path();
            if path.extension().is_some_and(|ext| ext == "json")
                && path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| {
                        name != engine_types::SIGNAL_READINESS_REQUEST_FILE
                            && name != engine_types::SIGNAL_READINESS_RESPONSE_FILE
                    })
            {
                paths.push(path);
            }
        }
        paths.sort();
        let mut observations = Vec::with_capacity(paths.len());
        for path in paths {
            if let Some(observation) = SpoolSignalFeed::read_one(&path)? {
                if engine_types::ManagedSignalSource::parse(&observation.source).is_some() {
                    return Err(SignalError::Source("managed signal replay requires chronological lifecycle grants and seals from the engine WAL; an observation-only directory cannot establish producer ownership".into()));
                }
                observations.push(observation);
            }
        }
        observations.sort_by_key(|o| (o.available_wall_ts_ms, o.sequence));
        Ok(SignalReplayFeed {
            observations: observations.into_iter().map(Some).collect(),
            outstanding: None,
            ready_observation: None,
            producer_frontiers: std::collections::BTreeMap::new(),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            scheduler,
        })
    }

    pub fn len(&self) -> usize {
        self.observations.len()
    }

    pub fn is_empty(&self) -> bool {
        self.observations.is_empty()
    }
}

impl SignalFeed for SignalReplayFeed {
    async fn next_event(&mut self) -> Result<engine_types::SignalFeedEvent, SignalError> {
        if let Some(observation) = self.ready_observation.take() {
            return Ok(engine_types::SignalFeedEvent::Observation(observation));
        }
        let observation = self.next_observation().await?;
        if self.producer_frontiers.contains_key(&observation.source) {
            return Ok(engine_types::SignalFeedEvent::Observation(observation));
        }
        // Replay advertises only the row which has reached its availability
        // clock. The rest of the tape cannot establish a present frontier.
        self.producer_frontiers.insert(
            observation.source.clone(),
            engine_types::SignalSourceFrontier {
                source: observation.source.clone(),
                destination: observation.destination,
                published_through: observation.sequence,
            },
        );
        self.ready_observation = Some(observation);
        Ok(engine_types::SignalFeedEvent::Ready(
            self.producer_frontiers.values().cloned().collect(),
        ))
    }

    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        let gaps = ordered_gap_requests(gaps)?;
        let blocked_destinations = ordered_blocked_destinations(blocked_destinations)?;
        self.gaps = gaps;
        self.blocked_destinations = blocked_destinations;
        Ok(())
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        let index = self
            .outstanding
            .take()
            .ok_or_else(|| SignalError::Source("signal replay has no row to acknowledge".into()))?;
        self.observations[index] = None;
        Ok(())
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        let index = self
            .outstanding
            .ok_or_else(|| SignalError::Source("signal replay has no row to defer".into()))?;
        if self.observations[index].as_ref() != Some(&observation) {
            return Err(SignalError::Source(
                "deferred signal replay row differs from delivery".into(),
            ));
        }
        self.outstanding = None;
        Ok(())
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        if self.outstanding.is_some() {
            return Err(SignalError::Source(
                "signal replay row was neither acknowledged nor deferred".into(),
            ));
        }
        let available_ns = |observation: &SignalObservation| {
            (observation.available_wall_ts_ms.max(0) as u64).saturating_mul(1_000_000)
        };
        let now_ns = self.scheduler.now_ns();
        let index = self
            .observations
            .iter()
            .position(|row| {
                row.as_ref().is_some_and(|row| {
                    available_ns(row) <= now_ns && signal_requested(&self.gaps, row)
                })
            })
            .or_else(|| {
                self.observations.iter().position(|row| {
                    row.as_ref().is_some_and(|row| {
                        signal_eligible(&self.gaps, &self.blocked_destinations, row)
                    })
                })
            });
        let Some(index) = index else {
            if self.observations.iter().any(Option::is_some) {
                return std::future::pending().await;
            }
            return Err(SignalError::Closed);
        };
        // Wall and monotonic virtual time share one origin in the replay:
        // both are the tape's receive stamp in nanoseconds since the epoch.
        let deadline = available_ns(
            self.observations[index]
                .as_ref()
                .expect("selected row exists"),
        );
        self.scheduler
            .sleep_until(deadline, WaiterKind::Signal)
            .await;
        // Advance the cursor only on the poll that returns, so a future the
        // loop dropped mid-wait loses nothing.
        let observation = self.observations[index]
            .as_ref()
            .expect("selected row exists")
            .clone();
        self.outstanding = Some(index);
        Ok(observation)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{StrategyId, SIGNAL_OBSERVATION_SCHEMA_VERSION};

    fn row(source: &str, sequence: u64, available_ms: i64) -> SignalObservation {
        let mut row = SignalObservation {
            schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: "replay".into(),
            destination: StrategyId(0),
            source: source.into(),
            sequence,
            observation_id: format!("{source}-{sequence}"),
            kind: "test".into(),
            observed_wall_ts_ms: 1,
            available_wall_ts_ms: available_ms,
            subscriptions: Vec::new(),
            payload: b"{}".to_vec(),
            content_sha256: String::new(),
        };
        row.content_sha256 = crate::signals::content_sha256(&row);
        row
    }

    #[tokio::test]
    async fn future_availability_live_channel_and_replay_share_virtual_time() {
        let _clock = engine_types::clock::install_virtual(2_000_000, 2_000_000).unwrap();
        let scheduler = Scheduler::starting_at(2_000_000);
        scheduler.open();
        let missing = row("gap", 1, 4);
        let mut ready = row("independent", 1, 2);
        ready.destination = StrategyId(1);
        ready.content_sha256 = crate::signals::content_sha256(&ready);
        let mut replay = SignalReplayFeed {
            observations: vec![Some(ready.clone()), Some(missing.clone())],
            outstanding: None,
            ready_observation: None,
            producer_frontiers: std::collections::BTreeMap::new(),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            scheduler: scheduler.clone(),
        };
        let (sender, mut live) = crate::signals::signal_channel();
        sender.try_send(missing.clone()).unwrap();
        sender.try_send(ready.clone()).unwrap();
        let gaps = [SignalGapRequest {
            source: "gap".into(),
            next_sequence: 1,
        }];
        replay.set_gap_requests(&gaps, &[StrategyId(0)]).unwrap();
        live.set_gap_requests(&gaps, &[StrategyId(0)]).unwrap();
        assert_eq!(replay.next_observation().await.unwrap(), ready);
        assert_eq!(live.next_observation().await.unwrap(), ready);
        replay.acknowledge_last().unwrap();
        live.acknowledge_last().unwrap();
        assert!(tokio::time::timeout(
            std::time::Duration::from_millis(5),
            replay.next_observation()
        )
        .await
        .is_err());
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(5), live.next_observation())
                .await
                .is_err()
        );
        assert_eq!(crate::clock::wall_ms(), 2);
        assert!(scheduler.earliest_pending(&[WaiterKind::Signal]).is_none());
        scheduler.advance_to(4_000_000);
        assert_eq!(crate::clock::wall_ms(), 4);
        assert_eq!(replay.next_observation().await.unwrap(), missing);
        assert_eq!(live.next_observation().await.unwrap(), missing);
        replay.acknowledge_last().unwrap();
        live.acknowledge_last().unwrap();
    }

    #[tokio::test]
    async fn replay_defer_and_cancellation_preserve_availability_and_exact_catchup() {
        let scheduler = Scheduler::starting_at(2_000_000);
        scheduler.open();
        let future = row("g1", 3, 2);
        let other = row("g2", 1, 3);
        let missing = row("g1", 1, 4);
        let mut feed = SignalReplayFeed {
            observations: vec![
                Some(future.clone()),
                Some(other.clone()),
                Some(missing.clone()),
            ],
            outstanding: None,
            ready_observation: None,
            producer_frontiers: std::collections::BTreeMap::new(),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            scheduler: scheduler.clone(),
        };
        let delivered = feed.next_observation().await.unwrap();
        assert_eq!(delivered, future);
        assert!(feed.next_observation().await.is_err());
        feed.defer_last(delivered).unwrap();
        feed.set_gap_requests(
            &[SignalGapRequest {
                source: "g1".into(),
                next_sequence: 1,
            }],
            &[],
        )
        .unwrap();
        assert!(tokio::time::timeout(
            std::time::Duration::from_millis(10),
            feed.next_observation()
        )
        .await
        .is_err());
        assert!(scheduler.earliest_pending(&[WaiterKind::Signal]).is_none());
        scheduler.advance_to(3_000_000);
        assert_eq!(feed.next_observation().await.unwrap(), other);
        feed.acknowledge_last().unwrap();
        assert!(tokio::time::timeout(
            std::time::Duration::from_millis(10),
            feed.next_observation()
        )
        .await
        .is_err());
        scheduler.advance_to(4_000_000);
        assert_eq!(feed.next_observation().await.unwrap(), missing);
        feed.acknowledge_last().unwrap();
        feed.set_gap_requests(
            &[SignalGapRequest {
                source: "g1".into(),
                next_sequence: 3,
            }],
            &[],
        )
        .unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), future);
        feed.acknowledge_last().unwrap();
        assert!(matches!(
            feed.next_observation().await,
            Err(SignalError::Closed)
        ));
    }

    #[tokio::test]
    async fn replay_prioritizes_only_already_available_missing_rows() {
        let scheduler = Scheduler::starting_at(5_000_000);
        scheduler.open();
        let other = row("g2", 1, 2);
        let missing = row("g1", 1, 4);
        let mut feed = SignalReplayFeed {
            observations: vec![Some(other.clone()), Some(missing.clone())],
            outstanding: None,
            ready_observation: None,
            producer_frontiers: std::collections::BTreeMap::new(),
            gaps: vec![SignalGapRequest {
                source: "g1".into(),
                next_sequence: 1,
            }],
            blocked_destinations: Vec::new(),
            scheduler,
        };
        assert_eq!(feed.next_observation().await.unwrap(), missing);
        feed.acknowledge_last().unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), other);
    }

    #[tokio::test]
    async fn replay_withholds_new_generations_until_the_required_old_prefix_is_complete() {
        let scheduler = Scheduler::starting_at(5_000_000);
        scheduler.open();
        let newer = row("new", 1, 3);
        let missing = row("old", 1, 4);
        let mut unrelated = row("other", 1, 2);
        unrelated.destination = StrategyId(1);
        unrelated.content_sha256 = crate::signals::content_sha256(&unrelated);
        let mut feed = SignalReplayFeed {
            observations: vec![
                Some(unrelated.clone()),
                Some(newer.clone()),
                Some(missing.clone()),
            ],
            outstanding: None,
            ready_observation: None,
            producer_frontiers: std::collections::BTreeMap::new(),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            scheduler,
        };
        feed.set_gap_requests(
            &[SignalGapRequest {
                source: "old".into(),
                next_sequence: 1,
            }],
            &[StrategyId(0)],
        )
        .unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), missing);
        feed.acknowledge_last().unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), unrelated);
        feed.acknowledge_last().unwrap();
        assert!(tokio::time::timeout(
            std::time::Duration::from_millis(10),
            feed.next_observation()
        )
        .await
        .is_err());
        feed.set_gap_requests(&[], &[]).unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), newer);
    }
    #[test]
    fn managed_observation_only_tape_requires_the_recorded_lifecycle_wal() {
        let directory = crate::testpath::temp_path("managed-replay-context");
        std::fs::create_dir_all(directory.path()).unwrap();
        let source = format!("native.e{:020}.g{}.long", 1, "a".repeat(32));
        let observation = row(&source, 1, 2);
        let path = directory.path().join(format!(
            "{:020}-{}.json",
            observation.sequence, observation.content_sha256
        ));
        std::fs::write(&path, serde_json::to_vec(&observation).unwrap()).unwrap();
        let result =
            SignalReplayFeed::from_directory(directory.path(), Scheduler::starting_at(2_000_000));
        assert!(
            matches!(result, Err(SignalError::Source(reason)) if reason.contains("chronological lifecycle grants and seals"))
        );
        std::fs::remove_dir_all(directory.path()).unwrap();
    }
}
