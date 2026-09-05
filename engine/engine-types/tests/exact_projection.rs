use engine_types::numeric::ExactNumber;
use engine_types::order_terms::ExactAmendedTerms;
use engine_types::OrderUpdate;
#[test]
fn serialized_exact_amend_projection_round_trips_the_original_binary64_bits() {
    let price = ExactNumber::venue_decimal("100.123456789012345678901").unwrap();
    let quantity = ExactNumber::venue_decimal("0.123456789012345678901").unwrap();
    let news = OrderUpdate::Amended {
        client_order_id: "typed".into(),
        px: price.value.to_f64().unwrap(),
        qty: quantity.value.to_f64().unwrap(),
        exact_terms: Some(Box::new(ExactAmendedTerms { price, quantity })),
        recv_ns: 1,
    };
    let bytes = serde_json::to_vec(&news).unwrap();
    let decoded: OrderUpdate = serde_json::from_slice(&bytes).unwrap();
    let OrderUpdate::Amended {
        px,
        qty,
        exact_terms: Some(terms),
        ..
    } = &decoded
    else {
        panic!("missing exact news")
    };
    assert!(
        terms.validate_projection(*px, *qty).is_ok(),
        "JSON parsing changed compatibility projection bits beside retained exact values: {}",
        String::from_utf8(bytes).unwrap()
    );
    assert_eq!(decoded, news);
}
