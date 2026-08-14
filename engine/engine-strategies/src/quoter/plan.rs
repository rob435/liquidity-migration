//! Deciding where a two-sided quote should sit, and what to do about the one
//! already resting.
//!
//! Pure arithmetic, like the target-book planner: the market, the orders this
//! strategy already has out, and the position are arguments, so every rule
//! here can be tested on its own. This is the other kind of strategy the
//! engine carries — one whose whole decision is in the loop, reacting to a
//! price in microseconds rather than following a book decided hours ago.

use engine_types::orders::Side;

/// Where a quote should be and how far it may drift before it is worth
/// moving.
#[derive(Copy, Clone, Debug)]
pub struct QuoteRules {
    /// Half the quoted spread, as a fraction of mid. A quote sits this far
    /// from mid on each side.
    pub half_spread: f64,
    /// How far a resting quote may sit from where it should be before it is
    /// moved. Every move costs a request and gives up queue position, so a
    /// quote that is nearly right is left alone.
    pub requote_tolerance: f64,
    /// Size per side, in base units.
    pub qty: f64,
    /// Inventory ceiling in base units. A side that would push the position
    /// past this stops being quoted.
    pub max_position: f64,
    /// How far the quote's centre moves away from the inventory when the
    /// position is at its ceiling, as a fraction of mid. Zero quotes
    /// symmetrically around the market whatever is held.
    ///
    /// This is the difference between a maker and a thing that accumulates.
    /// Without it the only answer to inventory is the ceiling, which is a
    /// wall: quote both sides at full size until the position hits the limit,
    /// then stop one side outright and wait for the market to come back. With
    /// it, every fill on one side moves both quotes away from that side, so
    /// the book pays a little more to be taken out of its position and a
    /// little less to be put further into it. It works out of inventory
    /// continuously instead of parking at the wall.
    pub skew: f64,
    /// How far below (bid) or above (ask) the fill a stop sits. The risk
    /// kernel refuses a position-opening order without one, and a quote is
    /// position-opening.
    pub stop_loss_fraction: f64,
}

/// One of this strategy's quotes that is currently working.
#[derive(Clone, Debug, PartialEq)]
pub struct Resting {
    pub client_order_id: String,
    pub side: Side,
    pub px: f64,
}

/// What to do about one side of the market.
#[derive(Clone, Debug, PartialEq)]
pub enum QuoteStep {
    /// Nothing is resting on this side and one should be.
    Place { side: Side, px: f64, qty: f64, stop_px: f64 },
    /// The resting quote is too far from where it belongs.
    Move { client_order_id: String, px: f64 },
    /// The quote should not be in the market at all: inventory is full on
    /// this side, or there is no usable price.
    Pull { client_order_id: String },
}

/// Where the quotes are centred, given what is already held.
///
/// Long pushes the centre down, short pushes it up — so the side that would
/// add to the position quotes further away and the side that would reduce it
/// quotes closer. `position` is signed, positive long, and the lean is capped
/// at the ceiling so a position somehow past it cannot send the centre through
/// the floor.
pub fn centre(mid: f64, position: f64, rules: QuoteRules) -> f64 {
    if rules.skew <= 0.0 || rules.max_position <= 0.0 || !position.is_finite() {
        return mid;
    }
    let lean = (position / rules.max_position).clamp(-1.0, 1.0);
    mid * (1.0 - lean * rules.skew)
}

/// Work out what each side needs. `position` is signed: positive is long.
///
/// A crossed or empty book yields no quotes and pulls what is resting — a
/// price that is not a price is not something to quote around, and staying in
/// the market on a broken feed is how a maker gets picked off.
pub fn plan_quotes(
    bid_px: f64,
    ask_px: f64,
    position: f64,
    resting: &[Resting],
    rules: QuoteRules,
) -> Vec<QuoteStep> {
    let mut steps = Vec::new();
    let usable = bid_px > 0.0 && ask_px > 0.0 && ask_px >= bid_px;
    let mid = (bid_px + ask_px) / 2.0;
    let centre = centre(mid, position, rules);

    for side in [Side::Buy, Side::Sell] {
        let working = resting.iter().find(|r| r.side == side);
        // Quoting a side that would take the position past its ceiling is how
        // a maker ends up long a falling market: the side that keeps filling
        // is exactly the side that should stop.
        let would_exceed = match side {
            Side::Buy => position + rules.qty > rules.max_position,
            Side::Sell => position - rules.qty < -rules.max_position,
        };
        if !usable || would_exceed {
            if let Some(order) = working {
                steps.push(QuoteStep::Pull {
                    client_order_id: order.client_order_id.clone(),
                });
            }
            continue;
        }

        let want_px = match side {
            Side::Buy => centre * (1.0 - rules.half_spread),
            Side::Sell => centre * (1.0 + rules.half_spread),
        };
        match working {
            None => steps.push(QuoteStep::Place {
                side,
                px: want_px,
                qty: rules.qty,
                stop_px: stop_for(want_px, side, rules.stop_loss_fraction),
            }),
            Some(order) => {
                let drift = (order.px - want_px).abs() / mid;
                if drift > rules.requote_tolerance {
                    steps.push(QuoteStep::Move {
                        client_order_id: order.client_order_id.clone(),
                        px: want_px,
                    });
                }
            }
        }
    }
    steps
}

fn stop_for(px: f64, side: Side, fraction: f64) -> f64 {
    match side {
        Side::Buy => px * (1.0 - fraction),
        Side::Sell => px * (1.0 + fraction),
    }
}

#[cfg(test)]
mod tests {
    pub(super) use super::*;

    /// No skew, so the existing rules read exactly as they always did: the
    /// quotes sit symmetrically around mid whatever is held.
    pub(super) const RULES: QuoteRules = QuoteRules {
        half_spread: 0.001,
        requote_tolerance: 0.0005,
        qty: 1.0,
        max_position: 3.0,
        stop_loss_fraction: 0.35,
        skew: 0.0,
    };

    /// The same book, leaning against inventory: at the ceiling the centre
    /// moves a full half-spread, so one side of the quote lands on mid.
    pub(super) const LEANING: QuoteRules = QuoteRules { skew: 0.001, ..RULES };

    fn resting(id: &str, side: Side, px: f64) -> Resting {
        Resting { client_order_id: id.into(), side, px }
    }

    #[test]
    fn an_empty_book_side_quotes_both_sides_around_mid() {
        let steps = plan_quotes(99.0, 101.0, 0.0, &[], RULES);
        assert_eq!(
            steps,
            vec![
                QuoteStep::Place { side: Side::Buy, px: 99.9, qty: 1.0, stop_px: 99.9 * 0.65 },
                QuoteStep::Place { side: Side::Sell, px: 100.1, qty: 1.0, stop_px: 100.1 * 1.35 },
            ]
        );
    }

    #[test]
    fn a_quote_that_is_nearly_right_is_left_where_it_is() {
        // Moving costs a request and the queue position it already earned.
        let steps = plan_quotes(
            99.0,
            101.0,
            0.0,
            &[resting("b", Side::Buy, 99.91), resting("a", Side::Sell, 100.09)],
            RULES,
        );
        assert!(steps.is_empty());
    }

    #[test]
    fn a_quote_that_has_drifted_too_far_is_moved() {
        let steps = plan_quotes(99.0, 101.0, 0.0, &[resting("b", Side::Buy, 99.0)], RULES);
        assert_eq!(steps[0], QuoteStep::Move { client_order_id: "b".into(), px: 99.9 });
    }

    #[test]
    fn the_side_that_would_break_the_inventory_ceiling_stops_being_quoted() {
        // Long 3 with a ceiling of 3: buying more is what turns a maker into
        // a position.
        let steps = plan_quotes(99.0, 101.0, 3.0, &[], RULES);
        assert_eq!(steps.len(), 1);
        assert!(matches!(steps[0], QuoteStep::Place { side: Side::Sell, .. }));
    }

    #[test]
    fn a_resting_quote_on_a_full_side_is_pulled() {
        let steps = plan_quotes(99.0, 101.0, 3.0, &[resting("b", Side::Buy, 99.9)], RULES);
        assert!(steps.contains(&QuoteStep::Pull { client_order_id: "b".into() }));
    }

    #[test]
    fn the_short_side_has_its_own_ceiling() {
        let steps = plan_quotes(99.0, 101.0, -3.0, &[], RULES);
        assert_eq!(steps.len(), 1);
        assert!(matches!(steps[0], QuoteStep::Place { side: Side::Buy, .. }));
    }

    #[test]
    fn an_empty_book_pulls_everything_and_quotes_nothing() {
        // A zero is not a price. Quoting around one, or leaving a quote out
        // while the feed is broken, is how a maker gets picked off.
        let steps = plan_quotes(
            0.0,
            0.0,
            0.0,
            &[resting("b", Side::Buy, 99.9), resting("a", Side::Sell, 100.1)],
            RULES,
        );
        assert_eq!(
            steps,
            vec![
                QuoteStep::Pull { client_order_id: "b".into() },
                QuoteStep::Pull { client_order_id: "a".into() },
            ]
        );
    }

    #[test]
    fn a_crossed_book_is_not_quoted_around() {
        let steps = plan_quotes(101.0, 99.0, 0.0, &[], RULES);
        assert!(steps.is_empty());
    }

    #[test]
    fn every_quote_carries_a_stop_because_the_kernel_demands_one() {
        let steps = plan_quotes(99.0, 101.0, 0.0, &[], RULES);
        for step in &steps {
            if let QuoteStep::Place { side, px, stop_px, .. } = step {
                match side {
                    Side::Buy => assert!(stop_px < px, "a long stop sits below"),
                    Side::Sell => assert!(stop_px > px, "a short stop sits above"),
                }
            }
        }
    }
}

#[cfg(test)]
mod skew_tests {
    use super::tests::{LEANING, RULES};
    use super::*;

    fn px_of(steps: &[QuoteStep], want: Side) -> f64 {
        steps
            .iter()
            .find_map(|s| match s {
                QuoteStep::Place { side, px, .. } if *side == want => Some(*px),
                _ => None,
            })
            .unwrap_or_else(|| panic!("no {want:?} quote in {steps:?}"))
    }

    #[test]
    fn a_flat_book_quotes_evenly_around_the_market() {
        assert_eq!(centre(100.0, 0.0, LEANING), 100.0);
    }

    #[test]
    fn being_long_moves_both_quotes_down() {
        // The whole idea: the side that would sell us out of the position gets
        // cheaper to hit, and the side that would add to it gets dearer. Both
        // move, which is what makes it a lean rather than a one-sided pull.
        let flat = plan_quotes(99.0, 101.0, 0.0, &[], LEANING);
        let long = plan_quotes(99.0, 101.0, 1.5, &[], LEANING);
        assert!(px_of(&long, Side::Buy) < px_of(&flat, Side::Buy), "bid backs off");
        assert!(px_of(&long, Side::Sell) < px_of(&flat, Side::Sell), "ask comes in");
    }

    #[test]
    fn being_short_moves_both_quotes_up() {
        let flat = plan_quotes(99.0, 101.0, 0.0, &[], LEANING);
        let short = plan_quotes(99.0, 101.0, -1.5, &[], LEANING);
        assert!(px_of(&short, Side::Buy) > px_of(&flat, Side::Buy));
        assert!(px_of(&short, Side::Sell) > px_of(&flat, Side::Sell));
    }

    #[test]
    fn the_lean_is_proportional_and_stops_at_the_ceiling() {
        // Half-full leans half as far as full, and a position somehow past the
        // ceiling cannot lean further than full — otherwise a stale reading
        // could send the centre through the floor.
        let mid = 100.0;
        let half = mid - centre(mid, 1.5, LEANING);
        let full = mid - centre(mid, 3.0, LEANING);
        let over = mid - centre(mid, 30.0, LEANING);
        assert!((full - 2.0 * half).abs() < 1e-12, "{full} vs {half}");
        assert_eq!(over, full, "capped at the ceiling");
    }

    #[test]
    fn no_skew_is_the_old_behaviour_exactly() {
        // The dial off must be the strategy that existed before it, or every
        // number measured against the old one is invalidated by a default.
        for position in [-3.0, -1.0, 0.0, 1.0, 3.0] {
            assert_eq!(centre(100.0, position, RULES), 100.0);
        }
    }

    #[test]
    fn a_ceiling_still_stops_a_side_outright() {
        // The lean is continuous pressure, not a limit. At the ceiling the
        // side that would add to the position stops being quoted at all.
        let steps = plan_quotes(99.0, 101.0, 3.0, &[], LEANING);
        assert_eq!(steps.len(), 1);
        assert!(matches!(steps[0], QuoteStep::Place { side: Side::Sell, .. }));
    }

    #[test]
    fn a_nonsense_position_leaves_the_centre_alone() {
        assert_eq!(centre(100.0, f64::NAN, LEANING), 100.0);
        assert_eq!(centre(100.0, 1.0, QuoteRules { max_position: 0.0, ..LEANING }), 100.0);
    }
}
