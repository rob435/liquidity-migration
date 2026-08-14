//! Choosing a venue by name.
//!
//! The engine's config names a venue; this is where a name becomes an
//! adapter. Adding Hyperliquid or MEXC is a module in this crate, a variant
//! below, and a name in [`KNOWN_VENUES`] — the engine's wiring does not move,
//! because the loop is generic over the gateway type either way.
//!
//! A name is not an address. It selects an adapter that is already compiled
//! in, and every venue host this crate knows is written in `lib.rs` — so no
//! name, valid or not, can point the engine at an endpoint that is not
//! already in the source. `tests/demo_fence.rs` reads the crate back and
//! fails the suite if another host ever appears.
//!
//! Dispatch is an enum rather than `Box<dyn VenueGateway>` for two reasons.
//! [`VenueGateway`] uses `async fn` in trait, which cannot be made into a
//! trait object at all; and a closed enum keeps the whole set of venues
//! visible in one place, which is the property the fence depends on.

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{AmendSpec, InstrumentRule, OrderAck, OrderRequest, VenueOrder};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};

use crate::gateway::BybitGateway;

/// Bybit's practice account: the engine's default venue, and today its only
/// one.
pub const BYBIT_DEMO: &str = "bybit_demo";

/// Every name [`Venue::by_name`] answers to.
pub const KNOWN_VENUES: &[&str] = &[BYBIT_DEMO];

/// The venue the engine is trading through, chosen at assembly and then
/// carried by value: static dispatch, no vtable on the order path.
pub enum Venue {
    BybitDemo(BybitGateway),
}

impl Venue {
    /// Build the venue this name selects. Credentials come from the
    /// environment, as they do for the adapter directly.
    ///
    /// An unknown name is refused rather than defaulted: a typo that quietly
    /// fell back to some other venue would be a strategy trading somewhere
    /// nobody chose.
    pub fn by_name(name: &str, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        match name {
            BYBIT_DEMO => Ok(Venue::BybitDemo(BybitGateway::new(symbols)?)),
            other => Err(VenueError::BadRequest(format!(
                "no venue named \"{other}\" is compiled into this engine (known: {})",
                KNOWN_VENUES.join(", ")
            ))),
        }
    }

    /// The name this venue was selected by — for the boot log, and so a test
    /// can prove each known name reaches its own adapter.
    pub fn name(&self) -> &'static str {
        match self {
            Venue::BybitDemo(_) => BYBIT_DEMO,
        }
    }
}

/// Every method hands straight to the chosen adapter. Nothing is decided
/// here: a wrapper that quietly substituted behaviour would be a venue the
/// caller never picked.
impl VenueGateway for Venue {
    fn caps(&self) -> VenueCaps {
        match self {
            Venue::BybitDemo(gw) => gw.caps(),
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        match self {
            Venue::BybitDemo(gw) => gw.send_order(req).await,
        }
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        match self {
            Venue::BybitDemo(gw) => gw.cancel_order(symbol, client_order_id).await,
        }
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        match self {
            Venue::BybitDemo(gw) => gw.amend_order(symbol, client_order_id, spec).await,
        }
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        match self {
            Venue::BybitDemo(gw) => gw.set_stop(symbol, trigger_px).await,
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        match self {
            Venue::BybitDemo(gw) => gw.account_identity().await,
        }
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        match self {
            Venue::BybitDemo(gw) => gw.account_view().await,
        }
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        match self {
            Venue::BybitDemo(gw) => gw.instrument_rules().await,
        }
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        match self {
            Venue::BybitDemo(gw) => gw.working_orders().await,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unknown_name_is_refused_by_name_and_says_what_is_known() {
        let Err(err) = Venue::by_name("mainnet", vec!["BTCUSDT".to_string()]) else {
            panic!("an unknown venue name was accepted");
        };
        let said = err.to_string();
        assert!(said.contains("mainnet"), "{said}");
        for known in KNOWN_VENUES {
            assert!(said.contains(known), "{said} does not mention {known}");
        }
    }
}
