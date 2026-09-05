use super::*;
use engine_types::ids::StrategyId;
use engine_types::numeric::Exact;
use engine_types::portfolio::PortfolioState;
use std::collections::{BTreeMap, BTreeSet};

struct Held {
    strategy: StrategyId,
    symbol: SymbolId,
    qty: f64,
    entry_px: Option<f64>,
    stop_px: Option<f64>,
}

pub(super) struct PortfolioFacts {
    positions: Vec<Held>,
    net: BTreeMap<SymbolId, f64>,
}

impl PortfolioFacts {
    pub(super) fn read(state: &PortfolioState) -> Result<Self, DenyReason> {
        if !matches!(state.schema_version, 1 | 2) {
            return Err(unknown("unsupported portfolio schema"));
        }
        let mut seen = BTreeSet::new();
        let mut positions = Vec::new();
        let mut exact_net: BTreeMap<SymbolId, Exact> = BTreeMap::new();
        let mut exact_gross: BTreeMap<SymbolId, Exact> = BTreeMap::new();
        for row in &state.positions {
            if !seen.insert((row.strategy, row.symbol)) {
                return Err(unknown("duplicate portfolio position"));
            }
            row.signed_qty
                .validate_storage()
                .map_err(|e| unknown(e.to_string()))?;
            let qty = row
                .signed_qty
                .to_f64()
                .map_err(|e| unknown(e.to_string()))?;
            if qty == 0.0 {
                return Err(unknown(
                    "portfolio position has zero or unprojectable quantity",
                ));
            }
            let entry_px = row
                .entry_value
                .as_ref()
                .map(|cost| {
                    cost.validate_storage()
                        .map_err(|e| unknown(e.to_string()))?;
                    if !cost.is_positive() {
                        return Err(unknown("portfolio cost is not positive"));
                    }
                    cost.checked_div(&row.signed_qty.abs())
                        .and_then(|px| px.to_f64())
                        .map_err(|e| unknown(e.to_string()))
                })
                .transpose()?;
            let stop_px = row
                .stop_px
                .as_ref()
                .map(|stop| {
                    stop.validate_storage()
                        .map_err(|e| unknown(e.to_string()))?;
                    if !stop.is_positive() {
                        return Err(unknown("portfolio stop is not positive"));
                    }
                    stop.to_f64().map_err(|e| unknown(e.to_string()))
                })
                .transpose()?;
            *exact_net.entry(row.symbol).or_insert_with(Exact::zero) += &row.signed_qty;
            *exact_gross.entry(row.symbol).or_insert_with(Exact::zero) += row.signed_qty.abs();
            positions.push(Held {
                strategy: row.strategy,
                symbol: row.symbol,
                qty,
                entry_px,
                stop_px,
            });
        }
        for qty in exact_gross.values() {
            qty.to_f64().map_err(|e| unknown(e.to_string()))?;
        }
        let net = exact_net
            .into_iter()
            .map(|(symbol, qty)| {
                qty.to_f64()
                    .map(|qty| (symbol, qty))
                    .map_err(|e| unknown(e.to_string()))
            })
            .collect::<Result<_, _>>()?;
        Ok(Self { positions, net })
    }

    pub(super) fn owned(&self, strategy: StrategyId, symbol: SymbolId) -> f64 {
        self.positions
            .iter()
            .find(|p| p.strategy == strategy && p.symbol == symbol)
            .map_or(0.0, |p| p.qty)
    }
}

impl Kernel {
    pub(super) fn physical_interval_for(
        &mut self,
        symbol: SymbolId,
        account: &AccountView,
    ) -> Result<engine_types::risk::PhysicalExposureInterval, DenyReason> {
        let view = ViewFacts::read(account, 0.0)?;
        let physical = view.net_qty(symbol)
            + self
                .book
                .fills_after(account.observed_ns)
                .get(&symbol.0)
                .map_or(0.0, |row| row.signed_qty);
        let (low, high) = self.book.physical_interval(symbol, physical);
        engine_types::risk::PhysicalExposureInterval::try_new(low, high)
    }

    pub(super) fn physical_reduction(&self, intent: &Intent, qty: f64, physical_qty: f64) -> bool {
        let (low, high) = self.book.physical_interval(intent.symbol, physical_qty);
        if !low.is_finite() || !high.is_finite() {
            return false;
        }
        match intent.side {
            Side::Sell => low >= qty,
            Side::Buy => high <= -qty,
        }
    }

    pub(super) fn incremental_physical_quantity(
        &self,
        intent: &Intent,
        qty: f64,
        physical_qty: f64,
    ) -> Result<f64, DenyReason> {
        let delta = signed(intent.side, qty);
        let (low, high) = self.book.physical_interval(intent.symbol, physical_qty);
        if !qty.is_finite() || qty <= 0.0 || !low.is_finite() || !high.is_finite() {
            return Err(unknown("unreadable physical margin reservation"));
        }
        let endpoint = if delta < 0.0 { low } else { high };
        // Maximize this order's increase over all earlier pending fill orderings.
        Ok(
            if endpoint == 0.0 || endpoint.is_sign_positive() == delta.is_sign_positive() {
                delta.abs()
            } else if endpoint.abs() >= delta.abs() / 2.0 {
                0.0
            } else {
                delta.abs() - 2.0 * endpoint.abs()
            },
        )
    }

    pub(super) fn check_virtual_reduction(
        &self,
        intent: &Intent,
        qty: f64,
        physical_qty: f64,
        age_ns: u64,
        view: &ViewFacts,
        portfolio: &PortfolioFacts,
    ) -> Result<(), DenyReason> {
        if self.physical_reduction(intent, qty, physical_qty) {
            return Ok(());
        }
        if age_ns > self.cfg.max_account_view_age_ns {
            return Err(DenyReason::StaleAccountView {
                age_ns,
                max_age_ns: self.cfg.max_account_view_age_ns,
            });
        }
        let delta = signed(intent.side, qty);
        let (low, high) = self.book.physical_interval(intent.symbol, physical_qty);
        let after = (low + delta, high + delta);
        if !after.0.is_finite() || !after.1.is_finite() {
            return Err(unknown(
                "portfolio reduction produces unreadable physical exposure",
            ));
        }
        let price = self.price_for(intent.symbol, view).ok_or_else(|| {
            unknown("no price for physical exposure produced by a virtual reduction")
        })?;
        let current = self
            .book
            .px(intent.symbol)
            .or_else(|| view.entry_px(intent.symbol))
            .ok_or_else(|| unknown("no current reference for virtual reduction protection"))?;
        // An exit may expose either side when other outstanding orders fill first.
        for row in portfolio
            .positions
            .iter()
            .filter(|p| p.symbol == intent.symbol)
        {
            let remaining = row.qty
                + if row.strategy == intent.strategy {
                    delta
                } else {
                    0.0
                };
            let can_be_physical =
                (remaining > 0.0 && after.1 > 0.0) || (remaining < 0.0 && after.0 < 0.0);
            if !can_be_physical {
                continue;
            }
            let stop = row.stop_px.ok_or(DenyReason::MissingStop)?;
            if (remaining > 0.0 && stop >= current) || (remaining < 0.0 && stop <= current) {
                return Err(DenyReason::MissingStop);
            }
        }
        let additional_margin_usdt =
            self.incremental_physical_quantity(intent, qty, physical_qty)? * price
                / self.cfg.leverage
                + self.unreflected_margin(view)?;
        if !additional_margin_usdt.is_finite() {
            return Err(unknown("portfolio reduction produces unreadable margin"));
        }
        if additional_margin_usdt > view.available_usdt {
            return Err(DenyReason::AvailableMarginExhausted {
                additional_margin_usdt,
                available_usdt: view.available_usdt,
            });
        }
        Ok(())
    }

    pub(super) fn projected_portfolio(
        &mut self,
        notional: f64,
        stop_fraction: f64,
        account: &AccountView,
        view: &ViewFacts,
        portfolio: &PortfolioFacts,
    ) -> Result<Projected, DenyReason> {
        let mut projected = Projected {
            gross_usdt: notional,
            worst_case_loss_usdt: self
                .envelope
                .position_worst_case_usdt(notional, stop_fraction),
        };
        for row in &portfolio.positions {
            let entry = row
                .entry_px
                .ok_or_else(|| unknown("portfolio position has unknown entry value"))?;
            let current = self.book.px(row.symbol).unwrap_or(entry);
            let price = current.max(entry);
            let low = current.min(entry);
            let stop = row.stop_px.ok_or(DenyReason::MissingStop)?;
            let fraction = match row.qty.is_sign_positive() {
                true if stop < current => (price - stop) / price,
                false if stop > current => (stop - low) / price,
                _ => return Err(DenyReason::MissingStop),
            };
            let notional = row.qty.abs() * price;
            projected.add(notional);
            projected.worst_case_loss_usdt +=
                self.envelope.position_worst_case_usdt(notional, fraction);
        }
        let recent = self.book.fills_after(account.observed_ns);
        let symbols: BTreeSet<_> = view
            .exposures()
            .map(|(s, _)| s)
            .chain(recent.keys().map(|s| SymbolId(*s)))
            .chain(portfolio.net.keys().copied())
            .collect();
        for symbol in symbols {
            let pending_fill = recent.get(&symbol.0);
            let physical = view.net_qty(symbol) + pending_fill.map_or(0.0, |p| p.signed_qty);
            let residual = physical - portfolio.net.get(&symbol).copied().unwrap_or(0.0);
            if residual == 0.0 {
                continue;
            }
            let price = self
                .price_for(symbol, view)
                .ok_or_else(|| unknown("no price for unallocated physical exposure"))?;
            let stop = if view.net_qty(symbol) != 0.0 {
                self.held_stop_fraction(symbol, view)?
            } else {
                pending_fill
                    .and_then(|p| p.stop_fraction)
                    .ok_or_else(|| unknown("unallocated physical exposure has no readable stop"))?
            };
            let notional = residual.abs() * price;
            projected.add(notional);
            projected.worst_case_loss_usdt +=
                self.envelope.position_worst_case_usdt(notional, stop);
        }
        for (_, notional, fraction) in self
            .book
            .pending_risk_rows(|symbol| self.price_for(symbol, view))
            .map_err(unknown)?
        {
            projected.add(notional);
            projected.worst_case_loss_usdt +=
                self.envelope.position_worst_case_usdt(notional, fraction);
        }
        if !projected.gross_usdt.is_finite() || !projected.worst_case_loss_usdt.is_finite() {
            return Err(unknown("portfolio risk total is unreadable"));
        }
        Ok(projected)
    }
}
