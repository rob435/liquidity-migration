use engine_types::numeric::Exact;
use engine_types::order_terms::{decimal_wire, ExactOrderTerms, OrderLegalityError};
use engine_types::{OrderRequest, VenueError};

pub(crate) fn error(error: impl std::fmt::Display) -> VenueError {
    VenueError::BadRequest(error.to_string())
}
pub(crate) fn terms(request: &OrderRequest) -> Result<Option<&ExactOrderTerms>, VenueError> {
    request
        .exact_terms
        .as_deref()
        .map(|terms| {
            terms.validate_projection(request).map_err(error)?;
            Ok(terms)
        })
        .transpose()
}
pub(crate) fn quantity(request: &OrderRequest) -> Result<String, VenueError> {
    match terms(request)? {
        Some(terms) => decimal_wire(&terms.quantity).map_err(error),
        None => crate::fmt::venue_num(request.qty),
    }
}
pub(crate) fn price(request: &OrderRequest, legacy: f64) -> Result<String, VenueError> {
    match terms(request)? {
        Some(terms) => decimal_wire(
            terms
                .limit_price
                .as_ref()
                .ok_or_else(|| error(OrderLegalityError::Projection))?,
        )
        .map_err(error),
        None => crate::fmt::venue_num(legacy),
    }
}
pub(crate) fn stop(request: &OrderRequest, legacy: f64) -> Result<String, VenueError> {
    match terms(request)? {
        Some(terms) => decimal_wire(
            terms
                .physical_stop_trigger_price
                .as_ref()
                .ok_or_else(|| error(OrderLegalityError::Projection))?,
        )
        .map_err(error),
        None => crate::fmt::venue_num(legacy),
    }
}
pub(crate) fn scaled(value: &Exact, decimals: u32, max: u64) -> Result<u64, VenueError> {
    if decimals > 4096 {
        return Err(error("integer scale is out of range"));
    }
    let step = Exact::parse_decimal(&format!("1e-{decimals}")).map_err(error)?;
    let integer = value
        .checked_div(&step)
        .and_then(|value| value.to_u64_exact())
        .map_err(error)?;
    if integer == 0 || integer > max {
        return Err(error("exact amount is outside venue integer field"));
    }
    Ok(integer)
}
