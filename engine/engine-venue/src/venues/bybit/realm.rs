//! Which Bybit account a credential authenticates against: `demo` or
//! `mainnet`.
//!
//! This is the one place in the engine where a Bybit realm becomes a hostname
//! and a pair of credential variable names. Everything else carries the realm
//! and asks here, so there is a single answer to "where do real orders go" and
//! a single place to read it.
//!
//! The arming and credential rules live entirely in this Rust engine. Python's
//! `liquidity_migration/core/venue_realm.py` names public-data endpoints only:
//!
//! - **A realm is always named.** There is no default. A config that does not
//!   say which account it trades does not get one picked for it.
//! - **The two realms read different credential variables.** A key left in the
//!   environment for one realm can never authenticate the other.
//! - **`REAL_MONEY` is the arming switch, and it cuts both ways.** The rule
//!   itself lives in [`crate::arming`], shared by every venue.
//! - **Testnet does not exist here.** It is not a Bybit realm, and its hosts
//!   are forbidden by `tests/venue_fence.rs`. The other three venues have no
//!   play-money account of Bybit's kind, so their practice realm *is* their
//!   testnet — which is their own realm table's business, not this one's.
//!
//! Naming a realm is not permission to trade it. `REAL_MONEY=true` lives in
//! the host credential file and is set by the account owner's own hand.

use std::fmt;

use engine_types::VenueError;

use crate::creds::Credentials;

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

    /// WebSocket order entry. Bybit exposes it on mainnet only; demo keeps
    /// the signed REST path.
    pub fn trade_ws(self) -> Option<&'static str> {
        match self {
            VenueRealm::Demo => None,
            VenueRealm::Mainnet => Some("wss://stream.bybit.com/v5/trade"),
        }
    }

    /// Public market data. One stream serves both realms: Bybit runs no
    /// separate demo price feed, and the demo account matches against these
    /// same prices. Written here rather than in the market-data crate so that
    /// every Bybit host this engine knows is still in one file.
    pub fn public_ws(self) -> &'static str {
        "wss://stream.bybit.com/v5/public/linear"
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

    /// Credentials available to the deployment attestor. Mainnet uses a
    /// physically separate read-only API key, so an inventory proof never
    /// receives the execution key merely because it needs signed reads.
    pub fn inventory_credential_vars(self) -> (&'static str, &'static str) {
        match self {
            VenueRealm::Demo => self.credential_vars(),
            VenueRealm::Mainnet => ("BYBIT_ATTEST_API_KEY", "BYBIT_ATTEST_API_SECRET"),
        }
    }

    /// Whether this realm moves real capital. Used where the distinction is
    /// about money rather than about which host to call.
    pub fn is_real_money(self) -> bool {
        matches!(self, VenueRealm::Mainnet)
    }

    /// This realm's credentials, read from the environment. The arming check
    /// runs inside, before either variable is read.
    pub fn credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.credential_vars();
        Credentials::from_env(self.as_str(), self.is_real_money(), key_var, secret_var)
    }

    /// Credentials for the read-only deployment inventory capability. This
    /// may read a disarmed funded account, but the resulting type has no send
    /// or mutation methods.
    pub(crate) fn inventory_credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.inventory_credential_vars();
        Credentials::from_env_read_only(self.as_str(), self.is_real_money(), key_var, secret_var)
    }

    /// Credentials for this realm, supplied rather than read. Tests and the
    /// mock venue only; the live path is [`VenueRealm::credentials`], which is
    /// the one that checks arming.
    pub fn credentials_for_test(self, key: &str, secret: &str) -> Credentials {
        Credentials::new(self.as_str(), self.is_real_money(), key, secret)
    }
}

impl fmt::Display for VenueRealm {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
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
        assert_eq!(
            VenueRealm::parse("  MainNet \n").unwrap(),
            VenueRealm::Mainnet
        );
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
        let (ak, as_) = main.inventory_credential_vars();
        assert_eq!(ak, "BYBIT_ATTEST_API_KEY");
        assert_eq!(as_, "BYBIT_ATTEST_API_SECRET");
        assert_ne!(ak, mk);
        assert_ne!(as_, ms);
    }

    #[test]
    fn both_realms_price_against_the_same_public_stream() {
        // Not an oversight: Bybit publishes no demo price feed, and the demo
        // account matches against the mainnet book. A separate host here
        // would be one that does not exist.
        assert_eq!(
            VenueRealm::Demo.public_ws(),
            VenueRealm::Mainnet.public_ws()
        );
    }

    #[test]
    fn only_mainnet_is_real_money() {
        assert!(VenueRealm::Mainnet.is_real_money());
        assert!(!VenueRealm::Demo.is_real_money());
    }
}
