//! MEXC futures request signing.
//!
//! The signed string is `api_key + timestamp + payload`, where payload is the
//! raw JSON body on POST and the sorted query string on GET — the exact bytes
//! that go on the wire, not a re-serialization of them. The signature is a
//! lowercase hex HMAC-SHA256 under the API secret, and the venue does not
//! want it base64'd.
//!
//! **The GET payload must be sorted by key.** MEXC signs the query string it
//! is sent, and its own clients build that string from a key-sorted map, so a
//! request whose parameters go out in any other order authenticates against a
//! different string than the venue computes. Callers hand [`query_string`] the
//! pairs and it does the sorting, so no call site has to remember.

use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;

pub(crate) const HEADER_KEY: &str = "ApiKey";
pub(crate) const HEADER_TIMESTAMP: &str = "Request-Time";
pub(crate) const HEADER_SIGN: &str = "Signature";
pub(crate) const HEADER_RECV_WINDOW: &str = "Recv-Window";

/// Seconds. The venue's maximum is 60; it rejects a request whose
/// `Request-Time` is further from its own clock than this.
pub(crate) const RECV_WINDOW_S: &str = "30";

/// The string MEXC expects to be signed.
pub(crate) fn sign_payload(api_key: &str, timestamp_ms: i64, body_or_query: &str) -> String {
    format!("{api_key}{timestamp_ms}{body_or_query}")
}

pub(crate) fn hmac_sha256_hex(secret: &str, message: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
        .expect("HMAC-SHA256 takes a key of any length");
    mac.update(message.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// Sign one REST request.
pub(crate) fn rest_signature(
    secret: &str,
    api_key: &str,
    timestamp_ms: i64,
    body_or_query: &str,
) -> String {
    hmac_sha256_hex(secret, &sign_payload(api_key, timestamp_ms, body_or_query))
}

/// The query string for a signed GET: sorted by key, `k=v` joined by `&`.
///
/// Sorted here rather than at the call sites because the signature is over
/// this exact string — two call sites disagreeing about order would be one
/// that authenticates and one that does not, and the failure would look like
/// a bad key.
pub(crate) fn query_string(pairs: &[(&str, String)]) -> String {
    let mut sorted: Vec<&(&str, String)> = pairs.iter().collect();
    sorted.sort_by_key(|(key, _)| *key);
    sorted
        .iter()
        .map(|(key, value)| format!("{key}={value}"))
        .collect::<Vec<_>>()
        .join("&")
}

#[cfg(test)]
mod tests {
    use super::*;

    // The expected signatures below were produced by Python's `hmac` module,
    // an implementation with nothing in common with this one. If both agree
    // the signing is right, not merely self-consistent.
    const KEY: &str = "demoKey000000000001";
    const SECRET: &str = "demoSecret00000000000000000001";
    const TS: i64 = 1_700_000_000_000;

    const POST_BODY: &str = concat!(
        r#"{"externalOid":"eng-1","openType":2,"price":"77500.1","side":1,"#,
        r#""symbol":"BTC_USDT","type":5,"vol":"10"}"#
    );

    #[test]
    fn payload_is_key_then_timestamp_then_body() {
        assert_eq!(
            sign_payload(KEY, TS, "symbol=BTC_USDT"),
            "demoKey0000000000011700000000000symbol=BTC_USDT"
        );
    }

    #[test]
    fn post_body_vector() {
        assert_eq!(
            rest_signature(SECRET, KEY, TS, POST_BODY),
            "411b8a03b672d179bcc6acd8c631cd94c3e81368f8d1abadb2838ee374d06b26"
        );
    }

    #[test]
    fn get_query_vector() {
        assert_eq!(
            rest_signature(SECRET, KEY, TS, "page_num=1&page_size=20&symbol=BTC_USDT"),
            "69be9eb907f0ded1997501e413ceaf4f94a19bd2ec5b8430bf4f18a2ccc4e46d"
        );
    }

    #[test]
    fn empty_payload_vector() {
        assert_eq!(
            rest_signature(SECRET, KEY, TS, ""),
            "817472c98f099ffdce67b2f245464691d98242068df4f4667c402c2ec7423755"
        );
    }

    #[test]
    fn a_query_is_sorted_by_key_whatever_order_it_was_given_in() {
        // The signature is over this exact string. Two call sites that built
        // it in different orders would be one that authenticates and one that
        // does not, and the venue would call both a bad key.
        let forwards = query_string(&[
            ("symbol", "BTC_USDT".into()),
            ("page_num", "1".into()),
            ("page_size", "20".into()),
        ]);
        let backwards = query_string(&[
            ("page_size", "20".into()),
            ("symbol", "BTC_USDT".into()),
            ("page_num", "1".into()),
        ]);
        assert_eq!(forwards, "page_num=1&page_size=20&symbol=BTC_USDT");
        assert_eq!(forwards, backwards);
    }

    #[test]
    fn an_empty_query_is_the_empty_string_not_a_stray_separator() {
        assert_eq!(query_string(&[]), "");
    }

    #[test]
    fn signature_is_lowercase_hex_of_the_right_length() {
        let sig = rest_signature(SECRET, KEY, TS, POST_BODY);
        assert_eq!(sig.len(), 64);
        assert!(sig.chars().all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()));
    }

    #[test]
    fn a_changed_body_changes_the_signature() {
        let a = rest_signature(SECRET, KEY, TS, POST_BODY);
        let b = rest_signature(SECRET, KEY, TS, &POST_BODY.replace("\"side\":1", "\"side\":3"));
        assert_ne!(a, b);
    }
}
