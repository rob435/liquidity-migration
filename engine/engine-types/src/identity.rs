use std::collections::BTreeSet;

use serde::{Deserialize, Deserializer, Serialize};

pub const IDENTITY_SCHEMA_VERSION: u16 = 1;
pub const DENSE_ID_CAPACITY: usize = u16::MAX as usize + 1;
const KEY_BYTES_MAX: usize = 256;

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum IdentityError {
    #[error("invalid durable identity: {0}")]
    Invalid(String),
    #[error("symbol id capacity {DENSE_ID_CAPACITY} is exhausted; existing ids remain valid")]
    SymbolIdsExhausted,
    #[error("sleeve id capacity {DENSE_ID_CAPACITY} is exhausted; existing ids remain valid")]
    SleeveIdsExhausted,
    #[error("instrument {0:?} has no authoritative native symbol binding")]
    UnresolvedInstrument(String),
}

fn valid_text(value: &str, label: &str) -> Result<(), IdentityError> {
    if value.trim().is_empty() || value.len() > KEY_BYTES_MAX {
        return Err(IdentityError::Invalid(format!(
            "{label} must contain 1..={KEY_BYTES_MAX} non-blank bytes"
        )));
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(transparent)]
pub struct SleeveKey(String);

impl SleeveKey {
    pub fn new(value: impl Into<String>) -> Result<Self, IdentityError> {
        let value = value.into();
        valid_text(&value, "sleeve key")?;
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl<'de> Deserialize<'de> for SleeveKey {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Self::new(String::deserialize(deserializer)?).map_err(serde::de::Error::custom)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentScope {
    pub venue: String,
    pub environment: String,
}

impl InstrumentScope {
    pub fn validate(&self) -> Result<(), IdentityError> {
        valid_text(&self.venue, "venue")?;
        valid_text(&self.environment, "environment")
    }
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentKey {
    pub venue: String,
    pub environment: String,
    pub native_symbol: String,
}

impl InstrumentKey {
    pub fn in_scope(scope: &InstrumentScope, native_symbol: impl Into<String>) -> Self {
        Self {
            venue: scope.venue.clone(),
            environment: scope.environment.clone(),
            native_symbol: native_symbol.into(),
        }
    }

    pub fn validate(&self, scope: &InstrumentScope) -> Result<(), IdentityError> {
        scope.validate()?;
        valid_text(&self.native_symbol, "native symbol")?;
        if self.venue != scope.venue || self.environment != scope.environment {
            return Err(IdentityError::Invalid(format!(
                "instrument scope {}/{} differs from registry scope {}/{}",
                self.venue, self.environment, scope.venue, scope.environment
            )));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    tag = "status",
    content = "key",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum InstrumentIdentity {
    Unresolved,
    Resolved(InstrumentKey),
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentBinding {
    pub symbol: String,
    pub identity: InstrumentIdentity,
}

/// Vector indices retain the exact meaning of every archived dense id.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct IdentityState {
    pub schema_version: u16,
    pub scope: Option<InstrumentScope>,
    pub sleeves: Vec<SleeveKey>,
    pub instruments: Vec<InstrumentBinding>,
}

impl Default for IdentityState {
    fn default() -> Self {
        Self {
            schema_version: IDENTITY_SCHEMA_VERSION,
            scope: None,
            sleeves: Vec::new(),
            instruments: Vec::new(),
        }
    }
}

impl IdentityState {
    pub fn validate(&self) -> Result<(), IdentityError> {
        if self.schema_version != IDENTITY_SCHEMA_VERSION {
            return Err(IdentityError::Invalid(format!(
                "unsupported registry schema {}",
                self.schema_version
            )));
        }
        if self.sleeves.len() > DENSE_ID_CAPACITY {
            return Err(IdentityError::SleeveIdsExhausted);
        }
        if self.instruments.len() > DENSE_ID_CAPACITY {
            return Err(IdentityError::SymbolIdsExhausted);
        }
        if let Some(scope) = &self.scope {
            scope.validate()?;
        }
        let mut sleeves = BTreeSet::new();
        for key in &self.sleeves {
            if !sleeves.insert(key) {
                return Err(IdentityError::Invalid(format!(
                    "duplicate sleeve key {:?}",
                    key.as_str()
                )));
            }
        }
        let mut symbols = BTreeSet::new();
        let mut native_keys = BTreeSet::new();
        for binding in &self.instruments {
            valid_text(&binding.symbol, "logged symbol")?;
            if !symbols.insert(&binding.symbol) {
                return Err(IdentityError::Invalid(format!(
                    "duplicate logged symbol {:?}",
                    binding.symbol
                )));
            }
            if let InstrumentIdentity::Resolved(key) = &binding.identity {
                key.validate(self.scope.as_ref().ok_or_else(|| {
                    IdentityError::Invalid("resolved instrument has no registry scope".into())
                })?)?;
                if !native_keys.insert(key) {
                    return Err(IdentityError::Invalid(format!(
                        "native instrument {:?} is assigned more than one dense id",
                        key.native_symbol
                    )));
                }
            }
        }
        Ok(())
    }

    pub fn validate_extension(&self, prior: &Self) -> Result<(), IdentityError> {
        self.validate()?;
        prior.validate()?;
        if prior.scope.is_some() && self.scope != prior.scope {
            return Err(IdentityError::Invalid(
                "registry venue/environment changed".into(),
            ));
        }
        if !self.sleeves.starts_with(&prior.sleeves) {
            return Err(IdentityError::Invalid(
                "registered sleeve ids were removed or reassigned".into(),
            ));
        }
        if self.instruments.len() < prior.instruments.len() {
            return Err(IdentityError::Invalid(
                "registered instrument ids were removed".into(),
            ));
        }
        for (old, new) in prior.instruments.iter().zip(&self.instruments) {
            if old.symbol != new.symbol
                || (matches!(old.identity, InstrumentIdentity::Resolved(_))
                    && old.identity != new.identity)
            {
                return Err(IdentityError::Invalid(format!(
                    "registered instrument {:?} was reassigned",
                    old.symbol
                )));
            }
        }
        Ok(())
    }
}

impl<'de> Deserialize<'de> for IdentityState {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            schema_version: u16,
            scope: Option<InstrumentScope>,
            sleeves: Vec<SleeveKey>,
            instruments: Vec<InstrumentBinding>,
        }
        let wire = Wire::deserialize(deserializer)?;
        let state = Self {
            schema_version: wire.schema_version,
            scope: wire.scope,
            sleeves: wire.sleeves,
            instruments: wire.instruments,
        };
        state.validate().map_err(serde::de::Error::custom)?;
        Ok(state)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalSourceSleeve {
    pub source: String,
    pub sleeve: SleeveKey,
}
