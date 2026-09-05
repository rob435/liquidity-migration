use std::collections::{BTreeMap, BTreeSet};

use engine_types::{
    SignalCursor, SignalGap, SignalObservation, SignalSubscriptionState, StrategyId, Subscription,
    WalRecord,
};

pub(crate) fn dependency_closure(
    names: &[String],
    dependencies: &[Vec<String>],
) -> Result<Vec<Vec<StrategyId>>, String> {
    if names.len() != dependencies.len() || names.len() > u16::MAX as usize + 1 {
        return Err("strategy dependency table has invalid dimensions".into());
    }
    let mut direct = Vec::with_capacity(names.len());
    for (owner, rows) in dependencies.iter().enumerate() {
        let mut ids = Vec::new();
        for name in rows {
            let matches: Vec<_> = names
                .iter()
                .enumerate()
                .filter(|(_, known)| *known == name)
                .map(|(index, _)| index)
                .collect();
            if matches.len() != 1 {
                return Err(format!(
                    "strategy {} input dependency {name:?} is absent or ambiguous",
                    names[owner]
                ));
            }
            ids.push(matches[0]);
        }
        direct.push(ids);
    }
    let mut closure = Vec::with_capacity(names.len());
    for owner in 0..names.len() {
        let mut pending = vec![owner];
        let mut visited = std::collections::BTreeSet::new();
        while let Some(next) = pending.pop() {
            if visited.insert(next) {
                pending.extend(&direct[next]);
            }
        }
        closure.push(
            visited
                .into_iter()
                .map(|id| StrategyId(id as u16))
                .collect(),
        );
    }
    Ok(closure)
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Admission {
    Duplicate,
    Ready,
    Gap(SignalGap),
}

#[derive(Debug, Default)]
pub(crate) struct SignalState {
    observations: BTreeMap<(String, u64), SignalObservation>,
    cursors: BTreeMap<String, SignalCursor>,
    subscriptions: BTreeMap<(String, u16), SignalSubscriptionState>,
    gaps: BTreeMap<String, SignalGap>,
    required_readiness: BTreeSet<StrategyId>,
    producer_frontiers: BTreeMap<String, engine_types::SignalSourceFrontier>,
    readiness_request_cursors: BTreeMap<String, u64>,
}

impl SignalState {
    pub fn replay(records: &[WalRecord], strategies: usize) -> Result<Self, String> {
        let mut state = Self::default();
        for record in records {
            match record {
                WalRecord::SignalObservation { observation, .. } => {
                    state.validate_destination(observation, strategies)?;
                    if state.gaps.contains_key(&observation.source)
                        && state.classify(observation)? != Admission::Ready
                    {
                        return Err(format!(
                            "signal {} #{} crosses a durable source gap",
                            observation.source, observation.sequence
                        ));
                    }
                    // Legacy accepted cursors can contain jumps. They are the
                    // migration boundary; erased input cannot be reconstructed.
                    state.accept(observation.clone());
                }
                WalRecord::SignalObservationRejected {
                    strategy,
                    source,
                    sequence,
                    observation_id,
                    ..
                }
                | WalRecord::SignalObservationConsumed {
                    strategy,
                    source,
                    sequence,
                    observation_id,
                    ..
                } => {
                    state.consumable(*strategy, source, *sequence, observation_id)?;
                    state.consume(source, *sequence);
                }
                WalRecord::SignalGapRecorded { gap, .. } => {
                    state.validate_gap(gap, strategies)?;
                    state.record_gap(gap.clone());
                }
                WalRecord::SegmentBase {
                    signal_observations,
                    signal_cursors,
                    signal_subscriptions,
                    signal_gaps,
                    ..
                } => {
                    state = Self::default();
                    for cursor in signal_cursors {
                        if cursor.sequence == 0
                            || cursor.source.is_empty()
                            || cursor.source.len() > 256
                            || cursor.content_sha256.len() != 64
                            || !cursor
                                .content_sha256
                                .bytes()
                                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                            || state
                                .cursors
                                .insert(cursor.source.clone(), cursor.clone())
                                .is_some()
                        {
                            return Err("invalid or repeated signal cursor in rotation".into());
                        }
                    }
                    for row in signal_subscriptions {
                        if row.destination.0 as usize >= strategies
                            || row.source.is_empty()
                            || row.source.len() > 256
                            || row.subscriptions.len()
                                > engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS
                            || state
                                .destination(&row.source)
                                .is_some_and(|id| id != row.destination)
                            || state
                                .subscriptions
                                .insert((row.source.clone(), row.destination.0), row.clone())
                                .is_some()
                        {
                            return Err(
                                "invalid or repeated signal subscription route in rotation".into(),
                            );
                        }
                    }
                    if state
                        .cursors
                        .keys()
                        .any(|source| state.destination(source).is_none())
                        || state
                            .subscriptions
                            .keys()
                            .any(|(source, _)| !state.cursors.contains_key(source))
                    {
                        return Err(
                            "rotated signal cursor and destination route are incomplete".into()
                        );
                    }
                    for gap in signal_gaps {
                        state.validate_gap(gap, strategies)?;
                        if state.gaps.insert(gap.source.clone(), gap.clone()).is_some() {
                            return Err("repeated signal gap in rotation".into());
                        }
                    }
                    for observation in signal_observations {
                        state.validate_destination(observation, strategies)?;
                        let cursor = state.cursors.get(&observation.source).ok_or_else(|| {
                            "rotated observation has no accepted cursor".to_string()
                        })?;
                        if observation.sequence > cursor.sequence
                            || (observation.sequence == cursor.sequence
                                && observation.content_sha256 != cursor.content_sha256)
                            || observation.subscriptions.iter().any(|subscription| {
                                !state
                                    .route_subscriptions(
                                        &observation.source,
                                        observation.destination,
                                    )
                                    .contains(subscription)
                            })
                            || state
                                .observations
                                .insert(
                                    (observation.source.clone(), observation.sequence),
                                    observation.clone(),
                                )
                                .is_some()
                        {
                            return Err(
                                "rotated observation disagrees with its accepted cursor".into()
                            );
                        }
                    }
                }
                _ => {}
            }
        }
        Ok(state)
    }

    pub fn require_readiness(&mut self, strategies: impl IntoIterator<Item = StrategyId>) {
        self.required_readiness = strategies.into_iter().collect();
        self.begin_readiness_request();
    }

    pub fn readiness_required(&self) -> bool {
        !self.required_readiness.is_empty()
    }

    pub fn begin_readiness_request(&mut self) {
        self.clear_readiness();
        self.readiness_request_cursors = self
            .cursors
            .values()
            .map(|cursor| (cursor.source.clone(), cursor.sequence))
            .collect();
    }

    pub fn clear_readiness(&mut self) {
        self.producer_frontiers.clear();
    }

    pub fn readiness_blocked(&self, destination: StrategyId) -> bool {
        self.required_readiness.contains(&destination)
            && (!self
                .producer_frontiers
                .values()
                .any(|row| row.destination == destination)
                || self
                    .producer_frontiers
                    .values()
                    .filter(|row| row.destination == destination)
                    .any(|row| {
                        self.cursors
                            .get(&row.source)
                            .map_or(0, |cursor| cursor.sequence)
                            < row.published_through
                    }))
    }

    pub fn frontier_gaps(
        &self,
        frontiers: &[engine_types::SignalSourceFrontier],
        strategies: usize,
    ) -> Result<Vec<SignalGap>, String> {
        if frontiers.len() > crate::signals::MAX_SIGNAL_GAP_REQUESTS {
            return Err("producer readiness contains too many source frontiers".into());
        }
        let mut seen = BTreeSet::new();
        let mut gaps = Vec::new();
        for row in frontiers {
            if row.source.is_empty()
                || row.source.len() > 256
                || row.destination.idx() >= strategies
                || !seen.insert(&row.source)
                || self
                    .destination(&row.source)
                    .is_some_and(|known| known != row.destination)
            {
                return Err("producer readiness contains an invalid source identity".into());
            }
            let accepted = self
                .cursors
                .get(&row.source)
                .map_or(0, |cursor| cursor.sequence);
            let requested = self
                .readiness_request_cursors
                .get(&row.source)
                .copied()
                .unwrap_or(0);
            if row.published_through < requested {
                return Err(format!(
                    "producer {} rewound to {} behind durable cursor {} at readiness request",
                    row.source, row.published_through, requested
                ));
            }
            if row.published_through <= accepted {
                continue;
            }
            let next = self.next_sequence(&row.source)?;
            if row.published_through >= next {
                gaps.push(SignalGap {
                    source: row.source.clone(),
                    destination: row.destination,
                    next_sequence: next,
                    observed_sequence: row.published_through.max(
                        self.gaps
                            .get(&row.source)
                            .map_or(0, |gap| gap.observed_sequence),
                    ),
                });
            }
        }
        Ok(gaps)
    }

    pub fn set_frontiers(&mut self, frontiers: Vec<engine_types::SignalSourceFrontier>) {
        self.producer_frontiers = frontiers
            .into_iter()
            .map(|row| (row.source.clone(), row))
            .collect();
    }

    pub fn consumer_pending(&self, destination: StrategyId) -> bool {
        self.observations
            .values()
            .any(|row| row.destination == destination)
    }

    pub fn ordinary_capacity(&self) -> bool {
        self.observations.len() < crate::signals::SIGNAL_CHANNEL_CAPACITY
            && self.retained_bytes() < crate::signals::SIGNAL_CHANNEL_BYTES
    }

    pub fn prefix_capacity(&self, destination: StrategyId) -> bool {
        (!self.consumer_pending(destination) && self.ordinary_capacity())
            || self.recovery_capacity()
    }

    fn recovery_capacity(&self) -> bool {
        let mut destinations = BTreeSet::new();
        self.observations.len() <= crate::signals::SIGNAL_CHANNEL_CAPACITY
            && self.retained_bytes() <= crate::signals::SIGNAL_CHANNEL_BYTES
            && !self
                .observations
                .values()
                .any(|row| !destinations.insert(row.destination))
    }

    pub fn can_accept(&self, observation: &SignalObservation) -> bool {
        let bytes = crate::signals::retained_bytes(observation);
        let retained = self.retained_bytes();
        let ordinary = !self.consumer_pending(observation.destination)
            && self.observations.len() < crate::signals::SIGNAL_CHANNEL_CAPACITY
            && retained.saturating_add(bytes) <= crate::signals::SIGNAL_CHANNEL_BYTES;
        let recovery = self
            .gaps
            .get(&observation.source)
            .is_some_and(|gap| gap.next_sequence == observation.sequence)
            && self.recovery_capacity();
        bytes <= crate::signals::MAX_SIGNAL_RETAINED_BYTES && (ordinary || recovery)
    }

    fn retained_bytes(&self) -> usize {
        self.observations.values().fold(0usize, |bytes, row| {
            bytes.saturating_add(crate::signals::retained_bytes(row))
        })
    }

    pub fn classify(&self, observation: &SignalObservation) -> Result<Admission, String> {
        if self
            .destination(&observation.source)
            .is_some_and(|id| id != observation.destination)
        {
            return Err(format!(
                "signal source {} changed its strategy destination",
                observation.source
            ));
        }
        if let Some(cursor) = self.cursors.get(&observation.source) {
            if observation.sequence <= cursor.sequence {
                if observation.sequence == cursor.sequence
                    && observation.content_sha256 != cursor.content_sha256
                {
                    return Err(format!(
                        "signal source {} rewrote durable sequence {}",
                        observation.source, observation.sequence
                    ));
                }
                return Ok(Admission::Duplicate);
            }
        }
        let next_sequence = self.next_sequence(&observation.source)?;
        if observation.sequence == next_sequence {
            return Ok(Admission::Ready);
        }
        Ok(Admission::Gap(SignalGap {
            source: observation.source.clone(),
            destination: observation.destination,
            next_sequence,
            observed_sequence: self
                .gaps
                .get(&observation.source)
                .map_or(observation.sequence, |gap| {
                    gap.observed_sequence.max(observation.sequence)
                }),
        }))
    }

    pub fn gap_changed(&self, gap: &SignalGap) -> bool {
        self.gaps.get(&gap.source) != Some(gap)
    }

    pub fn record_gap(&mut self, gap: SignalGap) {
        self.gaps.insert(gap.source.clone(), gap);
    }

    pub fn blocked(&self, strategy: StrategyId) -> bool {
        self.gaps.values().any(|gap| gap.destination == strategy)
    }

    pub fn accept(&mut self, observation: SignalObservation) {
        self.cursors.insert(
            observation.source.clone(),
            SignalCursor {
                source: observation.source.clone(),
                sequence: observation.sequence,
                content_sha256: observation.content_sha256.clone(),
            },
        );
        if let Some(gap) = self.gaps.get_mut(&observation.source) {
            if observation.sequence >= gap.observed_sequence {
                self.gaps.remove(&observation.source);
            } else {
                gap.next_sequence = observation.sequence + 1;
            }
        }
        let row = self
            .subscriptions
            .entry((observation.source.clone(), observation.destination.0))
            .or_insert_with(|| SignalSubscriptionState {
                source: observation.source.clone(),
                destination: observation.destination,
                subscriptions: Vec::new(),
            });
        for subscription in &observation.subscriptions {
            if !row.subscriptions.contains(subscription) {
                row.subscriptions.push(subscription.clone());
            }
        }
        self.observations.insert(
            (observation.source.clone(), observation.sequence),
            observation,
        );
    }

    pub fn consumable(
        &self,
        strategy: StrategyId,
        source: &str,
        sequence: u64,
        id: &str,
    ) -> Result<bool, String> {
        let Some(observation) = self.observations.get(&(source.to_string(), sequence)) else {
            return Ok(false);
        };
        if observation.destination != strategy || observation.observation_id != id {
            return Err(format!(
                "strategy {} cannot consume signal {source} #{sequence} {id}",
                strategy.0
            ));
        }
        Ok(true)
    }

    pub fn consume(&mut self, source: &str, sequence: u64) {
        self.observations.remove(&(source.to_string(), sequence));
    }

    pub fn route_subscriptions(&self, source: &str, destination: StrategyId) -> &[Subscription] {
        self.subscriptions
            .get(&(source.to_string(), destination.0))
            .map_or(&[], |row| row.subscriptions.as_slice())
    }

    pub fn observations(&self) -> impl Iterator<Item = &SignalObservation> {
        self.observations.values()
    }
    pub fn cursors(&self) -> impl Iterator<Item = &SignalCursor> {
        self.cursors.values()
    }
    pub fn subscriptions(&self) -> impl Iterator<Item = &SignalSubscriptionState> {
        self.subscriptions.values()
    }
    pub fn gaps(&self) -> impl Iterator<Item = &SignalGap> {
        self.gaps.values()
    }

    fn destination(&self, source: &str) -> Option<StrategyId> {
        self.gaps
            .get(source)
            .map(|gap| gap.destination)
            .or_else(|| {
                self.subscriptions
                    .range((source.to_string(), 0)..=(source.to_string(), u16::MAX))
                    .next()
                    .map(|(_, row)| row.destination)
            })
    }

    fn next_sequence(&self, source: &str) -> Result<u64, String> {
        self.cursors.get(source).map_or(Ok(1), |cursor| {
            cursor
                .sequence
                .checked_add(1)
                .ok_or_else(|| format!("signal source {source} exhausted its sequence range"))
        })
    }

    fn validate_destination(
        &self,
        observation: &SignalObservation,
        strategies: usize,
    ) -> Result<(), String> {
        crate::signals::validate(observation)?;
        if observation.destination.0 as usize >= strategies
            || self
                .destination(&observation.source)
                .is_some_and(|id| id != observation.destination)
        {
            return Err(format!(
                "signal source {} has an invalid or changed strategy destination",
                observation.source
            ));
        }
        Ok(())
    }

    fn validate_gap(&self, gap: &SignalGap, strategies: usize) -> Result<(), String> {
        if gap.destination.0 as usize >= strategies
            || gap.source.is_empty()
            || gap.source.len() > 256
            || gap.next_sequence != self.next_sequence(&gap.source)?
            || gap.observed_sequence < gap.next_sequence
            || self
                .destination(&gap.source)
                .is_some_and(|id| id != gap.destination)
            || self
                .gaps
                .get(&gap.source)
                .is_some_and(|old| old.observed_sequence > gap.observed_sequence)
        {
            return Err(format!("invalid durable signal gap for {}", gap.source));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dependencies_are_transitive_scoped_and_resolve_exact_names() {
        let names = vec![
            "source".into(),
            "consumer".into(),
            "child".into(),
            "independent".into(),
        ];
        let closure = dependency_closure(
            &names,
            &[
                vec![],
                vec!["source".into()],
                vec!["consumer".into()],
                vec![],
            ],
        )
        .unwrap();
        assert_eq!(
            closure,
            vec![
                vec![StrategyId(0)],
                vec![StrategyId(0), StrategyId(1)],
                vec![StrategyId(0), StrategyId(1), StrategyId(2)],
                vec![StrategyId(3)]
            ]
        );
        assert!(
            dependency_closure(&names, &[vec!["absent".into()], vec![], vec![], vec![]]).is_err()
        );
        assert!(dependency_closure(
            &["same".into(), "same".into()],
            &[vec!["same".into()], vec![]]
        )
        .is_err());
    }

    fn observation(source: &str, sequence: u64, destination: u16) -> SignalObservation {
        let mut row = SignalObservation {
            schema_version: engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: "test".into(),
            destination: StrategyId(destination),
            source: source.into(),
            sequence,
            observation_id: format!("{source}-{sequence}"),
            kind: "test".into(),
            observed_wall_ts_ms: 1,
            available_wall_ts_ms: 2,
            subscriptions: Vec::new(),
            payload: vec![1],
            content_sha256: String::new(),
        };
        row.content_sha256 = crate::signals::content_sha256(&row);
        row
    }

    fn accepted(row: SignalObservation) -> WalRecord {
        WalRecord::SignalObservation {
            wall_ts_ms: 2,
            observation: row,
        }
    }

    fn gap_record(source: &str, destination: u16, next: u64, observed: u64) -> WalRecord {
        WalRecord::SignalGapRecorded {
            wall_ts_ms: 3,
            gap: SignalGap {
                source: source.into(),
                destination: StrategyId(destination),
                next_sequence: next,
                observed_sequence: observed,
            },
        }
    }

    #[test]
    fn gap_state_recovers_only_through_its_contiguous_high_water() {
        let mut records = vec![
            accepted(observation("source", 9, 0)),
            gap_record("source", 0, 10, 12),
        ];
        let state = SignalState::replay(&records, 2).unwrap();
        assert!(state.blocked(StrategyId(0)));
        assert!(!state.blocked(StrategyId(1)));
        assert!(matches!(
            state.classify(&observation("source", 11, 0)).unwrap(),
            Admission::Gap(_)
        ));
        assert_eq!(
            state.classify(&observation("source", 10, 0)).unwrap(),
            Admission::Ready
        );
        records.push(accepted(observation("source", 10, 0)));
        records.push(accepted(observation("independent", 1, 1)));
        let state = SignalState::replay(&records, 2).unwrap();
        assert!(state.blocked(StrategyId(0)));
        assert_eq!(state.gaps().next().unwrap().next_sequence, 11);
        records.push(accepted(observation("source", 11, 0)));
        assert!(SignalState::replay(&records, 2)
            .unwrap()
            .blocked(StrategyId(0)));
        records.push(accepted(observation("source", 12, 0)));
        assert!(SignalState::replay(&records, 2)
            .unwrap()
            .gaps()
            .next()
            .is_none());
    }

    #[test]
    fn a_new_generation_cannot_erase_an_old_generation_gap() {
        let state = SignalState::replay(
            &[
                accepted(observation("worker.g1", 9, 0)),
                gap_record("worker.g1", 0, 10, 11),
                accepted(observation("worker.g2", 1, 0)),
            ],
            1,
        )
        .unwrap();
        assert!(state.blocked(StrategyId(0)));
        assert_eq!(state.gaps().next().unwrap().source, "worker.g1");
        assert_eq!(
            state.classify(&observation("worker.g2", 2, 0)).unwrap(),
            Admission::Ready
        );
    }

    #[test]
    fn source_destination_is_stable_before_and_after_acceptance() {
        let state = SignalState::replay(&[gap_record("new", 0, 1, 3)], 2).unwrap();
        assert!(state
            .classify(&observation("new", 1, 1))
            .unwrap_err()
            .contains("destination"));
        let state = SignalState::replay(&[accepted(observation("known", 1, 0))], 2).unwrap();
        assert!(state
            .classify(&observation("known", 2, 1))
            .unwrap_err()
            .contains("destination"));
        assert!(SignalState::replay(
            &[
                accepted(observation("known", 1, 0)),
                accepted(observation("known", 2, 1))
            ],
            2
        )
        .is_err());
    }

    #[test]
    fn replay_refuses_a_jump_across_a_recorded_gap_and_an_impossible_gap_cursor() {
        assert!(SignalState::replay(
            &[
                accepted(observation("source", 9, 0)),
                gap_record("source", 0, 10, 11),
                accepted(observation("source", 11, 0)),
            ],
            1
        )
        .is_err());
        assert!(SignalState::replay(
            &[
                accepted(observation("source", 9, 0)),
                gap_record("source", 0, 11, 12),
            ],
            1
        )
        .is_err());
    }

    #[test]
    fn source_sequence_exhaustion_never_wraps_into_a_new_prefix() {
        let state =
            SignalState::replay(&[accepted(observation("source", u64::MAX, 0))], 1).unwrap();
        assert_eq!(
            state.classify(&observation("source", 1, 0)).unwrap(),
            Admission::Duplicate
        );
        assert_eq!(
            state.classify(&observation("source", u64::MAX, 0)).unwrap(),
            Admission::Duplicate
        );
        assert!(state.next_sequence("source").is_err());
    }

    #[test]
    fn rewritten_current_sequence_is_not_acknowledged_as_a_duplicate() {
        let row = observation("source", 1, 0);
        let state = SignalState::replay(&[accepted(row.clone())], 1).unwrap();
        let mut rewritten = row;
        rewritten.payload.push(2);
        rewritten.content_sha256 = crate::signals::content_sha256(&rewritten);
        assert!(state.classify(&rewritten).unwrap_err().contains("rewrote"));
    }

    #[test]
    fn consuming_requires_the_addressed_strategy_and_exact_observation_id() {
        let row = observation("source", 1, 0);
        let mut state = SignalState::replay(&[accepted(row.clone())], 2).unwrap();
        assert!(state
            .consumable(StrategyId(1), "source", 1, &row.observation_id)
            .is_err());
        assert!(state
            .consumable(StrategyId(0), "source", 1, "wrong")
            .is_err());
        assert!(state
            .consumable(StrategyId(0), "source", 1, &row.observation_id)
            .unwrap());
        state.consume("source", 1);
        assert!(!state
            .consumable(StrategyId(0), "source", 1, &row.observation_id)
            .unwrap());
        assert_eq!(state.cursors().next().unwrap().sequence, 1);
    }

    #[test]
    fn rotation_rejects_a_missing_destination_route_or_subscription_union() {
        let mut row = observation("source", 1, 0);
        row.subscriptions.push(Subscription {
            symbol: "BTCUSDT".into(),
            feed: engine_types::Feed::Quote,
        });
        row.content_sha256 = crate::signals::content_sha256(&row);
        let state = SignalState::replay(&[accepted(row)], 1).unwrap();
        let value = serde_json::json!({
            "kind": "segment_base_v2", "wall_ts_ms": 2, "strategies": ["one"], "symbols": ["BTCUSDT"],
            "may_open": true, "control_anchors": [], "attribution": [], "logged_exposure": [],
            "intended_stops": [], "recent_execution_ids": [], "target_book_latches": [], "open_orders": [],
            "signal_observations": state.observations().collect::<Vec<_>>(),
            "signal_cursors": state.cursors().collect::<Vec<_>>(),
            "signal_subscriptions": state.subscriptions().collect::<Vec<_>>(), "signal_gaps": [],
        });
        let record = serde_json::from_value(value.clone()).unwrap();
        assert!(SignalState::replay(&[record], 1).is_ok());
        let mut missing_route = value.clone();
        missing_route["signal_subscriptions"] = serde_json::json!([]);
        assert!(
            SignalState::replay(&[serde_json::from_value(missing_route).unwrap()], 1)
                .unwrap_err()
                .contains("incomplete")
        );
        let mut missing_subscription = value;
        missing_subscription["signal_subscriptions"][0]["subscriptions"] = serde_json::json!([]);
        assert!(
            SignalState::replay(&[serde_json::from_value(missing_subscription).unwrap()], 1)
                .is_err()
        );
    }
    #[test]
    fn retained_input_capacity_keeps_one_prefix_recovery_slot_across_replay() {
        let mut records = (0..crate::signals::SIGNAL_CHANNEL_CAPACITY)
            .map(|index| accepted(observation(&format!("source-{index}"), 1, index as u16)))
            .collect::<Vec<_>>();
        records.push(gap_record("source-0", 0, 2, 3));
        let mut state = SignalState::replay(&records, 256).unwrap();
        assert!(!state.can_accept(&observation("other", 1, 1)));
        let missing = observation("source-0", 2, 0);
        assert!(
            state.can_accept(&missing),
            "capacity must retain prefix recovery"
        );
        records.push(accepted(missing.clone()));
        state.accept(missing);
        assert!(
            !state.can_accept(&observation("source-0", 3, 0)),
            "one recovery delivery cannot grow without a consumer outcome"
        );
        let mut restored = SignalState::replay(&records, 256).unwrap();
        assert!(!restored.can_accept(&observation("source-0", 3, 0)));
        restored.consume("source-0", 2);
        assert!(restored.can_accept(&observation("source-0", 3, 0)));
    }

    #[test]
    fn explicit_rejection_replays_as_terminal_without_claiming_success() {
        let row = observation("worker.g1", 1, 0);
        let rejected = WalRecord::SignalObservationRejected {
            wall_ts_ms: 3,
            strategy: row.destination,
            source: row.source.clone(),
            sequence: row.sequence,
            observation_id: row.observation_id.clone(),
            reason: "malformed payload".into(),
        };
        let records = vec![accepted(row.clone()), rejected.clone()];
        let decoded: WalRecord =
            serde_json::from_str(&serde_json::to_string(&rejected).unwrap()).unwrap();
        assert_eq!(decoded, rejected);
        let state = SignalState::replay(&records, 1).unwrap();
        assert_eq!(state.observations().count(), 0);
        assert_eq!(state.classify(&row).unwrap(), Admission::Duplicate);
        assert_eq!(
            SignalState::replay(&records[..1], 1)
                .unwrap()
                .observations()
                .count(),
            1,
            "an interrupted rejection keeps the durable accepted payload"
        );
    }

    #[test]
    fn producer_frontier_requires_catchup_without_waiving_old_generation_gaps() {
        use engine_types::SignalSourceFrontier;
        let mut state = SignalState::replay(
            &[
                accepted(observation("worker.g1", 9, 0)),
                gap_record("worker.g1", 0, 10, 11),
            ],
            2,
        )
        .unwrap();
        state.require_readiness([StrategyId(0)]);
        assert!(state.readiness_blocked(StrategyId(0)));
        assert!(!state.readiness_blocked(StrategyId(1)));
        let ready = vec![SignalSourceFrontier {
            source: "worker.g2".into(),
            destination: StrategyId(0),
            published_through: 0,
        }];
        assert!(state.frontier_gaps(&ready, 2).unwrap().is_empty());
        state.set_frontiers(ready);
        assert!(!state.readiness_blocked(StrategyId(0)));
        assert!(
            state.blocked(StrategyId(0)),
            "new producer participation must retain the old missing history"
        );
        let ready = vec![SignalSourceFrontier {
            source: "worker.g1".into(),
            destination: StrategyId(0),
            published_through: 12,
        }];
        let gaps = state.frontier_gaps(&ready, 2).unwrap();
        assert_eq!(gaps[0].observed_sequence, 12);
        for gap in gaps {
            state.record_gap(gap);
        }
        state.set_frontiers(ready);
        for seq in 10..=12 {
            assert!(state.readiness_blocked(StrategyId(0)));
            state.accept(observation("worker.g1", seq, 0));
        }
        assert!(!state.readiness_blocked(StrategyId(0)));
        assert!(!state.blocked(StrategyId(0)));
        state.clear_readiness();
        assert!(
            state.readiness_blocked(StrategyId(0)),
            "readiness never survives a producer disconnect"
        );
    }

    #[test]
    fn a_rewound_producer_cannot_declare_its_generation_ready() {
        let mut state =
            SignalState::replay(&[accepted(observation("worker.g1", 100, 0))], 1).unwrap();
        state.require_readiness([StrategyId(0)]);
        let frontiers = [engine_types::SignalSourceFrontier {
            source: "worker.g1".into(),
            destination: StrategyId(0),
            published_through: 0,
        }];
        assert!(
            state.frontier_gaps(&frontiers, 1).is_err(),
            "a same-generation producer behind the durable accepted cursor must not waive history"
        );
        assert!(state.readiness_blocked(StrategyId(0)));
    }

    #[test]
    fn readiness_response_can_trail_rows_accepted_after_its_request() {
        let mut state =
            SignalState::replay(&[accepted(observation("worker.g1", 100, 0))], 1).unwrap();
        state.require_readiness([StrategyId(0)]);
        state.accept(observation("worker.g1", 101, 0));
        let frontiers = [engine_types::SignalSourceFrontier {
            source: "worker.g1".into(),
            destination: StrategyId(0),
            published_through: 100,
        }];
        assert!(
            state.frontier_gaps(&frontiers, 1).is_ok(),
            "readiness compares against the request cursor, not observations racing the response"
        );
    }
}
