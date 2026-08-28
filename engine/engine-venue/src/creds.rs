//! API credentials, read from the environment, bound to their realm.
//!
//! A `Credentials` value knows which realm it authenticates against and
//! whether that realm moves real money. That is not decoration: the arming
//! check runs off it, and the REST client and the order stream both derive
//! their host from the same realm, so a key and a hostname cannot drift apart.
//! There is no constructor that takes a key without also taking the realm it
//! belongs to.
//!
//! What the two strings *mean* is the venue's business, and the four disagree:
//! Bybit's pair is an API key and an HMAC secret; Hyperliquid's is the account
//! address and an API-wallet private key; Lighter's is an account and API-key
//! index and a curve private key. This module holds them, checks they are
//! there, and keeps the secret out of every log line.

use std::fmt;

use engine_types::VenueError;

use crate::arming::check_arming;

#[derive(Clone)]
pub struct Credentials {
    realm: String,
    real_money: bool,
    key: String,
    secret: String,
}

impl Credentials {
    /// Read one realm's key and secret from the environment.
    ///
    /// The arming check runs *first*, before either variable is read, so a
    /// host with real-money keys sitting in its environment still refuses to
    /// build them into anything while `REAL_MONEY` is unset. Missing or blank
    /// is a typed error, never a silent unsigned request.
    pub fn from_env(
        realm: &str,
        real_money: bool,
        key_var: &str,
        secret_var: &str,
    ) -> Result<Self, VenueError> {
        check_arming(realm, real_money)?;
        Ok(Self {
            realm: realm.to_string(),
            real_money,
            key: read(key_var)?,
            secret: read(secret_var)?,
        })
    }

    /// Read credentials for a capability that cannot send or mutate.
    ///
    /// This deliberately does not consult `REAL_MONEY`: a stopped, disarmed
    /// funded account still has to be proved flat before a shared binary can
    /// cross a generation boundary. The constructor is crate-private, and its
    /// only live caller wraps the credentials in `InventoryProbe`, whose
    /// public API exposes identity and inventory reads only.
    pub(crate) fn from_env_read_only(
        realm: &str,
        real_money: bool,
        key_var: &str,
        secret_var: &str,
    ) -> Result<Self, VenueError> {
        Ok(Self {
            realm: realm.to_string(),
            real_money,
            key: read(key_var)?,
            secret: read(secret_var)?,
        })
    }

    /// Build credentials directly. Tests and the mock venue only; the live
    /// path is [`Credentials::from_env`], which is the one that checks arming.
    pub fn new(
        realm: &str,
        real_money: bool,
        key: impl Into<String>,
        secret: impl Into<String>,
    ) -> Self {
        Self {
            realm: realm.to_string(),
            real_money,
            key: key.into(),
            secret: secret.into(),
        }
    }

    /// Which realm these authenticate against.
    pub fn realm(&self) -> &str {
        &self.realm
    }

    /// Whether that realm moves real capital.
    pub fn is_real_money(&self) -> bool {
        self.real_money
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
        let tail: String = self
            .key
            .chars()
            .rev()
            .take(4)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        f.debug_struct("Credentials")
            .field("realm", &self.realm)
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
        let creds = Credentials::new("demo", false, "key-12345", "super-secret");
        let shown = format!("{creds:?}");
        assert!(!shown.contains("key-12345"), "the full key leaked: {shown}");
        assert!(
            shown.contains("2345"),
            "the tail identifies the key: {shown}"
        );
        assert!(!shown.contains("super-secret"));
    }

    #[test]
    fn debug_names_the_realm_so_a_log_line_says_which_account() {
        let shown = format!("{:?}", Credentials::new("mainnet", true, "k", "s"));
        assert!(shown.contains("mainnet"), "{shown}");
    }

    #[test]
    fn credentials_carry_the_realm_they_were_built_for() {
        let creds = Credentials::new("mainnet", true, "k", "s");
        assert_eq!(creds.realm(), "mainnet");
        assert!(creds.is_real_money());
        assert!(!Credentials::new("demo", false, "k", "s").is_real_money());
    }

    #[test]
    fn blank_env_value_is_a_credentials_error() {
        // read() is the whole of from_env past the arming check; drive it
        // directly so the test never depends on the ambient environment.
        let err = read("ENGINE_VENUE_DEFINITELY_UNSET_VAR").unwrap_err();
        assert!(matches!(err, VenueError::Credentials(_)));
    }
}
