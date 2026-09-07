use super::*;
use crate::tests::{callback_test_fixture, shared_sleeves};
use engine_types::numeric::{Exact, ExactError};

fn approval(quantity: Exact) -> RiskApprovedIntent {
    RiskApprovedIntent {
        intent: Intent {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            exact_quantity: None,
            exact_prices: None,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            tag: "metadata-refusal".into(),
            decided_ns: 1,
            work: None,
            leverage: None,
        },
        client_order_id: "missing-metadata".into(),
        allowed_qty: quantity,
        work: None,
    }
}

#[tokio::test(start_paused = true)]
async fn exact_admission_preserves_missing_metadata_refusals_and_quantity_error_priority() {
    let (mut engine, records) = callback_test_fixture(vec![shared_sleeves::idle("owner")]).await;
    engine.require_exact_instruments = true;
    assert!(engine.instrument_specs.is_empty());
    records.lock().unwrap().clear();

    let error = engine.quantize_approved_order(approval(Exact::parse_decimal("1e400").unwrap()));
    assert!(
        matches!(error, Err(EngineError::State(detail)) if detail == ExactError::RepresentationRange.to_string())
    );
    assert!(records.lock().unwrap().is_empty());

    assert!(engine
        .quantize_approved_order(approval(Exact::parse_decimal("1").unwrap()))
        .unwrap()
        .is_none());
    engine
        .process_set_stop(Some(StrategyId(0)), SymbolId(0), 99.0)
        .await
        .unwrap();

    engine.books.orders.apply(&WalRecord::OrderSent {
        dispatch: None,
        request: OrderRequest {
            client_order_id: "historical-resting".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Limit {
                px: 100.0,
                tif: TimeInForce::Gtc,
            },
            stop: Some(StopSpec { trigger_px: 99.0 }),
            reduce_only: false,
            exact_terms: None,
            sleeve_effect: None,
            close_position: false,
        },
        wire_ns: 1,
        arrival_mid: 100.0,
    });
    assert!(!engine
        .process_amend(
            SymbolId(0),
            "historical-resting",
            AmendSpec {
                px: Some(101.0),
                qty: None,
                exact_terms: None,
            },
            1,
        )
        .await
        .unwrap());

    let records = records.lock().unwrap();
    let notes = records
        .iter()
        .filter_map(|record| match record {
            WalRecord::Note { text, .. } => Some(text.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(notes, [
        "missing-metadata not sent (metadata-refusal): exact instrument metadata is unavailable for this symbol",
        "stop on 0 not moved to 99: exact instrument metadata is unavailable",
        "historical-resting not amended: exact instrument metadata is unavailable",
    ]);
    assert!(records.iter().all(|record| !matches!(
        record,
        WalRecord::OrderSent { .. } | WalRecord::AmendSent { .. } | WalRecord::StopSet { .. }
    )));
    assert!(engine.dispatches.write.is_none());
}
