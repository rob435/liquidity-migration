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
    /// Where the venue says the stop sits, or 0.0 when there is none. The
    /// venue is the only honest source for this: the engine's own memory of
    /// what it asked for says nothing about what the venue kept.
    #[serde(default)]
    pub stop_px: f64,
    /// The leverage the venue itself reports on this position, when the row
    /// carries one. This is the venue's own answer, not our cache — it is
    /// what lets an engine with sole leverage authority VERIFY instead of
    /// re-asking before every entry.
    #[serde(default)]
    pub leverage: Option<f64>,
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
    /// Read from historical logs, never written. The retired daily-loss guard
    /// produced this shape, so it remains decodable for WAL compatibility.
    LossGuardTripped { equity_usdt: f64, floor_usdt: f64 },
    /// The order would breach the equity-anchored envelope.
    EnvelopeBreached {
        worst_case_loss_usdt: f64,
        allowance_usdt: f64,
    },
    /// The whole book's gross notional, added up without letting one symbol's
    /// exposure cancel another's, breaches the account's second gross ceiling.
    ComponentGrossBreached { gross_usdt: f64, cap_usdt: f64 },
    /// Total margin the book commits would breach the account ceiling.
    InitialMarginBreached { margin_usdt: f64, cap_usdt: f64 },
    /// The account's spare margin cannot fund the margin this order adds. A
    /// negative reading is ordinary when the owner hand-trades, and it refuses
    /// every entry until it recovers.
    AvailableMarginExhausted {
        additional_margin_usdt: f64,
        available_usdt: f64,
    },
    /// Read from the log, never written. The per-sleeve capital partition
    /// that produced it is gone; the shape is frozen by the logs that already
    /// hold it, because a frame the reader cannot parse stops the engine at
    /// boot.
    PartitionExhausted {
        strategy: StrategyId,
        requested_usdt: f64,
        remaining_usdt: f64,
    },
    /// A position-opening intent carries no stop.
    MissingStop,
    /// The account view is too old to judge against.
    StaleAccountView { age_ns: u64, max_age_ns: u64 },
    /// The quote the decision was priced against is too old to open on — or
    /// the symbol has never quoted at all, in which case `age_ns` is the age
    /// of everything this engine has ever seen. Exits are never refused for
    /// this: taking risk off must not wait on a fresh price.
    StaleQuote { age_ns: u64, max_age_ns: u64 },
    /// Anything the kernel cannot positively classify. Fail closed.
    UnknownState { detail: String },
    /// Read from the log, never written. No kernel produces this reason, and
    /// the shape is frozen by the logs that already hold it: a frame the
    /// reader cannot parse stops the engine at boot.
    SymbolNotionalBreached {
        symbol: SymbolId,
        notional_usdt: f64,
        cap_usdt: f64,
    },
}

/// The kernel's answer. `Allow.qty` may be smaller than the intent's if a
/// control clamped size; the engine sends the clamped quantity.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum RiskVerdict {
    Allow { qty: f64 },
    Deny { reason: DenyReason },
}

/// The account-level capital controls. Unknown state refuses the order.
pub trait RiskKernel {
    fn assess(&mut self, intent: &Intent, account: &AccountView) -> RiskVerdict;
    /// Reassess the remaining quantity of an existing opening order at a new
    /// price. Implementations that track reservations override this to
    /// temporarily exclude the order's old reservation; the conservative
    /// default assesses with it still present and can therefore only
    /// over-count, never bypass, standing exposure.
    fn assess_price_amend(
        &mut self,
        _client_order_id: &str,
        intent: &Intent,
        account: &AccountView,
    ) -> RiskVerdict {
        self.assess(intent, account)
    }
    /// Keep internal exposure/fill accounting current.
    fn on_update(&mut self, update: &OrderUpdate);
    /// Latest price for a symbol, for valuing exposure. Default: ignore.
    fn observe_price(&mut self, _symbol: SymbolId, _px: f64) {}
    /// Fold every fresh account reading into account-level capital state.
    fn observe_account_view(&mut self, _account: &AccountView) {}
    /// Bind an engine-minted client order id to the intent it approved, so an
    /// order in flight is exposure the caps can see. Default: ignore.
    fn register_order(&mut self, _client_order_id: &str, _intent: &Intent, _approved_qty: f64) {}
    /// Register an order whose exact working price is temporarily ambiguous.
    /// The range is durable replay evidence: notional uses its high end while
    /// stop loss is evaluated across both ends. Kernels without range-aware
    /// accounting retain their conservative ordinary registration behavior.
    fn register_order_price_range(
        &mut self,
        client_order_id: &str,
        intent: &Intent,
        approved_qty: f64,
        _low_px: f64,
        _high_px: f64,
    ) {
        self.register_order(client_order_id, intent, approved_qty);
    }
}
