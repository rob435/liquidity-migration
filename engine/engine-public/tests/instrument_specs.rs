use engine_public::venues::{binance, bybit, lighter, mexc};
use engine_types::numeric::{AssetId, Exact, PricePrecision};
fn exact(text: &str) -> Exact {
    Exact::parse_decimal(text).unwrap()
}

#[test]
fn bybit_metadata_preserves_lexical_filters_cursor_and_explicit_currency_absence() {
    let raw = r#"{"retCode":0,"result":{"nextPageCursor":"next\u0026page","list":[{"symbol":"ODDUSDT","baseCoin":"ODD","priceFilter":{"tickSize":0.000000000000000000123456789},"lotSizeFilter":{"qtyStep":"0.0003","minOrderQty":"0.0006","maxOrderQty":"9007199254740993.1","maxMktOrderQty":"100","minNotionalValue":"5.5"}}]}}"#;
    let (rows, cursor) = bybit::spec::parse_page(raw).unwrap();
    let spec = &rows[0].1;
    assert_eq!(cursor, "next&page");
    assert_eq!(rows[0].0, "ODDUSDT");
    assert_eq!(spec.native_symbol, "ODDUSDT");
    assert_eq!(spec.tick_size, Some(exact("0.000000000000000000123456789")));
    assert_eq!(spec.max_qty, Some(exact("9007199254740993.1")));
    assert_eq!(spec.base_asset, AssetId::Named("ODD".into()));
    assert_eq!(spec.quote_asset, AssetId::Unknown);
    assert_eq!(spec.settlement_asset, AssetId::Unknown);
    assert_eq!(spec.fee_assets, None);
    let (rows, _) = bybit::spec::parse_page(&raw.replace(
        "\"baseCoin\":\"ODD\"",
        "\"baseCoin\":\"ODD\",\"quoteCoin\":\"UNLISTED\",\"settleCoin\":\"UNLISTED\"",
    ))
    .unwrap();
    assert_eq!(
        rows[0].1.settlement_asset,
        AssetId::Named("UNLISTED".into())
    );
    assert!(
        bybit::spec::parse_page(&raw.replace("\"qtyStep\":\"0.0003\"", "\"qtyStep\":true"))
            .is_err()
    );
}

#[test]
fn binance_keeps_limit_and_market_grids_separate_and_zero_price_caps_disabled() {
    let raw = r#"{"symbols":[{"symbol":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT","marginAsset":"USDT","status":"TRADING","contractType":"PERPETUAL","filters":[{"filterType":"PRICE_FILTER","tickSize":"0.1","minPrice":"0","maxPrice":"0"},{"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"1000"},{"filterType":"MARKET_LOT_SIZE","stepSize":"0.005","minQty":"0.010","maxQty":"100"},{"filterType":"MIN_NOTIONAL","notional":"5"}]}]}"#;
    let rows = binance::spec::parse(raw).unwrap();
    let spec = &rows[0].1;
    assert_eq!(spec.qty_step, Some(exact("0.001")));
    assert_eq!(spec.market_qty_step, Some(exact("0.005")));
    assert_eq!(spec.min_qty, Some(exact("0.001")));
    assert_eq!(spec.market_min_qty, Some(exact("0.010")));
    assert_eq!(spec.max_qty, Some(exact("1000")));
    assert_eq!(spec.max_market_qty, Some(exact("100")));
    assert_eq!(spec.min_price, None);
    assert_eq!(spec.max_price, None);
    assert_eq!(spec.settlement_asset, AssetId::Named("USDT".into()));
    assert!(
        binance::spec::parse(&raw.replace(
            "\"stepSize\":\"0.005\"",
            "\"stepSize\":\"0.0010000000000000000001\""
        ))
        .is_err(),
        "noncommensurate exact grids were accepted through float tolerance"
    );
}

#[test]
fn mexc_metadata_multiplies_base_quantities_without_losing_unquoted_decimals() {
    let raw = r#"{"data":[{"symbol":"ODD_TOKEN","baseCoin":"ODD","quoteCoin":"TOKEN","settleCoin":"TOKEN","contractSize":0.000000000000000000123456789,"priceUnit":"0.1","minVol":3,"maxVol":7,"limitMaxVol":11,"apiAllowed":true}]}"#;
    let table = mexc::contracts::Contracts::parse_raw(raw).unwrap();
    let rows = table.instrument_specs();
    let spec = &rows[0].1;
    assert_eq!(spec.native_symbol, "ODD_TOKEN");
    assert_eq!(
        spec.contract_multiplier,
        Some(exact("0.000000000000000000123456789"))
    );
    assert_eq!(spec.min_qty, Some(exact("0.000000000000000000370370367")));
    assert_eq!(
        spec.max_market_qty,
        Some(exact("0.000000000000000000864197523"))
    );
    assert_eq!(spec.max_qty, Some(exact("0.000000000000000001358024679")));
    assert_eq!(spec.settlement_asset, AssetId::Named("TOKEN".into()));
    assert_eq!(spec.fee_assets, None);
    assert_eq!(spec.min_notional, None);
}

#[test]
fn lighter_metadata_exposes_both_wire_integer_bounds_without_alias_currency_inference() {
    let raw = r#"{"code":200,"order_book_details":[{"symbol":"ODD","market_id":3,"status":"active","supported_size_decimals":3,"supported_price_decimals":2,"min_base_amount":"0.002","min_quote_amount":"10"}]}"#;
    let rows = lighter::parse::parse_markets_raw(raw).unwrap();
    let spec = rows[0].exact_spec.as_ref().unwrap();
    assert_eq!(spec.native_symbol, "ODD");
    assert_eq!(spec.price_precision, PricePrecision::Tick);
    assert_eq!(spec.max_price, Some(exact("42949672.95")));
    assert_eq!(spec.max_qty, Some(exact("281474976710.655")));
    assert_eq!(spec.min_price, Some(exact("0.01")));
    assert_eq!(spec.settlement_asset, AssetId::Unknown);
    assert_eq!(spec.quote_asset, AssetId::Unknown);
}

#[test]
fn duplicate_known_metadata_fields_use_last_raw_value_and_errors_need_no_success_payload() {
    let raw = r#"{"retCode":1,"retCode":0,"result":{"list":[{"symbol":"X","priceFilter":{"tickSize":"wrong","tickSize":0.123456789012345678901},"lotSizeFilter":{"qtyStep":"1","minOrderQty":"1"}}]}}"#;
    let (rows, _) = bybit::spec::parse_page(raw).unwrap();
    assert_eq!(rows[0].1.tick_size, Some(exact("0.123456789012345678901")));
    assert!(matches!(
        bybit::spec::parse_page(r#"{"retCode":10006,"retMsg":"busy"}"#),
        Err(engine_types::VenueError::Rejected { code: 10006, .. })
    ));
}
