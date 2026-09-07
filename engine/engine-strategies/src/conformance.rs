use engine_types::{
    strategy::MAX_STRATEGY_STATE_BYTES, Action, EngineEvent, InstrumentRule, OrderUpdate,
    SignalObservation, StrategyCheckpoint, StrategyCtx, StrategyId, TimerId,
};
use serde::Deserialize;

use crate::mock_ctx::Harness;

const WALL_MS: i64 = 1_700_000_000_000;
const STREAM: &str = include_str!("../tests/fixtures/plug-events.jsonl");

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Event {
    Boot,
    Quote { bid: f64, ask: f64 },
    Depth { bid: f64, ask: f64, quantity: f64 },
    Trades { buy: f64, sell: f64, price: f64 },
    Timer { id: u32 },
    FeedReset,
    Refused { reduce_only: bool },
    Reject,
    Signal { payload: String },
    EntryPermission { enabled: bool },
    Flatten,
}

fn params(name: &str) -> toml::Value {
    let native = match name {
        "long_native" => Some(serde_json::to_string(&crate::native_long::plug::tests::config()).unwrap()),
        "carry_native" => Some(serde_json::to_string(&crate::native_carry::plug::tests::config()).unwrap()),
        "exodus_native" => Some(serde_json::to_string(&crate::native_exodus::plug::tests::config()).unwrap()),
        "quoter" => return toml::from_str("symbols = ['BTCUSDT']\nhalf_spread_bps = 10.0\nrequote_bps = 2.0\nqty = 0.1\nmax_position = 0.3\nstop_loss_fraction = 0.35").unwrap(),
        "probe" => return toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nrest_ms = 2000\nnotional_usdt = 10.0").unwrap(),
        _ => panic!("registered plug {name} needs a conformance fixture"),
    };
    let mut table = toml::map::Map::new();
    table.insert("config_json".into(), toml::Value::String(native.unwrap()));
    toml::Value::Table(table)
}

fn setup(name: &str, builder: crate::Builder) -> Harness {
    let mut h = Harness::new(builder(StrategyId(0), &params(name)).unwrap());
    h.ctx.set_rule(
        "BTCUSDT",
        InstrumentRule {
            tick_size: 0.01,
            qty_step: 0.001,
            min_qty: 0.001,
            min_notional: 5.0,
        },
    );
    h.ctx.set_account_summary(10_000.0, 10_000.0);
    h.ctx.set_strategy_id("carry", StrategyId(1));
    h.ctx.set_strategy_id("exodus", StrategyId(2));
    h.ctx.set_now(1_000_000_000);
    h.ctx.set_wall_ms(WALL_MS);
    if let Some(mut checkpoint) = h.strategy.initial_checkpoint() {
        let mut state: serde_json::Value = serde_json::from_slice(&checkpoint.payload).unwrap();
        match name {
            "long_native" => state["cooldown_until_ms"]["BTCUSDT"] = (WALL_MS + 86_400_000).into(),
            "carry_native" => state["fired_exits"]["BTCUSDT"] = (WALL_MS - 1).into(),
            "exodus_native" => {
                state["consumed_event_ids"] = serde_json::json!(["carry-presettlement-conformance"])
            }
            _ => panic!("checkpoint fixture missing for {name}"),
        }
        checkpoint.payload = serde_json::to_vec(&state).unwrap();
        h.strategy.validate_checkpoint(&checkpoint).unwrap();
        h.ctx.set_global_checkpoint(checkpoint);
    }
    h
}

fn deliver(h: &mut Harness, event: &Event, sequence: usize) {
    let now = 1_000_000_000 + sequence as u64 * 1_000_000;
    h.ctx.set_now(now);
    h.ctx.set_wall_ms(WALL_MS + sequence as i64);
    let event = match event {
        Event::Boot => EngineEvent::Boot,
        Event::Quote { bid, ask } => {
            h.quote("BTCUSDT", *bid, *ask);
            return;
        }
        Event::Depth { bid, ask, quantity } => {
            h.depth("BTCUSDT", &[(*bid, *quantity)], &[(*ask, *quantity)]);
            return;
        }
        Event::Trades { buy, sell, price } => {
            h.trades("BTCUSDT", *buy, *sell, *price);
            return;
        }
        Event::Timer { id } => EngineEvent::Timer {
            id: TimerId(*id),
            now_ns: now,
        },
        Event::FeedReset => {
            h.feed_reset();
            return;
        }
        Event::Refused { reduce_only } => EngineEvent::IntentRefused {
            symbol: h.ctx.id_of("BTCUSDT"),
            reduce_only: *reduce_only,
            reason: "venue_busy".into(),
        },
        Event::Reject => EngineEvent::Order(OrderUpdate::Reject {
            client_order_id: "conformance-order".into(),
            code: 10001,
            reason: "fixture rejection".into(),
        }),
        Event::Signal { payload } => EngineEvent::Signal(SignalObservation {
            schema_version: 1,
            decision_fingerprint: h
                .strategy
                .checkpoint_identity()
                .map(|id| id.decision_fingerprint)
                .unwrap_or_default(),
            destination: StrategyId(0),
            source: "conformance".into(),
            sequence: sequence as u64,
            observation_id: format!("conformance-{sequence}"),
            kind: "invalid_fixture".into(),
            observed_wall_ts_ms: WALL_MS,
            available_wall_ts_ms: WALL_MS,
            subscriptions: Vec::new(),
            payload: payload.as_bytes().to_vec(),
            content_sha256: "0".repeat(64),
        }),
        Event::EntryPermission { enabled } => EngineEvent::EntryPermission {
            request_id: format!("permission-{sequence}"),
            entries_enabled: *enabled,
        },
        Event::Flatten => EngineEvent::FlattenDirectional {
            request_id: "conformance-flatten".into(),
        },
    };
    h.strategy.on_event(&event, &mut h.ctx);
}

fn actions(h: &mut Harness) -> Vec<u8> {
    let emitted = h.drain_actions();
    for action in &emitted {
        if let Action::SetStrategyGlobalCheckpoint { checkpoint, .. } = action {
            validate_checkpoint(h, checkpoint);
            h.ctx.set_global_checkpoint(checkpoint.clone());
        }
    }
    serde_json::to_vec(&emitted).unwrap()
}

fn validate_checkpoint(h: &Harness, checkpoint: &StrategyCheckpoint) {
    assert!(checkpoint.payload.len() <= MAX_STRATEGY_STATE_BYTES);
    h.strategy.validate_checkpoint(checkpoint).unwrap();
    let bytes = serde_json::to_vec(checkpoint).unwrap();
    let decoded: StrategyCheckpoint = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(bytes, serde_json::to_vec(&decoded).unwrap());
}

fn bounded_runtime(h: &Harness) -> engine_types::strategy_process::StrategyRuntimeState {
    let state = h
        .strategy
        .runtime_state()
        .unwrap()
        .expect("registered plug exposes retained state");
    assert!(
        state.payload.len() <= MAX_STRATEGY_STATE_BYTES,
        "{} state grew to {} bytes",
        h.strategy.name(),
        state.payload.len()
    );
    state
}

type RecordedActions = Vec<Vec<u8>>;
type RecordedTimers = Vec<(u32, u64, u64)>;

fn replay(name: &str, builder: crate::Builder) -> (RecordedActions, RecordedTimers) {
    let mut h = setup(name, builder);
    let mut bytes = Vec::new();
    for (sequence, line) in STREAM.lines().enumerate() {
        let event: Event = serde_json::from_str(line).unwrap();
        deliver(&mut h, &event, sequence);
        bytes.push(actions(&mut h));
        bounded_runtime(&h);
    }
    assert!(
        bytes.iter().any(|row| row != b"[]"),
        "{name} fixture exercised no action"
    );
    let timers = h
        .ctx
        .arm_calls
        .iter()
        .map(|timer| (timer.id.0, timer.armed_ns, timer.due_ns))
        .collect();
    (bytes, timers)
}

#[test]
fn conformance_every_registered_plug_replays_recorded_actions_and_timers_byte_identically() {
    for &(name, builder) in crate::PLUGS {
        let first = replay(name, builder);
        let second = replay(name, builder);
        assert_eq!(
            serde_json::to_vec(&first).unwrap(),
            serde_json::to_vec(&second).unwrap(),
            "{name}"
        );
        eprintln!("conformance recorded replay: {name}");
    }
}

#[test]
fn conformance_every_registered_plug_restores_checkpoint_idempotently_with_bounded_state() {
    for &(name, builder) in crate::PLUGS {
        let mut h = setup(name, builder);
        let seeded_checkpoint = h.ctx.strategy_global_checkpoint().cloned();
        for (sequence, line) in STREAM.lines().enumerate() {
            deliver(&mut h, &serde_json::from_str(line).unwrap(), sequence);
            actions(&mut h);
        }
        if let Some(checkpoint) = seeded_checkpoint {
            validate_checkpoint(&h, &checkpoint);
            assert_ne!(
                checkpoint,
                h.strategy.initial_checkpoint().unwrap(),
                "{name} tested only empty checkpoint"
            );
            let mut restored = setup(name, builder);
            restored.ctx.set_global_checkpoint(checkpoint);
            restored.boot();
            actions(&mut restored);
            let once = restored.ctx.strategy_global_checkpoint().unwrap().clone();
            let mut twice = setup(name, builder);
            twice.ctx.set_global_checkpoint(once.clone());
            twice.boot();
            actions(&mut twice);
            assert_eq!(
                once,
                *twice.ctx.strategy_global_checkpoint().unwrap(),
                "{name} checkpoint restore changed state twice"
            );
            assert_eq!(
                bounded_runtime(&restored),
                bounded_runtime(&twice),
                "{name} second restore changed private state"
            );
        } else {
            assert!(
                h.strategy.checkpoint_identity().is_none(),
                "{name} declares a checkpoint but does not seed one"
            );
        }
        let state = bounded_runtime(&h);
        let restored = crate::runtime::restore(&state).unwrap();
        let once = restored.runtime_state().unwrap().unwrap();
        let twice = crate::runtime::restore(&once)
            .unwrap()
            .runtime_state()
            .unwrap()
            .unwrap();
        assert_eq!(state, once, "{name} retained runtime round trip");
        assert_eq!(
            once, twice,
            "{name} retained runtime restore is not idempotent"
        );
        eprintln!("conformance checkpoint and retained state: {name}");
    }
}

#[test]
fn conformance_every_registered_plug_accepts_seeded_event_corpus_without_panic_or_state_growth() {
    for &(name, builder) in crate::PLUGS {
        for seed in [1_u64, 0x5eed, 0xdead_beef, u32::MAX as u64] {
            let mut random = seed;
            let mut h = setup(name, builder);
            deliver(&mut h, &Event::Boot, 0);
            actions(&mut h);
            for sequence in 1..=512 {
                random ^= random << 13;
                random ^= random >> 7;
                random ^= random << 17;
                let price = 1.0 + (random % 1_000_000) as f64 / 100.0;
                let event = match random % 10 {
                    0 => Event::Quote {
                        bid: price,
                        ask: price + 0.01,
                    },
                    1 => Event::Depth {
                        bid: price,
                        ask: price + 0.01,
                        quantity: (random % 1000) as f64 / 10.0,
                    },
                    2 => Event::Trades {
                        buy: (random % 100) as f64,
                        sell: ((random >> 8) % 100) as f64,
                        price,
                    },
                    3 => Event::FeedReset,
                    4 => Event::Timer {
                        id: [1, 2, 3, 0x4341_5259, 0x4c4f_4e47, 0x4558_4f44]
                            [(random >> 8) as usize % 6],
                    },
                    5 => Event::Refused {
                        reduce_only: random & 256 == 0,
                    },
                    6 => Event::Signal {
                        payload: format!("{{\"fuzz\":{random}}}"),
                    },
                    7 => Event::Reject,
                    8 => Event::EntryPermission {
                        enabled: random & 256 == 0,
                    },
                    _ => Event::Flatten,
                };
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    deliver(&mut h, &event, sequence)
                }));
                assert!(
                    outcome.is_ok(),
                    "{name} panicked seed={seed} event={sequence}: {event:?}"
                );
                actions(&mut h);
                bounded_runtime(&h);
            }
        }
        eprintln!("conformance seeded corpus: {name}, 2048 events");
    }
}
