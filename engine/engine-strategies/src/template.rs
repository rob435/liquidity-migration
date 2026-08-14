//! `template`: copy this directory to start a new strategy.
//!
//! It is a complete, working, tested plug that does something trivial — hold
//! a fixed position in one symbol — so that everything around the decision is
//! real and only the decision needs replacing. It is compiled and tested with
//! the rest of the crate, so it cannot rot, and it is deliberately **not**
//! registered in [`crate::KNOWN_STRATEGIES`], so no config can run it by
//! accident. Registering yours is one of the steps below.
//!
//! # The shape
//!
//! Two files, and the split between them is the rule, not a preference:
//!
//! - `plan.rs` — **the decision**. A pure function over plain numbers. No
//!   engine types beyond simple ones, no context, no clock, no I/O. This is
//!   where the strategy actually lives, and it is the part worth testing hard,
//!   because every case is one function call.
//! - `plug.rs` — **the wiring**. Reads config, resolves symbols, pulls the
//!   market picture out of the context, calls `plan`, turns its answers into
//!   actions. It should be boring. If it starts making decisions, move them.
//!
//! # What a strategy may touch
//!
//! Only [`engine_types::StrategyCtx`]. That is the whole world: quotes,
//! tickers, the account reading, instrument rules, both clocks, your own
//! resting orders, and the actions you can emit. There is deliberately no way
//! to open a file, read a socket, spawn a thread, or reach another strategy.
//!
//! **A strategy owns its symbols.** Two plugs trading the same symbol see one
//! another's fills through [`StrategyCtx::position`], which reports the
//! *account's* holding — the venue keeps no note of who asked for it. Each
//! would read the other's exposure as its own and size against it. The engine
//! refuses to boot such a config for that reason; see `assembly.rs`. If you
//! genuinely want two behaviours on one symbol, that is one strategy with two
//! branches, not two strategies.
//!
//! **A strategy owns its config.** Everything it needs comes from its own
//! `[[strategy]]` block. Never read another plug's parameters, another plug's
//! file, or a global. A strategy that is deleted must leave nothing behind
//! that anything else depends on.
//!
//! # Rules the engine will hold you to
//!
//! - **An opening order carries a stop.** The risk kernel refuses one that
//!   does not. Reducing orders do not need one.
//! - **`on_event` runs on the engine loop.** It must return quickly and must
//!   never block. Allocate in the constructor, keep scratch buffers on `self`.
//! - **The engine mints order ids.** You cannot choose one. To find an order
//!   you placed, read [`StrategyCtx::resting`].
//! - **The account reading lags.** It is the venue's picture on the engine's
//!   refresh schedule, not a running total of your fills — a fill can be gone
//!   from `resting` seconds before it appears in `position`. If acting twice
//!   in that window would be wrong, remember what you sent; `target_book`'s
//!   follower shows one way.
//! - **Quantize before you believe a size.** [`StrategyCtx::instrument`] gives
//!   tick, step and minimums, and `None` means nothing can be sent for that
//!   symbol at all. The engine quantizes again before the wire, so this is
//!   about not emitting an intent that could never become an order.
//! - **Config errors are refusals, not defaults.** Read every parameter, then
//!   call `reject_unknown` so a typo is an error rather than a silent change
//!   of behaviour.
//!
//! # Adding yours
//!
//! 1. `cp -r template mine`, then rename the module in `lib.rs`.
//! 2. Change `NAME` in `plug.rs`. It is the string a config names.
//! 3. Replace `plan.rs` with your decision, and its tests with yours.
//! 4. Add your name to [`crate::KNOWN_STRATEGIES`] and to the match in
//!    [`crate::build_strategy`].
//! 5. Write the config block into `docs/engine.md`'s strategy table.
//!
//! There is no registration file to edit, no trait to derive, and no macro.
//! The whole seam is those two lists in `lib.rs`.
//!
//! # What good tests look like here
//!
//! Test `plan` directly for the arithmetic — it is a pure function, so a case
//! is one line. Test the plug through [`crate::mock_ctx::Harness`] for the
//! wiring: which verb it reached for, what it emitted, what it did not.
//!
//! Every test must fail if the thing it tests is removed. Prove it: break the
//! code, watch the test go red, put it back. A test that passes either way is
//! worse than no test, because it reads like cover.

pub mod plan;
pub mod plug;
#[cfg(test)]
mod plug_tests;

pub use plan::{Held, Rules, Step, plan};
pub use plug::Template;
