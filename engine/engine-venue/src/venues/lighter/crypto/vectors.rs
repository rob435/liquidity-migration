//! Every layer of the signing stack, checked against the Go reference's own
//! answers.
//!
//! `reference_vectors.json` beside this file was produced by running
//! `elliottech/poseidon_crypto` — the implementation Lighter itself signs
//! with — over fixed inputs. So these are an independent implementation's
//! output, not a restatement of what this code happens to do, and that is the
//! whole point: nothing else here can tell us whether a round constant has a
//! wrong digit or the curve's doubling is subtly the wrong curve.
//!
//! What is NOT proved: that the venue accepts a signature made this way. Only
//! sending one can prove that, and the funded path is the owner's to arm.
//! What is proved is that this code and the venue's reference agree, byte for
//! byte, at every layer between a transaction's fields and its signature.

use serde_json::Value;

use super::{curve, gfp5, goldilocks as f, poseidon2, scalar, schnorr};

const VECTORS: &str = include_str!("reference_vectors.json");

fn vectors() -> Value {
    serde_json::from_str(VECTORS).expect("the reference vectors are valid JSON")
}

fn u64_at(value: &Value, key: &str) -> u64 {
    value
        .get(key)
        .and_then(Value::as_u64)
        .unwrap_or_else(|| panic!("no u64 at {key} in {value}"))
}

fn five(value: &Value, key: &str) -> [u64; 5] {
    let list = value
        .get(key)
        .and_then(Value::as_array)
        .unwrap_or_else(|| panic!("no list at {key} in {value}"));
    assert_eq!(list.len(), 5, "{key} is not five limbs");
    let mut out = [0u64; 5];
    for (i, item) in list.iter().enumerate() {
        out[i] = item.as_u64().expect("a limb");
    }
    out
}

fn list(value: &Value, key: &str) -> Vec<u64> {
    value
        .get(key)
        .and_then(Value::as_array)
        .unwrap_or_else(|| panic!("no list at {key}"))
        .iter()
        .map(|item| item.as_u64().expect("a limb"))
        .collect()
}

#[test]
fn the_base_field_agrees_with_the_reference() {
    let vectors = vectors();
    let cases = vectors["goldilocks"].as_array().expect("goldilocks cases");
    assert!(cases.len() >= 5, "too few cases to mean anything");
    for case in cases {
        let a = u64_at(case, "a");
        let b = u64_at(case, "b");
        assert_eq!(f::add(f::canonical(a), f::canonical(b)), u64_at(case, "add"), "add {a} {b}");
        assert_eq!(f::sub(f::canonical(a), f::canonical(b)), u64_at(case, "sub"), "sub {a} {b}");
        assert_eq!(f::mul(a, b), u64_at(case, "mul"), "mul {a} {b}");
        assert_eq!(
            f::inverse_or_zero(f::canonical(a)),
            u64_at(case, "inv"),
            "inverse {a}"
        );
    }
}

#[test]
fn the_quintic_extension_agrees_with_the_reference() {
    let vectors = vectors();
    let cases = vectors["gfp5"].as_array().expect("gfp5 cases");
    assert!(cases.len() >= 16, "too few cases to mean anything");
    for case in cases {
        let a = five(case, "a");
        let b = five(case, "b");
        assert_eq!(gfp5::add(&a, &b), five(case, "add"), "add {a:?} {b:?}");
        assert_eq!(gfp5::sub(&a, &b), five(case, "sub"), "sub {a:?} {b:?}");
        assert_eq!(gfp5::mul(&a, &b), five(case, "mul"), "mul {a:?} {b:?}");
    }
}

#[test]
fn the_extensions_harder_operations_agree_with_the_reference() {
    // Square root and the Legendre symbol are what the curve's decoding rests
    // on, and both have two possible answers that only a convention picks
    // between. This is where a disagreement would show.
    let vectors = vectors();
    for case in vectors["gfp5_unary"].as_array().expect("gfp5 unary cases") {
        let a = five(case, "a");
        assert_eq!(gfp5::square(&a), five(case, "square"), "square {a:?}");
        assert_eq!(gfp5::inverse_or_zero(&a), five(case, "inverse"), "inverse {a:?}");
        assert_eq!(gfp5::triple(&a), five(case, "triple"), "triple {a:?}");
        assert_eq!(gfp5::frobenius(&a), five(case, "frobenius"), "frobenius {a:?}");
        assert_eq!(gfp5::legendre(&a), u64_at(case, "legendre"), "legendre {a:?}");

        let expected_ok = case["sqrt_ok"].as_bool().expect("sqrt_ok");
        match gfp5::sqrt(&a) {
            Some(root) => {
                assert!(expected_ok, "{a:?} has a root here and none in the reference");
                assert_eq!(root, five(case, "sqrt"), "sqrt {a:?}");
            }
            None => assert!(!expected_ok, "{a:?} has a root in the reference and none here"),
        }
    }
}

#[test]
fn the_permutation_agrees_with_the_reference() {
    let vectors = vectors();
    let expected = list(&vectors, "poseidon2_permute_0_to_11");
    let mut state = [0u64; 12];
    for (i, lane) in state.iter_mut().enumerate() {
        *lane = i as u64;
    }
    poseidon2::permute(&mut state);
    assert_eq!(
        state.to_vec(),
        expected,
        "the permutation differs; a round constant or a linear layer is wrong"
    );
}

#[test]
fn the_hash_agrees_with_the_reference() {
    let vectors = vectors();
    let cases = vectors["poseidon2"].as_array().expect("poseidon2 cases");
    assert!(cases.len() >= 6, "too few cases to mean anything");
    for case in cases {
        let input = list(case, "input");
        let out = poseidon2::hash_to_quintic_extension(&input);
        assert_eq!(out, five(case, "out"), "hash of {input:?}");
        // And the byte encoding, which is what a signature actually covers.
        let expected_bytes = case["bytes"].as_str().expect("bytes");
        assert_eq!(hex::encode(gfp5::to_le_bytes(&out)), expected_bytes, "{input:?}");
    }
    // One of the cases is sixteen elements — the shape a create-order
    // transaction hashes — so this is not only exercising short inputs.
    assert!(
        cases.iter().any(|c| list(c, "input").len() == 16),
        "no case covers a two-block input"
    );
}

#[test]
fn the_scalar_field_agrees_with_the_reference() {
    let vectors = vectors();
    let cases = vectors["scalar"].as_array().expect("scalar cases");
    assert!(cases.len() >= 16, "too few cases to mean anything");
    for case in cases {
        let a = five(case, "a");
        let b = five(case, "b");
        assert_eq!(scalar::add(&a, &b), five(case, "add"), "add {a:?} {b:?}");
        assert_eq!(scalar::sub(&a, &b), five(case, "sub"), "sub {a:?} {b:?}");
        assert_eq!(scalar::mul(&a, &b), five(case, "mul"), "mul {a:?} {b:?}");
    }
    for case in vectors["scalar_from_gfp5"].as_array().expect("from_gfp5 cases") {
        let a = five(case, "a");
        assert_eq!(scalar::from_gfp5(&a), five(case, "out"), "from_gfp5 {a:?}");
    }
}

#[test]
fn the_curve_agrees_with_the_reference() {
    let vectors = vectors();
    assert_eq!(
        curve::GENERATOR.encode(),
        five(&vectors, "generator_encoded"),
        "the generator is not the reference's generator"
    );
    let cases = vectors["generator_multiples"]
        .as_array()
        .expect("generator multiples");
    assert!(cases.len() >= 4, "too few cases to mean anything");
    for case in cases {
        let s = five(case, "scalar");
        let point = curve::GENERATOR.mul(&s);
        let encoded = point.encode();
        assert_eq!(encoded, five(case, "encoded"), "G * {s:?}");
        assert_eq!(
            hex::encode(gfp5::to_le_bytes(&encoded)),
            case["encoded_bytes"].as_str().expect("encoded_bytes"),
            "G * {s:?} bytes"
        );
        // And it decodes back to the same point, as it does there.
        assert!(case["round_trips"].as_bool().unwrap_or(false));
        let decoded = curve::decode(&encoded).expect("a real point decodes");
        assert!(decoded.equals(&point), "G * {s:?} did not decode back");
    }
}

#[test]
fn a_key_derived_from_a_seed_agrees_with_the_reference() {
    // The venue's own key manager turns a seed into a scalar this way, so a
    // key configured as a seed has to land on the same account here as it
    // would in their SDK.
    let vectors = vectors();
    for case in vectors["schnorr"].as_array().expect("schnorr cases") {
        let seed = hex::decode(case["seed"].as_str().expect("seed")).expect("hex seed");
        let secret = schnorr::secret_from_seed(&seed).expect("a 32-byte seed");
        assert_eq!(secret, five(case, "sk"), "the seed made a different key");
        assert_eq!(
            hex::encode(scalar::to_le_bytes(&secret)),
            case["sk_bytes"].as_str().expect("sk_bytes")
        );
        // And the same key read back from its own bytes.
        assert_eq!(
            schnorr::secret_from_le_bytes(&scalar::to_le_bytes(&secret)).unwrap(),
            secret
        );
    }
}

#[test]
fn the_public_key_agrees_with_the_reference() {
    let vectors = vectors();
    for case in vectors["schnorr"].as_array().expect("schnorr cases") {
        let secret = five(case, "sk");
        let public = schnorr::public_key(&secret);
        assert_eq!(public, five(case, "pk"), "a different public key");
        assert_eq!(
            hex::encode(gfp5::to_le_bytes(&public)),
            case["pk_bytes"].as_str().expect("pk_bytes"),
            "the public key's bytes differ"
        );
    }
}

#[test]
fn a_signature_matches_the_reference_byte_for_byte() {
    // The end of the chain. Same key, same message, same nonce — so the
    // signature is fully determined, and any disagreement anywhere below shows
    // up here as different bytes.
    let vectors = vectors();
    let cases = vectors["schnorr"].as_array().expect("schnorr cases");
    assert!(!cases.is_empty());
    for case in cases {
        let secret = five(case, "sk");
        let message = five(case, "msg");
        let nonce = five(case, "k");
        let signature = schnorr::sign_with_nonce(&message, &secret, &nonce);
        assert_eq!(
            hex::encode(signature.to_bytes()),
            case["sig_bytes"].as_str().expect("sig_bytes"),
            "the signature differs from the reference's"
        );
        assert!(case["valid"].as_bool().unwrap_or(false), "the reference disowned it");
    }
}

#[test]
fn the_references_own_signatures_verify_here() {
    // The other direction: signatures produced there are accepted here. A
    // verifier that only accepts its own signer's output proves nothing.
    let vectors = vectors();
    for case in vectors["schnorr"].as_array().expect("schnorr cases") {
        let public = five(case, "pk");
        let message = five(case, "msg");
        let bytes = hex::decode(case["sig_bytes"].as_str().expect("sig_bytes")).expect("hex");
        let signature = schnorr::Signature::from_bytes(&bytes).expect("eighty bytes");
        assert!(
            schnorr::verify(&public, &message, &signature),
            "a reference signature was rejected"
        );
        // And it is rejected against a message it was not made over.
        let other = poseidon2::hash_to_quintic_extension(&[42]);
        assert!(!schnorr::verify(&public, &other, &signature));
    }
}

#[test]
fn a_signature_made_here_verifies_with_the_derived_nonce_too() {
    // The live path uses a derived nonce rather than the reference's random
    // one, so the bytes differ from the vector by design. What must hold is
    // that the signature is still a valid one for the same key and message.
    let vectors = vectors();
    for case in vectors["schnorr"].as_array().expect("schnorr cases") {
        let secret = five(case, "sk");
        let public = five(case, "pk");
        let message = five(case, "msg");
        let signature = schnorr::sign(&message, &secret);
        assert!(schnorr::verify(&public, &message, &signature));
    }
}
