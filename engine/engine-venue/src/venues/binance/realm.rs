//! Which Binance USDT-perpetual endpoint a credential trades on:
//! `binance_testnet` or `binance_mainnet`.
//!
//! The only place a Binance realm becomes a hostname or an environment
//! variable name. Two realms, and the difference between them is real money.
//!
//! **The testnet hosts are the ones the venue documents today.** Binance's
//! docs name the demo-prefixed pair below for futures testing; the venue's
//! earlier practice hosts (on the `binancefuture` domain) still answer,
//! byte-identically and against the same backend, with no deprecation header.
//! Like MEXC's retired REST host, an adapter built on them would pass every
//! test and die whenever the cutoff is enforced, so this table names only the
//! documented pair and the fence test forbids the old ones outright.
//!
use std::fmt;

use engine_types::VenueError;

use crate::creds::Credentials;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum BinanceRealm {
    /// The futures testnet: test funds on a real matching engine, and it
    /// carries the full private stream — which is what earns it canary duty.
    Testnet,
    /// The funded account. Real money.
    Mainnet,
}

impl BinanceRealm {
    pub fn parse(value: &str) -> Result<Self, VenueError> {
        match value.trim().to_ascii_lowercase().as_str() {
            "binance_testnet" => Ok(BinanceRealm::Testnet),
            "binance_mainnet" => Ok(BinanceRealm::Mainnet),
            other => Err(VenueError::BadRequest(format!(
                "a Binance realm is \"binance_testnet\" or \"binance_mainnet\"; got {other:?}"
            ))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            BinanceRealm::Testnet => "binance_testnet",
            BinanceRealm::Mainnet => "binance_mainnet",
        }
    }

    /// The REST host, public and signed calls alike.
    pub fn rest_base(self) -> &'static str {
        match self {
            BinanceRealm::Testnet => "https://demo-fapi.binance.com",
            BinanceRealm::Mainnet => "https://fapi.binance.com",
        }
    }

    /// The websocket host, market streams and the user-data stream alike.
    /// A bare host: each feed appends its current public, market, or private
    /// route.
    pub fn websocket(self) -> &'static str {
        match self {
            BinanceRealm::Testnet => "wss://demo-fstream.binance.com",
            BinanceRealm::Mainnet => "wss://fstream.binance.com",
        }
    }

    /// This realm's key and secret. Disjoint from every other realm's, of
    /// this venue and every other, so a key left on a host for one account
    /// cannot authenticate another.
    pub fn credential_vars(self) -> (&'static str, &'static str) {
        match self {
            BinanceRealm::Testnet => ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
            BinanceRealm::Mainnet => ("BINANCE_REAL_API_KEY", "BINANCE_REAL_API_SECRET"),
        }
    }

    pub fn is_real_money(self) -> bool {
        matches!(self, BinanceRealm::Mainnet)
    }

    pub fn credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.credential_vars();
        Credentials::from_env(self.as_str(), self.is_real_money(), key_var, secret_var)
    }

    /// Credentials supplied rather than read. Tests only; the live path is
    /// [`BinanceRealm::credentials`], which is the one that checks arming.
    pub fn credentials_for_test(self, key: &str, secret: &str) -> Credentials {
        Credentials::new(self.as_str(), self.is_real_money(), key, secret)
    }
}

impl fmt::Display for BinanceRealm {
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
            BinanceRealm::parse("binance_testnet").unwrap(),
            BinanceRealm::Testnet
        );
        assert_eq!(
            BinanceRealm::parse(" Binance_MainNet \n").unwrap(),
            BinanceRealm::Mainnet
        );
        // `mainnet` alone is Bybit's realm. Accepting it here would put two
        // venues on one name in the heartbeat every producer reads.
        for refused in ["", "mainnet", "testnet", "demo", "binance", "binance_demo"] {
            assert!(
                BinanceRealm::parse(refused).is_err(),
                "{refused:?} was accepted as a Binance realm"
            );
        }
    }

    #[test]
    fn each_realm_has_its_own_hosts_and_its_own_variables() {
        let test = BinanceRealm::Testnet;
        let main = BinanceRealm::Mainnet;
        assert_ne!(test.rest_base(), main.rest_base());
        assert_ne!(test.websocket(), main.websocket());
        let (tk, ts) = test.credential_vars();
        let (mk, ms) = main.credential_vars();
        for testnet_var in [tk, ts] {
            for mainnet_var in [mk, ms] {
                assert_ne!(testnet_var, mainnet_var);
            }
        }
        assert_ne!(tk, ts);
        assert_ne!(mk, ms);
    }

    #[test]
    fn only_mainnet_is_real_money_and_the_spelling_says_so() {
        assert!(BinanceRealm::Mainnet.is_real_money());
        assert!(BinanceRealm::Mainnet.as_str().contains("mainnet"));
        assert!(!BinanceRealm::Testnet.is_real_money());
        assert!(!BinanceRealm::Testnet.as_str().contains("main"));
    }

    #[test]
    fn rest_is_https_and_the_socket_is_wss_on_the_documented_hosts() {
        for realm in [BinanceRealm::Testnet, BinanceRealm::Mainnet] {
            assert!(realm.rest_base().starts_with("https://"), "{realm}");
            assert!(realm.websocket().starts_with("wss://"), "{realm}");
            assert!(
                !realm.websocket().ends_with('/'),
                "the socket is a bare host the feeds append their paths to"
            );
        }
        assert!(BinanceRealm::Testnet.rest_base().contains("demo-"));
        assert!(BinanceRealm::Testnet.websocket().contains("demo-"));
    }

    #[test]
    fn the_retired_practice_hosts_are_not_reachable_from_this_table() {
        // The venue's earlier practice domain still answers, byte-identically
        // and with no deprecation header, so nothing at runtime would notice a
        // fallback to it until a cutoff is enforced.
        for realm in [BinanceRealm::Testnet, BinanceRealm::Mainnet] {
            assert!(!realm.rest_base().contains("binancefuture"));
            assert!(!realm.websocket().contains("binancefuture"));
        }
    }
}
