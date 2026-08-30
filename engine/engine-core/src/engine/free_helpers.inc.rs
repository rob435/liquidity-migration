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

/// Active target-book latches after replaying the WAL in order.
/// A segment base is a restatement; edge records after it amend that state.
fn replay_target_book_latches(replayed: &[WalRecord]) -> std::collections::BTreeSet<(u16, u16)> {
    let mut active = std::collections::BTreeSet::new();
    for record in replayed {
        match record {
            WalRecord::TargetBookLatch {
                strategy,
                symbol,
                latched,
                ..
            } => {
                let key = (strategy.0, symbol.0);
                if *latched {
                    active.insert(key);
                } else {
                    active.remove(&key);
                }
            }
            WalRecord::SegmentBase {
                target_book_latches,
                ..
            } => {
                active = target_book_latches
                    .iter()
                    .map(|row| (row.strategy.0, row.symbol.0))
                    .collect();
            }
            _ => {}
        }
    }
    active
}

/// The target-book sleeve whose venue-native position stop just traded.
///
/// A blank client id is the typed `OrderUpdate` contract for a native
/// position stop. The fill stays foreign to attribution, but an opposite-side
/// execution against one sleeve's claim means that sleeve must not refill the
/// target while the whole-position stop is completing.
fn target_book_stop_owner(
    attribution: &Attribution,
    target_book_strategies: &std::collections::HashSet<u16>,
    client_order_id: &str,
    symbol: SymbolId,
    side: Side,
) -> Option<StrategyId> {
    if !client_order_id.is_empty() {
        return None;
    }
    let owner = attribution.sole_owner(symbol)?;
    if !target_book_strategies.contains(&owner.0) {
        return None;
    }
    let claimed = attribution.signed(owner, symbol);
    match (claimed > 0.0, claimed < 0.0, side) {
        (true, false, Side::Sell) | (false, true, Side::Buy) => Some(owner),
        _ => None,
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
/// strategy reports because target-book followers put kernel refusals before
/// weaker planner skips.
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
