//! One capital owner; canonical quantities and money remain exact until reporting.
use crate::config::{ConfigError, KernelConfig};
use crate::envelope::Envelope;
use crate::exposure::{Book, Pending};
use crate::loss_window::LossWindow;
use crate::margin::MarginBook;
use crate::ROLLING_LOSS_WINDOW_MS;
use engine_types::ids::SymbolId;
use engine_types::numeric::Exact;
use engine_types::orders::{Intent, OrderKind, OrderUpdate, Side};
use engine_types::risk::{
    AccountView, ClosedTradeRow, DenyReason, RiskKernel, RiskVerdict, RollingLossView,
};
mod portfolio;
use portfolio::PortfolioFacts;

pub struct Kernel {
    cfg: KernelConfig,
    envelope: Envelope,
    book: Book,
    loss_window: LossWindow,
    open_pnl_usdt: Option<Exact>,
    margin: MarginBook,
    latest_account_observed_ns: u64,
}
fn unknown(detail: impl Into<String>) -> DenyReason {
    DenyReason::UnknownState {
        detail: detail.into(),
    }
}
fn exact(value: f64) -> Result<Exact, DenyReason> {
    Exact::from_legacy_f64(value).map_err(|e| unknown(e.to_string()))
}
pub(crate) fn policy(value: f64) -> Exact {
    Exact::parse_decimal(&value.to_string()).expect("validated finite risk configuration")
}
fn report(value: &Exact) -> f64 {
    value.reporting_f64()
}

fn signed(side: Side, qty: &Exact) -> Exact {
    if side == Side::Buy {
        qty.clone()
    } else {
        -qty
    }
}
fn max_price<T: Ord>(a: Option<T>, b: Option<T>) -> Option<T> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.max(b)),
        (a, b) => a.or(b),
    }
}
fn valid_price(value: f64) -> Option<Exact> {
    exact(value).ok().filter(Exact::is_positive)
}
fn approved_quantity(intent: &Intent, qty: f64) -> Option<Exact> {
    if !qty.is_finite() || qty <= 0.0 {
        return None;
    }
    if qty == intent.qty {
        intent.quantity().ok()
    } else {
        Exact::parse_decimal(&qty.to_string()).ok()
    }
}
impl Kernel {
    pub fn new(cfg: KernelConfig) -> Result<Self, ConfigError> {
        cfg.validate()?;
        let envelope = Envelope::new(cfg.envelope.clone());
        Ok(Self {
            cfg,
            envelope,
            book: Book::default(),
            loss_window: LossWindow::default(),
            open_pnl_usdt: Some(Exact::zero()),
            margin: MarginBook::default(),
            latest_account_observed_ns: 0,
        })
    }
    pub fn observe_price(&mut self, symbol: SymbolId, px: f64) {
        self.book.observe_px(symbol, px);
    }
    fn observe_reference(&mut self, view: &ViewFacts, allow_expansion: bool) {
        let ordered = view.observed_ns >= self.latest_account_observed_ns;
        self.envelope
            .observe_equity_with_permission(&view.equity_usdt, allow_expansion && ordered);
        self.latest_account_observed_ns = self.latest_account_observed_ns.max(view.observed_ns);
    }

    fn require_viable_reference(&self) -> Result<(), DenyReason> {
        if self.envelope.viable_for_new_exposure() {
            Ok(())
        } else {
            Err(unknown("verified equity is below the minimum viable capital reference; new physical exposure is refused"))
        }
    }
    pub fn register_order(&mut self, id: &str, intent: &Intent, qty: f64) {
        let px = match intent.kind {
            OrderKind::Limit { px, .. } => px,
            _ => 0.0,
        };
        self.register_order_price_range(id, intent, qty, px, px);
    }
    pub fn register_order_price_range(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: f64,
        low: f64,
        high: f64,
    ) {
        self.register_prices(
            id,
            intent,
            approved_quantity(intent, qty),
            Self::reservation_price(intent, low),
            Self::reservation_price(intent, high),
        );
    }
    fn reservation_price(intent: &Intent, price: f64) -> Option<Exact> {
        if matches!(intent.kind, OrderKind::Limit { px, .. } if px == price) {
            intent.limit_price().ok().flatten()
        } else {
            valid_price(price)
        }
    }
    fn register_prices(
        &mut self,
        id: &str,
        intent: &Intent,
        quantity: Option<Exact>,
        first: Option<Exact>,
        second: Option<Exact>,
    ) {
        let observed = self.book.px(intent.symbol).cloned();
        let high = max_price(max_price(first.clone(), second.clone()), observed.clone());
        let low = first.into_iter().chain(second).chain(observed).min();
        let fraction = if intent.reduce_only {
            Some(Exact::zero())
        } else {
            low.as_ref()
                .zip(high.as_ref())
                .and_then(|(a, b)| read_stop(intent, a, b).ok())
        };
        let quantity = quantity.filter(|_| intent.validate_price_projection().is_ok());
        self.margin
            .register(id, intent.symbol, quantity.clone(), high.clone());
        self.book.register(
            id,
            Pending {
                strategy: intent.strategy,
                symbol: intent.symbol,
                signed_qty: quantity.map(|q| signed(intent.side, &q)),
                reduce_only: intent.reduce_only,
                px: high,
                stop_fraction: fraction,
            },
        );
    }
    pub fn register_order_with_account(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: f64,
        account: &AccountView,
    ) {
        let px = match intent.kind {
            OrderKind::Limit { px, .. } => px,
            _ => 0.0,
        };
        self.register_order_price_range_with_account(id, intent, qty, (px, px), account);
    }
    pub fn register_order_price_range_with_account(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: f64,
        price_range: (f64, f64),
        account: &AccountView,
    ) {
        self.register_owned_prices(
            id,
            intent,
            approved_quantity(intent, qty),
            (
                Self::reservation_price(intent, price_range.0),
                Self::reservation_price(intent, price_range.1),
            ),
            account,
        );
    }
    pub fn register_order_exact_price_range_with_account(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: &Exact,
        price_range: (&Exact, &Exact),
        account: &AccountView,
    ) {
        let quantity = intent.quantity().ok().filter(|value| value == qty);
        self.register_owned_prices(
            id,
            intent,
            quantity,
            (
                Some(price_range.0.clone()).filter(Exact::is_positive),
                Some(price_range.1.clone()).filter(Exact::is_positive),
            ),
            account,
        );
    }
    fn register_owned_prices(
        &mut self,
        id: &str,
        intent: &Intent,
        quantity: Option<Exact>,
        price_range: (Option<Exact>, Option<Exact>),
        account: &AccountView,
    ) {
        self.book.forget(id);
        let margin_quantity = if intent.reduce_only {
            ViewFacts::read(account, &Exact::zero())
                .ok()
                .and_then(|view| {
                    let recent = self.book.fills_after(account.observed_ns).ok()?;
                    let physical = view.net_qty(intent.symbol)
                        + recent
                            .get(&intent.symbol.0)
                            .map_or_else(Exact::zero, |r| r.signed_qty.clone());
                    self.incremental_physical_quantity(intent, quantity.as_ref()?, &physical)
                        .ok()
                })
        } else {
            quantity.clone()
        };
        self.register_prices(id, intent, quantity, price_range.0, price_range.1);
        self.margin.set_quantity(id, margin_quantity);
    }
    pub fn complete_order(&mut self, id: &str, ns: u64) {
        self.book.forget(id);
        self.margin.retire(id, ns);
    }
    pub fn mark_order_attempted(&mut self, id: &str) {
        self.margin.attempted(id);
    }
    pub fn mark_order_accepted(&mut self, id: &str, ns: u64) {
        self.margin.accepted(id, ns);
    }
    pub fn capital_reference_usdt(&self) -> f64 {
        report(self.envelope.reference_usdt())
    }
    pub fn observe_closed_trade(&mut self, row: ClosedTradeRow) {
        self.loss_window.observe(row);
    }
    pub fn observe_wall_clock_ms(&mut self, ms: i64) {
        self.loss_window.observe_clock(ms);
    }
    pub fn rolling_loss(&self) -> RollingLossView {
        let limit = self.rolling_loss_limit();
        let net = self.risk_net_usdt();
        RollingLossView {
            window_ms: ROLLING_LOSS_WINDOW_MS,
            trades: self.loss_window.trades(),
            net_usdt: net.as_ref().map_or(0.0, report),
            limit_usdt: report(&limit),
            tripped: !self.loss_window.valid()
                || net.is_none()
                || net.is_some_and(|net| net <= -limit),
        }
    }
    pub fn rolling_loss_rows(&self) -> Vec<ClosedTradeRow> {
        self.loss_window.rows()
    }
    pub fn restore_rolling_loss_rows(&mut self, rows: &[ClosedTradeRow]) {
        self.loss_window.restore(rows);
    }
    fn risk_net_usdt(&self) -> Option<Exact> {
        Some(
            self.loss_window.net_usdt().cloned().unwrap_or_default()
                + self.open_pnl_usdt.as_ref()?.clone().min(Exact::zero()),
        )
    }
    fn rolling_loss_limit(&self) -> Exact {
        policy(self.cfg.max_rolling_loss_fraction) * self.envelope.reference_usdt()
    }
    fn price_for<'a>(&'a self, symbol: SymbolId, view: &'a ViewFacts) -> Option<&'a Exact> {
        max_price(self.book.px(symbol), view.entry_px(symbol))
    }
    fn held_stop_fraction(&self, symbol: SymbolId, view: &ViewFacts) -> Result<Exact, DenyReason> {
        let mut worst = None;
        for (_, side, entry, stop) in view
            .stops
            .iter()
            .filter(|(held, _, _, _)| *held == symbol.0)
        {
            let current = self.book.px(symbol).unwrap_or(entry);
            let low = current.min(entry);
            let high = current.max(entry);
            let distance = match side {
                Side::Buy if stop < current => high - stop,
                Side::Sell if stop > current => stop - low,
                _ => {
                    return Err(unknown(
                        "held position stop is not on the protective side of plausible prices",
                    ))
                }
            };
            let fraction = distance
                .checked_div(high)
                .map_err(|e| unknown(e.to_string()))?;
            worst = Some(
                worst.map_or_else(|| fraction.clone(), |old: Exact| old.max(fraction.clone())),
            );
        }
        worst.ok_or_else(|| unknown("held position has no readable stop level"))
    }
    fn evaluate(
        &mut self,
        intent: &Intent,
        account: &AccountView,
        now_ns: u64,
    ) -> Result<Exact, DenyReason> {
        self.evaluate_inventory(intent, account, None, now_ns)
    }
    fn evaluate_inventory(
        &mut self,
        intent: &Intent,
        account: &AccountView,
        portfolio: Option<&PortfolioFacts>,
        now_ns: u64,
    ) -> Result<Exact, DenyReason> {
        if account.observed_ns > now_ns {
            return Err(unknown("account view is newer than its admission time"));
        }
        let age_ns = now_ns - account.observed_ns;
        let tolerance = if portfolio.is_some() {
            Exact::zero()
        } else {
            policy(self.cfg.qty_tolerance)
        };
        let view = ViewFacts::read(account, &tolerance)?;
        // Reductions also contract budgets, but do not need an expanding budget.
        self.observe_reference(&view, false);
        if intent.exact_prices.is_some() {
            intent
                .validate_price_projection()
                .map_err(|e| unknown(e.to_string()))?;
        }
        let ask = intent.quantity().map_err(|e| unknown(e.to_string()))?;
        let recent = self
            .book
            .fills_after(account.observed_ns)
            .map_err(unknown)?;
        let physical = view.net_qty(intent.symbol)
            + recent
                .get(&intent.symbol.0)
                .map_or_else(Exact::zero, |r| r.signed_qty.clone());
        let settled = portfolio.map_or_else(
            || physical.clone(),
            |p| p.owned(intent.strategy, intent.symbol),
        );
        let delta = signed(intent.side, &ask);
        let reduces = settled.abs() > tolerance
            && !delta.is_zero()
            && delta.is_negative() != settled.is_negative();
        if intent.reduce_only {
            if !reduces {
                return Err(unknown(
                    "reduce_only intent does not reduce the position it names",
                ));
            }
            if let OrderKind::Limit { px, .. } = intent.kind {
                if valid_price(px).is_none() {
                    return Err(unknown("exit limit price is not a positive number"));
                }
            }
            let covered = if portfolio.is_some() {
                self.book.owned_reduce_qty(intent.strategy, intent.symbol)
            } else {
                self.book.pending_reduce_qty(intent.symbol)
            }
            .map_err(unknown)?;
            let open = settled.abs() - covered;
            if open <= tolerance {
                return Err(unknown(
                    "the position is already fully covered by resting exits",
                ));
            }
            let qty = ask.min(open);
            if let Some(portfolio) = portfolio {
                self.check_virtual_reduction(intent, &qty, &physical, age_ns, &view, portfolio)?;
            }
            return Ok(qty);
        }
        if age_ns > self.cfg.max_account_view_age_ns {
            return Err(DenyReason::StaleAccountView {
                age_ns,
                max_age_ns: self.cfg.max_account_view_age_ns,
            });
        }
        if account.observed_ns < self.latest_account_observed_ns {
            return Err(unknown(
                "account view predates the latest observed account state",
            ));
        }
        self.observe_reference(&view, true);
        self.require_viable_reference()?;
        if !self.loss_window.valid() {
            return Err(unknown(
                "closed-trade account-unit valuation is unavailable or invalid",
            ));
        }
        self.open_pnl_usdt = Some(account_open_pnl(account)?);
        let limit = self.rolling_loss_limit();
        if let Some(net) = self.risk_net_usdt().filter(|net| net <= &-&limit) {
            return Err(DenyReason::RollingLossTripped {
                window_net_usdt: report(&net),
                limit_usdt: report(&limit),
                window_ms: ROLLING_LOSS_WINDOW_MS,
            });
        }
        if reduces {
            let opposite = if portfolio.is_some() {
                self.book
                    .owned_open_qty(intent.strategy, intent.symbol, intent.side)
            } else {
                self.book.pending_open_qty(intent.symbol, intent.side)
            }
            .map_err(unknown)?;
            if opposite + &ask > settled.abs() + tolerance {
                return Err(unknown("intent crosses through flat to the other side"));
            }
        }
        if view.unprotected || intent.stop.is_none() {
            return Err(DenyReason::MissingStop);
        }
        let (low, px) = self.entry_prices(intent, &view)?;
        let fraction = read_stop(intent, &low, &px)?;
        let notional = &ask * &px;
        let projected = if let Some(portfolio) = portfolio {
            self.projected_portfolio(&notional, &fraction, account, &view, portfolio)?
        } else {
            self.projected_book(&notional, &fraction, account, &view)?
        };
        let allowance = self.envelope.allowance_usdt();
        if projected.modelled_stop_charge_usdt > allowance {
            return Err(DenyReason::EnvelopeBreached {
                modelled_stop_charge_usdt: report(&projected.modelled_stop_charge_usdt),
                allowance_usdt: report(&allowance),
            });
        }
        self.account_caps(&notional, &projected, &view)?;
        let held_qty = portfolio.map_or_else(
            || physical.abs(),
            |p| p.symbol_gross_quantity(intent.symbol, &physical),
        );
        let held_price = self
            .price_for(intent.symbol, &view)
            .map_or_else(|| px.clone(), |p| p.max(&px).clone());
        let symbol_notional = &notional
            + held_qty * &held_price
            + self
                .book
                .pending_symbol_notional(intent.symbol, &held_price)
                .map_err(unknown)?;
        let symbol_cap = policy(self.cfg.envelope.max_symbol_notional_usdt) * self.envelope.scale();
        if symbol_notional > symbol_cap {
            return Err(DenyReason::SymbolNotionalBreached {
                symbol: intent.symbol,
                notional_usdt: report(&symbol_notional),
                cap_usdt: report(&symbol_cap),
            });
        }
        if &fraction * policy(self.cfg.leverage) >= Exact::one() {
            return Err(unknown("stop distance must be below 1 / leverage"));
        }
        self.check_venue_margin_and_liquidation(account)?;
        Ok(ask)
    }
    fn check_venue_margin_and_liquidation(&self, account: &AccountView) -> Result<(), DenyReason> {
        if let Some(amounts) = &account.exact_amounts {
            let cap = policy(self.cfg.envelope.max_initial_margin_usdt)
                .checked_div(&policy(self.cfg.envelope.reference_usdt))
                .map_err(|e| unknown(e.to_string()))?;
            if optional_number(amounts.initial_margin_rate.as_ref())?
                .is_some_and(|rate| rate >= cap)
                || optional_number(amounts.maintenance_margin_rate.as_ref())?
                    .is_some_and(|rate| rate >= Exact::one())
            {
                return Err(unknown("venue margin ratio leaves no capacity for growth"));
            }
        }
        for position in &account.positions {
            if !position
                .quantity()
                .map_err(|e| unknown(e.to_string()))?
                .is_positive()
            {
                continue;
            }
            let Some(amounts) = &position.exact_amounts else {
                continue;
            };
            if let Some(liquidation) = optional_number(amounts.liquidation_price.as_ref())? {
                let stop = position.stop_price().map_err(|e| unknown(e.to_string()))?;
                if !position.stop_attached
                    || match position.side {
                        Side::Buy => stop <= liquidation,
                        Side::Sell => stop >= liquidation,
                    }
                {
                    return Err(unknown("held stop is beyond the venue liquidation price"));
                }
            }
        }
        Ok(())
    }
    fn projected_book(
        &mut self,
        notional: &Exact,
        fraction: &Exact,
        account: &AccountView,
        view: &ViewFacts,
    ) -> Result<Projected, DenyReason> {
        let mut recent = self
            .book
            .fills_after(account.observed_ns)
            .map_err(unknown)?;
        let mut projected = Projected {
            gross_usdt: notional.clone(),
            modelled_stop_charge_usdt: self.envelope.modelled_stop_charge_usdt(notional, fraction),
        };
        for (symbol, qty) in view.exposures() {
            let fill = recent.remove(&symbol.0);
            let effective = qty
                + fill
                    .as_ref()
                    .map_or_else(Exact::zero, |r| r.signed_qty.clone());
            if effective.abs() <= policy(self.cfg.qty_tolerance) {
                continue;
            }
            let recent_stop = match fill {
                Some(row) => row.stop_fraction.ok_or_else(|| {
                    unknown("a fill newer than the account view has no readable stop distance")
                })?,
                None => Exact::zero(),
            };
            let price = self
                .price_for(symbol, view)
                .ok_or_else(|| unknown("no price for a held symbol"))?;
            let notional = effective.abs() * price;
            let stop = self.held_stop_fraction(symbol, view)?.max(recent_stop);
            projected.add(&notional);
            projected.modelled_stop_charge_usdt +=
                self.envelope.modelled_stop_charge_usdt(&notional, &stop);
        }
        for (symbol, row) in recent {
            if row.signed_qty.abs() <= policy(self.cfg.qty_tolerance) {
                continue;
            }
            let fraction = row.stop_fraction.ok_or_else(|| {
                unknown("a fill newer than the account view has no readable stop distance")
            })?;
            let price = self
                .price_for(SymbolId(symbol), view)
                .ok_or_else(|| unknown("no price for a just-filled symbol"))?;
            let notional = row.signed_qty.abs() * price;
            projected.add(&notional);
            projected.modelled_stop_charge_usdt += self
                .envelope
                .modelled_stop_charge_usdt(&notional, &fraction);
        }
        self.add_pending(&mut projected, view)?;
        Ok(projected)
    }
    fn add_pending(&self, projected: &mut Projected, view: &ViewFacts) -> Result<(), DenyReason> {
        let rows = self
            .book
            .pending_risk_rows(|symbol| self.price_for(symbol, view))
            .map_err(unknown)?;
        let (gross, loss) = self.envelope.pending_totals(&rows);
        projected.gross_usdt += gross;
        projected.modelled_stop_charge_usdt += loss;
        Ok(())
    }
    fn account_caps(
        &self,
        notional: &Exact,
        projected: &Projected,
        view: &ViewFacts,
    ) -> Result<(), DenyReason> {
        let caps = &self.cfg.envelope;
        let scale = self.envelope.scale();
        let cap = policy(caps.max_component_gross_notional_usdt) * &scale;
        if projected.gross_usdt > cap {
            return Err(DenyReason::ComponentGrossBreached {
                gross_usdt: report(&projected.gross_usdt),
                cap_usdt: report(&cap),
            });
        }
        let leverage = policy(self.cfg.leverage);
        let margin = projected
            .gross_usdt
            .checked_div(&leverage)
            .expect("positive leverage");
        let cap = policy(caps.max_initial_margin_usdt) * scale;
        if margin > cap {
            return Err(DenyReason::InitialMarginBreached {
                margin_usdt: report(&margin),
                cap_usdt: report(&cap),
            });
        }
        let additional = notional.checked_div(&leverage).expect("positive leverage")
            + self.unreflected_margin(view)?;
        self.check_available(&additional, view)
    }
    fn check_available(&self, additional: &Exact, view: &ViewFacts) -> Result<(), DenyReason> {
        if additional > &view.available_usdt {
            Err(DenyReason::AvailableMarginExhausted {
                additional_margin_usdt: report(additional),
                available_usdt: report(&view.available_usdt),
            })
        } else {
            Ok(())
        }
    }
    fn unreflected_margin(&self, view: &ViewFacts) -> Result<Exact, DenyReason> {
        self.margin
            .required(view.observed_ns, &policy(self.cfg.leverage), |symbol| {
                self.price_for(symbol, view)
            })
            .map_err(unknown)
    }
    fn entry_prices(
        &self,
        intent: &Intent,
        view: &ViewFacts,
    ) -> Result<(Exact, Exact), DenyReason> {
        let quoted = intent.limit_price().map_err(|_| {
            unknown("limit price is not a positive number or disagrees with its canonical value")
        })?;
        match (quoted, self.price_for(intent.symbol, view)) {
            (Some(a), Some(b)) if &a <= b => Ok((a, b.clone())),
            (Some(a), Some(b)) => Ok((b.clone(), a)),
            (Some(a), None) => Ok((a.clone(), a)),
            (None, Some(a)) => Ok((a.clone(), a.clone())),
            _ => Err(unknown("no price to value this symbol")),
        }
    }
}
impl RiskKernel for Kernel {
    fn stop_distance_cap(&self, observed_leverage: Option<f64>) -> Option<f64> {
        let leverage = observed_leverage
            .filter(|l| l.is_finite() && *l > 0.0)
            .unwrap_or(self.cfg.leverage)
            .max(self.cfg.leverage);
        Some(self.cfg.envelope.disaster_stop_fraction.min(0.5 / leverage))
    }

    fn assess(&mut self, intent: &Intent, account: &AccountView, now_ns: u64) -> RiskVerdict {
        match self
            .evaluate(intent, account, now_ns)
            .and_then(|qty| qty.to_f64().map_err(|e| unknown(e.to_string())))
        {
            Ok(qty) => RiskVerdict::Allow { qty },
            Err(reason) => RiskVerdict::Deny { reason },
        }
    }
    fn assess_portfolio(
        &mut self,
        intent: &Intent,
        account: &AccountView,
        portfolio: &engine_types::portfolio::PortfolioState,
        now_ns: u64,
    ) -> engine_types::risk::PortfolioRiskVerdict {
        use engine_types::risk::PortfolioRiskVerdict;
        let result = PortfolioFacts::read(portfolio)
            .and_then(|p| self.evaluate_inventory(intent, account, Some(&p), now_ns))
            .and_then(|qty| {
                let interval = self.physical_interval_for(intent.symbol, account)?;
                let reduce = intent.reduce_only && interval.certainly_reduces(intent.side, &qty);
                Ok((qty, reduce))
            });
        match result {
            Ok((qty, venue_reduce_only)) => PortfolioRiskVerdict::Allow {
                qty,
                venue_reduce_only,
            },
            Err(reason) => PortfolioRiskVerdict::Deny { reason },
        }
    }
    fn reassess_portfolio_order(
        &mut self,
        id: &str,
        intent: &Intent,
        account: &AccountView,
        portfolio: &engine_types::portfolio::PortfolioState,
        now_ns: u64,
    ) -> engine_types::risk::PortfolioRiskVerdict {
        use engine_types::risk::PortfolioRiskVerdict;
        let Some(previous) = self.book.take(id) else {
            return PortfolioRiskVerdict::Deny {
                reason: unknown("portfolio reassessment has no matching reservation"),
            };
        };
        let margin = self.margin.take(id);
        let verdict = if previous.symbol == intent.symbol
            && previous.strategy == intent.strategy
            && previous
                .signed_qty
                .as_ref()
                .is_some_and(|q| q.is_negative() == (intent.side == Side::Sell))
        {
            self.assess_portfolio(intent, account, portfolio, now_ns)
        } else {
            PortfolioRiskVerdict::Deny {
                reason: unknown("portfolio reassessment names another order owner or direction"),
            }
        };
        self.book.register(id, previous);
        self.margin.restore(id, margin);
        verdict
    }
    fn physical_exposure_interval_excluding(
        &mut self,
        id: &str,
        symbol: SymbolId,
        account: &AccountView,
    ) -> Result<engine_types::risk::PhysicalExposureInterval, DenyReason> {
        let previous = self
            .book
            .take(id)
            .ok_or_else(|| unknown("physical interval exclusion has no matching reservation"))?;
        let result = if previous.symbol == symbol {
            self.physical_interval_for(symbol, account)
        } else {
            Err(unknown("physical interval exclusion names another symbol"))
        };
        self.book.register(id, previous);
        result
    }
    fn assess_price_amend(
        &mut self,
        id: &str,
        intent: &Intent,
        account: &AccountView,
        now_ns: u64,
    ) -> RiskVerdict {
        let Some(previous) = self.book.take(id) else {
            return RiskVerdict::Deny {
                reason: unknown("opening amend has no matching risk reservation"),
            };
        };
        let margin = self.margin.take(id);
        let verdict = self.assess(intent, account, now_ns);
        self.book.register(id, previous);
        self.margin.restore(id, margin);
        verdict
    }
    fn on_update_with_remaining(
        &mut self,
        update: &OrderUpdate,
        remaining: f64,
    ) -> Result<(), DenyReason> {
        let remaining = exact(remaining)?;
        self.on_update_with_exact_remaining(update, &remaining)
    }
    fn on_update_with_exact_remaining(
        &mut self,
        update: &OrderUpdate,
        remaining: &Exact,
    ) -> Result<(), DenyReason> {
        if remaining.is_negative() {
            return Err(unknown("canonical remaining quantity is invalid"));
        }
        let OrderUpdate::Fill {
            client_order_id,
            symbol,
            side,
            qty,
            px,
            amounts,
            recv_ns,
            ..
        } = update
        else {
            self.on_update(update);
            return Ok(());
        };
        if remaining.is_positive() && !self.book.contains(client_order_id) {
            return Err(unknown("canonical partial fill has no pending reservation"));
        }
        let (quantity, price) = fill_amounts(*qty, *px, amounts.as_deref())?;
        self.book.observe_exact_px(*symbol, price);
        self.book.on_fill_with_remaining(
            client_order_id,
            *symbol,
            Some(signed(*side, &quantity)),
            *recv_ns,
            Some(remaining.clone()),
        );
        if self.book.contains(client_order_id) {
            self.margin.accepted(client_order_id, *recv_ns);
        } else {
            self.margin.retire(client_order_id, *recv_ns);
        }
        Ok(())
    }
    fn on_update(&mut self, update: &OrderUpdate) {
        match update {
            OrderUpdate::Fill {
                client_order_id,
                symbol,
                side,
                qty,
                px,
                amounts,
                recv_ns,
                ..
            } => {
                let values = fill_amounts(*qty, *px, amounts.as_deref()).ok();
                if let Some((_, price)) = &values {
                    self.book.observe_exact_px(*symbol, price.clone());
                }
                self.book.on_fill_with_remaining(
                    client_order_id,
                    *symbol,
                    values.map(|(q, _)| signed(*side, &q)),
                    *recv_ns,
                    None,
                );
                if self.book.contains(client_order_id) {
                    self.margin.accepted(client_order_id, *recv_ns);
                } else {
                    self.margin.retire(client_order_id, *recv_ns);
                }
            }
            OrderUpdate::Cancelled {
                client_order_id,
                recv_ns,
            } => {
                self.book.forget(client_order_id);
                self.margin.retire(client_order_id, *recv_ns);
            }
            OrderUpdate::Reject {
                client_order_id, ..
            } => {
                self.book.forget(client_order_id);
                self.margin
                    .retire(client_order_id, engine_types::clock::mono_ns());
            }
            OrderUpdate::Ack(ack) => self.margin.accepted(&ack.client_order_id, ack.ack_ns),
            _ => {}
        }
    }
    fn observe_price(&mut self, symbol: SymbolId, px: f64) {
        Kernel::observe_price(self, symbol, px);
    }
    fn observe_account_view(&mut self, account: &AccountView) {
        if account.observed_ns < self.latest_account_observed_ns {
            if let Ok(view) = ViewFacts::read(account, &Exact::zero()) {
                self.observe_reference(&view, false);
            }
            return;
        }
        self.latest_account_observed_ns = self.latest_account_observed_ns.max(account.observed_ns);
        self.open_pnl_usdt = account_open_pnl(account).ok();
        if let Ok(view) = ViewFacts::read(account, &Exact::zero()) {
            self.book.prune_through(account.observed_ns);
            self.margin.observe(account.observed_ns);
            // This callback supplies no admission clock. Only a later fresh
            // assessment can enlarge permission; observing may only contract.
            self.observe_reference(&view, false);
        } else {
            // A missing/invalid balance is not evidence of zero account loss.
            self.open_pnl_usdt = None;
        }
    }
    fn observe_closed_trade(&mut self, row: ClosedTradeRow) {
        Kernel::observe_closed_trade(self, row);
    }
    fn observe_wall_clock_ms(&mut self, ms: i64) {
        Kernel::observe_wall_clock_ms(self, ms);
    }
    fn rolling_loss(&self) -> Option<RollingLossView> {
        Some(Kernel::rolling_loss(self))
    }
    fn rolling_loss_rows(&self) -> Vec<ClosedTradeRow> {
        Kernel::rolling_loss_rows(self)
    }
    fn restore_rolling_loss_rows(&mut self, rows: &[ClosedTradeRow]) {
        Kernel::restore_rolling_loss_rows(self, rows);
    }
    fn register_order(&mut self, id: &str, intent: &Intent, qty: f64) {
        Kernel::register_order(self, id, intent, qty);
    }
    fn physical_exposure_interval(
        &mut self,
        symbol: SymbolId,
        account: &AccountView,
    ) -> Result<engine_types::risk::PhysicalExposureInterval, DenyReason> {
        self.physical_interval_for(symbol, account)
    }
    fn register_order_with_account(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: f64,
        account: &AccountView,
    ) {
        Kernel::register_order_with_account(self, id, intent, qty, account);
    }
    fn register_order_price_range_with_account(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: f64,
        range: (f64, f64),
        account: &AccountView,
    ) {
        Kernel::register_order_price_range_with_account(self, id, intent, qty, range, account);
    }
    fn register_order_exact_price_range_with_account(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: &Exact,
        price_range: (&Exact, &Exact),
        account: &AccountView,
    ) {
        Kernel::register_order_exact_price_range_with_account(
            self,
            id,
            intent,
            qty,
            price_range,
            account,
        );
    }
    fn complete_order(&mut self, id: &str, ns: u64) {
        Kernel::complete_order(self, id, ns);
    }
    fn mark_order_attempted(&mut self, id: &str) {
        Kernel::mark_order_attempted(self, id);
    }
    fn mark_order_accepted(&mut self, id: &str, ns: u64) {
        Kernel::mark_order_accepted(self, id, ns);
    }
    fn register_order_price_range(
        &mut self,
        id: &str,
        intent: &Intent,
        qty: f64,
        low: f64,
        high: f64,
    ) {
        Kernel::register_order_price_range(self, id, intent, qty, low, high);
    }
}
fn optional_number(
    number: Option<&engine_types::numeric::ExactNumber>,
) -> Result<Option<Exact>, DenyReason> {
    number
        .map(|number| {
            number
                .validate_provenance()
                .map_err(|e| unknown(e.to_string()))?;
            number
                .value
                .validate_storage()
                .map_err(|e| unknown(e.to_string()))?;
            if number.value.is_negative() {
                return Err(unknown("negative venue margin or price metric"));
            }
            Ok(number.value.clone())
        })
        .transpose()
}
fn account_open_pnl(account: &AccountView) -> Result<Exact, DenyReason> {
    account
        .positions
        .iter()
        .try_fold(Exact::zero(), |pnl, position| {
            let mark = optional_number(
                position
                    .exact_amounts
                    .as_ref()
                    .and_then(|a| a.mark_price.as_ref()),
            )?;
            let Some(mark) = mark else { return Ok(pnl) };
            let entry = position.entry_price().map_err(|e| unknown(e.to_string()))?;
            let quantity = position.quantity().map_err(|e| unknown(e.to_string()))?;
            Ok(pnl + (mark - entry) * signed(position.side, &quantity))
        })
}
fn fill_amounts(
    qty: f64,
    px: f64,
    amounts: Option<&engine_types::numeric::ExecutionAmounts>,
) -> Result<(Exact, Exact), DenyReason> {
    if let Some(amounts) = amounts {
        amounts
            .validate_projection(qty, px, None)
            .map_err(|e| unknown(e.to_string()))?;
        Ok((amounts.quantity.value.clone(), amounts.price.value.clone()))
    } else {
        let qty = exact(qty)?;
        let px = exact(px)?;
        if !qty.is_positive() || !px.is_positive() {
            return Err(unknown("execution quantity or price is invalid"));
        }
        Ok((qty, px))
    }
}
fn read_stop(intent: &Intent, low: &Exact, high: &Exact) -> Result<Exact, DenyReason> {
    let trigger = intent
        .stop_price()
        .map_err(|_| DenyReason::MissingStop)?
        .ok_or(DenyReason::MissingStop)?;
    let distance = match intent.side {
        Side::Buy if trigger < *low => high - trigger,
        Side::Sell if trigger > *high => trigger - low,
        _ => return Err(DenyReason::MissingStop),
    };
    distance
        .checked_div(high)
        .map_err(|e| unknown(e.to_string()))
}
#[derive(Default)]
struct Projected {
    gross_usdt: Exact,
    modelled_stop_charge_usdt: Exact,
}
impl Projected {
    fn add(&mut self, notional: &Exact) {
        self.gross_usdt += notional;
    }
}
struct ViewFacts {
    observed_ns: u64,
    equity_usdt: Exact,
    available_usdt: Exact,
    net: Vec<(u16, Exact)>,
    entry_px: Vec<(u16, Exact)>,
    stops: Vec<(u16, Side, Exact, Exact)>,
    unprotected: bool,
}
impl ViewFacts {
    fn read(account: &AccountView, tolerance: &Exact) -> Result<Self, DenyReason> {
        let equity = account.equity().map_err(|_| {
            unknown("account equity is not a number or disagrees with its projection")
        })?;
        if !equity.is_positive() {
            return Err(unknown("account equity is not positive"));
        }
        let available = account.available().map_err(|_| {
            unknown("available margin is not a number or disagrees with its projection")
        })?;
        let mut facts = Self {
            observed_ns: account.observed_ns,
            equity_usdt: equity,
            available_usdt: available,
            net: Vec::new(),
            entry_px: Vec::new(),
            stops: Vec::new(),
            unprotected: false,
        };
        for position in &account.positions {
            position
                .validate_stop_projection()
                .map_err(|_| unknown("native stop disagrees with its compatibility projection"))?;
            let qty = position
                .quantity()
                .map_err(|_| unknown("position quantity is not a readable size"))?;
            if qty.is_negative() {
                return Err(unknown("position quantity is not a readable size"));
            }
            if qty <= *tolerance {
                continue;
            }
            let entry = position
                .entry_price()
                .map_err(|_| unknown("position entry price is not a positive number"))?;
            if !entry.is_positive() {
                return Err(unknown("position entry price is not a positive number"));
            }
            let signed_qty = signed(position.side, &qty);
            match facts.net.iter_mut().find(|(s, _)| *s == position.symbol.0) {
                Some((_, running)) => {
                    if running.is_negative() != signed_qty.is_negative() {
                        return Err(unknown("account view holds both sides of one symbol"));
                    }
                    *running += signed_qty;
                }
                None => facts.net.push((position.symbol.0, signed_qty)),
            }
            if facts.entry_px.iter().all(|(s, _)| *s != position.symbol.0) {
                facts.entry_px.push((position.symbol.0, entry.clone()));
            }
            if !position.stop_attached {
                facts.unprotected = true;
            } else {
                let stop = position
                    .stop_price()
                    .map_err(|_| unknown("attached position stop has no positive price"))?;
                if !stop.is_positive() {
                    return Err(unknown("attached position stop has no positive price"));
                }
                facts
                    .stops
                    .push((position.symbol.0, position.side, entry, stop));
            }
        }
        Ok(facts)
    }
    fn net_qty(&self, symbol: SymbolId) -> Exact {
        self.net
            .iter()
            .find(|(s, _)| *s == symbol.0)
            .map_or_else(Exact::zero, |(_, q)| q.clone())
    }
    fn entry_px(&self, symbol: SymbolId) -> Option<&Exact> {
        self.entry_px
            .iter()
            .find(|(s, _)| *s == symbol.0)
            .map(|(_, px)| px)
    }
    fn exposures(&self) -> impl Iterator<Item = (SymbolId, &Exact)> {
        self.net.iter().map(|(s, q)| (SymbolId(*s), q))
    }
}

#[cfg(test)]
mod reporting_tests {
    use super::*;
    #[test]
    fn an_underflowing_report_is_zero_instead_of_maximum_money() {
        assert_eq!(report(&Exact::parse_decimal("1e-400").unwrap()), 0.0);
        assert_eq!(report(&Exact::parse_decimal("-1e-400").unwrap()), 0.0);
        assert_eq!(report(&Exact::parse_decimal("1e400").unwrap()), f64::MAX);
        assert_eq!(report(&Exact::parse_decimal("-1e400").unwrap()), -f64::MAX);
    }
}
