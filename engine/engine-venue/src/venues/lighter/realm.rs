//! Which Lighter network: `lighter_testnet` or `lighter_mainnet`.
//!
//! The only place a Lighter host, chain id, or credential variable is written
//! down.
//!
//! **The chain id is part of the signature.** Every transaction hashes it as
//! its first field, so a testnet-signed order cannot be replayed against
//! mainnet — the same job Hyperliquid's one-letter source byte does. It lives
//! here beside the host for that reason: the two must move together or a
//! signature is made for the wrong network.
//!
//! **The credentials are an account and a key, not a key and a secret.** The
//! "key" variable holds `<account index>:<api key index>` — which of the
//! venue's accounts, and which of the up to 253 API keys registered against it
//! — and the secret is that key's 40-byte private key. Both are needed because
//! a Lighter transaction names the account and the key slot in its own
//! signed fields.

use std::fmt;

use engine_types::VenueError;

use crate::creds::Credentials;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum LighterRealm {
    /// The testnet rollup. Test funds, real matching engine.
    Testnet,
    /// The funded account. Real money.
    Mainnet,
}

impl LighterRealm {
    pub fn parse(value: &str) -> Result<Self, VenueError> {
        match value.trim().to_ascii_lowercase().as_str() {
            "lighter_testnet" => Ok(LighterRealm::Testnet),
            "lighter_mainnet" => Ok(LighterRealm::Mainnet),
            other => Err(VenueError::BadRequest(format!(
                "a Lighter realm is \"lighter_testnet\" or \"lighter_mainnet\"; got {other:?}"
            ))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            LighterRealm::Testnet => "lighter_testnet",
            LighterRealm::Mainnet => "lighter_mainnet",
        }
    }

    pub fn rest_base(self) -> &'static str {
        match self {
            LighterRealm::Testnet => "https://testnet.zklighter.elliot.ai",
            LighterRealm::Mainnet => "https://mainnet.zklighter.elliot.ai",
        }
    }

    /// One socket carries the order books and this account's own updates; they
    /// are separate channels on it.
    pub fn websocket(self) -> &'static str {
        match self {
            LighterRealm::Testnet => "wss://testnet.zklighter.elliot.ai/stream",
            LighterRealm::Mainnet => "wss://mainnet.zklighter.elliot.ai/stream",
        }
    }

    /// The rollup's chain id, signed into every transaction. Not a hostname
    /// and not interchangeable with one: a right host with a wrong chain id
    /// produces signatures the venue refuses, and the refusal does not say so.
    pub fn chain_id(self) -> u32 {
        match self {
            LighterRealm::Testnet => 300,
            LighterRealm::Mainnet => 304,
        }
    }

    pub fn credential_vars(self) -> (&'static str, &'static str) {
        match self {
            LighterRealm::Testnet => ("LIGHTER_TESTNET_ACCOUNT", "LIGHTER_TESTNET_API_KEY_SECRET"),
            LighterRealm::Mainnet => ("LIGHTER_REAL_ACCOUNT", "LIGHTER_REAL_API_KEY_SECRET"),
        }
    }

    pub fn is_real_money(self) -> bool {
        matches!(self, LighterRealm::Mainnet)
    }

    pub fn credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.credential_vars();
        Credentials::from_env(self.as_str(), self.is_real_money(), key_var, secret_var)
    }

    /// Credentials supplied rather than read. Tests and the mock venue only.
    pub fn credentials_for_test(self, account: &str, secret: &str) -> Credentials {
        Credentials::new(self.as_str(), self.is_real_money(), account, secret)
    }
}

impl fmt::Display for LighterRealm {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Which account, and which of its API keys.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AccountKey {
    pub account_index: i64,
    pub api_key_index: u8,
}

impl AccountKey {
    /// Read `<account index>:<api key index>`.
    ///
    /// The key index is refused outside 2..=254: index 0 is reserved for the
    /// venue's desktop client and 1 for its mobile one, and an engine signing
    /// with either would be sharing a nonce sequence with a human's app — the
    /// venue takes each key's nonces strictly in order, so two writers on one
    /// key means one of them is always rejected.
    /// Nothing here quotes the value back. This variable sits beside the API
    /// key secret in the same file, and a message that echoed a mis-pasted one
    /// would carry the key into stderr and the journal.
    pub fn parse(raw: &str) -> Result<Self, VenueError> {
        let (account, key) = raw.trim().split_once(':').ok_or_else(|| {
            VenueError::Credentials(
                "a Lighter account is written \"<account index>:<api key index>\", two whole \
                 numbers with a colon between them"
                    .to_string(),
            )
        })?;
        let account_index: i64 = account.trim().parse().map_err(|_| {
            VenueError::Credentials(
                "the part before the colon is not a whole number, so it is not an account index"
                    .to_string(),
            )
        })?;
        let api_key_index: u8 = key.trim().parse().map_err(|_| {
            VenueError::Credentials(
                "the part after the colon is not a number between 2 and 254, so it is not an \
                 API key slot"
                    .to_string(),
            )
        })?;
        // Above zero, not merely non-negative: the account lease names its
        // file by this number and shares that naming with the Python fleet,
        // whose rule is `> 0`. An index of zero read fine here and then failed
        // at the lease, after the account had already been read.
        if account_index <= 0 {
            return Err(VenueError::Credentials(format!(
                "{account_index} is not an account index; the lease names its file by this \
                 number and will not take a zero or a negative one"
            )));
        }
        if !(2..=254).contains(&api_key_index) {
            return Err(VenueError::Credentials(format!(
                "API key index {api_key_index} is not one a program may use: 0 belongs to the \
                 venue's desktop client and 1 to its mobile one, and nonces are taken strictly \
                 in order per key, so sharing one means one writer is always refused"
            )));
        }
        Ok(AccountKey {
            account_index,
            api_key_index,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn both_realms_parse_and_no_bare_word_does() {
        assert_eq!(LighterRealm::parse("lighter_mainnet").unwrap(), LighterRealm::Mainnet);
        assert_eq!(LighterRealm::parse(" Lighter_Testnet ").unwrap(), LighterRealm::Testnet);
        for refused in ["", "mainnet", "testnet", "lighter", "zklighter"] {
            assert!(LighterRealm::parse(refused).is_err(), "{refused:?}");
        }
    }

    #[test]
    fn each_realm_has_its_own_host_websocket_chain_id_and_variables() {
        let test = LighterRealm::Testnet;
        let main = LighterRealm::Mainnet;
        assert_ne!(test.rest_base(), main.rest_base());
        assert_ne!(test.websocket(), main.websocket());
        assert_ne!(
            test.chain_id(),
            main.chain_id(),
            "one chain id for both networks would let a testnet order replay on the funded one"
        );
        let (ta, tk) = test.credential_vars();
        let (ma, mk) = main.credential_vars();
        for testnet_var in [ta, tk] {
            for mainnet_var in [ma, mk] {
                assert_ne!(testnet_var, mainnet_var);
            }
        }
    }

    #[test]
    fn only_mainnet_is_real_money() {
        assert!(LighterRealm::Mainnet.is_real_money());
        assert!(!LighterRealm::Testnet.is_real_money());
    }

    #[test]
    fn an_account_is_read_as_an_index_and_a_key_slot() {
        let parsed = AccountKey::parse("42:3").unwrap();
        assert_eq!(parsed.account_index, 42);
        assert_eq!(parsed.api_key_index, 3);
        assert_eq!(AccountKey::parse(" 7 : 254 ").unwrap().api_key_index, 254);
    }

    #[test]
    fn the_key_slots_the_venues_own_apps_use_are_refused() {
        // Sharing a key slot with a human's app means sharing its nonce
        // sequence, and the venue takes those strictly in order.
        for shared in ["1:0", "1:1", "1:255"] {
            let refused = AccountKey::parse(shared).unwrap_err();
            assert!(
                refused.to_string().contains("nonces") || refused.to_string().contains("not one"),
                "{refused}"
            );
        }
        // Zero is refused here rather than at the lease, which is where it
        // used to fail — after the credentials had been read and the account
        // addressed.
        for malformed in ["", "42", "42:", ":3", "abc:3", "42:abc", "-1:3", "0:3"] {
            assert!(AccountKey::parse(malformed).is_err(), "{malformed:?} was accepted");
        }
    }

}
