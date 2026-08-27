//! The gate. Fixed evaluation order, documented on [`Kernel::assess`].

use engine_types::ids::SymbolId;
use engine_types::orders::{Intent, OrderKind, OrderUpdate, Side};
use engine_types::risk::{AccountView, DenyReason, RiskKernel, RiskVerdict};

use crate::config::{ConfigError, KernelConfig};
use crate::envelope::Envelope;
use crate::exposure::{Book, Pending};

pub struct Kernel {
    cfg: KernelConfig,
    envelope: Envelope,
    book: Book,
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
        let envelope = Envelope::new(cfg.envelope.clone());
        Ok(Self {
            cfg,
            envelope,
            book: Book::default(),
        })
    }

    /// A price the kernel may value exposure at. The engine feeds this from
    /// market state; fills update it too.
    pub fn observe_price(&mut self, symbol: SymbolId, px: f64) {
        self.book.observe_px(symbol, px);
    }

    /// Bind an approved intent to the client order id the engine minted for it,
    /// before the order is sent. An unregistered order is exposure nothing has
    /// reserved: it is invisible to every cap until its fill arrives.
    pub fn register_order(&mut self, client_order_id: &str, intent: &Intent, approved_qty: f64) {
        let quoted = match intent.kind {
            OrderKind::Limit { px, .. } if px.is_finite() && px > 0.0 => px,
            _ => 0.0,
        };
        let px = quoted.max(self.book.px(intent.symbol).unwrap_or(0.0));
        self.book.register(
            client_order_id,
            Pending {
                symbol: intent.symbol,
                signed_qty: signed(intent.side, approved_qty),
                reduce_only: intent.reduce_only,
                px,
            },
        );
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

        // 3. A genuine exit passes the staleness refusal below: risk-reducing
        //    orders flow while blind, and the venue's own reduce-only
        //    enforcement bounds an exit sized from an old reading. Stale
        //    equity is NOT folded into the envelope here — only entries
        //    observe.
        let recent = self.book.fills_after(account.observed_ns);
        let net_qty = view.net_qty(intent.symbol)
            + recent.get(&intent.symbol.0).copied().unwrap_or(0.0);
        let delta = signed(intent.side, ask_qty);
        let reduces = net_qty.abs() > self.cfg.qty_tolerance && delta * net_qty < 0.0;
        if intent.reduce_only {
            if !reduces {
                return Err(unknown(
                    "reduce_only intent does not reduce the position it names",
                ));
            }
            if let OrderKind::Limit { px, .. } = intent.kind {
                if !px.is_finite() || px <= 0.0 {
                    return Err(unknown("exit limit price is not a positive number"));
                }
            }
            // The venue bounds ONE reduce-only order to the position, not a
            // stack of them: what resting exits already cover is spoken for.
            let covered = self.book.pending_reduce_qty(intent.symbol);
            let open = net_qty.abs() - covered;
            if open <= self.cfg.qty_tolerance {
                return Err(unknown(
                    "the position is already fully covered by resting exits",
                ));
            }
            return Ok(ask_qty.min(open));
        }

        // 4. Entries are judged only against evidence about the account now.
        if age_ns > self.cfg.max_account_view_age_ns {
            return Err(DenyReason::StaleAccountView {
                age_ns,
                max_age_ns: self.cfg.max_account_view_age_ns,
            });
        }

        self.envelope.observe_equity(view.equity_usdt);

        // An unflagged reduction is judged as an entry from here on, but must
        // not cross through flat to the other side.
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

        // The book this order leaves, walked once so the envelope and the
        // account caps below can never disagree about what is on it.
        let notional = ask_qty * px;
        let projected = self.projected_book(notional, stop_fraction, account, &view)?;

        // 6. The equity-anchored envelope.
        let allowance_usdt = self.envelope.allowance_usdt();
        if projected.worst_case_loss_usdt > allowance_usdt {
            return Err(DenyReason::EnvelopeBreached {
                worst_case_loss_usdt: projected.worst_case_loss_usdt,
                allowance_usdt,
            });
        }

        // 7. The account-wide capital caps.
        self.account_caps(notional, &projected, &view)?;
        Ok(ask_qty)
    }

    /// Account-wide gross notional once this order is added, plus the
    /// worst-case loss the envelope judges.
    ///
    /// Nothing here nets this order against the position it lands on: a book
    /// that already holds 100 long and asks for 100 more short counts 200, not
    /// zero. Both sides really are exposure until one of them closes, and the
    /// Python kernel's own gross figure is summed the same way.
    fn projected_book(
        &mut self,
        notional: f64,
        stop_fraction: f64,
        account: &AccountView,
        view: &ViewFacts,
    ) -> Result<Projected, DenyReason> {
        // Fills newer than the view are in neither the view nor the
        // reservations — fold them in so a just-filled position is never
        // counted nowhere.
        let mut recent = self.book.fills_after(account.observed_ns);
        let mut projected = Projected::default();
        projected.add(notional);
        projected.worst_case_loss_usdt = self
            .envelope
            .position_worst_case_usdt(notional, stop_fraction);
        for (symbol, qty) in view.exposures() {
            let held_px = self
                .price_for(symbol, view)
                .ok_or_else(|| unknown("no price for a held symbol"))?;
            let effective_qty = qty + recent.remove(&symbol.0).unwrap_or(0.0);
            let held_usdt = effective_qty.abs() * held_px;
            projected.add(held_usdt);
            projected.worst_case_loss_usdt +=
                self.envelope.position_worst_case_usdt(held_usdt, 0.0);
        }
        for (symbol, qty) in recent {
            if qty.abs() <= self.cfg.qty_tolerance {
                continue;
            }
            let held_px = self
                .price_for(SymbolId(symbol), view)
                .ok_or_else(|| unknown("no price for a just-filled symbol"))?;
            let held_usdt = qty.abs() * held_px;
            projected.add(held_usdt);
            projected.worst_case_loss_usdt +=
                self.envelope.position_worst_case_usdt(held_usdt, 0.0);
        }
        let in_flight = self
            .book
            .pending_notional_by_symbol(|symbol| self.price_for(symbol, view))
            .ok_or_else(|| unknown("no price for an in-flight symbol"))?;
        for (_symbol, pending_usdt) in in_flight {
            projected.add(pending_usdt);
            projected.worst_case_loss_usdt +=
                self.envelope.position_worst_case_usdt(pending_usdt, 0.0);
        }
        Ok(projected)
    }

    /// The caps that bound the whole account rather than one strategy, in
    /// order from the smallest thing an operator can change to the largest, so
    /// the first refusal is the most actionable one.
    ///
    /// Every cap here was sized against the configured capital reference, so
    /// each is multiplied by how far the reference has moved — the same
    /// rescale `profile_at_capital_reference` does to the whole profile.
    ///
    /// These refuse rather than clamp, as the Python kernel refuses the whole
    /// batch.
    fn account_caps(
        &self,
        notional: f64,
        projected: &Projected,
        view: &ViewFacts,
    ) -> Result<(), DenyReason> {
        let caps = &self.cfg.envelope;
        let scale = self.envelope.scale();

        let cap_usdt = caps.max_component_gross_notional_usdt * scale;
        if projected.gross_usdt > cap_usdt {
            return Err(DenyReason::ComponentGrossBreached {
                gross_usdt: projected.gross_usdt,
                cap_usdt,
            });
        }

        // The intent carries no leverage of its own, so the account leverage
        // stands in.
        let leverage = self.cfg.leverage;
        let margin_usdt = projected.gross_usdt / leverage;
        let cap_usdt = caps.max_initial_margin_usdt * scale;
        if margin_usdt > cap_usdt {
            return Err(DenyReason::InitialMarginBreached {
                margin_usdt,
                cap_usdt,
            });
        }

        // Available margin is what is left AFTER the standing book's margin is
        // deducted, so only the increase is new money — charging the whole book
        // against it would count the standing book twice. This order is the
        // whole increase, because nothing above nets it against the book.
        let additional_margin_usdt = notional / leverage;
        if additional_margin_usdt > view.available_usdt {
            return Err(DenyReason::AvailableMarginExhausted {
                additional_margin_usdt,
                available_usdt: view.available_usdt,
            });
        }
        Ok(())
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

}

impl RiskKernel for Kernel {
    /// Evaluated in this order, first refusal wins:
    ///
    /// 1. a view stamped after the decision — unknown state;
    /// 2. readability of the view and the intent — anything unreadable is
    ///    [`DenyReason::UnknownState`], before the age check, so a genuine
    ///    exit can still be sized from a stale-but-readable view;
    /// 3. exit or entry: a genuine exit is clamped to the position and stops
    ///    here — risk-reducing orders flow even under a stale reading;
    /// 4. entry freshness — too old is [`DenyReason::StaleAccountView`];
    /// 5. stop discipline — [`DenyReason::MissingStop`];
    /// 6. the equity-anchored envelope — [`DenyReason::EnvelopeBreached`];
    /// 7. the account-wide capital caps, smallest scope first: the whole
    ///    book's gross ([`DenyReason::ComponentGrossBreached`]), the whole
    ///    book's margin ([`DenyReason::InitialMarginBreached`]), and whether
    ///    the account's spare margin funds the increase
    ///    ([`DenyReason::AvailableMarginExhausted`]).
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
                recv_ns,
                ..
            } => {
                self.book.observe_px(*symbol, *px);
                self.book
                    .on_fill(client_order_id, *symbol, signed(*side, qty.abs()), *recv_ns);
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

    fn register_order(&mut self, client_order_id: &str, intent: &Intent, approved_qty: f64) {
        Kernel::register_order(self, client_order_id, intent, approved_qty);
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

/// The book once this order is added, as every cap below the envelope sees it.
#[derive(Default)]
struct Projected {
    gross_usdt: f64,
    worst_case_loss_usdt: f64,
}

impl Projected {
    fn add(&mut self, notional_usdt: f64) {
        self.gross_usdt += notional_usdt;
    }
}

/// What the kernel could read out of one account view.
struct ViewFacts {
    equity_usdt: f64,
    /// Spare margin the venue reports. Legitimately negative when the owner
    /// hand-trades the account, which is a reading, not a fault.
    available_usdt: f64,
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
            available_usdt: account.available_usdt,
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
