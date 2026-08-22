//! The ECgFp5 curve: a prime-order group over `GF(p^5)`.
//!
//! Points are held in the fractional coordinates the reference uses,
//! `(x, u) = (X/Z, U/T)`, which is what makes the addition formulas complete —
//! no special case for doubling, for the neutral element, or for a point and
//! its own negation. That completeness is why a plain double-and-add is safe
//! here.
//!
//! The group has prime order and no cofactor, so a point that decodes is a
//! group element: there is no small subgroup to check for and none to be
//! attacked through.
//!
//! Scalar multiplication is double-and-add, where the reference uses a
//! windowed method with precomputed tables. The answers are the same and the
//! vectors check that; the difference is speed, and one signature per order
//! against a hundred milliseconds of network is not where this engine's time
//! goes.

use super::gfp5::{self, Element};
use super::scalar::{self, Scalar};

/// The curve is `y^2 = x(x^2 + a x + b)` with these coefficients.
const A: Element = [2, 0, 0, 0, 0];
const B1: u64 = 263;
const B: Element = [0, B1, 0, 0, 0];
const B_MUL2: Element = [0, 2 * B1, 0, 0, 0];
const B_MUL4: Element = [0, 4 * B1, 0, 0, 0];

#[derive(Clone, Copy, Debug)]
pub(crate) struct Point {
    x: Element,
    z: Element,
    u: Element,
    t: Element,
}

pub(crate) const NEUTRAL: Point = Point {
    x: gfp5::ZERO,
    z: gfp5::ONE,
    u: gfp5::ZERO,
    t: gfp5::ONE,
};

/// The group's generator, as the reference states it.
pub(crate) const GENERATOR: Point = Point {
    x: [
        12_883_135_586_176_881_569,
        4_356_519_642_755_055_268,
        5_248_930_565_894_896_907,
        2_165_973_894_480_315_022,
        2_448_410_071_095_648_785,
    ],
    z: gfp5::ONE,
    u: gfp5::ONE,
    t: [4, 0, 0, 0, 0],
};

impl Point {
    pub(crate) fn is_neutral(&self) -> bool {
        gfp5::is_zero(&self.u)
    }

    pub(crate) fn equals(&self, other: &Point) -> bool {
        gfp5::equals(
            &gfp5::mul(&self.u, &other.t),
            &gfp5::mul(&other.u, &self.t),
        )
    }

    /// One point, one `GF(p^5)` element. The whole public key is this.
    pub(crate) fn encode(&self) -> Element {
        gfp5::mul(&self.t, &gfp5::inverse_or_zero(&self.u))
    }

    /// The group sum. Complete: no input pair is a special case.
    pub(crate) fn add(&self, rhs: &Point) -> Point {
        let t1 = gfp5::mul(&self.x, &rhs.x);
        let t2 = gfp5::mul(&self.z, &rhs.z);
        let t3 = gfp5::mul(&self.u, &rhs.u);
        let t4 = gfp5::mul(&self.t, &rhs.t);
        let t5 = gfp5::sub(
            &gfp5::mul(&gfp5::add(&self.x, &self.z), &gfp5::add(&rhs.x, &rhs.z)),
            &gfp5::add(&t1, &t2),
        );
        let t6 = gfp5::sub(
            &gfp5::mul(&gfp5::add(&self.u, &self.t), &gfp5::add(&rhs.u, &rhs.t)),
            &gfp5::add(&t3, &t4),
        );
        let t7 = gfp5::add(&t1, &gfp5::mul(&t2, &B));
        let t8 = gfp5::mul(&t4, &t7);
        let t9 = gfp5::mul(
            &t3,
            &gfp5::add(&gfp5::mul(&t5, &B_MUL2), &gfp5::double(&t7)),
        );
        let t10 = gfp5::mul(
            &gfp5::add(&t4, &gfp5::double(&t3)),
            &gfp5::add(&t5, &t7),
        );

        Point {
            x: gfp5::mul(&gfp5::sub(&t10, &t8), &B),
            z: gfp5::sub(&t8, &t9),
            u: gfp5::mul(&t6, &gfp5::sub(&gfp5::mul(&t2, &B), &t1)),
            t: gfp5::add(&t8, &t9),
        }
    }

    pub(crate) fn double(&self) -> Point {
        let t1 = gfp5::mul(&self.z, &self.t);
        let t2 = gfp5::mul(&t1, &self.t);
        let x1 = gfp5::square(&t2);
        let z1 = gfp5::mul(&t1, &self.u);
        let t3 = gfp5::square(&self.u);
        let w1 = gfp5::sub(
            &t2,
            &gfp5::mul(&t3, &gfp5::double(&gfp5::add(&self.x, &self.z))),
        );
        let t4 = gfp5::square(&z1);

        let z_new = gfp5::square(&w1);
        Point {
            x: gfp5::mul(&t4, &B_MUL4),
            z: z_new,
            u: gfp5::sub(
                &gfp5::square(&gfp5::add(&w1, &z1)),
                &gfp5::add(&t4, &z_new),
            ),
            t: gfp5::sub(
                &gfp5::double(&x1),
                &gfp5::add(&gfp5::mul(&t4, &[4, 0, 0, 0, 0]), &z_new),
            ),
        }
    }

    /// `self * s`, most significant bit first.
    pub(crate) fn mul(&self, s: &Scalar) -> Point {
        let bits = scalar::bits(s);
        let mut acc = NEUTRAL;
        for bit in bits.iter().rev() {
            acc = acc.double();
            if *bit {
                acc = acc.add(self);
            }
        }
        acc
    }
}

/// `G * a + P * b`, the sum a signature verification needs.
pub(crate) fn mul_add_g(p: &Point, a: &Scalar, b: &Scalar) -> Point {
    GENERATOR.mul(a).add(&p.mul(b))
}

/// Read a point back from its encoding.
///
/// Canonical: one group element has exactly one encoding, and anything else is
/// refused. That is what stops a signature being replayed under a second
/// spelling of the same key.
pub(crate) fn decode(w: &Element) -> Option<Point> {
    // The curve is y^2 = x(x^2 + a x + b) and the encoding is w = y/x, so
    // dividing through gives x^2 - (w^2 - a)x + b = 0. Exactly one of the two
    // roots is itself a square; the other is the one wanted.
    let e = gfp5::sub(&gfp5::square(w), &A);
    let delta = gfp5::sub(&gfp5::square(&e), &B_MUL4);
    let root = gfp5::canonical_sqrt(&delta);
    let solvable = root.is_some();
    let r = root.unwrap_or(gfp5::ZERO);

    let x1 = gfp5::div(&gfp5::add(&e, &r), &gfp5::TWO);
    let x2 = gfp5::div(&gfp5::sub(&e, &r), &gfp5::TWO);
    let mut x = if gfp5::legendre(&x1) == 1 { x2 } else { x1 };

    if !solvable {
        x = gfp5::ZERO;
    }
    let point = Point {
        x,
        z: gfp5::ONE,
        u: if solvable { gfp5::ONE } else { gfp5::ZERO },
        t: if solvable { *w } else { gfp5::ONE },
    };

    // Zero is the neutral element's encoding, and delta is a non-square there
    // — so it takes the unsolvable branch and is still a success.
    if solvable || gfp5::is_zero(w) {
        Some(point)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn small(n: u64) -> Scalar {
        [n, 0, 0, 0, 0]
    }

    #[test]
    fn the_neutral_element_is_the_identity() {
        assert!(NEUTRAL.is_neutral());
        assert!(!GENERATOR.is_neutral());
        assert!(GENERATOR.add(&NEUTRAL).equals(&GENERATOR));
        assert!(NEUTRAL.add(&GENERATOR).equals(&GENERATOR));
        assert!(NEUTRAL.double().is_neutral());
    }

    #[test]
    fn doubling_agrees_with_adding_to_itself() {
        // The completeness claim, checked rather than assumed: the addition
        // formulas have to handle a point and itself, because double-and-add
        // hands them exactly that.
        let g2 = GENERATOR.double();
        assert!(g2.equals(&GENERATOR.add(&GENERATOR)));
        let g4 = g2.double();
        assert!(g4.equals(&g2.add(&g2)));
        assert!(g4.equals(&GENERATOR.mul(&small(4))));
    }

    #[test]
    fn scalar_multiplication_is_repeated_addition() {
        let mut by_hand = NEUTRAL;
        for n in 0..8u64 {
            assert!(
                GENERATOR.mul(&small(n)).equals(&by_hand),
                "G * {n} is not the {n}th sum"
            );
            by_hand = by_hand.add(&GENERATOR);
        }
    }

    #[test]
    fn the_group_law_is_associative_and_commutative() {
        let a = GENERATOR.mul(&small(3));
        let b = GENERATOR.mul(&small(5));
        let c = GENERATOR.mul(&small(11));
        assert!(a.add(&b).equals(&b.add(&a)));
        assert!(a.add(&b).add(&c).equals(&a.add(&b.add(&c))));
        // And it lines up with adding the scalars.
        assert!(a.add(&b).equals(&GENERATOR.mul(&small(8))));
    }

    #[test]
    fn a_point_encodes_and_decodes_back_to_itself() {
        for n in [1u64, 2, 7, 1234] {
            let p = GENERATOR.mul(&small(n));
            let encoded = p.encode();
            let decoded = decode(&encoded).expect("a real point decodes");
            assert!(decoded.equals(&p), "G * {n} did not survive the round trip");
            // `equals` compares u/t and never looks at x — and x is the only
            // thing `decode` actually computes. Everything that uses a decoded
            // point goes through x, so it is asserted here rather than assumed.
            assert!(
                gfp5::equals(&gfp5::mul(&decoded.x, &p.z), &gfp5::mul(&p.x, &decoded.z)),
                "G * {n} decoded to a different x"
            );
            // And it is a point on the curve, reached again by the same route.
            assert!(gfp5::equals(&decoded.encode(), &encoded));
        }
    }

    #[test]
    fn the_neutral_encodes_as_zero_and_decodes_back() {
        let encoded = NEUTRAL.encode();
        assert!(gfp5::is_zero(&encoded));
        assert!(decode(&encoded).expect("zero is the neutral").is_neutral());
    }

    #[test]
    fn an_element_that_encodes_no_point_is_refused() {
        // Not every GF(p^5) element is a point. Accepting one would mean a
        // public key that verifies signatures nobody could have made.
        let mut refused = 0;
        for n in 2u64..40 {
            if decode(&[n, 0, 0, 0, 0]).is_none() {
                refused += 1;
            }
        }
        assert!(refused > 0, "every element decoded, so decoding checks nothing");
    }

    #[test]
    fn the_combined_multiply_matches_doing_it_in_two_steps() {
        let p = GENERATOR.mul(&small(9));
        let a = small(3);
        let b = small(4);
        assert!(mul_add_g(&p, &a, &b).equals(&GENERATOR.mul(&a).add(&p.mul(&b))));
        // G*3 + (G*9)*4 = G*39
        assert!(mul_add_g(&p, &a, &b).equals(&GENERATOR.mul(&small(39))));
    }
}
