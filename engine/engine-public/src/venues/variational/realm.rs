//! Non-secret variational realm metadata.

use std::fmt;

use engine_types::VenueError;

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
