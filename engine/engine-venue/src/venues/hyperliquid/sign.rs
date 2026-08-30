//! Hyperliquid L1 action signing.
//!
//! There is no API secret here. The account signs with its own wallet key —
//! in practice an *API wallet* (Hyperliquid calls it an agent), a separate
//! key the account owner has approved to trade on its behalf and which cannot
//! withdraw. The engine holds that key and never the account's own.
//!
//! Four steps, and every one of them is part of the signature:
//!
//! 1. The action is msgpack-encoded ([`super::msgpack`]), then the nonce, the
//!    vault flag and the optional expiry are appended as raw bytes, and the
//!    lot is keccak-256'd. That digest is the "connection id".
//! 2. The connection id goes into a *phantom agent* — a struct that exists
//!    only to be signed — whose `source` is `"a"` on mainnet and `"b"` on
//!    testnet. That one letter is the whole replay fence between the two
//!    networks, which is why the realm is threaded down here rather than read
//!    from a host string.
//! 3. The phantom agent is hashed as EIP-712 typed data under a fixed domain:
//!    name `Exchange`, version `1`, chain id 1337, zero verifying contract.
//!    1337 is not a real chain — it is a constant, the same on both networks.
//! 4. The digest is signed with secp256k1, and `r`, `s` and the recovery
//!    parameter `v` go in the request body beside the action.
//!
//! Every constant below is pinned by `tests` against the vectors published in
//! Hyperliquid's own Python SDK, which were produced by an implementation
//! with nothing in common with this one.

use engine_types::VenueError;
use k256::ecdsa::{RecoveryId, Signature, SigningKey};
use k256::elliptic_curve::sec1::ToSec1Point;
use sha3::{Digest, Keccak256};

use super::msgpack::{encode, Mp};

/// EIP-712 domain for every L1 action. Not a chain id in the usual sense:
/// 1337 is a constant both networks accept, and the network is told apart by
/// the phantom agent's `source` instead.
const DOMAIN_CHAIN_ID: u64 = 1337;
const DOMAIN_NAME: &str = "Exchange";
const DOMAIN_VERSION: &str = "1";

/// Mainnet and testnet actions differ by this single byte, and nothing else.
/// A testnet-signed action replayed against mainnet fails here.
const SOURCE_MAINNET: &str = "a";
const SOURCE_TESTNET: &str = "b";

pub(crate) fn keccak(bytes: &[u8]) -> [u8; 32] {
    let mut hasher = Keccak256::new();
    hasher.update(bytes);
    hasher.finalize().into()
}

/// The connection id: what the phantom agent carries and the signature covers.
///
/// The byte layout is the venue's, copied exactly — msgpack, then an 8-byte
/// big-endian nonce, then one flag byte for the vault (`0x00` none, `0x01`
/// followed by the 20 address bytes), then, only when an expiry is set, a
/// `0x00` byte and an 8-byte big-endian expiry. The last flag reads as a
/// mistake and is not one: the venue writes `0x00` there, not `0x01`.
pub(crate) fn action_hash(
    action: &Mp,
    vault_address: Option<[u8; 20]>,
    nonce: u64,
    expires_after: Option<u64>,
) -> [u8; 32] {
    let mut data = encode(action);
    data.extend_from_slice(&nonce.to_be_bytes());
    match vault_address {
        None => data.push(0x00),
        Some(address) => {
            data.push(0x01);
            data.extend_from_slice(&address);
        }
    }
    if let Some(expiry) = expires_after {
        data.push(0x00);
        data.extend_from_slice(&expiry.to_be_bytes());
    }
    keccak(&data)
}

/// The EIP-712 digest the wallet actually signs.
pub(crate) fn agent_digest(connection_id: [u8; 32], mainnet: bool) -> [u8; 32] {
    let source = if mainnet {
        SOURCE_MAINNET
    } else {
        SOURCE_TESTNET
    };

    let domain_type = keccak(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
    );
    let mut domain = Vec::with_capacity(160);
    domain.extend_from_slice(&domain_type);
    domain.extend_from_slice(&keccak(DOMAIN_NAME.as_bytes()));
    domain.extend_from_slice(&keccak(DOMAIN_VERSION.as_bytes()));
    domain.extend_from_slice(&word_u64(DOMAIN_CHAIN_ID));
    // The verifying contract is the zero address: 32 zero bytes once the
    // 20-byte address is left-padded into a word.
    domain.extend_from_slice(&[0u8; 32]);
    let domain_separator = keccak(&domain);

    let agent_type = keccak(b"Agent(string source,bytes32 connectionId)");
    let mut agent = Vec::with_capacity(96);
    agent.extend_from_slice(&agent_type);
    agent.extend_from_slice(&keccak(source.as_bytes()));
    agent.extend_from_slice(&connection_id);
    let struct_hash = keccak(&agent);

    let mut signed = Vec::with_capacity(66);
    signed.extend_from_slice(&[0x19, 0x01]);
    signed.extend_from_slice(&domain_separator);
    signed.extend_from_slice(&struct_hash);
    keccak(&signed)
}

/// One signature, in the shape the request body carries it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct WireSignature {
    pub(crate) r: String,
    pub(crate) s: String,
    pub(crate) v: u8,
}

/// Sign one L1 action.
pub(crate) fn sign_l1_action(
    key: &SigningKey,
    action: &Mp,
    vault_address: Option<[u8; 20]>,
    nonce: u64,
    expires_after: Option<u64>,
    mainnet: bool,
) -> Result<WireSignature, VenueError> {
    let digest = agent_digest(
        action_hash(action, vault_address, nonce, expires_after),
        mainnet,
    );
    sign_digest(key, digest)
}

pub(crate) fn sign_digest(key: &SigningKey, digest: [u8; 32]) -> Result<WireSignature, VenueError> {
    let (signature, recovery): (Signature, RecoveryId) = key.sign_prehash_recoverable(&digest);
    let bytes = signature.to_bytes();
    Ok(WireSignature {
        r: hex_minimal(&bytes[..32]),
        s: hex_minimal(&bytes[32..]),
        // Ethereum's historical offset. The venue reads 27 and 28, not 0 and 1.
        v: 27 + recovery.to_byte(),
    })
}

/// Read a wallet key: 32 bytes of hex, with or without the `0x`.
pub(crate) fn parse_key(raw: &str) -> Result<SigningKey, VenueError> {
    let trimmed = raw.trim();
    let body = trimmed
        .strip_prefix("0x")
        .or_else(|| trimmed.strip_prefix("0X"))
        .unwrap_or(trimmed);
    let bytes = hex::decode(body).map_err(|_| {
        VenueError::Credentials(
            "the Hyperliquid API-wallet key is not hex; it is a 32-byte private key, \
             written with or without a leading 0x"
                .to_string(),
        )
    })?;
    if bytes.len() != 32 {
        return Err(VenueError::Credentials(format!(
            "the Hyperliquid API-wallet key is {} bytes, and a private key is 32",
            bytes.len()
        )));
    }
    SigningKey::from_slice(&bytes).map_err(|e| {
        VenueError::Credentials(format!(
            "the API-wallet key is not a valid secp256k1 key: {e}"
        ))
    })
}

/// The 20-byte address a signing key signs as.
///
/// Keccak of the uncompressed public key with its `0x04` tag dropped, last
/// twenty bytes. The engine needs it to say which wallet is placing orders.
pub(crate) fn address_of(key: &SigningKey) -> [u8; 20] {
    address_of_verifying(key.verifying_key())
}

/// The same derivation from a public key alone, so a signature's recovered
/// signer can be named without the private key.
pub(crate) fn address_of_verifying(key: &k256::ecdsa::VerifyingKey) -> [u8; 20] {
    let point = key.as_affine().to_sec1_point(false);
    // Drop the 0x04 tag: the address is keccak of the 64 coordinate bytes.
    let hashed = keccak(&point.as_bytes()[1..]);
    let mut address = [0u8; 20];
    address.copy_from_slice(&hashed[12..]);
    address
}

/// A 20-byte address as the venue writes it: lowercase hex with `0x`.
pub(crate) fn address_text(address: [u8; 20]) -> String {
    format!("0x{}", hex::encode(address))
}

/// Parse an address written as hex, with or without `0x`.
///
/// The value is never quoted back. Both of this venue's credential variables
/// hold a hex blob, so pasting the API-wallet key into the address is the
/// likely mistake — and the message saying so would then carry the key into
/// stderr and the journal.
pub(crate) fn parse_address(raw: &str) -> Result<[u8; 20], VenueError> {
    let trimmed = raw.trim();
    let body = trimmed
        .strip_prefix("0x")
        .or_else(|| trimmed.strip_prefix("0X"))
        .unwrap_or(trimmed);
    let bytes = hex::decode(body).map_err(|_| {
        VenueError::Credentials(
            "the Hyperliquid account address is not hex; it is a 20-byte address, written \
             with or without a leading 0x"
                .to_string(),
        )
    })?;
    if bytes.len() != 20 {
        return Err(VenueError::Credentials(format!(
            "the Hyperliquid account address is {} bytes, and an address is 20 — a 32-byte \
             value here is the API-wallet key in the wrong variable",
            bytes.len()
        )));
    }
    let mut address = [0u8; 20];
    address.copy_from_slice(&bytes);
    Ok(address)
}

fn word_u64(value: u64) -> [u8; 32] {
    let mut word = [0u8; 32];
    word[24..].copy_from_slice(&value.to_be_bytes());
    word
}

/// Hex with the leading zeros shaved, which is how the venue's own client
/// writes `r` and `s`. Kept identical so a request from here is
/// byte-comparable with one from their SDK.
fn hex_minimal(bytes: &[u8]) -> String {
    let full = hex::encode(bytes);
    let trimmed = full.trim_start_matches('0');
    if trimmed.is_empty() {
        "0x0".to_string()
    } else {
        format!("0x{trimmed}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::venues::hyperliquid::wire::{order_action, OrderWire};
    use engine_types::TimeInForce;

    /// The key in Hyperliquid's own signing tests. A published test key, and
    /// the only reason the expected signatures below can be quoted at all.
    const TEST_KEY: &str = "0x0123456789012345678901234567890123456789012345678901234567890123";

    fn key() -> SigningKey {
        parse_key(TEST_KEY).expect("the published test key")
    }

    #[test]
    fn the_action_hash_matches_the_published_connection_id() {
        // hyperliquid-python-sdk, tests/signing_test.py,
        // test_phantom_agent_creation_matches_production. An ETH IOC buy of
        // 0.0147 at 1670.1, asset 4, nonce 1677777606040.
        let action = order_action(
            vec![OrderWire {
                asset: 4,
                is_buy: true,
                px: "1670.1".to_string(),
                sz: "0.0147".to_string(),
                reduce_only: false,
                kind: super::super::wire::OrderKindWire::Limit {
                    tif: TimeInForce::Ioc,
                },
                cloid: None,
            }],
            "na",
        );
        let hash = action_hash(&action, None, 1_677_777_606_040, None);
        assert_eq!(
            hex::encode(hash),
            "0fcbeda5ae3c4950a548021552a4fea2226858c4453571bf3f24ba017eac2908",
            "the msgpack encoding or the trailing bytes do not match the venue's"
        );
    }

    #[test]
    fn a_plain_action_signs_to_the_published_signature() {
        // test_l1_action_signing_matches: {"type": "dummy", "num": 100000000000}
        let action = Mp::Map(vec![
            ("type", Mp::str("dummy")),
            ("num", Mp::Int(100_000_000_000)),
        ]);
        let mainnet = sign_l1_action(&key(), &action, None, 0, None, true).unwrap();
        assert_eq!(
            mainnet.r,
            "0x53749d5b30552aeb2fca34b530185976545bb22d0b3ce6f62e31be961a59298"
        );
        assert_eq!(
            mainnet.s,
            "0x755c40ba9bf05223521753995abb2f73ab3229be8ec921f350cb447e384d8ed8"
        );
        assert_eq!(mainnet.v, 27);

        let testnet = sign_l1_action(&key(), &action, None, 0, None, false).unwrap();
        assert_eq!(
            testnet.r,
            "0x542af61ef1f429707e3c76c5293c80d01f74ef853e34b76efffcb57e574f9510"
        );
        assert_eq!(
            testnet.s,
            "0x17b8b32f086e8cdede991f1e2c529f5dd5297cbe8128500e00cbaf766204a613"
        );
        assert_eq!(testnet.v, 28);
    }

    #[test]
    fn an_order_action_signs_to_the_published_signature() {
        // test_l1_action_signing_order_matches: asset 1, buy 100 at 100, GTC.
        let action = order_action(
            vec![OrderWire {
                asset: 1,
                is_buy: true,
                px: "100".to_string(),
                sz: "100".to_string(),
                reduce_only: false,
                kind: super::super::wire::OrderKindWire::Limit {
                    tif: TimeInForce::Gtc,
                },
                cloid: None,
            }],
            "na",
        );
        let mainnet = sign_l1_action(&key(), &action, None, 0, None, true).unwrap();
        assert_eq!(
            mainnet.r,
            "0xd65369825a9df5d80099e513cce430311d7d26ddf477f5b3a33d2806b100d78e"
        );
        assert_eq!(
            mainnet.s,
            "0x2b54116ff64054968aa237c20ca9ff68000f977c93289157748a3162b6ea940e"
        );
        assert_eq!(mainnet.v, 28);

        let testnet = sign_l1_action(&key(), &action, None, 0, None, false).unwrap();
        assert_eq!(
            testnet.r,
            "0x82b2ba28e76b3d761093aaded1b1cdad4960b3af30212b343fb2e6cdfa4e3d54"
        );
        assert_eq!(
            testnet.s,
            "0x6b53878fc99d26047f4d7e8c90eb98955a109f44209163f52d8dc4278cbbd9f5"
        );
        assert_eq!(testnet.v, 27);
    }

    #[test]
    fn the_two_networks_never_produce_the_same_signature() {
        // The replay fence. If `source` ever stopped reaching the digest, this
        // is the test that would notice, and the symptom in production would
        // be a testnet order accepted by the funded account.
        let action = Mp::Map(vec![("type", Mp::str("dummy")), ("num", Mp::Int(1))]);
        let mainnet = sign_l1_action(&key(), &action, None, 7, None, true).unwrap();
        let testnet = sign_l1_action(&key(), &action, None, 7, None, false).unwrap();
        assert_ne!(mainnet.r, testnet.r);
        assert_ne!(
            agent_digest([0u8; 32], true),
            agent_digest([0u8; 32], false)
        );
    }

    #[test]
    fn the_nonce_the_vault_and_the_expiry_all_reach_the_hash() {
        let action = Mp::Map(vec![("type", Mp::str("dummy"))]);
        let plain = action_hash(&action, None, 1, None);
        assert_ne!(plain, action_hash(&action, None, 2, None));
        assert_ne!(plain, action_hash(&action, Some([7u8; 20]), 1, None));
        assert_ne!(plain, action_hash(&action, None, 1, Some(5)));
    }

    #[test]
    fn a_key_reads_with_or_without_the_prefix_and_nothing_else_does() {
        let bare = TEST_KEY.trim_start_matches("0x");
        assert_eq!(
            address_of(&parse_key(TEST_KEY).unwrap()),
            address_of(&parse_key(bare).unwrap())
        );
        for bad in ["", "0x", "zz", &"0".repeat(62), &"0".repeat(66)] {
            assert!(parse_key(bad).is_err(), "{bad:?} was accepted as a key");
        }
        // All-zero is not a valid secp256k1 scalar, and a key that silently
        // became one would sign as an address nobody funded.
        assert!(parse_key(&format!("0x{}", "0".repeat(64))).is_err());
    }

    #[test]
    fn the_address_is_the_last_twenty_bytes_of_the_hashed_public_key() {
        // The address the venue's own SDK tests carry for this published key.
        // Nothing else here pins the derivation: the address is not part of
        // any signature, so a compressed-versus-uncompressed mix-up would
        // leave every signature vector passing and every order addressed to an
        // account nobody funded.
        let address = address_text(address_of(&key()));
        assert_eq!(address, "0x14791697260e4c9a71f18484c9f997b308e59325");
        assert_eq!(address.len(), 42);
        assert!(address.starts_with("0x"));
        assert_eq!(parse_address(&address).unwrap(), address_of(&key()));
        assert!(parse_address("0x1234").is_err());
    }

    #[test]
    fn signature_words_are_written_the_way_the_venue_writes_them() {
        // Leading zeros shaved, `0x` kept — the published vectors are 63 hex
        // digits wide for exactly this reason, and a zero-padded word would
        // not compare equal to them.
        assert_eq!(hex_minimal(&[0x00, 0x01]), "0x1");
        assert_eq!(hex_minimal(&[0xab, 0xcd]), "0xabcd");
        assert_eq!(hex_minimal(&[0x00, 0x00]), "0x0");
    }
}
