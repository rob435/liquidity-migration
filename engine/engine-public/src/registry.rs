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

/// The behaviours a venue either does or does not do, defined in
/// `engine-types` so a strategy can name what its own actions need.
pub use engine_types::Capability;

/// What is known about one realm doing one thing.
#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash)]
pub enum Evidence {
    /// The adapter does not do this, so there is nothing to observe.
    Unknown,
    /// The adapter does it and offline conformance covers it. The live venue
    /// has not been seen doing it on this realm.
    Implemented,
    /// The live venue was seen doing it on this realm.
    Observed {
        /// The receipt's own date, `YYYY-MM-DD`.
        on: &'static str,
        /// What the receipt says, and where to read it.
        receipt: &'static str,
        /// The commit the receipt is bound to: the one the receipt names, or
        /// the one that recorded it.
        adapter_commit: &'static str,
        /// False once this adapter's execution semantics — request encoding,
        /// order types, quantity conversion, fill interpretation — changed
        /// after the receipt was taken. A stale receipt is worth exactly
        /// [`Evidence::Implemented`] to readiness.
        current: bool,
    },
}

impl Evidence {
    /// Whether this cell qualifies its capability. A stale receipt does not.
    pub fn qualifies(self) -> bool {
        matches!(self, Self::Observed { current: true, .. })
    }
}

/// What an unattended engine holding protected positions has to have been seen
/// doing on the realm it is pointed at, before that realm is `live-proven`.
///
/// Submit and cancel put an order on and take it off. Fill attribution is what
/// charges a fill to the sleeve that owns it, including a venue stop fill with
/// no id of ours. Protection place and trigger are the stop existing and the
/// stop working. Reconnect/history recovery is the engine finding an execution
/// the private stream never handed it.
pub const UNATTENDED_PROTECTED_TRADING: &[Capability] = &[
    Capability::Submit,
    Capability::Cancel,
    Capability::FillAttribution,
    Capability::ProtectionPlace,
    Capability::ProtectionTrigger,
    Capability::ReconnectHistoryRecovery,
];

/// The readiness one capability row derives to.
///
/// `evidence` answers for one realm and `real_money` says whose capital it is.
/// Both are parameters, so the rule can be exercised on a row this registry
/// does not hold without inventing a realm to hold it.
fn derive_readiness(real_money: bool, evidence: impl Fn(Capability) -> Evidence) -> VenueReadiness {
    if UNATTENDED_PROTECTED_TRADING
        .iter()
        .all(|capability| evidence(*capability).qualifies())
    {
        VenueReadiness::LiveProven
    } else if real_money {
        VenueReadiness::LiveCanary
    } else {
        VenueReadiness::TestnetCanary
    }
}

/// Evidence state attached to every selectable venue realm.
///
/// Derived from the realm's capability row, never assigned: a compiled adapter
/// is not production evidence, and neither is a promotion note. `LiveProven`
/// means every capability in [`UNATTENDED_PROTECTED_TRADING`] carries a current
/// receipt from that exact realm. `LiveCanary` names a funded realm still
/// owed that evidence. Whether such a realm trades is the owner's call, made
/// in the realm table's posture and the credential file's `REAL_MONEY`; the
/// boot log names what is unproven. A practice sibling's evidence is another
/// chain and another account and does not carry.
#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash)]
pub enum VenueReadiness {
    LiveProven,
    /// Offline conformance is green; this practice realm exists to gather the
    /// missing live evidence with test funds.
    TestnetCanary,
    /// Offline conformance is green and the live evidence this funded realm
    /// owes has not been taken. `engine canary-order` runs here, and `engine
    /// run` is the owner's forward test: the realm gathers its own receipts.
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
        matches!(
            self,
            Self::LiveProven | Self::TestnetCanary | Self::LiveCanary
        )
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

    /// What this realm has been seen doing, one explicit row per realm.
    ///
    /// Kept beside the one exhaustive realm registry so a newly added venue
    /// cannot silently inherit somebody else's evidence. Every `Observed` cell
    /// names a dated receipt that exists in `CHANGELOG.md`, git history or
    /// `STATE.md`; where no such receipt exists the cell says `Implemented`,
    /// or `Unknown` where the adapter does not do the thing at all. The
    /// matrix is published in [`docs/engine.md`] §2 and a test compares the
    /// two.
    ///
    /// [`docs/engine.md`]: https://github.com/rob435/liquidity-migration/blob/main/docs/engine.md
    pub fn capability(self, capability: Capability) -> Evidence {
        use Capability as C;
        match self {
            Self::BybitDemo => match capability {
                C::Submit => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "order snapshots match cumulative fills and fees for all 109 demo engine requests in the 2026-09-06 USDT-linear window (CHANGELOG 2026-09-07)",
                    adapter_commit: "f1fbe34f",
                    current: true,
                },
                C::Cancel => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "the 16:15 UTC demo probe is admitted and cancelled without a fill; the cancel itself takes 9.049 ms (CHANGELOG 2026-09-06)",
                    adapter_commit: "af09aab5",
                    current: true,
                },
                C::PostOnly => Evidence::Observed {
                    on: "2026-09-04",
                    receipt: "`probe rested symbol=BTCUSDT px=77314.0 qty=0.001`, about 3% under the bid, for two seconds (CHANGELOG 2026-09-04)",
                    adapter_commit: "68c44d33",
                    current: true,
                },
                C::FillAttribution => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "LONG closes NEAR and ZEC at 16:05:32 UTC and the exact owned quantities match the fills; the day's 38 demo trade fills reconcile against authenticated Bybit records (CHANGELOG 2026-09-06, 2026-09-07)",
                    adapter_commit: "af09aab5",
                    current: true,
                },
                // No dated demo receipt: the one demo partial in the record is
                // an undated historical XCN request read back in the
                // 2026-09-06 window.
                C::PartialFill => Evidence::Implemented,
                // No dated demo receipt. Bybit offers no demo trade WebSocket,
                // so demo amends go over REST and none is recorded.
                C::Amend => Evidence::Implemented,
                C::ExactQuantity => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "the NEAR and ZEC closes send the canonical owned lot and the venue's fills match it exactly (CHANGELOG 2026-09-06)",
                    adapter_commit: "af09aab5",
                    current: true,
                },
                // No receipt on either Bybit realm: CHANGELOG 2026-08-30
                // records the condition on the funded account, not a
                // below-minimum close being sent.
                C::ReduceBelowMinimum => Evidence::Implemented,
                C::ProtectionPlace => Evidence::Observed {
                    on: "2026-09-09",
                    receipt: "six demo positions, each with its native stop (HEMI 0.007911, INJ 5.804, LINK 11.997, LTC 49.18, TAO 231.82, WLD 0.4044), read on the host 15:29-15:31 UTC (STATE.md)",
                    adapter_commit: "42dd7446",
                    current: true,
                },
                C::ProtectionChange => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "the demo LIT stop tightens to 3.694 on the venue (CHANGELOG 2026-09-06)",
                    adapter_commit: "16689a98",
                    current: true,
                },
                C::ProtectionTrigger => Evidence::Observed {
                    on: "2026-08-25",
                    receipt: "the demo ENA StopLoss executes at 21:08:19.719 UTC, selling 1,564 units for a 0.12135702 USDT fee (CHANGELOG 2026-09-06)",
                    adapter_commit: "2422be0d",
                    current: true,
                },
                C::ReconnectHistoryRecovery => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "the complete 2026-09-06 window reconciles across that day's 16:01:07 UTC demo restart - 38 trade fills and 109 order snapshots against authenticated venue records, nothing lost over the gap (CHANGELOG 2026-09-07)",
                    adapter_commit: "f1fbe34f",
                    current: true,
                },
                C::FundingFeeCash => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "28 demo funding executions match the authenticated transaction record for the 2026-09-06 window (CHANGELOG 2026-09-07)",
                    adapter_commit: "f1fbe34f",
                    current: true,
                },
            },
            Self::BybitMainnet => match capability {
                C::Submit => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "order snapshots match cumulative fills and fees for all 49 mainnet engine requests in the 2026-09-06 USDT-linear window (CHANGELOG 2026-09-07)",
                    adapter_commit: "f1fbe34f",
                    current: true,
                },
                C::Cancel => Evidence::Observed {
                    on: "2026-08-29",
                    receipt: "the funded quoting trial sent 256 placements, 237 amendments and 258 cancels through the authenticated socket, and disabling it cancelled the last resting quote (CHANGELOG 2026-08-29)",
                    adapter_commit: "7a32faf9",
                    current: true,
                },
                C::PostOnly => Evidence::Observed {
                    on: "2026-08-30",
                    receipt: "the funded quoting run produced 10 attributed fills, all maker, at 8.81 bp all-in arrival cost (CHANGELOG 2026-08-30)",
                    adapter_commit: "4e9ae39a",
                    current: true,
                },
                C::FillAttribution => Evidence::Observed {
                    on: "2026-09-08",
                    receipt: "all 54 trades in the 48 h window ending 07:47:54 UTC match authenticated Bybit execution ids, order ids, prices, quantities, fees, maker flags and timestamps (CHANGELOG 2026-09-08)",
                    adapter_commit: "46d93345",
                    current: true,
                },
                C::PartialFill => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "eight 930-unit CAP shorts at 11:45 UTC and then a 6510-unit reduction take 29 venue fills, all matching WAL records (CHANGELOG 2026-09-06)",
                    adapter_commit: "420c7347",
                    current: true,
                },
                C::Amend => Evidence::Observed {
                    on: "2026-08-29",
                    receipt: "237 amendments accepted through the authenticated socket; Bybit answers `order.amend` by saying it took the request and never by saying what price it left the order at (CHANGELOG 2026-08-29)",
                    adapter_commit: "7a32faf9",
                    current: true,
                },
                C::ExactQuantity => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "two distinct 0.01 funded ZEC executions, and an exact 930-unit CAP reduction, match native execution history (CHANGELOG 2026-09-06)",
                    adapter_commit: "af09aab5",
                    current: true,
                },
                // The condition was seen — the 2026-08-30 quoting run left 10
                // AGI the ordinary checks would not submit — but no
                // below-minimum close has been sent to the venue.
                C::ReduceBelowMinimum => Evidence::Implemented,
                C::ProtectionPlace => Evidence::Observed {
                    on: "2026-09-09",
                    receipt: "six mainnet positions, each with its native stop (HEMI 0.007907, INJ 5.805, LINK 11.971, LTC 49.14, TAO 232, WLD 0.4055), read on the host 15:29-15:31 UTC (STATE.md)",
                    adapter_commit: "42dd7446",
                    current: true,
                },
                C::ProtectionChange => Evidence::Observed {
                    on: "2026-09-06",
                    receipt: "the mainnet ZEC and LIT stops tighten to 792.43 and 3.697 on the venue (CHANGELOG 2026-09-06)",
                    adapter_commit: "16689a98",
                    current: true,
                },
                C::ProtectionTrigger => Evidence::Observed {
                    on: "2026-09-02",
                    receipt: "the funded HNTUSDT venue stop closed itself at 09:53 UTC for -14.97 USDT, -16.14 net of fees (CHANGELOG 2026-09-02, audited 2026-09-03)",
                    adapter_commit: "00341e32",
                    current: true,
                },
                C::ReconnectHistoryRecovery => Evidence::Observed {
                    on: "2026-09-02",
                    receipt: "that HNTUSDT stop filled while the engine was down and execution history put it in the rolling-loss window on boot eleven minutes later, latching the trip until 2026-09-03 09:54 UTC (CHANGELOG 2026-09-02)",
                    adapter_commit: "00341e32",
                    current: true,
                },
                C::FundingFeeCash => Evidence::Observed {
                    on: "2026-09-08",
                    receipt: "the funded account window carries 55 funding settlements and a 0.34536423 USDT net funding credit against authenticated transaction records (CHANGELOG 2026-09-08)",
                    adapter_commit: "46d93345",
                    current: true,
                },
            },
            // One canary lifecycle on the current adapter; the rest of the row
            // is what the gateway implements, never observed live.
            Self::MexcMainnet => match capability {
                C::Submit => Evidence::Observed {
                    on: "2026-09-10",
                    receipt: "canary PASS 11:59:17 UTC on the funded account: `create=accepted client_id=lmcan-1a08b2fd4a5-087e-0000 venue_order_id=853000482766018560`, `private_order=New` (CHANGELOG 2026-09-10)",
                    adapter_commit: "6e5ca627",
                    current: true,
                },
                C::Cancel => Evidence::Observed {
                    on: "2026-09-10",
                    receipt: "the same 11:59 UTC canary: `cancel=accepted`, `private_order=Cancelled`, `order_status=Cancelled cumulative_filled_qty=0`, four clean scans (CHANGELOG 2026-09-10)",
                    adapter_commit: "6e5ca627",
                    current: true,
                },
                C::PostOnly => Evidence::Observed {
                    on: "2026-09-10",
                    receipt: "the same 11:59 UTC canary rested post-only at 77462.1 under bid 77851.4 and was `New` with nothing filled (CHANGELOG 2026-09-10)",
                    adapter_commit: "6e5ca627",
                    current: true,
                },
                C::FillAttribution => Evidence::Implemented,
                C::PartialFill => Evidence::Implemented,
                // `VenueCaps::amend_in_place` is false and `amend_order`
                // refuses: MEXC has no amend for an ordinary order.
                C::Amend => Evidence::Unknown,
                C::ExactQuantity => Evidence::Implemented,
                // `VenueCaps::close_position_below_minimum` is false.
                C::ReduceBelowMinimum => Evidence::Unknown,
                C::ProtectionPlace => Evidence::Implemented,
                C::ProtectionChange => Evidence::Implemented,
                C::ProtectionTrigger => Evidence::Implemented,
                C::ReconnectHistoryRecovery => Evidence::Implemented,
                // The adapter reads no funding row at all.
                C::FundingFeeCash => Evidence::Unknown,
            },
            // One canary lifecycle on the funded address; the rest of the row
            // is what the gateway implements, never observed live.
            Self::HyperliquidMainnet => match capability {
                C::Submit => Evidence::Observed {
                    on: "2026-09-10",
                    receipt: "canary PASS 11:59:20 UTC on the funded address: `create=accepted client_id=lmcan-1a08b2fcd82-08b6-0000 venue_order_id=541177774027`, `private_order=New` (CHANGELOG 2026-09-10)",
                    adapter_commit: "6e5ca627",
                    current: true,
                },
                C::Cancel => Evidence::Observed {
                    on: "2026-09-10",
                    receipt: "the same 11:59 UTC canary: `cancel=accepted`, `private_order=Cancelled`, `order_status=Cancelled cumulative_filled_qty=0`, four clean scans (CHANGELOG 2026-09-10)",
                    adapter_commit: "6e5ca627",
                    current: true,
                },
                C::PostOnly => Evidence::Observed {
                    on: "2026-09-10",
                    receipt: "the same 11:59 UTC canary rested post-only at 77460 under bid 77850 and was `New` with nothing filled (CHANGELOG 2026-09-10)",
                    adapter_commit: "6e5ca627",
                    current: true,
                },
                C::Amend => Evidence::Implemented,
                C::FillAttribution | C::PartialFill | C::ExactQuantity => Evidence::Implemented,
                C::ReduceBelowMinimum => Evidence::Unknown,
                C::ProtectionPlace | C::ProtectionChange | C::ProtectionTrigger => {
                    Evidence::Implemented
                }
                C::ReconnectHistoryRecovery => Evidence::Implemented,
                C::FundingFeeCash => Evidence::Unknown,
            },
            // Nothing has been observed on the testnet: it is a different chain
            // and a different account, so the funded row does not carry to it.
            Self::HyperliquidTestnet => match capability {
                C::Amend | C::Submit | C::Cancel | C::PostOnly => Evidence::Implemented,
                C::FillAttribution | C::PartialFill | C::ExactQuantity => Evidence::Implemented,
                C::ReduceBelowMinimum => Evidence::Unknown,
                C::ProtectionPlace | C::ProtectionChange | C::ProtectionTrigger => {
                    Evidence::Implemented
                }
                C::ReconnectHistoryRecovery => Evidence::Implemented,
                C::FundingFeeCash => Evidence::Unknown,
            },
            Self::LighterMainnet | Self::LighterTestnet => match capability {
                C::Submit | C::Cancel | C::PostOnly => Evidence::Implemented,
                C::FillAttribution | C::PartialFill | C::ExactQuantity => Evidence::Implemented,
                // Nothing here sends a modify, and there is no below-minimum
                // close.
                C::Amend | C::ReduceBelowMinimum => Evidence::Unknown,
                C::ProtectionPlace | C::ProtectionChange | C::ProtectionTrigger => {
                    Evidence::Implemented
                }
                C::ReconnectHistoryRecovery => Evidence::Implemented,
                C::FundingFeeCash => Evidence::Unknown,
            },
            Self::BinanceMainnet | Self::BinanceTestnet => match capability {
                C::Submit | C::Cancel | C::PostOnly | C::Amend => Evidence::Implemented,
                C::FillAttribution | C::PartialFill | C::ExactQuantity => Evidence::Implemented,
                C::ReduceBelowMinimum => Evidence::Unknown,
                C::ProtectionPlace | C::ProtectionChange | C::ProtectionTrigger => {
                    Evidence::Implemented
                }
                // `executions` refuses: account trades need a symbol, and a
                // complete account-wide interval cannot be proved.
                C::ReconnectHistoryRecovery => Evidence::Unknown,
                C::FundingFeeCash => Evidence::Unknown,
            },
            // Every write refuses before HTTP; there is no trading API to
            // qualify.
            Self::VariationalMainnet => match capability {
                C::Submit
                | C::Cancel
                | C::PostOnly
                | C::FillAttribution
                | C::PartialFill
                | C::Amend
                | C::ExactQuantity
                | C::ReduceBelowMinimum
                | C::ProtectionPlace
                | C::ProtectionChange
                | C::ProtectionTrigger
                | C::ReconnectHistoryRecovery
                | C::FundingFeeCash => Evidence::Unknown,
            },
        }
    }

    /// What every `Observed` cell in the rows above was taken against: a
    /// sha256 over that venue's whole adapter source.
    ///
    /// `engine/engine-venue/tests/venue/adapter_semantics.rs` recomputes it
    /// from `engine-venue/src/venues/<venue>/` — every `.rs` file that is not
    /// a test module — and fails on drift, naming the realms of that venue
    /// whose rows hold a current receipt. Request encoding, order types,
    /// quantity conversion and fill interpretation all live in those files,
    /// so a change to any of them is a change to what a receipt means.
    ///
    /// `Evidence::Observed { current }` stays a hand flag rather than being
    /// derived from this pin: a comment edit under `venues/bybit/` would
    /// otherwise demote `bybit_mainnet` to `live-canary`. The pin's job is to
    /// make the review happen, not to make the verdict.
    pub const fn adapter_semantics_fingerprint(self) -> &'static str {
        match self {
            Self::BybitDemo | Self::BybitMainnet => Self::BYBIT_SEMANTICS,
            Self::BinanceTestnet | Self::BinanceMainnet => Self::BINANCE_SEMANTICS,
            Self::HyperliquidTestnet | Self::HyperliquidMainnet => Self::HYPERLIQUID_SEMANTICS,
            Self::LighterTestnet | Self::LighterMainnet => Self::LIGHTER_SEMANTICS,
            Self::MexcMainnet => Self::MEXC_SEMANTICS,
            Self::VariationalMainnet => Self::VARIATIONAL_SEMANTICS,
        }
    }

    /// `gateway.rs`, `private.rs`, `public.rs`, `realm.rs`, `mod.rs`: the v5
    /// linear order and trading-stop encoding, the amend by `orderLinkId`,
    /// the qty=0 whole-position close, and the execution stream both funded
    /// Bybit realms' receipts were read off.
    const BYBIT_SEMANTICS: &'static str =
        "4fa5f63f502924cddbf5f94ddddd1d37c6c9d69321fcf9a32cc82c3358f47988";
    /// The USDT-M futures REST and user-stream encoding.
    const BINANCE_SEMANTICS: &'static str =
        "2cc4bc87617eb2bee1ed2d185402552510304d75ecd7024de0e1ba6584044b02";
    /// The signed exchange actions (`order`, `batchModify`, `cancel`), the
    /// EIP-712 digest, the `cloid` scheme, and the fill and open-order reads
    /// the funded address's canary receipt was taken against.
    const HYPERLIQUID_SEMANTICS: &'static str =
        "1639dd4e14b45214db68450998e3f29460b63d80d738f8ff08fb752372c08929";
    /// The transaction encoding and the account/history resync, `crypto/`
    /// included.
    const LIGHTER_SEMANTICS: &'static str =
        "7c94f6fc2582c7620dfc7c21b7144db568be0818be9d6613f0f6fbd6b51633e1";
    /// The contract-API order encoding, the contract-count quantity
    /// conversion, the position-bound stop record, and the private login and
    /// `personal.filter` frames the funded account's canary receipt was taken
    /// against.
    const MEXC_SEMANTICS: &'static str =
        "4a74c2f60dfaba4f2872fcaf8349c75bba3cb7ec612f73f8e295c8c2cc441454";
    /// The public market reads, and the refusal every write returns.
    const VARIATIONAL_SEMANTICS: &'static str =
        "a79c459e38b6768d387978c3cbb5d85af56ab5674193ca5577352a473924ac2c";

    /// The [`UNATTENDED_PROTECTED_TRADING`] capabilities this realm holds no
    /// current receipt for. Empty is what `live-proven` means.
    pub fn unproven_capabilities(self) -> Vec<Capability> {
        UNATTENDED_PROTECTED_TRADING
            .iter()
            .copied()
            .filter(|capability| !self.capability(*capability).qualifies())
            .collect()
    }

    /// The two readiness states no capability row produces.
    ///
    /// `lighter_mainnet`, `binance_testnet` and `binance_mainnet`: outside the
    /// funded binary's default feature set, with no owner direction to trade
    /// either venue, so per-capability evidence is not what is missing and
    /// gathering it would not admit a run. `variational_mainnet`: the endpoint
    /// publishes market data and no trading API, so there is nothing to
    /// qualify.
    const fn readiness_override(self) -> Option<VenueReadiness> {
        match self {
            Self::LighterMainnet | Self::BinanceTestnet | Self::BinanceMainnet => {
                Some(VenueReadiness::ProductionBlocked)
            }
            Self::VariationalMainnet => Some(VenueReadiness::ReadOnly),
            Self::BybitDemo
            | Self::BybitMainnet
            | Self::HyperliquidTestnet
            | Self::HyperliquidMainnet
            | Self::LighterTestnet
            | Self::MexcMainnet => None,
        }
    }

    /// Production evidence, derived from this realm's capability row.
    pub fn readiness(self) -> VenueReadiness {
        match self.readiness_override() {
            Some(fixed) => fixed,
            None => derive_readiness(self.is_real_money(), |capability| {
                self.capability(capability)
            }),
        }
    }

    /// Refuse before the engine opens a log, credential, or socket when this
    /// realm is one the owner has not directed the engine at: a venue outside
    /// the funded binary's default set, or one with no trading API.
    pub fn require_engine_run_ready(self) -> Result<(), VenueError> {
        if !self.compiled() {
            return Err(self.disabled_error());
        }
        let readiness = self.readiness();
        if readiness.permits_engine_run() {
            return Ok(());
        }
        Err(VenueError::BadRequest(format!(
            "{} readiness is {}; the execution engine is blocked until this exact realm has reviewed live order lifecycle evidence",
            self.as_str(),
            readiness.as_str()
        )))
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

#[cfg(all(test, feature = "mexc"))]
mod audit_readiness_tests {
    use super::*;

    #[test]
    fn a_submit_cancel_canary_does_not_qualify_general_protected_position_trading() {
        // The row stays live-canary and names what it owes; the run itself is
        // the owner's forward test, so the boot gate admits it.
        let venue = VenueName::MexcMainnet;
        assert_eq!(venue.readiness(), VenueReadiness::LiveCanary);
        assert!(venue
            .unproven_capabilities()
            .contains(&Capability::ProtectionTrigger));
        venue.require_engine_run_ready().unwrap();
        assert!(venue.require_canary_ready().is_ok());
    }
}

#[cfg(test)]
mod capability_matrix_tests {
    use super::*;
    use std::fmt::Write as _;

    /// The realms the matrix is published for: the funded realms and the
    /// practice realms that gather their evidence.
    const PUBLISHED: [VenueName; 5] = [
        VenueName::BybitDemo,
        VenueName::BybitMainnet,
        VenueName::MexcMainnet,
        VenueName::HyperliquidMainnet,
        VenueName::HyperliquidTestnet,
    ];

    const BEGIN: &str = "<!-- BEGIN GENERATED capability-matrix -->";
    const END: &str = "<!-- END GENERATED capability-matrix -->";

    fn cell(evidence: Evidence) -> String {
        match evidence {
            Evidence::Unknown => "unknown".to_string(),
            Evidence::Implemented => "implemented".to_string(),
            Evidence::Observed {
                on, current: true, ..
            } => format!("observed {on}"),
            Evidence::Observed {
                on, current: false, ..
            } => format!("stale {on}"),
        }
    }

    /// The matrix as the markdown table `docs/engine.md` §2 carries between
    /// its generated-block markers. One writer for the code and the doc.
    fn matrix_markdown() -> String {
        let mut out = String::from("| Capability |");
        for realm in PUBLISHED {
            let _ = write!(out, " `{}` |", realm.as_str());
        }
        out.push_str("\n| :--- |");
        for _ in PUBLISHED {
            out.push_str(" :--- |");
        }
        out.push('\n');
        for capability in Capability::ALL {
            let _ = write!(out, "| `{}` |", capability.as_str());
            for realm in PUBLISHED {
                let _ = write!(out, " {} |", cell(realm.capability(capability)));
            }
            out.push('\n');
        }
        out
    }

    #[test]
    fn every_realm_derives_the_readiness_the_fleet_runs_on() {
        let expected = [
            (VenueName::BybitDemo, VenueReadiness::LiveProven),
            (VenueName::BybitMainnet, VenueReadiness::LiveProven),
            (VenueName::HyperliquidTestnet, VenueReadiness::TestnetCanary),
            (VenueName::HyperliquidMainnet, VenueReadiness::LiveCanary),
            (VenueName::LighterTestnet, VenueReadiness::TestnetCanary),
            (VenueName::LighterMainnet, VenueReadiness::ProductionBlocked),
            (VenueName::MexcMainnet, VenueReadiness::LiveCanary),
            (VenueName::BinanceTestnet, VenueReadiness::ProductionBlocked),
            (VenueName::BinanceMainnet, VenueReadiness::ProductionBlocked),
            (VenueName::VariationalMainnet, VenueReadiness::ReadOnly),
        ];
        assert_eq!(expected.len(), VenueName::ALL.len());
        for (realm, readiness) in expected {
            assert_eq!(realm.readiness(), readiness, "{realm}");
        }
    }

    #[test]
    fn the_matrix_answers_for_every_realm_and_capability_pair() {
        for realm in VenueName::ALL {
            for capability in Capability::ALL {
                if let Evidence::Observed {
                    on,
                    receipt,
                    adapter_commit,
                    ..
                } = realm.capability(capability)
                {
                    assert_eq!(on.len(), 10, "{realm} {capability:?} date {on}");
                    assert!(
                        on.split('-')
                            .all(|part| part.chars().all(|c| c.is_ascii_digit())),
                        "{realm} {capability:?} date {on}"
                    );
                    assert!(receipt.len() > 30, "{realm} {capability:?} {receipt}");
                    assert!(
                        adapter_commit.len() >= 7
                            && adapter_commit.chars().all(|c| c.is_ascii_hexdigit()),
                        "{realm} {capability:?} {adapter_commit}"
                    );
                }
            }
        }
    }

    #[test]
    fn the_alt_realms_owe_what_a_submit_cancel_canary_cannot_show() {
        let owed = vec![
            Capability::FillAttribution,
            Capability::ProtectionPlace,
            Capability::ProtectionTrigger,
            Capability::ReconnectHistoryRecovery,
        ];
        assert_eq!(VenueName::MexcMainnet.unproven_capabilities(), owed);
        assert_eq!(VenueName::HyperliquidMainnet.unproven_capabilities(), owed);
        assert!(VenueName::BybitDemo.unproven_capabilities().is_empty());
        assert!(VenueName::BybitMainnet.unproven_capabilities().is_empty());
    }

    #[test]
    fn a_receipt_taken_before_an_execution_semantics_change_does_not_qualify() {
        let stale = Evidence::Observed {
            on: "2026-09-08",
            receipt: "synthetic row under test, not a receipt",
            adapter_commit: "0000000",
            current: false,
        };
        assert!(!stale.qualifies());
        assert!(!Evidence::Implemented.qualifies());
        assert!(!Evidence::Unknown.qualifies());
        assert!(VenueName::MexcMainnet
            .capability(Capability::Submit)
            .qualifies());
    }

    #[test]
    fn mexcs_own_row_with_current_receipts_would_derive_live_proven() {
        // The same row, with the six unattended capabilities carrying a
        // current receipt instead of a stale or absent one.
        let promoted = |capability: Capability| {
            if UNATTENDED_PROTECTED_TRADING.contains(&capability) {
                Evidence::Observed {
                    on: "2026-09-09",
                    receipt: "synthetic row under test, not a receipt",
                    adapter_commit: "0000000",
                    current: true,
                }
            } else {
                VenueName::MexcMainnet.capability(capability)
            }
        };
        assert_eq!(derive_readiness(true, promoted), VenueReadiness::LiveProven);
        assert_eq!(
            derive_readiness(true, |capability| VenueName::MexcMainnet
                .capability(capability)),
            VenueReadiness::LiveCanary
        );
        // One cell back to stale is enough to hold it at live-canary.
        assert_eq!(
            derive_readiness(true, |capability| {
                if capability == Capability::ProtectionTrigger {
                    Evidence::Observed {
                        on: "2026-09-09",
                        receipt: "synthetic row under test, not a receipt",
                        adapter_commit: "0000000",
                        current: false,
                    }
                } else {
                    promoted(capability)
                }
            }),
            VenueReadiness::LiveCanary
        );
    }

    #[test]
    fn the_published_capability_matrix_matches_the_registry() {
        const DOC: &str = include_str!("../../../docs/engine.md");
        let start = DOC
            .find(BEGIN)
            .expect("docs/engine.md carries the capability-matrix begin marker")
            + BEGIN.len();
        let end = DOC[start..]
            .find(END)
            .expect("docs/engine.md carries the capability-matrix end marker")
            + start;
        let published = DOC[start..end].trim();
        let generated = matrix_markdown();
        let generated = generated.trim();
        if published != generated {
            let drift = published
                .lines()
                .zip(generated.lines())
                .find(|(doc, code)| doc.trim() != code.trim())
                .map_or_else(
                    || "line counts differ".to_string(),
                    |(doc, code)| format!("doc:  {doc}\ncode: {code}"),
                );
            panic!(
                "docs/engine.md §2 capability matrix is stale.\nfirst difference:\n{drift}\n\nregenerate the block as:\n{generated}"
            );
        }
    }
}
