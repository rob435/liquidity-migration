use engine_types::numeric::{Exact, ExactNumber, NumericProvenance};
use engine_types::{SymbolId, SymbolTotal};

#[test]
fn physical_total_preserves_legacy_binary64_without_inventing_decimal_input() {
    let row: SymbolTotal = serde_json::from_str(r#"{"symbol":3,"signed_qty":0.1}"#).unwrap();
    assert!(row.exact_signed_qty.is_none());
    assert_eq!(
        row.exact_quantity().unwrap(),
        Exact::from_legacy_f64(0.1).unwrap()
    );
    assert_ne!(
        row.exact_quantity().unwrap(),
        Exact::parse_decimal("0.1").unwrap()
    );
}

#[test]
fn physical_total_roundtrips_decimal_and_rejects_a_different_projection() {
    let row = SymbolTotal {
        symbol: SymbolId(3),
        signed_qty: 0.1,
        exact_signed_qty: Some(ExactNumber::venue_decimal("0.1").unwrap()),
    };
    let bytes = serde_json::to_vec(&row).unwrap();
    let decoded: SymbolTotal = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(decoded, row);
    assert_eq!(
        decoded.exact_signed_qty.as_ref().unwrap().provenance,
        NumericProvenance::VenueDecimal
    );
    let mut invalid = serde_json::to_value(&row).unwrap();
    invalid["signed_qty"] = serde_json::json!(0.2);
    assert!(serde_json::from_value::<SymbolTotal>(invalid).is_err());
}
