//! The signing stack Lighter's transactions need, in Rust.
//!
//! Lighter does not sign with an HMAC. A transaction is hashed into a
//! `GF(p^5)` element with Poseidon2 and then signed with Schnorr over the
//! ECgFp5 curve — a zero-knowledge-friendly stack that has no equivalent in
//! any of the usual crypto crates.
//!
//! **Why it is ported rather than linked.** The venue ships a Go reference and
//! prebuilt shared libraries. Linking one would put a C ABI and a second
//! toolchain on the order path and in the deploy, for one signature per order.
//! So the arithmetic is here, in five small modules, each pinned against the
//! reference's own answers by `vectors.rs` beside this file — the vectors in that
//! file were produced by running the Go reference, so they are an independent
//! implementation's output and not this one's.
//!
//! Read it bottom-up: [`goldilocks`] is the base field, [`gfp5`] the quintic
//! extension the curve lives over, [`poseidon2`] the hash, [`scalar`] the
//! group's scalar field, [`curve`] the points, [`schnorr`] the signature.

// A complete implementation of the scheme, not only the half the live path
// walks. Verification, point decoding and the field's inverse are exercised by
// `vectors.rs` and by nothing else — and a signer that cannot check its own
// work is a signer nobody can test against the reference, which is the whole
// basis for trusting this code at all.
#![allow(dead_code)]

pub(crate) mod constants;
pub(crate) mod curve;
pub(crate) mod gfp5;
pub(crate) mod goldilocks;
pub(crate) mod poseidon2;
pub(crate) mod scalar;
pub(crate) mod schnorr;

#[cfg(test)]
mod vectors;
