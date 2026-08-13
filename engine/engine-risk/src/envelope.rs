//! The equity-anchored envelope, ported from `equity_anchored_envelope.py`.
//!
//! The capital reference is a fraction of the wallet, floored. Contraction is
//! immediate; expansion waits for a move larger than a dead band so ordinary
//! equity wander cannot re-scale the book every cycle. An unreadable equity
//! holds the reference — unknown is not evidence of small.

use crate::config::EnvelopeConfig;

#[derive(Clone, Debug)]
pub(crate) struct Envelope {
    cfg: EnvelopeConfig,
    reference_usdt: f64,
}

impl Envelope {
    pub(crate) fn new(cfg: EnvelopeConfig) -> Self {
        let reference_usdt = cfg.reference_usdt;
        Self {
            cfg,
            reference_usdt,
        }
    }

    pub(crate) fn reference_usdt(&self) -> f64 {
        self.reference_usdt
    }

    /// How much of the configured reference the envelope currently stands at.
    /// Every cap sized against the reference scales by this.
    pub(crate) fn scale(&self) -> f64 {
        self.reference_usdt / self.cfg.reference_usdt
    }

    /// The worst-case loss the whole book is allowed to carry: the account
    /// gross notional cap, priced at the distance a position can move before
    /// its stop ends it.
    pub(crate) fn allowance_usdt(&self) -> f64 {
        self.reference_usdt * self.cfg.gross_notional_multiple * self.cfg.disaster_stop_fraction
    }

    /// Fold one equity reading in. Returns true if the reference moved.
    pub(crate) fn observe_equity(&mut self, equity_usdt: f64) -> bool {
        if !self.cfg.tracks_equity {
            return false;
        }
        if !equity_usdt.is_finite() || equity_usdt <= 0.0 {
            return false;
        }
        let target = (equity_usdt * self.cfg.equity_fraction).max(self.cfg.floor_usdt);
        let current = self.reference_usdt;
        if close_enough(target, current) {
            return false;
        }
        let contracting = target < current;
        if !contracting && target <= current * (1.0 + self.cfg.expand_dead_band_fraction) {
            return false;
        }
        self.reference_usdt = target;
        true
    }

    /// Worst-case loss on one position: the disaster stop distance, or the
    /// intent's own stop distance when that is wider.
    pub(crate) fn position_worst_case_usdt(&self, notional_usdt: f64, stop_fraction: f64) -> f64 {
        let fraction =
            if stop_fraction.is_finite() && stop_fraction > self.cfg.disaster_stop_fraction {
                stop_fraction
            } else {
                self.cfg.disaster_stop_fraction
            };
        notional_usdt * fraction
    }
}

/// Python's `math.isclose(rel_tol=1e-12, abs_tol=1e-9)`.
fn close_enough(a: f64, b: f64) -> bool {
    (a - b).abs() <= f64::max(1e-12 * f64::max(a.abs(), b.abs()), 1e-9)
}
