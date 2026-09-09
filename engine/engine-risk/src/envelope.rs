//! Equity reference, deadband and loss allowance in account quote units.
use crate::config::EnvelopeConfig;
use crate::kernel::policy;
use engine_types::numeric::Exact;
use std::collections::BTreeMap;
#[derive(Clone, Debug)]
pub(crate) struct Envelope {
    tracks_equity: bool,
    configured_reference_usdt: Exact,
    gross_notional_multiple: Exact,
    disaster_stop_fraction: Exact,
    equity_fraction: Exact,
    floor_usdt: Exact,
    expansion_multiple: Exact,
    reference_usdt: Exact,
}
impl Envelope {
    pub(crate) fn new(cfg: EnvelopeConfig) -> Self {
        let reference_usdt = policy(cfg.reference_usdt);
        Self {
            tracks_equity: cfg.tracks_equity,
            configured_reference_usdt: reference_usdt.clone(),
            gross_notional_multiple: policy(cfg.gross_notional_multiple),
            disaster_stop_fraction: policy(cfg.disaster_stop_fraction),
            equity_fraction: policy(cfg.equity_fraction),
            floor_usdt: policy(cfg.floor_usdt),
            expansion_multiple: Exact::one() + policy(cfg.expand_dead_band_fraction),
            reference_usdt,
        }
    }
    pub(crate) fn reference_usdt(&self) -> &Exact {
        &self.reference_usdt
    }
    pub(crate) fn scale(&self) -> Exact {
        self.reference_usdt
            .checked_div(&self.configured_reference_usdt)
            .expect("validated positive reference")
    }
    pub(crate) fn allowance_usdt(&self) -> Exact {
        &self.reference_usdt * &self.gross_notional_multiple * &self.disaster_stop_fraction
    }
    pub(crate) fn observe_equity_with_permission(
        &mut self,
        equity: &Exact,
        allow_expansion: bool,
    ) -> bool {
        if !self.tracks_equity || !equity.is_positive() {
            return false;
        }
        // `floor_usdt` is a viability threshold, not invented economic capital.
        // A shrinking account must keep shrinking its loss/margin allowances.
        let target = equity * &self.equity_fraction;
        let current = &self.reference_usdt;
        if close_enough(&target, current) {
            return false;
        }
        if target >= *current && (!allow_expansion || target <= current * &self.expansion_multiple)
        {
            return false;
        }
        self.reference_usdt = target;
        true
    }
    pub(crate) fn viable_for_new_exposure(&self) -> bool {
        !self.tracks_equity || self.reference_usdt >= self.floor_usdt
    }
    /// What this position loses if its stop fills at the trigger, charged at
    /// the wider of the intent's own stop distance and `disaster_stop_fraction`.
    ///
    /// It is not a bound on the account's loss: a gap through the trigger, a
    /// liquidation, a venue outage that leaves the stop unfilled, funding, or
    /// collateral that stops being worth what it was all lose more than this.
    pub(crate) fn modelled_stop_charge_usdt(&self, notional: &Exact, stop: &Exact) -> Exact {
        notional * stop.max(&self.disaster_stop_fraction)
    }
    pub(crate) fn pending_totals(&self, rows: &[(Exact, Exact)]) -> (Exact, Exact) {
        let mut by_fraction = BTreeMap::<&Exact, Exact>::new();
        for (notional, fraction) in rows {
            *by_fraction
                .entry(fraction.max(&self.disaster_stop_fraction))
                .or_insert_with(Exact::zero) += notional;
        }
        let mut gross = Exact::zero();
        let mut loss = Exact::zero();
        for (fraction, notional) in by_fraction {
            gross += &notional;
            loss += notional * fraction;
        }
        (gross, loss)
    }
}
fn close_enough(a: &Exact, b: &Exact) -> bool {
    (a - b).abs() <= (policy(1e-12) * a.abs().max(b.abs())).max(policy(1e-9))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grouped_pending_charges_match_individual_exact_rows() {
        let envelope = Envelope::new(EnvelopeConfig {
            tracks_equity: true,
            reference_usdt: 1000.0,
            equity_fraction: 0.75,
            floor_usdt: 1.0,
            expand_dead_band_fraction: 0.05,
            gross_notional_multiple: 10.0,
            disaster_stop_fraction: 0.35,
            max_component_gross_notional_usdt: 10000.0,
            max_symbol_notional_usdt: 10000.0,
            max_initial_margin_usdt: 1000.0,
        });
        let mut rows = Vec::<(Exact, Exact)>::new();
        for step in 0..256u64 {
            if step.is_multiple_of(5) && !rows.is_empty() {
                rows.remove(0);
            } else {
                rows.push((
                    Exact::from_ratio(&(step * step + 1).to_string(), "37").unwrap(),
                    Exact::parse_decimal(
                        ["0.1", "0.35", "0.35000000000000000000000001", "0.7"][step as usize % 4],
                    )
                    .unwrap(),
                ));
            }
            if let Some((notional, _)) = rows.last_mut() {
                *notional = notional.checked_div(&Exact::from_u64(3)).unwrap();
            }
            let expected = rows.iter().fold(
                (Exact::zero(), Exact::zero()),
                |(gross, loss), (notional, fraction)| {
                    (
                        gross + notional,
                        loss + envelope.modelled_stop_charge_usdt(notional, fraction),
                    )
                },
            );
            let actual = envelope.pending_totals(&rows);
            assert_eq!(actual, expected, "step {step}");
            assert_eq!(
                serde_json::to_vec(&actual).unwrap(),
                serde_json::to_vec(&expected).unwrap()
            );
        }
        assert_eq!(envelope.pending_totals(&[]), (Exact::zero(), Exact::zero()));
    }
}
