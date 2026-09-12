#[cfg(feature = "lighter")]
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
/// An order term that reached the wire without the exact decimal it was
/// quantized from.
///
/// The engine does not produce one. It refuses a symbol whose exact
/// instrument metadata it does not hold (`EXACT_INSTRUMENT_METADATA_
/// UNAVAILABLE`), and every request it does admit carries the terms
/// `apply_projection` attached; the one path that built an order without them
/// was `#[cfg(test)]`. Refusing rather than formatting a float makes that a
/// property of this boundary instead of a property of the caller.
fn without_terms(field: &'static str) -> Result<String, VenueError> {
    Err(error(format!(
        "order {field} reached the wire with no exact term to send"
    )))
}
pub(crate) fn quantity(request: &OrderRequest) -> Result<String, VenueError> {
    match terms(request)? {
        Some(terms) => decimal_wire(&terms.quantity).map_err(error),
        None => without_terms("quantity"),
    }
}
pub(crate) fn price(request: &OrderRequest) -> Result<String, VenueError> {
    match terms(request)? {
        Some(terms) => decimal_wire(
            terms
                .limit_price
                .as_ref()
                .ok_or_else(|| error(OrderLegalityError::Projection))?,
        )
        .map_err(error),
        None => without_terms("price"),
    }
}
pub(crate) fn stop(request: &OrderRequest) -> Result<String, VenueError> {
    match terms(request)? {
        Some(terms) => decimal_wire(
            terms
                .physical_stop_trigger_price
                .as_ref()
                .ok_or_else(|| error(OrderLegalityError::Projection))?,
        )
        .map_err(error),
        None => without_terms("stop trigger"),
    }
}
#[cfg(feature = "lighter")]
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

pub(crate) fn amend_terms(
    spec: &engine_types::AmendSpec,
) -> Result<Option<&engine_types::order_terms::ExactAmendTerms>, VenueError> {
    spec.exact_terms
        .as_deref()
        .map(|terms| {
            terms.validate_projection(spec).map_err(error)?;
            Ok(terms)
        })
        .transpose()
}
pub(crate) fn amend_price(spec: &engine_types::AmendSpec) -> Result<Option<String>, VenueError> {
    match amend_terms(spec)? {
        Some(terms) => terms
            .limit_price
            .as_ref()
            .map(|value| decimal_wire(value).map_err(error))
            .transpose(),
        None => spec.px.map(|_| without_terms("amend price")).transpose(),
    }
}
pub(crate) fn amend_quantity(spec: &engine_types::AmendSpec) -> Result<Option<String>, VenueError> {
    match amend_terms(spec)? {
        Some(terms) => terms
            .quantity
            .as_ref()
            .map(|value| decimal_wire(value).map_err(error))
            .transpose(),
        None => spec
            .qty
            .map(|_| without_terms("amend quantity"))
            .transpose(),
    }
}
