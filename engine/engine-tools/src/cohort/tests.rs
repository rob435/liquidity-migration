use super::*;

use engine_types::order_dispatch::QueuedOrderDispatch;
use engine_types::{
    Cause, DecisionCause, DenyReason, Intent, OrderKind, OrderRequest, OrderUpdate, RiskVerdict,
    Side, SignalObservation, StrategyId, SymbolId, SIGNAL_OBSERVATION_SCHEMA_VERSION,
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
        cause: None,
    }
}

/// An intent the log says came out of one source row's callback.
fn caused_intent(decided_ns: u64, sequence: u64, callback_wall_ms: i64) -> WalRecord {
    WalRecord::Intent {
        intent: intent(decided_ns),
        cause: Some(Box::new(DecisionCause {
            callback_wall_ms,
            callback_id: Some(sequence),
            causes: vec![Cause::Signal {
                source: "worker.long".into(),
                sequence,
                observation_id: format!("long_feature_batch-{sequence}"),
            }],
        })),
    }
}

/// An intent woken by a timer: a real cause that names no source row.
fn timer_intent(decided_ns: u64, callback_wall_ms: i64) -> WalRecord {
    WalRecord::Intent {
        intent: intent(decided_ns),
        cause: Some(Box::new(DecisionCause {
            callback_wall_ms,
            callback_id: Some(1),
            causes: vec![Cause::Timer {
                id: engine_types::TimerId(4),
            }],
        })),
    }
}

fn refused(code: &str, client_order_id: Option<&str>) -> WalRecord {
    WalRecord::IntentRefused {
        wall_ts_ms: 1_000,
        strategy: DESTINATION,
        symbol: SymbolId(0),
        tag: "long_native_entry".into(),
        client_order_id: client_order_id.map(str::to_owned),
        code: code.into(),
        detail: String::new(),
    }
}

fn fill(id: &str) -> WalRecord {
    WalRecord::OrderUpdate {
        callbacks: None,
        update: OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: format!("exec-{id}"),
            client_order_id: id.into(),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            px: 100.0,
            fee: None,
            is_maker: false,
            forced_close: None,
            venue_ts_ms: 1,
            recv_ns: 1,
        },
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
        BTreeMap::from([("stale_quote".to_string(), 1)])
    );
    assert_eq!(
        cohort.order_lane.expired_by_reason,
        BTreeMap::from([(
            "authority: expired in the venue queue (dispatch TTL)".to_string(),
            1
        )])
    );

    assert_eq!(cohort.other_records.get("signal:funding_update"), Some(&1));
    assert_eq!(cohort.other_records.get("boot"), Some(&1));
    assert_eq!(cohort.other_records.get("verdict"), Some(&4));
    assert_eq!(cohort.other_records.get("order_sent_v2"), Some(&2));
    assert_eq!(cohort.other_records.get("intent"), None);

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
    let [source, join, source_to_decision, wire] = &cohort.ages[..] else {
        panic!("four intervals");
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

    // No intent in the mixed log carries a cause, so both engine-side
    // intervals are absent rather than zero.
    assert_eq!(join.name, "engine consume to decision");
    assert_eq!(join.count, 0);
    assert_eq!(join.p50_ms, None, "an absent interval is not zero");
    assert_eq!(join.without_a_stamp, 4);
    assert_eq!(source_to_decision.name, "source stamp to decision");
    assert_eq!(source_to_decision.count, 0);
    assert_eq!(source_to_decision.without_a_stamp, 4);

    // 1.0 and 2.5 ms.
    assert_eq!(wire.name, "decision to wire");
    assert_eq!(wire.count, 2);
    assert_eq!(wire.p50_ms, Some(1.0));
    assert_eq!(wire.p90_ms, Some(2.5));
    assert_eq!(wire.max_ms, Some(2.5));
    assert_eq!(wire.without_a_stamp, 0);

    let text = cohort.table();
    assert!(
        text.contains("two processes' realtime clocks"),
        "the source interval must say it crosses processes: {text}"
    );
    assert!(
        text.contains("4 unit(s) carry no such stamp at all"),
        "an absent stamp is a named count, not a zero quantile: {text}"
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
    assert_eq!(cohort.ages[3].name, "decision to wire");
    assert_eq!(cohort.ages[3].count, 0);
    assert_eq!(cohort.ages[3].without_a_stamp, 1);
    assert_eq!(cohort.ages[3].p50_ms, None);
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

/// A log written before `intent_refused` existed. Its notes are suppressed
/// for 60 s per (strategy, symbol, tag), so counting them would undercount the
/// population: the refusals stay unresolved and the footer says why.
#[test]
fn a_log_whose_refusals_are_only_notes_counts_them_unresolved_and_says_so() {
    let records = vec![
        intent_record(1_000_000),
        note("intent long_native_entry refused: same-symbol sibling batch asks for conflicting leverage values"),
        intent_record(2_000_000),
        allow("eng-9"),
        note("eng-9 not sent (long_native_entry): the intent carries a stop and this venue keeps none (and 3 more like it)"),
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.order_lane.rejected, 0);
    assert_eq!(cohort.order_lane.unresolved, 2);
    assert_eq!(cohort.order_lane.admitted, 0);
    assert!(cohort.refusals_predate_typed_records);
    assert!(cohort.balanced());
    assert!(
        cohort
            .table()
            .contains("This log predates typed refusal records"),
        "{}",
        cohort.table()
    );
}

/// Both refusal classes, typed. Neither is read from a note, and one typed
/// record in the log is enough to stop calling the log old.
#[test]
fn a_typed_refusal_closes_the_intent_before_the_verdict_and_the_order_after_it() {
    let records = vec![
        intent_record(1_000_000),
        note("intent long_native_entry refused: same-symbol sibling batch asks for conflicting leverage values"),
        refused("batch_leverage_conflict", None),
        intent_record(2_000_000),
        allow("eng-9"),
        note("eng-9 not sent (long_native_entry): the intent carries a stop and this venue keeps none"),
        refused("venue_keeps_no_stop", Some("eng-9")),
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.order_lane.rejected, 2);
    assert_eq!(cohort.order_lane.unresolved, 0);
    assert_eq!(cohort.order_lane.admitted, 0);
    assert_eq!(cohort.unattached_refusals, 0);
    assert!(!cohort.refusals_predate_typed_records);
    assert_eq!(
        cohort.order_lane.rejected_by_reason,
        BTreeMap::from([
            ("batch_leverage_conflict".to_string(), 1),
            ("venue_keeps_no_stop".to_string(), 1),
        ])
    );
    assert!(cohort.balanced());
}

/// The suppression is on the note, not on the record: the population is the
/// records.
#[test]
fn a_hundred_refusals_inside_one_suppression_window_all_count() {
    let mut records = vec![row("long_feature_batch", 1, 1_000)];
    for step in 0..100u64 {
        records.push(caused_intent(1_000_000 + step, 1, 2_000 + step as i64));
        if step == 0 {
            records.push(note(
                "intent long_native_entry refused: below the venue's smallest order value",
            ));
        }
        records.push(refused("below_minimum_notional", None));
    }
    let cohort = of_log(&records);
    assert_eq!(cohort.order_lane.rejected, 100);
    assert_eq!(cohort.order_lane.unresolved, 0);
    assert_eq!(
        cohort.order_lane.rejected_by_reason,
        BTreeMap::from([("below_minimum_notional".to_string(), 100)])
    );
    assert_eq!(
        cohort.other_records.get("note"),
        Some(&1),
        "one note for a hundred refusals is the suppression working"
    );
    assert_eq!(cohort.funnel.rows, 1);
    assert_eq!(cohort.funnel.intents, 100, "one row, a hundred decisions");
    assert_eq!(cohort.funnel.allowed, 0);
    assert!(cohort.balanced());
}

/// The whole chain, joined by the id the intent's own cause carries.
#[test]
fn a_source_row_is_followed_to_its_order_and_its_fill() {
    let records = vec![
        row("long_feature_batch", 1, 1_000),
        consumed("long_feature_batch", 1, 1_100),
        caused_intent(1_000_000, 1, 1_250),
        allow("eng-1"),
        sent("eng-1", 1_000_000, 3_500_000),
        fill("eng-1"),
    ];
    let cohort = of_log(&records);
    assert_eq!(
        cohort.funnel,
        Funnel {
            rows: 1,
            intents: 1,
            allowed: 1,
            wire: 1,
            filled: 1,
        }
    );
    assert_eq!(
        cohort.funnel_by_source.get("worker.long"),
        Some(&cohort.funnel)
    );
    assert_eq!(
        cohort.intents_by_cause,
        BTreeMap::from([("signal".to_string(), 1)])
    );

    // 150 ms from the source stamp to the decision, of which 50 ms is after
    // the engine consumed the row.
    let by_name = |name: &str| {
        cohort
            .ages
            .iter()
            .find(|age| age.name == name)
            .expect(name)
            .clone()
    };
    let consume = by_name("engine consume to decision");
    assert_eq!(consume.count, 1);
    assert_eq!(consume.p50_ms, Some(150.0));
    assert_eq!(consume.without_a_stamp, 0);
    assert_eq!(consume.clocks, "one process's realtime clock, read twice");
    let source = by_name("source stamp to decision");
    assert_eq!(source.count, 1);
    assert_eq!(source.p50_ms, Some(250.0));
    assert!(source.clocks.contains("two processes' realtime clocks"));

    let text = cohort.table();
    assert!(text.contains("rows to fills"), "{text}");
    assert!(text.contains("all sources"), "{text}");
}

/// A timer wake is a cause, and it names no source row. It belongs in the
/// census under `timer`, never charged to whichever row came before it.
#[test]
fn a_decision_no_source_row_caused_is_named_and_not_charged_to_one() {
    let records = vec![
        row("long_feature_batch", 1, 1_000),
        consumed("long_feature_batch", 1, 1_100),
        timer_intent(1_000_000, 1_250),
        allow("eng-1"),
        sent("eng-1", 1_000_000, 2_000_000),
        intent_record(2_000_000),
        allow("eng-2"),
        sent("eng-2", 2_000_000, 3_000_000),
    ];
    let cohort = of_log(&records);
    assert_eq!(cohort.funnel.rows, 1);
    assert_eq!(cohort.funnel.intents, 0);
    assert_eq!(
        cohort.intents_by_cause,
        BTreeMap::from([("timer".to_string(), 1), ("none".to_string(), 1)])
    );
    for name in ["engine consume to decision", "source stamp to decision"] {
        let age = cohort.ages.iter().find(|age| age.name == name).expect(name);
        assert_eq!(age.count, 0, "{name}");
        assert_eq!(age.without_a_stamp, 2, "{name}");
    }
    assert!(cohort.balanced());
}

/// A refusal for an intent this reader is not holding is reported, not folded
/// into a lane that would then be wrong.
#[test]
fn a_refusal_naming_no_held_intent_is_reported() {
    let records = vec![refused("engine_latched", None)];
    let cohort = of_log(&records);
    assert_eq!(cohort.unattached_refusals, 1);
    assert_eq!(cohort.order_lane.total(), 0);
    assert!(cohort.balanced());
    assert!(cohort
        .table()
        .contains("named neither an allowed order nor the intent"));
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
    assert_eq!(cohort.other_records.get("segment_base_v7"), Some(&1));
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
    assert_eq!(value["other_records"]["signal:funding_update"], 1);
    assert_eq!(value["order_lane"]["rejected_by_reason"]["stale_quote"], 1);
    assert_eq!(value["intents_by_cause"]["none"], 4);
    assert_eq!(value["refusals_predate_typed_records"], false);
    for field in ["rows", "intents", "allowed", "wire", "filled"] {
        assert!(value["funnel"][field].is_u64(), "funnel.{field}");
    }

    let ages = value["ages"].as_array().expect("four intervals");
    assert_eq!(ages.len(), 4);
    assert_eq!(ages[0]["name"], "source stamp to engine consume");
    assert_eq!(ages[0]["count"], 3);
    assert_eq!(ages[0]["p50_ms"], 300.0);
    assert_eq!(
        ages[0]["from_stamp"],
        "signal_observation.observation.observed_wall_ts_ms"
    );
    assert!(ages[0]["clocks"].is_string());
    assert_eq!(ages[1]["name"], "engine consume to decision");
    assert_eq!(ages[1]["to_stamp"], "intent.cause.callback_wall_ms");
    assert_eq!(ages[1]["p50_ms"], serde_json::Value::Null);
    assert_eq!(ages[1]["without_a_stamp"], 4);
    assert_eq!(ages[2]["name"], "source stamp to decision");
    assert_eq!(ages[3]["name"], "decision to wire");
    assert_eq!(ages[3]["p50_ms"], 1.0);
}
