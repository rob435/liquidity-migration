use engine_public::numeric_wire::{decode_object, DecimalField, IntegerField};
use engine_types::orders::{OrderLookup, TerminalOrderStatus};
use engine_types::VenueError;
use serde::Deserialize;
use serde_json::value::RawValue;

#[derive(Deserialize)]
struct Reply {
    code: i64,
    #[serde(default)]
    message: String,
    #[serde(default)]
    orders: Option<Vec<Box<RawValue>>>,
}
#[derive(Deserialize)]
struct Row {
    order_id: crate::wire::Id,
    client_order_index: IntegerField,
    market_index: IntegerField,
    owner_account_index: IntegerField,
    filled_base_amount: DecimalField,
    status: String,
}

pub(crate) fn parse(
    raw: &str,
    symbol: &str,
    client_id: &str,
    market: i16,
    account: i64,
) -> Result<OrderLookup, VenueError> {
    let reply: Reply = decode_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if reply.code != 200 {
        return Err(VenueError::Rejected {
            code: reply.code,
            message: reply.message,
        });
    }
    let rows = reply
        .orders
        .ok_or_else(|| VenueError::BadReply("order lookup carries no orders".into()))?;
    if rows.len() > 1 {
        return Err(VenueError::BadReply(
            "exact client order lookup returned multiple rows".into(),
        ));
    }
    let Some(raw) = rows.first() else {
        return Ok(crate::order_lookup::unknown(
            "Lighter client lookup retains only a bounded recent order window",
        ));
    };
    let decoded: Row = decode_object(raw.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if decoded.client_order_index.required("client_order_index")?
        != super::order_index::to_index(client_id)
        || decoded.market_index.required("market_index")? != i64::from(market)
        || decoded
            .owner_account_index
            .required("owner_account_index")?
            != account
    {
        return Err(VenueError::BadReply(
            "order lookup identity differs from request".into(),
        ));
    }
    let row = crate::order_lookup::row(
        symbol.into(),
        client_id.into(),
        decoded.order_id.into_text(),
        decoded.filled_base_amount.required("filled_base_amount")?,
    )?;
    Ok(match decoded.status.as_str() {
        "open" | "pending" => OrderLookup::Working(row),
        "filled" => crate::order_lookup::terminal(TerminalOrderStatus::Filled, row),
        "canceled" => crate::order_lookup::terminal(TerminalOrderStatus::Cancelled, row),
        "rejected" => crate::order_lookup::terminal(TerminalOrderStatus::Rejected, row),
        status => crate::order_lookup::unknown(format!("unknown Lighter order status {status:?}")),
    })
}
