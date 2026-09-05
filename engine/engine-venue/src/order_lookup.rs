use engine_types::numeric::ExactNumber;
use engine_types::orders::{OrderLookup, OrderLookupRow, TerminalOrderStatus};
use engine_types::VenueError;

pub(crate) fn unknown(reason: impl Into<String>) -> OrderLookup {
    OrderLookup::Unknown {
        reason: reason.into(),
    }
}

pub(crate) fn row(
    symbol: String,
    client_order_id: String,
    venue_order_id: String,
    filled_qty: ExactNumber,
) -> Result<OrderLookupRow, VenueError> {
    if symbol.is_empty()
        || client_order_id.is_empty()
        || venue_order_id.is_empty()
        || filled_qty.value.is_negative()
    {
        return Err(VenueError::BadReply(
            "invalid order lookup identity or filled quantity".into(),
        ));
    }
    filled_qty
        .validate_provenance()
        .map_err(|e| VenueError::BadReply(e.to_string()))?;
    Ok(OrderLookupRow {
        symbol,
        client_order_id,
        venue_order_id,
        filled_qty,
    })
}

pub(crate) fn terminal(status: TerminalOrderStatus, row: OrderLookupRow) -> OrderLookup {
    OrderLookup::Terminal { status, row }
}
