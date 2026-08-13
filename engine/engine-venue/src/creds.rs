//! Demo API credentials, read from the environment.

use std::fmt;

use engine_types::VenueError;

pub const API_KEY_ENV: &str = "BYBIT_DEMO_API_KEY";
pub const API_SECRET_ENV: &str = "BYBIT_DEMO_API_SECRET";

#[derive(Clone)]
pub struct Credentials {
    key: String,
    secret: String,
}

impl Credentials {
    /// Read the demo key and secret from the environment. Missing or blank is
    /// a typed error, never a silent unsigned request.
    pub fn from_env() -> Result<Self, VenueError> {
        Ok(Self {
            key: read(API_KEY_ENV)?,
            secret: read(API_SECRET_ENV)?,
        })
    }

    /// Build credentials directly. Used by tests and by the mock venue; the
    /// live gateway always goes through [`Credentials::from_env`].
    pub fn new(key: impl Into<String>, secret: impl Into<String>) -> Self {
        Self {
            key: key.into(),
            secret: secret.into(),
        }
    }

    pub fn key(&self) -> &str {
        &self.key
    }

    pub(crate) fn secret(&self) -> &str {
        &self.secret
    }
}

/// Redacted on purpose: this struct ends up inside error and trace context,
/// and half a redaction is one derive(Debug) away from a key in the log.
impl fmt::Debug for Credentials {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let tail: String = self.key.chars().rev().take(4).collect::<Vec<_>>().into_iter().rev().collect();
        f.debug_struct("Credentials")
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
        let creds = Credentials::new("key-12345", "super-secret");
        let shown = format!("{creds:?}");
        assert!(!shown.contains("key-12345"), "the full key leaked: {shown}");
        assert!(shown.contains("2345"), "the tail identifies the key: {shown}");
        assert!(!shown.contains("super-secret"));
    }

    #[test]
    fn blank_env_value_is_a_credentials_error() {
        // read() is the whole of from_env; drive it directly so the test
        // never depends on the ambient environment.
        let err = read("ENGINE_VENUE_DEFINITELY_UNSET_VAR").unwrap_err();
        assert!(matches!(err, VenueError::Credentials(_)));
    }
}
