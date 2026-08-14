//! Turning a target book into orders.
//!
//! The book says what to hold, in absolute notional per symbol. This works
//! out the difference from what is held now and says what to do about it.
//! Pure arithmetic: no clock of its own, no venue, no engine — everything it
//! needs is an argument, which is what makes the rules below testable one at
//! a time.
//!
//! Two absences mean different things, and confusing them is how a live book
//! gets flattened by a data outage:
//!   - no book at all (missing, unreadable, or a version we do not know) is
//!     *no decision*: hold what you hold, do nothing;
//!   - a book with no targets in it is a decision to *hold nothing*, and its
//!     exits are acted on like any other.
//! This module only ever sees a book that exists; the first case is the
//! caller's to handle, and it must handle it by not calling.

use engine_types::orders::{InstrumentRule, Side};
use engine_types::quantize::quantize_qty;

/// A symbol's absolute target, as research wrote it down.
#[derive(Clone, Debug, PartialEq)]
pub struct Target {
    pub symbol: String,
    /// Signed: positive long, negative short, exactly zero means hold none.
    pub notional_usdt: f64,
    /// How far below (long) or above (short) the entry the stop sits.
    pub stop_loss_fraction: f64,
}

/// What is held now, for one symbol.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct Held {
    pub qty: f64,
    pub side: Side,
    /// Mark or last price, used to value the holding and to size against.
    pub px: f64,
}

impl Held {
    /// Signed notional: what the position is worth, with its direction.
    pub fn notional(&self) -> f64 {
        let size = self.qty.abs() * self.px;
        match self.side {
            Side::Buy => size,
            Side::Sell => -size,
        }
    }
}

/// One thing to do about one symbol.
#[derive(Clone, Debug, PartialEq)]
pub enum Step {
    /// Open a position that is not there.
    Enter {
        symbol: String,
        side: Side,
        qty: f64,
        stop_px: f64,
    },
    /// Close what is held, whole.
    Exit { symbol: String, side: Side, qty: f64 },
    /// Move an existing position toward its target without closing it.
    Resize {
        symbol: String,
        side: Side,
        qty: f64,
        reduce_only: bool,
    },
}

impl Step {
    pub fn symbol(&self) -> &str {
        match self {
            Step::Enter { symbol, .. } | Step::Exit { symbol, .. } | Step::Resize { symbol, .. } => {
                symbol
            }
        }
    }
}

/// Why a symbol produced no step. Kept so an operator can see the difference
/// between "nothing to do" and "wanted to, could not".
#[derive(Clone, Debug, PartialEq)]
pub enum Skipped {
    /// Inside the dead band: the change is too small to be worth its cost.
    TooSmallToBother { symbol: String, delta_usdt: f64 },
    /// Below the floor an entry has to clear to be worth opening.
    BelowEntryFloor { symbol: String, notional_usdt: f64 },
    /// The size the venue would accept rounds to nothing.
    BelowVenueMinimum { symbol: String },
    /// The book's entry window has closed; exits are unaffected.
    EntryWindowClosed { symbol: String },
    /// No price for the symbol, so nothing can be sized.
    NoPrice { symbol: String },
    /// The engine has no instrument rule, so nothing can be quantized.
    NoInstrumentRule { symbol: String },
}

/// The knobs, all stated rather than assumed.
#[derive(Copy, Clone, Debug)]
pub struct PlanRules {
    /// An entry smaller than this is not worth opening. A coarse floor that
    /// sits above the venue's own minimum with headroom.
    pub entry_floor_usdt: f64,
    /// A resize must beat both of these to happen: an absolute floor, and a
    /// fraction of what is already standing. A resize is a round trip in
    /// fees, so small ones cost more than the accuracy they buy.
    pub resize_floor_usdt: f64,
    pub resize_floor_fraction: f64,
    /// How long before the book expires entries stop being opened. Exits
    /// keep working right up to and past expiry.
    pub entry_cutoff_ms: i64,
}

impl PlanRules {
    /// The fleet's numbers, which is what the carry book was measured with.
    pub const FLEET: PlanRules = PlanRules {
        entry_floor_usdt: 6.0,
        resize_floor_usdt: 1.0,
        resize_floor_fraction: 0.05,
        entry_cutoff_ms: 15 * 60 * 1000,
    };
}

/// What the planner decided, in the order the steps should be sent.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Plan {
    pub steps: Vec<Step>,
    pub skipped: Vec<Skipped>,
}

/// Everything the planner needs to know about one symbol, looked up by the
/// caller so this stays pure.
pub trait SymbolFacts {
    fn held(&self, symbol: &str) -> Option<Held>;
    fn price(&self, symbol: &str) -> Option<f64>;
    fn rule(&self, symbol: &str) -> Option<InstrumentRule>;
}

/// Work out what to do to move the book from what is held to what is wanted.
///
/// Exits come first in the returned order, always. A book that rotates out of
/// one name and into another must free the capital before it spends it, or
/// the entry is refused for room the exit was about to make.
pub fn plan(
    targets: &[Target],
    held_symbols: &[String],
    facts: &dyn SymbolFacts,
    now_ms: i64,
    valid_until_ms: i64,
    rules: PlanRules,
) -> Plan {
    let mut exits: Vec<Step> = Vec::new();
    let mut opens: Vec<Step> = Vec::new();
    let mut skipped: Vec<Skipped> = Vec::new();

    let entries_allowed = now_ms < valid_until_ms.saturating_sub(rules.entry_cutoff_ms);

    // Anything held that the book does not name at all is an exit. A book is
    // absolute: silence about a symbol is an instruction to hold none of it.
    for symbol in held_symbols {
        if targets.iter().any(|t| &t.symbol == symbol) {
            continue;
        }
        if let Some(position) = facts.held(symbol) {
            exits.push(Step::Exit {
                symbol: symbol.clone(),
                side: position.side.flipped(),
                qty: position.qty.abs(),
            });
        }
    }

    for target in targets {
        let symbol = target.symbol.as_str();
        let position = facts.held(symbol);

        // Zero, or a target that rounds to nothing, is an explicit exit.
        if target.notional_usdt == 0.0 {
            if let Some(position) = position {
                exits.push(Step::Exit {
                    symbol: symbol.to_string(),
                    side: position.side.flipped(),
                    qty: position.qty.abs(),
                });
            }
            continue;
        }

        let Some(px) = facts.price(symbol).filter(|p| *p > 0.0) else {
            skipped.push(Skipped::NoPrice { symbol: symbol.to_string() });
            continue;
        };
        let Some(rule) = facts.rule(symbol) else {
            skipped.push(Skipped::NoInstrumentRule { symbol: symbol.to_string() });
            continue;
        };

        let want_side = if target.notional_usdt > 0.0 { Side::Buy } else { Side::Sell };
        let standing = position.map(|p| p.notional()).unwrap_or(0.0);
        let delta_usdt = target.notional_usdt - standing;

        match position {
            None => {
                if !entries_allowed {
                    skipped.push(Skipped::EntryWindowClosed { symbol: symbol.to_string() });
                    continue;
                }
                let size = target.notional_usdt.abs();
                if size < rules.entry_floor_usdt {
                    skipped.push(Skipped::BelowEntryFloor {
                        symbol: symbol.to_string(),
                        notional_usdt: size,
                    });
                    continue;
                }
                let Some(qty) = quantize_qty(size / px, &rule) else {
                    skipped.push(Skipped::BelowVenueMinimum { symbol: symbol.to_string() });
                    continue;
                };
                opens.push(Step::Enter {
                    symbol: symbol.to_string(),
                    side: want_side,
                    qty,
                    stop_px: stop_price(px, want_side, target.stop_loss_fraction),
                });
            }
            Some(position) => {
                // A target that flips direction is two acts, not one: close
                // what is held, and let the next pass open the other way once
                // the position is actually flat. Reversing in one order would
                // ride through flat with a stop that belongs to neither side.
                if position.side != want_side {
                    exits.push(Step::Exit {
                        symbol: symbol.to_string(),
                        side: position.side.flipped(),
                        qty: position.qty.abs(),
                    });
                    continue;
                }
                let threshold =
                    rules.resize_floor_usdt.max(rules.resize_floor_fraction * standing.abs());
                if delta_usdt.abs() <= threshold {
                    skipped.push(Skipped::TooSmallToBother {
                        symbol: symbol.to_string(),
                        delta_usdt,
                    });
                    continue;
                }
                let growing = delta_usdt.abs() > 0.0 && (delta_usdt > 0.0) == (standing > 0.0);
                if growing && !entries_allowed {
                    skipped.push(Skipped::EntryWindowClosed { symbol: symbol.to_string() });
                    continue;
                }
                let Some(qty) = quantize_qty(delta_usdt.abs() / px, &rule) else {
                    skipped.push(Skipped::BelowVenueMinimum { symbol: symbol.to_string() });
                    continue;
                };
                let side = if growing { want_side } else { want_side.flipped() };
                let step = Step::Resize {
                    symbol: symbol.to_string(),
                    side,
                    qty,
                    reduce_only: !growing,
                };
                if growing {
                    opens.push(step);
                } else {
                    exits.push(step);
                }
            }
        }
    }

    exits.sort_by(|a, b| a.symbol().cmp(b.symbol()));
    opens.sort_by(|a, b| a.symbol().cmp(b.symbol()));
    let mut steps = exits;
    steps.extend(opens);
    Plan { steps, skipped }
}

/// Where the stop sits for a position opened at `px`.
fn stop_price(px: f64, side: Side, fraction: f64) -> f64 {
    match side {
        Side::Buy => px * (1.0 - fraction),
        Side::Sell => px * (1.0 + fraction),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    #[derive(Default)]
    struct Facts {
        held: BTreeMap<String, Held>,
        px: BTreeMap<String, f64>,
        rules: BTreeMap<String, InstrumentRule>,
    }

    const RULE: InstrumentRule =
        InstrumentRule { tick_size: 0.01, qty_step: 0.1, min_qty: 0.1, min_notional: 5.0 };

    impl Facts {
        fn with(symbol: &str, px: f64) -> Self {
            let mut me = Facts::default();
            me.px.insert(symbol.into(), px);
            me.rules.insert(symbol.into(), RULE);
            me
        }
        fn holding(mut self, symbol: &str, qty: f64, side: Side, px: f64) -> Self {
            self.held.insert(symbol.into(), Held { qty, side, px });
            self.px.insert(symbol.into(), px);
            self.rules.insert(symbol.into(), RULE);
            self
        }
    }

    impl SymbolFacts for Facts {
        fn held(&self, symbol: &str) -> Option<Held> {
            self.held.get(symbol).copied()
        }
        fn price(&self, symbol: &str) -> Option<f64> {
            self.px.get(symbol).copied()
        }
        fn rule(&self, symbol: &str) -> Option<InstrumentRule> {
            self.rules.get(symbol).copied()
        }
    }

    fn target(symbol: &str, notional: f64) -> Target {
        Target { symbol: symbol.into(), notional_usdt: notional, stop_loss_fraction: 0.35 }
    }

    const NOW: i64 = 1_000_000;
    const VALID: i64 = NOW + 3_600_000;

    fn plan_now(targets: &[Target], held: &[String], facts: &dyn SymbolFacts) -> Plan {
        plan(targets, held, facts, NOW, VALID, PlanRules::FLEET)
    }

    #[test]
    fn a_wanted_symbol_with_no_position_is_entered_with_a_stop() {
        let facts = Facts::with("KAITOUSDT", 10.0);
        let plan = plan_now(&[target("KAITOUSDT", 100.0)], &[], &facts);
        assert_eq!(
            plan.steps,
            vec![Step::Enter {
                symbol: "KAITOUSDT".into(),
                side: Side::Buy,
                qty: 10.0,
                stop_px: 6.5,
            }]
        );
    }

    #[test]
    fn a_held_symbol_the_book_stops_naming_is_exited() {
        let facts = Facts::default().holding("COTIUSDT", 50.0, Side::Buy, 2.0);
        let plan = plan_now(&[], &["COTIUSDT".into()], &facts);
        assert_eq!(
            plan.steps,
            vec![Step::Exit { symbol: "COTIUSDT".into(), side: Side::Sell, qty: 50.0 }]
        );
    }

    #[test]
    fn an_empty_book_exits_everything_held() {
        // Deciding cash is a decision, and it gets acted on. The caller is
        // what must tell "no book" from "an empty book".
        let facts = Facts::default()
            .holding("A", 10.0, Side::Buy, 5.0)
            .holding("B", 20.0, Side::Sell, 5.0);
        let plan = plan_now(&[], &["A".into(), "B".into()], &facts);
        assert_eq!(plan.steps.len(), 2);
        assert!(plan.steps.iter().all(|s| matches!(s, Step::Exit { .. })));
    }

    #[test]
    fn a_zero_target_is_an_exit_not_an_absence() {
        let facts = Facts::default().holding("A", 10.0, Side::Buy, 5.0);
        let plan = plan_now(&[target("A", 0.0)], &["A".into()], &facts);
        assert_eq!(plan.steps, vec![Step::Exit { symbol: "A".into(), side: Side::Sell, qty: 10.0 }]);
    }

    #[test]
    fn exits_are_sent_before_entries() {
        // Rotating out of one name into another has to free the capital
        // before it spends it, or the entry is refused for room the exit was
        // about to make.
        let facts = Facts::default().holding("OLD", 10.0, Side::Buy, 10.0);
        let mut facts = facts;
        facts.px.insert("NEW".into(), 10.0);
        facts.rules.insert("NEW".into(), RULE);
        let plan = plan_now(&[target("NEW", 100.0)], &["OLD".into()], &facts);
        assert_eq!(plan.steps.len(), 2);
        assert!(matches!(plan.steps[0], Step::Exit { .. }), "exit must come first");
        assert!(matches!(plan.steps[1], Step::Enter { .. }));
    }

    #[test]
    fn a_change_inside_the_dead_band_is_left_alone() {
        // 100 standing, 103 wanted: 3 is under 5% of 100, so it is not worth
        // the round trip.
        let facts = Facts::default().holding("A", 10.0, Side::Buy, 10.0);
        let plan = plan_now(&[target("A", 103.0)], &["A".into()], &facts);
        assert!(plan.steps.is_empty());
        assert!(matches!(plan.skipped[0], Skipped::TooSmallToBother { .. }));
    }

    #[test]
    fn a_change_past_the_dead_band_resizes() {
        let facts = Facts::default().holding("A", 10.0, Side::Buy, 10.0);
        let plan = plan_now(&[target("A", 150.0)], &["A".into()], &facts);
        assert_eq!(
            plan.steps,
            vec![Step::Resize {
                symbol: "A".into(),
                side: Side::Buy,
                qty: 5.0,
                reduce_only: false
            }]
        );
    }

    #[test]
    fn a_shrink_is_reduce_only_and_goes_with_the_exits() {
        let facts = Facts::default().holding("A", 20.0, Side::Buy, 10.0);
        let plan = plan_now(&[target("A", 100.0)], &["A".into()], &facts);
        assert_eq!(
            plan.steps,
            vec![Step::Resize {
                symbol: "A".into(),
                side: Side::Sell,
                qty: 10.0,
                reduce_only: true
            }]
        );
    }

    #[test]
    fn the_absolute_floor_stops_a_tiny_resize_on_a_tiny_position() {
        // 5% of 10 is 0.5, but a 0.9 USDT resize is still not worth doing.
        let facts = Facts::default().holding("A", 1.0, Side::Buy, 10.0);
        let plan = plan_now(&[target("A", 10.9)], &["A".into()], &facts);
        assert!(plan.steps.is_empty());
    }

    #[test]
    fn an_entry_under_the_floor_is_not_opened() {
        let facts = Facts::with("A", 10.0);
        let plan = plan_now(&[target("A", 5.0)], &[], &facts);
        assert!(plan.steps.is_empty());
        assert!(matches!(plan.skipped[0], Skipped::BelowEntryFloor { .. }));
    }

    #[test]
    fn an_entry_that_rounds_to_nothing_is_refused() {
        let mut facts = Facts::with("A", 1000.0);
        facts.rules.insert(
            "A".into(),
            InstrumentRule { tick_size: 0.01, qty_step: 1.0, min_qty: 1.0, min_notional: 5.0 },
        );
        let plan = plan_now(&[target("A", 100.0)], &[], &facts);
        assert!(plan.steps.is_empty());
        assert!(matches!(plan.skipped[0], Skipped::BelowVenueMinimum { .. }));
    }

    #[test]
    fn past_the_entry_cutoff_entries_stop_but_exits_do_not() {
        let facts = Facts::default().holding("OLD", 10.0, Side::Buy, 10.0);
        let mut facts = facts;
        facts.px.insert("NEW".into(), 10.0);
        facts.rules.insert("NEW".into(), RULE);
        let late = VALID - PlanRules::FLEET.entry_cutoff_ms + 1;
        let plan = plan(
            &[target("NEW", 100.0)],
            &["OLD".into()],
            &facts,
            late,
            VALID,
            PlanRules::FLEET,
        );
        assert_eq!(plan.steps.len(), 1);
        assert!(matches!(plan.steps[0], Step::Exit { .. }), "the exit still goes");
        assert!(matches!(plan.skipped[0], Skipped::EntryWindowClosed { .. }));
    }

    #[test]
    fn a_flip_closes_first_and_does_not_reverse_in_one_order() {
        // Reversing through flat in a single order would leave the position
        // carrying a stop that belongs to the side it just left.
        let facts = Facts::default().holding("A", 10.0, Side::Buy, 10.0);
        let plan = plan_now(&[target("A", -100.0)], &["A".into()], &facts);
        assert_eq!(plan.steps, vec![Step::Exit { symbol: "A".into(), side: Side::Sell, qty: 10.0 }]);
    }

    #[test]
    fn a_short_target_enters_short_with_the_stop_above() {
        let facts = Facts::with("A", 10.0);
        let plan = plan_now(&[target("A", -100.0)], &[], &facts);
        assert_eq!(
            plan.steps,
            vec![Step::Enter {
                symbol: "A".into(),
                side: Side::Sell,
                qty: 10.0,
                stop_px: 13.5,
            }]
        );
    }

    #[test]
    fn a_symbol_with_no_price_is_skipped_not_guessed() {
        let mut facts = Facts::default();
        facts.rules.insert("A".into(), RULE);
        let plan = plan_now(&[target("A", 100.0)], &[], &facts);
        assert!(plan.steps.is_empty());
        assert!(matches!(plan.skipped[0], Skipped::NoPrice { .. }));
    }

    #[test]
    fn a_symbol_with_no_instrument_rule_is_skipped() {
        let mut facts = Facts::default();
        facts.px.insert("A".into(), 10.0);
        let plan = plan_now(&[target("A", 100.0)], &[], &facts);
        assert!(matches!(plan.skipped[0], Skipped::NoInstrumentRule { .. }));
    }
}
