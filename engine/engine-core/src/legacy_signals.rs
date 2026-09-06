use std::error::Error;
use std::path::Path;

use engine_types::{LegacySignalSourceRetirement, Wal, WalRecord};
use serde::Deserialize;

use crate::{clock, config, identities, signal_state::SignalState};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetirementRequest {
    pub source: String,
    pub published_through: u64,
    pub reason: String,
}

pub fn retire(
    config_path: &Path,
    requests: &[RetirementRequest],
    execute: bool,
) -> Result<Vec<LegacySignalSourceRetirement>, Box<dyn Error>> {
    let loaded = config::load(config_path)?;
    let path = &loaded.config.engine.wal_path;
    let _claim = engine_wal::lock(path)?;
    let (records, torn) = engine_wal::replay_current(path)?;
    if torn {
        return Err("legacy source retirement refuses a torn WAL tail".into());
    }
    let records = records.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
    let keys = loaded
        .config
        .strategies
        .iter()
        .map(|strategy| strategy.sleeve_name().to_owned())
        .collect::<Vec<_>>();
    let registry = identities::plan_identities(&records, &keys, None, &Default::default(), &[])?;
    let strategies = registry.state.sleeves.len();
    let mut state = SignalState::replay(&records, strategies)?;
    drop(records);
    let mut planned = Vec::new();
    let mut pending = Vec::new();
    for request in requests {
        let retirement = state.plan_legacy_source_retirement(
            &request.source,
            request.published_through,
            &request.reason,
            strategies,
        )?;
        if !state
            .legacy_source_retirements()
            .any(|existing| existing.source == request.source)
        {
            state.apply_legacy_source_retirement(retirement.clone(), strategies)?;
            pending.push(retirement.clone());
        }
        planned.push(retirement);
    }
    if execute && !pending.is_empty() {
        let (mut wal, replayed) = engine_wal::open_current(path)?;
        drop(replayed);
        for retirement in pending {
            wal.append(&WalRecord::LegacySignalSourceRetired {
                wall_ts_ms: clock::wall_ms(),
                retirement,
            })?;
        }
        wal.barrier()?;
    }
    Ok(planned)
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{SignalObservation, StrategyId};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct Fixture {
        directory: PathBuf,
        config: PathBuf,
        wal: PathBuf,
        request: RetirementRequest,
    }

    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let directory = std::env::temp_dir().join(format!(
                "engine-legacy-retirement-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&directory).unwrap();
            let config = directory.join("engine.toml");
            let wal = directory.join("engine.wal");
            std::fs::write(
                &config,
                format!(
                    "[engine]\nwal_path = {:?}\n[[strategy]]\nname = \"quote_taker\"\n",
                    wal.to_str().unwrap()
                ),
            )
            .unwrap();
            let source = format!("native.g{}.long", "c".repeat(32));
            let mut observation = SignalObservation {
                schema_version: engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION,
                decision_fingerprint: "retirement-command-test".into(),
                destination: StrategyId(0),
                source: source.clone(),
                sequence: 5,
                observation_id: "accepted-five".into(),
                kind: "test".into(),
                observed_wall_ts_ms: 1,
                available_wall_ts_ms: 2,
                subscriptions: vec![],
                payload: vec![1],
                content_sha256: String::new(),
            };
            observation.content_sha256 = crate::signals::content_sha256(&observation);
            let (mut writer, _) = engine_wal::open_current(&wal).unwrap();
            for row in [
                WalRecord::Names {
                    strategies: vec!["quote_taker".into()],
                    symbols: vec![],
                },
                WalRecord::SignalObservation {
                    wall_ts_ms: 2,
                    observation,
                },
                WalRecord::SignalObservationConsumed {
                    wall_ts_ms: 3,
                    strategy: StrategyId(0),
                    source: source.clone(),
                    sequence: 5,
                    observation_id: "accepted-five".into(),
                },
            ] {
                writer.append(&row).unwrap();
            }
            writer.barrier().unwrap();
            Self {
                directory,
                config,
                wal,
                request: RetirementRequest {
                    source,
                    published_through: 7,
                    reason: "stopped source; six lost; seven retained without application".into(),
                },
            }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.directory).unwrap();
        }
    }

    #[test]
    fn retirement_command_is_read_only_until_execution_and_retries_without_rewriting_wal() {
        let fixture = Fixture::new();
        let before = std::fs::read(&fixture.wal).unwrap();
        let plan = [fixture.request.clone()];
        let preview = retire(&fixture.config, &plan, false).unwrap();
        assert_eq!(preview[0].accepted_through, 5);
        assert_eq!(std::fs::read(&fixture.wal).unwrap(), before);
        assert_eq!(retire(&fixture.config, &plan, true).unwrap(), preview);
        let after = std::fs::read(&fixture.wal).unwrap();
        let (records, torn) = engine_wal::replay_current(&fixture.wal).unwrap();
        assert!(!torn);
        assert_eq!(records.len(), 4);
        assert!(matches!(
            records.last().unwrap().1,
            WalRecord::LegacySignalSourceRetired { .. }
        ));
        let records = records.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
        let replayed = SignalState::replay(&records, 1).unwrap();
        assert_eq!(replayed.cursors().next().unwrap().sequence, 5);
        assert_eq!(replayed.legacy_source_retirements().count(), 1);
        assert_eq!(retire(&fixture.config, &plan, true).unwrap(), preview);
        assert_eq!(std::fs::read(&fixture.wal).unwrap(), after);
    }

    #[test]
    fn retirement_command_validates_the_whole_batch_before_writing() {
        let fixture = Fixture::new();
        let before = std::fs::read(&fixture.wal).unwrap();
        let mut conflicting = fixture.request.clone();
        conflicting.published_through += 1;
        let error = retire(
            &fixture.config,
            &[fixture.request.clone(), conflicting],
            true,
        )
        .unwrap_err();
        assert!(error.to_string().contains("immutable source outcome"));
        assert_eq!(std::fs::read(&fixture.wal).unwrap(), before);
    }

    #[test]
    fn retirement_command_refuses_a_running_writer_and_a_torn_tail_without_truncating() {
        use std::io::Write;

        let fixture = Fixture::new();
        let plan = [fixture.request.clone()];
        let before = std::fs::read(&fixture.wal).unwrap();
        let claim = engine_wal::lock(&fixture.wal).unwrap();
        assert!(retire(&fixture.config, &plan, true).is_err());
        assert_eq!(std::fs::read(&fixture.wal).unwrap(), before);
        drop(claim);
        std::fs::OpenOptions::new()
            .append(true)
            .open(&fixture.wal)
            .unwrap()
            .write_all(&[255, 255])
            .unwrap();
        let torn = std::fs::read(&fixture.wal).unwrap();
        assert!(retire(&fixture.config, &plan, true)
            .unwrap_err()
            .to_string()
            .contains("torn WAL tail"));
        assert_eq!(std::fs::read(&fixture.wal).unwrap(), torn);
    }
}
