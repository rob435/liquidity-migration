use std::collections::{BTreeMap, BTreeSet, VecDeque};

use engine_types::strategy_process::{
    CallbackEvent, CallbackPreparation, CallbackReply, CallbackSnapshot, StrategyCallbackInput,
    StrategyProcessState,
};
use engine_types::{
    AccountView, Action, MarketEvent, MarketState, OrderUpdate, StrategyId, SymbolId, WalRecord,
};
use serde_json::Value;

use crate::attribution::Attribution;
use crate::covers::CoverBook;
use crate::ctx::{Books, Ctx, Timers};
use crate::inflight::{LedgerOfOrders, OrderRegistry};

use super::state::CallbackState;
struct CallbackProposal {
    callback_id: u64,
    actions: Vec<Action>,
    timers: Vec<engine_types::strategy_process::StrategyTimerState>,
    state: engine_types::strategy_process::StrategyRuntimeState,
    retained_signal_subscriptions: Option<Vec<engine_types::Subscription>>,
}

const LONG: StrategyId = StrategyId(1);
const TAO: SymbolId = SymbolId(0);

fn remap_symbol(value: &mut Value) {
    match value {
        Value::Object(fields) => {
            for (key, value) in fields {
                if key == "symbol" && value.is_number() {
                    *value = serde_json::json!(TAO.0);
                } else {
                    remap_symbol(value);
                }
            }
        }
        Value::Array(values) => values.iter_mut().for_each(remap_symbol),
        _ => {}
    }
}

fn replay(records: &[WalRecord]) -> CallbackState {
    let encoded = serde_json::to_vec(records).unwrap();
    let decoded = serde_json::from_slice::<Vec<WalRecord>>(&encoded).unwrap();
    CallbackState::replay(&decoded, 2).unwrap()
}

fn queue(records: &mut Vec<WalRecord>, mut input: StrategyCallbackInput) {
    let preparation = std::mem::replace(&mut input.preparation, CallbackPreparation::Queued);
    records.push(WalRecord::Retained(
        engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
            input: input.clone(),
        },
    ));
    input.preparation = preparation;
    records.push(WalRecord::Retained(
        engine_types::wal::RetainedWalRecord::StrategyCallbackPrepared { input },
    ));
}

fn serve_retained(state: &CallbackState, input_id: u64) -> CallbackProposal {
    let input = state.inputs.get(&input_id).unwrap();
    let runtime = state.committed.get(&LONG).unwrap().runtime.clone();
    let mut strategy = engine_strategies::runtime::restore(&runtime).unwrap();
    let mut actions = Vec::new();
    let mut timers = Vec::new();
    let mut ctx =
        engine_types::strategy_process::SnapshotCtx::new(input.snapshot().unwrap(), |reply| {
            match reply {
                CallbackReply::Action { action } => actions.push(action),
                CallbackReply::Timer { timer } => timers.push(timer),
                _ => (),
            }
        })
        .unwrap();
    strategy.on_event(
        &engine_types::EngineEvent::try_from(&input.event).unwrap(),
        &mut ctx,
    );
    drop(ctx);
    CallbackProposal {
        callback_id: input_id,
        actions,
        timers,
        state: strategy.runtime_state().unwrap().unwrap(),
        retained_signal_subscriptions: strategy.retained_signal_subscriptions(),
    }
}

fn commit(records: &mut Vec<WalRecord>, proposal: CallbackProposal) {
    assert!(
        proposal
            .actions
            .iter()
            .all(|action| !matches!(action, Action::Place(_))),
        "retained order news must not submit another TAO entry"
    );
    records.push(WalRecord::Retained(
        engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued {
            input_id: proposal.callback_id,
            transition: None,
            process: StrategyProcessState {
                strategy: LONG,
                last_callback_id: proposal.callback_id,
                runtime: proposal.state,
                timers: proposal.timers,
                retained_signal_subscriptions: proposal.retained_signal_subscriptions,
            },
        },
    ));
}

fn tao_state(state: &CallbackState) -> Value {
    let runtime = &state.committed.get(&LONG).unwrap().runtime;
    let payload: Value = serde_json::from_slice(&runtime.payload).unwrap();
    payload["core"]["state"]["symbols"]["TAOUSDT"].clone()
}

fn snapshot(
    records: &[WalRecord],
    quote: &CallbackSnapshot,
    now_ns: u64,
    wall_ms: i64,
) -> CallbackSnapshot {
    let row = &quote.symbols[0];
    let mut market = MarketState::default();
    assert_eq!(market.add_symbol("TAOUSDT"), TAO);
    market.apply(&MarketEvent::Quote {
        symbol: TAO,
        quote: row.quote,
    });
    market.apply(&MarketEvent::Ticker {
        symbol: TAO,
        ticker: row.ticker,
    });
    let orders = LedgerOfOrders::try_from_records(records).unwrap();
    let mut registry = OrderRegistry::new("long-restart-fixture".into());
    for id in orders.orders.keys() {
        registry.own(id, LONG);
    }
    let books = Books {
        market,
        account: AccountView {
            exact_amounts: None,
            equity_usdt: quote.account.equity_usdt,
            available_usdt: quote.account.available_margin_usdt,
            positions: Vec::new(),
            observed_ns: now_ns,
        },
        rules: vec![row.instrument],
        portfolio_symbols: BTreeSet::from([TAO]),
        orders,
        registry,
        attribution: Attribution::try_from_records(records).unwrap(),
        covers: CoverBook::default(),
    };
    let mut actions = VecDeque::new();
    let mut timers = Timers::default();
    let checkpoints = BTreeMap::new();
    let global_checkpoints = BTreeMap::new();
    let events = BTreeMap::new();
    let names = vec!["carry".into(), "long".into()];
    let context = Ctx {
        books: &books,
        now_ns,
        strategy: LONG,
        out: &mut actions,
        timers: &mut timers,
        checkpoints: &checkpoints,
        global_checkpoints: &global_checkpoints,
        strategy_events: &events,
        strategy_names: &names,
        runtime_entries_enabled: Some(true),
    };
    let mut snapshot = context.callback_snapshot().unwrap();
    // The captured ACK has no durable snapshot; books reconstruct its inputs
    // and these recorded event times provide the test's clock.
    snapshot.wall_ms = wall_ms;
    snapshot
}

fn captured_long_ack_restarts_before_tao_fill_without_inventing_inventory(realm: &str) {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../tests/fixtures/long-tao-pending-restart.json"
    ))
    .unwrap();
    assert_eq!(
        fixture["cases"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|case| case["realm"] == realm)
            .count(),
        1
    );
    for original in fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| case["realm"] == realm)
    {
        let mut case = original.clone();
        remap_symbol(&mut case);
        case["prepared_quote"]["preparation"]["snapshot"]["symbols"][0]["id"] =
            serde_json::json!(TAO.0);
        case["process"]["runtime"]["payload"] =
            serde_json::to_value(serde_json::to_vec(&case["runtime_payload"]).unwrap()).unwrap();
        let process: StrategyProcessState =
            serde_json::from_value(case["process"].clone()).unwrap();
        engine_strategies::runtime::restore(&process.runtime).unwrap();
        let input: StrategyCallbackInput =
            serde_json::from_value(case["prepared_quote"].clone()).unwrap();
        let quote = input.snapshot().unwrap().clone();
        let sent: WalRecord = serde_json::from_value(case["order_sent"].clone()).unwrap();
        let ack: WalRecord = serde_json::from_value(case["ack"].clone()).unwrap();
        let fill: WalRecord = serde_json::from_value(case["fill"].clone()).unwrap();
        let WalRecord::OrderSent { request, .. } = &sent else {
            panic!("sent fixture")
        };
        let quantity = request.exact_terms.as_ref().unwrap().quantity.clone();
        let WalRecord::OrderUpdate {
            update: OrderUpdate::Ack(ack_update),
            ..
        } = &ack
        else {
            panic!("ACK fixture")
        };
        let ack_time = ack_update.ack_ns;
        let WalRecord::OrderUpdate {
            update:
                fill_update @ OrderUpdate::Fill {
                    px,
                    venue_ts_ms,
                    recv_ns,
                    ..
                },
            ..
        } = &fill
        else {
            panic!("Fill fixture")
        };
        let fill_update = fill_update.clone();
        let (fill_price, fill_wall, fill_time) = (*px, *venue_ts_ms, *recv_ns);
        let prior_id = process.last_callback_id;
        let mut records = vec![WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::Names {
                strategies: vec!["carry".into(), "long".into()],
                symbols: vec!["TAOUSDT".into()],
            },
        )];
        queue(&mut records, input);
        records.push(WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued {
                input_id: prior_id,
                transition: None,
                process,
            },
        ));
        records.extend([sent, ack.clone()]);
        let ack_snapshot = snapshot(&records, &quote, ack_time, quote.wall_ms);
        let symbol = &ack_snapshot.symbols[0];
        assert!(symbol.exact_my_position.as_ref().unwrap().is_zero());
        assert_eq!(symbol.exact_in_flight.as_deref(), Some(&quantity));
        assert_eq!(
            symbol
                .facts
                .as_ref()
                .unwrap()
                .allocated
                .as_ref()
                .unwrap()
                .entry_px,
            None
        );
        assert!(ack_snapshot.orders[0].acked && ack_snapshot.orders[0].resting);
        let WalRecord::OrderUpdate { update, .. } = ack else {
            unreachable!()
        };
        let ack_id = prior_id + 1;
        queue(
            &mut records,
            StrategyCallbackInput {
                order_origin: None,
                callback_id: ack_id,
                strategy: LONG,
                event: CallbackEvent::Order { update },
                preparation: CallbackPreparation::Prepared {
                    snapshot: ack_snapshot,
                },
            },
        );
        let retained = replay(&records);
        assert_eq!(retained.inputs.len(), 1);
        assert_eq!(tao_state(&retained)["filled"], false);
        let abandoned = serve_retained(&retained, ack_id);
        let retry = serve_retained(&replay(&records), ack_id);
        assert_eq!(
            abandoned.state, retry.state,
            "a crash before commit must replay the same prepared ACK"
        );
        assert_eq!(abandoned.actions, retry.actions);
        commit(&mut records, retry);
        let acknowledged = replay(&records);
        assert!(acknowledged.inputs.is_empty());
        assert_eq!(tao_state(&acknowledged)["filled"], false);
        assert_eq!(tao_state(&acknowledged)["entry_ts_ms"], 0);
        records.push(fill);
        let filled_snapshot = snapshot(&records, &quote, fill_time, fill_wall);
        assert_eq!(
            filled_snapshot.symbols[0].exact_my_position.as_deref(),
            Some(&quantity)
        );
        assert!(filled_snapshot.symbols[0]
            .exact_in_flight
            .as_ref()
            .unwrap()
            .is_zero());
        assert_eq!(
            filled_snapshot.symbols[0]
                .facts
                .as_ref()
                .unwrap()
                .allocated
                .as_ref()
                .unwrap()
                .entry_px,
            Some(fill_price)
        );
        let fill_id = ack_id + 1;
        queue(
            &mut records,
            StrategyCallbackInput {
                order_origin: None,
                callback_id: fill_id,
                strategy: LONG,
                event: CallbackEvent::Order {
                    update: fill_update,
                },
                preparation: CallbackPreparation::Prepared {
                    snapshot: filled_snapshot,
                },
            },
        );
        let proposal = serve_retained(&replay(&records), fill_id);
        commit(&mut records, proposal);
        let completed = replay(&records);
        assert!(completed.inputs.is_empty());
        assert_eq!(completed.committed[&LONG].last_callback_id, fill_id);
        let state = tao_state(&completed);
        assert_eq!(state["filled"], true);
        assert_eq!(state["entry_price"], fill_price);
        assert_eq!(state["entry_ts_ms"], fill_wall);
        assert_eq!(state["max_hold_deadline_ts_ms"], fill_wall + 259_200_000);
        let inventory = Attribution::try_from_records(&records).unwrap();
        assert_eq!(inventory.signed_exact(LONG, TAO), quantity);
        assert_eq!(
            inventory.snapshot(),
            Attribution::try_from_records(&records).unwrap().snapshot()
        );
    }
}

#[test]
fn captured_demo_long_ack_restarts_before_tao_fill_without_inventing_inventory() {
    captured_long_ack_restarts_before_tao_fill_without_inventing_inventory("demo");
}

#[test]
fn captured_mainnet_long_ack_restarts_before_tao_fill_without_inventing_inventory() {
    captured_long_ack_restarts_before_tao_fill_without_inventing_inventory("mainnet");
}
