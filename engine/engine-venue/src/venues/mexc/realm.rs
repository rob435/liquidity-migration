//! Which MEXC endpoint: there is one, `mexc_mainnet`.
//!
//! **One realm, and it is real money.** MEXC runs a demo-trading mode inside
//! its own app, but publishes no testnet host for the futures API — there is
//! no practice endpoint to point this engine at. So unlike Bybit, Hyperliquid
//! and Lighter, every MEXC name here reaches funded capital, and the engine
//! refuses to build one unless the owner has armed `REAL_MONEY` on the host.
//!
//! That has a consequence worth stating plainly: nothing in this adapter can
//! be exercised against the venue without risking real money. Its request
//! shapes are pinned against recorded bytes, not against a practice account.

use std::fmt;

use engine_types::VenueError;

use crate::creds::Credentials;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum MexcRealm {
    /// The production futures endpoint. The only one MEXC publishes.
    Mainnet,
}

impl MexcRealm {
    pub fn parse(value: &str) -> Result<Self, VenueError> {
        match value.trim().to_ascii_lowercase().as_str() {
            "mexc_mainnet" => Ok(MexcRealm::Mainnet),
            other => Err(VenueError::BadRequest(format!(
                "the only MEXC realm is \"mexc_mainnet\"; got {other:?}"
            ))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            MexcRealm::Mainnet => "mexc_mainnet",
        }
    }

    /// The futures REST host. The only MEXC host this engine knows.
    pub fn rest_base(self) -> &'static str {
        match self {
            MexcRealm::Mainnet => "https://contract.mexc.com",
        }
    }

    /// The futures websocket. Same host as REST.
    pub fn websocket(self) -> &'static str {
        match self {
            MexcRealm::Mainnet => "wss://contract.mexc.com/edge",
        }
    }

    /// This realm's key and secret. Disjoint from every other venue's, so a
    /// key left on a host for one account cannot authenticate another.
    pub fn credential_vars(self) -> (&'static str, &'static str) {
        match self {
            MexcRealm::Mainnet => ("MEXC_REAL_API_KEY", "MEXC_REAL_API_SECRET"),
        }
    }

    /// Always. There is no MEXC futures API endpoint that is not real money.
    pub fn is_real_money(self) -> bool {
        true
    }

    pub fn credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.credential_vars();
        Credentials::from_env(self.as_str(), self.is_real_money(), key_var, secret_var)
    }

    /// Credentials supplied rather than read. Tests only; the live path is
    /// [`MexcRealm::credentials`], which is the one that checks arming.
    pub fn credentials_for_test(self, key: &str, secret: &str) -> Credentials {
        Credentials::new(self.as_str(), self.is_real_money(), key, secret)
    }
}

impl fmt::Display for MexcRealm {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_one_realm_parses_and_no_bare_word_does() {
        assert_eq!(MexcRealm::parse("mexc_mainnet").unwrap(), MexcRealm::Mainnet);
        assert_eq!(MexcRealm::parse(" Mexc_MainNet ").unwrap(), MexcRealm::Mainnet);
        for refused in ["", "mainnet", "mexc", "mexc_testnet", "mexc_demo"] {
            assert!(
                MexcRealm::parse(refused).is_err(),
                "{refused:?} was accepted as a MEXC realm"
            );
        }
    }

    #[test]
    fn there_is_no_practice_realm_because_the_venue_publishes_no_practice_host() {
        // Stated as a test so a later addition is a deliberate one. MEXC's
        // demo trading lives in its app, not behind an API host; inventing one
        // here would point the engine at nothing.
        assert!(MexcRealm::parse("mexc_testnet").is_err());
        assert!(MexcRealm::parse("mexc_demo").is_err());
    }

    #[test]
    fn the_only_realm_is_real_money_and_says_so() {
        // Every other venue in this engine has a practice realm. This one does
        // not, so there is no spelling of MEXC that is safe to run unarmed.
        assert!(MexcRealm::Mainnet.is_real_money());
        assert!(MexcRealm::Mainnet.as_str().contains("mainnet"));
    }

    #[test]
    fn the_credential_variables_are_this_venues_own() {
        let (key, secret) = MexcRealm::Mainnet.credential_vars();
        assert_eq!(key, "MEXC_REAL_API_KEY");
        assert_eq!(secret, "MEXC_REAL_API_SECRET");
        assert_ne!(key, secret);
    }
}
