use super::markets::Market;
use crate::json::{int_field, num_field, str_field};
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
    let rows = reply
        .get("order_book_details")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("no order_book_details in the reply".to_string()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        // A market the venue is not currently running is not one to trade.
        let status = row
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("active");
        if !status.eq_ignore_ascii_case("active") {
            continue;
        }
        out.push(Market {
            symbol: str_field(row, "symbol")?,
            index: i16::try_from(int_field(row, "market_id")?).map_err(|_| {
                VenueError::BadReply(
                    "a market index does not fit the venue's own field".to_string(),
                )
            })?,
            size_decimals: u32::try_from(int_field(row, "supported_size_decimals")?)
                .map_err(|_| VenueError::BadReply("negative size decimals".to_string()))?,
            price_decimals: u32::try_from(int_field(row, "supported_price_decimals")?)
                .map_err(|_| VenueError::BadReply("negative price decimals".to_string()))?,
            min_base_amount: num_field(row, "min_base_amount")?,
            min_quote_amount: num_field(row, "min_quote_amount")?,
        });
    }
    if out.is_empty() {
        return Err(VenueError::BadReply(
            "the venue listed no active markets".to_string(),
        ));
    }
    Ok(out)
}
