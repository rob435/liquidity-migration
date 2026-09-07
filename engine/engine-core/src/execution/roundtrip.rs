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

use engine_types::numeric::{AssetId, Exact};
use engine_types::Side;
use serde::Serialize;

use super::{arrival_shortfall_bps, Fill, Weighted};

/// Compatibility for logs without retained execution quantities.
const LEGACY_FLAT: f64 = 1e-9;

/// A position a sleeve is now out of, and what it came to.
///
/// These field names are a contract with
/// `scripts/runtime/notify_book_changes.py`, which reads them off disk as one
/// JSON line and puts them on the owner's phone.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct ClosedTrade {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unpriced: Option<engine_types::risk::UnpricedTradeReason>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub internal_settlement: Option<u64>,
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
    /// Absent when a legacy checkpoint lacks entry cost or a fee's USDT value.
    pub round_trip: Option<RoundTrip>,
}

impl ClosedTrade {
    pub fn loss_row(&self) -> Option<engine_types::risk::ClosedTradeRow> {
        if let Some(trip) = &self.round_trip {
            Some(engine_types::risk::ClosedTradeRow {
                unpriced: None,
                net_usdt_exact: Some(trip.net_usdt_exact.clone()),
                closed_ms: self.closed_ms,
                net_usdt: trip.net_usdt,
            })
        } else {
            self.unpriced
                .map(|reason| engine_types::risk::ClosedTradeRow {
                    unpriced: Some(reason),
                    net_usdt_exact: None,
                    closed_ms: self.closed_ms,
                    net_usdt: 0.0,
                })
        }
    }
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
    #[serde(skip)]
    pub net_usdt_exact: Exact,
    /// `net_usdt` against what went in, in basis points.
    pub net_bps: f64,
    pub opened_ms: i64,
    pub held_ms: i64,
}

/// One sleeve's open position in one symbol.
#[derive(Clone, Debug)]
struct Lot {
    signed_qty: Exact,
    exact_quantity: bool,
    /// +1 while held long, −1 short. `signed_qty` is zero by the time the
    /// trip closes, so the side it was held on has to be remembered.
    held: f64,
    cash: Exact,
    in_qty: Exact,
    in_value: Exact,
    out_qty: Exact,
    out_value: Exact,
    fees: Option<Exact>,
    usdt: bool,
    fills: u64,
    notional: f64,
    maker_notional: f64,
    shortfall: Weighted,
    opened_ms: i64,
    /// False when a legacy checkpoint lacks this position's entry cost.
    priced: bool,
}

impl Default for Lot {
    fn default() -> Self {
        Lot {
            signed_qty: Exact::zero(),
            exact_quantity: false,
            held: 0.0,
            cash: Exact::zero(),
            in_qty: Exact::zero(),
            in_value: Exact::zero(),
            out_qty: Exact::zero(),
            out_value: Exact::zero(),
            fees: Some(Exact::zero()),
            usdt: true,
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
    fn fold(
        &mut self,
        fill: &Fill,
        quantity: &Exact,
        exact: bool,
        economics: Option<&super::AllocationEconomics>,
    ) -> Result<(), String> {
        let signed = match fill.side {
            Side::Buy => quantity.clone(),
            Side::Sell => -quantity,
        };
        let price = fill
            .amounts
            .as_ref()
            .map(|a| Ok(a.price.value.clone()))
            .unwrap_or_else(|| Exact::from_legacy_f64(fill.px))
            .map_err(|e| e.to_string())?;
        let value = match economics {
            Some(economics) => {
                &economics.consideration
                    * quantity
                        .checked_div(&economics.quantity)
                        .map_err(|e| e.to_string())?
            }
            None => &price * quantity,
        };
        if self.signed_qty.is_zero() {
            self.held = if signed.is_negative() { -1.0 } else { 1.0 };
            self.opened_ms = fill.venue_ts_ms;
            self.priced = true;
        }
        if signed.is_negative() == (self.held < 0.0) {
            self.in_qty += quantity;
            self.in_value += &value;
        } else {
            self.out_qty += quantity;
            self.out_value += &value;
        }
        self.exact_quantity |= exact;
        self.signed_qty += &signed;
        self.cash += if signed.is_positive() {
            -&value
        } else {
            value.clone()
        };
        self.fills += 1;
        let projected_value = value.reporting_f64();
        self.notional = (self.notional + projected_value).min(f64::MAX);
        if fill.is_maker {
            self.maker_notional = (self.maker_notional + projected_value).min(f64::MAX);
        }
        let (fee, whole_quantity) = if let Some(economics) = economics {
            if let Some(amounts) = &fill.amounts {
                self.usdt &=
                    matches!(&amounts.settlement_asset, AssetId::Named(asset) if asset == "USDT");
            }
            let fee = economics.fee.clone().filter(|_| {
                fill.amounts.as_ref().is_none_or(|amounts| {
                    amounts.fee.as_ref().is_some_and(|fee| {
                        fee.amount.value.is_zero()
                            || matches!(&fee.asset, AssetId::Named(asset) if asset == "USDT")
                    })
                })
            });
            (fee, economics.quantity.clone())
        } else if let Some(amounts) = &fill.amounts {
            self.usdt &=
                matches!(&amounts.settlement_asset, AssetId::Named(asset) if asset == "USDT");
            let fee = amounts.fee.as_ref().and_then(|fee| {
                (fee.amount.value.is_zero()
                    || matches!(&fee.asset, AssetId::Named(asset) if asset == "USDT"))
                .then(|| fee.amount.value.clone())
            });
            (fee, amounts.quantity.value.clone())
        } else {
            (
                fill.fee
                    .filter(|fee| fee.is_finite())
                    .map(Exact::from_legacy_f64)
                    .transpose()
                    .map_err(|e| e.to_string())?,
                Exact::from_legacy_f64(fill.qty).map_err(|e| e.to_string())?,
            )
        };
        self.fees = match (&self.fees, fee) {
            (Some(total), Some(fee)) => Some(
                total
                    + &(&fee
                        * &quantity
                            .checked_div(&whole_quantity)
                            .map_err(|e| e.to_string())?),
            ),
            _ => None,
        };
        if let Some(bps) = arrival_shortfall_bps(fill.side, fill.px, fill.arrival_mid) {
            let weight = self.shortfall.weight + projected_value;
            let total = self.shortfall.total + bps * projected_value;
            if weight.is_finite() && total.is_finite() && projected_value > 0.0 {
                self.shortfall = Weighted { weight, total };
            }
        }
        Ok(())
    }

    fn flat(&self) -> bool {
        self.signed_qty.is_zero()
    }

    fn closed(&self, sleeve: &str, symbol: &str, closed_ms: i64) -> Result<ClosedTrade, String> {
        let project = Exact::reporting_f64;
        let ratio = |a: &Exact, b: &Exact| {
            a.checked_div(b)
                .map_err(|e| e.to_string())
                .map(|v| project(&v))
        };
        let basis_known = self.priced && self.in_qty.is_positive() && self.in_value.is_positive();
        let priced = basis_known && self.usdt;
        let unpriced = if basis_known && self.exact_quantity {
            if !self.usdt {
                Some(engine_types::risk::UnpricedTradeReason::SettlementAsset)
            } else if self.fees.is_none() {
                Some(engine_types::risk::UnpricedTradeReason::FeeValue)
            } else {
                None
            }
        } else {
            None
        };
        let round_trip = if let Some(fees) = self.fees.as_ref().filter(|_| priced) {
            let net = &self.cash - fees;
            Some(RoundTrip {
                entry_px: ratio(&self.in_value, &self.in_qty)?,
                entry_notional_usdt: project(&self.in_value),
                gross_usdt: project(&self.cash),
                fees_usdt: project(fees),
                net_usdt: project(&net),
                net_bps: ratio(&(&net * &Exact::from_i64(10_000)), &self.in_value)?,
                net_usdt_exact: net,
                opened_ms: self.opened_ms,
                held_ms: closed_ms - self.opened_ms,
            })
        } else {
            None
        };
        Ok(ClosedTrade {
            unpriced,
            internal_settlement: None,
            sleeve: sleeve.to_string(),
            symbol: symbol.to_string(),
            side: if self.held < 0.0 { "short" } else { "long" },
            qty: project(&self.out_qty),
            exit_px: ratio(&self.out_value, &self.out_qty)?,
            closed_ms,
            fills: self.fills,
            maker_share: (self.notional > 0.0).then(|| self.maker_notional / self.notional),
            arrival_shortfall_bps: self.shortfall.mean(),
            gross_usdt: priced.then(|| project(&self.cash)),
            fees_usdt: self.fees.as_ref().filter(|_| priced).map(project),
            round_trip,
        })
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
    pub(crate) fn adopt_legacy_quantities(
        &mut self,
        corrections: &[(String, String, Exact, Exact)],
    ) -> Result<(), String> {
        let mut next = self.open.clone();
        let mut seen = std::collections::BTreeSet::new();
        for (sleeve, symbol, before, after) in corrections {
            let key = (sleeve.clone(), symbol.clone());
            if !seen.insert(key.clone()) {
                return Err("repeated analytic quantity adoption".into());
            }
            let lot = next
                .get_mut(&key)
                .ok_or("quantity adoption has no analytic lot")?;
            if &lot.signed_qty != before {
                return Err("quantity adoption changes its analytic source quantity".into());
            }
            if after.is_zero() {
                next.remove(&key);
                continue;
            }
            if lot.priced {
                let delta = after - before;
                lot.in_qty += if lot.held > 0.0 { delta } else { -delta };
                if lot.in_qty.is_negative() {
                    return Err("quantity adoption makes negative analytical input units".into());
                }
            }
            lot.signed_qty = after.clone();
            lot.held = if after.is_positive() { 1.0 } else { -1.0 };
            lot.exact_quantity = true;
        }
        self.open = next;
        Ok(())
    }

    pub fn checkpoint(&self) -> Vec<engine_types::trade::OpenTradeLot> {
        self.open
            .iter()
            .map(
                |((sleeve, symbol), lot)| engine_types::trade::OpenTradeLot {
                    sleeve: sleeve.clone(),
                    symbol: symbol.clone(),
                    signed_qty: lot.signed_qty.clone(),
                    exact_quantity: lot.exact_quantity,
                    cash: lot.cash.clone(),
                    in_qty: lot.in_qty.clone(),
                    in_value: lot.in_value.clone(),
                    out_qty: lot.out_qty.clone(),
                    out_value: lot.out_value.clone(),
                    fees: lot.fees.clone(),
                    usdt: lot.usdt,
                    fills: lot.fills,
                    notional: lot.notional,
                    maker_notional: lot.maker_notional,
                    shortfall_weight: lot.shortfall.weight,
                    shortfall_total: lot.shortfall.total,
                    opened_ms: lot.opened_ms,
                    priced: lot.priced,
                },
            )
            .collect()
    }

    pub fn restore(&mut self, rows: &[engine_types::trade::OpenTradeLot]) -> Result<(), String> {
        let mut open = BTreeMap::new();
        for row in rows {
            for value in [
                &row.signed_qty,
                &row.cash,
                &row.in_qty,
                &row.in_value,
                &row.out_qty,
                &row.out_value,
            ]
            .into_iter()
            .chain(row.fees.iter())
            {
                value.validate_storage().map_err(|e| e.to_string())?;
            }
            if row.signed_qty.is_zero()
                || [&row.in_qty, &row.in_value, &row.out_qty, &row.out_value]
                    .iter()
                    .any(|v| v.is_negative())
                || [
                    row.notional,
                    row.maker_notional,
                    row.shortfall_weight,
                    row.shortfall_total,
                ]
                .iter()
                .any(|v| !v.is_finite())
            {
                return Err("invalid open trade cost basis".into());
            }
            let expected_cash = if row.signed_qty.is_positive() {
                &row.out_value - &row.in_value
            } else {
                &row.in_value - &row.out_value
            };
            if row.cash != expected_cash {
                return Err("open trade cash disagrees with its entry and exit values".into());
            }
            let lot = Lot {
                signed_qty: row.signed_qty.clone(),
                exact_quantity: row.exact_quantity,
                held: if row.signed_qty.is_negative() {
                    -1.0
                } else {
                    1.0
                },
                cash: row.cash.clone(),
                in_qty: row.in_qty.clone(),
                in_value: row.in_value.clone(),
                out_qty: row.out_qty.clone(),
                out_value: row.out_value.clone(),
                fees: row.fees.clone(),
                usdt: row.usdt,
                fills: row.fills,
                notional: row.notional,
                maker_notional: row.maker_notional,
                shortfall: Weighted {
                    weight: row.shortfall_weight,
                    total: row.shortfall_total,
                },
                opened_ms: row.opened_ms,
                priced: row.priced,
            };
            if open
                .insert((row.sleeve.clone(), row.symbol.clone()), lot)
                .is_some()
            {
                return Err("duplicate open trade cost basis owner".into());
            }
        }
        self.open = open;
        Ok(())
    }

    pub(super) fn validate_internal(
        &self,
        sleeve: &str,
        symbol: &str,
        signed_delta: &Exact,
        px: &Exact,
    ) -> Result<(), String> {
        if signed_delta.is_zero() || !px.is_positive() {
            return Err("invalid internal settlement projection".into());
        }
        let delta = signed_delta.to_f64().map_err(|e| e.to_string())?;
        if let Some(lot) = self.open.get(&(sleeve.to_string(), symbol.to_string())) {
            let remaining = &lot.signed_qty + signed_delta;
            let agrees = if lot.exact_quantity {
                remaining.is_zero()
            } else {
                remaining
                    .to_f64()
                    .is_ok_and(|qty| qty.abs() <= 1e-12_f64.max(delta.abs() * 1e-12))
            };
            if lot.signed_qty.is_negative() == signed_delta.is_negative() || !agrees {
                return Err(
                    "internal settlement projection disagrees with the analytic lot".into(),
                );
            }
        }
        Ok(())
    }
    pub(super) fn settle_internal(
        &mut self,
        (sleeve, symbol): (&str, &str),
        signed_delta: &Exact,
        px: &Exact,
        closed_ms: i64,
        id: u64,
        asset: &AssetId,
    ) -> Result<(), String> {
        self.validate_internal(sleeve, symbol, signed_delta, px)?;
        let delta = signed_delta.to_f64().map_err(|e| e.to_string())?;
        let key = (sleeve.to_string(), symbol.to_string());
        let mut lot = self.open.get(&key).cloned().unwrap_or_else(|| Lot {
            signed_qty: -signed_delta,
            exact_quantity: true,
            held: -delta.signum(),
            ..Lot::default()
        });
        lot.out_qty += signed_delta.abs();
        lot.out_value += &signed_delta.abs() * px;
        lot.cash -= signed_delta * px;
        lot.usdt &= matches!(asset, AssetId::Named(asset) if asset == "USDT");
        lot.signed_qty = Exact::zero();
        let mut closed = lot.closed(sleeve, symbol, closed_ms)?;
        closed.internal_settlement = Some(id);
        self.open.remove(&key);
        self.closed.push(closed);
        Ok(())
    }
    pub fn on_fill(&mut self, sleeve: &str, symbol: &str, fill: &Fill) {
        self.on_fill_with_quantity(sleeve, symbol, fill, None)
            .expect("validated scalar analytic fill");
    }

    pub fn on_fill_with_quantity(
        &mut self,
        sleeve: &str,
        symbol: &str,
        fill: &Fill,
        exact_qty: Option<&Exact>,
    ) -> Result<(), String> {
        self.on_fill_with_economics(sleeve, symbol, fill, exact_qty, None)
    }

    pub(crate) fn on_fill_with_economics(
        &mut self,
        sleeve: &str,
        symbol: &str,
        fill: &Fill,
        exact_qty: Option<&Exact>,
        economics: Option<&super::AllocationEconomics>,
    ) -> Result<(), String> {
        if let Some(amounts) = &fill.amounts {
            amounts
                .validate_projection(fill.qty, fill.px, fill.fee)
                .map_err(|e| e.to_string())?;
            if exact_qty.is_some_and(|qty| qty != &amounts.quantity.value) {
                return Err("analytic quantity conflicts with execution amounts".into());
            }
        }
        let exact_qty = fill
            .amounts
            .as_ref()
            .map(|a| &a.quantity.value)
            .or(exact_qty);
        if let Some(exact) = exact_qty {
            if !exact.is_positive() || exact.to_f64().map_err(|e| e.to_string())? != fill.qty {
                return Err("analytic exact quantity disagrees with fill projection".into());
            }
        }
        if !fill.qty.is_finite() || fill.qty <= 0.0 || !fill.px.is_finite() || fill.px <= 0.0 {
            return Ok(());
        }
        let quantity = exact_qty
            .cloned()
            .map(Ok)
            .unwrap_or_else(|| Exact::from_legacy_f64(fill.qty))
            .map_err(|e| e.to_string())?;
        if !quantity.is_positive() || quantity.to_f64().map_err(|e| e.to_string())? != fill.qty {
            return Err("analytic exact quantity disagrees with fill projection".into());
        }
        let key = (sleeve.to_string(), symbol.to_string());
        let mut lot = self.open.get(&key).cloned().unwrap_or_default();
        let exact = exact_qty.is_some() || lot.exact_quantity;
        let signed_fill = if fill.side == Side::Buy {
            quantity.clone()
        } else {
            -&quantity
        };
        let legacy_flat_after = exact_qty.is_none()
            && (&lot.signed_qty + &signed_fill)
                .to_f64()
                .is_ok_and(|qty| qty.abs() < LEGACY_FLAT);
        let opposite =
            !lot.signed_qty.is_zero() && lot.signed_qty.is_negative() != (fill.side == Side::Sell);
        let closing = if opposite {
            quantity.clone().min(lot.signed_qty.abs())
        } else {
            Exact::zero()
        };
        let opening = &quantity - &closing;
        let mut closed = None;
        if closing.is_positive() {
            lot.fold(fill, &closing, exact, economics)?;
            if lot.flat() || legacy_flat_after {
                closed = Some(lot.closed(&key.0, &key.1, fill.venue_ts_ms)?);
                lot = Lot::default();
            }
        }
        if opening.is_positive() && !legacy_flat_after {
            lot.fold(fill, &opening, exact, economics)?;
        }
        if lot.signed_qty.is_zero() {
            self.open.remove(&key);
        } else {
            self.open.insert(key, lot);
        }
        if let Some(closed) = closed {
            self.closed.push(closed);
        }
        Ok(())
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
            if !signed_qty.is_finite() || *signed_qty == 0.0 {
                continue;
            }
            self.open.insert(
                (sleeve.clone(), symbol.clone()),
                Lot {
                    signed_qty: Exact::from_legacy_f64(*signed_qty)
                        .expect("finite legacy quantity"),
                    held: signed_qty.signum(),
                    ..Lot::default()
                },
            );
        }
    }

    pub fn restate_exact(&mut self, held: &[(String, String, Exact)]) -> Result<(), String> {
        let mut open = BTreeMap::new();
        for (sleeve, symbol, quantity) in held {
            if quantity.is_zero() {
                continue;
            }
            let projected = quantity.to_f64().map_err(|e| e.to_string())?;
            if open
                .insert(
                    (sleeve.clone(), symbol.clone()),
                    Lot {
                        signed_qty: quantity.clone(),
                        exact_quantity: true,
                        held: projected.signum(),
                        ..Lot::default()
                    },
                )
                .is_some()
            {
                return Err("duplicate analytic position in portfolio snapshot".into());
            }
        }
        self.open = open;
        Ok(())
    }

    /// Forget a sleeve's position without reporting a trip, for the symbols
    /// boot found the venue holding nothing of.
    ///
    /// The same act as `attribution`'s own drop, and for the same reason: the
    /// close happened somewhere this log cannot see — a hand close, an
    /// inherited holding wound down — so there is no exit price to report and
    /// inventing one would be worse than saying nothing.
    pub fn drop_symbols(&mut self, dropped: impl Fn(&str, &str) -> bool) {
        self.open
            .retain(|(sleeve, symbol), _| !dropped(sleeve, symbol));
    }

    /// The one sleeve holding this coin, and how much, signed. `None` when
    /// nobody holds it or two sleeves do — which is what a close nobody
    /// ordered has to be charged against.
    pub fn sole_holder(&self, symbol: &str) -> Option<(&str, f64)> {
        let mut held = self
            .open
            .iter()
            .filter(|((_, coin), lot)| coin == symbol && !lot.flat());
        let ((sleeve, _), lot) = held.next()?;
        held.next().is_none().then_some((
            sleeve.as_str(),
            lot.signed_qty
                .to_f64()
                .expect("validated analytic quantity"),
        ))
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
            amounts: None,
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

#[cfg(test)]
mod migration_tests {
    use super::*;
    use engine_types::{StrategyId, SymbolId};

    #[test]
    fn legacy_restated_tiny_quantity_remains_a_real_holder() {
        let mut lots = Lots::default();
        lots.restate(&[("owner".into(), "BTCUSDT".into(), 1e-10)]);
        assert_eq!(lots.sole_holder("BTCUSDT"), Some(("owner", 1e-10)));
    }

    #[test]
    fn legacy_close_of_canonical_snapshot_does_not_leave_analytic_residue() {
        let mut lots = Lots::default();
        lots.restate_exact(&[(
            "owner".into(),
            "BTCUSDT".into(),
            Exact::parse_decimal("0.3").unwrap(),
        )])
        .unwrap();
        for qty in [0.1, 0.2] {
            lots.on_fill(
                "owner",
                "BTCUSDT",
                &Fill {
                    amounts: None,
                    client_order_id: "legacy".into(),
                    strategy: StrategyId(0),
                    symbol: SymbolId(0),
                    side: Side::Sell,
                    qty,
                    px: 100.0,
                    fee: Some(0.0),
                    is_maker: false,
                    arrival_mid: 0.0,
                    venue_ts_ms: 1,
                },
            );
        }
        assert_eq!(
            lots.open(),
            0,
            "the canonical owner already settled legacy rounding residue"
        );
        assert_eq!(lots.closed().len(), 1);
        assert!(lots.closed()[0].round_trip.is_none());
    }
}

#[cfg(test)]
mod exact_money_tests {
    use super::*;
    use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
    use engine_types::{StrategyId, SymbolId};

    fn fill(side: Side, qty: &str, price: &str, fee: &str) -> Fill {
        let amounts = ExecutionAmounts {
            settlement_asset: AssetId::Named("USDT".into()),
            quantity: ExactNumber::venue_decimal(qty).unwrap(),
            price: ExactNumber::venue_decimal(price).unwrap(),
            fee: Some(AssetAmount {
                asset: AssetId::Named("USDT".into()),
                amount: ExactNumber::venue_decimal(fee).unwrap(),
            }),
        };
        Fill {
            qty: amounts.quantity.value.to_f64().unwrap(),
            px: amounts.price.value.to_f64().unwrap(),
            fee: Some(amounts.fee.as_ref().unwrap().amount.value.to_f64().unwrap()),
            amounts: Some(Box::new(amounts)),
            client_order_id: "precise".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side,
            is_maker: false,
            arrival_mid: 0.0,
            venue_ts_ms: 1,
        }
    }

    #[test]
    fn a_loss_smaller_than_one_price_ulp_reaches_the_loss_window_amount() {
        let mut lots = Lots::default();
        lots.on_fill(
            "long",
            "BTCUSDT",
            &fill(Side::Buy, "1", "9007199254740993", "0"),
        );
        lots.on_fill(
            "long",
            "BTCUSDT",
            &fill(Side::Sell, "1", "9007199254740992", "0"),
        );
        let trade = lots.closed()[0].round_trip.as_ref().unwrap();
        assert_eq!(trade.net_usdt_exact, Exact::from_i64(-1));
        assert_eq!(trade.net_usdt, -1.0);
    }

    #[test]
    fn one_reversing_fill_splits_its_fee_exactly_once_across_two_trips() {
        let mut lots = Lots::default();
        lots.on_fill("maker", "BTCUSDT", &fill(Side::Buy, "1", "100", "0"));
        lots.on_fill("maker", "BTCUSDT", &fill(Side::Sell, "3", "100", "0.1"));
        lots.on_fill("maker", "BTCUSDT", &fill(Side::Buy, "2", "100", "0"));
        let a = &lots.closed()[0].round_trip.as_ref().unwrap().net_usdt_exact;
        let b = &lots.closed()[1].round_trip.as_ref().unwrap().net_usdt_exact;
        assert_eq!(
            *a,
            Exact::from_i64(-1)
                .checked_div(&Exact::from_i64(30))
                .unwrap()
        );
        assert_eq!(a + b, Exact::parse_decimal("-0.1").unwrap());
    }

    #[test]
    fn a_partial_trip_keeps_its_exact_basis_and_fees_through_serialized_restart() {
        let mut lots = Lots::default();
        lots.on_fill(
            "long",
            "BTCUSDT",
            &fill(Side::Buy, "3", "9007199254740993", "0.1"),
        );
        lots.on_fill(
            "long",
            "BTCUSDT",
            &fill(Side::Sell, "1", "9007199254740992", "0.1"),
        );
        let payload = serde_json::to_vec(&lots.checkpoint()).unwrap();
        let mut restarted = Lots::default();
        restarted
            .restore(
                &serde_json::from_slice::<Vec<engine_types::trade::OpenTradeLot>>(&payload)
                    .unwrap(),
            )
            .unwrap();
        let finish = fill(Side::Sell, "2", "9007199254740992", "0.1");
        lots.on_fill("long", "BTCUSDT", &finish);
        restarted.on_fill("long", "BTCUSDT", &finish);
        assert_eq!(lots.closed(), restarted.closed());
        assert_eq!(
            restarted.closed()[0]
                .round_trip
                .as_ref()
                .unwrap()
                .net_usdt_exact,
            Exact::parse_decimal("-3.3").unwrap()
        );
        assert!(restarted.checkpoint().is_empty());
    }

    #[test]
    fn a_fee_in_another_asset_never_becomes_a_usdt_loss_amount() {
        let mut lots = Lots::default();
        lots.on_fill("long", "BTCUSDT", &fill(Side::Buy, "1", "100", "0"));
        let mut close = fill(Side::Sell, "1", "101", "0.1");
        close.amounts.as_mut().unwrap().fee.as_mut().unwrap().asset = AssetId::Named("BNB".into());
        lots.on_fill("long", "BTCUSDT", &close);
        assert_eq!(lots.closed()[0].gross_usdt, Some(1.0));
        assert!(lots.closed()[0].round_trip.is_none());
        assert!(lots.closed()[0].fees_usdt.is_none());
    }

    #[test]
    fn derived_money_never_aborts_a_live_close_or_serialized_restart() {
        for (quantity, entry, exit, expected) in [
            ("1e-310", "1.0000000000000000001", "1", "-1e-329"),
            ("1e-200", "1e-200", "2e-200", "1e-400"),
            ("1e200", "1e200", "2e200", "1e400"),
        ] {
            let mut live = Lots::default();
            live.on_fill_with_quantity(
                "owner",
                "BTCUSDT",
                &fill(Side::Buy, quantity, entry, "0"),
                None,
            )
            .unwrap();
            let mut restored = Lots::default();
            restored
                .restore(
                    &serde_json::from_slice::<Vec<engine_types::trade::OpenTradeLot>>(
                        &serde_json::to_vec(&live.checkpoint()).unwrap(),
                    )
                    .unwrap(),
                )
                .unwrap();
            for lots in [&mut live, &mut restored] {
                lots.on_fill_with_quantity(
                    "owner",
                    "BTCUSDT",
                    &fill(Side::Sell, quantity, exit, "0"),
                    None,
                )
                .unwrap();
                let row = lots.closed()[0].loss_row().unwrap();
                assert_eq!(
                    row.net().unwrap(),
                    Some(Exact::parse_decimal(expected).unwrap())
                );
                let replay: engine_types::risk::ClosedTradeRow =
                    serde_json::from_slice(&serde_json::to_vec(&row).unwrap()).unwrap();
                assert_eq!(replay, row);
                assert!(row.net_usdt.is_finite());
            }
            assert_eq!(live.closed(), restored.closed());
        }
    }

    #[test]
    fn unvalued_native_trips_retain_accounting_debt_after_restart() {
        use engine_types::risk::UnpricedTradeReason;
        for (unknown_settlement, expected) in [
            (true, UnpricedTradeReason::SettlementAsset),
            (false, UnpricedTradeReason::FeeValue),
        ] {
            let mut lots = Lots::default();
            let mut entry = fill(Side::Buy, "1", "100", "0");
            if unknown_settlement {
                entry.amounts.as_mut().unwrap().settlement_asset = AssetId::Unknown;
            }
            lots.on_fill_with_quantity("owner", "BTCUSDT", &entry, None)
                .unwrap();
            let mut restored = Lots::default();
            restored
                .restore(
                    &serde_json::from_slice::<Vec<engine_types::trade::OpenTradeLot>>(
                        &serde_json::to_vec(&lots.checkpoint()).unwrap(),
                    )
                    .unwrap(),
                )
                .unwrap();
            let mut close = fill(Side::Sell, "1", "90", "0.1");
            if unknown_settlement {
                close.amounts.as_mut().unwrap().settlement_asset = AssetId::Unknown;
            } else {
                close.amounts.as_mut().unwrap().fee.as_mut().unwrap().asset =
                    AssetId::Named("BNB".into());
            }
            for state in [&mut lots, &mut restored] {
                state
                    .on_fill_with_quantity("owner", "BTCUSDT", &close, None)
                    .unwrap();
                let row = state.closed()[0]
                    .loss_row()
                    .expect("native debt disappeared");
                assert_eq!(row.unpriced, Some(expected));
                assert_eq!(row.net().unwrap(), None);
            }
            assert_eq!(lots.closed(), restored.closed());
        }
    }

    #[test]
    fn legacy_missing_entry_basis_remains_explicitly_unpriced() {
        let mut lots = Lots::default();
        lots.restate_exact(&[("owner".into(), "BTCUSDT".into(), Exact::one())])
            .unwrap();
        lots.on_fill_with_quantity("owner", "BTCUSDT", &fill(Side::Sell, "1", "90", "0"), None)
            .unwrap();
        assert!(lots.closed()[0].loss_row().is_none());
    }

    #[test]
    fn a_checkpoint_cannot_change_the_cash_implied_by_its_entry_and_exit_values() {
        let mut lots = Lots::default();
        lots.on_fill("owner", "BTCUSDT", &fill(Side::Buy, "2", "100", "0"));
        lots.on_fill("owner", "BTCUSDT", &fill(Side::Sell, "1", "90", "0"));
        let mut rows = lots.checkpoint();
        rows[0].cash += Exact::one();
        assert!(Lots::default()
            .restore(&rows)
            .unwrap_err()
            .contains("cash disagrees"));
    }
}
