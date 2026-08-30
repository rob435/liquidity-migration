//! CARRY's checked-in Python decision to Rust execution fence.

use std::collections::BTreeMap;

use engine_strategies::target_book::{
    plan::plan, Held, PlanRules, Skipped, Step, SymbolFacts, Target,
};
use engine_types::orders::{InstrumentRule, Side};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FIXTURE_TEXT: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/fixtures/carry_cross_language_replay_v1.json"
));

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u64,
    name: String,
    execution_rules: ExecutionRules,
    target_book_utf8: String,
    target_book_sha256: String,
    rust_replay: RustReplay,
}

#[derive(Debug, Deserialize)]
struct ExecutionRules {
    schema_version: u64,
    entry_floor_usdt: f64,
    resize_floor_usdt: f64,
    resize_floor_fraction: f64,
    engine_entry_cutoff_ms: i64,
    signal_validity_ms: i64,
    book_validity_ms: i64,
    presettlement_window_ms: i64,
}

#[derive(Debug, Deserialize)]
struct TargetBook {
    version: u64,
    source: String,
    decision_ts_ms: i64,
    valid_until_ms: i64,
    targets: Vec<RecordedTarget>,
}

#[derive(Debug, Deserialize)]
struct RecordedTarget {
    symbol: String,
    notional_usdt: f64,
    stop_loss_fraction: f64,
    entry_valid_until_ms: Option<i64>,
    target_qty: Option<f64>,
    leverage: f64,
}

#[derive(Debug, Deserialize)]
struct RustReplay {
    now_ms: i64,
    held_symbols: Vec<String>,
    prices: BTreeMap<String, f64>,
    instrument_rule: RecordedRule,
    held: Vec<RecordedHeld>,
    expected_planner_steps: Vec<Value>,
    expected_planner_skips: Vec<Value>,
    boundary_cases: BoundaryCases,
}

#[derive(Copy, Clone, Debug, Deserialize)]
struct RecordedRule {
    tick_size: f64,
    qty_step: f64,
    min_qty: f64,
    min_notional: f64,
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

#[derive(Debug, Deserialize)]
struct RecordedHeld {
    symbol: String,
    side: String,
    qty: f64,
    px: f64,
    entry_px: f64,
    stop_px: f64,
}

#[derive(Debug, Deserialize)]
struct BoundaryCases {
    entry_below_floor_usdt: f64,
    entry_at_floor_usdt: f64,
    resize_at_absolute_floor_usdt: f64,
    resize_above_absolute_floor_usdt: f64,
    resize_at_fraction_floor_usdt: f64,
    resize_above_fraction_floor_usdt: f64,
}

#[derive(Default)]
struct Facts {
    held: BTreeMap<String, Held>,
    prices: BTreeMap<String, f64>,
    rules: BTreeMap<String, InstrumentRule>,
}

impl SymbolFacts for Facts {
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

fn side(raw: &str) -> Side {
    match raw {
        "buy" => Side::Buy,
        "sell" => Side::Sell,
        other => panic!("unknown fixture side {other}"),
    }
}

fn side_text(value: Side) -> &'static str {
    match value {
        Side::Buy => "buy",
        Side::Sell => "sell",
    }
}

fn step_json(step: &Step) -> Value {
    match step {
        Step::Enter {
            symbol,
            side,
            qty,
            stop_px,
        } => json!({
            "kind": "enter",
            "symbol": symbol,
            "side": side_text(*side),
            "qty": qty,
            "stop_px": stop_px,
        }),
        Step::Exit { symbol, side, qty } => json!({
            "kind": "exit",
            "symbol": symbol,
            "side": side_text(*side),
            "qty": qty,
        }),
        Step::Resize {
            symbol,
            side,
            qty,
            reduce_only,
            stop_px,
        } => json!({
            "kind": "resize",
            "symbol": symbol,
            "side": side_text(*side),
            "qty": qty,
            "reduce_only": reduce_only,
            "stop_px": stop_px,
        }),
        Step::Restop { symbol, stop_px } => json!({
            "kind": "restop",
            "symbol": symbol,
            "stop_px": stop_px,
        }),
    }
}

fn skipped_json(skipped: &Skipped) -> Value {
    match skipped {
        Skipped::TooSmallToBother { symbol, delta_usdt } => json!({
            "kind": "too_small_to_bother",
            "symbol": symbol,
            "delta_usdt": delta_usdt,
        }),
        Skipped::BelowEntryFloor {
            symbol,
            notional_usdt,
        } => json!({
            "kind": "below_entry_floor",
            "symbol": symbol,
            "notional_usdt": notional_usdt,
        }),
        Skipped::BelowVenueMinimum { symbol } => {
            json!({"kind": "below_venue_minimum", "symbol": symbol})
        }
        Skipped::EntryWindowClosed { symbol } => {
            json!({"kind": "entry_window_closed", "symbol": symbol})
        }
        Skipped::NoPrice { symbol } => json!({"kind": "no_price", "symbol": symbol}),
        Skipped::NoInstrumentRule { symbol } => {
            json!({"kind": "no_instrument_rule", "symbol": symbol})
        }
    }
}

fn target(symbol: &str, notional_usdt: f64) -> Target {
    Target {
        symbol: symbol.to_owned(),
        notional_usdt,
        stop_loss_fraction: 0.35,
        entry_valid_until_ms: None,
        target_qty: None,
    }
}

fn boundary_facts(held_notional: Option<f64>) -> Facts {
    let rule = InstrumentRule {
        tick_size: 0.01,
        qty_step: 0.01,
        min_qty: 0.01,
        min_notional: 0.01,
    };
    let mut facts = Facts::default();
    facts.prices.insert("BOUNDARYUSDT".to_owned(), 1.0);
    facts.rules.insert("BOUNDARYUSDT".to_owned(), rule);
    if let Some(notional) = held_notional {
        facts.held.insert(
            "BOUNDARYUSDT".to_owned(),
            Held {
                side: Side::Buy,
                qty: notional,
                px: 1.0,
                entry_px: 1.0,
                stop_px: 0.65,
            },
        );
    }
    facts
}

#[test]
fn carry_fixture_pins_exact_bytes_planner_events_and_fleet_floors() {
    let fixture: Fixture = serde_json::from_str(FIXTURE_TEXT).expect("valid CARRY fixture");
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.name, "carry_lifecycle_to_rust_target_planner_v1");
    let recorded = fixture.execution_rules;
    assert_eq!(recorded.schema_version, 1);
    assert_eq!(PlanRules::FLEET.entry_floor_usdt, recorded.entry_floor_usdt);
    assert_eq!(PlanRules::FLEET.resize_floor_usdt, recorded.resize_floor_usdt);
    assert_eq!(
        PlanRules::FLEET.resize_floor_fraction,
        recorded.resize_floor_fraction
    );
    assert_eq!(
        PlanRules::FLEET.entry_cutoff_ms,
        recorded.engine_entry_cutoff_ms
    );
    assert_eq!(recorded.signal_validity_ms, 21_600_000);
    assert_eq!(recorded.book_validity_ms, 108_000_000);
    assert_eq!(recorded.presettlement_window_ms, 900_000);

    let digest = Sha256::digest(fixture.target_book_utf8.as_bytes());
    let digest_hex = digest
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    assert_eq!(digest_hex, fixture.target_book_sha256);
    let book: TargetBook =
        serde_json::from_str(&fixture.target_book_utf8).expect("valid exact target bytes");
    assert_eq!(book.version, 2);
    assert_eq!(book.source, "carry_hold_v7_live_v1");
    assert_eq!(book.decision_ts_ms, 1_800_000_000_000);

    let mut facts = Facts::default();
    facts.prices = fixture.rust_replay.prices.clone();
    for symbol in facts.prices.keys() {
        facts
            .rules
            .insert(symbol.clone(), fixture.rust_replay.instrument_rule.engine());
    }
    for held in &fixture.rust_replay.held {
        facts.held.insert(
            held.symbol.clone(),
            Held {
                side: side(&held.side),
                qty: held.qty,
                px: held.px,
                entry_px: held.entry_px,
                stop_px: held.stop_px,
            },
        );
    }
    let targets: Vec<Target> = book
        .targets
        .iter()
        .map(|row| {
            assert_eq!(row.leverage, 2.0);
            Target {
                symbol: row.symbol.clone(),
                notional_usdt: row.notional_usdt,
                stop_loss_fraction: row.stop_loss_fraction,
                entry_valid_until_ms: row.entry_valid_until_ms,
                target_qty: row.target_qty,
            }
        })
        .collect();
    let result = plan(
        &targets,
        &fixture.rust_replay.held_symbols,
        &facts,
        fixture.rust_replay.now_ms,
        book.valid_until_ms,
        PlanRules::FLEET,
    );
    assert_eq!(
        result.steps.iter().map(step_json).collect::<Vec<_>>(),
        fixture.rust_replay.expected_planner_steps
    );
    assert_eq!(
        result.skipped.iter().map(skipped_json).collect::<Vec<_>>(),
        fixture.rust_replay.expected_planner_skips
    );

    let cases = fixture.rust_replay.boundary_cases;
    let entry_facts = boundary_facts(None);
    let below = plan(
        &[target("BOUNDARYUSDT", cases.entry_below_floor_usdt)],
        &[],
        &entry_facts,
        1,
        3_600_001,
        PlanRules::FLEET,
    );
    assert!(matches!(below.skipped.as_slice(), [Skipped::BelowEntryFloor { .. }]));
    let at = plan(
        &[target("BOUNDARYUSDT", cases.entry_at_floor_usdt)],
        &[],
        &entry_facts,
        1,
        3_600_001,
        PlanRules::FLEET,
    );
    assert!(matches!(at.steps.as_slice(), [Step::Enter { .. }]));

    let absolute_facts = boundary_facts(Some(10.0));
    let held = vec!["BOUNDARYUSDT".to_owned()];
    let at_absolute = plan(
        &[
            target(
                "BOUNDARYUSDT",
                10.0 + cases.resize_at_absolute_floor_usdt,
            ),
        ],
        &held,
        &absolute_facts,
        1,
        3_600_001,
        PlanRules::FLEET,
    );
    assert!(matches!(
        at_absolute.skipped.as_slice(),
        [Skipped::TooSmallToBother { .. }]
    ));
    let above_absolute = plan(
        &[
            target(
                "BOUNDARYUSDT",
                10.0 + cases.resize_above_absolute_floor_usdt,
            ),
        ],
        &held,
        &absolute_facts,
        1,
        3_600_001,
        PlanRules::FLEET,
    );
    assert!(matches!(above_absolute.steps.as_slice(), [Step::Resize { .. }]));

    let fraction_facts = boundary_facts(Some(100.0));
    let at_fraction = plan(
        &[
            target(
                "BOUNDARYUSDT",
                100.0 + cases.resize_at_fraction_floor_usdt,
            ),
        ],
        &held,
        &fraction_facts,
        1,
        3_600_001,
        PlanRules::FLEET,
    );
    assert!(matches!(
        at_fraction.skipped.as_slice(),
        [Skipped::TooSmallToBother { .. }]
    ));
    let above_fraction = plan(
        &[
            target(
                "BOUNDARYUSDT",
                100.0 + cases.resize_above_fraction_floor_usdt,
            ),
        ],
        &held,
        &fraction_facts,
        1,
        3_600_001,
        PlanRules::FLEET,
    );
    assert!(matches!(above_fraction.steps.as_slice(), [Step::Resize { .. }]));
}
