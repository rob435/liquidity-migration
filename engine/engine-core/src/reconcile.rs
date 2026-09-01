//! What the log believes, against what the venue says.
//!
//! The engine's log is a perfect record of what this engine did. It is not a
//! record of what happened to the account: the venue keeps trading while the
//! engine is stopped, a fill can land in the gap, somebody can place an order
//! by hand, and another process can hold a position in the same symbol. Boot
//! is the one moment those two pictures can be compared, and this module is
//! the comparison.
//!
//! It decides nothing about orders. It reports what it found and whether the
//! difference is one the engine may safely trade through. Everything here is
//! a pure function of two inputs, so every case is a test rather than a
//! rehearsal against a live account.
//!
//! The rule it enforces is the one the engine already lives by everywhere
//! else: **exposure the log cannot account for is unknown state.** The engine
//! does not guess whose it is, does not cancel it, and does not trade on top
//! of it. It says so and stops opening.

use std::collections::{BTreeMap, HashMap};

use engine_types::{
    AccountView, OrderRequest, OrderUpdate, Side, StrategyId, SymbolId, VenueOrder, WalRecord,
};

use crate::attribution::{forced_close_owner, Attribution};
use crate::inflight::LedgerOfOrders;

/// The smallest difference worth calling a difference when nothing better is
/// known. Real comparisons use the symbol's own quantity step instead — see
/// [`tolerance`].
const QTY_EPS: f64 = 1e-9;

/// A durable stop belongs to a position direction, not merely a symbol. A
/// two-sided quote may have opening siblings in both directions, and an
/// unfilled later sibling must never overwrite protection for the side that
/// actually filled.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct IntendedPositionStop {
    pub side: Side,
    pub trigger_px: f64,
}

/// How far the log's running total may sit from the venue's before it counts
/// as somebody else's trading.
///
/// Half a step, because no venue can hold a position that is not a whole
/// number of steps, so any real difference is at least one. A fixed tiny
/// number would be wrong in the other direction: this total is a sum over
/// every fill the log ever recorded, and after a few thousand of them the
/// rounding in that sum alone exceeds it. The engine would then latch itself
/// on its own arithmetic and refuse to trade until somebody cleared it.
fn tolerance(step: Option<f64>) -> f64 {
    match step {
        Some(step) if step.is_finite() && step > 0.0 => step / 2.0,
        _ => QTY_EPS,
    }
}

/// One difference between the log and the venue, in plain terms.
#[derive(Clone, Debug, PartialEq)]
pub enum Finding {
    /// Both agree it is working. It keeps its place in the log and stays
    /// charged to the strategy that placed it.
    OrderStillWorking { client_order_id: String },
    /// The log says working; the venue has never heard of it. It ended while
    /// the engine was down and the log never learned how.
    OrderVanished { client_order_id: String },
    /// The venue is working an order the log did not place. Not the engine's
    /// to cancel — it belongs to whoever placed it. `ours_to_trade` is whether
    /// it sits in a symbol a strategy subscribed to: the owner hand-trades
    /// this account, and an order in a symbol the engine cannot even address
    /// is news rather than a problem.
    ForeignOrder {
        client_order_id: String,
        symbol: String,
        ours_to_trade: bool,
    },
    /// A position with no valid stop, or with a venue stop looser than the
    /// durable intent. `venue_px` is absent when no valid stop exists;
    /// `repair_px` is absent when the log cannot prove a safe level.
    UnderprotectedPosition {
        symbol: SymbolId,
        venue_px: Option<f64>,
        repair_px: Option<f64>,
    },
    /// The venue holds more (or less) in a symbol than the log's own fills
    /// account for. Somebody else is trading this symbol.
    UnaccountedExposure {
        symbol: SymbolId,
        venue_qty: f64,
        logged_qty: f64,
    },
    /// The private stream or execution history reported a fill whose order
    /// this log never recorded sending.
    ForeignFill {
        client_order_id: String,
        symbol: SymbolId,
    },
}

impl Finding {
    /// Whether this finding means the engine must not open new exposure.
    ///
    /// An order or a position the log cannot explain is another writer on the
    /// account, and every number the risk kernel works from — the partition,
    /// the envelope — is then measuring somebody else's trading as well as
    /// its own.
    pub fn stops_opening(&self) -> bool {
        match self {
            Finding::ForeignOrder { ours_to_trade, .. } => *ours_to_trade,
            Finding::ForeignFill { .. } => true,
            Finding::UnaccountedExposure { .. } => true,
            // Only when the log cannot say where the stop belongs. A stop it
            // can put back is a repair, not a reason to stop trading.
            Finding::UnderprotectedPosition { repair_px, .. } => repair_px.is_none(),
            Finding::OrderStillWorking { .. } | Finding::OrderVanished { .. } => false,
        }
    }
}

/// What boot learned, and what it may do next.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct Reconciliation {
    pub findings: Vec<Finding>,
}

impl Reconciliation {
    /// True when something was found that the engine cannot trade through.
    /// The caller latches on this, and the latch outlives the process — a
    /// restart that quietly cleared it would turn "stop and tell somebody"
    /// into "stop until the next crash".
    pub fn must_not_open(&self) -> bool {
        self.findings.iter().any(Finding::stops_opening)
    }

    /// Stops the log says belong to symbols that have none at the venue.
    /// Orders the log still shows working that the venue is not. They ended
    /// while the engine was down, and no update for them will ever arrive —
    /// the private stream does not replay history.
    pub fn vanished(&self) -> Vec<String> {
        self.findings
            .iter()
            .filter_map(|finding| match finding {
                Finding::OrderVanished { client_order_id } => Some(client_order_id.clone()),
                _ => None,
            })
            .collect()
    }

    pub fn stop_repairs(&self) -> Vec<(SymbolId, f64)> {
        self.findings
            .iter()
            .filter_map(|f| match f {
                Finding::UnderprotectedPosition {
                    symbol,
                    repair_px: Some(px),
                    ..
                } => Some((*symbol, *px)),
                _ => None,
            })
            .collect()
    }

    /// One line per finding, for the log and for whoever reads it later.
    pub fn lines(&self) -> Vec<String> {
        self.findings.iter().map(describe).collect()
    }
}

fn describe(finding: &Finding) -> String {
    match finding {
        Finding::OrderStillWorking { client_order_id } => {
            format!("{client_order_id} is still working at the venue")
        }
        Finding::OrderVanished { client_order_id } => format!(
            "{client_order_id} was working when the engine stopped and the venue no longer has it"
        ),
        Finding::ForeignOrder {
            client_order_id,
            symbol,
            ours_to_trade: true,
        } => format!(
            "{symbol}: an order this engine did not place is working ({client_order_id}), \
             in a symbol a strategy here trades"
        ),
        Finding::ForeignOrder {
            client_order_id,
            symbol,
            ours_to_trade: false,
        } => format!(
            "{symbol}: somebody else is working an order ({client_order_id}); no strategy here \
             trades that symbol"
        ),
        Finding::UnderprotectedPosition {
            symbol,
            venue_px: None,
            repair_px: Some(px),
        } => format!(
            "symbol {}: held with no valid stop; the log says it belongs at {px}",
            symbol.0
        ),
        Finding::UnderprotectedPosition {
            symbol,
            venue_px: Some(venue_px),
            repair_px: Some(px),
        } => format!(
            "symbol {}: venue stop {venue_px} is looser than the durable intended stop {px}",
            symbol.0
        ),
        Finding::UnderprotectedPosition {
            symbol,
            repair_px: None,
            ..
        } => format!(
            "symbol {}: held with no valid stop, and the log does not say where one belongs",
            symbol.0
        ),
        Finding::UnaccountedExposure {
            symbol,
            venue_qty,
            logged_qty,
        } => format!(
            "symbol {}: the venue holds {venue_qty} and this log accounts for {logged_qty}",
            symbol.0
        ),
        Finding::ForeignFill {
            client_order_id,
            symbol,
        } => format!(
            "symbol {}: a fill names an order this engine did not send ({})",
            symbol.0,
            if client_order_id.is_empty() {
                "blank client id"
            } else {
                client_order_id
            }
        ),
    }
}

/// Compare the log against the venue.
///
/// `resolve` turns a venue symbol name into the engine's own id, and returns
/// `None` for a symbol no strategy subscribed to. An order in such a symbol is
/// somebody else's by definition — the engine could not have placed it, since
/// it can only send orders for symbols it subscribed to — but it is still
/// reported, because it is evidence about who else is on the account.
///
/// `qty_step_of` gives the symbol's smallest tradable quantity, which is what
/// decides whether a difference between the log and the venue is real.
pub fn reconcile(
    log: &LedgerOfOrders,
    replayed: &[WalRecord],
    venue_orders: &[VenueOrder],
    account: &AccountView,
    resolve: impl Fn(&str) -> Option<SymbolId>,
    qty_step_of: impl Fn(SymbolId) -> Option<f64>,
    px_tick_of: impl Fn(SymbolId) -> Option<f64>,
) -> Reconciliation {
    let mut findings = Vec::new();

    // Orders the log still shows working, against the venue's own list.
    for id in log.in_flight_ids() {
        if venue_orders.iter().any(|o| o.client_order_id == id) {
            findings.push(Finding::OrderStillWorking {
                client_order_id: id.to_string(),
            });
        } else {
            findings.push(Finding::OrderVanished {
                client_order_id: id.to_string(),
            });
        }
    }

    // Orders the venue is working that the log has never seen. An empty id is
    // the exchange's own doing — the stop it attaches to a position carries no
    // id of ours — and is not a foreign writer.
    for order in venue_orders {
        if order.client_order_id.is_empty() || log.contains(&order.client_order_id) {
            continue;
        }
        findings.push(Finding::ForeignOrder {
            client_order_id: order.client_order_id.clone(),
            symbol: order.symbol.clone(),
            ours_to_trade: resolve(&order.symbol).is_some(),
        });
    }

    findings.extend(foreign_fills(replayed));

    let (logged, intended) = position_state(replayed);

    for position in &account.positions {
        let intended_px = intended
            .get(&position.symbol.0)
            .filter(|stop| stop.side == position.side)
            .map(|stop| stop.trigger_px)
            .filter(|px| px.is_finite() && *px > 0.0);
        let venue_px =
            (position.stop_attached && position.stop_px.is_finite() && position.stop_px > 0.0)
                .then_some(position.stop_px);
        let px_tolerance = tolerance(px_tick_of(position.symbol));
        let venue_is_looser = match (position.side, venue_px, intended_px) {
            (Side::Buy, Some(venue), Some(want)) => venue + px_tolerance < want,
            (Side::Sell, Some(venue), Some(want)) => venue - px_tolerance > want,
            _ => false,
        };
        if venue_px.is_none() || venue_is_looser {
            findings.push(Finding::UnderprotectedPosition {
                symbol: position.symbol,
                venue_px,
                repair_px: intended_px,
            });
        }
        let venue_qty = signed(position.side, position.qty);
        let logged_qty = logged.get(&position.symbol.0).copied().unwrap_or(0.0);
        if (venue_qty - logged_qty).abs() > tolerance(qty_step_of(position.symbol)) {
            findings.push(Finding::UnaccountedExposure {
                symbol: position.symbol,
                venue_qty,
                logged_qty,
            });
        }
    }

    // A symbol the log has fills for but the venue reports flat is not a
    // finding: the position was closed while the engine was down, which is
    // ordinary. The venue is the truth about what is held.
    Reconciliation { findings }
}

/// Fills that cannot join to an order this log sent since its latest compact
/// state. A clear accepts earlier account history; a segment base carries the
/// durable latch and only keeps ids for orders still open at that boundary.
fn foreign_fills(replayed: &[WalRecord]) -> Vec<Finding> {
    let mut sent: HashMap<String, StrategyId> = HashMap::new();
    let mut claims = Attribution::default();
    let mut findings = Vec::new();
    for record in replayed {
        match record {
            WalRecord::OrderSent { request, .. } => {
                sent.insert(request.client_order_id.clone(), request.strategy);
            }
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        client_order_id,
                        symbol,
                        side,
                        qty,
                        forced_close,
                        ..
                    },
            }
            | WalRecord::RecoveredFill {
                client_order_id,
                symbol,
                side,
                qty,
                forced_close,
                ..
            } => {
                let owner = sent.get(client_order_id).copied().or_else(|| {
                    forced_close_owner(&claims, client_order_id, *symbol, *side, *forced_close)
                });
                match owner {
                    Some(owner) => claims.note(owner, *symbol, *side, *qty),
                    None => findings.push(Finding::ForeignFill {
                        client_order_id: client_order_id.clone(),
                        symbol: *symbol,
                    }),
                }
            }
            WalRecord::ClaimsDropped { rows, .. } => claims.forget(rows),
            WalRecord::LatchCleared {
                restated_exposure, ..
            } => {
                claims.keep_held(restated_exposure);
                findings.clear();
            }
            WalRecord::SegmentBase {
                open_orders,
                attribution,
                ..
            } => {
                claims.restate(attribution);
                sent = open_orders
                    .iter()
                    .map(|order| {
                        (
                            order.request.client_order_id.clone(),
                            order.request.strategy,
                        )
                    })
                    .collect();
                findings.clear();
            }
            _ => {}
        }
    }
    findings
}

fn side_of(signed_qty: f64) -> Option<Side> {
    if signed_qty > QTY_EPS {
        Some(Side::Buy)
    } else if signed_qty < -QTY_EPS {
        Some(Side::Sell)
    } else {
        None
    }
}

fn request_stop_for_fill(
    request: Option<&OrderRequest>,
    symbol: SymbolId,
    side: Side,
) -> Option<f64> {
    let request = request.filter(|request| {
        request.symbol == symbol && request.side == side && !request.reduce_only
    })?;
    let trigger_px = request.stop?.trigger_px;
    (trigger_px.is_finite() && trigger_px > 0.0).then_some(trigger_px)
}

/// Fold one owned fill into both trusted exposure and its stop intent. This
/// is the single arithmetic used by live processing and WAL replay.
///
/// A reduction keeps the current stop. Flat removes it. A fill that grows or
/// crosses the position advances intent only from that fill's own opening
/// request; a missing, malformed, mismatched, or reduce-only request clears
/// repair authority instead of borrowing a sibling's stop.
pub(crate) fn note_owned_fill(
    exposure: &mut BTreeMap<u16, f64>,
    intended: &mut BTreeMap<u16, IntendedPositionStop>,
    request: Option<&OrderRequest>,
    symbol: SymbolId,
    side: Side,
    qty: f64,
) {
    if !qty.is_finite() || qty <= 0.0 {
        return;
    }

    let prior = exposure.get(&symbol.0).copied().unwrap_or(0.0);
    let next = prior + signed(side, qty);
    if side_of(next).is_none() {
        exposure.remove(&symbol.0);
        intended.remove(&symbol.0);
        return;
    }

    exposure.insert(symbol.0, next);
    let next_side = side_of(next).expect("non-flat quantity has a side");
    let crossed_or_opened = side_of(prior) != Some(next_side);
    let grew = next.abs() > prior.abs() + QTY_EPS;
    if crossed_or_opened {
        match request_stop_for_fill(request, symbol, side) {
            Some(trigger_px) => {
                intended.insert(
                    symbol.0,
                    IntendedPositionStop {
                        side: next_side,
                        trigger_px,
                    },
                );
            }
            None => {
                intended.remove(&symbol.0);
            }
        }
    } else if grew {
        if let Some(candidate) = request_stop_for_fill(request, symbol, side) {
            let trigger_px = match intended.get(&symbol.0) {
                Some(current) if current.side == next_side => match next_side {
                    Side::Buy => current.trigger_px.max(candidate),
                    Side::Sell => current.trigger_px.min(candidate),
                },
                _ => candidate,
            };
            intended.insert(
                symbol.0,
                IntendedPositionStop {
                    side: next_side,
                    trigger_px,
                },
            );
        }
    } else if intended
        .get(&symbol.0)
        .is_some_and(|stop| stop.side != next_side)
    {
        intended.remove(&symbol.0);
    }
}

fn matching_intended_stop(
    row: &engine_types::IntendedStop,
    exposure: &BTreeMap<u16, f64>,
) -> Option<(u16, IntendedPositionStop)> {
    let side = row.side?;
    let trigger_px = row.trigger_px;
    if !trigger_px.is_finite()
        || trigger_px <= 0.0
        || side_of(exposure.get(&row.symbol.0).copied().unwrap_or(0.0)) != Some(side)
    {
        return None;
    }
    Some((row.symbol.0, IntendedPositionStop { side, trigger_px }))
}

fn position_state(
    replayed: &[WalRecord],
) -> (BTreeMap<u16, f64>, BTreeMap<u16, IntendedPositionStop>) {
    let mut exposure = BTreeMap::new();
    let mut intended = BTreeMap::new();
    let mut sent: HashMap<String, OrderRequest> = HashMap::new();
    // The claims a venue-initiated close is charged against, kept the way
    // `attribution` keeps them, so this walk resolves the same owner the live
    // path did and trusted exposure cannot drift across a restart.
    let mut claims = Attribution::default();

    for record in replayed {
        match record {
            WalRecord::OrderSent { request, .. } => {
                sent.insert(request.client_order_id.clone(), request.clone());
            }
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        client_order_id,
                        symbol,
                        side,
                        qty,
                        forced_close,
                        ..
                    },
            }
            | WalRecord::RecoveredFill {
                client_order_id,
                symbol,
                side,
                qty,
                forced_close,
                ..
            } => {
                let request = sent.get(client_order_id);
                let owner = request.map(|request| request.strategy).or_else(|| {
                    forced_close_owner(&claims, client_order_id, *symbol, *side, *forced_close)
                });
                if let Some(owner) = owner {
                    note_owned_fill(&mut exposure, &mut intended, request, *symbol, *side, *qty);
                    claims.note(owner, *symbol, *side, *qty);
                }
            }
            WalRecord::StopSet {
                symbol, trigger_px, ..
            } => {
                if trigger_px.is_finite() && *trigger_px > 0.0 {
                    if let Some(side) = side_of(exposure.get(&symbol.0).copied().unwrap_or(0.0)) {
                        intended.insert(
                            symbol.0,
                            IntendedPositionStop {
                                side,
                                trigger_px: *trigger_px,
                            },
                        );
                    }
                }
            }
            WalRecord::ClaimsDropped { rows, .. } => claims.forget(rows),
            WalRecord::SegmentBase {
                logged_exposure,
                intended_stops,
                open_orders,
                attribution,
                ..
            } => {
                claims.restate(attribution);
                exposure = logged_exposure
                    .iter()
                    .filter(|row| row.signed_qty.is_finite() && row.signed_qty.abs() > QTY_EPS)
                    .map(|row| (row.symbol.0, row.signed_qty))
                    .collect();
                intended = intended_stops
                    .iter()
                    .filter_map(|row| matching_intended_stop(row, &exposure))
                    .collect();
                sent = open_orders
                    .iter()
                    .map(|order| (order.request.client_order_id.clone(), order.request.clone()))
                    .collect();
            }
            WalRecord::LatchCleared {
                restated_exposure, ..
            } => {
                claims.keep_held(restated_exposure);
                exposure = restated_exposure
                    .iter()
                    .filter(|row| row.signed_qty.is_finite() && row.signed_qty.abs() > QTY_EPS)
                    .map(|row| (row.symbol.0, row.signed_qty))
                    .collect();
                // The operator accepted the venue's quantity, not an old
                // order's stop provenance. Reusing a same-direction stop from
                // before the clear could attach one writer's intent to
                // another writer's position, so repair authority starts empty.
                intended.clear();
            }
            _ => {}
        }
    }

    (exposure, intended)
}

/// Signed quantity per symbol from fills that join to orders this log sent.
/// Foreign fills remain durable records but never become trusted engine
/// exposure. A segment restatement is "set", not "add": at its place in the
/// stream it is exactly what the records before it added up to.
pub(crate) fn logged_exposure(replayed: &[WalRecord]) -> BTreeMap<u16, f64> {
    position_state(replayed).0
}

/// The stop belonging to each trusted filled position. Merely sending an
/// opposite-side sibling cannot alter this state; only an owned fill that
/// grows or crosses the position can do so.
pub(crate) fn intended_stops(replayed: &[WalRecord]) -> BTreeMap<u16, IntendedPositionStop> {
    position_state(replayed).1
}

fn signed(side: Side, qty: f64) -> f64 {
    match side {
        Side::Buy => qty,
        Side::Sell => -qty,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{OrderKind, PositionView, StopSpec, StrategyId};

    fn request(id: &str, symbol: u16, side: Side, qty: f64, stop: Option<f64>) -> OrderRequest {
        OrderRequest {
            client_order_id: id.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(symbol),
            side,
            qty,
            kind: OrderKind::Market,
            stop: stop.map(|trigger_px| StopSpec { trigger_px }),
            reduce_only: false,
            close_position: false,
        }
    }

    fn sent(id: &str, symbol: u16, side: Side, qty: f64, stop: Option<f64>) -> WalRecord {
        WalRecord::OrderSent {
            request: request(id, symbol, side, qty, stop),
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }

    fn fill(id: &str, symbol: u16, side: Side, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: String::new(),
                client_order_id: id.into(),
                symbol: SymbolId(symbol),
                side,
                qty,
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: 0,
                recv_ns: 0,
            },
        }
    }

    fn venue_order(id: &str, symbol: &str) -> VenueOrder {
        VenueOrder {
            client_order_id: id.into(),
            symbol: symbol.into(),
            side: Side::Buy,
            qty: 1.0,
            filled_qty: 0.0,
            reduce_only: false,
        }
    }

    fn account(positions: Vec<PositionView>) -> AccountView {
        AccountView {
            equity_usdt: 1_000.0,
            available_usdt: 900.0,
            positions,
            observed_ns: 1,
        }
    }

    fn held(symbol: u16, side: Side, qty: f64, stop_attached: bool) -> PositionView {
        PositionView {
            symbol: SymbolId(symbol),
            side,
            qty,
            entry_px: 100.0,
            stop_px: if stop_attached { 100.0 } else { 0.0 },
            stop_attached,
            leverage: None,
        }
    }

    /// Stands in for the engine's symbol table: BTCUSDT is subscribed, and
    /// anything else is a symbol no strategy here trades.
    fn names(symbol: &str) -> Option<SymbolId> {
        (symbol == "BTCUSDT").then_some(SymbolId(0))
    }

    /// A step of 0.001, the sort of thing a venue actually quotes.
    fn steps(_: SymbolId) -> Option<f64> {
        Some(0.001)
    }

    fn run(replayed: &[WalRecord], venue: &[VenueOrder], acct: &AccountView) -> Reconciliation {
        let log = LedgerOfOrders::from_records(replayed);
        reconcile(&log, replayed, venue, acct, names, steps, |_| Some(0.5))
    }

    #[test]
    fn a_quiet_account_finds_nothing() {
        let out = run(&[], &[], &account(vec![]));
        assert!(out.findings.is_empty());
        assert!(!out.must_not_open());
    }

    #[test]
    fn an_order_both_sides_agree_on_is_adopted() {
        let log = vec![sent("eng-1", 0, Side::Buy, 1.0, Some(90.0))];
        let out = run(&log, &[venue_order("eng-1", "BTCUSDT")], &account(vec![]));
        assert_eq!(
            out.findings,
            vec![Finding::OrderStillWorking {
                client_order_id: "eng-1".into()
            }]
        );
        assert!(!out.must_not_open(), "agreeing is not a reason to stop");
    }

    #[test]
    fn an_order_the_venue_forgot_is_reported_but_does_not_stop_trading() {
        // It ended while the engine was down. The position, if any, shows up
        // in the account reading, which is the picture that matters.
        let log = vec![sent("eng-1", 0, Side::Buy, 1.0, Some(90.0))];
        let out = run(&log, &[], &account(vec![]));
        assert_eq!(
            out.findings,
            vec![Finding::OrderVanished {
                client_order_id: "eng-1".into()
            }]
        );
        assert!(!out.must_not_open());
    }

    #[test]
    fn an_order_this_engine_never_placed_stops_it_opening() {
        let out = run(
            &[],
            &[venue_order("somebody-else-7", "BTCUSDT")],
            &account(vec![]),
        );
        assert_eq!(
            out.findings,
            vec![Finding::ForeignOrder {
                client_order_id: "somebody-else-7".into(),
                symbol: "BTCUSDT".into(),
                ours_to_trade: true,
            }]
        );
        assert!(
            out.must_not_open(),
            "a second writer makes every risk number wrong"
        );
    }

    #[test]
    fn a_hand_trade_in_a_symbol_nobody_here_trades_is_news_not_a_halt() {
        // The owner trades this account by hand. An order in a symbol no
        // strategy subscribed to cannot touch anything the engine does, and
        // stopping for it would mean the engine stops most days.
        let out = run(
            &[],
            &[venue_order("owners-own-3", "DOGEUSDT")],
            &account(vec![]),
        );
        assert_eq!(out.findings.len(), 1);
        assert!(!out.must_not_open(), "{:?}", out.findings);
        assert!(out.lines()[0].contains("DOGEUSDT"));
    }

    #[test]
    fn the_exchanges_own_stop_order_is_not_a_foreign_writer() {
        // A stop attached to a position carries no id of ours, and treating it
        // as somebody else's order would latch the engine every single boot
        // that it held anything.
        let mut stop = venue_order("", "BTCUSDT");
        stop.reduce_only = true;
        let out = run(&[], &[stop], &account(vec![]));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn a_position_the_log_accounts_for_is_not_unaccounted() {
        let log = vec![
            sent("eng-1", 3, Side::Buy, 2.0, Some(90.0)),
            fill("eng-1", 3, Side::Buy, 2.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 2.0, true)]));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn a_position_bigger_than_the_logs_own_fills_stops_it_opening() {
        let log = vec![
            sent("eng-1", 3, Side::Buy, 1.0, Some(90.0)),
            fill("eng-1", 3, Side::Buy, 1.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 1.5, true)]));
        assert_eq!(
            out.findings,
            vec![Finding::UnaccountedExposure {
                symbol: SymbolId(3),
                venue_qty: 1.5,
                logged_qty: 1.0
            }]
        );
        assert!(out.must_not_open());
    }

    #[test]
    fn a_position_on_a_fresh_log_is_unaccounted_rather_than_assumed_ours() {
        // A new engine, or one whose log was replaced, knows nothing about the
        // position it finds. Adopting it silently is how a second writer's
        // exposure becomes the engine's own problem.
        let out = run(&[], &[], &account(vec![held(3, Side::Buy, 1.0, true)]));
        assert!(out.must_not_open());
        assert!(matches!(
            out.findings[0],
            Finding::UnaccountedExposure { .. }
        ));
    }

    #[test]
    fn rounding_in_the_logs_own_sum_is_not_somebody_elses_trading() {
        // The running total is a sum over every fill the log ever kept. After
        // enough of them its last digits drift, and a fixed tiny tolerance
        // would read that drift as a second writer and latch the engine on
        // its own arithmetic.
        let mut log = vec![sent("eng-1", 3, Side::Buy, 1.0, Some(90.0))];
        for _ in 0..20_000 {
            log.push(fill("eng-1", 3, Side::Buy, 0.1));
        }
        let total: f64 = 20_000.0 * 0.1;
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, total, true)]));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn a_difference_of_one_whole_step_is_real() {
        let log = vec![
            sent("eng-1", 3, Side::Buy, 1.0, Some(90.0)),
            fill("eng-1", 3, Side::Buy, 1.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 1.001, true)]));
        assert!(
            out.must_not_open(),
            "one step is the smallest position a venue can hold"
        );
    }

    #[test]
    fn a_short_the_log_accounts_for_nets_out() {
        let log = vec![
            sent("eng-1", 3, Side::Sell, 2.0, Some(110.0)),
            fill("eng-1", 3, Side::Sell, 2.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Sell, 2.0, true)]));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn an_unprotected_position_is_repaired_at_the_level_the_log_intended() {
        let log = vec![
            sent("eng-1", 3, Side::Buy, 1.0, Some(88.5)),
            fill("eng-1", 3, Side::Buy, 1.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 1.0, false)]));
        assert_eq!(out.stop_repairs(), vec![(SymbolId(3), 88.5)]);
        assert!(
            !out.must_not_open(),
            "a stop it can put back is a repair, not a halt"
        );
    }

    #[test]
    fn an_attached_but_looser_long_stop_is_repaired() {
        let log = vec![
            sent("eng-1", 3, Side::Buy, 1.0, Some(90.0)),
            fill("eng-1", 3, Side::Buy, 1.0),
        ];
        let mut position = held(3, Side::Buy, 1.0, true);
        position.stop_px = 85.0;

        let out = run(&log, &[], &account(vec![position]));

        assert_eq!(out.stop_repairs(), vec![(SymbolId(3), 90.0)]);
    }

    #[test]
    fn an_attached_but_looser_short_stop_is_repaired() {
        let log = vec![
            sent("eng-1", 3, Side::Sell, 1.0, Some(110.0)),
            fill("eng-1", 3, Side::Sell, 1.0),
        ];
        let mut position = held(3, Side::Sell, 1.0, true);
        position.stop_px = 115.0;

        let out = run(&log, &[], &account(vec![position]));

        assert_eq!(out.stop_repairs(), vec![(SymbolId(3), 110.0)]);
    }

    #[test]
    fn an_equal_or_tighter_attached_stop_is_never_loosened() {
        for (side, intended, venue) in [
            (Side::Buy, 90.0, 95.0),
            (Side::Buy, 90.0, 90.0),
            (Side::Sell, 110.0, 105.0),
            (Side::Sell, 110.0, 110.0),
        ] {
            let log = vec![
                sent("eng-1", 3, side, 1.0, Some(intended)),
                fill("eng-1", 3, side, 1.0),
            ];
            let mut position = held(3, side, 1.0, true);
            position.stop_px = venue;

            let out = run(&log, &[], &account(vec![position]));

            assert!(out.stop_repairs().is_empty(), "{side:?} {out:?}");
        }
    }

    #[test]
    fn an_unprotected_position_the_log_cannot_place_a_stop_for_stops_it_opening() {
        let out = run(&[], &[], &account(vec![held(3, Side::Buy, 1.0, false)]));
        assert!(out.stop_repairs().is_empty(), "no level to repair to");
        assert!(
            out.must_not_open(),
            "guessing a stop level is worse than refusing"
        );
    }

    #[test]
    fn a_reducing_order_never_supplies_a_stop_level() {
        // An exit carries no stop, so it must not be read as one.
        let mut exit = request("eng-2", 3, Side::Sell, 1.0, Some(999.0));
        exit.reduce_only = true;
        let log = vec![
            sent("eng-1", 3, Side::Buy, 1.0, Some(88.5)),
            WalRecord::OrderSent {
                request: exit,
                wire_ns: 2,
                arrival_mid: 0.0,
            },
            fill("eng-1", 3, Side::Buy, 1.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 1.0, false)]));
        assert_eq!(out.stop_repairs(), vec![(SymbolId(3), 88.5)]);
    }

    #[test]
    fn the_newest_opening_order_supplies_the_stop_level() {
        let log = vec![
            sent("eng-1", 3, Side::Buy, 1.0, Some(80.0)),
            sent("eng-2", 3, Side::Buy, 1.0, Some(85.0)),
            fill("eng-1", 3, Side::Buy, 1.0),
            fill("eng-2", 3, Side::Buy, 1.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 2.0, false)]));
        assert_eq!(out.stop_repairs(), vec![(SymbolId(3), 85.0)]);
    }

    #[test]
    fn two_sided_siblings_follow_the_buy_that_fills_first() {
        let log = vec![
            sent("buy", 3, Side::Buy, 1.0, Some(90.0)),
            sent("sell", 3, Side::Sell, 1.0, Some(110.0)),
            fill("buy", 3, Side::Buy, 1.0),
        ];

        let stops = intended_stops(&log);
        assert_eq!(
            stops.get(&3),
            Some(&IntendedPositionStop {
                side: Side::Buy,
                trigger_px: 90.0,
            })
        );
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 1.0, false)]));
        assert_eq!(out.stop_repairs(), vec![(SymbolId(3), 90.0)]);
    }

    #[test]
    fn two_sided_siblings_follow_the_sell_that_fills_first() {
        let log = vec![
            sent("buy", 3, Side::Buy, 1.0, Some(90.0)),
            sent("sell", 3, Side::Sell, 1.0, Some(110.0)),
            fill("sell", 3, Side::Sell, 1.0),
        ];

        let stops = intended_stops(&log);
        assert_eq!(
            stops.get(&3),
            Some(&IntendedPositionStop {
                side: Side::Sell,
                trigger_px: 110.0,
            })
        );
        let out = run(&log, &[], &account(vec![held(3, Side::Sell, 1.0, false)]));
        assert_eq!(out.stop_repairs(), vec![(SymbolId(3), 110.0)]);
    }

    #[test]
    fn a_rejected_opposite_sibling_never_controls_stop_repair() {
        let log = vec![
            sent("buy", 3, Side::Buy, 1.0, Some(90.0)),
            sent("sell", 3, Side::Sell, 1.0, Some(110.0)),
            WalRecord::OrderUpdate {
                update: OrderUpdate::Reject {
                    client_order_id: "sell".into(),
                    code: 10001,
                    reason: "rejected".into(),
                },
            },
            fill("buy", 3, Side::Buy, 1.0),
        ];

        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 1.0, false)]));
        assert_eq!(out.stop_repairs(), vec![(SymbolId(3), 90.0)]);
    }

    #[test]
    fn a_reduction_keeps_the_stop_and_a_cross_takes_the_new_orders_stop() {
        let log = vec![
            sent("buy", 3, Side::Buy, 2.0, Some(90.0)),
            fill("buy", 3, Side::Buy, 2.0),
            sent("sell", 3, Side::Sell, 3.0, Some(110.0)),
            fill("sell", 3, Side::Sell, 1.0),
        ];
        assert_eq!(
            intended_stops(&log).get(&3),
            Some(&IntendedPositionStop {
                side: Side::Buy,
                trigger_px: 90.0,
            })
        );

        let mut crossed = log;
        crossed.push(fill("sell", 3, Side::Sell, 2.0));
        assert_eq!(
            intended_stops(&crossed).get(&3),
            Some(&IntendedPositionStop {
                side: Side::Sell,
                trigger_px: 110.0,
            })
        );
    }

    #[test]
    fn same_side_growth_can_t_loosen_a_long_positions_stop() {
        let log = vec![
            sent("first", 3, Side::Buy, 1.0, Some(95.0)),
            fill("first", 3, Side::Buy, 1.0),
            sent("grow", 3, Side::Buy, 1.0, Some(90.0)),
            fill("grow", 3, Side::Buy, 1.0),
        ];

        assert_eq!(
            intended_stops(&log).get(&3),
            Some(&IntendedPositionStop {
                side: Side::Buy,
                trigger_px: 95.0,
            })
        );
    }

    #[test]
    fn same_side_growth_can_t_loosen_a_short_positions_stop() {
        let log = vec![
            sent("first", 3, Side::Sell, 1.0, Some(105.0)),
            fill("first", 3, Side::Sell, 1.0),
            sent("grow", 3, Side::Sell, 1.0, Some(110.0)),
            fill("grow", 3, Side::Sell, 1.0),
        ];

        assert_eq!(
            intended_stops(&log).get(&3),
            Some(&IntendedPositionStop {
                side: Side::Sell,
                trigger_px: 105.0,
            })
        );
    }

    fn recovered(exec: &str, id: &str, symbol: u16, side: Side, qty: f64) -> WalRecord {
        WalRecord::RecoveredFill {
            exec_id: exec.into(),
            client_order_id: id.into(),
            symbol: SymbolId(symbol),
            side,
            qty,
            px: 100.0,
            fee: Some(0.0),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: 5,
            recovered_wall_ts_ms: 6,
        }
    }

    #[test]
    fn a_recovered_fill_squares_the_venue_position() {
        // A fill landed while the engine was down. Without recovery the
        // venue holds more than the log accounts for and the engine stops
        // opening; with the recovered record the same picture is clean.
        let log = vec![
            sent("eng-1", 3, Side::Buy, 3.0, Some(90.0)),
            fill("eng-1", 3, Side::Buy, 2.0),
        ];
        let without = run(&log, &[], &account(vec![held(3, Side::Buy, 3.0, true)]));
        assert!(
            without.must_not_open(),
            "the gap fill is unaccounted until recovered"
        );
        let mut with = log.clone();
        with.push(recovered("2f6e", "eng-1", 3, Side::Buy, 1.0));
        let out = run(&with, &[], &account(vec![held(3, Side::Buy, 3.0, true)]));
        // The order itself still reads as vanished (its remainder ended in
        // the same gap), which reports and does not stop anything.
        assert!(!out.must_not_open(), "{:?}", out.findings);
        assert!(
            !out.findings
                .iter()
                .any(|f| matches!(f, Finding::UnaccountedExposure { .. })),
            "{:?}",
            out.findings
        );
    }

    #[test]
    fn a_foreign_recovered_fill_cannot_square_trusted_exposure() {
        let log = vec![
            sent("eng-1", 3, Side::Buy, 2.0, Some(90.0)),
            fill("eng-1", 3, Side::Buy, 2.0),
            recovered("manual-exec", "manual-order", 3, Side::Buy, 1.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 3.0, true)]));

        assert_eq!(logged_exposure(&log).get(&3), Some(&2.0));
        assert!(out.findings.iter().any(|finding| matches!(
            finding,
            Finding::ForeignFill { client_order_id, symbol }
                if client_order_id == "manual-order" && *symbol == SymbolId(3)
        )));
        assert!(out.findings.iter().any(|finding| matches!(
            finding,
            Finding::UnaccountedExposure { symbol, logged_qty, .. }
                if *symbol == SymbolId(3) && *logged_qty == 2.0
        )));
        assert!(out.must_not_open());
    }

    #[test]
    fn a_blank_recovered_fill_is_foreign_even_with_one_known_owner() {
        let log = vec![
            sent("eng-1", 3, Side::Buy, 2.0, Some(90.0)),
            fill("eng-1", 3, Side::Buy, 2.0),
            recovered("blank-exec", "", 3, Side::Sell, 1.0),
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 1.0, true)]));

        assert_eq!(logged_exposure(&log).get(&3), Some(&2.0));
        assert!(out.findings.iter().any(|finding| matches!(
            finding,
            Finding::ForeignFill { client_order_id, symbol }
                if client_order_id.is_empty() && *symbol == SymbolId(3)
        )));
        assert!(out.must_not_open());
    }

    /// A close the venue itself started, replayed at the next boot. It must
    /// read the way the live path read it: the sleeve's own close, squaring
    /// trusted exposure and latching nothing. Otherwise every restart after a
    /// stop-out stops the engine opening.
    #[test]
    fn a_venue_stop_squares_trusted_exposure_on_replay() {
        let stop = WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: "venue-stop".into(),
                client_order_id: String::new(),
                symbol: SymbolId(3),
                side: Side::Sell,
                qty: 2.0,
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: Some(engine_types::ForcedClose::StopLoss),
                venue_ts_ms: 1,
                recv_ns: 1,
            },
        };
        let log = vec![
            sent("eng-1", 3, Side::Buy, 2.0, Some(90.0)),
            fill("eng-1", 3, Side::Buy, 2.0),
            stop,
        ];
        let out = run(&log, &[], &account(vec![]));

        assert_eq!(logged_exposure(&log).get(&3), None, "the stop closed it");
        assert!(
            !out.findings
                .iter()
                .any(|finding| matches!(finding, Finding::ForeignFill { .. })),
            "our own position closing is not a stranger's fill: {:?}",
            out.findings
        );
        assert!(!out.must_not_open());
    }

    #[test]
    fn an_operators_restatement_is_the_ledger_from_then_on() {
        use engine_types::SymbolTotal;
        // Debt older than the venue's history cannot be replayed, only
        // looked at and absorbed. After the clear the ledger says what the
        // venue said, and later fills move THAT number.
        let mut log = vec![
            sent("eng-1", 3, Side::Sell, 14_455.0, Some(110.0)),
            fill("eng-1", 3, Side::Sell, 14_455.0),
            WalRecord::LatchCleared {
                wall_ts_ms: 9,
                note: "looked at the log".into(),
                restated_exposure: vec![SymbolTotal {
                    symbol: SymbolId(3),
                    signed_qty: 371.1,
                }],
                findings: vec!["symbol 3: unaccounted".into()],
            },
        ];
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 371.1, true)]));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
        log.push(fill("eng-1", 3, Side::Sell, 100.0));
        let out = run(&log, &[], &account(vec![held(3, Side::Buy, 271.1, true)]));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn every_finding_says_something_a_person_can_act_on() {
        let log = vec![sent("eng-1", 3, Side::Buy, 1.0, Some(88.5))];
        let out = run(
            &log,
            &[venue_order("not-ours", "ETHUSDT")],
            &account(vec![held(3, Side::Buy, 1.0, false)]),
        );
        for line in out.lines() {
            assert!(!line.is_empty());
            assert!(line.len() > 20, "too terse to act on: {line}");
        }
        assert_eq!(out.lines().len(), out.findings.len());
    }
}
