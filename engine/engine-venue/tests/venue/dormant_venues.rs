//! Cargo availability and live evidence are independent properties.
use engine_venue::{known_venues, OrderFeeds, Venue, VenueName, VenueReadiness};

#[test]
fn conformance_feature_matrix_matches_every_registered_realm() {
    for name in VenueName::ALL {
        let enabled = match name.venue() {
            "bybit" => cfg!(feature = "bybit"),
            "binance" => cfg!(feature = "binance"),
            "hyperliquid" => cfg!(feature = "hyperliquid"),
            "lighter" => cfg!(feature = "lighter"),
            "mexc" => cfg!(feature = "mexc"),
            "variational" => cfg!(feature = "variational"),
            other => panic!("no feature conformance entry for {other}"),
        };
        assert_eq!(name.compiled(), enabled, "{name}");
        assert_eq!(known_venues().contains(&name.as_str()), enabled, "{name}");
        assert_eq!(VenueName::parse(name.as_str()).is_ok(), enabled, "{name}");
        if !enabled {
            assert!(
                matches!(Venue::build(name, vec![]), Err(error) if error.to_string().contains("Cargo feature"))
            );
            assert!(
                matches!(OrderFeeds::build(name, vec![]), Err(error) if error.to_string().contains("Cargo feature"))
            );
            assert!(name.require_engine_run_ready().is_err());
        }
        assert_eq!(
            name.readiness() == VenueReadiness::LiveProven,
            matches!(name.venue(), "bybit" | "mexc")
        );
    }
}

#[test]
fn conformance_local_fixtures_do_not_promote_dormant_realms() {
    for name in VenueName::ALL.into_iter().filter(|name| name.compiled()) {
        match name.readiness() {
            VenueReadiness::LiveProven => assert!(matches!(name.venue(), "bybit" | "mexc")),
            VenueReadiness::TestnetCanary => {
                assert!(matches!(
                    name,
                    VenueName::HyperliquidTestnet | VenueName::LighterTestnet
                ));
                name.require_engine_run_ready().unwrap();
            }
            VenueReadiness::LiveCanary => {
                assert!(matches!(name, VenueName::HyperliquidMainnet));
                // Funded capital, and the operator canary is the only thing
                // this state opens.
                assert!(name.is_real_money());
                name.require_canary_ready().unwrap();
                assert!(name.require_engine_run_ready().is_err());
            }
            VenueReadiness::ProductionBlocked | VenueReadiness::ReadOnly => {
                assert!(name.require_engine_run_ready().is_err());
                assert!(name.require_canary_ready().is_err());
            }
        }
    }
}
