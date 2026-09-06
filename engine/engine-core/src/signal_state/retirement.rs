use super::*;
use engine_types::{LegacySignalSourceRetirement, ManagedSignalSource};

const MAX_LEGACY_SOURCE_RETIREMENTS: usize = 256;
const MAX_RETIREMENT_REASON_BYTES: usize = 4096;

impl SignalState {
    pub fn legacy_source_retirements(&self) -> impl Iterator<Item = &LegacySignalSourceRetirement> {
        self.legacy_retirements.values()
    }

    pub fn plan_legacy_source_retirement(
        &self,
        source: &str,
        published_through: u64,
        reason: &str,
        strategies: usize,
    ) -> Result<LegacySignalSourceRetirement, String> {
        if let Some(known) = self.legacy_retirements.get(source) {
            if known.published_through != published_through
                || known.reason != reason
                || known.destination.idx() >= strategies
            {
                return Err("operator retirement rewrites an immutable source outcome".into());
            }
            return Ok(known.clone());
        }
        let destination = self
            .destination(source)
            .ok_or("operator retirement requires a known legacy source")?;
        let retirement = LegacySignalSourceRetirement {
            source: source.to_owned(),
            destination,
            accepted_through: self.cursors.get(source).map_or(0, |row| row.sequence),
            published_through,
            reason: reason.to_owned(),
        };
        self.validate_legacy_retirement(&retirement, strategies)?;
        if self.observations.keys().any(|(known, _)| known == source) {
            return Err(
                "operator retirement cannot discard an accepted pending observation".into(),
            );
        }
        if self
            .gaps
            .get(source)
            .is_some_and(|gap| published_through < gap.observed_sequence)
            || self
                .producers
                .values()
                .flat_map(|state| &state.legacy)
                .any(|row| {
                    row.source == source
                        && row
                            .published_through
                            .is_some_and(|last| last != published_through)
                })
        {
            return Err("operator retirement changes a known publication frontier".into());
        }
        Ok(retirement)
    }

    pub fn apply_legacy_source_retirement(
        &mut self,
        retirement: LegacySignalSourceRetirement,
        strategies: usize,
    ) -> Result<(), String> {
        let expected = self.plan_legacy_source_retirement(
            &retirement.source,
            retirement.published_through,
            &retirement.reason,
            strategies,
        )?;
        if expected != retirement {
            return Err("operator retirement changes the accepted cursor or destination".into());
        }
        self.gaps.remove(&retirement.source);
        self.producer_frontiers.remove(&retirement.source);
        self.readiness_request_cursors.remove(&retirement.source);
        for state in self.producers.values_mut() {
            for source in &mut state.legacy {
                if source.source == retirement.source {
                    source.published_through = Some(retirement.published_through);
                }
            }
        }
        self.legacy_retirements
            .insert(retirement.source.clone(), retirement);
        Ok(())
    }

    fn validate_legacy_retirement(
        &self,
        retirement: &LegacySignalSourceRetirement,
        strategies: usize,
    ) -> Result<(), String> {
        if retirement.source.is_empty()
            || retirement.source.len() > 256
            || ManagedSignalSource::parse(&retirement.source).is_some()
            || retirement.destination.idx() >= strategies
            || retirement.reason.trim().is_empty()
            || retirement.reason.len() > MAX_RETIREMENT_REASON_BYTES
            || retirement.published_through < retirement.accepted_through
            || self
                .cursors
                .get(&retirement.source)
                .map_or(0, |row| row.sequence)
                != retirement.accepted_through
            || self
                .destination(&retirement.source)
                .is_some_and(|known| known != retirement.destination)
            || self.legacy_retirements.len() >= MAX_LEGACY_SOURCE_RETIREMENTS
            || self
                .producers
                .values()
                .flat_map(|state| &state.legacy)
                .any(|row| {
                    row.source == retirement.source
                        && (row.destination != retirement.destination
                            || row
                                .published_through
                                .is_some_and(|last| last != retirement.published_through))
                })
        {
            return Err("invalid or over-budget legacy source retirement".into());
        }
        Ok(())
    }

    pub(super) fn restore_legacy_source_retirement(
        &mut self,
        retirement: LegacySignalSourceRetirement,
        strategies: usize,
    ) -> Result<(), String> {
        self.validate_legacy_retirement(&retirement, strategies)?;
        if self.legacy_retirements.contains_key(&retirement.source)
            || self.gaps.contains_key(&retirement.source)
        {
            return Err("rotation repeats a retirement or retains its discarded gap".into());
        }
        self.legacy_retirements
            .insert(retirement.source.clone(), retirement);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{Feed, SignalProducerReport, SignalSourceFrontier, Wal};

    fn row(source: &str, sequence: u64) -> SignalObservation {
        let mut row = SignalObservation {
            schema_version: engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: "retirement-test".into(),
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

    fn stopped_legacy() -> (SignalState, Vec<WalRecord>, String) {
        let source = format!("native.g{}.long", "b".repeat(32));
        let observation = row(&source, 5);
        let mut records = vec![
            WalRecord::SignalObservation {
                wall_ts_ms: 2,
                observation: observation.clone(),
            },
            WalRecord::SignalObservationConsumed {
                wall_ts_ms: 3,
                strategy: StrategyId(0),
                source: source.clone(),
                sequence: 5,
                observation_id: observation.observation_id,
            },
            WalRecord::SignalGapRecorded {
                wall_ts_ms: 4,
                gap: SignalGap {
                    source: source.clone(),
                    destination: StrategyId(0),
                    next_sequence: 6,
                    observed_sequence: 7,
                },
            },
        ];
        let mut state = SignalState::replay(&records, 2).unwrap();
        let generation = "a".repeat(32);
        let report = SignalProducerReport {
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
        };
        let next = state.plan_producer_report(&report, 2).unwrap();
        records.push(WalRecord::SignalProducerLifecycle {
            wall_ts_ms: 5,
            state: next.clone(),
        });
        state.apply_producer_lifecycle(next, 2).unwrap();
        (state, records, source)
    }

    fn snapshot(state: &SignalState) -> WalRecord {
        serde_json::from_value(serde_json::json!({
            "kind":"segment_base_v7", "wall_ts_ms":9, "strategies":["long","carry"], "symbols":["BTCUSDT"],
            "may_open":true, "control_anchors":[], "attribution":[], "logged_exposure":[], "intended_stops":[], "open_orders":[],
            "open_trade_lots":[], "portfolio":engine_types::portfolio::PortfolioState::default(),
            "signal_observations":state.observations().collect::<Vec<_>>(), "signal_cursors":state.cursors().collect::<Vec<_>>(),
            "signal_subscriptions":state.subscriptions().collect::<Vec<_>>(), "signal_gaps":state.gaps().collect::<Vec<_>>(),
            "signal_producers":state.producers().collect::<Vec<_>>(),
            "legacy_signal_source_retirements":state.legacy_source_retirements().collect::<Vec<_>>(),
        })).unwrap()
    }

    #[test]
    fn operator_retirement_closes_only_the_explicit_lost_tail_without_consuming_it() {
        let (mut state, mut records, source) = stopped_legacy();
        let cursors = state.cursors().cloned().collect::<Vec<_>>();
        let mut seal = SignalProducerReport {
            producer: "native".into(),
            epoch: None,
            generation: "b".repeat(32),
            sealed: true,
            sources: vec![SignalSourceFrontier {
                source: source.clone(),
                destination: StrategyId(0),
                published_through: 7,
            }],
        };
        let next = state.plan_producer_report(&seal, 2).unwrap();
        records.push(WalRecord::SignalProducerLifecycle {
            wall_ts_ms: 6,
            state: next.clone(),
        });
        state.apply_producer_lifecycle(next, 2).unwrap();
        assert!(
            state.lifecycle_advances().unwrap().is_empty(),
            "ordinary seal waived a gap"
        );
        assert!(state.blocked(StrategyId(0)));
        let retirement = state
            .plan_legacy_source_retirement(
                &source,
                7,
                "lost legacy frame; retained final row discarded",
                2,
            )
            .unwrap();
        assert_eq!(retirement.accepted_through, 5);
        let record = WalRecord::LegacySignalSourceRetired {
            wall_ts_ms: 7,
            retirement: retirement.clone(),
        };
        records.push(record);
        state
            .apply_legacy_source_retirement(retirement.clone(), 2)
            .unwrap();
        state
            .apply_legacy_source_retirement(retirement.clone(), 2)
            .unwrap();
        assert_eq!(state.cursors().cloned().collect::<Vec<_>>(), cursors);
        assert_eq!(state.gaps().count(), 0);
        assert!(state
            .lifecycle_legacy_sources()
            .iter()
            .all(|row| row.source != source));
        for sequence in [5, 6, 7, 8] {
            assert_eq!(
                state.classify(&row(&source, sequence)).unwrap(),
                Admission::Unregistered
            );
        }
        let next = state
            .lifecycle_advances()
            .unwrap()
            .pop()
            .expect("explicit terminal outcome did not close the legacy tail");
        records.push(WalRecord::SignalProducerLifecycle {
            wall_ts_ms: 8,
            state: next.clone(),
        });
        state.apply_producer_lifecycle(next, 2).unwrap();
        assert!(!state.lifecycle_blocked(StrategyId(0)));
        assert_eq!(
            state.cursors().cloned().collect::<Vec<_>>(),
            cursors,
            "lifecycle closure erased the accepted cursor"
        );
        for restored in [
            SignalState::replay(&records, 2).unwrap(),
            SignalState::replay(&[snapshot(&state)], 2).unwrap(),
        ] {
            assert_eq!(restored.cursors().cloned().collect::<Vec<_>>(), cursors);
            assert_eq!(
                restored
                    .legacy_source_retirements()
                    .cloned()
                    .collect::<Vec<_>>(),
                vec![retirement.clone()]
            );
            assert_eq!(
                restored.classify(&row(&source, 6)).unwrap(),
                Admission::Unregistered
            );
            assert!(!restored.lifecycle_blocked(StrategyId(0)));
        }
        let mut pruned = state.producers().next().unwrap().clone();
        for route in &mut pruned.routes {
            route.subscriptions.clear();
        }
        state.apply_producer_lifecycle(pruned, 2).unwrap();
        let restored = SignalState::replay(&[snapshot(&state)], 2).unwrap();
        assert!(restored
            .subscriptions()
            .all(|route| route.subscriptions.is_empty()));
        assert_eq!(restored.cursors().cloned().collect::<Vec<_>>(), cursors);
        assert_eq!(restored.legacy_source_retirements().count(), 1);
        seal.sources[0].published_through = 6;
        assert!(state.plan_producer_report(&seal, 2).is_err());
        records.push(WalRecord::SignalObservation {
            wall_ts_ms: 10,
            observation: row(&source, 6),
        });
        assert!(SignalState::replay(&records, 2).is_err());
    }

    #[test]
    fn retirement_refuses_pending_work_false_frontiers_and_changed_retries() {
        let (mut state, _, source) = stopped_legacy();
        assert!(state
            .plan_legacy_source_retirement("unknown.long", 7, "reason", 2)
            .is_err());
        assert!(state
            .plan_legacy_source_retirement(&source, 6, "reason", 2)
            .is_err());
        assert!(state
            .plan_legacy_source_retirement(&source, 7, " ", 2)
            .is_err());
        assert!(state
            .plan_legacy_source_retirement(&source, 7, "reason", 0)
            .is_err());
        state.accept(row(&source, 6));
        assert!(state
            .plan_legacy_source_retirement(&source, 7, "reason", 2)
            .is_err());
        state.consume(&source, 6);
        let retirement = state
            .plan_legacy_source_retirement(&source, 7, "reason", 2)
            .unwrap();
        let mut corrupt = retirement.clone();
        corrupt.accepted_through = 7;
        assert!(state.apply_legacy_source_retirement(corrupt, 2).is_err());
        state.apply_legacy_source_retirement(retirement, 2).unwrap();
        assert!(state
            .plan_legacy_source_retirement(&source, 7, "reason", 0)
            .is_err());
        assert!(state
            .plan_legacy_source_retirement(&source, 8, "reason", 2)
            .is_err());
        assert!(state
            .plan_legacy_source_retirement(&source, 7, "different", 2)
            .is_err());
        let mut corrupt = serde_json::to_value(snapshot(&state)).unwrap();
        corrupt["legacy_signal_source_retirements"][0]["accepted_through"] = 7.into();
        assert!(SignalState::replay(&[serde_json::from_value(corrupt).unwrap()], 2).is_err());
        let managed = ManagedSignalSource {
            producer: "native",
            epoch: 1,
            generation: &"a".repeat(32),
            lane: engine_types::SignalLane::Long,
        }
        .encode()
        .unwrap();
        let mut state = SignalState::default();
        state.accept(row(&managed, 1));
        state.consume(&managed, 1);
        assert!(state
            .plan_legacy_source_retirement(&managed, 1, "reason", 2)
            .is_err());
    }

    #[test]
    fn retirement_is_bounded_and_identical_retry_does_not_use_another_slot() {
        let mut state = SignalState::default();
        for index in 0..=MAX_LEGACY_SOURCE_RETIREMENTS {
            let source = format!("legacy-{index}.long");
            state.accept(row(&source, 1));
            state.consume(&source, 1);
            let plan = state.plan_legacy_source_retirement(&source, 2, "known loss", 2);
            if index == MAX_LEGACY_SOURCE_RETIREMENTS {
                assert!(plan.is_err());
            } else {
                state
                    .apply_legacy_source_retirement(plan.unwrap(), 2)
                    .unwrap();
            }
        }
        let first = state.legacy_source_retirements().next().unwrap().clone();
        state.apply_legacy_source_retirement(first, 2).unwrap();
        assert_eq!(
            state.legacy_source_retirements().count(),
            MAX_LEGACY_SOURCE_RETIREMENTS
        );
        assert!(state
            .plan_legacy_source_retirement(
                "legacy-256.long",
                2,
                &"x".repeat(MAX_RETIREMENT_REASON_BYTES + 1),
                2
            )
            .is_err());
    }

    #[test]
    fn retirement_survives_wal_rotation_and_torn_operator_record_keeps_the_gap() {
        let (mut state, records, source) = stopped_legacy();
        let retirement = state
            .plan_legacy_source_retirement(&source, 7, "lost historical frame", 2)
            .unwrap();
        let record = WalRecord::LegacySignalSourceRetired {
            wall_ts_ms: 7,
            retirement: retirement.clone(),
        };
        let directory = std::env::temp_dir().join(format!(
            "legacy-retirement-{}-{}",
            std::process::id(),
            engine_types::clock::wall_ns()
        ));
        std::fs::create_dir(&directory).unwrap();
        let path = directory.join("engine.wal");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        for row in &records {
            wal.append(row).unwrap();
        }
        wal.barrier().unwrap();
        let before = std::fs::metadata(&path).unwrap().len() as usize;
        wal.append(&record).unwrap();
        wal.barrier().unwrap();
        drop(wal);
        let full = std::fs::read(&path).unwrap();
        for cut in [before + 1, before + 9, full.len() - 1, full.len()] {
            let cut_path = directory.join(format!("cut-{cut}.wal"));
            std::fs::write(&cut_path, &full[..cut]).unwrap();
            let (writer, records) = engine_wal::WalWriter::open(&cut_path).unwrap();
            drop(writer);
            let records = records.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
            let restored = SignalState::replay(&records, 2).unwrap();
            assert_eq!(restored.cursors().next().unwrap().sequence, 5);
            assert_eq!(
                restored.legacy_source_retirements().count(),
                usize::from(cut == full.len())
            );
            assert_eq!(restored.gaps().count(), usize::from(cut != full.len()));
        }
        state
            .apply_legacy_source_retirement(retirement.clone(), 2)
            .unwrap();
        let advanced = state.lifecycle_advances().unwrap().pop().unwrap();
        state.apply_producer_lifecycle(advanced.clone(), 2).unwrap();
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        wal.append(&WalRecord::SignalProducerLifecycle {
            wall_ts_ms: 8,
            state: advanced,
        })
        .unwrap();
        wal.rotate(&snapshot(&state)).unwrap();
        drop(wal);
        for replayed in [
            engine_wal::replay_chain(&path).unwrap().0,
            engine_wal::replay_current(&path).unwrap().0,
        ] {
            let records = replayed.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
            let restored = SignalState::replay(&records, 2).unwrap();
            assert_eq!(restored.cursors().next().unwrap().sequence, 5);
            assert_eq!(
                restored.legacy_source_retirements().next(),
                Some(&retirement)
            );
            assert_eq!(
                restored.classify(&row(&source, 6)).unwrap(),
                Admission::Unregistered
            );
            assert!(!restored.lifecycle_blocked(StrategyId(0)));
        }
        std::fs::remove_dir_all(directory).unwrap();
    }
}
