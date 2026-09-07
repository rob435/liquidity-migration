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

pub mod faults;
pub mod harness;
pub mod invariants;
pub mod market;
pub mod rng;

pub use faults::FaultRates;
pub use harness::{run_seed, run_sweep, SimOptions, SimReport, SweepOptions, SweepReport};
