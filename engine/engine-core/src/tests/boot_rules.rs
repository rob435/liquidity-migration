use super::*;

#[tokio::test]
async fn a_configured_symbol_without_venue_rules_refuses_boot() {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.rules.clear();
    let (risk, _) = MockRisk::with(allow_all());
    let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);

    let result = Engine::boot(
        &settings(),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &[],
    )
    .await;
    let Err(EngineError::Boot(message)) = result else {
        panic!("a configured symbol without quantization rules must refuse boot")
    };
    assert!(message.contains("BTCUSDT"), "{message}");
}

#[tokio::test]
async fn a_historical_dynamic_symbol_without_rules_does_not_block_boot() {
    let replayed = vec![WalRecord::Names {
        strategies: vec!["buyer".to_string()],
        symbols: vec!["BTCUSDT".to_string(), "OLDUSDT".to_string()],
    }];
    let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);
    let _ = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &replayed).await;
}
