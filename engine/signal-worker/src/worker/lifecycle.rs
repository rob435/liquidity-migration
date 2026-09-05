use super::*;
use engine_types::{
    ManagedSignalSource, SignalLane, SignalLifecycleRequest, SignalLifecycleResponse,
    SignalProducerReport, SignalReadinessRequest, SignalReadinessResponse, SignalSourceFrontier,
};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerSignalLifecycle {
    pub epoch: Option<u64>,
    pub sealed: bool,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum ReadinessRequest {
    Lifecycle(SignalLifecycleRequest),
    Legacy(SignalReadinessRequest),
}

impl DurableSignalWorker {
    pub fn respond_to_readiness_request(&mut self) -> Result<(), WorkerError> {
        let directory = self.spool.directory();
        let request =
            AtomicJsonStore::new(directory.join(engine_types::SIGNAL_READINESS_REQUEST_FILE));
        let Some(request) = request.load::<ReadinessRequest>()? else {
            return Ok(());
        };
        match request {
            ReadinessRequest::Lifecycle(request) => self.respond_to_lifecycle(request),
            ReadinessRequest::Legacy(request) => {
                if request.schema_version != 1 || request.boot_nonce.is_empty() {
                    return Err(WorkerError::input("unsupported input readiness request"));
                }
                if self.worker.state.signal_lifecycle.is_some() {
                    return Err(WorkerError::state(
                        "managed producer cannot downgrade its publication protocol",
                    ));
                }
                let response = SignalReadinessResponse {
                    schema_version: 1,
                    boot_nonce: request.boot_nonce,
                    sources: self.publication_frontiers()?,
                };
                AtomicJsonStore::new(directory.join(engine_types::SIGNAL_READINESS_RESPONSE_FILE))
                    .save(&response)
            }
        }
    }

    pub fn seal_signal_generation(&mut self) -> Result<(), WorkerError> {
        if self.publication_pending || self.pending.load::<PendingTransaction>()?.is_some() {
            return Err(WorkerError::state(
                "cannot seal before recovering the pending publication transaction",
            ));
        }
        if self
            .worker
            .state
            .signal_lifecycle
            .as_ref()
            .is_some_and(|state| state.sealed)
        {
            return Ok(());
        }
        let mut candidate = self.worker.clone();
        let epoch = candidate
            .state
            .signal_lifecycle
            .as_ref()
            .and_then(|state| state.epoch);
        candidate.state.signal_lifecycle = Some(WorkerSignalLifecycle {
            epoch,
            sealed: true,
        });
        self.compact_candidate_checkpoint(&candidate, &[])?;
        self.worker = candidate;
        Ok(())
    }

    fn respond_to_lifecycle(&mut self, request: SignalLifecycleRequest) -> Result<(), WorkerError> {
        if request.schema_version != engine_types::SIGNAL_LIFECYCLE_SCHEMA_VERSION
            || request.boot_nonce.is_empty()
        {
            return Err(WorkerError::input("unsupported input lifecycle request"));
        }
        if self.publication_pending || self.pending.load::<PendingTransaction>()?.is_some() {
            return Err(WorkerError::state(
                "input lifecycle waits for pending publication recovery",
            ));
        }
        if self.worker.state.signal_lifecycle.is_none() {
            self.seal_signal_generation()?;
        }
        let producer = &self.worker.config.routing.source;
        let mut grants = request
            .producers
            .iter()
            .filter(|state| &state.producer == producer);
        let granted = grants.next();
        if grants.next().is_some() {
            return Err(WorkerError::input("engine repeats a producer grant"));
        }
        if let Some(granted) = granted {
            if !granted.unresolved_tail && granted.legacy.is_empty() {
                let active = granted.active.as_ref().ok_or_else(|| {
                    WorkerError::input("engine omitted its active producer grant")
                })?;
                let current = self
                    .worker
                    .state
                    .signal_lifecycle
                    .as_ref()
                    .expect("installed lifecycle");
                let epoch = current.epoch.unwrap_or(0);
                if active.epoch
                    == epoch
                        .checked_add(1)
                        .ok_or_else(|| WorkerError::state("producer epoch exhausted"))?
                {
                    if !current.sealed
                        || granted.retired_through != epoch
                        || active.sealed
                        || active.generation != self.worker.state.source_generation
                        || active.sources.len() != 2
                        || active.sources.iter().any(|row| row.published_through != 0)
                        || granted.previous_seal.len() != 2
                        || self
                            .publication_frontiers()?
                            .iter()
                            .any(|row| !granted.previous_seal.contains(row))
                    {
                        return Err(WorkerError::state(
                            "successor grant does not follow the durable producer seal",
                        ));
                    }
                    let expected = [
                        (true, self.worker.state.long_destination),
                        (false, self.worker.state.carry_destination),
                    ];
                    for (long, destination) in expected {
                        let source = managed_output_source(
                            producer,
                            &active.generation,
                            active.epoch,
                            long,
                        )?;
                        if active
                            .sources
                            .iter()
                            .filter(|row| {
                                row.source == source && row.destination == StrategyId(destination)
                            })
                            .count()
                            != 1
                        {
                            return Err(WorkerError::input(
                                "engine grant changed producer source routing",
                            ));
                        }
                    }
                    let mut candidate = self.worker.clone();
                    candidate.state.signal_lifecycle = Some(WorkerSignalLifecycle {
                        epoch: Some(active.epoch),
                        sealed: false,
                    });
                    candidate.state.long_output_sequence = 0;
                    candidate.state.carry_output_sequence = 0;
                    self.compact_candidate_checkpoint(&candidate, &[])?;
                    self.worker = candidate;
                } else if active.epoch != epoch {
                    return Err(WorkerError::state(
                        "engine producer grant rewound or skipped an epoch",
                    ));
                }
            }
        }
        let current = self
            .worker
            .state
            .signal_lifecycle
            .as_ref()
            .expect("installed lifecycle");
        if current.epoch.is_some() && granted.is_none() {
            return Err(WorkerError::state(
                "engine omitted an already granted producer",
            ));
        }
        let response = SignalLifecycleResponse {
            schema_version: engine_types::SIGNAL_LIFECYCLE_SCHEMA_VERSION,
            boot_nonce: request.boot_nonce,
            producer: SignalProducerReport {
                producer: self.worker.config.routing.source.clone(),
                epoch: current.epoch,
                generation: self.worker.state.source_generation.clone(),
                sealed: current.sealed,
                sources: self.publication_frontiers()?,
            },
        };
        AtomicJsonStore::new(
            self.spool
                .directory()
                .join(engine_types::SIGNAL_READINESS_RESPONSE_FILE),
        )
        .save(&response)
    }

    fn publication_frontiers(&self) -> Result<Vec<SignalSourceFrontier>, WorkerError> {
        let state = &self.worker.state;
        let epoch = state
            .signal_lifecycle
            .as_ref()
            .and_then(|state| state.epoch);
        [
            (true, state.long_destination, state.long_output_sequence),
            (false, state.carry_destination, state.carry_output_sequence),
        ]
        .into_iter()
        .map(|(long, destination, published_through)| {
            Ok(SignalSourceFrontier {
                source: match epoch {
                    Some(epoch) => managed_output_source(
                        &self.worker.config.routing.source,
                        &state.source_generation,
                        epoch,
                        long,
                    )?,
                    None => output_source(
                        &self.worker.config.routing.source,
                        &state.source_generation,
                        long,
                    )?,
                },
                destination: StrategyId(destination),
                published_through,
            })
        })
        .collect()
    }
}

pub(super) fn managed_output_source(
    producer: &str,
    generation: &str,
    epoch: u64,
    long: bool,
) -> Result<String, WorkerError> {
    ManagedSignalSource {
        producer,
        epoch,
        generation,
        lane: if long {
            SignalLane::Long
        } else {
            SignalLane::Carry
        },
    }
    .encode()
    .map_err(WorkerError::state)
}
