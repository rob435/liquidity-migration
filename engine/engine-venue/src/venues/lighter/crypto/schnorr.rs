//! Schnorr signatures over ECgFp5, with Poseidon2 as the challenge hash.
//!
//! `s = k - e·sk`, where `e = H(r ‖ H(m))` and `r` is the encoding of `k·G`.
//! A signature is the pair `(s, e)`, forty bytes each, little-endian, `s`
//! first.
//!
//! **The nonce is derived, not sampled.** The reference draws `k` at random.
//! Here it comes from a hash of the secret key and the message, which is the
//! modern convention (RFC 6979's reason): a repeated or predictable `k` hands
//! an attacker the private key, and an engine that signs thousands of orders
//! is exactly where a weak random source would eventually do that. A derived
//! nonce cannot repeat unless the same key signs the same message twice, in
//! which case the two signatures are identical and say nothing new.
//!
//! Verification is what has to agree with the venue, and it does — the
//! reference's own `SchnorrSignHashedMessage2` takes `k` as an argument, which
//! is how `vectors.rs` beside this file compares a signature this code produces
//! against one the reference produces from the same nonce.

use engine_types::VenueError;
use sha2::{Digest, Sha256};

use super::curve::{self, GENERATOR};
use super::gfp5::{self, Element};
use super::poseidon2::hash_to_quintic_extension;
use super::scalar::{self, Scalar};

/// Domain separators, so the two halves of the nonce cannot be the same hash.
const NONCE_DOMAIN_FIRST: u8 = 0x01;
const NONCE_DOMAIN_SECOND: u8 = 0x02;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Signature {
    pub(crate) s: Scalar,
    pub(crate) e: Scalar,
}

impl Signature {
    pub(crate) fn is_canonical(&self) -> bool {
        scalar::is_canonical(&self.s) && scalar::is_canonical(&self.e)
    }

    /// `s` little-endian, then `e` little-endian. Eighty bytes.
    pub(crate) fn to_bytes(self) -> [u8; 80] {
        let mut out = [0u8; 80];
        out[..40].copy_from_slice(&scalar::to_le_bytes(&self.s));
        out[40..].copy_from_slice(&scalar::to_le_bytes(&self.e));
        out
    }

    pub(crate) fn from_bytes(bytes: &[u8]) -> Option<Signature> {
        if bytes.len() != 80 {
            return None;
        }
        Some(Signature {
            s: scalar::from_le_bytes_canonical(&bytes[..40])?,
            e: scalar::from_le_bytes_canonical(&bytes[40..])?,
        })
    }
}

/// The public key for a secret: one `GF(p^5)` element, forty bytes.
pub(crate) fn public_key(secret: &Scalar) -> Element {
    GENERATOR.mul(secret).encode()
}

/// The challenge, `e = H(r ‖ H(m))`.
fn challenge(r: &Element, hashed_message: &Element) -> Scalar {
    let mut preimage = [0u64; 10];
    preimage[..5].copy_from_slice(r);
    preimage[5..].copy_from_slice(hashed_message);
    scalar::from_gfp5(&hash_to_quintic_extension(&preimage))
}

/// Sign, with the nonce supplied. The reference's `SchnorrSignHashedMessage2`,
/// and the only form a test can compare against it.
pub(crate) fn sign_with_nonce(
    hashed_message: &Element,
    secret: &Scalar,
    nonce: &Scalar,
) -> Signature {
    let r = GENERATOR.mul(nonce).encode();
    let e = challenge(&r, hashed_message);
    Signature {
        s: scalar::sub(nonce, &scalar::mul(&e, secret)),
        e,
    }
}

/// Sign, deriving the nonce from the key and the message.
pub(crate) fn sign(hashed_message: &Element, secret: &Scalar) -> Signature {
    sign_with_nonce(hashed_message, secret, &derive_nonce(secret, hashed_message))
}

/// A nonce that depends on the secret and the message and on nothing else.
///
/// Two SHA-256 blocks under different prefixes give forty bytes, folded into
/// the field — the same shape the venue's own key derivation uses to turn a
/// seed into a scalar.
fn derive_nonce(secret: &Scalar, hashed_message: &Element) -> Scalar {
    let mut material = Vec::with_capacity(1 + 40 + 40);
    material.push(0u8);
    material.extend_from_slice(&scalar::to_le_bytes(secret));
    material.extend_from_slice(&gfp5::to_le_bytes(hashed_message));

    let mut first = Sha256::new();
    material[0] = NONCE_DOMAIN_FIRST;
    first.update(&material);
    let first = first.finalize();

    let mut second = Sha256::new();
    material[0] = NONCE_DOMAIN_SECOND;
    second.update(&material);
    let second = second.finalize();

    let mut wide = [0u8; 40];
    wide[..32].copy_from_slice(&first);
    wide[32..].copy_from_slice(&second[..8]);
    // Big-endian, matching how the venue reads its own hash output into a
    // scalar.
    scalar::from_be_bytes(&wide).expect("forty bytes")
}

/// Whether a signature is this key's over this message.
///
/// Not on the order path — the venue does this — but a signer that cannot
/// check its own work is a signer nobody can test, and this is what
/// `vectors.rs` beside this file runs the reference's signatures through.
pub(crate) fn verify(public: &Element, hashed_message: &Element, signature: &Signature) -> bool {
    if !signature.is_canonical() {
        return false;
    }
    let Some(point) = curve::decode(public) else {
        return false;
    };
    let recovered = curve::mul_add_g(&point, &signature.s, &signature.e).encode();
    challenge(&recovered, hashed_message) == signature.e
}

/// A secret key from the forty bytes the venue's API key is.
pub(crate) fn secret_from_le_bytes(bytes: &[u8]) -> Result<Scalar, VenueError> {
    scalar::from_le_bytes(bytes).ok_or_else(|| {
        VenueError::Credentials(format!(
            "a Lighter API private key is 40 bytes; this one is {}",
            bytes.len()
        ))
    })
}

/// A secret key from a seed, the way the venue's own key manager derives one.
pub(crate) fn secret_from_seed(seed: &[u8]) -> Result<Scalar, VenueError> {
    if seed.len() < 32 {
        return Err(VenueError::Credentials(format!(
            "a Lighter seed is at least 32 bytes; this one is {}",
            seed.len()
        )));
    }
    let mut first = Sha256::new();
    first.update([NONCE_DOMAIN_FIRST]);
    first.update(seed);
    let first = first.finalize();

    let mut second = Sha256::new();
    second.update([NONCE_DOMAIN_SECOND]);
    second.update(seed);
    let second = second.finalize();

    let mut wide = [0u8; 40];
    wide[..32].copy_from_slice(&first);
    wide[32..].copy_from_slice(&second[..8]);
    scalar::from_be_bytes(&wide).ok_or_else(|| {
        VenueError::Credentials("the seed did not produce a usable key".to_string())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn secret() -> Scalar {
        secret_from_seed(&[7u8; 32]).expect("a 32-byte seed")
    }

    fn message() -> Element {
        hash_to_quintic_extension(&[1, 2, 3, 4, 5])
    }

    #[test]
    fn a_signature_verifies_against_its_own_key_and_message() {
        let sk = secret();
        let pk = public_key(&sk);
        let signature = sign(&message(), &sk);
        assert!(signature.is_canonical());
        assert!(verify(&pk, &message(), &signature));
    }

    #[test]
    fn a_signature_does_not_verify_against_another_message() {
        let sk = secret();
        let pk = public_key(&sk);
        let signature = sign(&message(), &sk);
        let other = hash_to_quintic_extension(&[1, 2, 3, 4, 6]);
        assert!(!verify(&pk, &other, &signature));
    }

    #[test]
    fn a_signature_does_not_verify_against_another_key() {
        let signature = sign(&message(), &secret());
        let other = public_key(&secret_from_seed(&[8u8; 32]).unwrap());
        assert!(!verify(&other, &message(), &signature));
    }

    #[test]
    fn a_tampered_signature_is_refused() {
        let sk = secret();
        let pk = public_key(&sk);
        let mut signature = sign(&message(), &sk);
        signature.s = scalar::add(&signature.s, &scalar::ONE);
        assert!(!verify(&pk, &message(), &signature));
    }

    #[test]
    fn the_nonce_is_derived_so_one_key_and_message_sign_the_same_way_twice() {
        // Not a convenience: a nonce that varied per call would be one drawn
        // from somewhere, and a repeat drawn from a weak source hands over the
        // private key.
        let sk = secret();
        assert_eq!(sign(&message(), &sk), sign(&message(), &sk));
        // And two different messages get two different nonces.
        let other = hash_to_quintic_extension(&[9, 9, 9]);
        assert_ne!(sign(&message(), &sk).s, sign(&other, &sk).s);
        // As do two different keys over one message.
        let other_key = secret_from_seed(&[8u8; 32]).unwrap();
        assert_ne!(sign(&message(), &sk).s, sign(&message(), &other_key).s);
    }

    #[test]
    fn a_second_encoding_of_the_same_signature_is_not_the_signature() {
        // Reading forty bytes by folding them into the field accepts `s + n`
        // as `s`, so a mutated encoding verifies as the original and the
        // canonicality check in `verify` can never fire.
        let sk = secret();
        let signature = sign(&message(), &sk);
        let mut bytes = signature.to_bytes();
        // n itself encodes the value zero, and is never a canonical encoding.
        let modulus: [u64; 5] = [
            0xE80F_D996_948B_FFE1,
            0xE888_5C39_D724_A09C,
            0x7FFF_FFE6_CFB8_0639,
            0x7FFF_FFF1_0000_0016,
            0x7FFF_FFFD_8000_0007,
        ];
        for (i, limb) in modulus.iter().enumerate() {
            bytes[i * 8..(i + 1) * 8].copy_from_slice(&limb.to_le_bytes());
        }
        assert_eq!(Signature::from_bytes(&bytes), None, "a non-canonical s was accepted");
    }

    #[test]
    fn the_signature_bytes_round_trip() {
        let signature = sign(&message(), &secret());
        let bytes = signature.to_bytes();
        assert_eq!(bytes.len(), 80);
        assert_eq!(Signature::from_bytes(&bytes), Some(signature));
        assert_eq!(Signature::from_bytes(&bytes[..79]), None);
    }

    #[test]
    fn a_key_that_is_not_forty_bytes_is_refused() {
        assert!(secret_from_le_bytes(&[0u8; 40]).is_ok());
        assert!(secret_from_le_bytes(&[0u8; 32]).is_err());
        assert!(secret_from_seed(&[0u8; 31]).is_err());
        assert!(secret_from_seed(&[0u8; 32]).is_ok());
    }

    #[test]
    fn signing_with_a_supplied_nonce_is_what_the_derived_one_feeds() {
        let sk = secret();
        let nonce = derive_nonce(&sk, &message());
        assert_eq!(sign(&message(), &sk), sign_with_nonce(&message(), &sk, &nonce));
    }
}
