use serde::{Deserialize, Serialize};

use crate::ids::{StrategyId, SymbolId};
use crate::orders::{Intent, OrderUpdate, Side};

/// One open position as the risk kernel sees it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PositionView {
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: f64,
    pub entry_px: f64,
    pub stop_attached: bool,
}

/// Account state the risk kernel judges against. `observed_ns` is the engine
/// monotonic time of the venue read that produced it; a kernel must treat a
/// stale view as unknown state and refuse.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AccountView {
    pub equity_usdt: f64,
    pub available_usdt: f64,
    pub positions: Vec<PositionView>,
    pub observed_ns: u64,
}

/// Why an intent was refused. Closed enum so every denial is nameable in the
/// log and in tests.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum DenyReason {
    /// The account loss guard tripped (equity below its floor).
    LossGuardTripped { equity_usdt: f64, floor_usdt: f64 },
    /// The order would breach the equity-anchored envelope.
    EnvelopeBreached { worst_case_loss_usdt: f64, allowance_usdt: f64 },
    /// The strategy's capital partition cannot fund this size.
    PartitionExhausted { strategy: StrategyId, requested_usdt: f64, remaining_usdt: f64 },
    /// A position-opening intent carries no stop.
    MissingStop,
    /// The account view is too old to judge against.
    StaleAccountView { age_ns: u64, max_age_ns: u64 },
    /// Anything the kernel cannot positively classify. Fail closed.
    UnknownState { detail: String },
}

/// The kernel's answer. `Allow.qty` may be smaller than the intent's if a
/// control clamped size; the engine sends the clamped quantity.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum RiskVerdict {
    Allow { qty: f64 },
    Deny { reason: DenyReason },
}

/// The four capital controls, ported from the Python fleet. Decision
/// semantics must match `account_loss_guard.py`, `equity_anchored_envelope.py`,
/// the partition in `account_kernel.py`, and the stop discipline of
/// `venue_protection.py`. Unknown state refuses the order.
pub trait RiskKernel {
    fn assess(&mut self, intent: &Intent, account: &AccountView) -> RiskVerdict;
    /// Keep internal exposure/fill accounting current.
    fn on_update(&mut self, update: &OrderUpdate);
    /// Latest price for a symbol, for valuing exposure. Default: ignore.
    fn observe_price(&mut self, _symbol: SymbolId, _px: f64) {}
    /// Wall time of the account reading being folded in — the loss guard's
    /// daily anchor rolls on the READING's UTC day, not on "now".
    /// Default: ignore.
    fn observe_wall_clock_ns(&mut self, _wall_ns: u64) {}
    /// Bind an engine-minted client order id to the intent it approved, so
    /// later fills can be attributed per strategy. Default: ignore.
    fn register_order(&mut self, _client_order_id: &str, _intent: &Intent, _approved_qty: f64) {}
}
