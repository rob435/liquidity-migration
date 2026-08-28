//! Quantization to instrument rules, applied once at the venue boundary.

use crate::orders::{InstrumentRule, Side};

/// How far a count of ticks may sit from a whole number and still be that
/// whole number. This is deliberately an absolute tolerance in step-count
/// space. A relative tolerance grows into a real fraction of a tick for large
/// orders and can silently round risk upward.
const DUST: f64 = 1e-12;

/// A count of whole steps, with float dust shaved before it is rounded.
///
/// `0.29 / 0.01` is 28.999999999999996, not 29. Flooring that loses a whole
/// step, so a price already on the venue's tick comes back a tick away from
/// where the strategy put it and a size the risk kernel approved is sent
/// short. No genuine sub-step offset can be this small: the venue cannot
/// represent one.
pub fn steps(value: f64, step: f64) -> f64 {
    shave_dust(value / step)
}

/// The same shave for venues that scale by a power of ten instead of dividing
/// by a step — `qty * 10^decimals` carries the same dust.
pub fn shave_dust(count: f64) -> f64 {
    let whole = count.round();
    if (count - whole).abs() <= DUST {
        whole
    } else {
        count
    }
}

/// Round a price to the instrument tick, toward the passive side: buys round
/// down, sells round up, so quantization never makes an order more
/// aggressive than the strategy asked for.
pub fn quantize_px(px: f64, side: Side, rule: &InstrumentRule) -> f64 {
    let ticks = steps(px, rule.tick_size);
    let snapped = match side {
        Side::Buy => ticks.floor(),
        Side::Sell => ticks.ceil(),
    };
    round_clean(snapped * rule.tick_size, rule.tick_size)
}

/// Round a quantity DOWN to the step (never size up), returning `None` when
/// the result falls below the venue minimum.
pub fn quantize_qty(qty: f64, rule: &InstrumentRule) -> Option<f64> {
    if !qty.is_finite()
        || qty <= 0.0
        || !rule.qty_step.is_finite()
        || rule.qty_step <= 0.0
        || !rule.min_qty.is_finite()
        || rule.min_qty < 0.0
    {
        return None;
    }

    let mut whole_steps = steps(qty, rule.qty_step).floor();
    if !whole_steps.is_finite() || whole_steps < 1.0 {
        return None;
    }
    let mut stepped = round_clean(whole_steps * rule.qty_step, rule.qty_step);

    // Dust recovery is useful, but this final boundary is intentionally
    // stricter: no representation artifact may enlarge approved risk.
    if stepped > qty {
        let smaller_steps = whole_steps - 1.0;
        if smaller_steps >= whole_steps {
            return None;
        }
        whole_steps = smaller_steps;
        stepped = round_clean(whole_steps * rule.qty_step, rule.qty_step);
    }
    if !stepped.is_finite() || stepped > qty || stepped + 1e-12 < rule.min_qty || stepped <= 0.0 {
        return None;
    }
    Some(stepped)
}

/// Shave float dust so 0.1 + 0.2-style artifacts do not leak into venue
/// payloads: round to the step's own decimal precision.
///
/// Public because quantizing an order is not the only venue-boundary
/// conversion that carries dust. A venue that reports sizes in its own unit —
/// MEXC counts contracts, not coins — multiplies on the way back, and `3 *
/// 0.0001` is 0.00030000000000000003. A size that differs from the engine's
/// own ledger in the last bit fails a reconciliation that is supposed to be
/// exact.
pub fn round_clean(value: f64, step: f64) -> f64 {
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
        assert_eq!(quantize_qty(f64::NAN, &RULE), None);
    }

    #[test]
    fn float_dust_is_shaved() {
        let rule = InstrumentRule {
            tick_size: 0.1,
            qty_step: 0.1,
            min_qty: 0.1,
            min_notional: 0.0,
        };
        assert_eq!(quantize_px(0.30000000000000004, Side::Buy, &rule), 0.3);
    }

    #[test]
    fn a_price_already_on_the_tick_is_left_where_it_is() {
        // `0.3 / 0.1` is 2.9999999999999996 and `0.07 / 0.01` is
        // 7.000000000000001. Rounding those to whole ticks without shaving the
        // dust moves a buy a tick down and a sell a tick up — silently, on
        // every order whose price the strategy took off the book.
        for (px, tick) in [
            (0.3, 0.1),
            (2.9, 0.1),
            (8.2, 0.1),
            (0.29, 0.01),
            (112.35, 0.05),
        ] {
            let rule = InstrumentRule {
                tick_size: tick,
                qty_step: tick,
                min_qty: 0.0,
                min_notional: 0.0,
            };
            assert_eq!(
                quantize_px(px, Side::Buy, &rule),
                px,
                "buy at {px} on {tick}"
            );
            assert_eq!(
                quantize_px(px, Side::Sell, &rule),
                px,
                "sell at {px} on {tick}"
            );
            assert_eq!(quantize_qty(px, &rule), Some(px), "size {px} on {tick}");
        }
    }

    #[test]
    fn a_price_between_ticks_still_moves_to_the_passive_one() {
        // The other half: the shave must not swallow a real difference. Half a
        // tick out is a real difference and still rounds the passive way.
        let rule = InstrumentRule {
            tick_size: 0.1,
            qty_step: 0.1,
            min_qty: 0.0,
            min_notional: 0.0,
        };
        assert_eq!(quantize_px(0.35, Side::Buy, &rule), 0.3);
        assert_eq!(quantize_px(0.35, Side::Sell, &rule), 0.4);
        assert_eq!(quantize_qty(0.35, &rule), Some(0.3));
    }

    #[test]
    fn the_shave_reaches_dust_and_stops_well_short_of_a_step() {
        assert_eq!(shave_dust(28.999999999999996), 29.0);
        assert_eq!(shave_dust(7.000000000000001), 7.0);
        // A tenth of a step out is a real difference at every scale.
        assert_eq!(shave_dust(29.1), 29.1);
        assert_eq!(shave_dust(1_000_000_029.1), 1_000_000_029.1);
        assert_eq!(shave_dust(1_000_000_000_000.75), 1_000_000_000_000.75);
        assert_eq!(shave_dust(0.0), 0.0);
    }

    #[test]
    fn quantity_quantization_never_exceeds_a_huge_approved_input() {
        let rule = InstrumentRule {
            tick_size: 1.0,
            qty_step: 1.0,
            min_qty: 1.0,
            min_notional: 0.0,
        };
        let approved = 1_000_000_000_000.75;
        let quantized = quantize_qty(approved, &rule).expect("quantity remains tradable");
        assert_eq!(quantized, 1_000_000_000_000.0);
        assert!(quantized <= approved);

        let just_below_one = f64::from_bits(1.0_f64.to_bits() - 1);
        assert_eq!(quantize_qty(just_below_one, &rule), None);
    }
}
