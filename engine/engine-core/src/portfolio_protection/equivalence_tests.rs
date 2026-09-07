use super::*;
use engine_types::numeric::ExactNumber;
use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
use engine_types::orders::SleeveOrderEffect;
use engine_types::portfolio::PortfolioPosition;
use engine_types::{StopSpec, StrategyId, SymbolId, TimeInForce};

pub(crate) fn d(text: &str) -> Exact {
    Exact::parse_decimal(text).unwrap()
}
pub(crate) fn spec() -> ExactInstrumentSpec {
    crate::tests::shared_sleeves::spec()
}
pub(crate) fn request(side: Side, reducing: bool) -> OrderRequest {
    let stop = if side == Side::Buy { 90.0 } else { 110.0 };
    let mut request = OrderRequest {
        client_order_id: "planned".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side,
        qty: 1.0,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: reducing,
        close_position: reducing,
        sleeve_effect: Some(if reducing {
            SleeveOrderEffect::Reduce
        } else {
            SleeveOrderEffect::Increase {
                stop: StopSpec { trigger_px: stop },
            }
        }),
        exact_terms: None,
    };
    ExactOrderTerms {
        quantity: d("1"),
        limit_price: None,
        stop_trigger_price: (!reducing).then(|| ExactNumber::legacy_binary64(stop).unwrap().value),
        physical_stop_trigger_price: (!reducing)
            .then(|| ExactNumber::legacy_binary64(stop).unwrap().value),
        input_policy: OrderInputPolicy::CanonicalPortfolio,
    }
    .apply_projection(&mut request)
    .unwrap();
    request
}
pub(crate) fn observe(
    result: Result<ProtectionPlan, String>,
) -> Result<(bool, Option<ExactStopTerms>), String> {
    result.map(|p| (p.reduce_only, p.native_stop))
}
fn row(owner: u16, qty: &str, stop: Option<&str>) -> PortfolioPosition {
    PortfolioPosition {
        strategy: StrategyId(owner),
        symbol: SymbolId(0),
        signed_qty: d(qty),
        entry_value: Some(d(qty).abs() * d("100")),
        stop_px: stop.map(d),
        settlement_asset: engine_types::numeric::AssetId::Unknown,
    }
}
pub(crate) fn reference_plan(
    portfolio: &PortfolioState,
    request: &OrderRequest,
    before: PhysicalExposureInterval,
    spec: &ExactInstrumentSpec,
    reference: &Exact,
    other_stops: impl IntoIterator<Item = (Side, Exact)>,
) -> Result<ProtectionPlan, String> {
    let quantity = request
        .exact_terms
        .as_ref()
        .map(|terms| Ok(terms.quantity.clone()))
        .unwrap_or_else(|| Exact::from_legacy_f64(request.qty))
        .map_err(|e| e.to_string())?;
    if before.certainly_reduces(request.side, &quantity) {
        return Ok(ProtectionPlan {
            reduce_only: true,
            native_stop: None,
        });
    }
    let after = before
        .after(request.side, &quantity)
        .map_err(|e| format!("{e:?}"))?;
    let side = if !after.low().is_negative() && after.high().is_positive() {
        Side::Buy
    } else if !after.high().is_positive() && after.low().is_negative() {
        Side::Sell
    } else if after.low().is_zero() && after.high().is_zero() {
        return Ok(ProtectionPlan {
            reduce_only: true,
            native_stop: None,
        });
    } else {
        return Err("outstanding orders leave the physical position direction unresolved".into());
    };
    if side != request.side {
        return Err("a physical growth order must protect the direction it can create".into());
    }
    let delta = if request.side == Side::Buy {
        quantity
    } else {
        Exact::zero() - quantity
    };
    let logical_stop = request
        .exact_terms
        .as_ref()
        .and_then(|terms| terms.stop_trigger_price.clone())
        .or_else(|| {
            request
                .sleeve_stop()
                .and_then(|stop| Exact::from_legacy_f64(stop.trigger_px).ok())
        });
    let mut candidates = Vec::new();
    let mut owner_seen = false;
    for row in portfolio
        .positions
        .iter()
        .filter(|row| row.symbol == request.symbol)
    {
        let mut quantity = row.signed_qty.clone();
        let mut stop = row.stop_px.clone();
        if row.strategy == request.strategy {
            owner_seen = true;
            quantity += &delta;
            if !request.is_sleeve_reduction() {
                stop = if row.signed_qty.signum() == delta.signum() {
                    tighter(side, stop, logical_stop.clone())
                } else {
                    logical_stop.clone()
                };
            }
        }
        if quantity.is_zero() || quantity.is_positive() != (side == Side::Buy) {
            continue;
        }
        candidates.push(stop.ok_or("a surviving sleeve has no durable stop")?);
    }
    if !owner_seen && !request.is_sleeve_reduction() {
        candidates.push(logical_stop.ok_or("the new sleeve has no durable stop")?);
    }
    candidates.extend(
        other_stops
            .into_iter()
            .filter(|(stop_side, _)| *stop_side == side)
            .map(|(_, stop)| stop),
    );
    let trigger = candidates
        .into_iter()
        .reduce(|a, b| match side {
            Side::Buy => a.max(b),
            Side::Sell => a.min(b),
        })
        .ok_or("physical growth has no surviving sleeve protection")?;
    let mut reference = reference.clone();
    if let OrderKind::Limit { px, .. } = request.kind {
        let limit = request
            .exact_terms
            .as_ref()
            .and_then(|terms| terms.limit_price.clone())
            .map(Ok)
            .unwrap_or_else(|| Exact::from_legacy_f64(px))
            .map_err(|e| e.to_string())?;
        reference = match side {
            Side::Buy => reference.min(limit),
            Side::Sell => reference.max(limit),
        };
    }
    let native_stop =
        ExactStopTerms::quantize(spec, side, &trigger, &reference).map_err(|e| e.to_string())?;
    Ok(ProtectionPlan {
        reduce_only: false,
        native_stop: Some(native_stop),
    })
}

#[test]
fn physical_stop_candidate_fold_matches_the_original_planner() {
    let portfolios = [
        vec![],
        vec![row(0, "1", Some("90"))],
        vec![row(0, "-1", Some("110"))],
        vec![row(0, "1", Some("90")), row(1, "2", Some("95"))],
        vec![row(0, "1", Some("90")), row(1, "-1", Some("110"))],
        vec![row(0, "1", None), row(1, "-1", None)],
    ];
    let intervals = [(-2.0, -2.0), (0.0, 0.0), (2.0, 2.0), (-1.0, 1.0)];
    let others = [
        vec![],
        vec![(Side::Buy, d("95")), (Side::Sell, d("105"))],
        vec![
            (Side::Buy, d("95")),
            (Side::Buy, d("95")),
            (Side::Sell, d("105")),
            (Side::Sell, d("105")),
        ],
        vec![(Side::Buy, d("0")), (Side::Sell, d("0"))],
    ];
    let (mut cases, mut allowed, mut denied) = (0, 0, 0);
    for (portfolio_index, positions) in portfolios.iter().enumerate() {
        let portfolio = PortfolioState {
            positions: positions.clone(),
            ..Default::default()
        };
        for side in [Side::Buy, Side::Sell] {
            for reducing in [false, true] {
                for limit in [false, true] {
                    let mut request = request(side, reducing);
                    if limit {
                        request.kind = OrderKind::Limit {
                            px: 100.0,
                            tif: TimeInForce::Gtc,
                        };
                        request.exact_terms.as_mut().unwrap().limit_price = Some(d("100"));
                    }
                    for (low, high) in intervals {
                        for (other_index, stops) in others.iter().enumerate() {
                            let interval = PhysicalExposureInterval::try_new(low, high).unwrap();
                            let expected = observe(reference_plan(
                                &portfolio,
                                &request,
                                interval.clone(),
                                &spec(),
                                &d("100"),
                                stops.clone(),
                            ));
                            let actual = observe(plan(
                                &portfolio,
                                &request,
                                interval,
                                &spec(),
                                &d("100"),
                                stops.clone(),
                            ));
                            assert_eq!(actual, expected, "portfolio={portfolio_index}, side={side:?}, reducing={reducing}, limit={limit}, interval={low}..{high}, others={other_index}");
                            if actual.is_ok() {
                                allowed += 1;
                            } else {
                                denied += 1;
                            }
                            cases += 1;
                        }
                    }
                }
            }
        }
    }
    assert_eq!(cases, 768);
    assert!(allowed > 0 && denied > 0);
    eprintln!(
        "physical stop planner oracle: {cases} cases, {allowed} exact plans, {denied} exact errors"
    );
}
