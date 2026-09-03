//! Durable signal observations replayed at the instant each became
//! available.
//!
//! The spool is read with the live feed's own row reader, so every row is
//! validated and checked against its name exactly as the engine would check
//! it live. Delivery waits on the virtual clock for the row's
//! `available_wall_ts_ms`: an observation is never seen before the moment
//! the worker had published it.

use std::path::Path;

use engine_types::{SignalError, SignalFeed, SignalObservation};

use super::scheduler::{Scheduler, WaiterKind};
use crate::signals::SpoolSignalFeed;

pub struct SignalReplayFeed {
    observations: Vec<SignalObservation>,
    next: usize,
    scheduler: Scheduler,
}

impl SignalReplayFeed {
    pub fn empty(scheduler: Scheduler) -> Self {
        SignalReplayFeed {
            observations: Vec::new(),
            next: 0,
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
            if path.extension().is_some_and(|ext| ext == "json") {
                paths.push(path);
            }
        }
        paths.sort();
        let mut observations = Vec::with_capacity(paths.len());
        for path in paths {
            if let Some(observation) = SpoolSignalFeed::read_one(&path)? {
                observations.push(observation);
            }
        }
        observations.sort_by_key(|o| (o.available_wall_ts_ms, o.sequence));
        Ok(SignalReplayFeed {
            observations,
            next: 0,
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
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        let Some(observation) = self.observations.get(self.next) else {
            return Err(SignalError::Closed);
        };
        // Wall and monotonic virtual time share one origin in the replay:
        // both are the tape's receive stamp in nanoseconds since the epoch.
        let available_ns =
            (observation.available_wall_ts_ms.max(0) as u64).saturating_mul(1_000_000);
        self.scheduler
            .sleep_until(available_ns, WaiterKind::Signal)
            .await;
        // Advance the cursor only on the poll that returns, so a future the
        // loop dropped mid-wait loses nothing.
        let observation = self.observations[self.next].clone();
        self.next += 1;
        Ok(observation)
    }
}
