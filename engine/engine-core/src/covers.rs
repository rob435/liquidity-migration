//! What each strategy has sent that the account reading has not yet shown.
//!
//! A resting order covers the gap between sending and filling, but not the
//! gap between filling and *seeing* the fill: the order ledger ends a fully
//! filled order at once, while the account reading refreshes on the engine's
//! own schedule and can be seconds behind. In that window the position exists
//! at the venue and shows nowhere a strategy can look, and a strategy that
//! decides from "target minus position" sends the same order twice.
//!
//! The target-book follower used to keep this memory for itself. It is an
//! execution problem, not a strategy decision, so the engine keeps it now —
//! one book, keyed per (strategy, symbol), fed from the engine's own
//! lifecycle knowledge and read back through `StrategyCtx::in_flight`:
//!
//! - **Booked at the send**, with the quantized size that actually went out —
//!   truer than the size the strategy asked for, so no ack ever needs to
//!   clamp it down.
//! - **A reject releases the whole send.** It never worked and none of it
//!   filled.
//! - **A cancel releases only the unfilled remainder.** The filled part stays
//!   covered until the reading shows it.
//! - **The reading moving toward a send absorbs it**, record by record, by
//!   exactly the amount shown; a partial catch-up shrinks a cover instead of
//!   dropping it. The reading moving *against* a send is describing something
//!   newer — a stop, a hand close, an exit landing — so the record is dropped
//!   rather than double-counted.
//! - **A refused reduce-only intent drops every cover on its symbol.** The
//!   engine refusing an exit means the covers and the reading disagree about
//!   what is held, and the reading is the fact; anything less re-plans the
//!   same doomed exit from the surviving phantom on every quote.
//! - **A refused entry releases nothing, because nothing was ever booked.**
//!   Covers are booked at the send and a refusal happens before it, so the
//!   refused intent has no cover — and releasing "the newest" here would free
//!   a live cover still bridging an earlier fill.
//!
//! Covers are deliberately not rebuilt from the log at boot: boot compares
//! the log against the venue directly, which is a better answer than a memory
//! of what was in flight, and the follower's own records never survived a
//! restart either.

use engine_types::{AccountView, Side, StrategyId, SymbolId};

/// Below this a cover record is bookkeeping noise, not exposure.
const QTY_EPS: f64 = 1e-12;

/// One send the reading has not caught up with, and the reading it went out
/// against. When the reading moves off `view_at_send` it has taken that much
/// into account and the record shrinks — no timer, no guess.
#[derive(Copy, Clone, Debug)]
struct Cover {
    strategy: StrategyId,
    symbol: SymbolId,
    view_at_send: f64,
    /// Signed quantity still covered. Positive is long.
    sent: f64,
}

/// The engine's in-flight position accounting, per (strategy, symbol).
#[derive(Default, Debug)]
pub struct CoverBook {
    /// In send order, oldest first. Strategies never share a symbol by
    /// config rule; if two ever held covers on one, the reading's movement is
    /// applied to every record in send order whichever strategy holds it —
    /// deterministic, and the disagreement is reconcile's to notice.
    records: Vec<Cover>,
}

/// What the reading says is held in this symbol, signed. Positive is long,
/// and a flat or missing row is zero.
fn view_signed(account: &AccountView, symbol: SymbolId) -> f64 {
    account
        .positions
        .iter()
        .find(|p| p.symbol == symbol)
        .map(|p| if p.side == Side::Buy { p.qty } else { -p.qty })
        .unwrap_or(0.0)
}

impl CoverBook {
    /// Book a cover for an order the engine is handing to the venue. `qty`
    /// is the quantized size actually going out, unsigned; the side signs it.
    /// The account reading anchors the record, so a later reading can say how
    /// much of this send it has absorbed.
    pub fn register(
        &mut self,
        strategy: StrategyId,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        account: &AccountView,
    ) {
        let sent = match side {
            Side::Buy => qty,
            Side::Sell => -qty,
        };
        self.records.push(Cover {
            strategy,
            symbol,
            view_at_send: view_signed(account, symbol),
            sent,
        });
    }

    /// Signed quantity this strategy has sent that the reading has not
    /// absorbed yet. What `StrategyCtx::in_flight` answers.
    pub fn in_flight(&self, strategy: StrategyId, symbol: SymbolId) -> f64 {
        self.records
            .iter()
            .filter(|r| r.strategy == strategy && r.symbol == symbol)
            .map(|r| r.sent)
            .sum()
    }

    /// Terminal news ended `qty` of a send without a fill: the whole size on
    /// a reject, the unfilled remainder on a cancel. It comes off the newest
    /// cover, because that is the send the news is about — an older record
    /// may still be bridging a fill the reading has not caught up with.
    pub fn release_newest(&mut self, strategy: StrategyId, symbol: SymbolId, qty: f64) {
        let Some(at) = self
            .records
            .iter()
            .rposition(|r| r.strategy == strategy && r.symbol == symbol)
        else {
            return;
        };
        let record = &mut self.records[at];
        let eaten = qty.min(record.sent.abs());
        record.sent -= eaten * record.sent.signum();
        if record.sent.abs() <= QTY_EPS {
            self.records.remove(at);
        }
    }

    /// An intent died inside the engine before it became an order.
    ///
    /// Refused reduce-only: every cover for the symbol goes — the reading is
    /// the fact about what is held, and it just said there is nothing to
    /// reduce. Refused entry: nothing to do, and deliberately so; see the
    /// module note on why releasing "the newest" here would be wrong.
    pub fn intent_refused(&mut self, strategy: StrategyId, symbol: SymbolId, reduce_only: bool) {
        if reduce_only {
            self.records
                .retain(|r| !(r.strategy == strategy && r.symbol == symbol));
        }
    }

    /// A fresh account reading. A reading that has moved since a send has
    /// taken that much of the send into account: each cover shrinks by
    /// exactly what the reading absorbed, and only what it has not yet shown
    /// stays covered.
    pub fn absorb(&mut self, account: &AccountView) {
        self.records.retain_mut(|record| {
            let now = view_signed(account, record.symbol);
            let moved = now - record.view_at_send;
            if moved == 0.0 {
                return true;
            }
            if moved.signum() == record.sent.signum() {
                let eaten = moved.abs().min(record.sent.abs());
                record.sent -= eaten * record.sent.signum();
                record.view_at_send = now;
                record.sent.abs() > QTY_EPS
            } else {
                // The reading moved AGAINST the send: it is describing
                // something newer than this record, so it has manifestly
                // caught up, and keeping the record against a reading it no
                // longer matches would double-count.
                false
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::PositionView;

    const CARRY: StrategyId = StrategyId(0);
    const LONG: StrategyId = StrategyId(1);
    const KAITO: SymbolId = SymbolId(3);
    const COTI: SymbolId = SymbolId(4);

    fn reading(rows: &[(SymbolId, f64)]) -> AccountView {
        AccountView {
            equity_usdt: 1_000.0,
            available_usdt: 1_000.0,
            positions: rows
                .iter()
                .map(|(symbol, signed)| PositionView {
                    symbol: *symbol,
                    side: if *signed >= 0.0 { Side::Buy } else { Side::Sell },
                    qty: signed.abs(),
                    entry_px: 10.0,
                    stop_attached: true,
                    leverage: None,
                })
                .collect(),
            observed_ns: 1,
        }
    }

    fn flat() -> AccountView {
        reading(&[])
    }

    #[test]
    fn a_cover_is_booked_signed_and_read_back_per_strategy_and_symbol() {
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &flat());
        book.register(CARRY, COTI, Side::Sell, 4.0, &flat());
        assert_eq!(book.in_flight(CARRY, KAITO), 10.0);
        assert_eq!(book.in_flight(CARRY, COTI), -4.0, "a sell covers short");
        assert_eq!(book.in_flight(LONG, KAITO), 0.0, "not the other strategy's");
    }

    #[test]
    fn the_reading_absorbing_part_of_a_send_shrinks_the_cover_by_that_much() {
        // A partial catch-up must shrink, not drop: dropping the whole record
        // left the unseen remainder uncovered, and the plug bought it again.
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &flat());
        book.absorb(&reading(&[(KAITO, 5.0)]));
        assert_eq!(book.in_flight(CARRY, KAITO), 5.0);
        book.absorb(&reading(&[(KAITO, 10.0)]));
        assert_eq!(book.in_flight(CARRY, KAITO), 0.0, "fully shown, fully released");
    }

    #[test]
    fn a_reading_that_moves_against_a_send_drops_the_record() {
        // A stop fired or somebody closed by hand: the reading is describing
        // something newer than the send, so it has manifestly caught up.
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &reading(&[(KAITO, 2.0)]));
        book.absorb(&reading(&[(KAITO, 1.0)]));
        assert_eq!(book.in_flight(CARRY, KAITO), 0.0);
    }

    #[test]
    fn an_unmoved_reading_keeps_the_cover_whole() {
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &reading(&[(KAITO, 2.0)]));
        book.absorb(&reading(&[(KAITO, 2.0)]));
        assert_eq!(book.in_flight(CARRY, KAITO), 10.0);
    }

    #[test]
    fn a_reject_releases_the_whole_send() {
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &flat());
        book.release_newest(CARRY, KAITO, 10.0);
        assert_eq!(book.in_flight(CARRY, KAITO), 0.0);
    }

    #[test]
    fn a_cancel_releases_only_the_unfilled_remainder() {
        // Pulled after filling 4 of its 10: the 6 that never happened are
        // freed, the filled 4 stay covered until the reading shows them.
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &flat());
        book.release_newest(CARRY, KAITO, 6.0);
        assert_eq!(book.in_flight(CARRY, KAITO), 4.0);
    }

    #[test]
    fn a_release_comes_off_the_newest_cover_and_leaves_older_ones_bridging() {
        // The news is about the newest send; an older record may still be
        // bridging a fill the reading has not caught up with.
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &flat());
        book.register(CARRY, KAITO, Side::Buy, 5.0, &flat());
        book.release_newest(CARRY, KAITO, 5.0);
        assert_eq!(book.in_flight(CARRY, KAITO), 10.0, "the older cover is intact");
    }

    #[test]
    fn a_refused_exit_drops_every_cover_for_the_symbol_and_nothing_else() {
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &flat());
        book.register(CARRY, KAITO, Side::Buy, 5.0, &flat());
        book.register(CARRY, COTI, Side::Buy, 3.0, &flat());
        book.intent_refused(CARRY, KAITO, true);
        assert_eq!(book.in_flight(CARRY, KAITO), 0.0, "the reading is the fact");
        assert_eq!(book.in_flight(CARRY, COTI), 3.0, "the other symbol keeps its cover");
    }

    #[test]
    fn a_refused_entry_releases_nothing_because_its_cover_was_never_booked() {
        // Covers are booked at the send and a refusal happens before it, so
        // the refused intent has no cover. Releasing "the newest" here would
        // free a live cover still bridging an earlier fill — the double-entry
        // window all over again.
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &flat());
        book.intent_refused(CARRY, KAITO, false);
        assert_eq!(book.in_flight(CARRY, KAITO), 10.0, "the earlier send stays covered");
    }

    #[test]
    fn the_readings_movement_lands_on_the_strategy_holding_the_covers() {
        // Two strategies, two symbols, one reading: each cover measures its
        // own symbol against the reading, so the movement cannot leak across.
        let mut book = CoverBook::default();
        book.register(CARRY, KAITO, Side::Buy, 10.0, &flat());
        book.register(LONG, COTI, Side::Buy, 8.0, &flat());
        book.absorb(&reading(&[(KAITO, 10.0)]));
        assert_eq!(book.in_flight(CARRY, KAITO), 0.0);
        assert_eq!(book.in_flight(LONG, COTI), 8.0, "the other sleeve's cover is untouched");
    }
}
