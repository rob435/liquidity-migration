//! The arming switch, and the law every venue obeys.
//!
//! One rule, stated once, applied to all four venues: a realm that moves real
//! money refuses to build unless the owner has armed `REAL_MONEY` on the host,
//! and a practice realm refuses to build while it is armed. The second half is
//! what stops an armed host from quietly running a practice engine beside the
//! funded one, which is how an operator ends up reading play numbers and
//! believing they are the funded account's.
//!
//! This module knows nothing about hosts or credentials. It answers one
//! question — may this realm run on this host — and every venue's realm table
//! asks it rather than restating it. A venue that restated it could get it
//! subtly wrong in its own file and nobody would see the two copies disagree.
//!
//! The engine never writes `REAL_MONEY`. It is set by the account owner's own
//! hand in the host credential file.

use engine_types::VenueError;

/// The arming switch. Read from the process environment, set by the owner in
/// the host credential file — never by the engine, and never by a config.
pub const REAL_MONEY_ENV: &str = "REAL_MONEY";

/// Values that mean yes. The Python fleet's `TRUE_ENV_VALUES`, exactly.
const TRUE_VALUES: &[&str] = &["1", "true", "yes", "on"];

/// Values that mean no. The Python fleet's `FALSE_ENV_VALUES`, exactly —
/// including the empty string, so `REAL_MONEY=` is a clear no rather than a
/// typo.
const FALSE_VALUES: &[&str] = &["", "0", "false", "no", "off"];

/// Read a boolean environment variable, refusing anything ambiguous.
///
/// The Python fleet splits this into `env_flag` (lenient) and
/// `reject_ambiguous_flag` (strict) and calls both at the safety-critical
/// sites. Here it is one strict function, because every site in the engine
/// that reads a flag is a safety-critical one.
pub fn env_flag(name: &str) -> Result<bool, VenueError> {
    match std::env::var(name) {
        Err(_) => Ok(false),
        Ok(raw) => flag_value(name, &raw),
    }
}

/// The parse itself, separated so a test can drive it without touching the
/// process environment — which is global, and which a parallel test suite
/// shares.
pub(crate) fn flag_value(name: &str, raw: &str) -> Result<bool, VenueError> {
    let normalised = raw.trim().to_ascii_lowercase();
    if TRUE_VALUES.contains(&normalised.as_str()) {
        return Ok(true);
    }
    if FALSE_VALUES.contains(&normalised.as_str()) {
        return Ok(false);
    }
    Err(VenueError::Credentials(format!(
        "{name}={raw:?} is not a recognised boolean. Use one of {TRUE_VALUES:?} to \
         enable or {FALSE_VALUES:?} (or unset) to disable — refusing to guess for a \
         safety-critical toggle."
    )))
}

/// Whether real money is armed on this host.
pub fn real_money_armed() -> Result<bool, VenueError> {
    env_flag(REAL_MONEY_ENV)
}

/// Refuse the realm that the arming switch does not permit, reading the switch.
pub fn check_arming(realm: &str, is_real_money: bool) -> Result<(), VenueError> {
    check_arming_with(realm, is_real_money, real_money_armed()?)
}

/// The rule itself, with the switch supplied rather than read.
///
/// Split out so the whole truth table can be tested without writing to the
/// process environment — which is shared by every test in a binary and racy to
/// mutate from a parallel suite. `tests/arming_env.rs` covers the reading half
/// in a process of its own.
pub fn check_arming_with(realm: &str, is_real_money: bool, armed: bool) -> Result<(), VenueError> {
    match (is_real_money, armed) {
        (true, false) => Err(VenueError::Credentials(format!(
            "the {realm} realm requires {REAL_MONEY_ENV} to be explicitly armed; naming \
             the funded account is not on its own authorization to trade real capital"
        ))),
        (false, true) => Err(VenueError::Credentials(format!(
            "the {realm} realm refuses to run while {REAL_MONEY_ENV} is armed; unset it, or \
             select a real-money realm explicitly"
        ))),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognised_boolean_values_match_the_python_fleet() {
        for yes in ["1", "true", "yes", "on", "TRUE", " On "] {
            assert!(flag_value("F", yes).unwrap(), "{yes:?} should be true");
        }
        for no in ["", "0", "false", "no", "off", "OFF", " "] {
            assert!(!flag_value("F", no).unwrap(), "{no:?} should be false");
        }
    }

    #[test]
    fn the_arming_truth_table_is_all_four_cases() {
        // Armed: a real-money realm may run, a practice realm may not.
        assert!(check_arming_with("mainnet", true, true).is_ok());
        assert!(check_arming_with("demo", false, true).is_err());
        // Unarmed: practice may run, real money may not.
        assert!(check_arming_with("demo", false, false).is_ok());
        assert!(check_arming_with("mainnet", true, false).is_err());
    }

    #[test]
    fn each_refusal_says_which_way_to_fix_it() {
        let unarmed = check_arming_with("mainnet", true, false)
            .unwrap_err()
            .to_string();
        assert!(unarmed.contains(REAL_MONEY_ENV), "{unarmed}");
        assert!(unarmed.contains("armed"), "{unarmed}");

        let armed = check_arming_with("demo", false, true)
            .unwrap_err()
            .to_string();
        assert!(armed.contains(REAL_MONEY_ENV), "{armed}");
        assert!(armed.contains("unset it"), "{armed}");
    }

    #[test]
    fn the_refusal_names_the_realm_that_was_asked_for() {
        // Four venues now share these two sentences, so an operator reading
        // one has to be able to tell which realm they named.
        let said = check_arming_with("hyperliquid_testnet", false, true)
            .unwrap_err()
            .to_string();
        assert!(said.contains("hyperliquid_testnet"), "{said}");
    }

    #[test]
    fn a_typo_in_the_arming_switch_is_an_error_not_a_false() {
        // The whole point: "ture" must not read as "not armed", because on the
        // practice side that is the value that would let an armed host run a
        // practice realm.
        for typo in ["ture", "yse", "enabled", "y", "2", "-1", "null"] {
            let err = flag_value(REAL_MONEY_ENV, typo).unwrap_err();
            assert!(
                matches!(err, VenueError::Credentials(_)),
                "{typo:?} gave {err:?}"
            );
            assert!(
                err.to_string().contains(typo),
                "{err} should quote {typo:?}"
            );
        }
    }
}
