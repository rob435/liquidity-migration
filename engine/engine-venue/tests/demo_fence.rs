//! The demo fence, checked against the source itself.
//!
//! This crate may name two venue hosts and no others. The test reads every
//! source file back and fails on anything else, so a mainnet host cannot be
//! added — by hand or by accident — without turning the suite red. Config
//! picks a venue adapter by name, and this is why that is safe: the name
//! reaches only what is compiled in, and what is compiled in is scanned here.
//!
//! Every needle below is assembled from fragments at runtime, so this file
//! never contains a hostname of its own for the scan to trip over.

use std::path::{Path, PathBuf};

fn allowed_hosts() -> Vec<String> {
    vec![
        ["api-demo", ".bybit", ".com"].concat(),
        ["stream-demo", ".bybit", ".com"].concat(),
    ]
}

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
fn only_the_two_demo_hosts_appear_in_this_crate() {
    let allowed = allowed_hosts();
    for file in crate_sources() {
        let text = std::fs::read_to_string(&file).unwrap();
        for host in hosts_in(&text) {
            assert!(
                allowed.contains(&host),
                "{} names the venue host {host}, which is not a demo host",
                file.display()
            );
        }
    }
}

#[test]
fn the_mainnet_and_testnet_hosts_are_absent() {
    let forbidden = [
        ["api", ".bybit", ".com"].concat(),
        ["stream", ".bybit", ".com"].concat(),
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
fn the_shipped_constants_are_the_demo_ones() {
    let rest = ["https://", &["api-demo", ".bybit", ".com"].concat()].concat();
    let ws = [
        "wss://",
        &["stream-demo", ".bybit", ".com"].concat(),
        "/v5/private",
    ]
    .concat();
    assert_eq!(engine_venue::DEMO_REST_BASE, rest);
    assert_eq!(engine_venue::DEMO_PRIVATE_WS, ws);
}

#[test]
fn the_scanner_would_notice_a_mainnet_host() {
    // Proof the fence is not vacuous: the same scan over a line that does
    // carry a mainnet host finds it and rejects it.
    let planted = format!("const BASE: &str = \"https://{}\";", ["api", ".bybit", ".com"].concat());
    let found = hosts_in(&planted);
    assert_eq!(found.len(), 1);
    assert!(!allowed_hosts().contains(&found[0]), "{found:?}");
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
fn no_venue_name_sounds_like_real_money() {
    // The scanner above is the real fence: it reads the source back, and a
    // venue whose host is not a demo host cannot pass it whatever it is
    // called. This is the cheap second check — a name is what an operator
    // types into engine.toml, and one that reads like a real-money endpoint
    // would be a trap even with every host correct.
    for name in engine_venue::KNOWN_VENUES {
        let lower = name.to_ascii_lowercase();
        for word in ["main", "prod", "live"] {
            assert!(
                !lower.contains(word),
                "the venue name {name} contains \"{word}\", which reads like real money"
            );
        }
    }
}

#[test]
fn every_known_venue_name_reaches_its_own_adapter() {
    assert!(!engine_venue::KNOWN_VENUES.is_empty(), "no venue is selectable");
    for name in engine_venue::KNOWN_VENUES {
        // Credentials come from the environment, which a test box may or may
        // not have; either the venue is built or it stops at the credential
        // read. What must never happen is a listed name the constructor does
        // not know, which is what a forgotten match arm looks like.
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
fn credentials_come_from_the_demo_environment_variables() {
    assert_eq!(engine_venue::API_KEY_ENV, "BYBIT_DEMO_API_KEY");
    assert_eq!(engine_venue::API_SECRET_ENV, "BYBIT_DEMO_API_SECRET");
}
