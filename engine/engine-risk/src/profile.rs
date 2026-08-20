//! Reading the fleet's operational profile into a [`KernelConfig`].
//!
//! `configs/operational.mainnet.json` is the document that says how much of
//! the funded account may be at risk. The Python fleet loads it and proves it
//! at start-up. Until now the engine did not read it at all — the same numbers
//! were transcribed by hand into a test, which is a copy, and a copy of a risk
//! limit is a limit that can drift without anyone noticing.
//!
//! So the engine reads the same file. If an operator tightens a cap, both
//! halves tighten. If the two disagree, they disagree because the file says
//! two things, which is a thing you can go and look at.
//!
//! **What is deliberately strict.** Unknown keys inside `account_risk` and
//! `capital_reference` are refused rather than ignored. Those are the blocks
//! the kernel reads, and a cap added on the Python side that the engine
//! silently did not enforce is exactly the failure this module exists to stop.
//! The producer blocks (`long`, `carry`, `hedge`) are sizing for the Python
//! producers, are not the kernel's business, and are skipped by name.
//!
//! **What is not in the file.** Three numbers the kernel needs live elsewhere,
//! so the caller supplies them through [`ProfileInputs`] rather than this
//! module inventing defaults: the disaster-stop distance (an environment
//! variable on the host, `DISASTER_STOP_FRACTION`), the minimum order notional
//! (a venue instrument rule), and how stale an account reading may be.

use std::collections::BTreeMap;

use engine_types::ids::StrategyId;
use serde_json::Value;

use crate::config::{
    ConfigError, EnvelopeConfig, KernelConfig, PartitionConfig, StrategyAllocation,
};

/// The only schema this reads. A profile from the future is refused rather
/// than read optimistically — the fields it added would be the interesting
/// ones.
pub const PROFILE_SCHEMA_VERSION: i64 = 1;

/// `operational_profile.py`'s `OPERATIONAL_PROFILE_KIND`.
pub const PROFILE_KIND: &str = "liquidity_migration_operational_profile";

/// The equity-tracking mode name, from the same module.
const MODE_ACCOUNT_EQUITY: &str = "account_equity";
const MODE_FIXED: &str = "fixed";

/// Top-level keys that belong to the Python producers rather than the kernel.
/// Named so that a genuinely unknown key is still an error.
const PRODUCER_BLOCKS: &[&str] = &["long", "carry", "hedge"];

const TOP_LEVEL_KEYS: &[&str] = &[
    "schema_version",
    "kind",
    "capital_reference_usdt",
    "capital_reference",
    "account_risk",
];

const ACCOUNT_RISK_KEYS: &[&str] = &[
    "max_component_gross_notional_usdt",
    "max_account_gross_notional_usdt",
    "max_symbol_notional_usdt",
    "max_initial_margin_usdt",
    "max_leverage",
    "quantity_tolerance",
    "sleeve_limits",
];

const CAPITAL_REFERENCE_KEYS: &[&str] = &[
    "mode",
    "equity_fraction",
    "floor_usdt",
    "expand_dead_band_fraction",
];

const SLEEVE_LIMIT_KEYS: &[&str] = &["max_gross_notional_usdt", "max_initial_margin_usdt"];

/// The numbers the kernel needs that the profile does not carry.
#[derive(Clone, Debug)]
pub struct ProfileInputs<'a> {
    /// How far a position may move against us before its stop ends it. The
    /// host's `DISASTER_STOP_FRACTION`.
    pub disaster_stop_fraction: f64,
    /// An order smaller than this after clamping is refused, not sent.
    pub min_order_notional_usdt: f64,
    /// An account view older than this is not evidence about the account now.
    pub max_account_view_age_ns: u64,
    /// Which strategy each sleeve name in `sleeve_limits` refers to. A sleeve
    /// the engine does not run is an error, not a share left on the floor.
    pub sleeves: &'a [(&'a str, StrategyId)],
}

fn bad(detail: impl Into<String>) -> ConfigError {
    ConfigError {
        detail: detail.into(),
    }
}

/// Refuse a key nobody reads. The whole point of the strictness above.
fn reject_unknown_keys(
    row: &serde_json::Map<String, Value>,
    known: &[&str],
    also_allowed: &[&str],
    where_: &str,
) -> Result<(), ConfigError> {
    let unknown: Vec<&str> = row
        .keys()
        .map(String::as_str)
        .filter(|key| !known.contains(key) && !also_allowed.contains(key))
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    Err(bad(format!(
        "{where_} has key(s) this engine does not read: {}. A limit the engine \
         ignores is not a limit — teach it the key or take the key out.",
        unknown.join(", ")
    )))
}

fn object<'a>(
    row: &'a serde_json::Map<String, Value>,
    key: &str,
    where_: &str,
) -> Result<&'a serde_json::Map<String, Value>, ConfigError> {
    row.get(key)
        .ok_or_else(|| bad(format!("{where_} is missing {key}")))?
        .as_object()
        .ok_or_else(|| bad(format!("{where_}.{key} must be an object")))
}

fn number(
    row: &serde_json::Map<String, Value>,
    key: &str,
    where_: &str,
) -> Result<f64, ConfigError> {
    let value = row
        .get(key)
        .ok_or_else(|| bad(format!("{where_} is missing {key}")))?;
    let n = value
        .as_f64()
        .ok_or_else(|| bad(format!("{where_}.{key} must be a number; got {value}")))?;
    if !n.is_finite() {
        return Err(bad(format!("{where_}.{key} must be finite")));
    }
    Ok(n)
}

fn optional_number(
    row: &serde_json::Map<String, Value>,
    key: &str,
    where_: &str,
) -> Result<Option<f64>, ConfigError> {
    match row.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => number(row, key, where_).map(Some),
    }
}

/// Read one operational profile document into a kernel config, and prove it.
///
/// The returned config has already been through [`KernelConfig::validate`], so
/// a profile that describes a book nobody can reach — caps that do not nest,
/// partition shares that sum above the account — is an error here rather than
/// a surprise at the first order.
pub fn kernel_config_from_profile(
    text: &str,
    inputs: &ProfileInputs<'_>,
) -> Result<KernelConfig, ConfigError> {
    let parsed: Value = serde_json::from_str(text)
        .map_err(|err| bad(format!("the operational profile is not valid JSON: {err}")))?;
    let root = parsed
        .as_object()
        .ok_or_else(|| bad("the operational profile must be a JSON object"))?;

    reject_unknown_keys(root, TOP_LEVEL_KEYS, PRODUCER_BLOCKS, "the operational profile")?;

    match root.get("schema_version").and_then(Value::as_i64) {
        Some(PROFILE_SCHEMA_VERSION) => {}
        Some(other) => {
            return Err(bad(format!(
                "operational profile schema_version {other} is unsupported; this engine \
                 reads version {PROFILE_SCHEMA_VERSION}"
            )))
        }
        None => return Err(bad("the operational profile is missing schema_version")),
    }
    match root.get("kind").and_then(Value::as_str) {
        Some(PROFILE_KIND) => {}
        Some(other) => {
            return Err(bad(format!(
                "this is not an operational profile: kind is {other:?}"
            )))
        }
        None => return Err(bad("the operational profile is missing kind")),
    }

    let reference_usdt = number(root, "capital_reference_usdt", "the operational profile")?;
    let account = object(root, "account_risk", "the operational profile")?;
    reject_unknown_keys(account, ACCOUNT_RISK_KEYS, &[], "account_risk")?;

    let account_gross = number(account, "max_account_gross_notional_usdt", "account_risk")?;
    if reference_usdt <= 0.0 {
        return Err(bad("capital_reference_usdt must be positive"));
    }

    // The kernel holds the account gross cap as a multiple of the reference so
    // that it scales when the reference follows the wallet. The file states it
    // in money at the declared reference; this is that same number.
    let gross_notional_multiple = account_gross / reference_usdt;

    let (tracks_equity, equity_fraction, floor_usdt, expand_dead_band_fraction) =
        match root.get("capital_reference") {
            None => (
                false,
                1.0,
                // No capital_reference block means the reference is pinned, and
                // the floor is only ever read on the equity-tracking path. The
                // reference itself is the honest value for "the reference never
                // falls below this" when it never moves; Python's 0.0 default
                // would trip the kernel's own positivity check for a number
                // that cannot be reached.
                reference_usdt,
                0.05,
            ),
            Some(value) => {
                let row = value
                    .as_object()
                    .ok_or_else(|| bad("capital_reference must be an object"))?;
                reject_unknown_keys(row, CAPITAL_REFERENCE_KEYS, &[], "capital_reference")?;
                let mode = row
                    .get("mode")
                    .and_then(Value::as_str)
                    .ok_or_else(|| bad("capital_reference is missing mode"))?;
                let tracks = match mode {
                    MODE_ACCOUNT_EQUITY => true,
                    MODE_FIXED => false,
                    other => {
                        return Err(bad(format!(
                            "capital_reference.mode must be {MODE_FIXED:?} or \
                             {MODE_ACCOUNT_EQUITY:?}; got {other:?}"
                        )))
                    }
                };
                let fraction =
                    optional_number(row, "equity_fraction", "capital_reference")?.unwrap_or(1.0);
                let dead_band =
                    optional_number(row, "expand_dead_band_fraction", "capital_reference")?
                        .unwrap_or(0.05);
                let floor = optional_number(row, "floor_usdt", "capital_reference")?
                    .unwrap_or(if tracks { 0.0 } else { reference_usdt });
                if tracks && floor <= 0.0 {
                    return Err(bad(
                        "capital_reference.floor_usdt must be positive in account_equity mode",
                    ));
                }
                if floor > reference_usdt {
                    return Err(bad(
                        "capital_reference.floor_usdt cannot exceed capital_reference_usdt",
                    ));
                }
                (tracks, fraction, floor, dead_band)
            }
        };

    let envelope = EnvelopeConfig {
        tracks_equity,
        reference_usdt,
        equity_fraction,
        floor_usdt,
        expand_dead_band_fraction,
        gross_notional_multiple,
        disaster_stop_fraction: inputs.disaster_stop_fraction,
        max_symbol_notional_usdt: number(account, "max_symbol_notional_usdt", "account_risk")?,
        max_component_gross_notional_usdt: number(
            account,
            "max_component_gross_notional_usdt",
            "account_risk",
        )?,
        max_initial_margin_usdt: number(account, "max_initial_margin_usdt", "account_risk")?,
    };

    let allocations = parse_sleeve_limits(account, inputs)?;

    let config = KernelConfig {
        max_account_view_age_ns: inputs.max_account_view_age_ns,
        envelope,
        partition: PartitionConfig {
            allocations,
            leverage: number(account, "max_leverage", "account_risk")?,
            min_order_notional_usdt: inputs.min_order_notional_usdt,
        },
        qty_tolerance: number(account, "quantity_tolerance", "account_risk")?,
    };
    config.validate()?;
    Ok(config)
}

fn parse_sleeve_limits(
    account: &serde_json::Map<String, Value>,
    inputs: &ProfileInputs<'_>,
) -> Result<Vec<StrategyAllocation>, ConfigError> {
    let Some(value) = account.get("sleeve_limits") else {
        // No partition. The account caps alone bind, exactly as an
        // unpartitioned Python profile behaves.
        return Ok(Vec::new());
    };
    let row = value
        .as_object()
        .ok_or_else(|| bad("account_risk.sleeve_limits must be an object"))?;

    let known: BTreeMap<&str, StrategyId> = inputs.sleeves.iter().copied().collect();
    let mut allocations = Vec::with_capacity(row.len());
    for (sleeve, limits) in row {
        let Some(strategy) = known.get(sleeve.as_str()) else {
            // A share of the account earmarked for something that is not
            // running is not harmless: the sums that prove the partition fits
            // inside the account would count capital nobody can spend, and the
            // engine would be enforcing a different partition from the fleet.
            return Err(bad(format!(
                "sleeve_limits names {sleeve:?}, which this engine does not run \
                 (it runs: {})",
                if inputs.sleeves.is_empty() {
                    "nothing".to_string()
                } else {
                    inputs
                        .sleeves
                        .iter()
                        .map(|(name, _)| *name)
                        .collect::<Vec<_>>()
                        .join(", ")
                }
            )));
        };
        let limits = limits.as_object().ok_or_else(|| {
            bad(format!("account_risk.sleeve_limits.{sleeve} must be an object"))
        })?;
        let where_ = format!("sleeve_limits.{sleeve}");
        reject_unknown_keys(limits, SLEEVE_LIMIT_KEYS, &[], &where_)?;
        allocations.push(StrategyAllocation {
            strategy: *strategy,
            max_gross_notional_usdt: number(limits, "max_gross_notional_usdt", &where_)?,
            max_initial_margin_usdt: number(limits, "max_initial_margin_usdt", &where_)?,
        });
    }
    // Stable order so two loads of one file produce equal configs.
    allocations.sort_by_key(|share| share.strategy.0);
    Ok(allocations)
}
