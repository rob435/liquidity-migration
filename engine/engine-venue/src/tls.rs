//! Pick the TLS crypto provider explicitly.
//!
//! rustls infers a provider from crate features, and gives up with a panic
//! when more than one is compiled in. The panic lands inside the HTTPS client
//! constructor — that is, on the order path. It was not hypothetical: a
//! dev-dependency chain pulled in `aws-lc-rs` beside ring until 2026-08-20,
//! when the unused rcgen/tokio-rustls declarations left engine-core and took
//! `aws-lc-rs` out of the graph. One provider is compiled in today; a single
//! dev-dependency with default features is enough to bring the second back,
//! which is why this pin stays.
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
