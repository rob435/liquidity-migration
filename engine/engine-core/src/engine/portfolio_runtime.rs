use std::collections::HashMap;

use super::*;
use engine_types::numeric::{AssetId, Exact};
use engine_types::portfolio_control::*;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn append_portfolio_control(
        &mut self,
        record: WalRecord,
    ) -> Result<(), EngineError> {
        let mut next = self.portfolio_controls.clone();
        next.apply(&record).map_err(EngineError::State)?;
        if let WalRecord::SleeveStopSet {
            strategy,
            symbol,
            side,
            trigger_price,
            ..
        } = &record
        {
            self.books
                .attribution
                .validate_sleeve_stop_exact(*strategy, *symbol, *side, trigger_price)
                .map_err(EngineError::State)?;
        }
        let settlement = match &record {
            WalRecord::PortfolioOffsetSettled { settlement } => {
                self.fills
                    .validate_internal_settlement(settlement)
                    .map_err(EngineError::State)?;
                Some(
                    self.books
                        .attribution
                        .prepare_internal_settlement(settlement)
                        .map_err(EngineError::State)?,
                )
            }
            _ => None,
        };
        self.wal.append(&record)?;
        if let Some(settlement) = settlement {
            self.books
                .attribution
                .commit_internal_settlement(settlement)
                .map_err(EngineError::State)?;
        }
        if let WalRecord::PortfolioOffsetSettled { settlement } = &record {
            self.fills
                .on_internal_settlement(settlement)
                .map_err(EngineError::State)?;
        }
        if let WalRecord::SleeveStopSet {
            strategy,
            symbol,
            side,
            trigger_price,
            ..
        } = record
        {
            self.books
                .attribution
                .set_sleeve_stop_exact(strategy, symbol, side, trigger_price)
                .map_err(EngineError::State)?;
        }
        self.portfolio_controls = next;
        self.portfolio_dirty = true;
        Ok(())
    }

    pub(super) fn observe_virtual_stops(&mut self, event: &MarketEvent) -> Result<(), EngineError> {
        let (symbol, bid, ask) = match event {
            MarketEvent::Quote { symbol, quote } => (*symbol, quote.bid_px, quote.ask_px),
            MarketEvent::Depth { symbol, depth } => {
                let quote = depth.quote();
                (*symbol, quote.bid_px, quote.ask_px)
            }
            MarketEvent::Trades { symbol, trades } => (*symbol, trades.last_px, trades.last_px),
            MarketEvent::Ticker { symbol, ticker } => (*symbol, ticker.last_px, ticker.last_px),
            MarketEvent::FeedReset { .. } => return Ok(()),
        };
        if !self.instrument_specs.contains_key(&symbol) {
            return Ok(());
        }
        let state = self.books.attribution.snapshot();
        for row in state.positions.iter().filter(|row| row.symbol == symbol) {
            if self.portfolio_controls.emergencies.contains_key(&symbol) {
                continue;
            }
            let Some(stop) = &row.stop_px else { continue };
            let value = if row.signed_qty.is_positive() {
                bid
            } else {
                ask
            };
            if !value.is_finite() || value <= 0.0 {
                continue;
            }
            let price =
                Exact::from_legacy_f64(value).map_err(|e| EngineError::State(e.to_string()))?;
            let triggered = if row.signed_qty.is_positive() {
                &price <= stop
            } else {
                &price >= stop
            };
            if triggered {
                self.request_portfolio_exit(
                    row.strategy,
                    symbol,
                    if row.signed_qty.is_positive() {
                        Side::Buy
                    } else {
                        Side::Sell
                    },
                    Exact::zero(),
                    Some(price),
                )?;
            }
        }
        Ok(())
    }

    pub(super) fn observe_portfolio_physical_update(&mut self, update: &OrderUpdate) {
        let symbol = match update {
            OrderUpdate::Fill { symbol, .. } => Some(*symbol),
            OrderUpdate::Cancelled {
                client_order_id, ..
            }
            | OrderUpdate::Reject {
                client_order_id, ..
            } => self
                .books
                .orders
                .orders
                .get(client_order_id)
                .map(|order| order.request.symbol),
            _ => None,
        };
        if let Some(symbol) = symbol {
            self.portfolio_physical_after
                .insert(symbol, clock::now_ns());
        }
    }

    fn settlement_account_ready(&mut self, symbol: SymbolId) -> bool {
        let after = self
            .portfolio_physical_after
            .get(&symbol)
            .copied()
            .unwrap_or(0);
        if self.account_refresh_started_ns <= after || self.books.account.observed_ns == 0 {
            self.request_account_refresh_after(after);
            return false;
        }
        if !self.private_stream_ready || !self.dispatches.unresolved.is_empty() {
            return false;
        }
        let expected = self
            .logged_exposure
            .get(&symbol)
            .cloned()
            .unwrap_or_else(Exact::zero);
        let rows: Vec<_> = self
            .books
            .account
            .positions
            .iter()
            .filter(|row| row.symbol == symbol && row.qty != 0.0)
            .collect();
        let observed = match rows.as_slice() {
            [] => Exact::zero(),
            [row] => match row.quantity() {
                Ok(quantity) if quantity.is_positive() => {
                    if row.side == Side::Buy {
                        quantity
                    } else {
                        -quantity
                    }
                }
                _ => return false,
            },
            _ => return false,
        };
        observed == expected
    }

    pub(super) fn retain_portfolio_reduction(
        &mut self,
        intent: &Intent,
    ) -> Result<(), EngineError> {
        if !intent.reduce_only
            || !intent.qty.is_finite()
            || intent.qty <= 0.0
            || !self.instrument_specs.contains_key(&intent.symbol)
        {
            return Ok(());
        }
        let state = self.books.attribution.snapshot();
        let Some(held) = state
            .positions
            .iter()
            .find(|row| row.strategy == intent.strategy && row.symbol == intent.symbol)
        else {
            return Ok(());
        };
        let side = if held.signed_qty.is_positive() {
            Side::Buy
        } else {
            Side::Sell
        };
        if intent.side != side.flipped() {
            return Ok(());
        }
        let requested = intent
            .quantity()
            .map_err(|e| EngineError::State(e.to_string()))?;
        let quantity = held.signed_qty.abs();
        let target = &quantity - &requested.min(quantity.clone());
        let price = self
            .reference_px(intent.symbol, &OrderKind::Market)
            .map(engine_types::order_terms::strategy_decimal)
            .transpose()
            .map_err(|e| EngineError::State(e.to_string()))?;
        self.request_portfolio_exit(intent.strategy, intent.symbol, side, target, price)
    }

    fn request_portfolio_exit(
        &mut self,
        strategy: StrategyId,
        symbol: SymbolId,
        side: Side,
        target: Exact,
        price: Option<Exact>,
    ) -> Result<(), EngineError> {
        if self.portfolio_controls.emergencies.contains_key(&symbol) {
            return Ok(());
        }
        let state = if let Some(old) = self.portfolio_controls.exits.get(&(strategy, symbol)) {
            if old.target_remaining <= target || old.position_side != side {
                return Ok(());
            }
            let mut state = old.clone();
            state.target_remaining = target;
            state
        } else {
            PortfolioExit {
                id: self
                    .portfolio_controls
                    .next_id()
                    .map_err(EngineError::State)?,
                strategy,
                symbol,
                position_side: side,
                target_remaining: target,
                trigger_price: price,
                started_ms: clock::wall_ms(),
                attempt: 0,
                order_id: None,
            }
        };
        self.append_portfolio_control(WalRecord::PortfolioExitChanged { state })
    }

    pub(super) fn start_portfolio_emergency(
        &mut self,
        symbol: SymbolId,
        price: Exact,
        reason: PortfolioEmergencyReason,
    ) -> Result<(), EngineError> {
        if self.portfolio_controls.emergencies.contains_key(&symbol) {
            return Ok(());
        }
        let id = self
            .portfolio_controls
            .next_id()
            .map_err(EngineError::State)?;
        self.append_portfolio_control(WalRecord::PortfolioEmergencyChanged {
            state: PortfolioEmergency {
                id,
                symbol,
                reference_price: price,
                reason,
                phase: PortfolioEmergencyPhase::ResolveOrders,
                started_ms: clock::wall_ms(),
                attempt: 0,
                order_id: None,
            },
        })
    }

    pub(super) async fn service_portfolio_controls(&mut self) -> Result<(), EngineError> {
        self.advance_portfolio_controls().await?;
        if self.portfolio_dirty && self.dispatches.write.is_none() {
            let barrier = self.wal.barrier_begin()?;
            self.portfolio_dirty = false;
            self.dispatches
                .begin(crate::order_dispatch::DispatchWrite::Portfolio, barrier);
        }
        Ok(())
    }

    async fn advance_portfolio_controls(&mut self) -> Result<(), EngineError> {
        if self.dispatches.write.is_some() {
            return Ok(());
        }
        if self.portfolio_dirty {
            let barrier = self.wal.barrier_begin()?;
            self.portfolio_dirty = false;
            self.dispatches
                .begin(crate::order_dispatch::DispatchWrite::Portfolio, barrier);
            return Ok(());
        }
        if let Some((symbol, price)) = self
            .portfolio_controls
            .native_pending
            .iter()
            .next()
            .map(|(symbol, price)| (*symbol, price.clone()))
        {
            return self.start_portfolio_emergency(
                symbol,
                price,
                PortfolioEmergencyReason::NativeClose,
            );
        }
        let mut controls: Vec<_> = self
            .portfolio_controls
            .emergencies
            .values()
            .map(|state| (state.id, Some(state.clone()), None))
            .chain(
                self.portfolio_controls
                    .exits
                    .values()
                    .map(|state| (state.id, None, Some(state.clone()))),
            )
            .collect();
        controls.sort_by_key(|(id, _, _)| (*id <= self.portfolio_cursor, *id));
        for (id, emergency, exit) in controls.into_iter().take(MAX_ORDERS_PER_BATCH) {
            self.portfolio_cursor = id;
            if let Some(state) = emergency {
                self.service_portfolio_emergency(state).await?;
            }
            if let Some(state) = exit {
                if !self
                    .portfolio_controls
                    .emergencies
                    .contains_key(&state.symbol)
                {
                    self.service_portfolio_exit(state).await?;
                }
            }
            if self.portfolio_dirty || self.dispatches.write.is_some() {
                break;
            }
        }
        Ok(())
    }

    async fn service_portfolio_exit(&mut self, mut exit: PortfolioExit) -> Result<(), EngineError> {
        if self.busy_symbols.contains_key(&exit.symbol) {
            return Ok(());
        }
        let orders = self
            .books
            .orders
            .in_flight()
            .into_iter()
            .filter(|order| {
                order.request.strategy == exit.strategy && order.request.symbol == exit.symbol
            })
            .collect::<Vec<_>>();
        if !orders.is_empty() {
            let cancellations = orders
                .iter()
                .filter(|order| !order.request.is_sleeve_reduction())
                .map(|order| order.request.client_order_id.clone())
                .collect::<Vec<_>>();
            for id in cancellations {
                self.enqueue_halt_cancel(exit.symbol, id);
            }
            return Ok(());
        }
        let state = self.books.attribution.snapshot();
        let held = state
            .positions
            .iter()
            .find(|row| row.strategy == exit.strategy && row.symbol == exit.symbol);
        let remaining = held
            .filter(|row| row.signed_qty.is_positive() == (exit.position_side == Side::Buy))
            .map(|row| row.signed_qty.abs())
            .unwrap_or_else(Exact::zero);
        if remaining <= exit.target_remaining {
            return self.append_portfolio_control(WalRecord::PortfolioExitCompleted {
                id: exit.id,
                strategy: exit.strategy,
                symbol: exit.symbol,
            });
        }
        let qty = if exit.position_side == Side::Buy {
            remaining - &exit.target_remaining
        } else {
            -(remaining - &exit.target_remaining)
        };
        if !self.portfolio_controls.retry_ready(exit.id) {
            return Ok(());
        }
        if exit
            .order_id
            .as_ref()
            .is_some_and(|id| self.books.orders.orders.contains_key(id))
        {
            exit.order_id = None;
        }
        if exit.order_id.is_none() {
            exit.attempt = exit.attempt.checked_add(1).ok_or_else(|| {
                EngineError::State("portfolio exit attempt capacity exhausted".into())
            })?;
            exit.order_id = Some(self.mint_id()?);
            return self.append_portfolio_control(WalRecord::PortfolioExitChanged { state: exit });
        }
        self.portfolio_controls.attempted(exit.id, exit.attempt);
        let intent = self.portfolio_exit_intent(
            exit.strategy,
            exit.symbol,
            qty,
            format!("portfolio-exit:{}", exit.id),
        )?;
        let mut protection = HashMap::new();
        if let Some(order) = self
            .prepare_intent(
                intent,
                exit.order_id.clone(),
                clock::now_ns(),
                &mut protection,
            )
            .await?
        {
            self.queue_order_dispatches(vec![order])?;
        } else {
            let price = exit.trigger_price.or_else(|| {
                self.reference_px(exit.symbol, &OrderKind::Market)
                    .and_then(|price| engine_types::order_terms::strategy_decimal(price).ok())
            });
            if let Some(price) = price {
                self.start_portfolio_emergency(
                    exit.symbol,
                    price,
                    PortfolioEmergencyReason::ExitUnavailable,
                )?;
            }
        }
        Ok(())
    }

    fn portfolio_exit_intent(
        &self,
        strategy: StrategyId,
        symbol: SymbolId,
        signed_qty: Exact,
        tag: String,
    ) -> Result<Intent, EngineError> {
        Ok(Intent {
            exact_prices: None,
            exact_quantity: Some(Box::new(signed_qty.abs())),
            strategy,
            symbol,
            side: if signed_qty.is_positive() {
                Side::Sell
            } else {
                Side::Buy
            },
            qty: signed_qty
                .abs()
                .to_f64()
                .map_err(|error| EngineError::State(error.to_string()))?,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag,
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        })
    }

    async fn service_portfolio_emergency(
        &mut self,
        mut state: PortfolioEmergency,
    ) -> Result<(), EngineError> {
        if self.busy_symbols.contains_key(&state.symbol) {
            return Ok(());
        }
        match state.phase {
            PortfolioEmergencyPhase::ResolveOrders => {
                let orders = self
                    .books
                    .orders
                    .in_flight()
                    .into_iter()
                    .filter(|order| order.request.symbol == state.symbol)
                    .map(|order| order.request.client_order_id.clone())
                    .collect::<Vec<_>>();
                if !orders.is_empty() {
                    for id in orders {
                        self.enqueue_halt_cancel(state.symbol, id);
                    }
                    return Ok(());
                }
                state.phase = PortfolioEmergencyPhase::CloseNet;
                self.append_portfolio_control(WalRecord::PortfolioEmergencyChanged { state })
            }
            PortfolioEmergencyPhase::CloseNet => {
                if self
                    .books
                    .orders
                    .in_flight()
                    .iter()
                    .any(|order| order.request.symbol == state.symbol)
                {
                    return Ok(());
                }
                let interval = match self
                    .risk
                    .physical_exposure_interval(state.symbol, &self.books.account)
                {
                    Ok(value) => value,
                    Err(_) => return Ok(()),
                };
                if interval.low() != interval.high() {
                    return Ok(());
                }
                let owned_net = self
                    .books
                    .attribution
                    .snapshot()
                    .positions
                    .into_iter()
                    .filter(|row| row.symbol == state.symbol)
                    .fold(Exact::zero(), |sum, row| sum + row.signed_qty);
                if owned_net.is_zero() {
                    state.phase = PortfolioEmergencyPhase::SettleOffsets;
                    state.order_id = None;
                    return self
                        .append_portfolio_control(WalRecord::PortfolioEmergencyChanged { state });
                }
                if !self.portfolio_controls.retry_ready(state.id) {
                    return Ok(());
                }
                if state
                    .order_id
                    .as_ref()
                    .is_some_and(|id| self.books.orders.orders.contains_key(id))
                {
                    state.order_id = None;
                }
                if state.order_id.is_none() {
                    state.attempt = state.attempt.checked_add(1).ok_or_else(|| {
                        EngineError::State("portfolio emergency attempt capacity exhausted".into())
                    })?;
                    state.order_id = Some(self.mint_id()?);
                    return self
                        .append_portfolio_control(WalRecord::PortfolioEmergencyChanged { state });
                }
                self.portfolio_controls.attempted(state.id, state.attempt);
                if let Some(order) = self.prepare_emergency_net_order(&state)? {
                    if !order.request.reduce_only {
                        return Err(EngineError::State(
                            "portfolio emergency attempted physical growth".into(),
                        ));
                    }
                    self.queue_order_dispatches(vec![order])?;
                }
                Ok(())
            }
            PortfolioEmergencyPhase::SettleOffsets => {
                if !self.settlement_account_ready(state.symbol) {
                    return Ok(());
                }
                let interval = match self
                    .risk
                    .physical_exposure_interval(state.symbol, &self.books.account)
                {
                    Ok(value) => value,
                    Err(_) => return Ok(()),
                };
                let expected = self
                    .logged_exposure
                    .get(&state.symbol)
                    .cloned()
                    .unwrap_or_else(Exact::zero);
                if interval.low() != &expected
                    || interval.high() != &expected
                    || self
                        .books
                        .orders
                        .in_flight()
                        .iter()
                        .any(|order| order.request.symbol == state.symbol)
                {
                    state.phase = PortfolioEmergencyPhase::ResolveOrders;
                    return self
                        .append_portfolio_control(WalRecord::PortfolioEmergencyChanged { state });
                }
                let portfolio = self.books.attribution.snapshot();
                let rows = portfolio
                    .positions
                    .iter()
                    .filter(|row| row.symbol == state.symbol)
                    .collect::<Vec<_>>();
                if rows.is_empty() {
                    return self.append_portfolio_control(WalRecord::PortfolioEmergencyCompleted {
                        id: state.id,
                        symbol: state.symbol,
                    });
                }
                let price = state.reference_price.clone();
                let asset = self
                    .instrument_specs
                    .get(&state.symbol)
                    .map(|spec| spec.settlement_asset.clone())
                    .unwrap_or(AssetId::Unknown);
                self.append_portfolio_control(WalRecord::PortfolioOffsetSettled {
                    settlement: PortfolioOffsetSettlement {
                        emergency_id: state.id,
                        symbol: state.symbol,
                        price,
                        settled_ms: clock::wall_ms(),
                        slices: rows
                            .iter()
                            .map(|row| PortfolioOffsetSlice {
                                strategy: row.strategy,
                                signed_quantity: -row.signed_qty.clone(),
                                settlement_asset: asset.clone(),
                            })
                            .collect(),
                    },
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn emergency_quantity_preserves_canonical_units_and_respects_the_market_maximum() {
        for (quantity, max, expected) in [
            ("0.100000000000000001", None, "0.100000000000000001"),
            ("1", Some("0.5"), "0.5"),
        ] {
            let mut engine =
                crate::tests::shared_sleeves::exact_single_sleeve_engine(quantity, max).await;
            engine.books.market.apply(&MarketEvent::Quote {
                symbol: SymbolId(0),
                quote: engine_types::Quote {
                    bid_px: 99.9,
                    ask_px: 100.1,
                    recv_ns: clock::now_ns(),
                    ..Default::default()
                },
            });
            engine.risk.observe_price(SymbolId(0), 100.0);
            engine
                .start_portfolio_emergency(
                    SymbolId(0),
                    Exact::parse_decimal("100").unwrap(),
                    PortfolioEmergencyReason::ExitUnavailable,
                )
                .unwrap();
            for _ in 0..32 {
                engine.service_order_dispatches().await.unwrap();
                engine.service_portfolio_controls().await.unwrap();
                if !engine.books.orders.in_flight().is_empty() {
                    break;
                }
                tokio::task::yield_now().await;
            }
            let orders = engine.books.orders.in_flight();
            assert_eq!(
                orders.len(),
                1,
                "an oversized net must close in legal chunks"
            );
            assert_eq!(
                orders[0].request.exact_terms.as_ref().unwrap().quantity,
                Exact::parse_decimal(expected).unwrap(),
                "engine-owned exact quantities must not round-trip through binary64"
            );
            assert!(
                !orders[0].request.close_position,
                "maximum order size is not permission to bypass venue bounds"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn an_emergency_combines_sleeve_fragments_into_one_legal_physical_close() {
        let mut engine = crate::tests::shared_sleeves::fragmented_engine().await;
        engine.books.market.apply(&MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: engine_types::Quote {
                bid_px: 99.9,
                ask_px: 100.1,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
        engine.risk.observe_price(SymbolId(0), 100.0);
        engine
            .start_portfolio_emergency(
                SymbolId(0),
                Exact::parse_decimal("100").unwrap(),
                PortfolioEmergencyReason::ExitUnavailable,
            )
            .unwrap();
        for _ in 0..32 {
            engine.service_order_dispatches().await.unwrap();
            engine.service_portfolio_controls().await.unwrap();
            if !engine.books.orders.in_flight().is_empty() {
                break;
            }
            tokio::task::yield_now().await;
        }
        let orders = engine.books.orders.in_flight();
        assert_eq!(
            orders.len(),
            1,
            "each sleeve is below the venue minimum but their physical net is a legal close"
        );
        assert_eq!(orders[0].request.qty, 1.0);
        assert!(orders[0].request.reduce_only);
    }

    #[tokio::test(start_paused = true)]
    async fn aggregate_close_allocates_actual_partial_fills_and_replays_each_owner_once() {
        use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
        let mut engine = crate::tests::shared_sleeves::fragmented_engine().await;
        let base = engine.rotation_base(clock::wall_ms());
        let prefix = engine.wal.snapshot_records().len();
        engine.books.market.apply(&MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: engine_types::Quote {
                bid_px: 99.9,
                ask_px: 100.1,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
        engine.risk.observe_price(SymbolId(0), 100.0);
        engine
            .start_portfolio_emergency(
                SymbolId(0),
                Exact::parse_decimal("100").unwrap(),
                PortfolioEmergencyReason::ExitUnavailable,
            )
            .unwrap();
        for _ in 0..32 {
            engine.service_order_dispatches().await.unwrap();
            engine.service_portfolio_controls().await.unwrap();
            if !engine.books.orders.in_flight().is_empty() {
                break;
            }
            tokio::task::yield_now().await;
        }
        let request = engine
            .books
            .orders
            .in_flight()
            .first()
            .expect("aggregate close must be admitted")
            .request
            .clone();
        assert_eq!(request.sleeve_owner(), None);
        assert_eq!(
            engine.books.registry.owner_of(&request.client_order_id),
            None
        );
        assert_eq!(engine.books.orders.owner_of(&request.client_order_id), None);
        for _ in 0..2 {
            let durable =
                tokio::time::timeout(Duration::from_secs(1), engine.dispatches.durable.recv())
                    .await
                    .unwrap();
            engine.on_order_dispatch_durable(durable).await.unwrap();
        }
        let completion =
            tokio::time::timeout(Duration::from_secs(1), engine.venue_completions.recv())
                .await
                .unwrap()
                .unwrap();
        engine.take_venue_completion(completion).await.unwrap();
        assert!(
            engine.books.orders.orders[&request.client_order_id].acked,
            "partial fills must follow a real acknowledged venue submission"
        );
        let make_fill = |exec_id: &str| OrderUpdate::Fill {
            client_order_id: request.client_order_id.clone(),
            exec_id: exec_id.into(),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.5,
            px: 99.0,
            fee: Some(0.005),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: clock::wall_ms(),
            recv_ns: clock::now_ns(),
            allocation: None,
            amounts: Some(Box::new(ExecutionAmounts {
                quantity: ExactNumber::venue_decimal("0.5").unwrap(),
                price: ExactNumber::venue_decimal("99").unwrap(),
                settlement_asset: AssetId::Named("USDT".into()),
                fee: Some(AssetAmount {
                    asset: AssetId::Named("USDT".into()),
                    amount: ExactNumber::venue_decimal("0.005").unwrap(),
                }),
            })),
        };
        let first = make_fill("engine-net-first");
        engine.take_update(first.clone()).await.unwrap();
        let once = engine.books.attribution.snapshot();
        assert_eq!(once.positions.len(), 1);
        assert_eq!(once.positions[0].strategy, StrategyId(1));
        assert_eq!(
            once.positions[0].signed_qty,
            Exact::parse_decimal("0.5").unwrap()
        );
        engine.take_update(first).await.unwrap();
        assert_eq!(
            engine.books.attribution.snapshot(),
            once,
            "duplicate private delivery cannot repeat allocation or fees"
        );
        let middle = engine.rotation_base(clock::wall_ms());
        assert_eq!(
            Attribution::try_from_records(std::slice::from_ref(&middle))
                .unwrap()
                .snapshot(),
            once
        );
        let after_middle = engine.wal.snapshot_records().len();
        engine
            .take_update(make_fill("engine-net-second"))
            .await
            .unwrap();
        let final_state = engine.books.attribution.snapshot();
        assert!(final_state.positions.is_empty());
        let records = engine.wal.snapshot_records();
        let all: Vec<_> = std::iter::once(base)
            .chain(records[prefix..].iter().cloned())
            .collect();
        assert_eq!(
            Attribution::try_from_records(&all).unwrap().snapshot(),
            final_state
        );
        let rotated: Vec<_> = std::iter::once(middle)
            .chain(records[after_middle..].iter().cloned())
            .collect();
        assert_eq!(
            Attribution::try_from_records(&rotated).unwrap().snapshot(),
            final_state
        );
        let fills: Vec<_> = records
            .iter()
            .filter_map(|record| match record {
                WalRecord::OrderUpdate {
                    update:
                        OrderUpdate::Fill {
                            allocation: Some(allocation),
                            client_order_id,
                            forced_close,
                            ..
                        },
                    callbacks,
                } if client_order_id == &request.client_order_id => {
                    assert!(
                        forced_close.is_none(),
                        "engine close must not masquerade as a venue stop"
                    );
                    Some((allocation, callbacks))
                }
                _ => None,
            })
            .collect();
        assert_eq!(fills.len(), 2);
        assert_eq!(
            fills[0]
                .0
                .slices
                .iter()
                .map(|slice| (slice.strategy, slice.quantity.clone()))
                .collect::<Vec<_>>(),
            vec![
                (StrategyId(0), Exact::parse_decimal("0.4").unwrap()),
                (StrategyId(1), Exact::parse_decimal("0.1").unwrap())
            ]
        );
        let callback_owners: Vec<Vec<_>> = records
            .iter()
            .filter_map(|record| match record {
                WalRecord::OrderUpdate {
                    update:
                        update @ OrderUpdate::Fill {
                            client_order_id, ..
                        },
                    ..
                } if client_order_id == &request.client_order_id => Some(
                    crate::portfolio_allocation::slice_updates(update)
                        .unwrap()
                        .unwrap()
                        .into_iter()
                        .map(|(owner, _)| owner)
                        .collect(),
                ),
                _ => None,
            })
            .collect();
        assert_eq!(
            callback_owners,
            vec![vec![StrategyId(0), StrategyId(1)], vec![StrategyId(1)]]
        );
        let fee_sum = fills
            .iter()
            .flat_map(|(a, _)| &a.slices)
            .fold(Exact::zero(), |sum, row| {
                sum + &row.fee.as_ref().unwrap().amount.value
            });
        assert_eq!(fee_sum, Exact::parse_decimal("0.01").unwrap());
        let mut corrupt = rotated;
        let record = corrupt
            .iter_mut()
            .find(|record| {
                matches!(
                    record,
                    WalRecord::OrderUpdate {
                        update: OrderUpdate::Fill { .. },
                        ..
                    }
                )
            })
            .unwrap();
        if let WalRecord::OrderUpdate {
            update: OrderUpdate::Fill { allocation, .. },
            ..
        } = record
        {
            *allocation = None;
        }
        assert!(
            Attribution::try_from_records(&corrupt).is_err(),
            "parent fill cannot silently become a single-sleeve allocation"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn balanced_sleeves_settle_beside_a_trusted_manual_baseline_after_restart() {
        let mut engine = crate::tests::shared_sleeves::balanced_engine().await;
        engine.books.account.positions = crate::tests::shared_sleeves::physical_long(0.5);
        engine.books.account.observed_ns = clock::now_ns();
        engine
            .logged_exposure
            .insert(SymbolId(0), Exact::parse_decimal("0.5").unwrap());
        engine.risk.observe_account_view(&engine.books.account);
        engine
            .start_portfolio_emergency(
                SymbolId(0),
                Exact::parse_decimal("99").unwrap(),
                PortfolioEmergencyReason::NativeClose,
            )
            .unwrap();
        let base = engine.rotation_base(clock::wall_ms());
        let mut restart = crate::tests::shared_sleeves::restart_portfolio(
            &[base],
            crate::tests::shared_sleeves::physical_long(0.5),
        )
        .await;
        restart.account_refresh_started_ns = clock::now_ns();
        for _ in 0..32 {
            restart.service_order_dispatches().await.unwrap();
            restart.service_portfolio_controls().await.unwrap();
            tokio::task::yield_now().await;
        }
        assert!(
            restart.books.attribution.snapshot().positions.is_empty(),
            "manual baseline stranded balanced sleeve offsets"
        );
        assert!(restart.portfolio_controls.emergencies.is_empty());
        assert!(
            restart.books.orders.in_flight().is_empty(),
            "internal settlement must never liquidate the manual holding"
        );
        assert_eq!(
            restart.logged_exposure.get(&SymbolId(0)),
            Some(&Exact::parse_decimal("0.5").unwrap())
        );
        assert_eq!(restart.books.account.positions[0].qty, 0.5);
        assert_eq!(
            restart
                .books
                .attribution
                .snapshot()
                .internal_settlements
                .len(),
            2
        );
    }

    #[tokio::test(start_paused = true)]
    async fn an_unpriced_exit_retains_ownership_without_flooding_the_journal() {
        let mut engine = crate::tests::shared_sleeves::fragmented_engine().await;
        engine
            .request_portfolio_exit(StrategyId(0), SymbolId(0), Side::Buy, Exact::zero(), None)
            .unwrap();
        let before = engine.wal.snapshot_records().len();
        for _ in 0..1000 {
            engine.service_order_dispatches().await.unwrap();
            engine.service_portfolio_controls().await.unwrap();
            tokio::task::yield_now().await;
        }
        assert!(engine
            .portfolio_controls
            .exits
            .contains_key(&(StrategyId(0), SymbolId(0))));
        assert!(
            engine.wal.snapshot_records().len() - before <= 10,
            "a missing reference price rewrites the same refusal every engine turn"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn internal_offsets_wait_for_private_recovery_even_when_the_old_account_is_flat() {
        let mut engine = crate::tests::shared_sleeves::balanced_engine().await;
        let mut state = PortfolioEmergency {
            id: 1,
            symbol: SymbolId(0),
            reference_price: Exact::parse_decimal("100").unwrap(),
            reason: PortfolioEmergencyReason::NativeClose,
            phase: PortfolioEmergencyPhase::ResolveOrders,
            started_ms: clock::wall_ms(),
            attempt: 0,
            order_id: None,
        };
        for phase in [
            PortfolioEmergencyPhase::ResolveOrders,
            PortfolioEmergencyPhase::CloseNet,
            PortfolioEmergencyPhase::SettleOffsets,
        ] {
            state.phase = phase;
            engine
                .portfolio_controls
                .apply(&WalRecord::PortfolioEmergencyChanged {
                    state: state.clone(),
                })
                .unwrap();
        }
        let before = engine.books.attribution.snapshot();
        engine.private_stream_ready = false;
        engine.books.account.observed_ns = 0;
        engine.service_portfolio_emergency(state).await.unwrap();
        assert_eq!(
            engine.books.attribution.snapshot(),
            before,
            "a stale flat snapshot settled sleeve balances during an unresolved private gap"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_refused_virtual_reduction_retains_an_engine_owned_exit() {
        let mut engine = crate::tests::shared_sleeves::balanced_engine().await;
        engine.books.market.apply(&MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: engine_types::Quote {
                bid_px: 99.9,
                ask_px: 100.1,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
        engine.risk.observe_price(SymbolId(0), 100.0);
        engine.books.account.available_usdt = 0.0;
        let order = engine
            .prepare_intent(
                Intent {
                    exact_prices: None,
                    exact_quantity: None,
                    strategy: StrategyId(0),
                    symbol: SymbolId(0),
                    side: Side::Sell,
                    qty: 0.5,
                    kind: OrderKind::Market,
                    stop: None,
                    reduce_only: true,
                    tag: "partial-exit".into(),
                    decided_ns: clock::now_ns(),
                    work: None,
                    leverage: None,
                },
                None,
                clock::now_ns(),
                &mut HashMap::new(),
            )
            .await
            .unwrap();
        assert!(
            order.is_none(),
            "virtual reduction requires margin for the resulting native short"
        );
        let retained = engine
            .portfolio_controls
            .exits
            .get(&(StrategyId(0), SymbolId(0)))
            .expect("temporary refusal discarded the accepted sleeve reduction");
        assert_eq!(
            retained.target_remaining,
            Exact::parse_decimal("0.5").unwrap(),
            "a partial reduction must not become a full sleeve exit"
        );
        engine.books.account.available_usdt = 1000.0;
        for _ in 0..24 {
            engine.service_order_dispatches().await.unwrap();
            engine.service_portfolio_controls().await.unwrap();
            if !engine.books.orders.in_flight().is_empty() {
                break;
            }
            tokio::task::yield_now().await;
        }
        let orders = engine.books.orders.in_flight();
        assert_eq!(
            orders.len(),
            1,
            "the retained reduction did not resume after margin recovered"
        );
        assert_eq!(orders[0].request.qty, 0.5);
    }

    #[tokio::test(start_paused = true)]
    async fn a_busy_emergency_does_not_starve_another_sleeves_exit() {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let strategy = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let (mut engine, records) = crate::tests::callback_test_fixture(vec![strategy]).await;
        let busy = SymbolId(0);
        let ready = SymbolId(1);
        engine.books.market.table.intern("ETHUSDT");
        engine
            .books
            .attribution
            .note(StrategyId(0), ready, Side::Buy, 1.0);
        engine.busy_symbols.insert(busy, 1);
        engine
            .portfolio_controls
            .apply(&WalRecord::PortfolioEmergencyChanged {
                state: PortfolioEmergency {
                    id: 1,
                    symbol: busy,
                    reference_price: Exact::parse_decimal("100").unwrap(),
                    reason: PortfolioEmergencyReason::NativeClose,
                    phase: PortfolioEmergencyPhase::ResolveOrders,
                    started_ms: clock::wall_ms(),
                    attempt: 0,
                    order_id: None,
                },
            })
            .unwrap();
        engine
            .portfolio_controls
            .apply(&WalRecord::PortfolioExitChanged {
                state: PortfolioExit {
                    id: 2,
                    strategy: StrategyId(0),
                    symbol: ready,
                    position_side: Side::Buy,
                    target_remaining: Exact::zero(),
                    trigger_price: Some(Exact::parse_decimal("90").unwrap()),
                    started_ms: clock::wall_ms(),
                    attempt: 0,
                    order_id: None,
                },
            })
            .unwrap();
        engine.service_portfolio_controls().await.unwrap();
        assert!(records.lock().unwrap().iter().any(|record| matches!(record, WalRecord::PortfolioExitChanged { state } if state.id == 2 && state.order_id.is_some())), "a busy symbol monopolized the durable exit owner");
    }
    fn priced(
        engine: &mut Engine<crate::tests::MockWal, engine_risk::Kernel, crate::tests::MockVenue>,
    ) {
        engine.books.market.apply(&MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: engine_types::Quote {
                bid_px: 99.9,
                ask_px: 100.1,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
        engine.risk.observe_price(SymbolId(0), 100.0);
    }

    async fn pending_order(
        engine: &mut Engine<crate::tests::MockWal, engine_risk::Kernel, crate::tests::MockVenue>,
    ) -> OrderRequest {
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while std::time::Instant::now() < deadline {
            engine.service_order_dispatches().await.unwrap();
            engine.service_portfolio_controls().await.unwrap();
            if let Some(order) = engine.books.orders.in_flight().first() {
                return order.request.clone();
            }
            tokio::task::yield_now().await;
            // Portfolio retry uses a real monotonic clock even in paused-runtime tests.
            std::thread::sleep(Duration::from_millis(2));
        }
        panic!(
            "exit obligation produced no pending order: {:?}",
            engine.wal.snapshot_records()
        );
    }

    fn assert_reversible_order_id(id: &str) {
        let fields: Vec<_> = id.split('-').collect();
        assert_eq!(fields.len(), 3);
        assert_eq!(fields[0], "eng");
        assert_eq!(fields[1].parse::<u64>().unwrap() % 1000, 0);
        assert!((1..(1 << 18)).contains(&fields[2].parse::<u32>().unwrap()));
    }

    async fn acknowledge(
        engine: &mut Engine<crate::tests::MockWal, engine_risk::Kernel, crate::tests::MockVenue>,
    ) {
        for _ in 0..2 {
            let durable =
                tokio::time::timeout(Duration::from_secs(1), engine.dispatches.durable.recv())
                    .await
                    .unwrap();
            engine.on_order_dispatch_durable(durable).await.unwrap();
        }
        let completion =
            tokio::time::timeout(Duration::from_secs(1), engine.venue_completions.recv())
                .await
                .unwrap()
                .unwrap();
        engine.take_venue_completion(completion).await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn ordinary_exits_keep_exact_units_and_chunk_without_emergency_takeover() {
        for (quantity, maximum, expected) in [
            ("0.100000000000000001", None, "0.100000000000000001"),
            ("1.000000000000000001", Some("0.5"), "0.5"),
            ("1", Some("0.500000000000000001"), "0.500000000000000001"),
        ] {
            let mut engine =
                crate::tests::shared_sleeves::exact_single_sleeve_engine(quantity, maximum).await;
            priced(&mut engine);
            engine
                .request_portfolio_exit(StrategyId(0), SymbolId(0), Side::Buy, Exact::zero(), None)
                .unwrap();
            let request = pending_order(&mut engine).await;
            assert_reversible_order_id(&request.client_order_id);
            assert_eq!(
                request.exact_terms.as_ref().unwrap().quantity,
                Exact::parse_decimal(expected).unwrap()
            );
            assert_eq!(request.sleeve_owner(), Some(StrategyId(0)));
            let interval = engine
                .risk
                .physical_exposure_interval(SymbolId(0), &engine.books.account)
                .unwrap();
            assert_eq!(
                interval.low(),
                &(Exact::parse_decimal(quantity).unwrap()
                    - Exact::parse_decimal(expected).unwrap()),
                "quantized reservation lost canonical units"
            );
            assert!(
                engine.portfolio_controls.emergencies.is_empty(),
                "an ordinary legal exit unnecessarily flattened other sleeves"
            );
            let snapshot = engine.rotation_base(clock::wall_ms());
            let restored =
                crate::portfolio_control::PortfolioControls::replay(&[snapshot]).unwrap();
            assert_eq!(
                restored.exits[&(StrategyId(0), SymbolId(0))].target_remaining,
                Exact::zero()
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn explicit_partial_quantity_does_not_become_a_full_close_at_the_same_projection() {
        for canonical in [false, true] {
            let mut engine = crate::tests::shared_sleeves::exact_single_sleeve_engine(
                "0.100000000000000001",
                None,
            )
            .await;
            priced(&mut engine);
            let quantity = Exact::parse_decimal("0.1").unwrap();
            let intent = Intent {
                exact_prices: None,
                exact_quantity: canonical.then(|| Box::new(quantity.clone())),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Sell,
                qty: 0.1,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: true,
                tag: "partial-precision".into(),
                decided_ns: clock::now_ns(),
                work: None,
                leverage: None,
            };
            let prepared = engine
                .prepare_intent(intent, None, clock::now_ns(), &mut HashMap::new())
                .await
                .unwrap()
                .unwrap();
            let expected = if canonical {
                quantity
            } else {
                Exact::parse_decimal("0.100000000000000001").unwrap()
            };
            assert_eq!(
                prepared.request.exact_terms.as_ref().unwrap().quantity,
                expected
            );
            assert_eq!(
                engine.portfolio_controls.exits[&(StrategyId(0), SymbolId(0))].target_remaining,
                if canonical {
                    Exact::parse_decimal("0.000000000000000001").unwrap()
                } else {
                    Exact::zero()
                }
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn canonical_partial_exit_target_and_order_survive_rotation_without_projection_loss() {
        let mut engine = crate::tests::shared_sleeves::exact_single_sleeve_engine("1", None).await;
        priced(&mut engine);
        let quantity = Exact::parse_decimal("0.100000000000000001").unwrap();
        let intent = Intent {
            exact_prices: None,
            exact_quantity: Some(Box::new(quantity.clone())),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: quantity.to_f64().unwrap(),
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "canonical-partial".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        let prepared = engine
            .prepare_intent(intent, None, clock::now_ns(), &mut HashMap::new())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            prepared.request.exact_terms.as_ref().unwrap().quantity,
            quantity
        );
        let base = engine.rotation_base(clock::wall_ms());
        let restored = crate::portfolio_control::PortfolioControls::replay(&[base]).unwrap();
        assert_eq!(
            restored.exits[&(StrategyId(0), SymbolId(0))].target_remaining,
            Exact::parse_decimal("0.899999999999999999").unwrap()
        );
    }

    #[tokio::test(start_paused = true)]
    async fn mismatched_exact_intent_is_refused_before_it_can_poison_the_journal() {
        let mut engine = crate::tests::shared_sleeves::exact_single_sleeve_engine("1", None).await;
        priced(&mut engine);
        let before = engine.wal.snapshot_records().len();
        let intent = Intent {
            exact_prices: None,
            exact_quantity: Some(Box::new(Exact::parse_decimal("0.2").unwrap())),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.1,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "inconsistent-quantity".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        assert!(engine
            .prepare_intent(intent, None, clock::now_ns(), &mut HashMap::new())
            .await
            .unwrap()
            .is_none());
        assert!(engine.portfolio_controls.exits.is_empty());
        assert!(engine.wal.snapshot_records()[before..]
            .iter()
            .all(|record| !matches!(
                record,
                WalRecord::Intent { .. } | WalRecord::OrderSent { .. }
            )));
    }

    #[tokio::test(start_paused = true)]
    async fn rejected_emergency_parent_restarts_with_the_same_owned_obligation_and_new_attempt() {
        let mut engine = crate::tests::shared_sleeves::fragmented_engine().await;
        priced(&mut engine);
        engine
            .start_portfolio_emergency(
                SymbolId(0),
                Exact::parse_decimal("100").unwrap(),
                PortfolioEmergencyReason::ExitUnavailable,
            )
            .unwrap();
        let request = pending_order(&mut engine).await;
        assert_reversible_order_id(&request.client_order_id);
        acknowledge(&mut engine).await;
        let before = engine.books.attribution.snapshot();
        engine
            .take_update(OrderUpdate::Reject {
                client_order_id: request.client_order_id.clone(),
                reason: "temporary venue refusal".into(),
                code: 10001,
            })
            .await
            .unwrap();
        assert_eq!(engine.books.attribution.snapshot(), before);
        let control = engine.portfolio_controls.emergencies[&SymbolId(0)].clone();
        assert_eq!(control.phase, PortfolioEmergencyPhase::CloseNet);
        assert!(
            control.order_id.is_none(),
            "terminal attempt identity must be retired before rotation"
        );
        let base = engine.rotation_base(clock::wall_ms());
        let mut restart = crate::tests::shared_sleeves::restart_portfolio(
            &[base],
            crate::tests::shared_sleeves::physical_long(1.0),
        )
        .await;
        priced(&mut restart);
        let next = pending_order(&mut restart).await;
        assert_ne!(next.client_order_id, request.client_order_id);
        assert_eq!(next.exact_terms.as_ref().unwrap().quantity, Exact::one());
        assert_eq!(next.sleeve_owner(), None);
        assert_eq!(
            restart.portfolio_controls.emergencies[&SymbolId(0)].id,
            control.id
        );
        assert_eq!(restart.books.attribution.snapshot(), before);
    }

    #[tokio::test(start_paused = true)]
    async fn aggregate_parent_fill_failure_and_restart_preserve_each_sleeve_and_fee_once() {
        use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
        let mut engine = crate::tests::shared_sleeves::fragmented_engine().await;
        priced(&mut engine);
        engine
            .start_portfolio_emergency(
                SymbolId(0),
                Exact::parse_decimal("100").unwrap(),
                PortfolioEmergencyReason::ExitUnavailable,
            )
            .unwrap();
        let request = pending_order(&mut engine).await;
        acknowledge(&mut engine).await;
        let before = engine.books.attribution.snapshot();
        let fill = OrderUpdate::Fill {
            allocation: None,
            amounts: Some(Box::new(ExecutionAmounts {
                quantity: ExactNumber::venue_decimal("0.5").unwrap(),
                price: ExactNumber::venue_decimal("99").unwrap(),
                fee: Some(AssetAmount {
                    asset: AssetId::Named("USDT".into()),
                    amount: ExactNumber::venue_decimal("0.005").unwrap(),
                }),
                settlement_asset: AssetId::Named("USDT".into()),
            })),
            client_order_id: request.client_order_id.clone(),
            exec_id: "parent-crash-fill".into(),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.5,
            px: 99.0,
            fee: Some(0.005),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: clock::wall_ms(),
            recv_ns: clock::now_ns(),
        };
        crate::tests::shared_sleeves::fail_private_updates(&mut engine, true);
        assert!(engine.take_update(fill.clone()).await.is_err());
        assert_eq!(engine.books.attribution.snapshot(), before);
        assert_eq!(
            engine.books.orders.orders[&request.client_order_id]
                .remaining_exact()
                .unwrap(),
            Exact::one()
        );
        crate::tests::shared_sleeves::fail_private_updates(&mut engine, false);
        engine.take_update(fill.clone()).await.unwrap();
        let partial = engine.books.attribution.snapshot();
        assert_eq!(partial.positions.len(), 1);
        assert_eq!(partial.positions[0].strategy, StrategyId(1));
        assert_eq!(
            partial.positions[0].signed_qty,
            Exact::parse_decimal("0.5").unwrap()
        );
        engine
            .take_update(OrderUpdate::Cancelled {
                client_order_id: request.client_order_id.clone(),
                recv_ns: clock::now_ns(),
            })
            .await
            .unwrap();
        let base = engine.rotation_base(clock::wall_ms());
        let mut restart = crate::tests::shared_sleeves::restart_portfolio(
            &[base],
            crate::tests::shared_sleeves::physical_long(0.5),
        )
        .await;
        priced(&mut restart);
        restart.take_update(fill).await.unwrap();
        assert_eq!(
            restart.books.attribution.snapshot(),
            partial,
            "replayed venue fill allocated twice"
        );
        let next = pending_order(&mut restart).await;
        assert_ne!(next.client_order_id, request.client_order_id);
        assert_eq!(
            next.exact_terms.as_ref().unwrap().quantity,
            Exact::parse_decimal("0.5").unwrap()
        );
        assert_eq!(next.sleeve_owner(), None);
        assert_eq!(restart.books.attribution.snapshot(), partial);
        let total = partial
            .accounting
            .iter()
            .fold(Exact::zero(), |sum, row| sum + &row.fees);
        assert_eq!(total, Exact::parse_decimal("0.005").unwrap());
    }

    #[tokio::test(start_paused = true)]
    async fn a_filled_general_exit_chunk_rotates_and_resumes_with_a_new_owned_order_id() {
        use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
        let mut engine =
            crate::tests::shared_sleeves::exact_single_sleeve_engine("1", Some("0.5")).await;
        priced(&mut engine);
        engine
            .request_portfolio_exit(StrategyId(0), SymbolId(0), Side::Buy, Exact::zero(), None)
            .unwrap();
        let first = pending_order(&mut engine).await;
        acknowledge(&mut engine).await;
        engine
            .take_update(OrderUpdate::Fill {
                allocation: None,
                amounts: Some(Box::new(ExecutionAmounts {
                    quantity: ExactNumber::venue_decimal("0.5").unwrap(),
                    price: ExactNumber::venue_decimal("100").unwrap(),
                    fee: Some(AssetAmount {
                        asset: AssetId::Named("USDT".into()),
                        amount: ExactNumber::venue_decimal("0").unwrap(),
                    }),
                    settlement_asset: AssetId::Named("USDT".into()),
                })),
                exec_id: "ordinary-first-chunk".into(),
                client_order_id: first.client_order_id.clone(),
                symbol: SymbolId(0),
                side: Side::Sell,
                qty: 0.5,
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: clock::wall_ms(),
                recv_ns: clock::now_ns(),
            })
            .await
            .unwrap();
        let before = engine.books.attribution.snapshot();
        let base = engine.rotation_base(clock::wall_ms());
        let mut restart = crate::tests::shared_sleeves::restart_portfolio(
            &[base],
            crate::tests::shared_sleeves::physical_long(0.5),
        )
        .await;
        priced(&mut restart);
        let second = pending_order(&mut restart).await;
        assert_ne!(first.client_order_id, second.client_order_id);
        assert_eq!(second.sleeve_owner(), Some(StrategyId(0)));
        assert_eq!(
            second.exact_terms.as_ref().unwrap().quantity,
            Exact::parse_decimal("0.5").unwrap()
        );
        assert_eq!(restart.books.attribution.snapshot(), before);
        assert!(restart.portfolio_controls.emergencies.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn a_late_fill_after_emergency_rejection_debits_the_original_sleeves_once() {
        use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
        for rotate_before_fill in [false, true] {
            let mut engine = crate::tests::shared_sleeves::fragmented_engine().await;
            priced(&mut engine);
            engine
                .start_portfolio_emergency(
                    SymbolId(0),
                    Exact::parse_decimal("100").unwrap(),
                    PortfolioEmergencyReason::ExitUnavailable,
                )
                .unwrap();
            let request = pending_order(&mut engine).await;
            acknowledge(&mut engine).await;
            engine
                .take_update(OrderUpdate::Reject {
                    client_order_id: request.client_order_id.clone(),
                    code: 10001,
                    reason: "late venue rejection".into(),
                })
                .await
                .unwrap();
            if rotate_before_fill {
                let base = engine.rotation_base(clock::wall_ms());
                engine = crate::tests::shared_sleeves::restart_portfolio(
                    &[base],
                    crate::tests::shared_sleeves::physical_long(1.0),
                )
                .await;
                priced(&mut engine);
                assert!(!engine.books.orders.orders[&request.client_order_id].in_flight());
            }
            let fill = OrderUpdate::Fill {
                allocation: None,
                amounts: Some(Box::new(ExecutionAmounts {
                    quantity: ExactNumber::venue_decimal("0.5").unwrap(),
                    price: ExactNumber::venue_decimal("100").unwrap(),
                    fee: Some(AssetAmount {
                        asset: AssetId::Named("USDT".into()),
                        amount: ExactNumber::venue_decimal("0.005").unwrap(),
                    }),
                    settlement_asset: AssetId::Named("USDT".into()),
                })),
                exec_id: "late-rejected-parent-fill".into(),
                client_order_id: request.client_order_id.clone(),
                symbol: SymbolId(0),
                side: Side::Sell,
                qty: 0.5,
                px: 100.0,
                fee: Some(0.005),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: clock::wall_ms(),
                recv_ns: clock::now_ns(),
            };
            engine.take_update(fill.clone()).await.unwrap();
            let once = engine.books.attribution.snapshot();
            assert_eq!(once.positions.len(), 1);
            assert_eq!(once.positions[0].strategy, StrategyId(1));
            assert_eq!(
                once.positions[0].signed_qty,
                Exact::parse_decimal("0.5").unwrap()
            );
            engine.take_update(fill.clone()).await.unwrap();
            assert_eq!(engine.books.attribution.snapshot(), once);
            let base = engine.rotation_base(clock::wall_ms());
            let mut restart = crate::tests::shared_sleeves::restart_portfolio(
                &[base],
                crate::tests::shared_sleeves::physical_long(0.5),
            )
            .await;
            priced(&mut restart);
            restart.take_update(fill).await.unwrap();
            assert_eq!(restart.books.attribution.snapshot(), once);
            let next = pending_order(&mut restart).await;
            assert_ne!(next.client_order_id, request.client_order_id);
            assert_eq!(
                next.exact_terms.as_ref().unwrap().quantity,
                Exact::parse_decimal("0.5").unwrap()
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn legacy_quantity_only_rotation_adopts_native_grid_before_general_exit() {
        use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
        let mut engine = crate::tests::shared_sleeves::legacy_single_sleeve_engine(
            0.2899999999999999,
            "0.29",
            "0.01",
        )
        .await;
        priced(&mut engine);
        let matched = super::super::account_recovery::history_account_matches(
            &engine.books.account,
            &engine.logged_exposure,
        )
        .unwrap();
        engine
            .request_portfolio_exit(StrategyId(0), SymbolId(0), Side::Buy, Exact::zero(), None)
            .unwrap();
        let first = pending_order(&mut engine).await;
        let sent = first.exact_terms.as_ref().unwrap().quantity.clone();
        acknowledge(&mut engine).await;
        engine
            .take_update(OrderUpdate::Fill {
                allocation: None,
                amounts: Some(Box::new(ExecutionAmounts {
                    quantity: ExactNumber::venue_decimal(&sent.to_decimal_string().unwrap())
                        .unwrap(),
                    price: ExactNumber::venue_decimal("100").unwrap(),
                    fee: Some(AssetAmount {
                        asset: AssetId::Named("USDT".into()),
                        amount: ExactNumber::venue_decimal("0").unwrap(),
                    }),
                    settlement_asset: AssetId::Named("USDT".into()),
                })),
                exec_id: "legacy-general-exit".into(),
                client_order_id: first.client_order_id,
                symbol: SymbolId(0),
                side: Side::Sell,
                qty: sent.to_f64().unwrap(),
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: clock::wall_ms(),
                recv_ns: clock::now_ns(),
            })
            .await
            .unwrap();
        let remaining = engine.books.attribution.snapshot().positions;
        for _ in 0..300 {
            engine.service_order_dispatches().await.unwrap();
            engine.service_portfolio_controls().await.unwrap();
            tokio::task::yield_now().await;
            std::thread::sleep(Duration::from_millis(5));
        }
        eprintln!("history_account_matches={matched}, sent={sent:?}, remaining={remaining:?}, in_flight={}, exits={}, emergencies={}",
            engine.books.orders.in_flight().len(), engine.portfolio_controls.exits.len(), engine.portfolio_controls.emergencies.len());
        assert_eq!(
            sent,
            Exact::parse_decimal("0.29").unwrap(),
            "legacy migration rounded a full close down by one venue step"
        );
        assert!(
            matched,
            "unchanged native account failed its migration boundary"
        );
        assert!(
            remaining.is_empty(),
            "legacy full close left non-executable binary dust"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn legacy_grid_adoption_precedes_a_full_native_stop_during_downtime() {
        use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
        let stopped = engine_types::VenueExecution {
            exec_id: "native-legacy-full-stop".into(),
            client_order_id: String::new(),
            symbol: "BTCUSDT".into(),
            side: Side::Sell,
            qty: 0.29,
            px: 90.0,
            fee: Some(0.0066016),
            amounts: Some(ExecutionAmounts {
                quantity: ExactNumber::venue_decimal("0.29").unwrap(),
                price: ExactNumber::venue_decimal("90").unwrap(),
                fee: Some(AssetAmount {
                    asset: AssetId::Named("USDT".into()),
                    amount: ExactNumber::venue_decimal("0.0066016").unwrap(),
                }),
                settlement_asset: AssetId::Named("USDT".into()),
            }),
            is_maker: false,
            forced_close: Some(engine_types::ForcedClose::StopLoss),
            venue_ts_ms: clock::wall_ms() - 10,
        };
        let engine = crate::tests::shared_sleeves::legacy_single_sleeve_recovery(
            0.2899999999999999,
            "0",
            "0.01",
            vec![stopped],
            None,
            None,
        )
        .await
        .unwrap();
        assert!(engine.books.attribution.snapshot().positions.is_empty());
        assert!(engine.logged_exposure.is_empty());
        assert!(engine.fills.open_trade_lots().is_empty());
        let records = engine.wal.snapshot_records();
        let adopted = records
            .iter()
            .position(|row| matches!(row, WalRecord::LegacyQuantityGridAdopted { .. }))
            .unwrap();
        let recovered=records.iter().position(|row|matches!(row,WalRecord::RecoveredFill{exec_id,..} if exec_id=="native-legacy-full-stop")).unwrap();
        assert!(adopted < recovered);
        assert!(!records.iter().any(|row|matches!(row,WalRecord::Reconciled{findings,..} if findings.iter().any(|finding|finding.contains("cannot allocate")))));
        assert!(super::super::account_recovery::history_account_matches(
            &engine.books.account,
            &engine.logged_exposure
        )
        .unwrap());
        let portfolio = engine.books.attribution.snapshot();
        assert_eq!(
            portfolio
                .accounting
                .iter()
                .fold(Exact::zero(), |sum, row| sum + &row.fees),
            Exact::parse_decimal("0.0066016").unwrap()
        );
        assert!(portfolio.unvalued.iter().any(|row| row.legacy_prefix));
    }

    #[tokio::test(start_paused = true)]
    async fn legacy_grid_adoption_barrier_failure_stops_boot_before_recovery() {
        let result = crate::tests::shared_sleeves::legacy_single_sleeve_recovery(
            0.2899999999999999,
            "0.29",
            "0.01",
            vec![],
            None,
            Some("legacy_quantity_grid_adopted_v2"),
        )
        .await;
        let error = result
            .err()
            .expect("boot ignored the grid adoption durability failure");
        assert!(error.to_string().contains("test barrier failure"));
    }
}
