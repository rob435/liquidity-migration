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
    /// The order would breach the equity-anchored envelope.
    EnvelopeBreached { worst_case_loss_usdt: f64, allowance_usdt: f64 },
    /// One symbol would carry more gross notional than the account allows on
    /// any single symbol.
    SymbolNotionalBreached { symbol: SymbolId, notional_usdt: f64, cap_usdt: f64 },
    /// The whole book's gross notional, added up without letting one symbol's
    /// exposure cancel another's, breaches the account's second gross ceiling.
    ComponentGrossBreached { gross_usdt: f64, cap_usdt: f64 },
    /// Total margin the book commits would breach the account ceiling.
    InitialMarginBreached { margin_usdt: f64, cap_usdt: f64 },
    /// The account's spare margin cannot fund the margin this order adds. A
    /// negative reading is ordinary when the owner hand-trades, and it refuses
    /// every entry until it recovers.
    AvailableMarginExhausted { additional_margin_usdt: f64, available_usdt: f64 },
    /// The strategy's capital partition cannot fund this size.
    PartitionExhausted { strategy: StrategyId, requested_usdt: f64, remaining_usdt: f64 },
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
}

/// The kernel's answer. `Allow.qty` may be smaller than the intent's if a
/// control clamped size; the engine sends the clamped quantity.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum RiskVerdict {
    Allow { qty: f64 },
    Deny { reason: DenyReason },
}

/// The three capital controls: the equity-anchored envelope, the per-strategy
/// capital partition, and the stop discipline. Unknown state refuses the
/// order. What was ported, and where these rules deliberately differ from the
/// fleet they came from, is recorded in `engine-risk/PORT_NOTES.md` and pinned
/// by the tests beside it.
pub trait RiskKernel {
    fn assess(&mut self, intent: &Intent, account: &AccountView) -> RiskVerdict;
    /// Keep internal exposure/fill accounting current.
    fn on_update(&mut self, update: &OrderUpdate);
    /// Latest price for a symbol, for valuing exposure. Default: ignore.
    fn observe_price(&mut self, _symbol: SymbolId, _px: f64) {}
    /// Wall time of the account reading being folded in — the READING's
    /// clock, not "now". No shipped kernel reads it back today.
    /// Default: ignore.
    fn observe_wall_clock_ns(&mut self, _wall_ns: u64) {}
    /// Bind an engine-minted client order id to the intent it approved, so
    /// later fills can be attributed per strategy. Default: ignore.
    fn register_order(&mut self, _client_order_id: &str, _intent: &Intent, _approved_qty: f64) {}
    /// Control state that must outlive the process. `Some` exactly when it
    /// changed since last taken; the engine writes it to the log and makes it
    /// durable. No shipped kernel overrides this default — the state that
    /// used it was the daily anchor and trip of a halt since removed, and
    /// existing logs still carry those records. Default: none.
    fn take_control_anchor(&mut self) -> Option<String> {
        None
    }
    /// Restore from the newest anchor found in the log, before the first
    /// evaluation. Default: ignore.
    fn restore_control_anchor(&mut self, _state: &str) {}
}
