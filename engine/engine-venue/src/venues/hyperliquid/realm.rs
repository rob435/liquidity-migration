//! Which Hyperliquid network a credential trades on: `hyperliquid_testnet` or
//! `hyperliquid_mainnet`.
//!
//! The only place a Hyperliquid realm becomes a hostname or a credential
//! variable name. Two networks, and the difference between them is real money.
//!
//! **Why the realm names carry the venue.** Bybit's realms are spelled `demo`
//! and `mainnet` because those strings are a contract with the Python fleet,
//! written since before there was a second venue. The three venues that came
//! after cannot borrow those spellings: a realm string travels in the engine's
//! heartbeat and the producers refuse to size from a heartbeat whose realm is
//! not the one they were told to trade. Two venues both saying `mainnet` would
//! let one venue's heartbeat pass the other's check.
//!
//! **Hyperliquid has no play-money account of Bybit's kind.** Its practice
//! realm is its testnet, which is a separate chain with its own funds. So
//! testnet is what the `REAL_MONEY` law calls a practice realm, and it refuses
//! to run on an armed host exactly as Bybit demo does.
//!
//! **The two credential variables are not a key and a secret.** They are the
//! account's address and the private key of an *API wallet* (the venue calls
//! it an agent) that the account owner has approved to trade for it. An API
//! wallet can place and cancel orders and cannot withdraw, so the key on the
//! host is not a key to the funds. The address is needed as well as the key
//! because orders are signed by the agent but every account read is addressed
//! to the account itself.

use std::fmt;

use engine_types::VenueError;

use crate::creds::Credentials;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum HyperliquidRealm {
    /// The testnet chain. Test funds, real matching engine.
    Testnet,
    /// The funded account. Real money.
    Mainnet,
}

impl HyperliquidRealm {
    pub fn parse(value: &str) -> Result<Self, VenueError> {
        match value.trim().to_ascii_lowercase().as_str() {
            "hyperliquid_testnet" => Ok(HyperliquidRealm::Testnet),
            "hyperliquid_mainnet" => Ok(HyperliquidRealm::Mainnet),
            other => Err(VenueError::BadRequest(format!(
                "a Hyperliquid realm is \"hyperliquid_testnet\" or \"hyperliquid_mainnet\"; \
                 got {other:?}"
            ))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            HyperliquidRealm::Testnet => "hyperliquid_testnet",
            HyperliquidRealm::Mainnet => "hyperliquid_mainnet",
        }
    }

    /// The REST host. Both the public `/info` reads and the signed `/exchange`
    /// writes go here — Hyperliquid has one host, not two.
    pub fn rest_base(self) -> &'static str {
        match self {
            HyperliquidRealm::Testnet => "https://api.hyperliquid-testnet.xyz",
            HyperliquidRealm::Mainnet => "https://api.hyperliquid.xyz",
        }
    }

    /// One socket carries both public market data and this account's own order
    /// and fill updates; they are separate subscriptions on it.
    pub fn websocket(self) -> &'static str {
        match self {
            HyperliquidRealm::Testnet => "wss://api.hyperliquid-testnet.xyz/ws",
            HyperliquidRealm::Mainnet => "wss://api.hyperliquid.xyz/ws",
        }
    }

    /// The account address, and the API wallet key that signs for it.
    /// Disjoint between realms, and disjoint from every other venue's.
    pub fn credential_vars(self) -> (&'static str, &'static str) {
        match self {
            HyperliquidRealm::Testnet => (
                "HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS",
                "HYPERLIQUID_TESTNET_API_WALLET_KEY",
            ),
            HyperliquidRealm::Mainnet => (
                "HYPERLIQUID_REAL_ACCOUNT_ADDRESS",
                "HYPERLIQUID_REAL_API_WALLET_KEY",
            ),
        }
    }

    pub fn is_real_money(self) -> bool {
        matches!(self, HyperliquidRealm::Mainnet)
    }

    /// Whether the signature says `mainnet`. Signed into every action, and the
    /// only thing that stops a testnet-signed order being replayed against the
    /// funded account.
    pub fn signs_as_mainnet(self) -> bool {
        self.is_real_money()
    }

    pub fn credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.credential_vars();
        Credentials::from_env(self.as_str(), self.is_real_money(), key_var, secret_var)
    }

    /// Credentials supplied rather than read. Tests and the mock venue only.
    pub fn credentials_for_test(self, address: &str, wallet_key: &str) -> Credentials {
        Credentials::new(self.as_str(), self.is_real_money(), address, wallet_key)
    }
}

impl fmt::Display for HyperliquidRealm {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn both_realms_parse_and_no_bare_word_does() {
        assert_eq!(
            HyperliquidRealm::parse("hyperliquid_mainnet").unwrap(),
            HyperliquidRealm::Mainnet
        );
        assert_eq!(
            HyperliquidRealm::parse(" HyperLiquid_Testnet \n").unwrap(),
            HyperliquidRealm::Testnet
        );
        // `mainnet` alone is Bybit's realm. Accepting it here would put two
        // venues on one name in the heartbeat every producer reads.
        for refused in ["", "mainnet", "testnet", "demo", "hyperliquid", "hl_mainnet"] {
            assert!(
                HyperliquidRealm::parse(refused).is_err(),
                "{refused:?} was accepted as a Hyperliquid realm"
            );
        }
    }

    #[test]
    fn each_realm_has_its_own_hosts_and_its_own_credential_variables() {
        let test = HyperliquidRealm::Testnet;
        let main = HyperliquidRealm::Mainnet;
        assert_ne!(test.rest_base(), main.rest_base());
        assert_ne!(test.websocket(), main.websocket());
        let (ta, tk) = test.credential_vars();
        let (ma, mk) = main.credential_vars();
        for testnet_var in [ta, tk] {
            for mainnet_var in [ma, mk] {
                assert_ne!(testnet_var, mainnet_var);
            }
        }
    }

    #[test]
    fn only_mainnet_is_real_money_and_only_mainnet_signs_as_mainnet() {
        assert!(HyperliquidRealm::Mainnet.is_real_money());
        assert!(HyperliquidRealm::Mainnet.signs_as_mainnet());
        assert!(!HyperliquidRealm::Testnet.is_real_money());
        assert!(!HyperliquidRealm::Testnet.signs_as_mainnet());
    }

}
