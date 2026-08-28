//! Account-level daily loss halt.
//!
//! The anchor is the UTC day's opening equity, not a high-water mark. A trip
//! stays latched until an operator resets it, and both states survive restart.

use serde::{Deserialize, Serialize};

use crate::config::LossGuardConfig;

const NS_PER_DAY: u64 = 86_400 * 1_000_000_000;
const EVIDENCE_CHECKPOINT_NS: u64 = 60 * 1_000_000_000;

/// The equity reading that tripped the ceiling and its floor.
#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Trip {
    pub equity_usdt: f64,
    pub floor_usdt: f64,
}

/// The state the write-ahead log must preserve across a restart.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LossGuardAnchor {
    pub day: Option<u64>,
    pub opening_equity_usdt: Option<f64>,
    /// A recent durable reading used to bridge a UTC boundary or restart.
    /// It is checkpointed once a minute and immediately whenever equity rises:
    /// losing a higher pre-boundary observation could otherwise refresh away
    /// a loss after a crash.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_equity_usdt: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_observed_wall_ns: Option<u64>,
    pub trip: Option<Trip>,
}

/// A serialized empty anchor for the operator reset path.
pub fn cleared_loss_guard_state() -> String {
    serde_json::to_string(&LossGuardAnchor::default())
        .expect("the fixed loss-guard reset state must serialize")
}

#[derive(Clone, Debug)]
pub(crate) struct LossGuard {
    cfg: LossGuardConfig,
    anchor: LossGuardAnchor,
    latest_equity_usdt: Option<f64>,
    latest_wall_ns: Option<u64>,
}

impl LossGuard {
    pub(crate) fn new(cfg: LossGuardConfig) -> Self {
        Self {
            cfg,
            anchor: LossGuardAnchor::default(),
            latest_equity_usdt: None,
            latest_wall_ns: None,
        }
    }

    pub(crate) fn anchor(&self) -> LossGuardAnchor {
        self.anchor.clone()
    }

    pub(crate) fn restore(&mut self, anchor: LossGuardAnchor) -> Result<(), String> {
        if let Some(opening) = anchor.opening_equity_usdt {
            if !opening.is_finite() || opening <= 0.0 {
                return Err("opening equity is not a positive finite number".to_string());
            }
        }

        match (anchor.last_equity_usdt, anchor.last_observed_wall_ns) {
            (Some(equity), Some(_)) if equity.is_finite() => {}
            (None, None) => {}
            (Some(_), Some(_)) => {
                return Err("last checkpoint equity is not finite".to_string());
            }
            _ => {
                return Err(
                    "last checkpoint must contain both equity and observation time".to_string(),
                );
            }
        }
        if let (Some(day), Some(observed_ns)) = (anchor.day, anchor.last_observed_wall_ns) {
            if observed_ns / NS_PER_DAY > day {
                return Err("last checkpoint is newer than the anchored UTC day".to_string());
            }
        }

        if self.cfg.max_daily_loss_usdt.is_none() && anchor != LossGuardAnchor::default() {
            return Err("loss-guard state exists while the loss guard is disabled".to_string());
        }

        if let Some(trip) = anchor.trip {
            let opening = anchor
                .opening_equity_usdt
                .ok_or_else(|| "a loss trip has no opening-equity anchor".to_string())?;
            let ceiling = self
                .cfg
                .max_daily_loss_usdt
                .ok_or_else(|| "a loss trip exists while the loss guard is disabled".to_string())?;
            if !trip.equity_usdt.is_finite() || !trip.floor_usdt.is_finite() {
                return Err("loss-trip values are not finite".to_string());
            }
            let expected_floor = opening - ceiling;
            if trip.floor_usdt.to_bits() != expected_floor.to_bits() {
                return Err(format!(
                    "loss-trip floor {} does not match opening equity {} minus ceiling {}",
                    trip.floor_usdt, opening, ceiling
                ));
            }
            if trip.equity_usdt > trip.floor_usdt {
                return Err("loss-trip equity is above its trip floor".to_string());
            }
        }

        self.latest_equity_usdt = anchor.last_equity_usdt;
        self.latest_wall_ns = anchor.last_observed_wall_ns;
        self.anchor = anchor;
        Ok(())
    }

    /// Clear a trip and its opening anchor. Only an explicit operator action
    /// may call this.
    pub(crate) fn reset(&mut self) {
        self.anchor = LossGuardAnchor::default();
        self.latest_equity_usdt = None;
        self.latest_wall_ns = None;
    }

    pub(crate) fn is_tripped(&self) -> bool {
        self.anchor.trip.is_some()
    }

    /// Fold one fresh equity reading in. Without wall time, the first reading
    /// anchors once and the budget never refreshes.
    pub(crate) fn observe(&mut self, equity_usdt: f64, wall_ns: Option<u64>) -> Option<Trip> {
        if let Some(trip) = self.anchor.trip {
            return Some(trip);
        }
        if !equity_usdt.is_finite() {
            return None;
        }
        let ceiling = self.cfg.max_daily_loss_usdt?;

        let day = wall_ns.map(|ns| ns / NS_PER_DAY);
        let prior_equity = self.latest_equity_usdt;
        let prior_day = self.latest_wall_ns.map(|ns| ns / NS_PER_DAY);
        let rolled = matches!((self.anchor.day, day), (Some(then), Some(now)) if now > then);
        if self.anchor.opening_equity_usdt.is_none() && equity_usdt > 0.0 {
            self.anchor.day = day;
            self.anchor.opening_equity_usdt = Some(equity_usdt);
            self.checkpoint(equity_usdt, wall_ns, true);
            self.remember(equity_usdt, wall_ns);
            return None;
        }

        if rolled {
            // A read cannot identify the exact midnight mark. Use the last
            // pre-boundary evidence when it really belongs to the prior day,
            // and choose the higher value. That can consume too much budget
            // after an outage, but it can never erase a cross-boundary loss.
            let prior = match (prior_equity, prior_day, day) {
                (Some(equity), Some(then), Some(now)) if then < now => Some(equity),
                _ => None,
            };
            let opening = prior.map_or(equity_usdt, |equity| equity.max(equity_usdt));
            self.anchor.day = day;
            self.anchor.opening_equity_usdt = (opening > 0.0).then_some(opening);
            self.checkpoint(equity_usdt, wall_ns, true);
        }

        // Learning the clock after the first reading must not refresh the
        // day's budget.
        if self.anchor.day.is_none() {
            self.anchor.day = day;
        }

        self.checkpoint(equity_usdt, wall_ns, false);
        self.remember(equity_usdt, wall_ns);

        let opening = self.anchor.opening_equity_usdt?;
        let floor_usdt = opening - ceiling;
        if equity_usdt > floor_usdt {
            return None;
        }

        let trip = Trip {
            equity_usdt,
            floor_usdt,
        };
        self.anchor.trip = Some(trip);
        Some(trip)
    }

    fn remember(&mut self, equity_usdt: f64, wall_ns: Option<u64>) {
        let Some(wall_ns) = wall_ns else {
            return;
        };
        if self.latest_wall_ns.is_none_or(|prior| wall_ns >= prior) {
            self.latest_equity_usdt = Some(equity_usdt);
            self.latest_wall_ns = Some(wall_ns);
        }
    }

    fn checkpoint(&mut self, equity_usdt: f64, wall_ns: Option<u64>, force: bool) {
        let Some(wall_ns) = wall_ns else {
            return;
        };
        let due = self
            .anchor
            .last_observed_wall_ns
            .is_none_or(|prior| wall_ns >= prior.saturating_add(EVIDENCE_CHECKPOINT_NS));
        let raises_boundary_evidence = self
            .anchor
            .last_equity_usdt
            .is_none_or(|prior| equity_usdt > prior);
        if force || due || raises_boundary_evidence {
            self.anchor.last_equity_usdt = Some(equity_usdt);
            self.anchor.last_observed_wall_ns = Some(wall_ns);
        }
    }
}
