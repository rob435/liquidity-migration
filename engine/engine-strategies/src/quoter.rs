//! Two-sided quoting: the in-the-loop kind of strategy.
//!
//! A market maker's whole decision happens between a price arriving and an
//! order leaving, which is what the engine is for. It needs a vocabulary
//! wider than "place": it has to pull its quotes when the book breaks or
//! inventory fills, and move them when the market walks away — so it uses
//! `Action::Cancel` and `Action::Amend`, and reads its own working orders
//! through `StrategyCtx::resting`, since the engine mints the order ids.
//!
//! [`plan::plan_quotes`] is the decision and it is pure.

pub mod plan;

pub use plan::{QuoteRules, QuoteStep, Resting, plan_quotes};
