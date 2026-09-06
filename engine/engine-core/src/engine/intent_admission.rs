use super::*;

/// Why the engine refused to open exposure before the risk kernel saw the
/// intent. `as_str` is the word the strategy hears in `IntentRefused` and the
/// operator reads in the heartbeat; `detail` is the sentence the verdict
/// record carries. Both are stable: strategies and tests match on them.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub(crate) enum OpeningRefusal {
    /// Another strategy owns exposure or a live opening order on the symbol.
    ForeignStrategyOwner,
    PortfolioExitPending,
    StopRepairPending,
    /// A durable signal source this strategy depends on has a recorded gap.
    SignalSequenceGap,
    SignalProducerUnready,
    StrategyCallbackUnavailable,
    StrategyInactive,
    InstrumentCatalogUnready,
    InstrumentUnlisted,
    OrderDispatchUnresolved,
    /// The operator switched this strategy's entries off.
    RuntimeEntriesDisabled,
    /// The private account stream has not completed gap recovery.
    PrivateStreamUnready,
    /// Boot found orders or exposure the log cannot account for.
    EngineLatched,
}

impl OpeningRefusal {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::ForeignStrategyOwner => "foreign_strategy_owner",
            Self::PortfolioExitPending => "portfolio_exit_pending",
            Self::StopRepairPending => "stop_repair_pending",
            Self::SignalSequenceGap => "signal_sequence_gap",
            Self::SignalProducerUnready => "signal_producer_unready",
            Self::StrategyCallbackUnavailable => "strategy_callback_unavailable",
            Self::StrategyInactive => "strategy_inactive",
            Self::InstrumentCatalogUnready => "instrument_catalog_unready",
            Self::InstrumentUnlisted => "instrument_unlisted",
            Self::OrderDispatchUnresolved => "order_dispatch_unresolved",
            Self::RuntimeEntriesDisabled => "runtime_entries_disabled",
            Self::PrivateStreamUnready => "private_stream_unready",
            Self::EngineLatched => "engine_latched",
        }
    }

    fn detail(self) -> &'static str {
        match self {
            Self::PortfolioExitPending => "portfolio_exit_pending: this sleeve or symbol has an unfinished engine-owned exit",
            Self::StopRepairPending => "stop_repair_pending: this instrument is waiting for confirmed native protection",
            Self::ForeignStrategyOwner => {
                "foreign_strategy_owner: another strategy owns exposure or a live opening order on this symbol"
            }
            Self::SignalSequenceGap => {
                "signal_sequence_gap: a required source has missing observations"
            }
            Self::SignalProducerUnready => "signal_producer_unready: a required producer has not established its startup frontier",
            Self::StrategyCallbackUnavailable => "strategy_callback_unavailable: a strategy callback has no committed outcome",
            Self::StrategyInactive => "strategy_inactive: this durable sleeve has no active configured owner",
            Self::InstrumentCatalogUnready => "instrument_catalog_unready: retained metadata supports recovery while an authoritative refresh is pending",
            Self::InstrumentUnlisted => "instrument_unlisted: retained native metadata permits reductions and stops only",
            Self::OrderDispatchUnresolved => "order_dispatch_unresolved: an attempted order has no authoritative venue outcome",
            Self::RuntimeEntriesDisabled => {
                "this strategy's runtime entry permission is disabled"
            }
            Self::PrivateStreamUnready => "private account stream has not completed gap recovery",
            Self::EngineLatched => "boot could not account for what this account holds",
        }
    }
}

impl std::fmt::Display for OpeningRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

struct RiskApprovedIntent {
    intent: Intent,
    client_order_id: String,
    allowed_qty: engine_types::numeric::Exact,
    work: Option<WorkPolicy>,
}
struct LegalOrder {
    request: OrderRequest,
    approval: RiskApprovedIntent,
}
/// Only the leverage/protection phase can hand an order to the durable commit phase.
struct ProtectedOrder(LegalOrder);

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn signal_inputs_blocked(&self, strategy: StrategyId) -> bool {
        self.signal_dependencies
            .get(strategy.0 as usize)
            .is_some_and(|dependencies| {
                dependencies
                    .iter()
                    .any(|source| self.signals.blocked(*source))
            })
    }

    /// Why this strategy in particular may not open: a signal gap or an
    /// operator switch. Per strategy, not per symbol, and not the engine's
    /// own state.
    pub(super) fn opening_permission_reason(&self, strategy: StrategyId) -> Option<OpeningRefusal> {
        if !self.host.callbacks.is_active(strategy) {
            Some(OpeningRefusal::StrategyInactive)
        } else if self.symbol_admission.refresh_required() {
            Some(OpeningRefusal::InstrumentCatalogUnready)
        } else if self.host.callbacks.faults.contains_key(&strategy) {
            Some(OpeningRefusal::StrategyCallbackUnavailable)
        } else if self.signal_inputs_blocked(strategy) {
            Some(OpeningRefusal::SignalSequenceGap)
        } else if self
            .signal_dependencies
            .get(strategy.idx())
            .is_some_and(|dependencies| {
                dependencies
                    .iter()
                    .any(|source| self.signals.readiness_blocked(*source))
            })
        {
            Some(OpeningRefusal::SignalProducerUnready)
        } else if self.host.entries_enabled.get(&strategy).copied() == Some(false) {
            Some(OpeningRefusal::RuntimeEntriesDisabled)
        } else {
            None
        }
    }

    /// Every reason an entry from this strategy may not open right now, in
    /// the order they are reported: the strategy's own permission, then the
    /// private stream, then the boot latch. Exits flow past all of them.
    pub(super) fn opening_refusal(&self, strategy: StrategyId) -> Option<OpeningRefusal> {
        self.opening_permission_reason(strategy)
            .or_else(|| {
                (!self.dispatches.unresolved.is_empty())
                    .then_some(OpeningRefusal::OrderDispatchUnresolved)
            })
            .or_else(|| {
                (!self.private_stream_ready).then_some(OpeningRefusal::PrivateStreamUnready)
            })
            .or_else(|| (!self.may_open).then_some(OpeningRefusal::EngineLatched))
    }

    pub(super) fn symbol_owned_by_another(&self, strategy: StrategyId, symbol: SymbolId) -> bool {
        self.books.attribution.held_by_another(strategy, symbol)
            || self.books.orders.opening_owned_by_another(strategy, symbol)
    }

    /// Judge and reserve one sibling, appending its send record to the WAL.
    /// The caller owns the accepted group's durability barrier and dispatch.
    pub(super) async fn prepare_intent(
        &mut self,
        intent: Intent,
        client_order_id: Option<String>,
        origin_ns: u64,
        batch_protection: &mut std::collections::HashMap<(SymbolId, Side), f64>,
    ) -> Result<Option<PreparedOrder>, EngineError> {
        let mut intent = intent;
        if intent.reduce_only
            && intent.exact_quantity.is_none()
            && self.instrument_specs.contains_key(&intent.symbol)
        {
            let held = self
                .books
                .attribution
                .signed_exact(intent.strategy, intent.symbol);
            // Legacy full-close requests carry only the projection of the owned lot.
            if held.is_positive() == (intent.side == Side::Sell)
                && held.abs().to_f64().ok() == Some(intent.qty)
            {
                intent.exact_quantity = Some(Box::new(held.abs()));
            }
        }
        let decided_ns = if intent.decided_ns > 0 {
            intent.decided_ns
        } else {
            clock::now_ns()
        };
        self.ledger
            .record(Segment::Decide, decided_ns.saturating_sub(origin_ns));

        if !self.journal_and_admit_intent(&intent, decided_ns, client_order_id.as_deref())? {
            return Ok(None);
        }
        self.retain_portfolio_reduction(&intent)?;
        if self
            .portfolio_controls
            .emergencies
            .contains_key(&intent.symbol)
        {
            return Ok(None);
        }
        let Some(approval) = self.assess_intent(intent, client_order_id)? else {
            return Ok(None);
        };
        let Some(legal) = self.quantize_approved_order(approval)? else {
            return Ok(None);
        };
        let Some(protected) = self
            .confirm_order_protection(legal, batch_protection)
            .await?
        else {
            return Ok(None);
        };
        self.commit_prepared_order(protected, decided_ns, origin_ns)
            .map(Some)
    }

    pub(super) fn emergency_order_refusal(
        &mut self,
        request: &OrderRequest,
        excluding: Option<&str>,
    ) -> Option<String> {
        use engine_types::orders::SleeveOrderEffect;
        use engine_types::portfolio_control::PortfolioEmergencyPhase;
        let Some(SleeveOrderEffect::EmergencyNetReduction { emergency_id }) = request.sleeve_effect
        else {
            return Some("order has no engine emergency owner".into());
        };
        let valid_control = self
            .portfolio_controls
            .emergencies
            .get(&request.symbol)
            .is_some_and(|state| {
                state.id == emergency_id
                    && state.phase == PortfolioEmergencyPhase::CloseNet
                    && state.order_id.as_deref() == Some(request.client_order_id.as_str())
            });
        if !valid_control {
            return Some("emergency order no longer belongs to the active control".into());
        }
        let Some(terms) = request.exact_terms.as_ref() else {
            return Some("emergency order has no exact terms".into());
        };
        let net = self
            .books
            .attribution
            .snapshot()
            .positions
            .into_iter()
            .filter(|row| row.symbol == request.symbol)
            .fold(engine_types::numeric::Exact::zero(), |sum, row| {
                sum + row.signed_qty
            });
        if !request.reduce_only
            || request.stop.is_some()
            || net.is_zero()
            || net.is_positive() == (request.side == Side::Buy)
            || terms.quantity > net.abs()
        {
            return Some("emergency order exceeds the owned physical net".into());
        }
        let interval = match excluding {
            Some(id) => self.risk.physical_exposure_interval_excluding(
                id,
                request.symbol,
                &self.books.account,
            ),
            None => self
                .risk
                .physical_exposure_interval(request.symbol, &self.books.account),
        };
        if !interval.is_ok_and(|interval| {
            interval.certainly_reduces(request.side, &terms.quantity)
                && (!request.close_position
                    || (interval.low() == interval.high()
                        && interval.low().abs() == terms.quantity))
        }) {
            return Some(
                "emergency order no longer certainly reduces the physical position".into(),
            );
        }
        let Some(spec) = self.instrument_specs.get(&request.symbol) else {
            return Some("emergency instrument metadata is unavailable".into());
        };
        let policy = if request.close_position {
            engine_types::order_terms::QuantityPolicy::CloseEntirePosition
        } else {
            engine_types::order_terms::QuantityPolicy::Normal
        };
        if terms
            .validate_projection(request)
            .and_then(|()| terms.validate_wire_grid(spec, request.kind, policy))
            .is_err()
        {
            return Some("emergency order no longer matches the instrument grid".into());
        }
        None
    }

    pub(super) fn prepare_emergency_net_order(
        &mut self,
        state: &engine_types::portfolio_control::PortfolioEmergency,
    ) -> Result<Option<PreparedOrder>, EngineError> {
        use engine_types::numeric::Exact;
        use engine_types::order_terms::{quantize_portfolio_close, QuantityPolicy};
        use engine_types::orders::SleeveOrderEffect;
        let Some(client_order_id) = state.order_id.clone() else {
            return Ok(None);
        };
        let rows = self.books.attribution.snapshot().positions;
        let net = rows
            .iter()
            .filter(|row| row.symbol == state.symbol)
            .fold(Exact::zero(), |sum, row| sum + &row.signed_qty);
        if net.is_zero() {
            return Ok(None);
        }
        let Some(owner) = rows
            .iter()
            .filter(|row| {
                row.symbol == state.symbol && row.signed_qty.is_positive() == net.is_positive()
            })
            .min_by_key(|row| row.strategy)
            .map(|row| row.strategy)
        else {
            return Ok(None);
        };
        let interval = match self
            .risk
            .physical_exposure_interval(state.symbol, &self.books.account)
        {
            Ok(value) => value,
            Err(_) => return Ok(None),
        };
        let side = if net.is_positive() {
            Side::Sell
        } else {
            Side::Buy
        };
        let physical_qty = if side == Side::Sell {
            interval.low().clone().max(Exact::zero())
        } else {
            (-interval.high()).max(Exact::zero())
        };
        let mut quantity = net.abs().min(physical_qty.clone());
        if !quantity.is_positive() {
            return Ok(None);
        }
        let Some(spec) = self.instrument_specs.get(&state.symbol) else {
            return Ok(None);
        };
        if let Some(max) = spec.max_market_qty.as_ref() {
            quantity = quantity.min(max.clone());
        }
        let reference = self.reference_px(state.symbol, &OrderKind::Market);
        let mut policy = QuantityPolicy::Normal;
        let mut terms = quantize_portfolio_close(spec, side, &quantity, reference, policy);
        let below_minimum = spec
            .market_min_qty
            .as_ref()
            .is_some_and(|min| &quantity < min)
            || reference
                .and_then(|px| engine_types::order_terms::strategy_decimal(px).ok())
                .is_some_and(|px| {
                    spec.min_notional
                        .as_ref()
                        .is_some_and(|min| &quantity * &px < *min)
                });
        if terms.is_err()
            && below_minimum
            && self.venue.caps().close_position_below_minimum
            && interval.low() == interval.high()
            && quantity == physical_qty
        {
            policy = QuantityPolicy::CloseEntirePosition;
            terms = quantize_portfolio_close(spec, side, &quantity, reference, policy);
        }
        let Ok(terms) = terms else {
            return Ok(None);
        };
        let qty = terms
            .quantity
            .to_f64()
            .map_err(|e| EngineError::State(e.to_string()))?;
        let mut request = OrderRequest {
            client_order_id: client_order_id.clone(),
            strategy: owner,
            symbol: state.symbol,
            side,
            qty,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            close_position: policy == QuantityPolicy::CloseEntirePosition,
            exact_terms: None,
            sleeve_effect: Some(SleeveOrderEffect::EmergencyNetReduction {
                emergency_id: state.id,
            }),
        };
        terms
            .apply_projection(&mut request)
            .map_err(|e| EngineError::State(e.to_string()))?;
        if self.emergency_order_refusal(&request, None).is_some() {
            return Ok(None);
        }
        let decided_ns = clock::now_ns();
        let intent = Intent {
            exact_prices: None,
            exact_quantity: Some(Box::new(terms.quantity.clone())),
            strategy: owner,
            symbol: state.symbol,
            side,
            qty: request.qty,
            kind: request.kind,
            stop: None,
            reduce_only: true,
            tag: format!("portfolio-emergency:{}", state.id),
            decided_ns,
            work: None,
            leverage: None,
        };
        self.wal.append(&WalRecord::Intent {
            intent: intent.clone(),
        })?;
        self.wal.append(&WalRecord::Verdict {
            client_order_id: Some(client_order_id.clone()),
            verdict: RiskVerdict::Allow { qty: request.qty },
        })?;
        let approval = RiskApprovedIntent {
            allowed_qty: request
                .exact_terms
                .as_ref()
                .expect("canonical emergency terms")
                .quantity
                .clone(),
            intent,
            client_order_id,
            work: None,
        };
        self.commit_prepared_order(
            ProtectedOrder(LegalOrder { request, approval }),
            decided_ns,
            decided_ns,
        )
        .map(Some)
    }

    fn journal_and_admit_intent(
        &mut self,
        intent: &Intent,
        decided_ns: u64,
        client_order_id: Option<&str>,
    ) -> Result<bool, EngineError> {
        // A non-finite number would be written to the log as null and stop
        // the next boot's replay dead, so it is refused before any append.
        if let Some(what) = unreal_number(intent) {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "intent {} refused: {what} is not a finite number",
                    intent.tag
                ),
            })?;
            tracing::error!(tag = %intent.tag, what, "intent carries an unreal number");
            self.tell_refused(intent, "unreal_number", client_order_id)?;
            return Ok(false);
        }

        if intent.validate_price_projection().is_err() {
            self.tell_refused(intent, "invalid_exact_prices", client_order_id)?;
            return Ok(false);
        }
        if intent.exact_quantity.is_some() && intent.quantity().is_err() {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "intent {} refused: invalid canonical quantity projection",
                    intent.tag
                ),
            })?;
            self.tell_refused(intent, "invalid_exact_quantity", client_order_id)?;
            return Ok(false);
        }

        // The strategy's own words, work policy included, before the engine
        // touches anything.
        self.wal.append(&WalRecord::Intent {
            intent: intent.clone(),
        })?;

        // The engine's own reasons an entry may not open, checked before the
        // kernel sees it. Whoever owns the symbol comes first: a symbol held
        // by another sleeve is refused however healthy this one is.
        if !intent.reduce_only {
            let refusal = if self.stop_repairs_pending.contains(&intent.symbol) {
                Some(OpeningRefusal::StopRepairPending)
            } else if self
                .portfolio_controls
                .blocked(intent.strategy, intent.symbol)
            {
                Some(OpeningRefusal::PortfolioExitPending)
            } else if !self.instrument_specs.contains_key(&intent.symbol)
                && self.symbol_owned_by_another(intent.strategy, intent.symbol)
            {
                Some(OpeningRefusal::ForeignStrategyOwner)
            } else if intent.symbol.idx() < self.books.market.table.len()
                && !self
                    .symbol_admission
                    .listed(self.books.market.table.name(intent.symbol))
            {
                Some(OpeningRefusal::InstrumentUnlisted)
            } else {
                self.opening_refusal(intent.strategy)
            };
            if let Some(refusal) = refusal {
                self.deny_opening(intent, refusal, client_order_id)?;
                return Ok(false);
            }
        }

        // The quote this decision was priced against, bounded the way the
        // account reading already is: a price older than the bound is not
        // evidence about the market now. The feed pings and reconnects on
        // its own, but a silent-but-alive socket, or a reconnect that never
        // lands, leaves the last quote standing — and this is the one place
        // that refuses to OPEN against it. The stamp is the quote's own
        // receive time on the engine's monotonic clock, never a wall-clock
        // guess; a symbol that has never quoted has no stamp at all, and
        // the absence of a price is the stalest price there is (a feed
        // reset clears every stamp for the same reason). Exits flow
        // whatever the age — taking risk off must never wait on a fresh
        // price — and cancels and amends of protective orders never come
        // through here at all.
        if !intent.reduce_only {
            let quote_ns = self
                .books
                .market
                .quotes
                .get(intent.symbol.0 as usize)
                .map(|quote| quote.recv_ns)
                .unwrap_or(0);
            let age_ns = decided_ns.saturating_sub(quote_ns);
            if quote_ns == 0 || age_ns > self.max_quote_age_ns {
                let verdict = RiskVerdict::Deny {
                    reason: DenyReason::StaleQuote {
                        age_ns,
                        max_age_ns: self.max_quote_age_ns,
                    },
                };
                self.wal.append(&WalRecord::Verdict {
                    client_order_id: None,
                    verdict,
                })?;
                tracing::warn!(
                    tag = %intent.tag,
                    symbol = self.books.market.table.name(intent.symbol),
                    age_ms = age_ns / 1_000_000,
                    never_quoted = quote_ns == 0,
                    "refused: the quote this entry was decided against is too old to open on"
                );
                self.tell_refused(intent, "stale_quote", client_order_id)?;
                return Ok(false);
            }
        }

        Ok(true)
    }

    fn assess_intent(
        &mut self,
        intent: Intent,
        client_order_id: Option<String>,
    ) -> Result<Option<RiskApprovedIntent>, EngineError> {
        // An entry the strategy asked to have worked starts as a resting
        // limit instead of crossing the spread. Rewritten here, before the
        // kernel judges it, so the kernel judges the order that is actually
        // sent — and before the id is minted, so a refusal still costs
        // nothing.
        let mut intent = intent;
        let work = self.plan_resting_entry(&mut intent);

        let mut canonical_allowed = None;
        let verdict = if self.instrument_specs.contains_key(&intent.symbol) {
            match self.risk.assess_portfolio(
                &intent,
                &self.books.account,
                &self.books.attribution.snapshot(),
            ) {
                engine_types::risk::PortfolioRiskVerdict::Allow { qty, .. } => {
                    let permitted = intent
                        .quantity()
                        .is_ok_and(|requested| qty.is_positive() && qty <= requested);
                    match qty.to_f64() {
                        Ok(projection) if permitted => {
                            canonical_allowed = Some(qty);
                            RiskVerdict::Allow { qty: projection }
                        }
                        _ => RiskVerdict::Deny { reason: DenyReason::UnknownState {
                            detail: "canonical risk quantity exceeds or cannot represent the request".into(),
                        } },
                    }
                }
                engine_types::risk::PortfolioRiskVerdict::Deny { reason } => {
                    RiskVerdict::Deny { reason }
                }
            }
        } else {
            self.risk.assess(&intent, &self.books.account)
        };
        let verdict = durable_risk_verdict(verdict, intent.qty, false);
        let allowed_qty = match &verdict {
            RiskVerdict::Allow { qty } => canonical_allowed.unwrap_or(
                engine_types::numeric::Exact::from_legacy_f64(*qty)
                    .map_err(|error| EngineError::State(error.to_string()))?,
            ),
            RiskVerdict::Deny { reason } => {
                let reason = format!("{reason:?}");
                self.wal.append(&WalRecord::Verdict {
                    client_order_id: None,
                    verdict,
                })?;
                tracing::info!(tag = %intent.tag, reason, "risk refused the order");
                self.tell_refused(&intent, &reason, client_order_id.as_deref())?;
                return Ok(None);
            }
        };

        // A refused intent never consumes an order identity.
        let client_order_id = match client_order_id {
            Some(id) => id,
            None => self.mint_id()?,
        };
        self.wal.append(&WalRecord::Verdict {
            client_order_id: Some(client_order_id.clone()),
            verdict,
        })?;

        Ok(Some(RiskApprovedIntent {
            intent,
            client_order_id,
            allowed_qty,
            work,
        }))
    }

    fn quantize_approved_order(
        &mut self,
        approval: RiskApprovedIntent,
    ) -> Result<Option<LegalOrder>, EngineError> {
        let RiskApprovedIntent {
            ref intent,
            ref client_order_id,
            ref allowed_qty,
            ..
        } = approval;
        let allowed_qty = allowed_qty
            .to_f64()
            .map_err(|error| EngineError::State(error.to_string()))?;
        // The risk kernel requires a position-opening intent to carry a stop.
        // A venue that keeps no stop of its own would leave that rule
        // unenforced without ever saying so: the order goes out, the log
        // records a stop, and nothing at the venue is watching the position.
        // An exit sheds its stop below in any case, so it is not held back.
        if intent.stop.is_some() && !intent.reduce_only && !self.venue.caps().native_position_stop {
            self.refuse(
                client_order_id,
                intent,
                "the intent carries a stop and this venue keeps none",
            )?;
            return Ok(None);
        }

        if let Some(spec) = self.instrument_specs.get(&intent.symbol).cloned() {
            return self.quantize_exact_order(approval, &spec);
        }
        if self.require_exact_instruments {
            self.refuse(
                client_order_id,
                intent,
                "exact instrument metadata is unavailable for this symbol",
            )?;
            return Ok(None);
        }

        let Some(rule) = self
            .books
            .rules
            .get(intent.symbol.0 as usize)
            .copied()
            .flatten()
        else {
            self.refuse(
                client_order_id,
                intent,
                "no instrument rule for this symbol",
            )?;
            return Ok(None);
        };
        let kind = match intent.kind {
            OrderKind::Market => OrderKind::Market,
            OrderKind::Limit { px, tif } => OrderKind::Limit {
                px: quantize::quantize_px(px, intent.side, &rule),
                tif,
            },
        };
        let mut held = self
            .books
            .account
            .positions
            .iter()
            .filter(|position| position.symbol == intent.symbol && position.qty > 0.0);
        let held_position = held.next().map(|position| (position.side, position.qty));
        let one_position = held.next().is_none();
        let close_position_candidate = intent.reduce_only
            && matches!(intent.kind, OrderKind::Market)
            && self.venue.caps().close_position_below_minimum
            && one_position
            && held_position.is_some_and(|(side, qty)| {
                let tolerance = rule.qty_step.max(1e-12) * 1e-9;
                side == intent.side.flipped() && (allowed_qty - qty).abs() <= tolerance
            });
        let held_qty = held_position.map(|(_, qty)| qty).unwrap_or(0.0);
        let close_below_minimum_qty = close_position_candidate && held_qty + 1e-12 < rule.min_qty;
        let mut close_below_minimum_value = false;
        if close_position_candidate {
            if let Some(reference_px) = self.reference_px(intent.symbol, &kind) {
                close_below_minimum_value = held_qty * reference_px + 1e-9 < rule.min_notional;
            }
        }
        let close_position =
            close_position_candidate && (close_below_minimum_qty || close_below_minimum_value);
        let qty = if close_position {
            // Bybit receives qty=0 for this request and closes the whole venue
            // position. The WAL keeps the actual held quantity so its fill can
            // be validated and accounted without inventing one venue step.
            held_qty
        } else if let Some(qty) = quantize::quantize_qty(allowed_qty, &rule) {
            qty
        } else {
            self.refuse(
                client_order_id,
                intent,
                &format!(
                    "{allowed_qty} does not reach the smallest tradable size ({} step, {} minimum)",
                    rule.qty_step, rule.min_qty
                ),
            )?;
            return Ok(None);
        };
        if let Some(reference_px) = self.reference_px(intent.symbol, &kind) {
            let notional = qty * reference_px;
            if notional + 1e-9 < rule.min_notional && !close_position {
                self.refuse(
                    client_order_id,
                    intent,
                    &format!(
                        "{notional:.4} is under the venue's smallest order value ({})",
                        rule.min_notional
                    ),
                )?;
                return Ok(None);
            }
        }

        let request = OrderRequest {
            client_order_id: client_order_id.clone(),
            strategy: intent.strategy,
            symbol: intent.symbol,
            side: intent.side,
            qty,
            kind,
            // The venue rejects a reduce-only order that carries stop
            // fields, so an exit sheds its stop here — the log records what
            // is actually sent. An entry's stop is quantized against the
            // instrument tick, rounded toward triggering sooner.
            stop: if intent.reduce_only {
                None
            } else {
                intent.stop.map(|s| StopSpec {
                    trigger_px: quantize::quantize_px(s.trigger_px, intent.side.flipped(), &rule),
                })
            },
            reduce_only: intent.reduce_only,
            exact_terms: None,
            sleeve_effect: if intent.reduce_only {
                Some(engine_types::orders::SleeveOrderEffect::Reduce)
            } else {
                intent
                    .stop
                    .map(|stop| engine_types::orders::SleeveOrderEffect::Increase {
                        stop: StopSpec {
                            trigger_px: quantize::quantize_px(
                                stop.trigger_px,
                                intent.side.flipped(),
                                &rule,
                            ),
                        },
                    })
            },
            close_position,
        };

        Ok(Some(LegalOrder { request, approval }))
    }

    fn quantize_exact_order(
        &mut self,
        approval: RiskApprovedIntent,
        spec: &engine_types::numeric::ExactInstrumentSpec,
    ) -> Result<Option<LegalOrder>, EngineError> {
        use engine_types::order_terms::{
            quantize_with_exact_prices, strategy_decimal, OrderInputPolicy, QuantityPolicy,
        };
        use engine_types::orders::SleeveOrderEffect;
        let intent = &approval.intent;
        let reference = self.reference_px(intent.symbol, &intent.kind);
        let stop = if intent.reduce_only {
            None
        } else {
            intent.stop
        };
        let mut quantity = approval.allowed_qty.clone();
        if intent.reduce_only {
            let maximum = if matches!(intent.kind, OrderKind::Market) {
                &spec.max_market_qty
            } else {
                &spec.max_qty
            };
            if let Some(maximum) = maximum {
                quantity = quantity.min(maximum.clone());
            }
        }
        let input_policy = if intent.exact_quantity.is_some() {
            OrderInputPolicy::CanonicalPortfolio
        } else {
            OrderInputPolicy::StrategyShortestDecimal
        };
        let mut policy = QuantityPolicy::Normal;
        let mut terms = quantize_with_exact_prices(
            spec,
            intent.side,
            (
                quantity.clone(),
                input_policy,
                intent.exact_prices.as_deref(),
            ),
            intent.kind,
            stop,
            reference,
            policy,
        );
        if terms.is_err()
            && intent.reduce_only
            && matches!(intent.kind, OrderKind::Market)
            && self.venue.caps().close_position_below_minimum
        {
            let held: Vec<_> = self
                .books
                .account
                .positions
                .iter()
                .filter(|p| p.symbol == intent.symbol)
                .collect();
            if let [position] = held.as_slice() {
                let exact_match = position.quantity().is_ok_and(|held| quantity == held);
                let below_minimum = position.quantity().is_ok_and(|qty| {
                    spec.market_min_qty.as_ref().is_some_and(|min| &qty < min)
                        || reference
                            .and_then(|px| strategy_decimal(px).ok())
                            .is_some_and(|px| {
                                spec.min_notional
                                    .as_ref()
                                    .is_some_and(|min| &qty * &px < *min)
                            })
                });
                if exact_match && below_minimum && position.side == intent.side.flipped() {
                    policy = QuantityPolicy::CloseEntirePosition;
                    terms = quantize_with_exact_prices(
                        spec,
                        intent.side,
                        (quantity, input_policy, None),
                        intent.kind,
                        None,
                        reference,
                        policy,
                    );
                }
            }
        }
        let terms = match terms {
            Ok(terms) => terms,
            Err(error) => {
                self.refuse(
                    &approval.client_order_id,
                    intent,
                    &format!("exact instrument legality: {error}"),
                )?;
                return Ok(None);
            }
        };
        let mut request = OrderRequest {
            client_order_id: approval.client_order_id.clone(),
            strategy: intent.strategy,
            symbol: intent.symbol,
            side: intent.side,
            qty: approval
                .allowed_qty
                .to_f64()
                .map_err(|error| EngineError::State(error.to_string()))?,
            kind: intent.kind,
            stop,
            reduce_only: intent.reduce_only,
            exact_terms: None,
            sleeve_effect: if intent.reduce_only {
                Some(SleeveOrderEffect::Reduce)
            } else {
                stop.map(|stop| SleeveOrderEffect::Increase { stop })
            },
            close_position: policy == QuantityPolicy::CloseEntirePosition,
        };
        terms
            .apply_projection(&mut request)
            .map_err(|error| EngineError::State(error.to_string()))?;
        if request
            .exact_terms
            .as_ref()
            .expect("canonical quantized order")
            .quantity
            > approval.allowed_qty
        {
            return Err(EngineError::State(
                "exact quantity projection enlarged the risk approval".into(),
            ));
        }
        let Some(request) = self.translate_physical_order(request, &approval, spec)? else {
            return Ok(None);
        };
        Ok(Some(LegalOrder { request, approval }))
    }

    fn translate_physical_order(
        &mut self,
        mut request: OrderRequest,
        approval: &RiskApprovedIntent,
        spec: &engine_types::numeric::ExactInstrumentSpec,
    ) -> Result<Option<OrderRequest>, EngineError> {
        let planned = self.physical_order_plan(&request, spec, None);
        let plan = match planned {
            Ok(plan) => plan,
            Err(reason) => {
                self.refuse(
                    &approval.client_order_id,
                    &approval.intent,
                    &format!("physical protection: {reason}"),
                )?;
                return Ok(None);
            }
        };
        request.reduce_only = plan.reduce_only;
        if request.close_position && !request.reduce_only {
            self.refuse(
                &approval.client_order_id,
                &approval.intent,
                "whole-position close does not certainly reduce the physical position",
            )?;
            return Ok(None);
        }
        let terms = request.exact_terms.take().ok_or_else(|| {
            EngineError::State("physical translation requires exact order terms".into())
        })?;
        terms
            .with_physical_stop(plan.native_stop.map(|stop| stop.trigger_price))
            .and_then(|terms| terms.apply_projection(&mut request))
            .map_err(|e| EngineError::State(e.to_string()))?;
        Ok(Some(request))
    }

    pub(super) fn physical_order_plan(
        &mut self,
        request: &OrderRequest,
        spec: &engine_types::numeric::ExactInstrumentSpec,
        excluding: Option<&str>,
    ) -> Result<crate::portfolio_protection::ProtectionPlan, String> {
        let interval = match excluding {
            Some(id) => self.risk.physical_exposure_interval_excluding(
                id,
                request.symbol,
                &self.books.account,
            ),
            None => self
                .risk
                .physical_exposure_interval(request.symbol, &self.books.account),
        }
        .map_err(|reason| format!("{reason:?}"))?;
        let reference = self
            .reference_px(request.symbol, &OrderKind::Market)
            .or_else(|| self.reference_px(request.symbol, &request.kind))
            .ok_or("no reference price for physical protection")?;
        let reference =
            engine_types::order_terms::strategy_decimal(reference).map_err(|e| e.to_string())?;
        let mut stops = Vec::new();
        for order in self.books.orders.in_flight().into_iter().filter(|order| {
            order.request.symbol == request.symbol
                && excluding != Some(order.request.client_order_id.as_str())
                && !order.request.is_sleeve_reduction()
        }) {
            if let Some(stop) = order
                .request
                .exact_terms
                .as_ref()
                .and_then(|terms| terms.stop_trigger_price.clone())
            {
                stops.push((order.request.side, stop));
            } else if let Some(stop) = order.request.sleeve_stop() {
                stops.push((
                    order.request.side,
                    engine_types::numeric::Exact::from_legacy_f64(stop.trigger_px)
                        .map_err(|e| e.to_string())?,
                ));
            }
        }
        for position in self
            .books
            .account
            .positions
            .iter()
            .filter(|position| position.symbol == request.symbol && position.stop_attached)
        {
            stops.push((
                position.side,
                engine_types::numeric::Exact::from_legacy_f64(position.stop_px)
                    .map_err(|e| e.to_string())?,
            ));
        }
        let plan = crate::portfolio_protection::plan(
            &self.books.attribution.snapshot(),
            request,
            interval,
            spec,
            &reference,
            stops,
        )?;
        if !plan.reduce_only {
            if self.stop_repairs_pending.contains(&request.symbol) {
                return Err("physical growth is waiting for native stop repair".into());
            }
            if !self.private_stream_ready
                || !self.may_open
                || !self.dispatches.unresolved.is_empty()
            {
                return Err(
                    "physical growth requires reconciled private state and resolved order outcomes"
                        .into(),
                );
            }
            if self.symbol_admission.refresh_required()
                || !self
                    .symbol_admission
                    .listed(self.books.market.table.name(request.symbol))
            {
                return Err("physical growth requires a current listed instrument".into());
            }
            let quote_ns = self.books.market.quote(request.symbol).recv_ns;
            if quote_ns == 0 || clock::now_ns().saturating_sub(quote_ns) > self.max_quote_age_ns {
                return Err("physical growth requires a fresh quote".into());
            }
        }
        Ok(plan)
    }

    async fn confirm_order_protection(
        &mut self,
        legal: LegalOrder,
        batch_protection: &mut std::collections::HashMap<(SymbolId, Side), f64>,
    ) -> Result<Option<ProtectedOrder>, EngineError> {
        let request = &legal.request;
        let intent = &legal.approval.intent;
        let client_order_id = &legal.approval.client_order_id;
        // Before the durable record, because a leverage that could not be set
        // means this order must not go at all — and an OrderSent record is
        // the engine saying it is about to put one on the wire.
        //
        // Entries only. An exit at the wrong leverage is still an exit, and
        // making it wait on a round trip would be the wrong trade.
        if !intent.reduce_only {
            if let Some(want) = intent.leverage {
                if let Err(reason) = self.ensure_leverage(request.symbol, want).await {
                    self.refuse(client_order_id, intent, &reason)?;
                    return Ok(None);
                }
            }
        }

        // Bybit's Full TP/SL belongs to the entire one-way position. A later
        // same-side fill with a looser stop would therefore weaken units that
        // were already protected. Hold each same-side batch chain against
        // both the fresh account view and durable fill-owned intent; only
        // equal or tighter protection may reach the wire.
        if let Some(stop) = request.stop.filter(|_| !request.reduce_only) {
            let key = stop_key(request.symbol, request.side);
            let tolerance = self
                .books
                .rules
                .get(request.symbol.0 as usize)
                .and_then(|rule| rule.as_ref())
                .map(|rule| rule.tick_size / 2.0)
                .unwrap_or(1e-9);
            if let Some(protected) = batch_protection.get(&key).copied() {
                if stop_is_looser(request.side, stop.trigger_px, protected, tolerance) {
                    self.refuse(
                        client_order_id,
                        intent,
                        &format!(
                            "stop {} would loosen the whole {:?} position from {}",
                            stop.trigger_px, request.side, protected
                        ),
                    )?;
                    return Ok(None);
                }
                batch_protection
                    .insert(key, tighter_stop(request.side, protected, stop.trigger_px));
            } else {
                batch_protection.insert(key, stop.trigger_px);
            }
        }

        Ok(Some(ProtectedOrder(legal)))
    }

    fn commit_prepared_order(
        &mut self,
        protected: ProtectedOrder,
        decided_ns: u64,
        origin_ns: u64,
    ) -> Result<PreparedOrder, EngineError> {
        let LegalOrder { request, approval } = protected.0;
        let RiskApprovedIntent {
            mut intent,
            client_order_id,
            work,
            ..
        } = approval;
        let qty = request.qty;
        intent.qty = request.qty;
        intent.exact_quantity = request
            .exact_terms
            .as_ref()
            .map(|terms| Box::new(terms.quantity.clone()));
        intent.exact_prices = request.canonical_intent_prices();
        intent.kind = request.kind;
        intent.stop = request.sleeve_stop();
        // Appended before reservation and venue dispatch. One disk barrier
        // covers every accepted sibling in the group.
        let mut dispatch_intent = intent.clone();
        dispatch_intent.decided_ns = decided_ns;
        let dispatch = engine_types::order_dispatch::QueuedOrderDispatch {
            intent: dispatch_intent.clone(),
            origin_ns,
        };
        let sent_record = WalRecord::OrderSent {
            dispatch: Some(Box::new(dispatch)),
            request: request.clone(),
            wire_ns: clock::now_ns(),
            // `M0`. Read here rather than at the fill because this is the only
            // moment it exists: a worked entry can rest for a minute, and by
            // the time it fills the price it was decided against is gone.
            // Zero when the book was unreadable, which makes every arrival
            // number for this order missing rather than flattering.
            arrival_mid: self.decision_mid(request.symbol),
        };
        self.portfolio_controls
            .validate_engine_order(&request)
            .map_err(EngineError::State)?;
        self.wal.append(&sent_record)?;
        self.dispatches.orders.insert(
            client_order_id.clone(),
            engine_types::order_dispatch::OrderDispatchState {
                request: request.clone(),
                intent: dispatch_intent,
                origin_ns,
                phase: engine_types::order_dispatch::OrderDispatchPhase::Queued,
            },
        );
        self.books
            .orders
            .try_apply(&sent_record)
            .map_err(EngineError::State)?;
        if let Some(owner) = request.sleeve_owner() {
            self.books.registry.own(&client_order_id, owner);
        }
        // The engine's own note of what just went out, at the size that
        // actually went — strategies read it back as `ctx.in_flight`, so the
        // window between a fill and the next account reading cannot look flat.
        if request.is_portfolio_reduction() {
            // Its real fills are allocated across the contributing sleeves.
        } else if request.is_sleeve_reduction() {
            self.books.covers.register_reduce(
                intent.strategy,
                request.symbol,
                request.side,
                qty,
                &self.books.account,
            );
        } else {
            self.books.covers.register(
                intent.strategy,
                request.symbol,
                request.side,
                qty,
                &self.books.account,
            );
        }
        self.risk
            .register_order_with_account(&client_order_id, &intent, qty, &self.books.account);
        self.orders_sent += 1;

        // Start working it from the price that is actually resting — the
        // quantized one, not the one the planner asked for.
        if let (Some(policy), OrderKind::Limit { px, .. }) = (work, request.kind) {
            let mid = self.decision_mid(request.symbol);
            let state = working::plan::WorkState::new(request.side, px, mid, clock::now_ns());
            self.working
                .take_on(&client_order_id, request.symbol, policy, state);
        }

        Ok(PreparedOrder {
            intent,
            request,
            decided_ns,
            origin_ns,
        })
    }

    pub(super) async fn process_intents(
        &mut self,
        intents: Vec<(Intent, Option<String>)>,
        origin_ns: u64,
    ) -> Result<bool, EngineError> {
        if intents.len() > MAX_ORDERS_PER_BATCH {
            return Err(EngineError::State(format!(
                "placement batch has {} orders; hard maximum is {MAX_ORDERS_PER_BATCH}",
                intents.len()
            )));
        }
        // Leverage is venue-global per symbol. If two siblings require
        // different valid leverage values, setting A and then B before the
        // concurrent send would put A on the wire at B despite having been
        // sized and approved at A. There is no safe ordering once both are
        // meant to become live together, so refuse every opening sibling for
        // that symbol. Reduce-only exits still flow and never change leverage.
        let mut leverage_by_symbol = std::collections::HashMap::new();
        let mut leverage_conflicts = std::collections::HashSet::new();
        for (intent, _) in &intents {
            let Some(want) = intent
                .leverage
                .filter(|value| value.is_finite() && *value > 0.0)
            else {
                continue;
            };
            if intent.reduce_only {
                continue;
            }
            match leverage_by_symbol.insert(intent.symbol, want) {
                Some(previous) if previous != want => {
                    leverage_conflicts.insert(intent.symbol);
                }
                _ => {}
            }
        }

        let mut prepared = Vec::with_capacity(intents.len());
        let mut batch_protection = std::collections::HashMap::new();
        for (symbol, stop) in &self.intended_stops {
            batch_protection.insert(stop_key(*symbol, stop.side), stop.trigger_px);
        }
        for (key, trigger_px) in self.books.orders.tightest_opening_stops() {
            let side = key.1;
            batch_protection
                .entry(key)
                .and_modify(|protected| *protected = tighter_stop(side, *protected, trigger_px))
                .or_insert(trigger_px);
        }
        for position in &self.books.account.positions {
            if !position.stop_attached || !position.stop_px.is_finite() || position.stop_px <= 0.0 {
                continue;
            }
            let key = stop_key(position.symbol, position.side);
            batch_protection
                .entry(key)
                .and_modify(|protected| {
                    *protected = tighter_stop(position.side, *protected, position.stop_px)
                })
                .or_insert(position.stop_px);
        }
        for (intent, client_order_id) in intents {
            if !intent.reduce_only
                && leverage_conflicts.contains(&intent.symbol)
                // Keep non-finite values out of the WAL. `prepare_intent`
                // owns that refusal and performs it before any append.
                && unreal_number(&intent).is_none()
            {
                self.wal.append(&WalRecord::Intent {
                    intent: intent.clone(),
                })?;
                let reason = "same-symbol sibling batch asks for conflicting leverage values";
                self.wal.append(&WalRecord::Note {
                    source: "leverage".to_string(),
                    text: format!("intent {} refused: {reason}", intent.tag),
                })?;
                tracing::error!(
                    symbol = self.books.market.table.name(intent.symbol),
                    tag = %intent.tag,
                    "refused leverage-conflicting sibling batch"
                );
                self.tell_refused(
                    &intent,
                    "batch_leverage_conflict",
                    client_order_id.as_deref(),
                )?;
                continue;
            }
            if let Some(order) = self
                .prepare_intent(intent, client_order_id, origin_ns, &mut batch_protection)
                .await?
            {
                prepared.push(order);
            }
        }
        if prepared.is_empty() {
            return Ok(false);
        }

        self.queue_order_dispatches(prepared)
    }

    /// Refuse an entry for an engine-level reason. The verdict is written
    /// the way the kernel's would be, so replay and the fills report read
    /// one shape; then the strategy hears the code.
    fn deny_opening(
        &mut self,
        intent: &Intent,
        refusal: OpeningRefusal,
        client_order_id: Option<&str>,
    ) -> Result<(), EngineError> {
        self.wal.append(&WalRecord::Verdict {
            client_order_id: None,
            verdict: RiskVerdict::Deny {
                reason: DenyReason::UnknownState {
                    detail: refusal.detail().to_string(),
                },
            },
        })?;
        tracing::warn!(
            strategy = intent.strategy.0,
            tag = %intent.tag,
            reason = refusal.as_str(),
            "refused: this strategy cannot open exposure"
        );
        self.tell_refused(intent, refusal.as_str(), client_order_id)?;
        Ok(())
    }

    fn refuse(
        &mut self,
        client_order_id: &str,
        intent: &Intent,
        why: &str,
    ) -> Result<(), EngineError> {
        let key = (intent.strategy, intent.symbol, intent.tag.clone());
        let now_ns = clock::now_ns();
        let repeated = self.refusals.get(&key).is_some_and(|last| {
            last.why == why && now_ns.saturating_sub(last.at_ns) < REFUSAL_REPEAT_NS
        });
        if repeated {
            if let Some(last) = self.refusals.get_mut(&key) {
                last.suppressed += 1;
            }
            self.tell_refused(intent, why, Some(client_order_id))?;
            return Ok(());
        }
        let suppressed = self
            .refusals
            .insert(
                key,
                Refusal {
                    why: why.to_string(),
                    at_ns: now_ns,
                    suppressed: 0,
                },
            )
            .map(|last| last.suppressed)
            .unwrap_or(0);
        let also = if suppressed > 0 {
            format!(" (and {suppressed} more like it)")
        } else {
            String::new()
        };
        tracing::warn!(id = client_order_id, tag = %intent.tag, why, suppressed, "order not sent");
        self.wal.append(&WalRecord::Note {
            source: "engine".into(),
            text: format!("{client_order_id} not sent ({}): {why}{also}", intent.tag),
        })?;
        self.tell_refused(intent, why, Some(client_order_id))?;
        Ok(())
    }

    /// Settle the in-flight accounting for an intent that died inside the
    /// engine, then tell the strategy. A refused exit means the covers
    /// describe exposure the account reading says is not there, and left
    /// standing they would re-plan the same doomed exit on every quote.
    pub(super) fn tell_refused(
        &mut self,
        intent: &Intent,
        reason: &str,
        client_order_id: Option<&str>,
    ) -> Result<(), EngineError> {
        // Bookkeeping first, so the strategy woken below already reads the
        // truthful in-flight number. A refused exit drops every cover on the
        // symbol; a refused entry has none to drop, because covers are booked
        // at the send and a refusal never reaches it.
        self.books
            .covers
            .intent_refused(intent.strategy, intent.symbol, intent.reduce_only);
        let event = EngineEvent::IntentRefused {
            symbol: intent.symbol,
            reduce_only: intent.reduce_only,
            reason: reason.to_string(),
        };
        self.deliver_callback_source(intent.strategy, event, client_order_id.map(str::to_owned))
    }

    /// Turn an entry the strategy asked to have worked into the resting limit
    /// it should start as, and say whether it will be worked at all.
    ///
    /// `None` leaves the intent exactly as the strategy wrote it: no policy,
    /// an exit, a symbol with no instrument rule, or a spread too thin for
    /// resting to pay for itself.
    fn plan_resting_entry(&self, intent: &mut Intent) -> Option<WorkPolicy> {
        let rule = self
            .books
            .rules
            .get(intent.symbol.0 as usize)
            .copied()
            .flatten()?;
        let touch = self
            .books
            .market
            .quotes
            .get(intent.symbol.0 as usize)
            .map(working::touch_of)
            .unwrap_or_default();
        match working::plan::opening(intent, touch, &rule) {
            working::plan::Opening::AsWritten => None,
            working::plan::Opening::WorkAsPriced { policy } => Some(policy),
            working::plan::Opening::Rest { px, policy } => {
                // Good-till-cancelled, not post-only. The overnight lab that
                // first measured resting ran post-only into the demo realm's
                // pretend internal liquidity, which flattered it; the numbers
                // this recipe is built on are GTC numbers.
                intent.kind = OrderKind::Limit {
                    px,
                    tif: TimeInForce::Gtc,
                };
                Some(policy)
            }
        }
    }

    /// The mid this order was decided against, or zero when the book was not
    /// two-sided. Only the early cross reads it, and it stays off at zero.
    fn decision_mid(&self, symbol: SymbolId) -> f64 {
        let quote = self.books.market.quote(symbol);
        if quote.bid_px > 0.0 && quote.ask_px > quote.bid_px {
            (quote.bid_px + quote.ask_px) / 2.0
        } else {
            0.0
        }
    }

    pub(super) fn reference_px(&self, symbol: SymbolId, kind: &OrderKind) -> Option<f64> {
        if let OrderKind::Limit { px, .. } = kind {
            return Some(*px);
        }
        let quote = self.books.market.quote(symbol);
        if quote.bid_px > 0.0 && quote.ask_px > 0.0 {
            return Some((quote.bid_px + quote.ask_px) / 2.0);
        }
        let ticker = self.books.market.ticker(symbol);
        [ticker.last_px, ticker.mark_px]
            .into_iter()
            .find(|px| *px > 0.0)
    }

    /// `M0` for an order of ours, off the order ledger. Zero for one the
    /// ledger no longer holds, which makes every arrival number for its fills
    /// missing rather than wrong.
    pub(super) fn arrival_mid_of(&self, client_order_id: &str) -> f64 {
        self.books
            .orders
            .orders
            .get(client_order_id)
            .map(|order| order.arrival_mid)
            .unwrap_or(0.0)
    }
}
