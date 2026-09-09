//! Durable per-sleeve inventory; account reconciliation owns the physical net.

use std::collections::HashMap;

use engine_types::{
    FilledTotal, ForcedClose, OrderUpdate, Side, StrategyId, SymbolId, SymbolTotal, WalRecord,
};

mod allocated;
mod internal;
pub(crate) use allocated::PreparedPortfolioFill;

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
    inventory: crate::inventory::Inventory,
    pub(crate) legacy_quantities:
        std::collections::BTreeMap<(StrategyId, SymbolId), crate::legacy_quantity::Origin>,
    internal: internal::InternalAccounting,
    accounting: crate::execution_accounting::ExecutionAccounting,
}

#[derive(Debug)]
pub(crate) struct PreparedAllocation {
    change: crate::inventory::InventoryChange,
    accounting: crate::execution_accounting::AccountingChange,
    strategy: StrategyId,
    symbol: SymbolId,
    /// The binary64 signed quantity this fill puts into the sleeve's sum.
    /// `None` when the venue stated exact amounts.
    legacy_input: Option<engine_types::numeric::Exact>,
}

impl Attribution {
    /// Rebuild from the log. Exactly the live path's arithmetic, run over the
    /// join the log already holds, so boot and steady state cannot drift.
    ///
    /// One pass is enough: an order's `OrderSent` record is made durable
    /// before its bytes leave the socket, so it is always ahead of its fills.
    pub fn from_records(records: &[WalRecord]) -> Self {
        Self::try_from_records(records).expect("validated portfolio WAL")
    }

    pub fn try_from_records(records: &[WalRecord]) -> Result<Self, String> {
        crate::legacy_quantity::Replay::new(records, None)?.finish()
    }

    pub(crate) fn replay_clone(&self) -> Result<Self, String> {
        let mut state = Self::restore(&self.snapshot())?;
        state.legacy_quantities = self.legacy_quantities.clone();
        Ok(state)
    }

    pub(crate) fn apply_record(
        &mut self,
        record: &WalRecord,
        sender: &HashMap<&str, &engine_types::OrderRequest>,
        strategy_names: &[String],
    ) -> Result<(), String> {
        match record {
            WalRecord::PortfolioOffsetSettled { settlement } => {
                let prepared = self.prepare_internal_settlement(settlement)?;
                self.commit_internal_settlement(prepared)?;
            }
            WalRecord::SleeveStopSet {
                strategy,
                symbol,
                side,
                trigger_price,
                ..
            } => {
                self.set_sleeve_stop_exact(*strategy, *symbol, *side, trigger_price.clone())?;
            }
            // A fill for an order this log never recorded sending belongs
            // to somebody else on the account, unless the venue named it
            // a close of a position one sleeve holds. Anything else is
            // charged to nobody on purpose: the engine does not guess
            // whose it is, and `reconcile` is what notices the account
            // holds more than the log accounts for.
            WalRecord::OrderUpdate { update, .. } => {
                let OrderUpdate::Fill {
                    client_order_id,
                    symbol,
                    side,
                    forced_close,
                    ..
                } = update
                else {
                    return Ok(());
                };
                let request = sender.get(client_order_id.as_str()).copied();
                if matches!(
                    update,
                    OrderUpdate::Fill {
                        allocation: Some(_),
                        ..
                    }
                ) {
                    let prepared = self
                        .prepare_portfolio_update_for_order(request, strategy_names, update)?
                        .ok_or("recorded fill has no valid allocation")?;
                    self.commit_portfolio_fill(prepared, true)?;
                    if let Some(request) = request {
                        self.remember_order_stop(request);
                    }
                    return Ok(());
                }
                if request.is_some_and(|request| request.is_portfolio_reduction()) {
                    return Err("engine net execution is missing its durable allocation".into());
                }
                let strategy = request
                    .and_then(|request| request.sleeve_owner())
                    .or_else(|| {
                        forced_close_owner(self, client_order_id, *symbol, *side, *forced_close)
                    });
                let Some(strategy) = strategy else {
                    return Ok(());
                };
                self.try_on_update(strategy, update)?;
                if let Some(request) = request {
                    self.remember_order_stop(request);
                }
            }
            // A fill recovered from the venue's history joins the same
            // two ways: through the order that produced it, or through
            // the position a venue-named close reduced.
            WalRecord::RecoveredFill {
                client_order_id,
                symbol,
                side,
                forced_close,
                ..
            } => {
                let request = sender.get(client_order_id.as_str()).copied();
                if matches!(
                    record,
                    WalRecord::RecoveredFill {
                        allocation: Some(_),
                        ..
                    }
                ) {
                    let prepared = self
                        .prepare_portfolio_recovered_for_order(request, strategy_names, record)?
                        .ok_or("recorded recovered fill has no valid allocation")?;
                    self.commit_portfolio_fill(prepared, true)?;
                    if let Some(request) = request {
                        self.remember_order_stop(request);
                    }
                    return Ok(());
                }
                if request.is_some_and(|request| request.is_portfolio_reduction()) {
                    return Err("engine net execution is missing its durable allocation".into());
                }
                let strategy = request
                    .and_then(|request| request.sleeve_owner())
                    .or_else(|| {
                        forced_close_owner(self, client_order_id, *symbol, *side, *forced_close)
                    });
                let Some(strategy) = strategy else {
                    return Ok(());
                };
                self.try_on_recovered(strategy, record)?;
                if let Some(request) = request {
                    self.remember_order_stop(request);
                }
            }
            WalRecord::Retained(engine_types::wal::RetainedWalRecord::ClaimsDropped {
                rows,
                ..
            }) => self.forget(rows),
            WalRecord::LatchCleared {
                restated_exposure, ..
            } => self.keep_held(restated_exposure)?,
            // Still-open orders arrive through the same record, so
            // `sender` keeps resolving their later fills.
            WalRecord::SegmentBase {
                attribution,
                portfolio,
                intended_stops,
                ..
            } => {
                if let Some(state) = portfolio {
                    let restored = Self::restore(state)?;
                    let mut projected = std::collections::BTreeMap::new();
                    for row in attribution {
                        if !row.signed_qty.is_finite()
                            || row.signed_qty == 0.0
                            || projected
                                .insert((row.strategy, row.symbol), row.signed_qty.to_bits())
                                .is_some()
                        {
                            return Err("invalid portfolio quantity projection".into());
                        }
                    }
                    let expected: std::collections::BTreeMap<_, _> = restored
                        .rows()
                        .into_iter()
                        .map(|(strategy, symbol, quantity)| {
                            ((strategy, symbol), quantity.to_bits())
                        })
                        .collect();
                    if projected != expected {
                        return Err(
                            "portfolio snapshot disagrees with its quantity projection".into()
                        );
                    }
                    *self = restored;
                } else {
                    self.inventory.restate_legacy(attribution)?;
                    self.restate_legacy_origins(attribution)?;
                    let mut legacy = self.inventory.snapshot();
                    legacy.schema_version = 1;
                    self.accounting =
                        crate::execution_accounting::ExecutionAccounting::restore(&legacy)?;
                }
                for stop in intended_stops {
                    if let Some(strategy) = self.sole_owner(stop.symbol) {
                        let row = self
                            .inventory
                            .position(strategy, stop.symbol)
                            .expect("sole owner has a position");
                        let side = if row.signed_qty.is_positive() {
                            Side::Buy
                        } else {
                            Side::Sell
                        };
                        if portfolio.is_some() && (row.stop_px.is_some() || stop.side != Some(side))
                        {
                            continue;
                        }
                        if stop.side.is_none_or(|recorded| recorded == side) {
                            self.remember_stop(strategy, stop.symbol, side, stop.trigger_px);
                        }
                    }
                }
            }
            WalRecord::StopSet {
                symbol, trigger_px, ..
            } => {
                if let Some(strategy) = self.sole_owner(*symbol) {
                    let side = if self.signed(strategy, *symbol) > 0.0 {
                        Side::Buy
                    } else {
                        Side::Sell
                    };
                    self.remember_stop(strategy, *symbol, side, *trigger_px);
                }
            }
            _ => {}
        }
        Ok(())
    }

    /// Replace every claim with a rotation's own account of them. Set, not
    /// add: at its place in a chain read these rows are exactly what the fills
    /// before them summed to, and in a fresh segment they are all there is.
    pub fn restate(&mut self, rows: &[FilledTotal]) {
        self.inventory
            .restate_legacy(rows)
            .expect("validated legacy inventory");
        self.restate_legacy_origins(rows)
            .expect("validated legacy quantity origin");
        let mut legacy = self.inventory.snapshot();
        legacy.schema_version = 1;
        self.accounting = crate::execution_accounting::ExecutionAccounting::restore(&legacy)
            .expect("legacy accounting marker");
    }

    /// Forget the claims a boot dropped against a flat venue reading. The drop
    /// replays like everything else, or a restart would rebuild the residue
    /// from the old fills — and by then the symbol may be held by another
    /// sleeve, making it undroppable.
    pub fn forget(&mut self, rows: &[FilledTotal]) {
        for row in rows {
            self.inventory.remove(row.strategy, row.symbol);
            self.legacy_quantities.remove(&(row.strategy, row.symbol));
        }
    }

    /// Restating physical flatness cannot remove balanced virtual holdings.
    pub fn keep_held(&mut self, restated: &[SymbolTotal]) -> Result<(), String> {
        let held = restated
            .iter()
            .map(|row| {
                row.exact_quantity()
                    .map(|quantity| (row.symbol, quantity))
                    .map_err(|error| error.to_string())
            })
            .collect::<Result<Vec<_>, _>>()?;
        self.drop_where_flat(|symbol| {
            !held
                .iter()
                .any(|(known, quantity)| *known == symbol && !quantity.is_zero())
        });
        Ok(())
    }

    /// Every non-flat row, sorted, for a rotation to restate.
    pub fn rows(&self) -> Vec<(StrategyId, SymbolId, f64)> {
        self.inventory
            .rows()
            .map(|row| {
                (
                    row.strategy,
                    row.symbol,
                    row.signed_qty
                        .to_f64()
                        .expect("finite inventory projection"),
                )
            })
            .collect()
    }

    /// Charge one fill to the strategy whose order produced it. The caller
    /// resolves the owner from the order ledger, so a fill can never be
    /// charged to a strategy that did not place it. Anything that is not a
    /// fill is ignored.
    pub fn on_update(&mut self, strategy: StrategyId, update: &OrderUpdate) {
        self.try_on_update(strategy, update)
            .expect("validated owned execution");
    }

    pub fn try_on_update(
        &mut self,
        strategy: StrategyId,
        update: &OrderUpdate,
    ) -> Result<(), String> {
        if let Some(change) = self.prepare_update(strategy, update)? {
            self.commit_prepared(change)?;
        }
        Ok(())
    }

    pub(crate) fn prepare_update(
        &self,
        strategy: StrategyId,
        update: &OrderUpdate,
    ) -> Result<Option<PreparedAllocation>, String> {
        let OrderUpdate::Fill {
            symbol,
            side,
            qty,
            px,
            fee,
            amounts,
            ..
        } = update
        else {
            return Ok(None);
        };
        let fill = Self::execution_fill(
            strategy,
            *symbol,
            *side,
            *qty,
            *px,
            *fee,
            amounts.as_deref(),
        )?;
        let accounting_input = Self::accounting_input(&fill, *fee, amounts.as_deref())?;
        let legacy_input = amounts.is_none().then(|| {
            if *side == Side::Buy {
                fill.qty.clone()
            } else {
                -&fill.qty
            }
        });
        let change = self.inventory.prepare_fill(fill)?;
        let accounting =
            self.accounting
                .prepare(crate::execution_accounting::ExecutionAccountingInput {
                    realized: change.realized.clone(),
                    ..accounting_input
                })?;
        Ok(Some(PreparedAllocation {
            change,
            accounting,
            strategy,
            symbol: *symbol,
            legacy_input,
        }))
    }

    pub fn try_on_recovered(
        &mut self,
        strategy: StrategyId,
        record: &WalRecord,
    ) -> Result<(), String> {
        let change = self.prepare_recovered(strategy, record)?;
        self.commit_prepared(change)
    }

    pub(crate) fn prepare_recovered(
        &self,
        strategy: StrategyId,
        record: &WalRecord,
    ) -> Result<PreparedAllocation, String> {
        let WalRecord::RecoveredFill {
            symbol,
            side,
            qty,
            px,
            fee,
            amounts,
            ..
        } = record
        else {
            return Err("expected recovered execution".into());
        };
        let fill =
            Self::execution_fill(strategy, *symbol, *side, *qty, *px, *fee, amounts.as_ref())?;
        let accounting_input = Self::accounting_input(&fill, *fee, amounts.as_ref())?;
        let legacy_input = amounts.is_none().then(|| {
            if *side == Side::Buy {
                fill.qty.clone()
            } else {
                -&fill.qty
            }
        });
        let change = self.inventory.prepare_fill(fill)?;
        let accounting =
            self.accounting
                .prepare(crate::execution_accounting::ExecutionAccountingInput {
                    realized: change.realized.clone(),
                    ..accounting_input
                })?;
        Ok(PreparedAllocation {
            change,
            accounting,
            strategy,
            symbol: *symbol,
            legacy_input,
        })
    }

    /// This sleeve's legacy sum with one more binary64 reading in it. Taken
    /// before the fill is applied, because a note that cannot be stored must
    /// not leave the fill half applied.
    fn note_legacy_reading(
        &self,
        key: (StrategyId, SymbolId),
        signed: &engine_types::numeric::Exact,
    ) -> Result<crate::legacy_quantity::Origin, String> {
        let mut origin = self
            .legacy_quantities
            .get(&key)
            .cloned()
            .unwrap_or_default();
        origin.note(signed)?;
        Ok(origin)
    }

    /// The tail of a fill whose quantity the log keeps only as the binary64
    /// `qty` field: under `FLAT` the sum is the readings' own rounding rather
    /// than a holding, and what survives remembers the readings it came from
    /// so a grid adoption can resolve it.
    fn settle_legacy_sum(
        &mut self,
        key: (StrategyId, SymbolId),
        origin: crate::legacy_quantity::Origin,
    ) {
        if self.signed(key.0, key.1).abs() < FLAT {
            self.inventory.remove(key.0, key.1);
        }
        if self.inventory.position(key.0, key.1).is_none() {
            self.legacy_quantities.remove(&key);
        } else {
            self.legacy_quantities.insert(key, origin);
        }
    }

    pub(crate) fn commit_prepared(&mut self, prepared: PreparedAllocation) -> Result<(), String> {
        let PreparedAllocation {
            change,
            accounting,
            strategy,
            symbol,
            legacy_input,
        } = prepared;
        let key = (strategy, symbol);
        let origin = legacy_input
            .map(|input| self.note_legacy_reading(key, &input))
            .transpose()?;
        self.inventory.validate_change(&change)?;
        self.accounting.validate_change(&accounting)?;
        self.inventory.apply(change)?;
        self.accounting.apply(accounting)?;
        match origin {
            Some(origin) => self.settle_legacy_sum(key, origin),
            None if self.inventory.position(strategy, symbol).is_none() => {
                self.legacy_quantities.remove(&key);
            }
            None => {}
        }
        Ok(())
    }

    fn accounting_input(
        fill: &crate::inventory::InventoryFill,
        fee: Option<f64>,
        amounts: Option<&engine_types::numeric::ExecutionAmounts>,
    ) -> Result<crate::execution_accounting::ExecutionAccountingInput, String> {
        use engine_types::numeric::{AssetAmount, AssetId, ExactNumber};
        let fee = if let Some(amounts) = amounts {
            amounts.fee.clone()
        } else {
            fee.map(|value| {
                ExactNumber::legacy_binary64(value).map(|amount| AssetAmount {
                    asset: AssetId::Unknown,
                    amount,
                })
            })
            .transpose()
            .map_err(|e| e.to_string())?
        };
        Ok(crate::execution_accounting::ExecutionAccountingInput {
            strategy: fill.strategy,
            symbol: fill.symbol,
            side: fill.side,
            qty: fill.qty.clone(),
            px: fill.px.clone(),
            settlement_asset: fill.settlement_asset.clone(),
            fee,
            realized: crate::inventory::RealizedValue {
                amount: None,
                asset: AssetId::Unknown,
            },
        })
    }

    fn execution_fill(
        strategy: StrategyId,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        fee: Option<f64>,
        amounts: Option<&engine_types::numeric::ExecutionAmounts>,
    ) -> Result<crate::inventory::InventoryFill, String> {
        use engine_types::numeric::{AssetId, Exact};
        let (qty, px, settlement_asset) = if let Some(amounts) = amounts {
            amounts
                .validate_projection(qty, px, fee)
                .map_err(|error| error.to_string())?;
            (
                amounts.quantity.value.clone(),
                amounts.price.value.clone(),
                amounts.settlement_asset.clone(),
            )
        } else {
            (
                Exact::from_legacy_f64(qty).map_err(|error| error.to_string())?,
                Exact::from_legacy_f64(px).map_err(|error| error.to_string())?,
                AssetId::Unknown,
            )
        };
        Ok(crate::inventory::InventoryFill {
            strategy,
            symbol,
            side,
            qty,
            px: Some(px),
            stop: None,
            settlement_asset,
        })
    }

    fn apply_fill(
        &mut self,
        fill: crate::inventory::InventoryFill,
        legacy: bool,
    ) -> Result<(), String> {
        let (strategy, symbol) = (fill.strategy, fill.symbol);
        let legacy_input = legacy.then(|| {
            if fill.side == Side::Buy {
                fill.qty.clone()
            } else {
                -&fill.qty
            }
        });
        let input = Self::accounting_input(&fill, None, None)?;
        let change = self.inventory.prepare_fill(fill)?;
        let accounting =
            self.accounting
                .prepare(crate::execution_accounting::ExecutionAccountingInput {
                    realized: change.realized.clone(),
                    ..input
                })?;
        self.commit_prepared(PreparedAllocation {
            change,
            accounting,
            strategy,
            symbol,
            legacy_input,
        })
    }

    pub fn note(&mut self, strategy: StrategyId, symbol: SymbolId, side: Side, qty: f64) {
        use engine_types::numeric::{AssetId, Exact};
        let Ok(qty) = Exact::from_legacy_f64(qty) else {
            return;
        };
        let _ = self.apply_fill(
            crate::inventory::InventoryFill {
                strategy,
                symbol,
                side,
                qty,
                px: None,
                stop: None,
                settlement_asset: AssetId::Unknown,
            },
            true,
        );
    }

    pub fn snapshot(&self) -> engine_types::portfolio::PortfolioState {
        let mut state = self.inventory.snapshot();
        self.accounting.write_snapshot(&mut state);
        state.internal_settlements = self.internal.snapshot();
        state
    }

    pub fn restore(state: &engine_types::portfolio::PortfolioState) -> Result<Self, String> {
        Ok(Self {
            inventory: crate::inventory::Inventory::restore(state)?,
            legacy_quantities: Default::default(),
            internal: internal::InternalAccounting::restore(&state.internal_settlements)?,
            accounting: crate::execution_accounting::ExecutionAccounting::restore(state)?,
        })
    }

    fn restate_legacy_origins(&mut self, rows: &[FilledTotal]) -> Result<(), String> {
        let mut origins = std::collections::BTreeMap::new();
        for row in rows {
            if row.signed_qty == 0.0 {
                continue;
            }
            let mut origin = crate::legacy_quantity::Origin::default();
            origin.note(
                &engine_types::numeric::Exact::from_legacy_f64(row.signed_qty)
                    .map_err(|e| e.to_string())?,
            )?;
            origins.insert((row.strategy, row.symbol), origin);
        }
        self.legacy_quantities = origins;
        Ok(())
    }

    pub(crate) fn adopt_legacy_quantity_subset(
        &mut self,
        corrections: &[engine_types::LegacySleeveQuantityCorrection],
    ) -> Result<(), String> {
        let mut state = self.inventory.snapshot();
        let mut seen = std::collections::BTreeSet::new();
        for correction in corrections {
            let key = (correction.strategy, correction.symbol);
            let origin = self
                .legacy_quantities
                .get(&key)
                .ok_or("adoption has no legacy owner")?;
            let row = state
                .positions
                .iter_mut()
                .find(|row| (row.strategy, row.symbol) == key)
                .ok_or("adoption has no current owner")?;
            if !seen.insert(key)
                || row.signed_qty != correction.before
                || correction.after != origin.resolve(&correction.before, &correction.step)?
            {
                return Err(
                    "adoption changes its original owner quantity or grid resolution".into(),
                );
            }
            row.signed_qty = correction.after.clone();
        }
        state.positions.retain(|row| !row.signed_qty.is_zero());
        self.inventory = crate::inventory::Inventory::restore(&state)?;
        for correction in corrections {
            self.legacy_quantities
                .remove(&(correction.strategy, correction.symbol));
        }
        Ok(())
    }

    pub fn remember_order_stop(&mut self, request: &engine_types::OrderRequest) {
        if request.is_sleeve_reduction() {
            return;
        }
        if let Some(terms) = &request.exact_terms {
            if let Some(stop) = &terms.stop_trigger_price {
                self.inventory.tighten_stop(
                    request.strategy,
                    request.symbol,
                    request.side,
                    stop.clone(),
                );
            }
            return;
        }
        if let Some(stop) = request.sleeve_stop() {
            self.remember_stop(
                request.strategy,
                request.symbol,
                request.side,
                stop.trigger_px,
            );
        }
    }

    pub fn remember_stop(&mut self, strategy: StrategyId, symbol: SymbolId, side: Side, stop: f64) {
        if let Ok(stop) = engine_types::numeric::Exact::from_legacy_f64(stop) {
            self.inventory.tighten_stop(strategy, symbol, side, stop);
        }
    }

    pub fn allocated(
        &self,
        strategy: StrategyId,
        symbol: SymbolId,
    ) -> Option<engine_types::strategy::StrategyAllocatedPosition> {
        let position = self.inventory.position(strategy, symbol)?;
        Some(engine_types::strategy::StrategyAllocatedPosition {
            entry_px: position
                .entry_value
                .as_ref()
                .and_then(|cost| cost.checked_div(&position.signed_qty.abs()).ok())
                .and_then(|price| price.to_f64().ok()),
            stop_px: position
                .stop_px
                .as_ref()
                .and_then(|stop| stop.to_f64().ok()),
        })
    }

    pub(crate) fn positions_on_symbol(
        &self,
        symbol: SymbolId,
    ) -> impl Iterator<Item = &engine_types::portfolio::PortfolioPosition> {
        self.inventory.positions_on_symbol(symbol)
    }

    pub fn signed_exact(
        &self,
        strategy: StrategyId,
        symbol: SymbolId,
    ) -> engine_types::numeric::Exact {
        self.inventory
            .position(strategy, symbol)
            .map_or_else(engine_types::numeric::Exact::zero, |row| {
                row.signed_qty.clone()
            })
    }

    /// Signed quantity this strategy's own orders opened in this symbol.
    pub fn signed(&self, strategy: StrategyId, symbol: SymbolId) -> f64 {
        self.inventory
            .position(strategy, symbol)
            .map_or(0.0, |row| {
                row.signed_qty
                    .to_f64()
                    .expect("validated inventory projection")
            })
    }

    pub(crate) fn all_symbols(&self) -> impl Iterator<Item = SymbolId> + '_ {
        self.inventory.rows().map(|row| row.symbol)
    }

    /// Symbols this strategy still has a non-flat fill claim on.
    pub fn symbols(&self, strategy: StrategyId) -> impl Iterator<Item = SymbolId> + '_ {
        self.inventory
            .rows()
            .filter_map(move |row| (row.strategy == strategy).then_some(row.symbol))
    }

    /// The only sleeve with a non-flat claim on this symbol.
    pub fn sole_owner(&self, symbol: SymbolId) -> Option<StrategyId> {
        let mut owners = self
            .inventory
            .rows()
            .filter_map(|row| (row.symbol == symbol).then_some(row.strategy));
        let owner = owners.next()?;
        owners.next().is_none().then_some(owner)
    }

    /// Remove unexplained nonzero claims only when the venue is conclusively flat.
    pub fn drop_where_flat(
        &mut self,
        flat: impl Fn(SymbolId) -> bool,
    ) -> Vec<(StrategyId, SymbolId, f64)> {
        let dropped: Vec<_> = self
            .rows()
            .into_iter()
            .filter(|(_, symbol, _)| flat(*symbol) && self.sole_owner(*symbol).is_some())
            .collect();
        for (strategy, symbol, _) in &dropped {
            self.inventory.remove(*strategy, *symbol);
            self.legacy_quantities.remove(&(*strategy, *symbol));
        }
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
        self.inventory
            .rows()
            .any(|row| row.symbol == symbol && row.strategy != mine)
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

    #[test]
    fn physical_flatness_cannot_discard_unsettled_shared_inventory() {
        let records = vec![
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            sent("b", LONG, BTC),
            fill("b", BTC, Side::Sell, 1.0),
        ];
        let mut attribution = Attribution::from_records(&records);
        assert!(attribution.drop_where_flat(|_| true).is_empty());
        attribution.keep_held(&[]).unwrap();
        assert_eq!(attribution.signed(CARRY, BTC), 2.0);
        assert_eq!(attribution.signed(LONG, BTC), -1.0);
        let state =
            serde_json::from_slice(&serde_json::to_vec(&attribution.snapshot()).unwrap()).unwrap();
        let mut restarted = Attribution::restore(&state).unwrap();
        assert!(restarted.drop_where_flat(|_| true).is_empty());
    }

    #[test]
    fn opposing_sleeves_survive_a_flat_venue_reading_and_restart() {
        let records = vec![
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            sent("b", LONG, BTC),
            fill("b", BTC, Side::Sell, 2.0),
        ];
        let mut attribution = Attribution::from_records(&records);
        assert!(attribution.drop_where_flat(|_| true).is_empty());
        let encoded = serde_json::to_vec(&attribution.snapshot()).unwrap();
        let state = serde_json::from_slice(&encoded).unwrap();
        let mut restarted = Attribution::restore(&state).unwrap();
        assert!(restarted.drop_where_flat(|_| true).is_empty());
        assert_eq!(restarted.signed(CARRY, BTC), 2.0);
        assert_eq!(restarted.signed(LONG, BTC), -2.0);
    }

    #[test]
    fn net_flat_restatement_preserves_offsets_but_drops_unexplained_nonzero_claims() {
        let mut attribution = Attribution::from_records(&[
            sent("a", CARRY, BTC),
            fill("a", BTC, Side::Buy, 2.0),
            sent("b", LONG, BTC),
            fill("b", BTC, Side::Sell, 2.0),
            sent("c", CARRY, ETH),
            fill("c", ETH, Side::Buy, 3.0),
        ]);
        attribution.keep_held(&[]).unwrap();
        assert_eq!(attribution.signed(CARRY, BTC), 2.0);
        assert_eq!(attribution.signed(LONG, BTC), -2.0);
        assert_eq!(attribution.signed(CARRY, ETH), 0.0);
    }

    fn sent(id: &str, strategy: StrategyId, symbol: SymbolId) -> WalRecord {
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: id.to_string(),
                strategy,
                symbol,
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                exact_terms: None,
                sleeve_effect: None,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }

    fn fill(id: &str, symbol: SymbolId, side: Side, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
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
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
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
            callbacks: None,
            allocation: None,
            amounts: None,
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
            WalRecord::Retained(engine_types::wal::RetainedWalRecord::ClaimsDropped {
                wall_ts_ms: 2,
                rows: vec![engine_types::FilledTotal {
                    strategy: CARRY,
                    symbol: BTC,
                    signed_qty: 2.0,
                }],
            }),
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
                    exact_signed_qty: None,
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

#[cfg(test)]
mod exact_stop_tests {
    use super::*;
    use engine_types::numeric::{AssetId, Exact};
    use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
    use engine_types::portfolio::{PortfolioPosition, PortfolioState};
    #[test]
    fn exact_order_stop_retains_its_decimal_value_in_owned_rotation_state() {
        let exact = |s: &str| Exact::parse_decimal(s).unwrap();
        let mut inventory = Attribution::restore(&PortfolioState {
            positions: vec![PortfolioPosition {
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                signed_qty: exact("1"),
                entry_value: Some(exact("100")),
                stop_px: None,
                settlement_asset: AssetId::Unknown,
            }],
            ..PortfolioState::default()
        })
        .unwrap();
        let terms = ExactOrderTerms {
            quantity: exact("1"),
            limit_price: None,
            stop_trigger_price: Some(exact("90.1")),
            physical_stop_trigger_price: Some(exact("90.1")),
            input_policy: OrderInputPolicy::StrategyShortestDecimal,
        };
        let mut order = engine_types::OrderRequest {
            client_order_id: "exact-stop".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: engine_types::OrderKind::Market,
            stop: Some(engine_types::StopSpec { trigger_px: 90.1 }),
            reduce_only: false,
            close_position: false,
            exact_terms: None,
            sleeve_effect: None,
        };
        terms.apply_projection(&mut order).unwrap();
        inventory.remember_order_stop(&order);
        let state = inventory.snapshot();
        assert_eq!(
            state.positions[0].stop_px,
            Some(exact("90.1")),
            "durable protection must not regain a binary rounding error"
        );
        let bytes = serde_json::to_vec(&state).unwrap();
        let restored = Attribution::restore(&serde_json::from_slice(&bytes).unwrap()).unwrap();
        assert_eq!(restored.snapshot(), state);
    }
}
