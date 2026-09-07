use super::*;

struct Reference<'a>(&'a ExactOrderTerms);
impl Reference<'_> {
    fn validate_storage(&self) -> Result<(), OrderLegalityError> {
        for value in std::iter::once(&self.0.quantity)
            .chain(self.0.limit_price.iter())
            .chain(self.0.stop_trigger_price.iter())
            .chain(self.0.physical_stop_trigger_price.iter())
        {
            decimal_wire(value)?;
            value.to_f64()?;
        }
        Ok(())
    }

    fn validate_projection(&self, request: &OrderRequest) -> Result<(), OrderLegalityError> {
        self.validate_storage()?;
        let price = match request.kind {
            OrderKind::Market => None,
            OrderKind::Limit { px, .. } => Some(px),
        };
        let sleeve_stop = request.sleeve_stop().map(|s| s.trigger_px);
        if self.0.quantity.to_f64()? != request.qty
            || self.0.limit_price.as_ref().map(Exact::to_f64).transpose()? != price
            || self
                .0
                .stop_trigger_price
                .as_ref()
                .map(Exact::to_f64)
                .transpose()?
                != sleeve_stop
            || self
                .0
                .physical_stop_trigger_price
                .as_ref()
                .map(Exact::to_f64)
                .transpose()?
                != request.stop.map(|stop| stop.trigger_px)
        {
            return Err(OrderLegalityError::Projection);
        }
        Ok(())
    }

    fn apply_projection(&self, request: &mut OrderRequest) -> Result<(), OrderLegalityError> {
        self.validate_storage()?;
        request.qty = self.0.quantity.to_f64()?;
        match (&mut request.kind, &self.0.limit_price) {
            (OrderKind::Market, None) => (),
            (OrderKind::Limit { px, .. }, Some(exact)) => *px = exact.to_f64()?,
            _ => return Err(OrderLegalityError::Projection),
        }
        let stop = self
            .0
            .stop_trigger_price
            .as_ref()
            .map(|px| px.to_f64().map(|trigger_px| StopSpec { trigger_px }))
            .transpose()?;
        request.stop = self
            .0
            .physical_stop_trigger_price
            .as_ref()
            .map(|px| px.to_f64().map(|trigger_px| StopSpec { trigger_px }))
            .transpose()?;
        if let Some(SleeveOrderEffect::Increase { stop: held }) = &mut request.sleeve_effect {
            *held = stop.ok_or(OrderLegalityError::Projection)?;
        }
        request.exact_terms = Some(Box::new(self.0.clone()));
        self.validate_projection(request)
    }
}

fn outcome(result: Result<(), OrderLegalityError>) -> Result<(), String> {
    result.map_err(|error| format!("{error:?}"))
}

#[test]
fn projection_matches_original_values_errors_and_partial_mutation() {
    let values = [
        "0",
        "-1",
        "0.001",
        "1.000000000000000001",
        "77777.5",
        "1e-300",
        "1e300",
        "1e-400",
        "1e400",
    ]
    .map(|text| Exact::parse_decimal(text).unwrap());
    let mut options = vec![None];
    options.extend(values.iter().cloned().map(Some));
    options.push(Some(Exact::from_ratio("1", "3").unwrap()));
    let mut terms = ExactOrderTerms {
        quantity: Exact::one(),
        limit_price: None,
        stop_trigger_price: None,
        physical_stop_trigger_price: None,
        input_policy: OrderInputPolicy::CanonicalPortfolio,
    };
    for step in 0..512 {
        terms.quantity = values[step % values.len()].clone();
        terms.limit_price = options[(step / 3) % options.len()].clone();
        terms.stop_trigger_price = options[(step / 7) % options.len()].clone();
        terms.physical_stop_trigger_price = options[(step / 13) % options.len()].clone();
        let canonical = serde_json::to_vec(&terms).unwrap();
        let reference = Reference(&terms);
        assert_eq!(
            outcome(terms.validate_storage()),
            outcome(reference.validate_storage()),
            "storage {step}"
        );
        for kind in [
            OrderKind::Market,
            OrderKind::Limit {
                px: 10.0,
                tif: crate::TimeInForce::Gtc,
            },
        ] {
            for sleeve_effect in [
                None,
                Some(SleeveOrderEffect::Reduce),
                Some(SleeveOrderEffect::Increase {
                    stop: StopSpec { trigger_px: 9.0 },
                }),
            ] {
                let request = OrderRequest {
                    client_order_id: "projection".into(),
                    strategy: crate::StrategyId(0),
                    symbol: crate::SymbolId(0),
                    side: Side::Buy,
                    qty: 3.0,
                    kind,
                    stop: Some(StopSpec { trigger_px: 8.0 }),
                    reduce_only: false,
                    close_position: false,
                    exact_terms: None,
                    sleeve_effect,
                };
                assert_eq!(
                    outcome(terms.validate_projection(&request)),
                    outcome(reference.validate_projection(&request)),
                    "validate {step}"
                );
                let mut actual = request.clone();
                let mut expected = request;
                assert_eq!(
                    outcome(terms.apply_projection(&mut actual)),
                    outcome(reference.apply_projection(&mut expected)),
                    "apply {step}"
                );
                assert_eq!(
                    serde_json::to_vec(&actual).unwrap(),
                    serde_json::to_vec(&expected).unwrap(),
                    "partial mutation {step}"
                );
                assert_eq!(
                    outcome(terms.validate_projection(&actual)),
                    outcome(reference.validate_projection(&expected)),
                    "applied validation {step}"
                );
                assert_eq!(serde_json::to_vec(&terms).unwrap(), canonical);
            }
        }
    }
}
