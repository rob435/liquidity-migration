//! Which Variational endpoint: there is one, `variational_mainnet`.
//!
//! **One realm, and no credentials.** Variational publishes a read-only market
//! statistics API and nothing else — no testnet host, and no trading endpoints
//! at all (their own documentation says the trading API "is still in
//! development, and is not yet available to any users"). So there is one host
//! here, it needs no key, and the adapter that uses it cannot place an order.
//!
//! **Why the `REAL_MONEY` law is not checked here.** On the other three venues
//! the arming check runs inside the credential read, because a credential is
//! what makes an order possible. This venue reads no credential and can send
//! no order — [`super::gateway`] refuses every write, and a test pins that —
//! so there is nothing for arming to protect. The moment a trading API exists
//! it arrives as a credential read through [`VariationalRealm::credentials`],
//! which checks arming like every other venue's, and a real-money realm will
//! be refused on an unarmed host exactly as Bybit mainnet is.

use std::fmt;

use engine_types::VenueError;

use crate::creds::Credentials;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum VariationalRealm {
    /// The production endpoint. Read-only today.
    Mainnet,
}

impl VariationalRealm {
    pub fn parse(value: &str) -> Result<Self, VenueError> {
        match value.trim().to_ascii_lowercase().as_str() {
            "variational_mainnet" => Ok(VariationalRealm::Mainnet),
            other => Err(VenueError::BadRequest(format!(
                "the only Variational realm is \"variational_mainnet\"; got {other:?}"
            ))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            VariationalRealm::Mainnet => "variational_mainnet",
        }
    }

    /// The public client API. The whole of it, as things stand.
    pub fn rest_base(self) -> &'static str {
        match self {
            VariationalRealm::Mainnet => {
                "https://omni-client-api.prod.ap-northeast-1.variational.io"
            }
        }
    }

    /// The variables a signed API would read. Named now so the shape matches
    /// the other three venues and so nothing has to be invented later under
    /// pressure; nothing reads them yet.
    pub fn credential_vars(self) -> (&'static str, &'static str) {
        match self {
            VariationalRealm::Mainnet => {
                ("VARIATIONAL_REAL_API_KEY", "VARIATIONAL_REAL_API_SECRET")
            }
        }
    }

    pub fn is_real_money(self) -> bool {
        // The production venue. It says nothing about whether an order can be
        // sent today — it cannot — only about which endpoint this is.
        true
    }

    /// Credentials from the environment, arming checked. Nothing calls this
    /// yet: there is no endpoint to sign for.
    pub fn credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.credential_vars();
        Credentials::from_env(self.as_str(), self.is_real_money(), key_var, secret_var)
    }
}

impl fmt::Display for VariationalRealm {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_one_realm_parses_and_no_bare_word_does() {
        assert_eq!(
            VariationalRealm::parse("variational_mainnet").unwrap(),
            VariationalRealm::Mainnet
        );
        assert_eq!(
            VariationalRealm::parse(" Variational_Mainnet ").unwrap(),
            VariationalRealm::Mainnet
        );
        for refused in ["", "mainnet", "variational", "variational_testnet"] {
            assert!(
                VariationalRealm::parse(refused).is_err(),
                "{refused:?} was accepted as a Variational realm"
            );
        }
    }

    #[test]
    fn there_is_no_testnet_realm_because_the_venue_publishes_no_testnet() {
        // Stated as a test so a later addition is a deliberate one: inventing
        // a host to fill the shape would point the engine at nothing.
        assert!(VariationalRealm::parse("variational_testnet").is_err());
    }
}
