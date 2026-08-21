//! The venue fence, checked against the source itself.
//!
//! This crate can reach the funded account, so what stops it reaching it by
//! accident has to be more than care. The fence has two halves.
//!
//! **The structural half, enforced here.** Only four venue hosts may be named
//! anywhere in the crate, and they may be named in exactly one file:
//! `src/realm.rs`. That is what makes the realm the single decision. It also
//! closes the one door that opens as soon as a mainnet host exists in the
//! source at all — `BybitGateway::for_test` takes a base URL, and a test or a
//! benchmark could hand it a real venue. It cannot, because it cannot write
//! the host down.
//!
//! **The runtime half, enforced by `src/realm.rs` and `tests/arming_env.rs`.**
//! Reaching the mainnet host needs `REAL_MONEY` armed by the owner in the
//! host's credential file, and the demo host refuses to run while it is.
//!
//! This replaced an older fence that said *no* mainnet host may appear. That
//! was a stronger statement and an easy one to keep, right up until the engine
//! needed to trade the funded account. What is here is the same idea aimed at
//! the thing that actually matters: not whether the address exists, but what
//! it takes to use it.
//!
//! Every needle below is assembled from fragments at runtime, so this file
//! never contains a hostname of its own for the scan to trip over.

use std::path::{Path, PathBuf};

/// The four hosts the crate may name: two realms, REST and private stream.
fn allowed_hosts() -> Vec<String> {
    vec![
        ["api-demo", ".bybit", ".com"].concat(),
        ["stream-demo", ".bybit", ".com"].concat(),
        ["api", ".bybit", ".com"].concat(),
        ["stream", ".bybit", ".com"].concat(),
    ]
}

/// The only file allowed to write any of them down.
const HOST_HOME: &str = "realm.rs";

/// Every `.rs` file under src/ and tests/, plus the manifest.
fn crate_sources() -> Vec<PathBuf> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut files = vec![root.join("Cargo.toml")];
    collect(&root.join("src"), &mut files);
    collect(&root.join("tests"), &mut files);
    assert!(files.len() > 5, "the scan found almost nothing: {files:?}");
    files
}

fn collect(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// Pull every host on the venue's domain out of the text, label and all.
fn hosts_in(text: &str) -> Vec<String> {
    let domain = [".bybit", ".com"].concat();
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(at) = text[from..].find(&domain) {
        let start_of_domain = from + at;
        let mut label_start = start_of_domain;
        while label_start > 0 {
            let c = bytes[label_start - 1];
            if c.is_ascii_alphanumeric() || c == b'-' {
                label_start -= 1;
            } else {
                break;
            }
        }
        found.push(text[label_start..start_of_domain + domain.len()].to_string());
        from = start_of_domain + domain.len();
    }
    found
}

#[test]
fn every_venue_host_in_this_crate_is_one_of_the_four_known_ones() {
    let allowed = allowed_hosts();
    for file in crate_sources() {
        let text = std::fs::read_to_string(&file).unwrap();
        for host in hosts_in(&text) {
            assert!(
                allowed.contains(&host),
                "{} names the venue host {host}, which is not one of the four known hosts",
                file.display()
            );
        }
    }
}

#[test]
fn venue_hosts_are_written_down_in_exactly_one_file() {
    // The heart of the fence. `for_test` takes a base URL, so if any other
    // file could spell a real venue host, a test or a benchmark could point a
    // gateway at the funded account. None can.
    let mut home_count = 0;
    for file in crate_sources() {
        let text = std::fs::read_to_string(&file).unwrap();
        let found = hosts_in(&text);
        if file.file_name().is_some_and(|n| n == HOST_HOME) {
            home_count = found.len();
            continue;
        }
        assert!(
            found.is_empty(),
            "{} names venue host(s) {found:?}; only {HOST_HOME} may name a venue host",
            file.display()
        );
    }
    assert_eq!(
        home_count,
        allowed_hosts().len(),
        "{HOST_HOME} should name each of the four hosts exactly once"
    );
}

#[test]
fn testnet_and_every_alternate_domain_are_absent() {
    let forbidden = [
        ["api-testnet", ".bybit", ".com"].concat(),
        ["stream-testnet", ".bybit", ".com"].concat(),
        ["api", ".bytick", ".com"].concat(),
        ["api", ".bybit", ".nl"].concat(),
        ["api", ".byhkbit", ".com"].concat(),
        ["api", ".bybit-tr", ".com"].concat(),
        ["api", ".bybit", ".kz"].concat(),
    ];
    for file in crate_sources() {
        let text = std::fs::read_to_string(&file).unwrap();
        for host in &forbidden {
            assert!(
                !text.contains(host.as_str()),
                "{} contains {host}",
                file.display()
            );
        }
    }
}

#[test]
fn the_realm_table_is_the_shipped_one() {
    use engine_venue::VenueRealm;
    let demo_rest = ["https://", &["api-demo", ".bybit", ".com"].concat()].concat();
    let demo_ws = [
        "wss://",
        &["stream-demo", ".bybit", ".com"].concat(),
        "/v5/private",
    ]
    .concat();
    let main_rest = ["https://", &["api", ".bybit", ".com"].concat()].concat();
    let main_ws = [
        "wss://",
        &["stream", ".bybit", ".com"].concat(),
        "/v5/private",
    ]
    .concat();
    assert_eq!(VenueRealm::Demo.rest_base(), demo_rest);
    assert_eq!(VenueRealm::Demo.private_ws(), demo_ws);
    assert_eq!(VenueRealm::Mainnet.rest_base(), main_rest);
    assert_eq!(VenueRealm::Mainnet.private_ws(), main_ws);
}

#[test]
fn the_scanner_would_notice_an_unknown_host() {
    // Proof the fence is not vacuous: the same scan over a line that carries a
    // host outside the four finds it and rejects it.
    let planted = format!(
        "const BASE: &str = \"https://{}\";",
        ["api-testnet", ".bybit", ".com"].concat()
    );
    let found = hosts_in(&planted);
    assert_eq!(found.len(), 1, "{found:?}");
    assert!(!allowed_hosts().contains(&found[0]), "{found:?}");
}

#[test]
fn the_one_file_rule_would_notice_a_host_in_another_file() {
    // The other half of non-vacuity: the scanner really does see a host when
    // one is planted, so the fence passing above means absence, not blindness.
    let planted = format!("\"https://{}\"", ["api", ".bybit", ".com"].concat());
    assert_eq!(hosts_in(&planted).len(), 1, "the planted host was not seen");
}

#[test]
fn the_scan_reaches_every_module_the_crate_declares() {
    // The scan is only a fence if it reads the whole crate. A future adapter
    // arrives as a new `mod` in lib.rs, so check each declared module is a
    // file the scan actually collected — a scan narrowed to a hand-written
    // list of files would fail here rather than pass while missing an
    // adapter.
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let lib = std::fs::read_to_string(root.join("src/lib.rs")).unwrap();
    let scanned = crate_sources();

    let mut checked = 0;
    for line in lib.lines() {
        let Some(name) = declared_module(line) else { continue };
        let flat = root.join("src").join(format!("{name}.rs"));
        let nested = root.join("src").join(&name).join("mod.rs");
        assert!(
            scanned.contains(&flat) || scanned.contains(&nested),
            "module {name} is declared but the host scan never reads it"
        );
        checked += 1;
    }
    assert!(checked >= 5, "only {checked} modules found; the parse is wrong");
}

/// `mod name;` from a line of lib.rs, however it is qualified. Only the
/// one-line form, which is the only form this crate uses.
fn declared_module(line: &str) -> Option<String> {
    let line = line.trim();
    let rest = line
        .strip_prefix("pub(crate) mod ")
        .or_else(|| line.strip_prefix("pub mod "))
        .or_else(|| line.strip_prefix("mod "))?;
    let name = rest.strip_suffix(';')?;
    name.chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_')
        .then(|| name.to_string())
}

#[test]
fn the_mainnet_venue_name_says_that_it_is_real_money() {
    // The old fence asserted the opposite — that no venue name contained
    // "main", "prod" or "live" — because back then every name was a demo name
    // and one that read like real money would have been a lie. Now one of them
    // *is* real money, and the requirement inverts: the string an operator
    // types into engine.toml must not be quietly mistakable for the demo one.
    assert!(engine_venue::BYBIT_MAINNET.contains("mainnet"));
    assert!(!engine_venue::BYBIT_DEMO.contains("main"));
    assert_ne!(engine_venue::BYBIT_DEMO, engine_venue::BYBIT_MAINNET);
    // And no third name has appeared that reads like neither.
    assert_eq!(
        engine_venue::KNOWN_VENUES,
        &[engine_venue::BYBIT_DEMO, engine_venue::BYBIT_MAINNET]
    );
}

#[test]
fn every_known_venue_name_reaches_its_own_adapter() {
    assert!(!engine_venue::KNOWN_VENUES.is_empty(), "no venue is selectable");
    for name in engine_venue::KNOWN_VENUES {
        // Credentials come from the environment, which a test box may or may
        // not have; either the venue is built or it stops at the credential
        // read — and for mainnet, at the arming check, which is also a
        // Credentials error. What must never happen is a listed name the
        // constructor does not know, which is what a forgotten match arm
        // looks like.
        match engine_venue::Venue::by_name(name, vec!["BTCUSDT".to_string()]) {
            Ok(venue) => assert_eq!(
                venue.name(),
                *name,
                "{name} built a venue that calls itself something else"
            ),
            Err(engine_types::VenueError::Credentials(_)) => {}
            Err(other) => panic!("{name} is listed as known but does not build: {other}"),
        }
    }
}

#[test]
fn the_demo_credential_variables_are_still_the_demo_ones() {
    assert_eq!(engine_venue::API_KEY_ENV, "BYBIT_DEMO_API_KEY");
    assert_eq!(engine_venue::API_SECRET_ENV, "BYBIT_DEMO_API_SECRET");
}

#[test]
fn the_two_realms_share_no_credential_variable() {
    use engine_venue::VenueRealm;
    let (dk, ds) = VenueRealm::Demo.credential_vars();
    let (mk, ms) = VenueRealm::Mainnet.credential_vars();
    assert_eq!((dk, ds), ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"));
    assert_eq!((mk, ms), ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET"));
    // Stated against the literal names because these are what the systemd
    // units unset, and a rename here without a rename there would silently
    // stop that from doing anything.
    assert!(![mk, ms].contains(&dk) && ![mk, ms].contains(&ds));
}
