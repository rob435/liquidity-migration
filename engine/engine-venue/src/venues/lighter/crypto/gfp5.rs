//! `GF(p^5)`, the quintic extension of Goldilocks: `Fp[X]/(X^5 - 3)`.
//!
//! Where the curve lives, and where a hashed transaction lands. An element is
//! five base-field coefficients, lowest degree first.

use super::goldilocks as f;

pub(crate) type Element = [u64; 5];

pub(crate) const ZERO: Element = [0, 0, 0, 0, 0];
pub(crate) const ONE: Element = [1, 0, 0, 0, 0];
pub(crate) const TWO: Element = [2, 0, 0, 0, 0];

/// `X^5 = W`. What makes the multiplication fold rather than grow.
const W: u64 = 3;
/// A fifth root of unity in the base field: raising a coefficient's index by
/// one Frobenius step multiplies it by a power of this.
const DTH_ROOT: u64 = 1_041_288_259_238_279_555;

pub(crate) fn from_base(x: u64) -> Element {
    [f::canonical(x), 0, 0, 0, 0]
}

pub(crate) fn is_zero(a: &Element) -> bool {
    a.iter().all(|limb| f::canonical(*limb) == 0)
}

pub(crate) fn equals(a: &Element, b: &Element) -> bool {
    (0..5).all(|i| f::canonical(a[i]) == f::canonical(b[i]))
}

pub(crate) fn add(a: &Element, b: &Element) -> Element {
    let mut out = ZERO;
    for i in 0..5 {
        out[i] = f::add(a[i], b[i]);
    }
    out
}

pub(crate) fn sub(a: &Element, b: &Element) -> Element {
    let mut out = ZERO;
    for i in 0..5 {
        out[i] = f::sub(a[i], b[i]);
    }
    out
}

pub(crate) fn neg(a: &Element) -> Element {
    sub(&ZERO, a)
}

pub(crate) fn double(a: &Element) -> Element {
    add(a, a)
}

pub(crate) fn triple(a: &Element) -> Element {
    add(&double(a), a)
}

pub(crate) fn scalar_mul(a: &Element, scalar: u64) -> Element {
    let mut out = ZERO;
    for i in 0..5 {
        out[i] = f::mul(a[i], scalar);
    }
    out
}

/// Schoolbook, with the `X^5 = 3` fold applied to every product whose degree
/// ran over.
// i, j and k are the polynomial's own indices and j is derived from the other
// two; iterators would hide the identity this has to be checked against.
#[allow(clippy::needless_range_loop)]
pub(crate) fn mul(a: &Element, b: &Element) -> Element {
    let mut out = ZERO;
    for k in 0..5 {
        let mut acc = 0u64;
        for i in 0..5 {
            let j = (k + 5 - i) % 5;
            let product = f::mul(a[i], b[j]);
            acc = if i + j >= 5 {
                f::add(acc, f::mul(W, product))
            } else {
                f::add(acc, product)
            };
        }
        out[k] = acc;
    }
    out
}

pub(crate) fn square(a: &Element) -> Element {
    mul(a, a)
}

pub(crate) fn exp_power_of_2(mut x: Element, power: u32) -> Element {
    for _ in 0..power {
        x = square(&x);
    }
    x
}

/// The Frobenius map applied `count` times: raising to `p^count`, which in
/// this basis is one multiplication per coefficient.
pub(crate) fn repeated_frobenius(x: &Element, count: usize) -> Element {
    if count == 0 {
        return *x;
    }
    if count >= 5 {
        return repeated_frobenius(x, count % 5);
    }
    let mut z0 = DTH_ROOT;
    for _ in 1..count {
        z0 = f::mul(DTH_ROOT, z0);
    }
    let mut out = ZERO;
    let mut z = f::ONE;
    for i in 0..5 {
        out[i] = f::mul(x[i], z);
        z = f::mul(z, z0);
    }
    out
}

pub(crate) fn frobenius(x: &Element) -> Element {
    repeated_frobenius(x, 1)
}

/// `1/a`, or zero for zero.
///
/// Through the norm: `a * a^p * a^(p^2) * a^(p^3) * a^(p^4)` lands in the base
/// field, so inverting there and multiplying back by the other four factors
/// costs one base-field inversion instead of an extension-field one.
pub(crate) fn inverse_or_zero(a: &Element) -> Element {
    if is_zero(a) {
        return ZERO;
    }
    let d = frobenius(a);
    let e = mul(&d, &frobenius(&d));
    let g = mul(&e, &repeated_frobenius(&e, 2));

    let norm = f::add(
        f::mul(a[0], g[0]),
        f::mul(
            W,
            f::add(
                f::add(f::mul(a[1], g[4]), f::mul(a[2], g[3])),
                f::add(f::mul(a[3], g[2]), f::mul(a[4], g[1])),
            ),
        ),
    );
    scalar_mul(&g, f::inverse_or_zero(norm))
}

pub(crate) fn div(a: &Element, b: &Element) -> Element {
    mul(a, &inverse_or_zero(b))
}

/// The Legendre symbol, as a base-field element: one for a square, `p-1` for a
/// non-square, zero for zero.
pub(crate) fn legendre(x: &Element) -> u64 {
    let frob1 = frobenius(x);
    let frob2 = frobenius(&frob1);
    let frob1_times_frob2 = mul(&frob1, &frob2);
    let frob2_frob1_times_frob2 = repeated_frobenius(&frob1_times_frob2, 2);
    let xr_ext = mul(&mul(x, &frob1_times_frob2), &frob2_frob1_times_frob2);
    let xr = xr_ext[0];

    let xr31 = f::exp_power_of_2(xr, 31);
    let xr31_inv = f::inverse_or_zero(xr31);
    let xr63 = f::exp_power_of_2(xr31, 32);
    f::mul(xr63, xr31_inv)
}

/// A square root, or `None`. The algorithm is the reference's, so the root it
/// returns is the same one — which matters, because the curve's decoding picks
/// between two candidate points by looking at it.
pub(crate) fn sqrt(x: &Element) -> Option<Element> {
    let v = exp_power_of_2(*x, 31);
    let d = mul(&mul(x, &exp_power_of_2(v, 32)), &inverse_or_zero(&v));
    let e = frobenius(&mul(&d, &repeated_frobenius(&d, 2)));
    let sq = square(&e);

    let added = f::add(
        f::add(f::mul(x[1], sq[4]), f::mul(x[2], sq[3])),
        f::add(f::mul(x[3], sq[2]), f::mul(x[4], sq[1])),
    );
    let g = f::add(f::mul(x[0], sq[0]), f::mul(W, added));
    let s = f::sqrt(g)?;
    Some(mul(&from_base(s), &inverse_or_zero(&e)))
}

/// The reference's sign convention: the first non-zero coefficient decides,
/// and an even one is "negative" in the sense this uses.
pub(crate) fn sgn0(x: &Element) -> bool {
    let mut sign = false;
    let mut zero = true;
    for limb in x {
        let canonical = f::canonical(*limb);
        let sign_i = (canonical & 1) == 0;
        let zero_i = canonical == 0;
        sign = sign || (zero && sign_i);
        zero = zero && zero_i;
    }
    sign
}

/// The square root with the sign fixed, so one value has one root. The curve's
/// canonical encoding depends on this.
pub(crate) fn canonical_sqrt(x: &Element) -> Option<Element> {
    let root = sqrt(x)?;
    if sgn0(&root) {
        Some(neg(&root))
    } else {
        Some(root)
    }
}

pub(crate) fn to_le_bytes(x: &Element) -> [u8; 40] {
    let mut out = [0u8; 40];
    for i in 0..5 {
        out[i * 8..(i + 1) * 8].copy_from_slice(&f::to_le_bytes(x[i]));
    }
    out
}

/// Read forty bytes as an element, refusing a coefficient that is not already
/// canonical — the reference does the same, and a non-canonical encoding is a
/// second spelling of one value.
pub(crate) fn from_canonical_le_bytes(bytes: &[u8]) -> Option<Element> {
    if bytes.len() != 40 {
        return None;
    }
    let mut out = ZERO;
    for i in 0..5 {
        let mut limb = [0u8; 8];
        limb.copy_from_slice(&bytes[i * 8..(i + 1) * 8]);
        let value = u64::from_le_bytes(limb);
        if value >= super::goldilocks::P {
            return None;
        }
        out[i] = value;
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const A: Element = [1, 2, 3, 4, 5];
    const B: Element = [9, 8, 7, 6, 5];

    #[test]
    fn the_ring_laws_hold() {
        assert!(equals(&add(&A, &B), &add(&B, &A)));
        assert!(equals(&mul(&A, &B), &mul(&B, &A)));
        assert!(equals(&sub(&add(&A, &B), &B), &A));
        assert!(equals(&mul(&A, &ONE), &A));
        assert!(equals(&mul(&A, &ZERO), &ZERO));
        assert!(equals(&double(&A), &add(&A, &A)));
        assert!(equals(&triple(&A), &add(&double(&A), &A)));
        assert!(equals(&square(&A), &mul(&A, &A)));
    }

    #[test]
    fn multiplication_folds_the_fifth_power_by_three() {
        // X^5 = 3, so X^3 * X^3 = 3X. Getting this wrong is the single most
        // likely way for the extension to be subtly not this extension.
        let x3: Element = [0, 0, 0, 1, 0];
        assert!(equals(&mul(&x3, &x3), &[0, 3, 0, 0, 0]));
        let x4: Element = [0, 0, 0, 0, 1];
        assert!(equals(&mul(&x4, &x4), &[0, 0, 0, 3, 0]));
    }

    #[test]
    fn inverse_undoes_multiplication() {
        for a in [A, B, ONE, [0, 0, 1, 0, 0]] {
            assert!(equals(&mul(&a, &inverse_or_zero(&a)), &ONE), "{a:?}");
        }
        assert!(is_zero(&inverse_or_zero(&ZERO)));
        assert!(equals(&div(&A, &B), &mul(&A, &inverse_or_zero(&B))));
    }

    #[test]
    fn frobenius_is_raising_to_the_field_size() {
        // x^(p^5) = x for every element, so five steps come back to where they
        // started, and none of the first four does.
        assert!(equals(&repeated_frobenius(&A, 5), &A));
        assert!(equals(&repeated_frobenius(&A, 0), &A));
        for count in 1..5 {
            assert!(!equals(&repeated_frobenius(&A, count), &A), "{count}");
        }
        // And it is a ring homomorphism.
        assert!(equals(
            &frobenius(&mul(&A, &B)),
            &mul(&frobenius(&A), &frobenius(&B))
        ));
    }

    #[test]
    fn legendre_says_square_or_not_and_sqrt_agrees() {
        let square_of_a = square(&A);
        assert_eq!(legendre(&square_of_a), 1, "a square read as a non-square");
        let root = sqrt(&square_of_a).expect("a square has a root");
        assert!(equals(&square(&root), &square_of_a));
        assert_eq!(legendre(&ZERO), 0);
    }

    #[test]
    fn the_canonical_root_is_one_of_the_two_and_always_the_same_one() {
        let value = square(&A);
        let root = canonical_sqrt(&value).expect("a square has a root");
        assert!(equals(&square(&root), &value));
        assert!(!sgn0(&root), "the canonical root has the fixed sign");
        // Both runs agree, which is what "canonical" has to mean.
        assert!(equals(&root, &canonical_sqrt(&value).unwrap()));
    }

    #[test]
    fn bytes_round_trip_and_a_non_canonical_limb_is_refused() {
        let bytes = to_le_bytes(&A);
        assert_eq!(from_canonical_le_bytes(&bytes), Some(A));
        // p itself is not a canonical limb: it is a second spelling of zero.
        let mut bad = [0u8; 40];
        bad[..8].copy_from_slice(&super::super::goldilocks::P.to_le_bytes());
        assert_eq!(from_canonical_le_bytes(&bad), None);
        assert_eq!(from_canonical_le_bytes(&bytes[..39]), None);
    }
}
