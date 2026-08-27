//! Poseidon2 over Goldilocks, width 12 — the hash Lighter's transactions are
//! signed under.
//!
//! A sponge: absorb eight field elements at a time, permute, then read the
//! first outputs back. The permutation is four full rounds, twenty-two partial
//! ones, four more full ones, with the round constants in [`constants`].
//!
//! The reference carries values unreduced through the linear layers and folds
//! them at the end, for speed. Here every step reduces. Same answers, and
//! `vectors.rs` beside this file checks that against the reference's own output
//! rather than taking it on trust.

use super::constants::{
    EXTERNAL_CONSTANTS, INTERNAL_CONSTANTS, MATRIX_DIAG_12, RATE, ROUNDS_P, WIDTH,
};
use super::gfp5;
use super::goldilocks as f;

/// `x^7`, the S-box.
fn sbox_one(x: u64) -> u64 {
    let x2 = f::square(x);
    let x4 = f::square(x2);
    f::mul(f::mul(x, x2), x4)
}

fn sbox(state: &mut [u64; WIDTH]) {
    for limb in state.iter_mut() {
        *limb = sbox_one(*limb);
    }
}

/// The external (full-round) linear layer: an MDS matrix applied to each block
/// of four, then every lane gains the sum of its column across the three
/// blocks.
fn external_linear_layer(state: &mut [u64; WIDTH]) {
    let mut n = [0u64; WIDTH];
    for chunk in 0..3 {
        let base = chunk * 4;
        let v0 = state[base];
        let v1 = state[base + 1];
        let v2 = state[base + 2];
        let v3 = state[base + 3];
        let t01 = f::add(v0, v1);
        let t23 = f::add(v2, v3);
        let t = f::add(t01, t23);
        n[base] = f::add(f::add(t, t01), v1);
        n[base + 1] = f::add(f::add(t, v1), f::double(v2));
        n[base + 2] = f::add(f::add(t, t23), v3);
        n[base + 3] = f::add(f::add(t, v3), f::double(v0));
    }
    let mut sums = [0u64; 4];
    for (lane, sum) in sums.iter_mut().enumerate() {
        *sum = f::add(f::add(n[lane], n[lane + 4]), n[lane + 8]);
    }
    for lane in 0..WIDTH {
        state[lane] = f::add(n[lane], sums[lane % 4]);
    }
}

fn add_round_constants(state: &mut [u64; WIDTH], round: usize) {
    for (lane, value) in state.iter_mut().enumerate() {
        *value = f::add(*value, f::canonical(EXTERNAL_CONSTANTS[round][lane]));
    }
}

/// The partial rounds: one S-box on the first lane, then a cheap diagonal
/// layer, twenty-two times.
fn partial_rounds(state: &mut [u64; WIDTH]) {
    state[0] = f::add(state[0], f::canonical(INTERNAL_CONSTANTS[0]));
    for round in 0..ROUNDS_P {
        let s0 = sbox_one(state[0]);
        let mut sum = s0;
        for limb in state.iter().skip(1) {
            sum = f::add(sum, *limb);
        }
        // The last partial round carries the FULL round's constants, one per
        // lane, folded in here rather than applied by a separate layer. That
        // is why `permute` never adds `EXTERNAL_CONSTANTS[4]` itself: it is
        // added here.
        let last = round + 1 == ROUNDS_P;
        let mut next = [0u64; WIDTH];
        let first_constant = if last {
            f::canonical(EXTERNAL_CONSTANTS[4][0])
        } else {
            f::canonical(INTERNAL_CONSTANTS[round + 1])
        };
        next[0] = f::add(f::add(f::mul(s0, MATRIX_DIAG_12[0]), sum), first_constant);
        for lane in 1..WIDTH {
            let lane_sum = if last {
                f::add(sum, f::canonical(EXTERNAL_CONSTANTS[4][lane]))
            } else {
                sum
            };
            next[lane] = f::add(f::mul(state[lane], MATRIX_DIAG_12[lane]), lane_sum);
        }
        *state = next;
    }
}

/// One permutation of the whole state.
///
/// Note the round constants: the four before the partial rounds are `[0..=3]`
/// and the three after are `[5..=7]`. Row four is not skipped — it belongs to
/// the full round that follows the partial ones, and [`partial_rounds`] folds
/// it into its own last round rather than applying it separately.
pub(crate) fn permute(state: &mut [u64; WIDTH]) {
    for round in 0..4 {
        external_linear_layer(state);
        add_round_constants(state, round);
        sbox(state);
    }
    external_linear_layer(state);

    partial_rounds(state);

    for round in 5..8 {
        sbox(state);
        external_linear_layer(state);
        add_round_constants(state, round);
    }
    sbox(state);
    external_linear_layer(state);
}

/// Absorb every input, then squeeze `outputs` field elements.
///
/// No padding: the reference hashes exactly what it is given, so an input
/// whose length is not a multiple of the rate leaves the tail of the last
/// block holding whatever the previous permutation left there. Copied rather
/// than improved — a different sponge is a different hash.
pub(crate) fn hash_n_to_m_no_pad(input: &[u64], outputs: usize) -> Vec<u64> {
    let mut state = [0u64; WIDTH];
    let mut at = 0;
    while at < input.len() {
        for lane in 0..RATE {
            if at + lane < input.len() {
                state[lane] = f::canonical(input[at + lane]);
            }
        }
        permute(&mut state);
        at += RATE;
    }
    let mut out = Vec::with_capacity(outputs);
    loop {
        for limb in state.iter().take(RATE) {
            out.push(*limb);
            if out.len() == outputs {
                return out;
            }
        }
        permute(&mut state);
    }
}

/// What a transaction hash is: five field elements, read as one `GF(p^5)`
/// element.
pub(crate) fn hash_to_quintic_extension(input: &[u64]) -> gfp5::Element {
    let out = hash_n_to_m_no_pad(input, 5);
    [out[0], out[1], out[2], out[3], out[4]]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_permutation_changes_every_lane_and_stays_in_the_field() {
        let mut state = [0u64; WIDTH];
        for (i, lane) in state.iter_mut().enumerate() {
            *lane = i as u64;
        }
        let before = state;
        permute(&mut state);
        assert_ne!(state, before);
        for lane in state {
            assert!(lane < f::P, "a lane left the field: {lane}");
        }
    }

    #[test]
    fn the_hash_depends_on_every_input_and_on_their_order() {
        let a = hash_to_quintic_extension(&[1, 2, 3, 4]);
        assert_ne!(a, hash_to_quintic_extension(&[1, 2, 3, 5]));
        assert_ne!(a, hash_to_quintic_extension(&[4, 3, 2, 1]));
        // Deterministic, which the whole scheme rests on.
        assert_eq!(a, hash_to_quintic_extension(&[1, 2, 3, 4]));
    }

    #[test]
    fn a_trailing_zero_is_not_a_different_input() {
        // A property of the sponge, stated so it is known rather than
        // discovered: there is no padding, so an input and the same input with
        // zeros after it write the same state and hash the same.
        //
        // It is safe HERE because every transaction hashes a fixed-length list
        // of fields -- sixteen for an order -- so two different transactions
        // can never differ only in a trailing zero. Anything that later hashes
        // a variable-length list has to carry its own length into the input.
        assert_eq!(
            hash_to_quintic_extension(&[1, 2, 3, 4]),
            hash_to_quintic_extension(&[1, 2, 3, 4, 0])
        );
    }

    #[test]
    fn an_input_longer_than_the_rate_takes_more_than_one_permutation() {
        // Nine elements is two blocks. If the second block were dropped the
        // hash would ignore everything past the eighth field of a
        // transaction — which is most of an order.
        let eight = hash_to_quintic_extension(&[1, 2, 3, 4, 5, 6, 7, 8]);
        let nine = hash_to_quintic_extension(&[1, 2, 3, 4, 5, 6, 7, 8, 9]);
        assert_ne!(eight, nine);
        let sixteen: Vec<u64> = (1..=16).collect();
        let mut changed = sixteen.clone();
        changed[15] = 99;
        assert_ne!(
            hash_to_quintic_extension(&sixteen),
            hash_to_quintic_extension(&changed),
            "the last field of a two-block input did not reach the hash"
        );
    }

    #[test]
    fn squeezing_more_than_the_rate_permutes_again() {
        let out = hash_n_to_m_no_pad(&[1, 2, 3], 12);
        assert_eq!(out.len(), 12);
        // The rate is eight, so the last four come from a second permutation.
        // Without one they would be the first four again — which is the whole
        // of the property, and comparing overlapping windows tested none of
        // it.
        assert_ne!(out[8..12], out[0..4]);
        assert_ne!(out[8..12], out[4..8]);
    }
}

