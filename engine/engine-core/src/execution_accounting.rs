use std::collections::{BTreeMap, BTreeSet};

use engine_types::numeric::{AssetAmount, AssetId, Exact};
use engine_types::portfolio::{AssetExecutionTotals, PortfolioState, UnvaluedExecutionTotals};
use engine_types::{Side, StrategyId, SymbolId};

use crate::inventory::RealizedValue;

type AssetKey = (StrategyId, SymbolId, AssetId);
type SleeveKey = (StrategyId, SymbolId);

#[derive(Debug)]
pub(crate) struct ExecutionAccounting {
    balances: BTreeMap<AssetKey, AssetExecutionTotals>,
    unvalued: BTreeMap<SleeveKey, UnvaluedExecutionTotals>,
    complete_from_start: bool,
}
impl Default for ExecutionAccounting {
    fn default() -> Self {
        Self {
            balances: BTreeMap::new(),
            unvalued: BTreeMap::new(),
            complete_from_start: true,
        }
    }
}

pub(crate) struct ExecutionAccountingInput {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: Exact,
    pub px: Option<Exact>,
    pub settlement_asset: AssetId,
    pub fee: Option<AssetAmount>,
    pub realized: RealizedValue,
}

#[derive(Debug)]
struct BalanceChange {
    key: AssetKey,
    prior: Option<AssetExecutionTotals>,
    next: AssetExecutionTotals,
}
#[derive(Debug)]
struct UnvaluedChange {
    key: SleeveKey,
    prior: Option<UnvaluedExecutionTotals>,
    next: UnvaluedExecutionTotals,
}
#[derive(Debug)]
pub(crate) struct AccountingChange {
    balances: Vec<BalanceChange>,
    unvalued: Vec<UnvaluedChange>,
}
pub(crate) type AccountingBatch = AccountingChange;

impl ExecutionAccounting {
    pub(crate) fn restore(state: &PortfolioState) -> Result<Self, String> {
        if !matches!(state.schema_version, 1 | 2) {
            return Err("unsupported execution accounting schema".into());
        }
        let mut accounting = Self::default();
        if state.schema_version == 1 {
            if !state.accounting.is_empty() || !state.unvalued.is_empty() {
                return Err("legacy portfolio carries unsupported accounting fields".into());
            }
            accounting.complete_from_start = false;
            for position in &state.positions {
                let key = (position.strategy, position.symbol);
                accounting
                    .unvalued
                    .entry(key)
                    .or_insert_with(|| UnvaluedExecutionTotals {
                        legacy_prefix: true,
                        ..UnvaluedExecutionTotals::empty(key.0, key.1)
                    });
            }
            return Ok(accounting);
        }
        if state.accounting_complete_from_start
            && state.unvalued.iter().any(|row| row.legacy_prefix)
        {
            return Err("complete accounting prefix conflicts with a legacy gap marker".into());
        }
        accounting.complete_from_start = state.accounting_complete_from_start;
        for row in &state.accounting {
            validate_balance(row)?;
            let key = (row.strategy, row.symbol, row.asset.clone());
            if accounting.balances.insert(key, row.clone()).is_some() {
                return Err("duplicate asset execution accounting row".into());
            }
        }
        for row in &state.unvalued {
            if accounting
                .unvalued
                .insert((row.strategy, row.symbol), row.clone())
                .is_some()
            {
                return Err("duplicate unvalued execution accounting row".into());
            }
        }
        Ok(accounting)
    }

    pub(crate) fn write_snapshot(&self, state: &mut PortfolioState) {
        state.schema_version = 2;
        state.accounting = self.balances.values().cloned().collect();
        state.unvalued = self.unvalued.values().cloned().collect();
        state.accounting_complete_from_start = self.complete_from_start;
    }

    pub(crate) fn prepare(
        &self,
        input: ExecutionAccountingInput,
    ) -> Result<AccountingChange, String> {
        let ExecutionAccountingInput {
            strategy,
            symbol,
            side,
            qty,
            px,
            settlement_asset,
            fee,
            realized,
        } = input;
        if !qty.is_positive() || px.as_ref().is_some_and(|price| !price.is_positive()) {
            return Err("invalid execution accounting quantity or price".into());
        }
        qty.validate_storage().map_err(|e| e.to_string())?;
        if let Some(price) = &px {
            price.validate_storage().map_err(|e| e.to_string())?;
        }
        let mut touched = BTreeMap::<AssetId, AssetExecutionTotals>::new();
        let key = (strategy, symbol);
        let prior_unvalued = self.unvalued.get(&key).cloned();
        let mut unvalued = prior_unvalued
            .clone()
            .unwrap_or_else(|| UnvaluedExecutionTotals::empty(strategy, symbol));
        match (&settlement_asset, &px) {
            (AssetId::Named(_), Some(price)) => {
                let value = qty * price;
                balance(&mut touched, self, (strategy, symbol), &settlement_asset)?
                    .execution_cash_flow += if side == Side::Sell { value } else { -value };
            }
            _ => increment(&mut unvalued.execution_cash_flow_events)?,
        }
        match fee {
            Some(fee) => {
                fee.amount
                    .validate_provenance()
                    .map_err(|e| e.to_string())?;
                fee.amount
                    .value
                    .validate_storage()
                    .map_err(|e| e.to_string())?;
                match &fee.asset {
                    AssetId::Named(_) => {
                        balance(&mut touched, self, (strategy, symbol), &fee.asset)?.fees +=
                            fee.amount.value
                    }
                    AssetId::Unknown if !fee.amount.value.is_zero() => {
                        increment(&mut unvalued.fee_events)?
                    }
                    AssetId::Unknown => (),
                }
            }
            None => increment(&mut unvalued.fee_events)?,
        }
        match realized.amount {
            Some(amount) => {
                amount.validate_storage().map_err(|e| e.to_string())?;
                match &realized.asset {
                    AssetId::Named(_) => {
                        balance(&mut touched, self, (strategy, symbol), &realized.asset)?
                            .realized_gross += amount
                    }
                    AssetId::Unknown if !amount.is_zero() => {
                        increment(&mut unvalued.realized_events)?
                    }
                    AssetId::Unknown => (),
                }
            }
            None => increment(&mut unvalued.realized_events)?,
        }
        let mut balances = Vec::with_capacity(touched.len());
        for (asset, next) in touched {
            validate_balance(&next)?;
            let key = (strategy, symbol, asset);
            balances.push(BalanceChange {
                prior: self.balances.get(&key).cloned(),
                key,
                next,
            });
        }
        Ok(AccountingChange {
            balances,
            unvalued: if Some(&unvalued) != prior_unvalued.as_ref() && !unvalued_empty(&unvalued) {
                vec![UnvaluedChange {
                    key,
                    prior: prior_unvalued,
                    next: unvalued,
                }]
            } else {
                Vec::new()
            },
        })
    }

    pub(crate) fn prepare_batch(
        &self,
        inputs: Vec<ExecutionAccountingInput>,
    ) -> Result<AccountingBatch, String> {
        let mut staged = Self::default();
        let mut prior_balances = BTreeMap::<AssetKey, Option<AssetExecutionTotals>>::new();
        let mut prior_unvalued = BTreeMap::<SleeveKey, Option<UnvaluedExecutionTotals>>::new();
        for input in inputs {
            let owner = (input.strategy, input.symbol);
            let mut assets =
                BTreeSet::from([input.settlement_asset.clone(), input.realized.asset.clone()]);
            if let Some(fee) = &input.fee {
                assets.insert(fee.asset.clone());
            }
            for asset in assets {
                if asset == AssetId::Unknown {
                    continue;
                }
                let key = (owner.0, owner.1, asset);
                prior_balances.entry(key.clone()).or_insert_with(|| {
                    let prior = self.balances.get(&key).cloned();
                    if let Some(row) = &prior {
                        staged.balances.insert(key, row.clone());
                    }
                    prior
                });
            }
            prior_unvalued.entry(owner).or_insert_with(|| {
                let prior = self.unvalued.get(&owner).cloned();
                if let Some(row) = &prior {
                    staged.unvalued.insert(owner, row.clone());
                }
                prior
            });
            let change = staged.prepare(input)?;
            staged.apply(change)?;
        }
        Ok(AccountingChange {
            balances: prior_balances
                .into_iter()
                .filter_map(|(key, prior)| {
                    staged
                        .balances
                        .remove(&key)
                        .filter(|next| Some(next) != prior.as_ref())
                        .map(|next| BalanceChange { key, prior, next })
                })
                .collect(),
            unvalued: prior_unvalued
                .into_iter()
                .filter_map(|(key, prior)| {
                    staged
                        .unvalued
                        .remove(&key)
                        .filter(|next| Some(next) != prior.as_ref())
                        .map(|next| UnvaluedChange { key, prior, next })
                })
                .collect(),
        })
    }

    pub(crate) fn validate_change(&self, change: &AccountingChange) -> Result<(), String> {
        if change
            .balances
            .iter()
            .any(|row| self.balances.get(&row.key) != row.prior.as_ref())
            || change
                .unvalued
                .iter()
                .any(|row| self.unvalued.get(&row.key) != row.prior.as_ref())
        {
            return Err("stale prepared execution accounting change".into());
        }
        Ok(())
    }
    pub(crate) fn validate_batch(&self, change: &AccountingBatch) -> Result<(), String> {
        self.validate_change(change)
    }
    pub(crate) fn apply(&mut self, change: AccountingChange) -> Result<(), String> {
        self.validate_change(&change)?;
        for row in change.balances {
            self.balances.insert(row.key, row.next);
        }
        for row in change.unvalued {
            self.unvalued.insert(row.key, row.next);
        }
        Ok(())
    }
    pub(crate) fn apply_batch(&mut self, change: AccountingBatch) -> Result<(), String> {
        self.apply(change)
    }
}

fn validate_asset(asset: &AssetId) -> Result<(), String> {
    match asset {
        AssetId::Named(name) if !name.is_empty() => Ok(()),
        _ => Err("asset accounting requires an explicit currency".into()),
    }
}
fn validate_balance(row: &AssetExecutionTotals) -> Result<(), String> {
    validate_asset(&row.asset)?;
    for value in [&row.execution_cash_flow, &row.fees, &row.realized_gross] {
        value.validate_storage().map_err(|e| e.to_string())?;
    }
    Ok(())
}
fn increment(value: &mut u64) -> Result<(), String> {
    *value = value
        .checked_add(1)
        .ok_or_else(|| "unvalued execution counter overflow".to_owned())?;
    Ok(())
}
fn unvalued_empty(row: &UnvaluedExecutionTotals) -> bool {
    row.execution_cash_flow_events == 0
        && row.fee_events == 0
        && row.realized_events == 0
        && !row.legacy_prefix
}

fn balance<'a>(
    touched: &'a mut BTreeMap<AssetId, AssetExecutionTotals>,
    accounting: &ExecutionAccounting,
    owner: SleeveKey,
    asset: &AssetId,
) -> Result<&'a mut AssetExecutionTotals, String> {
    validate_asset(asset)?;
    Ok(touched.entry(asset.clone()).or_insert_with(|| {
        accounting
            .balances
            .get(&(owner.0, owner.1, asset.clone()))
            .cloned()
            .unwrap_or_else(|| AssetExecutionTotals::zero(owner.0, owner.1, asset.clone()))
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::numeric::{ExactNumber, MAX_RATIO_DIGITS};
    fn exact(value: &str) -> Exact {
        Exact::parse_decimal(value).unwrap()
    }
    fn named(value: &str) -> AssetId {
        AssetId::Named(value.into())
    }
    fn input(
        side: Side,
        qty: &str,
        px: Option<&str>,
        asset: AssetId,
        fee: Option<(&str, AssetId)>,
        realized: Option<&str>,
    ) -> ExecutionAccountingInput {
        ExecutionAccountingInput {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side,
            qty: exact(qty),
            px: px.map(exact),
            settlement_asset: asset.clone(),
            fee: fee.map(|(amount, asset)| AssetAmount {
                asset,
                amount: ExactNumber::venue_decimal(amount).unwrap(),
            }),
            realized: RealizedValue {
                amount: realized.map(exact),
                asset,
            },
        }
    }
    fn state(accounting: &ExecutionAccounting) -> PortfolioState {
        let mut state = PortfolioState::default();
        accounting.write_snapshot(&mut state);
        state
    }
    fn apply(accounting: &mut ExecutionAccounting, input: ExecutionAccountingInput) {
        let change = accounting.prepare(input).unwrap();
        accounting.apply(change).unwrap();
    }

    #[test]
    fn separate_asset_fees_rebates_and_exact_realized_values_survive_rotation() {
        let mut accounting = ExecutionAccounting::default();
        apply(
            &mut accounting,
            input(
                Side::Buy,
                "0.3",
                Some("100"),
                named("USDT"),
                Some(("0.0003", named("BNB"))),
                Some("0"),
            ),
        );
        apply(
            &mut accounting,
            input(
                Side::Sell,
                "0.1",
                Some("110"),
                named("USDT"),
                Some(("-0.01", named("USDT"))),
                Some("1"),
            ),
        );
        let snapshot = state(&accounting);
        let usdt = snapshot
            .accounting
            .iter()
            .find(|row| row.asset == named("USDT"))
            .unwrap();
        assert_eq!(usdt.execution_cash_flow, exact("-19"));
        assert_eq!(usdt.fees, exact("-0.01"));
        assert_eq!(usdt.realized_gross, Exact::one());
        let bnb = snapshot
            .accounting
            .iter()
            .find(|row| row.asset == named("BNB"))
            .unwrap();
        assert_eq!(bnb.fees, exact("0.0003"));
        assert!(bnb.realized_gross.is_zero());
        let decoded = serde_json::from_str(&serde_json::to_string(&snapshot).unwrap()).unwrap();
        let mut restored = ExecutionAccounting::restore(&decoded).unwrap();
        assert_eq!(state(&restored), snapshot);
        apply(
            &mut restored,
            input(
                Side::Sell,
                "0.2",
                Some("120"),
                named("USDT"),
                Some(("0.001", named("BNB"))),
                Some("4"),
            ),
        );
        let complete = state(&restored);
        let usdt = complete
            .accounting
            .iter()
            .find(|row| row.asset == named("USDT"))
            .unwrap();
        assert_eq!(usdt.execution_cash_flow, exact("5"));
        assert_eq!(usdt.realized_gross, exact("5"));
        assert_eq!(usdt.fees, exact("-0.01"));
    }

    #[test]
    fn unknown_units_and_missing_values_stay_marked_without_currency_sums() {
        let mut accounting = ExecutionAccounting::default();
        apply(
            &mut accounting,
            input(
                Side::Buy,
                "1",
                Some("10"),
                AssetId::Unknown,
                Some(("1.2", AssetId::Unknown)),
                Some("2"),
            ),
        );
        apply(
            &mut accounting,
            input(Side::Sell, "1", None, named("USDT"), None, None),
        );
        let snapshot = state(&accounting);
        assert!(snapshot.accounting.is_empty());
        assert_eq!(snapshot.unvalued.len(), 1);
        let row = &snapshot.unvalued[0];
        assert_eq!(row.execution_cash_flow_events, 2);
        assert_eq!(row.fee_events, 2);
        assert_eq!(row.realized_events, 2);
        let restored = ExecutionAccounting::restore(&snapshot).unwrap();
        assert_eq!(state(&restored), snapshot);
        apply(
            &mut accounting,
            input(
                Side::Buy,
                "1",
                Some("1"),
                AssetId::Unknown,
                Some(("0", AssetId::Unknown)),
                Some("0"),
            ),
        );
        let row = &state(&accounting).unvalued[0];
        assert_eq!(row.fee_events, 2, "explicit zero became an unknown fee");
        assert_eq!(row.realized_events, 2);
    }

    #[test]
    fn legacy_prefix_uncertainty_survives_an_empty_position_snapshot_and_future_exact_rows() {
        let legacy = PortfolioState {
            schema_version: 1,
            ..Default::default()
        };
        let mut accounting = ExecutionAccounting::restore(&legacy).unwrap();
        assert!(!state(&accounting).accounting_complete_from_start);
        apply(
            &mut accounting,
            input(
                Side::Buy,
                "1",
                Some("2"),
                named("USDT"),
                Some(("0", named("USDT"))),
                Some("0"),
            ),
        );
        let restored = ExecutionAccounting::restore(&state(&accounting)).unwrap();
        assert!(!state(&restored).accounting_complete_from_start);
        assert_eq!(
            state(&restored).accounting[0].execution_cash_flow,
            exact("-2")
        );
    }

    #[test]
    fn prepare_and_batch_failure_do_not_mutate_or_partially_commit() {
        let mut accounting = ExecutionAccounting::default();
        let before = state(&accounting);
        let first = input(
            Side::Buy,
            "1",
            Some("10"),
            named("USDT"),
            Some(("0.1", named("BNB"))),
            Some("0"),
        );
        let mut invalid = input(Side::Buy, "1", Some("10"), named("USDT"), None, None);
        invalid.qty = -Exact::one();
        assert!(accounting.prepare_batch(vec![first, invalid]).is_err());
        assert_eq!(state(&accounting), before);
        let token = accounting
            .prepare(input(
                Side::Buy,
                "1",
                Some("10"),
                named("USDT"),
                Some(("0.1", named("BNB"))),
                Some("0"),
            ))
            .unwrap();
        assert_eq!(state(&accounting), before);
        apply(
            &mut accounting,
            input(
                Side::Buy,
                "1",
                Some("10"),
                named("USDT"),
                Some(("0.2", named("BNB"))),
                Some("0"),
            ),
        );
        let changed = state(&accounting);
        assert!(accounting.apply(token).is_err());
        assert_eq!(state(&accounting), changed);
    }

    #[test]
    fn a_batch_accumulates_shared_rows_once_and_checks_every_prior_before_applying() {
        let mut accounting = ExecutionAccounting::default();
        let token = accounting
            .prepare_batch(vec![
                input(
                    Side::Buy,
                    "1",
                    Some("10"),
                    named("USDT"),
                    Some(("0.1", named("BNB"))),
                    Some("0"),
                ),
                input(
                    Side::Sell,
                    "1",
                    Some("12"),
                    named("USDT"),
                    Some(("0.2", named("BNB"))),
                    Some("2"),
                ),
            ])
            .unwrap();
        accounting.validate_batch(&token).unwrap();
        accounting.apply_batch(token).unwrap();
        let snapshot = state(&accounting);
        let usdt = snapshot
            .accounting
            .iter()
            .find(|row| row.asset == named("USDT"))
            .unwrap();
        let bnb = snapshot
            .accounting
            .iter()
            .find(|row| row.asset == named("BNB"))
            .unwrap();
        assert_eq!(usdt.execution_cash_flow, exact("2"));
        assert_eq!(usdt.realized_gross, exact("2"));
        assert_eq!(bnb.fees, exact("0.3"));
    }

    #[test]
    fn a_complete_prefix_cannot_coexist_with_a_legacy_gap_marker() {
        let mut snapshot = PortfolioState::default();
        snapshot.unvalued.push(UnvaluedExecutionTotals {
            legacy_prefix: true,
            ..UnvaluedExecutionTotals::empty(StrategyId(0), SymbolId(0))
        });
        assert!(
            ExecutionAccounting::restore(&snapshot).is_err(),
            "contradictory accounting completeness was restored"
        );
    }

    #[test]
    fn storage_counter_overflow_and_invalid_restore_are_refused_before_mutation() {
        let huge = Exact::from_ratio(&"9".repeat(MAX_RATIO_DIGITS), "1").unwrap();
        let mut snapshot = PortfolioState::default();
        snapshot.accounting.push(AssetExecutionTotals {
            fees: huge.clone(),
            ..AssetExecutionTotals::zero(StrategyId(0), SymbolId(0), named("USDT"))
        });
        let mut accounting = ExecutionAccounting::restore(&snapshot).unwrap();
        let before = state(&accounting);
        assert!(accounting
            .prepare(input(
                Side::Buy,
                "1",
                Some("1"),
                named("USDT"),
                Some(("1", named("USDT"))),
                Some("0")
            ))
            .is_err());
        assert_eq!(state(&accounting), before);
        snapshot.accounting[0].fees = huge + Exact::one();
        assert!(ExecutionAccounting::restore(&snapshot).is_err());
        snapshot = PortfolioState::default();
        snapshot.unvalued.push(UnvaluedExecutionTotals {
            fee_events: u64::MAX,
            ..UnvaluedExecutionTotals::empty(StrategyId(0), SymbolId(0))
        });
        accounting = ExecutionAccounting::restore(&snapshot).unwrap();
        let before = state(&accounting);
        assert!(accounting
            .prepare(input(
                Side::Buy,
                "1",
                Some("1"),
                named("USDT"),
                None,
                Some("0")
            ))
            .is_err());
        assert_eq!(state(&accounting), before);
        snapshot.accounting.push(AssetExecutionTotals::zero(
            StrategyId(0),
            SymbolId(0),
            AssetId::Unknown,
        ));
        assert!(
            ExecutionAccounting::restore(&snapshot).is_err(),
            "unknown unit entered asset totals"
        );
    }
}
