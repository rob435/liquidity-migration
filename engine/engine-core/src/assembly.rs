//! Where the real parts get plugged in.
//!
//! The loop is written against the traits in `engine-types`, so it compiles
//! and is tested without any of the crates that will supply them. The crates
//! that are still empty are named here, one constructor each. Each returns a
//! plain "not wired yet" error when it runs, so `engine run` starts, says
//! exactly which part is missing, and stops — instead of pretending.
//!
//! Swapping one in is a two-line change: build the real thing and return it.
//! Nothing above this file moves.

use std::path::Path;

use engine_types::{
    AccountView, FeedError, InstrumentRule, Intent, MarketEvent, MarketFeed, OrderAck, OrderFeed,
    OrderRequest, OrderUpdate, RiskKernel, RiskVerdict, Strategy, Subscription, Symbol, SymbolId,
    VenueError, VenueGateway, WalError,
};

use crate::config::StrategyConfig;
use crate::filewal::FileWal;

#[derive(Debug)]
pub struct NotWired {
    pub part: &'static str,
    pub crate_name: &'static str,
}

impl std::fmt::Display for NotWired {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "the {} is not wired yet: {} is still empty",
            self.part, self.crate_name
        )
    }
}

impl std::error::Error for NotWired {}

/// The log. Today this is the small file log inside engine-core; when
/// `engine-wal` lands, build that here instead and delete `filewal.rs`.
pub fn wal(path: &Path) -> Result<FileWal, WalError> {
    FileWal::open(path)
}

/// Reading the log back. Same swap as `wal` — the reader must match whoever
/// wrote the file.
pub fn replay(path: &Path) -> Result<crate::filewal::Scan, WalError> {
    crate::filewal::read_frames(path)
}

/// Bybit's public market stream. Needs `engine-marketdata`.
pub fn market_feed(_wanted: &[Subscription]) -> Result<PendingMarketFeed, NotWired> {
    Err(NotWired {
        part: "market feed",
        crate_name: "engine-marketdata",
    })
}

/// The venue's private order stream. Needs `engine-venue`.
pub fn order_feed() -> Result<PendingOrderFeed, NotWired> {
    Err(NotWired {
        part: "order feed",
        crate_name: "engine-venue",
    })
}

/// The demo venue gateway. Needs `engine-venue`.
pub fn venue() -> Result<PendingVenue, NotWired> {
    Err(NotWired {
        part: "venue gateway",
        crate_name: "engine-venue",
    })
}

/// The four capital controls. Needs `engine-risk`. Note what the absence
/// means: with no kernel there is nothing to gate a send, so the engine will
/// not run without it, in shadow mode or otherwise.
pub fn risk(_settings: &toml::Table) -> Result<PendingRisk, NotWired> {
    Err(NotWired {
        part: "risk kernel",
        crate_name: "engine-risk",
    })
}

/// Name plus config block to a live strategy. Needs `engine-strategies`.
pub fn strategies(_configured: &[StrategyConfig]) -> Result<Vec<Box<dyn Strategy>>, NotWired> {
    Err(NotWired {
        part: "strategy registry",
        crate_name: "engine-strategies",
    })
}

// The types below exist so the loop type-checks against real signatures
// before the crates arrive. They are never built at runtime.

pub struct PendingMarketFeed;

impl MarketFeed for PendingMarketFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        Err(FeedError::Transport("market feed not wired".into()))
    }
}

pub struct PendingOrderFeed;

impl OrderFeed for PendingOrderFeed {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        Err(FeedError::Transport("order feed not wired".into()))
    }
}

pub struct PendingVenue;

impl VenueGateway for PendingVenue {
    async fn send_order(&mut self, _req: &OrderRequest) -> Result<OrderAck, VenueError> {
        Err(VenueError::Transport("venue not wired".into()))
    }

    async fn cancel_order(&mut self, _symbol: SymbolId, _id: &str) -> Result<(), VenueError> {
        Err(VenueError::Transport("venue not wired".into()))
    }

    async fn set_stop(&mut self, _symbol: SymbolId, _trigger_px: f64) -> Result<(), VenueError> {
        Err(VenueError::Transport("venue not wired".into()))
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        Err(VenueError::Transport("venue not wired".into()))
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        Err(VenueError::Transport("venue not wired".into()))
    }
}

pub struct PendingRisk;

impl RiskKernel for PendingRisk {
    fn assess(&mut self, _intent: &Intent, _account: &AccountView) -> RiskVerdict {
        RiskVerdict::Deny {
            reason: engine_types::DenyReason::UnknownState {
                detail: "risk kernel not wired".into(),
            },
        }
    }

    fn on_update(&mut self, _update: &OrderUpdate) {}
}
