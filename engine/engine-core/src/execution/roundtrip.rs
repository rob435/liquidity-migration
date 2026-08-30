//! What a position made, from the fill that opened it to the fill that closed
//! it.
//!
//! [`super::Fills`] answers what the trading cost, fill by fill. This answers
//! the other question a person asks: the sleeve is out of that coin now — did
//! it make money. Both ride on the same fill, so the two can never describe
//! different trading.
//!
//! ## Cash is the whole of the arithmetic
//!
//! A buy pays out and a sell takes in. Summed with that sign over every fill
//! of a position, `cash` IS the round trip's gross the moment the quantity
//! comes back to zero. Long and short need no separate cases, and no sign can
//! be got the wrong way round.
//!
//! ## What "net" leaves out
//!
//! Net is what the position closed at against what it opened at, less what
//! the venue charged for both — every term a receipt out of this engine's own
//! log. **The crowd fee (funding) is not in it.** The venue settles that into
//! the wallet on its own eight-hourly clock and tells this engine nothing,
//! so a number claiming to carry it would be an estimate dressed as a
//! receipt. `docs/notifications.md` says so where the owner reads the number.

use std::collections::BTreeMap;

use engine_types::Side;
use serde::Serialize;

use super::{arrival_shortfall_bps, Fill, Weighted};

/// Smaller than this is flat — the same value and the same reasoning as
/// `attribution`'s: a venue position is a whole number of quantity steps, so
/// anything under it is this sum's own rounding.
const FLAT: f64 = 1e-9;

/// A position a sleeve is now out of, and what it came to.
///
/// These field names are a contract with
/// `scripts/runtime/notify_book_changes.py`, which reads them off disk as one
/// JSON line and puts them on the owner's phone.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct ClosedTrade {
    pub sleeve: String,
    pub symbol: String,
    /// The side it was held on, spelled the way the heartbeat's position rows
    /// spell it.
    pub side: &'static str,
    /// What was closed.
    pub qty: f64,
    pub exit_px: f64,
    pub closed_ms: i64,
    pub fills: u64,
    /// Share of this trip's notional that rested.
    pub maker_share: Option<f64>,
    /// How far the fills landed from the price on the screen when their
    /// orders left. Positive is adverse.
    pub arrival_shortfall_bps: Option<f64>,
    /// Exit against entry before fees. Present whenever this log saw both
    /// sides, even when the venue omitted a fee.
    pub gross_usdt: Option<f64>,
    /// Total venue fee when every contributing fill stated one. `None` is an
    /// unknown fee, not a numeric zero.
    pub fees_usdt: Option<f64>,
    /// What it made — absent when this log does not hold the fills that
    /// opened it, which is what a rotation leaves behind. The close is still
    /// worth saying then; what it made is not knowable from here, and a
    /// number invented for the gap would be worse than the gap.
    pub round_trip: Option<RoundTrip>,
}

/// The money, present only when both legs of the trip are in the log.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct RoundTrip {
    pub entry_px: f64,
    pub entry_notional_usdt: f64,
    /// Exit against entry, before the venue's charges.
    pub gross_usdt: f64,
    /// What the venue charged over both legs. Negative is a rebate.
    pub fees_usdt: f64,
    /// `gross_usdt - fees_usdt`, and no crowd fee (module note).
    pub net_usdt: f64,
    /// `net_usdt` against what went in, in basis points.
    pub net_bps: f64,
    pub opened_ms: i64,
    pub held_ms: i64,
}

/// One sleeve's open position in one symbol.
#[derive(Clone, Debug)]
struct Lot {
    signed_qty: f64,
    /// +1 while held long, −1 short. `signed_qty` is zero by the time the
    /// trip closes, so the side it was held on has to be remembered.
    held: f64,
    cash: f64,
    in_qty: f64,
    in_value: f64,
    out_qty: f64,
    out_value: f64,
    fees: Option<f64>,
    fills: u64,
    notional: f64,
    maker_notional: f64,
    shortfall: Weighted,
    opened_ms: i64,
    /// Whether this reader watched the position open.
    ///
    /// False for one restated across a log rotation, and it stays false for
    /// the rest of that position's life however much is added to it: `cash`
    /// never saw what the earlier segment paid, so the difference at the end
    /// is not what the trip made. Being out by a whole entry is not a small
    /// error — a coin that doubled reads as a profit twice over.
    priced: bool,
}

impl Default for Lot {
    fn default() -> Self {
        Lot {
            signed_qty: 0.0,
            held: 0.0,
            cash: 0.0,
            in_qty: 0.0,
            in_value: 0.0,
            out_qty: 0.0,
            out_value: 0.0,
            fees: Some(0.0),
            fills: 0,
            notional: 0.0,
            maker_notional: 0.0,
            shortfall: Weighted::default(),
            opened_ms: 0,
            priced: false,
        }
    }
}

impl Lot {
    /// Fold in `qty` of a fill — all of it, or the part of it that belongs to
    /// this lot when one fill takes a position through zero.
    fn fold(&mut self, fill: &Fill, qty: f64) {
        let signed = match fill.side {
            Side::Buy => qty,
            Side::Sell => -qty,
        };
        let value = fill.px * qty;
        if self.signed_qty == 0.0 {
            self.held = signed.signum();
            self.opened_ms = fill.venue_ts_ms;
            self.priced = true;
        }
        if signed.signum() == self.held {
            self.in_qty += qty;
            self.in_value += value;
        } else {
            self.out_qty += qty;
            self.out_value += value;
        }
        self.signed_qty += signed;
        self.cash -= signed * fill.px;
        self.fills += 1;
        self.notional += value;
        if fill.is_maker {
            self.maker_notional += value;
        }
        // Charged pro rata, because a fill split across two lots was one
        // charge at the venue.
        self.fees = match (self.fees, fill.fee.filter(|fee| fee.is_finite())) {
            (Some(total), Some(fee)) if fill.qty > 0.0 => Some(total + fee * (qty / fill.qty)),
            _ => None,
        };
        if let Some(bps) = arrival_shortfall_bps(fill.side, fill.px, fill.arrival_mid) {
            self.shortfall.add(bps, value);
        }
    }

    fn flat(&self) -> bool {
        self.signed_qty.abs() < FLAT
    }

    fn closed(&self, sleeve: &str, symbol: &str, closed_ms: i64) -> ClosedTrade {
        let priced = self.priced && self.in_qty > 0.0 && self.in_value > 0.0;
        ClosedTrade {
            sleeve: sleeve.to_string(),
            symbol: symbol.to_string(),
            side: if self.held < 0.0 { "short" } else { "long" },
            qty: self.out_qty,
            exit_px: self.out_value / self.out_qty,
            closed_ms,
            fills: self.fills,
            maker_share: (self.notional > 0.0).then(|| self.maker_notional / self.notional),
            arrival_shortfall_bps: self.shortfall.mean(),
            gross_usdt: priced.then_some(self.cash),
            fees_usdt: priced.then_some(self.fees).flatten(),
            round_trip: priced.then_some(self.fees).flatten().map(|fees| {
                let net = self.cash - fees;
                RoundTrip {
                    entry_px: self.in_value / self.in_qty,
                    entry_notional_usdt: self.in_value,
                    gross_usdt: self.cash,
                    fees_usdt: fees,
                    net_usdt: net,
                    net_bps: 10_000.0 * net / self.in_value,
                    opened_ms: self.opened_ms,
                    held_ms: closed_ms - self.opened_ms,
                }
            }),
        }
    }
}

/// Every sleeve's open position, and the trips that have closed.
///
/// Keyed by the sleeve's and the coin's names for the reason [`super::Fills`]
/// is: an id is a place in a table rebuilt every boot, and a position outlives
/// boots.
#[derive(Debug, Default)]
pub struct Lots {
    open: BTreeMap<(String, String), Lot>,
    closed: Vec<ClosedTrade>,
}

impl Lots {
    /// Fold one fill into the sleeve's position in this symbol, closing the
    /// trip if it takes the position to flat.
    pub fn on_fill(&mut self, sleeve: &str, symbol: &str, fill: &Fill) {
        if !fill.qty.is_finite() || fill.qty <= 0.0 || !fill.px.is_finite() || fill.px <= 0.0 {
            return;
        }
        let key = (sleeve.to_string(), symbol.to_string());
        let held = self.open.get(&key).map_or(0.0, |lot| lot.signed_qty);
        let signed = match fill.side {
            Side::Buy => fill.qty,
            Side::Sell => -fill.qty,
        };
        // A fill that takes the position through zero is two fills: it closes
        // what was open and opens the rest the other way. Splitting it is what
        // keeps the two trips' entry prices apart.
        let closing = if held != 0.0 && signed.signum() != held.signum() {
            fill.qty.min(held.abs())
        } else {
            0.0
        };
        if closing > 0.0 {
            let lot = self.open.entry(key.clone()).or_default();
            lot.fold(fill, closing);
            if lot.flat() {
                let trade = lot.closed(&key.0, &key.1, fill.venue_ts_ms);
                self.open.remove(&key);
                self.closed.push(trade);
            }
        }
        let opening = fill.qty - closing;
        if opening > 0.0 {
            self.open.entry(key).or_default().fold(fill, opening);
        }
    }

    /// Restate every position from a rotation's own account of them.
    ///
    /// A new segment's first record says what each sleeve holds, and that is
    /// the only place a position opened before the rotation survives: the
    /// fills that opened it are in a segment boot never reads. It is restated
    /// as **a quantity with no entry price** — what it will make cannot be
    /// known from here, and the alternative is not merely a gap. Left out,
    /// the sale that closes such a position reads as opening a short, and the
    /// purchase that opens the NEXT position closes that phantom and reports
    /// a profit nobody made.
    ///
    /// "Set", not "add", exactly as the record's own contract says: a sleeve
    /// absent from the restatement is flat.
    pub fn restate(&mut self, held: &[(String, String, f64)]) {
        self.open.clear();
        for (sleeve, symbol, signed_qty) in held {
            if !signed_qty.is_finite() || signed_qty.abs() < FLAT {
                continue;
            }
            self.open.insert(
                (sleeve.clone(), symbol.clone()),
                Lot {
                    signed_qty: *signed_qty,
                    held: signed_qty.signum(),
                    ..Lot::default()
                },
            );
        }
    }

    /// Forget a sleeve's position without reporting a trip, for the symbols
    /// boot found the venue holding nothing of.
    ///
    /// The same act as `attribution`'s own drop, and for the same reason: the
    /// close happened somewhere this log cannot see — a venue stop firing
    /// under the position, an inherited holding wound down — so there is no
    /// exit price to report and inventing one would be worse than saying
    /// nothing.
    pub fn drop_symbols(&mut self, dropped: impl Fn(&str, &str) -> bool) {
        self.open
            .retain(|(sleeve, symbol), _| !dropped(sleeve, symbol));
    }

    /// Every trip that has closed and not yet been taken.
    pub fn closed(&self) -> &[ClosedTrade] {
        &self.closed
    }

    /// The trips that closed since this was last asked, and clear them.
    pub fn take_closed(&mut self) -> Vec<ClosedTrade> {
        std::mem::take(&mut self.closed)
    }

    /// How many positions are open. The rebuild at boot is judged by it.
    pub fn open(&self) -> usize {
        self.open.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{StrategyId, SymbolId};

    fn fill(side: Side, px: f64, qty: f64, fee: f64, ts_ms: i64) -> Fill {
        Fill {
            client_order_id: "eng-1".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side,
            qty,
            px,
            fee: Some(fee),
            is_maker: false,
            arrival_mid: 0.0,
            venue_ts_ms: ts_ms,
        }
    }

    fn one(lots: &mut Lots) -> ClosedTrade {
        let mut closed = lots.take_closed();
        assert_eq!(closed.len(), 1, "exactly one trip should have closed");
        closed.remove(0)
    }

    #[test]
    fn a_long_bought_low_and_sold_high_made_the_difference() {
        let mut lots = Lots::default();
        lots.on_fill(
            "carry",
            "ONGUSDT",
            &fill(Side::Buy, 0.30, 1_000.0, 0.10, 1_000),
        );
        assert!(lots.take_closed().is_empty(), "still holding");
        lots.on_fill(
            "carry",
            "ONGUSDT",
            &fill(Side::Sell, 0.33, 1_000.0, 0.11, 8_000),
        );

        let trade = one(&mut lots);
        assert_eq!(trade.side, "long");
        assert_eq!(trade.qty, 1_000.0);
        assert_eq!(trade.fills, 2);
        let rt = trade.round_trip.expect("both legs are here");
        assert_eq!(rt.entry_px, 0.30);
        assert_eq!(trade.exit_px, 0.33);
        assert!((rt.gross_usdt - 30.0).abs() < 1e-9, "{}", rt.gross_usdt);
        assert!((rt.fees_usdt - 0.21).abs() < 1e-9, "{}", rt.fees_usdt);
        assert!((rt.net_usdt - 29.79).abs() < 1e-9, "{}", rt.net_usdt);
        assert!((rt.net_bps - 993.0).abs() < 1.0, "{}", rt.net_bps);
        assert_eq!(rt.held_ms, 7_000);
    }

    #[test]
    fn a_closed_trip_with_an_unstated_fee_keeps_gross_but_not_invents_net() {
        let mut lots = Lots::default();
        lots.on_fill("long", "BTCUSDT", &fill(Side::Buy, 100.0, 1.0, 0.10, 1_000));
        let mut close = fill(Side::Sell, 110.0, 1.0, 0.0, 2_000);
        close.fee = None;
        lots.on_fill("long", "BTCUSDT", &close);

        let trade = one(&mut lots);
        assert_eq!(trade.gross_usdt, Some(10.0));
        assert_eq!(trade.fees_usdt, None);
        assert_eq!(
            trade.round_trip, None,
            "net and net basis points require every fee"
        );
    }

    /// The sign that a per-side ledger gets wrong: a short makes money when
    /// the price falls.
    #[test]
    fn a_short_covered_lower_made_money() {
        let mut lots = Lots::default();
        lots.on_fill(
            "exodus",
            "MOVEUSDT",
            &fill(Side::Sell, 0.50, 200.0, 0.05, 0),
        );
        lots.on_fill(
            "exodus",
            "MOVEUSDT",
            &fill(Side::Buy, 0.45, 200.0, 0.05, 60_000),
        );

        let trade = one(&mut lots);
        assert_eq!(trade.side, "short");
        let rt = trade.round_trip.expect("both legs are here");
        assert_eq!(rt.entry_px, 0.50);
        assert_eq!(trade.exit_px, 0.45);
        assert!((rt.gross_usdt - 10.0).abs() < 1e-9, "{}", rt.gross_usdt);
        assert!((rt.net_usdt - 9.90).abs() < 1e-9, "{}", rt.net_usdt);
        assert!(rt.net_bps > 0.0, "a short that fell is a gain");
    }

    #[test]
    fn scaling_in_averages_the_entry_and_holds_the_first_clock() {
        let mut lots = Lots::default();
        lots.on_fill("long", "SOLUSDT", &fill(Side::Buy, 100.0, 1.0, 0.0, 500));
        lots.on_fill("long", "SOLUSDT", &fill(Side::Buy, 120.0, 3.0, 0.0, 900));
        lots.on_fill("long", "SOLUSDT", &fill(Side::Sell, 130.0, 4.0, 0.0, 5_500));

        let trade = one(&mut lots);
        let rt = trade.round_trip.expect("both legs are here");
        assert_eq!(rt.entry_px, 115.0, "volume weighted, not the first price");
        assert_eq!(rt.opened_ms, 500, "the clock starts at the first fill");
        assert_eq!(rt.held_ms, 5_000);
        assert_eq!(trade.fills, 3);
        assert!((rt.gross_usdt - 60.0).abs() < 1e-9, "{}", rt.gross_usdt);
    }

    #[test]
    fn a_position_half_sold_reports_nothing_until_it_is_flat() {
        let mut lots = Lots::default();
        lots.on_fill("long", "BTCUSDT", &fill(Side::Buy, 100.0, 2.0, 0.0, 0));
        lots.on_fill("long", "BTCUSDT", &fill(Side::Sell, 110.0, 1.0, 0.0, 100));
        assert!(lots.take_closed().is_empty(), "half out is not out");
        assert_eq!(lots.open(), 1);

        lots.on_fill("long", "BTCUSDT", &fill(Side::Sell, 120.0, 1.0, 0.0, 200));
        let trade = one(&mut lots);
        assert_eq!(trade.exit_px, 115.0, "both exits, volume weighted");
        assert_eq!(lots.open(), 0);
    }

    /// A rotation leaves boot replaying a segment that holds the close and not
    /// the open. The close is still worth saying; what it made is not
    /// knowable, and a number invented for the gap would be worse.
    #[test]
    fn a_close_whose_entry_is_not_in_the_log_reports_no_money() {
        let mut lots = Lots::default();
        lots.on_fill(
            "long",
            "HYPEUSDT",
            &fill(Side::Sell, 40.0, 5.0, 0.02, 7_000),
        );

        // The sell opened a short as far as this log can tell, so nothing
        // closed. What the notifier must never see is a trip claiming a
        // profit measured against an entry price of zero.
        assert!(lots.take_closed().is_empty());
        assert_eq!(lots.open(), 1);
    }

    #[test]
    fn maker_share_is_by_notional_not_by_fill_count() {
        let mut lots = Lots::default();
        let mut resting = fill(Side::Buy, 10.0, 1.0, 0.0, 0);
        resting.is_maker = true;
        lots.on_fill("long", "ETHUSDT", &resting);
        lots.on_fill("long", "ETHUSDT", &fill(Side::Buy, 10.0, 9.0, 0.0, 1));
        lots.on_fill("long", "ETHUSDT", &fill(Side::Sell, 10.0, 10.0, 0.0, 2));

        let trade = one(&mut lots);
        let share = trade.maker_share.expect("something traded");
        assert!((share - 0.05).abs() < 1e-9, "{share}");
    }

    /// One fill that takes a position through zero is two trips, and the
    /// second must not inherit the first's entry price.
    #[test]
    fn a_fill_through_zero_closes_one_trip_and_opens_the_next() {
        let mut lots = Lots::default();
        lots.on_fill("long", "XRPUSDT", &fill(Side::Buy, 2.0, 100.0, 0.0, 0));
        lots.on_fill("long", "XRPUSDT", &fill(Side::Sell, 3.0, 150.0, 0.0, 1_000));

        let trade = one(&mut lots);
        assert_eq!(trade.qty, 100.0, "only what was open is closed");
        let rt = trade.round_trip.expect("both legs are here");
        assert!((rt.gross_usdt - 100.0).abs() < 1e-9, "{}", rt.gross_usdt);
        assert_eq!(lots.open(), 1, "the other 50 opened a short");
    }

    #[test]
    fn two_sleeves_in_one_coin_keep_their_own_trips() {
        let mut lots = Lots::default();
        lots.on_fill("carry", "AGIUSDT", &fill(Side::Buy, 1.0, 10.0, 0.0, 0));
        lots.on_fill("long", "AGIUSDT", &fill(Side::Buy, 2.0, 10.0, 0.0, 0));
        lots.on_fill("carry", "AGIUSDT", &fill(Side::Sell, 1.5, 10.0, 0.0, 100));

        let trade = one(&mut lots);
        assert_eq!(trade.sleeve, "carry");
        let rt = trade.round_trip.expect("both legs are here");
        assert_eq!(rt.entry_px, 1.0, "long's entry is not carry's");
        assert_eq!(lots.open(), 1);
    }

    /// The rotation trap, and the reason [`Lots::restate`] exists. Without
    /// the restated quantity, the sale that closes a position opened in an
    /// earlier segment reads as opening a short, and the purchase that opens
    /// the NEXT position closes that phantom and books a profit nobody made.
    #[test]
    fn a_position_restated_across_a_rotation_does_not_invert_into_a_short() {
        let mut lots = Lots::default();
        lots.restate(&[("carry".into(), "ONGUSDT".into(), 5_056.0)]);
        assert_eq!(lots.open(), 1);

        // Carry sells out at 0.0886, and hours later buys back in at 0.068.
        lots.on_fill(
            "carry",
            "ONGUSDT",
            &fill(Side::Sell, 0.0886, 5_056.0, 0.24, 1_000),
        );
        let trade = one(&mut lots);
        assert_eq!(
            trade.side, "long",
            "it was long, whatever the closing fill was"
        );
        assert!(
            trade.round_trip.is_none(),
            "the entry is in the segment before this one: {:?}",
            trade.round_trip
        );

        lots.on_fill(
            "carry",
            "ONGUSDT",
            &fill(Side::Buy, 0.068, 7_347.0, 0.27, 80_000),
        );
        assert!(
            lots.take_closed().is_empty(),
            "a fresh entry closes nothing"
        );
        assert_eq!(lots.open(), 1);
    }

    /// Scaling into a restated position does not make it priceable. `cash`
    /// never saw what the earlier segment paid, so the difference at the end
    /// is not what the trip made.
    #[test]
    fn adding_to_a_restated_position_still_reports_no_money() {
        let mut lots = Lots::default();
        lots.restate(&[("carry".into(), "ACEUSDT".into(), 500.0)]);
        lots.on_fill(
            "carry",
            "ACEUSDT",
            &fill(Side::Buy, 0.21, 443.0, 0.05, 1_000),
        );
        lots.on_fill(
            "carry",
            "ACEUSDT",
            &fill(Side::Sell, 0.236, 943.0, 0.12, 9_000),
        );

        let trade = one(&mut lots);
        assert!(
            trade.round_trip.is_none(),
            "half the entry is missing, so the whole number is: {:?}",
            trade.round_trip
        );
        assert_eq!(trade.qty, 943.0, "what closed is still known");
    }

    /// A restatement is "set", not "add": a sleeve absent from it is flat.
    #[test]
    fn a_restatement_replaces_every_position() {
        let mut lots = Lots::default();
        lots.on_fill("long", "BTCUSDT", &fill(Side::Buy, 100.0, 1.0, 0.0, 0));
        lots.restate(&[("carry".into(), "ONGUSDT".into(), 10.0)]);
        assert_eq!(lots.open(), 1, "long's BTC is gone, carry's ONG is there");
    }

    #[test]
    fn a_dropped_claim_leaves_no_position_waiting_for_an_exit() {
        let mut lots = Lots::default();
        lots.on_fill("carry", "ACEUSDT", &fill(Side::Buy, 1.0, 10.0, 0.0, 0));
        lots.drop_symbols(|sleeve, symbol| sleeve == "carry" && symbol == "ACEUSDT");
        assert_eq!(lots.open(), 0);
        assert!(lots.take_closed().is_empty(), "a drop is not a trip");
    }
}
