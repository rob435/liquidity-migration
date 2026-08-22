//! The engine's order id, carried inside Lighter's 48-bit client order index.
//!
//! Same problem as Hyperliquid's sixteen-byte client id, with less room. The
//! venue's `ClientOrderIndex` is an integer between 1 and `2^48 - 1`, and the
//! engine's ids are `eng-<boot ms>-<n>` — the boot stamp alone is about 41
//! bits, which leaves seven for the counter and is not enough.
//!
//! So the stamp is packed at second resolution rather than millisecond, and
//! counted from 2020 rather than 1970: 29 bits of seconds and 18 of counter.
//! That reaches 2037 for the stamp and 262,143 orders in one boot for the
//! counter, and this fleet places a few a day.
//!
//! Past either bound the id is hashed instead. A hashed index is still unique
//! and still valid; what is lost is recognising the order as ours after a
//! restart, and the top bit says which of the two happened so the two can
//! never be confused.

use super::crypto::poseidon2::hash_to_quintic_extension;

/// The venue's ceiling: `2^48 - 1`.
const MAX_INDEX: i64 = (1 << 48) - 1;
/// Set on a packed index, clear on a hashed one.
const PACKED_FLAG: i64 = 1 << 47;

/// Seconds are counted from here rather than from the Unix epoch: the epoch
/// itself costs 31 bits and leaves too few for the counter, and no engine will
/// ever have booted before this.
const EPOCH_S: u64 = 1_600_000_000;

const SECONDS_BITS: u32 = 29;
const COUNTER_BITS: u32 = 18;
const MAX_SECONDS: u64 = (1 << SECONDS_BITS) - 1;
const MAX_COUNTER: u64 = (1 << COUNTER_BITS) - 1;

const ENGINE_PREFIX: &str = "eng-";

/// The venue's client order index for one engine order id.
pub(crate) fn to_index(client_order_id: &str) -> i64 {
    if let Some((boot_ms, counter)) = split_engine_id(client_order_id) {
        let seconds = boot_ms / 1000;
        if seconds >= EPOCH_S && seconds - EPOCH_S <= MAX_SECONDS && counter <= MAX_COUNTER {
            let since = seconds - EPOCH_S;
            return PACKED_FLAG | ((since as i64) << COUNTER_BITS) | (counter as i64);
        }
    }
    // Hashed: the low 47 bits of the transaction hash's first limb, never
    // zero, and with the packed flag clear.
    let digest = hash_to_quintic_extension(&bytes_as_fields(client_order_id.as_bytes()));
    let low = (digest[0] & ((1u64 << 47) - 1)) as i64;
    if low == 0 {
        1
    } else {
        low
    }
}

/// The engine id an index was made from, or `None` when it was hashed or was
/// never one of ours.
///
/// Byte-identical, which is what every consumer needs: reconcile and
/// attribution both match on the string, so an id that came back rounded would
/// charge each fill to nobody and read each resting order as a stranger's.
/// That holds because the engine mints its boot stamp on a whole second — see
/// `OrderRegistry::new` in engine-core — which is what makes a 48-bit field
/// enough.
pub(crate) fn from_index(index: i64) -> Option<String> {
    if index <= 0 || index > MAX_INDEX || index & PACKED_FLAG == 0 {
        return None;
    }
    let body = index & (PACKED_FLAG - 1);
    let since = (body >> COUNTER_BITS) as u64;
    let counter = (body & (MAX_COUNTER as i64)) as u64;
    Some(format!("{ENGINE_PREFIX}{}-{counter}", (since + EPOCH_S) * 1000))
}

/// `eng-<boot ms>-<n>` split into its two numbers.
fn split_engine_id(id: &str) -> Option<(u64, u64)> {
    let rest = id.strip_prefix(ENGINE_PREFIX)?;
    let (boot, counter) = rest.split_once('-')?;
    if boot.is_empty() || counter.is_empty() {
        return None;
    }
    if !boot.bytes().all(|b| b.is_ascii_digit()) || !counter.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some((boot.parse().ok()?, counter.parse().ok()?))
}

/// Bytes as field elements: eight little-endian bytes each, zero-padded. The
/// venue's own way of hashing a string.
pub(crate) fn bytes_as_fields(bytes: &[u8]) -> Vec<u64> {
    bytes
        .chunks(8)
        .map(|chunk| {
            let mut limb = [0u8; 8];
            limb[..chunk.len()].copy_from_slice(chunk);
            u64::from_le_bytes(limb)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_engine_id_comes_back_byte_for_byte() {
        // Reconcile and attribution both match on the string, so anything less
        // than byte-identical means every fill is charged to nobody and every
        // resting order of ours reads as a stranger's — which is a latch at
        // the next boot.
        for id in [
            "eng-1700000000000-1",
            "eng-1700000000000-7",
            "eng-1700000000000-262143",
            "eng-1600000000000-1",
            "eng-2000000000000-42",
        ] {
            assert_eq!(from_index(to_index(id)).as_deref(), Some(id), "{id} did not survive");
        }
    }

    #[test]
    fn the_stamp_the_engine_actually_mints_is_one_this_field_can_hold() {
        // The engine zeroes the millisecond part of its boot stamp precisely
        // so this holds. If that ever changes, every Lighter id stops round
        // tripping, and it stops silently.
        let boot_ms: i64 = 1_762_000_000_123;
        let stamp = boot_ms - boot_ms.rem_euclid(1_000);
        let id = format!("eng-{stamp}-9");
        assert_eq!(from_index(to_index(&id)).as_deref(), Some(id.as_str()));
    }

    #[test]
    fn every_index_is_inside_the_venues_range() {
        for id in [
            "eng-1700000000000-1",
            "eng-1700000000000-262143",
            "eng-1-0",
            "hand-placed",
            "eng-99999999999999999-1",
        ] {
            let index = to_index(id);
            assert!(index > 0, "{id} made {index}");
            assert!(index <= MAX_INDEX, "{id} made {index}, above the venue's ceiling");
        }
    }

    #[test]
    fn different_orders_get_different_indices() {
        let a = to_index("eng-1700000000000-1");
        let b = to_index("eng-1700000000000-2");
        let c = to_index("eng-1700000001000-1");
        assert_ne!(a, b);
        assert_ne!(a, c);
        assert_ne!(b, c);
    }

    #[test]
    fn an_id_past_the_counter_bound_is_hashed_rather_than_wrapped() {
        // Wrapping would hand two different orders one index, and the venue
        // would take the second as a duplicate of the first.
        let inside = to_index("eng-1700000000000-262143");
        let outside = to_index("eng-1700000000000-262144");
        assert!(from_index(inside).is_some());
        assert_eq!(from_index(outside), None, "an out-of-range id was packed");
        assert_ne!(inside, outside);
    }

    #[test]
    fn an_index_from_somewhere_else_is_not_read_as_ours() {
        for foreign in [0i64, -1, 1, 12345, MAX_INDEX] {
            let read = from_index(foreign);
            assert!(
                read.is_none() || foreign & PACKED_FLAG != 0,
                "{foreign} was read as ours"
            );
        }
        // A hand-placed order's index has the flag clear.
        assert_eq!(from_index(to_index("by hand")), None);
    }

    #[test]
    fn bytes_become_eight_byte_field_elements() {
        assert_eq!(bytes_as_fields(b""), Vec::<u64>::new());
        assert_eq!(bytes_as_fields(b"\x01"), vec![1]);
        assert_eq!(bytes_as_fields(&[0u8; 8]), vec![0]);
        assert_eq!(bytes_as_fields(&[1u8, 0, 0, 0, 0, 0, 0, 0, 2]), vec![1, 2]);
    }
}
