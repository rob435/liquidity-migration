use super::*;

/// Whether two renders of one sleeve describe the same decision rules.
///
/// `Ok(true)`: the checkpoint carries over under the new fingerprint.
/// `Ok(false)`: the rules changed, and what happens next depends on whether
/// the sleeve holds anything. A change of ownership or a wider EXODUS stop is
/// refused outright.
fn compatible_config(
    previous: &config::StrategyConfig,
    next: &config::StrategyConfig,
) -> Result<bool, Box<dyn Error>> {
    if previous.name != next.name || previous.sleeve_name() != next.sleeve_name() {
        return Err("checkpoint rebind changes strategy ownership".into());
    }
    let json = |row: &config::StrategyConfig| -> Result<serde_json::Value, Box<dyn Error>> {
        Ok(serde_json::from_str(
            row.params
                .get("config_json")
                .and_then(toml::Value::as_str)
                .ok_or("native strategy config_json is missing")?,
        )?)
    };
    let compatible = match next.name.as_str() {
        "carry_native" => {
            use engine_strategies::native_carry::plan::StrategyConfig;
            let mut old: StrategyConfig = serde_json::from_value(json(previous)?)?;
            let new: StrategyConfig = serde_json::from_value(json(next)?)?;
            old.rule_sha256.clone_from(&new.rule_sha256);
            old.fingerprint() == new.fingerprint()
        }
        "exodus_native" => {
            use engine_strategies::native_exodus::plan::StrategyConfig;
            let mut old: StrategyConfig = serde_json::from_value(json(previous)?)?;
            let new: StrategyConfig = serde_json::from_value(json(next)?)?;
            if new.rule.stop_loss_fraction > old.rule.stop_loss_fraction {
                return Err("checkpoint rebind cannot widen the EXODUS stop".into());
            }
            old.rule_sha256.clone_from(&new.rule_sha256);
            old.rule.stop_loss_fraction = new.rule.stop_loss_fraction;
            old.fingerprint() == new.fingerprint()
        }
        _ => false,
    };
    Ok(compatible)
}

/// Whether the replayed log attributes this sleeve nothing: no exposure, no
/// order in flight, no queued callback and no committed process. Such a sleeve
/// has no state a rule change could orphan, so a fresh initial checkpoint is
/// exactly what a first start would give it.
fn holds_nothing(
    index: usize,
    replayed: &[WalRecord],
    strategy_count: usize,
) -> Result<bool, Box<dyn Error>> {
    let id = StrategyId(u16::try_from(index)?);
    let attribution = engine_core::attribution::Attribution::try_from_records(replayed)?;
    if attribution
        .rows()
        .iter()
        .any(|(strategy, _, _)| *strategy == id)
    {
        return Ok(false);
    }
    let orders = engine_core::inflight::LedgerOfOrders::try_from_records(replayed)?;
    if orders
        .orders
        .values()
        .any(|order| order.request.strategy == id && order.in_flight())
    {
        return Ok(false);
    }
    let callbacks =
        engine_core::callback_recovery::state::CallbackState::replay(replayed, strategy_count)?;
    Ok(!callbacks.committed.contains_key(&id)
        && !callbacks.inputs.values().any(|input| input.strategy == id))
}

fn rebind_records(
    previous: &[config::StrategyConfig],
    next: &[config::StrategyConfig],
    replayed: &[WalRecord],
) -> Result<Vec<WalRecord>, Box<dyn Error>> {
    let names = configured_names(next);
    if configured_names(previous) != names {
        return Err("checkpoint rebind must preserve the complete sleeve table".into());
    }
    verify_names(&names, replayed)?;
    let old_strategies = assembly::strategies(previous)?;
    let new_strategies = assembly::strategies(next)?;
    let mut current = std::collections::BTreeMap::new();
    for record in replayed {
        match record {
            WalRecord::SegmentBase {
                strategy_global_checkpoints,
                ..
            } => {
                current = strategy_global_checkpoints
                    .iter()
                    .map(|row| (row.strategy.0, row.clone()))
                    .collect();
            }
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
            _ => {}
        }
    }
    let mut changes = Vec::new();
    for (index, strategy) in new_strategies.iter().enumerate() {
        let Some(identity) = strategy.checkpoint_identity() else {
            continue;
        };
        let id = u16::try_from(index)?;
        let state = current
            .get(&id)
            .ok_or("checkpoint rebind has no source state")?;
        if state.checkpoint.decision_fingerprint == identity.decision_fingerprint {
            validate_checkpoint_contract(strategy.as_ref(), &identity, &state.checkpoint)?;
            continue;
        }
        let old_identity = old_strategies[index]
            .checkpoint_identity()
            .ok_or("source strategy has no checkpoint identity")?;
        validate_checkpoint_contract(
            old_strategies[index].as_ref(),
            &old_identity,
            &state.checkpoint,
        )?;
        let (checkpoint, provenance) = if compatible_config(&previous[index], &next[index])? {
            let mut checkpoint = state.checkpoint.clone();
            checkpoint
                .decision_fingerprint
                .clone_from(&identity.decision_fingerprint);
            (checkpoint, state.provenance.clone())
        } else if holds_nothing(index, replayed, new_strategies.len())? {
            println!(
                "reinitialized {:?}: decision rules changed while it held no exposure, working orders or callback work",
                next[index].sleeve_name()
            );
            let fresh = strategy
                .initial_checkpoint()
                .ok_or("strategy has no canonical initial checkpoint")?;
            (fresh, None)
        } else {
            return Err(format!(
                "checkpoint rebind changes {:?} decision rules while it holds exposure or pending work",
                next[index].sleeve_name()
            )
            .into());
        };
        validate_checkpoint_contract(strategy.as_ref(), &identity, &checkpoint)?;
        changes.push(WalRecord::StrategyGlobalCheckpoint {
            wall_ts_ms: clock::wall_ms(),
            strategy: StrategyId(id),
            checkpoint,
            provenance,
        });
    }
    let (callbacks, _) = engine_core::callback_recovery::paging::CallbackPages::replay(
        replayed,
        new_strategies.len(),
        1,
    )?;
    for (owner, process) in callbacks.committed {
        let index = owner.idx();
        let next_runtime = new_strategies[index]
            .runtime_state()?
            .ok_or("saved callback process has no configured runtime")?;
        if process.runtime.kind == next_runtime.kind
            && process.runtime.configuration_sha256 == next_runtime.configuration_sha256
        {
            engine_strategies::runtime::restore(&process.runtime)?;
            continue;
        }
        let previous_runtime = old_strategies[index]
            .runtime_state()?
            .ok_or("saved callback process has no previous runtime")?;
        let runtime = engine_strategies::probe::Probe::reconfigure_offset(
            &process.runtime,
            &previous_runtime,
            &next_runtime,
        )?;
        changes.push(WalRecord::StrategyRuntimeReconfigured {
            strategy: owner,
            previous_configuration_sha256: process.runtime.configuration_sha256,
            runtime,
        });
    }
    let mut result = replayed.to_vec();
    result.extend_from_slice(&changes);
    verify_records(&names, &new_strategies, &result)?;
    engine_core::callback_recovery::host::CallbackHost::validate_recovery(
        &new_strategies,
        &result,
    )?;
    Ok(changes)
}

/// Rebind compatible configurations while retaining holdings and callback work.
pub async fn rebind_native_strategy_state(
    previous_path: &Path,
    config_path: &Path,
    execute: bool,
) -> Result<(), Box<dyn Error>> {
    let previous = config::load(previous_path)?;
    let loaded = config::load(config_path)?;
    let settings = &loaded.config.engine;
    if previous.config.engine.venue != settings.venue
        || previous.config.engine.wal_path != settings.wal_path
    {
        return Err("checkpoint rebind changes venue or WAL family".into());
    }
    let _log_claim = engine_wal::lock(&settings.wal_path)?;
    let (records, torn) = engine_wal::replay_current(&settings.wal_path)?;
    if torn {
        return Err("checkpoint rebind refuses a torn WAL tail".into());
    }
    let replayed: Vec<_> = records.into_iter().map(|(_, row)| row).collect();
    let changes = rebind_records(
        &previous.config.strategies,
        &loaded.config.strategies,
        &replayed,
    )?;
    println!("compatible checkpoint rebinds {}", changes.len());
    if !execute || changes.is_empty() {
        return Ok(());
    }
    let chosen = assembly::venue_name(&settings.venue)?;
    let who = account_identity(chosen).await?;
    if who.venue != chosen.venue() || who.realm != chosen.realm() {
        return Err("checkpoint rebind account realm mismatch".into());
    }
    require_expected_account(&who)?;
    let _account_claim = engine_venue::lease::acquire(
        &who.venue,
        &who.realm,
        &who.user_id,
        "strategy-state-rebind",
    )?;
    let (mut wal, _) = assembly::wal(&settings.wal_path)?;
    for record in &changes {
        wal.append(record)?;
    }
    wal.barrier()?;
    println!(
        "result    compatible strategy configuration rebound; holdings and pending work preserved"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probe_offset_rebind_preserves_pending_work_through_replay_and_rotation() {
        use engine_core::callback_recovery::{
            host::CallbackHost, paging::CallbackPages, state::CallbackState,
        };
        use engine_types::strategy_process::{
            CallbackEvent, CallbackPreparation, StrategyCallbackInput, StrategyProcessState,
            StrategyTimerState,
        };
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap();
        let next = config::load(&root.join("deploy/engine.demo.toml.template"))
            .unwrap()
            .config
            .strategies;
        let mut previous = next.clone();
        previous[3]
            .params
            .insert("offset_bps".into(), toml::Value::Integer(300));
        let old_strategies = assembly::strategies(&previous).unwrap();
        let new_strategies = assembly::strategies(&next).unwrap();
        let mut runtime = old_strategies[3].runtime_state().unwrap().unwrap();
        let mut payload: serde_json::Value = serde_json::from_slice(&runtime.payload).unwrap();
        payload["fired"] = serde_json::json!(51);
        payload["refused"] = serde_json::json!(18);
        payload["draining"] = serde_json::json!(true);
        runtime.payload = serde_json::to_vec(&payload).unwrap();
        let process = StrategyProcessState {
            strategy: StrategyId(3),
            last_callback_id: 100,
            runtime,
            timers: vec![StrategyTimerState {
                id: engine_types::TimerId(3),
                deadline_ns: 9000,
                deadline_wall_ms: 18000,
            }],
            retained_signal_subscriptions: Some(Vec::new()),
        };
        let input = StrategyCallbackInput {
            callback_id: 101,
            strategy: StrategyId(3),
            order_origin: None,
            event: CallbackEvent::Boot,
            preparation: CallbackPreparation::Queued,
        };
        let mut records = source_records(&previous);
        let WalRecord::SegmentBase {
            strategy_processes,
            strategy_callbacks,
            ..
        } = &mut records[0]
        else {
            panic!("base")
        };
        strategy_processes.push(process.clone());
        strategy_callbacks.push(input.clone());
        CallbackHost::validate_recovery(&old_strategies, &records).unwrap();
        assert!(CallbackHost::validate_recovery(&new_strategies, &records).is_err());
        let changes = rebind_records(&previous, &next, &records).unwrap();
        assert_eq!(changes.len(), 1);
        let encoded = serde_json::to_vec(&changes[0]).unwrap();
        records.push(serde_json::from_slice(&encoded).unwrap());
        let state = CallbackState::replay(&records, next.len()).unwrap();
        assert_eq!(state.inputs.get(&101), Some(&input));
        assert_eq!(state.next_id, 102);
        let rebound = state.committed.get(&StrategyId(3)).unwrap();
        let mut expected = process;
        expected.runtime = rebound.runtime.clone();
        assert_eq!(*rebound, expected);
        payload["offset"] = serde_json::json!(0.005);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&rebound.runtime.payload).unwrap(),
            payload
        );
        engine_strategies::runtime::restore(&rebound.runtime).unwrap();
        CallbackHost::validate_recovery(&new_strategies, &records).unwrap();
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("engine.wal");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        for record in &records {
            wal.append(record).unwrap();
        }
        wal.barrier().unwrap();
        drop(wal);
        let (mut wal, restored) = engine_wal::WalWriter::open(&path).unwrap();
        let restored: Vec<_> = restored.into_iter().map(|(_, row)| row).collect();
        assert_eq!(restored, records);
        let host = CallbackHost::new_paged(
            engine_core::callback_recovery::host::CallbackExecution::Embedded,
            &new_strategies,
            &restored,
            wal.callback_reader().unwrap().unwrap(),
        )
        .unwrap();
        assert!(host.pending_for(StrategyId(3)));
        assert_eq!(host.state.committed, state.committed);
        let (paged, pages) = CallbackPages::replay(&records, next.len(), 70).unwrap();
        assert_eq!(paged.committed, state.committed);
        assert_eq!(pages.slots.len(), 1);
        assert_eq!(pages.slots[&101].strategy, StrategyId(3));
        let mut rotated = source_records(&next);
        let WalRecord::SegmentBase {
            strategy_processes,
            strategy_callback_queues,
            ..
        } = &mut rotated[0]
        else {
            panic!("base")
        };
        *strategy_processes = paged.committed.into_values().collect();
        *strategy_callback_queues = pages.slots.into_values().collect();
        CallbackHost::validate_recovery(&new_strategies, &rotated).unwrap();
        assert!(rebind_records(&previous, &next, &rotated)
            .unwrap()
            .is_empty());
        let mut changed = next.clone();
        changed[3]
            .params
            .insert("rest_ms".into(), toml::Value::Integer(5000));
        assert!(rebind_records(&previous, &changed, &records[..1]).is_err());
        let mut unknown = previous.clone();
        unknown[3]
            .params
            .insert("offset_bps".into(), toml::Value::Integer(200));
        assert!(rebind_records(&unknown, &next, &records[..1]).is_err());
        let mut duplicate = records.clone();
        duplicate.push(records.last().unwrap().clone());
        assert!(CallbackPages::replay(&duplicate, next.len(), 70).is_err());
    }

    fn configs() -> (Vec<config::StrategyConfig>, Vec<config::StrategyConfig>) {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap();
        let loaded = config::load(&root.join("deploy/engine.demo.toml.template")).unwrap();
        let next = loaded.config.strategies[..3].to_vec();
        let mut previous = next.clone();
        for (index, row) in previous.iter_mut().enumerate() {
            if index == 1 {
                continue;
            }
            let mut value: serde_json::Value =
                serde_json::from_str(row.params["config_json"].as_str().unwrap()).unwrap();
            value["rule_sha256"] = serde_json::json!("1".repeat(64));
            if index == 2 {
                value["rule"]["stop_loss_fraction"] = serde_json::json!(0.35);
            }
            row.params
                .insert("config_json".into(), toml::Value::String(value.to_string()));
        }
        (previous, next)
    }

    fn source_records(previous: &[config::StrategyConfig]) -> Vec<WalRecord> {
        let names = configured_names(previous);
        let strategies = assembly::strategies(previous).unwrap();
        vec![initial_state_record(&names, &strategies).unwrap()]
    }

    #[test]
    fn rebind_preserves_payloads_and_recovers_after_a_partial_append() {
        let (previous, next) = configs();
        let records = source_records(&previous);
        let strategies = assembly::strategies(&next).unwrap();
        let names = configured_names(&next);
        assert!(verify_records(&names, &strategies, &records).is_err());
        let changes = rebind_records(&previous, &next, &records).unwrap();
        assert_eq!(changes.len(), 2);
        let WalRecord::SegmentBase {
            strategy_global_checkpoints: source,
            ..
        } = &records[0]
        else {
            panic!("source base")
        };
        for row in &changes {
            let WalRecord::StrategyGlobalCheckpoint {
                strategy,
                checkpoint,
                ..
            } = row
            else {
                panic!("checkpoint")
            };
            assert_eq!(
                checkpoint.payload,
                source[usize::from(strategy.0)].checkpoint.payload
            );
        }
        let mut partial = records;
        partial.push(changes[0].clone());
        let remaining = rebind_records(&previous, &next, &partial).unwrap();
        assert_eq!(remaining.len(), 1);
        partial.extend(remaining);
        verify_records(&names, &strategies, &partial).unwrap();
        assert!(rebind_records(&previous, &next, &partial)
            .unwrap()
            .is_empty());
    }

    fn with_rule_change(
        next: &[config::StrategyConfig],
        index: usize,
        field: &str,
        value: serde_json::Value,
    ) -> Vec<config::StrategyConfig> {
        let mut changed = next.to_vec();
        let mut json: serde_json::Value =
            serde_json::from_str(changed[index].params["config_json"].as_str().unwrap()).unwrap();
        json["rule"][field] = value;
        changed[index]
            .params
            .insert("config_json".into(), toml::Value::String(json.to_string()));
        changed
    }

    #[test]
    fn a_rule_change_on_a_sleeve_that_holds_nothing_reinitialises_its_checkpoint() {
        let (previous, next) = configs();
        let records = source_records(&previous);
        for (index, field, value) in [
            (0, "enter_bp", serde_json::json!(20.0)),
            (2, "cover_minutes_after_settlement", serde_json::json!(120)),
        ] {
            let changed = with_rule_change(&next, index, field, value);
            let strategies = assembly::strategies(&changed).unwrap();
            let changes = rebind_records(&previous, &changed, &records).unwrap();
            let fresh = changes
                .iter()
                .find_map(|row| match row {
                    WalRecord::StrategyGlobalCheckpoint {
                        strategy,
                        checkpoint,
                        provenance,
                        ..
                    } if usize::from(strategy.0) == index => Some((checkpoint, provenance)),
                    _ => None,
                })
                .unwrap_or_else(|| panic!("{field}: no checkpoint for sleeve {index}"));
            let initial = strategies[index].initial_checkpoint().unwrap();
            assert_eq!(*fresh.0, initial, "{field}");
            assert!(fresh.1.is_none(), "{field}");
            let mut result = records.clone();
            result.extend(changes);
            verify_records(&configured_names(&changed), &strategies, &result).unwrap();
        }
        // A wider EXODUS stop is refused whatever the sleeve holds.
        let widened = with_rule_change(&next, 2, "stop_loss_fraction", serde_json::json!(0.5));
        assert!(rebind_records(&previous, &widened, &records).is_err());
    }

    #[test]
    fn a_rule_change_on_a_sleeve_with_exposure_or_pending_work_is_refused() {
        use engine_types::strategy_process::{
            CallbackEvent, CallbackPreparation, StrategyCallbackInput,
        };
        let (previous, next) = configs();
        let changed = with_rule_change(&next, 0, "enter_bp", serde_json::json!(20.0));

        // Exposure the log attributes to the carry sleeve, written the way a
        // rotation writes it: the snapshot and its rows agree.
        let mut ledger = engine_core::attribution::Attribution::default();
        ledger.note(
            StrategyId(0),
            engine_types::SymbolId(0),
            engine_types::Side::Buy,
            12.0,
        );
        let mut held = source_records(&previous);
        let WalRecord::SegmentBase {
            attribution,
            portfolio,
            ..
        } = &mut held[0]
        else {
            panic!("source base")
        };
        *portfolio = Some(ledger.snapshot());
        *attribution = ledger
            .rows()
            .into_iter()
            .map(|(strategy, symbol, signed_qty)| engine_types::FilledTotal {
                strategy,
                symbol,
                signed_qty,
            })
            .collect();
        let error = rebind_records(&previous, &changed, &held)
            .unwrap_err()
            .to_string();
        assert!(error.contains("exposure or pending work"), "{error}");

        let mut queued = source_records(&previous);
        let WalRecord::SegmentBase {
            strategy_callbacks, ..
        } = &mut queued[0]
        else {
            panic!("source base")
        };
        strategy_callbacks.push(StrategyCallbackInput {
            callback_id: 1,
            strategy: StrategyId(0),
            order_origin: None,
            event: CallbackEvent::Boot,
            preparation: CallbackPreparation::Queued,
        });
        let error = rebind_records(&previous, &changed, &queued)
            .unwrap_err()
            .to_string();
        assert!(error.contains("exposure or pending work"), "{error}");
    }

    #[test]
    fn rebind_refuses_unknown_source_identities_and_a_reordered_sleeve_table() {
        let (previous, next) = configs();
        let records = source_records(&previous);
        let mut unknown = records.clone();
        let WalRecord::SegmentBase {
            strategy_global_checkpoints,
            ..
        } = &mut unknown[0]
        else {
            panic!("source base")
        };
        strategy_global_checkpoints[0]
            .checkpoint
            .decision_fingerprint = "unknown".into();
        assert!(rebind_records(&previous, &next, &unknown).is_err());
        let mut reordered = next;
        reordered.swap(0, 1);
        assert!(rebind_records(&previous, &reordered, &records).is_err());
    }
}
