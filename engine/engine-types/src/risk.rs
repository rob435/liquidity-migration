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
    /// Native stop decimal retained before its compatibility projection.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact_stop_px: Option<Box<crate::numeric::Exact>>,
    /// The leverage the venue itself reports on this position, when the row
    /// carries one. This is the venue's own answer, not our cache — it is
    /// what lets an engine with sole leverage authority VERIFY instead of
    /// re-asking before every entry.
    #[serde(default)]
    pub leverage: Option<f64>,
}

impl PositionView {
    pub fn validate_stop_projection(&self) -> Result<(), crate::numeric::ExactError> {
        if let Some(exact) = &self.exact_stop_px {
            if !self.stop_attached || !exact.is_positive() || exact.to_f64()? != self.stop_px {
                return Err(crate::numeric::ExactError::InvalidProjection);
            }
        }
        Ok(())
    }
}

/// Account state the risk kernel judges against. `observed_ns` is the engine
/// monotonic time of the venue read that produced it; a kernel must treat a
/// stale view as unknown state and refuse.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AccountView {
    pub equity_usdt: f64,
    pub available_usdt: f64,
    pub positions: Vec<PositionView>,
    /// Monotonic start of the earliest request composing this view; zero is stale.
    pub observed_ns: u64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PhysicalExposureInterval {
    low: f64,
    high: f64,
}

impl PhysicalExposureInterval {
    pub fn try_new(low: f64, high: f64) -> Result<Self, DenyReason> {
        if !low.is_finite() || !high.is_finite() || low > high {
            return Err(DenyReason::UnknownState {
                detail: "physical exposure interval is unreadable".into(),
            });
        }
        Ok(Self { low, high })
    }
    pub fn low(self) -> f64 {
        self.low
    }
    pub fn high(self) -> f64 {
        self.high
    }
    pub fn after(self, side: crate::orders::Side, qty: f64) -> Result<Self, DenyReason> {
        if !qty.is_finite() || qty <= 0.0 {
            return Err(DenyReason::UnknownState {
                detail: "physical interval delta is unreadable".into(),
            });
        }
        let delta = if side == crate::orders::Side::Buy {
            qty
        } else {
            -qty
        };
        Self::try_new(self.low + delta, self.high + delta)
    }
    pub fn certainly_reduces(self, side: crate::orders::Side, qty: f64) -> bool {
        qty.is_finite()
            && qty > 0.0
            && match side {
                crate::orders::Side::Sell => self.low >= qty,
                crate::orders::Side::Buy => self.high <= -qty,
            }
    }
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
    /// This engine's own closed round trips, net of venue fees, lost more than
    /// the limit inside the rolling window. Entries and growth wait for the
    /// losing trades to age out; genuine exits still pass.
    RollingLossTripped {
        window_net_usdt: f64,
        limit_usdt: f64,
        window_ms: i64,
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

/// One closed round trip, as the rolling loss window keeps it. `closed_ms` is
/// the venue's wall clock at the closing fill; `net_usdt` is gross minus the
/// venue's fees, with no funding in it.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct ClosedTradeRow {
    pub closed_ms: i64,
    pub net_usdt: f64,
}

/// What the rolling loss window holds right now, for the log and the operator.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct RollingLossView {
    pub window_ms: i64,
    pub trades: usize,
    pub net_usdt: f64,
    pub limit_usdt: f64,
    pub tripped: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum PortfolioRiskVerdict {
    Allow { qty: f64, venue_reduce_only: bool },
    Deny { reason: DenyReason },
}

/// The account-level capital controls. Unknown state refuses the order.
pub trait RiskKernel {
    fn assess(&mut self, intent: &Intent, account: &AccountView) -> RiskVerdict;
    fn assess_portfolio(
        &mut self,
        _intent: &Intent,
        _account: &AccountView,
        _portfolio: &crate::portfolio::PortfolioState,
    ) -> PortfolioRiskVerdict {
        PortfolioRiskVerdict::Deny {
            reason: DenyReason::UnknownState {
                detail: "risk kernel does not support virtual portfolio ownership".into(),
            },
        }
    }

    fn reassess_portfolio_order(
        &mut self,
        _id: &str,
        _intent: &Intent,
        _account: &AccountView,
        _portfolio: &crate::portfolio::PortfolioState,
    ) -> PortfolioRiskVerdict {
        PortfolioRiskVerdict::Deny {
            reason: DenyReason::UnknownState {
                detail: "kernel cannot re-evaluate an owned portfolio order".into(),
            },
        }
    }
    fn physical_exposure_interval_excluding(
        &mut self,
        _id: &str,
        _symbol: SymbolId,
        _account: &AccountView,
    ) -> Result<PhysicalExposureInterval, DenyReason> {
        Err(DenyReason::UnknownState {
            detail: "kernel cannot exclude an owned pending order".into(),
        })
    }
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
    fn on_update_with_remaining(
        &mut self,
        update: &OrderUpdate,
        _remaining_qty: f64,
    ) -> Result<(), DenyReason> {
        self.on_update(update);
        Ok(())
    }

    /// Latest price for a symbol, for valuing exposure. Default: ignore.
    fn observe_price(&mut self, _symbol: SymbolId, _px: f64) {}
    /// Fold every fresh account reading into account-level capital state.
    fn observe_account_view(&mut self, _account: &AccountView) {}
    /// Hand the kernel one closed round trip of its own, so the rolling loss
    /// window can count it. Default: ignore.
    fn observe_closed_trade(&mut self, _row: ClosedTradeRow) {}
    /// Tell the kernel what the venue's wall clock reads, so the rolling loss
    /// window ages out old trades even while nothing closes. Default: ignore.
    fn observe_wall_clock_ms(&mut self, _wall_ms: i64) {}
    /// What the rolling loss window holds, when the kernel keeps one.
    fn rolling_loss(&self) -> Option<RollingLossView> {
        None
    }
    /// The closed trades still inside the window, for the caller to persist.
    fn rolling_loss_rows(&self) -> Vec<ClosedTradeRow> {
        Vec::new()
    }
    /// Set the window's trades to these, which is how a restart reads its own
    /// closed trades back. Default: ignore.
    fn restore_rolling_loss_rows(&mut self, _rows: &[ClosedTradeRow]) {}
    /// Bind an engine-minted client order id to the intent it approved, so an
    /// order in flight is exposure the caps can see. Default: ignore.
    fn register_order(&mut self, _client_order_id: &str, _intent: &Intent, _approved_qty: f64) {}
    fn physical_exposure_interval(
        &mut self,
        _symbol: SymbolId,
        _account: &AccountView,
    ) -> Result<PhysicalExposureInterval, DenyReason> {
        Err(DenyReason::UnknownState {
            detail: "kernel cannot establish physical exposure interval".into(),
        })
    }
    /// Reserve cash margin against the causal account query used for admission.
    fn register_order_with_account(
        &mut self,
        client_order_id: &str,
        intent: &Intent,
        approved_qty: f64,
        _account: &AccountView,
    ) {
        self.register_order(client_order_id, intent, approved_qty);
    }
    fn register_order_price_range_with_account(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: f64,
        price_range: (f64, f64),
        _account: &AccountView,
    ) {
        self.register_order_price_range(id, intent, qty, price_range.0, price_range.1);
    }
    /// Canonical order ledger completion; does not synthesize or erase executions.
    fn complete_order(&mut self, _client_order_id: &str, _confirmed_ns: u64) {}
    fn mark_order_attempted(&mut self, _client_order_id: &str) {}
    /// Confirmation is a receipt in this process's monotonic epoch, never a replayed stamp.
    fn mark_order_accepted(&mut self, _client_order_id: &str, _confirmed_ns: u64) {}
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
