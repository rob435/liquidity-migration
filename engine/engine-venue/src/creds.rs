//! API credentials, read from the environment, bound to their realm.
//!
//! A `Credentials` value knows which account it authenticates against. That is
//! not decoration: the REST client and the order stream both derive their host
//! from it, so a key and a hostname cannot drift apart. There is no
//! constructor that takes a key without also taking the realm it belongs to.

use std::fmt;

use engine_types::VenueError;

use crate::realm::{check_arming, VenueRealm};

/// The demo credential variables, re-exported for the operator-facing checks
/// that name them. The authority is [`VenueRealm::credential_vars`].
pub const API_KEY_ENV: &str = "BYBIT_DEMO_API_KEY";
pub const API_SECRET_ENV: &str = "BYBIT_DEMO_API_SECRET";

#[derive(Clone)]
pub struct Credentials {
    realm: VenueRealm,
    key: String,
    secret: String,
}

impl Credentials {
    /// Read one realm's key and secret from the environment.
    ///
    /// The arming check runs *first*, before either variable is read, so a
    /// host with mainnet keys sitting in its environment still refuses to
    /// build them into anything while `REAL_MONEY` is unset. Missing or blank
    /// is a typed error, never a silent unsigned request.
    pub fn from_env(realm: VenueRealm) -> Result<Self, VenueError> {
        check_arming(realm)?;
        let (key_var, secret_var) = realm.credential_vars();
        Ok(Self {
            realm,
            key: read(key_var)?,
            secret: read(secret_var)?,
        })
    }

    /// Build credentials directly. Tests and the mock venue only; the live
    /// path is [`Credentials::from_env`], which is the one that checks arming.
    pub fn new(realm: VenueRealm, key: impl Into<String>, secret: impl Into<String>) -> Self {
        Self {
            realm,
            key: key.into(),
            secret: secret.into(),
        }
    }

    /// Which account these authenticate against.
    pub fn realm(&self) -> VenueRealm {
        self.realm
    }

    pub fn key(&self) -> &str {
        &self.key
    }

    pub(crate) fn secret(&self) -> &str {
        &self.secret
    }
}

/// Redacted on purpose: this struct ends up inside error and trace context,
/// and half a redaction is one derive(Debug) away from a key in the log. The
/// realm is shown in full — it is the thing you most want in a log line, and
/// it is not a secret.
impl fmt::Debug for Credentials {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let tail: String = self.key.chars().rev().take(4).collect::<Vec<_>>().into_iter().rev().collect();
        f.debug_struct("Credentials")
            .field("realm", &self.realm.as_str())
            .field("key", &format!("<redacted>..{tail}"))
            .field("secret", &"<redacted>")
            .finish()
    }
}

fn read(var: &str) -> Result<String, VenueError> {
    match std::env::var(var) {
        Ok(v) if !v.trim().is_empty() => Ok(v),
        Ok(_) => Err(VenueError::Credentials(format!("{var} is empty"))),
        Err(_) => Err(VenueError::Credentials(format!("{var} is not set"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_hides_the_secret_and_all_but_the_keys_tail() {
        let creds = Credentials::new(VenueRealm::Demo, "key-12345", "super-secret");
        let shown = format!("{creds:?}");
        assert!(!shown.contains("key-12345"), "the full key leaked: {shown}");
        assert!(shown.contains("2345"), "the tail identifies the key: {shown}");
        assert!(!shown.contains("super-secret"));
    }

    #[test]
    fn debug_names_the_realm_so_a_log_line_says_which_account() {
        let shown = format!("{:?}", Credentials::new(VenueRealm::Mainnet, "k", "s"));
        assert!(shown.contains("mainnet"), "{shown}");
    }

    #[test]
    fn credentials_carry_the_realm_they_were_built_for() {
        assert_eq!(
            Credentials::new(VenueRealm::Mainnet, "k", "s").realm(),
            VenueRealm::Mainnet
        );
    }

    #[test]
    fn blank_env_value_is_a_credentials_error() {
        // read() is the whole of from_env past the arming check; drive it
        // directly so the test never depends on the ambient environment.
        let err = read("ENGINE_VENUE_DEFINITELY_UNSET_VAR").unwrap_err();
        assert!(matches!(err, VenueError::Credentials(_)));
    }

    #[test]
    fn the_exported_demo_variable_names_match_the_realm_table() {
        assert_eq!(
            VenueRealm::Demo.credential_vars(),
            (API_KEY_ENV, API_SECRET_ENV)
        );
    }
}
