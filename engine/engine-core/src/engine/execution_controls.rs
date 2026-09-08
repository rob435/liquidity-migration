use super::*;
use engine_types::numeric::Exact;
use engine_types::orders::IntentPrices;
use engine_types::StopSpec;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn cap_entry_stop_distance(&self, intent: &mut Intent) -> Result<(), String> {
        if intent.reduce_only {
            return Ok(());
        }
        let leverage = self
            .books
            .account
            .positions
            .iter()
            .filter(|position| position.symbol == intent.symbol)
            .filter_map(|position| position.leverage)
            .chain(intent.leverage)
            .filter(|value| value.is_finite() && *value > 0.0)
            .reduce(f64::max);
        let Some(cap) = self.risk.stop_distance_cap(leverage) else {
            return Ok(());
        };
        let Some(stop) = intent.stop_price().map_err(|e| e.to_string())? else {
            return Ok(());
        };
        let limit = intent.limit_price().map_err(|e| e.to_string())?;
        let entry = match &limit {
            Some(px) => px.clone(),
            None => engine_types::order_terms::strategy_decimal(
                self.reference_px(intent.symbol, &intent.kind)
                    .ok_or("stop cap: executable reference is unavailable")?,
            )
            .map_err(|e| e.to_string())?,
        };
        if !stop.is_positive()
            || (intent.side == Side::Buy && stop >= entry)
            || (intent.side == Side::Sell && stop <= entry)
        {
            return Err("stop cap: requested stop is on the wrong side of entry".into());
        }
        let fraction =
            engine_types::order_terms::strategy_decimal(cap).map_err(|e| e.to_string())?;
        let bound = &entry
            * if intent.side == Side::Buy {
                Exact::one() - &fraction
            } else {
                Exact::one() + &fraction
            };
        let stop = if intent.side == Side::Buy {
            stop.max(bound)
        } else {
            stop.min(bound)
        };
        intent.stop = Some(StopSpec {
            trigger_px: stop.to_f64().map_err(|e| e.to_string())?,
        });
        intent.exact_prices = Some(Box::new(IntentPrices {
            limit_price: limit,
            stop_trigger_price: Some(stop),
        }));
        Ok(())
    }
    fn mark_collar(&self, symbol: SymbolId) -> Result<Option<(Exact, Exact)>, String> {
        let Some(limits) = &self.execution_limits else {
            return Ok(None);
        };
        let now = clock::now_ns();
        let ticker = self.books.market.ticker(symbol);
        let mark = if ticker.recv_ns > 0
            && ticker.recv_ns <= now
            && now - ticker.recv_ns <= self.max_quote_age_ns
            && ticker.mark_px.is_finite()
            && ticker.mark_px > 0.0
        {
            Some(ticker.mark_px)
        } else if self.books.account.observed_ns > 0
            && self.books.account.observed_ns <= now
            && now - self.books.account.observed_ns <= self.max_quote_age_ns
        {
            self.books
                .account
                .positions
                .iter()
                .find(|p| p.symbol == symbol)
                .and_then(|p| p.exact_amounts.as_deref())
                .and_then(|p| p.mark_price.as_ref())
                .and_then(|p| p.value.to_f64().ok())
                .filter(|p| p.is_finite() && *p > 0.0)
        } else {
            None
        }
        .ok_or("price_collar: fresh mark price is unavailable")?;
        let mark = engine_types::order_terms::strategy_decimal(mark).map_err(|e| e.to_string())?;
        let fraction =
            engine_types::order_terms::strategy_decimal(limits.mark_collar_bps / 10_000.0)
                .map_err(|e| e.to_string())?;
        Ok(Some((
            &mark * (Exact::one() - &fraction),
            &mark * (Exact::one() + &fraction),
        )))
    }

    pub(super) fn collar_intent(&self, intent: &mut Intent) -> Result<(), String> {
        let Some((low, high)) = self.mark_collar(intent.symbol)? else {
            return Ok(());
        };
        match intent.kind {
            OrderKind::Market => {
                let bound = if intent.side == Side::Buy { high } else { low };
                intent.kind = OrderKind::Limit {
                    px: bound.to_f64().map_err(|e| e.to_string())?,
                    tif: TimeInForce::Ioc,
                };
                let stop = intent.stop_price().map_err(|e| e.to_string())?;
                intent.exact_prices = Some(Box::new(IntentPrices {
                    limit_price: Some(bound),
                    stop_trigger_price: stop,
                }));
            }
            OrderKind::Limit { .. } => {
                let px = intent
                    .limit_price()
                    .map_err(|e| e.to_string())?
                    .ok_or("price_collar: limit price is absent")?;
                if px < low || px > high {
                    return Err("price_collar: limit is outside the mark band".into());
                }
            }
        }
        Ok(())
    }

    pub(super) fn collar_amend(&self, symbol: SymbolId, spec: &AmendSpec) -> Result<(), String> {
        let Some((low, high)) = self.mark_collar(symbol)? else {
            return Ok(());
        };
        let px = match spec
            .exact_terms
            .as_deref()
            .and_then(|t| t.limit_price.clone())
        {
            Some(px) => px,
            None => engine_types::order_terms::strategy_decimal(
                spec.px.ok_or("price_collar: amend price is absent")?,
            )
            .map_err(|e| e.to_string())?,
        };
        if px < low || px > high {
            return Err("price_collar: amendment is outside the mark band".into());
        }
        Ok(())
    }

    pub(super) fn note_order_rejection(&mut self, id: &str) -> Result<(), EngineError> {
        if !self.books.orders.orders.contains_key(id) {
            return Ok(());
        }
        let Some(limits) = &self.execution_limits else {
            return Ok(());
        };
        let now = clock::now_ns();
        let window = limits.reject_window_ms.saturating_mul(1_000_000);
        self.recent_rejections
            .retain(|(at, _)| now.saturating_sub(*at) <= window);
        if self.recent_rejections.iter().any(|(_, known)| known == id) {
            return Ok(());
        }
        self.recent_rejections.push_back((now, id.to_owned()));
        if !self.may_open || self.recent_rejections.len() < limits.reject_limit {
            return Ok(());
        }
        self.may_open = false;
        self.portfolio_dirty = true;
        self.wal.append(&WalRecord::Reconciled {
            wall_ts_ms: clock::wall_ms(),
            may_open: false,
            findings: vec![format!(
                "reject storm: {} distinct orders rejected within {}ms",
                self.recent_rejections.len(),
                limits.reject_window_ms
            )],
        })?;
        let openings: Vec<_> = self
            .books
            .orders
            .in_flight()
            .into_iter()
            .filter(|order| !order.request.is_sleeve_reduction())
            .map(|order| (order.request.symbol, order.request.client_order_id.clone()))
            .collect();
        for (symbol, id) in openings {
            self.enqueue_halt_cancel(symbol, id);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn limits() -> crate::config::ExecutionLimits {
        crate::config::ExecutionLimits {
            mark_collar_bps: 100.0,
            reject_limit: 5,
            reject_window_ms: 10_000,
        }
    }
    fn priced<W: Wal, R: RiskKernel, V: VenueGateway>(engine: &mut Engine<W, R, V>) {
        engine.execution_limits = Some(limits());
        engine.books.market.apply(&MarketEvent::Ticker {
            symbol: SymbolId(0),
            ticker: engine_types::Ticker {
                mark_px: 100.0,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
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
    fn intent(side: Side, reduce_only: bool) -> Intent {
        Intent {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side,
            qty: if reduce_only { 1.0 } else { 0.1 },
            kind: OrderKind::Market,
            stop: (!reduce_only).then_some(StopSpec { trigger_px: 90.0 }),
            reduce_only,
            exact_prices: None,
            exact_quantity: None,
            tag: "collar-fixture".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        }
    }

    #[tokio::test(start_paused = true)]
    async fn mark_collar_prices_both_entry_and_exit_before_risk_and_durability() {
        for (side, reducing, expected) in [(Side::Buy, false, 101.0), (Side::Sell, true, 99.0)] {
            let mut engine =
                crate::tests::shared_sleeves::exact_single_sleeve_engine("1", None).await;
            priced(&mut engine);
            let prepared = engine
                .prepare_intent(
                    intent(side, reducing),
                    None,
                    clock::now_ns(),
                    None,
                    &mut Default::default(),
                )
                .await
                .unwrap()
                .unwrap();
            assert_eq!(
                prepared.request.kind,
                OrderKind::Limit {
                    px: expected,
                    tif: TimeInForce::Ioc
                }
            );
            assert_eq!(
                prepared.request.exact_terms.as_ref().unwrap().limit_price,
                Some(Exact::parse_decimal(&expected.to_string()).unwrap())
            );
            assert_eq!(prepared.request.is_sleeve_reduction(), reducing);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn an_incoming_wide_stop_is_capped_before_the_risk_reservation() {
        for (observed_leverage, expected) in [(None, 75.8), (Some(10.0), 96.0)] {
            let mut engine =
                crate::tests::shared_sleeves::exact_single_sleeve_engine("1", None).await;
            priced(&mut engine);
            engine.enforce_position_stop_intent().await.unwrap();
            engine.books.account.positions[0].leverage = observed_leverage;
            let mut request = intent(Side::Buy, false);
            request.stop = Some(StopSpec { trigger_px: 60.0 });
            let prepared = engine
                .prepare_intent(
                    request,
                    None,
                    clock::now_ns(),
                    None,
                    &mut Default::default(),
                )
                .await
                .unwrap()
                .unwrap_or_else(|| panic!("entry refused: {:?}", engine.wal.snapshot_records()));
            assert_eq!(
                prepared.request.sleeve_stop().unwrap().trigger_px,
                expected,
                "stop distance uses configured and observed leverage, then rounds inward"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn mark_collar_refuses_bad_limits_amends_and_stale_or_absent_marks() {
        let mut engine = crate::tests::shared_sleeves::exact_single_sleeve_engine("1", None).await;
        priced(&mut engine);
        let mut request = intent(Side::Buy, false);
        request.kind = OrderKind::Limit {
            px: 102.0,
            tif: TimeInForce::PostOnly,
        };
        assert!(engine.collar_intent(&mut request).is_err());
        assert!(engine
            .collar_amend(
                SymbolId(0),
                &AmendSpec {
                    px: Some(98.9),
                    qty: None,
                    exact_terms: None
                }
            )
            .is_err());
        engine.books.market.apply(&MarketEvent::Ticker {
            symbol: SymbolId(0),
            ticker: Default::default(),
        });
        assert!(engine.collar_intent(&mut intent(Side::Sell, true)).is_err());
        priced(&mut engine);
        let _clock =
            engine_types::clock::install_virtual(clock::wall_ns(), clock::now_ns()).unwrap();
        engine_types::clock::advance_virtual_to(clock::now_ns() + 61_000_000_000).unwrap();
        assert!(engine.collar_intent(&mut intent(Side::Buy, false)).is_err());
    }

    #[tokio::test(start_paused = true)]
    async fn emergency_closes_use_an_ioc_mark_collar_and_preserve_exact_dust() {
        for quantity in ["1", "0.000000000000000001"] {
            let mut engine =
                crate::tests::shared_sleeves::exact_single_sleeve_engine(quantity, None).await;
            priced(&mut engine);
            engine
                .start_portfolio_emergency(
                    SymbolId(0),
                    Exact::from_u64(100),
                    engine_types::portfolio_control::PortfolioEmergencyReason::ExitUnavailable,
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
            assert_eq!(orders.len(), 1);
            let request = &orders[0].request;
            assert_eq!(
                request.kind,
                OrderKind::Limit {
                    px: 99.0,
                    tif: TimeInForce::Ioc
                }
            );
            assert_eq!(
                request.exact_terms.as_ref().unwrap().quantity,
                Exact::parse_decimal(quantity).unwrap()
            );
            engine
                .portfolio_controls
                .validate_engine_order(request)
                .unwrap();
        }
    }

    #[tokio::test(start_paused = true)]
    async fn distinct_venue_rejections_latch_openings_once_and_leave_exits_available() {
        let (mut engine, records) =
            crate::tests::callback_test_fixture(vec![crate::tests::shared_sleeves::idle("left")])
                .await;
        engine.execution_limits = Some(limits());
        for index in 0..7 {
            let reducing = index == 6;
            let request = OrderRequest {
                client_order_id: format!("reject-{index}"),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: if reducing { Side::Sell } else { Side::Buy },
                qty: 1.0,
                kind: OrderKind::Limit {
                    px: 100.0,
                    tif: TimeInForce::Gtc,
                },
                stop: (!reducing).then_some(StopSpec { trigger_px: 90.0 }),
                reduce_only: reducing,
                close_position: false,
                exact_terms: None,
                sleeve_effect: None,
            };
            let record = WalRecord::OrderSent {
                dispatch: None,
                request,
                arrival_mid: 100.0,
                wire_ns: clock::now_ns(),
            };
            engine.books.orders.apply(&record);
            engine
                .books
                .registry
                .own(&format!("reject-{index}"), StrategyId(0));
            engine.wal.append(&record).unwrap();
        }
        for index in [0, 0, 1, 2, 3] {
            engine
                .take_update(OrderUpdate::Reject {
                    client_order_id: format!("reject-{index}"),
                    code: 10001,
                    reason: "fixture".into(),
                })
                .await
                .unwrap();
            assert!(
                engine.may_open,
                "four distinct rejections must not trip a five-order limit"
            );
        }
        engine
            .take_update(OrderUpdate::Reject {
                client_order_id: "reject-4".into(),
                code: 10001,
                reason: "fixture".into(),
            })
            .await
            .unwrap();
        assert!(!engine.may_open);
        assert!(engine
            .halt_cancel_queue
            .iter()
            .any(|(_, id)| id == "reject-5"));
        assert!(!engine
            .halt_cancel_queue
            .iter()
            .any(|(_, id)| id == "reject-6"));
        assert_eq!(records.lock().unwrap().iter().filter(|r| matches!(r, WalRecord::Reconciled { may_open: false, findings, .. } if findings.iter().any(|s| s.starts_with("reject storm:")))).count(), 1);
    }
}
