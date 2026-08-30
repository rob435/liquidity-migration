//! The venue fence, checked against the source itself.
//!
//! This crate can reach funded accounts on more than one venue, so what stops
//! it reaching one by accident has to be more than care. The fence has two
//! halves.
//!
//! **The structural half, enforced here.** Every venue hostname the engine
//! knows may be named in exactly one file: that venue's own
//! `src/venues/<venue>/realm.rs`. That is what makes the realm the single
//! decision. It also closes the one door that opens as soon as a real-money
//! host exists in the source at all — each gateway's `for_test` takes a base
//! URL, and a test or a benchmark could hand it a real venue. None can,
//! because none can write the host down.
//!
//! The scan covers the market-data crate too. Public prices need a hostname
//! and no credential, so that crate is the easy place for a host to be
//! written outside any realm table; it reads them from the realm tables
//! instead, and this is what proves it.
//!
//! **The runtime half, enforced by `src/arming.rs` and `tests/arming_env.rs`.**
//! Reaching a real-money host needs `REAL_MONEY` armed by the owner in the
//! host's credential file, and a practice realm refuses to run while it is.
//!
//! Every needle below is assembled from fragments at runtime, so this file
//! never contains a hostname of its own for the scan to trip over.

use std::path::{Path, PathBuf};

/// One venue's hosts, and the one file allowed to write them down.
struct VenueHosts {
    /// The directory under `src/venues/` whose `realm.rs` owns these.
    venue: &'static str,
    /// Domain suffixes that belong to this venue. Any hostname ending in one
    /// of these is a venue host and is fenced.
    domains: Vec<String>,
    /// Every host the crate may name.
    allowed: Vec<String>,
}

fn venue_hosts() -> Vec<VenueHosts> {
    vec![
        VenueHosts {
            venue: "bybit",
            domains: vec![[".bybit", ".com"].concat()],
            allowed: vec![
                ["api-demo", ".bybit", ".com"].concat(),
                ["stream-demo", ".bybit", ".com"].concat(),
                ["api", ".bybit", ".com"].concat(),
                ["stream", ".bybit", ".com"].concat(),
            ],
        },
        VenueHosts {
            venue: "hyperliquid",
            domains: vec![
                [".hyperliquid", ".xyz"].concat(),
                [".hyperliquid-testnet", ".xyz"].concat(),
            ],
            allowed: vec![
                ["api", ".hyperliquid", ".xyz"].concat(),
                ["api", ".hyperliquid-testnet", ".xyz"].concat(),
            ],
        },
        VenueHosts {
            venue: "lighter",
            domains: vec![[".zklighter", ".elliot", ".ai"].concat()],
            allowed: vec![
                ["mainnet", ".zklighter", ".elliot", ".ai"].concat(),
                ["testnet", ".zklighter", ".elliot", ".ai"].concat(),
            ],
        },
        VenueHosts {
            venue: "mexc",
            domains: vec![[".mexc", ".com"].concat()],
            allowed: vec![
                // REST and the websocket are on different hosts here, and the
                // venue moved only the REST one. Both are declared; nothing
                // else on this domain is.
                ["api", ".mexc", ".com"].concat(),
                ["contract", ".mexc", ".com"].concat(),
            ],
        },
        VenueHosts {
            venue: "variational",
            domains: vec![[".variational", ".io"].concat()],
            allowed: vec![["omni-client-api.prod.ap-northeast-1", ".variational", ".io"].concat()],
        },
    ]
}

/// Every `.rs` file the fence reads: this crate, and the market-data crate
/// that serves the public side of the same venues.
fn scanned_sources() -> Vec<PathBuf> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let marketdata = root
        .parent()
        .expect("the crate sits inside the workspace")
        .join("engine-marketdata");
    let mut files = vec![root.join("Cargo.toml")];
    collect(&root.join("src"), &mut files);
    collect(&root.join("tests"), &mut files);
    collect(&marketdata.join("src"), &mut files);
    assert!(files.len() > 15, "the scan found almost nothing: {files:?}");
    assert!(
        files.iter().any(|f| f.starts_with(&marketdata)),
        "the market-data crate was not scanned, so a host written there would be invisible"
    );
    files
}

fn collect(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// Pull every host on one of these domains out of the text, label and all.
fn hosts_in(text: &str, domains: &[String]) -> Vec<String> {
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    for domain in domains {
        let mut from = 0;
        while let Some(at) = text[from..].find(domain.as_str()) {
            let start_of_domain = from + at;
            let mut label_start = start_of_domain;
            while label_start > 0 {
                let c = bytes[label_start - 1];
                if c.is_ascii_alphanumeric() || c == b'-' || c == b'.' {
                    label_start -= 1;
                } else {
                    break;
                }
            }
            found.push(text[label_start..start_of_domain + domain.len()].to_string());
            from = start_of_domain + domain.len();
        }
    }
    found
}

/// Whether this file is the realm table that owns the venue's hosts.
///
/// Anchored to `src/venues/`, not merely to a directory named after the
/// venue. Any crate here may grow a per-venue directory — the market-data
/// crate now has one — and without the anchor a `realm.rs` inside any of them
/// would become a second legal place to write that venue's hosts.
fn is_host_home(file: &Path, venue: &str) -> bool {
    let named =
        |path: Option<&Path>, want: &str| path.and_then(Path::file_name).is_some_and(|n| n == want);
    file.file_name().is_some_and(|n| n == "realm.rs")
        && named(file.parent(), venue)
        && named(file.parent().and_then(Path::parent), "venues")
}

#[test]
fn every_venue_host_in_the_scanned_crates_is_one_the_engine_declares() {
    for venue in venue_hosts() {
        for file in scanned_sources() {
            let text = std::fs::read_to_string(&file).unwrap();
            for host in hosts_in(&text, &venue.domains) {
                assert!(
                    venue.allowed.contains(&host),
                    "{} names the host {host}, which is not one {} declares",
                    file.display(),
                    venue.venue
                );
            }
        }
    }
}

#[test]
fn each_venues_hosts_are_written_down_only_in_its_own_realm_table() {
    // The heart of the fence. Every gateway's `for_test` takes a base URL, so
    // if any other file could spell a real venue host, a test or a benchmark
    // could point a gateway at a funded account. None can.
    for venue in venue_hosts() {
        let mut seen_at_home: Vec<String> = Vec::new();
        for file in scanned_sources() {
            let text = std::fs::read_to_string(&file).unwrap();
            let found = hosts_in(&text, &venue.domains);
            if is_host_home(&file, venue.venue) {
                seen_at_home.extend(found);
                continue;
            }
            assert!(
                found.is_empty(),
                "{} names {} host(s) {found:?}; only src/venues/{}/realm.rs may name them",
                file.display(),
                venue.venue,
                venue.venue
            );
        }
        for host in &venue.allowed {
            assert!(
                seen_at_home.contains(host),
                "{host} is declared for {} but its realm table never names it — either the \
                 table lost a host or this list has one the engine does not use",
                venue.venue
            );
        }
    }
}

#[test]
fn testnet_and_every_alternate_domain_are_absent() {
    // Bybit's testnet is not a realm here, and its regional domains are not
    // the venue this engine trades. Hyperliquid's testnet IS a realm, so its
    // host is in the allowed list above rather than this one.
    let forbidden = [
        ["api-testnet", ".bybit", ".com"].concat(),
        ["stream-testnet", ".bybit", ".com"].concat(),
        ["api", ".bytick", ".com"].concat(),
        ["api", ".bybit", ".nl"].concat(),
        ["api", ".byhkbit", ".com"].concat(),
        ["api", ".bybit-tr", ".com"].concat(),
        ["api", ".bybit", ".kz"].concat(),
        // MEXC's undocumented third futures host. It answers REST and the
        // websocket correctly and appears in no MEXC announcement, so it has
        // no support commitment at all — and it is easy to find empirically
        // and reach for.
        ["futures", ".mexc", ".com"].concat(),
    ];
    for file in scanned_sources() {
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
fn the_realm_tables_are_the_shipped_ones() {
    use engine_venue::{HyperliquidRealm, LighterRealm, MexcRealm, VariationalRealm, VenueRealm};

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
    let public_ws = [
        "wss://",
        &["stream", ".bybit", ".com"].concat(),
        "/v5/public/linear",
    ]
    .concat();
    assert_eq!(VenueRealm::Demo.rest_base(), demo_rest);
    assert_eq!(VenueRealm::Demo.private_ws(), demo_ws);
    assert_eq!(VenueRealm::Mainnet.rest_base(), main_rest);
    assert_eq!(VenueRealm::Mainnet.private_ws(), main_ws);
    assert_eq!(VenueRealm::Demo.public_ws(), public_ws);

    let hl_test = [
        "https://",
        &["api", ".hyperliquid-testnet", ".xyz"].concat(),
    ]
    .concat();
    let hl_main = ["https://", &["api", ".hyperliquid", ".xyz"].concat()].concat();
    assert_eq!(HyperliquidRealm::Testnet.rest_base(), hl_test);
    assert_eq!(HyperliquidRealm::Mainnet.rest_base(), hl_main);
    assert_eq!(
        HyperliquidRealm::Testnet.websocket(),
        [
            "wss://",
            &["api", ".hyperliquid-testnet", ".xyz"].concat(),
            "/ws"
        ]
        .concat()
    );
    assert_eq!(
        HyperliquidRealm::Mainnet.websocket(),
        ["wss://", &["api", ".hyperliquid", ".xyz"].concat(), "/ws"].concat()
    );

    let lighter_main = [
        "https://",
        &["mainnet", ".zklighter", ".elliot", ".ai"].concat(),
    ]
    .concat();
    let lighter_test = [
        "https://",
        &["testnet", ".zklighter", ".elliot", ".ai"].concat(),
    ]
    .concat();
    assert_eq!(LighterRealm::Mainnet.rest_base(), lighter_main);
    assert_eq!(LighterRealm::Testnet.rest_base(), lighter_test);
    assert_eq!(
        LighterRealm::Mainnet.websocket(),
        [&lighter_main.replace("https://", "wss://"), "/stream"].concat()
    );
    // The chain id is signed into every transaction, so it is as much a part
    // of "which network" as the host is.
    assert_eq!(LighterRealm::Mainnet.chain_id(), 304);
    assert_eq!(LighterRealm::Testnet.chain_id(), 300);

    // MEXC's two hosts are deliberately different: the January 2026 domain move
    // took the REST host and left the websocket behind. Pinned so a later
    // tidy-up that makes them match fails here rather than silently taking the
    // market feed down.
    let mexc_rest = ["https://", &["api", ".mexc", ".com"].concat()].concat();
    let mexc_ws = ["wss://", &["contract", ".mexc", ".com"].concat(), "/edge"].concat();
    assert_eq!(MexcRealm::Mainnet.rest_base(), mexc_rest);
    assert_eq!(MexcRealm::Mainnet.websocket(), mexc_ws);
    assert!(
        MexcRealm::Mainnet.is_real_money(),
        "MEXC has no practice realm"
    );

    assert_eq!(
        VariationalRealm::Mainnet.rest_base(),
        [
            "https://",
            &["omni-client-api.prod.ap-northeast-1", ".variational", ".io"].concat()
        ]
        .concat()
    );
}

#[test]
fn the_scanner_would_notice_an_unknown_host() {
    // Proof the fence is not vacuous: the same scan over a line that carries a
    // host outside the allowed set finds it and rejects it.
    for (planted_host, domains, allowed) in [
        (
            ["api-testnet", ".bybit", ".com"].concat(),
            vec![[".bybit", ".com"].concat()],
            venue_hosts()[0].allowed.clone(),
        ),
        (
            ["evil", ".hyperliquid", ".xyz"].concat(),
            vec![[".hyperliquid", ".xyz"].concat()],
            venue_hosts()[1].allowed.clone(),
        ),
    ] {
        let planted = format!("const BASE: &str = \"https://{planted_host}\";");
        let found = hosts_in(&planted, &domains);
        assert_eq!(found.len(), 1, "{found:?}");
        assert!(!allowed.contains(&found[0]), "{found:?}");
    }
}

#[test]
fn the_one_file_rule_would_notice_a_host_in_another_file() {
    // The other half of non-vacuity: the scanner really does see a host when
    // one is planted, so the fence passing above means absence, not blindness.
    for (host, domains) in [
        (
            ["api", ".bybit", ".com"].concat(),
            vec![[".bybit", ".com"].concat()],
        ),
        (
            ["api", ".hyperliquid", ".xyz"].concat(),
            vec![[".hyperliquid", ".xyz"].concat()],
        ),
        (
            ["omni-client-api.prod.ap-northeast-1", ".variational", ".io"].concat(),
            vec![[".variational", ".io"].concat()],
        ),
    ] {
        let planted = format!("\"https://{host}\"");
        let found = hosts_in(&planted, &domains);
        assert_eq!(
            found.len(),
            1,
            "the planted host {host} was not seen: {found:?}"
        );
        assert_eq!(found[0], host);
    }
}

#[test]
fn every_venue_the_crate_declares_has_a_realm_table_the_fence_reads() {
    // The scan is only a fence if it reads every venue. A new adapter arrives
    // as a directory under src/venues/ with a realm.rs in it; if one appears
    // that `venue_hosts` above does not list, its hosts would be unfenced.
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let venues = root.join("src/venues");
    let declared: Vec<String> = std::fs::read_dir(&venues)
        .expect("src/venues must exist")
        .flatten()
        .filter(|entry| entry.path().is_dir())
        .filter(|entry| entry.path().join("realm.rs").exists())
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .collect();
    assert!(declared.len() >= 4, "only found {declared:?}");
    let fenced: Vec<&str> = venue_hosts().iter().map(|v| v.venue).collect();
    for venue in &declared {
        assert!(
            fenced.contains(&venue.as_str()),
            "src/venues/{venue}/realm.rs exists but the fence does not know about {venue}, \
             so whatever hosts it names are unfenced"
        );
    }
}

#[test]
fn every_real_money_venue_name_says_that_it_is_real_money() {
    // The string an operator types into engine.toml must not be quietly
    // mistakable for a practice one, and vice versa.
    use engine_venue::{known_venues, VenueName};
    assert!(!known_venues().is_empty(), "no venue is selectable");
    for name in known_venues() {
        let parsed = VenueName::parse(name).expect(name);
        assert_eq!(parsed.is_real_money(), name.contains("mainnet"), "{name}");
    }
    assert!(engine_venue::BYBIT_MAINNET.contains("mainnet"));
    assert!(!engine_venue::BYBIT_DEMO.contains("main"));
}

#[test]
fn every_known_venue_name_reaches_its_own_adapter() {
    use engine_venue::{known_venues, Venue, VenueName};
    for name in known_venues() {
        // Credentials come from the environment, which a test box may or may
        // not have; either the venue is built or it stops at the credential
        // read — and for a real-money realm, at the arming check, which is
        // also a Credentials error. What must never happen is a listed name
        // the constructor does not know, which is what a forgotten match arm
        // looks like.
        let chosen = VenueName::parse(name).unwrap();
        match Venue::by_name(name, vec!["BTCUSDT".to_string()]) {
            Ok(venue) => assert_eq!(
                venue.name(),
                chosen,
                "{name} built a venue that calls itself something else"
            ),
            // As above: the refusal has to name this realm's own variable, or
            // it is not evidence that this name reached this adapter.
            Err(engine_types::VenueError::Credentials(why)) => {
                let (key, secret) = chosen.credential_vars();
                // The arming refusal names the realm, and it must be THIS
                // realm. Accepting the bare word "REAL_MONEY" accepted every
                // venue's refusal, so on an unarmed box — every box this runs
                // on — the four funded names proved nothing. The prefix is
                // load-bearing: bybit_mainnet's realm is the bare "mainnet",
                // which is a substring of the other three.
                let mine = format!("the {} realm", chosen.realm());
                assert!(
                    why.contains(key) || why.contains(secret) || why.contains(&mine),
                    "{name} stopped on something that is not its own credential: {why}"
                );
            }
            Err(other) => panic!("{name} is listed as known but does not build: {other}"),
        }
    }
}

#[test]
fn every_known_venue_name_reaches_its_own_private_stream() {
    // The other half of the switch. A name whose feed constructor was
    // forgotten would be an engine that sends orders and never hears what
    // happened to them.
    use engine_venue::{known_venues, OrderFeeds, VenueName};
    for name in known_venues() {
        let chosen = VenueName::parse(name).unwrap();
        match OrderFeeds::build(chosen, vec!["BTCUSDT".to_string()]) {
            Ok(_) => {}
            // A box with no venue credentials is every box this runs on, so
            // "it stopped at the credential read" on its own says nothing
            // about which stream was built. What says it is WHICH variable it
            // stopped on: that is the chosen realm's own, and no other's.
            Err(engine_types::VenueError::Credentials(why)) => {
                let (key, secret) = chosen.credential_vars();
                // The arming refusal names the realm, and it must be THIS
                // realm. Accepting the bare word "REAL_MONEY" accepted every
                // venue's refusal, so on an unarmed box — every box this runs
                // on — the four funded names proved nothing. The prefix is
                // load-bearing: bybit_mainnet's realm is the bare "mainnet",
                // which is a substring of the other three.
                let mine = format!("the {} realm", chosen.realm());
                assert!(
                    why.contains(key) || why.contains(secret) || why.contains(&mine),
                    "{name} stopped on something that is not its own credential: {why}"
                );
            }
            Err(other) => panic!("{name} has no private stream: {other}"),
        }
    }
}

#[test]
fn the_demo_credential_variables_are_still_the_demo_ones() {
    assert_eq!(engine_venue::API_KEY_ENV, "BYBIT_DEMO_API_KEY");
    assert_eq!(engine_venue::API_SECRET_ENV, "BYBIT_DEMO_API_SECRET");
}

#[test]
fn a_credential_refusal_never_quotes_the_value_it_refused() {
    // Every venue's two variables sit in one file and all of them hold a hex
    // blob, so pasting one into the other's slot is the likely mistake. A
    // refusal that quoted the value back would then write a private key into
    // stderr and the system journal, where it outlives the process.
    use engine_venue::{HyperliquidRealm, LighterRealm};
    let secret = "b21a86da73de9fd10146bff211c12999db2dfe8f51f3dcfdc2d0a7d31ae278b3d1d45ae76717044e";
    for realm in [HyperliquidRealm::Testnet, HyperliquidRealm::Mainnet] {
        let creds = realm.credentials_for_test(secret, secret);
        let refused =
            engine_venue::HyperliquidGateway::for_test("http://127.0.0.1:1", realm, creds, vec![])
                .map(|_| ())
                .unwrap_err()
                .to_string();
        assert!(
            !refused.contains(secret),
            "a refusal quoted the key: {refused}"
        );
    }
    for realm in [LighterRealm::Testnet, LighterRealm::Mainnet] {
        let creds = realm.credentials_for_test(secret, secret);
        let refused =
            engine_venue::LighterGateway::for_test("http://127.0.0.1:1", realm, creds, vec![])
                .map(|_| ())
                .unwrap_err()
                .to_string();
        assert!(
            !refused.contains(secret),
            "a refusal quoted the key: {refused}"
        );
    }
}

#[test]
fn no_two_realms_anywhere_share_a_credential_variable() {
    // What makes "a key for one account cannot authenticate another" true of
    // the environment and not merely of the code that reads it — across
    // venues as well as within one. These are also the names the systemd
    // units unset, so a rename here without a rename there would silently
    // stop that from doing anything.
    //
    // Walked from the venue list rather than re-typed, because a second list is what a
    // new venue falls out of without anyone noticing.
    use engine_venue::{known_venues, VenueName};
    let mut all: Vec<(&str, &str)> = Vec::new();
    for name in known_venues() {
        let (key, secret) = VenueName::parse(name).unwrap().credential_vars();
        assert_ne!(key, secret, "{name} reads one variable for both halves");
        all.push((name, key));
        all.push((name, secret));
    }
    assert_eq!(all.len(), known_venues().len() * 2);
    for (i, (name, var)) in all.iter().enumerate() {
        for (other_name, other) in &all[i + 1..] {
            assert_ne!(
                var, other,
                "{name} and {other_name} both read {var}, so a key left on a host for one \
                 would authenticate the other"
            );
        }
    }

    // Bybit's four are the Python fleet's, and the fleet still reads them.
    use engine_venue::VenueRealm;
    assert_eq!(
        VenueRealm::Demo.credential_vars(),
        ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET")
    );
    assert_eq!(
        VenueRealm::Mainnet.credential_vars(),
        ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET")
    );
}
