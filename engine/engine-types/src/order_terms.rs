//! Exact order terms are computed once and survive WAL replay without wire rounding.
use serde::{Deserialize, Serialize};

use crate::numeric::{Exact, ExactError, ExactInstrumentSpec, PricePrecision};
use crate::orders::{AmendSpec, OrderKind, OrderRequest, Side, SleeveOrderEffect, StopSpec};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum OrderInputPolicy {
    /// Strategy binary64 inputs use their shortest round-trip decimal spelling.
    /// This preserves 0.29 as 29 decimal cents without relabeling execution data.
    StrategyShortestDecimal,
    CanonicalPortfolio,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExactOrderTerms {
    pub quantity: Exact,
    pub limit_price: Option<Exact>,
    pub stop_trigger_price: Option<Exact>,
    pub physical_stop_trigger_price: Option<Exact>,
    pub input_policy: OrderInputPolicy,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExactAmendTerms {
    pub quantity: Option<Exact>,
    pub limit_price: Option<Exact>,
    pub input_policy: OrderInputPolicy,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExactAmendedTerms {
    pub price: crate::numeric::ExactNumber,
    pub quantity: crate::numeric::ExactNumber,
}
impl ExactAmendedTerms {
    pub fn validate_projection(&self, px: f64, qty: f64) -> Result<(), OrderLegalityError> {
        self.price.value.validate_storage()?;
        self.quantity.value.validate_storage()?;
        self.price.validate_provenance()?;
        self.quantity.validate_provenance()?;
        if !self.price.value.is_positive() || self.quantity.value.is_negative() {
            return Err(OrderLegalityError::Constraint("amended order amounts"));
        }
        if self.price.value.to_f64()? != px || self.quantity.value.to_f64()? != qty {
            return Err(OrderLegalityError::Projection);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExactStopTerms {
    pub trigger_price: Exact,
    pub position_side: Side,
    pub reference_price: Exact,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum QuantityPolicy {
    Normal,
    /// The caller must first establish the venue's full-position-close capability.
    CloseEntirePosition,
}

#[derive(Debug, thiserror::Error)]
pub enum OrderLegalityError {
    #[error("invalid exact order amount: {0}")]
    Number(#[from] ExactError),
    #[error("required order capability unavailable: {0}")]
    Unavailable(&'static str),
    #[error("order violates venue constraint: {0}")]
    Constraint(&'static str),
    #[error("exact terms disagree with compatibility order fields")]
    Projection,
}

pub fn strategy_decimal(value: f64) -> Result<Exact, OrderLegalityError> {
    if !value.is_finite() || value <= 0.0 {
        return Err(OrderLegalityError::Constraint("positive finite input"));
    }
    Ok(Exact::parse_decimal(&value.to_string())?)
}

pub fn decimal_wire(value: &Exact) -> Result<String, OrderLegalityError> {
    value.validate_storage()?;
    if !value.is_positive() {
        return Err(OrderLegalityError::Constraint("positive wire amount"));
    }
    value
        .to_decimal_string()
        .ok_or(OrderLegalityError::Constraint("finite decimal wire amount"))
}

fn required_positive<'a>(
    value: &'a Option<Exact>,
    field: &'static str,
) -> Result<&'a Exact, OrderLegalityError> {
    let value = value
        .as_ref()
        .ok_or(OrderLegalityError::Unavailable(field))?;
    decimal_wire(value)?;
    Ok(value)
}

fn check_bounds(
    value: &Exact,
    min: &Option<Exact>,
    max: &Option<Exact>,
    field: &'static str,
) -> Result<(), OrderLegalityError> {
    for bound in min.iter().chain(max.iter()) {
        bound.validate_storage()?;
        if bound.is_negative() {
            return Err(OrderLegalityError::Constraint("negative metadata bound"));
        }
    }
    if min.as_ref().is_some_and(|min| value < min) || max.as_ref().is_some_and(|max| value > max) {
        return Err(OrderLegalityError::Constraint(field));
    }
    Ok(())
}

fn magnitude(value: &Exact) -> Result<i32, OrderLegalityError> {
    let text = decimal_wire(value)?;
    let (whole, fraction) = text.split_once('.').unwrap_or((&text, ""));
    if whole != "0" {
        Ok(whole.len() as i32 - 1)
    } else {
        fraction
            .bytes()
            .position(|b| b != b'0')
            .map(|at| -(at as i32) - 1)
            .ok_or(OrderLegalityError::Constraint("positive price"))
    }
}

fn price_step(value: &Exact, spec: &ExactInstrumentSpec) -> Result<Exact, OrderLegalityError> {
    match spec.price_precision {
        PricePrecision::Tick => Ok(required_positive(&spec.tick_size, "price tick")?.clone()),
        PricePrecision::Unavailable => Err(OrderLegalityError::Unavailable("price precision")),
        PricePrecision::SignificantFigures {
            max_digits,
            max_decimals,
            integer_exception,
        } => {
            if max_digits == 0 || max_digits > 4096 || max_decimals > 4096 {
                return Err(OrderLegalityError::Constraint("price precision range"));
            }
            let exponent = (magnitude(value)? - max_digits as i32 + 1).max(-(max_decimals as i32));
            let exponent = if integer_exception {
                exponent.min(0)
            } else {
                exponent
            };
            Ok(Exact::parse_decimal(&format!("1e{exponent}"))?)
        }
    }
}

pub fn quantize_price(
    value: &Exact,
    side: Side,
    spec: &ExactInstrumentSpec,
) -> Result<Exact, OrderLegalityError> {
    let step = price_step(value, spec)?;
    let snapped = match side {
        Side::Buy => value.floor_to(&step)?,
        Side::Sell => value.ceil_to(&step)?,
    };
    decimal_wire(&snapped)?;
    // A power-of-ten carry can change the significant-figure precision.
    let final_step = price_step(&snapped, spec)?;
    if !snapped.is_multiple_of(&final_step)? {
        return Err(OrderLegalityError::Constraint("price significant figures"));
    }
    check_bounds(&snapped, &spec.min_price, &spec.max_price, "price bounds")?;
    Ok(snapped)
}

pub fn quantize_order(
    spec: &ExactInstrumentSpec,
    side: Side,
    qty: f64,
    kind: OrderKind,
    stop: Option<StopSpec>,
    reference_px: Option<f64>,
    policy: QuantityPolicy,
) -> Result<ExactOrderTerms, OrderLegalityError> {
    quantize_with_quantity(
        spec,
        side,
        (
            strategy_decimal(qty)?,
            OrderInputPolicy::StrategyShortestDecimal,
        ),
        kind,
        stop,
        reference_px,
        policy,
    )
}

pub fn quantize_portfolio_close(
    spec: &ExactInstrumentSpec,
    side: Side,
    quantity: &Exact,
    reference_px: Option<f64>,
    policy: QuantityPolicy,
) -> Result<ExactOrderTerms, OrderLegalityError> {
    quantize_with_quantity(
        spec,
        side,
        (quantity.clone(), OrderInputPolicy::CanonicalPortfolio),
        OrderKind::Market,
        None,
        reference_px,
        policy,
    )
}

fn quantize_with_quantity(
    spec: &ExactInstrumentSpec,
    side: Side,
    input: (Exact, OrderInputPolicy),
    kind: OrderKind,
    stop: Option<StopSpec>,
    reference_px: Option<f64>,
    policy: QuantityPolicy,
) -> Result<ExactOrderTerms, OrderLegalityError> {
    let (input_qty, input_policy) = input;
    input_qty.validate_storage()?;
    let market = matches!(kind, OrderKind::Market);
    let quantity = if policy == QuantityPolicy::CloseEntirePosition {
        if !market {
            return Err(OrderLegalityError::Constraint(
                "full-position close must be market",
            ));
        }
        input_qty
    } else {
        let (step, min, max) = if market {
            (
                &spec.market_qty_step,
                &spec.market_min_qty,
                &spec.max_market_qty,
            )
        } else {
            (&spec.qty_step, &spec.min_qty, &spec.max_qty)
        };
        let quantity = input_qty.floor_to(required_positive(step, "quantity step")?)?;
        decimal_wire(&quantity)?;
        check_bounds(&quantity, min, max, "quantity bounds")?;
        quantity
    };
    let limit_price = match kind {
        OrderKind::Market => None,
        OrderKind::Limit { px, .. } => Some(quantize_price(&strategy_decimal(px)?, side, spec)?),
    };
    let reference = reference_px.map(strategy_decimal).transpose()?;
    if policy != QuantityPolicy::CloseEntirePosition {
        if let Some(min) = &spec.min_notional {
            if min.is_negative() {
                return Err(OrderLegalityError::Constraint("negative minimum notional"));
            }
            min.validate_storage()?;
            if min.is_positive() {
                let price = limit_price
                    .as_ref()
                    .or(reference.as_ref())
                    .ok_or(OrderLegalityError::Unavailable("notional reference price"))?;
                if &quantity * price < *min {
                    return Err(OrderLegalityError::Constraint("minimum notional"));
                }
            }
        }
    }
    let stop_trigger_price = stop
        .map(|stop| {
            let trigger =
                quantize_price(&strategy_decimal(stop.trigger_px)?, side.flipped(), spec)?;
            let reference = reference
                .as_ref()
                .ok_or(OrderLegalityError::Unavailable("stop reference price"))?;
            if match side {
                Side::Buy => {
                    &trigger >= reference
                        || limit_price.as_ref().is_some_and(|limit| &trigger >= limit)
                }
                Side::Sell => {
                    &trigger <= reference
                        || limit_price.as_ref().is_some_and(|limit| &trigger <= limit)
                }
            } {
                return Err(OrderLegalityError::Constraint(
                    "stop crosses current reference or order limit",
                ));
            }
            Ok(trigger)
        })
        .transpose()?;
    let terms = ExactOrderTerms {
        quantity,
        limit_price,
        physical_stop_trigger_price: stop_trigger_price.clone(),
        stop_trigger_price,
        input_policy,
    };
    terms.validate_storage()?;
    Ok(terms)
}

impl ExactOrderTerms {
    pub fn with_physical_stop(
        mut self,
        trigger: Option<Exact>,
    ) -> Result<Self, OrderLegalityError> {
        self.physical_stop_trigger_price = trigger;
        self.validate_storage()?;
        Ok(self)
    }

    pub fn validate_wire_grid(
        &self,
        spec: &ExactInstrumentSpec,
        kind: OrderKind,
        policy: QuantityPolicy,
    ) -> Result<(), OrderLegalityError> {
        self.validate_storage()?;
        if policy != QuantityPolicy::CloseEntirePosition {
            let (step, min, max) = if matches!(kind, OrderKind::Market) {
                (
                    &spec.market_qty_step,
                    &spec.market_min_qty,
                    &spec.max_market_qty,
                )
            } else {
                (&spec.qty_step, &spec.min_qty, &spec.max_qty)
            };
            if !self
                .quantity
                .is_multiple_of(required_positive(step, "quantity step")?)?
            {
                return Err(OrderLegalityError::Constraint("quantity grid"));
            }
            check_bounds(&self.quantity, min, max, "quantity bounds")?;
        }
        for price in self
            .limit_price
            .iter()
            .chain(self.stop_trigger_price.iter())
            .chain(self.physical_stop_trigger_price.iter())
        {
            if quantize_price(price, Side::Buy, spec)? != *price {
                return Err(OrderLegalityError::Constraint("price grid"));
            }
        }
        Ok(())
    }

    pub fn validate_storage(&self) -> Result<(), OrderLegalityError> {
        for value in std::iter::once(&self.quantity)
            .chain(self.limit_price.iter())
            .chain(self.stop_trigger_price.iter())
            .chain(self.physical_stop_trigger_price.iter())
        {
            decimal_wire(value)?;
            value.to_f64()?;
        }
        Ok(())
    }

    pub fn validate_projection(&self, request: &OrderRequest) -> Result<(), OrderLegalityError> {
        self.validate_storage()?;
        let price = match request.kind {
            OrderKind::Market => None,
            OrderKind::Limit { px, .. } => Some(px),
        };
        let sleeve_stop = request.sleeve_stop().map(|s| s.trigger_px);
        if self.quantity.to_f64()? != request.qty
            || self.limit_price.as_ref().map(Exact::to_f64).transpose()? != price
            || self
                .stop_trigger_price
                .as_ref()
                .map(Exact::to_f64)
                .transpose()?
                != sleeve_stop
            || self
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

    pub fn apply_projection(&self, request: &mut OrderRequest) -> Result<(), OrderLegalityError> {
        self.validate_storage()?;
        request.qty = self.quantity.to_f64()?;
        match (&mut request.kind, &self.limit_price) {
            (OrderKind::Market, None) => (),
            (OrderKind::Limit { px, .. }, Some(exact)) => *px = exact.to_f64()?,
            _ => return Err(OrderLegalityError::Projection),
        }
        let stop = self
            .stop_trigger_price
            .as_ref()
            .map(|px| px.to_f64().map(|trigger_px| StopSpec { trigger_px }))
            .transpose()?;
        request.stop = self
            .physical_stop_trigger_price
            .as_ref()
            .map(|px| px.to_f64().map(|trigger_px| StopSpec { trigger_px }))
            .transpose()?;
        if let Some(SleeveOrderEffect::Increase { stop: held }) = &mut request.sleeve_effect {
            *held = stop.ok_or(OrderLegalityError::Projection)?;
        }
        request.exact_terms = Some(Box::new(self.clone()));
        self.validate_projection(request)
    }
}

impl ExactAmendTerms {
    pub fn validate_wire_grid(&self, spec: &ExactInstrumentSpec) -> Result<(), OrderLegalityError> {
        if let Some(qty) = &self.quantity {
            decimal_wire(qty)?;
            if !qty.is_multiple_of(required_positive(&spec.qty_step, "quantity step")?)? {
                return Err(OrderLegalityError::Constraint("amend quantity grid"));
            }
            check_bounds(qty, &spec.min_qty, &spec.max_qty, "amend quantity bounds")?;
        }
        if let Some(px) = &self.limit_price {
            if quantize_price(px, Side::Buy, spec)? != *px {
                return Err(OrderLegalityError::Constraint("amend price grid"));
            }
        }
        Ok(())
    }

    pub fn validate_projection(&self, amendment: &AmendSpec) -> Result<(), OrderLegalityError> {
        if self.quantity.is_none() && self.limit_price.is_none() {
            return Err(OrderLegalityError::Constraint("empty amendment"));
        }
        for value in self.quantity.iter().chain(self.limit_price.iter()) {
            decimal_wire(value)?;
            value.to_f64()?;
        }
        if self.quantity.as_ref().map(Exact::to_f64).transpose()? != amendment.qty
            || self.limit_price.as_ref().map(Exact::to_f64).transpose()? != amendment.px
        {
            return Err(OrderLegalityError::Projection);
        }
        Ok(())
    }

    pub fn apply_projection(&self, amendment: &mut AmendSpec) -> Result<(), OrderLegalityError> {
        amendment.qty = self.quantity.as_ref().map(Exact::to_f64).transpose()?;
        amendment.px = self.limit_price.as_ref().map(Exact::to_f64).transpose()?;
        self.validate_projection(amendment)?;
        amendment.exact_terms = Some(Box::new(self.clone()));
        Ok(())
    }
}

pub fn quantize_amend(
    spec: &ExactInstrumentSpec,
    current: &OrderRequest,
    amendment: &AmendSpec,
    reference_px: Option<f64>,
) -> Result<ExactAmendTerms, OrderLegalityError> {
    if amendment.px.is_none() && amendment.qty.is_none() {
        return Err(OrderLegalityError::Constraint("empty amendment"));
    }
    let OrderKind::Limit { px: current_px, .. } = current.kind else {
        return Err(OrderLegalityError::Constraint(
            "only limit orders can be amended",
        ));
    };
    if let Some(terms) = current.exact_terms.as_deref() {
        terms.validate_projection(current)?;
    }
    let quantity = amendment
        .qty
        .map(|qty| {
            strategy_decimal(qty)?
                .floor_to(required_positive(&spec.qty_step, "quantity step")?)
                .map_err(OrderLegalityError::from)
        })
        .transpose()?;
    let limit_price = amendment
        .px
        .map(|px| quantize_price(&strategy_decimal(px)?, current.side, spec))
        .transpose()?;
    let full_quantity = match &quantity {
        Some(qty) => qty.clone(),
        None => match &current.exact_terms {
            Some(terms) => terms.quantity.clone(),
            None => strategy_decimal(current.qty)?,
        },
    };
    let full_price = match &limit_price {
        Some(px) => px.clone(),
        None => match &current.exact_terms {
            Some(terms) => terms
                .limit_price
                .clone()
                .ok_or(OrderLegalityError::Projection)?,
            None => strategy_decimal(current_px)?,
        },
    };
    let projected = ExactOrderTerms {
        quantity: full_quantity,
        limit_price: Some(full_price.clone()),
        stop_trigger_price: None,
        physical_stop_trigger_price: None,
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    };
    projected.validate_wire_grid(spec, current.kind, QuantityPolicy::Normal)?;
    if let Some(min) = &spec.min_notional {
        min.validate_storage()?;
        if min.is_negative() {
            return Err(OrderLegalityError::Constraint("negative minimum notional"));
        }
        if &projected.quantity * &full_price < *min {
            return Err(OrderLegalityError::Constraint("minimum notional"));
        }
    }
    if let Some(stop) = current.sleeve_stop() {
        let trigger = match current
            .exact_terms
            .as_deref()
            .and_then(|terms| terms.stop_trigger_price.as_ref())
        {
            Some(trigger) => trigger.clone(),
            None => strategy_decimal(stop.trigger_px)?,
        };
        let reference = reference_px
            .map(strategy_decimal)
            .transpose()?
            .ok_or(OrderLegalityError::Unavailable("stop reference price"))?;
        if match current.side {
            Side::Buy => trigger >= full_price || trigger >= reference,
            Side::Sell => trigger <= full_price || trigger <= reference,
        } {
            return Err(OrderLegalityError::Constraint(
                "amend crosses protective stop",
            ));
        }
    }
    Ok(ExactAmendTerms {
        quantity,
        limit_price,
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    })
}

impl ExactStopTerms {
    pub fn quantize(
        spec: &ExactInstrumentSpec,
        position_side: Side,
        trigger: &Exact,
        reference: &Exact,
    ) -> Result<Self, OrderLegalityError> {
        let terms = Self {
            trigger_price: quantize_price(trigger, position_side.flipped(), spec)?,
            position_side,
            reference_price: reference.clone(),
        };
        terms.validate_storage()?;
        Ok(terms)
    }

    pub fn validate_storage(&self) -> Result<(), OrderLegalityError> {
        decimal_wire(&self.trigger_price)?;
        decimal_wire(&self.reference_price)?;
        self.trigger_price.to_f64()?;
        self.reference_price.to_f64()?;
        if match self.position_side {
            Side::Buy => self.trigger_price >= self.reference_price,
            Side::Sell => self.trigger_price <= self.reference_price,
        } {
            return Err(OrderLegalityError::Constraint(
                "stop crosses current reference",
            ));
        }
        Ok(())
    }

    pub fn validate_wire_grid(&self, spec: &ExactInstrumentSpec) -> Result<(), OrderLegalityError> {
        self.validate_storage()?;
        if quantize_price(&self.trigger_price, Side::Buy, spec)? != self.trigger_price {
            return Err(OrderLegalityError::Constraint("stop price grid"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::numeric::AssetId;
    use crate::{StrategyId, SymbolId, TimeInForce};
    fn d(text: &str) -> Exact {
        Exact::parse_decimal(text).unwrap()
    }
    fn spec() -> ExactInstrumentSpec {
        ExactInstrumentSpec {
            native_symbol: "TEST".into(),
            base_asset: AssetId::Named("TEST".into()),
            quote_asset: AssetId::Unknown,
            settlement_asset: AssetId::Unknown,
            tick_size: Some(d("0.01")),
            min_price: None,
            max_price: None,
            price_precision: PricePrecision::Tick,
            qty_step: Some(d("0.01")),
            min_qty: Some(d("0.01")),
            market_qty_step: Some(d("0.1")),
            market_min_qty: Some(d("0.1")),
            max_qty: Some(d("100")),
            max_market_qty: Some(d("10")),
            min_notional: None,
            contract_multiplier: Some(Exact::one()),
            fee_assets: None,
            fee_step: None,
        }
    }
    fn limit(px: f64) -> OrderKind {
        OrderKind::Limit {
            px,
            tif: TimeInForce::Gtc,
        }
    }
    #[test]
    fn standalone_stops_round_earlier_without_crossing_the_current_reference() {
        let long = ExactStopTerms::quantize(&spec(), Side::Buy, &d("1.001"), &d("1.1")).unwrap();
        assert_eq!(long.trigger_price, d("1.01"));
        let short = ExactStopTerms::quantize(&spec(), Side::Sell, &d("1.099"), &d("1.0")).unwrap();
        assert_eq!(short.trigger_price, d("1.09"));
        assert!(ExactStopTerms::quantize(&spec(), Side::Buy, &d("1.001"), &d("1.01")).is_err());
        assert!(ExactStopTerms::quantize(&spec(), Side::Sell, &d("1.009"), &d("1.0")).is_err());
        long.validate_wire_grid(&spec()).unwrap();
    }

    #[test]
    fn amendment_fields_are_optional_but_the_assembled_order_remains_legal() {
        let mut current = OrderRequest {
            client_order_id: "amend".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: limit(1.0),
            stop: None,
            reduce_only: false,
            close_position: false,
            sleeve_effect: None,
            exact_terms: None,
        };
        let amendment = AmendSpec {
            px: Some(1.009),
            qty: None,
            exact_terms: None,
        };
        let terms = quantize_amend(&spec(), &current, &amendment, None).unwrap();
        assert_eq!(terms.limit_price, Some(d("1")));
        assert_eq!(terms.quantity, None);
        let mut projected = amendment.clone();
        terms.apply_projection(&mut projected).unwrap();
        assert_eq!(projected.px, Some(1.0));
        let mut s = spec();
        s.qty_step = Some(d("1e-21"));
        s.min_qty = s.qty_step.clone();
        s.min_notional = Some(d("0.123456789012345679"));
        ExactOrderTerms {
            quantity: d("0.123456789012345678901"),
            limit_price: Some(d("1")),
            stop_trigger_price: None,
            physical_stop_trigger_price: None,
            input_policy: OrderInputPolicy::StrategyShortestDecimal,
        }
        .apply_projection(&mut current)
        .unwrap();
        assert!(
            quantize_amend(&s, &current, &amendment, None).is_err(),
            "unchanged exact quantity was relabeled through a float"
        );
        let too_small = AmendSpec {
            qty: Some(0.0001),
            px: None,
            exact_terms: None,
        };
        assert!(quantize_amend(&spec(), &current, &too_small, None).is_err());
        projected.px = Some(2.0);
        assert!(terms.validate_projection(&projected).is_err());
        let legacy: AmendSpec = serde_json::from_str(r#"{"px":1.0,"qty":null}"#).unwrap();
        assert_eq!(
            serde_json::to_string(&legacy).unwrap(),
            r#"{"px":1.0,"qty":null}"#
        );
    }

    #[test]
    fn strategy_conversion_preserves_decimal_ticks_without_erasing_a_real_ulp() {
        for qty in [0.29, 0.3, 8.2] {
            let terms = quantize_order(
                &spec(),
                Side::Buy,
                qty,
                limit(1.0),
                None,
                None,
                QuantityPolicy::Normal,
            )
            .unwrap();
            assert_eq!(terms.quantity, d(&qty.to_string()));
        }
        let below = f64::from_bits(1f64.to_bits() - 1);
        assert_eq!(
            quantize_order(
                &spec(),
                Side::Buy,
                below,
                limit(1.0),
                None,
                None,
                QuantityPolicy::Normal
            )
            .unwrap()
            .quantity,
            d("0.99")
        );
        assert_eq!(
            quantize_order(
                &spec(),
                Side::Buy,
                0.30000000000000004,
                limit(1.0),
                None,
                None,
                QuantityPolicy::Normal
            )
            .unwrap()
            .quantity,
            d("0.3")
        );
    }
    #[test]
    fn independent_market_lots_and_ceilings_apply_and_zero_is_an_explicit_error() {
        let s = spec();
        assert_eq!(
            quantize_order(
                &s,
                Side::Buy,
                0.29,
                OrderKind::Market,
                None,
                None,
                QuantityPolicy::Normal
            )
            .unwrap()
            .quantity,
            d("0.2")
        );
        for qty in [0.001, 0.0, -1.0, f64::INFINITY, 10.1] {
            assert!(
                quantize_order(
                    &s,
                    Side::Sell,
                    qty,
                    OrderKind::Market,
                    None,
                    None,
                    QuantityPolicy::Normal
                )
                .is_err(),
                "{qty}"
            );
        }
        assert!(quantize_order(
            &s,
            Side::Sell,
            10.1,
            limit(1.0),
            None,
            None,
            QuantityPolicy::Normal
        )
        .is_ok());
    }
    #[test]
    fn tiny_exact_terms_survive_a_decimal_wire_round_trip() {
        let mut s = spec();
        s.qty_step = Some(d("0.0000000000001"));
        s.min_qty = s.qty_step.clone();
        s.tick_size = s.qty_step.clone();
        let terms = quantize_order(
            &s,
            Side::Buy,
            0.1234567890123,
            limit(0.0000000000003),
            None,
            None,
            QuantityPolicy::Normal,
        )
        .unwrap();
        assert_eq!(decimal_wire(&terms.quantity).unwrap(), "0.1234567890123");
        assert_eq!(
            decimal_wire(terms.limit_price.as_ref().unwrap()).unwrap(),
            "0.0000000000003"
        );
        assert_eq!(
            serde_json::from_str::<ExactOrderTerms>(&serde_json::to_string(&terms).unwrap())
                .unwrap(),
            terms
        );
    }
    #[test]
    fn passive_prices_and_stops_use_opposite_rounding_and_never_cross_reference() {
        let s = spec();
        let long = quantize_order(
            &s,
            Side::Buy,
            1.0,
            limit(1.239),
            Some(StopSpec { trigger_px: 1.201 }),
            Some(1.3),
            QuantityPolicy::Normal,
        )
        .unwrap();
        assert_eq!(long.limit_price, Some(d("1.23")));
        assert_eq!(long.stop_trigger_price, Some(d("1.21")));
        let short = quantize_order(
            &s,
            Side::Sell,
            1.0,
            limit(1.231),
            Some(StopSpec { trigger_px: 1.299 }),
            Some(1.2),
            QuantityPolicy::Normal,
        )
        .unwrap();
        assert_eq!(short.limit_price, Some(d("1.24")));
        assert_eq!(short.stop_trigger_price, Some(d("1.29")));
        for (side, stop, reference) in [(Side::Buy, 1.201, 1.21), (Side::Sell, 1.299, 1.29)] {
            assert!(quantize_order(
                &s,
                side,
                1.0,
                OrderKind::Market,
                Some(StopSpec { trigger_px: stop }),
                Some(reference),
                QuantityPolicy::Normal
            )
            .is_err());
        }
    }
    #[test]
    fn price_bounds_notional_and_missing_reference_do_not_default_to_legal() {
        let mut s = spec();
        s.min_price = Some(d("1"));
        s.max_price = Some(d("2"));
        s.min_notional = Some(d("1"));
        assert!(quantize_order(
            &s,
            Side::Buy,
            0.1,
            OrderKind::Market,
            None,
            None,
            QuantityPolicy::Normal
        )
        .is_err());
        assert!(quantize_order(
            &s,
            Side::Buy,
            0.1,
            limit(1.0),
            None,
            None,
            QuantityPolicy::Normal
        )
        .is_err());
        for px in [0.99, 2.01] {
            assert!(quantize_order(
                &s,
                Side::Buy,
                1.0,
                limit(px),
                None,
                None,
                QuantityPolicy::Normal
            )
            .is_err());
        }
        let closed = quantize_order(
            &s,
            Side::Sell,
            0.001,
            OrderKind::Market,
            None,
            None,
            QuantityPolicy::CloseEntirePosition,
        )
        .unwrap();
        assert_eq!(closed.quantity, d("0.001"));
    }
    #[test]
    fn significant_figures_apply_after_power_of_ten_carry_and_keep_integer_exception() {
        let mut s = spec();
        s.price_precision = PricePrecision::SignificantFigures {
            max_digits: 5,
            max_decimals: 4,
            integer_exception: true,
        };
        for (px, buy, sell) in [
            ("12.3456", "12.345", "12.346"),
            ("9999.91", "9999.9", "10000"),
            ("123456.7", "123456", "123457"),
            ("0.012345", "0.0123", "0.0124"),
        ] {
            assert_eq!(quantize_price(&d(px), Side::Buy, &s).unwrap(), d(buy));
            assert_eq!(quantize_price(&d(px), Side::Sell, &s).unwrap(), d(sell));
        }
    }
    #[test]
    fn projection_retains_sleeve_stop_when_physical_stop_is_stripped_and_detects_drift() {
        let terms = quantize_order(
            &spec(),
            Side::Buy,
            0.29,
            limit(1.239),
            Some(StopSpec { trigger_px: 1.201 }),
            Some(1.3),
            QuantityPolicy::Normal,
        )
        .unwrap()
        .with_physical_stop(None)
        .unwrap();
        let mut request = OrderRequest {
            exact_terms: None,
            sleeve_effect: Some(SleeveOrderEffect::Increase {
                stop: StopSpec { trigger_px: 1.201 },
            }),
            client_order_id: "id".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.29,
            kind: limit(1.239),
            stop: None,
            reduce_only: true,
            close_position: false,
        };
        terms.apply_projection(&mut request).unwrap();
        assert_eq!(request.sleeve_stop().unwrap().trigger_px, 1.21);
        assert!(request.stop.is_none());
        let restored: OrderRequest =
            serde_json::from_str(&serde_json::to_string(&request).unwrap()).unwrap();
        terms.validate_projection(&restored).unwrap();
        request.qty = 0.3;
        assert!(terms.validate_projection(&request).is_err());
        let mut legacy = serde_json::to_value(restored).unwrap();
        legacy.as_object_mut().unwrap().remove("exact_terms");
        assert!(serde_json::from_value::<OrderRequest>(legacy)
            .unwrap()
            .exact_terms
            .is_none());
    }

    #[test]
    fn rounded_stop_must_clear_the_quantized_limit_as_well_as_current_reference() {
        for (side, limit_px, stop, current) in [
            (Side::Buy, 1.20, 1.199, 1.30),
            (Side::Sell, 1.20, 1.201, 1.10),
        ] {
            assert!(
                quantize_order(
                    &spec(),
                    side,
                    1.0,
                    limit(limit_px),
                    Some(StopSpec { trigger_px: stop }),
                    Some(current),
                    QuantityPolicy::Normal
                )
                .is_err(),
                "rounded stop reached the entry price"
            );
        }
    }
}
