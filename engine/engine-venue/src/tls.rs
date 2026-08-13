//! Pick the TLS crypto provider explicitly.
//!
//! rustls infers a provider from crate features, and gives up with a panic
//! when more than one is compiled in. That is not hypothetical here: another
//! crate in the workspace pulls in `aws-lc-rs`, and feature unification then
//! leaves rustls with two providers and no way to choose. The panic lands
//! inside the HTTPS client constructor — that is, on the order path.
//!
//! So name the provider instead of letting rustls guess. Installing it is
//! idempotent by intent: if something else got there first, any working
//! provider will do, and the point is only that one is chosen.

use std::sync::Once;

pub(crate) fn install_crypto_provider() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_provider_is_available_after_install() {
        install_crypto_provider();
        install_crypto_provider();
        assert!(
            rustls::crypto::CryptoProvider::get_default().is_some(),
            "no provider installed, so building a TLS config would panic"
        );
    }
}
