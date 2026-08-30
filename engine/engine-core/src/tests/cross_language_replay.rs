//! One stable fence across the Python decision and Rust execution seam.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use engine_risk::{EnvelopeConfig, Kernel, KernelConfig};
use engine_strategies::target_book::plan::plan as plan_targets;
use engine_strategies::target_book::{
    Held, PlanRules, Skipped, Step as PlannerStep, SymbolFacts, Target,
};
use engine_strategies::TargetBookFollower;
use engine_types::BookTarget;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::*;

const FIXTURE_TEXT: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/fixtures/long_cross_language_replay_v1.json"
));

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReplayFixture {
    schema_version: u64,
    name: String,
    python_decision_cases: Vec<PythonDecisionCase>,
    strategy_event_tape: RecordedStrategyEventTape,
    strategy_config: Value,
    expected_decision_output: Value,
    expected_live_state: Value,
    target_book_utf8: String,
    target_book_sha256: String,
    rust_replay: RustReplay,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RecordedStrategyEventTape {
    event_count: usize,
    final_tape_hash: String,
    sha256: String,
    utf8: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PythonDecisionCase {
    name: String,
    expected_decision_output: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedEffectiveConfig {
    profile_name: String,
    layers: Vec<RecordedConfigLayer>,
    operational_profile: RecordedOperationalProfile,
    resolved_config_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedConfigLayer {
    source: String,
    detail: String,
    values: RecordedConfigValues,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedConfigValues {
    notional_multiplier: f64,
    entry_leverage: f64,
    order_notional_pct_equity: f64,
    max_new_entries_per_cycle: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedOperationalProfile {
    path: String,
    sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RustReplay {
    expected_planner_events: Vec<Value>,
    expected_risk_events: Vec<Value>,
    expected_wal_events: Vec<Value>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedTapeRow {
    schema_version: u64,
    prior_tape_hash: String,
    tape_hash: String,
    event: RecordedStrategyEvent,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedStrategyEvent {
    event_id: String,
    event_ts_ns: u64,
    ingest_ts_ns: u64,
    source: String,
    source_sequence: u64,
    kind: String,
    payload: RecordedEventPayload,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedEventPayload {
    execution_environment: String,
    strategy_profile: String,
    replay_envelope: ReplayEnvelope,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ReplayEnvelope {
    schema_version: u64,
    case_name: String,
    decision_input: RecordedDecisionInput,
    prior_state: RecordedPriorState,
    effective_config: RecordedEffectiveConfig,
    quote: RecordedQuote,
    instrument_rule: RecordedRule,
    account: RecordedAccount,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedDecisionInput {
    schema_version: u64,
    decision_ts_ms: i64,
    symbol: String,
    signal_ts_ms: i64,
    equity_usdt: f64,
    market_price: f64,
    signal_close: f64,
    observed_low: Option<f64>,
    feature_row: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedPriorState {
    requested: bool,
    filled: bool,
    entry_ts_ms: i64,
    entry_price: f64,
    target_notional_usdt: f64,
    stop_loss_fraction: f64,
    decayed_stop_loss_fraction: f64,
    stop_decay_after_ms: i64,
    max_hold_deadline_ts_ms: i64,
    entry_valid_until_ms: i64,
    cooldown_until_ms: i64,
    attempted_signal_ts_ms: i64,
    active_positions: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedQuote {
    symbol: String,
    bid_px: f64,
    bid_qty: f64,
    ask_px: f64,
    ask_qty: f64,
    venue_ts_ms: i64,
    seq: u64,
}

#[derive(Copy, Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedRule {
    tick_size: f64,
    qty_step: f64,
    min_qty: f64,
    min_notional: f64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedAccount {
    equity_usdt: f64,
    available_usdt: f64,
    observed_ts_ns: u64,
    positions: Vec<RecordedPosition>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedPosition {
    symbol: String,
    side: String,
    qty: f64,
    px: f64,
    entry_px: f64,
    stop_attached: bool,
    stop_px: f64,
    leverage: f64,
}

impl RecordedRule {
    fn engine(self) -> InstrumentRule {
        InstrumentRule {
            tick_size: self.tick_size,
            qty_step: self.qty_step,
            min_qty: self.min_qty,
            min_notional: self.min_notional,
        }
    }
}

struct RecordedFacts {
    prices: BTreeMap<String, f64>,
    rules: BTreeMap<String, InstrumentRule>,
    held: BTreeMap<String, Held>,
}

impl RecordedFacts {
    fn from_envelope(replay: &ReplayEnvelope) -> Self {
        let mut prices = BTreeMap::new();
        prices.insert(
            replay.quote.symbol.clone(),
            (replay.quote.bid_px + replay.quote.ask_px) / 2.0,
        );
        let mut rules = BTreeMap::new();
        rules.insert(replay.quote.symbol.clone(), replay.instrument_rule.engine());
        let held = replay
            .account
            .positions
            .iter()
            .map(|position| {
                (
                    position.symbol.clone(),
                    Held {
                        side: parse_side(&position.side),
                        qty: position.qty,
                        px: position.px,
                        entry_px: position.entry_px,
                        stop_px: position.stop_px,
                    },
                )
            })
            .collect();
        RecordedFacts {
            prices,
            rules,
            held,
        }
    }
}

impl SymbolFacts for RecordedFacts {
    fn held(&self, symbol: &str) -> Option<Held> {
        self.held.get(symbol).copied()
    }

    fn price(&self, symbol: &str) -> Option<f64> {
        self.prices.get(symbol).copied()
    }

    fn rule(&self, symbol: &str) -> Option<InstrumentRule> {
        self.rules.get(symbol).copied()
    }
}

fn fixture() -> ReplayFixture {
    serde_json::from_str(FIXTURE_TEXT).expect("cross-language replay fixture is valid JSON")
}

fn canonical_json<T: Serialize>(value: &T) -> Vec<u8> {
    let sorted = serde_json::to_value(value).expect("recorded value is valid JSON");
    serde_json::to_vec(&sorted).expect("canonical recorded JSON serializes")
}

fn event_phase(kind: &str) -> u8 {
    match kind {
        "confirmed_bar" => 10,
        "timer" => 20,
        other => panic!("unknown strategy-event phase in replay fixture: {other}"),
    }
}

fn recorded_strategy_events(tape: &RecordedStrategyEventTape) -> Vec<RecordedStrategyEvent> {
    let bytes = tape.utf8.as_bytes();
    assert!(bytes.ends_with(b"\n"), "the recorded tape has a torn tail");
    assert_eq!(
        hex::encode(Sha256::digest(bytes)),
        tape.sha256,
        "the recorded strategy-event tape bytes drifted"
    );
    let rows: Vec<RecordedTapeRow> = tape
        .utf8
        .lines()
        .map(|line| serde_json::from_str(line).expect("strategy-event tape row is valid JSON"))
        .collect();
    assert_eq!(rows.len(), tape.event_count);
    assert_eq!(rows.len(), 5, "the tape must carry all five decisions");

    let mut prior_hash = hex::encode(Sha256::digest(
        b"liquidity-migration-strategy-event-tape-v1",
    ));
    let mut previous_order_key: Option<(u64, u8, String, u64, String)> = None;
    let mut event_ids = BTreeSet::new();
    let mut events = Vec::with_capacity(rows.len());
    for row in rows {
        assert_eq!(row.schema_version, 1);
        assert_eq!(row.prior_tape_hash, prior_hash, "the tape chain broke");
        assert!(row.event.event_ts_ns > 0);
        assert!(row.event.ingest_ts_ns >= row.event.event_ts_ns);

        let id_material = json!({
            "event_ts_ns": row.event.event_ts_ns,
            "kind": &row.event.kind,
            "payload": &row.event.payload,
            "source": &row.event.source,
            "source_sequence": row.event.source_sequence,
        });
        let expected_event_id = format!(
            "strategy-event-{}",
            hex::encode(Sha256::digest(canonical_json(&id_material)))
        );
        assert_eq!(
            row.event.event_id, expected_event_id,
            "the event id does not name its canonical contents"
        );
        assert!(
            event_ids.insert(row.event.event_id.clone()),
            "the tape repeats a strategy event"
        );

        let order_key = (
            row.event.event_ts_ns,
            event_phase(&row.event.kind),
            row.event.source.clone(),
            row.event.source_sequence,
            row.event.event_id.clone(),
        );
        if let Some(previous) = &previous_order_key {
            assert!(
                order_key >= *previous,
                "the strategy-event tape moves backward"
            );
        }
        previous_order_key = Some(order_key);

        let mut digest = Sha256::new();
        digest.update(prior_hash.as_bytes());
        digest.update(canonical_json(&json!({"event": &row.event})));
        let expected_tape_hash = hex::encode(digest.finalize());
        assert_eq!(
            row.tape_hash, expected_tape_hash,
            "a tape row does not name its canonical event"
        );
        prior_hash = expected_tape_hash;
        events.push(row.event);
    }
    assert_eq!(prior_hash, tape.final_tape_hash);
    events
}

fn parse_side(side: &str) -> Side {
    match side {
        "buy" => Side::Buy,
        "sell" => Side::Sell,
        other => panic!("unknown side in replay fixture: {other}"),
    }
}

fn side_name(side: Side) -> &'static str {
    match side {
        Side::Buy => "buy",
        Side::Sell => "sell",
    }
}

fn target(book_target: &BookTarget) -> Target {
    Target {
        symbol: book_target.symbol.clone(),
        notional_usdt: book_target.notional_usdt,
        stop_loss_fraction: book_target.stop_loss_fraction,
        entry_valid_until_ms: book_target.entry_valid_until_ms,
        target_qty: book_target.target_qty,
    }
}

fn planner_event(step: &PlannerStep) -> Value {
    match step {
        PlannerStep::Enter {
            symbol,
            side,
            qty,
            stop_px,
        } => json!({
            "kind": "enter",
            "symbol": symbol,
            "side": side_name(*side),
            "qty": qty,
            "stop_px": stop_px,
        }),
        PlannerStep::Exit { symbol, side, qty } => json!({
            "kind": "exit",
            "symbol": symbol,
            "side": side_name(*side),
            "qty": qty,
        }),
        PlannerStep::Resize {
            symbol,
            side,
            qty,
            reduce_only,
            stop_px,
        } => json!({
            "kind": "resize",
            "symbol": symbol,
            "side": side_name(*side),
            "qty": qty,
            "reduce_only": reduce_only,
            "stop_px": stop_px,
        }),
        PlannerStep::Restop { symbol, stop_px } => json!({
            "kind": "restop",
            "symbol": symbol,
            "stop_px": stop_px,
        }),
    }
}

fn skipped_event(skip: &Skipped) -> Value {
    match skip {
        Skipped::TooSmallToBother { symbol, delta_usdt } => json!({
            "kind": "skipped_too_small",
            "symbol": symbol,
            "delta_usdt": delta_usdt,
        }),
        Skipped::BelowEntryFloor {
            symbol,
            notional_usdt,
        } => json!({
            "kind": "skipped_below_entry_floor",
            "symbol": symbol,
            "notional_usdt": notional_usdt,
        }),
        Skipped::BelowVenueMinimum { symbol } => json!({
            "kind": "skipped_below_venue_minimum",
            "symbol": symbol,
        }),
        Skipped::EntryWindowClosed { symbol } => json!({
            "kind": "skipped_entry_window_closed",
            "symbol": symbol,
        }),
        Skipped::NoPrice { symbol } => json!({
            "kind": "skipped_no_price",
            "symbol": symbol,
        }),
        Skipped::NoInstrumentRule { symbol } => json!({
            "kind": "skipped_no_instrument_rule",
            "symbol": symbol,
        }),
    }
}

fn order_kind(kind: &OrderKind) -> Value {
    match kind {
        OrderKind::Market => json!({"kind": "market"}),
        OrderKind::Limit { px, tif } => json!({
            "kind": "limit",
            "px": px,
            "time_in_force": match tif {
                TimeInForce::Gtc => "gtc",
                TimeInForce::Ioc => "ioc",
                TimeInForce::PostOnly => "post_only",
            },
        }),
    }
}

fn stable_intent(intent: &Intent) -> Value {
    json!({
        "strategy_id": intent.strategy.0,
        "symbol_id": intent.symbol.0,
        "side": side_name(intent.side),
        "qty": intent.qty,
        "order_kind": order_kind(&intent.kind),
        "stop_px": intent.stop.map(|stop| stop.trigger_px),
        "reduce_only": intent.reduce_only,
        "tag": intent.tag,
        "work": intent.work,
        "leverage": intent.leverage,
    })
}

fn verdict_value(verdict: &RiskVerdict) -> Value {
    match verdict {
        RiskVerdict::Allow { qty } => json!({"kind": "allow", "qty": qty}),
        RiskVerdict::Deny { reason } => {
            json!({"kind": "deny", "reason": format!("{reason:?}")})
        }
    }
}

fn order_id_alias(client_order_id: Option<&str>, order_id: &str) -> Option<&'static str> {
    client_order_id.map(|id| {
        assert_eq!(id, order_id, "the verdict names a different order");
        "$order_1"
    })
}

fn risk_events(records: &[WalRecord], order_id: &str) -> Vec<Value> {
    records
        .iter()
        .filter_map(|record| match record {
            WalRecord::Intent { intent } => Some(json!({
                "kind": "assess",
                "intent": stable_intent(intent),
            })),
            WalRecord::Verdict {
                client_order_id,
                verdict,
            } => Some(json!({
                "kind": "result",
                "client_order_id": order_id_alias(client_order_id.as_deref(), order_id),
                "verdict": verdict_value(verdict),
            })),
            _ => None,
        })
        .collect()
}

fn wal_events(records: &[WalRecord], order_id: &str) -> Vec<Value> {
    records
        .iter()
        .filter_map(|record| match record {
            WalRecord::Intent { intent } => Some(json!({
                "kind": "intent",
                "intent": stable_intent(intent),
            })),
            WalRecord::Verdict {
                client_order_id,
                verdict,
            } => Some(json!({
                "kind": "verdict",
                "client_order_id": order_id_alias(client_order_id.as_deref(), order_id),
                "verdict": verdict_value(verdict),
            })),
            WalRecord::OrderSent {
                request,
                arrival_mid,
                ..
            } => {
                assert_eq!(
                    request.client_order_id, order_id,
                    "the durable send names a different order"
                );
                Some(json!({
                    "kind": "order_sent",
                    "client_order_id": "$order_1",
                    "strategy_id": request.strategy.0,
                    "symbol_id": request.symbol.0,
                    "side": side_name(request.side),
                    "qty": request.qty,
                    "order_kind": order_kind(&request.kind),
                    "stop_px": request.stop.map(|stop| stop.trigger_px),
                    "reduce_only": request.reduce_only,
                    "close_position": request.close_position,
                    "arrival_mid": arrival_mid,
                }))
            }
            _ => None,
        })
        .collect()
}

fn one_recorded_quote(replay: &ReplayEnvelope) -> ScriptFeed {
    ScriptFeed {
        events: VecDeque::from([MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: Quote {
                bid_px: replay.quote.bid_px,
                bid_qty: replay.quote.bid_qty,
                ask_px: replay.quote.ask_px,
                ask_qty: replay.quote.ask_qty,
                venue_ts_ms: replay.quote.venue_ts_ms,
                recv_ns: clock::now_ns(),
                seq: replay.quote.seq,
            },
        }]),
        close_at_end: false,
        admitted: Rc::new(RefCell::new(Vec::new())),
        known: 1,
        admits_wrongly: false,
    }
}

async fn until_sent(sends: Rc<RefCell<Vec<OrderRequest>>>) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while sends.lock().unwrap().is_empty() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
}

struct ReplayHarness {
    records: Rc<RefCell<Vec<WalRecord>>>,
    sends: Rc<RefCell<Vec<OrderRequest>>>,
}

async fn build_replay_engine(
    settings: &EngineSection,
    follower: TargetBookFollower,
    replay: &ReplayEnvelope,
) -> (Engine<MockWal, Kernel, MockVenue>, ReplayHarness) {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape, &[replay.quote.symbol.as_str()]);
    venue.rules[0].1 = replay.instrument_rule.engine();
    assert_eq!(replay.account.equity_usdt, 10_000.0);
    assert_eq!(replay.account.available_usdt, 9_000.0);
    let positions = replay
        .account
        .positions
        .iter()
        .map(|position| {
            assert_eq!(position.symbol, replay.quote.symbol);
            engine_types::PositionView {
                symbol: SymbolId(0),
                side: parse_side(&position.side),
                qty: position.qty,
                entry_px: position.entry_px,
                stop_attached: position.stop_attached,
                stop_px: position.stop_px,
                leverage: Some(position.leverage),
            }
        })
        .collect();
    venue.account_readings.lock().unwrap().push_back(positions);
    let entry_leverage = replay.effective_config.layers[0].values.entry_leverage;
    let risk = Kernel::new(KernelConfig {
        max_account_view_age_ns: settings.account_view_max_age_ms.saturating_mul(1_000_000),
        envelope: EnvelopeConfig {
            tracks_equity: false,
            reference_usdt: replay.account.equity_usdt,
            equity_fraction: 1.0,
            floor_usdt: 100.0,
            expand_dead_band_fraction: 0.05,
            gross_notional_multiple: entry_leverage,
            disaster_stop_fraction: 0.35,
            max_component_gross_notional_usdt: replay.account.equity_usdt * entry_leverage,
            max_initial_margin_usdt: replay.account.equity_usdt,
        },
        leverage: entry_leverage,
        qty_tolerance: 1e-12,
    })
    .expect("the recorded risk configuration is valid");
    let engine = Engine::boot(
        settings,
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(follower)],
        &[],
    )
    .await
    .expect("boots the replay engine");
    (engine, ReplayHarness { records, sends })
}

#[tokio::test]
async fn one_fixture_fences_python_bytes_planner_risk_and_wal() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.name, "long_mainnet_recorded_replay_v1");
    let strategy_events = recorded_strategy_events(&fixture.strategy_event_tape);
    for (label, value) in [
        ("strategy_config", &fixture.strategy_config),
        (
            "expected_decision_output",
            &fixture.expected_decision_output,
        ),
        ("expected_live_state", &fixture.expected_live_state),
    ] {
        assert!(value.is_object(), "{label} must stay a typed JSON object");
    }
    assert_eq!(
        fixture
            .python_decision_cases
            .iter()
            .map(|case| case.name.as_str())
            .collect::<Vec<_>>(),
        [
            "pending_entry_holds_original_target",
            "base_stop_exits_before_decay",
            "decayed_stop_exits_after_fill_clock",
            "time_exit_wins_at_fill_deadline",
        ]
    );
    for case in &fixture.python_decision_cases {
        assert!(case.expected_decision_output.is_object());
    }

    let mut expected_case_names = vec!["flat_entry"];
    expected_case_names.extend(
        fixture
            .python_decision_cases
            .iter()
            .map(|case| case.name.as_str()),
    );
    assert_eq!(
        strategy_events
            .iter()
            .map(|event| event.payload.replay_envelope.case_name.as_str())
            .collect::<Vec<_>>(),
        expected_case_names
    );

    let first_event = &strategy_events[0];
    let replay = &first_event.payload.replay_envelope;
    let expected_config_hash =
        hex::encode(Sha256::digest(canonical_json(&fixture.strategy_config)));
    for (index, event) in strategy_events.iter().enumerate() {
        let envelope = &event.payload.replay_envelope;
        assert_eq!(event.source, "long-native-mainnet");
        assert_eq!(event.source_sequence, index as u64 + 1);
        assert_eq!(
            event.kind,
            if index == 0 { "confirmed_bar" } else { "timer" }
        );
        assert_eq!(event.payload.execution_environment, "mainnet");
        assert_eq!(envelope.schema_version, 1);
        assert_eq!(envelope.decision_input.schema_version, 1);
        assert_eq!(
            envelope.decision_input.decision_ts_ms,
            i64::try_from(event.event_ts_ns / 1_000_000).expect("recorded event time fits i64")
        );
        assert_eq!(event.event_ts_ns % 1_000_000, 0);
        assert_eq!(
            envelope.quote.venue_ts_ms,
            envelope.decision_input.decision_ts_ms
        );
        assert_eq!(envelope.quote.seq, event.source_sequence);
        assert_eq!(envelope.quote.symbol, envelope.decision_input.symbol);
        assert!(envelope.quote.bid_px < envelope.quote.ask_px);
        assert!(envelope.quote.bid_qty > 0.0);
        assert!(envelope.quote.ask_qty > 0.0);
        assert_eq!(
            (envelope.quote.bid_px + envelope.quote.ask_px) / 2.0,
            envelope.decision_input.market_price
        );
        assert_eq!(
            envelope.account.equity_usdt,
            envelope.decision_input.equity_usdt
        );
        assert!(envelope.account.available_usdt >= 0.0);
        assert!(envelope.account.available_usdt <= envelope.account.equity_usdt);
        assert_eq!(envelope.account.observed_ts_ns, event.event_ts_ns);
        assert_eq!(envelope.decision_input.observed_low, None);
        assert_eq!(envelope.effective_config.profile_name, "v12");
        assert_eq!(event.payload.strategy_profile, "long_native_v12_wide_stop");
        assert_eq!(
            event.payload.strategy_profile,
            fixture.strategy_config["rule"]["execution_strategy_id"]
        );
        assert_eq!(
            envelope.effective_config.resolved_config_sha256,
            expected_config_hash
        );
        assert_eq!(
            canonical_json(&envelope.effective_config),
            canonical_json(&replay.effective_config)
        );
        assert_eq!(
            canonical_json(&envelope.instrument_rule),
            canonical_json(&replay.instrument_rule)
        );

        let prior = &envelope.prior_state;
        assert_eq!(prior.active_positions, 0);
        assert_eq!(prior.filled, !envelope.account.positions.is_empty());
        assert!(envelope.decision_input.signal_ts_ms > 0);
        if prior.attempted_signal_ts_ms > 0 {
            assert_eq!(
                envelope.decision_input.signal_ts_ms,
                prior.attempted_signal_ts_ms
            );
        }
        if prior.filled {
            assert_eq!(envelope.account.positions.len(), 1);
            assert!(prior.requested);
            assert!(prior.entry_ts_ms > 0);
            assert!(prior.entry_valid_until_ms >= prior.entry_ts_ms);
            assert!(prior.max_hold_deadline_ts_ms > prior.entry_ts_ms);
            assert!(prior.stop_decay_after_ms > 0);
            assert!(prior.stop_loss_fraction > prior.decayed_stop_loss_fraction);
            assert!(prior.target_notional_usdt > 0.0);
            assert!(prior.attempted_signal_ts_ms > 0);
            let position = &envelope.account.positions[0];
            assert_eq!(position.symbol, envelope.decision_input.symbol);
            assert_eq!(position.side, "buy");
            assert_eq!(position.entry_px, prior.entry_price);
            assert_eq!(position.px, envelope.decision_input.market_price);
            assert!(position.stop_attached);
            assert!(position.stop_px > 0.0);
            assert_eq!(
                position.leverage,
                envelope.effective_config.layers[0].values.entry_leverage
            );
        } else {
            assert_eq!(prior.entry_price, 0.0);
            assert_eq!(prior.max_hold_deadline_ts_ms, 0);
            assert_eq!(prior.cooldown_until_ms, 0);
            if prior.requested {
                assert!(prior.entry_valid_until_ms > envelope.decision_input.decision_ts_ms);
                assert!(prior.attempted_signal_ts_ms > 0);
                assert!(prior.stop_decay_after_ms > 0);
                assert!(prior.stop_loss_fraction > prior.decayed_stop_loss_fraction);
                assert!(prior.target_notional_usdt > 0.0);
            } else {
                assert_eq!(prior.entry_ts_ms, 0);
                assert_eq!(prior.entry_valid_until_ms, 0);
                assert_eq!(prior.attempted_signal_ts_ms, 0);
                assert_eq!(prior.stop_decay_after_ms, 0);
                assert_eq!(prior.stop_loss_fraction, 0.0);
                assert_eq!(prior.decayed_stop_loss_fraction, 0.0);
                assert_eq!(prior.target_notional_usdt, 0.0);
            }
        }
    }

    assert_eq!(replay.effective_config.profile_name, "v12");
    assert_eq!(replay.effective_config.layers.len(), 1);
    let operational_layer = &replay.effective_config.layers[0];
    assert_eq!(operational_layer.source, "operational_profile");
    assert_eq!(
        operational_layer.detail,
        format!(
            "{}#{}",
            replay.effective_config.operational_profile.path,
            replay.effective_config.operational_profile.sha256
        )
    );
    assert_eq!(operational_layer.values.notional_multiplier, 6.0);
    assert_eq!(operational_layer.values.entry_leverage, 5.0);
    assert_eq!(operational_layer.values.order_notional_pct_equity, 0.0);
    assert_eq!(operational_layer.values.max_new_entries_per_cycle, 5);
    assert_eq!(
        fixture.strategy_config["execution"]["notional_multiplier"],
        json!(6.0)
    );
    assert_eq!(
        fixture.strategy_config["execution"]["entry_leverage"],
        json!(5.0)
    );
    let quote_mid = (replay.quote.bid_px + replay.quote.ask_px) / 2.0;
    assert_eq!(replay.decision_input.market_price, quote_mid);
    assert_eq!(
        replay.decision_input.signal_close,
        replay.decision_input.feature_row["close"]
            .as_f64()
            .expect("entry feature close is numeric")
    );
    assert_eq!(
        replay.decision_input.signal_ts_ms,
        replay.decision_input.feature_row["ts_ms"]
            .as_i64()
            .expect("entry feature timestamp is an integer")
    );
    assert!(
        quote_mid
            <= replay.decision_input.signal_close
                * (1.0
                    - fixture.strategy_config["rule"]["fc_sniper_retrace_pct"]
                        .as_f64()
                        .expect("the expected config pins retrace"))
    );

    let bytes = fixture.target_book_utf8.as_bytes();
    assert_eq!(
        hex::encode(Sha256::digest(bytes)),
        fixture.target_book_sha256,
        "the fixture hash must name its exact handoff bytes"
    );
    let book = crate::targets::parse_book(bytes).expect("Rust accepts Python's exact target bytes");
    assert_eq!(book.source, "long_native_v12_wide_stop");
    assert_eq!(first_event.payload.strategy_profile, book.source);

    let event_now_ms = i64::try_from(first_event.event_ts_ns / 1_000_000)
        .expect("recorded event time fits the Rust planner clock");
    let facts = RecordedFacts::from_envelope(replay);
    let targets: Vec<Target> = book.targets.iter().map(target).collect();
    let held_symbols: Vec<String> = replay
        .account
        .positions
        .iter()
        .map(|position| position.symbol.clone())
        .collect();
    let planned = plan_targets(
        &targets,
        &held_symbols,
        &facts,
        event_now_ms,
        book.valid_until_ms,
        PlanRules::FLEET,
    );
    let mut planner_events: Vec<Value> = planned.steps.iter().map(planner_event).collect();
    planner_events.extend(planned.skipped.iter().map(skipped_event));
    assert_eq!(
        planner_events, fixture.rust_replay.expected_planner_events,
        "the pure Rust target planner drifted"
    );

    let path = temp_path("long-cross-language-replay");
    std::fs::write(&path, bytes).expect("writes the exact fixture bytes");
    let params: toml::Value = toml::from_str(&format!(
        "symbols = [\"{}\"]\nrest_entries = false\n",
        replay.quote.symbol
    ))
    .expect("target follower parameters");
    let follower = TargetBookFollower::from_params(StrategyId(0), &params)
        .expect("builds the target-book follower");
    let mut chosen_settings = settings();
    chosen_settings.leverage_authority = crate::config::LeverageAuthority::Sole;
    let (mut engine, harness) = build_replay_engine(&chosen_settings, follower, replay).await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        crate::targets::TargetBookWatcher::with_poll(
            path.path().to_path_buf(),
            Duration::from_millis(5),
        ),
    )]));

    engine
        .run(
            &mut one_recorded_quote(replay),
            &mut ScriptOrderFeed::empty(),
            until_sent(harness.sends.clone()),
        )
        .await
        .expect("replays the target through the real engine loop");

    let sends = harness.sends.lock().unwrap();
    assert_eq!(sends.len(), 1, "the fixture must produce one exact order");
    let order_id = sends[0].client_order_id.clone();
    let risk_events = risk_events(&harness.records.lock().unwrap(), &order_id);
    assert_eq!(
        risk_events, fixture.rust_replay.expected_risk_events,
        "the real risk kernel's input or verdict drifted"
    );

    let projected_wal = wal_events(&harness.records.lock().unwrap(), &order_id);
    assert_eq!(
        projected_wal, fixture.rust_replay.expected_wal_events,
        "the durable planner/risk/send sequence drifted"
    );
}
