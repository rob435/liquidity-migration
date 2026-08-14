//! The decision, as a pure function.
//!
//! Everything here is plain numbers. No context, no clock, no engine state —
//! which is what makes every case below one function call instead of a
//! simulated market.
//!
//! The example decision: hold `target_qty` of one symbol, signed, and correct
//! the difference when it grows past a tolerance. Replace all of it. What is
//! worth keeping is the shape — one struct of settled rules, one struct of
//! what is currently true, one enum of what to do, and a function between
//! them that cannot lie about having read a file.

use engine_types::Side;

/// The settings, read once from config and never changed afterwards.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct Rules {
    /// How much to hold, signed: positive is long, negative is short, zero is
    /// flat.
    pub target_qty: f64,
    /// How far the holding may drift from the target before it is worth an
    /// order, as a fraction of the target. A tolerance stops a strategy from
    /// paying a fee to correct rounding.
    pub tolerance_fraction: f64,
    /// Where the stop goes on a new entry, as a fraction below (long) or
    /// above (short) the price it entered at.
    pub stop_loss_fraction: f64,
}

/// What the account reading says is held right now.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct Held {
    pub side: Side,
    /// Always positive; the side says which way.
    pub qty: f64,
    /// What the venue says the position was opened at. The stop on an
    /// existing position is measured from here, not from today's price —
    /// anchoring on today's price walks the stop along behind a losing
    /// position and quietly widens the loss it was there to bound.
    pub entry_px: f64,
}

impl Held {
    /// Positive for a long, negative for a short: the form the arithmetic
    /// wants, so nothing below has to branch on side.
    pub fn signed(&self) -> f64 {
        match self.side {
            Side::Buy => self.qty,
            Side::Sell => -self.qty,
        }
    }
}

/// One thing to do. `None` from [`plan`] means do nothing, which is a real
/// answer and the common one.
#[derive(Copy, Clone, Debug, PartialEq)]
pub enum Step {
    /// Add exposure. Carries a stop because the risk kernel refuses an
    /// opening order without one.
    Enter { side: Side, qty: f64, stop_px: f64 },
    /// Take exposure off. No stop: it is reducing, and it is sent
    /// reduce-only so it can never flip the position by overshooting.
    Reduce { side: Side, qty: f64 },
}

/// What to do about the difference between what is held and what is wanted.
///
/// `mark_px` is the price the difference is valued and stopped at — a mid, or
/// the side of the book the order would cross. A non-positive one means the
/// market picture is not usable, and the answer is to do nothing rather than
/// to guess.
pub fn plan(held: Option<Held>, mark_px: f64, rules: Rules) -> Option<Step> {
    // A missing price arrives as zero and a broken one as NaN, and neither is
    // something to size an order against.
    if !mark_px.is_finite() || mark_px <= 0.0 || !rules.target_qty.is_finite() {
        return None;
    }
    let have = held.map(|h| h.signed()).unwrap_or(0.0);
    let want = rules.target_qty;

    // Crossing zero is two decisions, not one: a single order sized
    // `want - have` would both close the old side and open the new one, and
    // the opening half would have no stop of its own. Take the old side off
    // first and let the next wake open the new one. Exits before entries is
    // the house rule everywhere in this engine.
    if have != 0.0 && want != 0.0 && have.signum() != want.signum() {
        return Some(Step::Reduce { side: closing_side(have), qty: have.abs() });
    }

    let delta = want - have;
    if delta.abs() <= drift_allowed(want, have, rules.tolerance_fraction) {
        return None;
    }

    // Past the cross-zero case above, the two are on the same side or one of
    // them is zero, so which way the order goes is settled by which is bigger.
    if want.abs() < have.abs() {
        return Some(Step::Reduce { side: closing_side(have), qty: delta.abs() });
    }

    let side = if delta > 0.0 { Side::Buy } else { Side::Sell };
    let anchor = held.map(|h| h.entry_px).unwrap_or(mark_px);
    Some(Step::Enter { side, qty: delta.abs(), stop_px: stop_for(side, anchor, rules.stop_loss_fraction) })
}

/// How far the holding may sit from the target without being worth an order.
/// Measured against whichever of the two is larger, so that a target of zero
/// still allows nothing — going flat means going flat.
fn drift_allowed(want: f64, have: f64, fraction: f64) -> f64 {
    if want == 0.0 {
        return 0.0;
    }
    want.abs().max(have.abs()) * fraction
}

fn closing_side(have: f64) -> Side {
    if have > 0.0 {
        Side::Sell
    } else {
        Side::Buy
    }
}

fn stop_for(side: Side, anchor_px: f64, fraction: f64) -> f64 {
    match side {
        Side::Buy => anchor_px * (1.0 - fraction),
        Side::Sell => anchor_px * (1.0 + fraction),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RULES: Rules =
        Rules { target_qty: 1.0, tolerance_fraction: 0.05, stop_loss_fraction: 0.2 };

    fn long(qty: f64, entry_px: f64) -> Option<Held> {
        Some(Held { side: Side::Buy, qty, entry_px })
    }

    fn short(qty: f64, entry_px: f64) -> Option<Held> {
        Some(Held { side: Side::Sell, qty, entry_px })
    }

    #[test]
    fn flat_and_wanting_a_long_enters_with_a_stop_below() {
        let step = plan(None, 100.0, RULES).expect("a wanted position is entered");
        assert_eq!(step, Step::Enter { side: Side::Buy, qty: 1.0, stop_px: 80.0 });
    }

    #[test]
    fn flat_and_wanting_a_short_enters_with_a_stop_above() {
        let rules = Rules { target_qty: -1.0, ..RULES };
        let step = plan(None, 100.0, rules).expect("a wanted short is entered");
        assert_eq!(step, Step::Enter { side: Side::Sell, qty: 1.0, stop_px: 120.0 });
    }

    #[test]
    fn holding_the_target_does_nothing() {
        assert_eq!(plan(long(1.0, 100.0), 100.0, RULES), None);
    }

    #[test]
    fn a_drift_inside_the_tolerance_is_not_worth_an_order() {
        // 4% short of the target, tolerance is 5%.
        assert_eq!(plan(long(0.96, 100.0), 100.0, RULES), None);
    }

    #[test]
    fn a_drift_past_the_tolerance_is_topped_up() {
        let step = plan(long(0.8, 100.0), 110.0, RULES).expect("a real gap is closed");
        assert!(matches!(step, Step::Enter { side: Side::Buy, .. }));
        let Step::Enter { qty, stop_px, .. } = step else { unreachable!() };
        assert!((qty - 0.2).abs() < 1e-12);
        // The stop is measured from what the position was opened at, not from
        // today's 110: the whole holding is behind one stop.
        assert!((stop_px - 80.0).abs() < 1e-12, "{stop_px}");
    }

    #[test]
    fn holding_too_much_is_reduced() {
        let step = plan(long(1.5, 100.0), 100.0, RULES).expect("an overweight is trimmed");
        let Step::Reduce { side, qty } = step else { panic!("expected a reduce, got {step:?}") };
        assert_eq!(side, Side::Sell);
        assert!((qty - 0.5).abs() < 1e-12);
    }

    #[test]
    fn a_target_of_zero_goes_all_the_way_flat() {
        let rules = Rules { target_qty: 0.0, ..RULES };
        let step = plan(long(0.3, 100.0), 100.0, rules).expect("flat means flat");
        assert_eq!(step, Step::Reduce { side: Side::Sell, qty: 0.3 });
    }

    #[test]
    fn a_target_of_zero_ignores_the_tolerance() {
        // A tolerance that let "nearly flat" count as flat would leave a
        // position nobody asked for standing forever.
        let rules = Rules { target_qty: 0.0, tolerance_fraction: 0.9, ..RULES };
        assert!(plan(long(0.01, 100.0), 100.0, rules).is_some());
    }

    #[test]
    fn turning_a_long_into_a_short_closes_the_long_first() {
        let rules = Rules { target_qty: -1.0, ..RULES };
        let step = plan(long(1.0, 100.0), 100.0, rules).expect("the old side comes off first");
        assert_eq!(
            step,
            Step::Reduce { side: Side::Sell, qty: 1.0 },
            "one order for both halves would open the new side with no stop"
        );
    }

    #[test]
    fn turning_a_short_into_a_long_closes_the_short_first() {
        let step = plan(short(1.0, 100.0), 100.0, RULES).expect("the old side comes off first");
        assert_eq!(step, Step::Reduce { side: Side::Buy, qty: 1.0 });
    }

    #[test]
    fn an_unusable_price_decides_nothing() {
        for px in [0.0, -1.0, f64::NAN] {
            assert_eq!(plan(None, px, RULES), None, "px {px} is not a price");
        }
    }
}
