//! The engine's order id, carried inside Hyperliquid's 16-byte client id.
//!
//! Bybit takes the engine's `orderLinkId` as a string and hands it back
//! unchanged, so the engine recognises its own orders at a glance. Hyperliquid
//! takes a *cloid*: exactly 16 bytes, written as hex. The engine's ids are
//! longer than that — `eng-<boot ms>-<n>`, up to 36 characters — so something
//! has to give.
//!
//! Hashing the id would fit, and would be wrong in one specific way: after a
//! restart the engine reads back the orders the venue is still working, and
//! ids it cannot recognise are foreign orders — placed by a hand, by another
//! process. A hash cannot be undone, so every order that outlived a restart
//! would come back looking like somebody else's, and boot would refuse to open
//! on a book the engine itself had left.
//!
//! So the id is *packed*, not hashed. `eng-<boot ms>-<n>` is two numbers and a
//! shape, and both numbers fit with room to spare: 48 bits of milliseconds
//! reaches the year 10889, and 40 bits of counter is a trillion orders in one
//! boot. Anything that does not fit that shape falls back to a hash, which
//! still round-trips *within* a run because the engine keeps its own book —
//! only the cross-restart recognition is lost, and only for an id shape the
//! engine does not currently mint.
//!
//! The first byte says which of the two it is, so a later change of scheme can
//! be told apart from this one rather than silently misread.

use super::sign::keccak;

/// The packed form: engine id recoverable from the bytes.
const SCHEME_PACKED: u8 = 0x01;
/// The hashed fallback: unique, but not reversible.
const SCHEME_HASHED: u8 = 0x02;

/// What the engine's ids look like. Kept here as one string so the coupling to
/// `engine-core`'s minting is visible rather than implied.
const ENGINE_PREFIX: &str = "eng-";

const MAX_BOOT_MS: u64 = (1 << 48) - 1;
const MAX_COUNTER: u64 = (1 << 40) - 1;

/// The venue's client id for one engine order id: `0x` and 32 hex digits.
pub(crate) fn to_cloid(client_order_id: &str) -> String {
    format!("0x{}", hex::encode(to_bytes(client_order_id)))
}

fn to_bytes(client_order_id: &str) -> [u8; 16] {
    let mut out = [0u8; 16];
    if let Some((boot_ms, counter)) = split_engine_id(client_order_id) {
        out[0] = SCHEME_PACKED;
        out[1..7].copy_from_slice(&boot_ms.to_be_bytes()[2..]);
        out[7..12].copy_from_slice(&counter.to_be_bytes()[3..]);
        return out;
    }
    let hashed = keccak(client_order_id.as_bytes());
    out[0] = SCHEME_HASHED;
    out[1..].copy_from_slice(&hashed[..15]);
    out
}

/// The engine id a cloid was made from, or `None` when it was hashed or was
/// never one of ours.
pub(crate) fn from_cloid(cloid: &str) -> Option<String> {
    let body = cloid.trim().strip_prefix("0x").or_else(|| cloid.trim().strip_prefix("0X"))?;
    let bytes = hex::decode(body).ok()?;
    let bytes: [u8; 16] = bytes.try_into().ok()?;
    // The scheme byte alone is one byte in two hundred and fifty-six: this
    // reply is account-wide, the owner hand-trades it, and a stranger's cloid
    // that happened to start with it would deliver their fill to a strategy
    // here. The packed form always leaves these four bytes zero, so they are
    // part of the proof that the id is one this engine minted.
    if bytes[0] != SCHEME_PACKED || bytes[12..] != [0u8; 4] {
        return None;
    }
    let mut boot = [0u8; 8];
    boot[2..].copy_from_slice(&bytes[1..7]);
    let mut counter = [0u8; 8];
    counter[3..].copy_from_slice(&bytes[7..12]);
    Some(format!(
        "{ENGINE_PREFIX}{}-{}",
        u64::from_be_bytes(boot),
        u64::from_be_bytes(counter)
    ))
}

/// `eng-<boot ms>-<n>` split into its two numbers, or `None` for any other
/// shape.
fn split_engine_id(id: &str) -> Option<(u64, u64)> {
    let rest = id.strip_prefix(ENGINE_PREFIX)?;
    let (boot, counter) = rest.split_once('-')?;
    // Refuse anything a round trip would not reproduce: a leading zero or a
    // plus sign parses fine and prints back differently, and an id that came
    // back changed is an order the engine would disown.
    if boot.is_empty() || counter.is_empty() {
        return None;
    }
    if !boot.bytes().all(|b| b.is_ascii_digit()) || !counter.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    if (boot.starts_with('0') && boot.len() > 1) || (counter.starts_with('0') && counter.len() > 1) {
        return None;
    }
    let boot_ms: u64 = boot.parse().ok()?;
    let n: u64 = counter.parse().ok()?;
    (boot_ms <= MAX_BOOT_MS && n <= MAX_COUNTER).then_some((boot_ms, n))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_engine_id_survives_the_round_trip() {
        // The ids `engine-core` actually mints: `eng-{boot_ms}-{n}`.
        for id in [
            "eng-1700000000000-1",
            "eng-1700000000000-999999",
            "eng-1-1",
            "eng-281474976710655-1099511627775",
        ] {
            let cloid = to_cloid(id);
            assert_eq!(cloid.len(), 34, "{cloid} is not 16 bytes of hex");
            assert_eq!(
                from_cloid(&cloid).as_deref(),
                Some(id),
                "{id} did not come back from {cloid}"
            );
        }
    }

    #[test]
    fn different_orders_get_different_client_ids() {
        let a = to_cloid("eng-1700000000000-1");
        let b = to_cloid("eng-1700000000000-2");
        let c = to_cloid("eng-1700000000001-1");
        assert_ne!(a, b);
        assert_ne!(a, c);
        assert_ne!(b, c);
    }

    #[test]
    fn an_id_of_another_shape_still_gets_a_client_id_but_not_a_reversible_one() {
        // Hashed, so it is still unique and still valid; it simply cannot be
        // recognised as ours after a restart. Stated as a test so the loss is
        // visible rather than discovered.
        let cloid = to_cloid("hand-placed-1");
        assert_eq!(cloid.len(), 34);
        assert_eq!(from_cloid(&cloid), None);
        assert_ne!(cloid, to_cloid("hand-placed-2"));
    }

    #[test]
    fn a_client_id_from_somewhere_else_is_not_read_as_ours() {
        // Orders placed by a hand carry ids we never minted, and reading one
        // as an engine id would attribute a stranger's fill to a strategy.
        for foreign in [
            "0x00000000000000000000000000000000",
            "0xff000000000000000000000000000000",
            "not hex",
            "0x123",
            "",
        ] {
            assert_eq!(from_cloid(foreign), None, "{foreign} was read as ours");
        }
    }

    #[test]
    fn a_padded_or_signed_number_is_not_packed() {
        // These parse as numbers and print back differently, so packing them
        // would hand back an id the engine never minted.
        for odd in ["eng-01700000000000-1", "eng-1700000000000-01", "eng-+1-1", "eng--1-1"] {
            assert_eq!(from_cloid(&to_cloid(odd)), None, "{odd} was packed");
        }
    }

    #[test]
    fn the_first_byte_says_which_scheme_made_it() {
        assert!(to_cloid("eng-1-1").starts_with("0x01"));
        assert!(to_cloid("something else").starts_with("0x02"));
    }

    #[test]
    fn a_stranger_id_that_starts_with_our_scheme_byte_is_not_ours() {
        // `userFills` is account-wide and the owner hand-trades this account.
        // Reading the scheme byte alone would claim one foreign id in every
        // 256 and charge somebody else's fill to a strategy here.
        let mut foreign = [0u8; 16];
        foreign[0] = SCHEME_PACKED;
        foreign[12] = 0x01;
        assert_eq!(from_cloid(&format!("0x{}", hex::encode(foreign))), None);
        // Every trailing byte is checked, not just the first of them.
        for byte in 12..16 {
            let mut near = [0u8; 16];
            near[0] = SCHEME_PACKED;
            near[byte] = 0xff;
            assert_eq!(
                from_cloid(&format!("0x{}", hex::encode(near))),
                None,
                "a foreign id differing only at byte {byte} was read as ours"
            );
        }
        // And ours still round-trips.
        assert_eq!(
            from_cloid(&to_cloid("eng-1700000000000-9")).as_deref(),
            Some("eng-1700000000000-9")
        );
    }
}
