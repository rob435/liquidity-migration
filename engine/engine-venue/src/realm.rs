//! Which venue account a credential authenticates against: `demo` or
//! `mainnet`.
//!
//! This is the one place in the engine where a realm becomes a hostname and a
//! pair of credential variable names. Everything else carries the realm and
//! asks here, so there is a single answer to "where do real orders go" and a
//! single place to read it.
//!
//! The rules are the Python fleet's, ported so both halves behave the same
//! way while both are running (`liquidity_migration/core/venue_realm.py` and
//! the arming checks in `liquidity_migration/venue/bybit.py`):
//!
//! - **A realm is always named.** There is no default. A config that does not
//!   say which account it trades does not get one picked for it.
//! - **The two realms read different credential variables.** A key left in the
//!   environment for one realm can never authenticate the other.
//! - **`REAL_MONEY` is the arming switch, and it cuts both ways.** Mainnet
//!   refuses to build unless it is armed; demo refuses to build while it is.
//!   The second half is what stops an armed host from quietly running the demo
//!   engine against real money because someone edited one line.
//! - **An unrecognised `REAL_MONEY` value stops the engine.** `REAL_MONEY=ture`
//!   is not false, it is a typo in a safety-critical switch, and guessing
//!   which way the operator meant it is exactly the wrong move.
//! - **Testnet does not exist here.** It is not a realm, and its hosts are
//!   forbidden by `tests/venue_fence.rs`.
//!
//! Naming a realm is not permission to trade it. `REAL_MONEY=true` lives in
//! the host credential file and is set by the account owner's own hand.

use std::fmt;

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

/// Which venue account is being addressed.
///
/// Distinct from the engine's *mode* (shadow or live), which says whether the
/// engine sends orders at all. The two are independent: a shadow engine still
/// signs read requests, and it signs them against a realm.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum VenueRealm {
    /// Bybit's practice account. Play money, real matching engine.
    Demo,
    /// The funded account. Real money.
    Mainnet,
}

impl VenueRealm {
    /// Parse one explicit `demo` or `mainnet`, refusing every fallback.
    ///
    /// Surrounding space and letter case are forgiven because operators type
    /// these into env files; nothing else is. In particular there is no
    /// "unset means demo" — a caller that wants demo says demo.
    pub fn parse(value: &str) -> Result<Self, VenueError> {
        match value.trim().to_ascii_lowercase().as_str() {
            "demo" => Ok(VenueRealm::Demo),
            "mainnet" => Ok(VenueRealm::Mainnet),
            other => Err(VenueError::BadRequest(format!(
                "venue realm must be explicitly \"demo\" or \"mainnet\"; got {other:?}"
            ))),
        }
    }

    /// The name this realm parses from and logs as.
    pub fn as_str(self) -> &'static str {
        match self {
            VenueRealm::Demo => "demo",
            VenueRealm::Mainnet => "mainnet",
        }
    }

    /// The REST host for this realm. The only mapping from realm to REST host
    /// in the engine.
    pub fn rest_base(self) -> &'static str {
        match self {
            VenueRealm::Demo => "https://api-demo.bybit.com",
            VenueRealm::Mainnet => "https://api.bybit.com",
        }
    }

    /// The private order stream for this realm. Public market data is a
    /// different crate's business and is served from the mainnet stream for
    /// both realms — Bybit has no separate demo feed for it.
    pub fn private_ws(self) -> &'static str {
        match self {
            VenueRealm::Demo => "wss://stream-demo.bybit.com/v5/private",
            VenueRealm::Mainnet => "wss://stream.bybit.com/v5/private",
        }
    }

    /// The environment variables holding this realm's key and secret.
    ///
    /// Deliberately disjoint between realms: this is what makes "a demo key
    /// cannot authenticate mainnet" true of the environment and not merely of
    /// the code that reads it.
    pub fn credential_vars(self) -> (&'static str, &'static str) {
        match self {
            VenueRealm::Demo => ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"),
            VenueRealm::Mainnet => ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET"),
        }
    }

    /// Whether this realm moves real capital. Used where the distinction is
    /// about money rather than about which host to call.
    pub fn is_real_money(self) -> bool {
        matches!(self, VenueRealm::Mainnet)
    }
}

impl fmt::Display for VenueRealm {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

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
fn flag_value(name: &str, raw: &str) -> Result<bool, VenueError> {
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
///
/// The engine never writes this. It is `REAL_MONEY=true` in the host
/// credential file, set by the account owner.
pub fn real_money_armed() -> Result<bool, VenueError> {
    env_flag(REAL_MONEY_ENV)
}

/// Refuse the realm that the arming switch does not permit.
///
/// Both directions are errors, and both matter:
///
/// - Mainnet without arming is the obvious one — naming the funded account is
///   not on its own authorization to trade it.
/// - Demo *with* arming is the less obvious one. On a host where the owner has
///   armed real money, a demo engine starting anyway means two different
///   answers to "is this host live" are in play at once. That is how an
///   operator ends up reading demo numbers and believing they are the funded
///   account's.
pub fn check_arming(realm: VenueRealm) -> Result<(), VenueError> {
    check_arming_with(realm, real_money_armed()?)
}

/// The rule itself, with the switch supplied rather than read.
///
/// Split out so the whole truth table can be tested without writing to the
/// process environment — which is shared by every test in a binary and racy
/// to mutate from a parallel suite. `tests/arming_env.rs` covers the reading
/// half in a process of its own.
pub fn check_arming_with(realm: VenueRealm, armed: bool) -> Result<(), VenueError> {
    match (realm, armed) {
        (VenueRealm::Mainnet, false) => Err(VenueError::Credentials(format!(
            "the mainnet realm requires {REAL_MONEY_ENV} to be explicitly armed; naming \
             the funded account is not on its own authorization to trade real capital"
        ))),
        (VenueRealm::Demo, true) => Err(VenueError::Credentials(format!(
            "the demo realm refuses to run while {REAL_MONEY_ENV} is armed; unset it, or \
             select the mainnet realm explicitly"
        ))),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_two_realms_parse_and_nothing_else_does() {
        assert_eq!(VenueRealm::parse("demo").unwrap(), VenueRealm::Demo);
        assert_eq!(VenueRealm::parse("mainnet").unwrap(), VenueRealm::Mainnet);
        // Operators type these into env files.
        assert_eq!(VenueRealm::parse("  MainNet \n").unwrap(), VenueRealm::Mainnet);
        for refused in ["", " ", "testnet", "main", "prod", "live", "real", "demo1"] {
            assert!(
                VenueRealm::parse(refused).is_err(),
                "{refused:?} was accepted as a realm"
            );
        }
    }

    #[test]
    fn there_is_no_default_realm() {
        // Stated as a test because "unset means demo" is the single most
        // likely thing for a later change to reintroduce, and it is the bug
        // that puts an unnamed config on some account by accident.
        assert!(VenueRealm::parse("").is_err());
    }

    #[test]
    fn each_realm_has_its_own_hosts_and_its_own_credential_variables() {
        let demo = VenueRealm::Demo;
        let main = VenueRealm::Mainnet;
        assert_ne!(demo.rest_base(), main.rest_base());
        assert_ne!(demo.private_ws(), main.private_ws());
        let (dk, ds) = demo.credential_vars();
        let (mk, ms) = main.credential_vars();
        assert_ne!(dk, mk, "the two realms read the same key variable");
        assert_ne!(ds, ms, "the two realms read the same secret variable");
        // The disjointness that matters: no variable name is shared at all.
        for demo_var in [dk, ds] {
            for main_var in [mk, ms] {
                assert_ne!(demo_var, main_var);
            }
        }
    }

    #[test]
    fn only_mainnet_is_real_money() {
        assert!(VenueRealm::Mainnet.is_real_money());
        assert!(!VenueRealm::Demo.is_real_money());
    }

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
        use VenueRealm::{Demo, Mainnet};
        // Armed: mainnet may run, demo may not.
        assert!(check_arming_with(Mainnet, true).is_ok());
        assert!(check_arming_with(Demo, true).is_err());
        // Unarmed: demo may run, mainnet may not.
        assert!(check_arming_with(Demo, false).is_ok());
        assert!(check_arming_with(Mainnet, false).is_err());
    }

    #[test]
    fn each_refusal_says_which_way_to_fix_it() {
        let unarmed_mainnet = check_arming_with(VenueRealm::Mainnet, false).unwrap_err().to_string();
        assert!(unarmed_mainnet.contains(REAL_MONEY_ENV), "{unarmed_mainnet}");
        assert!(
            unarmed_mainnet.contains("armed"),
            "the mainnet refusal should say the switch is not armed: {unarmed_mainnet}"
        );

        let armed_demo = check_arming_with(VenueRealm::Demo, true).unwrap_err().to_string();
        assert!(armed_demo.contains(REAL_MONEY_ENV), "{armed_demo}");
        assert!(
            armed_demo.contains("unset it"),
            "the demo refusal should say to unset the switch: {armed_demo}"
        );
    }

    #[test]
    fn a_typo_in_the_arming_switch_is_an_error_not_a_false() {
        // The whole point: "ture" must not read as "not armed", because on the
        // demo side that is the value that would let an armed host run demo.
        for typo in ["ture", "yse", "enabled", "y", "2", "-1", "null"] {
            let err = flag_value(REAL_MONEY_ENV, typo).unwrap_err();
            assert!(
                matches!(err, VenueError::Credentials(_)),
                "{typo:?} gave {err:?}"
            );
            assert!(err.to_string().contains(typo), "{err} should quote {typo:?}");
        }
    }
}
