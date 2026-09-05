use crate::numeric::{AssetId, Exact};
use crate::{StrategyId, SymbolId};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioExit {
    pub id: u64,
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub position_side: crate::Side,
    pub target_remaining: Exact,
    pub trigger_price: Option<Exact>,
    pub started_ms: i64,
    pub attempt: u32,
    pub order_id: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PortfolioEmergencyReason {
    ExitUnavailable,
    NativeClose,
    ProtectionUnavailable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PortfolioEmergencyPhase {
    ResolveOrders,
    CloseNet,
    SettleOffsets,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioEmergency {
    pub id: u64,
    pub symbol: SymbolId,
    pub reference_price: Exact,
    pub reason: PortfolioEmergencyReason,
    pub phase: PortfolioEmergencyPhase,
    pub started_ms: i64,
    pub attempt: u32,
    pub order_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioControlState {
    pub schema_version: u16,
    pub next_id: u64,
    #[serde(default)]
    pub native_pending: Vec<(SymbolId, Exact)>,
    pub exits: Vec<PortfolioExit>,
    pub emergencies: Vec<PortfolioEmergency>,
}
impl Default for PortfolioControlState {
    fn default() -> Self {
        Self {
            schema_version: 1,
            next_id: 1,
            native_pending: vec![],
            exits: vec![],
            emergencies: vec![],
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioOffsetSlice {
    pub strategy: StrategyId,
    pub signed_quantity: Exact,
    pub settlement_asset: AssetId,
}

/// Internal closure of balanced virtual allocations; never a venue execution.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioOffsetSettlement {
    pub emergency_id: u64,
    pub symbol: SymbolId,
    pub price: Exact,
    pub settled_ms: i64,
    pub slices: Vec<PortfolioOffsetSlice>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct InternalSettlementTotals {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub asset: AssetId,
    pub cash_flow: Exact,
    pub realized_gross: Exact,
    pub unvalued_realized_events: u64,
}
