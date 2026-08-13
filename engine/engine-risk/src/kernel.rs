//! The gate. Fixed evaluation order, documented on [`Kernel::assess`].

use engine_types::ids::{StrategyId, SymbolId};
use engine_types::orders::{Intent, OrderKind, OrderUpdate, Side};
use engine_types::risk::{AccountView, DenyReason, RiskKernel, RiskVerdict};

use crate::config::{ConfigError, KernelConfig};
use crate::envelope::Envelope;
use crate::exposure::{Book, Pending};
use crate::loss_guard::{LossGuard, LossGuardAnchor};

pub struct Kernel {
    cfg: KernelConfig,
    guard: LossGuard,
    envelope: Envelope,
    book: Book,
    wall_ns: Option<u64>,
    /// The anchor as last handed to the engine for the log, so
    /// `take_control_anchor` reports only real changes.
    taken_anchor: Option<LossGuardAnchor>,
}

fn unknown(detail: impl Into<String>) -> DenyReason {
    DenyReason::UnknownState {
        detail: detail.into(),
    }
}

fn signed(side: Side, qty: f64) -> f64 {
    match side {
        Side::Buy => qty,
        Side::Sell => -qty,
    }
}

impl Kernel {
    pub fn new(cfg: KernelConfig) -> Result<Self, ConfigError> {
        cfg.validate()?;
        let guard = LossGuard::new(cfg.loss_guard.clone());
        let envelope = Envelope::new(cfg.envelope.clone());
        Ok(Self {
            cfg,
            guard,
            envelope,
            book: Book::default(),
            wall_ns: None,
            taken_anchor: None,
        })
    }

    /// Venue wall-clock nanoseconds, for the loss guard's UTC day roll. Until
    /// the engine supplies one the day's anchor is set once and never
    /// re-anchors, which spends the budget rather than refreshing it.
    pub fn observe_wall_clock_ns(&mut self, wall_ns: u64) {
        self.wall_ns = Some(wall_ns);
    }

    /// A price the kernel may value exposure at. The engine feeds this from
    /// market state; fills update it too.
    pub fn observe_price(&mut self, symbol: SymbolId, px: f64) {
        self.book.observe_px(symbol, px);
    }

    /// Bind an approved intent to the client order id the engine minted for it,
    /// before the order is sent. Unregistered orders are invisible to the
    /// partition until their fills arrive, and those fills are then charged to
    /// every strategy.
    pub fn register_order(&mut self, client_order_id: &str, intent: &Intent, approved_qty: f64) {
        let quoted = match intent.kind {
            OrderKind::Limit { px, .. } if px.is_finite() && px > 0.0 => px,
            _ => 0.0,
        };
        let px = quoted.max(self.book.px(intent.symbol).unwrap_or(0.0));
        self.book.register(
            client_order_id,
            Pending {
                strategy: intent.strategy,
                symbol: intent.symbol,
                signed_qty: signed(intent.side, approved_qty),
                reduce_only: intent.reduce_only,
                px,
            },
        );
    }

    /// The persistable loss-guard anchor. Write it beside the log.
    pub fn loss_guard_anchor(&self) -> LossGuardAnchor {
        self.guard.anchor()
    }

    pub fn restore_loss_guard(&mut self, anchor: LossGuardAnchor) {
        self.guard.restore(anchor);
    }

    /// Clear a loss-guard trip. Only an explicit operator action may call this.
    pub fn reset_loss_guard(&mut self) {
        self.guard.reset();
    }

    pub fn capital_reference_usdt(&self) -> f64 {
        self.envelope.reference_usdt()
    }

    fn price_for(&self, symbol: SymbolId, view: &ViewFacts) -> Option<f64> {
        match (self.book.px(symbol), view.entry_px(symbol)) {
            (Some(a), Some(b)) => Some(a.max(b)),
            (Some(a), None) => Some(a),
            (None, Some(b)) => Some(b),
            (None, None) => None,
        }
    }

    fn evaluate(&mut self, intent: &Intent, account: &AccountView) -> Result<f64, DenyReason> {
        // 1. A view stamped after the decision is nonsense for everyone.
        if account.observed_ns > intent.decided_ns {
            return Err(unknown("account view is newer than the decision it judges"));
        }
        let age_ns = intent.decided_ns - account.observed_ns;

        // 2. Can the view and the intent be read at all? Exits need this
        //    too: the clamp below sizes against the position rows.
        let view = ViewFacts::read(account, self.cfg.qty_tolerance)?;
        let ask_qty = read_intent_qty(intent)?;

        // 3. A genuine exit passes the staleness and trip refusals: the
        //    fleet lets risk-reducing orders flow under both BLOCKED and
        //    TRIPPED (a trip's remedy IS exits), and the venue's own
        //    reduce-only enforcement bounds an exit sized from an old
        //    reading. Stale or tripped equity is NOT folded into the guard
        //    or the envelope here — only entries observe.
        let net_qty = view.net_qty(intent.symbol);
        let delta = signed(intent.side, ask_qty);
        let reduces = net_qty.abs() > self.cfg.qty_tolerance && delta * net_qty < 0.0;
        if intent.reduce_only {
            if !reduces {
                return Err(unknown(
                    "reduce_only intent does not reduce the position it names",
                ));
            }
            return Ok(ask_qty.min(net_qty.abs()));
        }

        // 4. Entries are judged only against evidence about the account now.
        if age_ns > self.cfg.max_account_view_age_ns {
            return Err(DenyReason::StaleAccountView {
                age_ns,
                max_age_ns: self.cfg.max_account_view_age_ns,
            });
        }

        // 5. The account loss guard.
        if let Some(trip) = self.guard.observe(view.equity_usdt, self.wall_ns) {
            return Err(DenyReason::LossGuardTripped {
                equity_usdt: trip.equity_usdt,
                floor_usdt: trip.floor_usdt,
            });
        }
        self.envelope.observe_equity(view.equity_usdt);

        // 6. An unflagged reduction is judged as an entry from here on, but
        //    must not cross through flat to the other side.
        if reduces && ask_qty > net_qty.abs() + self.cfg.qty_tolerance {
            return Err(unknown("intent crosses through flat to the other side"));
        }

        // 5. Stop discipline. Every position carries one: an entry without a
        //    stop is refused before it reaches the venue, and a book already
        //    holding an unprotected position takes no new risk.
        if view.unprotected || intent.stop.is_none() {
            return Err(DenyReason::MissingStop);
        }
        let (low_px, px) = self.entry_prices(intent, &view)?;
        let stop_fraction = read_stop(intent, low_px, px)?;

        // 6. The equity-anchored envelope, against the book this order leaves.
        let notional = ask_qty * px;
        let mut worst_case_loss_usdt = self
            .envelope
            .position_worst_case_usdt(notional, stop_fraction);
        for (symbol, qty) in view.exposures() {
            let held_px = self
                .price_for(symbol, &view)
                .ok_or_else(|| unknown("no price for a held symbol"))?;
            worst_case_loss_usdt += self
                .envelope
                .position_worst_case_usdt(qty.abs() * held_px, 0.0);
        }
        let in_flight = self
            .book
            .pending_notional_usdt(|symbol| self.price_for(symbol, &view))
            .ok_or_else(|| unknown("no price for an in-flight symbol"))?;
        worst_case_loss_usdt += self.envelope.position_worst_case_usdt(in_flight, 0.0);
        let allowance_usdt = self.envelope.allowance_usdt();
        if worst_case_loss_usdt > allowance_usdt {
            return Err(DenyReason::EnvelopeBreached {
                worst_case_loss_usdt,
                allowance_usdt,
            });
        }

        // 7. The per-strategy capital partition.
        self.partition_qty(intent.strategy, ask_qty, px, &view)
    }

    /// The lowest and highest price this order could reasonably fill at, from
    /// its own limit and from what the kernel last saw. Exposure is valued at
    /// the higher one and the stop is judged against both.
    fn entry_prices(&self, intent: &Intent, view: &ViewFacts) -> Result<(f64, f64), DenyReason> {
        let quoted = match intent.kind {
            OrderKind::Limit { px, .. } => {
                if !px.is_finite() || px <= 0.0 {
                    return Err(unknown("limit price is not a positive number"));
                }
                Some(px)
            }
            OrderKind::Market => None,
        };
        match (quoted, self.price_for(intent.symbol, view)) {
            (Some(a), Some(b)) => Ok((a.min(b), a.max(b))),
            (Some(a), None) => Ok((a, a)),
            (None, Some(b)) => Ok((b, b)),
            (None, None) => Err(unknown("no price to value this symbol")),
        }
    }

    fn partition_qty(
        &self,
        strategy: StrategyId,
        ask_qty: f64,
        px: f64,
        view: &ViewFacts,
    ) -> Result<f64, DenyReason> {
        if self.cfg.partition.allocations.is_empty() {
            return Ok(ask_qty);
        }
        let requested_usdt = ask_qty * px;
        let Some(share) = self.cfg.partition.share(strategy) else {
            // A partition that exempts the sleeves it does not name is not a
            // partition.
            return Err(DenyReason::PartitionExhausted {
                strategy,
                requested_usdt,
                remaining_usdt: 0.0,
            });
        };
        let scale = self.envelope.scale();
        let cap_usdt = (share.max_gross_notional_usdt * scale)
            .min(share.max_initial_margin_usdt * scale * self.cfg.partition.leverage);
        let used_usdt = self
            .book
            .strategy_notional_usdt(strategy, |symbol| self.price_for(symbol, view))
            .ok_or_else(|| unknown("no price for a symbol this strategy holds"))?;
        let remaining_usdt = (cap_usdt - used_usdt).max(0.0);
        if requested_usdt <= remaining_usdt {
            return Ok(ask_qty);
        }
        let clamped_qty = remaining_usdt / px;
        if remaining_usdt < self.cfg.partition.min_order_notional_usdt
            || clamped_qty <= self.cfg.qty_tolerance
        {
            return Err(DenyReason::PartitionExhausted {
                strategy,
                requested_usdt,
                remaining_usdt,
            });
        }
        Ok(clamped_qty)
    }
}

impl RiskKernel for Kernel {
    /// Evaluated in this order, first refusal wins:
    ///
    /// 1. a view stamped after the decision — unknown state;
    /// 2. readability of the view and the intent — anything unreadable is
    ///    [`DenyReason::UnknownState`] (the Python guard also puts "no
    ///    reading" before its age check);
    /// 3. exit or entry: a genuine exit is clamped to the position and stops
    ///    here — risk-reducing orders flow even under a stale reading or a
    ///    tripped guard, exactly as the fleet lets them;
    /// 4. entry freshness — too old is [`DenyReason::StaleAccountView`];
    /// 5. the account loss guard — [`DenyReason::LossGuardTripped`];
    /// 6. stop discipline — [`DenyReason::MissingStop`];
    /// 7. the equity-anchored envelope — [`DenyReason::EnvelopeBreached`];
    /// 8. the per-strategy partition, which clamps before it refuses —
    ///    [`DenyReason::PartitionExhausted`].
    fn assess(&mut self, intent: &Intent, account: &AccountView) -> RiskVerdict {
        match self.evaluate(intent, account) {
            Ok(qty) => RiskVerdict::Allow { qty },
            Err(reason) => RiskVerdict::Deny { reason },
        }
    }

    fn on_update(&mut self, update: &OrderUpdate) {
        match update {
            OrderUpdate::Fill {
                client_order_id,
                symbol,
                side,
                qty,
                px,
                ..
            } => {
                self.book.observe_px(*symbol, *px);
                self.book
                    .on_fill(client_order_id, *symbol, signed(*side, qty.abs()));
            }
            OrderUpdate::Cancelled {
                client_order_id, ..
            } => self.book.forget(client_order_id),
            OrderUpdate::Reject {
                client_order_id, ..
            } => self.book.forget(client_order_id),
            OrderUpdate::Ack(_) | OrderUpdate::StopAttached { .. } => {}
            // A private-stream gap loses nothing here: registered orders may
            // still fill, and the engine refreshes the account view that
            // `assess` judges against.
            OrderUpdate::StreamReset { .. } => {}
        }
    }

    // Forward the trait hooks to the inherent methods, so a caller generic
    // over `RiskKernel` reaches the real accounting and not the no-op
    // defaults.
    fn observe_price(&mut self, symbol: SymbolId, px: f64) {
        Kernel::observe_price(self, symbol, px);
    }

    fn observe_wall_clock_ns(&mut self, wall_ns: u64) {
        Kernel::observe_wall_clock_ns(self, wall_ns);
    }

    fn register_order(&mut self, client_order_id: &str, intent: &Intent, approved_qty: f64) {
        Kernel::register_order(self, client_order_id, intent, approved_qty);
    }

    fn take_control_anchor(&mut self) -> Option<String> {
        let current = self.guard.anchor();
        if current == LossGuardAnchor::default() && self.taken_anchor.is_none() {
            return None;
        }
        if self.taken_anchor.as_ref() == Some(&current) {
            return None;
        }
        let state = serde_json::to_string(&current).ok()?;
        self.taken_anchor = Some(current);
        Some(state)
    }

    fn restore_control_anchor(&mut self, state: &str) {
        // An unreadable anchor restores nothing: the log's checksums make
        // this unreachable short of a hand-edited record.
        if let Ok(anchor) = serde_json::from_str::<LossGuardAnchor>(state) {
            self.taken_anchor = Some(anchor.clone());
            self.guard.restore(anchor);
        }
    }
}

fn read_intent_qty(intent: &Intent) -> Result<f64, DenyReason> {
    if !intent.qty.is_finite() || intent.qty <= 0.0 {
        return Err(unknown("intent quantity is not a positive number"));
    }
    Ok(intent.qty)
}

/// The worst stop distance, as a fraction of the notional. A stop that cannot
/// protect — absent, unreadable, or on the wrong side of any price this order
/// could fill at — is no stop.
fn read_stop(intent: &Intent, low_px: f64, high_px: f64) -> Result<f64, DenyReason> {
    let Some(stop) = intent.stop else {
        return Err(DenyReason::MissingStop);
    };
    let trigger = stop.trigger_px;
    if !trigger.is_finite() || trigger <= 0.0 {
        return Err(DenyReason::MissingStop);
    }
    match intent.side {
        Side::Buy if trigger >= low_px => Err(DenyReason::MissingStop),
        Side::Sell if trigger <= high_px => Err(DenyReason::MissingStop),
        Side::Buy => Ok((high_px - trigger) / high_px),
        Side::Sell => Ok((trigger - low_px) / high_px),
    }
}

/// What the kernel could read out of one account view.
struct ViewFacts {
    equity_usdt: f64,
    /// Net signed quantity per symbol: positive long, negative short.
    net: Vec<(u16, f64)>,
    entry_px: Vec<(u16, f64)>,
    /// The book holds exposure with no stop attached. New risk waits until it
    /// is protected again.
    unprotected: bool,
}

impl ViewFacts {
    fn read(account: &AccountView, qty_tolerance: f64) -> Result<Self, DenyReason> {
        let equity_usdt = account.equity_usdt;
        if !equity_usdt.is_finite() {
            return Err(unknown("account equity is not a number"));
        }
        if equity_usdt <= 0.0 {
            return Err(unknown("account equity is not positive"));
        }
        if !account.available_usdt.is_finite() {
            return Err(unknown("available margin is not a number"));
        }
        let mut facts = ViewFacts {
            equity_usdt,
            net: Vec::new(),
            entry_px: Vec::new(),
            unprotected: false,
        };
        for position in &account.positions {
            if !position.qty.is_finite() || position.qty < 0.0 {
                return Err(unknown("position quantity is not a readable size"));
            }
            if position.qty <= qty_tolerance {
                continue;
            }
            if !position.entry_px.is_finite() || position.entry_px <= 0.0 {
                return Err(unknown("position entry price is not a positive number"));
            }
            let signed_qty = signed(position.side, position.qty);
            match facts
                .net
                .iter_mut()
                .find(|(symbol, _)| *symbol == position.symbol.0)
            {
                Some((_, running)) => {
                    if *running * signed_qty < 0.0 {
                        return Err(unknown("account view holds both sides of one symbol"));
                    }
                    *running += signed_qty;
                }
                None => facts.net.push((position.symbol.0, signed_qty)),
            }
            if facts
                .entry_px
                .iter()
                .all(|(symbol, _)| *symbol != position.symbol.0)
            {
                facts.entry_px.push((position.symbol.0, position.entry_px));
            }
            if !position.stop_attached {
                facts.unprotected = true;
            }
        }
        Ok(facts)
    }

    fn net_qty(&self, symbol: SymbolId) -> f64 {
        self.net
            .iter()
            .find(|(held, _)| *held == symbol.0)
            .map(|(_, qty)| *qty)
            .unwrap_or(0.0)
    }

    fn entry_px(&self, symbol: SymbolId) -> Option<f64> {
        self.entry_px
            .iter()
            .find(|(held, _)| *held == symbol.0)
            .map(|(_, px)| *px)
    }

    fn exposures(&self) -> impl Iterator<Item = (SymbolId, f64)> + '_ {
        self.net
            .iter()
            .map(|(symbol, qty)| (SymbolId(*symbol), *qty))
    }
}
