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
    AccountInventory, AmendSpec, InstrumentRule, OrderAck, OrderRequest, OrderUpdate,
    VenueExecution, VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway, VenueMutationTiming};

use crate::venues::binance::{BinanceGateway, BinanceOrderFeed, BinanceRealm};
use crate::venues::bybit::{BybitGateway, BybitInventoryProbe, BybitOrderFeed, VenueRealm};
use crate::venues::hyperliquid::{HyperliquidGateway, HyperliquidOrderFeed, HyperliquidRealm};
use crate::venues::lighter::{LighterGateway, LighterOrderFeed, LighterRealm};
use crate::venues::mexc::{MexcGateway, MexcOrderFeed, MexcRealm};
use crate::venues::variational::{VariationalGateway, VariationalRealm};

/// Bybit's practice account: play money on a real matching engine, and the
/// engine's default.
pub const BYBIT_DEMO: &str = "bybit_demo";

/// Bybit's funded account: real money. Requires `REAL_MONEY` armed by the
/// owner before it will build.
pub const BYBIT_MAINNET: &str = "bybit_mainnet";

/// Hyperliquid's testnet: test funds on a real matching engine.
pub const HYPERLIQUID_TESTNET: &str = "hyperliquid_testnet";

/// Hyperliquid's funded account. Real money, and armed the same way.
pub const HYPERLIQUID_MAINNET: &str = "hyperliquid_mainnet";

/// Lighter's testnet rollup: test funds on a real matching engine.
pub const LIGHTER_TESTNET: &str = "lighter_testnet";

/// Lighter's funded account. Real money, and armed the same way.
pub const LIGHTER_MAINNET: &str = "lighter_mainnet";

/// MEXC's funded futures account. Real money, and the only MEXC realm there
/// is — the venue publishes no testnet host, so there is no practice spelling
/// of this name.
pub const MEXC_MAINNET: &str = "mexc_mainnet";

/// Binance's futures testnet: test funds on a real matching engine, carrying
/// the full private stream.
pub const BINANCE_TESTNET: &str = "binance_testnet";

/// Binance's funded futures account. Real money, and armed the same way.
pub const BINANCE_MAINNET: &str = "binance_mainnet";

/// Variational's production endpoint, which publishes market data and no
/// trading API. Selecting it gives an engine that reads the venue and refuses
/// to trade it, saying why.
pub const VARIATIONAL_MAINNET: &str = "variational_mainnet";

/// Every name [`VenueName::parse`] answers to, derived from the one list.
pub fn known_venues() -> [&'static str; VenueName::ALL.len()] {
    VenueName::ALL.map(VenueName::as_str)
}

/// A venue name that has been read: which adapter, and which of its realms.
///
/// Parsed once, at assembly, and then carried to all three constructors. That
/// is the switch: the gateway, the private stream and the market feed cannot
/// disagree about which venue is being traded, because none of them is told a
/// name of its own.
#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash)]
pub enum VenueName {
    BybitDemo,
    BybitMainnet,
    HyperliquidTestnet,
    HyperliquidMainnet,
    LighterTestnet,
    LighterMainnet,
    MexcMainnet,
    BinanceTestnet,
    BinanceMainnet,
    VariationalMainnet,
}

/// Evidence state attached to every selectable venue realm.
///
/// A compiled adapter is not production evidence. Moving a realm from
/// `ProductionBlocked` to `LiveProven` is a reviewed source change made only
/// after the smallest permitted order and cancel/fill lifecycle has been
/// observed on that exact venue.
#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash)]
pub enum VenueReadiness {
    LiveProven,
    /// Offline conformance is green; this practice realm exists to gather the
    /// missing live evidence with test funds.
    TestnetCanary,
    /// Real capital is blocked until exact-venue live evidence exists.
    ProductionBlocked,
    /// Public data only; no trading API is available to this adapter.
    ReadOnly,
}

impl VenueReadiness {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::LiveProven => "live-proven",
            Self::TestnetCanary => "testnet-canary",
            Self::ProductionBlocked => "production-blocked",
            Self::ReadOnly => "read-only",
        }
    }

    pub fn permits_engine_run(self) -> bool {
        matches!(self, Self::LiveProven | Self::TestnetCanary)
    }
}

impl VenueName {
    /// Every venue this engine can be pointed at.
    ///
    /// The one list. The parser walks it, the refusal names it, and every
    /// completeness check iterates it — so a venue cannot be selectable and
    /// unchecked at the same time. Rust cannot enumerate an enum's variants,
    /// so this is still typed by hand; what it buys is that a variant left out
    /// is a venue no config can select, refused at boot, rather than one that
    /// works and is visited by no test.
    pub const ALL: [VenueName; 10] = [
        VenueName::BybitDemo,
        VenueName::BybitMainnet,
        VenueName::HyperliquidTestnet,
        VenueName::HyperliquidMainnet,
        VenueName::LighterTestnet,
        VenueName::LighterMainnet,
        VenueName::MexcMainnet,
        VenueName::BinanceTestnet,
        VenueName::BinanceMainnet,
        VenueName::VariationalMainnet,
    ];

    /// Read one of the names above, refusing every fallback.
    ///
    /// An unknown name is refused rather than defaulted: a typo that quietly
    /// fell back to some other venue would be a strategy trading somewhere
    /// nobody chose.
    pub fn parse(name: &str) -> Result<Self, VenueError> {
        let name = name.trim();
        VenueName::ALL
            .into_iter()
            .find(|known| known.as_str() == name)
            .ok_or_else(|| {
                VenueError::BadRequest(format!(
                    "no venue named \"{name}\" is compiled into this engine (known: {})",
                    known_venues().join(", ")
                ))
            })
    }

    pub fn as_str(self) -> &'static str {
        match self {
            VenueName::BybitDemo => BYBIT_DEMO,
            VenueName::BybitMainnet => BYBIT_MAINNET,
            VenueName::HyperliquidTestnet => HYPERLIQUID_TESTNET,
            VenueName::HyperliquidMainnet => HYPERLIQUID_MAINNET,
            VenueName::LighterTestnet => LIGHTER_TESTNET,
            VenueName::LighterMainnet => LIGHTER_MAINNET,
            VenueName::MexcMainnet => MEXC_MAINNET,
            VenueName::BinanceTestnet => BINANCE_TESTNET,
            VenueName::BinanceMainnet => BINANCE_MAINNET,
            VenueName::VariationalMainnet => VARIATIONAL_MAINNET,
        }
    }

    /// Which venue, without the realm. What the lease file and the heartbeat
    /// are named by.
    pub fn venue(self) -> &'static str {
        match self {
            VenueName::BybitDemo | VenueName::BybitMainnet => crate::lease::VENUE_BYBIT,
            VenueName::HyperliquidTestnet | VenueName::HyperliquidMainnet => {
                crate::venues::hyperliquid::VENUE_NAME
            }
            VenueName::LighterTestnet | VenueName::LighterMainnet => {
                crate::venues::lighter::VENUE_NAME
            }
            VenueName::MexcMainnet => crate::venues::mexc::VENUE_NAME,
            VenueName::BinanceTestnet | VenueName::BinanceMainnet => {
                crate::venues::binance::VENUE_NAME
            }
            VenueName::VariationalMainnet => crate::venues::variational::VENUE_NAME,
        }
    }

    /// Which of that venue's realms, spelled the way the heartbeat spells it.
    ///
    /// The venues added after Bybit qualify realm names with the venue. This
    /// string travels in the engine heartbeat and names the lease file, so it
    /// must identify one account namespace without ambiguity.
    pub fn realm(self) -> &'static str {
        match self {
            VenueName::BybitDemo => VenueRealm::Demo.as_str(),
            VenueName::BybitMainnet => VenueRealm::Mainnet.as_str(),
            VenueName::HyperliquidTestnet => HyperliquidRealm::Testnet.as_str(),
            VenueName::HyperliquidMainnet => HyperliquidRealm::Mainnet.as_str(),
            VenueName::LighterTestnet => LighterRealm::Testnet.as_str(),
            VenueName::LighterMainnet => LighterRealm::Mainnet.as_str(),
            VenueName::MexcMainnet => MexcRealm::Mainnet.as_str(),
            VenueName::BinanceTestnet => BinanceRealm::Testnet.as_str(),
            VenueName::BinanceMainnet => BinanceRealm::Mainnet.as_str(),
            VenueName::VariationalMainnet => VariationalRealm::Mainnet.as_str(),
        }
    }

    /// The two environment variables this name's realm reads.
    ///
    /// Here rather than only in each realm table so the whole set can be
    /// walked from [`VenueName::ALL`]. "A key left on a host for one account can
    /// never authenticate another" is a claim about every pair of realms in
    /// the engine, and a check that lists them by hand is one a new venue
    /// silently falls out of — which is exactly what happened to Lighter. The
    /// match below is exhaustive, so the compiler asks the question instead.
    pub fn credential_vars(self) -> (&'static str, &'static str) {
        match self {
            VenueName::BybitDemo => VenueRealm::Demo.credential_vars(),
            VenueName::BybitMainnet => VenueRealm::Mainnet.credential_vars(),
            VenueName::HyperliquidTestnet => HyperliquidRealm::Testnet.credential_vars(),
            VenueName::HyperliquidMainnet => HyperliquidRealm::Mainnet.credential_vars(),
            VenueName::LighterTestnet => LighterRealm::Testnet.credential_vars(),
            VenueName::LighterMainnet => LighterRealm::Mainnet.credential_vars(),
            VenueName::MexcMainnet => MexcRealm::Mainnet.credential_vars(),
            VenueName::BinanceTestnet => BinanceRealm::Testnet.credential_vars(),
            VenueName::BinanceMainnet => BinanceRealm::Mainnet.credential_vars(),
            VenueName::VariationalMainnet => VariationalRealm::Mainnet.credential_vars(),
        }
    }

    /// Whether this name reaches real capital. Read for the boot log and for
    /// the operator-facing checks; the refusal itself lives in
    /// [`crate::arming`], at the credential read.
    pub fn is_real_money(self) -> bool {
        match self {
            VenueName::BybitDemo => VenueRealm::Demo.is_real_money(),
            VenueName::BybitMainnet => VenueRealm::Mainnet.is_real_money(),
            VenueName::HyperliquidTestnet => HyperliquidRealm::Testnet.is_real_money(),
            VenueName::HyperliquidMainnet => HyperliquidRealm::Mainnet.is_real_money(),
            VenueName::LighterTestnet => LighterRealm::Testnet.is_real_money(),
            VenueName::LighterMainnet => LighterRealm::Mainnet.is_real_money(),
            VenueName::MexcMainnet => MexcRealm::Mainnet.is_real_money(),
            VenueName::BinanceTestnet => BinanceRealm::Testnet.is_real_money(),
            VenueName::BinanceMainnet => BinanceRealm::Mainnet.is_real_money(),
            VenueName::VariationalMainnet => VariationalRealm::Mainnet.is_real_money(),
        }
    }

    /// Production evidence, kept beside the one exhaustive realm registry so
    /// a newly added venue cannot silently inherit somebody else's status.
    pub fn readiness(self) -> VenueReadiness {
        match self {
            VenueName::BybitDemo | VenueName::BybitMainnet => VenueReadiness::LiveProven,
            VenueName::HyperliquidTestnet | VenueName::LighterTestnet => {
                VenueReadiness::TestnetCanary
            }
            VenueName::HyperliquidMainnet
            | VenueName::LighterMainnet
            | VenueName::MexcMainnet
            | VenueName::BinanceTestnet
            | VenueName::BinanceMainnet => VenueReadiness::ProductionBlocked,
            VenueName::VariationalMainnet => VenueReadiness::ReadOnly,
        }
    }

    /// Refuse before the engine opens a log, credential, or socket when this
    /// realm has not earned the evidence its capital class needs.
    pub fn require_engine_run_ready(self) -> Result<(), VenueError> {
        if self.readiness().permits_engine_run() {
            return Ok(());
        }
        Err(VenueError::BadRequest(format!(
            "{} readiness is {}; the execution engine is blocked until this exact realm has reviewed live order lifecycle evidence",
            self.as_str(),
            self.readiness().as_str()
        )))
    }
}

impl std::fmt::Display for VenueName {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The venue the engine is trading through, chosen at assembly and then
/// carried by value: static dispatch, no vtable on the order path.
// Built once at boot; keeping adapters inline preserves static dispatch.
#[allow(clippy::large_enum_variant)]
pub enum Venue {
    Bybit(BybitGateway),
    Hyperliquid(HyperliquidGateway),
    Lighter(LighterGateway),
    Mexc(MexcGateway),
    Binance(BinanceGateway),
    Variational(VariationalGateway),
}

/// Credential-wide read capability used by generation-changing rollouts.
///
/// Deliberately separate from [`Venue`]: a disarmed funded account must still
/// be readable for a flatness proof, but the resulting value must not carry a
/// method that can place, cancel, amend, or otherwise mutate an order.
pub enum InventoryProbe {
    Bybit(BybitInventoryProbe),
}

impl InventoryProbe {
    pub fn build(name: VenueName) -> Result<Self, VenueError> {
        match name {
            VenueName::BybitDemo => Ok(Self::Bybit(BybitInventoryProbe::new(VenueRealm::Demo)?)),
            VenueName::BybitMainnet => {
                Ok(Self::Bybit(BybitInventoryProbe::new(VenueRealm::Mainnet)?))
            }
            other => Err(VenueError::BadRequest(format!(
                "{} has no credential-wide inventory probe; flatness cannot be attested",
                other.as_str()
            ))),
        }
    }

    pub async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        match self {
            Self::Bybit(probe) => probe.account_identity().await,
        }
    }

    pub async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        match self {
            Self::Bybit(probe) => probe.account_inventory().await,
        }
    }
}

impl Venue {
    /// Build the venue this name selects. The realm comes from the name, and
    /// credentials come from the environment for that realm.
    pub fn build(name: VenueName, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        Ok(match name {
            VenueName::BybitDemo => Venue::Bybit(BybitGateway::new(VenueRealm::Demo, symbols)?),
            VenueName::BybitMainnet => {
                Venue::Bybit(BybitGateway::new(VenueRealm::Mainnet, symbols)?)
            }
            VenueName::HyperliquidTestnet => {
                Venue::Hyperliquid(HyperliquidGateway::new(HyperliquidRealm::Testnet, symbols)?)
            }
            VenueName::HyperliquidMainnet => {
                Venue::Hyperliquid(HyperliquidGateway::new(HyperliquidRealm::Mainnet, symbols)?)
            }
            VenueName::LighterTestnet => {
                Venue::Lighter(LighterGateway::new(LighterRealm::Testnet, symbols)?)
            }
            VenueName::LighterMainnet => {
                Venue::Lighter(LighterGateway::new(LighterRealm::Mainnet, symbols)?)
            }
            VenueName::MexcMainnet => Venue::Mexc(MexcGateway::new(MexcRealm::Mainnet, symbols)?),
            VenueName::BinanceTestnet => {
                Venue::Binance(BinanceGateway::new(BinanceRealm::Testnet, symbols)?)
            }
            VenueName::BinanceMainnet => {
                Venue::Binance(BinanceGateway::new(BinanceRealm::Mainnet, symbols)?)
            }
            VenueName::VariationalMainnet => {
                Venue::Variational(VariationalGateway::new(VariationalRealm::Mainnet, symbols)?)
            }
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
            Venue::Bybit(gw) => match gw.realm() {
                VenueRealm::Demo => VenueName::BybitDemo,
                VenueRealm::Mainnet => VenueName::BybitMainnet,
            },
            Venue::Hyperliquid(gw) => match gw.realm() {
                HyperliquidRealm::Testnet => VenueName::HyperliquidTestnet,
                HyperliquidRealm::Mainnet => VenueName::HyperliquidMainnet,
            },
            Venue::Lighter(gw) => match gw.realm() {
                LighterRealm::Testnet => VenueName::LighterTestnet,
                LighterRealm::Mainnet => VenueName::LighterMainnet,
            },
            Venue::Mexc(gw) => match gw.realm() {
                MexcRealm::Mainnet => VenueName::MexcMainnet,
            },
            Venue::Binance(gw) => match gw.realm() {
                BinanceRealm::Testnet => VenueName::BinanceTestnet,
                BinanceRealm::Mainnet => VenueName::BinanceMainnet,
            },
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
            Venue::Bybit(_) => None,
            Venue::Hyperliquid(gw) => Some(gw.signer_address()),
            Venue::Lighter(gw) => Some(gw.public_key()),
            // Not a secret: it rides in a header on every signed request.
            Venue::Mexc(_) => None,
            // Not a secret: it rides in a header on every signed request.
            Venue::Binance(_) => None,
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
            Venue::Bybit(gw) => gw.caps(),
            Venue::Hyperliquid(gw) => gw.caps(),
            Venue::Lighter(gw) => gw.caps(),
            Venue::Mexc(gw) => gw.caps(),
            Venue::Binance(gw) => gw.caps(),
            Venue::Variational(gw) => gw.caps(),
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.send_order(req).await,
            Venue::Hyperliquid(gw) => gw.send_order(req).await,
            Venue::Lighter(gw) => gw.send_order(req).await,
            Venue::Mexc(gw) => gw.send_order(req).await,
            Venue::Binance(gw) => gw.send_order(req).await,
            Venue::Variational(gw) => gw.send_order(req).await,
        }
    }

    async fn send_orders(&mut self, reqs: &[OrderRequest]) -> Vec<Result<OrderAck, VenueError>> {
        match self {
            Venue::Bybit(gw) => gw.send_orders(reqs).await,
            Venue::Hyperliquid(gw) => gw.send_orders(reqs).await,
            Venue::Lighter(gw) => gw.send_orders(reqs).await,
            Venue::Mexc(gw) => gw.send_orders(reqs).await,
            Venue::Binance(gw) => gw.send_orders(reqs).await,
            Venue::Variational(gw) => gw.send_orders(reqs).await,
        }
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        match self {
            Venue::Bybit(gw) => gw.cancel_order(symbol, client_order_id).await,
            Venue::Hyperliquid(gw) => gw.cancel_order(symbol, client_order_id).await,
            Venue::Lighter(gw) => gw.cancel_order(symbol, client_order_id).await,
            Venue::Mexc(gw) => gw.cancel_order(symbol, client_order_id).await,
            Venue::Binance(gw) => gw.cancel_order(symbol, client_order_id).await,
            Venue::Variational(gw) => gw.cancel_order(symbol, client_order_id).await,
        }
    }

    async fn cancel_orders(
        &mut self,
        requests: &[(SymbolId, String)],
    ) -> Vec<Result<(), VenueError>> {
        match self {
            Venue::Bybit(gw) => gw.cancel_orders(requests).await,
            Venue::Hyperliquid(gw) => gw.cancel_orders(requests).await,
            Venue::Lighter(gw) => gw.cancel_orders(requests).await,
            Venue::Mexc(gw) => gw.cancel_orders(requests).await,
            Venue::Binance(gw) => gw.cancel_orders(requests).await,
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
            Venue::Bybit(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            Venue::Hyperliquid(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            Venue::Lighter(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            Venue::Mexc(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            Venue::Binance(gw) => gw.amend_order(symbol, client_order_id, spec).await,
            Venue::Variational(gw) => gw.amend_order(symbol, client_order_id, spec).await,
        }
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        match self {
            Venue::Bybit(gw) => gw.set_stop(symbol, trigger_px).await,
            Venue::Hyperliquid(gw) => gw.set_stop(symbol, trigger_px).await,
            Venue::Lighter(gw) => gw.set_stop(symbol, trigger_px).await,
            Venue::Mexc(gw) => gw.set_stop(symbol, trigger_px).await,
            Venue::Binance(gw) => gw.set_stop(symbol, trigger_px).await,
            Venue::Variational(gw) => gw.set_stop(symbol, trigger_px).await,
        }
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        match self {
            Venue::Bybit(gw) => gw.set_leverage(symbol, leverage).await,
            Venue::Hyperliquid(gw) => gw.set_leverage(symbol, leverage).await,
            Venue::Lighter(gw) => gw.set_leverage(symbol, leverage).await,
            Venue::Mexc(gw) => gw.set_leverage(symbol, leverage).await,
            Venue::Binance(gw) => gw.set_leverage(symbol, leverage).await,
            Venue::Variational(gw) => gw.set_leverage(symbol, leverage).await,
        }
    }

    fn take_mutation_timing(&mut self) -> Option<VenueMutationTiming> {
        match self {
            Venue::Bybit(gw) => gw.take_mutation_timing(),
            Venue::Hyperliquid(gw) => gw.take_mutation_timing(),
            Venue::Lighter(gw) => gw.take_mutation_timing(),
            Venue::Mexc(gw) => gw.take_mutation_timing(),
            Venue::Binance(gw) => gw.take_mutation_timing(),
            Venue::Variational(gw) => gw.take_mutation_timing(),
        }
    }

    fn take_rate_wait_ns(&mut self) -> Option<u64> {
        match self {
            Venue::Bybit(gw) => gw.take_rate_wait_ns(),
            Venue::Hyperliquid(gw) => gw.take_rate_wait_ns(),
            Venue::Lighter(gw) => gw.take_rate_wait_ns(),
            Venue::Mexc(gw) => gw.take_rate_wait_ns(),
            Venue::Binance(gw) => gw.take_rate_wait_ns(),
            Venue::Variational(gw) => gw.take_rate_wait_ns(),
        }
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        match self {
            Venue::Bybit(gw) => VenueGateway::add_symbol(gw, symbol),
            Venue::Hyperliquid(gw) => VenueGateway::add_symbol(gw, symbol),
            Venue::Lighter(gw) => VenueGateway::add_symbol(gw, symbol),
            Venue::Mexc(gw) => VenueGateway::add_symbol(gw, symbol),
            Venue::Binance(gw) => VenueGateway::add_symbol(gw, symbol),
            Venue::Variational(gw) => VenueGateway::add_symbol(gw, symbol),
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.account_identity().await,
            Venue::Hyperliquid(gw) => gw.account_identity().await,
            Venue::Lighter(gw) => gw.account_identity().await,
            Venue::Mexc(gw) => gw.account_identity().await,
            Venue::Binance(gw) => gw.account_identity().await,
            Venue::Variational(gw) => gw.account_identity().await,
        }
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.account_view().await,
            Venue::Hyperliquid(gw) => gw.account_view().await,
            Venue::Lighter(gw) => gw.account_view().await,
            Venue::Mexc(gw) => gw.account_view().await,
            Venue::Binance(gw) => gw.account_view().await,
            Venue::Variational(gw) => gw.account_view().await,
        }
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.instrument_rules().await,
            Venue::Hyperliquid(gw) => gw.instrument_rules().await,
            Venue::Lighter(gw) => gw.instrument_rules().await,
            Venue::Mexc(gw) => gw.instrument_rules().await,
            Venue::Binance(gw) => gw.instrument_rules().await,
            Venue::Variational(gw) => gw.instrument_rules().await,
        }
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.working_orders().await,
            Venue::Hyperliquid(gw) => gw.working_orders().await,
            Venue::Lighter(gw) => gw.working_orders().await,
            Venue::Mexc(gw) => gw.working_orders().await,
            Venue::Binance(gw) => gw.working_orders().await,
            Venue::Variational(gw) => gw.working_orders().await,
        }
    }

    async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.account_inventory().await,
            Venue::Hyperliquid(gw) => gw.account_inventory().await,
            Venue::Lighter(gw) => gw.account_inventory().await,
            Venue::Mexc(gw) => gw.account_inventory().await,
            Venue::Binance(gw) => gw.account_inventory().await,
            Venue::Variational(gw) => gw.account_inventory().await,
        }
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        match self {
            Venue::Bybit(gw) => gw.executions(start_ms, end_ms).await,
            Venue::Hyperliquid(gw) => gw.executions(start_ms, end_ms).await,
            Venue::Lighter(gw) => gw.executions(start_ms, end_ms).await,
            Venue::Mexc(gw) => gw.executions(start_ms, end_ms).await,
            Venue::Binance(gw) => gw.executions(start_ms, end_ms).await,
            Venue::Variational(gw) => gw.executions(start_ms, end_ms).await,
        }
    }
}

/// The chosen venue's private order stream, behind one type for the same
/// reason the gateway is: `async fn` in trait cannot be a trait object, and a
/// closed enum keeps every feed visible in one place.
pub enum OrderFeeds {
    Bybit(BybitOrderFeed),
    Hyperliquid(HyperliquidOrderFeed),
    /// Not a socket. Lighter's account channel names a fill by the venue's own
    /// order id and not by the client order index the engine minted, so a live
    /// fill cannot be attributed to the strategy that caused it. This feed
    /// paces the engine's own resync instead, and the fills arrive from the
    /// venue's execution history — see `venues/lighter/ws.rs`.
    Lighter(LighterOrderFeed),
    /// Not a socket either, and for a different reason from Lighter's: MEXC's
    /// private channel does carry the engine's own order id, but it is entered
    /// by a login frame that cannot be exercised anywhere except a funded
    /// account. See `venues/mexc/ws.rs`.
    Mexc(MexcOrderFeed),
    /// A real socket: the listen-key user-data stream, which the venue's
    /// testnet carries in full. See `venues/binance/ws.rs`.
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
        Ok(match name {
            VenueName::BybitDemo => {
                OrderFeeds::Bybit(BybitOrderFeed::new(VenueRealm::Demo, symbols)?)
            }
            VenueName::BybitMainnet => {
                OrderFeeds::Bybit(BybitOrderFeed::new(VenueRealm::Mainnet, symbols)?)
            }
            VenueName::HyperliquidTestnet => OrderFeeds::Hyperliquid(HyperliquidOrderFeed::new(
                HyperliquidRealm::Testnet,
                symbols,
            )?),
            VenueName::HyperliquidMainnet => OrderFeeds::Hyperliquid(HyperliquidOrderFeed::new(
                HyperliquidRealm::Mainnet,
                symbols,
            )?),
            VenueName::LighterTestnet => {
                OrderFeeds::Lighter(LighterOrderFeed::new(LighterRealm::Testnet)?)
            }
            VenueName::LighterMainnet => {
                OrderFeeds::Lighter(LighterOrderFeed::new(LighterRealm::Mainnet)?)
            }
            VenueName::MexcMainnet => OrderFeeds::Mexc(MexcOrderFeed::new(MexcRealm::Mainnet)?),
            VenueName::BinanceTestnet => {
                OrderFeeds::Binance(BinanceOrderFeed::new(BinanceRealm::Testnet, symbols)?)
            }
            VenueName::BinanceMainnet => {
                OrderFeeds::Binance(BinanceOrderFeed::new(BinanceRealm::Mainnet, symbols)?)
            }
            VenueName::VariationalMainnet => OrderFeeds::Silent,
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
            OrderFeeds::Bybit(feed) => OrderFeed::learn(feed, symbol, id),
            OrderFeeds::Hyperliquid(feed) => OrderFeed::learn(feed, symbol, id),
            OrderFeeds::Lighter(feed) => OrderFeed::learn(feed, symbol, id),
            OrderFeeds::Mexc(feed) => OrderFeed::learn(feed, symbol, id),
            OrderFeeds::Binance(feed) => OrderFeed::learn(feed, symbol, id),
            OrderFeeds::Silent => (),
        }
    }

    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        match self {
            OrderFeeds::Bybit(feed) => feed.next_update().await,
            OrderFeeds::Hyperliquid(feed) => feed.next_update().await,
            OrderFeeds::Lighter(feed) => feed.next_update().await,
            OrderFeeds::Mexc(feed) => feed.next_update().await,
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
        for venue in VenueName::ALL {
            match venue {
                VenueName::BybitDemo | VenueName::BybitMainnet => {
                    assert_eq!(venue.readiness(), VenueReadiness::LiveProven);
                    venue.require_engine_run_ready().unwrap();
                }
                VenueName::HyperliquidTestnet | VenueName::LighterTestnet => {
                    assert_eq!(venue.readiness(), VenueReadiness::TestnetCanary);
                    venue.require_engine_run_ready().unwrap();
                }
                VenueName::HyperliquidMainnet
                | VenueName::LighterMainnet
                | VenueName::MexcMainnet
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
}
