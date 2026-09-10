//! What must be true of the venue, the log and the engine when a run ends,
//! whatever was injected along the way.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{
    AccountView, OrderUpdate, Side, SignalObservation, Strategy, StrategyId, SymbolId,
    VenueExecution, VenueOrder, WalRecord,
};

use crate::backtest::venue::Accounting;

/// A batch published this close to the end of the tape has no time left to
/// become a decision: LONG's own entry window is `book_validity_ms` less
/// `engine_entry_cutoff_ms`, 45 minutes in every rendered config.
const ENTRY_WINDOW_MS: i64 = 45 * 60_000;

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct Check {
    pub name: &'static str,
    pub passed: bool,
    pub detail: String,
}

impl Check {
    fn pass(name: &'static str, detail: impl Into<String>) -> Self {
        Check {
            name,
            passed: true,
            detail: detail.into(),
        }
    }

    fn fail(name: &'static str, detail: impl Into<String>) -> Self {
        Check {
            name,
            passed: false,
            detail: detail.into(),
        }
    }

    fn judge(name: &'static str, problems: &[String], when_clean: impl Into<String>) -> Self {
        if problems.is_empty() {
            Check::pass(name, when_clean)
        } else {
            Check::fail(name, problems.join("; "))
        }
    }
}

/// Everything the judges read. `engine_*` is `None` when the last segment
/// ended in an error rather than a stop, so the engine's memory is gone.
pub struct Evidence<'a> {
    pub records: &'a [WalRecord],
    pub venue_view: &'a AccountView,
    pub venue_orders: &'a [VenueOrder],
    pub venue_executions: &'a [VenueExecution],
    pub venue_accounting: &'a Accounting,
    pub engine_in_flight: Option<&'a [String]>,
    pub engine_account: Option<&'a AccountView>,
    pub stopped_by: Option<&'a str>,
    pub engine_error: Option<&'a str>,
    /// Σ `net_usdt` over the round trips the log closes, as `engine fills`
    /// reads them; `None` when it closes none.
    pub ledger_net_usdt: Option<f64>,
    /// Every row the producer published, whether or not it was delivered.
    pub published: &'a [SignalObservation],
    /// The sleeves' own health when the loop stopped; `None` when it did not.
    pub strategy_health: Option<&'a [(String, String)]>,
    /// The sleeves rebuilt from the config the run booted with, in slot order.
    pub judged: &'a [(StrategyId, String, Box<dyn Strategy>)],
    pub long: Option<StrategyId>,
    pub carry: Option<StrategyId>,
    pub tape_end_ms: i64,
}

/// How many rows the log settled, either way.
#[derive(Clone, Copy, Debug, Default)]
pub struct SignalCounts {
    pub observed: u64,
    pub consumed: u64,
    pub rejected: u64,
}

pub fn signal_counts(records: &[WalRecord]) -> SignalCounts {
    let mut counts = SignalCounts::default();
    for record in records {
        match record {
            WalRecord::SignalObservation { .. } => counts.observed += 1,
            WalRecord::SignalObservationConsumed { .. } => counts.consumed += 1,
            WalRecord::SignalObservationRejected { .. } => counts.rejected += 1,
            _ => {}
        }
    }
    counts
}

/// Every refusal the engine wrote, by its stable code.
pub fn refusals_by_code(records: &[WalRecord]) -> BTreeMap<String, u64> {
    let mut out = BTreeMap::new();
    for record in records {
        if let WalRecord::IntentRefused { code, .. } = record {
            *out.entry(code.clone()).or_insert(0) += 1;
        }
    }
    out
}

/// Orders and fills charged to each configured sleeve name.
#[derive(Clone, Debug, Default)]
pub struct BySleeve {
    pub orders: BTreeMap<String, u64>,
    pub fills: BTreeMap<String, u64>,
}

pub fn by_sleeve(records: &[WalRecord], sleeves: &[String]) -> BySleeve {
    let mut out = BySleeve::default();
    let mut owner: BTreeMap<&str, usize> = BTreeMap::new();
    for record in records {
        match record {
            WalRecord::OrderSent { request, .. } => {
                let slot = request.strategy.0 as usize;
                owner.insert(request.client_order_id.as_str(), slot);
                if let Some(name) = sleeves.get(slot) {
                    *out.orders.entry(name.clone()).or_insert(0) += 1;
                }
            }
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        client_order_id, ..
                    },
                ..
            } => {
                if let Some(name) = owner
                    .get(client_order_id.as_str())
                    .and_then(|slot| sleeves.get(*slot))
                {
                    *out.fills.entry(name.clone()).or_insert(0) += 1;
                }
            }
            _ => {}
        }
    }
    out
}

pub fn all(e: &Evidence<'_>) -> Vec<Check> {
    vec![
        engine_ran_clean(e),
        stopped_by_feed_closed(e),
        positions_agree(e),
        every_fill_journaled(e),
        no_orphan_orders(e),
        cash_flow_agrees_when_flat(e),
        ledger_agrees_when_flat(e),
        numbers_finite(e),
        strategies_healthy(e),
        signals_consumed_exactly_once(e),
        checkpoint_identity_holds(e),
        sleeve_attribution_agrees(e),
        no_opening_before_readiness(e),
        working_entries_settled(e),
    ]
}

fn engine_ran_clean(e: &Evidence<'_>) -> Check {
    match e.engine_error {
        None => Check::pass("engine_ran_clean", "no boot or run error"),
        Some(error) => Check::fail("engine_ran_clean", error),
    }
}

fn stopped_by_feed_closed(e: &Evidence<'_>) -> Check {
    match e.stopped_by {
        Some("FeedClosed") => Check::pass("stopped_by_feed_closed", "the tape ended"),
        Some(other) => Check::fail("stopped_by_feed_closed", format!("stopped by {other}")),
        None => Check::fail("stopped_by_feed_closed", "the loop never stopped cleanly"),
    }
}

fn signed(side: Side, qty: f64) -> f64 {
    match side {
        Side::Buy => qty,
        Side::Sell => -qty,
    }
}

fn positions_agree(e: &Evidence<'_>) -> Check {
    let logged = match crate::reconcile::logged_exposure(e.records) {
        Ok(logged) => logged,
        Err(error) => {
            return Check::fail(
                "positions_agree",
                format!("the log's exposure is unreadable: {error}"),
            )
        }
    };
    let mut venue: BTreeMap<SymbolId, f64> = BTreeMap::new();
    for p in &e.venue_view.positions {
        *venue.entry(p.symbol).or_insert(0.0) += signed(p.side, p.qty);
    }
    let symbols: BTreeSet<SymbolId> = logged.keys().chain(venue.keys()).copied().collect();
    let mut problems = Vec::new();
    for symbol in &symbols {
        let log_qty = logged.get(symbol).copied().unwrap_or(0.0);
        let venue_qty = venue.get(symbol).copied().unwrap_or(0.0);
        if (log_qty - venue_qty).abs() > 1e-9 {
            problems.push(format!(
                "symbol {}: log {log_qty} venue {venue_qty}",
                symbol.0
            ));
        }
    }
    Check::judge(
        "positions_agree",
        &problems,
        format!("{} symbols compared", symbols.len()),
    )
}

fn wal_exec_ids(records: &[WalRecord]) -> BTreeSet<&str> {
    records
        .iter()
        .filter_map(|record| match record {
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill { exec_id, .. },
                ..
            }
            | WalRecord::OrderUpdate {
                update: OrderUpdate::FastFill { exec_id, .. },
                ..
            }
            | WalRecord::Retained(engine_types::wal::RetainedWalRecord::FastExecution {
                exec_id,
                ..
            })
            | WalRecord::RecoveredFill { exec_id, .. } => Some(exec_id.as_str()),
            _ => None,
        })
        .collect()
}

fn every_fill_journaled(e: &Evidence<'_>) -> Check {
    let venue: BTreeSet<&str> = e
        .venue_executions
        .iter()
        .map(|x| x.exec_id.as_str())
        .collect();
    let logged = wal_exec_ids(e.records);
    let mut problems = Vec::new();
    let missing: Vec<&str> = venue.difference(&logged).copied().collect();
    if !missing.is_empty() {
        problems.push(format!(
            "{} venue fills never reached the log: {}",
            missing.len(),
            missing
                .iter()
                .take(5)
                .copied()
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    let invented: Vec<&str> = logged.difference(&venue).copied().collect();
    if !invented.is_empty() {
        problems.push(format!(
            "{} logged fills the venue never made: {}",
            invented.len(),
            invented
                .iter()
                .take(5)
                .copied()
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    Check::judge(
        "every_fill_journaled",
        &problems,
        format!("{} executions, all in the log", venue.len()),
    )
}

fn no_orphan_orders(e: &Evidence<'_>) -> Check {
    let Some(in_flight) = e.engine_in_flight else {
        return Check::pass(
            "no_orphan_orders",
            "not judged: the engine did not stop cleanly",
        );
    };
    let known: BTreeSet<&str> = in_flight.iter().map(String::as_str).collect();
    let orphans: Vec<&str> = e
        .venue_orders
        .iter()
        .map(|o| o.client_order_id.as_str())
        .filter(|id| !known.contains(id))
        .collect();
    let problems = if orphans.is_empty() {
        Vec::new()
    } else {
        vec![format!(
            "{} orders working at the venue that the engine does not hold: {}",
            orphans.len(),
            orphans
                .iter()
                .take(5)
                .copied()
                .collect::<Vec<_>>()
                .join(", ")
        )]
    };
    Check::judge(
        "no_orphan_orders",
        &problems,
        format!(
            "{} working at the venue, {} in flight in the engine",
            e.venue_orders.len(),
            in_flight.len()
        ),
    )
}

/// Every fill in the log, once each, as money: sells in, buys out, fees
/// out. With no position left, that sum is the venue's realized P&L net of
/// every fee, whatever was dropped, repeated or recovered on the way.
fn cash_flow_agrees_when_flat(e: &Evidence<'_>) -> Check {
    let name = "cash_flow_agrees_when_flat";
    if e.venue_accounting.open_positions != 0 {
        return Check::pass(
            name,
            format!(
                "not judged: {} positions open at the end",
                e.venue_accounting.open_positions
            ),
        );
    }
    // Per execution id, preferring a record that states the fee.
    let mut fills: BTreeMap<&str, (Side, f64, f64, Option<f64>)> = BTreeMap::new();
    for record in e.records {
        let (exec_id, row) = match record {
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        exec_id,
                        side,
                        qty,
                        px,
                        fee,
                        ..
                    },
                ..
            }
            | WalRecord::RecoveredFill {
                exec_id,
                side,
                qty,
                px,
                fee,
                ..
            } => (exec_id.as_str(), (*side, *qty, *px, *fee)),
            WalRecord::Retained(engine_types::wal::RetainedWalRecord::FastExecution {
                exec_id,
                side,
                qty,
                px,
                ..
            })
            | WalRecord::OrderUpdate {
                update:
                    OrderUpdate::FastFill {
                        exec_id,
                        side,
                        qty,
                        px,
                        ..
                    },
                ..
            } => (exec_id.as_str(), (*side, *qty, *px, None)),
            _ => continue,
        };
        match fills.get(exec_id) {
            Some((_, _, _, Some(_))) => {}
            _ => {
                fills.insert(exec_id, row);
            }
        }
    }
    let mut engine = 0.0;
    let mut without_fee = 0usize;
    for (side, qty, px, fee) in fills.values() {
        engine += signed(side.flipped(), qty * px);
        match fee {
            Some(fee) => engine -= fee,
            None => without_fee += 1,
        }
    }
    if without_fee > 0 {
        return Check::pass(
            name,
            format!("not judged: {without_fee} logged fills state no fee"),
        );
    }
    let a = e.venue_accounting;
    let venue = a.realized_pnl_usdt - a.fees_paid_usdt;
    let difference = engine - venue;
    let tolerance = 1e-6 * venue.abs().max(1.0);
    if difference.abs() <= tolerance {
        Check::pass(
            name,
            format!("{} fills; engine {engine:.6} venue {venue:.6}", fills.len()),
        )
    } else {
        Check::fail(
            name,
            format!(
                "{} fills; engine {engine:.6} venue {venue:.6} differ by {difference:.6}",
                fills.len()
            ),
        )
    }
}

fn ledger_agrees_when_flat(e: &Evidence<'_>) -> Check {
    let name = "ledger_agrees_when_flat";
    if e.venue_accounting.open_positions != 0 {
        return Check::pass(
            name,
            format!(
                "not judged: {} positions open at the end",
                e.venue_accounting.open_positions
            ),
        );
    }
    let Some(engine_net) = e.ledger_net_usdt else {
        return Check::pass(name, "not judged: the log closes no round trip");
    };
    let a = e.venue_accounting;
    let venue_net = a.realized_pnl_usdt - (a.fees_paid_usdt - a.open_entry_fees_usdt);
    let difference = engine_net - venue_net;
    let tolerance = 1e-6 * engine_net.abs().max(1.0);
    if difference.abs() <= tolerance {
        Check::pass(name, format!("engine {engine_net:.6} venue {venue_net:.6}"))
    } else {
        Check::fail(
            name,
            format!("engine {engine_net:.6} venue {venue_net:.6} differ by {difference:.6}"),
        )
    }
}

fn numbers_finite(e: &Evidence<'_>) -> Check {
    let a = e.venue_accounting;
    let mut problems = Vec::new();
    for (name, value) in [
        ("cash", a.cash_usdt),
        ("realized", a.realized_pnl_usdt),
        ("fees", a.fees_paid_usdt),
        ("funding", a.funding_paid_usdt),
        ("unrealized", a.unrealized_usdt),
        ("equity", a.equity_usdt),
    ] {
        if !value.is_finite() {
            problems.push(format!("venue {name} is {value}"));
        }
    }
    if let Some(account) = e.engine_account {
        for (name, value) in [
            ("equity", account.equity_usdt),
            ("available", account.available_usdt),
        ] {
            if !value.is_finite() {
                problems.push(format!("engine account {name} is {value}"));
            }
        }
        for p in &account.positions {
            if !p.qty.is_finite() || !p.entry_px.is_finite() {
                problems.push(format!(
                    "engine position {} has a non-finite figure",
                    p.symbol.0
                ));
            }
        }
    }
    Check::judge("numbers_finite", &problems, "every figure is finite")
}

/// No sleeve is reporting a health error, and no callback fault is latched.
///
/// A LONG or CARRY reducer that refuses one of its own producer's rows sets
/// exactly this, so a producer whose envelope drifts from the rendered config
/// fails here rather than quietly trading nothing.
fn strategies_healthy(e: &Evidence<'_>) -> Check {
    let name = "strategies_healthy";
    let Some(health) = e.strategy_health else {
        return Check::pass(name, "not judged: the engine did not stop cleanly");
    };
    let mut problems: Vec<String> = health
        .iter()
        .map(|(sleeve, error)| format!("{sleeve}: {error}"))
        .collect();
    // A latched callback fault refuses every later opening with this code;
    // the record is the log's own statement of it.
    if let Some((sleeve, tag)) = e.records.iter().find_map(|record| match record {
        WalRecord::IntentRefused {
            code,
            strategy,
            tag,
            ..
        } if code == "strategy_callback_unavailable" => Some((
            e.judged
                .iter()
                .find(|(id, _, _)| id == strategy)
                .map_or_else(|| strategy.0.to_string(), |(_, name, _)| name.clone()),
            tag.clone(),
        )),
        _ => None,
    }) {
        problems.push(format!(
            "a callback fault reached the log: {sleeve} refused {tag} with strategy_callback_unavailable"
        ));
    }
    Check::judge(
        name,
        &problems,
        format!("{} sleeves, none reporting an error", e.judged.len()),
    )
}

/// Every row the producer published in time to matter reached the log once and
/// was settled once, and no gap the engine recorded is still open.
fn signals_consumed_exactly_once(e: &Evidence<'_>) -> Check {
    let name = "signals_consumed_exactly_once";
    if e.published.is_empty() {
        return Check::pass(name, "not judged: no producer");
    }
    let mut observed: BTreeMap<(&str, u64), usize> = BTreeMap::new();
    let mut settled: BTreeMap<(&str, u64), usize> = BTreeMap::new();
    let mut last_gap: BTreeMap<&str, u64> = BTreeMap::new();
    for record in e.records {
        match record {
            WalRecord::SignalObservation { observation, .. } => {
                *observed
                    .entry((observation.source.as_str(), observation.sequence))
                    .or_insert(0) += 1;
            }
            WalRecord::SignalObservationConsumed {
                source, sequence, ..
            }
            | WalRecord::SignalObservationRejected {
                source, sequence, ..
            } => {
                *settled.entry((source.as_str(), *sequence)).or_insert(0) += 1;
            }
            WalRecord::SignalGapRecorded { gap, .. } => {
                last_gap.insert(gap.source.as_str(), gap.next_sequence);
            }
            _ => {}
        }
    }
    let deadline = e.tape_end_ms - ENTRY_WINDOW_MS;
    let mut due = 0usize;
    let mut problems = Vec::new();
    for row in e
        .published
        .iter()
        .filter(|row| row.available_wall_ts_ms <= deadline)
    {
        due += 1;
        let key = (row.source.as_str(), row.sequence);
        match observed.get(&key).copied().unwrap_or(0) {
            1 => {}
            0 => problems.push(format!("{} {} never reached the log", key.0, key.1)),
            n => problems.push(format!("{} {} is in the log {n} times", key.0, key.1)),
        }
        match settled.get(&key).copied().unwrap_or(0) {
            1 => {}
            0 => problems.push(format!("{} {} was never settled", key.0, key.1)),
            n => problems.push(format!("{} {} was settled {n} times", key.0, key.1)),
        }
    }
    for key in settled.keys() {
        if !observed.contains_key(key) {
            problems.push(format!(
                "{} {} was settled without ever being recorded",
                key.0, key.1
            ));
        }
    }
    for (source, next_sequence) in &last_gap {
        if !observed.contains_key(&(source, *next_sequence)) {
            problems.push(format!(
                "{source} is still missing {next_sequence} at the end"
            ));
        }
    }
    Check::judge(
        name,
        &problems,
        format!(
            "{due} of {} published rows were due; {} observed, {} settled, {} gaps recorded",
            e.published.len(),
            observed.len(),
            settled.len(),
            last_gap.len()
        ),
    )
}

/// The newest durable state each sleeve wrote is state that sleeve accepts,
/// and no boot wrote its initial checkpoint over what an earlier one had.
fn checkpoint_identity_holds(e: &Evidence<'_>) -> Check {
    let name = "checkpoint_identity_holds";
    if e.judged.is_empty() {
        return Check::pass(name, "not judged: no strategies");
    }
    let mut newest: BTreeMap<StrategyId, &engine_types::StrategyCheckpoint> = BTreeMap::new();
    let mut initial_writes: BTreeMap<StrategyId, usize> = BTreeMap::new();
    let initial: BTreeMap<StrategyId, Option<engine_types::StrategyCheckpoint>> = e
        .judged
        .iter()
        .map(|(id, _, strategy)| (*id, strategy.initial_checkpoint()))
        .collect();
    for record in e.records {
        let WalRecord::StrategyGlobalCheckpoint {
            strategy,
            checkpoint,
            ..
        } = record
        else {
            continue;
        };
        newest.insert(*strategy, checkpoint);
        if initial
            .get(strategy)
            .and_then(Option::as_ref)
            .is_some_and(|first| first == checkpoint)
        {
            *initial_writes.entry(*strategy).or_insert(0) += 1;
        }
    }
    let mut problems = Vec::new();
    for (id, sleeve, strategy) in e.judged {
        if let Some(checkpoint) = newest.get(id) {
            if let Err(error) = strategy.validate_checkpoint(checkpoint) {
                problems.push(format!("{sleeve}: {error}"));
            }
            if let Some(identity) = strategy.checkpoint_identity() {
                if identity.decision_fingerprint != checkpoint.decision_fingerprint {
                    problems.push(format!(
                        "{sleeve}: the log's newest checkpoint is fingerprinted {} against the block's {}",
                        checkpoint.decision_fingerprint, identity.decision_fingerprint
                    ));
                }
            }
        }
        let writes = initial_writes.get(id).copied().unwrap_or(0);
        if writes > 1 {
            problems.push(format!(
                "{sleeve}: a boot wrote the initial checkpoint again ({writes} times in the log)"
            ));
        }
    }
    Check::judge(
        name,
        &problems,
        format!("{} sleeve checkpoints validated", newest.len()),
    )
}

/// Per symbol, the sleeves' own inventories add up to the venue's position:
/// `positions_agree` for a log that has more than one owner in it.
fn sleeve_attribution_agrees(e: &Evidence<'_>) -> Check {
    let name = "sleeve_attribution_agrees";
    let attribution = match engine_core::attribution::Attribution::try_from_records(e.records) {
        Ok(attribution) => attribution,
        Err(error) => {
            return Check::fail(
                name,
                format!("the log's attribution is unreadable: {error}"),
            )
        }
    };
    let mut owned: BTreeMap<SymbolId, f64> = BTreeMap::new();
    let mut sleeves: BTreeSet<StrategyId> = BTreeSet::new();
    for (strategy, symbol, signed_qty) in attribution.rows() {
        sleeves.insert(strategy);
        *owned.entry(symbol).or_insert(0.0) += signed_qty;
    }
    let mut venue: BTreeMap<SymbolId, f64> = BTreeMap::new();
    for p in &e.venue_view.positions {
        *venue.entry(p.symbol).or_insert(0.0) += signed(p.side, p.qty);
    }
    let symbols: BTreeSet<SymbolId> = owned.keys().chain(venue.keys()).copied().collect();
    let mut problems = Vec::new();
    for symbol in &symbols {
        let sleeve_qty = owned.get(symbol).copied().unwrap_or(0.0);
        let venue_qty = venue.get(symbol).copied().unwrap_or(0.0);
        if (sleeve_qty - venue_qty).abs() > 1e-9 {
            problems.push(format!(
                "symbol {}: sleeves {sleeve_qty} venue {venue_qty}",
                symbol.0
            ));
        }
    }
    Check::judge(
        name,
        &problems,
        format!("{} sleeves over {} symbols", sleeves.len(), symbols.len()),
    )
}

/// LONG sent nothing before it had durably taken one of its producer's rows.
fn no_opening_before_readiness(e: &Evidence<'_>) -> Check {
    let name = "no_opening_before_readiness";
    let Some(long) = e.long else {
        return Check::pass(name, "not judged: no producer");
    };
    let mut consumed = false;
    let mut problems = Vec::new();
    for record in e.records {
        match record {
            WalRecord::SignalObservationConsumed { source, .. }
                if engine_types::ManagedSignalSource::parse(source)
                    .is_some_and(|id| id.lane == engine_types::SignalLane::Long) =>
            {
                consumed = true;
            }
            WalRecord::OrderSent { request, .. }
                if request.strategy == long && !consumed && !request.reduce_only =>
            {
                problems.push(format!(
                    "{} left before LONG had consumed a row",
                    request.client_order_id
                ));
            }
            _ => {}
        }
    }
    Check::judge(
        name,
        &problems,
        if consumed {
            "LONG consumed a row before it sent anything".to_string()
        } else {
            "LONG never consumed a row and never sent one".to_string()
        },
    )
}

/// Every worked LONG entry ended: filled, cancelled or refused in the log, or
/// still in the engine's own in-flight ledger when the tape ran out.
fn working_entries_settled(e: &Evidence<'_>) -> Check {
    let name = "working_entries_settled";
    let Some(long) = e.long else {
        return Check::pass(name, "not judged: no producer");
    };
    let Some(in_flight) = e.engine_in_flight else {
        return Check::pass(name, "not judged: the engine did not stop cleanly");
    };
    let mut ledger = engine_core::inflight::LedgerOfOrders::default();
    for record in e.records {
        if let Err(error) = ledger.try_apply(record) {
            return Check::fail(
                name,
                format!("the log's order ledger is unreadable: {error}"),
            );
        }
    }
    let known: BTreeSet<&str> = in_flight.iter().map(String::as_str).collect();
    let mut worked = 0usize;
    let mut problems = Vec::new();
    for (id, order) in &ledger.orders {
        if order.request.strategy != long || order.request.reduce_only || order.entry_work.is_none()
        {
            continue;
        }
        worked += 1;
        if order.ending.is_none() && !known.contains(id.as_str()) {
            problems.push(format!(
                "{id} is neither terminal in the log nor in flight in the engine"
            ));
        }
    }
    Check::judge(name, &problems, format!("{worked} worked LONG entries"))
}
