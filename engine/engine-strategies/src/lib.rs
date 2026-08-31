//! The plugs: strategies the engine loads by name.
//!
//! The credential-free Rust signal worker supplies normalized public
//! observations. The engine journals them, then native LONG and CARRY plugs
//! run typed pure reducers; native Exodus consumes CARRY's durable internal
//! events. Offline research supplies rules and replay fixtures only. Plugs do
//! not reach out for data, a clock, or a socket: they translate engine events
//! and context into reducer input and apply the reducer's ordered effects.

pub mod native_carry;
pub mod native_common;
pub mod native_config;
pub mod native_exodus;
pub mod native_long;
mod params;
pub mod position_plan;
pub mod quoter;

#[cfg(test)]
mod mock_ctx;
#[cfg(test)]
mod registry_tests;

use std::fmt;

use engine_types::{Strategy, StrategyId};

/// How one plug is built from its id and its parameter table.
type Builder = fn(StrategyId, &toml::Value) -> Result<Box<dyn Strategy>, BuildError>;

/// Every plug the engine can load, and how to load it.
///
/// One table: the name the engine advertises and the builder it reaches are
/// one fact in one place, so a plug is added in one line and cannot be half
/// added.
const PLUGS: &[(&str, Builder)] = &[
    (native_carry::plug::NAME, |id, params| {
        Ok(Box::new(native_carry::plug::NativeCarry::from_params(
            id, params,
        )?))
    }),
    (native_long::plug::NAME, |id, params| {
        Ok(Box::new(native_long::plug::NativeLong::from_params(
            id, params,
        )?))
    }),
    (native_exodus::plug::NAME, |id, params| {
        Ok(Box::new(native_exodus::plug::NativeExodus::from_params(
            id, params,
        )?))
    }),
    (quoter::plug::NAME, |id, params| {
        Ok(Box::new(quoter::Quoter::from_params(id, params)?))
    }),
];

/// Every name [`build_strategy`] answers to, in the order the table lists
/// them.
pub fn known_strategies() -> Vec<&'static str> {
    PLUGS.iter().map(|(name, _)| *name).collect()
}

/// Why a config block could not become a strategy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BuildError {
    /// No plug by that name is compiled in.
    UnknownStrategy { name: String },
    /// The params were not a table of key/value pairs at all.
    ParamsNotATable {
        strategy: &'static str,
        got: &'static str,
    },
    /// A required parameter is absent.
    MissingParam {
        strategy: &'static str,
        param: &'static str,
    },
    /// A parameter is present but unusable.
    InvalidParam {
        strategy: &'static str,
        param: &'static str,
        detail: String,
    },
    /// A key the strategy does not read — a typo would otherwise silently
    /// change behavior.
    UnknownParam {
        strategy: &'static str,
        param: String,
        known: &'static [&'static str],
    },
}

impl fmt::Display for BuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            BuildError::UnknownStrategy { name } => {
                write!(
                    f,
                    "unknown strategy \"{name}\" (known: {})",
                    known_strategies().join(", ")
                )
            }
            BuildError::ParamsNotATable { strategy, got } => {
                write!(f, "{strategy}: expected a table of parameters, got {got}")
            }
            BuildError::MissingParam { strategy, param } => {
                write!(f, "{strategy}: missing parameter \"{param}\"")
            }
            BuildError::InvalidParam {
                strategy,
                param,
                detail,
            } => {
                write!(f, "{strategy}: parameter \"{param}\" is invalid: {detail}")
            }
            BuildError::UnknownParam {
                strategy,
                param,
                known,
            } => {
                write!(
                    f,
                    "{strategy}: parameter \"{param}\" is not one it reads (it reads: {})",
                    known.join(", ")
                )
            }
        }
    }
}

impl std::error::Error for BuildError {}

/// Build one strategy from its name and its parameter table.
///
/// `strategy_id` is the engine's own index for this plug, assigned at boot in
/// config order; every intent the plug emits carries it, which is how the log
/// says whose position is whose.
///
/// Engine configuration keeps `name` and `sleeve`; the remaining strict
/// parameter table is handed to the selected plug.
pub fn build_strategy(
    name: &str,
    strategy_id: StrategyId,
    params: &toml::Value,
) -> Result<Box<dyn Strategy>, BuildError> {
    match PLUGS.iter().find(|(known, _)| *known == name) {
        Some((_, build)) => build(strategy_id, params),
        None => Err(BuildError::UnknownStrategy {
            name: name.to_string(),
        }),
    }
}
