//! Quantization to instrument rules, applied once at the venue boundary.

use crate::orders::{InstrumentRule, Side};

/// Round a price to the instrument tick, toward the passive side: buys round
/// down, sells round up, so quantization never makes an order more
/// aggressive than the strategy asked for.
pub fn quantize_px(px: f64, side: Side, rule: &InstrumentRule) -> f64 {
    let ticks = px / rule.tick_size;
    let snapped = match side {
        Side::Buy => ticks.floor(),
        Side::Sell => ticks.ceil(),
    };
    round_clean(snapped * rule.tick_size, rule.tick_size)
}

/// Round a quantity DOWN to the step (never size up), returning `None` when
/// the result falls below the venue minimum.
pub fn quantize_qty(qty: f64, rule: &InstrumentRule) -> Option<f64> {
    let stepped = round_clean((qty / rule.qty_step).floor() * rule.qty_step, rule.qty_step);
    if stepped + 1e-12 < rule.min_qty || stepped <= 0.0 {
        return None;
    }
    Some(stepped)
}

/// Shave float dust so 0.1 + 0.2-style artifacts do not leak into venue
/// payloads: round to the step's own decimal precision.
fn round_clean(value: f64, step: f64) -> f64 {
    let decimals = decimals_of(step);
    let scale = 10f64.powi(decimals);
    (value * scale).round() / scale
}

fn decimals_of(step: f64) -> i32 {
    let mut decimals = 0;
    let mut s = step;
    while s.fract().abs() > 1e-9 && decimals < 12 {
        s *= 10.0;
        decimals += 1;
    }
    decimals
}

#[cfg(test)]
mod tests {
    use super::*;

    const RULE: InstrumentRule = InstrumentRule {
        tick_size: 0.5,
        qty_step: 0.001,
        min_qty: 0.001,
        min_notional: 5.0,
    };

    #[test]
    fn buys_round_down_sells_round_up() {
        assert_eq!(quantize_px(100.3, Side::Buy, &RULE), 100.0);
        assert_eq!(quantize_px(100.3, Side::Sell, &RULE), 100.5);
        assert_eq!(quantize_px(100.5, Side::Buy, &RULE), 100.5);
    }

    #[test]
    fn qty_rounds_down_and_respects_minimum() {
        assert_eq!(quantize_qty(0.0019, &RULE), Some(0.001));
        assert_eq!(quantize_qty(0.0009, &RULE), None);
        assert_eq!(quantize_qty(0.0, &RULE), None);
    }

    #[test]
    fn float_dust_is_shaved() {
        let rule = InstrumentRule { tick_size: 0.1, qty_step: 0.1, min_qty: 0.1, min_notional: 0.0 };
        assert_eq!(quantize_px(0.30000000000000004, Side::Buy, &rule), 0.3);
    }
}
