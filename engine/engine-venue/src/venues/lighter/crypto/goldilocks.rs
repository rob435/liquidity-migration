//! The Goldilocks field, `p = 2^64 - 2^32 + 1`.
//!
//! The base field of everything Lighter signs with. Values are kept canonical
//! — always below `p` — which the reference implementation does not bother
//! doing on every operation; the answers agree because the reference
//! canonicalises before it compares or emits, and `vectors.rs` beside this file
//! checks that against its output rather than assuming it.

/// `2^64 - 2^32 + 1`.
pub(crate) const P: u64 = 0xFFFF_FFFF_0000_0001;
/// `2^32 - 1`. What `2^64` is congruent to modulo `p`, which is the whole
/// trick that makes reduction two subtractions instead of a division.
const EPSILON: u64 = 0xFFFF_FFFF;

/// The field has `2^32` as its largest power-of-two subgroup, and this
/// generates it. Both are needed by the square root.
const TWO_ADICITY: u32 = 32;
const POWER_OF_TWO_GENERATOR: u64 = 7_277_203_076_849_721_926;

pub(crate) const ZERO: u64 = 0;
pub(crate) const ONE: u64 = 1;

/// Fold any 64-bit value into the field. Inputs already below `p` come back
/// unchanged.
pub(crate) const fn canonical(x: u64) -> u64 {
    if x >= P {
        x - P
    } else {
        x
    }
}

pub(crate) fn add(a: u64, b: u64) -> u64 {
    let (sum, carried) = a.overflowing_add(b);
    let (reduced, borrowed) = sum.overflowing_sub(P);
    // Carrying out of 64 bits means the true sum is at least 2^64, which is
    // above p; not borrowing means the sum was at least p. Either way the
    // reduced value is the answer.
    if carried || !borrowed {
        reduced
    } else {
        sum
    }
}

pub(crate) fn sub(a: u64, b: u64) -> u64 {
    let (diff, borrowed) = a.overflowing_sub(b);
    if borrowed {
        diff.wrapping_add(P)
    } else {
        diff
    }
}

pub(crate) fn neg(a: u64) -> u64 {
    if a == 0 {
        0
    } else {
        P - a
    }
}

pub(crate) fn double(a: u64) -> u64 {
    add(a, a)
}

/// Reduce a 128-bit product. `2^64 ≡ 2^32 - 1` and `2^96 ≡ -1`, which is what
/// turns the high half into two cheap corrections.
pub(crate) fn reduce128(x: u128) -> u64 {
    let lo = x as u64;
    let hi = (x >> 64) as u64;
    let hi_hi = hi >> 32;
    let hi_lo = hi & EPSILON;

    let (mut t0, borrowed) = lo.overflowing_sub(hi_hi);
    if borrowed {
        t0 = t0.wrapping_sub(EPSILON);
    }
    let t1 = hi_lo.wrapping_mul(EPSILON);
    let (sum, carried) = t0.overflowing_add(t1);
    let folded = if carried { sum.wrapping_add(EPSILON) } else { sum };
    canonical(folded)
}

pub(crate) fn mul(a: u64, b: u64) -> u64 {
    reduce128((a as u128) * (b as u128))
}

pub(crate) fn square(a: u64) -> u64 {
    mul(a, a)
}

/// `x^(2^n)`.
pub(crate) fn exp_power_of_2(mut x: u64, n: u32) -> u64 {
    for _ in 0..n {
        x = square(x);
    }
    x
}

pub(crate) fn exp(base: u64, exponent: u64) -> u64 {
    let mut result = ONE;
    let mut acc = base;
    let mut e = exponent;
    while e > 0 {
        if e & 1 == 1 {
            result = mul(result, acc);
        }
        acc = square(acc);
        e >>= 1;
    }
    result
}

/// `1/a`, or zero for zero. By Fermat, which is slower than the extended
/// Euclid and has no branches to get wrong.
pub(crate) fn inverse_or_zero(a: u64) -> u64 {
    if a == 0 {
        return 0;
    }
    exp(a, P - 2)
}

pub(crate) fn is_quadratic_residue(x: u64) -> bool {
    if x == 0 {
        return true;
    }
    exp(x, (P - 1) / 2) == ONE
}

/// A square root, or `None` when the value is not a square. Tonelli-Shanks,
/// copied from the reference so the branch that picks between the two roots
/// picks the same one.
pub(crate) fn sqrt(value: u64) -> Option<u64> {
    if value == 0 {
        return Some(0);
    }
    if !is_quadratic_residue(value) {
        return None;
    }
    let value = canonical(value);
    let t = (P - 1) / (1u64 << TWO_ADICITY);
    let mut z = POWER_OF_TWO_GENERATOR;
    let mut w = exp(value, (t - 1) / 2);
    let mut x = mul(value, w);
    let mut b = mul(x, w);
    let mut v = TWO_ADICITY;

    while b != ONE {
        let mut k = 0u32;
        let mut b2k = b;
        while b2k != ONE {
            b2k = square(b2k);
            k += 1;
        }
        let j = v - k - 1;
        w = z;
        for _ in 0..j {
            w = square(w);
        }
        z = square(w);
        b = mul(b, z);
        x = mul(x, w);
        v = k;
    }
    Some(x)
}

pub(crate) fn to_le_bytes(x: u64) -> [u8; 8] {
    canonical(x).to_le_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn addition_and_subtraction_wrap_at_the_modulus() {
        assert_eq!(add(P - 1, 1), 0);
        assert_eq!(add(P - 1, 2), 1);
        assert_eq!(sub(0, 1), P - 1);
        assert_eq!(sub(1, 1), 0);
        assert_eq!(neg(0), 0);
        assert_eq!(add(neg(5), 5), 0);
        assert_eq!(double(P - 1), P - 2);
    }

    #[test]
    fn multiplication_reduces_the_whole_product() {
        assert_eq!(mul(0, 12345), 0);
        assert_eq!(mul(1, 12345), 12345);
        // (p-1)^2 = 1 mod p, the classic check that the high half is folded
        // rather than truncated.
        assert_eq!(mul(P - 1, P - 1), 1);
        assert_eq!(mul(P - 1, 2), P - 2);
        for a in [2u64, 3, 1 << 32, P - 2, 12_345_678_901_234_567] {
            assert!(mul(a, a) < P, "a product left the field");
        }
    }

    #[test]
    fn inverse_undoes_multiplication() {
        for a in [1u64, 2, 3, 1 << 32, P - 2, 9_876_543_210_987_654_321 % P] {
            assert_eq!(mul(a, inverse_or_zero(a)), ONE, "{a}");
        }
        assert_eq!(inverse_or_zero(0), 0);
    }

    #[test]
    fn a_square_root_squares_back_and_a_non_residue_has_none() {
        let mut found = 0;
        for a in 1u64..200 {
            match sqrt(a) {
                Some(root) => {
                    assert_eq!(square(root), a, "sqrt({a}) = {root}");
                    found += 1;
                }
                None => assert!(!is_quadratic_residue(a), "{a} is a square with no root"),
            }
        }
        assert!(found > 50, "only {found} squares in the first 200 values");
        assert_eq!(sqrt(0), Some(0));
    }

    #[test]
    fn exponentiation_agrees_with_repeated_multiplication() {
        let base = 1_234_567_890_123u64;
        let mut by_hand = ONE;
        for _ in 0..10 {
            by_hand = mul(by_hand, base);
        }
        assert_eq!(exp(base, 10), by_hand);
        assert_eq!(exp_power_of_2(base, 3), exp(base, 8));
        assert_eq!(exp(base, 0), ONE);
    }
}
