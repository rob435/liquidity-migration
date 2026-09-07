use engine_types::numeric::{Exact, ExactInstrumentSpec};
use engine_types::order_terms::ExactStopTerms;
use engine_types::portfolio::PortfolioState;
use engine_types::risk::PhysicalExposureInterval;
use engine_types::{OrderKind, OrderRequest, Side};

pub(crate) struct ProtectionPlan {
    pub reduce_only: bool,
    pub native_stop: Option<ExactStopTerms>,
}

/// Outstanding orders contribute their logical stop only on the resulting net side.
pub(crate) fn plan(
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
    let mut trigger = None;
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
        trigger = tighter(
            side,
            trigger,
            Some(stop.ok_or("a surviving sleeve has no durable stop")?),
        );
    }
    if !owner_seen && !request.is_sleeve_reduction() {
        trigger = tighter(
            side,
            trigger,
            Some(logical_stop.ok_or("the new sleeve has no durable stop")?),
        );
    }
    for (_, stop) in other_stops
        .into_iter()
        .filter(|(stop_side, _)| *stop_side == side)
    {
        trigger = tighter(side, trigger, Some(stop));
    }
    let trigger = trigger.ok_or("physical growth has no surviving sleeve protection")?;
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

fn tighter(side: Side, a: Option<Exact>, b: Option<Exact>) -> Option<Exact> {
    match (a, b) {
        (Some(a), Some(b)) => Some(match side {
            Side::Buy => a.max(b),
            Side::Sell => a.min(b),
        }),
        (a, b) => a.or(b),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::numeric::{AssetId, PricePrecision};
    use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
    use engine_types::orders::SleeveOrderEffect;
    use engine_types::portfolio::PortfolioPosition;
    use engine_types::{StopSpec, StrategyId, SymbolId};
    fn d(s: &str) -> Exact {
        Exact::parse_decimal(s).unwrap()
    }
    fn spec() -> ExactInstrumentSpec {
        ExactInstrumentSpec {
            native_symbol: "BTCUSDT".into(),
            base_asset: AssetId::Unknown,
            quote_asset: AssetId::Unknown,
            settlement_asset: AssetId::Unknown,
            tick_size: Some(d("0.1")),
            min_price: None,
            max_price: None,
            price_precision: PricePrecision::Tick,
            qty_step: Some(d("0.1")),
            min_qty: None,
            market_qty_step: Some(d("0.1")),
            market_min_qty: None,
            max_qty: None,
            max_market_qty: None,
            min_notional: None,
            contract_multiplier: Some(d("1")),
            fee_assets: None,
            fee_step: None,
        }
    }
    fn row(id: u16, qty: &str, stop: &str) -> PortfolioPosition {
        PortfolioPosition {
            strategy: StrategyId(id),
            symbol: SymbolId(0),
            signed_qty: d(qty),
            entry_value: Some(d(qty).abs() * d("100")),
            stop_px: Some(d(stop)),
            settlement_asset: AssetId::Unknown,
        }
    }
    fn request(id: u16, side: Side, qty: &str, stop: Option<&str>) -> OrderRequest {
        let logical = stop.map(d);
        let terms = ExactOrderTerms {
            quantity: d(qty),
            limit_price: None,
            stop_trigger_price: logical.clone(),
            physical_stop_trigger_price: logical,
            input_policy: OrderInputPolicy::StrategyShortestDecimal,
        };
        let mut request = OrderRequest {
            client_order_id: "planned".into(),
            strategy: StrategyId(id),
            symbol: SymbolId(0),
            side,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: stop.is_none(),
            close_position: false,
            sleeve_effect: Some(
                stop.map(|s| SleeveOrderEffect::Increase {
                    stop: StopSpec {
                        trigger_px: s.parse().unwrap(),
                    },
                })
                .unwrap_or(SleeveOrderEffect::Reduce),
            ),
            exact_terms: Some(Box::new(terms.clone())),
        };
        terms.apply_projection(&mut request).unwrap();
        request
    }
    fn interval(low: f64, high: f64) -> PhysicalExposureInterval {
        PhysicalExposureInterval::try_new(low, high).unwrap()
    }
    #[test]
    fn closing_one_of_opposite_sleeves_protects_the_surviving_side() {
        let portfolio = PortfolioState {
            positions: vec![row(0, "1", "90.1"), row(1, "-1", "110.1")],
            ..Default::default()
        };
        for (id, side, stop) in [(0, Side::Sell, "110.1"), (1, Side::Buy, "90.1")] {
            let plan = plan(
                &portfolio,
                &request(id, side, "1", None),
                interval(0.0, 0.0),
                &spec(),
                &d("100"),
                [],
            )
            .unwrap();
            assert!(!plan.reduce_only);
            let native = plan.native_stop.unwrap();
            assert_eq!(native.position_side, side);
            assert_eq!(native.trigger_price, d(stop));
        }
    }
    #[test]
    fn a_virtual_entry_can_be_a_physical_reduction_without_borrowing_its_stop() {
        let portfolio = PortfolioState {
            positions: vec![row(0, "1", "90")],
            ..Default::default()
        };
        let request = request(1, Side::Sell, "1", Some("110"));
        let plan = plan(
            &portfolio,
            &request,
            interval(1.0, 1.0),
            &spec(),
            &d("100"),
            [],
        )
        .unwrap();
        assert!(plan.reduce_only);
        assert!(plan.native_stop.is_none());
        assert_eq!(
            request.exact_terms.as_ref().unwrap().stop_trigger_price,
            Some(d("110"))
        );
    }
    #[test]
    fn pending_orders_cannot_leave_growth_protected_on_the_wrong_side() {
        let portfolio = PortfolioState {
            positions: vec![row(0, "1", "90"), row(1, "-1", "110")],
            ..Default::default()
        };
        assert!(plan(
            &portfolio,
            &request(0, Side::Sell, "1", None),
            interval(-1.0, 2.0),
            &spec(),
            &d("100"),
            []
        )
        .is_err());
        let mut missing = portfolio;
        missing.positions[1].stop_px = None;
        assert!(plan(
            &missing,
            &request(0, Side::Sell, "1", None),
            interval(0.0, 0.0),
            &spec(),
            &d("100"),
            [(Side::Buy, d("90"))]
        )
        .is_err());
    }
    #[test]
    fn same_side_pending_stops_tighten_native_protection_without_changing_sleeve_policy() {
        let portfolio = PortfolioState {
            positions: vec![row(0, "1", "90")],
            ..Default::default()
        };
        let request = request(1, Side::Buy, "1", Some("85"));
        let plan = plan(
            &portfolio,
            &request,
            interval(1.0, 2.0),
            &spec(),
            &d("100"),
            [(Side::Buy, d("95")), (Side::Sell, d("110"))],
        )
        .unwrap();
        assert_eq!(plan.native_stop.unwrap().trigger_price, d("95"));
        assert_eq!(request.sleeve_stop().unwrap().trigger_px, 85.0);
    }
}

#[cfg(test)]
pub(crate) mod equivalence_tests;
