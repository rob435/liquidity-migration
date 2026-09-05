use engine_public::numeric_wire::{decode_object, DecimalField};
use engine_types::orders::{OrderLookup, TerminalOrderStatus};
use engine_types::VenueError;
use serde::Deserialize;
use serde_json::value::RawValue;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Reply {
    ret_code: i64,
    #[serde(default)]
    ret_msg: String,
    #[serde(default)]
    result: Option<Box<RawValue>>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Page {
    category: String,
    list: Vec<Box<RawValue>>,
    #[serde(default)]
    next_page_cursor: String,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Row {
    symbol: String,
    order_link_id: String,
    order_id: String,
    order_status: String,
    cum_exec_qty: DecimalField,
}

pub(crate) fn parse(
    raw: &str,
    symbol: &str,
    client_id: &str,
) -> Result<Option<OrderLookup>, VenueError> {
    let reply: Reply = decode_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if reply.ret_code != 0 {
        return Err(VenueError::Rejected {
            code: reply.ret_code,
            message: reply.ret_msg,
        });
    }
    let data = reply
        .result
        .ok_or_else(|| VenueError::BadReply("order lookup carries no result".into()))?;
    let page: Page = decode_object(data.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if page.category != "linear" || !page.next_page_cursor.is_empty() || page.list.len() > 1 {
        return Err(VenueError::BadReply(
            "order lookup has foreign category or ambiguous pagination".into(),
        ));
    }
    let Some(raw) = page.list.first() else {
        return Ok(None);
    };
    let decoded: Row = decode_object(raw.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if decoded.symbol != symbol || decoded.order_link_id != client_id {
        return Err(VenueError::BadReply(
            "order lookup identity differs from request".into(),
        ));
    }
    let row = crate::order_lookup::row(
        decoded.symbol,
        decoded.order_link_id,
        decoded.order_id,
        decoded.cum_exec_qty.required("cumExecQty")?,
    )?;
    let lookup = match decoded.order_status.as_str() {
        "Created" | "New" | "PartiallyFilled" | "Untriggered" | "Triggered" | "Active"
        | "PendingCancel" => OrderLookup::Working(row),
        "Filled" => crate::order_lookup::terminal(TerminalOrderStatus::Filled, row),
        "Cancelled" | "PartiallyFilledCanceled" | "PartiallyFilledCancelled" | "Deactivated" => {
            crate::order_lookup::terminal(TerminalOrderStatus::Cancelled, row)
        }
        "Rejected" => crate::order_lookup::terminal(TerminalOrderStatus::Rejected, row),
        status => crate::order_lookup::unknown(format!("unknown Bybit order status {status:?}")),
    };
    Ok(Some(lookup))
}
