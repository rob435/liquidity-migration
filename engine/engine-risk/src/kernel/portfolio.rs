use super::*;
use engine_types::ids::StrategyId;
use engine_types::portfolio::PortfolioState;
use std::collections::{BTreeMap, BTreeSet};
struct Held {
    strategy: StrategyId,
    symbol: SymbolId,
    qty: Exact,
    entry_px: Option<Exact>,
    stop_px: Option<Exact>,
}
pub(super) struct PortfolioFacts {
    positions: Vec<Held>,
    net: BTreeMap<SymbolId, Exact>,
}
impl PortfolioFacts {
    pub(super) fn read(state: &PortfolioState) -> Result<Self, DenyReason> {
        if !matches!(state.schema_version, 1 | 2) {
            return Err(unknown("unsupported portfolio schema"));
        }
        let mut seen = BTreeSet::new();
        let mut positions = Vec::new();
        let mut net = BTreeMap::new();
        for row in &state.positions {
            if !seen.insert((row.strategy, row.symbol)) {
                return Err(unknown("duplicate portfolio position"));
            }
            row.signed_qty
                .validate_storage()
                .map_err(|e| unknown(e.to_string()))?;
            if row.signed_qty.is_zero() {
                return Err(unknown("portfolio position has zero quantity"));
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
                    Ok(stop.clone())
                })
                .transpose()?;
            *net.entry(row.symbol).or_insert_with(Exact::zero) += &row.signed_qty;
            positions.push(Held {
                strategy: row.strategy,
                symbol: row.symbol,
                qty: row.signed_qty.clone(),
                entry_px,
                stop_px,
            });
        }
        Ok(Self { positions, net })
    }
    pub(super) fn symbol_gross_quantity(&self, symbol: SymbolId, physical: &Exact) -> Exact {
        self.positions
            .iter()
            .filter(|p| p.symbol == symbol)
            .fold(Exact::zero(), |sum, p| sum + p.qty.abs())
            + (physical - self.net.get(&symbol).cloned().unwrap_or_default()).abs()
    }
    pub(super) fn owned(&self, strategy: StrategyId, symbol: SymbolId) -> Exact {
        self.positions
            .iter()
            .find(|p| p.strategy == strategy && p.symbol == symbol)
            .map_or_else(Exact::zero, |p| p.qty.clone())
    }
}
impl Kernel {
    pub(super) fn physical_interval_for(
        &mut self,
        symbol: SymbolId,
        account: &AccountView,
    ) -> Result<engine_types::risk::PhysicalExposureInterval, DenyReason> {
        let view = ViewFacts::read(account, &Exact::zero())?;
        let recent = self
            .book
            .fills_after(account.observed_ns)
            .map_err(unknown)?;
        let physical = view.net_qty(symbol)
            + recent
                .get(&symbol.0)
                .map_or_else(Exact::zero, |r| r.signed_qty.clone());
        let (low, high) = self
            .book
            .physical_interval(symbol, &physical)
            .map_err(unknown)?;
        engine_types::risk::PhysicalExposureInterval::from_exact(low, high)
    }
    pub(super) fn incremental_physical_quantity(
        &self,
        intent: &Intent,
        qty: &Exact,
        physical: &Exact,
    ) -> Result<Exact, DenyReason> {
        if !qty.is_positive() {
            return Err(unknown("unreadable physical margin reservation"));
        }
        let delta = signed(intent.side, qty);
        let (low, high) = self
            .book
            .physical_interval(intent.symbol, physical)
            .map_err(unknown)?;
        let endpoint = if delta.is_negative() { low } else { high };
        Ok(
            if endpoint.is_zero() || endpoint.is_negative() == delta.is_negative() {
                delta.abs()
            } else {
                (delta.abs() - endpoint.abs() * Exact::from_u64(2)).max(Exact::zero())
            },
        )
    }
    pub(super) fn check_virtual_reduction(
        &self,
        intent: &Intent,
        qty: &Exact,
        physical: &Exact,
        age_ns: u64,
        view: &ViewFacts,
        portfolio: &PortfolioFacts,
    ) -> Result<(), DenyReason> {
        let (low, high) = self
            .book
            .physical_interval(intent.symbol, physical)
            .map_err(unknown)?;
        if match intent.side {
            Side::Sell => low >= *qty,
            Side::Buy => high <= -qty,
        } {
            return Ok(());
        }
        if age_ns > self.cfg.max_account_view_age_ns {
            return Err(DenyReason::StaleAccountView {
                age_ns,
                max_age_ns: self.cfg.max_account_view_age_ns,
            });
        }
        if view.observed_ns < self.latest_account_observed_ns {
            return Err(unknown(
                "virtual reduction would add physical risk against an older account view",
            ));
        }
        let delta = signed(intent.side, qty);
        let after = (low + &delta, high + &delta);
        let price = self.price_for(intent.symbol, view).ok_or_else(|| {
            unknown("no price for physical exposure produced by a virtual reduction")
        })?;
        let current = self
            .book
            .px(intent.symbol)
            .or_else(|| view.entry_px(intent.symbol))
            .ok_or_else(|| unknown("no current reference for virtual reduction protection"))?;
        for row in portfolio
            .positions
            .iter()
            .filter(|p| p.symbol == intent.symbol)
        {
            let remaining = &row.qty
                + if row.strategy == intent.strategy {
                    delta.clone()
                } else {
                    Exact::zero()
                };
            if !(remaining.is_positive() && after.1.is_positive()
                || remaining.is_negative() && after.0.is_negative())
            {
                continue;
            }
            let stop = row.stop_px.as_ref().ok_or(DenyReason::MissingStop)?;
            if remaining.is_positive() && stop >= current
                || remaining.is_negative() && stop <= current
            {
                return Err(DenyReason::MissingStop);
            }
        }
        let margin = (self.incremental_physical_quantity(intent, qty, physical)? * price)
            .checked_div(&policy(self.cfg.leverage))
            .expect("positive leverage")
            + self.unreflected_margin(view)?;
        self.check_available(&margin, view)
    }
    pub(super) fn projected_portfolio(
        &mut self,
        notional: &Exact,
        fraction: &Exact,
        account: &AccountView,
        view: &ViewFacts,
        portfolio: &PortfolioFacts,
    ) -> Result<Projected, DenyReason> {
        let mut projected = Projected {
            gross_usdt: notional.clone(),
            modelled_stop_charge_usdt: self.envelope.modelled_stop_charge_usdt(notional, fraction),
        };
        for row in &portfolio.positions {
            let current = self
                .book
                .px(row.symbol)
                .or(row.entry_px.as_ref())
                .ok_or_else(|| unknown("portfolio position has no market price or entry value"))?;
            let entry = row.entry_px.as_ref().unwrap_or(current);
            let price = current.max(entry);
            let low = current.min(entry);
            let stop = row.stop_px.as_ref().ok_or(DenyReason::MissingStop)?;
            let distance = match row.qty.is_positive() {
                true if stop < current => price - stop,
                false if stop > current => stop - low,
                _ => return Err(DenyReason::MissingStop),
            };
            let fraction = distance
                .checked_div(price)
                .map_err(|e| unknown(e.to_string()))?;
            let notional = row.qty.abs() * price;
            projected.add(&notional);
            projected.modelled_stop_charge_usdt += self
                .envelope
                .modelled_stop_charge_usdt(&notional, &fraction);
        }
        let recent = self
            .book
            .fills_after(account.observed_ns)
            .map_err(unknown)?;
        let symbols: BTreeSet<_> = view
            .exposures()
            .map(|(s, _)| s)
            .chain(recent.keys().map(|s| SymbolId(*s)))
            .chain(portfolio.net.keys().copied())
            .collect();
        for symbol in symbols {
            let fill = recent.get(&symbol.0);
            let physical =
                view.net_qty(symbol) + fill.map_or_else(Exact::zero, |p| p.signed_qty.clone());
            let residual = physical
                - portfolio
                    .net
                    .get(&symbol)
                    .cloned()
                    .unwrap_or_else(Exact::zero);
            if residual.is_zero() {
                continue;
            }
            let price = self
                .price_for(symbol, view)
                .ok_or_else(|| unknown("no price for unallocated physical exposure"))?;
            let stop = if !view.net_qty(symbol).is_zero() {
                self.held_stop_fraction(symbol, view)?
            } else {
                fill.and_then(|p| p.stop_fraction.clone())
                    .ok_or_else(|| unknown("unallocated physical exposure has no readable stop"))?
            };
            let notional = residual.abs() * price;
            projected.add(&notional);
            projected.modelled_stop_charge_usdt +=
                self.envelope.modelled_stop_charge_usdt(&notional, &stop);
        }
        self.add_pending(&mut projected, view)?;
        Ok(projected)
    }
}
