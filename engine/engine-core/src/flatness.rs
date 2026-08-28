//! Credential-wide, read-only flatness attestation for guarded rollouts.
//!
//! This intentionally does not read the engine's configured symbol table. A
//! generation change is safe only when the venue adapter can enumerate the
//! whole account surface it owns; adapters that cannot do that refuse through
//! [`VenueGateway::account_inventory`].

use std::error::Error;
use std::path::Path;

use crate::{assembly, clock, config};

const MAX_LOCAL_SAMPLE_AGE_MS: i64 = 30_000;
const MAX_LOCAL_FUTURE_SKEW_MS: i64 = 5_000;
const MAX_DOUBLE_SCAN_AGE_MS: i64 = 60_000;

fn validate_sample(
    inventory: &engine_types::AccountInventory,
    sample: usize,
) -> Result<(), Box<dyn Error>> {
    if inventory.scope.trim().is_empty() {
        return Err(format!("venue returned an empty inventory scope on sample {sample}").into());
    }
    let now_ms = clock::wall_ms();
    let age_ms = now_ms.saturating_sub(inventory.observed_ms);
    if age_ms > MAX_LOCAL_SAMPLE_AGE_MS
        || inventory.observed_ms > now_ms.saturating_add(MAX_LOCAL_FUTURE_SKEW_MS)
    {
        return Err(format!(
            "venue inventory sample {sample} is not fresh: observed_ms={} now_ms={now_ms}",
            inventory.observed_ms
        )
        .into());
    }
    if inventory
        .positions
        .iter()
        .any(|position| !position.qty.is_finite() || position.qty <= 0.0)
    {
        return Err(format!(
            "venue inventory sample {sample} contains an invalid non-flat position quantity"
        )
        .into());
    }
    if inventory.is_flat() {
        return Ok(());
    }
    for position in &inventory.positions {
        eprintln!(
            "flat-blocker sample={} position product={} symbol={} side={:?} qty={}",
            sample, position.product, position.symbol, position.side, position.qty
        );
    }
    for order in &inventory.open_orders {
        eprintln!(
            "flat-blocker sample={} order product={} symbol={} client_order_id={}",
            sample, order.product, order.symbol, order.client_order_id
        );
    }
    Err(format!(
        "account is not flat on sample {sample}: {} position(s), {} open order(s)",
        inventory.positions.len(),
        inventory.open_orders.len()
    )
    .into())
}

/// Probe the account selected by `config_path` without taking its writer lease
/// or mutating venue state. Success is an attestation that the adapter's
/// credential-wide inventory was fresh and empty.
pub async fn run(config_path: &Path) -> Result<(), Box<dyn Error>> {
    let attestation_started_ms = clock::wall_ms();
    let loaded = config::load(config_path)?;
    let chosen = assembly::venue_name(&loaded.config.engine.venue)?;
    let mut venue = assembly::inventory_probe(chosen)?;

    let who = venue.account_identity().await?;
    if who.venue != chosen.venue() || who.realm != chosen.realm() {
        return Err(format!(
            "venue identity mismatch: config selects {}/{} but credentials answered as {}/{}",
            chosen.venue(),
            chosen.realm(),
            who.venue,
            who.realm
        )
        .into());
    }
    let expected = std::env::var("EXPECTED_ENGINE_ACCOUNT_USER_ID")
        .map_err(|_| "EXPECTED_ENGINE_ACCOUNT_USER_ID is required for a bound attestation")?;
    let expected = expected.trim();
    if expected.is_empty() {
        return Err(
            "EXPECTED_ENGINE_ACCOUNT_USER_ID is empty; flatness is not account-bound".into(),
        );
    }
    if expected != who.user_id {
        return Err(format!(
            "account identity mismatch: expected {expected}, credentials answered as {}",
            who.user_id
        )
        .into());
    }

    // Two complete scans make a cross-endpoint race visible in either sample.
    // This is still an attestation, not an atomic venue snapshot; rollout
    // policy separately prohibits manual trading while it runs.
    let first = venue.account_inventory().await?;
    validate_sample(&first, 1)?;
    let second = venue.account_inventory().await?;
    validate_sample(&second, 2)?;
    if first.scope != second.scope {
        return Err(format!(
            "inventory scope changed between flat samples: first={:?} second={:?}",
            first.scope, second.scope
        )
        .into());
    }
    let completed_ms = clock::wall_ms();
    if completed_ms.saturating_sub(attestation_started_ms) > MAX_DOUBLE_SCAN_AGE_MS {
        return Err("the two-scan flatness attestation took longer than 60 seconds".into());
    }

    println!(
        "flat-attestation account={} venue={} realm={} observed_ms_first={} observed_ms_second={} scope={}",
        who.user_id,
        who.venue,
        who.realm,
        first.observed_ms,
        second.observed_ms,
        second.scope
    );
    println!("flat=true samples=2 positions=0 open_orders=0");
    Ok(())
}
