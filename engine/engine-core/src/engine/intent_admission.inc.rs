impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// Judge and reserve one sibling, appending its send record to the WAL.
    /// The caller starts one barrier for the accepted group and dispatches
    /// while that barrier runs.
    async fn prepare_intent(
        &mut self,
        intent: Intent,
        origin_ns: u64,
        batch_protection: &mut std::collections::HashMap<(u16, bool), f64>,
    ) -> Result<Option<PreparedOrder>, EngineError> {
        let decided_ns = if intent.decided_ns > 0 {
            intent.decided_ns
        } else {
            clock::now_ns()
        };
        self.ledger
            .record(Segment::Decide, decided_ns.saturating_sub(origin_ns));

        // A non-finite number would be written to the log as null and stop
        // the next boot's replay dead, so it is refused before any append.
        if let Some(what) = unreal_number(&intent) {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "intent {} refused: {what} is not a finite number",
                    intent.tag
                ),
            })?;
            tracing::error!(tag = %intent.tag, what, "intent carries an unreal number");
            self.tell_refused(&intent, "unreal_number");
            return Ok(None);
        }

        // The strategy's own words, work policy included, before the engine
        // touches anything.
        self.wal.append(&WalRecord::Intent {
            intent: intent.clone(),
        })?;

        // A REST account read cannot replace a private-stream continuity
        // proof: it may predate a fill that the disconnected stream missed.
        // Reconnect handling refreshes the view and recovers execution
        // history before setting this bit again.
        if !self.private_stream_ready && !intent.reduce_only {
            let verdict = RiskVerdict::Deny {
                reason: DenyReason::UnknownState {
                    detail: "private account stream has not completed gap recovery".to_string(),
                },
            };
            self.wal.append(&WalRecord::Verdict {
                client_order_id: None,
                verdict,
            })?;
            tracing::warn!(tag = %intent.tag, "refused: private account stream is not ready");
            self.tell_refused(&intent, "private_stream_unready");
            return Ok(None);
        }

        // Boot found orders or exposure this log cannot account for, which
        // means somebody else is on this account and every number the kernel
        // works from is measuring their trading too. Reducing is still
        // allowed — taking exposure off is safe whoever put it on — but
        // nothing new is added until an operator has looked.
        if !self.may_open && !intent.reduce_only {
            let verdict = RiskVerdict::Deny {
                reason: DenyReason::UnknownState {
                    detail: "boot could not account for what this account holds".to_string(),
                },
            };
            self.wal.append(&WalRecord::Verdict {
                client_order_id: None,
                verdict,
            })?;
            tracing::warn!(
                tag = %intent.tag,
                "refused: this engine is not opening new positions after what boot found"
            );
            self.tell_refused(&intent, "engine_latched");
            return Ok(None);
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
                    symbol = self.market.table.name(intent.symbol),
                    age_ms = age_ns / 1_000_000,
                    never_quoted = quote_ns == 0,
                    "refused: the quote this entry was decided against is too old to open on"
                );
                self.tell_refused(&intent, "stale_quote");
                return Ok(None);
            }
        }

        // An entry the strategy asked to have worked starts as a resting
        // limit instead of crossing the spread. Rewritten here, before the
        // kernel judges it, so the kernel judges the order that is actually
        // sent — and before the id is minted, so a refusal still costs
        // nothing.
        let mut intent = intent;
        let work = self.plan_resting_entry(&mut intent);

        let verdict = {
            let Engine { risk, account, .. } = self;
            risk.assess(&intent, account)
        };
        let verdict = durable_risk_verdict(verdict, intent.qty, false);
        let allowed_qty = match &verdict {
            RiskVerdict::Allow { qty } => *qty,
            RiskVerdict::Deny { reason } => {
                let reason = format!("{reason:?}");
                self.wal.append(&WalRecord::Verdict {
                    client_order_id: None,
                    verdict,
                })?;
                tracing::info!(tag = %intent.tag, reason, "risk refused the order");
                self.tell_refused(&intent, &reason);
                return Ok(None);
            }
        };

        // Minting the id here (not a log write) lets the verdict record name
        // the order it approved; a refused intent never burns an id.
        let client_order_id = self.mint_id();
        self.wal.append(&WalRecord::Verdict {
            client_order_id: Some(client_order_id.clone()),
            verdict,
        })?;

        // The risk kernel requires a position-opening intent to carry a stop.
        // A venue that keeps no stop of its own would leave that rule
        // unenforced without ever saying so: the order goes out, the log
        // records a stop, and nothing at the venue is watching the position.
        // An exit sheds its stop below in any case, so it is not held back.
        if intent.stop.is_some() && !intent.reduce_only && !self.venue.caps().native_position_stop {
            self.refuse(
                &client_order_id,
                &intent,
                "the intent carries a stop and this venue keeps none",
            )?;
            return Ok(None);
        }

        let Some(rule) = self.rules.get(intent.symbol.0 as usize).copied().flatten() else {
            self.refuse(
                &client_order_id,
                &intent,
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
                &client_order_id,
                &intent,
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
                    &client_order_id,
                    &intent,
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
            close_position,
        };

        // Before the durable record, because a leverage that could not be set
        // means this order must not go at all — and an OrderSent record is
        // the engine saying it is about to put one on the wire.
        //
        // Entries only. An exit at the wrong leverage is still an exit, and
        // making it wait on a round trip would be the wrong trade.
        if !intent.reduce_only {
            if let Some(want) = intent.leverage {
                if let Err(reason) = self.ensure_leverage(request.symbol, want).await {
                    self.refuse(&client_order_id, &intent, &reason)?;
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
                .rules
                .get(request.symbol.0 as usize)
                .and_then(|rule| rule.as_ref())
                .map(|rule| rule.tick_size / 2.0)
                .unwrap_or(1e-9);
            if let Some(protected) = batch_protection.get(&key).copied() {
                if stop_is_looser(request.side, stop.trigger_px, protected, tolerance) {
                    self.refuse(
                        &client_order_id,
                        &intent,
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

        // Appended before reservation and venue dispatch. The caller starts
        // one disk barrier covering every accepted sibling, then lets that
        // barrier race the group send.
        let sent_record = WalRecord::OrderSent {
            request: request.clone(),
            wire_ns: clock::now_ns(),
            // `M0`. Read here rather than at the fill because this is the only
            // moment it exists: a worked entry can rest for a minute, and by
            // the time it fills the price it was decided against is gone.
            // Zero when the book was unreadable, which makes every arrival
            // number for this order missing rather than flattering.
            arrival_mid: self.decision_mid(request.symbol),
        };
        self.wal.append(&sent_record)?;
        self.orders.apply(&sent_record);
        self.registry.own(&client_order_id, intent.strategy);
        // The engine's own note of what just went out, at the size that
        // actually went — strategies read it back as `ctx.in_flight`, so the
        // window between a fill and the next account reading cannot look flat.
        if request.reduce_only {
            self.covers.register_reduce(
                intent.strategy,
                request.symbol,
                request.side,
                qty,
                &self.account,
            );
        } else {
            self.covers.register(
                intent.strategy,
                request.symbol,
                request.side,
                qty,
                &self.account,
            );
        }
        self.risk.register_order(&client_order_id, &intent, qty);
        self.orders_sent += 1;

        // Start working it from the price that is actually resting — the
        // quantized one, not the one the planner asked for.
        if let (Some(policy), OrderKind::Limit { px, .. }) = (work, request.kind) {
            let mid = self.decision_mid(request.symbol);
            let state = working::plan::WorkState::new(request.side, px, mid, clock::now_ns());
            self.working
                .take_on(&client_order_id, request.symbol, policy, state);
        }

        Ok(Some(PreparedOrder {
            request,
            decided_ns,
            origin_ns,
        }))
    }

    async fn process_intents(
        &mut self,
        intents: Vec<Intent>,
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
        for intent in &intents {
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
            batch_protection.insert(stop_key(SymbolId(*symbol), stop.side), stop.trigger_px);
        }
        for (key, trigger_px) in self.orders.tightest_opening_stops() {
            let side = if key.1 { Side::Sell } else { Side::Buy };
            batch_protection
                .entry(key)
                .and_modify(|protected| *protected = tighter_stop(side, *protected, trigger_px))
                .or_insert(trigger_px);
        }
        for position in &self.account.positions {
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
        for intent in intents {
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
                    symbol = self.market.table.name(intent.symbol),
                    tag = %intent.tag,
                    "refused leverage-conflicting sibling batch"
                );
                self.tell_refused(&intent, "batch_leverage_conflict");
                continue;
            }
            if let Some(order) = self
                .prepare_intent(intent, origin_ns, &mut batch_protection)
                .await?
            {
                prepared.push(order);
            }
        }
        if prepared.is_empty() {
            return Ok(false);
        }

        // Settle the preceding placement group before opening this one. The
        // current records are already in the operating system's cache; their
        // disk barrier starts below and races the venue dispatch. Private
        // order updates settle it before they advance engine state.
        //
        // What this gives up, stated plainly: a machine that dies inside the
        // barrier can leave an order at the venue that the log does not name.
        // Boot reconciliation sees that as a foreign order and latches
        // opening off, which is the same answer it gives for any order it
        // cannot account for.
        self.settle_barrier()?;
        self.pending_barrier = Some(self.wal.barrier_begin()?);
        let durable_ns = clock::now_ns();
        for order in &prepared {
            self.ledger.record(
                Segment::Durable,
                durable_ns.saturating_sub(order.decided_ns),
            );
        }

        let mut requests = Vec::with_capacity(prepared.len());
        let mut timings = Vec::with_capacity(prepared.len());
        for order in prepared {
            requests.push(order.request);
            timings.push((order.decided_ns, order.origin_ns));
        }

        let queued_ns = clock::now_ns();
        let command_id = self.venue.dispatch_orders(requests.clone())?;
        self.mark_symbols_busy(requests.iter().map(|request| request.symbol));
        self.pending_mutations.insert(
            command_id,
            PendingMutation::Orders {
                requests,
                timings,
                queued_ns,
            },
        );
        Ok(true)
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
            self.tell_refused(intent, why);
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
        self.tell_refused(intent, why);
        Ok(())
    }

    /// Settle the in-flight accounting for an intent that died inside the
    /// engine, then tell the strategy. A refused exit means the covers
    /// describe exposure the account reading says is not there, and left
    /// standing they would re-plan the same doomed exit on every quote.
    fn tell_refused(&mut self, intent: &Intent, reason: &str) {
        // Bookkeeping first, so the strategy woken below already reads the
        // truthful in-flight number. A refused exit drops every cover on the
        // symbol; a refused entry has none to drop, because covers are booked
        // at the send and a refusal never reaches it.
        self.covers
            .intent_refused(intent.strategy, intent.symbol, intent.reduce_only);
        let event = EngineEvent::IntentRefused {
            symbol: intent.symbol,
            reduce_only: intent.reduce_only,
            reason: reason.to_string(),
        };
        let now = clock::now_ns();
        let Engine {
            strategies,
            market,
            timers,
            pending,
            orders,
            registry,
            attribution,
            covers,
            account,
            rules,
            ..
        } = self;
        feed_strategy(
            strategies,
            market,
            account,
            rules,
            timers,
            pending,
            orders,
            registry,
            attribution,
            covers,
            intent.strategy,
            &event,
            now,
        );
    }

    /// Turn an entry the strategy asked to have worked into the resting limit
    /// it should start as, and say whether it will be worked at all.
    ///
    /// `None` leaves the intent exactly as the strategy wrote it: no policy,
    /// an exit, a symbol with no instrument rule, or a spread too thin for
    /// resting to pay for itself.
    fn plan_resting_entry(&self, intent: &mut Intent) -> Option<WorkPolicy> {
        let rule = self
            .rules
            .get(intent.symbol.0 as usize)
            .copied()
            .flatten()?;
        let touch = self
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
        let quote = self.market.quote(symbol);
        if quote.bid_px > 0.0 && quote.ask_px > quote.bid_px {
            (quote.bid_px + quote.ask_px) / 2.0
        } else {
            0.0
        }
    }

    fn reference_px(&self, symbol: SymbolId, kind: &OrderKind) -> Option<f64> {
        if let OrderKind::Limit { px, .. } = kind {
            return Some(*px);
        }
        let quote = self.market.quote(symbol);
        if quote.bid_px > 0.0 && quote.ask_px > 0.0 {
            return Some((quote.bid_px + quote.ask_px) / 2.0);
        }
        let ticker = self.market.ticker(symbol);
        [ticker.last_px, ticker.mark_px]
            .into_iter()
            .find(|px| *px > 0.0)
    }

    /// `M0` for an order of ours, off the order ledger. Zero for one the
    /// ledger no longer holds, which makes every arrival number for its fills
    /// missing rather than wrong.
    fn arrival_mid_of(&self, client_order_id: &str) -> f64 {
        self.orders
            .orders
            .get(client_order_id)
            .map(|order| order.arrival_mid)
            .unwrap_or(0.0)
    }
}
