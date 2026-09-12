use super::markets::Market;
use engine_types::VenueError;
use serde_json::Value;

/// The only `code` that means success.
const CODE_OK: i64 = 200;

/// Check the envelope every reply shares.
pub fn venue_result(reply: Value) -> Result<Value, VenueError> {
    let code = reply
        .get("code")
        .and_then(Value::as_i64)
        .ok_or_else(|| VenueError::BadReply("reply carries no code".to_string()))?;
    if code != CODE_OK {
        let message = reply
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("no message")
            .to_string();
        return Err(VenueError::Rejected { code, message });
    }
    Ok(reply)
}

/// The venue's tradable markets out of an `orderBookDetails` reply.
pub fn parse_markets(reply: &Value) -> Result<Vec<Market>, VenueError> {
    let mut reply = reply.clone();
    if let Some(object) = reply.as_object_mut() {
        object.insert("code".into(), Value::from(200));
    }
    parse_markets_raw(&reply.to_string())
}

pub fn parse_markets_raw(raw: &str) -> Result<Vec<Market>, VenueError> {
    use crate::numeric_wire::{decode_object, DecimalField, IntegerField};
    use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, PricePrecision};
    use serde::Deserialize;
    #[derive(Deserialize)]
    struct Reply {
        code: i64,
        #[serde(default)]
        message: String,
        #[serde(default)]
        order_book_details: Option<Vec<Box<serde_json::value::RawValue>>>,
    }
    #[derive(Deserialize)]
    struct Row {
        symbol: String,
        market_id: IntegerField,
        #[serde(default)]
        status: Option<String>,
        supported_size_decimals: IntegerField,
        supported_price_decimals: IntegerField,
        min_base_amount: DecimalField,
        min_quote_amount: DecimalField,
    }
    let reply: Reply = decode_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if reply.code != CODE_OK {
        return Err(VenueError::Rejected {
            code: reply.code,
            message: reply.message,
        });
    }
    let rows = reply
        .order_book_details
        .ok_or_else(|| VenueError::BadReply("no order_book_details in the reply".into()))?;
    let mut out = Vec::with_capacity(rows.len());
    for raw in rows {
        let row: Row = decode_object(raw.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
        if !row
            .status
            .as_deref()
            .unwrap_or("active")
            .eq_ignore_ascii_case("active")
        {
            continue;
        }
        let size_decimals = u32::try_from(
            row.supported_size_decimals
                .required("supported_size_decimals")?,
        )
        .map_err(|_| VenueError::BadReply("negative size decimals".into()))?;
        let price_decimals = u32::try_from(
            row.supported_price_decimals
                .required("supported_price_decimals")?,
        )
        .map_err(|_| VenueError::BadReply("negative price decimals".into()))?;
        let qty_step = Exact::parse_decimal(&format!("1e-{size_decimals}"))
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        let tick_size = Exact::parse_decimal(&format!("1e-{price_decimals}"))
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        qty_step
            .to_f64()
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        tick_size
            .to_f64()
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        for step in [&qty_step, &tick_size] {
            Exact::one()
                .checked_div(step)
                .and_then(|scale| scale.to_f64())
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
        }
        let min_qty = row.min_base_amount.required("min_base_amount")?.value;
        let max_qty = &qty_step * Exact::from_u64((1u64 << 48) - 1);
        let spec = ExactInstrumentSpec {
            native_symbol: row.symbol.clone(),
            base_asset: AssetId::Named(row.symbol.clone()),
            quote_asset: AssetId::Unknown,
            settlement_asset: AssetId::Unknown,
            min_price: Some(tick_size.clone()),
            max_price: Some(&tick_size * Exact::from_u64(u32::MAX.into())),
            tick_size: Some(tick_size),
            price_precision: PricePrecision::Tick,
            qty_step: Some(qty_step.clone()),
            market_qty_step: Some(qty_step),
            min_qty: Some(min_qty.clone()),
            market_min_qty: Some(min_qty),
            max_qty: Some(max_qty.clone()),
            max_market_qty: Some(max_qty),
            min_notional: Some(row.min_quote_amount.required("min_quote_amount")?.value),
            contract_multiplier: Some(Exact::one()),
            fee_assets: None,
            fee_step: None,
        };
        out.push(Market {
            symbol: row.symbol,
            index: i16::try_from(row.market_id.required("market_id")?).map_err(|_| {
                VenueError::BadReply("a market index does not fit the venue's own field".into())
            })?,
            size_decimals,
            price_decimals,
            exact_spec: Some(spec),
        });
    }
    if out.is_empty() {
        return Err(VenueError::BadReply(
            "the venue listed no active markets".into(),
        ));
    }
    Ok(out)
}

#[cfg(test)]
mod precision_tests {
    use super::*;
    #[test]
    fn decimal_metadata_must_have_a_finite_compatibility_scale() {
        let reply = serde_json::json!({"code":200,"order_book_details":[{
            "symbol":"BTC","market_id":0,"supported_size_decimals":309,
            "supported_price_decimals":1,"min_base_amount":"1e-309","min_quote_amount":"10"
        }]});
        assert!(
            parse_markets(&reply).is_err(),
            "a representable step hid an infinite inverse scale"
        );
    }

    #[test]
    fn enormous_decimal_metadata_is_refused_before_it_can_wrap_an_integer_exponent() {
        let reply = serde_json::json!({"code":200,"order_book_details":[{
            "symbol":"BTC","market_id":0,"supported_size_decimals":4294967295u32,
            "supported_price_decimals":1,"min_base_amount":"1","min_quote_amount":"10"
        }]});
        assert!(
            parse_markets(&reply).is_err(),
            "unbounded size decimals reached integer scaling"
        );
    }
}
