use super::*;
use crate::portfolio_protection::equivalence_tests::{observe, request, spec};
use crate::tests::{callback_test_fixture, shared_sleeves};
use engine_types::Quote;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    fn reference_physical_order_plan(
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
        let plan = crate::portfolio_protection::equivalence_tests::reference_plan(
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
}

#[tokio::test(start_paused = true)]
async fn physical_stop_candidate_fold_preserves_eager_legacy_errors() {
    let (mut cases, mut allowed, mut denied) = (0, 0, 0);
    for side in [Side::Buy, Side::Sell] {
        for held in [None, Some(side), Some(side.flipped())] {
            for variant in 0..13 {
                let (mut engine, records) =
                    callback_test_fixture(vec![shared_sleeves::idle("owner")]).await;
                engine.private_stream_ready = true;
                engine.may_open = true;
                engine.max_quote_age_ns = u64::MAX;
                engine.books.market.quotes[0] = Quote {
                    bid_px: 99.0,
                    ask_px: 101.0,
                    recv_ns: clock::now_ns().max(1),
                    ..Default::default()
                };
                engine.books.account.positions.clear();
                if let Some(held) = held {
                    let mut position = shared_sleeves::physical_long(1.0).remove(0);
                    position.side = held;
                    position.stop_px = if held == Side::Buy { 90.0 } else { 110.0 };
                    engine.books.account.positions.push(position);
                }
                let mut intent = request(side, held == Some(side.flipped()));
                let mut excluded = None;
                for index in 0..3 {
                    let pending_side = if index == 1 { side.flipped() } else { side };
                    let mut pending = request(pending_side, false);
                    pending.client_order_id = format!("pending-{index}");
                    engine.books.orders.apply(&WalRecord::OrderSent {
                        dispatch: None,
                        request: pending,
                        wire_ns: 1,
                        arrival_mid: 100.0,
                    });
                }
                for index in 0..3 {
                    let id = format!("pending-{index}");
                    let row = engine.books.orders.orders.get_mut(&id).unwrap();
                    match variant {
                        0 => {}
                        1 => {
                            row.request.exact_terms = None;
                        }
                        2 => {
                            row.request.exact_terms = None;
                            row.request.stop = None;
                            row.request.sleeve_effect = None;
                        }
                        3 if index == 1 => {
                            row.request.exact_terms = None;
                            row.request.stop = Some(StopSpec {
                                trigger_px: f64::NAN,
                            });
                            row.request.sleeve_effect = None;
                        }
                        4 if index == 0 => {
                            row.request.exact_terms = None;
                            row.request.stop = Some(StopSpec {
                                trigger_px: f64::INFINITY,
                            });
                            row.request.sleeve_effect = None;
                        }
                        5 if index == 0 => {
                            row.request.exact_terms = None;
                            row.request.stop = Some(StopSpec {
                                trigger_px: f64::NAN,
                            });
                            row.request.sleeve_effect = None;
                            excluded = Some("pending-0");
                        }
                        6 if index == 0 => {
                            row.request.exact_terms = None;
                            row.request.stop = Some(StopSpec {
                                trigger_px: f64::NAN,
                            });
                            row.request.sleeve_effect =
                                Some(engine_types::orders::SleeveOrderEffect::Reduce);
                        }
                        7 if index == 0 => {
                            row.request.exact_terms = None;
                            row.request.stop = Some(StopSpec {
                                trigger_px: f64::NAN,
                            });
                            row.request.sleeve_effect = None;
                            row.ending = Some(crate::inflight::Ending::Cancelled);
                        }
                        8 if index == 0 => {
                            row.request.exact_terms = None;
                            row.request.stop = Some(StopSpec {
                                trigger_px: f64::NAN,
                            });
                            row.request.sleeve_effect = None;
                            row.request.symbol = SymbolId(1);
                        }
                        9 if index == 1 => {
                            row.request.exact_terms = None;
                            row.request.stop = Some(StopSpec { trigger_px: -1.0 });
                            row.request.sleeve_effect = None;
                        }
                        10 if index == 0 => {
                            row.request.stop = Some(StopSpec {
                                trigger_px: f64::NAN,
                            });
                            row.request.sleeve_effect = None;
                        }
                        _ => {}
                    }
                }
                if variant == 11 {
                    let mut malformed = shared_sleeves::physical_long(0.0).remove(0);
                    malformed.side = side.flipped();
                    malformed.stop_px = f64::NAN;
                    engine.books.account.positions.push(malformed);
                    intent.stop = None;
                    intent.sleeve_effect = None;
                    intent.exact_terms.as_mut().unwrap().stop_trigger_price = None;
                }
                if variant == 12 {
                    engine.books.market.quotes[0] = Quote::default();
                    engine.books.market.tickers[0] = engine_types::Ticker::default();
                    let row = engine.books.orders.orders.get_mut("pending-0").unwrap();
                    row.request.exact_terms = None;
                    row.request.stop = Some(StopSpec {
                        trigger_px: f64::NAN,
                    });
                    row.request.sleeve_effect = None;
                }
                let before = serde_json::to_vec(&*records.lock().unwrap()).unwrap();
                let expected =
                    observe(engine.reference_physical_order_plan(&intent, &spec(), excluded));
                let actual = observe(engine.physical_order_plan(&intent, &spec(), excluded));
                assert_eq!(
                    actual, expected,
                    "side={side:?}, held={held:?}, variant={variant}"
                );
                assert_eq!(
                    serde_json::to_vec(&*records.lock().unwrap()).unwrap(),
                    before
                );
                if [3, 4, 11].contains(&variant) {
                    assert!(actual.is_err(), "invalid legacy stop must be read eagerly");
                }
                if variant == 12 {
                    assert_eq!(
                        actual.as_ref().unwrap_err(),
                        "no reference price for physical protection"
                    );
                }
                if actual.is_ok() {
                    allowed += 1;
                } else {
                    denied += 1;
                }
                cases += 1;
            }
        }
    }
    assert_eq!(cases, 78);
    assert!(allowed > 0 && denied > 0);
    eprintln!("physical stop collection oracle: {cases} cases, {allowed} exact plans, {denied} exact errors");
}
