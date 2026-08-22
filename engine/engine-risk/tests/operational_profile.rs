//! The engine reads the fleet's own profile documents.
//!
//! These load `configs/operational.mainnet.json` and
//! `configs/operational.demo.json` from the repository — the real files, the
//! ones the Python fleet installs — rather than a copy. A cap that changes in
//! the file changes here, and if a change makes the profile unloadable this
//! suite says so before a deploy does.

use std::path::PathBuf;

use engine_risk::{kernel_config_from_profile, ProfileInputs};

mod common;
use common::{DISASTER_STOP_FRACTION, MAX_VIEW_AGE_NS};

fn inputs() -> ProfileInputs {
    ProfileInputs {
        disaster_stop_fraction: DISASTER_STOP_FRACTION,
        max_account_view_age_ns: MAX_VIEW_AGE_NS,
    }
}

fn repo_config(name: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../configs")
        .join(name);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("cannot read {}: {err}", path.display()))
}

#[test]
fn the_committed_mainnet_profile_loads_and_says_what_the_file_says() {
    let cfg = kernel_config_from_profile(&repo_config("operational.mainnet.json"), &inputs())
        .expect("the shipped mainnet profile must load");

    // Every assertion below is the literal number in the file. If the owner
    // changes a cap, this test is where the engine finds out.
    // 2026-08-21: a static document — entry leverage 5x, gross cap = what the
    // reference funds at that leverage, multipliers living in the env dials.
    assert_eq!(cfg.envelope.reference_usdt, 100.0);
    assert_eq!(cfg.envelope.max_component_gross_notional_usdt, 500.0);
    assert_eq!(cfg.envelope.max_initial_margin_usdt, 100.0);
    assert_eq!(cfg.leverage, 5.0);
    assert_eq!(cfg.qty_tolerance, 1e-12);

    // 500 of gross against a 100 reference: exactly what leverage 5 funds.
    assert_eq!(cfg.envelope.gross_notional_multiple, 5.0);
    assert_eq!(cfg.envelope.account_gross_cap_usdt(), 500.0);

    // The funded account's reference follows the wallet, floored at 100.
    assert!(cfg.envelope.tracks_equity);
    assert_eq!(cfg.envelope.equity_fraction, 1.0);
    assert_eq!(cfg.envelope.floor_usdt, 100.0);
    assert_eq!(cfg.envelope.expand_dead_band_fraction, 0.05);
}

#[test]
fn the_committed_demo_profile_loads_pinned() {
    let cfg = kernel_config_from_profile(&repo_config("operational.demo.json"), &inputs())
        .expect("the shipped demo profile must load");
    assert_eq!(cfg.envelope.reference_usdt, 250_000.0);
    // 1,250,000 gross over the 250,000 reference: the 2026-08-21 risk-on dials.
    assert_eq!(cfg.envelope.gross_notional_multiple, 5.0);
    // No capital_reference block: the reference is pinned and never follows
    // the wallet.
    assert!(!cfg.envelope.tracks_equity);
}

#[test]
fn a_cap_the_engine_does_not_read_is_refused_rather_than_ignored() {
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["max_overnight_notional_usdt"] = serde_json::json!(25.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs())
        .expect_err("an unread cap was accepted");
    assert!(
        err.to_string().contains("max_overnight_notional_usdt"),
        "{err}"
    );
}

#[test]
fn the_retired_daily_loss_ceiling_is_refused_rather_than_ignored() {
    // The daily loss halt was removed 2026-08-20 on the owner's instruction.
    // Every funded host had `max_daily_loss_usdt` in its installed profile at
    // that moment, so the dangerous outcome is not refusal — it is an engine
    // that reads the key, ignores it, and lets the operator believe a ceiling
    // is still in force. Refusal names the key and stops the start.
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["max_daily_loss_usdt"] = serde_json::json!(25.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs())
        .expect_err("a retired ceiling was accepted");
    assert!(err.to_string().contains("max_daily_loss_usdt"), "{err}");
}

#[test]
fn a_profile_still_declaring_sleeve_shares_is_refused_rather_than_ignored() {
    // The per-sleeve capital partition is gone. A profile that still carves
    // the account into shares would otherwise boot an engine that reads the
    // shares, enforces none of them, and lets the operator believe two sleeves
    // are fenced from each other.
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["sleeve_limits"] = serde_json::json!({
        "carry": {"max_gross_notional_usdt": 200.0, "max_initial_margin_usdt": 40.0},
        "long": {"max_gross_notional_usdt": 300.0, "max_initial_margin_usdt": 60.0},
    });
    let err = kernel_config_from_profile(&doc.to_string(), &inputs())
        .expect_err("declared sleeve shares were accepted");
    assert!(err.to_string().contains("sleeve_limits"), "{err}");
}

#[test]
fn a_profile_from_the_future_is_refused() {
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["schema_version"] = serde_json::json!(2);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs()).unwrap_err();
    assert!(err.to_string().contains("schema_version"), "{err}");
}

#[test]
fn some_other_json_document_is_not_an_operational_profile() {
    let err = kernel_config_from_profile(
        r#"{"schema_version": 1, "kind": "something_else"}"#,
        &inputs(),
    )
    .unwrap_err();
    assert!(err.to_string().contains("not an operational profile"), "{err}");
}

#[test]
fn a_profile_whose_caps_do_not_nest_is_refused_at_load() {
    // The load-time proof is the kernel's own validate(), reached from here.
    // A second gross ceiling above the account gross cap describes a book
    // nobody can reach, and the outer cap would never bind.
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["max_component_gross_notional_usdt"] = serde_json::json!(1_000.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs()).unwrap_err();
    assert!(
        err.to_string().contains("max_component_gross_notional_usdt"),
        "{err}"
    );
}

#[test]
// The key is gone from the schema, and profile.rs refuses a key it does not
// read rather than ignoring it — so an old profile still carrying the retired
// per-symbol cap stops the engine instead of booting with a cap nobody
// enforces.
fn a_profile_still_carrying_the_retired_symbol_cap_is_refused() {
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["max_symbol_notional_usdt"] = serde_json::json!(50.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs()).unwrap_err();
    assert!(err.to_string().contains("max_symbol_notional_usdt"), "{err}");
}

#[test]
fn the_two_profiles_are_not_accidentally_the_same_shape() {
    // The demo profile is pinned; the funded one follows the wallet. If a
    // change ever made them load identically, the funded account would be
    // running under demo limits, and every other assertion here would still
    // pass.
    let demo = kernel_config_from_profile(&repo_config("operational.demo.json"), &inputs())
        .unwrap();
    let main =
        kernel_config_from_profile(&repo_config("operational.mainnet.json"), &inputs()).unwrap();
    assert_ne!(demo, main);
    assert!(!demo.envelope.tracks_equity && main.envelope.tracks_equity);
    assert!(
        main.envelope.account_gross_cap_usdt() < demo.envelope.account_gross_cap_usdt(),
        "the funded account should be the smaller book of the two"
    );
}

#[test]
fn a_gross_cap_no_amount_of_margin_could_fund_is_refused() {
    // operational_profile.py:409, the last load-time proof the Rust side did
    // not run. Gross above the whole capital reference times leverage is book
    // nobody can reach, so a cap set up there is scenery -- an operator
    // tightening it would watch nothing change.
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    // 100 of reference at leverage 5 funds 500 of book. Ask for 501.
    doc["account_risk"]["max_account_gross_notional_usdt"] = serde_json::json!(501.0);
    doc["account_risk"]["max_component_gross_notional_usdt"] = serde_json::json!(501.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs()).unwrap_err();
    assert!(err.to_string().contains("cannot be reached"), "{err}");
}

#[test]
fn the_shipped_mainnet_profile_sits_inside_what_its_capital_can_fund() {
    // 500 of book against a 100 of reference at leverage 5, which funds
    // exactly that. The full book posts 100 of margin — the declared margin
    // cap to the last decimal — so a change that ate the headroom would
    // otherwise show up only as a refusal to boot.
    let cfg =
        kernel_config_from_profile(&repo_config("operational.mainnet.json"), &inputs()).unwrap();
    let reachable = cfg.envelope.reference_usdt * cfg.leverage;
    assert_eq!(reachable, 500.0);
    assert_eq!(cfg.envelope.account_gross_cap_usdt(), 500.0);
}

#[test]
fn a_profile_whose_numbers_do_not_survive_a_round_trip_still_loads() {
    // The engine holds the account gross cap as a multiple of the reference,
    // because its reference follows the wallet. The profile states it as
    // money. Rebuilding one from the other lands a fraction of a cent off for
    // most numbers -- 100 * (177/100) is not 177 -- and both shipped profiles
    // set the component cap equal to the account cap. Without a tolerance on
    // that comparison, a profile would be refused for agreeing with itself.
    //
    // 113 is deliberate. 1.75 and 2.0 are exact in binary, which is why the
    // two files in the repository never showed this; 100 * (113/100) comes
    // back as 112.99999999999999, just *under* the number the profile stated,
    // which is the direction that gets a profile refused.
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["max_account_gross_notional_usdt"] = serde_json::json!(113.0);
    doc["account_risk"]["max_component_gross_notional_usdt"] = serde_json::json!(113.0);
    let cfg = kernel_config_from_profile(&doc.to_string(), &inputs())
        .expect("a profile that states one cap twice must load");
    assert_eq!(cfg.envelope.max_component_gross_notional_usdt, 113.0);
    // And the rebuild really is lossy, which is what makes the tolerance real
    // rather than defensive.
    assert!(
        cfg.envelope.account_gross_cap_usdt() < 113.0,
        "the rebuild has to land under the stated cap, or this test proves nothing"
    );
}
