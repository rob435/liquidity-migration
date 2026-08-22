//! Lighter transactions: the fields, the hash over them, and the JSON the
//! venue is sent.
//!
//! A transaction is not a request body that happens to be signed. It is a
//! fixed list of integers, hashed with Poseidon2 into one `GF(p^5)` element
//! and signed with Schnorr; the JSON is only how the signed thing travels. So
//! the field ORDER below is the contract — the same way Hyperliquid's msgpack
//! key order is — and a field inserted in the middle changes the hash of every
//! transaction after it.
//!
//! The lists are copied from the venue's reference
//! (`elliottech/lighter-go`, `types/txtypes/create_order.go` and
//! `cancel_order.go`), and the JSON field names are the Go struct's own field
//! names, which is what `encoding/json` emits for a struct carrying no tags.
//!
//! Every price and size here is an integer in the market's own units, not a
//! decimal: the venue's fields are `uint32` and `int64`, and a rounding done
//! anywhere but at the boundary would be a rounding the signature covers.

use serde_json::{json, Value};

use super::crypto::gfp5::Element;
use super::crypto::poseidon2::hash_to_quintic_extension;

/// The transaction types this adapter sends. The venue's own numbering.
pub(crate) const TX_TYPE_CREATE_ORDER: u8 = 14;
pub(crate) const TX_TYPE_CANCEL_ORDER: u8 = 15;

/// Order types, as the venue numbers them.
pub(crate) const ORDER_TYPE_LIMIT: u8 = 0;
pub(crate) const ORDER_TYPE_MARKET: u8 = 1;
pub(crate) const ORDER_TYPE_STOP_LOSS: u8 = 2;

/// Time in force, as the venue numbers them.
pub(crate) const TIF_IMMEDIATE_OR_CANCEL: u8 = 0;
pub(crate) const TIF_GOOD_TILL_TIME: u8 = 1;
pub(crate) const TIF_POST_ONLY: u8 = 2;

/// "No value here". The venue's own sentinels, and not interchangeable with
/// zero: a zero trigger price is a real price and a zero expiry is not.
pub(crate) const NIL_TRIGGER_PRICE: u32 = 0;
pub(crate) const NIL_ORDER_EXPIRY: i64 = 0;

/// A create-order transaction, in the venue's own units.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct CreateOrder {
    pub(crate) account_index: i64,
    pub(crate) api_key_index: u8,
    pub(crate) market_index: i16,
    /// The engine's own order id, as the integer the venue allows for one.
    pub(crate) client_order_index: i64,
    /// Size in the market's base units.
    pub(crate) base_amount: i64,
    /// Price in the market's price units.
    pub(crate) price: u32,
    /// One for a sell, zero for a buy. The venue's spelling is "is ask".
    pub(crate) is_ask: u8,
    pub(crate) order_type: u8,
    pub(crate) time_in_force: u8,
    pub(crate) reduce_only: u8,
    pub(crate) trigger_price: u32,
    pub(crate) order_expiry: i64,
    /// When the transaction itself stops being valid, in milliseconds.
    pub(crate) expired_at: i64,
    pub(crate) nonce: i64,
}

impl CreateOrder {
    /// The sixteen field elements the signature covers, in the venue's order.
    fn fields(&self, chain_id: u32) -> [u64; 16] {
        [
            chain_id as u64,
            TX_TYPE_CREATE_ORDER as u64,
            field(self.nonce),
            field(self.expired_at),
            field(self.account_index),
            self.api_key_index as u64,
            field(self.market_index as i64),
            field(self.client_order_index),
            field(self.base_amount),
            self.price as u64,
            self.is_ask as u64,
            self.order_type as u64,
            self.time_in_force as u64,
            self.reduce_only as u64,
            self.trigger_price as u64,
            field(self.order_expiry),
        ]
    }

    pub(crate) fn hash(&self, chain_id: u32) -> Element {
        hash_to_quintic_extension(&self.fields(chain_id))
    }

    /// The JSON the venue reads. Field names are the reference struct's own.
    pub(crate) fn to_json(&self, signature: &[u8; 80]) -> Value {
        json!({
            "AccountIndex": self.account_index,
            "ApiKeyIndex": self.api_key_index,
            "MarketIndex": self.market_index,
            "ClientOrderIndex": self.client_order_index,
            "BaseAmount": self.base_amount,
            "Price": self.price,
            "IsAsk": self.is_ask,
            "Type": self.order_type,
            "TimeInForce": self.time_in_force,
            "ReduceOnly": self.reduce_only,
            "TriggerPrice": self.trigger_price,
            "OrderExpiry": self.order_expiry,
            "ExpiredAt": self.expired_at,
            "Nonce": self.nonce,
            // Go marshals a []byte as base64, so that is what the venue reads.
            "Sig": base64(signature),
        })
    }
}

/// A cancel-order transaction.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct CancelOrder {
    pub(crate) account_index: i64,
    pub(crate) api_key_index: u8,
    pub(crate) market_index: i16,
    /// The client order index the order was placed with.
    pub(crate) index: i64,
    pub(crate) expired_at: i64,
    pub(crate) nonce: i64,
}

impl CancelOrder {
    fn fields(&self, chain_id: u32) -> [u64; 8] {
        [
            chain_id as u64,
            TX_TYPE_CANCEL_ORDER as u64,
            field(self.nonce),
            field(self.expired_at),
            field(self.account_index),
            self.api_key_index as u64,
            field(self.market_index as i64),
            field(self.index),
        ]
    }

    pub(crate) fn hash(&self, chain_id: u32) -> Element {
        hash_to_quintic_extension(&self.fields(chain_id))
    }

    pub(crate) fn to_json(&self, signature: &[u8; 80]) -> Value {
        json!({
            "AccountIndex": self.account_index,
            "ApiKeyIndex": self.api_key_index,
            "MarketIndex": self.market_index,
            "Index": self.index,
            "ExpiredAt": self.expired_at,
            "Nonce": self.nonce,
            "Sig": base64(signature),
        })
    }
}

/// A signed field is a field element, and the reference reaches one by casting
/// the integer straight to the field's `u64`. A negative value therefore wraps
/// exactly as it does there — which matters only for the account index of the
/// treasury, and matters absolutely that it is the same wrap.
fn field(value: i64) -> u64 {
    value as u64
}

/// Standard base64, which is what Go's `encoding/json` writes a `[]byte` as.
fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(triple >> 18) as usize & 63] as char);
        out.push(ALPHABET[(triple >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[(triple >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[triple as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn order() -> CreateOrder {
        CreateOrder {
            account_index: 42,
            api_key_index: 3,
            market_index: 0,
            client_order_index: 99,
            base_amount: 1_000_000,
            price: 2_500_000,
            is_ask: 0,
            order_type: ORDER_TYPE_LIMIT,
            time_in_force: TIF_GOOD_TILL_TIME,
            reduce_only: 0,
            trigger_price: NIL_TRIGGER_PRICE,
            order_expiry: 1_735_689_600_000,
            expired_at: 1_735_689_600_000,
            nonce: 7,
        }
    }

    #[test]
    fn the_signed_fields_are_the_venues_sixteen_in_the_venues_order() {
        // Copied from `create_order.go`. The order is the contract: a field
        // moved is every later transaction hashing differently — and the
        // venue re-derives this same list server-side, so a swap here is an
        // order signed for something other than what was asked.
        //
        // Every index is asserted, and the values are picked to differ from
        // one another wherever the field's own range allows: two fields that
        // hold the same number cannot be told apart by any test, and swapping
        // them changes nothing the venue would see.
        let distinct = CreateOrder {
            account_index: 42,
            api_key_index: 3,
            market_index: 5,
            client_order_index: 99,
            base_amount: 1_000_000,
            price: 2_500_000,
            is_ask: 1,
            order_type: ORDER_TYPE_STOP_LOSS,
            time_in_force: TIF_GOOD_TILL_TIME,
            reduce_only: 1,
            trigger_price: 2_400_000,
            order_expiry: 1_735_689_600_000,
            expired_at: 1_700_000_000_000,
            nonce: 7,
        };
        let fields = distinct.fields(304);
        assert_eq!(fields.len(), 16);
        assert_eq!(fields[0], 304, "the chain id leads");
        assert_eq!(fields[1], u64::from(TX_TYPE_CREATE_ORDER), "transaction type");
        assert_eq!(fields[2], 7, "nonce");
        assert_eq!(fields[3], 1_700_000_000_000, "expired at");
        assert_eq!(fields[4], 42, "account index");
        assert_eq!(fields[5], 3, "api key index");
        assert_eq!(fields[6], 5, "market index");
        assert_eq!(fields[7], 99, "client order index");
        assert_eq!(fields[8], 1_000_000, "base amount");
        assert_eq!(fields[9], 2_500_000, "price");
        assert_eq!(fields[10], 1, "is ask");
        assert_eq!(fields[11], u64::from(ORDER_TYPE_STOP_LOSS), "order type");
        assert_eq!(fields[12], u64::from(TIF_GOOD_TILL_TIME), "time in force");
        assert_eq!(fields[13], 1, "reduce only");
        assert_eq!(fields[14], 2_400_000, "trigger price");
        assert_eq!(fields[15], 1_735_689_600_000, "order expiry");
    }

    #[test]
    fn every_field_reaches_the_hash() {
        // One at a time, because a field left out of the list would still
        // produce a hash — just not one that covers it, and an order whose
        // price is not signed is an order anyone can reprice.
        let base = order();
        let reference = base.hash(304);
        let mut changed = Vec::new();

        let mut m = base.clone();
        m.nonce += 1;
        changed.push(("nonce", m.hash(304)));
        let mut m = base.clone();
        m.expired_at += 1;
        changed.push(("expired_at", m.hash(304)));
        let mut m = base.clone();
        m.account_index += 1;
        changed.push(("account_index", m.hash(304)));
        let mut m = base.clone();
        m.api_key_index += 1;
        changed.push(("api_key_index", m.hash(304)));
        let mut m = base.clone();
        m.market_index += 1;
        changed.push(("market_index", m.hash(304)));
        let mut m = base.clone();
        m.client_order_index += 1;
        changed.push(("client_order_index", m.hash(304)));
        let mut m = base.clone();
        m.base_amount += 1;
        changed.push(("base_amount", m.hash(304)));
        let mut m = base.clone();
        m.price += 1;
        changed.push(("price", m.hash(304)));
        let mut m = base.clone();
        m.is_ask = 1;
        changed.push(("is_ask", m.hash(304)));
        let mut m = base.clone();
        m.order_type = ORDER_TYPE_MARKET;
        changed.push(("order_type", m.hash(304)));
        let mut m = base.clone();
        m.time_in_force = TIF_POST_ONLY;
        changed.push(("time_in_force", m.hash(304)));
        let mut m = base.clone();
        m.reduce_only = 1;
        changed.push(("reduce_only", m.hash(304)));
        let mut m = base.clone();
        m.trigger_price = 1;
        changed.push(("trigger_price", m.hash(304)));
        let mut m = base.clone();
        m.order_expiry += 1;
        changed.push(("order_expiry", m.hash(304)));

        for (name, hash) in changed {
            assert_ne!(hash, reference, "changing {name} did not change the hash");
        }
    }

    #[test]
    fn the_chain_id_reaches_the_hash() {
        // The replay fence between the two networks. Without it a testnet
        // order is a valid mainnet order.
        assert_ne!(order().hash(300), order().hash(304));
        assert_ne!(
            CancelOrder {
                account_index: 42,
                api_key_index: 3,
                market_index: 0,
                index: 99,
                expired_at: 1,
                nonce: 2,
            }
            .hash(300),
            CancelOrder {
                account_index: 42,
                api_key_index: 3,
                market_index: 0,
                index: 99,
                expired_at: 1,
                nonce: 2,
            }
            .hash(304)
        );
    }

    #[test]
    fn a_cancel_and_an_order_never_hash_the_same() {
        // The transaction type is the second field for exactly this reason.
        let cancel = CancelOrder {
            account_index: 42,
            api_key_index: 3,
            market_index: 0,
            index: 99,
            expired_at: 1_735_689_600_000,
            nonce: 7,
        };
        assert_ne!(cancel.hash(304), order().hash(304));
    }

    #[test]
    fn the_json_carries_every_field_the_hash_covers() {
        let signature = [7u8; 80];
        let body = order().to_json(&signature);
        for name in [
            "AccountIndex",
            "ApiKeyIndex",
            "MarketIndex",
            "ClientOrderIndex",
            "BaseAmount",
            "Price",
            "IsAsk",
            "Type",
            "TimeInForce",
            "ReduceOnly",
            "TriggerPrice",
            "OrderExpiry",
            "ExpiredAt",
            "Nonce",
            "Sig",
        ] {
            assert!(body.get(name).is_some(), "the body has no {name}");
        }
        assert_eq!(body["Price"], 2_500_000);
        assert_eq!(body["Nonce"], 7);
    }

    #[test]
    fn the_signature_travels_as_base64() {
        // Go marshals a byte slice that way, so anything else is a signature
        // the venue reads as gibberish.
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64(&[0xFB, 0xFF, 0xFE]), "+//+");
        let signature = [7u8; 80];
        let encoded = base64(&signature);
        assert_eq!(encoded.len(), 108, "80 bytes is 108 base64 characters");
    }
}
