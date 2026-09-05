use engine_public::numeric_wire::{decode_object, DecimalField};
use engine_types::numeric::ExactNumber;
use engine_types::orders::{OrderLookup, TerminalOrderStatus};
use engine_types::VenueError;
use serde::Deserialize;
use serde_json::value::RawValue;

#[derive(Deserialize)]
struct Reply {
    status: String,
    #[serde(default)]
    order: Option<Box<RawValue>>,
}
#[derive(Deserialize)]
struct Status {
    status: String,
    order: Box<RawValue>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Row {
    coin: String,
    cloid: Option<String>,
    oid: crate::wire::Id,
    sz: DecimalField,
    orig_sz: DecimalField,
}

pub(crate) fn parse(raw: &str, symbol: &str, client_id: &str) -> Result<OrderLookup, VenueError> {
    let reply: Reply = decode_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if reply.status == "unknownOid" {
        return Ok(crate::order_lookup::unknown(
            "Hyperliquid has no retained order for this cloid",
        ));
    }
    if reply.status != "order" {
        return Ok(crate::order_lookup::unknown(format!(
            "unknown Hyperliquid lookup status {:?}",
            reply.status
        )));
    }
    let data = reply
        .order
        .ok_or_else(|| VenueError::BadReply("order lookup carries no order".into()))?;
    let status: Status =
        decode_object(data.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
    let decoded: Row =
        decode_object(status.order.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if !decoded
        .coin
        .eq_ignore_ascii_case(&super::assets::coin_of(symbol))
        || !decoded
            .cloid
            .as_deref()
            .is_some_and(|value| value.eq_ignore_ascii_case(&super::cloid::to_cloid(client_id)))
    {
        return Err(VenueError::BadReply(
            "order lookup identity differs from request".into(),
        ));
    }
    let original = decoded.orig_sz.required("origSz")?.value;
    let remaining = decoded.sz.required("sz")?.value;
    if original.is_negative() || remaining.is_negative() || remaining > original {
        return Err(VenueError::BadReply(
            "order lookup quantities are inconsistent".into(),
        ));
    }
    let row = crate::order_lookup::row(
        symbol.into(),
        client_id.into(),
        decoded.oid.into_text(),
        ExactNumber::derived(original - remaining),
    )?;
    Ok(match status.status.as_str() {
        "open" | "triggered" => OrderLookup::Working(row),
        "filled" => crate::order_lookup::terminal(TerminalOrderStatus::Filled, row),
        "canceled"
        | "marginCanceled"
        | "vaultWithdrawalCanceled"
        | "openInterestCapCanceled"
        | "selfTradeCanceled"
        | "reduceOnlyCanceled"
        | "siblingFilledCanceled"
        | "delistedCanceled"
        | "liquidatedCanceled"
        | "scheduledCancel" => crate::order_lookup::terminal(TerminalOrderStatus::Cancelled, row),
        "rejected"
        | "tickRejected"
        | "minTradeNtlRejected"
        | "perpMarginRejected"
        | "reduceOnlyRejected"
        | "badAloPxRejected"
        | "iocCancelRejected"
        | "badTriggerPxRejected"
        | "marketOrderNoLiquidityRejected"
        | "positionIncreaseAtOpenInterestCapRejected"
        | "positionFlipAtOpenInterestCapRejected"
        | "tooAggressiveAtOpenInterestCapRejected"
        | "openInterestIncreaseRejected"
        | "insufficientSpotBalanceRejected"
        | "oracleRejected"
        | "perpMaxPositionRejected" => {
            crate::order_lookup::terminal(TerminalOrderStatus::Rejected, row)
        }
        status => {
            crate::order_lookup::unknown(format!("unknown Hyperliquid order status {status:?}"))
        }
    })
}
