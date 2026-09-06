//! Equity reference, deadband and loss allowance in account quote units.
use crate::config::EnvelopeConfig;
use crate::kernel::policy;
use engine_types::numeric::Exact;
#[derive(Clone, Debug)]
pub(crate) struct Envelope {
    cfg: EnvelopeConfig,
    reference_usdt: Exact,
}
impl Envelope {
    pub(crate) fn new(cfg: EnvelopeConfig) -> Self {
        let reference_usdt = policy(cfg.reference_usdt);
        Self {
            cfg,
            reference_usdt,
        }
    }
    pub(crate) fn reference_usdt(&self) -> &Exact {
        &self.reference_usdt
    }
    pub(crate) fn scale(&self) -> Exact {
        self.reference_usdt
            .checked_div(&policy(self.cfg.reference_usdt))
            .expect("validated positive reference")
    }
    pub(crate) fn allowance_usdt(&self) -> Exact {
        &self.reference_usdt
            * policy(self.cfg.gross_notional_multiple)
            * policy(self.cfg.disaster_stop_fraction)
    }
    pub(crate) fn observe_equity(&mut self, equity: &Exact) -> bool {
        if !self.cfg.tracks_equity || !equity.is_positive() {
            return false;
        }
        let target = (equity * policy(self.cfg.equity_fraction)).max(policy(self.cfg.floor_usdt));
        let current = &self.reference_usdt;
        if close_enough(&target, current) {
            return false;
        }
        if target >= *current
            && target <= current * (Exact::one() + policy(self.cfg.expand_dead_band_fraction))
        {
            return false;
        }
        self.reference_usdt = target;
        true
    }
    pub(crate) fn position_worst_case_usdt(&self, notional: &Exact, stop: &Exact) -> Exact {
        notional * stop.clone().max(policy(self.cfg.disaster_stop_fraction))
    }
}
fn close_enough(a: &Exact, b: &Exact) -> bool {
    (a - b).abs() <= (policy(1e-12) * a.abs().max(b.abs())).max(policy(1e-9))
}
