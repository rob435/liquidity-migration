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

#[tokio::test]
async fn an_appended_sleeve_preserves_existing_wal_strategy_ids() {
    let replayed = vec![WalRecord::Names {
        strategies: vec!["carry".to_string(), "long".to_string()],
        symbols: vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()],
    }];
    let (carry, _) = Buyer::new("BTCUSDT", 1, 0.01);
    let (long, _) = Buyer::new("ETHUSDT", 1, 0.01);
    let (exodus, _) = Buyer::new("DOGEUSDT", 1, 0.01);
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (venue, _) = MockVenue::new(tape, &["BTCUSDT", "ETHUSDT", "DOGEUSDT"]);
    let (risk, _) = MockRisk::with(allow_all());

    Engine::boot_as(
        &settings(),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(carry), Box::new(long), Box::new(exodus)],
        &[
            "carry".to_string(),
            "long".to_string(),
            "exodus".to_string(),
        ],
        &replayed,
    )
    .await
    .expect("a suffix addition leaves every existing id unchanged");
}

#[tokio::test]
async fn changing_an_existing_wal_strategy_id_still_refuses_boot() {
    let replayed = vec![WalRecord::Names {
        strategies: vec!["carry".to_string(), "long".to_string()],
        symbols: vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()],
    }];
    let (long, _) = Buyer::new("ETHUSDT", 1, 0.01);
    let (carry, _) = Buyer::new("BTCUSDT", 1, 0.01);
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (venue, _) = MockVenue::new(tape, &["BTCUSDT", "ETHUSDT"]);
    let (risk, _) = MockRisk::with(allow_all());

    let result = Engine::boot_as(
        &settings(),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(long), Box::new(carry)],
        &["long".to_string(), "carry".to_string()],
        &replayed,
    )
    .await;
    let Err(EngineError::Boot(message)) = result else {
        panic!("reordering an existing strategy table must refuse boot")
    };
    assert!(
        message.contains("does not preserve the WAL prefix"),
        "{message}"
    );
}
