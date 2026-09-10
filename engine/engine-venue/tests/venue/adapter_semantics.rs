//! Two machine checks on what an adapter says about itself.
//!
//! The capability row an operator reads and the `VenueCaps` the engine acts on
//! are two hand-written answers to one question, and only one of them refuses
//! an order. The fingerprint is the other half: a receipt in the row is
//! evidence about the adapter source it was taken against, and nothing else.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use engine_types::VenueGateway;
use engine_venue::{Capability, Evidence, VenueName};
use sha2::{Digest, Sha256};

use crate::support::TestServer;

/// The venue directories under `src/venues/`, and the realm whose fixture
/// `conformance::build` constructs that adapter with. Caps are the adapter's,
/// not the realm's — the realm only decides the endpoint.
const VENUES: [(&str, VenueName); 6] = [
    ("binance", VenueName::BinanceTestnet),
    ("bybit", VenueName::BybitDemo),
    ("hyperliquid", VenueName::HyperliquidTestnet),
    ("lighter", VenueName::LighterTestnet),
    ("mexc", VenueName::MexcMainnet),
    ("variational", VenueName::VariationalMainnet),
];

fn venue_dir(venue: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("src")
        .join("venues")
        .join(venue)
}

/// Every `.rs` file under `dir` that is not a test module, by path relative to
/// `dir` with `/` separators.
fn adapter_sources(dir: &Path) -> BTreeMap<String, PathBuf> {
    let mut found = BTreeMap::new();
    let mut pending = vec![dir.to_path_buf()];
    while let Some(current) = pending.pop() {
        let entries = std::fs::read_dir(&current).unwrap_or_else(|e| {
            panic!(
                "{} is adapter source and must be readable: {e}",
                current.display()
            )
        });
        for entry in entries {
            let path = entry.expect("a readable directory entry").path();
            if path.is_dir() {
                pending.push(path);
                continue;
            }
            let name = path
                .file_name()
                .and_then(|name| name.to_str())
                .expect("a utf-8 file name")
                .to_string();
            if !name.ends_with(".rs") || name == "tests.rs" || name.ends_with("_tests.rs") {
                continue;
            }
            let relative = path
                .strip_prefix(dir)
                .expect("a path under the venue directory")
                .components()
                .map(|part| part.as_os_str().to_str().expect("a utf-8 path component"))
                .collect::<Vec<_>>()
                .join("/");
            found.insert(relative, path);
        }
    }
    assert!(
        !found.is_empty(),
        "{} holds no adapter source",
        dir.display()
    );
    found
}

fn fingerprint(venue: &str) -> String {
    let dir = venue_dir(venue);
    let mut digest = Sha256::new();
    for (relative, path) in adapter_sources(&dir) {
        let bytes = std::fs::read(&path)
            .unwrap_or_else(|e| panic!("{} must be readable: {e}", path.display()));
        digest.update(relative.as_bytes());
        digest.update([0u8]);
        digest.update(&bytes);
        digest.update([0u8]);
    }
    hex::encode(digest.finalize())
}

/// The realms of one venue whose row holds a receipt that would have to be
/// reviewed if that adapter's execution semantics changed.
fn realms_with_current_receipts(venue: &str) -> Vec<&'static str> {
    VenueName::ALL
        .into_iter()
        .filter(|realm| realm.venue() == venue)
        .filter(|realm| {
            Capability::ALL
                .into_iter()
                .any(|capability| realm.capability(capability).qualifies())
        })
        .map(|realm| realm.as_str())
        .collect()
}

/// `VenueCaps` is what refuses an order; the capability row is what an
/// operator reads and what readiness derives from. `Unknown` in the row means
/// "the adapter does not do it", so it is the same claim as a false cap and a
/// test can hold the two together.
#[tokio::test]
async fn every_adapters_caps_agree_with_its_capability_row() {
    let server = TestServer::start(|_, _| (200, "{}".to_string())).await;
    let mut checked = 0;
    for realm in VenueName::ALL {
        assert!(
            VENUES.iter().any(|(venue, _)| *venue == realm.venue()),
            "{realm} has no adapter fixture in this test"
        );
    }
    for (venue, fixture) in VENUES {
        if !fixture.compiled() {
            continue;
        }
        let gateway = crate::conformance::build(fixture, &server);
        let caps = gateway.caps();
        for realm in VenueName::ALL
            .into_iter()
            .filter(|realm| realm.venue() == venue)
        {
            let known = |capability: Capability| realm.capability(capability) != Evidence::Unknown;
            assert_eq!(
                caps.amend_in_place,
                known(Capability::Amend),
                "{realm}: VenueCaps::amend_in_place and the amend row disagree"
            );
            assert_eq!(
                caps.native_position_stop,
                known(Capability::ProtectionPlace),
                "{realm}: VenueCaps::native_position_stop and the protection-place row disagree"
            );
            assert_eq!(
                caps.close_position_below_minimum,
                known(Capability::ReduceBelowMinimum),
                "{realm}: VenueCaps::close_position_below_minimum and the reduce-below-minimum row disagree"
            );
            checked += 1;
        }
    }
    assert_eq!(
        checked,
        VenueName::ALL
            .into_iter()
            .filter(|realm| realm.compiled())
            .count(),
        "a compiled realm's row went unchecked"
    );
}

/// Every venue's pin, whatever this build compiles: the source is on disk
/// either way, and a receipt going stale is not a per-feature question.
#[test]
fn every_adapters_source_matches_the_semantics_pin_its_receipts_were_taken_against() {
    for (venue, fixture) in VENUES {
        let actual = fingerprint(venue);
        let pinned = fixture.adapter_semantics_fingerprint();
        if actual == pinned {
            continue;
        }
        let holders = realms_with_current_receipts(venue);
        let holders = if holders.is_empty() {
            "none".to_string()
        } else {
            holders.join(", ")
        };
        panic!(
            "the {venue} adapter's source no longer matches its semantics pin.\n\
             pinned: {pinned}\n\
             actual: {actual}\n\
             realms holding a current receipt on this adapter: {holders}\n\
             if request encoding, order types, quantity conversion or fill interpretation changed, \
             set current: false on those receipts; otherwise update the pin."
        );
    }
}
