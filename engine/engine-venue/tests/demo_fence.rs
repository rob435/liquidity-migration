//! The demo fence, checked against the source itself.
//!
//! This crate may name two venue hosts and no others. The test reads every
//! source file back and fails on anything else, so a mainnet host cannot be
//! added — by hand or by accident — without turning the suite red.
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
fn credentials_come_from_the_demo_environment_variables() {
    assert_eq!(engine_venue::API_KEY_ENV, "BYBIT_DEMO_API_KEY");
    assert_eq!(engine_venue::API_SECRET_ENV, "BYBIT_DEMO_API_SECRET");
}
