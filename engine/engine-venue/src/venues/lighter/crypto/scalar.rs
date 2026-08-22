//! The ECgFp5 group's scalar field: a prime of about `2^319`, held as five
//! 64-bit limbs, least significant first.
//!
//! Multiplication is Montgomery's, copied from the reference so the same
//! inputs give the same limbs. Addition and subtraction are plain 320-bit
//! arithmetic with one conditional fold, which is all that is needed when both
//! operands are already below the order.
//!
//! The selection between two candidate results is written without a branch,
//! the way the reference writes it. Not because a timing attack is in this
//! engine's threat model — the key sits on a host nobody else runs on — but
//! because the branchless form is no harder to read and removes the question.

/// The group order, `n`, least significant limb first.
const N: [u64; 5] = [
    0xE80F_D996_948B_FFE1,
    0xE888_5C39_D724_A09C,
    0x7FFF_FFE6_CFB8_0639,
    0x7FFF_FFF1_0000_0016,
    0x7FFF_FFFD_8000_0007,
];

/// `-1/n[0] mod 2^64`, the Montgomery factor.
const N0I: u64 = 0xD78B_EF72_057B_7BDF;

/// `2^640 mod n`, for converting into Montgomery form.
const R2: [u64; 5] = [
    0xA010_01DC_E33D_C739,
    0x6C32_28D3_3F62_ACCF,
    0xD1D7_96CC_91CF_8525,
    0xAADF_FF5D_1574_C1D8,
    0x4ACA_13B2_8CA2_51F5,
];

pub(crate) type Scalar = [u64; 5];

pub(crate) const ZERO: Scalar = [0; 5];
pub(crate) const ONE: Scalar = [1, 0, 0, 0, 0];

/// Below the group order — the only shape a scalar may be handed round in.
pub(crate) fn is_canonical(s: &Scalar) -> bool {
    for i in (0..5).rev() {
        if s[i] != N[i] {
            return s[i] < N[i];
        }
    }
    false
}

/// `a0` when `c` is zero, `a1` when `c` is all ones.
fn select(c: u64, a0: Scalar, a1: Scalar) -> Scalar {
    let mut out = ZERO;
    for i in 0..5 {
        out[i] = a0[i] ^ (c & (a0[i] ^ a1[i]));
    }
    out
}

/// 320-bit addition, no reduction.
fn add_inner(a: &Scalar, b: &Scalar) -> Scalar {
    let mut out = ZERO;
    let mut carry = 0u64;
    for i in 0..5 {
        let sum = (a[i] as u128) + (b[i] as u128) + (carry as u128);
        out[i] = sum as u64;
        carry = (sum >> 64) as u64;
    }
    out
}

/// 320-bit subtraction. The second return is all ones when it borrowed.
fn sub_inner(a: &Scalar, b: &Scalar) -> (Scalar, u64) {
    let mut out = ZERO;
    let mut borrow = 0u64;
    for i in 0..5 {
        let diff = (a[i] as i128) - (b[i] as i128) - (borrow as i128);
        out[i] = diff as u64;
        borrow = if diff < 0 { 1 } else { 0 };
    }
    (out, if borrow != 0 { u64::MAX } else { 0 })
}

pub(crate) fn add(a: &Scalar, b: &Scalar) -> Scalar {
    let sum = add_inner(a, b);
    let (folded, borrowed) = sub_inner(&sum, &N);
    select(borrowed, folded, sum)
}

pub(crate) fn sub(a: &Scalar, b: &Scalar) -> Scalar {
    let (diff, borrowed) = sub_inner(a, b);
    let folded = add_inner(&diff, &N);
    select(borrowed, diff, folded)
}

/// `(a * b) / 2^320 mod n`. `a` must already be below the order.
fn monty_mul(a: &Scalar, b: &Scalar) -> Scalar {
    let mut r = ZERO;
    for &m in b.iter() {
        let f = a[0].wrapping_mul(m).wrapping_add(r[0]).wrapping_mul(N0I);
        let mut cc1 = 0u64;
        let mut cc2 = 0u64;
        for j in 0..5 {
            let z = (a[j] as u128) * (m as u128) + (r[j] as u128) + (cc1 as u128);
            cc1 = (z >> 64) as u64;
            let z2 = (f as u128) * (N[j] as u128) + ((z as u64) as u128) + (cc2 as u128);
            cc2 = (z2 >> 64) as u64;
            if j > 0 {
                r[j - 1] = z2 as u64;
            }
        }
        r[4] = cc1.wrapping_add(cc2);
    }
    let (folded, borrowed) = sub_inner(&r, &N);
    select(borrowed, folded, r)
}

pub(crate) fn mul(a: &Scalar, b: &Scalar) -> Scalar {
    monty_mul(&monty_mul(a, &R2), b)
}

/// Fold any 320-bit value into the field.
///
/// The order is a little above `2^319`, so a 320-bit value is at most two
/// orders; two conditional subtractions are enough and a loop would only hide
/// that.
pub(crate) fn from_non_canonical(value: Scalar) -> Scalar {
    let mut out = value;
    for _ in 0..2 {
        let (folded, borrowed) = sub_inner(&out, &N);
        out = select(borrowed, folded, out);
    }
    out
}

/// Read forty little-endian bytes as an encoding, refusing one that is not
/// the canonical form of its value.
///
/// For a signature, unlike for a key, the bytes ARE the object: folding
/// `s + n` back to `s` would accept a second encoding of the same signature as
/// though it were the one that was made, and would make
/// [`super::schnorr::verify`]'s canonicality check unreachable.
pub(crate) fn from_le_bytes_canonical(bytes: &[u8]) -> Option<Scalar> {
    let read = from_le_bytes(bytes)?;
    (to_le_bytes(&read).as_slice() == bytes).then_some(read)
}

/// Read forty little-endian bytes, folding into the field if they run over.
pub(crate) fn from_le_bytes(bytes: &[u8]) -> Option<Scalar> {
    if bytes.len() != 40 {
        return None;
    }
    let mut out = ZERO;
    for i in 0..5 {
        let mut limb = [0u8; 8];
        limb.copy_from_slice(&bytes[i * 8..(i + 1) * 8]);
        out[i] = u64::from_le_bytes(limb);
    }
    Some(from_non_canonical(out))
}

/// Read forty BIG-endian bytes. The venue's own key derivation reads its
/// hash output this way round, so the seed path has to as well.
pub(crate) fn from_be_bytes(bytes: &[u8]) -> Option<Scalar> {
    if bytes.len() != 40 {
        return None;
    }
    let mut reversed = [0u8; 40];
    for (i, byte) in bytes.iter().rev().enumerate() {
        reversed[i] = *byte;
    }
    from_le_bytes(&reversed)
}

pub(crate) fn to_le_bytes(s: &Scalar) -> [u8; 40] {
    let mut out = [0u8; 40];
    for i in 0..5 {
        out[i * 8..(i + 1) * 8].copy_from_slice(&s[i].to_le_bytes());
    }
    out
}

/// A `GF(p^5)` element read as a 320-bit number and folded into the field.
/// This is how a hash becomes the challenge in a signature.
pub(crate) fn from_gfp5(x: &super::gfp5::Element) -> Scalar {
    let mut limbs = ZERO;
    for i in 0..5 {
        limbs[i] = super::goldilocks::canonical(x[i]);
    }
    from_non_canonical(limbs)
}

/// The bits, least significant first — what a double-and-add walks.
pub(crate) fn bits(s: &Scalar) -> [bool; 320] {
    let mut out = [false; 320];
    for (i, slot) in out.iter_mut().enumerate() {
        *slot = (s[i / 64] >> (i % 64)) & 1 == 1;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const A: Scalar = [12_345_678_901_234_567_890, 1, 2, 3, 4];

    #[test]
    fn the_order_itself_is_not_canonical_and_one_below_it_is() {
        assert!(!is_canonical(&N));
        let (below, _) = sub_inner(&N, &ONE);
        assert!(is_canonical(&below));
        assert!(is_canonical(&ZERO));
        assert!(is_canonical(&A));
    }

    #[test]
    fn addition_and_subtraction_are_inverses_and_stay_canonical() {
        let sum = add(&A, &A);
        assert!(is_canonical(&sum));
        assert_eq!(sub(&sum, &A), A);
        assert_eq!(add(&A, &ZERO), A);
        assert_eq!(sub(&A, &A), ZERO);
        // Wrapping past the order folds rather than overflowing.
        let (below, _) = sub_inner(&N, &ONE);
        assert_eq!(add(&below, &ONE), ZERO);
        assert_eq!(sub(&ZERO, &ONE), below);
    }

    #[test]
    fn multiplication_has_one_as_its_identity_and_is_commutative() {
        assert_eq!(mul(&A, &ONE), A);
        assert_eq!(mul(&ONE, &A), A);
        assert_eq!(mul(&A, &ZERO), ZERO);
        let b: Scalar = [7, 8, 9, 10, 11];
        assert_eq!(mul(&A, &b), mul(&b, &A));
        assert!(is_canonical(&mul(&A, &b)));
    }

    #[test]
    fn multiplication_distributes_over_addition() {
        // The property that would catch a Montgomery constant being wrong.
        let b: Scalar = [7, 8, 9, 10, 11];
        let c: Scalar = [99, 0, 0, 0, 1];
        assert_eq!(mul(&A, &add(&b, &c)), add(&mul(&A, &b), &mul(&A, &c)));
    }

    #[test]
    fn doubling_by_addition_matches_multiplying_by_two() {
        let two: Scalar = [2, 0, 0, 0, 0];
        assert_eq!(add(&A, &A), mul(&A, &two));
    }

    #[test]
    fn bytes_round_trip_both_ways_round() {
        assert_eq!(from_le_bytes(&to_le_bytes(&A)), Some(A));
        let mut be = to_le_bytes(&A);
        be.reverse();
        assert_eq!(from_be_bytes(&be), Some(A));
        assert_eq!(from_le_bytes(&[0u8; 39]), None);
    }

    #[test]
    fn a_value_above_the_order_folds_into_the_field() {
        // Forty bytes of ones is above the order; reading it must land inside
        // the field rather than keeping a number no operation is defined on.
        let all_ones = from_le_bytes(&[0xFFu8; 40]).unwrap();
        assert!(is_canonical(&all_ones));
        assert_eq!(from_non_canonical(N), ZERO);
    }

    #[test]
    fn the_bit_walk_reads_the_limbs_in_order() {
        assert!(bits(&ONE)[0]);
        assert!(!bits(&ONE)[1]);
        let high: Scalar = [0, 0, 0, 0, 1];
        assert!(bits(&high)[256]);
        assert_eq!(bits(&ZERO).iter().filter(|b| **b).count(), 0);
    }
}
