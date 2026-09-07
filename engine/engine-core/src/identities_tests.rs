use super::*;
use engine_types::{OrderKind, OrderRequest, Side};

fn scope(environment: &str) -> InstrumentScope {
    InstrumentScope {
        venue: "hyperliquid".into(),
        environment: environment.into(),
    }
}

fn names(sleeves: &[&str], symbols: &[&str]) -> WalRecord {
    WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
        strategies: sleeves.iter().map(|value| (*value).into()).collect(),
        symbols: symbols.iter().map(|value| (*value).into()).collect(),
    })
}

fn registry(state: IdentityState) -> WalRecord {
    WalRecord::IdentityState {
        wall_ts_ms: 1,
        state,
    }
}

fn rotation(state: Option<&IdentityState>, sleeves: &[&str], symbols: &[&str]) -> WalRecord {
    serde_json::from_value(serde_json::json!({
        "kind": "segment_base_v3", "wall_ts_ms": 1,
        "strategies": sleeves, "symbols": symbols, "may_open": true,
        "control_anchors": [], "attribution": [], "logged_exposure": [],
        "intended_stops": [], "open_orders": [], "identities": state,
    }))
    .unwrap()
}

#[test]
fn reordered_config_keeps_archived_orders_and_checkpoints_with_their_sleeve() {
    let records = vec![
        names(&["alpha", "beta"], &["BTCUSDT"]),
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: "owned-alpha".into(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                close_position: false,
                sleeve_effect: None,
                exact_terms: None,
            },
            wire_ns: 1,
            arrival_mid: 100.0,
        },
        WalRecord::StrategyGlobalCheckpoint {
            wall_ts_ms: 1,
            strategy: StrategyId(1),
            checkpoint: engine_types::StrategyCheckpoint {
                schema_version: 1,
                decision_fingerprint: "beta-v1".into(),
                payload: b"beta durable state".to_vec(),
            },
            provenance: None,
        },
    ];
    let archived_bytes = serde_json::to_vec(&records).unwrap();
    let plan = plan_identities(
        &records,
        &["beta".into(), "alpha".into(), "gamma".into()],
        None,
        &BTreeMap::new(),
        &[],
    )
    .unwrap();
    assert_eq!(
        plan.configured_ids,
        [StrategyId(1), StrategyId(0), StrategyId(2)]
    );
    assert_eq!(plan.slot_configs, [Some(1), Some(0), Some(2)]);
    let WalRecord::OrderSent { request, .. } = &records[1] else {
        unreachable!()
    };
    let configured = ["beta", "alpha", "gamma"];
    assert_eq!(
        configured[plan.slot_configs[request.strategy.idx()].unwrap()],
        "alpha"
    );
    assert_eq!(configured[plan.slot_configs[1].unwrap()], "beta");
    assert_eq!(serde_json::to_vec(&records).unwrap(), archived_bytes);
    assert!(
        plan.changed,
        "legacy Names bindings need their first durable registry record"
    );
}

#[test]
fn missing_sleeves_keep_passive_slots_and_reactivation_uses_the_same_id() {
    let first = plan_identities(
        &[names(&["alpha", "beta"], &[])],
        &["beta".into(), "gamma".into()],
        None,
        &BTreeMap::new(),
        &[],
    )
    .unwrap();
    assert_eq!(first.slot_configs, [None, Some(0), Some(1)]);
    assert_eq!(first.configured_ids, [StrategyId(1), StrategyId(2)]);
    let resumed = plan_identities(
        &[registry(first.state)],
        &["gamma".into(), "alpha".into(), "beta".into()],
        None,
        &BTreeMap::new(),
        &[],
    )
    .unwrap();
    assert_eq!(
        resumed.configured_ids,
        [StrategyId(2), StrategyId(0), StrategyId(1)]
    );
    assert!(
        !resumed.changed,
        "config ordering and activation do not rewrite stable identities"
    );
}

#[test]
fn ambiguous_legacy_names_and_reassigned_dense_history_are_refused() {
    for records in [
        vec![names(&["same", "same"], &["BTCUSDT"])],
        vec![
            names(&["alpha", "beta"], &["BTCUSDT"]),
            names(&["beta", "alpha"], &["BTCUSDT"]),
        ],
        vec![
            names(&["alpha"], &["OLDUSDT"]),
            names(&["alpha"], &["NEWUSDT"]),
        ],
    ] {
        assert!(replay_identities(&records).is_err());
    }
    assert!(plan_identities(
        &[],
        &["same".into(), "same".into()],
        None,
        &BTreeMap::new(),
        &[]
    )
    .is_err());
    assert!(plan_identities(
        &[],
        &["maker-alpha".into(), "maker-beta".into()],
        None,
        &BTreeMap::new(),
        &[]
    )
    .is_ok());
}

#[test]
fn native_identity_binding_is_scoped_immutable_and_does_not_invent_legacy_metadata() {
    let legacy = [names(&["alpha"], &["BTCUSDT", "DELISTEDUSDT"])];
    let plan = plan_identities(
        &legacy,
        &["alpha".into()],
        Some(&scope("mainnet")),
        &BTreeMap::from([("BTCUSDT".into(), "BTC".into())]),
        &[],
    )
    .unwrap();
    assert_eq!(
        plan.state.instruments[0].identity,
        InstrumentIdentity::Resolved(InstrumentKey::in_scope(&scope("mainnet"), "BTC"))
    );
    assert_eq!(
        plan.state.instruments[1].identity,
        InstrumentIdentity::Unresolved
    );
    let replay = [registry(plan.state.clone())];
    assert!(plan_identities(
        &replay,
        &["alpha".into()],
        Some(&scope("testnet")),
        &BTreeMap::new(),
        &[]
    )
    .is_err());
    assert!(plan_identities(
        &replay,
        &["alpha".into()],
        Some(&scope("mainnet")),
        &BTreeMap::from([("BTCUSDT".into(), "BTC-CHANGED".into())]),
        &[]
    )
    .is_err());
    assert!(plan_instrument(
        &plan.state,
        "ALIASUSDT",
        InstrumentKey::in_scope(&scope("mainnet"), "BTC")
    )
    .is_err());
    assert_eq!(
        plan_identities(
            &replay,
            &["alpha".into()],
            Some(&scope("mainnet")),
            &BTreeMap::new(),
            &["NEWUSDT".into()]
        )
        .err(),
        Some(IdentityError::UnresolvedInstrument("NEWUSDT".into()))
    );
}

#[test]
fn registry_rotation_replay_and_extension_preserve_every_existing_binding() {
    let plan = plan_identities(
        &[names(&["alpha", "beta"], &["BTCUSDT"])],
        &["beta".into(), "alpha".into()],
        Some(&scope("mainnet")),
        &BTreeMap::from([("BTCUSDT".into(), "BTC".into())]),
        &[],
    )
    .unwrap();
    let rotated = rotation(Some(&plan.state), &["alpha", "beta"], &["BTCUSDT"]);
    assert_eq!(
        replay_identities(std::slice::from_ref(&rotated)).unwrap(),
        Some(plan.state.clone())
    );
    let (extended, id) = plan_instrument(
        &plan.state,
        "ETHUSDT",
        InstrumentKey::in_scope(&scope("mainnet"), "ETH"),
    )
    .unwrap();
    assert_eq!(id, SymbolId(1));
    let replayed = replay_identities(&[
        rotated,
        registry(extended.clone()),
        names(&["alpha", "beta"], &["BTCUSDT", "ETHUSDT"]),
    ])
    .unwrap()
    .unwrap();
    assert_eq!(replayed, extended);
    let mut corrupt = extended.clone();
    corrupt.sleeves.swap(0, 1);
    assert!(replay_identities(&[registry(extended.clone()), registry(corrupt)]).is_err());
    assert!(replay_identities(&[
        registry(extended),
        rotation(None, &["alpha", "beta"], &["BTCUSDT", "ETHUSDT"])
    ])
    .is_err());
}

#[test]
fn instrument_id_exhaustion_is_typed_and_existing_ids_remain_usable() {
    let state = IdentityState {
        scope: Some(scope("mainnet")),
        instruments: (0..DENSE_ID_CAPACITY)
            .map(|index| InstrumentBinding {
                symbol: format!("S{index}USDT"),
                identity: InstrumentIdentity::Resolved(InstrumentKey::in_scope(
                    &scope("mainnet"),
                    format!("S{index}"),
                )),
            })
            .collect(),
        ..IdentityState::default()
    };
    let before = state.clone();
    assert_eq!(
        plan_instrument(
            &state,
            "OVERFLOWUSDT",
            InstrumentKey::in_scope(&scope("mainnet"), "OVERFLOW")
        )
        .unwrap_err(),
        IdentityError::SymbolIdsExhausted
    );
    assert_eq!(state, before);
    let (same, last) = plan_instrument(
        &state,
        "S65535USDT",
        InstrumentKey::in_scope(&scope("mainnet"), "S65535"),
    )
    .unwrap();
    assert_eq!(last, SymbolId(u16::MAX));
    assert_eq!(same, state);
}
