use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;

pub(crate) fn hmac_sha256_hex(secret: &str, message: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
        .expect("HMAC-SHA256 takes a key of any length");
    mac.update(message.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}
