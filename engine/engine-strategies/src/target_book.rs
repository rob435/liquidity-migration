//! Following a target book written by the research system.
//!
//! Some strategies decide in the loop; this one does not. Carry reads ninety
//! days of settled funding and hourly bars and holds a state machine across
//! all of it — a batch computation that has no business running inside a
//! trading process. So research decides on its own clock and writes down what
//! it wants held, and this plug does the trading: work out the difference,
//! exit before entering, size, quantize, attach the stop.
//!
//! [`plan`] is the whole decision and it is pure, so every rule in it can be
//! tested on its own.

pub mod plan;

pub use plan::{Held, Plan, PlanRules, Skipped, Step, SymbolFacts, Target};
