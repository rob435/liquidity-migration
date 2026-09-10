//! `engine sim`: the live loop under a seeded world with faults and deaths.
//!
//! One seed decides everything: the synthetic market ([`market`]), which
//! venue replies are lost, slowed or refused, which private updates are
//! dropped, duplicated or delayed, where the market feed hiccups
//! ([`faults`]), and when the process dies ([`harness`]). The engine under
//! test is the real one — `Engine::boot_as_exact`, the risk kernel, the strategy
//! plugs, the log — on the backtest's virtual clock and simulated venue, so
//! two runs of one seed write byte-identical logs and a failing seed
//! reproduces on any machine.
//!
//! After the tape ends the venue's books, the log and the engine are read
//! back and judged by [`invariants`].
//!
//! | Injected | Where | What the engine must do |
//! | --- | --- | --- |
//! | venue refusal; request lost before the venue; reply lost after it; slow reply; account read failure | [`faults::FaultyGateway`] | treat the ambiguous send as ambiguous; learn the order's fate before growing |
//! | private update dropped, duplicated or delayed; socket hiccup | [`faults::FaultyOrderFeed`] | dedupe by execution id; recover a gap from the venue's fill history |
//! | market feed hiccup; feed reset | [`faults::FaultyMarketFeed`] | re-arm; never open against a stale quote |
//! | signal row delayed past its window, delivered twice, or withheld so the source has a hole | [`faults::FaultySignalFeed`] | consume a late row without deciding; dedupe on the durable cursor; record the gap, refuse openings, accept the missing row and resume |
//! | one symbol falls 20 % and holds there | [`market::Shock`] | the native stop triggers on the mark, and its fill is owned |
//! | process death at a seeded instant, private socket lost with it | [`harness`] | boot from the log; catch up on the fills the dead process never saw |
//!
//! | Judged | Holds when |
//! | --- | --- |
//! | `positions_agree` | the log's signed exposure per symbol equals the venue's positions |
//! | `every_fill_journaled` | the venue's execution ids and the log's are the same set |
//! | `no_orphan_orders` | every order working at the venue is in the engine's in-flight ledger |
//! | `ledger_agrees_when_flat` | with no open position, the closed-trade ledger equals venue realized P&L net of closed fees |
//! | `numbers_finite` | no NaN or infinity in the venue's books or the engine's account view |
//! | `stopped_by_feed_closed` | the loop stopped because the tape ended, not because of an error |
//! | `engine_ran_clean` | no boot or run returned an error |
//! | `strategies_healthy` | no sleeve reports a health error, and no `intent_refused` record carries `strategy_callback_unavailable` |
//! | `signals_consumed_exactly_once` | every row published in time to matter is in the log once, settled once, and no recorded gap is still open |
//! | `checkpoint_identity_holds` | each sleeve accepts its own newest durable state, at the block's fingerprint, and no boot rewrote the initial checkpoint |
//! | `sleeve_attribution_agrees` | the sleeves' own inventories add up per symbol to the venue's position |
//! | `no_opening_before_readiness` | LONG sent nothing before it durably consumed one of its producer's rows |
//! | `working_entries_settled` | every worked LONG entry is terminal in the log or in flight in the engine |
//!
//! In quoter mode there is no producer, and the six signal and sleeve checks
//! report "not judged" and pass. Realm mode does not model the signal worker,
//! the spool files, the socket, or the venue's real latency: the producer is a
//! function, the spool is a queue in memory, and every clock is virtual.

pub mod faults;
pub mod harness;
pub mod invariants;
pub mod market;
pub mod rng;
pub mod signals;

pub use faults::FaultRates;
pub use harness::{run_seed, run_sweep, SimOptions, SimReport, SweepOptions, SweepReport};
pub use market::{Realm, Shock, SimStrategies};
pub use signals::Producer;
