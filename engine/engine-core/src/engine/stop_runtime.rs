use super::*;
use engine_types::numeric::Exact;
use engine_types::order_terms::ExactStopTerms;

#[derive(Clone, Debug)]
pub(crate) struct DurableStop {
    pub symbol: SymbolId,
    pub side: Side,
    pub trigger_px: f64,
    pub exact: Option<ExactStopTerms>,
}

enum NativeStopPlan {
    Satisfied,
    Waiting,
    Required(DurableStop),
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) async fn process_set_stop(
        &mut self,
        caller: Option<StrategyId>,
        symbol: SymbolId,
        trigger_px: f64,
    ) -> Result<(), EngineError> {
        let refuse = |reason: &str| WalRecord::Note {
            source: "engine".into(),
            text: format!("stop on {} not moved to {trigger_px}: {reason}", symbol.0),
        };
        if !trigger_px.is_finite() || trigger_px <= 0.0 {
            self.wal
                .append(&refuse("trigger is not positive and finite"))?;
            return Ok(());
        }
        if let Some(spec) = self.instrument_specs.get(&symbol) {
            let owner = caller.or_else(|| self.books.attribution.sole_owner(symbol));
            let portfolio = self.books.attribution.snapshot();
            let Some(row) = portfolio.positions.iter().find(|row| {
                Some(row.strategy) == owner && row.symbol == symbol && !row.signed_qty.is_zero()
            }) else {
                self.wal
                    .append(&refuse("caller has no canonical sleeve position"))?;
                return Ok(());
            };
            let side = if row.signed_qty.is_positive() {
                Side::Buy
            } else {
                Side::Sell
            };
            let reference = match self.stop_reference(symbol, side) {
                Ok(value) => value,
                Err(reason) => {
                    self.wal.append(&refuse(&reason))?;
                    return Ok(());
                }
            };
            let trigger = Exact::parse_decimal(&trigger_px.to_string())
                .map_err(|e| EngineError::State(e.to_string()))?;
            let terms = match ExactStopTerms::quantize(spec, side, &trigger, &reference) {
                Ok(terms) => terms,
                Err(error) => {
                    self.wal.append(&refuse(&error.to_string()))?;
                    return Ok(());
                }
            };
            if row.stop_px.as_ref().is_some_and(|old| match side {
                Side::Buy => terms.trigger_price < *old,
                Side::Sell => terms.trigger_price > *old,
            }) {
                self.wal.append(&refuse(
                    "the requested stop would loosen this sleeve's protection",
                ))?;
                return Ok(());
            }
            if row.stop_px.as_ref() != Some(&terms.trigger_price) {
                self.append_portfolio_control(WalRecord::SleeveStopSet {
                    strategy: row.strategy,
                    symbol,
                    side,
                    trigger_price: terms.trigger_price,
                    wall_ts_ms: clock::wall_ms(),
                })?;
            }
            match self.native_stop_plan(symbol) {
                Ok(NativeStopPlan::Required(stop)) => {
                    self.stop_repairs_pending.insert(symbol);
                    self.queue_native_stops(vec![stop])?;
                }
                Ok(NativeStopPlan::Satisfied) => {
                    self.stop_repairs_pending.remove(&symbol);
                    self.service_portfolio_controls().await?;
                }
                Ok(NativeStopPlan::Waiting) => {
                    self.stop_repairs_pending.insert(symbol);
                    self.service_portfolio_controls().await?;
                }
                Err(reason) => {
                    self.wal.append(&refuse(&reason))?;
                    self.service_portfolio_controls().await?;
                }
            }
            return Ok(());
        }
        if self.require_exact_instruments {
            self.wal
                .append(&refuse("exact instrument metadata is unavailable"))?;
            return Ok(());
        }
        let rows: Vec<_> = self
            .books
            .account
            .positions
            .iter()
            .filter(|p| p.symbol == symbol && p.qty > 0.0)
            .collect();
        let [position] = rows.as_slice() else {
            self.wal.append(&refuse(
                "the latest account view has no unambiguous held position",
            ))?;
            return Ok(());
        };
        if !position.qty.is_finite() {
            self.wal
                .append(&refuse("the latest position quantity is unreadable"))?;
            return Ok(());
        }
        let side = position.side;
        let baseline = self
            .intended_stops
            .get(&symbol)
            .filter(|s| s.side == side)
            .map(|s| s.trigger_px)
            .into_iter()
            .chain((position.stop_attached && position.stop_px > 0.0).then_some(position.stop_px))
            .reduce(|a, b| tighter(side, a, b));
        if baseline.is_some_and(|old| stop_is_looser(side, trigger_px, old, 0.0)) {
            self.wal
                .append(&refuse("the requested stop would loosen protection"))?;
            return Ok(());
        }
        let stop = DurableStop {
            symbol,
            side,
            trigger_px,
            exact: None,
        };
        if self.stop_is_confirmed(&stop) {
            return Ok(());
        }
        self.queue_native_stops(vec![stop])
    }

    fn stop_reference(&self, symbol: SymbolId, side: Side) -> Result<Exact, String> {
        let quote = self.books.market.quote(symbol);
        let now = clock::now_ns();
        if quote.recv_ns == 0
            || quote.recv_ns > now
            || now.saturating_sub(quote.recv_ns) > self.max_quote_age_ns
        {
            return Err("current executable reference is stale".into());
        }
        let px = if side == Side::Buy {
            quote.bid_px
        } else {
            quote.ask_px
        };
        if !px.is_finite() || px <= 0.0 {
            return Err("current executable reference is unavailable".into());
        }
        Exact::from_legacy_f64(px).map_err(|e| e.to_string())
    }

    fn native_stop_plan(&mut self, symbol: SymbolId) -> Result<NativeStopPlan, String> {
        if !self.private_stream_ready
            || self.books.account.observed_ns == 0
            || clock::now_ns().saturating_sub(self.books.account.observed_ns)
                > self.refresh_after_ns.saturating_mul(2)
        {
            return Ok(NativeStopPlan::Waiting);
        }
        let interval = self
            .risk
            .physical_exposure_interval(symbol, &self.books.account)
            .map_err(|e| format!("physical exposure unavailable: {e:?}"))?;
        let side = if !interval.low().is_negative() && interval.high().is_positive() {
            Side::Buy
        } else if !interval.high().is_positive() && interval.low().is_negative() {
            Side::Sell
        } else if interval.low().is_zero() && interval.high().is_zero() {
            return Ok(NativeStopPlan::Satisfied);
        } else {
            return Ok(NativeStopPlan::Waiting);
        };
        let spec = self
            .instrument_specs
            .get(&symbol)
            .ok_or("exact instrument metadata is unavailable")?;
        let portfolio = self.books.attribution.snapshot();
        let mut candidates = Vec::new();
        for row in portfolio.positions.iter().filter(|row| {
            row.symbol == symbol
                && !row.signed_qty.is_zero()
                && row.signed_qty.is_positive() == (side == Side::Buy)
        }) {
            candidates.push(
                row.stop_px
                    .clone()
                    .ok_or("surviving sleeve has no durable stop")?,
            );
        }
        for order in self.books.orders.in_flight().into_iter().filter(|order| {
            order.request.symbol == symbol
                && order.request.side == side
                && !order.request.is_sleeve_reduction()
        }) {
            let stop = if let Some(terms) = &order.request.exact_terms {
                terms
                    .stop_trigger_price
                    .clone()
                    .ok_or("working sleeve has no exact durable stop")?
            } else {
                let legacy = order
                    .request
                    .sleeve_stop()
                    .ok_or("working sleeve has no durable stop")?;
                Exact::from_legacy_f64(legacy.trigger_px).map_err(|e| e.to_string())?
            };
            candidates.push(stop);
        }
        let trigger = candidates
            .into_iter()
            .reduce(|a, b| match side {
                Side::Buy => a.max(b),
                Side::Sell => a.min(b),
            })
            .ok_or("physical position has no durable sleeve stop")?;
        let projected = trigger.to_f64().map_err(|e| e.to_string())?;
        if self.stop_price_is_confirmed(symbol, side, projected, Some(&trigger)) {
            return Ok(NativeStopPlan::Satisfied);
        }
        let reference = match self.stop_reference(symbol, side) {
            Ok(reference) => reference,
            Err(_) => return Ok(NativeStopPlan::Waiting),
        };
        let terms = ExactStopTerms::quantize(spec, side, &trigger, &reference)
            .map_err(|e| e.to_string())?;
        Ok(NativeStopPlan::Required(DurableStop {
            symbol,
            side,
            trigger_px: terms.trigger_price.to_f64().map_err(|e| e.to_string())?,
            exact: Some(terms),
        }))
    }

    fn stop_is_confirmed(&self, stop: &DurableStop) -> bool {
        self.stop_price_is_confirmed(
            stop.symbol,
            stop.side,
            stop.trigger_px,
            stop.exact.as_ref().map(|terms| &terms.trigger_price),
        )
    }

    fn stop_price_is_confirmed(
        &self,
        symbol: SymbolId,
        side: Side,
        projected: f64,
        exact: Option<&Exact>,
    ) -> bool {
        let venue = self
            .books
            .account
            .positions
            .iter()
            .filter(|p| p.symbol == symbol && p.side == side && p.qty > 0.0)
            .any(|p| {
                if !p.stop_attached || !p.stop_px.is_finite() || p.stop_px <= 0.0 {
                    return false;
                }
                if let Some(observed) = p.exact_stop_px.as_deref() {
                    if !observed.is_positive() || observed.to_f64().ok() != Some(p.stop_px) {
                        return false;
                    }
                    let legacy;
                    let target = if let Some(target) = exact {
                        target
                    } else {
                        let Ok(target) = Exact::from_legacy_f64(projected) else {
                            return false;
                        };
                        legacy = target;
                        &legacy
                    };
                    return match side {
                        Side::Buy => observed >= target,
                        Side::Sell => observed <= target,
                    };
                }
                !stop_is_looser(side, p.stop_px, projected, 0.0)
            });
        if venue {
            return true;
        }
        if let Some(target) = exact {
            self.confirmed_native_stops
                .get(&symbol)
                .is_some_and(|known| {
                    known.position_side == side
                        && match side {
                            Side::Buy => known.trigger_price >= *target,
                            Side::Sell => known.trigger_price <= *target,
                        }
                })
        } else {
            self.confirmed_stop_moves
                .get(&symbol)
                .is_some_and(|known| known.side == side && known.trigger_px == projected)
        }
    }

    fn queue_native_stops(&mut self, stops: Vec<DurableStop>) -> Result<(), EngineError> {
        if self.dispatches.write.is_some() {
            return Err(EngineError::State(
                "stop intent overtook dispatch durability".into(),
            ));
        }
        let stops: Vec<_> = stops
            .into_iter()
            .filter(|stop| {
                !self.stop_is_confirmed(stop) && !self.busy_symbols.contains_key(&stop.symbol)
            })
            .collect();
        if stops.is_empty() {
            return Ok(());
        }
        for stop in &stops {
            let covered = stop.exact.as_ref().is_some_and(|terms| {
                self.intended_stops
                    .get(&stop.symbol)
                    .is_some_and(|old| old.side == stop.side && old.trigger_px == stop.trigger_px)
                    && self
                        .books
                        .attribution
                        .snapshot()
                        .positions
                        .iter()
                        .any(|row| {
                            row.symbol == stop.symbol
                                && !row.signed_qty.is_zero()
                                && row.signed_qty.is_positive() == (stop.side == Side::Buy)
                                && row.stop_px.as_ref() == Some(&terms.trigger_price)
                        })
            });
            if !covered {
                self.wal.append(&WalRecord::StopSet {
                    symbol: stop.symbol,
                    trigger_px: stop.trigger_px,
                    wall_ts_ms: clock::wall_ms(),
                })?;
            }
            self.intended_stops.insert(
                stop.symbol,
                reconcile::IntendedPositionStop {
                    side: stop.side,
                    trigger_px: stop.trigger_px,
                },
            );
        }
        let barrier = self.begin_dispatch_barrier()?;
        self.portfolio_dirty = false;
        self.dispatches
            .begin(crate::order_dispatch::DispatchWrite::Stop(stops), barrier);
        Ok(())
    }

    pub(super) fn dispatch_durable_stops(
        &mut self,
        stops: Vec<DurableStop>,
    ) -> Result<(), EngineError> {
        let mut changed = Vec::new();
        for mut stop in stops {
            if self.stop_is_confirmed(&stop) {
                continue;
            }
            if stop.exact.is_some() {
                match self.native_stop_plan(stop.symbol) {
                    Ok(NativeStopPlan::Required(current))
                        if current.side == stop.side
                            && current.exact.as_ref().map(|s| &s.trigger_price)
                                == stop.exact.as_ref().map(|s| &s.trigger_price) =>
                    {
                        stop.exact = current.exact;
                    }
                    Ok(NativeStopPlan::Required(current)) => {
                        changed.push(current);
                        continue;
                    }
                    Ok(NativeStopPlan::Satisfied) => {
                        self.stop_repairs_pending.remove(&stop.symbol);
                        continue;
                    }
                    Ok(NativeStopPlan::Waiting) => {
                        self.stop_repairs_pending.insert(stop.symbol);
                        continue;
                    }
                    Err(reason) => {
                        self.wal.append(&WalRecord::Note {
                            source: "stop-supervisor".into(),
                            text: reason,
                        })?;
                        continue;
                    }
                }
            } else if !self
                .books
                .account
                .positions
                .iter()
                .any(|p| p.symbol == stop.symbol && p.side == stop.side && p.qty > 0.0)
            {
                continue;
            }
            let queued_ns = clock::now_ns();
            let command_id =
                self.venue
                    .dispatch_stop(stop.symbol, stop.trigger_px, stop.exact.clone())?;
            self.mark_symbols_busy([stop.symbol]);
            self.pending_mutations
                .insert(command_id, PendingMutation::SetStop { stop, queued_ns });
        }
        if !changed.is_empty() {
            self.queue_native_stops(changed)?;
        }
        Ok(())
    }

    pub(super) fn complete_stop(
        &mut self,
        stop: DurableStop,
        reply: Result<(), VenueError>,
    ) -> Result<(), EngineError> {
        match reply {
            Ok(()) => {
                self.wal.append(&WalRecord::Note {
                    source: "stop-supervisor".into(),
                    text: format!(
                        "restored {} {:?} position stop to durable level {}",
                        self.books.market.table.name(stop.symbol),
                        stop.side,
                        stop.trigger_px
                    ),
                })?;
                self.confirmed_stop_moves.insert(
                    stop.symbol,
                    reconcile::IntendedPositionStop {
                        side: stop.side,
                        trigger_px: stop.trigger_px,
                    },
                );
                if let Some(terms) = stop.exact {
                    self.confirmed_native_stops.insert(stop.symbol, terms);
                    if matches!(
                        self.native_stop_plan(stop.symbol),
                        Ok(NativeStopPlan::Satisfied)
                    ) {
                        self.stop_repairs_pending.remove(&stop.symbol);
                    }
                }
            }
            Err(error) => {
                let finding = format!(
                    "{}: failed to restore {:?} position stop {}: {error}",
                    self.books.market.table.name(stop.symbol),
                    stop.side,
                    stop.trigger_px
                );
                self.wal.append(&WalRecord::Note {
                    source: "stop-supervisor".into(),
                    text: finding.clone(),
                })?;
                let protected = self.books.account.positions.iter().any(|p| {
                    p.symbol == stop.symbol
                        && p.side == stop.side
                        && p.qty > 0.0
                        && p.stop_attached
                        && p.stop_px.is_finite()
                        && p.stop_px > 0.0
                });
                if !protected {
                    self.may_open = false;
                    self.portfolio_dirty = true;
                    self.wal.append(&WalRecord::Reconciled {
                        wall_ts_ms: clock::wall_ms(),
                        findings: vec![finding],
                        may_open: false,
                    })?;
                    if let Some(terms) = stop.exact {
                        self.start_portfolio_emergency(stop.symbol, terms.reference_price, engine_types::portfolio_control::PortfolioEmergencyReason::ProtectionUnavailable)?;
                    }
                }
            }
        }
        self.release_symbols([stop.symbol]);
        Ok(())
    }

    pub(super) async fn enforce_position_stop_intent(&mut self) -> Result<(), EngineError> {
        if self.dispatches.write.is_some() {
            return Ok(());
        }
        let symbols: std::collections::BTreeSet<_> = self
            .books
            .account
            .positions
            .iter()
            .filter(|p| p.qty > 0.0)
            .map(|p| p.symbol)
            .chain(self.stop_repairs_pending.iter().copied())
            .collect();
        let mut repairs = Vec::new();
        let mut failures = Vec::new();
        for symbol in symbols {
            if self.busy_symbols.contains_key(&symbol) {
                continue;
            }
            if self.instrument_specs.contains_key(&symbol) {
                match self.native_stop_plan(symbol) {
                    Ok(NativeStopPlan::Required(stop)) => {
                        self.stop_repairs_pending.insert(symbol);
                        repairs.push(stop);
                    }
                    Ok(NativeStopPlan::Waiting) => {
                        self.stop_repairs_pending.insert(symbol);
                    }
                    Ok(NativeStopPlan::Satisfied) => {
                        self.stop_repairs_pending.remove(&symbol);
                    }
                    Err(reason) => {
                        self.stop_repairs_pending.insert(symbol);
                        failures.push(format!("{}: {reason}", symbol.0));
                    }
                }
            } else if let Some(position) = self
                .books
                .account
                .positions
                .iter()
                .find(|p| p.symbol == symbol && p.qty > 0.0)
            {
                if let Some(stop) = self
                    .intended_stops
                    .get(&symbol)
                    .filter(|s| s.side == position.side)
                {
                    repairs.push(DurableStop {
                        symbol,
                        side: position.side,
                        trigger_px: stop.trigger_px,
                        exact: None,
                    });
                } else if !position.stop_attached
                    || !position.stop_px.is_finite()
                    || position.stop_px <= 0.0
                {
                    failures.push(format!(
                        "{}: held position has no venue stop and no fill-owned durable stop intent",
                        symbol.0
                    ));
                }
            }
        }
        if !failures.is_empty() {
            self.may_open = false;
            self.portfolio_dirty = true;
            self.wal.append(&WalRecord::Reconciled {
                wall_ts_ms: clock::wall_ms(),
                findings: failures,
                may_open: false,
            })?;
        }
        repairs.truncate(MAX_ORDERS_PER_BATCH);
        self.queue_native_stops(repairs)?;
        self.service_portfolio_controls().await
    }
}

fn tighter(side: Side, a: f64, b: f64) -> f64 {
    match side {
        Side::Buy => a.max(b),
        Side::Sell => a.min(b),
    }
}
