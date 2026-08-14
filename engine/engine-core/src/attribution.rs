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

use engine_types::{OrderUpdate, Side, StrategyId, SymbolId, WalRecord};

/// The order id of a fill, and nothing else's.
fn fill_id(update: &OrderUpdate) -> Option<&str> {
    match update {
        OrderUpdate::Fill {
            client_order_id, ..
        } => Some(client_order_id.as_str()),
        _ => None,
    }
}

/// Smaller than this is flat. A venue position is a whole number of quantity
/// steps, so anything under it is this sum's own rounding rather than a
/// holding — the same reasoning as `reconcile`'s tolerance, one order of
/// magnitude coarser than nothing.
const FLAT: f64 = 1e-9;

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
                WalRecord::OrderUpdate { update } => {
                    let Some(id) = fill_id(update) else { continue };
                    // A fill for an order this log never recorded sending
                    // belongs to somebody else on the account. Charged to
                    // nobody on purpose: the engine does not guess whose it
                    // is, and `reconcile` is what notices the account holds
                    // more than the log accounts for.
                    let Some(strategy) = sender.get(id).copied() else {
                        continue;
                    };
                    me.on_update(strategy, update);
                }
                _ => {}
            }
        }
        me
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
        // An unreal number would poison the running total for good.
        if !qty.is_finite() {
            return;
        }
        let signed = match side {
            Side::Buy => *qty,
            Side::Sell => -*qty,
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
            },
            wire_ns: 1,
        }
    }

    fn fill(id: &str, symbol: SymbolId, side: Side, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                client_order_id: id.to_string(),
                symbol,
                side,
                qty,
                px: 100.0,
                fee: 0.0,
                venue_ts_ms: 1,
                recv_ns: 1,
            },
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
}
