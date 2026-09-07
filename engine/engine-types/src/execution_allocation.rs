use crate::numeric::{AssetAmount, Exact};
use crate::StrategyId;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AllocationPolicy {
    DirectOrder,
    EmergencyNetFifo,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecutionSlice {
    pub strategy: StrategyId,
    pub strategy_key: String,
    pub quantity: Exact,
    pub fee: Option<AssetAmount>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecutionAllocation {
    pub policy: AllocationPolicy,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub legacy_quantity_step: Option<Exact>,
    pub slices: Vec<ExecutionSlice>,
}
