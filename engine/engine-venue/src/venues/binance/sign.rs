//! Binance futures request signing.
//!
//! The signed string is what the venue calls `totalParams`: the query string
//! concatenated with the request body. This adapter sends every parameter in
//! the query string — the venue documents that POST, PUT and DELETE may — so
//! the signed string IS the query string, byte for byte as it goes out, and
//! the signature is appended to it as the final `signature` parameter. It is
//! a lowercase hex HMAC-SHA256 under the API secret; the API key itself rides
//! in the `X-MBX-APIKEY` header and is not part of the signed string.
//!
//! Values are percent-encoded before signing, because the signature covers
//! the encoded bytes on the wire — a JSON list parameter (the batch
//! endpoints) signed unencoded would authenticate against a different string
//! than the venue reads.

use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;

use crate::http::percent_encode;

pub(crate) const HEADER_KEY: &str = "X-MBX-APIKEY";

/// Milliseconds after `timestamp` the request stays valid. The venue's
/// default; its maximum is 60000.
pub(crate) const RECV_WINDOW_MS: &str = "5000";

/// The query string exactly as it is sent and signed: `k=v` joined by `&`,
/// in the order given, values percent-encoded. No sorting — the venue signs
/// whatever order it receives, and one builder keeps sent and signed equal.
pub(crate) fn query_string(pairs: &[(&str, String)]) -> String {
    pairs
        .iter()
        .map(|(key, value)| format!("{key}={}", percent_encode(value)))
        .collect::<Vec<_>>()
        .join("&")
}

pub(crate) fn hmac_sha256_hex(secret: &str, message: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
        .expect("HMAC-SHA256 takes a key of any length");
    mac.update(message.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// The full query for one signed request: the given query with `signature`
/// appended last, which is where the venue requires it.
pub(crate) fn signed_query(secret: &str, query: &str) -> String {
    let signature = hmac_sha256_hex(secret, query);
    format!("{query}&signature={signature}")
}

#[cfg(test)]
mod tests {
    use super::*;

    // The venue's own worked example from its signature documentation: this
    // exact totalParams under this exact secret must produce this exact hex.
    // An implementation with nothing in common with this one (Python's hmac)
    // agrees, so the signing is right and not merely self-consistent.
    const DOC_SECRET: &str = "2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9";
    const DOC_PARAMS: &str = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000\
                              &timeInForce=GTC&recvWindow=5000&timestamp=1591702613943";
    const DOC_SIGNATURE: &str = "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9";

    #[test]
    fn the_venues_own_worked_example_signs_to_its_published_hex() {
        assert_eq!(hmac_sha256_hex(DOC_SECRET, DOC_PARAMS), DOC_SIGNATURE);
    }

    #[test]
    fn the_signature_is_appended_as_the_final_parameter() {
        assert_eq!(
            signed_query(DOC_SECRET, DOC_PARAMS),
            format!("{DOC_PARAMS}&signature={DOC_SIGNATURE}")
        );
    }

    #[test]
    fn the_query_keeps_the_order_it_was_given_in() {
        // The venue signs the string it receives, whatever the order, so the
        // builder must not re-order what will be signed.
        let query = query_string(&[
            ("symbol", "BTCUSDT".into()),
            ("side", "BUY".into()),
            ("quantity", "1".into()),
        ]);
        assert_eq!(query, "symbol=BTCUSDT&side=BUY&quantity=1");
    }

    #[test]
    fn a_json_list_value_is_encoded_and_the_encoded_bytes_are_what_is_signed() {
        // The batch endpoints take a JSON list as one parameter value. The
        // quotes, brackets and commas must be encoded, and the signature must
        // cover the encoded form — the bytes on the wire.
        let query = query_string(&[("origClientOrderIdList", r#"["eng-1","eng-2"]"#.into())]);
        assert_eq!(
            query,
            "origClientOrderIdList=%5B%22eng-1%22%2C%22eng-2%22%5D"
        );
        let signed = signed_query(DOC_SECRET, &query);
        assert!(signed.starts_with(&query));
        assert_eq!(
            signed,
            format!("{query}&signature={}", hmac_sha256_hex(DOC_SECRET, &query))
        );
    }

    #[test]
    fn signature_is_lowercase_hex_of_the_right_length() {
        let signature = hmac_sha256_hex(DOC_SECRET, DOC_PARAMS);
        assert_eq!(signature.len(), 64);
        assert!(signature
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()));
    }

    #[test]
    fn a_changed_parameter_changes_the_signature() {
        let a = hmac_sha256_hex(DOC_SECRET, DOC_PARAMS);
        let b = hmac_sha256_hex(DOC_SECRET, &DOC_PARAMS.replace("side=BUY", "side=SELL"));
        assert_ne!(a, b);
    }

    #[test]
    fn an_empty_query_is_the_empty_string_not_a_stray_separator() {
        assert_eq!(query_string(&[]), "");
    }
}
