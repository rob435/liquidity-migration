use serde::{Deserialize, Serialize};

use crate::numeric::{AssetId, Exact};
use crate::{StrategyId, SymbolId};

/// A sleeve's settled allocation, independent of the venue's net position.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioPosition {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub signed_qty: Exact,
    /// Absolute cost of the remaining units; legacy quantity-only rotations are unknown.
    pub entry_value: Option<Exact>,
    pub stop_px: Option<Exact>,
    pub settlement_asset: AssetId,
}

/// Trade consideration and realized value; these are not collateral transfers.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AssetExecutionTotals {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub asset: AssetId,
    /// Sell consideration positive; buy consideration negative.
    pub execution_cash_flow: Exact,
    /// Costs positive; rebates negative, in this asset only.
    pub fees: Exact,
    pub realized_gross: Exact,
}

/// Raw unknown-unit values remain in execution WAL records, never currency totals.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct UnvaluedExecutionTotals {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub execution_cash_flow_events: u64,
    pub fee_events: u64,
    pub realized_events: u64,
    pub legacy_prefix: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioState {
    pub schema_version: u16,
    pub positions: Vec<PortfolioPosition>,
    #[serde(default)]
    pub accounting: Vec<AssetExecutionTotals>,
    #[serde(default)]
    pub unvalued: Vec<UnvaluedExecutionTotals>,
    /// False when a legacy rotation omits any prefix of execution accounting.
    #[serde(default)]
    pub accounting_complete_from_start: bool,
}

impl Default for PortfolioState {
    fn default() -> Self {
        Self {
            schema_version: 2,
            positions: Vec::new(),
            accounting: Vec::new(),
            unvalued: Vec::new(),
            accounting_complete_from_start: true,
        }
    }
}

impl AssetExecutionTotals {
    pub fn zero(strategy: StrategyId, symbol: SymbolId, asset: AssetId) -> Self {
        Self {
            strategy,
            symbol,
            asset,
            execution_cash_flow: Exact::zero(),
            fees: Exact::zero(),
            realized_gross: Exact::zero(),
        }
    }
}
impl UnvaluedExecutionTotals {
    pub fn empty(strategy: StrategyId, symbol: SymbolId) -> Self {
        Self {
            strategy,
            symbol,
            execution_cash_flow_events: 0,
            fee_events: 0,
            realized_events: 0,
            legacy_prefix: false,
        }
    }
}
