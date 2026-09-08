use crate::{
    BinanceRealm, HyperliquidRealm, LighterRealm, MexcRealm, VariationalRealm, VenueRealm,
};
use engine_types::VenueError;

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
pub fn known_venues() -> Vec<&'static str> {
    VenueName::ALL
        .into_iter()
        .filter(|name| name.compiled())
        .map(VenueName::as_str)
        .collect()
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
/// A compiled adapter is not production evidence. Moving a realm to
/// `LiveProven` is a reviewed source change made only after the smallest
/// permitted order and cancel/fill lifecycle has been observed on that exact
/// venue. `LiveCanary` is the state between: a funded realm that still owes
/// that lifecycle. A practice sibling's evidence is another chain and another
/// account and does not carry, so `engine canary-order` is permitted here with
/// `REAL_MONEY` armed while the execution engine stays refused.
#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash)]
pub enum VenueReadiness {
    LiveProven,
    /// Offline conformance is green; this practice realm exists to gather the
    /// missing live evidence with test funds.
    TestnetCanary,
    /// Offline conformance is green and the live evidence this funded realm
    /// owes has not been taken. `engine canary-order` may run here; `engine
    /// run` may not.
    LiveCanary,
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
            Self::LiveCanary => "live-canary",
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

    /// Availability in this binary; realm identities remain stable across builds.
    pub const fn compiled(self) -> bool {
        match self {
            Self::BybitDemo | Self::BybitMainnet => cfg!(feature = "bybit"),
            Self::BinanceTestnet | Self::BinanceMainnet => cfg!(feature = "binance"),
            Self::HyperliquidTestnet | Self::HyperliquidMainnet => cfg!(feature = "hyperliquid"),
            Self::LighterTestnet | Self::LighterMainnet => cfg!(feature = "lighter"),
            Self::MexcMainnet => cfg!(feature = "mexc"),
            Self::VariationalMainnet => cfg!(feature = "variational"),
        }
    }

    pub fn disabled_error(self) -> VenueError {
        VenueError::BadRequest(format!(
            "{} is not compiled; enable the {} Cargo feature",
            self.as_str(),
            self.venue()
        ))
    }

    /// Read one of the names above, refusing every fallback.
    ///
    /// An unknown name is refused rather than defaulted: a typo that quietly
    /// fell back to some other venue would be a strategy trading somewhere
    /// nobody chose.
    pub fn parse(name: &str) -> Result<Self, VenueError> {
        let name = name.trim();
        VenueName::ALL
            .into_iter()
            .find(|known| known.as_str() == name && known.compiled())
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
            VenueName::BybitDemo | VenueName::BybitMainnet => "bybit",
            VenueName::HyperliquidTestnet | VenueName::HyperliquidMainnet => "hyperliquid",
            VenueName::LighterTestnet | VenueName::LighterMainnet => "lighter",
            VenueName::MexcMainnet => "mexc",
            VenueName::BinanceTestnet | VenueName::BinanceMainnet => "binance",
            VenueName::VariationalMainnet => "variational",
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
    /// the private adapter arming check, at the credential read.
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
            // `mexc_mainnet`: one canary lifecycle observed on the funded
            // account on 2026-09-08 20:16 UTC, venue order 852400800159322624
            // (create, `New`, cancel, `Cancelled`, two clean scans).
            VenueName::BybitDemo | VenueName::BybitMainnet | VenueName::MexcMainnet => {
                VenueReadiness::LiveProven
            }
            VenueName::HyperliquidTestnet | VenueName::LighterTestnet => {
                VenueReadiness::TestnetCanary
            }
            // `hyperliquid_mainnet`: the missing evidence is one reviewed
            // `engine canary-order` lifecycle on the funded account. The
            // testnet realm's evidence is a different chain and a different
            // account, so it does not carry.
            VenueName::HyperliquidMainnet => VenueReadiness::LiveCanary,
            VenueName::LighterMainnet | VenueName::BinanceTestnet | VenueName::BinanceMainnet => {
                VenueReadiness::ProductionBlocked
            }
            VenueName::VariationalMainnet => VenueReadiness::ReadOnly,
        }
    }

    /// Refuse before the engine opens a log, credential, or socket when this
    /// realm has not earned the evidence its capital class needs.
    pub fn require_engine_run_ready(self) -> Result<(), VenueError> {
        if !self.compiled() {
            return Err(self.disabled_error());
        }
        let readiness = self.readiness();
        if readiness.permits_engine_run() {
            return Ok(());
        }
        Err(VenueError::BadRequest(match readiness {
            VenueReadiness::LiveCanary => format!(
                "{} readiness is {}; the missing evidence is one reviewed `engine canary-order` lifecycle on this exact realm, and the execution engine stays refused until that review moves it to live-proven",
                self.as_str(),
                readiness.as_str()
            ),
            other => format!(
                "{} readiness is {}; the execution engine is blocked until this exact realm has reviewed live order lifecycle evidence",
                self.as_str(),
                other.as_str()
            ),
        }))
    }

    /// Refuse the operator canary on any realm it is not the right instrument
    /// for: the practice account it was written against, and the funded realms
    /// whose only route to live evidence it is.
    pub fn require_canary_ready(self) -> Result<(), VenueError> {
        if !self.compiled() {
            return Err(self.disabled_error());
        }
        if self == VenueName::BybitDemo || self.readiness() == VenueReadiness::LiveCanary {
            return Ok(());
        }
        Err(VenueError::BadRequest(format!(
            "canary-order runs on {BYBIT_DEMO} and on live-canary realms; {} readiness is {}",
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
