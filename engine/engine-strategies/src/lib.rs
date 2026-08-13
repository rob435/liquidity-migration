//! The plugs: strategies the engine loads by name.
//!
//! This crate is the seam between the two halves of the repository. The
//! research system (Python) decides what to trade and writes it down as a
//! small config block; the engine core reads that file and hands each block
//! here. Nothing else crosses the wall — no research code runs in the engine,
//! and no plug reaches back for data, a clock, or a socket. A plug sees only
//! the events it is handed and the context it is given.

mod params;
mod touch_sniper;

#[cfg(test)]
mod mock_ctx;
#[cfg(test)]
mod registry_tests;

use std::fmt;

use engine_types::{Strategy, StrategyId};

pub use touch_sniper::TouchSniper;

/// Every name [`build_strategy`] answers to.
pub const KNOWN_STRATEGIES: &[&str] = &[touch_sniper::NAME];

/// Why a config block could not become a strategy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BuildError {
    /// No plug by that name is compiled in.
    UnknownStrategy { name: String },
    /// The params were not a table of key/value pairs at all.
    ParamsNotATable { strategy: &'static str, got: &'static str },
    /// A required parameter is absent.
    MissingParam { strategy: &'static str, param: &'static str },
    /// A parameter is present but unusable.
    InvalidParam { strategy: &'static str, param: &'static str, detail: String },
    /// A key the strategy does not read — a typo would otherwise silently
    /// change behavior (a misspelled take_px means "no profit level").
    UnknownParam { strategy: &'static str, param: String, known: &'static [&'static str] },
}

impl fmt::Display for BuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            BuildError::UnknownStrategy { name } => {
                write!(f, "unknown strategy \"{name}\" (known: {})", KNOWN_STRATEGIES.join(", "))
            }
            BuildError::ParamsNotATable { strategy, got } => {
                write!(f, "{strategy}: expected a table of parameters, got {got}")
            }
            BuildError::MissingParam { strategy, param } => {
                write!(f, "{strategy}: missing parameter \"{param}\"")
            }
            BuildError::InvalidParam { strategy, param, detail } => {
                write!(f, "{strategy}: parameter \"{param}\" is invalid: {detail}")
            }
            BuildError::UnknownParam { strategy, param, known } => {
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
/// config order; every intent the plug emits carries it, which is how the risk
/// kernel keeps each plug inside its own slice of capital.
///
/// The research system emits blocks in this shape, and the engine core hands
/// the `params` table of each one to this function:
///
/// ```toml
/// [[strategy]]
/// name = "touch_sniper"
///
/// [strategy.params]
/// symbol = "BTCUSDT"      # venue symbol
/// side = "buy"            # "buy" or "sell"
/// trigger_px = 61000.0    # the level to watch
/// qty = 0.01              # size in base units
/// stop_px = 60500.0       # required: an entry always carries a stop
/// take_px = 61800.0       # optional: leave here for a profit
/// ttl_s = 900             # optional: leave anyway after this long
/// ```
pub fn build_strategy(
    name: &str,
    strategy_id: StrategyId,
    params: &toml::Value,
) -> Result<Box<dyn Strategy>, BuildError> {
    match name {
        touch_sniper::NAME => Ok(Box::new(TouchSniper::from_params(strategy_id, params)?)),
        other => Err(BuildError::UnknownStrategy { name: other.to_string() }),
    }
}
