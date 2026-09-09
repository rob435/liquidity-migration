//! Canonical native strategy state initialization and stopped verification.

use std::error::Error;
use std::path::Path;

use engine_types::{
    StrategyCheckpoint, StrategyCheckpointIdentity, StrategyGlobalCheckpointState, StrategyId,
    VenueGateway, Wal, WalRecord, MAX_STRATEGY_STATE_BYTES,
};

use crate::{assembly, clock, config};

const INITIALIZE_LEASE_ROLE: &str = "strategy-state-initialize";

mod rebind;
pub use rebind::rebind_native_strategy_state;

fn validate_checkpoint_contract(
    strategy: &dyn engine_types::Strategy,
    identity: &StrategyCheckpointIdentity,
    checkpoint: &StrategyCheckpoint,
) -> Result<(), Box<dyn Error>> {
    if identity.schema_version == 0
        || identity.decision_fingerprint.is_empty()
        || identity.decision_fingerprint.len() > 256
    {
        return Err("strategy returned an invalid checkpoint identity".into());
    }
    if checkpoint.schema_version != identity.schema_version
        || checkpoint.decision_fingerprint != identity.decision_fingerprint
    {
        return Err(format!(
            "checkpoint identity ({}, {:?}) does not match configured ({}, {:?})",
            checkpoint.schema_version,
            checkpoint.decision_fingerprint,
            identity.schema_version,
            identity.decision_fingerprint
        )
        .into());
    }
    if checkpoint.payload.len() > MAX_STRATEGY_STATE_BYTES {
        return Err(format!(
            "checkpoint is {} bytes; maximum is {}",
            checkpoint.payload.len(),
            MAX_STRATEGY_STATE_BYTES
        )
        .into());
    }
    strategy
        .validate_checkpoint(checkpoint)
        .map_err(|error| format!("strategy refused canonical checkpoint: {error}"))?;
    Ok(())
}

fn configured_names(configured: &[config::StrategyConfig]) -> Vec<String> {
    configured
        .iter()
        .map(|row| row.sleeve_name().to_string())
        .collect()
}

fn initial_state_record(
    configured: &[String],
    strategies: &[Box<dyn engine_types::Strategy>],
) -> Result<WalRecord, Box<dyn Error>> {
    let mut checkpoints = Vec::new();
    for (index, strategy) in strategies.iter().enumerate() {
        let Some(identity) = strategy.checkpoint_identity() else {
            if strategy.initial_checkpoint().is_some() {
                return Err(format!(
                    "strategy {:?} provides initial state without a checkpoint identity",
                    configured[index]
                )
                .into());
            }
            continue;
        };
        let checkpoint = strategy.initial_checkpoint().ok_or_else(|| {
            format!(
                "strategy {:?} declares whole-sleeve state but no canonical initial checkpoint",
                configured[index]
            )
        })?;
        validate_checkpoint_contract(strategy.as_ref(), &identity, &checkpoint).map_err(
            |error| {
                format!(
                    "strategy {:?} refused its canonical initial checkpoint: {error}",
                    configured[index]
                )
            },
        )?;
        checkpoints.push(StrategyGlobalCheckpointState {
            strategy: StrategyId(u16::try_from(index)?),
            checkpoint,
            provenance: None,
        });
    }
    if checkpoints.is_empty() {
        return Err("config has no strategy with a whole-sleeve checkpoint contract".into());
    }
    let wall_ts_ms = clock::wall_ms();
    Ok(WalRecord::SegmentBase {
        order_id_epoch_ms: None,
        open_trade_lots: Some(Vec::new()),
        legacy_signal_source_retirements: Vec::new(),
        portfolio_control: Default::default(),
        portfolio: Some(Default::default()),
        pending_order_dispatches: Vec::new(),
        signal_producers: Vec::new(),
        identities: Some(
            crate::identities::plan_identities(&[], configured, None, &Default::default(), &[])?
                .state,
        ),
        instrument_catalog: None,
        signal_suspensions: Vec::new(),
        strategy_processes: Vec::new(),
        strategy_callback_queues: Vec::new(),
        strategy_callback_sources: Vec::new(),
        signal_callback_deliveries: Vec::new(),
        strategy_callbacks: Vec::new(),
        wall_ts_ms,
        strategies: configured.to_vec(),
        symbols: Vec::new(),
        may_open: true,
        control_anchors: Vec::new(),
        attribution: Vec::new(),
        logged_exposure: Vec::new(),
        intended_stops: Vec::new(),
        recent_execution_ids: Vec::new(),
        // This locked stopped-runtime handoff is the first instant whose
        // executions belong to the Rust WAL. Earlier holdings live in the
        // imported/initial reducer state and account view, not as Rust fills.
        execution_history_through_ms: Some(wall_ts_ms),
        target_book_latches: Vec::new(),
        strategy_checkpoints: Vec::new(),
        strategy_global_checkpoints: checkpoints,
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
        owed_markouts: Vec::new(),
    })
}

fn verify_records(
    configured: &[String],
    strategies: &[Box<dyn engine_types::Strategy>],
    replayed: &[WalRecord],
) -> Result<(), Box<dyn Error>> {
    verify_names(configured, replayed)?;
    let mut current = std::collections::BTreeMap::new();
    for record in replayed {
        match record {
            WalRecord::StrategyGlobalCheckpoint {
                strategy,
                checkpoint,
                provenance,
                ..
            } => {
                current.insert(
                    strategy.0,
                    StrategyGlobalCheckpointState {
                        strategy: *strategy,
                        checkpoint: checkpoint.clone(),
                        provenance: provenance.clone(),
                    },
                );
            }
            WalRecord::SegmentBase {
                strategy_global_checkpoints,
                ..
            } => {
                current = strategy_global_checkpoints
                    .iter()
                    .map(|state| (state.strategy.0, state.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    if let Some(owner) = current
        .keys()
        .find(|owner| usize::from(**owner) >= strategies.len())
    {
        return Err(format!(
            "whole-sleeve checkpoint names strategy {owner} outside the configured table"
        )
        .into());
    }
    for (index, strategy) in strategies.iter().enumerate() {
        let owner = StrategyId(u16::try_from(index)?);
        if !strategy.callback_enabled() {
            continue;
        }
        let state = current.get(&owner.0);
        match (strategy.checkpoint_identity(), state) {
            (Some(identity), Some(state)) => {
                if state
                    .provenance
                    .as_ref()
                    .is_some_and(|proof| !proof.import_complete)
                {
                    return Err(format!(
                        "strategy {:?} has an incomplete stopped-runtime import",
                        configured[index]
                    )
                    .into());
                }
                validate_checkpoint_contract(strategy.as_ref(), &identity, &state.checkpoint)
                    .map_err(|error| {
                        format!(
                            "strategy {:?} checkpoint is invalid: {error}",
                            configured[index]
                        )
                    })?;
            }
            (Some(_), None) => {
                return Err(format!(
                    "strategy {:?} has no whole-sleeve checkpoint",
                    configured[index]
                )
                .into());
            }
            (None, Some(_)) => {
                return Err(format!(
                    "strategy {:?} has whole-sleeve state but no configured checkpoint contract",
                    configured[index]
                )
                .into());
            }
            (None, None) => {}
        }
    }
    Ok(())
}

fn verify_expected_account(
    who: &engine_types::AccountIdentity,
    expected: &str,
) -> Result<(), Box<dyn Error>> {
    if expected.is_empty() {
        return Err("EXPECTED_ENGINE_ACCOUNT_USER_ID is empty".into());
    }
    if expected != who.user_id {
        return Err(format!(
            "authenticated account user id {:?} does not match EXPECTED_ENGINE_ACCOUNT_USER_ID {:?}",
            who.user_id, expected
        )
        .into());
    }
    Ok(())
}

fn require_expected_account(who: &engine_types::AccountIdentity) -> Result<(), Box<dyn Error>> {
    let expected = std::env::var("EXPECTED_ENGINE_ACCOUNT_USER_ID")
        .map_err(|_| "EXPECTED_ENGINE_ACCOUNT_USER_ID is required for strategy state changes")?;
    verify_expected_account(who, &expected)
}

fn verify_names(configured: &[String], replayed: &[WalRecord]) -> Result<(), Box<dyn Error>> {
    let logged = crate::identities::replay_identities(replayed)?
        .unwrap_or_default()
        .sleeves
        .iter()
        .map(|key| key.as_str().to_string())
        .collect::<Vec<_>>();
    if logged.is_empty() {
        return Err("the nonempty WAL has no Names strategy table".into());
    }
    if !configured.starts_with(logged.as_slice()) {
        return Err(format!(
            "config strategy order {:?} does not preserve the WAL Names prefix {:?}",
            configured, logged
        )
        .into());
    }
    Ok(())
}

async fn account_identity(
    chosen: engine_venue::VenueName,
) -> Result<engine_types::AccountIdentity, Box<dyn Error>> {
    if let Ok(mut probe) = assembly::inventory_probe(chosen) {
        return Ok(probe.account_identity().await?);
    }
    let mut venue = assembly::venue(chosen, Vec::new())?;
    Ok(venue.account_identity().await?)
}

/// Seed every configured whole-sleeve contract in one WAL frame. The account
/// lease and exact expected user binding make this a stopped-runtime action.
pub async fn initialize_native_strategy_state(config_path: &Path) -> Result<(), Box<dyn Error>> {
    let loaded = config::load(config_path)?;
    let settings = &loaded.config.engine;
    let configured = configured_names(&loaded.config.strategies);
    let strategies = assembly::strategies(&loaded.config.strategies)?;
    let mut initial = initial_state_record(&configured, &strategies)?;

    let _log_claim = engine_wal::lock(&settings.wal_path)?;
    let chosen = assembly::venue_name(&settings.venue)?;
    let who = account_identity(chosen).await?;
    if who.venue != chosen.venue() || who.realm != chosen.realm() {
        return Err(format!(
            "venue identity mismatch: config selects {}/{} but credentials answered as {}/{}",
            chosen.venue(),
            chosen.realm(),
            who.venue,
            who.realm
        )
        .into());
    }
    require_expected_account(&who)?;
    let _account_claim =
        engine_venue::lease::acquire(&who.venue, &who.realm, &who.user_id, INITIALIZE_LEASE_ROLE)?;
    let (mut wal, replayed) = assembly::wal(&settings.wal_path)?;
    if !replayed.is_empty() {
        return Err("initialize-native-strategy-state requires a truly empty WAL".into());
    }
    if let WalRecord::SegmentBase {
        identities: Some(state),
        ..
    } = &mut initial
    {
        state.scope = Some(engine_types::identity::InstrumentScope {
            venue: who.venue.clone(),
            environment: who.realm.clone(),
        });
        state.validate()?;
    }
    wal.append(&initial)?;
    wal.barrier()?;
    println!("log       {}", settings.wal_path.display());
    println!("account   {} on {} ({})", who.user_id, who.venue, who.realm);
    println!("result    canonical native strategy state initialized");
    Ok(())
}

/// Verify effective native state under the WAL's single-writer lock without
/// touching venue state or rewriting the log.
pub fn verify_native_strategy_state(config_path: &Path) -> Result<(), Box<dyn Error>> {
    let loaded = config::load(config_path)?;
    let settings = &loaded.config.engine;
    let _log_claim = engine_wal::lock(&settings.wal_path)?;
    // The newest trusted segment, as boot replays it. The whole chain is
    // unbounded: on the host it is gigabytes and does not fit in memory.
    let (records, torn) = engine_wal::replay_current(&settings.wal_path)?;
    if torn {
        return Err("WAL has a torn tail; native strategy state is not verified".into());
    }
    let replayed: Vec<WalRecord> = records.into_iter().map(|(_, record)| record).collect();
    if replayed.is_empty() {
        return Err("WAL is empty; native strategy state is not initialized".into());
    }
    let keys = configured_names(&loaded.config.strategies);
    let plan =
        crate::identities::plan_identities(&replayed, &keys, None, &Default::default(), &[])?;
    let configured = plan
        .state
        .sleeves
        .iter()
        .map(|key| key.as_str().to_string())
        .collect::<Vec<_>>();
    let strategies =
        assembly::strategies_for_registry(&loaded.config.strategies, &plan, &replayed)?;
    verify_records(&configured, &strategies, &replayed)?;
    engine_core::callback_recovery::host::CallbackHost::validate_recovery(&strategies, &replayed)?;
    println!("log       {}", settings.wal_path.display());
    println!("result    native strategy state verified");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{CheckpointProvenance, Strategy, Subscription};

    struct Stateless;

    impl Strategy for Stateless {
        fn name(&self) -> &str {
            "stateless"
        }

        fn subscriptions(&self) -> Vec<Subscription> {
            Vec::new()
        }
    }

    struct Stateful;

    impl Strategy for Stateful {
        fn name(&self) -> &str {
            "stateful"
        }

        fn subscriptions(&self) -> Vec<Subscription> {
            Vec::new()
        }

        fn checkpoint_identity(&self) -> Option<StrategyCheckpointIdentity> {
            Some(StrategyCheckpointIdentity {
                schema_version: 7,
                decision_fingerprint: "stateful-v7".into(),
            })
        }

        fn initial_checkpoint(&self) -> Option<StrategyCheckpoint> {
            Some(StrategyCheckpoint {
                schema_version: 7,
                decision_fingerprint: "stateful-v7".into(),
                payload: b"canonical-empty".to_vec(),
            })
        }

        fn validate_checkpoint(&self, checkpoint: &StrategyCheckpoint) -> Result<(), String> {
            if checkpoint.payload == b"canonical-empty" {
                Ok(())
            } else {
                Err("payload is not canonical stateful state".into())
            }
        }
    }

    #[test]
    fn cold_native_state_is_one_atomic_segment_base_and_verifies_strictly() {
        let configured = vec!["long".to_string(), "stateless".to_string()];
        let strategies: Vec<Box<dyn Strategy>> = vec![Box::new(Stateful), Box::new(Stateless)];
        let initial = initial_state_record(&configured, &strategies).unwrap();
        let WalRecord::SegmentBase {
            strategies: names,
            strategy_global_checkpoints,
            open_orders,
            ..
        } = &initial
        else {
            panic!("cold initialization must be one complete state frame");
        };
        assert_eq!(names, &configured);
        assert!(open_orders.is_empty());
        assert_eq!(strategy_global_checkpoints.len(), 1);
        assert_eq!(strategy_global_checkpoints[0].strategy, StrategyId(0));
        assert_eq!(
            strategy_global_checkpoints[0].checkpoint.payload,
            b"canonical-empty"
        );
        verify_records(&configured, &strategies, std::slice::from_ref(&initial)).unwrap();

        let mut wrong_owner = initial.clone();
        let WalRecord::SegmentBase {
            strategy_global_checkpoints,
            ..
        } = &mut wrong_owner
        else {
            unreachable!()
        };
        let mut extra = strategy_global_checkpoints[0].clone();
        extra.strategy = StrategyId(9);
        strategy_global_checkpoints.push(extra);
        assert!(verify_records(&configured, &strategies, &[wrong_owner])
            .unwrap_err()
            .to_string()
            .contains("outside the configured table"));

        let mut incomplete = initial.clone();
        let WalRecord::SegmentBase {
            strategy_global_checkpoints,
            ..
        } = &mut incomplete
        else {
            unreachable!()
        };
        strategy_global_checkpoints[0].provenance = Some(CheckpointProvenance {
            source_format: "legacy".into(),
            source_sha256: "aa".into(),
            bundle_sha256: "bb".into(),
            import_complete: false,
        });
        assert!(verify_records(&configured, &strategies, &[incomplete])
            .unwrap_err()
            .to_string()
            .contains("incomplete"));

        let mut malformed = initial;
        let WalRecord::SegmentBase {
            strategy_global_checkpoints,
            ..
        } = &mut malformed
        else {
            unreachable!()
        };
        strategy_global_checkpoints[0].checkpoint.payload = b"arbitrary".to_vec();
        assert!(verify_records(&configured, &strategies, &[malformed])
            .unwrap_err()
            .to_string()
            .contains("not canonical"));
    }

    #[test]
    fn completed_import_provenance_survives_wal_replay_and_native_verification() {
        let configured = vec!["long".to_string()];
        let strategies: Vec<Box<dyn Strategy>> = vec![Box::new(Stateful)];
        let mut imported = initial_state_record(&configured, &strategies).unwrap();
        let WalRecord::SegmentBase {
            strategy_global_checkpoints,
            ..
        } = &mut imported
        else {
            unreachable!()
        };
        strategy_global_checkpoints[0].provenance = Some(CheckpointProvenance {
            source_format: "long-book-state-v2".into(),
            source_sha256: "a".repeat(64),
            bundle_sha256: "b".repeat(64),
            import_complete: true,
        });
        let path = crate::testpath::temp_path("retained-import-provenance");
        let (mut wal, existing) = engine_wal::WalWriter::open(path.path()).unwrap();
        assert!(existing.is_empty());
        wal.append(&imported).unwrap();
        wal.barrier().unwrap();
        drop(wal);

        let (replayed, torn) = engine_wal::replay_current(path.path()).unwrap();
        assert!(!torn);
        let records = replayed.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
        assert_eq!(records, [imported]);
        verify_records(&configured, &strategies, &records).unwrap();
    }

    #[test]
    fn names_pin_strategy_ids_to_exact_config_order() {
        let replayed = vec![WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::Names {
                strategies: vec!["long".into(), "carry".into(), "exodus".into()],
                symbols: vec![],
            },
        )];
        verify_names(&["long".into(), "carry".into(), "exodus".into()], &replayed).unwrap();
        assert!(
            verify_names(&["carry".into(), "long".into(), "exodus".into()], &replayed)
                .unwrap_err()
                .to_string()
                .contains("does not preserve")
        );
    }

    #[test]
    fn an_appended_strategy_preserves_wal_identity_and_a_dropped_one_does_not() {
        let replayed = vec![WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::Names {
                strategies: vec!["carry".into(), "long".into(), "exodus".into()],
                symbols: vec![],
            },
        )];
        verify_names(
            &[
                "carry".into(),
                "long".into(),
                "exodus".into(),
                "probe".into(),
            ],
            &replayed,
        )
        .expect("appending a configured strategy preserves the WAL's existing identities");
        assert!(
            verify_names(&["carry".into(), "long".into()], &replayed)
                .unwrap_err()
                .to_string()
                .contains("does not preserve"),
            "dropping an id the WAL owns is not an append"
        );
        assert!(
            verify_names(
                &[
                    "probe".into(),
                    "carry".into(),
                    "long".into(),
                    "exodus".into()
                ],
                &replayed
            )
            .unwrap_err()
            .to_string()
            .contains("does not preserve"),
            "inserting before the WAL's ids renumbers them"
        );
        // Same length, same members, different places: every id the log owns
        // is renumbered, so this is not an append either.
        assert!(
            verify_names(&["carry".into(), "exodus".into(), "long".into()], &replayed)
                .unwrap_err()
                .to_string()
                .contains("does not preserve")
        );
        // A rename keeps the shape and changes whose fills id 2 owns.
        assert!(verify_names(
            &["carry".into(), "long".into(), "exodus_v2".into()],
            &replayed
        )
        .unwrap_err()
        .to_string()
        .contains("does not preserve"));
        // An emptied table is a removal of every id, not a fresh start: a
        // non-empty WAL still owns them.
        assert!(verify_names(&[], &replayed)
            .unwrap_err()
            .to_string()
            .contains("does not preserve"));
        // More than one id may arrive at once.
        verify_names(
            &[
                "carry".into(),
                "long".into(),
                "exodus".into(),
                "probe".into(),
                "maker_canary".into(),
            ],
            &replayed,
        )
        .expect("two appended ids are still appended ids");
    }

    #[test]
    fn stopped_initialization_claims_are_exclusive() {
        let wal_path = crate::testpath::temp_path("initialize-wal-lock");
        let _wal_claim = engine_wal::lock(wal_path.path()).unwrap();
        assert!(matches!(
            engine_wal::lock(wal_path.path()),
            Err(engine_wal::WalLockError::AlreadyHeld { .. })
        ));

        let lease_path = crate::testpath::temp_path("initialize-account-lock");
        let _account_claim = engine_venue::lease::acquire_at(
            lease_path.path(),
            engine_venue::lease::REALM_DEMO,
            "strategy-state-initialize-test",
        )
        .unwrap();
        assert!(matches!(
            engine_venue::lease::acquire_at(
                lease_path.path(),
                engine_venue::lease::REALM_DEMO,
                "second-initialize-test",
            ),
            Err(engine_venue::lease::LeaseError::AlreadyHeld { .. })
        ));
    }

    #[test]
    fn initialization_account_binding_is_exact() {
        let who = engine_types::AccountIdentity {
            venue: "bybit".into(),
            realm: "demo".into(),
            user_id: "555899665".into(),
        };
        verify_expected_account(&who, "555899665").unwrap();
        assert!(verify_expected_account(&who, "579580669")
            .unwrap_err()
            .to_string()
            .contains("does not match"));
        assert!(verify_expected_account(&who, "")
            .unwrap_err()
            .to_string()
            .contains("empty"));
    }
}
