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
        let Ok(expected) = expected.to_f64() else {
            return false;
        };
        let rows: Vec<_> = self
            .books
            .account
            .positions
            .iter()
            .filter(|row| row.symbol == symbol && row.qty != 0.0)
            .collect();
        let observed = match rows.as_slice() {
            [] => 0.0,
            [row] if row.qty.is_finite() && row.qty > 0.0 => {
                if row.side == Side::Buy {
                    row.qty
                } else {
                    -row.qty
                }
            }
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
        let requested = engine_types::order_terms::strategy_decimal(intent.qty)
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
        let qty = (remaining - &exit.target_remaining)
            .to_f64()
            .map_err(|e| EngineError::State(e.to_string()))?
            * if exit.position_side == Side::Buy {
                1.0
            } else {
                -1.0
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
            exit.order_id = Some(format!("eng-px-{}-{}", exit.id, exit.attempt));
            return self.append_portfolio_control(WalRecord::PortfolioExitChanged { state: exit });
        }
        self.portfolio_controls.attempted(exit.id, exit.attempt);
        let intent = self.portfolio_exit_intent(
            exit.strategy,
            exit.symbol,
            qty,
            format!("portfolio-exit:{}", exit.id),
        );
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
        signed_qty: f64,
        tag: String,
    ) -> Intent {
        Intent {
            strategy,
            symbol,
            side: if signed_qty > 0.0 {
                Side::Sell
            } else {
                Side::Buy
            },
            qty: signed_qty.abs(),
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag,
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        }
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
                    state.order_id = Some(format!("eng-pe-{}-{}", state.id, state.attempt));
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
                    .unwrap_or_else(Exact::zero)
                    .to_f64()
                    .map_err(|e| EngineError::State(e.to_string()))?;
                if interval.low() != expected
                    || interval.high() != expected
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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
}
