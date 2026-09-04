//! Stopped-runtime import of one native strategy's whole-sleeve state.

use std::error::Error;
use std::fs::OpenOptions;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::Path;

use engine_types::{
    CheckpointProvenance, StrategyCheckpoint, StrategyCheckpointIdentity, StrategyEvent,
    StrategyGlobalCheckpointState, StrategyId, StrategyImportContext, StrategyImportSource,
    VenueGateway, Wal, WalRecord, MAX_STRATEGY_EVENT_BYTES, MAX_STRATEGY_STATE_BYTES,
};

use crate::{assembly, clock, config};

const LEASE_ROLE: &str = "strategy-state-import";
const INITIALIZE_LEASE_ROLE: &str = "strategy-state-initialize";
const MAX_IMPORT_SOURCE_BYTES: u64 = 16 * 1024 * 1024;
const MAX_IMPORT_BUNDLE_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ImportOutcome {
    Imported,
    AlreadyPresent,
}

fn latest_checkpoint(
    replayed: &[WalRecord],
    strategy: StrategyId,
) -> Option<StrategyGlobalCheckpointState> {
    let mut current = None;
    for record in replayed {
        match record {
            WalRecord::StrategyGlobalCheckpoint {
                strategy: owner,
                checkpoint,
                provenance,
                ..
            } if *owner == strategy => {
                current = Some(StrategyGlobalCheckpointState {
                    strategy,
                    checkpoint: checkpoint.clone(),
                    provenance: provenance.clone(),
                });
            }
            WalRecord::SegmentBase {
                strategy_global_checkpoints,
                ..
            } => {
                current = strategy_global_checkpoints
                    .iter()
                    .find(|row| row.strategy == strategy)
                    .cloned();
            }
            _ => {}
        }
    }
    current
}

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
        runtime_control_requests: Vec::new(),
        runtime_control_consumed: Vec::new(),
        open_orders: Vec::new(),
        rolling_loss_rows: Vec::new(),
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

fn append_import<W: Wal>(
    wal: &mut W,
    replayed: &[WalRecord],
    strategy: StrategyId,
    checkpoint: StrategyCheckpoint,
    mut provenance: CheckpointProvenance,
    events: &[StrategyEvent],
) -> Result<ImportOutcome, Box<dyn Error>> {
    let same_bundle = |existing: &CheckpointProvenance| {
        existing.source_format == provenance.source_format
            && existing.source_sha256 == provenance.source_sha256
            && existing.bundle_sha256 == provenance.bundle_sha256
    };
    if let Some(existing) = latest_checkpoint(replayed, strategy) {
        let Some(existing_provenance) = existing.provenance.as_ref() else {
            return Err(format!(
                "strategy {} already has live whole-sleeve state",
                strategy.0
            )
            .into());
        };
        if existing.checkpoint != checkpoint || !same_bundle(existing_provenance) {
            return Err(format!(
                "strategy {} already has different whole-sleeve state or import provenance",
                strategy.0
            )
            .into());
        }
        if existing_provenance.import_complete {
            return Ok(ImportOutcome::AlreadyPresent);
        }
    } else if events.is_empty() {
        provenance.import_complete = true;
        wal.append(&WalRecord::StrategyGlobalCheckpoint {
            wall_ts_ms: clock::wall_ms(),
            strategy,
            checkpoint,
            provenance: Some(provenance),
        })?;
        wal.barrier()?;
        return Ok(ImportOutcome::Imported);
    } else {
        provenance.import_complete = false;
        wal.append(&WalRecord::StrategyGlobalCheckpoint {
            wall_ts_ms: clock::wall_ms(),
            strategy,
            checkpoint: checkpoint.clone(),
            provenance: Some(provenance.clone()),
        })?;
        wal.barrier()?;
    }

    let mut published = std::collections::BTreeMap::new();
    for record in replayed {
        match record {
            WalRecord::StrategyEventPublished { event, .. } => {
                published.insert((event.source.0, event.event_id.clone()), event.clone());
            }
            WalRecord::SegmentBase {
                strategy_events, ..
            } => {
                published = strategy_events
                    .iter()
                    .map(|event| ((event.source.0, event.event_id.clone()), event.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    let mut appended_event = false;
    for event in events {
        let key = (event.source.0, event.event_id.clone());
        if let Some(existing) = published.get(&key) {
            if existing != event {
                return Err(format!(
                    "strategy {} event id {:?} already has different bytes",
                    event.source.0, event.event_id
                )
                .into());
            }
            continue;
        }
        wal.append(&WalRecord::StrategyEventPublished {
            wall_ts_ms: clock::wall_ms(),
            event: event.clone(),
        })?;
        appended_event = true;
    }
    if appended_event {
        wal.barrier()?;
    }
    provenance.import_complete = true;
    wal.append(&WalRecord::StrategyGlobalCheckpoint {
        wall_ts_ms: clock::wall_ms(),
        strategy,
        checkpoint,
        provenance: Some(provenance),
    })?;
    wal.barrier()?;
    Ok(ImportOutcome::Imported)
}

// Strategy identity is append-only: a WAL name owns its id forever, and a
// config may add ids after the ones the WAL knows. `Engine::boot` takes the
// same rule (`configured.starts_with(prior)`), so this must too, or a deploy
// that appends a sleeve cannot import the state of the sleeves that existed.
fn verify_names(configured: &[String], replayed: &[WalRecord]) -> Result<(), Box<dyn Error>> {
    let logged = crate::replay::LogNames::of_log(replayed).strategies;
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

fn initialize_or_verify_names<W: Wal>(
    wal: &mut W,
    configured: &[String],
    replayed: &mut Vec<WalRecord>,
) -> Result<(), Box<dyn Error>> {
    if replayed.is_empty() {
        let wall_ts_ms = clock::wall_ms();
        let names = WalRecord::SegmentBase {
            wall_ts_ms,
            strategies: configured.to_vec(),
            symbols: Vec::new(),
            may_open: true,
            control_anchors: Vec::new(),
            attribution: Vec::new(),
            logged_exposure: Vec::new(),
            intended_stops: Vec::new(),
            recent_execution_ids: Vec::new(),
            execution_history_through_ms: Some(wall_ts_ms),
            target_book_latches: Vec::new(),
            strategy_checkpoints: Vec::new(),
            strategy_global_checkpoints: Vec::new(),
            strategy_events: Vec::new(),
            signal_observations: Vec::new(),
            signal_cursors: Vec::new(),
            signal_subscriptions: Vec::new(),
            runtime_control_requests: Vec::new(),
            runtime_control_consumed: Vec::new(),
            open_orders: Vec::new(),
            rolling_loss_rows: Vec::new(),
        };
        wal.append(&names)?;
        wal.barrier()?;
        replayed.push(names);
        return Ok(());
    }
    verify_names(configured, replayed)
}

fn checkpoint_from_source(
    strategy: &dyn engine_types::Strategy,
    identity: StrategyCheckpointIdentity,
    context: &StrategyImportContext,
    source_format: &str,
    sources: &[StrategyImportSource],
) -> Result<
    (
        StrategyCheckpoint,
        Vec<engine_types::TranslatedStrategyEvent>,
    ),
    Box<dyn Error>,
> {
    if identity.schema_version == 0 || identity.decision_fingerprint.trim().is_empty() {
        return Err("strategy returned an invalid checkpoint identity".into());
    }
    let translated = strategy
        .translate_checkpoint(context, source_format, sources)
        .map_err(|error| format!("strategy refused {source_format:?} source state: {error}"))?;
    if translated.checkpoint_payload.len() > MAX_STRATEGY_STATE_BYTES {
        return Err(format!(
            "checkpoint is {} bytes; maximum is {}",
            translated.checkpoint_payload.len(),
            MAX_STRATEGY_STATE_BYTES
        )
        .into());
    }
    let checkpoint = StrategyCheckpoint {
        schema_version: identity.schema_version,
        decision_fingerprint: identity.decision_fingerprint,
        payload: translated.checkpoint_payload,
    };
    strategy
        .validate_checkpoint(&checkpoint)
        .map_err(|error| format!("strategy refused translated canonical checkpoint: {error}"))?;
    Ok((checkpoint, translated.pending_events))
}

fn same_source_snapshot(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    left.dev() == right.dev()
        && left.ino() == right.ino()
        && left.mode() == right.mode()
        && left.nlink() == right.nlink()
        && left.uid() == right.uid()
        && left.gid() == right.gid()
        && left.size() == right.size()
        && left.mtime() == right.mtime()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime() == right.ctime()
        && left.ctime_nsec() == right.ctime_nsec()
}

fn read_sources_with_before_open<F>(
    configured: &[(String, std::path::PathBuf)],
    mut before_open: F,
) -> Result<Vec<StrategyImportSource>, Box<dyn Error>>
where
    F: FnMut(&Path),
{
    if configured.is_empty() {
        return Err("import-strategy-state needs at least one --source NAME=PATH".into());
    }
    let mut ordered = configured.to_vec();
    ordered.sort_by(|left, right| left.0.cmp(&right.0));
    let mut previous: Option<&str> = None;
    let mut total = 0u64;
    let mut sources = Vec::with_capacity(ordered.len());
    for (name, path) in &ordered {
        if name.is_empty()
            || name.len() > 64
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
        {
            return Err(format!("source name {name:?} must be 1..=64 ASCII name bytes").into());
        }
        if previous == Some(name) {
            return Err(format!("duplicate import source name {name:?}").into());
        }
        previous = Some(name);
        let metadata = std::fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink()
            || !metadata.file_type().is_file()
            || metadata.nlink() != 1
        {
            return Err(format!(
                "import source {} is not a single regular non-symlink file",
                path.display()
            )
            .into());
        }
        if metadata.len() > MAX_IMPORT_SOURCE_BYTES {
            return Err(format!(
                "import source {} is {} bytes; maximum is {}",
                path.display(),
                metadata.len(),
                MAX_IMPORT_SOURCE_BYTES
            )
            .into());
        }
        before_open(path);
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)?;
        let opened = file.metadata()?;
        if !opened.file_type().is_file()
            || opened.nlink() != 1
            || !same_source_snapshot(&metadata, &opened)
        {
            return Err(format!(
                "import source {} changed while it was opened",
                path.display()
            )
            .into());
        }
        total = total.saturating_add(opened.len());
        if total > MAX_IMPORT_BUNDLE_BYTES {
            return Err(
                format!("import source bundle exceeds {MAX_IMPORT_BUNDLE_BYTES} bytes").into(),
            );
        }
        let mut bytes = Vec::with_capacity(opened.len() as usize);
        file.by_ref()
            .take(MAX_IMPORT_SOURCE_BYTES + 1)
            .read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        if bytes.len() as u64 != opened.len() || !same_source_snapshot(&opened, &after) {
            return Err(
                format!("import source {} changed while it was read", path.display()).into(),
            );
        }
        sources.push(StrategyImportSource {
            name: name.clone(),
            bytes,
        });
    }
    Ok(sources)
}

fn read_sources(
    configured: &[(String, std::path::PathBuf)],
) -> Result<Vec<StrategyImportSource>, Box<dyn Error>> {
    read_sources_with_before_open(configured, |_| {})
}

fn source_bundle_sha256(sources: &[StrategyImportSource]) -> String {
    let mut encoded = b"engine.strategy-import-sources.v1\0".to_vec();
    for source in sources {
        encoded.extend_from_slice(&(source.name.len() as u64).to_le_bytes());
        encoded.extend_from_slice(source.name.as_bytes());
        encoded.extend_from_slice(&(source.bytes.len() as u64).to_le_bytes());
        encoded.extend_from_slice(&source.bytes);
    }
    config::sha256_hex(&encoded)
}

fn resolve_events(
    translated: Vec<engine_types::TranslatedStrategyEvent>,
    configured: &[String],
    selected: StrategyId,
) -> Result<Vec<StrategyEvent>, Box<dyn Error>> {
    let mut out = Vec::with_capacity(translated.len());
    let mut keys = std::collections::BTreeSet::new();
    for pending in translated {
        let source = configured
            .iter()
            .position(|name| name == &pending.source_strategy)
            .ok_or_else(|| {
                format!(
                    "pending event source {:?} is not configured",
                    pending.source_strategy
                )
            })?;
        let destination = configured
            .iter()
            .position(|name| name == &pending.destination_strategy)
            .ok_or_else(|| {
                format!(
                    "pending event destination {:?} is not configured",
                    pending.destination_strategy
                )
            })?;
        let source = StrategyId(u16::try_from(source)?);
        let destination = StrategyId(u16::try_from(destination)?);
        if source == destination || (source != selected && destination != selected) {
            return Err(
                "a translated pending event must cross and involve the selected strategy".into(),
            );
        }
        if pending.kind.trim().is_empty()
            || pending.kind.len() > 256
            || pending.event_id.trim().is_empty()
            || pending.event_id.len() > 256
            || pending.payload.len() > MAX_STRATEGY_EVENT_BYTES
        {
            return Err("translated pending event has invalid kind, id, or payload size".into());
        }
        if !keys.insert((source.0, pending.event_id.clone())) {
            return Err(format!(
                "translated state repeats strategy {} event id {:?}",
                source.0, pending.event_id
            )
            .into());
        }
        out.push(StrategyEvent {
            source,
            destination,
            kind: pending.kind,
            event_id: pending.event_id,
            payload: pending.payload,
        });
    }
    Ok(out)
}

fn bundle_sha256(checkpoint: &StrategyCheckpoint, events: &[StrategyEvent]) -> String {
    fn bytes(out: &mut Vec<u8>, value: &[u8]) {
        out.extend_from_slice(&(value.len() as u64).to_le_bytes());
        out.extend_from_slice(value);
    }
    let mut encoded = b"engine.strategy-import.v1\0".to_vec();
    encoded.extend_from_slice(&checkpoint.schema_version.to_le_bytes());
    bytes(&mut encoded, checkpoint.decision_fingerprint.as_bytes());
    bytes(&mut encoded, &checkpoint.payload);
    encoded.extend_from_slice(&(events.len() as u64).to_le_bytes());
    for event in events {
        encoded.extend_from_slice(&event.source.0.to_le_bytes());
        encoded.extend_from_slice(&event.destination.0.to_le_bytes());
        bytes(&mut encoded, event.kind.as_bytes());
        bytes(&mut encoded, event.event_id.as_bytes());
        bytes(&mut encoded, &event.payload);
    }
    config::sha256_hex(&encoded)
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
    let initial = initial_state_record(&configured, &strategies)?;

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
    let configured = configured_names(&loaded.config.strategies);
    let strategies = assembly::strategies(&loaded.config.strategies)?;
    let _log_claim = engine_wal::lock(&settings.wal_path)?;
    let (records, torn) = engine_wal::replay_chain(&settings.wal_path)?;
    if torn {
        return Err("WAL has a torn tail; native strategy state is not verified".into());
    }
    let replayed: Vec<WalRecord> = records.into_iter().map(|(_, record)| record).collect();
    if replayed.is_empty() {
        return Err("WAL is empty; native strategy state is not initialized".into());
    }
    verify_records(&configured, &strategies, &replayed)?;
    println!("log       {}", settings.wal_path.display());
    println!("result    native strategy state verified");
    Ok(())
}

/// Import translated canonical bytes while both the WAL and account writer
/// leases prove the live engine is stopped.
pub async fn run(
    config_path: &Path,
    strategy_name: &str,
    source_format: &str,
    source_paths: &[(String, std::path::PathBuf)],
) -> Result<ImportOutcome, Box<dyn Error>> {
    let source_format = source_format.trim();
    if source_format.is_empty() || source_format.len() > 256 {
        return Err("--source-format must contain 1..=256 bytes".into());
    }
    let loaded = config::load(config_path)?;
    let configured: Vec<String> = loaded
        .config
        .strategies
        .iter()
        .map(|row| row.sleeve_name().to_string())
        .collect();
    let at = configured
        .iter()
        .position(|name| name == strategy_name)
        .ok_or_else(|| format!("strategy {strategy_name:?} is not in this config"))?;
    let strategy_id = StrategyId(u16::try_from(at).map_err(|_| "more than 65535 strategies")?);
    let strategies = assembly::strategies(&loaded.config.strategies)?;
    let identity = strategies[at].checkpoint_identity().ok_or_else(|| {
        format!("strategy {strategy_name:?} does not declare a whole-sleeve checkpoint contract")
    })?;
    let settings = &loaded.config.engine;
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
        engine_venue::lease::acquire(&who.venue, &who.realm, &who.user_id, LEASE_ROLE)?;
    let (mut wal, mut replayed) = assembly::wal(&settings.wal_path)?;
    initialize_or_verify_names(&mut wal, &configured, &mut replayed)?;
    let sources = read_sources(source_paths)?;
    let context = StrategyImportContext {
        venue: who.venue.clone(),
        realm: who.realm.clone(),
        account_user_id: who.user_id.clone(),
    };
    let (checkpoint, translated_events) = checkpoint_from_source(
        strategies[at].as_ref(),
        identity,
        &context,
        source_format,
        &sources,
    )?;
    let events = resolve_events(translated_events, &configured, strategy_id)?;
    let provenance = CheckpointProvenance {
        source_format: source_format.to_string(),
        source_sha256: source_bundle_sha256(&sources),
        bundle_sha256: bundle_sha256(&checkpoint, &events),
        import_complete: false,
    };
    let outcome = append_import(
        &mut wal,
        &replayed,
        strategy_id,
        checkpoint,
        provenance,
        &events,
    )?;
    println!("log       {}", settings.wal_path.display());
    println!("strategy  {} ({})", strategy_name, strategy_id.0);
    println!("account   {} on {} ({})", who.user_id, who.venue, who.realm);
    println!("result    {outcome:?}");
    Ok(outcome)
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{Strategy, Subscription, TranslatedStrategyState, WalError, WalRecord};

    #[derive(Default)]
    struct MemoryWal {
        records: Vec<WalRecord>,
        barriers: usize,
    }

    impl Wal for MemoryWal {
        fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
            self.records.push(record.clone());
            Ok(self.records.len() as u64)
        }

        fn barrier(&mut self) -> Result<(), WalError> {
            self.barriers += 1;
            Ok(())
        }

        fn flush(&mut self) -> Result<(), WalError> {
            Ok(())
        }
    }

    fn checkpoint(payload: &[u8]) -> StrategyCheckpoint {
        StrategyCheckpoint {
            schema_version: 3,
            decision_fingerprint: "carry-native-v3".into(),
            payload: payload.to_vec(),
        }
    }

    fn provenance(hash: &str) -> CheckpointProvenance {
        CheckpointProvenance {
            source_format: "carry-python-v1".into(),
            source_sha256: hash.into(),
            bundle_sha256: "bundle-aa".into(),
            import_complete: false,
        }
    }

    struct StrictTranslator;

    impl Strategy for StrictTranslator {
        fn name(&self) -> &str {
            "strict"
        }

        fn subscriptions(&self) -> Vec<Subscription> {
            Vec::new()
        }

        fn translate_checkpoint(
            &self,
            context: &StrategyImportContext,
            source_format: &str,
            sources: &[StrategyImportSource],
        ) -> Result<TranslatedStrategyState, String> {
            if context.account_user_id != "account-7"
                || source_format != "legacy-v1"
                || sources
                    != [StrategyImportSource {
                        name: "state".into(),
                        bytes: b"legacy-state".to_vec(),
                    }]
            {
                return Err("unknown or malformed legacy state".into());
            }
            Ok(TranslatedStrategyState {
                checkpoint_payload: b"canonical-state".to_vec(),
                pending_events: Vec::new(),
            })
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
        let strategies: Vec<Box<dyn Strategy>> =
            vec![Box::new(Stateful), Box::new(StrictTranslator)];
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
    fn selected_strategy_is_the_only_checkpoint_translation_authority() {
        let context = StrategyImportContext {
            venue: "bybit".into(),
            realm: "demo".into(),
            account_user_id: "account-7".into(),
        };
        let identity = StrategyCheckpointIdentity {
            schema_version: 3,
            decision_fingerprint: "strict-v3".into(),
        };
        let (translated, events) = checkpoint_from_source(
            &StrictTranslator,
            identity.clone(),
            &context,
            "legacy-v1",
            &[StrategyImportSource {
                name: "state".into(),
                bytes: b"legacy-state".to_vec(),
            }],
        )
        .unwrap();
        assert_eq!(translated.payload, b"canonical-state");
        assert!(events.is_empty());
        assert!(checkpoint_from_source(
            &StrictTranslator,
            identity,
            &context,
            "legacy-v1",
            &[StrategyImportSource {
                name: "state".into(),
                bytes: b"caller-chosen-canonical-bytes".to_vec(),
            }]
        )
        .unwrap_err()
        .to_string()
        .contains("refused"));

        let wrong_account = StrategyImportContext {
            account_user_id: "account-8".into(),
            ..context
        };
        assert!(checkpoint_from_source(
            &StrictTranslator,
            StrategyCheckpointIdentity {
                schema_version: 3,
                decision_fingerprint: "strict-v3".into(),
            },
            &wrong_account,
            "legacy-v1",
            &[StrategyImportSource {
                name: "state".into(),
                bytes: b"legacy-state".to_vec(),
            }]
        )
        .unwrap_err()
        .to_string()
        .contains("refused"));
    }

    #[test]
    fn import_barriers_once_and_identical_retry_is_a_noop() {
        let mut wal = MemoryWal::default();
        assert_eq!(
            append_import(
                &mut wal,
                &[],
                StrategyId(1),
                checkpoint(b"state"),
                provenance("aa"),
                &[]
            )
            .unwrap(),
            ImportOutcome::Imported
        );
        assert_eq!(wal.barriers, 1);
        let replayed = wal.records.clone();
        assert_eq!(
            append_import(
                &mut wal,
                &replayed,
                StrategyId(1),
                checkpoint(b"state"),
                provenance("aa"),
                &[]
            )
            .unwrap(),
            ImportOutcome::AlreadyPresent
        );
        assert_eq!(wal.records.len(), 1);
        assert_eq!(wal.barriers, 1);
    }

    #[test]
    fn pending_handoff_is_between_checkpoint_and_completion_barriers() {
        let event = StrategyEvent {
            source: StrategyId(0),
            destination: StrategyId(1),
            kind: "carry_fire".into(),
            event_id: "fire-7".into(),
            payload: b"exact-target".to_vec(),
        };
        let mut wal = MemoryWal::default();
        assert_eq!(
            append_import(
                &mut wal,
                &[],
                StrategyId(1),
                checkpoint(b"state"),
                provenance("aa"),
                std::slice::from_ref(&event),
            )
            .unwrap(),
            ImportOutcome::Imported
        );
        assert_eq!(wal.barriers, 3);
        assert!(matches!(
            &wal.records[0],
            WalRecord::StrategyGlobalCheckpoint {
                provenance: Some(CheckpointProvenance {
                    import_complete: false,
                    ..
                }),
                ..
            }
        ));
        assert!(matches!(
            &wal.records[1],
            WalRecord::StrategyEventPublished { event: written, .. } if written == &event
        ));
        assert!(matches!(
            &wal.records[2],
            WalRecord::StrategyGlobalCheckpoint {
                provenance: Some(CheckpointProvenance {
                    import_complete: true,
                    ..
                }),
                ..
            }
        ));
        let replayed = wal.records.clone();
        assert_eq!(
            append_import(
                &mut wal,
                &replayed,
                StrategyId(1),
                checkpoint(b"state"),
                provenance("aa"),
                &[event],
            )
            .unwrap(),
            ImportOutcome::AlreadyPresent
        );
        assert_eq!(wal.records.len(), 3);
        assert_eq!(wal.barriers, 3);
    }

    #[test]
    fn import_refuses_state_or_provenance_conflicts() {
        let existing = vec![WalRecord::StrategyGlobalCheckpoint {
            wall_ts_ms: 1,
            strategy: StrategyId(0),
            checkpoint: checkpoint(b"old"),
            provenance: Some({
                let mut proof = provenance("aa");
                proof.import_complete = true;
                proof
            }),
        }];
        for (state, proof) in [(b"new".as_slice(), "aa"), (b"old".as_slice(), "bb")] {
            let mut wal = MemoryWal::default();
            assert!(append_import(
                &mut wal,
                &existing,
                StrategyId(0),
                checkpoint(state),
                provenance(proof),
                &[]
            )
            .unwrap_err()
            .to_string()
            .contains("different"));
            assert!(wal.records.is_empty());
            assert_eq!(wal.barriers, 0);
        }
    }

    #[test]
    fn names_pin_strategy_ids_to_exact_config_order() {
        let replayed = vec![WalRecord::Names {
            strategies: vec!["long".into(), "carry".into(), "exodus".into()],
            symbols: vec![],
        }];
        verify_names(&["long".into(), "carry".into(), "exodus".into()], &replayed).unwrap();
        assert!(
            verify_names(&["carry".into(), "long".into(), "exodus".into()], &replayed)
                .unwrap_err()
                .to_string()
                .contains("does not preserve")
        );
    }

    #[test]
    fn an_appended_strategy_keeps_the_takeover_and_a_dropped_one_does_not() {
        let replayed = vec![WalRecord::Names {
            strategies: vec!["carry".into(), "long".into(), "exodus".into()],
            symbols: vec![],
        }];
        verify_names(
            &[
                "carry".into(),
                "long".into(),
                "exodus".into(),
                "probe".into(),
            ],
            &replayed,
        )
        .expect("a config that appends an id after the WAL's own may still import state");
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
    fn empty_wal_is_initialized_but_nonempty_nameless_wal_is_refused() {
        let configured = vec!["long".into(), "carry".into(), "exodus".into()];
        let mut wal = MemoryWal::default();
        let mut replayed = Vec::new();
        initialize_or_verify_names(&mut wal, &configured, &mut replayed).unwrap();
        assert_eq!(wal.barriers, 1);
        assert_eq!(wal.records, replayed);
        assert!(matches!(
            &replayed[0],
            WalRecord::SegmentBase {
                strategies,
                symbols,
                execution_history_through_ms: Some(_),
                strategy_global_checkpoints,
                ..
            } if strategies == &configured
                && symbols.is_empty()
                && strategy_global_checkpoints.is_empty()
        ));
        initialize_or_verify_names(&mut wal, &configured, &mut replayed).unwrap();
        assert_eq!(wal.records.len(), 1);
        assert_eq!(wal.barriers, 1);

        let mut nameless = vec![WalRecord::Note {
            source: "legacy".into(),
            text: "already contains bytes".into(),
        }];
        let before = nameless.clone();
        let mut refused = MemoryWal::default();
        assert!(
            initialize_or_verify_names(&mut refused, &configured, &mut nameless)
                .unwrap_err()
                .to_string()
                .contains("nonempty")
        );
        assert_eq!(nameless, before);
        assert!(refused.records.is_empty());
        assert_eq!(refused.barriers, 0);
    }

    #[test]
    fn stopped_takeover_claims_are_exclusive() {
        let wal_path = crate::testpath::temp_path("takeover-wal-lock");
        let _wal_claim = engine_wal::lock(wal_path.path()).unwrap();
        assert!(matches!(
            engine_wal::lock(wal_path.path()),
            Err(engine_wal::WalLockError::AlreadyHeld { .. })
        ));

        let lease_path = crate::testpath::temp_path("takeover-account-lock");
        let _account_claim = engine_venue::lease::acquire_at(
            lease_path.path(),
            engine_venue::lease::REALM_DEMO,
            "strategy-state-import-test",
        )
        .unwrap();
        assert!(matches!(
            engine_venue::lease::acquire_at(
                lease_path.path(),
                engine_venue::lease::REALM_DEMO,
                "second-import-test",
            ),
            Err(engine_venue::lease::LeaseError::AlreadyHeld { .. })
        ));
    }

    #[test]
    fn takeover_account_binding_is_exact() {
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

    #[test]
    fn named_sources_are_sorted_and_unsafe_inputs_are_refused() {
        let left = crate::testpath::temp_path("takeover-source-left");
        let right = crate::testpath::temp_path("takeover-source-right");
        std::fs::write(left.path(), b"left").unwrap();
        std::fs::write(right.path(), b"right").unwrap();
        let sources = read_sources(&[
            ("zeta".into(), right.path().to_path_buf()),
            ("alpha".into(), left.path().to_path_buf()),
        ])
        .unwrap();
        assert_eq!(
            sources
                .iter()
                .map(|row| row.name.as_str())
                .collect::<Vec<_>>(),
            ["alpha", "zeta"]
        );
        assert!(read_sources(&[
            ("same".into(), left.path().to_path_buf()),
            ("same".into(), right.path().to_path_buf()),
        ])
        .unwrap_err()
        .to_string()
        .contains("duplicate"));

        let link = crate::testpath::temp_path("takeover-source-link");
        std::os::unix::fs::symlink(left.path(), link.path()).unwrap();
        assert!(read_sources(&[("link".into(), link.path().to_path_buf())])
            .unwrap_err()
            .to_string()
            .contains("non-symlink"));
    }

    #[test]
    fn source_swap_between_identity_check_and_open_is_refused() {
        let source = crate::testpath::temp_path("takeover-source-swap");
        let replacement = crate::testpath::temp_path("takeover-source-replacement");
        std::fs::write(source.path(), b"first").unwrap();
        std::fs::write(replacement.path(), b"other").unwrap();

        let error =
            read_sources_with_before_open(&[("state".into(), source.path().to_path_buf())], |_| {
                std::fs::rename(replacement.path(), source.path()).unwrap()
            })
            .unwrap_err();
        assert!(error.to_string().contains("changed while it was opened"));
    }
}
