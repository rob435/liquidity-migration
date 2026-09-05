//! The runtime skeleton every native directional plug shares.
//!
//! A plug is a registered reducer plus the bookkeeping that keeps it alive
//! inside the engine: its identity, its configuration, the durable state
//! the reducer owns, whether that state has been read back from the
//! checkpoint yet, and what it has to tell the operator. The reducers
//! differ per sleeve; this part does not.

use std::collections::BTreeMap;

use engine_types::{
    StrategyCheckpoint, StrategyCheckpointIdentity, StrategyCtx, StrategyId, WorkPolicy,
};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

use super::{checkpoint_payload, DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION};

/// A sleeve's registered configuration, as the skeleton reads it.
pub trait SleeveConfig {
    fn validate(&self) -> Result<(), &'static str>;
    fn fingerprint(&self) -> String;
    fn rest_entries(&self) -> bool;
    fn hold_decision_price(&self) -> bool;
    fn give_up_instead_of_crossing(&self) -> bool;
}

/// A sleeve's durable reducer state, as the skeleton restores it.
pub trait SleeveState: Serialize + DeserializeOwned + Default {
    fn schema_version(&self) -> u16;
    fn set_schema_version(&mut self, version: u16);
    fn validate(&self) -> Result<(), &'static str>;
}

#[derive(Serialize, Deserialize)]
pub struct SleeveCore<C, S> {
    pub id: StrategyId,
    pub config: C,
    pub state: S,
    /// Whether the durable checkpoint has been read. False from boot until
    /// the first wake; a hand-built sleeve starts restored.
    pub restored: bool,
    /// The fingerprint of the checkpoint last read or written, so a config
    /// change since is visible to the reducer.
    pub checkpoint_fingerprint: Option<String>,
    /// Why each asked-for name is not being opened, for the heartbeat.
    pub blockers: BTreeMap<String, String>,
    pub last_error: Option<String>,
    pub flatten_request_id: Option<String>,
}

impl<C: SleeveConfig, S: SleeveState> SleeveCore<C, S> {
    /// A sleeve over a hand-built state: tests and the contract binaries.
    pub fn new(config: C, mut state: S) -> Result<Self, &'static str> {
        config.validate()?;
        if state.schema_version() == 0 {
            state.set_schema_version(DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION);
        }
        state.validate()?;
        Ok(Self {
            id: StrategyId(0),
            config,
            state,
            restored: true,
            checkpoint_fingerprint: None,
            blockers: BTreeMap::new(),
            last_error: None,
            flatten_request_id: None,
        })
    }

    /// A sleeve booting from its registered config. Its state is the
    /// canonical empty one until the checkpoint is read.
    pub fn from_config(id: StrategyId, config: C) -> Self {
        Self {
            id,
            config,
            state: Self::initial_state(),
            restored: false,
            checkpoint_fingerprint: None,
            blockers: BTreeMap::new(),
            last_error: None,
            flatten_request_id: None,
        }
    }

    fn initial_state() -> S {
        let mut state = S::default();
        state.set_schema_version(DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION);
        state
    }

    /// Read the durable whole-sleeve checkpoint once. A checkpoint from
    /// another config or schema is refused and recorded, never applied.
    /// `label` names the sleeve in the operator-facing error.
    pub fn ensure_restored(&mut self, label: &str, ctx: &dyn StrategyCtx) {
        if self.restored {
            return;
        }
        self.restored = true;
        let Some(checkpoint) = ctx.strategy_global_checkpoint() else {
            return;
        };
        self.checkpoint_fingerprint = Some(checkpoint.decision_fingerprint.clone());
        if checkpoint.schema_version != DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION
            || checkpoint.decision_fingerprint != self.config.fingerprint()
        {
            self.last_error = Some(format!("{label} checkpoint identity mismatch"));
            return;
        }
        match serde_json::from_slice::<S>(&checkpoint.payload)
            .map_err(|error| error.to_string())
            .and_then(|state| {
                state.validate().map_err(str::to_owned)?;
                Ok(state)
            }) {
            Ok(state) => self.state = state,
            Err(error) => {
                self.last_error = Some(format!("{label} checkpoint refused: {error}"));
                self.checkpoint_fingerprint = Some("invalid-checkpoint".to_owned());
            }
        }
    }

    /// How an entry is worked when the config asks for resting entries.
    pub fn entry_work(&self) -> Option<WorkPolicy> {
        self.config.rest_entries().then_some(WorkPolicy {
            hold_decision_px: self.config.hold_decision_price(),
            give_up_instead_of_crossing: self.config.give_up_instead_of_crossing(),
            ..WorkPolicy::default()
        })
    }

    pub fn checkpoint_identity(&self) -> Option<StrategyCheckpointIdentity> {
        Some(StrategyCheckpointIdentity {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            decision_fingerprint: self.config.fingerprint(),
        })
    }

    pub fn initial_checkpoint(&self) -> Option<StrategyCheckpoint> {
        Some(StrategyCheckpoint {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            decision_fingerprint: self.config.fingerprint(),
            payload: checkpoint_payload(&Self::initial_state()),
        })
    }

    pub fn validate_checkpoint(
        &self,
        label: &str,
        checkpoint: &StrategyCheckpoint,
    ) -> Result<(), String> {
        if checkpoint.schema_version != DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION
            || checkpoint.decision_fingerprint != self.config.fingerprint()
        {
            return Err(format!("{label} checkpoint identity mismatch"));
        }
        let state: S =
            serde_json::from_slice(&checkpoint.payload).map_err(|error| error.to_string())?;
        state.validate().map_err(str::to_owned)
    }

    pub fn health_error(&self) -> Option<&str> {
        self.last_error.as_deref()
    }

    pub fn entry_blockers(&self) -> Vec<(String, String)> {
        self.blockers
            .iter()
            .map(|(symbol, reason)| (symbol.clone(), reason.clone()))
            .collect()
    }
}
