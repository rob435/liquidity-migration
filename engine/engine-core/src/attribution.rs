//! Which strategy's trading a venue position came from.
//!
//! The venue keeps one position per symbol however many strategies run, and
//! its account reading carries no attribution at all. Two book-following plugs
//! on one account therefore read the same position as each of theirs. On the
//! first cycle where their books disagreed, one would exit the other's holding
//! — a full-size close of a position it never opened — and both would size
//! against exposure only one of them put on.
//!
//! The engine already knows the answer. It minted every order and wrote the
//! strategy on it before the bytes left the socket, so the join is right there
//! in the log: the order says who sent it, the fill says which order it was.
//! This keeps that running total, and rebuilds it from the log at boot so a
//! restart does not forget whose position is whose.
//!
//! It is **not** a second position record. It says who opened what; the
//! account reading remains the only word on how much is actually there. Where
//! the two disagree, the difference is somebody else's trading, and
//! [`crate::reconcile`] is what has an opinion about that.

use std::collections::HashMap;

use engine_types::{
    FilledTotal, ForcedClose, OrderUpdate, Side, StrategyId, SymbolId, SymbolTotal, WalRecord,
};

/// Smaller than this is flat. A venue position is a whole number of quantity
/// steps, so anything under it is this sum's own rounding rather than a
/// holding — the same reasoning as `reconcile`'s tolerance, one order of
/// magnitude coarser than nothing.
const FLAT: f64 = 1e-9;

/// Whether a fill on this side makes a signed claim smaller. Positive is long,
/// so a sale reduces a long and a purchase reduces a short.
pub fn reduces(claim: f64, side: Side) -> bool {
    match side {
        Side::Sell => claim > 0.0,
        Side::Buy => claim < 0.0,
    }
}

/// Whose position a close the venue itself started belongs to.
///
/// The venue closes a position, not an order: the row carries no
/// `orderLinkId`, so the join every other fill uses is not there. The holding
/// is the join instead — one sleeve claims the symbol, and the close reduces
/// that claim. A symbol two sleeves claim, or a fill that would grow the
/// claim rather than cut it, belongs to nobody here: guessing would charge one
/// sleeve for another's stop.
pub fn forced_close_owner(
    attribution: &Attribution,
    client_order_id: &str,
    symbol: SymbolId,
    side: Side,
    forced_close: Option<ForcedClose>,
) -> Option<StrategyId> {
    if !client_order_id.is_empty() || forced_close.is_none() {
        return None;
    }
    let owner = attribution.sole_owner(symbol)?;
    reduces(attribution.signed(owner, symbol), side).then_some(owner)
}

#[derive(Debug, Default)]
pub struct Attribution {
    /// Signed filled quantity per strategy and symbol. Positive is long.
    filled: HashMap<(u16, u16), f64>,
}

impl Attribution {
    /// Rebuild from the log. Exactly the live path's arithmetic, run over the
    /// join the log already holds, so boot and steady state cannot drift.
    ///
    /// One pass is enough: an order's `OrderSent` record is made durable
    /// before its bytes leave the socket, so it is always ahead of its fills.
    pub fn from_records(records: &[WalRecord]) -> Self {
        let mut sender: HashMap<&str, StrategyId> = HashMap::new();
        let mut me = Attribution::default();
        for record in records {
            match record {
                WalRecord::OrderSent { request, .. } => {
                    sender.insert(request.client_order_id.as_str(), request.strategy);
                }
                // A fill for an order this log never recorded sending belongs
                // to somebody else on the account, unless the venue named it
                // a close of a position one sleeve holds. Anything else is
                // charged to nobody on purpose: the engine does not guess
                // whose it is, and `reconcile` is what notices the account
                // holds more than the log accounts for.
                WalRecord::OrderUpdate { update } => {
                    let OrderUpdate::Fill {
                        client_order_id,
                        symbol,
                        side,
                        forced_close,
                        ..
                    } = update
                    else {
                        continue;
                    };
                    let strategy = sender.get(client_order_id.as_str()).copied().or_else(|| {
                        forced_close_owner(&me, client_order_id, *symbol, *side, *forced_close)
                    });
                    let Some(strategy) = strategy else {
                        continue;
                    };
                    me.on_update(strategy, update);
                }
                // A fill recovered from the venue's history joins the same
                // two ways: through the order that produced it, or through
                // the position a venue-named close reduced.
                WalRecord::RecoveredFill {
                    client_order_id,
                    symbol,
                    side,
                    qty,
                    forced_close,
                    ..
                } => {
                    let strategy = sender.get(client_order_id.as_str()).copied().or_else(|| {
                        forced_close_owner(&me, client_order_id, *symbol, *side, *forced_close)
                    });
                    let Some(strategy) = strategy else {
                        continue;
                    };
                    me.note(strategy, *symbol, *side, *qty);
                }
                WalRecord::ClaimsDropped { rows, .. } => me.forget(rows),
                WalRecord::LatchCleared {
                    restated_exposure, ..
                } => me.keep_held(restated_exposure),
                // Still-open orders arrive through the same record, so
                // `sender` keeps resolving their later fills.
                WalRecord::SegmentBase {
                    attribution,
                    open_orders,
                    ..
                } => {
                    me.restate(attribution);
                    for open in open_orders {
                        sender.insert(open.request.client_order_id.as_str(), open.request.strategy);
                    }
                }
                _ => {}
            }
        }
        me
    }

    /// Replace every claim with a rotation's own account of them. Set, not
    /// add: at its place in a chain read these rows are exactly what the fills
    /// before them summed to, and in a fresh segment they are all there is.
    pub fn restate(&mut self, rows: &[FilledTotal]) {
        self.filled = rows
            .iter()
            .map(|row| ((row.strategy.0, row.symbol.0), row.signed_qty))
            .collect();
    }

    /// Forget the claims a boot dropped against a flat venue reading. The drop
    /// replays like everything else, or a restart would rebuild the residue
    /// from the old fills — and by then the symbol may be held by another
    /// sleeve, making it undroppable.
    pub fn forget(&mut self, rows: &[FilledTotal]) {
        for row in rows {
            self.filled.remove(&(row.strategy.0, row.symbol.0));
        }
    }

    /// Keep only the symbols an operator's restatement (`engine
    /// reconcile-clear`) still shows held. A symbol it reports flat is held by
    /// nobody, whatever the fills before it summed to.
    pub fn keep_held(&mut self, restated: &[SymbolTotal]) {
        self.filled.retain(|(_, symbol), _| {
            restated
                .iter()
                .any(|row| row.symbol.0 == *symbol && row.signed_qty.abs() >= FLAT)
        });
    }

    /// Every non-flat row, sorted, for a rotation to restate.
    pub fn rows(&self) -> Vec<(StrategyId, SymbolId, f64)> {
        let mut rows: Vec<(StrategyId, SymbolId, f64)> = self
            .filled
            .iter()
            .map(|((strategy, symbol), qty)| (StrategyId(*strategy), SymbolId(*symbol), *qty))
            .collect();
        rows.sort_by_key(|(strategy, symbol, _)| (strategy.0, symbol.0));
        rows
    }

    /// Charge one fill to the strategy whose order produced it. The caller
    /// resolves the owner from the order ledger, so a fill can never be
    /// charged to a strategy that did not place it. Anything that is not a
    /// fill is ignored.
    pub fn on_update(&mut self, strategy: StrategyId, update: &OrderUpdate) {
        let OrderUpdate::Fill {
            symbol, side, qty, ..
        } = update
        else {
            return;
        };
        self.note(strategy, *symbol, *side, *qty);
    }

    /// The one arithmetic behind both the delivered and the recovered path.
    pub fn note(&mut self, strategy: StrategyId, symbol: SymbolId, side: Side, qty: f64) {
        // An unreal number would poison the running total for good.
        if !qty.is_finite() {
            return;
        }
        let signed = match side {
            Side::Buy => qty,
            Side::Sell => -qty,
        };
        let key = (strategy.0, symbol.0);
        let total = self.filled.entry(key).or_insert(0.0);
        *total += signed;
        if total.abs() < FLAT {
            self.filled.remove(&key);
        }
    }

    /// Signed quantity this strategy's own orders opened in this symbol.
    pub fn signed(&self, strategy: StrategyId, symbol: SymbolId) -> f64 {
        self.filled
            .get(&(strategy.0, symbol.0))
            .copied()
            .unwrap_or(0.0)
    }

    /// Symbols this strategy still has a non-flat fill claim on.
    pub fn symbols(&self, strategy: StrategyId) -> impl Iterator<Item = SymbolId> + '_ {
        self.filled
            .iter()
            .filter_map(move |((owner, symbol), qty)| {
                (*owner == strategy.0 && qty.abs() >= FLAT).then_some(SymbolId(*symbol))
            })
    }

    /// The only sleeve with a non-flat claim on this symbol.
    pub fn sole_owner(&self, symbol: SymbolId) -> Option<StrategyId> {
        let mut owners = self.filled.iter().filter_map(|((strategy, held), qty)| {
            (*held == symbol.0 && qty.abs() >= FLAT).then_some(StrategyId(*strategy))
        });
        let owner = owners.next()?;
        owners.next().is_none().then_some(owner)
    }

    /// Drop every row in the symbols the caller says are flat, returning
    /// what was dropped, sorted.
    ///
    /// The account reading is the only word on how much is actually there
    /// (module note above). A row surviving in a symbol the venue holds
    /// nothing of is a close this log never got to charge — a hand close, or
    /// an inherited position wound down — and
    /// left in place it keeps [`Attribution::held_by_another`] claiming a
    /// holding that does not exist, locking every other sleeve out of the
    /// name for good. The caller decides what "flat" means; boot asks it
    /// with the venue reading reconcile just judged, skipping symbols with
    /// an order still in flight.
    pub fn drop_where_flat(
        &mut self,
        flat: impl Fn(SymbolId) -> bool,
    ) -> Vec<(StrategyId, SymbolId, f64)> {
        let mut dropped: Vec<(StrategyId, SymbolId, f64)> = Vec::new();
        self.filled.retain(|(strategy, symbol), qty| {
            if flat(SymbolId(*symbol)) {
                dropped.push((StrategyId(*strategy), SymbolId(*symbol), *qty));
                false
            } else {
                true
            }
        });
        dropped.sort_by_key(|(strategy, symbol, _)| (strategy.0, symbol.0));
        dropped
    }

    /// Whether a strategy other than this one is holding this symbol.
    ///
    /// The question a plug needs answered before it acts on a name. It is
    /// asked per symbol rather than per quantity because a venue stop covers
    /// the whole position: two strategies sharing a symbol cannot each have
    /// their own stop on it, so sharing is not something to be sized around.
    /// The one who got there first keeps it until it is flat.
    pub fn held_by_another(&self, mine: StrategyId, symbol: SymbolId) -> bool {
        self.filled.iter().any(|((strategy, held), qty)| {
            *held == symbol.0 && *strategy != mine.0 && qty.abs() >= FLAT
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{OrderKind, OrderRequest};

    const CARRY: StrategyId = StrategyId(0);
    const LONG: StrategyId = StrategyId(1);
    const BTC: SymbolId = SymbolId(7);
    const ETH: SymbolId = SymbolId(8);

    fn sent(id: &str, strategy: StrategyId, symbol: SymbolId) -> WalRecord {
        WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: id.to_string(),
                strategy,
                symbol,
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }

    fn fill(id: &str, symbol: SymbolId, side: Side, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: String::new(),
                client_order_id: id.to_string(),
                symbol,
                side,
                qty,
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: 1,
                recv_ns: 1,
            },
        }
    }

    /// A close the venue itself started: no `orderLinkId`, and a reason.
    fn stop_fill(symbol: SymbolId, side: Side, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: "venue-stop".into(),
                client_order_id: String::new(),
                symbol,
                side,
                qty,
                px: 99.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: Some(ForcedClose::StopLoss),
                venue_ts_ms: 2,
                recv_ns: 2,
            },
        }
    }

    fn recovered(
        forced_close: Option<ForcedClose>,
        symbol: SymbolId,
        side: Side,
        qty: f64,
    ) -> WalRecord {
        WalRecord::RecoveredFill {
            exec_id: "native-or-manual".into(),
            client_order_id: String::new(),
            symbol,
            side,
            qty,
            px: 99.0,
            fee: Some(0.0),
            is_maker: false,
            forced_close,
            venue_ts_ms: 2,
            recovered_wall_ts_ms: 3,
        }
    }

    #[test]
    fn a_fill_is_charged_to_the_strategy_that_sent_the_order() {
        let a = Attribution::from_records(&[sent("a", CARRY, BTC), fill("a", BTC, Side::Buy, 2.0)]);
        assert_eq!(a.signed(CARRY, BTC), 2.0);
        assert_eq!(a.signed(LONG, BTC), 0.0, "not the other strategy's");
    }

    #[test]
    fn one_strategys_holding_is_foreign_to_the_other() {
        let a = Attribution::from_records(&[sent("a", CARRY, BTC), fill("a", BTC, Side::Buy, 2.0)]);
        assert!(a.held_by_another(LONG, BTC), "carry is holding it");
        assert!(!a.held_by_another(CARRY, BTC), "your own is not foreign");
        assert!(!a.held_by_another(LONG, ETH), "nobody holds this one");
    }

    #[test]
    fn selling_out_hands_the_symbol_back() {
        let a = Attribution::from_records(&[
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            sent("b", CARRY, BTC),
            fill("b", BTC, Side::Sell, 2.0),
        ]);
        assert_eq!(a.signed(CARRY, BTC), 0.0);
        assert!(!a.held_by_another(LONG, BTC), "flat is not held");
    }

    #[test]
    fn a_fill_for_an_order_we_never_sent_is_charged_to_nobody() {
        // Somebody hand-trading the account. Guessing an owner would let one
        // strategy's plug size against a position it did not open.
        let a = Attribution::from_records(&[fill("stranger", BTC, Side::Buy, 5.0)]);
        assert_eq!(a.signed(CARRY, BTC), 0.0);
        assert_eq!(a.signed(LONG, BTC), 0.0);
        assert!(!a.held_by_another(LONG, BTC));
    }

    /// A blank id and no reason from the venue is a hand trade: somebody
    /// closing by hand on the same account. Nobody's.
    #[test]
    fn a_blank_fill_with_no_venue_reason_is_not_assigned_to_the_only_sleeve() {
        let log = vec![
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            recovered(None, BTC, Side::Sell, 1.0),
        ];
        let a = Attribution::from_records(&log);
        assert_eq!(a.signed(CARRY, BTC), 2.0);
        assert_eq!(a.signed(LONG, BTC), 0.0);
    }

    #[test]
    fn a_venue_stop_closes_the_position_of_the_sleeve_that_held_it() {
        let a = Attribution::from_records(&[
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 10.0),
            stop_fill(BTC, Side::Sell, 10.0),
        ]);
        assert_eq!(a.signed(CARRY, BTC), 0.0, "the stop closed carry's holding");
        assert!(!a.held_by_another(LONG, BTC), "the name is free again");
    }

    #[test]
    fn a_recovered_venue_stop_closes_the_same_position() {
        let a = Attribution::from_records(&[
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 10.0),
            recovered(Some(ForcedClose::Liquidation), BTC, Side::Sell, 10.0),
        ]);
        assert_eq!(a.signed(CARRY, BTC), 0.0);
    }

    #[test]
    fn a_forced_close_in_a_symbol_nobody_holds_is_charged_to_nobody() {
        let a = Attribution::from_records(&[stop_fill(BTC, Side::Sell, 10.0)]);
        assert_eq!(a.signed(CARRY, BTC), 0.0);
        assert_eq!(a.signed(LONG, BTC), 0.0);
    }

    #[test]
    fn a_forced_close_that_would_grow_the_claim_is_charged_to_nobody() {
        // A purchase against a long is not a close of it, whatever the venue
        // calls the row.
        let a = Attribution::from_records(&[
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 10.0),
            stop_fill(BTC, Side::Buy, 5.0),
        ]);
        assert_eq!(a.signed(CARRY, BTC), 10.0);
    }

    #[test]
    fn a_forced_close_in_a_symbol_two_sleeves_hold_is_charged_to_nobody() {
        let a = Attribution::from_records(&[
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 10.0),
            sent("b", LONG, BTC),
            fill("b", BTC, Side::Buy, 4.0),
            stop_fill(BTC, Side::Sell, 10.0),
        ]);
        assert_eq!(a.signed(CARRY, BTC), 10.0, "neither sleeve is charged");
        assert_eq!(a.signed(LONG, BTC), 4.0);
    }

    #[test]
    fn the_forced_close_rule_needs_a_blank_id_and_a_venue_reason() {
        let held =
            Attribution::from_records(&[sent("a", CARRY, BTC), fill("a", BTC, Side::Buy, 10.0)]);
        assert_eq!(
            forced_close_owner(&held, "", BTC, Side::Sell, Some(ForcedClose::StopLoss)),
            Some(CARRY)
        );
        assert_eq!(
            forced_close_owner(&held, "", BTC, Side::Sell, None),
            None,
            "no reason from the venue is a hand close"
        );
        assert_eq!(
            forced_close_owner(&held, "eng-9", BTC, Side::Sell, Some(ForcedClose::StopLoss)),
            None,
            "an id names an order, and the order ledger answers for those"
        );
        assert_eq!(
            forced_close_owner(&held, "", ETH, Side::Sell, Some(ForcedClose::StopLoss)),
            None,
            "nobody holds this one"
        );
    }

    #[test]
    fn the_log_rebuilds_what_a_restart_would_otherwise_forget() {
        // The whole reason this reads the log rather than starting empty: a
        // restart with positions on would leave every symbol looking free,
        // and the other sleeve would trade straight into it.
        let log = vec![
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            sent("b", LONG, ETH),
            fill("b", ETH, Side::Buy, 3.0),
        ];
        let after_restart = Attribution::from_records(&log);
        assert_eq!(after_restart.signed(CARRY, BTC), 2.0);
        assert_eq!(after_restart.signed(LONG, ETH), 3.0);
        assert_eq!(after_restart.symbols(CARRY).collect::<Vec<_>>(), [BTC]);
        assert_eq!(after_restart.symbols(LONG).collect::<Vec<_>>(), [ETH]);
        assert!(after_restart.held_by_another(LONG, BTC));
        assert!(after_restart.held_by_another(CARRY, ETH));
    }

    #[test]
    fn partial_fills_add_up_under_one_owner() {
        let a = Attribution::from_records(&[
            sent("a", LONG, ETH),
            fill("a", ETH, Side::Buy, 1.5),
            fill("a", ETH, Side::Buy, 0.5),
        ]);
        assert_eq!(a.signed(LONG, ETH), 2.0);
    }

    #[test]
    fn a_flat_symbol_loses_its_stale_claim() {
        // The residue case: carry bought, the position later closed by a
        // fill this log never charged (a hand close), and the leftover row
        // keeps every other sleeve out of the name.
        let mut a =
            Attribution::from_records(&[sent("a", CARRY, BTC), fill("a", BTC, Side::Buy, 2.0)]);
        assert!(
            a.held_by_another(LONG, BTC),
            "the residue blocks the other sleeve"
        );

        let dropped = a.drop_where_flat(|symbol| symbol == BTC);
        assert_eq!(
            dropped,
            vec![(CARRY, BTC, 2.0)],
            "the receipt says what was dropped"
        );
        assert!(!a.held_by_another(LONG, BTC), "flat cleared the claim");
        assert_eq!(a.signed(CARRY, BTC), 0.0);
    }

    #[test]
    fn a_held_symbol_keeps_its_claim() {
        let mut a = Attribution::from_records(&[
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            sent("b", LONG, ETH),
            fill("b", ETH, Side::Buy, 3.0),
        ]);
        let dropped = a.drop_where_flat(|symbol| symbol == BTC);
        assert_eq!(dropped, vec![(CARRY, BTC, 2.0)]);
        assert_eq!(a.signed(LONG, ETH), 3.0, "the held name is untouched");
        assert!(a.held_by_another(CARRY, ETH));
    }

    #[test]
    fn a_replayed_drop_keeps_the_residue_from_coming_back() {
        // The wedge this record exists for: boot dropped the claim against a
        // flat venue, the other sleeve entered the name, and the next boot
        // replays the same old fills — with the symbol now held, a flat
        // sweep can never fire again.
        let log = vec![
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            WalRecord::ClaimsDropped {
                wall_ts_ms: 2,
                rows: vec![engine_types::FilledTotal {
                    strategy: CARRY,
                    symbol: BTC,
                    signed_qty: 2.0,
                }],
            },
            sent("b", LONG, BTC),
            fill("b", BTC, Side::Buy, 0.5),
        ];
        let a = Attribution::from_records(&log);
        assert_eq!(
            a.signed(CARRY, BTC),
            0.0,
            "the drop replays like everything else"
        );
        assert_eq!(
            a.signed(LONG, BTC),
            0.5,
            "fills after the drop charge normally"
        );
        assert!(
            !a.held_by_another(LONG, BTC),
            "the residue must not lock the new owner out"
        );
    }

    #[test]
    fn an_operator_restatement_clears_claims_the_venue_reports_flat() {
        // `engine reconcile-clear` restated the account to the venue's own
        // positions. A symbol that restatement reports flat is nobody's,
        // whatever the fills before it summed to; a symbol it still shows
        // held keeps its claims.
        let log = vec![
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            sent("b", LONG, ETH),
            fill("b", ETH, Side::Buy, 3.0),
            WalRecord::LatchCleared {
                wall_ts_ms: 2,
                note: "operator looked".to_string(),
                restated_exposure: vec![engine_types::SymbolTotal {
                    symbol: ETH,
                    signed_qty: 3.0,
                }],
                findings: Vec::new(),
            },
        ];
        let a = Attribution::from_records(&log);
        assert_eq!(
            a.signed(CARRY, BTC),
            0.0,
            "flat in the restatement clears the claim"
        );
        assert!(!a.held_by_another(LONG, BTC));
        assert_eq!(a.signed(LONG, ETH), 3.0, "held in the restatement keeps it");
        assert!(a.held_by_another(CARRY, ETH));
    }
}
