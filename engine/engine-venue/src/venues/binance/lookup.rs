use engine_public::numeric_wire::{decode_object, DecimalField};
use engine_types::orders::{OrderLookup, TerminalOrderStatus};
use engine_types::VenueError;
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Row {
    symbol: String,
    client_order_id: String,
    order_id: crate::wire::Id,
    status: String,
    executed_qty: DecimalField,
}

pub(crate) fn parse(raw: &str, symbol: &str, client_id: &str) -> Result<OrderLookup, VenueError> {
    let decoded: Row = decode_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if decoded.symbol != symbol || decoded.client_order_id != client_id {
        return Err(VenueError::BadReply(
            "order lookup identity differs from request".into(),
        ));
    }
    let row = crate::order_lookup::row(
        decoded.symbol,
        decoded.client_order_id,
        decoded.order_id.into_text(),
        decoded.executed_qty.required("executedQty")?,
    )?;
    Ok(match decoded.status.as_str() {
        "NEW" | "PARTIALLY_FILLED" | "PENDING_CANCEL" => OrderLookup::Working(row),
        "FILLED" => crate::order_lookup::terminal(TerminalOrderStatus::Filled, row),
        "CANCELED" | "EXPIRED" | "EXPIRED_IN_MATCH" => {
            crate::order_lookup::terminal(TerminalOrderStatus::Cancelled, row)
        }
        "REJECTED" => crate::order_lookup::terminal(TerminalOrderStatus::Rejected, row),
        status => crate::order_lookup::unknown(format!("unknown Binance order status {status:?}")),
    })
}
