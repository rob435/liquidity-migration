//! The venues that are compiled and not traded.
//!
//! Six venues are in this crate; one is traded. The other five are kept
//! options — the value is that picking one up is a config change rather than
//! a quarter of work, and the price is that nothing exercises them at an
//! order, so they rot silently. This file is what makes the rot loud.
//!
//! Three things are pinned. **Which realms are dormant**, as a list that must
//! cover `VenueName::ALL` together with the traded set, so a venue cannot be
//! added without a declared state. **What dormancy means at boot**, per
//! readiness class. **That the adapters still exist**: every dormant gateway,
//! private stream, and realm table is named below, so deleting one fails to
//! compile here rather than at somebody's order.
//!
//! `docs/engine.md` §2 is the same table in prose. Promoting a realm is a
//! source change in `VenueName::readiness`, and it fails this file until the
//! lists here are moved too.

use engine_venue::{
    BinanceGateway, BinanceOrderFeed, BinanceRealm, HyperliquidGateway, HyperliquidOrderFeed,
    HyperliquidRealm, LighterGateway, LighterOrderFeed, LighterRealm, MexcGateway, MexcOrderFeed,
    MexcRealm, VariationalGateway, VariationalRealm, VenueName, VenueReadiness, BINANCE_MAINNET,
    BINANCE_TESTNET, BYBIT_DEMO, BYBIT_MAINNET, HYPERLIQUID_MAINNET, HYPERLIQUID_TESTNET,
    LIGHTER_MAINNET, LIGHTER_TESTNET, MEXC_MAINNET, VARIATIONAL_MAINNET,
};

/// The realms an order has actually been through. Both are the same Bybit
/// adapter pointed at different accounts.
const TRADED: [VenueName; 2] = [VenueName::BybitDemo, VenueName::BybitMainnet];

/// Compiled, fenced, never traded.
const DORMANT: [VenueName; 8] = [
    VenueName::HyperliquidTestnet,
    VenueName::HyperliquidMainnet,
    VenueName::LighterTestnet,
    VenueName::LighterMainnet,
    VenueName::MexcMainnet,
    VenueName::BinanceTestnet,
    VenueName::BinanceMainnet,
    VenueName::VariationalMainnet,
];

#[test]
fn every_selectable_realm_is_either_traded_or_declared_dormant() {
    let mut declared: Vec<VenueName> = TRADED.into_iter().chain(DORMANT).collect();
    let count = declared.len();
    declared.sort_by_key(|venue| venue.as_str());
    declared.dedup();
    assert_eq!(declared.len(), count, "a realm is in both lists");

    let mut all: Vec<VenueName> = VenueName::ALL.to_vec();
    all.sort_by_key(|venue| venue.as_str());
    assert_eq!(
        declared, all,
        "VenueName::ALL and the traded/dormant lists disagree: a venue was added \
         or removed without declaring what it is"
    );
}

#[test]
fn the_traded_realms_are_the_only_live_proven_ones() {
    for venue in TRADED {
        assert_eq!(
            venue.readiness(),
            VenueReadiness::LiveProven,
            "{venue} is traded"
        );
        venue.require_engine_run_ready().unwrap();
    }
    for venue in DORMANT {
        assert_ne!(
            venue.readiness(),
            VenueReadiness::LiveProven,
            "{venue} is dormant: no order has been through it, so it cannot claim live evidence"
        );
    }
}

#[test]
fn a_dormant_realm_boots_only_as_a_named_testnet_canary() {
    for venue in DORMANT {
        let readiness = venue.readiness();
        match readiness {
            // Test funds on a real matching engine: the run is permitted
            // because that is how the missing live evidence gets gathered.
            VenueReadiness::TestnetCanary => {
                assert!(
                    venue.as_str().contains("testnet"),
                    "{venue} may run as a canary but is not spelled as a testnet"
                );
                venue.require_engine_run_ready().unwrap();
            }
            VenueReadiness::ProductionBlocked | VenueReadiness::ReadOnly => {
                let refusal = venue.require_engine_run_ready().unwrap_err().to_string();
                assert!(
                    refusal.contains(venue.as_str()) && refusal.contains(readiness.as_str()),
                    "the refusal must name the realm and why: {refusal}"
                );
            }
            VenueReadiness::LiveProven => unreachable!("checked above"),
        }
    }
}

#[test]
fn the_canaries_are_exactly_the_two_testnets_with_a_signed_order_path() {
    let canaries: Vec<&str> = DORMANT
        .into_iter()
        .filter(|venue| venue.readiness() == VenueReadiness::TestnetCanary)
        .map(VenueName::as_str)
        .collect();
    assert_eq!(canaries, vec![HYPERLIQUID_TESTNET, LIGHTER_TESTNET]);
}

#[test]
fn every_venue_name_has_a_constant_the_crate_exports() {
    let exported = [
        BYBIT_DEMO,
        BYBIT_MAINNET,
        HYPERLIQUID_TESTNET,
        HYPERLIQUID_MAINNET,
        LIGHTER_TESTNET,
        LIGHTER_MAINNET,
        MEXC_MAINNET,
        BINANCE_TESTNET,
        BINANCE_MAINNET,
        VARIATIONAL_MAINNET,
    ];
    let mut names: Vec<&str> = VenueName::ALL.iter().map(|venue| venue.as_str()).collect();
    names.sort_unstable();
    let mut constants = exported.to_vec();
    constants.sort_unstable();
    assert_eq!(
        names, constants,
        "an operator types these strings into engine.toml; every one is a named constant"
    );
    for name in exported {
        assert_eq!(VenueName::parse(name).unwrap().as_str(), name);
    }
}

#[test]
fn every_dormant_adapter_is_still_linked() {
    // Naming the types is the whole test: no gateway is constructed, because
    // building one wants credentials and a socket. Deleting an adapter, or
    // renaming one of its parts, fails to compile right here.
    let parts = [
        std::any::type_name::<HyperliquidGateway>(),
        std::any::type_name::<HyperliquidOrderFeed>(),
        std::any::type_name::<HyperliquidRealm>(),
        std::any::type_name::<LighterGateway>(),
        std::any::type_name::<LighterOrderFeed>(),
        std::any::type_name::<LighterRealm>(),
        std::any::type_name::<MexcGateway>(),
        std::any::type_name::<MexcOrderFeed>(),
        std::any::type_name::<MexcRealm>(),
        std::any::type_name::<BinanceGateway>(),
        std::any::type_name::<BinanceOrderFeed>(),
        std::any::type_name::<BinanceRealm>(),
        // Read-only: this venue's adapter has no private order stream, which
        // is why it can never leave `ReadOnly` as written.
        std::any::type_name::<VariationalGateway>(),
        std::any::type_name::<VariationalRealm>(),
    ];
    assert_eq!(parts.len(), 14);
    for part in parts {
        assert!(part.starts_with("engine_venue::"), "{part}");
    }
}

#[test]
fn each_dormant_venue_names_itself_the_way_the_lease_and_heartbeat_spell_it() {
    for venue in DORMANT {
        let (name, realm) = (venue.venue(), venue.realm());
        assert!(!name.is_empty() && !realm.is_empty(), "{venue}");
        assert!(
            venue.as_str().starts_with(name),
            "{venue}: the config name must lead with the venue it selects"
        );
        // The realm string names the lease file and travels in the
        // heartbeat, so two realms of one venue must not collide.
        assert!(
            realm.contains(name) || name == engine_venue::lease::VENUE_BYBIT,
            "{venue}: realm {realm} does not identify its venue"
        );
    }
}
