use super::*;
use engine_types::identity::{IdentityState, SignalSourceSleeve, SleeveKey};

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerDestinationSleeves {
    pub long: SleeveKey,
    pub carry: SleeveKey,
}

impl WorkerDestinationSleeves {
    fn configured(config: &SignalWorkerConfig) -> Result<Self, WorkerError> {
        Ok(Self {
            long: SleeveKey::new(config.routing.long_sleeve.clone())
                .map_err(|error| WorkerError::config(error.to_string()))?,
            carry: SleeveKey::new(config.routing.carry_sleeve.clone())
                .map_err(|error| WorkerError::config(error.to_string()))?,
        })
    }
}

pub(super) fn restore_destinations(
    config: &mut SignalWorkerConfig,
    state: &WorkerState,
) -> Result<bool, WorkerError> {
    if state.long_destination == state.carry_destination {
        return Err(WorkerError::state(
            "checkpoint directional destinations share one durable id",
        ));
    }
    let configured = WorkerDestinationSleeves::configured(config)?;
    if state
        .destination_sleeves
        .as_ref()
        .is_some_and(|old| old != &configured)
    {
        return Err(WorkerError::state(
            "checkpoint directional sleeve keys changed",
        ));
    }
    let needs_verification = state.destination_sleeves.is_none()
        && (state.long_destination != config.long_destination
            || state.carry_destination != config.carry_destination);
    config.long_destination = state.long_destination;
    config.carry_destination = state.carry_destination;
    Ok(needs_verification)
}

impl DurableSignalWorker {
    pub fn require_named_destinations(&mut self) {
        self.worker.routing_verification_required = true;
    }

    pub fn destinations_verified(&self) -> bool {
        !self.worker.routing_verification_required
    }

    pub(super) fn bind_destination_sleeves(
        &mut self,
        keys: &[SleeveKey],
    ) -> Result<(), WorkerError> {
        if keys.is_empty() {
            if self.worker.routing_verification_required
                || self.worker.state.destination_sleeves.is_some()
            {
                return Err(WorkerError::state(
                    "engine omitted the named destination registry",
                ));
            }
            return Ok(());
        }
        IdentityState {
            sleeves: keys.to_vec(),
            ..IdentityState::default()
        }
        .validate()
        .map_err(|error| WorkerError::input(error.to_string()))?;
        let configured = WorkerDestinationSleeves::configured(&self.worker.config)?;
        let find = |key: &SleeveKey| -> Result<u16, WorkerError> {
            let index = keys
                .iter()
                .position(|candidate| candidate == key)
                .ok_or_else(|| {
                    WorkerError::state(format!(
                        "engine registry omits directional sleeve {:?}",
                        key.as_str()
                    ))
                })?;
            u16::try_from(index)
                .map_err(|_| WorkerError::state("engine registry exceeds durable id capacity"))
        };
        let long = find(&configured.long)?;
        let carry = find(&configured.carry)?;
        if long == carry {
            return Err(WorkerError::state(
                "directional sleeves share one durable id",
            ));
        }
        let state = &self.worker.state;
        let routing_is_durable = state.destination_sleeves.is_some()
            || state.long_output_sequence != 0
            || state.carry_output_sequence != 0
            || state
                .signal_lifecycle
                .as_ref()
                .is_some_and(|lifecycle| lifecycle.epoch.is_some());
        if routing_is_durable
            && (long != state.long_destination || carry != state.carry_destination)
        {
            return Err(WorkerError::state(
                "engine named destinations reinterpret already published source history",
            ));
        }
        let mut candidate = self.worker.clone();
        candidate.state.destination_sleeves = Some(configured);
        candidate.state.long_destination = long;
        candidate.state.carry_destination = carry;
        candidate.config.long_destination = long;
        candidate.config.carry_destination = carry;
        candidate.routing_verification_required = false;
        if candidate.state.destination_sleeves != self.worker.state.destination_sleeves {
            self.compact_candidate_checkpoint(&candidate, &[])?;
        }
        self.worker = candidate;
        Ok(())
    }

    pub(super) fn source_sleeves(&self) -> Result<Vec<SignalSourceSleeve>, WorkerError> {
        let Some(keys) = &self.worker.state.destination_sleeves else {
            return Ok(Vec::new());
        };
        Ok(self
            .publication_frontiers()?
            .into_iter()
            .map(|source| SignalSourceSleeve {
                sleeve: if source.destination.0 == self.worker.state.long_destination {
                    keys.long.clone()
                } else {
                    keys.carry.clone()
                },
                source: source.source,
            })
            .collect())
    }
}
