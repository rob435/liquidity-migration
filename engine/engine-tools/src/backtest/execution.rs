//! Explicit execution assumptions for inputs without observed depth.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
pub enum ExecutionModel {
    #[default]
    Books,
    Trades {
        spread_bps: f64,
        slippage_bps: f64,
        participation: f64,
    },
    Bars {
        spread_bps: f64,
        slippage_bps: f64,
        participation: f64,
    },
}

impl ExecutionModel {
    pub fn validate(&self) -> Result<(), String> {
        let Some((spread, slip, participation)) = self.assumptions() else {
            return Ok(());
        };
        if !spread.is_finite()
            || spread < 0.0
            || !slip.is_finite()
            || slip < 0.0
            || spread / 2.0 + slip >= 10_000.0
            || !participation.is_finite()
            || participation <= 0.0
            || participation > 1.0
        {
            return Err("execution assumptions require nonnegative finite spread/slippage below 10000 bps combined and 0 < participation <= 1".into());
        }
        Ok(())
    }

    pub fn assumptions(&self) -> Option<(f64, f64, f64)> {
        match *self {
            Self::Books => None,
            Self::Trades {
                spread_bps,
                slippage_bps,
                participation,
            }
            | Self::Bars {
                spread_bps,
                slippage_bps,
                participation,
            } => Some((spread_bps, slippage_bps, participation)),
        }
    }

    pub fn price(&self, reference: f64, side: engine_types::Side) -> f64 {
        let Some((spread, slip, _)) = self.assumptions() else {
            return reference;
        };
        let sign = if side == engine_types::Side::Buy {
            1.0
        } else {
            -1.0
        };
        reference * (1.0 + sign * (spread / 2.0 + slip) / 10_000.0)
    }
}
