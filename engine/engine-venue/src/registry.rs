//! Choosing a venue by name — the switch.
//!
//! The engine's config names a venue; this is where a name becomes an adapter.
//! One name decides all three of the venue's parts: the gateway that sends
//! orders, the private stream that reports what happened to them, and the
//! public market feed the strategies price against. That is what stops a
//! config half-switching — an engine sending orders to one venue while pricing
//! them off another's book.
//!
//! A name is not an address. It selects an adapter that is already compiled
//! in, and every venue host this crate knows is written in that venue's own
//! `realm` module — so no name, valid or not, can point the engine at an
//! endpoint that is not already in the source. `tests/venue_fence.rs` reads
//! the crate back and fails the suite if a host appears anywhere else.
//!
//! **The name carries the realm**, and it says so out loud. `bybit_mainnet`
//! is the funded account and is spelled that way deliberately: this is the
//! string an operator types into `engine.toml`, and the one place it is worth
//! spending a long name to make a mistake read as a mistake. Selecting it is
//! still not permission — the gateway refuses to build unless the owner has
//! armed `REAL_MONEY` on the host.
//!
//! Dispatch is an enum rather than `Box<dyn VenueGateway>` because
//! [`VenueGateway`] uses `async fn` in trait, which cannot be made into a
//! trait object as written, and because a match arm per venue is a forwarding
//! the compiler checks. The fence does not rest on this enum: it reads the
//! directory tree under `src/venues/`, and the completeness checks walk
//! [`VenueName::ALL`].
//!
//! The variants are one per *venue*, not one per realm: Bybit demo and Bybit
//! mainnet are the same adapter pointed at different accounts, and giving them
//! separate variants would duplicate every method below to no purpose.

use engine_types::ids::{Symbol, SymbolId};
use engine_types::market::{FeedError, OrderFeed};
use engine_types::orders::{
    AccountInventory, AmendSpec, InstrumentRule, OrderAck, OrderRequest, OrderUpdate, VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway, VenueMutationTiming};

#[cfg(feature = "binance")]
use crate::venues::binance::{BinanceGateway, BinanceOrderFeed, BinanceRealm};
#[cfg(feature = "bybit")]
use crate::venues::bybit::{BybitGateway, BybitInventoryProbe, BybitOrderFeed, VenueRealm};
#[cfg(feature = "hyperliquid")]
use crate::venues::hyperliquid::{
    HyperliquidGateway, HyperliquidInventoryProbe, HyperliquidOrderFeed, HyperliquidRealm,
};
#[cfg(feature = "lighter")]
use crate::venues::lighter::{LighterGateway, LighterOrderFeed, LighterRealm};
#[cfg(feature = "mexc")]
use crate::venues::mexc::{MexcGateway, MexcInventoryProbe, MexcOrderFeed, MexcRealm};
#[cfg(feature = "variational")]
use crate::venues::variational::{VariationalGateway, VariationalRealm};

pub use engine_public::registry::*;

/// The venue the engine is trading through, chosen at assembly and then
/// carried by value: static dispatch, no vtable on the order path.
// Built once at boot; keeping adapters inline preserves static dispatch.
#[allow(clippy::large_enum_variant)]
pub enum Venue {
    #[cfg(feature = "bybit")]
    Bybit(BybitGateway),
    #[cfg(feature = "hyperliquid")]
    Hyperliquid(HyperliquidGateway),
    #[cfg(feature = "lighter")]
    Lighter(LighterGateway),
    #[cfg(feature = "mexc")]
    Mexc(MexcGateway),
    #[cfg(feature = "binance")]
    Binance(BinanceGateway),
    #[cfg(feature = "variational")]
    Variational(VariationalGateway),
}

/// Credential-wide read capability used by generation-changing rollouts.
///
/// Deliberately separate from [`Venue`]: a disarmed funded account must still
/// be readable for a flatness proof, but the resulting value must not carry a
/// method that can place, cancel, amend, or otherwise mutate an order.
// Built once per command; keeping the probes inline preserves static dispatch.
#[allow(clippy::large_enum_variant)]
pub enum InventoryProbe {
    #[cfg(feature = "bybit")]
    Bybit(BybitInventoryProbe),
    #[cfg(feature = "mexc")]
    Mexc(MexcInventoryProbe),
    #[cfg(feature = "hyperliquid")]
    Hyperliquid(HyperliquidInventoryProbe),
}

impl InventoryProbe {
    pub fn build(name: VenueName) -> Result<Self, VenueError> {
        #[allow(unreachable_patterns)]
        match name {
            #[cfg(feature = "bybit")]
            VenueName::BybitDemo => Ok(Self::Bybit(BybitInventoryProbe::new(VenueRealm::Demo)?)),
            #[cfg(feature = "bybit")]
            VenueName::BybitMainnet => {
                Ok(Self::Bybit(BybitInventoryProbe::new(VenueRealm::Mainnet)?))
            }
            #[cfg(feature = "mexc")]
            VenueName::MexcMainnet => Ok(Self::Mexc(MexcInventoryProbe::new(MexcRealm::Mainnet)?)),
            #[cfg(feature = "hyperliquid")]
            VenueName::HyperliquidMainnet => Ok(Self::Hyperliquid(HyperliquidInventoryProbe::new(
                HyperliquidRealm::Mainnet,
            )?)),
            other => Err(VenueError::BadRequest(format!(
                "{} has no credential-wide inventory probe; flatness cannot be attested",
                other.as_str()
            ))),
        }
    }

    pub async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        match *self {
            #[cfg(feature = "bybit")]
            Self::Bybit(ref mut probe) => probe.account_identity().await,
            #[cfg(feature = "mexc")]
            Self::Mexc(ref mut probe) => probe.account_identity().await,
            #[cfg(feature = "hyperliquid")]
            Self::Hyperliquid(ref mut probe) => probe.account_identity().await,
        }
    }

    pub async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        match *self {
            #[cfg(feature = "bybit")]
            Self::Bybit(ref mut probe) => probe.account_inventory().await,
            #[cfg(feature = "mexc")]
            Self::Mexc(ref mut probe) => probe.account_inventory().await,
            #[cfg(feature = "hyperliquid")]
            Self::Hyperliquid(ref mut probe) => probe.account_inventory().await,
        }
    }
}

impl Venue {
    /// Build the venue this name selects. The realm comes from the name, and
    /// credentials come from the environment for that realm.
    pub fn build(name: VenueName, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        #[allow(unreachable_patterns)]
        Ok(match name {
            #[cfg(feature = "bybit")]
            VenueName::BybitDemo => Venue::Bybit(BybitGateway::new(VenueRealm::Demo, symbols)?),
            #[cfg(feature = "bybit")]
            VenueName::BybitMainnet => {
                Venue::Bybit(BybitGateway::new(VenueRealm::Mainnet, symbols)?)
            }
            #[cfg(feature = "hyperliquid")]
            VenueName::HyperliquidTestnet => {
                Venue::Hyperliquid(HyperliquidGateway::new(HyperliquidRealm::Testnet, symbols)?)
            }
            #[cfg(feature = "hyperliquid")]
            VenueName::HyperliquidMainnet => {
                Venue::Hyperliquid(HyperliquidGateway::new(HyperliquidRealm::Mainnet, symbols)?)
            }
            #[cfg(feature = "lighter")]
            VenueName::LighterTestnet => {
                Venue::Lighter(LighterGateway::new(LighterRealm::Testnet, symbols)?)
            }
            #[cfg(feature = "lighter")]
            VenueName::LighterMainnet => {
                Venue::Lighter(LighterGateway::new(LighterRealm::Mainnet, symbols)?)
            }
            #[cfg(feature = "mexc")]
            VenueName::MexcMainnet => Venue::Mexc(MexcGateway::new(MexcRealm::Mainnet, symbols)?),
            #[cfg(feature = "binance")]
            VenueName::BinanceTestnet => {
                Venue::Binance(BinanceGateway::new(BinanceRealm::Testnet, symbols)?)
            }
            #[cfg(feature = "binance")]
            VenueName::BinanceMainnet => {
                Venue::Binance(BinanceGateway::new(BinanceRealm::Mainnet, symbols)?)
            }
            #[cfg(feature = "variational")]
            VenueName::VariationalMainnet => {
                Venue::Variational(VariationalGateway::new(VariationalRealm::Mainnet, symbols)?)
            }
            other => return Err(other.disabled_error()),
        })
    }

    /// Build by the config's spelling. The parse and the build in one call,
    /// for callers that hold only the string.
    pub fn by_name(name: &str, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        Venue::build(VenueName::parse(name)?, symbols)
    }

    /// The name this venue was selected by — for the boot log, and so a test
    /// can prove each known name reaches its own adapter.
    pub fn name(&self) -> VenueName {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => match gw.realm() {
                VenueRealm::Demo => VenueName::BybitDemo,
                VenueRealm::Mainnet => VenueName::BybitMainnet,
            },
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => match gw.realm() {
                HyperliquidRealm::Testnet => VenueName::HyperliquidTestnet,
                HyperliquidRealm::Mainnet => VenueName::HyperliquidMainnet,
            },
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => match gw.realm() {
                LighterRealm::Testnet => VenueName::LighterTestnet,
                LighterRealm::Mainnet => VenueName::LighterMainnet,
            },
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => match gw.realm() {
                MexcRealm::Mainnet => VenueName::MexcMainnet,
            },
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => match gw.realm() {
                BinanceRealm::Testnet => VenueName::BinanceTestnet,
                BinanceRealm::Mainnet => VenueName::BinanceMainnet,
            },
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => match gw.realm() {
                VariationalRealm::Mainnet => VenueName::VariationalMainnet,
            },
        }
    }

    /// Which account this venue addresses. The engine logs it at boot and the
    /// heartbeat carries it, so an operator never has to infer from a config
    /// file which account a running process is on.
    pub fn realm(&self) -> &'static str {
        self.name().realm()
    }

    /// What this host signs as, in the venue's own terms — never the secret.
    ///
    /// Setting up a venue means registering something with it: an API wallet's
    /// address on Hyperliquid, a public key against an API key slot on
    /// Lighter. Deriving that by hand from the key on the box is exactly the
    /// step somebody gets wrong, so the engine will say it.
    pub fn signing_identity(&self) -> Option<String> {
        match self {
            // Not a secret: it rides in a header on every signed request.
            #[cfg(feature = "bybit")]
            Venue::Bybit(_) => None,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => Some(gw.signer_address()),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => Some(gw.public_key()),
            // Not a secret: it rides in a header on every signed request.
            #[cfg(feature = "mexc")]
            Venue::Mexc(_) => None,
            // Not a secret: it rides in a header on every signed request.
            #[cfg(feature = "binance")]
            Venue::Binance(_) => None,
            #[cfg(feature = "variational")]
            Venue::Variational(_) => None,
        }
    }
}

/// Every method hands straight to the chosen adapter. Nothing is decided
/// here: a wrapper that quietly substituted behaviour would be a venue the
/// caller never picked.
#[engine_types::async_trait]
impl VenueGateway for Venue {
    fn caps(&self) -> VenueCaps {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.caps(),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.caps(),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.caps(),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.caps(),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.caps(),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.caps(),
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.send_order(req).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.send_order(req).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.send_order(req).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.send_order(req).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.send_order(req).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.send_order(req).await,
        }
    }

    async fn send_orders(&mut self, reqs: &[OrderRequest]) -> Vec<Result<OrderAck, VenueError>> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.send_orders(reqs).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.send_orders(reqs).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.send_orders(reqs).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.send_orders(reqs).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.send_orders(reqs).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.send_orders(reqs).await,
        }
    }

    async fn send_orders_under(
        &mut self,
        reqs: &[OrderRequest],
        authority: Option<(
            &engine_types::AuthorityEpoch,
            engine_types::CommandAuthority,
        )>,
    ) -> Vec<Result<OrderAck, VenueError>> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.send_orders_under(reqs, authority).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.send_orders_under(reqs, authority).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.send_orders_under(reqs, authority).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.send_orders_under(reqs, authority).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.send_orders_under(reqs, authority).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.send_orders_under(reqs, authority).await,
        }
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.cancel_order(symbol, client_order_id).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.cancel_order(symbol, client_order_id).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.cancel_order(symbol, client_order_id).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.cancel_order(symbol, client_order_id).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.cancel_order(symbol, client_order_id).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.cancel_order(symbol, client_order_id).await,
        }
    }

    async fn cancel_orders(
        &mut self,
        requests: &[(SymbolId, String)],
    ) -> Vec<Result<(), VenueError>> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.cancel_orders(requests).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.cancel_orders(requests).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.cancel_orders(requests).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.cancel_orders(requests).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.cancel_orders(requests).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.cancel_orders(requests).await,
        }
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.amend_order(symbol, client_order_id, spec).await,
        }
    }

    async fn amend_orders(
        &mut self,
        requests: &[(SymbolId, String, AmendSpec)],
    ) -> Vec<Result<(), VenueError>> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.amend_orders(requests).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.amend_orders(requests).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.amend_orders(requests).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.amend_orders(requests).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.amend_orders(requests).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.amend_orders(requests).await,
        }
    }

    async fn amend_orders_under(
        &mut self,
        requests: &[engine_types::AmendRequest],
        epoch: &engine_types::AuthorityEpoch,
    ) -> Vec<Result<(), VenueError>> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.amend_orders_under(requests, epoch).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.amend_orders_under(requests, epoch).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.amend_orders_under(requests, epoch).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.amend_orders_under(requests, epoch).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.amend_orders_under(requests, epoch).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.amend_orders_under(requests, epoch).await,
        }
    }

    async fn set_stop_exact(
        &mut self,
        symbol: SymbolId,
        terms: &engine_types::order_terms::ExactStopTerms,
    ) -> Result<(), VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.set_stop_exact(symbol, terms).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.set_stop_exact(symbol, terms).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.set_stop_exact(symbol, terms).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.set_stop_exact(symbol, terms).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.set_stop_exact(symbol, terms).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.set_stop_exact(symbol, terms).await,
        }
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.set_stop(symbol, trigger_px).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.set_stop(symbol, trigger_px).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.set_stop(symbol, trigger_px).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.set_stop(symbol, trigger_px).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.set_stop(symbol, trigger_px).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.set_stop(symbol, trigger_px).await,
        }
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.set_leverage(symbol, leverage).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.set_leverage(symbol, leverage).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.set_leverage(symbol, leverage).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.set_leverage(symbol, leverage).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.set_leverage(symbol, leverage).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.set_leverage(symbol, leverage).await,
        }
    }

    fn take_mutation_timing(&mut self) -> Option<VenueMutationTiming> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.take_mutation_timing(),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.take_mutation_timing(),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.take_mutation_timing(),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.take_mutation_timing(),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.take_mutation_timing(),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.take_mutation_timing(),
        }
    }

    fn take_rate_wait_ns(&mut self) -> Option<u64> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.take_rate_wait_ns(),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.take_rate_wait_ns(),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.take_rate_wait_ns(),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.take_rate_wait_ns(),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.take_rate_wait_ns(),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.take_rate_wait_ns(),
        }
    }

    fn quota_wait(&self, command: engine_types::QueuedCommand) -> std::time::Duration {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.quota_wait(command),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.quota_wait(command),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.quota_wait(command),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.quota_wait(command),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.quota_wait(command),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.quota_wait(command),
        }
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => VenueGateway::add_symbol(gw, symbol),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => VenueGateway::add_symbol(gw, symbol),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => VenueGateway::add_symbol(gw, symbol),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => VenueGateway::add_symbol(gw, symbol),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => VenueGateway::add_symbol(gw, symbol),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => VenueGateway::add_symbol(gw, symbol),
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.account_identity().await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.account_identity().await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.account_identity().await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.account_identity().await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.account_identity().await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.account_identity().await,
        }
    }

    fn account_recovery_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::AccountRecoveryClient>> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gateway) => gateway.account_recovery_client(),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gateway) => gateway.account_recovery_client(),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gateway) => gateway.account_recovery_client(),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gateway) => gateway.account_recovery_client(),
            #[cfg(feature = "binance")]
            Venue::Binance(gateway) => gateway.account_recovery_client(),
            #[cfg(feature = "variational")]
            Venue::Variational(gateway) => gateway.account_recovery_client(),
        }
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.account_view().await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.account_view().await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.account_view().await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.account_view().await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.account_view().await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.account_view().await,
        }
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.instrument_rules().await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.instrument_rules().await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.instrument_rules().await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.instrument_rules().await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.instrument_rules().await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.instrument_rules().await,
        }
    }

    fn restore_instrument_catalog(
        &self,
        checkpoint: &engine_types::orders::InstrumentCatalogCheckpoint,
    ) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.restore_instrument_catalog(checkpoint),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.restore_instrument_catalog(checkpoint),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.restore_instrument_catalog(checkpoint),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.restore_instrument_catalog(checkpoint),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.restore_instrument_catalog(checkpoint),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.restore_instrument_catalog(checkpoint),
        }
    }
    fn install_instrument_catalog(
        &mut self,
        catalog: &engine_types::orders::InstrumentCatalog,
    ) -> Result<(), VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.install_instrument_catalog(catalog),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.install_instrument_catalog(catalog),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.install_instrument_catalog(catalog),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.install_instrument_catalog(catalog),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.install_instrument_catalog(catalog),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.install_instrument_catalog(catalog),
        }
    }

    fn instrument_catalog_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::InstrumentCatalogClient>> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.instrument_catalog_client(),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.instrument_catalog_client(),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.instrument_catalog_client(),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.instrument_catalog_client(),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.instrument_catalog_client(),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.instrument_catalog_client(),
        }
    }

    fn order_lookup_client(&self) -> Option<Box<dyn engine_types::orders::OrderLookupClient>> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.order_lookup_client(),
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.order_lookup_client(),
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.order_lookup_client(),
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.order_lookup_client(),
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.order_lookup_client(),
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.order_lookup_client(),
        }
    }

    async fn order_status(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.order_status(symbol, client_order_id).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.order_status(symbol, client_order_id).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.order_status(symbol, client_order_id).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.order_status(symbol, client_order_id).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.order_status(symbol, client_order_id).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.order_status(symbol, client_order_id).await,
        }
    }

    async fn instrument_specs(
        &mut self,
    ) -> Result<Vec<(Symbol, engine_types::numeric::ExactInstrumentSpec)>, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.instrument_specs().await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.instrument_specs().await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.instrument_specs().await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.instrument_specs().await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.instrument_specs().await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.instrument_specs().await,
        }
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.working_orders().await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.working_orders().await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.working_orders().await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.working_orders().await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.working_orders().await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.working_orders().await,
        }
    }

    async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.account_inventory().await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.account_inventory().await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.account_inventory().await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.account_inventory().await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.account_inventory().await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.account_inventory().await,
        }
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        match self {
            #[cfg(feature = "bybit")]
            Venue::Bybit(gw) => gw.executions(start_ms, end_ms).await,
            #[cfg(feature = "hyperliquid")]
            Venue::Hyperliquid(gw) => gw.executions(start_ms, end_ms).await,
            #[cfg(feature = "lighter")]
            Venue::Lighter(gw) => gw.executions(start_ms, end_ms).await,
            #[cfg(feature = "mexc")]
            Venue::Mexc(gw) => gw.executions(start_ms, end_ms).await,
            #[cfg(feature = "binance")]
            Venue::Binance(gw) => gw.executions(start_ms, end_ms).await,
            #[cfg(feature = "variational")]
            Venue::Variational(gw) => gw.executions(start_ms, end_ms).await,
        }
    }
}

/// The chosen venue's private order stream, behind one type for the same
/// reason the gateway is: `async fn` in trait cannot be a trait object, and a
/// closed enum keeps every feed visible in one place.
pub enum OrderFeeds {
    #[cfg(feature = "bybit")]
    Bybit(BybitOrderFeed),
    #[cfg(feature = "hyperliquid")]
    Hyperliquid(HyperliquidOrderFeed),
    /// Not a socket. Lighter's account channel names a fill by the venue's own
    /// order id and not by the client order index the engine minted, so a live
    /// fill cannot be attributed to the strategy that caused it. This feed
    /// paces the engine's own resync instead, and the fills arrive from the
    /// venue's execution history — see `venues/lighter/ws.rs`.
    #[cfg(feature = "lighter")]
    Lighter(LighterOrderFeed),
    /// A real socket: the logged-in `push.personal.order` and
    /// `push.personal.order.deal` channels, with a paced resync behind them
    /// because the venue's execution history stays the accounting authority.
    /// See `venues/mexc/ws.rs`.
    #[cfg(feature = "mexc")]
    Mexc(MexcOrderFeed),
    /// A real socket: the listen-key user-data stream, which the venue's
    /// testnet carries in full. See `venues/binance/ws.rs`.
    #[cfg(feature = "binance")]
    Binance(BinanceOrderFeed),
    /// Variational publishes no account stream, because it publishes no
    /// account. The feed exists so the engine's loop is the same shape on
    /// every venue; it simply never delivers an update.
    Silent,
}

impl OrderFeeds {
    /// Build the private stream for the venue this name selects — the same
    /// name that built the gateway, so the two cannot address different
    /// accounts.
    pub fn build(name: VenueName, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        #[allow(unreachable_patterns)]
        Ok(match name {
            #[cfg(feature = "bybit")]
            VenueName::BybitDemo => {
                OrderFeeds::Bybit(BybitOrderFeed::new(VenueRealm::Demo, symbols)?)
            }
            #[cfg(feature = "bybit")]
            VenueName::BybitMainnet => {
                OrderFeeds::Bybit(BybitOrderFeed::new(VenueRealm::Mainnet, symbols)?)
            }
            #[cfg(feature = "hyperliquid")]
            VenueName::HyperliquidTestnet => OrderFeeds::Hyperliquid(HyperliquidOrderFeed::new(
                HyperliquidRealm::Testnet,
                symbols,
            )?),
            #[cfg(feature = "hyperliquid")]
            VenueName::HyperliquidMainnet => OrderFeeds::Hyperliquid(HyperliquidOrderFeed::new(
                HyperliquidRealm::Mainnet,
                symbols,
            )?),
            #[cfg(feature = "lighter")]
            VenueName::LighterTestnet => {
                OrderFeeds::Lighter(LighterOrderFeed::new(LighterRealm::Testnet)?)
            }
            #[cfg(feature = "lighter")]
            VenueName::LighterMainnet => {
                OrderFeeds::Lighter(LighterOrderFeed::new(LighterRealm::Mainnet)?)
            }
            #[cfg(feature = "mexc")]
            VenueName::MexcMainnet => OrderFeeds::Mexc(MexcOrderFeed::new(MexcRealm::Mainnet)?),
            #[cfg(feature = "binance")]
            VenueName::BinanceTestnet => {
                OrderFeeds::Binance(BinanceOrderFeed::new(BinanceRealm::Testnet, symbols)?)
            }
            #[cfg(feature = "binance")]
            VenueName::BinanceMainnet => {
                OrderFeeds::Binance(BinanceOrderFeed::new(BinanceRealm::Mainnet, symbols)?)
            }
            #[cfg(feature = "variational")]
            VenueName::VariationalMainnet => OrderFeeds::Silent,
            other => return Err(other.disabled_error()),
        })
    }

    /// Establish the private-account observation channel before boot takes
    /// any snapshots. The first reset is a readiness watermark emitted only
    /// after the venue subscription is live; history recovery can then cover
    /// up to its own end while subsequent socket updates wait in this feed's
    /// queue. That closes the otherwise unobservable window between boot REST
    /// reads and the first lazy websocket poll.
    pub async fn await_ready(&mut self) -> Result<(), FeedError> {
        if matches!(self, OrderFeeds::Silent) {
            return Ok(());
        }
        match self.next_update().await? {
            OrderUpdate::StreamReset { .. } => Ok(()),
            other => Err(FeedError::BadMessage(format!(
                "private feed produced {other:?} before its readiness watermark"
            ))),
        }
    }
}

impl OrderFeed for OrderFeeds {
    fn learn(&mut self, symbol: &str, id: SymbolId) {
        match self {
            #[cfg(feature = "bybit")]
            OrderFeeds::Bybit(feed) => OrderFeed::learn(feed, symbol, id),
            #[cfg(feature = "hyperliquid")]
            OrderFeeds::Hyperliquid(feed) => OrderFeed::learn(feed, symbol, id),
            #[cfg(feature = "lighter")]
            OrderFeeds::Lighter(feed) => OrderFeed::learn(feed, symbol, id),
            #[cfg(feature = "mexc")]
            OrderFeeds::Mexc(feed) => OrderFeed::learn(feed, symbol, id),
            #[cfg(feature = "binance")]
            OrderFeeds::Binance(feed) => OrderFeed::learn(feed, symbol, id),
            OrderFeeds::Silent => (),
        }
    }

    fn learn_instrument(
        &mut self,
        id: SymbolId,
        spec: &engine_types::numeric::ExactInstrumentSpec,
    ) {
        match self {
            #[cfg(feature = "bybit")]
            OrderFeeds::Bybit(feed) => feed.learn_instrument(id, spec),
            #[cfg(feature = "hyperliquid")]
            OrderFeeds::Hyperliquid(feed) => feed.learn_instrument(id, spec),
            #[cfg(feature = "lighter")]
            OrderFeeds::Lighter(feed) => feed.learn_instrument(id, spec),
            #[cfg(feature = "mexc")]
            OrderFeeds::Mexc(feed) => feed.learn_instrument(id, spec),
            #[cfg(feature = "binance")]
            OrderFeeds::Binance(feed) => feed.learn_instrument(id, spec),
            OrderFeeds::Silent => (),
        }
    }

    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        match self {
            #[cfg(feature = "bybit")]
            OrderFeeds::Bybit(feed) => feed.next_update().await,
            #[cfg(feature = "hyperliquid")]
            OrderFeeds::Hyperliquid(feed) => feed.next_update().await,
            #[cfg(feature = "lighter")]
            OrderFeeds::Lighter(feed) => feed.next_update().await,
            #[cfg(feature = "mexc")]
            OrderFeeds::Mexc(feed) => feed.next_update().await,
            #[cfg(feature = "binance")]
            OrderFeeds::Binance(feed) => feed.next_update().await,
            // Never ready, rather than an error every loop turn: the engine
            // waits on this inside a `select!`, and a feed that returned an
            // error immediately would spin the loop at full speed reporting a
            // failure that is not one.
            OrderFeeds::Silent => std::future::pending().await,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unknown_name_is_refused_and_says_what_is_known() {
        let Err(err) = VenueName::parse("bybit") else {
            panic!("an unknown venue name was accepted");
        };
        let said = err.to_string();
        assert!(said.contains("bybit"), "{said}");
        for known in known_venues() {
            assert!(said.contains(known), "{said} does not mention {known}");
        }
    }

    #[test]
    fn a_bare_realm_name_is_not_a_venue_name() {
        // "mainnet" is a realm, not a venue. Accepting it here would mean two
        // spellings reach the funded account, and only one of them is the one
        // the fence and the docs talk about.
        for near_miss in [
            "mainnet",
            "demo",
            "bybit_main",
            "BYBIT_MAINNET",
            "hyperliquid",
            "",
        ] {
            assert!(
                VenueName::parse(near_miss).is_err(),
                "{near_miss:?} was accepted as a venue name"
            );
        }
    }

    #[test]
    fn every_known_name_parses_and_prints_back_the_same() {
        for name in known_venues() {
            let parsed = VenueName::parse(name).expect(name);
            assert_eq!(parsed.as_str(), name);
        }
    }

    #[test]
    fn the_name_decides_the_venue_the_realm_and_whether_it_is_real_money() {
        assert_eq!(VenueName::BybitDemo.venue(), "bybit");
        assert_eq!(VenueName::BybitDemo.realm(), "demo");
        assert!(!VenueName::BybitDemo.is_real_money());

        assert_eq!(VenueName::BybitMainnet.venue(), "bybit");
        assert_eq!(VenueName::BybitMainnet.realm(), "mainnet");
        assert!(VenueName::BybitMainnet.is_real_money());

        assert_eq!(VenueName::HyperliquidTestnet.venue(), "hyperliquid");
        assert_eq!(VenueName::HyperliquidTestnet.realm(), "hyperliquid_testnet");
        assert!(!VenueName::HyperliquidTestnet.is_real_money());

        assert_eq!(VenueName::HyperliquidMainnet.venue(), "hyperliquid");
        assert!(VenueName::HyperliquidMainnet.is_real_money());

        assert_eq!(VenueName::BinanceTestnet.venue(), "binance");
        assert_eq!(VenueName::BinanceTestnet.realm(), "binance_testnet");
        assert!(!VenueName::BinanceTestnet.is_real_money());

        assert_eq!(VenueName::BinanceMainnet.venue(), "binance");
        assert_eq!(VenueName::BinanceMainnet.realm(), "binance_mainnet");
        assert!(VenueName::BinanceMainnet.is_real_money());

        assert_eq!(VenueName::VariationalMainnet.venue(), "variational");
        assert_eq!(VenueName::VariationalMainnet.realm(), "variational_mainnet");
    }

    #[test]
    fn no_two_names_share_a_realm_string() {
        // The realm travels in the heartbeat and lease path. Sharing one realm
        // string would make two venue account identities indistinguishable.
        let mut seen: Vec<&str> = Vec::new();
        for name in known_venues() {
            let realm = VenueName::parse(name).unwrap().realm();
            assert!(
                !seen.contains(&realm),
                "{realm} is claimed by two venue names"
            );
            seen.push(realm);
        }
    }

    #[test]
    fn a_real_money_name_reads_as_real_money_in_its_own_spelling() {
        // The string an operator types has to make a mistake read as a
        // mistake. Every real-money name says "mainnet"; no practice name
        // does.
        for name in known_venues() {
            let parsed = VenueName::parse(name).unwrap();
            if parsed.is_real_money() {
                assert!(
                    name.contains("mainnet"),
                    "{name} moves real money and does not say so"
                );
            } else {
                assert!(
                    !name.contains("mainnet"),
                    "{name} reads like real money and is not"
                );
            }
        }
    }

    #[test]
    fn production_readiness_is_explicit_for_every_registered_realm() {
        for venue in VenueName::ALL.into_iter().filter(|name| name.compiled()) {
            match venue {
                VenueName::BybitDemo | VenueName::BybitMainnet => {
                    assert_eq!(venue.readiness(), VenueReadiness::LiveProven);
                    venue.require_engine_run_ready().unwrap();
                }
                VenueName::HyperliquidTestnet | VenueName::LighterTestnet => {
                    assert_eq!(venue.readiness(), VenueReadiness::TestnetCanary);
                    venue.require_engine_run_ready().unwrap();
                }
                VenueName::HyperliquidMainnet | VenueName::MexcMainnet => {
                    assert_eq!(venue.readiness(), VenueReadiness::LiveCanary);
                    assert!(!venue.unproven_capabilities().is_empty());
                    venue.require_engine_run_ready().unwrap();
                }
                VenueName::LighterMainnet
                | VenueName::BinanceTestnet
                | VenueName::BinanceMainnet => {
                    assert_eq!(venue.readiness(), VenueReadiness::ProductionBlocked);
                    let error = venue.require_engine_run_ready().unwrap_err().to_string();
                    assert!(error.contains("production-blocked"), "{error}");
                }
                VenueName::VariationalMainnet => {
                    assert_eq!(venue.readiness(), VenueReadiness::ReadOnly);
                    assert!(venue.require_engine_run_ready().is_err());
                }
            }
        }
    }

    #[test]
    fn the_canary_runs_on_the_practice_realm_and_on_every_live_canary_realm() {
        for venue in VenueName::ALL.into_iter().filter(|name| name.compiled()) {
            let permitted =
                venue == VenueName::BybitDemo || venue.readiness() == VenueReadiness::LiveCanary;
            assert_eq!(venue.require_canary_ready().is_ok(), permitted, "{venue}");
            if !permitted {
                let error = venue.require_canary_ready().unwrap_err().to_string();
                assert!(error.contains(venue.readiness().as_str()), "{error}");
            }
        }
    }

    #[test]
    fn a_live_canary_realm_takes_the_canary_and_runs_as_the_owners_forward_test() {
        // Funded capital owed its receipts: the bounded operator proof runs,
        // and so does the strategy loop, on posture and arming. The two
        // states no capability row produces stay refused.
        assert!(VenueReadiness::LiveCanary.permits_engine_run());
        assert!(!VenueReadiness::ProductionBlocked.permits_engine_run());
        assert!(!VenueReadiness::ReadOnly.permits_engine_run());
        assert_eq!(VenueReadiness::LiveCanary.as_str(), "live-canary");
    }
}
