//! Open trade cost basis retained across WAL rotation for the loss window.

use crate::numeric::Exact;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OpenTradeLot {
    pub sleeve: String,
    pub symbol: String,
    pub signed_qty: Exact,
    pub exact_quantity: bool,
    pub cash: Exact,
    pub in_qty: Exact,
    pub in_value: Exact,
    pub out_qty: Exact,
    pub out_value: Exact,
    pub fees: Option<Exact>,
    pub usdt: bool,
    pub fills: u64,
    pub notional: f64,
    pub maker_notional: f64,
    pub shortfall_weight: f64,
    pub shortfall_total: f64,
    pub opened_ms: i64,
    pub priced: bool,
}
