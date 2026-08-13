//! Account-level daily loss halt, ported from `account_loss_guard.py`.
//!
//! The anchor is the UTC day's opening equity, not a high-water mark, and it is
//! snapshotable so a restart cannot hand the day a fresh loss budget.

use serde::{Deserialize, Serialize};

use crate::config::LossGuardConfig;

const NS_PER_DAY: u64 = 86_400 * 1_000_000_000;

/// The frozen reading that tripped the ceiling.
#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Trip {
    pub equity_usdt: f64,
    pub floor_usdt: f64,
}

/// The persistable anchor. Write this beside the log; a forgotten anchor is an
/// unlimited daily loss, one restart at a time.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct LossGuardAnchor {
    pub day: Option<u64>,
    pub opening_equity_usdt: Option<f64>,
    pub trip: Option<Trip>,
}

#[derive(Clone, Debug)]
pub(crate) struct LossGuard {
    cfg: LossGuardConfig,
    anchor: LossGuardAnchor,
}

impl LossGuard {
    pub(crate) fn new(cfg: LossGuardConfig) -> Self {
        Self {
            cfg,
            anchor: LossGuardAnchor::default(),
        }
    }

    pub(crate) fn anchor(&self) -> LossGuardAnchor {
        self.anchor.clone()
    }

    pub(crate) fn restore(&mut self, anchor: LossGuardAnchor) {
        let opening = anchor
            .opening_equity_usdt
            .filter(|value| value.is_finite() && *value > 0.0);
        self.anchor = LossGuardAnchor {
            day: anchor.day,
            opening_equity_usdt: opening,
            trip: anchor.trip,
        };
    }

    /// Clear a trip. Only an explicit operator action may call this.
    pub(crate) fn reset(&mut self) {
        self.anchor = LossGuardAnchor::default();
    }

    /// Fold one fresh equity reading in. `wall_ns` is venue wall-clock time for
    /// the UTC day roll, or `None` when the engine has not observed one yet;
    /// without it the anchor is set once and never re-anchors.
    pub(crate) fn observe(&mut self, equity_usdt: f64, wall_ns: Option<u64>) -> Option<Trip> {
        if let Some(trip) = self.anchor.trip {
            return Some(trip);
        }
        let day = wall_ns.map(|ns| ns / NS_PER_DAY);
        let rolled = match (self.anchor.day, day) {
            (Some(anchored), Some(now)) => anchored != now,
            _ => false,
        };
        if self.anchor.opening_equity_usdt.is_none() || rolled {
            self.anchor.day = day;
            self.anchor.opening_equity_usdt = Some(equity_usdt);
            return None;
        }
        // Learning the clock late records the day but must not hand the day a
        // fresh budget.
        if self.anchor.day.is_none() {
            self.anchor.day = day;
        }
        let ceiling = self.cfg.max_daily_loss_usdt?;
        let opening = self.anchor.opening_equity_usdt?;
        let floor_usdt = opening - ceiling;
        if equity_usdt <= floor_usdt {
            let trip = Trip {
                equity_usdt,
                floor_usdt,
            };
            self.anchor.trip = Some(trip);
            return Some(trip);
        }
        None
    }
}
