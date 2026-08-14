//! Choosing a venue by name.
//!
//! The engine's config names a venue; this is where a name becomes an
//! adapter. Adding Hyperliquid or MEXC is a module in this crate, a variant
//! below, and a name in [`KNOWN_VENUES`] — the engine's wiring does not move,
//! because the loop is generic over the gateway type either way.
//!
//! A name is not an address. It selects an adapter that is already compiled
//! in, and every venue host this crate knows is written in [`crate::realm`] —
//! so no name, valid or not, can point the engine at an endpoint that is not
//! already in the source. `tests/venue_fence.rs` reads the crate back and
//! fails the suite if a host appears anywhere else.
//!
//! **The name carries the realm**, and it says so out loud. `bybit_mainnet`
//! is the funded account and is spelled that way deliberately: this is the
//! string an operator types into `engine.toml`, and the one place it is worth
//! spending a long name to make a mistake read as a mistake. Selecting it is
//! still not permission — the gateway refuses to build unless the owner has
//! armed `REAL_MONEY` on the host.
//!
//! Dispatch is an enum rather than `Box<dyn VenueGateway>` for two reasons.
//! [`VenueGateway`] uses `async fn` in trait, which cannot be made into a
//! trait object at all; and a closed enum keeps the whole set of venues
//! visible in one place, which is the property the fence depends on.
//!
//! The variants are one per *venue*, not one per realm: Bybit demo and Bybit
//! mainnet are the same adapter pointed at different accounts, and giving them
//! separate variants would duplicate every method below to no purpose.

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{AmendSpec, InstrumentRule, OrderAck, OrderRequest, VenueOrder};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};

use crate::gateway::BybitGateway;
use crate::realm::VenueRealm;

/// Bybit's practice account: play money on a real matching engine, and the
/// engine's default.
pub const BYBIT_DEMO: &str = "bybit_demo";

/// Bybit's funded account: real money. Requires `REAL_MONEY` armed by the
/// owner before it will build.
pub const BYBIT_MAINNET: &str = "bybit_mainnet";

/// Every name [`Venue::by_name`] answers to.
pub const KNOWN_VENUES: &[&str] = &[BYBIT_DEMO, BYBIT_MAINNET];

/// The venue the engine is trading through, chosen at assembly and then
/// carried by value: static dispatch, no vtable on the order path.
pub enum Venue {
    Bybit(BybitGateway),
}

impl Venue {
    /// Build the venue this name selects. The realm comes from the name, and
    /// credentials come from the environment for that realm.
    ///
    /// An unknown name is refused rather than defaulted: a typo that quietly
    /// fell back to some other venue would be a strategy trading somewhere
    /// nobody chose.
    pub fn by_name(name: &str, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let realm = match name {
            BYBIT_DEMO => VenueRealm::Demo,
            BYBIT_MAINNET => VenueRealm::Mainnet,
            other => {
                return Err(VenueError::BadRequest(format!(
                    "no venue named \"{other}\" is compiled into this engine (known: {})",
                    KNOWN_VENUES.join(", ")
                )))
            }
        };
        Ok(Venue::Bybit(BybitGateway::new(realm, symbols)?))
    }

    /// The name this venue was selected by — for the boot log, and so a test
    /// can prove each known name reaches its own adapter.
    pub fn name(&self) -> &'static str {
        match self {
            Venue::Bybit(gw) => match gw.realm() {
                VenueRealm::Demo => BYBIT_DEMO,
                VenueRealm::Mainnet => BYBIT_MAINNET,
            },
        }
    }

    /// Which account this venue addresses. The engine logs it at boot and the
    /// heartbeat carries it, so an operator never has to infer from a config
    /// file which account a running process is on.
    pub fn realm(&self) -> VenueRealm {
        match self {
            Venue::Bybit(gw) => gw.realm(),
        }
    }
}

/// Every method hands straight to the chosen adapter. Nothing is decided
/// here: a wrapper that quietly substituted behaviour would be a venue the
/// caller never picked.
impl VenueGateway for Venue {
    fn caps(&self) -> VenueCaps {
        match self {
            Venue::Bybit(gw) => gw.caps(),
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.send_order(req).await,
        }
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        match self {
            Venue::Bybit(gw) => gw.cancel_order(symbol, client_order_id).await,
        }
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        match self {
            Venue::Bybit(gw) => gw.amend_order(symbol, client_order_id, spec).await,
        }
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        match self {
            Venue::Bybit(gw) => gw.set_stop(symbol, trigger_px).await,
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.account_identity().await,
        }
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.account_view().await,
        }
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.instrument_rules().await,
        }
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.working_orders().await,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unknown_name_is_refused_by_name_and_says_what_is_known() {
        let Err(err) = Venue::by_name("bybit", vec!["BTCUSDT".to_string()]) else {
            panic!("an unknown venue name was accepted");
        };
        let said = err.to_string();
        assert!(said.contains("bybit"), "{said}");
        for known in KNOWN_VENUES {
            assert!(said.contains(known), "{said} does not mention {known}");
        }
    }

    #[test]
    fn a_bare_realm_name_is_not_a_venue_name() {
        // "mainnet" is a realm, not a venue. Accepting it here would mean two
        // spellings reach the funded account, and only one of them is the one
        // the fence and the docs talk about.
        for near_miss in ["mainnet", "demo", "bybit_main", "BYBIT_MAINNET"] {
            assert!(
                Venue::by_name(near_miss, vec!["BTCUSDT".to_string()]).is_err(),
                "{near_miss:?} was accepted as a venue name"
            );
        }
    }
}
