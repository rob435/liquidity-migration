//! Bybit v5 request signing.
//!
//! The signed string is `timestamp + api_key + recv_window + payload`, where
//! payload is the raw JSON body on POST and the raw query string on GET —
//! the exact bytes that go on the wire, not a re-serialization of them. The
//! signature is a lowercase hex HMAC-SHA256 under the API secret.

use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;

/// Sent as `X-BAPI-RECV-WINDOW`; the venue also defaults to this.
pub(crate) const RECV_WINDOW_MS: &str = "5000";

pub(crate) const HEADER_KEY: &str = "X-BAPI-API-KEY";
pub(crate) const HEADER_TIMESTAMP: &str = "X-BAPI-TIMESTAMP";
pub(crate) const HEADER_SIGN: &str = "X-BAPI-SIGN";
pub(crate) const HEADER_RECV_WINDOW: &str = "X-BAPI-RECV-WINDOW";

/// The string Bybit expects to be signed.
pub(crate) fn sign_payload(
    timestamp_ms: i64,
    api_key: &str,
    recv_window: &str,
    body_or_query: &str,
) -> String {
    format!("{timestamp_ms}{api_key}{recv_window}{body_or_query}")
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
    timestamp_ms: i64,
    api_key: &str,
    body_or_query: &str,
) -> String {
    hmac_sha256_hex(
        secret,
        &sign_payload(timestamp_ms, api_key, RECV_WINDOW_MS, body_or_query),
    )
}

/// Sign the private WebSocket handshake. The venue signs a fixed literal
/// joined to the expiry, not a request.
pub(crate) fn ws_signature(secret: &str, expires_ms: i64) -> String {
    hmac_sha256_hex(secret, &format!("GET/realtime{expires_ms}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    // Expected signatures below were produced by Python's `hmac` module, an
    // implementation with nothing in common with this one. If both agree the
    // signing is right, not merely self-consistent.
    const KEY: &str = "demoKey000000000001";
    const SECRET: &str = "demoSecret00000000000000000001";
    const TS: i64 = 1_700_000_000_000;

    const POST_BODY: &str = concat!(
        r#"{"category":"linear","orderLinkId":"eng-1","orderType":"Market","#,
        r#""qty":"0.001","reduceOnly":false,"side":"Buy","symbol":"BTCUSDT"}"#
    );

    #[test]
    fn payload_is_timestamp_key_window_then_body() {
        assert_eq!(
            sign_payload(TS, KEY, "5000", "accountType=UNIFIED"),
            "1700000000000demoKey0000000000015000accountType=UNIFIED"
        );
    }

    #[test]
    fn post_body_vector() {
        assert_eq!(
            rest_signature(SECRET, TS, KEY, POST_BODY),
            "3d8cc1150ffb3663a2d2e201d84713d9d6c90a565d56bb2fe23a4ce9d79ec009"
        );
    }

    #[test]
    fn get_query_vector() {
        assert_eq!(
            rest_signature(SECRET, TS, KEY, "accountType=UNIFIED"),
            "b23058fe5c2579c1920fcd326e69b815c92b15831924d79423169338e1200d35"
        );
    }

    #[test]
    fn empty_payload_vector() {
        assert_eq!(
            rest_signature(SECRET, TS, KEY, ""),
            "a3495a6be9dfbbade45210c7455d95604d4c642be2388d250caff5bfc06af658"
        );
    }

    #[test]
    fn websocket_handshake_vector() {
        assert_eq!(
            ws_signature(SECRET, 1_700_000_005_000),
            "7729aed00c9edca2392cf9cbed63f6243afe7b6bbd0e8f02472545d0f5cd331e"
        );
    }

    #[test]
    fn signature_is_lowercase_hex_of_the_right_length() {
        let sig = rest_signature(SECRET, TS, KEY, POST_BODY);
        assert_eq!(sig.len(), 64);
        assert!(sig
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()));
    }

    #[test]
    fn a_changed_body_changes_the_signature() {
        let a = rest_signature(SECRET, TS, KEY, POST_BODY);
        let b = rest_signature(SECRET, TS, KEY, &POST_BODY.replace("Buy", "Sell"));
        assert_ne!(a, b);
    }
}
