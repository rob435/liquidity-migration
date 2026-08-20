//! The engine reads the fleet's own profile documents.
//!
//! These load `configs/operational.mainnet.json` and
//! `configs/operational.demo.json` from the repository — the real files, the
//! ones the Python fleet installs — rather than a copy. A cap that changes in
//! the file changes here, and if a change makes the profile unloadable this
//! suite says so before a deploy does.

use std::path::PathBuf;

use engine_risk::{kernel_config_from_profile, ProfileInputs};
use engine_types::ids::StrategyId;

mod common;
use common::{CARRY, DISASTER_STOP_FRACTION, LONG, MAX_VIEW_AGE_NS, MIN_ORDER_NOTIONAL_USDT};

fn inputs<'a>(sleeves: &'a [(&'a str, StrategyId)]) -> ProfileInputs<'a> {
    ProfileInputs {
        disaster_stop_fraction: DISASTER_STOP_FRACTION,
        min_order_notional_usdt: MIN_ORDER_NOTIONAL_USDT,
        max_account_view_age_ns: MAX_VIEW_AGE_NS,
        sleeves,
    }
}

fn both_sleeves() -> Vec<(&'static str, StrategyId)> {
    vec![("carry", CARRY), ("long", LONG)]
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
    let sleeves = both_sleeves();
    let cfg = kernel_config_from_profile(&repo_config("operational.mainnet.json"), &inputs(&sleeves))
        .expect("the shipped mainnet profile must load");

    // Every assertion below is the literal number in the file. If the owner
    // changes a cap, this test is where the engine finds out.
    // 2026-08-20 owner sizing directive: each sleeve half the wallet
    // (dials 0.5/0.5), venue entry leverage floored at 5.
    assert_eq!(cfg.envelope.reference_usdt, 100.0);
    assert_eq!(cfg.envelope.max_symbol_notional_usdt, 50.0);
    assert_eq!(cfg.envelope.max_component_gross_notional_usdt, 100.0);
    assert_eq!(cfg.envelope.max_initial_margin_usdt, 100.0);
    assert_eq!(cfg.partition.leverage, 5.0);
    assert_eq!(cfg.qty_tolerance, 1e-12);
    assert_eq!(cfg.loss_guard.max_daily_loss_usdt, Some(10.0));

    // 100 of gross against a 100 reference: the two half-wallet sleeves.
    assert_eq!(cfg.envelope.gross_notional_multiple, 1.0);
    assert_eq!(cfg.envelope.account_gross_cap_usdt(), 100.0);

    // The funded account's reference follows the wallet, floored at 100.
    assert!(cfg.envelope.tracks_equity);
    assert_eq!(cfg.envelope.equity_fraction, 1.0);
    assert_eq!(cfg.envelope.floor_usdt, 100.0);
    assert_eq!(cfg.envelope.expand_dead_band_fraction, 0.05);

    // The partition, in strategy order.
    let carry = cfg.partition.share(CARRY).expect("carry has a share");
    assert_eq!(carry.max_gross_notional_usdt, 50.0);
    assert_eq!(carry.max_initial_margin_usdt, 50.0);
    let long = cfg.partition.share(LONG).expect("long has a share");
    assert_eq!(long.max_gross_notional_usdt, 50.0);
    assert_eq!(long.max_initial_margin_usdt, 50.0);
}

#[test]
fn the_mainnet_partition_exactly_fills_the_account_and_no_more() {
    // Worth stating on its own: the two shares sum to the account caps to the
    // last decimal place. That is what makes the margin comparison in
    // PartitionConfig::validate a real check rather than one with slack in it,
    // and it is the property that broke once when the proof compared against
    // gross divided by leverage instead of the declared margin cap.
    let sleeves = both_sleeves();
    let cfg = kernel_config_from_profile(&repo_config("operational.mainnet.json"), &inputs(&sleeves))
        .unwrap();
    let gross: f64 = cfg
        .partition
        .allocations
        .iter()
        .map(|s| s.max_gross_notional_usdt)
        .sum();
    let margin: f64 = cfg
        .partition
        .allocations
        .iter()
        .map(|s| s.max_initial_margin_usdt)
        .sum();
    assert_eq!(gross, cfg.envelope.account_gross_cap_usdt());
    assert_eq!(margin, cfg.envelope.max_initial_margin_usdt);
}

#[test]
fn the_committed_demo_profile_loads_unpartitioned_and_pinned() {
    let sleeves = both_sleeves();
    let cfg = kernel_config_from_profile(&repo_config("operational.demo.json"), &inputs(&sleeves))
        .expect("the shipped demo profile must load");
    assert_eq!(cfg.envelope.reference_usdt, 250_000.0);
    assert_eq!(cfg.envelope.gross_notional_multiple, 2.0);
    assert_eq!(cfg.envelope.max_symbol_notional_usdt, 125_000.0);
    // No capital_reference block: the reference is pinned and never follows
    // the wallet.
    assert!(!cfg.envelope.tracks_equity);
    // No sleeve_limits: the account caps alone bind.
    assert!(cfg.partition.allocations.is_empty());
    // No max_daily_loss_usdt: the ceiling is off, though the guard still runs
    // and still refuses to act on an unreadable account.
    assert_eq!(cfg.loss_guard.max_daily_loss_usdt, None);
}

#[test]
fn a_cap_the_engine_does_not_read_is_refused_rather_than_ignored() {
    let sleeves = both_sleeves();
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["max_overnight_notional_usdt"] = serde_json::json!(25.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs(&sleeves))
        .expect_err("an unread cap was accepted");
    assert!(
        err.to_string().contains("max_overnight_notional_usdt"),
        "{err}"
    );
}

#[test]
fn a_sleeve_the_engine_does_not_run_is_refused() {
    // A share earmarked for something that is not running would make the sums
    // that prove the partition fits count capital nobody can spend.
    let only_carry = vec![("carry", CARRY)];
    let err = kernel_config_from_profile(
        &repo_config("operational.mainnet.json"),
        &inputs(&only_carry),
    )
    .expect_err("a share for an absent sleeve was accepted");
    assert!(err.to_string().contains("long"), "{err}");
}

#[test]
fn a_profile_from_the_future_is_refused() {
    let sleeves = both_sleeves();
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["schema_version"] = serde_json::json!(2);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs(&sleeves)).unwrap_err();
    assert!(err.to_string().contains("schema_version"), "{err}");
}

#[test]
fn some_other_json_document_is_not_an_operational_profile() {
    let sleeves = both_sleeves();
    let err = kernel_config_from_profile(
        r#"{"schema_version": 1, "kind": "something_else"}"#,
        &inputs(&sleeves),
    )
    .unwrap_err();
    assert!(err.to_string().contains("not an operational profile"), "{err}");
}

#[test]
fn a_profile_whose_caps_do_not_nest_is_refused_at_load() {
    // The load-time proof is the kernel's own validate(), reached from here.
    // A symbol cap above the account gross cap describes a book nobody can
    // reach, and the outer cap would never bind.
    let sleeves = both_sleeves();
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["max_symbol_notional_usdt"] = serde_json::json!(1_000.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs(&sleeves)).unwrap_err();
    assert!(err.to_string().contains("max_symbol_notional_usdt"), "{err}");
}

#[test]
fn a_partition_that_oversubscribes_the_account_is_refused_at_load() {
    let sleeves = both_sleeves();
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["sleeve_limits"]["carry"]["max_gross_notional_usdt"] =
        serde_json::json!(200.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs(&sleeves)).unwrap_err();
    assert!(err.to_string().contains("above the account gross"), "{err}");
}

#[test]
fn the_two_profiles_are_not_accidentally_the_same_shape() {
    // The demo profile is pinned and unpartitioned; the mainnet one follows
    // the wallet and is split between the sleeves. If a change ever made them
    // load identically, the funded account would be running under demo
    // limits, and every other assertion here would still pass.
    let sleeves = both_sleeves();
    let demo = kernel_config_from_profile(&repo_config("operational.demo.json"), &inputs(&sleeves))
        .unwrap();
    let main =
        kernel_config_from_profile(&repo_config("operational.mainnet.json"), &inputs(&sleeves))
            .unwrap();
    assert_ne!(demo, main);
    assert!(!demo.envelope.tracks_equity && main.envelope.tracks_equity);
    assert!(demo.partition.allocations.is_empty() && !main.partition.allocations.is_empty());
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
    let sleeves = both_sleeves();
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    // 100 of reference at leverage 5 funds 500 of book. Ask for 501.
    doc["account_risk"]["max_account_gross_notional_usdt"] = serde_json::json!(501.0);
    doc["account_risk"]["max_component_gross_notional_usdt"] = serde_json::json!(501.0);
    let err = kernel_config_from_profile(&doc.to_string(), &inputs(&sleeves)).unwrap_err();
    assert!(err.to_string().contains("cannot be reached"), "{err}");
}

#[test]
fn the_shipped_mainnet_profile_sits_inside_what_its_capital_can_fund() {
    // 100 of book against 100 of reference at leverage 5, which funds 500.
    // Stated because it is the margin the check above leaves, and a change to
    // the dials that ate it would otherwise show up only as a refusal to boot.
    let sleeves = both_sleeves();
    let cfg = kernel_config_from_profile(&repo_config("operational.mainnet.json"), &inputs(&sleeves))
        .unwrap();
    let reachable = cfg.envelope.reference_usdt * cfg.partition.leverage;
    assert_eq!(reachable, 500.0);
    assert_eq!(cfg.envelope.account_gross_cap_usdt(), 100.0);
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
    let sleeves = both_sleeves();
    let mut doc: serde_json::Value =
        serde_json::from_str(&repo_config("operational.mainnet.json")).unwrap();
    doc["account_risk"]["max_account_gross_notional_usdt"] = serde_json::json!(113.0);
    doc["account_risk"]["max_component_gross_notional_usdt"] = serde_json::json!(113.0);
    // The sleeve shares have to fit inside the smaller account too.
    doc["account_risk"]["sleeve_limits"]["carry"]["max_gross_notional_usdt"] =
        serde_json::json!(60.0);
    doc["account_risk"]["sleeve_limits"]["long"]["max_gross_notional_usdt"] =
        serde_json::json!(49.0);
    let cfg = kernel_config_from_profile(&doc.to_string(), &inputs(&sleeves))
        .expect("a profile that states one cap twice must load");
    assert_eq!(cfg.envelope.max_component_gross_notional_usdt, 113.0);
    // And the rebuild really is lossy, which is what makes the tolerance real
    // rather than defensive.
    assert!(
        cfg.envelope.account_gross_cap_usdt() < 113.0,
        "the rebuild has to land under the stated cap, or this test proves nothing"
    );
}
