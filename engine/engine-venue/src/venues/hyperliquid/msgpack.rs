//! MessagePack, written out by hand, because the bytes are hashed.
//!
//! Hyperliquid signs a keccak of the msgpack encoding of the action. That
//! makes the encoding part of the signature: a map whose keys come out in a
//! different order is a different hash, and the venue rejects the signature
//! without ever saying the field order was the problem. Their own docs list
//! this as the first thing people get wrong.
//!
//! So the value type here keeps map entries in a `Vec` in the order they were
//! written, and there is no `Serialize` impl anywhere near it — serde would
//! decide the order for us, in a place nobody would think to look.
//!
//! Only the shapes an action actually uses are encoded: maps, arrays, strings,
//! integers, booleans, and raw 32-byte binary. No floats: every price and size
//! goes over the wire as a decimal string, which is also what makes the hash
//! reproducible from a log.

/// A value on its way into an action hash.
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Mp {
    Str(String),
    Int(i64),
    Bool(bool),
    Arr(Vec<Mp>),
    /// Insertion-ordered, and that order is load-bearing.
    Map(Vec<(&'static str, Mp)>),
}

impl Mp {
    pub(crate) fn str(value: impl Into<String>) -> Mp {
        Mp::Str(value.into())
    }
}

pub(crate) fn encode(value: &Mp) -> Vec<u8> {
    let mut out = Vec::with_capacity(128);
    write(value, &mut out);
    out
}

fn write(value: &Mp, out: &mut Vec<u8>) {
    match value {
        Mp::Str(text) => write_str(text, out),
        Mp::Int(n) => write_int(*n, out),
        Mp::Bool(b) => out.push(if *b { 0xc3 } else { 0xc2 }),
        Mp::Arr(items) => {
            write_len(items.len(), 0x90, 0xdc, 0xdd, out);
            for item in items {
                write(item, out);
            }
        }
        Mp::Map(entries) => {
            write_len(entries.len(), 0x80, 0xde, 0xdf, out);
            for (key, item) in entries {
                write_str(key, out);
                write(item, out);
            }
        }
    }
}

fn write_str(text: &str, out: &mut Vec<u8>) {
    let bytes = text.as_bytes();
    let len = bytes.len();
    if len < 32 {
        out.push(0xa0 | len as u8);
    } else if len < 256 {
        out.push(0xd9);
        out.push(len as u8);
    } else if len < 65536 {
        out.push(0xda);
        out.extend_from_slice(&(len as u16).to_be_bytes());
    } else {
        out.push(0xdb);
        out.extend_from_slice(&(len as u32).to_be_bytes());
    }
    out.extend_from_slice(bytes);
}

/// The smallest encoding that holds the value, which is what Python's
/// `msgpack.packb` picks. Anything wider would hash differently.
fn write_int(n: i64, out: &mut Vec<u8>) {
    if (0..128).contains(&n) {
        out.push(n as u8);
    } else if (-32..0).contains(&n) {
        out.push((n as i8) as u8);
    } else if n >= 0 {
        let n = n as u64;
        if n < 256 {
            out.push(0xcc);
            out.push(n as u8);
        } else if n < 65536 {
            out.push(0xcd);
            out.extend_from_slice(&(n as u16).to_be_bytes());
        } else if n < 4_294_967_296 {
            out.push(0xce);
            out.extend_from_slice(&(n as u32).to_be_bytes());
        } else {
            out.push(0xcf);
            out.extend_from_slice(&n.to_be_bytes());
        }
    } else if n >= -128 {
        out.push(0xd0);
        out.push((n as i8) as u8);
    } else if n >= -32768 {
        out.push(0xd1);
        out.extend_from_slice(&(n as i16).to_be_bytes());
    } else if n >= -2_147_483_648 {
        out.push(0xd2);
        out.extend_from_slice(&(n as i32).to_be_bytes());
    } else {
        out.push(0xd3);
        out.extend_from_slice(&n.to_be_bytes());
    }
}

fn write_len(len: usize, fixed: u8, medium: u8, large: u8, out: &mut Vec<u8>) {
    if len < 16 {
        out.push(fixed | len as u8);
    } else if len < 65536 {
        out.push(medium);
        out.extend_from_slice(&(len as u16).to_be_bytes());
    } else {
        out.push(large);
        out.extend_from_slice(&(len as u32).to_be_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(value: &Mp) -> String {
        hex::encode(encode(value))
    }

    #[test]
    fn small_integers_use_the_one_byte_forms() {
        // These are the widths `msgpack.packb` chooses, and a wider one would
        // change the hash the venue checks the signature against.
        assert_eq!(hex(&Mp::Int(0)), "00");
        assert_eq!(hex(&Mp::Int(127)), "7f");
        assert_eq!(hex(&Mp::Int(-1)), "ff");
        assert_eq!(hex(&Mp::Int(-32)), "e0");
        assert_eq!(hex(&Mp::Int(128)), "cc80");
        assert_eq!(hex(&Mp::Int(255)), "ccff");
        assert_eq!(hex(&Mp::Int(256)), "cd0100");
        assert_eq!(hex(&Mp::Int(65536)), "ce00010000");
        assert_eq!(hex(&Mp::Int(4_294_967_296)), "cf0000000100000000");
        assert_eq!(hex(&Mp::Int(-33)), "d0df");
        assert_eq!(hex(&Mp::Int(-129)), "d1ff7f");
    }

    #[test]
    fn short_strings_are_fixstr() {
        assert_eq!(hex(&Mp::str("")), "a0");
        assert_eq!(hex(&Mp::str("order")), "a56f72646572");
        let long = "x".repeat(40);
        let encoded = hex(&Mp::str(&long));
        assert!(encoded.starts_with("d928"), "{encoded}");
    }

    #[test]
    fn booleans_and_arrays_and_maps_take_their_fixed_forms() {
        assert_eq!(hex(&Mp::Bool(true)), "c3");
        assert_eq!(hex(&Mp::Bool(false)), "c2");
        assert_eq!(hex(&Mp::Arr(vec![Mp::Int(1), Mp::Int(2)])), "920102");
        assert_eq!(hex(&Mp::Map(vec![("a", Mp::Int(1))])), "81a16101");
    }

    #[test]
    fn map_order_is_the_order_it_was_written_in() {
        // The property the whole module exists for. A serde-derived encoder
        // would be free to sort these, and the signature would stop verifying
        // with nothing in the reply to say why.
        let one = Mp::Map(vec![("a", Mp::Int(1)), ("b", Mp::Int(2))]);
        let other = Mp::Map(vec![("b", Mp::Int(2)), ("a", Mp::Int(1))]);
        assert_ne!(encode(&one), encode(&other));
        assert_eq!(hex(&one), "82a16101a16202");
    }
}
