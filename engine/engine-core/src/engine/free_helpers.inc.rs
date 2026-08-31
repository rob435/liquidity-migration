fn stop_key(symbol: SymbolId, side: Side) -> (u16, bool) {
    (symbol.0, side == Side::Sell)
}

fn tighter_stop(side: Side, left: f64, right: f64) -> f64 {
    match side {
        Side::Buy => left.max(right),
        Side::Sell => left.min(right),
    }
}

fn stop_is_looser(side: Side, candidate: f64, protected: f64, tolerance: f64) -> bool {
    match side {
        Side::Buy => candidate + tolerance < protected,
        Side::Sell => candidate - tolerance > protected,
    }
}

/// The newest boundary a successful execution-history read proved. No generic
/// wall stamp belongs here: a fill, rotation, or graceful stop does not prove
/// that an otherwise empty interval was scanned.
fn execution_history_through_ms(replayed: &[WalRecord]) -> Option<i64> {
    let mut newest = None;
    for record in replayed {
        let stamp = match record {
            WalRecord::ExecutionHistoryCheckpoint { through_wall_ts_ms } => {
                Some(*through_wall_ts_ms)
            }
            WalRecord::SegmentBase {
                execution_history_through_ms,
                ..
            } => *execution_history_through_ms,
            _ => None,
        };
        if let Some(stamp) = stamp {
            if newest.is_none_or(|n| stamp > n) {
                newest = Some(stamp);
            }
        }
    }
    newest.or_else(|| legacy_boot_ms(replayed))
}

/// Older WALs used their boot stamp as the recovery boundary. Keep that one
/// compatibility path, but never promote a later reconciliation, rotation,
/// fill, or shutdown stamp into a history proof.
fn legacy_boot_ms(replayed: &[WalRecord]) -> Option<i64> {
    let mut newest = None;
    for record in replayed {
        if let WalRecord::Boot { wall_ts_ms, .. } = record {
            if newest.is_none_or(|known| *wall_ts_ms > known) {
                newest = Some(*wall_ts_ms);
            }
        }
    }
    newest
}

/// Newest strategy checkpoint after replaying records in order.
/// A segment base is a complete restatement; later records replace one key.
fn replay_strategy_checkpoints(
    replayed: &[WalRecord],
) -> std::collections::BTreeMap<(u16, u16), StrategyCheckpoint> {
    let mut active = std::collections::BTreeMap::new();
    for record in replayed {
        match record {
            WalRecord::StrategyCheckpoint {
                strategy,
                symbol,
                checkpoint,
                ..
            } => {
                active.insert((strategy.0, symbol.0), checkpoint.clone());
            }
            WalRecord::SegmentBase {
                strategy_checkpoints,
                ..
            } => {
                active = strategy_checkpoints
                    .iter()
                    .map(|row| ((row.strategy.0, row.symbol.0), row.checkpoint.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    active
}

/// Newest whole-sleeve checkpoint after replaying records in order.
fn replay_strategy_global_checkpoints(
    replayed: &[WalRecord],
) -> std::collections::BTreeMap<u16, StrategyGlobalCheckpointState> {
    let mut active = std::collections::BTreeMap::new();
    for record in replayed {
        match record {
            WalRecord::StrategyGlobalCheckpoint {
                strategy,
                checkpoint,
                provenance,
                ..
            } => {
                active.insert(
                    strategy.0,
                    StrategyGlobalCheckpointState {
                        strategy: *strategy,
                        checkpoint: checkpoint.clone(),
                        provenance: provenance.clone(),
                    },
                );
            }
            WalRecord::SegmentBase {
                strategy_global_checkpoints,
                ..
            } => {
                active = strategy_global_checkpoints
                    .iter()
                    .map(|row| (row.strategy.0, row.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    active
}

/// Durable cross-sleeve events still awaiting their destination.
fn replay_strategy_events(
    replayed: &[WalRecord],
) -> std::collections::BTreeMap<(u16, String), StrategyEvent> {
    let mut active = std::collections::BTreeMap::new();
    for record in replayed {
        match record {
            WalRecord::StrategyEventPublished { event, .. } => {
                active.insert((event.source.0, event.event_id.clone()), event.clone());
            }
            WalRecord::StrategyEventConsumed {
                source, event_id, ..
            } => {
                active.remove(&(source.0, event_id.clone()));
            }
            WalRecord::SegmentBase {
                strategy_events, ..
            } => {
                active = strategy_events
                    .iter()
                    .map(|event| ((event.source.0, event.event_id.clone()), event.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    active
}

/// Durable external observations still awaiting their strategy, and each
/// source's contiguous accepted sequence.
struct ReplayedSignalState {
    observations: std::collections::BTreeMap<(String, u64), SignalObservation>,
    cursors: std::collections::BTreeMap<String, SignalCursor>,
    subscriptions: std::collections::BTreeMap<(String, u16), SignalSubscriptionState>,
}

struct ReplayedRuntimeControlState {
    requests: Vec<engine_types::RuntimeControlRequest>,
    consumed: std::collections::BTreeSet<(u16, String)>,
    entries_enabled: std::collections::BTreeMap<u16, bool>,
}

fn replay_runtime_control_state(
    replayed: &[WalRecord],
) -> Result<ReplayedRuntimeControlState, EngineError> {
    let mut requests: Vec<engine_types::RuntimeControlRequest> = Vec::new();
    let mut consumed = std::collections::BTreeSet::new();
    for record in replayed {
        match record {
            WalRecord::RuntimeControlAccepted { request, .. } => {
                if let Some(known) = requests.iter().find(|known| {
                    known.strategy == request.strategy && known.request_id == request.request_id
                }) {
                    if known != request {
                        return Err(EngineError::Boot(format!(
                            "strategy {} runtime request id {:?} has conflicting bytes",
                            request.strategy.0, request.request_id
                        )));
                    }
                } else {
                    requests.push(request.clone());
                }
            }
            WalRecord::RuntimeControlConsumed {
                strategy,
                request_id,
                ..
            } => {
                consumed.insert((strategy.0, request_id.clone()));
            }
            WalRecord::SegmentBase {
                runtime_control_requests,
                runtime_control_consumed,
                ..
            } => {
                requests = runtime_control_requests.clone();
                consumed = runtime_control_consumed
                    .iter()
                    .map(|(strategy, request_id)| (strategy.0, request_id.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    let mut entries_enabled = std::collections::BTreeMap::new();
    for request in &requests {
        if let engine_types::RuntimeControlCommand::SetEntriesEnabled { entries_enabled: value } =
            request.command
        {
            entries_enabled.insert(request.strategy.0, value);
        }
    }
    Ok(ReplayedRuntimeControlState {
        requests,
        consumed,
        entries_enabled,
    })
}

fn replay_signal_state(replayed: &[WalRecord]) -> ReplayedSignalState {
    let mut active = std::collections::BTreeMap::new();
    let mut cursors = std::collections::BTreeMap::new();
    let mut subscriptions = std::collections::BTreeMap::new();
    for record in replayed {
        match record {
            WalRecord::SignalObservation { observation, .. } => {
                active.insert(
                    (observation.source.clone(), observation.sequence),
                    observation.clone(),
                );
                cursors.insert(
                    observation.source.clone(),
                    SignalCursor {
                        source: observation.source.clone(),
                        sequence: observation.sequence,
                        content_sha256: observation.content_sha256.clone(),
                    },
                );
                let row = subscriptions
                    .entry((observation.source.clone(), observation.destination.0))
                    .or_insert_with(|| SignalSubscriptionState {
                        source: observation.source.clone(),
                        destination: observation.destination,
                        subscriptions: Vec::new(),
                    });
                for subscription in &observation.subscriptions {
                    if !row.subscriptions.contains(subscription) {
                        row.subscriptions.push(subscription.clone());
                    }
                }
            }
            WalRecord::SignalObservationConsumed {
                source, sequence, ..
            } => {
                active.remove(&(source.clone(), *sequence));
            }
            WalRecord::SegmentBase {
                signal_observations,
                signal_cursors,
                signal_subscriptions,
                ..
            } => {
                active = signal_observations
                    .iter()
                    .map(|observation| {
                        (
                            (observation.source.clone(), observation.sequence),
                            observation.clone(),
                        )
                    })
                    .collect();
                cursors = signal_cursors
                    .iter()
                    .map(|cursor| (cursor.source.clone(), cursor.clone()))
                    .collect();
                subscriptions = signal_subscriptions
                    .iter()
                    .map(|row| ((row.source.clone(), row.destination.0), row.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    ReplayedSignalState {
        observations: active,
        cursors,
        subscriptions,
    }
}

pub(crate) fn venue_minus_local_ms(
    venue_ts_ms: i64,
    recv_ns: u64,
    now_ns: u64,
    wall_ts_ms: i64,
) -> i64 {
    let age_ms = (now_ns.saturating_sub(recv_ns) / 1_000_000) as i64;
    venue_ts_ms - (wall_ts_ms - age_ms)
}

/// Entry blockers with the configured sleeve name that owns each one.
///
/// A symbol is not a unique key on a multi-sleeve account. Deduplication is
/// deliberately per (strategy, symbol), preserving the first reason each
/// strategy reports because native sleeves put kernel refusals before weaker
/// planner skips.
pub(crate) fn named_entry_blockers(
    strategies: &[Box<dyn Strategy>],
    names: &[String],
) -> Vec<(String, String, String)> {
    let mut blockers: Vec<(String, String, String)> = Vec::new();
    for (index, strategy) in strategies.iter().enumerate() {
        let Some(strategy_name) = names.get(index) else {
            tracing::error!(
                index,
                "strategy has no configured name; omitting its entry blockers"
            );
            continue;
        };
        for (symbol, reason) in strategy.entry_blockers() {
            if !blockers.iter().any(|(seen_strategy, seen_symbol, _)| {
                seen_strategy == strategy_name && seen_symbol == &symbol
            }) {
                blockers.push((strategy_name.clone(), symbol, reason));
            }
        }
    }
    blockers.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    blockers
}

/// Current strategy-level faults with the configured sleeve that owns each
/// one. These are kept out of entry blockers so an expected per-symbol skip
/// cannot be mistaken for a broken reducer by the fleet watchdog.
pub(crate) fn named_strategy_errors(
    strategies: &[Box<dyn Strategy>],
    names: &[String],
) -> Vec<(String, String)> {
    let mut errors = Vec::new();
    for (index, strategy) in strategies.iter().enumerate() {
        let Some(error) = strategy.health_error() else {
            continue;
        };
        let Some(strategy_name) = names.get(index) else {
            tracing::error!(index, "strategy has no configured name; omitting its health error");
            continue;
        };
        errors.push((strategy_name.clone(), error.to_owned()));
    }
    errors.sort_by(|a, b| a.0.cmp(&b.0));
    errors
}

#[allow(clippy::too_many_arguments)]
fn feed_strategy(
    strategies: &mut [Box<dyn Strategy>],
    market: &MarketState,
    account: &AccountView,
    rules: &[Option<InstrumentRule>],
    timers: &mut Timers,
    pending: &mut VecDeque<Action>,
    orders: &LedgerOfOrders,
    registry: &OrderRegistry,
    attribution: &Attribution,
    covers: &CoverBook,
    checkpoints: &std::collections::BTreeMap<(u16, u16), StrategyCheckpoint>,
    global_checkpoints: &std::collections::BTreeMap<u16, StrategyGlobalCheckpointState>,
    strategy_events: &std::collections::BTreeMap<(u16, String), StrategyEvent>,
    strategy_names: &[String],
    runtime_entries_enabled: &std::collections::BTreeMap<u16, bool>,
    sid: StrategyId,
    event: &EngineEvent,
    now_ns: u64,
) {
    let Some(strategy) = strategies.get_mut(sid.0 as usize) else {
        return;
    };
    let mut ctx = Ctx {
        market,
        account,
        rules,
        now_ns,
        strategy: sid,
        out: pending,
        timers,
        orders,
        registry,
        attribution,
        covers,
        checkpoints,
        global_checkpoints,
        strategy_events,
        strategy_names,
        runtime_entries_enabled: runtime_entries_enabled.get(&sid.0).copied(),
    };
    strategy.on_event(event, &mut ctx);
}

/// Drop the remembered leverage of every symbol the reading shows flat.
///
/// A symbol with no position may be reopened at any leverage by anyone holding
/// a key, and the owner trades the funded account by hand. Remembering what we
/// last set it to would then be remembering something that is no longer true,
/// and the next entry would skip the call that would have corrected it.
///
/// A symbol still open keeps its entry: its leverage cannot be changed at the
/// venue while a position is on it.
pub(crate) fn forget_leverage_where_flat(
    leverage_at: &mut std::collections::HashMap<SymbolId, f64>,
    positions: &[engine_types::risk::PositionView],
) {
    leverage_at.retain(|symbol, _| positions.iter().any(|p| p.symbol == *symbol));
}

/// When this message reached us. The whole chain is measured from here.
/// Mint the next client order id, skipping any the log already knows: the
/// boot prefix comes from a wall clock, and a clock stepped back must not
/// let a new order overwrite a recovered one's ledger entry.
pub(crate) fn mint_unused(prefix: &str, next_n: &mut u64, taken: impl Fn(&str) -> bool) -> String {
    loop {
        *next_n += 1;
        let id = format!("{prefix}{next_n}");
        assert!(id.len() <= 36, "client order id too long: {id}");
        if !taken(&id) {
            return id;
        }
    }
}

/// The first non-finite number an intent carries, named, or None.
fn unreal_number(intent: &Intent) -> Option<&'static str> {
    if !intent.qty.is_finite() {
        return Some("quantity");
    }
    if let OrderKind::Limit { px, .. } = intent.kind {
        if !px.is_finite() {
            return Some("limit price");
        }
    }
    if let Some(stop) = intent.stop {
        if !stop.trigger_px.is_finite() {
            return Some("stop price");
        }
    }
    None
}

fn arrival_ns(event: &MarketEvent, fallback: u64) -> u64 {
    let stamp = match event {
        MarketEvent::Quote { quote, .. } => quote.recv_ns,
        MarketEvent::Depth { depth, .. } => depth.recv_ns,
        MarketEvent::Trades { trades, .. } => trades.recv_ns,
        MarketEvent::Ticker { ticker, .. } => ticker.recv_ns,
        MarketEvent::FeedReset { recv_ns } => *recv_ns,
    };
    if stamp == 0 {
        fallback
    } else {
        stamp
    }
}

/// Return a verdict that can be written and that cannot enlarge the request.
/// `serde_json` represents non-finite floats as `null`; round-tripping catches
/// them in every current and future denial field before they poison replay.
pub(crate) fn durable_risk_verdict(
    verdict: RiskVerdict,
    requested_qty: f64,
    exact_qty: bool,
) -> RiskVerdict {
    let valid = match &verdict {
        RiskVerdict::Allow { qty } => {
            requested_qty.is_finite()
                && requested_qty > 0.0
                && qty.is_finite()
                && *qty > 0.0
                && if exact_qty {
                    *qty == requested_qty
                } else {
                    *qty <= requested_qty
                }
        }
        RiskVerdict::Deny { .. } => serde_json::to_vec(&verdict)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<RiskVerdict>(&bytes).ok())
            .is_some(),
    };
    if valid {
        verdict
    } else {
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState {
                detail: "risk kernel returned a non-durable or invalid quantity verdict"
                    .to_string(),
            },
        }
    }
}

/// Both id tables as a log record, so every number in the log can be turned
/// back into a sleeve and a coin.
fn names_record(strategies: &[String], market: &MarketState) -> WalRecord {
    WalRecord::Names {
        strategies: strategies.to_vec(),
        symbols: (0..market.table.len())
            .map(|i| market.table.name(SymbolId(i as u16)).to_string())
            .collect(),
    }
}
fn validate_strategy_checkpoint(
    strategy: &dyn Strategy,
    checkpoint: &StrategyCheckpoint,
) -> Result<(), String> {
    if checkpoint.schema_version == 0 {
        return Err("checkpoint schema_version must be positive".to_string());
    }
    if checkpoint.decision_fingerprint.is_empty()
        || checkpoint.decision_fingerprint.len() > 256
    {
        return Err("checkpoint fingerprint must contain 1..=256 bytes".to_string());
    }
    if checkpoint.payload.len() > engine_types::MAX_STRATEGY_STATE_BYTES {
        return Err(format!(
            "checkpoint is {} bytes; maximum is {}",
            checkpoint.payload.len(),
            engine_types::MAX_STRATEGY_STATE_BYTES
        ));
    }
    if let Some(identity) = strategy.checkpoint_identity() {
        if checkpoint.schema_version != identity.schema_version
            || checkpoint.decision_fingerprint != identity.decision_fingerprint
        {
            return Err(format!(
                "checkpoint identity ({}, {:?}) does not match configured ({}, {:?})",
                checkpoint.schema_version,
                checkpoint.decision_fingerprint,
                identity.schema_version,
                identity.decision_fingerprint
            ));
        }
    }
    strategy.validate_checkpoint(checkpoint)
}
