use super::*;
use engine_types::{
    legacy_signal_lane, valid_signal_generation, ManagedSignalSource, SignalGenerationState,
    SignalLegacyGeneration, SignalProducerLifecycle, SignalProducerReport, SignalProducerRoute,
    SignalSourceFrontier,
};

impl SignalState {
    pub fn producers(&self) -> impl Iterator<Item = &SignalProducerLifecycle> {
        self.producers.values()
    }

    pub fn lifecycle_legacy_sources(&self) -> Vec<SignalSourceFrontier> {
        let mut rows = BTreeMap::new();
        for cursor in self.cursors.values() {
            if self.legacy_retirements.contains_key(&cursor.source) {
                continue;
            }
            if ManagedSignalSource::parse(&cursor.source).is_none() {
                if let Some(destination) = self.destination(&cursor.source) {
                    rows.insert(
                        cursor.source.clone(),
                        SignalSourceFrontier {
                            source: cursor.source.clone(),
                            destination,
                            published_through: cursor.sequence,
                        },
                    );
                }
            }
        }
        for gap in self.gaps.values() {
            if ManagedSignalSource::parse(&gap.source).is_none() {
                rows.insert(
                    gap.source.clone(),
                    SignalSourceFrontier {
                        source: gap.source.clone(),
                        destination: gap.destination,
                        published_through: gap.observed_sequence,
                    },
                );
            }
        }
        rows.into_values().collect()
    }

    pub fn producer_routes(&self) -> impl Iterator<Item = &SignalProducerRoute> {
        self.producers.values().flat_map(|state| &state.routes)
    }

    pub fn plan_producer_report(
        &self,
        report: &SignalProducerReport,
        strategies: usize,
    ) -> Result<SignalProducerLifecycle, String> {
        if report.producer.is_empty()
            || report.producer.len() > 192
            || !valid_signal_generation(&report.generation)
            || report.sources.is_empty()
            || report.sources.len() > strategies
        {
            return Err("invalid producer lifecycle identity".into());
        }
        let mut lanes = BTreeSet::new();
        let mut destinations = BTreeSet::new();
        for source in &report.sources {
            if self
                .legacy_retirements
                .get(&source.source)
                .is_some_and(|retirement| {
                    retirement.destination != source.destination
                        || retirement.published_through != source.published_through
                })
            {
                return Err("producer rewrote an operator-retired source frontier".into());
            }
            let lane = match report.epoch {
                Some(epoch) => {
                    let identity = ManagedSignalSource::parse(&source.source)
                        .ok_or("producer report has an invalid managed source")?;
                    if identity.producer != report.producer
                        || identity.epoch != epoch
                        || identity.generation != report.generation
                    {
                        return Err("producer report changed its granted source".into());
                    }
                    identity.lane
                }
                None => {
                    let lane = legacy_signal_lane(&report.producer, &source.source)
                        .ok_or("producer report has an invalid legacy source")?;
                    let expected = if report.generation == "0".repeat(32) {
                        format!("{}.{}", report.producer, lane.name())
                    } else {
                        format!("{}.g{}.{}", report.producer, report.generation, lane.name())
                    };
                    if source.source != expected {
                        return Err("legacy producer report changed its generation".into());
                    }
                    lane
                }
            };
            if source.destination.idx() >= strategies
                || !destinations.insert(source.destination)
                || !lanes.insert(lane.name())
                || self
                    .destination(&source.source)
                    .is_some_and(|known| known != source.destination)
                || source.published_through
                    < self
                        .cursors
                        .get(&source.source)
                        .map_or(0, |row| row.sequence)
                    && report.sealed
            {
                return Err("producer report has invalid routes or a rewound seal".into());
            }
        }
        if let Some(known) = self.producers.get(&report.producer) {
            let mut next = known.clone();
            if report.epoch.is_none() {
                if !report.sealed || known.legacy.is_empty() {
                    return Err("legacy producer has no unresolved generation to seal".into());
                }
                for source in &report.sources {
                    let legacy = next
                        .legacy
                        .iter_mut()
                        .find(|row| row.source == source.source)
                        .ok_or("producer omitted an unregistered legacy generation")?;
                    if legacy.destination != source.destination
                        || legacy
                            .published_through
                            .is_some_and(|value| value != source.published_through)
                    {
                        return Err("producer rewrote an immutable legacy seal".into());
                    }
                    legacy.published_through = Some(source.published_through);
                }
            } else {
                if !known.legacy.is_empty() {
                    return Err("producer started its successor before legacy closure".into());
                }
                let active = next.active.as_mut().ok_or("producer has no active grant")?;
                if report.epoch != Some(active.epoch)
                    || report.generation != active.generation
                    || !same_routes(&active.sources, &report.sources)
                {
                    return Err(
                        "producer readiness does not cover the complete active roster".into(),
                    );
                }
                if active.sealed && (!report.sealed || active.sources != report.sources) {
                    return Err("producer changed a sealed generation".into());
                }
                if report.sealed {
                    active.sources = report.sources.clone();
                    active.sealed = true;
                }
            }
            return Ok(next);
        }
        if report.epoch.is_some() || !report.sealed {
            return Err("producer has no durable engine epoch grant".into());
        }
        if self.lifecycle_legacy_sources().iter().any(|row| {
            destinations.contains(&row.destination)
                && legacy_signal_lane(&report.producer, &row.source).is_none()
        }) {
            return Err("producer readiness omits an accepted legacy namespace".into());
        }
        if self.producers.len() >= strategies
            || self.producers.values().any(|known| {
                known
                    .routes
                    .iter()
                    .any(|route| destinations.contains(&route.destination))
            })
        {
            return Err("producer namespace overlaps a configured destination".into());
        }
        let mut legacy = BTreeMap::new();
        for source in self.lifecycle_legacy_sources() {
            if legacy_signal_lane(&report.producer, &source.source).is_some() {
                legacy.insert(
                    source.source.clone(),
                    SignalLegacyGeneration {
                        source: source.source,
                        destination: source.destination,
                        published_through: None,
                    },
                );
            }
        }
        for source in &report.sources {
            legacy.insert(
                source.source.clone(),
                SignalLegacyGeneration {
                    source: source.source.clone(),
                    destination: source.destination,
                    published_through: Some(source.published_through),
                },
            );
        }
        let mut routes = Vec::new();
        for destination in destinations {
            let mut subscriptions = Vec::new();
            for row in self
                .subscriptions
                .values()
                .filter(|row| row.destination == destination && legacy.contains_key(&row.source))
            {
                for subscription in &row.subscriptions {
                    if !subscriptions.contains(subscription) {
                        subscriptions.push(subscription.clone());
                    }
                }
            }
            if subscriptions.len() > engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS {
                return Err(
                    "legacy producer routes exceed the per-destination subscription budget".into(),
                );
            }
            routes.push(SignalProducerRoute {
                destination,
                subscriptions,
            });
        }
        Ok(SignalProducerLifecycle {
            producer: report.producer.clone(),
            retired_through: 0,
            active: Some(next_generation(
                &report.producer,
                &report.generation,
                1,
                &report.sources,
            )?),
            legacy: legacy.into_values().collect(),
            routes,
            unresolved_tail: false,
            previous_seal: Vec::new(),
        })
    }

    pub fn lifecycle_gaps(
        &self,
        state: &SignalProducerLifecycle,
    ) -> Result<Vec<SignalGap>, String> {
        let mut sources = Vec::new();
        for row in &state.legacy {
            if let Some(published_through) = row.published_through {
                sources.push(SignalSourceFrontier {
                    source: row.source.clone(),
                    destination: row.destination,
                    published_through,
                });
            }
        }
        if let Some(active) = &state.active {
            if active.sealed {
                sources.extend(active.sources.iter().cloned());
            }
        }
        self.frontier_gaps(&sources, usize::from(u16::MAX) + 1)
    }

    pub fn lifecycle_advances(&self) -> Result<Vec<SignalProducerLifecycle>, String> {
        let mut updates = Vec::new();
        for state in self.producers.values() {
            if state.unresolved_tail {
                continue;
            }
            let mut next = state.clone();
            if !state.legacy.is_empty() {
                if state.legacy.iter().all(|row| {
                    row.published_through
                        .is_some_and(|last| self.source_finished(&row.source, last))
                }) {
                    let active = state
                        .active
                        .as_ref()
                        .ok_or("legacy closure has no reserved successor")?;
                    next.previous_seal = state
                        .legacy
                        .iter()
                        .filter(|row| {
                            legacy_signal_lane(&state.producer, &row.source).is_some_and(|lane| {
                                row.source
                                    == legacy_source(&state.producer, &active.generation, lane)
                            })
                        })
                        .map(|row| SignalSourceFrontier {
                            source: row.source.clone(),
                            destination: row.destination,
                            published_through: row
                                .published_through
                                .expect("checked final frontier"),
                        })
                        .collect();
                    next.previous_seal.sort_by_key(|row| row.destination);
                    next.legacy.clear();
                    updates.push(next);
                }
                continue;
            }
            if let Some(active) = &state.active {
                if active.sealed
                    && active
                        .sources
                        .iter()
                        .all(|row| self.source_finished(&row.source, row.published_through))
                {
                    next.retired_through = active.epoch;
                    next.previous_seal = active.sources.clone();
                    next.previous_seal.sort_by_key(|row| row.destination);
                    next.active = Some(next_generation(
                        &state.producer,
                        &active.generation,
                        active
                            .epoch
                            .checked_add(1)
                            .ok_or("producer epoch exhausted")?,
                        &active.sources,
                    )?);
                    updates.push(next);
                }
            }
        }
        Ok(updates)
    }

    fn source_finished(&self, source: &str, last: u64) -> bool {
        (self.cursors.get(source).map_or(0, |row| row.sequence) == last
            || self
                .legacy_retirements
                .get(source)
                .is_some_and(|row| row.published_through == last))
            && !self.gaps.contains_key(source)
            && !self.observations.keys().any(|(known, _)| known == source)
    }

    pub fn restore_producer_snapshot(
        &mut self,
        state: SignalProducerLifecycle,
        strategies: usize,
    ) -> Result<(), String> {
        validate_snapshot(&state, strategies)?;
        if self.producers.len() >= strategies
            || self.producers.values().any(|known| {
                known.routes.iter().any(|route| {
                    state
                        .routes
                        .iter()
                        .any(|other| route.destination == other.destination)
                })
            })
        {
            return Err("rotation overlaps signal producer destinations".into());
        }
        if self
            .producers
            .insert(state.producer.clone(), state)
            .is_some()
        {
            return Err("rotation repeats a signal producer lifecycle".into());
        }
        Ok(())
    }

    pub fn validate_producer_lifecycle(
        &self,
        state: &SignalProducerLifecycle,
        strategies: usize,
    ) -> Result<(), String> {
        validate_snapshot(state, strategies)?;
        if self
            .producers
            .values()
            .filter(|known| known.producer != state.producer)
            .any(|known| {
                known.routes.iter().any(|route| {
                    state
                        .routes
                        .iter()
                        .any(|other| route.destination == other.destination)
                })
            })
        {
            return Err("producer lifecycle overlaps another destination owner".into());
        }
        if let Some(old) = self.producers.get(&state.producer) {
            if old.unresolved_tail && !state.unresolved_tail
                || old.routes.len() != state.routes.len()
                || old.routes.iter().any(|before| {
                    !state.routes.iter().any(|after| {
                        after.destination == before.destination
                            && after
                                .subscriptions
                                .iter()
                                .all(|subscription| before.subscriptions.contains(subscription))
                    })
                })
                || self.observations.values().any(|row| {
                    state.routes.iter().any(|route| {
                        route.destination == row.destination
                            && row
                                .subscriptions
                                .iter()
                                .any(|subscription| !route.subscriptions.contains(subscription))
                    })
                })
            {
                return Err(
                    "producer lifecycle discards unresolved history or subscriptions".into(),
                );
            }
            if !old.legacy.is_empty() && state.legacy.is_empty() {
                if !old.legacy.iter().all(|row| {
                    row.published_through
                        .is_some_and(|last| self.source_finished(&row.source, last))
                }) {
                    return Err("producer retires an unfinished legacy source".into());
                }
            } else if old.legacy.len() != state.legacy.len()
                || old.legacy.iter().zip(&state.legacy).any(|(a, b)| {
                    a.source != b.source
                        || a.destination != b.destination
                        || a.published_through.is_some()
                            && a.published_through != b.published_through
                })
            {
                return Err("producer lifecycle rewrites the legacy roster".into());
            }
            match (&old.active, &state.active) {
                (Some(before), Some(after)) if before.epoch == after.epoch => {
                    if old.retired_through != state.retired_through
                        || before.generation != after.generation
                        || !same_routes(&before.sources, &after.sources)
                        || before.sealed && before != after
                        || old.legacy.is_empty() && old.previous_seal != state.previous_seal
                    {
                        return Err("producer lifecycle rewrites a grant or seal".into());
                    }
                }
                (Some(before), Some(after)) if before.epoch.checked_add(1) == Some(after.epoch) => {
                    if !old.legacy.is_empty()
                        || !before.sealed
                        || state.retired_through != before.epoch
                        || !before
                            .sources
                            .iter()
                            .all(|row| self.source_finished(&row.source, row.published_through))
                        || after.sealed
                        || after.sources.iter().any(|row| row.published_through != 0)
                        || before.generation != after.generation
                        || before.sources.iter().any(|row| {
                            !after.sources.iter().any(|next| {
                                next.destination == row.destination
                                    && ManagedSignalSource::parse(&next.source).map(|id| id.lane)
                                        == ManagedSignalSource::parse(&row.source).map(|id| id.lane)
                            })
                        })
                        || !same_frontiers(&state.previous_seal, &before.sources)
                    {
                        return Err("producer advances before every terminal input outcome".into());
                    }
                }
                _ => return Err("producer lifecycle skips its granted epoch".into()),
            }
        } else if state.retired_through != 0
            || state.active.as_ref().is_none_or(|active| active.epoch != 1)
            || state.legacy.is_empty()
            || self.producers.len() >= strategies
        {
            return Err("producer lifecycle starts without a legacy adoption seal".into());
        }
        let producer = state.producer.clone();
        let retired_through = state.retired_through;
        let legacy = state
            .legacy
            .iter()
            .map(|row| row.source.clone())
            .collect::<BTreeSet<_>>();
        let retired = |source: &str| {
            ManagedSignalSource::parse(source)
                .is_some_and(|id| id.producer == producer && id.epoch <= retired_through)
                || legacy_signal_lane(&producer, source).is_some() && !legacy.contains(source)
        };
        if self.observations.keys().any(|(source, _)| retired(source))
            || self.gaps.keys().any(|source| retired(source))
        {
            return Err("producer lifecycle retires unfinished input".into());
        }
        Ok(())
    }

    pub fn apply_producer_lifecycle(
        &mut self,
        state: SignalProducerLifecycle,
        strategies: usize,
    ) -> Result<(), String> {
        self.validate_producer_lifecycle(&state, strategies)?;
        let producer = state.producer.clone();
        let retired_through = state.retired_through;
        let legacy = state
            .legacy
            .iter()
            .map(|row| row.source.clone())
            .collect::<BTreeSet<_>>();
        let retired = |source: &str| {
            ManagedSignalSource::parse(source)
                .is_some_and(|id| id.producer == producer && id.epoch <= retired_through)
                || legacy_signal_lane(&producer, source).is_some() && !legacy.contains(source)
        };
        if self.observations.keys().any(|(source, _)| retired(source))
            || self.gaps.keys().any(|source| retired(source))
        {
            return Err("producer lifecycle retires unfinished input".into());
        }
        self.cursors
            .retain(|source, _| !retired(source) || self.legacy_retirements.contains_key(source));
        self.subscriptions.retain(|(source, _), _| {
            !retired(source) || self.legacy_retirements.contains_key(source)
        });
        for row in self.subscriptions.values_mut() {
            if let Some(route) = state
                .routes
                .iter()
                .find(|route| route.destination == row.destination)
            {
                row.subscriptions
                    .retain(|subscription| route.subscriptions.contains(subscription));
            }
        }
        self.producer_frontiers.retain(|source, _| !retired(source));
        self.readiness_request_cursors
            .retain(|source, _| !retired(source));
        self.producers.insert(producer, state);
        Ok(())
    }

    pub fn lifecycle_blocked(&self, destination: StrategyId) -> bool {
        self.producers.values().any(|state| {
            state
                .routes
                .iter()
                .any(|row| row.destination == destination)
                && (state.unresolved_tail
                    || !state.legacy.is_empty()
                    || state.active.as_ref().is_none_or(|active| active.sealed))
        })
    }

    pub fn validate_retained_source(
        &self,
        source: &str,
        destination: StrategyId,
        last: u64,
    ) -> Result<(), String> {
        if let Some(retirement) = self.legacy_retirements.get(source) {
            return if retirement.destination == destination && retirement.accepted_through == last {
                Ok(())
            } else {
                Err("retired source rewrites its accepted cursor".into())
            };
        }
        let allowed = if let Some(identity) = ManagedSignalSource::parse(source) {
            self.producers.get(identity.producer).is_some_and(|state| {
                state.legacy.is_empty()
                    && state.active.as_ref().is_some_and(|active| {
                        identity.epoch == active.epoch
                            && identity.generation == active.generation
                            && active.sources.iter().any(|row| {
                                row.source == source
                                    && row.destination == destination
                                    && (!active.sealed || last <= row.published_through)
                            })
                    })
            })
        } else if let Some(state) = self
            .producers
            .values()
            .find(|state| legacy_signal_lane(&state.producer, source).is_some())
        {
            state.legacy.iter().any(|row| {
                row.source == source
                    && row.destination == destination
                    && row
                        .published_through
                        .is_none_or(|final_sequence| last <= final_sequence)
            })
        } else {
            !self
                .producer_routes()
                .any(|row| row.destination == destination)
        };
        if allowed {
            Ok(())
        } else {
            Err("retained signal is outside its current producer grant".into())
        }
    }

    pub fn managed_admission(
        &self,
        observation: &SignalObservation,
    ) -> Result<Option<Admission>, String> {
        if self.legacy_retirements.contains_key(&observation.source) {
            return Ok(Some(Admission::Unregistered));
        }
        if let Some(identity) = ManagedSignalSource::parse(&observation.source) {
            let Some(state) = self.producers.get(identity.producer) else {
                return Ok(Some(Admission::Unregistered));
            };
            let Some(active) = &state.active else {
                return Ok(Some(Admission::Unregistered));
            };
            if identity.generation != active.generation
                || !active.sources.iter().any(|source| {
                    source.destination == observation.destination
                        && ManagedSignalSource::parse(&source.source)
                            .is_some_and(|known| known.lane == identity.lane)
                })
            {
                return Ok(Some(Admission::Unregistered));
            }
            if identity.epoch <= state.retired_through {
                if state.previous_seal.iter().any(|source| {
                    source.source == observation.source
                        && observation.sequence > source.published_through
                }) {
                    return Ok(Some(Admission::Unregistered));
                }
                return Ok(Some(Admission::Duplicate));
            }
            if state.unresolved_tail
                || !state.legacy.is_empty()
                || identity.epoch != active.epoch
                || identity.generation != active.generation
            {
                return Ok(Some(Admission::Unregistered));
            }
            let Some(source) = active.sources.iter().find(|row| {
                row.source == observation.source && row.destination == observation.destination
            }) else {
                return Ok(Some(Admission::Unregistered));
            };
            if active.sealed && observation.sequence > source.published_through {
                return Ok(Some(Admission::Unregistered));
            }
        } else if let Some(state) = self
            .producers
            .values()
            .find(|state| legacy_signal_lane(&state.producer, &observation.source).is_some())
        {
            let Some(source) = state
                .legacy
                .iter()
                .find(|row| row.source == observation.source)
            else {
                return Ok(Some(Admission::Unregistered));
            };
            if source.destination != observation.destination
                || source
                    .published_through
                    .is_some_and(|last| observation.sequence > last)
            {
                return Ok(Some(Admission::Unregistered));
            }
        } else if self
            .producer_routes()
            .any(|row| row.destination == observation.destination)
        {
            return Ok(Some(Admission::Unregistered));
        }
        Ok(None)
    }
}

fn legacy_source(producer: &str, generation: &str, lane: engine_types::SignalLane) -> String {
    if generation == "0".repeat(32) {
        format!("{producer}.{}", lane.name())
    } else {
        format!("{producer}.g{generation}.{}", lane.name())
    }
}

fn same_routes(a: &[SignalSourceFrontier], b: &[SignalSourceFrontier]) -> bool {
    a.len() == b.len()
        && a.iter().all(|row| {
            b.iter()
                .any(|other| row.source == other.source && row.destination == other.destination)
        })
}

fn same_frontiers(a: &[SignalSourceFrontier], b: &[SignalSourceFrontier]) -> bool {
    a.len() == b.len() && a.iter().all(|row| b.contains(row))
}

fn next_generation(
    producer: &str,
    generation: &str,
    epoch: u64,
    sources: &[SignalSourceFrontier],
) -> Result<SignalGenerationState, String> {
    let sources = sources
        .iter()
        .map(|row| {
            let lane = ManagedSignalSource::parse(&row.source)
                .map(|id| id.lane)
                .or_else(|| legacy_signal_lane(producer, &row.source))
                .ok_or("invalid source lane")?;
            Ok(SignalSourceFrontier {
                source: ManagedSignalSource {
                    producer,
                    epoch,
                    generation,
                    lane,
                }
                .encode()?,
                destination: row.destination,
                published_through: 0,
            })
        })
        .collect::<Result<Vec<_>, String>>()?;
    Ok(SignalGenerationState {
        epoch,
        generation: generation.to_owned(),
        sources,
        sealed: false,
    })
}

fn validate_snapshot(state: &SignalProducerLifecycle, strategies: usize) -> Result<(), String> {
    if state.producer.is_empty()
        || state.producer.len() > 192
        || state.routes.is_empty()
        || state.routes.len() > strategies
    {
        return Err("invalid producer lifecycle snapshot".into());
    }
    let mut destinations = BTreeSet::new();
    for route in &state.routes {
        if route.destination.idx() >= strategies
            || !destinations.insert(route.destination)
            || route.subscriptions.len() > engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS
            || route
                .subscriptions
                .iter()
                .enumerate()
                .any(|(index, row)| route.subscriptions[..index].contains(row))
        {
            return Err("invalid producer subscription ownership".into());
        }
    }
    let active = state
        .active
        .as_ref()
        .ok_or("producer snapshot has no current grant")?;
    if state.retired_through.checked_add(1) != Some(active.epoch)
        || !valid_signal_generation(&active.generation)
        || active.sources.len() != state.routes.len()
    {
        return Err("producer snapshot skips its retirement floor".into());
    }
    if state.previous_seal.len() > state.routes.len()
        || state.legacy.is_empty() && state.previous_seal.len() != state.routes.len()
    {
        return Err("producer grant omits its predecessor seal".into());
    }
    let mut seen = BTreeSet::new();
    let mut lanes = BTreeSet::new();
    for source in &active.sources {
        let id = ManagedSignalSource::parse(&source.source).ok_or("invalid granted source")?;
        if id.producer != state.producer
            || id.epoch != active.epoch
            || id.generation != active.generation
            || !destinations.contains(&source.destination)
            || !seen.insert(source.destination)
            || !lanes.insert(id.lane.name())
            || !active.sealed && source.published_through != 0
        {
            return Err("producer snapshot changes granted source routes".into());
        }
    }
    let mut previous_destinations = BTreeSet::new();
    for source in &state.previous_seal {
        let lane = active
            .sources
            .iter()
            .find(|row| row.destination == source.destination)
            .and_then(|row| ManagedSignalSource::parse(&row.source))
            .ok_or("predecessor seal changes destination")?
            .lane;
        let expected = if state.retired_through == 0 {
            legacy_source(&state.producer, &active.generation, lane)
        } else {
            ManagedSignalSource {
                producer: &state.producer,
                epoch: state.retired_through,
                generation: &active.generation,
                lane,
            }
            .encode()?
        };
        if source.source != expected || !previous_destinations.insert(source.destination) {
            return Err("producer predecessor seal changes identity".into());
        }
    }
    let mut seen = BTreeSet::new();
    for source in &state.legacy {
        if legacy_signal_lane(&state.producer, &source.source).is_none()
            || !destinations.contains(&source.destination)
            || !seen.insert(&source.source)
        {
            return Err("producer snapshot has an invalid legacy roster".into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{Feed, SignalLane};

    fn row(source: &str, sequence: u64) -> SignalObservation {
        let mut row = SignalObservation {
            schema_version: engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: "test".into(),
            destination: StrategyId(0),
            source: source.into(),
            sequence,
            observation_id: format!("{source}-{sequence}"),
            kind: "test".into(),
            observed_wall_ts_ms: 1,
            available_wall_ts_ms: 2,
            subscriptions: vec![Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
            payload: vec![1],
            content_sha256: String::new(),
        };
        row.content_sha256 = crate::signals::content_sha256(&row);
        row
    }

    fn discovery() -> SignalProducerReport {
        let generation = "a".repeat(32);
        SignalProducerReport {
            producer: "native".into(),
            epoch: None,
            generation: generation.clone(),
            sealed: true,
            sources: vec![
                SignalSourceFrontier {
                    source: format!("native.g{generation}.long"),
                    destination: StrategyId(0),
                    published_through: 0,
                },
                SignalSourceFrontier {
                    source: format!("native.g{generation}.carry"),
                    destination: StrategyId(1),
                    published_through: 0,
                },
            ],
        }
    }

    fn managed() -> SignalState {
        let mut state = SignalState::default();
        state.require_readiness([StrategyId(0), StrategyId(1)]);
        let next = state.plan_producer_report(&discovery(), 2).unwrap();
        state.apply_producer_lifecycle(next, 2).unwrap();
        let next = state.lifecycle_advances().unwrap().pop().unwrap();
        state.apply_producer_lifecycle(next, 2).unwrap();
        state
    }

    fn active_report(state: &SignalState, sealed: bool, long_through: u64) -> SignalProducerReport {
        let producer = state.producers().next().unwrap();
        let active = producer.active.as_ref().unwrap();
        let mut sources = active.sources.clone();
        sources
            .iter_mut()
            .find(|row| row.destination == StrategyId(0))
            .unwrap()
            .published_through = long_through;
        SignalProducerReport {
            producer: producer.producer.clone(),
            epoch: Some(active.epoch),
            generation: active.generation.clone(),
            sealed,
            sources,
        }
    }

    fn rotation(state: &SignalState) -> WalRecord {
        serde_json::from_value(serde_json::json!({
            "kind":"segment_base", "wall_ts_ms":2, "strategies":["long","carry"], "symbols":["BTCUSDT"],
            "may_open":true, "control_anchors":[], "attribution":[], "logged_exposure":[], "intended_stops":[], "open_orders":[],
            "signal_observations":state.observations().collect::<Vec<_>>(), "signal_cursors":state.cursors().collect::<Vec<_>>(),
            "signal_subscriptions":state.subscriptions().collect::<Vec<_>>(), "signal_gaps":state.gaps().collect::<Vec<_>>(),
            "signal_producers":state.producers().collect::<Vec<_>>(),
        })).unwrap()
    }

    #[test]
    fn lifecycle_readiness_preserves_an_omitted_prior_generation_tail() {
        let mut state = SignalState::default();
        let old = row(&format!("native.g{}.long", "b".repeat(32)), 1);
        state.accept(old.clone());
        state.consume(&old.source, 1);
        let next = state.plan_producer_report(&discovery(), 2).unwrap();
        state.apply_producer_lifecycle(next, 2).unwrap();
        assert!(
            state.lifecycle_blocked(StrategyId(0)),
            "a new generation cannot erase the old unknown final frontier"
        );
        assert!(state.lifecycle_advances().unwrap().is_empty());
        let old = state
            .producers()
            .next()
            .unwrap()
            .legacy
            .iter()
            .find(|row| row.source == old.source)
            .unwrap();
        assert_eq!(old.published_through, None);
        let restored = SignalState::replay(&[rotation(&state)], 2).unwrap();
        assert!(restored.lifecycle_blocked(StrategyId(0)));
        assert!(restored.lifecycle_advances().unwrap().is_empty());
    }

    #[test]
    fn lifecycle_grant_rejects_unregistered_epochs_and_opaque_sources_without_allocating() {
        let state = managed();
        let future = ManagedSignalSource {
            producer: "native",
            epoch: 2,
            generation: &"a".repeat(32),
            lane: SignalLane::Long,
        }
        .encode()
        .unwrap();
        for source in [
            future,
            "native.gbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.long".into(),
            "new_namespace.long".into(),
        ] {
            assert_eq!(
                state.classify(&row(&source, 1)).unwrap(),
                Admission::Unregistered
            );
        }
        assert_eq!(state.cursors().count(), 0);
        assert_eq!(state.producers().count(), 1);
    }

    #[test]
    fn lifecycle_seal_waits_for_the_terminal_outcome_and_rejects_late_publication() {
        let mut state = managed();
        let report = active_report(&state, true, 1);
        let input = row(&report.sources[0].source, 1);
        state.accept(input.clone());
        let sealed = state.plan_producer_report(&report, 2).unwrap();
        state.apply_producer_lifecycle(sealed, 2).unwrap();
        assert!(
            state.lifecycle_advances().unwrap().is_empty(),
            "durable acceptance is not a terminal consumer outcome"
        );
        assert_eq!(
            state.classify(&row(&input.source, 2)).unwrap(),
            Admission::Unregistered
        );
        let restored = SignalState::replay(&[rotation(&state)], 2).unwrap();
        assert!(restored.lifecycle_advances().unwrap().is_empty());
        state.consume(&input.source, 1);
        let next = state.lifecycle_advances().unwrap().pop().unwrap();
        state.apply_producer_lifecycle(next, 2).unwrap();
        assert_eq!(state.classify(&input).unwrap(), Admission::Duplicate);
        assert_eq!(state.cursors().count(), 0);
    }

    #[test]
    fn lifecycle_epoch_churn_retains_bounded_metadata_and_routes_across_rotation() {
        let mut state = managed();
        let first = active_report(&state, false, 0).sources[0].source.clone();
        for epoch in 1..=1024 {
            let report = active_report(&state, true, 1);
            let input = row(&report.sources[0].source, 1);
            state.accept(input.clone());
            state.consume(&input.source, 1);
            let sealed = state.plan_producer_report(&report, 2).unwrap();
            state.apply_producer_lifecycle(sealed, 2).unwrap();
            let next = state.lifecycle_advances().unwrap().pop().unwrap();
            state.apply_producer_lifecycle(next, 2).unwrap();
            assert_eq!(state.cursors().count(), 0);
            assert_eq!(state.subscriptions().count(), 0);
            assert_eq!(state.producers().next().unwrap().retired_through, epoch);
        }
        let restored = SignalState::replay(&[rotation(&state)], 2).unwrap();
        assert_eq!(
            restored.classify(&row(&first, 1)).unwrap(),
            Admission::Duplicate
        );
        assert_eq!(restored.producers().count(), 1);
        assert_eq!(
            restored
                .producer_routes()
                .map(|route| route.subscriptions.len())
                .sum::<usize>(),
            1
        );
        assert_eq!(restored.cursors().count(), 0);
        assert_eq!(
            crate::signals::active_subscriptions(&[rotation(&state)]),
            vec![Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote
            }]
        );
        assert!(
            serde_json::to_vec(restored.producers().next().unwrap())
                .unwrap()
                .len()
                < 1024
        );
    }

    #[test]
    fn lifecycle_readiness_must_cover_every_granted_lane_and_cannot_unseal() {
        let mut state = managed();
        let mut partial = active_report(&state, false, 0);
        partial.sources.pop();
        assert!(state.plan_producer_report(&partial, 2).is_err());
        let sealed = state
            .plan_producer_report(&active_report(&state, true, 0), 2)
            .unwrap();
        state.apply_producer_lifecycle(sealed, 2).unwrap();
        assert!(state
            .plan_producer_report(&active_report(&state, false, 0), 2)
            .is_err());
    }
    #[test]
    fn lifecycle_retired_floor_dedups_only_the_granted_identity_and_final_tail() {
        let mut state = managed();
        let report = active_report(&state, true, 1);
        let original = row(&report.sources[0].source, 1);
        state.accept(original.clone());
        state.consume(&original.source, 1);
        let sealed = state.plan_producer_report(&report, 2).unwrap();
        state.apply_producer_lifecycle(sealed, 2).unwrap();
        let next = state.lifecycle_advances().unwrap().pop().unwrap();
        state.apply_producer_lifecycle(next, 2).unwrap();
        assert_eq!(state.classify(&original).unwrap(), Admission::Duplicate);
        let changed_generation = row(
            &original.source.replace(&"a".repeat(32), &"b".repeat(32)),
            1,
        );
        assert_eq!(
            state.classify(&changed_generation).unwrap(),
            Admission::Unregistered
        );
        let mut changed_destination = original.clone();
        changed_destination.destination = StrategyId(1);
        assert_eq!(
            state.classify(&changed_destination).unwrap(),
            Admission::Unregistered
        );
        assert_eq!(
            state.classify(&row(&original.source, 2)).unwrap(),
            Admission::Unregistered
        );
        let mut snapshot = serde_json::to_value(rotation(&state)).unwrap();
        snapshot["signal_cursors"] = serde_json::json!([{ "source":original.source, "sequence":1, "content_sha256":original.content_sha256 }]);
        snapshot["signal_subscriptions"] = serde_json::json!([{ "source":original.source, "destination":0, "subscriptions":original.subscriptions }]);
        let bad_rotation: WalRecord = serde_json::from_value(snapshot).unwrap();
        assert!(SignalState::replay(&[bad_rotation], 2)
            .unwrap_err()
            .contains("outside its current producer grant"));
        assert!(SignalState::replay(
            &[
                rotation(&state),
                WalRecord::SignalObservation {
                    wall_ts_ms: 3,
                    observation: original
                }
            ],
            2
        )
        .unwrap_err()
        .contains("outside its current producer grant"));
    }

    #[test]
    fn lifecycle_rotation_preserves_exact_grant_routes_and_predecessor_identity() {
        let state = managed();
        let base = rotation(&state);
        let mut wrong_seal = serde_json::to_value(&base).unwrap();
        wrong_seal["signal_producers"][0]["previous_seal"][0]["source"] =
            serde_json::json!("native.gbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.long");
        assert!(
            SignalState::replay(&[serde_json::from_value(wrong_seal).unwrap()], 2)
                .unwrap_err()
                .contains("predecessor seal changes identity")
        );
        let mut wrong_lane = serde_json::to_value(&base).unwrap();
        let carry = wrong_lane["signal_producers"][0]["active"]["sources"][1]["source"].clone();
        wrong_lane["signal_producers"][0]["active"]["sources"][0]["source"] = carry;
        assert!(
            SignalState::replay(&[serde_json::from_value(wrong_lane).unwrap()], 2)
                .unwrap_err()
                .contains("granted source routes")
        );
    }
}
