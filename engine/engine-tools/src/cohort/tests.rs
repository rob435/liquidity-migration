use super::*;

use engine_types::order_dispatch::QueuedOrderDispatch;
use engine_types::{
    DenyReason, Intent, OrderKind, OrderRequest, OrderUpdate, RiskVerdict, Side, SignalObservation,
    StrategyId, SymbolId, SIGNAL_OBSERVATION_SCHEMA_VERSION,
};

const DESTINATION: StrategyId = StrategyId(1);

fn observation(
    kind: &str,
    sequence: u64,
    observed_wall_ts_ms: i64,
    payload: &[u8],
) -> SignalObservation {
    let mut observation = SignalObservation {
        schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
        decision_fingerprint: "fingerprint".into(),
        destination: DESTINATION,
        source: "worker.long".into(),
        sequence,
        observation_id: format!("{kind}-{sequence}"),
        kind: kind.into(),
        observed_wall_ts_ms,
        available_wall_ts_ms: observed_wall_ts_ms,
        subscriptions: Vec::new(),
        payload: payload.to_vec(),
        content_sha256: String::new(),
    };
    observation.content_sha256 = engine_core::signals::content_sha256(&observation);
    observation
}

fn row(kind: &str, sequence: u64, observed_wall_ts_ms: i64) -> WalRecord {
    WalRecord::SignalObservation {
        wall_ts_ms: observed_wall_ts_ms,
        observation: observation(kind, sequence, observed_wall_ts_ms, b"{}"),
    }
}

fn gate_row(sequence: u64, observed_wall_ts_ms: i64, valid_until_ms: i64) -> WalRecord {
    let payload = format!(
        r#"{{"schema_version":1,"payload":{{"kind":"llm_gate_candidates","decision_ts_ms":{observed_wall_ts_ms},"valid_until_ms":{valid_until_ms},"btc_rv_30":null,"rows":[]}}}}"#
    );
    WalRecord::SignalObservation {
        wall_ts_ms: observed_wall_ts_ms,
        observation: observation(GATE_KIND, sequence, observed_wall_ts_ms, payload.as_bytes()),
    }
}

fn consumed(kind: &str, sequence: u64, wall_ts_ms: i64) -> WalRecord {
    WalRecord::SignalObservationConsumed {
        wall_ts_ms,
        strategy: DESTINATION,
        source: "worker.long".into(),
        sequence,
        observation_id: format!("{kind}-{sequence}"),
    }
}

fn rejected(kind: &str, sequence: u64, wall_ts_ms: i64, reason: &str) -> WalRecord {
    WalRecord::SignalObservationRejected {
        wall_ts_ms,
        strategy: DESTINATION,
        source: "worker.long".into(),
        sequence,
        observation_id: format!("{kind}-{sequence}"),
        reason: reason.into(),
    }
}

fn intent(decided_ns: u64) -> Intent {
    Intent {
        exact_prices: None,
        exact_quantity: None,
        strategy: DESTINATION,
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 1.0,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
        tag: "long_native_entry".into(),
        decided_ns,
        work: None,
        leverage: None,
    }
}

fn intent_record(decided_ns: u64) -> WalRecord {
    WalRecord::Intent {
        intent: intent(decided_ns),
    }
}

fn allow(id: &str) -> WalRecord {
    WalRecord::Verdict {
        client_order_id: Some(id.into()),
        verdict: RiskVerdict::Allow { qty: 1.0 },
    }
}

fn deny(reason: DenyReason) -> WalRecord {
    WalRecord::Verdict {
        client_order_id: None,
        verdict: RiskVerdict::Deny { reason },
    }
}

fn request(id: &str) -> OrderRequest {
    OrderRequest {
        exact_terms: None,
        sleeve_effect: None,
        client_order_id: id.into(),
        strategy: DESTINATION,
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 1.0,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
        close_position: false,
    }
}

fn sent(id: &str, decided_ns: u64, wire_ns: u64) -> WalRecord {
    WalRecord::OrderSent {
        dispatch: Some(Box::new(QueuedOrderDispatch {
            intent: intent(decided_ns),
            origin_ns: decided_ns,
        })),
        request: request(id),
        wire_ns,
        arrival_mid: 100.0,
    }
}

fn reject_update(id: &str, reason: &str) -> WalRecord {
    WalRecord::OrderUpdate {
        callbacks: None,
        update: OrderUpdate::Reject {
            client_order_id: id.into(),
            code: 0,
            reason: reason.into(),
        },
    }
}

fn note(text: &str) -> WalRecord {
    WalRecord::Note {
        source: "engine".into(),
        text: text.into(),
    }
}

fn boot_at(wall_ts_ms: i64) -> WalRecord {
    WalRecord::Boot {
        version: "test".into(),
        config_sha256: String::new(),
        wall_ts_ms,
        commit: String::new(),
    }
}

fn segment_base(observations: &[SignalObservation]) -> WalRecord {
    let base = serde_json::json!({
        "kind": "segment_base_v7",
        "wall_ts_ms": 9_000,
        "strategies": ["LONG", "CARRY"],
        "symbols": ["BTCUSDT"],
        "may_open": true,
        "control_anchors": [],
        "attribution": [],
        "logged_exposure": [],
        "intended_stops": [],
        "open_orders": [],
        "signal_observations": observations,
    });
    serde_json::from_value(base).expect("segment restatement")
}

/// One of each fate in both lanes, one row that is no opportunity, and the
/// ages hand-computed from the stamps below.
fn mixed_log() -> Vec<WalRecord> {
    vec![
        boot_at(1),
        // consumed 100 ms after the source stamped it
        row("long_feature_batch", 1, 1_000),
        consumed("long_feature_batch", 1, 1_100),
        // rejected 300 ms after the source stamped it
        row("carry_feature_batch", 2, 2_000),
        rejected(
            "carry_feature_batch",
            2,
            2_300,
            "CARRY signal config does not bind this reducer",
        ),
        // consumed 1000 ms later, 500 ms after its own validity ran out
        gate_row(3, 3_000, 3_500),
        consumed(GATE_KIND, 3, 4_000),
        // never acknowledged
        row("long_feature_batch", 4, 5_000),
        // not an order opportunity
        row("funding_update", 5, 6_000),
        // allowed and on the wire 2.5 ms after the decision
        intent_record(1_000_000),
        allow("eng-1"),
        sent("eng-1", 1_000_000, 3_500_000),
        // denied by the kernel
        intent_record(2_000_000),
        deny(DenyReason::StaleQuote {
            age_ns: 9_000_000_000,
            max_age_ns: 2_000_000_000,
        }),
        // journaled to the wire 1.0 ms after the decision, then never sent
        intent_record(3_000_000),
        allow("eng-2"),
        sent("eng-2", 3_000_000, 4_000_000),
        reject_update(
            "eng-2",
            "never sent: authority: expired after 11500 ms in the venue queue",
        ),
        // allowed and nothing else
        intent_record(4_000_000),
        allow("eng-3"),
    ]
}

#[test]
fn every_opportunity_lands_in_one_bucket_and_the_totals_add_up() {
    let cohort = of_log(&mixed_log());

    assert_eq!(cohort.source_lane.admitted, 1);
    assert_eq!(cohort.source_lane.rejected, 1);
    assert_eq!(cohort.source_lane.expired, 1);
    assert_eq!(cohort.source_lane.unresolved, 1);
    assert_eq!(
        cohort.source_lane.rejected_by_reason,
        BTreeMap::from([(
            "CARRY signal config does not bind this reducer".to_string(),
            1
        )])
    );
    assert_eq!(
        cohort.source_lane.expired_by_reason,
        BTreeMap::from([(
            "llm_gate_candidates: validity passed before the engine consumed it".to_string(),
            1
        )])
    );

    assert_eq!(cohort.order_lane.admitted, 1);
    assert_eq!(cohort.order_lane.rejected, 1);
    assert_eq!(cohort.order_lane.expired, 1);
    assert_eq!(cohort.order_lane.unresolved, 1);
    assert_eq!(
        cohort.order_lane.rejected_by_reason,
        BTreeMap::from([("StaleQuote".to_string(), 1)])
    );
    assert_eq!(
        cohort.order_lane.expired_by_reason,
        BTreeMap::from([(
            "authority: expired in the venue queue (dispatch TTL)".to_string(),
            1
        )])
    );

    assert_eq!(
        cohort.not_an_opportunity.get("signal:funding_update"),
        Some(&1)
    );
    assert_eq!(cohort.not_an_opportunity.get("boot"), Some(&1));
    assert_eq!(cohort.not_an_opportunity.get("verdict"), Some(&4));
    assert_eq!(cohort.not_an_opportunity.get("order_sent_v2"), Some(&2));
    assert_eq!(cohort.not_an_opportunity.get("intent"), None);

    assert_eq!(cohort.records, 20);
    assert_eq!(cohort.source_row_records, 4);
    assert_eq!(cohort.source_rows, 4);
    assert_eq!(cohort.intent_records, 4);
    assert_eq!(cohort.restated_only_rows, 0);
    assert_eq!(cohort.unattached_verdicts, 0);
    assert_eq!(cohort.accounted(), cohort.records);
    assert!(cohort.balanced());

    let text = cohort.table();
    assert!(
        text.contains("every record in this log is on exactly one line above."),
        "{text}"
    );
    assert!(text.contains("20  records read"), "{text}");
    assert!(
        text.contains("replaceable_outputs_coalesced"),
        "the footer must name the worker's coalescing counter: {text}"
    );
    assert!(
        text.contains("universe.listed_on"),
        "the footer must name where unlisted names are dropped: {text}"
    );
}

#[test]
fn the_ages_are_the_stamps_the_log_actually_has() {
    let cohort = of_log(&mixed_log());
    let [source, join, wire] = &cohort.ages[..] else {
        panic!("three intervals");
    };

    // 100, 300 and 1000 ms, nearest-rank over three samples.
    assert_eq!(source.name, "source stamp to engine consume");
    assert_eq!(source.count, 3);
    assert_eq!(source.p50_ms, Some(300.0));
    assert_eq!(source.p90_ms, Some(1_000.0));
    assert_eq!(source.p99_ms, Some(1_000.0));
    assert_eq!(source.p999_ms, Some(1_000.0));
    assert_eq!(source.max_ms, Some(1_000.0));
    assert_eq!(source.stamped_backwards, 0);
    assert!(source.unmeasurable.is_none());

    // The same three samples again, each under the kind of row it came from.
    let by_kind = &cohort.source_ages_by_kind;
    assert_eq!(
        by_kind.values().map(|age| age.count).sum::<usize>(),
        source.count,
        "every source age belongs to exactly one observation kind"
    );
    assert!(by_kind.contains_key("long_feature_batch"));
    assert!(by_kind
        .values()
        .all(|age| age.name == "source stamp to engine consume"));
    let longest = by_kind
        .values()
        .filter_map(|age| age.max_ms)
        .fold(0.0_f64, f64::max);
    assert_eq!(Some(longest), source.max_ms);

    assert_eq!(join.name, "engine consume to decision");
    assert_eq!(join.count, 0);
    assert_eq!(join.p50_ms, None, "an unmeasurable interval is not zero");
    assert!(join
        .unmeasurable
        .expect("the join has no stamps")
        .contains("no id joins a source row to an intent"));

    // 1.0 and 2.5 ms.
    assert_eq!(wire.name, "decision to wire");
    assert_eq!(wire.count, 2);
    assert_eq!(wire.p50_ms, Some(1.0));
    assert_eq!(wire.p90_ms, Some(2.5));
    assert_eq!(wire.max_ms, Some(2.5));
    assert_eq!(wire.without_a_stamp, 0);

    let text = cohort.table();
    assert!(text.contains("UNMEASURABLE:"), "{text}");
    assert!(
        text.contains("two processes' realtime clocks"),
        "the source interval must say it crosses processes: {text}"
    );
}

#[test]
fn a_source_stamp_after_the_engines_own_is_reported_and_not_read_as_zero() {
    let records = vec![
        row("long_feature_batch", 1, 5_000),
        consumed("long_feature_batch", 1, 4_000),
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.ages[0].count, 0);
    assert_eq!(cohort.ages[0].stamped_backwards, 1);
    assert_eq!(cohort.ages[0].p50_ms, None);
    assert_eq!(cohort.source_lane.admitted, 1);
    assert!(cohort
        .table()
        .contains("stamped backwards, left out rather than read as zero"));
}

#[test]
fn an_order_sent_with_no_dispatch_has_no_decision_stamp() {
    let records = vec![
        intent_record(1_000_000),
        allow("eng-1"),
        WalRecord::OrderSent {
            dispatch: None,
            request: request("eng-1"),
            wire_ns: 9_000_000,
            arrival_mid: 100.0,
        },
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.ages[2].count, 0);
    assert_eq!(cohort.ages[2].without_a_stamp, 1);
    assert_eq!(cohort.ages[2].p50_ms, None);
    assert_eq!(cohort.order_lane.admitted, 1);
}

#[test]
fn an_amend_reverdict_does_not_take_an_intents_place() {
    let records = vec![
        intent_record(1_000_000),
        allow("eng-1"),
        sent("eng-1", 1_000_000, 2_000_000),
        // Re-judged under the id the working order already carries.
        allow("eng-1"),
        WalRecord::Verdict {
            client_order_id: Some("eng-1".into()),
            verdict: RiskVerdict::Deny {
                reason: DenyReason::MissingStop,
            },
        },
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.order_lane.admitted, 1);
    assert_eq!(cohort.order_lane.rejected, 0);
    assert_eq!(cohort.order_lane.unresolved, 0);
    assert_eq!(cohort.unattached_verdicts, 0);
    assert_eq!(cohort.intent_records, 1);
}

#[test]
fn a_refusal_the_engine_wrote_only_as_a_note_is_not_read_as_unresolved() {
    let records = vec![
        intent_record(1_000_000),
        note("intent long_native_entry refused: same-symbol sibling batch asks for conflicting leverage values"),
        intent_record(2_000_000),
        allow("eng-9"),
        note("eng-9 not sent (long_native_entry): the intent carries a stop and this venue keeps none (and 3 more like it)"),
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.order_lane.rejected, 2);
    assert_eq!(cohort.order_lane.unresolved, 0);
    assert_eq!(cohort.order_lane.admitted, 0);
    assert_eq!(
        cohort.order_lane.rejected_by_reason,
        BTreeMap::from([
            (
                "same-symbol sibling batch asks for conflicting leverage values".to_string(),
                1
            ),
            (
                "the intent carries a stop and this venue keeps none".to_string(),
                1
            ),
        ])
    );
}

#[test]
fn a_row_only_a_segment_restatement_names_is_in_the_lane_and_outside_the_record_count() {
    let held = observation("long_feature_batch", 7, 8_000, b"{}");
    let cohort = of_log(&[segment_base(std::slice::from_ref(&held))]);
    assert_eq!(cohort.records, 1);
    assert_eq!(cohort.source_row_records, 0);
    assert_eq!(cohort.source_rows, 1);
    assert_eq!(cohort.restated_only_rows, 1);
    assert_eq!(cohort.source_lane.unresolved, 1);
    assert_eq!(cohort.not_an_opportunity.get("segment_base_v7"), Some(&1));
    assert!(cohort.balanced());
    assert!(cohort
        .table()
        .contains("named only by a segment restatement"));
}

#[test]
fn a_row_a_restatement_and_its_own_record_both_name_is_counted_once() {
    let held = observation("long_feature_batch", 7, 8_000, b"{}");
    let records = vec![
        WalRecord::SignalObservation {
            wall_ts_ms: 8_000,
            observation: held.clone(),
        },
        segment_base(std::slice::from_ref(&held)),
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.source_rows, 1);
    assert_eq!(cohort.restated_only_rows, 0);
    assert_eq!(cohort.source_lane.total(), 1);
    assert!(cohort.balanced());
}

#[test]
fn a_gate_row_still_held_past_its_validity_expires_rather_than_staying_open() {
    let records = vec![gate_row(1, 1_000, 1_500), boot_at(9_000)];
    let cohort = of_log(&records);
    assert_eq!(cohort.source_lane.expired, 1);
    assert_eq!(cohort.source_lane.unresolved, 0);
    assert_eq!(
        cohort.source_lane.expired_by_reason,
        BTreeMap::from([(
            "llm_gate_candidates: validity passed with no decision in this log".to_string(),
            1
        )])
    );
    // A feature batch declares no validity, so the same shape stays open.
    let cohort = of_log(&[row("long_feature_batch", 1, 1_000), boot_at(9_000)]);
    assert_eq!(cohort.source_lane.expired, 0);
    assert_eq!(cohort.source_lane.unresolved, 1);
}

/// Order ids are unique per boot, so the same id in a later boot is a second
/// order rather than a re-verdict on the first.
#[test]
fn one_id_reused_after_a_boot_is_two_orders() {
    let records = vec![
        boot_at(1),
        intent_record(1_000_000),
        allow("eng-1"),
        sent("eng-1", 1_000_000, 2_000_000),
        boot_at(2),
        intent_record(1_000_000),
        allow("eng-1"),
        sent("eng-1", 1_000_000, 2_000_000),
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.intent_records, 2);
    assert_eq!(cohort.order_lane.admitted, 2);
    assert_eq!(cohort.order_lane.unresolved, 0);
    assert_eq!(cohort.unattached_verdicts, 0);
    assert!(cohort.balanced());
}

#[test]
fn the_json_report_names_every_bucket_and_every_interval() {
    let value = serde_json::to_value(of_log(&mixed_log())).expect("json report");
    assert_eq!(value["records"], 20);
    assert_eq!(value["source_row_records"], 4);
    assert_eq!(value["intent_records"], 4);
    assert_eq!(value["restated_only_rows"], 0);
    assert_eq!(value["unattached_verdicts"], 0);
    for lane in ["source_lane", "order_lane"] {
        for bucket in ["admitted", "rejected", "expired", "unresolved"] {
            assert_eq!(value[lane][bucket], 1, "{lane}.{bucket}");
        }
        assert!(value[lane]["rejected_by_reason"].is_object(), "{lane}");
        assert!(value[lane]["expired_by_reason"].is_object(), "{lane}");
    }
    assert_eq!(value["order_lane"]["rejected_by_reason"]["StaleQuote"], 1);
    assert_eq!(value["not_an_opportunity"]["signal:funding_update"], 1);

    let ages = value["ages"].as_array().expect("three intervals");
    assert_eq!(ages.len(), 3);
    assert_eq!(ages[0]["name"], "source stamp to engine consume");
    assert_eq!(ages[0]["count"], 3);
    assert_eq!(ages[0]["p50_ms"], 300.0);
    assert_eq!(
        ages[0]["from_stamp"],
        "signal_observation.observation.observed_wall_ts_ms"
    );
    assert!(ages[0]["clocks"].is_string());
    assert_eq!(ages[1]["name"], "engine consume to decision");
    assert!(ages[1]["unmeasurable"].is_string());
    assert_eq!(ages[1]["p50_ms"], serde_json::Value::Null);
    assert_eq!(ages[2]["name"], "decision to wire");
    assert_eq!(ages[2]["p50_ms"], 1.0);
    assert_eq!(ages[2]["unmeasurable"], serde_json::Value::Null);
}
