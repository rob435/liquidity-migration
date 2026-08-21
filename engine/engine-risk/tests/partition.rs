//! The per-strategy capital partition, against the same table as
//! tests/account/test_sleeve_capital_partition.py. Room for 1000 USDT of book,
//! split 600 CARRY / 300 LONG, every symbol priced at 10.0.
//!
//! One deliberate difference, recorded in PORT_NOTES.md: where the Python
//! kernel refuses a batch that puts a sleeve over its share, this kernel clamps
//! the order to what the share still funds and only refuses when nothing
//! meaningful is left.

mod common;

use common::*;
use engine_risk::{Kernel, PartitionConfig, StrategyAllocation};
use engine_types::ids::StrategyId;
use engine_types::orders::{Intent, OrderUpdate, Side};
use engine_types::risk::{DenyReason, RiskKernel, RiskVerdict};

const NOW: u64 = SEC;

fn kernel() -> Kernel {
    Kernel::new(partition_config()).expect("config")
}

fn buy(strategy: StrategyId, symbol: engine_types::ids::SymbolId, qty: f64) -> Intent {
    entry(strategy, symbol, Side::Buy, qty, 10.0, 9.0, NOW)
}

/// Take the order all the way to a fill, the way the engine core does.
fn fill(kernel: &mut Kernel, id: &str, intent: &Intent, qty: f64) {
    kernel.register_order(id, intent, qty);
    kernel.on_update(&OrderUpdate::Fill {
        client_order_id: id.to_string(),
        symbol: intent.symbol,
        side: intent.side,
        qty,
        px: 10.0,
        fee: 0.0,
        is_maker: false,
        venue_ts_ms: 0,
        recv_ns: 0,
    });
}

#[test]
// test_a_sleeve_inside_its_share_is_accepted: 500 USDT of 600.
fn a_strategy_inside_its_share_is_accepted() {
    let mut kernel = kernel();
    assert_eq!(
        kernel.assess(&buy(CARRY, BUSDT, 50.0), &flat(10_000.0, NOW)),
        RiskVerdict::Allow { qty: 50.0 }
    );
}

#[test]
// test_a_sleeve_over_its_own_share_is_refused_while_the_account_has_room:
// 700 USDT fits the 1000 account cap but not CARRY's 600.
fn a_strategy_over_its_own_share_is_clamped_while_the_account_has_room() {
    let mut unpartitioned = Kernel::new(demo_config()).expect("config");
    assert_eq!(
        unpartitioned.assess(&buy(CARRY, BUSDT, 70.0), &flat(10_000.0, NOW)),
        RiskVerdict::Allow { qty: 70.0 },
        "the account cap alone would have allowed this"
    );

    let mut kernel = kernel();
    assert_eq!(
        kernel.assess(&buy(CARRY, BUSDT, 70.0), &flat(10_000.0, NOW)),
        RiskVerdict::Allow { qty: 60.0 },
        "clamped to the 600 USDT the share funds"
    );
}

#[test]
// test_one_sleeve_cannot_crowd_the_other_out
fn one_strategy_cannot_crowd_the_other_out() {
    let mut kernel = kernel();
    let first = buy(CARRY, BUSDT, 60.0);
    assert_eq!(
        kernel.assess(&first, &flat(10_000.0, NOW)),
        RiskVerdict::Allow { qty: 60.0 }
    );
    fill(&mut kernel, "carry-1", &first, 60.0);

    let held = view(
        10_000.0,
        vec![position(BUSDT, Side::Buy, 60.0, 10.0, true)],
        NOW,
    );
    assert_eq!(
        kernel.assess(&buy(LONG, CUSDT, 30.0), &held),
        RiskVerdict::Allow { qty: 30.0 },
        "LONG spends its own share"
    );
}

#[test]
// test_a_sleeve_the_partition_does_not_name_gets_nothing
fn a_strategy_the_partition_does_not_name_gets_nothing() {
    let mut kernel = kernel();
    assert_eq!(
        kernel.assess(&buy(CONTINUOUS, BUSDT, 1.0), &flat(10_000.0, NOW)),
        RiskVerdict::Deny {
            reason: DenyReason::PartitionExhausted {
                strategy: CONTINUOUS,
                requested_usdt: 10.0,
                remaining_usdt: 0.0,
            }
        }
    );
}

#[test]
// test_an_unpartitioned_policy_keeps_the_historical_behaviour
fn an_unpartitioned_config_keeps_the_historical_behaviour() {
    let mut kernel = Kernel::new(demo_config()).expect("config");
    assert_eq!(
        kernel.assess(&buy(CONTINUOUS, BUSDT, 50.0), &flat(10_000.0, NOW)),
        RiskVerdict::Allow { qty: 50.0 }
    );
}

#[test]
// test_the_margin_share_binds_independently_of_the_gross_share: 1000 USDT
// gross at 1x is 1000 of margin, inside the 5000 gross share and far outside
// the 100 margin share.
fn the_margin_share_binds_independently_of_the_gross_share() {
    let mut cfg = partition_config();
    cfg.envelope.reference_usdt = 5_000.0;
    // The partition below runs at leverage 1, so the most book this account
    // could ever carry is the reference itself. The gross multiple has to say
    // so: at the fixture's 2.0 the account cap would be 10,000 of book that no
    // amount of margin could fund, which the load-time proof now refuses.
    cfg.envelope.gross_notional_multiple = 1.0;
    // The reference moves tenfold, so the account caps move with it. Left at
    // the 500 USDT shape's numbers the account margin cap would refuse this
    // order before the margin share got to clamp it, and the test would be
    // watching the wrong control.
    cfg.envelope.max_component_gross_notional_usdt = 5_000.0;
    cfg.envelope.max_initial_margin_usdt = 5_000.0;
    cfg.partition = PartitionConfig {
        allocations: vec![StrategyAllocation {
            strategy: CARRY,
            max_gross_notional_usdt: 5_000.0,
            max_initial_margin_usdt: 100.0,
        }],
        leverage: 1.0,
        min_order_notional_usdt: MIN_ORDER_NOTIONAL_USDT,
    };
    let mut kernel = Kernel::new(cfg).expect("config");
    assert_eq!(
        kernel.assess(&buy(CARRY, BUSDT, 100.0), &flat(10_000.0, NOW)),
        RiskVerdict::Allow { qty: 10.0 },
        "clamped to the 100 USDT the margin share funds"
    );
}

#[test]
// test_an_exit_is_never_blocked_by_the_partition: the share shrinks below the
// position already held, as an equity contraction would shrink it.
fn an_exit_is_never_blocked_by_the_partition() {
    let mut cfg = partition_config();
    cfg.partition.allocations = vec![StrategyAllocation {
        strategy: CARRY,
        max_gross_notional_usdt: 1.0,
        max_initial_margin_usdt: 1.0,
    }];
    let mut kernel = Kernel::new(cfg).expect("config");
    let held = view(
        10_000.0,
        vec![position(BUSDT, Side::Buy, 60.0, 10.0, true)],
        NOW,
    );
    assert_eq!(
        kernel.assess(&exit(CARRY, BUSDT, Side::Sell, 60.0, 10.0, NOW), &held),
        RiskVerdict::Allow { qty: 60.0 }
    );
}

#[test]
// test_a_sleeve_over_its_share_still_cannot_grow: relief is not permission.
fn a_strategy_over_its_share_cannot_grow() {
    let mut kernel = kernel();
    let opened = buy(CARRY, BUSDT, 55.0);
    assert!(matches!(
        kernel.assess(&opened, &flat(10_000.0, NOW)),
        RiskVerdict::Allow { .. }
    ));
    fill(&mut kernel, "carry-1", &opened, 55.0);

    // The share contracts under the 550 USDT CARRY already holds.
    let mut contracted = partition_config();
    contracted.partition.allocations = vec![StrategyAllocation {
        strategy: CARRY,
        max_gross_notional_usdt: 400.0,
        max_initial_margin_usdt: 200.0,
    }];
    let mut tightened = Kernel::new(contracted).expect("config");
    let opened_again = buy(CARRY, BUSDT, 55.0);
    tightened.register_order("carry-1", &opened_again, 55.0);
    tightened.on_update(&OrderUpdate::Fill {
        client_order_id: "carry-1".to_string(),
        symbol: BUSDT,
        side: Side::Buy,
        qty: 55.0,
        px: 10.0,
        fee: 0.0,
        is_maker: false,
        venue_ts_ms: 0,
        recv_ns: 0,
    });

    let held = view(
        10_000.0,
        vec![position(BUSDT, Side::Buy, 55.0, 10.0, true)],
        NOW,
    );
    assert_eq!(
        tightened.assess(&buy(CARRY, BUSDT, 1.0), &held),
        RiskVerdict::Deny {
            reason: DenyReason::PartitionExhausted {
                strategy: CARRY,
                requested_usdt: 10.0,
                remaining_usdt: 0.0,
            }
        }
    );
}

#[test]
fn the_partition_clamps_when_a_smaller_size_fits() {
    let mut kernel = kernel();
    let opened = buy(CARRY, BUSDT, 59.9);
    assert!(matches!(
        kernel.assess(&opened, &flat(10_000.0, NOW)),
        RiskVerdict::Allow { .. }
    ));
    fill(&mut kernel, "carry-1", &opened, 59.9);

    let held = view(
        10_000.0,
        vec![position(BUSDT, Side::Buy, 59.9, 10.0, true)],
        NOW,
    );
    // 1 USDT of the 600 share is left; asking for 50 gets 0.1 of quantity.
    match kernel.assess(&buy(CARRY, BUSDT, 5.0), &held) {
        RiskVerdict::Allow { qty } => assert!((qty - 0.1).abs() < 1e-9, "clamped to {qty}"),
        other => panic!("expected a clamp, got {other:?}"),
    }
}

#[test]
fn the_partition_refuses_when_the_clamped_size_is_not_meaningful() {
    let mut cfg = partition_config();
    cfg.partition.min_order_notional_usdt = 2.0;
    let mut kernel = Kernel::new(cfg).expect("config");
    let opened = buy(CARRY, BUSDT, 59.9);
    assert!(matches!(
        kernel.assess(&opened, &flat(10_000.0, NOW)),
        RiskVerdict::Allow { .. }
    ));
    fill(&mut kernel, "carry-1", &opened, 59.9);

    let held = view(
        10_000.0,
        vec![position(BUSDT, Side::Buy, 59.9, 10.0, true)],
        NOW,
    );
    match kernel.assess(&buy(CARRY, BUSDT, 5.0), &held) {
        RiskVerdict::Deny {
            reason:
                DenyReason::PartitionExhausted {
                    remaining_usdt,
                    requested_usdt,
                    ..
                },
        } => {
            assert!((remaining_usdt - 1.0).abs() < 1e-9);
            assert!((requested_usdt - 50.0).abs() < 1e-9);
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
}

#[test]
fn an_order_in_flight_spends_the_share_before_it_fills() {
    let mut kernel = kernel();
    let opened = buy(CARRY, BUSDT, 55.0);
    assert!(matches!(
        kernel.assess(&opened, &flat(10_000.0, NOW)),
        RiskVerdict::Allow { .. }
    ));
    kernel.register_order("carry-1", &opened, 55.0);

    // 50 USDT of the share is left while the first order is still in flight.
    match kernel.assess(&buy(CARRY, BUSDT, 10.0), &flat(10_000.0, NOW)) {
        RiskVerdict::Allow { qty } => assert!((qty - 5.0).abs() < 1e-9, "clamped to {qty}"),
        other => panic!("expected a clamp, got {other:?}"),
    }

    // A cancel gives the share back.
    kernel.on_update(&OrderUpdate::Cancelled {
        client_order_id: "carry-1".to_string(),
        recv_ns: 0,
    });
    assert_eq!(
        kernel.assess(&buy(CARRY, BUSDT, 10.0), &flat(10_000.0, NOW)),
        RiskVerdict::Allow { qty: 10.0 }
    );
}

#[test]
// A fill for an order the kernel never approved is a second writer on the
// account. It is charged to every share until it nets out.
fn an_unattributed_fill_is_charged_to_every_share() {
    let mut kernel = kernel();
    kernel.on_update(&OrderUpdate::Fill {
        client_order_id: "not-ours".to_string(),
        symbol: BUSDT,
        side: Side::Buy,
        qty: 29.0,
        px: 10.0,
        fee: 0.0,
        is_maker: false,
        venue_ts_ms: 0,
        recv_ns: 0,
    });
    let held = view(
        10_000.0,
        vec![position(BUSDT, Side::Buy, 29.0, 10.0, true)],
        NOW,
    );
    match kernel.assess(&buy(LONG, CUSDT, 5.0), &held) {
        RiskVerdict::Allow { qty } => assert!((qty - 1.0).abs() < 1e-9, "clamped to {qty}"),
        other => panic!("expected a clamp, got {other:?}"),
    }
}
